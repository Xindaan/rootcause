/* T-0037/T-0610 Q3: Query-Parameter der Entscheidungs-Historie.
 *
 * Eigene Datei ohne Laufzeit-Abhaengigkeiten, damit
 * `tests/pruefe_entscheidungs_filter.mjs` sie ohne Browser pruefen kann.
 * Der Parametername `ohne_monitoring` muss zum Backend passen
 * (`api_server.letzte_entscheidungen`); ein falscher Name faellt dort still
 * auf den Default (Monitoring-Zeilen sichtbar) zurueck.
 *
 * T-0610 EL-3: Blocker-Beschriftung der Historie liegt ebenfalls hier. Der
 * Typ `Record<BlockerTyp, string>` laesst tsc rot werden, sobald ein neuer
 * Wert in der Union (`typen.ts`, Spiegel von `modelle.BlockerTyp`) keine
 * Beschriftung hat; STRATEGIE_KEIN_BEDARF fehlte so bis 14.10.2026.
 */
import type { BlockerTyp } from './typen'

export const BLOCKER_LABEL: Record<BlockerTyp, string> = {
  FEUCHTE_OK: 'Feuchte ausreichend',
  KEINE_MESSUNG: 'Keine Messung',
  REGEN_ERWARTET: 'Regen erwartet',
  ZEITFENSTER: 'Zeitfenster',
  BUDGET_ERSCHOEPFT: 'Tagesbudget voll',
  PAUSE_AKTIV: 'Pause aktiv',
  FROSTSPERRE: 'Frostsperre',
  FROST_VORHERSAGE_FEHLT: 'Vorhersage fehlt',
  AUTOMATIK_SPERRFENSTER: 'Sperrfenster',
  STRATEGIE_KEIN_BEDARF: 'Strategie: kein Bedarf',
  // Scharfe Kanal-Zone: Start und Dosis stehen in der Kanalzeile.
  KANAL_ENTSCHEIDET: 'Kanal entscheidet',
}

/** Filter-Optionen der Historie ('' = alle), in der Reihenfolge oben. */
export const BLOCKER_OPTIONEN: { wert: string; label: string }[] = [
  { wert: '', label: 'Alle' },
  ...Object.entries(BLOCKER_LABEL).map(([wert, label]) => ({ wert, label })),
]

/** Beschriftung einer Zeile; unbekannte Werte roh statt verschluckt. */
export function blockerLabel(typ: string | null | undefined): string {
  if (!typ) return '–'
  return (BLOCKER_LABEL as Record<string, string>)[typ] ?? typ
}

export interface EntscheidungsFilter {
  zone_id?: string
  von?: string   // ISO
  bis?: string   // ISO
  blocker_typ?: string
  limit?: number
  /** T-0610 Q3: Routinezeilen "Zone im Monitoring-Modus" serverseitig
   *  weglassen (vor dem Limit). */
  ohne_monitoring?: boolean
}

export function baueEntscheidungsParams(f: EntscheidungsFilter): URLSearchParams {
  const p = new URLSearchParams()
  if (f.zone_id) p.set('zone_id', f.zone_id)
  if (f.von) p.set('von', f.von)
  if (f.bis) p.set('bis', f.bis)
  if (f.blocker_typ) p.set('blocker_typ', f.blocker_typ)
  if (f.limit != null) p.set('limit', String(f.limit))
  if (f.ohne_monitoring) p.set('ohne_monitoring', 'true')
  return p
}
