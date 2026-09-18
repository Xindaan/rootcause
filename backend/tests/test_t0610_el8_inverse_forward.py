"""T-0610 EL-8: die beiden Prognose-Quellen der Drift-Ampel.

- Heuristik: `_wirkung_pp_fuer_dauer` ist die Inverse von `_berechne_dauer`.
  Gegenprobe ueber die Hin-und-Rueck-Rechnung: aus einem Ziel-Delta die
  Dauer, aus der Dauer wieder das Delta.
- ML: `MLResponseService.forward_delta` rechnet mit dem deployten
  Forward-q50-Modell und verweigert bei fehlender Merkmalsspalte (keine
  0-Imputation, vgl. T-0564).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from bewaesserung.modelle import ZonenKonfig, ZonenModus
from test_entscheidung import baue_motor


def _zone(**k) -> ZonenKonfig:
    basis = dict(
        zone_id="z", name="z", modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=35.0, feuchte_kritisch=20.0,
        max_dauer_sekunden=36000,
    )
    basis.update(k)
    return ZonenKonfig(**basis)


@pytest.mark.parametrize(
    ("zone_kw", "deltas", "toleranz"),
    [
        ({"delta_pp_pro_minute": 0.5}, [1.0, 4.0, 12.0], 0.02),
        ({"wirkung_max_pp": 20.0, "wirkungsrate_initial": 1.0}, [1.0, 8.0, 18.0], 0.02),
        # Log-Decay: `_berechne_dauer` naehert den Fixpunkt in 2 Schritten,
        # die Inverse rechnet exakt -- daher eine relative Toleranz.
        ({"delta_pp_pro_minute": 0.5, "wirkungsrate_dauer_alpha": -0.2}, [4.0, 20.0, 40.0], 0.06),
    ],
    ids=["linear", "plateau", "log_decay"],
)
@pytest.mark.parametrize("et0_6h", [0.0, 3.0])
def test_inverse_berechne_dauer_hin_und_zurueck(zone_kw, deltas, toleranz, et0_6h):
    zone = _zone(**zone_kw)
    motor, _ = baue_motor(zone)
    for delta in deltas:
        dauer = motor._berechne_dauer(
            zone, 20.0, et0_6h, ziel_schwelle=20.0 + delta, clip_auf_max=False,
        )
        zurueck = motor._wirkung_pp_fuer_dauer(zone, dauer, et0_6h)
        assert zurueck == pytest.approx(delta, rel=toleranz, abs=0.02), (delta, dauer)


def test_heuristik_wirkung_pp_nutzt_aufgeloeste_rate():
    """600 s bei 0,5 pp/min ohne Verdunstung = 5 pp; mit ET0 3 mm in 6 h
    teilt der Verdunstungsfaktor 1,5 die wirksame Zeit. Unbekannte Zone: None."""
    zone = _zone(delta_pp_pro_minute=0.5)
    motor, _ = baue_motor(zone)
    t = datetime(2026, 4, 22, 6, 0)
    assert asyncio.run(motor.heuristik_wirkung_pp("z", 600, 0.0, t)) == pytest.approx(5.0)
    assert asyncio.run(motor.heuristik_wirkung_pp("z", 600, 3.0, t)) == pytest.approx(5.0 / 1.5)
    assert asyncio.run(motor.heuristik_wirkung_pp("fremd", 600, 0.0, t)) is None


def test_forward_delta_rechnet_mit_forward_modell_und_verweigert_bei_luecke(tmp_path):
    lgb = pytest.importorskip("lightgbm")
    np = pytest.importorskip("numpy")
    from bewaesserung.ml.response_vorhersage import MLResponseService

    rng = np.random.default_rng(0)
    dauer = rng.uniform(60, 3600, 400)
    f_vor = rng.uniform(10, 50, 400)
    y = dauer / 100.0  # 600 s -> 6 pp
    booster = lgb.train(
        {"objective": "regression", "verbose": -1, "min_data_in_leaf": 5, "num_threads": 1},
        lgb.Dataset(np.column_stack([dauer, f_vor]), label=y), num_boost_round=80,
    )
    zone_dir = tmp_path / "z"
    zone_dir.mkdir()
    booster.save_model(str(zone_dir / "aktuell_z_forward_q50.lgbm"))
    (zone_dir / "aktuell_z_metadata.json").write_text(json.dumps({
        "feature_cols_forward": ["dauer_s", "f_vor"],
        "feature_cols_inverse": ["ziel_delta", "f_vor"],
    }))
    service = MLResponseService(tmp_path)

    wert = service.forward_delta("z", {"dauer_s": 600, "f_vor": 30.0, "extra": 1})
    assert wert == pytest.approx(6.0, abs=1.0)
    assert service.forward_delta("z", {"dauer_s": 600}) is None
    assert service.forward_delta("z", {"dauer_s": 600, "f_vor": float("nan")}) is None
    assert service.forward_delta("ohne_modell", {"dauer_s": 600, "f_vor": 30.0}) is None
