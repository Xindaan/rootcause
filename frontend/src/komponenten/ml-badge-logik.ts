/* T-0610 ML-11: Zustand des ML-Badges im Kopf.
 *
 * Vorher hiess "Modell geladen" immer "ML aktiv", auch wenn die Engine fuer
 * einzelne oder alle Zonen auf die Heuristik zurueckfiel (ML-Prognose
 * ungueltig, Zone ohne Modell). Jetzt zaehlt das Badge die
 * `prognose_quelle` der Empfehlung je Zone (Backend:
 * `_prognose_ml_oder_heuristik`) und unterscheidet ML / teilweise /
 * Heuristik. Ohne aktuelle Zonendaten (Snapshot nur in den Uebersichts-Tabs)
 * sagt es nur "ML geladen", statt eine Zuordnung zu behaupten.
 *
 * Reine Funktion, geprueft von `tests/pruefe_ml_badge.mjs`.
 */

export type MlBadgeKlasse = 'aktiv' | 'teilweise' | 'heuristik' | 'geladen' | 'inaktiv' | 'unbekannt'

export interface MlBadgeZustand {
  klasse: MlBadgeKlasse
  text: string
  titel: string
}

/**
 * @param geladen  `ist_geladen` aus /api/ml/status; `null` = Status nicht abrufbar.
 * @param quellen  zone_id -> prognose_quelle ('ml' | 'heuristik' | 'keine')
 *                 aus aktuellen Zonendaten; leer = unbekannt.
 * @param namen    zone_id -> Anzeigename fuer den Tooltip.
 */
export function mlBadgeZustand(
  geladen: boolean | null,
  quellen: Record<string, string>,
  namen: Record<string, string> = {},
): MlBadgeZustand {
  if (geladen === null) {
    return { klasse: 'unbekannt', text: 'ML ?', titel: 'ML-Status nicht abrufbar.' }
  }
  if (!geladen) {
    return {
      klasse: 'inaktiv',
      text: 'ML aus',
      titel: 'Kein ML-Modell geladen: alle Feuchte-Prognosen kommen aus der Heuristik.',
    }
  }
  const ml: string[] = []
  const heuristik: string[] = []
  for (const [zid, q] of Object.entries(quellen)) {
    if (q === 'ml') ml.push(zid)
    else if (q === 'heuristik') heuristik.push(zid)
  }
  const n = ml.length + heuristik.length
  const name = (zid: string) => namen[zid] ?? zid
  if (n === 0) {
    return {
      klasse: 'geladen',
      text: 'ML geladen',
      titel: 'ML-Modell geladen. Ob eine Zone ML oder Heuristik nutzt, ist nur mit aktuellen Zonendaten (Übersicht) bekannt.',
    }
  }
  if (heuristik.length === 0) {
    return {
      klasse: 'aktiv',
      text: 'ML aktiv',
      titel: `Feuchte-Prognose aller ${n} Zonen aus dem ML-Modell.`,
    }
  }
  if (ml.length === 0) {
    return {
      klasse: 'heuristik',
      text: 'Heuristik',
      titel: `ML-Modell geladen, aber alle ${n} Zonen nutzen die Heuristik (ML-Prognose ungültig oder fehlt).`,
    }
  }
  return {
    klasse: 'teilweise',
    text: `ML teilweise (${ml.length}/${n})`,
    titel: `ML: ${ml.map(name).join(', ')}. Heuristik: ${heuristik.map(name).join(', ')}.`,
  }
}

/** T-0610 ML-6: ein Modell schlaegt auf einem Horizont die Persistenz nicht
 *  (strukturgleich zu `MLPersistenzWarnung` in `typen.ts`). */
export interface MlPersistenzWarnungKurz {
  quelle: string
  horizont_stunden: number
  mae: number
  persistenz_mae: number
}

/** Klassen, bei denen eine ML-Prognose tatsaechlich im Spiel ist. Bei
 *  "ML aus", "Heuristik" und "unbekannt" waere die Warnung irrefuehrend. */
const WARN_KLASSEN: ReadonlySet<MlBadgeKlasse> = new Set(['aktiv', 'teilweise', 'geladen'])

export interface MlBadgeAnzeige extends MlBadgeZustand {
  warnt: boolean
}

/**
 * T-0610 ML-6 auf dem Badge-Modell von ML-11: haengt die Persistenz-Warnung
 * an, aber nur wenn das Modell geladen ist (`geladen === true`) und die
 * Badge-Klasse aktiv/teilweise/geladen ist. Nur Hinweis, kein Rueckfall.
 */
export function mlBadgeMitWarnung(
  z: MlBadgeZustand,
  geladen: boolean | null,
  warnungen: MlPersistenzWarnungKurz[] | undefined,
): MlBadgeAnzeige {
  const w = warnungen ?? []
  if (geladen !== true || !WARN_KLASSEN.has(z.klasse) || w.length === 0) {
    return { ...z, warnt: false }
  }
  const zeilen = w.map(x =>
    `${x.quelle} ${x.horizont_stunden} h: MAE ${x.mae.toFixed(2)} gegen ${x.persistenz_mae.toFixed(2)}`,
  )
  const warnTitel = [
    'Auf diesen Horizonten ist die ML-Prognose im Training nicht besser als „Feuchte bleibt gleich“:',
    ...zeilen,
    'Nur Hinweis: die Automatik rechnet weiter mit der ML-Prognose.',
  ].join('\n')
  return {
    ...z,
    text: `${z.text} · ${w.length}× schwach`,
    titel: `${z.titel}\n\n${warnTitel}`,
    warnt: true,
  }
}
