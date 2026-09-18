/* Zustand-Logik und Aktions-Typen fuer Zonen-Karte v2.
   Eigene Datei, damit Komponenten-Dateien nur Components exportieren
   (react-refresh / Vite HMR Anforderung). */

import type { Zone, GiessEmpfehlung } from '../../typen'

export type Aktion =
  | { typ: 'manuell' }
  | { typ: 'bestaetigen' }
  | { typ: 'verschieben' }
  | { typ: 'snooze' }
  | { typ: 'schwelle-nachziehen' }
  | { typ: 'drift-inspect' }
  | { typ: 'plan-oeffnen' }

export type ActionZustand = 'kritisch' | 'giessen' | 'blocker' | 'ok'

export function getActionState(
  zone: Zone, empfehlung: GiessEmpfehlung | null,
): ActionZustand {
  const ist = zone.aktuelle_feuchte
  if (ist != null && zone.feuchte_kritisch != null && ist < zone.feuchte_kritisch) {
    return 'kritisch'
  }
  if (empfehlung?.empfehlungs_typ === 'akut') return 'kritisch'
  if (empfehlung?.soll_bewaessern) return 'giessen'
  if (empfehlung && !empfehlung.soll_bewaessern && empfehlung.blocker_typ != null
      && empfehlung.blocker_typ !== 'FEUCHTE_OK') {
    return 'blocker'
  }
  return 'ok'
}
