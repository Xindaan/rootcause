/* T-0227: Tagesplan-Vorschau "heute" / "morgen".
 *
 * Kompakte Liste: was wird das System heute/morgen tun, ueber alle
 * Zonen aggregiert. Zeigt geplante Zeit + Dauer + Strategie +
 * Wetter-Highlight. Bewaesserungs-Bedarf vorne, kein_bedarf am Ende.
 *
 * Datenquelle: /api/tagesplan (5-min-Polling).
 */

import { useEffect, useState } from 'react'
import { holeTagesplan } from '../api'
import { istAbbruch, strategieKlartext } from '../hilfsfunktionen'
import type { Tagesplan, Zone } from '../typen'
import './TagesplanBlock.css'

interface Props {
  /** T-0259-Bug-Fix (26.05.): wenn ein Ventil gerade laeuft (auto-Loop,
   *  manuell, externe Gardena-App), zeigt der Tagesplan eine
   *  "Jetzt laeuft"-Zeile oben. Realfall 26.05.: Hecke wurde manuell
   *  gegossen (09:46-09:55), UI hat das nirgendwo angezeigt. */
  aktiveZonen?: Set<string>
  /** Zonen-Liste, um aktiveZonen-IDs in Zonen-Namen aufzuloesen. */
  zonen?: Zone[]
}

function formatTimeOnly(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleTimeString('de-DE', {
      hour: '2-digit', minute: '2-digit',
    })
  } catch {
    return iso
  }
}

function typKlasse(typ: string): string {
  if (typ === 'akut') return 'tp-typ-akut'
  if (typ === 'praeventiv') return 'tp-typ-praeventiv'
  if (typ === 'wohlfuehl_grenze') return 'tp-typ-wohlfuehl'
  if (typ === 'fehler') return 'tp-typ-fehler'
  return 'tp-typ-kein'
}

function typLabel(typ: string): string {
  if (typ === 'akut') return 'akut'
  if (typ === 'praeventiv') return 'präventiv'
  if (typ === 'wohlfuehl_grenze') return 'wohl-grenze'
  if (typ === 'kein_bedarf') return 'kein Bedarf'
  if (typ === 'fehler') return 'Fehler'
  return typ
}

/** T-0370: Redundanz aus dem Backend-`grund` strippen. Die Zeile zeigt
 *  Typ-Badge + Dauer schon strukturiert -- der grund-Text beginnt aber
 *  oft mit exakt denselben Angaben ("praeventiv — 17 min fuer 4 Tage
 *  Reserve" neben Badge "präventiv" + "17 min"). Sichtbar bleibt nur
 *  der Informations-Rest; der volle Text haengt im title-Tooltip. */
function grundOhneRedundanz(
  grund: string, typ: string, dauerMin: number | null,
): string {
  let t = grund.trim()
  for (const prefix of [typ, typLabel(typ)]) {
    if (prefix && t.toLowerCase().startsWith(prefix.toLowerCase())) {
      t = t.slice(prefix.length).replace(/^\s*[—–-]+\s*/, '')
    }
  }
  if (dauerMin != null) {
    const m = t.match(/^(\d+)\s*min\s+/)
    if (m && Number(m[1]) === dauerMin) t = t.slice(m[0].length)
  }
  return t
}

export function TagesplanBlock({ aktiveZonen, zonen }: Props = {}) {
  const [tag, setTag] = useState<'heute' | 'morgen'>('heute')
  const [plan, setPlan] = useState<Tagesplan | null>(null)
  const [fehler, setFehler] = useState<string | null>(null)

  useEffect(() => {
    const ctrl = new AbortController()
    const laden = () => {
      holeTagesplan(tag, ctrl.signal)
        .then(p => { setPlan(p); setFehler(null) })
        .catch(e => { if (!istAbbruch(e)) setFehler(e.message ?? String(e)) })
    }
    laden()
    const t = setInterval(laden, 5 * 60_000)
    return () => { ctrl.abort(); clearInterval(t) }
  }, [tag])

  if (fehler) {
    return (
      <div className="tagesplan-block tagesplan-fehler">
        Tagesplan konnte nicht geladen werden: {fehler}
      </div>
    )
  }
  if (!plan) {
    return <div className="tagesplan-block">Lade Tagesplan…</div>
  }

  // T-0257-Bug-Fix (26.05.): drei Kategorien statt zwei.
  // Vorher hat das UI `!soll_bewaessern` als "kein Bedarf" gelesen --
  // das stimmt nicht: eine Zone mit `empfehlungs_typ='akut'` UND
  // `soll_bewaessern=false` (z.B. ausserhalb Zeitfenster, Regen erwartet,
  // Modus monitoring) hatte realen Bedarf, wurde aber als "kein Bedarf"
  // gelistet. Realfall 26.05.: Yogaraum praeventiv + Hecke akut landeten
  // in "14 Zonen ohne Bedarf", obwohl 2 davon Bedarf hatten.
  // Korrekte Trennung: `empfehlungs_typ` ist die Bedarfs-Klassifikation,
  // `soll_bewaessern` ist die End-Aktion (kann durch Blocker negiert sein).
  const istBedarf = (typ: string) =>
    typ === 'akut' || typ === 'praeventiv' || typ === 'wohlfuehl_grenze'
  const bewaessernd = plan.eintraege.filter(e => e.soll_bewaessern)
  const bedarf_blockiert = plan.eintraege.filter(
    e => !e.soll_bewaessern && istBedarf(e.empfehlungs_typ),
  )
  const ohne_bedarf = plan.eintraege.filter(
    e => !e.soll_bewaessern && !istBedarf(e.empfehlungs_typ),
  )

  return (
    <div className="tagesplan-block">
      <header className="tp-header">
        <h3>Tagesplan</h3>
        <div className="tp-toggle" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={tag === 'heute'}
            className={`tp-toggle-btn ${tag === 'heute' ? 'aktiv' : ''}`}
            onClick={() => setTag('heute')}
          >
            Heute
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tag === 'morgen'}
            className={`tp-toggle-btn ${tag === 'morgen' ? 'aktiv' : ''}`}
            onClick={() => setTag('morgen')}
          >
            Morgen
          </button>
        </div>
        {plan.wetter_pro_standort.length > 0 && (
          <div className="tp-wetter">
            {plan.wetter_pro_standort.map(w => (
              <span key={w.standort_id} className="tp-wetter-eintrag">
                {/* T-0397 (F20): rohe standort_id (z.B. "garten") ist ein
                    Stadtname -> Erstbuchstabe gross statt Roh-ID beim Nutzer. */}
                <strong>{w.standort_id.charAt(0).toUpperCase() + w.standort_id.slice(1)}</strong>:{' '}
                {w.min_temp_c !== null && w.max_temp_c !== null
                  ? `${w.min_temp_c.toFixed(0)}–${w.max_temp_c.toFixed(0)} °C`
                  : '— °C'}
                {w.regen_summe_mm > 0.3 && (
                  <> · {w.regen_summe_mm.toFixed(1)} mm Regen</>
                )}
              </span>
            ))}
          </div>
        )}
      </header>

      {/* T-0259: Live-Lauf-Indikator. Quelle ist /api/ventil-status
          (Set kommt von App.tsx, deckt eigener Auto-Loop + externe
          Gardena-App + manuelle UI-Aktion ab). */}
      {aktiveZonen && aktiveZonen.size > 0 && (
        <div className="tp-live">
          <span className="tp-live-dot" aria-hidden />
          <span className="tp-live-label">Jetzt läuft:</span>
          {Array.from(aktiveZonen).map(zid => {
            const zone = zonen?.find(z => z.zone_id === zid)
            return (
              <span key={zid} className="tp-live-zone">
                {zone?.name ?? zid}
              </span>
            )
          })}
        </div>
      )}

      {bewaessernd.length > 0 ? (
        <ul className="tp-liste">
          {bewaessernd.map(e => (
            <li key={e.zone_id} className="tp-eintrag">
              <span className="tp-zeit">{formatTimeOnly(e.geplante_zeit)}</span>
              <span className="tp-zone">{e.zone_name}</span>
              <span className={`tp-typ ${typKlasse(e.empfehlungs_typ)}`}>
                {typLabel(e.empfehlungs_typ)}
              </span>
              {e.dauer_min !== null && (
                <span className="tp-dauer">{e.dauer_min} min</span>
              )}
              {/* T-0295: Plateau-Transparenz -- die Dauer ist eine
                  plateau-begrenzte Einzeldose; bei dosen_bis_ziel > 1
                  erreicht sie das Ziel NICHT in einem Lauf (sonst wirkt
                  "X min fuer N Tage Reserve" irrefuehrend). */}
              {e.dosen_bis_ziel !== null && e.dosen_bis_ziel > 1
                && e.erwarteter_endwert_pp !== null && (
                <span
                  className="tp-grund"
                  title={`Plateau-begrenzt: ${e.dauer_min ?? '?'} min erreicht `
                    + `~${Math.round(e.erwarteter_endwert_pp)} %, Ziel braucht `
                    + `~${e.dosen_bis_ziel} Dosen`}
                >
                  → ~{Math.round(e.erwarteter_endwert_pp)} % (~{e.dosen_bis_ziel} Dosen)
                </span>
              )}
              <span className="tp-strat">{strategieKlartext(e.aktive_strategie)}</span>
              {e.exklusiv && (
                <span className="tp-konflikt" title="Druckabhaengig, kein Parallel-Lauf">
                  exklusiv
                </span>
              )}
              {e.grund && (
                <span className="tp-grund" title={e.grund}>
                  {grundOhneRedundanz(e.grund, e.empfehlungs_typ, e.dauer_min)}
                </span>
              )}
            </li>
          ))}
        </ul>
      ) : bedarf_blockiert.length > 0 ? (
        <p className="tp-leer">
          {tag === 'heute' ? 'Heute' : 'Morgen'} keine Bewässerung geplant
          {' — '}
          <strong>{bedarf_blockiert.length}</strong>{' '}
          {bedarf_blockiert.length === 1 ? 'Zone hat' : 'Zonen haben'} Bedarf,
          aber blockiert (siehe unten).
        </p>
      ) : (
        <p className="tp-leer">
          {tag === 'heute' ? 'Heute' : 'Morgen'} keine Bewässerung geplant.
        </p>
      )}

      {/* T-0257: Bedarf erkannt, aber Aktion blockiert (Zeitfenster,
          Regen, Tagesbudget, Monitoring-Modus). Separate Sektion oben
          weil "Bedarf" unterschiedlich ist von "kein Bedarf". */}
      {bedarf_blockiert.length > 0 && (
        <details className="tp-details tp-details-bedarf" open>
          <summary>
            <strong>{bedarf_blockiert.length}</strong>{' '}
            {bedarf_blockiert.length === 1 ? 'Zone' : 'Zonen'} mit
            Bedarf (blockiert)
          </summary>
          <ul className="tp-liste tp-liste-bedarf">
            {bedarf_blockiert.map(e => (
              <li key={e.zone_id} className="tp-eintrag tp-eintrag-bedarf">
                <span className="tp-zone">{e.zone_name}</span>
                <span className={`tp-typ ${typKlasse(e.empfehlungs_typ)}`}>
                  {typLabel(e.empfehlungs_typ)}
                </span>
                {e.dauer_min !== null && e.dauer_min > 0 && (
                  <span className="tp-dauer">{e.dauer_min} min</span>
                )}
                {e.grund && (
                  <span className="tp-grund" title={e.grund}>
                    {grundOhneRedundanz(e.grund, e.empfehlungs_typ, e.dauer_min)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}

      {ohne_bedarf.length > 0 && (
        <details className="tp-details">
          <summary>
            {ohne_bedarf.length} Zone{ohne_bedarf.length !== 1 ? 'n' : ''}{' '}
            ohne Bedarf
          </summary>
          <ul className="tp-liste tp-liste-kein">
            {ohne_bedarf.map(e => (
              <li key={e.zone_id} className="tp-eintrag tp-eintrag-kein">
                <span className="tp-zone">{e.zone_name}</span>
                <span className={`tp-typ ${typKlasse(e.empfehlungs_typ)}`}>
                  {typLabel(e.empfehlungs_typ)}
                </span>
                {e.tage_bis_welkepunkt !== null && (
                  <small className="tp-reserve">
                    Reserve {e.tage_bis_welkepunkt.toFixed(1)}d
                  </small>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
