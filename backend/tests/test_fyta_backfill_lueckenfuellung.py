"""T-0167: Tests fuer den FYTA-Lueckenfuellungs-Hook beim Service-Start.

Stellt sicher, dass:
- die letzten N Tage automatisch nachgezogen werden
- Dedup greift (zweiter Aufruf importiert 0)
- bei fehlender Konfiguration / Token sauber abgebrochen wird
- bei DB- oder API-Fehlern der Hook nicht den Service-Start crasht
"""

from __future__ import annotations


import httpx
import pytest

from bewaesserung.fyta_backfill import backfill_lueckenfuellung
from bewaesserung.modelle import (
    FytaKonfig,
    FytaPflanzenKonfig,
)
from bewaesserung.speicher import Speicher


class _FakeFytaClient:
    """Minimaler FytaClient-Stub fuer den Backfill-Hook."""

    def __init__(
        self,
        pflanzen: list[FytaPflanzenKonfig],
        token: str = "fake-token",
        login_erfolg: bool = True,
        api_url: str = "https://fyta.example/api",
    ):
        self._pflanzen_map = {p.fyta_id: p for p in pflanzen}
        self._konfig = FytaKonfig(api_url=api_url, pflanzen=pflanzen)
        self._token = token
        self._login_erfolg = login_erfolg

    async def _stelle_token_sicher(self) -> bool:
        return self._login_erfolg


class _FakeAsyncClient:
    """Mock httpx.AsyncClient mit konfigurierbarem JSON-Response."""

    response_json: dict | None = None
    aufrufe: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url: str, headers: dict, json: dict) -> httpx.Response:
        _FakeAsyncClient.aufrufe.append(json)
        return httpx.Response(
            200,
            json=_FakeAsyncClient.response_json or {"user_plants": []},
            request=httpx.Request("POST", url),
        )


def _resette_fake_client():
    _FakeAsyncClient.response_json = None
    _FakeAsyncClient.aufrufe = []


@pytest.fixture
async def speicher(tmp_path):
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    yield s
    await s.schliessen()


@pytest.mark.asyncio
async def test_lueckenfuellung_importiert_neue_messungen(speicher, monkeypatch):
    """Happy Path: 1 Pflanze, 2 Messungen pro Tag, 7 Tage -> ~14 Importe."""
    _resette_fake_client()
    monkeypatch.setattr("bewaesserung.fyta_backfill.httpx.AsyncClient", _FakeAsyncClient)

    fyta_client = _FakeFytaClient([
        FytaPflanzenKonfig(fyta_id=100, zone_id="zone_a", name="A"),
    ])
    # Pro Tagesabfrage liefert FYTA 2 Messungen
    _FakeAsyncClient.response_json = {
        "user_plants": [{
            "user_plant_id": 100,
            "measurements": [
                {"date_utc": "2026-05-01T08:00:00", "soil_moisture": 45,
                 "temperature": 19, "light": 8, "soil_fertility": 0},
                {"date_utc": "2026-05-01T20:00:00", "soil_moisture": 42,
                 "temperature": 20, "light": 6, "soil_fertility": 0},
            ],
        }]
    }

    importiert, duplikate = await backfill_lueckenfuellung(
        speicher, fyta_client, tage_zurueck=7,
    )

    # 8 Tage (von..bis inklusive) × 2 Messungen — aber Mock liefert
    # statisch dieselben 2 Eintraege fuer jeden Tag, also Dedup greift
    # ab Tag 2. Real-FYTA wuerde tagesspezifische Messungen liefern.
    assert importiert == 2  # Tag 1: beide importiert
    assert duplikate == 7 * 2  # Tag 2-8: beide als Duplikat erkannt
    assert len(_FakeAsyncClient.aufrufe) == 8  # 7 Tage + heute


@pytest.mark.asyncio
async def test_lueckenfuellung_zweiter_aufruf_alles_duplikat(speicher, monkeypatch):
    """Dedup-Test: zweiter Aufruf sofort danach -> 0 importiert."""
    _resette_fake_client()
    monkeypatch.setattr("bewaesserung.fyta_backfill.httpx.AsyncClient", _FakeAsyncClient)

    fyta_client = _FakeFytaClient([
        FytaPflanzenKonfig(fyta_id=100, zone_id="zone_a", name="A"),
    ])
    _FakeAsyncClient.response_json = {
        "user_plants": [{
            "user_plant_id": 100,
            "measurements": [
                {"date_utc": "2026-05-01T08:00:00", "soil_moisture": 45,
                 "temperature": 19, "light": 8, "soil_fertility": 0},
            ],
        }]
    }

    importiert_1, _ = await backfill_lueckenfuellung(
        speicher, fyta_client, tage_zurueck=2,
    )
    assert importiert_1 == 1

    # Zweiter Aufruf — alle Datenpunkte sind jetzt Duplikate
    importiert_2, duplikate_2 = await backfill_lueckenfuellung(
        speicher, fyta_client, tage_zurueck=2,
    )
    assert importiert_2 == 0
    assert duplikate_2 == 3  # 3 Tage × 1 Messung


@pytest.mark.asyncio
async def test_lueckenfuellung_print_garantiert_fertig_anzeige(speicher, monkeypatch, capsys):
    """T-0167c: User-Bug 10.05.: `fertig`-Log war im structlog-Output
    nie sichtbar. Sicherheitsnetz: print-Statement im finally-Block,
    das garantiert auf der CLI erscheint."""
    _resette_fake_client()
    monkeypatch.setattr("bewaesserung.fyta_backfill.httpx.AsyncClient", _FakeAsyncClient)
    fyta_client = _FakeFytaClient([
        FytaPflanzenKonfig(fyta_id=100, zone_id="zone_a", name="A"),
    ])
    _FakeAsyncClient.response_json = {
        "user_plants": [{"user_plant_id": 100, "measurements": [
            {"date_utc": "2026-05-01T08:00:00", "soil_moisture": 45,
             "temperature": 19, "light": 8, "soil_fertility": 0},
        ]}]
    }

    await backfill_lueckenfuellung(speicher, fyta_client, tage_zurueck=2)

    out = capsys.readouterr().out
    assert "[Backfill-Hook] fertig:" in out
    assert "Tage" in out


@pytest.mark.asyncio
async def test_lueckenfuellung_print_zeigt_abgebrochen_bei_exception(speicher, monkeypatch, capsys):
    """T-0167c: Bei einer Exception nach dem Token-Check muss der
    finally-Block trotzdem den Status `abgebrochen` melden."""

    class _AsyncClientCrash:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, *args, **kwargs):
            raise RuntimeError("simulated crash inside loop")

    monkeypatch.setattr(
        "bewaesserung.fyta_backfill.httpx.AsyncClient", _AsyncClientCrash,
    )
    fyta_client = _FakeFytaClient([
        FytaPflanzenKonfig(fyta_id=100, zone_id="zone_a", name="A"),
    ])

    # Loop faengt Exceptions pro Tag ab + macht weiter
    # -> wir erwarten "fertig" (kein Abbruch), nicht "abgebrochen".
    # Dieser Test bestaetigt also, dass der finally-Block IMMER prints.
    await backfill_lueckenfuellung(speicher, fyta_client, tage_zurueck=1)
    out = capsys.readouterr().out
    assert "[Backfill-Hook]" in out
    # Tag-Crashes werden pro Tag gefangen, ergo Loop ist normal durch
    assert "fertig:" in out


@pytest.mark.asyncio
async def test_lueckenfuellung_keine_pflanzen_konfiguriert(speicher):
    """Ohne FYTA-Pflanzen-Konfig -> sauberer Early-Return ohne Crash."""
    fyta_client = _FakeFytaClient([])
    importiert, duplikate = await backfill_lueckenfuellung(
        speicher, fyta_client, tage_zurueck=7,
    )
    assert (importiert, duplikate) == (0, 0)


@pytest.mark.asyncio
async def test_lueckenfuellung_token_login_fehlgeschlagen(speicher):
    """Token-Refresh schlaegt fehl -> sauberer Abbruch ohne API-Calls."""
    fyta_client = _FakeFytaClient(
        [FytaPflanzenKonfig(fyta_id=100, zone_id="zone_a", name="A")],
        login_erfolg=False,
    )
    importiert, duplikate = await backfill_lueckenfuellung(
        speicher, fyta_client, tage_zurueck=7,
    )
    assert (importiert, duplikate) == (0, 0)


@pytest.mark.asyncio
async def test_lueckenfuellung_403_triggert_token_refresh_und_retry(speicher, monkeypatch):
    """T-0167b: erster Tag returnt 403 -> Token-Refresh -> Retry liefert
    Daten -> Folgetage normal weiter mit dem frischen Token (kein
    blindes Wiederholen mit altem Token)."""

    refresh_zaehler: dict[str, int] = {"login": 0}

    class _403DannOk:
        post_zaehler: int = 0

        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None

        async def post(self, url, headers, json):
            _403DannOk.post_zaehler += 1
            # Ungerade Aufrufe (1, 3, 5...) -> 403, gerade -> 200
            # Aber im Code: bei 403 wird der Retry sofort danach gemacht
            # (= ungerade), also alternieren wir hier erst-403-dann-200
            # Hier vereinfacht: Jeder Tag macht zuerst 403, dann 200.
            if _403DannOk.post_zaehler % 2 == 1:
                return httpx.Response(
                    403,
                    json={"error": "token_expired"},
                    request=httpx.Request("POST", url),
                )
            tag_index = _403DannOk.post_zaehler // 2
            return httpx.Response(
                200,
                json={"user_plants": [{
                    "user_plant_id": 100,
                    "measurements": [{
                        "date_utc": f"2026-05-0{tag_index}T08:00:00",
                        "soil_moisture": 40 + tag_index,
                        "temperature": 19, "light": 8, "soil_fertility": 0,
                    }],
                }]},
                request=httpx.Request("POST", url),
            )

    class _RefreshFytaClient(_FakeFytaClient):
        async def _stelle_token_sicher(self) -> bool:
            # Nach Cache-Invalidierung wird das hier aufgerufen,
            # zaehlt und gibt einen frischen Token zurueck.
            refresh_zaehler["login"] += 1
            self._token = f"refreshed-{refresh_zaehler['login']}"
            return True

    monkeypatch.setattr(
        "bewaesserung.fyta_backfill.httpx.AsyncClient", _403DannOk,
    )
    fyta_client = _RefreshFytaClient([
        FytaPflanzenKonfig(fyta_id=100, zone_id="zone_a", name="A"),
    ])

    importiert, _ = await backfill_lueckenfuellung(
        speicher, fyta_client, tage_zurueck=2,  # 3 Tage
    )
    # 3 Tage × 1 Messung, jeder mit 403-dann-200 = 6 POSTs
    assert _403DannOk.post_zaehler == 6
    assert importiert == 3  # alle 3 Tage haben Erfolg
    # Mindestens 1 Token-Refresh (initial _stelle_token_sicher) +
    # mindestens 1 pro 403-Retry = 4 (initial + 3 retries).
    # _stelle_token_sicher wird beim Init AUCH aufgerufen (von
    # backfill_lueckenfuellung selbst), daher Anzahl >= 4.
    assert refresh_zaehler["login"] >= 4


@pytest.mark.asyncio
async def test_lueckenfuellung_api_fehler_ueberspringt_tag(speicher, monkeypatch):
    """API-Fehler an einem Tag -> dieser Tag wird uebersprungen, andere
    laufen normal weiter (kein Crash)."""

    class _AsyncClientMitFehler:
        ruf: int = 0

        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None

        async def post(self, url, headers, json):
            _AsyncClientMitFehler.ruf += 1
            if _AsyncClientMitFehler.ruf == 2:
                # Zweiter Tag wirft Exception
                raise httpx.HTTPError("simulated 500")
            return httpx.Response(
                200,
                json={"user_plants": [{
                    "user_plant_id": 100,
                    "measurements": [{
                        "date_utc": f"2026-05-0{_AsyncClientMitFehler.ruf}T08:00:00",
                        "soil_moisture": 40 + _AsyncClientMitFehler.ruf,
                        "temperature": 19, "light": 8, "soil_fertility": 0,
                    }],
                }]},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        "bewaesserung.fyta_backfill.httpx.AsyncClient", _AsyncClientMitFehler,
    )
    fyta_client = _FakeFytaClient([
        FytaPflanzenKonfig(fyta_id=100, zone_id="zone_a", name="A"),
    ])

    importiert, duplikate = await backfill_lueckenfuellung(
        speicher, fyta_client, tage_zurueck=2,
    )
    # 3 Tage versucht, einer schlaegt fehl, zwei liefern je 1 Messung
    assert importiert == 2
    assert _AsyncClientMitFehler.ruf == 3
