/* T-0238: Betriebsstatus-Zentrale.
 *
 * Eine Kachel im Ops-Tab, die System-Health-Daten an einer Stelle
 * zeigt: Endpoint-Health (DHS + FYTA), Husqvarna-Cadence,
 * ML-Retrain-Stand, Backup-Snapshot, letzter Watchdog-Push.
 * Schliesst auch T-0246-Scope ab (Backend-Version sichtbar im UI).
 *
 * Vor T-0238 musste der User SQLite-CLI + Logs + mehrere Tabs
 * konsultieren, um zu sehen, ob das System gerade gesund laeuft.
 *
 * Quelle: /api/ops/betriebsstatus (60 s Polling).
 */

import { useEffect, useState } from 'react'
import { holeBetriebsstatus } from '../api'
import { istAbbruch } from '../hilfsfunktionen'
import { starteSichtbarkeitsIntervall } from '../sichtbarkeitsPolling'
import type { Betriebsstatus } from '../typen'
import './BetriebsstatusKachel.css'

function relativDauer(iso: string | null): string {
  if (!iso) return '—'
  const dann = new Date(iso).getTime()
  if (Number.isNaN(dann)) return iso
  const min = Math.round((Date.now() - dann) / 60_000)
  if (min < 1) return 'gerade eben'
  if (min < 60) return `vor ${min} min`
  const std = Math.round(min / 60)
  if (std < 48) return `vor ${std} h`
  return `vor ${Math.round(std / 24)} d`
}

function statusKlasse(status: string): string {
  if (status === 'ok') return 'bs-status-ok'
  if (status.includes('auth') || status.includes('schema')) return 'bs-status-fehler'
  if (status.includes('connect') || status.startsWith('http_')) return 'bs-status-warn'
  return 'bs-status-info'
}

export function BetriebsstatusKachel() {
  const [stand, setStand] = useState<Betriebsstatus | null>(null)
  const [fehler, setFehler] = useState<string | null>(null)

  useEffect(() => {
    const ctrl = new AbortController()
    const laden = () => {
      holeBetriebsstatus(ctrl.signal)
        .then(s => { setStand(s); setFehler(null) })
        .catch(e => { if (!istAbbruch(e)) setFehler(e.message ?? String(e)) })
    }
    laden()
    const stoppeIntervall = starteSichtbarkeitsIntervall(laden, 60_000)
    return () => { ctrl.abort(); stoppeIntervall() }
  }, [])

  if (fehler) {
    return (
      <div className="betriebsstatus-kachel betriebsstatus-fehler">
        Betriebsstatus konnte nicht geladen werden: {fehler}
      </div>
    )
  }
  if (!stand) {
    return <div className="betriebsstatus-kachel">Lade Betriebsstatus…</div>
  }

  return (
    <div className="betriebsstatus-kachel">
      <header className="bs-header">
        <h3>Betriebsstatus</h3>
        <span className="bs-meta">
          v{stand.system.version} · {stand.system.zonen_anzahl ?? '?'} Zonen
        </span>
      </header>

      <div className="bs-grid">
        {/* Endpoints (DHS + FYTA) */}
        <section className="bs-card">
          <h4>Endpoints</h4>
          {stand.endpoints.length === 0 && (
            <p className="bs-leer">Keine Health-Daten vorhanden.</p>
          )}
          <ul className="bs-liste">
            {stand.endpoints.map(e => (
              <li key={e.endpoint}>
                <span className={`bs-badge ${statusKlasse(e.status)}`}>
                  {e.status}
                </span>
                <strong>{e.endpoint}</strong>
                <span className="bs-zeit">{relativDauer(e.letzte_pruefung)}</span>
                {e.details && (
                  <small className="bs-details" title={e.details}>{e.details}</small>
                )}
              </li>
            ))}
          </ul>
        </section>

        {/* Husqvarna-Cadence */}
        <section className="bs-card">
          <h4>Husqvarna-Cadence</h4>
          <div className="bs-zeile">
            <span
              className={`bs-badge ${stand.husqvarna.stale ? 'bs-status-warn' : 'bs-status-ok'}`}
            >
              {stand.husqvarna.stale ? 'stale' : 'live'}
            </span>
            <span>letzter Beat {relativDauer(stand.husqvarna.letzter_beat)}</span>
          </div>
          <div className="bs-zeile">
            <small>{stand.husqvarna.beats_24h} Beats in 24h</small>
          </div>
        </section>

        {/* ML-Retrain */}
        <section className="bs-card">
          <h4>ML-Modell</h4>
          {stand.ml.ist_geladen ? (
            <>
              <div className="bs-zeile">
                <span className="bs-badge bs-status-ok">geladen</span>
                {stand.ml.cluster_count != null && (
                  <span>{stand.ml.cluster_count} Cluster-Modelle</span>
                )}
              </div>
              {/* T-0397 (F7): `trainiert_am` ist das ALTE Legacy-/Basismodell
                  (globaler Fallback, seit dem Umstieg auf Pro-Cluster nicht mehr
                  nachtrainiert). Die Cluster-Modelle -- die real vorhersagen --
                  retrainiert der 3-Tage-Job (Datum s. "ML-Auto-Retrain" im
                  Uebersicht-Tab). Ohne dieses Label liest sich "vor 75 d" wie
                  "das ML ist veraltet" und provoziert einen unnoetigen Retrain. */}
              <div className="bs-zeile">
                <small>Basismodell (Fallback) trainiert {relativDauer(stand.ml.trainiert_am)}</small>
              </div>
              <div className="bs-zeile">
                <small>Cluster-Modelle: 3-Tage-Auto-Retrain (Stand im Uebersicht-Tab)</small>
              </div>
            </>
          ) : (
            <div className="bs-zeile">
              <span className="bs-badge bs-status-warn">nicht geladen</span>
            </div>
          )}
        </section>

        {/* Backup */}
        <section className="bs-card">
          <h4>Backup</h4>
          <div className="bs-zeile">
            <span className="bs-badge bs-status-ok">{stand.backup.snapshot_count} Snapshots</span>
            {stand.backup.spiegel_aktiv && (
              <span className="bs-badge bs-status-info">iCloud-Spiegel</span>
            )}
          </div>
          <div className="bs-zeile">
            <small>letzter {relativDauer(stand.backup.letzter_snapshot_zeit)}</small>
            {stand.backup.letzter_snapshot && (
              <small className="bs-details">{stand.backup.letzter_snapshot}</small>
            )}
          </div>
        </section>

        {/* Watchdog / Push */}
        <section className="bs-card">
          <h4>Watchdog</h4>
          {stand.watchdog_letzter_push.zeit ? (
            <>
              <div className="bs-zeile">
                <span className="bs-badge bs-status-info">{stand.watchdog_letzter_push.trigger}</span>
                <span>{relativDauer(stand.watchdog_letzter_push.zeit)}</span>
              </div>
              {stand.watchdog_letzter_push.zone && (
                <div className="bs-zeile">
                  <small>Zone: {stand.watchdog_letzter_push.zone}</small>
                </div>
              )}
            </>
          ) : (
            <p className="bs-leer">Noch kein Push gesendet.</p>
          )}
        </section>

        {/* Sensor-Warnungen */}
        <section className="bs-card">
          <h4>Sensor-Warnungen</h4>
          <div className="bs-zeile">
            <span
              className={`bs-badge ${stand.offene_sensor_warnungen > 0 ? 'bs-status-warn' : 'bs-status-ok'}`}
            >
              {stand.offene_sensor_warnungen} offen
            </span>
          </div>
        </section>
      </div>
    </div>
  )
}
