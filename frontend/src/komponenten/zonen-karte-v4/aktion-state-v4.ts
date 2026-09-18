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

/** T-0397 (F9): einzige Wahrheit dafuer, ob die Sensordaten einer Zone gerade
 *  unzuverlaessig sind (Ausfall/eingefroren, kein aktueller Wert, oder 0.0-
 *  Defekt). getActionStateV4 leitet daraus 'sensor' ab; OptimumDotsV4 graut
 *  damit die F/L/T/N-Dots aus -- sonst zeigen tote Karten bunte "ok"-Achsen. */
export function datenUnsicher(zone: Zone): boolean {
  return (
    (zone.offene_warnungen ?? []).some(w => DATEN_UNZUVERLAESSIG.has(w.typ))
    || zone.aktuelle_feuchte == null
    || istWahrscheinlichSensorDefekt(zone)   // T-0390: 0.0-Defekt auch ohne offene Warnung
    || leadAusgefallen(zone)                 // T-0532: Wert stammt nicht vom Lead
  )
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
