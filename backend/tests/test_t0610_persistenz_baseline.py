"""T-0610 ML-6: Persistenz ("Feuchte bleibt gleich") als zweite Baseline.

Entscheid Andre 13.10.2026: die Persistenz wird beim Training mitgeschrieben;
schlaegt ein Modell sie auf einem Horizont nicht, gibt es NUR eine Warnung
(Training-Log, ML-Status fuer das Badge, Prognose-Guete fuer die Karte).
Kein automatischer Rueckfall, keine neue Modell-Pflicht.

Die Tests fuehren die echte Kette aus: Training auf synthetischen Daten,
Status eines Services mit Cluster-Metriken, Guete-Urteil bis ins API-Dict.
Kein Netz, keine DB; Modelle landen in `tmp_path`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("pandas")

import numpy as np
import pandas as pd

from bewaesserung.ml.modelle_ml import MLVorhersage, TrainingsMetriken
from bewaesserung.ml.training import TrainingsPipeline
from bewaesserung.ml.vorhersage import MLVorhersageService, _ClusterCache


def _daten(n: int, delta: float, seed: int = 7) -> pd.DataFrame:
    """Zielwert = aktuelle Feuchte + `delta` auf allen Horizonten.

    `delta=0` macht die Persistenz perfekt (MAE 0); `delta=5` macht sie
    systematisch 5 Punkte daneben, waehrend ein Modell das konstante Delta
    lernen kann. Der Trend ist bewusst nicht null, damit sich die
    regelbasierte Baseline von der Persistenz unterscheidet.
    """
    rng = np.random.default_rng(seed)
    t0 = datetime(2026, 6, 1)
    feuchte = 40 + 8 * np.sin(np.arange(n) / 30) + rng.normal(0, 0.5, n)
    df = pd.DataFrame({
        "zone_id": ["garten_test"] * n,
        "zeitstempel": [(t0 + timedelta(hours=i)).isoformat() for i in range(n)],
        "boden_feuchte_aktuell": feuchte,
        "boden_temperatur": 15 + rng.normal(0, 1, n),
        "feuchte_trend_6h": rng.normal(0, 0.8, n),
        "quelle": ["gardena"] * n,
    })
    for h in (6, 12, 24):
        df[f"ziel_feuchte_{h}h"] = feuchte + delta
        df[f"ziel_delta_{h}h"] = np.full(n, delta)
    return df


def _metrik(mae: float, persistenz_mae: float | None, h: int = 6) -> TrainingsMetriken:
    return TrainingsMetriken(
        zeitstempel=datetime(2026, 10, 13),
        horizont_stunden=h,
        anzahl_samples=100,
        anzahl_features=5,
        mae=mae, rmse=mae, r2=0.0,
        baseline_mae=9.0, baseline_rmse=9.0, baseline_r2=0.0,
        persistenz_mae=persistenz_mae,
    )


def _warnzeilen(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if "ml.training.persistenz_nicht_geschlagen" in r.getMessage()
    ]


# ------------------------------------------------------------- Training

def test_training_schreibt_persistenz_und_warnt_bei_niederlage(tmp_path, caplog):
    """Persistenz perfekt: persistenz_mae = 0, das Modell kann sie nicht
    schlagen (Gleichstand zaehlt als nicht geschlagen) -> Warnung im Log.
    Die regelbasierte Baseline liegt daneben, sie ist also NICHT die
    Groesse, die hier mitgeschrieben wird."""
    caplog.set_level(logging.WARNING, logger="bewaesserung.ml.training")
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)

    m = pipeline.trainiere(_daten(400, delta=0.0), horizonte=[6])[6].metriken

    assert m.persistenz_mae == 0.0
    assert m.baseline_mae > 0.1
    assert m.persistenz_mae_pro_zone == {"garten_test": 0.0}
    assert m.schlaegt_persistenz() is False
    assert any("horizont=6h" in z for z in _warnzeilen(caplog)), _warnzeilen(caplog)


def test_training_ohne_warnung_wenn_modell_persistenz_schlaegt(tmp_path, caplog):
    """Konstantes Delta von 5: Persistenz liegt 5 daneben, das Modell lernt
    das Delta -> geschlagen, keine Warnung."""
    caplog.set_level(logging.WARNING, logger="bewaesserung.ml.training")
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)

    m = pipeline.trainiere(_daten(400, delta=5.0), horizonte=[6])[6].metriken

    assert m.persistenz_mae == pytest.approx(5.0, abs=1e-6)
    assert m.mae < 1.0
    assert m.schlaegt_persistenz() is True
    assert _warnzeilen(caplog) == []


def test_schlaegt_persistenz_grenzen():
    assert _metrik(2.0, None).schlaegt_persistenz() is None   # Altmodell
    assert _metrik(2.0, 2.0).schlaegt_persistenz() is False   # Gleichstand
    assert _metrik(2.0, 2.5).schlaegt_persistenz() is True
    assert _metrik(2.0, 1.5).schlaegt_persistenz() is False


# --------------------------------------------------- Status fuer das Badge

def _service(tmp_path, cluster: dict[str, dict[int, TrainingsMetriken]],
             legacy: dict[int, TrainingsMetriken]) -> MLVorhersageService:
    svc = MLVorhersageService(modell_verzeichnis=str(tmp_path))
    svc._cluster_caches = {
        cid: _ClusterCache(metriken=metriken) for cid, metriken in cluster.items()
    }
    svc._metriken = legacy
    return svc


def test_status_listet_cluster_horizonte_ohne_persistenz_sieg(tmp_path):
    svc = _service(
        tmp_path,
        cluster={
            "a": {6: _metrik(3.0, 2.8, 6), 24: _metrik(3.0, 4.0, 24)},
            "b": {6: _metrik(3.0, None, 6)},          # Altmodell: keine Aussage
        },
        legacy={6: _metrik(5.0, 1.0, 6)},             # ungenutzt, da Cluster da
    )
    w = svc.status().persistenz_warnungen
    assert [(x.quelle, x.horizont_stunden, x.mae, x.persistenz_mae) for x in w] == [
        ("cluster:a", 6, 3.0, 2.8),
    ]


def test_status_legacy_zaehlt_ohne_cluster(tmp_path):
    svc = _service(tmp_path, cluster={}, legacy={12: _metrik(5.0, 1.0, 12)})
    svc._modelle = {12: object()}   # Legacy-Booster geladen (sonst ist_geladen=False)
    w = svc.status().persistenz_warnungen
    assert [(x.quelle, x.horizont_stunden) for x in w] == [("legacy", 12)]


# ------------------------------------- Guete-Urteil bis ins API-Dict (Karte)

def test_guete_traegt_persistenz_warnung_ohne_gueltig_zu_aendern(tmp_path):
    from bewaesserung.api_server import _ml_eintrag_dict

    svc = _service(
        tmp_path,
        cluster={"a": {6: _metrik(3.0, 4.0, 6), 24: _metrik(3.0, 2.0, 24)}},
        legacy={},
    )
    svc._zone_zu_cluster = {"garten_test": "a"}
    jetzt = datetime(2026, 10, 13, 12, 0)
    frisch = jetzt - timedelta(minutes=15)

    def urteil(h: int) -> dict:
        mlv = MLVorhersage(
            zone_id="garten_test", zeitstempel=jetzt, horizont_stunden=h,
            feuchte_aktuell=40.0, feuchte_prognose=38.0,
            feature_zeitstempel=frisch, inferenz_geraet_id="g1",
        )
        return _ml_eintrag_dict(
            svc._mit_guete(mlv, "garten_test", jetzt, {}, {"garten_test": frisch}),
        )

    d24, d6 = urteil(24), urteil(6)
    assert d24["persistenz_warnung"] is True
    assert d6["persistenz_warnung"] is False
    # Nur Warnung: die Zahl bleibt gueltig und wird weiter genutzt.
    assert d24["gueltig"] is True and d24["feuchte_prognose"] == 38.0
