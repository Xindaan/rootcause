"""T-0190: API-Endpoint /api/zonen/{zone_id}/messwerte.

Regression-Schutz: FYTA-spezifische Felder (`licht`, `boden_fruchtbarkeit`)
muessen im Response auftauchen, damit der DetailsDrawer und der
Karten-Chart die FYTA-Werte als zusaetzliche Linien zeichnen koennen.
Vor T-0190 hat der Endpoint die Felder weggefiltert -- Drawer-Charts
fuer FYTA-Zonen waren leer trotz vorhandener DB-Werte.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.modelle import (
    DatenQuelle,
    GardenaKonfig,
    GesamtKonfig,
    SensorMessung,
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
        zonen=[
            ZonenKonfig(zone_id="fyta_topf", name="FYTA Topf", ventil_kanal=1),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten",
                name="Garten",
                wetter_standort="o",
                zonen=["fyta_topf"],
            ),
        ],
    )


@pytest.fixture
def client_mit_fyta_messung(tmp_path):
    speicher = Speicher(str(tmp_path / "messwerte.db"))
    _run(speicher.verbinden())

    _run(speicher.speichere_messung(SensorMessung(
        zeitstempel=datetime(2026, 5, 16, 12, 0),
        zone_id="fyta_topf",
        geraet_id="fyta_42",
        boden_feuchte=45.0,
        boden_temperatur=21.5,
        licht=18.7,
        boden_fruchtbarkeit=120.0,
        quelle=DatenQuelle.FYTA,
    )))

    konfiguriere_api(speicher, _konfig(), MagicMock(), MagicMock())
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        _run(speicher.schliessen())


def test_messwerte_enthaelt_fyta_extra_felder(client_mit_fyta_messung):
    """licht und boden_fruchtbarkeit muessen im /messwerte-Response stehen."""
    antwort = client_mit_fyta_messung.get(
        "/api/zonen/fyta_topf/messwerte?stunden=10000"
    )
    assert antwort.status_code == 200
    daten = antwort.json()
    assert len(daten) == 1, f"Erwartet 1 Eintrag, war {len(daten)}"

    eintrag = daten[0]

    erwartete_felder = {
        "zeitstempel",
        "boden_feuchte",
        "boden_temperatur",
        "umgebungs_temperatur",
        "licht_intensitaet",
        "licht",
        "boden_fruchtbarkeit",
        "batterie_prozent",
        # T-0211c: Pro-Sensor-Zuordnung fuer Multi-Linien-Chart.
        "geraet_id",
        "quelle",
    }
    fehlende = erwartete_felder - eintrag.keys()
    assert not fehlende, f"Fehlende Felder im Response: {sorted(fehlende)}"

    assert eintrag["licht"] == 18.7
    assert eintrag["boden_fruchtbarkeit"] == 120.0
    assert eintrag["boden_feuchte"] == 45.0
    # T-0211c: geraet_id + quelle muessen pro Messung mitgeliefert
    # werden, damit das Frontend pro-Sensor-Linien rendern kann.
    assert eintrag["geraet_id"] == "fyta_42"
    assert eintrag["quelle"] == "fyta"


def test_messwerte_gardena_zone_hat_fyta_felder_null(tmp_path):
    """Bei Gardena-Messungen sind licht und boden_fruchtbarkeit None,
    der Response-Vertrag liefert die Felder trotzdem als null."""
    speicher = Speicher(str(tmp_path / "messwerte_gardena.db"))
    _run(speicher.verbinden())
    try:
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=datetime(2026, 5, 16, 12, 0),
            zone_id="fyta_topf",
            geraet_id="gardena_1",
            boden_feuchte=55.0,
            boden_temperatur=19.0,
            licht_intensitaet=14000.0,
            batterie_prozent=85.0,
            quelle=DatenQuelle.GARDENA,
        )))

        konfiguriere_api(speicher, _konfig(), MagicMock(), MagicMock())
        client = TestClient(app)
        try:
            antwort = client.get("/api/zonen/fyta_topf/messwerte?stunden=10000")
            assert antwort.status_code == 200
            daten = antwort.json()
            assert len(daten) == 1
            eintrag = daten[0]
            assert eintrag["licht"] is None
            assert eintrag["boden_fruchtbarkeit"] is None
            assert eintrag["licht_intensitaet"] == 14000.0
        finally:
            client.close()
    finally:
        _run(speicher.schliessen())
