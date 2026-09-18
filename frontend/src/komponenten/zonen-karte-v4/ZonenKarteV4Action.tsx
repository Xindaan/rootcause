/* =========================================================
   ZonenKarteV4Action -- Schicht 2 der Zonen-Karte v3.
   Eine Zeile mit Titel + Wann + Grund + Vergleich + 1-2 Buttons.
   Vier Zustaende:
     kritisch  -- Feuchte unter feuchte_kritisch ODER Empfehlung akut.
     giessen   -- soll_bewaessern=true ohne Blocker.
     blocker   -- soll_bewaessern=false mit blocker_typ != null.
     ok        -- soll_bewaessern=false, kein Blocker.
   Zustand kommt aus getActionState(zone, empfehlung).
   Zusaetzlich:
     - Tag rechts in der Titelzeile zeigt Quelle der Dauer (ML/Heuristik).
     - Sub-Zeile "Vergleich" zeigt analog V1 den jeweils anderen Wert.
     - Buttons haben title-Tooltips mit Klartext-Beschreibung.
   ========================================================= */

import type { ReactNode } from 'react'
import type { Zone, GiessEmpfehlung } from '../../typen'
import { datumZeitFormat } from '../../hilfsfunktionen'
import type { Aktion } from './aktion-state-base'
import { getActionStateV4, type ActionZustandV4 } from './aktion-state-v4'
import { leadAusgefallen, leadAusfallDetail } from '../sensor-defekt'

// Obs01 (T-0320): Sensor-Warnungs-Maps -- aus dem V3-Glance hierher gezogen.
// Warnungen sind ein Handlungssignal und gehoeren in die Action-Zeile, nicht
// als Badge in den Glance-Header (1:1-Schema mit Backend-`SensorWarnungTyp`).
const WARNUNG_LABEL: Record<string, string> = {
  ausfall: 'Sensor-Ausfall',
  batterie_niedrig: 'Batterie niedrig',
  batterie_kritisch: 'Batterie kritisch',
  bewaesserung_ohne_wirkung: 'Bewässerung ohne Wirkung',
  sensor_eingefroren: 'Sensor eingefroren',
  // T-0433-Nachzug: fehlte hier, obwohl das Backend den Typ seit T-0416
  // liefert -- die Karte rendert ohne Eintrag den rohen Enum-String.
  fyta_kalibrier_push: 'FYTA-Kalibrierung verschoben',
  // T-0433: die vom Kanal-Trigger ausgeschlossene Zone meldet kritisch,
  // waehrend der Lead satt meldet. Kein Sensor-Defekt, ein Dissens.
  lead_divergenz: 'Ausgeschlossene Zone meldet kritisch',
  // T-0445: waehrend des Laufs kam kein neuer Messwert an -- der Max-Stop
  // urteilte auf Werten von vor dem Giessen. Gleich beim Anlegen des Typs
  // gemappt, damit sich der T-0433-Nachzug oben nicht wiederholt.
  keine_ankunft_im_lauf: 'Kein neuer Messwert während des Laufs',
}
const WARNUNG_KRITISCH = new Set([
  'ausfall',
  'batterie_kritisch',
  'bewaesserung_ohne_wirkung',
  'sensor_eingefroren',
  'lead_divergenz',
  // T-0445: haelt die Karte mit der Ops-Timeline synchron -- dort ist der
  // Typ KRITISCH (api_server `_normalisiere_sensor_eintraege`).
  'keine_ankunft_im_lauf',
])

interface Props {
  zone: Zone
  empfehlung: GiessEmpfehlung | null
  onAktion: (a: Aktion) => void
  /** Obs01 (T-0320): laeuft gerade eine Bewaesserung? Dann wird die
   *  `bewaesserung_ohne_wirkung`-Warnung unterdrueckt (Anti-Pattern T-0186). */
  bewaesserungAktiv?: boolean
  /** Beliebiger Inhalt, der am Ende des Action-Containers gerendert wird
   *  (typischerweise <ManuellesGiessen/> mit Lauf-Banner + Live-Buttons).
   *  Erspart einen visuellen Bruch zwischen "Plan/Manuell" und den
   *  Eingabe-Buttons. */
  children?: ReactNode
}

export function ZonenKarteV4Action({ zone, empfehlung, onAktion, bewaesserungAktiv, children }: Props) {
  const zustand = getActionStateV4(zone, empfehlung)
  const inhalt = bauInhalt(zustand, zone, empfehlung)
  // Obs01 (T-0320): Warnungen (eingefroren / Batterie / ohne-Wirkung) leben
  // jetzt hier -- ein Handlungssignal, das prominent UEBER der Empfehlung
  // steht. Der `bewaesserung_ohne_wirkung`-Filter spiegelt die alte
  // Glance-Logik (verstummt waehrend eines Laufs, T-0186).
  const warnungen = (zone.offene_warnungen ?? []).filter(
    w => !(bewaesserungAktiv && w.typ === 'bewaesserung_ohne_wirkung'),
  )

  return (
    <div className={`zk-action zk-action--${zustand}`}>
      {warnungen.length > 0 && (
        <div className="zk4-warnzeile">
          {warnungen.map((w, idx) => (
            <div
              key={`${w.typ}-${idx}`}
              className={WARNUNG_KRITISCH.has(w.typ)
                ? 'zk4-warn zk4-warn--kritisch'
                : 'zk4-warn zk4-warn--hinweis'}
              title={w.details ?? undefined}
            >
              <span className="zk4-warn__ic" aria-hidden>⚠</span>
              <span>
                <b>{WARNUNG_LABEL[w.typ] ?? w.typ}</b>
                {w.details ? <span className="zk4-warn__det"> · {w.details}</span> : null}
              </span>
            </div>
          ))}
        </div>
      )}
      <div className="zk-action__titel">
        <b>{inhalt.titel}</b>
        {inhalt.quelle && (
          <span
            className="zk-action__wann"
            title={inhalt.quelle === 'ML'
              ? 'Dauer aus ML-Inverse-Modell (kausale Empfehlung).'
              : inhalt.quelle === 'Heuristik'
                ? 'Dauer aus Heuristik (Wirkungsrate x Bedarf), kein aktives ML.'
                : inhalt.quelle === 'Testlauf'
                  ? 'Dosis-Test T-0535: feste Stufe statt berechneter Dosis'
                  : 'Dauer manuell gesetzt.'}
          >
            {inhalt.quelle}
          </span>
        )}
        {inhalt.wann && <span className="zk-action__wann">{inhalt.wann}</span>}
      </div>
      {inhalt.detail && (
        <div className="zk-action__detail">
          {/* T-0432: waehrend eines Laufs ist diese Zahl eine LIVE-
              Neuberechnung gegen den aktuellen Sensorwert -- nicht die
              Dauer, die gerade laeuft. Die steigt waehrend des Giessens,
              die Neuberechnung faellt also. Ununterschieden nebeneinander
              sah das aus, als haette die Automatik die Dosis verkuerzt.
              Die committete Dauer steht im Lauf-Banner (ManuellesGiessen,
              `haupt_s`); hier wird nur noch klar, dass das eine Hypothese
              ist. */}
          {bewaesserungAktiv && (
            <span className="zk-action__waere-jetzt">wäre jetzt: </span>
          )}
          {inhalt.detail}
        </div>
      )}
      {inhalt.body && (
        <div className="zk-action__body" title={inhalt.body}>{inhalt.body}</div>
      )}
      {inhalt.buttons.length > 0 && (
        <div className="zk-action__buttons">
          {inhalt.buttons.map((b, i) => (
            <button
              key={i}
              type="button"
              className={`zk-action__btn ${b.primary ? 'zk-action__btn--primary' : ''}`}
              onClick={() => onAktion(b.aktion)}
              title={b.tooltip}
            >
              {b.label}
            </button>
          ))}
        </div>
      )}
      {children && <div className="zk-action__extras">{children}</div>}
    </div>
  )
}

interface Inhalt {
  titel: string
  wann?: string
  /** Dauer-Detail-Zeile: praeventiv: 47 min · ML: 41 min · Rate 0.13 pp/min */
  detail?: ReactNode
  /** Lange erklarung_kurz als optionaler Sub-Body (klein, gedaempft). Nur
   *  gezeigt wenn vorhanden + signifikant mehr Info als die Detail-Zeile. */
  body?: string | null
  /** Quelle der Hauptdauer ("ML"/"Heuristik"/"Manuell"/null). */
  quelle?: 'ML' | 'Heuristik' | 'Manuell' | 'Testlauf' | null
  buttons: Array<{ label: string; primary?: boolean; aktion: Aktion; tooltip: string }>
}

/** Liest aus der Empfehlung, ob die Hauptdauer aus dem ML-Modell oder der
 *  Heuristik kommt (oder gar nicht gesetzt ist).
 *
 *  T-0535: laeuft der Dosis-Test scharf, gewinnt er vor beidem — die Karte
 *  zeigt dann die feste Teststufe, und die Quelle muss das benennen, sonst
 *  steht eine abweichende Zahl unter falschem Etikett. */
function dauerQuelle(
  e: GiessEmpfehlung | null,
): 'ML' | 'Heuristik' | 'Testlauf' | null {
  if (!e) return null
  if (e.dauer_s_dosis_test != null) return 'Testlauf'
  if (e.dauer_s_ml != null && e.ml_wirksam) return 'ML'
  if (e.dauer_s_ml != null || e.dauer_s_empfehlung != null || e.dauer_s_heuristik != null) return 'Heuristik'
  return null
}

/** Klartext-Label fuer e.empfehlungs_typ -- Klassifikator der Empfehlung. */
function empfehlungsTypLabel(typ: GiessEmpfehlung['empfehlungs_typ'] | undefined): string | null {
  if (!typ) return null
  switch (typ) {
    case 'akut':              return 'akut'
    case 'praeventiv':        return 'präventiv'
    case 'wohlfuehl_grenze':  return 'Wohl-Grenze'
    case 'kein_bedarf':       return null  // bei OK-Zustand kein Tag noetig
    default:                  return null
  }
}

/** Wirkungsrate als kompakter Inline-Text. Quelle (gemessen/manuell/
 *  Default) wandert in den Tooltip, nicht in den Inline-Text. */
function wirkungsrateText(e: GiessEmpfehlung | null): { kurz: string; tooltip: string } | null {
  if (!e || e.delta_pp_pro_minute_wert == null) return null
  const wert = e.delta_pp_pro_minute_wert.toFixed(2)
  const quelle = e.delta_pp_pro_minute_quelle
  const quellenLabel = quelle === 'kalibrierung' ? 'aus letzten Bewaesserungen gemessen'
    : quelle === 'manuell' ? 'manuell konfiguriert'
    : quelle === 'default' ? 'Default-Wert (keine Daten)'
    : ''
  return {
    kurz: `Rate ${wert} pp/min`,
    tooltip: `Wirkungsrate ${wert} pp/min -- ${quellenLabel}`,
  }
}

/** T-0295: Plateau-Transparenz kompakt -- "→ ~63 % (~3 Dosen)". Nur wenn die
 *  empfohlene Einzeldose plateau-begrenzt ist (dosen_bis_ziel > 1), sonst
 *  wirkt die flache Dauer wie "ignoriert Feuchte" (T-0291-Befund). Felder aus
 *  der Empfehlung (T-0291: erwarteter_endwert_pp / einzeldosis_max_pp /
 *  dosen_bis_ziel). */
function plateauText(e: GiessEmpfehlung | null): { kurz: string; tooltip: string } | null {
  if (!e || e.dosen_bis_ziel == null || e.dosen_bis_ziel <= 1
      || e.erwarteter_endwert_pp == null) return null
  const endwert = Math.round(e.erwarteter_endwert_pp)
  const maxPp = e.einzeldosis_max_pp != null ? Math.round(e.einzeldosis_max_pp) : null
  return {
    kurz: `→ ~${endwert} % (~${e.dosen_bis_ziel} Dosen)`,
    tooltip: maxPp != null
      ? `Eine Dose hebt max ~${maxPp} pp → erreicht ~${endwert} %; Ziel braucht ~${e.dosen_bis_ziel} Dosen (Plateau-Modell)`
      : `Plateau-begrenzt: erreicht ~${endwert} %, Ziel braucht ~${e.dosen_bis_ziel} Dosen`,
  }
}

/** Strukturierte Dauer-Detail-Zeile:
 *    praeventiv: 47 min · ML: 41 min · Rate 0.13 pp/min
 *  Quelle und Strategie sind NICHT hier (Quelle ist Tag im Titel, Strategie
 *  wandert in den Glance-Sub-Titel). Modell-Version weggelassen.
 *
 *  T-0266 (2026-05-26): "Heuristik" zeigt `dauer_s_empfehlung`, NICHT
 *  `dauer_s_heuristik`. Hintergrund:
 *  - `dauer_s_heuristik`: simpler Bring-zur-Min-Schwelle-Wert (z.B. 8 min
 *    wenn Sensor genau auf 60 % Schwelle steht). Diagnose, nicht Empfehlung.
 *  - `dauer_s_empfehlung`: Strategie-aware Empfehlung mit 3-Tage-Welkepunkt-
 *    Reserve / SELTEN_GROSS-Tiefendose / HAEUFIG_KLEIN-Puls (z.B. 48 min).
 *  Realfall yogaraum 26.05.: UI zeigte 8 min, Shadow-Push zeigte 48 min --
 *  zwei Quellen, ein Widerspruch fuer den User. Push hat's richtig. */
function dauerDetailZeile(e: GiessEmpfehlung | null): ReactNode {
  if (!e) return null
  const ml = e.dauer_s_ml
  // T-0266: Empfehlung (strategie-aware) bevorzugt vor heuristik (Schwellen-
  // Rueckerlangung). Falls dauer_s_empfehlung fehlt (z.B. kein Welkepunkt),
  // Fallback auf dauer_s_heuristik.
  const heuristik = e.dauer_s_empfehlung ?? e.dauer_s_heuristik
  // T-0535 im Guard: eine gesetzte Teststufe ist fuer sich schon eine
  // anzeigbare Dauer — sonst verschluckte die Zeile genau die Zahl, die
  // real gefahren wird, falls ML und Empfehlung beide fehlen.
  if (ml == null && heuristik == null && e.dauer_s_dosis_test == null) return null
  const haupt = dauerQuelle(e)
  const teile: ReactNode[] = []

  // Hauptzahl mit Empfehlungs-Typ-Prefix.
  const typLabel = empfehlungsTypLabel(e.empfehlungs_typ)
  // T-0503: Pump-Zonen (AquaBloom, kein Ventil) haben keine Heuristik-Dosis
  // — die Heuristik rechnet gegen eine Ventil-Laufzeit, die es dort nicht
  // gibt. Ohne diesen Fallback fiel `hauptMin` auf null und die ML-Dosis
  // wurde gar nicht angezeigt, obwohl das Backend sie seit heute liefert.
  const nurMl = ml != null && heuristik == null
  // T-0535: die Teststufe ist die real gefahrene Dauer und steht deshalb
  // ganz oben — vor ML und vor der kausalen Empfehlung. Die Quelle daneben
  // (`dauerQuelle`) sagt "Testlauf", damit die Zahl erklaert ist.
  const hauptMin = e.dauer_s_dosis_test != null
    ? Math.round(e.dauer_s_dosis_test / 60)
    : haupt === 'ML' && ml != null
      ? Math.round(ml / 60)
      : heuristik != null
        ? Math.round(heuristik / 60)
        : nurMl
          ? Math.round(ml / 60)
          : null
  if (hauptMin != null) {
    teile.push(
      <span key="haupt">
        {/* Bei reiner Modell-Aussage die Quelle benennen statt des
            Empfehlungs-Typs: die Zone giesst nicht automatisch, die Zahl
            ist eine Dosier-Empfehlung fuer die Pumpen-Einstellung. */}
        {nurMl
          ? <span className="zk-action__typ">Modell-Dosis: </span>
          : typLabel && <span className="zk-action__typ">{typLabel}: </span>}
        {hauptMin} min
      </span>,
    )
  }

  // Vergleichszahl: jeweils andere Quelle, wenn beide da sind.
  if (ml != null && heuristik != null) {
    const andere = haupt === 'ML'
      ? { label: 'Heuristik', wert: Math.round(heuristik / 60) }
      : { label: 'ML', wert: Math.round(ml / 60) }
    teile.push(<span key="v">{andere.label}: {andere.wert} min</span>)
  } else if (heuristik != null && ml == null) {
    const grund = e.ml_status_grund && e.ml_status_grund !== 'ok' ? ` (ML aus: ${e.ml_status_grund})` : ''
    teile.push(<span key="v">kein ML-Vergleich{grund}</span>)
  }

  // Wirkungsrate kompakt.
  const rate = wirkungsrateText(e)
  if (rate) teile.push(<span key="w" title={rate.tooltip}>{rate.kurz}</span>)

  // T-0295: Plateau-Transparenz -- macht die flache Einzeldose ehrlich
  // (erreicht Ziel NICHT in einem Lauf, braucht N Dosen).
  const plateau = plateauText(e)
  if (plateau) {
    teile.push(
      <span key="p" className="zk-action__plateau" title={plateau.tooltip}>
        {plateau.kurz}
      </span>,
    )
  }

  if (teile.length === 0) return null
  return (
    <>
      {teile.map((t, i) => (
        <span key={i}>
          {i > 0 && ' · '}
          {t}
        </span>
      ))}
    </>
  )
}

/** Lange erklarung_kurz nur als Body zeigen, wenn sie mehr Info hat als
 *  die strukturierte detail-Zeile. Faustregel: >60 Zeichen + enthaelt
 *  keinen reinen "X min fuer Y Tage Reserve"-Trivialtext. */
function relevanterBody(e: GiessEmpfehlung | null): string | null {
  if (!e?.erklarung_kurz) return null
  const txt = e.erklarung_kurz.trim()
  if (txt.length < 60) return null
  return txt
}

function bauInhalt(z: ActionZustandV4, zone: Zone, e: GiessEmpfehlung | null): Inhalt {
  const quelle = dauerQuelle(e)
  const detail = dauerDetailZeile(e)
  const body = relevanterBody(e)
  switch (z) {
    case 'kritisch':
      return {
        titel: 'Sofort gießen',
        quelle,
        wann: tagBisReserve(e) ?? (e?.tage_bis_welkepunkt != null
          ? `Welkepunkt in ~${Math.round(e.tage_bis_welkepunkt * 24)}h`
          : undefined),
        // T-0378: der Bypass feuert per Definition unter `feuchte_kritisch`,
        // also genau in diesem Zustand -- hier gehoert der Hinweis hin.
        detail: mitBypassHinweis(
          detail ?? (zone.aktuelle_feuchte != null && zone.feuchte_kritisch != null
            ? `Feuchte ${Math.round(zone.aktuelle_feuchte)}% unter kritisch ${zone.feuchte_kritisch}%`
            : undefined),
          e,
        ),
        body,
        buttons: [],
      }

    // F3b (T-0397): akut NUR aus ML-Prognose, Ist noch im Korridor. Amber-
    // Rahmen (nicht kritisch-rot) + ehrlicher Titel: es ist eine Vorhersage,
    // keine real unterschrittene Schwelle. "Ist ist rot, Warnung gelb" (Andre).
    case 'giessen_akut':
      return {
        titel: 'Jetzt gießen (ML-Prognose)',
        quelle,
        wann: tagBisReserve(e) ?? (e?.tage_bis_welkepunkt != null
          ? `Welkepunkt in ~${Math.round(e.tage_bis_welkepunkt * 24)}h`
          : undefined),
        detail,
        body,
        buttons: [],
      }

    case 'giessen': {
      // Titel ohne Dauer-Zahl -- die kommt strukturiert in der Detail-Zeile.
      const titel = zone.ventil_kanal != null
        ? `Empfehlung Kanal ${zone.ventil_kanal}`
        : 'Empfehlung'
      return {
        titel,
        quelle,
        wann: tagBisReserve(e) ?? (e?.zeitstempel ? datumZeitFormat(e.zeitstempel) : undefined),
        // T-0378: auch hier, falls eine Zone mit hoher kritisch-Schwelle den
        // Bypass ausloest, ohne im 'kritisch'-Kartenzustand zu landen.
        detail: mitBypassHinweis(detail, e),
        body,
        buttons: [],
      }
    }

    case 'blocker': {
      // T-0356: beim Budget-Blocker die Budget-Detailzeile (effektives
      // Budget, Single Source) statt der generischen Dauer-Detailzeile --
      // sonst zeigt die Karte eine Giess-Dauer neben "Tagesbudget voll".
      const budgetDetail = e?.blocker_typ === 'BUDGET_ERSCHOEPFT'
        ? budgetDetailZeile(e)
        : null
      return {
        titel: blockerLabel(e?.blocker_typ ?? null),
        quelle: budgetDetail ? null : quelle,
        wann: tagBisReserve(e) ?? (e?.zeitstempel ? datumZeitFormat(e.zeitstempel) : undefined),
        detail: budgetDetail ?? detail,
        body,
        buttons: [],
      }
    }

    // T-0370: praeventiver Bedarf ohne Aktion (z.B. Monitoring-Modus,
    // kein Blocker gesetzt) -- vorher fiel das auf "Im Korridor" und
    // widersprach dem Tagesplan ("Bedarf, blockiert"). Ehrlich benennen.
    case 'beobachten':
      return {
        titel: zone.modus === 'monitoring'
          ? 'Bedarf erkannt — Monitoring, keine Automatik'
          : 'Bedarf erkannt — beobachten',
        quelle,
        wann: tagBisReserve(e),
        detail,
        body,
        buttons: [],
      }

    // T-0370: Datenbasis unzuverlaessig (Sensor-Ausfall/eingefroren) oder
    // gar kein aktueller Wert. Die Warnzeile daruber traegt das Detail;
    // hier NUR der ehrliche Zustand -- kein "Im Korridor" aus toten Daten.
    // T-0532: Lead-Ausfall ist ein Unterfall von 'sensor', aber ein
    // diagnostizierbarer -- "Sensor unzuverlässig" waere hier zu unscharf
    // (kein Sensor ist defekt, es fehlt nur der massgebliche). Das `detail`
    // benennt die Quelle der angezeigten Zahl, sonst steht der grosse
    // %-Wert der Karte ohne Herkunft da.
    case 'sensor':
      if (leadAusgefallen(zone)) {
        return {
          titel: 'Lead-Sensor ausgefallen — Zustand unbekannt',
          quelle: null,
          wann: undefined,
          detail: leadAusfallDetail(zone),
          body: null,
          buttons: [],
        }
      }
      return {
        titel: zone.aktuelle_feuchte == null
          ? 'Keine aktuellen Daten'
          : 'Sensor unzuverlässig — Zustand unbekannt',
        quelle: null,
        wann: undefined,
        detail: undefined,
        body: null,
        buttons: [],
      }

    // T-0397 (F2): ist > feuchte_schwelle_max. Vorher fiel das auf 'ok'/"Im
    // Korridor" mit gruenem Rahmen -- die Karte log, waehrend der Strip
    // "zu nass" zaehlte.
    case 'nass':
      return {
        titel: 'Zu nass',
        quelle: null,
        wann: undefined,
        detail: zone.aktuelle_feuchte != null && zone.feuchte_schwelle_max != null
          ? `Feuchte ${Math.round(zone.aktuelle_feuchte)}% über Maximum ${zone.feuchte_schwelle_max}%`
          : undefined,
        body: null,
        buttons: [],
      }

    case 'ok':
    default:
      // T-0201-Folge: bei OK keine Detail-Zeile -- "0 min" wirkt sinnlos.
      return {
        titel: 'Im Korridor',
        quelle: null,
        wann: tagBisReserve(e),
        detail: undefined,
        body,
        buttons: [],
      }
  }
}

/** Kompaktes Reserve-Label: bei <1 Tag -> Stunden, sonst Tage. Nutzt
 *  `tage_bis_reserve_grenze` (strategie-spezifische Reserve, T-0105) und
 *  faellt auf `tage_bis_welkepunkt` zurueck. */
function tagBisReserve(e: GiessEmpfehlung | null): string | undefined {
  if (!e) return undefined
  const tage = e.tage_bis_reserve_grenze ?? e.tage_bis_welkepunkt
  if (tage == null) return undefined
  const label = e.reserve_grenze_label ?? 'Welkepunkt'
  if (tage < 1) return `${label} in ~${Math.round(tage * 24)}h`
  if (tage < 10) return `${label} in ~${tage.toFixed(1)} Tagen`
  return `${label} in ~${Math.round(tage)} Tagen`
}

function blockerLabel(typ: GiessEmpfehlung['blocker_typ']): string {
  switch (typ) {
    case 'REGEN_ERWARTET':    return 'Regen erwartet — keine Bewässerung'
    case 'PAUSE_AKTIV':       return 'Pause aktiv'
    // T-0356: ruhiges Wording. Das Tagesbudget ist eine Runaway-Notbremse,
    // kein Notfall -- "erschoepft" alarmierte, obwohl die Pflanze bestens
    // versorgt sein kann. Bei echtem Trockenstress giesst das Backend via
    // Notreserve weiter (effektives Budget), darum der beruhigende Zusatz.
    case 'BUDGET_ERSCHOEPFT': return 'Tagesbudget voll'
    case 'ZEITFENSTER':       return 'Außerhalb Bewässerungs-Fenster'
    case 'KEINE_MESSUNG':     return 'Keine Messung'
    default:                  return 'Im Korridor'
  }
}

/** T-0356: Tagesbudget-Detailzeile fuer den Budget-Blocker -- spiegelt das
 *  EFFEKTIVE Budget aus dem Backend (Single Source, inkl. Notreserve-
 *  Anhebung), nicht selbst nachgerechnet. Zeigt "X/Y min" in Minuten
 *  (Backend liefert Sekunden) plus den Notreserve-Hinweis, wenn das Backend
 *  wegen kritischer Trockenheit bereits angehoben hat. Faellt auf null
 *  zurueck, solange ein aelteres Backend die Felder nicht liefert -- dann
 *  bleibt es beim ruhigen Label ohne Zahlen. */
function budgetDetailZeile(e: GiessEmpfehlung | null): ReactNode {
  // T-0356: "Notreserve aktiv" NUR im Bedarfsfall -- nur wenn das Backend wegen
  // kritischer Trockenheit die Reserve wirklich angehoben hat. Sonst nichts
  // (das ruhige Label "Tagesbudget voll" reicht; keine X/Y-Zahlen gewuenscht).
  if (!e || !e.budget_notreserve_aktiv) return null
  return 'Notreserve aktiv (wg. Trockenstress angehoben)'
}

/** T-0378: Hinweis, dass die Mindest-Pause wegen kritischer Trockenheit
 *  uebersprungen wurde. Gegenstueck zur Notreserve-Zeile oben -- beides sind
 *  gedeckelte Ausnahmen vom Normalbetrieb, und beide muessen sichtbar sein.
 *  Ohne diesen Hinweis giesst die Engine, waehrend die Karte nichts
 *  Besonderes zeigt -- der Nutzer kann den Lauf dann nicht einordnen. */
function pauseBypassZeile(e: GiessEmpfehlung | null): ReactNode {
  if (!e?.pause_bypass_kritisch_aktiv) return null
  return 'Mindest-Pause übersprungen (kritische Trockenheit)'
}

/** Detail-Zeile plus optionalen Bypass-Hinweis darunter. Der Hinweis haengt
 *  sich an, statt die Dauer-Info zu verdraengen -- die Dosis bleibt die
 *  wichtigere Information, der Bypass erklaert nur, warum jetzt. */
function mitBypassHinweis(basis: ReactNode, e: GiessEmpfehlung | null): ReactNode {
  const hinweis = pauseBypassZeile(e)
  if (!hinweis) return basis
  if (!basis) return hinweis
  return (
    <>
      {basis}
      <br />
      {hinweis}
    </>
  )
}
