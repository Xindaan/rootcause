import type { OpsTimelineEintrag as OpsTimelineEintragTyp, Zone } from '../typen'
import { OpsTimelineEintrag } from './OpsTimelineEintrag'

interface Props {
  eintraege: OpsTimelineEintragTyp[]
  routineUnterdrueckt: number
  zonen: Zone[]
  onAktualisiert?: () => void
}

export function OpsTimeline({
  eintraege, routineUnterdrueckt, zonen, onAktualisiert,
}: Props) {
  const zonenMap = Object.fromEntries(zonen.map(zone => [zone.zone_id, zone.name]))

  return (
    <section className="ops-timeline">
      <div className="ops-panel-kopf">
        <h2>Timeline</h2>
        {routineUnterdrueckt > 0 && (
          <span className="ops-hinweis">
            {routineUnterdrueckt} Routine-Eintraege ausgeblendet
          </span>
        )}
      </div>

      {eintraege.length === 0 ? (
        <div className="ops-leerzustand">
          Keine Ereignisse fuer den gewaehlten Filter.
        </div>
      ) : (
        <div className="ops-eintrag-liste">
          {eintraege.map(eintrag => (
            <OpsTimelineEintrag
              key={eintrag.id}
              eintrag={eintrag}
              zonenMap={zonenMap}
              onAktualisiert={onAktualisiert}
            />
          ))}
        </div>
      )}
    </section>
  )
}
