"""Tests fuer T-0065 ml_dauer_vorschlag (Speicher-Helper).

Deckt die drei Shadow-Persistenz-Hooks fuer das Response-Modell ab:
speichere_dauer_vorschlag, hole_dauer_vorschlaege_unbewertet,
markiere_dauer_vorschlag_bewertet + den MAE-Report hole_dauer_drift_metriken.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "dauer.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _features() -> str:
    return json.dumps({"f_vor": 32.0, "et0_6h": 3.2, "vpd": 0.9})


def test_speichere_und_hole_unbewertet(speicher):
    jetzt = datetime(2026, 4, 22, 6, 0)
    row_id = _run(speicher.speichere_dauer_vorschlag(
        zeitstempel=jetzt,
        zone_id="waldblumenhain",
        f_vor=32.0, ziel_schwelle=55.0,
        heuristik_s=1800, ml_s=3600,
        ml_modell_version="v1",
        features_json=_features(),
        modus="shadow",
    ))
    assert row_id > 0

    # vor 6h: noch nicht reif
    offen = _run(speicher.hole_dauer_vorschlaege_unbewertet(
        bis_zeitstempel=jetzt + timedelta(hours=5, minutes=59),
    ))
    assert offen == []

    # nach 6h: kommt raus
    offen = _run(speicher.hole_dauer_vorschlaege_unbewertet(
        bis_zeitstempel=jetzt + timedelta(hours=6, minutes=5),
    ))
    assert len(offen) == 1
    z = offen[0]
    assert z["zone_id"] == "waldblumenhain"
    assert z["heuristik_s"] == 1800
    assert z["ml_s"] == 3600
    assert z["modus"] == "shadow"


def test_modus_check_verhindert_tippfehler(speicher):
    """Constraint (Python + DB) faengt unbekannte Modi."""
    jetzt = datetime(2026, 4, 22, 6, 0)
    with pytest.raises(ValueError):
        _run(speicher.speichere_dauer_vorschlag(
            zeitstempel=jetzt, zone_id="x", f_vor=0, ziel_schwelle=0,
            heuristik_s=0, ml_s=None, ml_modell_version=None,
            features_json="{}", modus="produktiv",
        ))


def test_markiere_bewertet_und_drift_metriken(speicher):
    jetzt = datetime(2026, 4, 22, 6, 0)
    id_a = _run(speicher.speichere_dauer_vorschlag(
        zeitstempel=jetzt, zone_id="waldblumenhain",
        f_vor=32.0, ziel_schwelle=55.0,
        heuristik_s=1800, ml_s=3600, ml_modell_version="v1",
        features_json=_features(), modus="shadow",
    ))
    id_b = _run(speicher.speichere_dauer_vorschlag(
        zeitstempel=jetzt + timedelta(hours=1), zone_id="waldblumenhain",
        f_vor=28.0, ziel_schwelle=55.0,
        heuristik_s=1800, ml_s=None, ml_modell_version=None,
        features_json=_features(), modus="shadow",
    ))

    # Szenario: Heuristik prognostizierte 30 pp, ML 50 pp, tatsaechlich 5 pp
    _run(speicher.markiere_dauer_vorschlag_bewertet(
        row_id=id_a, bewertet_am=jetzt + timedelta(hours=6),
        ist_delta_6h=5.0,
        heuristik_prognose_delta=30.0,
        ml_prognose_delta=50.0,
        heuristik_fehler=25.0,  # |30 - 5|
        ml_fehler=45.0,          # |50 - 5|
    ))
    # Nur Heuristik bewertet (kein ML-Modell verfuegbar)
    _run(speicher.markiere_dauer_vorschlag_bewertet(
        row_id=id_b, bewertet_am=jetzt + timedelta(hours=7),
        ist_delta_6h=4.0,
        heuristik_prognose_delta=30.0,
        ml_prognose_delta=None,
        heuristik_fehler=26.0,
        ml_fehler=None,
    ))

    # Nach Markierung: kein offener Vorschlag mehr
    offen = _run(speicher.hole_dauer_vorschlaege_unbewertet(
        bis_zeitstempel=jetzt + timedelta(hours=8),
    ))
    assert offen == []

    # Drift-Report: MAE gemittelt ueber beide Zeilen
    metriken = _run(speicher.hole_dauer_drift_metriken(
        zone_id=None, fenster_tage=30, jetzt=jetzt + timedelta(hours=10),
    ))
    assert "waldblumenhain" in metriken
    wb = metriken["waldblumenhain"]
    assert wb["n_bewertet"] == 2
    assert wb["n_ml_bewertet"] == 1
    # MAE_heuristik = mean(25, 26) = 25.5
    assert wb["mae_heuristik"] == 25.5
    # MAE_ml = 45 (nur eine bewertete Zeile)
    assert wb["mae_ml"] == 45.0


def test_drift_metriken_leer_ohne_bewertung(speicher):
    """Ohne bewertete Zeilen ist das Dict leer (kein KeyError-Risiko)."""
    jetzt = datetime(2026, 4, 22, 6, 0)
    _run(speicher.speichere_dauer_vorschlag(
        zeitstempel=jetzt, zone_id="waldblumenhain",
        f_vor=30.0, ziel_schwelle=55.0,
        heuristik_s=1800, ml_s=1800, ml_modell_version="v1",
        features_json=_features(), modus="shadow",
    ))
    metriken = _run(speicher.hole_dauer_drift_metriken(
        zone_id=None, fenster_tage=30, jetzt=jetzt + timedelta(hours=1),
    ))
    assert metriken == {}
