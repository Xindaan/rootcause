"""LightGBM Training mit Walk-Forward Cross-Validation.

Walk-Forward: Zeitlich geordnet, kein Shuffle, 24h Gap zwischen Train/Test.
Stark regularisiert fuer kleine Datenmengen mit autokorrelierter Zeitreihe.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

try:
    import lightgbm as lgb
    import numpy as np
    import pandas as pd
except ImportError:
    raise ImportError(
        "ML-Abhaengigkeiten fehlen. Installiere mit: pip install -e '.[ml]'"
    )

from bewaesserung.ml.evaluation import (
    BaselineVorhersage,
    abdeckung_intervall,
    mae,
    pinball_loss,
    rmse,
    r2,
)
from bewaesserung.ml.modelle_ml import TrainingsMetriken

logger = logging.getLogger(__name__)

# Features die NICHT ins Modell gehen (Identifikation + Ziel)
# T-0062a: `ziel_delta_Xh` sind die neuen Trainingsziele (Delta zur
# aktuellen Feuchte), `ziel_feuchte_Xh` bleiben als Referenz fuer den
# Gate-Vergleich + Backward-Kompatibilitaet.
NICHT_FEATURES = {
    "zone_id", "zeitstempel", "quelle",
    # T-0267: geraet_id wird in features.py mitgefuehrt, damit
    # `ml_ausschluss_fenster` pro-Sensor filtern kann. Vom Modell
    # ignoriert (keine Feature-Spalte).
    "geraet_id",
    "ziel_feuchte_6h", "ziel_feuchte_12h", "ziel_feuchte_24h",
    "ziel_delta_6h", "ziel_delta_12h", "ziel_delta_24h",
}

# Kategorische Features (LightGBM nativ)
KATEGORISCHE = {"zone_kategorie", "sensor_quelle"}

# T-0061b: Parameter fuer Sample-Weighting (Regen-Ignoranz-Korrektur).
# Samples mit viel Regen (>=10 mm/24h) ODER Saettigung (>=90 % Feuchte)
# bekommen erhoehtes Gewicht — sie sind selten, aber die Prognose dort
# ist besonders wichtig (genau da irrt sich das Modell ohne Weighting).
# Gewicht = 3.0 ist empirisch: hoch genug um Regen-Features sichtbar zu
# machen, nicht so hoch dass Normal-Samples unterrepraesentiert werden.
# T-0127 (05.05.2026): Sample-Weighting fuer Regen umgestellt von binaerer
# Schwelle auf kontinuierliche Gewichtung. User-Befund: "Sollte Regen nicht
# immer beruecksichtigt werden?" Antwort: Niederschlag IST schon als Feature
# im Modell (niederschlag_summe_6h/12h/24h) — Modell sieht jeden Tropfen.
# Sample-Weighting ist eine separate Schicht: "wie intensiv soll aus
# Regentagen gelernt werden?". Binaerer 10-mm-Cutoff war zu grob — ein
# 2.5-mm-Tag mit 5 pp Sensor-Anstieg ist genauso aussagekraeftig wie ein
# 12-mm-Tag, wurde aber wie ein Trockentag behandelt.
# Neue Logik (lineare Skala mit Cap):
#   regen_gewicht = clip(1.0 + regen_24h / REGEN_GEWICHT_DIVISOR, 1.0, GEWICHT_SELTEN)
# 0 mm -> 1.0, 5 mm -> 2.0, 10 mm -> 3.0 (capped, sonst dominiert ein
# Starkregen-Tag das Training).
REGEN_GEWICHT_DIVISOR = 5.0    # = 5 mm pro +1.0 Gewicht
SAETTIGUNG_SCHWELLE_PROZENT = 90.0
GEWICHT_SELTEN = 3.0           # Cap
GEWICHT_NORMAL = 1.0
# Backward-Compat: alte Schwellen-Konstante (wird nur noch als Marker genutzt,
# falls Tests "klares Regen-Event"-Schwelle abfragen). Ab 3 mm ist das
# kontinuierliche Gewicht > 1.6 — also klar erhoehte Aufmerksamkeit.
REGEN_SCHWELLE_24H_MM = 3.0


def _berechne_sample_weights(df: "pd.DataFrame") -> "np.ndarray":
    """T-0061b/T-0127: Pro Trainings-Sample ein Gewicht — Regen/Saettigungs-
    Events staerker gewichten, damit LightGBM nicht zum Mean-Reversion-
    Prior konvergiert. Nutzt `niederschlag_summe_24h` und
    `boden_feuchte_aktuell` aus dem Feature-DataFrame; fehlt eine Spalte,
    gibt es einheitlich `1.0` zurueck (Backward-Kompat).

    T-0127: Regen-Gewichtung jetzt KONTINUIERLICH (vorher binaer >= 10 mm).
    Lineare Skala mit Cap: 0 mm -> 1.0, 5 mm -> 2.0, 10+ mm -> 3.0.
    Saettigungs-Gewicht bleibt binaer (Saettigung ist ein Zustand, kein
    Mengen-Signal).
    """
    n = len(df)
    weights = np.full(n, GEWICHT_NORMAL, dtype=float)
    if "niederschlag_summe_24h" not in df.columns or "boden_feuchte_aktuell" not in df.columns:
        return weights
    regen_24h = pd.to_numeric(df["niederschlag_summe_24h"], errors="coerce").fillna(0.0)
    feuchte = pd.to_numeric(df["boden_feuchte_aktuell"], errors="coerce").fillna(50.0)

    # Regen-Gewicht: kontinuierlich linear mit Cap.
    regen_gewicht = np.clip(
        GEWICHT_NORMAL + regen_24h.values / REGEN_GEWICHT_DIVISOR,
        GEWICHT_NORMAL,
        GEWICHT_SELTEN,
    )
    # Saettigungs-Gewicht: binaer (Zustand, nicht Menge).
    saettigung_gewicht = np.where(
        (feuchte >= SAETTIGUNG_SCHWELLE_PROZENT).values,
        GEWICHT_SELTEN,
        GEWICHT_NORMAL,
    )
    # Kombiniert: max — wenn beide Bedingungen zutreffen, das hoechste
    # Gewicht gilt (kein doppeltes Gewichten).
    return np.maximum(regen_gewicht, saettigung_gewicht)

# LightGBM Default-Parameter (stark regularisiert)
STANDARD_PARAMS = {
    "objective": "regression",
    "metric": "mae",
    "verbosity": -1,
    "num_leaves": 31,           # Nicht zu gross (Overfitting)
    "min_data_in_leaf": 50,     # Min Samples pro Blatt
    "feature_fraction": 0.7,    # 70% Features pro Baum
    "bagging_fraction": 0.8,    # 80% Daten pro Baum
    "bagging_freq": 5,
    "learning_rate": 0.05,
    "lambda_l1": 0.1,           # L1 Regularisierung
    "lambda_l2": 1.0,           # L2 Regularisierung
    "max_depth": -1,            # Unbegrenzt (num_leaves begrenzt)
    "seed": 42,
}


class TrainingsPipeline:
    """Trainiert LightGBM-Modelle mit Walk-Forward CV.

    Unterstuetzt drei Modi:
    - Drei separate Modelle (6h, 12h, 24h) — Default
    - Ein Modell mit horizont_stunden als Feature
    - Vergleich beider Varianten
    """

    def __init__(
        self,
        modell_verzeichnis: str = "",
        params: dict | None = None,
        n_folds: int = 3,
        gap_stunden: int = 24,
        max_rounds: int = 500,
        monotone_constraints: bool = False,
        cluster_id: str | None = None,
        cluster_zonen: list[str] | None = None,
    ):
        """T-0082: `cluster_id` + `cluster_zonen` aktivieren Pro-Cluster-
        Modus. Default `None` haelt das alte Verhalten (alle Zonen, Files
        flach in `modell_verzeichnis`). Mit Cluster:
          - `_modell_dir = base / "feuchte" / cluster_id` (eigenes Subdir)
          - DataFrame wird in `_filtere_cluster_zonen(df)` auf
            `cluster_zonen` reduziert
          - Symlinks bleiben relativ zum Cluster-Subdir
        """
        if not modell_verzeichnis:
            from bewaesserung.konfig import ML_DATEN_PFAD
            modell_verzeichnis = ML_DATEN_PFAD
        self._cluster_id = cluster_id
        self._cluster_zonen = cluster_zonen
        if cluster_id is not None:
            self._modell_dir = Path(modell_verzeichnis) / "feuchte" / cluster_id
        else:
            self._modell_dir = Path(modell_verzeichnis)
        self._modell_dir.mkdir(parents=True, exist_ok=True)
        self._params = {**STANDARD_PARAMS, **(params or {})}
        self._n_folds = n_folds
        self._gap_stunden = gap_stunden
        self._max_rounds = max_rounds
        self._monotone_constraints = monotone_constraints

    def _filtere_cluster_zonen(self, daten: "pd.DataFrame") -> "pd.DataFrame":
        """T-0082: filtert DataFrame auf die Cluster-Zonen, falls gesetzt."""
        if self._cluster_zonen is None:
            return daten
        if "zone_id" not in daten.columns:
            # Defensive: ohne zone_id-Spalte kann kein Filter angewendet werden.
            logger.warning(
                "TrainingsPipeline.cluster_filter: 'zone_id'-Spalte fehlt — "
                "Filter ignoriert (Pipeline laeuft auf allen Daten)"
            )
            return daten
        return daten[daten["zone_id"].isin(self._cluster_zonen)].reset_index(drop=True)

    def trainiere(
        self,
        daten: "pd.DataFrame",
        horizonte: list[int] | None = None,
    ) -> dict[int, "TrainingsErgebnis"]:
        """Trainiert Modelle fuer alle Horizonte.

        Returns:
            Dict {horizont: TrainingsErgebnis} mit Modell + Metriken
        """
        if horizonte is None:
            horizonte = [6, 12, 24]

        daten = self._filtere_cluster_zonen(daten)

        ergebnisse = {}
        for h in horizonte:
            logger.info(f"Training Horizont {h}h...")
            ergebnis = self._trainiere_horizont(daten, h)
            if ergebnis is not None:
                ergebnisse[h] = ergebnis
                self._speichere_modell(ergebnis, h)
                logger.info(
                    f"  {h}h: MAE={ergebnis.metriken.mae:.2f}% "
                    f"(Baseline: {ergebnis.metriken.baseline_mae:.2f}%)"
                )

        # Symlink auf neuestes Modell
        self._aktualisiere_symlinks(ergebnisse)
        return ergebnisse

    def trainiere_quantile(
        self,
        daten: "pd.DataFrame",
        horizonte: list[int] | None = None,
        alphas: tuple[float, ...] = (0.1, 0.5, 0.9),
    ) -> dict[tuple[int, int], "TrainingsErgebnis"]:
        """T-0046: Trainiert pro Horizont je ein Modell pro Quantile.

        Ergebnis: dict keyed by (horizont, alpha_int) — z. B. (6, 10) fuer
        das q10-Modell mit 6h-Horizont. Speichert 9 `.lgbm`-Dateien und
        legt fuer jeden (horizont, alpha) einen Symlink
        `aktuell_{horizont}h_q{alpha_int}.lgbm` an.

        Metriken fuer Quantile-Modelle enthalten absolute MAE fuer Gates
        und zusaetzlich Pinball-Loss fuer die Quantile-Kalibrierung.
        """
        if horizonte is None:
            horizonte = [6, 12, 24]

        # q50 MUSS trainiert werden — der Punkt-Symlink `aktuell_Xh.lgbm`
        # zeigt darauf, Inferenz-Code erwartet einen Median. Ohne q50 waere
        # die Deploy-Konvention inkonsistent (alte Punktmodelle + neue
        # Quantile-Modelle gemischt).
        assert 0.5 in alphas, (
            "trainiere_quantile erfordert alpha=0.5 (Punkt-Symlink) — "
            f"gegeben: {alphas}"
        )

        daten = self._filtere_cluster_zonen(daten)

        ergebnisse: dict[tuple[int, int], TrainingsErgebnis] = {}
        for h in horizonte:
            for alpha in alphas:
                alpha_int = int(round(alpha * 100))
                logger.info(f"Quantile-Training {h}h q{alpha_int}...")
                ergebnis = self._trainiere_horizont(
                    daten, h,
                    params_override={"objective": "quantile", "alpha": alpha},
                )
                if ergebnis is not None:
                    ergebnisse[(h, alpha_int)] = ergebnis
                    self._speichere_modell(ergebnis, h, alpha_int=alpha_int)
                    pinball = ergebnis.metriken.pinball_loss
                    pinball_text = f"{pinball:.3f}%" if pinball is not None else "n/a"
                    logger.info(
                        f"  {h}h q{alpha_int}: "
                        f"MAE={ergebnis.metriken.mae:.3f}% "
                        f"Pinball={pinball_text}"
                    )

        self._aktualisiere_symlinks_quantile(ergebnisse)

        # T-0062d: Band-Scale-Kalibrierung — pro Horizont messen, ob
        # q10-q90-Abdeckung in [0.75, 0.85] liegt. Wenn nicht, einfachen
        # Scale-Faktor pro Horizont speichern. Das vermeidet das Aufblasen
        # auf 27 Modelle (3 pro Klasse), gibt aber kalibrierte Baender.
        self._kalibriere_band_scales(daten, ergebnisse, horizonte)

        return ergebnisse

    def _kalibriere_band_scales(
        self,
        daten: "pd.DataFrame",
        ergebnisse: dict[tuple[int, int], "TrainingsErgebnis"],
        horizonte: list[int],
        ziel_abdeckung: float = 0.80,
        min_abdeckung: float = 0.75,
        max_abdeckung: float = 0.85,
    ) -> None:
        """T-0062d: Pro Horizont einen Band-Scale-Faktor schaetzen und
        speichern. Nutzt das letzte 20%-Fenster der zeitlich sortierten
        Trainingsdaten als Kalibrier-Set. Dafuer werden temporaere
        Holdout-Modelle nur auf den ersten 80 % trainiert, damit die
        Kalibrierung nicht auf In-Sample-Predictions der finalen Modelle
        basiert.

        Scale > 1 → Band zu schmal → bei Inferenz verbreitern.
        Scale < 1 → Band zu breit.
        Scale = 1 (Default) → Abdeckung bereits im Zielbereich.
        """
        for h in horizonte:
            # Nur wenn q10 + q90 beide trainiert wurden
            if (h, 10) not in ergebnisse or (h, 90) not in ergebnisse:
                continue
            # Kalibrier-Set: sortiert, letzte 20 %
            df_sort = daten[daten[f"ziel_feuchte_{h}h"].notna()].copy()
            df_sort = df_sort.sort_values("zeitstempel").reset_index(drop=True)
            if len(df_sort) < 100:
                logger.warning(f"band_scale.zu_wenig_daten horizont={h} n={len(df_sort)}")
                continue
            kalib_start = int(len(df_sort) * 0.8)
            df_train = df_sort.iloc[:kalib_start]
            df_kalib = df_sort.iloc[kalib_start:]
            if len(df_train) < 50 or len(df_kalib) < 20:
                logger.warning(
                    "band_scale.zu_wenig_holdout_daten horizont=%s "
                    "train=%s kalib=%s",
                    h, len(df_train), len(df_kalib),
                )
                continue

            # Delta-Flag aus dem q50-Ergebnis (alle Alphas sind gleich
            # konfiguriert — entweder alle Delta oder alle absolut).
            ist_delta = getattr(
                ergebnisse[(h, 50)].metriken, "delta_ziel", False,
            )

            # Feature-Matrix aufbauen wie bei der Inferenz
            e_q10 = ergebnisse[(h, 10)]
            feature_cols = e_q10.feature_cols
            try:
                modell_q10 = self._trainiere_holdout_quantil(
                    df_train, h, feature_cols, alpha=0.1,
                    delta_mode=ist_delta,
                )
                modell_q90 = self._trainiere_holdout_quantil(
                    df_train, h, feature_cols, alpha=0.9,
                    delta_mode=ist_delta,
                )
            except Exception:
                logger.exception(f"band_scale.holdout_training_fehler horizont={h}")
                continue
            if modell_q10 is None or modell_q90 is None:
                continue

            X = df_kalib[feature_cols].copy()
            for col in feature_cols:
                if col in KATEGORISCHE:
                    X[col] = X[col].astype("category")
                else:
                    X[col] = pd.to_numeric(X[col], errors="coerce")

            try:
                q10_pred = modell_q10.predict(X)
                q90_pred = modell_q90.predict(X)
            except Exception:
                logger.exception(f"band_scale.predict_fehler horizont={h}")
                continue

            # Bei Delta-Training: absoluten Bereich herleiten
            aktuell = df_kalib["boden_feuchte_aktuell"].values
            if ist_delta:
                q10_abs = aktuell + q10_pred
                q90_abs = aktuell + q90_pred
            else:
                q10_abs = q10_pred
                q90_abs = q90_pred
            y_true = df_kalib[f"ziel_feuchte_{h}h"].values

            # Sortieren, falls q10 > q90 irgendwo (Quantile-Crossing)
            q_lo = np.minimum(q10_abs, q90_abs)
            q_hi = np.maximum(q10_abs, q90_abs)

            abdeckung_roh = float(abdeckung_intervall(y_true, q_lo, q_hi))

            # Scale berechnen. Clipping auf [0.5, 2.0] damit ein einzelner
            # Ausreisser-Fold nicht das Band zerstoert.
            if min_abdeckung <= abdeckung_roh <= max_abdeckung:
                scale = 1.0
            elif abdeckung_roh <= 0:
                scale = 1.0
                logger.warning(f"band_scale.abdeckung_null horizont={h}")
            else:
                # Raw-Scale: Wenn Abdeckung 0.5 und Ziel 0.8 → Band muss
                # 1.6× so breit sein. Aber nicht-linear — mit Root als
                # Daempfung, weil Gauss-Breite ~ 1/cdf_inv.
                raw_scale = ziel_abdeckung / abdeckung_roh
                scale = max(0.5, min(2.0, raw_scale))

            # Simulierte Abdeckung mit Scale (nur informativ geloggt)
            median = (q_lo + q_hi) / 2
            halbbreite = (q_hi - q_lo) / 2 * scale
            q_lo_scaled = median - halbbreite
            q_hi_scaled = median + halbbreite
            abdeckung_nach_scale = float(
                abdeckung_intervall(y_true, q_lo_scaled, q_hi_scaled)
            )

            daten_json = {
                "horizont_h": h,
                "scale": round(float(scale), 3),
                "abdeckung_roh": round(abdeckung_roh, 3),
                "abdeckung_nach_scale": round(abdeckung_nach_scale, 3),
                "ziel_abdeckung": ziel_abdeckung,
                "n_samples": int(len(df_kalib)),
                "train_samples": int(len(df_train)),
                "kalibrierung": "holdout_80_20",
                "berechnet_am": datetime.now().isoformat(),
            }
            pfad = self._modell_dir / f"band_scale_{h}h.json"
            with open(pfad, "w") as f:
                json.dump(daten_json, f, indent=2)
            # ACHTUNG stdlib-logger in diesem Modul: KEINE structlog-kwargs.
            # Vorher crashte der Retrain-Job mit TypeError: Logger._log() got
            # an unexpected keyword argument 'horizont'.
            logger.info(
                "band_scale.kalibriert horizont=%dh scale=%.3f "
                "abdeckung_roh=%.3f abdeckung_nach_scale=%.3f",
                h,
                round(float(scale), 3),
                round(float(abdeckung_roh), 3),
                round(float(abdeckung_nach_scale), 3),
            )

    def _trainiere_holdout_quantil(
        self,
        df_train: "pd.DataFrame",
        horizont: int,
        feature_cols: list[str],
        alpha: float,
        delta_mode: bool,
    ) -> "lgb.Booster | None":
        """Trainiert ein temporaeres Quantil-Modell fuer Band-Kalibrierung."""
        delta_col = f"ziel_delta_{horizont}h"
        absolut_col = f"ziel_feuchte_{horizont}h"
        ziel_col = (
            delta_col
            if delta_mode and delta_col in df_train.columns
            else absolut_col
        )
        if ziel_col not in df_train.columns:
            return None

        df = df_train[df_train[ziel_col].notna()].copy()
        if len(df) < 50:
            return None

        X = df[feature_cols].copy()
        for col in feature_cols:
            if col in KATEGORISCHE:
                X[col] = X[col].astype("category")
            else:
                X[col] = pd.to_numeric(X[col], errors="coerce")
        y = df[ziel_col].values
        kat_indices = [
            i for i, col in enumerate(feature_cols) if col in KATEGORISCHE
        ]
        ds_train = lgb.Dataset(
            X, label=y,
            weight=_berechne_sample_weights(df),
            categorical_feature=kat_indices,
            free_raw_data=False,
        )
        params = {**self._params, "objective": "quantile", "alpha": alpha}
        params, _ = self._params_fuer_features(params, feature_cols)
        return lgb.train(
            params,
            ds_train,
            num_boost_round=min(self._max_rounds, 50),
        )

    def _trainiere_horizont(
        self, daten: "pd.DataFrame", horizont: int,
        params_override: dict | None = None,
        delta_mode: bool = True,
    ) -> "TrainingsErgebnis | None":
        """Trainiert ein Modell fuer einen einzelnen Horizont.

        `params_override` ueberschreibt selektiv `self._params` — genutzt
        fuer Quantile-Regression (objective="quantile", alpha=...).

        T-0062a: `delta_mode=True` (Default) trainiert auf
        `ziel_delta_Xh` (Aenderung gegenueber aktueller Feuchte) statt
        auf `ziel_feuchte_Xh` (absolutes Niveau). Eliminiert den
        Mean-Reversion-Bias-Anker bei ~44 %. MAE wird intern weiterhin
        auf absoluter Prognose (aktuell + Delta) berechnet, damit Gate
        und Baseline-Vergleiche fair bleiben.
        `delta_mode=False` bleibt als Notausstieg falls Delta-Daten
        fehlen (z.B. wenn altes DataFrame ohne `ziel_delta_*`).
        """
        basis_params = {**self._params, **(params_override or {})}
        quantile_alpha = (
            float(basis_params.get("alpha", 0.5))
            if basis_params.get("objective") == "quantile"
            else None
        )
        # Ziel-Spalte: Delta oder absolut. Bei delta_mode=True aber Spalte
        # fehlt (alte Features): Fallback auf absolute Spalte mit Warning.
        delta_col = f"ziel_delta_{horizont}h"
        absolut_col = f"ziel_feuchte_{horizont}h"
        if delta_mode and delta_col in daten.columns:
            ziel_col = delta_col
            ist_delta_training = True
        else:
            if delta_mode and delta_col not in daten.columns:
                logger.warning(
                    "ml.training.delta_fallback spalte=%s nicht vorhanden — nutze %s",
                    delta_col, absolut_col,
                )
            ziel_col = absolut_col
            ist_delta_training = False

        if ziel_col not in daten.columns:
            logger.warning(f"Zielvariable {ziel_col} nicht im DataFrame")
            return None

        # Nur Zeilen mit gueltigem Ziel, global zeitlich sortiert
        df = daten[daten[ziel_col].notna()].copy()
        df = df.sort_values("zeitstempel").reset_index(drop=True)
        if len(df) < 100:
            logger.warning(f"Zu wenig Daten fuer {horizont}h: {len(df)} Zeilen")
            return None

        # Features vorbereiten
        feature_cols = self._feature_spalten(df)
        effektive_params, monotone_features = self._params_fuer_features(
            basis_params,
            feature_cols,
        )
        X = df[feature_cols].copy()
        y = df[ziel_col].values

        # Numerische Spalten erzwingen (None → NaN, object → float)
        # LightGBM verarbeitet NaN nativ, braucht aber float/int/bool Dtype
        for col in feature_cols:
            if col in KATEGORISCHE:
                X[col] = X[col].astype("category")
            else:
                X[col] = pd.to_numeric(X[col], errors="coerce")

        # Kategorische Features markieren
        kat_indices = [
            i for i, col in enumerate(feature_cols) if col in KATEGORISCHE
        ]

        # T-0061b: Sample-Weighting gegen Regen-Ignoranz. Regen- und
        # Saettigungs-Events sind im Datensatz selten → das Modell lernt
        # die Mean-Reversion als Default. Mit 3x Gewicht auf diesen Samples
        # verschieben wir den Trainings-Loss, sodass LightGBM die Regen-
        # Features staerker beachtet, ohne harte Regeln im Code zu haben.
        sample_weights = _berechne_sample_weights(df)

        # Walk-Forward CV mit Gap.
        # T-0062a: bei Delta-Training wird die rohe y_pred ein Delta sein;
        # der CV-Loop muss `aktuell + y_pred` rechnen um absolute MAE zu
        # bekommen, damit Gate-Vergleich gegen alte absolute Modelle fair
        # bleibt.
        cv_ergebnisse = self._walk_forward_cv(
            X, y, kat_indices, df, horizont,
            effektive_params=effektive_params,
            quantile_alpha=quantile_alpha,
            sample_weights=sample_weights,
            delta_ziel=ist_delta_training,
        )
        if not cv_ergebnisse:
            logger.warning(
                "ml.training.keine_cv_folds horizont=%s samples=%s",
                horizont, len(df),
            )
            return None

        # Finales Modell auf allen Daten
        ds_train = lgb.Dataset(
            X, label=y,
            weight=sample_weights,
            categorical_feature=kat_indices,
            free_raw_data=False,
        )
        # Mittlere Runden-Anzahl aus CV als Stopping-Punkt
        mittlere_runden = max(
            10,
            int(np.mean([r["beste_runde"] for r in cv_ergebnisse]))
        )
        modell = lgb.train(
            effektive_params,
            ds_train,
            num_boost_round=mittlere_runden,
        )

        # Feature Importances
        importances = dict(zip(
            feature_cols,
            modell.feature_importance(importance_type="gain"),
        ))
        # Normalisieren
        total = sum(importances.values()) or 1
        importances = {
            k: round(v / total, 4)
            for k, v in sorted(importances.items(), key=lambda x: -x[1])
        }

        # Gesamt-Metriken (Mittel ueber CV-Folds)
        cv_mae = np.mean([r["mae"] for r in cv_ergebnisse])
        cv_rmse = np.mean([r["rmse"] for r in cv_ergebnisse])
        cv_r2 = np.mean([r["r2"] for r in cv_ergebnisse])
        cv_baseline_mae = np.mean([r["baseline_mae"] for r in cv_ergebnisse])
        cv_baseline_rmse = np.mean([r["baseline_rmse"] for r in cv_ergebnisse])
        cv_baseline_r2 = np.mean([r["baseline_r2"] for r in cv_ergebnisse])
        cv_pinball = [
            r["pinball_loss"]
            for r in cv_ergebnisse
            if r.get("pinball_loss") is not None
        ]
        cv_baseline_pinball = [
            r["baseline_pinball_loss"]
            for r in cv_ergebnisse
            if r.get("baseline_pinball_loss") is not None
        ]

        # MAE pro Zone (aus letztem Fold)
        mae_pro_zone = cv_ergebnisse[-1].get("mae_pro_zone", {})

        metriken = TrainingsMetriken(
            zeitstempel=datetime.now(),
            horizont_stunden=horizont,
            anzahl_samples=len(df),
            anzahl_features=len(feature_cols),
            mae=round(float(cv_mae), 3),
            rmse=round(float(cv_rmse), 3),
            r2=round(float(cv_r2), 4),
            baseline_mae=round(float(cv_baseline_mae), 3),
            baseline_rmse=round(float(cv_baseline_rmse), 3),
            baseline_r2=round(float(cv_baseline_r2), 4),
            pinball_loss=(
                round(float(np.mean(cv_pinball)), 3) if cv_pinball else None
            ),
            baseline_pinball_loss=(
                round(float(np.mean(cv_baseline_pinball)), 3)
                if cv_baseline_pinball else None
            ),
            feature_schema_version=2,
            feature_importances=dict(list(importances.items())[:15]),
            mae_pro_zone=mae_pro_zone,
            delta_ziel=ist_delta_training,
            monotone_constraints=self._monotone_constraints,
            monotone_features=monotone_features,
            regen_slice_mae=self._gewichtete_regen_slice_mae(cv_ergebnisse),
            regen_slice_n=sum(int(r.get("regen_slice_n", 0)) for r in cv_ergebnisse),
        )

        return TrainingsErgebnis(
            modell=modell,
            metriken=metriken,
            feature_cols=feature_cols,
        )

    def _walk_forward_cv(
        self,
        X: "pd.DataFrame",
        y: "np.ndarray",
        kat_indices: list[int],
        df_voll: "pd.DataFrame",
        horizont: int = 6,
        effektive_params: dict | None = None,
        quantile_alpha: float | None = None,
        sample_weights: "np.ndarray | None" = None,
        delta_ziel: bool = False,
    ) -> list[dict]:
        """Walk-Forward Cross-Validation mit zeitbasiertem Gap.

        Daten muessen global nach zeitstempel sortiert sein (nicht zoneweise).
        Gap wird anhand tatsaechlicher Zeitstempel berechnet, nicht Zeilenanzahl.
        Wenn `quantile_alpha` gesetzt ist, wird Pinball-Loss separat
        protokolliert; `mae` bleibt absolute MAE fuer Gate-Vergleiche.
        """
        effektive_params = effektive_params or self._params
        n = len(X)
        # ISO8601 statt fixem Format — Zeitstempel koennen mit oder ohne
        # Mikrosekunden kommen (Sensor-Messungen vs. Wetter-Vorhersagen).
        zeitstempel = pd.to_datetime(df_voll["zeitstempel"].values, format="ISO8601")
        gap_delta = pd.Timedelta(hours=self._gap_stunden)

        # Mindest-Trainingsgroesse: 60% der Daten
        min_train = max(200, int(n * 0.6))
        # Test-Fenster: ~1 Woche oder 1/n_folds der restlichen Daten
        rest = n - min_train
        test_groesse = max(50, rest // self._n_folds)

        ergebnisse = []
        baseline = BaselineVorhersage()

        for fold in range(self._n_folds):
            train_ende = min_train + fold * test_groesse

            if train_ende >= n:
                break

            # Zeitbasierter Gap: erste Zeile deren Zeitstempel >= train_ende + gap
            grenze = zeitstempel[train_ende - 1] + gap_delta
            test_kandidaten = np.where(zeitstempel >= grenze)[0]
            if len(test_kandidaten) == 0:
                break
            test_start = int(test_kandidaten[0])
            test_ende = min(test_start + test_groesse, n)

            if test_start >= test_ende:
                break

            X_train = X.iloc[:train_ende]
            y_train = y[:train_ende]
            X_test = X.iloc[test_start:test_ende]
            y_test = y[test_start:test_ende]

            w_train = (
                sample_weights[:train_ende]
                if sample_weights is not None else None
            )
            w_valid = (
                sample_weights[test_start:test_ende]
                if sample_weights is not None else None
            )
            ds_train = lgb.Dataset(
                X_train, label=y_train,
                weight=w_train,
                categorical_feature=kat_indices,
                free_raw_data=False,
            )
            ds_valid = lgb.Dataset(
                X_test, label=y_test,
                weight=w_valid,
                categorical_feature=kat_indices,
                reference=ds_train,
                free_raw_data=False,
            )

            # Training mit Early Stopping
            callbacks = [
                lgb.early_stopping(stopping_rounds=20, verbose=False),
                lgb.log_evaluation(period=0),  # Still
            ]
            modell = lgb.train(
                effektive_params,
                ds_train,
                num_boost_round=self._max_rounds,
                valid_sets=[ds_valid],
                valid_names=["valid"],
                callbacks=callbacks,
            )

            y_pred = modell.predict(X_test)
            beste_runde = modell.best_iteration or self._max_rounds

            # Baseline-Vorhersage fuer Vergleich
            df_test = df_voll.iloc[test_start:test_ende]
            y_baseline = baseline.vorhersage(df_test, horizont)

            # T-0062a: bei Delta-Training ist `y_pred` das Delta, `y_test`
            # ebenfalls. Fuer die Gate-Metriken brauchen wir absolute
            # Prognose und absolutes Ziel — addieren + zurueckfallen auf
            # die absolute Zielspalte aus `df_test`.
            aktuell_test = df_test["boden_feuchte_aktuell"].values
            if delta_ziel:
                y_pred_abs = y_pred + aktuell_test
                y_test_abs = df_test[f"ziel_feuchte_{horizont}h"].values
            else:
                y_pred_abs = y_pred
                y_test_abs = y_test

            abs_mae = float(mae(y_test_abs, y_pred_abs))
            baseline_abs_mae = float(mae(y_test_abs, y_baseline))
            quantile_pinball = (
                float(pinball_loss(y_test, y_pred, quantile_alpha))
                if quantile_alpha is not None else None
            )
            baseline_quantile_pinball = (
                float(pinball_loss(y_test_abs, y_baseline, quantile_alpha))
                if quantile_alpha is not None else None
            )

            regen_slice_mae = None
            regen_slice_n = 0
            if (
                (quantile_alpha is None or abs(quantile_alpha - 0.5) < 1e-9)
                and "niederschlag_summe_24h" in df_test.columns
            ):
                regen_24h = pd.to_numeric(
                    df_test["niederschlag_summe_24h"],
                    errors="coerce",
                ).fillna(0.0)
                regen_maske = (regen_24h >= REGEN_SCHWELLE_24H_MM).values
                regen_slice_n = int(regen_maske.sum())
                if regen_slice_n > 0:
                    regen_slice_mae = float(
                        mae(y_test_abs[regen_maske], y_pred_abs[regen_maske])
                    )

            # Fehler pro Zone (analog, gleiche Metrik)
            mae_pro_zone = {}
            if "zone_id" in df_test.columns:
                for zone_id in df_test["zone_id"].unique():
                    zm = df_test["zone_id"] == zone_id
                    if zm.sum() >= 2:
                        wert = mae(y_test_abs[zm.values], y_pred_abs[zm.values])
                        mae_pro_zone[zone_id] = round(float(wert), 3)

            ergebnisse.append({
                "fold": fold,
                "train_n": len(X_train),
                "test_n": len(X_test),
                "beste_runde": beste_runde,
                "mae": abs_mae,
                "rmse": float(rmse(y_test_abs, y_pred_abs)),
                "r2": float(r2(y_test_abs, y_pred_abs)),
                "baseline_mae": baseline_abs_mae,
                "baseline_rmse": float(rmse(y_test_abs, y_baseline)),
                "baseline_r2": float(r2(y_test_abs, y_baseline)),
                "pinball_loss": quantile_pinball,
                "baseline_pinball_loss": baseline_quantile_pinball,
                "mae_pro_zone": mae_pro_zone,
                "regen_slice_mae": regen_slice_mae,
                "regen_slice_n": regen_slice_n,
            })

            logger.info(
                f"  Fold {fold}: MAE={ergebnisse[-1]['mae']:.2f}% "
                f"(Baseline: {ergebnisse[-1]['baseline_mae']:.2f}%), "
                f"Runden: {beste_runde}"
            )

        return ergebnisse

    def _feature_spalten(self, df: "pd.DataFrame") -> list[str]:
        """Bestimmt Feature-Spalten (alles ausser Identifikation + Ziel)."""
        return [
            col for col in df.columns
            if col not in NICHT_FEATURES
        ]

    @staticmethod
    def _monotone_constraint_liste(feature_cols: list[str]) -> tuple[list[int], list[str]]:
        """T-0062c: Regen-Summen monoton steigend, alle anderen Features frei."""
        monotone_features = [
            col for col in feature_cols
            if col.startswith("niederschlag_summe_")
        ]
        constraints = [
            1 if col.startswith("niederschlag_summe_") else 0
            for col in feature_cols
        ]
        return constraints, monotone_features

    def _params_fuer_features(
        self,
        params: dict,
        feature_cols: list[str],
    ) -> tuple[dict, list[str]]:
        """Ergaenzt LightGBM-Parameter um optionale Monotonie-Constraints."""
        if not self._monotone_constraints:
            return params, []
        constraints, monotone_features = self._monotone_constraint_liste(feature_cols)
        return {**params, "monotone_constraints": constraints}, monotone_features

    @staticmethod
    def _gewichtete_regen_slice_mae(cv_ergebnisse: list[dict]) -> float | None:
        """Aggregiert Regen-Slice-MAE ueber CV-Folds nach Sample-Anzahl."""
        zaehler = 0.0
        nenner = 0
        for ergebnis in cv_ergebnisse:
            n = int(ergebnis.get("regen_slice_n", 0))
            wert = ergebnis.get("regen_slice_mae")
            if n <= 0 or wert is None:
                continue
            zaehler += float(wert) * n
            nenner += n
        if nenner == 0:
            return None
        return round(float(zaehler / nenner), 3)

    def _speichere_modell(
        self, ergebnis: "TrainingsErgebnis", horizont: int,
        alpha_int: int | None = None,
    ) -> Path:
        """Speichert Modell + Metriken.

        `alpha_int` None → Punkt-Modell, sonst Quantile-Suffix `_q{alpha}`.
        """
        datum = datetime.now().strftime("%Y-%m-%d")
        suffix = f"_q{alpha_int}" if alpha_int is not None else ""
        modell_pfad = self._modell_dir / f"modell_{horizont}h{suffix}_{datum}.lgbm"
        metriken_pfad = self._modell_dir / f"metriken_{horizont}h{suffix}_{datum}.json"

        ergebnis.modell.save_model(str(modell_pfad))

        with open(metriken_pfad, "w") as f:
            json.dump(ergebnis.metriken.model_dump(mode="json"), f, indent=2)

        logger.info(f"  Modell gespeichert: {modell_pfad}")
        return modell_pfad

    def _aktualisiere_symlinks(
        self, ergebnisse: dict[int, "TrainingsErgebnis"],
    ) -> None:
        """Erstellt/aktualisiert 'aktuell_Xh.lgbm' Symlinks."""
        datum = datetime.now().strftime("%Y-%m-%d")
        for horizont in ergebnisse:
            link = self._modell_dir / f"aktuell_{horizont}h.lgbm"
            ziel = self._modell_dir / f"modell_{horizont}h_{datum}.lgbm"
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(ziel.name)
            logger.info(f"  Symlink: {link.name} → {ziel.name}")

    def _aktualisiere_symlinks_quantile(
        self, ergebnisse: dict[tuple[int, int], "TrainingsErgebnis"],
    ) -> None:
        """Erstellt 'aktuell_Xh_qY.lgbm' Symlinks fuer Quantile-Modelle.

        Zusaetzlich zeigt `aktuell_Xh.lgbm` auf das q50-Modell — damit
        bleibt der Punkt-Wert der Vorhersage der Median (Plan-konform).
        """
        datum = datetime.now().strftime("%Y-%m-%d")
        for (horizont, alpha_int) in ergebnisse:
            link = self._modell_dir / f"aktuell_{horizont}h_q{alpha_int}.lgbm"
            ziel = self._modell_dir / (
                f"modell_{horizont}h_q{alpha_int}_{datum}.lgbm"
            )
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(ziel.name)
            logger.info(f"  Symlink: {link.name} → {ziel.name}")

        # Punkt-Modell-Symlink auf q50 zeigen lassen, damit der Service
        # bisherige Vorhersage-Pfade nicht veraendern muss.
        horizonte = {h for (h, _) in ergebnisse}
        for h in horizonte:
            if (h, 50) not in ergebnisse:
                # Sollte durch den assert in trainiere_quantile nicht mehr
                # vorkommen — aber hier hart fehlschlagen, damit alter
                # Punkt-Symlink nicht gegen ein veraltetes Modell zeigt,
                # waehrend q10/q90 neu sind.
                raise RuntimeError(
                    f"q50-Modell fuer Horizont {h} fehlt — "
                    "Symlink-Deploy abgebrochen."
                )
            link = self._modell_dir / f"aktuell_{h}h.lgbm"
            ziel = self._modell_dir / f"modell_{h}h_q50_{datum}.lgbm"
            if not ziel.exists():
                raise RuntimeError(
                    f"q50-Modelldatei fehlt auf Platte: {ziel}"
                )
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(ziel.name)
            logger.info(f"  Punkt-Symlink (q50): {link.name} → {ziel.name}")

            # Metriken-Datei kopieren: aktuell_Xh.lgbm erwartet beim Laden
            # "metriken_Xh_DATUM.json". Statt umzubenennen: Symlink setzen.
            metriken_link = self._modell_dir / f"metriken_{h}h_{datum}.json"
            metriken_ziel = self._modell_dir / f"metriken_{h}h_q50_{datum}.json"
            if metriken_ziel.exists():
                if metriken_link.exists() or metriken_link.is_symlink():
                    metriken_link.unlink()
                metriken_link.symlink_to(metriken_ziel.name)


class TrainingsErgebnis:
    """Container fuer trainiertes Modell + Metriken."""

    def __init__(
        self,
        modell: "lgb.Booster",
        metriken: TrainingsMetriken,
        feature_cols: list[str],
    ):
        self.modell = modell
        self.metriken = metriken
        self.feature_cols = feature_cols
