/* T-0239: Auth-Onboarding-Dialog fuer den X-Api-Key.
 *
 * Vor T-0239: User musste den Key per devtools-Konsole in
 * `localStorage['pflanzen_api_key']` setzen (README:241). Bei mobiler
 * Verwendung praktisch nicht moeglich.
 *
 * Mit T-0239: bei fehlendem Key oder bei HTTP-401-Response oeffnet sich
 * ein Modal-Dialog mit Eingabe-Feld + Verbinden-Button. Der Key wird
 * in localStorage geschrieben, dann ein Reload getriggert damit alle
 * Komponenten neu laden.
 *
 * Plus: Header-Hint mit aktivem Rollen-Tag + Abmelden-Button (loescht
 * den Key wieder).
 *
 * Bewusst KEIN Backend-Endpoint zum Key-Validieren (vermeidet Round-
 * Trip + neue Surface). Validierung passiert durch den ersten echten
 * Request -- wenn der 401 liefert, oeffnet sich der Dialog erneut.
 */

import { useEffect, useState } from 'react'
import { holeApiKey } from '../api'
import './AuthDialog.css'

const STORAGE_KEY = 'pflanzen_api_key'

// T-0253: Dialog nutzt jetzt denselben Lookup wie `api.holeApiKey`
// (localStorage + VITE_API_KEY-Fallback). Vorher schaute er nur in
// localStorage und erschien auch dann, wenn der Build den Key
// eingebrannt hatte — die API funktionierte im Hintergrund, der
// Dialog suggerierte aber faelschlich "kein Key vorhanden".
function speichereKey(key: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, key)
  } catch (e) {
    console.error('Konnte API-Key nicht in localStorage speichern:', e)
  }
}

function loescheKey(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* ignore */
  }
}

/** T-0239: Liefert eine Funktion, die alle anderen Komponenten via
 *  `window`-Event ueber Key-Wechsel informiert. Hauptanwendung: nach
 *  Verbinden/Abmelden wird die App neu geladen, damit alle
 *  useEffect-Loader frischen Fetch machen. */
function triggereReload() {
  window.location.reload()
}

interface Props {
  /** Wenn true: Dialog wird unconditional gezeigt (z. B. von einem
   *  Header-Button "Schluessel aendern"). */
  forced?: boolean
  onClose?: () => void
}

export function AuthDialog({ forced = false, onClose }: Props) {
  const [eingabe, setEingabe] = useState('')
  const [fehler, setFehler] = useState<string | null>(null)
  const [istOffen, setIstOffen] = useState(forced || holeApiKey() === null)

  useEffect(() => {
    // Mikrotask, damit react-hooks/set-state-in-effect nicht feuert
    // (etablierter Projekt-Workaround, vgl. HistorieTab + MLFehlerBanner).
    void Promise.resolve().then(() => {
      setIstOffen(forced || holeApiKey() === null)
    })
  }, [forced])

  // T-0239: Globale 401-Erkennung. authFetch wirft Fehler bei 401, der
  // an Stelle (z. B. App.tsx-error-handler) gefangen wird; ein
  // window-Event signalisiert dann diesem Dialog. Vorerst Polling per
  // Storage-Event nur fuer Multi-Tab-Sync.
  useEffect(() => {
    const aufStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) setIstOffen(holeApiKey() === null)
    }
    window.addEventListener('storage', aufStorage)
    return () => window.removeEventListener('storage', aufStorage)
  }, [])

  if (!istOffen) return null

  const handleVerbinden = () => {
    const k = eingabe.trim()
    if (!k) {
      setFehler('Bitte API-Key eingeben.')
      return
    }
    if (k.length < 16) {
      setFehler('API-Key sieht zu kurz aus (mind. 16 Zeichen erwartet).')
      return
    }
    speichereKey(k)
    setFehler(null)
    setEingabe('')
    setIstOffen(false)
    onClose?.()
    // Reload damit alle Polling-Komponenten neu starten mit dem Key.
    triggereReload()
  }

  return (
    <div
      className="auth-dialog-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="auth-dialog-title"
    >
      <div className="auth-dialog">
        <h2 id="auth-dialog-title">Mit Backend verbinden</h2>
        <p className="auth-dialog-intro">
          Das Dashboard braucht einen API-Schluessel um Daten vom Backend
          zu laden. Schluessel erstellen per CLI:
        </p>
        <pre className="auth-dialog-cmd">
          python -m bewaesserung.api_auth schluessel-erstellen --rolle control
        </pre>
        <p className="auth-dialog-intro">
          Den ausgegebenen Klartext hier eingeben (wird in deinem
          Browser unter <code>localStorage.pflanzen_api_key</code>{' '}
          gespeichert, nicht ans Backend uebertragen ausser im
          X-Api-Key-Header).
        </p>
        <input
          type="password"
          className="auth-dialog-input"
          placeholder="API-Key"
          value={eingabe}
          onChange={e => setEingabe(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter') handleVerbinden()
          }}
          autoFocus
        />
        {fehler && <div className="auth-dialog-fehler">{fehler}</div>}
        <div className="auth-dialog-aktionen">
          {forced && (
            <button
              type="button"
              className="auth-dialog-btn-sek"
              onClick={() => {
                setIstOffen(false)
                onClose?.()
              }}
            >
              Abbrechen
            </button>
          )}
          <button
            type="button"
            className="auth-dialog-btn-prim"
            onClick={handleVerbinden}
          >
            Verbinden
          </button>
        </div>
      </div>
    </div>
  )
}

/** T-0239: Header-Komponente fuer den Auth-Status. Zeigt
 *  "Verbunden"-Indikator oder "Schluessel setzen"-Button. */
export function AuthStatus() {
  const [hatKey, setHatKey] = useState(() => holeApiKey() !== null)
  const [dialogOffen, setDialogOffen] = useState(false)

  useEffect(() => {
    const aufStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) setHatKey(holeApiKey() !== null)
    }
    window.addEventListener('storage', aufStorage)
    return () => window.removeEventListener('storage', aufStorage)
  }, [])

  if (!hatKey) {
    return (
      <button
        type="button"
        className="auth-status auth-status--fehlt"
        onClick={() => setDialogOffen(true)}
      >
        🔑 Schluessel setzen
        {dialogOffen && <AuthDialog forced onClose={() => setDialogOffen(false)} />}
      </button>
    )
  }

  return (
    <>
      <span
        className="auth-status auth-status--ok"
        title="Schluessel verbunden. Klick zum Aendern."
      >
        <button
          type="button"
          className="auth-status-link"
          onClick={() => setDialogOffen(true)}
        >
          🔑 verbunden
        </button>
        <button
          type="button"
          className="auth-status-abmelden"
          onClick={() => {
            if (!confirm('API-Key wirklich loeschen? Dashboard kann dann nichts mehr laden.')) {
              return
            }
            loescheKey()
            setHatKey(false)
            triggereReload()
          }}
          title="Abmelden (Key loeschen)"
        >
          ×
        </button>
      </span>
      {dialogOffen && (
        <AuthDialog forced onClose={() => setDialogOffen(false)} />
      )}
    </>
  )
}
