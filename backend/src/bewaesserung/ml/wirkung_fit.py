"""T-0292: Plateau-Wirkungs-Fit (wmax / r0) mit Quality-Gate.

Pure-Funktion -- fittet das Plateau-Modell

    total_wirkung(d) = wmax * (1 - exp(-d / tau))

aus (dauer_min, delta_pp)-Paaren echter Bewaesserungs-Laeufe.
`wmax` = Plateau (max pp je Einzeldose), `r0 = wmax / tau` = initiale Rate.
Diese Parameter steuern `_berechne_dauer` (entscheidung.py); heute aus der
Konfig (`zone.wirkung_max_pp` / `zone.wirkungsrate_initial`).

WICHTIGER BEFUND (T-0291/T-0292): Beobachtungsdaten sind oft NICHT
dosis-dauer-dominiert -- Regen, Startfeuchte, Verdunstung und die 5pp-
Quantisierung ueberlagern die Dosis-Wirkung (Realdaten 06.06.: delta
2-40 pp bei gleicher Dauer-Spanne). Ein naiver Fit liefert dann instabilen
Muell. Daher STRENGE Quality-Gates: ein Fit wird nur als verwertbar
(`angenommen=True`) markiert, wenn er

  1. genug Paare hat (`min_n`),
  2. ueber verschiedene Dauern streut (`min_dauer_spread`, sonst ist tau
     unbestimmt),
  3. die Varianz erklaert (`R^2 >= min_r2`),
  4. ein kleines Residuum hat (`MSE <= max_mse_pp2`) und
  5. stabil ist -- curve_fit (scipy) und ein unabhaengiger Grid-Fit muessen
     bei `wmax` grob uebereinstimmen (Doppel-Modell-Gegencheck,
     `arbeitspattern_kalibrierung`).

Sonst: `angenommen=False` + `grund` -- der Aufrufer behaelt den Konfig-
Default (kein Raten). Die heutigen Realdaten fallen damit korrekt durch.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class WirkungFit:
    """Ergebnis eines Plateau-Fits inkl. Quality-Verdikt."""

    wmax: float
    r0: float
    tau: float
    n: int
    r2: float
    mse: float
    angenommen: bool
    grund: str


def _plateau(d: "np.ndarray", wmax: float, tau: float) -> "np.ndarray":
    return wmax * (1.0 - np.exp(-d / tau))


def fitte_plateau(
    paare: list[tuple[float, float]],
    *,
    min_n: int = 30,
    min_dauer_spread: float = 3.0,
    min_r2: float = 0.5,
    max_mse_pp2: float = 9.0,
    konsens_toleranz: float = 0.35,
) -> WirkungFit:
    """Fittet wmax/r0 aus (dauer_min, delta_pp)-Paaren mit Quality-Gate.

    Gibt IMMER ein `WirkungFit` zurueck; `angenommen` sagt, ob es verwertbar
    ist. Bei Ablehnung steht der Grund in `grund` und der Aufrufer soll den
    Konfig-Wert behalten.
    """
    n = len(paare)
    if n < min_n:
        return WirkungFit(0.0, 0.0, 0.0, n, 0.0, 0.0, False,
                          f"zu wenig Paare ({n} < {min_n})")

    d = np.array([p[0] for p in paare], dtype=float)
    y = np.array([p[1] for p in paare], dtype=float)
    spread = (d.max() / d.min()) if d.min() > 0 else 0.0
    if spread < min_dauer_spread:
        return WirkungFit(0.0, 0.0, 0.0, n, 0.0, 0.0, False,
                          f"Dauer-Spread zu klein ({spread:.1f} < "
                          f"{min_dauer_spread}) -> tau unbestimmt")

    # Fit 1: scipy curve_fit.
    from scipy.optimize import curve_fit
    p0 = [max(float(y.max()), 1.0), max(float(np.median(d)), 1.0)]
    try:
        (wmax_c, tau_c), _ = curve_fit(
            _plateau, d, y, p0=p0,
            bounds=([1.0, 1.0], [100.0, 1000.0]), maxfev=10000,
        )
    except Exception as exc:  # noqa: BLE001
        return WirkungFit(0.0, 0.0, 0.0, n, 0.0, 0.0, False,
                          f"curve_fit fehlgeschlagen: {type(exc).__name__}")

    # Fit 2 (unabhaengiger Doppel-Check): Grid-Suche.
    best: tuple[float, float, float] | None = None
    for wmax_g in np.arange(5.0, 61.0, 2.5):
        for tau_g in np.arange(5.0, 121.0, 5.0):
            sse = float(np.sum((_plateau(d, wmax_g, tau_g) - y) ** 2))
            if best is None or sse < best[0]:
                best = (sse, float(wmax_g), float(tau_g))
    assert best is not None
    _, wmax_g, _tau_g = best

    if abs(wmax_c - wmax_g) > konsens_toleranz * max(wmax_c, wmax_g):
        return WirkungFit(round(float(wmax_c), 1), round(float(wmax_c / tau_c), 3),
                          round(float(tau_c), 1), n, 0.0, 0.0, False,
                          f"Fit instabil: curve_fit wmax={wmax_c:.0f} vs "
                          f"grid {wmax_g:.0f}")

    pred = _plateau(d, wmax_c, tau_c)
    mse = float(np.mean((pred - y) ** 2))
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    r0 = float(wmax_c / tau_c)

    angenommen = (r2 >= min_r2) and (mse <= max_mse_pp2)
    grund = (
        "ok"
        if angenommen
        else f"Quality-Gate verfehlt: r2={r2:.2f} (>= {min_r2}), "
             f"mse={mse:.1f} (<= {max_mse_pp2})"
    )
    return WirkungFit(
        round(float(wmax_c), 1), round(r0, 3), round(float(tau_c), 1),
        n, round(r2, 3), round(mse, 1), angenommen, grund,
    )
