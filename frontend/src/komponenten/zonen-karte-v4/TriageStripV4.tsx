/* =========================================================
   TriageStripV4 -- Obs02 (T-0320). Persistenter, STICKY Triage-Strip ueber
   allen Standort-Sektionen: "was braucht mich jetzt?" ueber ALLE Zonen
   (alle Standorte) hinweg, in einer Zeile -- die Bruecke zwischen Karte
   (Detail) und Dashboard (Ueberblick).

   Fork von StatusLeiste (V3 bleibt unberuehrt) mit drei Erweiterungen aus
   dem UX-Review:
     - STICKY: bleibt beim Scrollen oben, "alles im Korridor?" jederzeit
       ohne Scroll beantwortbar.
     - Chips "gießt gerade" (laufende Bewaesserung) + "Kalibrierung" --
       in der alten StatusLeiste nicht vorhanden.
     - Ruhe-Zustand: ist nichts offen, schrumpft der Strip auf eine ruhige
       gruene Zeile "N Zonen · alle im Korridor".
   Klick auf einen Chip scrollt zur ersten betroffenen Zone.
   ========================================================= */

import type { Zone, Prognose } from '../../typen'
import { triageGruppen, type TriageBucket as Bucket } from './triage-logik'

interface Props {
  zonen: Zone[]
  prognosen: Prognose[]
  /** Zone-IDs mit aktuell offenem Ventil (aus App.tsx ventilAktiv). */
  bewaesserungAktiv: Set<string>
  /** T-0601: Uhr fuer das Alterslimit des Ist-Werts (App-Takt 30 s). */
  jetzt: number
  onZoneClick?: (zoneId: string) => void
}

/** Aktive Sensor-Kalibrierung? */
function inKalibrierung(zone: Zone, jetzt: number): boolean {
  return (zone.ausschluss_fenster ?? []).some(f => {
    const zweck = f.zweck ?? 'sensor_kalibrierung'
    if (zweck !== 'sensor_kalibrierung') return false
    return new Date(f.von).getTime() <= jetzt && jetzt <= new Date(f.bis).getTime()
  })
}

const CHIP_REIHENFOLGE: { key: Bucket; label: string }[] = [
  { key: 'kritisch',   label: 'kritisch' },
  { key: 'giesst',     label: 'gießt gerade' },
  { key: 'empfehlung', label: 'Empfehlung offen' },
  { key: 'nass',       label: 'zu nass' },
  { key: 'korridor',   label: 'im Korridor' },
  { key: 'unbekannt',  label: 'keine Daten' },
]

export function TriageStripV4({ zonen, prognosen, bewaesserungAktiv, jetzt, onZoneClick }: Props) {
  if (zonen.length === 0) return null

  const empfZonen = new Set(
    prognosen.filter(p => p.bewaesserung_erwartet).map(p => p.zone_id),
  )
  // T-0610 Q6: kritisch vor giesst; kritische Zonen mit offenem Ventil
  // tragen die Zusatzmarke "gießt" am kritisch-Chip.
  const { gruppen, kritischGiesst } = triageGruppen(zonen, bewaesserungAktiv, empfZonen, jetzt)
  const kritischGiesstIds = new Set(kritischGiesst.map(z => z.zone_id))
  const kalibrierZonen = zonen.filter(z => inKalibrierung(z, jetzt))

  const sprungZu = (zs: Zone[]) => { if (zs[0]) onZoneClick?.(zs[0].zone_id) }

  // Ruhe-Zustand: nichts offen ausser dem Korridor -> eine ruhige Zeile.
  const offen = gruppen.kritisch.length + gruppen.giesst.length
    + gruppen.empfehlung.length + gruppen.nass.length + gruppen.unbekannt.length
    + kalibrierZonen.length
  if (offen === 0) {
    return (
      <div className="triage-v4 triage-v4--ruhig" role="status">
        <span className="triage-v4__dot" aria-hidden />
        {zonen.length} Zonen · alle im Korridor
      </div>
    )
  }

  return (
    <div className="triage-v4" role="group" aria-label="Triage über alle Zonen">
      {CHIP_REIHENFOLGE.map(({ key, label }) => {
        const zs = gruppen[key]
        if (zs.length === 0) return null
        return (
          <button
            key={key}
            type="button"
            className={`triage-v4__chip triage-v4__chip--${key}`}
            title={zs.map(z => (kritischGiesstIds.has(z.zone_id) ? `${z.name} (gießt gerade)` : z.name)).join(', ')}
            onClick={() => sprungZu(zs)}
          >
            <span className="triage-v4__n">{zs.length}</span> {label}
            {key === 'kritisch' && kritischGiesst.length > 0 && (
              <span className="triage-v4__marke"> · {kritischGiesst.length} gießt</span>
            )}
          </button>
        )
      })}
      {kalibrierZonen.length > 0 && (
        <button
          type="button"
          className="triage-v4__chip triage-v4__chip--kalibrierung triage-v4__chip--rechts"
          title={kalibrierZonen.map(z => z.name).join(', ')}
          onClick={() => sprungZu(kalibrierZonen)}
        >
          <span className="triage-v4__n">{kalibrierZonen.length}</span> Kalibrierung
        </button>
      )}
    </div>
  )
}
