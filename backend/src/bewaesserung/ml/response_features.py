"""T-0065: Event-basierte Features fuer das Bewaesserungs-Response-Modell.

Pro echtem SCHLIESSEN-Event mit dauer>0 wird eine Row gebaut:
- `f_vor`, `f_vor_gradient_3h`, `f_vor_gradient_24h` aus Sensor-Messungen
- `dauer_s`, `liter_pro_sekunde` aus dem Event
- Wetter-Kontext (et0_nach_6h, vpd_mittel, niederschlag_nach_24h,
  temperatur_mittel) aus leakage-sicheren Forecasts
- Jahreszeit/Tageszeit als Sin/Cos
- Label: `delta_6h_ist`, `delta_12h_ist`, `delta_24h_ist` aus
  Sensor-Messungen nach t_ende

Filter (muss isomorph in Training + Drift-Evaluation gelten):
- `aktion == SCHLIESSEN`
- `dauer_sekunden > 0`
- `_ist_trainingsfaehig(...)`: UNBEKANNT/IGNORIERT/FREMDWASSER immer raus.
  AUTOMATIK seit T-0492 KONDITIONAL -- drin ab `zone.auto_loop_scharf_seit`,
  davor lief dieselbe Zone im Shadow und loggte `automatik` ohne Wasser.
  (Der Satz "da aktuell Shadow -> komplett raus" stand hier bis 05.08.2026
  und war seit dem Go-Live am 26.06. falsch.)
- `ventil_id not in {'', 'manuell', None}` (Schlauch-Events sind variable
  Rate, nicht vergleichbar)
- nicht innerhalb `ml_ausschluss_fenster`
- kein ueberlappendes SCHLIESSEN-Event im Fenster
  [t_start, t_ende + 24h] (verhindert Response-Vermischung)

Bambuswald/Yogaraum: beide Zonen teilen Kanal 2. Ein einzelnes Event auf
Kanal 2 erzeugt ZWEI Rows (eine pro Zone) mit dem jeweiligen Sensor-Signal
als f_vor. `shared_valve=True` als Feature.

Das Modul nutzt stdlib `logging` — in Regression-Tests `caplog.set_level(INFO)`
setzen (siehe fehlerpattern_stdlib_structlog_mix.md).
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bewaesserung.modelle import (
    Ausloser,
    GesamtKonfig,
    SensorMessung,
    VentilAktion,
    VentilEreignis,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher

# Single Source of Truth fuer "ist das eine AquaBloom-Pump-Zone" — dieselbe
# Praedikat-Funktion, die der Konversions-Job nutzt. Eine zweite Wahrheit
# hier wuerde exakt die Drift erzeugen, die T-0482 beseitigt.
from bewaesserung.aquabloom_job import (
    _ist_konfiguriert as _ist_aquabloom_konfiguriert,
)

try:
    import pandas as pd
except ImportError:
    raise ImportError(
        "ML-Abhaengigkeiten fehlen. Installiere mit: pip install -e '.[ml]'"
    )

logger = logging.getLogger(__name__)

# Toleranz fuer die f_vor-Suche (rueckwaerts ab t_start).
# 75 min deckt den typischen Gardena-Smart-Sensor-II-Cadence von ~1 Messung/h
# mit 8-120 min Variation (siehe Memory gardena_dhs_endpoint.md Sampling).
# Vorher 30 min war zu eng: Puls 23.04. 10:33 → Sensor 09:33 (60 min davor)
# fiel raus, obwohl physikalisch perfekter f_vor-Wert.
F_VOR_TOLERANZ_MIN = 75
# Kleines Vorwaerts-Fenster fuer f_vor, um Messungen knapp NACH t_start als
# f_vor zu nutzen (z. B. 10:33:38 Puls-Start → 10:33:54 Messung ist 16 s
# spaeter, aber Wasser hat sich in dieser Zeit noch nicht ausgewirkt).
# Wichtig: muss deutlich kleiner als ein Puls dauern, sonst kontaminiert.
F_VOR_VORWAERTS_MIN = 5
# Toleranz fuer die f_nach_Xh-Suche (beide Richtungen um t_ende+Xh).
F_NACH_TOLERANZ_MIN = 90

# Label-Horizonte in Stunden.
LABEL_HORIZONTE = (6, 12, 24)

# Ausloser, die stumm ausgefiltert werden (kein Training, kein Drift-Log).
# - AUTOMATIK: aktuell kein echter Wasserfluss (Shadow-Pfad, ventilsteuerung
#   inaktiv). Wenn T-0021 scharf ist, muss dieser Filter konditional werden.
# - UNBEKANNT/IGNORIERT: Sensor-Heuristik ohne Wasser (siehe Ausloser-Enum-
#   Kommentar). IGNORIERT wurde vom User explizit als Phantom markiert.
# - FREMDWASSER (T-0453): der Feuchte-Sprung ist echt, stammt aber aus dem
#   Kanal einer NACHBAR-Zone (Cross-Spray). Als Response auf die eigene
#   Dosis gelesen waere er ein frei erfundener Wirkungspunkt -- die Dauer
#   ist die konstruierte Pseudo-Dauer der Heuristik, nicht gelaufenes Wasser.
#
# ZEITPLAN (T-0455) steht bewusst NICHT hier: das ist echtes Kanal-Wasser mit
# echter Dauer und bekannter Ursache. Wirkung im Vergleich zu vorher: dieselben
# Cloud-Zeitplan-Laeufe kamen bisher je nach Ingest-Pfad als MANUELL (drin) oder
# AUTOMATIK (raus) an -- der Filter war also inkonsistent, nicht streng. Ab
# jetzt sind sie einheitlich drin. Betrifft nur NEUE Events; die Bestandsserien
# behalten ihr altes Etikett, bis sie umklassifiziert werden.
AUSGESCHLOSSENE_AUSLOSER = {
    Ausloser.AUTOMATIK, Ausloser.UNBEKANNT, Ausloser.IGNORIERT,
    Ausloser.FREMDWASSER,
}


def _automatik_scharf_ab(zonen: list["ZonenKonfig"]) -> datetime | None:
    """T-0492: ab wann `automatik` auf diesem Kanal echtes Wasser bedeutet.

    `auto_loop_opt_in` beantwortet die Gegenwartsfrage ("giesst die Zone
    heute autonom?"), taugt aber nicht fuer Historie: dieselbe Zone hat
    vorher im Shadow gelaufen und dabei `ausloser=automatik` geloggt, OHNE
    dass Wasser floss. Solche Events als Response-Punkte zu lesen hiesse,
    eine Dosis mit garantiert null Wirkung ins Training zu geben.

    **Frueheste** gesetzte Scharfschaltung der beteiligten Zonen, nicht die
    spaeteste: die Frage lautet "floss echtes Wasser?", und sobald EINE
    Zone des Kanals scharf ist, oeffnet die Engine den Kanal real -- das
    Wasser erreicht dann alle Zonen daran. (Praktisch identisch, solange
    bambuswald und bambuswald_yogaraum denselben Zeitpunkt tragen; die
    Regel muss trotzdem stimmen, nicht zufaellig richtig sein.)

    `None` = keine der Zonen war je scharf -> `automatik` bleibt komplett
    ausgeschlossen (positive Provenienzregel: im Zweifel raus).
    """
    zeitpunkte = [
        z.auto_loop_scharf_seit for z in zonen
        if z is not None and z.auto_loop_scharf_seit is not None
    ]
    return min(zeitpunkte) if zeitpunkte else None


def _ist_trainingsfaehig(
    ausloser: Ausloser | None,
    zeitstempel: datetime,
    automatik_scharf_ab: datetime | None,
) -> bool:
    """Einzige Stelle, die ueber die Auslöser-Filterung entscheidet.

    Bewusst eine Funktion fuer Pipeline und Zaehl-Proxy: driften die
    beiden auseinander, entwertet das den Events-Trigger des Retrains
    still ([[fehlerpattern_detektor_ohne_konsument]] in der Wirkung).
    """
    if ausloser is None or ausloser not in AUSGESCHLOSSENE_AUSLOSER:
        return True
    if ausloser is not Ausloser.AUTOMATIK:
        return False
    # T-0492: konditional statt bedingungslos -- aber nur nachweislich
    # scharfe Laeufe, kein pauschales Umdeuten der Alt-Labels.
    return (
        automatik_scharf_ab is not None
        and zeitstempel >= automatik_scharf_ab
    )

# Ventil-IDs, die keine Kanal-UUID sind — damit kein Schlauch-/Sonder-Event
# durchrutscht (variable Durchflussrate).
AUSGESCHLOSSENE_VENTIL_IDS = {"", "manuell", "sensor_heuristik"}
# Events mit diesen ventil_ids haben keinen ':K'-Kanal-Suffix, sind aber
# echte Wasser-Events einer einzigen Zone (z. B. Backfill aus dem
# Gardena-Web-DHS oder manuell via UI). Der Kanal wird dann aus
# `zone.ventil_kanal` aufgeloest.
BACKFILL_VENTIL_IDS = {"backfill_app", "gardena_web"}

# Default-Gap fuer Puls-Clustering wenn die Konfig keinen Wert liefert.
# Deckt das typische "5 min Anpuls + 30 min Pause + Hauptgiessen"-Muster.
DEFAULT_CLUSTER_GAP_MIN = 60


@dataclass
class BewaesserungsPuls:
    """Ein logischer Bewaesserungs-Puls (ein oder mehrere eng getaktete
    SCHLIESSEN-Events auf dem gleichen Kanal).

    Ein Puls entsteht, wenn aufeinanderfolgende Events weniger als
    `cluster_gap_min` Minuten Pause zwischen `prev.t_end` und
    `next.t_start` haben. Aggregation:

    - `t_start`: Start des ersten Events.
    - `t_end`: Ende des letzten Events (= event.zeitstempel).
    - `dauer_s`: Summe der Einzel-Dauern (Netto-Wasserzeit; Pausen
      zwischen Events zaehlen NICHT mit, sie sind ja gerade das, was
      den Anpuls vom Hauptgiessen trennt).
    - `liter`: Summe der Einzel-Liter wenn alle Events `liter` gesetzt
      haben, sonst None (Fallback ueber Kanalrate im Feature-Builder).
    - `ausloser`: vom ersten Event (Cluster sollten homogen sein).
    - `first_event`: fuer Metadaten (event_id, zone-Zuordnung).
    - `event_ids`: IDs aller aggregierten Events (Debug/Traceability).
    """
    t_start: datetime
    t_end: datetime
    dauer_s: int
    liter: float | None
    ausloser: Ausloser
    first_event: VentilEreignis
    event_ids: list[int] = field(default_factory=list)


def _baue_pulse(
    events_kanal: list[VentilEreignis],
    cluster_gap_min: int,
    automatik_scharf_ab: datetime | None = None,
) -> list[BewaesserungsPuls]:
    """Fasst echte Wasser-SCHLIESSEN-Events eines Kanals zu Pulsen zusammen.

    Input: chronologisch sortierte Event-Liste eines Kanals.
    Rueckgabe: Liste von BewaesserungsPuls (chronologisch, disjunkt).

    Filterregeln (passen zur `erstelle_response_features`-Logik):
    - Nur `aktion=SCHLIESSEN` und `dauer_sekunden>0`.
    - `_ist_trainingsfaehig(...)`: alles ausser AUSGESCHLOSSENE_AUSLOSER,
      plus `automatik` ab `automatik_scharf_ab` (T-0492).

    `automatik_scharf_ab=None` (Default) = altes Verhalten, `automatik`
    faellt komplett raus.

    Clustering: Events werden in den aktuellen Puls aufgenommen, wenn
    `event.t_start <= puls.t_end + cluster_gap_min`.
    """
    gap = timedelta(minutes=max(0, cluster_gap_min))
    pulse: list[BewaesserungsPuls] = []
    aktuell: BewaesserungsPuls | None = None

    for e in events_kanal:
        if e.aktion != VentilAktion.SCHLIESSEN or e.dauer_sekunden <= 0:
            continue
        if not _ist_trainingsfaehig(
            e.ausloser, e.zeitstempel, automatik_scharf_ab,
        ):
            continue
        t_end = e.zeitstempel
        t_start = t_end - timedelta(seconds=int(e.dauer_sekunden))

        if aktuell is None or t_start > aktuell.t_end + gap:
            # neuen Puls starten
            aktuell = BewaesserungsPuls(
                t_start=t_start,
                t_end=t_end,
                dauer_s=int(e.dauer_sekunden),
                liter=(float(e.liter) if e.liter is not None else None),
                ausloser=e.ausloser,
                first_event=e,
                event_ids=[e.id] if e.id is not None else [],
            )
            pulse.append(aktuell)
        else:
            # in aktuellen Puls mergen
            aktuell.t_end = max(aktuell.t_end, t_end)
            aktuell.dauer_s += int(e.dauer_sekunden)
            if aktuell.liter is not None and e.liter is not None:
                aktuell.liter += float(e.liter)
            elif e.liter is None:
                # sobald ein Teil-Event keine Liter hat, wird Summe None
                # (damit _liter_pro_sekunde auf Kanalrate-Fallback geht)
                aktuell.liter = None
            if e.id is not None:
                aktuell.event_ids.append(e.id)

    return pulse


def _anderer_puls_im_fenster(
    pulse: list[BewaesserungsPuls],
    eigener_index: int,
    t_start: datetime,
    t_end: datetime,
) -> bool:
    """True, wenn ein anderer Puls im Fenster [t_start, t_end] seinen
    `t_start` hat. Genutzt fuer die pro-Horizont-Label-Validierung:
    delta_Xh ist nur sauber, wenn kein nachfolgender Puls die
    Sensor-Response verfaelscht hat.
    """
    for i, p in enumerate(pulse):
        if i == eigener_index:
            continue
        if t_start <= p.t_start <= t_end:
            return True
    return False


def _vpd_kpa(t_celsius: float, rh_prozent: float) -> float:
    """VPD in kPa via Magnus — Kopie aus features.py::_vpd_kpa, damit
    response_features.py ohne Import vom schweren FeatureExtraktor steht.
    """
    if rh_prozent < 0 or rh_prozent > 100:
        rh_prozent = max(0.0, min(100.0, rh_prozent))
    e_s = 0.6108 * math.exp(17.27 * t_celsius / (t_celsius + 237.3))
    return e_s * (1.0 - rh_prozent / 100.0)


def _finde_naechste_messung(
    messungen: list[SensorMessung],
    ziel_zeit: datetime,
    richtung: str,
    toleranz_min: int,
    vorwaerts_toleranz_min: int = 0,
) -> float | None:
    """Findet die Boden-Feuchte am naechsten zu `ziel_zeit`.

    richtung='rueckwaerts': bevorzugt Messungen <= ziel_zeit. Mit
        `vorwaerts_toleranz_min > 0` werden auch Messungen knapp NACH
        `ziel_zeit` akzeptiert (im Fenster [ziel, ziel+vorwaerts_tol]),
        um Boundary-Faelle zu faengen — z. B. Puls-Start 10:33:38 mit
        Sensor-Messung 10:33:54 (16 s spaeter, physikalisch noch der
        Pre-Event-Zustand).
    richtung='bidirektional': bester Wert in [ziel-tol, ziel+tol].

    `messungen` muss chronologisch sortiert sein.
    """
    tol = timedelta(minutes=toleranz_min)
    vorwaerts_tol = timedelta(minutes=vorwaerts_toleranz_min)
    untergrenze = ziel_zeit - tol
    if richtung == "rueckwaerts":
        obergrenze = ziel_zeit + vorwaerts_tol
    else:
        obergrenze = ziel_zeit + tol
    # beste_abweichung startet als groesstes erlaubtes Toleranz — ein Kandidat
    # muss mindestens so nah dran sein.
    beste_abweichung = max(tol, vorwaerts_tol)
    bester_wert: float | None = None
    for m in messungen:
        if m.boden_feuchte is None:
            continue
        if m.zeitstempel < untergrenze:
            continue
        if m.zeitstempel > obergrenze:
            break  # sortiert → Rest liegt weiter in der Zukunft
        if richtung == "rueckwaerts" and m.zeitstempel > ziel_zeit + vorwaerts_tol:
            continue
        abw = abs(m.zeitstempel - ziel_zeit)
        if abw <= beste_abweichung:
            beste_abweichung = abw
            bester_wert = float(m.boden_feuchte)
    return bester_wert


def _messungen_pro_geraet(
    messungen: list[SensorMessung],
) -> dict[str, list[SensorMessung]]:
    """Gruppiert Messungen mit Feuchte-Wert pro geraet_id (None -> "").

    Reihenfolge innerhalb der Gruppen bleibt erhalten — chronologisch
    sortierter Input ergibt chronologisch sortierte Gruppen.
    """
    gruppen: dict[str, list[SensorMessung]] = {}
    for m in messungen:
        if m.boden_feuchte is None:
            continue
        gruppen.setdefault(m.geraet_id or "", []).append(m)
    return gruppen


def _geraete_reihenfolge(
    gruppen: dict[str, list[SensorMessung]],
    lead_geraet: str | None,
) -> list[str]:
    """Lead-Geraet der Zone zuerst, dann die uebrigen sortiert."""
    reihenfolge: list[str] = []
    if lead_geraet and lead_geraet in gruppen:
        reihenfolge.append(lead_geraet)
    reihenfolge.extend(g for g in sorted(gruppen) if g not in reihenfolge)
    return reihenfolge


def _feuchte_paar_gleiches_geraet(
    messungen: list[SensorMessung],
    t_start: datetime,
    t_eval: datetime,
    lead_geraet: str | None,
    *,
    f_vor_toleranz_min: int = F_VOR_TOLERANZ_MIN,
    f_vor_vorwaerts_min: int = F_VOR_VORWAERTS_MIN,
    f_nach_toleranz_min: int = F_NACH_TOLERANZ_MIN,
) -> tuple[float | None, float | None, str | None]:
    """T-0366/F9: f_vor/f_nach muessen vom selben Sensor stammen.

    Bei Zonen mit mehreren Sensoren sind die Skalen nicht vergleichbar
    (Gardena Relativ-Index vs. FYTA `soil_moisture`) — ein Delta ueber
    Geraete-Grenzen ist Muell. T-0410: die Differenz ist NICHT als
    Bereichs-Unterschied bezifferbar (die frueher hier genannten "FYTA
    0-65 %" sind widerlegt -- real bis 100, 25 % der Werte > 65); sie
    folgt aus verschiedenen Messprinzipien/Standorten. Empirisch:
    Korrelation FYTA<->Gardena ~0, kein gueltiges Skalen-Mapping.
    Erstes Geraet (Lead zuerst, Rest sortiert),
    das BEIDE Werte liefert, gewinnt; sonst `(None, None, None)`.
    """
    gruppen = _messungen_pro_geraet(messungen)
    if not gruppen:
        return None, None, None

    for geraet_id in _geraete_reihenfolge(gruppen, lead_geraet):
        werte = gruppen[geraet_id]
        f_vor = _finde_naechste_messung(
            werte, t_start, richtung="rueckwaerts",
            toleranz_min=f_vor_toleranz_min,
            vorwaerts_toleranz_min=f_vor_vorwaerts_min,
        )
        f_nach = _finde_naechste_messung(
            werte, t_eval, richtung="bidirektional",
            toleranz_min=f_nach_toleranz_min,
        )
        if f_vor is not None and f_nach is not None:
            return f_vor, f_nach, geraet_id
    return None, None, None


def _gradient_prozent_pro_stunde(
    messungen: list[SensorMessung],
    ziel_zeit: datetime,
    stunden: int,
) -> float | None:
    """Lineare Steigung der Feuchte ueber die letzten N Stunden vor `ziel_zeit`.

    Rueckgabe in Prozentpunkten pro Stunde; None wenn zu wenig Daten.
    """
    grenze = ziel_zeit - timedelta(hours=stunden)
    punkte = []  # (h_offset, wert)
    for m in messungen:
        if m.zeitstempel < grenze:
            continue
        if m.zeitstempel > ziel_zeit:
            break
        if m.boden_feuchte is None:
            continue
        offset = (m.zeitstempel - grenze).total_seconds() / 3600
        punkte.append((offset, float(m.boden_feuchte)))
    if len(punkte) < 2:
        return None
    n = len(punkte)
    sx = sum(p[0] for p in punkte)
    sy = sum(p[1] for p in punkte)
    sxy = sum(p[0] * p[1] for p in punkte)
    sxx = sum(p[0] ** 2 for p in punkte)
    nenner = n * sxx - sx * sx
    if abs(nenner) < 1e-9:
        return 0.0
    return round((n * sxy - sx * sy) / nenner, 4)


def _wetter_fenster(
    wetter_roh: list[dict],
    wetter_standort: str,
    von: datetime,
    bis: datetime,
    as_of: datetime | None = None,
) -> list[dict]:
    """Wetterstunden im Fenster [von, bis] aus leakage-sicherem Forecast.

    Nutzt die juengste `abfrage_zeitstempel` <= `as_of` (Default: `von`).
    Fuer Response-Features muss `as_of=t_start` sein, damit Forecasts aus
    der Laufzeit oder nach dem Lauf nicht ins Training leaken.
    """
    as_of = as_of or von
    von_iso = von.isoformat()
    bis_iso = bis.isoformat()
    as_of_iso = as_of.isoformat()
    passend = [w for w in wetter_roh if w.get("standort_id") == wetter_standort]
    # juengste Abfrage <= as_of
    letzte_abfrage = None
    for w in passend:
        if w["abfrage_zeitstempel"] <= as_of_iso:
            if letzte_abfrage is None or w["abfrage_zeitstempel"] > letzte_abfrage:
                letzte_abfrage = w["abfrage_zeitstempel"]
    if letzte_abfrage is None:
        return []
    out: list[dict] = []
    for w in passend:
        if (w["abfrage_zeitstempel"] == letzte_abfrage
                and von_iso <= w["vorhersage_zeitstempel"] <= bis_iso):
            out.append(w)
    return out


def _wetter_stats(stunden: list[dict]) -> dict:
    """Aggregiert Niederschlag, ET0, Temperatur-Mittel, VPD-Mittel."""
    if not stunden:
        return {
            "niederschlag_mm": 0.0, "et0_mm": 0.0,
            "temp_mittel": None, "vpd_mittel": None, "n": 0,
        }
    nieder = sum((w.get("niederschlag_mm") or 0.0) for w in stunden)
    et0 = sum((w.get("et0_mm") or 0.0) for w in stunden)
    temps = [w.get("temperatur") for w in stunden if w.get("temperatur") is not None]
    vpd_werte = [
        _vpd_kpa(w["temperatur"], w["luftfeuchte"])
        for w in stunden
        if w.get("temperatur") is not None and w.get("luftfeuchte") is not None
    ]
    return {
        "niederschlag_mm": round(float(nieder), 3),
        "et0_mm": round(float(et0), 3),
        "temp_mittel": (
            round(sum(temps) / len(temps), 2) if temps else None
        ),
        "vpd_mittel": (
            round(sum(vpd_werte) / len(vpd_werte), 4) if vpd_werte else None
        ),
        "n": len(stunden),
    }


def _liegt_in_ausschluss_fenster(
    konfig: GesamtKonfig,
    zone_id: str,
    t_start: datetime,
    t_end: datetime,
    geraet_id: str | None = None,
) -> bool:
    """True wenn das Event-Intervall [t_start, t_end] ein Ausschluss-Fenster
    einer Zone schneidet.

    T-0386-Semantik (gespiegelt aus `speicher.hole_feuchte_werte`):
    `fenster.geraet_id is None` -> das Fenster gilt fuer ALLE Sensoren der
    Zone; gesetzt -> nur fuer Zeilen dieses Sensors. `geraet_id=None`
    (Aufrufer kennt den Sensor nicht) matcht wie bisher jedes Fenster.
    """
    fenster = getattr(konfig, "ml_ausschluss_fenster", []) or []
    for f in fenster:
        if f.zone_id != zone_id:
            continue
        f_geraet = getattr(f, "geraet_id", None)
        if (f_geraet is not None and geraet_id is not None
                and f_geraet != geraet_id):
            continue
        # Schnittmenge zweier Intervalle
        if t_start <= f.bis and f.von <= t_end:
            return True
    return False


AQUABLOOM_KREIS_KANAL = 0


def _aquabloom_kreis(zone_id: str) -> tuple[str, int]:
    """Pseudo-Kreis einer AquaBloom-Pump-Zone (ein Pump = ein Kreis).

    EINE Quelle fuer beide Richtungen -- `_circuit_fuer_event` (Event ->
    Kreis) und `_circuit_zuordnung` (Kreis -> Zonen). Getrennt gebaut
    laufen die Haelften auseinander: genau das war F9/T-0482, wo nur die
    Erzeugung existierte und der Schluessel nie als Key auftauchte.
    """
    return (f"aquabloom:{zone_id}", AQUABLOOM_KREIS_KANAL)


def _circuit_zuordnung(konfig: GesamtKonfig) -> dict[tuple[str | None, int], list[str]]:
    """{(geraet_id, ventil_kanal): [zone_id, ...]} fuer Shared-Valves.

    T-0482: AquaBloom-Pump-Zonen haben kein Gardena-Ventil und damit
    keinen `ventil_kanal` — sie kommen ueber ihren Pseudo-Kreis rein.
    Ohne diese Haelfte erzeugt `_circuit_fuer_event` zwar einen Kreis,
    der hier aber nie als Key steht; `erstelle_response_features`
    verbucht den Puls dann unter `kanal_ohne_zone` und verwirft ihn.
    """
    zuordnung: dict[tuple[str | None, int], list[str]] = defaultdict(list)
    for z in konfig.zonen:
        if z.ventil_kanal is not None:
            zuordnung[(z.ventil_geraet_id, int(z.ventil_kanal))].append(z.zone_id)
        # Kein elif: eine Zone kann Ventil UND Pump haben — dann gehoert
        # sie in beide Kreise (verschiedene Wasserquellen, eigene Pulse).
        if _ist_aquabloom_konfiguriert(z):
            zuordnung[_aquabloom_kreis(z.zone_id)].append(z.zone_id)
    return zuordnung


def _kanal_aus_ventil_id(ventil_id: str) -> int | None:
    """Extrahiert den Kanal aus der ventil_id '<UUID>:K'.

    Rueckgabe None fuer nicht-Kanal-Events (Schlauch, 'manuell', leer,
    oder backfill/gardena_web — diese brauchen einen Zone-Lookup, siehe
    `_circuit_fuer_event`).
    """
    if not ventil_id or ventil_id in AUSGESCHLOSSENE_VENTIL_IDS:
        return None
    if ":" not in ventil_id:
        return None
    try:
        return int(ventil_id.rsplit(":", 1)[1])
    except ValueError:
        return None


def _circuit_fuer_event(
    event: VentilEreignis,
    zonen_nach_id: dict[str, ZonenKonfig],
) -> tuple[str | None, int] | None:
    """Ermittelt den Bewaesserungskreis inklusive Backfill-/Web-Fallback.

    - bekannte `zone_id` -> `(zone.ventil_geraet_id, zone.ventil_kanal)`.
    - `'<UUID>:K'` ohne bekannte Zone -> `(UUID, K)`.
    - Schlauch (`'manuell'`, leer) oder unbekannte ID → None.
    """
    vid = event.ventil_id
    # F9: Konvertierte AquaBloom-Pulse tragen ventil_id='sensor_heuristik'
    # (in AUSGESCHLOSSENE_VENTIL_IDS) UND ihre Zone hat keinen ventil_kanal
    # (Pump-Zone, kein Gardena-Ventil). Ohne Sonderfall faellt der Puls hier
    # raus und der AQUABLOOM-Zweig (delta_6h>1.0) ist toter Code -- entgegen
    # dem dokumentierten Design (kalibrierung: "AquaBloom landet in den
    # Response-Features") und dem Wochen-Review-Bedarf. Pro Zone ein
    # eindeutiger Pseudo-Kreis (ein Pump = ein unabhaengiger Kreis); der
    # delta_6h>1.0-Filter haelt rauscharme Mini-Pulse weiter raus.
    # Gegenstueck in `_circuit_zuordnung` (T-0482) — beide Haelften bauen
    # den Schluessel ueber `_aquabloom_kreis`, nie getrennt.
    if event.ausloser == Ausloser.AQUABLOOM:
        return _aquabloom_kreis(event.zone_id)
    if not vid or vid in AUSGESCHLOSSENE_VENTIL_IDS:
        return None
    zone = zonen_nach_id.get(event.zone_id)
    if zone is not None and zone.ventil_kanal is not None:
        return (zone.ventil_geraet_id, int(zone.ventil_kanal))
    if ":" in vid:
        kanal = _kanal_aus_ventil_id(vid)
        if kanal is None:
            return None
        return (vid.rsplit(":", 1)[0] or None, kanal)
    if vid in BACKFILL_VENTIL_IDS:
        return None
    return None


def _kanal_fuer_event(
    event: VentilEreignis,
    zonen_nach_id: dict[str, ZonenKonfig],
) -> int | None:
    """Backward-Compat-Testhelper: nur die Kanalnummer eines Circuits."""
    circuit = _circuit_fuer_event(event, zonen_nach_id)
    return circuit[1] if circuit is not None else None


def _liter_pro_sekunde_puls(
    puls: BewaesserungsPuls,
    zone: ZonenKonfig,
    konfig: GesamtKonfig,
) -> float | None:
    """liter/sekunde fuer einen Puls — aus `puls.liter` (wenn vollstaendig
    aggregiert) oder aus der Bilanz-Kanal-Rate der Zone.
    """
    if puls.liter is not None and puls.liter > 0 and puls.dauer_s > 0:
        return round(float(puls.liter) / puls.dauer_s, 4)
    # Kanal-Rate aus BilanzKonfig: Liter/Minute pro physischem Circuit -> L/s
    rate_l_min = konfig.bilanz.liter_pro_minute_fuer_zone(zone)
    if rate_l_min is None or rate_l_min <= 0:
        return None
    return round(float(rate_l_min) / 60.0, 4)


def _baue_row(
    *,
    ziel_zone: ZonenKonfig,
    messungen_der_zone: list[SensorMessung],
    puls: BewaesserungsPuls,
    puls_index: int,
    alle_pulse: list[BewaesserungsPuls],
    wetter_roh: list[dict],
    wetter_standort: str,
    shared_valve: bool,
    liter_pro_sekunde: float,
    lead_geraet: str | None = None,
) -> dict | None:
    """Baut eine Feature-Row fuer die Zielzone aus einem Puls.

    Rueckgabe None wenn kein f_vor gefunden wird oder alle Label-
    Horizonte durch Sensor-Luecken ODER nachfolgende Pulse entwertet
    sind.

    T-0366/F9 generalisiert an den Ursprung: f_vor und JEDES f_nach der
    Zeile stammen garantiert vom selben Sensor. Erstes Geraet (Lead
    zuerst, Rest sortiert), das f_vor UND mindestens ein Label liefert,
    gewinnt. Ein Horizont, fuer den das Gewinner-Geraet keinen Wert im
    Toleranzfenster hat, bleibt None — er weicht NICHT auf ein anderes
    Geraet aus (Skalen nicht vergleichbar, s. T-0410 -- nicht als
    Bereichs-Unterschied bezifferbar, FYTA liefert real ebenfalls bis 100).
    """
    t_start = puls.t_start
    t_end = puls.t_end

    gruppen = _messungen_pro_geraet(messungen_der_zone)

    # Label-Horizonte: delta_6h/12h/24h.
    # Pro Horizont gilt separat (geraete-unabhaengig): es darf kein
    # anderer Puls innerhalb [t_start, t_end + Xh] seinen Start haben,
    # sonst wird die Sensor-Response vermischt und das Label ist entwertet.
    horizont_entwertet = {
        h: _anderer_puls_im_fenster(
            alle_pulse, puls_index, t_start, t_end + timedelta(hours=h),
        )
        for h in LABEL_HORIZONTE
    }

    f_vor: float | None = None
    labels: dict[str, float | None] = {}
    geraet_id: str | None = None
    for kandidat in _geraete_reihenfolge(gruppen, lead_geraet):
        werte = gruppen[kandidat]
        kandidat_f_vor = _finde_naechste_messung(
            werte, t_start,
            richtung="rueckwaerts", toleranz_min=F_VOR_TOLERANZ_MIN,
            vorwaerts_toleranz_min=F_VOR_VORWAERTS_MIN,
        )
        if kandidat_f_vor is None:
            continue
        kandidat_labels: dict[str, float | None] = {}
        for h in LABEL_HORIZONTE:
            if horizont_entwertet[h]:
                kandidat_labels[f"delta_{h}h"] = None
                continue
            f_nach = _finde_naechste_messung(
                werte, t_end + timedelta(hours=h),
                richtung="bidirektional", toleranz_min=F_NACH_TOLERANZ_MIN,
            )
            kandidat_labels[f"delta_{h}h"] = (
                round(float(f_nach - kandidat_f_vor), 3)
                if f_nach is not None else None
            )
        if any(v is not None for v in kandidat_labels.values()):
            f_vor = kandidat_f_vor
            labels = kandidat_labels
            geraet_id = kandidat
            break

    # Kein Geraet liefert f_vor + mindestens ein Label — Row nutzlos.
    if f_vor is None or geraet_id is None:
        return None
    messungen_geraet = gruppen[geraet_id]

    # T-0168 AquaBloom-Filter: Pulse sind sehr klein (10 min × 0.083 L);
    # Sensor-Quantisierung ist 5 pp. Bei delta_6h <= 1.0 ist der Signal-
    # Rausch-Abstand zu klein und wuerde das Modell zu "0 min reicht"
    # verzerren. Filter am Trainings-Label (nicht am Feature-Input) —
    # so kommen nur Rows mit echtem Sensor-Antwort-Signal ins Training.
    # stdlib-Logger -> %s-Format pflicht (Memory
    # `fehlerpattern_stdlib_structlog_mix.md`).
    if puls.ausloser == Ausloser.AQUABLOOM:
        delta_6h = labels.get("delta_6h")
        if delta_6h is None or delta_6h <= 1.0:
            logger.debug(
                "response_features.aquabloom_kein_signal zone=%s delta_6h=%s",
                ziel_zone.zone_id, delta_6h,
            )
            return None

    # Gradienten aus demselben Geraet wie f_vor — dieselbe Skalen-
    # Misch-Fehlerklasse wie beim f_vor/f_nach-Paar.
    grad_3h = _gradient_prozent_pro_stunde(messungen_geraet, t_start, 3)
    grad_24h = _gradient_prozent_pro_stunde(messungen_geraet, t_start, 24)

    # Wetter-Fenster. NUR vorwaertsgerichtet -- das Rueckwaerts-Fenster ist
    # mit T-0512 entfallen (s. `KONTEXT_FEATURES`).
    stunden_nach_6h = _wetter_fenster(
        wetter_roh, wetter_standort, t_end, t_end + timedelta(hours=6),
        as_of=t_start,
    )
    stunden_ereignis = _wetter_fenster(
        wetter_roh, wetter_standort, t_start, t_end + timedelta(hours=6),
        as_of=t_start,
    )
    stunden_nach_24h = _wetter_fenster(
        wetter_roh, wetter_standort, t_end, t_end + timedelta(hours=24),
        as_of=t_start,
    )
    stats_nach_6h = _wetter_stats(stunden_nach_6h)
    stats_ereignis = _wetter_stats(stunden_ereignis)
    stats_nach_24h = _wetter_stats(stunden_nach_24h)

    # Zeitzone: DOY + Tageszeit
    doy = t_start.timetuple().tm_yday
    minute_des_tages = t_start.hour + t_start.minute / 60.0
    jahreszeit_sin = math.sin(2 * math.pi * doy / 365.0)
    jahreszeit_cos = math.cos(2 * math.pi * doy / 365.0)
    tageszeit_sin = math.sin(2 * math.pi * minute_des_tages / 24.0)
    tageszeit_cos = math.cos(2 * math.pi * minute_des_tages / 24.0)

    # Event-Referenz: erstes Event des Pulses (fuer Metadaten).
    first_event = puls.first_event

    row: dict = {
        "zone_id": ziel_zone.zone_id,
        "zeitstempel": t_start.isoformat(),
        # Meta-Spalte (Traceability + geraet-scoped ml_ausschluss_fenster),
        # KEIN Trainings-Feature — darf nie in KONTEXT_FEATURES landen,
        # sonst lernt das Modell die Sensor-ID.
        "geraet_id": geraet_id,
        "event_id": first_event.id,
        "puls_event_count": len(puls.event_ids) or 1,
        "puls_event_ids": list(puls.event_ids),
        "shared_valve": bool(shared_valve),
        # Puls-Aggregat (Summe der Netto-Wasserzeit)
        "dauer_s": int(puls.dauer_s),
        "liter_pro_sekunde": round(float(liter_pro_sekunde), 4),
        "f_vor": round(float(f_vor), 2),
        "f_vor_gradient_3h": grad_3h,
        "f_vor_gradient_24h": grad_24h,
        # Wetter-Kontext
        "et0_nach_6h": stats_nach_6h["et0_mm"],
        "niederschlag_nach_24h": stats_nach_24h["niederschlag_mm"],
        "temperatur_ereignis": stats_ereignis["temp_mittel"],
        "vpd_mittel": stats_ereignis["vpd_mittel"],
        # Saisonalitaet
        "jahreszeit_sin": round(jahreszeit_sin, 4),
        "jahreszeit_cos": round(jahreszeit_cos, 4),
        "tageszeit_sin": round(tageszeit_sin, 4),
        "tageszeit_cos": round(tageszeit_cos, 4),
    }
    row.update(labels)
    return row


async def erstelle_response_features(
    speicher: Speicher,
    konfig: GesamtKonfig,
    von: datetime,
    bis: datetime,
) -> "pd.DataFrame":
    """Baut das Feature-Set fuer das Response-Modell (Puls-Aggregation).

    Aufeinanderfolgende Events eines Kanals, die durch weniger als
    `ml.bewaesserungs_response.cluster_gap_min` Minuten getrennt sind,
    werden zu einem Bewaesserungs-Puls zusammengefasst ("5 min Anpuls
    + 30 min Pause + Hauptgiessen" = ein Puls, `dauer_s` = Summe).

    Liefert ein DataFrame mit einer Zeile pro Puls; Kanaele mit
    mehreren Zonen (bambuswald/yogaraum) werden pro Zone gespiegelt.
    Leeres DataFrame wenn kein Puls durch den Filter kommt.
    """
    # Puffer: 24h rueckwaerts fuer Gradients, 24h+ vorwaerts fuer Labels.
    puffer_von = von - timedelta(hours=25)
    puffer_bis = bis + timedelta(hours=max(LABEL_HORIZONTE) + 2)

    ventil_ereignisse = await speicher.hole_alle_ventil_ereignisse(
        puffer_von, puffer_bis,
    )
    messungen_all = await speicher.hole_alle_messungen(puffer_von, puffer_bis)
    wetter_roh = await speicher.hole_wetter_vorhersagen(puffer_von, puffer_bis)

    if not ventil_ereignisse or not messungen_all:
        logger.info(
            "ml.response_features.leere_rohdaten ventile=%d messungen=%d",
            len(ventil_ereignisse), len(messungen_all),
        )
        return pd.DataFrame()

    # T-0403: Ab hier ist alles reine CPU-Arbeit -- verschachtelte Schleifen
    # ueber Kanaele, Pulse und Zonen plus der DataFrame-Bau. Vorher lief das
    # direkt im async-Rumpf und blockierte damit den Event-Loop; ueber 365
    # Tage kostet es laut Messung in T-0458 rund 59 Sekunden, in denen weder
    # HTTP-Requests noch der Entscheidungsloop drankommen.
    #
    # Dasselbe Muster wie im Feuchte-Pfad (`ml/features.py`, T-0064:
    # `_baue_dataframe_sync` hinter `asyncio.to_thread`). Die drei DB-Abrufe
    # oben bleiben async, alles danach wandert in den Thread -- der Schnitt
    # liegt genau hier, weil unterhalb kein `await` mehr vorkommt.
    return await asyncio.to_thread(
        _baue_response_df_sync,
        konfig, von, bis, ventil_ereignisse, messungen_all, wetter_roh,
    )


def _baue_response_df_sync(
    konfig: GesamtKonfig,
    von: datetime,
    bis: datetime,
    ventil_ereignisse: list,
    messungen_all: list,
    wetter_roh: object,
) -> "pd.DataFrame":
    """Der CPU-Teil von `erstelle_response_features` (T-0403).

    Bewusst eine reine Funktion ohne `speicher`: sie laeuft in einem
    Worker-Thread, und ein aiosqlite-Zugriff von dort waere ein Fehler
    (die Verbindung gehoert dem Event-Loop). Alles, was aus der Datenbank
    gebraucht wird, kommt fertig als Argument herein.
    """
    # Cluster-Gap aus Konfig (Default fuer Tests/Kompat: 60 min).
    response_konfig = getattr(konfig, "ml_bewaesserungs_response", None)
    cluster_gap_min = int(
        getattr(response_konfig, "cluster_gap_min", DEFAULT_CLUSTER_GAP_MIN)
    )

    # Messungen pro Zone (sortiert) + Ventil-Events pro Kanal.
    messungen_pro_zone: dict[str, list[SensorMessung]] = defaultdict(list)
    for m in messungen_all:
        messungen_pro_zone[m.zone_id].append(m)
    for z in messungen_pro_zone:
        messungen_pro_zone[z].sort(key=lambda m: m.zeitstempel)

    circuit_zonen = _circuit_zuordnung(konfig)
    zonen_nach_id = {z.zone_id: z for z in konfig.zonen}

    # Events pro physischem Bewaesserungskreis fuer die Puls-Bildung gruppieren.
    # Kanal wird aus `ventil_id=<UUID>:K` extrahiert; fuer backfill_app /
    # gardena_web (ohne Suffix) wird der Kanal aus `zone.ventil_kanal`
    # nachgeschlagen, damit diese echten Wasser-Events nicht rausfallen.
    # Bambuswald + Yogaraum teilen Kanal 2 — die beiden Zonen liefern
    # identische backfill_app/gardena_web-Paare; wir deduplizieren sie
    # anhand (zeitstempel, dauer, ventil_id) damit ein geteilter Event
    # nicht als zwei Einzel-Pulse auf Kanal 2 landet.
    ventile_pro_circuit: dict[tuple[str | None, int], list[VentilEreignis]] = defaultdict(list)
    gesehen_pro_circuit: dict[tuple[str | None, int], set[tuple]] = defaultdict(set)
    for e in ventil_ereignisse:
        circuit = _circuit_fuer_event(e, zonen_nach_id)
        if circuit is None:
            continue
        fingerprint = (e.zeitstempel, int(e.dauer_sekunden), e.ventil_id)
        if fingerprint in gesehen_pro_circuit[circuit]:
            continue
        gesehen_pro_circuit[circuit].add(fingerprint)
        ventile_pro_circuit[circuit].append(e)
    for circuit in ventile_pro_circuit:
        ventile_pro_circuit[circuit].sort(key=lambda x: x.zeitstempel)

    zeilen: list[dict] = []
    gesamt_pulse = 0
    ausgeschlossen = {
        "kanal_ohne_zone": 0, "kein_f_vor": 0,
        "ausschluss_fenster": 0, "ausserhalb_fenster": 0,
        "schlauch": 0, "alle_labels_entwertet": 0,
    }

    for circuit, events_kanal in ventile_pro_circuit.items():
        zone_ids = circuit_zonen.get(circuit, [])
        if not zone_ids:
            ausgeschlossen["kanal_ohne_zone"] += len(events_kanal)
            continue
        shared = len(zone_ids) > 1

        # Pulse bilden (inkl. der Primaer-Event-Filter aus `_baue_pulse`).
        # T-0492: der Kanal, nicht die Einzelzone, entscheidet ueber
        # `automatik` -- ein Lauf naesst alle Zonen daran.
        pulse = _baue_pulse(
            events_kanal,
            cluster_gap_min=cluster_gap_min,
            automatik_scharf_ab=_automatik_scharf_ab(
                [zonen_nach_id[zid] for zid in zone_ids if zid in zonen_nach_id]
            ),
        )
        gesamt_pulse += len(pulse)

        for idx, puls in enumerate(pulse):
            # Zeitfenster des Anfragers — Puls muss mit t_start im
            # gewuenschten Fenster liegen.
            if not (von <= puls.t_start <= bis):
                ausgeschlossen["ausserhalb_fenster"] += 1
                continue

            # Pro verfuegbare Zone eine Row bauen
            for zone_id in zone_ids:
                zone = zonen_nach_id.get(zone_id)
                if zone is None:
                    continue

                lps = _liter_pro_sekunde_puls(puls, zone, konfig)
                if lps is None:
                    ausgeschlossen["schlauch"] += 1
                    continue

                # Fallback = erster konfigurierter Standort (frueher hartkodiert
                # der reale Wohnort -> fuer andere Installationen ein toter
                # Standort ohne Wetterdaten).
                wetter_standort = next(
                    (s.wetter_standort or s.standort_id
                     for s in (konfig.standorte or [])),
                    "",
                )
                for s in (konfig.standorte or []):
                    if zone_id in s.zonen:
                        wetter_standort = s.wetter_standort or s.standort_id
                        break

                row = _baue_row(
                    ziel_zone=zone,
                    messungen_der_zone=messungen_pro_zone.get(zone_id, []),
                    puls=puls,
                    puls_index=idx,
                    alle_pulse=pulse,
                    wetter_roh=wetter_roh,
                    wetter_standort=wetter_standort,
                    shared_valve=shared,
                    liter_pro_sekunde=lps,
                    lead_geraet=zone.aggregat_lead_geraet,
                )
                if row is None:
                    # Entweder kein f_vor oder alle Labels entwertet;
                    # beides wird hier gebuendelt gemeldet.
                    ausgeschlossen["kein_f_vor"] += 1
                    continue
                # T-0386: Fenster-Check NACH dem Row-Bau, weil ein
                # geraet-scoped Fenster nur greift, wenn es den Sensor
                # der Zeile trifft (zone-weite Fenster wie bisher).
                if _liegt_in_ausschluss_fenster(
                    konfig, zone_id, puls.t_start,
                    puls.t_end + timedelta(hours=24),
                    geraet_id=row["geraet_id"],
                ):
                    ausgeschlossen["ausschluss_fenster"] += 1
                    continue
                zeilen.append(row)

    logger.info(
        "ml.response_features.fertig n=%d pulse=%d cluster_gap=%dmin filter=%s",
        len(zeilen), gesamt_pulse, cluster_gap_min, ausgeschlossen,
    )

    if not zeilen:
        return pd.DataFrame()
    df = pd.DataFrame(zeilen)
    # Deterministische Sortierung (Training erwartet chronologisch)
    df = df.sort_values(["zeitstempel", "zone_id"]).reset_index(drop=True)
    return df


async def zaehle_response_event_kandidaten(
    speicher: Speicher,
    konfig: GesamtKonfig,
    von: datetime,
    bis: datetime,
) -> dict[str, int]:
    """T-0473: Obere Schranke fuer die Zeilenzahl pro Zone -- ohne Feature-Bau.

    Der Events-Trigger des Response-Retrains
    (`MlResponseRetrainJob._ist_faellig`) braucht von
    `erstelle_response_features` nur eine Zahl pro Zone, und davon nur die
    Differenz zum Stand des letzten Retrains. Der volle Feature-Aufbau ueber
    365 Tage kostet dafuer ~59 s Event-Loop-Blockade (T-0458). Diese Funktion
    beantwortet dieselbe Frage aus einer GROUP-BY-Zaehlung.

    **Richtung der Abweichung ist die eigentliche Zusage**: der Wert liegt
    NIE unter der echten Zeilenzahl, kann aber darueber liegen. Ueberzaehlen
    laesst den Retrain hoechstens frueher feuern (das Zeit-Gate deckelt das
    ohnehin); Unterzaehlen wuerde den Events-Trigger still entwerten -- eine
    Zone wuerde nie wieder event-getrieben retrainen, ohne dass irgendwo ein
    Fehler auftaucht.

    Deckungsgleich mit der Pipeline (dieselbe Vorauswahl, gleiche Semantik):
    - `aktion=SCHLIESSEN` + `dauer_sekunden>0` (SQL, s. `_baue_pulse`)
    - `ausloser not in AUSGESCHLOSSENE_AUSLOSER`
    - Kreis-Zuordnung ueber `_circuit_fuer_event` + `_circuit_zuordnung`,
      inklusive der Spiegelung geteilter Kanaele auf beide Zonen
      (bambuswald/yogaraum teilen Kanal 2 -> ein Puls, zwei Zeilen).

    Bewusst NICHT nachgebildet -- jeder dieser Schritte kann die echte
    Zeilenzahl nur SENKEN, der Proxy bleibt damit obere Schranke:
    - Puls-Clustering (mehrere Events -> eine Zeile)
    - Fingerprint-Dedup geteilter Kanaele
    - `liter_pro_sekunde is None` (Schlauch)
    - fehlendes `f_vor` / entwertete Labels
    - `ml_ausschluss_fenster`
    - `von <= puls.t_start <= bis` (der Puls-Start kann vor `von` liegen,
      obwohl das Ende drin ist; das Ende ist hier der gezaehlte Zeitstempel)
    """
    gruppen = await speicher.zaehle_wasser_ereignisse_gruppiert(von, bis)
    circuit_zonen = _circuit_zuordnung(konfig)
    zonen_nach_id = {z.zone_id: z for z in konfig.zonen}

    zaehler: dict[str, int] = {z.zone_id: 0 for z in konfig.zonen}
    for zone_id, ventil_id, ausloser_roh, anzahl in gruppen:
        try:
            ausloser = Ausloser(ausloser_roh)
        except ValueError:
            # Unbekannter Ausloeser-Wert: die Pipeline wuerde ihn NICHT
            # filtern (er steht in keinem Ausschluss-Set), also hier auch
            # nicht -- sonst entstuende genau die Unterzaehlung, die dieser
            # Proxy ausschliessen soll (fehlerpattern_neuer_enumwert_faellt_
            # aus_positivfilter).
            ausloser = None
        # T-0492: dieselbe Regel wie die Pipeline, aber bewusst grosszuegiger
        # angewandt. Die Gruppen kommen ohne Einzel-Zeitstempel (nach zone/
        # ventil/ausloser aggregiert), also kann hier nicht pro Event
        # entschieden werden. Als OBERE SCHRANKE ist das korrekt: `bis` statt
        # des echten Event-Zeitpunkts laesst `automatik` schon zaehlen, wenn
        # das Fenster die Scharfschaltung ueberhaupt beruehrt. Ueberzaehlen
        # laesst den Retrain hoechstens frueher feuern, Unterzaehlen wuerde
        # den Events-Trigger still entwerten (s. Docstring).
        if not _ist_trainingsfaehig(
            ausloser, bis,
            _automatik_scharf_ab(
                [zonen_nach_id[zone_id]] if zone_id in zonen_nach_id else []
            ),
        ):
            continue
        pseudo = VentilEreignis(
            zeitstempel=von,
            zone_id=zone_id,
            ventil_id=ventil_id,
            aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=1,
            ausloser=ausloser or Ausloser.MANUELL,
        )
        circuit = _circuit_fuer_event(pseudo, zonen_nach_id)
        if circuit is None:
            continue
        for ziel_zone in circuit_zonen.get(circuit, []):
            zaehler[ziel_zone] = zaehler.get(ziel_zone, 0) + anzahl
    return zaehler


# --- Feature-Spalten-Konstanten (Inferenz + Training nutzen dieselbe Liste) ---

# Features, die gemeinsam vom Forward- (Event liefert Delta) und Inverse-Modell
# (Entscheidung liefert Dauer) genutzt werden — ohne Zone-/Event-Metadaten.
KONTEXT_FEATURES: tuple[str, ...] = (
    "f_vor",
    "f_vor_gradient_3h",
    "f_vor_gradient_24h",
    # T-0512 (05.08.2026): `et0_vor_24h` gestrichen. Der kontrollierte A/B
    # ueber 455 Ereignisse zeigte keinen messbaren Beitrag -- jeder Effekt lag
    # weit innerhalb der Fold-Spanne derselben Variante, der groesste war
    # 4,5-mal kleiner als sein eigenes Rauschband. Zwei Zonen am selben Ventil
    # zeigten sogar entgegengesetzte Vorzeichen.
    #
    # Ausschlaggebend war nicht der fehlende Nutzen allein, sondern dass das
    # Feature als EINZIGES ein Rueckwaerts-Wetterfenster brauchte. Mit ihm
    # entfaellt die gesamte Stitching-Maschinerie aus T-0511 -- und damit
    # genau die Naht zwischen Training und Inferenz, die dort jahrelang
    # unbemerkt auseinanderlief. Ein Feature ohne Nutzen, das eine belegt
    # fehleranfaellige Konstruktion traegt, ist ein schlechter Tausch.
    # Belege: docs/analyse/t0512_ergebnis.md
    "et0_nach_6h",
    "niederschlag_nach_24h",
    "temperatur_ereignis",
    "vpd_mittel",
    "jahreszeit_sin",
    "jahreszeit_cos",
    "tageszeit_sin",
    "tageszeit_cos",
    "shared_valve",
    "liter_pro_sekunde",
)

# Forward-Modell: gegeben Dauer + Kontext → Delta.
FORWARD_FEATURES: tuple[str, ...] = ("dauer_s",) + KONTEXT_FEATURES

# Inverse-Modell: gegeben Ziel-Delta + Kontext → Dauer.
INVERSE_FEATURES: tuple[str, ...] = ("ziel_delta",) + KONTEXT_FEATURES


# Monotonie-Constraints fuer LightGBM (0=keine, 1=steigend, -1=fallend).
# Must-Have fuer die Inverse-Inferenz (Entscheidung): bei groesserem
# ziel_delta darf die vorhergesagte Dauer nicht fallen.
FORWARD_MONOTONIE: dict[str, int] = {
    "dauer_s": 1,
    "liter_pro_sekunde": 1,
    "f_vor": -1,
    "et0_nach_6h": -1,
    "vpd_mittel": -1,
}
INVERSE_MONOTONIE: dict[str, int] = {
    "ziel_delta": 1,
    "f_vor": 1,
    "et0_nach_6h": 1,
    "vpd_mittel": 1,
    "liter_pro_sekunde": -1,
}


def monotone_vektor(feature_cols: list[str], mapping: dict[str, int]) -> list[int]:
    """LightGBM erwartet eine Liste mit +1/-1/0 in Feature-Reihenfolge."""
    return [int(mapping.get(col, 0)) for col in feature_cols]
