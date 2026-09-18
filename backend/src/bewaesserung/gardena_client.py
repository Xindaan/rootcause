"""Gardena Smart System API Client.

Kapselt die py-smart-gardena Library und bietet ein sauberes Interface
fuer Sensor-Callbacks und Ventilsteuerung. Verwaltet OAuth2-Auth,
WebSocket-Verbindung und Geraete-Discovery.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import structlog

# Monkey-Patch: websockets 13.x hat kein .closed-Attribut auf ClientConnection,
# aber py-smart-gardena prueft es im WebSocket-Loop. Wir fuegen es hinzu.
try:
    from websockets.asyncio.client import ClientConnection
    if not hasattr(ClientConnection, "closed"):
        @property
        def _closed(self):
            return self.protocol.state.name == "CLOSED"
        ClientConnection.closed = _closed
except ImportError:
    pass

from gardena.smart_system import SmartSystem


# T-0154: Bugfix py-smart-gardena 1.3.17 — `SmartSystem.start_ws` referenziert
# die Variable `delay` auch im Erfolgs-Pfad (wenn `__launch_websocket_loop`
# clean zurueckkehrt), in dem sie nie zugewiesen wird. Das wirft
# `UnboundLocalError`, was in unserem aeusseren Reconnect-Loop als
# "Verbindungsabbruch" geloggt wird (siehe `gardena.websocket_abgebrochen`-
# Warnings) und einen kompletten Re-Auth + Re-Subscribe ausloest. Folgen:
# (a) Live-Sensor-Updates kommen waehrend der Backoff-Pause nicht an,
# (b) Husqvarna-Soft-Ban-Risiko durch unnoetig haeufige Re-Auths.
#
# Fix: `delay = 10` als Default am Schleifen-Start setzen. Patch wird
# beim Modul-Import angewendet, bevor irgendein SmartSystem-Objekt
# erzeugt wird.
def _patche_start_ws_delay_bug() -> None:
    """T-0154: Library-Patch fuer py-smart-gardena 1.3.17 start_ws."""
    from websockets.exceptions import ConnectionClosed
    from authlib.integrations.base_client.errors import OAuthError, InvalidTokenError

    async def start_ws_gepatched(self, location):  # type: ignore[no-redef]
        """Start WebSocket connection — patched for delay-default bug."""
        connection_attempts = 0
        max_consecutive_failures = 5

        while not self.should_stop:
            # T-0154 FIX: Default damit der Erfolgs-Pfad (try-block ohne
            # Exception) im Sleep-Block keinen UnboundLocalError wirft.
            delay = 10
            self.logger.debug(
                f"Trying to connect to gardena API.... (attempt {connection_attempts + 1})"
            )
            websocket = None

            try:
                ws_url = await self._SmartSystem__get_ws_url(location)
                websocket = await self._SmartSystem__launch_websocket_loop(ws_url)
                connection_attempts = 0
            except (ConnectionClosed, InvalidTokenError, OAuthError) as error:
                connection_attempts += 1
                self.logger.warning(
                    f"WebSocket connection error (attempt {connection_attempts}): {error}"
                )
                if connection_attempts >= max_consecutive_failures:
                    self.logger.error(
                        f"Too many consecutive WebSocket failures "
                        f"({connection_attempts}), extending delay"
                    )
                    delay = min(60, 10 + (connection_attempts - max_consecutive_failures) * 10)
                else:
                    delay = 10
            except Exception as error:
                connection_attempts += 1
                self.logger.error(
                    f"Unexpected WebSocket error (attempt {connection_attempts}): "
                    f"{type(error).__name__}: {error}"
                )
                delay = min(30, 5 + connection_attempts * 2)
            finally:
                self.set_ws_status(False)
                if websocket and not websocket.closed:
                    try:
                        await websocket.close()
                        self.logger.debug("WebSocket connection closed")
                    except Exception as close_error:
                        self.logger.warning(
                            f"Error closing WebSocket: {close_error}"
                        )

            if not self.should_stop:
                self.logger.debug(f"Sleeping {delay} seconds before reconnect..")
                for _ in range(delay):
                    if self.should_stop:
                        break
                    await asyncio.sleep(1)

    SmartSystem.start_ws = start_ws_gepatched


_patche_start_ws_delay_bug()


class GardenaKommandoFehler(Exception):
    """T-0543: Ein Ventil-Kommando wurde nicht mit 202 quittiert.

    Traegt den **Status-Code**, weil genau der ueber das richtige Verhalten
    entscheidet: 429 heisst Soft-Ban, und dort ist ein Retry das Falscheste
    (2-4 h warten, [[fehlerpattern_husqvarna_softban]]); ein 502 ist dagegen
    eine Cloud-Delle, bei der ein Retry richtig ist. Vor diesem Patch war
    beides ununterscheidbar -- s. `_patche_kommando_fehlermeldung`.
    """

    def __init__(self, status_code: int, detail: str):
        self.status_code = int(status_code)
        self.detail = detail
        super().__init__(f"Gardena-Kommando abgelehnt: {status_code} -- {detail}")

    @property
    def ist_soft_ban(self) -> bool:
        """429 = Husqvarna hat Polling erkannt. NICHT retryen."""
        return self.status_code == 429


# T-0543: Bugfix py-smart-gardena -- `call_smart_system_service` baut die
# Fehlermeldung aus `response['errors'][0]['title']`. Fehlt dieses Feld, wirft
# die FORMATIERUNGSZEILE selbst `KeyError: 'errors'`, und damit ist der
# Status-Code weg, bevor ihn jemand sieht.
#
# Realfall 15.08.2026: drei abgelehnte Ventil-Kommandos, zwei davon
# SCHLIESSEN (13:57:41 oeffnen, 14:39:51 + 14:40:31 schliessen, Kanal 2). Der
# Lauf wurde 14:55:46 sauber geschlossen, es haing also nichts -- aber im Log
# stand nur `KeyError`. Ob die Cloud 429 (Soft-Ban) oder 502 (Delle)
# geantwortet hat, ist nicht mehr feststellbar; 40 s nach dem ersten
# Fehlschlag wurde erneut geschlossen. Bei einem 429 waere genau das der
# Fehler gewesen.
#
# Der Patch aendert **kein Verhalten**, er macht nur sichtbar: gleiche
# Anfrage, gleiche Bedingung (`!= 202`), aber die Exception traegt Code und
# (gekuerzten) Body, und ein 429 wird zusaetzlich laut geloggt. Ob der
# Retry-Pfad bei 429 anders reagieren muss, ist die Folgefrage -- sie laesst
# sich erst entscheiden, seit der Code ueberhaupt ankommt.
def _patche_kommando_fehlermeldung() -> None:
    """T-0543: Status-Code eines abgelehnten Kommandos erhalten."""
    import json as _json

    async def call_gepatched(self, service_id, data):  # type: ignore[no-redef]
        args = {"data": data}
        headers = self.create_header(True)
        r = await self.client.put(
            f"{self.SMART_HOST}/v2/command/{service_id}",
            headers=headers,
            data=_json.dumps(args, ensure_ascii=False),
        )
        if r.status_code == 202:
            return
        # Erst der schoene Weg (so wie die Lib es meint), dann der ehrliche.
        detail = ""
        try:
            body = r.json()
            detail = str(body["errors"][0]["title"])
        except Exception:  # noqa: BLE001 -- jede Abweichung vom Schema
            roh = getattr(r, "text", "") or ""
            detail = roh.strip()[:300] or "<leerer Body>"
        if r.status_code == 429:
            logger.error(
                "gardena.kommando_soft_ban",
                status_code=r.status_code,
                detail=detail[:200],
                hinweis="429 = Husqvarna hat Polling erkannt. NICHT retryen, "
                        "2-4 h warten (fehlerpattern_husqvarna_softban).",
            )
        raise GardenaKommandoFehler(r.status_code, detail)

    SmartSystem.call_smart_system_service = call_gepatched


_patche_kommando_fehlermeldung()

from bewaesserung.modelle import Ausloser, SensorMessung, VentilAktion, VentilEreignis

logger = structlog.get_logger()

# T-0288: Fenster nach einem (Re)Connect, in dem die py-smart-gardena-Lib
# gepufferte Alt-Events erneut einspielt (Replay-Burst). Statuswechsel in
# diesem Fenster werden nur als State nachgefuehrt, NICHT als echte
# Bewaesserungs-Transition gewertet (sonst Phantom-OEFFNEN/SCHLIESSEN).
REPLAY_GUARD_SEKUNDEN = 120
# T-0548: eigenes, KUERZERES Guard-Fenster fuer SENSOR-Messungen.
#
# Warum getrennt: die beiden Pfade haben verschiedene Schadensbilanzen.
# - Ventil: ein unterdruecktes Fremd-Event heilt der DHS-Backfill (T-0358);
#   ein durchgelassenes Phantom schreibt dagegen einen erfundenen Lauf in die
#   Historie (Realfall 16.06., 3882 s Hecke). Dort bleibt es bei 120 s.
# - Sensor: eine unterdrueckte Messung ist ENDGUELTIG weg (die Luecke vom
#   20.08. 00:40 steht bis heute in der DB), und sie faellt bevorzugt in die
#   Sekunden nach einem Pulsende, also ins Wirkungsfenster.
#
# Messung 23.08. ueber die Logs 03.06.-23.08. (2067 Armierungen): ein echter
# Replay-Schwall liefert 15-19 Events praktisch alle in DERSELBEN Sekunde; in
# 11 von 16 echten Offline-Faellen ist nach 1 s Schluss. 30 s decken 14/16
# vollstaendig ab, 120 s waren um Groessenordnungen zu grosszuegig bemessen.
# Was in den restlichen 2 Faellen durchkaeme, ist je EIN spaeter Einzelwert.
REPLAY_GUARD_SENSOR_SEKUNDEN = 30

# T-0306: WS-Gap-Schwelle. py-smart-gardena reconnectet teils INTERN, ohne
# dass der Backend-WS-Loop (`starte_websocket`) iteriert -> der dortige
# T-0288-Guard armiert dann NICHT. Eine WS-Luecke ueber ALLE Events (Sensor +
# Ventil) > dieser Schwelle bedeutet: die Verbindung war offline; der naechste
# Event-Schwall ist ein Replay-Burst der Lib. Wir armieren den Replay-Guard
# dann gap-getriggert. 15 min liegt klar ueber der normalen Sensor-Cadence
# (~10 min) + Ventil-Heartbeats, aber unter der beobachteten 43-min-Luecke
# (Realfall 16.06.). Falsch-positiv (echtes Event nach langer Stille
# unterdrueckt) ist unkritisch: der State wird nachgefuehrt + DHS-Backfill
# (gardena_web) holt echte Laeufe ohnehin nach.
REPLAY_GAP_SEKUNDEN = 900

# Typ fuer Callbacks
SensorCallback = Callable[[SensorMessung], Awaitable[None]]
VentilCallback = Callable[[VentilEreignis], Awaitable[None]]
# T-0324: Callback bei erkanntem langem WS-Gap (Reconnect nach Offline-Phase).
# Argument = Luecke in Sekunden. Konsument: Sensor-DHS-Catch-up.
WsGapCallback = Callable[[float], Awaitable[None]]
# T-0547: Praedikat "haelt unsere EIGENE VentilSicherung dieses Ventil gerade
# offen?" (valve_id -> bool). Synchron, weil es mitten im WS-Callback-Pfad
# ausgewertet wird. main.py injiziert es ueber alle DSWC-Sicherungen.
EigenerLaufPruefer = Callable[[str], bool]


class WebSocketMetriken:
    """Zaehler fuer WebSocket-Ventil-Events (T-0055-B4 Diagnose).

    Dokumentiert, wo Events auf dem Weg vom Gardena-Callback bis zum
    `ventil_ereignis`-Insert stehen bleiben. Der Heartbeat des Entscheidungs-
    Loops liest `diff_seit_snapshot()` und loggt die Differenz, damit man
    nach einer vermissten Bewaesserung sofort die Statistik sieht.

    Nicht thread-sicher, aber async-sicher (alle Aufrufer laufen sequenziell
    im gleichen Event-Loop).
    """

    def __init__(self) -> None:
        self._counter: dict[tuple[str, str], int] = {}

    def inc(self, kategorie: str, label: str = "") -> None:
        key = (kategorie, label)
        self._counter[key] = self._counter.get(key, 0) + 1

    def snapshot(self) -> dict[str, dict[str, int]]:
        aus: dict[str, dict[str, int]] = {}
        for (kat, lab), n in self._counter.items():
            aus.setdefault(kat, {})[lab] = n
        return aus

    def diff_seit_snapshot(
        self, vorher: dict[str, dict[str, int]],
    ) -> dict[str, dict[str, int]]:
        """Differenz zwischen aktuellem Snapshot und `vorher`. Nur positive Deltas."""
        aktuell = self.snapshot()
        diff: dict[str, dict[str, int]] = {}
        for kat, labels in aktuell.items():
            for lab, n in labels.items():
                d = n - vorher.get(kat, {}).get(lab, 0)
                if d > 0:
                    diff.setdefault(kat, {})[lab] = d
        return diff


class GardenaClient:
    """Async Client fuer die Gardena Smart System API.

    Verwaltet Verbindung, Geraete-Discovery und bietet Methoden
    fuer Sensor-Lesen und Ventil-Steuerung.
    """

    def __init__(self, client_id: str, client_secret: str = ""):
        self._client_id = client_id
        self._client_secret = client_secret
        self._smart_system: SmartSystem | None = None
        self._location_id: str | None = None
        self._sensor_callbacks: list[SensorCallback] = []
        self._ventil_callbacks: list[VentilCallback] = []
        self._ws_gap_callbacks: list[WsGapCallback] = []  # T-0324
        self._geraete_zone_map: dict[str, str] = {}  # geraet_id -> zone_id
        self._zone_name_map: dict[str, str] = {}  # lower(zone_name) -> zone_id
        self._ventil_status: dict[str, str] = {}  # valve_id -> letzter bekannter Status
        # T-0361: Startup-Reconciliation darf erst entscheiden, wenn fuer die
        # konkrete Valve mindestens ein echter WS-Status eingetroffen ist.
        self._ventil_status_events: dict[str, asyncio.Event] = {}
        self._ventil_offen_seit: dict[str, datetime] = {}  # valve_id -> Zeitpunkt des Oeffnens
        # T-0455: Ausloeser des GERADE offenen Laufs pro valve_id. Muss ueber
        # die Offen-Phase getragen werden, weil der SCHLIESSEN-Payload nur
        # "CLOSED"/"CLOSING" liefert -- ob der Lauf aus der App (MANUAL_
        # WATERING) oder aus dem Cloud-Zeitplan (SCHEDULED_WATERING) kam,
        # steht dann nicht mehr drin. Ohne diese Karte bekaeme das
        # SCHLIESSEN einen anderen Ausloeser als sein OEFFNEN -- und genau
        # das Paar ist die Einheit, die Bilanz/Wirkungsrate auswerten.
        # Lebenszyklus strikt parallel zu `_ventil_offen_seit` (setzen,
        # poppen, Replay-Guard-Cleanup an denselben drei Stellen).
        self._ventil_offen_ausloser: dict[str, Ausloser] = {}
        # T-0300: lokale Schliess-Timer pro Valve (call_later in ventil_oeffnen).
        # Muessen cancelbar sein -- sonst schliesst ein stale Timer nach Stop +
        # Neustart den FOLGE-Lauf vorzeitig. Key = (geraet_id, valve_id).
        self._schliess_timer: dict[tuple[str, str | None], asyncio.TimerHandle] = {}
        # T-0288: bis hierhin gilt das Replay-Guard-Fenster (None = inaktiv).
        self._replay_guard_bis: datetime | None = None
        # T-0548: Zeitpunkt der Armierung. Der Sensor-Pfad leitet daraus sein
        # eigenes, kuerzeres Fenster ab (REPLAY_GUARD_SENSOR_SEKUNDEN).
        self._replay_guard_armiert_am: datetime | None = None
        # T-0306: Zeitstempel des letzten EMPFANGENEN WS-Events (Sensor ODER
        # Ventil, auch unveraenderte). Eine Luecke > REPLAY_GAP_SEKUNDEN
        # signalisiert einen (lib-internen) Reconnect -> Replay-Guard armieren.
        self._letzter_ws_event: datetime | None = None
        # T-0547: siehe `setze_eigener_lauf_pruefer`. None = kein Pruefer
        # injiziert -> Verhalten exakt wie vor T-0547.
        self._eigener_lauf_pruefer: "EigenerLaufPruefer | None" = None
        self._ventil_geloggte_namen: set[str] = set()  # valve_ids fuer Initial-Debug-Log
        self._callbacks_registriert = False
        self._aktive_tasks: set[asyncio.Task] = set()  # Verhindert GC von Fire-and-Forget Tasks
        # T-0055-B4: Diagnose-Zaehler fuer WebSocket-Event-Verluste
        self._metriken = WebSocketMetriken()
        # T-0210-Folge: Nach-Reconnect-Sync-Callback. main.py injiziert
        # einen Handler, der den Live-State (aus `update_devices`) mit
        # offenen DB-OEFFNEN-Events abgleicht und Diskrepanzen
        # (DB-offen, Gardena-zu) als synthetisches SCHLIESSEN
        # nachtraegt. Wird VOR jedem `start_ws`-Versuch aufgerufen
        # (inkl. Initial-Start) -- nach Re-Auth + `update_devices`.
        self._reconnect_sync_callback: (
            "Callable[[dict[str, str]], Awaitable[int]] | None"
        ) = None

    @property
    def location_id(self) -> str | None:
        """Gardena-Location-UUID (nach verbinden() verfuegbar)."""
        return self._location_id

    @property
    def metriken(self) -> "WebSocketMetriken":
        """Zugriff auf die WebSocket-Diagnose-Zaehler (T-0055-B4)."""
        return self._metriken

    def water_control_geraet_id(self) -> str | None:
        """UUID des ersten Smart Water Control / Dual Water Control (nach verbinden()).

        Backward-Compat-Wrapper um `water_control_geraet_ids()`. Liefert
        den ersten Eintrag aus der Liste oder None. **Nur fuer Single-DSWC-
        Code-Pfade** — Multi-DSWC-aware Code soll `water_control_geraet_ids()`
        nutzen.
        """
        ids = self.water_control_geraet_ids()
        return ids[0] if ids else None

    def water_control_geraet_ids(self) -> list[str]:
        """T-0203: UUIDs aller Smart/Dual Water Controls (nach verbinden()).

        Mehrere DSWC-Geraete (z. B. eines fuer Bambus+Waldblumen, eines fuer
        Magerwiese+Hecke) werden alle zurueckgegeben — der Aufrufer routet
        pro Zone via `ZonenKonfig.ventil_geraet_id`.

        Reihenfolge: stabile Insertion-Reihenfolge der SmartSystem-Library
        (= grob nach Discovery-Zeit). Sortiere im Aufrufer falls noetig.
        """
        if not self._smart_system or not self._location_id:
            return []
        location = self._smart_system.locations.get(self._location_id)
        if not location:
            return []
        ids: list[str] = []
        for geraet_id, geraet in location.devices.items():
            typname = geraet.__class__.__name__
            if typname in ("SmartIrrigationControl", "WaterControl"):
                ids.append(geraet_id)
        return ids

    def _starte_callback_task(self, coro) -> None:
        """Startet einen async Callback-Task mit Fehler-Logging.

        Verhindert dass Tasks vom GC eingesammelt werden und loggt
        unbehandelte Exceptions statt sie stillschweigend zu verlieren.
        """
        task = asyncio.create_task(coro)
        self._aktive_tasks.add(task)
        task.add_done_callback(self._aktive_tasks.discard)
        task.add_done_callback(self._task_fehler_handler)

    def _markiere_ventil_status_gesehen(self, valve_id: str) -> None:
        """T-0361: weckt Startup-Checks, sobald eine Valve initial gesehen wurde."""
        event = self._ventil_status_events.get(valve_id)
        if event is None:
            event = asyncio.Event()
            self._ventil_status_events[valve_id] = event
        event.set()

    async def warte_auf_ventil_status(
        self, valve_id: str, timeout_s: float,
    ) -> bool:
        """Wartet auf den ersten bekannten WS-Status einer Valve.

        Returns True, wenn `valve_id` danach in `_ventil_status` steht. False
        bedeutet "unbekannt" und darf nicht als "geschlossen" interpretiert
        werden.
        """
        if valve_id in self._ventil_status:
            return True
        event = self._ventil_status_events.get(valve_id)
        if event is None:
            event = asyncio.Event()
            self._ventil_status_events[valve_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return False
        return valve_id in self._ventil_status

    @staticmethod
    def _task_fehler_handler(task: asyncio.Task) -> None:
        """Loggt Fehler aus Callback-Tasks."""
        if not task.cancelled() and task.exception():
            logger.error(
                "gardena.callback_fehler",
                fehler=str(task.exception()),
                exc_info=task.exception(),
            )

    async def verbinden(self) -> None:
        """Authentifiziert und laedt Locations + Geraete."""
        logger.info("gardena.verbinde", client_id=self._client_id[:8] + "...")

        self._smart_system = SmartSystem(
            client_id=self._client_id,
            client_secret=self._client_secret,
        )

        # Klare Fehlermeldung bei DNS-/Netzwerk-Problemen statt 60-Zeilen-
        # httpx-Stack-Trace. Typisches Symptom: Mac wacht aus Sleep auf, WLAN
        # noch nicht ready, mDNSResponder hat den Cache geflusht. Backend
        # crasht dann beim ersten OAuth-Call. User-Sicht: "Errno 8" mitten
        # im Trace ist nicht hilfreich.
        import httpx
        try:
            await self._smart_system.authenticate()
        except httpx.ConnectError as exc:
            raise RuntimeError(
                "Verbindung zur Husqvarna-Authentication-API fehlgeschlagen "
                f"({exc}). Wahrscheinliche Ursachen: kein Internet, DNS noch "
                "nicht bereit (z.B. nach Sleep / WLAN-Wechsel) oder Firewall. "
                "Pruefe `ping api.authentication.husqvarnagroup.dev` und "
                "starte das Backend erneut."
            ) from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                "Husqvarna-Authentication-API antwortet nicht "
                f"({exc.__class__.__name__}). Server-Probleme oder schlechte "
                "Verbindung. In ein paar Minuten erneut versuchen."
            ) from exc

        # py-smart-gardena erstellt seinen httpx-OAuth2-Client mit Default-
        # Timeout 5 s. Bei kalten Initial-Calls direkt nach OAuth-Token-
        # Aushaendigung (insbes. nach App-Neuanlage / Recovery) reicht das
        # nicht — der OAuth2-Auth-Hook + Connection-Pool-Setup verbraucht
        # Sekunden bevor der eigentliche GET losgeht. Empirisch (26.04.2026
        # nach Husqvarna-WAF-Recovery): GET /v2/locations 200 in ~0.3 s, aber
        # GET /v2/locations/{id} mit OAuth2Client-Wrapping → ReadTimeout.
        # 30 s Read-Timeout deckt diesen Spike + die WebSocket-Channel-
        # Reservierung sicher ab. Connect/Pool-Timeouts bleiben kurz, damit
        # Netzwerk-Probleme weiterhin schnell sichtbar werden.
        import httpx
        if self._smart_system.client is not None:
            self._smart_system.client.timeout = httpx.Timeout(
                connect=5.0, read=30.0, write=10.0, pool=5.0,
            )

        await self._smart_system.update_locations()

        if not self._smart_system.locations:
            raise RuntimeError("Keine Gardena-Locations gefunden. Geraete in der App einrichten.")

        # Erste Location verwenden (typisch: eine pro Haushalt)
        self._location_id = list(self._smart_system.locations.keys())[0]
        location = self._smart_system.locations[self._location_id]

        await self._smart_system.update_devices(location)

        logger.info(
            "gardena.verbunden",
            location=location.name,
            geraete_anzahl=len(location.devices),
        )

        # Geraete auflisten
        for geraet_id, geraet in location.devices.items():
            logger.info(
                "gardena.geraet",
                id=geraet_id,
                name=geraet.name,
                typ=geraet.type,
            )

    def registriere_geraet_zone(self, geraet_id: str, zone_id: str) -> None:
        """Ordnet ein Gardena-Geraet einer Zone zu."""
        self._geraete_zone_map[geraet_id] = zone_id
        logger.info("gardena.zone_zuordnung", geraet_id=geraet_id, zone_id=zone_id)

    def registriere_zone_namen(self, zone_namen: dict[str, str]) -> None:
        """Registriert Mapping zone.name (Gardena-App) -> zone_id fuer Valve-Matching.

        Notwendig fuer SMART_IRRIGATION_CONTROL (Dual Water Control): dort identifiziert
        sich jedes Ventil per Name (wie in der Gardena-App vergeben) statt per Kanal-
        Nummer. Case-insensitive Vergleich.
        """
        for name, zone_id in zone_namen.items():
            if name:
                self._zone_name_map[name.strip().lower()] = zone_id

    def registriere_sensor_callback(self, callback: SensorCallback) -> None:
        """Registriert einen Callback fuer Sensor-Updates."""
        self._sensor_callbacks.append(callback)

    def registriere_ventil_callback(self, callback: VentilCallback) -> None:
        """Registriert einen Callback fuer Ventil-Events (oeffnen/schliessen)."""
        self._ventil_callbacks.append(callback)

    def setze_eigener_lauf_pruefer(
        self, pruefer: "EigenerLaufPruefer | None",
    ) -> None:
        """T-0547: injiziert die Frage "fuehrt unsere eigene VentilSicherung
        dieses Ventil gerade als offen?".

        Der Replay-Guard entscheidet sonst allein nach der Uhr (Stille >=
        REPLAY_GAP_SEKUNDEN = Reconnect). Bei einem eigenen Puls >= 15 min
        stimmt diese Praemisse nicht: die Stille entsteht durch den Puls
        selbst. Mit diesem Praedikat kann der Guard gegen den EIGENEN Zustand
        pruefen statt gegen die Uhr.
        """
        self._eigener_lauf_pruefer = pruefer

    def _sensor_guard_aktiv(self, jetzt: datetime) -> bool:
        """T-0548: Guard-Fenster fuer SENSOR-Messungen -- kuerzer als das der
        Ventile, Begruendung an REPLAY_GUARD_SENSOR_SEKUNDEN.

        Faellt auf das Ventil-Fenster zurueck, wenn kein Armierungs-Zeitpunkt
        bekannt ist (kann nur passieren, wenn `_replay_guard_bis` je woanders
        gesetzt wuerde) -- im Zweifel lieber zu lange schuetzen als zu kurz.
        """
        if self._replay_guard_bis is None or jetzt >= self._replay_guard_bis:
            return False
        if self._replay_guard_armiert_am is None:
            return True
        sensor_bis = self._replay_guard_armiert_am + timedelta(
            seconds=REPLAY_GUARD_SENSOR_SEKUNDEN,
        )
        return jetzt < sensor_bis

    def _haelt_eigener_lauf(self, valve_id: str) -> bool:
        """True wenn eine unserer VentilSicherungen dieses Ventil offen fuehrt.

        Fehler im injizierten Praedikat duerfen den WS-Pfad nicht abreissen --
        im Zweifel False (= Verhalten wie vor T-0547, Guard greift).
        """
        if self._eigener_lauf_pruefer is None:
            return False
        try:
            return bool(self._eigener_lauf_pruefer(valve_id))
        except Exception:
            logger.exception(
                "gardena.eigener_lauf_pruefer_fehler", valve_id=valve_id,
            )
            return False

    def registriere_ws_gap_callback(self, callback: WsGapCallback) -> None:
        """T-0324: Registriert einen Callback, der bei einem erkannten langen
        WS-Gap (Reconnect nach Offline-Phase) feuert -- z.B. um den Sensor-DHS-
        Catch-up sofort zu triggern, statt auf die langsame Cadence zu warten."""
        self._ws_gap_callbacks.append(callback)

    def _erstelle_sensor_callback(self, geraet_id: str):
        """Erstellt einen Geraete-Callback der unsere Sensor-Callbacks aufruft."""
        def _on_update(device):
            zone_id = self._geraete_zone_map.get(geraet_id, geraet_id)
            jetzt = datetime.now()
            # T-0306: WS-Gap-Detection (armiert den Replay-Guard bei
            # lib-internem Reconnect) -- VOR dem Guard-Check unten.
            self._pruefe_ws_gap_und_armiere(jetzt)
            # T-0288-Folge (Isomorphie): Replay-Guard auch fuer Sensor-
            # Messungen. Nach einem Reconnect spielt die Lib Alt-Sensorwerte
            # erneut ein (Realfall 03.06.: Morgen-Peak 85 mit 18:28-Stempel
            # -> Phantom-Spike). Im Guard-Fenster verwerfen; der naechste
            # Live-Wert bzw. der DHS-Backfill fuellt zeitnah nach.
            # T-0548: eigenes, kuerzeres Fenster -- s. _sensor_guard_aktiv.
            if self._sensor_guard_aktiv(jetzt):
                self._metriken.inc("events_verworfen", "sensor_replay_guard")
                logger.info(
                    "gardena.sensor_replay_guard_unterdrueckt",
                    zone_id=zone_id, geraet=geraet_id,
                    guard_bis=self._replay_guard_bis.isoformat(),
                    sensor_fenster_s=REPLAY_GUARD_SENSOR_SEKUNDEN,
                )
                return
            messung = SensorMessung(
                zeitstempel=jetzt,
                zone_id=zone_id,
                geraet_id=geraet_id,
                boden_feuchte=getattr(device, "soil_humidity", None),
                boden_temperatur=getattr(device, "soil_temperature", None),
                umgebungs_temperatur=getattr(device, "ambient_temperature", None),
                licht_intensitaet=getattr(device, "light_intensity", None),
                batterie_prozent=getattr(device, "battery_level", None),
            )

            logger.debug(
                "gardena.sensor_update",
                zone_id=zone_id,
                feuchte=messung.boden_feuchte,
                temp=messung.boden_temperatur,
            )

            # Callbacks async ausfuehren
            for cb in self._sensor_callbacks:
                self._starte_callback_task(cb(messung))

        return _on_update

    # Aktivitaets-Strings aus py-smart-gardena fuer Oeffnen/Schliessen.
    # MANUAL_WATERING + SCHEDULED_WATERING: App/Schedule-Oeffnungen der Smart Irrigation Control.
    _VENTIL_OFFEN_STATES = {
        "OPEN", "OPENING",
        "MANUAL_WATERING", "SCHEDULED_WATERING",
    }
    _VENTIL_ZU_STATES = {"CLOSED", "CLOSING"}
    # T-0455: Aktivitaeten, die den Gardena-CLOUD-ZEITPLAN als Ursache
    # ausweisen (App-Schedule, laeuft auch wenn dieser Rechner aus ist).
    # Alles andere im Offen-Set ist eine App-/API-Oeffnung -> MANUELL.
    _VENTIL_ZEITPLAN_STATES = {"SCHEDULED_WATERING"}

    @classmethod
    def _offen_ausloser(cls, aktivitaet: str) -> Ausloser:
        """T-0455: Cloud-Zeitplan von App-Bedienung trennen.

        Vorher schrieben beide Zweige MANUELL, waehrend der DHS-Backfill
        DASSELBE Ereignis als AUTOMATIK fuehrte -- wer das Dedup-Rennen
        gewann, entschied die Semantik. `zeitplan` ist echtes Wasser
        (ECHTES_KANAL_WASSER), aber kein Engine-Lauf.
        """
        if aktivitaet in cls._VENTIL_ZEITPLAN_STATES:
            return Ausloser.ZEITPLAN
        return Ausloser.MANUELL

    def _valve_zu_zone(self, valve_name: str | None, valve_id: str) -> str:
        """Matcht valve_name (Gardena-App-Name) gegen zone.name -> zone_id.

        Fallback-Kaskade: exact lower-match -> valve_id als Platzhalter (+ Warnung).
        Bei Fallback landen Events trotzdem in der DB, damit nichts verloren geht.
        """
        if not valve_name:
            self._metriken.inc("events_verworfen", "valve_name_leer")
            return valve_id
        normalisiert = valve_name.strip().lower()
        zone_id = self._zone_name_map.get(normalisiert)
        if zone_id:
            return zone_id
        self._metriken.inc("events_verworfen", "kein_zone_match")
        logger.warning(
            "gardena.valve_kein_zone_match",
            valve_name=valve_name,
            valve_id=valve_id,
            bekannte_zone_namen=list(self._zone_name_map.keys()),
        )
        return valve_id

    def _pruefe_ws_gap_und_armiere(self, jetzt: datetime) -> None:
        """T-0306: armiert den Replay-Guard, wenn die WS-Verbindung laut
        Event-Strom offline war.

        Der T-0288-Guard armiert nur im Backend-WS-Loop (`starte_websocket`,
        vor `start_ws`). Reconnectet py-smart-gardena INTERN (start_ws kehrt
        nicht zum Loop zurueck), bleibt der Guard stale -> der Replay-Burst
        laeuft ungebremst -> Phantom-OEFFNEN/SCHLIESSEN (Realfall 16.06.:
        43-min-Luecke 03:48->04:31, dann 20-Event-Burst -> Hecken-Phantom).

        Diese Methode wird am ANFANG jedes WS-Events (Sensor + Ventil, auch
        unveraenderte) aufgerufen und trackt die Luecke zum letzten Event.
        Luecke > REPLAY_GAP_SEKUNDEN -> Guard armieren, BEVOR der Event
        verarbeitet wird (die bestehenden Guard-Checks in `_on_update` /
        `_verarbeite_ventil_update` unterdruecken den Burst dann).
        """
        letzter = self._letzter_ws_event
        self._letzter_ws_event = jetzt
        if letzter is None:
            return
        luecke_s = (jetzt - letzter).total_seconds()
        if luecke_s >= REPLAY_GAP_SEKUNDEN:
            self._replay_guard_bis = jetzt + timedelta(seconds=REPLAY_GUARD_SEKUNDEN)
            self._replay_guard_armiert_am = jetzt
            self._metriken.inc("replay_guard_armiert", "ws_gap")
            logger.warning(
                "gardena.replay_guard_armiert_nach_ws_gap",
                luecke_s=round(luecke_s, 1),
                guard_bis=self._replay_guard_bis.isoformat(),
            )
            # T-0324: Nach einem langen WS-Gap (Host war offline) holt die WS
            # selbst auf, aber der Sensor-DHS-Backfill laeuft sonst erst bei
            # Startup/Cadence -> Sensorwerte bleiben stale bis manueller Restart.
            # Hier sofort die registrierten Catch-up-Callbacks triggern.
            for cb in self._ws_gap_callbacks:
                self._starte_callback_task(cb(luecke_s))

    def _verarbeite_ventil_update(
        self,
        valve_id: str,
        valve_name: str | None,
        aktivitaet: str,
        device_name: str,
    ) -> None:
        """Kanonischer Pfad fuer einen Ventil-Statuswechsel.

        Wird von _erstelle_ventil_callback fuer beide Device-Typen aufgerufen
        (WATER_CONTROL mit einem Ventil, SMART_IRRIGATION_CONTROL mit N Ventilen).
        Key ueberall = valve_id (UUID des Ventils selbst, nicht des Devices).

        T-0055-B4: jeder Entscheidungs-Zweig inkrementiert einen Counter in
        `self._metriken`, damit unverhoffte Verluste sichtbar werden.
        """
        # Roh-Counter + Log BEVOR irgendwelche Filter greifen.
        # T-0055-B4-Runde-2 (22.04.): Log auf INFO hochgezogen, damit der
        # stdout-Strom im interaktiven Start (`./start.sh`) die rohen
        # Activity-Werte zeigt. So kann man nach vermissten Bewaesserungen
        # manuell rekonstruieren, welcher Activity-String bei welchem
        # valve_id zu welchem Zeitpunkt reinkam (oder eben nicht kam).
        self._metriken.inc("events_empfangen", aktivitaet or "NONE")
        logger.info(
            "ws.event_roh",
            valve_id=valve_id,
            activity=aktivitaet,
            valve_name=valve_name,
            geraet=device_name,
        )

        # T-0306: WS-Gap-Detection VOR allen Filtern/Early-Returns -- auch ein
        # unveraenderter Event beweist, dass die WS lebt, und muss die Luecke
        # nachfuehren. Bei erkanntem Gap armiert das den Replay-Guard, den der
        # Check weiter unten dann anwendet.
        jetzt = datetime.now()
        self._pruefe_ws_gap_und_armiere(jetzt)

        if aktivitaet in (None, "N/A"):
            self._metriken.inc("events_verworfen", "aktivitaet_leer")
            return

        self._markiere_ventil_status_gesehen(valve_id)

        vorheriger = self._ventil_status.get(valve_id)
        if aktivitaet == vorheriger:
            # Sub-Label = konkreter Activity-Wert, damit wir im Heartbeat
            # direkt sehen, welcher Zustand (`CLOSED`, `MANUAL_WATERING`, ...)
            # als "unveraendert" gefiltert wird. Wenn waehrend einer
            # Bewaesserung nur {'unveraendert:CLOSED': N} vorkommt, heisst
            # das: der Library-Callback propagiert den Open-Zustand nicht.
            self._metriken.inc("events_verworfen", f"unveraendert:{aktivitaet}")
            return  # Keine Aenderung

        # Initial-Sichtung eines Ventils: nur State speichern, kein Event emittieren.
        # Sonst produziert jeder Prozess-Start ein Phantom-SCHLIESSEN (dauer_s=0)
        # fuer jedes gerade ruhende Ventil in der DB.
        if vorheriger is None:
            self._ventil_status[valve_id] = aktivitaet
            # Falls Service mitten in laufender Bewaesserung startet:
            # Offenstand merken, damit ein spaeteres SCHLIESSEN eine (anteilige)
            # Dauer bekommt. Das Oeffnungs-Event selbst haben wir verpasst.
            if aktivitaet in self._VENTIL_OFFEN_STATES:
                self._ventil_offen_seit[valve_id] = datetime.now()
                # T-0455: auch bei der Initial-Sichtung mitschreiben. Das
                # OEFFNEN haben wir verpasst, die Aktivitaet sagt aber
                # weiterhin, WER giesst -- ohne das bekaeme ein Lauf, der
                # ueber einen Service-Neustart hinweg lief, beim SCHLIESSEN
                # faelschlich MANUELL statt ZEITPLAN.
                self._ventil_offen_ausloser[valve_id] = self._offen_ausloser(
                    aktivitaet,
                )
            self._metriken.inc("events_verworfen", "initial_state")
            return

        # T-0261: Wechsel innerhalb derselben semantischen Zustands-Menge
        # ist KEIN neuer Bewaesserungs-Event. Konkretes Beispiel: Gardena
        # sendet beim manuellen App-Start zwei Activity-Wechsel kurz
        # hintereinander (z.B. `MANUAL_WATERING` -> `OPEN`, ~0.9 s
        # Abstand) -- beide gehoeren zur SELBEN Bewaesserung. Vor T-0261
        # erzeugte das zwei OEFFNEN-Eintraege; der zweite blieb verwaist
        # (kein Pair-SCHLIESSEN), und der OrphanCloseJob (T-0210) hat
        # ihn 30+ min spaeter mit synthetischer watchdog-Dauer
        # geschlossen -> die ML-Features in `_bewaesserungs_features`
        # zaehlten die Bewaesserungs-Dauer ~4x. Fix: nur State
        # aktualisieren, keinen Callback feuern. Realfall 27.05.07:39
        # bambuswald: zwei OEFFNEN 0.879 s auseinander, manueller
        # SCHLIESSEN 957 s (real) + watchdog-SCHLIESSEN 2954 s (Folge).
        if (vorheriger in self._VENTIL_OFFEN_STATES
                and aktivitaet in self._VENTIL_OFFEN_STATES):
            self._ventil_status[valve_id] = aktivitaet
            # T-0455: `_ventil_offen_ausloser` bewusst NICHT nachziehen. Die
            # ERSTE Offen-Aktivitaet entscheidet, damit OEFFNEN und
            # SCHLIESSEN garantiert denselben Ausloeser tragen -- das Paar
            # ist die Einheit, die Bilanz/Wirkungsrate auswerten, und ein
            # Update hier wuerde nur das SCHLIESSEN umetikettieren, weil das
            # OEFFNEN schon geschrieben ist. Restrisiko: kaeme je eine
            # Sequenz OPEN -> SCHEDULED_WATERING, bliebe der Lauf MANUELL
            # (= Verhalten vor T-0455, keine Verschlechterung). Bei der
            # Smart Irrigation Control tritt das nicht auf: sie meldet
            # MANUAL_/SCHEDULED_WATERING zuerst, OPEN/OPENING gehoeren zur
            # Water-Control-Geraeteklasse.
            self._metriken.inc(
                "events_verworfen", f"intra_offen:{aktivitaet}",
            )
            return
        if (vorheriger in self._VENTIL_ZU_STATES
                and aktivitaet in self._VENTIL_ZU_STATES):
            self._ventil_status[valve_id] = aktivitaet
            self._metriken.inc(
                "events_verworfen", f"intra_zu:{aktivitaet}",
            )
            return

        self._ventil_status[valve_id] = aktivitaet
        zone_id = self._valve_zu_zone(valve_name, valve_id)
        # `jetzt` ist oben (T-0306-Gap-Check) bereits gesetzt -- denselben
        # Zeitstempel weiterverwenden (Guard-Check + Event-Zeitstempel).

        # T-0288: Replay-Guard. Direkt nach einem (Re)Connect spielt die
        # py-smart-gardena-Lib gepufferte Alt-Events erneut ein (Realfall
        # 03.06. nach Laptop-Schlaf: Morgen-Peaks mit Echtzeit-Stempel ->
        # Cross-State-Wechsel CLOSED<->OPEN -> Phantom-OEFFNEN/SCHLIESSEN
        # fuer alle vier Ventile). Im Guard-Fenster nur State re-baselinen
        # (analog Initial-Sichtung), KEIN Event emittieren.
        if (
            self._replay_guard_bis is not None
            and jetzt < self._replay_guard_bis
            and not self._haelt_eigener_lauf(valve_id)
        ):
            # T-0306: Bei einem guard-unterdrueckten OEFFNEN KEIN
            # `_ventil_offen_seit` ankern. Sonst koennte ein SPAETERER,
            # ausserhalb des 120s-Fensters liegender (z.B. in einem zweiten
            # Replay-Schwall replayter) SCHLIESSEN eine riesige Phantom-Dauer
            # gegen diesen Replay-OEFFNEN-Zeitpunkt rechnen (Realfall 16.06.:
            # OEFFNEN 04:31 -> SCHLIESSEN 05:36 = 3882 s Phantom). Echte
            # Laeufe waehrend der Offline-Phase fuehrt der DHS-Backfill
            # (gardena_web, automatik) ohnehin korrekt -- der WS-Pfad ist da
            # redundant. Ohne Anker bekommt ein solcher CLOSE dauer=0 und
            # faellt durch WIRKUNGSRATE_MIN_DAUER/leck_detektor-Filter.
            if aktivitaet in self._VENTIL_ZU_STATES:
                self._ventil_offen_seit.pop(valve_id, None)
                self._ventil_offen_ausloser.pop(valve_id, None)
            self._metriken.inc("events_verworfen", f"replay_guard:{aktivitaet}")
            logger.info(
                "gardena.replay_guard_unterdrueckt",
                valve_id=valve_id, zone=zone_id, activity=aktivitaet,
                guard_bis=self._replay_guard_bis.isoformat(),
            )
            return

        # T-0547: Der Guard war armiert, aber dieses Ventil fuehrt eine
        # unserer Sicherungen selbst als offen -- das ist kein Replay, sondern
        # der erwartete Zustandswechsel unseres eigenen Laufs. Sichtbar
        # machen, damit die Ausnahme im Log auffindbar bleibt.
        if self._replay_guard_bis is not None and jetzt < self._replay_guard_bis:
            self._metriken.inc("replay_guard_durchgelassen", aktivitaet)
            logger.info(
                "gardena.replay_guard_eigener_lauf_durchgelassen",
                valve_id=valve_id, zone=zone_id, activity=aktivitaet,
                guard_bis=self._replay_guard_bis.isoformat(),
            )

        if aktivitaet in self._VENTIL_OFFEN_STATES:
            self._ventil_offen_seit[valve_id] = jetzt
            # T-0455: Zeitplan vs. App-Bedienung trennen (vorher pauschal
            # MANUELL). Der Wert wird fuer das spaetere SCHLIESSEN gemerkt.
            offen_ausloser = self._offen_ausloser(aktivitaet)
            self._ventil_offen_ausloser[valve_id] = offen_ausloser
            ereignis = VentilEreignis(
                zeitstempel=jetzt,
                zone_id=zone_id,
                ventil_id=valve_id,
                aktion=VentilAktion.OEFFNEN,
                dauer_sekunden=0,
                ausloser=offen_ausloser,
            )
            logger.info(
                "gardena.ventil_geoeffnet",
                zone=zone_id,
                valve_name=valve_name,
                activity=aktivitaet,
                geraet=device_name,
            )
            self._metriken.inc("events_emittiert", "oeffnen")
            for cb in self._ventil_callbacks:
                self._starte_callback_task(cb(ereignis))

        elif aktivitaet in self._VENTIL_ZU_STATES:
            offen_seit = self._ventil_offen_seit.pop(valve_id, None)
            dauer = int((jetzt - offen_seit).total_seconds()) if offen_seit else 0
            # T-0455: Ausloeser vom zugehoerigen OEFFNEN uebernehmen, damit
            # das Paar konsistent bleibt. Fallback MANUELL nur, wenn wir das
            # OEFFNEN nie gesehen haben (Prozessstart mitten im Lauf ohne
            # Initial-Sichtung) -- das ist der bisherige Wert, also keine
            # Verhaltensaenderung fuer diesen Fall.
            schliess_ausloser = self._ventil_offen_ausloser.pop(
                valve_id, Ausloser.MANUELL,
            )
            ereignis = VentilEreignis(
                zeitstempel=jetzt,
                zone_id=zone_id,
                ventil_id=valve_id,
                aktion=VentilAktion.SCHLIESSEN,
                dauer_sekunden=dauer,
                ausloser=schliess_ausloser,
            )
            logger.info(
                "gardena.ventil_geschlossen",
                zone=zone_id,
                valve_name=valve_name,
                dauer_s=dauer,
                ausloser=schliess_ausloser.value,
                geraet=device_name,
            )
            self._metriken.inc("events_emittiert", "schliessen")
            for cb in self._ventil_callbacks:
                self._starte_callback_task(cb(ereignis))
        else:
            # Aktivitaet ist weder in OFFEN- noch ZU-Whitelist — das ist die
            # wahrscheinlichste Ursache fuer verpasste Events. Wichtig als
            # Warning (nicht Debug): jede neue/unbekannte Status-Variante faellt auf.
            self._metriken.inc("events_verworfen", f"unbekannter_status:{aktivitaet}")
            logger.warning(
                "gardena.ventil_status_unbekannt",
                valve_id=valve_id, valve_name=valve_name,
                activity=aktivitaet, geraet=device_name,
                hinweis="Status nicht in _VENTIL_OFFEN_STATES/_VENTIL_ZU_STATES",
            )

    def _erstelle_ventil_callback(self, geraet_id: str):
        """Erstellt einen Geraete-Callback fuer Ventil-Statusaenderungen.

        Unterstuetzt beide Device-Typen:
          - WATER_CONTROL (Einzel-Ventil): device.valve_activity / device.valve_name
          - SMART_IRRIGATION_CONTROL (Multi-Ventil, z.B. Dual Water Control):
            device.valves = {valve_id: {activity, name, state, ...}, ...}
        """
        def _on_update(device):
            device_typ = getattr(device, "type", None)

            if device_typ == "SMART_IRRIGATION_CONTROL":
                valves = getattr(device, "valves", {}) or {}
                for valve_id, valve_data in valves.items():
                    valve_name = valve_data.get("name")
                    # Einmaliger Debug-Log pro Valve bei Erst-Sichtung
                    if valve_id not in self._ventil_geloggte_namen:
                        self._ventil_geloggte_namen.add(valve_id)
                        logger.info(
                            "gardena.valve_entdeckt",
                            valve_id=valve_id,
                            valve_name=valve_name,
                            geraet=device.name,
                        )
                    self._verarbeite_ventil_update(
                        valve_id=valve_id,
                        valve_name=valve_name,
                        aktivitaet=valve_data.get("activity"),
                        device_name=device.name,
                    )
            else:
                # WATER_CONTROL (oder Fallback): genau ein Ventil pro Device
                valve_id = getattr(device, "valve_id", None) or geraet_id
                valve_name = getattr(device, "valve_name", None) or device.name
                if valve_id not in self._ventil_geloggte_namen:
                    self._ventil_geloggte_namen.add(valve_id)
                    logger.info(
                        "gardena.valve_entdeckt",
                        valve_id=valve_id,
                        valve_name=valve_name,
                        geraet=device.name,
                        typ=device_typ,
                    )
                self._verarbeite_ventil_update(
                    valve_id=valve_id,
                    valve_name=valve_name,
                    aktivitaet=getattr(device, "valve_activity", None),
                    device_name=device.name,
                )

        return _on_update

    def setze_reconnect_sync_callback(
        self,
        callback: "Callable[[dict[str, str]], Awaitable[int]] | None",
    ) -> None:
        """T-0210-Folge: Hook fuer Live-State-Sync nach Reconnect.

        `callback(live_state)` wird VOR jedem `start_ws`-Versuch
        aufgerufen (inkl. Initial-Start) mit einem Mapping
        `zone_id -> "offen" | "zu" | "unbekannt"`. Der Handler kann
        Diskrepanzen zur DB als synthetische SCHLIESSEN-Events
        nachtragen und gibt die Anzahl der nachgetragenen Events
        zurueck (rein informativ fuer's Logging).
        """
        self._reconnect_sync_callback = callback

    def _live_state_pro_zone(self) -> dict[str, str]:
        """T-0210-Folge: bauen einer `zone_id -> activity`-Map aus dem
        aktuell von `update_devices` gefuellten py-smart-gardena-State.

        Wert ist `"offen"`, `"zu"` oder `"unbekannt"` -- abstrakt
        gegenueber den vielen activity-Strings (OPEN/OPENING/
        MANUAL_WATERING/SCHEDULED_WATERING vs CLOSED/CLOSING vs Rest).
        Pro Zone der erste Ventil-Hit gewinnt; mehrere Ventile pro
        Zone gibt es bei uns nicht.
        """
        if self._smart_system is None or self._location_id is None:
            return {}
        location = self._smart_system.locations.get(self._location_id)
        if location is None:
            return {}
        ergebnis: dict[str, str] = {}
        for geraet in location.devices.values():
            if geraet.type not in (
                "WATER_CONTROL", "SMART_IRRIGATION_CONTROL",
            ):
                continue
            # py-smart-gardena legt Valves entweder als geraet.valves[i]
            # (DSWC) oder als direkte Eigenschaften (Single-WC) ab. Wir
            # nutzen `valves`-Liste falls vorhanden; sonst Fallback auf
            # die Geraet-Eigenschaften.
            valves = getattr(geraet, "valves", None)
            if valves:
                for v in valves.values() if hasattr(valves, "values") else valves:
                    # T-0302: py-smart-gardena legt valves als dict-von-dicts ab
                    # ({id, name, activity, state}). getattr() darauf liefert
                    # IMMER None -> der ganze T-0210-Reconnect-Sync war fuer DSWC
                    # ein No-op (0 Sync-Events je). dict-Zugriff mit Objekt-
                    # Fallback (aeltere/andere Lib-Versionen).
                    if isinstance(v, dict):
                        name = v.get("name")
                        vid = v.get("id") or v.get("valve_id") or ""
                        activity = v.get("activity")
                    else:
                        name = getattr(v, "name", None)
                        vid = getattr(v, "id", None) or getattr(v, "valve_id", None) or ""
                        activity = getattr(v, "activity", None)
                    if not activity:
                        continue
                    zone_id = self._valve_zu_zone(name, vid)
                    if activity in self._VENTIL_OFFEN_STATES:
                        ergebnis[zone_id] = "offen"
                    elif activity in self._VENTIL_ZU_STATES:
                        # "zu" nur setzen falls nicht schon "offen"
                        # eingetragen wurde (anderer Valve hat Vorrang).
                        ergebnis.setdefault(zone_id, "zu")
                    else:
                        ergebnis.setdefault(zone_id, "unbekannt")
            else:
                # Single-Valve Water Control: aktivitaet aus
                # `valve_activity`.
                name = getattr(geraet, "name", None)
                activity = getattr(geraet, "valve_activity", None)
                if not activity:
                    continue
                zone_id = self._valve_zu_zone(name, geraet.id)
                if activity in self._VENTIL_OFFEN_STATES:
                    ergebnis[zone_id] = "offen"
                elif activity in self._VENTIL_ZU_STATES:
                    ergebnis.setdefault(zone_id, "zu")
                else:
                    ergebnis.setdefault(zone_id, "unbekannt")
        return ergebnis

    async def _trigger_reconnect_sync(self) -> None:
        """T-0210-Folge: ruft den Sync-Callback mit frischem Live-State
        auf. Fehler werden gefangen + geloggt -- der WS-Loop soll
        nicht crashen, falls die DB gerade nicht verfuegbar ist.
        """
        if self._reconnect_sync_callback is None:
            return
        try:
            live_state = self._live_state_pro_zone()
            n = await self._reconnect_sync_callback(live_state)
            if n > 0:
                logger.info(
                    "gardena.reconnect_sync.synthetische_events",
                    anzahl=n, zonen_im_state=len(live_state),
                )
            else:
                logger.debug(
                    "gardena.reconnect_sync.kein_drift",
                    zonen_im_state=len(live_state),
                )
        except Exception:
            logger.exception("gardena.reconnect_sync.fehler")

    async def starte_websocket(self, max_retries: int = 0) -> None:
        """Startet die WebSocket-Verbindung fuer Echtzeit-Updates.

        Registriert Callbacks auf allen Sensor-/Ventil-Geraeten und startet
        den WebSocket-Listener. Bei Verbindungsabbruch: exponentieller Backoff.

        Args:
            max_retries: 0 = unbegrenzt (Dauerbetrieb), >0 = max. Versuche.
        """
        assert self._smart_system is not None
        assert self._location_id is not None

        location = self._smart_system.locations[self._location_id]

        # Callbacks auf Sensoren und Ventilen registrieren (nur beim ersten Mal)
        if not self._callbacks_registriert:
            for geraet_id, geraet in location.devices.items():
                if geraet.type in ("SENSOR", "SOIL_SENSOR"):
                    callback = self._erstelle_sensor_callback(geraet_id)
                    geraet.add_callback(callback)
                    logger.info("gardena.sensor_callback_registriert", geraet=geraet.name)
                elif geraet.type in ("WATER_CONTROL", "SMART_IRRIGATION_CONTROL"):
                    callback = self._erstelle_ventil_callback(geraet_id)
                    geraet.add_callback(callback)
                    logger.info("gardena.ventil_callback_registriert", geraet=geraet.name)
            self._callbacks_registriert = True

        # Reconnection-Loop mit exponentiellem Backoff
        versuch = 0
        backoff = 5  # Startwert Sekunden
        max_backoff = 300  # Max 5 Minuten

        while True:
            try:
                # T-0210-Folge: VOR dem WS-Start (sowohl Initial als
                # auch Reconnect) den State mit der DB abgleichen.
                # `update_devices` lief entweder im Init (verbinden())
                # oder im Re-Auth-Pfad unten — entsprechend ist der
                # py-smart-gardena-State frisch und enthaelt den
                # aktuellen activity-String pro Ventil.
                # T-0288: Replay-Guard pro (Re)Connect armieren -- direkt
                # nach dem Verbinden spielt die Lib gepufferte Alt-Events
                # erneut ein; im Fenster darunter keine Phantom-Transitionen.
                _guard_jetzt = datetime.now()
                self._replay_guard_bis = (
                    _guard_jetzt + timedelta(seconds=REPLAY_GUARD_SEKUNDEN)
                )
                self._replay_guard_armiert_am = _guard_jetzt
                await self._trigger_reconnect_sync()

                await self._smart_system.start_ws(location)
                logger.info("gardena.websocket_gestartet")
                # start_ws blockiert bis Verbindung abbricht
                # Wenn wir hier ankommen, ist die Verbindung beendet
                versuch = 0
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                versuch += 1
                if max_retries and versuch >= max_retries:
                    logger.error(
                        "gardena.websocket_max_retries",
                        versuche=versuch,
                        fehler=str(exc),
                    )
                    raise
                logger.warning(
                    "gardena.websocket_abgebrochen",
                    versuch=versuch,
                    naechster_versuch_s=backoff,
                    fehler=str(exc),
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

                # T-0210-Folge: nach jedem Reconnect-Versuch
                # `update_devices` aufrufen (nicht erst alle 5
                # Versuche). Sonst arbeitet der Sync-Callback mit
                # stale State aus dem letzten erfolgreichen Connect.
                # Schlaegt das fehl, faellt der Loop in die nicht-
                # gesynced-Variante zurueck — kein Crash.
                try:
                    await self._smart_system.update_devices(location)
                except Exception as ud_exc:
                    logger.warning(
                        "gardena.update_devices_nach_reconnect_fehler",
                        fehler=str(ud_exc),
                    )

                # Re-Auth bei laengerem Ausfall
                if versuch % 5 == 0:
                    try:
                        logger.info("gardena.re_auth", versuch=versuch)
                        await self._smart_system.authenticate()
                        await self._smart_system.update_locations()
                        location = self._smart_system.locations[self._location_id]
                        await self._smart_system.update_devices(location)
                    except Exception as auth_exc:
                        logger.error("gardena.re_auth_fehlgeschlagen", fehler=str(auth_exc))

    def offene_valves(self) -> dict[str, dict]:
        """Gibt alle aktuell offenen Valves zurueck (App + Schedule + Auto).

        Quelle: Live-WebSocket-State (`_ventil_status` + `_ventil_offen_seit`).
        Der Aufrufer mappt valve_id -> Kanal/Zone selbst.

        Returns:
            dict valve_id -> {"activity": str, "offen_seit": datetime}.
        """
        return {
            vid: {
                "activity": self._ventil_status.get(vid, ""),
                "offen_seit": self._ventil_offen_seit.get(vid),
            }
            for vid in self._ventil_offen_seit.keys()
        }

    # --- Geraete-Zugriff ---

    def hole_geraete(self) -> dict:
        """Gibt alle Geraete mit ihren aktuellen Zustaenden zurueck."""
        assert self._smart_system is not None
        assert self._location_id is not None

        location = self._smart_system.locations[self._location_id]
        ergebnis = {}

        for geraet_id, geraet in location.devices.items():
            info = {
                "id": geraet_id,
                "name": geraet.name,
                "typ": geraet.type,
            }

            if geraet.type in ("SENSOR", "SOIL_SENSOR"):
                info.update({
                    "boden_feuchte": getattr(geraet, "soil_humidity", None),
                    "boden_temperatur": getattr(geraet, "soil_temperature", None),
                    "umgebungs_temperatur": getattr(geraet, "ambient_temperature", None),
                    "licht_intensitaet": getattr(geraet, "light_intensity", None),
                    "batterie": getattr(geraet, "battery_level", None),
                })
            elif geraet.type in ("WATER_CONTROL", "SMART_IRRIGATION_CONTROL"):
                info.update({
                    "ventil_status": getattr(geraet, "valve_state", None),
                    "ventil_aktivitaet": getattr(geraet, "valve_activity", None),
                })

            ergebnis[geraet_id] = info

        return ergebnis

    def finde_geraet_nach_typ(self, typ: str) -> list:
        """Findet Geraete eines bestimmten Typs."""
        assert self._smart_system is not None
        assert self._location_id is not None

        location = self._smart_system.locations[self._location_id]
        return [g for g in location.devices.values() if g.type == typ]

    def hole_sensor_aktuell(self, geraet_id: str) -> SensorMessung | None:
        """Liest den aktuellen Sensorwert direkt (nicht aus DB)."""
        assert self._smart_system is not None
        assert self._location_id is not None

        location = self._smart_system.locations[self._location_id]
        geraet = location.devices.get(geraet_id)

        if not geraet or geraet.type not in ("SENSOR", "SOIL_SENSOR"):
            return None

        zone_id = self._geraete_zone_map.get(geraet_id, geraet_id)
        return SensorMessung(
            zeitstempel=datetime.now(),
            zone_id=zone_id,
            geraet_id=geraet_id,
            boden_feuchte=getattr(geraet, "soil_humidity", None),
            boden_temperatur=getattr(geraet, "soil_temperature", None),
            umgebungs_temperatur=getattr(geraet, "ambient_temperature", None),
            licht_intensitaet=getattr(geraet, "light_intensity", None),
            batterie_prozent=getattr(geraet, "battery_level", None),
        )

    # --- Ventilsteuerung ---

    async def ventil_oeffnen(
        self,
        geraet_id: str,
        dauer_sekunden: int = 1800,
        valve_id: str | None = None,
    ) -> None:
        """Oeffnet ein Ventil.

        py-smart-gardena hat zwei Geraete-Klassen mit unterschiedlicher API:
        - `WaterControl` (1 Kanal): `start_seconds_to_override(duration)` ohne
          valve_id (das Geraet hat genau eine Valve).
        - `SmartIrrigationControl` (N Kanaele, z. B. unser "Dual Water Control"):
          `start_seconds_to_override(duration, valve_id)` — pro Kanal, valve_id
          ist die UUID der Valve aus `geraet.valves`.

        Der Cloud-Override schliesst nach `dauer_sekunden` automatisch
        serverseitig (Sicherheitsnetz, max 3600 s). Bei `dauer < 3600` setzen
        wir zusaetzlich einen lokalen Schliess-Timer.

        Args:
            geraet_id: ID des Water-Control-Geraets.
            dauer_sekunden: Gewuenschte Oeffnungsdauer (1..3600).
            valve_id: Pflicht bei `SmartIrrigationControl`, ignoriert bei
                `WaterControl`. Wenn None und Geraet ist multi-valve: ValueError.
        """
        assert self._smart_system is not None
        assert self._location_id is not None

        location = self._smart_system.locations[self._location_id]
        geraet = location.devices.get(geraet_id)

        if not geraet:
            raise ValueError(f"Geraet {geraet_id} nicht gefunden")

        logger.info(
            "gardena.ventil_oeffnen",
            geraet=geraet.name, dauer=dauer_sekunden, valve_id=valve_id,
        )

        if getattr(geraet, "type", None) == "SMART_IRRIGATION_CONTROL":
            if not valve_id:
                raise ValueError(
                    f"SmartIrrigationControl {geraet.name} braucht valve_id "
                    "(Multi-Channel-Geraet)"
                )
            await geraet.start_seconds_to_override(dauer_sekunden, valve_id)
        else:
            # WaterControl (1 Kanal): valve_id wird ignoriert, falls mitgegeben.
            await geraet.start_seconds_to_override(dauer_sekunden)

        # Lokaler Schliess-Timer fuer dauer < 3600 (Cloud-Override deckt 3600).
        # T-0300: Handle pro Valve speichern + alten canceln. Sonst ueberlebt
        # der Timer einen Stop und schliesst den naechsten Lauf vorzeitig.
        if dauer_sekunden < 3600:
            self._cancel_schliess_timer(geraet_id, valve_id)
            self._schliess_timer[(geraet_id, valve_id)] = (
                asyncio.get_event_loop().call_later(
                    dauer_sekunden,
                    lambda: self._starte_callback_task(
                        self.ventil_schliessen(geraet_id, valve_id=valve_id),
                    ),
                )
            )

    def _cancel_schliess_timer(
        self, geraet_id: str, valve_id: str | None,
    ) -> None:
        """T-0300: Cancelt + entfernt den lokalen Schliess-Timer einer Valve."""
        handle = self._schliess_timer.pop((geraet_id, valve_id), None)
        if handle is not None:
            handle.cancel()

    async def ventil_schliessen(
        self,
        geraet_id: str,
        valve_id: str | None = None,
    ) -> None:
        """Schliesst ein Ventil.

        Analog zu `ventil_oeffnen`: bei `SmartIrrigationControl` ist `valve_id`
        Pflicht, bei `WaterControl` wird er ignoriert.
        """
        # T-0300: lokalen Schliess-Timer dieser Valve entfernen -- egal ob der
        # Close vom Timer selbst, von einem Stop (VentilSicherung) oder extern
        # kommt. Sonst feuert ein stale Timer spaeter auf einen Folge-Lauf.
        self._cancel_schliess_timer(geraet_id, valve_id)
        assert self._smart_system is not None
        assert self._location_id is not None

        location = self._smart_system.locations[self._location_id]
        geraet = location.devices.get(geraet_id)

        if not geraet:
            raise ValueError(f"Geraet {geraet_id} nicht gefunden")

        logger.info(
            "gardena.ventil_schliessen", geraet=geraet.name, valve_id=valve_id,
        )

        if getattr(geraet, "type", None) == "SMART_IRRIGATION_CONTROL":
            if not valve_id:
                raise ValueError(
                    f"SmartIrrigationControl {geraet.name} braucht valve_id "
                    "(Multi-Channel-Geraet)"
                )
            await geraet.stop_until_next_task(valve_id)
        else:
            await geraet.stop_until_next_task()

    def ist_multi_valve_geraet(self, geraet_id: str) -> bool | None:
        """T-0448: Ist das Geraet mehrkanalig (valve_id ist Pflicht)?

        Spiegelt bewusst exakt die Weiche aus `ventil_oeffnen` /
        `ventil_schliessen` (`geraet.type == "SMART_IRRIGATION_CONTROL"`),
        damit Aufrufer vorab wissen, ob ein Aufruf ohne `valve_id` dort
        zwingend in den ValueError laeuft. Andere Typ-Quelle waere eine
        zweite Wahrheit ueber denselben Sachverhalt.

        Returns:
            True  -> mehrkanalig, valve_id Pflicht.
            False -> einkanalig (WaterControl), valve_id wird ignoriert.
            None  -> nicht beantwortbar (nicht verbunden / Geraet unbekannt).
                Aufrufer duerfen daraus KEIN "ist einkanalig" ableiten.
        """
        if not self._smart_system or not self._location_id:
            return None
        location = self._smart_system.locations.get(self._location_id)
        if not location:
            return None
        geraet = location.devices.get(geraet_id)
        if geraet is None:
            return None
        return getattr(geraet, "type", None) == "SMART_IRRIGATION_CONTROL"

    def baue_kanal_zu_valve_id(
        self,
        geraet_id: str,
        zonen_kanal_zuordnung: list[tuple[int, str | None, str]],
    ) -> dict[int, str]:
        """Baut das Mapping `kanal -> valve_id` fuer ein SmartIrrigationControl.

        Bei einkanaligen `WaterControl`-Geraeten gibt es keine Valves-Liste,
        Rueckgabe ist dann ein leeres Dict (Caller benutzt fuer dieses Geraet
        keine valve_id-Aufrufe).

        `zonen_kanal_zuordnung`: Liste `(kanal, ventil_name, zone_name)`. Match
        ueber `valve.name` case-insensitive gegen `ventil_name` ODER `zone_name`
        (ventil_name hat Vorrang, falls gesetzt).
        """
        assert self._smart_system is not None
        assert self._location_id is not None
        location = self._smart_system.locations[self._location_id]
        geraet = location.devices.get(geraet_id)
        if geraet is None or getattr(geraet, "type", None) != "SMART_IRRIGATION_CONTROL":
            return {}

        valves: dict = getattr(geraet, "valves", {}) or {}
        if not valves:
            logger.warning(
                "gardena.kanal_zu_valve.keine_valves",
                geraet_id=geraet_id, geraet_name=geraet.name,
            )
            return {}

        # valve_name (lower) -> valve_id
        valve_index: dict[str, str] = {}
        for vid, vdata in valves.items():
            vname = (vdata.get("name") or "").strip().lower()
            if vname:
                valve_index[vname] = vid

        mapping: dict[int, str] = {}
        ungemappte_kanaele: list[int] = []
        for kanal, ventil_name, zone_name in zonen_kanal_zuordnung:
            kandidaten: list[str] = []
            if ventil_name:
                kandidaten.append(ventil_name.strip().lower())
            if zone_name:
                kandidaten.append(zone_name.strip().lower())
            valve_id = next(
                (valve_index[k] for k in kandidaten if k in valve_index),
                None,
            )
            if valve_id is None:
                ungemappte_kanaele.append(kanal)
                continue
            mapping[kanal] = valve_id

        if ungemappte_kanaele:
            logger.warning(
                "gardena.kanal_zu_valve.ungemappt",
                kanaele=ungemappte_kanaele,
                bekannte_valves=list(valve_index.keys()),
                hinweis="Setze 'ventil_name' in der Zone-Konfig auf den "
                        "exakten Gardena-App-Namen der Valve.",
            )
        else:
            logger.info(
                "gardena.kanal_zu_valve.gemappt",
                mapping={k: v[:8] + "..." for k, v in mapping.items()},
            )
        return mapping

    async def trennen(self) -> None:
        """Trennt die Verbindung zur Gardena API."""
        if self._smart_system:
            logger.info("gardena.trennen")
            # SmartSystem hat keine explizite close-Methode,
            # der WebSocket wird beim GC aufgeraeumt
            self._smart_system = None
