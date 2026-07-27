"""T-0065: Trainingspipeline fuer das Bewaesserungs-Response-Modell.

Zwei Modelle pro Zone:

1. **Forward** (Shadow-Benchmark, Akzeptanz-Messung):
   Features = dauer_s + Kontext → Ziel = delta_6h_ist.
   Drei Quantile (q10/q50/q90) fuer Unsicherheits-Baender.

2. **Inverse** (Entscheidungs-Produktion):
   Features = ziel_delta + Kontext → Ziel = dauer_s.
   Punkt-Modell (q50-Objective = Median).

Beide mit Walk-Forward-CV (n_folds=3, Gap 24h) und Monotonie-Constraints.
Persistenz: `{basis_verzeichnis}/{zone}/v{timestamp}/*.lgbm` + Symlinks
`aktuell_{zone}_forward_q{10,50,90}.lgbm`, `aktuell_{zone}_inverse.lgbm`,
`aktuell_{zone}_metadata.json`.

Das Modul nutzt stdlib `logging` — in Regression-Tests
`caplog.set_level(INFO)` setzen (fehlerpattern_stdlib_structlog_mix.md).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

try:
    import lightgbm as lgb
    import numpy as np
    import pandas as pd
except ImportError:
    raise ImportError(
        "ML-Abhaengigkeiten fehlen. Installiere mit: pip install -e '.[ml]'"
    )

from bewaesserung.ml.response_features import (
    FORWARD_FEATURES,
    FORWARD_MONOTONIE,
    INVERSE_FEATURES,
    INVERSE_MONOTONIE,
    monotone_vektor,
)

logger = logging.getLogger(__name__)


# Konservative Parameter fuer kleines N (n=5..20 pro Zone).
STANDARD_PARAMS: dict = {
    "objective": "regression",
    "metric": "mae",
    "verbosity": -1,
    "num_leaves": 7,
    "min_data_in_leaf": 3,
    "max_depth": 3,
    "learning_rate": 0.03,
    "lambda_l1": 0.2,
    "lambda_l2": 1.0,
    "seed": 42,
}

STANDARD_N_ESTIMATORS = 300
STANDARD_EARLY_STOPPING = 30

# Quantile fuer das Forward-Modell.
FORWARD_QUANTILE = (0.1, 0.5, 0.9)


@dataclass
class ResponseTrainErgebnis:
    """Ergebnis eines Zone-Trainingslaufs."""
    zone_id: str
    n_events: int
    # {alpha_int: mae}  fuer Forward (z. B. {10: 4.5, 50: 3.2, 90: 4.1})
    forward_metriken: dict[int, float] = field(default_factory=dict)
    # MAE in Sekunden fuer das Inverse-Modell (q50).
    inverse_mae_s: float | None = None
    inverse_mape: float | None = None
    liter_pro_sekunde_median: float | None = None
    version: str | None = None
    status: str = "ok"   # 'ok' | 'uebersprungen' | 'fehler'
    grund: str | None = None
    feature_cols_forward: list[str] = field(default_factory=list)
    feature_cols_inverse: list[str] = field(default_factory=list)


def _walk_forward_splits(
    n: int,
    k: int = 3,
    min_train: int = 3,
    min_test: int = 1,
    zeitstempel: "pd.Series | list | None" = None,
    gap_stunden: int = 24,
):
    """Walk-Forward-CV-Indizes: chronologisch, ohne Shuffle.

    n Samples → liefert bis zu k (train_idx, test_idx)-Paare. Jedes fold
    behaelt alle bisherigen Samples im Training; wenn Zeitstempel uebergeben
    werden, startet der Testblock erst nach `gap_stunden`.
    """
    if n < min_train + min_test:
        return []
    nutzbar = n - min_train
    test_groesse = max(min_test, nutzbar // k)
    folds = []
    zeiten = (
        pd.to_datetime(zeitstempel, format="ISO8601")
        if zeitstempel is not None else None
    )
    if zeiten is not None and not zeiten.is_monotonic_increasing:
        # Wenn Zeitstempel unsortiert reinkommen, bricht der 24h-Gap-
        # Schutz still: `zeiten[train_ende - 1] + gap` zeigt nicht aufs
        # zeitliche Trainings-Ende, sondern auf einen beliebigen Punkt
        # in der Mitte → Test-Folds koennen Trainings-Zeitpunkte
        # ueberlappen → Leakage. Einziger Caller heute (`_train_lgbm_cv`)
        # sortiert vorher, aber kuenftige Aufrufer muessten das auch.
        raise ValueError(
            "_walk_forward_splits: zeitstempel muss chronologisch "
            "sortiert reinkommen (is_monotonic_increasing=False)"
        )
    gap = timedelta(hours=gap_stunden)
    for i in range(k):
        train_ende = min_train + i * test_groesse
        if train_ende >= n:
            break
        test_start = train_ende
        if zeiten is not None:
            grenze = zeiten[train_ende - 1] + gap
            kandidaten = np.where(zeiten >= grenze)[0]
            if len(kandidaten) == 0:
                break
            test_start = int(kandidaten[0])
        test_ende = min(test_start + test_groesse, n)
        if test_start >= n:
            break
        train_idx = list(range(0, train_ende))
        test_idx = list(range(test_start, test_ende))
        if not train_idx or not test_idx:
            break
        folds.append((train_idx, test_idx))
    return folds


def _mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean(np.abs(y_true - y_pred)))


def _mape(y_true: np.ndarray, y_pred: np.ndarray) -> float | None:
    mask = np.abs(y_true) > 1e-6
    if not mask.any():
        return None
    return float(np.mean(np.abs((y_true[mask] - y_pred[mask]) / y_true[mask])))


class ResponseTrainingsPipeline:
    """Pro Zone: Forward-Quantile-Modelle + Inverse-Punkt-Modell.

    Ein Pipeline-Lauf = eine Zone. Der Retrain-Job erzeugt pro Zone eine
    eigene Instanz (parallele Deployments stoeren sich nicht, weil jedes
    Modell separate Datei + separater Symlink hat).
    """

    def __init__(
        self,
        zone_id: str,
        basis_verzeichnis: str | Path,
        min_events: int = 5,
        n_folds: int = 3,
    ):
        self.zone_id = zone_id
        self._basis = Path(basis_verzeichnis)
        self._basis.mkdir(parents=True, exist_ok=True)
        self._zone_dir = self._basis / zone_id
        self._zone_dir.mkdir(parents=True, exist_ok=True)
        self._min_events = min_events
        self._n_folds = n_folds

    # --- Dataset-Aufbau ---

    @staticmethod
    def _zone_subset(df: pd.DataFrame, zone_id: str) -> pd.DataFrame:
        if df.empty:
            return df
        teil = df[df["zone_id"] == zone_id].copy()
        # nur Zeilen mit Label delta_6h — Forward und Inverse trainieren
        # beide gegen denselben Labelblock; 6h ist das Primaerlabel.
        teil = teil[teil["delta_6h"].notna()]
        teil = teil.sort_values("zeitstempel").reset_index(drop=True)
        return teil

    def lauf(self, df_alle: pd.DataFrame, jetzt: datetime | None = None) -> ResponseTrainErgebnis:
        """Haupt-Trainingszyklus: validiert, trainiert, persistiert.

        Rueckgabe: ResponseTrainErgebnis. Status='uebersprungen' wenn die
        Daten nicht ausreichen (Aufrufer = MlResponseRetrainJob muss dann
        das alte Modell lassen).
        """
        jetzt = jetzt or datetime.now()
        teil = self._zone_subset(df_alle, self.zone_id)
        n = int(len(teil))
        ergebnis = ResponseTrainErgebnis(zone_id=self.zone_id, n_events=n)
        if n < self._min_events:
            ergebnis.status = "uebersprungen"
            ergebnis.grund = f"zu_wenig_events:{n}<{self._min_events}"
            logger.warning(
                "ml.response_training.uebersprungen zone=%s n=%d min=%d",
                self.zone_id, n, self._min_events,
            )
            return ergebnis

        # Feature-Spalten in deterministischer Reihenfolge.
        forward_cols = [c for c in FORWARD_FEATURES if c in teil.columns]
        inverse_cols_no_ziel = [
            c for c in INVERSE_FEATURES if c != "ziel_delta" and c in teil.columns
        ]
        inverse_cols = ["ziel_delta"] + inverse_cols_no_ziel

        ergebnis.feature_cols_forward = list(forward_cols)
        ergebnis.feature_cols_inverse = list(inverse_cols)

        # --- Forward Quantile ---
        try:
            for alpha in FORWARD_QUANTILE:
                alpha_int = int(round(alpha * 100))
                mae_alpha = self._trainiere_quantile(
                    teil, forward_cols, "delta_6h", alpha, alpha_int,
                )
                if mae_alpha is not None:
                    ergebnis.forward_metriken[alpha_int] = round(mae_alpha, 3)
        except Exception as exc:
            logger.exception(
                "ml.response_training.forward_fehler zone=%s", self.zone_id,
            )
            ergebnis.status = "fehler"
            ergebnis.grund = f"forward:{type(exc).__name__}:{exc}"
            return ergebnis

        # --- Inverse (Punkt) ---
        # Training-Label: ziel_delta := delta_6h_ist (selbe Row). Damit
        # lernt das Modell: "welche Dauer hat Delta X historisch produziert".
        teil_inv = teil.copy()
        teil_inv["ziel_delta"] = teil_inv["delta_6h"]
        try:
            mae_inv, mape_inv = self._trainiere_inverse(teil_inv, inverse_cols)
            ergebnis.inverse_mae_s = round(mae_inv, 1) if mae_inv is not None else None
            ergebnis.inverse_mape = round(mape_inv, 3) if mape_inv is not None else None
        except Exception as exc:
            logger.exception(
                "ml.response_training.inverse_fehler zone=%s", self.zone_id,
            )
            ergebnis.status = "fehler"
            ergebnis.grund = f"inverse:{type(exc).__name__}:{exc}"
            return ergebnis

        if ergebnis.inverse_mae_s is None:
            self._loesche_staging()
            ergebnis.status = "uebersprungen"
            ergebnis.grund = "keine_cv_folds"
            return ergebnis

        # Zonen-Median der Liter/s (fuer Inferenz-Default, wenn Event-Rate
        # unbekannt ist).
        if "liter_pro_sekunde" in teil.columns and not teil["liter_pro_sekunde"].empty:
            ergebnis.liter_pro_sekunde_median = float(
                teil["liter_pro_sekunde"].median()
            )

        # Version-Verzeichnis setzen (Symlinks zeigen nachher hierher).
        version = jetzt.strftime("v%Y%m%d_%H%M%S")
        ergebnis.version = version
        (self._zone_dir / version).mkdir(parents=True, exist_ok=True)

        # Metadaten schreiben + Symlinks pflegen.
        self._schreibe_metadata(ergebnis, jetzt)
        self._aktualisiere_symlinks(version, ergebnis)
        return ergebnis

    # --- Modell-Training ---

    def _fit_lightgbm(
        self,
        df: pd.DataFrame,
        feature_cols: list[str],
        ziel_col: str,
        alpha: float | None,
        monotonie: dict[str, int],
    ) -> tuple["lgb.Booster | None", float | None]:
        """Trainiert ein Modell mit Walk-Forward-CV.

        Der finale Booster wird am Schluss auf ALLEN Daten trainiert (CV
        dient nur der MAE-Schaetzung). `alpha` None = Regression, sonst
        Quantile. Rueckgabe: (booster, mae_cv).
        """
        params = {**STANDARD_PARAMS}
        if alpha is not None:
            params["objective"] = "quantile"
            params["alpha"] = float(alpha)
            # LightGBM erlaubt (in dieser Version) keine monotone_constraints
            # beim quantile-Objective. Forward ist ohnehin Shadow-Benchmark;
            # strikte Monotonie braucht nur das Inverse (Produktiv).
        else:
            params["monotone_constraints"] = monotone_vektor(feature_cols, monotonie)

        X = df[feature_cols].astype(float).values
        y = df[ziel_col].astype(float).values

        # Walk-Forward-CV fuer MAE-Schaetzung.
        cv_maes: list[float] = []
        splits = _walk_forward_splits(
            len(df), k=self._n_folds, min_train=max(3, self._min_events - 1),
            zeitstempel=df["zeitstempel"], gap_stunden=24,
        )
        if not splits:
            logger.warning(
                "ml.response_training.keine_cv_folds zone=%s n=%d ziel=%s",
                self.zone_id, len(df), ziel_col,
            )
            return None, None

        for train_idx, test_idx in splits:
            Xtr, ytr = X[train_idx], y[train_idx]
            Xte, yte = X[test_idx], y[test_idx]
            booster = lgb.train(
                params,
                lgb.Dataset(Xtr, label=ytr),
                num_boost_round=min(STANDARD_N_ESTIMATORS, 100),
            )
            pred = booster.predict(Xte)
            cv_maes.append(_mae(np.asarray(yte), np.asarray(pred)))
        mae_cv = float(np.mean(cv_maes)) if cv_maes else None

        # Finales Training auf allen Daten.
        final_booster = lgb.train(
            params,
            lgb.Dataset(X, label=y),
            num_boost_round=STANDARD_N_ESTIMATORS,
        )
        return final_booster, mae_cv

    def _trainiere_quantile(
        self,
        df: pd.DataFrame,
        feature_cols: list[str],
        ziel_col: str,
        alpha: float,
        alpha_int: int,
    ) -> float | None:
        booster, mae_cv = self._fit_lightgbm(
            df, feature_cols, ziel_col, alpha=alpha,
            monotonie=FORWARD_MONOTONIE,
        )
        if booster is None:
            return None
        # Speichern im zone_dir (Version-Verzeichnis kommt erst am Ende —
        # wir speichern zwischendurch mit einem Staging-Namen und bewegen
        # ihn danach. Einfacher: direkt mit datum-basiertem Namen.)
        staging = self._zone_dir / f"_staging_forward_q{alpha_int}.lgbm"
        booster.save_model(str(staging))
        return mae_cv

    def _trainiere_inverse(
        self,
        df: pd.DataFrame,
        feature_cols: list[str],
    ) -> tuple[float | None, float | None]:
        booster, mae_cv = self._fit_lightgbm(
            df, feature_cols, "dauer_s", alpha=None,
            monotonie=INVERSE_MONOTONIE,
        )
        if booster is None:
            return None, None
        staging = self._zone_dir / "_staging_inverse.lgbm"
        booster.save_model(str(staging))
        # MAPE ueber in-sample (nur diagnostisch — MAE_cv ist Gate-Groesse).
        X = df[feature_cols].astype(float).values
        y = df["dauer_s"].astype(float).values
        mape = _mape(np.asarray(y), np.asarray(booster.predict(X)))
        return mae_cv, mape

    # --- Persistenz ---

    def _schreibe_metadata(
        self, ergebnis: ResponseTrainErgebnis, jetzt: datetime,
    ) -> None:
        version_dir = self._zone_dir / (ergebnis.version or "unbekannt")
        version_dir.mkdir(parents=True, exist_ok=True)
        daten = {
            "zone_id": ergebnis.zone_id,
            "version": ergebnis.version,
            "trainiert_am": jetzt.isoformat(),
            "n_events": ergebnis.n_events,
            "forward_metriken": ergebnis.forward_metriken,
            "inverse_mae_s": ergebnis.inverse_mae_s,
            "inverse_mape": ergebnis.inverse_mape,
            "liter_pro_sekunde_median": ergebnis.liter_pro_sekunde_median,
            "feature_cols_forward": ergebnis.feature_cols_forward,
            "feature_cols_inverse": ergebnis.feature_cols_inverse,
            "status": ergebnis.status,
            "grund": ergebnis.grund,
        }
        (version_dir / "metadata.json").write_text(
            json.dumps(daten, indent=2, default=str)
        )

    def _aktualisiere_symlinks(
        self, version: str, ergebnis: ResponseTrainErgebnis,
    ) -> None:
        """Verschiebt die Staging-Modelle nach <zone_dir>/<version>/ und
        legt die `aktuell_*`-Symlinks auf die neuen Ziele.
        """
        version_dir = self._zone_dir / version
        # Forward-Quantile verschieben.
        for alpha_int in sorted(ergebnis.forward_metriken):
            staging = self._zone_dir / f"_staging_forward_q{alpha_int}.lgbm"
            ziel = version_dir / f"forward_q{alpha_int}.lgbm"
            if not staging.exists():
                continue
            staging.replace(ziel)
            link = self._zone_dir / f"aktuell_{self.zone_id}_forward_q{alpha_int}.lgbm"
            self._setze_symlink(link, ziel)
        # Inverse.
        staging_inv = self._zone_dir / "_staging_inverse.lgbm"
        if staging_inv.exists():
            ziel_inv = version_dir / "inverse.lgbm"
            staging_inv.replace(ziel_inv)
            link = self._zone_dir / f"aktuell_{self.zone_id}_inverse.lgbm"
            self._setze_symlink(link, ziel_inv)
        # Metadaten-Symlink.
        meta_ziel = version_dir / "metadata.json"
        if meta_ziel.exists():
            meta_link = self._zone_dir / f"aktuell_{self.zone_id}_metadata.json"
            self._setze_symlink(meta_link, meta_ziel)
        logger.info(
            "ml.response_training.deploy zone=%s version=%s "
            "forward=%s inverse_mae_s=%s",
            self.zone_id, version,
            ergebnis.forward_metriken, ergebnis.inverse_mae_s,
        )

    def _loesche_staging(self) -> None:
        """Entfernt halb erzeugte Staging-Modelle bei nicht deploybarem Lauf."""
        for pfad in self._zone_dir.glob("_staging_*.lgbm"):
            try:
                pfad.unlink()
            except OSError:
                logger.exception(
                    "ml.response_training.staging_cleanup_fehler pfad=%s",
                    pfad,
                )

    @staticmethod
    def _setze_symlink(link: Path, ziel: Path) -> None:
        if link.exists() or link.is_symlink():
            link.unlink()
        # Relativer Pfad zum Ziel, damit das Verzeichnis kopierbar bleibt.
        try:
            rel = ziel.relative_to(link.parent)
        except ValueError:
            rel = ziel
        link.symlink_to(rel)
