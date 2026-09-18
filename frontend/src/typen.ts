/* Gemeinsame TypeScript-Interfaces fuer API-Antworten. */

import type { GiessfensterInfo } from './komponenten/giessfenster_hinweis'

export interface Standort {
  standort_id: string
  name: string
  zonen: string[]
  wetter_standort: string
}

/** T-0179c: Einzelsensor-Messung fuer Multi-Sensor-Diagnose pro Zone.
 *  Backend liefert das in `Zone.sensoren[]` (api_server.py:606). */
export interface SensorEinzeln {
  geraet_id: string
  quelle: string | null
  /** T-0204: Klartextname aus `config.sensor_namen[]`. `null` wenn nicht
   *  gepflegt -- Frontend faellt dann auf gekuerzte ID zurueck. */
  name?: string | null
  boden_feuchte: number | null
  boden_temperatur: number | null
  zeitstempel: string
}

/** T-0125/T-0183: offene Sensor-Warnung pro Zone (api_server.py).
 *  Wird in `Zone.offene_warnungen[]` als Badge gerendert. */
export interface OffeneSensorWarnung {
  typ: string
  zeitstempel: string
  details: string | null
}

/** T-0136 (H-4): konfiguriertes Feuchte-Regime (Saison/Phase) einer Zone.
 *  Spiegel von `bewaesserung.modelle.FeuchteRegime`. */
export interface FeuchteRegime {
  name: string
  grund?: string | null
  von?: string | null
  bis?: string | null
  feuchte_schwelle_min?: number | null
  feuchte_schwelle_max?: number | null
  feuchte_kritisch?: number | null
  optimum_feuchte_min?: number | null
  optimum_feuchte_max?: number | null
}

export interface Zone {
  zone_id: string
  name: string
  modus: string
  /** T-0370: true = Auto-Loop giesst diese Zone autonom (3-stufig scharf:
   *  global ventilsteuerung_aktiv UND modus=automatik UND auto_loop_opt_in).
   *  false bei modus=automatik = Shadow (Entscheidungen nur protokolliert).
   *  Optional: aeltere Backends liefern das Feld nicht -> Badge faellt auf
   *  das alte AUTOMATIK-Verhalten zurueck. */
  autonom_scharf?: boolean
  feuchte_schwelle_min: number
  /** T-0491 (13.08.2026): darf null sein. Zonen, deren Nass-Warnung nichts
   *  mehr aussagte, haben sie abgegeben (zitrus und mandevilla_maxi standen
   *  zu 100 % darueber). null heisst "diese Zone hat keine Obergrenze" --
   *  NICHT 0. Ohne diesen Typ wertete JavaScript `wert > null` als
   *  `wert > 0` und haette genau die Zonen dauerhaft als nass markiert, die
   *  die Warnung gerade losgeworden sind. */
  feuchte_schwelle_max: number | null
  // Optional — alt-API liefert diese Felder noch nicht. Neue Komponenten
  // (SchwellenRange, EntscheidungsErklaerungNeu) pruefen auf null.
  feuchte_kritisch?: number | null
  ventil_kanal?: number | null
  // T-0221: DSWC-Geraet (Multi-DSWC, T-0203). null = primaere DSWC.
  // V3 leitet daraus "DSWC 1/2" + die Bewaesserungs-Gruppierung in der
  // Sortierung ab (Zonen mit gleichem (geraet_id, kanal) = ein Ventil).
  ventil_geraet_id?: string | null
  optimum_feuchte_min?: number | null
  optimum_feuchte_max?: number | null
  aktuelle_feuchte: number | null
  boden_temperatur: number | null
  batterie: number | null
  licht: number | null
  licht_intensitaet: number | null
  boden_fruchtbarkeit: number | null
  quelle: string | null
  /** T-0532: geraet_id, aus der `aktuelle_feuchte` stammt. "aggregat:<n>"
   *  bei Median ueber mehrere Sensoren. Ueber `sensor_namen` in Klartext
   *  aufloesbar. Optional -- aeltere Backends liefern das Feld nicht. */
  feuchte_geraet_id?: string | null
  /** T-0532: Zone hat einen Aggregat-Lead, `aktuelle_feuchte` stammt aber
   *  nicht von ihm (Lead ausserhalb des 90-min-Fensters, Backend faellt auf
   *  den Verarbeiter-Cache zurueck). Dann KEINE Schwellen-Aussage ableiten,
   *  siehe `leadAusgefallen` in `komponenten/sensor-defekt.ts`. */
  lead_ausgefallen?: boolean
  /** T-0532: konfigurierter Aggregat-Lead der Zone (null = keiner). */
  aggregat_lead_geraet?: string | null
  // T-0573 AK6: letzter bekannter Messwert, wenn `aktuelle_feuchte` aus
  // dem Abruf-Fenster gefallen ist (240 min). NUR fuer die Anzeige -- die
  // Zone bekommt weiterhin keine Empfehlung, sie zeigt nur nicht mehr "-",
  // wo sie "10 % · vor 10 h" sagen kann. Null, solange ein frischer Wert
  // existiert.
  letzter_bekannter_wert?: number | null
  letzter_bekannter_zeit?: string | null
  letzter_bekannter_alter_h?: number | null
  letzter_bekannter_geraet_id?: string | null
  /** T-0502: Urteil des 0.0-Guards, vom BACKEND berechnet. `null`/fehlend =
   *  der Wert ist nicht 0.0, die Frage stellt sich nicht. Nicht selbst
   *  nachbauen -- das Urteil haengt am 24-h-Fenster in der DB. */
  null_ist_defekt?: boolean | null
  letztes_update: string | null
  // T-0169: Manuell-Logging-Einheit (Frontend-Toggle Sekunden vs. ml).
  // Backend-persistierter Default pro Zone.
  logging_einheit?: 'sekunden' | 'ml'
  logging_optionen_ml?: number[]
  // T-0170: Pre-Soak-Defaults aus YAML, jetzt korrekt durchgereicht
  // (waren zuvor als Props in ManuellesGiessen.tsx deklariert, aber
  // ZonenKarte[Neu].tsx hat sie nicht weitergegeben).
  pre_soak_min?: number | null
  pre_soak_pause_min?: number | null
  // T-0136 (H-4 Stufe 1b): konfigurierte Regimes + Name des aktuell
  // gueltigen. Frontend rendert via FeuchteRegimeBadge (T-0137).
  feuchte_regime?: FeuchteRegime[]
  feuchte_regime_aktiv?: string | null
  // T-0179c: Liste der Einzelsensoren der Zone (Multi-Sensor-Diagnose).
  // Bei Single-Sensor-Zonen hat das Array 1 Eintrag. Enthaelt nur die
  // in den letzten 6h aktiven Sensoren.
  sensoren?: SensorEinzeln[]
  // T-0221-Folge: geraet_id -> Klartextname fuer ALLE konfigurierten
  // Sensoren. `sensoren` oben hat nur die 6h-aktiven -- diese Map loest
  // auch im 48h-Chart sichtbare, aber stille Sensoren auf einen Namen
  // statt der rohen geraet_id auf.
  sensor_namen?: Record<string, string>
  // T-0125/T-0183: offene Sensor-Warnungen fuer Badge-Render.
  offene_warnungen?: OffeneSensorWarnung[]
  // T-0196: FYTA-Optimum-Schwellen pro Achse (Multi-Achse EAV).
  // Schluessel: 'feuchte', 'licht_ppfd', 'licht_dli', 'temperatur',
  // 'salinitaet'. Leeres Dict wenn keine FYTA-Zone oder Job noch nicht
  // gelaufen. Frontend rendert Optimum-Baender nur fuer vorhandene
  // Achsen + nur wenn min_good UND max_good != null.
  optima?: Record<string, PlantOptimumAchse>
  // T-0211b: Ausschluss-Fenster (z.B. Bodenart-Reset-Phasen). Chart
  // rendert eine Schraffur ueber diese Zeitraeume + Tooltip-Hint, damit
  // User die zickigen Sensor-Werte als "Kalibrierung laeuft" erkennt.
  ausschluss_fenster?: AusschlussFenster[]
  // T-0221: Flaeche + Topf/Beet fuer die V3-Karten-Sub-Zeile
  // ("BEET · 40 m²" bzw. "TOPF").
  flaeche_m2?: number | null
  ist_topf?: boolean
  // T-0221: AquaBloom-Pumpen-Konfig (T-0168). null = keine AquaBloom-
  // Pumpe. `aktiv` = konfiguriert UND aktuell in Saison.
  aquabloom_konfig?: AquabloomKonfig | null
}

/** T-0221: AquaBloom-Pumpen-Konfig einer Zone (Spiegel des
 *  `aquabloom_konfig`-Sub-Dicts aus `_baue_zone_dict`). */
export interface AquabloomKonfig {
  intervall_stunden: number | null
  dauer_sekunden: number | null
  anker_zeitstempel: string | null
  aktiv: boolean
}

/** T-0211b: Ein Ausschluss-Fenster aus `ml_ausschluss_fenster` (Backend-
 *  Konfig). Wird vom Frontend im Karten-Chart als Schraffur dargestellt.
 */
export interface AusschlussFenster {
  von: string    // ISO 8601
  bis: string    // ISO 8601
  grund: string  // User-freundlicher Erklaerungs-Text
  // T-0304: Zweck -- 'sensor_kalibrierung' (Sensor-Werte unzuverlaessig,
  // Karte zeigt "Kalibrierung", Chart dimmt) vs 'event_ignore' (Sensor ok,
  // nur Kanal-Events ignoriert, z.B. Magerwiese-Gras T-0300 -> Empfehlung
  // gilt). Optional fuer Backward-Compat; undefined wird wie
  // 'sensor_kalibrierung' behandelt (sicherer Default = flaggen).
  zweck?: 'sensor_kalibrierung' | 'event_ignore'
  // T-0386: gesetzt -> das Fenster gilt nur fuer genau diesen Sensor,
  // nicht fuer die ganze Zone. T-0573 braucht das, um beim Zerschneiden
  // der Feuchte-Linie die gesunden Nachbarsensoren in Ruhe zu lassen.
  geraet_id?: string | null
}

/** T-0196: Optimum-Schwellen einer Achse (Backend: plant_optimum_achse).
 *  FYTA Plant-Detail-API liefert min_good, max_good, min_akzeptabel,
 *  max_akzeptabel + Einheit + aktualisiert-Zeitstempel. Einheit ist
 *  ein Anzeige-String (z.B. 'μmol/h', 'mol/day', '°C/h').
 *  T-0196d/e: `current` ist der zuletzt beobachtete Wert. Quelle:
 *  FYTA `values.current` fuer moisture/light_ppfd/temperatur/
 *  salinitaet, Backend-Aggregat fuer light_dli (Tagessumme PPFD →
 *  mol/day). null wenn noch kein Live-Wert verfuegbar. */
export interface PlantOptimumAchse {
  min_good: number | null
  max_good: number | null
  min_akzeptabel: number | null
  max_akzeptabel: number | null
  current: number | null
  einheit: string
  aktualisiert: string
  quelle: string
}

export interface Messwert {
  zeitstempel: string
  boden_feuchte: number | null
  boden_temperatur: number | null
  // T-0190: FYTA-spezifische Felder im Verlaufs-Endpoint. Bei
  // Gardena-Zonen `null`. Visualisierung im Drawer/Karten-Chart kommt
  // mit T-0194 (Einheiten-Trennung Lux vs. FYTA-Index).
  licht?: number | null
  licht_intensitaet?: number | null
  boden_fruchtbarkeit?: number | null
  umgebungs_temperatur?: number | null
  batterie_prozent?: number | null
  // T-0211c: Pro-Sensor-Zuordnung fuer Multi-Linien-Chart. Bei
  // Multi-Sensor-Zonen (z.B. waldblumenhain mit 1 Gardena + 2 FYTA)
  // damit nicht alle Werte in eine Misch-Linie wandern, was wie
  // wilde Sprünge aussieht. Optional, weil aeltere Backend-Versionen
  // die Felder nicht liefern (Frontend faellt dann auf eine Linie
  // zurueck).
  geraet_id?: string | null
  quelle?: string | null
}

export interface Prognose {
  zone_id: string
  name: string
  bewaesserung_erwartet: string | null
  begruendung: string
}

export interface FeatureBeitrag {
  name: string
  wert: number | null
  beitrag: number
}

export interface MLVorhersage {
  feuchte_prognose: number
  feuchte_aktuell: number
  // T-0046: Quantile-Baender (optional, nur gesetzt wenn Service
  // Quantile-Modelle geladen hat)
  q10?: number
  q90?: number
  // T-0040: Top-5 Feature-Beitraege (nur gesetzt bei ?details=top_features)
  top_features?: FeatureBeitrag[]
  // T-0573: Herkunft + Guete der Prognose. `feuchte_prognose` allein sagt
  // nicht, ob die Zahl zum heutigen Sensor gehoert: `zeitstempel` ist die
  // RECHENZEIT (immer jetzt), `feature_zeitstempel` die Zeile, auf der
  // gerechnet wurde. Steht die still (Ausschlussfenster), rechnet die
  // Pipeline endlos denselben Wert -- Realfall Maxibaer 09.09.: Istwert
  // 30, daneben "24h: 66" aus einer 25 h alten Beam-Zeile.
  feature_zeitstempel?: string | null
  feature_alter_h?: number | null
  /** Rueckstand auf die juengste Messung der Zone -- die Groesse, an der
   *  das Urteil haengt. 0 heisst: so frisch, wie die Daten es zulassen. */
  feature_rueckstand_h?: number | null
  geraet_id?: string | null
  /** false -> die Zahl NICHT als Prognose anzeigen. */
  gueltig?: boolean
  /** 'veraltet' | 'geraetewechsel' | 'herkunft_unbekannt' */
  ungueltig_grund?: string | null
}

/** T-0108: Crash-Info eines ML-Jobs. `null` wenn der Job zuletzt
 *  fehlerfrei lief. Wird beim naechsten erfolgreichen Lauf wieder
 *  geleert, damit das Fehler-Banner automatisch verschwindet. */
export interface MLJobFehler {
  zeit: string
  typ: string
  nachricht: string
}

/** T-0048: Auto-Retrain-Block in `/api/ml/status`. */
export interface MLRetrainStatus {
  aktiv: boolean
  gate_faktor: number
  intervall_tage: number
  letzter_lauf: string | null
  letztes_ergebnis: Record<string, unknown> | null
  // T-0108: einheitliche Fehler-/Erfolg-Sichtbarkeit.
  letzter_erfolg?: string | null
  letzter_fehler?: MLJobFehler | null
}

/** T-0108: schlanker Job-Status-Block (Response-Retrain, Kalibrierung). */
export interface MLJobStatus {
  aktiv: boolean
  letzter_erfolg: string | null
  letzter_fehler: MLJobFehler | null
}

export interface MLStatus {
  ist_geladen: boolean
  hinweis?: string
  // T-0183 (Folge): Auto-Retrain-Statistik fuer Frontend-Tile. Backend
  // liefert das nur wenn der MlRetrainJob in main.py verdrahtet ist.
  retrain?: MLRetrainStatus
  // T-0108: weitere ML-Jobs, deren Crashes vorher nur im Log standen.
  response_retrain?: MLJobStatus
  kalibrierung?: MLJobStatus
}

export interface WetterStunde {
  zeitstempel: string
  temperatur: number
  niederschlag_mm: number
  wind_kmh: number
  wind_richtung_grad: number
  et0_mm: number
}

export type BlockerTyp =
  | 'FEUCHTE_OK'
  | 'KEINE_MESSUNG'
  | 'REGEN_ERWARTET'
  | 'ZEITFENSTER'
  | 'BUDGET_ERSCHOEPFT'
  | 'PAUSE_AKTIV'

export interface Entscheidung {
  zeitstempel: string
  zone_id: string
  soll_bewaessern: boolean
  dauer_sekunden: number
  begruendung: string
  blocker_typ: BlockerTyp | null
  scope: 'zone' | 'kanal'
  scope_ref: string
}

export interface VentilEreignis {
  zeitstempel: string
  aktion: string
  dauer_sekunden: number
  ausloser: string
}

/** Detailiertes Ventil-Ereignis aus GET /api/ventil-ereignisse (mit ID fuer PATCH). */
export interface VentilEreignisDetail {
  id: number
  zeitstempel: string
  zone_id: string
  ventil_id: string
  aktion: 'oeffnen' | 'schliessen' | 'pause' | 'fortsetzen'
  dauer_sekunden: number
  ausloser: string
  liter: number | null
  // T-0335: Pre-Soak-Lauf-Gruppierung. Events mit gleicher `lauf_gruppe`
  // gehoeren zu EINEM Pre-Soak-Lauf; `phase` = 'pre_soak' | 'haupt'. Null bei
  // normalen Einzellaeufen (Auto-Loop, manuell, watchdog).
  lauf_gruppe: string | null
  phase: string | null
}

// T-0335: Ein Giess-Lauf (gruppiert aus rohen Events vom Backend).
export interface GiessPhase {
  phase: string | null          // 'pre_soak' | 'haupt' | null (Einzel)
  start: string                 // ISO 8601
  dauer_s: number | null
}

export interface GiessLauf {
  start: string                 // ISO 8601 (erster OEFFNEN)
  ende: string | null           // ISO 8601 (letzter SCHLIESSEN) | null wenn laufend
  methode: 'einzel' | 'pre_soak'
  ausloser: string              // 'automatik' | 'manuell' | 'zeitplan' | 'fremdwasser' | 'ignoriert' | ...
  zaehlt: boolean               // false = ignoriert/fremdwasser/aquabloom
  laeuft_noch: boolean
  dauer_gesamt_s: number | null
  liter_gesamt: number | null
  // Zonen dieses physischen Laufs. Meist eine; bei seriellem Strang (ein Lauf
  // naesst mehrere Zonen am selben Ventil, z.B. Bambus) mehrere.
  zone_ids: string[]
  phasen: GiessPhase[]
}

// 'zeitplan' (T-0455) = Gardena-Cloud-Zeitplan: echtes Wasser, aber kein
// Engine-Lauf. 'fremdwasser' (T-0453) = echter Feuchte-Sprung aus dem Kanal
// einer Nachbar-Zone (Cross-Spray), zaehlt NICHT als Wasser dieses Kanals.
export type AusloeserPatch =
  | 'automatik' | 'manuell' | 'zeitplan' | 'fremdwasser'
  | 'unbekannt' | 'watchdog' | 'notfall_stopp'

export interface VentilEreignisPatch {
  ausloser?: AusloeserPatch
  zeitstempel?: string
  liter?: number
  dauer_sekunden?: number
  /** T-0453: nur mit `ausloser: 'fremdwasser'`. Ohne Angabe leitet das
   *  Backend die Cross-Spray-Quelle ab, sofern sie eindeutig ist. */
  quell_zone?: string
}

/** T-0241: aktives Wetter-Ereignis (Frost/Hitze/Starkregen) aus
 *  `wetter_ereignis`-Tabelle. Backend liefert die juengste pro
 *  (typ, standort_id), die noch nicht abgelaufen ist (ende >= jetzt). */
export interface WetterEreignisAktiv {
  typ: 'frost' | 'hitze' | 'starkregen'
  standort_id: string
  details: string
  beginn: string | null
  ende: string | null
  zeitstempel: string
}

export interface Wetter {
  niederschlag_6h_mm: number
  et0_6h_mm: number
  wind_6h_kmh: number
  /** T-0241: aktive Frost/Hitze/Starkregen-Warnungen. Optional fuer
   *  Backward-Compat mit aelteren Backends ohne den Feld-Lift. */
  aktive_ereignisse?: WetterEreignisAktiv[]
  stunden: WetterStunde[]
}

export interface SchwellenVorschlag {
  zone_id: string
  min_aktuell: number
  max_aktuell: number
  min_vorschlag: number | null
  max_vorschlag: number | null
  basis: 'berechnet' | 'zu_wenig_daten'
  n_messungen: number
  fenster_tage: number
  // T-0050a: Pflanzen-Optimum aus Zone-Config. null wenn nicht gesetzt.
  optimum_min: number | null
  optimum_max: number | null
  quelle: 'empirisch' | 'optimum_dominiert'
}

export type BilanzFenster = '24h' | '7d' | '30d'
export type BilanzQuelle = 'archiv' | 'forecast' | 'gemischt' | 'keine'

export interface WasserBilanz {
  zone_id: string
  fenster: BilanzFenster
  fenster_von: string
  fenster_bis: string
  flaeche_m2: number
  bewaesserung_liter: number
  regen_liter: number
  zugefuehrt_liter: number
  verdunstet_liter: number
  bilanz_liter: number
  quelle_niederschlag: BilanzQuelle
  indikativ: boolean
  // T-0575: welche SEITE unsicher ist. `indikativ` faltet zwei Ursachen in
  // ein Bool (Bewaesserung ohne Literwert vs. Wetter aus Vorhersage); die
  // Kachel nannte deshalb regelmaessig die falsche. Optional, damit ein
  // aelteres Backend weiter bedient wird.
  bewaesserung_indikativ?: boolean
}

export interface WasserBilanzFehler {
  fehler: string
  zone_id?: string
}

export type OpsSeverityFilter = 'kritisch' | 'aktion' | 'wetter' | 'routine'

export interface OpsSummary {
  zeitraum: {
    von: string
    bis: string
  }
  bewaesserungen_heute: number
  shadow_vorschlaege_heute: number
  blocker_verteilung: Record<string, number>
  wetter_warnungen: number
  sensor_warnungen: number
}

/** T-0236: Empfehlungs-Audit-Eintrag aus /api/empfehlungs-audit.
 *  Pro Empfehlung ein Snapshot, nach 6/24h evaluiert. */
export interface EmpfehlungsAuditEintrag {
  zeitstempel: string
  zone_id: string
  empfehlungs_typ: string
  soll_bewaessern: boolean
  blocker_typ?: string | null
  feuchte_aktuell: number | null
  welkepunkt_wert: number | null
  optimum_min: number | null
  optimum_max: number | null
  prognose_quelle: string
  prognose_6h: number | null
  prognose_12h?: number | null
  prognose_24h: number | null
  tage_bis_welkepunkt: number | null
  dauer_s_empfehlung: number | null
  aktive_strategie: string
  ist_6h?: number | null
  ist_24h?: number | null
  abweichung_6h?: number | null
  abweichung_24h?: number | null
  evaluiert_am?: string | null
}

/** T-0236: Aggregierte Statistik aus /api/empfehlungs-audit. */
export interface EmpfehlungsAuditStats {
  n: number
  n_evaluiert: number
  mae_6h_pp: number | null
  mae_24h_pp: number | null
  typ_verteilung: Record<string, number>
}

export interface EmpfehlungsAuditAntwort {
  zone_id: string | null
  fenster_tage: number
  stats: EmpfehlungsAuditStats
  /** T-0236-Bug-Fix (25.05.): pro-Zone-Aggregat per SQL ohne Eintrags-
   *  Limit. Vorher hat das Frontend MAE/n_evaluiert pro Zone aus
   *  paginierten Eintraegen selbst gerechnet -- bei 14 Zonen reichte
   *  das 500er-Limit nur fuer ~1.5 Tage. Reife-Pill war systematisch
   *  falsch. Jetzt: Backend liefert pro Zone die echten Stats ueber
   *  den gewuenschten Fenster-Zeitraum. */
  pro_zone_stats: Record<string, EmpfehlungsAuditStats>
  eintraege: EmpfehlungsAuditEintrag[]
}

/** T-0236: Dauer-Drift pro Zone aus /api/ml/dauer-drift. */
export interface DauerDriftZone {
  mae_heuristik: number | null
  mae_ml: number | null
  n_bewertet: number
  n_ml_bewertet: number
  ampel: 'gruen' | 'gelb' | 'rot' | 'keine_daten' | 'nur_heuristik'
}

export interface DauerDriftAntwort {
  fenster_tage: number
  zonen: Record<string, DauerDriftZone>
}

/** T-0227: Tagesplan-Vorschau "heute" / "morgen". */
export interface TagesplanWetter {
  standort_id: string
  // T-0575: `null` = keine Wetterdaten fuer den Tag (Abfrage gescheitert),
  // NICHT "kein Regen". Vorher war das Feld `number` und lieferte 0.0, was
  // von einer echten Trockenprognose nicht zu unterscheiden war.
  regen_summe_mm: number | null
  max_temp_c: number | null
  min_temp_c: number | null
  et0_summe_mm: number | null
  /** T-0575: Wieviele Vorhersage-Stunden die Aggregate tragen. 0 = nichts. */
  stunden_im_tag?: number
}

export interface TagesplanEintrag {
  zone_id: string
  zone_name: string
  modus: string
  soll_bewaessern: boolean
  empfehlungs_typ: string
  grund: string
  geplante_zeit: string | null    // ISO
  dauer_min: number | null
  // T-0576: im Giessfenster-Aktivmodus die Klammer, sonst die alten Fenster.
  bevorzugte_zeiten: string[]     // ["06:00-07:00", ...]
  giessfenster?: GiessfensterInfo | null
  aktive_strategie: string
  hahn_cluster: string | null
  exklusiv: boolean
  standort_id: string
  tage_bis_welkepunkt: number | null
  // T-0295: Plateau-Transparenz (T-0291) -- dauer_min ist eine plateau-
  // begrenzte Einzeldose; bei dosen_bis_ziel > 1 erreicht sie das Ziel NICHT
  // in einem Lauf.
  erwarteter_endwert_pp: number | null
  einzeldosis_max_pp: number | null
  dosen_bis_ziel: number | null
}

export interface Tagesplan {
  tag: 'heute' | 'morgen'
  datum: string                   // ISO-date YYYY-MM-DD
  wetter_pro_standort: TagesplanWetter[]
  eintraege: TagesplanEintrag[]
}

/** T-0228 Stufe 2: Wartungs-Fenster pro Zone. UI-Toggle in V0-Karte
 *  setzt ein offenes Fenster (`bis_am=null`); Konsumenten (Heuristik
 *  via sensor_backfill, spaeter Leck/ML/Schwellen-Vorschlag) pausieren
 *  waehrend des Fensters. */
export interface WartungsFenster {
  id: number
  zone_id: string
  von_am: string
  bis_am: string | null
  grund: string
  angelegt_am: string
}

/** T-0228 Stufe 1: Pflege-/Wartungs-Erinnerung. Persistiert in
 *  Tabelle `pflege_erinnerung`. Frontend rendert die offenen mit
 *  faellig_am im Vorlauf-Fenster (Default 3 Tage). */
export interface PflegeErinnerung {
  id: number
  zone_id: string | null
  typ: string
  faellig_am: string                     // ISO
  intervall_tage: number | null
  beschreibung: string
  quelle: string                         // 'manuell' / 'memory' / ...
  angelegt_am: string
  erledigt_am: string | null
}

/** T-0238: Betriebsstatus-Zentrale. Aggregat aus
 *  /api/ops/betriebsstatus -- ein Render-Slot fuer Endpoint-Health,
 *  Husqvarna-Cadence, ML-Retrain, Backup, Watchdog-Push, Sensor-Warnungen.
 *  Schliesst T-0246-Scope ab (Backend-Version + Konfig-Status). */
export interface EndpointHealthEintrag {
  endpoint: string
  status: string
  letzte_pruefung: string
  letzter_erfolg: string | null
  details: string
}

export interface Betriebsstatus {
  zeitstempel: string
  system: {
    version: string
    konfiguriert: boolean
    zonen_anzahl: number | null
  }
  endpoints: EndpointHealthEintrag[]
  husqvarna: {
    letzter_beat: string | null
    beats_24h: number
    stale: boolean
  }
  ml: {
    ist_geladen: boolean
    trainiert_am: string | null
    modell_version?: string
    // T-0397 (F7): Anzahl der Pro-Cluster-Modelle (die real vorhersagen);
    // `trainiert_am` bezieht sich dagegen aufs Legacy-/Basismodell.
    cluster_count?: number
    horizonte?: number[]
  }
  backup: {
    verzeichnis: string | null
    letzter_snapshot: string | null
    letzter_snapshot_zeit: string | null
    snapshot_count: number
    spiegel_aktiv: boolean
  }
  watchdog_letzter_push: {
    zeit: string | null
    trigger: string | null
    zone: string | null
  }
  offene_sensor_warnungen: number
}

export interface OpsTimelineEintrag {
  id: string
  zeitstempel: string
  typ: string
  severity: 'KRITISCH' | 'AKTION' | 'WETTER' | 'ROUTINE'
  zone_id: string | null
  zone_name: string | null
  scope: string
  scope_ref: string
  betroffene_zonen: string[]
  titel: string
  details: string
  meta: Record<string, string | number | boolean | null>
}

export interface OpsTimelineAntwort {
  eintraege: OpsTimelineEintrag[]
  aggregiert: {
    routine_unterdueckt: number
  }
}

/** T-0066: Dry-Run-Gießempfehlung pro Zone für das Dashboard-Panel.
 *  Spiegel von `backend/bewaesserung/modelle.py::GiessEmpfehlung`. */
export interface GiessEmpfehlung {
  zone_id: string
  zeitstempel: string
  soll_bewaessern: boolean
  blocker_typ:
    | 'FEUCHTE_OK'
    | 'KEINE_MESSUNG'
    | 'REGEN_ERWARTET'
    | 'ZEITFENSTER'
    | 'BUDGET_ERSCHOEPFT'
    | 'PAUSE_AKTIV'
    | null
  grund: string
  feuchte_aktuell: number | null
  effektive_schwelle: number | null
  dauer_s_heuristik: number | null
  liter_heuristik: number | null
  // T-0535: feste Teststufe des laufenden Dosis-Tests statt berechneter
  // Dosis — gesetzt genau dann, wenn der Auto-Loop sie auch faehrt.
  // Steht an der SPITZE der Dauer-Hierarchie (siehe `dauerHauptSekunden`),
  // weil sie die real gefahrene Dauer ist. `dauer_s_heuristik` bleibt
  // daneben die echte Heuristik-Zahl (Drift-Vergleich), wird NICHT ersetzt.
  dauer_s_dosis_test: number | null
  // T-0089: Liter passend zur tatsaechlich angezeigten Hauptdauer
  // (`dauer_s_ml` bei mlPrimaer, sonst `dauer_s_empfehlung`, sonst
  // `dauer_s_heuristik`). Ersetzt die alte Anzeige von liter_heuristik
  // bei kausaler Empfehlung.
  liter_haupt: number | null
  // T-0085: Wirkungsrate-Aufloesung pro Zone — manueller Konfig-Override,
  // automatisch berechneter Median aus letzten Bewaesserungs-Events,
  // oder globaler Default 1.0 wenn keine Daten.
  delta_pp_pro_minute_wert: number | null
  delta_pp_pro_minute_quelle:
    | 'manuell'
    | 'kalibrierung'
    | 'default'
    | 'keine'
  ml_aktiv: boolean
  ml_wirksam: boolean
  dauer_s_ml: number | null
  modell_version: string | null
  // T-0164: Status-Grund warum kein ML-Wert da ist (oder "ok").
  // Wert | Bedeutung
  // -----+----------
  // ok                | dauer_s_ml ist gesetzt
  // konfig_aus        | ml_bewaesserungs_response.aktiv=false
  // service_aus       | kein Response-Service initialisiert
  // modell_fehlt      | Modell-Datei fuer Zone fehlt
  // sensor_ueber_ziel | Sensor schon ueber Ziel-Schwelle
  // inverse_kein_wert | Modell-inverse returnte None (Edge-Case)
  // inferenz_fehler   | Exception in der Inferenz (geloggt)
  // kein_bedarf       | Empfehlungs-Klassifikation = kein_bedarf
  // zone_unbekannt    | Zone-ID nicht in der Konfig
  // zone_monitoring   | Zone im Monitoring-Modus
  // keine_messung     | Keine gueltige Feuchtemessung
  ml_status_grund: string | null
  drift_ampel: 'gruen' | 'gelb' | 'rot' | null
  drift_mae_heuristik: number | null
  drift_mae_ml: number | null
  drift_n_bewertet: number | null
  // T-0075: kausale Empfehlung
  welkepunkt_wert: number | null
  welkepunkt_quelle:
    | 'manuell'
    | 'kalibrierung'
    | 'tagesmin_schaetzung'
    | 'feuchte_kritisch_fallback'
    | 'keine'
  optimum_min: number | null
  optimum_max: number | null
  feldkapazitaet_wert: number | null
  feldkapazitaet_quelle: 'kalibrierung' | 'keine'
  prognose_6h: number | null
  prognose_12h: number | null
  prognose_24h: number | null
  prognose_quelle: 'ml' | 'heuristik' | 'keine'
  tage_bis_welkepunkt: number | null
  /** T-0105: Tage bis zur strategie-spezifischen Reserve-Grenze.
   *  Bei HAEUFIG_KLEIN = Tage bis Wohl-Min, sonst = tage_bis_welkepunkt. */
  tage_bis_reserve_grenze?: number | null
  /** T-0105: Label fuer die Reserve-Grenze ('Welkepunkt' | 'Wohl-Min'). */
  reserve_grenze_label?: string
  dauer_s_empfehlung: number | null
  deckung_nach_giessen_tage: number | null
  empfehlungs_typ: 'akut' | 'praeventiv' | 'wohlfuehl_grenze' | 'kein_bedarf'
  erklarung_kurz: string
  erklarung_lang: string
  // T-0103: aktive Bewaesserungs-Strategie aus ZonenKonfig.
  // KORRIDOR (Default), HAEUFIG_KLEIN (Bambus), SELTEN_GROSS, KONSTANT_NIEDRIG.
  aktive_strategie:
    | 'korridor'
    | 'haeufig_klein'
    | 'selten_gross'
    | 'konstant_niedrig'
  // T-0086: Mehrfach-Takt-Empfehlung. Wenn die rechnerische Empfehlungs-
  // Dauer max_dauer_sekunden uebersteigt, wird die Hauptdose auf
  // max_dauer geclippt und die Restdauer als Folge-Dose ausgewiesen.
  folge_dose_dauer_s: number | null
  folge_dose_verzoegerung_h: number | null
  folge_dose_liter: number | null
  // T-0291: Plateau-Transparenz. Die empfohlene Einzeldosis ist gedeckelt
  // (~einzeldosis_max_pp); bei grossem Ziel-Abstand braucht das Ziel
  // mehrere Dosen (dosen_bis_ziel > 1) -- die flache Dauer ist kein Bug.
  erwarteter_endwert_pp: number | null
  einzeldosis_max_pp: number | null
  dosen_bis_ziel: number | null
  // T-0356: Tagesbudget-Transparenz. Das Budget ist eine Runaway-Notbremse
  // (kein Notfall) -- die Karte zeigt ruhig "voll (X/Y min)" statt zu
  // alarmieren und spiegelt das EFFEKTIVE Budget (Single Source). Bei
  // kritischer Trockenheit hebt das Backend das Budget via Notreserve
  // (`tages_budget_kritisch_faktor`, _effektives_tagesbudget) -- dann ist
  // `budget_effektiv_s` der angehobene Wert und `budget_notreserve_aktiv`
  // true. Alle in Sekunden, passend zu `tagesverbrauch_s`. Optional:
  // aeltere Backends ohne diese Felder -> Karte faellt auf das ruhige
  // Label ohne Zahlen zurueck.
  tagesverbrauch_s?: number | null
  budget_effektiv_s?: number | null
  budget_basis_s?: number | null
  budget_notreserve_aktiv?: boolean | null
  // T-0378: true, wenn die min_pause wegen kritischer Trockenheit
  // uebersprungen wurde. Gegenstueck zu `budget_notreserve_aktiv` -- ohne
  // dieses Feld zeigte die Karte weiter "Min-Pause aktiv", waehrend die
  // Engine bereits giesst (die Karte wuerde der Entscheidung widersprechen).
  // Optional, damit aeltere Backends die Karte nicht brechen.
  pause_bypass_kritisch_aktiv?: boolean | null
}

/* T-0080d: Drift-API-Response fuer Frontend-MAE-Tile. */

export type MlDriftAmpel = 'gruen' | 'rot' | 'unbekannt' | 'backlog' | 'keine_daten'

export interface MlDriftHorizont {
  horizont_h: number
  mae_aktuell: number | null
  n: number
  n_offen_im_fenster: number
  n_offen_total: number
  letzte_evaluierung: string | null
  mae_baseline: number | null
  ampel: MlDriftAmpel
}

export interface MlDriftAntwort {
  zone_id: string | null
  fenster_tage: number
  baseline_trainiert_am: string | null
  horizonte: MlDriftHorizont[]
}

/* T-0080e: Drift-Inspektor — rohe Zeile aus ml_vorhersage_log. */

export interface MlDriftLogEintrag {
  zeitstempel: string
  zone_id: string
  horizont_h: number
  prognose_ziel_zeit: string
  prognose_feuchte: number
  prognose_q10: number | null
  prognose_q90: number | null
  ist_feuchte: number | null
  abweichung: number | null
  modell_version: string
  evaluiert_am: string | null
}

export interface MlDriftLogAntwort {
  zone_id: string | null
  horizont: number | null
  n_zurueck: number
  nur_evaluiert: boolean
  eintraege: MlDriftLogEintrag[]
}

/** T-0200: Erlaubte Sensor-Fenster fuer den Dashboard-Snapshot. */
export type SnapshotFenster = '24h' | '48h' | '7d' | '30d'

/** T-0200: Pro Zone gelieferte Sub-Daten im Dashboard-Snapshot.
 *
 * - `zone` ist byte-identisch zu einem Element aus `/api/zonen`.
 * - `messwerte[fenster]` ist die gleiche Liste wie aus
 *   `/api/zonen/{id}/messwerte?stunden=<fenster>`.
 * - `empfehlung` ist das GiessEmpfehlung-Modell aus
 *   `/api/zonen/{id}/empfehlung-jetzt`.
 * - `ml_vorhersage` spiegelt `/api/ml/vorhersage/{id}` — Horizont-Dict
 *   oder leeres Dict bei Fehler.
 * - `ml_vorhersage_fehler` ist der Fehler-Grund (`null` bei Erfolg).
 */
export interface DashboardSnapshotZone {
  zone: Zone
  empfehlung: GiessEmpfehlung
  messwerte: Partial<Record<SnapshotFenster, Messwert[]>>
  ml_vorhersage: Record<string, MLVorhersage>
  ml_vorhersage_fehler: string | null
}

export interface DashboardSnapshot {
  zeitstempel: string
  fenster: SnapshotFenster[]
  ml_verfuegbar: boolean
  zonen: DashboardSnapshotZone[]
}
