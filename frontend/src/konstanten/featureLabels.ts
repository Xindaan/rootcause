/* Klartext-Labels fuer ML-Feature-Namen aus LightGBM (T-0040 SHAP-Attribution).
 *
 * Werden im neuen MLAttributierung-Panel verwendet, damit User nicht mit
 * `feuchte_trend_6h`-artigen Raw-Keys konfrontiert sind. Fallback: wenn ein
 * Feature nicht in der Map ist, rendert die UI den Raw-Key in Monospace,
 * damit neu hinzugefuegte Features nicht die Anzeige brechen.
 */

export const FEATURE_LABELS: Record<string, string> = {
  // Feuchte-Kern
  boden_feuchte_aktuell: 'Aktuelle Feuchte',
  feuchte_jetzt: 'Aktuelle Feuchte',
  feuchte_6h: 'Feuchte vor 6 h',
  boden_feuchte_lag1h: 'Feuchte vor 1 h',
  boden_feuchte_lag6h: 'Feuchte vor 6 h',
  boden_feuchte_lag24h: 'Feuchte vor 24 h',
  feuchte_trend_6h: 'Feuchte-Trend 6 h',
  boden_feuchte_diff_1h: 'Feuchte-Delta 1 h',
  boden_feuchte_diff_24h: 'Feuchte-Delta 24 h',
  feuchte_rolling_6h: 'Feuchte rollend 6 h',
  feuchte_rolling_24h: 'Feuchte rollend 24 h',
  boden_feuchte_rolling_mean_6h: 'Feuchte Mittel 6 h',
  boden_feuchte_rolling_std_6h: 'Feuchte Std 6 h',
  feuchte_schwelle_min: 'Feuchte-Schwelle (min)',

  // Umgebung
  boden_temperatur: 'Bodentemperatur',
  licht: 'Licht',
  quelle: 'Sensor-Quelle',
  sensor_quelle: 'Sensor-Quelle',

  // Zeit-Features
  stunde_sin: 'Tageszeit (sin)',
  stunde_cos: 'Tageszeit (cos)',
  stunde_des_tages: 'Stunde',
  tag_der_woche: 'Wochentag',
  tag_im_jahr_sin: 'Tag im Jahr (sin)',
  tag_im_jahr_cos: 'Tag im Jahr (cos)',

  // Bewaesserung
  letzte_bewaesserung_dauer_s: 'Letzte Bew.-Dauer',
  letzte_bewaesserung_vor_h: 'Letzte Bew. vor',

  // Wetter + Bilanz
  niederschlag_summe_6h: 'Niederschlag 6 h',
  niederschlag_summe_12h: 'Niederschlag 12 h',
  niederschlag_summe_24h: 'Niederschlag 24 h',
  vpd_mittel_6h: 'Dampfdruckdefizit 6 h',
  vpd_mittel_12h: 'Dampfdruckdefizit 12 h',
  vpd_mittel_24h: 'Dampfdruckdefizit 24 h',
  bilanz_liter_6h: 'Wasser-Bilanz 6 h',
  bilanz_liter_12h: 'Wasser-Bilanz 12 h',
  bilanz_liter_24h: 'Wasser-Bilanz 24 h',
  bilanz_diff_24h: 'Aufnahme-Residual 24 h',

  // Zone-Eigenschaften
  wind_match: 'Wind-Exposition',
  ist_indoor: 'Indoor-Flag',
  ist_topf: 'Topf-Flag',
  flaeche_m2: 'Fläche',
}

export function featureLabel(name: string): string | null {
  return FEATURE_LABELS[name] ?? null
}
