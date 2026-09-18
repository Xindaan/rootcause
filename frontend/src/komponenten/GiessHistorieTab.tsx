/* T-0335: Giess-Historie pro Zone -- wann, wie lange, WIE (Einzel vs Pre-Soak),
 * Ausloeser (auto/manuell). Pre-Soaks erscheinen als EIN Lauf (Backend-Marker
 * `lauf_gruppe`), ignorierte/Cross-Spray-Laeufe sind ausgegraut (zaehlt=false).
 * Datengruppierung passiert im Backend (giess_historie.gruppiere_giess_laeufe),
 * diese Komponente zeigt nur an.
 */

import { useEffect, useMemo, useState } from 'react'
import type { GiessLauf, Zone } from '../typen'
import { holeGiessHistorie } from '../api'
import { datumZeitFormat, istAbbruch } from '../hilfsfunktionen'
import './GiessHistorieTab.css'

interface Props {
  zonen: Zone[]
}

function formatiereDauer(s: number | null): string {
  if (s == null) return '—'
  if (s < 90) return `${s}s`
  const min = Math.round(s / 60)
  if (min < 60) return `${min} min`
  const h = Math.floor(min / 60)
  const rest = min % 60
  return rest ? `${h} h ${rest} min` : `${h} h`
}

function ausloeserLabel(a: string): string {
  const map: Record<string, string> = {
    automatik: 'Automatik',
    manuell: 'Manuell',
    // T-0455: Gardena-Cloud-Zeitplan. Realer Lauf, aber nicht unsere
    // Engine -- der Unterschied ist genau der Punkt der Task.
    zeitplan: 'Gardena-Zeitplan',
    // T-0453: Wasser vom Regner einer Nachbar-Zone (Cross-Spray).
    fremdwasser: 'Fremdwasser',
    unbekannt: 'Unbekannt',
    watchdog: 'Watchdog',
    ignoriert: 'Ignoriert',
    aquabloom: 'AquaBloom',
    notfall_stopp: 'Notfall',
  }
  // Fallback zeigt den Rohwert. Das ist Absicht (nichts verschwindet),
  // versteckt aber eine fehlende Zuordnung als Kleinschreibung statt sie
  // zu melden -- genau so fiel `zeitplan` nach der Migration auf.
  return map[a] ?? a
}

function phaseLabel(p: string | null): string {
  if (p === 'pre_soak') return 'Vorwässern'
  if (p === 'haupt') return 'Hauptdose'
  return 'Lauf'
}

export function GiessHistorieTab({ zonen }: Props) {
  // Default: erste Zone mit Ventilkanal (sonst erste Zone).
  const giessbar = useMemo(
    () => zonen.filter(z => z.ventil_kanal != null),
    [zonen],
  )
  // Default: '' = alle Zonen (chronologisch gemischt). Einzelne Zone waehlbar
  // und wieder auf "Alle" zuruecksetzbar.
  const [zoneId, setZoneId] = useState<string>('')
  const [tage, setTage] = useState<number>(14)
  const [laeufe, setLaeufe] = useState<GiessLauf[] | null>(null)
  const [fehler, setFehler] = useState<string | null>(null)
  const [laden, setLaden] = useState(true)

  // "Laedt..." haengt hier NICHT an `!laeufe` -- der Hinweis soll bei jedem
  // Filterwechsel erscheinen, nicht nur beim ersten Laden. Ein blosses
  // Entfernen des Setzens waere deshalb eine Regression. Stattdessen das von
  // React dokumentierte Muster "State beim Wechsel im Render anpassen":
  // billiger als ein setState im Effect (kein Extra-Commit) und ohne den
  // Kaskaden-Render, den `react-hooks/set-state-in-effect` anmahnt.
  const filterSchluessel = `${zoneId}|${tage}`
  const [geladenerFilter, setGeladenerFilter] = useState(filterSchluessel)
  if (filterSchluessel !== geladenerFilter) {
    setGeladenerFilter(filterSchluessel)
    setLaden(true)
  }

  useEffect(() => {
    const controller = new AbortController()
    holeGiessHistorie(zoneId || undefined, tage, controller.signal)
      .then(daten => {
        setLaeufe(daten)
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
  }, [zoneId, tage])

  const auswahlZonen = giessbar.length ? giessbar : zonen
  const zonenName = Object.fromEntries(zonen.map(z => [z.zone_id, z.name]))
  // Serielle Straenge laufen unter mehreren Zonen -> mit " + " verbinden.
  const zonenLabel = (ids: string[]) =>
    ids.map(id => zonenName[id] ?? id).join(' + ')

  return (
    <div className={`giess-historie ${zoneId ? '' : 'giess-historie--alle'}`}>
      <header className="gh-filter">
        <div className="gh-filter-gruppe">
          <label className="gh-label" htmlFor="gh-zone">Zone</label>
          <select
            id="gh-zone"
            className="gh-select"
            value={zoneId}
            onChange={e => setZoneId(e.target.value)}
          >
            <option value="">Alle Zonen</option>
            {auswahlZonen.map(z => (
              <option key={z.zone_id} value={z.zone_id}>{z.name}</option>
            ))}
          </select>
        </div>
        <div className="gh-filter-gruppe">
          <label className="gh-label">Zeitraum</label>
          <div className="gh-preset-leiste" role="tablist">
            {[7, 14, 30].map(t => (
              <button
                key={t}
                type="button"
                role="tab"
                aria-selected={tage === t}
                className={`gh-preset-btn ${tage === t ? 'aktiv' : ''}`}
                onClick={() => setTage(t)}
              >
                {t} Tage
              </button>
            ))}
          </div>
        </div>
      </header>

      {laden && <div className="gh-status">Lädt…</div>}
      {fehler && <div className="gh-fehler">{fehler}</div>}
      {!laden && laeufe !== null && laeufe.length === 0 && (
        <div className="gh-leer">Keine Bewässerung im gewählten Zeitraum.</div>
      )}

      {laeufe !== null && laeufe.length > 0 && (
        <ul className="gh-liste">
          {laeufe.map((lauf, i) => (
            <li
              key={`${lauf.start}-${i}`}
              className={`gh-lauf ${lauf.zaehlt ? '' : 'gh-lauf--ignoriert'}`}
            >
              <div className="gh-kopf">
                <span className="gh-zeit">{datumZeitFormat(lauf.start)}</span>
                <span className="gh-zone">{zonenLabel(lauf.zone_ids)}</span>
                <span className={`gh-methode gh-methode--${lauf.methode}`}>
                  {lauf.methode === 'pre_soak' ? 'Pre-Soak' : 'Einzel'}
                </span>
                <span className="gh-dauer">{formatiereDauer(lauf.dauer_gesamt_s)}</span>
                <span className={`gh-ausloeser gh-ausloeser--${lauf.ausloser}`}>
                  {ausloeserLabel(lauf.ausloser)}
                </span>
                {lauf.laeuft_noch && <span className="gh-laeuft">läuft…</span>}
                {!lauf.zaehlt && (
                  <span className="gh-nichtgezaehlt" title="Cross-Spray / ignoriert — keine echte Zonen-Bewässerung">
                    zählt nicht
                  </span>
                )}
              </div>

              {lauf.methode === 'pre_soak' && lauf.phasen.length > 1 && (
                <div className="gh-phasen">
                  {lauf.phasen.map((p, j) => (
                    <span key={j} className="gh-phase">
                      {phaseLabel(p.phase)} {formatiereDauer(p.dauer_s)}
                      {j < lauf.phasen.length - 1 ? ' · ' : ''}
                    </span>
                  ))}
                </div>
              )}

              {lauf.liter_gesamt != null && (
                <div className="gh-liter">{lauf.liter_gesamt} L</div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
