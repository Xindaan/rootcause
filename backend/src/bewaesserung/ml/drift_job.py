"""Periodischer Job, der offene ML-Prognosen gegen Sensor-Messungen evaluiert (T-0047).

Jede Live-Inferenz aus `MLVorhersageService.live_vorhersage()` landet in
`ml_vorhersage_log`. Der `MlDriftJob` laeuft im Entscheidungs-Loop und setzt
Ist-Feuchte + Abweichung fuer Zeilen, deren `prognose_ziel_zeit` weit genug
in der Vergangenheit liegt, um eine passende Messung zu finden.

Ein rollender MAE kann dann ueber `Speicher.hole_drift_metriken()` oder den
Endpoint `/api/ml/drift` aus diesen Zeilen aggregiert werden.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

STANDARD_INTERVALL_STUNDEN = 1
STANDARD_TOLERANZ_MINUTEN = 30
# Pro Drift-Tick maximal so viele Zeilen abarbeiten (Async-Loop-Schutz).
# 5000 Zeilen ~ 5 s Block, vertretbar bei 1×/h-Tick. Bei groesserem Backlog
# laeuft die Catchup-Schleife mehrfach im selben Tick — Hardcap CATCHUP_MAX_RUNS
# verhindert, dass der Loop-Thread zu lange haengt, wenn DB ungewoehnlich
# langsam wird.
STANDARD_BATCH_LIMIT = 5000
CATCHUP_MAX_RUNS = 10


class MlDriftJob:
    """Evaluiert faellige Drift-Log-Zeilen im Intervall-Gate."""

    def __init__(
        self,
        speicher: Speicher,
        intervall_stunden: int = STANDARD_INTERVALL_STUNDEN,
        toleranz_minuten: int = STANDARD_TOLERANZ_MINUTEN,
        batch_limit: int = STANDARD_BATCH_LIMIT,
        catchup_max_runs: int = CATCHUP_MAX_RUNS,
    ):
        self._speicher = speicher
        self._intervall = timedelta(hours=intervall_stunden)
        self._toleranz_minuten = toleranz_minuten
        self._batch_limit = batch_limit
        self._catchup_max_runs = catchup_max_runs
        self._letzte_aktualisierung: datetime | None = None

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> bool:
        """Laeuft hoechstens einmal pro Intervall. True wenn ausgefuehrt.

        Bei grossem Backlog (z. B. nach Backend-Offline-Tagen) laeuft
        intern eine Catchup-Schleife: solange ein Aufruf von
        `evaluiere_offene_vorhersagen` >= batch_limit Zeilen liefert,
        nochmal aufrufen — Hardcap `catchup_max_runs`. Abbruch auch
        wenn `aktualisiert == 0` (= keine evaluierbaren Zeilen mehr,
        Rest bleibt offen z. B. wegen fehlender Sensor-Messung in der
        Toleranz; die warten auf einen spaeteren Zyklus mit mehr Daten
        oder werden von einem manuellen Backfill aufgeraeumt).
        """
        jetzt = jetzt or datetime.now()
        if (self._letzte_aktualisierung
                and jetzt - self._letzte_aktualisierung < self._intervall):
            return False
        try:
            n_gesamt = 0
            cursor: str | None = None
            for run in range(self._catchup_max_runs):
                n, neuer_cursor = await self._speicher.evaluiere_offene_vorhersagen(
                    jetzt,
                    toleranz_minuten=self._toleranz_minuten,
                    limit=self._batch_limit,
                    nach_ziel_zeit=cursor,
                )
                n_gesamt += n
                # Abbruch-Bedingungen (in Reihenfolge):
                # 1. Cursor hat sich nicht bewegt → SELECT lieferte nichts
                #    Neues, Backlog ist leer.
                # 2. Aktualisiert war 0 UND keine neuen Zeilen kamen →
                #    nichts mehr fetchbar (analog zu `n_fetched == 0`).
                # T-0568: gegen den GERADE benutzten Cursor vergleichen.
                # Vorher wurde `vorheriger_cursor` erst NACH dem Vergleich
                # gesetzt, der Test lief also gegen den vorvorigen Wert --
                # bei stehendem Cursor brauchte die Schleife drei Runden
                # statt zwei. Durch `CATCHUP_MAX_RUNS` begrenzt, deshalb
                # Performance und keine Korrektheit.
                if neuer_cursor is None or neuer_cursor == cursor:
                    break
                cursor = neuer_cursor
            else:
                # for-else: Hardcap erreicht.
                logger.warning(
                    "ml.drift_job.catchup_hardcap_erreicht",
                    runs=self._catchup_max_runs,
                    zeilen=n_gesamt,
                )
            n = n_gesamt
        except Exception:
            # Fehler-Isolation: ein DB-Schluckauf darf den Service nicht
            # kippen. _letzte_aktualisierung bleibt ungesetzt, der naechste
            # Zyklus versucht es erneut.
            logger.exception("ml.drift_job.fehler")
            return False

        # T-0065: zweite Drift-Routine fuer ml_dauer_vorschlag — gleicht
        # Heuristik- und ML-Dauer-Empfehlungen gegen die gemessene 6h-Delta
        # aus dem Feuchte-Sensor ab.
        n_dauer = 0
        try:
            n_dauer = await self._speicher.evaluiere_offene_dauer_vorschlaege(
                jetzt, toleranz_minuten=self._toleranz_minuten,
            )
        except Exception:
            logger.exception("ml.dauer_drift_job.fehler")

        self._letzte_aktualisierung = jetzt
        if n or n_dauer:
            logger.info(
                "ml.drift_job.evaluiert",
                zeilen=n, zeilen_dauer=n_dauer,
            )
        return True
