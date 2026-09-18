/* T-0610 FE-13: Fokusfalle des Detail-Drawers.
 *
 * Laeuft die echte Funktion aus `src/komponenten/zonen-karte-v4/drawer-fokus.ts`
 * und prueft das ERGEBNIS: Tab vom letzten Element springt aufs erste,
 * Shift+Tab vom ersten aufs letzte, ein Fokus ausserhalb der Elemente (Titel
 * nach dem Oeffnen) betritt die Liste am richtigen Ende, dazwischen macht der
 * Browser den normalen Schritt, ohne Elemente bleibt der Fokus im Dialog.
 * Die Verdrahtung in ZonenKarteV4Drawer.tsx deckt tsc/eslint ab.
 *
 * Lauf (in frontend/):  npm run test:drawer
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import assert from 'node:assert/strict'

const WURZEL = new URL('..', import.meta.url).pathname
const QUELLE = join(WURZEL, 'src/komponenten/zonen-karte-v4/drawer-fokus.ts')
const tmp = mkdtempSync(join(tmpdir(), 't0610-fe13-'))
const kopie = join(tmp, 'drawer-fokus.ts')
writeFileSync(kopie, readFileSync(QUELLE, 'utf8'))
const ziel = join(tmp, 'drawer-fokus.js')
try {
  execFileSync(
    join(WURZEL, 'node_modules/typescript/bin/tsc'),
    ['--target', 'es2022', '--module', 'es2022', '--outDir', tmp, kopie],
    { stdio: 'pipe', cwd: tmpdir() },
  )
} catch (e) {
  if (!existsSync(ziel)) { console.error(String(e.stdout ?? e)); process.exit(1) }
}
const { fokusfalleZiel } = await import(ziel)

let fehler = 0
function fall(name, fn) {
  try { fn(); console.log(`ok   ${name}`) }
  catch (e) { fehler += 1; console.log(`FAIL ${name}: ${e.message}`) }
}

fall('Tab auf dem letzten Element springt aufs erste', () => {
  assert.equal(fokusfalleZiel(5, 4, false), 0)
})
fall('Shift+Tab auf dem ersten Element springt aufs letzte', () => {
  assert.equal(fokusfalleZiel(5, 0, true), 4)
})
fall('Tab vom Titel (nicht in der Liste) betritt die Liste vorne', () => {
  assert.equal(fokusfalleZiel(5, -1, false), 0)
})
fall('Shift+Tab vom Titel betritt die Liste hinten', () => {
  assert.equal(fokusfalleZiel(5, -1, true), 4)
})
fall('dazwischen: Browser-Schritt (null), vorwaerts und rueckwaerts', () => {
  assert.equal(fokusfalleZiel(5, 2, false), null)
  assert.equal(fokusfalleZiel(5, 2, true), null)
  assert.equal(fokusfalleZiel(5, 0, false), null)
  assert.equal(fokusfalleZiel(5, 4, true), null)
})
fall('ein einziges Element: Tab und Shift+Tab bleiben darauf', () => {
  assert.equal(fokusfalleZiel(1, 0, false), 0)
  assert.equal(fokusfalleZiel(1, 0, true), 0)
})
fall('nichts Fokussierbares: Fokus bleibt im Dialog (-1)', () => {
  assert.equal(fokusfalleZiel(0, -1, false), -1)
  assert.equal(fokusfalleZiel(0, -1, true), -1)
})
if (fehler > 0) { console.log(`\n${fehler} Fall/Faelle FAIL`); process.exit(1) }
console.log('\nalle Faelle ok')
