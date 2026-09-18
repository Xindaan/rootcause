/* T-0610 Q3/W7: Schalter fuer die Routinezeilen "Zone im Monitoring-Modus".
 *
 * Gemeinsam fuer Historie-Tab und Entscheidungsprotokoll. Aus = die Seite
 * fragt mit `ohne_monitoring=true` (entscheidungs-filter.ts); das Backend
 * filtert vor dem Limit, damit die Routinezeilen das Limit nicht auffressen.
 */

interface Props {
  /** true = Routinezeilen einblenden. */
  an: boolean
  onChange: (an: boolean) => void
  className: string
  text?: string
}

export function MonitoringSchalter({ an, onChange, className, text = 'Routinezeilen zeigen' }: Props) {
  return (
    <label className={className} title="Routinezeilen „Zone im Monitoring-Modus“ (eine je Monitoring-Zone und Runde) einblenden">
      <input
        type="checkbox"
        checked={an}
        onChange={e => onChange(e.target.checked)}
      />
      {text}
    </label>
  )
}
