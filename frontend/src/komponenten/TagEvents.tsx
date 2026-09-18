/* T-0055-B2 — Liste aller heutigen Ventil-Events mit Klassifikations-Buttons.
 *
 * Zeigt pro Event: Uhrzeit, Zone, Dauer/Liter, Quelle, Ausloeser.
 * Bei ausloser=UNBEKANNT (Sensor-Heuristik, noch nicht klassifiziert)
 * erscheinen Buttons: Automatik | Manuell | Regen (loescht Event).
 */

import { useEffect, useState } from 'react'
import type { VentilEreignisDetail, Zone } from '../typen'
import {
  holeVentilEreignisse,
  loescheVentilEreignis,
  patchVentilEreignis,
} from '../api'
import { datumZeitFormat, istAbbruch } from '../hilfsfunktionen'
import './TagEvents.css'

interface Props {
  zonen: Zone[]
}

function quelleLabel(ventil_id: string): string {
  if (ventil_id === 'manuell') return 'Manuell'
  if (ventil_id === 'backfill_app') return 'App-Backfill'
  if (ventil_id === 'gardena_web') return 'Gardena-App'
  if (ventil_id === 'sensor_heuristik') return 'Sensor-Heuristik'
  return 'Live'
}

function ausloeserLabel(a: string): string {
  const map: Record<string, string> = {
    automatik: 'Automatik',
    manuell: 'Manuell',
    // T-0455 / T-0453: siehe gleichnamige Funktion in GiessHistorieTab --
    // die beiden Maps sind bewusst getrennt (verschiedene Tabs, eigene
    // Wortwahl), muessen bei neuen Ausloeser-Werten aber BEIDE nachgezogen
    // werden. Der `?? a`-Fallback laesst ein Versaeumnis sonst nur als
    // kleingeschriebenen Rohwert durchrutschen.
    zeitplan: 'Gardena-Zeitplan',
    fremdwasser: 'Fremdwasser',
    ignoriert: 'Ignoriert',
    aquabloom: 'AquaBloom',
    unbekannt: 'Unbekannt',
    watchdog: 'Watchdog',
    notfall_stopp: 'Notfall',
  }
  return map[a] ?? a
}

export function TagEvents({ zonen }: Props) {
  const [events, setEvents] = useState<VentilEreignisDetail[] | null>(null)
  const [fehler, setFehler] = useState<string | null>(null)
  const [laden, setLaden] = useState(true)
  const [refreshTick, setRefreshTick] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    const heuteStart = new Date()
    heuteStart.setHours(0, 0, 0, 0)
    holeVentilEreignisse(heuteStart.toISOString(), undefined, undefined, controller.signal)
      .then(daten => {
        setEvents(daten)
        setFehler(null)
        setLaden(false)
      })
      .catch(err => {
        if (!istAbbruch(err)) {
          setFehler(String(err?.message ?? err))
          setLaden(false)
        }
      })
    return () => controller.abort()
  }, [refreshTick])

  const zonenName = Object.fromEntries(zonen.map(z => [z.zone_id, z.name]))

  const triggerRefresh = () => setRefreshTick(t => t + 1)

  // Heuristik-Events sind paarweise (OEFFNEN+SCHLIESSEN). Klassifikation und
  // Loeschung muessen deshalb das Paar mitnehmen, sonst bleibt ein halbes
  // unklares Event uebrig. Das Backend findet den Partner ueber ventil_id +
  // Zone + ±120s-Fenster.
  const klassifiziere = async (id: number, ausloser: 'automatik' | 'manuell') => {
    await patchVentilEreignis(id, { ausloser }, /* paar */ true)
    triggerRefresh()
  }

  const loesche = async (id: number) => {
    await loescheVentilEreignis(id, /* paar */ true)
    triggerRefresh()
  }

  return (
    <section className="tag-events">
      <header className="te-kopf">
        <h3>Heute: Ventil-Events</h3>
        <button className="te-refresh" onClick={triggerRefresh} title="Neu laden">↻</button>
      </header>

      {laden && <div className="te-status">Laedt…</div>}
      {fehler && <div className="te-fehler">{fehler}</div>}
      {!laden && events !== null && events.length === 0 && (
        <div className="te-leer">Keine Events heute.</div>
      )}

      {events !== null && events.length > 0 && (
        <ul className="te-liste">
          {events.map(e => {
            const unklassifiziert = e.ausloser === 'unbekannt'
            // T-0300: ein auto-ignoriertes ECHTES Ventil-Event (z.B.
            // Magerwiese-Kanal im events_auto_ignorieren-Fenster, wo die
            // temporaere Gras-Beregnung default ignoriert wird) kann der
            // User per Positiv-Opt-in als zaehlend markieren -> Hebung auf
            // 'automatik'. Der AutoIgnorierenJob flippt nur manuell/watchdog,
            // laesst automatik in Ruhe -> die Hebung bleibt. sensor_heuristik-
            // Phantome (Regen/Glitch) sind ausgenommen (kein echter Lauf).
            const autoIgnoriert =
              e.ausloser === 'ignoriert' && e.ventil_id !== 'sensor_heuristik'
            const dauerOderLiter = e.liter != null
              ? `${e.liter} L`
              : e.dauer_sekunden > 0 ? `${e.dauer_sekunden}s` : '—'
            return (
              <li key={e.id} className={unklassifiziert ? 'te-zeile unklassifiziert' : 'te-zeile'}>
                <div className="te-haupt">
                  <span className="te-zeit">{datumZeitFormat(e.zeitstempel)}</span>
                  <span className="te-zone">{zonenName[e.zone_id] ?? e.zone_id}</span>
                  <span className="te-aktion">{e.aktion === 'schliessen' ? 'SCHLIESSEN' : 'OEFFNEN'}</span>
                  <span className="te-dauer">{dauerOderLiter}</span>
                  <span className="te-quelle">{quelleLabel(e.ventil_id)}</span>
                  <span className={`te-ausloeser te-${e.ausloser}`}>
                    {ausloeserLabel(e.ausloser)}
                  </span>
                </div>

                {unklassifiziert && (
                  <div className="te-aktionen">
                    <span className="te-aktionen-hinweis">Bitte klassifizieren:</span>
                    <button onClick={() => klassifiziere(e.id, 'automatik')} title="War Gardena-App-Automatik">
                      Automatik
                    </button>
                    <button onClick={() => klassifiziere(e.id, 'manuell')} title="War manuelle Bewaesserung">
                      Manuell
                    </button>
                    <button
                      onClick={() => loesche(e.id)}
                      title="War gar keine Bewaesserung (z.B. Regen, Fehlalarm)"
                      className="te-btn-loeschen"
                    >
                      Regen / Fehler
                    </button>
                  </div>
                )}

                {autoIgnoriert && (
                  <div className="te-aktionen">
                    <span className="te-aktionen-hinweis">Auto-ignoriert:</span>
                    <button
                      onClick={() => klassifiziere(e.id, 'automatik')}
                      title="Echte Bewaesserung -- zaehlt fuer Wirkungsrate/Bilanz (hebt auf Automatik, bleibt dann erhalten)"
                    >
                      Zaehlt / echte Bewaesserung
                    </button>
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
