/* Pflanzen-Dashboard — Top-Level Layout + Daten-Orchestrierung.
 *
 * Tab-Layout (Stand 2026-07-09, T-0323):
 *   - "Übersicht" (V4, Default): Bulk-Snapshot + sticky Triage-Strip +
 *     Detail-Drawer (UX-Review "Reife").
 *   - "Übersicht (klassisch)" (V0): klassische Karte.
 *   - "Ops" / "Historie" / "Gieß-Historie" / "Freigabe".
 *
 * Historie: V1/V2 mit T-0248 ausgemustert; V3 (Q-A-Matrix-Iteration) war
 * die Basis fuer den V4-Fork (T-0320) und wurde mit T-0323 vollstaendig
 * entfernt -- V4 hat die geteilten Leaves (Inspect/aktion-state/`.zk-*`-CSS)
 * in die v4-Welt uebernommen.
 */

import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import type { Standort, Zone, Prognose, Wetter, SchwellenVorschlag, MLVorhersage, MLStatus, DashboardSnapshotZone, Messwert, SnapshotFenster, GiessEmpfehlung } from './typen'

// T-0201-Perf: stabile leere Defaults, damit `?? {}` / `?? null` im JSX
// keine neue Referenz pro Render erzeugt. React.memo auf ZonenKarteV3
// kann sonst nicht greifen, weil jedes Prop "neu" aussieht.
const LEERES_ML_OBJ: Record<string, MLVorhersage> = Object.freeze({})
const LEERES_MESSWERTE_OBJ: Partial<Record<SnapshotFenster, Messwert[]>> = Object.freeze({})
const LEERE_EMPFEHLUNG: GiessEmpfehlung | null = null
// T-0248: stabile leere Map fuer mlVorhersagenEffektiv-Fallback ausserhalb
// des V3-Tabs (vorher hing das an `mlVorhersagen`-State, der mit V1 wegfiel).
const LEERES_ML_PRO_ZONE: Record<string, Record<string, MLVorhersage>> = Object.freeze({})
import {
  holeStandorte, holeZonen, holePrognosen, holeMLStatus, holeWetter,
  holeVentilStatus, notfallStopp, holeSchwellenVorschlaege,
  holeDashboardSnapshot, holeWartungsFenster,
} from './api'
import { istAbbruch } from './hilfsfunktionen'
import { ZonenKarte } from './komponenten/ZonenKarte'
// T-0323: `.zk-*`-Basis-Styles (frueher unter zonen-karte-v3/) -- weiter
// global, jetzt aus der v4-Welt geladen. V4 nutzt dieselben `.zk-*`-Klassen;
// die V3-Karte ist mit T-0323 entfallen.
import './komponenten/zonen-karte-v4/karte-basis.css'
import './komponenten/zonen-karte-v4/zonen-karte-basis-v3.css'
// T-0320/T-0323: V4 ist die (einzige) "Uebersicht". V0 = klassisch.
import { ZonenKarteV4 } from './komponenten/zonen-karte-v4/ZonenKarteV4'
import { TriageStripV4 } from './komponenten/zonen-karte-v4/TriageStripV4'
import './komponenten/zonen-karte-v4/zonen-karte-v4.css'
import { WetterKarte } from './komponenten/WetterKarte'
import { StatusLeiste } from './komponenten/StatusLeiste'
import { MLStatusBadge } from './komponenten/MLStatusBadge'
import { MLRetrainStatusKachel } from './komponenten/MLRetrainStatusKachel'
import { MLFehlerBanner } from './komponenten/MLFehlerBanner'
import { VerbindungsBanner } from './komponenten/VerbindungsBanner'
import { EntscheidungsLog } from './komponenten/EntscheidungsLog'
// T-0275: Lazy-Load fuer die 3 schweren Tabs (Bundle-Split). Werden
// nur geladen, wenn der User auf den Tab klickt. Vorher 698 kB
// Hauptbundle, jetzt < 400 kB.
const OpsTab = lazy(() => import('./komponenten/OpsTab').then(m => ({ default: m.OpsTab })))
const HistorieTab = lazy(() => import('./komponenten/HistorieTab').then(m => ({ default: m.HistorieTab })))
const GiessHistorieTab = lazy(() => import('./komponenten/GiessHistorieTab').then(m => ({ default: m.GiessHistorieTab })))
const FreigabeTab = lazy(() => import('./komponenten/FreigabeTab').then(m => ({ default: m.FreigabeTab })))
import { FehlerGrenze } from './komponenten/FehlerGrenze'
import { KritischBand, ermittleKritischeZoneIds } from './komponenten/KritischBand'
import { HeutePlan } from './komponenten/HeutePlan'
import { PflegeErinnerungenBlock } from './komponenten/PflegeErinnerungenBlock'
import { TagesplanBlock } from './komponenten/TagesplanBlock'
import { AuthDialog, AuthStatus } from './komponenten/AuthDialog'
import './App.css'

// T-0221 Stage D / T-0320: Detail-Drawer lazy (V4) -- zieht Recharts erst beim Oeffnen.
const ZonenKarteV4Drawer = lazy(() => import('./komponenten/zonen-karte-v4/ZonenKarteV4Drawer'))

type TabName = 'uebersicht' | 'ops' | 'historie' | 'giess-historie' | 'uebersicht-v4' | 'freigabe'

/** T-0295: Referenz-stabiler Zonen-Merge. Der 30s-`zonen`-Poll liefert
 *  jedes Mal frische Objekte -- selbst fuer inhaltlich unveraenderte Zonen.
 *  Da `zone` ein Prop der `memo`-Karten (ZonenKarte / ZonenKarteV3) ist,
 *  bricht das die Memoisierung und ALLE Karten rendern bei jedem Poll neu
 *  (V3 spuerbar teurer: Glance + Action + ManuellesGiessen + Banner pro
 *  Karte). Hier behalten inhaltsgleiche Zonen ihre alte Objekt-Referenz
 *  (JSON-Deep-Equal) -> die memo-Karte ueberspringt den Re-Render. Das
 *  Array selbst bleibt eine neue Referenz, damit Array-Konsumenten
 *  (useMemo/useEffect mit `[zonen]`) unveraendert weiterlaufen. */
function mergeZonenStabil(prev: Zone[], neu: Zone[]): Zone[] {
  if (prev.length === 0) return neu
  const altById = new Map(prev.map(z => [z.zone_id, z]))
  return neu.map(z => {
    const alt = altById.get(z.zone_id)
    return alt && JSON.stringify(alt) === JSON.stringify(z) ? alt : z
  })
}

function App() {
  const [aktuellerTab, setAktuellerTab] = useState<TabName>('uebersicht-v4')
  // T-0221 Stage D: offener Detail-Drawer -- Zone-ID + Anker-Y der
  // geklickten Karte (der Drawer oeffnet auf Karten-Hoehe statt in der
  // oberen Ecke). null = zu.
  const [offeneDrawerZone, setOffeneDrawerZone] =
    useState<{ zoneId: string; ankerY: number } | null>(null)
  // useCallback -> stabile Ref, damit React.memo auf ZonenKarteV3 greift.
  const oeffneDrawer = useCallback(
    (zoneId: string, ankerY: number) => setOffeneDrawerZone({ zoneId, ankerY }),
    [],
  )
  const [standorte, setStandorte] = useState<Standort[]>([])
  const [zonen, setZonen] = useState<Zone[]>([])
  const [prognosen, setPrognosen] = useState<Prognose[]>([])
  const [wetterMap, setWetterMap] = useState<Record<string, Wetter>>({})
  const [fehler, setFehler] = useState<string | null>(null)
  const [letzterRefresh, setLetzterRefresh] = useState<Date>(new Date())
  const [mlVerfuegbar, setMlVerfuegbar] = useState(false)
  // T-0183 (Folge B3): voller MLStatus fuer MLRetrainStatusKachel.
  const [mlStatus, setMlStatus] = useState<MLStatus | null>(null)
  const [ventilAktiv, setVentilAktiv] = useState<Set<string>>(new Set())
  const [ventilStatusFehler, setVentilStatusFehler] = useState<string | null>(null)
  const [schwellenVorschlaege, setSchwellenVorschlaege] = useState<SchwellenVorschlag[]>([])
  // T-0200: Bulk-Snapshot-Daten pro Zone (Messwerte, Empfehlung). Wird
  // ausschliesslich bei aktivem V3-Tab gepollt. Ersetzt die drei
  // Per-Karten-Fetches in ZonenKarteV3 durch genau einen Backend-Call.
  // T-0248: V2-Tab entfaellt, nur noch V3 als Snapshot-Konsument.
  const [snapshotProZone, setSnapshotProZone] = useState<Record<string, DashboardSnapshotZone>>({})
  // T-0228 Stufe 2: Map zone_id -> fenster_id der offenen Wartungs-
  // Fenster. App-Level-Polling alle 60 s; die Karten lesen daraus
  // ob "Wartung aktiv" angezeigt werden soll, und kennen die ID fuer
  // den Beenden-Klick.
  const [wartungsFensterProZone, setWartungsFensterProZone] = useState<Record<string, number>>({})

  const aufNotfallStopp = async () => {
    if (!confirm('Alle Ventile sofort schließen?')) return
    let ergebnis: Awaited<ReturnType<typeof notfallStopp>>
    try {
      ergebnis = await notfallStopp()
    } catch (e) {
      // Safety (codex 2026-04-10): Fehlschlag NICHT optimistisch behandeln --
      // der Ventilzustand ist unklar, die UI darf das nicht verschlucken.
      const nachricht = e instanceof Error ? e.message : 'unbekannter Fehler'
      setVentilStatusFehler(`Notfall-Stopp nicht bestätigt (${nachricht}) — Ventilzustand UNKLAR!`)
      alert(`Notfall-Stopp fehlgeschlagen: ${nachricht}\nVentilzustand UNKLAR — bitte App/Hardware prüfen.`)
      return
    }
    let refetchOk = false
    try {
      const status = await holeVentilStatus()
      setVentilAktiv(sammleVentilZonen(status))
      setVentilStatusFehler(null)
      refetchOk = true
    } catch (e) {
      const nachricht = e instanceof Error ? e.message : 'unbekannter Fehler'
      setVentilStatusFehler(`Ventilstatus unklar: ${nachricht}`)
    }
    if (ergebnis.fehler) {
      alert(`Notfall-Stopp fehlgeschlagen: ${ergebnis.fehler}`)
    } else if (!refetchOk) {
      alert('Notfall-Stopp gesendet, aber der frische Ventilstatus ist unklar.')
    }
  }

  useEffect(() => {
    const controller = new AbortController()
    const signal = controller.signal

    const laden = () => {
      holeMLStatus(signal)
        .then(s => { setMlVerfuegbar(s.ist_geladen); setMlStatus(s) })
        .catch(e => { if (!istAbbruch(e)) { setMlVerfuegbar(false); setMlStatus(null) } })

      holeZonen(signal)
        .then(z => {
          // T-0295: referenz-stabiler Merge -> inhaltsgleiche Zonen behalten
          // ihre Objekt-Referenz, memo-Karten rendern beim Poll nicht neu.
          setZonen(prev => mergeZonenStabil(prev, z))
          setFehler(null); setLetzterRefresh(new Date())
        })
        .catch(e => { if (!istAbbruch(e)) setFehler(`Backend nicht erreichbar: ${e.message}`) })

      holePrognosen(signal)
        .then(setPrognosen)
        .catch(e => { if (!istAbbruch(e)) { /* leise */ } })

      holeVentilStatus(signal)
        .then(s => { setVentilAktiv(sammleVentilZonen(s)); setVentilStatusFehler(null) })
        .catch(e => {
          if (!istAbbruch(e)) {
            setVentilStatusFehler(`Ventilstatus unklar: ${e.message}`)
          }
        })

      holeStandorte(signal)
        .then(sList => {
          setStandorte(sList)
          const wetterIds = [...new Set(sList.map(s => s.wetter_standort).filter(Boolean))]
          for (const wid of wetterIds) {
            holeWetter(wid, signal)
              .then(w => setWetterMap(prev => ({ ...prev, [wid]: w })))
              .catch(e => { if (!istAbbruch(e)) { /* leise */ } })
          }
        })
        .catch(e => { if (!istAbbruch(e)) { /* leise */ } })

      holeSchwellenVorschlaege(signal)
        .then(setSchwellenVorschlaege)
        .catch(e => { if (!istAbbruch(e)) { /* leise */ } })

      holeWartungsFenster(signal)
        .then(r => {
          const map: Record<string, number> = {}
          for (const w of r.eintraege) map[w.zone_id] = w.id
          setWartungsFensterProZone(map)
        })
        .catch(e => { if (!istAbbruch(e)) { /* leise */ } })
    }

    laden()
    const intervall = setInterval(laden, 30_000)
    return () => {
      controller.abort()
      clearInterval(intervall)
    }
  }, [])

  // T-0248: `zoneIdsKey` war Dep fuer den V1-spezifischen ML-Mass-Loader
  // (Per-Zone-`holeMLVorhersage`). Mit V1 weg ist die stabile Zonen-Liste
  // hier nicht mehr noetig -- V3 zieht ML-Daten aus dem Bulk-Snapshot.

  // T-0201-Perf: Lookup-Map statt 14× find() pro Render. Plus stabile
  // Referenz, damit React.memo auf ZonenKarteV3 greift und Karten nicht
  // jeden 30s-Parent-Refresh neu rendern.
  const schwellenVorschlagProZone = useMemo(() => {
    const m: Record<string, SchwellenVorschlag | undefined> = {}
    for (const v of schwellenVorschlaege) m[v.zone_id] = v
    return m
  }, [schwellenVorschlaege])

  // T-0221/Q3a: "DSWC 1/2"-Label pro Zone fuer die V3-Karten-Sub-Zeile.
  // Nur in Multi-DSWC-Setups gesetzt (sonst null -> keine Pill).
  // Distinkte Ventil-Geraete werden sortiert durchnummeriert; das
  // per-Karte-Prop ist ein String und damit React.memo-vertraeglich.
  const dswcLabelProZone = useMemo<Record<string, string | null>>(() => {
    const geraete = [...new Set(
      zonen
        .filter(z => z.ventil_kanal != null)
        .map(z => z.ventil_geraet_id ?? 'primaer'),
    )].sort()
    const ordinal = new Map(geraete.map((id, i) => [id, i + 1]))
    const r: Record<string, string | null> = {}
    for (const z of zonen) {
      r[z.zone_id] = geraete.length > 1 && z.ventil_kanal != null
        ? `DSWC ${ordinal.get(z.ventil_geraet_id ?? 'primaer')}`
        : null
    }
    return r
  }, [zonen])

  // T-0248: Der V1-spezifische ML-Vorhersagen-Mass-Loader (Per-Zone-
  // `holeMLVorhersage` pro Tab-Aktivierung) wurde mit V1 ausgemustert.
  // V3 zieht ML-Vorhersagen aus `snapshotProZone` (siehe nachfolgenden
  // Bulk-Snapshot-Effect + `mlVorhersagenEffektiv`-useMemo).

  // T-0200: Bulk-Snapshot-Loader fuer den V3-Tab. Eine HTTP-Anfrage
  // ersetzt 14 Zonen × 3 Per-Karten-Calls (Messwerte, Empfehlung,
  // ML-Vorhersage). Die Per-Karten-useEffects in ZonenKarteV3 erkennen
  // an den `*Propagiert`-Props, dass die Daten von oben kommen, und
  // unterdruecken ihre Fetches.
  useEffect(() => {
    if (aktuellerTab !== 'uebersicht-v4') {
      // T-0201-Perf: snapshotProZone BLEIBT erhalten, wenn der User
      // weg von V3 wechselt -- nur das Polling stoppt. Beim Re-Wechsel
      // auf V3 sind die Karten dann sofort wieder voll, statt 1-3s auf
      // einen frischen Snapshot zu warten. Stale-Daten werden vom
      // nachfolgenden 60s-Polling ueberschrieben.
      return
    }
    const controller = new AbortController()
    let abgebrochen = false

    const laden = async () => {
      try {
        // Schnell-Pfad: 24h + 48h, ohne ML-Details. Reicht fuer Trend,
        // Action-State und den initialen Inspect-Chart. 7d/30d und
        // ml_details werden im naechsten Schritt nachgeladen, damit
        // der Initial-Render nicht das Backend uebermaessig belastet.
        const snap = await holeDashboardSnapshot(
          controller.signal,
          ['24h', '48h'],
          false,
        )
        if (abgebrochen) return
        const nach_zone: Record<string, DashboardSnapshotZone> = {}
        for (const z of snap.zonen) nach_zone[z.zone.zone_id] = z
        setSnapshotProZone(nach_zone)
      } catch (e) {
        if (!istAbbruch(e)) { /* leise — Per-Karten-Fallback greift */ }
      }
    }

    // Mittel-Stufe: 7d + 30d-Messwerte + ML-Details. Ladet 5 Sekunden
    // verzoegert, damit der Schnell-Pfad zuerst rendert und das Backend
    // nicht beide Bulk-Calls parallel verarbeiten muss.
    const ladenMittel = async () => {
      try {
        const snap = await holeDashboardSnapshot(
          controller.signal,
          ['7d', '30d'],
          true,
        )
        if (abgebrochen) return
        // Merge: nur die zusaetzlichen Fenster + ML-Details in den State,
        // 24h/48h aus Schnell-Pfad bleiben erhalten.
        setSnapshotProZone(prev => {
          const next: Record<string, DashboardSnapshotZone> = { ...prev }
          for (const z of snap.zonen) {
            const vorhanden = next[z.zone.zone_id]
            next[z.zone.zone_id] = vorhanden
              ? {
                  ...vorhanden,
                  messwerte: { ...vorhanden.messwerte, ...z.messwerte },
                  ml_vorhersage: z.ml_vorhersage,
                  ml_vorhersage_fehler: z.ml_vorhersage_fehler,
                }
              : z
          }
          return next
        })
      } catch (e) {
        if (!istAbbruch(e)) { /* leise */ }
      }
    }
    void laden()
    // Mittel-Stufe leicht verzoegert + selten gepollt (5 Sek nach Mount,
    // dann alle 15 Min). Vermeidet doppelten Bulk-Hit beim Tab-Wechsel.
    const tInitMittel = setTimeout(() => void ladenMittel(), 5000)
    const timer = setInterval(() => void laden(), 60_000)
    const timerMittel = setInterval(() => void ladenMittel(), 15 * 60_000)

    return () => {
      abgebrochen = true
      controller.abort()
      clearTimeout(tInitMittel)
      clearInterval(timer)
      clearInterval(timerMittel)
    }
  }, [aktuellerTab])

  // T-0200/T-0248: ML-Vorhersagen fuer KritischBand/HeutePlan im V3-Tab
  // aus dem Bulk-Snapshot ableiten (statt extra Polling). Leere Map
  // wenn V3 nicht aktiv ist.
  const mlVorhersagenEffektiv = useMemo(() => {
    if (aktuellerTab !== 'uebersicht-v4') return LEERES_ML_PRO_ZONE
    const aus_snapshot: Record<string, Record<string, MLVorhersage>> = {}
    for (const [zid, z] of Object.entries(snapshotProZone)) {
      if (Object.keys(z.ml_vorhersage).length > 0) {
        aus_snapshot[zid] = z.ml_vorhersage
      }
    }
    return aus_snapshot
  }, [aktuellerTab, snapshotProZone])

  // ML-24h-Werte fuer KritischBand (nur .feuchte_prognose, kompakt).
  // T-0200: nutzt den effektiven ML-Stand (Snapshot im V2-Tab,
  // Mass-Loader sonst).
  const vorhersagen24h: Record<string, number> = useMemo(() => {
    const r: Record<string, number> = {}
    for (const [zid, h] of Object.entries(mlVorhersagenEffektiv)) {
      if (h['24h']) r[zid] = h['24h'].feuchte_prognose
    }
    return r
  }, [mlVorhersagenEffektiv])

  // T-0278: Engine-Empfehlung pro Zone fuer KritischBand. Das Banner
  // darf nicht allein auf der rohen ML-24h-Prognose feuern (Fehlalarm
  // waldblumen 30.05.: ML sagte 33% in 24h, Engine sagte kein_bedarf
  // FEUCHTE_OK). Aus dem V3-Snapshot abgeleitet.
  const empfehlungProZone: Record<string, GiessEmpfehlung> = useMemo(() => {
    const r: Record<string, GiessEmpfehlung> = {}
    for (const [zid, z] of Object.entries(snapshotProZone)) {
      if (z.empfehlung) r[zid] = z.empfehlung
    }
    return r
  }, [snapshotProZone])

  // T-0397 (F10b): Zonen, die das KRITISCH-Band zeigt -- der Heute-Plan blendet
  // sie aus, damit dieselben Zonen nicht doppelt direkt untereinander stehen.
  const kritischeZoneIds = useMemo(
    () => ermittleKritischeZoneIds(zonen, vorhersagen24h, empfehlungProZone),
    [zonen, vorhersagen24h, empfehlungProZone],
  )

  const zuZoneScrollen = (zoneId: string) => {
    const el = document.getElementById(`zone-${zoneId}`)
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  const gezeigtesWetter = new Set<string>()

  // T-0248: V1 (Uebersicht neu), V2 (Uebersicht v2), Ops/Historie neu
  // wurden ausgemustert. V3 traegt die neue Karten-Iteration, V0 bleibt
  // als Legacy-Default-Tab.
  const tabs: Array<{ id: TabName; label: string }> = [
    // T-0323 (Go-Live 01.07.): V4 hat V3 abgeloest -> V4 ist die primaere
    // "Uebersicht" (Default). V3-Tab entfernt. V0 bleibt als klassische Karte.
    { id: 'uebersicht-v4', label: 'Übersicht' },
    { id: 'ops', label: 'Ops' },
    { id: 'historie', label: 'Historie' },
    // T-0335: Giess-Laeufe pro Zone (wann/wie-lange/wie inkl. Pre-Soak).
    { id: 'giess-historie', label: 'Gieß-Historie' },
    // T-0236: Audit-Trail + Heuristik-vs-ML-Dauer-Vergleich.
    // Konkrete Voraussetzung fuer T-0021 (ventilsteuerung_aktiv=true).
    { id: 'freigabe', label: 'Freigabe' },
    { id: 'uebersicht', label: 'Übersicht (klassisch)' },
  ]

  return (
    <div className={`app ${aktuellerTab === 'uebersicht-v4' ? 'app--v4' : ''}`}>
      {/* T-0239: Modal-Dialog wenn kein API-Key in localStorage gesetzt
          ist (Initial-Setup oder Multi-Tab-Abmelden). */}
      <AuthDialog />
      <header className="app-header">
        <div className="app-header-links">
          <h1>Pflanzen-Dashboard</h1>
          <MLStatusBadge verfuegbar={mlVerfuegbar} />
          {/* T-0239: Status-Indikator + Schluessel-Wechsel/Abmelden. */}
          <AuthStatus />
        </div>
        <div className="app-header-rechts">
          {(ventilAktiv.size > 0 || ventilStatusFehler) && (
            <button className="notfall-stopp-btn" onClick={aufNotfallStopp}>
              Notfall-Stopp
            </button>
          )}
          <span className="refresh-info">
            {ventilStatusFehler ?? (fehler ? fehler : `Aktualisiert ${letzterRefresh.toLocaleTimeString('de-DE')}`)}
          </span>
        </div>
      </header>

      <div className="tab-leiste" role="tablist" aria-label="Dashboard Ansichten">
        {tabs.map(t => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={aktuellerTab === t.id}
            className={`tab-btn ${aktuellerTab === t.id ? 'aktiv' : ''} ${t.id.endsWith('-neu') ? 'tab-btn-neu' : ''}`}
            onClick={() => setAktuellerTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* T-0311: prominenter Verbindungsverlust-/Stale-Indikator — sobald
          der letzte Daten-Abruf fehlschlug oder zu lange her ist (statt still
          tickender Alt-Werte). Tab-uebergreifend. */}
      <VerbindungsBanner fehler={fehler} letzterErfolg={letzterRefresh} />

      {/* T-0108: globales Banner bei stillen ML-Job-Crashes — tab-uebergreifend. */}
      <MLFehlerBanner />

      {aktuellerTab === 'uebersicht' && (
        <FehlerGrenze titel="Uebersicht konnte nicht geladen werden">
          {/* T-0227: Tagesplan-Vorschau "heute/morgen". */}
          <TagesplanBlock aktiveZonen={ventilAktiv} zonen={zonen} />
          {/* T-0228 Stufe 1: Pflege-/Wartungs-Erinnerungen.
              Block bleibt unsichtbar wenn nichts ansteht. */}
          <PflegeErinnerungenBlock zonen={zonen} />
          <StatusLeiste zonen={zonen} prognosen={prognosen} />

          <main>
            {standorte.map(standort => {
              const standortZonen = zonen.filter(z => standort.zonen.includes(z.zone_id))
              const wetterId = standort.wetter_standort
              const wetterDaten = wetterId ? wetterMap[wetterId] : null
              const zeigeWetter = wetterId && !gezeigtesWetter.has(wetterId) && wetterDaten
              if (zeigeWetter) gezeigtesWetter.add(wetterId)

              if (standortZonen.length === 0 && !zeigeWetter) return null
              return (
                <section key={standort.standort_id} className="standort-sektion">
                  <h2 className="standort-titel">{standort.name}</h2>
                  {standortZonen.length > 0 && (
                    <div className="zonen-grid">
                      {standortZonen.map(zone => (
                        <FehlerGrenze key={zone.zone_id} titel={`Zone ${zone.name} — Anzeige fehlgeschlagen`}>
                          <ZonenKarte
                            zone={zone}
                            prognose={prognosen.find(p => p.zone_id === zone.zone_id)}
                            mlVerfuegbar={mlVerfuegbar}
                            bewaesserungAktiv={ventilAktiv.has(zone.zone_id)}
                            schwellenVorschlag={schwellenVorschlaege.find(v => v.zone_id === zone.zone_id)}
                            wartungsFensterId={wartungsFensterProZone[zone.zone_id]}
                            onWartungsAenderung={() => {
                              // T-0228 Stufe 2: nach Start/Stop sofort
                              // Mass-Loader neu feuern, damit andere
                              // Karten gleichzeitig die Aenderung sehen.
                              holeWartungsFenster()
                                .then(r => {
                                  const map: Record<string, number> = {}
                                  for (const w of r.eintraege) map[w.zone_id] = w.id
                                  setWartungsFensterProZone(map)
                                })
                                .catch(() => {})
                            }}
                          />
                        </FehlerGrenze>
                      ))}
                    </div>
                  )}
                  {zeigeWetter && (
                    <div className="wetter-section">
                      <WetterKarte
                        wetter={wetterDaten}
                        titel={`Wetter ${standort.name}`}
                      />
                    </div>
                  )}
                </section>
              )
            })}
          </main>

          <EntscheidungsLog zonen={zonen} />
        </FehlerGrenze>
      )}

      {aktuellerTab === 'ops' && (
        <FehlerGrenze titel="Ops-Tab konnte nicht geladen werden">
          <main>
            <Suspense fallback={<p style={{ padding: '1rem' }}>Lade Ops-Tab …</p>}>
              <OpsTab zonen={zonen} />
            </Suspense>
          </main>
        </FehlerGrenze>
      )}
      {aktuellerTab === 'historie' && (
        <FehlerGrenze titel="Historie-Tab konnte nicht geladen werden">
          <main>
            <Suspense fallback={<p style={{ padding: '1rem' }}>Lade Historie-Tab …</p>}>
              <HistorieTab zonen={zonen} />
            </Suspense>
          </main>
        </FehlerGrenze>
      )}
      {aktuellerTab === 'giess-historie' && (
        <FehlerGrenze titel="Gieß-Historie-Tab konnte nicht geladen werden">
          <main>
            <Suspense fallback={<p style={{ padding: '1rem' }}>Lade Gieß-Historie …</p>}>
              <GiessHistorieTab zonen={zonen} />
            </Suspense>
          </main>
        </FehlerGrenze>
      )}
      {/* T-0236: Audit-Trail (T-0122) + Dauer-Drift (T-0065) pro Zone. */}
      {aktuellerTab === 'freigabe' && (
        <FehlerGrenze titel="Freigabe-Tab konnte nicht geladen werden">
          <main>
            <Suspense fallback={<p style={{ padding: '1rem' }}>Lade Freigabe-Tab …</p>}>
              <FreigabeTab zonen={zonen} />
            </Suspense>
          </main>
        </FehlerGrenze>
      )}

      {/* T-0320/T-0323: V4 ist die "Übersicht" (Default-Tab). Sticky Triage-
          Strip + eigene V4-Karten (`-v4`-Layout, geteilte `.zk-*`-Basis) +
          eigener Drawer. Fork von V3 (T-0320), V3 mit T-0323 entfernt. */}
      {aktuellerTab === 'uebersicht-v4' && (
        <FehlerGrenze titel="Übersicht v4 konnte nicht geladen werden">
          {/* Obs02 (T-0320): sticky Triage-Strip ganz oben -- Zaehl-Ueberblick
              ueber ALLE Zonen/Standorte, ersetzt die StatusLeiste im V4-Tab. */}
          <TriageStripV4
            zonen={zonen}
            prognosen={prognosen}
            bewaesserungAktiv={ventilAktiv}
            onZoneClick={zuZoneScrollen}
          />
          {/* F10 (T-0397, Andre 11.07. "Overhead oben stoert"): was-braucht-
              mich-jetzt zuerst -- Kritisch-Band direkt unter dem Strip, dann der
              heutige Plan, dann die Karten. Kontext/Operatives (Tagesplan,
              Anstehend, Retrain) wandert UNTER die Karten. */}
          <KritischBand
            zonen={zonen}
            prognosen={prognosen}
            vorhersagen24h={vorhersagen24h}
            empfehlungProZone={empfehlungProZone}
            onZoneClick={zuZoneScrollen}
          />
          <HeutePlan
            zonen={zonen}
            prognosen={prognosen}
            vorhersagenProZone={mlVorhersagenEffektiv}
            onZoneClick={zuZoneScrollen}
            ausblendenZoneIds={kritischeZoneIds}
          />

          {/* T-0397 (F21): leere Zonen-Liste NICHT stumm als leere Flaeche
              rendern -- das las sich live wie "alles weg". Explizit einordnen:
              Backend-Fehler (fehler gesetzt) vs. noch-nicht-geladen. So weiss
              der Blick, ob ein Problem vorliegt oder nur der erste Poll laeuft. */}
          {zonen.length === 0 && (
            <div className="zonen-leer-hinweis" role="status">
              {fehler
                ? `Keine Zonen geladen — ${fehler}`
                : 'Zonen werden geladen…'}
            </div>
          )}

          <main>
            {standorte.map(standort => {
              const standortZonen = sortiereZonenV3(
                zonen.filter(z => standort.zonen.includes(z.zone_id)),
              )
              const wetterId = standort.wetter_standort
              const wetterDaten = wetterId ? wetterMap[wetterId] : null
              const zeigeWetter = wetterId && !gezeigtesWetter.has(wetterId) && wetterDaten
              if (zeigeWetter) gezeigtesWetter.add(wetterId)

              if (standortZonen.length === 0 && !zeigeWetter) return null
              return (
                <section key={standort.standort_id} className="standort-sektion-v4">
                  <div className="standort-kopf-v4">
                    <span className="standort-nm-v4">{standort.name}</span>
                    <span className="standort-kopf-v4__rechts">
                      {standortZonen.length > 0 && (
                        <span className="standort-meta-v4">{standortMetaV3(standortZonen)}</span>
                      )}
                      {/* Obs05 (T-0320): Farb-Legende -- erklaert das Farbsystem
                          (Quelle vs Status vs ML) einmal pro Standort-Header. */}
                      <span
                        className="zk4-legende"
                        title={'Farben im V4-Tab:\n• Rahmen oben = Zustand (grün ok · amber gießen/Bedarf · rot kritisch · grau Sensor-Problem)\n• Badge = Steuerung (AUTOMATIK gießt autonom · SHADOW nur protokolliert · MONITORING beobachtet)\n• Achsen-Punkte F/L/T/N = Pflanzen-Optimum (FYTA)\n• Sensor-Pills = Quelle (blau Gardena · violett FYTA)\n• ML-Prognose = entsättigtes Violett (Analyse, nicht FYTA)\n• Warnungen stehen in der Action-Zeile, nicht als Badge'}
                      >ⓘ Farben</span>
                    </span>
                  </div>
                  {standortZonen.length > 0 && (
                    <div className="zonen-grid-v4">
                      {standortZonen.map(zone => {
                        const snap = snapshotProZone[zone.zone_id]
                        return (
                          <FehlerGrenze key={zone.zone_id} titel={`Zone ${zone.name} — Anzeige fehlgeschlagen`}>
                            <ZonenKarteV4
                              zone={zone}
                              mlVerfuegbar={mlVerfuegbar}
                              bewaesserungAktiv={ventilAktiv.has(zone.zone_id)}
                              dswcLabel={dswcLabelProZone[zone.zone_id]}
                              onKarteKlick={oeffneDrawer}
                              schwellenVorschlag={schwellenVorschlagProZone[zone.zone_id]}
                              mlVorhersagePropagiert={snap?.ml_vorhersage ?? LEERES_ML_OBJ}
                              messwertePropagiert={snap?.messwerte ?? LEERES_MESSWERTE_OBJ}
                              empfehlungPropagiert={snap?.empfehlung ?? LEERE_EMPFEHLUNG}
                            />
                          </FehlerGrenze>
                        )
                      })}
                    </div>
                  )}
                  {zeigeWetter && (
                    <div className="wetter-section">
                      <WetterKarte
                        wetter={wetterDaten}
                        titel={`Wetter ${standort.name}`}
                      />
                    </div>
                  )}
                </section>
              )
            })}
          </main>

          {/* F10: Kontext/Operatives unter die Karten -- Tagesplan (Tages-/
              Wetterkontext), Anstehend (Pflege 3 Tage), ML-Retrain-Status. */}
          <TagesplanBlock aktiveZonen={ventilAktiv} zonen={zonen} />
          <PflegeErinnerungenBlock zonen={zonen} />
          <MLRetrainStatusKachel status={mlStatus} />

          <EntscheidungsLog zonen={zonen} />

          {offeneDrawerZone && (() => {
            const drawerZone = zonen.find(z => z.zone_id === offeneDrawerZone.zoneId)
            if (!drawerZone) return null
            const drawerSnap = snapshotProZone[offeneDrawerZone.zoneId]
            return (
              <Suspense fallback={null}>
                <ZonenKarteV4Drawer
                  zone={drawerZone}
                  ankerY={offeneDrawerZone.ankerY}
                  messwerte={drawerSnap?.messwerte ?? LEERES_MESSWERTE_OBJ}
                  mlVorhersage={drawerSnap?.ml_vorhersage ?? LEERES_ML_OBJ}
                  empfehlung={drawerSnap?.empfehlung ?? LEERE_EMPFEHLUNG}
                  onSchliessen={() => setOffeneDrawerZone(null)}
                />
              </Suspense>
            )
          })()}
        </FehlerGrenze>
      )}
    </div>
  )
}

/** Sammelt alle Zone-IDs mit aktuell offenem Ventil aus dem Backend-
 *  Status. Liest BEIDE Quellen: `aktiv` (vom Bewaesserungs-Backend
 *  selbst gestartet) UND `extern` (User per Gardena-App, Schedule,
 *  MANUAL_WATERING). Ohne `extern` wuerden Bambuswald-Bewaesserungen
 *  per Gardena-App nicht angezeigt -- User-Bug aus T-0201-Folge. */
function sammleVentilZonen(s: { aktiv: Record<string, { zone_ids: string[] }>; extern: Record<string, { zone_ids: string[] }> }): Set<string> {
  const aktiveZonen = new Set<string>()
  for (const info of Object.values(s.aktiv)) {
    for (const zid of info.zone_ids) aktiveZonen.add(zid)
  }
  for (const info of Object.values(s.extern)) {
    for (const zid of info.zone_ids) aktiveZonen.add(zid)
  }
  return aktiveZonen
}

/** T-0207 (17.05.): Feste Outdoor-Reihenfolge fuer V3.
 *  Bambus-Geschwisterzonen direkt hintereinander, danach Sprinkler-
 *  Beete in Pflanz-Reihenfolge (Bestand -> Etablierung -> Anwachs).
 *  Zonen, die nicht in der Liste sind, fallen auf die Akut-Sortierung
 *  zurueck — Indoor/Balkon bleibt also unveraendert.
 *  (T-0248: vorher V2_FESTE_REIHENFOLGE, V2 ist mit V1 weg.) */
const V3_FESTE_REIHENFOLGE: Record<string, number> = {
  bambuswald: 0,
  bambuswald_yogaraum: 1,
  waldblumenhain: 2,
  magerwiese: 3,
  hecke: 4,
}

/* T-0221: Sortierung fuer den V3-Reiter -- Hybrid + Bewaesserungs-
   Kohaesion (User-Entscheidung 20.05.):
   - Zonen am selben Ventil (gleiche `(ventil_geraet_id, ventil_kanal)`)
     bilden eine Gruppe, die NIE auseinandergerissen wird.
   - Kritische Gruppen schwimmen nach oben (dringendste zuerst), eine
     kritische Zone zieht ihre Kanal-Geschwister mit.
   - Nicht-kritische Gruppen bleiben in der festen T-0207-Reihenfolge. */
function sortiereZonenV3(zonen: Zone[]): Zone[] {
  const rang = (z: Zone): number => {
    const ist = z.aktuelle_feuchte
    const kritisch = z.feuchte_kritisch ?? null
    const min = z.feuchte_schwelle_min
    const warnungKrit = (z.offene_warnungen ?? []).some(w => /kritisch/i.test(w.typ))
    if (warnungKrit) return 0
    if (ist == null) return 3
    if (kritisch != null && ist < kritisch) return 0
    if (ist < min) return 1
    if (ist < min + 5) return 2
    return 3
  }
  const festIndex = (z: Zone): number =>
    V3_FESTE_REIHENFOLGE[z.zone_id] ?? Number.POSITIVE_INFINITY
  // Bewaesserungs-Gruppe: gleiches Ventil = gleiche (geraet_id, kanal).
  // Zonen ohne Kanal stehen fuer sich (eigener Key ueber zone_id).
  const gruppenKey = (z: Zone): string =>
    z.ventil_kanal != null
      ? `v:${z.ventil_geraet_id ?? 'primaer'}#${z.ventil_kanal}`
      : `z:${z.zone_id}`
  const gruppen = new Map<string, Zone[]>()
  for (const z of zonen) {
    const k = gruppenKey(z)
    const g = gruppen.get(k)
    if (g) g.push(z)
    else gruppen.set(k, [z])
  }
  const gruppenListe = [...gruppen.values()].map(mitglieder => {
    const sortiert = [...mitglieder].sort((a, b) => {
      const fd = festIndex(a) - festIndex(b)
      if (fd !== 0) return fd
      const rd = rang(a) - rang(b)
      if (rd !== 0) return rd
      return (a.aktuelle_feuchte ?? Infinity) - (b.aktuelle_feuchte ?? Infinity)
    })
    return {
      mitglieder: sortiert,
      gruppenRang: Math.min(...mitglieder.map(rang)),
      minFest: Math.min(...mitglieder.map(festIndex)),
      minFeuchte: Math.min(
        ...mitglieder.map(z => z.aktuelle_feuchte ?? Infinity),
      ),
    }
  })
  gruppenListe.sort((a, b) => {
    const aKrit = a.gruppenRang === 0 ? 0 : 1
    const bKrit = b.gruppenRang === 0 ? 0 : 1
    if (aKrit !== bKrit) return aKrit - bKrit
    if (aKrit === 0) return a.minFeuchte - b.minFeuchte
    if (a.minFest !== b.minFest) return a.minFest - b.minFest
    if (a.gruppenRang !== b.gruppenRang) return a.gruppenRang - b.gruppenRang
    return a.minFeuchte - b.minFeuchte
  })
  return gruppenListe.flatMap(g => g.mitglieder)
}

/* T-0221: Meta-Zeile fuer den V3-Standort-Header
   ("5 ZONEN · 2× DSWC · 1 MISCHZONE · HECKE OHNE SENSOR"). */
function standortMetaV3(zonen: Zone[]): string {
  const teile: string[] = [`${zonen.length} Zonen`]
  const dswcs = new Set(
    zonen
      .filter(z => z.ventil_kanal != null)
      .map(z => z.ventil_geraet_id ?? 'primaer'),
  )
  if (dswcs.size > 0) teile.push(`${dswcs.size}× DSWC`)
  const misch = zonen.filter(z => (z.sensoren?.length ?? 0) > 1).length
  if (misch > 0) teile.push(`${misch} Mischzone`)
  for (const z of zonen.filter(z => (z.sensoren?.length ?? 0) === 0)) {
    teile.push(`${z.name} ohne Sensor`)
  }
  return teile.join(' · ').toUpperCase()
}

export default App
