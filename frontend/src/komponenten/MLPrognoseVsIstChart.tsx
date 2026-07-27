/* MLPrognoseVsIstChart — Recharts Liniendiagramm "Prognose vs. Ist" (T-0080e).
 *
 * Pro Zone: zeigt fuer einen waehlbaren Horizont (6/12/24 h) die letzten
 * N evaluierten Prognosen. X-Achse = Zielzeit (wann der Wert eintraf),
 * Y-Achse = Feuchte %:
 *   - graues Band : q10..q90 (Modell-Konfidenz)
 *   - blaue Linie : q50 (Punktprognose)
 *   - gruene Linie: tatsaechlich gemessener Wert
 *
 * So wird Bias visuell:
 *   - Gardena-5-pp-Quantisierung → Ist-Linie zeigt Stufen
 *   - Modell-Drift              → Ist liegt systematisch ueber/unter q50
 *   - Saturierung               → Ist plateau, Prognose Mean-Reversion
 *   - Quantil-Crossing          → Band entartet
 *
 * On-demand: standardmaessig collapsed, expand per Klick. Beim ersten
 * Expand werden die Daten geholt + alle 5 Min refreshed waehrend offen.
 *
 * Nutzt /api/ml/drift/log (T-0080c) — verfuegbar erst nach Backend-Restart
 * mit dem Inspektor-Endpoint.
 */

import { useEffect, useState } from 'react'
import {
  ComposedChart, Line, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Legend,
} from 'recharts'
import { holeMlDriftLog } from '../api'
import type { MlDriftLogEintrag } from '../typen'
import { istAbbruch } from '../hilfsfunktionen'
import './MLPrognoseVsIstChart.css'

interface Props {
  zoneId: string
  /** T-0201-Folge: internen Toggle-Button ueberspringen, Chart direkt
   *  rendern. V2-Inspect nutzt einen aeusseren <details>-Toggle und
   *  will keinen doppelten Klick-Aufwand. V1 (ZonenKarteNeu) ruft
   *  ohne diese Prop -> Default-Verhalten unveraendert. */
  versteckeToggle?: boolean
}

type Horizont = 6 | 12 | 24

interface ChartPunkt {
  /* Zielzeit als Unix-ms (X-Achse) */
  ziel_ms: number
  /* Anzeige-Label fuer Tooltip */
  ziel_label: string
  prognose: number
  q10: number | null
  q90: number | null
  ist: number | null
  abweichung: number | null
  /* Hilfsfeld fuer Recharts Area-Renderer (0-Punkt = q10, Hoehe = q90-q10) */
  band_lower: number | null
  band_upper: number | null
}

const POLLING_INTERVALL_MS = 5 * 60_000

function eintragZuPunkt(e: MlDriftLogEintrag): ChartPunkt {
  const ziel = new Date(e.prognose_ziel_zeit)
  return {
    ziel_ms: ziel.getTime(),
    ziel_label: ziel.toLocaleString('de-DE', {
      day: '2-digit', month: '2-digit',
      hour: '2-digit', minute: '2-digit',
    }),
    prognose: Math.round(e.prognose_feuchte * 10) / 10,
    q10: e.prognose_q10,
    q90: e.prognose_q90,
    ist: e.ist_feuchte,
    abweichung: e.abweichung,
    band_lower: e.prognose_q10,
    band_upper: e.prognose_q90,
  }
}

function formatiereXTick(ms: number): string {
  const d = new Date(ms)
  return d.toLocaleString('de-DE', {
    day: '2-digit', month: '2-digit', hour: '2-digit',
  })
}

export function MLPrognoseVsIstChart({ zoneId, versteckeToggle }: Props) {
  const [offenLokal, setOffen] = useState(false)
  const offen = versteckeToggle ? true : offenLokal
  const [horizont, setHorizont] = useState<Horizont>(6)
  const [eintraege, setEintraege] = useState<MlDriftLogEintrag[] | null>(null)
  const [laedt, setLaedt] = useState(false)
  const [fehler, setFehler] = useState<string | null>(null)

  useEffect(() => {
    if (!offen) return
    const controller = new AbortController()

    async function laden() {
      setLaedt(true)
      try {
        const daten = await holeMlDriftLog(zoneId, horizont, 100, controller.signal)
        setEintraege(daten.eintraege)
        setFehler(null)
      } catch (err) {
        if (istAbbruch(err)) return
        setFehler(err instanceof Error ? err.message : 'Unbekannter Fehler')
      } finally {
        setLaedt(false)
      }
    }

    laden()
    const intervallId = window.setInterval(laden, POLLING_INTERVALL_MS)
    return () => {
      controller.abort()
      window.clearInterval(intervallId)
    }
  }, [offen, zoneId, horizont])

  if (!offen) {
    return (
      <button
        type="button"
        className="ml-prognose-chart-toggle"
        onClick={() => setOffen(true)}
        title="Liniendiagramm Prognose vs. tatsaechlich gemessener Wert"
      >
        📈 Prognose vs. Ist anzeigen
      </button>
    )
  }

  // Recharts braucht aufsteigend sortiert nach X-Achse
  const punkte: ChartPunkt[] = (eintraege ?? [])
    .map(eintragZuPunkt)
    .sort((a, b) => a.ziel_ms - b.ziel_ms)

  const hatBand = punkte.some((p) => p.q10 !== null && p.q90 !== null)
  const ohneIst = punkte.filter((p) => p.ist === null).length
  const mitIst = punkte.length - ohneIst

  return (
    <div className="ml-prognose-chart">
      <div className="ml-prognose-chart-header">
        <span className="titel">Prognose vs. Ist</span>
        <div className="horizont-tabs">
          {([6, 12, 24] as Horizont[]).map((h) => (
            <button
              key={h}
              type="button"
              className={horizont === h ? 'aktiv' : ''}
              onClick={() => setHorizont(h)}
            >
              {h}h
            </button>
          ))}
        </div>
        <button
          type="button"
          className="schliessen"
          onClick={() => setOffen(false)}
          aria-label="Diagramm schliessen"
        >
          ✕
        </button>
      </div>

      {laedt && punkte.length === 0 && (
        <div className="hinweis">Lade Daten…</div>
      )}
      {fehler && (
        <div className="hinweis fehler">
          Konnte nicht laden: {fehler}
          <br />
          <small>(Endpoint /api/ml/drift/log braucht Backend-Restart nach T-0080c)</small>
        </div>
      )}
      {!laedt && !fehler && punkte.length === 0 && (
        <div className="hinweis">
          Keine evaluierten Prognosen fuer Horizont {horizont}h.
          <br />
          <small>
            Nach Modell-Retrain dauert es {horizont}h bis erste Werte kommen.
          </small>
        </div>
      )}

      {punkte.length > 0 && (
        <>
          <div className="meta">
            n = {mitIst} evaluiert · {horizont}h-Horizont
          </div>
          <ResponsiveContainer width="100%" height={260}>
            <ComposedChart data={punkte} margin={{ top: 10, right: 16, bottom: 10, left: 0 }}>
              <CartesianGrid stroke="rgba(0,0,0,0.06)" strokeDasharray="3 3" />
              <XAxis
                dataKey="ziel_ms"
                type="number"
                domain={['dataMin', 'dataMax']}
                tickFormatter={formatiereXTick}
                stroke="var(--text-gedaempft)"
                fontSize={10}
                minTickGap={40}
              />
              <YAxis
                domain={[0, 100]}
                stroke="var(--text-gedaempft)"
                fontSize={11}
                tickCount={6}
              />
              <Tooltip
                labelFormatter={(label) => {
                  const ms = typeof label === 'number' ? label : Number(label)
                  return Number.isFinite(ms) ? formatiereXTick(ms) : ''
                }}
                formatter={(wert, name) => {
                  if (wert === null || wert === undefined) return ['—', name]
                  const num = typeof wert === 'number' ? wert : Number(wert)
                  if (!Number.isFinite(num)) return ['—', name]
                  return [`${num.toFixed(1)} %`, name]
                }}
                contentStyle={{ fontSize: 12 }}
              />
              {/* T-0397 (F16): Recharts faerbt den Legendentext mit der Serien-
                  farbe. Die Ist-Linie ist bewusst blass (rgba .18, "duenne
                  Hilfslinie" -- der Sensor-Wert steckt in den vollen Punkten),
                  aber dadurch war der Legendentext "Ist (Sensor)" mit 1.27:1
                  praktisch unsichtbar. Text hart auf lesbare Primaerfarbe
                  setzen; das farbige Serien-Symbol bleibt unveraendert. */}
              <Legend
                wrapperStyle={{ fontSize: 11 }}
                formatter={(wert) => (
                  <span style={{ color: 'var(--text-primaer)' }}>{wert}</span>
                )}
              />

              {hatBand && (
                <Area
                  type="monotone"
                  dataKey="band_upper"
                  stroke="none"
                  fill="rgba(124, 92, 191, 0.15)"
                  name="q90"
                  isAnimationActive={false}
                  legendType="none"
                />
              )}
              {hatBand && (
                <Area
                  type="monotone"
                  dataKey="band_lower"
                  stroke="none"
                  fill="rgba(255,255,255,1)"
                  name="q10"
                  isAnimationActive={false}
                  legendType="none"
                />
              )}

              <Line
                type="monotone"
                dataKey="prognose"
                stroke="var(--chart-ml, #7c5cbf)"
                strokeWidth={2}
                strokeDasharray="6 3"
                dot={false}
                name="Prognose q50"
                isAnimationActive={false}
              />
              {/* Ist als Streupunkte ohne Verbindungslinie — Sensor-
                  Messungen sind diskret, eine Volllinie zwischen weit
                  auseinander liegenden Punkten suggeriert einen
                  kontinuierlichen Verlauf, den es nicht gibt. So fallen
                  die Punkte direkt auf die Prognose-Strichlinie und
                  treffer-vs-daneben ist visuell klar lesbar. */}
              <Line
                type="monotone"
                dataKey="ist"
                stroke="rgba(46, 125, 50, 0.18)"
                strokeWidth={1}
                strokeDasharray="2 4"
                dot={{
                  r: 4,
                  fill: 'var(--chart-feuchte, #2e7d32)',
                  stroke: 'var(--chart-feuchte, #2e7d32)',
                  strokeWidth: 1.5,
                }}
                activeDot={{ r: 6 }}
                name="Ist (Sensor)"
                isAnimationActive={false}
                connectNulls={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
          <div className="legende-detail">
            Band q10–q90 = 80 % Modell-Konfidenz · Strichlinie = q50 (Prognose, kontinuierlich) ·
            Punkte = tatsächlich gemessener Sensor-Wert zur Zielzeit (diskret, dünne Hilfslinie nur zur Reihenfolge).
            Treffergüte: wie nah die Punkte an der Strichlinie liegen.
          </div>
        </>
      )}
    </div>
  )
}
