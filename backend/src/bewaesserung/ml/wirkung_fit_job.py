"""T-0292 Stufe 2: Fit-Job fuer die Plateau-Wirkungs-Parameter (wmax/r0).

Pro Zone:
  1. Die schon quality-gefilterten ``(dauer_min, delta_pp)``-Paare aus
     den ``wirkungsrate``-Kalibrierungs-Records (T-0085) holen. Diese
     Records speichern ``wert`` = rate (= delta/dauer_min) +
     ``basis_mm`` = dauer_min, also ``delta = wert * basis_mm``. Sie
     haben bereits den vollen Wirkungsraten-Filter aus
     ``kalibrierung._scan_wirkungsrate`` durchlaufen (kein Regen /
     Cluster-Konkurrenz / Pre-/Post-Saturierung, plausible Dauer,
     delta in der 5pp-Quantisierungs-Range). Es gibt daher KEINE
     zweite Roh-Event-Extraktion -- Single Source of Truth.
  2. ``fitte_plateau`` (ml/wirkung_fit.py) mit strengem Quality-Gate.
  3. UPSERT in Tabelle ``wirkung_fit`` -- IMMER, auch wenn der Fit
     abgelehnt wird (``angenommen=False``), damit ``/api/ml/status``
     den Ablehnungsgrund zeigen kann.

T-0108-Pattern: ``letzter_erfolg`` / ``letzter_fehler`` sichtbar in
``/api/ml/status``. Read-only bzgl. der Entscheidung: aendert NICHTS an
``pruefe_zone`` / ``vorhersage_zone`` / ``_berechne_dauer``, solange
``MlWirkungFitKonfig.adoptieren`` False ist (Default). Erst mit
``adoptieren=True`` liest ``entscheidung._aufgeloeste_wirkung`` die
hier persistierten Fits.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from bewaesserung.ml.wirkung_fit import fitte_plateau
from bewaesserung.modelle import GesamtKonfig, MlWirkungFitKonfig
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

TYP_WIRKUNGSRATE = "wirkungsrate"
# Obergrenze fuer den DB-Read pro Zone. Bei ~1-2 Wirkungsraten-Events/Tag
# und 60 Tagen Fenster liegt die reale Anzahl weit darunter; 500 ist ein
# safety-Cap, kein erwarteter Normalfall.
_MAX_RECORDS = 500


class WirkungFitJob:
    """Periodischer Fit-Job fuer Tabelle ``wirkung_fit``."""

    def __init__(
        self,
        speicher: Speicher,
        konfig: GesamtKonfig,
        wirkung_konfig: MlWirkungFitKonfig,
    ) -> None:
        self._speicher = speicher
        self._konfig = konfig
        self._wf = wirkung_konfig
        self._intervall = timedelta(hours=wirkung_konfig.intervall_stunden)
        self._letzte_aktualisierung: datetime | None = None
        self._letzter_erfolg: datetime | None = None
        self._letzter_fehler: dict | None = None

    @property
    def letzter_erfolg(self) -> datetime | None:
        return self._letzter_erfolg

    @property
    def letzter_fehler(self) -> dict | None:
        return self._letzter_fehler

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> bool:
        if not self._wf.aktiv:
            return False
        jetzt = jetzt or datetime.now()
        if (
            self._letzte_aktualisierung
            and jetzt - self._letzte_aktualisierung < self._intervall
        ):
            return False
        try:
            await self._scan_alle_zonen(jetzt)
        except Exception as exc:  # noqa: BLE001
            logger.exception("wirkung_fit.fehler")
            self._letzter_fehler = {
                "zeit": jetzt.isoformat(),
                "typ": type(exc).__name__,
                "nachricht": str(exc)[:300],
            }
            return False
        self._letzte_aktualisierung = jetzt
        self._letzter_erfolg = jetzt
        self._letzter_fehler = None
        return True

    async def _scan_alle_zonen(self, jetzt: datetime) -> None:
        von = jetzt - timedelta(days=self._wf.fenster_tage)
        gefittet = 0
        angenommen = 0
        for zone in self._konfig.zonen:
            paare = await self._sammle_paare(zone.zone_id, von)
            if not paare:
                continue
            fit = fitte_plateau(
                paare,
                min_n=self._wf.min_n,
                min_dauer_spread=self._wf.min_dauer_spread,
                min_r2=self._wf.min_r2,
                max_mse_pp2=self._wf.max_mse_pp2,
                konsens_toleranz=self._wf.konsens_toleranz,
            )
            await self._speicher.upsert_wirkung_fit(
                zone_id=zone.zone_id,
                wmax=fit.wmax,
                r0=fit.r0,
                tau=fit.tau,
                n=fit.n,
                r2=fit.r2,
                mse=fit.mse,
                angenommen=fit.angenommen,
                grund=fit.grund,
                gefittet_am=jetzt,
            )
            gefittet += 1
            if fit.angenommen:
                angenommen += 1
            logger.info(
                "wirkung_fit.zone",
                zone_id=zone.zone_id,
                n=fit.n,
                wmax=fit.wmax,
                r0=fit.r0,
                r2=fit.r2,
                mse=fit.mse,
                angenommen=fit.angenommen,
                grund=fit.grund,
            )
        if gefittet:
            logger.info(
                "wirkung_fit.abgeschlossen",
                zonen=gefittet,
                angenommen=angenommen,
            )

    async def _sammle_paare(
        self, zone_id: str, von: datetime,
    ) -> list[tuple[float, float]]:
        """Rekonstruiert die ``(dauer_min, delta_pp)``-Paare aus den
        ``wirkungsrate``-Kalibrierungs-Records (``delta = wert * basis_mm``).
        """
        try:
            eintraege = await self._speicher.hole_kalibrierungen(
                zone_id=zone_id, typ=TYP_WIRKUNGSRATE, limit=_MAX_RECORDS,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "wirkung_fit.kalibrierung_fehler", zone_id=zone_id,
            )
            return []
        paare: list[tuple[float, float]] = []
        for e in eintraege:
            ts_raw = e.get("zeitstempel")
            dauer_min = e.get("basis_mm")
            rate = e.get("wert")
            if ts_raw is None or dauer_min is None or rate is None:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw)
            except (TypeError, ValueError):
                continue
            if ts < von:
                continue
            dauer_min_f = float(dauer_min)
            if dauer_min_f <= 0:
                continue
            delta = float(rate) * dauer_min_f
            paare.append((dauer_min_f, delta))
        return paare
