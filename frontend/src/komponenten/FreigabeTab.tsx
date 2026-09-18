/* T-0236: Freigabe-/Vertrauens-Tab.
 *
 * Konkrete Voraussetzung fuer T-0021 (ventilsteuerung_aktiv=true).
 * Vor dem Scharfschalten will der User pro Zone sehen:
 * - Hat die System-Empfehlung in der Vergangenheit tatsaechlich gepasst?
 * - Wie genau war die kausale 6h/24h-Prognose (MAE)?
 * - Heuristik vs. ML Dauer-Empfehlung -- welches Modell ist besser?
 * - Reife-Grad-Hinweis: hat die Zone genug Stichproben?
 *
 * Datenquellen:
 * - /api/empfehlungs-audit (T-0122 EmpfehlungsAuditJob): Snapshot pro
 *   Empfehlung + ist-Werte nach 6/24h evaluiert.
 * - /api/ml/dauer-drift (T-0065): MAE-Vergleich Heuristik vs. ML.
 *
 * Beide Endpoints existieren seit Monaten ohne UI -- T-0236 schliesst
 * die Visualisierungs-Luecke.
 */

import { useCallback, useEffect, useState } from 'react'
import {
  holeEmpfehlungsAudit,
  holeMlDauerDrift,
} from '../api'
import { istAbbruch } from '../hilfsfunktionen'
import type {
  DauerDriftAntwort,
  EmpfehlungsAuditAntwort,
  EmpfehlungsAuditEintrag,
  Zone,
} from '../typen'
import './FreigabeTab.css'

interface Props {
  zonen: Zone[]
}

type Fenster = 7 | 30 | 90

/** T-0397 (F19): empfehlungs_typ in Klartext. Backend liefert ihn mal
 *  gross (typ_verteilung: KEIN_BEDARF), mal klein (Audit-Trail: kein_bedarf)
 *  -> case-insensitiv. Vorher standen roh 292x "kein_bedarf" im DOM. */
function typLabel(typ: string): string {
  switch (typ.toLowerCase()) {
    case 'akut':            return 'akut'
    case 'praeventiv':      return 'präventiv'
    case 'wohlfuehl_grenze': return 'Wohlfühl-Grenze'
    case 'kein_bedarf':     return 'kein Bedarf'
    case 'fehler':          return 'Fehler'
    default:                return typ
  }
}

function ampelKlasse(amp: string): string {
  if (amp === 'gruen') return 'fg-ampel-gruen'
  if (amp === 'gelb') return 'fg-ampel-gelb'
  if (amp === 'rot') return 'fg-ampel-rot'
  if (amp === 'nur_heuristik') return 'fg-ampel-info'
  return 'fg-ampel-grau'
}

// Wird NUR im Tooltip der Reife-Pill mit eingeblendet, nicht im
// sichtbaren <p>. Schaerft die Abgrenzung zur Dauer-Modell-Pill, die
// nebenan in derselben Header-Zeile steht und sonst leicht als
// "dasselbe nochmal" gelesen wird.
const REIFE_TOOLTIP_KONTEXT =
  'Misst Feuchte-Prognose-Fehler nach 6 h. Davon haengt die Empfehlung "giessen ja/nein" ab.'

function reifegradLabel(stats: EmpfehlungsAuditAntwort['stats'], mae6: number | null): {
  label: string
  klasse: string
  begruendung: string
} {
  const n_ev = stats.n_evaluiert
  if (n_ev < 5) {
    return {
      label: 'Zu wenig Daten',
      klasse: 'fg-reife-noch',
      begruendung: `Nur ${n_ev} evaluierte Empfehlungen -- mindestens 5 noetig fuer Aussagekraft.`,
    }
  }
  if (mae6 === null) {
    return {
      label: 'Nicht messbar',
      klasse: 'fg-reife-noch',
      begruendung: 'Keine 6h-Abweichung berechenbar.',
    }
  }
  if (n_ev >= 20 && mae6 < 5) {
    return {
      label: 'Reif fuer Auto',
      klasse: 'fg-reife-reif',
      begruendung: `${n_ev} Stichproben, 6h-MAE ${mae6.toFixed(1)}pp < 5pp -- Empfehlungen sind konsistent.`,
    }
  }
  if (mae6 > 10) {
    return {
      label: 'Drift hoch',
      klasse: 'fg-reife-rot',
      begruendung: `6h-MAE ${mae6.toFixed(1)}pp -- Empfehlung weicht systematisch ab. Erst Modell pruefen.`,
    }
  }
  return {
    label: 'Beobachten',
    klasse: 'fg-reife-mittel',
    begruendung: `${n_ev} Stichproben, 6h-MAE ${mae6.toFixed(1)}pp. Mehr Daten oder Drift reduzieren.`,
  }
}

/** Erklaert den Dauer-Modell-Ampel-Wert. Wichtigste Botschaft:
 *  rot ist KEIN Blocker fuer ventilsteuerung_aktiv=true, weil der
 *  Auto-Modus die Heuristik-Dauer nimmt -- ML ist Diagnose-Vergleich. */
function driftTooltip(d: { mae_heuristik: number | null; mae_ml: number | null; ampel: string }): string {
  const mh = d.mae_heuristik?.toFixed(1) ?? '—'
  const mm = d.mae_ml?.toFixed(1) ?? '—'
  const kopf = `Heuristik-Dauer-MAE ${mh} pp vs ML ${mm} pp.`
  let kommentar = ''
  if (d.ampel === 'gruen') {
    kommentar = 'ML mind. 50 % besser als Heuristik.'
  } else if (d.ampel === 'gelb') {
    kommentar = 'ML zwischen 50 und 80 % der Heuristik -- ok, aber knapp.'
  } else if (d.ampel === 'rot') {
    kommentar = 'ML schlechter als Heuristik. Auto-Modus nutzt die Heuristik -- KEIN Blocker fuer Scharfschalten.'
  } else if (d.ampel === 'nur_heuristik') {
    kommentar = 'Noch kein ML-Vergleich verfuegbar.'
  } else {
    kommentar = 'Keine bewerteten Daten im Fenster.'
  }
  return `${kopf} ${kommentar}`
}

function formatDatum(iso: string): string {
  try {
    return new Date(iso).toLocaleString('de-DE', {
      day: '2-digit', month: '2-digit',
      hour: '2-digit', minute: '2-digit',
    })
  } catch {
    return iso
  }
}

function formatAbw(wert: number | null | undefined): string {
  if (wert === null || wert === undefined) return '—'
  const sign = wert >= 0 ? '+' : ''
  return `${sign}${wert.toFixed(1)}pp`
}

export function FreigabeTab({ zonen }: Props) {
  const [fenster, setFenster] = useState<Fenster>(30)
  const [zoneFilter, setZoneFilter] = useState<string>('')
  const [audit, setAudit] = useState<EmpfehlungsAuditAntwort | null>(null)
  const [drift, setDrift] = useState<DauerDriftAntwort | null>(null)
  const [fehler, setFehler] = useState<string | null>(null)
  // Startet als `true`: der Effect laedt sofort beim Mount. Frueher stand
  // dafuer ein `setLaedt(true)` synchron im `laden()`-Rumpf -- das erzwang
  // einen Extra-Render. Beim Filterwechsel aendert der Wegfall nichts: der
  // Ladehinweis haengt an `laedt && !audit`, und `audit` traegt dann noch die
  // alten Daten, der Hinweis erschien also auch vorher nicht.
  const [laedt, setLaedt] = useState(true)

  const laden = useCallback(() => {
    const ctrl = new AbortController()
    Promise.all([
      holeEmpfehlungsAudit(fenster, zoneFilter || undefined, ctrl.signal),
      holeMlDauerDrift(`${fenster}d`, zoneFilter || undefined, ctrl.signal),
    ])
      .then(([a, d]) => { setAudit(a); setDrift(d); setFehler(null) })
      .catch(e => {
        if (!istAbbruch(e)) setFehler(e.message ?? String(e))
      })
      .finally(() => setLaedt(false))
    return ctrl
  }, [fenster, zoneFilter])

  useEffect(() => {
    const ctrl = laden()
    return () => ctrl.abort()
  }, [laden])

  const eintraege_pro_zone = audit ? (() => {
    const m: Record<string, EmpfehlungsAuditEintrag[]> = {}
    for (const e of audit.eintraege) {
      ;(m[e.zone_id] ??= []).push(e)
    }
    return m
  })() : {}

  return (
    <section className="freigabe-tab">
      <header className="fg-header">
        <h2>Freigabe &amp; Vertrauen</h2>
        <p className="fg-intro">
          Pro Zone Audit-Trail (Empfehlung vs. tatsaechlicher Sensor-Wert
          nach 6/24 h) + Heuristik-vs-ML-Dauer-Vergleich. Konkrete
          Vorbereitung fuer{' '}
          <code>ventilsteuerung_aktiv: true</code>.
        </p>
        <div className="fg-filter">
          <label>Fenster:&nbsp;
            {[7, 30, 90].map(n => (
              <button
                key={n}
                type="button"
                className={`fg-btn ${fenster === n ? 'aktiv' : ''}`}
                onClick={() => setFenster(n as Fenster)}
              >
                {n}d
              </button>
            ))}
          </label>
          <label>Zone:&nbsp;
            <select
              value={zoneFilter}
              onChange={e => setZoneFilter(e.target.value)}
            >
              <option value="">Alle Zonen</option>
              {zonen.map(z => (
                <option key={z.zone_id} value={z.zone_id}>{z.name}</option>
              ))}
            </select>
          </label>
        </div>
      </header>

      {fehler && <div className="fg-fehler">Fehler: {fehler}</div>}
      {laedt && !audit && <div className="fg-leer">Lade…</div>}

      {audit && (
        <section className="fg-global-stats">
          <h3>Gesamt-Stats ({audit.fenster_tage} Tage)</h3>
          <div className="fg-stats-grid">
            <div className="fg-stat">
              <span className="fg-stat-label">Eintraege</span>
              <span className="fg-stat-wert">{audit.stats.n}</span>
            </div>
            <div className="fg-stat">
              <span className="fg-stat-label">evaluiert</span>
              <span className="fg-stat-wert">{audit.stats.n_evaluiert}</span>
            </div>
            <div className="fg-stat">
              <span className="fg-stat-label">MAE 6h</span>
              <span className="fg-stat-wert">
                {audit.stats.mae_6h_pp !== null ? `${audit.stats.mae_6h_pp}pp` : '—'}
              </span>
            </div>
            <div className="fg-stat">
              <span className="fg-stat-label">MAE 24h</span>
              <span className="fg-stat-wert">
                {audit.stats.mae_24h_pp !== null ? `${audit.stats.mae_24h_pp}pp` : '—'}
              </span>
            </div>
          </div>
          <div className="fg-typ-verteilung">
            {Object.entries(audit.stats.typ_verteilung).map(([typ, n]) => (
              <span key={typ} className={`fg-typ fg-typ-${typ.toLowerCase()}`}>
                {typLabel(typ)}: <strong>{n}</strong>
              </span>
            ))}
          </div>
        </section>
      )}

      {/* Pro Zone: Reife + Dauer-Drift + Audit-Trail.
       *
       * T-0236-Bug-Fix (25.05.): Reife-Stats kommen JETZT aus
       * audit.pro_zone_stats (Backend-SQL-GROUP-BY ueber den ganzen
       * Fenster-Zeitraum). Vorher hat das Frontend MAE selbst aus
       * `eintraege_pro_zone` gerechnet -- bei 14 Zonen reichte das
       * 500er-Limit nur fuer ~1.5 Tage. yogaraum 30d-MAE 8.6 pp wurde
       * als "Reif" gelabelt. */}
      {zonen.map(zone => {
        const eintraege = eintraege_pro_zone[zone.zone_id] ?? []
        const stats = audit?.pro_zone_stats?.[zone.zone_id]
        const reife = reifegradLabel(
          stats ?? {
            n: eintraege.length,
            n_evaluiert: eintraege.filter(e => e.evaluiert_am).length,
            mae_6h_pp: null,
            mae_24h_pp: null,
            typ_verteilung: {},
          },
          stats?.mae_6h_pp ?? null,
        )
        const driftZ = drift?.zonen[zone.zone_id]

        if (!stats && eintraege.length === 0 && !driftZ) return null

        return (
          <section key={zone.zone_id} className="fg-zone">
            <header className="fg-zone-header">
              <h3>{zone.name}</h3>
              <span
                className={`fg-reife ${reife.klasse}`}
                title={`${REIFE_TOOLTIP_KONTEXT}\n\n${reife.begruendung}`}
              >
                {reife.label}
              </span>
              {driftZ && (
                <span
                  className={`fg-ampel ${ampelKlasse(driftZ.ampel)}`}
                  title={driftTooltip(driftZ)}
                >
                  Dauer-Modell: {driftZ.ampel}
                </span>
              )}
            </header>
            <p className="fg-reife-begr">{reife.begruendung}</p>

            {driftZ && (
              <div className="fg-drift-block">
                <span>MAE Heuristik: <strong>{driftZ.mae_heuristik?.toFixed(1) ?? '—'}</strong></span>
                <span>MAE ML: <strong>{driftZ.mae_ml?.toFixed(1) ?? '—'}</strong></span>
                <span className="fg-drift-n">
                  n={driftZ.n_bewertet} (ML: {driftZ.n_ml_bewertet})
                </span>
              </div>
            )}

            {eintraege.length > 0 && (
              <details className="fg-audit-details">
                <summary>Audit-Trail ({eintraege.length} Eintraege)</summary>
                <table className="fg-tabelle">
                  <thead>
                    <tr>
                      <th>Datum</th>
                      <th>Typ</th>
                      <th>Quelle</th>
                      <th>Prog 6h</th>
                      <th>Ist 6h</th>
                      <th>Abw 6h</th>
                      <th>Ist 24h</th>
                      <th>Abw 24h</th>
                    </tr>
                  </thead>
                  <tbody>
                    {eintraege.slice(0, 30).map((e, i) => (
                      <tr key={`${e.zeitstempel}-${i}`}>
                        <td className="fg-zell-dat">{formatDatum(e.zeitstempel)}</td>
                        <td>
                          <span className={`fg-typ fg-typ-${e.empfehlungs_typ.toLowerCase()}`}>
                            {typLabel(e.empfehlungs_typ)}
                          </span>
                        </td>
                        <td className="fg-zell-q">{e.prognose_quelle}</td>
                        <td className="fg-zell-num">{e.prognose_6h?.toFixed(0) ?? '—'}</td>
                        <td className="fg-zell-num">{e.ist_6h?.toFixed(0) ?? '—'}</td>
                        <td className={`fg-zell-num ${(e.abweichung_6h ?? 0) > 5 ? 'fg-abw-hoch' : ''}`}>
                          {formatAbw(e.abweichung_6h)}
                        </td>
                        <td className="fg-zell-num">{e.ist_24h?.toFixed(0) ?? '—'}</td>
                        <td className={`fg-zell-num ${(e.abweichung_24h ?? 0) > 5 ? 'fg-abw-hoch' : ''}`}>
                          {formatAbw(e.abweichung_24h)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {eintraege.length > 30 && (
                  <p className="fg-leer">
                    … {eintraege.length - 30} weitere Eintraege ausgeblendet.
                  </p>
                )}
              </details>
            )}
          </section>
        )
      })}
    </section>
  )
}
