import base64
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from bewaesserung.fyta_client import FytaClient, parse_fyta_zeitstempel
from bewaesserung.modelle import DatenQuelle, FytaKonfig, FytaPflanzenKonfig


# T-0176: parse_fyta_zeitstempel — UTC-Konvertierung -----------------------


def test_t0176_parse_fyta_zeitstempel_naive_string_als_utc():
    """FYTA `date_utc`-Feld kommt als naiver ISO-String ohne Suffix.
    Helper muss das als UTC interpretieren und in lokale Zone konvertieren.
    Sommer (CEST = UTC+2): 09:16:50 UTC -> 11:16:50 lokal.
    """
    erwartet_utc = datetime(2026, 5, 10, 9, 16, 50, tzinfo=timezone.utc)
    erwartet_lokal = erwartet_utc.astimezone().replace(tzinfo=None)
    assert parse_fyta_zeitstempel("2026-05-10T09:16:50") == erwartet_lokal


def test_t0176_parse_fyta_zeitstempel_mit_z_suffix():
    """Z-Suffix wird auch als UTC erkannt (zukunftssicher falls FYTA umstellt)."""
    erwartet = datetime(2026, 5, 10, 9, 16, 50,
                        tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert parse_fyta_zeitstempel("2026-05-10T09:16:50Z") == erwartet


def test_t0176_parse_fyta_zeitstempel_mit_offset():
    """+00:00-Suffix wird ebenfalls als UTC interpretiert."""
    erwartet = datetime(2026, 5, 10, 9, 16, 50,
                        tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert parse_fyta_zeitstempel("2026-05-10T09:16:50+00:00") == erwartet


def test_t0176_parse_fyta_zeitstempel_ungueltig_wirft_value_error():
    with pytest.raises(ValueError):
        parse_fyta_zeitstempel("not-a-date")


# --- Bestand FytaClient-Tests --------------------------------------------


class FakeAsyncClient:
    letzte_json: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url: str, headers: dict, json: dict) -> httpx.Response:
        FakeAsyncClient.letzte_json = json
        return httpx.Response(
            200,
            json={
                "user_plants": [
                    {
                        "user_plant_id": 100001,
                        "measurements": [
                            {
                                "date_utc": "2026-04-09T22:15:00",
                                "soil_moisture": 41,
                                "temperature": 19,
                                "light": 8,
                                "soil_fertility": 0,
                            },
                            {
                                "date_utc": "2026-04-10T08:30:00",
                                "soil_moisture": 43,
                                "temperature": 20,
                                "light": 10,
                                "soil_fertility": 0,
                            },
                        ],
                    }
                ]
            },
            request=httpx.Request("POST", url),
        )


@pytest.mark.asyncio
async def test_hole_aktuelle_werte_fragt_gestern_bis_heute_ab(monkeypatch):
    monkeypatch.setattr("bewaesserung.fyta_client.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("bewaesserung.fyta_client.date", _FakeDate)
    client = FytaClient(
        FytaKonfig(
            api_url="https://fyta.example/api",
            pflanzen=[
                FytaPflanzenKonfig(fyta_id=100001, zone_id="zitrus", name="Zitrobaer"),
            ],
        ),
        access_token="token",
    )

    messungen = await client.hole_aktuelle_werte()

    assert FakeAsyncClient.letzte_json == {
        "userPlantIds": [100001],
        "scanFromDate": "2026-04-09",
        "scanToDate": "2026-04-10",
    }
    # T-0166: ALLE Eintraege werden uebernommen, nicht nur der letzte.
    # FakeAsyncClient mockt 2 measurements -> beide kommen.
    # T-0176: Zeitstempel werden von UTC nach lokal konvertiert.
    assert len(messungen) == 2
    assert all(m.quelle == DatenQuelle.FYTA for m in messungen)
    assert all(m.zone_id == "zitrus" for m in messungen)
    erwartet = sorted([
        datetime(2026, 4, 9, 22, 15, 0,
                 tzinfo=timezone.utc).astimezone().replace(tzinfo=None),
        datetime(2026, 4, 10, 8, 30, 0,
                 tzinfo=timezone.utc).astimezone().replace(tzinfo=None),
    ])
    assert sorted(m.zeitstempel for m in messungen) == erwartet


@pytest.mark.asyncio
async def test_t0166_dedup_zweiter_poll_liefert_keine_doppelung(monkeypatch):
    """T-0166: Zweiter Aufruf liefert dieselben measurements zurueck —
    der In-Memory-Dedup muss sie als bereits gesehen erkennen und
    keine erneuten SensorMessung-Objekte erzeugen."""
    monkeypatch.setattr("bewaesserung.fyta_client.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("bewaesserung.fyta_client.date", _FakeDate)
    client = FytaClient(
        FytaKonfig(
            api_url="https://fyta.example/api",
            pflanzen=[
                FytaPflanzenKonfig(fyta_id=100001, zone_id="zitrus", name="Zitrobaer"),
            ],
        ),
        access_token="token",
    )
    erste = await client.hole_aktuelle_werte()
    assert len(erste) == 2
    # Zweiter Poll: gleicher Mock-Response, aber Dedup blockt.
    zweite = await client.hole_aktuelle_werte()
    assert len(zweite) == 0


@pytest.mark.asyncio
async def test_t0166_dedup_nur_neuere_einsehen(monkeypatch):
    """T-0166: Wenn FYTA einen NEUEREN Eintrag dazu liefert, muss der
    durchkommen, die alten gefilterten bleiben gefiltert."""
    class FakeAsyncClientErweitert:
        ruf: int = 0

        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None

        async def post(self, url: str, headers: dict, json: dict) -> httpx.Response:
            FakeAsyncClientErweitert.ruf += 1
            measurements = [
                {"date_utc": "2026-04-09T22:15:00", "soil_moisture": 41,
                 "temperature": 19, "light": 8, "soil_fertility": 0},
                {"date_utc": "2026-04-10T08:30:00", "soil_moisture": 43,
                 "temperature": 20, "light": 10, "soil_fertility": 0},
            ]
            if FakeAsyncClientErweitert.ruf >= 2:
                measurements.append({
                    "date_utc": "2026-04-10T12:00:00", "soil_moisture": 45,
                    "temperature": 21, "light": 12, "soil_fertility": 0,
                })
            return httpx.Response(
                200,
                json={"user_plants": [{
                    "user_plant_id": 100001, "measurements": measurements,
                }]},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("bewaesserung.fyta_client.httpx.AsyncClient", FakeAsyncClientErweitert)
    monkeypatch.setattr("bewaesserung.fyta_client.date", _FakeDate)
    client = FytaClient(
        FytaKonfig(
            api_url="https://fyta.example/api",
            pflanzen=[
                FytaPflanzenKonfig(fyta_id=100001, zone_id="zitrus", name="Z"),
            ],
        ),
        access_token="token",
    )
    erste = await client.hole_aktuelle_werte()
    assert len(erste) == 2
    zweite = await client.hole_aktuelle_werte()
    # Nur der neue (12:00 UTC = 14:00 CEST) Eintrag kommt durch.
    # T-0176: parse_fyta_zeitstempel konvertiert UTC -> lokale Zone.
    assert len(zweite) == 1
    erwartet_lokal = datetime(
        2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc,
    ).astimezone().replace(tzinfo=None)
    assert zweite[0].zeitstempel == erwartet_lokal
    assert zweite[0].boden_feuchte == 45.0


@pytest.mark.asyncio
async def test_t0224_multi_fyta_zone_sensoren_hungern_sich_nicht_aus(monkeypatch):
    """T-0224: Eine Zone mit ZWEI FYTA-Sensoren (Realfall waldblumenhain).
    Der In-Memory-Dedup darf NICHT zonenweit greifen -- sonst hebt der
    Sensor mit den spaeteren Zeitstempeln die gemeinsame Schwelle, und
    die Messungen des anderen Sensors gelten faelschlich als 'nicht neu'.

    Aufbau: Sensor A (100003) liefert SPAETERE Zeitstempel und steht
    zuerst in `user_plants`; Sensor B (100004) FRUEHERE. Mit dem alten
    zone_id-Dedup wuerde B komplett wegfallen (0 statt 2 Messungen).
    """
    class FakeMultiSensor:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers, json) -> httpx.Response:
            return httpx.Response(
                200,
                json={"user_plants": [
                    {   # Sensor A -- spaetere Zeitstempel, zuerst gelistet.
                        "user_plant_id": 100003,
                        "measurements": [
                            {"date_utc": "2026-04-10T12:00:00", "soil_moisture": 50,
                             "temperature": 20, "light": 9, "soil_fertility": 0},
                            {"date_utc": "2026-04-10T14:00:00", "soil_moisture": 51,
                             "temperature": 21, "light": 9, "soil_fertility": 0},
                        ],
                    },
                    {   # Sensor B -- fruehere Zeitstempel als A.
                        "user_plant_id": 100004,
                        "measurements": [
                            {"date_utc": "2026-04-10T08:00:00", "soil_moisture": 47,
                             "temperature": 19, "light": 7, "soil_fertility": 0},
                            {"date_utc": "2026-04-10T09:00:00", "soil_moisture": 47,
                             "temperature": 19, "light": 7, "soil_fertility": 0},
                        ],
                    },
                ]},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("bewaesserung.fyta_client.httpx.AsyncClient", FakeMultiSensor)
    monkeypatch.setattr("bewaesserung.fyta_client.date", _FakeDate)
    client = FytaClient(
        FytaKonfig(
            api_url="https://fyta.example/api",
            pflanzen=[
                FytaPflanzenKonfig(
                    fyta_id=100003, zone_id="waldblumenhain", name="Waldblumen A"),
                FytaPflanzenKonfig(
                    fyta_id=100004, zone_id="waldblumenhain", name="Waldblumen D-Rand"),
            ],
        ),
        access_token="token",
    )

    messungen = await client.hole_aktuelle_werte()

    # Beide Sensoren muessen ihre Messungen liefern -- keiner hungert aus.
    pro_geraet: dict[str, list] = {}
    for m in messungen:
        pro_geraet.setdefault(m.geraet_id, []).append(m)
    assert set(pro_geraet) == {"fyta_100003", "fyta_100004"}
    assert len(pro_geraet["fyta_100003"]) == 2
    assert len(pro_geraet["fyta_100004"]) == 2

    # Zweiter Poll: gleicher Response -> beide Sensoren dedupen vollstaendig.
    zweite = await client.hole_aktuelle_werte()
    assert zweite == []


class _FakeDate(date):
    @classmethod
    def today(cls) -> "_FakeDate":
        return cls(2026, 4, 10)


# ---------- T-0072: Login-Flow, Auto-Refresh, Token-Cache ----------

def _route_client(routen: list[tuple[str, callable]]) -> type:
    """Erzeugt einen FakeAsyncClient, der Requests per URL-Substring auf
    Handler-Funktionen routet. Handler bekommt das httpx.Request und gibt
    ein httpx.Response zurueck. Die aufgezeichneten Requests landen in
    der Klassen-Variable `verlauf` fuer Asserts.
    """
    class RouteClient:
        verlauf: list[httpx.Request] = []

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def _dispatch(
            self, methode: str, url: str, **kwargs,
        ) -> httpx.Response:
            req = httpx.Request(methode, url, **{
                k: v for k, v in kwargs.items()
                if k in ("headers", "params", "json", "content", "data")
            })
            # Auth als separaten Parameter in Request-Header spiegeln
            auth = kwargs.get("auth")
            if auth is not None:
                user, pw = auth
                credential = base64.b64encode(f"{user}:{pw}".encode()).decode()
                req.headers["Authorization"] = f"Basic {credential}"
            RouteClient.verlauf.append(req)
            for muster, handler in routen:
                if muster in url:
                    antw = handler(req)
                    antw.request = req
                    return antw
            return httpx.Response(404, json={"fehler": "no route"}, request=req)

        async def get(self, url, headers=None, **kwargs):
            return await self._dispatch("GET", url, headers=headers or {}, **kwargs)

        async def post(self, url, headers=None, json=None, auth=None, **kwargs):
            return await self._dispatch(
                "POST", url, headers=headers or {}, json=json, auth=auth, **kwargs,
            )

    RouteClient.verlauf = []
    return RouteClient


def _login_handler(access_token: str = "neu-token", expires_in: int = 3600):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={
                "access_token": access_token,
                "expires_in": expires_in,
            },
        )
    return handler


@pytest.mark.asyncio
async def test_login_sendet_email_password_basic_plus_json(monkeypatch, tmp_path):
    """FYTA erwartet Basic-Auth-Header UND JSON-Body mit Email+Password."""
    Route = _route_client([
        ("/auth/login", _login_handler(access_token="fyta-token-neu")),
    ])
    monkeypatch.setattr("bewaesserung.fyta_client.httpx.AsyncClient", Route)
    monkeypatch.setenv("FYTA_EMAIL", "test@example.com")
    monkeypatch.setenv("FYTA_PASSWORD", "geheim123")
    monkeypatch.delenv("FYTA_ACCESS_TOKEN", raising=False)

    client = FytaClient(
        FytaKonfig(api_url="https://fyta.example/api"),
        token_pfad=tmp_path / "fyta_token.json",
    )
    erfolg = await client._login()
    assert erfolg is True

    assert len(Route.verlauf) == 1
    req = Route.verlauf[0]
    # Basic-Auth-Header
    assert req.headers["Authorization"].startswith("Basic ")
    decoded = base64.b64decode(
        req.headers["Authorization"][6:]
    ).decode()
    assert decoded == "test@example.com:geheim123"
    # JSON-Body
    body = json.loads(req.content.decode())
    assert body == {"email": "test@example.com", "password": "geheim123"}
    # Token persistiert
    persisted = json.loads((tmp_path / "fyta_token.json").read_text())
    assert persisted["access_token"] == "fyta-token-neu"


@pytest.mark.asyncio
async def test_login_ohne_credentials_failt_graceful(monkeypatch, tmp_path):
    """Fehlende Credentials → Login liefert False, kein Crash."""
    monkeypatch.delenv("FYTA_EMAIL", raising=False)
    monkeypatch.delenv("FYTA_PASSWORD", raising=False)
    monkeypatch.delenv("FYTA_ACCESS_TOKEN", raising=False)

    client = FytaClient(
        FytaKonfig(api_url="https://fyta.example/api"),
        token_pfad=tmp_path / "fyta_token.json",
    )
    assert await client._login() is False


@pytest.mark.asyncio
async def test_auto_refresh_bei_403_holt_neues_token_und_retryt(
    monkeypatch, tmp_path,
):
    """Gealteter Token → 403 beim GET → Client logt sich neu ein + retryt."""
    antworten = iter([
        httpx.Response(403, json={"error": "Forbidden"}),   # 1. Aufruf: alt
        httpx.Response(200, json={"plants": [{"id": 1}]}),  # nach reauth
    ])

    def plant_handler(req: httpx.Request) -> httpx.Response:
        return next(antworten)

    Route = _route_client([
        ("/auth/login", _login_handler(access_token="refreshed-token")),
        ("/user-plant", plant_handler),
    ])
    monkeypatch.setattr("bewaesserung.fyta_client.httpx.AsyncClient", Route)
    monkeypatch.setenv("FYTA_EMAIL", "test@example.com")
    monkeypatch.setenv("FYTA_PASSWORD", "geheim")
    monkeypatch.delenv("FYTA_ACCESS_TOKEN", raising=False)

    client = FytaClient(
        FytaKonfig(api_url="https://fyta.example/api"),
        access_token="alt-abgelaufen",
        token_pfad=tmp_path / "fyta_token.json",
    )
    # Ablauf manuell in die Zukunft setzen, damit _stelle_token_sicher
    # den Retry-Pfad erst nach der 403-Antwort triggert.
    client._token_ablauf = datetime.now() + timedelta(hours=1)

    pflanzen = await client.hole_pflanzen()
    assert pflanzen == [{"id": 1}]
    # Erwartete Reihenfolge: GET /user-plant (403) → POST /auth/login → GET /user-plant (200)
    urls = [r.url.path for r in Route.verlauf]
    assert urls == ["/api/user-plant", "/api/auth/login", "/api/user-plant"]
    # Zweiter GET traegt jetzt das frische Token.
    assert Route.verlauf[2].headers["Authorization"] == "Bearer refreshed-token"


def test_t0557_token_cache_hat_0600_rechte(monkeypatch, tmp_path):
    """Der Token-Cache traegt ein Bearer-Token und darf nicht weltlesbar sein.

    Vorher `write_text` ohne `chmod` -> 0644 (auf der Maschine nachgesehen:
    0644 hier, 0600 beim Gardena-Token daneben). Der Schreibpfad ist jetzt
    derselbe wie in `gardena_customer_auth._speichere_cache`: tmp, chmod,
    replace.
    """
    monkeypatch.delenv("FYTA_ACCESS_TOKEN", raising=False)
    cache = tmp_path / "fyta_token.json"
    client = FytaClient(
        FytaKonfig(api_url="https://fyta.example/api"), token_pfad=cache,
    )
    client._token = "geheim"
    client._token_ablauf = datetime.now() + timedelta(hours=2)
    client._speichere_token_cache()

    assert cache.is_file()
    assert json.loads(cache.read_text())["access_token"] == "geheim"
    assert (cache.stat().st_mode & 0o077) == 0, (
        f"Token-Cache ist fuer Gruppe/Andere lesbar: {cache.stat().st_mode:o}"
    )
    assert not cache.with_suffix(".tmp").exists(), "tmp-Datei blieb liegen"


@pytest.mark.asyncio
async def test_token_cache_wird_beim_start_geladen(monkeypatch, tmp_path):
    """Gespeichertes Token + Ablauf werden bei Init wieder eingelesen."""
    monkeypatch.delenv("FYTA_ACCESS_TOKEN", raising=False)
    cache = tmp_path / "fyta_token.json"
    ablauf = (datetime.now() + timedelta(hours=2)).isoformat()
    cache.write_text(json.dumps({
        "access_token": "persistiertes-token",
        "expires_at": ablauf,
    }))
    client = FytaClient(
        FytaKonfig(api_url="https://fyta.example/api"),
        token_pfad=cache,
    )
    assert client._token == "persistiertes-token"
    assert client._token_noch_gueltig() is True


@pytest.mark.asyncio
async def test_token_cache_laedt_kein_ausdruecklich_uebergebenes_token(
    monkeypatch, tmp_path,
):
    """Explizit uebergebenes access_token hat Vorrang vor dem Cache."""
    monkeypatch.delenv("FYTA_ACCESS_TOKEN", raising=False)
    cache = tmp_path / "fyta_token.json"
    cache.write_text(json.dumps({
        "access_token": "stale-cache",
        "expires_at": (datetime.now() + timedelta(hours=2)).isoformat(),
    }))
    client = FytaClient(
        FytaKonfig(api_url="https://fyta.example/api"),
        access_token="explizit",
        token_pfad=cache,
    )
    assert client._token == "explizit"


@pytest.mark.asyncio
async def test_abgelaufenes_token_triggert_login_vor_request(
    monkeypatch, tmp_path,
):
    """_stelle_token_sicher: Ablauf liegt in der Vergangenheit → Login."""
    Route = _route_client([
        ("/auth/login", _login_handler(access_token="frisch", expires_in=7200)),
        ("/user-plant", lambda r: httpx.Response(200, json={"plants": []})),
    ])
    monkeypatch.setattr("bewaesserung.fyta_client.httpx.AsyncClient", Route)
    monkeypatch.setenv("FYTA_EMAIL", "test@example.com")
    monkeypatch.setenv("FYTA_PASSWORD", "geheim")
    monkeypatch.delenv("FYTA_ACCESS_TOKEN", raising=False)

    client = FytaClient(
        FytaKonfig(api_url="https://fyta.example/api"),
        access_token="alt",
        token_pfad=tmp_path / "fyta_token.json",
    )
    # Ablauf in die Vergangenheit → _stelle_token_sicher muss einloggen.
    client._token_ablauf = datetime.now() - timedelta(seconds=1)

    await client.hole_pflanzen()
    urls = [r.url.path for r in Route.verlauf]
    assert urls[0] == "/api/auth/login", "Login muss VOR dem GET passieren"
    assert urls[1] == "/api/user-plant"
    assert Route.verlauf[1].headers["Authorization"] == "Bearer frisch"
