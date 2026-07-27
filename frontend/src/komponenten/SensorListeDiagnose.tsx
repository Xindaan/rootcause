/* SensorListeDiagnose — T-0179c Folge.
 *
 * Multi-Sensor-Diagnose pro Zone. Backend liefert `Zone.sensoren[]`
 * mit allen Sensoren der Zone (api_server.py:606). Bei Single-Sensor-
 * Zonen rendert die Komponente NICHTS (waere redundant zur Hauptanzeige).
 * Bei Multi-Sensor-Zonen wie Waldblumenhain (FYTA Terra + Gardena)
 * zeigt sie pro Sensor: Quelle, ID-Kurzform, Feuchte, Temperatur, Zeit.
 *
 * Default eingeklappt — Diagnose-Werkzeug, nicht primaere Sicht.
 */

import { useState } from 'react'
import type { SensorEinzeln } from '../typen'
import { datumZeitFormat } from '../hilfsfunktionen'
import './SensorListeDiagnose.css'

interface Props {
  sensoren?: SensorEinzeln[]
}

function quelleLabel(quelle: string | null): string {
  if (!quelle) return '—'
  if (quelle === 'gardena') return 'Gardena'
  if (quelle === 'fyta') return 'FYTA'
  return quelle
}

function geraeteIdKurz(id: string): string {
  // Gardena-/FYTA-IDs sind UUID-aehnlich lang. Kuerze auf 8 + … fuer
  // Platz in der Tabelle, full id als title.
  if (id.length <= 12) return id
  return id.slice(0, 8) + '…'
}

/** T-0204: Klartextname falls vorhanden, sonst gekuerzte ID. */
function geraetLabel(s: SensorEinzeln): string {
  if (s.name && s.name.trim()) return s.name
  return geraeteIdKurz(s.geraet_id)
}

export function SensorListeDiagnose({ sensoren }: Props) {
  const [offen, setOffen] = useState(false)
  if (!sensoren || sensoren.length <= 1) return null

  return (
    <div className="sld-container">
      <button
        type="button"
        className="sld-toggle"
        onClick={() => setOffen(o => !o)}
        aria-expanded={offen}
      >
        {offen ? '▾' : '▸'} {sensoren.length} Einzel-Sensoren
      </button>
      {offen && (
        <table className="sld-tabelle">
          <thead>
            <tr>
              <th>Quelle</th>
              <th>Gerät</th>
              <th>Feuchte</th>
              <th>Temp</th>
              <th>Zeit</th>
            </tr>
          </thead>
          <tbody>
            {sensoren.map(s => (
              <tr key={s.geraet_id}>
                <td>{quelleLabel(s.quelle)}</td>
                <td title={s.geraet_id}>{geraetLabel(s)}</td>
                <td className="sld-num">
                  {s.boden_feuchte !== null
                    ? `${s.boden_feuchte.toFixed(0)}%`
                    : '—'}
                </td>
                <td className="sld-num">
                  {s.boden_temperatur !== null
                    ? `${s.boden_temperatur.toFixed(1)}°C`
                    : '—'}
                </td>
                <td className="sld-zeit">{datumZeitFormat(s.zeitstempel)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

export default SensorListeDiagnose
