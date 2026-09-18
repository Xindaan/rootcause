"""T-0353 (re-scoped): State-Space-Shadow-Forecaster fuer die Bodenfeuchte.

Zielgleichung (Stunden-Integration, Forward-Trajektorie):

    f(t+1h) = clip( wp + (f(t) - wp) * exp(-k_eff(t))   # Decay wie physik_trocknung
                    + regen_zufluss(t)                   # Regen als exogener Input
                    + puls_zufluss(t),                   # Giessen als exogener Input
                    wp, obergrenze )

Unterschied zu `physik_trocknung.prognose_physik`: Regen und Giess-Pulse
sind bekannte INPUTS (kein Regressions-Target). Der Giess-Puls wirkt
sensor-verzoegert (~1h Einsickern bis Sensor-Tiefe, Memory
domain_giesswirkung_sensor_verzoegerung) als lineare Ramp ueber
`ramp_stunden` nach Dosen-Ende. Die Puls-Magnitude ist NICHT dauer-linear
ohne Deckel (Memory domain_wirkung_fit_nicht_dauer_dominiert):
Plateau-Modell `wmax * (1 - exp(-dauer/tau))` mit `tau = wmax / r0`
aus der Zonen-Konfig, Fallback `delta_pp_pro_minute * dauer` gedeckelt
auf `wirkung_max_pp`.

Shadow-only: Das Modul hat KEINE Verbindung zu `entscheidung.py`.
Verbraucher ist ausschliesslich die Audit-Augmentation
(`augmentiere_statespace_prognose`), die additive Spalten in
`empfehlungs_audit` fuellt, plus das Offline-Backtest-Skript
`docs/analyse/t0353_statespace_backtest.py` (Single Source: dieselben
Funktionen).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

# Fallback-Deckel fuer die Puls-Magnitude, wenn die Zone kein
# `wirkung_max_pp` konfiguriert hat (nur `delta_pp_pro_minute`).
# Konservativ: mehr als ~30 pp hebt eine Einzeldose real nicht
# (beobachtete Maxima: bambus +25-30 pp / 90 min, hecke +15 pp / 46 min).
PULS_MAX_PP_FALLBACK = 30.0


@dataclass(frozen=True)
class GiessPuls:
    """Ein abgeschlossener Bewaesserungs-Lauf als exogener Input.

    `t_ende` = Zeitstempel des SCHLIESSEN-Events (Ende des Wasserflusses),
    `dauer_s` = Netto-Wasserzeit in Sekunden.
    """
    t_ende: datetime
    dauer_s: float


@dataclass(frozen=True)
class StateSpaceParams:
    """Zonen-Parameter fuer die Forward-Trajektorie."""
    welkepunkt: float
    k_basis_pro_h: float
    et0_basis_mm_pro_h: float
    # Puls-Magnitude: Plateau-Modell wenn wirkung_max_pp + wirkungsrate_initial
    # gesetzt; sonst delta_pp_pro_minute linear mit Deckel.
    wirkung_max_pp: float | None = None
    wirkungsrate_initial: float | None = None
    delta_pp_pro_minute: float | None = None
    # Regen-Zufluss in pp pro mm (Default = Engine-Konstante
    # REGEN_FEUCHTE_FAKTOR aus entscheidung.py, KEIN neuer Fit).
    regen_faktor_pp_pro_mm: float = 4.0
    # Sensor-Verzoegerung: Puls-Wirkung materialisiert linear ueber
    # [t_ende, t_ende + ramp_stunden].
    ramp_stunden: float = 1.5
    # Obergrenze der Trajektorie (Feldkapazitaet wenn kalibriert, sonst 100).
    obergrenze: float = 100.0


def puls_magnitude_pp(params: StateSpaceParams, dauer_s: float) -> float:
    """Erwarteter Sensor-Hub EINER Dose in pp.

    Plateau-Modell (nicht dauer-linear): wmax * (1 - exp(-d/tau)),
    tau = wmax / r0. Fallback linear * Deckel.
    Return 0.0 wenn keine Wirkungs-Parameter konfiguriert sind (dann
    degeneriert der Forecaster bewusst zur reinen Physik+Regen-Prognose,
    statt eine Wirkung zu raten).
    """
    dauer_min = max(0.0, dauer_s) / 60.0
    if dauer_min <= 0:
        return 0.0
    wmax = params.wirkung_max_pp
    r0 = params.wirkungsrate_initial
    if wmax is not None and wmax > 0 and r0 is not None and r0 > 0:
        tau = wmax / r0
        return wmax * (1.0 - math.exp(-dauer_min / tau))
    rate = params.delta_pp_pro_minute
    if rate is not None and rate > 0:
        deckel = wmax if (wmax is not None and wmax > 0) else PULS_MAX_PP_FALLBACK
        return min(rate * dauer_min, deckel)
    return 0.0


def _ramp_anteil(params: StateSpaceParams, puls: GiessPuls, t: datetime) -> float:
    """Anteil [0..1] der Puls-Wirkung, der bis Zeitpunkt `t` am Sensor
    angekommen ist (lineare Ramp ueber ramp_stunden nach Dosen-Ende)."""
    if params.ramp_stunden <= 0:
        return 1.0 if t >= puls.t_ende else 0.0
    delta_h = (t - puls.t_ende).total_seconds() / 3600.0
    if delta_h <= 0:
        return 0.0
    return min(1.0, delta_h / params.ramp_stunden)


def prognose_statespace(
    f_start: float | None,
    t_start: datetime,
    horizont_h: int,
    params: StateSpaceParams,
    et0_zukunft_pro_h: list[float],
    regen_zukunft_pro_h: list[float],
    pulse: list[GiessPuls],
) -> float | None:
    """Prognostizierte Bodenfeuchte `horizont_h` Stunden nach `t_start`.

    `pulse`: abgeschlossene Laeufe, deren Ramp-Fenster in die Trajektorie
    hineinreicht — sowohl Pulse VOR t_start (Rest-Wirkung, der bereits in
    f_start enthaltene Anteil wird abgezogen) als auch Pulse IM Fenster
    (Offline-Backtest; live sind zukuenftige Pulse unbekannt).

    `et0_zukunft_pro_h` / `regen_zukunft_pro_h`: stuendliche Werte ab
    t_start; zu kurze Listen werden mit et0_basis bzw. 0.0 aufgefuellt
    (identisch zum Verhalten von `prognose_physik`).

    Return None bei fehlendem f_start/welkepunkt-aequivalenten Zustand
    (Vertrag identisch zu `prognose_physik`).
    """
    if f_start is None:
        return None
    if not math.isfinite(f_start) or not math.isfinite(params.welkepunkt):
        return None
    if params.et0_basis_mm_pro_h <= 0:
        return None
    if horizont_h <= 0:
        return float(f_start)

    wp = float(params.welkepunkt)
    obergrenze = max(params.obergrenze, wp)
    # Untergrenze: normal der Welkepunkt; startet der Sensor schon
    # darunter, ist f_start selbst der Boden (kein weiterer Decay,
    # analog prognose_physik).
    untergrenze = min(float(f_start), wp)
    f = min(float(f_start), 100.0)

    # Pro Puls: Gesamt-Magnitude + bereits in f_start materialisierter
    # Anteil (nur der Rest fliesst in die Trajektorie).
    puls_rest: list[tuple[GiessPuls, float, float]] = []
    for p in pulse:
        mag = puls_magnitude_pp(params, p.dauer_s)
        if mag <= 0:
            continue
        schon = _ramp_anteil(params, p, t_start)
        if schon >= 1.0:
            continue  # Wirkung vollstaendig in f_start enthalten
        puls_rest.append((p, mag, schon))

    for stunde in range(horizont_h):
        t_von = t_start + timedelta(hours=stunde)
        t_bis = t_von + timedelta(hours=1)
        et0_h = (
            et0_zukunft_pro_h[stunde]
            if stunde < len(et0_zukunft_pro_h)
            else params.et0_basis_mm_pro_h
        )
        et0_h = max(0.0, float(et0_h))
        regen_h = (
            regen_zukunft_pro_h[stunde]
            if stunde < len(regen_zukunft_pro_h)
            else 0.0
        )
        regen_h = max(0.0, float(regen_h))

        # 1) Decay-Schritt — identische Formel wie physik_trocknung
        #    (Regressionstest: ohne Regen/Pulse == prognose_physik).
        #    Unterhalb des Welkepunkts kein weiterer Decay (Pulse/Regen
        #    koennen f aber wieder darueber heben).
        if f > wp:
            k_eff = params.k_basis_pro_h * (et0_h / params.et0_basis_mm_pro_h)
            f = wp + (f - wp) * math.exp(-k_eff)

        # 2) Regen-Zufluss (Engine-Semantik pp/mm, siehe Konfig-Default).
        f += params.regen_faktor_pp_pro_mm * regen_h

        # 3) Puls-Zufluss: Ramp-Zuwachs dieser Stunde.
        for p, mag, schon in puls_rest:
            anteil_bis = _ramp_anteil(params, p, t_bis)
            anteil_von = max(_ramp_anteil(params, p, t_von), schon)
            if anteil_bis > anteil_von:
                f += mag * (anteil_bis - anteil_von)

        f = max(untergrenze, min(f, obergrenze))

    return f
