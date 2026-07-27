"""T-0065: MLResponseService — Inferenz fuer das Inverse-Modell.

Singleton-Service, der pro Zone das aktuelle `inverse`-Modell (Symlink
`aktuell_{zone}_inverse.lgbm`) laedt und auf Anfrage eine
Dauer-Empfehlung liefert. Modell-Fehler oder fehlende Modelle fuehren zu
`None`-Rueckgabe; Aufrufer (entscheidung.py) faellt auf die Heuristik
zurueck.

Das Modul nutzt stdlib `logging` — Regression-Tests muessen
`caplog.set_level(INFO)` setzen (fehlerpattern_stdlib_structlog_mix.md).
"""
from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime
from pathlib import Path

try:
    import lightgbm as lgb
    import numpy as np
except ImportError:
    lgb = None  # type: ignore
    np = None  # type: ignore

logger = logging.getLogger(__name__)


# Clip-Grenzen fuer ziel_delta (Prozentpunkte). Extrapolation nach aussen
# macht die Schaetzung unbrauchbar — wir beschraenken den Input auf den
# Range, den wir typisch gesehen haben.
MIN_ZIEL_DELTA = 1.0
MAX_ZIEL_DELTA = 60.0


class ResponseModellNichtGeladen(Exception):
    """Erwartbarer Fall: Kein aktuelles Modell deployed fuer diese Zone."""


class MLResponseService:
    """Laedt pro Zone das inverse Dauer-Modell und liefert Empfehlungen.

    Benutzung:
        service = MLResponseService.instanz(basis_verzeichnis)
        service.lade_zone("waldblumenhain")  # einmalig
        dauer_s = service.inverse_dauer(zone_id="waldblumenhain", ...)

    Thread-sicher ueber einen Modul-Lock; der Service cached geladene
    Modelle (Symlink-Target-Pfad merken und bei Aenderung neu laden).
    """

    _instanz: "MLResponseService | None" = None
    _instanz_lock = threading.Lock()

    def __init__(self, basis_verzeichnis: str | Path):
        if lgb is None or np is None:
            raise ImportError(
                "ML-Abhaengigkeiten fehlen (lightgbm/numpy). "
                "pip install -e '.[ml]'"
            )
        self._basis = Path(basis_verzeichnis)
        # zone_id -> (geladenes_ziel_pfad, booster, feature_cols,
        #             lps_median, version, trainiert_am)
        self._modelle: dict[str, dict] = {}
        self._lock = threading.Lock()

    # --- Singleton ---

    @classmethod
    def instanz(cls, basis_verzeichnis: str | Path | None = None) -> "MLResponseService":
        """Zugriff auf Singleton. Beim ersten Aufruf muss basis_verzeichnis gesetzt sein."""
        with cls._instanz_lock:
            if cls._instanz is None:
                if basis_verzeichnis is None:
                    raise RuntimeError(
                        "MLResponseService: erster instanz()-Aufruf "
                        "benoetigt basis_verzeichnis."
                    )
                cls._instanz = cls(basis_verzeichnis)
            return cls._instanz

    @classmethod
    def zuruecksetzen(cls) -> None:
        """Nur fuer Tests: Singleton aufloesen."""
        with cls._instanz_lock:
            cls._instanz = None

    # --- Laden ---

    def _pfade(self, zone_id: str) -> tuple[Path, Path]:
        zone_dir = self._basis / zone_id
        return (
            zone_dir / f"aktuell_{zone_id}_inverse.lgbm",
            zone_dir / f"aktuell_{zone_id}_metadata.json",
        )

    def lade_zone(self, zone_id: str, *, force: bool = False) -> bool:
        """Laedt das inverse Modell fuer eine Zone.

        Rueckgabe: True wenn geladen, False wenn kein Modell deployed.
        """
        modell_link, meta_link = self._pfade(zone_id)
        if not modell_link.exists() or not meta_link.exists():
            logger.info(
                "ml.response_service.kein_modell zone=%s", zone_id,
            )
            with self._lock:
                self._modelle.pop(zone_id, None)
            return False
        # Symlink-Ziel als Schluessel — bei Deploy zeigt es auf neues Verz.
        try:
            ziel_pfad = str(modell_link.resolve())
        except OSError:
            ziel_pfad = str(modell_link)
        with self._lock:
            bestand = self._modelle.get(zone_id)
            if bestand and not force and bestand.get("ziel_pfad") == ziel_pfad:
                return True
        try:
            booster = lgb.Booster(model_file=str(modell_link))
            meta = json.loads(meta_link.read_text())
        except Exception:
            logger.exception(
                "ml.response_service.ladefehler zone=%s", zone_id,
            )
            return False
        with self._lock:
            self._modelle[zone_id] = {
                "ziel_pfad": ziel_pfad,
                "booster": booster,
                "feature_cols": list(meta.get("feature_cols_inverse") or []),
                "lps_median": meta.get("liter_pro_sekunde_median"),
                "version": meta.get("version"),
                "trainiert_am": meta.get("trainiert_am"),
            }
        logger.info(
            "ml.response_service.geladen zone=%s version=%s n_features=%d",
            zone_id, meta.get("version"),
            len(self._modelle[zone_id]["feature_cols"]),
        )
        return True

    def entladen(self, zone_id: str) -> None:
        with self._lock:
            self._modelle.pop(zone_id, None)

    def version(self, zone_id: str) -> str | None:
        with self._lock:
            eintrag = self._modelle.get(zone_id)
            return eintrag.get("version") if eintrag else None

    def ist_geladen(self, zone_id: str) -> bool:
        with self._lock:
            return zone_id in self._modelle

    # --- Inferenz ---

    def inverse_dauer(
        self,
        *,
        zone_id: str,
        f_vor: float,
        ziel_schwelle: float,
        f_vor_gradient_3h: float = 0.0,
        f_vor_gradient_24h: float = 0.0,
        et0_vor_24h: float = 0.0,
        et0_nach_6h: float = 0.0,
        niederschlag_nach_24h: float = 0.0,
        temperatur_ereignis: float = 18.0,
        vpd_mittel: float = 0.8,
        liter_pro_sekunde: float | None = None,
        shared_valve: bool = False,
        jetzt: datetime | None = None,
    ) -> int | None:
        """Liefert eine Dauer-Empfehlung in Sekunden, oder None.

        None-Faelle (Heuristik-Fallback im Aufrufer):
        - Kein Modell fuer die Zone geladen (`lade_zone` kehrte False zurueck).
        - Feature-Liste aus dem Metadata-File passt nicht.
        - Predict-Ausnahme.
        - ziel_delta <= 0 (Zone ist bereits feucht genug).
        """
        with self._lock:
            eintrag = self._modelle.get(zone_id)
        if eintrag is None:
            # Versuch: lazy reload.
            if not self.lade_zone(zone_id):
                return None
            with self._lock:
                eintrag = self._modelle.get(zone_id)
            if eintrag is None:
                return None

        ziel_delta_raw = float(ziel_schwelle) - float(f_vor)
        if ziel_delta_raw <= 0.0:
            # Boden bereits ueber Schwelle — keine Empfehlung.
            return None
        ziel_delta = max(MIN_ZIEL_DELTA, min(MAX_ZIEL_DELTA, ziel_delta_raw))

        jetzt = jetzt or datetime.now()
        doy = jetzt.timetuple().tm_yday
        stunde_float = jetzt.hour + jetzt.minute / 60.0
        jahr_sin = math.sin(2 * math.pi * doy / 365)
        jahr_cos = math.cos(2 * math.pi * doy / 365)
        tag_sin = math.sin(2 * math.pi * stunde_float / 24)
        tag_cos = math.cos(2 * math.pi * stunde_float / 24)

        lps = liter_pro_sekunde
        if lps is None:
            lps = eintrag.get("lps_median") or 0.08
        lps = max(0.0, float(lps))

        feature_werte = {
            "ziel_delta": ziel_delta,
            "f_vor": float(f_vor),
            "f_vor_gradient_3h": float(f_vor_gradient_3h),
            "f_vor_gradient_24h": float(f_vor_gradient_24h),
            "et0_vor_24h": float(et0_vor_24h),
            "et0_nach_6h": float(et0_nach_6h),
            "niederschlag_nach_24h": float(niederschlag_nach_24h),
            "temperatur_ereignis": float(temperatur_ereignis),
            "vpd_mittel": float(vpd_mittel),
            "jahreszeit_sin": jahr_sin,
            "jahreszeit_cos": jahr_cos,
            "tageszeit_sin": tag_sin,
            "tageszeit_cos": tag_cos,
            "shared_valve": 1.0 if shared_valve else 0.0,
            "liter_pro_sekunde": lps,
        }
        feature_cols = eintrag["feature_cols"]
        # Unbekannte Features mit 0 imputieren (sollte nicht passieren —
        # Feature-Liste kommt aus Metadata derselben Pipeline).
        row = [float(feature_werte.get(c, 0.0)) for c in feature_cols]
        X = np.array([row], dtype=float)

        try:
            pred = eintrag["booster"].predict(X)[0]
        except Exception:
            logger.exception(
                "ml.response_service.predict_fehler zone=%s", zone_id,
            )
            return None
        if not np.isfinite(pred):
            return None
        dauer_s = int(round(max(0.0, float(pred))))
        return dauer_s


def baue_response_service(basis_verzeichnis: str | Path) -> MLResponseService:
    """Factory — legt den Singleton an falls noetig."""
    return MLResponseService.instanz(basis_verzeichnis)
