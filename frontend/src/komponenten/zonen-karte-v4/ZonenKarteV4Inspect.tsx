/* =========================================================
   ZonenKarteV3Inspect -- Schicht 3 der Zonen-Karte v3.
   Collapsible Detail-Section. Jede Sub-Komponente rendert nur wenn
   sie echte Daten hat:
     1. Feuchte-Chart   (Toggle 24h/48h/7d/30d, ML-Overlay falls verfuegbar)
     2. Sub-Sensor-Liste (nur Multi-Sensor)
     3. FYTA-Optimum-KPIs (nur FYTA-Zone mit Optima)
     4. Warum-Panel       (nur Kanal-Scope)
     5. ML SHAP 24h       (nur ML, top_features wird via Prop propagiert)
     6. ML-Drift-Ampel    (nur ML)
     7. Wasser-Bilanz     (Backend liefert flaeche_m2 mit, Komponente
                           rendert sonst sauberen "indikativ"-Hinweis)
   ========================================================= */

import { useEffect, useState, type ReactNode } from 'react'
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ReferenceLine, ReferenceArea, ResponsiveContainer, Line,
} from 'recharts'
import { holeMesswerte } from '../../api'
import { istAbbruch } from '../../hilfsfunktionen'
import type { Zone, Messwert, MLVorhersage, SnapshotFenster, GiessEmpfehlung } from '../../typen'
import { SensorListeDiagnose } from '../SensorListeDiagnose'
import { FytaKpiBlock } from '../FytaKpiBlock'
import { MLAttributierungNeu } from '../MLAttributierungNeu'
import { MLDriftAmpel } from '../MLDriftAmpel'
import { MLPrognoseVsIstChart } from '../MLPrognoseVsIstChart'
import { WasserBilanz } from '../WasserBilanz'
import {
  ausschlussFarbe,
  ausschlussLabel,
  baueAusschlussMarkierungen,
  baueChartDatenProSensor,
  baueSensorLinienMeta,
  baueSegmentLinien,
  baueXTickRenderer,
  findeAusschluss,
  fremdSkalenSensoren,
  hatGemischteQuellen,
} from '../feuchte_chart_helpers'

interface Props {
  zone: Zone
  /** Messwerte pro Zeitfenster (24h/48h/7d/30d), kommt aus dem Bulk-
   *  Snapshot. Toggle wechselt clientseitig ohne weiteren Fetch. */
  messwerte: Partial<Record<SnapshotFenster, Messwert[]>>
  /** ML-Vorhersage je Horizont. Wenn aus Snapshot mit `ml_details=true`
   *  geladen, enthaelt jeder Eintrag bereits `top_features` -- dann
   *  zeigt MLAttributierungNeu die SHAP-Werte ohne Lazy-Fetch. */
  mlVorhersage?: Record<string, MLVorhersage>
  mlVerfuegbar: boolean
  /** T-0221/Q15: Empfehlung -- liefert `welkepunkt_wert` (Plateau-Modell
   *  pro Zone, T-0197). Die Chart-Welkepunkt-Linie nutzt das statt der
   *  globalen Konfig-Konstante `feuchte_kritisch`. */
  empfehlung?: GiessEmpfehlung | null
}

const ZEITRAUM_LISTE: SnapshotFenster[] = ['24h', '48h', '7d', '30d']

// T-0298: Fenster -> Stunden fuer den Lazy-Fetch. Der Bulk-Snapshot liefert
// nur 24h/48h (leicht halten); 7d/30d werden bei Bedarf pro Karte nachgeladen.
const FENSTER_STUNDEN: Record<SnapshotFenster, number> = {
  '24h': 24, '48h': 48, '7d': 24 * 7, '30d': 24 * 30,
}

// T-0221-Folge: Violett-Palette fuer FYTA-Sensor-Linien im V3-Chart.
// Klar gegen Gardena-Blau (--chart-feuchte) abgesetzt und konsistent
// mit den FYTA-Sensor-Pills (--quelle-fyta). Pro FYTA-Sensor ein Ton.
const FYTA_LINIEN_VIOLETT = ['#7c3aed', '#c026d3', '#9333ea']

/** T-0410: Sensor-Meta MIT der V4-Violett-Ueberschreibung fuer FYTA.
 *
 *  Single Source of Truth fuer die Linien-Farben in dieser Komponente --
 *  Chart UND Legende MUESSEN hierdurch gehen. Vorher lag die
 *  Ueberschreibung inline im Chart, die (neue) Legende nahm die rohen
 *  Helper-Farben -> Legenden-Swatch cyan/teal, Linie violett/magenta.
 *  Klassischer Werte-Konsistenz-Bug: dieselbe Information aus zwei
 *  Quellen gerendert. */
function baueSensorMetaV4(
  messwerte: Messwert[],
  zone: Zone,
): ReturnType<typeof baueSensorLinienMeta> {
  let fytaFarbIdx = 0
  return baueSensorLinienMeta(messwerte, zone.sensoren, zone.sensor_namen).map(s =>
    s.quelle === 'fyta'
      ? { ...s, farbe: FYTA_LINIEN_VIOLETT[fytaFarbIdx++ % FYTA_LINIEN_VIOLETT.length] }
      : s,
  )
}

/** Kuerzt einen Sensor-Namen fuer den Chart-Tooltip: der Klammer-Zusatz
 *  (Hardware-Typ etc.) faellt weg, damit die Wert-Spalte Platz hat --
 *  "Waldblumen A (FYTA Terra 11 cm, ...)" -> "Waldblumen A". */
function kurzSensorName(name: string): string {
  const i = name.indexOf(' (')
  return i > 0 ? name.slice(0, i) : name
}

export default function ZonenKarteV3Inspect({
  zone, messwerte, mlVerfuegbar, mlVorhersage, empfehlung,
}: Props) {
  const [zeitraum, setZeitraum] = useState<SnapshotFenster>('48h')

  // Q15: Welkepunkt aus dem Plateau-Modell pro Zone, sonst Fallback auf
  // die Konfig-Konstante feuchte_kritisch.
  const welkepunkt = empfehlung?.welkepunkt_wert ?? zone.feuchte_kritisch ?? null

  // T-0298: 7d/30d kommen NICHT aus dem Bulk-Snapshot (der liefert nur
  // 24h/48h). Frueher hing der Toggle dann dauerhaft auf "Lade...", weil
  // es keinen Fetch-Fallback gab. Jetzt: fehlendes Fenster pro Karte lazy
  // nachladen (moderate Volumina, ~700-5000 Punkte -> sub-Sekunde).
  const [lokalGeholt, setLokalGeholt] = useState<
    Partial<Record<SnapshotFenster, Messwert[]>>
  >({})
  const [ladeFehler, setLadeFehler] = useState<string | null>(null)
  const ausSnapshot = messwerte[zeitraum]

  // Der Fehler gehoert zu GENAU einem (Zone, Fenster)-Paar. Frueher wurde er
  // synchron im Effect zurueckgesetzt; das erzwang einen Extra-Render
  // (`react-hooks/set-state-in-effect`). Ihn stattdessen erst im `.then` zu
  // loeschen waere falsch: waehrend des Nachladens stuende sonst der Fehler
  // des VORIGEN Fensters unter dem Namen des neuen. Deshalb der Reset im
  // Render beim Wechsel -- React's dokumentiertes Muster dafuer.
  const ladeSchluessel = `${zone.zone_id}|${zeitraum}`
  const [fehlerFuer, setFehlerFuer] = useState(ladeSchluessel)
  if (fehlerFuer !== ladeSchluessel) {
    setFehlerFuer(ladeSchluessel)
    setLadeFehler(null)
  }

  useEffect(() => {
    if (ausSnapshot !== undefined) return          // Snapshot liefert das Fenster
    if (lokalGeholt[zeitraum] !== undefined) return  // schon nachgeladen
    const ctrl = new AbortController()
    holeMesswerte(zone.zone_id, FENSTER_STUNDEN[zeitraum], ctrl.signal)
      .then(d => setLokalGeholt(prev => ({ ...prev, [zeitraum]: d })))
      .catch(e => {
        if (!istAbbruch(e)) {
          setLadeFehler(e instanceof Error ? e.message : 'Fehler')
        }
      })
    return () => ctrl.abort()
  }, [zone.zone_id, zeitraum, ausSnapshot, lokalGeholt])

  const aktuelleMesswerte = ausSnapshot ?? lokalGeholt[zeitraum] ?? []
  const hatMesswerte = aktuelleMesswerte.length > 1
  // Wurde das Fenster schon (erfolgreich) geladen? Dann kein "Lade..." mehr.
  const fensterGeladen =
    ausSnapshot !== undefined || lokalGeholt[zeitraum] !== undefined
  const hatSubSensoren = (zone.sensoren ?? []).length > 1
  const hatFytaOptima = zone.quelle === 'fyta'
    && zone.optima != null && Object.keys(zone.optima).length > 0
  // Phase 3 Perf (T-0220): das frueher hier durchgereichte `alleZonen`
  // war ungenutzt (`void alleZonen`) und brach als instabile Array-Prop
  // den React.memo der Karte -- entfernt.
  const hatML = mlVerfuegbar
    && mlVorhersage != null && Object.keys(mlVorhersage).length > 0

  return (
    <div className="zk-inspect">
      {/* T-0221-Folge: das UnbekanntEventBanner ist von hier nach
          ZonenKarteV3 (always-visible auf der Karte) gewandert --
          Paritaet zu V0, der "Was war das?"-Dialog soll ohne
          DETAILS-Aufklappen sichtbar sein. */}
      <Row lbl="FEUCHTE-VERLAUF">
        <div className="zk-zeitraum-toggle">
          {ZEITRAUM_LISTE.map(z => (
            <button
              key={z}
              type="button"
              className={`zk-zeitraum-btn ${zeitraum === z ? 'zk-zeitraum-btn--aktiv' : ''}`}
              onClick={() => setZeitraum(z)}
              title={`Verlauf der letzten ${z}`}
            >
              {z}
            </button>
          ))}
        </div>
        {hatMesswerte ? (
          <>
            <FeuchteChartV3
              zone={zone}
              messwerte={aktuelleMesswerte}
              mlVorhersage={mlVorhersage}
              welkepunkt={welkepunkt}
            />
            <ChartLegende
              zone={zone}
              messwerte={aktuelleMesswerte}
              welkepunkt={welkepunkt}
              optimumBandQuelle={
                zone.optima?.feuchte?.min_good != null
                  ? 'FYTA-Optimum'
                  : zone.optimum_feuchte_min != null
                    ? 'Pflanzen-Optimum (Config)'
                    : 'User-Korridor'
              }
              hatPrognose={mlVorhersage != null && Object.keys(mlVorhersage).length > 0}
            />
          </>
        ) : ladeFehler ? (
          <div className="zk-inspect__lbl">
            Messwerte fuer {zeitraum} nicht ladbar: {ladeFehler}
          </div>
        ) : fensterGeladen ? (
          <div className="zk-inspect__lbl">Keine Messwerte fuer {zeitraum}.</div>
        ) : (
          <div className="zk-inspect__lbl">Lade Messwerte fuer {zeitraum}...</div>
        )}
      </Row>

      {hatSubSensoren && (
        <Row lbl="SUB-SENSOREN">
          <SensorListeDiagnose sensoren={zone.sensoren!} />
        </Row>
      )}

      {hatFytaOptima && (
        <details className="zk-inspect__details">
          <summary className="zk-inspect__lbl">FYTA-Optimum live (Achsen-Details)</summary>
          <FytaKpiBlock optima={zone.optima} kompakt />
        </details>
      )}

      {hatML && (
        <Row lbl="ML">
          {/* T-0201-Folge: Drift-Pills (6h/12h/24h) sofort sichtbar --
              das ist die wichtigste Gesundheits-Info. EIN gemeinsamer
              Toggle fuer beide Detail-Sektionen (Top-5 Features +
              Prognose-vs-Ist-Chart), statt zwei separate Toggles plus
              jeweils nochmal interner Toggle der V1-Komponente. */}
          <MLDriftAmpel zoneId={zone.zone_id} />
          <details className="zk-inspect__details">
            <summary className="zk-inspect__lbl">Mehr ML-Details (Top-5 Features + Prognose vs. Ist)</summary>
            <MLAttributierungNeu
              zoneId={zone.zone_id}
              horizonte={[24]}
              vorhersagenPropagiert={mlVorhersage}
              versteckeToggle
            />
            <MLPrognoseVsIstChart zoneId={zone.zone_id} versteckeToggle />
          </details>
        </Row>
      )}

      {/* WasserBilanz hat eigenen "Wasser-Bilanz"-Header -- kein
          zusaetzliches Row-Label (waere Doppelung). */}
      <WasserBilanz zoneId={zone.zone_id} />
    </div>
  )
}

function Row({ lbl, children }: { lbl: string; children: ReactNode }) {
  return (
    <div className="zk-inspect__row">
      <span className="zk-inspect__lbl">{lbl}</span>
      {children}
    </div>
  )
}

/** Erzeugt ~6 gleichmaessig verteilte Tick-Positionen entlang der Zeitachse,
 *  damit X-Achsen-Labels nicht ueberlappen. Anker-Positionen werden zu
 *  ganzen Stunden gerundet, was natuerlicher aussieht als willkuerliche
 *  Zeitstempel aus den Messpunkten. */
function berechneTicks(daten: Array<{ zeit: number }>): number[] {
  if (daten.length < 2) return daten.map(p => p.zeit)
  const start = daten[0].zeit
  const ende = daten[daten.length - 1].zeit
  const spanne = ende - start
  const n = 6
  const ticks: number[] = []
  for (let i = 0; i < n; i++) {
    const ts = start + (spanne * i) / (n - 1)
    // Auf naechste volle Stunde runden.
    const d = new Date(ts)
    d.setMinutes(0, 0, 0)
    ticks.push(d.getTime())
  }
  return ticks
}

/* Feuchte-Verlauf mit ML-Prognose-Overlay (q10-q90-Band + gestrichelte
   Prognose-Linie). Prognose haengt sich an die Vergangenheit dran --
   egal welcher Zeitraum-Toggle aktiv ist, die 24h-Zukunft kommt
   ans Ende. Bei 30d ist die Prognose-Spannweite (24h) optisch klein,
   aber semantisch konsistent. */
// T-0221 Stage D: exportiert -- der Detail-Drawer rendert denselben
// Chart mit dem 30-Tage-Fenster.
export function FeuchteChartV3({
  zone, messwerte, mlVorhersage, welkepunkt,
}: {
  zone: Zone
  messwerte: Messwert[]
  mlVorhersage?: Record<string, MLVorhersage>
  welkepunkt: number | null
}) {
  interface Punkt {
    zeit: number
    feuchte?: number  // Backward-Compat (Fallback / Tooltip-Label)
    prognose?: number
    band?: [number, number]
  }
  // T-0211c: Pro-Sensor-Linien statt einer Misch-Linie. Bei Multi-
  // Sensor-Zonen (waldblumenhain) verbindet Recharts sonst Werte
  // verschiedener Sensoren chronologisch -> wirkt wie wilde Spruenge.
  // Bei Single-Sensor-Zonen ist die Logik identisch zur alten (eine
  // Linie, gleiche Werte).
  // T-0221-Folge: FYTA-Linien in Violett-Toenen (siehe baueSensorMetaV4)
  // -- cyan/teal aus der geteilten Palette liegt zu nah an Gardena-Blau.
  const sensorMeta = baueSensorMetaV4(messwerte, zone)
  // Basis-Punkte: ein Eintrag pro Messung (Zeitstempel + Roh-Feuchte
  // als Fallback fuer Tooltips / Single-Sensor). Wir kopieren die
  // Reihenfolge 1:1, damit `baueChartDatenProSensor` ueber den Index
  // passend zuordnen kann.
  const basis: Punkt[] = messwerte
    .filter(m => m.boden_feuchte != null)
    .map(m => ({
      zeit: new Date(m.zeitstempel).getTime(),
      feuchte: m.boden_feuchte as number,
    }))
  const messwerteGefiltert = messwerte.filter(m => m.boden_feuchte != null)
  const daten = baueChartDatenProSensor(
    basis, messwerteGefiltert, sensorMeta, zone.ausschluss_fenster,
  ) as (Punkt & Record<string, number | null>)[]
  // T-0573: Segmente >= 1 entstehen nur an den Grenzen eines
  // `sensor_kalibrierung`-Fensters. Ohne Fenster ist die Liste leer und
  // der Chart rendert exakt wie vorher.
  const segmentLinien = baueSegmentLinien(sensorMeta, daten)

  // Zeitstempel der letzten echten Messung. Alles im Chart danach ist
  // ML-Prognose (Zukunft) -- der Tooltip blendet dort die Ist-Sensoren
  // aus, weil es an Zukunfts-Punkten keine echten Messwerte gibt und
  // der "letzter bekannter Wert"-Fallback sonst veraltete Ist-Werte
  // neben der Prognose zeigen wuerde (User-Feedback).
  const letzteMesszeit = daten.length > 0 ? daten[daten.length - 1].zeit : null

  // ML-Prognose-Punkte ans Ende anhaengen (6h/12h/24h).
  if (mlVorhersage && daten.length > 0) {
    const letzterZeit = daten[daten.length - 1].zeit
    const letzteFeuchte = daten[daten.length - 1].feuchte
    // T-0573 (Isomorphie zum Bar-Suffix): eine Prognose, die als Zahl
    // unterdrueckt wird, darf auch nicht als Kurve dastehen -- sonst
    // verschwindet die 66 aus dem Header und bleibt im Chart.
    const g24 = mlVorhersage['24h']
    if (letzteFeuchte != null) {
      daten[daten.length - 1].prognose = letzteFeuchte
      if (g24?.gueltig !== false && g24?.q10 != null && g24?.q90 != null) {
        daten[daten.length - 1].band = [letzteFeuchte, letzteFeuchte]
      }
    }
    for (const h of [6, 12, 24]) {
      const v = mlVorhersage[`${h}h`]
      if (!v || v.gueltig === false) continue
      const punkt: Punkt & Record<string, number | null> = {
        zeit: letzterZeit + h * 3600_000,
        prognose: v.feuchte_prognose,
      }
      if (v.q10 != null && v.q90 != null) punkt.band = [v.q10, v.q90]
      daten.push(punkt)
    }
  }

  if (daten.length < 2) return <div className="zk-inspect__lbl">Zu wenig Messwerte</div>

  const gradId = `grad-v3-${zone.zone_id}`
  const hatBand = daten.some(p => p.band !== undefined)
  const hatPrognose = daten.some(p => p.prognose !== undefined)
  // Gruenes "Pflanzen-Optimum"-Band: FYTA-Optimum hat Vorrang, sonst
  // Fallback auf zone.optimum_feuchte_min/max (aus default.yaml,
  // typischerweise fuer Gardena-Zonen) oder als letzten Fallback auf
  // den User-Korridor (feuchte_schwelle_min/max). So sehen Gardena-
  // und FYTA-Karten ein konsistentes gruenes Band.
  const optimumBandMin = zone.optima?.feuchte?.min_good
    ?? zone.optimum_feuchte_min
    ?? zone.feuchte_schwelle_min
  const optimumBandMax = zone.optima?.feuchte?.max_good
    ?? zone.optimum_feuchte_max
    ?? zone.feuchte_schwelle_max

  // T-0221-Folge: eigener Tooltip. Der Recharts-Default zeigt pro
  // Hover-Punkt nur den EINEN Sensor, der dort einen Messwert hat --
  // die anderen Sensor-Spalten sind an dem Zeitpunkt null. Dadurch
  // springt der Tooltip zwischen den Sensoren. Hier zeigen wir pro
  // Sensor den letzten bekannten Wert <= Hover-Zeitpunkt, also immer
  // alle Sensoren stabil nebeneinander.
  const tooltipInhalt = (props: { active?: boolean; label?: number | string }): ReactNode => {
    if (!props.active || props.label == null) return null
    const labelMs = Number(props.label)
    // Im Prognose-Bereich (nach der letzten echten Messung) gibt es
    // keine Ist-Werte -- nur die ML-Prognose zeigen, sonst stuenden
    // veraltete Sensor-Werte neben dem Zukunfts-Punkt.
    const istZukunft = letzteMesszeit != null && labelMs > letzteMesszeit
    const zeilen: { name: string; farbe: string; wert: number }[] = []
    if (!istZukunft) {
      for (const s of sensorMeta) {
        let wert: number | null = null
        for (const p of daten) {
          if (p.zeit > labelMs) break
          const v = p[s.dataKey]
          if (v != null) wert = v
        }
        if (wert != null) zeilen.push({ name: s.name, farbe: s.farbe, wert })
      }
    }
    const punkt = daten.find(p => p.zeit === labelMs)
    // T-0407: laeuft an diesem Zeitpunkt ein ausschluss_fenster? Dann
    // den `grund` zeigen -- die Schiene oben sagt DASS eines laeuft,
    // der Tooltip sagt WARUM.
    const fenster = istZukunft ? null : findeAusschluss(labelMs, zone.ausschluss_fenster)
    return (
      <div className="zk-chart-tt">
        <div className="zk-chart-tt__zeit">
          {new Date(labelMs).toLocaleString('de-DE', {
            day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
          })}
        </div>
        {zeilen.map(z => (
          <div key={z.name} className="zk-chart-tt__zeile">
            <span className="zk-chart-tt__dot" style={{ background: z.farbe }} />
            <span className="zk-chart-tt__nm">{kurzSensorName(z.name)}</span>
            <span className="zk-chart-tt__wert">{Math.round(z.wert)} %</span>
          </div>
        ))}
        {punkt?.prognose != null && (
          <div className="zk-chart-tt__zeile">
            <span className="zk-chart-tt__dot" style={{ background: 'var(--chart-ml)' }} />
            <span className="zk-chart-tt__nm">ML-Prognose</span>
            <span className="zk-chart-tt__wert">{Math.round(punkt.prognose)} %</span>
          </div>
        )}
        {istZukunft && (
          <div className="zk-chart-tt__fuss">Zukunft -- nur ML-Prognose, keine Messung</div>
        )}
        {fenster && (
          <div
            className="zk-chart-tt__fuss zk-chart-tt__fuss--grund"
            style={{ color: ausschlussFarbe(fenster) }}
            title={fenster.grund}
          >
            {ausschlussLabel(fenster)}: {fenster.grund}
          </div>
        )}
      </div>
    )
  }

  // Chart-Spannweite in Stunden -- entscheidet ueber Tick-Format.
  // 2-zeiliger X-Tick-Renderer ist im `feuchte_chart_helpers` als
  // Factory zentralisiert (geteilt mit V0/V1/V2, T-0221-Folge).
  const spanne = (daten[daten.length - 1].zeit - daten[0].zeit) / 3600_000
  const renderXTick = baueXTickRenderer(spanne)

  return (
    <ResponsiveContainer width="100%" height={140}>
      <AreaChart data={daten}>
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor="var(--chart-feuchte)" stopOpacity={0.15} />
            <stop offset="95%" stopColor="var(--chart-feuchte)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-gitter)" />
        <XAxis
          dataKey="zeit" type="number" scale="time"
          domain={['dataMin', 'dataMax']}
          stroke="var(--text-gedaempft)"
          // Tick-Positionen explizit setzen, damit Recharts nicht versucht,
          // bei 100+ Datenpunkten Ticks pro Messung zu rendern.
          ticks={berechneTicks(daten)}
          interval={0}
          minTickGap={45}
          // T-0221-Folge: 2-zeiliger Custom-Tick (Datum + Uhrzeit
          // untereinander) -- braucht ein paar Pixel mehr Hoehe als
          // der Recharts-Default (~30 px), bleibt aber innerhalb der
          // Chart-Container-Hoehe (140 px).
          tick={renderXTick}
          height={36}
        />
        <YAxis domain={[0, 100]} stroke="var(--text-gedaempft)" fontSize={10} />
        {/* T-0221-Folge: eigener Tooltip -> zeigt alle Sensoren stabil
            (letzter Wert pro Sensor), statt zwischen ihnen zu springen. */}
        <Tooltip content={tooltipInhalt} />
        {optimumBandMin != null && optimumBandMax != null && (
          <ReferenceArea y1={optimumBandMin} y2={optimumBandMax} fill="var(--farbe-gesund)" fillOpacity={0.07} ifOverflow="visible" />
        )}
        {/* T-0407: ml_ausschluss_fenster als schmale Annotations-Schiene
            oben (vorher vollflaechige Einfaerbung -> lange Fenster machten
            den Chart unlesbar). Der `grund`-Text steht im Tooltip. */}
        {baueAusschlussMarkierungen(
          zone.ausschluss_fenster, daten[0].zeit, daten[daten.length - 1].zeit,
        )}
        {welkepunkt != null && (
          <ReferenceLine
            y={welkepunkt}
            stroke="var(--farbe-gefahr)"
            strokeDasharray="2 4"
            strokeWidth={1.5}
            label={{ value: 'Welke', fill: 'var(--farbe-gefahr)', fontSize: 9, position: 'insideBottomRight' }}
          />
        )}
        <ReferenceLine y={zone.feuchte_schwelle_min} stroke="var(--farbe-gefahr)" strokeDasharray="5 5" />
        {zone.feuchte_schwelle_max != null && (
          <ReferenceLine y={zone.feuchte_schwelle_max} stroke="var(--farbe-gesund)" strokeDasharray="5 5" />
        )}
        {/* T-0211c: Pro-Sensor-Linien. Single-Sensor-Zone behaelt den
            Gradient-Fill (Area). Multi-Sensor (z.B. waldblumenhain mit
            1× Gardena + 2× FYTA) bekommt pro Sensor eine eigene
            Linie -- vorher wurden alle Sensoren chronologisch durch
            EINE Linie verbunden, was wie wilde Feuchte-Spruenge wirkte
            (Cadence-Mismatch Gardena 10 min vs FYTA 3-4 h). */}
        {sensorMeta.length === 1 ? (
          <Area
            type="monotone"
            dataKey={sensorMeta[0].dataKey}
            stroke={sensorMeta[0].farbe}
            fill={`url(#${gradId})`}
            strokeWidth={2}
            connectNulls
            name={sensorMeta[0].name}
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
              // T-0221-Folge: keine Punkt-Dots. Beim dichten Gardena-
              // Sensor (~10-min-Takt) verschmolzen die Dots zu einem
              // dicken Band -- mit 3 Sensoren wurde der Chart unlesbar
              // (User-Feedback). Der Hover-activeDot bleibt.
              dot={false}
              connectNulls
              name={s.name}
              isAnimationActive={false}
            />
          ))
        )}
        {/* T-0573: Fortsetzungs-Segmente der Sensor-Linien. Gleiche Farbe
            wie die Basis-Linie -- die Aussage ist die LUECKE an der
            Fenstergrenze, nicht eine zweite Messgroesse. */}
        {segmentLinien.map(s => (
          <Line
            key={s.dataKey}
            type="monotone"
            dataKey={s.dataKey}
            stroke={s.farbe}
            strokeWidth={sensorMeta.length === 1 ? 2 : 1.5}
            dot={false}
            connectNulls
            name={s.name}
            legendType="none"
            isAnimationActive={false}
          />
        ))}
        {hatBand && (
          <Area type="monotone" dataKey="band" stroke="none" fill="var(--chart-ml)" fillOpacity={0.15} name="ML-Unsicherheit q10-q90" connectNulls isAnimationActive={false} activeDot={false} />
        )}
        {hatPrognose && (
          <Line type="monotone" dataKey="prognose" stroke="var(--chart-ml)" strokeWidth={2} strokeDasharray="6 3" dot={{ fill: 'var(--chart-ml)', r: 3 }} name="ML-Prognose" connectNulls />
        )}
      </AreaChart>
    </ResponsiveContainer>
  )
}

/** Kompakte Legende unter dem Feuchte-Verlauf-Chart. Erklaert die
 *  Linien und Baender ohne Recharts' eingebaute Legend (die schluckt
 *  Vertikalraum und mischt Reihen-Namen ein). */
function ChartLegende({ zone, messwerte, welkepunkt, optimumBandQuelle, hatPrognose }: {
  zone: Zone
  messwerte: Messwert[]
  welkepunkt: number | null
  optimumBandQuelle: string
  hatPrognose: boolean
}) {
  const optMin = zone.optima?.feuchte?.min_good ?? zone.optimum_feuchte_min ?? zone.feuchte_schwelle_min
  const optMax = zone.optima?.feuchte?.max_good ?? zone.optimum_feuchte_max ?? zone.feuchte_schwelle_max
  // T-0410: in Zonen mit Gardena UND FYTA sind die Schwellen-Linien
  // mehrdeutig -- sie gelten nur fuer den Gardena-Sensor. Kennzeichnen
  // statt so tun, als gaelten sie fuer alle Linien.
  // MUSS dieselbe Funktion wie der Chart nutzen -- sonst zeigt der
  // Legenden-Swatch eine andere Farbe als die Linie (T-0410).
  const sensorMeta = baueSensorMetaV4(messwerte, zone)
  const gemischt = hatGemischteQuellen(sensorMeta)
  const fremde = fremdSkalenSensoren(sensorMeta)
  const skalenHinweis = gemischt
    ? ` Gilt nur fuer den Gardena-Sensor -- ${fremde.map(f => f.name).join(', ')} `
      + 'misst auf einer anderen, nicht umrechenbaren Skala (T-0410).'
    : ''
  const schwellenSuffix = gemischt ? ' (Gardena)' : ''
  return (
    <div className="zk-chart-legende">
      <span className="zk-chart-legende__eintrag" title="Aktueller Sensor-Messwert.">
        <span className="zk-chart-legende__swatch zk-chart-legende__swatch--feuchte" />
        Ist
      </span>
      {optMin != null && optMax != null && (
        <span className="zk-chart-legende__eintrag" title={`Optimum-Band ${Math.round(optMin)}-${Math.round(optMax)}% (Quelle: ${optimumBandQuelle}).`}>
          <span className="zk-chart-legende__swatch zk-chart-legende__swatch--optimum" />
          Opt {Math.round(optMin)}-{Math.round(optMax)}
        </span>
      )}
      {welkepunkt != null && (
        <span className="zk-chart-legende__eintrag" title={`Welkepunkt aus dem Plateau-Modell pro Zone (T-0197): darunter welkt die Pflanze sichtbar.${skalenHinweis}`}>
          <span className="zk-chart-legende__swatch zk-chart-legende__swatch--welke" />
          Welke {Math.round(welkepunkt)}{schwellenSuffix}
        </span>
      )}
      <span className="zk-chart-legende__eintrag" title={`Wenn unterschritten: giessen.${skalenHinweis}`}>
        <span className="zk-chart-legende__swatch zk-chart-legende__swatch--min" />
        Min {zone.feuchte_schwelle_min}{schwellenSuffix}
      </span>
      <span className="zk-chart-legende__eintrag" title={`Wenn ueberschritten: zu nass.${skalenHinweis}`}>
        <span className="zk-chart-legende__swatch zk-chart-legende__swatch--max" />
        Max {zone.feuchte_schwelle_max}{schwellenSuffix}
      </span>
      {/* T-0410: eigener Eintrag pro Fremd-Skalen-Sensor. Macht sichtbar,
          dass die Linien oben fuer diese Sensoren NICHT gelten -- vorher
          suggerierte die Legende mit einem einzigen "Ist"-Swatch, alle
          Linien laegen auf derselben Skala. */}
      {gemischt && fremde.map(f => (
        <span
          key={f.geraet_id}
          className="zk-chart-legende__eintrag zk-chart-legende__eintrag--fremdskala"
          title={`${f.name}: andere Messskala als der Gardena-Sensor. Die Schwellen-Linien (Min/Max/Welke) sind gegen Gardena kalibriert und gelten fuer diesen Sensor NICHT. Eine Umrechnung existiert nicht (Korrelation ~0, kein gueltiges Mapping) -- Werte nur im Verlauf lesen, nicht gegen die Linien.`}
        >
          <span className="zk-chart-legende__swatch" style={{ background: f.farbe }} />
          {kurzSensorName(f.name)} · andere Skala
        </span>
      ))}
      {hatPrognose && (
        <span className="zk-chart-legende__eintrag" title="ML-Prognose mit 80%-Konfidenz-Band fuer die naechsten 24h.">
          <span className="zk-chart-legende__swatch zk-chart-legende__swatch--ml" />
          ML
        </span>
      )}
    </div>
  )
}
