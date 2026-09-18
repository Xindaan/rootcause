"""T-0062a: Delta-Regressor-Tests.

Absolute Regressoren hatten einen Bias-Anker bei ~44 % (Mean der
Trainingsziel-Verteilung), der bei Saettigung 100 % die Prognose
Richtung 70 zog. Der Delta-Regressor trainiert auf `ziel_delta_Xh`
(Aenderung gegenueber aktueller Feuchte); Inferenz ist dann
`aktuell + delta_predict`. Mean des Delta-Ziels ist ~0 → kein
Pull-to-Mean-Effekt.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("pandas")

import numpy as np
import pandas as pd

from bewaesserung.ml.training import TrainingsPipeline
from bewaesserung.ml.vorhersage import MLVorhersageService


class _FakeModell:
    def __init__(self, pred: float, contribs: list[float] | None = None):
        self._pred = pred
        self._contribs = contribs

    def predict(self, X, pred_contrib: bool = False):
        if pred_contrib:
            werte = self._contribs or [self._pred] * len(X.columns)
            return np.array([werte + [0.0] for _ in range(len(X))])
        return np.array([self._pred for _ in range(len(X))])


def _synth_mit_delta(n: int = 400) -> pd.DataFrame:
    """Synthetische Daten wie test_ml_quantile + `ziel_delta_Xh`."""
    rng = np.random.default_rng(42)
    t0 = datetime(2026, 2, 1)
    zeiten = [t0 + timedelta(hours=i) for i in range(n)]
    feuchte = 50 + 10 * np.sin(np.arange(n) / 24) + rng.normal(0, 2, n)
    temperatur = 15 + 5 * np.sin(np.arange(n) / 24)
    et0 = 0.1 + rng.uniform(0, 0.2, n)
    ziel6 = np.roll(feuchte, -6)
    ziel12 = np.roll(feuchte, -12)
    ziel24 = np.roll(feuchte, -24)
    df = pd.DataFrame({
        "zone_id": ["bambuswald"] * n,
        "zeitstempel": [t.isoformat() for t in zeiten],
        "boden_feuchte_aktuell": feuchte,
        "boden_temperatur": temperatur,
        "feuchte_trend_6h": rng.normal(0, 0.5, n),
        "niederschlag_summe_6h": rng.uniform(0, 1.5, n),
        "niederschlag_summe_12h": rng.uniform(0, 2.5, n),
        "niederschlag_summe_24h": rng.uniform(0, 4.0, n),
        "et0_summe_6h": et0,
        "et0_summe_12h": et0 * 2,
        "et0_summe_24h": et0 * 4,
        "quelle": ["gardena"] * n,
        "ziel_feuchte_6h": ziel6,
        "ziel_feuchte_12h": ziel12,
        "ziel_feuchte_24h": ziel24,
        # T-0062a: Delta-Ziel
        "ziel_delta_6h": ziel6 - feuchte,
        "ziel_delta_12h": ziel12 - feuchte,
        "ziel_delta_24h": ziel24 - feuchte,
    })
    return df.iloc[:-24]


def test_features_enthalten_ziel_delta_spalten():
    """Synthetische Daten haben `ziel_delta_{6,12,24}h` — nachgestellt was
    `FeatureExtraktor.erstelle_trainingsdaten` erzeugen muss.
    """
    df = _synth_mit_delta(400)
    for h in (6, 12, 24):
        assert f"ziel_delta_{h}h" in df.columns
        # Delta ~ 0 im Mittel (Sinus + Rauschen)
        assert abs(df[f"ziel_delta_{h}h"].mean()) < 3.0
        # Delta ist nicht konstant
        assert df[f"ziel_delta_{h}h"].std() > 0.5


def test_training_delta_modus_setzt_metriken_flag(tmp_path):
    """Bei `delta_mode=True` (Default) muss `metriken.delta_ziel=True` sein."""
    df = _synth_mit_delta(400)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    ergebnisse = pipeline.trainiere(df)  # delta_mode ist Default True
    assert set(ergebnisse.keys()) == {6, 12, 24}
    for h, ergebnis in ergebnisse.items():
        assert ergebnis.metriken.delta_ziel is True, (
            f"Horizont {h}h: Metriken haben delta_ziel nicht gesetzt"
        )


def test_training_mae_auf_absoluter_skala(tmp_path):
    """Auch wenn Delta-Ziel trainiert wird, MAE muss auf absoluter Feuchte
    gemessen werden (damit Gate-Vergleich gegen alte absolute Modelle fair
    bleibt). Fuer synthetische Daten mit Variance ~10 %-Punkte sollte
    MAE < 20 sein (sanity-check, nicht strenges Ziel).
    """
    df = _synth_mit_delta(400)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    ergebnisse = pipeline.trainiere(df)
    for h, ergebnis in ergebnisse.items():
        # Absolute MAE sollte plausibel niedrig sein; wuerde MAE auf
        # Delta-Skala liegen, waere er deutlich kleiner (~2-4 %-Punkte),
        # wuerde er auf absoluter Skala schlecht sein, waere er >30.
        assert 0 < ergebnis.metriken.mae < 20, (
            f"Horizont {h}h: MAE={ergebnis.metriken.mae} sieht nicht "
            "nach absoluter Skala aus"
        )


def test_inferenz_addiert_delta_auf_aktuelle_feuchte(tmp_path):
    """Das wichtigste Verhalten: bei Delta-Modell muss die Inferenz
    `aktuell + predict` rechnen, nicht nur `predict`.

    Konkret: wenn aktuelle Feuchte = 100 und das Modell 0 als Delta
    prognostiziert, muss die absolute Prognose ~100 sein — NICHT der
    Mean-Bias-Anker der alten absoluten Modelle.
    """
    df = _synth_mit_delta(400)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    pipeline.trainiere(df)

    service = MLVorhersageService(modell_verzeichnis=str(tmp_path))
    service.lade_modelle()
    # delta_ziel-Flag muss nach Laden gesetzt sein
    for h in (6, 12, 24):
        assert service._delta_ziel.get(h) is True

    # Sample mit hoher aktueller Feuchte + 0 Regen
    df_in = pd.DataFrame([{
        "zone_id": "bambuswald",
        "boden_feuchte_aktuell": 100.0,
        "boden_temperatur": 15.0,
        "feuchte_trend_6h": 0.0,
        "niederschlag_summe_6h": 0.0,
        "niederschlag_summe_12h": 0.0,
        "niederschlag_summe_24h": 0.0,
        "et0_summe_6h": 0.1,
        "et0_summe_12h": 0.2,
        "et0_summe_24h": 0.5,
        "quelle": "gardena",
    }])
    ergebnisse = service.vorhersage(df_in, horizont=6)
    assert ergebnisse is not None
    prognose = ergebnisse[0].feuchte_prognose
    # Bei abs. Modellen (Mean-Bias ~50) waere die Prognose ~50-60.
    # Bei Delta-Modellen mit 0-Mean-Delta sollte sie um 100 herum liegen.
    # Mit synthetischen Sinus-Daten ist der Trend um aktuell stark, aber
    # klar: Prognose > 70 ist das "passed"-Kriterium.
    assert prognose > 70, (
        f"Delta-Inferenz scheint nicht zu greifen: aktuell=100, Prognose={prognose}"
    )


def test_delta_inferenz_erlaubt_negative_roh_deltas():
    """Regression: negative Delta-Predictions duerfen nicht vor Addition
    auf 0 geclippt werden."""
    service = MLVorhersageService(modell_verzeichnis="/tmp/nicht_verwendet")
    service._modelle[6] = _FakeModell(-5.0)
    service._feature_cols[6] = ["boden_feuchte_aktuell"]
    service._delta_ziel[6] = True

    df_in = pd.DataFrame([{"boden_feuchte_aktuell": 80.0}])
    ergebnisse = service.vorhersage(df_in, horizont=6)

    assert ergebnisse is not None
    assert ergebnisse[0].feuchte_prognose == 75.0


def test_absolute_inferenz_clippt_final_auf_feuchteskala():
    service = MLVorhersageService(modell_verzeichnis="/tmp/nicht_verwendet")
    service._feature_cols[6] = ["boden_feuchte_aktuell"]

    service._modelle[6] = _FakeModell(110.0)
    oben = service.vorhersage(
        pd.DataFrame([{"boden_feuchte_aktuell": 80.0}]),
        horizont=6,
    )
    assert oben is not None
    assert oben[0].feuchte_prognose == 100.0

    service._modelle[6] = _FakeModell(-5.0)
    unten = service.vorhersage(
        pd.DataFrame([{"boden_feuchte_aktuell": 80.0}]),
        horizont=6,
    )
    assert unten is not None
    assert unten[0].feuchte_prognose == 0.0


def test_delta_top_features_markieren_delta_skala():
    service = MLVorhersageService(modell_verzeichnis="/tmp/nicht_verwendet")
    service._modelle[6] = _FakeModell(-5.0, contribs=[-4.0, -1.0])
    service._feature_cols[6] = ["boden_feuchte_aktuell", "feuchte_trend_6h"]
    service._delta_ziel[6] = True

    df_in = pd.DataFrame([{
        "boden_feuchte_aktuell": 80.0,
        "feuchte_trend_6h": -0.2,
    }])
    ergebnisse = service.vorhersage(df_in, horizont=6, details=True)

    assert ergebnisse is not None
    top = ergebnisse[0].top_features
    assert top is not None
    assert {f.skala for f in top} == {"delta"}


def test_abwaertskompatibel_mit_altem_modell_ohne_delta_ziel_flag(tmp_path):
    """Alte Modelle (vor T-0062a) haben kein delta_ziel-Flag in den
    Metriken. Beim Laden muss das als False interpretiert werden
    (absolute Inferenz)."""
    # Simuliere ein altes Modell: trainiere auf ziel_feuchte_* absolute
    df = _synth_mit_delta(400)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    # Kein Delta-Mode: `delta_mode=False` pro horizont
    horizonte = [6, 12, 24]
    ergebnisse = {}
    for h in horizonte:
        e = pipeline._trainiere_horizont(df, h, delta_mode=False)
        if e is not None:
            ergebnisse[h] = e
            pipeline._speichere_modell(e, h)
    pipeline._aktualisiere_symlinks(ergebnisse)

    for h, e in ergebnisse.items():
        assert e.metriken.delta_ziel is False

    service = MLVorhersageService(modell_verzeichnis=str(tmp_path))
    assert service.lade_modelle()
    for h in (6, 12, 24):
        assert service._delta_ziel.get(h) is False, (
            f"Horizont {h}h: delta_ziel sollte False sein (absolutes Training)"
        )


def test_ist_verfuegbar_mit_nur_cluster_caches():
    """T-0101: Sobald die Legacy-Wurzelmodelle weg sind, muss
    `ist_verfuegbar` weiterhin True liefern, solange Cluster-Caches da sind.
    """
    from bewaesserung.ml.vorhersage import _ClusterCache

    service = MLVorhersageService(modell_verzeichnis="/tmp/nicht_verwendet")
    assert service.ist_verfuegbar is False  # leer == False
    cache = _ClusterCache()
    cache.modelle[6] = _FakeModell(0.0)
    cache.feature_cols[6] = ["boden_feuchte_aktuell"]
    service._cluster_caches["bambuswald"] = cache

    assert service.ist_verfuegbar is True


def test_status_listet_cluster_horizonte():
    """T-0101: `status()` muss Cluster-Horizonte ausweisen, sonst sind
    Pro-Cluster-Modelle ueber /api/ml/status unsichtbar.
    """
    from bewaesserung.ml.vorhersage import _ClusterCache

    service = MLVorhersageService(modell_verzeichnis="/tmp/nicht_verwendet")
    cache = _ClusterCache()
    cache.modelle[6] = _FakeModell(0.0)
    cache.modelle[24] = _FakeModell(0.0)
    cache.feature_cols[6] = ["boden_feuchte_aktuell"]
    cache.feature_cols[24] = ["boden_feuchte_aktuell"]
    service._cluster_caches["bambuswald"] = cache

    status = service.status()
    assert status.ist_geladen is True
    assert status.cluster_horizonte == {"bambuswald": [6, 24]}
