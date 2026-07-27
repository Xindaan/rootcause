"""Evaluation und Baseline-Vergleich fuer ML-Modelle.

Metriken: MAE, RMSE, R², Schwellen-Praezision/-Recall.
Baseline: Regelbasierte Vorhersage (letzte Feuchte + Trend, analog entscheidung.py).
"""


try:
    import numpy as np
    import pandas as pd
except ImportError:
    raise ImportError(
        "ML-Abhaengigkeiten fehlen. Installiere mit: pip install -e '.[ml]'"
    )


def mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Mean Absolute Error."""
    mask = ~(np.isnan(y_true) | np.isnan(y_pred))
    if mask.sum() == 0:
        return float("nan")
    return float(np.mean(np.abs(y_true[mask] - y_pred[mask])))


def rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Root Mean Squared Error."""
    mask = ~(np.isnan(y_true) | np.isnan(y_pred))
    if mask.sum() == 0:
        return float("nan")
    return float(np.sqrt(np.mean((y_true[mask] - y_pred[mask]) ** 2)))


def r2(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """R-Squared (Bestimmtheitsmass)."""
    mask = ~(np.isnan(y_true) | np.isnan(y_pred))
    if mask.sum() < 2:
        return float("nan")
    y_t = y_true[mask]
    y_p = y_pred[mask]
    ss_res = np.sum((y_t - y_p) ** 2)
    ss_tot = np.sum((y_t - np.mean(y_t)) ** 2)
    if ss_tot < 1e-10:
        return 0.0
    return float(1 - ss_res / ss_tot)


def pinball_loss(
    y_true: np.ndarray, y_pred: np.ndarray, alpha: float,
) -> float:
    """Pinball-Loss (Quantile Loss) fuer Quantile-Regression.

    Gibt den mittleren Verlust fuer Quantil alpha. Bei `alpha=0.5` ist
    pinball_loss = 0.5 * MAE. Niedriger = besser kalibriertes Quantil.
    """
    mask = ~(np.isnan(y_true) | np.isnan(y_pred))
    if mask.sum() == 0:
        return float("nan")
    diff = y_true[mask] - y_pred[mask]
    return float(np.mean(np.maximum(alpha * diff, (alpha - 1) * diff)))


def abdeckung_intervall(
    y_true: np.ndarray, y_unten: np.ndarray, y_oben: np.ndarray,
) -> float:
    """Anteil der echten Werte, die im [unten, oben]-Intervall liegen.

    Fuer q10/q90-Intervall sollte dieser Wert um 0.80 liegen, wenn die
    Quantile gut kalibriert sind. Abweichung zeigt Mis-Kalibrierung.
    """
    mask = ~(np.isnan(y_true) | np.isnan(y_unten) | np.isnan(y_oben))
    if mask.sum() == 0:
        return float("nan")
    innerhalb = (y_true[mask] >= y_unten[mask]) & (y_true[mask] <= y_oben[mask])
    return float(np.mean(innerhalb))


def schwellen_metriken(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    feuchte_aktuell: np.ndarray,
    schwelle: np.ndarray,
) -> dict:
    """Schwellen-basierte Evaluation (operativ entscheidend).

    Berechnet Praezision und Recall fuer die Vorhersage
    'Feuchte wird unter Schwelle fallen'.

    Args:
        y_true: Tatsaechliche Feuchte zum Horizont-Zeitpunkt
        y_pred: Vorhergesagte Feuchte
        feuchte_aktuell: Aktuelle Feuchte (zum Vorhersage-Zeitpunkt)
        schwelle: Schwellenwert pro Zeile (feuchte_schwelle_min)
    """
    mask = ~(np.isnan(y_true) | np.isnan(y_pred) | np.isnan(schwelle))
    if mask.sum() == 0:
        return {
            "schwellen_praezision": float("nan"),
            "schwellen_recall": float("nan"),
            "schwellen_f1": float("nan"),
        }

    y_t = y_true[mask]
    y_p = y_pred[mask]
    s = schwelle[mask]

    # Tatsaechlich unter Schwelle gefallen?
    wahr_unter = y_t < s
    # Vorhersage: unter Schwelle?
    pred_unter = y_p < s

    # Praezision: von allen Alarmen, wie viele stimmen?
    true_positive = np.sum(pred_unter & wahr_unter)
    false_positive = np.sum(pred_unter & ~wahr_unter)
    praezision = (
        float(true_positive / (true_positive + false_positive))
        if (true_positive + false_positive) > 0
        else float("nan")
    )

    # Recall: von allen echten Unterschreitungen, wie viele erkannt?
    false_negative = np.sum(~pred_unter & wahr_unter)
    recall = (
        float(true_positive / (true_positive + false_negative))
        if (true_positive + false_negative) > 0
        else float("nan")
    )

    # F1
    if praezision > 0 and recall > 0:
        f1 = 2 * praezision * recall / (praezision + recall)
    else:
        f1 = 0.0

    return {
        "schwellen_praezision": round(praezision, 4),
        "schwellen_recall": round(recall, 4),
        "schwellen_f1": round(f1, 4),
    }


class BaselineVorhersage:
    """Regelbasierte Baseline (analog entscheidung.py).

    Vorhersage = aktuelle_feuchte + trend_6h * horizont
                 + niederschlag * REGEN_FAKTOR
                 - et0 * ET0_FAKTOR

    Dient als Vergleichsmassstab fuer das ML-Modell.
    """

    REGEN_FEUCHTE_FAKTOR = 4.0  # Aus entscheidung.py
    ET0_FEUCHTE_FAKTOR = 2.0    # Aus entscheidung.py

    def vorhersage(self, df: pd.DataFrame, horizont: int) -> np.ndarray:
        """Berechnet Baseline-Vorhersage fuer einen Horizont.

        Args:
            df: Feature-DataFrame (muss boden_feuchte_aktuell, feuchte_trend_6h,
                niederschlag_summe_Xh, et0_summe_Xh enthalten)
            horizont: Vorhersage-Horizont in Stunden (6, 12, 24)

        Returns:
            Array mit vorhergesagten Feuchte-Werten
        """
        feuchte = df["boden_feuchte_aktuell"].values.copy()
        trend = df["feuchte_trend_6h"].fillna(0).values

        # Trend extrapolieren
        vorhersage = feuchte + trend * horizont

        # Wetter-Korrektur (wenn vorhanden)
        nieder_col = f"niederschlag_summe_{horizont}h"
        et0_col = f"et0_summe_{horizont}h"

        if nieder_col in df.columns:
            niederschlag = pd.to_numeric(df[nieder_col], errors="coerce").fillna(0).values
            vorhersage += niederschlag * self.REGEN_FEUCHTE_FAKTOR

        if et0_col in df.columns:
            et0 = pd.to_numeric(df[et0_col], errors="coerce").fillna(0).values
            vorhersage -= et0 * self.ET0_FEUCHTE_FAKTOR

        # Clamp auf 0–100
        vorhersage = np.clip(vorhersage, 0, 100)
        return vorhersage


def evaluiere_baseline(
    df: pd.DataFrame,
    horizonte: list[int] | None = None,
) -> dict:
    """Evaluiert die regelbasierte Baseline auf dem Feature-DataFrame.

    Returns:
        Dict mit Metriken pro Horizont und gesamt.
    """
    if horizonte is None:
        horizonte = [6, 12, 24]

    baseline = BaselineVorhersage()
    ergebnisse = {}

    for h in horizonte:
        ziel_col = f"ziel_feuchte_{h}h"
        if ziel_col not in df.columns:
            continue

        # Nur Zeilen mit gueltigem Ziel
        mask = df[ziel_col].notna()
        if mask.sum() == 0:
            continue

        df_valid = df[mask]
        y_true = df_valid[ziel_col].values
        y_pred = baseline.vorhersage(df_valid, h)

        metriken = {
            "mae": round(mae(y_true, y_pred), 3),
            "rmse": round(rmse(y_true, y_pred), 3),
            "r2": round(r2(y_true, y_pred), 4),
            "n_samples": int(mask.sum()),
        }

        # Schwellen-Metriken wenn verfuegbar
        if "feuchte_schwelle_min" in df_valid.columns:
            metriken.update(schwellen_metriken(
                y_true, y_pred,
                df_valid["boden_feuchte_aktuell"].values,
                df_valid["feuchte_schwelle_min"].values,
            ))

        # Pro Zone
        mae_pro_zone = {}
        for zone_id in df_valid["zone_id"].unique():
            zone_mask = df_valid["zone_id"] == zone_id
            if zone_mask.sum() < 2:
                continue
            y_z = df_valid.loc[zone_mask, ziel_col].values
            p_z = y_pred[zone_mask.values]
            mae_pro_zone[zone_id] = round(mae(y_z, p_z), 3)

        metriken["mae_pro_zone"] = mae_pro_zone
        ergebnisse[f"{h}h"] = metriken

    return ergebnisse
