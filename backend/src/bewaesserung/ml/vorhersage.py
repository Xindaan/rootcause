"""ML-Vorhersage-Service fuer Live-Inferenz.

Laedt trainierte LightGBM-Modelle und liefert Feuchte-Prognosen.
Graceful Degradation: Wenn kein Modell vorhanden, wird None zurueckgegeben.
"""

import asyncio
import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from bewaesserung.ml.modelle_ml import (
    FeatureBeitrag,
    MLVorhersage,
    ModellStatus,
    TrainingsMetriken,
)

if TYPE_CHECKING:
    from bewaesserung.konfig import GesamtKonfig
    from bewaesserung.speicher import Speicher


@dataclass
class _ClusterCache:
    """T-0082: Booster + Metadaten pro Cluster.

    Heutiges Verhalten lebt komplett in einem `_legacy_global`-Cluster.
    Pro-Cluster-Modelle aus `feuchte/<cluster>/` werden parallel als
    eigene `_ClusterCache`-Instanzen gehalten. Die Felder spiegeln 1:1
    die ehemals direkten Service-Felder.
    """
    modelle: dict = field(default_factory=dict)
    metriken: dict = field(default_factory=dict)
    feature_cols: dict = field(default_factory=dict)
    quantile_modelle: dict = field(default_factory=dict)
    quantile_feature_cols: dict = field(default_factory=dict)
    delta_ziel: dict = field(default_factory=dict)
    quantile_delta_ziel: dict = field(default_factory=dict)
    band_scale: dict = field(default_factory=dict)
    # Pfad des Cluster-Verzeichnisses (fuer Status / Debug). Bei Legacy
    # = Wurzel-Verzeichnis; bei Pro-Cluster = `<base>/feuchte/<cluster>/`.
    verzeichnis: Path | None = None

try:
    import lightgbm as lgb
    import numpy as np
    import pandas as pd
except ImportError:
    lgb = None  # type: ignore
    np = None   # type: ignore
    pd = None   # type: ignore

logger = logging.getLogger(__name__)

KATEGORISCHE_FEATURES = {"quelle", "zone_kategorie", "sensor_quelle"}


class MLVorhersageService:
    """Laedt Modelle und liefert Live-Vorhersagen.

    Thread-safe: Modelle werden einmal geladen und sind read-only.

    Quantile-Modelle (T-0046) werden zusaetzlich geladen, wenn
    `aktuell_Xh_q10.lgbm` / `aktuell_Xh_q90.lgbm` existieren. Das Punkt-
    Modell `aktuell_Xh.lgbm` bleibt als q50/Default erhalten — Inferenz
    liefert dann alle drei Werte.
    """

    HORIZONTE = [6, 12, 24]
    QUANTILE_ALPHAS = (10, 90)

    # T-0076b: aktuelle Schema-Version, die der Code erwartet. Modelle
    # mit aelterer `feature_schema_version` werden beim Laden mit Warnung
    # markiert — sie sind nicht zwingend ungueltig (Lader fuegt fehlende
    # Features als NaN ein, LightGBM toleriert das), aber Drift-Auswertung
    # und Auto-Retrain-Vergleich werden ungenau. Bei einem echten
    # Schema-Bruch (z. B. neues Pflicht-Feature, anderes Encoding fuer
    # `zone_kategorie`) muss diese Version inkrementiert + die Bedingung
    # in `_pruefe_feature_schema` verschaerft werden, damit alte Modelle
    # explizit als "nicht ladbar" markiert sind.
    AKTUELLE_FEATURE_SCHEMA_VERSION = 2

    # T-0082: Default-Cluster-Schluessel fuer Backward-Compat. Solange
    # keine `feuchte/<cluster>/`-Subdirs existieren, lebt das gesamte
    # alte Verhalten in diesem einen Cluster.
    LEGACY_CLUSTER_ID = "_legacy_global"

    def __init__(self, modell_verzeichnis: str = ""):
        if not modell_verzeichnis:
            from bewaesserung.konfig import ML_DATEN_PFAD
            modell_verzeichnis = ML_DATEN_PFAD
        self._modell_dir = Path(modell_verzeichnis)
        # Heutige Felder = Legacy-Cluster-Stand. T-0082 fuegt parallel
        # `_cluster_caches` fuer Pro-Cluster-Modelle hinzu — heutiger
        # Inferenz-Pfad bleibt auf diesen Feldern und ist damit unveraendert.
        self._modelle: dict[int, "lgb.Booster"] = {}
        self._metriken: dict[int, TrainingsMetriken] = {}
        self._feature_cols: dict[int, list[str]] = {}
        # T-0046: {(horizont, alpha_int): Booster}
        self._quantile_modelle: dict[tuple[int, int], "lgb.Booster"] = {}
        self._quantile_feature_cols: dict[tuple[int, int], list[str]] = {}
        # T-0062a: Pro Modell merken ob Delta-Ziel oder absolutes Ziel
        # trainiert wurde. Bei True rechnet vorhersage() `aktuell + predict`.
        # Key Format identisch mit _modelle / _quantile_modelle.
        self._delta_ziel: dict[int, bool] = {}
        self._quantile_delta_ziel: dict[tuple[int, int], bool] = {}
        # T-0062d: Band-Scale-Faktor pro Horizont (aus band_scale_{h}h.json).
        # Scale > 1 = Band breiter als Roh-Quantile, < 1 = enger.
        # Default 1.0 bei Fehlen der Datei (keine Kalibrierung → Roh).
        self._band_scale: dict[int, float] = {}
        # T-0082: Pro-Cluster-Booster-Sets aus `feuchte/<cluster>/`. Wird
        # in `lade_modelle()` zusaetzlich zum Legacy-Pfad bevoelkert. Wenn
        # eine Zone via `_zone_zu_cluster` einem Cluster zugeordnet ist
        # UND dieser Cluster geladen ist, nutzt `live_vorhersage()` das
        # Cluster-Set; sonst Legacy-Fallback.
        self._cluster_caches: dict[str, "_ClusterCache"] = {}
        self._zone_zu_cluster: dict[str, str] = {}
        # F7: letzte Missing-Feature-Quote fuer /api/ml/status
        self._letzte_missing_quote: float = 0.0
        # Live-Vorhersage-Cache gegen Event-Loop-Blockade (2026-04-21):
        # `live_vorhersage` baut pro Call den 48-h-Feature-DataFrame fuer ALLE
        # Zonen, filtert dann eine heraus. Bei 11 parallelen Frontend-Requests
        # (KritischBand + HeutePlan + Zonen-Karten) waren das 11× synchrones
        # Feature-Engineering im Event-Loop — alle anderen API-Calls (z. B.
        # /api/zonen/.../messwerte) hungerten aus. Cache-Key: (zone_id, details).
        # TTL 45 s (< Frontend-Poll-Interval 60 s, damit erstes Frontend-Poll
        # nach Cache-Expire ohne Stale-Gefuehl dursch kommt).
        self._live_cache: dict[tuple[str, bool], tuple[datetime, dict]] = {}
        self._live_cache_ttl_s: int = 45
        # T-0314 Root-Fix: gemeinsamer Feature-DF-Cache. live_vorhersage
        # (pro Zone) und live_vorhersage_bulk bauen denselben 48h-DF ueber
        # ALLE Zonen -> ohne gemeinsamen Cache N Rebuilds pro Snapshot/
        # Entscheidungszyklus (gemessen: Snapshot details=true 15x, Loop 5x;
        # 1 Build ~3 s). Single-Flight-Lock, damit parallele Cache-Misses
        # nicht gleichzeitig bauen.
        self._df_cache: tuple[datetime, "pd.DataFrame"] | None = None
        self._df_cache_ttl_s: int = 45
        self._df_cache_lock = asyncio.Lock()

    def setze_cluster_zuordnung(self, zone_zu_cluster: dict[str, str]) -> None:
        """T-0082: Mapping `zone_id → cluster_id` fuer Inferenz-Routing.

        Wird beim Service-Start aufgerufen, sobald die Konfig-Zonen
        geladen sind. Default-Mapping: `cluster_id = zone.cluster_id or
        zone.zone_id` (eines pro Sensor). Override moeglich, wenn zwei
        Sensoren ein gemeinsames Modell teilen sollen.
        """
        self._zone_zu_cluster = dict(zone_zu_cluster)

    def _cluster_fuer_zone(self, zone_id: str) -> str | None:
        """Resolved zone_id → cluster_id; None wenn Zone unbekannt oder
        Cluster nicht geladen (Inferenz faellt dann auf Legacy zurueck).

        Defensive Lookup — Tests instanziieren den Service via
        `__new__` ohne `__init__`, daher kann `_zone_zu_cluster` /
        `_cluster_caches` fehlen.
        """
        zone_zu_cluster = getattr(self, "_zone_zu_cluster", None) or {}
        cluster_caches = getattr(self, "_cluster_caches", None) or {}
        cid = zone_zu_cluster.get(zone_id)
        if cid and cid in cluster_caches:
            return cid
        return None

    def lade_modelle(self) -> bool:
        """Laedt alle verfuegbaren Modelle (aktuell_Xh.lgbm Symlinks).

        Returns:
            True wenn mindestens ein Modell geladen wurde.
        """
        if lgb is None:
            logger.warning("LightGBM nicht installiert — ML deaktiviert")
            return False

        geladen = 0
        for h in self.HORIZONTE:
            modell_pfad = self._modell_dir / f"aktuell_{h}h.lgbm"
            if not modell_pfad.exists():
                continue

            try:
                self._modelle[h] = lgb.Booster(model_file=str(modell_pfad))
                # Feature-Namen aus Modell extrahieren
                self._feature_cols[h] = self._modelle[h].feature_name()
                geladen += 1
                logger.info(f"ML-Modell geladen: {h}h ({modell_pfad})")

                # Metriken laden falls vorhanden
                # Symlink aufloesen um Datums-Dateinamen zu finden
                tatsaechlicher_name = modell_pfad.resolve().stem
                metriken_pfad = self._modell_dir / f"{tatsaechlicher_name.replace('modell_', 'metriken_')}.json"
                if metriken_pfad.exists():
                    with open(metriken_pfad) as f:
                        metriken = TrainingsMetriken(**json.load(f))
                    self._metriken[h] = metriken
                    # T-0062a: Delta-Flag aus Metriken uebernehmen
                    self._delta_ziel[h] = bool(
                        getattr(metriken, "delta_ziel", False)
                    )
                    if self._delta_ziel[h]:
                        logger.info(f"  Modell {h}h ist auf Delta-Ziel trainiert")
                    # T-0076b: Schema-Version pruefen
                    self._pruefe_feature_schema(metriken, f"{h}h")

            except Exception as e:
                logger.error(f"Fehler beim Laden von {modell_pfad}: {e}")

        # T-0062d: Band-Scale-Faktor pro Horizont laden (optional).
        for h in self.HORIZONTE:
            scale_pfad = self._modell_dir / f"band_scale_{h}h.json"
            if not scale_pfad.exists():
                continue
            try:
                with open(scale_pfad) as f:
                    scale_data = json.load(f)
                self._band_scale[h] = float(scale_data.get("scale", 1.0))
                if abs(self._band_scale[h] - 1.0) > 0.01:
                    logger.info(
                        f"Band-Scale geladen: {h}h scale={self._band_scale[h]:.2f} "
                        f"(Abdeckung roh {scale_data.get('abdeckung_roh', '?')} "
                        f"→ nach Scale {scale_data.get('abdeckung_nach_scale', '?')})"
                    )
            except Exception as e:
                logger.warning(f"Band-Scale-Lesefehler {scale_pfad}: {e}")

        # T-0046: Quantile-Modelle zusaetzlich laden (nur wenn vorhanden).
        for h in self.HORIZONTE:
            for alpha_int in self.QUANTILE_ALPHAS:
                pfad = self._modell_dir / f"aktuell_{h}h_q{alpha_int}.lgbm"
                if not pfad.exists():
                    continue
                try:
                    booster = lgb.Booster(model_file=str(pfad))
                    self._quantile_modelle[(h, alpha_int)] = booster
                    self._quantile_feature_cols[(h, alpha_int)] = booster.feature_name()
                    # T-0062a: Delta-Flag pro Quantile-Modell
                    metriken_pfad = self._modell_dir / f"{pfad.resolve().stem.replace('modell_', 'metriken_')}.json"
                    if metriken_pfad.exists():
                        with open(metriken_pfad) as f:
                            q_metriken = TrainingsMetriken(**json.load(f))
                        self._quantile_delta_ziel[(h, alpha_int)] = bool(
                            getattr(q_metriken, "delta_ziel", False)
                        )
                    else:
                        self._quantile_delta_ziel[(h, alpha_int)] = False
                    logger.info(f"Quantile-Modell geladen: {h}h q{alpha_int}")
                except Exception as e:
                    logger.error(f"Fehler beim Laden von {pfad}: {e}")

        if geladen > 0:
            logger.info(
                f"{geladen} Punkt-Modell(e) + "
                f"{len(self._quantile_modelle)} Quantile-Modell(e) geladen"
            )
        else:
            logger.info("Keine ML-Modelle gefunden — ML-Vorhersage deaktiviert")

        # T-0082: zusaetzlich pro Cluster aus `feuchte/<cluster>/` laden.
        # Schlaegt fehl ohne Schaden — Inferenz nutzt dann Legacy-Fallback.
        self._lade_cluster_modelle()

        return geladen > 0 or bool(self._cluster_caches)

    def _lade_cluster_modelle(self) -> None:
        """T-0082: scant `<modell_dir>/feuchte/<cluster_id>/` und laed pro
        Cluster ein eigenes Booster-Set in `_cluster_caches`. Fehler in
        einem Cluster fuehren nur zum Skip dieses Clusters; Legacy bleibt
        unangetastet.
        """
        if lgb is None:
            return
        feuchte_basis = self._modell_dir / "feuchte"
        if not feuchte_basis.exists() or not feuchte_basis.is_dir():
            return
        cluster_count = 0
        for cluster_dir in sorted(feuchte_basis.iterdir()):
            if not cluster_dir.is_dir():
                continue
            cluster_id = cluster_dir.name
            try:
                cache = self._lade_einen_cluster(cluster_id, cluster_dir)
            except Exception as exc:
                logger.error(
                    "ml.cluster.lade_fehler cluster=%s pfad=%s fehler=%s",
                    cluster_id, cluster_dir, exc,
                )
                continue
            if cache.modelle:
                self._cluster_caches[cluster_id] = cache
                cluster_count += 1
        if cluster_count > 0:
            logger.info(
                "ml.cluster.geladen anzahl=%d cluster=%s",
                cluster_count, ",".join(sorted(self._cluster_caches.keys())),
            )

    def _lade_einen_cluster(
        self, cluster_id: str, cluster_dir: Path,
    ) -> "_ClusterCache":
        """Liest alle Booster + Metadaten aus `cluster_dir` in ein
        `_ClusterCache`. Format identisch zum Legacy-Layout, nur unter
        `feuchte/<cluster>/` statt direkt im Wurzel-Verzeichnis.
        """
        cache = _ClusterCache(verzeichnis=cluster_dir)
        # Punkt-Modelle (q50-Default).
        for h in self.HORIZONTE:
            modell_pfad = cluster_dir / f"aktuell_{h}h.lgbm"
            if not modell_pfad.exists():
                continue
            booster = lgb.Booster(model_file=str(modell_pfad))
            cache.modelle[h] = booster
            cache.feature_cols[h] = booster.feature_name()
            tatsaechlicher_name = modell_pfad.resolve().stem
            metriken_pfad = cluster_dir / (
                f"{tatsaechlicher_name.replace('modell_', 'metriken_')}.json"
            )
            if metriken_pfad.exists():
                with open(metriken_pfad) as f:
                    metriken = TrainingsMetriken(**json.load(f))
                cache.metriken[h] = metriken
                cache.delta_ziel[h] = bool(getattr(metriken, "delta_ziel", False))
                self._pruefe_feature_schema(metriken, f"{cluster_id}/{h}h")
        # Band-Scale.
        for h in self.HORIZONTE:
            scale_pfad = cluster_dir / f"band_scale_{h}h.json"
            if not scale_pfad.exists():
                continue
            try:
                with open(scale_pfad) as f:
                    scale_data = json.load(f)
                cache.band_scale[h] = float(scale_data.get("scale", 1.0))
            except Exception as exc:
                logger.warning(
                    "ml.cluster.band_scale_fehler cluster=%s h=%dh fehler=%s",
                    cluster_id, h, exc,
                )
        # Quantile-Modelle.
        for h in self.HORIZONTE:
            for alpha_int in self.QUANTILE_ALPHAS:
                pfad = cluster_dir / f"aktuell_{h}h_q{alpha_int}.lgbm"
                if not pfad.exists():
                    continue
                booster = lgb.Booster(model_file=str(pfad))
                cache.quantile_modelle[(h, alpha_int)] = booster
                cache.quantile_feature_cols[(h, alpha_int)] = booster.feature_name()
                metriken_pfad = cluster_dir / (
                    f"{pfad.resolve().stem.replace('modell_', 'metriken_')}.json"
                )
                if metriken_pfad.exists():
                    with open(metriken_pfad) as f:
                        q_metriken = TrainingsMetriken(**json.load(f))
                    cache.quantile_delta_ziel[(h, alpha_int)] = bool(
                        getattr(q_metriken, "delta_ziel", False)
                    )
                else:
                    cache.quantile_delta_ziel[(h, alpha_int)] = False
        return cache

    def _pruefe_feature_schema(
        self, metriken: TrainingsMetriken, etikett: str,
    ) -> None:
        """T-0076b: warnt wenn Modell auf aelterem Feature-Schema trainiert.

        `feature_schema_version` wird beim Training in die metriken_*.json
        geschrieben. Wenn das geladene Modell aus einer aelteren Version
        stammt (z. B. nach einem Feature-Set-Refactor), ist die Drift-
        Auswertung gegenueber neuen Trainings-Baselines nicht direkt
        vergleichbar. Heute nur Warnung — bei zukuenftigen Schema-Brueche
        kann hier `return False` ergaenzt werden, damit der Lader das
        Modell ablehnt.
        """
        modell_version = int(getattr(metriken, "feature_schema_version", 1))
        if modell_version < self.AKTUELLE_FEATURE_SCHEMA_VERSION:
            # vorhersage.py nutzt stdlib logging, nicht structlog —
            # Kwargs wuerden TypeError werfen (siehe Memory
            # fehlerpattern_stdlib_structlog_mix.md). Daher als f-String.
            logger.warning(
                "ml.feature_schema_veraltet horizont=%s "
                "modell_version=%d code_version=%d "
                "(Modell auf aelterem Feature-Schema trainiert; "
                "Auto-Retrain korrigiert das beim naechsten Lauf, "
                "bis dahin Drift-Vergleich mit Vorsicht lesen)",
                etikett, modell_version, self.AKTUELLE_FEATURE_SCHEMA_VERSION,
            )

    @property
    def ist_verfuegbar(self) -> bool:
        """Prueft ob mindestens ein Modell geladen ist (Legacy oder Cluster).

        T-0101: Cluster-Caches sind seit T-0082 ein gleichberechtigter Pfad —
        sobald die Legacy-Wurzelmodelle weg sind, wuerde ein reiner
        `len(self._modelle)`-Check faelschlich `False` melden, obwohl
        Inferenz weiterhin moeglich ist.
        """
        return len(self._modelle) > 0 or len(self._cluster_caches) > 0

    def status(self) -> ModellStatus:
        """Gibt den Status aller geladenen Modelle zurueck."""
        # T-0101: Auch ohne Legacy-Modelle als geladen melden, sofern
        # mindestens ein Cluster da ist.
        if not self._modelle and not self._cluster_caches:
            return ModellStatus(ist_geladen=False)

        cluster_horizonte = {
            cid: sorted(cache.modelle.keys())
            for cid, cache in self._cluster_caches.items()
        }
        # trainiert_am: bevorzugt aelteste Legacy-Trainings-Zeit; ohne Legacy
        # auf den juengsten Cluster-Stand zurueckfallen, sonst None.
        trainiert_am = (
            self._metriken[min(self._metriken)].zeitstempel
            if self._metriken
            else None
        )

        return ModellStatus(
            modell_pfad=str(self._modell_dir),
            trainiert_am=trainiert_am,
            horizonte=sorted(self._modelle.keys()),
            metriken=list(self._metriken.values()),
            ist_geladen=True,
            letzte_missing_feature_quote=round(self._letzte_missing_quote, 3),
            cluster_horizonte=cluster_horizonte,
        )

    def _prediction_mit_features(
        self, modell: "lgb.Booster", features: "pd.DataFrame",
        erwartete_features: list[str],
    ) -> "np.ndarray":
        fehlend = [c for c in erwartete_features if c not in features.columns]
        if fehlend:
            quote = len(fehlend) / max(len(erwartete_features), 1)
            if quote > 0.3:
                # Bei >30 % fehlender Features liefert LightGBM zwar noch
                # Zahlen, aber sie sind inhaltlich nicht belastbar — die
                # Warnung landet in den Logs, der UI-Consumer kriegt die
                # Missing-Quote via /api/ml/status als Ampel.
                logger.warning(
                    "ml.inferenz.features_fehlen quote=%.2f fehlt=%s",
                    quote, fehlend[:10],
                )
            self._letzte_missing_quote = quote
        else:
            self._letzte_missing_quote = 0.0
        X = features.reindex(columns=erwartete_features).copy()
        for col in X.columns:
            if col in KATEGORISCHE_FEATURES:
                X[col] = X[col].astype("category")
            else:
                X[col] = pd.to_numeric(X[col], errors="coerce")
        return modell.predict(X)

    @staticmethod
    def _clip_feuchte(wert: float | None) -> float | None:
        """Clippt absolute Feuchtewerte auf die physikalische Sensorspanne."""
        if wert is None:
            return None
        wert_float = float(wert)
        if math.isnan(wert_float):
            return None
        return max(0.0, min(100.0, wert_float))

    # Schwelle, ab der ein Quantile-Crossing ueberhaupt geloggt wird.
    # LightGBM-Quantil-Modelle laufen separat — bei nahezu konstanten
    # Prognosen (Topfpflanzen mit kleiner Varianz) verursacht Tree-Split-
    # Noise sub-pp-Crossings (z. B. zitrus 24h q90=70.72 vs. q50=70.76).
    # Diese sind numerisches Rauschen, kein Modellproblem. Echte
    # Modellprobleme zeigen sich erst bei >= 1 pp Crossing.
    _QUANTILE_CROSSING_LOG_SCHWELLE_PP = 1.0

    def _korrigiere_quantile_band(
        self,
        q10_val: float | None,
        q50_val: float,
        q90_val: float | None,
        features: "pd.DataFrame",
        zeilen_index: int,
        horizont: int,
        hat_quelle: bool,
    ) -> tuple[float | None, float, float | None]:
        """Erzwingt q10 <= q50 <= q90 ohne den Punktwert zu veraendern."""
        if q10_val is None or q90_val is None:
            return q10_val, q50_val, q90_val

        q10_neu = min(q10_val, q50_val)
        q90_neu = max(q90_val, q50_val)
        # Nur loggen wenn Crossing fachlich relevant — sub-pp ist Noise.
        crossing_pp = max(q10_val - q50_val, q50_val - q90_val, 0.0)
        if (
            (q10_neu != q10_val or q90_neu != q90_val)
            and crossing_pp >= self._QUANTILE_CROSSING_LOG_SCHWELLE_PP
        ):
            logger.warning(
                "ml.quantile_crossing zone=%s horizont=%dh "
                "q10=%.2f q50=%.2f q90=%.2f crossing=%.2fpp -> band_korrigiert",
                features.iloc[zeilen_index].get("zone_id", "unbekannt")
                if hat_quelle else "unbekannt",
                horizont, q10_val, q50_val, q90_val, crossing_pp,
            )
        return q10_neu, q50_val, q90_neu

    def _berechne_top_features(
        self,
        modell: "lgb.Booster",
        features: "pd.DataFrame",
        erwartete_features: list[str],
        anzahl: int = 5,
        skala: str = "absolut",
    ) -> list[list[FeatureBeitrag]]:
        """T-0040: Pro Zeile die Top-N Feature-Beitraege (SHAP/pred_contrib).

        LightGBM liefert bei `pred_contrib=True` ein Array der Form
        `[n_samples, n_features + 1]`; die letzte Spalte ist der Bias
        (Intercept), den wir hier nicht zurueckgeben — nur die Features.
        Sortiert nach absolutem Beitrag absteigend.
        """
        X = features.reindex(columns=erwartete_features).copy()
        for col in X.columns:
            if col in KATEGORISCHE_FEATURES:
                X[col] = X[col].astype("category")
            else:
                X[col] = pd.to_numeric(X[col], errors="coerce")
        # pred_contrib liefert je Zeile einen Beitrag pro Feature + 1 Bias-Spalte
        contribs = modell.predict(X, pred_contrib=True)
        ergebnisse: list[list[FeatureBeitrag]] = []
        for i in range(len(X)):
            paare: list[tuple[str, float, float | None]] = []
            for j, name in enumerate(erwartete_features):
                wert = X.iloc[i, j]
                # Categorical-Features (z. B. `quelle`="gardena") sind
                # Strings und nicht float-konvertierbar — fuer die Anzeige
                # setzen wir `wert=None`, Beitrag bleibt korrekt.
                wert_float: float | None
                try:
                    wert_float = None if pd.isna(wert) else float(wert)
                except (TypeError, ValueError):
                    wert_float = None
                paare.append((name, float(contribs[i, j]), wert_float))
            paare.sort(key=lambda p: abs(p[1]), reverse=True)
            ergebnisse.append([
                FeatureBeitrag(name=n, beitrag=round(b, 3),
                               wert=round(w, 3) if w is not None else None,
                               skala=skala)
                for (n, b, w) in paare[:anzahl]
            ])
        return ergebnisse

    def vorhersage(
        self,
        features: "pd.DataFrame",
        horizont: int = 6,
        details: bool = False,
        cluster_id: str | None = None,
    ) -> list[MLVorhersage] | None:
        """Erstellt Vorhersagen fuer einen Feature-DataFrame.

        Args:
            features: DataFrame mit denselben Feature-Spalten wie beim Training.
                      Muss zone_id und boden_feuchte_aktuell enthalten.
            horizont: Vorhersage-Horizont (6, 12, 24)
            details: Wenn True, wird `top_features` (SHAP-Beitraege der 5
                     wichtigsten Features) mitgeliefert. Kostet ca. 10-30 ms
                     pro Prognose extra; daher Opt-In pro Aufruf.
            cluster_id: T-0082 — wenn gesetzt UND der Cluster geladen ist,
                     wird das Cluster-Modell statt des Legacy-Modells genutzt.
                     None = Legacy-Pfad (alle bisherigen Caller unveraendert).

        Returns:
            Liste von MLVorhersage-Objekten, oder None wenn Modell fehlt.
            Wenn Quantile-Modelle (T-0046) vorhanden sind, tragen die
            Eintraege zusaetzlich `q10` und `q90`. Ohne Quantile-Modelle
            bleibt `feuchte_prognose` der Punkt-Wert und q10/q90 sind None.
        """
        # T-0082: lokale Aliase auf das aktive Booster-Set (Legacy oder
        # Cluster). Standard = Legacy-Cluster (= heutige Felder); bei
        # `cluster_id` mit geladenem Cluster werden die Aliase auf das
        # Cluster-Set gesetzt. Damit bleiben alle Lookups unveraendert,
        # nur die Source-Dicts wechseln.
        if cluster_id and cluster_id in self._cluster_caches:
            cache = self._cluster_caches[cluster_id]
            modelle = cache.modelle
            feature_cols = cache.feature_cols
            quantile_modelle = cache.quantile_modelle
            quantile_feature_cols = cache.quantile_feature_cols
            delta_ziel = cache.delta_ziel
            quantile_delta_ziel = cache.quantile_delta_ziel
            band_scale_dict = cache.band_scale
        else:
            modelle = self._modelle
            feature_cols = self._feature_cols
            quantile_modelle = self._quantile_modelle
            quantile_feature_cols = self._quantile_feature_cols
            delta_ziel = self._delta_ziel
            quantile_delta_ziel = self._quantile_delta_ziel
            band_scale_dict = self._band_scale

        if horizont not in modelle:
            return None

        y_pred = self._prediction_mit_features(
            modelle[horizont], features,
            feature_cols[horizont],
        )
        top_features_pro_zeile: list[list[FeatureBeitrag]] | None = None
        if details:
            try:
                top_features_pro_zeile = self._berechne_top_features(
                    modelle[horizont], features, feature_cols[horizont],
                    skala=(
                        "delta"
                        if delta_ziel.get(horizont, False)
                        else "absolut"
                    ),
                )
            except Exception:
                # SHAP ist "nice-to-have" — Inferenz soll nicht fehlschlagen,
                # wenn pred_contrib aus irgendeinem Grund einmal scheitert.
                logger.exception("ml.top_features.fehler horizont=%d", horizont)
                top_features_pro_zeile = None

        # T-0046: Quantile-Werte, wenn verfuegbar.
        q10_arr = None
        q90_arr = None
        if (horizont, 10) in quantile_modelle:
            q10_arr = self._prediction_mit_features(
                quantile_modelle[(horizont, 10)], features,
                quantile_feature_cols[(horizont, 10)],
            )
        if (horizont, 90) in quantile_modelle:
            q90_arr = self._prediction_mit_features(
                quantile_modelle[(horizont, 90)], features,
                quantile_feature_cols[(horizont, 90)],
            )

        ergebnisse = []
        jetzt = datetime.now()
        hat_quelle = "zone_id" in features.columns
        ist_delta_punkt = delta_ziel.get(horizont, False)
        ist_delta_q10 = quantile_delta_ziel.get((horizont, 10), False)
        ist_delta_q90 = quantile_delta_ziel.get((horizont, 90), False)
        # T-0062d: Band-Scale (1.0 = keine Kalibrierung)
        band_scale = band_scale_dict.get(horizont, 1.0)
        for i in range(len(features)):
            q10_val = float(q10_arr[i]) if q10_arr is not None else None
            q90_val = float(q90_arr[i]) if q90_arr is not None else None
            q50_val = float(y_pred[i])

            # T-0062a: bei Delta-Training liefert `predict()` ein Delta;
            # die absolute Prognose ist `aktuelle Feuchte + Delta`. Wir
            # konvertieren hier, damit der restliche Code (Quantile-
            # Sortierung, Saettigungs-Cap) mit absoluten Werten arbeitet.
            aktuell_feuchte = float(features.iloc[i].get("boden_feuchte_aktuell", 0))
            if ist_delta_punkt:
                q50_val = aktuell_feuchte + q50_val
            if q10_val is not None and ist_delta_q10:
                q10_val = aktuell_feuchte + q10_val
            if q90_val is not None and ist_delta_q90:
                q90_val = aktuell_feuchte + q90_val

            # T-0062d: Band-Scale anwenden um Median. Nur wenn q10+q90
            # vorhanden und Scale != 1.0 (minimal-invasiv).
            if (q10_val is not None and q90_val is not None
                    and abs(band_scale - 1.0) > 0.01):
                median = (q10_val + q90_val) / 2
                halbbreite = abs(q90_val - q10_val) / 2 * band_scale
                q10_val = median - halbbreite
                q90_val = median + halbbreite

            # T-0061a + T-0130 (H-6): Saettigungs-Post-Processing. Bei
            # gleichzeitiger Saettigung UND angekuendigtem Regen prognostiziert
            # das Modell systematisch zu niedrig (Mean-Reversion-Prior
            # dominiert -- siehe ml_regen_ignoranz.md). Wir cappen dann die
            # Prognose nach unten.
            #
            # T-0061a (urspruenglich): nur sehr enge Schwelle (>95 % UND
            # >5 mm) -> Cap auf `aktuell - 2`. Greift kaum.
            # T-0130 (H-6, 2026-05-04): zwei zusaetzliche Stufen, die den
            # Pre-Mortem-Akt-5-Fall (90 % + Dauerregen) abdecken. Memory
            # ml_regen_ignoranz.md: 12h+24h-MAE bei Saturierung war zuletzt
            # 6-8 pp -- die Stufen sind speziell dort relevant.
            #
            # Stufenlogik (von eng nach grosszuegig):
            #   feuchte > 95 + regen > 5    -> cap aktuell - 2  (T-0061a)
            #   feuchte > 85 + regen > 7    -> cap aktuell - 4  (H-6)
            #   feuchte > 75 + regen > 10   -> cap aktuell - 6  (H-6)
            # Anmerkung T-0062a: Mit Delta-Regressor sollte dieser Cap
            # seltener greifen, weil das Modell selbst keine Mean-Reversion
            # mehr lernt. Als Safety-Schicht bleibt er -- besonders fuer
            # 12h/24h-Horizonte, wo das Sample-Weighting weniger wirkt.
            regen_spalte = f"niederschlag_summe_{horizont}h"
            regen_forecast = features.iloc[i].get(regen_spalte)
            regen_mm = float(regen_forecast) if regen_forecast is not None else 0.0
            untergrenze = None
            stufe = None
            if aktuell_feuchte > 95.0 and regen_mm > 5.0:
                untergrenze, stufe = aktuell_feuchte - 2.0, "eng"
            elif aktuell_feuchte > 85.0 and regen_mm > 7.0:
                untergrenze, stufe = aktuell_feuchte - 4.0, "mittel"
            elif aktuell_feuchte > 75.0 and regen_mm > 10.0:
                untergrenze, stufe = aktuell_feuchte - 6.0, "weit"
            if untergrenze is not None:
                q50_vor = q50_val
                q50_val = max(q50_val, untergrenze)
                if q90_val is not None:
                    q90_val = max(q90_val, untergrenze)
                if q10_val is not None:
                    # q10 darf bis 3 %-Punkte unter die Untergrenze — gibt dem
                    # Band etwas Luft fuer verbleibende Unsicherheit.
                    q10_val = max(q10_val, untergrenze - 3.0)
                if q50_val != q50_vor:
                    logger.info(
                        "ml.saettigungs_cap zone=%s horizont=%dh stufe=%s "
                        "feuchte=%.1f regen_mm=%.1f q50_vor=%.2f q50_nach=%.2f",
                        features.iloc[i].get("zone_id", "unbekannt") if hat_quelle else "unbekannt",
                        horizont, stufe, aktuell_feuchte, regen_mm, q50_vor, q50_val,
                    )

            # Erst nach Delta-Addition und Post-Processing auf absolute
            # Feuchte clippen. Roh-Deltas duerfen negativ sein.
            q50_val = self._clip_feuchte(q50_val) or 0.0
            q10_val = self._clip_feuchte(q10_val)
            q90_val = self._clip_feuchte(q90_val)
            q10_val, q50_val, q90_val = self._korrigiere_quantile_band(
                q10_val, q50_val, q90_val,
                features, i, horizont, hat_quelle,
            )

            ergebnisse.append(MLVorhersage(
                zone_id=features.iloc[i].get("zone_id", "unbekannt") if hat_quelle else "unbekannt",
                zeitstempel=jetzt,
                horizont_stunden=horizont,
                feuchte_aktuell=float(features.iloc[i].get(
                    "boden_feuchte_aktuell", 0
                )),
                feuchte_prognose=round(q50_val, 2),
                q10=round(q10_val, 2) if q10_val is not None else None,
                q90=round(q90_val, 2) if q90_val is not None else None,
                top_features=(
                    top_features_pro_zeile[i]
                    if top_features_pro_zeile is not None else None
                ),
            ))

        return ergebnisse

    def vorhersage_einzeln(
        self,
        features: dict,
        horizont: int = 6,
    ) -> MLVorhersage | None:
        """Einzelne Vorhersage fuer eine Zone (Convenience).

        Args:
            features: Dict mit Feature-Werten (wie eine Zeile des DataFrames)
            horizont: Vorhersage-Horizont (6, 12, 24)
        """
        if pd is None or horizont not in self._modelle:
            return None

        df = pd.DataFrame([features])
        ergebnisse = self.vorhersage(df, horizont)
        return ergebnisse[0] if ergebnisse else None

    def _modell_version(
        self, horizont: int, cluster_id: str | None = None,
    ) -> str:
        """Aufgeloestes Symlink-Ziel des aktiven Modells.

        Default (cluster_id=None): Wurzel-Verzeichnis (Legacy). Bei
        gesetztem cluster_id mit geladenem Cache wird der Cluster-Pfad
        genommen — die `modell_version`-Spalte im Drift-Log enthaelt
        damit den Cluster, sodass nachtraegliches Filtering moeglich ist
        (`WHERE modell_version LIKE '<cluster>/%'`).
        """
        if cluster_id and cluster_id in self._cluster_caches:
            cache = self._cluster_caches[cluster_id]
            if cache.verzeichnis is not None:
                modell_pfad = cache.verzeichnis / f"aktuell_{horizont}h.lgbm"
                try:
                    if modell_pfad.is_symlink():
                        return f"{cluster_id}/{os.path.basename(os.readlink(str(modell_pfad)))}"
                    if modell_pfad.exists():
                        return f"{cluster_id}/{modell_pfad.resolve().name}"
                except OSError:
                    pass
        modell_pfad = self._modell_dir / f"aktuell_{horizont}h.lgbm"
        try:
            if modell_pfad.is_symlink():
                # os.readlink gibt den relativen Namen, das reicht als Version.
                return os.path.basename(os.readlink(str(modell_pfad)))
            if modell_pfad.exists():
                return modell_pfad.resolve().name
        except OSError:
            pass
        return "unbekannt"

    async def _hole_live_df(
        self,
        speicher: "Speicher",
        konfig: "GesamtKonfig",
        jetzt: datetime,
    ) -> "pd.DataFrame":
        """T-0314: Gemeinsamer 48h-Feature-DF-Build (alle Zonen) mit TTL.

        `live_vorhersage` (pro Zone) und `live_vorhersage_bulk` brauchen
        denselben 48h-DF ueber ALLE Zonen. Ohne gemeinsamen Cache entstand
        N-facher Rebuild pro Snapshot/Entscheidungszyklus (gemessen:
        Snapshot details=true 15 Builds, Entscheidungsloop 5 Builds; 1 Build
        ~3 s). Dieser Cache (TTL = `_df_cache_ttl_s`, identisch zum Ergebnis-
        Cache) macht daraus EINEN Build, den sich alle Pfade (bulk, per-Zone,
        details=true/false) teilen. Single-Flight via Lock.

        Staleness <= TTL (Sensor-Cadence ~10 min) -- gleiche Garantie wie der
        bestehende `_live_cache`.
        """
        # Defensiv: Tests instanziieren den Service teils via __new__ ohne
        # __init__ (siehe _cluster_fuer_zone). Pro Attribut pruefen, damit
        # Tests einzelne Felder (z. B. _df_cache_ttl_s) vorab setzen koennen.
        if not hasattr(self, "_df_cache"):
            self._df_cache = None
        if not hasattr(self, "_df_cache_ttl_s"):
            self._df_cache_ttl_s = 45
        if not hasattr(self, "_df_cache_lock"):
            self._df_cache_lock = asyncio.Lock()

        cached = self._df_cache
        if cached is not None:
            c_zeit, c_df = cached
            if (jetzt - c_zeit).total_seconds() < self._df_cache_ttl_s:
                return c_df

        async with self._df_cache_lock:
            # Double-Check: ein paralleler Coro koennte den DF inzwischen
            # gebaut haben, waehrend wir auf den Lock warteten.
            cached = self._df_cache
            if cached is not None:
                c_zeit, c_df = cached
                if (datetime.now() - c_zeit).total_seconds() < self._df_cache_ttl_s:
                    return c_df
            from bewaesserung.ml.features import FeatureExtraktor

            von = jetzt - timedelta(hours=48)
            extraktor = FeatureExtraktor(speicher, konfig)
            df = await extraktor.erstelle_trainingsdaten(von=von, bis=jetzt)
            self._df_cache = (datetime.now(), df)
            return df

    async def live_vorhersage(
        self,
        zone_id: str,
        speicher: "Speicher",
        konfig: "GesamtKonfig",
        details: bool = False,
    ) -> dict[str, MLVorhersage]:
        """Live-Vorhersage fuer eine Zone mit vollem Feature-Vektor.

        Baut den Feature-Vektor ueber FeatureExtraktor aus DB-Daten —
        gleiche Features wie beim Training (Lags, Rolling Averages, Wetter etc.).
        T-0047: jede Prognose wird in `ml_vorhersage_log` persistiert, damit
        der MLDriftJob sie spaeter gegen die echte Messung evaluieren kann.
        T-0040: `details=True` setzt `top_features` pro Prognose
        (SHAP-Beitraege via LightGBM `pred_contrib=True`).

        Performance (2026-04-21): Cache mit 45 s TTL. Ohne Cache wuerden 11
        parallele Frontend-Requests 11× den 48-h-Feature-DataFrame (mit
        Pandas-Slicing, Rolling-Averages, Wetter-Merge) aufbauen — im
        asyncio-Event-Loop blockierend. Das liess /api/.../messwerte
        30+ s hungern und den Backend-Prozess bei 96 % CPU dauerlasten.

        Returns:
            Dict mit Horizont-Keys ("6h", "12h", "24h") → MLVorhersage
        """
        # T-0101: Cluster-Caches sind ein gleichberechtigter Inferenz-Pfad
        # (T-0082). Sobald die Legacy-Wurzelmodelle weg sind, wuerde ein
        # reiner `not self._modelle`-Check faelschlich abbrechen, obwohl
        # Cluster-Modelle vorhanden sind.
        if pd is None or (not self._modelle and not self._cluster_caches):
            return {}

        # Cache-Lookup: parallele Requests fuer dieselbe Zone innerhalb der
        # TTL teilen sich das Ergebnis. KEIN Drift-Log-Insert bei Cache-Hit,
        # sonst verdoppeln sich die Log-Zeilen.
        cache_key = (zone_id, details)
        jetzt = datetime.now()
        cached = self._live_cache.get(cache_key)
        if cached is not None:
            cached_zeit, cached_ergebnisse = cached
            alter_s = (jetzt - cached_zeit).total_seconds()
            if alter_s < self._live_cache_ttl_s:
                return cached_ergebnisse

        # T-0314: gemeinsamer DF-Cache (siehe _hole_live_df) statt pro Call
        # den 48h-DF ueber alle Zonen neu zu bauen.
        df = await self._hole_live_df(speicher, konfig, jetzt)

        if df.empty:
            return {}

        # Nur die angefragte Zone, letzter Zeitpunkt
        df_zone = df[df["zone_id"] == zone_id]
        if df_zone.empty:
            return {}

        letzte_zeile = df_zone.sort_values("zeitstempel").iloc[[-1]]
        feature_zeit_roh = letzte_zeile.iloc[0].get("zeitstempel")
        try:
            feature_zeitstempel = (
                feature_zeit_roh
                if isinstance(feature_zeit_roh, datetime)
                else datetime.fromisoformat(str(feature_zeit_roh))
            )
        except (TypeError, ValueError):
            feature_zeitstempel = jetzt

        # T-0082: Cluster-Lookup. Wenn die Zone in einem geladenen
        # Cluster lebt, geht die Inferenz dort durch — sonst Legacy.
        cluster_id = self._cluster_fuer_zone(zone_id)

        ergebnisse = {}
        for horizont in self.HORIZONTE:
            vorhersagen = self.vorhersage(
                letzte_zeile, horizont, details=details, cluster_id=cluster_id,
            )
            if not vorhersagen:
                continue
            mlv = vorhersagen[0]
            ergebnisse[f"{horizont}h"] = mlv
            try:
                await speicher.logge_ml_vorhersage(
                    zeitstempel=mlv.zeitstempel,
                    zone_id=mlv.zone_id,
                    horizont_h=horizont,
                    prognose_ziel_zeit=(
                        feature_zeitstempel + timedelta(hours=horizont)
                    ),
                    prognose_feuchte=mlv.feuchte_prognose,
                    modell_version=self._modell_version(horizont, cluster_id),
                    q10=mlv.q10,
                    q90=mlv.q90,
                    feature_zeitstempel=feature_zeitstempel,
                )
            except Exception:
                logger.exception("ml.drift_log.insert_fehler")

        # Cache setzen (auch bei leerem dict — verhindert Folge-Stuerme
        # wenn eine Zone dauerhaft ohne Messungen ist).
        self._live_cache[cache_key] = (jetzt, ergebnisse)
        # Cache gelegentlich aufraeumen: wenn > 50 Eintraege, alte
        # ueber TTL-Grenze entfernen. Der Dict-Scan ist bei < 50 Eintraegen
        # vernachlaessigbar und laeuft hier im Callpfad, kein separater Timer.
        if len(self._live_cache) > 50:
            grenze = jetzt - timedelta(seconds=self._live_cache_ttl_s * 2)
            self._live_cache = {
                k: v for k, v in self._live_cache.items() if v[0] > grenze
            }

        return ergebnisse

    async def live_vorhersage_bulk(
        self,
        zone_ids: list[str],
        speicher: "Speicher",
        konfig: "GesamtKonfig",
        details: bool = False,
    ) -> dict[str, dict[str, MLVorhersage]]:
        """T-0200: Bulk-Variante fuer den Dashboard-Snapshot.

        `live_vorhersage` baut bei Cache-Miss den 48h-Feature-DF ueber
        ALLE Zonen — bei sequenziellem Aufruf von 14 Zonen heisst das
        14x Pandas-Slicing + Wetter-Merge + Rolling-Averages. Der
        TTL-Cache (45 s) hilft nur bei *parallelen* Calls auf dieselbe
        Zone; sequenziell ist er nutzlos, weil die fruehen Cache-
        Eintraege laengst expired sind, bis die spaeten Zonen drankommen.

        Diese Methode baut den DF **einmal**, slicet pro Zone, und ruft
        pro Zone die gleiche Inferenz-Schleife wie `live_vorhersage` —
        inkl. Drift-Log-Insert. Returns:
            dict[zone_id, dict[horizont_key, MLVorhersage]]

        Cache wird pro Zone genauso aktualisiert, damit nachfolgende
        Per-Zone-Calls innerhalb der TTL Cache-Hit haben.
        """
        if pd is None or (not self._modelle and not self._cluster_caches):
            return {zid: {} for zid in zone_ids}
        if not zone_ids:
            return {}

        jetzt = datetime.now()
        # T-0314: gemeinsamer DF-Cache (siehe _hole_live_df) -- teilt den
        # 48h-DF mit per-Zone live_vorhersage statt N-fach neu zu bauen.
        df = await self._hole_live_df(speicher, konfig, jetzt)

        ergebnis: dict[str, dict[str, MLVorhersage]] = {zid: {} for zid in zone_ids}
        if df.empty:
            return ergebnis

        for zone_id in zone_ids:
            df_zone = df[df["zone_id"] == zone_id]
            if df_zone.empty:
                # Wie live_vorhersage: leeres dict ergebnis + Cache-Setzen
                # gegen Folge-Stuerme.
                self._live_cache[(zone_id, details)] = (jetzt, {})
                continue

            letzte_zeile = df_zone.sort_values("zeitstempel").iloc[[-1]]
            feature_zeit_roh = letzte_zeile.iloc[0].get("zeitstempel")
            try:
                feature_zeitstempel = (
                    feature_zeit_roh
                    if isinstance(feature_zeit_roh, datetime)
                    else datetime.fromisoformat(str(feature_zeit_roh))
                )
            except (TypeError, ValueError):
                feature_zeitstempel = jetzt

            cluster_id = self._cluster_fuer_zone(zone_id)

            zone_ergebnis: dict[str, MLVorhersage] = {}
            for horizont in self.HORIZONTE:
                vorhersagen = self.vorhersage(
                    letzte_zeile, horizont, details=details, cluster_id=cluster_id,
                )
                if not vorhersagen:
                    continue
                mlv = vorhersagen[0]
                zone_ergebnis[f"{horizont}h"] = mlv
                try:
                    await speicher.logge_ml_vorhersage(
                        zeitstempel=mlv.zeitstempel,
                        zone_id=mlv.zone_id,
                        horizont_h=horizont,
                        prognose_ziel_zeit=(
                            feature_zeitstempel + timedelta(hours=horizont)
                        ),
                        prognose_feuchte=mlv.feuchte_prognose,
                        modell_version=self._modell_version(horizont, cluster_id),
                        q10=mlv.q10,
                        q90=mlv.q90,
                        feature_zeitstempel=feature_zeitstempel,
                    )
                except Exception:
                    logger.exception("ml.drift_log.insert_fehler")

            ergebnis[zone_id] = zone_ergebnis
            self._live_cache[(zone_id, details)] = (jetzt, zone_ergebnis)

        return ergebnis
