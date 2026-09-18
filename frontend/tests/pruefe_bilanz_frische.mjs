/* T-0575, Akzeptanzkriterium 2: Die Bilanz-Kachel kennzeichnet ihre Zahlen
 * statt zu verschweigen -- und nennt die Ursache, die WIRKLICH vorliegt.
 *
 * Eigenes Skript und kein Quelltext-Match: das Frontend hat keine
 * allgemeine Test-Suite, und ein Test, der nur nachsieht, ob irgendwo
 * "Bewaesserung geschaetzt" im Code steht, beweist nur, dass ich es
 * hingeschrieben habe. Hier laeuft die echte Funktion und ihr ERGEBNIS
 * wird geprueft.
 *
 * Lauf (in frontend/):  npm run test:bilanz
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// frontend/ -- die Datei liegt in frontend/tests/ und laeuft auch im
// oeffentlichen Ableger, der kein tools/ hat (T-0581).
const WURZEL = new URL('..', import.meta.url).pathname
const QUELLE = join(WURZEL, 'src/komponenten/bilanz_frische.ts')

const tmp = mkdtempSync(join(tmpdir(), 't0575-'))
const kopie = join(tmp, 'frische.ts')
writeFileSync(kopie, readFileSync(QUELLE, 'utf8').replace(/^import type .*$/gm, ''))
const ziel = join(tmp, 'frische.js')
try {
  execFileSync(
    join(WURZEL, 'node_modules/typescript/bin/tsc'),
    ['--target', 'es2022', '--module', 'es2022', '--outDir', tmp, kopie],
    { stdio: 'pipe', cwd: tmpdir() }  // nicht in frontend/: dort liegt eine tsconfig.json (TS5112),
  )
} catch (e) {
  if (!existsSync(ziel)) { console.error(String(e.stdout ?? e)); process.exit(1) }
}
const { bilanzHinweis } = await import(ziel)

let fehler = 0
function pruefe(was, bedingung) {
  console.log(`  ${bedingung ? 'OK  ' : 'FAIL'}  ${was}`)
  if (!bedingung) fehler++
}

console.log('Gemessene Zahlen bleiben unmarkiert (Negativprobe gegen Rauschen)')
{
  const h = bilanzHinweis({
    indikativ: false, bewaesserung_indikativ: false, quelle_niederschlag: 'archiv',
  })
  pruefe('kein Marker bei gemessenem Wetter + gemessener Bewaesserung', h.markieren === false)
  pruefe('Kurzlabel leer', h.kurz === '')
}

console.log('Bewaesserungs-Unsicherheit wird als solche benannt')
{
  // Genau der Fall, den die alte Kachel falsch erklaerte: die Unsicherheit
  // kommt von der Bewaesserung, das Wetter ist gemessen.
  const h = bilanzHinweis({
    indikativ: true, bewaesserung_indikativ: true, quelle_niederschlag: 'archiv',
  })
  pruefe('markiert', h.markieren === true)
  pruefe('Kurzlabel nennt die Bewaesserung', h.kurz === 'Bewässerung geschätzt')
  pruefe('Tooltip nennt den Literwert', /Literwert/.test(h.tooltip))
  // Der eigentliche Regressionsschutz: NICHT mehr das Wetter beschuldigen.
  pruefe('Tooltip behauptet KEINE Wetter-Ursache',
    !/Vorhersage|ERA5|Wetterdaten/.test(h.tooltip))
}

console.log('Wetter-Unsicherheit wird als solche benannt')
{
  const h = bilanzHinweis({
    indikativ: true, bewaesserung_indikativ: false, quelle_niederschlag: 'forecast',
  })
  pruefe('Kurzlabel nennt das Wetter', h.kurz === 'Wetter aus Vorhersage')
  pruefe('Tooltip nennt die Vorhersage', /Vorhersage/.test(h.tooltip))
  pruefe('Tooltip beschuldigt NICHT die Bewaesserung', !/Literwert/.test(h.tooltip))
}

console.log('Beide Seiten unsicher -> beide genannt')
{
  const h = bilanzHinweis({
    indikativ: true, bewaesserung_indikativ: true, quelle_niederschlag: 'forecast',
  })
  pruefe('Kurzlabel nennt beide', h.kurz === 'Bew. + Wetter geschätzt')
  pruefe('Tooltip nennt Bewaesserung UND Wetter',
    /Literwert/.test(h.tooltip) && /Vorhersage/.test(h.tooltip))
}

console.log('Aelteres Backend ohne `bewaesserung_indikativ`')
{
  // Vertragsbruch-Schutz: dann KEINE Ursache erfinden.
  const h = bilanzHinweis({ indikativ: true, quelle_niederschlag: 'archiv' })
  pruefe('markiert trotzdem', h.markieren === true)
  pruefe('nennt die Ursache ausdruecklich als unbekannt',
    /ohne die Ursache zu nennen/.test(h.tooltip))
  pruefe('erfindet keine Bewaesserungs-Ursache', !/Literwert/.test(h.tooltip))
}

console.log('Fehlende Wetterdaten sind eine eigene Aussage')
{
  const h = bilanzHinweis({
    indikativ: false, bewaesserung_indikativ: false, quelle_niederschlag: 'keine',
  })
  pruefe('markiert auch ohne `indikativ`', h.markieren === true)
  pruefe('Kurzlabel nennt die Luecke', h.kurz === 'ohne Wetterdaten')
}

console.log(fehler === 0 ? '\nALLE PRUEFUNGEN BESTANDEN' : `\n${fehler} FEHLER`)
process.exit(fehler === 0 ? 0 : 1)
