/* =========================================================
   ZonenKarteV4Drawer -- T-0221 Stage D, Detail-Drawer (L5/L6).
   Dritte Klick-Tiefe: Klick auf den Karten-Body oeffnet diesen
   rechten Slide-In. ESC / Backdrop / ✕ schliessen, Overlay legt
   sich ueber das Grid (Push-Modus = Folge-Arbeit).
   Inhalt: 30-Tage-Verlauf, FYTA-Optimum, ML (Drift + Top-5-Features +
   Prognose-vs-Ist), Wasser-Bilanz, Sub-Sensoren, Anomalien.
   Schwellen-Editor bewusst NICHT hier -- braucht einen Config-
   Write-Endpoint (Folge-Task T-0221-Backlog).
   Lazy geladen (App.tsx) -- zieht Recharts erst beim Oeffnen.
   ========================================================= */

import { useEffect, useState } from 'react'
import type {
  Zone, Messwert, MLVorhersage, SnapshotFenster, GiessEmpfehlung,
} from '../../typen'
import { FytaKpiBlock } from '../FytaKpiBlock'
import { SensorListeDiagnose } from '../SensorListeDiagnose'
import { MLDriftAmpel } from '../MLDriftAmpel'
import { MLAttributierungNeu } from '../MLAttributierungNeu'
import { MLPrognoseVsIstChart } from '../MLPrognoseVsIstChart'
import { WasserBilanz } from '../WasserBilanz'
import { FeuchteChartV3 } from './ZonenKarteV4Inspect'

interface Props {
  zone: Zone
  messwerte: Partial<Record<SnapshotFenster, Messwert[]>>
  mlVorhersage?: Record<string, MLVorhersage>
  empfehlung?: GiessEmpfehlung | null
  /** Viewport-Y der geklickten Karte -- der Drawer oeffnet auf dieser
   *  Hoehe statt in der oberen Ecke. */
  ankerY: number
  onSchliessen: () => void
}

/** Vertikale Position des Drawers: startet auf Hoehe der geklickten
 *  Karte (`ankerY`), klemmt aber so, dass mindestens MIN_HOEHE px
 *  sichtbar bleiben und der Drawer nicht unten aus dem Viewport laeuft.
 *  Ausserhalb der Komponente, damit der `window`-Zugriff den
 *  React-Hooks-Purity-Lint nicht ausloest. */
function berechneDrawerPosition(ankerY: number): { top: number; maxHeight: number } {
  const RAND = 8
  const MIN_HOEHE = 320
  const vh = typeof window !== 'undefined' ? window.innerHeight : 900
  const top = Math.max(RAND, Math.min(ankerY, vh - MIN_HOEHE - RAND))
  return { top, maxHeight: vh - top - RAND }
}

// Zeitraum-Umschalter wie in ZonenKarteV3Inspect -- bewusst dupliziert
// (ZonenKarteV3Inspect exportiert eine Komponente, ein zusaetzlicher
// Nicht-Komponenten-Export wuerde den Vite-react-refresh-Lint ausloesen).
const ZEITRAUM_LISTE: SnapshotFenster[] = ['24h', '48h', '7d', '30d']

// Klartext-Labels fuer Anomalie-Typen (gespiegelt aus ZonenKarteV3Glance
// WARNUNG_LABEL -- bewusst dupliziert, weil eine Komponenten-Datei wegen
// Vite-react-refresh keine Nicht-Komponenten exportieren soll).
const ANOMALIE_LABEL: Record<string, string> = {
  ausfall: 'Sensor-Ausfall',
  batterie_niedrig: 'Batterie niedrig',
  batterie_kritisch: 'Batterie kritisch',
  bewaesserung_ohne_wirkung: 'Bewässerung ohne Wirkung',
  sensor_eingefroren: 'Sensor eingefroren',
  // T-0445-Nachzug: diese Map ist die Kopie aus ZonenKarteV4Action.tsx und
  // hinkte ihr um zwei Typen hinterher -- ohne Eintrag rendert der Drawer
  // den rohen Enum-String. Gleiche Klasse wie der T-0433-Nachzug dort.
  fyta_kalibrier_push: 'FYTA-Kalibrierung verschoben',
  lead_divergenz: 'Ausgeschlossene Zone meldet kritisch',
  keine_ankunft_im_lauf: 'Kein neuer Messwert während des Laufs',
}

export default function ZonenKarteV4Drawer({
  zone, messwerte, mlVorhersage, empfehlung, ankerY, onSchliessen,
}: Props) {
  const { top, maxHeight } = berechneDrawerPosition(ankerY)
  // Zeitraum des Feuchte-Verlaufs -- Default 30d (Drawer = Maximal-
  // Ueberblick), aber umschaltbar wie in der inline-DETAILS-Schicht.
  const [zeitraum, setZeitraum] = useState<SnapshotFenster>('30d')
  // ESC schliesst den Drawer.
  useEffect(() => {
    const aufEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onSchliessen()
    }
    window.addEventListener('keydown', aufEsc)
    return () => window.removeEventListener('keydown', aufEsc)
  }, [onSchliessen])

  // Obs06 (T-0320): Hintergrund-Scroll-Lock, solange der Drawer offen ist --
  // bei einem Overlay-Drawer erwartet man, dass das Grid dahinter fixiert
  // ist (sonst scrollt der Hintergrund mit). Zusaetzlich ein einmaliger
  // Border-Puls auf der geklickten Karte als sichtbarer Bezug Karte ->
  // Drawer (haelt den Zusammenhang, auch wenn der Anker den Drawer hoch
  // klemmt). Dep `zone.zone_id`: bei Wechsel auf eine andere Karte feuert
  // der Puls neu, der Scroll-Lock bleibt.
  useEffect(() => {
    const vorherigesOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const karte = document.getElementById(`zone-${zone.zone_id}`)
    karte?.classList.add('zk4-puls')
    const t = window.setTimeout(() => karte?.classList.remove('zk4-puls'), 950)
    return () => {
      document.body.style.overflow = vorherigesOverflow
      window.clearTimeout(t)
      karte?.classList.remove('zk4-puls')
    }
  }, [zone.zone_id])

  // Q15: Welkepunkt aus dem Plateau-Modell pro Zone.
  const welkepunkt = empfehlung?.welkepunkt_wert ?? zone.feuchte_kritisch ?? null
  // Verlauf des gewaehlten Zeitraums; `hatVerlauf` = mind. eines der
  // Fenster hat Daten -> die Sektion (samt Toggle) wird gerendert.
  const aktuellerVerlauf = messwerte[zeitraum] ?? []
  const hatVerlauf = ZEITRAUM_LISTE.some(z => (messwerte[z]?.length ?? 0) > 1)
  const hatOptima = zone.optima != null && Object.keys(zone.optima).length > 0
  const hatSubSensoren = (zone.sensoren ?? []).length > 1
  const warnungen = zone.offene_warnungen ?? []
  const hatML = mlVorhersage != null && Object.keys(mlVorhersage).length > 0
  const hatFlaeche = zone.flaeche_m2 != null

  return (
    <>
      <div
        className="zk-drawer-backdrop"
        onClick={onSchliessen}
        aria-hidden="true"
      />
      <aside
        className="zk-drawer"
        style={{ top, maxHeight }}
        role="dialog"
        aria-modal="true"
        aria-label={`Details ${zone.name}`}
      >
        <div className="zk-drawer__kopf">
          <div>
            <div className="zk-drawer__lbl">DETAILS</div>
            <div className="zk-drawer__titel">{zone.name}</div>
          </div>
          <button
            type="button"
            className="zk-drawer__close"
            onClick={onSchliessen}
            aria-label="Drawer schliessen"
          >
            ✕
          </button>
        </div>

        <div className="zk-drawer__body">
          {hatVerlauf && (
            <section className="zk-drawer__sektion">
              <div className="zk-drawer__sektion-lbl">FEUCHTE-VERLAUF</div>
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
              {aktuellerVerlauf.length > 1 ? (
                <FeuchteChartV3
                  zone={zone}
                  messwerte={aktuellerVerlauf}
                  mlVorhersage={mlVorhersage}
                  welkepunkt={welkepunkt}
                />
              ) : (
                <div className="zk-drawer__sektion-lbl">
                  Kein Verlauf fuer {zeitraum} verfuegbar.
                </div>
              )}
            </section>
          )}

          {hatOptima && (
            <section className="zk-drawer__sektion">
              <div className="zk-drawer__sektion-lbl">FYTA-OPTIMUM · LIVE</div>
              <FytaKpiBlock optima={zone.optima} kompakt />
            </section>
          )}

          {hatML && (
            <section className="zk-drawer__sektion">
              <div className="zk-drawer__sektion-lbl">ML-DRIFT</div>
              <MLDriftAmpel zoneId={zone.zone_id} />
            </section>
          )}

          {hatML && (
            <section className="zk-drawer__sektion">
              <div className="zk-drawer__sektion-lbl">ML · TOP-5 FEATURES</div>
              <MLAttributierungNeu
                zoneId={zone.zone_id}
                horizonte={[24]}
                vorhersagenPropagiert={mlVorhersage}
                versteckeToggle
              />
            </section>
          )}

          {hatML && (
            <section className="zk-drawer__sektion">
              <div className="zk-drawer__sektion-lbl">ML · PROGNOSE VS. IST</div>
              <MLPrognoseVsIstChart zoneId={zone.zone_id} versteckeToggle />
            </section>
          )}

          {hatFlaeche && (
            <section className="zk-drawer__sektion">
              {/* WasserBilanz bringt einen eigenen "Wasser-Bilanz"-Header
                  mit -- kein zusaetzliches Sektion-Label. */}
              <WasserBilanz zoneId={zone.zone_id} />
            </section>
          )}

          {hatSubSensoren && (
            <section className="zk-drawer__sektion">
              <div className="zk-drawer__sektion-lbl">SUB-SENSOREN</div>
              <SensorListeDiagnose sensoren={zone.sensoren!} />
            </section>
          )}

          {warnungen.length > 0 && (
            <section className="zk-drawer__sektion">
              <div className="zk-drawer__sektion-lbl">ANOMALIEN · OFFEN</div>
              <ul className="zk-drawer__anomalien">
                {warnungen.map((w, i) => (
                  <li key={`${w.typ}-${i}`}>
                    <b>{ANOMALIE_LABEL[w.typ] ?? w.typ}</b>
                    {w.details ? <span> — {w.details}</span> : null}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {!hatVerlauf && !hatOptima && !hatML && !hatFlaeche
            && !hatSubSensoren && warnungen.length === 0 && (
            <div className="zk-drawer__sektion-lbl">
              Keine Detail-Daten fuer diese Zone verfuegbar.
            </div>
          )}
        </div>
      </aside>
    </>
  )
}
