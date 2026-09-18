"""T-0518: LightGBM laeuft mit EINEM Thread -- gemessen, nicht geraten.

Benchmark 13.08.2026 ueber alle 13 Cluster des echten 60-Tage-Fensters
(58.459 Zeilen, `docs/analyse/t0518_num_threads/`):

    num_threads=1    128,57 s
    num_threads=10   401,93 s   (+213 %)

Jeder einzelne Cluster war mit einem Thread schneller, auch der groesste
(waldblumenhain, 12.703 Zeilen: 20,3 s gegen 38,1 s).

Diese Tests sichern nicht die Geschwindigkeit -- die haengt an der Maschine --
sondern dass der Parameter ueberhaupt noch bis zu LightGBM durchkommt. Genau
das ist die Stelle, an der er beim naechsten Params-Refactor still verloren
gehen wuerde: er steht in einem Dict, das an mehreren Stellen gemerged wird,
und sein Fehlen faellt nur als "der Retrain dauert wieder dreimal so lang"
auf -- Monate spaeter.
"""

from __future__ import annotations

import pandas as pd

from bewaesserung.ml.response_training import (
    STANDARD_PARAMS as RESPONSE_PARAMS,
)
from bewaesserung.ml.training import STANDARD_PARAMS, TrainingsPipeline


def test_feuchte_default_ist_ein_thread():
    assert STANDARD_PARAMS["num_threads"] == 1


def test_response_default_ist_ein_thread():
    """Response-Datensaetze sind noch kleiner als der kleinste Feuchte-
    Cluster -- der Overhead kann dort nur groesser sein."""
    assert RESPONSE_PARAMS["num_threads"] == 1


def test_pipeline_reicht_den_parameter_durch(tmp_path):
    """Der Weg vom Dict bis in die effektiven Params der Pipeline."""
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path))
    assert pipeline._params["num_threads"] == 1


def test_override_gewinnt(tmp_path):
    """Der Benchmark braucht genau diesen Weg -- ohne ihn liesse sich die
    Entscheidung nicht nachmessen, und eine Zahl ohne Nachmess-Weg ist in
    diesem Projekt keine."""
    pipeline = TrainingsPipeline(
        modell_verzeichnis=str(tmp_path), params={"num_threads": 4},
    )
    assert pipeline._params["num_threads"] == 4


def test_parameter_erreicht_lightgbm(tmp_path, monkeypatch):
    """Nicht nur im Dict, sondern im Aufruf: was landet in `lgb.train`?

    Ein Merge, der `num_threads` unterwegs verwirft (z. B. weil jemand
    `effektive_params` neu aufbaut statt zu erweitern), wuerde die drei Tests
    oben gruen lassen und trotzdem zehn Threads starten.
    """
    import bewaesserung.ml.training as tm

    gesehen: list[dict] = []
    echtes_lgb = tm.lgb

    class _LgbAttrappe:
        Dataset = echtes_lgb.Dataset
        early_stopping = echtes_lgb.early_stopping
        log_evaluation = getattr(echtes_lgb, "log_evaluation", None)

        @staticmethod
        def train(params, *args, **kwargs):
            gesehen.append(dict(params))
            return echtes_lgb.train(params, *args, **kwargs)

    monkeypatch.setattr(tm, "lgb", _LgbAttrappe)

    # Minimaler, aber echter Trainingslauf: genug Zeilen fuer einen Fold.
    # Spaltennamen aus dem echten Feature-Set (`ml/features.py`) -- mit
    # erfundenen Namen laeuft `trainiere` durch, ohne je `lgb.train` zu
    # erreichen, und der Test waere gruen ohne etwas zu pruefen.
    n = 400
    df = pd.DataFrame({
        "zeitstempel": pd.date_range("2026-06-01", periods=n, freq="h"),
        "zone_id": ["a"] * n,
        "boden_feuchte_aktuell": [30.0 + (i % 17) for i in range(n)],
        "boden_temperatur": [18.0 + (i % 7) for i in range(n)],
        "feuchte_diff_1h": [(i % 5) - 2.0 for i in range(n)],
        "ziel_delta_6h": [(i % 13) - 6.0 for i in range(n)],
        "ziel_delta_12h": [(i % 11) - 5.0 for i in range(n)],
        "ziel_delta_24h": [(i % 9) - 4.0 for i in range(n)],
    })
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    try:
        pipeline.trainiere(df)
    except Exception:
        # Der Trainingsvertrag (Spaltennamen, Mindestmengen) ist nicht das
        # Testziel -- gepruefte Aussage ist, was in `lgb.train` ankommt.
        pass

    assert gesehen, "lgb.train wurde nicht erreicht -- Test taugt nicht"
    assert all(p.get("num_threads") == 1 for p in gesehen), (
        f"num_threads kam nicht bei LightGBM an: "
        f"{[p.get('num_threads') for p in gesehen]}"
    )
