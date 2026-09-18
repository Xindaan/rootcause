import type { KanalSperre, Zone } from '../typen'

/** T-0612: Texte fuer das Sperr-Banner nach einem Notfall-Stopp.
 *
 *  Reine Funktionen, damit der Node-Test (`npm run test:notfall`) sie ohne
 *  React pruefen kann. Das Banner zeigt je Sperre die Zonen im Klartext
 *  (Anzeigenamen aus den Zonendaten, nie die interne Kennung), seit wann sie
 *  gilt, und einen Knopf zum Freigeben mit Rueckfrage. */

type ZoneName = Pick<Zone, 'zone_id' | 'name'>

/** "A", "A und B", "A, B und C". */
export function verbindeNamen(namen: ReadonlyArray<string>): string {
  if (namen.length <= 1) return namen[0] ?? ''
  return `${namen.slice(0, -1).join(', ')} und ${namen[namen.length - 1]}`
}

/** Zonen einer Sperre im Klartext, in der Reihenfolge der Sperre.
 *
 *  Unbekannte Zone (Zonendaten noch nicht geladen oder Zone umbenannt): die
 *  Kennung als Notbehelf -- besser als eine leere Stelle, hinter der niemand
 *  mehr erkennt, welcher Strang gesperrt ist. Ohne Zonen: "Kanal N". */
export function zonenKlartext(
  sperre: Pick<KanalSperre, 'zone_ids' | 'kanal'>,
  zonen: ReadonlyArray<ZoneName>,
): string {
  if (sperre.zone_ids.length === 0) return `Kanal ${sperre.kanal}`
  const nachId = new Map(zonen.map(z => [z.zone_id, z.name]))
  return verbindeNamen(sperre.zone_ids.map(id => nachId.get(id) || id))
}

/** "30.09. 14:02" aus naiver Lokalzeit (`2026-09-30T14:02:11.5`).
 *  Ohne Zonenangabe liest JavaScript den String als Lokalzeit -- genau so,
 *  wie das Backend ihn schreibt. Unlesbar -> "unbekannt". */
export function sperreSeitText(gesetztAm: string): string {
  const d = new Date(gesetztAm)
  if (Number.isNaN(d.getTime())) return 'unbekannt'
  const zwei = (n: number) => String(n).padStart(2, '0')
  return `${zwei(d.getDate())}.${zwei(d.getMonth() + 1)}. ${zwei(d.getHours())}:${zwei(d.getMinutes())}`
}

export interface SperrBannerEintrag {
  id: number
  zonen: string
  seit: string
  knopf: string
  rueckfrage: string
}

/** Ein Eintrag je offener Sperre, aelteste zuerst (Reihenfolge des Backends). */
export function sperrBannerEintraege(
  sperren: ReadonlyArray<KanalSperre>,
  zonen: ReadonlyArray<ZoneName>,
): SperrBannerEintrag[] {
  return sperren.map(s => {
    const namen = zonenKlartext(s, zonen)
    return {
      id: s.id,
      zonen: namen,
      seit: sperreSeitText(s.gesetzt_am),
      knopf: `Automatik für ${namen} wieder freigeben`,
      rueckfrage:
        `Automatik für ${namen} wieder freigeben?\n\n` +
        'Danach darf die Automatik dort wieder selbst gießen. Vorher prüfen, ' +
        'ob der Grund für den Notfall-Stopp (z. B. ein Leck) behoben ist.',
    }
  })
}

/** Gate G3-1: Stand der Sperrliste im Dashboard.
 *  - 'aktuell': letzter Abruf ok, das Backend konnte die Sperren lesen.
 *  - 'abruf_fehlgeschlagen': der Abruf selbst scheiterte (Netz, HTTP). Ob
 *    eine Sperre gilt, ist unbekannt; gezeigt wird der letzte bekannte Stand.
 *  - 'unlesbar': das Backend meldet `fehler`, es kann die Sperren nicht
 *    lesen. Loop und zentraler Waechter stehen dann fail-closed: die
 *    Automatik giesst auf keinem Kanal. */
export type SperrStand = 'aktuell' | 'abruf_fehlgeschlagen' | 'unlesbar'

export interface SperrZustand {
  sperren: KanalSperre[]
  stand: SperrStand
}

/** Neuer Zustand nach einem Abruf von `/api/kanal-sperren`.
 *  `antwort` null = Abruf gescheitert. Eine schon bekannte Sperre verschwindet
 *  nie, nur weil ein Abruf scheiterte oder das Backend nicht lesen konnte
 *  (es liefert dann eine leere Liste mit `fehler`). */
export function sperrZustandNachAbruf(
  vorher: SperrZustand,
  antwort: { sperren: KanalSperre[]; fehler?: string } | null,
): SperrZustand {
  if (antwort === null) return { sperren: vorher.sperren, stand: 'abruf_fehlgeschlagen' }
  if (antwort.fehler) return { sperren: vorher.sperren, stand: 'unlesbar' }
  return { sperren: antwort.sperren, stand: 'aktuell' }
}

export interface SperrBannerAnsicht {
  sichtbar: boolean
  /** true: die Automatik steht laut Backend ueberall (Warnfarbe Gefahr). */
  dringend: boolean
  titel: string
  eintraege: SperrBannerEintrag[]
  /** Hinweis zum Stand der Liste; null, wenn sie aktuell ist. */
  standHinweis: string | null
}

/** Was das Sperr-Banner zeigt. Ohne Sperren UND mit aktuellem Stand bleibt es
 *  unsichtbar. Ist der Stand unklar, erscheint es auch ohne Eintraege: sonst
 *  stuende die Automatik fail-closed still, ohne dass es jemand sieht. */
export function sperrBannerAnsicht(
  sperren: ReadonlyArray<KanalSperre>,
  zonen: ReadonlyArray<ZoneName>,
  stand: SperrStand,
): SperrBannerAnsicht {
  const eintraege = sperrBannerEintraege(sperren, zonen)
  let standHinweis: string | null = null
  if (stand === 'unlesbar') {
    standHinweis =
      'Sperrstatus unklar: Das Backend kann die Kanal-Sperren nicht lesen. ' +
      'Die Automatik gießt deshalb aus Vorsicht nicht.'
  } else if (stand === 'abruf_fehlgeschlagen') {
    standHinweis = eintraege.length > 0
      ? 'Stand nicht aktuell: Der letzte Abruf ist fehlgeschlagen.'
      : 'Sperrstatus unklar: Der Abruf der Kanal-Sperren ist fehlgeschlagen. ' +
        'Ob die Automatik gesperrt ist, ist gerade nicht bekannt.'
  }
  return {
    sichtbar: eintraege.length > 0 || standHinweis !== null,
    dringend: stand === 'unlesbar',
    titel: eintraege.length > 0
      ? 'Automatik nach Notfall-Stopp gesperrt'
      : 'Sperrstatus unklar',
    eintraege,
    standHinweis,
  }
}

/** Antwort von `/api/notfall-stopp`, soweit die Meldung sie braucht. */
export interface NotfallErgebnis {
  fehler?: string
  geschlossen?: number
  fehlgeschlagen?: ReadonlyArray<unknown>
  pre_soak_fehler?: ReadonlyArray<unknown>
  sperre_fehler?: ReadonlyArray<unknown>
}

/** Gate G3-4: Meldung nach dem Notfall-Stopp, null = Erfolg ohne Meldung.
 *
 *  Scheitert NUR die Automatik-Sperre, sind die Ventile zu. "Notfall-Stopp
 *  fehlgeschlagen" liesse sich dann als "Ventile offen" lesen; deshalb
 *  getrennt: erst der Ventilzustand, dann die unvollstaendige Sperre mit dem
 *  Ausweg ueber die Konfiguration. Offene Ventile oder nicht abgebrochene
 *  Sequenzen bleiben "fehlgeschlagen" (T-0589). */
export function notfallErgebnisMeldung(e: NotfallErgebnis): string | null {
  if (!e.fehler) return null
  const nichtZu = e.fehlgeschlagen?.length ?? 0
  const preSoak = e.pre_soak_fehler?.length ?? 0
  const sperre = e.sperre_fehler?.length ?? 0
  if (nichtZu > 0 || preSoak > 0 || sperre === 0) {
    return `Notfall-Stopp fehlgeschlagen: ${e.fehler}`
  }
  const n = e.geschlossen ?? 0
  const ventile = n > 0
    ? `Alle offenen Ventile sind zu (${n} geschlossen).`
    : 'Es war kein Ventil offen, das geschlossen werden musste.'
  return (
    `Notfall-Stopp ausgeführt. ${ventile}\n\n` +
    `Aber die Automatik-Sperre ist unvollständig (${sperre} Fehler): ` +
    'Die Automatik kann auf mindestens einem betroffenen Kanal wieder selbst gießen. ' +
    'Im Zweifel die Automatik in der Konfiguration ausschalten ' +
    '(ventilsteuerung_aktiv: false, danach den Dienst neu starten).'
  )
}
