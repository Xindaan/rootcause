/* T-0610 API-10/FE-4: Klassifikation eines Ventil-Event-Paars aus der
 * Ops-Liste "Heute: Ventil-Events".
 *
 * Der Knopf "Regen / Fehler" hat das Paar frueher per DELETE hart geloescht:
 * keine Korrektur-Spur, und die Sensor-Heuristik durfte denselben Sprung beim
 * naechsten Scan als neues Phantom schreiben. Jetzt setzt er wie das
 * UnbekanntEventBanner `ausloser: 'ignoriert'` auf beide Haelften
 * (`?paar=true`). Das Backend protokolliert die Aenderung in
 * `ausloser_korrektur`/`korrektur_log`, und ein ignoriertes Heuristik-Event
 * blockiert die Neuerkennung (T-0114, `_event_im_fenster`).
 *
 * Reine Funktion mit uebergebenem PATCH-Aufruf, damit
 * `tests/pruefe_tag_events_aktion.mjs` das Verhalten ohne Netz pruefen kann.
 */

export type TagEventKlasse = 'automatik' | 'manuell' | 'ignoriert'

export type PatchPaar = (
  id: number,
  patch: { ausloser: TagEventKlasse },
  paar: boolean,
) => Promise<{ ok?: boolean; fehler?: string }>

/** Klassifiziert das Event-Paar; liefert `null` bei Erfolg, sonst den
 *  Fehlertext fuer die Anzeige. Wirft nie. */
export async function klassifiziereEventPaar(
  id: number, klasse: TagEventKlasse, patch: PatchPaar,
): Promise<string | null> {
  try {
    const antwort = await patch(id, { ausloser: klasse }, /* paar */ true)
    if (antwort?.fehler) return antwort.fehler
    if (antwort?.ok !== true) return 'Unerwartete Antwort vom Server'
    return null
  } catch (err) {
    return err instanceof Error ? err.message : String(err)
  }
}
