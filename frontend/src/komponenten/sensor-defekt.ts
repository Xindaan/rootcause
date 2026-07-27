import type { Zone } from '../typen'

/** T-0258 (26.05.) / T-0390 (Single Source): 0.0%-Lesungen sind physikalisch
 *  verdaechtig (Sensor-Defekt, ausgesteckt, Substrat-Isolation). Ein Gardena-
 *  Sensor (oder Quelle unbekannt) mit exakt 0.0 gilt als unzuverlaessig und
 *  darf NICHT als echter -- gar kritischer -- Messwert behandelt werden.
 *  Realfall hecke 26.05.: 4 Tage konstant 0.0 -> faelschlich "kritisch 22%
 *  unterschritten", obwohl Sensor im Lager.
 *
 *  Zentral, damit KritischBand, V4-Karte (aktion-state-v4) und Triage-Strip
 *  dieselbe Wahrheit fuehren. Vorher lag der Guard NUR im KritischBand ->
 *  Karte/Strip rahmten "kritisch", Band schwieg = widerspruechliche Anzeige
 *  auf derselben Seite (Fable-Audit F5). */
export function istWahrscheinlichSensorDefekt(zone: Zone): boolean {
  return zone.aktuelle_feuchte === 0 && (zone.quelle === 'gardena' || zone.quelle == null)
}
