/* Aggregierte Zonen-Gesundheit — zeigt auf einen Blick wie viele Zonen OK/Trocken/Nass sind.
   Klick auf einen Chip scrollt zur ersten betroffenen Zone. */

import type { Zone, Prognose } from '../typen'
import './StatusLeiste.css'

interface Props {
  zonen: Zone[]
  prognosen: Prognose[]
}

type Status = 'ok' | 'kritisch' | 'giessen_bald' | 'nass' | 'unbekannt'

function klassifiziere(zone: Zone, hatPrognose: boolean): Status {
  if (zone.aktuelle_feuchte === null) return 'unbekannt'
  if (zone.aktuelle_feuchte < zone.feuchte_schwelle_min) return 'kritisch'
  if (zone.aktuelle_feuchte > zone.feuchte_schwelle_max) return 'nass'
  if (hatPrognose) return 'giessen_bald'
  return 'ok'
}

function scrollZuZone(zoneId: string) {
  document.getElementById(`zone-${zoneId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

export function StatusLeiste({ zonen, prognosen }: Props) {
  if (zonen.length === 0) return null

  const prognoseZonen = new Set(prognosen.filter(p => p.bewaesserung_erwartet).map(p => p.zone_id))

  const gruppen: Record<Status, Zone[]> = { ok: [], kritisch: [], giessen_bald: [], nass: [], unbekannt: [] }
  for (const z of zonen) {
    gruppen[klassifiziere(z, prognoseZonen.has(z.zone_id))].push(z)
  }

  const chipDaten: { key: Status; label: string; klasse: string; zonen: Zone[] }[] = [
    { key: 'ok', label: 'OK', klasse: 'ok', zonen: gruppen.ok },
    { key: 'kritisch', label: 'Kritisch', klasse: 'kritisch', zonen: gruppen.kritisch },
    { key: 'giessen_bald', label: 'Giessen bald', klasse: 'giessen-bald', zonen: gruppen.giessen_bald },
    { key: 'nass', label: 'Nass', klasse: 'nass', zonen: gruppen.nass },
    { key: 'unbekannt', label: 'Keine Daten', klasse: 'unbekannt', zonen: gruppen.unbekannt },
  ]

  return (
    <div className="status-leiste">
      {chipDaten.map(({ key, label, klasse, zonen: z }) =>
        z.length > 0 && (
          <span
            key={key}
            className={`status-chip ${klasse}`}
            title={z.map(zone => zone.name).join(', ')}
            onClick={() => scrollZuZone(z[0].zone_id)}
          >
            {z.length} {label}
          </span>
        )
      )}
    </div>
  )
}
