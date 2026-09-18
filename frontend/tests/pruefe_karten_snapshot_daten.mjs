/* T-0610 FE-9: V4-Karten warten auf den ersten Bulk-Snapshot.
 *
 * Laeuft die echte Funktion aus
 * `src/komponenten/zonen-karte-v4/karten-snapshot-daten.ts` und zaehlt, wie
 * viele Einzelabrufe die Karten beim ersten Laden ausloesen. Die Karte
 * (ZonenKarteV4.tsx) holt Messwerte/ML/Empfehlung genau dann selbst, wenn das
 * jeweilige `*Propagiert`-Prop `undefined` ist (ML nur mit mlVerfuegbar);
 * diese Regel bildet `einzelabrufe` unten nach.
 *
 *  - vor dem ersten Snapshot-Versuch: 0 Einzelabrufe (vorher 14 x 3 = 42),
 *  - Snapshot gescheitert: alle Karten holen selbst (T-0565-Fallback bleibt),
 *  - Snapshot da, eine Zone fehlt: nur diese holt selbst,
 *  - Snapshot da, Feld fehlt: Stub statt undefined (kein Einzelabruf),
 *  - Stubs sind referenzstabil (React.memo der Karte).
 *
 * Lauf (in frontend/):  npm run test:kartendaten
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import assert from 'node:assert/strict'

const WURZEL = new URL('..', import.meta.url).pathname
const QUELLE = join(WURZEL, 'src/komponenten/zonen-karte-v4/karten-snapshot-daten.ts')
const tmp = mkdtempSync(join(tmpdir(), 't0610-fe9-'))
const kopie = join(tmp, 'karten-snapshot-daten.ts')
writeFileSync(kopie, readFileSync(QUELLE, 'utf8'))
const ziel = join(tmp, 'karten-snapshot-daten.js')
try {
  execFileSync(
    join(WURZEL, 'node_modules/typescript/bin/tsc'),
    ['--target', 'es2022', '--module', 'es2022', '--outDir', tmp, kopie],
    { stdio: 'pipe', cwd: tmpdir() },
  )
} catch (e) {
  if (!existsSync(ziel)) { console.error(String(e.stdout ?? e)); process.exit(1) }
}
const { kartenSnapshotDaten } = await import(ziel)

const ZONEN = Array.from({ length: 14 }, (_, i) => `zone_${i}`)
const snapZone = id => ({
  zone_id: id, messwerte: { '48h': [] }, ml_vorhersage: { '24': {} }, empfehlung: { zone_id: id },
})

function einzelabrufe(snapshots, versucht, mlVerfuegbar = true) {
  let n = 0
  for (const id of ZONEN) {
    const d = kartenSnapshotDaten(snapshots[id], versucht)
    if (d.messwertePropagiert === undefined) n += 1
    if (d.mlVorhersagePropagiert === undefined && mlVerfuegbar) n += 1
    if (d.empfehlungPropagiert === undefined) n += 1
  }
  return n
}

let fehler = 0
function fall(name, fn) {
  try { fn(); console.log(`ok   ${name}`) }
  catch (e) { fehler += 1; console.log(`FAIL ${name}: ${e.message}`) }
}

fall('vor dem ersten Snapshot-Versuch: keine Einzelabrufe (vorher 42)', () => {
  assert.equal(einzelabrufe({}, false), 0)
})
fall('Snapshot gescheitert: alle 14 Karten holen selbst (T-0565)', () => {
  assert.equal(einzelabrufe({}, true), 42)
  assert.equal(einzelabrufe({}, true, false), 28)
})
fall('Snapshot da: keine Einzelabrufe', () => {
  const alle = Object.fromEntries(ZONEN.map(id => [id, snapZone(id)]))
  assert.equal(einzelabrufe(alle, true), 0)
})
fall('Snapshot da, eine Zone fehlt (neu konfiguriert): nur sie holt selbst', () => {
  const ohneEine = Object.fromEntries(ZONEN.slice(1).map(id => [id, snapZone(id)]))
  assert.equal(einzelabrufe(ohneEine, true), 3)
})
fall('Snapshot da, Felder fehlen: Stubs statt undefined', () => {
  const d = kartenSnapshotDaten({ zone_id: 'x' }, true)
  assert.notEqual(d.messwertePropagiert, undefined)
  assert.notEqual(d.mlVorhersagePropagiert, undefined)
  assert.equal(d.empfehlungPropagiert, null)
})
fall('Snapshot-Werte werden unveraendert durchgereicht', () => {
  const s = snapZone('a')
  const d = kartenSnapshotDaten(s, true)
  assert.equal(d.messwertePropagiert, s.messwerte)
  assert.equal(d.mlVorhersagePropagiert, s.ml_vorhersage)
  assert.equal(d.empfehlungPropagiert, s.empfehlung)
})
fall('Warte-Stubs sind referenzstabil (React.memo)', () => {
  const a = kartenSnapshotDaten(undefined, false)
  const b = kartenSnapshotDaten(undefined, false)
  assert.equal(a.messwertePropagiert, b.messwertePropagiert)
  assert.equal(a.mlVorhersagePropagiert, b.mlVorhersagePropagiert)
})

if (fehler > 0) { console.log(`\n${fehler} Fall/Faelle FAIL`); process.exit(1) }
console.log('\nalle Faelle ok')
