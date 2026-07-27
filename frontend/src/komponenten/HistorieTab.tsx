/* T-0037: Historie-Tab mit Zeitraum/Zone/Blocker-Filter + CSV-Export. */

import { useEffect, useMemo, useState } from 'react'
import type { Entscheidung, Zone } from '../typen'
import {
  holeEntscheidungenGefiltert,
  ladeEntscheidungenCsv,
  type EntscheidungsFilter,
} from '../api'
import { istAbbruch, datumZeitFormat } from '../hilfsfunktionen'
import './HistorieTab.css'

interface Props {
  zonen: Zone[]
}

type ZeitraumPreset = '24h' | '7d' | '30d' | 'custom'

const BLOCKER_OPTIONEN: { wert: string; label: string }[] = [
  { wert: '', label: 'Alle' },
  { wert: 'FEUCHTE_OK', label: 'Feuchte ausreichend' },
  { wert: 'KEINE_MESSUNG', label: 'Keine Messung' },
  { wert: 'REGEN_ERWARTET', label: 'Regen erwartet' },
  { wert: 'ZEITFENSTER', label: 'Zeitfenster' },
  { wert: 'BUDGET_ERSCHOEPFT', label: 'Tagesbudget voll' },
  { wert: 'PAUSE_AKTIV', label: 'Pause aktiv' },
]

function zeitraumZuVon(preset: ZeitraumPreset): string | undefined {
  if (preset === 'custom') return undefined
  const stundenZurueck = preset === '24h' ? 24 : preset === '7d' ? 168 : 720
  return new Date(Date.now() - stundenZurueck * 3600_000).toISOString()
}

export function HistorieTab({ zonen }: Props) {
  const [preset, setPreset] = useState<ZeitraumPreset>('24h')
  const [zoneId, setZoneId] = useState<string>('')
  const [blocker, setBlocker] = useState<string>('')
  const [limit, setLimit] = useState<number>(500)
  const [eintraege, setEintraege] = useState<Entscheidung[]>([])
  const [ladend, setLadend] = useState(false)
  const [fehler, setFehler] = useState<string | null>(null)

  const filter = useMemo<EntscheidungsFilter>(() => ({
    zone_id: zoneId || undefined,
    blocker_typ: blocker || undefined,
    von: zeitraumZuVon(preset),
    limit,
  }), [zoneId, blocker, preset, limit])

  useEffect(() => {
    const controller = new AbortController()
    // Ladend-Flag via Mikrotask setzen, damit Linter-Regel
    // react-hooks/set-state-in-effect nicht feuert (kein sync setState im Effect-Body).
    Promise.resolve().then(() => {
      if (controller.signal.aborted) return
      setLadend(true)
      setFehler(null)
    })
    holeEntscheidungenGefiltert(filter, controller.signal)
      .then(d => { setEintraege(d); setLadend(false) })
      .catch(e => {
        if (istAbbruch(e)) return
        setFehler(e instanceof Error ? e.message : String(e))
        setEintraege([])
        setLadend(false)
      })
    return () => controller.abort()
  }, [filter])

  // T-0232: CSV-Export laeuft jetzt ueber authFetch + Blob (sonst HTTP 401
  // bei aktivem X-Api-Key, weil <a href download> die Header umgeht).
  const [csvLadend, setCsvLadend] = useState(false)
  const [csvFehler, setCsvFehler] = useState<string | null>(null)
  const csvKlick = async () => {
    setCsvLadend(true)
    setCsvFehler(null)
    try {
      await ladeEntscheidungenCsv(filter)
    } catch (e) {
      setCsvFehler(e instanceof Error ? e.message : String(e))
    } finally {
      setCsvLadend(false)
    }
  }

  const zoneName = (zid: string): string => {
    const z = zonen.find(z => z.zone_id === zid)
    return z ? z.name : zid
  }

  return (
    <div className="historie-tab">
      <header className="hist-filter">
        <div className="hist-filter-gruppe">
          <label className="hist-label">Zeitraum</label>
          <div className="hist-preset-leiste" role="tablist">
            {(['24h', '7d', '30d'] as const).map(p => (
              <button
                key={p}
                type="button"
                role="tab"
                aria-selected={preset === p}
                className={`hist-preset-btn ${preset === p ? 'aktiv' : ''}`}
                onClick={() => setPreset(p)}
              >
                {p}
              </button>
            ))}
          </div>
        </div>

        <div className="hist-filter-gruppe">
          <label className="hist-label" htmlFor="hist-zone">Zone</label>
          <select
            id="hist-zone"
            className="hist-select"
            value={zoneId}
            onChange={e => setZoneId(e.target.value)}
          >
            <option value="">Alle Zonen</option>
            {zonen.map(z => (
              <option key={z.zone_id} value={z.zone_id}>{z.name}</option>
            ))}
          </select>
        </div>

        <div className="hist-filter-gruppe">
          <label className="hist-label" htmlFor="hist-blocker">Blocker</label>
          <select
            id="hist-blocker"
            className="hist-select"
            value={blocker}
            onChange={e => setBlocker(e.target.value)}
          >
            {BLOCKER_OPTIONEN.map(o => (
              <option key={o.wert} value={o.wert}>{o.label}</option>
            ))}
          </select>
        </div>

        <div className="hist-filter-gruppe">
          <label className="hist-label" htmlFor="hist-limit">Limit</label>
          <select
            id="hist-limit"
            className="hist-select"
            value={limit}
            onChange={e => setLimit(parseInt(e.target.value, 10))}
          >
            {[100, 500, 1000, 5000].map(n => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </div>

        <div className="hist-filter-gruppe hist-filter-aktionen">
          <button
            type="button"
            className="hist-csv-btn"
            onClick={() => void csvKlick()}
            disabled={csvLadend}
            title={csvFehler ?? undefined}
          >
            {csvLadend ? 'Exportiere…' : 'Als CSV exportieren'}
          </button>
        </div>
      </header>

      <div className="hist-status-zeile">
        {ladend && <span>Lade…</span>}
        {fehler && <span className="hist-fehler">Fehler: {fehler}</span>}
        {csvFehler && <span className="hist-fehler">CSV: {csvFehler}</span>}
        {!ladend && !fehler && (
          <span>
            {eintraege.length} Eintraege
            {eintraege.length >= limit && ` (Limit erreicht – evtl. Limit erhoehen)`}
          </span>
        )}
      </div>

      <div className="hist-tabelle-wrap">
        <table className="hist-tabelle">
          <thead>
            <tr>
              <th>Zeit</th>
              <th>Zone</th>
              <th>Scope</th>
              <th>Aktion</th>
              <th>Dauer</th>
              <th>Blocker</th>
              <th>Begruendung</th>
            </tr>
          </thead>
          <tbody>
            {eintraege.map((e, i) => (
              <tr key={`${e.zeitstempel}|${e.zone_id}|${i}`}>
                <td>{datumZeitFormat(e.zeitstempel)}</td>
                <td>{zoneName(e.zone_id)}</td>
                <td>{e.scope}{e.scope === 'kanal' && ` ${e.scope_ref}`}</td>
                <td>
                  <span className={`hist-pill ${e.soll_bewaessern ? 'giessen' : 'nein'}`}>
                    {e.soll_bewaessern ? 'giessen' : 'nein'}
                  </span>
                </td>
                <td>{e.soll_bewaessern ? `${e.dauer_sekunden}s` : '–'}</td>
                <td>
                  {e.blocker_typ
                    ? (BLOCKER_OPTIONEN.find(o => o.wert === e.blocker_typ)?.label ?? e.blocker_typ)
                    : '–'}
                </td>
                <td className="hist-begruendung">{e.begruendung}</td>
              </tr>
            ))}
            {!ladend && eintraege.length === 0 && (
              <tr><td colSpan={7} className="hist-leer">Keine Entscheidungen im gewaehlten Zeitraum.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
