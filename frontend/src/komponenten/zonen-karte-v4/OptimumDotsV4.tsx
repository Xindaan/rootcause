/* =========================================================
   OptimumDots -- Zonen-Karte v2
   Vier-Achsen-Glance-Indikator:
     F = Feuchte
     L = Licht (PPFD)
     T = Temperatur
     N = Naehrsalz (Salinitaet)

   FYTA-Zonen liefern alle 4 Achsen ueber zone.optima.* (mit current +
   min_good + max_good). Gardena-Zonen haben nur Feuchte; statt die Pill
   wegzulassen, wird sie aus den User-Schwellen (feuchte_schwelle_min/max
   + feuchte_kritisch) plus aktuelle_feuchte gerendert -- so haben FYTA-
   und Gardena-Karten ein konsistentes Layout (Gardena = nur F, FYTA =
   F + L + T + N).
   ========================================================= */

import type { Zone, PlantOptimumAchse } from '../../typen'
import { bewerteOptimum } from '../../hilfsfunktionen'
import { datenUnsicher } from './aktion-state-v4'

interface Props {
  zone: Zone
}

interface AchseDef {
  key: 'feuchte' | 'licht_ppfd' | 'temperatur' | 'salinitaet'
  label: 'F' | 'L' | 'T' | 'N'
  tooltip: string
}

const ACHSEN: AchseDef[] = [
  { key: 'feuchte',    label: 'F', tooltip: 'Feuchte' },
  { key: 'licht_ppfd', label: 'L', tooltip: 'Licht (PPFD)' },
  { key: 'temperatur', label: 'T', tooltip: 'Temperatur' },
  { key: 'salinitaet', label: 'N', tooltip: 'Naehrsalz' },
]

interface Pill {
  def: AchseDef
  klasse: 'ok' | 'warn' | 'gefahr'
  tooltip: string
}

/** Synthetische Feuchte-Bewertung fuer Gardena-Zonen (ohne FYTA-Optimum). */
function feuchtePillAusSchwellen(zone: Zone): Pill | null {
  const ist = zone.aktuelle_feuchte
  if (ist == null) return null
  const min = zone.feuchte_schwelle_min
  const max = zone.feuchte_schwelle_max
  const kritisch = zone.feuchte_kritisch ?? null
  let klasse: 'ok' | 'warn' | 'gefahr' = 'ok'
  let text = 'im Korridor'
  if (kritisch != null && ist < kritisch) {
    klasse = 'gefahr'; text = `kritisch (< ${kritisch}%)`
  } else if (ist < min) {
    klasse = 'warn'; text = `unter Korridor (< ${min}%)`
  } else if (max != null && ist > max) {
    klasse = 'warn'; text = `ueber Korridor (> ${max}%)`
  } else {
    text = max != null ? `im Korridor (${min}-${max}%)` : `ueber ${min}%`
  }
  return {
    def: ACHSEN[0],
    klasse,
    tooltip: `Feuchte: ${Math.round(ist)}% -- ${text} (User-Schwellen).`,
  }
}

/** Synthetische Boden-Temperatur-Bewertung mit groben Default-Schwellen
 *  fuer Garten-Pflanzen: <5°C = Wurzel-Aktivitaet reduziert, >30°C =
 *  Verdunstungs-Stress. Der Tooltip macht klar, dass es Default-Werte
 *  sind und kein zonenspezifisches FYTA-Optimum. */
function temperaturPillAusSchwellen(zone: Zone): Pill | null {
  const t = zone.boden_temperatur
  if (t == null) return null
  let klasse: 'ok' | 'warn' | 'gefahr' = 'ok'
  let bewertung = 'im typischen Bereich'
  if (t < 5) {
    klasse = 'warn'; bewertung = 'kalt (Wurzel-Aktivitaet reduziert)'
  } else if (t > 30) {
    klasse = 'warn'; bewertung = 'heiss (Verdunstungs-Stress)'
  } else if (t > 25) {
    bewertung = 'warm, noch im typischen Bereich'
  }
  return {
    def: ACHSEN[2], // T
    klasse,
    tooltip: `Boden-Temperatur: ${t.toFixed(1)}°C -- ${bewertung} (Default-Schwellen 5-30°C, kein FYTA-Optimum).`,
  }
}

function fytaPill(def: AchseDef, opt: PlantOptimumAchse): Pill | null {
  if (opt.current == null) return null
  if (opt.min_good == null || opt.max_good == null) return null
  const b = bewerteOptimum(opt.current, opt)
  return {
    def,
    klasse: b.klasse,
    tooltip: `${def.tooltip}: ${opt.current.toFixed(1)} (${b.text}, FYTA-Optimum ${opt.min_good}-${opt.max_good}).`,
  }
}

export function OptimumDotsV4({ zone }: Props) {
  if (!zone) return null

  const pills: Pill[] = []

  // Feuchte: FYTA-Optimum hat Vorrang, sonst Fallback auf User-Schwellen.
  const fytaFeuchte = zone.optima?.feuchte
  const fytaFeuchtePill = fytaFeuchte ? fytaPill(ACHSEN[0], fytaFeuchte) : null
  if (fytaFeuchtePill) {
    pills.push(fytaFeuchtePill)
  } else {
    const fallback = feuchtePillAusSchwellen(zone)
    if (fallback) pills.push(fallback)
  }

  // Licht (L): nur wenn FYTA-Optimum-Daten existieren.
  const fytaLicht = zone.optima?.licht_ppfd
  if (fytaLicht) {
    const pill = fytaPill(ACHSEN[1], fytaLicht)
    if (pill) pills.push(pill)
  }

  // Temperatur (T): FYTA-Optimum hat Vorrang, sonst Fallback auf
  // boden_temperatur + Default-Schwellen (auch fuer Gardena-Zonen).
  const fytaTemp = zone.optima?.temperatur
  const fytaTempPill = fytaTemp ? fytaPill(ACHSEN[2], fytaTemp) : null
  if (fytaTempPill) {
    pills.push(fytaTempPill)
  } else {
    const fallback = temperaturPillAusSchwellen(zone)
    if (fallback) pills.push(fallback)
  }

  // Naehrsalz (N): nur wenn FYTA-Optimum-Daten existieren.
  const fytaSalz = zone.optima?.salinitaet
  if (fytaSalz) {
    const pill = fytaPill(ACHSEN[3], fytaSalz)
    if (pill) pills.push(pill)
  }

  if (pills.length === 0) return null

  // T-0397 (F9): Bei unzuverlaessigen Daten (Sensor-Ausfall/eingefroren/kein
  // Update/0.0-Defekt) sind die Achsen-Werte stale -- gruene Dots wuerden
  // "aktuell ok" vortaeuschen, obwohl seit Stunden nichts gemessen wurde.
  // Dann alle Achsen neutral-grau mit "?" + Klartext-Tooltip (kein "FYTA ok").
  // Single Source fuer "unzuverlaessig": datenUnsicher (== getActionStateV4 'sensor').
  if (datenUnsicher(zone)) {
    return (
      <div className="opt-dots" role="list" aria-label="Achsen-Status (Daten unsicher)">
        {pills.map(p => (
          <span
            key={p.def.key}
            role="listitem"
            className="opt-dots__od opt-dots__od--unsicher"
            title={`${p.def.tooltip}: Daten unzuverlaessig (Sensor-Ausfall/kein aktuelles Update) -- letzter Wert nicht aussagekraeftig.`}
          >
            <span className="opt-dots__punkt" />
            {p.def.label}?
          </span>
        ))}
      </div>
    )
  }

  // Obs04 (T-0320): sind ALLE Achsen gruen, sagt ein einzelner Punkt
  // "alles ok" dasselbe wie vier gruene Dots -- nur leiser. Volle Dots
  // erscheinen wieder, sobald eine Achse nicht ok ist (dann sind sie
  // relevant). So werden ruhige Zonen ruhig und kritische treten hervor.
  const alleOk = pills.every(p => p.klasse === 'ok')
  if (alleOk) {
    return (
      <div
        className="opt-dots zk4-allok"
        role="status"
        title={`Alle ${pills.length} Achsen im Optimum (${pills.map(p => p.def.label).join(' ')}). Volle Achsen-Dots erscheinen, sobald eine Achse aus dem Bereich faellt.`}
      >
        <span className="opt-dots__punkt" />
        FYTA ok
      </div>
    )
  }

  return (
    <div className="opt-dots" role="list" aria-label="Achsen-Status">
      {pills.map(p => (
        <span
          key={p.def.key}
          role="listitem"
          className={`opt-dots__od opt-dots__od--${p.klasse}`}
          title={p.tooltip}
        >
          <span className="opt-dots__punkt" />
          {p.def.label}
        </span>
      ))}
    </div>
  )
}
