/* MLFehlerBanner — T-0108.
 *
 * Globales Banner ueber allen Dashboard-Tabs. Pollt `/api/ml/status`
 * und warnt, sobald einer der ML-Hintergrund-Jobs (Auto-Retrain,
 * Response-Retrain, Kalibrierung) zuletzt mit einem Crash geendet hat.
 *
 * Hintergrund: Vor T-0108 wurden Job-Crashes nur via `logger.exception`
 * geloggt — ein stiller Ausfall blieb bis zur naechsten manuellen
 * Log-Sichtung unentdeckt. Das Backend fuehrt jetzt pro Job ein
 * `letzter_fehler`-Feld, das beim naechsten erfolgreichen Lauf wieder
 * geleert wird; entsprechend verschwindet dieses Banner von selbst.
 */
import { useEffect, useState, useCallback } from 'react'
import { API, authFetch } from '../api'
import { datumZeitFormat } from '../hilfsfunktionen'
import type { MLStatus, MLJobFehler } from '../typen'
import './MLFehlerBanner.css'

// 60 s — der Job-Status aendert sich hoechstens im Stunden-/Tagestakt,
// haeufigeres Pollen brauchte es nicht.
const POLL_MS = 60_000

interface JobFehler {
  job: string
  fehler: MLJobFehler
}

export function MLFehlerBanner() {
  const [fehlerListe, setFehlerListe] = useState<JobFehler[]>([])

  const laden = useCallback(async () => {
    try {
      const r = await authFetch(`${API}/ml/status`)
      if (!r.ok) return
      const status: MLStatus = await r.json()
      const gesammelt: JobFehler[] = []
      if (status.retrain?.letzter_fehler) {
        gesammelt.push({ job: 'Auto-Retrain', fehler: status.retrain.letzter_fehler })
      }
      if (status.response_retrain?.letzter_fehler) {
        gesammelt.push({
          job: 'Response-Retrain', fehler: status.response_retrain.letzter_fehler,
        })
      }
      if (status.kalibrierung?.letzter_fehler) {
        gesammelt.push({
          job: 'Kalibrierung', fehler: status.kalibrierung.letzter_fehler,
        })
      }
      setFehlerListe(gesammelt)
    } catch {
      // Netzwerk-/Parse-Fehler still ignorieren — das Banner ist
      // Best-Effort und darf das Dashboard nicht stoeren.
    }
  }, [])

  useEffect(() => {
    const t = setInterval(() => void laden(), POLL_MS)
    // Erst-Ladung via Mikrotask — sonst feuert die Linter-Regel
    // react-hooks/set-state-in-effect auf das synchrone laden() im
    // Effect-Body (etablierter Projekt-Workaround, vgl. HistorieTab).
    void Promise.resolve().then(() => laden())
    return () => clearInterval(t)
  }, [laden])

  if (fehlerListe.length === 0) return null

  return (
    <div className="mfb-banner" role="alert">
      <span className="mfb-icon" aria-hidden="true">⚠️</span>
      <div className="mfb-inhalt">
        <strong>
          {fehlerListe.length === 1
            ? 'Ein ML-Hintergrund-Job ist fehlgeschlagen'
            : `${fehlerListe.length} ML-Hintergrund-Jobs sind fehlgeschlagen`}
        </strong>
        <ul className="mfb-liste">
          {fehlerListe.map(({ job, fehler }) => (
            <li key={job}>
              <span className="mfb-job">{job}</span>: {fehler.typ} — {fehler.nachricht}
              <span className="mfb-zeit"> ({datumZeitFormat(fehler.zeit)})</span>
            </li>
          ))}
        </ul>
        <span className="mfb-hinweis">
          Verschwindet automatisch, sobald der Job wieder erfolgreich läuft.
        </span>
      </div>
    </div>
  )
}

export default MLFehlerBanner
