/* ML-Badge fuer den Header.
 * T-0610 ML-11: unterscheidet ML / teilweise / Heuristik (Logik in
 * ml-badge-logik.ts), statt jedes geladene Modell als "ML aktiv" zu melden.
 * T-0610 ML-6: Persistenz-Warnung als Zusatz (Klasse `warnung`), nur wenn
 * eine ML-Prognose im Spiel ist (aktiv/teilweise/geladen). */

import type { MLPersistenzWarnung } from '../typen'
import { mlBadgeMitWarnung, mlBadgeZustand } from './ml-badge-logik'
import './MLStatusBadge.css'

interface Props {
  /** `ist_geladen` aus /api/ml/status; `null` = Status nicht abrufbar. */
  geladen: boolean | null
  /** zone_id -> prognose_quelle aus aktuellen Zonendaten (leer = unbekannt). */
  quellen: Record<string, string>
  /** zone_id -> Anzeigename fuer den Tooltip. */
  namen: Record<string, string>
  /** T-0610 ML-6: Modelle/Horizonte, die die Persistenz nicht schlagen. */
  persistenzWarnungen?: MLPersistenzWarnung[]
}

export function MLStatusBadge({ geladen, quellen, namen, persistenzWarnungen }: Props) {
  const z = mlBadgeMitWarnung(mlBadgeZustand(geladen, quellen, namen), geladen, persistenzWarnungen)
  return (
    <span className={`ml-badge ${z.klasse}${z.warnt ? ' warnung' : ''}`} title={z.titel}>
      <span className="ml-punkt" />
      {z.text}
    </span>
  )
}
