/* KritischBand — oberste Dashboard-Zeile, rendert nur wenn eine Zone
 * unter ihrer kritischen Schwelle liegt ODER die 24h-ML-Prognose unter
 * die Mindest-Schwelle faellt.
 *
 * UX-Review 2026-04-20. "Was ist heute kritisch" in 2 Sekunden erfassbar.
 */

import type { Zone, Prognose, GiessEmpfehlung } from '../typen'
import { ermittleGefahr } from './kritisch-band-logik'
import type { KritischerEintrag } from './kritisch-band-logik'
import './KritischBand.css'

interface Props {
  zonen: Zone[]
  prognosen: Prognose[]
  vorhersagen24h?: Record<string, number>
  // T-0278: Engine-Empfehlung pro Zone. Das KRITISCH-Banner koppelt
  // sich daran, statt allein der rohen ML-24h-Prognose zu vertrauen.
  empfehlungProZone?: Record<string, GiessEmpfehlung>
  onZoneClick?: (zoneId: string) => void
}

export function KritischBand({
  zonen, prognosen, vorhersagen24h, empfehlungProZone, onZoneClick,
}: Props) {
  const eintraege: KritischerEintrag[] = []
  for (const zone of zonen) {
    const ev = ermittleGefahr(
      zone,
      vorhersagen24h?.[zone.zone_id],
      empfehlungProZone?.[zone.zone_id],
    )
    if (ev) eintraege.push(ev)
  }
  if (eintraege.length === 0) return null

  // T-0370: gemessen-kritisch und ML-Prognose-Warnung getrennt zaehlen.
  // Vorher stand "KRITISCH · 2 ZONEN" ueber einem Eintrag, der nur eine
  // 24h-Prognose war -- und widersprach dem Triage-Strip ("1 kritisch").
  // T-0532: Lead-Ausfall ist die dritte Kategorie und darf NICHT in
  // `gemessen` landen -- sonst behauptet die Kopfzeile wieder eine
  // gemessene Schwellenunterschreitung, die die Kachel darunter gerade
  // dementiert. Der Triage-Strip buckt dieselbe Zone als 'unbekannt'.
  const prognose = eintraege.filter(e => e.grund === 'prognose_unter_min').length
  const ohneLead = eintraege.filter(e => e.grund === 'lead_ausgefallen').length
  const gemessen = eintraege.length - prognose - ohneLead
  const titelTeile: string[] = []
  if (gemessen > 0) titelTeile.push(`${gemessen} ${gemessen === 1 ? 'Zone' : 'Zonen'}`)
  if (prognose > 0) titelTeile.push(`${prognose} ML-${prognose === 1 ? 'Warnung' : 'Warnungen'}`)
  if (ohneLead > 0) titelTeile.push(`${ohneLead}× Lead ausgefallen`)

  return (
    <section className="krit-band" aria-label="Kritische Zonen">
      <header className="krit-band-kopf">
        <span className="krit-band-titel">
          <span className="krit-band-dot" aria-hidden />
          {/* T-0532: sind ALLE Eintraege Lead-Ausfaelle, ist nichts kritisch --
              dann darf auch die Kopfzeile das Wort nicht fuehren. */}
          {gemessen + prognose > 0 ? 'Kritisch' : 'Datenlage'} · {titelTeile.join(' · ')}
        </span>
      </header>
      {/* T-0397 (F10b-3): Kacheln nebeneinander statt Full-Width-Zeilen. Vorher
          bekam jede kritische Zone eine 1900px-Zeile mit einem riesigen leeren
          Mittelraum -- das Layout-Primitiv (Liste) war falsch fuer 2 Datenpunkte
          pro Item. Jetzt schmale Kacheln im Auto-Grid (mehrere pro Reihe, gleiche
          Dichte wie die Zonen-Karten), die den Horizontalraum nutzen. Ganze
          Kachel klickbar -> zur Zone. */}
      <div className="krit-band-grid">
        {eintraege.map(e => {
          const prog = prognosen.find(p => p.zone_id === e.zone.zone_id)
          // T-0532: bei Lead-Ausfall ist die Zahl kein Zonen-Wert. Sie ganz
          // wegzulassen waere auch nicht ehrlicher (sie steht ja weiter auf
          // der Karte) -- also bleibt sie stehen, aber mit "?" markiert und
          // ohne die Schwellen-Zeile, die eine Vergleichbarkeit behauptet.
          const ohneLeadQuelle = e.grund === 'lead_ausgefallen'
          const istText = e.zone.aktuelle_feuchte == null
            ? '–'
            : ohneLeadQuelle
              ? `${e.zone.aktuelle_feuchte.toFixed(0)}% ?`
              : `${e.zone.aktuelle_feuchte.toFixed(0)}%`
          return (
            <button
              key={e.zone.zone_id}
              type="button"
              className={`krit-band-kachel grund-${e.grund}`}
              onClick={() => onZoneClick?.(e.zone.zone_id)}
              title={prog?.begruendung ?? 'Zur Zone springen'}
            >
              <div className="kbk-kopf">
                <span className="krit-band-zeilendot" aria-hidden />
                <span className="kbk-name">{e.zone.name}</span>
                <span className="kbk-ist">{istText}</span>
              </div>
              {ohneLeadQuelle ? (
                <div className="kbk-schwelle">Quelle nicht der Lead-Sensor</div>
              ) : (
                <div className="kbk-schwelle">
                  Schwelle {e.zone.feuchte_schwelle_min}%
                  {e.prognose24h != null && (
                    <> · 24 h {e.prognose24h.toFixed(0)}%</>
                  )}
                </div>
              )}
              <div className="kbk-sub">{e.text}</div>
            </button>
          )
        })}
      </div>
    </section>
  )
}
