/* MLRetrainStatusKachel — T-0183 (Folge B3).
 *
 * Zeigt den Status des ML-Auto-Retrain-Jobs als kleine Kachel:
 *   - aktiv/inaktiv
 *   - letzter Lauf (Datum) + Ergebnis (uebernommen / abgelehnt / pro_cluster)
 *   - gate_faktor + intervall_tage (Kontext-Info, kein Polling-Driver)
 *
 * Backend-Quelle: `/api/ml/status` → `retrain`-Sub-Objekt (api_server.py:1494).
 * Bei nicht-konfiguriertem Retrain-Job (z. B. lokale Dev-Instanz)
 * rendert die Komponente nichts.
 */

import type { MLStatus, MLRetrainStatus } from '../typen'
import { datumZeitFormat } from '../hilfsfunktionen'
import './MLRetrainStatusKachel.css'

interface Props {
  status: MLStatus | null
}

function ergebnisLabel(letztes: MLRetrainStatus['letztes_ergebnis']): {
  text: string
  klasse: 'ok' | 'warn' | 'neutral'
} {
  if (!letztes || typeof letztes !== 'object') {
    return { text: 'noch kein Lauf', klasse: 'neutral' }
  }
  const status = (letztes as { status?: string }).status
  if (status === 'uebernommen') {
    return { text: 'übernommen (Gate bestanden)', klasse: 'ok' }
  }
  if (status === 'abgelehnt') {
    return { text: 'abgelehnt (Gate fehlgeschlagen)', klasse: 'warn' }
  }
  if (status === 'pro_cluster') {
    // Pro-Cluster-Lauf: zaehle uebernommen / abgelehnt.
    const cluster = (letztes as { cluster?: Record<string, { status: string }> }).cluster
    if (cluster) {
      const eintraege = Object.values(cluster)
      const uebernommen = eintraege.filter(c => c.status === 'uebernommen').length
      const abgelehnt = eintraege.filter(c => c.status === 'abgelehnt').length
      return {
        text: `Cluster: ${uebernommen} übernommen, ${abgelehnt} abgelehnt`,
        klasse: abgelehnt > 0 ? 'warn' : 'ok',
      }
    }
  }
  return { text: String(status ?? 'unbekannt'), klasse: 'neutral' }
}

export function MLRetrainStatusKachel({ status }: Props) {
  if (!status?.retrain) return null
  const r = status.retrain
  const ergebnis = ergebnisLabel(r.letztes_ergebnis)

  return (
    <div className="mrk-kachel" title="Auto-Retrain (T-0048): trainiert periodisch neue Modelle und ersetzt sie nur, wenn das Gate bestanden ist.">
      <div className="mrk-zeile-eins">
        <span className="mrk-titel">ML-Auto-Retrain</span>
        <span className={`mrk-status ${r.aktiv ? 'aktiv' : 'inaktiv'}`}>
          {r.aktiv ? 'aktiv' : 'aus'}
        </span>
      </div>
      <div className="mrk-zeile-zwei">
        <span className="mrk-label">Letzter Lauf:</span>
        <span className="mrk-wert">
          {r.letzter_lauf ? datumZeitFormat(r.letzter_lauf) : '—'}
        </span>
      </div>
      <div className={`mrk-zeile-zwei mrk-${ergebnis.klasse}`}>
        <span className="mrk-label">Ergebnis:</span>
        <span className="mrk-wert">{ergebnis.text}</span>
      </div>
      <div className="mrk-zeile-meta">
        <span>Gate-Faktor {r.gate_faktor.toFixed(2)}</span>
        <span>·</span>
        <span>Intervall {r.intervall_tage} Tage</span>
      </div>
    </div>
  )
}

export default MLRetrainStatusKachel
