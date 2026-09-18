/* T-0610 FE-9: welche Daten bekommt eine V4-Karte aus dem Bulk-Snapshot?
 *
 * Die Karte holt Messwerte, ML und Empfehlung nur dann selbst, wenn das
 * jeweilige `*Propagiert`-Prop `undefined` ist (T-0565-Fallback). Vor dem
 * ersten Snapshot-Versuch stand fuer jede Zone `undefined` -- alle Karten
 * feuerten ihre Einzelabrufe parallel zum Bulk-Snapshot (14 Zonen: 28 bis
 * 42 Requests, die der Snapshot Sekunden spaeter ohnehin ersetzt).
 *
 * Regel:
 * - Snapshot fuer die Zone da -> seine Werte (fehlende Felder als stabile
 *   leere Stubs, damit React.memo und Effect-Dependencies ruhig bleiben).
 * - noch kein Versuch abgeschlossen -> Stubs: die Karte wartet ("lade ...").
 * - Versuch abgeschlossen, Zone fehlt (Fehler oder neue Zone) -> `undefined`:
 *   die Karte holt selbst nach, wie seit T-0565.
 */
import type {
  DashboardSnapshotZone, GiessEmpfehlung, Messwert, MLVorhersage, SnapshotFenster,
} from '../../typen'

// T-0201-Perf: stabile leere Defaults, damit `?? {}` / `?? null` im JSX
// keine neue Referenz pro Render erzeugt. React.memo auf der Karte
// kann sonst nicht greifen, weil jedes Prop "neu" aussieht.
export const LEERES_ML_OBJ: Record<string, MLVorhersage> = Object.freeze({})
export const LEERES_MESSWERTE_OBJ: Partial<Record<SnapshotFenster, Messwert[]>> = Object.freeze({})
export const LEERE_EMPFEHLUNG: GiessEmpfehlung | null = null

export interface KartenSnapshotDaten {
  mlVorhersagePropagiert: Record<string, MLVorhersage> | undefined
  messwertePropagiert: Partial<Record<SnapshotFenster, Messwert[]>> | undefined
  empfehlungPropagiert: GiessEmpfehlung | null | undefined
}

export function kartenSnapshotDaten(
  snap: DashboardSnapshotZone | undefined,
  snapshotVersucht: boolean,
): KartenSnapshotDaten {
  if (snap) {
    return {
      mlVorhersagePropagiert: snap.ml_vorhersage ?? LEERES_ML_OBJ,
      messwertePropagiert: snap.messwerte ?? LEERES_MESSWERTE_OBJ,
      empfehlungPropagiert: snap.empfehlung ?? LEERE_EMPFEHLUNG,
    }
  }
  if (!snapshotVersucht) {
    return {
      mlVorhersagePropagiert: LEERES_ML_OBJ,
      messwertePropagiert: LEERES_MESSWERTE_OBJ,
      empfehlungPropagiert: LEERE_EMPFEHLUNG,
    }
  }
  return {
    mlVorhersagePropagiert: undefined,
    messwertePropagiert: undefined,
    empfehlungPropagiert: undefined,
  }
}
