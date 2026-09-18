"""T-0068 — DHS-Backfill fuer Bodenfeuchte-Sensoren.

Holt fehlende Sensor-Messwerte (Bodenfeuchte + Bodentemperatur) aus dem
inoffiziellen Device History Service der Gardena-Webapp und schreibt sie
als `sensor_messung` in die DB. Ergaenzt den Live-WebSocket-Pfad: wenn
das Backend offline ist, fehlen Live-Pushes. DHS hat ein 7-Tage-Rolling-
Window und kann diese Luecken nachtraeglich schliessen.

**Warum**: Beleg 23.04.2026 — Bambus-Peak 60 % war in der Gardena-App
sichtbar, in unserer DB nicht (Service 07:46–16:54 down). Der ML-
Retrain konnte den Puls-Event nicht als n=6-Sample nutzen, weil
delta_6h mangels Sensorwert um 16:32 nicht berechnet werden konnte.
**Wir verlieren unser wertvollstes Trainings-Signal an Service-Downtimes.**

**Dedup**: 3-min-Toleranzfenster gegen bestehende `sensor_messung`-
Zeilen. Live-WebSocket und DHS senden den gleichen Messpunkt mit
minutenweise abweichenden Zeitstempeln (Live ~60 s-Cadence im Relay,
DHS-Server rundet anders) — exakter Match wuerde Phantom-Duplikate
erzeugen. Bei Treffer im Fenster wird der DHS-Wert verworfen
(Live-WebSocket schreibt zuerst, DHS zieht nur Luecken nach).

**Auth**: gleicher `GardenaCustomerAuth` wie `gardena_web_backfill.py`.

**Risiko**: Inoffizieller Endpoint, Gardena kann ihn aendern. Job
loggt Fehler, blockiert nie den Hauptloop. Live-WebSocket bleibt
weiterhin Hauptpfad.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

import httpx
import structlog

from bewaesserung.gardena_customer_auth import GardenaCustomerAuth
from bewaesserung.modelle import DatenQuelle, SensorMessung
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

DHS_BASIS_URL = "https://smart.gardena.com/v1/dhs"
PRESET_SENSOR = "sensor2"

# 30 Min: konsistent zu gardena_web_backfill. Sensor-Cadence ~1 Event/h
# nominell, also liefert jeder zweite Lauf neue Daten. Genuegend Luft
# bis zur 7-Tage-Endpoint-Grenze auch bei laengeren Service-Ausfaellen.
INTERVALL_MINUTEN_DEFAULT = 30

# 3-min-Dedup-Fenster gegen Zeitstempel-Drift zwischen Live-WS und DHS.
# Sensor misst intern stuendlich, Drift durch Relay/Cloud-Rundung liegt
# typisch bei <60 s, aber wir wollen sicher sein, dass z. B.
# 10:00:23 (Live) und 09:59:47 (DHS) als gleicher Messpunkt erkannt werden.
DEDUP_FENSTER_MIN = 3

# Bucket-Merge zwischen humidity- und temperature-Events, die zum selben
# Sensor-Hardware-Tick gehoeren aber server-seitig 1-2 s versetzt
# eintreffen. 90 s sind komfortabel ueber dem typischen Versatz und
# sicher unter dem Sensor-Cadence-Minimum (~8 min beobachtet).
MERGE_FENSTER_SEKUNDEN = 90

# Periodischer Catch-up zieht nur die letzten 2 h — deckt 1 verpassten
# 30-min-Lauf + Puffer ab und vermeidet unnoetige Netzwerk-Last bei
# jedem Tick.
CATCHUP_FENSTER_STUNDEN = 2

# Startup-Gap-Fill nutzt das volle 7-Tage-Endpoint-Fenster, damit nach
# einem laengeren Crash alles nachgezogen wird.
STARTUP_FENSTER_STUNDEN = 24 * 7

HTTP_TIMEOUT_S = 30.0


class SensorDhsBackfillJob:
    """Periodischer DHS-Abruf fuer Bodenfeuchte-Sensoren.

    Args:
        auth: GardenaCustomerAuth-Instanz mit gueltigem Bearer-Token.
        speicher: aktive Speicher-Verbindung.
        location_id: Gardena-Location-UUID (aus Discovery).
        sensor_zu_zone: Mapping `sensor_uuid -> zone_id` fuer alle
            Bodenfeuchte-Sensoren, die wir nachziehen wollen.
        intervall_minuten: Mindestabstand zwischen periodischen Laeufen.
    """

    def __init__(
        self,
        auth: GardenaCustomerAuth,
        speicher: Speicher,
        location_id: str,
        sensor_zu_zone: dict[str, str],
        intervall_minuten: int = INTERVALL_MINUTEN_DEFAULT,
    ) -> None:
        self._auth = auth
        self._speicher = speicher
        self._location_id = location_id
        self._sensor_zu_zone = dict(sensor_zu_zone)
        self._intervall = timedelta(minutes=intervall_minuten)
        self._letzte_aktualisierung: datetime | None = None

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> int:
        """Laeuft max. einmal pro `intervall_minuten`. Gibt Anzahl neuer Messungen."""
        jetzt = jetzt or datetime.now()
        if (
            self._letzte_aktualisierung
            and jetzt - self._letzte_aktualisierung < self._intervall
        ):
            return 0
        anzahl = await self.aktualisiere(
            jetzt, von_stunden=self._adaptives_catchup_fenster(jetzt),
        )
        self._letzte_aktualisierung = jetzt
        return anzahl

    def _adaptives_catchup_fenster(self, jetzt: datetime) -> int:
        """Catch-up-Fenster (Stunden) = Zeit seit letztem Lauf + Puffer,
        gedeckelt auf das 7-Tage-Endpoint-Fenster.

        T-0287: Heilt Offline-Phasen (Laptop-Schlaf, kurzer Crash) ohne
        Restart. Schlaeft der Host z. B. 8.6 h, ist der erste Lauf nach dem
        Aufwachen ~8.6 h ueberfaellig -> Fenster ~10 h statt fix 2 h, sodass
        die Luecke abgedeckt wird. Vorher zog nur der Prozess-START das volle
        Fenster (STARTUP_FENSTER_STUNDEN); ein Aufwachen ohne Restart blieb
        auf CATCHUP_FENSTER_STUNDEN sitzen und verlor 08:00..(jetzt-2h).
        Re-Fetch ist idempotent (zeitstempel-basiertes Dedup).
        """
        if self._letzte_aktualisierung is None:
            return CATCHUP_FENSTER_STUNDEN
        verstrichen_h = (jetzt - self._letzte_aktualisierung).total_seconds() / 3600.0
        # +2 h Puffer deckt int-Abrundung + Cadence-/Cloud-Drift ab.
        gewuenscht = int(verstrichen_h) + 2
        return max(CATCHUP_FENSTER_STUNDEN, min(STARTUP_FENSTER_STUNDEN, gewuenscht))

    async def aktualisiere(
        self,
        jetzt: datetime | None = None,
        von_stunden: int = CATCHUP_FENSTER_STUNDEN,
    ) -> int:
        """Holt DHS-Daten fuer alle konfigurierten Sensoren + persistiert.

        Fehler werden geloggt, nicht geworfen — der Hauptloop darf nicht
        blockieren.
        """
        jetzt = jetzt or datetime.now()
        von = jetzt - timedelta(hours=von_stunden)

        gesamt_neu = 0
        for sensor_uuid, zone_id in self._sensor_zu_zone.items():
            try:
                daten = await self._hole_dhs(sensor_uuid)
            except Exception:
                logger.exception(
                    "sensor_dhs_backfill.abruf_fehlgeschlagen",
                    sensor=sensor_uuid[:8] + "...",
                    zone=zone_id,
                )
                continue
            messungen = self._parse_messungen(daten, zone_id, sensor_uuid, von)
            for messung in messungen:
                if await self._existiert_messung(
                    zone_id, messung.zeitstempel, messung.geraet_id,
                ):
                    continue
                await self._speicher.speichere_messung(messung)
                gesamt_neu += 1

        if gesamt_neu > 0:
            logger.info(
                "sensor_dhs_backfill.messungen_geschrieben",
                neu=gesamt_neu,
                location=self._location_id,
                sensoren=len(self._sensor_zu_zone),
            )
        else:
            logger.debug(
                "sensor_dhs_backfill.keine_neuen_messungen",
                location=self._location_id,
                sensoren=len(self._sensor_zu_zone),
            )
        return gesamt_neu

    async def _hole_dhs(self, sensor_uuid: str) -> dict:
        """HTTP-GET gegen den DHS-Sensor-Endpoint. Wirft bei Fehler."""
        token = await self._auth.hole_gueltigen_token()
        url = f"{DHS_BASIS_URL}/{sensor_uuid}"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            antwort = await client.get(
                url,
                params={
                    "preset": PRESET_SENSOR,
                    "location_id": self._location_id,
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.api+json",
                },
            )
        antwort.raise_for_status()
        return antwort.json()

    def _parse_messungen(
        self,
        daten: dict,
        zone_id: str,
        sensor_uuid: str,
        nicht_vor: datetime,
    ) -> list[SensorMessung]:
        """Wandelt JSON:API-Antwort in `SensorMessung`-Objekte.

        Der `sensor2`-Preset liefert zwei `dh-point-serie`-Eintraege
        (humidity + temperature) mit `attributes['property-name']` und
        `relationships['dh-events'].data[]` als Liste der Event-IDs.
        Im `included`-Array stehen die `dh-point-event`-Items mit
        `id`, `attributes.timestamp`, `attributes.value`. Wir matchen
        Events der beiden Serien ueber die ID-Listen.

        **Bucket-Merge**: humidity- und temperature-Events haben in der
        Praxis 1-2 s versetzte Zeitstempel (Server-seitiges Reporting).
        Damit aus einem Sensor-Hardware-Event nicht zwei DB-Zeilen mit
        jeweils nur halbem Wert werden, mergen wir Events innerhalb
        `MERGE_FENSTER_SEKUNDEN` zu einer SensorMessung. Erster Event
        liefert den kanonischen Zeitstempel.

        Werte kommen als String-Integer ("55", "16") — werden zu float
        gecastet. Fehlende Werte → None (kein Failure).
        """
        # 1) Map: event_id -> property_name ("humidity" / "temperature").
        event_zu_property: dict[str, str] = {}
        for serie in daten.get("data") or []:
            if serie.get("type") != "dh-point-serie":
                continue
            attr = serie.get("attributes") or {}
            # Pruefe beide Schreibweisen (mit/ohne Bindestrich)
            prop = (
                attr.get("property-name") or attr.get("property") or ""
            ).lower()
            if prop not in ("humidity", "temperature"):
                continue
            rels = (serie.get("relationships") or {}).get("dh-events") or {}
            for ref in rels.get("data") or []:
                eid = ref.get("id")
                if eid:
                    event_zu_property[eid] = prop

        # 2) Events flach sammeln, sortiert nach Zeit.
        roh: list[tuple[datetime, str, float]] = []
        for block in daten.get("included") or []:
            if block.get("type") != "dh-point-event":
                continue
            event_id = block.get("id")
            prop = event_zu_property.get(event_id) if event_id else None
            if prop is None:
                continue

            attr = block.get("attributes") or {}
            zeit_str = attr.get("timestamp") or attr.get("time") or attr.get("at")
            if not zeit_str:
                continue
            try:
                zeit = _parse_iso(zeit_str)
            except ValueError:
                continue
            if zeit < nicht_vor:
                continue

            wert = _parse_zahl(attr.get("value"))
            if wert is None:
                continue

            roh.append((zeit, prop, wert))

        roh.sort(key=lambda x: x[0])

        # 3) Bucket-Merge: aufeinanderfolgende Events innerhalb
        #    MERGE_FENSTER_SEKUNDEN gehoeren zum selben Hardware-Sensor-Tick.
        messungen: list[SensorMessung] = []
        i = 0
        while i < len(roh):
            zeit_anker, prop_anker, wert_anker = roh[i]
            eintrag: dict[str, float] = {prop_anker: wert_anker}
            j = i + 1
            while j < len(roh):
                zeit_j, prop_j, wert_j = roh[j]
                if (zeit_j - zeit_anker).total_seconds() > MERGE_FENSTER_SEKUNDEN:
                    break
                if prop_j in eintrag:
                    # Selbe Property zweimal im Fenster: getrennte Messungen
                    break
                eintrag[prop_j] = wert_j
                j += 1
            # Reine Temperatur-Buckets (humidity fehlt) sind Cloud-Reporting-
            # Artefakte — Gardena schickt zwischen Voll-Ticks gelegentlich
            # nur einen temperature-Beat. Eine Zeile mit boden_feuchte=NULL
            # wuerde nur das Frontend-Diagramm reissen lassen, ohne neuen
            # Erkenntniswert zu liefern. Skippen, naechster Bucket faengt
            # bei j an.
            if "humidity" not in eintrag:
                i = j
                continue
            messungen.append(
                SensorMessung(
                    zeitstempel=zeit_anker,
                    zone_id=zone_id,
                    geraet_id=sensor_uuid,
                    boden_feuchte=eintrag.get("humidity"),
                    boden_temperatur=eintrag.get("temperature"),
                    quelle=DatenQuelle.GARDENA,
                )
            )
            i = j
        return messungen

    async def _existiert_messung(
        self,
        zone_id: str,
        zeitstempel: datetime,
        geraet_id: str,
        toleranz_min: int = DEDUP_FENSTER_MIN,
    ) -> bool:
        """True wenn fuer Zone+Geraet +/-toleranz_min schon eine Messung existiert.

        Live-WebSocket-Cadence weicht minutenweise von DHS-Server-
        Zeitstempeln ab — exakter Match wuerde Duplikate produzieren.
        Wir suchen ein Fenster und behandeln jeden Treffer als
        "selber Messpunkt, schon da".

        F14/T-0224: NUR gegen denselben `geraet_id` deduplizieren. In
        Multi-Sensor-Zonen (waldblumenhain: Gardena + 2x FYTA) wuerde eine
        FYTA-Messung sonst einen zeitnahen Gardena-DHS-Punkt als Duplikat
        verwerfen -> systematische Gardena-Luecken genau in den Offline-
        Phasen, die der Job heilen soll.
        """
        von = zeitstempel - timedelta(minutes=toleranz_min)
        bis = zeitstempel + timedelta(minutes=toleranz_min)
        bestehende = await self._speicher.hole_messungen(
            zone_id, von=von, bis=bis,
        )
        return any(m.geraet_id == geraet_id for m in bestehende)


# --- Helfer ---


def _parse_iso(s: str) -> datetime:
    """Parse DHS-ISO-Zeitstempel (`2026-04-18T09:25:07Z`) zu naivem Lokal-datetime.

    DHS liefert UTC ('Z'). Wir normalisieren auf die lokale Zeitzone und
    machen naive (kein tzinfo) — der Rest des Systems nutzt naive-lokale
    Zeitstempel (analog `gardena_web_backfill._parse_iso`).
    """
    dt_utc = datetime.fromisoformat(s.replace("Z", "+00:00"))
    lokal = dt_utc.astimezone()
    return lokal.replace(tzinfo=None)


def _parse_zahl(roh) -> float | None:
    """Castet Sensor-Werte aus DHS (oft String-Integer "55") zu float.

    Ungueltige Werte (None, leerer String, Buchstabe) -> None statt
    Exception, damit ein einzelner kaputter Event-Eintrag nicht den
    ganzen Sensor-Pull blockiert.
    """
    if roh is None:
        return None
    try:
        return float(roh)
    except (TypeError, ValueError):
        return None


def baue_sensor_zu_zone(
    zuordnungen: dict[str, str],
    sensor_geraete_ids: Iterable[str],
) -> dict[str, str]:
    """Filtert das volle Geraete-Mapping auf Bodenfeuchte-Sensoren.

    `zuordnungen` enthaelt ALLE Geraete (Sensoren + Water Control etc.).
    `sensor_geraete_ids` ist die Liste der UUIDs, die als
    SENSOR/SOIL_SENSOR aus der Discovery erkannt wurden. Wir nehmen
    die Schnittmenge — nur registrierte Sensoren mit Zone-Match.
    """
    sensor_set = set(sensor_geraete_ids)
    return {
        gid: zid for gid, zid in zuordnungen.items() if gid in sensor_set
    }
