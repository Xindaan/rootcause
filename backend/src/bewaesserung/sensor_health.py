"""Sensor-Gesundheitsueberwachung.

Erkennt Sensor-Ausfaelle (keine Daten seit X Stunden) und niedrige Batterien.
Persistiert Warnungen fuer den Ops-Tab mit offen/behoben-Lifecycle.
"""

from datetime import datetime, timedelta

import structlog

from bewaesserung.modelle import (
    EREIGNIS_WARNUNG_LEBENSDAUER_TAGE,
    DatenQuelle,
    SensorWarnung,
    SensorWarnungTyp,
)
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

# Schwellenwerte
SENSOR_TIMEOUT_STUNDEN = 3       # Gardena: Kein Update seit X Stunden = Ausfall
FYTA_SENSOR_TIMEOUT_STUNDEN = 12  # FYTA meldet oft seltener als Gardena
BATTERIE_WARNUNG_PROZENT = 20.0  # Unter X% = Warnung
BATTERIE_KRITISCH_PROZENT = 10.0  # Unter X% = Kritisch

# T-0526: Wie lange ein Geraet nach seiner letzten Messung noch als
# "erwartet" gilt. Danach faellt es aus der Ueberwachung und seine
# Ausfall-Warnung wird geschlossen.
#
# Die Grenze ist noetig, weil `sensor_health` die Soll-Bestueckung einer
# Zone nicht kennt: Gardena-Geraete kommen per Discovery, FYTA aus der
# Konfig, und ein dauerhaft ausgebauter Sensor wuerde sonst ewig eine
# offene Warnung tragen -- exakt die Signal-zu-Noise-Falle, die T-0214
# fuer die Bluetooth-Sensoren geloest hat.
#
# 30 Tage, weil der laengste legitime Sync-Abstand im Bestand bei ~4 Tagen
# liegt (Bluetooth-Only-Sensoren, Pro-Zone-Override 96 h) und ein Geraet,
# das einen ganzen Monat schweigt, kein Ausfall mehr ist, sondern eine
# Konfig-Aenderung. Der Uebergang wird geloggt, nicht verschluckt.
GERAET_ERWARTET_TAGE = 30


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

    async def pruefe_alle(
        self, zone_ids: list[str], jetzt: datetime | None = None,
    ) -> list[dict]:
        """Prueft alle Zonen auf Sensor-Gesundheit.

        T-0546: raeumt zuerst abgelaufene EREIGNIS-Warnungen weg. Dieser Ort
        ist bewusst gewaehlt -- hier laeuft ohnehin die Warn-Hygiene der
        Zustands-Typen, und ein eigener Job waere eine weitere Schleife fuer
        einen Einzeiler. `jetzt` ist durchgereicht, damit Tests nicht von der
        Uhr abhaengen.

        Gibt nur neu geoeffnete Warnungen zurueck.

        T-0526: Ausfall und Batterie werden PRO GERAET geprueft, nicht pro
        Zone. Vorher fragte diese Schleife `letzte_messung(zone_id)` -- den
        juengsten Wert IRGENDEINES Sensors der Zone. In einer Zone mit
        mehreren Sensoren hielt damit jeder lebende Sensor die Zone
        "frisch", und ein totes Geraet loeste nie eine Warnung aus.

        Realfall, der das aufgedeckt hat: einer von drei Sensoren einer Zone
        lieferte ab einem Zeitpunkt nichts mehr. Die beiden anderen liefen
        weiter, die Zone galt damit als frisch. Vier Tage
        lang entstand keine einzige Ausfall-Warnung -- aufgefallen ist es
        nur, weil der Sensor in der FYTA-App fehlte. Derselbe Ausfall vom
        29.07. bis 03.08. blieb aus demselben Grund unbemerkt.
        """
        abgelaufen = await self._speicher.schliesse_abgelaufene_ereignis_warnungen(
            jetzt=jetzt,
        )
        if abgelaufen:
            logger.info(
                "sensor_health.ereignis_warnungen_abgelaufen",
                anzahl=abgelaufen,
                lebensdauer_tage=EREIGNIS_WARNUNG_LEBENSDAUER_TAGE,
            )

        # T-0571: `jetzt` war ab hier ueberschrieben -- der Parameter galt nur
        # fuer die Ereignis-Hygiene darueber, jede Ausfall- und
        # Batterie-Pruefung lief gegen die Wanduhr. Der Docstring versprach
        # das Gegenteil ("damit Tests nicht von der Uhr abhaengen"), und
        # Tests, die eine Zeit uebergaben, prueften still etwas anderes als
        # gedacht. Fehlerklasse `fehlerpattern_jetzt_nicht_durchgereicht`.
        jetzt = jetzt or datetime.now()
        warnungen: list[dict] = []
        fyta_status = await self._hole_fyta_status()

        for zone_id in zone_ids:
            await self._loese_keine_ankunft_warnung(zone_id, jetzt)
            geraete = await self._speicher.letzte_messungen_pro_geraet_ohne_fenster(
                zone_id, seit=jetzt - timedelta(days=GERAET_ERWARTET_TAGE),
            )

            if not geraete:
                # Zonen-Warnung ohne geraet_id: es gibt kein Geraet, dem man
                # sie zuordnen koennte. Deckt beide Faelle ab -- noch nie
                # Daten, und alle Geraete laenger als GERAET_ERWARTET_TAGE
                # stumm. Die Unterscheidung steht im Text.
                letzte = await self._speicher.letzte_messung(zone_id)
                if letzte is None:
                    text = "Noch nie Daten empfangen"
                else:
                    tage = (jetzt - letzte.zeitstempel).days
                    text = (
                        f"Kein ueberwachtes Geraet mehr: letzte Messung vor "
                        f"{tage} Tagen ({letzte.zeitstempel.strftime('%d.%m.')}), "
                        f"ausserhalb des {GERAET_ERWARTET_TAGE}-Tage-Fensters"
                    )
                warnung = await self._oeffne_warnung(
                    zone_id, "ausfall", text, jetzt,
                )
                if warnung:
                    warnungen.append(warnung)
                # Kein Geraet -> keine Batterie-Aussage moeglich.
                await self._schliesse_batterie_warnungen(zone_id, jetzt, None)
                continue

            # Eine veraltete Zonen-Warnung (geraet_id='') aus der Zeit vor
            # T-0526 wuerde sonst nie wieder geschlossen -- die Geraete-Pfade
            # unten fassen sie nicht an.
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.AUSFALL, jetzt, geraet_id="",
            )

            for messung in geraete:
                geraet_id = messung.geraet_id
                warnungen += await self._pruefe_ausfall(
                    zone_id, geraet_id, messung, jetzt,
                    fyta_status.get(geraet_id),
                )
                warnungen += await self._pruefe_batterie(
                    zone_id, geraet_id, messung, fyta_status.get(geraet_id), jetzt,
                )

            await self._schliesse_verwaiste_ausfall_warnungen(
                zone_id, {m.geraet_id for m in geraete}, jetzt,
            )

        return warnungen

    async def _schliesse_verwaiste_ausfall_warnungen(
        self, zone_id: str, ueberwacht: set[str], jetzt: datetime,
    ) -> None:
        """T-0571: Ausfall-Warnungen ohne zugehoeriges Geraet schliessen.

        `GERAET_ERWARTET_TAGE` sollte laut seinem eigenen Kommentar dafuer
        sorgen, dass ein Geraet nach 30 Tagen "aus der Ueberwachung faellt
        und seine Ausfall-Warnung geschlossen wird". Umgesetzt war das nur
        fuer den Fall, dass die Zone GAR KEIN Geraet mehr hat. Bleiben
        andere Geraete uebrig, faehrt die Schleife oben nur ueber diese --
        die Warnung des verschwundenen Geraets fasst niemand mehr an und sie
        steht dauerhaft offen.

        Zweiter, haeufigerer Weg in denselben Zustand: ein Sensor WECHSELT
        die Zone. Realfall 08.09.2026 -- ein FYTA-Sensor wurde einer anderen
        Pflanze zugeordnet, seine alte Zone behielt die offene Warnung.

        Bewusst nur AUSFALL: Batterie-Warnungen haengen am selben Geraet und
        werden im Geraete-Pfad geschlossen; eine Warnung ohne `geraet_id`
        (Zonen-Warnung aus dem Zweig darueber) bleibt unangetastet.
        """
        offene = await self._speicher.offene_sensor_warnungen(zone_id)
        for warnung in offene:
            geraet_id = warnung.geraet_id or ""
            if warnung.typ != SensorWarnungTyp.AUSFALL or not geraet_id:
                continue
            if geraet_id in ueberwacht:
                continue
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.AUSFALL, jetzt, geraet_id=geraet_id,
            )
            logger.info(
                "sensor_health.verwaiste_ausfall_warnung_geschlossen",
                zone_id=zone_id, geraet_id=geraet_id,
            )

    async def _pruefe_ausfall(
        self, zone_id: str, geraet_id: str, messung, jetzt: datetime,
        fyta_status=None,
    ) -> list[dict]:
        """Ausfall-Warnung fuer genau ein Geraet.

        T-0571: Eine FYTA-"geraet_id" ist eine PFLANZE, kein Geraet -- und an
        einer Pflanze kann gerade gar kein Sensor stecken (Andre steckt sie in
        der FYTA-App um). Dann meldet der Geraete-Status eine LEERE
        `sensor_id`, und `last_data_received_at` friert auf dem Zeitpunkt ein,
        an dem das letzte Geraet abgezogen wurde. Ohne diesen Zweig waechst
        daraus eine Ausfall-Warnung, die nie wieder weggeht und die Zone in
        der Karte als "Sensor unzuverlaessig -- Zustand unbekannt" markiert,
        obwohl alle tatsaechlich vorhandenen Sensoren der Zone sauber messen.
        Realfall 09.09.2026: die Hecke zeigte 397,9 h Ausfall fuer eine
        Pflanze, deren Sensor seit dem Vortag am Mandevilla Maxibaer sitzt.

        Bewusst eng: nur wenn der Status VORLIEGT und seine `sensor_id` leer
        ist. Fehlt der Status ganz (kein FYTA, Job noch nie gelaufen), bleibt
        es beim normalen Ausfall-Pfad -- ein fehlender Status darf keine
        Ausfall-Erkennung abschalten.
        """
        if fyta_status is not None and not getattr(fyta_status, "sensor_id", ""):
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.AUSFALL, jetzt, geraet_id=geraet_id,
            )
            logger.info(
                "sensor_health.pflanze_ohne_geraet",
                zone_id=zone_id, geraet_id=geraet_id,
            )
            return []

        timeout_stunden = self._sensor_timeout_stunden(messung.quelle, zone_id)
        alter_stunden = (jetzt - messung.zeitstempel).total_seconds() / 3600
        if alter_stunden <= timeout_stunden:
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.AUSFALL, jetzt, geraet_id=geraet_id,
            )
            return []

        warnung = await self._oeffne_warnung(
            zone_id, "ausfall",
            f"{geraet_id}: letztes Update vor {alter_stunden:.1f}h "
            f"({messung.zeitstempel.strftime('%d.%m. %H:%M')})",
            jetzt, geraet_id=geraet_id,
        )
        return [warnung] if warnung else []

    async def _pruefe_batterie(
        self, zone_id: str, geraet_id: str, messung,
        fyta_status, jetzt: datetime,
    ) -> list[dict]:
        """Batterie-Warnung fuer genau ein Geraet.

        T-0527: Der Kommentar an dieser Stelle lautete jahrelang "FYTA hat
        keine Batterie" und war falsch. Die Messreihe fuehrt das Feld
        tatsaechlich nicht, der Geraete-Status aber sehr wohl
        (`battery_level`, 0-100). Seit `FytaStatusJob` ihn mitschreibt, ist
        der Wert hier verfuegbar und FYTA-Sensoren fallen nicht mehr still
        aus der Batterie-Ueberwachung.
        """
        prozent = messung.batterie_prozent
        if prozent is None and fyta_status is not None:
            prozent = fyta_status.battery_level

        if prozent is None:
            await self._schliesse_batterie_warnungen(zone_id, jetzt, geraet_id)
            return []

        if prozent < BATTERIE_KRITISCH_PROZENT:
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.BATTERIE_NIEDRIG, jetzt,
                geraet_id=geraet_id,
            )
            warnung = await self._oeffne_warnung(
                zone_id, "batterie_kritisch",
                f"{geraet_id}: Batterie {prozent:.0f}% — Sofort wechseln!",
                jetzt, geraet_id=geraet_id,
            )
            return [warnung] if warnung else []

        if prozent < BATTERIE_WARNUNG_PROZENT:
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.BATTERIE_KRITISCH, jetzt,
                geraet_id=geraet_id,
            )
            warnung = await self._oeffne_warnung(
                zone_id, "batterie_niedrig",
                f"{geraet_id}: Batterie {prozent:.0f}%",
                jetzt, geraet_id=geraet_id,
            )
            return [warnung] if warnung else []

        await self._schliesse_batterie_warnungen(zone_id, jetzt, geraet_id)
        return []

    async def _schliesse_batterie_warnungen(
        self, zone_id: str, jetzt: datetime, geraet_id: str | None,
    ) -> None:
        for typ in (
            SensorWarnungTyp.BATTERIE_NIEDRIG,
            SensorWarnungTyp.BATTERIE_KRITISCH,
        ):
            await self._speicher.schliesse_sensor_warnung(
                zone_id, typ, jetzt, geraet_id=geraet_id,
            )

    async def _hole_fyta_status(self) -> dict:
        """T-0527: Geraete-Status je `geraet_id`, leer wenn nicht verfuegbar.

        Defensiv per `getattr`, weil aeltere Test-Attrappen des Speichers
        die Methode nicht kennen -- dieselbe Ueberlegung wie bei
        `letzte_ankunft_feuchte` in `_loese_keine_ankunft_warnung`. Ein
        Fehler hier darf die Ausfall-Erkennung nicht mitreissen: sie ist der
        wichtigere der beiden Checks.
        """
        hole = getattr(self._speicher, "letzter_fyta_geraete_status", None)
        if hole is None:
            return {}
        try:
            return await hole()
        except Exception:
            logger.exception("sensor_health.fyta_status_fehler")
            return {}

    async def _loese_keine_ankunft_warnung(
        self, zone_id: str, jetzt: datetime,
    ) -> None:
        """T-0445: Auto-Resolve fuer `KEINE_ANKUNFT_IM_LAUF`.

        Die Warnung entsteht im Entscheidungsmotor beim Frische-Stop und hat
        dort keinen Gegenpart -- ohne Schliesser bliebe nach dem ersten Stop
        dauerhaft ein KRITISCH-Eintrag im Default-Ops-Feed stehen und wuerde
        genau die Aufmerksamkeit abstumpfen, fuer die er gebaut ist.

        Kriterium ist die ANKUNFT, nicht die Messzeit: die Warnung sagt "seit
        Laufbeginn kam nichts an", behoben ist sie, sobald wieder etwas
        ankommt. Verglichen wird gegen den Zeitstempel der offenen Warnung,
        nicht gegen `jetzt` -- sonst wuerde ein alter Backfill-Nachtrag sie
        schliessen.

        T-0450 (Nachpruefung des Gate-Nebenfunds): die Asymmetrie zwischen
        `getattr` auf `letzte_ankunft_feuchte` und dem direkten Aufruf von
        `offene_sensor_warnungen` ist gewollt, nicht vergessen.
        `offene_sensor_warnungen` gehoert zum festen Speicher-Vertrag und wird
        auch in `leck_detektor.py` und `api_server.py` direkt gerufen; nur
        `entscheidung.py` sichert sie ab, weil dort ein Speicher-Fehler sonst
        eine Giess-ENTSCHEIDUNG kippen wuerde. `letzte_ankunft_feuchte` kam
        erst mit T-0445 dazu und fehlt aelteren Test-Attrappen -- daher dort
        der getattr. Der Reihenfolge nach kostet das im Normalfall EINE
        Zusatz-Query pro Zone (die Ankunft wird nur geholt, wenn wirklich eine
        KEINE_ANKUNFT-Warnung offen ist), nicht zwei.
        """
        hole_ankunft = getattr(self._speicher, "letzte_ankunft_feuchte", None)
        if hole_ankunft is None:
            return
        offene = await self._speicher.offene_sensor_warnungen(zone_id)
        betroffen = [
            w for w in offene
            if w.typ == SensorWarnungTyp.KEINE_ANKUNFT_IM_LAUF
        ]
        if not betroffen:
            return
        ankunft = await hole_ankunft(zone_id)
        if ankunft is None:
            return
        if all(ankunft > w.zeitstempel for w in betroffen):
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.KEINE_ANKUNFT_IM_LAUF, jetzt,
            )

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
        self, zone_id: str, typ: str, details: str, jetzt: datetime,
        geraet_id: str = "",
    ) -> dict | None:
        """Oeffnet eine Warnung genau einmal solange sie offen ist."""
        warnung = SensorWarnung(
            zeitstempel=jetzt,
            zone_id=zone_id,
            typ=SensorWarnungTyp(typ),
            details=details,
            geraet_id=geraet_id,
        )
        if not await self._speicher.oeffne_sensor_warnung(warnung):
            # T-0397 (F4): schon offen -> nur die `details` auffrischen (z.B.
            # die "vor Xh"-Angabe der Ausfall-Warnung), still, ohne Re-Log.
            # T-0526: geraetescharf, sonst ueberschreiben sich in einer
            # Multi-Sensor-Zone die Geraete gegenseitig ihre Texte.
            await self._speicher.aktualisiere_offene_warn_details(
                zone_id, SensorWarnungTyp(typ), details, geraet_id=geraet_id,
            )
            return None

        if "kritisch" in typ:
            logger.error(
                "sensor_health.kritisch", zone=zone_id, geraet=geraet_id,
                typ=typ, details=details,
            )
        else:
            logger.warning(
                "sensor_health.warnung", zone=zone_id, geraet=geraet_id,
                typ=typ, details=details,
            )

        return {
            "zone_id": zone_id, "typ": typ, "details": details,
            "geraet_id": geraet_id,
        }
