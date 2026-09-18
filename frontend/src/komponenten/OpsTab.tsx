import { useEffect, useRef, useState } from 'react'
import { holeOpsSummary, holeOpsTimeline } from '../api'
import { istAbbruch } from '../hilfsfunktionen'
import { starteSichtbarkeitsIntervall } from '../sichtbarkeitsPolling'
import type {
  OpsSeverityFilter,
  OpsSummary,
  OpsTimelineAntwort,
  Zone,
} from '../typen'
import { OpsFilter } from './OpsFilter'
import { OpsKpiZeile } from './OpsKpiZeile'
import { OpsTimeline } from './OpsTimeline'
import { TagEvents } from './TagEvents'
// T-0238: Betriebsstatus-Zentrale am Anfang des Ops-Tabs.
import { BetriebsstatusKachel } from './BetriebsstatusKachel'
import './OpsTab.css'

const DEFAULT_SEVERITY: OpsSeverityFilter[] = ['kritisch', 'aktion', 'wetter']

interface Props {
  zonen: Zone[]
}

export function OpsTab({ zonen }: Props) {
  const [summary, setSummary] = useState<OpsSummary | null>(null)
  const [timeline, setTimeline] = useState<OpsTimelineAntwort | null>(null)
  const [stunden, setStunden] = useState(24)
  const [zoneId, setZoneId] = useState('')
  const [severity, setSeverity] = useState<OpsSeverityFilter[]>(DEFAULT_SEVERITY)
  const [fehler, setFehler] = useState<string | null>(null)
  const [laedt, setLaedt] = useState(true)
  const [reloadTick, setReloadTick] = useState(0)
  // Erstlade-Indikator per Ref: verhindert dass summary/timeline in den
  // useEffect-Dependencies landen (waere endlose Re-Fetches) und haelt
  // den Lint-Check sauber.
  const ersterLadeRef = useRef(true)

  useEffect(() => {
    let aktiv = true
    const controller = new AbortController()
    const signal = controller.signal

    const ladeDaten = async () => {
      if (ersterLadeRef.current) setLaedt(true)
      try {
        const [summaryDaten, timelineDaten] = await Promise.all([
          holeOpsSummary(signal),
          holeOpsTimeline(stunden, severity, zoneId || undefined, signal),
        ])
        if (!aktiv) return
        setSummary(summaryDaten)
        setTimeline(timelineDaten)
        setFehler(null)
      } catch (error) {
        if (!aktiv || istAbbruch(error)) return
        const nachricht = error instanceof Error ? error.message : 'Unbekannter Fehler'
        setFehler(`Ops-Daten konnten nicht geladen werden: ${nachricht}`)
      } finally {
        if (aktiv) {
          setLaedt(false)
          ersterLadeRef.current = false
        }
      }
    }

    void ladeDaten()
    const stoppeIntervall = starteSichtbarkeitsIntervall(() => { void ladeDaten() }, 60_000)

    return () => {
      aktiv = false
      controller.abort()
      stoppeIntervall()
    }
  }, [stunden, zoneId, severity, reloadTick])

  const toggleSeverity = (wert: OpsSeverityFilter) => {
    setSeverity(aktive => {
      if (aktive.includes(wert)) {
        return aktive.length === 1 ? aktive : aktive.filter(eintrag => eintrag !== wert)
      }
      return [...aktive, wert]
    })
  }

  return (
    <section className="ops-tab">
      <div className="ops-intro">
        <h2>Ops Review</h2>
        <p>
          Shadow-Entscheidungen, reale Ventil-Events, Wetter- und Sensorwarnungen
          in einer Timeline fuer das Review der Automatik.
        </p>
      </div>

      <OpsFilter
        zonen={zonen}
        stunden={stunden}
        zoneId={zoneId}
        severity={severity}
        eintraegeAnzahl={timeline?.eintraege.length}
        onStundenChange={setStunden}
        onZoneChange={setZoneId}
        onToggleSeverity={toggleSeverity}
      />

      {fehler && <div className="ops-fehler">{fehler}</div>}

      {/* T-0238: Betriebsstatus-Zentrale ueber den heute-Stats.
          Eigene Polling-Cadence (60 s), independent von OpsTimeline. */}
      <BetriebsstatusKachel />

      <OpsKpiZeile summary={summary} />

      <TagEvents zonen={zonen} />

      {laedt && !timeline ? (
        <div className="ops-ladezustand">Ops-Daten werden geladen...</div>
      ) : (
        <OpsTimeline
          eintraege={timeline?.eintraege ?? []}
          routineUnterdrueckt={timeline?.aggregiert.routine_unterdueckt ?? 0}
          zonen={zonen}
          onAktualisiert={() => setReloadTick(t => t + 1)}
        />
      )}
    </section>
  )
}
