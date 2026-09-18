/* Wasser-Bilanz pro Zone (T-0029): Zugefuehrt vs. Verdunstet im Zeitfenster.
 *
 * Liefert die Frage "wieviel Wasser geht rein, wieviel raus" als 3 KPIs.
 * Bilanz negativ = Defizit (Boden trocknet), positiv = Ueberschuss.
 * Wird nur gerendert, wenn die Zone eine konfigurierte Flaeche hat.
 */

import { useEffect, useState } from 'react'
import type { BilanzFenster, WasserBilanz, WasserBilanzFehler } from '../typen'
import { holeBilanz } from '../api'
import { istAbbruch } from '../hilfsfunktionen'
import { starteSichtbarkeitsIntervall } from '../sichtbarkeitsPolling'
import { bilanzHinweis } from './bilanz_frische'
import './WasserBilanz.css'

interface Props {
  zoneId: string
}

const FENSTER: BilanzFenster[] = ['24h', '7d', '30d']


function ist_bilanz(d: WasserBilanz | WasserBilanzFehler): d is WasserBilanz {
  return 'bilanz_liter' in d
}

function formatLiter(liter: number): string {
  // Bei < 10 L: eine Nachkommastelle, sonst gerundet auf ganze Liter
  if (Math.abs(liter) < 10) return `${liter.toFixed(1)} L`
  return `${Math.round(liter)} L`
}

function formatBilanz(liter: number): string {
  const prefix = liter > 0 ? '+' : ''
  return `${prefix}${formatLiter(liter)}`
}

/** Flaeche adaptiv: Topfgroessen (< 1 m²) zwei Nachkommastellen,
 *  sonst eine. Sonst zeigt 0.05 m² faelschlich als "0.1 m²".
 */
function formatFlaeche(m2: number): string {
  if (m2 < 1) return `${m2.toFixed(2)} m²`
  return `${m2.toFixed(1)} m²`
}

export function WasserBilanz({ zoneId }: Props) {
  const [fenster, setFenster] = useState<BilanzFenster>('24h')
  const [daten, setDaten] = useState<WasserBilanz | null>(null)
  const [verfuegbar, setVerfuegbar] = useState(true)
  const [refreshZaehler, setRefreshZaehler] = useState(0)

  useEffect(() => {
    const stoppeIntervall = starteSichtbarkeitsIntervall(() => setRefreshZaehler(z => z + 1), 60_000)
    return () => stoppeIntervall()
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    holeBilanz(zoneId, fenster, controller.signal)
      .then(antwort => {
        if (ist_bilanz(antwort)) {
          setDaten(antwort)
          setVerfuegbar(true)
        } else {
          setDaten(null)
          setVerfuegbar(false)
        }
      })
      .catch(err => {
        if (!istAbbruch(err)) {
          setDaten(null)
          setVerfuegbar(false)
        }
      })
    return () => controller.abort()
  }, [zoneId, fenster, refreshZaehler])

  if (!verfuegbar || daten === null) return null

  const bilanzKlasse =
    daten.bilanz_liter > 0 ? 'positiv'
    : daten.bilanz_liter < 0 ? 'negativ'
    : 'neutral'

  // T-0575: Herkunft der Zahlen, bevor sie gerendert werden.
  const hinweis = bilanzHinweis(daten)

  return (
    <section className="wasser-bilanz" title={hinweis.tooltip}>
      <header className="wb-kopf">
        <span className="wb-titel">Wasser-Bilanz</span>
        {/* T-0575: sichtbar statt nur im `title`. Ein Hover-Tooltip ist auf
            dem Touchscreen gar nicht erreichbar -- die Kachel sah dort aus
            wie eine gemessene Zahl. */}
        {hinweis.markieren && (
          <span className="wb-herkunft" title={hinweis.tooltip}>{hinweis.kurz}</span>
        )}
        <div className="wb-fenster" role="tablist">
          {FENSTER.map(f => (
            <button
              key={f}
              role="tab"
              aria-selected={f === fenster}
              className={`wb-fenster-btn ${f === fenster ? 'aktiv' : ''}`}
              onClick={() => setFenster(f)}
            >
              {f}
            </button>
          ))}
        </div>
      </header>

      <div className="wb-kpis">
        <div className="wb-kpi">
          <span className="wb-kpi-label">Zugeführt</span>
          <span className="wb-kpi-wert">{formatLiter(daten.zugefuehrt_liter)}</span>
          <small className="wb-kpi-detail">
            {formatLiter(daten.bewaesserung_liter)} Bew. + {formatLiter(daten.regen_liter)} Regen
          </small>
        </div>
        <div className="wb-kpi">
          <span className="wb-kpi-label">Verdunstet</span>
          <span className="wb-kpi-wert">{formatLiter(daten.verdunstet_liter)}</span>
          <small className="wb-kpi-detail">{formatFlaeche(daten.flaeche_m2)} &times; ET0</small>
        </div>
        <div className={`wb-kpi wb-bilanz ${bilanzKlasse}`}>
          <span className="wb-kpi-label">Bilanz</span>
          <span className="wb-kpi-wert">{formatBilanz(daten.bilanz_liter)}</span>
          <small className="wb-kpi-detail">
            {daten.bilanz_liter > 0 ? 'Überschuss' : daten.bilanz_liter < 0 ? 'Defizit' : 'ausgeglichen'}
          </small>
        </div>
      </div>

      {/* T-0201-Folge: Indikativ-Banner entfernt -- bei 24h ist Forecast
          strukturell (Fenster juenger als ERA5-Latenz), bei 7d/30d nur
          uebergangsweise. Die Info bleibt als Tooltip auf der Box (siehe
          QUELLE_LABEL). */}
    </section>
  )
}
