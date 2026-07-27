/* FeuchteRegimeBadge — T-0137 (H-4 Stufe 2).
 *
 * Rendert das aktuell aktive Feuchte-Regime einer Zone (Saison/Phase)
 * als kleines Info-Badge. Backend liefert `feuchte_regime_aktiv` als
 * Name + `feuchte_regime[]` als Liste der konfigurierten Regimes
 * (api_server.py:586). Bei Zonen ohne Regime-Konfig rendert die
 * Komponente NICHTS (Backward-Compat).
 */

import type { FeuchteRegime } from '../typen'
import './FeuchteRegimeBadge.css'

interface Props {
  regimes?: FeuchteRegime[]
  aktiv?: string | null
}

export function FeuchteRegimeBadge({ regimes, aktiv }: Props) {
  if (!aktiv || !regimes || regimes.length === 0) return null

  const aktivesRegime = regimes.find(r => r.name === aktiv)
  if (!aktivesRegime) return null

  const grund = aktivesRegime.grund?.trim() || null

  return (
    <div
      className="frb-badge"
      title="Aktives Feuchte-Regime (Saison/Phase) — Schwellen + Optimum stammen aus diesem Eintrag."
    >
      <span className="frb-label">Regime:</span>
      <span className="frb-name">{aktivesRegime.name}</span>
      {grund && <span className="frb-grund">({grund})</span>}
    </div>
  )
}

export default FeuchteRegimeBadge
