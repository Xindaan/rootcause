/* Einklappbares Entscheidungsprotokoll — zeigt die letzten Bewaesserungsentscheidungen. */

import { useEffect, useState } from 'react'
import type { Entscheidung, Zone } from '../typen'
import { holeEntscheidungen } from '../api'
import { datumZeitFormat } from '../hilfsfunktionen'
import { starteSichtbarkeitsIntervall } from '../sichtbarkeitsPolling'
import './EntscheidungsLog.css'

interface Props {
  zonen: Zone[]
}

export function EntscheidungsLog({ zonen }: Props) {
  const [daten, setDaten] = useState<Entscheidung[]>([])
  const [offen, setOffen] = useState(false)

  useEffect(() => {
    const laden = () => { holeEntscheidungen().then(setDaten).catch(() => {}) }
    laden()
    const stoppeIntervall = starteSichtbarkeitsIntervall(laden, 60_000)
    return () => stoppeIntervall()
  }, [])

  const zonenMap = Object.fromEntries(zonen.map(z => [z.zone_id, z.name]))

  if (daten.length === 0) return null

  return (
    <section className="entscheidungs-log">
      <button className="log-toggle" onClick={() => setOffen(o => !o)}>
        <span className="log-toggle-text">Entscheidungsprotokoll</span>
        <span className="log-toggle-count">{daten.length}</span>
        <span className={`log-toggle-pfeil ${offen ? 'offen' : ''}`}>&#9662;</span>
      </button>

      {offen && (
        <div className="log-tabelle-container">
          <table className="log-tabelle">
            <thead>
              <tr>
                <th>Zeitpunkt</th>
                <th>Zone</th>
                <th></th>
                <th>Dauer</th>
                <th>Begruendung</th>
              </tr>
            </thead>
            <tbody>
              {daten.slice(0, 30).map(e => (
                <tr key={`${e.zeitstempel}|${e.zone_id}`} className={e.soll_bewaessern ? 'ja' : 'nein'}>
                  <td className="zeit">{datumZeitFormat(e.zeitstempel)}</td>
                  <td>{zonenMap[e.zone_id] ?? e.zone_id}</td>
                  <td className="icon">{e.soll_bewaessern ? '\u2714' : '\u2012'}</td>
                  <td className="dauer">{e.soll_bewaessern && e.dauer_sekunden > 0 ? `${e.dauer_sekunden}s` : ''}</td>
                  <td className="grund">{e.begruendung}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
