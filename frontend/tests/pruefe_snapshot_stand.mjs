/* T-0603: Snapshot-Merge und Frische-Regel (src/komponenten/snapshot-stand.ts).
 *
 * FE-5: der 60-s-Schnellpfad ersetzte `snapshotProZone` komplett und loeschte
 * die im Drawer nachgeladene 30-Tage-Reihe samt Top-Features. Geprueft wird
 * das ERGEBNIS der echten Funktion ueber einen Schnellpfad-Durchlauf.
 * FE-6: ein gescheiterter Refresh nach einem Erfolg muss als "nicht aktuell"
 * gelten und im V4-Zweig eine Hinweiszeile erzeugen.
 *
 * Lauf (in frontend/):  npm run test:overview  (laeuft nach uebersicht-neu)
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const WURZEL = new URL('..', import.meta.url).pathname
const QUELLE = join(WURZEL, 'src/komponenten/snapshot-stand.ts')

const tmp = mkdtempSync(join(tmpdir(), 't0603-'))
const kopie = join(tmp, 'snapshot-stand.ts')
writeFileSync(kopie, readFileSync(QUELLE, 'utf8').replace(/^import type .*$/gm, ''))
const ziel = join(tmp, 'snapshot-stand.js')
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
  mergeSchnellpfad, mergeLanghistorie, snapshotAktuell, snapshotHinweisV4,
} = await import(ziel)

let fehler = 0
function pruefe(was, ist, soll) {
  const ok = ist === soll
  console.log(`  ${ok ? 'OK  ' : 'FAIL'}  ${was}${ok ? '' : `\n        ist:  ${ist}\n        soll: ${soll}`}`)
  if (!ok) fehler++
}

const JETZT = Date.parse('2026-09-26T12:00:00')
const iso = h => new Date(JETZT - h * 3600_000).toISOString()
const reihe = (...stunden) => stunden.map(h => ({ zeitstempel: iso(h), boden_feuchte: 40 }))
const TOP = [{ name: 'regen_24h', wert: 0, beitrag: -1.5 }]
const zone = (id, extra = {}) => ({ zone: { zone_id: id, aktuelle_feuchte: 40 }, empfehlung: { grund: 'alt' }, messwerte: {}, ml_vorhersage: {}, ml_vorhersage_fehler: null, ...extra })

// Stand nach Schnellpfad + Drawer-Nachladen (30d mit Details).
const vorher = {
  wald: zone('wald', {
    messwerte: { '48h': reihe(40, 2), '24h': reihe(2), '30d': reihe(600, 100, 40, 2), '7d': reihe(100, 40, 2) },
    ml_vorhersage: { '24h': { feuchte_prognose: 35, gueltig: true, top_features: TOP }, '6h': { feuchte_prognose: 39 } },
  }),
  weg: zone('weg'),
}
// Naechster Schnellpfad: nur 48h, schlanke ML ohne top_features, neue Empfehlung.
const schnell = [
  zone('wald', {
    empfehlung: { grund: 'neu' },
    messwerte: { '48h': reihe(30, 20, 1) },
    ml_vorhersage: { '24h': { feuchte_prognose: 33, gueltig: true } },
  }),
]
console.log('FE-5: Schnellpfad mergt statt zu ersetzen')
const nach = mergeSchnellpfad(vorher, schnell, JETZT)
pruefe("'30d' bleibt erhalten", nach.wald?.messwerte['30d']?.length, 4)
pruefe("'7d' bleibt erhalten", nach.wald?.messwerte['7d']?.length, 3)
pruefe('Top-Features bleiben erhalten', nach.wald?.ml_vorhersage['24h']?.top_features, TOP)
pruefe('schlanke ML ersetzt die Prognose', nach.wald?.ml_vorhersage['24h']?.feuchte_prognose, 33)
pruefe('Horizont ohne neue Prognose faellt weg', nach.wald?.ml_vorhersage['6h'], undefined)
pruefe("'48h' kommt aus dem Schnellpfad", nach.wald?.messwerte['48h']?.length, 3)
pruefe("'24h' aus dem neuen '48h' geschnitten", nach.wald?.messwerte['24h']?.length, 2)
pruefe('Empfehlung ersetzt', nach.wald?.empfehlung.grund, 'neu')
pruefe('Zone ohne neuen Snapshot faellt weg', 'weg' in nach, false)
const mitDetails = mergeSchnellpfad(vorher, [zone('wald', {
  ml_vorhersage: { '24h': { feuchte_prognose: 33, top_features: [] } },
})], JETZT)
pruefe('neue Top-Features gewinnen', mitDetails.wald.ml_vorhersage['24h'].top_features.length, 0)

console.log('Mittelstufe/Drawer: Langhistorie mergt in den Stand')
const lang = mergeLanghistorie(nach, [zone('wald', {
  messwerte: { '30d': reihe(500, 150, 3) },
  ml_vorhersage: { '24h': { feuchte_prognose: 34, top_features: TOP } },
})], JETZT)
pruefe("'48h' bleibt", lang.wald.messwerte['48h'].length, 3)
pruefe("'30d' ersetzt", lang.wald.messwerte['30d'].length, 3)
pruefe("'7d' aus dem neuen '30d' geschnitten", lang.wald.messwerte['7d'].length, 2)

console.log('FE-6: Frische-Regel und V4-Hinweis')
const vor = min => new Date(JETZT - min * 60_000).toISOString()
pruefe('frisch (1 min) ist aktuell', snapshotAktuell(vor(1), null, JETZT), true)
pruefe('Fehler nach Erfolg ist nicht aktuell', snapshotAktuell(vor(1), 'HTTP 500', JETZT), false)
pruefe('150 s alt (Server-Cache 90 s + Poll 60 s) ist noch aktuell', snapshotAktuell(vor(2.5), null, JETZT), true)
pruefe('aelter als Cache-TTL plus zwei Poll-Zyklen ist nicht aktuell', snapshotAktuell(vor(4), null, JETZT), false)
pruefe('ohne Stand ist nicht aktuell', snapshotAktuell(null, null, JETZT), false)
pruefe('kein Hinweis bei aktuellem Stand', snapshotHinweisV4(vor(1), null, JETZT), null)
pruefe('kein Hinweis vor dem ersten Erfolg', snapshotHinweisV4(null, 'HTTP 500', JETZT), null)
const hinweis = snapshotHinweisV4(vor(5), 'HTTP 500', JETZT) ?? ''
pruefe('Hinweis nennt das Scheitern', hinweis.includes('Aktualisierung fehlgeschlagen (HTTP 500)'), true)
pruefe('Hinweis nennt den Stand', hinweis.startsWith('Bewertung vom Stand '), true)

if (fehler > 0) {
  console.error(`\n${fehler} Pruefung(en) fehlgeschlagen.`)
  process.exit(1)
}
console.log('\nAlle Snapshot-Pruefungen gruen.')
