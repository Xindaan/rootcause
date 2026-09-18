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

from .wetter_ensemble import (
    ENSEMBLE_URL,
    MODELL,
    brauchbare_indizes,
    perzentil_der_membersummen,
)

logger = structlog.get_logger()

# T-0516 (06.08.2026): nur noch 24 h. Der 48-h-Horizont ist ersatzlos raus,
# aus zwei zusammenwirkenden Gruenden:
#
# 1. **Er hatte nie einen Konsumenten.** `wasserbilanz_job._ensemble_urteil`
#    filtert hart auf `horizont_stunden == 24`; kein anderer Leser existiert.
#    Der urspruengliche Kommentar hier hielt ihn fuer T-0425 vor ("welcher
#    Horizont taugt, soll T-0425 beantworten"). T-0425 ist am 05.08.
#    beantwortet -- und hat den 48-h-Wert nicht gebraucht, weil die Regel an
#    `Dr` haengt und nicht am Regen-Perzentil.
# 2. **Er ist seit dem T-0425-Fix ohnehin unerfuellbar.** Gemessen 06.08.
#    04:06: `icon_d2_eps` liefert 72 Slots, ab Index 48 sind sie None. Ab
#    `jetzt` bleiben `48 - Abfragestunde` nutzbare Stunden -- die
#    Vollstaendigkeitspruefung schlaegt zu jeder Stunde ausser Mitternacht
#    zu. Der Job hat den Wert seit 04:03 kein einziges Mal geschrieben.
#
# Zusammen: ein Abruf, den niemand liest und der nichts liefert. Lieber
# ersatzlos weg als als toter Zweig stehen lassen
# ([[fehlerpattern_detektor_ohne_konsument]] -- hier die Umkehrung, ein
# Detektor, dessen Konsument nie kam).
#
# **Wer spaeter einen laengeren Vorlauf will**, faengt nicht hier an, sondern
# bei der Modellwahl: `icon_d2_eps` reicht nicht ueber 48 h ab Mitternacht
# hinaus. Das waere ein eigener Task mit `ecmwf_ifs025_ensemble` und der
# Frage, ob man zwei Modelle in einer Reihe mischen will
# ([[fehlerpattern_skalen_mix_multisensor_aggregat]]).
# T-0538 (13.08.2026): 6 h dazu. Die heutige Sperr-Regel liest einen
# 6-h-PUNKTWERT (`niederschlag_6h >= 2.0`), und der wackelt: gemessen ueber
# 1.304 aufeinanderfolgende Abfragepaare seit dem 15.07. kippte das Urteil
# "sperrt / sperrt nicht" **24-mal**, am 20.07. viermal in zwei Stunden.
# Um den Punktwert durch den p20 der Member-Summen ersetzen zu koennen,
# muss dieses Fenster erst einmal gesammelt werden -- ohne Reihe keine
# Akzeptanzpruefung ("Kipp-Rate sinkt messbar").
#
# Der 6-h-Horizont hat das Problem des gestrichenen 48-h-Horizonts NICHT:
# `icon_d2_eps` reicht ab Mitternacht 48 h weit, sechs Stunden ab `jetzt`
# liegen also immer im Modell -- auch spaet abends.
HORIZONTE = (6, 24)


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
                    # T-0425: 3 statt 2, s. EnsembleClient.hole -- ab `jetzt`
                    # gezaehlt reichen zwei Tage fuer keinen der Horizonte.
                    "forecast_days": 3, "timezone": "Europe/Berlin",
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
                ens = perzentil_der_membersummen(
                    hourly, stunden=stunden, jetzt=jetzt,
                )
                if ens is None:
                    continue
                # T-0425, Isomorphie zur Fensterkorrektur: `det[:stunden]`
                # trug denselben Fehler wie die Member-Summen und haette
                # sonst einen Punktwert aus einem ANDEREN Fenster neben die
                # Perzentile geschrieben -- der Vergleich, um den es in
                # T-0425 geht, waere damit gegenstandslos gewesen.
                indizes = brauchbare_indizes(hourly, stunden, jetzt=jetzt)
                det_summe = (
                    sum(det[i] or 0.0 for i in indizes)
                    if det and indizes else None
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
