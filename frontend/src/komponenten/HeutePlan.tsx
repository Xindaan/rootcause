/* HeutePlan — Liste heute erwarteter Bewässerungs-Events mit ML-Confidence.
 *
 * Nutzt `prognosen` (Bewaesserungs-Erwartung aus /api/prognose) und
 * optional die ML-24h-Vorhersage, um Konfidenz darzustellen:
 *   - "hoch"   wenn q10/q90 eng (Spanne <= 10 pp)
 *   - "mittel" wenn Spanne <= 25 pp
 *   - "niedrig" sonst
 *
 * UX-Review 2026-04-20.
 */

import type { Prognose, Zone, MLVorhersage } from '../typen'
import { datumZeitFormat } from '../hilfsfunktionen'
import './HeutePlan.css'

interface Props {
  zonen: Zone[]
  prognosen: Prognose[]
  vorhersagenProZone?: Record<string, Record<string, MLVorhersage>>
  onZoneClick?: (zoneId: string) => void
  // T-0397 (F10b): Zonen, die schon das KRITISCH-Band zeigt, hier ausblenden --
  // sonst stehen dieselben Zonen doppelt direkt untereinander. Heute-Plan zeigt
  // dann nur noch das ZUSAETZLICHE (Bedarf heute, aber noch nicht kritisch).
  ausblendenZoneIds?: Set<string>
}

type Konfidenz = 'hoch' | 'mittel' | 'niedrig'

function istHeute(iso: string): boolean {
  const d = new Date(iso)
  const jetzt = new Date()
  return d.getFullYear() === jetzt.getFullYear()
    && d.getMonth() === jetzt.getMonth()
    && d.getDate() === jetzt.getDate()
}

function klassifiziereKonfidenz(q10?: number, q90?: number): Konfidenz | null {
  if (q10 == null || q90 == null) return null
  const spanne = Math.abs(q90 - q10)
  if (spanne <= 10) return 'hoch'
  if (spanne <= 25) return 'mittel'
  return 'niedrig'
}

export function HeutePlan({ zonen, prognosen, vorhersagenProZone, onZoneClick, ausblendenZoneIds }: Props) {
  const events = prognosen
    .filter(p => p.bewaesserung_erwartet && istHeute(p.bewaesserung_erwartet))
    .filter(p => !ausblendenZoneIds?.has(p.zone_id))
    .map(p => {
      const zone = zonen.find(z => z.zone_id === p.zone_id)
      const v = vorhersagenProZone?.[p.zone_id]?.['24h']
      return {
        prognose: p,
        zone,
        vorhersage: v,
        konfidenz: klassifiziereKonfidenz(v?.q10, v?.q90),
      }
    })
    .sort((a, b) => {
      const ta = a.prognose.bewaesserung_erwartet ?? ''
      const tb = b.prognose.bewaesserung_erwartet ?? ''
      return ta.localeCompare(tb)
    })

  // T-0397 (F10b): leeren Heute-Plan NICHT als leere Kopfzeile rendern -- das
  // war reiner Overhead ganz oben (Andre 11.07.). Nach Dedup gegen das KRITISCH-
  // Band bleibt oft nichts uebrig -> Block ganz weglassen, Real Estate sparen.
  if (events.length === 0) return null

  return (
    <section className="heute-plan" aria-label="Heute-Plan">
      <header className="heute-kopf">
        <span className="heute-titel">Heute</span>
        <span className="heute-zaehler">
          {`${events.length} Event${events.length === 1 ? '' : 's'}`}
        </span>
      </header>
      {events.length > 0 && (
        /* T-0397 (F10b-3): Kacheln nebeneinander statt Full-Width-Zeilen -- wie
           KritischBand, loest den leeren Mittelraum auf. */
        <div className="heute-grid">
          {events.map(e => (
            <button
              key={e.prognose.zone_id}
              type="button"
              className="heute-kachel"
              onClick={() => onZoneClick?.(e.prognose.zone_id)}
              title={e.prognose.begruendung}
            >
              <div className="hk-kopf">
                <span className="heute-zeit">
                  {e.prognose.bewaesserung_erwartet
                    ? datumZeitFormat(e.prognose.bewaesserung_erwartet).split(' ').pop()
                    : '--'}
                </span>
                <span className="hk-zone">{e.zone?.name ?? e.prognose.name}</span>
                {e.vorhersage && (
                  <span
                    className="hk-wert"
                    title={`ML-Prognose der Boden-Feuchte in 24 h: ${e.vorhersage.feuchte_prognose.toFixed(0)}%${
                      e.vorhersage.q10 != null && e.vorhersage.q90 != null
                        ? ` (wahrscheinlich zwischen ${e.vorhersage.q10.toFixed(0)} und ${e.vorhersage.q90.toFixed(0)}%)`
                        : ''}`}
                  >
                    {e.vorhersage.feuchte_prognose.toFixed(0)}%
                  </span>
                )}
              </div>
              <div className="hk-grund">{e.prognose.begruendung}</div>
              {(e.vorhersage?.q10 != null && e.vorhersage?.q90 != null) || e.konfidenz ? (
                <div className="hk-meta">
                  {e.vorhersage?.q10 != null && e.vorhersage?.q90 != null && (
                    <span className="heute-band">
                      {e.vorhersage.q10.toFixed(0)}–{e.vorhersage.q90.toFixed(0)}
                    </span>
                  )}
                  {e.konfidenz && (
                    <span className={`heute-konf heute-konf-${e.konfidenz}`}>
                      {e.konfidenz === 'hoch' ? 'hohe' : e.konfidenz === 'mittel' ? 'mittlere' : 'niedrige'} Konfidenz
                    </span>
                  )}
                </div>
              ) : null}
            </button>
          ))}
        </div>
      )}
    </section>
  )
}
