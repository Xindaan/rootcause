/* T-0612: Rueckfrage zum Notfall-Stopp als kleines, barrierearmes Modal.
 *
 * Ersetzt `window.confirm`, weil der Dialog jetzt eine Wahl traegt: das
 * Haekchen "Automatik der betroffenen Kanäle bis zur Freigabe sperren"
 * (standardmaessig gesetzt, Entscheidung Andre 30.09.2026).
 *
 * Barrierearm: role="dialog" + aria-modal, Titel und Beschreibung verknuepft,
 * Fokus beim Oeffnen auf dem Bestaetigen-Knopf (Enter bestaetigt wie bisher
 * beim confirm), Tab bleibt im Dialog, Escape bricht ab, danach kehrt der
 * Fokus zum ausloesenden Element zurueck. */
import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import './NotfallDialog.css'

interface Props {
  /** false: Konfig `notfall_kanal_sperre_aktiv` ist aus -> kein Haekchen. */
  sperreAnbieten: boolean
  onBestaetigen: (sperren: boolean) => void
  onAbbrechen: () => void
}

export function NotfallDialog({ sperreAnbieten, onBestaetigen, onAbbrechen }: Props) {
  const [sperren, setSperren] = useState(true)
  const dialogRef = useRef<HTMLDivElement>(null)
  const bestaetigenRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const vorher = document.activeElement instanceof HTMLElement ? document.activeElement : null
    bestaetigenRef.current?.focus()
    return () => { vorher?.focus() }
  }, [])

  const aufTaste = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      onAbbrechen()
      return
    }
    if (e.key !== 'Tab' || !dialogRef.current) return
    const fokussierbar = Array.from(
      dialogRef.current.querySelectorAll<HTMLElement>('button, input'),
    ).filter(el => !el.hasAttribute('disabled'))
    if (fokussierbar.length === 0) return
    const erstes = fokussierbar[0]
    const letztes = fokussierbar[fokussierbar.length - 1]
    if (e.shiftKey && document.activeElement === erstes) {
      e.preventDefault()
      letztes.focus()
    } else if (!e.shiftKey && document.activeElement === letztes) {
      e.preventDefault()
      erstes.focus()
    }
  }

  return (
    <div className="notfall-dialog-overlay" onKeyDown={aufTaste}>
      <div
        ref={dialogRef}
        className="notfall-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="notfall-dialog-titel"
        aria-describedby="notfall-dialog-text"
      >
        <h2 id="notfall-dialog-titel">Notfall-Stopp</h2>
        <p id="notfall-dialog-text">
          Alle Ventile sofort schließen und laufende Gieß-Sequenzen abbrechen?
        </p>
        {sperreAnbieten && (
          <label className="notfall-dialog-haekchen">
            <input
              type="checkbox"
              checked={sperren}
              onChange={e => setSperren(e.target.checked)}
              aria-describedby="notfall-dialog-haekchen-hinweis"
            />
            <span>
              Automatik der betroffenen Kanäle bis zur Freigabe sperren
              <small id="notfall-dialog-haekchen-hinweis">
                Die Automatik gießt dort erst wieder, wenn du sie im Dashboard
                freigibst. Manuelles Gießen bleibt möglich.
              </small>
            </span>
          </label>
        )}
        <div className="notfall-dialog-aktionen">
          <button type="button" className="notfall-dialog-btn-sek" onClick={onAbbrechen}>
            Abbrechen
          </button>
          <button
            ref={bestaetigenRef}
            type="button"
            className="notfall-dialog-btn-prim"
            onClick={() => onBestaetigen(sperreAnbieten && sperren)}
          >
            Alle Ventile schließen
          </button>
        </div>
      </div>
    </div>
  )
}
