/* Manuelle Bewaesserungs-Steuerung pro Zone.
 *
 * Drei Modi:
 * - "Gegossen" (Logging): User hat manuell mit Schlauch/Kanne gegossen,
 *   wird per POST /api/giessen als Audit-Trail eingetragen. Kein Ventil-
 *   Befehl. Kurze Dauern (30s-5min) fuer Mini-Spruehrunden.
 * - T-0110 "Live starten" (echter Befehl): Ventil wirklich oeffnen
 *   per POST /api/ventil/manuell-start. Funktioniert auch bei
 *   ventilsteuerung_aktiv=false. Stop-Button stoppt sofort.
 * - T-0111 "Pre-Soak": Vorwasser + Pause + Hauptdose als orchestrierte
 *   Sequenz. Default-Dauern aus Zone-Konfig (pre_soak_min,
 *   pre_soak_pause_min). Polling /pre-soak-status zeigt Phase + Stop.
 */

import { useEffect, useRef, useState } from 'react'
import {
  loggeGiessen,
  startePreSoak,
  stoppePreSoak,
  holePreSoakStatus,
  holeVentilStatus,
  type BudgetWarnung,
  type PreSoakLauf,
  type VentilAktivEintrag,
  type VentilExternEintrag,
} from '../api'
import { API, authFetch } from '../api'
import { starteSichtbarkeitsIntervall } from '../sichtbarkeitsPolling'
import './ManuellesGiessen.css'

interface Props {
  zoneId: string
  hatVentilKanal: boolean   // nur wenn Zone an einem Kanal haengt
  preSoakDefaultMin?: number | null   // aus ZonenKonfig.pre_soak_min
  preSoakPauseMin?: number | null     // aus ZonenKonfig.pre_soak_pause_min
  // T-0169: Manuell-Logging-Einheit pro Zone (Backend-persistierter Default).
  // 'sekunden' (Schlauch) oder 'ml' (Giesskanne, FYTA-Topf). Bei 'ml' werden
  // die ml-Optionen aus `loggingOptionenMl` genutzt; sonst Sekunden-Default.
  loggingEinheit?: 'sekunden' | 'ml'
  loggingOptionenMl?: number[]
  // T-0279: Empfohlene Hauptdauer in Sekunden (z.B. Bambus 48 min = 2880).
  // Wenn > 0, rendert die Komponente einen "Übernehmen"-Block, der mit
  // 1 Klick die Live-Dauer + die Pre-Soak-Hauptdose vorbefuellt. Die
  // Pre-Soak-Anfeucht- und Pause-Felder kommen aus `preSoakDefaultMin`
  // bzw. `preSoakPauseMin` (Konfig) -- siehe Befund A der T-0279-Spec.
  empfDauerSec?: number | null
}

const LOG_DAUER_OPTIONEN = [30, 60, 120, 300]
const LOG_LITER_OPTIONEN_DEFAULT_ML = [100, 250, 500, 1000]
const LIVE_DAUER_OPTIONEN_MIN = [5, 15, 30, 60, 90]

/** Anzeige-Label fuer ml-Buttons: "100 ml" / "1 L" / "1.5 L". */
function formatMlButton(ml: number): string {
  if (ml < 1000) return `${ml} ml`
  const l = ml / 1000
  // Ganze Liter ohne Komma, sonst 1 Nachkomma.
  return Number.isInteger(l) ? `${l} L` : `${l.toFixed(1)} L`
}

type Status = 'idle' | 'sende' | 'ok' | 'fehler'

/** Phase-Label fuer Anzeige.
 *
 * T-0462: Der Default war `return 'Fehler'` -- damit hat JEDE dem Frontend
 * unbekannte Backend-Phase als Defekt ausgesehen. Genau so meldete die Karte
 * waehrend der seit T-0437 regulaeren `haupt_pause` (bei waldblumenhain rund
 * zweieinhalb Stunden pro Lauf) "Automatik: Fehler (0:00)".
 * 'Fehler' heisst jetzt nur noch, was das Backend auch so nennt; alles
 * Unbekannte wird als unbekannt beschriftet statt als kaputt. Klasse:
 * `fehlerpattern_neuer_enumwert_faellt_aus_positivfilter`.
 */
function phaseLabel(p: PreSoakLauf['phase']): string {
  if (p === 'pre_soak') return 'Vorwässern'
  if (p === 'pause') return 'Pause'
  if (p === 'haupt') return 'Hauptdose'
  // T-0437: Einsickerpause zwischen zwei Haupt-Pulsen, Ventil zu.
  if (p === 'haupt_pause') return 'Soak-Pause'
  if (p === 'fertig') return 'Fertig'
  if (p === 'stop_fehler') return 'Stop unklar'
  if (p === 'fehler') return 'Fehler'
  return `Phase ${p}`
}

/** Verbleibende Sekunden in der aktuellen Phase berechnen. */
/** Aktiv laufende Bewaesserung auf dem Kanal dieser Zone (T-0459: benannt,
 *  damit der Vergleichs-Helfer sie typisieren kann). */
type VentilLaufState =
  | { kind: 'backend'; eintrag: VentilAktivEintrag }
  | { kind: 'extern'; eintrag: VentilExternEintrag }
  | null

/** T-0459: Restzeit eines Backend-Laufs LOKAL aus `gestartet` + `dauer_s`.
 *
 * Vorher las die UI `eintrag.verbleibend_s` direkt vom Server. Das sah nur
 * deshalb fluessig aus, weil die Rueckkopplung ~35x/s gepollt hat; mit dem
 * korrekten 5-s-Raster wuerde der Countdown in 5-Sekunden-Spruengen laufen.
 * Lokal gerechnet braucht die Anzeige gar kein Polling -- der Sekunden-Tick
 * reicht. Gleiche Bauart wie `verbleibendSec` fuer Pre-Soak.
 * Weicht der Server ab (Watchdog, Verlaengerung), aendert sich `gestartet`
 * oder `dauer_s` und der naechste Poll korrigiert. */
function verbleibendBackendSec(eintrag: VentilAktivEintrag): number {
  // T-0481: Kein Null-Guard auf `dauer_s`/`gestartet`. Beide sind in
  // `VentilAktivEintrag` (api.ts) non-null, und das Backend liefert sie
  // immer: `ventil_sicherung.py` fuehrt `dauer_s: int` und schreibt
  // `gestartet` per `.astimezone().isoformat()`. Ein Guard darauf war
  // toter Code und behauptete eine Nullbarkeit, die der Vertrag nicht hat.
  // Der NaN-Zweig unten bleibt: er faengt einen unparsbaren Zeitstempel,
  // was ein anderer Fall ist als ein fehlendes Feld.
  const start = new Date(eintrag.gestartet).getTime()
  if (Number.isNaN(start)) return eintrag.verbleibend_s ?? 0
  const verstrichen = Math.floor((Date.now() - start) / 1000)
  return Math.max(0, eintrag.dauer_s - verstrichen)
}

/** T-0459: Inhaltsvergleich OHNE `verbleibend_s`.
 *
 * `verbleibend_s` faellt bei jedem Poll -- wer es mitvergleicht, erzeugt bei
 * jedem Fetch eine neue State-Referenz. Genau daraus entstand der Sturm.
 * Alles andere im Eintrag ist ueber die Laufzeit stabil. */
function gleicherVentilLauf(
  a: VentilLaufState, b: VentilLaufState,
): boolean {
  if (a === b) return true
  if (a == null || b == null) return false
  if (a.kind !== b.kind) return false
  const ohneRest = (e: Record<string, unknown>) => {
    const kopie = { ...e }
    delete kopie.verbleibend_s
    return JSON.stringify(kopie)
  }
  return ohneRest(a.eintrag as unknown as Record<string, unknown>)
    === ohneRest(b.eintrag as unknown as Record<string, unknown>)
}

/** T-0462: Puls-Raster der Haupt-Phase, spiegelt `pre_soak.py`.
 *
 * Die Formeln stehen im Backend (`pre_soak_puls_s` in `kanal_zustand.py`,
 * `haupt_puls_start_s` in `pre_soak.py`) und werden hier NUR fuer die Anzeige
 * nachgerechnet -- die Sequenz selbst steuert weiterhin allein das Backend.
 * `dauer` ist bewusst `floor` mit Untergrenze 1, exakt wie
 * `max(1, haupt_s // haupt_pulse)`; sonst laeuft der Countdown gegen eine
 * andere Zahl als die, nach der das Ventil schaltet.
 */
function pulsRaster(lauf: PreSoakLauf) {
  const anzahl = Math.max(1, lauf.haupt_pulse ?? 1)
  const pause = lauf.haupt_pause_s ?? 0
  const dauer = anzahl > 1
    ? Math.max(1, Math.floor(lauf.haupt_s / anzahl))
    : lauf.haupt_s
  // Offset ab `gestartet_am`, an dem Puls `index` (1-basiert) beginnt.
  const start = (index: number) => lauf.pause_s + (index - 1) * (dauer + pause)
  return { anzahl, pause, dauer, start }
}

function verbleibendSec(lauf: PreSoakLauf): number {
  const start = new Date(lauf.gestartet_am).getTime()
  const jetzt = Date.now()
  const verstrichen = Math.floor((jetzt - start) / 1000)
  if (lauf.phase === 'pre_soak') {
    return Math.max(0, lauf.pre_soak_s - verstrichen)
  }
  if (lauf.phase === 'pause') {
    return Math.max(0, lauf.pause_s - verstrichen)
  }
  if (lauf.phase === 'haupt') {
    // T-0462: bis zum Ende des LAUFENDEN Pulses, nicht der Gesamt-Hauptdose.
    // Vorher rechnete die Zeile `haupt_s - (verstrichen - pause_s)`, was nur
    // bei einem einzigen Puls stimmt: ab Puls 2 zaehlte sie die schon
    // gelaufenen Pulse und alle Soak-Pausen von der Restzeit ab.
    const r = pulsRaster(lauf)
    const index = Math.min(r.anzahl, Math.max(1, lauf.haupt_pulse_gestartet ?? 1))
    return Math.max(0, r.start(index) + r.dauer - verstrichen)
  }
  if (lauf.phase === 'haupt_pause') {
    // T-0462: bis zum Start des NAECHSTEN Pulses. Das Backend setzt diese
    // Phase nur, solange noch einer aussteht (`haupt_pulse_gestartet <
    // haupt_pulse`), der Index bleibt also im Raster.
    const r = pulsRaster(lauf)
    const naechster = Math.min(r.anzahl, (lauf.haupt_pulse_gestartet ?? 1) + 1)
    return Math.max(0, r.start(naechster) - verstrichen)
  }
  return 0
}

function formatMinSec(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${m}:${s.toString().padStart(2, '0')}`
}

// T-0431/T-0432-Nachzug: der Pausier-Hinweis ("<Grund> -- Empfehlung pausiert")
// lebte als CSS-`content` an der Marker-Klasse `.mg-lauf` und wurde ueber drei
// Modifier-Regeln (.mg-lauf, --automatik, --fremd) auseinandergehalten. Ein
// Zustandstext in CSS-`content` hat aber keinen Datenzugriff und kippt still
// zur Falschaussage, sobald es einen zweiten Fall gibt -- genau so wurde ein
// Automatik-Lauf als "Manuell aktiv" beschriftet. Jetzt rendert ihn jeder
// Lauf-Pfad selbst aus dem Zustand, den er ohnehin kennt; die Modifier-Kette
// entfaellt. Siehe Memory `fehlerpattern_zustand_in_css_content`.
function pausierHinweis(grund: string) {
  return <div className="mg-pausier-hinweis">{grund} — Empfehlung pausiert</div>
}

/** T-0450: Anzeige des Tagesbudget-Advisorys aus dem Backend.
 *
 *  Bewusst mit explizitem Schliessen statt Auto-Ausblenden: die uebrigen
 *  Meldungen dieser Komponente laufen ueber `status`, das nach 1.5-2 s auf
 *  'idle' zurueckfaellt und den Modus zuklappt. Eine Warnung, die genau so
 *  lange steht, waere ein zweiter Detektor ohne wirksamen Konsumenten
 *  (`fehlerpattern_detektor_ohne_konsument`) -- sie soll bis zur Kenntnis-
 *  nahme stehen bleiben.
 */
function budgetWarnBanner(
  warnung: BudgetWarnung, onSchliessen: () => void,
) {
  return (
    <div className="mg-budget-warnung" role="status">
      <span className="mg-budget-warnung__text">⚠️ {warnung.text}</span>
      <button
        className="mg-budget-warnung__schliessen"
        onClick={onSchliessen}
        title="Hinweis ausblenden"
        aria-label="Budget-Hinweis ausblenden"
      >
        ×
      </button>
    </div>
  )
}

/** T-0450: duenner Rahmen um die eigentliche Komponente.
 *
 *  Der Budget-Hinweis MUSS ausserhalb liegen: `ManuellesGiessenInner` hat
 *  mehrere fruehe `return`s (externer Lauf, Backend-Lauf, Pre-Soak-Lauf), und
 *  nach einem erfolgreichen Start greift durch das Optimistic-Update genau
 *  einer davon. Ein Banner im Formular-Zweig waere also im Moment seiner
 *  Relevanz nie zu sehen.
 */
export function ManuellesGiessen(props: Props) {
  const [budgetWarnung, setBudgetWarnung] = useState<BudgetWarnung | null>(null)
  return (
    <>
      {budgetWarnung && budgetWarnBanner(
        budgetWarnung, () => setBudgetWarnung(null),
      )}
      <ManuellesGiessenInner {...props} onBudgetWarnung={setBudgetWarnung} />
    </>
  )
}

function ManuellesGiessenInner({
  zoneId, hatVentilKanal, preSoakDefaultMin, preSoakPauseMin,
  loggingEinheit, loggingOptionenMl,
  empfDauerSec, onBudgetWarnung,
}: Props & { onBudgetWarnung: (w: BudgetWarnung | null) => void }) {
  const [modus, setModus] = useState<'zu' | 'log' | 'log_back' | 'live' | 'pre_soak'>('zu')
  const [status, setStatus] = useState<Status>('idle')
  const [fehler, setFehler] = useState<string | null>(null)

  // T-0174: rueckwirkendes Loggen — Form-State
  const [backMl, setBackMl] = useState<number>(250)
  const [backSekunden, setBackSekunden] = useState<number>(60)
  const [backDatum, setBackDatum] = useState<string>(() => {
    const d = new Date()
    // T-0565: LOKALES Datum. `toISOString()` liefert das UTC-Datum --
    // zwischen 00:00 und 02:00 Sommerzeit ist das der Vortag, und der
    // Nachtrag landete stillschweigend einen Tag zu frueh, wenn der
    // Nutzer das vorbelegte Feld nicht korrigiert.
    return [
      d.getFullYear(),
      String(d.getMonth() + 1).padStart(2, '0'),
      String(d.getDate()).padStart(2, '0'),
    ].join('-')  // YYYY-MM-DD
  })
  const [backZeit, setBackZeit] = useState<string>(() => {
    const d = new Date()
    return `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
  })

  // Pre-Soak-Form-State
  const [psPreSoak, setPsPreSoak] = useState<number>(preSoakDefaultMin ?? 5)
  const [psPause, setPsPause] = useState<number>(preSoakPauseMin ?? 30)
  const [psHaupt, setPsHaupt] = useState<number>(60)

  // Aktuelle Pre-Soak-Sequenz (gepollt)
  const [psLauf, setPsLauf] = useState<PreSoakLauf | null>(null)

  // T-0262 (2026-05-26): freie Dauer im Live-Modus. Default = 5 min,
  // wird auf jede Shadow-Empfehlung mit konkretem Dauer-Vorschlag
  // ueberschrieben (Realfall yogaraum 26.05.: Empfehlung 48 min,
  // Presets 5/15/30/60/90 hatten keinen passenden Knopf).
  const [liveFreieDauer, setLiveFreieDauer] = useState<number>(5)
  // Aktiv laufende Bewaesserung auf dem Kanal dieser Zone (gepollt).
  // Quelle: /api/ventil-status. Backend-eigen ODER extern (App/Schedule).
  const [ventilLauf, setVentilLauf] = useState<VentilLaufState>(null)
  const [ventilStatusFehler, setVentilStatusFehler] = useState<string | null>(null)
  // Tickt jede Sekunde, damit Countdown sich aktualisiert
  const [tick, setTick] = useState<number>(0)

  // Polling: Ventil-Status (zeigt Backend-eigene + externe Bewaesserungen).
  // T-0201-Perf: Adaptive Cadence -- 5 s waeren bei 14 V2-Karten = 336
  // Calls/min auf /api/ventil-status. Stattdessen idle 30 s, nur wenn
  // gerade eine Bewaesserung laeuft Cadence auf 5 s erhoehen (User
  // erwartet schnelles Stop-Button-Feedback, Countdown-Updates).
  //
  // T-0459: `ventilLauf` stand hier frueher in den Dependencies, damit die
  // Cadence umschalten kann -- und der Effekt schrieb denselben State bei
  // JEDEM Fetch als neues Objektliteral. Ergebnis: Fetch -> neue Referenz ->
  // Dependency-Aenderung -> Cleanup + Neuaufbau -> sofort wieder Fetch.
  // Gemessen 40,5 req/s statt 0,2/s. Zwei Aenderungen dagegen:
  //  (1) Die Cadence kommt aus einem Ref statt aus den Dependencies; der
  //      Effekt haengt nur noch an zoneId/hatVentilKanal.
  //  (2) `setVentilLauf` behaelt die alte Referenz, wenn sich inhaltlich
  //      nichts geaendert hat (`gleicherVentilLauf`, ohne `verbleibend_s`).
  // (1) allein macht die Schleife strukturell unmoeglich; (2) spart zusaetzlich
  // die Re-Renders und haelt den Sekunden-Tick-Effekt unten stabil.
  const laeuftRef = useRef(false)
  useEffect(() => {
    if (!hatVentilKanal) return
    const ctrl = new AbortController()
    let aktiv = true
    let timer: number | undefined
    // T-0460: waehrend fetchStatus() auf ihr await wartet, darf handleVisibility
    // keinen zweiten parallelen Zyklus anstossen -- sonst laufen zwei Timer-Ketten
    // gleichzeitig weiter und die Pollrate verdoppelt sich dauerhaft.
    let zyklusLaeuft = false
    const fetchStatus = async () => {
      try {
        const r = await holeVentilStatus(ctrl.signal)
        if (!aktiv) return
        let treffer: VentilLaufState = null
        for (const eintrag of Object.values(r.aktiv ?? {})) {
          if (eintrag.zone_ids?.includes(zoneId)) {
            treffer = { kind: 'backend', eintrag }
            break
          }
        }
        if (treffer == null) {
          for (const eintrag of Object.values(r.extern ?? {})) {
            if (eintrag.zone_ids?.includes(zoneId)) {
              treffer = { kind: 'extern', eintrag }
              break
            }
          }
        }
        laeuftRef.current = treffer != null
        setVentilLauf(vorher =>
          gleicherVentilLauf(vorher, treffer) ? vorher : treffer,
        )
        setVentilStatusFehler(null)
      } catch (e) {
        if (!aktiv) return
        const nachricht = e instanceof Error ? e.message : 'unbekannter Fehler'
        setVentilStatusFehler(`Ventilstatus unklar: ${nachricht}`)
      }
    }
    // Selbst planender Zyklus: liest die Cadence bei jedem Durchlauf neu,
    // ohne dass der Effekt dafuer neu aufgebaut werden muss. T-0465: try/finally,
    // damit ein Wurf aus fetchStatus den naechsten Timer nicht verhindert.
    // T-0460: setTimeout-Ketten sind promise-getrieben und damit immun gegen
    // Chromes Hintergrund-Timer-Drosselung (Audit-Befund B8) -- anders als bei
    // einem einfachen setInterval reicht hier ein document.hidden-Guard direkt
    // im Zyklus, plus sofortiges Nachholen beim Sichtbarwerden.
    const zyklus = async () => {
      if (document.hidden) {
        if (aktiv) timer = window.setTimeout(zyklus, 30000)
        return
      }
      zyklusLaeuft = true
      try {
        await fetchStatus()
      } finally {
        zyklusLaeuft = false
        if (aktiv) timer = window.setTimeout(zyklus, laeuftRef.current ? 5000 : 30000)
      }
    }
    const handleVisibility = () => {
      if (!document.hidden && aktiv && !zyklusLaeuft) {
        if (timer !== undefined) { window.clearTimeout(timer); timer = undefined }
        void zyklus()
      }
    }
    document.addEventListener('visibilitychange', handleVisibility)
    void zyklus()
    return () => {
      aktiv = false
      ctrl.abort()
      document.removeEventListener('visibilitychange', handleVisibility)
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [zoneId, hatVentilKanal])

  // Polling: Pre-Soak-Status. T-0201-Perf: adaptiv -- 5 s nur waehrend
  // aktivem Pre-Soak-Lauf, sonst 30 s (sonst 14 V2-Karten = 168 Calls/min
  // nur fuer einen sehr seltenen Zustand).
  const psLaeuftRef = useRef(false)
  useEffect(() => {
    if (!hatVentilKanal) return
    const ctrl = new AbortController()
    let aktiv = true
    let timer: number | undefined
    let zyklusLaeuft = false
    const fetchStatus = async () => {
      try {
        const r = await holePreSoakStatus(zoneId, ctrl.signal)
        if (!aktiv) return
        // T-0411: NICHT mehr auf `l.zone_id === zoneId` filtern. Das Backend
        // liefert zu dieser zone_id genau den Lauf, der die Zone physisch
        // betrifft -- inklusive eines Laufs der Geschwister-Zone am selben
        // Ventil-Kanal (dann `fremd: true`). Der alte Filter warf genau den
        // weg, weshalb die Yogaraum-Karte "ruhig" zeigte, waehrend auf ihrem
        // Kanal Wasser lief.
        const lauf = r.laeufe.find(
          l => l.phase !== 'fertig' && l.phase !== 'fehler'
        ) ?? null
        psLaeuftRef.current = lauf != null
        // T-0459: Referenz nur ersetzen, wenn sich inhaltlich etwas geaendert
        // hat. `PreSoakLauf` enthaelt kein laufend fallendes Feld (die Restzeit
        // rechnet `verbleibendSec` lokal aus `gestartet_am`), deshalb genuegt
        // hier der vollstaendige Vergleich.
        setPsLauf(vorher =>
          JSON.stringify(vorher) === JSON.stringify(lauf) ? vorher : lauf,
        )
      } catch {
        /* Polling-Fehler still ignorieren */
      }
    }
    // T-0459: identische Konstruktion wie oben, identischer Fix. Dieser Pfad
    // war der teurere von beiden -- gemessen 118,9 req/s -- weil ein reglaerer
    // Cycle-and-Soak-Lauf ueber Stunden in einer nicht-terminalen Phase steht
    // und die Schleife damit dauerhaft offen hielt.
    // T-0465: try/finally, damit ein Wurf aus fetchStatus den naechsten Timer
    // nicht verhindert (identischer Fix wie im Ventil-Status-Zyklus oben).
    // T-0460: gleicher Hintergrund-Guard wie oben (Audit-Befund B8 -- die
    // setTimeout-Kette ist promise-getrieben, native Timer-Drosselung greift
    // hier nicht).
    const zyklus = async () => {
      if (document.hidden) {
        if (aktiv) timer = window.setTimeout(zyklus, 30000)
        return
      }
      zyklusLaeuft = true
      try {
        await fetchStatus()
      } finally {
        zyklusLaeuft = false
        if (aktiv) timer = window.setTimeout(zyklus, psLaeuftRef.current ? 5000 : 30000)
      }
    }
    const handleVisibility = () => {
      if (!document.hidden && aktiv && !zyklusLaeuft) {
        if (timer !== undefined) { window.clearTimeout(timer); timer = undefined }
        void zyklus()
      }
    }
    document.addEventListener('visibilitychange', handleVisibility)
    void zyklus()
    return () => {
      aktiv = false
      ctrl.abort()
      document.removeEventListener('visibilitychange', handleVisibility)
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [zoneId, hatVentilKanal])

  // Sekunden-Tick fuer Countdown-Anzeige (Pre-Soak ODER laufende Bewaesserung)
  // T-0459: Deps auf Booleans reduziert. Vorher haben die Objekt-Referenzen
  // diesen Timer bei jedem Poll mit ab- und wieder aufgebaut -- gemessen
  // 98 Neuanlagen/s. Ob ein Lauf existiert, ist alles was hier zaehlt.
  const tickNoetig = psLauf != null || ventilLauf != null
  useEffect(() => {
    if (!tickNoetig) return
    const stoppeIntervall = starteSichtbarkeitsIntervall(() => setTick(t => t + 1), 1000)
    return () => stoppeIntervall()
  }, [tickNoetig])

  const sendeLog = (dauer: number) => {
    setStatus('sende')
    loggeGiessen(zoneId, dauer)
      .then(r => {
        setStatus(r.fehler ? 'fehler' : 'ok')
        if (r.fehler) setFehler(r.fehler)
        setTimeout(() => { setStatus('idle'); setModus('zu') }, 2000)
      })
      .catch(e => {
        setStatus('fehler')
        setFehler(e instanceof Error ? e.message : 'Fehler')
        setTimeout(() => setStatus('idle'), 2000)
      })
  }

  /** T-0174: Rueckwirkend mit Zeitstempel + Menge. Wahlweise ml ODER Sek.
   *  Backend akzeptiert das `zeitstempel`-Feld im /api/giessen-Endpoint. */
  const sendeLogRueckwirkend = () => {
    setStatus('sende')
    // Lokales ISO ohne Zeitzone (Backend behandelt naive datetime als lokal)
    const iso = `${backDatum}T${backZeit}:00`
    const opts =
      loggingEinheit === 'ml'
        ? { liter: backMl / 1000, zeitstempel: iso }
        : { dauer_sekunden: backSekunden, zeitstempel: iso }
    loggeGiessen(zoneId, opts)
      .then(r => {
        setStatus(r.fehler ? 'fehler' : 'ok')
        if (r.fehler) setFehler(r.fehler)
        setTimeout(() => { setStatus('idle'); setModus('zu') }, 2000)
      })
      .catch(e => {
        setStatus('fehler')
        setFehler(e instanceof Error ? e.message : 'Fehler')
        setTimeout(() => setStatus('idle'), 2000)
      })
  }

  /** T-0169: Liter-Variante (ml-Buttons). Backend rechnet Pseudo-Dauer. */
  const sendeLogLiter = (ml: number) => {
    setStatus('sende')
    loggeGiessen(zoneId, { liter: ml / 1000 })
      .then(r => {
        setStatus(r.fehler ? 'fehler' : 'ok')
        if (r.fehler) setFehler(r.fehler)
        setTimeout(() => { setStatus('idle'); setModus('zu') }, 2000)
      })
      .catch(e => {
        setStatus('fehler')
        setFehler(e instanceof Error ? e.message : 'Fehler')
        setTimeout(() => setStatus('idle'), 2000)
      })
  }

  const sendeLiveStart = async (dauerMin: number) => {
    setStatus('sende')
    setFehler(null)
    // T-0450: Hinweis des VORIGEN Laufs zuruecksetzen. Er bezieht sich auf
    // einen anderen Verbrauchsstand und wuerde sonst als Aussage ueber den
    // neuen Lauf gelesen.
    onBudgetWarnung(null)
    try {
      const r = await authFetch(`${API}/ventil/manuell-start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          zone_id: zoneId,
          dauer_sekunden: dauerMin * 60,
        }),
      })
      const daten = await r.json()
      if (!r.ok || !daten.ok) {
        setStatus('fehler')
        // T-0151: bei HAHN_BELEGT lesbaren `grund` zeigen statt Code.
        if (daten.fehler === 'HAHN_BELEGT' && daten.grund) {
          const aktiv = (daten.aktive_zonen as string[] | undefined)?.join(', ')
          setFehler(
            aktiv
              ? `Hahn belegt durch ${aktiv}. ${daten.grund}`
              : daten.grund,
          )
        } else {
          setFehler(daten.fehler ?? `HTTP ${r.status}`)
        }
        setTimeout(() => setStatus('idle'), 4500)
        return
      }
      setStatus('ok')
      // T-0450: Tagesbudget-Advisory des Backends anzeigen. Fehlt das Feld,
      // gab es keine Ueberschreitung.
      if (daten.budget_warnung) {
        onBudgetWarnung(daten.budget_warnung as BudgetWarnung)
      }
      // T-0201-Folge: Optimistic-Update -- ventilLauf direkt setzen,
      // damit Lauf-Banner sofort erscheint. Sonst muesste User auf
      // den naechsten 30 s-Idle-Poll warten, bevor sichtbar wird, dass
      // die Bewaesserung tatsaechlich laeuft.
      setVentilLauf({
        kind: 'backend',
        eintrag: {
          zone_ids: [zoneId],
          dauer_s: dauerMin * 60,
          gestartet: new Date().toISOString(),
          verbleibend_s: dauerMin * 60,
          ausloser: 'manuell',
          quelle: 'backend',
          stoppbar: true,
        },
      })
      setTimeout(() => { setStatus('idle'); setModus('zu') }, 2000)
    } catch (e) {
      setStatus('fehler')
      setFehler(e instanceof Error ? e.message : 'Fehler')
      setTimeout(() => setStatus('idle'), 3000)
    }
  }

  const sendeLiveStop = async () => {
    setStatus('sende')
    setFehler(null)
    try {
      const r = await authFetch(`${API}/ventil/manuell-stop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ zone_id: zoneId }),
      })
      const daten = await r.json()
      if (!r.ok || !daten.ok) {
        setStatus('fehler')
        setFehler(daten.fehler ?? `HTTP ${r.status}`)
        setTimeout(() => setStatus('idle'), 3000)
        return
      }
      setStatus('ok')
      // Safety: nach Stop nicht optimistisch loeschen. Erst ein frischer
      // Status darf sagen, ob die Zone wirklich nicht mehr aktiv ist.
      try {
        const statusNeu = await holeVentilStatus()
        let naechsterLauf: typeof ventilLauf = null
        for (const eintrag of Object.values(statusNeu.aktiv ?? {})) {
          if (eintrag.zone_ids?.includes(zoneId)) {
            naechsterLauf = { kind: 'backend', eintrag }
            break
          }
        }
        if (naechsterLauf == null) {
          for (const eintrag of Object.values(statusNeu.extern ?? {})) {
            if (eintrag.zone_ids?.includes(zoneId)) {
              naechsterLauf = { kind: 'extern', eintrag }
              break
            }
          }
        }
        setVentilLauf(naechsterLauf)
        setVentilStatusFehler(null)
        if (naechsterLauf != null) {
          setStatus('fehler')
          setFehler('Ventil meldet nach Stop weiterhin aktiv')
          setTimeout(() => setStatus('idle'), 4000)
          return
        }
      } catch (e) {
        const nachricht = e instanceof Error ? e.message : 'unbekannter Fehler'
        setVentilStatusFehler(`Ventilstatus nach Stop unklar: ${nachricht}`)
        setStatus('fehler')
        setFehler(`Ventilstatus nach Stop unklar: ${nachricht}`)
        setTimeout(() => setStatus('idle'), 4000)
        return
      }
      setTimeout(() => { setStatus('idle'); setModus('zu') }, 1500)
    } catch (e) {
      setStatus('fehler')
      setFehler(e instanceof Error ? e.message : 'Fehler')
      setTimeout(() => setStatus('idle'), 3000)
    }
  }

  const sendePreSoakStart = async () => {
    setStatus('sende')
    setFehler(null)
    onBudgetWarnung(null)   // T-0450, s. sendeLiveStart
    try {
      const r = await startePreSoak(zoneId, psPreSoak, psPause, psHaupt)
      if (!r.ok) {
        setStatus('fehler')
        // T-0151: HAHN_BELEGT mit Klartext.
        if (r.fehler === 'HAHN_BELEGT' && r.grund) {
          const aktiv = r.aktive_zonen?.join(', ')
          setFehler(
            aktiv
              ? `Hahn belegt durch ${aktiv}. ${r.grund}`
              : r.grund,
          )
        } else {
          setFehler(r.fehler ?? 'Pre-Soak-Start fehlgeschlagen')
        }
        setTimeout(() => setStatus('idle'), 4500)
        return
      }
      setStatus('ok')
      // T-0450: Advisory analog Live-Start. Geplant ist hier die WASSERZEIT
      // (Vorwaesser + Hauptdose), die Soak-Pause zaehlt nicht mit -- die
      // Rechnung macht das Backend, hier wird nur angezeigt.
      if (r.budget_warnung) onBudgetWarnung(r.budget_warnung)
      // T-0240: Optimistic-Update analog Live-Start. Ohne diesen Stub
      // zeigt das Pre-Soak-Banner erst nach dem naechsten Polling-Tick
      // (idle 30 s) -- User sieht zwischen Klick und Banner nichts.
      // `kanal: 0` ist ein Platzhalter, der beim naechsten 5-s-Polling
      // (cadence schaltet auf 5000 ms sobald psLauf != null) durch den
      // echten Wert ersetzt wird. UI-Renderer (phaseLabel / verbleibendSec)
      // nutzt `kanal` nicht. Backend liefert in `startePreSoak`-Response
      // keine Lauf-Felder, deshalb der Stub.
      setPsLauf({
        zone_id: zoneId,
        kanal: 0,
        phase: 'pre_soak',
        pre_soak_s: psPreSoak * 60,
        pause_s: psPause * 60,
        haupt_s: psHaupt * 60,
        gestartet_am: new Date().toISOString(),
        fehler: null,
      })
      setTimeout(() => { setStatus('idle'); setModus('zu') }, 1500)
    } catch (e) {
      setStatus('fehler')
      setFehler(e instanceof Error ? e.message : 'Fehler')
      setTimeout(() => setStatus('idle'), 3000)
    }
  }

  const sendePreSoakStop = async () => {
    setStatus('sende')
    setFehler(null)
    try {
      const r = await stoppePreSoak(zoneId)
      if (!r.ok) {
        setStatus('fehler')
        setFehler(r.fehler ?? 'Pre-Soak-Stop fehlgeschlagen')
        setTimeout(() => setStatus('idle'), 3000)
        return
      }
      setPsLauf(null)
      setStatus('ok')
      setTimeout(() => setStatus('idle'), 1500)
    } catch (e) {
      setStatus('fehler')
      setFehler(e instanceof Error ? e.message : 'Fehler')
      setTimeout(() => setStatus('idle'), 3000)
    }
  }

  // T-0279: Empfehlungs-Übernehmen-Block. Wird im Live- und im Pre-Soak-
  // Modus oberhalb der Inputs gerendert, wenn die Engine eine Hauptdauer
  // empfiehlt. Klick belegt Live-Dauer + Pre-Soak-Hauptdose mit der
  // Empfehlungs-Minutenzahl; Anfeuchten/Pause bleiben bei den Konfig-
  // Defaults (psPreSoak/psPause sind schon initialisiert).
  const empfMin = empfDauerSec != null && empfDauerSec > 0
    ? Math.max(1, Math.round(empfDauerSec / 60))
    : null
  const uebernehmeEmpfehlung = () => {
    if (empfMin == null) return
    setLiveFreieDauer(Math.min(180, empfMin))
    setPsHaupt(Math.min(180, empfMin))
  }
  const renderUebernehmenBanner = () => {
    if (empfMin == null) return null
    return (
      <div className="giessen-empf-uebernehmen" role="group" aria-label="Empfehlung übernehmen">
        <span className="giessen-label">Empfehlung: {empfMin} min</span>
        <button
          className="giessen-dauer giessen-dauer-uebernehmen"
          onClick={uebernehmeEmpfehlung}
          disabled={status === 'sende'}
          title={`Live-Dauer und Pre-Soak-Hauptdose auf ${empfMin} min setzen`}
        >
          Übernehmen
        </button>
      </div>
    )
  }

  // Wenn eine externe Bewaesserung laeuft (App/Schedule): Banner mit
  // explizitem Stop-Button. Stop ist NUR auf User-Klick (kein Auto-Stop)
  // und schliesst das Ventil ueber die normale Gardena-API. Pre-Soak/
  // Backend-Live haben Vorrang (werden in eigenen Bloecken unten angezeigt).
  if (ventilLauf && ventilLauf.kind === 'extern' && !psLauf) {
    void tick
    const seit = ventilLauf.eintrag.gestartet
      ? new Date(ventilLauf.eintrag.gestartet).toLocaleTimeString(
          'de-DE', { hour: '2-digit', minute: '2-digit' },
        )
      : '?'
    return (
      <>
        {pausierHinweis('App-Bewässerung aktiv')}
        <div className="giessen-container giessen-auswahl-live mg-lauf">
          <span className="giessen-label">
            💧 Bewässerung läuft (App, seit {seit})
          </span>
          <button
            className="giessen-stop"
            onClick={() => void sendeLiveStop()}
            disabled={status === 'sende'}
            title="Ventil sofort stoppen (auch wenn die App es gestartet hat)"
          >
            Stop
          </button>
        </div>
      </>
    )
  }

  // Backend-eigene Live-Bewaesserung (kein Pre-Soak): Banner mit Countdown +
  // Stop-Button. Wir nutzen den vorhandenen manuell-stop-Endpoint.
  if (ventilLauf && ventilLauf.kind === 'backend' && !psLauf) {
    void tick
    // T-0459: lokal gerechnet, nicht mehr direkt aus der Server-Antwort --
    // sonst springt der Countdown im 5-s-Raster (siehe verbleibendBackendSec).
    const verbleibend = verbleibendBackendSec(ventilLauf.eintrag)
    const seit = new Date(ventilLauf.eintrag.gestartet).toLocaleTimeString(
      'de-DE', { hour: '2-digit', minute: '2-digit' },
    )
    // T-0431 (Isomorphie-Check): dieselbe Fehlerklasse wie beim Pre-Soak --
    // der Ausloeser bestimmt die Beschriftung, nicht die blosse Anwesenheit
    // eines Laufs. Er liegt hier schon vor (`VentilAktivEintrag.ausloser`,
    // api_server.py:2727), wurde nur nicht gelesen.
    const istAutomatik = ventilLauf.eintrag.ausloser === 'automatik'
    // T-0500: ab `verbleibend <= 0` weiss die Karte NICHT, ob geschlossen
    // wurde -- es gibt kein Schliessen-Event, sonst waere der Lauf weg.
    // "noch 0:00" waere daher keine Information, sondern die Behauptung
    // "laeuft noch, gleich fertig". Realfall 02.08.: Laptop-Schlaf fror den
    // Schliess-Watchdog ein ([[fehlerpattern_asyncio_timer_laptop_sleep]]),
    // die Karte zeigte den Lauf 13 Minuten nach seinem Ende als aktiv.
    // Dritter Fall der Klasse "Zustand aus Anwesenheit abgeleitet statt
    // gelesen" (T-0431, T-0432).
    const abgelaufen = verbleibend <= 0
    return (
      <>
        {pausierHinweis(
          abgelaufen
            ? 'Schließen unbestätigt'
            : istAutomatik ? 'Automatik gießt' : 'Manuell aktiv',
        )}
        <div className="giessen-container giessen-auswahl-live mg-lauf">
          <span className="giessen-label">
            {abgelaufen
              ? `⏳ Dauer abgelaufen (seit ${seit}), Schließen unbestätigt`
              : `💧 ${istAutomatik ? 'Automatik gießt' : 'Bewässerung läuft'} (seit ${seit}, noch ${formatMinSec(verbleibend)})`}
          </span>
          <button
            className="giessen-stop"
            onClick={() => void sendeLiveStop()}
            disabled={status === 'sende'}
            title={abgelaufen
              ? 'Zustand aufräumen: schickt ein Schließen an das Ventil (gefahrlos, auch wenn es bereits zu ist)'
              : 'Ventil sofort stoppen'}
          >
            {abgelaufen ? 'Aufräumen' : 'Stop'}
          </button>
        </div>
      </>
    )
  }

  // Wenn eine Pre-Soak-Sequenz laeuft, immer Status + Stop anzeigen,
  // unabhaengig vom modus-Local-State. Tick-Bezug fuer Countdown.
  if (psLauf) {
    const sec = verbleibendSec(psLauf)
    void tick // Re-Render-Trigger
    // T-0346: Countdown ist echtzeit, der Phasenwechsel kommt vom Backend-Tick.
    // Beim Erreichen von 0 in einer Zeitphase nicht ein eingefrorenes "(0:00)"
    // zeigen (sieht nach Haenger aus), sondern "(…)" -> die Aktion feuert mit
    // dem feinen Pre-Soak-Ticker auf die Sekunde, die Karte folgt beim naechsten
    // Poll (<=5s). Endphasen (stop_fehler) behalten ihr Label ohne Countdown.
    const istZeitphase = (
      psLauf.phase === 'pre_soak'
      || psLauf.phase === 'pause'
      || psLauf.phase === 'haupt'
      // T-0462: die Soak-Pause laeuft ebenfalls gegen eine Uhr (bis zum
      // naechsten Puls) und gehoert damit in dieselbe Behandlung.
      || psLauf.phase === 'haupt_pause'
    )
    const countdownText = (sec === 0 && istZeitphase) ? '…' : formatMinSec(sec)
    // T-0437/T-0462: bei Cycle-and-Soak sagt die Phase allein nicht, wo im Lauf
    // man steht -- "Soak-Pause 12:30" waere bei drei Pulsen zweimal dieselbe
    // Anzeige. `status_dict()` liefert die Felder seit T-0437 genau dafuer.
    const pulsText = (
      (psLauf.haupt_pulse ?? 1) > 1
      && (psLauf.phase === 'haupt' || psLauf.phase === 'haupt_pause')
    )
      ? ` · Puls ${Math.min(
          psLauf.haupt_pulse ?? 1,
          Math.max(1, psLauf.haupt_pulse_gestartet ?? 1),
        )}/${psLauf.haupt_pulse}`
      : ''
    // T-0411: Lauf einer Geschwister-Zone am selben Ventil-Kanal. Das Wasser
    // laeuft auch hier, die Sequenz gehoert aber der anderen Zone -> read-only
    // anzeigen und benennen, statt sie als eigene auszugeben. Kein Abbrechen-
    // Button: `pre-soak-stop` ist zone-gekeyt und wuerde hier mit "Keine
    // laufende Sequenz" scheitern -- die Steuerung sitzt auf der Karte der
    // startenden Zone (bzw. im Notfall-Stopp).
    if (psLauf.fremd) {
      return (
        <>
          {pausierHinweis('Geteilter Ventil-Kanal aktiv')}
          <div className="giessen-container giessen-auswahl-live mg-lauf mg-lauf--fremd">
            <span className="giessen-label">
              Kanal {psLauf.kanal} giesst ({psLauf.zone_id}):{' '}
              {phaseLabel(psLauf.phase)} ({countdownText}){pulsText}
            </span>
          </div>
        </>
      )
    }
    // T-0431: der Ausloeser bestimmt die Beschriftung. Er steht seit T-0343 in
    // `pre_soak_state.ausloser` und kommt seither ueber `status_dict()` mit.
    const istAutomatik = psLauf.ausloser === 'automatik'
    // T-0432: waehrend einer laufenden Sequenz ist die Hauptdosis
    // FESTGESCHRIEBEN (`haupt_s`, gesetzt beim Start). Sie hier zeigen,
    // damit die Karte nicht eine inzwischen neu gerechnete Zahl behauptet.
    const committetMin = psLauf.haupt_s ? Math.round(psLauf.haupt_s / 60) : null
    return (
      <>
        {pausierHinweis(istAutomatik ? 'Automatik gießt' : 'Manuell aktiv')}
        <div className="giessen-container giessen-auswahl-live mg-lauf">
          <span className="giessen-label">
            {istAutomatik ? 'Automatik' : 'Pre-Soak'}: {phaseLabel(psLauf.phase)} ({countdownText}){pulsText}
            {committetMin != null && (
              <span className="mg-lauf__committed">
                {' '}· Hauptdosis {committetMin} min (festgelegt)
              </span>
            )}
          </span>
          <button
            className="giessen-stop"
            onClick={() => void sendePreSoakStop()}
            disabled={status === 'sende'}
            title="Sequenz abbrechen + Ventil sofort schließen"
          >
            Abbrechen
          </button>
        </div>
      </>
    )
  }

  if (ventilStatusFehler && hatVentilKanal) {
    return (
      <>
        {pausierHinweis('Ventil-Status unklar')}
        <div className="giessen-container giessen-auswahl-live mg-lauf">
          <span className="giessen-label">{ventilStatusFehler}</span>
          <button
            className="giessen-stop"
            onClick={() => void sendeLiveStop()}
            disabled={status === 'sende'}
            title="Sicherheits-Stop versuchen, weil der frische Status unklar ist"
          >
            Stop versuchen
          </button>
        </div>
      </>
    )
  }

  if (status === 'ok') return <span className="giessen-ok">OK</span>
  if (status === 'fehler') {
    return (
      <span className="giessen-fehler" title={fehler ?? ''}>
        Fehler{fehler ? `: ${fehler}` : ''}
      </span>
    )
  }

  if (modus === 'zu') {
    return (
      <div className="giessen-container">
        <button
          className="giessen-btn giessen-log-btn"
          onClick={() => setModus('log')}
          disabled={status === 'sende'}
          title="Eintrag im Log (kein Ventil-Befehl)"
        >
          Gegossen
        </button>
        {hatVentilKanal && (
          <>
            <button
              className="giessen-btn giessen-live-btn"
              onClick={() => setModus('live')}
              disabled={status === 'sende'}
              title="Echter Live-Befehl ans Ventil"
            >
              Live starten
            </button>
            <button
              className="giessen-btn giessen-live-btn"
              onClick={() => setModus('pre_soak')}
              disabled={status === 'sende'}
              title="Vorwässern + Pause + Hauptdose als Sequenz"
            >
              Pre-Soak
            </button>
          </>
        )}
      </div>
    )
  }

  if (modus === 'log') {
    // T-0169: Liter-Variante wenn Zone auf logging_einheit='ml' steht.
    // Default: Sekunden (Schlauch-Bewaesserung).
    const istMl = loggingEinheit === 'ml'
    if (istMl) {
      const optionen = (loggingOptionenMl && loggingOptionenMl.length > 0)
        ? loggingOptionenMl
        : LOG_LITER_OPTIONEN_DEFAULT_ML
      return (
        <div className="giessen-auswahl">
          <span className="giessen-label">Logging:</span>
          {optionen.map(ml => (
            <button
              key={ml}
              className="giessen-dauer"
              onClick={() => sendeLogLiter(ml)}
              disabled={status === 'sende'}
            >
              {formatMlButton(ml)}
            </button>
          ))}
          <button
            className="giessen-dauer"
            onClick={() => setModus('log_back')}
            disabled={status === 'sende'}
            title="Rueckwirkend mit anderem Zeitpunkt + freier Menge loggen"
          >
            ↶ rückwirkend
          </button>
          <button className="giessen-abbruch" onClick={() => setModus('zu')}>×</button>
        </div>
      )
    }
    return (
      <div className="giessen-auswahl">
        <span className="giessen-label">Logging:</span>
        {LOG_DAUER_OPTIONEN.map(d => (
          <button
            key={d}
            className="giessen-dauer"
            onClick={() => sendeLog(d)}
            disabled={status === 'sende'}
          >
            {d < 60 ? `${d}s` : `${d / 60}m`}
          </button>
        ))}
        <button
          className="giessen-dauer"
          onClick={() => setModus('log_back')}
          disabled={status === 'sende'}
          title="Rueckwirkend mit anderem Zeitpunkt + freier Menge loggen"
        >
          ↶ rückwirkend
        </button>
        <button className="giessen-abbruch" onClick={() => setModus('zu')}>×</button>
      </div>
    )
  }

  // T-0174: Rueckwirkende Form mit Datum + Zeit + freier Menge.
  if (modus === 'log_back') {
    const istMl = loggingEinheit === 'ml'
    return (
      <div className="giessen-auswahl">
        <span className="giessen-label">↶ Rückwirkend:</span>
        <label className="giessen-label">
          Datum:
          <input
            type="date"
            value={backDatum}
            onChange={e => setBackDatum(e.target.value)}
            disabled={status === 'sende'}
            style={{ marginLeft: 4, marginRight: 8 }}
          />
        </label>
        <label className="giessen-label">
          Zeit:
          <input
            type="time"
            value={backZeit}
            onChange={e => setBackZeit(e.target.value)}
            disabled={status === 'sende'}
            style={{ marginLeft: 4, marginRight: 8 }}
          />
        </label>
        {istMl ? (
          <label className="giessen-label">
            Menge:
            <input
              type="number"
              min={1}
              max={5000}
              step={50}
              value={backMl}
              onChange={e => setBackMl(Math.max(1, Number(e.target.value) || 0))}
              disabled={status === 'sende'}
              style={{ width: 70, marginLeft: 4, marginRight: 4 }}
            />ml
          </label>
        ) : (
          <label className="giessen-label">
            Dauer:
            <input
              type="number"
              min={1}
              max={3600}
              step={30}
              value={backSekunden}
              onChange={e => setBackSekunden(Math.max(1, Number(e.target.value) || 0))}
              disabled={status === 'sende'}
              style={{ width: 70, marginLeft: 4, marginRight: 4 }}
            />s
          </label>
        )}
        <button
          className="giessen-dauer"
          onClick={sendeLogRueckwirkend}
          disabled={status === 'sende'}
        >
          Speichern
        </button>
        <button className="giessen-abbruch" onClick={() => setModus('zu')}>×</button>
      </div>
    )
  }

  if (modus === 'live') {
    // T-0262: freie Dauer-Eingabe neben den Presets. 1-180 min.
    // Sanitize: nicht-numerische Eingabe -> 1, Range-Clamp.
    const liveFreieDauerValid = Math.max(1, Math.min(180, liveFreieDauer))
    // T-0262-Folge (2026-05-26): zwei Sub-Gruppen, sodass die freie
    // Eingabe + Start als Block zusammenbleibt wenn die Zeile in V3-
    // Karten (~360 px) umbricht. Sonst landeten Trenner, Input, "min"
    // und Start isoliert hinter den Presets.
    return (
      <div className="giessen-auswahl giessen-auswahl-live">
        {renderUebernehmenBanner()}
        <div className="giessen-live-presets">
          <span className="giessen-label">Live:</span>
          {LIVE_DAUER_OPTIONEN_MIN.map(min => (
            <button
              key={min}
              className="giessen-dauer giessen-dauer-live"
              onClick={() => void sendeLiveStart(min)}
              disabled={status === 'sende'}
            >
              {min} min
            </button>
          ))}
        </div>
        <div className="giessen-live-frei">
          <input
            type="number"
            min={1}
            max={180}
            step={1}
            value={liveFreieDauer}
            onChange={e => setLiveFreieDauer(Math.max(1, Math.min(180, Number(e.target.value) || 1)))}
            disabled={status === 'sende'}
            className="giessen-dauer-input"
            aria-label="Freie Dauer in Minuten"
            title="Beliebige Dauer in Minuten (1-180), z.B. exakte Empfehlungs-Dauer"
          />
          <span className="giessen-label" aria-hidden>min</span>
          <button
            className="giessen-dauer giessen-dauer-live"
            onClick={() => void sendeLiveStart(liveFreieDauerValid)}
            disabled={status === 'sende'}
            title={`Bewässerung für ${liveFreieDauerValid} min starten`}
          >
            Start
          </button>
          <button
            className="giessen-stop"
            onClick={() => void sendeLiveStop()}
            disabled={status === 'sende'}
            title="Laufendes Ventil sofort stoppen"
          >
            Stop
          </button>
          <button className="giessen-abbruch" onClick={() => setModus('zu')}>×</button>
        </div>
      </div>
    )
  }

  // modus === 'pre_soak'
  return (
    <div className="giessen-auswahl giessen-auswahl-live">
      {renderUebernehmenBanner()}
      <span className="giessen-label">Pre-Soak:</span>
      <label className="giessen-label">
        Vor:
        <input
          type="number"
          min={1}
          max={30}
          value={psPreSoak}
          onChange={e => setPsPreSoak(Math.max(1, Number(e.target.value) || 0))}
          disabled={status === 'sende'}
          style={{ width: 40, marginLeft: 4, marginRight: 4 }}
        />min
      </label>
      <label className="giessen-label">
        Pause:
        <input
          type="number"
          min={1}
          max={120}
          value={psPause}
          onChange={e => setPsPause(Math.max(1, Number(e.target.value) || 0))}
          disabled={status === 'sende'}
          style={{ width: 40, marginLeft: 4, marginRight: 4 }}
        />min
      </label>
      <label className="giessen-label">
        Haupt:
        <input
          type="number"
          min={1}
          max={240}
          value={psHaupt}
          onChange={e => setPsHaupt(Math.max(1, Number(e.target.value) || 0))}
          disabled={status === 'sende'}
          style={{ width: 40, marginLeft: 4, marginRight: 4 }}
        />min
      </label>
      <button
        className="giessen-dauer giessen-dauer-live"
        onClick={() => void sendePreSoakStart()}
        disabled={status === 'sende' || psPause < psPreSoak}
        title={
          psPause < psPreSoak
            ? 'Pause muss >= Vor-Dauer sein'
            : 'Sequenz starten'
        }
      >
        Start
      </button>
      <button className="giessen-abbruch" onClick={() => setModus('zu')}>×</button>
    </div>
  )
}
