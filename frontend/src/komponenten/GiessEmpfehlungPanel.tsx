/* GiessEmpfehlungPanel — Kausale Gieß-Empfehlung pro Zone (T-0066/T-0075).
 *
 * Statt nur "Würde 10 min gießen, blockiert: außerhalb Zeit" liefert das
 * Backend jetzt eine kausale Aussage: aktueller Sensor + Welkepunkt +
 * Prognose ohne Gießen + Reserve-Tage + Empfehlungs-Typ. Diese Komponente
 * rendert das als Erzählung statt als Werte-Liste.
 *
 * Drei Empfehlungs-Typen (vom Backend klassifiziert):
 *   - 'akut'        : Sensor nahe Welkepunkt oder weniger als 1.5 Tage Reserve
 *   - 'praeventiv'  : unter Schwelle, aber Reserve noch ok
 *   - 'kein_bedarf' : über Schwelle, lange Reserve
 *
 * Ruft /api/zonen/{id}/empfehlung-jetzt alle 60 s ab; Backend cached
 * per Cache-Control entsprechend, damit Polling-Kosten klein bleiben.
 * Kein Side-Effect im Backend (kein entscheidung_log, kein shadow_log).
 */

import { useEffect, useState } from 'react'
import { holeGiessEmpfehlung, holeVentilStatus, type VentilAktivEintrag } from '../api'
import type { GiessEmpfehlung } from '../typen'
import { istAbbruch } from '../hilfsfunktionen'
import './GiessEmpfehlungPanel.css'

interface Props {
  zoneId: string
}

function formatiereMinuten(dauerS: number): string {
  const minuten = Math.round(dauerS / 60)
  return `${minuten} min`
}

function formatiereLiter(liter: number | null): string {
  if (liter === null) return ''
  return `≈ ${liter.toFixed(1)} L`
}

function ampelLabel(ampel: GiessEmpfehlung['drift_ampel']): string {
  switch (ampel) {
    case 'gruen': return 'MAE-Gate grün'
    case 'gelb':  return 'MAE-Gate gelb'
    case 'rot':   return 'MAE-Gate rot'
    default:      return ''
  }
}

function welkepunktQuelleLabel(quelle: GiessEmpfehlung['welkepunkt_quelle']): string {
  switch (quelle) {
    case 'manuell':                    return ''
    case 'kalibrierung':               return ' (aus Kalibrierungs-Median)'
    case 'tagesmin_schaetzung':        return ' (Schätzung aus Sensor-Historie)'
    case 'feuchte_kritisch_fallback':  return ' (Konfig-Fallback)'
    default:                           return ''
  }
}

// T-0085: Quelle der Wirkungsrate (`delta_pp_pro_minute`).
function wirkungsrateQuelleLabel(
  quelle: GiessEmpfehlung['delta_pp_pro_minute_quelle'],
): string {
  switch (quelle) {
    case 'manuell':       return ''  // Konfig-Wert, normaler Stand
    case 'kalibrierung':  return ' (aus letzten Bewässerungen gemessen)'
    case 'default':       return ' (Standardwert, noch keine Bewässerungs-Daten)'
    default:              return ''
  }
}

function typIcon(typ: GiessEmpfehlung['empfehlungs_typ']): string {
  switch (typ) {
    case 'akut':              return '🚨'
    case 'praeventiv':        return '🚿'
    case 'wohlfuehl_grenze':  return '💧'
    case 'kein_bedarf':       return '✓'
  }
}

// T-0103: Strategie-Label fuer Anzeige im Panel.
function strategieLabel(strategie: GiessEmpfehlung['aktive_strategie']): string {
  switch (strategie) {
    case 'korridor':         return 'Korridor'
    case 'haeufig_klein':    return 'Häufig + klein'
    case 'selten_gross':     return 'Selten + groß'
    case 'konstant_niedrig': return 'Konstant niedrig'
  }
}

function blockerIcon(blocker: GiessEmpfehlung['blocker_typ']): string {
  switch (blocker) {
    case 'FEUCHTE_OK':        return '✓'
    case 'REGEN_ERWARTET':    return '☔'
    case 'ZEITFENSTER':       return '⏱'
    // T-0356: kein ⛔ (Verbotsschild alarmiert) -- das Tagesbudget ist eine
    // ruhige Notbremse, kein Fehler. 🔋 = "Kapazitaet fuer heute voll".
    case 'BUDGET_ERSCHOEPFT': return '🔋'
    case 'PAUSE_AKTIV':       return '⏸'
    case 'KEINE_MESSUNG':     return '⚠'
    default:                  return ''
  }
}

const POLLING_INTERVALL_MS = 60_000   // Backend-Cache-Header = 60 s

export function GiessEmpfehlungPanel({ zoneId }: Props) {
  const [empf, setEmpf] = useState<GiessEmpfehlung | null>(null)
  const [laedt, setLaedt] = useState(true)
  const [fehler, setFehler] = useState<string | null>(null)
  // T-0120: Banner ueber der Empfehlung wenn auf dem Kanal gerade
  // Backend-Bewaesserung laeuft. Polling separat von der Empfehlung,
  // weil ventil-status nicht gecacht ist und schneller reagieren soll.
  const [aktiverLauf, setAktiverLauf] = useState<VentilAktivEintrag | null>(null)

  useEffect(() => {
    const controller = new AbortController()

    async function laden() {
      try {
        const daten = await holeGiessEmpfehlung(zoneId, controller.signal)
        setEmpf(daten)
        setFehler(null)
      } catch (e) {
        if (istAbbruch(e)) return
        setFehler(e instanceof Error ? e.message : String(e))
      } finally {
        setLaedt(false)
      }
    }

    setLaedt(true)
    void laden()
    const intervallId = window.setInterval(laden, POLLING_INTERVALL_MS)

    return () => {
      controller.abort()
      window.clearInterval(intervallId)
    }
  }, [zoneId])

  // T-0120: Ventil-Status alle 5 s pollen.
  useEffect(() => {
    const ctrl = new AbortController()
    let aktiv = true
    const fetchStatus = async () => {
      try {
        const r = await holeVentilStatus(ctrl.signal)
        if (!aktiv) return
        const treffer = Object.values(r.aktiv ?? {}).find(
          e => e.zone_ids?.includes(zoneId),
        )
        setAktiverLauf(treffer ?? null)
      } catch {
        /* still ignorieren */
      }
    }
    void fetchStatus()
    const id = window.setInterval(fetchStatus, 5000)
    return () => {
      aktiv = false
      ctrl.abort()
      window.clearInterval(id)
    }
  }, [zoneId])

  if (laedt && !empf) {
    return (
      <div className="gep-container gep-laden">
        <div className="gep-kopf">
          <span className="gep-titel">Gießempfehlung …</span>
        </div>
      </div>
    )
  }

  if (fehler && !empf) {
    return (
      <div className="gep-container gep-fehler">
        <div className="gep-kopf">
          <span className="gep-titel">Gießempfehlung</span>
          <span className="gep-fehlertext">nicht verfügbar</span>
        </div>
      </div>
    )
  }

  if (!empf) return null

  // Status-Klasse hängt am Empfehlungs-Typ + Blocker-Status:
  //   - kein_bedarf + nicht-blockiert         → gep-inaktiv (gesund-grün)
  //   - akut/praeventiv + soll_bewaessern=true → gep-aktiv (info-blau)
  //   - akut/praeventiv + blockiert            → gep-hypothetisch (warnung-gelb)
  //   - T-0183 Monitoring-Zone akut/praeventiv → eigene Klassen, nicht
  //     als "blockiert" framen sondern als bewusste Beobachtung.
  const istMonitoring = empf.ml_status_grund === 'zone_monitoring'
  const istBlockiert =
    !istMonitoring
    && !empf.soll_bewaessern
    && empf.empfehlungs_typ !== 'kein_bedarf'
  const statusKlasse =
    empf.empfehlungs_typ === 'kein_bedarf'
      ? 'gep-inaktiv'
      : istMonitoring && empf.empfehlungs_typ === 'akut'
        ? 'gep-monitoring-akut'
        : istMonitoring
          ? 'gep-monitoring-praev'
          : empf.empfehlungs_typ === 'wohlfuehl_grenze'
            ? 'gep-wohlfuehl'   // T-0103: sanfte Stufe (blau)
            : empf.soll_bewaessern
              ? 'gep-aktiv'
              : 'gep-hypothetisch'

  // Wenn ML wirksam UND aktiv gegossen werden soll → ML primär in der
  // Hauptzeile. Sonst: kausale Empfehlungs-Dauer (oder Heuristik wenn fehlend).
  const mlPrimaer =
    empf.soll_bewaessern && empf.ml_wirksam && empf.dauer_s_ml !== null
  const dauer_haupt =
    mlPrimaer ? empf.dauer_s_ml
    : empf.dauer_s_empfehlung !== null ? empf.dauer_s_empfehlung
    : empf.dauer_s_heuristik

  // T-0183: bei Monitoring-Zonen sprechen wir nicht von "Gießempfehlung"
  // sondern von "Beobachtung" — es wird bewusst nicht automatisch gegossen.
  const kopfTitel = istMonitoring
    ? (
        empf.empfehlungs_typ === 'akut'        ? 'Beobachtung — akut'
        : empf.empfehlungs_typ === 'praeventiv' ? 'Beobachtung — praeventiv'
        : empf.empfehlungs_typ === 'wohlfuehl_grenze' ? 'Beobachtung — Wohlfühl-Grenze'
        : 'Beobachtung'
      )
    : (
        empf.empfehlungs_typ === 'akut'        ? 'Gießempfehlung — akut'
        : empf.empfehlungs_typ === 'praeventiv' ? (
            empf.soll_bewaessern ? 'Gießempfehlung jetzt' : 'Würde jetzt gießen'
          )
        : empf.empfehlungs_typ === 'wohlfuehl_grenze' ? 'Wohlfühl-Grenze'
        : 'Kein Gießbedarf'
      )

  // Mini-Marker-Reihe: Welkepunkt | Optimum | Sensor | FK
  // Nur rendern wenn mindestens Welkepunkt + Sensor da sind.
  const zeigeMarker =
    empf.welkepunkt_wert !== null && empf.feuchte_aktuell !== null

  return (
    <div className={`gep-container ${statusKlasse}`}>
      {/* T-0120: Banner ueber der Empfehlung wenn gerade aktiv gegossen
          wird — sonst ist das ganze Panel verwirrend ("Wuerde jetzt
          giessen 48 min" waehrend bereits seit 15 min gegossen wird).
          Sensor-Wert + Empfehlung darunter werden in 5-15 min wieder
          frisch sein, sobald der Sensor den Sprung mitbekommen hat. */}
      {aktiverLauf && (
        <div className="gep-laufend-banner">
          <span aria-hidden="true">💧</span>
          {' '}
          Bewässerung läuft — seit{' '}
          {new Date(aktiverLauf.gestartet).toLocaleTimeString('de-DE', {
            hour: '2-digit', minute: '2-digit',
          })}
          , noch {Math.max(0, Math.round(aktiverLauf.verbleibend_s / 60))} min.
          Empfehlung aktualisiert sich, sobald Sensor den Sprung sieht.
        </div>
      )}
      <div className="gep-kopf">
        <span className="gep-titel">
          <span className="gep-typ-icon" aria-hidden="true">
            {typIcon(empf.empfehlungs_typ)}
          </span>
          {kopfTitel}
        </span>
        {/* T-0103: Strategie-Label nur wenn nicht Default-Korridor (= heutiges
            Verhalten — keine Aenderung bei Bestand). */}
        {empf.aktive_strategie !== 'korridor' && (
          <span className="gep-strategie" title="Bewässerungs-Strategie">
            Strategie: {strategieLabel(empf.aktive_strategie)}
          </span>
        )}
        {istBlockiert && empf.blocker_typ && (
          <span className="gep-blocker-icon" aria-hidden="true">
            {blockerIcon(empf.blocker_typ)}
          </span>
        )}
      </div>

      <div className="gep-inhalt">
        {/* Primärzeile: Dauer + Liter (nur wenn Empfehlung > 0).
            T-0089: liter_haupt passt zur tatsaechlich angezeigten Dauer;
            Quelle-Label unterscheidet ML / Kausal / Heuristik (vorher
            pauschal "Heuristik", obwohl dauer_haupt aus der kausalen
            dauer_s_empfehlung kam). */}
        {dauer_haupt !== null && empf.empfehlungs_typ !== 'kein_bedarf' && (
          <>
            <div className="gep-zeile gep-primaer">
              <span className="gep-dauer">{formatiereMinuten(dauer_haupt)}</span>
              <span className="gep-liter">
                {formatiereLiter(empf.liter_haupt ?? empf.liter_heuristik)}
              </span>
              <span className="gep-quelle">
                {mlPrimaer
                  ? `ML ${empf.modell_version ?? ''}`
                  : empf.dauer_s_empfehlung !== null
                    && empf.dauer_s_empfehlung !== empf.dauer_s_heuristik
                    ? 'Kausal'
                    : 'Heuristik'}
              </span>
            </div>
            {/* T-0085: Wirkungsrate-Quelle als kleine Hinweis-Zeile —
                "(aus letzten Bewaesserungen gemessen)" wenn Auto-
                Kalibrierung greift, "(Standardwert)" wenn noch keine
                Daten. Bei manuellem Konfig-Override (Default) bleibt
                die Zeile leer, sonst Rauschen im UI. */}
            {empf.delta_pp_pro_minute_wert !== null
              && empf.delta_pp_pro_minute_quelle !== 'manuell'
              && empf.delta_pp_pro_minute_quelle !== 'keine' && (
                <div className="gep-zeile gep-kleinschrift">
                  <span>
                    Wirkungsrate {empf.delta_pp_pro_minute_wert.toFixed(2)} pp/min
                    {wirkungsrateQuelleLabel(empf.delta_pp_pro_minute_quelle)}
                  </span>
                </div>
              )}

            {/* T-0086: Mehrfach-Takt-Hinweis wenn folge_dose vorhanden.
                Empfehlung "X min jetzt + Y min in Z h" als zweite Zeile. */}
            {empf.folge_dose_dauer_s !== null
              && empf.folge_dose_verzoegerung_h !== null && (
                <div className="gep-zeile gep-folge-dose">
                  <span>
                    + Folge-Bewässerung empfohlen:{' '}
                    {Math.round(empf.folge_dose_dauer_s / 60)} min in{' '}
                    {empf.folge_dose_verzoegerung_h.toFixed(0)} h
                    {empf.folge_dose_liter !== null
                      && ` ≈ ${empf.folge_dose_liter.toFixed(1)} L`}
                  </span>
                </div>
              )}

            {/* T-0291: Plateau-Transparenz. Die empfohlene Einzeldosis ist
                gedeckelt (~Max pp/Dosis); bei grossem Ziel-Abstand erreicht
                sie das Ziel NICHT in einem Schritt. Ehrlich anzeigen, damit
                eine flache Dauer nicht wie ein Bug wirkt. */}
            {empf.dosen_bis_ziel !== null
              && empf.dosen_bis_ziel > 1
              && empf.erwarteter_endwert_pp !== null && (
                <div className="gep-zeile gep-kleinschrift">
                  <span>
                    Diese Dosis hebt auf ≈ {empf.erwarteter_endwert_pp.toFixed(0)} %
                    {empf.einzeldosis_max_pp !== null
                      && ` (max +${empf.einzeldosis_max_pp.toFixed(0)} pp/Dosis)`}
                    {' '}— Ziel braucht ~{empf.dosen_bis_ziel} Dosen.
                  </span>
                </div>
              )}
          </>
        )}

        {/* Kein-Bedarf-Zeile prominent: Reserve-Tage gegen die strategie-
            spezifische Grenze (Wohl-Min bei HAEUFIG_KLEIN, sonst Welkepunkt).
            T-0105: nutzt tage_bis_reserve_grenze + reserve_grenze_label. */}
        {empf.empfehlungs_typ === 'kein_bedarf' && (
          (() => {
            const reserveTage = empf.tage_bis_reserve_grenze ?? empf.tage_bis_welkepunkt
            const label = empf.reserve_grenze_label ?? 'Welkepunkt'
            if (reserveTage === null || reserveTage === undefined) return null
            return (
              <div className="gep-zeile gep-primaer">
                <span className="gep-dauer">
                  Reserve {reserveTage.toFixed(0)} Tage
                </span>
                <span className="gep-quelle">über {label}</span>
              </div>
            )
          })()
        )}

        {/* Kausal-Erklärung — die eigentliche Aussage */}
        {empf.erklarung_lang ? (
          <div className="gep-erklaerung">
            {istBlockiert && (
              <span className="gep-blocker-hinweis">Blockiert: </span>
            )}
            {empf.erklarung_lang}
            {empf.welkepunkt_wert !== null && empf.welkepunkt_quelle !== 'manuell' && (
              <span className="gep-quelle-hinweis">
                {welkepunktQuelleLabel(empf.welkepunkt_quelle)}
              </span>
            )}
          </div>
        ) : (
          <div className="gep-grund">
            {istBlockiert && <span className="gep-blocker-hinweis">Blockiert: </span>}
            {empf.grund}
          </div>
        )}

        {/* Mini-Marker-Reihe (visuelle Verortung) */}
        {zeigeMarker && (
          <div className="gep-marker-reihe">
            <span className="gep-marker gep-marker-welke">
              Welke {empf.welkepunkt_wert!.toFixed(0)}
            </span>
            {empf.optimum_min !== null && empf.optimum_max !== null && (
              <span className="gep-marker gep-marker-wohl">
                Wohl {empf.optimum_min.toFixed(0)}–{empf.optimum_max.toFixed(0)}
              </span>
            )}
            <span className="gep-marker gep-marker-sensor">
              Du {empf.feuchte_aktuell!.toFixed(0)}
            </span>
            {/* T-0088: erwarteter Sensor-Wert nach Empfehlung
                (aktuelle_feuchte + dauer × wirkungsrate). Nur wenn
                Empfehlung > 0 UND Wirkungsrate bekannt. */}
            {empf.delta_pp_pro_minute_wert !== null
              && dauer_haupt !== null && dauer_haupt > 0
              && empf.feuchte_aktuell !== null && (
                <span
                  className="gep-marker gep-marker-erwartet"
                  title="Erwarteter Sensor-Wert nach der empfohlenen Dauer"
                >
                  Erwartet {Math.min(
                    100,
                    empf.feuchte_aktuell
                      + (dauer_haupt / 60) * empf.delta_pp_pro_minute_wert,
                  ).toFixed(0)}
                </span>
              )}
            {empf.feldkapazitaet_wert !== null && (
              <span className="gep-marker gep-marker-fk">
                FK {empf.feldkapazitaet_wert.toFixed(0)}
              </span>
            )}
          </div>
        )}

        {/* Sekundär: ML-Vergleich (nur wenn aktiv und vorhanden) */}
        {empf.ml_aktiv && empf.dauer_s_ml !== null && !mlPrimaer && empf.empfehlungs_typ !== 'kein_bedarf' && (
          <div className="gep-zeile gep-sekundaer">
            <span className="gep-kleinschrift">
              ML-Vergleich: {formatiereMinuten(empf.dauer_s_ml)}
              {empf.modell_version && ` (${empf.modell_version})`}
            </span>
            {empf.drift_ampel && (
              <span className={`gep-ampel gep-ampel-${empf.drift_ampel}`}>
                {ampelLabel(empf.drift_ampel)}
                {empf.drift_n_bewertet !== null && ` (n=${empf.drift_n_bewertet})`}
              </span>
            )}
          </div>
        )}

        {/* T-0164: ML aktiv aber kein Wert -> Klartext-Grund statt Lücke.
            T-0183: bei Monitoring-Zonen redundant — der Banner-Titel
            erklaert das schon, der ML-Hinweis waere doppelter Text. */}
        {empf.ml_aktiv && empf.dauer_s_ml === null && empf.ml_status_grund
          && empf.ml_status_grund !== 'ok' && !istMonitoring && (
          <div className="gep-zeile gep-sekundaer">
            <span className="gep-kleinschrift gep-ml-status">
              ML-Vergleich: {mlStatusLabel(empf.ml_status_grund)}
            </span>
          </div>
        )}
      </div>
    </div>
  )
}

/** T-0164: Klartext-Beschriftung für ML-Status-Grund (warum kein Wert). */
function mlStatusLabel(grund: string): string {
  switch (grund) {
    case 'sensor_ueber_ziel':
      return 'kein Vorschlag (Sensor schon am Ziel)'
    case 'kein_bedarf':
      return 'kein Vorschlag (kausal: kein Bedarf)'
    case 'modell_fehlt':
      return 'kein Modell für diese Zone'
    case 'inverse_kein_wert':
      return 'Modell konnte keine Dauer berechnen'
    case 'inferenz_fehler':
      return 'Modell-Fehler (siehe Logs)'
    case 'konfig_aus':
      return 'ML deaktiviert (Konfig)'
    case 'service_aus':
      return 'ML-Service nicht aktiv'
    case 'zone_monitoring':
      return 'kein Vorschlag (Zone in Monitoring)'
    case 'keine_messung':
      return 'kein Vorschlag (keine Messung)'
    case 'zone_unbekannt':
      return 'Zone unbekannt'
    default:
      return `kein Vorschlag (${grund})`
  }
}

export default GiessEmpfehlungPanel
