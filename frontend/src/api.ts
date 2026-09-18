/* Zentraler API-Zugriff — typisierte Fetch-Wrapper. */

import type {
  BilanzFenster,
  DashboardSnapshot,
  GiessEmpfehlung,
  SnapshotFenster,
  Standort,
  Zone,
  Prognose,
  MLStatus,
  Wetter,
  Messwert,
  MLVorhersage,
  Entscheidung,
  GiessLauf,
  SchwellenVorschlag,
  VentilEreignis,
  VentilEreignisDetail,
  VentilEreignisPatch,
  OpsSeverityFilter,
  OpsSummary,
  OpsTimelineAntwort,
  Betriebsstatus,
  DauerDriftAntwort,
  EmpfehlungsAuditAntwort,
  PflegeErinnerung,
  Tagesplan,
  WartungsFenster,
  WasserBilanz,
  WasserBilanzFehler,
  MlDriftAntwort,
  MlDriftLogAntwort,
} from './typen'

export const API = import.meta.env.DEV ? 'http://127.0.0.1:8090/api' : '/api'

// T-0140: API-Key fuer den Auth-Header `X-Api-Key`. Runtime-Override
// aus `localStorage['pflanzen_api_key']` (User kann den Key per UI bzw.
// devtool setzen, ohne Rebuild). Fallback aus Vite-Var `VITE_API_KEY`
// in `frontend/.env.local` -- diese Datei greift in `vite dev` UND
// `vite build`. Achtung: `.env.development.local` waere ein Bug, weil
// vite build (Production-Mode) sie ignoriert und der Key dann nicht
// im Bundle landet. Falls beides leer ist, wird der Header weggelassen
// -- Backend antwortet dann mit 401, was die UI als klare Auth-Fehler-
// meldung anzeigen soll.
// T-0253: exportiert, damit `AuthDialog` denselben Lookup-Pfad
// (localStorage + VITE_API_KEY-Fallback) nutzt und sich nicht zeigt,
// wenn der Build den Key schon eingebrannt hat.
export function holeApiKey(): string | null {
  try {
    const ls = window.localStorage.getItem('pflanzen_api_key')
    if (ls) return ls
  } catch {
    /* SSR / privacy mode: localStorage nicht verfuegbar */
  }
  const vite = import.meta.env.VITE_API_KEY as string | undefined
  return vite && vite.length > 0 ? vite : null
}

/**
 * fetch()-Wrapper, der den `X-Api-Key`-Header automatisch setzt
 * (T-0140). Komponenten, die `fetch` direkt brauchen (z. B. weil sie
 * den Status-Code selbst auswerten oder den Body als Stream lesen),
 * MUESSEN diesen Wrapper statt globalem `fetch` verwenden -- sonst
 * laeuft die Anfrage ohne Header und kassiert 401. URL-Argument ist
 * vollstaendig (mit `${API}`-Praefix), damit dieselbe Signatur wie
 * `fetch` verwendet werden kann.
 */
export async function authFetch(url: string, init?: RequestInit): Promise<Response> {
  const apiKey = holeApiKey()
  const headers = new Headers(init?.headers)
  if (apiKey && !headers.has('X-Api-Key')) headers.set('X-Api-Key', apiKey)
  return fetch(url, { ...init, headers })
}

/**
 * Wrapper um fetch(): prueft HTTP-Status und parst JSON.
 * Wirft Error bei !r.ok — sonst wird ein 500-Fehler vom Backend
 * stumm als "leere Daten" interpretiert.
 *
 * Alle Funktionen akzeptieren ein optionales AbortSignal, damit Aufrufer
 * in useEffect-Cleanups in-flight Requests abbrechen koennen.
 *
 * T-0140: setzt automatisch `X-Api-Key` aus `holeApiKey()`.
 */
// T-0212c: Exponential-Backoff bei HTTP 429. Anders als 5xx ist 429
// "die Anfrage ist nicht durchgekommen" -- der Server hat sie wegen
// Rate-Limit verworfen. Auch fuer nicht-idempotente Methoden sicher
// zu retryen, weil **nichts passiert ist**.
//
// Strategie: max 3 Versuche, Backoff 500ms / 1500ms / 4500ms (Faktor 3),
// plus +/- 30% Jitter. Honor `Retry-After`-Header wenn Server ihn
// setzt (Sekunden-Zahl oder HTTP-Datum). Bei einem aufeinanderfolgenden
// 429-Storm dauert die Retry-Kaskade ca. 6.5 s -- mehr als der
// Rate-Limit-Bucket (5 calls / 60 s) im T-0140-Auth-Layer ausgleicht.
const RATE_LIMIT_MAX_RETRIES = 3
const RATE_LIMIT_BASE_DELAY_MS = 500
const RATE_LIMIT_BACKOFF_FAKTOR = 3

function parseRetryAfter(header: string | null): number | null {
  if (!header) return null
  const num = Number(header)
  if (Number.isFinite(num) && num >= 0) {
    return Math.min(num * 1000, 30_000)  // Cap auf 30s
  }
  const date = Date.parse(header)
  if (!Number.isNaN(date)) {
    return Math.max(0, Math.min(date - Date.now(), 30_000))
  }
  return null
}

function rateLimitDelayMs(versuch: number, retryAfter: string | null): number {
  const fromHeader = parseRetryAfter(retryAfter)
  if (fromHeader !== null) return fromHeader
  const basis = RATE_LIMIT_BASE_DELAY_MS * Math.pow(RATE_LIMIT_BACKOFF_FAKTOR, versuch)
  const jitter = basis * 0.3 * (Math.random() * 2 - 1)
  return Math.round(basis + jitter)
}

async function jsonRequest<T>(pfad: string, init?: RequestInit): Promise<T> {
  // GET-Requests duerfen bei Netzwerk-Flakiness 1x neu versucht werden.
  // Nicht-GET (POST/PATCH/DELETE) NICHT retryen — diese sind nicht
  // zwingend idempotent (z. B. loggeGiessen doppelt -> doppelter Event).
  const methode = (init?.method ?? 'GET').toUpperCase()
  const idempotent = methode === 'GET'

  const apiKey = holeApiKey()
  const headers = new Headers(init?.headers)
  if (apiKey) headers.set('X-Api-Key', apiKey)
  const initMitAuth: RequestInit = { ...init, headers }

  let letzterFehler: unknown = null
  let rateLimitVersuch = 0
  const maxVersuche = idempotent ? 2 : 1
  // Eigene Schleife, weil 429-Retries unabhaengig von GET/POST sind.
  // Aussere Schleife: Idempotenz-Retry (Netzwerk-Glitches, 5xx).
  // Innere Schleife: 429-Retry mit exponential-backoff.
  for (let versuch = 0; versuch < maxVersuche; versuch++) {
    try {
      let r = await fetch(`${API}${pfad}`, initMitAuth)
      // T-0212c: 429-Schleife _separat_ vom idempotent-Retry, damit
      // POST/PATCH/DELETE im 429-Fall auch mehrere Anlaeufe bekommen.
      while (r.status === 429 && rateLimitVersuch < RATE_LIMIT_MAX_RETRIES) {
        const delay = rateLimitDelayMs(
          rateLimitVersuch, r.headers.get('Retry-After'),
        )
        await new Promise(res => setTimeout(res, delay))
        rateLimitVersuch++
        r = await fetch(`${API}${pfad}`, initMitAuth)
      }
      if (!r.ok) {
        // 5xx kann ein temporaerer Server-Glitch sein -> retry;
        // 4xx ist unser Fehler -> kein retry (429 wurde oben behandelt).
        if (idempotent && r.status >= 500 && versuch === 0) {
          await new Promise(res => setTimeout(res, 500))
          continue
        }
        let detail = ''
        try {
          const body = await r.text()
          if (body) detail = `: ${body.slice(0, 200)}`
        } catch {
          /* body nicht lesbar */
        }
        throw new Error(`API ${pfad} → HTTP ${r.status}${detail}`)
      }
      return r.json() as Promise<T>
    } catch (e) {
      // Abort nie retryen — Aufrufer hat Controller.abort() aufgerufen.
      if (e instanceof DOMException && e.name === 'AbortError') throw e
      letzterFehler = e
      if (idempotent && versuch === 0) {
        await new Promise(res => setTimeout(res, 500))
        continue
      }
      throw e
    }
  }
  throw letzterFehler instanceof Error ? letzterFehler : new Error(String(letzterFehler))
}

export async function holeStandorte(signal?: AbortSignal): Promise<Standort[]> {
  return jsonRequest<Standort[]>('/standorte', { signal })
}

export async function holeZonen(signal?: AbortSignal): Promise<Zone[]> {
  return jsonRequest<Zone[]>('/zonen', { signal })
}

export async function holePrognosen(signal?: AbortSignal): Promise<Prognose[]> {
  return jsonRequest<Prognose[]>('/prognose', { signal })
}

export async function holeMLStatus(signal?: AbortSignal): Promise<MLStatus> {
  return jsonRequest<MLStatus>('/ml/status', { signal })
}

export async function holeWetter(standortId: string, signal?: AbortSignal): Promise<Wetter> {
  return jsonRequest<Wetter>(`/wetter/${encodeURIComponent(standortId)}`, { signal })
}

export async function holeMesswerte(zoneId: string, stunden = 48, signal?: AbortSignal): Promise<Messwert[]> {
  return jsonRequest<Messwert[]>(
    `/zonen/${encodeURIComponent(zoneId)}/messwerte?stunden=${stunden}`,
    { signal },
  )
}

/** T-0184: Sensor-Verlauf in einem expliziten Zeitfenster (fuer
 *  DetailsDrawer ±2h-Sicht um ein Ventil-Event). */
export async function holeMesswerteRange(
  zoneId: string,
  von: string,
  bis: string,
  signal?: AbortSignal,
): Promise<Messwert[]> {
  const params = new URLSearchParams({ von, bis })
  return jsonRequest<Messwert[]>(
    `/zonen/${encodeURIComponent(zoneId)}/messwerte?${params.toString()}`,
    { signal },
  )
}

export async function holeMLVorhersage(
  zoneId: string,
  signal?: AbortSignal,
  details: boolean = false,
): Promise<Record<string, MLVorhersage>> {
  // `details=top_features` liefert SHAP-Beitraege (T-0040). Dashboard-Polling
  // ruft es OHNE details (Performance), Drawer-Expand laedt on-demand MIT.
  const suffix = details ? '?details=top_features' : ''
  const d = await jsonRequest<Record<string, MLVorhersage> & { fehler?: string }>(
    `/ml/vorhersage/${encodeURIComponent(zoneId)}${suffix}`,
    { signal },
  )
  if (d.fehler) return {}
  return d
}

export async function holeEntscheidungen(
  zoneId?: string,
  limit?: number,
  signal?: AbortSignal,
): Promise<Entscheidung[]> {
  const params = new URLSearchParams()
  if (zoneId) params.set('zone_id', zoneId)
  if (limit != null) params.set('limit', String(limit))
  const suffix = params.toString() ? `?${params.toString()}` : ''
  return jsonRequest<Entscheidung[]>(`/entscheidungen${suffix}`, { signal })
}

export interface EntscheidungsFilter {
  zone_id?: string
  von?: string   // ISO
  bis?: string   // ISO
  blocker_typ?: string
  limit?: number
}

function baueEntscheidungsParams(f: EntscheidungsFilter): URLSearchParams {
  const p = new URLSearchParams()
  if (f.zone_id) p.set('zone_id', f.zone_id)
  if (f.von) p.set('von', f.von)
  if (f.bis) p.set('bis', f.bis)
  if (f.blocker_typ) p.set('blocker_typ', f.blocker_typ)
  if (f.limit != null) p.set('limit', String(f.limit))
  return p
}

export async function holeEntscheidungenGefiltert(
  filter: EntscheidungsFilter,
  signal?: AbortSignal,
): Promise<Entscheidung[]> {
  const params = baueEntscheidungsParams(filter)
  const suffix = params.toString() ? `?${params.toString()}` : ''
  return jsonRequest<Entscheidung[]>(`/entscheidungen${suffix}`, { signal })
}

/** T-0232: CSV-Export via authFetch + Blob-Download.
 *
 * Vorher rendete HistorieTab einen `<a href={csvUrl} download>` -- das
 * Browser-Native-Download umgeht aber den X-Api-Key-Header, den
 * `authFetch` injiziert. Mit aktivem T-0140-Auth fuehrt das zu HTTP 401.
 *
 * Fix: blob fetch ueber authFetch, dann `URL.createObjectURL` +
 * temporaerer `<a>`-Click + revoke. Filename wird aus dem
 * Content-Disposition-Header gelesen (Backend setzt
 * `entscheidungen_YYYY-MM-DD.csv`); Fallback auf eigenes Datum.
 *
 * Returnt `void` -- Fehler werden geworfen, damit der Caller einen
 * Banner zeigen kann.
 */
export async function ladeEntscheidungenCsv(
  filter: EntscheidungsFilter,
): Promise<void> {
  const params = baueEntscheidungsParams(filter)
  params.set('format', 'csv')
  const r = await authFetch(
    `${API}/entscheidungen?${params.toString()}`,
  )
  if (!r.ok) {
    throw new Error(`CSV-Export fehlgeschlagen: HTTP ${r.status}`)
  }
  const blob = await r.blob()
  // Filename aus Content-Disposition: 'attachment; filename=entscheidungen_2026-05-25.csv'
  const disp = r.headers.get('Content-Disposition') ?? ''
  const match = /filename="?([^";]+)"?/.exec(disp)
  const heute = new Date().toISOString().slice(0, 10)
  const dateiname = match?.[1] ?? `entscheidungen_${heute}.csv`

  const url = URL.createObjectURL(blob)
  try {
    const a = document.createElement('a')
    a.href = url
    a.download = dateiname
    document.body.appendChild(a)
    a.click()
    a.remove()
  } finally {
    // Browser haelt ObjectURL bis revokeObjectURL -- ein Tick warten,
    // damit der Click-Handler abschliessen kann, dann freigeben.
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }
}

export async function holeEreignisse(zoneId: string, stunden = 24, signal?: AbortSignal): Promise<VentilEreignis[]> {
  return jsonRequest<VentilEreignis[]>(
    `/zonen/${encodeURIComponent(zoneId)}/ereignisse?stunden=${stunden}`,
    { signal },
  )
}

export interface VentilAktivEintrag {
  zone_ids: string[]
  dauer_s: number
  gestartet: string
  verbleibend_s: number
  ausloser?: string
  quelle?: 'backend' | 'extern'
  stoppbar?: boolean
}

export interface VentilExternEintrag {
  zone_ids: string[]
  gestartet: string | null
  activity: string | null
  quelle: 'extern'
  stoppbar: false
}

export interface VentilStatusAntwort {
  aktiv: Record<string, VentilAktivEintrag>
  extern: Record<string, VentilExternEintrag>
}

export async function holeVentilStatus(signal?: AbortSignal): Promise<VentilStatusAntwort> {
  return jsonRequest<VentilStatusAntwort>('/ventil-status', { signal })
}

export async function notfallStopp(): Promise<{ ok?: boolean; geschlossen?: number; fehler?: string }> {
  return jsonRequest('/notfall-stopp', { method: 'POST' })
}

/**
 * T-0169 / T-0174: Manuelles Giessen loggen.
 *
 * Backward-Compat: alte Aufrufer mit `loggeGiessen(zoneId, 60)` bleiben
 * gueltig (Sekunden-Pfad). Neue Aufrufer mit Objekt-Argument koennen
 * `{ dauer_sekunden }` ODER `{ liter }` ODER beides senden, optional
 * mit `{ zeitstempel }` (ISO-8601) fuer rueckwirkendes Loggen (T-0174).
 */
export interface GiessenOpts {
  dauer_sekunden?: number
  liter?: number
  zeitstempel?: string  // ISO-8601, z.B. "2026-05-10T08:30:00"
}
export async function loggeGiessen(
  zoneId: string,
  opts: number | GiessenOpts,
): Promise<{ ok?: boolean; fehler?: string }> {
  const body: Record<string, unknown> = { zone_id: zoneId }
  if (typeof opts === 'number') {
    body.dauer_sekunden = opts
  } else {
    if (opts.dauer_sekunden !== undefined) body.dauer_sekunden = opts.dauer_sekunden
    if (opts.liter !== undefined) body.liter = opts.liter
    if (opts.zeitstempel !== undefined) body.zeitstempel = opts.zeitstempel
  }
  return jsonRequest('/giessen', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export async function holeBilanz(
  zoneId: string,
  fenster: BilanzFenster,
  signal?: AbortSignal,
): Promise<WasserBilanz | WasserBilanzFehler> {
  return jsonRequest<WasserBilanz | WasserBilanzFehler>(
    `/zonen/${encodeURIComponent(zoneId)}/bilanz?fenster=${fenster}`,
    { signal },
  )
}

export async function holeVentilEreignisse(
  von?: string, bis?: string, zoneId?: string, signal?: AbortSignal,
): Promise<VentilEreignisDetail[]> {
  const params = new URLSearchParams()
  if (von) params.set('von', von)
  if (bis) params.set('bis', bis)
  if (zoneId) params.set('zone_id', zoneId)
  const suffix = params.toString() ? `?${params.toString()}` : ''
  return jsonRequest<VentilEreignisDetail[]>(`/ventil-ereignisse${suffix}`, { signal })
}

// T-0335: Gruppierte Giess-Laeufe (Pre-Soaks als EIN Lauf). Ohne zoneId ->
// alle giessbaren Zonen chronologisch gemischt (serielle Straenge dedupliziert).
export async function holeGiessHistorie(
  zoneId: string | undefined, tage = 14, signal?: AbortSignal,
): Promise<GiessLauf[]> {
  const pfad = zoneId
    ? `/zonen/${encodeURIComponent(zoneId)}/giess-historie?tage=${tage}`
    : `/giess-historie?tage=${tage}`
  return jsonRequest<GiessLauf[]>(pfad, { signal })
}

export async function patchVentilEreignis(
  id: number, patch: VentilEreignisPatch, paar: boolean = false,
): Promise<{ ok?: boolean; fehler?: string; ids?: number[] }> {
  const suffix = paar ? '?paar=true' : ''
  return jsonRequest(`/ventil-ereignis/${id}${suffix}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
}

export async function loescheVentilEreignis(
  id: number, paar: boolean = false,
): Promise<{ ok?: boolean; fehler?: string; ids?: number[] }> {
  const suffix = paar ? '?paar=true' : ''
  return jsonRequest(`/ventil-ereignis/${id}${suffix}`, { method: 'DELETE' })
}

export async function holeSchwellenVorschlaege(
  signal?: AbortSignal,
  fensterTage: number = 30,
): Promise<SchwellenVorschlag[]> {
  return jsonRequest<SchwellenVorschlag[]>(
    `/schwellen-vorschlag?fenster_tage=${fensterTage}`,
    { signal },
  )
}

export async function holeOpsSummary(signal?: AbortSignal): Promise<OpsSummary> {
  return jsonRequest<OpsSummary>('/ops/summary', { signal })
}

/** T-0238: Betriebsstatus-Zentrale. Polling-Cadence im Tab: 60 s. */
export async function holeBetriebsstatus(
  signal?: AbortSignal,
): Promise<Betriebsstatus> {
  return jsonRequest<Betriebsstatus>('/ops/betriebsstatus', { signal })
}

/** T-0228 Stufe 1: offene Pflege-Erinnerungen mit faellig_am <=
 *  jetzt + `anstehend_tage`. Default 3 Tage Vorlauf. */
export async function holePflegeErinnerungen(
  anstehendTage: number = 3,
  signal?: AbortSignal,
): Promise<{ eintraege: PflegeErinnerung[] }> {
  const params = new URLSearchParams({
    anstehend_tage: String(anstehendTage),
  })
  return jsonRequest<{ eintraege: PflegeErinnerung[] }>(
    `/pflege-erinnerungen?${params.toString()}`,
    { signal },
  )
}

/** T-0236: Empfehlungs-Audit-Log + Aggregate pro Zone bzw. global.
 *  Optional `zone_id` als Filter, Fenster in Tagen. */
export async function holeEmpfehlungsAudit(
  fensterTage: number = 30,
  zoneId?: string,
  signal?: AbortSignal,
): Promise<EmpfehlungsAuditAntwort> {
  const params = new URLSearchParams({ tage: String(fensterTage), limit: '500' })
  if (zoneId) params.set('zone_id', zoneId)
  return jsonRequest<EmpfehlungsAuditAntwort>(
    `/empfehlungs-audit?${params.toString()}`,
    { signal },
  )
}

/** T-0236: ML-Dauer-Drift pro Zone (Heuristik vs. ML-MAE).
 *  Schwellen-Ampel laut Backend: gruen <= 50%, gelb <= 80%, rot > 80%
 *  von Heuristik-MAE. */
export async function holeMlDauerDrift(
  fenster: string = '30d',
  zoneId?: string,
  signal?: AbortSignal,
): Promise<DauerDriftAntwort> {
  const params = new URLSearchParams({ fenster })
  if (zoneId) params.set('zone_id', zoneId)
  return jsonRequest<DauerDriftAntwort>(
    `/ml/dauer-drift?${params.toString()}`,
    { signal },
  )
}

/** T-0227: Tagesplan-Vorschau "heute" / "morgen". Aggregat pro Zone +
 *  Wetter pro Standort. Polling-Cadence im Frontend: 5 min. */
export async function holeTagesplan(
  tag: 'heute' | 'morgen' = 'heute',
  signal?: AbortSignal,
): Promise<Tagesplan> {
  return jsonRequest<Tagesplan>(
    `/tagesplan?tag=${encodeURIComponent(tag)}`,
    { signal },
  )
}

/** T-0228 Stufe 2: offene Wartungs-Fenster. App-Level-Polling
 *  speist die Karten mit "Wartung aktiv"-Info. */
export async function holeWartungsFenster(
  signal?: AbortSignal,
): Promise<{ eintraege: WartungsFenster[] }> {
  return jsonRequest<{ eintraege: WartungsFenster[] }>(
    '/wartungs-fenster',
    { signal },
  )
}

export async function starteWartungsFenster(
  zoneId: string,
  grund: string = '',
): Promise<{ ok: boolean; id?: number; fehler?: string }> {
  return jsonRequest('/wartungs-fenster', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ zone_id: zoneId, grund }),
  })
}

export async function beendeWartungsFenster(
  fensterId: number,
): Promise<{ ok: boolean; geaendert: boolean }> {
  return jsonRequest(`/wartungs-fenster/${fensterId}/beenden`, {
    method: 'POST',
  })
}

/** T-0228 Stufe 1: Pflege-Erinnerung als erledigt markieren.
 *  Bei wiederkehrenden Erinnerungen (`intervall_tage`) legt das
 *  Backend automatisch den naechsten Eintrag an. */
export async function erledigePflegeErinnerung(
  id: number,
): Promise<{ ok: boolean; folge: PflegeErinnerung | null }> {
  return jsonRequest<{ ok: boolean; folge: PflegeErinnerung | null }>(
    `/pflege-erinnerungen/${id}/erledigen`,
    { method: 'POST' },
  )
}

/** T-0066: Dry-Run-Gießempfehlung pro Zone (keine DB-Writes backend-seitig).
 *  Wird vom `GiessEmpfehlungPanel` alle 60 s gepollt. Backend setzt
 *  Cache-Control: max-age=60, d.h. das Panel darf optimistisch polling. */
export async function holeGiessEmpfehlung(
  zoneId: string, signal?: AbortSignal,
): Promise<GiessEmpfehlung> {
  return jsonRequest<GiessEmpfehlung>(
    `/zonen/${encodeURIComponent(zoneId)}/empfehlung-jetzt`,
    { signal },
  )
}

export async function holeMlDrift(
  zoneId?: string, fensterTage = 7, signal?: AbortSignal,
): Promise<MlDriftAntwort> {
  const params = new URLSearchParams({ fenster: `${fensterTage}d` })
  if (zoneId) params.set('zone_id', zoneId)
  return jsonRequest<MlDriftAntwort>(`/ml/drift?${params.toString()}`, { signal })
}

export async function holeMlDriftLog(
  zoneId: string, horizont: 6 | 12 | 24, n = 100,
  signal?: AbortSignal,
): Promise<MlDriftLogAntwort> {
  const params = new URLSearchParams({
    zone_id: zoneId,
    horizont: String(horizont),
    n: String(n),
  })
  return jsonRequest<MlDriftLogAntwort>(
    `/ml/drift/log?${params.toString()}`, { signal },
  )
}

/* T-0111: Pre-Soak-Sequenz */

export interface PreSoakLauf {
  zone_id: string
  kanal: number
  /** T-0462: `haupt_pause` (Soak-Pause ZWISCHEN zwei Haupt-Pulsen) fehlte hier,
   *  obwohl das Backend sie seit T-0437 liefert (`pre_soak.py` `_PHASE_RANG`).
   *  Folge: `phaseLabel()` fiel auf seinen Default "Fehler" durch und die Karte
   *  meldete waehrend eines regulaeren, stundenlangen Zustands einen Defekt.
   *  Klasse: `fehlerpattern_neuer_enumwert_faellt_aus_positivfilter`. */
  phase:
    | 'pre_soak' | 'pause' | 'haupt' | 'haupt_pause'
    | 'fertig' | 'fehler' | 'stop_fehler'
  pre_soak_s: number
  pause_s: number
  haupt_s: number
  /** T-0437 Cycle-and-Soak: `haupt_s` wird in `haupt_pulse` gleich lange Pulse
   *  aufgeteilt (Gesamtmenge unveraendert), dazwischen `haupt_pause_s`
   *  Einsickerzeit; `haupt_pulse_gestartet` ist der 1-basierte Index des
   *  zuletzt gestarteten Pulses. Das Backend liefert die drei Felder immer --
   *  optional sind sie nur wegen des Optimistic-Update-Stubs beim Start, der
   *  die Aufteilung noch nicht kennt (sie kommt aus der Zonen-Konfig).
   *  Fehlen sie, gilt der Einzel-Lauf (1 Puls, keine Pause). */
  haupt_pulse?: number
  haupt_pause_s?: number
  haupt_pulse_gestartet?: number
  gestartet_am: string
  fehler: string | null
  /** T-0431: wer die Sequenz gestartet hat. Die Karte beschriftete jeden
   *  laufenden Pre-Soak als "Manuell aktiv", weil sie den Ausloeser nicht
   *  kannte -- auch einen von der Automatik gestarteten (Realfall 23.07.
   *  hecke). Der Wert stand in `pre_soak_state.ausloser`, kam aber nie im
   *  Frontend an. */
  ausloser?: 'automatik' | 'manuell' | string
  /** T-0411: true, wenn der Lauf von einer ANDEREN Zone am selben Ventil-
   *  Kanal gestartet wurde. Das Ventil haengt am Kanal, nicht an der Zone --
   *  ein Pre-Soak auf `bambuswald` giesst `bambuswald_yogaraum` mit. Die
   *  Karte muss das zeigen, darf es aber nicht als eigenen Lauf ausgeben.
   *  Optional: nur die zone_id-gefilterte Abfrage setzt das Feld. */
  fremd?: boolean
}

/** T-0444/T-0450: Advisory des Backends, wenn der geplante manuelle Lauf das
 *  Tagesbudget der Zone ueberschreitet. Kein Blocker -- der Lauf startet
 *  trotzdem, der Nutzer soll es nur sehen. Wird von BEIDEN manuellen
 *  Start-Endpoints geliefert (`/ventil/manuell-start`, `/ventil/pre-soak-start`,
 *  api_server.py `_baue_budget_warnung`) und deshalb hier zentral typisiert.
 *  Das Feld fehlt in der Antwort komplett, wenn keine Warnung vorliegt. */
export interface BudgetWarnung {
  tages_budget_sekunden: number
  /** T-0452: Schwelle, an der gewarnt wird (`tages_advisory_anteil` x Budget).
   *  Kann unter dem Budget liegen -- das Budget ist die Automatik-Notbremse,
   *  nicht die Warnschwelle. */
  advisory_schwelle_sekunden: number
  /** true = auch das Budget selbst ist ueberschritten, nicht nur die
   *  Warnschwelle. Steuert die Wortwahl im `text`. */
  budget_ueberschritten: boolean
  verbraucht_sekunden: number
  geplant_sekunden: number
  /** Fertig formulierter ASCII-Text aus dem Backend. */
  text: string
}

export async function startePreSoak(
  zoneId: string,
  preSoakMin: number,
  pauseMin: number,
  hauptMin: number,
): Promise<{
  ok?: boolean
  fehler?: string
  // T-0151: bei `fehler === 'HAHN_BELEGT'` durchgereichte Felder.
  grund?: string
  aktive_zonen?: string[]
  verbrauch_aktuell_lpm?: number
  verbrauch_neu_lpm?: number
  budget_lpm?: number
  // T-0450: nur im Erfolgsfall und nur bei Ueberschreitung gesetzt.
  budget_warnung?: BudgetWarnung
}> {
  return jsonRequest('/ventil/pre-soak-start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      zone_id: zoneId,
      pre_soak_min: preSoakMin,
      pause_min: pauseMin,
      haupt_min: hauptMin,
    }),
  })
}

export async function stoppePreSoak(
  zoneId: string,
): Promise<{ ok?: boolean; fehler?: string }> {
  return jsonRequest('/ventil/pre-soak-stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ zone_id: zoneId }),
  })
}

export async function holePreSoakStatus(
  zoneId?: string,
  signal?: AbortSignal,
): Promise<{ laeufe: PreSoakLauf[] }> {
  const suffix = zoneId ? `?zone_id=${encodeURIComponent(zoneId)}` : ''
  return jsonRequest(`/ventil/pre-soak-status${suffix}`, { signal })
}

/**
 * T-0200: Aggregierter Dashboard-Snapshot — ein Call statt N pro Zone.
 *
 * Loest die Per-Karte-Polls aus dem V2-Tab ab. Backend liefert pro Zone
 * `zone` + `empfehlung` + `messwerte[fenster]` + `ml_vorhersage` in
 * einer Antwort. Default-Fenster `24h,48h` deckt Karten-Chart + Trend.
 *
 * Polling-Cadence: 60 s (Backend setzt Cache-Control max-age=30, halb
 * so lang wie das Polling-Intervall, damit der Browser-Cache keine
 * Daten unterschlaegt zwischen zwei Refreshes).
 */
export async function holeDashboardSnapshot(
  signal?: AbortSignal,
  fenster: SnapshotFenster[] = ['24h', '48h'],
  mlDetails: boolean = false,
): Promise<DashboardSnapshot> {
  const params = new URLSearchParams({ fenster: fenster.join(',') })
  if (mlDetails) params.set('ml_details', 'true')
  return jsonRequest<DashboardSnapshot>(
    `/dashboard-snapshot?${params.toString()}`,
    { signal },
  )
}

export async function holeOpsTimeline(
  stunden = 24,
  severity: OpsSeverityFilter[] = ['kritisch', 'aktion', 'wetter'],
  zoneId?: string,
  signal?: AbortSignal,
): Promise<OpsTimelineAntwort> {
  const params = new URLSearchParams({
    stunden: String(stunden),
    severity: severity.join(','),
  })
  if (zoneId) params.set('zone_id', zoneId)
  return jsonRequest<OpsTimelineAntwort>(`/ops/timeline?${params.toString()}`, { signal })
}
