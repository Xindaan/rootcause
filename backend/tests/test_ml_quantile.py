"""Tests fuer T-0046 Quantile Regression (Training, Inferenz, Pinball, API)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import numpy as np
import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("pandas")

import pandas as pd

from bewaesserung.ml.evaluation import abdeckung_intervall, pinball_loss
from bewaesserung.ml.training import TrainingsPipeline
from bewaesserung.ml.vorhersage import MLVorhersageService


class _FakeModell:
    def __init__(self, pred: float):
        self._pred = pred

    def predict(self, X, pred_contrib: bool = False):
        if pred_contrib:
            return np.array([[self._pred, 0.0] for _ in range(len(X))])
        return np.array([self._pred for _ in range(len(X))])


def _run(coro):
    return asyncio.run(coro)


# --- pinball_loss + abdeckung_intervall ---


def test_pinball_loss_median_entspricht_halbem_mae():
    # Bei alpha=0.5 ist pinball = 0.5 * mean(|y-pred|)
    y = np.array([10.0, 20.0, 30.0])
    p = np.array([12.0, 18.0, 25.0])  # Fehler 2, -2, -5 -> |...|=2,2,5 -> mean=3
    assert abs(pinball_loss(y, p, 0.5) - 1.5) < 1e-9


def test_pinball_loss_q90_bestraft_unterschaetzung_stark():
    # Alpha=0.9: Unterschaetzung (y > p) wird mit 0.9 bestraft
    y = np.array([10.0])
    over_pred = np.array([15.0])    # ueberschaetzt: loss = 0.1 * 5 = 0.5
    under_pred = np.array([5.0])    # unterschaetzt: loss = 0.9 * 5 = 4.5
    assert pinball_loss(y, over_pred, 0.9) == pytest.approx(0.5)
    assert pinball_loss(y, under_pred, 0.9) == pytest.approx(4.5)


def test_pinball_loss_leere_eingabe_ist_nan():
    y = np.array([np.nan, np.nan])
    p = np.array([1.0, 2.0])
    assert np.isnan(pinball_loss(y, p, 0.5))


def test_abdeckung_intervall_zaehlt_inner_punkte():
    y = np.array([5.0, 10.0, 15.0, 20.0])
    lo = np.array([0.0, 5.0, 10.0, 25.0])
    hi = np.array([10.0, 15.0, 12.0, 30.0])
    # 5 in [0,10] ✓, 10 in [5,15] ✓, 15 nicht in [10,12], 20 nicht in [25,30]
    assert abdeckung_intervall(y, lo, hi) == pytest.approx(0.5)


# --- Training: 9 Modelle ---


def _synth_trainingsdaten(n: int = 400) -> pd.DataFrame:
    """Erzeugt reproduzierbare synthetische Trainingsdaten."""
    rng = np.random.default_rng(42)
    t0 = datetime(2026, 2, 1)
    zeiten = [t0 + timedelta(hours=i) for i in range(n)]
    feuchte = 50 + 10 * np.sin(np.arange(n) / 24) + rng.normal(0, 2, n)
    temperatur = 15 + 5 * np.sin(np.arange(n) / 24)
    et0 = 0.1 + rng.uniform(0, 0.2, n)

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
        "ziel_feuchte_6h": np.roll(feuchte, -6),
        "ziel_feuchte_12h": np.roll(feuchte, -12),
        "ziel_feuchte_24h": np.roll(feuchte, -24),
    })
    return df.iloc[:-24]  # letzte 24h haben kein gueltiges Ziel


def test_trainiere_quantile_erzeugt_neun_modelle_und_symlinks(tmp_path):
    df = _synth_trainingsdaten(400)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    ergebnisse = pipeline.trainiere_quantile(df)

    # 3 Horizonte × 3 Alphas = 9 Modelle
    assert len(ergebnisse) == 9
    for h in (6, 12, 24):
        for alpha in (10, 50, 90):
            assert (h, alpha) in ergebnisse

    # Je 3 Symlinks pro Horizont
    for h in (6, 12, 24):
        for alpha in (10, 50, 90):
            link = tmp_path / f"aktuell_{h}h_q{alpha}.lgbm"
            assert link.is_symlink(), f"Symlink fuer {h}h q{alpha} fehlt"


def test_quantile_q50_mae_bleibt_absolute_mae_und_pinball_ist_separat(tmp_path):
    df = _synth_trainingsdaten(400)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    ergebnisse = pipeline.trainiere_quantile(df, horizonte=[6])

    metriken = ergebnisse[(6, 50)].metriken

    assert metriken.mae > 0
    assert metriken.pinball_loss is not None
    assert metriken.baseline_pinball_loss is not None
    assert metriken.mae == pytest.approx(metriken.pinball_loss * 2, rel=0.25)
    assert metriken.baseline_mae == pytest.approx(
        metriken.baseline_pinball_loss * 2, rel=0.25,
    )


def test_quantile_modelle_hat_sinnvolle_reihenfolge_auf_synth_daten(tmp_path):
    """Auf genug Daten sollte q10 <= q50 <= q90 im Mittel gelten."""
    df = _synth_trainingsdaten(400)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    pipeline.trainiere_quantile(df)

    service = MLVorhersageService(modell_verzeichnis=str(tmp_path))
    service.lade_modelle()

    # Vorhersage auf einer Stichprobe
    sample = df.head(50).copy()
    ergebnisse = service.vorhersage(sample, horizont=6)
    assert ergebnisse is not None and len(ergebnisse) == 50

    q10s = [e.q10 for e in ergebnisse]
    q90s = [e.q90 for e in ergebnisse]
    assert all(q is not None for q in q10s)
    assert all(q is not None for q in q90s)
    # Im Mittel muss q90 > q10 sein (einzelnes Crossing laut Plan erlaubt).
    assert float(np.mean(q90s)) > float(np.mean(q10s))


# --- Service: Laden + Inferenz ---


def test_service_laedt_quantile_modelle_optional(tmp_path):
    """Ohne Quantile-Modelle funktioniert der Service weiter (q10/q90 = None)."""
    df = _synth_trainingsdaten(400)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    pipeline.trainiere(df)  # nur Punkt-Modelle

    service = MLVorhersageService(modell_verzeichnis=str(tmp_path))
    assert service.lade_modelle()

    sample = df.head(3)
    ergebnisse = service.vorhersage(sample, horizont=6)
    assert ergebnisse is not None
    assert all(e.q10 is None and e.q90 is None for e in ergebnisse)
    assert all(isinstance(e.feuchte_prognose, float) for e in ergebnisse)


def test_service_quantile_vorhersagen_werden_in_mlvorhersage_gesetzt(tmp_path):
    df = _synth_trainingsdaten(400)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    pipeline.trainiere(df)
    pipeline.trainiere_quantile(df)

    service = MLVorhersageService(modell_verzeichnis=str(tmp_path))
    service.lade_modelle()

    sample = df.head(1)
    ergebnisse = service.vorhersage(sample, horizont=6)
    assert ergebnisse is not None
    e = ergebnisse[0]
    assert e.q10 is not None and e.q90 is not None
    assert e.feuchte_prognose is not None


def test_service_top_features_nur_bei_details_flag(tmp_path):
    """T-0040: ohne details=True bleibt top_features None, mit True sind 5 sortierte Beitraege da."""
    df = _synth_trainingsdaten(400)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    pipeline.trainiere(df)

    service = MLVorhersageService(modell_verzeichnis=str(tmp_path))
    service.lade_modelle()

    sample = df.head(1)
    ohne = service.vorhersage(sample, horizont=6)
    assert ohne is not None
    assert ohne[0].top_features is None

    mit = service.vorhersage(sample, horizont=6, details=True)
    assert mit is not None
    tf = mit[0].top_features
    assert tf is not None
    assert len(tf) == 5
    # Sortiert absteigend nach |beitrag|
    betraege = [abs(f.beitrag) for f in tf]
    assert betraege == sorted(betraege, reverse=True)
    # Keine Bias-Spalte in Ausgabe
    assert all(f.name != "bias" for f in tf)
    # Feature-Namen entsprechen Trainings-Spalten
    trainings_features = set(service._feature_cols[6])
    assert all(f.name in trainings_features for f in tf)
    assert {f.skala for f in tf} == {"absolut"}


def test_quantile_crossing_korrigiert_band_ohne_q50_zu_aendern():
    service = MLVorhersageService(modell_verzeichnis="/tmp/nicht_verwendet")
    service._modelle[6] = _FakeModell(50.0)
    service._feature_cols[6] = ["boden_feuchte_aktuell"]
    service._quantile_modelle[(6, 10)] = _FakeModell(60.0)
    service._quantile_feature_cols[(6, 10)] = ["boden_feuchte_aktuell"]
    service._quantile_modelle[(6, 90)] = _FakeModell(55.0)
    service._quantile_feature_cols[(6, 90)] = ["boden_feuchte_aktuell"]

    ergebnisse = service.vorhersage(
        pd.DataFrame([{"boden_feuchte_aktuell": 80.0}]),
        horizont=6,
    )

    assert ergebnisse is not None
    e = ergebnisse[0]
    assert e.feuchte_prognose == 50.0
    assert e.q10 == 50.0
    assert e.q90 == 55.0


def test_quantile_crossing_sub_pp_wird_nicht_geloggt(caplog):
    """Numerisches Tree-Split-Rauschen (z. B. q90=70.72 vs. q50=70.76)
    soll keine Warning erzeugen — nur Crossings >= 1 pp sind fachlich
    relevant. Das Band wird trotzdem korrigiert (q90 := max(q90, q50))."""
    import logging
    service = MLVorhersageService(modell_verzeichnis="/tmp/nicht_verwendet")
    service._modelle[24] = _FakeModell(70.76)
    service._feature_cols[24] = ["boden_feuchte_aktuell"]
    service._quantile_modelle[(24, 10)] = _FakeModell(59.71)
    service._quantile_feature_cols[(24, 10)] = ["boden_feuchte_aktuell"]
    service._quantile_modelle[(24, 90)] = _FakeModell(70.72)  # 0.04 pp unter q50
    service._quantile_feature_cols[(24, 90)] = ["boden_feuchte_aktuell"]

    with caplog.at_level(logging.WARNING, logger="bewaesserung.ml.vorhersage"):
        ergebnisse = service.vorhersage(
            pd.DataFrame([{"boden_feuchte_aktuell": 65.0}]),
            horizont=24,
        )

    assert ergebnisse is not None
    e = ergebnisse[0]
    # Band weiterhin korrigiert, q50 unangetastet
    assert e.feuchte_prognose == 70.76
    assert e.q90 == 70.76
    # ABER: keine Warning fuer sub-pp-Noise
    assert not any("quantile_crossing" in r.message for r in caplog.records)


def test_feature_schema_veraltetes_modell_warnt_aber_blockt_nicht(caplog):
    """T-0076b: Modelle mit aelterem feature_schema_version laden weiter,
    aber loggen eine Warnung — UI/Drift-Auswertung kann das anzeigen."""
    import logging
    from bewaesserung.ml.modelle_ml import TrainingsMetriken
    service = MLVorhersageService(modell_verzeichnis="/tmp/nicht_verwendet")

    # Aelteres Schema (=1), Code erwartet 2.
    veraltet = TrainingsMetriken(
        zeitstempel="2026-01-01T00:00:00",
        horizont_stunden=6,
        anzahl_samples=1000, anzahl_features=10,
        mae=2.0, rmse=3.0, r2=0.5,
        baseline_mae=4.0, baseline_rmse=6.0, baseline_r2=0.0,
        feature_schema_version=1,
    )
    with caplog.at_level(logging.WARNING, logger="bewaesserung.ml.vorhersage"):
        service._pruefe_feature_schema(veraltet, "6h")
    assert any("ml.feature_schema_veraltet" in r.message
               for r in caplog.records)


def test_feature_schema_aktuelles_modell_keine_warnung(caplog):
    """Aktuelles Schema → keine Warning."""
    import logging
    from bewaesserung.ml.modelle_ml import TrainingsMetriken
    service = MLVorhersageService(modell_verzeichnis="/tmp/nicht_verwendet")

    aktuell = TrainingsMetriken(
        zeitstempel="2026-04-26T00:00:00",
        horizont_stunden=6,
        anzahl_samples=1000, anzahl_features=10,
        mae=2.0, rmse=3.0, r2=0.5,
        baseline_mae=4.0, baseline_rmse=6.0, baseline_r2=0.0,
        feature_schema_version=service.AKTUELLE_FEATURE_SCHEMA_VERSION,
    )
    with caplog.at_level(logging.WARNING, logger="bewaesserung.ml.vorhersage"):
        service._pruefe_feature_schema(aktuell, "6h")
    assert not any("ml.feature_schema_veraltet" in r.message
                   for r in caplog.records)


def test_quantile_crossing_ueber_1_pp_wird_geloggt(caplog):
    """Echte Modellprobleme (Crossing >= 1 pp) erzeugen weiterhin
    eine Warning, damit Drift sichtbar bleibt."""
    import logging
    service = MLVorhersageService(modell_verzeichnis="/tmp/nicht_verwendet")
    service._modelle[6] = _FakeModell(50.0)
    service._feature_cols[6] = ["boden_feuchte_aktuell"]
    service._quantile_modelle[(6, 10)] = _FakeModell(60.0)  # 10 pp ueber q50
    service._quantile_feature_cols[(6, 10)] = ["boden_feuchte_aktuell"]
    service._quantile_modelle[(6, 90)] = _FakeModell(55.0)
    service._quantile_feature_cols[(6, 90)] = ["boden_feuchte_aktuell"]

    with caplog.at_level(logging.WARNING, logger="bewaesserung.ml.vorhersage"):
        service.vorhersage(
            pd.DataFrame([{"boden_feuchte_aktuell": 80.0}]),
            horizont=6,
        )

    assert any(
        "quantile_crossing" in r.message and "crossing=10" in r.message
        for r in caplog.records
    )


# --- Drift-Log-Schema: q10/q90 spalten persistiert ---


def test_drift_log_akzeptiert_q10_q90(tmp_path):
    from bewaesserung.speicher import Speicher

    s = Speicher(str(tmp_path / "drift.db"))
    _run(s.verbinden())
    try:
        jetzt = datetime(2026, 4, 19, 10, 0)
        _run(s.logge_ml_vorhersage(
            zeitstempel=jetzt,
            zone_id="bambuswald",
            horizont_h=6,
            prognose_ziel_zeit=jetzt + timedelta(hours=6),
            prognose_feuchte=42.0,
            modell_version="modell_6h_2026-04-19.lgbm",
            q10=38.5, q90=46.2,
        ))

        async def _pruef():
            assert s._db is not None
            async with s._db.execute(
                "SELECT prognose_q10, prognose_q90 FROM ml_vorhersage_log",
            ) as cur:
                zeile = await cur.fetchone()
            return zeile
        zeile = _run(_pruef())
        assert zeile is not None
        assert zeile[0] == pytest.approx(38.5)
        assert zeile[1] == pytest.approx(46.2)
    finally:
        _run(s.schliessen())
