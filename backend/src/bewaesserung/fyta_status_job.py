"""T-0527: schreibt den Geraete-Zustand der FYTA-Sensoren als Zeitreihe mit.

Warum es diesen Job gibt
------------------------
Die FYTA-Messreihe (`/user-plant/list-measurements`) fuehrt genau vier
Nutzwerte: soil_moisture, temperature, light, soil_fertility. Alles, was
den ZUSTAND des Geraets beschreibt -- Akkustand, Online-Flag, Firmware,
letzter Kontakt -- haengt am Pflanzen-Objekt und ist dort eine
Momentaufnahme ohne Historie. Bei einem toten Geraet friert sie auf dem
Stand des letzten Kontakts ein.

Genau daran scheiterte die Diagnose des Ausfalls "Hecke Faulbaum"
(08.08.2026): die Frage "wie lief der Akku VOR dem Ausfall?" liess sich
nicht beantworten, weil `battery_level` auf 100 stand und vom Ausfalltag
stammte. Ein einzelner Messpunkt beweist nichts; erst die Reihe zeigt, ob
ein Wert faellt, springt oder feststeckt.

Takt
----
Der Abruf kostet 1 + N Requests (`battery_level` steht nur im
Detail-Endpoint), bei 15 Pflanzen also 16. Alle sechs Stunden sind vier
Stuetzpunkte am Tag -- genug fuer einen Akku-Trend in 5er-Schritten und
sparsam genug, um nicht als Polling aufzufallen.
"""

from datetime import datetime, timedelta

import structlog

from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

INTERVALL_STUNDEN_DEFAULT = 6


class FytaStatusJob:
    """Persistiert periodisch den FYTA-Geraetestatus."""

    def __init__(
        self,
        speicher: Speicher,
        fyta_client,
        intervall_stunden: int = INTERVALL_STUNDEN_DEFAULT,
    ):
        self._speicher = speicher
        self._fyta_client = fyta_client
        self._intervall = timedelta(hours=intervall_stunden)
        self._letzte_aktualisierung: datetime | None = None
        self.letzter_fehler: str | None = None

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> int:
        """Laeuft max. einmal pro `intervall_stunden`.

        Returns: Anzahl geschriebener Geraete-Saetze (0 wenn nicht faellig).
        Wirft nie -- der Aufrufer ist der Entscheidungsloop.
        """
        jetzt = jetzt or datetime.now()
        if (
            self._letzte_aktualisierung
            and jetzt - self._letzte_aktualisierung < self._intervall
        ):
            return 0

        # Faelligkeit VOR der Arbeit stempeln, sonst schickt ein Fehler den
        # Job in eine Dauerschleife (Klasse
        # `fehlerpattern_teure_vorarbeit_vor_dem_gate`).
        self._letzte_aktualisierung = jetzt
        self.letzter_fehler = None

        try:
            saetze = await self._fyta_client.hole_geraete_status()
        except Exception as exc:
            self.letzter_fehler = str(exc)
            logger.exception("fyta_status.abruf_fehler")
            return 0

        geschrieben = 0
        auffaellig: list[str] = []
        for satz in saetze:
            try:
                await self._speicher.speichere_fyta_geraete_status(satz)
                geschrieben += 1
            except Exception as exc:
                self.letzter_fehler = str(exc)
                logger.exception(
                    "fyta_status.schreibfehler", geraet=satz.geraet_id,
                )
                continue
            # Auffaellig heisst hier: FYTA selbst haelt das Geraet fuer
            # abgehaengt. Nur geloggt, keine Warnung -- die Ausfall-Warnung
            # entsteht in `sensor_health` aus den Messdaten, und zwei
            # Quellen fuer dieselbe Aussage waeren eine zweite Wahrheit.
            if satz.is_outdated or satz.sensor_status == 2:
                auffaellig.append(satz.geraet_id)

        logger.info(
            "fyta_status.geschrieben",
            geraete=geschrieben,
            auffaellig=auffaellig or None,
        )
        return geschrieben
