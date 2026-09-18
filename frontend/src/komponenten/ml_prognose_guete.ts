/* T-0573: Anzeige-Seite des Prognose-Guete-Urteils aus dem Backend
 * (`bewaesserung/ml/prognose_guete.py`). Eigene Datei, damit die
 * Karten-Komponenten reine Komponenten-Module bleiben (Fast Refresh).
 */
/** T-0573: warum die 24h-Prognose nicht als Zahl dasteht.
 *
 *  Bewusst kurz im Badge und ausfuehrlich im Tooltip: die Karte soll auf
 *  einen Blick sagen "hier steht absichtlich keine Zahl", und auf Hover,
 *  woran es liegt.
 */
export function prognoseUngueltigLabel(
  v: {
    ungueltig_grund?: string | null
    feature_alter_h?: number | null
    feature_rueckstand_h?: number | null
    geraet_id?: string | null
  },
): { kurz: string; tooltip: string } {
  const alter = v.feature_alter_h != null
    ? `Die Prognose rechnet auf einer ${v.feature_alter_h.toFixed(1)} h alten Messzeile`
      + (v.feature_rueckstand_h != null
        ? ` und ignoriert dabei ${v.feature_rueckstand_h.toFixed(1)} h neuerer Messungen.`
        : '.')
    : ''
  switch (v.ungueltig_grund) {
    case 'geraetewechsel':
      return {
        kurz: 'Sensortausch',
        tooltip: `Unter dieser Geraete-ID steckt seit der Berechnung ein anderer Sensor${v.geraet_id ? ` (${v.geraet_id})` : ''} -- die Prognose gehoert zum alten Geraet und zu einer anderen Kennlinie. ${alter}`.trim(),
      }
    case 'veraltet':
      return {
        kurz: 'veraltet',
        tooltip: `${alter} Meist steht die Zeile still, weil ein Ausschlussfenster (Kalibrierung) alle neueren Messungen ausschliesst.`.trim(),
      }
    default:
      return {
        kurz: 'Herkunft unklar',
        tooltip: 'Zu dieser Prognose ist nicht bekannt, auf welcher Messzeile sie rechnet -- sie wird deshalb nicht als Zahl gezeigt.',
      }
  }
}
