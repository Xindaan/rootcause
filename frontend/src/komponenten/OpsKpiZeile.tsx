import type { OpsSummary } from '../typen'

interface Props {
  summary: OpsSummary | null
}

function blockerSumme(summary: OpsSummary | null): number {
  if (!summary) return 0
  return Object.values(summary.blocker_verteilung).reduce((summe, wert) => summe + wert, 0)
}

export function OpsKpiZeile({ summary }: Props) {
  const karten: Array<{ label: string; wert: number | string; klasse: string; title?: string }> = [
    {
      label: 'Shadow-Empfehlungen',
      wert: summary?.shadow_vorschlaege_heute ?? '\u2014',
      klasse: 'info',
      title: 'Regel-Motor haette gerne gegossen, aber ventilsteuerung_aktiv=false verhindert Ausfuehrung.',
    },
    {
      label: 'Bewaesserungen heute',
      wert: summary?.bewaesserungen_heute ?? '\u2014',
      klasse: 'gesund',
      title: 'Echte Ventil-Events: Live, manuell geloggt, DHS-Backfill, Sensor-Heuristik.',
    },
    {
      label: 'Blocker',
      wert: summary ? blockerSumme(summary) : '\u2014',
      klasse: 'gedaempft',
    },
    {
      label: 'Wetterwarnungen',
      wert: summary?.wetter_warnungen ?? '\u2014',
      klasse: 'warnung',
    },
    {
      label: 'Sensorwarnungen',
      wert: summary?.sensor_warnungen ?? '\u2014',
      klasse: 'gefahr',
    },
  ]

  return (
    <section className="ops-kpi-block">
      <header className="ops-kpi-kopf">
        <h3>Heute</h3>
        <span className="ops-kpi-hinweis">seit 00:00 — vom Zeitraum-Filter unabhaengig</span>
      </header>
      <div className="ops-kpi-zeile">
        {karten.map(karte => (
          <article
            key={karte.label}
            className={`ops-kpi-karte ${karte.klasse}`}
            title={karte.title}
          >
            <span className="ops-kpi-label">{karte.label}</span>
            <strong className="ops-kpi-wert">{karte.wert}</strong>
          </article>
        ))}
      </div>
    </section>
  )
}
