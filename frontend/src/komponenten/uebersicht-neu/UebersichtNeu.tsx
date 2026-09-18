import { lazy, Suspense, useEffect, useRef, useState, type RefObject } from 'react'
import type { DashboardSnapshotZone, MLStatus, Standort, Wetter, Zone } from '../../typen'
import { dauerHauptSekunden, strategieKlartext } from '../../hilfsfunktionen'
import { prognoseUngueltigLabel } from '../ml_prognose_guete'
import { ManuellesGiessen } from '../ManuellesGiessen'
import { ZonenKarteV4Action } from '../zonen-karte-v4/ZonenKarteV4Action'
import { UnbekanntEventBanner } from '../UnbekanntEventBanner'
import { TagesplanBlock } from '../TagesplanBlock'
import { PflegeErinnerungenBlock } from '../PflegeErinnerungenBlock'
import { MLRetrainStatusKachel } from '../MLRetrainStatusKachel'
import { FehlerGrenze } from '../FehlerGrenze'
import { WetterKarte } from '../WetterKarte'
import { baueZonenAnzeige, betriebsModus, messQuelle, zeitAlter, alterStunden, type ZonenAnzeige } from './uebersicht-modell'
import './uebersicht-neu.css'

const Verlauf = lazy(() => import('../zonen-karte-v4/ZonenKarteV4Inspect'))
const LEERE_MESSWERTE = Object.freeze({})

interface Props {
  zonen: Zone[]
  standorte: Standort[]
  snapshots: Record<string, DashboardSnapshotZone>
  snapshotZeit: string | null
  snapshotFehler: string | null
  zonenFehler: string | null
  aktiveZonen: Set<string>
  ventilStatusFehler: string | null
  wetterMap: Record<string, Wetter>
  mlVerfuegbar: boolean
  mlStatus: MLStatus | null
  dswcLabelProZone: Record<string, string | null>
}

export function UebersichtNeu({
  zonen, standorte, snapshots, snapshotZeit, snapshotFehler, zonenFehler,
  aktiveZonen, ventilStatusFehler, wetterMap, mlVerfuegbar, mlStatus, dswcLabelProZone,
}: Props) {
  const [standortId, setStandortId] = useState('alle')
  const [nurHinweise, setNurHinweise] = useState(false)
  const [auswahl, setAuswahl] = useState<string | null>(null)
  const [mobilDetails, setMobilDetails] = useState(false)
  const [planOffen, setPlanOffen] = useState(false)
  const [jetzt, setJetzt] = useState(() => Date.now())
  const detailRef = useRef<HTMLHeadingElement>(null)
  const auswahlRef = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    const timer = window.setInterval(() => setJetzt(Date.now()), 30_000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    if (mobilDetails) detailRef.current?.focus()
  }, [mobilDetails, auswahl])

  const snapshotAlter = alterStunden(snapshotZeit, jetzt)
  const snapshotAktuell = !snapshotFehler && snapshotAlter != null && snapshotAlter <= 2 / 60
  const anzeigen = zonen.map(z => baueZonenAnzeige(z, snapshots[z.zone_id], aktiveZonen.has(z.zone_id), snapshotAktuell, jetzt))
  const zonenReihenfolge = new Map(standorte.flatMap(s => s.zonen).map((id, i) => [id, i]))
  anzeigen.sort((a, b) => (zonenReihenfolge.get(a.zone.zone_id) ?? Infinity) - (zonenReihenfolge.get(b.zone.zone_id) ?? Infinity))
  const hinweise = anzeigen.filter(a => a.aufmerksamkeit).length
  const kritische = anzeigen.filter(a => a.ton === 'kritisch').length
  const standort = standorte.find(s => s.standort_id === standortId)
  const sichtbar = anzeigen.filter(a => (!standort || standort.zonen.includes(a.zone.zone_id)) && (!nurHinweise || a.aufmerksamkeit))
  if (nurHinweise) sichtbar.sort((a, b) => a.prioritaet - b.prioritaet)
  const gewaehlt = sichtbar.find(a => a.zone.zone_id === auswahl) ?? sichtbar[0]
  const standortFuer = (id: string) => standorte.find(s => s.zonen.includes(id))
  const standortName = (id: string) => standortFuer(id)?.name ?? 'Standort nicht zugeordnet'
  const wetterStandort = gewaehlt ? standortFuer(gewaehlt.zone.zone_id) : undefined
  const wetter = wetterStandort ? wetterMap[wetterStandort.wetter_standort] : undefined
  const listeZurueck = () => {
    setMobilDetails(false)
    requestAnimationFrame(() => auswahlRef.current?.focus())
  }

  return (
    <main className={`un ${mobilDetails ? 'un--mobil-details' : ''}`}>
      <div className="un-kopf">
        <div><p className="un-oberzeile">PFLANZEN & BEWÄSSERUNG</p><h2>Dein Garten im Blick.</h2></div>
        <p className="un-meta">{zonen.length} Zonen · {snapshotZeit ? `Datenstand ${new Date(snapshotZeit).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })}` : 'Daten werden geladen'}</p>
      </div>

      {(snapshotFehler || (snapshotZeit && !snapshotAktuell)) && (
        <div className="un-fehler" role="status">Bewertung nicht aktuell. {snapshotFehler || 'Der letzte Snapshot ist älter als zwei Minuten.'} Empfehlungen und Prognosen werden bis zum nächsten erfolgreichen Abruf ausgesetzt.</div>
      )}
      {ventilStatusFehler && <div className="un-fehler" role="alert">{ventilStatusFehler}</div>}

      <div className={`un-heute ${kritische ? 'un-heute--kritisch' : ''}`} role="status">
        <strong>{!snapshotAktuell ? (snapshotZeit ? 'Bewertung wird aktualisiert' : 'Bewertung wird geladen') : `${hinweise} ${hinweise === 1 ? 'Zone braucht' : 'Zonen brauchen'} Aufmerksamkeit`}</strong>
        <span>{kritische > 0 ? `${kritische} kritisch · ` : ''}{aktiveZonen.size > 0 ? `${aktiveZonen.size} Zonen werden bewässert` : 'Hinweise, Messwerte und Empfehlungen gemeinsam prüfen'}</span>
      </div>

      <div className="un-auswahlleiste">
        <label>Standort<select value={standortId} onChange={e => { setStandortId(e.target.value); setMobilDetails(false) }}><option value="alle">Alle Standorte</option>{standorte.map(s => <option key={s.standort_id} value={s.standort_id}>{s.name}</option>)}</select></label>
        <div className="un-filter" role="group" aria-label="Zonen filtern">
          <button type="button" aria-pressed={!nurHinweise} onClick={() => { setNurHinweise(false); setMobilDetails(false) }}>Alle Zonen</button>
          <button type="button" aria-pressed={nurHinweise} onClick={() => { setNurHinweise(true); setMobilDetails(false) }}>Mit Hinweisen</button>
        </div>
      </div>

      {zonen.length === 0 ? <p className="un-leer" role="status">{zonenFehler || 'Zonen werden geladen …'}</p>
        : sichtbar.length === 0 ? <p className="un-leer" role="status">Keine Zonen für diese Auswahl. Mit „Alle Zonen“ siehst du den gesamten Standort.</p>
        : <div className="un-layout">
          <div className="un-liste" role="group" aria-label="Zone auswählen">
            {sichtbar.map((a, i) => {
              const z = a.zone
              const gruppenStart = !standort && !nurHinweise && (i === 0 || standortName(z.zone_id) !== standortName(sichtbar[i - 1].zone.zone_id))
              return <div key={z.zone_id}>
                {gruppenStart && <h3 className="un-gruppe">{standortName(z.zone_id)}</h3>}
                <button type="button" className={`un-zone un-ton--${a.ton}`} aria-pressed={gewaehlt?.zone.zone_id === z.zone_id} aria-controls="un-detail" onClick={e => { auswahlRef.current = e.currentTarget; setAuswahl(z.zone_id); setMobilDetails(true) }}>
                  <span className="un-zone-kopf"><span>{z.name}</span><span className="un-zone-wert">{a.datenBelastbar && z.aktuelle_feuchte != null ? <>{Math.round(z.aktuelle_feuchte)}<small> %</small></> : '—'}</span></span>
                  <span className="un-zone-status"><span className="un-punkt" />{a.titel}</span>
                  <span className="un-zone-meta">{nurHinweise && !standort ? `${standortName(z.zone_id)} · ` : ''}{betriebsModus(z)} · {zeitAlter(z.letztes_update ?? z.letzter_bekannter_zeit, jetzt)}</span>
                </button>
              </div>
            })}
          </div>
          {gewaehlt && <section className="un-detail" id="un-detail" aria-label={`Details ${gewaehlt.zone.name}`}>
            <button type="button" className="un-zurueck" onClick={listeZurueck}>← Zur Zonenliste</button>
            <FehlerGrenze key={gewaehlt.zone.zone_id} titel={`Details ${gewaehlt.zone.name} konnten nicht geladen werden`}>
              <ZonenDetails
                anzeige={gewaehlt} jetzt={jetzt} mlVerfuegbar={mlVerfuegbar}
                aktiv={aktiveZonen.has(gewaehlt.zone.zone_id)} snapshotAktuell={snapshotAktuell}
                dswcLabel={dswcLabelProZone[gewaehlt.zone.zone_id]}
                standortName={standortName(gewaehlt.zone.zone_id)}
                wetter={wetter} detailRef={detailRef}
              />
            </FehlerGrenze>
          </section>}
        </div>}

      <details className="un-plan" open={planOffen} onToggle={e => setPlanOffen(e.currentTarget.open)}>
        <summary>Tagesplan, Pflege und Systemstatus</summary>
        {planOffen && <><TagesplanBlock aktiveZonen={aktiveZonen} zonen={zonen} /><PflegeErinnerungenBlock zonen={zonen} /><MLRetrainStatusKachel status={mlStatus} /></>}
      </details>
    </main>
  )
}

function ZonenDetails({ anzeige: a, jetzt, mlVerfuegbar, aktiv, snapshotAktuell, dswcLabel, standortName, wetter, detailRef }: {
  anzeige: ZonenAnzeige; jetzt: number; mlVerfuegbar: boolean; aktiv: boolean; snapshotAktuell: boolean
  dswcLabel?: string | null; standortName: string; wetter?: Wetter
  detailRef: RefObject<HTMLHeadingElement | null>
}) {
  const [verlaufOffen, setVerlaufOffen] = useState(false)
  const [wetterOffen, setWetterOffen] = useState(false)
  const z = a.zone
  const letzteFeuchte = z.aktuelle_feuchte ?? z.letzter_bekannter_wert
  const quelle = messQuelle(z)
  const zeit = z.letztes_update ?? z.letzter_bekannter_zeit
  const roh = a.snapshot?.ml_vorhersage?.['24h']
  const min = z.feuchte_schwelle_min
  const max = z.feuchte_schwelle_max
  const begrenze = (wert: number) => Math.min(100, Math.max(0, wert))
  const sensorId = z.feuchte_geraet_id
  // Ungueltige Prognosen werden auch im bestehenden Detailchart nicht als
  // aktuelle Kurve weitergereicht. Die Messhistorie bleibt lesbar.
  const ml = a.datenBelastbar && snapshotAktuell ? a.snapshot?.ml_vorhersage : undefined

  return <>
    <div className="un-detail-kopf">
      <div><p className="un-meta">{standortName} · {betriebsModus(z)}</p><h2 ref={detailRef} tabIndex={-1}>{z.name}</h2></div>
      <span className={`un-status un-ton--${a.ton}`}><span className="un-punkt" />{a.titel}</span>
    </div>
    <div className="un-messung">
      <div className={`un-wert ${!a.datenBelastbar ? 'un-wert--unsicher' : ''}`}>{letzteFeuchte != null ? <>{Math.round(letzteFeuchte)}<small> %</small></> : '—'}</div>
      <div><strong>{a.datenBelastbar ? 'Gemessene Feuchte' : 'Letzter Wert · nicht belastbar'}</strong><p className="un-meta">{quelle}</p><p className="un-meta">{zeitAlter(zeit, jetzt)}</p></div>
    </div>
    <p className="un-begruendung">{a.detail}</p>
    {a.datenBelastbar && z.aktuelle_feuchte != null && <div className="un-skala" role="img" aria-label={`Sensorskala 0 bis 100 Prozent, Messwert ${z.aktuelle_feuchte}, Untergrenze ${min}${max != null ? `, Obergrenze ${max}` : ', keine Obergrenze festgelegt'}`}>
      <div className="un-skala-label"><span>Feuchte · Sensorskala</span><span>{max != null ? `Zielbereich ${min}–${max} %` : `Untergrenze ${min} % · ohne Obergrenze`}</span></div>
      <div className="un-spur">{max != null && <span className="un-band" style={{ left: `${begrenze(min)}%`, width: `${Math.max(0, begrenze(max) - begrenze(min))}%` }} />}<span className="un-grenze" style={{ left: `${begrenze(min)}%` }} /><span className="un-ist" style={{ left: `${begrenze(z.aktuelle_feuchte)}%` }} /></div>
      <div className="un-skala-label"><span>0 %</span><span>100 %</span></div>
    </div>}

    <div className="un-prognose">
      <div><p className="un-meta">Modellprognose in 24 Stunden</p><strong>{a.prognose ? `${Math.round(a.prognose.feuchte_prognose)} %` : '—'}</strong></div>
      <p className="un-meta">{a.prognose ? a.prognose.q10 != null && a.prognose.q90 != null ? `Modellspanne ${Math.round(a.prognose.q10)}–${Math.round(a.prognose.q90)} %` : 'Keine Modellspanne verfügbar'
        : roh?.gueltig === false ? `Nicht verwendbar: ${prognoseUngueltigLabel(roh)}`
        : a.snapshot?.ml_vorhersage_fehler || 'Keine belastbare Prognose verfügbar'}</p>
    </div>

    <section className="un-steuerung" aria-label={`Bewässerung ${z.name}`}>
      <h3>Bewässerung</h3>
      {a.empfehlung && !aktiv && <ZonenKarteV4Action zone={z} empfehlung={a.empfehlung} onAktion={() => {}} bewaesserungAktiv={aktiv} />}
      {!a.empfehlung && <p className="un-meta">{a.titel}. Manuelle Bedienung bleibt eine eigene Entscheidung.</p>}
      {aktiv && <p className="un-meta">Die laufende Bewässerung hat Vorrang. Status und Stopp erscheinen unten.</p>}
      <ManuellesGiessen
        zoneId={z.zone_id} hatVentilKanal={z.ventil_kanal != null}
        preSoakDefaultMin={z.pre_soak_min ?? null} preSoakPauseMin={z.pre_soak_pause_min ?? null}
        loggingEinheit={z.logging_einheit ?? 'sekunden'} loggingOptionenMl={z.logging_optionen_ml}
        empfDauerSec={a.empfehlung ? dauerHauptSekunden(a.empfehlung) : null}
      />
      <p className="un-bedienhinweis">„Gegossen“ erfasst eine erfolgte Wassergabe.{z.ventil_kanal != null ? ' „Live starten“ und „Pre-Soak“ öffnen die Ventilsteuerung.' : ''}</p>
    </section>

    <section className="un-datenbasis" aria-label="Datenbasis">
      <h3>Datenbasis</h3>
      {z.sensoren?.length ? <ul>{z.sensoren.map(s => <li key={s.geraet_id}><div><strong>{s.name || z.sensor_namen?.[s.geraet_id] || s.geraet_id}</strong><span className="un-meta">{s.quelle === 'fyta' ? 'FYTA' : s.quelle === 'gardena' ? 'Gardena' : 'Sensor'} · {s.geraet_id === z.aggregat_lead_geraet ? 'maßgeblicher Sensor' : s.geraet_id === sensorId ? 'angezeigte Messung' : 'ergänzend'} · {zeitAlter(s.zeitstempel, jetzt)}</span></div><span className="un-sensor-wert">{s.boden_feuchte != null ? `${Math.round(s.boden_feuchte)} %` : '—'}</span></li>)}</ul> : <p className="un-meta">Keine aktuellen Einzelmessungen vorhanden. {quelle}.</p>}
      {(z.sensoren?.length ?? 0) > 1 && <p className="un-meta">Die Quellen bleiben getrennt. Unterschiedliche Sensorskalen dürfen nicht direkt gegeneinander bewertet werden.</p>}
      {(z.offene_warnungen?.length ?? 0) > 0 && <ul className="un-warnungen">{z.offene_warnungen!.map((w, i) => <li key={`${w.typ}-${i}`}>{w.details || w.typ.replaceAll('_', ' ')}</li>)}</ul>}
      <p className="un-technik">{[dswcLabel, z.ventil_kanal != null ? `Kanal ${z.ventil_kanal}` : null, z.flaeche_m2 != null ? `${z.flaeche_m2} m²` : null, strategieKlartext(a.empfehlung?.aktive_strategie)].filter(Boolean).join(' · ')}</p>
      {a.empfehlung?.grund && <details className="un-systemgrund"><summary>Begründung aus dem System</summary><p className="un-meta">{a.empfehlung.grund}</p></details>}
    </section>

    <UnbekanntEventBanner zoneId={z.zone_id} istAquabloom={!!z.aquabloom_konfig} />
    <details className="un-analyse" open={verlaufOffen} onToggle={e => setVerlaufOffen(e.currentTarget.open)}>
      <summary>Verlauf und Analyse <span>24 h bis 30 Tage · Sensoren · ML · Wasserbilanz</span></summary>
      {verlaufOffen && <Suspense fallback={<p className="un-meta">Verlauf wird geladen …</p>}><Verlauf zone={z} messwerte={a.snapshot?.messwerte ?? LEERE_MESSWERTE} mlVorhersage={ml} mlVerfuegbar={mlVerfuegbar} empfehlung={a.empfehlung} /></Suspense>}
    </details>
    {wetter && <details className="un-analyse" open={wetterOffen} onToggle={e => setWetterOffen(e.currentTarget.open)}><summary>Wetter am Standort</summary>{wetterOffen && <WetterKarte wetter={wetter} titel={`Wetter ${standortName}`} />}</details>}
  </>
}
