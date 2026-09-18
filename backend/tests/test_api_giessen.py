"""T-0169: Tests fuer den /api/giessen-Endpoint mit Liter-Variante.

Deckt:
- Sekunden-Body bleibt Backward-Compat
- Liter-Body schreibt event.liter + Pseudo-Dauer
- Beide Felder gleichzeitig: liter gewinnt, Pseudo-Dauer wird NICHT
  ueberschrieben (Caller-dauer wird respektiert)
- Beide leer / 0: 400-aequivalent (`{fehler: ...}`)
- /api/zonen liefert logging_einheit, logging_optionen_ml,
  pre_soak_min, pre_soak_pause_min (T-0169 + T-0170)
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import bewaesserung.api_server as api_server
from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.modelle import (
    BilanzKonfig,
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    StandortKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import HahnLockEntscheidung


def _run(coro):
    return asyncio.run(coro)


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[
            ZonenKonfig(
                zone_id="bambuswald", name="Bambus", ventil_kanal=2,
                pre_soak_min=5, pre_soak_pause_min=30,
            ),
            ZonenKonfig(
                zone_id="zitrus", name="Zitrus", ventil_kanal=None,
                logging_einheit="ml",
                logging_optionen_ml=[100, 250, 500, 1000],
            ),
        ],
        bilanz=BilanzKonfig(manuell_liter_pro_minute=10.0),
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten", wetter_standort="o",
                zonen=["bambuswald", "zitrus"],
            ),
        ],
    )


@pytest.fixture
def client(tmp_path):
    speicher = Speicher(str(tmp_path / "giessen.db"))
    _run(speicher.verbinden())
    konfiguriere_api(speicher, _konfig(), MagicMock(), MagicMock())
    c = TestClient(app)
    try:
        yield c, speicher
    finally:
        c.close()
        _run(speicher.schliessen())


# --- Backward-Compat: Sekunden-Body ---------------------------------------


def test_giessen_sekunden_backward_compat(client):
    c, speicher = client
    r = c.post(
        "/api/giessen",
        json={"zone_id": "bambuswald", "dauer_sekunden": 60},
    )
    assert r.status_code == 200
    daten = r.json()
    assert daten.get("ok") is True
    assert daten.get("dauer_sekunden") == 60
    assert daten.get("liter") is None

    events = _run(speicher.hole_ventil_ereignisse("bambuswald"))
    assert len(events) == 1
    assert events[0].dauer_sekunden == 60
    assert events[0].liter is None


def test_notfall_stopp_aggregiert_anzahl_statt_liste(tmp_path):
    """Regression: Sicherung liefert `geschlossen: int`, API darf nicht extend(int)."""
    speicher = Speicher(str(tmp_path / "notfall.db"))
    _run(speicher.verbinden())

    class SicherungAttrappe:
        async def notfall_stopp(self):
            return {"geschlossen": 1, "fehlgeschlagen": []}

    konfiguriere_api(
        speicher,
        _konfig(),
        MagicMock(),
        MagicMock(),
        ventil_sicherungen={"dswc-1": SicherungAttrappe()},  # type: ignore[dict-item]
    )
    c = TestClient(app)
    try:
        r = c.post("/api/notfall-stopp")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "geschlossen": 1}
    finally:
        c.close()
        _run(speicher.schliessen())


def test_pre_soak_manager_routing_folgt_dswc_mapping(tmp_path):
    """Pre-Soak darf bei gleichem Kanal nicht die falsche DSWC-Sicherung nutzen."""
    speicher = Speicher(str(tmp_path / "pre_soak_routing.db"))
    _run(speicher.verbinden())

    class SicherungAttrappe:
        async def pruefe_hahn_lock(self, _kanal):
            return HahnLockEntscheidung(erlaubt=True)

    sicherung_a = SicherungAttrappe()
    sicherung_b = SicherungAttrappe()
    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[
            ZonenKonfig(
                zone_id="zone_a", name="Zone A",
                ventil_geraet_id="dswc-a", ventil_kanal=1,
            ),
            ZonenKonfig(
                zone_id="zone_b", name="Zone B",
                ventil_geraet_id="dswc-b", ventil_kanal=1,
            ),
        ],
        wetter=WetterKonfig(),
    )
    try:
        konfiguriere_api(
            speicher,
            konfig,
            MagicMock(),
            MagicMock(),
            ventil_sicherungen={
                "dswc-a": sicherung_a,  # type: ignore[dict-item]
                "dswc-b": sicherung_b,  # type: ignore[dict-item]
            },
        )

        mgr_a = api_server._pre_soak_manager_fuer_zone("zone_a")
        mgr_b = api_server._pre_soak_manager_fuer_zone("zone_b")

        assert mgr_a is not mgr_b
        assert mgr_a._sicherung is sicherung_a  # type: ignore[attr-defined]
        assert mgr_b._sicherung is sicherung_b  # type: ignore[attr-defined]
    finally:
        _run(speicher.schliessen())


# --- Liter-Variante (T-0169) ---------------------------------------------


def test_giessen_liter_schreibt_pseudo_dauer(client):
    """Bei reinem Liter-Body wird `dauer_sekunden = max(1, round(liter*60/rate))`
    berechnet, damit dauer-basierte ML-Features das Event nicht ausblenden.

    Konfig: manuell_liter_pro_minute=10.0 -> 0.25 L * 60 / 10 = 1.5 -> 2 s.
    """
    c, speicher = client
    r = c.post(
        "/api/giessen",
        json={"zone_id": "zitrus", "liter": 0.25},
    )
    assert r.status_code == 200, r.text
    daten = r.json()
    assert daten.get("ok") is True
    assert daten.get("liter") == 0.25
    assert daten.get("dauer_sekunden") == 2  # round(1.5)

    events = _run(speicher.hole_ventil_ereignisse("zitrus"))
    assert len(events) == 1
    assert events[0].liter == 0.25
    assert events[0].dauer_sekunden == 2


def test_giessen_kleines_liter_wird_zu_mind_1_sekunde(client):
    """50 ml × 60/10 = 0.3 s -> max(1, round(...)) ergibt 1 s.

    Codex-Iteration-4-Catch: ohne max(1) waere 0 s -> Event faellt aus
    Dauer-basierten ML-Features raus.
    """
    c, speicher = client
    r = c.post(
        "/api/giessen",
        json={"zone_id": "zitrus", "liter": 0.05},
    )
    assert r.status_code == 200
    daten = r.json()
    assert daten.get("dauer_sekunden") == 1
    events = _run(speicher.hole_ventil_ereignisse("zitrus"))
    assert events[0].dauer_sekunden == 1
    assert events[0].liter == 0.05


def test_giessen_beide_felder_caller_dauer_wird_respektiert(client):
    """Beide gesetzt: `liter` ist kanonisch fuer die Bilanz, aber die vom
    Caller geschickte `dauer_sekunden` bleibt erhalten (kein Overwrite).
    """
    c, speicher = client
    r = c.post(
        "/api/giessen",
        json={"zone_id": "zitrus", "dauer_sekunden": 90, "liter": 0.5},
    )
    assert r.status_code == 200
    events = _run(speicher.hole_ventil_ereignisse("zitrus"))
    assert events[0].dauer_sekunden == 90
    assert events[0].liter == 0.5


# --- Validierung -----------------------------------------------------------


def test_giessen_beides_leer_liefert_fehler(client):
    c, _ = client
    r = c.post("/api/giessen", json={"zone_id": "bambuswald"})
    assert r.status_code == 200  # API-Convention: 200 mit fehler-Feld
    assert "fehler" in r.json()


def test_giessen_beides_null_liefert_fehler(client):
    c, _ = client
    r = c.post(
        "/api/giessen",
        json={"zone_id": "bambuswald", "dauer_sekunden": 0, "liter": 0},
    )
    assert "fehler" in r.json()


def test_giessen_negative_liter_liefert_fehler(client):
    c, _ = client
    r = c.post(
        "/api/giessen",
        json={"zone_id": "bambuswald", "liter": -1.0},
    )
    assert "fehler" in r.json()


# --- T-0174 Rueckwirkend mit Zeitstempel ---------------------------------


def test_giessen_rueckwirkend_mit_zeitstempel(client):
    """T-0174: User kann eine bereits stattgefundene Bewaesserung mit
    explizitem `zeitstempel` nachtragen — Backend uebernimmt den Wert
    statt jetzt() einzusetzen.
    """
    from datetime import datetime as _dt
    c, speicher = client
    rueck_zeit = "2026-05-10T08:30:00"
    r = c.post(
        "/api/giessen",
        json={"zone_id": "zitrus", "liter": 0.5, "zeitstempel": rueck_zeit},
    )
    assert r.status_code == 200, r.text
    daten = r.json()
    assert daten.get("ok") is True
    assert daten.get("zeitstempel", "").startswith("2026-05-10T08:30")

    events = _run(speicher.hole_ventil_ereignisse("zitrus"))
    assert len(events) == 1
    assert events[0].zeitstempel == _dt(2026, 5, 10, 8, 30, 0)
    assert events[0].liter == 0.5


# --- /api/zonen Response (T-0169 + T-0170) --------------------------------


def test_zonen_endpoint_logging_einheit_und_pre_soak(client):
    c, _ = client
    r = c.get("/api/zonen")
    assert r.status_code == 200
    nach_id = {z["zone_id"]: z for z in r.json()}

    # Zitrus: logging_einheit ml + Optionen
    z = nach_id["zitrus"]
    assert z["logging_einheit"] == "ml"
    assert z["logging_optionen_ml"] == [100, 250, 500, 1000]

    # Bambus: pre_soak-Defaults durchgereicht (T-0170)
    b = nach_id["bambuswald"]
    assert b["pre_soak_min"] == 5
    assert b["pre_soak_pause_min"] == 30

    # Default-Logging bei nicht-konfigurierten Zonen
    assert b["logging_einheit"] == "sekunden"
    assert b["logging_optionen_ml"] == []
