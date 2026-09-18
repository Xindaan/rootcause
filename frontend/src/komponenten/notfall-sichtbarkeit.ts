import type { PreSoakLauf } from '../api'

/** T-0589: Wann der Notfall-Stopp im Kopf erscheint.
 *
 *  Bis 30.09.2026 nur bei offenem Ventil (`ventilAktiv.size > 0`) oder
 *  unklarem Ventilstatus. In der Pause einer Pre-Soak-Sequenz ist das Ventil
 *  aber zu, `/ventil-status` meldet nichts -- der Knopf fehlte also genau
 *  dann, wenn die Automatik gleich wieder Wasser gibt (Gesamtreview FE-7,
 *  Realfall 22.09.: bambuswald ab 21:13:41 in der Pause, Knopf weg).
 *
 *  Endphasen fuer DIESE Entscheidung sind nur `fertig` und `fehler`.
 *  `stop_fehler` heisst: Ventil-Stop fehlgeschlagen, Ventil evtl. noch offen
 *  -- dort bleibt der Knopf. Bewusst eine Liste der ENDphasen und keine der
 *  laufenden Phasen: eine neue oder unbekannte Phase zeigt den Knopf, statt
 *  still durch einen Positivfilter zu fallen (vgl. T-0462, `haupt_pause`). */
export const NOTFALL_KNOPF_ENDPHASEN: ReadonlySet<string> = new Set(['fertig', 'fehler'])

/** `laeufe === null` heisst: Pre-Soak-Status unbekannt (Abruf gescheitert).
 *  Dann wie beim unklaren Ventilstatus den Knopf zeigen -- ein Knopf zu viel
 *  kostet einen Klick mit Rueckfrage, einer zu wenig im Leckfall Wasser. */
export function preSoakSequenzLaeuft(
  laeufe: ReadonlyArray<Pick<PreSoakLauf, 'phase'>> | null,
): boolean {
  if (laeufe === null) return true
  return laeufe.some(l => !NOTFALL_KNOPF_ENDPHASEN.has(l.phase))
}

export function notfallKnopfSichtbar(stand: {
  /** Anzahl offener Ventil-Zonen aus `/ventil-status`. */
  ventilAktiv: number
  /** Ventilstatus unklar (Abruf gescheitert oder Notfall unbestaetigt). */
  ventilStatusFehler: boolean
  /** Alle Laeufe aus `/ventil/pre-soak-status` ohne zone_id; null = unbekannt. */
  preSoakLaeufe: ReadonlyArray<Pick<PreSoakLauf, 'phase'>> | null
}): boolean {
  return stand.ventilAktiv > 0
    || stand.ventilStatusFehler
    || preSoakSequenzLaeuft(stand.preSoakLaeufe)
}
