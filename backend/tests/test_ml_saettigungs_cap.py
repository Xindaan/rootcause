"""T-0061a: Post-Processing-Regel gegen Regen-Ignoranz.

Bei Sensor-Saettigung (>95 %) + angekuendigtem Regen (>5 mm) hebt das
Modell-Output nachtraeglich auf `aktuell - 2` an. Verhindert unrealistische
"Feuchte faellt trotz Dauerregen"-Prognosen.
"""
from __future__ import annotations

import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("pandas")

import numpy as np
import pandas as pd

from bewaesserung.ml.training import (
    GEWICHT_NORMAL,
    GEWICHT_SELTEN,
    TrainingsPipeline,
    _berechne_sample_weights,
)
from bewaesserung.ml.vorhersage import MLVorhersageService


def _synth_daten(n: int = 400):
    """Reproduzierbare synthetische Trainingsdaten (wie in test_ml_quantile)."""
    from datetime import datetime, timedelta
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
    return df.iloc[:-24]


@pytest.fixture
def service(tmp_path):
    df = _synth_daten(400)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    pipeline.trainiere(df)
    s = MLVorhersageService(modell_verzeichnis=str(tmp_path))
    assert s.lade_modelle()
    return s


def test_cap_greift_bei_saettigung_plus_regen(service):
    """Feuchte=100, Regen=10 mm → Prognose muss >= 98 (= aktuell - 2) sein."""
    df = pd.DataFrame([{
        "zone_id": "bambuswald",
        "boden_feuchte_aktuell": 100.0,
        "boden_temperatur": 5.0,
        "feuchte_trend_6h": 0.0,
        "niederschlag_summe_6h": 10.0,
        "niederschlag_summe_12h": 15.0,
        "niederschlag_summe_24h": 20.0,
        "et0_summe_6h": 0.1,
        "et0_summe_12h": 0.2,
        "et0_summe_24h": 0.5,
        "quelle": "gardena",
    }])
    ergebnisse = service.vorhersage(df, horizont=6)
    assert ergebnisse is not None
    assert ergebnisse[0].feuchte_prognose >= 98.0, (
        f"Cap greift nicht: Prognose={ergebnisse[0].feuchte_prognose}"
    )


def test_cap_nicht_aktiv_ohne_regen(service):
    """Feuchte=100, Regen=0 → Modell-Output unveraendert (kein Cap)."""
    df = pd.DataFrame([{
        "zone_id": "bambuswald",
        "boden_feuchte_aktuell": 100.0,
        "boden_temperatur": 15.0,
        "feuchte_trend_6h": 0.0,
        "niederschlag_summe_6h": 0.0,
        "niederschlag_summe_12h": 0.0,
        "niederschlag_summe_24h": 0.0,
        "et0_summe_6h": 0.3,
        "et0_summe_12h": 0.6,
        "et0_summe_24h": 1.2,
        "quelle": "gardena",
    }])
    ergebnisse = service.vorhersage(df, horizont=6)
    # Ohne Regen darf Modell Mean-Reversion spielen — wir erwarten
    # klar unter 95 (sonst waere der Test sinnlos).
    assert ergebnisse[0].feuchte_prognose < 95.0


def test_cap_nicht_aktiv_unter_allen_stufen(service):
    """Feuchte=70 (< 75), Regen=20 → kein Cap, alle drei Stufen verfehlen.
    T-0130 (H-6): nach Einfuehrung der mittel/weit-Stufen muss die Untergrenze
    der niedrigsten Stufe (75 %) wirklich beachtet werden."""
    df = pd.DataFrame([{
        "zone_id": "bambuswald",
        "boden_feuchte_aktuell": 70.0,
        "boden_temperatur": 10.0,
        "feuchte_trend_6h": 0.0,
        "niederschlag_summe_6h": 20.0,
        "niederschlag_summe_12h": 25.0,
        "niederschlag_summe_24h": 30.0,
        "et0_summe_6h": 0.1,
        "et0_summe_12h": 0.2,
        "et0_summe_24h": 0.5,
        "quelle": "gardena",
    }])
    ergebnisse = service.vorhersage(df, horizont=6)
    # Bei feuchte=70 + Mean-Reversion erwartet das Modell eine Annaeherung
    # an den Mittelwert (~50 %). Wichtig: keine der Stufen-Untergrenzen
    # (70-2=68, 70-4=66, 70-6=64) wird als untere Klemme angewendet.
    assert ergebnisse[0].feuchte_prognose < 64.0


def test_cap_mittlere_stufe_85_plus_regen(service):
    """T-0130 (H-6): feuchte=88, regen=10 mm -> mittlere Stufe greift,
    Prognose muss >= 84 (= 88 - 4) sein."""
    df = pd.DataFrame([{
        "zone_id": "bambuswald",
        "boden_feuchte_aktuell": 88.0,
        "boden_temperatur": 10.0,
        "feuchte_trend_6h": 0.0,
        "niederschlag_summe_6h": 10.0,
        "niederschlag_summe_12h": 12.0,
        "niederschlag_summe_24h": 18.0,
        "et0_summe_6h": 0.1,
        "et0_summe_12h": 0.2,
        "et0_summe_24h": 0.5,
        "quelle": "gardena",
    }])
    ergebnisse = service.vorhersage(df, horizont=6)
    assert ergebnisse[0].feuchte_prognose >= 84.0


def test_cap_weite_stufe_75_plus_starkregen(service):
    """T-0130 (H-6): feuchte=80, regen=15 mm -> weite Stufe greift,
    Prognose muss >= 74 (= 80 - 6) sein. Pre-Mortem-Akt-5-Szenario:
    sommerlicher Bambus bei 80 % nach 5 Tagen Dauerregen."""
    df = pd.DataFrame([{
        "zone_id": "bambuswald",
        "boden_feuchte_aktuell": 80.0,
        "boden_temperatur": 10.0,
        "feuchte_trend_6h": 0.0,
        "niederschlag_summe_6h": 15.0,
        "niederschlag_summe_12h": 20.0,
        "niederschlag_summe_24h": 30.0,
        "et0_summe_6h": 0.1,
        "et0_summe_12h": 0.2,
        "et0_summe_24h": 0.5,
        "quelle": "gardena",
    }])
    ergebnisse = service.vorhersage(df, horizont=6)
    assert ergebnisse[0].feuchte_prognose >= 74.0


def test_sample_weights_normal_bei_null_regen():
    """T-0127: ohne Regen Gewicht = 1.0 (kontinuierliche Skala startet hier)."""
    df = pd.DataFrame({
        "niederschlag_summe_24h": [0.0, 0.0, 0.0],
        "boden_feuchte_aktuell": [40.0, 50.0, 70.0],
    })
    weights = _berechne_sample_weights(df)
    assert (weights == GEWICHT_NORMAL).all()


def test_sample_weights_kontinuierlich_mit_regen():
    """T-0127: lineare Skala — 0 mm = 1.0, 5 mm = 2.0, 10+ mm = 3.0."""
    df = pd.DataFrame({
        "niederschlag_summe_24h": [0.0, 2.5, 5.0, 7.5, 10.0, 25.0],
        "boden_feuchte_aktuell": [40.0, 40.0, 40.0, 40.0, 40.0, 40.0],
    })
    weights = _berechne_sample_weights(df)
    # Skala: weight = 1.0 + regen/5, capped bei 3.0
    assert weights[0] == pytest.approx(1.0)
    assert weights[1] == pytest.approx(1.5)
    assert weights[2] == pytest.approx(2.0)
    assert weights[3] == pytest.approx(2.5)
    assert weights[4] == pytest.approx(3.0)  # genau am Cap
    assert weights[5] == pytest.approx(3.0)  # capped (nicht 6.0)


def test_sample_weights_kleiner_regen_bekommt_kleines_gewicht():
    """T-0127 Kernpunkt: 2.5 mm Regen erzeugt jetzt erhoehtes Gewicht (1.5),
    nicht 1.0 wie unter alter binaerer 10-mm-Schwelle. Audit-Realfall:
    Regentage waren oft <10 mm und wurden komplett ignoriert."""
    df = pd.DataFrame({
        "niederschlag_summe_24h": [2.5],
        "boden_feuchte_aktuell": [50.0],
    })
    weights = _berechne_sample_weights(df)
    assert weights[0] > GEWICHT_NORMAL
    assert weights[0] == pytest.approx(1.5)


def test_sample_weights_saettigung_wird_erhoeht():
    """T-0061b: Feuchte >= 90 % → Gewicht 3.0 (auch ohne Regen)."""
    df = pd.DataFrame({
        "niederschlag_summe_24h": [0.0, 0.0, 0.0],
        "boden_feuchte_aktuell": [89.9, 90.0, 100.0],
    })
    weights = _berechne_sample_weights(df)
    assert weights[0] == GEWICHT_NORMAL
    assert weights[1] == GEWICHT_SELTEN
    assert weights[2] == GEWICHT_SELTEN


def test_sample_weights_fehlende_spalten_geben_normal():
    """Backward-Kompat: wenn Features fehlen, werden alle Gewichte = 1."""
    df = pd.DataFrame({"ziel_feuchte_6h": [40.0, 50.0]})
    weights = _berechne_sample_weights(df)
    assert (weights == GEWICHT_NORMAL).all()


def test_training_mit_weighting_laeuft_durch(tmp_path):
    """T-0061b: End-to-end — Pipeline trainiert auch mit Weighting ohne Fehler."""
    df = _synth_daten(400)
    # Einen Regen-Event reinstreuen
    df.loc[df.index[:50], "niederschlag_summe_24h"] = 15.0
    df.loc[df.index[50:80], "boden_feuchte_aktuell"] = 95.0
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    ergebnisse = pipeline.trainiere(df)
    # 3 Horizonte (6/12/24h) sollten trainieren
    assert set(ergebnisse.keys()) == {6, 12, 24}


def test_cap_haelt_quantile_sortierung(service):
    """Mit Quantile-Modellen muss nach Cap weiterhin q10 <= q50 <= q90 gelten."""
    from pathlib import Path
    # Quantile-Modelle zusaetzlich trainieren
    df = _synth_daten(400)
    pipeline = TrainingsPipeline(
        modell_verzeichnis=str(Path(service._modell_dir)),
        n_folds=2,
    )
    pipeline.trainiere_quantile(df)
    service._quantile_modelle.clear()
    service._quantile_feature_cols.clear()
    assert service.lade_modelle()

    df_in = pd.DataFrame([{
        "zone_id": "bambuswald",
        "boden_feuchte_aktuell": 100.0,
        "boden_temperatur": 5.0,
        "feuchte_trend_6h": 0.0,
        "niederschlag_summe_6h": 10.0,
        "niederschlag_summe_12h": 15.0,
        "niederschlag_summe_24h": 20.0,
        "et0_summe_6h": 0.1,
        "et0_summe_12h": 0.2,
        "et0_summe_24h": 0.5,
        "quelle": "gardena",
    }])
    ergebnisse = service.vorhersage(df_in, horizont=6)
    assert ergebnisse is not None
    e = ergebnisse[0]
    assert e.q10 is not None and e.q90 is not None
    # Sortier-Invariante bleibt erhalten
    assert e.q10 <= e.feuchte_prognose <= e.q90
    # q50 >= 98
    assert e.feuchte_prognose >= 98.0
