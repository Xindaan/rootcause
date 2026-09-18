/* Wetterkarte mit 24h-Vorhersage-Chart. */

import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  Legend, ResponsiveContainer,
} from 'recharts'
import type { Wetter } from '../typen'
import { windRichtungText } from '../hilfsfunktionen'
import './WetterKarte.css'

interface Props {
  wetter: Wetter | null
  titel?: string
}

export function WetterKarte({ wetter, titel }: Props) {
  if (!wetter) return null

  const chartDaten = wetter.stunden.slice(0, 24).map(s => ({
    zeit: new Date(s.zeitstempel).getTime(),
    temperatur: s.temperatur,
    niederschlag: s.niederschlag_mm,
    et0: s.et0_mm,
  }))

  // Ticks auf runde Stunden (alle 3h)
  const achsenTicks = (() => {
    if (chartDaten.length < 2) return undefined
    const erster = chartDaten[0].zeit
    const letzter = chartDaten[chartDaten.length - 1].zeit
    const startStunde = Math.ceil(erster / 3600_000) * 3600_000
    const ticks: number[] = []
    for (let t = startStunde; t <= letzter; t += 3 * 3600_000) {
      ticks.push(t)
    }
    return ticks
  })()

  const wind6h = wetter.stunden.slice(0, 6)
  const windRichtung = (() => {
    if (wind6h.length === 0) return null
    const sinSumme = wind6h.reduce((s, h) => s + Math.sin((h.wind_richtung_grad ?? 0) * Math.PI / 180), 0)
    const cosSumme = wind6h.reduce((s, h) => s + Math.cos((h.wind_richtung_grad ?? 0) * Math.PI / 180), 0)
    const mittel = Math.atan2(sinSumme, cosSumme) * 180 / Math.PI
    return Math.round((mittel + 360) % 360)
  })()

  // T-0241: aktive Wetter-Ereignisse (Frost/Hitze/Starkregen) als
  // Banner-Slot direkt unter der Ueberschrift. Backend liefert nur die
  // juengste pro Typ+Standort, deshalb max 3 Banner. Ohne diesen Block
  // sah man Frost-/Hitze-Warnungen nur im Ops-Tab -- Ops ist nicht
  // Default-Tab und User schaut typischerweise zuerst auf "Uebersicht".
  const ereignisse = wetter.aktive_ereignisse ?? []

  return (
    <div className="karte wetter-karte">
      <h2>{titel ?? 'Wetter'} (24h)</h2>
      {ereignisse.length > 0 && (
        <div className="wetter-ereignisse">
          {ereignisse.map((e, idx) => (
            <div
              key={`${e.typ}-${e.standort_id}-${idx}`}
              className={`wetter-ereignis wetter-ereignis--${e.typ}`}
              title={[
                e.details,
                e.beginn ? `Beginn: ${new Date(e.beginn).toLocaleString('de-DE', { dateStyle: 'short', timeStyle: 'short' })}` : null,
                e.ende ? `Ende: ${new Date(e.ende).toLocaleString('de-DE', { dateStyle: 'short', timeStyle: 'short' })}` : null,
              ].filter(Boolean).join(' · ')}
            >
              <span className="wetter-ereignis-icon" aria-hidden="true">
                {e.typ === 'frost' ? '❄' : e.typ === 'hitze' ? '☀' : '⛈'}
              </span>
              <span className="wetter-ereignis-label">
                {e.typ === 'frost'
                  ? 'Frostwarnung'
                  : e.typ === 'hitze'
                    ? 'Hitzewarnung'
                    : 'Starkregenwarnung'}
              </span>
              {e.details && (
                <span className="wetter-ereignis-details">— {e.details}</span>
              )}
            </div>
          ))}
        </div>
      )}
      <div className="karte-details">
        <div className="detail">
          <span className="label">Regen 6h</span>
          <span className="wert">{wetter.niederschlag_6h_mm.toFixed(1)} mm</span>
        </div>
        <div className="detail">
          <span className="label">Verdunstung 6h</span>
          <span className="wert">{wetter.et0_6h_mm.toFixed(2)} mm</span>
        </div>
        <div className="detail">
          <span className="label">Wind 6h</span>
          <span className="wert">{wetter.wind_6h_kmh?.toFixed(0) ?? '--'} km/h{windRichtung !== null ? ` ${windRichtungText(windRichtung)}` : ''}</span>
        </div>
      </div>
      {chartDaten.length > 0 && (
        <div className="chart-container">
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={chartDaten}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-gitter)" />
              <XAxis
                dataKey="zeit" type="number" scale="time"
                domain={['dataMin', 'dataMax']}
                ticks={achsenTicks}
                stroke="var(--text-gedaempft)" fontSize={11}
                tickFormatter={(ts: number) => new Date(ts).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })}
              />
              <YAxis yAxisId="temp" stroke="var(--chart-temperatur)" fontSize={11} label={{ value: '\u00B0C', position: 'insideTopLeft', fill: 'var(--chart-temperatur)', fontSize: 10 }} />
              <YAxis yAxisId="regen" orientation="right" stroke="var(--text-gedaempft)" fontSize={11} label={{ value: 'mm', position: 'insideTopRight', fill: 'var(--text-gedaempft)', fontSize: 10 }} />
              <Tooltip
                contentStyle={{
                  background: 'var(--bg-karte)',
                  border: '1px solid var(--rahmen)',
                  borderRadius: 10,
                  boxShadow: 'var(--karte-schatten-hover)',
                }}
                labelFormatter={(ts) => new Date(Number(ts)).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })}
              />
              <Legend wrapperStyle={{ fontSize: 11, paddingTop: 4 }} />
              <Line yAxisId="temp" type="monotone" dataKey="temperatur" stroke="var(--chart-temperatur)" dot={false} name="Temperatur (°C)" />
              <Line yAxisId="regen" type="monotone" dataKey="niederschlag" stroke="var(--chart-regen)" dot={false} name="Niederschlag (mm)" />
              <Line yAxisId="regen" type="monotone" dataKey="et0" stroke="var(--chart-et0)" dot={false} name="Verdunstung (mm)" strokeDasharray="4 2" />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  )
}
