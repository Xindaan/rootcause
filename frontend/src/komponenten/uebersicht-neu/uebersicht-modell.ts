import type { DashboardSnapshotZone, GiessEmpfehlung, MLVorhersage, Zone } from '../../typen'
import { getActionStateV4, datenUnsicher } from '../zonen-karte-v4/aktion-state-v4'
import { leadAusfallDetail, sensorNameInZone } from '../sensor-defekt'

export interface ZonenAnzeige {
  zone: Zone
  snapshot?: DashboardSnapshotZone
  titel: string
  detail: string
  ton: 'ruhig' | 'bedarf' | 'kritisch' | 'info' | 'unbekannt' | 'aktiv'
  prioritaet: number
  aufmerksamkeit: boolean
  empfehlung: GiessEmpfehlung | null
  datenBelastbar: boolean
  prognose?: MLVorhersage
}

export function alterStunden(zeit: string | null | undefined, jetzt: number): number | null {
  if (!zeit) return null
  const ms = Date.parse(zeit)
  return Number.isFinite(ms) ? Math.max(0, (jetzt - ms) / 3_600_000) : null
}

export function zeitAlter(zeit: string | null | undefined, jetzt: number): string {
  const h = alterStunden(zeit, jetzt)
  if (h == null) return 'Zeitpunkt unbekannt'
  if (h < 1 / 60) return 'gerade eben'
  if (h < 1.5) return `vor ${Math.round(h * 60)} min`
  if (h < 48) return `vor ${h.toLocaleString('de-DE', { maximumFractionDigits: 1 })} h`
  return `vor ${Math.round(h / 24)} Tagen`
}

export function betriebsModus(zone: Zone): string {
  if (zone.modus === 'automatik') {
    if (zone.autonom_scharf === true) return 'Automatik'
    if (zone.autonom_scharf === false) return 'Shadow · keine Automatik'
    return 'Automatikstatus unbekannt'
  }
  return zone.modus === 'monitoring' ? 'Beobachtung' : zone.modus
}

export function messQuelle(zone: Zone): string {
  const id = zone.feuchte_geraet_id ?? zone.letzter_bekannter_geraet_id
  if (id?.startsWith('aggregat:')) return `Sensor-Median (${id.slice('aggregat:'.length)})`
  if (id) {
    const s = zone.sensoren?.find(s => s.geraet_id === id)
    const q = s?.quelle ?? zone.quelle
    const name = sensorNameInZone(zone, id)
    return `${q === 'gardena' ? 'Gardena' : q === 'fyta' ? 'FYTA' : 'Sensor'} · ${name}`
  }
  return zone.quelle === 'gardena' ? 'Gardena' : zone.quelle === 'fyta' ? 'FYTA' : 'Quelle unbekannt'
}

/** Liste, Hinweiszaehler und Detailkopf nutzen dieselbe Einstufung.
 * Messung und Empfehlung stammen zusammen aus einem Snapshot. Bei einem
 * fehlgeschlagenen Refresh bleibt der letzte Stand sichtbar, verliert aber
 * seine aktuelle Empfehlung. Das aendert keine Backend-Entscheidung.
 */
export function baueZonenAnzeige(
  basis: Zone, snapshot: DashboardSnapshotZone | undefined,
  aktiv: boolean, snapshotAktuell: boolean, jetzt: number,
): ZonenAnzeige {
  const zone = snapshot?.zone ?? basis
  const alter = alterStunden(zone.letztes_update, jetzt)
  const kalibrierung = zone.ausschluss_fenster?.find(f =>
    (f.zweck ?? 'sensor_kalibrierung') === 'sensor_kalibrierung'
    && Date.parse(f.von) <= jetzt && Date.parse(f.bis) >= jetzt,
  )
  // 4 h entsprechen dem Abruf-Fenster des Backends. Nach einem Poll darf
  // dessen letzte Antwort beim Ueberschreiten nicht unbegrenzt gueltig bleiben.
  const datenBelastbar = !datenUnsicher(zone) && !kalibrierung && alter != null && alter <= 4
  const empfehlung = snapshotAktuell && datenBelastbar ? snapshot?.empfehlung ?? null : null
  const roh = snapshot?.ml_vorhersage?.['24h']
  const prognose = snapshotAktuell && datenBelastbar && roh?.gueltig !== false ? roh : undefined
  const anzeige: ZonenAnzeige = {
    zone, snapshot, empfehlung, datenBelastbar, prognose,
    titel: 'Empfehlung wird geladen', detail: 'Die Bewertung ist noch nicht verfügbar.',
    ton: 'unbekannt', prioritaet: 6, aufmerksamkeit: false,
  }
  if (!datenBelastbar) {
    anzeige.titel = kalibrierung ? 'Kalibrierung läuft'
      : zone.lead_ausgefallen ? 'Maßgeblicher Sensor fehlt'
      : zone.aktuelle_feuchte == null ? 'Keine aktuelle Messung'
      : alter == null || alter > 4 ? 'Messung veraltet' : 'Sensordaten prüfen'
    anzeige.detail = kalibrierung
      ? `Bis ${new Date(kalibrierung.bis).toLocaleDateString('de-DE')}: ${kalibrierung.grund || 'Messwerte werden kalibriert.'}`
      : zone.lead_ausgefallen ? leadAusfallDetail(zone)
      : 'Die Daten tragen derzeit keine neue Gießempfehlung.'
    anzeige.aufmerksamkeit = true
    anzeige.prioritaet = 2
  } else if (!snapshotAktuell && snapshot) {
    anzeige.titel = 'Bewertung nicht aktuell'
    anzeige.detail = 'Der letzte Stand bleibt sichtbar. Eine aktuelle Empfehlung fehlt.'
  } else if (empfehlung) {
    const zustand = getActionStateV4(zone, empfehlung)
    const texte = {
      kritisch: ['Kritisch trocken', 'kritisch', 1],
      giessen_akut: ['Gießbedarf laut Prognose', 'bedarf', 3],
      giessen: ['Bewässerung vorgeschlagen', 'bedarf', 3],
      beobachten: ['Bedarf beobachten', 'bedarf', 3],
      blocker: ['Bewässerung zurückgestellt', 'info', 4],
      nass: ['Über dem Zielbereich', 'info', 4],
      ok: ['Kein aktueller Gießbedarf', 'ruhig', 9],
      sensor: ['Sensordaten prüfen', 'unbekannt', 2],
    } as const
    const [titel, ton, prioritaet] = texte[zustand]
    anzeige.titel = titel
    anzeige.ton = ton
    anzeige.prioritaet = prioritaet
    anzeige.aufmerksamkeit = zustand !== 'ok'
    const blockerTexte: Record<string, string> = {
      REGEN_ERWARTET: 'Erwarteter Regen stellt die Bewässerung zurück.',
      ZEITFENSTER: 'Die Bewässerung wartet auf das erlaubte Zeitfenster.',
      BUDGET_ERSCHOEPFT: 'Das verfügbare Bewässerungsbudget ist ausgeschöpft.',
      PAUSE_AKTIV: 'Die Bewässerungspause ist noch aktiv.',
      KEINE_MESSUNG: 'Für die Entscheidung fehlt eine verwendbare Messung.',
    }
    anzeige.detail = zustand === 'kritisch'
      ? `Der Messwert liegt unter der kritischen Grenze von ${zone.feuchte_kritisch} %.`
      : zustand === 'giessen_akut' ? 'Die Prognose erwartet einen kritischen Feuchteabfall.'
      : zustand === 'giessen' ? 'Die Bewässerungsstrategie schlägt eine weitere Wassergabe vor.'
      : zustand === 'beobachten' ? 'Ein Bedarf wurde erkannt, derzeit wird keine automatische Bewässerung ausgelöst.'
      : zustand === 'blocker' ? blockerTexte[empfehlung.blocker_typ ?? ''] ?? 'Die automatische Bewässerung ist vorerst zurückgestellt.'
      : zustand === 'nass' ? `Der Messwert liegt über der eingestellten Obergrenze von ${zone.feuchte_schwelle_max} %.`
      : zustand === 'ok' ? 'Die letzte Bewertung sieht keinen aktuellen Gießbedarf.'
      : titel
  }
  if (aktiv) {
    anzeige.titel = 'Bewässerung läuft'
    anzeige.ton = 'aktiv'
    anzeige.prioritaet = 0
    anzeige.aufmerksamkeit = true
    anzeige.detail = 'Lauf und Stopp sind in der Bewässerungssteuerung sichtbar.'
  }
  return anzeige
}
