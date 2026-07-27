/* SchwellenRange — konsolidiert 2026-04-23.
 *
 * Philosophie: das Pflanzen-Optimum ist die **zentrale Referenz** (botanisch
 * fundiert, bei FYTA aus deren Pflanzen-DB, bei Gardena aus Claude+ChatGPT-
 * Doppelrecherche). Die operative "Meine Schwelle" aus `config/default.yaml`
 * ist der Trigger-Puffer, der i.d.R. leicht verschoben vom Optimum liegt —
 * sie wird nur als Overlay gezeigt, wenn sie signifikant vom Optimum abweicht.
 *
 * Historie: vorher drei gleichberechtigte Schichten
 *   (User-Schwelle / Pflanzen-Optimum / Daten-Vorschlag) + 3-Zeilen-Legende.
 * Problem 1: visuelle Konkurrenz gleichrangiger Zeilen, obwohl inhaltlich
 *   ungleich — Daten-Vorschlag ist rein deskriptiv (Perzentil aus 30-Tage-
 *   Historie, **kein** Gesundheits-Check).
 * Problem 2: "Meine Schwelle" wurde optisch zur Hauptaussage, obwohl sie
 *   eigentlich nur ein operativer Puffer um das Pflanzen-Optimum ist.
 * Problem 3: Karten wurden hoeher als der Viewport.
 *
 * Jetzt:
 *  - Haupt-Bar = Pflanzen-Optimum (gefuellt). Falls kein Optimum vorhanden
 *    → Fallback auf Meine Schwelle als Haupt-Bar.
 *  - Overlay = Meine Schwelle als gestrichelter Rahmen, NUR wenn sie
 *    sichtbar vom Optimum abweicht (>= 2 pp).
 *  - Daten-Vorschlag ist weg aus der Visualisierung; dient nur als
 *    Fallback-Referenz fuer Sanity-Hinweis, wenn kein Optimum vorliegt.
 *  - Sanity-Hint NUR wenn meldenswert:
 *      (a) Schwelle > 8 pp vom Optimum abweichend → Hinweis-gelb.
 *      (b) Schwelle komplett ausserhalb Optimum → Warnung-rot.
 *
 * Noch offen (separate Task): **zeitvariable Feuchte-Regimes** (Magerwiese
 * braucht Trockenphasen etc.). Der heutige Ansatz ist konstantes Band —
 * realistisch bei Waldstauden/Bambus, nicht bei trockenstolerant-kompetitiven
 * Pflanzen. Siehe TASK.md T-0074.
 */

import './SchwellenRange.css'

export interface Props {
  ist: number | null
  schwelleMin: number
  schwelleMax: number
  optimumMin?: number | null
  optimumMax?: number | null
  vorschlagMin?: number | null
  vorschlagMax?: number | null
  onVorschlagUebernehmen?: () => void
}

function clampPct(wert: number): number {
  if (Number.isNaN(wert)) return 0
  if (wert < 0) return 0
  if (wert > 100) return 100
  return wert
}

/** Entfernt Ticks die naeher als 3 pp beieinander liegen — haelt Achse lesbar. */
function verdichteTicks(werte: number[]): number[] {
  const sortiert = [...new Set(werte.map(w => Math.round(w)))].sort((a, b) => a - b)
  const ergebnis: number[] = []
  for (const w of sortiert) {
    if (ergebnis.length === 0 || w - ergebnis[ergebnis.length - 1] >= 3) {
      ergebnis.push(w)
    }
  }
  return ergebnis
}

interface SanityHinweis {
  schweregrad: 'warnung' | 'hinweis'
  text: string
}

/** Sanity-Check: nur melden, wenn die aktive Schwelle sich signifikant
 *  von der verfuegbaren Referenz unterscheidet. */
function baueSanity(
  schwelleMin: number,
  schwelleMax: number,
  optimumMin: number | null | undefined,
  optimumMax: number | null | undefined,
  vorschlagMin: number | null | undefined,
  vorschlagMax: number | null | undefined,
): SanityHinweis | null {
  const hatOptimum = optimumMin != null && optimumMax != null
  const hatVorschlag = vorschlagMin != null && vorschlagMax != null

  if (hatOptimum) {
    const oMin = optimumMin as number
    const oMax = optimumMax as number
    if (schwelleMin > oMax) {
      return {
        schweregrad: 'warnung',
        text: `Deine Schwelle ${schwelleMin}–${schwelleMax}% liegt ueber dem Pflanzen-Optimum ${oMin}–${oMax}%. Pflanze wird nasser gehalten, als die Literatur empfiehlt.`,
      }
    }
    if (schwelleMax < oMin) {
      return {
        schweregrad: 'warnung',
        text: `Deine Schwelle ${schwelleMin}–${schwelleMax}% liegt unter dem Pflanzen-Optimum ${oMin}–${oMax}%. Pflanze wird trockener gehalten, als die Literatur empfiehlt.`,
      }
    }
    const diffMin = schwelleMin - oMin
    const diffMax = schwelleMax - oMax
    if (Math.abs(diffMin) > 8 || Math.abs(diffMax) > 8) {
      const teile: string[] = []
      if (Math.abs(diffMin) > 8) {
        teile.push(`min ${diffMin > 0 ? '+' : ''}${diffMin} pp`)
      }
      if (Math.abs(diffMax) > 8) {
        teile.push(`max ${diffMax > 0 ? '+' : ''}${diffMax} pp`)
      }
      return {
        schweregrad: 'hinweis',
        text: `Deine Schwelle weicht deutlich vom Pflanzen-Optimum ab (${teile.join(', ')}). Absichtlicher Puffer?`,
      }
    }
    return null
  }

  // Fallback: kein Pflanzen-Optimum → Empirie als grobe Referenz.
  if (hatVorschlag) {
    const vMin = vorschlagMin as number
    const vMax = vorschlagMax as number
    const diffMin = schwelleMin - vMin
    const diffMax = schwelleMax - vMax
    if (Math.abs(diffMin) > 8 || Math.abs(diffMax) > 8) {
      return {
        schweregrad: 'hinweis',
        text: `Kein Pflanzen-Optimum hinterlegt. Feuchte bewegte sich in den letzten 30 Tagen meist bei ${vMin}–${vMax}% — Schwelle liegt deutlich daneben.`,
      }
    }
  }
  return null
}

export function SchwellenRange({
  ist,
  schwelleMin,
  schwelleMax,
  optimumMin,
  optimumMax,
  vorschlagMin,
  vorschlagMax,
}: Props) {
  const hatOptimum = optimumMin != null && optimumMax != null

  // Schwellen-Overlay sichtbar, wenn Optimum vorhanden UND die Schwelle
  // um >= 2 pp abweicht. Deckungsgleiche Bereiche waeren nur visuelles
  // Rauschen. Ohne Optimum rendern wir die Schwelle direkt als Haupt-Bar.
  const schwelleAbweicht = hatOptimum && (
    Math.abs(schwelleMin - (optimumMin as number)) >= 2
    || Math.abs(schwelleMax - (optimumMax as number)) >= 2
  )

  // Ticks: nur Werte, die auch visualisiert sind.
  const tickWerte: number[] = [0, 100]
  if (hatOptimum) {
    tickWerte.push(optimumMin as number, optimumMax as number)
  } else {
    tickWerte.push(schwelleMin, schwelleMax)
  }
  if (schwelleAbweicht) {
    tickWerte.push(schwelleMin, schwelleMax)
  }
  if (ist != null) tickWerte.push(ist)
  const ticks = verdichteTicks(tickWerte)

  const sanity = baueSanity(
    schwelleMin, schwelleMax, optimumMin, optimumMax,
    vorschlagMin, vorschlagMax,
  )

  return (
    <div className="schwellen-range">
      <div className="sr-track-wrap">
        <div className="sr-track" aria-hidden>
          {/* Haupt-Schicht: Pflanzen-Optimum wenn vorhanden, sonst Schwelle. */}
          {hatOptimum ? (
            <div
              className="sr-schicht sr-optimum-fill"
              style={{
                left: `${clampPct(optimumMin as number)}%`,
                width: `${clampPct((optimumMax as number) - (optimumMin as number))}%`,
              }}
            />
          ) : (
            <div
              className="sr-schicht sr-user-fill"
              style={{
                left: `${clampPct(schwelleMin)}%`,
                width: `${clampPct(schwelleMax - schwelleMin)}%`,
              }}
            />
          )}
          {/* Overlay: Meine Schwelle nur wenn signifikant abweichend. */}
          {schwelleAbweicht && (
            <div
              className="sr-schicht sr-user-outline"
              style={{
                left: `${clampPct(schwelleMin)}%`,
                width: `${clampPct(schwelleMax - schwelleMin)}%`,
              }}
            />
          )}
          {/* Ist-Marker */}
          {ist != null && (
            <div
              className="sr-ist"
              style={{ left: `${clampPct(ist)}%` }}
              role="img"
              aria-label={`Aktuelle Feuchte ${ist.toFixed(0)} %`}
            >
              <div className="sr-ist-linie" />
              <div className="sr-ist-kreis" />
            </div>
          )}
        </div>
        <div className="sr-achse" aria-hidden>
          {ticks.map(t => (
            <span
              key={t}
              className="sr-tick"
              style={{ left: `${clampPct(t)}%` }}
            >
              {t}
            </span>
          ))}
        </div>
      </div>

      <div className="sr-legende">
        {hatOptimum ? (
          <div className="sr-kachel sr-kachel-optimum">
            <span className="sr-swatch sr-swatch-optimum-fill" aria-hidden />
            <span className="sr-kachel-label">Pflanzen-Optimum</span>
            <span className="sr-kachel-wert">{optimumMin}–{optimumMax} %</span>
          </div>
        ) : (
          <div className="sr-kachel sr-kachel-user">
            <span className="sr-swatch sr-swatch-user-fill" aria-hidden />
            <span className="sr-kachel-label">Meine Schwelle</span>
            <span className="sr-kachel-wert">{schwelleMin}–{schwelleMax} %</span>
          </div>
        )}
        {schwelleAbweicht && (
          <div className="sr-kachel sr-kachel-user-outline">
            <span className="sr-swatch sr-swatch-user-outline" aria-hidden />
            <span className="sr-kachel-label">Meine Schwelle</span>
            <span className="sr-kachel-wert">{schwelleMin}–{schwelleMax} %</span>
          </div>
        )}
      </div>

      {sanity && (
        <div className={`sr-sanity sr-sanity-${sanity.schweregrad}`}>
          <span className="sr-sanity-icon" aria-hidden>
            {sanity.schweregrad === 'warnung' ? '!' : 'i'}
          </span>
          <span className="sr-sanity-text">{sanity.text}</span>
        </div>
      )}
    </div>
  )
}
