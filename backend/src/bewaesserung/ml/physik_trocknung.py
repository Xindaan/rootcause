"""Hybrid Stufe 1: Physikalisches Trocknungs-Modul (read-only-Diagnose).

Modell
------
Boden-Trocknung als exponentieller Decay zum Welkepunkt:

    f(t) = wp + (f0 - wp) * exp(-k_eff * t)

mit `k_eff(t) = k_basis * ET0_stunde / ET0_basis`. Die ET0-Skalierung
sorgt dafuer, dass kuehle/feuchte Tage langsamer trocknen als heisse
Trocken-Tage (ET0 als bestes Proxy fuer reale Verdunstung; ET0-Quelle
ist `WetterVorhersage.et0_naechste_stunden(...)`, FAO-56-Penman-Monteith).

Stunden-weise Integration: pro Stunde wird `k_eff` aus der jeweiligen
Stunde der ET0-Vorhersage berechnet und der Decay angewendet.

Edge-Cases
----------
- `welkepunkt is None`: keine Prognose moeglich (Welkepunkt ist
  Asymptote des Decays). Aufrufer kriegt `None`.
- `f_start <= welkepunkt`: kein weiterer Decay unter Welkepunkt
  (Modell wird unphysikalisch negativ -> auf Startwert klammern).
- `et0_zukunft` kuerzer als `horizont_h`: mit `et0_basis` auffuellen.

Verwendung
----------
Das Modul ist **read-only**. Es liefert eine zusaetzliche Prognose
neben der ML/heuristischen Prognose im `GiessEmpfehlung`-Response;
die Entscheidungslogik bleibt unveraendert.
"""

from __future__ import annotations

import math


def prognose_physik(
    f_start: float | None,
    welkepunkt: float | None,
    k_basis_pro_h: float,
    et0_basis_mm_pro_h: float,
    et0_zukunft_pro_h: list[float],
    horizont_h: int,
) -> float | None:
    """Liefert die prognostizierte Bodenfeuchte nach `horizont_h` Stunden.

    `f_start` und `welkepunkt` sind in derselben Skala (Sensor-Index
    0-100). `k_basis_pro_h` ist die Trocknungs-Konstante bei Basis-ET0.
    `et0_basis_mm_pro_h` ist die ET0-Norm, gegen die k_eff skaliert.
    `et0_zukunft_pro_h` ist die Liste der stuendlichen ET0-Werte fuer
    die naechsten N Stunden (mm/h), aus `WetterVorhersage.stunden[i].et0_mm`.

    Return None nur bei fehlendem Welkepunkt oder fehlendem f_start.
    """
    if f_start is None or welkepunkt is None:
        return None
    if not math.isfinite(f_start) or not math.isfinite(welkepunkt):
        return None
    if horizont_h <= 0:
        return float(f_start)
    if et0_basis_mm_pro_h <= 0:
        # Robustheit: ohne Basis-ET0 koennen wir nicht skalieren.
        return None
    # Wenn der Sensor schon unter Welkepunkt ist, ist das Modell nicht
    # mehr aussagekraeftig (Asymptote unterschritten). Stabilen Wert
    # zurueckgeben: f_start selbst (keine weitere Trocknung).
    if f_start <= welkepunkt:
        return float(f_start)
    f = float(f_start)
    wp = float(welkepunkt)
    for stunde in range(horizont_h):
        et0_h = (
            et0_zukunft_pro_h[stunde]
            if stunde < len(et0_zukunft_pro_h)
            else et0_basis_mm_pro_h
        )
        # Defensive: negative ET0 ist unphysikalisch, clip auf 0.
        et0_h = max(0.0, float(et0_h))
        k_eff = k_basis_pro_h * (et0_h / et0_basis_mm_pro_h)
        # Stunden-Schritt der ODE-Loesung: f_neu = wp + (f_alt - wp) * exp(-k_eff * 1h).
        f = wp + (f - wp) * math.exp(-k_eff)
        if f <= wp:
            # Unter Welkepunkt nicht weiter "fallen" lassen.
            f = wp
            break
    return f


def tage_bis_zielfeuchte_physik(
    f_start: float | None,
    ziel_feuchte: float | None,
    welkepunkt: float | None,
    k_basis_pro_h: float,
    et0_basis_mm_pro_h: float,
    et0_zukunft_pro_h: list[float],
    max_tage: float = 30.0,
) -> float | None:
    """T-0279 Phase 2: Tage bis die Physik-Trocknungs-Prognose eine
    Ziel-Feuchte-Linie erreicht (Default-Verwendung: optimum_min).

    Robuste Reserve-Quelle fuer den proaktiven Bewaesserungs-Trigger --
    im Gegensatz zur ML-24h-Prognose (Mean-Reversion-Halluzination,
    T-0278) ist der exponentielle Physik-Decay monoton + stabil.

    WICHTIG (T-0279 Phase 2, Metrik-Redesign): Die Bezugslinie ist
    `ziel_feuchte`, NICHT der Welkepunkt. Der Welkepunkt ist die
    Asymptote des Decays -- "Tage bis Welkepunkt" ist mathematisch
    unbrauchbar als Trigger-Metrik: exakt-asymptotisch dauert es
    quasi-unendlich, linear-extrapoliert ist es wegen exp-Selbst-
    aehnlichkeit konstant (unabhaengig von der Feuchte). Erst eine
    Linie OBERHALB des Welkepunkts (z. B. optimum_min, die Komfort-
    Unterkante) liefert ein feuchte-abhaengiges, kalibrierbares Signal.
    `welkepunkt` bleibt noetig: er ist die Decay-Asymptote der ODE.

    `max_tage`: Such-Obergrenze. Wenn die Prognose innerhalb dieser
    Zeit das Ziel nicht erreicht, Return `max_tage` (= "weit weg, kein
    proaktiver Bedarf").

    Return None wenn: f_start/ziel/welkepunkt fehlen, ET0/k_basis
    nicht positiv, oder `ziel_feuchte <= welkepunkt` (degeneriert:
    Ziel-Linie auf/unter der Asymptote, per Decay nie erreichbar ->
    proaktiver Trigger inaktiv, Safe-Default).

    Stunden-weise Integration, identisch zu `prognose_physik`
    (ET0-Skalierung, et0_zukunft mit et0_basis aufgefuellt).
    """
    if f_start is None or welkepunkt is None or ziel_feuchte is None:
        return None
    if not (
        math.isfinite(f_start)
        and math.isfinite(welkepunkt)
        and math.isfinite(ziel_feuchte)
    ):
        return None
    if et0_basis_mm_pro_h <= 0 or k_basis_pro_h <= 0:
        return None
    if ziel_feuchte <= welkepunkt:
        # Degeneriert: Ziel auf/unter der Asymptote -> per Decay nie
        # erreichbar. Trigger inaktiv (Safe-Default).
        return None
    if f_start <= ziel_feuchte:
        return 0.0
    f = float(f_start)
    wp = float(welkepunkt)
    ziel = float(ziel_feuchte)
    max_stunden = int(max_tage * 24)
    for stunde in range(max_stunden):
        et0_h = (
            et0_zukunft_pro_h[stunde]
            if stunde < len(et0_zukunft_pro_h)
            else et0_basis_mm_pro_h
        )
        et0_h = max(0.0, float(et0_h))
        k_eff = k_basis_pro_h * (et0_h / et0_basis_mm_pro_h)
        f = wp + (f - wp) * math.exp(-k_eff)
        if f <= ziel:
            return round((stunde + 1) / 24.0, 2)
    return round(max_tage, 2)


def fitte_k_basis_phase(
    sensor_zeitreihe: list[tuple[float, float]],
    welkepunkt: float,
    et0_mittel_mm_pro_h: float,
    et0_basis_mm_pro_h: float,
    k_kandidaten: list[float] | None = None,
) -> tuple[float, float] | None:
    """Fittet `k_basis` per Grid-Search fuer EINE Trockenphase.

    `sensor_zeitreihe`: Liste `(t_stunden_seit_phase_start, feuchte)`,
    chronologisch aufsteigend. Erster Eintrag ist der Phase-Start
    (t=0, f0). `welkepunkt` ist der unter Decay erlaubte Mindestwert.
    `et0_mittel_mm_pro_h` ist die mittlere ET0 dieser Phase. Wenn der
    Mittelwert ≠ Basis-ET0 ist, wird der Fit gegen
    `k_eff = k_basis * et0_mittel / et0_basis` rechnen.

    Return `(k_basis, mae_pp)` oder None bei zu wenigen Daten/
    Welkepunkt-Unterschreitung am Startpunkt.

    Grid: Default 10 Kandidaten 0.005..0.05 / h (= tau 20-200 h).
    """
    if len(sensor_zeitreihe) < 3:
        return None
    t0, f0 = sensor_zeitreihe[0]
    if f0 <= welkepunkt:
        return None
    if et0_basis_mm_pro_h <= 0:
        return None
    if k_kandidaten is None:
        # 10 Werte logarithmisch zwischen 0.005 und 0.05.
        k_kandidaten = [
            0.005, 0.0075, 0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05,
        ]
    skala = et0_mittel_mm_pro_h / et0_basis_mm_pro_h
    bestes_k: float | None = None
    bestes_mae: float | None = None
    for k_basis in k_kandidaten:
        k_eff = k_basis * skala
        if k_eff <= 0:
            continue
        # Modell-Prognosen an den t-Punkten.
        fehler = []
        for t_h, f_ist in sensor_zeitreihe[1:]:
            dt = t_h - t0
            if dt <= 0:
                continue
            f_mod = welkepunkt + (f0 - welkepunkt) * math.exp(-k_eff * dt)
            fehler.append(abs(f_mod - f_ist))
        if not fehler:
            continue
        mae = sum(fehler) / len(fehler)
        if bestes_mae is None or mae < bestes_mae:
            bestes_mae = mae
            bestes_k = k_basis
    if bestes_k is None or bestes_mae is None:
        return None
    return bestes_k, bestes_mae


def fitte_k_und_welkepunkt_phase(
    sensor_zeitreihe: list[tuple[float, float]],
    et0_mittel_mm_pro_h: float,
    et0_basis_mm_pro_h: float,
    wp_hinweis: float | None = None,
    k_kandidaten: list[float] | None = None,
) -> tuple[float, float, float] | None:
    """T-0400 (b): fittet `k_basis` UND die Asymptote (Welkepunkt) gemeinsam
    fuer EINE Trockenphase EINES Geraets.

    Motivation: bei Multi-Sensor-Zonen liegen Gardena-Index und FYTA-
    `soil_moisture` auf verschiedenen Skalen (T-0410: NICHT als Bereichs-
    Unterschied bezifferbar -- die frueher hier genannten "FYTA 0-65" sind
    widerlegt, real bis 100). Ein gemeinsamer Welkepunkt waere fuer
    das Nicht-Referenz-Geraet die falsche Asymptote. Statt die Skalen zu mischen
    (alte Bug-Klasse) bekommt jedes Geraet seinen eigenen, aus seinen Daten
    gefitteten Welkepunkt. `k` ist eine Rate (1/h) und damit ueber Geraete
    hinweg vergleichbar/medianbar -- die Asymptote nicht, die ist skalen-lokal.

    `wp_hinweis` (der konfigurierte Zonen-Welkepunkt) wird als zusaetzlicher
    Kandidat aufgenommen: fuer das Referenz-Skala-Geraet trifft er die wahre
    Asymptote exakt; Nicht-Referenz-Geraete waehlen einen daten-getriebenen Wert.

    Return `(k_basis, welkepunkt, mae_pp)` oder None (zu wenige Punkte, keine
    Trocknung, kein gueltiger Fit).
    """
    if len(sensor_zeitreihe) < 3:
        return None
    if et0_basis_mm_pro_h <= 0:
        return None
    t0, f0 = sensor_zeitreihe[0]
    feuchten = [f for _, f in sensor_zeitreihe]
    f_min = min(feuchten)
    # Ohne Gefaelle (flach/steigend) ist kein Decay-Fit moeglich.
    if f0 <= f_min:
        return None
    if k_kandidaten is None:
        k_kandidaten = [
            0.005, 0.0075, 0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05,
        ]
    # Asymptote wird von oben approached -> Welkepunkt < f_min. Grid bis knapp
    # unter das Phasen-Minimum, plus den konfigurierten Hinweis als Kandidat.
    obergrenze = f_min * 0.98
    wp_kandidaten = [obergrenze * i / 9.0 for i in range(10)]
    if wp_hinweis is not None and 0.0 <= wp_hinweis < f0:
        wp_kandidaten.append(float(wp_hinweis))
    skala = et0_mittel_mm_pro_h / et0_basis_mm_pro_h
    bestes: tuple[float, float, float] | None = None  # (mae, k, wp)
    for wp in wp_kandidaten:
        if f0 <= wp:
            continue
        for k_basis in k_kandidaten:
            k_eff = k_basis * skala
            if k_eff <= 0:
                continue
            fehler = []
            for t_h, f_ist in sensor_zeitreihe[1:]:
                dt = t_h - t0
                if dt <= 0:
                    continue
                f_mod = wp + (f0 - wp) * math.exp(-k_eff * dt)
                fehler.append(abs(f_mod - f_ist))
            if not fehler:
                continue
            mae = sum(fehler) / len(fehler)
            if bestes is None or mae < bestes[0]:
                bestes = (mae, k_basis, wp)
    if bestes is None:
        return None
    return bestes[1], bestes[2], bestes[0]
