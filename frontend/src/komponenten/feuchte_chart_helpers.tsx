/**
 * T-0211c: Helper fuer Pro-Sensor-Feuchte-Linien im Chart.
 *
 * Hintergrund: `/api/zonen/{id}/messwerte` liefert eine flache Liste
 * aller Sensoren der Zone, sortiert nach Zeitstempel. Bei Multi-Sensor-
 * Zonen (z.B. waldblumenhain mit 1× Gardena + 2× FYTA Terra) werden
 * Werte verschiedener Sensoren chronologisch durch eine Recharts-Linie
 * verbunden -> wirkt wie wilde Feuchte-Spruenge.
 *
 * Fix: pro Sensor eine eigene `<Line>`. Die Helper hier bauen aus der
 * flachen Liste Pivot-Daten (eine Zeile pro Zeitpunkt, pro Sensor eine
 * Spalte). Recharts `connectNulls={true}` verbindet dann nur Punkte
 * desselben Sensors, andere Spalten bleiben `null`.
 *
 * Genutzt von ZonenKarte (V0), ZonenKarteNeu (V1) und
 * ZonenKarteV2Inspect (V2). Single Source of Truth (~/src/CLAUDE.md
 * `Plan-Delta-Capture` + globale Isomorphie-Regel).
 */
import type { ReactElement } from 'react'
import { ReferenceArea } from 'recharts'
import type { AusschlussFenster, Messwert, SensorEinzeln } from '../typen'

/** Stabiler Schluessel fuer einen Sensor im chartDaten-Objekt. */
export function sensorDatenKey(geraetId: string): string {
  // Recharts dataKey muss ein valider JS-Property-String sein. UUIDs
  // mit Bindestrichen sind ok, aber wir prefixen, damit es nicht mit
  // anderen Feldern (`feuchte`, `band`, `prognose`) kollidiert.
  return `feuchte__${geraetId}`
}

/** Pseudo-ID fuer Messwerte, die keine `geraet_id` haben (Legacy-
 * Backend ohne T-0211c-Patch oder DB-Eintraege aus alten WS-Bursts).
 * Damit alle Werte trotzdem in einer Linie landen. */
export const LEGACY_GERAET_ID = '_legacy'

/** Sammelt alle unique geraet_ids aus messwerte + zone.sensoren.
 *  `namenMap` (T-0221-Folge): geraet_id -> Klartextname fuer ALLE
 *  konfigurierten Sensoren -- loest auch Sensoren auf, die nicht in
 *  `zoneSensoren` (6h-Aktivliste) stehen, aber im Chart-Fenster noch
 *  Daten haben (FYTA-Cadence-Drift). */
function sammleSensoren(
  messwerte: Messwert[],
  zoneSensoren: SensorEinzeln[] | undefined,
  namenMap?: Record<string, string>,
): { geraet_id: string; quelle: string | null; name: string | null }[] {
  // Map fuer dedup + stabile Reihenfolge (Sensoren in Zone-Reihenfolge,
  // unbekannte hinten dran).
  const ergebnis = new Map<string, { quelle: string | null; name: string | null }>()

  for (const s of zoneSensoren ?? []) {
    ergebnis.set(s.geraet_id, {
      quelle: s.quelle ?? null,
      name: s.name ?? namenMap?.[s.geraet_id] ?? null,
    })
  }
  for (const m of messwerte) {
    const gid = m.geraet_id ?? LEGACY_GERAET_ID
    if (!ergebnis.has(gid)) {
      ergebnis.set(gid, { quelle: m.quelle ?? null, name: namenMap?.[gid] ?? null })
    }
  }
  // Falls gar keine Sensoren bekannt (z.B. komplett leere Messwerte-
  // Liste), liefere eine Pseudo-Reihe -- die Aufrufer rendern dann
  // trotzdem die Achsen / ReferenceAreas.
  if (ergebnis.size === 0) {
    ergebnis.set(LEGACY_GERAET_ID, { quelle: null, name: null })
  }
  return Array.from(ergebnis.entries()).map(([geraet_id, v]) => ({
    geraet_id, quelle: v.quelle, name: v.name,
  }))
}

/** Klartext-Name fuer Sensor — bevorzugt `sensor_namen`, sonst
 * gekuerzte Geraet-ID + Quelle-Suffix. */
export function sensorAnzeigeName(
  geraetId: string,
  name: string | null,
  quelle: string | null,
): string {
  if (name) return name
  const kurz = geraetId.length > 12 ? geraetId.slice(0, 6) + '…' : geraetId
  if (quelle) return `${kurz} (${quelle})`
  return kurz
}

/**
 * Recharts-CSS-Farben fuer pro-Sensor-Linien. Gardena = blaue
 * Standardfarbe (`--chart-feuchte`), FYTA-Sensoren in einer
 * abgegrenzten Palette. Index fuellt sich modulo, falls mehr Sensoren
 * als Farben.
 */
const FYTA_FARBEN = [
  'var(--chart-feuchte-fyta-1, #0ea5e9)',  // FYTA-A: cyan
  'var(--chart-feuchte-fyta-2, #06b6d4)',  // FYTA-B: teal
  'var(--chart-feuchte-fyta-3, #14b8a6)',  // FYTA-C: emerald
]

export function sensorStrich(quelle: string | null, fytaIdx: number): string {
  if (quelle === 'fyta') return FYTA_FARBEN[fytaIdx % FYTA_FARBEN.length]
  // Default: Gardena/unbekannt/IFTTT etc. nutzen die Haupt-Feuchte-Farbe.
  return 'var(--chart-feuchte)'
}

export interface SensorLinieMeta {
  geraet_id: string
  quelle: string | null
  name: string
  /** dataKey der Sensor-Linie. Traegt ALLE Messwerte des Sensors --
   *  auch die innerhalb eines `ausschluss_fenster`s (T-0407). */
  dataKey: string
  farbe: string
}

/** Liefert pro Sensor die Meta-Infos fuer eine Recharts-`<Line>`.
 *  `namenMap` (optional): geraet_id -> Klartextname aus `zone.sensor_namen`
 *  -- benannt auch stale Sensoren, die nicht mehr in `zoneSensoren`
 *  stehen. */
export function baueSensorLinienMeta(
  messwerte: Messwert[],
  zoneSensoren: SensorEinzeln[] | undefined,
  namenMap?: Record<string, string>,
): SensorLinieMeta[] {
  const sensoren = sammleSensoren(messwerte, zoneSensoren, namenMap)
  let fytaIdx = 0
  return sensoren.map(s => {
    const farbe = sensorStrich(s.quelle, fytaIdx)
    if (s.quelle === 'fyta') fytaIdx++
    return {
      geraet_id: s.geraet_id,
      quelle: s.quelle,
      name: sensorAnzeigeName(s.geraet_id, s.name, s.quelle),
      dataKey: sensorDatenKey(s.geraet_id),
      farbe,
    }
  })
}

/**
 * Baut Recharts-Pivot-Daten aus der flachen Messwert-Liste:
 * - eine Zeile pro Zeitstempel (jeder Sensor seinen eigenen Punkt)
 * - pro Sensor eine Spalte `feuchte__<geraet_id>` mit `number | null`
 * - bei `connectNulls={true}` verbindet Recharts nur Punkte mit
 *   demselben dataKey -> jede Sensor-Linie ist sauber, keine Misch-
 *   Linie.
 *
 * Fuer Single-Sensor-Zonen (1 geraet_id) ist das aequivalent zur
 * alten Logik mit `dataKey="feuchte"` — nur dass die Spalte jetzt
 * `feuchte__<id>` heisst.
 *
 * T-0407: Messwerte in einem `ausschluss_fenster` landen in DERSELBEN
 * Spalte wie alle anderen -- die Linie bleibt durchgezogen. Vorher
 * (T-0211d) wurden sie in eine zweite Spalte umgeleitet und gedimmt-
 * gepunktet gezeichnet; zusammen mit der vollflaechigen Einfaerbung
 * machte das lange Fenster unlesbar. Dass ein Fenster laeuft, sagt
 * jetzt allein die Annotations-Schiene (`baueAusschlussMarkierungen`).
 */
export function baueChartDatenProSensor<T extends { zeit: number }>(
  basisDaten: T[],
  messwerte: Messwert[],
  sensoren: SensorLinieMeta[],
): (T & Record<string, number | null>)[] {
  return basisDaten.map((punkt, i) => {
    const m = messwerte[i]
    const sensorWerte: Record<string, number | null> = {}
    const gid = m?.geraet_id ?? LEGACY_GERAET_ID
    for (const s of sensoren) {
      sensorWerte[s.dataKey] = gid === s.geraet_id ? (m?.boden_feuchte ?? null) : null
    }
    return { ...punkt, ...sensorWerte } as T & Record<string, number | null>
  })
}

/**
 * T-0410: Hat die Zone Sensoren aus MEHR ALS EINER Quelle (Gardena +
 * FYTA)? Dann sind die horizontalen Referenz-Linien (Min/Max/Welke)
 * mehrdeutig: sie sind gegen den **Gardena**-Sensor kalibriert
 * (`feuchte_schwelle_*` aus der Config, Welkepunkt aus dem Plateau-
 * Modell), werden aber quer ueber ALLE Sensor-Linien gezogen.
 *
 * Warum das nicht nur Kosmetik ist: Realfall waldblumenhain 16.07. --
 * Gardena liest 80, die beiden FYTA lesen 23 und 29, und EINE
 * Schwellenlinie 40-55 laeuft quer darueber. Die FYTA-Linien sehen
 * damit dramatisch "unter Minimum" aus, obwohl die Schwelle fuer sie
 * gar nicht gilt. Die beiden Skalen sind nicht ineinander umrechenbar
 * (Mapping-Tabelle leer, Korrelation ~0 -- s. T-0410), also kann die
 * UI das nur KENNZEICHNEN, nicht aufloesen.
 *
 * Nur bei gemischten Quellen markieren: in reinen Gardena-Zonen (die
 * Mehrheit) waere der Zusatz sinnloses Rauschen.
 */
export function hatGemischteQuellen(sensoren: SensorLinieMeta[]): boolean {
  const quellen = new Set(sensoren.map(s => s.quelle ?? 'gardena'))
  return quellen.size > 1
}

/** Sensoren, fuer die die Gardena-kalibrierten Schwellen NICHT gelten
 *  (alles ausser Gardena/Legacy). Fuer den Legenden-Hinweis. */
export function fremdSkalenSensoren(sensoren: SensorLinieMeta[]): SensorLinieMeta[] {
  return sensoren.filter(s => s.quelle != null && s.quelle !== 'gardena')
}

/**
 * Liefert das `ausschluss_fenster`, in dem ein Zeitstempel liegt --
 * oder null. Traegt den `grund`-Text, den der Tooltip zeigt.
 */
export function findeAusschluss(
  zeit_ms: number,
  fenster: AusschlussFenster[] | undefined,
): AusschlussFenster | null {
  for (const f of fenster ?? []) {
    const von_ms = new Date(f.von).getTime()
    const bis_ms = new Date(f.bis).getTime()
    if (von_ms <= zeit_ms && zeit_ms <= bis_ms) return f
  }
  return null
}

/** T-0304: `sensor_kalibrierung` (Sensor-Werte unzuverlaessig) vs
 *  `event_ignore` (Sensor ok, nur Kanal-Events ignoriert, z.B.
 *  Magerwiese-Gras T-0300). undefined = sensor_kalibrierung, weil
 *  flaggen der sichere Default ist. */
export function istKalibrierFenster(f: AusschlussFenster): boolean {
  return (f.zweck ?? 'sensor_kalibrierung') === 'sensor_kalibrierung'
}

export function ausschlussLabel(f: AusschlussFenster): string {
  return istKalibrierFenster(f) ? 'Kalibrierung' : 'Events ignoriert'
}

export function ausschlussFarbe(f: AusschlussFenster): string {
  return istKalibrierFenster(f)
    ? 'var(--farbe-warnung, #f59e0b)'
    : 'var(--farbe-gesund, #2d8a4e)'
}

/** Hoehe der Annotations-Schiene in Y-Datenkoordinaten (Achse 0-100),
 *  also ~3 % der Chart-Hoehe am oberen Rand.
 *
 *  Oben statt unten, weil der untere Rand deutlich staerker belegt ist:
 *  tote/stuck Sensoren liegen dauerhaft auf 0 (Klasse A/D, s. Memory
 *  `gardena_sensor_kalibrierung`), und Trockenphasen laufen gegen 0.
 *  Oben kollidiert nur kurzzeitige Vollsaettigung -- die gibt es real
 *  (Einschlemmen treibt den Gardena-Index auf 100, verifiziert am
 *  waldblumenhain-Peak 09.07.), sie ist aber selten und kurz, und die
 *  Sensor-Linien werden NACH der Schiene gezeichnet, liegen also
 *  obenauf -- die Schiene verdeckt keinen Messwert.
 *
 *  (Hier stand urspruenglich "FYTA (VWC 0-65) erreicht den Bereich nie".
 *  Falsch: FYTA liefert real bis 100, 25 % der Werte liegen ueber 65 --
 *  DB-gemessen, s. T-0410. Fuer die Rail-Seite aendert das nichts, die
 *  Begruendung steht auf der Belegungs-Dichte unten vs oben.) */
const SCHIENE_VON = 97
const SCHIENE_BIS = 100

/**
 * T-0407: `ausschluss_fenster` als schmale Annotations-Schiene am
 * oberen Chart-Rand, statt die ganze Fenster-Flaeche einzufaerben.
 *
 * Warum: die Info "hier lief eine Kalibrierung" ist eine Meta-
 * Annotation -- sie darf die Daten nicht unlesbar machen. Vorher
 * (T-0211b) skalierte der Schaden mit der Fensterlaenge: beim
 * 2-Wochen-Fenster aus T-0385 war der ganze sichtbare Chart-Bereich
 * zu. Das erzeugte einen falschen Anreiz, Fenster kuenstlich kurz zu
 * halten statt sie fachlich richtig zu setzen. Die Schiene ist
 * laengen-unabhaengig: ein 2-Wochen-Fenster kostet exakt so viel
 * Lesbarkeit wie ein 2-Stunden-Fenster, naemlich keine.
 *
 * Geteilt von ZonenKarte (V0) und ZonenKarteV4Inspect (V4) -- Single
 * Source of Truth, sonst driften die beiden Overlays auseinander.
 */
export function baueAusschlussMarkierungen(
  fenster: AusschlussFenster[] | undefined,
  chartMin: number,
  chartMax: number,
): ReactElement[] {
  const markierungen: ReactElement[] = []
  ;(fenster ?? []).forEach((f, idx) => {
    const von_ms = new Date(f.von).getTime()
    const bis_ms = new Date(f.bis).getTime()
    // Fenster komplett ausserhalb des sichtbaren Zeitraums -> nichts.
    if (bis_ms < chartMin || von_ms > chartMax) return
    const farbe = ausschlussFarbe(f)
    markierungen.push(
      <ReferenceArea
        key={`ausschluss-${idx}`}
        x1={Math.max(von_ms, chartMin)}
        x2={Math.min(bis_ms, chartMax)}
        y1={SCHIENE_VON}
        y2={SCHIENE_BIS}
        fill={farbe}
        fillOpacity={0.5}
        stroke="none"
        ifOverflow="visible"
        label={{
          value: ausschlussLabel(f),
          fill: farbe,
          fontSize: 9,
          position: 'bottom',
        }}
      />,
    )
  })
  return markierungen
}

/** T-0221-Folge: gemeinsamer X-Achsen-Tick-Renderer fuer alle vier
 *  Chart-Komponenten (V0/V1/V2/V3). Die alte 1-zeilige Variante
 *  "DD.MM. HH:MM" ueberlappte sich bei 6 Ticks im schmalen Chart
 *  (User-Feedback). Drei Stufen nach Chart-Spannweite:
 *
 *    > 168 h (>7 d) : nur Datum, 1-zeilig.
 *    >  48 h (2-7 d): Datum + Uhrzeit, 2-zeilig (verhindert Ueberlappung).
 *    <= 48 h        : nur Uhrzeit, 1-zeilig.
 *
 *  Zusaetzlich: erster Tick `text-anchor=start`, letzter `=end`, sonst
 *  `middle` -- so wird der Text nicht am Chart-Rand beschnitten.
 *
 *  `tick`-Prop des `<XAxis>` braucht extra Hoehe gegenueber Recharts-
 *  Default (~30 px); 36 px reichen fuer die 2-zeilige Variante.
 *  Aufrufer sollte `height={36}` setzen.
 */
export function baueXTickRenderer(spanneStunden: number) {
  return (props: {
    x?: string | number
    y?: string | number
    payload?: { value: number }
    index?: number
    visibleTicksCount?: number
  }) => {
    if (!props.payload) return null
    const x = Number(props.x) || 0
    const y = Number(props.y) || 0
    const i = props.index ?? 0
    const n = props.visibleTicksCount ?? 0
    const anchor: 'start' | 'middle' | 'end' =
      i === 0 ? 'start' : (n > 1 && i === n - 1) ? 'end' : 'middle'
    const d = new Date(props.payload.value)
    const datum = d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' })
    const zeit = d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
    const gem = { textAnchor: anchor, fill: 'var(--text-gedaempft)', fontSize: 10 }
    if (spanneStunden > 168) {
      return (
        <g transform={`translate(${x},${y})`}>
          <text dy={12} {...gem}>{datum}</text>
        </g>
      )
    }
    if (spanneStunden > 48) {
      return (
        <g transform={`translate(${x},${y})`}>
          <text {...gem}>
            <tspan x={0} dy={11}>{datum}</tspan>
            <tspan x={0} dy={11}>{zeit}</tspan>
          </text>
        </g>
      )
    }
    return (
      <g transform={`translate(${x},${y})`}>
        <text dy={12} {...gem}>{zeit}</text>
      </g>
    )
  }
}

