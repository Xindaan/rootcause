import type { OpsSeverityFilter, Zone } from '../typen'

const SEVERITY_OPTIONEN: Array<{ wert: OpsSeverityFilter; label: string }> = [
  { wert: 'kritisch', label: 'Kritisch' },
  { wert: 'aktion', label: 'Aktion' },
  { wert: 'wetter', label: 'Wetter' },
  { wert: 'routine', label: 'Routine' },
]

interface Props {
  zonen: Zone[]
  stunden: number
  zoneId: string
  severity: OpsSeverityFilter[]
  eintraegeAnzahl?: number
  onStundenChange: (stunden: number) => void
  onZoneChange: (zoneId: string) => void
  onToggleSeverity: (severity: OpsSeverityFilter) => void
}

export function OpsFilter({
  zonen,
  stunden,
  zoneId,
  severity,
  eintraegeAnzahl,
  onStundenChange,
  onZoneChange,
  onToggleSeverity,
}: Props) {
  return (
    <section className="ops-filter">
      <div className="ops-filter-block">
        <label htmlFor="ops-stunden">Zeitraum</label>
        <select
          id="ops-stunden"
          value={stunden}
          onChange={event => onStundenChange(Number(event.target.value))}
        >
          <option value={24}>24h</option>
          <option value={48}>48h</option>
          <option value={72}>72h</option>
        </select>
        {eintraegeAnzahl !== undefined && (
          <span className="ops-filter-meta">
            {eintraegeAnzahl} {eintraegeAnzahl === 1 ? 'Eintrag' : 'Eintraege'}
          </span>
        )}
      </div>

      <div className="ops-filter-block">
        <label htmlFor="ops-zone">Zone</label>
        <select
          id="ops-zone"
          value={zoneId}
          onChange={event => onZoneChange(event.target.value)}
        >
          <option value="">Alle Zonen</option>
          {zonen.map(zone => (
            <option key={zone.zone_id} value={zone.zone_id}>
              {zone.name}
            </option>
          ))}
        </select>
      </div>

      <div className="ops-filter-block ops-filter-severity">
        <span>Severity</span>
        <div className="ops-severity-liste">
          {SEVERITY_OPTIONEN.map(option => {
            const aktiv = severity.includes(option.wert)
            return (
              <button
                key={option.wert}
                type="button"
                className={`ops-severity-chip ${aktiv ? 'aktiv' : ''}`}
                onClick={() => onToggleSeverity(option.wert)}
              >
                {option.label}
              </button>
            )
          })}
        </div>
      </div>
    </section>
  )
}
