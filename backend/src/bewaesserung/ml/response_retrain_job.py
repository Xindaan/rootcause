"""T-0065: MlResponseRetrainJob — zyklischer Retrain der Response-Modelle.

Pro Zone triggert der Job entweder zeit- oder event-getrieben und traininert
Forward- + Inverse-Modell via `ResponseTrainingsPipeline.lauf()`. Nach
erfolgreichem Training wird gegen das aktuell deployte Modell verglichen
(Gate: `mae_neu_inverse <= gate_mae_faktor * mae_alt_inverse`); besteht der
Check, werden die Symlinks geswappt (das macht die Pipeline bereits inline
durch `_aktualisiere_symlinks` — dieser Job validiert nur und rollt bei
Gate-Rejection zurueck).

Das Modul nutzt stdlib `logging` — Regression-Tests
`caplog.set_level(INFO)` setzen (fehlerpattern_stdlib_structlog_mix.md).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from bewaesserung.modelle import GesamtKonfig, MlBewaesserungsResponseKonfig
from bewaesserung.speicher import Speicher

logger = logging.getLogger(__name__)


class MlResponseRetrainJob:
    """Pro Zone retrainen, Gate pruefen, Symlinks pflegen."""

    def __init__(
        self,
        speicher: Speicher,
        konfig: GesamtKonfig,
        response_konfig: MlBewaesserungsResponseKonfig,
        basis_verzeichnis: str | Path = "",
    ):
        self._speicher = speicher
        self._konfig = konfig
        self._resp = response_konfig
        if not basis_verzeichnis and not response_konfig.ausgabe_pfad:
            from bewaesserung.konfig import ML_DATEN_PFAD
            basis_verzeichnis = str(Path(ML_DATEN_PFAD) / "response")
        self._basis = Path(basis_verzeichnis or response_konfig.ausgabe_pfad)
        self._basis.mkdir(parents=True, exist_ok=True)
        self._intervall = timedelta(days=response_konfig.retrain_intervall_tage)
        # Pro Zone den letzten Lauf + Eventzaehler merken.
        self._letzter_lauf: dict[str, datetime] = {}
        self._letzter_event_zaehler: dict[str, int] = {}
        self._letztes_ergebnis: dict[str, dict] = {}
        # T-0108: Job-globale Fehler-/Erfolg-Sichtbarkeit fuer
        # /api/ml/status. `_letzter_fehler` deckt sowohl den globalen
        # Daten-Fehler (`_baue_trainingsdaten`) als auch einen
        # Zone-Retrain-Crash ab.
        self._letzter_erfolg: datetime | None = None
        self._letzter_fehler: dict | None = None

    @property
    def letztes_ergebnis(self) -> dict[str, dict]:
        return self._letztes_ergebnis

    @property
    def letzter_erfolg(self) -> datetime | None:
        """Zeitpunkt des letzten fehlerfreien Laufs (T-0108)."""
        return self._letzter_erfolg

    @property
    def letzter_fehler(self) -> dict | None:
        """`{zeit, typ, nachricht}` des letzten Crashs, sonst None (T-0108)."""
        return self._letzter_fehler

    # --- Trigger ---

    def _ist_faellig(self, zone_id: str, events_gesamt: int, jetzt: datetime) -> str | None:
        """Rueckgabe: Trigger-Grund ('zeit' | 'events' | None)."""
        letzter = self._letzter_lauf.get(zone_id)
        if letzter is None:
            return "initial"
        if jetzt - letzter >= self._intervall:
            return "zeit"
        seit = events_gesamt - self._letzter_event_zaehler.get(zone_id, events_gesamt)
        if seit >= self._resp.retrain_event_schwelle:
            return "events"
        return None

    # --- Haupt-Zyklus ---

    async def aktualisiere_wenn_faellig(self, jetzt: datetime | None = None) -> bool:
        """Laeuft ueber alle Zonen, retrainiert Faellige. True wenn mind. einer lief."""
        if not self._resp.aktiv:
            return False
        jetzt = jetzt or datetime.now()

        try:
            df = await self._baue_trainingsdaten(jetzt)
        except Exception as exc:
            logger.exception("ml.response_retrain.daten_fehler")
            # T-0108: globaler Daten-Fehler war vorher still (nur Log).
            self._letzter_fehler = {
                "zeit": jetzt.isoformat(),
                "typ": type(exc).__name__,
                "nachricht": str(exc)[:300],
            }
            return False
        if df is None or df.empty:
            return False

        irgendein_lauf = False
        fehler_zone: dict | None = None
        for zone in self._konfig.zonen:
            n_zone = int((df["zone_id"] == zone.zone_id).sum())
            trigger = self._ist_faellig(zone.zone_id, n_zone, jetzt)
            if trigger is None:
                continue
            ergebnis = await self._retrain_zone(zone.zone_id, df, jetzt, trigger)
            self._letztes_ergebnis[zone.zone_id] = ergebnis
            if ergebnis.get("status") == "fehler":
                fehler_zone = ergebnis
            self._letzter_lauf[zone.zone_id] = jetzt
            self._letzter_event_zaehler[zone.zone_id] = n_zone
            irgendein_lauf = True
        # T-0108: Job-globalen Fehler-/Erfolg-Status nachziehen.
        if fehler_zone is not None:
            self._letzter_fehler = {
                "zeit": fehler_zone.get("zeit", jetzt.isoformat()),
                "typ": fehler_zone.get("fehler_typ", "Fehler"),
                "nachricht": (
                    f"Zone {fehler_zone.get('zone_id')}: "
                    f"{fehler_zone.get('fehler_nachricht', '')}"
                )[:300],
            }
        elif irgendein_lauf:
            self._letzter_erfolg = jetzt
            self._letzter_fehler = None
        return irgendein_lauf

    async def _baue_trainingsdaten(self, jetzt: datetime):
        from bewaesserung.ml.response_features import erstelle_response_features

        von = jetzt - timedelta(days=365)
        df = await erstelle_response_features(
            self._speicher, self._konfig, von=von, bis=jetzt,
        )
        return df

    async def _retrain_zone(
        self, zone_id: str, df, jetzt: datetime, trigger: str,
    ) -> dict:
        from bewaesserung.ml.response_training import ResponseTrainingsPipeline
        from bewaesserung.ml.response_vorhersage import MLResponseService

        # Alten Zustand sichern — bei Gate-Rejection rollen wir zurueck.
        zone_dir = self._basis / zone_id
        alte_mae = _lade_inverse_mae(zone_dir, zone_id)

        pipeline = ResponseTrainingsPipeline(
            zone_id=zone_id,
            basis_verzeichnis=self._basis,
            min_events=max(1, self._resp.min_events),
        )

        # Snapshot der alten Symlinks + metadata (fuer Rollback).
        snapshot = _snapshot_aktuell(zone_dir, zone_id)

        try:
            erg = await asyncio.to_thread(pipeline.lauf, df, jetzt)
        except Exception as exc:
            logger.exception(
                "ml.response_retrain.fehler zone=%s", zone_id,
            )
            return {
                "status": "fehler", "zone_id": zone_id,
                "trigger": trigger,
                "fehler_typ": type(exc).__name__,
                "fehler_nachricht": str(exc)[:500],
                "zeit": jetzt.isoformat(),
            }

        if erg.status != "ok":
            return {
                "status": erg.status, "zone_id": zone_id, "trigger": trigger,
                "grund": erg.grund, "n_events": erg.n_events,
                "zeit": jetzt.isoformat(),
            }

        # Gate: nur pruefen, wenn es ein altes Modell gab.
        neue_mae = erg.inverse_mae_s
        if neue_mae is None:
            _rollback_aktuell(zone_dir, zone_id, snapshot)
            return {
                "status": "uebersprungen", "zone_id": zone_id,
                "trigger": trigger,
                "grund": "keine_inverse_mae",
                "n_events": erg.n_events,
                "zeit": jetzt.isoformat(),
            }
        if alte_mae is not None and neue_mae is not None:
            schwelle = self._resp.gate_mae_faktor * alte_mae
            if neue_mae > schwelle:
                # Rollback.
                _rollback_aktuell(zone_dir, zone_id, snapshot)
                logger.warning(
                    "ml.response_retrain.abgelehnt zone=%s mae_alt=%.2f "
                    "mae_neu=%.2f schwelle=%.2f",
                    zone_id, alte_mae, neue_mae, schwelle,
                )
                return {
                    "status": "abgelehnt", "zone_id": zone_id,
                    "trigger": trigger,
                    "mae_alt_inverse_s": round(alte_mae, 1),
                    "mae_neu_inverse_s": round(neue_mae, 1),
                    "gate_mae_faktor": self._resp.gate_mae_faktor,
                    "n_events": erg.n_events,
                    "zeit": jetzt.isoformat(),
                }

        # Service-Cache pingen, damit Live-Inferenz das neue Modell sieht.
        try:
            svc = MLResponseService.instanz(self._basis)
            svc.lade_zone(zone_id, force=True)
        except Exception:
            logger.exception("ml.response_retrain.service_reload_fehler zone=%s", zone_id)

        logger.info(
            "ml.response_retrain.uebernommen zone=%s version=%s "
            "mae_alt=%s mae_neu=%s n_events=%d trigger=%s",
            zone_id, erg.version, alte_mae, neue_mae, erg.n_events, trigger,
        )
        return {
            "status": "uebernommen", "zone_id": zone_id, "trigger": trigger,
            "mae_alt_inverse_s": round(alte_mae, 1) if alte_mae is not None else None,
            "mae_neu_inverse_s": round(neue_mae, 1) if neue_mae is not None else None,
            "forward_metriken": erg.forward_metriken,
            "version": erg.version, "n_events": erg.n_events,
            "zeit": jetzt.isoformat(),
        }


# --- Helper ---


def _lade_inverse_mae(zone_dir: Path, zone_id: str) -> float | None:
    """Liest `inverse_mae_s` aus der aktuellen Metadata-Datei."""
    meta_link = zone_dir / f"aktuell_{zone_id}_metadata.json"
    if not meta_link.exists():
        return None
    try:
        daten = json.loads(meta_link.read_text())
        mae = daten.get("inverse_mae_s")
        return float(mae) if mae is not None else None
    except (OSError, ValueError):
        return None


def _snapshot_aktuell(zone_dir: Path, zone_id: str) -> dict:
    """Merkt die aktuellen Symlink-Ziele fuer einen moeglichen Rollback."""
    snapshot: dict[str, str] = {}
    if not zone_dir.exists():
        return snapshot
    for kind in ("forward_q10", "forward_q50", "forward_q90", "inverse"):
        link = zone_dir / f"aktuell_{zone_id}_{kind}.lgbm"
        if link.is_symlink():
            try:
                snapshot[kind] = str(link.readlink())
            except OSError:
                pass
    meta_link = zone_dir / f"aktuell_{zone_id}_metadata.json"
    if meta_link.is_symlink():
        try:
            snapshot["metadata"] = str(meta_link.readlink())
        except OSError:
            pass
    return snapshot


def _rollback_aktuell(zone_dir: Path, zone_id: str, snapshot: dict) -> None:
    """Stellt alte Symlinks wieder her (nach Gate-Rejection)."""
    if not snapshot:
        return
    for kind in ("forward_q10", "forward_q50", "forward_q90", "inverse"):
        link = zone_dir / f"aktuell_{zone_id}_{kind}.lgbm"
        ziel = snapshot.get(kind)
        if ziel is None:
            continue
        try:
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(ziel)
        except OSError:
            logger.exception(
                "ml.response_retrain.rollback_fehler zone=%s kind=%s",
                zone_id, kind,
            )
    meta_ziel = snapshot.get("metadata")
    meta_link = zone_dir / f"aktuell_{zone_id}_metadata.json"
    if meta_ziel:
        try:
            if meta_link.exists() or meta_link.is_symlink():
                meta_link.unlink()
            meta_link.symlink_to(meta_ziel)
        except OSError:
            logger.exception(
                "ml.response_retrain.rollback_metadata_fehler zone=%s", zone_id,
            )
