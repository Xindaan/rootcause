/* T-0040: Top-5 Feature-Beitraege (SHAP) pro Horizont als aufklappbares Panel.
 *
 * On-demand: erst bei Klick auf den Toggle wird das Detail-Endpoint mit
 * `?details=top_features` abgefragt. Dashboard-Polling bleibt schlank.
 */

import { useEffect, useState } from 'react'
import type { FeatureBeitrag, MLVorhersage } from '../typen'
import { holeMLVorhersage } from '../api'
import { istAbbruch } from '../hilfsfunktionen'
import './MLAttributierung.css'

interface Props {
  zoneId: string
  horizonte: number[]
}

const FEATURE_LABELS: Record<string, string> = {
  boden_feuchte_aktuell: 'Aktuelle Feuchte',
  boden_feuchte_lag1h: 'Feuchte vor 1 h',
  boden_feuchte_lag6h: 'Feuchte vor 6 h',
  boden_feuchte_lag24h: 'Feuchte vor 24 h',
  boden_feuchte_diff_1h: 'Feuchte-Delta 1 h',
  boden_feuchte_diff_24h: 'Feuchte-Delta 24 h',
  boden_feuchte_rolling_mean_6h: 'Feuchte Mittel 6 h',
  boden_feuchte_rolling_std_6h: 'Feuchte Std 6 h',
  boden_temperatur: 'Bodentemperatur',
  stunde_des_tages: 'Stunde',
  tag_der_woche: 'Wochentag',
  letzte_bewaesserung_dauer_s: 'Letzte Bew.-Dauer',
  letzte_bewaesserung_vor_h: 'Letzte Bew. vor',
  wind_match: 'Wind-Exposition',
  ist_indoor: 'Indoor-Flag',
  ist_topf: 'Topf-Flag',
  flaeche_m2: 'Flaeche',
  quelle: 'Sensor-Quelle',
}

function label(name: string): string {
  if (FEATURE_LABELS[name]) return FEATURE_LABELS[name]
  // Dynamische Spalten wie niederschlag_summe_6h → "Niederschlag 6h"
  const hMatch = name.match(/^(.+?)_(?:summe|mittel|diff)_(\d+)h$/)
  if (hMatch) {
    const base = hMatch[1].replace(/_/g, ' ')
    return `${base.charAt(0).toUpperCase()}${base.slice(1)} ${hMatch[2]}h`
  }
  return name.replace(/_/g, ' ')
}

function formatWert(wert: number | null): string {
  if (wert === null) return '–'
  if (Math.abs(wert) >= 100) return wert.toFixed(0)
  if (Math.abs(wert) >= 10) return wert.toFixed(1)
  return wert.toFixed(2)
}

export function MLAttributierung({ zoneId, horizonte }: Props) {
  const [offen, setOffen] = useState(false)
  const [vorhersagen, setVorhersagen] = useState<Record<string, MLVorhersage> | null>(null)
  const [fehler, setFehler] = useState<string | null>(null)

  useEffect(() => {
    if (!offen || vorhersagen !== null) return
    const controller = new AbortController()
    holeMLVorhersage(zoneId, controller.signal, true)
      .then(d => { setVorhersagen(d) })
      .catch(e => {
        if (istAbbruch(e)) return
        setFehler(e instanceof Error ? e.message : String(e))
      })
    return () => controller.abort()
  }, [offen, zoneId, vorhersagen])

  // `ladend` abgeleitet — kein race zwischen Mikrotask-setState und Response-setState.
  const ladend = offen && vorhersagen === null && fehler === null

  return (
    <div className="ml-attr">
      <button
        type="button"
        className="ml-attr-toggle"
        aria-expanded={offen}
        onClick={() => setOffen(o => !o)}
      >
        {offen ? '▾' : '▸'} Erklaerung (Top-5 Features)
      </button>
      {offen && (
        <div className="ml-attr-panel">
          {ladend && <div className="ml-attr-status">Lade Beitraege…</div>}
          {fehler && <div className="ml-attr-fehler">Fehler: {fehler}</div>}
          {!ladend && !fehler && vorhersagen && horizonte.map(h => {
            const v = vorhersagen[`${h}h`]
            const tf = v?.top_features
            if (!tf || tf.length === 0) return null
            // Groesster absoluter Beitrag fuer Balken-Skalierung
            const max = Math.max(...tf.map(f => Math.abs(f.beitrag)))
            return (
              <div key={h} className="ml-attr-horizont">
                <div className="ml-attr-kopf">{h}h — Prognose {v.feuchte_prognose.toFixed(0)}%</div>
                <ul className="ml-attr-liste">
                  {tf.map((f: FeatureBeitrag) => {
                    const prozent = max > 0 ? Math.abs(f.beitrag) / max * 100 : 0
                    const klasse = f.beitrag >= 0 ? 'positiv' : 'negativ'
                    return (
                      <li key={f.name} className="ml-attr-zeile">
                        <div className="ml-attr-name" title={f.name}>
                          {label(f.name)}
                        </div>
                        <div className="ml-attr-balken-wrap">
                          <div
                            className={`ml-attr-balken ${klasse}`}
                            style={{ width: `${prozent}%` }}
                            aria-label={`Beitrag ${f.beitrag > 0 ? '+' : ''}${f.beitrag.toFixed(2)}%`}
                          />
                        </div>
                        <div className="ml-attr-wert">
                          <span className="ml-attr-beitrag">
                            {f.beitrag > 0 ? '+' : ''}{f.beitrag.toFixed(2)}%
                          </span>
                          <span className="ml-attr-featurewert">{formatWert(f.wert)}</span>
                        </div>
                      </li>
                    )
                  })}
                </ul>
              </div>
            )
          })}
          {!ladend && !fehler && vorhersagen && horizonte.every(h => !vorhersagen[`${h}h`]?.top_features) && (
            <div className="ml-attr-status">Keine Feature-Beitraege verfuegbar.</div>
          )}
        </div>
      )}
    </div>
  )
}
