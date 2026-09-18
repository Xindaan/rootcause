"""Pydantic-Modelle fuer ML-Vorhersagen und Trainingsmetriken."""

from datetime import datetime
from pydantic import BaseModel


class FeatureBeitrag(BaseModel):
    """T-0040: Ein einzelner SHAP-Feature-Beitrag (LightGBM pred_contrib).

    `wert` ist der tatsaechliche Feature-Wert der Zeile (zur Anzeige),
    `beitrag` die Verschiebung des Prognosewerts (Prozentpunkte), die
    LightGBM diesem Feature zuschreibt. `skala` sagt, ob der Beitrag eine
    absolute Feuchte-Prognose oder ein Delta zur aktuellen Feuchte erklaert.
    """
    name: str
    wert: float | None = None
    beitrag: float
    skala: str = "absolut"


class MLVorhersage(BaseModel):
    """Einzelne ML-Vorhersage fuer eine Zone."""
    zone_id: str
    zeitstempel: datetime              # Wann die Vorhersage erstellt wurde
    horizont_stunden: int              # 6, 12 oder 24
    feuchte_aktuell: float             # Aktuelle Feuchte (Eingabe)
    feuchte_prognose: float            # Vorhergesagte Feuchte (q50 bei Quantile)
    konfidenz: float | None = None     # Legacy-Feld, nicht mehr gesetzt
    # T-0046: Quantile-Baender (None wenn Punkt-Modell geladen)
    q10: float | None = None
    q90: float | None = None
    # T-0040: Top-N Feature-Beitraege (nur gesetzt bei `details=top_features`)
    top_features: list[FeatureBeitrag] | None = None
    # T-0573: Herkunft der Prognose. `zeitstempel` sagt nur, wann GERECHNET
    # wurde -- das ist bei jedem Poll jetzt. Die fachlich interessante Frage
    # ist, auf WELCHER Zeile gerechnet wurde: liegt die im Ausschlussfenster
    # oder davor, rechnet die Pipeline endlos auf einem eingefrorenen Stand
    # weiter, ohne dass die Karte es erfaehrt (Maxibaer 08./09.09.).
    feature_zeitstempel: datetime | None = None
    # T-0573/T-0567: Sensor, auf dessen Zeile gerechnet wurde. Erlaubt der
    # Anzeige zu pruefen, ob unter dieser `geraet_id` inzwischen ein
    # anderes physisches Geraet steckt.
    inferenz_geraet_id: str | None = None
    # T-0577: das Guete-Urteil, EINMAL berechnet in `live_vorhersage` und von
    # allen Konsumenten nur gelesen (API und Entscheidungs-Engine). Vorher
    # rechnete nur die API es aus -- die Karte zeigte keine veraltete Zahl
    # mehr, die Engine rechnete mit genau derselben Zahl Trigger und Dosis.
    #
    # Default `True` nur fuer Modell-Instanzen, die nicht aus dem Service
    # kommen (Tests, Altdaten). `live_vorhersage` setzt es IMMER explizit.
    gueltig: bool = True
    ungueltig_grund: str | None = None
    feature_alter_h: float | None = None
    feature_rueckstand_h: float | None = None
    # T-0610 ML-6: das Modell, das diese Zone rechnet, hat auf diesem
    # Horizont im Training die Persistenz ("Feuchte bleibt gleich") nicht
    # geschlagen. Reine Warnung: `gueltig` bleibt unberuehrt, die Engine
    # rechnet weiter mit der Zahl (Entscheid Andre 13.10., kein Rueckfall).
    persistenz_warnung: bool = False


# T-0610 ML-7: EINE Quelle fuer die Feature-Schema-Version des Feuchte-Modells.
# Das Training schreibt sie in jede metriken_*.json, die Inferenz warnt bei
# aelteren Modellen, und das Retrain-Gate bewertet einen Amtsinhaber mit
# aelterer Version immer fair auf den neu gebauten Testzeilen; geht das
# nicht, bleibt er (kein blinder Austausch, Entscheid Andre 14.10.2026).
# Vorher standen 2 als Literal an zwei Stellen.
# Historie: 1 = vor T-0076b (Feld fehlt), 2 = T-0076b,
# 3 = T-0610 ML-7 (eigener Ventil-Rueckblick, Training und Live gleich).
FEATURE_SCHEMA_VERSION = 3


class TrainingsMetriken(BaseModel):
    """Ergebnis eines Trainingslaufs."""
    zeitstempel: datetime
    horizont_stunden: int
    anzahl_samples: int
    anzahl_features: int
    # Gesamt-Metriken (auf absoluter Skala, auch bei Delta-Training)
    mae: float                         # Mean Absolute Error
    rmse: float                        # Root Mean Squared Error
    r2: float                          # R-Squared
    # Baseline-Vergleich
    baseline_mae: float
    baseline_rmse: float
    baseline_r2: float
    # Quantile-spezifische Kalibrierungsmetriken. `mae` bleibt auch bei
    # Quantile-Modellen absolute MAE, damit Retrain-Gates skalenstabil sind.
    pinball_loss: float | None = None
    baseline_pinball_loss: float | None = None
    feature_schema_version: int = 1
    # Top Features
    feature_importances: dict[str, float] = {}
    # Pro-Zone aufgeschluesselt (optional)
    mae_pro_zone: dict[str, float] = {}
    # T-0062a: Flag ob das Modell auf Delta-Ziel trainiert wurde
    # (ziel_delta_Xh statt ziel_feuchte_Xh). Bei True muss die Inferenz
    # `boden_feuchte_aktuell + modell.predict(X)` rechnen.
    # Default False fuer Backward-Kompat (alte Modelle ohne Flag sind
    # absolut trainiert).
    delta_ziel: bool = False
    monotone_constraints: bool = False
    monotone_features: list[str] = []
    regen_slice_mae: float | None = None
    regen_slice_n: int = 0
    # T-0610 ML-6: zweite Baseline "Feuchte bleibt gleich" (Persistenz),
    # gleiche CV-Folds und gleiche absolute Skala wie `mae`. None bei
    # Modellen, die vor dieser Aenderung trainiert wurden.
    persistenz_mae: float | None = None
    persistenz_mae_pro_zone: dict[str, float] = {}

    def schlaegt_persistenz(self) -> bool | None:
        """T-0610 ML-6: True, wenn das Modell die Persistenz schlaegt.

        None = nicht bewertbar (Altmodell ohne `persistenz_mae`). Gleichstand
        zaehlt als NICHT geschlagen: dann fuegt das Modell nichts hinzu.
        Einzige Stelle fuer diesen Vergleich (Training-Log, Status, Guete).
        """
        if self.persistenz_mae is None:
            return None
        return self.mae < self.persistenz_mae


class PersistenzWarnung(BaseModel):
    """T-0610 ML-6: ein geladenes Modell schlaegt auf einem Horizont die
    Persistenz nicht. Nur Anzeige; die Engine faellt deshalb NICHT zurueck."""
    quelle: str                        # "cluster:<id>" oder "legacy"
    horizont_stunden: int
    mae: float
    persistenz_mae: float


class ModellStatus(BaseModel):
    """Status des aktuell geladenen Modells."""
    modell_pfad: str | None = None
    trainiert_am: datetime | None = None
    horizonte: list[int] = []
    metriken: list[TrainingsMetriken] = []
    ist_geladen: bool = False
    letzte_missing_feature_quote: float = 0.0  # F7: 0=alles da, >0.3=Ampel rot
    # T-0101: Pro-Cluster-Modelle (T-0082) sichtbar machen.
    # Mapping cluster_id -> sortierte Liste der dort geladenen Horizonte.
    # Leer wenn nur Legacy-Wurzel-Modelle aktiv sind.
    cluster_horizonte: dict[str, list[int]] = {}
    # T-0610 ML-6: Modelle/Horizonte, die die Persistenz nicht schlagen.
    # Leer = keine Warnung ODER nur Altmodelle ohne Persistenz-Metrik.
    persistenz_warnungen: list[PersistenzWarnung] = []
