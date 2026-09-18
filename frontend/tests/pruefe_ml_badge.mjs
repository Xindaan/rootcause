/* T-0610 ML-11: ML-Badge im Kopf unterscheidet ML / teilweise / Heuristik.
 *
 * Laeuft die echte Funktion `mlBadgeZustand` aus
 * `src/komponenten/ml-badge-logik.ts` und prueft das ERGEBNIS. Vorher hiess
 * jedes geladene Modell "ML aktiv", auch wenn Zonen auf die Heuristik
 * zurueckfielen.
 *
 * Lauf (in frontend/):  npm run test:ml
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import assert from 'node:assert/strict'

const WURZEL = new URL('..', import.meta.url).pathname
const QUELLE = join(WURZEL, 'src/komponenten/ml-badge-logik.ts')
const tmp = mkdtempSync(join(tmpdir(), 't0610-ml11-'))
const kopie = join(tmp, 'ml-badge-logik.ts')
writeFileSync(kopie, readFileSync(QUELLE, 'utf8'))
const ziel = join(tmp, 'ml-badge-logik.js')
try {
  execFileSync(
    join(WURZEL, 'node_modules/typescript/bin/tsc'),
    ['--target', 'es2022', '--module', 'es2022', '--outDir', tmp, kopie],
    { stdio: 'pipe', cwd: tmpdir() },
  )
} catch (e) {
  if (!existsSync(ziel)) { console.error(String(e.stdout ?? e)); process.exit(1) }
}
const { mlBadgeZustand, mlBadgeMitWarnung } = await import(ziel)

let fehler = 0
function fall(name, fn) {
  try { fn(); console.log(`ok   ${name}`) }
  catch (e) { fehler += 1; console.log(`FAIL ${name}: ${e.message}`) }
}

fall('alle Zonen ML: ML aktiv', () => {
  const z = mlBadgeZustand(true, { a: 'ml', b: 'ml' })
  assert.equal(z.klasse, 'aktiv'); assert.equal(z.text, 'ML aktiv')
})
fall('gemischt: ML teilweise mit Zaehlung und Namen', () => {
  const z = mlBadgeZustand(true, { a: 'ml', b: 'heuristik', c: 'ml' }, { b: 'Bambuswald' })
  assert.equal(z.klasse, 'teilweise')
  assert.equal(z.text, 'ML teilweise (2/3)')
  assert.match(z.titel, /Heuristik: Bambuswald/)
})
fall('keine Zone ML trotz geladenem Modell: Heuristik', () => {
  const z = mlBadgeZustand(true, { a: 'heuristik', b: 'heuristik' })
  assert.equal(z.klasse, 'heuristik'); assert.equal(z.text, 'Heuristik')
})
fall('"keine" zaehlt weder als ML noch als Heuristik', () => {
  const z = mlBadgeZustand(true, { a: 'ml', b: 'keine' })
  assert.equal(z.klasse, 'aktiv')
})
fall('ohne Zonendaten nur "ML geladen", keine Behauptung', () => {
  const z = mlBadgeZustand(true, {})
  assert.equal(z.klasse, 'geladen'); assert.equal(z.text, 'ML geladen')
})
fall('kein Modell: ML aus', () => {
  const z = mlBadgeZustand(false, { a: 'ml' })
  assert.equal(z.klasse, 'inaktiv')
})
fall('Status nicht abrufbar: unbekannt', () => {
  const z = mlBadgeZustand(null, { a: 'ml' })
  assert.equal(z.klasse, 'unbekannt')
})

// T-0610 ML-6 auf dem ML-11-Modell (Merge W6): Persistenz-Warnung nur, wenn
// eine ML-Prognose im Spiel ist (aktiv/teilweise/geladen), nie bei aus/heuristik/unbekannt.
const W = [{ quelle: 'global', horizont_stunden: 24, mae: 3.2, persistenz_mae: 2.9 }]
const mitW = (geladen, quellen) => mlBadgeMitWarnung(mlBadgeZustand(geladen, quellen), geladen, W)
for (const [name, quellen] of [['aktiv', { a: 'ml' }], ['teilweise', { a: 'ml', b: 'heuristik' }], ['geladen', {}]]) {
  fall(`Warnung bei ${name}`, () => {
    const z = mitW(true, quellen)
    assert.equal(z.klasse, name)
    assert.equal(z.warnt, true)
    assert.match(z.text, / · 1× schwach$/)
    assert.match(z.titel, /global 24 h: MAE 3\.20 gegen 2\.90/)
  })
}
for (const [name, geladen, quellen] of [['inaktiv', false, { a: 'ml' }], ['heuristik', true, { a: 'heuristik' }], ['unbekannt', null, { a: 'ml' }]]) {
  fall(`keine Warnung bei ${name}`, () => {
    const z = mitW(geladen, quellen)
    assert.equal(z.klasse, name)
    assert.equal(z.warnt, false)
    assert.doesNotMatch(z.text, /schwach/)
  })
}
fall('keine Warnung ohne Warnliste', () => {
  const z = mlBadgeMitWarnung(mlBadgeZustand(true, { a: 'ml' }), true, undefined)
  assert.equal(z.warnt, false); assert.equal(z.text, 'ML aktiv')
})

if (fehler) { console.log(`${fehler} Fall/Faelle rot`); process.exit(1) }
console.log('ml-badge: alle Faelle gruen')
