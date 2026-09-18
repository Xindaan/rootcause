/* T-0601: EINE Antwort auf "ist die Zone gerade kritisch?".
 *
 * Gesamtreview 25.09. (FE-2): bei denselben vier Testzonen zaehlte der
 * Triage-Strip "3 kritisch", das Kritisch-Band "4 Zonen", die neue Uebersicht
 * "1". Band und Strip prueften weder die Ausfall-Warnung noch das Wertalter.
 * FE-3: eine Ausfall-Warnung eines NEBENsensors stufte einen frischen
 * Lead-Wert auf "Sensordaten pruefen" herab. ML-5: das Band zitierte
 * "ML sagt X% in 24 h" auch aus Prognosen mit gueltig=false.
 *
 * Geprueft wird das ERGEBNIS der echten Module (TypeScript per Require-Hook
 * transpiliert wie in uebersicht-neu.test.cjs), kein Quelltext-Match.
 *
 * Lauf (in frontend/):  npm run test:defekt  (laeuft nach pruefe_sensor_defekt)
 */
import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const WURZEL = new URL('..', import.meta.url).pathname
const require = createRequire(join(WURZEL, 'package.json'))
const ts = require('typescript')
for (const ext of ['.ts', '.tsx']) {
  require.extensions[ext] = (modul, datei) => {
    const { outputText } = ts.transpileModule(readFileSync(datei, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
      fileName: datei,
    })
    modul._compile(outputText, datei)
  }
}
require.extensions['.css'] = () => {}
const src = p => join(WURZEL, 'src', p)
const { ermittleGefahr, vorhersagen24hGueltig, gueltige24h } = require(src('komponenten/kritisch-band-logik.ts'))
const { triageBucket, triageGruppen, sortierRang } = require(src('komponenten/zonen-karte-v4/triage-logik.ts'))
const { getActionStateV4 } = require(src('komponenten/zonen-karte-v4/aktion-state-v4.ts'))
const { baueZonenAnzeige } = require(src('komponenten/uebersicht-neu/uebersicht-modell.ts'))

let fehler = 0
function pruefe(was, ist, soll) {
  const ok = JSON.stringify(ist) === JSON.stringify(soll)
  console.log(`  ${ok ? 'OK  ' : 'FAIL'}  ${was}${ok ? '' : `\n        ist:  ${JSON.stringify(ist)}\n        soll: ${JSON.stringify(soll)}`}`)
  if (!ok) fehler++
}

const JETZT = Date.parse('2026-09-25T21:15:00')
const vorH = h => new Date(JETZT - h * 3600_000).toISOString()
const LEAD = 'gardena-lead', FYTA = 'fyta_900001'
const zone = (id, extra = {}) => ({
  zone_id: id, name: id, modus: 'monitoring',
  aktuelle_feuchte: 50, feuchte_schwelle_min: 30, feuchte_schwelle_max: 70, feuchte_kritisch: 15,
  letztes_update: vorH(0.5), quelle: 'gardena', feuchte_geraet_id: LEAD,
  offene_warnungen: [], sensoren: [], ...extra,
})
const ausfall = geraet_id => ({ typ: 'ausfall', zeitstempel: vorH(0.2), details: 'kein Update', geraet_id })

// Die vier Zonen aus dem Repro des Gesamtreviews (ohne die laufende Bewaesserung,
// siehe unten): Wert mit Ausfall-Warnung des eigenen Sensors, Mischzone mit
// frischem Lead und Ausfall NUR eines Nebensensors, 5 h alter Wert ohne
// Warnung, kritisch und frisch.
const zonen = [
  zone('zitrus', { aktuelle_feuchte: 3, feuchte_geraet_id: FYTA, quelle: 'fyta', offene_warnungen: [ausfall(FYTA)] }),
  zone('wald', { aktuelle_feuchte: 20, feuchte_kritisch: 25, aggregat_lead_geraet: LEAD, offene_warnungen: [ausfall(FYTA)] }),
  zone('kroton', { aktuelle_feuchte: 12, letztes_update: vorH(5) }),
  zone('hecke', { aktuelle_feuchte: 10, feuchte_kritisch: 22 }),
]
const KRITISCH = ['wald', 'hecke']

console.log('FE-2: Strip, Band, Sortierung und neue Uebersicht zaehlen gleich')
const strip = zonen.filter(z => triageBucket(z, false, false, JETZT) === 'kritisch').map(z => z.zone_id)
const band = zonen.filter(z => ermittleGefahr(z, undefined, undefined, JETZT)?.grund === 'unter_kritisch').map(z => z.zone_id)
const sortierung = zonen.filter(z => sortierRang(z, JETZT) === 0).map(z => z.zone_id)
const neu = zonen.filter(z => baueZonenAnzeige(z, undefined, false, true, JETZT).datenBelastbar
  && z.aktuelle_feuchte < z.feuchte_kritisch).map(z => z.zone_id)
pruefe('Triage-Strip "kritisch"', strip, KRITISCH)
pruefe('Kritisch-Band "unter_kritisch"', band, KRITISCH)
pruefe('Karten-Sortierung Rang 0', sortierung, KRITISCH)
pruefe('neue Uebersicht (belastbar und unter kritisch)', neu, KRITISCH)
pruefe('Strip: Ausfall des Quell-Sensors -> keine Daten', triageBucket(zonen[0], false, false, JETZT), 'unbekannt')
pruefe('Strip: 5 h alter Wert -> keine Daten', triageBucket(zonen[2], false, false, JETZT), 'unbekannt')
const altBand = ermittleGefahr(zonen[2], undefined, undefined, JETZT)
pruefe('Band: 5 h alter Wert ist Datenlage, nicht kritisch', altBand?.grund, 'daten_unsicher')
pruefe('Band nennt das Alter', /5 h alt/.test(altBand?.text ?? ''), true)
pruefe('Band: Ausfall des Quell-Sensors ist Datenlage', ermittleGefahr(zonen[0], undefined, undefined, JETZT)?.grund, 'daten_unsicher')
pruefe('Sortierung: 5 h alter Wert rankt wie kein Wert', sortierRang(zonen[2], JETZT), 3)
pruefe('Grenze: 3,9 h alter Wert bleibt kritisch',
  triageBucket(zone('k', { aktuelle_feuchte: 5, letztes_update: vorH(3.9) }), false, false, JETZT), 'kritisch')

console.log('T-0610 Q6: kritisch bleibt kritisch, "giesst" ist Zusatzmarke')
const giessend = new Set(['hecke', 'nass_giesst', 'ohne_daten_giesst'])
const q6zonen = [
  ...zonen,
  zone('nass_giesst', { aktuelle_feuchte: 50 }),
  zone('ohne_daten_giesst', { aktuelle_feuchte: 50, letztes_update: vorH(5) }),
]
const q6 = triageGruppen(q6zonen, giessend, new Set(), JETZT)
pruefe('Strip: kritische Zone mit offenem Ventil bleibt kritisch',
  triageBucket(zonen[3], true, false, JETZT), 'kritisch')
pruefe('Strip und Band zaehlen bei laufender Bewaesserung gleich',
  q6.gruppen.kritisch.map(z => z.zone_id),
  q6zonen.filter(z => ermittleGefahr(z, undefined, undefined, JETZT)?.grund === 'unter_kritisch').map(z => z.zone_id))
pruefe('Zusatzmarke: genau die kritische Zone mit offenem Ventil',
  q6.kritischGiesst.map(z => z.zone_id), ['hecke'])
pruefe('nicht kritische Zone mit offenem Ventil bleibt "giesst"',
  q6.gruppen.giesst.map(z => z.zone_id), ['nass_giesst', 'ohne_daten_giesst'])
pruefe('nicht belastbarer Wert unter kritisch + Ventil offen -> giesst, nicht kritisch',
  triageBucket(zonen[2], true, false, JETZT), 'giesst')

console.log('FE-3: Ausfall eines Nebensensors versteckt den frischen Lead nicht')
pruefe('Karte: Lead frisch + Nebensensor-Ausfall -> kritisch', getActionStateV4(zonen[1], null), 'kritisch')
pruefe('Karte: Ausfall des Quell-Sensors -> sensor', getActionStateV4(zonen[0], null), 'sensor')
pruefe('Karte: Warnung ohne geraet_id (Zonen-Warnung) zaehlt weiter',
  getActionStateV4(zone('w', { aktuelle_feuchte: 20, offene_warnungen: [ausfall(undefined)] }), null), 'sensor')
pruefe('Karte: Median-Quelle (aggregat) -> jede Ausfall-Warnung zaehlt',
  getActionStateV4(zone('m', { aktuelle_feuchte: 20, feuchte_geraet_id: 'aggregat:2', offene_warnungen: [ausfall(FYTA)] }), null), 'sensor')
pruefe('Neu: Lead frisch + Nebensensor-Ausfall -> Kritisch trocken',
  baueZonenAnzeige(zonen[1], { zone: zonen[1], empfehlung: { soll_bewaessern: false, empfehlungs_typ: 'kein_bedarf' }, messwerte: {}, ml_vorhersage: {} }, false, true, JETZT).titel,
  'Kritisch trocken')

console.log('ML-5: ungueltige 24-h-Prognosen werden nicht zitiert')
const ml = {
  a: { '24h': { feuchte_prognose: 20, gueltig: false } },
  b: { '24h': { feuchte_prognose: 22, gueltig: true } },
  c: { '24h': { feuchte_prognose: 24 } },
  d: { '6h': { feuchte_prognose: 40 } },
}
pruefe('Band-Map nur gueltige (gueltig fehlt = gueltig)', vorhersagen24hGueltig(ml), { b: 22, c: 24 })
pruefe('Heute-Plan: ungueltige Prognose -> keine Zahl', gueltige24h(ml.a), undefined)
pruefe('Heute-Plan: gueltige Prognose bleibt', gueltige24h(ml.b)?.feuchte_prognose, 22)
const bedarf = { soll_bewaessern: true, empfehlungs_typ: 'akut' }
const mlZone = zone('a', { aktuelle_feuchte: 40 })
pruefe('Band: ungueltige Prognose + Engine-Bedarf -> kein "ML sagt"',
  ermittleGefahr(mlZone, vorhersagen24hGueltig(ml)['a'], bedarf, JETZT), null)

if (fehler > 0) {
  console.error(`\n${fehler} Pruefung(en) fehlgeschlagen.`)
  process.exit(1)
}
console.log('\nAlle Kritisch-Einstufungs-Pruefungen gruen.')
