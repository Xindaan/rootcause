/* T-0573: Segmentierung der Feuchte-Linie an Kalibrier-Fenstergrenzen.
 *
 * Bewusst OHNE React- und Recharts-Import: dieses Modul enthaelt reine
 * Datenlogik und ist damit in node ausfuehrbar. Ein Test, der nur den
 * Quelltext nach einem Muster durchsucht, wuerde hier gar nichts
 * beweisen -- die Frage ist, welche Spalten am Ende herauskommen.
 */
import type { AusschlussFenster, Messwert } from '../typen'
import type { SensorLinieMeta } from './feuchte_chart_helpers'

/** Pseudo-ID fuer Messwerte ohne `geraet_id` (siehe
 *  feuchte_chart_helpers). Hier gespiegelt, damit dieses Modul keine
 *  Laufzeit-Abhaengigkeit auf die Chart-Helfer braucht. */
const LEGACY_GERAET_ID = '_legacy'

/** T-0304: `sensor_kalibrierung` (Sensor-Werte unzuverlaessig) vs
 *  `event_ignore` (Sensor ok, nur Kanal-Events ignoriert, z.B.
 *  Magerwiese-Gras T-0300). undefined = sensor_kalibrierung, weil
 *  flaggen der sichere Default ist. */
export function istKalibrierFenster(f: AusschlussFenster): boolean {
  return (f.zweck ?? 'sensor_kalibrierung') === 'sensor_kalibrierung'
}

export function baueChartDatenProSensor<T extends { zeit: number }>(
  basisDaten: T[],
  messwerte: Messwert[],
  sensoren: SensorLinieMeta[],
  fenster?: AusschlussFenster[],
): (T & Record<string, number | null>)[] {
  // T-0573: pro Sensor ein Segment-Zaehler. Er springt genau dann, wenn
  // ein Punkt die Grenze eines `sensor_kalibrierung`-Fensters ueberquert
  // -- der Punkt landet dann in einer neuen Spalte, und weil Recharts nur
  // Punkte DESSELBEN dataKey verbindet, entsteht dort die Luecke.
  const segmentIdx: Record<string, number> = {}
  const letztesFenster: Record<string, AusschlussFenster | null | undefined> = {}

  const zeilen = basisDaten.map((punkt, i) => {
    const m = messwerte[i]
    const sensorWerte: Record<string, number | null> = {}
    const gid = m?.geraet_id ?? LEGACY_GERAET_ID
    for (const s of sensoren) {
      const trifft = gid === s.geraet_id
      let key = s.dataKey
      if (trifft) {
        const f = findeKalibrierFensterFuer(punkt.zeit, fenster, s.geraet_id)
        if (letztesFenster[s.geraet_id] !== undefined
            && letztesFenster[s.geraet_id] !== f) {
          segmentIdx[s.geraet_id] = (segmentIdx[s.geraet_id] ?? 0) + 1
        }
        letztesFenster[s.geraet_id] = f
        key = segmentDatenKey(s.dataKey, segmentIdx[s.geraet_id] ?? 0)
      }
      sensorWerte[key] = trifft ? (m?.boden_feuchte ?? null) : null
      // Basis-Spalte immer belegen, damit Tooltip und Single-Sensor-Area
      // dieselben Schluessel finden wie vor T-0573.
      if (!(s.dataKey in sensorWerte)) sensorWerte[s.dataKey] = null
    }
    return { ...punkt, ...sensorWerte } as T & Record<string, number | null>
  })

  return zeilen
}

/** T-0573: Spaltenname eines Linien-Segments. Segment 0 behaelt den
 *  Basis-Schluessel -- ohne Kalibrier-Fenster ist das Ergebnis damit
 *  identisch zu vor T-0573 (eine Spalte, eine durchgezogene Linie). */
export function segmentDatenKey(dataKey: string, idx: number): string {
  return idx === 0 ? dataKey : `${dataKey}__seg${idx}`
}

/** Das `sensor_kalibrierung`-Fenster, in dem ein Zeitpunkt fuer DIESEN
 *  Sensor liegt -- oder null.
 *
 *  T-0386: ein Fenster mit gesetzter `geraet_id` gilt nur fuer genau
 *  diesen Sensor. Ohne diese Pruefung wuerde das Maxibaer-Fenster (das
 *  nur `fyta_900001` meint) in einer Multi-Sensor-Zone auch die Linien
 *  der gesunden Nachbarsensoren zerschneiden.
 *
 *  `event_ignore`-Fenster schneiden NICHT (T-0304): dort ist der Sensor
 *  in Ordnung, nur seine Kanal-Events werden ignoriert.
 */
export function findeKalibrierFensterFuer(
  zeit_ms: number,
  fenster: AusschlussFenster[] | undefined,
  geraet_id: string,
): AusschlussFenster | null {
  for (const f of fenster ?? []) {
    if (!istKalibrierFenster(f)) continue
    if (f.geraet_id != null && f.geraet_id !== geraet_id) continue
    const von_ms = new Date(f.von).getTime()
    const bis_ms = new Date(f.bis).getTime()
    if (von_ms <= zeit_ms && zeit_ms <= bis_ms) return f
  }
  return null
}

/** T-0573: die ZUSAETZLICHEN Segment-Linien (Segment >= 1), die eine
 *  Komponente neben ihrer normalen Sensor-Linie rendern muss.
 *
 *  Gleiche Farbe und gleicher Name wie die Basis-Linie: die Aussage ist
 *  "hier ist die Reihe unterbrochen", nicht "hier ist eine andere
 *  Messgroesse". T-0407 hatte die frueher gedimmt-gepunktete Darstellung
 *  aus Lesbarkeitsgruenden entfernt; das gilt weiter.
 */
export function baueSegmentLinien(
  sensoren: SensorLinieMeta[],
  daten: Record<string, unknown>[],
): SensorLinieMeta[] {
  const vorhanden = new Set<string>()
  for (const zeile of daten) {
    for (const k of Object.keys(zeile)) {
      if (k.includes('__seg')) vorhanden.add(k)
    }
  }
  const extra: SensorLinieMeta[] = []
  for (const s of sensoren) {
    for (const key of vorhanden) {
      if (key.startsWith(`${s.dataKey}__seg`)) {
        extra.push({ ...s, dataKey: key })
      }
    }
  }
  return extra
}

