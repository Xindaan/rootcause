/* DetailsDrawer — T-0184.
 *
 * Drawer fuer Ops-Tab "Details"-Button mit drei Tabs:
 *   - Sensor:  Feuchte ±2 h um das Event (Recharts-Linie)
 *   - ML:      Aktuelle ML-Vorhersage fuer die Zone (Snapshot)
 *   - Wetter:  Wetter-Forecast am Standort der Zone
 *
 * Datenquellen sind alle GET-Endpoints (kein destruktiver Call):
 *   - /api/zonen/{id}/messwerte?von=&bis=         (T-0184 Range-Modus)
 *   - /api/ml/vorhersage/{id}                     (T-0046)
 *   - /api/wetter/{standort_id}                   (Open-Meteo Forecast-Cache)
 */

import { useEffect, useMemo, useState } from 'react'
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ReferenceLine, ResponsiveContainer,
} from 'recharts'
import type {
  VentilEreignisDetail,
  Messwert,
  MLVorhersage,
  Standort,
  Wetter,
  Zone,
} from '../typen'
import { datumZeitFormat } from '../hilfsfunktionen'
import {
  holeMesswerteRange,
  holeMLVorhersage,
  holeStandorte,
  holeWetter,
} from '../api'
import { FytaKpiBlock } from './FytaKpiBlock'
import './DetailsDrawer.css'

interface Props {
  ereignis: VentilEreignisDetail | null
  onClose: () => void
  /** T-0196: Optional, fuer FYTA-KPI-Block-Lookup ueber ereignis.zone_id.
   *  Parent (OpsTabNeu) hat die Liste bereits geladen — kein eigener
   *  API-Fetch im Drawer noetig. */
  alleZonen?: Zone[]
}

type TabName = 'sensor' | 'ml' | 'wetter'

function isoMinusStunden(iso: string, stunden: number): string {
  return new Date(new Date(iso).getTime() - stunden * 3600_000).toISOString()
}

function isoPlusStunden(iso: string, stunden: number): string {
  return new Date(new Date(iso).getTime() + stunden * 3600_000).toISOString()
}

export function DetailsDrawer({ ereignis, onClose, alleZonen }: Props) {
  const [tab, setTab] = useState<TabName>('sensor')
  const [sensor, setSensor] = useState<Messwert[]>([])
  const [sensorFehler, setSensorFehler] = useState<string | null>(null)
  const [ml, setMl] = useState<Record<string, MLVorhersage>>({})
  const [mlFehler, setMlFehler] = useState<string | null>(null)
  const [wetter, setWetter] = useState<Wetter | null>(null)
  const [wetterFehler, setWetterFehler] = useState<string | null>(null)
  const [standorte, setStandorte] = useState<Standort[]>([])

  // Standorte einmal laden (fuer zone -> wetter_standort Mapping).
  useEffect(() => {
    let abgebrochen = false
    holeStandorte()
      .then(s => { if (!abgebrochen) setStandorte(s) })
      .catch(() => { if (!abgebrochen) setStandorte([]) })
    return () => { abgebrochen = true }
  }, [])

  // Sensor-Verlauf ±2h um das Event. Reset auf Old-State bewusst NICHT
  // synchron im Effect — React-Hooks-Regel "set-state-in-effect"; der
  // alte Chart bleibt kurz sichtbar bis der neue Fetch resolved, was
  // beim Klick zwischen Events kaum wahrnehmbar ist.
  useEffect(() => {
    if (!ereignis) return
    let abgebrochen = false
    const von = isoMinusStunden(ereignis.zeitstempel, 2)
    const bis = isoPlusStunden(ereignis.zeitstempel, 2)
    holeMesswerteRange(ereignis.zone_id, von, bis)
      .then(d => { if (!abgebrochen) { setSensor(d); setSensorFehler(null) } })
      .catch(e => { if (!abgebrochen) { setSensor([]); setSensorFehler(String(e)) } })
    return () => { abgebrochen = true }
  }, [ereignis])

  // ML-Vorhersage (Snapshot — die API liefert keine historische Vorhersage,
  // sondern nur die aktuelle. Wir nutzen sie als "Kontext jetzt").
  useEffect(() => {
    if (!ereignis) return
    let abgebrochen = false
    holeMLVorhersage(ereignis.zone_id)
      .then(d => { if (!abgebrochen) { setMl(d); setMlFehler(null) } })
      .catch(e => { if (!abgebrochen) { setMl({}); setMlFehler(String(e)) } })
    return () => { abgebrochen = true }
  }, [ereignis])

  // Standort-Validierung als reine Ableitung -> useMemo statt useEffect+setState.
  const standortDerZone = useMemo(
    () => ereignis ? standorte.find(s => s.zonen.includes(ereignis.zone_id)) : undefined,
    [ereignis, standorte],
  )
  const standortFehler = useMemo(
    () => ereignis && standorte.length > 0 && !standortDerZone
      ? 'Kein Standort fuer diese Zone konfiguriert' : null,
    [ereignis, standorte.length, standortDerZone],
  )

  // Wetter-Forecast am Standort der Zone.
  useEffect(() => {
    if (!ereignis) return
    if (!standortDerZone) return
    let abgebrochen = false
    holeWetter(standortDerZone.wetter_standort)
      .then(d => { if (!abgebrochen) { setWetter(d); setWetterFehler(null) } })
      .catch(e => { if (!abgebrochen) { setWetter(null); setWetterFehler(String(e)) } })
    return () => { abgebrochen = true }
  }, [ereignis, standortDerZone])

  if (!ereignis) return null

  const eventTs = new Date(ereignis.zeitstempel).getTime()
  const chartDaten = sensor
    .filter(m => m.boden_feuchte !== null)
    .map(m => ({
      zeit: new Date(m.zeitstempel).getTime(),
      feuchte: m.boden_feuchte,
    }))

  // T-0196: Zone fuer KPI-Block ueber ereignis.zone_id aufloesen
  // (Parent OpsTabNeu hat alleZonen schon geladen — kein eigener Fetch
  // im Drawer). Bei Gardena-Zonen oder fehlenden optima rendert
  // FytaKpiBlock nichts.
  const zoneDesEvents = alleZonen?.find(z => z.zone_id === ereignis.zone_id)

  return (
    <>
      <div className="dd-overlay" onClick={onClose} aria-hidden />
      <aside className="dd-panel" role="dialog" aria-modal="true">
        <header className="dd-kopf">
          <h3>Event-Details</h3>
          <button
            type="button"
            className="dd-close"
            onClick={onClose}
            aria-label="Drawer schließen"
          >
            ✕
          </button>
        </header>

        <dl className="dd-liste">
          <dt>Zeit</dt>
          <dd>{datumZeitFormat(ereignis.zeitstempel)}</dd>
          <dt>Zone</dt>
          <dd>{ereignis.zone_id}</dd>
          <dt>Aktion</dt>
          <dd>{ereignis.aktion}</dd>
          <dt>Dauer</dt>
          <dd>{ereignis.dauer_sekunden > 0 ? `${ereignis.dauer_sekunden} s` : '—'}</dd>
          {ereignis.liter != null && (
            <>
              <dt>Liter</dt>
              <dd>{ereignis.liter} L</dd>
            </>
          )}
          <dt>Auslöser</dt>
          <dd>{ereignis.ausloser}</dd>
          <dt>Quelle</dt>
          <dd>{ereignis.ventil_id}</dd>
        </dl>

        <div className="dd-tabs" role="tablist">
          <button
            role="tab"
            aria-selected={tab === 'sensor'}
            className={`dd-tab ${tab === 'sensor' ? 'aktiv' : ''}`}
            onClick={() => setTab('sensor')}
          >
            Sensor ±2 h
          </button>
          <button
            role="tab"
            aria-selected={tab === 'ml'}
            className={`dd-tab ${tab === 'ml' ? 'aktiv' : ''}`}
            onClick={() => setTab('ml')}
          >
            ML-Prognose
          </button>
          <button
            role="tab"
            aria-selected={tab === 'wetter'}
            className={`dd-tab ${tab === 'wetter' ? 'aktiv' : ''}`}
            onClick={() => setTab('wetter')}
          >
            Wetter
          </button>
        </div>

        <div className="dd-tab-inhalt" role="tabpanel">
          {tab === 'sensor' && (
            <div className="dd-sensor">
              {sensorFehler && <p className="dd-fehler">{sensorFehler}</p>}
              {!sensorFehler && chartDaten.length < 2 && (
                <p className="dd-hinweis">Keine Sensor-Daten im ±2h-Fenster.</p>
              )}
              {chartDaten.length >= 2 && (
                <ResponsiveContainer width="100%" height={200}>
                  <AreaChart data={chartDaten}>
                    <defs>
                      <linearGradient id={`dd-grad-${ereignis.id}`} x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="var(--chart-feuchte)" stopOpacity={0.2} />
                        <stop offset="95%" stopColor="var(--chart-feuchte)" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-gitter)" />
                    <XAxis
                      dataKey="zeit" type="number" scale="time"
                      domain={['dataMin', 'dataMax']}
                      stroke="var(--text-gedaempft)" fontSize={11}
                      tickFormatter={(ts: number) =>
                        new Date(ts).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
                      }
                    />
                    <YAxis
                      domain={[0, 100]}
                      stroke="var(--text-gedaempft)" fontSize={11}
                      tickFormatter={(v: number) => `${v}%`}
                    />
                    <Tooltip
                      labelFormatter={(ts) => new Date(Number(ts)).toLocaleString('de-DE', {
                        day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
                      })}
                      formatter={(value) => typeof value === 'number'
                        ? [`${value.toFixed(0)}%`, 'Feuchte']
                        : [value, 'Feuchte']
                      }
                    />
                    <ReferenceLine
                      x={eventTs}
                      stroke="var(--farbe-info)"
                      strokeDasharray="3 3"
                      label={{
                        value: ereignis.aktion,
                        position: 'top',
                        fill: 'var(--farbe-info)',
                        fontSize: 11,
                      }}
                    />
                    <Area
                      type="monotone"
                      dataKey="feuchte"
                      stroke="var(--chart-feuchte)"
                      strokeWidth={2}
                      fill={`url(#dd-grad-${ereignis.id})`}
                      isAnimationActive={false}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              )}
              {/* T-0196d/e: FYTA-Pflanzen-Status (Licht, DLI, Temperatur,
                  Salinity, Feuchte) als KPI-Block — identisch zur Karten-
                  Sicht (ZonenKarteNeu). Werte aus zone.optima[].current,
                  vom PlantOptimumJob gepflegt. Zone-Lookup ueber
                  ereignis.zone_id, Parent (OpsTabNeu) hat alleZonen
                  bereits geladen — kein eigener Fetch. */}
              {zoneDesEvents && zoneDesEvents.quelle === 'fyta' && (
                <FytaKpiBlock optima={zoneDesEvents.optima} kompakt />
              )}
            </div>
          )}

          {tab === 'ml' && (
            <div className="dd-ml">
              {mlFehler && <p className="dd-fehler">{mlFehler}</p>}
              {!mlFehler && Object.keys(ml).length === 0 && (
                <p className="dd-hinweis">Keine ML-Vorhersage für diese Zone verfügbar.</p>
              )}
              {Object.entries(ml).map(([key, v]) => (
                <div key={key} className="dd-ml-zeile">
                  <span className="dd-ml-horizont">{key}</span>
                  <span className="dd-ml-wert">{v.feuchte_prognose.toFixed(0)}%</span>
                  {v.q10 != null && v.q90 != null && (
                    <span className="dd-ml-band">
                      ({v.q10.toFixed(0)}–{v.q90.toFixed(0)} %)
                    </span>
                  )}
                </div>
              ))}
              {Object.keys(ml).length > 0 && (
                <p className="dd-hinweis-klein">
                  Snapshot der aktuellen Prognose — historische ML-Werte zum Event-Zeitpunkt sind im Drift-Log.
                </p>
              )}
            </div>
          )}

          {tab === 'wetter' && (
            <div className="dd-wetter">
              {(wetterFehler || standortFehler) && (
                <p className="dd-fehler">{wetterFehler ?? standortFehler}</p>
              )}
              {!wetterFehler && !standortFehler && !wetter && (
                <p className="dd-hinweis">Wetter wird geladen…</p>
              )}
              {wetter && (
                <>
                  <div className="dd-wetter-kpi">
                    <div>
                      <span className="dd-wetter-label">Regen 6 h</span>
                      <span className="dd-wetter-wert">{wetter.niederschlag_6h_mm.toFixed(1)} mm</span>
                    </div>
                    <div>
                      <span className="dd-wetter-label">ET0 6 h</span>
                      <span className="dd-wetter-wert">{wetter.et0_6h_mm.toFixed(1)} mm</span>
                    </div>
                    <div>
                      <span className="dd-wetter-label">Wind 6 h</span>
                      <span className="dd-wetter-wert">{wetter.wind_6h_kmh.toFixed(0)} km/h</span>
                    </div>
                  </div>
                  <p className="dd-hinweis-klein">
                    Forecast-Snapshot — gilt fuer die naechsten 6 h ab jetzt, nicht zum Event-Zeitpunkt.
                  </p>
                </>
              )}
            </div>
          )}
        </div>
      </aside>
    </>
  )
}
