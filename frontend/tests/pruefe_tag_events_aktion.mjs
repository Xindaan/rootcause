/* T-0610 API-10/FE-4: Knopf "Regen / Fehler" in "Heute: Ventil-Events".
 *
 * Laeuft die echte Funktion `klassifiziereEventPaar` aus
 * `src/komponenten/tag-events-aktion.ts` gegen einen aufgezeichneten
 * PATCH-Stub und prueft das ERGEBNIS:
 *  - "ignoriert" geht als PATCH `{ausloser: 'ignoriert'}` mit paar=true raus
 *    (kein DELETE mehr; das Backend schreibt die Korrektur-Spur),
 *  - eine Fehlerantwort und eine geworfene Ausnahme kommen als Text zurueck
 *    (vorher still verschluckt),
 *  - eine Antwort ohne ok:true gilt nicht als Erfolg.
 *
 * Lauf (in frontend/):  npm run test:events
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import assert from 'node:assert/strict'

const WURZEL = new URL('..', import.meta.url).pathname
const QUELLE = join(WURZEL, 'src/komponenten/tag-events-aktion.ts')

const tmp = mkdtempSync(join(tmpdir(), 't0610-fe4-'))
const kopie = join(tmp, 'tag-events-aktion.ts')
writeFileSync(kopie, readFileSync(QUELLE, 'utf8'))
const ziel = join(tmp, 'tag-events-aktion.js')
try {
  execFileSync(
    join(WURZEL, 'node_modules/typescript/bin/tsc'),
    ['--target', 'es2022', '--module', 'es2022', '--outDir', tmp, kopie],
    { stdio: 'pipe', cwd: tmpdir() },
  )
} catch (e) {
  if (!existsSync(ziel)) { console.error(String(e.stdout ?? e)); process.exit(1) }
}
const { klassifiziereEventPaar } = await import(ziel)

function stub(antwort) {
  const aufrufe = []
  const patch = async (id, body, paar) => {
    aufrufe.push({ id, body, paar })
    if (antwort instanceof Error) throw antwort
    return antwort
  }
  return { patch, aufrufe }
}

let fehler = 0
async function fall(name, fn) {
  try { await fn(); console.log(`ok   ${name}`) }
  catch (e) { fehler += 1; console.log(`FAIL ${name}: ${e.message}`) }
}

await fall('Regen/Fehler setzt ignoriert auf das Paar', async () => {
  const s = stub({ ok: true, ids: [7, 8], geaendert: 2 })
  const r = await klassifiziereEventPaar(7, 'ignoriert', s.patch)
  assert.equal(r, null)
  assert.deepEqual(s.aufrufe, [{ id: 7, body: { ausloser: 'ignoriert' }, paar: true }])
})

await fall('Automatik geht ebenfalls paarweise', async () => {
  const s = stub({ ok: true })
  await klassifiziereEventPaar(3, 'automatik', s.patch)
  assert.deepEqual(s.aufrufe, [{ id: 3, body: { ausloser: 'automatik' }, paar: true }])
})

await fall('Fehlerantwort wird als Text gemeldet', async () => {
  const s = stub({ fehler: 'Event 7 nicht gefunden oder keine Aenderung' })
  const r = await klassifiziereEventPaar(7, 'ignoriert', s.patch)
  assert.equal(r, 'Event 7 nicht gefunden oder keine Aenderung')
})

await fall('Ausnahme wird als Text gemeldet, nicht geworfen', async () => {
  const s = stub(new Error('HTTP 403'))
  const r = await klassifiziereEventPaar(7, 'ignoriert', s.patch)
  assert.equal(r, 'HTTP 403')
})

await fall('Antwort ohne ok:true ist kein Erfolg', async () => {
  const s = stub({})
  const r = await klassifiziereEventPaar(7, 'ignoriert', s.patch)
  assert.notEqual(r, null)
})

if (fehler) { console.log(`${fehler} Fall/Faelle rot`); process.exit(1) }
console.log('tag-events-aktion: alle Faelle gruen')
