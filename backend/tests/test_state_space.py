"""T-0353: Tests fuer den State-Space-Shadow-Forecaster (pure Funktionen)."""
from __future__ import annotations

from datetime import datetime, timedelta

from bewaesserung.ml.physik_trocknung import prognose_physik
from bewaesserung.ml.state_space import (
    GiessPuls,
    PULS_MAX_PP_FALLBACK,
    StateSpaceParams,
    prognose_statespace,
    puls_magnitude_pp,
)

T0 = datetime(2026, 6, 1, 12, 0)
ET0_BASIS = 0.1042


def _params(**kw) -> StateSpaceParams:
    basis = dict(
        welkepunkt=30.0,
        k_basis_pro_h=0.02,
        et0_basis_mm_pro_h=ET0_BASIS,
        wirkung_max_pp=None,
        wirkungsrate_initial=None,
        delta_pp_pro_minute=None,
        regen_faktor_pp_pro_mm=4.0,
        ramp_stunden=1.5,
        obergrenze=100.0,
    )
    basis.update(kw)
    return StateSpaceParams(**basis)


# ---------------------------------------------------------------------------
# Paritaet: ohne Regen + ohne Pulse MUSS state_space == prognose_physik sein
# (Regressionstest gegen Decay-Formel-Drift, Single-Source-Anspruch).
# ---------------------------------------------------------------------------

def test_paritaet_mit_prognose_physik_ohne_inputs():
    et0 = [0.05, 0.2, 0.1, 0.0, 0.15] + [ET0_BASIS] * 19
    # 25.0 = Start UNTER Welkepunkt: prognose_physik gibt f_start
    # unveraendert zurueck — der Special-Case muss identisch sein
    # (Verifier-Review F5).
    for f_start in (75.0, 45.0, 31.0, 25.0):
        for horizont in (1, 6, 24):
            phys = prognose_physik(
                f_start=f_start, welkepunkt=30.0, k_basis_pro_h=0.02,
                et0_basis_mm_pro_h=ET0_BASIS, et0_zukunft_pro_h=et0,
                horizont_h=horizont,
            )
            ss = prognose_statespace(
                f_start=f_start, t_start=T0, horizont_h=horizont,
                params=_params(), et0_zukunft_pro_h=et0,
                regen_zukunft_pro_h=[], pulse=[],
            )
            assert ss is not None and phys is not None
            assert abs(ss - phys) < 1e-9, (f_start, horizont)


def test_unter_welkepunkt_kein_weiterer_decay():
    """f_start unter Welkepunkt: kein Decay (Vertrag wie prognose_physik)."""
    ss = prognose_statespace(
        f_start=25.0, t_start=T0, horizont_h=24,
        params=_params(), et0_zukunft_pro_h=[], regen_zukunft_pro_h=[],
        pulse=[],
    )
    assert ss == 25.0


def test_none_pfade():
    assert prognose_statespace(
        f_start=None, t_start=T0, horizont_h=6, params=_params(),
        et0_zukunft_pro_h=[], regen_zukunft_pro_h=[], pulse=[],
    ) is None
    assert prognose_statespace(
        f_start=50.0, t_start=T0, horizont_h=6,
        params=_params(et0_basis_mm_pro_h=0.0),
        et0_zukunft_pro_h=[], regen_zukunft_pro_h=[], pulse=[],
    ) is None
    # horizont 0 -> f_start unveraendert.
    assert prognose_statespace(
        f_start=50.0, t_start=T0, horizont_h=0, params=_params(),
        et0_zukunft_pro_h=[], regen_zukunft_pro_h=[], pulse=[],
    ) == 50.0


# ---------------------------------------------------------------------------
# Puls-Magnitude (Plateau, nicht dauer-linear).
# ---------------------------------------------------------------------------

def test_puls_magnitude_plateau_saettigt():
    p = _params(wirkung_max_pp=20.0, wirkungsrate_initial=0.4)
    kurz = puls_magnitude_pp(p, 10 * 60)
    lang = puls_magnitude_pp(p, 180 * 60)
    sehr_lang = puls_magnitude_pp(p, 600 * 60)
    assert 0 < kurz < lang < sehr_lang <= 20.0
    # Saettigung: Verdreifachung der Dauer bringt kaum noch Zuwachs.
    assert sehr_lang - lang < 2.0
    # Initiale Rate: kleine Dosen ~ r0 * dauer.
    assert abs(kurz - 0.4 * 10) < 0.5


def test_puls_magnitude_linear_fallback_mit_deckel():
    p = _params(delta_pp_pro_minute=0.33)
    assert abs(puls_magnitude_pp(p, 60 * 60) - 0.33 * 60) < 1e-9
    # Ohne wirkung_max_pp greift der konservative Fallback-Deckel.
    assert puls_magnitude_pp(p, 600 * 60) == PULS_MAX_PP_FALLBACK
    # Mit wirkung_max_pp (aber ohne r0) deckelt wirkung_max_pp.
    p2 = _params(delta_pp_pro_minute=0.33, wirkung_max_pp=15.0)
    assert puls_magnitude_pp(p2, 600 * 60) == 15.0


def test_puls_magnitude_ohne_parameter_null():
    """Ohne Wirkungs-Parameter wird NICHT geraten (degeneriert zu Physik)."""
    assert puls_magnitude_pp(_params(), 3600) == 0.0


# ---------------------------------------------------------------------------
# Puls-Timing: Sensor-Verzoegerung als Ramp.
# ---------------------------------------------------------------------------

def test_puls_im_fenster_voll_materialisiert():
    """Puls endet 2h nach t_start -> volle Magnitude im 24h-Horizont."""
    p = _params(delta_pp_pro_minute=0.33, k_basis_pro_h=0.0)
    puls = GiessPuls(t_ende=T0 + timedelta(hours=2), dauer_s=60 * 60)
    ohne = prognose_statespace(
        f_start=40.0, t_start=T0, horizont_h=24, params=p,
        et0_zukunft_pro_h=[], regen_zukunft_pro_h=[], pulse=[],
    )
    mit = prognose_statespace(
        f_start=40.0, t_start=T0, horizont_h=24, params=p,
        et0_zukunft_pro_h=[], regen_zukunft_pro_h=[], pulse=[puls],
    )
    assert abs((mit - ohne) - 0.33 * 60) < 1e-9


def test_puls_vor_t_start_nur_restanteil():
    """Puls endete 0.75h VOR t_start bei ramp=1.5h -> die Haelfte der
    Wirkung steckt schon in f_start, nur der Rest kommt in die Trajektorie."""
    p = _params(delta_pp_pro_minute=0.33, k_basis_pro_h=0.0, ramp_stunden=1.5)
    puls = GiessPuls(t_ende=T0 - timedelta(minutes=45), dauer_s=60 * 60)
    mit = prognose_statespace(
        f_start=40.0, t_start=T0, horizont_h=6, params=p,
        et0_zukunft_pro_h=[], regen_zukunft_pro_h=[], pulse=[puls],
    )
    erwartet_rest = 0.33 * 60 * 0.5
    assert abs((mit - 40.0) - erwartet_rest) < 1e-9


def test_puls_komplett_vor_ramp_fenster_ignoriert():
    """Puls, dessen Ramp vor t_start abgeschlossen ist, aendert nichts."""
    p = _params(delta_pp_pro_minute=0.33, k_basis_pro_h=0.0)
    puls = GiessPuls(t_ende=T0 - timedelta(hours=3), dauer_s=60 * 60)
    mit = prognose_statespace(
        f_start=40.0, t_start=T0, horizont_h=6, params=p,
        et0_zukunft_pro_h=[], regen_zukunft_pro_h=[], pulse=[puls],
    )
    assert mit == 40.0


def test_kurzer_horizont_sieht_nur_ramp_anteil():
    """1h-Horizont bei ramp=2h: nur die Haelfte der Puls-Wirkung sichtbar."""
    p = _params(delta_pp_pro_minute=0.33, k_basis_pro_h=0.0, ramp_stunden=2.0)
    puls = GiessPuls(t_ende=T0, dauer_s=60 * 60)
    mit = prognose_statespace(
        f_start=40.0, t_start=T0, horizont_h=1, params=p,
        et0_zukunft_pro_h=[], regen_zukunft_pro_h=[], pulse=[puls],
    )
    assert abs((mit - 40.0) - 0.33 * 60 * 0.5) < 1e-9


def test_puls_hebt_ueber_welkepunkt_dann_decay():
    """Start unter wp, Puls hebt darueber, danach greift der Decay wieder."""
    p = _params(
        welkepunkt=30.0, delta_pp_pro_minute=0.4, k_basis_pro_h=0.05,
        ramp_stunden=1.0,
    )
    puls = GiessPuls(t_ende=T0 + timedelta(hours=1), dauer_s=30 * 60)
    mit = prognose_statespace(
        f_start=28.0, t_start=T0, horizont_h=24, params=p,
        et0_zukunft_pro_h=[ET0_BASIS] * 24, regen_zukunft_pro_h=[],
        pulse=[puls],
    )
    # Ueber wp gehoben (+12pp brutto), aber durch Decay wieder Richtung wp.
    assert 30.0 < mit < 40.0


# ---------------------------------------------------------------------------
# Regen-Input + Obergrenze.
# ---------------------------------------------------------------------------

def test_regen_zufluss():
    p = _params(k_basis_pro_h=0.0, regen_faktor_pp_pro_mm=4.0)
    mit = prognose_statespace(
        f_start=40.0, t_start=T0, horizont_h=6, params=p,
        et0_zukunft_pro_h=[], regen_zukunft_pro_h=[0.0, 1.5, 0.5],
        pulse=[],
    )
    assert abs(mit - (40.0 + 4.0 * 2.0)) < 1e-9


def test_obergrenze_clippt():
    p = _params(k_basis_pro_h=0.0, obergrenze=56.0)
    mit = prognose_statespace(
        f_start=50.0, t_start=T0, horizont_h=6, params=p,
        et0_zukunft_pro_h=[], regen_zukunft_pro_h=[5.0, 5.0], pulse=[],
    )
    assert mit == 56.0
