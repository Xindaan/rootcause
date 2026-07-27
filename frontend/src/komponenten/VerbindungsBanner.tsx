/* T-0311: Client-seitiger Verbindungs-/Frische-Indikator.
 *
 * Problem (Realfall 18.06.): Bei unerreichbarem Backend (Host schlief/offline)
 * faengt App.tsx die Poll-Fetch-Fehler still ab und behaelt die letzten Daten.
 * Das Sensor-Alter auf den Karten ("vor 17,9 h") tickt weiter hoch -> sieht aus
 * wie "Sensor tot", ist aber Verbindungsverlust. Der husqvarna.stale-Badge
 * reflektiert die BACKEND-Sicht, nicht "mein Browser erreicht das Backend nicht".
 *
 * Fix: prominenter Banner, sobald der letzte Daten-Abruf fehlschlug ODER seit
 * > STALE_MS kein erfolgreicher Abruf mehr lief (faengt auch still gescheiterte
 * Polls + Laptop-Schlaf -> beim Aufwachen sofort sichtbar). Eigener 30s-Tick,
 * damit das angezeigte Alter live mitlaeuft, auch wenn keine Poll-Re-Render
 * mehr kommen (Backend down -> `fehler`-String bleibt gleich -> kein App-Re-Render).
 */
import { useEffect, useState } from 'react'
import './VerbindungsBanner.css'

interface Props {
  /** Letzter Backend-Fehler (null = letzter Abruf war ok). */
  fehler: string | null
  /** Zeitpunkt des letzten ERFOLGREICHEN Daten-Abrufs (holeZonen). */
  letzterErfolg: Date
}

// Ohne erfolgreichen Abruf seit > 2 min (~3-4 Poll-Zyklen bei 30 s) gelten die
// Daten als veraltet -- auch wenn `fehler` aus irgendeinem Grund nicht gesetzt
// wurde (z.B. nur still gescheiterte Sekundaer-Polls, Tab nach Schlaf).
const STALE_MS = 120_000

function formatAlter(ms: number): string {
  const min = Math.floor(ms / 60_000)
  if (min < 1) return 'unter 1 min'
  if (min < 60) return `${min} min`
  const h = Math.floor(min / 60)
  const restMin = min % 60
  return restMin > 0 ? `${h} h ${restMin} min` : `${h} h`
}

export function VerbindungsBanner({ fehler, letzterErfolg }: Props) {
  const [jetzt, setJetzt] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setJetzt(Date.now()), 30_000)
    return () => clearInterval(t)
  }, [])

  const alterMs = jetzt - letzterErfolg.getTime()
  if (fehler == null && alterMs < STALE_MS) return null

  const zeit = letzterErfolg.toLocaleString('de-DE', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
  })
  return (
    <div className="verbindungs-banner" role="alert">
      <span className="vb-icon" aria-hidden="true">⚠</span>
      <span className="vb-text">
        <strong>Keine Verbindung zum Backend.</strong>{' '}
        Die angezeigten Werte sind veraltet — letzter erfolgreicher Abruf vor{' '}
        <strong>{formatAlter(alterMs)}</strong> ({zeit}). Das Sensor-Alter auf
        den Karten spiegelt evtl. nur den Verbindungsverlust, nicht den echten
        Sensor-Stand.
      </span>
    </div>
  )
}
