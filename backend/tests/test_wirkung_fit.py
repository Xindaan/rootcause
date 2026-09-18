"""T-0292: Tests fuer den Plateau-Wirkungs-Fit + Quality-Gate.

Kernnachweis: akzeptiert sauberes (dosis-dominiertes) Signal mit korrektem
wmax, lehnt rauschiges/nicht-dauer-dominiertes Signal (= die heutigen
Realdaten) korrekt ab -- damit kein Raten in die Live-Empfehlung gelangt.
"""
from __future__ import annotations

import numpy as np

from bewaesserung.ml.wirkung_fit import fitte_plateau


def _synthetisch_sauber(wmax: float, tau: float, sigma: float, seed: int):
    """(dauer_min, delta_pp)-Paare aus dem Plateau-Modell + kleinem Rauschen,
    ueber verschiedene Dauern (Spread)."""
    rng = np.random.default_rng(seed)
    pts = []
    for d in list(range(5, 61, 2)) * 3:  # Spread 5..60 min, n ~ 84
        true = wmax * (1.0 - np.exp(-d / tau))
        pts.append((float(d), float(true + rng.normal(0, sigma))))
    return pts


def test_fitte_plateau_akzeptiert_sauberes_signal():
    """Sauberes Plateau-Signal (wmax=20, tau=25) -> angenommen + korrekt."""
    fit = fitte_plateau(_synthetisch_sauber(20.0, 25.0, 0.8, seed=1))
    assert fit.angenommen, fit.grund
    assert 17.0 <= fit.wmax <= 23.0
    assert fit.r2 > 0.8
    # r0 = wmax/tau ~ 0.8
    assert 0.5 <= fit.r0 <= 1.2


def test_fitte_plateau_lehnt_rauschen_ab():
    """Delta unkorreliert zur Dauer (wie die Realdaten 06.06.) -> abgelehnt
    (R^2 ~ 0), kein wmax-Raten."""
    rng = np.random.default_rng(2)
    pts = [(float(d), float(rng.uniform(2, 40)))
           for d in list(range(5, 61, 2)) * 3]
    fit = fitte_plateau(pts)
    assert not fit.angenommen
    assert "Quality-Gate" in fit.grund or "instabil" in fit.grund


def test_fitte_plateau_zu_wenig_paare():
    fit = fitte_plateau([(10.0, 5.0), (20.0, 8.0)])
    assert not fit.angenommen
    assert "zu wenig" in fit.grund


def test_fitte_plateau_kein_dauer_spread():
    """Alle Laeufe gleiche Dauer -> tau unbestimmt -> abgelehnt."""
    pts = [(20.0, 5.0 + 0.05 * i) for i in range(40)]
    fit = fitte_plateau(pts)
    assert not fit.angenommen
    assert "Spread" in fit.grund
