/* T-0589: Der Notfall-Stopp bleibt sichtbar, solange eine Pre-Soak-Sequenz
 * laeuft -- auch in der Pause, in der kein Ventil offen ist.
 *
 * Bis 30.09.2026 erschien der Knopf nur bei offenem Ventil. In der Pause
 * einer Sequenz fehlte er genau dann, wenn die Automatik gleich wieder Wasser
 * gibt (Gesamtreview FE-7). Die Regel steht in
 * `src/komponenten/notfall-sichtbarkeit.ts`; hier laeuft die echte Funktion
 * und ihr ERGEBNIS wird geprueft, kein Quelltext-Match.
 *
 * Lauf (in frontend/):  npm run test:notfall
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// frontend/ -- die Datei liegt in frontend/tests/ (Linie wie pruefe_sensor_defekt.mjs).
const WURZEL = new URL('..', import.meta.url).pathname
const QUELLE = join(WURZEL, 'src/komponenten/notfall-sichtbarkeit.ts')

const tmp = mkdtempSync(join(tmpdir(), 't0589-'))
const kopie = join(tmp, 'notfall-sichtbarkeit.ts')
// Typ-Import auf api.ts entfernen: zur Laufzeit ohnehin weg, und api.ts zieht
// hier nicht aufloesbare Module nach.
writeFileSync(kopie, readFileSync(QUELLE, 'utf8').replace(/^import type .*$/gm, ''))
const ziel = join(tmp, 'notfall-sichtbarkeit.js')
try {
  execFileSync(
    join(WURZEL, 'node_modules/typescript/bin/tsc'),
    ['--target', 'es2022', '--module', 'es2022', '--outDir', tmp, kopie],
    { stdio: 'pipe', cwd: tmpdir() },  // nicht in frontend/: dort liegt eine tsconfig.json (TS5112)
  )
} catch (e) {
  // Typfehler wegen des entfernten Typ-Imports; tsc emittiert trotzdem. Ein
  // Syntaxfehler faellt auf, weil dann keine .js entsteht.
  if (!existsSync(ziel)) { console.error(String(e.stdout ?? e)); process.exit(1) }
}
const { notfallKnopfSichtbar } = await import(ziel)

let fehler = 0
function pruefe(was, ist, soll) {
  const ok = ist === soll
  console.log(`  ${ok ? 'OK  ' : 'FAIL'}  ${was}${ok ? '' : `\n        ist:  ${ist}\n        soll: ${soll}`}`)
  if (!ok) fehler++
}

// Ruhiger Grundzustand; jeder Fall aendert genau ein Feld.
const ruhig = { ventilAktiv: 0, ventilStatusFehler: false, preSoakLaeufe: [] }
const mit = (felder) => notfallKnopfSichtbar({ ...ruhig, ...felder })
const laeufe = (...phasen) => phasen.map(phase => ({ phase }))

console.log('Bisherige Ausloeser bleiben')
pruefe('nichts offen, keine Sequenz -> kein Knopf', mit({}), false)
pruefe('ein Ventil offen -> Knopf', mit({ ventilAktiv: 1 }), true)
pruefe('Ventilstatus unklar -> Knopf', mit({ ventilStatusFehler: true }), true)

console.log('Pre-Soak-Sequenz bei geschlossenem Ventil (der Befund FE-7)')
pruefe('Pause vor der Hauptdose -> Knopf', mit({ preSoakLaeufe: laeufe('pause') }), true)
pruefe('Soak-Pause zwischen Haupt-Pulsen -> Knopf',
  mit({ preSoakLaeufe: laeufe('haupt_pause') }), true)
pruefe('Puls laeuft (Statusabruf noch ohne Ventil) -> Knopf',
  mit({ preSoakLaeufe: laeufe('pre_soak') }), true)
pruefe('Hauptdose laeuft -> Knopf', mit({ preSoakLaeufe: laeufe('haupt') }), true)
pruefe('stop_fehler (Ventil evtl. offen) -> Knopf',
  mit({ preSoakLaeufe: laeufe('stop_fehler') }), true)
pruefe('unbekannte Phase -> Knopf (kein Positivfilter)',
  mit({ preSoakLaeufe: laeufe('neu') }), true)
pruefe('Pre-Soak-Status unbekannt (Abruf gescheitert) -> Knopf',
  mit({ preSoakLaeufe: null }), true)

console.log('Beendete Sequenzen halten den Knopf nicht (Negativprobe gegen Dauer-an)')
pruefe('nur fertig -> kein Knopf', mit({ preSoakLaeufe: laeufe('fertig') }), false)
pruefe('nur fehler (auch nach Notfall-Abbruch) -> kein Knopf',
  mit({ preSoakLaeufe: laeufe('fehler') }), false)
pruefe('fertig + fehler -> kein Knopf',
  mit({ preSoakLaeufe: laeufe('fertig', 'fehler') }), false)
pruefe('fertig + pause -> Knopf', mit({ preSoakLaeufe: laeufe('fertig', 'pause') }), true)

console.log(fehler === 0 ? '\nAlle Pruefungen bestanden.' : `\n${fehler} Pruefung(en) fehlgeschlagen.`)
process.exit(fehler === 0 ? 0 : 1)
