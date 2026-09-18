/* MLAttributierungNeu — T-0040 SHAP-Attribution mit Klartext-Labels.
 *
 * Unterschied zur Klassik:
 *   - Klartext-Label oben (ohne Raw-Key), Raw-Key nur als title-Attribut
 *   - Fallback: wenn Label fehlt -> Raw-Key in Monospace (neue Features brechen nicht)
 *   - Bar mit expliziter Nulllinie, + nach rechts (--farbe-info), - nach links (--farbe-warnung)
 *   - Wert rechts, 2 Nachkommastellen, farbkodiert nach Vorzeichen
 *   - Legende pro Horizont "+ erhöht / − senkt"
 */

import { useEffect, useState } from 'react'
import type { FeatureBeitrag, MLVorhersage } from '../typen'
import { holeMLVorhersage } from '../api'
import { istAbbruch } from '../hilfsfunktionen'
import { featureLabel } from '../konstanten/featureLabels'
import './MLAttributierungNeu.css'

interface Props {
  zoneId: string
  horizonte: number[]
  /** T-0198/T-0200: Vom Parent vorgeladene ML-Vorhersagen mit
   *  `top_features` (z.B. aus dem Dashboard-Snapshot mit
   *  `ml_details=true`). Wenn gesetzt, springt der Lazy-Load-Effekt
   *  nicht an und die Erklaerung ist beim Aufklappen sofort sichtbar.
   *  Default-Verhalten (Lazy-Load on toggle) bleibt fuer V1-Aufrufer
   *  (ZonenKarteNeu) unveraendert. */
  vorhersagenPropagiert?: Record<string, MLVorhersage>
  /** T-0201-Folge: Den inneren Toggle-Button verstecken und das Panel
   *  immer zeigen. V2-Inspect packt die Komponente in ein <details>-
   *  Element und nutzt dessen <summary> als Toggle -- doppelte
   *  Toggle-Schicht waere redundant. */
  versteckeToggle?: boolean
}

function formatWert(wert: number | null): string {
  if (wert === null) return '–'
  if (Math.abs(wert) >= 100) return wert.toFixed(0)
  if (Math.abs(wert) >= 10) return wert.toFixed(1)
  return wert.toFixed(2)
}

interface ZeileProps {
  feature: FeatureBeitrag
  maxAbs: number
}

function AttrZeile({ feature, maxAbs }: ZeileProps) {
  const label = featureLabel(feature.name)
  const isNegativ = feature.beitrag < 0
  const prozent = maxAbs > 0 ? Math.abs(feature.beitrag) / maxAbs * 50 : 0
  return (
    <li className={`man-zeile ${isNegativ ? 'negativ' : 'positiv'}`}>
      <div className="man-name" title={feature.name}>
        {label ? label : <code className="man-raw">{feature.name}</code>}
        <span className="man-featurewert">{formatWert(feature.wert)}</span>
      </div>
      <div className="man-bar-wrap" aria-hidden>
        <div className="man-bar-track">
          <div className="man-bar-nulllinie" />
          {isNegativ ? (
            <div
              className="man-bar man-bar-negativ"
              style={{ right: '50%', width: `${prozent}%` }}
            />
          ) : (
            <div
              className="man-bar man-bar-positiv"
              style={{ left: '50%', width: `${prozent}%` }}
            />
          )}
        </div>
      </div>
      <div className={`man-wert ${isNegativ ? 'negativ' : 'positiv'}`}>
        {isNegativ ? '−' : '+'}{Math.abs(feature.beitrag).toFixed(2)}
      </div>
    </li>
  )
}

export function MLAttributierungNeu({ zoneId, horizonte, vorhersagenPropagiert, versteckeToggle }: Props) {
  // T-0201-Folge: bei versteckeToggle wird das Panel "immer offen"
  // behandelt -- Parent (z.B. V2-Inspect mit <details>) kontrolliert
  // die Sichtbarkeit selbst.
  const [offenLokal, setOffen] = useState(false)
  const offen = versteckeToggle ? true : offenLokal
  const [vorhersagenLokal, setVorhersagenLokal] = useState<Record<string, MLVorhersage> | null>(null)
  const [fehler, setFehler] = useState<string | null>(null)

  // T-0198/T-0200: wenn Parent propagiert hat, kein Lazy-Fetch -- Erklaerung
  // erscheint sofort beim Aufklappen.
  const vorhersagen = vorhersagenPropagiert ?? vorhersagenLokal

  useEffect(() => {
    if (vorhersagenPropagiert !== undefined) return
    if (!offen || vorhersagenLokal !== null) return
    const controller = new AbortController()
    holeMLVorhersage(zoneId, controller.signal, true)
      .then(d => setVorhersagenLokal(d))
      .catch(e => {
        if (istAbbruch(e)) return
        setFehler(e instanceof Error ? e.message : String(e))
      })
    return () => controller.abort()
  }, [offen, zoneId, vorhersagenLokal, vorhersagenPropagiert])

  const ladend = offen && vorhersagen === null && fehler === null

  return (
    <div className="man-container">
      {!versteckeToggle && (
        <button
          type="button"
          className="man-toggle"
          aria-expanded={offen}
          onClick={() => setOffen(o => !o)}
        >
          {offen ? '▾' : '▸'} Erklärung (Top-5 Features)
        </button>
      )}
      {offen && (
        <div className="man-panel">
          {ladend && <div className="man-status">Lade Beiträge…</div>}
          {fehler && <div className="man-fehler">Fehler: {fehler}</div>}
          {!ladend && !fehler && vorhersagen && horizonte.map(h => {
            const v = vorhersagen[`${h}h`]
            const tf = v?.top_features
            if (!tf || tf.length === 0) return null
            const max = Math.max(...tf.map(f => Math.abs(f.beitrag)))
            return (
              <div key={h} className="man-horizont">
                <div className="man-kopf">
                  <span className="man-kopf-titel">{h}h · Prognose {v.feuchte_prognose.toFixed(0)}%</span>
                  <span className="man-legende">
                    <span className="man-legende-eintrag positiv">+ erhöht</span>
                    <span className="man-legende-sep">·</span>
                    <span className="man-legende-eintrag negativ">− senkt</span>
                  </span>
                </div>
                <ul className="man-liste">
                  {tf.map(f => (
                    <AttrZeile key={f.name} feature={f} maxAbs={max} />
                  ))}
                </ul>
              </div>
            )
          })}
          {!ladend && !fehler && vorhersagen && horizonte.every(h => !vorhersagen[`${h}h`]?.top_features) && (
            <div className="man-status">Keine Feature-Beiträge verfügbar.</div>
          )}
        </div>
      )}
    </div>
  )
}
