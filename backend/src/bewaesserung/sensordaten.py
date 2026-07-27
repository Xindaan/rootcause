"""Sensordaten-Verarbeitung — Validierung und Speicherung.

Empfaengt SensorMessung-Objekte vom GardenaClient-Callback,
validiert die Werte und speichert sie in der Datenbank.
Optional: Gibt strukturierte Log-Ausgaben fuer das Terminal.
"""

import asyncio
from datetime import timedelta

import structlog

from bewaesserung.modelle import DatenQuelle, SensorMessung
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()


class SensorDatenVerarbeiter:
    """Verarbeitet und speichert eingehende Sensordaten."""

    # Auch bei unveraenderten Werten mindestens alle MAX_DUPLIKAT_INTERVALL speichern
    # (wichtig fuer lueckenlose Charts und ML-Features wie Rolling Averages)
    MAX_DUPLIKAT_INTERVALL = timedelta(hours=1)

    def __init__(self, speicher: Speicher):
        self._speicher = speicher
        self._letzte_werte: dict[str, SensorMessung] = {}  # zone_id -> letzte Messung
        self._locks: dict[str, asyncio.Lock] = {}  # zone_id -> Lock (Race-Schutz)

    def _hole_lock(self, zone_id: str) -> asyncio.Lock:
        """Gibt den Lock fuer eine Zone zurueck (lazy erzeugt)."""
        if zone_id not in self._locks:
            self._locks[zone_id] = asyncio.Lock()
        return self._locks[zone_id]

    async def verarbeite(self, messung: SensorMessung) -> None:
        """Callback fuer den GardenaClient — validiert und speichert eine Messung.

        Args:
            messung: Sensormessung vom Gardena-Callback.
        """
        # Plausibilitaetspruefung (vor Lock — guenstig)
        if not self._ist_plausibel(messung):
            logger.warning(
                "sensor.unplausibel",
                zone_id=messung.zone_id,
                feuchte=messung.boden_feuchte,
                temp=messung.boden_temperatur,
            )
            return

        async with self._hole_lock(messung.zone_id):
            # Nur speichern wenn sich Werte geaendert haben (Gardena sendet teils Duplikate)
            # Aber: mindestens alle MAX_DUPLIKAT_INTERVALL, damit keine Luecken entstehen
            vorherige = self._letzte_werte.get(messung.zone_id)
            if vorherige and self._ist_duplikat(vorherige, messung):
                alter = messung.zeitstempel - vorherige.zeitstempel
                if alter < self.MAX_DUPLIKAT_INTERVALL:
                    logger.debug("sensor.duplikat_uebersprungen", zone_id=messung.zone_id)
                    return

            # Speichern
            await self._speicher.speichere_messung(messung)
            self._letzte_werte[messung.zone_id] = messung

        # Terminal-Ausgabe (ausserhalb Lock)
        logger.info(
            "sensor.messung",
            zone=messung.zone_id,
            feuchte=f"{messung.boden_feuchte}%" if messung.boden_feuchte is not None else "-",
            boden_temp=f"{messung.boden_temperatur}°C" if messung.boden_temperatur is not None else "-",
            luft_temp=f"{messung.umgebungs_temperatur}°C" if messung.umgebungs_temperatur is not None else "-",
            licht=messung.licht_intensitaet,
            batterie=f"{messung.batterie_prozent}%" if messung.batterie_prozent is not None else "-",
        )

    def _ist_plausibel(self, messung: SensorMessung) -> bool:
        """Prueft ob die Messwerte in einem realistischen Bereich liegen."""
        # Gardena-Cloud schickt zwischen Voll-Ticks gelegentlich nur
        # einen temperature-Beat ohne humidity. Solche Zeilen mit
        # boden_feuchte=None wuerden das Feuchte-Diagramm im Frontend
        # reissen lassen und in `boden_feuchte`-basierten ML-/Bilanz-
        # Queries als Loch erscheinen. Fuer FYTA gilt das nicht — dort
        # ist `boden_feuchte` immer Pflicht-Bestandteil eines Telemetrie-
        # Punkts. Siehe auch sensor_dhs_backfill: Bucket-Merge skippt
        # diese Beats schon dort; dies hier deckt zusaetzlich den
        # Live-WebSocket-Pfad ab (Isomorphie-Check beider Quellen).
        if (
            messung.quelle == DatenQuelle.GARDENA
            and messung.boden_feuchte is None
        ):
            return False

        if messung.boden_feuchte is not None:
            if messung.boden_feuchte < 0 or messung.boden_feuchte > 100:
                return False

        if messung.boden_temperatur is not None:
            if messung.boden_temperatur < -30 or messung.boden_temperatur > 70:
                return False

        if messung.umgebungs_temperatur is not None:
            if messung.umgebungs_temperatur < -40 or messung.umgebungs_temperatur > 60:
                return False

        return True

    def _ist_duplikat(self, alt: SensorMessung, neu: SensorMessung) -> bool:
        """Prueft ob eine Messung ein Duplikat der vorherigen ist."""
        return (
            alt.boden_feuchte == neu.boden_feuchte
            and alt.boden_temperatur == neu.boden_temperatur
            and alt.umgebungs_temperatur == neu.umgebungs_temperatur
            and alt.licht_intensitaet == neu.licht_intensitaet
            and alt.batterie_prozent == neu.batterie_prozent
            and alt.boden_fruchtbarkeit == neu.boden_fruchtbarkeit
            and alt.licht == neu.licht
        )

    def hole_letzten_wert(self, zone_id: str) -> SensorMessung | None:
        """Gibt den letzten bekannten Messwert fuer eine Zone zurueck (aus Cache)."""
        return self._letzte_werte.get(zone_id)
