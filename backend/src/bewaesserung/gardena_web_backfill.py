"""T-0055-B3 — DHS-Backfill gegen smart.gardena.com.

Holt die tatsaechlich abgelaufenen Bewaesserungs-Events aus dem Device History
Service der Gardena-Webapp (inoffiziell, aber stabil genutzt) und schreibt
sie als `ventil_ereignis` in die DB.

**Warum ueberhaupt**: Der WebSocket-Pfad ueber die Developer-API verliert
Events (nachgewiesen am 18.04.2026: 12 von 12 Events nicht geloggt trotz
laufendem Backend). DHS ist die Ground-Truth.

**Dedup-Prioritaet**: Live-WebSocket-Events (ventil_id = UUID) und manuelle
API-Eintraege (ventil_id = 'manuell') haben Vorrang. DHS-Events werden nur
geschrieben, wenn fuer denselben Start ± 60 s noch kein Event existiert.

**Auth**: separater Customer-Login via `GardenaCustomerAuth` (nicht
Developer-API-Key).

**Inoffizieller Endpoint — Risiko**: Gardena kann diesen Pfad jederzeit
aendern. Der Job logt Fehler und blockt nie den Hauptloop.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

import httpx
import structlog

from bewaesserung.gardena_customer_auth import GardenaCustomerAuth
from bewaesserung.modelle import (
    ist_mehrdeutige_lokalzeit,
    Ausloser,
    VentilAktion,
    VentilEreignis,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

DHS_BASIS_URL = "https://smart.gardena.com/v1/dhs"
PRESET_DUAL = "watering_computer_dual"

# Cadence-Default: 30 Min ist ein guter Kompromiss zwischen Aktualitaet
# und API-Last. Bei Offline-Phasen holt der erste Lauf nach Re-Connect
# alles nach, weil DHS die volle Historie liefert.
INTERVALL_MINUTEN_DEFAULT = 30

# Dedup-Fenster: wenn innerhalb +/- 60 s zur Start-Zeit bereits ein
# Live-Event oder manueller Eintrag existiert, nicht doppelt schreiben.
DEDUP_FENSTER_SEKUNDEN = 60

# T-0555b: ab welcher Unterschreitung gilt ein Lauf als vorzeitig beendet?
# Die DHS-Antwort rundet `duration` gegenueber `stop - start` um bis zu einer
# Minute (beobachtet: 5340 s Spanne, 5400 s gemeldet). 120 s laesst dafuer
# Luft und trennt trotzdem sauber vom Realfall 29.08., wo die Dauer 2728 s
# unter der Spanne lag.
STOP_VS_DAUER_TOLERANZ_S = 120

# T-0055-B4 Option C (29.04.): Konsistenz-Check Live-WS vs. DHS-SCHLIESSEN.
# Wenn das Live-WS-SCHLIESSEN-Event mehr als KORREKTUR_TOLERANZ_SEKUNDEN von
# der DHS-Cloud-Wahrheit abweicht, wird der Live-WS-Wert ueberschrieben.
# Realfall 29.04.: Live-WS loggte 30 min spaeter als App-Stop — sturkturelles
# py-smart-gardena-Problem. DHS ist Cloud-Ground-Truth.
KORREKTUR_TOLERANZ_SEKUNDEN = 30

# T-0321: Karenz, bevor der DHS-Backfill einen FEHLENDEN Live-Close aus der
# Cloud-Ground-Truth nachtraegt. Schuetzt vor Doppel-Close, falls der Lauf noch
# laeuft / der Live-Close nur verspaetet ist. Erst wenn der DHS-Stop sicher in
# der Vergangenheit liegt, gilt der Live-Close als verloren.
CLOSE_INSERT_KARENZ_SEKUNDEN = 300

HTTP_TIMEOUT_S = 30.0


class GardenaWebBackfillJob:
    """Periodischer DHS-Abruf fuer Dual Water Control Events.

    Braucht fuer jedes zu beobachtende Water-Control-Device:
    - `device_id`: Gardena-UUID des Geraets (aus Discovery).
    - `location_id`: Gardena-Location-UUID.
    - `kanal_zu_zonen`: Mapping `ventil_kanal -> list[zone_id]` fuer die
      Kanal-Expansion (pro Event werden mehrere ventil_ereignis-Zeilen
      geschrieben, eine pro Zone am Kanal — wie auch beim Live-Pfad).
    """

    def __init__(
        self,
        auth: GardenaCustomerAuth,
        speicher: Speicher,
        location_id: str,
        water_control_geraet_id: str,
        kanal_zu_zonen: dict[int, list[str]],
        intervall_minuten: int = INTERVALL_MINUTEN_DEFAULT,
    ) -> None:
        self._auth = auth
        self._speicher = speicher
        self._location_id = location_id
        self._device_id = water_control_geraet_id
        self._kanal_zu_zonen = dict(kanal_zu_zonen)
        self._intervall = timedelta(minutes=intervall_minuten)
        self._letzte_aktualisierung: datetime | None = None

    async def aktualisiere_wenn_faellig(self, jetzt: datetime | None = None) -> int:
        """Laeuft max. einmal pro `intervall_minuten`. Gibt Anzahl neuer Events."""
        jetzt = jetzt or datetime.now()
        if (
            self._letzte_aktualisierung
            and jetzt - self._letzte_aktualisierung < self._intervall
        ):
            return 0
        # T-0562: der Anker gehoert ins `finally`. Er ist das Rate-Limit
        # gegen einen inoffiziellen Endpoint, nicht die Erfolgsmeldung --
        # zwei verschiedene Dinge, die hier eins waren. Wirft `aktualisiere`,
        # ist die richtige Antwort "erst in 30 min wieder", nicht "sofort
        # nochmal".
        try:
            return await self.aktualisiere(jetzt)
        finally:
            self._letzte_aktualisierung = jetzt

    async def aktualisiere(self, jetzt: datetime | None = None) -> int:
        """Holt DHS-Daten + persistiert neue Events. Fehler loggen, nicht werfen."""
        jetzt = jetzt or datetime.now()
        try:
            daten = await self._hole_dhs()
        except Exception:
            logger.exception("dhs_backfill.abruf_fehlgeschlagen")
            return 0

        eingelesene = self._parse_events(daten)
        neu = 0
        for event_tuple in eingelesene:
            # T-0562: pro Event isolieren. Vorher riss ein einziger Fehler
            # den ganzen Lauf ab, und alle noch nicht verarbeiteten Events
            # desselben Pulls gingen verloren. Der konkrete Ausloeser war
            # ein UNIQUE-Konflikt im Korrektur-Pfad (s. u.), aber die
            # Isolation gehoert unabhaengig davon hierher: ein einzelnes
            # kaputtes DHS-Event darf den Nachtrag nicht stilllegen.
            try:
                neu += await self._persistiere_event(event_tuple)
            except Exception:
                logger.exception(
                    "dhs_backfill.event_fehlgeschlagen",
                    start=event_tuple[0].isoformat(),
                    kanal=event_tuple[3],
                )

        if neu > 0:
            logger.info(
                "dhs_backfill.events_geschrieben",
                neu=neu, location=self._location_id, device=self._device_id,
            )
        else:
            # Ohne dieses Log ist ein ruhiger DHS-Pull kaum von einem
            # komplett ausgefallenen Job zu unterscheiden — wichtig wenn
            # der Customer-Login abgelaufen ist und `eingelesene` leer bleibt.
            #
            # T-0419 (23.07.): von DEBUG auf INFO. Das Log-Level der Anwendung
            # ist INFO, also wurde diese Zeile NIE ausgegeben -- und damit war
            # der Job faktisch unbeobachtbar. Realfall: der hecke-Close vom
            # 21.07. blieb mit 5400 s statt 3720 s stehen, und es liess sich
            # nicht unterscheiden zwischen (a) DHS liefert den Lauf nicht,
            # (b) DHS liefert ihn, aber die Korrektur greift nicht, und
            # (c) der Job laeuft gar nicht. Genau diese Unterscheidung sollte
            # das Log leisten -- auf DEBUG konnte es das nie.
            #
            # `gesehen` ist dabei die entscheidende Zahl: 0 heisst "DHS gibt
            # nichts her" (Login abgelaufen, Geraet stumm), > 0 heisst "DHS
            # liefert, aber alles ist schon in der DB".
            logger.info(
                "dhs_backfill.keine_neuen_events",
                gesehen=len(eingelesene),
                location=self._location_id, device=self._device_id,
            )
        return neu

    async def _hole_dhs(self) -> dict:
        """HTTP-GET gegen den DHS-Endpoint. Wirft bei Fehler."""
        token = await self._auth.hole_gueltigen_token()
        url = f"{DHS_BASIS_URL}/{self._device_id}"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            antwort = await client.get(
                url,
                params={
                    "preset": PRESET_DUAL,
                    "location_id": self._location_id,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        antwort.raise_for_status()
        return antwort.json()

    def _parse_events(
        self, daten: dict,
    ) -> list[tuple[datetime, datetime, int, int, Ausloser]]:
        """Parst JSON:API-Antwort zu (start, stop, dauer_s, kanal, ausloser)-Tupeln.

        Filtert uebersprungene Bewaesserungen aus: Events mit
        `summary.startswith('SKIPPED_')` oder `decision == 'SKIP'` sind
        geplante Schedules, die die Gardena-App wegen Sensor-Feuchte
        ignoriert hat — sie gingen nie durch das Ventil.
        """
        events = []
        for block in daten.get("included", []):
            if block.get("type") != "dh-action-event":
                continue
            attr = block.get("attributes", {}) or {}
            if not _ist_ausgefuehrt(attr):
                continue
            start_str = attr.get("start")
            stop_str = attr.get("stop")
            if not start_str or not stop_str:
                continue
            try:
                start = _parse_iso(start_str)
                stop = _parse_iso(stop_str)
            except ValueError:
                continue
            dauer = int(attr.get("duration") or (stop - start).total_seconds())
            if dauer <= 0:
                continue
            # T-0555b: `stop` und `duration` koennen sich WIDERSPRECHEN, und
            # dann gilt die Dauer.
            #
            # Realfall 29.08.2026, bambuswald: start 20:58:53, duration 2668,
            # aber stop 22:28:49 -- eine Differenz von 45 Minuten. Die
            # Rueckrechnung `start + duration` = 21:43:21 trifft exakt den
            # Moment, in dem unser eigenes Kommando das Ventil geschlossen hat.
            # Die Deutung, die dazu passt: bei einem VORZEITIG BEENDETEN Lauf
            # meldet die Cloud als `stop` das GEPLANTE Ende und als `duration`
            # die TATSAECHLICHE Laufzeit.
            #
            # **Die Regel ist bewusst ASYMMETRISCH, und das ist der Kern.**
            # Nur eine Dauer, die deutlich KUERZER ist als die Spanne, heisst
            # "vorzeitig beendet". Ist sie laenger oder gleich, ist es
            # Rundung -- die DHS-Antworten der Bestandstests melden z. B.
            # 06:42-08:11 (5340 s Spanne) mit `duration: 5400`, also 60 s
            # mehr. Dort ist `stop` die Wahrheit und darf nicht angefasst
            # werden. Eine symmetrische Regel (`abs(...) > 1`) hat genau
            # diese vier Bestandstests gebrochen -- gefunden, weil sie
            # liefen.
            #
            # **Warum das an dieser Stelle behoben gehoert und nicht weiter
            # unten.** Am 29./30.08. habe ich stattdessen die bereits
            # geschriebenen Zeilen von Hand korrigiert -- und damit gegen
            # diesen Parser gearbeitet, der die Quelle alle 30 min neu liest.
            # Jeder Nachtragslauf hat die Korrektur wieder eingerissen, einmal
            # sogar einen Guss nachgetragen, den es nie gab (#7200/#7201).
            # Wer abgeleitete Daten repariert, ohne die Ableitung zu aendern,
            # fuehrt einen Kampf, den er nicht gewinnen kann.
            spanne = (stop - start).total_seconds()
            if spanne - dauer > STOP_VS_DAUER_TOLERANZ_S:
                stop = start + timedelta(seconds=dauer)
            firmware_id = attr.get("firmware-action-id")
            if firmware_id is None:
                continue
            kanal = int(firmware_id) + 1  # action_0 -> Kanal 1, action_1 -> Kanal 2
            ausloser = _summary_zu_ausloser(
                attr.get("summary") or "", attr.get("action") or "",
            )
            events.append((start, stop, dauer, kanal, ausloser))
        return events

    async def _persistiere_event(
        self,
        event_tuple: tuple[datetime, datetime, int, int, Ausloser],
    ) -> int:
        """Schreibt OEFFNEN + SCHLIESSEN fuer jede Zone am Kanal. Dedup-sicher.

        Heuristik-Events (sensor_heuristik) im selben Fenster werden als
        "Kandidaten" behandelt und durch das DHS-Ground-Truth-Event ERSETZT —
        DHS hat hoehere Prioritaet als Sensor-Heuristik.
        """
        start, stop, dauer, kanal, ausloser = event_tuple
        zonen_ids = self._kanal_zu_zonen.get(kanal, [])
        if not zonen_ids:
            return 0

        geschrieben = 0
        for zone_id in zonen_ids:
            if await self._existiert_ground_truth_bereits(zone_id, start, stop):
                # T-0055-B4 Option C: Live-WS-SCHLIESSEN kann verspaetet sein
                # (py-smart-gardena-Reconnect mit stale state). DHS ist
                # Cloud-Ground-Truth — wenn das Live-WS-SCHLIESSEN deutlich
                # abweicht, mit DHS-Werten korrigieren.
                korrigiert = await self._korrigiere_schliesszeit_falls_abweichend(
                    zone_id, start, stop, dauer, ausloser,
                )
                if korrigiert:
                    logger.info(
                        "dhs.live_ws_schliesszeit_korrigiert",
                        zone_id=zone_id,
                        oeffnen_start=start.isoformat(),
                        dhs_schliesszeit=stop.isoformat(),
                        dhs_dauer_sekunden=dauer,
                    )
                continue
            oeffnen = VentilEreignis(
                zeitstempel=start,
                zone_id=zone_id,
                ventil_id="gardena_web",
                aktion=VentilAktion.OEFFNEN,
                dauer_sekunden=0,
                ausloser=ausloser,
            )
            schliessen = VentilEreignis(
                zeitstempel=stop,
                zone_id=zone_id,
                ventil_id="gardena_web",
                aktion=VentilAktion.SCHLIESSEN,
                dauer_sekunden=dauer,
                ausloser=ausloser,
            )
            # Delete der Heuristik-Kandidaten + Insert OEFFNEN/SCHLIESSEN in
            # EINER Transaktion: sonst zeigt die UI zwischen den Schritten
            # eine "leere" Zone (Heuristik weg, DHS noch nicht da) und ein
            # Insert-Fehler wuerde die Heuristik unwiederbringlich loeschen.
            async with self._speicher.transaktion():
                geloescht = await self._loesche_heuristik_im_fenster(
                    zone_id, start, stop,
                )
                await self._speicher.speichere_ventil_ereignis(oeffnen)
                await self._speicher.speichere_ventil_ereignis(schliessen)
            if geloescht:
                logger.debug(
                    "dhs.heuristik_ersetzt",
                    zone_id=zone_id, anzahl=geloescht,
                    fenster_start=start.isoformat(),
                )
            geschrieben += 1
        return geschrieben

    async def _existiert_ground_truth_bereits(
        self, zone_id: str, start_zeit: datetime,
        stop_zeit: datetime | None = None,
    ) -> bool:
        """True wenn ein Live/Manuell/DHS-Event den Lauf schon abdeckt.

        Heuristik-Events (`ventil_id='sensor_heuristik'`) gelten als Kandidaten
        mit niedrigerer Prioritaet und blockieren den DHS-Insert NICHT — sie
        werden separat via `_loesche_heuristik_im_fenster` ersetzt.

        F10: Nicht nur ein OEFFNEN nahe `start` zaehlt als Ground-Truth,
        sondern auch ein SCHLIESSEN nahe `stop`. Sonst entsteht ein
        Doppel-Paar, wenn der T-0288-Replay-Guard das OEFFNEN eines ECHTEN
        Laufs verschluckt hat und nur ein lone SCHLIESSEN (am Lauf-Ende) in
        der DB liegt -- das ±60s-Fenster um `start` sieht es nicht
        (T-0283-Spannen-Lehre auf den DHS-Pfad uebertragen).
        """
        von = start_zeit - timedelta(seconds=DEDUP_FENSTER_SEKUNDEN)
        bis = (stop_zeit or start_zeit) + timedelta(seconds=DEDUP_FENSTER_SEKUNDEN)
        ereignisse = await self._speicher.hole_ventil_ereignisse(
            zone_id, von=von, bis=bis,
        )
        for e in ereignisse:
            if e.ventil_id == "sensor_heuristik":
                continue  # Kandidat, kein Ground-Truth-Block
            if e.aktion == VentilAktion.OEFFNEN:
                if abs(
                    (e.zeitstempel - start_zeit).total_seconds()
                ) <= DEDUP_FENSTER_SEKUNDEN:
                    return True
            elif e.aktion == VentilAktion.SCHLIESSEN and stop_zeit is not None:
                if abs(
                    (e.zeitstempel - stop_zeit).total_seconds()
                ) <= DEDUP_FENSTER_SEKUNDEN:
                    return True
        # T-0555 C: die Punkt-Pruefung oben verfehlt einen Live-Lauf, dessen
        # OEFFNEN und SCHLIESSEN beide weiter als 60 s neben DHS liegen. Ein
        # UEBERLAPPENDER Live-Lauf ist trotzdem derselbe physische Lauf.
        if stop_zeit is not None and await self._finde_ueberlappenden_live_close(
            zone_id, start_zeit, stop_zeit,
        ) is not None:
            return True
        return False

    async def _finde_ueberlappenden_live_close(
        self, zone_id: str, dhs_start: datetime, dhs_stop: datetime,
    ):
        """T-0555 C: das Live-SCHLIESSEN, dessen LAUFINTERVALL den DHS-Lauf
        ueberlappt -- oder None.

        **Warum Intervalle statt Zeitpunkte.** Bis 16.09. suchte der Nachtrag
        ein Live-Pendant ueber ZEITPUNKTE mit +-60 s Toleranz: OEFFNEN nahe
        `dhs_start` oder SCHLIESSEN nahe `dhs_stop`. Realfall bambuswald
        30.08.:

            DHS    13:33:53 - 14:14:49   2458 s
            Live   OEFFNEN 13:37:53  (+240 s, der Loop kroch -- T-0556)
                   SCHLIESSEN 14:47:20 (+1951 s, WS-Luecke 4003 s)

        Beide weit ausserhalb von 60 s, also galt der Lauf als NEU und wurde
        ein zweites Mal geschrieben. Derselbe physische Lauf zaehlte doppelt;
        in der Live-DB stehen fuer den 30.08. bis heute 2458 s zu viel.
        Klasse: [[fehlerpattern_dedup_fenster_unter_eventdauer]] -- "Dedup =
        volle Spanne, nicht Punkt".

        Das Intervall eines Live-Laufs kommt aus seinem SCHLIESSEN:
        `[zeitstempel - dauer, zeitstempel]`, genau wie `_baue_pulse` im ML es
        ableitet. Seit T-0572b ist diese Dauer gedeckelt, das Intervall also
        auch bei einem sehr spaeten Close brauchbar.

        **Strikte Ueberlappung, groesste gewinnt.** Nur echte gemeinsame
        Laufzeit zaehlt, kein Toleranzsaum: ein Pre-Soak, der 13:03 endet, und
        eine Hauptdose ab 13:33 sind zwei Laeufe und duerfen nicht verschmelzen
        (die Falle #7200/#7201, vor der T-0555 warnt). Bei mehreren Kandidaten
        (Cycle-and-Soak) nimmt jeder DHS-Lauf den Puls mit der groessten
        gemeinsamen Zeit.

        Stillgelegte Zeilen (`ignoriert`/`unbekannt`) sind kein Pendant --
        dieselbe Regel wie im T-0556-Filter der Korrektur.
        """
        # Ein Live-Close kann Stunden nach dem echten Ende eintreffen
        # (T-0055-B4). Dieselben 2 h wie die bestehende Pendant-Suche.
        ereignisse = await self._speicher.hole_ventil_ereignisse(
            zone_id,
            von=dhs_start - timedelta(seconds=DEDUP_FENSTER_SEKUNDEN),
            bis=dhs_stop + timedelta(hours=2),
        )
        bester = None
        beste_s = 0.0
        for e in ereignisse:
            if e.aktion != VentilAktion.SCHLIESSEN or e.id is None:
                continue
            if e.ventil_id in ("sensor_heuristik", "gardena_web"):
                continue
            if e.ausloser in NICHT_BELASTBARE_AUSLOSER:
                continue
            live_stop = e.zeitstempel
            live_start = live_stop - timedelta(seconds=e.dauer_sekunden or 0)
            gemeinsam = (
                min(live_stop, dhs_stop) - max(live_start, dhs_start)
            ).total_seconds()
            if gemeinsam > beste_s:
                bester, beste_s = e, gemeinsam
        return bester

    async def _hat_live_oeffnen_im_lauf(
        self, zone_id: str, ventil_id: str,
        dhs_start: datetime, dhs_stop: datetime,
    ) -> bool:
        """T-0555 C: gab es waehrend des DHS-Laufs ein Live-OEFFNEN dieses
        Ventils?

        Trennt den spaet geschriebenen Lauf (30.08., OEFFNEN +240 s) vom
        verwaisten Close (T-0358, OEFFNEN verschluckt). Gesucht wird im
        DHS-Intervall selbst, mit dem ueblichen Dedup-Saum am Anfang.
        """
        ereignisse = await self._speicher.hole_ventil_ereignisse(
            zone_id,
            von=dhs_start - timedelta(seconds=DEDUP_FENSTER_SEKUNDEN),
            bis=dhs_stop,
        )
        return any(
            e.aktion == VentilAktion.OEFFNEN
            and e.ventil_id == ventil_id
            and e.ausloser not in NICHT_BELASTBARE_AUSLOSER
            for e in ereignisse
        )

    async def _korrigiere_schliesszeit_falls_abweichend(
        self,
        zone_id: str,
        dhs_start: datetime,
        dhs_stop: datetime,
        dhs_dauer: int,
        dhs_ausloser: Ausloser,
    ) -> bool:
        """T-0055-B4 Option C: Live-WS-SCHLIESSEN auf DHS-Wert korrigieren.

        Sucht das Live-WS- oder Manuell-OEFFNEN-Pendant zum DHS-Start
        (im DEDUP_FENSTER), findet das zugehoerige SCHLIESSEN via
        `finde_ventil_paar`. Wenn dessen Zeitstempel oder Dauer um mehr
        als KORREKTUR_TOLERANZ_SEKUNDEN von DHS abweicht, UPDATE auf
        DHS-Werte.

        Realfall 29.04.: Bambus 08:42-10:11 (90 min via App), Live-WS
        loggte SCHLIESSEN um 10:41 (= +30 min). DHS sagt 10:11. Korrigiert.

        Returns: True wenn UPDATE durchgefuehrt.
        """
        von = dhs_start - timedelta(seconds=DEDUP_FENSTER_SEKUNDEN)
        bis = dhs_start + timedelta(seconds=DEDUP_FENSTER_SEKUNDEN)
        ereignisse = await self._speicher.hole_ventil_ereignisse(
            zone_id, von=von, bis=bis,
        )
        # OEFFNEN-Pendant: Live-WS (UUID) oder Manuell, nicht Heuristik/DHS.
        oeffnen_pendant = None
        for e in ereignisse:
            if e.aktion != VentilAktion.OEFFNEN:
                continue
            if e.ventil_id in ("sensor_heuristik", "gardena_web"):
                continue
            oeffnen_pendant = e
            break
        if oeffnen_pendant is None or oeffnen_pendant.id is None:
            # T-0555 C: bevor ein "verwaister Close" repariert wird -- gibt es
            # einen Live-Lauf, der den DHS-Lauf UEBERLAPPT? Dann ist das OEFFNEN
            # nicht verschluckt, sondern nur spaet geschrieben (30.08.: +240 s),
            # und der richtige Schritt ist, DIESEN Close zu korrigieren. Ohne
            # diese Pruefung liefe der Fall in die Reparatur unten und legte ein
            # weiteres Paar an -- genau die Falle #7200/#7201.
            ueberlappend = await self._finde_ueberlappenden_live_close(
                zone_id, dhs_start, dhs_stop,
            )
            # **Nur wenn der Lauf ein Live-OEFFNEN HAT.** Ein ueberlappender
            # Close ohne jedes OEFFNEN ist der T-0358-Fall (der Replay-Guard
            # hat das OEFFNEN verschluckt) -- der braucht die Reparatur unten,
            # die das OEFFNEN nachtraegt UND den Ausloeser setzt. Nur zu
            # korrigieren liesse ihn ohne OEFFNEN und mit falschem Ausloeser.
            #
            # Das OEFFNEN wird im DHS-Intervall gesucht, NICHT ueber
            # `zeitstempel - dauer` zurueckgerechnet: seit T-0572b ist die
            # Live-Dauer gedeckelt, `Close - Dauer` liegt dann hinter dem
            # echten OEFFNEN und haette einen spaeten Lauf faelschlich als
            # verwaist eingestuft.
            if ueberlappend is not None and await self._hat_live_oeffnen_im_lauf(
                zone_id, ueberlappend.ventil_id, dhs_start, dhs_stop,
            ):
                return await self._korrigiere_close_auf_dhs(
                    zone_id, ueberlappend, dhs_stop, dhs_dauer,
                )
            # T-0358/T-0368: Kein OEFFNEN-Pendant zum DHS-Start -> moeglich,
            # dass der T-0288-Replay-Guard das ECHTE OEFFNEN verschluckt hat
            # oder ein Restart nur einen partiellen Close-Anker erzeugte. DHS
            # ist der Diskriminator (ein Replay-Phantom stuende NICHT in der
            # DHS-Historie) -> reparieren.
            return await self._repariere_verwaisten_close(
                zone_id, dhs_start, dhs_stop, dhs_dauer, dhs_ausloser,
            )
        # SCHLIESSEN-Pendant suchen: gleiche ventil_id, aktion=SCHLIESSEN,
        # nach OEFFNEN-Zeit. Toleranz weit, weil T-0055-B4-Events bis zu
        # mehrere Stunden verspaetet sein koennen — `finde_ventil_paar`
        # mit ±120s-Fenster reicht hier NICHT.
        # T-0321: ABER nur im EIGENEN Pulsfenster (bis zum naechsten OEFFNEN
        # desselben Ventils). Vorher griff der erstbeste SCHLIESSEN bis +2 h ->
        # bei fehlendem Close wurde der des FOLGE-Pulses uebernommen und zeitlich
        # zurueckgeschoben (Mis-Pairing, der verwaiste Open wanderte vorwaerts).
        suche_bis = dhs_stop + timedelta(hours=2)
        ereignisse_breit = await self._speicher.hole_ventil_ereignisse(
            zone_id, von=oeffnen_pendant.zeitstempel, bis=suche_bis,
        )
        naechstes_oeffnen = min(
            (
                e.zeitstempel for e in ereignisse_breit
                if e.aktion == VentilAktion.OEFFNEN
                and e.ventil_id == oeffnen_pendant.ventil_id
                and e.id is not None and e.id != oeffnen_pendant.id
                and e.zeitstempel > oeffnen_pendant.zeitstempel
            ),
            default=None,
        )
        schliessen = next(
            (
                e for e in ereignisse_breit
                if e.aktion == VentilAktion.SCHLIESSEN
                and e.ventil_id == oeffnen_pendant.ventil_id
                and e.id is not None
                and e.id != oeffnen_pendant.id
                and e.zeitstempel > oeffnen_pendant.zeitstempel
                and (naechstes_oeffnen is None or e.zeitstempel < naechstes_oeffnen)
                # T-0556: eine STILLGELEGTE Zeile ist kein gueltiger Close.
                # `ignoriert`/`unbekannt` heisst "das war kein echter Lauf" --
                # von Wirkungsrate, Bilanz, Budget und ML wird die Zeile
                # uebersprungen, also darf der Nachtrag sie erst recht nicht
                # als Pendant nehmen.
                #
                # Realfall 30.08. 21:36-21:47: die stillgelegte Phantom-Zeile
                # #7200 (22:28:49) liegt zwischen dem OEFFNEN der Hauptdose
                # (22:03:24) und ihrem echten Close (#7158, 23:33:22). Ohne
                # diesen Filter wird sie gegriffen, der Nachtrag will sie auf
                # 23:33:22 heben -- und laeuft in die UNIQUE-Bedingung, weil
                # dort schon der echte Close steht. Der Job brach dann jeden
                # 30-min-Zyklus mit `dhs_backfill.fehler` ab.
                and e.ausloser not in NICHT_BELASTBARE_AUSLOSER
            ),
            None,
        )
        if schliessen is None or schliessen.id is None:
            # T-0321: Safety-Net-Loch. OEFFNEN ist persistiert, aber im eigenen
            # Pulsfenster fehlt der SCHLIESSEN (Live-Close verloren, T-0321
            # Primaerursache). Frueher: nichts -> permanent verwaister Open,
            # STATE bleibt OFFEN. Jetzt: fehlenden Close aus DHS-Ground-Truth
            # NACHTRAGEN -- aber nur wenn der Lauf laut DHS sicher vorbei ist
            # (Karenz), sonst koennte ein noch laufender / nur verspaeteter
            # Live-Close doppelt geschlossen werden. Ein spaeter doch
            # eintreffender Live-Close ist dann ein No-op (Re-Run findet den
            # Close -> UPDATE-Pfad unten).
            if datetime.now() - dhs_stop < timedelta(
                seconds=CLOSE_INSERT_KARENZ_SEKUNDEN
            ):
                return False
            # T-0408: Idempotenz-Guard. Die Pulsfenster-Suche oben endet am
            # naechsten OEFFNEN desselben Ventils. Liegt ein ZWEITES OEFFNEN
            # zwischen Pendant und echtem Close (Realfall hecke 09.07.:
            # Live-WS-OEFFNEN 03:56:09 vs. DHS-Repair-OEFFNEN 03:54:00;
            # magerwiese 11.07.: Doppel-OEFFNEN 62 ms auseinander), ist das
            # Fenster zu kurz -> der echte Close wird NIE gefunden -> dieser
            # Insert feuerte bei JEDEM 30-min-Lauf erneut. Die eigenen Kopien
            # liegen selbst ausserhalb des Pulsfensters, also gab es keinen
            # Fixpunkt (205 magerwiese- + 278 hecke-Kopien).
            # Der Guard prueft fensterUNabhaengig direkt am Ziel-Zeitstempel:
            # existiert dort schon ein SCHLIESSEN desselben Ventils, ist der
            # Close da -- egal ob die Pulsfenster-Logik ihn sehen konnte.
            if await self._existiert_schliessen_nahe(
                zone_id, oeffnen_pendant.ventil_id, dhs_stop,
            ):
                return False
            await self._speicher.speichere_ventil_ereignis(VentilEreignis(
                zeitstempel=dhs_stop,
                zone_id=zone_id,
                ventil_id=oeffnen_pendant.ventil_id,
                aktion=VentilAktion.SCHLIESSEN,
                dauer_sekunden=dhs_dauer,
                ausloser=oeffnen_pendant.ausloser,
            ))
            logger.info(
                "dhs.fehlenden_close_eingefuegt",
                zone_id=zone_id,
                oeffnen_start=oeffnen_pendant.zeitstempel.isoformat(),
                dhs_schliesszeit=dhs_stop.isoformat(),
                dhs_dauer_sekunden=dhs_dauer,
            )
            return True
        return await self._korrigiere_close_auf_dhs(
            zone_id, schliessen, dhs_stop, dhs_dauer,
        )

    async def _korrigiere_close_auf_dhs(
        self,
        zone_id: str,
        schliessen,
        dhs_stop: datetime,
        dhs_dauer: int,
    ) -> bool:
        """Hebt EIN gefundenes Live-SCHLIESSEN auf die DHS-Werte.

        T-0555 C: aus `_korrigiere_schliesszeit_falls_abweichend` extrahiert,
        damit der neue Ueberlappungs-Pfad DIESELBE Korrektur nutzt --
        Toleranzpruefung, UNIQUE-Waechter (T-0562) und UPDATE -- statt einer
        zweiten Kopie, die frueher oder spaeter auseinanderlaeuft.
        Verhaltensneutral fuer den bestehenden Pfad.
        """
        schliessen_id = schliessen.id
        # Vergleich: Zeitstempel und Dauer
        delta_zeit_s = abs(
            (schliessen.zeitstempel - dhs_stop).total_seconds()
        )
        delta_dauer_s = abs(schliessen.dauer_sekunden - dhs_dauer)
        if (
            delta_zeit_s < KORREKTUR_TOLERANZ_SEKUNDEN
            and delta_dauer_s < KORREKTUR_TOLERANZ_SEKUNDEN
        ):
            return False  # Live-WS und DHS sind nahe genug — kein UPDATE
        # T-0419: der Korrektur-Pfad ist erreicht und die Abweichung ist
        # gross. Das explizit loggen, BEVOR das UPDATE laeuft -- damit sich
        # spaeter unterscheiden laesst, ob ein unkorrigierter Close hier
        # ankam (dann liegt der Fehler im UPDATE) oder gar nicht erst
        # (dann liefert DHS den Lauf nicht, oder das Pendant fehlt).
        # Realfall hecke 21.07.: watchdog schrieb 5400 s statt 3720 s und
        # blieb stehen -- ohne dieses Log war nicht feststellbar, an welcher
        # Station der Kette er verlorenging.
        logger.info(
            "dhs.korrektur_faellig",
            zone_id=zone_id,
            schliessen_id=schliessen_id,
            db_dauer_s=schliessen.dauer_sekunden,
            dhs_dauer_s=dhs_dauer,
            delta_zeit_s=round(delta_zeit_s),
            delta_dauer_s=round(delta_dauer_s),
        )
        # T-0562: derselbe Guard wie im INSERT-Pfad (T-0408). Er hing bis
        # hier an genau EINEM von drei Schreibern. Steht an `dhs_stop`
        # bereits ein SCHLIESSEN desselben Ventils, laeuft das UPDATE in
        # den UNIQUE-Index (zone_id, ventil_id, aktion, zeitstempel) --
        # und weil DHS dieselbe Historie alle 30 min erneut liefert, war
        # der Konflikt deterministisch und heilte nie von selbst.
        #
        # Zwei Folgen hatte das, die zweite ist die teurere: der Lauf brach
        # ab UND `_letzte_aktualisierung` blieb ungesetzt, das 30-min-Gate
        # griff also nicht mehr. Der Job feuerte dann bei JEDEM
        # Entscheidungs-Tick (300 s) einen neuen GET gegen
        # smart.gardena.com -- der bekannte Soft-Ban-Vektor
        # ([[fehlerpattern_husqvarna_softban]]).
        if await self._existiert_schliessen_nahe(
            zone_id, schliessen.ventil_id, dhs_stop, ausser_id=schliessen_id,
        ):
            logger.info(
                "dhs.korrektur_uebersprungen_pendant_vorhanden",
                zone_id=zone_id, schliessen_id=schliessen_id,
                dhs_stop=dhs_stop.isoformat(),
            )
            return False
        # UPDATE auf DHS-Werte
        await self._speicher.aktualisiere_ventil_ereignis(
            schliessen_id,
            zeitstempel=dhs_stop,
            dauer_sekunden=dhs_dauer,
        )
        return True

    async def _existiert_schliessen_nahe(
        self,
        zone_id: str,
        ventil_id: str,
        ziel: datetime,
        ausser_id: int | None = None,
    ) -> bool:
        """T-0408: True wenn fuer `ventil_id` bereits ein SCHLIESSEN im
        DEDUP_FENSTER um `ziel` liegt.

        Bewusst UNABHAENGIG vom T-0321-Pulsfenster: genau dessen Blindheit
        (zweites OEFFNEN verkuerzt das Suchfenster) erzeugte den
        Duplikat-Sturm. Der Guard fragt nicht "gehoert der Close zu diesem
        Puls?", sondern "steht an dieser Stelle schon einer?" -- und ist
        damit auch gegen die eigenen Alt-Kopien ein Fixpunkt.
        """
        von = ziel - timedelta(seconds=DEDUP_FENSTER_SEKUNDEN)
        bis = ziel + timedelta(seconds=DEDUP_FENSTER_SEKUNDEN)
        ereignisse = await self._speicher.hole_ventil_ereignisse(
            zone_id, von=von, bis=bis,
        )
        # T-0562: `ausser_id` schliesst die Zeile aus, die gerade VERSCHOBEN
        # wird. Fuer den INSERT-Pfad (T-0408) war das egal -- dort entsteht
        # eine neue Zeile. Die beiden UPDATE-Pfade heben dagegen einen
        # existierenden Close auf `ziel`, und der liegt manchmal selbst im
        # Dedup-Fenster: der Guard haette sich dann selbst blockiert.
        # Aufgefallen an `test_t0358...` (Close 11:26:05, DHS-Ziel 11:26:03,
        # zwei Sekunden Abstand) -- der Bestandstest hat den Fehler in
        # meinem eigenen Fix gefangen.
        return any(
            e.aktion == VentilAktion.SCHLIESSEN and e.ventil_id == ventil_id
            and (ausser_id is None or e.id != ausser_id)
            for e in ereignisse
        )

    async def _repariere_verwaisten_close(
        self,
        zone_id: str,
        dhs_start: datetime,
        dhs_stop: datetime,
        dhs_dauer: int,
        dhs_ausloser: Ausloser,
    ) -> bool:
        """T-0358: Repariert einen echten Lauf, dessen OEFFNEN der T-0288-Replay-
        Guard verschluckt hat.

        Symptom (Realfall Bambus 01.07. 04:56-05:40): ein 990s-WS-Gap armierte
        den Replay-Guard genau als der Gardena-ZEITPLAN oeffnete; das echte
        SCHEDULED_WATERING-OEFFNEN traf im Guard-Fenster ein und wurde als
        vermeintlicher Replay unterdrueckt (State auf OFFEN gesetzt, aber KEIN
        `_ventil_offen_seit`-Anker) -> das reale SCHLIESSEN am Lauf-Ende bekam
        `dauer_s=0` und blieb ohne OEFFNEN. Folge: Historie zeigt 0s statt 44min,
        Wasserbilanz unterschaetzt (~62L). DHS ist der Diskriminator -- ein
        Replay-Phantom stuende NICHT in der Cloud-Historie, ein echter Lauf schon.

        Reparatur: fehlendes OEFFNEN am DHS-Start nachtragen (gepaart mit dem
        verwaisten Close: gleiche ventil_id) UND das SCHLIESSEN auf
        DHS-Zeit/-Dauer/-Ausloeser korrigieren. Idempotent: nach der Reparatur existiert
        das OEFFNEN-Pendant -> der naechste Backfill nimmt den Normalpfad.
        """
        von = dhs_stop - timedelta(seconds=DEDUP_FENSTER_SEKUNDEN)
        bis = dhs_stop + timedelta(seconds=DEDUP_FENSTER_SEKUNDEN)
        ereignisse = await self._speicher.hole_ventil_ereignisse(
            zone_id, von=von, bis=bis,
        )
        # Verwaister Close = echtes (nicht Heuristik/DHS) SCHLIESSEN nahe dem
        # DHS-Stop. `dauer=0` war die erste Signatur; nach Restart mitten im
        # Lauf kann der WS-Close aber eine partielle Dauer > 0 tragen.
        #
        # T-0556: eine STILLGELEGTE Zeile ist kein verwaister Close, sondern
        # eine, die absichtlich nicht mehr zaehlt -- ihr ein OEFFNEN zu
        # spendieren macht sie nicht gueltiger, erzeugt aber Zeilen. Realfall
        # 30.08. 21:52: #7174/#7175 waren als Duplikat stillgelegt und bekamen
        # trotzdem ein OEFFNEN (#7222/#7223). Folgenlos fuers ML, weil OEFFNEN
        # dort nicht gelesen wird -- aber es ist dieselbe Regel wie bei der
        # Pendant-Suche, und eine halb angewandte Regel ist genau der Anfang,
        # aus dem der 30.08. entstanden ist.
        verwaister_close = next(
            (
                e for e in ereignisse
                if e.aktion == VentilAktion.SCHLIESSEN
                and e.ventil_id not in ("sensor_heuristik", "gardena_web")
                and e.id is not None
                and e.ausloser not in NICHT_BELASTBARE_AUSLOSER
            ),
            None,
        )
        if verwaister_close is None:
            return False
        # T-0562, Isomorphie: dritter Schreiber, gleiche Klasse. Auch hier
        # wird ein SCHLIESSEN auf `dhs_stop` gehoben; steht dort schon eines
        # desselben Ventils, laeuft das UPDATE in den UNIQUE-Index. Der
        # `transaktion()`-Block rollt dann auch das frisch geschriebene
        # OEFFNEN zurueck -- der Reparaturversuch wiederholt sich damit bei
        # jedem Lauf ergebnislos.
        if await self._existiert_schliessen_nahe(
            zone_id, verwaister_close.ventil_id, dhs_stop,
            ausser_id=verwaister_close.id,
        ):
            logger.info(
                "dhs.reparatur_uebersprungen_pendant_vorhanden",
                zone_id=zone_id,
                close_id=verwaister_close.id,
                dhs_stop=dhs_stop.isoformat(),
            )
            return False
        async with self._speicher.transaktion():
            await self._speicher.speichere_ventil_ereignis(VentilEreignis(
                zeitstempel=dhs_start,
                zone_id=zone_id,
                ventil_id=verwaister_close.ventil_id,
                aktion=VentilAktion.OEFFNEN,
                dauer_sekunden=0,
                ausloser=dhs_ausloser,
            ))
            await self._speicher.aktualisiere_ventil_ereignis(
                verwaister_close.id,
                ausloser=dhs_ausloser,
                zeitstempel=dhs_stop,
                dauer_sekunden=dhs_dauer,
            )
        logger.info(
            "dhs.verwaisten_close_repariert",
            zone_id=zone_id,
            dhs_start=dhs_start.isoformat(),
            dhs_stop=dhs_stop.isoformat(),
            dhs_dauer_sekunden=dhs_dauer,
            close_id=verwaister_close.id,
        )
        return True

    async def _loesche_heuristik_im_fenster(
        self, zone_id: str, start_zeit: datetime, stop_zeit: datetime,
    ) -> int:
        """Entfernt sensor_heuristik-Events im Fenster [start-60s, stop+60s].

        Returns: Anzahl geloeschter Events — fuer Logging im Aufrufer.
        """
        von = start_zeit - timedelta(seconds=DEDUP_FENSTER_SEKUNDEN)
        bis = stop_zeit + timedelta(seconds=DEDUP_FENSTER_SEKUNDEN)
        # bis-Parameter spart serverseitig den Scan ueber juengere Events.
        ereignisse = await self._speicher.hole_ventil_ereignisse(
            zone_id, von=von, bis=bis,
        )
        geloescht = 0
        for e in ereignisse:
            if e.ventil_id == "sensor_heuristik" and e.id is not None:
                await self._speicher.loesche_ventil_ereignis(e.id)
                geloescht += 1
        return geloescht


# --- Helfer ---

def _parse_iso(s: str) -> datetime:
    """Parse DHS-ISO-Zeitstempel (`2026-04-18T09:25:07Z`) zu timezone-naiven Lokal-datetime.

    DHS liefert UTC ('Z'). Wir normalisieren auf die lokale Zeitzone und
    machen naive (kein tzinfo) — weil der Rest des Systems naive-lokale
    Zeitstempel nutzt (siehe TZ-Fix in T-0054 + main.py Logging).

    **T-0566: in der Wiederholungsstunde der Zeitumstellung ist das Ergebnis
    NICHT eindeutig.** Am 25.10.2026 gibt es 02:00-03:00 lokal zweimal; zwei
    reale UTC-Zeitpunkte eine Stunde auseinander ergeben denselben naiven
    Stempel, und `INSERT OR IGNORE` verwirft den zweiten still. Ausgefuehrt
    reproduziert: `00:30Z` und `01:30Z` liefern beide `2026-10-25 02:30:00`.

    Mit naiv-lokaler Speicherung ist das nicht loesbar -- die Spalte kann
    die beiden Zeitpunkte gar nicht unterscheiden. Was hier geht und jetzt
    passiert: die Mehrdeutigkeit wird ERKANNT und geloggt, damit ein
    verlorener Datenpunkt nicht als "war nichts" durchgeht. Die saubere
    Loesung waere aware-UTC in der DB samt Migration; siehe T-0566 in
    TASK.md.
    """
    # '2026-04-18T09:25:07Z' → datetime in UTC → lokale Zeit (naive)
    dt_utc = datetime.fromisoformat(s.replace("Z", "+00:00"))
    lokal = dt_utc.astimezone()
    if ist_mehrdeutige_lokalzeit(lokal):
        logger.warning(
            "dhs.zeitstempel_mehrdeutig",
            roh=s,
            lokal=lokal.replace(tzinfo=None).isoformat(),
            hinweis="Wiederholungsstunde der Zeitumstellung -- zwei reale "
                    "Zeitpunkte teilen diesen Stempel, der zweite kann "
                    "still verworfen werden",
        )
    return lokal.replace(tzinfo=None)


def _ist_ausgefuehrt(attr: dict) -> bool:
    """True wenn das DHS-Event eine echte Bewaesserung war.

    **Whitelist-Ansatz** (nach Befund 21.04.2026): Nur Events mit
    `summary.startswith("EXECUTED_")` UND gesetztem, positivem `duration`
    gelten als echt. Alles andere (SKIPPED_*, VALVE_ERROR, decision=NOOP/
    SKIP/CANCELED, MOTOR_ERROR, TIMEOUT) wird verworfen.

    **Warum Whitelist statt Blacklist**: Am 21.04.2026 hat die Gardena-App
    geplante Schedules wegen Frostgefahr uebersprungen. DHS listete sie als
    `summary=VALVE_ERROR, decision=NOOP, duration=null` — der vorherige
    Blacklist-Filter liess sie durch, und der Parser-Fallback
    `stop-start` erfand 300s/1200s als Dauer → Phantom-Bewaesserungen
    in der DB. Whitelist ist konservativer und faengt auch kuenftige
    Error-Codes ab, die wir noch nicht gesehen haben.
    """
    summary = (attr.get("summary") or "").upper()
    if not summary.startswith("EXECUTED_"):
        return False
    duration = attr.get("duration")
    if duration is None:
        return False
    try:
        if int(duration) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    return True


# T-0556: Auslöser, die KEIN Nachweis eines gelaufenen Vorgangs sind.
# `UNBEKANNT` ist eine unbestaetigte Heuristik-Vermutung, `IGNORIERT` eine
# verworfene oder stillgelegte Zeile. Beide werden von Wirkungsrate, Bilanz,
# Budget und ML uebersprungen (siehe `Ausloser`-Enum) -- der Nachtrag muss
# dieselbe Grenze ziehen.
NICHT_BELASTBARE_AUSLOSER = frozenset({Ausloser.UNBEKANNT, Ausloser.IGNORIERT})


def _summary_zu_ausloser(summary: str, action: str) -> Ausloser:
    """Mappt DHS-summary/action auf unser Ausloser-Enum.

    Beobachtete Werte am 19.04.2026:
    - `EXECUTED_MANUAL` / `MANUAL_START_MANUAL_STOP` (action=MANUAL) → MANUELL
    - `EXECUTED_SCHEDULE` (action=SINGLE, decision=NOOP) → ZEITPLAN
    Unbekannte Werte fallen auf AUTOMATIK zurueck.

    T-0455 (29.07.2026): `EXECUTED_SCHEDULE` ergab frueher AUTOMATIK, waehrend
    der WS-Pfad DASSELBE Ereignis als MANUELL schrieb -- der Dedup-Sieger
    entschied damit die Semantik. Beide Pfade schreiben jetzt ZEITPLAN.
    Der Rest-Fallback bleibt bewusst AUTOMATIK: er greift nur bei
    UNBEKANNTEN summary-Werten, und dort waere `zeitplan` eine Behauptung
    ueber die Ursache, die die Daten nicht hergeben.
    """
    s = (summary or "").upper()
    a = (action or "").upper()
    if "MANUAL" in s or a == "MANUAL":
        return Ausloser.MANUELL
    if "SCHEDULE" in s:
        return Ausloser.ZEITPLAN
    return Ausloser.AUTOMATIK



def baue_kanal_zu_zonen(zonen: Iterable[ZonenKonfig]) -> dict[int, list[str]]:
    """Hilfsfunktion fuer main.py: macht aus ZonenKonfig-Liste das Mapping.

    T-0203 (2026-05-17): Bei Multi-DSWC-Setups ist `ventil_kanal` allein
    mehrdeutig (DSWC 1 Kanal 1 = waldblumenhain, DSWC 2 Kanal 1 = magerwiese).
    Diese Funktion bleibt fuer Backward-Compat (Single-DSWC ohne
    `ventil_geraet_id`). Multi-DSWC-aware Aufrufer sollen
    `baue_kanal_zu_zonen_pro_geraet()` nutzen.
    """
    out: dict[int, list[str]] = {}
    for z in zonen:
        if z.ventil_kanal is None:
            continue
        out.setdefault(z.ventil_kanal, []).append(z.zone_id)
    return out


def baue_kanal_zu_zonen_pro_geraet(
    zonen: Iterable[ZonenKonfig], primary_geraet_id: str | None = None,
) -> dict[str, dict[int, list[str]]]:
    """T-0203: Multi-DSWC-Mapping `geraet_id -> kanal -> [zone_id]`.

    Zonen ohne `ventil_geraet_id` werden dem `primary_geraet_id`
    zugeordnet (= erste entdeckte DSWC, Backward-Compat). Wenn auch
    das None ist, werden sie ignoriert.
    """
    out: dict[str, dict[int, list[str]]] = {}
    for z in zonen:
        if z.ventil_kanal is None:
            continue
        gid = z.ventil_geraet_id or primary_geraet_id
        if gid is None:
            continue
        out.setdefault(gid, {}).setdefault(z.ventil_kanal, []).append(z.zone_id)
    return out


def baue_zone_dswc_kanal_map(
    zonen: Iterable[ZonenKonfig],
    primary_geraet_id: str | None = None,
) -> dict[tuple[str | None, int], list[str]]:
    """T-0252: Mapping `(geraet_id, kanal) -> [zone_id]` fuer die
    Geschwister-Expansion von Live-Ventil-Events in `main.py`.

    DSWC-Disambiguierung ist Pflicht: zwei Zonen am Kanal 1
    verschiedener DSWCs duerfen nicht aufeinander expandiert werden.
    Realfall vom 24.05.2026: ein manueller Lauf auf DSWC 1 Kanal 1
    (waldblumenhain) erzeugte Phantom-OEFFNEN/SCHLIESSEN-Events fuer
    magerwiese (DSWC 2 Kanal 1), weil die alte Map nur nach Kanal-
    Nummer schluesselte.

    Zonen ohne `ventil_geraet_id` fallen auf `primary_geraet_id`
    zurueck (Backward-Compat fuer Single-DSWC-Setups). Zonen ohne
    `ventil_kanal` (Monitoring-only) werden ignoriert; sie haben
    keinen Bewaesserungskreis zum Expandieren.
    """
    out: dict[tuple[str | None, int], list[str]] = {}
    for z in zonen:
        if z.ventil_kanal is None:
            continue
        gid = z.ventil_geraet_id or primary_geraet_id
        out.setdefault((gid, z.ventil_kanal), []).append(z.zone_id)
    return out
