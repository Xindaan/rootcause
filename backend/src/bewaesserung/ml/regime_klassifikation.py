"""T-0349: Regime-Klassifikation von Prognose-Fenstern (Single Source).

Klassifiziert das Fenster `[snap_ts - settling_h, snap_ts + horizont_h]`
eines Audit-Snapshots in genau ein Regime:

    ausgeschlossen_mlausschluss — Fenster ueberlappt ein ml_ausschluss-
                                  Fenster der Zone (Sensor unzuverlaessig
                                  -> Ist-Wert nicht bewertbar)
    ausgeschlossen_crossspray   — Lauf einer cross_spray-Quell-Zone im Fenster
                                  (physisch: JEDER ausloser, auch 'ignoriert')
    giess_recovery              — echter eigener Giesslauf im Fenster
    regen                       — > max_regen_mm Niederschlag im Fenster
    regen_unbekannt             — wetter_archiv deckt das Fenster nicht ab
                                  (ERA5-Lag) -> Regen nicht beurteilbar
    trocknung                   — Rest = reine Trocknung

Prioritaet in dieser Reihenfolge. BEWUSSTE Abweichungen vom T-0348-
Referenzskript `docs/analyse/t0348_regime_backtest.py` (Verifier-Review
01.07.): (1) `ausgeschlossen_*` VOR `giess_recovery` — in einem
Kalibrier-/Cross-Spray-Fenster ist der Ist-Wert unbrauchbar bzw. die
Wirkung nicht attribuierbar, ein Giesslauf macht ihn nicht bewertbar;
(2) ml_ausschluss prueft den Overlap des GESAMTEN Fensters (auch der
Ist-Wert bei ts+h kann im Fenster liegen), nicht nur den Snapshot, und
nutzt die ECHTEN von/bis aus der Konfig (Skript: bis + 14d-Heuristik);
(3) Cross-Spray gilt unbefristet solange `cross_spray_quell_zonen`
konfiguriert ist (das Feld ist die Wahrheit ueber die physische
Ueberlappung; das Skript band es ans Grass-Regime-Fenster). Die
n-Verschiebung gegen die T-0348-Baseline wird im Backtest ausgewiesen.

Verbraucher: EmpfehlungsAuditJob (Eval schreibt regime_6h/regime_24h),
/api/ml/physik-bias?regime=..., Offline-Backtest T-0353.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bewaesserung.modelle import (
    GesamtKonfig,
    KEINE_WASSER_AUSLOESER,
    VentilAktion,
)

REGIME_TROCKNUNG = "trocknung"
REGIME_GIESS_RECOVERY = "giess_recovery"
REGIME_REGEN = "regen"
REGIME_REGEN_UNBEKANNT = "regen_unbekannt"
REGIME_AUSGESCHLOSSEN_CROSSSPRAY = "ausgeschlossen_crossspray"
REGIME_AUSGESCHLOSSEN_MLAUSSCHLUSS = "ausgeschlossen_mlausschluss"

ALLE_REGIMES = (
    REGIME_TROCKNUNG,
    REGIME_GIESS_RECOVERY,
    REGIME_REGEN,
    REGIME_REGEN_UNBEKANNT,
    REGIME_AUSGESCHLOSSEN_CROSSSPRAY,
    REGIME_AUSGESCHLOSSEN_MLAUSSCHLUSS,
)

# Look-back vor dem Snapshot: ein Lauf kurz VOR dem Snapshot wirkt
# (Sensor-Verzoegerung + Einsickern) noch ins Prognose-Fenster hinein.
SETTLING_H_DEFAULT = 6
# Konsistent mit MlPhysikDiagnoseKonfig.max_regen_im_fenster_mm.
MAX_REGEN_MM_DEFAULT = 1.0


@dataclass
class RegimeKontext:
    """Vorab geladener Daten-Kontext EINER Zone fuer die Klassifikation.

    Alle Listen chronologisch aufsteigend. `laeufe_eigene` = SCHLIESSEN
    dauer>0 mit ausloser NICHT in KEINE_WASSER_AUSLOESER (echte
    Wassergabe). `laeufe_cross` = SCHLIESSEN dauer>0 der cross_spray-
    Quell-Zonen mit ALLEN ausloesern (auch 'ignoriert' — der Lauf war
    physisch, nur buchhalterisch ignoriert).
    """
    laeufe_eigene: list[datetime] = field(default_factory=list)
    laeufe_cross: list[datetime] = field(default_factory=list)
    ausschluss_fenster: list[tuple[datetime, datetime]] = field(
        default_factory=list,
    )
    # (zeitstempel, niederschlag_mm) des Zonen-Standorts.
    wetter_regen: list[tuple[datetime, float]] = field(default_factory=list)
    # Juengster wetter_archiv-Zeitstempel des Standorts (ERA5-Lag-Grenze).
    wetter_max_ts: datetime | None = None


def klassifiziere_regime(
    snap_ts: datetime,
    horizont_h: int,
    kontext: RegimeKontext,
    settling_h: int = SETTLING_H_DEFAULT,
    max_regen_mm: float = MAX_REGEN_MM_DEFAULT,
) -> str:
    """Regime des Fensters [snap_ts - settling_h, snap_ts + horizont_h]."""
    t_lo = snap_ts - timedelta(hours=settling_h)
    t_hi = snap_ts + timedelta(hours=horizont_h)
    # 1. ml_ausschluss-Fenster: Overlap mit dem GESAMTEN Fenster.
    if any(
        not (bis < t_lo or von > t_hi)
        for von, bis in kontext.ausschluss_fenster
    ):
        return REGIME_AUSGESCHLOSSEN_MLAUSSCHLUSS
    # 2. Cross-Spray (physisch, alle ausloser) — Wirkung nicht
    #    attribuierbar, auch wenn zusaetzlich selbst gegossen wurde.
    if any(t_lo <= ts <= t_hi for ts in kontext.laeufe_cross):
        return REGIME_AUSGESCHLOSSEN_CROSSSPRAY
    # 3. Eigener echter Giesslauf im Fenster.
    if any(t_lo <= ts <= t_hi for ts in kontext.laeufe_eigene):
        return REGIME_GIESS_RECOVERY
    # 4. Regen — nur beurteilbar, wenn das Archiv das Fenster abdeckt.
    if kontext.wetter_max_ts is None or kontext.wetter_max_ts < t_hi:
        return REGIME_REGEN_UNBEKANNT
    regen = sum(
        mm for ts, mm in kontext.wetter_regen if t_lo <= ts <= t_hi
    )
    if regen > max_regen_mm:
        return REGIME_REGEN
    # 5. Rest = reine Trocknung.
    return REGIME_TROCKNUNG


def cross_spray_quellen(konfig: GesamtKonfig, zone_id: str) -> list[str]:
    """Quell-Zonen, deren Laeufe diese Zone physisch mit-treffen."""
    zone = next(
        (z for z in konfig.zonen if z.zone_id == zone_id), None,
    )
    if zone is None:
        return []
    return list(getattr(zone, "cross_spray_quell_zonen", []) or [])


def ausschluss_fenster_fuer_zone(
    konfig: GesamtKonfig, zone_id: str,
) -> list[tuple[datetime, datetime]]:
    """Echte (von, bis) der ml_ausschluss-Fenster einer Zone.

    Bewusst OHNE geraet_id-Filter: fuer die Regime-Frage zaehlt, ob die
    Zone im Fenster als unzuverlaessig markiert war (Kalibrierung, Umzug,
    Fremd-Nutzung) — egal welcher Sensor betroffen war.
    """
    out: list[tuple[datetime, datetime]] = []
    for f in getattr(konfig, "ml_ausschluss_fenster", []) or []:
        if f.zone_id == zone_id:
            out.append((f.von, f.bis))
    return out


async def lade_regime_kontext(
    speicher,
    konfig: GesamtKonfig,
    zone_id: str,
    von: datetime,
    bis: datetime,
) -> RegimeKontext:
    """Laedt den Klassifikations-Kontext einer Zone aus dem Speicher.

    `von`/`bis` muessen das gesamte zu klassifizierende Fenster
    (inkl. Settling-Vorlauf) abdecken. Fehlende Wetter-Daten sind kein
    Fehler — die Klassifikation liefert dann `regen_unbekannt`.
    """
    quellen = cross_spray_quellen(konfig, zone_id)
    laeufe_eigene: list[datetime] = []
    laeufe_cross: list[datetime] = []
    events = await speicher.hole_alle_ventil_ereignisse(von, bis)
    for e in events:
        if e.aktion != VentilAktion.SCHLIESSEN or (e.dauer_sekunden or 0) <= 0:
            continue
        if e.zone_id == zone_id:
            if e.ausloser not in KEINE_WASSER_AUSLOESER:
                laeufe_eigene.append(e.zeitstempel)
        elif e.zone_id in quellen:
            laeufe_cross.append(e.zeitstempel)

    # Standort der Zone -> Wetter-Archiv.
    standort_id: str | None = None
    for st in konfig.standorte or []:
        if zone_id in (st.zonen or []):
            standort_id = st.wetter_standort
            break
    wetter_regen: list[tuple[datetime, float]] = []
    wetter_max_ts: datetime | None = None
    if standort_id:
        archiv = await speicher.hole_wetter_archiv(
            standort_id, von=von, bis=bis,
        )
        wetter_regen = [
            (w.zeitstempel, float(w.niederschlag_mm or 0.0)) for w in archiv
        ]
        # ERA5-Lag-Grenze: juengster Archiv-Eintrag GLOBAL fuer den
        # Standort (nicht nur im Fenster) — sonst wuerde ein altes
        # Fenster faelschlich als unbekannt gelten.
        wetter_max_ts = await speicher.juengster_archiv_zeitstempel(standort_id)

    return RegimeKontext(
        laeufe_eigene=sorted(laeufe_eigene),
        laeufe_cross=sorted(laeufe_cross),
        ausschluss_fenster=ausschluss_fenster_fuer_zone(konfig, zone_id),
        wetter_regen=wetter_regen,
        wetter_max_ts=wetter_max_ts,
    )
