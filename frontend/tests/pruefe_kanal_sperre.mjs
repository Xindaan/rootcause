/* T-0612: Texte des Sperr-Banners nach einem Notfall-Stopp.
 *
 * Das Banner muss die gesperrten Zonen im Klartext nennen (Anzeigenamen aus
 * den Zonendaten, nicht die interne Kennung), die Zeit der Sperre in
 * Lokalzeit und den Knopf "Automatik für <Zonen> wieder freigeben". Die
 * Regeln stehen in `src/komponenten/kanal-sperre.ts`; hier laeuft die echte
 * Funktion und ihr ERGEBNIS wird geprueft, kein Quelltext-Match.
 *
 * Gate G3-1: bei unklarem Sperrstatus erscheint das Banner auch ohne
 * Eintraege, und eine bekannte Sperre verschwindet nicht durch einen
 * Fehler. Gate G3-4: die Notfall-Meldung trennt "Ventile zu" von
 * "Sperre unvollstaendig".
 *
 * Lauf (in frontend/):  npm run test:notfall  (laeuft nach der Sichtbarkeit)
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const WURZEL = new URL('..', import.meta.url).pathname
const QUELLE = join(WURZEL, 'src/komponenten/kanal-sperre.ts')

const tmp = mkdtempSync(join(tmpdir(), 't0612-'))
const kopie = join(tmp, 'kanal-sperre.ts')
// Typ-Import auf typen.ts entfernen (zur Laufzeit ohnehin weg).
writeFileSync(kopie, readFileSync(QUELLE, 'utf8').replace(/^import type .*$/gm, ''))
const ziel = join(tmp, 'kanal-sperre.js')
try {
  execFileSync(
    join(WURZEL, 'node_modules/typescript/bin/tsc'),
    ['--target', 'es2022', '--module', 'es2022', '--outDir', tmp, kopie],
    { stdio: 'pipe', cwd: tmpdir() },
  )
} catch (e) {
  if (!existsSync(ziel)) { console.error(String(e.stdout ?? e)); process.exit(1) }
}
const {
  sperrBannerEintraege, zonenKlartext, sperreSeitText,
  sperrBannerAnsicht, sperrZustandNachAbruf, notfallErgebnisMeldung,
} = await import(ziel)

let fehler = 0
function pruefe(was, ist, soll) {
  const ok = ist === soll
  console.log(`  ${ok ? 'OK  ' : 'FAIL'}  ${was}${ok ? '' : `\n        ist:  ${ist}\n        soll: ${soll}`}`)
  if (!ok) fehler++
}

const zonen = [
  { zone_id: 'bambuswald', name: 'Bambuswald' },
  { zone_id: 'bambuswald_yogaraum', name: 'Yogaraum' },
  { zone_id: 'hecke', name: 'Hecke' },
  { zone_id: 'waldblumenhain', name: 'Waldblumenhain' },
]
const sperre = (zone_ids, extra = {}) => ({
  id: 7, geraet_id: 'a1b2c3d4-uuid', kanal: 1, zone_ids,
  grund: 'Notfall-Stopp', gesetzt_am: '2026-09-30T14:02:11.483', gesetzt_durch: 'notfall-stopp',
  ...extra,
})

console.log('Zonen im Klartext, in der Reihenfolge der Sperre')
pruefe('eine Zone', zonenKlartext(sperre(['hecke']), zonen), 'Hecke')
pruefe('zwei Zonen am geteilten Kanal',
  zonenKlartext(sperre(['bambuswald', 'bambuswald_yogaraum']), zonen), 'Bambuswald und Yogaraum')
pruefe('drei Zonen',
  zonenKlartext(sperre(['hecke', 'bambuswald', 'waldblumenhain']), zonen),
  'Hecke, Bambuswald und Waldblumenhain')
pruefe('Zonendaten fehlen noch -> Kennung als Notbehelf',
  zonenKlartext(sperre(['hecke']), []), 'hecke')
pruefe('Sperre ohne Zonen -> Kanal', zonenKlartext(sperre([], { kanal: 3 }), zonen), 'Kanal 3')

console.log('Zeit in Lokalzeit (Backend schreibt naive Lokalzeit)')
pruefe('30.09. 14:02', sperreSeitText('2026-09-30T14:02:11.483'), '30.09. 14:02')
pruefe('unlesbar -> unbekannt', sperreSeitText('kaputt'), 'unbekannt')

console.log('Banner-Eintrag')
const [e] = sperrBannerEintraege([sperre(['bambuswald', 'bambuswald_yogaraum'])], zonen)
pruefe('Id durchgereicht (fuer die Freigabe)', e.id, 7)
pruefe('Knopftext nennt die Zonen',
  e.knopf, 'Automatik für Bambuswald und Yogaraum wieder freigeben')
pruefe('Rueckfrage nennt die Zonen',
  e.rueckfrage.startsWith('Automatik für Bambuswald und Yogaraum wieder freigeben?'), true)
pruefe('keine Kennung im Text', /bambuswald|uuid/.test(`${e.zonen} ${e.knopf} ${e.rueckfrage}`), false)
pruefe('seit', e.seit, '30.09. 14:02')
pruefe('keine Sperre -> kein Eintrag (Banner unsichtbar)',
  sperrBannerEintraege([], zonen).length, 0)

console.log('Gate G3-1: Banner auch ohne Eintraege, wenn der Stand unklar ist')
const s1 = [sperre(['hecke'])]
{
  const a = sperrBannerAnsicht([], zonen, 'aktuell')
  pruefe('aktuell, keine Sperre -> unsichtbar', a.sichtbar, false)
}
{
  const a = sperrBannerAnsicht(s1, zonen, 'aktuell')
  pruefe('aktuell, eine Sperre -> sichtbar', a.sichtbar, true)
  pruefe('aktuell, eine Sperre -> Titel', a.titel, 'Automatik nach Notfall-Stopp gesperrt')
  pruefe('aktuell -> kein Stand-Hinweis', a.standHinweis, null)
  pruefe('aktuell -> nicht dringend', a.dringend, false)
}
{
  const a = sperrBannerAnsicht([], zonen, 'unlesbar')
  pruefe('Backend kann nicht lesen, keine Eintraege -> sichtbar', a.sichtbar, true)
  pruefe('... Titel "Sperrstatus unklar"', a.titel, 'Sperrstatus unklar')
  pruefe('... Hinweis: Automatik giesst aus Vorsicht nicht',
    a.standHinweis, 'Sperrstatus unklar: Das Backend kann die Kanal-Sperren nicht lesen. '
      + 'Die Automatik gießt deshalb aus Vorsicht nicht.')
  pruefe('... dringend (Gefahr-Farbe)', a.dringend, true)
}
{
  const a = sperrBannerAnsicht(s1, zonen, 'unlesbar')
  pruefe('Backend kann nicht lesen, alte Sperre -> Eintrag bleibt', a.eintraege.length, 1)
  pruefe('... Hinweis nennt den Stillstand', /aus Vorsicht nicht/.test(a.standHinweis ?? ''), true)
}
{
  const a = sperrBannerAnsicht([], zonen, 'abruf_fehlgeschlagen')
  pruefe('Abruf gescheitert, keine Eintraege -> sichtbar', a.sichtbar, true)
  pruefe('... Titel "Sperrstatus unklar"', a.titel, 'Sperrstatus unklar')
  pruefe('... behauptet keinen Stillstand (Backend evtl. gesund)',
    /aus Vorsicht/.test(a.standHinweis ?? ''), false)
  pruefe('... sagt, dass der Stand unbekannt ist',
    /nicht bekannt/.test(a.standHinweis ?? ''), true)
}
{
  const a = sperrBannerAnsicht(s1, zonen, 'abruf_fehlgeschlagen')
  pruefe('Abruf gescheitert, alte Sperre -> Stand nicht aktuell',
    a.standHinweis, 'Stand nicht aktuell: Der letzte Abruf ist fehlgeschlagen.')
}

console.log('Gate G3-1: bekannte Sperre verschwindet nicht durch einen Fehler')
{
  const vorher = { sperren: s1, stand: 'aktuell' }
  const z1 = sperrZustandNachAbruf(vorher, { sperren: [], fehler: 'Kanal-Sperren nicht lesbar' })
  pruefe('Backend-Fehler (leere Liste) -> alte Sperre bleibt', z1.sperren.length, 1)
  pruefe('Backend-Fehler -> Stand unlesbar', z1.stand, 'unlesbar')
  const z2 = sperrZustandNachAbruf(vorher, null)
  pruefe('Abruf gescheitert -> alte Sperre bleibt', z2.sperren.length, 1)
  pruefe('Abruf gescheitert -> Stand abruf_fehlgeschlagen', z2.stand, 'abruf_fehlgeschlagen')
  const z3 = sperrZustandNachAbruf(z1, { sperren: [] })
  pruefe('danach erfolgreicher Abruf ohne Sperren -> leer', z3.sperren.length, 0)
  pruefe('danach erfolgreicher Abruf -> aktuell', z3.stand, 'aktuell')
}

console.log('Gate G3-4: Meldung trennt Ventilzustand und unvollstaendige Sperre')
{
  pruefe('Erfolg -> keine Meldung', notfallErgebnisMeldung({ ok: true, geschlossen: 1 }), null)
  const m = notfallErgebnisMeldung({
    ok: false, fehler: 'Automatik-Sperre unvollstaendig (1 Fehler)', geschlossen: 2,
    fehlgeschlagen: [], sperre_fehler: [{ kanal: 1 }],
  }) ?? ''
  pruefe('nur Sperre gescheitert -> nicht "fehlgeschlagen"', /fehlgeschlagen/.test(m), false)
  pruefe('... nennt geschlossene Ventile',
    m.startsWith('Notfall-Stopp ausgeführt. Alle offenen Ventile sind zu (2 geschlossen).'), true)
  pruefe('... nennt die unvollstaendige Sperre', /Automatik-Sperre ist unvollständig \(1 Fehler\)/.test(m), true)
  pruefe('... Ausweg ueber die Konfiguration', /ventilsteuerung_aktiv: false/.test(m), true)
  const m0 = notfallErgebnisMeldung({
    ok: false, fehler: 'x', geschlossen: 0, sperre_fehler: [{}],
  }) ?? ''
  pruefe('nur Sperre gescheitert, nichts offen gewesen',
    /kein Ventil offen/.test(m0) && !/fehlgeschlagen/.test(m0), true)
  pruefe('Ventil nicht zu + Sperre -> "fehlgeschlagen"',
    notfallErgebnisMeldung({
      ok: false, fehler: '1 Kanaele konnten nicht geschlossen werden', geschlossen: 0,
      fehlgeschlagen: [{ kanal: 1 }], sperre_fehler: [{}],
    }), 'Notfall-Stopp fehlgeschlagen: 1 Kanaele konnten nicht geschlossen werden')
  pruefe('Pre-Soak-Abbruch gescheitert -> "fehlgeschlagen" (T-0589)',
    (notfallErgebnisMeldung({
      ok: false, fehler: 'Hauptdose kann wieder oeffnen', geschlossen: 1,
      pre_soak_fehler: [{}], sperre_fehler: [{}],
    }) ?? '').startsWith('Notfall-Stopp fehlgeschlagen'), true)
  pruefe('keine Ventilsteuerung -> "fehlgeschlagen"',
    notfallErgebnisMeldung({ fehler: 'Keine Ventilsteuerung verfuegbar' }),
    'Notfall-Stopp fehlgeschlagen: Keine Ventilsteuerung verfuegbar')
}

console.log(fehler === 0 ? '\nAlle Pruefungen bestanden.' : `\n${fehler} Pruefung(en) fehlgeschlagen.`)
process.exit(fehler === 0 ? 0 : 1)
