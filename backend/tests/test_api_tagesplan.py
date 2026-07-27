"""T-0227: /api/tagesplan aggregiert die kausale Empfehlung pro Zone +
Wetter pro Standort fuer heute oder morgen.

Regression-Schutz:
- Endpoint liefert alle Top-Level-Schluessel (tag, datum,
  wetter_pro_standort, eintraege).
- Pro Zone Eintrag mit zone_id, soll_bewaessern, empfehlungs_typ.
- Heute-/Morgen-Toggle waehlt korrektes Datum.
- Auth-Matrix: read.
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
    GiessEmpfehlung,
    SpeicherKonfig,
    StandortKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    WetterStunde,
    WetterVorhersage,
    ZeitFenster,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[
            ZonenKonfig(
                zone_id="bambus", name="Bambus", ventil_kanal=1,
                bevorzugte_zeiten=[ZeitFenster(von="06:00", bis="07:00")],
            ),
            ZonenKonfig(
                zone_id="zitrus", name="Zitrus", ventil_kanal=2,
                bevorzugte_zeiten=[
                    ZeitFenster(von="07:00", bis="08:00"),
                    ZeitFenster(von="19:00", bis="20:00"),
                ],
            ),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten",
                wetter_standort="o", zonen=["bambus", "zitrus"],
            ),
        ],
    )


def _motor_mock(soll: dict[str, bool]) -> MagicMock:
    """Mock-Motor: liefert pro Zone eine GiessEmpfehlung gemaess
    `soll`-Map. Empfehlungs-Typ haengt vom Schalter ab."""
    motor = MagicMock()

    async def _vorhersage(zone_id, sicherheits_tage_override=None):
        bewaessern = soll.get(zone_id, False)
        return GiessEmpfehlung(
            zone_id=zone_id,
            zeitstempel=datetime.now(),
            soll_bewaessern=bewaessern,
            grund="praeventiv" if bewaessern else "kein_bedarf",
            empfehlungs_typ="praeventiv" if bewaessern else "kein_bedarf",
            dauer_s_empfehlung=2700 if bewaessern else 0,
            aktive_strategie="korridor",
            tage_bis_welkepunkt=2.5 if bewaessern else 8.0,
        )

    motor.vorhersage_zone = _vorhersage
    return motor


def _wetter_mock(regen_summe: float = 0.0) -> MagicMock:
    jetzt = datetime.now().replace(minute=0, second=0, microsecond=0)
    stunden = [
        WetterStunde(
            zeitstempel=jetzt + timedelta(hours=i),
            temperatur=20.0,
            niederschlag_mm=regen_summe / 24.0,
            niederschlag_wahrscheinlichkeit=0.0,
            wind_kmh=0.0, wind_richtung_grad=0.0, et0_mm=0.1,
        )
        for i in range(48)
    ]
    mgr = MagicMock()

    async def _hole(_sid):
        return WetterVorhersage(
            abfrage_zeitstempel=datetime.now(),
            stunden=stunden,
        )

    mgr.hole_vorhersage = _hole
    return mgr


@pytest.fixture
def client_mit_motor(tmp_path):
    speicher = Speicher(str(tmp_path / "t.db"))
    _run(speicher.verbinden())
    konfiguriere_api(
        speicher, _konfig(),
        _motor_mock({"bambus": True, "zitrus": False}),
        MagicMock(),
        wetter_manager=_wetter_mock(),
    )
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        _run(speicher.schliessen())


def test_tagesplan_top_level_felder(client_mit_motor):
    r = client_mit_motor.get("/api/tagesplan")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {"tag", "datum", "wetter_pro_standort", "eintraege"}
    assert body["tag"] == "heute"


def test_tagesplan_morgen_param(client_mit_motor):
    r = client_mit_motor.get("/api/tagesplan?tag=morgen")
    body = r.json()
    assert body["tag"] == "morgen"
    morgen = (datetime.now() + timedelta(days=1)).date().isoformat()
    assert body["datum"] == morgen


def test_tagesplan_eintrag_pro_zone(client_mit_motor):
    body = client_mit_motor.get("/api/tagesplan").json()
    zone_ids = [e["zone_id"] for e in body["eintraege"]]
    assert "bambus" in zone_ids
    assert "zitrus" in zone_ids


def test_tagesplan_soll_bewaessern_durchgereicht(client_mit_motor):
    body = client_mit_motor.get("/api/tagesplan").json()
    nach_id = {e["zone_id"]: e for e in body["eintraege"]}
    assert nach_id["bambus"]["soll_bewaessern"] is True
    assert nach_id["bambus"]["empfehlungs_typ"] == "praeventiv"
    assert nach_id["bambus"]["dauer_min"] == 45
    assert nach_id["zitrus"]["soll_bewaessern"] is False
    assert nach_id["zitrus"]["empfehlungs_typ"] == "kein_bedarf"
    assert nach_id["zitrus"]["dauer_min"] is None


def test_tagesplan_sortierung_bewaesserung_zuerst(client_mit_motor):
    """Zonen mit geplanter Zeit kommen vor 'kein_bedarf'."""
    body = client_mit_motor.get("/api/tagesplan?tag=morgen").json()
    eintraege = body["eintraege"]
    # bambus (soll_bewaessern=True) muss vor zitrus (kein_bedarf) liegen
    bambus_idx = next(i for i, e in enumerate(eintraege) if e["zone_id"] == "bambus")
    zitrus_idx = next(i for i, e in enumerate(eintraege) if e["zone_id"] == "zitrus")
    assert bambus_idx < zitrus_idx


def test_tagesplan_wetter_pro_standort(client_mit_motor):
    body = client_mit_motor.get("/api/tagesplan").json()
    assert len(body["wetter_pro_standort"]) == 1
    w = body["wetter_pro_standort"][0]
    assert w["standort_id"] == "o"
    assert "regen_summe_mm" in w
    assert "max_temp_c" in w


def test_tagesplan_bevorzugte_zeiten_durchgereicht(client_mit_motor):
    body = client_mit_motor.get("/api/tagesplan").json()
    nach_id = {e["zone_id"]: e for e in body["eintraege"]}
    assert nach_id["bambus"]["bevorzugte_zeiten"] == ["06:00-07:00"]
    assert nach_id["zitrus"]["bevorzugte_zeiten"] == [
        "07:00-08:00", "19:00-20:00",
    ]
