/* Inline-Statuszeile pro Zone: zeigt die letzte Entscheidung + Drawer mit Top 3.
 *
 * T-0028 "Warum"-Panel: erklaert in der ZonenKarte ohne zusaetzlichen Klickpfad,
 * warum gerade (nicht) gegossen wird. Nutzt die strukturierten Felder
 * blocker_typ / scope aus /api/entscheidungen (seit 2026-04-17).
 */

import { useEffect, useState } from 'react'
import type { BlockerTyp, Entscheidung } from '../typen'
import { holeEntscheidungen } from '../api'
import { datumZeitFormat, istAbbruch, zeitFormat } from '../hilfsfunktionen'
import './EntscheidungsErklaerung.css'

interface Props {
  zoneId: string
}

const BLOCKER_LABEL: Record<BlockerTyp, string> = {
  FEUCHTE_OK: 'Feuchte ausreichend',
  KEINE_MESSUNG: 'Keine aktuelle Messung',
  REGEN_ERWARTET: 'Regen erwartet',
  ZEITFENSTER: 'Ausserhalb Bewaesserungsfenster',
  // T-0356: ruhiges Wording -- das Tagesbudget ist eine Runaway-Notbremse,
  // kein Notfall; bei Trockenstress giesst das Backend via Notreserve weiter.
  BUDGET_ERSCHOEPFT: 'Tagesbudget voll',
  PAUSE_AKTIV: 'Mindestpause laeuft',
}

function kurzLabel(e: Entscheidung): string {
  if (e.soll_bewaessern) {
    const dauer = e.dauer_sekunden > 0 ? ` ${e.dauer_sekunden}s` : ''
    return `Giess-Empfehlung${dauer}`
  }
  if (e.blocker_typ) return BLOCKER_LABEL[e.blocker_typ]
  return 'Kein Giessen'
}

export function EntscheidungsErklaerung({ zoneId }: Props) {
  const [entscheidungen, setEntscheidungen] = useState<Entscheidung[]>([])
  const [offen, setOffen] = useState(false)
  const [refreshZaehler, setRefreshZaehler] = useState(0)

  useEffect(() => {
    const timer = setInterval(() => setRefreshZaehler(z => z + 1), 60_000)
    return () => clearInterval(timer)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    holeEntscheidungen(zoneId, 3, controller.signal)
      .then(setEntscheidungen)
      .catch(err => { if (!istAbbruch(err)) setEntscheidungen([]) })
    return () => controller.abort()
  }, [zoneId, refreshZaehler])

  if (entscheidungen.length === 0) return null

  // Defensiv: auch wenn der Server mehr liefert, zeigen wir nur die
  // letzten drei — die Komponente ist keine Historie, dafuer gibt es T-0037.
  const top3 = entscheidungen.slice(0, 3)
  const letzte = top3[0]
  const zustandsKlasse = letzte.soll_bewaessern ? 'giess' : 'kein-giess'

  return (
    <div className={`entscheidungs-erklaerung ${zustandsKlasse}`}>
      <button
        className="ee-status"
        onClick={() => setOffen(o => !o)}
        aria-expanded={offen}
        title="Letzte Entscheidungen anzeigen"
      >
        <span className="ee-icon" aria-hidden>{letzte.soll_bewaessern ? '\u2714' : '\u2012'}</span>
        <span className="ee-label">{kurzLabel(letzte)}</span>
        {/* T-0397 (F6): Scope schon in der eingeklappten Zeile zeigen. Sonst
            liest sich eine KANAL-Aggregat-Entscheidung ("Feuchte ausreichend",
            yogaraum fuehrt) wie ein Zonen-Urteil und widerspricht der Zonen-
            Empfehlungs-Box darunter ("akut"). Der Scope macht klar: das ist die
            gemittelte Kanal-Entscheidung, nicht der Sensor dieser Zone. */}
        <span
          className="ee-scope"
          title={
            letzte.scope === 'kanal'
              ? `Kanal-Entscheidung (Durchschnitt aller Zonen an Kanal ${letzte.scope_ref})`
              : 'Entscheidung nur auf Basis des Sensors dieser Zone'
          }
        >
          {letzte.scope === 'kanal' ? `Kanal ${letzte.scope_ref}` : 'Einzel-Sensor'}
        </span>
        <span className="ee-zeit">{zeitFormat(letzte.zeitstempel)}</span>
        <span className={`ee-pfeil ${offen ? 'offen' : ''}`} aria-hidden>&#9662;</span>
      </button>

      {offen && (
        <ul className="ee-liste">
          {top3.map(e => (
            <li key={`${e.zeitstempel}|${e.zone_id}`} className={e.soll_bewaessern ? 'ja' : 'nein'}>
              <div className="ee-zeile-kopf">
                <span className="ee-icon">{e.soll_bewaessern ? '\u2714' : '\u2012'}</span>
                <span className="ee-zeile-label">{kurzLabel(e)}</span>
                <span
                  className="ee-zeile-scope"
                  title={
                    e.scope === 'kanal'
                      ? `Entscheidung fuer alle Zonen an Kanal ${e.scope_ref} (Durchschnitts-Feuchte)`
                      : 'Entscheidung basierend nur auf dem Sensor dieser Zone'
                  }
                >
                  {e.scope === 'kanal' ? `Kanal ${e.scope_ref}` : 'Einzel-Sensor'}
                </span>
                <span className="ee-zeile-zeit">{datumZeitFormat(e.zeitstempel)}</span>
              </div>
              {e.begruendung && <div className="ee-zeile-grund">{e.begruendung}</div>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
