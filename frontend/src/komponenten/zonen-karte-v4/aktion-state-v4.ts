/* T-0370: V4-Zustandsableitung -- erweitert getActionState (v3, bleibt
   unberuehrt) um zwei Ehrlichkeits-Zustaende:

   - 'sensor': die Datenbasis ist unzuverlaessig (Sensor-Ausfall /
     eingefroren) oder es gibt gar keinen aktuellen Wert. Vorher fiel das
     auf 'ok' zurueck -- gruener Rahmen + "Im Korridor" auf Basis toter
     Daten (Realfall 02.07.: Zitrobaer II + Fuchsie 12h ohne Update,
     Karte gruen). Stale-Daten duerfen keinen OK-Zustand vortaeuschen.
   - 'beobachten': die Engine sieht praeventiven Bedarf, handelt aber
     nicht (kein Blocker, z.B. Monitoring-Modus). Vorher 'ok'/"Im
     Korridor", waehrend der Tagesplan dieselbe Zone unter "Bedarf
     (blockiert)" listete -- zwei Antworten auf dieselbe Frage. */

import type { Zone, GiessEmpfehlung } from '../../typen'
import { getActionState } from './aktion-state-base'
import { istWahrscheinlichSensorDefekt, leadAusgefallen } from '../sensor-defekt'

export type ActionZustandV4 =
  | 'kritisch' | 'giessen_akut' | 'giessen' | 'beobachten' | 'blocker'
  | 'sensor' | 'nass' | 'ok'

/** T-0397 (F2/F3): reine Feuchte-Einordnung, UNABHAENGIG von Empfehlung/Aktion.
 *  Single Source fuer den nass-Zustand der Karte UND die Wert-Farbe -- damit
 *  Karte, Strip und Wert nicht auseinanderlaufen. `null`-Feuchte -> 'ok';
 *  fehlende Daten faengt der 'sensor'-Zustand separat ab. */
export type FeuchteBand = 'kritisch' | 'nass' | 'ok'
export function feuchteBand(zone: Zone): FeuchteBand {
  const ist = zone.aktuelle_feuchte
  if (ist == null) return 'ok'
  if (zone.feuchte_kritisch != null && ist < zone.feuchte_kritisch) return 'kritisch'
  if (zone.feuchte_schwelle_max != null && ist > zone.feuchte_schwelle_max) return 'nass'
  return 'ok'
}

/** Warnungs-Typen, bei denen der angezeigte Feuchte-Wert selbst nicht
 *  mehr vertrauenswuerdig ist. `batterie_*` bleibt bewusst draussen --
 *  niedrige Batterie liefert noch echte Werte (nur Warnzeile, kein
 *  Zustands-Downgrade). */
const DATEN_UNZUVERLAESSIG = new Set(['ausfall', 'sensor_eingefroren'])

/** T-0601 (FE-3): betrifft eine offene Warnung den Sensor, aus dem
 *  `aktuelle_feuchte` stammt?
 *
 *  Vorher zaehlte JEDE Ausfall-Warnung der Zone. In einer Mischzone mit Lead
 *  (waldblumenhain, hecke) versteckte so ein ausgefallener FYTA-Nebensensor
 *  einen frischen Lead-Wert 20 % unter kritisch 25 % hinter "Sensordaten
 *  pruefen" (Gesamtreview 25.09.). Jetzt zaehlt eine Warnung nur, wenn
 *  - sie kein Geraet nennt (Zonen-Warnung wie `sensor_eingefroren`, oder ein
 *    Backend ohne das Feld -- dann bleibt es bei der vorsichtigen Regel),
 *  - die Quelle des Werts unbekannt ist oder ein Median ueber mehrere
 *    Sensoren (`aggregat:<n>`: alle Sensoren sind beteiligt), oder
 *  - sie genau den Quell-Sensor betrifft. */
function warnungBetrifftMesswert(zone: Zone, geraetId: string | null | undefined): boolean {
  if (!geraetId) return true
  const quelle = zone.feuchte_geraet_id
  if (!quelle || quelle.startsWith('aggregat:')) return true
  return geraetId === quelle
}

/** T-0397 (F9): einzige Wahrheit dafuer, ob die Sensordaten einer Zone gerade
 *  unzuverlaessig sind (Ausfall/eingefroren, kein aktueller Wert, oder 0.0-
 *  Defekt). getActionStateV4 leitet daraus 'sensor' ab; OptimumDotsV4 graut
 *  damit die F/L/T/N-Dots aus -- sonst zeigen tote Karten bunte "ok"-Achsen. */
export function datenUnsicher(zone: Zone): boolean {
  return (
    (zone.offene_warnungen ?? []).some(w =>
      DATEN_UNZUVERLAESSIG.has(w.typ) && warnungBetrifftMesswert(zone, w.geraet_id))
    || zone.aktuelle_feuchte == null
    || istWahrscheinlichSensorDefekt(zone)   // T-0390: 0.0-Defekt auch ohne offene Warnung
    || leadAusgefallen(zone)                 // T-0532: Wert stammt nicht vom Lead
  )
}

/** Alter eines ISO-Zeitstempels in Stunden (`null` = unbekannt). */
export function alterStunden(zeit: string | null | undefined, jetzt: number): number | null {
  if (!zeit) return null
  const ms = Date.parse(zeit)
  return Number.isFinite(ms) ? Math.max(0, (jetzt - ms) / 3_600_000) : null
}

/** T-0601: Alterslimit fuer einen Ist-Wert, der eine Schwellen-Aussage
 *  tragen darf. 4 h = das Abruf-Fenster des Backends
 *  (`AGGREGAT_FALLBACK_FENSTER_MIN`, 240 min), auf dem auch die Automatik
 *  giesst. Im Frontend greift es, wenn der Wert zwischen zwei Abrufen ueber
 *  die Grenze altert oder der Abruf scheitert. */
export const MAX_WERT_ALTER_STUNDEN = 4

/** T-0601 (FE-2): EINE Antwort auf "darf der Ist-Wert der Zone eine
 *  Schwellen-Aussage tragen?" -- offene Warnung des Quell-Sensors, 0.0-Defekt,
 *  Lead-Ausfall (alles in `datenUnsicher`) und das Alterslimit.
 *
 *  Vorher beantworteten Kritisch-Band, Triage-Strip, Karten-Sortierung und die
 *  neue Uebersicht die Frage nach drei bis vier Regeln: bei denselben vier
 *  Testzonen zaehlte der Strip "3 kritisch", das Band "4 Zonen", die neue
 *  Uebersicht "1". Jetzt nutzen alle diese Funktion; die neue Uebersicht
 *  prueft zusaetzlich die Kalibrierung. */
export function messwertBelastbar(zone: Zone, jetzt: number): boolean {
  if (datenUnsicher(zone)) return false
  const alter = alterStunden(zone.letztes_update, jetzt)
  return alter != null && alter <= MAX_WERT_ALTER_STUNDEN
}

/** T-0601: belastbarer Ist-Wert unter `feuchte_kritisch`. */
export function istMessKritisch(zone: Zone, jetzt: number): boolean {
  return messwertBelastbar(zone, jetzt) && feuchteBand(zone) === 'kritisch'
}

export function getActionStateV4(
  zone: Zone, empfehlung: GiessEmpfehlung | null,
): ActionZustandV4 {
  if (datenUnsicher(zone)) return 'sensor'

  // T-0397 (F2): "zu nass" ist eine Feuchte-Wahrheit, nicht "Im Korridor".
  // Vorher fehlte der Zweig -> gruener Rahmen bei ist > schwelle_max, waehrend
  // der Triage-Strip dieselbe Zone als "zu nass" zaehlte (Widerspruch auf einer
  // Seite). Substrat-Stau/Wurzelfaeule ist ein dokumentierter Realfall.
  if (feuchteBand(zone) === 'nass') return 'nass'

  const basis = getActionState(zone, empfehlung)

  // F3b (T-0397, Andre 11.07. "Ist ist rot, Warnung gelb"): akut NUR aus
  // ML-Prognose (Ist noch im Korridor) -> amber, nicht kritisch-rot. Rot bleibt
  // einer REAL unterschrittenen Schwelle vorbehalten. getActionState faerbt
  // beides 'kritisch'; hier trennen wir Prognose von realer Unterschreitung.
  if (basis === 'kritisch') {
    const realKritisch = zone.aktuelle_feuchte != null
      && zone.feuchte_kritisch != null
      && zone.aktuelle_feuchte < zone.feuchte_kritisch
    if (!realKritisch) return 'giessen_akut'
  }

  if (basis === 'ok'
      && empfehlung != null
      && !empfehlung.soll_bewaessern
      && (empfehlung.empfehlungs_typ === 'praeventiv'
        || empfehlung.empfehlungs_typ === 'wohlfuehl_grenze')) {
    return 'beobachten'
  }
  return basis
}
