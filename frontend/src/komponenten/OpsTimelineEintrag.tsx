import { useState } from 'react'
import type { OpsTimelineEintrag as OpsTimelineEintragTyp } from '../typen'
import { datumZeitFormat } from '../hilfsfunktionen'
import { patchVentilEreignis } from '../api'

interface Props {
  eintrag: OpsTimelineEintragTyp
  zonenMap: Record<string, string>
  onAktualisiert?: () => void
}

const AUSLOSER_OPTIONEN = [
  { wert: 'automatik', label: 'Automatik' },
  { wert: 'manuell', label: 'Manuell' },
  { wert: 'unbekannt', label: 'Unbekannt' },
  { wert: 'watchdog', label: 'Watchdog' },
  { wert: 'notfall_stopp', label: 'Notfall-Stopp' },
] as const

function metaText(meta: Record<string, string | number | boolean | null>): string[] {
  const labelMap: Record<string, string> = {
    blocker_typ: 'Blocker',
    dauer_sekunden: 'Dauer',
    ausloser: 'Ausloeser',
    aktion: 'Aktion',
    typ: 'Typ',
    behoben_um: 'Behoben',
    anzahl: 'Anzahl',
  }
  // Edit-Marker und numerische ID sind interne Felder, nicht fuer Anzeige.
  const technisch = new Set(['ventil_event_id', 'ausloeser_korrigiert', 'aggregiert'])

  return Object.entries(meta)
    .filter(([key, wert]) => !technisch.has(key) && wert !== null && wert !== false)
    .map(([key, wert]) => `${labelMap[key] ?? key}: ${String(wert)}`)
}

export function OpsTimelineEintrag({ eintrag, zonenMap, onAktualisiert }: Props) {
  const [offen, setOffen] = useState(false)
  const [editAuf, setEditAuf] = useState(false)
  const [neuerAusloser, setNeuerAusloser] = useState<string>(
    String(eintrag.meta.ausloser ?? ''),
  )
  const [speichert, setSpeichert] = useState(false)
  const [speicherFehler, setSpeicherFehler] = useState<string | null>(null)

  // Bevorzugt nimmt die UI die numerische `ventil_event_id` aus meta (sauber
  // typisiert). Fallback: aus dem zusammengesetzten id-String parsen
  // ("ventil:366" -> 366), damit die UI auch gegen aelteren Backend-Stand
  // funktioniert, der das meta-Feld noch nicht durchreicht.
  let ventilEventId = eintrag.meta.ventil_event_id as number | undefined
  if (ventilEventId === undefined && eintrag.id.startsWith('ventil:')) {
    const parsed = Number(eintrag.id.slice('ventil:'.length))
    if (!Number.isNaN(parsed)) ventilEventId = parsed
  }
  const istVentilEvent = eintrag.typ === 'VENTIL_EREIGNIS'
    && typeof ventilEventId === 'number'
  const istKorrigiert = Boolean(eintrag.meta.ausloeser_korrigiert)

  const betroffeneZonen = eintrag.betroffene_zonen
    .map(zoneId => zonenMap[zoneId] ?? zoneId)
    .join(', ')
  const metaZeilen = metaText(eintrag.meta)
  const hatDetails = Boolean(eintrag.details || betroffeneZonen || metaZeilen.length > 0)

  const speichern = async () => {
    if (!istVentilEvent) return
    setSpeichert(true)
    setSpeicherFehler(null)
    try {
      const antwort = await patchVentilEreignis(
        ventilEventId as number, { ausloser: neuerAusloser as never }, true,
      )
      if (antwort.fehler) {
        setSpeicherFehler(antwort.fehler)
      } else {
        setEditAuf(false)
        onAktualisiert?.()
      }
    } catch (err) {
      setSpeicherFehler(err instanceof Error ? err.message : String(err))
    } finally {
      setSpeichert(false)
    }
  }

  return (
    <article className={`ops-eintrag ops-${eintrag.severity.toLowerCase()}`}>
      <div className="ops-eintrag-kopf">
        <div className="ops-eintrag-meta">
          <span className={`ops-badge ops-${eintrag.severity.toLowerCase()}`}>
            {eintrag.severity}
          </span>
          <time className="ops-zeit">{datumZeitFormat(eintrag.zeitstempel)}</time>
          {istKorrigiert && (
            <span className="ops-korrigiert" title="Ausloeser wurde nachklassifiziert">
              korrigiert
            </span>
          )}
        </div>
        <div className="ops-eintrag-aktionen">
          {istVentilEvent && !editAuf && (
            <button
              type="button"
              className="ops-expand-btn"
              onClick={() => setEditAuf(true)}
            >
              Ausloeser aendern
            </button>
          )}
          {hatDetails && (
            <button
              type="button"
              className="ops-expand-btn"
              onClick={() => setOffen(wert => !wert)}
            >
              {offen ? 'Weniger' : 'Details'}
            </button>
          )}
        </div>
      </div>

      <div className="ops-eintrag-inhalt">
        <h3>{eintrag.titel}</h3>
        <p className="ops-eintrag-kontext">
          {eintrag.zone_name ?? (eintrag.scope === 'kanal' ? `Kanal ${eintrag.scope_ref}` : eintrag.typ)}
        </p>
      </div>

      {istVentilEvent && editAuf && (
        <div className="ops-eintrag-edit">
          <label htmlFor={`ausloser-${eintrag.id}`}>Neuer Ausloeser</label>
          <select
            id={`ausloser-${eintrag.id}`}
            value={neuerAusloser}
            onChange={e => setNeuerAusloser(e.target.value)}
            disabled={speichert}
          >
            {AUSLOSER_OPTIONEN.map(opt => (
              <option key={opt.wert} value={opt.wert}>{opt.label}</option>
            ))}
          </select>
          <button
            type="button"
            className="ops-edit-speichern"
            onClick={speichern}
            disabled={speichert || neuerAusloser === eintrag.meta.ausloser}
          >
            {speichert ? 'Speichert…' : 'Speichern'}
          </button>
          <button
            type="button"
            className="ops-edit-abbrechen"
            onClick={() => {
              setEditAuf(false)
              setNeuerAusloser(String(eintrag.meta.ausloser ?? ''))
              setSpeicherFehler(null)
            }}
            disabled={speichert}
          >
            Abbrechen
          </button>
          <p className="ops-edit-hinweis">
            Aenderung gilt auch fuers OEFFNEN/SCHLIESSEN-Pendant. Wirkt auf
            Wasser-Bilanz, Tages-Budget und ML-Trainingsdaten.
          </p>
          {speicherFehler && (
            <p className="ops-edit-fehler">{speicherFehler}</p>
          )}
        </div>
      )}

      {offen && hatDetails && (
        <div className="ops-eintrag-details">
          {eintrag.details && <p>{eintrag.details}</p>}
          {betroffeneZonen && (
            <p>
              <strong>Betroffene Zonen:</strong> {betroffeneZonen}
            </p>
          )}
          {metaZeilen.length > 0 && (
            <ul className="ops-meta-liste">
              {metaZeilen.map(zeile => (
                <li key={zeile}>{zeile}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </article>
  )
}
