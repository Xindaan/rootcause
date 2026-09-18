/* T-0583: Der 0.0-Guard der Anzeige urteilt NICHT mehr selbst.
 *
 * `istWahrscheinlichSensorDefekt` reicht das Urteil des Backends
 * (`zone.null_ist_defekt`) durch und faellt ohne dieses Urteil auf "kein
 * Defektverdacht" zurueck. Bis 19.09.2026 stand an dieser Stelle die durch
 * T-0502 widerlegte Regel "Wert 0 und Quelle Gardena/unbekannt = defekt" --
 * sie urteilt am Wert statt an der Trajektorie und meldete die magerwiese
 * (35 -> ... -> 0 ueber zwoelf Tage) als defekt.
 *
 * Eigenes Skript und kein Quelltext-Match: das Frontend hat keine allgemeine
 * Test-Suite, und ein Test, der nachsieht, ob "return false" im Code steht,
 * beweist nur, dass ich es hingeschrieben habe. Hier laeuft die echte
 * Funktion und ihr ERGEBNIS wird geprueft.
 *
 * Lauf (in frontend/):  npm run test:defekt
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// frontend/ -- die Datei liegt in frontend/tests/ und laeuft auch im
// oeffentlichen Ableger, der kein tools/ hat (T-0581).
const WURZEL = new URL('..', import.meta.url).pathname
const QUELLE = join(WURZEL, 'src/komponenten/sensor-defekt.ts')

// --- transpilieren (nur Syntax, Typen sind zur Laufzeit ohnehin weg) ---
const tmp = mkdtempSync(join(tmpdir(), 't0583-'))
const kopie = join(tmp, 'sensor-defekt.ts')
// Typ-Importe entfernen: sie zeigen auf Module mit React/Recharts, die hier
// weder gebraucht noch aufloesbar sind (Linie wie pruefe_chart_segmente.mjs).
writeFileSync(kopie, readFileSync(QUELLE, 'utf8').replace(/^import type .*$/gm, ''))
// `sensorNameInZone` holt den Klartextnamen aus dem Chart-Helfer -- ein
// echter Wert-Import, der nicht wegfaellt. Die drei geprueften Funktionen
// brauchen ihn nicht, das Modul laedt ihn aber beim Import. Deshalb ein
// Platzhalter statt der echten .tsx, die Recharts nachzieht.
writeFileSync(join(tmp, 'feuchte_chart_helpers.ts'),
  'export function sensorAnzeigeName(id, name, quelle) '
  + '{ return name ?? `${quelle ?? "?"}:${id}` }\n')
const ziel = join(tmp, 'sensor-defekt.js')
try {
  execFileSync(
    join(WURZEL, 'node_modules/typescript/bin/tsc'),
    ['--target', 'es2022', '--module', 'es2022', '--outDir', tmp,
      kopie, join(tmp, 'feuchte_chart_helpers.ts')],
    { stdio: 'pipe', cwd: tmpdir() }  // nicht in frontend/: dort liegt eine tsconfig.json (TS5112),
  )
} catch (e) {
  // tsc meldet Typfehler, weil die Typ-Importe oben entfernt wurden -- es
  // EMITTIERT trotzdem. Ein echter Syntaxfehler faellt darunter auf, weil
  // dann keine .js entsteht. Die Typen prueft `npm run build`.
  if (!existsSync(ziel)) { console.error(String(e.stdout ?? e)); process.exit(1) }
}
// tsc laesst den Spezifizierer so stehen, wie er im Quelltext steht; Node
// braucht im ESM-Modus die Endung.
writeFileSync(ziel, readFileSync(ziel, 'utf8')
  .replace("'./feuchte_chart_helpers'", "'./feuchte_chart_helpers.js'"))
const { istWahrscheinlichSensorDefekt } = await import(ziel)

let fehler = 0
function pruefe(was, ist, soll) {
  const ok = ist === soll
  console.log(`  ${ok ? 'OK  ' : 'FAIL'}  ${was}${ok ? '' : `\n        ist:  ${ist}\n        soll: ${soll}`}`)
  if (!ok) fehler++
}

// Minimale Zone; nur die Felder, die der Guard liest.
const zone = (felder) => ({ zone_id: 'z', name: 'z', ...felder })

console.log('Das Backend-Urteil entscheidet')
pruefe('Sprung aus gesundem Bereich (hecke, Sensor im Lager) -> Defekt',
  istWahrscheinlichSensorDefekt(
    zone({ aktuelle_feuchte: 0, quelle: 'gardena', null_ist_defekt: true })), true)
pruefe('Abstieg ueber Tage (magerwiese) -> echter Messwert',
  istWahrscheinlichSensorDefekt(
    zone({ aktuelle_feuchte: 0, quelle: 'gardena', null_ist_defekt: false })), false)
pruefe('FYTA-Null mit Backend-Urteil false -> echter Messwert',
  istWahrscheinlichSensorDefekt(
    zone({ aktuelle_feuchte: 0, quelle: 'fyta', null_ist_defekt: false })), false)

console.log('Ohne Backend-Urteil kein Defektverdacht (Negativprobe gegen die widerlegte Regel)')
// Genau diese drei Faelle lieferten mit der alten Regel `true` -- sie ist der
// Grund fuer den Test: fiele sie zurueck, schlagen sie an.
pruefe('Feld fehlt, Wert 0, Quelle Gardena',
  istWahrscheinlichSensorDefekt(
    zone({ aktuelle_feuchte: 0, quelle: 'gardena' })), false)
pruefe('Feld fehlt, Wert 0, Quelle unbekannt',
  istWahrscheinlichSensorDefekt(
    zone({ aktuelle_feuchte: 0, quelle: null })), false)
pruefe('Feld null (JSON), Wert 0, Quelle Gardena',
  istWahrscheinlichSensorDefekt(
    zone({ aktuelle_feuchte: 0, quelle: 'gardena', null_ist_defekt: null })), false)

console.log('Nur die 0 stellt die Frage ueberhaupt')
pruefe('Wert 22 -> kein Defekt',
  istWahrscheinlichSensorDefekt(
    zone({ aktuelle_feuchte: 22, quelle: 'gardena' })), false)
pruefe('kein Messwert -> kein Defekt (das faengt der Sensor-Zustand ab)',
  istWahrscheinlichSensorDefekt(
    zone({ aktuelle_feuchte: null, quelle: 'gardena' })), false)

console.log(fehler === 0 ? '\nAlle Pruefungen bestanden.' : `\n${fehler} Pruefung(en) fehlgeschlagen.`)
process.exit(fehler === 0 ? 0 : 1)
