"""T-0049: Datengetriebene Schwellen-Vorschlaege aus historischer Feuchte.

Nimmt die Feuchte-Messwerte einer Zone ueber ein Fenster (Default 30 Tage)
und schlaegt daraus `feuchte_schwelle_min` und `feuchte_schwelle_max` vor.

**Logik**: unteres Quantil + Puffer (Boden soll selten darunter fallen),
oberes Quantil - Puffer (Sattigungs-Schwelle knapp unter den hoechsten
Messwerten). Bewusst simpel, rein informativ — der User trifft die
Entscheidung selbst.

**Kein Auto-Apply**: Vorschlag wird nur in der UI angezeigt, nicht in
`config/default.yaml` geschrieben.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from bewaesserung.modelle import ZonenKonfig
    from bewaesserung.speicher import Speicher


# Default-Parameter. Fenster 30 Tage deckt typische Wetter-Schwankungen ab
# ohne zu viel saisonale Drift (Sommer/Winter haben sehr unterschiedliche
# Verdunstungs-Raten). Puffer 5 %-Punkte gibt User Sicherheitsabstand.
FENSTER_TAGE_DEFAULT = 30
PERZENTIL_UNTEN = 10     # ~10 % der Messwerte liegen darunter
PERZENTIL_OBEN = 90      # ~10 % der Messwerte liegen darueber
PUFFER_PROZENTPUNKTE = 5
MIN_MESSUNGEN = 200      # weniger als das → kein Vorschlag (Rauschen)


class SchwellenVorschlag(BaseModel):
    """Vorschlag pro Zone inklusive Basis-Info fuer UI-Anzeige."""
    zone_id: str
    min_aktuell: float
    # T-0491 (13.08.2026): darf None sein, seit zitrus und mandevilla_maxi
    # ihre Nass-Warnung abgegeben haben. Ohne diese Zeile warf der Endpoint
    # `/api/schwellen-vorschlag` einen ValidationError und lieferte 500 --
    # gefunden bei der Browser-Verifikation von T-0496, NICHT beim
    # Isomorphie-Check: der hatte auf das Feld `feuchte_schwelle_max`
    # gegrept, dieser Aufrufer geht aber ueber `effektiv_schwelle_max()`.
    # Merksatz: bei einem Feld, das optional wird, auch die ACCESSOR-Funktion
    # greppen, nicht nur den Feldnamen.
    max_aktuell: float | None
    min_vorschlag: float | None
    max_vorschlag: float | None
    basis: str     # "berechnet" | "zu_wenig_daten"
    n_messungen: int
    fenster_tage: int
    # T-0050a: Pflanzen-Optimum aus zone.optimum_feuchte_*. None wenn
    # nicht konfiguriert. Frontend rendert "(Pflanze: A-B %)" wenn vorhanden.
    optimum_min: float | None = None
    optimum_max: float | None = None
    # Wenn das Optimum den empirischen Vorschlag ueberschrieben hat,
    # steht hier "optimum_dominiert"; sonst "empirisch".
    quelle: str = "empirisch"


def _perzentil(werte: list[float], p: float) -> float:
    """Lineares Perzentil — nutzt keine numpy-Abhaengigkeit (wird in API-Pfad
    aufgerufen, und numpy ist zwar da, aber fuer diese triviale Berechnung
    ist reines Python klarer und schneller).
    """
    if not werte:
        raise ValueError("Leere Werte-Liste")
    sortiert = sorted(werte)
    if len(sortiert) == 1:
        return sortiert[0]
    pos = (p / 100.0) * (len(sortiert) - 1)
    unten = int(pos)
    oben = min(unten + 1, len(sortiert) - 1)
    gewicht = pos - unten
    return sortiert[unten] * (1 - gewicht) + sortiert[oben] * gewicht


def berechne_vorschlag(
    werte: list[float],
    min_aktuell: float,
    max_aktuell: float | None,   # T-0491: None = Zone ohne Nass-Obergrenze
    perzentil_unten: float = PERZENTIL_UNTEN,
    perzentil_oben: float = PERZENTIL_OBEN,
    puffer: float = PUFFER_PROZENTPUNKTE,
    min_messungen: int = MIN_MESSUNGEN,
    optimum_min: float | None = None,
    optimum_max: float | None = None,
) -> tuple[float | None, float | None, str, str]:
    """Rechnet (min_vorschlag, max_vorschlag, basis, quelle) fuer eine Messreihe.

    Vorschlag ist bewusst "clamped" auf (0, 100) und wird in Einserschritten
    gerundet — Feuchte-Schwellen in der Konfig sind typisch ganzzahlig.

    T-0050a: wenn `optimum_min`/`optimum_max` (Pflanzen-Range) gesetzt
    sind, wird der empirische Vorschlag weich dominiert:
      min_vorschlag = max(empirisch_min, optimum_min - 2)
      max_vorschlag = min(empirisch_max, optimum_max + 2)
    Effekt: Pflanze darf nicht unter ihr Optimum-Minimum (weniger 2 %-Puffer)
    fallen, auch wenn die empirische Historie niedriger war (d. h. der User
    hat bisher zu wenig gegossen). Der `quelle`-Rueckgabewert zeigt, ob das
    Optimum den empirischen Wert ueberschrieben hat.
    """
    if len(werte) < min_messungen:
        return None, None, "zu_wenig_daten", "empirisch"
    p_unten = _perzentil(werte, perzentil_unten)
    p_oben = _perzentil(werte, perzentil_oben)
    # Unten: Vorschlag etwas ueber dem unteren Perzentil, damit selten unterschritten.
    # Oben: Vorschlag etwas unter dem oberen Perzentil, damit nicht gleich "nass"-
    # getriggert wird.
    emp_min = max(0.0, min(100.0, round(p_unten + puffer)))
    emp_max = max(0.0, min(100.0, round(p_oben - puffer)))

    min_v = emp_min
    max_v = emp_max
    quelle = "empirisch"
    if optimum_min is not None:
        # Puffer unter dem Optimum-Minimum ist ok (kleine Toleranz), aber
        # nicht weiter runter.
        kombiniert_min = max(emp_min, round(optimum_min - 2))
        if kombiniert_min != min_v:
            quelle = "optimum_dominiert"
        min_v = kombiniert_min
    if optimum_max is not None:
        kombiniert_max = min(emp_max, round(optimum_max + 2))
        if kombiniert_max != max_v:
            quelle = "optimum_dominiert"
        max_v = kombiniert_max

    # Clamp auf gueltigen Bereich
    min_v = max(0.0, min(100.0, min_v))
    max_v = max(0.0, min(100.0, max_v))

    # Invariante: min_v < max_v. Wenn Spanne zu klein, als zu_wenig_daten markieren.
    if min_v >= max_v:
        return None, None, "zu_wenig_daten", "empirisch"
    return float(min_v), float(max_v), "berechnet", quelle


async def berechne_vorschlaege_fuer_alle(
    speicher: "Speicher",
    zonen: "list[ZonenKonfig]",
    jetzt: datetime | None = None,
    fenster_tage: int = FENSTER_TAGE_DEFAULT,
    ml_ausschluss_fenster: "list | None" = None,
) -> list[SchwellenVorschlag]:
    """Baut fuer jede Zone ein `SchwellenVorschlag`-Objekt — auch wenn
    zu wenig Daten da sind (dann `min_vorschlag=None`, `basis='zu_wenig_daten'`),
    damit die UI konsistent pro Zone rendern kann.

    T-0050b: Pflanzen-Optimum-Quelle wird hier zonenweise entschieden:
    1. Wenn `zone.optimum_feuchte_*` gesetzt (User-Override via Config)
       → nutze diese Werte. Hat Vorrang, weil User-Wissen spezifischer
       als FYTA-Default.
    2. Sonst: falls in `plant_optimum`-Tabelle Wert aus FYTA-Cache
       (T-0050b-Job) vorhanden → nutze diesen.
    3. Sonst: kein Optimum, rein empirisch.

    `ml_ausschluss_fenster`: Liste von `MlAusschlussFenster` (aus
    `config.ml_ausschluss_fenster`). Zeitraeume darin werden pro Zone
    aus der Feuchte-Stichprobe entfernt. Fixt Perzentil-Verzerrung durch
    Sensor-Umzuege oder Initial-Setup (z. B. bambuswald_yogaraum hatte
    94 x 0.0 % am 6./7.4., bevor Sensor richtig eingeschlemmt war).
    """
    jetzt = jetzt or datetime.now()
    von = jetzt - timedelta(days=fenster_tage)
    # Einmalig den gesamten Plant-Optimum-Cache holen, damit wir nicht
    # pro Zone einen DB-Call machen.
    cache = await speicher.hole_plant_optima()
    # Ausschluss-Fenster nach Zone gruppieren (fuer O(1)-Lookup).
    # T-0386: 3-Tupel (von, bis, geraet_id) -- hole_feuchte_werte schneidet bei
    # gesetzter geraet_id nur die Rows DIESES Sensors weg (Gardena bleibt im
    # Training). Wartungs-Fenster sind zone-weit -> geraet_id=None.
    ausschluss_pro_zone: dict[
        str, list[tuple[datetime, datetime, str | None]]
    ] = {}
    if ml_ausschluss_fenster:
        for f in ml_ausschluss_fenster:
            ausschluss_pro_zone.setdefault(f.zone_id, []).append(
                (f.von, f.bis, f.geraet_id),
            )
    # T-0228 Stufe 2c: dynamische Wartungs-Fenster aus DB ebenfalls
    # in die Ausschluss-Map einfliessen lassen. Offene Fenster
    # (bis_am IS NULL) werden mit Cap "jetzt+1d" begrenzt.
    try:
        offene = await speicher.hole_wartungs_fenster(nur_offen=True)
    except Exception:
        offene = []
    cap = jetzt + timedelta(days=1)
    for w in offene:
        zid = w["zone_id"]
        try:
            v = datetime.fromisoformat(w["von_am"])
        except (TypeError, ValueError):
            continue
        ausschluss_pro_zone.setdefault(zid, []).append((v, cap, None))
    ergebnisse: list[SchwellenVorschlag] = []
    # T-0135 (H-4 Stufe 1b): Schwellen + Optimum Regime-aware aufloesen.
    # Magerwiese-Sommer-Trockenphase soll z. B. einen Vorschlag bekommen,
    # der nicht das Bambus-Optimum als Referenz nimmt.
    from bewaesserung.modelle import (
        effektiv_optimum_min, effektiv_optimum_max,
        effektiv_schwelle_min, effektiv_schwelle_max,
    )
    for zone in zonen:
        # T-0561: bei Zonen mit `aggregat_lead_geraet` nur dessen Werte.
        # Der Vorschlag wird gegen `effektiv_schwelle_min` gestellt, und die
        # gilt fuer den Lead -- ein Perzentil ueber gemischte Sensoren
        # vergleicht zwei Skalen. An der Live-DB gemessen (30 Tage):
        # waldblumenhain p10 gemischt 12,0 gegen 40,0 nur-Lead, hecke 30
        # gegen 40. Uebernommen haette der User die Schwelle um rund 15 pp
        # gesenkt -- auf Sand die teure Fehlerrichtung
        # ([[feedback_sand_lieber_zu_viel_giessen]]).
        lead = None
        hole_lead = getattr(speicher, "aggregat_lead", None)
        if callable(hole_lead):
            lead = hole_lead(zone.zone_id)
        werte = await speicher.hole_feuchte_werte(
            zone.zone_id, von, jetzt,
            ausschluss_fenster=ausschluss_pro_zone.get(zone.zone_id),
            geraet_id=lead,
        )
        eff_opt_min = effektiv_optimum_min(zone, jetzt)
        eff_opt_max = effektiv_optimum_max(zone, jetzt)
        eff_schwell_min = effektiv_schwelle_min(zone, jetzt)
        eff_schwell_max = effektiv_schwelle_max(zone, jetzt)
        # Optimum aus Regime-Override > Zone-Konfig > FYTA-Cache.
        if eff_opt_min is not None:
            opt_min = eff_opt_min
            opt_max = eff_opt_max
        elif zone.zone_id in cache:
            c = cache[zone.zone_id]
            opt_min = c["feuchte_min"]
            opt_max = c["feuchte_max"]
        else:
            opt_min = None
            opt_max = None
        min_v, max_v, basis, quelle = berechne_vorschlag(
            werte,
            min_aktuell=eff_schwell_min,
            max_aktuell=eff_schwell_max,
            optimum_min=opt_min,
            optimum_max=opt_max,
        )
        ergebnisse.append(SchwellenVorschlag(
            zone_id=zone.zone_id,
            min_aktuell=eff_schwell_min,
            max_aktuell=eff_schwell_max,
            min_vorschlag=min_v,
            max_vorschlag=max_v,
            basis=basis,
            n_messungen=len(werte),
            fenster_tage=fenster_tage,
            optimum_min=opt_min,
            optimum_max=opt_max,
            quelle=quelle,
        ))
    return ergebnisse
