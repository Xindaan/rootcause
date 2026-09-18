/* T-0610 Q3: Historie blendet Monitoring-Routinezeilen standardmaessig aus.
 *
 * Laeuft die echte Funktion `baueEntscheidungsParams` aus
 * `src/entscheidungs-filter.ts` und prueft die erzeugten Query-Parameter.
 * Der Name `ohne_monitoring` muss exakt der Backend-Query entsprechen
 * (backend/tests/test_ops_api.py::test_historie_ohne_monitoring_filtert_vor_dem_limit
 * nutzt denselben); ein Tippfehler faellt im Backend still auf "alles zeigen".
 *
 * Lauf (in frontend/):  npm run test:historie
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import assert from 'node:assert/strict'

const WURZEL = new URL('..', import.meta.url).pathname
const QUELLE = join(WURZEL, 'src/entscheidungs-filter.ts')
const tmp = mkdtempSync(join(tmpdir(), 't0610-q3-'))
const kopie = join(tmp, 'entscheidungs-filter.ts')
writeFileSync(kopie, readFileSync(QUELLE, 'utf8'))
const ziel = join(tmp, 'entscheidungs-filter.js')
try {
  execFileSync(
    join(WURZEL, 'node_modules/typescript/bin/tsc'),
    ['--target', 'es2022', '--module', 'es2022', '--lib', 'es2022,dom', '--outDir', tmp, kopie],
    { stdio: 'pipe', cwd: tmpdir() },
  )
} catch (e) {
  if (!existsSync(ziel)) { console.error(String(e.stdout ?? e)); process.exit(1) }
}
const { baueEntscheidungsParams, blockerLabel, BLOCKER_OPTIONEN } = await import(ziel)

let fehler = 0
function fall(name, fn) {
  try { fn(); console.log(`ok   ${name}`) }
  catch (e) { fehler += 1; console.log(`FAIL ${name}: ${e.message}`) }
}

fall('ohne_monitoring=true wird als Parameter gesendet', () => {
  const p = baueEntscheidungsParams({ limit: 500, ohne_monitoring: true })
  assert.equal(p.get('ohne_monitoring'), 'true')
  assert.equal(p.get('limit'), '500')
})
fall('Schalter an (Monitoring zeigen): kein Parameter', () => {
  const p = baueEntscheidungsParams({ limit: 500, ohne_monitoring: false })
  assert.equal(p.has('ohne_monitoring'), false)
})
fall('uebrige Filter unveraendert', () => {
  const p = baueEntscheidungsParams({ zone_id: 'hecke', blocker_typ: 'FEUCHTE_OK', von: '2026-10-13T00:00:00Z' })
  assert.equal(p.toString(), 'zone_id=hecke&von=2026-10-13T00%3A00%3A00Z&blocker_typ=FEUCHTE_OK')
})
fall('T-0610 EL-3: KANAL_ENTSCHEIDET hat Beschriftung und Filteroption', () => {
  assert.equal(blockerLabel('KANAL_ENTSCHEIDET'), 'Kanal entscheidet')
  assert.ok(BLOCKER_OPTIONEN.some(o => o.wert === 'KANAL_ENTSCHEIDET'))
  assert.equal(BLOCKER_OPTIONEN[0].wert, '')
})
fall('unbekannter Blocker roh, leerer als Strich', () => {
  assert.equal(blockerLabel('NEU_X'), 'NEU_X')
  assert.equal(blockerLabel(null), '–')
})

if (fehler) { console.log(`${fehler} Fall/Faelle rot`); process.exit(1) }
console.log('entscheidungs-filter: alle Faelle gruen')
