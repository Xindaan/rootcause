/* T-0576: Was der Tagesplan zum Giessfenster einer blockierten Zone sagt.
 *
 * Bewusst ohne React-Import, damit die Aussage in node ausfuehrbar ist
 * (`frontend/tests/pruefe_giessfenster_hinweis.mjs`) -- gleiche Linie wie
 * `bilanz_frische.ts`.
 *
 * Das Backend liefert `giessfenster` nur fuer Zonen im Aktivmodus. Dort
 * gelten die alten Uhrzeitfenster nicht mehr; stattdessen sperren Klammer,
 * Sonne und Trocknung. Der Grund steht schon in `grund` (Engine-Text), was
 * fehlt, ist die Antwort auf die naechste Frage: "und wann dann?".
 */

export interface GiessfensterInfo {
  modus: string
  klammer: string[]
  /** ISO mit TZ; erster freier Start in den naechsten 24 h, sonst null. */
  naechster_start: string | null
  /** Warum es JETZT zu ist; null, wenn das Fenster offen ist. */
  sperrgrund: string | null
  /** Keine Wettervorhersage: dann entscheidet nur die Klammer. */
  daten_fehlen: boolean
}

function tagesSchluessel(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
}

/** Hinweistext oder null, wenn das Giessfenster nicht der Blocker ist. */
export function giessfensterHinweis(
  gf: GiessfensterInfo | null | undefined, jetzt: Date,
): string | null {
  // Offenes Fenster: die Zone ist aus einem ANDEREN Grund blockiert (Regen,
  // Budget, Pause). Dann waere "frei ab jetzt" eine falsche Fährte.
  if (!gf || gf.sperrgrund === null) return null
  const ohneDaten = gf.daten_fehlen ? ' (ohne Wettervorhersage)' : ''
  if (gf.naechster_start === null) {
    return `Gießfenster: in den nächsten 24 h keine freie Zeit${ohneDaten}`
  }
  const start = new Date(gf.naechster_start)
  const uhr = start.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
  const morgen = new Date(jetzt)
  morgen.setDate(morgen.getDate() + 1)
  const tag = tagesSchluessel(start) === tagesSchluessel(jetzt)
    ? 'heute'
    : tagesSchluessel(start) === tagesSchluessel(morgen) ? 'morgen' : 'später'
  return `Gießfenster frei ab ${uhr} (${tag})${ohneDaten}`
}
