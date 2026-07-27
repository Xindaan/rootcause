/* Formatierungs-Hilfsfunktionen. */

import type { GiessEmpfehlung, Messwert, PlantOptimumAchse } from './typen'

/**
 * T-0279: Liefert die Haupt-Dauer in Sekunden, die in der GiessEmpfehlung
 * angezeigt wird (gleiche Hierarchie wie in `GiessEmpfehlungPanel`-Header).
 *
 *   mlPrimaer ? dauer_s_ml : dauer_s_empfehlung ?? dauer_s_heuristik
 *
 * `mlPrimaer` ist genau dann True, wenn der Auto-Loop giessen wuerde
 * UND ein gueltiges ML-Wirksam-Modell laeuft. Sonst kausale Empfehlung,
 * Heuristik als Fallback.
 *
 * Return:
 *   - positive Zahl: brauchbare Dauer
 *   - null: keine Empfehlung verfuegbar (Zone hat heute keine Dauer,
 *           z.B. waldblumen kein_bedarf oder Monitoring-Stub)
 */
export function dauerHauptSekunden(
  empf: GiessEmpfehlung | null | undefined,
): number | null {
  if (!empf) return null
  const mlPrimaer =
    empf.soll_bewaessern && empf.ml_wirksam && empf.dauer_s_ml !== null
  const wert = mlPrimaer
    ? empf.dauer_s_ml
    : empf.dauer_s_empfehlung !== null
      ? empf.dauer_s_empfehlung
      : empf.dauer_s_heuristik
  if (typeof wert !== 'number' || wert <= 0) return null
  return wert
}

export function zeitFormat(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
}

export function datumZeitFormat(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleString('de-DE', {
    day: '2-digit', month: '2-digit',
    hour: '2-digit', minute: '2-digit',
  })
}

export function windRichtungText(grad: number): string {
  const richtungen = ['N', 'NO', 'O', 'SO', 'S', 'SW', 'W', 'NW']
  const index = Math.round(grad / 45) % 8
  return richtungen[index]
}

/**
 * True wenn der Fehler ein Abort (via AbortController) ist.
 * Nach abort().catch sollen Aufrufer KEIN Fallback-setState ausloesen.
 */
export function istAbbruch(e: unknown): boolean {
  return e instanceof DOMException && e.name === 'AbortError'
}

/**
 * Bewertet einen Sensor-Wert gegen das FYTA-Optimum.
 * Logik isomorph zu `FytaKpiBlock.bewerte()` — fuer Zonen-Karte v2
 * als geteilter Helper. Rueckgabe nutzt Klassen-Strings (statt
 * CSS-Variablen-Strings), weil Konsumenten Klassen-Suffixe brauchen.
 */
export function bewerteOptimum(
  wert: number | null | undefined,
  opt: PlantOptimumAchse,
): { text: string; klasse: 'ok' | 'warn' | 'gefahr' } {
  if (wert == null) return { text: 'unbekannt', klasse: 'warn' }
  const { min_good, max_good, min_akzeptabel, max_akzeptabel } = opt
  if (max_akzeptabel != null && wert > max_akzeptabel) {
    return { text: 'zu hoch', klasse: 'gefahr' }
  }
  if (min_akzeptabel != null && wert < min_akzeptabel) {
    return { text: 'zu niedrig', klasse: 'gefahr' }
  }
  if (max_good != null && wert > max_good) {
    return { text: 'ueber Optimum', klasse: 'warn' }
  }
  if (min_good != null && wert < min_good) {
    return { text: 'unter Optimum', klasse: 'warn' }
  }
  return { text: 'im Optimum', klasse: 'ok' }
}

/**
 * Trend in pp/h (Prozentpunkte pro Stunde) aus den letzten 6h Messwerten.
 * Rueckgabe null wenn zu wenig Daten oder Zeitfenster < 30 min.
 */
export function berechneTrendPpProH(messwerte: Messwert[]): number | null {
  if (messwerte.length < 2) return null
  const jetzt = Date.now()
  const fenster = messwerte.filter(m => {
    const t = new Date(m.zeitstempel).getTime()
    return jetzt - t < 6 * 3600_000
  })
  if (fenster.length < 2) return null
  const ersterWert = fenster[0].boden_feuchte
  const letzterWert = fenster[fenster.length - 1].boden_feuchte
  if (ersterWert == null || letzterWert == null) return null
  const dh = (new Date(fenster[fenster.length - 1].zeitstempel).getTime()
           - new Date(fenster[0].zeitstempel).getTime()) / 3600_000
  if (dh < 0.5) return null
  return (letzterWert - ersterWert) / dh
}

/**
 * Formatiert einen pp/h-Trend zu Anzeige-String + Klassen-Suffix.
 * Schwellen: |pp| < 0.3 -> flat, sonst Richtung. Zonen-Karte v2.
 */
export function formatTrend(
  pp: number | null,
): { text: string; klasse: 'down' | 'up' | 'flat' } {
  if (pp == null) return { text: '–', klasse: 'flat' }
  if (Math.abs(pp) < 0.3) return { text: `→ ${pp.toFixed(1)} pp/h`, klasse: 'flat' }
  if (pp < 0) return { text: `▼ ${pp.toFixed(1)} pp/h`, klasse: 'down' }
  return { text: `▲ +${pp.toFixed(1)} pp/h`, klasse: 'up' }
}

/**
 * T-0370: Klartext-Label fuer Bewaesserungs-Strategie-Enums. Single
 * Source fuer ZonenKarteV4Glance (Sub-Titel-Tooltip) und TagesplanBlock
 * (tp-strat) -- vorher stand im Tagesplan das rohe Enum ("haeufig_klein").
 * Unbekannte Werte fallen lesbar zurueck (Unterstriche -> Leerzeichen).
 */
export function strategieKlartext(s: string | null | undefined): string | null {
  if (!s) return null
  switch (s) {
    case 'korridor':         return 'Korridor'
    case 'haeufig_klein':    return 'häufig + klein'
    case 'selten_gross':     return 'selten + groß'
    case 'konstant_niedrig': return 'konstant niedrig'
    default:                 return s.replace(/_/g, ' ')
  }
}
