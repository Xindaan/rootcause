/* Kleine Regressionstests gegen die echten Frontend-Module. TypeScript wird
   nur fuer diesen Testprozess transpiliert; kein zusaetzlicher Test-Runner. */
const fs = require('node:fs')
const assert = require('node:assert/strict')
const { test } = require('node:test')
const ts = require('typescript')
for (const ext of ['.ts', '.tsx']) {
  require.extensions[ext] = (modul, datei) => {
    const { outputText } = ts.transpileModule(fs.readFileSync(datei, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
      fileName: datei,
    })
    modul._compile(outputText, datei)
  }
}
require.extensions['.css'] = () => {}
const { baueZonenAnzeige, messQuelle, betriebsModus } = require('../src/komponenten/uebersicht-neu/uebersicht-modell.ts')
const JETZT = Date.parse('2026-09-09T17:00:00Z')
const zone = (extra = {}) => ({
  zone_id: 'wald', name: 'Waldblumenhain', modus: 'automatik', autonom_scharf: true,
  aktuelle_feuchte: 50, feuchte_schwelle_min: 40, feuchte_schwelle_max: 55,
  feuchte_kritisch: 25, letztes_update: '2026-09-09T16:40:00Z',
  quelle: 'gardena', feuchte_geraet_id: 'g1', aggregat_lead_geraet: 'g1',
  sensoren: [
    { geraet_id: 'g1', quelle: 'gardena', name: 'Gardena Wald', boden_feuchte: 50, zeitstempel: '2026-09-09T16:40:00Z' },
    { geraet_id: 'f1', quelle: 'fyta', name: 'FYTA Wald', boden_feuchte: 11, zeitstempel: '2026-09-09T16:40:00Z' },
  ], sensor_namen: { g1: 'Gardena Wald' }, ...extra,
})
const snapshot = (z, extra = {}) => ({
  zone: z, empfehlung: { soll_bewaessern: false, empfehlungs_typ: 'kein_bedarf', blocker_typ: 'FEUCHTE_OK', grund: 'Feuchte ausreichend' },
  messwerte: {}, ml_vorhersage: { '24h': { feuchte_prognose: 48, q10: 43, q90: 50, gueltig: true } }, ...extra,
})
const anzeigen = (z, s = snapshot(z), aktuell = true, aktiv = false) => baueZonenAnzeige(z, s, aktiv, aktuell, JETZT)

test('Lead-Wert und Herkunft bleiben erhalten, FYTA wird nicht gemittelt', () => {
  const z = zone(), a = anzeigen(z)
  assert.equal(a.zone.aktuelle_feuchte, 50)
  assert.match(messQuelle(a.zone), /Gardena Wald/)
  assert.doesNotMatch(messQuelle(a.zone), /Median/)
  assert.equal(a.aufmerksamkeit, false)
})
test('Snapshot-Messung und Empfehlung werden gemeinsam angezeigt', () => {
  const alt = zone({ aktuelle_feuchte: 10 })
  const a = anzeigen(alt, snapshot(zone()))
  assert.equal(a.zone.aktuelle_feuchte, 50)
  assert.equal(a.ton, 'ruhig')
})
test('Lead-Ausfall macht niedrigen Ersatzwert nicht zum Giesstrigger', () => {
  const a = anzeigen(zone({ aktuelle_feuchte: 11, feuchte_geraet_id: 'f1', lead_ausgefallen: true }))
  assert.equal(a.datenBelastbar, false)
  assert.equal(a.empfehlung, null)
  assert.equal(a.prognose, undefined)
  assert.equal(a.ton, 'unbekannt')
})
test('Kalibrierung unterdrueckt Empfehlung und Modellwert', () => {
  const a = anzeigen(zone({ ausschluss_fenster: [{ von: '2026-09-08T00:00:00Z', bis: '2026-09-11T00:00:00Z', zweck: 'sensor_kalibrierung' }] }))
  assert.match(a.titel, /Kalibrierung/)
  assert.equal(a.empfehlung, null)
  assert.equal(a.prognose, undefined)
})
test('Event-Ignore ist keine Sensorkalibrierung', () => {
  const a = anzeigen(zone({ ausschluss_fenster: [{ von: '2026-09-08T00:00:00Z', bis: '2026-09-11T00:00:00Z', zweck: 'event_ignore' }] }))
  assert.equal(a.datenBelastbar, true)
})
test('Kein aktueller Messwert: letzter bekannter Wert bleibt nur Kontext', () => {
  const a = anzeigen(zone({ aktuelle_feuchte: null, letztes_update: null, letzter_bekannter_wert: 9 }))
  assert.equal(a.zone.letzter_bekannter_wert, 9)
  assert.equal(a.datenBelastbar, false)
  assert.equal(a.empfehlung, null)
  assert.equal(a.aufmerksamkeit, true)
})
test('240-Minuten-Grenze wird auch zwischen zwei Polls eingehalten', () => {
  assert.equal(anzeigen(zone({ letztes_update: '2026-09-09T13:00:00Z' })).datenBelastbar, true)
  assert.equal(anzeigen(zone({ letztes_update: '2026-09-09T12:59:59Z' })).datenBelastbar, false)
})
test('Fehlgeschlagener Snapshot-Refresh zeigt keine veraltete Empfehlung', () => {
  const z = zone(), a = anzeigen(z, snapshot(z), false)
  assert.equal(a.zone.aktuelle_feuchte, 50)
  assert.equal(a.empfehlung, null)
  assert.equal(a.prognose, undefined)
  assert.match(a.titel, /nicht aktuell/)
})
test('Fehlender Snapshot behauptet nicht: kein Bedarf', () => {
  const a = baueZonenAnzeige(zone(), undefined, false, true, JETZT)
  assert.equal(a.ton, 'unbekannt')
  assert.equal(a.empfehlung, null)
})
test('Null-Obergrenze bedeutet keine Nasswarnung', () => {
  const a = anzeigen(zone({ aktuelle_feuchte: 80, feuchte_schwelle_max: null }))
  assert.equal(a.ton, 'ruhig')
})
test('Echte Nullmessung folgt dem Backend-Urteil', () => {
  assert.equal(anzeigen(zone({ aktuelle_feuchte: 0, null_ist_defekt: false })).ton, 'kritisch')
  assert.equal(anzeigen(zone({ aktuelle_feuchte: 0, null_ist_defekt: true })).ton, 'unbekannt')
})
test('Ungueltige Prognose nach Sensortausch bleibt verborgen', () => {
  const z = zone(), s = snapshot(z, { ml_vorhersage: { '24h': { feuchte_prognose: 66, gueltig: false, ungueltig_grund: 'geraetewechsel' } } })
  assert.equal(anzeigen(z, s).prognose, undefined)
})
test('Laufender Kanal bleibt trotz Sensorausfall als aktiv sichtbar', () => {
  const z = zone({ aktuelle_feuchte: null }), a = anzeigen(z, snapshot(z), false, true)
  assert.equal(a.titel, 'Bewässerung läuft')
  assert.equal(a.ton, 'aktiv')
  assert.equal(a.empfehlung, null)
})
test('Unbekannte Schaerfung wird nicht als aktive Automatik beschriftet', () => {
  assert.equal(betriebsModus(zone({ autonom_scharf: undefined })), 'Automatikstatus unbekannt')
  assert.match(betriebsModus(zone({ autonom_scharf: false })), /Shadow/)
})
