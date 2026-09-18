/* T-0601: reine Logik hinter Triage-Strip und Karten-Sortierung der V4-
 * Uebersicht (ohne JSX, damit die Node-Tests sie direkt ausfuehren koennen;
 * Komponenten-Dateien duerfen fuer Fast Refresh nur Komponenten exportieren).
 *
 * Beide fragen "ist der Ist-Wert kritisch?" ueber `messwertBelastbar`
 * (aktion-state-v4.ts) -- dieselbe Regel wie Kritisch-Band und neue
 * Uebersicht. Vorher prueften Strip und Sortierung weder offene
 * Ausfall-Warnungen noch das Wertalter (Gesamtreview 25.09., FE-2).
 */

import type { Zone } from '../../typen'
import { messwertBelastbar } from './aktion-state-v4'

export type TriageBucket = 'kritisch' | 'giesst' | 'empfehlung' | 'nass' | 'korridor' | 'unbekannt'

/** Primaerer Triage-Zustand einer Zone (mutually exclusive). Kalibrierung ist
 *  orthogonal und wird im Strip separat gezaehlt.
 *
 *  T-0610 Q6 (Entscheid Andre 13.10.): "kritisch" geht VOR "giesst". Vorher
 *  zaehlte der Strip eine kritische Zone mit offenem Ventil als "gießt
 *  gerade", das Kritisch-Band dieselbe Zone als kritisch -- die Zahlen wichen
 *  um eins ab. Jetzt zaehlen beide gleich; dass die kritische Zone gerade
 *  bewaessert wird, traegt der Strip als Zusatzmarke (`triageGruppen`,
 *  `kritischGiesst`). Fuer nicht kritische Zonen bleibt "giesst" vorn, auch
 *  vor "keine Daten" (wie bisher). */
export function triageBucket(
  zone: Zone, giesst: boolean, hatEmpfehlung: boolean, jetzt: number,
): TriageBucket {
  // T-0390/T-0532/T-0601: kein Wert, 0.0-Defekt, Lead-Ausfall, offene
  // Ausfall-Warnung des Quell-Sensors oder ein zu alter Wert = keine
  // belastbaren Daten -> nicht nach dem Wert buckten.
  const belastbar = messwertBelastbar(zone, jetzt)
  // T-0381: kein Fallback auf schwelle_min -- das buckete eine Zone knapp unter
  // min faelschlich als "kritisch". Fehlt feuchte_kritisch, gibt es keinen
  // kritisch-Bucket (Backend setzt es real fuer alle Zonen; Guard defensiv).
  const kritisch = zone.feuchte_kritisch
  if (belastbar && kritisch != null && (zone.aktuelle_feuchte as number) < kritisch) {
    return 'kritisch'
  }
  if (giesst) return 'giesst'
  if (!belastbar) return 'unbekannt'
  const ist = zone.aktuelle_feuchte as number
  // T-0491: null = keine Obergrenze, also kein nass-Bucket.
  const maxSchwelle = zone.feuchte_schwelle_max
  if (maxSchwelle != null && ist > maxSchwelle) return 'nass'
  if (ist < zone.feuchte_schwelle_min || hatEmpfehlung) return 'empfehlung'
  return 'korridor'
}

/** T-0610 Q6: Strip-Gruppen plus Zusatzmarke "kritisch und giesst".
 *  Reine Funktion, damit Zaehlung und Marke ohne Browser pruefbar sind. */
export function triageGruppen(
  zonen: Zone[], bewaesserungAktiv: Set<string>, empfZonen: Set<string>, jetzt: number,
): { gruppen: Record<TriageBucket, Zone[]>; kritischGiesst: Zone[] } {
  const gruppen: Record<TriageBucket, Zone[]> = {
    kritisch: [], giesst: [], empfehlung: [], nass: [], korridor: [], unbekannt: [],
  }
  const kritischGiesst: Zone[] = []
  for (const z of zonen) {
    const giesst = bewaesserungAktiv.has(z.zone_id)
    const bucket = triageBucket(z, giesst, empfZonen.has(z.zone_id), jetzt)
    gruppen[bucket].push(z)
    if (bucket === 'kritisch' && giesst) kritischGiesst.push(z)
  }
  return { gruppen, kritischGiesst }
}

/** T-0207 (17.05.): Feste Outdoor-Reihenfolge fuer V3.
 *  Bambus-Geschwisterzonen direkt hintereinander, danach Sprinkler-
 *  Beete in Pflanz-Reihenfolge (Bestand -> Etablierung -> Anwachs).
 *  Zonen, die nicht in der Liste sind, fallen auf die Akut-Sortierung
 *  zurueck — Indoor/Balkon bleibt also unveraendert.
 *  (T-0248: vorher V2_FESTE_REIHENFOLGE, V2 ist mit V1 weg.) */
const V3_FESTE_REIHENFOLGE: Record<string, number> = {
  bambuswald: 0,
  bambuswald_yogaraum: 1,
  waldblumenhain: 2,
  magerwiese: 3,
  hecke: 4,
}

/** T-0601: Sortier-Rang einer Zone (0 = kritisch ... 3 = ruhig/unbekannt).
 *  Nicht belastbare Werte ranken wie "kein Wert", nicht nach der Zahl. */
export function sortierRang(zone: Zone, jetzt: number): number {
  const warnungKrit = (zone.offene_warnungen ?? []).some(w => /kritisch/i.test(w.typ))
  if (warnungKrit) return 0
  if (!messwertBelastbar(zone, jetzt)) return 3
  const ist = zone.aktuelle_feuchte as number
  const kritisch = zone.feuchte_kritisch ?? null
  const min = zone.feuchte_schwelle_min
  if (kritisch != null && ist < kritisch) return 0
  if (ist < min) return 1
  if (ist < min + 5) return 2
  return 3
}

/* T-0221: Sortierung fuer den V3-Reiter -- Hybrid + Bewaesserungs-
   Kohaesion (User-Entscheidung 20.05.):
   - Zonen am selben Ventil (gleiche `(ventil_geraet_id, ventil_kanal)`)
     bilden eine Gruppe, die NIE auseinandergerissen wird.
   - Kritische Gruppen schwimmen nach oben (dringendste zuerst), eine
     kritische Zone zieht ihre Kanal-Geschwister mit.
   - Nicht-kritische Gruppen bleiben in der festen T-0207-Reihenfolge.
   T-0601: aus App.tsx hierher verschoben, Rang ueber `sortierRang`. */
export function sortiereZonenV3(zonen: Zone[], jetzt: number): Zone[] {
  const rang = (z: Zone): number => sortierRang(z, jetzt)
  const festIndex = (z: Zone): number =>
    V3_FESTE_REIHENFOLGE[z.zone_id] ?? Number.POSITIVE_INFINITY
  // Bewaesserungs-Gruppe: gleiches Ventil = gleiche (geraet_id, kanal).
  // Zonen ohne Kanal stehen fuer sich (eigener Key ueber zone_id).
  const gruppenKey = (z: Zone): string =>
    z.ventil_kanal != null
      ? `v:${z.ventil_geraet_id ?? 'primaer'}#${z.ventil_kanal}`
      : `z:${z.zone_id}`
  const gruppen = new Map<string, Zone[]>()
  for (const z of zonen) {
    const k = gruppenKey(z)
    const g = gruppen.get(k)
    if (g) g.push(z)
    else gruppen.set(k, [z])
  }
  const gruppenListe = [...gruppen.values()].map(mitglieder => {
    const sortiert = [...mitglieder].sort((a, b) => {
      const fd = festIndex(a) - festIndex(b)
      if (fd !== 0) return fd
      const rd = rang(a) - rang(b)
      if (rd !== 0) return rd
      return (a.aktuelle_feuchte ?? Infinity) - (b.aktuelle_feuchte ?? Infinity)
    })
    return {
      mitglieder: sortiert,
      gruppenRang: Math.min(...mitglieder.map(rang)),
      minFest: Math.min(...mitglieder.map(festIndex)),
      minFeuchte: Math.min(
        ...mitglieder.map(z => z.aktuelle_feuchte ?? Infinity),
      ),
    }
  })
  gruppenListe.sort((a, b) => {
    const aKrit = a.gruppenRang === 0 ? 0 : 1
    const bKrit = b.gruppenRang === 0 ? 0 : 1
    if (aKrit !== bKrit) return aKrit - bKrit
    if (aKrit === 0) return a.minFeuchte - b.minFeuchte
    if (a.minFest !== b.minFest) return a.minFest - b.minFest
    if (a.gruppenRang !== b.gruppenRang) return a.gruppenRang - b.gruppenRang
    return a.minFeuchte - b.minFeuchte
  })
  return gruppenListe.flatMap(g => g.mitglieder)
}
