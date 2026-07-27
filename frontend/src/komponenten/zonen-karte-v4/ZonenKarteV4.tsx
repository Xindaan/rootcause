/* =========================================================
   ZonenKarteV4 -- Wrapper der Drei-Schichten-Karte (v3).
   Klon von ZonenKarteV2 als gefahrloser Test-Reiter (siehe TASK T-0220).
   Datenholung (Messwerte, ML, GiessEmpfehlung) wie ZonenKarteNeu;
   Expanded-Zustand pro Zone-ID in LocalStorage (`zk-v4-expanded-{id}`).
   Aktion-Handler vorerst nur console.warn -- konkrete Implementierung
   in Folge-Task.
   ========================================================= */

import { lazy, memo, Suspense, useEffect, useState, type MouseEvent } from 'react'
import type {
  Zone, MLVorhersage, SchwellenVorschlag, GiessEmpfehlung, Messwert,
  SnapshotFenster,
} from '../../typen'
import { holeMesswerte, holeMLVorhersage, holeGiessEmpfehlung } from '../../api'
import { berechneTrendPpProH, dauerHauptSekunden, istAbbruch } from '../../hilfsfunktionen'
import { ZonenKarteV4Glance } from './ZonenKarteV4Glance'
import { ZonenKarteV4Action } from './ZonenKarteV4Action'
import { ManuellesGiessen } from '../ManuellesGiessen'
import { UnbekanntEventBanner } from '../UnbekanntEventBanner'
import { getActionStateV4, type ActionZustandV4 } from './aktion-state-v4'

// T-0201-Perf: Inspect-Schicht lazy laden -- zieht Recharts + alle
// Detail-Sub-Komponenten (FytaKpiBlock, MLAttributierungNeu, WasserBilanz,
// EntscheidungsErklaerungNeu, MLDriftAmpel, MLPrognoseVsIstChart,
// SensorListeDiagnose) erst beim Aufklappen aus dem Bundle. Spart
// >200 KB gzip im Initial-Render des V2-Tabs.
const ZonenKarteV3Inspect = lazy(() => import('./ZonenKarteV4Inspect'))

interface Props {
  zone: Zone
  mlVerfuegbar: boolean
  /** Wird vom Parent durchgereicht (von App.tsx aus ventil-status).
   *  Q27: der Glance nutzt das, um das Anomalie-Badge
   *  `bewaesserung_ohne_wirkung` waehrend eines aktiven Laufs zu
   *  verstummen (Anti-Pattern T-0186). */
  bewaesserungAktiv?: boolean
  /** T-0221/Q3a: "DSWC 1/2"-Label, vom Parent (App.tsx) zonenweit
   *  berechnet. null bei Single-DSWC-Setup -- dann keine DSWC-Pill. */
  dswcLabel?: string | null
  /** T-0221 Stage D: Klick auf den Karten-Body oeffnet den Detail-
   *  Drawer. Zweites Argument = Anker-Y der Karte (viewport-relativ),
   *  damit der Drawer auf Karten-Hoehe aufgeht. Klicks auf Buttons/
   *  Toggle werden intern ausgefiltert. */
  onKarteKlick?: (zoneId: string, ankerY: number) => void
  schwellenVorschlag?: SchwellenVorschlag
  /** ML-Vorhersagen je Horizont. Wenn der Bulk-Snapshot mit
   *  `ml_details=true` geladen wurde, enthaelt jeder Eintrag auch
   *  `top_features` -- dann zeigt MLAttributierungNeu die SHAP-Werte
   *  ohne Lazy-Fetch. */
  mlVorhersagePropagiert?: Record<string, MLVorhersage>
  /** T-0200: Messwerte pro Zeitfenster (24h/48h/7d/30d), kommt aus dem
   *  Bulk-Snapshot. Der Inspect-Toggle wechselt clientseitig ohne
   *  weiteren Fetch. Wenn `undefined`, faellt die Karte auf einen
   *  eigenen 48h-Fetch zurueck (Backward-Compat). */
  messwertePropagiert?: Partial<Record<SnapshotFenster, Messwert[]>>
  /** T-0200: Wenn gesetzt, ueberspringt die Karte den
   *  `/empfehlung-jetzt`-Fetch. Bei `null` (explizit) zeigt die
   *  Action-Schicht "lade..."; bei `undefined` faellt die Karte
   *  auf ihren eigenen Fetch zurueck (Backward-Compat). */
  empfehlungPropagiert?: GiessEmpfehlung | null
}

function ZonenKarteV4Inner({
  zone,
  mlVerfuegbar,
  bewaesserungAktiv,
  dswcLabel,
  onKarteKlick,
  schwellenVorschlag,
  mlVorhersagePropagiert,
  messwertePropagiert,
  empfehlungPropagiert,
}: Props) {
  const [messwerteFallback, setMesswerteFallback] = useState<Messwert[]>([])
  const [mlLokal, setMlLokal] = useState<Record<string, MLVorhersage>>({})
  const [empfehlungLokal, setEmpfehlungLokal] = useState<GiessEmpfehlung | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)

  useEffect(() => {
    if (messwertePropagiert !== undefined) return
    const ctrl = new AbortController()
    holeMesswerte(zone.zone_id, 48, ctrl.signal)
      .then(d => setMesswerteFallback(d))
      .catch(e => { if (!istAbbruch(e)) setMesswerteFallback([]) })
    return () => ctrl.abort()
  }, [zone.zone_id, refreshKey, messwertePropagiert])

  useEffect(() => {
    if (mlVorhersagePropagiert !== undefined) return
    const ctrl = new AbortController()
    if (mlVerfuegbar) {
      holeMLVorhersage(zone.zone_id, ctrl.signal)
        .then(d => setMlLokal(d))
        .catch(e => { if (!istAbbruch(e)) setMlLokal({}) })
    }
    return () => ctrl.abort()
  }, [zone.zone_id, mlVerfuegbar, mlVorhersagePropagiert, refreshKey])

  useEffect(() => {
    if (empfehlungPropagiert !== undefined) return
    const ctrl = new AbortController()
    holeGiessEmpfehlung(zone.zone_id, ctrl.signal)
      .then(d => setEmpfehlungLokal(d))
      .catch(e => { if (!istAbbruch(e)) setEmpfehlungLokal(null) })
    return () => ctrl.abort()
  }, [zone.zone_id, refreshKey, empfehlungPropagiert])

  // Phase 3 Perf (T-0220): der 60s-Refresh-Timer treibt ausschliesslich
  // die drei Fallback-Fetches oben. Im Bulk-Snapshot-Pfad (alle drei
  // *Propagiert-Props gesetzt) sind diese Effects No-ops -- der Timer
  // waere dann 1x/min nur ein nutzloser Re-Render der Karte. Nur im
  // Fallback-Modus laufen lassen.
  const imFallbackModus =
    messwertePropagiert === undefined
    || mlVorhersagePropagiert === undefined
    || empfehlungPropagiert === undefined
  useEffect(() => {
    if (!imFallbackModus) return
    const t = setInterval(() => setRefreshKey(k => k + 1), 60_000)
    return () => clearInterval(t)
  }, [imFallbackModus])

  // Bulk-Daten gewinnen, sonst Lokal-Fetch.
  const mlVorhersage = mlVorhersagePropagiert ?? mlLokal
  const empfehlung = empfehlungPropagiert !== undefined ? empfehlungPropagiert : empfehlungLokal
  // Trend nutzt 24h-Daten (oder 48h als Fallback) -- `berechneTrendPpProH`
  // filtert intern auf das letzte 6h-Fenster.
  const trendBasis: Messwert[] = messwertePropagiert
    ? (messwertePropagiert['24h'] ?? messwertePropagiert['48h'] ?? messwerteFallback)
    : messwerteFallback
  // T-0370: V4-Zustand kennt zusaetzlich 'sensor' (Daten unzuverlaessig /
  // keine Daten -> neutral statt gruen) und 'beobachten' (praeventiver
  // Bedarf ohne Aktion). V3 (getActionState) bleibt unberuehrt.
  const aktionZustand = getActionStateV4(zone, empfehlung)
  // L5 Reaktive Tiefe (Claude Design): der Inspect-Default haengt vom
  // Karten-Zustand ab -- kritisch/giessen starten auto-offen. Daher
  // muss `aktionZustand` VOR `useExpandedState` stehen.
  const [expanded, setExpanded] = useExpandedState(zone.zone_id, aktionZustand)
  const glanceZustand: 'kritisch' | 'warn' | 'info' | 'ok' | 'stumm' =
    aktionZustand === 'kritisch' ? 'kritisch'
    : aktionZustand === 'nass' ? 'info'   // T-0397 (F2): "zu nass" -> blauer Rahmen
    : aktionZustand === 'giessen_akut' ? 'warn'  // F3b: ML-Prognose -> amber, nicht rot
    : aktionZustand === 'giessen' ? 'warn'
    : aktionZustand === 'beobachten' ? 'warn'
    : aktionZustand === 'blocker' ? 'info'
    : aktionZustand === 'sensor' ? 'stumm'
    : 'ok'
  const trendPpProH = berechneTrendPpProH(trendBasis)

  // Inspect-Schicht bekommt die volle Multi-Fenster-Map. Wenn Fallback
  // (kein Bulk-Snapshot), wickeln wir die 48h-Liste in das Map-Format.
  const messwerteFuerInspect: Partial<Record<SnapshotFenster, Messwert[]>> =
    messwertePropagiert ?? { '48h': messwerteFallback }

  // V2-Action zeigt keine eigenen Buttons mehr -- die echten Aktionen
  // (Gegossen / Live starten / Pre-Soak / Stop) liefert ManuellesGiessen
  // unten in derselben Box. Plan/Bestaetigen/Snooze/Schwelle waren Stubs,
  // ohne Backend-Implementierung -- weg. Dieser Handler bleibt als
  // Compat-Slot fuer den ZonenKarteV4Action-Prop.
  const onAktion = () => { /* no-op */ }

  // T-0221 Stage D: Klick auf den Karten-Body oeffnet den Detail-Drawer.
  // Klicks auf interaktive Elemente (Buttons, DETAILS-Toggle,
  // ManuellesGiessen-Eingaben) zaehlen NICHT als Karten-Klick.
  const onKarteKlickIntern = (e: MouseEvent<HTMLDivElement>) => {
    if (!onKarteKlick) return
    // `summary` = Aufklapper (z.B. "Mehr ML-Details"); `.zk-inspect` =
    // der gesamte inline-DETAILS-Bereich. Klicks dort sollen den Drawer
    // NICHT oeffnen -- man ist schon im Detail-Modus.
    if ((e.target as HTMLElement).closest(
      'button, a, input, select, textarea, label, summary, .zk-inspect',
    )) return
    // Anker-Y der Karte mitgeben -> Drawer oeffnet auf Karten-Hoehe.
    onKarteKlick(zone.zone_id, e.currentTarget.getBoundingClientRect().top)
  }

  return (
    <div
      id={`zone-${zone.zone_id}`}
      className={`zk zk4 zk--${aktionZustand} ${expanded ? 'zk--expanded' : ''} ${onKarteKlick ? 'zk--klickbar' : ''}`}
      onClick={onKarteKlickIntern}
    >
      <ZonenKarteV4Glance
        zone={zone}
        trendPpProH={trendPpProH}
        schwellenVorschlag={schwellenVorschlag}
        mlVorhersage={mlVorhersage}
        zustand={glanceZustand}
        empfehlung={empfehlung}
        dswcLabel={dswcLabel}
      />

      <ZonenKarteV4Action
        zone={zone}
        empfehlung={empfehlung}
        onAktion={onAktion}
        bewaesserungAktiv={bewaesserungAktiv}
      >
        {/* ManuellesGiessen visuell IM Action-Block: Lauf-Banner + Live-
            Buttons (Gegossen/Live/Pre-Soak) gehoeren zur Aktion, keine
            extra Box drunter. */}
        <ManuellesGiessen
          zoneId={zone.zone_id}
          hatVentilKanal={zone.ventil_kanal != null}
          preSoakDefaultMin={zone.pre_soak_min ?? null}
          preSoakPauseMin={zone.pre_soak_pause_min ?? null}
          loggingEinheit={zone.logging_einheit ?? 'sekunden'}
          loggingOptionenMl={zone.logging_optionen_ml}
          // T-0279: Empfohlene Hauptdauer aus der schon im V3-Snapshot
          // geladenen Empfehlung -- Helper spiegelt die GiessEmpfehlung
          // Panel-Header-Hierarchie.
          empfDauerSec={dauerHauptSekunden(empfehlung)}
        />
      </ZonenKarteV4Action>

      {/* T-0094 / T-0114: Klassifikations-Banner fuer unbekannte
          Ventil-Events. Rendert `null` solange keine offenen Events --
          deshalb sicher always-visible (kein Burst-Risiko ohne Event).
          Steht wie in V0 direkt auf der Karte, nicht im DETAILS-Bereich,
          damit der "Was war das?"-Dialog ohne Aufklappen sichtbar ist.
          Banner-Buttons sind <button> -> der Karten-Klick-Guard
          (onKarteKlickIntern) filtert sie, sie oeffnen nicht den Drawer. */}
      <UnbekanntEventBanner zoneId={zone.zone_id} istAquabloom={!!zone.aquabloom_konfig} />

      {/* Obs03 (T-0320): zwei klar getrennte Detail-Pfade.
          - Inline-Toggle "Verlauf & ML v" -- klappt UNTER der Karte auf (Chart
            + ML), Pfeil nach unten.
          - "Alle Details ↗" -- Hover-Hinweis unten rechts, macht sichtbar dass
            der ganze Karten-Body klickbar ist und den Detail-Drawer oeffnet
            (Pfeil nach oben-rechts = neues Panel). Span statt Button, damit der
            Karten-Klick-Guard (onKarteKlickIntern) ihn NICHT ausfiltert. */}
      <div className="zk4-fuss">
        <button
          type="button"
          className={`zk-toggle ${expanded ? 'zk-toggle--offen' : ''}`}
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
        >
          <span>Verlauf &amp; ML</span>
          <span className="zk-toggle__arrow">v</span>
        </button>
        {onKarteKlick && (
          <span className="zk4-drawer-hint" aria-hidden>Alle Details ↗</span>
        )}
      </div>

      {expanded && (
        <Suspense fallback={<div className="zk-inspect__lbl" style={{ padding: 12 }}>Lade Details...</div>}>
          <ZonenKarteV3Inspect
            zone={zone}
            messwerte={messwerteFuerInspect}
            mlVorhersage={mlVorhersage}
            mlVerfuegbar={mlVerfuegbar}
            empfehlung={empfehlung}
          />
        </Suspense>
      )}
    </div>
  )
}

// React.memo ueberspringt Re-Renders, wenn alle Props referenzgleich
// bleiben. Greift bei Parent-Re-Renders, die `zonen` NICHT neu laden
// (z.B. Ventil-Status-Update, Snapshot-Merge). Beim 30s-`zonen`-Poll
// ist `zone` eine neue Referenz -- dann rendert die Karte neu.
// Vollstaendige zone-Referenz-Stabilitaet waere Folge-Arbeit (Backlog).
export const ZonenKarteV4 = memo(ZonenKarteV4Inner)

/** L5 Reaktive Tiefe: der Inspect-Default ergibt sich aus dem Karten-
 *  Zustand -- `kritisch`/`giessen` (= crit/warn) starten auto-offen,
 *  damit die Frage "warum?" sofort beantwortbar ist. Ein gespeicherter
 *  LocalStorage-Wert ist der "User hat aktiv interagiert"-Marker und
 *  gewinnt immer -- sonst spraenge der Inspect bei jedem Re-Mount auf.
 *  Der useState-Initializer laeuft nur 1x pro Mount: ein spaeterer
 *  Live-Zustandswechsel oeffnet den Inspect bewusst NICHT (das waere
 *  visuelle Unruhe); Auto-Open gilt nur fuer die erste Sichtung. */
function useExpandedState(
  zoneId: string,
  zustand: ActionZustandV4,
): [boolean, (v: boolean) => void] {
  const key = `zk-v4-expanded-${zoneId}`
  const [v, setV] = useState<boolean>(() => {
    try {
      const stored = localStorage.getItem(key)
      if (stored !== null) return stored === '1'   // User-Override gewinnt
    } catch { /* leise */ }
    // Default aus dem Zustand: kritisch + giessen (crit/warn) -> offen.
    return zustand === 'kritisch' || zustand === 'giessen'
  })
  const setExpanded = (next: boolean) => {
    setV(next)
    try { localStorage.setItem(key, next ? '1' : '0') } catch { /* leise */ }
  }
  return [v, setExpanded]
}
