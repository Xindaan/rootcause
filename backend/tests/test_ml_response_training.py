"""Tests fuer T-0065 ResponseTrainingsPipeline (Forward + Inverse)."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("pandas")

import numpy as np
import pandas as pd

from bewaesserung.ml.response_training import (
    ResponseTrainingsPipeline,
    _walk_forward_splits,
)


def _synthetik_events(n: int, seed: int = 7) -> pd.DataFrame:
    """Generiert eine halbwegs realistische Event-Tabelle (monotone Response).

    Regel: delta_6h = 0.02 * dauer_s * liter_pro_sekunde - 0.1 * f_vor
           - 0.2 * et0_nach_6h + noise. Das ist klar monoton in dauer_s.
    """
    rng = np.random.default_rng(seed)
    t0 = datetime(2026, 1, 1, 6, 0)
    zeilen = []
    for i in range(n):
        dauer_s = float(rng.integers(300, 1800))
        lps = float(rng.uniform(0.03, 0.12))
        f_vor = float(rng.uniform(25, 65))
        et0_nach = float(rng.uniform(0.5, 4.0))
        et0_vor = float(rng.uniform(0.5, 4.0))
        temp = float(rng.uniform(10, 25))
        vpd = float(rng.uniform(0.2, 1.8))
        delta = (
            0.02 * dauer_s * lps
            - 0.1 * f_vor
            - 0.2 * et0_nach
            + rng.normal(0, 0.5)
            + 10
        )
        zeilen.append({
            "zone_id": "waldblumenhain",
            "zeitstempel": (t0 + timedelta(hours=i * 6)).isoformat(),
            "event_id": i + 1,
            "shared_valve": False,
            "dauer_s": int(dauer_s),
            "liter_pro_sekunde": lps,
            "f_vor": f_vor,
            "f_vor_gradient_3h": rng.uniform(-0.5, 0.5),
            "f_vor_gradient_24h": rng.uniform(-0.5, 0.5),
            "et0_nach_6h": et0_nach,
            "niederschlag_nach_24h": float(rng.uniform(0, 1)),
            "temperatur_ereignis": temp,
            "vpd_mittel": vpd,
            "jahreszeit_sin": math.sin(2 * math.pi * i / 365),
            "jahreszeit_cos": math.cos(2 * math.pi * i / 365),
            "tageszeit_sin": math.sin(2 * math.pi * 6 / 24),
            "tageszeit_cos": math.cos(2 * math.pi * 6 / 24),
            "delta_6h": delta,
            "delta_12h": delta * 0.9,
            "delta_24h": delta * 0.6,
        })
    return pd.DataFrame(zeilen)


def test_walk_forward_splits_struktur():
    folds = _walk_forward_splits(n=12, k=3, min_train=3, min_test=1)
    assert len(folds) == 3
    # Train-Indizes sind streng aufsteigend
    for tr, te in folds:
        assert max(tr) < min(te)


def test_walk_forward_splits_respektiert_zeitlichen_gap():
    t0 = datetime(2026, 1, 1, 6, 0)
    zeiten = [(t0 + timedelta(hours=i * 6)).isoformat() for i in range(12)]

    folds = _walk_forward_splits(
        n=12, k=2, min_train=3, min_test=1,
        zeitstempel=zeiten, gap_stunden=24,
    )

    assert folds
    train_idx, test_idx = folds[0]
    assert max(train_idx) == 2
    # t[2] + 24h = t[6], also muss der erste Testindex mindestens 6 sein.
    assert min(test_idx) >= 6


def test_walk_forward_splits_zu_wenig_daten():
    assert _walk_forward_splits(n=2, k=3, min_train=3, min_test=1) == []


def test_walk_forward_splits_lehnt_unsortierte_zeitstempel_ab():
    """T-0076a: Wenn zeitstempel unsortiert reinkommt, bricht der 24h-Gap-
    Schutz still — Test-Folds koennen Trainings-Zeitpunkte ueberlappen
    (Leakage). Daher hartes Fail-Fast statt Silent-Bug."""
    t0 = datetime(2026, 1, 1, 6, 0)
    zeiten = [(t0 + timedelta(hours=i * 6)).isoformat() for i in range(12)]
    # Zeit-3 und Zeit-8 vertauschen → unsortiert
    zeiten[3], zeiten[8] = zeiten[8], zeiten[3]

    with pytest.raises(ValueError, match="monotonic_increasing"):
        _walk_forward_splits(
            n=12, k=2, min_train=3, min_test=1,
            zeitstempel=zeiten, gap_stunden=24,
        )


def test_fallback_bei_n_kleiner_min(tmp_path):
    pipe = ResponseTrainingsPipeline(
        zone_id="waldblumenhain",
        basis_verzeichnis=tmp_path,
        min_events=10,
    )
    df = _synthetik_events(n=4)
    erg = pipe.lauf(df)
    assert erg.status == "uebersprungen"
    assert erg.n_events == 4
    # Keine Symlinks angelegt.
    assert not list((tmp_path / "waldblumenhain").glob("aktuell_*"))


def test_lauf_ueberspringt_wenn_kein_cv_fold_mit_gap_moeglich(tmp_path):
    pipe = ResponseTrainingsPipeline(
        zone_id="waldblumenhain",
        basis_verzeichnis=tmp_path,
        min_events=5,
    )
    df = _synthetik_events(n=5)

    erg = pipe.lauf(df)

    assert erg.status == "uebersprungen"
    assert erg.grund == "keine_cv_folds"
    assert erg.inverse_mae_s is None
    assert not list((tmp_path / "waldblumenhain").glob("aktuell_*"))
    assert not list((tmp_path / "waldblumenhain").glob("_staging_*.lgbm"))


def test_erfolgreicher_lauf_setzt_symlinks(tmp_path):
    pipe = ResponseTrainingsPipeline(
        zone_id="waldblumenhain",
        basis_verzeichnis=tmp_path,
        min_events=5,
    )
    df = _synthetik_events(n=40)
    erg = pipe.lauf(df)
    assert erg.status == "ok", erg.grund
    assert erg.n_events == 40
    assert erg.version is not None
    assert set(erg.forward_metriken) == {10, 50, 90}
    assert erg.inverse_mae_s is not None and erg.inverse_mae_s > 0

    zone_dir = tmp_path / "waldblumenhain"
    # Aktuell-Symlinks vorhanden.
    for alpha in (10, 50, 90):
        link = zone_dir / f"aktuell_waldblumenhain_forward_q{alpha}.lgbm"
        assert link.exists() and link.is_symlink()
    assert (zone_dir / "aktuell_waldblumenhain_inverse.lgbm").is_symlink()
    meta_link = zone_dir / "aktuell_waldblumenhain_metadata.json"
    assert meta_link.is_symlink()
    meta = json.loads(meta_link.read_text())
    assert meta["zone_id"] == "waldblumenhain"
    assert meta["n_events"] == 40
    assert meta["inverse_mae_s"] is not None


def test_inverse_modell_respektiert_monotonie(tmp_path):
    """Property-Test: mehr ziel_delta → nicht weniger Dauer (ceteris paribus).

    Wir laden das soeben trainierte Modell zurueck und evaluieren auf
    einem einheitlichen Feature-Frame mit variierender ziel_delta.
    """
    import lightgbm as lgb  # noqa

    pipe = ResponseTrainingsPipeline(
        zone_id="waldblumenhain",
        basis_verzeichnis=tmp_path,
        min_events=5,
    )
    df = _synthetik_events(n=60)
    erg = pipe.lauf(df)
    assert erg.status == "ok"

    modell = lgb.Booster(
        model_file=str(tmp_path / "waldblumenhain" / "aktuell_waldblumenhain_inverse.lgbm")
    )
    fcols = erg.feature_cols_inverse
    basis = {col: 0.0 for col in fcols}
    basis["f_vor"] = 35.0
    basis["liter_pro_sekunde"] = 0.08
    basis["et0_nach_6h"] = 2.0
    basis["vpd_mittel"] = 0.8
    basis["temperatur_ereignis"] = 18.0

    rows = []
    for delta in (5.0, 10.0, 20.0, 30.0):
        row = dict(basis)
        row["ziel_delta"] = delta
        rows.append([row[c] for c in fcols])
    X = np.array(rows, dtype=float)
    preds = modell.predict(X)
    # Monotonie: nicht fallend bei steigendem ziel_delta.
    for i in range(1, len(preds)):
        assert preds[i] >= preds[i - 1] - 1e-6, (
            f"Monotonie verletzt: pred[{i-1}]={preds[i-1]} "
            f"pred[{i}]={preds[i]}"
        )
