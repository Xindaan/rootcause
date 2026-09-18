"""T-0228 Stufe 2: Wartungs-Fenster pro Zone.

Wartungs-Modus: UI-Toggle pro Karte (Sensor-Reset, Wiedereinsetzen,
Fremdnutzung). Aktive Fenster pausieren die Heuristik (sensor_backfill).
Weitere Konsumenten (Leck-Detektor, ML-Training, Schwellen-Vorschlag)
folgen in Stufe 2c.

Regression-Schutz:
- starten/beenden/listen + Idempotenz (Doppel-Start, Doppel-Stop).
- API-CRUD: GET, POST start, POST end.
- Auth-Matrix: GET=read, POST=control.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

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


def _run(coro):
    return asyncio.run(coro)


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[ZonenKonfig(zone_id="zitrus", name="Zitrus", ventil_kanal=1)],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="haus", name="Haus", wetter_standort="o",
                zonen=["zitrus"],
            ),
        ],
    )


# --- Speicher-Tests ---------------------------------------------------------

def test_starte_und_hole_wartungs_fenster(tmp_path):
    s = Speicher(str(tmp_path / "w.db"))
    _run(s.verbinden())
    try:
        fid = _run(s.starte_wartungs_fenster("zitrus", grund="Sensor-Reset"))
        assert fid > 0
        offene = _run(s.hole_wartungs_fenster())
        assert len(offene) == 1
        assert offene[0]["zone_id"] == "zitrus"
        assert offene[0]["bis_am"] is None
        assert offene[0]["grund"] == "Sensor-Reset"
    finally:
        _run(s.schliessen())


def test_doppel_start_ist_idempotent(tmp_path):
    """Zweiter Start auf einer Zone mit offenem Fenster legt KEINEN
    neuen Eintrag an -- gibt die existierende id zurueck."""
    s = Speicher(str(tmp_path / "w.db"))
    _run(s.verbinden())
    try:
        f1 = _run(s.starte_wartungs_fenster("zitrus"))
        f2 = _run(s.starte_wartungs_fenster("zitrus", grund="anderer grund"))
        assert f1 == f2
        offene = _run(s.hole_wartungs_fenster())
        assert len(offene) == 1
    finally:
        _run(s.schliessen())


def test_beende_setzt_bis_am(tmp_path):
    s = Speicher(str(tmp_path / "w.db"))
    _run(s.verbinden())
    try:
        fid = _run(s.starte_wartungs_fenster("zitrus"))
        ergebnis = _run(s.beende_wartungs_fenster(fid))
        assert ergebnis is True
        offene = _run(s.hole_wartungs_fenster())
        assert offene == []
        alle = _run(s.hole_wartungs_fenster(nur_offen=False))
        assert len(alle) == 1
        assert alle[0]["bis_am"] is not None
    finally:
        _run(s.schliessen())


def test_beende_doppelt_ist_idempotent(tmp_path):
    """Zweiter Stop-Call auf schon geschlossenes Fenster meldet False
    (rowcount=0)."""
    s = Speicher(str(tmp_path / "w.db"))
    _run(s.verbinden())
    try:
        fid = _run(s.starte_wartungs_fenster("zitrus"))
        r1 = _run(s.beende_wartungs_fenster(fid))
        r2 = _run(s.beende_wartungs_fenster(fid))
        assert r1 is True
        assert r2 is False
    finally:
        _run(s.schliessen())


def test_zone_filter_isoliert(tmp_path):
    s = Speicher(str(tmp_path / "w.db"))
    _run(s.verbinden())
    try:
        _run(s.starte_wartungs_fenster("zitrus"))
        _run(s.starte_wartungs_fenster("kasten_4"))
        nur_zitrus = _run(s.hole_wartungs_fenster(zone_id="zitrus"))
        assert len(nur_zitrus) == 1
        assert nur_zitrus[0]["zone_id"] == "zitrus"
    finally:
        _run(s.schliessen())


# --- API-Tests --------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    s = Speicher(str(tmp_path / "api.db"))
    _run(s.verbinden())
    konfiguriere_api(s, _konfig(), MagicMock(), MagicMock())
    c = TestClient(app)
    try:
        yield c
    finally:
        c.close()
        _run(s.schliessen())


def test_api_roundtrip(client):
    # Start
    r = client.post("/api/wartungs-fenster", json={
        "zone_id": "zitrus", "grund": "Sensor-Reset",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    fid = r.json()["id"]
    # Liste
    r = client.get("/api/wartungs-fenster")
    eintraege = r.json()["eintraege"]
    assert len(eintraege) == 1
    assert eintraege[0]["zone_id"] == "zitrus"
    # Beenden
    r = client.post(f"/api/wartungs-fenster/{fid}/beenden")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # Liste leer
    assert client.get("/api/wartungs-fenster").json()["eintraege"] == []


def test_api_start_braucht_zone_id(client):
    r = client.post("/api/wartungs-fenster", json={})
    assert r.json()["ok"] is False
    assert "zone_id" in r.json()["fehler"]


# --- sensor_backfill-Integration --------------------------------------------

def test_sensor_backfill_pausiert_bei_aktivem_wartungs_fenster(tmp_path):
    """T-0228 Stufe 2: _ist_in_ausschluss erkennt DB-Wartungs-Fenster
    nach `_lade_wartungs_fenster`. Heuristik laeuft dann nicht."""
    from bewaesserung.sensor_backfill import SensorBackfillJob

    s = Speicher(str(tmp_path / "b.db"))
    _run(s.verbinden())
    try:
        _run(s.starte_wartungs_fenster("zitrus", grund="Test"))
        job = SensorBackfillJob(speicher=s, zone_ids=["zitrus"])
        jetzt = datetime.now()
        # Vor Cache-Lade: noch keine Wartung erkannt.
        assert job._ist_in_ausschluss("zitrus", jetzt) is False
        # Cache laden -> jetzt erkannt.
        _run(job._lade_wartungs_fenster(jetzt))
        assert job._ist_in_ausschluss("zitrus", jetzt) is True
        # Andere Zone bleibt unbeeinflusst.
        assert job._ist_in_ausschluss("kasten_4", jetzt) is False
    finally:
        _run(s.schliessen())


def test_sensor_backfill_cap_24h_bei_offenem_fenster(tmp_path):
    """T-0228 Stufe 2: ein offenes Wartungs-Fenster wird mit Cap
    'jetzt + 1 Tag' eingehaengt. Pruefung an einem Zeitpunkt in 2 Tagen
    -> NICHT mehr im Fenster, schuetzt vor vergessenem Beenden."""
    from bewaesserung.sensor_backfill import SensorBackfillJob

    s = Speicher(str(tmp_path / "b.db"))
    _run(s.verbinden())
    try:
        _run(s.starte_wartungs_fenster("zitrus"))
        job = SensorBackfillJob(speicher=s, zone_ids=["zitrus"])
        jetzt = datetime.now()
        _run(job._lade_wartungs_fenster(jetzt))
        # Jetzt drin
        assert job._ist_in_ausschluss("zitrus", jetzt) is True
        # In 2 Tagen draussen (Cap war jetzt+1 Tag)
        assert job._ist_in_ausschluss(
            "zitrus", jetzt + timedelta(days=2),
        ) is False
    finally:
        _run(s.schliessen())
