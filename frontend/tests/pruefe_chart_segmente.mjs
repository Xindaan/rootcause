/* T-0573, Akzeptanzkriterium 1: Innerhalb eines
 * `sensor_kalibrierung`-Fensters ist die Feuchtelinie an der
 * Fenstergrenze UNTERBROCHEN.
 *
 * Warum ein eigenes Skript und kein Quelltext-Match: das Frontend hat
 * bewusst keine Test-Suite (Projekt-CLAUDE.md), und ein Test, der nur
 * nachsieht, ob irgendwo `__seg` im Code steht, wuerde beweisen, dass
 * ich etwas hingeschrieben habe -- nicht, dass die Linie bricht. Hier
 * wird die echte Funktion transpiliert, ausgefuehrt und ihr ERGEBNIS
 * geprueft: welche Spalte traegt welchen Messwert.
 *
 * Mechanik der Unterbrechung: Recharts laeuft mit `connectNulls`, ein
 * `null` erzeugt also KEINE Luecke -- deshalb wandern die Punkte hinter
 * der Fenstergrenze in eine eigene Spalte (`...__seg1`), die als eigene
 * <Line> gerendert wird. Zwei Linien, kein verbindendes Segment.
 *
 * Lauf (in frontend/):  npm run test:segmente
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// frontend/ -- die Datei liegt in frontend/tests/ und laeuft auch im
// oeffentlichen Ableger, der kein tools/ hat (T-0581).
const WURZEL = new URL('..', import.meta.url).pathname
const QUELLE = join(WURZEL, 'src/komponenten/feuchte_chart_segmente.ts')

// --- transpilieren (nur Syntax, Typen sind zur Laufzeit ohnehin weg) ---
const tmp = mkdtempSync(join(tmpdir(), 't0573-'))
const kopie = join(tmp, 'segmente.ts')
// Typ-Importe entfernen: sie zeigen auf Module mit React/Recharts, die
// hier weder gebraucht noch aufloesbar sind.
writeFileSync(kopie, readFileSync(QUELLE, 'utf8').replace(/^import type .*$/gm, ''))
const ziel = join(tmp, 'segmente.js')
try {
  execFileSync(
    join(WURZEL, 'node_modules/typescript/bin/tsc'),
    ['--target', 'es2022', '--module', 'es2022', '--outDir', tmp, kopie],
    { stdio: 'pipe', cwd: tmpdir() }  // nicht in frontend/: dort liegt eine tsconfig.json (TS5112),
  )
} catch (e) {
  // tsc meldet Typfehler, weil die Typ-Importe oben entfernt wurden --
  // es EMITTIERT trotzdem. Ein echter Syntaxfehler faellt darunter auf,
  // weil dann keine .js entsteht. Die Typen prueft `npm run build`.
  if (!existsSync(ziel)) {
    console.error(String(e.stdout ?? e))
    process.exit(1)
  }
}
const M = await import(ziel)

// ---------------------------------------------------------------- Daten
// Realfall Maxibaer: unter `fyta_900101` stehen bis 08.09. 05:55 die
// Werte des FYTA Beam (66-67), ab 21:12 die des Terra (35 -> 30).
// Gleiche geraet_id, zwei verschiedene physische Sensoren.
const GID = 'fyta_900101'
const NACHBAR = 'gardena_90000001'
const FENSTER_KALIBRIERUNG = {
  von: '2026-09-08T06:00:00',
  bis: '2026-09-11T06:00:00',
  grund: 'Sensortausch am 08.09.',
  zweck: 'sensor_kalibrierung',
  geraet_id: GID,
}

const messwerte = [
  { zeitstempel: '2026-09-07T22:00:00', geraet_id: GID, boden_feuchte: 67 },
  { zeitstempel: '2026-09-08T05:55:00', geraet_id: GID, boden_feuchte: 66 },
  // ---- Fenstergrenze 06:00 ----
  { zeitstempel: '2026-09-08T21:12:00', geraet_id: GID, boden_feuchte: 35 },
  { zeitstempel: '2026-09-09T06:57:00', geraet_id: GID, boden_feuchte: 30 },
]
const basis = messwerte.map(m => ({ zeit: new Date(m.zeitstempel).getTime() }))
const sensoren = [{
  geraet_id: GID, quelle: 'fyta', name: 'Maxibaer',
  dataKey: `feuchte__${GID}`, farbe: '#000',
}]

// ------------------------------------------------------------- Helfer
let fehler = 0
function pruefe(name, bedingung, info) {
  if (bedingung) { console.log(`  OK    ${name}`) }
  else { console.log(`  FEHLT ${name}${info ? ` -- ${info}` : ''}`); fehler++ }
}
const spalten = z => Object.keys(z).filter(k => k.startsWith('feuchte__') && z[k] !== null)

// ------------------------------------------------- AK1: Bruch an der Grenze
console.log('AK1: Linie bricht an der Fenstergrenze')
{
  const d = M.baueChartDatenProSensor(basis, messwerte, sensoren, [FENSTER_KALIBRIERUNG])
  const basisKey = `feuchte__${GID}`
  const segKey = `feuchte__${GID}__seg1`

  pruefe('Werte VOR dem Fenster in der Basis-Spalte',
    d[0][basisKey] === 67 && d[1][basisKey] === 66, JSON.stringify(spalten(d[1])))
  pruefe('Werte IM Fenster in einer ANDEREN Spalte',
    d[2][segKey] === 35 && d[3][segKey] === 30, JSON.stringify(spalten(d[2])))
  pruefe('kein Punkt traegt beide Spalten (sonst waere die Linie verbunden)',
    d.every(z => spalten(z).length === 1))
  // Der eigentliche Beweis: es gibt keine durchgehende Spalte, die den
  // 66er und den 35er Punkt beide traegt. Genau das war der Screenshot.
  pruefe('keine Spalte verbindet 66 (Beam) mit 35 (Terra)',
    !Object.keys(d[1]).some(k => k.startsWith('feuchte__')
      && d[1][k] !== null && d[2][k] !== null))
  pruefe('Segment-Linie wird zum Rendern gemeldet',
    M.baueSegmentLinien(sensoren, d).map(s => s.dataKey).join() === segKey)
}

// --------------------------------- Negativprobe A: ohne Fenster kein Bruch
console.log('Negativprobe A: ohne Kalibrier-Fenster bleibt alles wie vor T-0573')
{
  const d = M.baueChartDatenProSensor(basis, messwerte, sensoren, [])
  const basisKey = `feuchte__${GID}`
  pruefe('alle vier Werte in EINER Spalte',
    d.every(z => z[basisKey] !== null) && d.every(z => spalten(z).length === 1))
  pruefe('keine Segment-Spalte entstanden',
    !d.some(z => Object.keys(z).some(k => k.includes('__seg'))))
  pruefe('keine Segment-Linie gemeldet', M.baueSegmentLinien(sensoren, d).length === 0)
}

// ------------- Negativprobe B: event_ignore darf NICHT schneiden (T-0304)
console.log('Negativprobe B: event_ignore schneidet nicht (Sensor ist ok)')
{
  const d = M.baueChartDatenProSensor(
    basis, messwerte, sensoren,
    [{ ...FENSTER_KALIBRIERUNG, zweck: 'event_ignore' }],
  )
  pruefe('Linie bleibt durchgezogen',
    !d.some(z => Object.keys(z).some(k => k.includes('__seg'))))
}

// ------------- Negativprobe C: fremdes geraet_id darf NICHT schneiden (T-0386)
console.log('Negativprobe C: Fenster eines anderen Sensors laesst die Linie in Ruhe')
{
  const d = M.baueChartDatenProSensor(
    basis, messwerte, sensoren,
    [{ ...FENSTER_KALIBRIERUNG, geraet_id: NACHBAR }],
  )
  pruefe('Linie bleibt durchgezogen',
    !d.some(z => Object.keys(z).some(k => k.includes('__seg'))))
}

// ------------- AK1b: auch das ENDE eines Fensters bricht
console.log('AK1b: auch die AUSTRITTS-Grenze bricht (vergangenes Fenster)')
{
  const mw = [
    { zeitstempel: '2026-09-01T10:00:00', geraet_id: GID, boden_feuchte: 70 },
    { zeitstempel: '2026-09-02T10:00:00', geraet_id: GID, boden_feuchte: 40 },
    { zeitstempel: '2026-09-04T10:00:00', geraet_id: GID, boden_feuchte: 38 },
  ]
  const b = mw.map(m => ({ zeit: new Date(m.zeitstempel).getTime() }))
  const d = M.baueChartDatenProSensor(b, mw, sensoren, [{
    ...FENSTER_KALIBRIERUNG,
    von: '2026-09-02T00:00:00', bis: '2026-09-03T00:00:00',
  }])
  // Ohne Bruch am Fenster-ENDE haetten Punkt 1 und 3 dieselbe Spalte und
  // connectNulls wuerde quer ueber das Fenster hinweg verbinden.
  pruefe('Punkt vor und Punkt nach dem Fenster teilen KEINE Spalte',
    !Object.keys(d[0]).some(k => k.startsWith('feuchte__')
      && d[0][k] !== null && d[2][k] !== null),
    JSON.stringify([spalten(d[0]), spalten(d[1]), spalten(d[2])]))
}

// ---------------- Nachbarsensor in derselben Zone bleibt unversehrt
console.log('Multi-Sensor: der gesunde Nachbar wird nicht mitgeschnitten')
{
  const mw = [
    { zeitstempel: '2026-09-07T22:00:00', geraet_id: NACHBAR, boden_feuchte: 45 },
    { zeitstempel: '2026-09-08T22:00:00', geraet_id: NACHBAR, boden_feuchte: 44 },
    { zeitstempel: '2026-09-09T02:00:00', geraet_id: GID, boden_feuchte: 30 },
  ]
  const b = mw.map(m => ({ zeit: new Date(m.zeitstempel).getTime() }))
  const s2 = [
    ...sensoren,
    { geraet_id: NACHBAR, quelle: 'gardena', name: 'Lead',
      dataKey: `feuchte__${NACHBAR}`, farbe: '#111' },
  ]
  const d = M.baueChartDatenProSensor(b, mw, s2, [FENSTER_KALIBRIERUNG])
  const nk = `feuchte__${NACHBAR}`
  pruefe('Nachbar-Linie ungeteilt trotz aktivem Fenster',
    d[0][nk] === 45 && d[1][nk] === 44)
  pruefe('Nachbar hat keine Segment-Spalte',
    !d.some(z => Object.keys(z).some(k => k.startsWith(`${nk}__seg`))))
}

// =====================================================================
// AK5 (Isomorphie-Check): derselbe Defekt im Trend "pp/h".
// `berechneTrendPpProH` nahm ersten und letzten Messwert im 6h-Fenster --
// ueber ALLE Sensoren der Zone und ueber Fenstergrenzen hinweg.
// Realzahlen waldblumenhain 09.09.2026 01:16-07:03: Gardena-Lead 40,
// FYTA A bei 9-10, FYTA B bei 10-12. Drei Sensoren, zwei nicht
// ineinander umrechenbare Skalen (T-0410).
// =====================================================================
const H = await (async () => {
  const kopieH = join(tmp, 'hilf.ts')
  const roh = readFileSync(join(WURZEL, 'src/hilfsfunktionen.ts'), 'utf8')
  // Typ-Importe raus, echten Import auf die transpilierte Segment-Datei
  // umbiegen -- die Funktion selbst bleibt unveraendert.
  writeFileSync(kopieH, roh
    .replace(/^import type [\s\S]*?from '\.\/typen'$/m, '')
    .replace("from './komponenten/feuchte_chart_segmente'", "from './segmente.js'"))
  try {
    execFileSync(
      join(WURZEL, 'node_modules/typescript/bin/tsc'),
      ['--target', 'es2022', '--module', 'es2022', '--outDir', tmp, kopieH],
      { stdio: 'pipe', cwd: tmpdir() }  // nicht in frontend/: dort liegt eine tsconfig.json (TS5112),
    )
  } catch { /* Typfehler erwartet, Emit zaehlt */ }
  return await import(join(tmp, 'hilf.js'))
})()

console.log('AK5: Trend pp/h mischt keine Sensoren mehr')
{
  const LEAD = '00000000-0000-4000-8000-000000000001'
  const jetzt = Date.now()
  const h = n => new Date(jetzt - n * 3600_000).toISOString()
  // Reihe faengt auf dem Gardena an (40) und endet auf einem FYTA (10) --
  // genau die Konstellation, die -5 pp/h erfindet.
  const gemischt = [
    { zeitstempel: h(5), geraet_id: LEAD, boden_feuchte: 40 },
    { zeitstempel: h(4), geraet_id: 'fyta_900102', boden_feuchte: 9 },
    { zeitstempel: h(1), geraet_id: LEAD, boden_feuchte: 40 },
    { zeitstempel: h(0.1), geraet_id: 'fyta_900102', boden_feuchte: 10 },
  ]
  const ohneLead = H.berechneTrendPpProH(gemischt)
  const mitLead = H.berechneTrendPpProH(gemischt, { leadGeraet: LEAD })

  pruefe('ohne Lead-Angabe: kein Trend statt eines gemischten',
    ohneLead === null, `war ${ohneLead}`)
  pruefe('mit Lead: rechnet nur auf dem Lead (40 -> 40 = 0 pp/h)',
    mitLead !== null && Math.abs(mitLead) < 0.01, `war ${mitLead}`)
  // Der alte Code haette hier (10-40)/4.9 = -6.1 pp/h gemeldet.
  pruefe('der erfundene Sturz-Trend entsteht nicht mehr',
    mitLead === null || mitLead > -1, `war ${mitLead}`)
}

console.log('Negativprobe D: Single-Sensor-Zone rechnet unveraendert weiter')
{
  const jetzt = Date.now()
  const h = n => new Date(jetzt - n * 3600_000).toISOString()
  const einer = [
    { zeitstempel: h(4), geraet_id: 'fyta_1', boden_feuchte: 40 },
    { zeitstempel: h(0), geraet_id: 'fyta_1', boden_feuchte: 20 },
  ]
  const t = H.berechneTrendPpProH(einer, { leadGeraet: null })
  pruefe('echter Trend bleibt erhalten (-5 pp/h)',
    t !== null && Math.abs(t + 5) < 0.01, `war ${t}`)
}

console.log('AK5b: Trend rechnet nicht ueber eine Kalibrier-Fenstergrenze')
{
  const jetzt = Date.now()
  const h = n => new Date(jetzt - n * 3600_000).toISOString()
  const grenze = new Date(jetzt - 2 * 3600_000).toISOString()
  const reihe = [
    { zeitstempel: h(5), geraet_id: 'fyta_900101', boden_feuchte: 66 },
    { zeitstempel: h(4), geraet_id: 'fyta_900101', boden_feuchte: 66 },
    // ---- Sensortausch ----
    { zeitstempel: h(1.5), geraet_id: 'fyta_900101', boden_feuchte: 31 },
    { zeitstempel: h(0.1), geraet_id: 'fyta_900101', boden_feuchte: 30 },
  ]
  const f = [{
    von: grenze, bis: new Date(jetzt + 3600_000).toISOString(),
    grund: 'Sensortausch', zweck: 'sensor_kalibrierung',
    geraet_id: 'fyta_900101',
  }]
  const ohne = H.berechneTrendPpProH(reihe)
  const mit = H.berechneTrendPpProH(reihe, { fenster: f })
  // Ohne Grenze: (30-66)/4.9 = -7.3 pp/h -- ein Geraetewechsel als
  // Austrocknung gelesen, exakt der Defekt aus dem Screenshot.
  pruefe('ohne Fenster wuerde der Wechsel als Sturz gelesen',
    ohne !== null && ohne < -5, `war ${ohne}`)
  pruefe('mit Fenster: nur das Segment nach dem Tausch (31 -> 30)',
    mit !== null && mit > -1 && mit < 0, `war ${mit}`)
}

rmSync(tmp, { recursive: true, force: true })
console.log(fehler === 0 ? '\nALLE PRUEFUNGEN BESTANDEN' : `\n${fehler} PRUEFUNG(EN) FEHLGESCHLAGEN`)
process.exit(fehler === 0 ? 0 : 1)
