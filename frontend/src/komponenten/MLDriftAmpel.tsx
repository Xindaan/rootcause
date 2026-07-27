/* MLDriftAmpel — kompakte MAE-Anzeige pro Zone (T-0080d).
 *
 * Zeigt rollenden MAE pro Horizont (6h/12h/24h) gegen die Trainings-
 * Baseline mit Ampel:
 *   - gruen     : aktueller MAE <= 1.5x Baseline
 *   - rot       : aktueller MAE  > 1.5x Baseline (Modell driftet)
 *   - backlog   : Drift-Job hat offene Zeilen im Fenster, noch keine
 *                 Auswertung — typisch nach Backend-Offline-Phasen
 *   - keine_daten: nichts geloggt, nichts offen — Modell vermutlich neu
 *
 * Polling 5 Min (Drift-Job laeuft 1x/h, schnelleres Polling lohnt nicht).
 * Nutzt /api/ml/drift?zone_id=X&fenster=7d.
 */

import { useEffect, useState } from 'react'
import { holeMlDrift } from '../api'
import type { MlDriftAntwort, MlDriftHorizont } from '../typen'
import { istAbbruch } from '../hilfsfunktionen'
import './MLDriftAmpel.css'

interface Props {
  zoneId: string
}

const POLLING_INTERVALL_MS = 5 * 60_000

function ampelText(h: MlDriftHorizont): string {
  if (h.mae_aktuell !== null) return `${h.mae_aktuell.toFixed(1)} pp`
  if (h.ampel === 'backlog') return `${h.n_offen_im_fenster} offen`
  return '—'
}

/** Ratio aktuell/baseline -- erklaert die Ampel-Farbe (>1.5x = rot).
 *  Nur wenn beide Werte da sind, sonst null. */
function ampelRatio(h: MlDriftHorizont): string | null {
  if (h.mae_aktuell == null || h.mae_baseline == null || h.mae_baseline === 0) {
    return null
  }
  const r = h.mae_aktuell / h.mae_baseline
  return `${r.toFixed(1)}x`
}

function tooltip(h: MlDriftHorizont): string {
  const teile: string[] = []
  if (h.mae_aktuell !== null) {
    teile.push(`Aktueller MAE: ${h.mae_aktuell.toFixed(2)} pp (n=${h.n})`)
  }
  if (h.mae_baseline !== null) {
    teile.push(`Trainings-Baseline: ${h.mae_baseline.toFixed(2)} pp`)
  }
  if (h.ampel === 'rot') {
    teile.push('Modell driftet (>1.5x Baseline)')
  } else if (h.ampel === 'backlog') {
    teile.push(`${h.n_offen_im_fenster} Prognosen warten auf Auswertung`)
    if (h.letzte_evaluierung) {
      teile.push(`Letzte Auswertung: ${h.letzte_evaluierung.slice(0, 16).replace('T', ' ')}`)
    }
  } else if (h.ampel === 'keine_daten') {
    teile.push('Keine Drift-Daten im Fenster')
  }
  return teile.join('\n')
}

export function MLDriftAmpel({ zoneId }: Props) {
  const [drift, setDrift] = useState<MlDriftAntwort | null>(null)
  const [laedt, setLaedt] = useState(true)
  const [fehler, setFehler] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()

    async function laden() {
      try {
        const daten = await holeMlDrift(zoneId, 7, controller.signal)
        setDrift(daten)
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
  }, [zoneId])

  if (laedt) return null
  if (fehler || !drift || drift.horizonte.length === 0) return null

  return (
    <div className="ml-drift-ampel">
      <span className="label">Modell-Drift (7d)</span>
      <div className="horizonte">
        {drift.horizonte.map((h) => (
          <span
            key={h.horizont_h}
            className={`horizont ampel-${h.ampel}`}
            title={tooltip(h)}
          >
            <small>{h.horizont_h}h</small>
            {ampelText(h)}
            {ampelRatio(h) && <small className="ratio">{ampelRatio(h)}</small>}
          </span>
        ))}
      </div>
    </div>
  )
}
