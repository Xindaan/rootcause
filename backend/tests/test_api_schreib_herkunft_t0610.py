"""T-0610 E21: Herkunftspruefung fuer schreibende Requests.

Das Bundle traegt den Schluessel mit voller control-Rolle. Als zweite Linie
hinter Host-Pruefung (T-0606) und CORS weist der Dienst POST/PATCH/DELETE ab,
wenn der Browser eine fremde Herkunft meldet (`Origin` nicht in
ERLAUBTE_ORIGINS, bzw. ohne `Origin` ein `Sec-Fetch-Site` ausser
"same-origin"/"none"). Nicht-Browser-Clients (curl in scripts/regner.sh,
iPhone-App) senden keinen der Header und bleiben mit Schluessel erlaubt.

Testkoerper: temporaere DB, Testschluessel, nur Pflege-Erinnerungen und
Ventil-Ereignis-Korrekturen auf nicht vorhandene IDs. Auch ohne die Pruefung
harmlos (keine Hardware, keine Live-Daten).
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

CONTROL_KEY = "control-test-key-herkunft-cccccccc"
KOPF = {"X-Api-Key": CONTROL_KEY}


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
                standort_id="garten", name="Garten",
                wetter_standort="o", zonen=["bambuswald"],
            ),
        ],
    )


@pytest.fixture
def client(tmp_path):
    # Auth scharf (conftest setzt sonst einen No-Op-Override): die erlaubten
    # Faelle sollen die ganze Kette mit Schluessel durchlaufen.
    app.dependency_overrides.pop(auth_dependency, None)
    speicher = Speicher(str(tmp_path / "herkunft.db"))
    _run(speicher.verbinden())
    konfiguriere_api(speicher, _konfig(), MagicMock(), MagicMock())
    s = SchluesselSpeicher(pfad=tmp_path / "api_keys.json")
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


def _kopf(origin: str | None, sec_fetch_site: str | None) -> dict[str, str]:
    h = dict(KOPF)
    if origin is not None:
        h["Origin"] = origin
    if sec_fetch_site is not None:
        h["Sec-Fetch-Site"] = sec_fetch_site
    return h


def _lege_erinnerung_an(client, origin, sec_fetch_site):
    return client.post(
        "/api/pflege-erinnerungen",
        json={"typ": "herkunft-test", "faellig_am": "2026-10-20T08:00:00"},
        headers=_kopf(origin, sec_fetch_site),
    )


def _anzahl_erinnerungen(client) -> int:
    r = client.get("/api/pflege-erinnerungen", headers=KOPF)
    assert r.status_code == 200
    return len(r.json()["eintraege"])


ABGEWIESEN = [
    pytest.param("https://evil.example", None, id="fremder-origin"),
    pytest.param("https://evil.example", "cross-site", id="fremder-origin-cross-site"),
    pytest.param("null", None, id="origin-null-sandbox"),
    pytest.param("http://localhost:3000", "same-site", id="anderer-lokaler-port"),
    pytest.param(None, "cross-site", id="ohne-origin-cross-site"),
    pytest.param(None, "same-site", id="ohne-origin-same-site"),
]

ERLAUBT = [
    pytest.param("http://127.0.0.1:8090", "same-origin", id="dashboard-same-origin"),
    pytest.param("http://localhost:8090", "same-origin", id="dashboard-localhost"),
    pytest.param("http://localhost:5173", "same-site", id="vite-dev"),
    pytest.param(None, "same-origin", id="ohne-origin-same-origin"),
    pytest.param(None, "none", id="ohne-origin-nutzer-direkt"),
    pytest.param(None, None, id="nicht-browser-curl"),
]


@pytest.mark.parametrize(("origin", "sec_fetch_site"), ABGEWIESEN)
def test_fremde_herkunft_darf_nicht_schreiben(client, origin, sec_fetch_site):
    r = _lege_erinnerung_an(client, origin, sec_fetch_site)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "herkunft_nicht_erlaubt"
    # Wirkung: die Route lief nicht, nichts wurde angelegt.
    assert _anzahl_erinnerungen(client) == 0


@pytest.mark.parametrize(("origin", "sec_fetch_site"), ERLAUBT)
def test_eigene_herkunft_und_nicht_browser_duerfen_schreiben(
    client, origin, sec_fetch_site,
):
    r = _lege_erinnerung_an(client, origin, sec_fetch_site)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert _anzahl_erinnerungen(client) == 1


@pytest.mark.parametrize("methode", ["PATCH", "DELETE"])
def test_pruefung_gilt_auch_fuer_patch_und_delete(client, methode):
    pfad = "/api/ventil-ereignis/999999"
    body = {"ausloser": "manuell"} if methode == "PATCH" else None
    fremd = client.request(
        methode, pfad, json=body, headers=_kopf("https://evil.example", "cross-site"),
    )
    assert fremd.status_code == 403
    assert fremd.json()["detail"] == "herkunft_nicht_erlaubt"
    # Gegenprobe ohne Origin: die Route laeuft und meldet die fehlende ID.
    eigen = client.request(methode, pfad, json=body, headers=KOPF)
    assert eigen.status_code == 200
    assert "fehler" in eigen.json()


def test_abgewiesener_request_verbraucht_kein_ratelimit(client):
    """Fremde Seiten duerfen das control-Ratelimit (5/min) nicht leerlaufen
    lassen und damit Giessen und Ventilstart des Dashboards blockieren
    (Notfall-Stopp und Stopps sind vom Ratelimit ausgenommen)."""
    for _ in range(6):
        assert _lege_erinnerung_an(client, "https://evil.example", None).status_code == 403
    assert _lege_erinnerung_an(client, None, None).status_code == 200
