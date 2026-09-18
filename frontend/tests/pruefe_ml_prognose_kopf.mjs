/* T-0610 E2: Feature-Erklaerung (MLAttributierungNeu) zitiert keine
 * ungueltige Prognose als Zahl. T-0610 ML-6 (W7): persistenzHinweis.
 *
 * Laeuft die echte Funktion `horizontKopf` aus
 * `src/komponenten/ml_prognose_guete.ts` und prueft das ERGEBNIS.
 *
 * Lauf (in frontend/):  npm run test:ml
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import assert from 'node:assert/strict'

const WURZEL = new URL('..', import.meta.url).pathname
const QUELLE = join(WURZEL, 'src/komponenten/ml_prognose_guete.ts')
const tmp = mkdtempSync(join(tmpdir(), 't0610-e2-'))
const kopie = join(tmp, 'ml_prognose_guete.ts')
writeFileSync(kopie, readFileSync(QUELLE, 'utf8'))
const ziel = join(tmp, 'ml_prognose_guete.js')
try {
  execFileSync(
    join(WURZEL, 'node_modules/typescript/bin/tsc'),
    ['--target', 'es2022', '--module', 'es2022', '--outDir', tmp, kopie],
    { stdio: 'pipe', cwd: tmpdir() },
  )
} catch (e) {
  if (!existsSync(ziel)) { console.error(String(e.stdout ?? e)); process.exit(1) }
}
const { horizontKopf, persistenzHinweis } = await import(ziel)

let fehler = 0
function fall(name, fn) {
  try { fn(); console.log(`ok   ${name}`) }
  catch (e) { fehler += 1; console.log(`FAIL ${name}: ${e.message}`) }
}

fall('gueltige Prognose: Zahl', () => {
  assert.deepEqual(horizontKopf(24, { feuchte_prognose: 41.6, gueltig: true }), { text: '24h · Prognose 42%' })
})
fall('ohne Feld gueltig: Zahl (Altvertrag)', () => {
  assert.equal(horizontKopf(6, { feuchte_prognose: 30 }).text, '6h · Prognose 30%')
})
fall('gueltig=false: ungueltig mit Grund statt Zahl', () => {
  const k = horizontKopf(24, { feuchte_prognose: 66, gueltig: false, ungueltig_grund: 'veraltet', feature_alter_h: 25 })
  assert.equal(k.text, '24h · Prognose ungültig (veraltet)')
  assert.ok(!k.text.includes('66'))
  assert.match(k.titel, /25\.0 h alten Messzeile/)
})

// T-0610 ML-6 (W7): Hinweis nur bei gesetzter Warnung an einer angezeigten Prognose.
fall('persistenz_warnung=true: Hinweis', () => {
  const h = persistenzHinweis({ persistenz_warnung: true, gueltig: true })
  assert.equal(h.text, 'Modell schlägt Persistenz nicht')
  assert.equal(h.kurz, 'schwach')
  assert.match(h.tooltip, /^Modell schlägt Persistenz nicht/)
})
fall('persistenz_warnung=false, fehlend oder ohne Prognose: kein Hinweis', () => {
  assert.equal(persistenzHinweis({ persistenz_warnung: false, gueltig: true }), null)
  assert.equal(persistenzHinweis({ gueltig: true }), null)
  assert.equal(persistenzHinweis(null), null)
  assert.equal(persistenzHinweis(undefined), null)
})
fall('ungueltige Prognose: kein Hinweis (keine Zahl, an der er stehen koennte)', () => {
  assert.equal(persistenzHinweis({ persistenz_warnung: true, gueltig: false }), null)
})

if (fehler) { console.log(`${fehler} Fall/Faelle rot`); process.exit(1) }
console.log('ml-prognose-kopf: alle Faelle gruen')
