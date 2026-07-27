/* FytaKpiBlock — T-0196d/e/g.
 *
 * Kompakter KPI-Block, der pro FYTA-Achse den aktuellen Sensor-Wert mit
 * dem FYTA-Optimum-Bereich vergleicht und bewertet ("im Optimum" /
 * "über Optimum" / "zu hoch" etc.). Wird sowohl im Karten-Chart
 * (`ZonenKarteNeu`) als auch im Detail-Drawer (`DetailsDrawer`)
 * verwendet.
 *
 * Werte-Quelle: `zone.optima[achse].current` (Backend-Tabelle
 * `plant_optimum_achse.current`, gefuellt vom PlantOptimumJob). FYTA-
 * Plant-Detail-API liefert current fuer Feuchte/Licht-PPFD/Temperatur/
 * Salinity; DLI wird im Backend aus PPFD-Stundenwerten aggregiert.
 *
 * Zeigt nur Achsen, fuer die `current` UND `min_good`/`max_good`
 * gesetzt sind (sonst ist die Anzeige sinnlos).
 */

import type { PlantOptimumAchse } from '../typen'

interface AchsenDefinition {
  /** Interner Schluessel in `zone.optima` (Backend: plant_optimum_achse.achse). */
  schluessel: 'feuchte' | 'licht_ppfd' | 'licht_dli' | 'temperatur' | 'salinitaet'
  /** UI-Label fuer die Zeile. */
  label: string
  /** Anzeige-Einheit (kann von `opt.einheit` abweichen wenn FYTA
   *  irreführende Strings liefert wie '°C/h' statt '°C'). */
  einheitAnzeige: string
  /** Nachkommastellen fuer die Wert-Anzeige. */
  stellen: number
}

const ACHSEN: AchsenDefinition[] = [
  { schluessel: 'feuchte',     label: 'Feuchte',     einheitAnzeige: '%',       stellen: 0 },
  { schluessel: 'licht_ppfd',  label: 'Licht',       einheitAnzeige: 'μmol/m²s', stellen: 0 },
  { schluessel: 'licht_dli',   label: 'Licht/Tag',   einheitAnzeige: 'mol/Tag', stellen: 1 },
  { schluessel: 'temperatur',  label: 'Temperatur',  einheitAnzeige: '°C',      stellen: 1 },
  { schluessel: 'salinitaet',  label: 'Nährsalz',    einheitAnzeige: 'mS/cm',   stellen: 2 },
]

/** Bewertet einen Sensor-Wert gegen das FYTA-Optimum.
 *  Wert > max_akzeptabel oder < min_akzeptabel: rot ("zu hoch/niedrig").
 *  Zwischen akzeptabel und good: gelb ("über/unter Optimum").
 *  Im good-Bereich: gruen ("im Optimum"). */
function bewerte(
  wert: number, opt: PlantOptimumAchse,
): { text: string; farbe: string } {
  const { min_good, max_good, min_akzeptabel, max_akzeptabel } = opt
  if (max_akzeptabel != null && wert > max_akzeptabel) {
    return { text: 'zu hoch', farbe: 'var(--farbe-gefahr)' }
  }
  if (min_akzeptabel != null && wert < min_akzeptabel) {
    return { text: 'zu niedrig', farbe: 'var(--farbe-gefahr)' }
  }
  if (max_good != null && wert > max_good) {
    return { text: 'über Optimum', farbe: 'var(--farbe-warnung-text)' }
  }
  if (min_good != null && wert < min_good) {
    return { text: 'unter Optimum', farbe: 'var(--farbe-warnung-text)' }
  }
  return { text: 'im Optimum', farbe: 'var(--farbe-gesund)' }
}

interface Props {
  /** Optima-Dict aus `Zone.optima`, Schluessel sind Achsen-Identifier. */
  optima: Record<string, PlantOptimumAchse> | undefined
  /** Optionaler Block-Titel (Default: "FYTA-Pflanzen-Status"). */
  titel?: string
  /** Optional: kompaktere Variante ohne Titel (z.B. fuer Drawer-Sicht). */
  kompakt?: boolean
}

export function FytaKpiBlock({ optima, titel = 'FYTA-Pflanzen-Status', kompakt = false }: Props) {
  if (!optima) return null
  const zeilen = ACHSEN
    .map(def => {
      const opt = optima[def.schluessel]
      if (!opt) return null
      if (opt.current == null) return null
      if (opt.min_good == null || opt.max_good == null) return null
      return { def, opt, wert: opt.current }
    })
    .filter((x): x is { def: AchsenDefinition; opt: PlantOptimumAchse; wert: number } => x !== null)

  if (zeilen.length === 0) return null

  return (
    <div className="fyta-status">
      {!kompakt && <div className="fyta-status-titel">{titel}</div>}
      {zeilen.map(({ def, opt, wert }) => {
        const b = bewerte(wert, opt)
        const fmt = (n: number) => n.toFixed(def.stellen)
        return (
          <div className="fyta-status-zeile" key={def.schluessel}>
            <span className="fyta-status-label">{def.label}</span>
            <span className="fyta-status-wert">{fmt(wert)} {def.einheitAnzeige}</span>
            {opt.min_good != null && opt.max_good != null && (
              <span className="fyta-status-opt">
                Optimum {fmt(opt.min_good)}–{fmt(opt.max_good)}
              </span>
            )}
            <span className="fyta-status-bewertung" style={{ color: b.farbe }}>
              {b.text}
            </span>
          </div>
        )
      })}
    </div>
  )
}
