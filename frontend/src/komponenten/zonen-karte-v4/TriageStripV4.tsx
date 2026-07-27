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
import { istWahrscheinlichSensorDefekt } from '../sensor-defekt'

type Bucket = 'kritisch' | 'giesst' | 'empfehlung' | 'nass' | 'korridor' | 'unbekannt'

interface Props {
  zonen: Zone[]
  prognosen: Prognose[]
  /** Zone-IDs mit aktuell offenem Ventil (aus App.tsx ventilAktiv). */
  bewaesserungAktiv: Set<string>
  onZoneClick?: (zoneId: string) => void
}

/** Aktive Sensor-Kalibrierung? `Date.now()` modul-level wegen des
 *  React-Hooks-Purity-Lints (analog pruefeAusschluss in ZonenKarteV4Glance). */
function inKalibrierung(zone: Zone): boolean {
  const jetzt = Date.now()
  return (zone.ausschluss_fenster ?? []).some(f => {
    const zweck = f.zweck ?? 'sensor_kalibrierung'
    if (zweck !== 'sensor_kalibrierung') return false
    return new Date(f.von).getTime() <= jetzt && jetzt <= new Date(f.bis).getTime()
  })
}

/** Primaerer Triage-Zustand einer Zone (mutually exclusive). Kalibrierung ist
 *  orthogonal und wird separat gezaehlt. */
function bucketVon(zone: Zone, giesst: boolean, hatEmpfehlung: boolean): Bucket {
  if (giesst) return 'giesst'
  const ist = zone.aktuelle_feuchte
  // T-0390: null ODER 0.0-Defekt (Gardena/unbekannt) = keine verlaesslichen
  // Daten -> nicht als "kritisch" buckten (Konsistenz mit KritischBand/V4-Karte).
  if (ist == null || istWahrscheinlichSensorDefekt(zone)) return 'unbekannt'
  // T-0381: kein Fallback auf schwelle_min -- das buckete eine Zone knapp unter
  // min faelschlich als "kritisch". Fehlt feuchte_kritisch, gibt es keinen
  // kritisch-Bucket (Backend setzt es real fuer alle Zonen; Guard defensiv).
  const kritisch = zone.feuchte_kritisch
  if (kritisch != null && ist < kritisch) return 'kritisch'
  if (ist > zone.feuchte_schwelle_max) return 'nass'
  if (ist < zone.feuchte_schwelle_min || hatEmpfehlung) return 'empfehlung'
  return 'korridor'
}

const CHIP_REIHENFOLGE: { key: Bucket; label: string }[] = [
  { key: 'kritisch',   label: 'kritisch' },
  { key: 'giesst',     label: 'gießt gerade' },
  { key: 'empfehlung', label: 'Empfehlung offen' },
  { key: 'nass',       label: 'zu nass' },
  { key: 'korridor',   label: 'im Korridor' },
  { key: 'unbekannt',  label: 'keine Daten' },
]

export function TriageStripV4({ zonen, prognosen, bewaesserungAktiv, onZoneClick }: Props) {
  if (zonen.length === 0) return null

  const empfZonen = new Set(
    prognosen.filter(p => p.bewaesserung_erwartet).map(p => p.zone_id),
  )
  const gruppen: Record<Bucket, Zone[]> = {
    kritisch: [], giesst: [], empfehlung: [], nass: [], korridor: [], unbekannt: [],
  }
  const kalibrierZonen: Zone[] = []
  for (const z of zonen) {
    const giesst = bewaesserungAktiv.has(z.zone_id)
    gruppen[bucketVon(z, giesst, empfZonen.has(z.zone_id))].push(z)
    if (inKalibrierung(z)) kalibrierZonen.push(z)
  }

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
            title={zs.map(z => z.name).join(', ')}
            onClick={() => sprungZu(zs)}
          >
            <span className="triage-v4__n">{zs.length}</span> {label}
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
