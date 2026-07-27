/* Grosser Feuchtewert mit Farbcodierung nach Zonenstatus. */

interface Props {
  wert: number | null
  min: number
  max: number
}

export function FeuchteAnzeige({ wert, min, max }: Props) {
  if (wert === null) return <span className="wert-gross">--</span>

  let farbe = 'var(--farbe-gesund)'
  if (wert < min) farbe = 'var(--farbe-gefahr)'
  else if (wert > max) farbe = 'var(--farbe-info)'

  return <span className="wert-gross" style={{ color: farbe }}>{wert.toFixed(0)}%</span>
}
