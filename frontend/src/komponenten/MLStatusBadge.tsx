/* ML-Verfuegbarkeitsbadge fuer den Header. */

import './MLStatusBadge.css'

interface Props {
  verfuegbar: boolean
}

export function MLStatusBadge({ verfuegbar }: Props) {
  return (
    <span className={`ml-badge ${verfuegbar ? 'aktiv' : 'inaktiv'}`}>
      <span className="ml-punkt" />
      {verfuegbar ? 'ML aktiv' : 'ML \u2014'}
    </span>
  )
}
