/* T-0228 Stufe 1: "Anstehend"-Block fuer Pflege-/Wartungs-Erinnerungen.
 *
 * Loest das `arbeitspattern_conditional_trigger_calendar.md`-Pattern
 * (Claude-Anweisung "scanne TASK.md gegen heute") strukturell ab --
 * Erinnerungen liegen jetzt in der DB-Tabelle `pflege_erinnerung`.
 *
 * Vorlauf-Fenster Default 3 Tage. Wird nur gerendert wenn es
 * mindestens einen Eintrag gibt -- ansonsten unsichtbar.
 *
 * Klick auf "Erledigen": markiert via POST + entfernt den Eintrag aus
 * der lokalen Liste (Optimistic-Update, Memory
 * `arbeitspattern_optimistic_update_live.md`). Bei wiederkehrenden
 * Erinnerungen legt das Backend automatisch den naechsten Eintrag an;
 * der erscheint beim naechsten Refresh.
 */

import { useCallback, useEffect, useState } from 'react'
import {
  holePflegeErinnerungen,
  erledigePflegeErinnerung,
} from '../api'
import { istAbbruch } from '../hilfsfunktionen'
import { starteSichtbarkeitsIntervall } from '../sichtbarkeitsPolling'
import type { PflegeErinnerung, Zone } from '../typen'
import './PflegeErinnerungenBlock.css'

const VORLAUF_TAGE = 3

/** T-0370: Klartext statt rohem Typ-Enum ("ml_kalibrierung" stand als
 *  Badge im primaeren Dashboard). Unbekannte Typen lesbar zurueckfallen. */
const TYP_KLARTEXT: Record<string, string> = {
  ml_kalibrierung: 'ML-Kalibrierung',
  sensor_wartung: 'Sensor-Wartung',
  batterie: 'Batterie',
  duengen: 'Düngen',
  umtopfen: 'Umtopfen',
}

function typKlartext(typ: string): string {
  return TYP_KLARTEXT[typ] ?? typ.replace(/_/g, ' ')
}

function relativDatum(iso: string): string {
  const ts = new Date(iso).getTime()
  if (Number.isNaN(ts)) return iso
  const diffMs = ts - Date.now()
  const diffTage = Math.round(diffMs / (24 * 3600_000))
  if (diffTage < 0) return `seit ${-diffTage} Tag${diffTage === -1 ? '' : 'en'}`
  if (diffTage === 0) return 'heute'
  if (diffTage === 1) return 'morgen'
  return `in ${diffTage} Tagen`
}

function dringlichkeitsKlasse(iso: string): string {
  const ts = new Date(iso).getTime()
  if (Number.isNaN(ts)) return ''
  const diffTage = Math.floor((ts - Date.now()) / (24 * 3600_000))
  if (diffTage < 0) return 'pe-ueberfaellig'
  if (diffTage <= 1) return 'pe-akut'
  return ''
}

interface Props {
  /** Optional: andere Vorlauf-Spanne als Default. */
  anstehendTage?: number
  /** T-0370: Zonen-Liste, um zone_id-Slugs in Klartext-Namen aufzuloesen
   *  ("waldblumenhain" -> "Waldblumenhain"). Optional (Backward-Compat). */
  zonen?: Zone[]
}

export function PflegeErinnerungenBlock({
  anstehendTage = VORLAUF_TAGE,
  zonen,
}: Props) {
  const [eintraege, setEintraege] = useState<PflegeErinnerung[]>([])
  const [fehler, setFehler] = useState<string | null>(null)
  const [erledigend, setErledigend] = useState<number | null>(null)

  const laden = useCallback((signal?: AbortSignal) => {
    holePflegeErinnerungen(anstehendTage, signal)
      .then(r => { setEintraege(r.eintraege); setFehler(null) })
      .catch(e => { if (!istAbbruch(e)) setFehler(e.message ?? String(e)) })
  }, [anstehendTage])

  useEffect(() => {
    const ctrl = new AbortController()
    laden(ctrl.signal)
    // 5-Minuten-Polling: Pflege-Erinnerungen aendern sich selten,
    // hoehere Cadence waere Verschwendung.
    const stoppeIntervall = starteSichtbarkeitsIntervall(() => laden(), 5 * 60_000)
    return () => { ctrl.abort(); stoppeIntervall() }
  }, [laden])

  const erledigen = async (id: number) => {
    setErledigend(id)
    try {
      const r = await erledigePflegeErinnerung(id)
      if (r.ok) {
        // Optimistic-Update: aus lokaler Liste entfernen. Folge-Eintrag
        // (bei wiederkehrenden) kommt beim naechsten Polling-Refresh.
        setEintraege(prev => prev.filter(e => e.id !== id))
      } else {
        setFehler('Erledigen fehlgeschlagen')
      }
    } catch (e) {
      setFehler(e instanceof Error ? e.message : String(e))
    } finally {
      setErledigend(null)
    }
  }

  // Block bleibt unsichtbar wenn nichts ansteht -- nicht jeden Tag
  // einen leeren "Anstehend"-Header zeigen.
  if (eintraege.length === 0 && !fehler) return null

  return (
    <div className="pflege-erinnerungen-block">
      <h3>Anstehend ({anstehendTage} Tage)</h3>
      {fehler && (
        <div className="pe-fehler">
          Erinnerungen konnten nicht geladen werden: {fehler}
        </div>
      )}
      <ul>
        {eintraege.map(e => (
          <li
            key={e.id}
            className={`pe-eintrag ${dringlichkeitsKlasse(e.faellig_am)}`}
          >
            <div className="pe-text">
              <span className="pe-frist">{relativDatum(e.faellig_am)}</span>
              <span className="pe-typ">{typKlartext(e.typ)}</span>
              {e.zone_id && (
                <span className="pe-zone">
                  {zonen?.find(z => z.zone_id === e.zone_id)?.name ?? e.zone_id}
                </span>
              )}
              {e.intervall_tage && (
                <span className="pe-intervall" title={`Wiederkehrend alle ${e.intervall_tage} Tage`}>
                  ↻ {e.intervall_tage}d
                </span>
              )}
              {e.beschreibung && (
                <span className="pe-beschreibung" title={e.beschreibung}>
                  {e.beschreibung}
                </span>
              )}
            </div>
            <button
              type="button"
              className="pe-erledigen"
              onClick={() => void erledigen(e.id)}
              disabled={erledigend === e.id}
            >
              {erledigend === e.id ? '…' : 'Erledigt'}
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
