/* T-0610 FE-10 (+ T-0616 d): Texte einer laufenden Pre-Soak-Sequenz.
 *
 * Laeuft die echten Funktionen aus `src/komponenten/presoak-anzeige.ts` und
 * prueft das ERGEBNIS:
 *  - in `pause`/`haupt_pause` (Ventil zu) meldet die Kopfzeile NICHT
 *    "Automatik gießt", die Fremd-Ansicht nicht "gießt",
 *  - eine verdraengte Sequenz "wartet",
 *  - in `pre_soak`/`haupt` bleibt "Automatik gießt" bzw. "Manuell aktiv",
 *  - der Countdown liest sich als Dauer ("noch 15:48 min"), nicht als Uhrzeit,
 *    und zeigt bei 0 in einer Zeitphase "…".
 *
 * Lauf (in frontend/):  npm run test:presoak
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import assert from 'node:assert/strict'

const WURZEL = new URL('..', import.meta.url).pathname
const QUELLE = join(WURZEL, 'src/komponenten/presoak-anzeige.ts')
const tmp = mkdtempSync(join(tmpdir(), 't0610-fe10-'))
const kopie = join(tmp, 'presoak-anzeige.ts')
writeFileSync(kopie, readFileSync(QUELLE, 'utf8'))
const ziel = join(tmp, 'presoak-anzeige.js')
try {
  execFileSync(
    join(WURZEL, 'node_modules/typescript/bin/tsc'),
    ['--target', 'es2022', '--module', 'es2022', '--outDir', tmp, kopie],
    { stdio: 'pipe', cwd: tmpdir() },
  )
} catch (e) {
  if (!existsSync(ziel)) { console.error(String(e.stdout ?? e)); process.exit(1) }
}
const { preSoakKopf, fremdLaufText, restzeitText, preSoakCountdown } = await import(ziel)

let fehler = 0
function fall(name, fn) {
  try { fn(); console.log(`ok   ${name}`) }
  catch (e) { fehler += 1; console.log(`FAIL ${name}: ${e.message}`) }
}

fall('Pause nach dem Vorwaessern: Automatik-Sequenz in Pause', () => {
  assert.equal(preSoakKopf('pause', true, false), 'Automatik-Sequenz in Pause')
})
fall('Soak-Pause zwischen Pulsen: Automatik-Sequenz in Pause', () => {
  assert.equal(preSoakKopf('haupt_pause', true, false), 'Automatik-Sequenz in Pause')
})
fall('manuelle Sequenz in Pause', () => {
  assert.equal(preSoakKopf('haupt_pause', false, false), 'Sequenz in Pause')
})
fall('verdraengt: wartet', () => {
  assert.equal(preSoakKopf('pause', true, true), 'Automatik-Sequenz wartet')
})
fall('Ventil offen: Automatik gießt / Manuell aktiv', () => {
  assert.equal(preSoakKopf('pre_soak', true, false), 'Automatik gießt')
  assert.equal(preSoakKopf('haupt', true, false), 'Automatik gießt')
  assert.equal(preSoakKopf('haupt', false, false), 'Manuell aktiv')
})
fall('Fremd-Ansicht in der Pause sagt nicht "gießt"', () => {
  const t = fremdLaufText(2, 'bambuswald', 'haupt_pause', false)
  assert.equal(t, 'Kanal 2, Sequenz von bambuswald in Pause')
  assert.equal(fremdLaufText(2, 'bambuswald', 'pause', true), 'Kanal 2, Sequenz von bambuswald wartet')
})
fall('Fremd-Ansicht bei offenem Ventil', () => {
  assert.equal(fremdLaufText(2, 'bambuswald', 'haupt', false), 'Kanal 2 gießt (bambuswald)')
})
fall('Restzeit als Dauer', () => {
  assert.equal(restzeitText(948), 'noch 15:48 min')
  assert.equal(restzeitText(65), 'noch 1:05 min')
  assert.equal(restzeitText(-3), 'noch 0:00 min')
})
fall('Countdown bei 0 in Zeitphase: …', () => {
  assert.equal(preSoakCountdown(0, true), '…')
  assert.equal(preSoakCountdown(0, false), 'noch 0:00 min')
  assert.equal(preSoakCountdown(948, true), 'noch 15:48 min')
})

if (fehler) { console.log(`${fehler} Fall/Faelle rot`); process.exit(1) }
console.log('presoak-anzeige: alle Faelle gruen')
