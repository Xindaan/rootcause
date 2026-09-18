/* T-0576: Der Tagesplan sagt bei einer blockierten Zone, WANN das
 * Giessfenster wieder offen ist -- und schweigt, wenn nicht das Fenster
 * blockiert.
 *
 * Kein Quelltext-Match: die echte Funktion wird uebersetzt, ausgefuehrt und
 * ihr ERGEBNIS geprueft (Linie wie frontend/tests/pruefe_bilanz_frische.mjs).
 *
 * Lauf (in frontend/):  npm run test:giessfenster
 */
// Uhrzeiten werden lokal formatiert; die Sollwerte sind Berliner Zeit.
// Vor jedem Date-Aufruf setzen, damit der Lauf nicht von der Maschine abhaengt.
process.env.TZ = 'Europe/Berlin'

import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// frontend/ -- die Datei liegt in frontend/tests/ und laeuft auch im
// oeffentlichen Ableger, der kein tools/ hat (T-0581).
const WURZEL = new URL('..', import.meta.url).pathname
const QUELLE = join(WURZEL, 'src/komponenten/giessfenster_hinweis.ts')

const tmp = mkdtempSync(join(tmpdir(), 't0576-'))
const kopie = join(tmp, 'hinweis.ts')
writeFileSync(kopie, readFileSync(QUELLE, 'utf8'))
const ziel = join(tmp, 'hinweis.js')
try {
  execFileSync(
    join(WURZEL, 'node_modules/typescript/bin/tsc'),
    ['--target', 'es2022', '--module', 'es2022', '--outDir', tmp, kopie],
    { stdio: 'pipe', cwd: tmpdir() }  // nicht in frontend/: dort liegt eine tsconfig.json (TS5112),
  )
} catch (e) {
  if (!existsSync(ziel)) { console.error(String(e.stdout ?? e)); process.exit(1) }
}
const { giessfensterHinweis } = await import(ziel)

let fehler = 0
function pruefe(was, ist, soll) {
  const ok = ist === soll
  console.log(`  ${ok ? 'OK  ' : 'FAIL'}  ${was}${ok ? '' : `\n        ist:  ${ist}\n        soll: ${soll}`}`)
  if (!ok) fehler++
}

const jetzt = new Date('2026-09-16T12:07:00+02:00')
const basis = { modus: 'aktiv', klammer: ['04:00-17:00'], daten_fehlen: false }

pruefe('mittags Sonne, frei morgen frueh',
  giessfensterHinweis({ ...basis, naechster_start: '2026-09-17T04:00:00+02:00',
    sperrgrund: 'zu viel Sonne beim Giessen' }, jetzt),
  'Gießfenster frei ab 04:00 (morgen)')
pruefe('noch heute frei',
  giessfensterHinweis({ ...basis, naechster_start: '2026-09-16T14:15:00+02:00',
    sperrgrund: 'zu viel Sonne beim Giessen' }, jetzt),
  'Gießfenster frei ab 14:15 (heute)')
pruefe('24 h keine freie Zeit',
  giessfensterHinweis({ ...basis, naechster_start: null, sperrgrund: 'x' }, jetzt),
  'Gießfenster: in den nächsten 24 h keine freie Zeit')
pruefe('ohne Wettervorhersage wird gekennzeichnet',
  giessfensterHinweis({ ...basis, daten_fehlen: true,
    naechster_start: '2026-09-17T04:00:00+02:00', sperrgrund: 'ausserhalb Giessfenster' }, jetzt),
  'Gießfenster frei ab 04:00 (morgen) (ohne Wettervorhersage)')
// Negativproben: Fenster offen (anderer Blocker) bzw. kein Aktivmodus
pruefe('Fenster offen -> kein Hinweis (Blocker ist etwas anderes)',
  giessfensterHinweis({ ...basis, naechster_start: '2026-09-16T12:15:00+02:00',
    sperrgrund: null }, jetzt),
  null)
pruefe('kein Aktivmodus -> kein Hinweis', giessfensterHinweis(null, jetzt), null)

if (fehler) { console.error(`\n${fehler} Pruefung(en) rot`); process.exit(1) }
console.log('\nalle gruen')
