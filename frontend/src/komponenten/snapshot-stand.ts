/* T-0603: reine Logik rund um den Dashboard-Snapshot (ohne JSX, testbar).
 *
 * Zwei Befunde aus dem Gesamtreview 25.09.:
 *
 * FE-5: Der 60-s-Schnellpfad holt nur '48h' ohne ML-Details und ersetzte
 * damit `snapshotProZone` komplett. Bei offenem Detail-Drawer verschwand so
 * nach spaetestens 60 s die nachgeladene 30-Tage-Reihe samt Top-Features
 * ("Kein Verlauf fuer 30d verfuegbar."), und der Drawer-Effekt laedt bei
 * unveraenderten Abhaengigkeiten nicht nach. `mergeSchnellpfad` ersetzt nur,
 * was der Schnellpfad wirklich liefert, und behaelt '7d'/'30d' und die
 * Detail-ML des vorherigen Stands.
 *
 * FE-6: Scheitert ein Refresh nach mindestens einem Erfolg, blieb der alte
 * Stand im V4-Zweig ohne Kennzeichnung stehen. `snapshotAktuell` ist die
 * Frische-Regel, die die neue Uebersicht schon hatte (Fehler oder zu alt =
 * nicht aktuell); V4 und Neu nutzen jetzt dieselbe.
 */

import type { DashboardSnapshotZone, Messwert, MLVorhersage } from '../typen'

type SnapshotProZone = Record<string, DashboardSnapshotZone>

/** Ab diesem Alter gilt der Snapshot-Stand als nicht aktuell.
 *
 *  Der `zeitstempel` der Antwort ist die RECHENZEIT im Backend, und das
 *  Backend cached den Snapshot bis zu 90 s (`_SNAPSHOT_CACHE_TTL_S`,
 *  api_server.py). Bei 60-s-Poll ist ein Stand im Normalbetrieb also bis
 *  ~150 s alt (plus Antwortzeit). Die fruehere Grenze der neuen Uebersicht
 *  (2 min) lag darunter und konnte ohne jeden Fehler kurz "nicht aktuell"
 *  melden. Grenze daher: Cache-TTL plus zwei Poll-Zyklen. */
export const SNAPSHOT_MAX_ALTER_MS = 90_000 + 2 * 60_000

/** T-0496: verlustfreier Ausschnitt eines groesseren Zeitfensters.
 *
 * Das Backend liefert pro angefordertem Fenster eine eigene, vollstaendige
 * Reihe -- 24 h steckt aber komplett in 48 h und 7 d komplett in 30 d.
 * Der Schnitt ist verlustfrei, nicht approximativ: `hole_messungen_bulk`
 * (speicher.py) macht ein reines `SELECT * ... WHERE zeitstempel >= von`
 * ohne LIMIT und ohne Downsampling. */
export function schneideFenster(
  reihe: Messwert[] | undefined, stunden: number, jetzt: number = Date.now(),
): Messwert[] {
  if (!reihe || reihe.length === 0) return []
  const grenze = jetzt - stunden * 3600_000
  return reihe.filter(m => new Date(m.zeitstempel).getTime() >= grenze)
}

/** ML-Horizonte der neuen (schlanken) Antwort; Top-Features eines Horizonts
 *  werden aus dem vorherigen Stand uebernommen, solange die neue Antwort
 *  keine eigenen traegt. Horizonte, die die neue Antwort nicht mehr kennt,
 *  fallen weg -- eine verschwundene Prognose wird nicht konserviert. */
function mlMitDetails(
  neu: Record<string, MLVorhersage>, alt: Record<string, MLVorhersage> | undefined,
): Record<string, MLVorhersage> {
  if (!alt) return neu
  const r: Record<string, MLVorhersage> = {}
  for (const [h, v] of Object.entries(neu)) {
    const altTop = alt[h]?.top_features
    r[h] = v.top_features === undefined && altTop !== undefined
      ? { ...v, top_features: altTop }
      : v
  }
  return r
}

/** FE-5: Schnellpfad-Ergebnis in den vorherigen Stand einarbeiten.
 *
 *  Ersetzt je Zone `zone`, `empfehlung`, '48h' (plus daraus geschnittenes
 *  '24h') und die schlanke ML; behaelt vorhandene '7d'/'30d' und die
 *  Top-Features. Zonen, die der neue Snapshot nicht mehr enthaelt, fallen
 *  weg (wie vorher beim Ersetzen). */
export function mergeSchnellpfad(
  prev: SnapshotProZone, zonen: DashboardSnapshotZone[], jetzt: number = Date.now(),
): SnapshotProZone {
  const next: SnapshotProZone = {}
  for (const z of zonen) {
    const alt = prev[z.zone.zone_id]
    const messwerte: DashboardSnapshotZone['messwerte'] = {}
    for (const lang of ['7d', '30d'] as const) {
      const reihe = alt?.messwerte[lang]
      if (reihe) messwerte[lang] = reihe
    }
    Object.assign(messwerte, z.messwerte)
    messwerte['24h'] = schneideFenster(z.messwerte['48h'], 24, jetzt)
    next[z.zone.zone_id] = {
      ...z,
      messwerte,
      ml_vorhersage: mlMitDetails(z.ml_vorhersage, alt?.ml_vorhersage),
    }
  }
  return next
}

/** Mittelstufe und Drawer-Nachladen: '30d' (plus geschnittenes '7d') und
 *  die Detail-ML in den vorhandenen Stand mergen, 24h/48h bleiben. */
export function mergeLanghistorie(
  prev: SnapshotProZone, zonen: DashboardSnapshotZone[], jetzt: number = Date.now(),
): SnapshotProZone {
  const next: SnapshotProZone = { ...prev }
  for (const z of zonen) {
    const vorhanden = next[z.zone.zone_id]
    const lang = {
      ...z.messwerte,
      '7d': schneideFenster(z.messwerte['30d'], 24 * 7, jetzt),
    }
    next[z.zone.zone_id] = vorhanden
      ? {
          ...vorhanden,
          messwerte: { ...vorhanden.messwerte, ...lang },
          ml_vorhersage: z.ml_vorhersage,
          ml_vorhersage_fehler: z.ml_vorhersage_fehler,
        }
      : { ...z, messwerte: lang }
  }
  return next
}

/** FE-6: Ist der Snapshot-Stand aktuell genug, um Empfehlung und ML als
 *  "jetzt" zu zeigen? Nein bei Fehler, unbekanntem oder zu altem Stand. */
export function snapshotAktuell(
  zeit: string | null | undefined, fehler: string | null | undefined, jetzt: number,
): boolean {
  if (fehler || !zeit) return false
  const ms = Date.parse(zeit)
  if (!Number.isFinite(ms)) return false
  return jetzt - ms <= SNAPSHOT_MAX_ALTER_MS
}

/** FE-6: Hinweiszeile fuer V4. `null`, solange der Stand aktuell ist oder
 *  noch nie ein Snapshot ankam (dann laden die Karten selbst nach). */
export function snapshotHinweisV4(
  zeit: string | null | undefined, fehler: string | null | undefined, jetzt: number,
): string | null {
  if (!zeit || snapshotAktuell(zeit, fehler, jetzt)) return null
  const ms = Date.parse(zeit)
  const uhrzeit = Number.isFinite(ms)
    ? new Date(ms).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
    : 'unbekannt'
  const grund = fehler
    ? `Aktualisierung fehlgeschlagen (${fehler})`
    : 'Aktualisierung ausstehend'
  return `Bewertung vom Stand ${uhrzeit}. ${grund}. Empfehlungen und ML-Werte auf den Karten sind nicht aktuell; Kritisch-Band und Heute-Plan setzen sie bis zum nächsten erfolgreichen Abruf aus.`
}
