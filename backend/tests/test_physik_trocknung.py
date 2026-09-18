"""Hybrid Stufe 1: Tests fuer das physikalische Trocknungs-Modul.

Verifiziert Formel-Korrektheit, ET0-Skalierung und Edge-Cases.
"""
from __future__ import annotations

import math

from bewaesserung.ml.physik_trocknung import (
    fitte_k_basis_phase,
    prognose_physik,
    tage_bis_zielfeuchte_physik,
)


def test_exponentialer_decay_24h():
    """Bei k=0.02, et0=basis, f0=70, wp=20:
    f(24) = 20 + 50 * exp(-0.48) ≈ 50.94.
    """
    out = prognose_physik(
        f_start=70.0, welkepunkt=20.0,
        k_basis_pro_h=0.02, et0_basis_mm_pro_h=0.1042,
        et0_zukunft_pro_h=[0.1042] * 24,
        horizont_h=24,
    )
    assert out is not None
    erwartet = 20.0 + 50.0 * math.exp(-0.48)
    assert abs(out - erwartet) < 0.01


def test_et0_skalierung_doppelt():
    """Doppelte ET0 -> doppelter k_eff -> schnellerer Decay.
    Vergleich: gleiche Konfig mit ET0 = 2 * et0_basis.
    """
    basis = 0.1042
    normal = prognose_physik(
        f_start=70.0, welkepunkt=20.0, k_basis_pro_h=0.02,
        et0_basis_mm_pro_h=basis, et0_zukunft_pro_h=[basis] * 24,
        horizont_h=24,
    )
    heiss = prognose_physik(
        f_start=70.0, welkepunkt=20.0, k_basis_pro_h=0.02,
        et0_basis_mm_pro_h=basis, et0_zukunft_pro_h=[basis * 2] * 24,
        horizont_h=24,
    )
    assert normal is not None and heiss is not None
    # Heisser Tag muss niedrigere Feuchte ergeben.
    assert heiss < normal


def test_welkepunkt_ist_asymptote():
    """Sehr langes Fenster -> Konvergenz Richtung Welkepunkt."""
    out = prognose_physik(
        f_start=70.0, welkepunkt=20.0, k_basis_pro_h=0.05,
        et0_basis_mm_pro_h=0.1042,
        et0_zukunft_pro_h=[0.1042] * 200,
        horizont_h=200,
    )
    assert out is not None
    assert out >= 20.0
    assert out < 22.0


def test_welkepunkt_none_returns_none():
    out = prognose_physik(
        f_start=70.0, welkepunkt=None, k_basis_pro_h=0.02,
        et0_basis_mm_pro_h=0.1042, et0_zukunft_pro_h=[],
        horizont_h=24,
    )
    assert out is None


def test_f_start_under_welkepunkt_keeps_start():
    """Wenn Sensor schon unter Welkepunkt liegt -> Modell nicht
    aussagekraeftig, f_start zurueckgeben (kein weiterer Decay)."""
    out = prognose_physik(
        f_start=15.0, welkepunkt=20.0, k_basis_pro_h=0.02,
        et0_basis_mm_pro_h=0.1042, et0_zukunft_pro_h=[0.1042] * 24,
        horizont_h=24,
    )
    assert out == 15.0


def test_horizon_null_returns_f_start():
    out = prognose_physik(
        f_start=70.0, welkepunkt=20.0, k_basis_pro_h=0.02,
        et0_basis_mm_pro_h=0.1042, et0_zukunft_pro_h=[],
        horizont_h=0,
    )
    assert out == 70.0


def test_et0_zu_kurz_wird_mit_basis_aufgefuellt():
    """Wenn nur 12 h ET0 vorliegen, aber 24 h Prognose gewuenscht ist,
    werden die fehlenden Stunden mit `et0_basis` aufgefuellt."""
    basis = 0.1042
    halb = prognose_physik(
        f_start=70.0, welkepunkt=20.0, k_basis_pro_h=0.02,
        et0_basis_mm_pro_h=basis, et0_zukunft_pro_h=[basis] * 12,
        horizont_h=24,
    )
    voll = prognose_physik(
        f_start=70.0, welkepunkt=20.0, k_basis_pro_h=0.02,
        et0_basis_mm_pro_h=basis, et0_zukunft_pro_h=[basis] * 24,
        horizont_h=24,
    )
    assert halb is not None and voll is not None
    # Auffuellen mit basis -> identisches Ergebnis (alle Werte = basis).
    assert abs(halb - voll) < 1e-6


def test_fitte_k_basis_phase_synthetisch():
    """Synthetisch generierte Phase mit k=0.02 sollte mit Grid-Search
    annaehernd `k=0.02` zurueckliefern, MAE ≈ 0."""
    wp = 20.0
    f0 = 70.0
    true_k = 0.02
    et0 = 0.1042
    sensor = [(0.0, f0)] + [
        (h, wp + (f0 - wp) * math.exp(-true_k * h))
        for h in [1, 3, 6, 12, 24, 48]
    ]
    out = fitte_k_basis_phase(
        sensor, welkepunkt=wp,
        et0_mittel_mm_pro_h=et0, et0_basis_mm_pro_h=et0,
    )
    assert out is not None
    k_basis, mae = out
    assert abs(k_basis - 0.02) < 1e-6
    assert mae < 1e-6


def test_fitte_k_basis_phase_zu_wenige_punkte():
    """Weniger als 3 Punkte -> None."""
    out = fitte_k_basis_phase(
        [(0.0, 70.0), (5.0, 60.0)],
        welkepunkt=20.0,
        et0_mittel_mm_pro_h=0.1042, et0_basis_mm_pro_h=0.1042,
    )
    assert out is None


def test_fitte_k_basis_phase_f0_unter_welkepunkt():
    """Startwert <= Welkepunkt -> None (Phase ungueltig)."""
    out = fitte_k_basis_phase(
        [(0.0, 18.0), (3.0, 17.0), (6.0, 16.0)],
        welkepunkt=20.0,
        et0_mittel_mm_pro_h=0.1042, et0_basis_mm_pro_h=0.1042,
    )
    assert out is None


# --- T-0279 Phase 2: tage_bis_zielfeuchte_physik (Metrik-Redesign 31.05.) ---
# Bezugslinie ist die Ziel-Feuchte (typisch optimum_min), NICHT der
# Welkepunkt. Der Welkepunkt ist die Decay-Asymptote -- "Tage bis
# Welkepunkt" ist als Trigger-Metrik unbrauchbar (asymptotisch quasi
# unendlich bzw. linear konstant). Erst eine Linie OBERHALB des
# Welkepunkts liefert ein feuchte-abhaengiges, kalibrierbares Signal.


def test_t0279_tage_bis_ziel_physik_plausibel():
    """f0=50, wp=32, ziel=40, k=0.01/h bei Basis-ET0. exp-Decay:
    f(t)=32+18*exp(-0.01*t). Ziel 40 erreicht bei 18*exp(-0.01t)=8 ->
    t=-100*ln(8/18)=81.1h ~ 3.4 Tage. Toleranz wegen Stunden-Raster."""
    out = tage_bis_zielfeuchte_physik(
        f_start=50.0, ziel_feuchte=40.0, welkepunkt=32.0,
        k_basis_pro_h=0.01, et0_basis_mm_pro_h=0.1042,
        et0_zukunft_pro_h=[0.1042] * 24,
    )
    assert out is not None
    assert 3.2 < out < 3.6


def test_t0279_tage_bis_ziel_physik_feuchteabhaengig():
    """Anders als 'Tage bis Welkepunkt' (degeneriert konstant) muss die
    Ziel-Metrik mit der Start-Feuchte steigen: trockener Start -> weniger
    Tage bis optimum_min."""
    feucht = tage_bis_zielfeuchte_physik(
        f_start=55.0, ziel_feuchte=40.0, welkepunkt=32.0,
        k_basis_pro_h=0.01, et0_basis_mm_pro_h=0.1042,
        et0_zukunft_pro_h=[0.1042] * 24,
    )
    trocken = tage_bis_zielfeuchte_physik(
        f_start=45.0, ziel_feuchte=40.0, welkepunkt=32.0,
        k_basis_pro_h=0.01, et0_basis_mm_pro_h=0.1042,
        et0_zukunft_pro_h=[0.1042] * 24,
    )
    assert feucht is not None and trocken is not None
    assert trocken < feucht


def test_t0279_tage_bis_ziel_physik_schneller_bei_hoherem_k():
    """Hoeheres k_basis -> Ziel-Linie frueher erreicht."""
    langsam = tage_bis_zielfeuchte_physik(
        f_start=50.0, ziel_feuchte=40.0, welkepunkt=32.0, k_basis_pro_h=0.01,
        et0_basis_mm_pro_h=0.1042, et0_zukunft_pro_h=[0.1042] * 24,
    )
    schnell = tage_bis_zielfeuchte_physik(
        f_start=50.0, ziel_feuchte=40.0, welkepunkt=32.0, k_basis_pro_h=0.05,
        et0_basis_mm_pro_h=0.1042, et0_zukunft_pro_h=[0.1042] * 24,
    )
    assert langsam is not None and schnell is not None
    assert schnell < langsam


def test_t0279_tage_bis_ziel_physik_schon_am_ziel():
    """Sensor schon auf/unter der Ziel-Linie -> 0.0 Tage."""
    out = tage_bis_zielfeuchte_physik(
        f_start=39.5, ziel_feuchte=40.0, welkepunkt=32.0, k_basis_pro_h=0.02,
        et0_basis_mm_pro_h=0.1042, et0_zukunft_pro_h=[],
    )
    assert out == 0.0


def test_t0279_tage_bis_ziel_physik_max_tage_cap():
    """Sehr feucht + langsamer Decay -> Cap bei max_tage statt None."""
    out = tage_bis_zielfeuchte_physik(
        f_start=95.0, ziel_feuchte=40.0, welkepunkt=32.0, k_basis_pro_h=0.002,
        et0_basis_mm_pro_h=0.1042, et0_zukunft_pro_h=[],
        max_tage=10.0,
    )
    assert out == 10.0


def test_t0279_tage_bis_ziel_physik_none_ohne_welkepunkt():
    out = tage_bis_zielfeuchte_physik(
        f_start=50.0, ziel_feuchte=40.0, welkepunkt=None, k_basis_pro_h=0.02,
        et0_basis_mm_pro_h=0.1042, et0_zukunft_pro_h=[],
    )
    assert out is None


def test_t0279_tage_bis_ziel_physik_none_ohne_ziel():
    out = tage_bis_zielfeuchte_physik(
        f_start=50.0, ziel_feuchte=None, welkepunkt=32.0, k_basis_pro_h=0.02,
        et0_basis_mm_pro_h=0.1042, et0_zukunft_pro_h=[],
    )
    assert out is None


def test_t0279_tage_bis_ziel_physik_none_wenn_ziel_unter_welkepunkt():
    """Degeneriert: Ziel-Linie auf/unter der Asymptote -> per Decay nie
    erreichbar -> None (proaktiver Trigger inaktiv, Safe-Default)."""
    out = tage_bis_zielfeuchte_physik(
        f_start=50.0, ziel_feuchte=30.0, welkepunkt=32.0, k_basis_pro_h=0.02,
        et0_basis_mm_pro_h=0.1042, et0_zukunft_pro_h=[0.1042] * 24,
    )
    assert out is None
