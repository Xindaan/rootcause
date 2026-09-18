/* T-0575: Was darf die Bilanz-Kachel ueber ihre eigenen Zahlen behaupten?
 *
 * Bewusst ohne React-Import, damit die Entscheidung in node ausfuehrbar
 * ist (`frontend/tests/pruefe_bilanz_frische.mjs`) -- gleiche Linie wie
 * `feuchte_chart_segmente.ts` aus T-0573.
 *
 * **Was hier NICHT geprueft wird, und warum.** Die naheliegende Idee waere,
 * das Fensterende (`fenster_bis`) als Frische zu zeigen. Das waere eine
 * Schein-Pruefung: `bilanz.py:82` setzt `fenster_bis` auf den `bis`-Parameter,
 * und `api_server.py:2660` setzt den auf `datetime.now()`. Das Feld ist also
 * die Uhr, nicht die Datenlage -- ein Indikator daraus koennte nur
 * bestaetigen (fehlerpattern_detektor_erbt_die_zu_pruefende_praemisse).
 *
 * Echte Herkunftsaussagen liefert das Backend dagegen zwei:
 *   `bewaesserung_indikativ`  mindestens ein Ventil-Ereignis ohne Literwert
 *   `quelle_niederschlag`     archiv (gemessen) vs forecast/gemischt
 */

export type BilanzQuelleWert = 'archiv' | 'forecast' | 'gemischt' | 'keine'

export interface BilanzHerkunft {
  indikativ?: boolean
  bewaesserung_indikativ?: boolean
  quelle_niederschlag?: BilanzQuelleWert | string
}

export interface BilanzHinweis {
  /** Sichtbare Markierung noetig? */
  markieren: boolean
  /** Kurzlabel an der Kachel. Leer, wenn nichts zu markieren ist. */
  kurz: string
  /** Volltext fuer den Hover. Nennt die Ursache, die WIRKLICH vorliegt. */
  tooltip: string
}

const WETTER_TEXT: Record<string, string> = {
  archiv: 'Wetter aus dem Open-Meteo-ERA5-Archiv (gemessen).',
  forecast: 'Wetter aus der Vorhersage, nicht gemessen (das ERA5-Archiv hinkt rund 5 Tage hinterher).',
  gemischt: 'Wetter teils gemessen, der juengste Teil noch Vorhersage.',
  keine: 'Keine Wetterdaten fuer dieses Fenster.',
}

/**
 * T-0575: Vor dieser Funktion zeigte die Kachel bei `indikativ` einen
 * Tooltip "Wetter-Quelle: ..." -- auch dann, wenn die Unsicherheit von der
 * BEWAESSERUNGS-Seite kam. Der Hinweis nannte also regelmaessig die falsche
 * Ursache, und sichtbar war ohnehin nichts: ein `title`-Attribut ist auf
 * dem Touchscreen gar nicht erreichbar.
 */
export function bilanzHinweis(d: BilanzHerkunft): BilanzHinweis {
  const wetterUnsicher =
    d.quelle_niederschlag === 'forecast' || d.quelle_niederschlag === 'gemischt'
  const wetterFehlt = d.quelle_niederschlag === 'keine'
  const bewUnsicher = d.bewaesserung_indikativ === true

  // `indikativ` bleibt der Trigger des Backends. Ein aelteres Backend ohne
  // `bewaesserung_indikativ` liefert nur dieses Bool -- dann markieren wir
  // weiterhin, benennen die Ursache aber ausdruecklich als unklar, statt
  // eine zu erfinden.
  if (!d.indikativ && !bewUnsicher && !wetterFehlt) {
    return { markieren: false, kurz: '', tooltip: WETTER_TEXT.archiv }
  }

  const gruende: string[] = []
  if (bewUnsicher) {
    gruende.push(
      'Mindestens ein Bewaesserungs-Ereignis im Fenster hat keinen gemessenen '
      + 'Literwert; seine Menge ist geschaetzt.',
    )
  }
  if (wetterUnsicher || wetterFehlt) {
    gruende.push(WETTER_TEXT[String(d.quelle_niederschlag)] ?? String(d.quelle_niederschlag))
  }
  if (gruende.length === 0) {
    gruende.push(
      'Das Backend meldet die Zahlen als indikativ, ohne die Ursache zu nennen.',
    )
  }

  // Kurzlabel nennt die Seite, nicht das Wort "indikativ" -- der Nutzer
  // soll ohne Hover wissen, WELCHE Zahl wackelt.
  let kurz: string
  if (bewUnsicher && (wetterUnsicher || wetterFehlt)) kurz = 'Bew. + Wetter geschätzt'
  else if (bewUnsicher) kurz = 'Bewässerung geschätzt'
  else if (wetterFehlt) kurz = 'ohne Wetterdaten'
  else if (wetterUnsicher) kurz = 'Wetter aus Vorhersage'
  else kurz = 'indikativ'

  return { markieren: true, kurz, tooltip: gruende.join(' ') }
}
