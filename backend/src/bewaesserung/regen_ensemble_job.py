"""T-0423: periodischer Abruf des Regen-Ensembles (Shadow, read-only).

**Der Job entscheidet nichts.** Er sammelt die Verteilung, damit T-0425 spaeter
beantworten kann, ob der p20 besser entschieden haette als der deterministische
Punktwert. Erst wenn das belegt ist, wandert der Wert in die Bilanz (T-0422).

Intervall 60 min: icon_d2_eps laeuft 8x taeglich (alle 3 h). Haeufiger
abzufragen liefert dieselben Zahlen und belastet eine fremde Gratis-API ohne
Gegenwert. Der 5-min-Entscheidungsloop ruft nur das Intervall-Gate auf.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import structlog

from .wetter_ensemble import ENSEMBLE_URL, MODELL, perzentil_der_membersummen

logger = structlog.get_logger()

# Beide Horizonte speichern: dieselbe Abfrage liefert ueber 24 h und 48 h
# voellig verschiedene Verteilungen (22.07.: p20 0,0 gegen 1,2 -- der Regen lag
# im zweiten Tag). Welcher Horizont fuer welche Entscheidung taugt, soll
# T-0425 aus den Daten beantworten, nicht eine Vorab-Annahme.
HORIZONTE = (24, 48)


class RegenEnsembleJob:
    """Holt das Ensemble je Standort und schreibt die Verteilung fort."""

    INTERVALL_MIN = 60

    def __init__(self, speicher, standorte: list[tuple[str, float, float]]):
        # `standorte`: [(standort_id, breite, laenge)]
        self._speicher = speicher
        self._standorte = standorte
        self._letzter_lauf: datetime | None = None

    async def _hole(self, breite: float, laenge: float) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                antwort = await client.get(ENSEMBLE_URL, params={
                    "latitude": breite, "longitude": laenge,
                    "hourly": "precipitation", "models": MODELL,
                    "forecast_days": 2, "timezone": "Europe/Berlin",
                })
                antwort.raise_for_status()
                return antwort.json()
        except Exception as exc:  # noqa: BLE001
            # Ein Ausfall ist folgenlos -- der Job ist Shadow. Keine
            # Eskalation, nur eine Zeile, damit Luecken erklaerbar sind.
            logger.warning("regen_ensemble.abruf_fehlgeschlagen",
                           fehler=str(exc), breite=breite, laenge=laenge)
            return None

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> int:
        jetzt = jetzt or datetime.now()
        if self._letzter_lauf is not None:
            if (jetzt - self._letzter_lauf).total_seconds() < self.INTERVALL_MIN * 60:
                return 0
        self._letzter_lauf = jetzt

        geschrieben = 0
        for standort_id, breite, laenge in self._standorte:
            daten = await self._hole(breite, laenge)
            if not daten:
                continue
            hourly = daten.get("hourly") or {}
            # Deterministischer Vergleichswert: die erste Member-Spalte ist
            # der Kontrolllauf. Er wird NICHT zum Rechnen benutzt, sondern
            # nur mitgeschrieben, damit der Kernbefund (Punktwert liegt unter
            # dem p25 des Ensembles) laufend nachpruefbar bleibt.
            det = hourly.get("precipitation")
            for stunden in HORIZONTE:
                ens = perzentil_der_membersummen(hourly, stunden=stunden)
                if ens is None:
                    continue
                det_summe = (
                    sum(v or 0.0 for v in det[:stunden]) if det else None
                )
                await self._speicher.speichere_regen_ensemble(
                    jetzt, standort_id, stunden, ens, det_summe,
                )
                geschrieben += 1
                logger.info(
                    "regen_ensemble.gespeichert",
                    standort=standort_id, horizont_h=stunden,
                    p20=round(ens.p20, 2), p50=round(ens.p50, 2),
                    spread=round(ens.spread, 2), n_member=ens.n_member,
                    deterministisch=(round(det_summe, 2)
                                     if det_summe is not None else None),
                )
        return geschrieben
