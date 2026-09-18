"""T-0584: schreibt den Zustand der Gardena-Ventile mit und warnt bei Fehlern.

Warum es diesen Job gibt
------------------------
Die Gardena-App zeigte fuer die Hecke-Einheit "Steuerteil geloest". Wie lange
schon, konnte niemand sagen -- wir holten diesen Zustand gar nicht ab. Aus dem
WebSocket nahmen wir nur `valve_activity` (offen/zu); `state` und
`lastErrorCode` liegen am selben Objekt und wurden ignoriert.

Die Wirkung war zu dem Zeitpunkt messbar KEINE: der Hecke-Lead reagierte auf
jeden der 25 Laeufe der letzten vier Wochen. Das Steuerteil sass also lose
genug fuer eine Warnung und fest genug zum Giessen. Faellt es ab, wuerde unser
erstes Signal eine ausbleibende Sensorreaktion sein -- ein bis zwei Tage
spaeter (`fehlerpattern_empfehlung_ohne_hardware_check`: die Zahlen bleiben
plausibel, waehrend sich der Raum aendert).

Kosten und Takt
---------------
Der Snapshot ist ein Speicher-Lesevorgang im Client, **kein HTTP-Abruf** --
also kein Polling und kein Soft-Ban-Risiko. Geschrieben wird trotzdem nicht
jeden Zyklus: eine Zeile bei jeder Aenderung, sonst hoechstens alle
`HEARTBEAT_MINUTEN`. Das ergibt eine datierbare Reihe ohne 300 identische
Zeilen pro Ventil und Tag.

Was hier NICHT passiert
-----------------------
Der Giessbetrieb wird nicht gesperrt. Ein Ventilfehler ist eine Aussage des
Herstellers ueber Hardware, kein Sensordefekt und kein Bodenereignis; ob bei
losem Steuerteil noch Wasser ankommt, sagen die Sensoren besser als ein
Statuscode. Die Warnung ist fuer Menschen.

Welcher Code "Steuerteil geloest" bedeutet, ist offen (T-0584 AK5): solange
das nicht an Live-Daten gesehen ist, gilt JEDER Code als meldepflichtig, der
nicht in `CODES_OHNE_FEHLER` steht. Ein Filter auf einen geratenen Code waere
`fehlerpattern_detektor_erbt_die_zu_pruefende_praemisse`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from bewaesserung.modelle import (
    GardenaGeraeteStatus,
    SensorWarnung,
    SensorWarnungTyp,
)
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

#: Auch ohne Aenderung alle 30 min eine Zeile -- sonst ist nach einem Fehler
#: nicht unterscheidbar, ob der Zustand stabil war oder der Job stillstand.
HEARTBEAT_MINUTEN = 30

#: `state`-Werte, die KEIN Fehler sind. Alles andere (WARNING, ERROR,
#: UNAVAILABLE, ...) wird gemeldet.
STATES_OHNE_FEHLER = frozenset({"", "OK"})

#: `lastErrorCode`-Werte, die KEIN Fehler sind. "" heisst "nicht geliefert"
#: (py-smart-gardena setzt "N/A", das normalisiert der Client), "NO_MESSAGE"
#: ist die Gardena-Formulierung fuer "nichts anzuzeigen".
CODES_OHNE_FEHLER = frozenset({"", "NO_MESSAGE"})


def _ist_fehler(state: str, code: str) -> bool:
    """Ein Fehler liegt vor, wenn der Zustand nicht OK ist ODER ein Code steht."""
    return state not in STATES_OHNE_FEHLER or code not in CODES_OHNE_FEHLER


class GardenaStatusJob:
    """Persistiert den Gardena-Geraetestatus und oeffnet/schliesst Warnungen."""

    def __init__(
        self,
        speicher: Speicher,
        gardena_client,
        heartbeat_minuten: int = HEARTBEAT_MINUTEN,
    ):
        self._speicher = speicher
        self._client = gardena_client
        self._heartbeat = timedelta(minutes=heartbeat_minuten)
        #: (geraet_id, ventil_id) -> (Signatur, Zeitpunkt der letzten Zeile)
        self._zuletzt: dict[tuple[str, str], tuple[tuple, datetime]] = {}
        self.letzter_fehler: str | None = None

    async def aktualisiere(self, jetzt: datetime | None = None) -> int:
        """Snapshot lesen, bei Bedarf schreiben, Warnungen nachziehen.

        Returns: Anzahl geschriebener Zeilen. Wirft nie -- der Aufrufer ist der
        Entscheidungsloop (`fehlerpattern_job_nicht_in_loop_verdrahtet`).
        """
        jetzt = jetzt or datetime.now()
        self.letzter_fehler = None
        try:
            saetze = self._client.geraete_status_snapshot(jetzt)
        except Exception as exc:
            self.letzter_fehler = str(exc)
            logger.exception("gardena_status.snapshot_fehler")
            return 0

        geschrieben = 0
        for satz in saetze:
            try:
                if self._soll_schreiben(satz, jetzt):
                    await self._speicher.speichere_gardena_geraete_status(satz)
                    geschrieben += 1
            except Exception as exc:
                self.letzter_fehler = str(exc)
                logger.exception(
                    "gardena_status.schreibfehler",
                    geraet=satz.geraet_id, ventil=satz.ventil_id,
                )
            try:
                await self._pruefe_warnung(satz, saetze, jetzt)
            except Exception as exc:
                self.letzter_fehler = str(exc)
                logger.exception(
                    "gardena_status.warnfehler",
                    geraet=satz.geraet_id, ventil=satz.ventil_id,
                )
        return geschrieben

    # ---------------------------------------------------------------- schreiben

    @staticmethod
    def _signatur(satz: GardenaGeraeteStatus) -> tuple:
        """Alles ausser dem Zeitstempel -- daran haengt "hat sich was geaendert"."""
        return (
            satz.valve_state, satz.valve_activity, satz.last_error_code,
            satz.valve_set_state, satz.valve_set_last_error_code,
            satz.battery_level, satz.battery_state,
            satz.rf_link_level, satz.rf_link_state,
            satz.geraet_name, satz.ventil_name, satz.zone_id,
            satz.serial, satz.model_type,
        )

    def _soll_schreiben(
        self, satz: GardenaGeraeteStatus, jetzt: datetime,
    ) -> bool:
        schluessel = (satz.geraet_id, satz.ventil_id)
        signatur = self._signatur(satz)
        vorher = self._zuletzt.get(schluessel)
        if vorher is not None:
            alte_signatur, alte_zeit = vorher
            if signatur == alte_signatur and jetzt - alte_zeit < self._heartbeat:
                return False
        self._zuletzt[schluessel] = (signatur, jetzt)
        return True

    # ----------------------------------------------------------------- warnen

    async def _pruefe_warnung(
        self,
        satz: GardenaGeraeteStatus,
        alle: list[GardenaGeraeteStatus],
        jetzt: datetime,
    ) -> None:
        if satz.ventil_id:
            fehler = _ist_fehler(satz.valve_state, satz.last_error_code)
            zonen = [satz.zone_id] if satz.zone_id else []
            detail = self._detail_ventil(satz)
            bezug = satz.ventil_id
        else:
            fehler = _ist_fehler(
                satz.valve_set_state, satz.valve_set_last_error_code,
            )
            # Geraeteebene trifft ALLE Zonen an diesem Geraet (AK3).
            zonen = sorted({
                s.zone_id for s in alle
                if s.geraet_id == satz.geraet_id and s.ventil_id and s.zone_id
            })
            detail = self._detail_geraet(satz)
            bezug = satz.geraet_id

        for zone_id in zonen:
            if fehler:
                neu = await self._speicher.oeffne_sensor_warnung(SensorWarnung(
                    zeitstempel=jetzt, zone_id=zone_id,
                    typ=SensorWarnungTyp.GERAETE_FEHLER,
                    details=detail, geraet_id=bezug,
                ))
                if neu:
                    logger.warning(
                        "gardena_status.geraetefehler",
                        zone_id=zone_id, geraet=satz.geraet_id,
                        ventil=satz.ventil_id, detail=detail,
                    )
                else:
                    # Der Text traegt den aktuellen Code; ohne Auffrischen
                    # bliebe der erste Wortlaut stehen (T-0397).
                    auffrischen = getattr(
                        self._speicher, "aktualisiere_offene_warn_details", None,
                    )
                    if auffrischen is not None:
                        await auffrischen(
                            zone_id, SensorWarnungTyp.GERAETE_FEHLER, detail,
                            bezug,
                        )
            else:
                await self._speicher.schliesse_sensor_warnung(
                    zone_id, SensorWarnungTyp.GERAETE_FEHLER, jetzt, bezug,
                )

    @staticmethod
    def _detail_ventil(satz: GardenaGeraeteStatus) -> str:
        name = satz.ventil_name or satz.ventil_id
        teile = [f"Gardena meldet fuer Ventil {name}:"]
        if satz.valve_state not in STATES_OHNE_FEHLER:
            teile.append(f"Zustand {satz.valve_state}")
        if satz.last_error_code not in CODES_OHNE_FEHLER:
            teile.append(f"Fehlercode {satz.last_error_code}")
        teile.append(f"(Geraet {satz.geraet_name or satz.geraet_id})")
        return " ".join(teile)

    @staticmethod
    def _detail_geraet(satz: GardenaGeraeteStatus) -> str:
        name = satz.geraet_name or satz.geraet_id
        teile = [f"Gardena meldet fuer Geraet {name}:"]
        if satz.valve_set_state not in STATES_OHNE_FEHLER:
            teile.append(f"Ventilblock-Zustand {satz.valve_set_state}")
        if satz.valve_set_last_error_code not in CODES_OHNE_FEHLER:
            teile.append(f"Fehlercode {satz.valve_set_last_error_code}")
        if satz.battery_level is not None:
            teile.append(f"(Akku {satz.battery_level:.0f} %)")
        return " ".join(teile)
