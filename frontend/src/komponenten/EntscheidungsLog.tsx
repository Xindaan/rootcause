/* Einklappbares Entscheidungsprotokoll — zeigt die letzten Bewaesserungsentscheidungen. */

import { useEffect, useState } from 'react'
import type { Entscheidung, Zone } from '../typen'
import { holeEntscheidungenGefiltert } from '../api'
import { datumZeitFormat } from '../hilfsfunktionen'
import { starteSichtbarkeitsIntervall } from '../sichtbarkeitsPolling'
import { MonitoringSchalter } from './MonitoringSchalter'
import './EntscheidungsLog.css'

interface Props {
  zonen: Zone[]
}

export function EntscheidungsLog({ zonen }: Props) {
  const [daten, setDaten] = useState<Entscheidung[] | null>(null)
  const [offen, setOffen] = useState(false)
  // T-0610 Q3 (W7): wie im Historie-Tab sind die Routinezeilen "Zone im
  // Monitoring-Modus" standardmaessig aus. Das Backend filtert vor dem
  // Limit (50), sonst verdraengen sie die echten Entscheidungen.
  const [zeigeMonitoring, setZeigeMonitoring] = useState(false)

  useEffect(() => {
    // Abbruch beim Umschalten: eine spaete Antwort mit dem alten Filter
    // darf die neue Liste nicht ueberschreiben.
    const controller = new AbortController()
    const laden = () => {
      holeEntscheidungenGefiltert({ limit: 50, ohne_monitoring: !zeigeMonitoring }, controller.signal)
        .then(setDaten).catch(() => {})
    }
    laden()
    const stoppeIntervall = starteSichtbarkeitsIntervall(laden, 60_000)
    return () => { stoppeIntervall(); controller.abort() }
  }, [zeigeMonitoring])

  const zonenMap = Object.fromEntries(zonen.map(z => [z.zone_id, z.name]))

  // Ausgeblendet nur, solange nichts geladen ist oder auch MIT Routinezeilen
  // nichts da ist. Bleibt nach dem Filter nichts uebrig, muss der Schalter
  // erreichbar bleiben.
  if (daten === null || (daten.length === 0 && zeigeMonitoring)) return null

  return (
    <section className="entscheidungs-log">
      <button className="log-toggle" onClick={() => setOffen(o => !o)}>
        <span className="log-toggle-text">Entscheidungsprotokoll</span>
        <span className="log-toggle-count">{daten.length}</span>
        <span className={`log-toggle-pfeil ${offen ? 'offen' : ''}`}>&#9662;</span>
      </button>

      {offen && (
        <div className="log-monitoring-zeile">
          <MonitoringSchalter
            an={zeigeMonitoring}
            onChange={setZeigeMonitoring}
            className="log-monitoring"
            text="Monitoring-Routinezeilen zeigen"
          />
        </div>
      )}

      {offen && daten.length === 0 && (
        <p className="log-leer">Keine Einträge außer Monitoring-Routinezeilen.</p>
      )}

      {offen && daten.length > 0 && (
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
