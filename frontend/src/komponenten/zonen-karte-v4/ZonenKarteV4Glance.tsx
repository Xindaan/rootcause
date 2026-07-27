/* =========================================================
   ZonenKarteV4Glance -- Schicht 1 der Zonen-Karte v3.
   In <2s lesbar: Header (Name, Sub, Badges, Quellen), Ist-Feuchte gross
   mit Trend-Pfeil, OptimumDotsV4 (falls FYTA-Daten), Schwellen-Bar mit
   User-Korridor + Optimum-Kontur + Vorschlag + Sub-Sensor-Marker,
   ML-24h als Bar-Suffix (Q19).
   Bedingungen:
     - OptimumDotsV4: nur wenn zone.optima existiert.
     - Vorschlag-Linie: nur wenn SchwellenVorschlag.basis === 'berechnet'.
     - Sub-Sensor-Marker: nur wenn zone.sensoren mehr als 1 hat.
     - ML-Suffix: nur wenn mlVorhersage einen 24h-Wert hat (Q19).
   ========================================================= */

import type { Zone, MLVorhersage, SchwellenVorschlag, GiessEmpfehlung, AusschlussFenster } from '../../typen'
import { formatTrend, datumZeitFormat, strategieKlartext } from '../../hilfsfunktionen'
import { feuchteBand } from './aktion-state-v4'

/** Stale-Check ausserhalb der Komponente, damit Date.now() den
 *  React-Hooks-purity-Lint nicht ausloest. Re-Render alle 30s durch
 *  Parent (holeZonen) reicht fuer ausreichend frische Anzeige. */
function pruefeStale(isoStr: string | null | undefined): {
  stale: boolean
  tooltip: string
  /** Kurz-Label fuer die sichtbare "Letztes Update"-Zeile: "vor 3 min",
   *  "vor 1.4 h", "vor 2 d" oder "kein Update" wenn nicht gesetzt. */
  kurzLabel: string
} {
  if (!isoStr) return {
    stale: false,
    tooltip: 'Keine Update-Zeit verfuegbar.',
    kurzLabel: 'kein Update',
  }
  const ms = new Date(isoStr).getTime()
  const altMs = Date.now() - ms
  const altMin = altMs / 60_000
  const altStunden = altMs / 3600_000
  const altTage = altStunden / 24
  const stale = altStunden > 2
  let kurzLabel: string
  if (altMin < 1) kurzLabel = 'gerade'
  else if (altMin < 90) kurzLabel = `vor ${Math.round(altMin)} min`
  else if (altStunden < 48) kurzLabel = `vor ${altStunden.toFixed(1)} h`
  else kurzLabel = `vor ${altTage.toFixed(1)} d`
  return {
    stale,
    tooltip: `Letzte Messung: ${datumZeitFormat(isoStr)}${stale ? ` -- VERALTET (vor ${altStunden.toFixed(1)} h)` : ''}`,
    kurzLabel,
  }
}
/** Q43/T-0221: Prueft, ob die Zone JETZT in einem ML-Ausschluss-Fenster
 *  (Sensor-Kalibrierung) liegt. Modul-Ebene wegen `Date.now()` -- analog
 *  pruefeStale, damit der React-Hooks-Purity-Lint nicht ausloest. */
function pruefeAusschluss(fenster: AusschlussFenster[] | undefined): {
  aktiv: boolean
  zweck: 'sensor_kalibrierung' | 'event_ignore' | null
  bisLabel: string | null
  grund: string | null
} {
  const jetzt = Date.now()
  const aktive = (fenster ?? []).filter(f => {
    const von = new Date(f.von).getTime()
    const bis = new Date(f.bis).getTime()
    return von <= jetzt && jetzt <= bis
  })
  if (aktive.length === 0) {
    return { aktiv: false, zweck: null, bisLabel: null, grund: null }
  }
  // T-0304: zwei Zweck-Typen. sensor_kalibrierung hat Vorrang, falls beide
  // gleichzeitig aktiv sind (Werte-unzuverlaessig ist schwerwiegender als
  // nur-Events-ignorieren). undefined zweck = sensor_kalibrierung (Default).
  const istKal = (f: AusschlussFenster) =>
    (f.zweck ?? 'sensor_kalibrierung') === 'sensor_kalibrierung'
  const gewinner = aktive.find(istKal) ?? aktive[0]
  return {
    aktiv: true,
    zweck: istKal(gewinner) ? 'sensor_kalibrierung' : 'event_ignore',
    bisLabel: new Date(gewinner.bis).toLocaleDateString('de-DE', {
      day: '2-digit', month: '2-digit',
    }),
    grund: gewinner.grund || null,
  }
}
import { OptimumDotsV4 } from './OptimumDotsV4'
import { QuellenIndikatorV4 } from './QuellenIndikatorV4'
import { FeuchteRegimeBadge } from '../FeuchteRegimeBadge'

interface Props {
  zone: Zone
  trendPpProH: number | null
  schwellenVorschlag?: SchwellenVorschlag
  mlVorhersage?: Record<string, MLVorhersage>
  /** T-0370: 'stumm' = Datenbasis unzuverlaessig/fehlt -- neutrale
   *  Wert-Farbe statt gruen (kein OK aus toten Daten). */
  zustand: 'kritisch' | 'warn' | 'info' | 'ok' | 'stumm'
  /** GiessEmpfehlung -- nur fuer Strategie-Label im Sub-Titel
   *  ("KANAL 2 · haeufig + klein"). Optional, weil pro Zone meist
   *  konstant und nicht alle Zonen liefern eine Empfehlung. */
  empfehlung?: GiessEmpfehlung | null
  /** T-0221/Q3a: "DSWC 1/2" -- nur in Multi-DSWC-Setups gesetzt, sonst
   *  null. Wird vom Parent (App.tsx) zonenweit berechnet. */
  dswcLabel?: string | null
}

// Obs01 (T-0320): die Sensor-Warnungs-Maps leben jetzt in ZonenKarteV4Action.
// Warnungen sind ein Handlungssignal und wandern aus dem Glance-Header in die
// Action-Zeile -- "ein Badge sagt was die Zone IST, eine Warnung was zu TUN ist".

export function ZonenKarteV4Glance({
  zone, trendPpProH, schwellenVorschlag, mlVorhersage, zustand, empfehlung,
  dswcLabel,
}: Props) {
  const ist = zone.aktuelle_feuchte
  const sv = schwellenVorschlag
  const vorschlagAktiv = sv?.basis === 'berechnet'
    && sv.min_vorschlag != null && sv.max_vorschlag != null
  const subSensoren = (zone.sensoren ?? []).filter(s => s.boden_feuchte != null)
  const hatSubSensoren = subSensoren.length > 1
  const trend = formatTrend(trendPpProH)
  // Stale-Check: wenn letztes Update aelter als 2h, ist die Anzeige
  // misstrauisch zu betrachten (Sensor schlaeft / Soft-Ban / Akku leer).
  const { stale, tooltip: updateTooltip, kurzLabel: updateKurz } = pruefeStale(zone.letztes_update)
  // Q19: nur der 24h-ML-Wert als Bar-Suffix. 6h/12h + q10-q90-Band
  // stehen im Detail-Chart -- ersetzt die drei Header-Werte (MlKompakt).
  const ml24h = mlVorhersage?.['24h']?.feuchte_prognose ?? null
  // Obs01 (T-0320): sichtbare Warnungen werden jetzt in ZonenKarteV4Action
  // berechnet + gerendert (Warnung = Handlungssignal -> Action-Zeile).
  // Q2/Q3a: Sub-Zeile = Beet/Topf · DSWC · Kanal · Flaeche. Die
  // Strategie (Q4) wandert in einen ⓘ-Anchor, nicht in die Sub-Zeile.
  const subTeile = subZeilenTeile(zone, dswcLabel)
  const strategie = strategieKlartext(empfehlung?.aktive_strategie)
  // Q15: Welkepunkt aus dem Plateau-Modell pro Zone (T-0197) statt der
  // globalen Konfig-Konstante; faellt auf feuchte_kritisch zurueck.
  const welkepunkt = empfehlung?.welkepunkt_wert ?? zone.feuchte_kritisch ?? null
  // Q40: Aggregations-Hinweis fuer den Tooltip auf der grossen %-Zahl.
  const nSensoren = zone.sensoren?.length ?? 0
  // T-0397 (F3): Wert-Farbe aus der Feuchte-Einordnung, NICHT aus dem Aktions-
  // Zustand. Vorher faerbte eine akut-ML-Empfehlung den Ist-Wert kritisch-rot,
  // obwohl der Wert im Korridor lag (bambuswald 70%). 'stumm' (Sensor tot/
  // fehlend) bleibt neutral; 'nass' liest sich blau (info) statt rot.
  const band = feuchteBand(zone)
  const pctKlasse = zustand === 'stumm'
    ? 'stumm'
    : band === 'nass' ? 'info' : band
  // Q43 / T-0304: aktives ml_ausschluss_fenster + sein Zweck (Sensor-
  // Kalibrierung = Werte unzuverlaessig, vs. Event-Ignore = Sensor ok).
  const ausschluss = pruefeAusschluss(zone.ausschluss_fenster)

  return (
    <div className="zk-glance">
      <div className="zk-head">
        <div className="zk-head__l">
          {/* Obs01 (T-0320): Karten-Modus-Qualifier (Kalibrierung / Event-
              Ignore / Regime / AquaBloom) wandern an den Namen -- sie sagen
              "was ist diese Zone". Echte Glance-Badges bleiben nur Modus +
              Sensor-Pills; Warnungen stehen in der Action-Zeile. */}
          <div className="zk4-nm-row">
            <span className="zk-head__nm">{zone.name}</span>
            <span className="zk4-nm-tags">
              {ausschluss.aktiv && ausschluss.zweck === 'sensor_kalibrierung' && (
                <span
                  className="zk-badge zk-badge--kalibrierung"
                  title={`Sensor-Kalibrierung laeuft (bis ${ausschluss.bisLabel}). ML-Vorhersage und Automatik pausieren so lange.${ausschluss.grund ? `\n\nGrund: ${ausschluss.grund}` : ''}`}
                >
                  ⚙ Kalibrierung bis {ausschluss.bisLabel}
                </span>
              )}
              {ausschluss.aktiv && ausschluss.zweck === 'event_ignore' && (
                <span
                  className="zk-badge zk-badge--event-ignore"
                  title={`Kanal-Events werden ignoriert (bis ${ausschluss.bisLabel}) -- z.B. Fremd-Beregnung ueber diesen Ventilkanal. Sensor-Werte und Empfehlung gelten normal.${ausschluss.grund ? `\n\nGrund: ${ausschluss.grund}` : ''}`}
                >
                  🌱 Events ignoriert bis {ausschluss.bisLabel}
                </span>
              )}
              <FeuchteRegimeBadge regimes={zone.feuchte_regime} aktiv={zone.feuchte_regime_aktiv} />
              {zone.aquabloom_konfig && (
                <span
                  className="zk-badge zk-badge--aquabloom"
                  title={`AquaBloom-Pumpe${zone.aquabloom_konfig.aktiv ? '' : ' (ausserhalb der Saison)'}.`}
                >
                  AQUABLOOM{zone.aquabloom_konfig.intervall_stunden != null
                    ? ` · ${Math.round(zone.aquabloom_konfig.intervall_stunden)}h`
                    : ''}
                </span>
              )}
            </span>
          </div>
          {(subTeile.length > 0 || strategie) && (
            <span className="zk-head__sub">
              {subTeile.join(' · ')}
              {strategie && (
                <span
                  className="zk-sub-info"
                  title={`Bewässerungs-Strategie: ${strategie}`}
                >i</span>
              )}
            </span>
          )}
          {/* Obs01: nur noch Modus + Sensor-Pills als echte Glance-Badges. */}
          <div className="zk-head__badges">
            <ModusBadge zone={zone} />
            <QuellenIndikatorV4 sensoren={zone.sensoren} />
          </div>
        </div>
        <div className="zk-head__r">
          <span
            className={`zk-head__pct zk-head__pct--${pctKlasse} ${stale ? 'zk-head__pct--stale' : ''}`}
            title={ist != null
              ? `Aktuelle Boden-Feuchte: ${Math.round(ist)}% (${nSensoren > 1 ? `Median aus ${nSensoren} Sensoren` : 'Einzel-Sensor'}).\n${updateTooltip}`
              : updateTooltip}
          >
            {ist != null ? Math.round(ist) : '-'}
            {stale && <span className="zk-head__stale-mark" aria-label="veraltete Messung"> ⚠</span>}
          </span>
          {/* T-0201-Folge: Trend + Update in eine Mono-Zeile fusionieren,
              statt zwei vertikale Eintraege. Spart ~14 px pro Karte. */}
          <span className="zk-head__meta">
            <span
              className={`zk-head__trend zk-head__trend--${trend.klasse}`}
              title="Trend in Prozentpunkten pro Stunde, berechnet aus den letzten 6h Messwerten."
            >
              {trend.text}
            </span>
            <span
              className={`zk-head__update ${stale ? 'zk-head__update--stale' : ''}`}
              title={updateTooltip}
            >
              · {updateKurz}
            </span>
          </span>
        </div>
      </div>

      <OptimumDotsV4 zone={zone} />

      {/* Q19: Schwellen-Bar + 24h-ML-Suffix in einer Zeile. Frueher zeigte
          der Header drei ML-Werte (MlKompakt) -- jetzt nur die 24h-Zahl
          direkt an der Bar, 6h/12h liegen im Detail-Chart. */}
      <div className="zk-bar-row">
        <div className="zk-bar-wrap">
          <SchwellenBar
            ist={ist}
            subIstWerte={hatSubSensoren ? subSensoren.map(s => s.boden_feuchte!) : []}
            schwelleMin={zone.feuchte_schwelle_min}
            schwelleMax={zone.feuchte_schwelle_max}
            welkepunkt={welkepunkt}
            optimumMin={zone.optimum_feuchte_min ?? sv?.optimum_min ?? null}
            optimumMax={zone.optimum_feuchte_max ?? sv?.optimum_max ?? null}
            vorschlagMin={vorschlagAktiv ? sv!.min_vorschlag : null}
            vorschlagMax={vorschlagAktiv ? sv!.max_vorschlag : null}
          />
        </div>
        {ml24h != null && (
          <span
            className="zk-ml-suffix"
            title="ML-Prognose der Boden-Feuchte in 24h. 6h/12h und das q10-q90-Konfidenzband stehen im Detail-Chart (DETAILS aufklappen)."
          >
            <span className="zk-ml-suffix__lbl">{zustand === 'ok' ? '→' : '→ 24h:'}</span>
            <b>{Math.round(ml24h)}</b>
          </span>
        )}
      </div>
    </div>
  )
}

/** T-0370: scharf vs. Shadow ehrlich unterscheiden. `autonom_scharf`
 *  kommt aus dem Backend (3-stufige Logik T-0334); modus=automatik OHNE
 *  scharf = Shadow (Entscheidungen nur protokolliert, kein Ventil).
 *  Fehlt das Feld (aelteres Backend), bleibt das alte AUTOMATIK-Label. */
function ModusBadge({ zone }: { zone: Zone }) {
  if (zone.modus === 'automatik') {
    if (zone.autonom_scharf === false) {
      return (
        <span
          className="badge zk4-shadow"
          title={'Shadow-Modus: die Automatik berechnet und protokolliert Entscheidungen fuer diese Zone, schaltet aber KEIN Ventil (Opt-in oder globaler Master-Switch aus). Giessen laeuft manuell.'}
        >
          SHADOW
        </span>
      )
    }
    return (
      <span
        className="badge automatik"
        title="Autonome Automatik: die Engine giesst diese Zone selbststaendig."
      >
        AUTOMATIK
      </span>
    )
  }
  return (
    <span
      className="badge monitoring"
      title="Monitoring: nur Beobachtung + Empfehlungen, keine Ventilsteuerung."
    >
      MONITORING
    </span>
  )
}

interface TickLabel { pos: number; text: string; color?: string; prio: number }

/** T-0397 (F18): Tick-Labels der Schwellen-Bar kollisionsfrei auswaehlen.
 *  Die Labels sind an ihrer Wert-Position absolut platziert; nahe Werte
 *  (z.B. Welke 20 + Min 22) verschmolzen sonst zu Phantomzahlen ("2022").
 *  Prioritaet: Endpunkte (0/100) > Korridor (min/max) > Welke > Ist. Bei
 *  Kollision (< MIN_ABSTAND Prozentpunkte) gewinnt die hoehere Prioritaet;
 *  verworfene Werte bleiben in den bestehenden Tooltips sichtbar (Ist als
 *  grosse Zahl, Korridor/Welke an ihren Markern). */
function sichtbareTickLabels(p: {
  ist: number | null
  schwelleMin: number
  schwelleMax: number
  welkepunkt: number | null
}): TickLabel[] {
  const MIN_ABSTAND = 7 // Prozentpunkte auf der 0-100-Skala
  const kandidaten: TickLabel[] = [
    { pos: 0, text: '0', prio: 0 },
    { pos: 100, text: '100', prio: 0 },
    { pos: p.schwelleMin, text: String(p.schwelleMin), color: 'var(--farbe-gesund)', prio: 1 },
    { pos: p.schwelleMax, text: String(p.schwelleMax), color: 'var(--farbe-gesund)', prio: 1 },
  ]
  if (p.welkepunkt != null) {
    kandidaten.push({ pos: p.welkepunkt, text: String(p.welkepunkt), color: 'var(--farbe-gefahr)', prio: 2 })
  }
  if (p.ist != null) {
    kandidaten.push({ pos: Math.round(p.ist), text: String(Math.round(p.ist)), prio: 3 })
  }
  // Greedy nach Prioritaet: hoehere Prioritaet belegt ihren Slot zuerst,
  // spaetere Kandidaten fallen weg, wenn sie einem gesetzten Label zu nah sind.
  const gesetzt: TickLabel[] = []
  for (const k of [...kandidaten].sort((a, b) => a.prio - b.prio)) {
    if (gesetzt.every(g => Math.abs(g.pos - k.pos) >= MIN_ABSTAND)) gesetzt.push(k)
  }
  return gesetzt.sort((a, b) => a.pos - b.pos)
}

function SchwellenBar(p: {
  ist: number | null
  subIstWerte: number[]
  schwelleMin: number
  schwelleMax: number
  /** Welkepunkt (`zone.feuchte_kritisch`) -- rote Vertikallinie links
   *  vom User-Korridor, zeigt wo die Pflanze welkt. Macht Dringlichkeit
   *  sichtbar: Ist=55, Welke=40 -> 15pp Reserve. */
  welkepunkt: number | null
  optimumMin: number | null
  optimumMax: number | null
  vorschlagMin: number | null
  vorschlagMax: number | null
}) {
  return (
    <>
      <div
        className="zk-bar"
        title="Feuchte-Skala 0-100%. Gruener Balken = Bewaesserungs-Korridor (User-Schwellen). Gestrichelt = Pflanzen-Optimum (FYTA). Goldlinie unten = empirischer Vorschlag aus letzten 30 Tagen. Schwarzer Strich = aktuelle Feuchte, duenne graue Striche = Sub-Sensoren."
      >
        {p.optimumMin != null && p.optimumMax != null && (
          <div
            className="zk-bar__optimum"
            style={{ left: `${p.optimumMin}%`, right: `${100 - p.optimumMax}%` }}
            title={`Pflanzen-Optimum ${p.optimumMin}-${p.optimumMax}% (FYTA-Empfehlung).`}
          />
        )}
        <div
          className="zk-bar__korridor"
          style={{ left: `${p.schwelleMin}%`, right: `${100 - p.schwelleMax}%` }}
          title={`Bewaesserungs-Korridor ${p.schwelleMin}-${p.schwelleMax}% (User-Schwellen, Automatik haelt diesen Bereich).`}
        />
        {p.vorschlagMin != null && p.vorschlagMax != null && (
          <div
            className="zk-bar__vorschlag"
            style={{ left: `${p.vorschlagMin}%`, right: `${100 - p.vorschlagMax}%` }}
            title={`Vorschlag ${p.vorschlagMin}-${p.vorschlagMax}% (empirisch aus letzten 30 Tagen, noch nicht aktiv).`}
          />
        )}
        {p.subIstWerte.map((v, i) => (
          <div
            key={i}
            className="zk-bar__ist zk-bar__ist--sub"
            style={{ left: `${v}%` }}
            title={`Sub-Sensor ${i + 1}: ${Math.round(v)}%`}
          />
        ))}
        {p.welkepunkt != null && (
          <div
            className="zk-bar__welke"
            style={{ left: `${p.welkepunkt}%` }}
            title={`Welkepunkt: ${p.welkepunkt}% -- darunter welkt die Pflanze sichtbar. Reserve aktuell ${p.ist != null ? Math.round(p.ist) - p.welkepunkt : '?'}pp.`}
          />
        )}
        {p.ist != null && (
          <div
            className="zk-bar__ist"
            style={{ left: `${p.ist}%` }}
            title={`Aktuelle Feuchte (Aggregat): ${Math.round(p.ist)}%`}
          />
        )}
      </div>
      <div className="zk-bar__labels">
        {sichtbareTickLabels(p).map((t, i) => (
          <span key={i} style={{ left: `${t.pos}%`, color: t.color }}>{t.text}</span>
        ))}
      </div>
    </>
  )
}

/** Q2/Q3a (T-0221): Bestandteile der Karten-Sub-Zeile
 *  "BEET · DSWC 1 · KANAL 2 · 8 m²". Teile sind bereits korrekt
 *  geschrieben (Grossbuchstaben ausser der Einheit m²) -- der Aufrufer
 *  joint sie OHNE toUpperCase. Die Strategie steht NICHT hier, sondern
 *  als ⓘ-Anchor (Q4). */
function subZeilenTeile(zone: Zone, dswcLabel?: string | null): string[] {
  const teile: string[] = [zone.ist_topf ? 'TOPF' : 'BEET']
  if (dswcLabel) teile.push(dswcLabel)
  if (zone.ventil_kanal != null) teile.push(`KANAL ${zone.ventil_kanal}`)
  // Flaeche nur fuer Beete -- bei Toepfen ist die m²-Angabe nicht
  // aussagekraeftig (Topf-Volumen != Grundflaeche).
  if (!zone.ist_topf && zone.flaeche_m2 != null) {
    const f = zone.flaeche_m2
    const txt = f >= 10 ? String(Math.round(f)) : String(Math.round(f * 10) / 10)
    teile.push(`${txt} m²`)
  }
  return teile
}
