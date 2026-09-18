/* T-0094 / T-0114: Banner in der Zonen-Karte bei unklassifizierten
 * Sensor-Sprung-Events (sensor_heuristik mit ausloeser='unbekannt').
 *
 * Realfall 18.04.: User-Schlauch-Bewaesserung wurde als unbekannt-
 * Heuristik geloggt, aber nie klassifiziert -> T-0085 Auto-Kalibrierung
 * lief in Outlier-Datenpunkte rein. Mit dem Banner kann der User pro
 * unklarem Event entscheiden was passiert ist.
 *
 * Buttons:
 * - "Manuell gegossen": PATCH ausloeser='manuell' (Event bleibt, zaehlt
 *   als echte Bewaesserung).
 * - "Regen / Sensor-Glitch": PATCH ausloeser='ignoriert' (Event bleibt
 *   als Marker, damit der Heuristik-Job es nicht regeneriert. Wird von
 *   Wirkungsrate/Bilanz/Budget/ML wie unbekannt behandelt = ignoriert).
 * - "Fremdwasser" (T-0453): PATCH ausloeser='fremdwasser'. Der Sprung ist
 *   ECHT, aber das Wasser kam aus dem Kanal einer Nachbar-Zone (der
 *   Rasen-Regner trifft diesen Sensor). Zaehlt deshalb nicht als eigenes
 *   Kanal-Wasser. Vorher fehlte dieser Zielwert: "Manuell gegossen" war
 *   die einzige Option, die "hier kam wirklich Wasser an" ausdrueckte, und
 *   belastete damit Tagesbudget und Pause-Anker dieser Zone. "Regen /
 *   Glitch" waere ebenso falsch -- das bedeutet "ist nie passiert".
 *   Die Quell-Zone leitet das Backend ab, wenn sie eindeutig ist.
 * - "Spaeter": tut nichts, Banner bleibt.
 *
 * T-0114-Fix: DELETE wuerde der Heuristik-Job sofort wieder schreiben,
 * weil der Sprung in den Sensordaten ja existiert. Daher PATCH statt
 * DELETE. Plus: Anzeige der echten Reaktionsdauer aus dem gepaarten
 * SCHLIESSEN-Event (vorher 0 min, weil OEFFNEN-Dauer per Vertrag 0 ist).
 */
import { useEffect, useState, useCallback } from 'react'
import { API, authFetch } from '../api'
import { starteSichtbarkeitsIntervall } from '../sichtbarkeitsPolling'
import './UnbekanntEventBanner.css'

interface VentilEreignisDetail {
  id: number
  zeitstempel: string
  zone_id: string
  ventil_id: string
  aktion: string
  dauer_sekunden: number
  ausloser: string
  liter: number | null
}

interface Props {
  zoneId: string
  /** T-0294b: zeigt zusaetzlich eine "AquaBloom"-Klassifizier-Option, wenn
   *  die Zone eine AquaBloom-Pumpe konfiguriert hat. Dann kann der User
   *  einen Sensor-Sprung, den die Auto-Konversion (aquabloom_job) verpasst
   *  hat (z.B. Config-Cadence-Drift, T-0294), selbst korrekt zuordnen. Bei
   *  Nicht-AquaBloom-Zonen ausgeblendet (verhindert Fehl-Klassifikation). */
  istAquabloom?: boolean
}

const POLLING_MS = 60_000

// OEFFNEN-Event mit zugehoerigem SCHLIESSEN-Pendant fuer Anzeige.
interface UnbekanntDoppel {
  oeffnen: VentilEreignisDetail
  schliessen: VentilEreignisDetail | null
}

export function UnbekanntEventBanner({ zoneId, istAquabloom = false }: Props) {
  const [doppel, setDoppel] = useState<UnbekanntDoppel[]>([])
  const [aktion, setAktion] = useState<string | null>(null)
  const [fehler, setFehler] = useState<string | null>(null)

  const laden = useCallback(async () => {
    try {
      const von = new Date(Date.now() - 48 * 60 * 60 * 1000).toISOString()
      const url = `${API}/ventil-ereignisse?zone_id=${encodeURIComponent(zoneId)}&von=${encodeURIComponent(von)}`
      const r = await authFetch(url)
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const daten: VentilEreignisDetail[] = await r.json()
      // Nur unbekannt-OEFFNEN. ignoriert-Events sind klassifiziert
      // (User hat als Phantom markiert) — bewusst NICHT mehr fragen.
      const unbekannt = daten.filter(
        (e) => e.ausloser === 'unbekannt' && e.aktion === 'oeffnen',
      )
      unbekannt.sort((a, b) => b.zeitstempel.localeCompare(a.zeitstempel))
      // T-0296: Pendant-SCHLIESSEN ueber Naehe statt starrem 15-min-Fenster
      // suchen -- lange Laeufe (z.B. waldblumenhain 1260s = 21 min) fielen
      // sonst raus und der Banner zeigte keine Dauer. Das autoritative Paaren
      // bei der Klassifikation macht das Backend (finde_ventil_paar, Anker).
      const MAX_LAUF_MS = 12 * 60 * 60 * 1000
      const paare: UnbekanntDoppel[] = unbekannt.map(o => {
        const t_o = new Date(o.zeitstempel).getTime()
        const pendant = daten
          .filter(s =>
            s.aktion === 'schliessen' &&
            s.ventil_id === o.ventil_id &&
            s.ausloser === 'unbekannt' &&
            new Date(s.zeitstempel).getTime() > t_o &&
            new Date(s.zeitstempel).getTime() - t_o <= MAX_LAUF_MS,
          )
          .sort((a, b) =>
            new Date(a.zeitstempel).getTime() - new Date(b.zeitstempel).getTime(),
          )[0] ?? null
        return { oeffnen: o, schliessen: pendant }
      })
      setDoppel(paare)
    } catch (e) {
      setFehler(e instanceof Error ? e.message : 'Fehler')
    }
  }, [zoneId])

  useEffect(() => {
    // Regel-Ausnahme mit Begruendung: `laden` ist `async` und setzt State erst
    // NACH dem ersten `await` (authFetch) -- synchron laeuft hier nichts, was
    // einen Extra-Render ausloest. Die Regel flaggt konservativ jeden Aufruf in
    // eine Funktion, die irgendwo setState enthaelt, ohne die await-Grenze zu
    // beruecksichtigen. Geprueft am 09.08.2026: erste Anweisung ist der fetch,
    // `setDoppel`/`setFehler` liegen dahinter.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void laden()
    const stoppeIntervall = starteSichtbarkeitsIntervall(() => void laden(), POLLING_MS)
    return () => stoppeIntervall()
  }, [laden])

  /** T-0212a (2026-05-19): Bulk-Klassifikation aller aktuell sichtbaren
   *  UNBEKANNT-Events in einem Request. Verhindert Rate-Limit-Lawine
   *  bei Phantom-Mass-Aufkommen (z.B. waehrend Bodenart-Reset-Phasen,
   *  20+ Events pro Nacht moeglich). Ein User-Klick -> ein Backend-
   *  Request -> alle Events klassifiziert.
   *
   *  T-0216 (2026-05-19): Optimistic Update. Symptom: Banner blieb
   *  nach Klick sichtbar — nicht permanent, aber ueber mehrere
   *  Minuten, Buttons in `cursor: wait`-Style (`:disabled`, opacity
   *  0.5). Mechanik: `await laden()` als blockierender GET nach
   *  erfolgreichem POST; fetch hat keinen Timeout-Default,
   *  `aktion !== null` bleibt aktiv solange dieser GET haengt.
   *  Realfall 19.05.: Backend-Prozess lief noch mit altem Code (vor
   *  T-0210), DB-Patch ging trotzdem durch (Audit-Trail 21:38:32
   *  fuer magerwiese:937/938), aber der nachgelagerte GET wartete
   *  unter Lock-Stress lange — fuer den User nicht von "stuck"
   *  unterscheidbar. Fix: lokal die geklickten IDs sofort aus
   *  `doppel` entfernen, `void laden()` entkoppelt im Hintergrund;
   *  `finally setAktion(null)` re-enabled die Buttons sofort.
   *  Pattern aus Memory `arbeitspattern_optimistic_update_live.md`.
   */
  const klassifiziereAlle = async (
    ausloser: 'manuell' | 'ignoriert' | 'aquabloom' | 'fremdwasser',
    alle_ids: number[],
  ) => {
    setAktion(`bulk-${ausloser}`)
    try {
      const r = await authFetch(`${API}/ventil-ereignisse/klassifiziere-bulk`, {
        method: 'POST',
        body: JSON.stringify({ ids: alle_ids, ausloser, paar: true }),
        headers: { 'Content-Type': 'application/json' },
      })
      if (r.status === 429) {
        throw new Error('Rate-Limit erreicht — bitte 60 s warten und nochmal probieren')
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setFehler(null)
      // T-0216 Optimistic Update: Banner sofort weg, Polling-Resync
      // im Hintergrund. Wenn das Backend einzelne Events nicht patchen
      // konnte, kommen sie beim naechsten `laden()` zurueck.
      setDoppel(prev => prev.filter(d => !alle_ids.includes(d.oeffnen.id)))
    } catch (e) {
      setFehler(e instanceof Error ? e.message : 'Fehler')
    } finally {
      setAktion(null)
    }
    // Reload entkoppelt vom UI-Re-Enable. Wenn der Server haengt oder
    // langsam ist, sieht der User trotzdem sofort, dass sein Klick
    // angekommen ist.
    void laden()
  }

  if (doppel.length === 0) return null

  const aktuell = doppel[0]
  const event = aktuell.oeffnen
  const zeit = new Date(event.zeitstempel).toLocaleString('de-DE', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
  })
  // Reaktionsdauer aus dem gepaarten SCHLIESSEN — OEFFNEN-Dauer ist per
  // Event-Vertrag immer 0. Ohne Pendant kein Dauer-Hinweis.
  const dauerMin = aktuell.schliessen
    ? Math.round(aktuell.schliessen.dauer_sekunden / 60)
    : null

  // T-0212a: Bei mehreren Events nutzt der Bulk-Klick alle ids auf
  // einmal — sonst wuerde User bei Phantom-Mass-Aufkommen das Rate-
  // Limit reissen. Bei einem einzelnen Event ist alle_ids == [aktuelles].
  const alle_ids = doppel.map(d => d.oeffnen.id)
  const bulk_n = doppel.length

  return (
    <div className="ueb-banner">
      <div className="ueb-frage">
        <span className="ueb-icon" aria-hidden="true">❓</span>
        <span>
          Sensor-Sprung am {zeit}
          {dauerMin !== null && dauerMin > 0 ? ` (${dauerMin} min)` : ''}.
          Was war das?
        </span>
        {bulk_n > 1 && (
          <span className="ueb-counter">+{bulk_n - 1} weitere</span>
        )}
      </div>
      {/* T-0277: Bei mehreren Events GETRENNTE Button-Paare fuer "nur
          dieser" vs "alle N". Vorher gab es NUR den Bulk-Button -- ein
          Klick flippte alle Events auf einmal (Realfall waldblumen
          28.05.: 14 Heuristik-Events in <1 s als Glitch geflippt, obwohl
          evtl. einzelne echte Spruenge dabei waren). `klassifiziereAlle`
          ist generisch ueber die uebergebene id-Liste; "nur dieser"
          ruft mit `[event.id]` (+ Paar via Backend `paar=true`). */}
      <div className="ueb-buttons">
        <button
          type="button"
          disabled={aktion !== null}
          onClick={() => void klassifiziereAlle('manuell', [event.id])}
          title="Nur diesen Sensor-Sprung als manuell gegossen markieren"
        >
          Manuell gegossen
        </button>
        <button
          type="button"
          disabled={aktion !== null}
          onClick={() => void klassifiziereAlle('ignoriert', [event.id])}
          title="Nur diesen Sensor-Sprung als Regen/Glitch markieren"
        >
          Regen / Glitch
        </button>
        {/* T-0453: vierte Option. Bewusst fuer ALLE Zonen sichtbar (nicht
            wie AquaBloom an eine Config gekoppelt): welcher Regner welchen
            Sensor trifft, haengt an seiner Aufstellposition und aendert
            sich von Lauf zu Lauf -- eine Config-Bedingung waere hier eine
            zweite Wahrheit ueber die Gartengeometrie. */}
        <button
          type="button"
          disabled={aktion !== null}
          onClick={() => void klassifiziereAlle('fremdwasser', [event.id])}
          title="Wasser kam vom Regner einer Nachbar-Zone (Cross-Spray) — echter Sprung, aber nicht aus diesem Kanal"
        >
          Fremdwasser
        </button>
        {/* T-0294b: AquaBloom-Option nur bei konfigurierten AquaBloom-Zonen.
            Faengt verpasste Auto-Konversionen (Config-Cadence-Drift) ab. */}
        {istAquabloom && (
          <button
            type="button"
            disabled={aktion !== null}
            onClick={() => void klassifiziereAlle('aquabloom', [event.id])}
            title="Nur diesen Sensor-Sprung als AquaBloom-Pumpe markieren"
          >
            AquaBloom
          </button>
        )}
      </div>
      {bulk_n > 1 && (
        <div className="ueb-buttons ueb-buttons-bulk">
          <button
            type="button"
            disabled={aktion !== null}
            onClick={() => void klassifiziereAlle('manuell', alle_ids)}
            title={`Alle ${bulk_n} Sensor-Spruenge als manuell markieren`}
          >
            Alle {bulk_n} manuell
          </button>
          <button
            type="button"
            disabled={aktion !== null}
            onClick={() => void klassifiziereAlle('ignoriert', alle_ids)}
            title={`Alle ${bulk_n} Sensor-Spruenge als Regen/Glitch markieren`}
          >
            Alle {bulk_n} Regen / Glitch
          </button>
          <button
            type="button"
            disabled={aktion !== null}
            onClick={() => void klassifiziereAlle('fremdwasser', alle_ids)}
            title={`Alle ${bulk_n} Sensor-Spruenge als Fremdwasser (Nachbar-Regner) markieren`}
          >
            Alle {bulk_n} Fremdwasser
          </button>
          {istAquabloom && (
            <button
              type="button"
              disabled={aktion !== null}
              onClick={() => void klassifiziereAlle('aquabloom', alle_ids)}
              title={`Alle ${bulk_n} Sensor-Spruenge als AquaBloom markieren`}
            >
              Alle {bulk_n} AquaBloom
            </button>
          )}
        </div>
      )}
      {fehler && <div className="ueb-fehler">Fehler: {fehler}</div>}
    </div>
  )
}
