/* T-0610 FE-10 (+ T-0616 d / T-0618-Rest): Texte der laufenden Bewaesserung
 * in ManuellesGiessen.
 *
 * 1. In den Pausen einer Pre-Soak-Sequenz (`pause` nach dem Vorwaessern,
 *    `haupt_pause` zwischen zwei Haupt-Pulsen) ist das Ventil ZU. Die Karte
 *    meldete trotzdem "Automatik gießt", die Fremd-Ansicht der Geschwister-
 *    Zone "Kanal N giesst". Jetzt nennen Kopfzeile und Fremd-Ansicht die
 *    Pause; eine verdraengte Sequenz (T-0618) "wartet".
 * 2. Der Countdown stand als "(15:48)" da und las sich wie eine Uhrzeit,
 *    direkt neben "seit 14:02". Jetzt "noch 15:48 min".
 *
 * Reine Funktionen, geprueft von `tests/pruefe_presoak_anzeige.mjs`.
 */

/** Phasen, in denen eine laufende Sequenz das Ventil geschlossen haelt. */
export function istVentilZuPhase(phase: string): boolean {
  return phase === 'pause' || phase === 'haupt_pause'
}

/** Kopfzeile ("<Grund> — Empfehlung pausiert") einer eigenen Sequenz. */
export function preSoakKopf(
  phase: string, istAutomatik: boolean, verdraengt: boolean,
): string {
  const wer = istAutomatik ? 'Automatik-Sequenz' : 'Sequenz'
  if (verdraengt) return `${wer} wartet`
  if (istVentilZuPhase(phase)) return `${wer} in Pause`
  return istAutomatik ? 'Automatik gießt' : 'Manuell aktiv'
}

/** Beschriftung der Sequenz einer Geschwister-Zone am selben Kanal. */
export function fremdLaufText(
  kanal: number | string, zoneId: string, phase: string, verdraengt: boolean,
): string {
  if (verdraengt || istVentilZuPhase(phase)) {
    return `Kanal ${kanal}, Sequenz von ${zoneId} ${verdraengt ? 'wartet' : 'in Pause'}`
  }
  return `Kanal ${kanal} gießt (${zoneId})`
}

/** Restzeit als Dauer, nicht als Uhrzeit lesbar: "noch 15:48 min". */
export function restzeitText(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  const m = Math.floor(s / 60)
  const r = s % 60
  return `noch ${m}:${r.toString().padStart(2, '0')} min`
}

/** Countdown einer Sequenz-Phase. Bei 0 in einer Zeitphase "…" statt eines
 *  eingefrorenen Werts (T-0346: der Phasenwechsel kommt mit dem naechsten
 *  Poll). */
export function preSoakCountdown(sec: number, istZeitphase: boolean): string {
  return (sec === 0 && istZeitphase) ? '…' : restzeitText(sec)
}
