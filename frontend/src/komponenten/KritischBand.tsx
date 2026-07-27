/* KritischBand — oberste Dashboard-Zeile, rendert nur wenn eine Zone
 * unter ihrer kritischen Schwelle liegt ODER die 24h-ML-Prognose unter
 * die Mindest-Schwelle faellt.
 *
 * UX-Review 2026-04-20. "Was ist heute kritisch" in 2 Sekunden erfassbar.
 */

import type { Zone, Prognose, GiessEmpfehlung } from '../typen'
import { istWahrscheinlichSensorDefekt } from './sensor-defekt'
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

type GefahrGrund = 'unter_kritisch' | 'unter_min' | 'prognose_unter_min'

interface KritischerEintrag {
  zone: Zone
  grund: GefahrGrund
  text: string
  prognose24h?: number
}

function ermittleGefahr(
  zone: Zone,
  prognose24h: number | undefined,
  empf: GiessEmpfehlung | undefined,
): KritischerEintrag | null {
  // Defekt-Hinweise nicht als Kritisch melden -- Daten unzuverlaessig.
  if (istWahrscheinlichSensorDefekt(zone)) {
    return null
  }
  const ist = zone.aktuelle_feuchte
  const kritisch = zone.feuchte_kritisch
  // Echter Ist-Wert unter kritischer Schwelle: immer KRITISCH, das ist
  // ein gemessenes Faktum, keine Prognose.
  if (ist != null && kritisch != null && ist < kritisch) {
    return {
      zone,
      grund: 'unter_kritisch',
      text: `kritische Schwelle ${kritisch}% unterschritten`,
    }
  }
  // T-0278: Prognose-getriebene Warnung NUR wenn die Engine zustimmt.
  // Vorher feuerte das Banner allein auf `prognose24h < schwelle_min`
  // (rohe ML), auch wenn die Engine `kein_bedarf`/`FEUCHTE_OK` sagte
  // (Fehlalarm waldblumen 30.05.: ML 33%, Engine kein_bedarf, Physik
  // ~7d Reserve). Wenn keine Empfehlung vorliegt (V0-Tab ohne Snapshot),
  // faellt der Prognose-Zweig komplett weg -- besser kein Alarm als
  // Fehlalarm.
  if (prognose24h != null && prognose24h < zone.feuchte_schwelle_min) {
    if (empf == null) {
      // Keine Engine-Info: rohe ML allein reicht nicht fuer KRITISCH.
      return null
    }
    const engineSiehtBedarf =
      empf.soll_bewaessern
      || empf.empfehlungs_typ === 'akut'
      || empf.empfehlungs_typ === 'praeventiv'
    if (!engineSiehtBedarf) {
      // ML-Prognose alarmiert, Engine widerspricht (z.B. FEUCHTE_OK).
      // Kein KRITISCH-Eintrag -- das ist genau der Fehlalarm-Fall.
      return null
    }
    return {
      zone,
      grund: 'prognose_unter_min',
      text: `ML sagt ${prognose24h.toFixed(0)}% in 24 h (unter ${zone.feuchte_schwelle_min}%)`,
      prognose24h,
    }
  }
  return null
}

/** T-0397 (F10b): Zone-IDs, die das KRITISCH-Band zeigt -- damit der Heute-
 *  Plan sie ausblenden kann (sonst dieselben Zonen doppelt direkt untereinander,
 *  "Schwelle unterschritten" = kein echter Tages-Plan). Single Source: gleiche
 *  `ermittleGefahr`-Logik wie die Band-Anzeige. */
export function ermittleKritischeZoneIds(
  zonen: Zone[],
  vorhersagen24h?: Record<string, number>,
  empfehlungProZone?: Record<string, GiessEmpfehlung>,
): Set<string> {
  const ids = new Set<string>()
  for (const zone of zonen) {
    if (ermittleGefahr(zone, vorhersagen24h?.[zone.zone_id], empfehlungProZone?.[zone.zone_id])) {
      ids.add(zone.zone_id)
    }
  }
  return ids
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
  const gemessen = eintraege.filter(e => e.grund !== 'prognose_unter_min').length
  const prognose = eintraege.length - gemessen
  const titelTeile: string[] = []
  if (gemessen > 0) titelTeile.push(`${gemessen} ${gemessen === 1 ? 'Zone' : 'Zonen'}`)
  if (prognose > 0) titelTeile.push(`${prognose} ML-${prognose === 1 ? 'Warnung' : 'Warnungen'}`)

  return (
    <section className="krit-band" aria-label="Kritische Zonen">
      <header className="krit-band-kopf">
        <span className="krit-band-titel">
          <span className="krit-band-dot" aria-hidden />
          Kritisch · {titelTeile.join(' · ')}
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
          const istText = e.zone.aktuelle_feuchte != null
            ? `${e.zone.aktuelle_feuchte.toFixed(0)}%`
            : '–'
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
              <div className="kbk-schwelle">
                Schwelle {e.zone.feuchte_schwelle_min}%
                {e.prognose24h != null && (
                  <> · 24 h {e.prognose24h.toFixed(0)}%</>
                )}
              </div>
              <div className="kbk-sub">{e.text}</div>
            </button>
          )
        })}
      </div>
    </section>
  )
}
