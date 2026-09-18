/* Reine Logik hinter dem KRITISCH-Band -- ohne JSX, damit sie sowohl die
 * Band-Anzeige als auch der Heute-Plan nutzen kann.
 *
 * Warum eigenes Modul: `KritischBand.tsx` exportierte neben der Komponente
 * auch `ermittleKritischeZoneIds`. Fast Refresh arbeitet nur zuverlaessig,
 * wenn eine Datei ausschliesslich Komponenten exportiert
 * (`react-refresh/only-export-components`) -- sonst faellt der Hot-Reload beim
 * Editieren auf einen vollen Reload zurueck. Die Logik liegt deshalb hier;
 * `ermittleGefahr` bleibt die EINE Quelle fuer beide Verbraucher.
 */

import type { Zone, GiessEmpfehlung } from '../typen'
import {
  istWahrscheinlichSensorDefekt, leadAusgefallen, leadAusfallDetail,
} from './sensor-defekt'

export type GefahrGrund =
  | 'unter_kritisch' | 'unter_min' | 'prognose_unter_min' | 'lead_ausgefallen'

export interface KritischerEintrag {
  zone: Zone
  grund: GefahrGrund
  text: string
  prognose24h?: number
}

export function ermittleGefahr(
  zone: Zone,
  prognose24h: number | undefined,
  empf: GiessEmpfehlung | undefined,
): KritischerEintrag | null {
  // Defekt-Hinweise nicht als Kritisch melden -- Daten unzuverlaessig.
  if (istWahrscheinlichSensorDefekt(zone)) {
    return null
  }
  const ist = zone.aktuelle_feuchte
  const kritisch = zone.feuchte_kritisch
  // Echter Ist-Wert unter kritischer Schwelle: immer KRITISCH, das ist
  // ein gemessenes Faktum, keine Prognose.
  if (ist != null && kritisch != null && ist < kritisch) {
    // T-0532: ... sofern die Zahl ueberhaupt vom Lead-Sensor stammt. Tut sie
    // das nicht, ist sie kein gemessenes Faktum ueber die Zone, sondern ein
    // Messwert der Sensoren, gegen die der Lead gesetzt wurde. Dann meldet
    // das Band einen eigenen Zustand MIT Quelle statt einer
    // Schwellenunterschreitung. Der Eintrag ersetzt die Kritisch-Meldung,
    // er kommt nicht zusaetzlich -- also kein neues Grundrauschen: sichtbar
    // wird er nur dort, wo vorher der Fehlalarm stand.
    if (leadAusgefallen(zone)) {
      return {
        zone,
        grund: 'lead_ausgefallen',
        text: `${leadAusfallDetail(zone)} Zonen-Zustand unbekannt.`,
      }
    }
    return {
      zone,
      grund: 'unter_kritisch',
      text: `kritische Schwelle ${kritisch}% unterschritten`,
    }
  }
  // T-0278: Prognose-getriebene Warnung NUR wenn die Engine zustimmt.
  // Vorher feuerte das Banner allein auf `prognose24h < schwelle_min`
  // (rohe ML), auch wenn die Engine `kein_bedarf`/`FEUCHTE_OK` sagte
  // (Fehlalarm waldblumen 30.05.: ML 33%, Engine kein_bedarf, Physik
  // ~7d Reserve). Wenn keine Empfehlung vorliegt (V0-Tab ohne Snapshot),
  // faellt der Prognose-Zweig komplett weg -- besser kein Alarm als
  // Fehlalarm.
  if (prognose24h != null && prognose24h < zone.feuchte_schwelle_min) {
    if (empf == null) {
      // Keine Engine-Info: rohe ML allein reicht nicht fuer KRITISCH.
      return null
    }
    const engineSiehtBedarf =
      empf.soll_bewaessern
      || empf.empfehlungs_typ === 'akut'
      || empf.empfehlungs_typ === 'praeventiv'
    if (!engineSiehtBedarf) {
      // ML-Prognose alarmiert, Engine widerspricht (z.B. FEUCHTE_OK).
      // Kein KRITISCH-Eintrag -- das ist genau der Fehlalarm-Fall.
      return null
    }
    return {
      zone,
      grund: 'prognose_unter_min',
      text: `ML sagt ${prognose24h.toFixed(0)}% in 24 h (unter ${zone.feuchte_schwelle_min}%)`,
      prognose24h,
    }
  }
  return null
}

/** T-0397 (F10b): Zone-IDs, die das KRITISCH-Band zeigt -- damit der Heute-
 *  Plan sie ausblenden kann (sonst dieselben Zonen doppelt direkt untereinander,
 *  "Schwelle unterschritten" = kein echter Tages-Plan). Single Source: gleiche
 *  `ermittleGefahr`-Logik wie die Band-Anzeige. */
export function ermittleKritischeZoneIds(
  zonen: Zone[],
  vorhersagen24h?: Record<string, number>,
  empfehlungProZone?: Record<string, GiessEmpfehlung>,
): Set<string> {
  const ids = new Set<string>()
  for (const zone of zonen) {
    if (ermittleGefahr(zone, vorhersagen24h?.[zone.zone_id], empfehlungProZone?.[zone.zone_id])) {
      ids.add(zone.zone_id)
    }
  }
  return ids
}
