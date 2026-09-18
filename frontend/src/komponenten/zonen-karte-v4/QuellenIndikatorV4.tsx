/* =========================================================
   QuellenIndikatorV4 -- Paket 2 aus zonen-karte-v4-prompt.md (T-0320 S8).
   Sensor-Pills OHNE "+n"-Verstecken. Genau drei erlaubte Modi:

     'alle'   -- jede Sensor-Pill einzeln + gleichberechtigt (Default;
                 passt bei <=3 Sensoren mit normalen Namen i.d.R. in die
                 Zeile). Zeigt Klartextnamen wie v3, nur ohne "+n".
     'quelle' -- nach Quelle aggregiert: "Gardena 68 · ⌀ FYTA 65 (n=2)".
                 Genutzt, wenn die Einzel-Pills die Zeile sprengen wuerden.
     'keine'  -- gar keine Pills (nur der grosse %-Median im Header),
                 Sicherheits-Fallback wenn selbst (2) zu breit waere.

   Verboten: willkuerliche Teilmenge ("+1") -- entweder alle gleich, oder
   sauber nach Quelle aggregiert, oder keine. Der grosse %-Wert bleibt der
   Median ueber ALLE Sensoren (T-0179c); der Aggregat-Tooltip nennt n.
   v3-QuellenIndikator bleibt unveraendert.
   ========================================================= */

import type { SensorEinzeln } from '../../typen'

type QuellenTyp = 'g' | 'f'
interface Echt { s: SensorEinzeln; typ: QuellenTyp }

interface Props {
  sensoren?: SensorEinzeln[]
}

// Zeichen-Budget fuer die Einzel-Pill-Reihe (Modus 'alle'). Statt echter
// DOM-Messung eine Heuristik: passt die Namens-Reihe neben dem Modus-Badge
// in eine ~320px-Karte? Tunable -- favorisiert bewusst 'alle' (bevorzugt).
const BUDGET_ALLE = 46

export function QuellenIndikatorV4({ sensoren = [] }: Props) {
  const echte: Echt[] = sensoren
    .map(s => ({ s, typ: mapQuelle(s.quelle) }))
    .filter((x): x is Echt => x.typ !== null)
  if (echte.length === 0) return null

  const modus = waehleModus(echte)
  if (modus === 'keine') return null

  if (modus === 'alle') {
    return (
      <span className="sensor-pills" aria-label="Sensoren">
        {echte.map(({ s, typ }, i) => {
          // T-0209-Anti-Pattern: nie eine UUID zeigen -- lieber "Sensor N".
          const name = s.name?.trim() || `Sensor ${i + 1}`
          return (
            <span
              key={s.geraet_id}
              className={`sensor-pill sensor-pill--${typ}`}
              title={pillTooltip(typ, name)}
            >
              {kurzName(name)}
            </span>
          )
        })}
      </span>
    )
  }

  // modus === 'quelle': je Quelle eine aggregierte Pill (Median-Wert + n).
  return (
    <span className="sensor-pills" aria-label="Sensoren (nach Quelle aggregiert)">
      {aggregiereNachQuelle(echte).map(p => (
        <span key={p.typ} className={`sensor-pill sensor-pill--${p.typ}`} title={p.tooltip}>
          {p.text}
        </span>
      ))}
    </span>
  )
}

interface AggPill { typ: QuellenTyp; text: string; tooltip: string }

/** Aggregiert die Sensoren je Quelle zu max. zwei Pills (Gardena, FYTA) mit
 *  Median-Wert + Anzahl. Reihenfolge Gardena -> FYTA. */
function aggregiereNachQuelle(echte: Echt[]): AggPill[] {
  const out: AggPill[] = []
  const reihenfolge: { typ: QuellenTyp; label: string }[] = [
    { typ: 'g', label: 'Gardena' },
    { typ: 'f', label: 'FYTA' },
  ]
  for (const { typ, label } of reihenfolge) {
    const grp = echte.filter(x => x.typ === typ)
    if (grp.length === 0) continue
    const werte = grp.map(x => x.s.boden_feuchte).filter((v): v is number => v != null)
    const med = werte.length ? Math.round(median(werte)) : null
    const n = grp.length
    let text: string
    if (n === 1) text = med != null ? `${label} ${med}` : label
    else text = med != null ? `⌀ ${label} ${med} (n=${n})` : `${label} (n=${n})`
    out.push({
      typ,
      text,
      tooltip: `${n} ${label}-Sensor${n === 1 ? '' : 'en'}${med != null ? `, Median ${med}%` : ''}. Einzelwerte im Drawer.`,
    })
  }
  return out
}

/** Waehlt 'alle' / 'quelle' / 'keine' nach Pill-Anzahl + Namenlaenge (nicht
 *  nach fixem n -- so der Prompt). */
function waehleModus(echte: Echt[]): 'alle' | 'quelle' | 'keine' {
  if (echte.length === 1) return 'alle'
  const namensKosten = echte.reduce((sum, { s }, i) => {
    const name = s.name?.trim() || `Sensor ${i + 1}`
    return sum + kurzName(name).length + 4   // +4 ~ Padding/Border/Gap je Pill
  }, 0)
  if (echte.length <= 3 && namensKosten <= BUDGET_ALLE) return 'alle'
  // Sonst nach Quelle aggregieren (max 2 kurze Pills).
  const aggrKosten = aggregiereNachQuelle(echte).reduce((sum, p) => sum + p.text.length + 4, 0)
  return aggrKosten <= BUDGET_ALLE ? 'quelle' : 'keine'
}

function median(xs: number[]): number {
  const a = [...xs].sort((x, y) => x - y)
  const m = Math.floor(a.length / 2)
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2
}

/** Entfernt den Hardware-Klammer-Zusatz ("Waldblumen A (FYTA Terra 11 cm)"
 *  -> "Waldblumen A"); voller Name bleibt im Tooltip. Wie v3. */
function kurzName(name: string): string {
  const i = name.indexOf(' (')
  return i > 0 ? name.slice(0, i) : name
}

function pillTooltip(typ: QuellenTyp, name: string): string {
  const quelle = typ === 'g'
    ? 'Gardena-Bodensensor (Feuchte + Temperatur)'
    : 'FYTA-Pflanzensensor (Feuchte, Licht, Temperatur, Naehrsalz)'
  return `${name} -- ${quelle}.`
}

function mapQuelle(q: string | null): QuellenTyp | null {
  if (!q) return null
  const lc = q.toLowerCase()
  if (lc.includes('gardena')) return 'g'
  if (lc.includes('fyta')) return 'f'
  return null
}
