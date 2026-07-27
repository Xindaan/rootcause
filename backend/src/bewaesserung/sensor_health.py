"""Sensor-Gesundheitsueberwachung.

Erkennt Sensor-Ausfaelle (keine Daten seit X Stunden) und niedrige Batterien.
Persistiert Warnungen fuer den Ops-Tab mit offen/behoben-Lifecycle.
"""

from datetime import datetime

import structlog

from bewaesserung.modelle import DatenQuelle, SensorWarnung, SensorWarnungTyp
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

# Schwellenwerte
SENSOR_TIMEOUT_STUNDEN = 3       # Gardena: Kein Update seit X Stunden = Ausfall
FYTA_SENSOR_TIMEOUT_STUNDEN = 12  # FYTA meldet oft seltener als Gardena
BATTERIE_WARNUNG_PROZENT = 20.0  # Unter X% = Warnung
BATTERIE_KRITISCH_PROZENT = 10.0  # Unter X% = Kritisch


class SensorHealthMonitor:
    """Ueberwacht Sensor-Gesundheit und loggt Warnungen."""

    def __init__(
        self,
        speicher: Speicher,
        ausfall_schwelle_pro_zone: dict[str, int] | None = None,
    ):
        """T-0214: optionale Pro-Zone-Ueberschreibung der globalen
        Ausfall-Schwellen. Caller (main.py) baut das Mapping aus
        `konfig.zonen[].ausfall_schwelle_stunden`. Fehlt eine Zone im
        Mapping -> globaler Default (3h Gardena, 12h FYTA).

        Hintergrund T-0214: Bluetooth-Only-FYTA-Sensoren wie
        mandevilla_maxi + pilea synchen nur bei User-Anwesenheit
        (alle 2-4 Tage), nicht ueber FYTA-Beam. Globale 12h-Schwelle
        markiert sie dauernd als 'ausfall' -- Signal-zu-Noise sinkt,
        weil die Warnung nie weg ist.
        """
        self._speicher = speicher
        self._ausfall_schwelle_pro_zone: dict[str, int] = dict(
            ausfall_schwelle_pro_zone or {}
        )

    async def pruefe_alle(self, zone_ids: list[str]) -> list[dict]:
        """Prueft alle Zonen auf Sensor-Gesundheit.

        Gibt nur neu geoeffnete Warnungen zurueck.
        """
        jetzt = datetime.now()
        warnungen: list[dict] = []

        for zone_id in zone_ids:
            messung = await self._speicher.letzte_messung(zone_id)

            # Sensor-Ausfall: Keine Messung oder zu alt
            if messung is None:
                warnung = await self._oeffne_warnung(
                    zone_id, "ausfall",
                    "Noch nie Daten empfangen",
                    jetzt,
                )
                if warnung:
                    warnungen.append(warnung)
            else:
                timeout_stunden = self._sensor_timeout_stunden(
                    messung.quelle, zone_id,
                )
                alter_stunden = (jetzt - messung.zeitstempel).total_seconds() / 3600
                if alter_stunden > timeout_stunden:
                    warnung = await self._oeffne_warnung(
                        zone_id, "ausfall",
                        f"Letztes Update vor {alter_stunden:.1f}h "
                        f"({messung.zeitstempel.strftime('%d.%m. %H:%M')})",
                        jetzt,
                    )
                    if warnung:
                        warnungen.append(warnung)
                else:
                    await self._speicher.schliesse_sensor_warnung(
                        zone_id, SensorWarnungTyp.AUSFALL, jetzt
                    )

            # Batterie-Check (nur wenn Wert vorhanden, z.B. FYTA hat keine Batterie)
            if messung is None or messung.batterie_prozent is None:
                await self._speicher.schliesse_sensor_warnung(
                    zone_id, SensorWarnungTyp.BATTERIE_NIEDRIG, jetzt
                )
                await self._speicher.schliesse_sensor_warnung(
                    zone_id, SensorWarnungTyp.BATTERIE_KRITISCH, jetzt
                )
                continue

            if messung.batterie_prozent < BATTERIE_KRITISCH_PROZENT:
                await self._speicher.schliesse_sensor_warnung(
                    zone_id, SensorWarnungTyp.BATTERIE_NIEDRIG, jetzt
                )
                warnung = await self._oeffne_warnung(
                    zone_id,
                    "batterie_kritisch",
                    f"Batterie {messung.batterie_prozent:.0f}% — Sofort wechseln!",
                    jetzt,
                )
                if warnung:
                    warnungen.append(warnung)
            elif messung.batterie_prozent < BATTERIE_WARNUNG_PROZENT:
                await self._speicher.schliesse_sensor_warnung(
                    zone_id, SensorWarnungTyp.BATTERIE_KRITISCH, jetzt
                )
                warnung = await self._oeffne_warnung(
                    zone_id,
                    "batterie_niedrig",
                    f"Batterie {messung.batterie_prozent:.0f}%",
                    jetzt,
                )
                if warnung:
                    warnungen.append(warnung)
            else:
                await self._speicher.schliesse_sensor_warnung(
                    zone_id, SensorWarnungTyp.BATTERIE_NIEDRIG, jetzt
                )
                await self._speicher.schliesse_sensor_warnung(
                    zone_id, SensorWarnungTyp.BATTERIE_KRITISCH, jetzt
                )

        return warnungen

    def _sensor_timeout_stunden(
        self, quelle: DatenQuelle, zone_id: str,
    ) -> int:
        """Waehlt einen quellenabhaengigen Stale-Timeout. T-0214:
        Pro-Zone-Override aus `ausfall_schwelle_pro_zone` hat Vorrang.

        Beispiel mandevilla_maxi/pilea sind FYTA-Indoor-Pflanzen ohne
        FYTA-Beam-Hub-Reichweite -- der User syncht alle 2-4 Tage per
        Handy-Bluetooth. Globale FYTA-12h-Schwelle markiert sie
        dauerhaft als 'ausfall'. Mit Pro-Zone-96h ist die Warnung nur
        noch ein echtes Sync-Erinnerung.
        """
        override = self._ausfall_schwelle_pro_zone.get(zone_id)
        if override is not None:
            return int(override)
        if quelle == DatenQuelle.FYTA:
            return FYTA_SENSOR_TIMEOUT_STUNDEN
        return SENSOR_TIMEOUT_STUNDEN

    async def _oeffne_warnung(
        self, zone_id: str, typ: str, details: str, jetzt: datetime
    ) -> dict | None:
        """Oeffnet eine Warnung genau einmal solange sie offen ist."""
        warnung = SensorWarnung(
            zeitstempel=jetzt,
            zone_id=zone_id,
            typ=SensorWarnungTyp(typ),
            details=details,
        )
        if not await self._speicher.oeffne_sensor_warnung(warnung):
            # T-0397 (F4): schon offen -> nur die `details` auffrischen (z.B.
            # die "vor Xh"-Angabe der Ausfall-Warnung), still, ohne Re-Log.
            await self._speicher.aktualisiere_offene_warn_details(
                zone_id, SensorWarnungTyp(typ), details,
            )
            return None

        if "kritisch" in typ:
            logger.error("sensor_health.kritisch", zone=zone_id, typ=typ, details=details)
        else:
            logger.warning("sensor_health.warnung", zone=zone_id, typ=typ, details=details)

        return {"zone_id": zone_id, "typ": typ, "details": details}
