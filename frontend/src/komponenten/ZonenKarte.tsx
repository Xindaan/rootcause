/* Karte fuer eine einzelne Zone mit Sensordaten, Prognose und Chart. */

import { useEffect, useState } from 'react'
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ReferenceLine, ResponsiveContainer, Line,
} from 'recharts'
import type {
  Zone, Prognose, Messwert, MLVorhersage, SchwellenVorschlag,
  GiessEmpfehlung,
} from '../typen'
import {
  holeMesswerte, holeMLVorhersage,
  starteWartungsFenster, beendeWartungsFenster,
  holeGiessEmpfehlung,
} from '../api'
import { datumZeitFormat, dauerHauptSekunden } from '../hilfsfunktionen'
import { FeuchteAnzeige } from './FeuchteAnzeige'
import { ManuellesGiessen } from './ManuellesGiessen'
import { EntscheidungsErklaerung } from './EntscheidungsErklaerung'
import { GiessEmpfehlungPanel } from './GiessEmpfehlungPanel'
import { UnbekanntEventBanner } from './UnbekanntEventBanner'
import {
  baueAusschlussMarkierungen,
  baueChartDatenProSensor,
  baueSensorLinienMeta,
  baueXTickRenderer,
  hatGemischteQuellen,
} from './feuchte_chart_helpers'
import { MLDriftAmpel } from './MLDriftAmpel'
import { MLPrognoseVsIstChart } from './MLPrognoseVsIstChart'
import { WasserBilanz } from './WasserBilanz'
import { SensorListeDiagnose } from './SensorListeDiagnose'
import { MLAttributierung } from './MLAttributierung'
import './ZonenKarte.css'

// T-0243: Sensor-Warnungs-Klassen (Spiegel von backend SensorWarnungTyp
// + api_server.py titel_map). Duplikation zu ZonenKarteNeu.tsx ist OK
// solange V1 noch nicht ausgemustert ist (T-0248).
const WARNUNG_LABEL: Record<string, string> = {
  ausfall: 'Sensor-Ausfall',
  batterie_niedrig: 'Batterie niedrig',
  batterie_kritisch: 'Batterie kritisch',
  bewaesserung_ohne_wirkung: 'Bewässerung ohne Wirkung',
  sensor_eingefroren: 'Sensor eingefroren',
}
const WARNUNG_KRITISCH = new Set([
  'ausfall',
  'batterie_kritisch',
  'bewaesserung_ohne_wirkung',
  'sensor_eingefroren',
])

interface Props {
  zone: Zone
  prognose?: Prognose
  mlVerfuegbar: boolean
  bewaesserungAktiv?: boolean
  schwellenVorschlag?: SchwellenVorschlag
  /** T-0228 Stufe 2: id des offenen Wartungs-Fensters fuer diese Zone,
   *  oder undefined wenn keines aktiv. */
  wartungsFensterId?: number
  /** T-0228 Stufe 2: Callback nach Start/Stop -- App lade Mass-State neu. */
  onWartungsAenderung?: () => void
}

const ZEITRAEUME = [
  { label: '24h', stunden: 24 },
  { label: '48h', stunden: 48 },
  { label: '7d', stunden: 168 },
  { label: '30d', stunden: 720 },
] as const

// Passenden Tick-Abstand fuer ~6-8 Ticks waehlen
const TICK_STUFEN = [3, 6, 12, 24, 48, 72, 168] // Stunden
function tickAbstandFuerSpanne(spanneMs: number): number {
  const spanneH = spanneMs / 3600_000
  for (const stufe of TICK_STUFEN) {
    if (spanneH / stufe <= 10) return stufe
  }
  return TICK_STUFEN[TICK_STUFEN.length - 1]
}

export function ZonenKarte({
  zone, prognose, mlVerfuegbar, bewaesserungAktiv, schwellenVorschlag,
  wartungsFensterId, onWartungsAenderung,
}: Props) {
  const [messwerte, setMesswerte] = useState<Messwert[]>([])
  const [mlVorhersage, setMlVorhersage] = useState<Record<string, MLVorhersage>>({})
  const [refreshZaehler, setRefreshZaehler] = useState(0)
  // T-0279: Empfehlung 1x pro Karte holen, damit ManuellesGiessen die
  // empfohlene Hauptdauer in den Live-/Pre-Soak-Inputs vorbefuellen kann.
  // Eigener Fetch (V0 hat keinen Snapshot-Mechanismus wie V3).
  // GiessEmpfehlungPanel fetcht zusaetzlich -- akzeptiert weil 60 s
  // Cache-Header. Auf Backend-Last-Sicht: 14 Karten x 1 Call/Min = 14/Min.
  const [empfehlung, setEmpfehlung] = useState<GiessEmpfehlung | null>(null)
  const [zeitraumIdx, setZeitraumIdx] = useState(1) // Standard: 48h
  const zeitraum = ZEITRAEUME[zeitraumIdx]

  useEffect(() => {
    const timer = setInterval(() => setRefreshZaehler(z => z + 1), 60_000)
    return () => clearInterval(timer)
  }, [])

  // Messwerte: Zone + Zeitraum (Click auf 24h/48h/7d/30d). Separat vom
  // ML-Fetch, damit ein Zeitraum-Wechsel nicht auch die ML-Vorhersage neu
  // zieht — das kostete pro Klick ~1 s Backend-Warten und fuehlte sich
  // trage an (User-Feedback 2026-04-21).
  useEffect(() => {
    let abgebrochen = false
    holeMesswerte(zone.zone_id, zeitraum.stunden)
      .then(daten => { if (!abgebrochen) setMesswerte(daten) })
      .catch(() => { if (!abgebrochen) setMesswerte([]) })
    return () => { abgebrochen = true }
  }, [zone.zone_id, refreshZaehler, zeitraum.stunden])

  // T-0279: Empfehlungs-Fetch (1x bei Mount + bei Refresh-Tick).
  useEffect(() => {
    const ctrl = new AbortController()
    holeGiessEmpfehlung(zone.zone_id, ctrl.signal)
      .then(d => setEmpfehlung(d))
      .catch(() => { /* still bleiben, ManuellesGiessen rendert dann ohne Banner */ })
    return () => ctrl.abort()
  }, [zone.zone_id, refreshZaehler])

  // ML-Vorhersage: eigene Deps (Zone + Verfuegbarkeits-Flag + Refresh-Timer).
  // Aendert sich NICHT bei Zeitraum-Wechsel.
  useEffect(() => {
    let abgebrochen = false
    if (mlVerfuegbar) {
      holeMLVorhersage(zone.zone_id)
        .then(daten => { if (!abgebrochen) setMlVorhersage(daten) })
        .catch(() => { if (!abgebrochen) setMlVorhersage({}) })
    } else {
      // Verzoegert zuruecksetzen um Lint-Regel (set-state-in-effect) zu umgehen
      Promise.resolve().then(() => { if (!abgebrochen) setMlVorhersage({}) })
    }
    return () => { abgebrochen = true }
  }, [zone.zone_id, mlVerfuegbar, refreshZaehler])

  // Chart-Daten aufbereiten
  type ChartPunkt = {
    zeit: number
    feuchte: number | null | undefined
    temperatur: number | null | undefined
    prognose: number | undefined
    // T-0046: Quantile-Band [q10, q90] als Recharts-Range-Area
    band: [number, number] | undefined
    q10?: number
    q90?: number
  } & Record<string, number | null | undefined | [number, number]>

  // T-0211c: Pro-Sensor-Meta-Infos (geraet_id, Klartextname, Farbe).
  // Bei Single-Sensor-Zonen: 1 Eintrag, Linie sieht aus wie vorher.
  // Bei Multi-Sensor (z.B. waldblumenhain): pro Sensor eine eigene
  // Linie -- statt einer Misch-Linie durch alle Sensoren.
  const sensorMeta = baueSensorLinienMeta(messwerte, zone.sensoren, zone.sensor_namen)

  const basisDaten: ChartPunkt[] = messwerte.map(m => ({
    zeit: new Date(m.zeitstempel).getTime(),
    feuchte: m.boden_feuchte,
    temperatur: m.boden_temperatur,
    prognose: undefined,
    band: undefined,
  }))
  const chartDaten: ChartPunkt[] = baueChartDatenProSensor(
    basisDaten, messwerte, sensorMeta,
  ) as ChartPunkt[]
  // T-0410: Gardena + FYTA in einer Zone -> die Schwellen-Linien sind
  // gegen Gardena kalibriert und gelten fuer die FYTA-Linien nicht.
  const schwellenSuffix = hatGemischteQuellen(sensorMeta) ? ' (Gardena)' : ''

  if (Object.keys(mlVorhersage).length > 0 && chartDaten.length > 0) {
    const letzterZeitpunkt = chartDaten[chartDaten.length - 1].zeit
    const letzteFeuchte = chartDaten[chartDaten.length - 1].feuchte
    chartDaten[chartDaten.length - 1].prognose = letzteFeuchte ?? undefined
    // Anker-Punkt fuer das Band auf die aktuelle Feuchte — vermeidet
    // dass das Band erst beim 6h-Punkt anfaengt.
    if (typeof letzteFeuchte === 'number') {
      chartDaten[chartDaten.length - 1].band = [letzteFeuchte, letzteFeuchte]
    }
    for (const [horizont, v] of Object.entries(mlVorhersage)) {
      const h = parseInt(horizont)
      if (isNaN(h)) continue
      chartDaten.push({
        zeit: letzterZeitpunkt + h * 3600_000,
        feuchte: undefined,
        temperatur: undefined,
        prognose: v.feuchte_prognose,
        band: v.q10 != null && v.q90 != null ? [v.q10, v.q90] : undefined,
        q10: v.q10,
        q90: v.q90,
      })
    }
  }

  const hatQuantileBand = chartDaten.some(p => p.band !== undefined)

  // Ticks auf runde Stunden — Abstand passt sich an tatsaechliche Datenspanne an
  const achsenTicks = (() => {
    if (chartDaten.length < 2) return undefined
    const erster = chartDaten[0].zeit
    const letzter = chartDaten[chartDaten.length - 1].zeit
    const abstandH = tickAbstandFuerSpanne(letzter - erster)
    const abstandMs = abstandH * 3600_000
    const startTick = Math.ceil(erster / abstandMs) * abstandMs
    const ticks: number[] = []
    for (let t = startTick; t <= letzter; t += abstandMs) {
      ticks.push(t)
    }
    return ticks
  })()

  // Achsenformat basierend auf tatsaechlicher Datenspanne
  const datenSpanneH = chartDaten.length >= 2
    ? (chartDaten[chartDaten.length - 1].zeit - chartDaten[0].zeit) / 3600_000
    : 0

  // Status-Farbe fuer Akzentstreifen
  let akzentFarbe = 'var(--farbe-gesund)'
  if (zone.aktuelle_feuchte !== null) {
    if (zone.aktuelle_feuchte < zone.feuchte_schwelle_min) akzentFarbe = 'var(--farbe-gefahr)'
    else if (zone.aktuelle_feuchte > zone.feuchte_schwelle_max) akzentFarbe = 'var(--farbe-info)'
    else if (prognose?.bewaesserung_erwartet) akzentFarbe = 'var(--farbe-warnung)'
  } else {
    akzentFarbe = 'var(--rahmen)'
  }

  return (
    <div id={`zone-${zone.zone_id}`} className="karte" style={{ borderTopColor: akzentFarbe }}>
      <div className="karte-header">
        <div>
          <h2>{zone.name}</h2>
          {/* T-0397 (F5): Shadow von scharf unterscheiden -- eine automatik-
              Zone ohne autonom_scharf ist Shadow (die Engine giesst NICHT).
              Vorher trug die kritische Shadow-Zone Waldblumenhain dasselbe
              "Automatik"-Badge wie scharfe Zonen (T-0370-A1, im Legacy-Tab nie
              gefixt) -- wer hier prueft, haelt sie faelschlich fuer versorgt. */}
          <span className={`badge ${zone.modus}`}>
            {zone.modus !== 'automatik' ? 'Monitoring'
              : zone.autonom_scharf ? 'Automatik' : 'Shadow'}
          </span>
          {bewaesserungAktiv && (
            <span className="badge bewaesserung-aktiv">Bewaessert</span>
          )}
          {/* T-0243: offene Sensor-Warnungen als Badges, analog V1
              (ZonenKarteNeu.tsx). Backend liefert `offene_warnungen[]` pro
              Zone (api_server._baue_zone_dict). Ohne diese Badges blieben
              SENSOR_AUSFALL / BATTERIE_KRITISCH / BEWAESSERUNG_OHNE_WIRKUNG
              / SENSOR_EINGEFROREN im Default-Tab "Uebersicht" unsichtbar.
              kritisch=rot, sonst gelb (gleiche Map wie V1). */}
          {(zone.offene_warnungen ?? []).map((w, idx) => (
            <span
              key={`${w.typ}-${idx}`}
              className={
                WARNUNG_KRITISCH.has(w.typ)
                  ? 'badge warnung-kritisch'
                  : 'badge warnung-hinweis'
              }
              title={w.details ?? undefined}
            >
              ⚠ {WARNUNG_LABEL[w.typ] ?? w.typ}
            </span>
          ))}
          {/* T-0228 Stufe 2: Wartungs-Badge wenn ein offenes Fenster
              aktiv ist. Klick auf das Badge beendet das Fenster
              (Toggle-UX). Heuristik (sensor_backfill) pausiert
              waehrend des Fensters. */}
          {wartungsFensterId !== undefined && (
            <button
              type="button"
              className="badge wartung-aktiv"
              title="Wartungs-Modus aktiv (Heuristik pausiert). Klick beendet."
              onClick={async () => {
                await beendeWartungsFenster(wartungsFensterId)
                onWartungsAenderung?.()
              }}
            >
              🔧 WARTUNG
            </button>
          )}
          {/* T-0245: AquaBloom-Pumpen-Pill (Solar-Tropfer an FYTA-Topf).
              Bewaesserungs-System, kein Sensor -- daher eigenes Badge.
              Adoption-Lift aus V3 (ZonenKarteV3Glance.tsx:210). */}
          {zone.aquabloom_konfig && (
            <span
              className="badge aquabloom-pill"
              title={`AquaBloom-Pumpe${zone.aquabloom_konfig.aktiv ? '' : ' (ausserhalb der Saison)'}.`}
            >
              AQUABLOOM{zone.aquabloom_konfig.intervall_stunden != null
                ? ` · ${Math.round(zone.aquabloom_konfig.intervall_stunden)}h`
                : ''}
            </span>
          )}
        </div>
        <FeuchteAnzeige wert={zone.aktuelle_feuchte} min={zone.feuchte_schwelle_min} max={zone.feuchte_schwelle_max} />
      </div>

      <div className="karte-details">
        <div className="detail">
          <span className="label">Bodentemperatur</span>
          <span className="wert">{zone.boden_temperatur !== null ? `${zone.boden_temperatur}°C` : '--'}</span>
        </div>
        {zone.licht_intensitaet !== null && (
          <div className="detail">
            <span className="label">Licht</span>
            <span className="wert">{zone.licht_intensitaet.toFixed(0)} lux</span>
          </div>
        )}
        {zone.licht !== null && zone.licht_intensitaet === null && (
          <div className="detail">
            <span className="label">Licht</span>
            <span className="wert">{zone.licht.toFixed(0)}</span>
          </div>
        )}
        {zone.boden_fruchtbarkeit !== null && (
          // T-0242: `boden_fruchtbarkeit` ist FYTA-Salinitaet/Naehrsalz
          // (soil_fertility in mS/cm), NICHT NPK-Index. Backend hat das
          // in api_server.py:_ueberlagere_current_aus_messung klargestellt
          // (Mapping boden_fruchtbarkeit -> salinitaet). Vor Fix zeigte V0
          // "Fruchtbarkeit: 1" (klingt nach "wenig Naehrstoff, duengen!"),
          // korrekt ist "Naehrsalz 1.00 mS/cm" (hoch = Versalzung).
          <div className="detail">
            <span className="label">Nährsalz</span>
            <span className="wert">{zone.boden_fruchtbarkeit.toFixed(2)} mS/cm</span>
          </div>
        )}
        {zone.batterie !== null && (
          <div className="detail">
            <span className="label">Batterie</span>
            <span className="wert">{zone.batterie}%</span>
          </div>
        )}
        <div className="detail">
          <span className="label">Update</span>
          <span className="wert">{zone.letztes_update ? datumZeitFormat(zone.letztes_update) : '--'}</span>
        </div>
        {/* T-0228 Stufe 2: Start-Link fuer Wartungs-Modus. Nur sichtbar
            wenn aktuell keine Wartung laeuft (sonst zeigt der Header-
            Badge "WARTUNG" das Beenden). */}
        {wartungsFensterId === undefined && (
          <div className="detail wartung-start-zelle">
            <button
              type="button"
              className="wartung-start-link"
              title="Heuristik fuer Sensor-Reset / Wiedereinsetzen / Fremdnutzung pausieren"
              onClick={async () => {
                const grund = window.prompt(
                  'Grund fuer Wartung (z. B. "Sensor neu eingeschlemmt"):',
                  '',
                ) ?? ''
                await starteWartungsFenster(zone.zone_id, grund)
                onWartungsAenderung?.()
              }}
            >
              🔧 Wartung
            </button>
          </div>
        )}
      </div>

      <EntscheidungsErklaerung zoneId={zone.zone_id} />

      {/* T-0094: Banner bei unklassifizierten Sensor-Sprung-Events.
          T-0294b: AquaBloom-Option bei AquaBloom-Zonen. */}
      <UnbekanntEventBanner zoneId={zone.zone_id} istAquabloom={!!zone.aquabloom_konfig} />

      {/* T-0066: Dry-Run-Gießempfehlung (was würde jetzt passieren?) */}
      <GiessEmpfehlungPanel zoneId={zone.zone_id} />

      {/* T-0049 + T-0050a: Schwellen-Vorschlag aus 30-Tage-Historie kombiniert mit Pflanzen-Optimum */}
      {schwellenVorschlag && schwellenVorschlag.basis === 'berechnet' &&
        schwellenVorschlag.min_vorschlag !== null && schwellenVorschlag.max_vorschlag !== null &&
        (Math.abs(schwellenVorschlag.min_vorschlag - schwellenVorschlag.min_aktuell) >= 2 ||
         Math.abs(schwellenVorschlag.max_vorschlag - schwellenVorschlag.max_aktuell) >= 2) && (
        <div
          className="schwellen-vorschlag"
          title={
            `Basis: ${schwellenVorschlag.n_messungen} Messungen der letzten ${schwellenVorschlag.fenster_tage} Tage ` +
            `(10./90. Perzentil +/- 5 %-Puffer).` +
            (schwellenVorschlag.quelle === 'optimum_dominiert'
              ? ` Pflanzen-Optimum zieht den Vorschlag zu ${schwellenVorschlag.optimum_min}-${schwellenVorschlag.optimum_max} % hin.`
              : '') +
            ' Rein informativ, nicht automatisch angewandt.'
          }
        >
          💡 Vorschlag {schwellenVorschlag.min_vorschlag.toFixed(0)}/{schwellenVorschlag.max_vorschlag.toFixed(0)} %
          <small> (aktuell {schwellenVorschlag.min_aktuell.toFixed(0)}/{schwellenVorschlag.max_aktuell.toFixed(0)} %{
            schwellenVorschlag.optimum_min !== null && schwellenVorschlag.optimum_max !== null
              ? `, Pflanze ${schwellenVorschlag.optimum_min}-${schwellenVorschlag.optimum_max} %`
              : ''
          })</small>
        </div>
      )}

      <WasserBilanz zoneId={zone.zone_id} />

      {/* T-0208 (18.05.): Multi-Sensor-Diagnose-Liste auch in V0,
          analog zu V1 (ZonenKarteNeu). Rendert nur bei > 1 Sensor
          (Waldblumenhain mit Gardena + 2x FYTA Terra seit T-0179d). */}
      <SensorListeDiagnose sensoren={zone.sensoren} />

      {Object.keys(mlVorhersage).length > 0 && (
        <div className="ml-kurzinfo">
          <span className="label">ML</span>
          {[6, 12, 24].map(h => {
            const v = mlVorhersage[`${h}h`]
            if (!v) return null
            const unterSchwelle = v.feuchte_prognose < zone.feuchte_schwelle_min
            // T-0046: wenn Quantile-Baender da, Spanne im Tooltip zeigen
            const titel = v.q10 != null && v.q90 != null
              ? `${v.q10.toFixed(0)} – ${v.q90.toFixed(0)}% (q10–q90)`
              : undefined
            return (
              <span key={h} className={`ml-wert ${unterSchwelle ? 'unter-schwelle' : ''}`} title={titel}>
                {v.feuchte_prognose.toFixed(0)}%
                <small>{h}h</small>
              </span>
            )
          })}
        </div>
      )}

      {mlVerfuegbar && <MLDriftAmpel zoneId={zone.zone_id} />}
      {mlVerfuegbar && <MLPrognoseVsIstChart zoneId={zone.zone_id} />}

      {/* T-0040: SHAP-Erklaerung — on-demand */}
      {mlVerfuegbar && <MLAttributierung zoneId={zone.zone_id} horizonte={[6, 12, 24]} />}

      {prognose && prognose.bewaesserung_erwartet && (
        <div className="prognose-box">
          Bewaesserung erwartet: {datumZeitFormat(prognose.bewaesserung_erwartet)}
          <br /><small>{prognose.begruendung}</small>
        </div>
      )}
      {prognose && !prognose.bewaesserung_erwartet && (
        <div className="prognose-box ok">
          {prognose.begruendung}
        </div>
      )}

      {chartDaten.length > 1 && (
        <div className="chart-container">
          <div className="zeitraum-toggle">
            {ZEITRAEUME.map((z, i) => (
              <button key={z.label} className={`zeitraum-btn ${i === zeitraumIdx ? 'aktiv' : ''}`} onClick={() => setZeitraumIdx(i)}>
                {z.label}
              </button>
            ))}
          </div>
          <ResponsiveContainer width="100%" height={180}>
            <AreaChart data={chartDaten}>
              <defs>
                <linearGradient id={`grad-${zone.zone_id}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="var(--chart-feuchte)" stopOpacity={0.15} />
                  <stop offset="95%" stopColor="var(--chart-feuchte)" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-gitter)" />
              <XAxis
                dataKey="zeit" type="number" scale="time"
                domain={['dataMin', 'dataMax']}
                ticks={achsenTicks}
                stroke="var(--text-gedaempft)"
                tick={baueXTickRenderer(datenSpanneH)}
                height={36}
              />
              <YAxis domain={[0, 100]} stroke="var(--text-gedaempft)" fontSize={11} />
              <Tooltip
                contentStyle={{
                  background: 'var(--bg-karte)',
                  border: '1px solid var(--rahmen)',
                  borderRadius: 10,
                  boxShadow: 'var(--karte-schatten-hover)',
                }}
                labelStyle={{ color: 'var(--text-sekundaer)' }}
                labelFormatter={(ts) => new Date(Number(ts)).toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}
                formatter={(value, name) => {
                  // Quantile-Band kommt als [q10, q90]-Array; Default-Tooltip
                  // zeigt ein hässliches "[12, 34]". Nutzer sehen statt dessen
                  // die Spanne + Erklärung des 80 %-Konfidenzintervalls.
                  if (Array.isArray(value) && value.length === 2) {
                    const [q10, q90] = value as [number, number]
                    return [`${q10.toFixed(0)}% – ${q90.toFixed(0)}% (q10–q90, 80 % Konfidenz)`, 'Unsicherheit']
                  }
                  if (typeof value === 'number') {
                    return [`${value.toFixed(0)}%`, name]
                  }
                  return [value, name]
                }}
              />
              {/* T-0407: ml_ausschluss_fenster als schmale Annotations-
                  Schiene oben statt vollflaechiger Einfaerbung. Geteilt
                  mit V4-Inspect (feuchte_chart_helpers). */}
              {chartDaten.length > 0 && baueAusschlussMarkierungen(
                zone.ausschluss_fenster,
                chartDaten[0].zeit,
                chartDaten[chartDaten.length - 1].zeit,
              )}
              {/* T-0410: in Zonen mit Gardena UND FYTA gelten diese
                  Schwellen nur fuer den Gardena-Sensor -- kennzeichnen. */}
              <ReferenceLine y={zone.feuchte_schwelle_min} stroke="var(--farbe-gefahr)" strokeDasharray="5 5" label={{ value: `Min${schwellenSuffix}`, fill: 'var(--farbe-gefahr)', fontSize: 10 }} />
              <ReferenceLine y={zone.feuchte_schwelle_max} stroke="var(--farbe-gesund)" strokeDasharray="5 5" label={{ value: `Max${schwellenSuffix}`, fill: 'var(--farbe-gesund)', fontSize: 10 }} />
              {/* T-0211c: Pro-Sensor-Linien (Multi-Sensor) bzw. Area
                  mit Gradient (Single-Sensor). Vorher wurde fuer
                  Multi-Sensor-Zonen eine Misch-Linie durch alle
                  Sensoren chronologisch gezeichnet -- wirkte wie
                  wilde Feuchte-Spruenge. */}
              {sensorMeta.length === 1 ? (
                <Area
                  type="monotone"
                  dataKey={sensorMeta[0].dataKey}
                  stroke={sensorMeta[0].farbe}
                  fill={`url(#grad-${zone.zone_id})`}
                  strokeWidth={2}
                  name={sensorMeta[0].name}
                  connectNulls
                  isAnimationActive={false}
                />
              ) : (
                sensorMeta.map(s => (
                  <Line
                    key={s.geraet_id}
                    type="monotone"
                    dataKey={s.dataKey}
                    stroke={s.farbe}
                    strokeWidth={1.5}
                    // T-0221-Folge: keine Punkt-Dots -- beim dichten
                    // Gardena-Sensor (~10-min-Takt) verschmelzen sie zu
                    // einem dicken Band, der Multi-Sensor-Chart wird
                    // unlesbar (User-Feedback).
                    dot={false}
                    connectNulls
                    name={s.name}
                    isAnimationActive={false}
                  />
                ))
              )}
              {hatQuantileBand && (
                <Area
                  type="monotone"
                  dataKey="band"
                  stroke="none"
                  fill="var(--chart-ml)"
                  fillOpacity={0.15}
                  name="Unsicherheit q10-q90"
                  connectNulls
                  isAnimationActive={false}
                  activeDot={false}
                />
              )}
              {Object.keys(mlVorhersage).length > 0 && (
                <Line type="monotone" dataKey="prognose" stroke="var(--chart-ml)" strokeWidth={2} strokeDasharray="6 3" dot={{ fill: '#7c5cbf', r: 3 }} name="ML-Prognose" connectNulls />
              )}
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}

      <ManuellesGiessen
        zoneId={zone.zone_id}
        hatVentilKanal={zone.ventil_kanal != null}
        // T-0170: Pre-Soak-Defaults aus YAML hierhin durchreichen — vorher
        // landeten sie nie an der ManuellesGiessen-Komponente.
        preSoakDefaultMin={zone.pre_soak_min ?? null}
        preSoakPauseMin={zone.pre_soak_pause_min ?? null}
        // T-0169: Logging-Einheit + ml-Optionen pro Zone.
        loggingEinheit={zone.logging_einheit ?? 'sekunden'}
        loggingOptionenMl={zone.logging_optionen_ml}
        // T-0279: Empfohlene Hauptdauer fuer 1-Klick-Uebernehmen.
        empfDauerSec={dauerHauptSekunden(empfehlung)}
      />
    </div>
  )
}
