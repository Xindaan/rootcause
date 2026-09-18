"""T-0610 ML-1: zwei Schutzpfade der ML-Pipeline warfen NameError.

Gesamtreview 25.09. (ML-1): Beide Pfade sollen einen unbrauchbaren
Zustand LEISE behandeln (Diagnosezeile + leeres Ergebnis bzw. Horizont
ueberspringen). Stattdessen referenzierten sie eine Variable, die es im
Scope nicht gibt (`h` statt `horizont`, `messungen_pro_zone` statt
`messungen_pro_sensor` seit T-0567). Folge: (a) die Prognose fuer ALLE
Zeilen des Aufrufs flog mit NameError raus statt nur den einen Horizont
zu ueberspringen; (b) der Retrain meldete einen Absturz statt "keine
Feature-Zeilen".

Die Tests fuehren genau diese Pfade aus. Kein Netz, keine DB: der
Extraktor bekommt `speicher=None`, der Service ein Stubmodell.
"""
from __future__ import annotations

import logging
from datetime import datetime

import pytest

pytest.importorskip("pandas")

import numpy as np
import pandas as pd

from bewaesserung.ml.features import FeatureExtraktor
from bewaesserung.ml.vorhersage import MLVorhersageService
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
    ZonenModus,
)


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="test"),
        zonen=[
            ZonenKonfig(
                zone_id="garten_test",
                name="Testgarten",
                modus=ZonenModus.AUTOMATIK,
                feuchte_schwelle_min=30,
                feuchte_schwelle_max=60,
                ist_topf=False,
                ist_indoor=False,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[
                WetterStandortKonfig(id="test_ort", breite=52.5, laenge=13.4),
            ]
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
    )


def test_keine_feature_zeilen_liefert_leeren_df_mit_diagnose(caplog):
    """Ohne verwertbare Messung: leerer DataFrame plus Diagnosezeile."""
    extraktor = FeatureExtraktor(speicher=None, konfig=_konfig())  # type: ignore[arg-type]
    caplog.set_level(logging.WARNING, logger="bewaesserung.ml.features")

    df = extraktor._baue_dataframe_sync(
        messungen=[],
        ventil_ereignisse=[],
        wetter_index={},
        archiv_pro_standort={},
        von=datetime(2026, 10, 1),
        bis=datetime(2026, 10, 3),
    )

    assert isinstance(df, pd.DataFrame) and df.empty
    zeilen = [r.getMessage() for r in caplog.records]
    assert any("ml.features.keine_feature_zeilen" in z for z in zeilen), zeilen


def test_nan_prognose_ueberspringt_horizont_statt_absturz(tmp_path, caplog):
    """NaN nach Post-Processing: Horizont ueberspringen, nicht 0 %, kein Absturz.

    Zwei Zeilen, nur die erste liefert NaN. Die zweite muss weiterhin eine
    Prognose bekommen -- der Schutz wirkt pro Zeile, nicht pro Aufruf.
    """
    service = MLVorhersageService(modell_verzeichnis=str(tmp_path))
    service._modelle = {6: object()}
    service._feature_cols = {6: []}
    service._prediction_mit_features = (  # type: ignore[method-assign]
        lambda modell, features, cols: np.array([np.nan, 42.0])
    )
    caplog.set_level(logging.WARNING, logger="bewaesserung.ml.vorhersage")

    features = pd.DataFrame([
        {"zone_id": "garten_test", "boden_feuchte_aktuell": 40.0},
        {"zone_id": "garten_test", "boden_feuchte_aktuell": 41.0},
    ])
    ergebnisse = service.vorhersage(features, horizont=6)

    assert ergebnisse is not None
    assert [e.feuchte_prognose for e in ergebnisse] == [42.0]
    zeilen = [r.getMessage() for r in caplog.records]
    assert any(
        "ml.vorhersage.q50_unbrauchbar horizont=6" in z for z in zeilen
    ), zeilen
