/* T-0610 FE-13: Fokusfalle des Detail-Drawers als reine Funktion.
 *
 * Der Drawer ist ein modaler Dialog (aria-modal). Tab und Shift+Tab duerfen
 * ihn nicht verlassen: vom letzten fokussierbaren Element geht es zum ersten,
 * vom ersten rueckwaerts zum letzten. Steht der Fokus auf keinem der
 * Elemente (Titel mit tabIndex -1 nach dem Oeffnen, oder ausserhalb), springt
 * Tab auf das erste, Shift+Tab auf das letzte.
 *
 * Rueckgabe: Index des Elements, das fokussiert werden soll, -1 = Fokus auf
 * dem Dialog lassen (nichts Fokussierbares darin), null = Browser macht den
 * normalen Schritt (Fokus bleibt ohnehin innerhalb).
 */
export const DRAWER_FOKUSSIERBAR =
  'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), '
  + 'textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'

export function fokusfalleZiel(
  anzahl: number,
  aktuellerIndex: number,
  rueckwaerts: boolean,
): number | null {
  if (anzahl <= 0) return -1
  if (aktuellerIndex < 0 || aktuellerIndex >= anzahl) {
    return rueckwaerts ? anzahl - 1 : 0
  }
  if (rueckwaerts && aktuellerIndex === 0) return anzahl - 1
  if (!rueckwaerts && aktuellerIndex === anzahl - 1) return 0
  return null
}
