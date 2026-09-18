import type { Zone } from '../typen'
import { sensorAnzeigeName } from './feuchte_chart_helpers'

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
  if (zone.aktuelle_feuchte !== 0) return false
  // T-0502 (10.08.2026): das Urteil kommt jetzt vom Backend, weil es die
  // TRAJEKTORIE braucht und nicht nur den Wert. Entscheidend ist das Maximum
  // der letzten 24 h: ein Sprung aus gesundem Bereich (hecke im Mai, Sensor
  // lag im Lager -- max24h 30..100) ist ein Defekt, ein Abstieg ueber Tage
  // (magerwiese im Juli -- max24h 5..15) ein echter Messwert am Skalenende.
  //
  // Das laesst sich hier nicht nachrechnen: die 24-h-Historie steckt in der
  // DB, nicht im Zone-Dict. Eine eigene Regel waere eine zweite Wahrheit --
  // genau der Fehler, den der Kommentar oben fuer T-0390 beschreibt.
  if (zone.null_ist_defekt != null) return zone.null_ist_defekt
  // Aelteres Backend ohne das Feld: altes Verhalten (Wert 0 + Gardena/unbekannt).
  //
  // ACHTUNG (16.09.2026): das ist die durch T-0502
  // WIDERLEGTE Regel -- sie urteilt am Wert statt an der Trajektorie und
  // wuerde die magerwiese wieder als defekt melden (35 -> ... -> 0 ueber zwoelf
  // Tage ist ein echter Abstieg, kein Defekt). Der Zweig ist heute tot:
  // Backend und Frontend deployen als EIN Prozess (`start.sh`), das Feld ist
  // seit T-0502 immer dabei. Er greift nur, wenn `null_ist_defekt` unerwartet
  // fehlt -- und dann still und falsch.
  //
  // Entfernen (auf `return false` -- ohne Backend-Urteil kein Defektverdacht),
  // sobald sicher ist, dass kein Client mehr gegen ein Backend ohne das Feld
  // laeuft. Bewusst nicht in derselben Runde geaendert, weil es eine
  // Verhaltensaenderung ist und kein Kommentar.
  return zone.quelle === 'gardena' || zone.quelle == null
}

/** T-0532: die Zone hat einen konfigurierten Aggregat-Lead, `aktuelle_feuchte`
 *  stammt aber von einem anderen Sensor. Dann ist die Zahl KEINE Grundlage
 *  fuer eine Schwellen-Aussage -- der Lead wurde ja gerade deshalb gesetzt,
 *  weil die uebrigen Sensoren der Zone nicht belastbar sind (Cross-Spray
 *  T-0332, FYTA-Skalenbruch T-0385).
 *
 *  Realfall waldblumenhain 09.08.: Lead 40, FYTA 18/11 -> das Band meldete
 *  "kritische Schwelle 25% unterschritten" auf den 18ern. Der Lead funkt
 *  stuendlich, das Aggregat-Fenster war damals 90 min -- ein verpasster
 *  Beat genuegte, und der Backend-Fallback griff auf den Verarbeiter-Cache
 *  zurueck, der nur nach zone_id verschluesselt ist. Seit T-0476 ruft die
 *  Anzeige mit 240 min ab (Entscheidungs-Horizont); das Flag bleibt fuer
 *  echte Lead-Ausfaelle noetig.
 *
 *  Backend liefert das Flag fertig (`api_server._baue_zone_dict`); hier steht
 *  keine zweite Wahrheit, nur der gemeinsame Zugriff fuer KritischBand,
 *  V4-Karte und Triage-Strip. Genau diese drei liefen bei T-0390 schon einmal
 *  auseinander, weil der Guard nur an einer Stelle lag. */
export function leadAusgefallen(zone: Zone): boolean {
  return zone.lead_ausgefallen === true
}

/** T-0532: Klartextname eines Zonen-Sensors. Nutzt denselben Resolver wie das
 *  Chart, damit derselbe Sensor auf Karte, Band und Chart gleich heisst. */
export function sensorNameInZone(
  zone: Zone, geraetId: string | null | undefined,
): string {
  if (!geraetId) return 'unbekannter Quelle'
  const quelle = zone.sensoren?.find(s => s.geraet_id === geraetId)?.quelle ?? null
  return sensorAnzeigeName(geraetId, zone.sensor_namen?.[geraetId] ?? null, quelle)
}

/** T-0532 (b): "die Karte soll den Sensor zeigen, aus dem die Zahl stammt."
 *  Eine nackte Prozentzahl ist bei 40 / 18 / 11 in derselben Zone nicht
 *  interpretierbar. Ein Satz, geteilt von KritischBand und V4-Karte -- zwei
 *  Formulierungen desselben Zustands waeren wieder zwei Wahrheiten. */
export function leadAusfallDetail(zone: Zone): string {
  const wert = zone.aktuelle_feuchte
  const wertText = wert != null ? `die ${wert.toFixed(0)} %` : 'der angezeigte Wert'
  return (
    `Lead-Sensor ${sensorNameInZone(zone, zone.aggregat_lead_geraet)} ohne `
    + `aktuellen Wert — ${wertText} stammen von `
    + `${sensorNameInZone(zone, zone.feuchte_geraet_id)}.`
  )
}
