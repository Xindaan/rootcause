"""Tests fuer T-0140 API-Auth-Layer.

Deckt ab:
- Public-Routen ohne Token (Health public-minimal, /api/health/detail nicht).
- Read-Routen: 401 ohne Token, 401 mit Bad-Token, durch mit Read/Control.
- Alle 8 Control-Routen: 401 ohne Token, 403 mit Read-Token, kein Auth-
  Fehler mit Control-Token.
- Ratelimit (5/min/Key) -- 6. Aufruf liefert 429.
- Matrix-Vollstaendigkeit: jede FastAPI-Route hat einen ROUTE_ROLLEN-
  Eintrag (Schutz gegen "neuer Endpoint vergessen + Auth offen").
- CORS allow_headers enthaelt `X-Api-Key`.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bewaesserung.api_auth import (
    Ratelimiter,
    SchluesselSpeicher,
    auth_dependency,
    setze_ratelimiter_fuer_tests,
    setze_speicher_fuer_tests,
    validiere_route_matrix,
)
from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    StandortKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher


READ_KEY = "read-test-key-aaaaaaaaaaaaaa"
CONTROL_KEY = "control-test-key-bbbbbbbbbbbb"


def _run(coro):
    return asyncio.run(coro)


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[ZonenKonfig(zone_id="bambuswald", name="Bambus", ventil_kanal=2)],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten",
                name="Garten",
                wetter_standort="o",
                zonen=["bambuswald"],
            ),
        ],
    )


@pytest.fixture
def client(tmp_path):
    # conftest.py setzt per Default einen No-Op-Override fuer
    # `auth_dependency`. Hier wollen wir Auth scharf testen, also
    # Override entfernen.
    app.dependency_overrides.pop(auth_dependency, None)

    speicher = Speicher(str(tmp_path / "auth.db"))
    _run(speicher.verbinden())
    konfiguriere_api(speicher, _konfig(), MagicMock(), MagicMock())

    s = SchluesselSpeicher(pfad=tmp_path / "api_keys.json")
    s.hinzufuegen("read-test", "read", READ_KEY)
    s.hinzufuegen("control-test", "control", CONTROL_KEY)
    setze_speicher_fuer_tests(s)
    setze_ratelimiter_fuer_tests(Ratelimiter())

    c = TestClient(app)
    try:
        yield c
    finally:
        c.close()
        _run(speicher.schliessen())
        setze_speicher_fuer_tests(None)
        setze_ratelimiter_fuer_tests(Ratelimiter())


def _read_h() -> dict[str, str]:
    return {"X-Api-Key": READ_KEY}


def _control_h() -> dict[str, str]:
    return {"X-Api-Key": CONTROL_KEY}


# --- Public: /api/health ---

def test_health_oeffentlich_minimal(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    daten = r.json()
    assert daten["ok"] is True
    assert "zeitstempel" in daten
    # Public-minimal: keine internen Details leaken.
    assert "version" not in daten
    assert "konfiguriert" not in daten
    assert "zonen_anzahl" not in daten


def test_health_detail_braucht_read_token(client):
    r = client.get("/api/health/detail")
    assert r.status_code == 401

    r = client.get("/api/health/detail", headers=_read_h())
    assert r.status_code == 200
    daten = r.json()
    assert daten["ok"] is True
    assert daten["version"]
    assert daten["konfiguriert"] is True


# --- Read-Routen ---

def test_read_ohne_token_liefert_401(client):
    r = client.get("/api/zonen")
    assert r.status_code == 401


def test_read_mit_bad_token_liefert_401(client):
    r = client.get("/api/zonen", headers={"X-Api-Key": "voellig-falsch"})
    assert r.status_code == 401


def test_read_mit_read_key_durch(client):
    r = client.get("/api/zonen", headers=_read_h())
    # 200 oder 5xx (Mock-Motor) -- aber NICHT 401/403.
    assert r.status_code not in (401, 403), (
        f"GET /api/zonen mit Read-Key sollte durch Auth durch, war {r.status_code}"
    )


def test_read_mit_control_key_durch(client):
    # Aufwertung: Control-Key darf auch GETs.
    r = client.get("/api/zonen", headers=_control_h())
    assert r.status_code not in (401, 403)


# --- Control-Routen (alle 8 parametrisiert) ---

CONTROL_ROUTEN: list[tuple[str, str, dict | None]] = [
    ("POST", "/api/giessen", {"zone_id": "bambuswald"}),
    (
        "POST", "/api/ventil/manuell-start",
        {"zone_id": "bambuswald", "dauer_sekunden": 60},
    ),
    ("POST", "/api/ventil/manuell-stop", {"zone_id": "bambuswald"}),
    ("POST", "/api/ventil/pre-soak-start", {"zone_id": "bambuswald"}),
    ("POST", "/api/ventil/pre-soak-stop", {"zone_id": "bambuswald"}),
    ("PATCH", "/api/ventil-ereignis/1", {"liter": 1.0}),
    ("DELETE", "/api/ventil-ereignis/1", None),
    ("POST", "/api/notfall-stopp", None),
]


@pytest.mark.parametrize("method,pfad,body", CONTROL_ROUTEN)
def test_control_ohne_token_liefert_401(client, method, pfad, body):
    r = client.request(method, pfad, json=body)
    assert r.status_code == 401, (
        f"{method} {pfad} sollte 401 ohne Token sein, war {r.status_code}"
    )


@pytest.mark.parametrize("method,pfad,body", CONTROL_ROUTEN)
def test_control_mit_read_key_liefert_403(client, method, pfad, body):
    r = client.request(method, pfad, headers=_read_h(), json=body)
    assert r.status_code == 403, (
        f"{method} {pfad} sollte 403 mit Read-Key sein, war {r.status_code}"
    )


@pytest.mark.parametrize("method,pfad,body", CONTROL_ROUTEN)
def test_control_mit_control_key_passiert_auth(client, method, pfad, body):
    r = client.request(method, pfad, headers=_control_h(), json=body)
    # Auth ok -- Geschaeftslogik darf 200/4xx/5xx werfen, aber nicht 401/403.
    assert r.status_code not in (401, 403), (
        f"{method} {pfad} mit Control-Key sollte Auth passieren, "
        f"war {r.status_code}"
    )


# --- Ratelimit (5/min/Key) ---

def test_ratelimit_5_dann_429(client):
    body = {"zone_id": "bambuswald"}
    for i in range(5):
        r = client.post("/api/giessen", headers=_control_h(), json=body)
        assert r.status_code != 429, f"Aufruf {i+1} darf nicht ratelimited sein"
    r = client.post("/api/giessen", headers=_control_h(), json=body)
    assert r.status_code == 429


def test_stopp_pfade_sind_vom_ratelimit_ausgenommen(client):
    """T-0557: der Notfall-Stopp darf nie an 429 scheitern.

    Das Limit zaehlt VOR der Ausfuehrung und damit auch fehlgeschlagene
    Aufrufe. Fuenf erfolglose Startversuche (im Review gemessen: 5x 500)
    reichten, damit `POST /api/notfall-stopp` mit 429 antwortete -- bei
    offenem Ventil. Genau die Klick-Folge eines Notfalls.

    Alle drei Stopp-Routen mitgetestet: sie koennen nur schliessen, teilen
    aber bis zum Fix denselben Eimer wie die oeffnenden Routen.
    """
    body = {"zone_id": "bambuswald"}
    for _ in range(6):
        client.post("/api/giessen", headers=_control_h(), json=body)
    # Eimer ist jetzt sicher leer -- Gegenprobe:
    assert client.post(
        "/api/giessen", headers=_control_h(), json=body,
    ).status_code == 429

    for pfad, nutzlast in (
        ("/api/notfall-stopp", None),
        ("/api/ventil/manuell-stop", {"zone_id": "bambuswald"}),
        ("/api/ventil/pre-soak-stop", {"zone_id": "bambuswald"}),
    ):
        r = client.post(pfad, headers=_control_h(), json=nutzlast)
        assert r.status_code != 429, (
            f"{pfad} haengt am Ratelimit -- ein Stopp darf nie an einem "
            "vollen Eimer scheitern"
        )


def test_stopp_ausnahme_gilt_nicht_fuer_read_key(client):
    """Die Ausnahme lockert das Ratelimit, nicht die Rollenpruefung.

    Negativprobe zur Ausnahme oben: ohne diesen Fall koennte man
    `RATELIMIT_AUSNAHMEN` versehentlich vor die Rollenpruefung ziehen und
    haette einen read-Key auf dem Notfall-Stopp.
    """
    r = client.post("/api/notfall-stopp", headers=_read_h())
    assert r.status_code == 403


def test_ratelimit_pro_key_separat(client):
    # Read-Key macht 5 GETs (control-Limit gilt nicht fuer read).
    for _ in range(5):
        r = client.get("/api/zonen", headers=_read_h())
        assert r.status_code != 429
    # Read auf Read-Endpoints hat KEIN Ratelimit -> 6. Aufruf ok.
    r = client.get("/api/zonen", headers=_read_h())
    assert r.status_code != 429


# --- Matrix-Vollstaendigkeit (Codex-Empfehlung) ---

def test_route_matrix_deckt_alle_routes_ab():
    """Schutz gegen 'neuer Endpoint vergessen + Auth offen'.

    Wenn dieser Test failt, wurde eine neue FastAPI-Route registriert
    aber kein Eintrag in `ROUTE_ROLLEN` hinzugefuegt. Eintrag pflegen
    in `bewaesserung/api_auth.py` -- entweder als 'public', 'read'
    oder 'control', je nach Sensitivitaet.
    """
    fehlt = validiere_route_matrix(app)
    assert fehlt == [], (
        f"Routen ohne Matrix-Eintrag: {fehlt}. "
        f"Pflegen in bewaesserung/api_auth.py:ROUTE_ROLLEN."
    )


# --- CORS ---

def test_cors_erlaubt_x_api_key_header():
    cors = next(
        m for m in app.user_middleware
        if m.cls.__name__ == "CORSMiddleware"
    )
    assert "X-Api-Key" in cors.kwargs["allow_headers"], (
        "CORSMiddleware muss `X-Api-Key` in allow_headers haben, "
        "sonst blockt der Browser den Vite-Dev-Frontend-Aufruf."
    )


# --- Speicher-Datei: Permissions, atomic write ---

def test_schluessel_speicher_datei_mode_0600(tmp_path):
    s = SchluesselSpeicher(pfad=tmp_path / "k.json")
    s.hinzufuegen("test", "read", "geheim")
    s.speichere()
    mode = (tmp_path / "k.json").stat().st_mode & 0o777
    assert mode == 0o600, f"Erwartet 0600, war {oct(mode)}"


def test_schluessel_findet_korrekten_eintrag(tmp_path):
    s = SchluesselSpeicher(pfad=tmp_path / "k.json")
    s.hinzufuegen("alpha", "read", "alpha-secret")
    s.hinzufuegen("beta", "control", "beta-secret")
    assert s.finde("alpha-secret").id == "alpha"
    assert s.finde("beta-secret").id == "beta"
    assert s.finde("falsch") is None


def test_schluessel_persistenz_round_trip(tmp_path):
    pfad = tmp_path / "k.json"
    s = SchluesselSpeicher(pfad=pfad)
    s.hinzufuegen("test", "control", "secret-xyz")
    s.speichere()

    geladen = SchluesselSpeicher.laden(pfad)
    assert len(geladen.eintraege) == 1
    assert geladen.eintraege[0].id == "test"
    assert geladen.eintraege[0].rolle == "control"
    assert geladen.finde("secret-xyz") is not None
    assert geladen.finde("falsch") is None


# --- T-0217: Auth-Cache + scrypt-Executor ---------------------------

def test_auth_cache_hasht_nur_beim_ersten_request(client):
    """Cache-Hit darf scrypt NICHT erneut aufrufen. Vorher lief
    `finde()` (scrypt, ~36 ms) bei jedem Request synchron im
    Event-Loop -> CPU-Saturierung bei pollenden Dashboards."""
    from bewaesserung import api_auth

    aufrufe = {"n": 0}
    orig_finde = SchluesselSpeicher.finde

    def _zaehl_finde(self, kandidat):
        aufrufe["n"] += 1
        return orig_finde(self, kandidat)

    api_auth.SchluesselSpeicher.finde = _zaehl_finde
    try:
        for _ in range(5):
            r = client.get("/api/zonen", headers=_read_h())
            assert r.status_code not in (401, 403)
    finally:
        api_auth.SchluesselSpeicher.finde = orig_finde

    # 5 Requests, aber nur 1 echter scrypt-Lookup (Rest aus dem Cache).
    assert aufrufe["n"] == 1, (
        f"Erwartet 1 scrypt-Lookup fuer 5 Requests, war {aufrufe['n']}"
    )


def test_auth_negativ_cache_unterdrueckt_rehash(client):
    """Ein ungueltiger Key wird nicht bei jedem Request neu gehasht —
    Negativ-Cache mit TTL faengt Wiederholungen ab."""
    from bewaesserung import api_auth

    aufrufe = {"n": 0}
    orig_finde = SchluesselSpeicher.finde

    def _zaehl_finde(self, kandidat):
        aufrufe["n"] += 1
        return orig_finde(self, kandidat)

    api_auth.SchluesselSpeicher.finde = _zaehl_finde
    try:
        for _ in range(4):
            r = client.get("/api/zonen", headers={"X-Api-Key": "falsch-xyz"})
            assert r.status_code == 401
    finally:
        api_auth.SchluesselSpeicher.finde = orig_finde

    assert aufrufe["n"] == 1, (
        f"Erwartet 1 scrypt-Lookup fuer 4 Bad-Key-Requests, war {aufrufe['n']}"
    )


def test_setze_speicher_fuer_tests_leert_auth_cache(tmp_path):
    """Der Test-Hook muss den Auth-Cache mitleeren, sonst leakt ein
    gecachter Key aus einem vorherigen Test in den naechsten."""
    from bewaesserung import api_auth

    s = SchluesselSpeicher(pfad=tmp_path / "k.json")
    s.hinzufuegen("a", "read", "key-a")
    setze_speicher_fuer_tests(s)
    _run(api_auth._finde_mit_cache("key-a"))
    assert "key-a" in api_auth._auth_cache

    setze_speicher_fuer_tests(None)
    assert api_auth._auth_cache == {}
    assert api_auth._auth_negativ_cache == {}
