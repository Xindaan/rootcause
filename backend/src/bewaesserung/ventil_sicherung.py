"""Safety-Wrapper fuer Ventilsteuerung mit Watchdog-Timer.

Sitzt zwischen Entscheidungsmotor und GardenaClient:
- Max-Duration-Timer als Sicherheitsnetz
- Event-Recording (OEFFNEN/SCHLIESSEN) mit Ausloser-Tracking
- Notfall-Stopp
- Startup-Recovery (offene Ventile nach Crash erkennen)
- Callback-Integration: synchronisiert _aktiv mit echtem Ventilzustand
- T-0151: Hahn-Cluster-Lock (Pre-flight Durchfluss-Budget-Pruefung)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

from bewaesserung.gardena_client import GardenaClient
from bewaesserung.modelle import Ausloser, VentilAktion, VentilEreignis
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()


@dataclass(frozen=True)
class HahnLockEntscheidung:
    """T-0151: Ergebnis der Cluster-Budget-Pruefung.

    `erlaubt=True` -> Ventil darf oeffnen.
    `erlaubt=False` -> `grund` enthaelt Klartext-Begruendung,
    `aktive_zonen` listet die Zonen, deren Verbrauch das Budget ausschoepft.
    """
    erlaubt: bool
    grund: str = ""
    aktive_zonen: tuple[str, ...] = ()
    verbrauch_aktuell_lpm: float = 0.0
    verbrauch_neu_lpm: float = 0.0
    budget_lpm: float = 0.0

# Nach so vielen fehlgeschlagenen Close-Versuchen wird kein weiterer Retry-Timer
# mehr gesetzt. _aktiv bleibt (ventil-status meldet weiter als offen) damit der
# Nutzer via notfall_stopp oder manuell eingreift.
MAX_RETRY_VERSUCHE = 3

# T-0287: Frische-Gate. Ist die neueste Gardena-Messung aelter als das,
# gilt der Live-Zustand als blind (Laptop-Schlaf, WS-Verlust) und der
# Automatik-Pfad schaltet NICHT (kein Giessen auf veraltetem Zustand).
# main.py reicht das an VentilSicherung durch; im Konstruktor Default
# None = Gate aus (schuetzt Unit-Tests, greift nur scharf im Auto-Loop).
AUTOMATIK_MAX_DATEN_ALTER_MINUTEN = 120

# T-0361: Startup-Reconciliation wartet pro Valve auf den ersten echten
# WS-Initialstatus. Timeout bedeutet "unbekannt", nicht "zu".
STARTUP_VALVE_STATUS_TIMEOUT_S = 45.0


class AktiveBewaesserung:
    """Zustand einer laufenden Bewaesserung."""

    def __init__(self, kanal: int, geraet_id: str, zone_ids: list[str],
                 dauer_s: int, ausloser: Ausloser,
                 valve_id: str | None = None,
                 lauf_gruppe: str | None = None,
                 phase: str | None = None):
        self.kanal = kanal
        self.geraet_id = geraet_id
        # T-0335: Pre-Soak-Marker, getragen von OEFFNEN aufs SCHLIESSEN, damit
        # beide Events eines Pulses dieselbe Lauf-Gruppe/Phase tragen. None bei
        # Einzellaeufen.
        self.lauf_gruppe = lauf_gruppe
        self.phase = phase
        # `event_ventil_id` wird in OEFFNEN/SCHLIESSEN-Events geschrieben.
        # Bei SmartIrrigationControl: valve_id (matcht WS-Events). Bei
        # WaterControl: geraet_id (= valve_id de facto, da single-valve).
        self.valve_id = valve_id
        self.event_ventil_id = valve_id or geraet_id
        self.zone_ids = zone_ids
        self.dauer_s = dauer_s
        self.ausloser = ausloser
        self.gestartet = datetime.now()
        self.timer_handle: asyncio.TimerHandle | None = None
        self.retry_versuche: int = 0


class VentilSicherung:
    """Safety-Wrapper fuer Ventilsteuerung."""

    def __init__(
        self,
        client: GardenaClient,
        speicher: Speicher,
        ventil_geraet_id: str,
        puffer_sekunden: int = 30,
        kanal_zu_valve_id: dict[int, str] | None = None,
        # T-0151 + T-0153: Hahn-Cluster + Verbrauch + Exklusivitaet.
        # `kanal_zu_zone_lockprofile`: pro Kanal die Liste (zone_id, cluster_id,
        # verbrauch_lpm, exklusiv) der zugeordneten Zonen. Bei mehreren Zonen
        # am gleichen Kanal (Bambus-Kette) wird der maximale verbrauch_lpm
        # genommen (alle teilen denselben Lauf), und `exklusiv` ist True
        # wenn IRGENDEINE Kanal-Zone exklusiv ist.
        # `cluster_max_lpm`: cluster_id -> max_durchfluss_lpm.
        kanal_zu_zone_lockprofile: dict[
            int, list[tuple[str, str | None, float | None, bool]]
        ] | None = None,
        cluster_max_lpm: dict[str, float] | None = None,
        # T-0287: max. Alter der neuesten Gardena-Messung, ab dem der
        # Automatik-Pfad als blind gilt und NICHT schaltet. None = Gate aus.
        max_daten_alter_minuten: int | None = None,
    ):
        self._client = client
        self._speicher = speicher
        self._ventil_geraet_id = ventil_geraet_id
        self._puffer_s = puffer_sekunden
        self._max_daten_alter: timedelta | None = (
            timedelta(minutes=max_daten_alter_minuten)
            if max_daten_alter_minuten is not None
            else None
        )
        self._aktiv: dict[int, AktiveBewaesserung] = {}  # kanal -> AktiveBewaesserung
        # Unterdrueckt naechsten GardenaClient-Callback (nach stoppe()-Aufruf)
        self._unterdruecke_callback: set[str] = set()
        # T-0299: Pro-Kanal-Lock serialisiert das Schliessen (stoppe/Watchdog/
        # notfall_stopp). Verhindert doppelte SCHLIESSEN-Writes + Cloud-Stops
        # bei parallelen Aufrufern (check-then-await-then-pop ohne Guard).
        self._stop_locks: dict[int, asyncio.Lock] = {}
        # Pflicht bei SmartIrrigationControl (multi-valve), leer bei WaterControl.
        # main.py befuellt das via GardenaClient.baue_kanal_zu_valve_id().
        self._kanal_zu_valve_id: dict[int, str] = dict(kanal_zu_valve_id or {})
        # T-0151 + T-0153: Hahn-Cluster-Lock-Profile.
        self._kanal_zu_lockprofile: dict[
            int, list[tuple[str, str | None, float | None, bool]]
        ] = dict(kanal_zu_zone_lockprofile or {})
        self._cluster_max_lpm: dict[str, float] = dict(cluster_max_lpm or {})

    def setze_kanal_mapping(self, kanal_zu_valve_id: dict[int, str]) -> None:
        """Aktualisiert das Kanal->Valve-ID-Mapping (z. B. nach Reconnect)."""
        self._kanal_zu_valve_id = dict(kanal_zu_valve_id)

    def _event_gehoert_zu_sicherung(self, ventil_id: str) -> bool:
        """True wenn ein Gardena-Callback zu dieser DSWC-Sicherung gehoert."""
        if ventil_id == self._ventil_geraet_id:
            return True
        if ventil_id in set(self._kanal_zu_valve_id.values()):
            return True
        return any(a.event_ventil_id == ventil_id for a in self._aktiv.values())

    def _kanal_lockinfo(
        self, kanal: int,
    ) -> tuple[str | None, float | None, bool]:
        """T-0151 + T-0153: Cluster + max-Verbrauch + Exklusivitaet des Kanals.

        Mehrere Zonen am gleichen Kanal teilen einen Lauf (Bambus-Kette),
        also der hoechste verbrauch_lpm zaehlt. Cluster muss eindeutig
        sein (alle Zonen am Kanal im selben Cluster). `exklusiv=True`,
        wenn IRGENDEINE Kanal-Zone exklusiv markiert ist.

        Returns (cluster_id_oder_None, verbrauch_lpm_oder_None, exklusiv).
        Wenn kein Lockprofile fuer diesen Kanal gepflegt ist oder
        verbrauch_lpm fehlt -> (None, None, False) -> Lock-Check
        wird uebersprungen (Backward-Compat).
        """
        profile = self._kanal_zu_lockprofile.get(kanal, [])
        cluster: str | None = None
        verbrauch: float | None = None
        exklusiv = False
        for _zid, c, v, e in profile:
            if c is not None:
                cluster = c  # letzter Wert reicht (im Konsistenzfall identisch)
            if v is not None:
                verbrauch = v if verbrauch is None else max(verbrauch, v)
            if e:
                exklusiv = True
        return cluster, verbrauch, exklusiv

    def pruefe_hahn_cluster_budget(
        self, kanal: int,
    ) -> HahnLockEntscheidung:
        """T-0151 + T-0153: Pre-flight Pruefung ob Kanal jetzt starten darf.

        Reihenfolge der Checks:
        1. **Backward-Compat**: kein Cluster oder kein verbrauch_lpm
           konfiguriert -> immer erlaubt.
        2. **T-0153 Exklusivitaet**: wenn die zu startende Zone exklusiv
           ist und IRGENDETWAS im Cluster laeuft, oder eine aktive
           Cluster-Zone exklusiv ist -> blockt unabhaengig vom Volumen.
           Modelliert Druck-Konflikt (Sprinkler-Topologie kollabiert).
        3. **T-0151 Volumen**: Summe aller aktiven Cluster-Verbraeuche +
           neuer Verbrauch <= max_durchfluss_lpm.
        """
        cluster, neu_verbrauch, neu_exklusiv = self._kanal_lockinfo(kanal)
        if cluster is None or neu_verbrauch is None:
            return HahnLockEntscheidung(erlaubt=True)
        budget = self._cluster_max_lpm.get(cluster)
        if budget is None:
            return HahnLockEntscheidung(erlaubt=True)
        # Aktiver Verbrauch summiert ueber alle laufenden Kanaele im selben
        # Cluster (Kanal mit Mehrzonen-Profilen wird einmal gezaehlt -> max).
        aktiver_verbrauch = 0.0
        aktive_zonen: list[str] = []
        aktiver_exklusiv = False
        for laufender_kanal, aktiv in self._aktiv.items():
            if laufender_kanal == kanal:
                continue  # eigener Kanal — wird von bewaessere() bereits abgewiesen
            l_cluster, l_verbrauch, l_exklusiv = self._kanal_lockinfo(laufender_kanal)
            if l_cluster != cluster or l_verbrauch is None:
                continue
            aktiver_verbrauch += l_verbrauch
            aktive_zonen.extend(aktiv.zone_ids)
            if l_exklusiv:
                aktiver_exklusiv = True
        # T-0153: Exklusivitaets-Check VOR Volumen-Check (kann auch durchgehen
        # wenn Volumen reichen wuerde — Druck-Konflikt ist haerter).
        if aktive_zonen and (neu_exklusiv or aktiver_exklusiv):
            grund_typ = "neue Zone exklusiv" if neu_exklusiv else "aktive Zone exklusiv"
            return HahnLockEntscheidung(
                erlaubt=False,
                grund=(
                    f"hahn_cluster '{cluster}' exklusiv blockiert "
                    f"({grund_typ}): aktive Zonen {list(aktive_zonen)} "
                    f"verhindern Mitstart einer druckabhaengigen Zone."
                ),
                aktive_zonen=tuple(aktive_zonen),
                verbrauch_aktuell_lpm=aktiver_verbrauch,
                verbrauch_neu_lpm=neu_verbrauch,
                budget_lpm=budget,
            )
        gesamt = aktiver_verbrauch + neu_verbrauch
        if gesamt > budget + 1e-9:  # Float-Toleranz
            return HahnLockEntscheidung(
                erlaubt=False,
                grund=(
                    f"hahn_cluster '{cluster}' belegt: aktuell {aktiver_verbrauch:.2f} L/min, "
                    f"neu +{neu_verbrauch:.2f} L/min = {gesamt:.2f} > "
                    f"Budget {budget:.2f} L/min."
                ),
                aktive_zonen=tuple(aktive_zonen),
                verbrauch_aktuell_lpm=aktiver_verbrauch,
                verbrauch_neu_lpm=neu_verbrauch,
                budget_lpm=budget,
            )
        return HahnLockEntscheidung(
            erlaubt=True,
            verbrauch_aktuell_lpm=aktiver_verbrauch,
            verbrauch_neu_lpm=neu_verbrauch,
            budget_lpm=budget,
        )

    async def recover_aus_db(self) -> int:
        """T-0115: Repopuliert _aktiv aus dem persistierten Live-Lauf-State.

        Nach Backend-Restart kennt VentilSicherung den `_aktiv`-Zustand
        nicht mehr — der Cloud-Override schliesst das Ventil zwar regulaer,
        aber UI haette nur "extern"-Anzeige (kein Countdown), und das
        SCHLIESSEN-Event wuerde nicht via stoppe()/Watchdog erzeugt sondern
        nur ueber die WS-Pipeline.

        Diese Methode laedt jeden persistierten State und rekonstruiert
        AktiveBewaesserung + Watchdog-Timer mit verbleibender Dauer. Wenn
        die geplante Endzeit bereits in der Vergangenheit liegt
        (Cloud-Timer hat das Ventil sicher schon geschlossen), wird der
        State entsorgt + ein synthetisches SCHLIESSEN nicht geschrieben
        (das macht die WS-Pipeline beim naechsten Update).

        Returns: Anzahl rekonstruierter Bewaesserungen.
        """
        states = await self._speicher.hole_live_lauf_states(self._ventil_geraet_id)
        anzahl = 0
        for s in states:
            kanal = s["kanal"]
            if kanal in self._aktiv:
                continue  # bereits durch anderen Recovery-Pfad gesetzt
            verbleibend = s["dauer_sekunden"] - int(
                (datetime.now() - s["gestartet_am"]).total_seconds()
            )
            if verbleibend <= 0:
                logger.info(
                    "ventil.recover_skip_abgelaufen",
                    kanal=kanal, gestartet_am=s["gestartet_am"].isoformat(),
                    dauer_sekunden=s["dauer_sekunden"],
                )
                await self._speicher.loesche_live_lauf_state(
                    kanal, self._ventil_geraet_id,
                )
                continue
            try:
                ausloser = Ausloser(s["ausloser"])
            except ValueError:
                ausloser = Ausloser.MANUELL
            aktiv = AktiveBewaesserung(
                kanal=kanal,
                geraet_id=s["geraet_id"],
                zone_ids=s["zone_ids"],
                dauer_s=s["dauer_sekunden"],
                ausloser=ausloser,
                valve_id=s["valve_id"],
            )
            aktiv.gestartet = s["gestartet_am"]
            # Watchdog mit Restdauer + Puffer setzen
            loop = asyncio.get_event_loop()
            aktiv.timer_handle = loop.call_later(
                verbleibend + self._puffer_s,
                lambda k=kanal: asyncio.ensure_future(
                    self._watchdog_schliessen(k)
                ),
            )
            self._aktiv[kanal] = aktiv
            anzahl += 1
            logger.info(
                "ventil.recover_aktiv",
                kanal=kanal, valve_id=aktiv.valve_id,
                verbleibend_s=verbleibend,
                ausloser=ausloser.value,
            )
        return anzahl

    async def bewaessere(
        self, kanal: int, zone_ids: list[str],
        dauer_s: int, ausloser: Ausloser,
        *, lauf_gruppe: str | None = None, phase: str | None = None,
    ) -> bool:
        """Oeffnet Ventil mit Safety-Checks. Gibt True zurueck wenn gestartet.

        T-0335: `lauf_gruppe`/`phase` markieren Pre-Soak-Phasen (vom Pre-Soak-
        Manager gesetzt). Sie werden in AktiveBewaesserung gehalten und sowohl
        ins OEFFNEN- als auch ins spaetere SCHLIESSEN-Event geschrieben ->
        Giess-Historie gruppiert Puls + Haupt als EINEN Lauf. None = Einzellauf.
        """
        if kanal in self._aktiv:
            logger.warning(
                "ventil.bereits_aktiv",
                kanal=kanal,
                seit=self._aktiv[kanal].gestartet.isoformat(),
            )
            return False

        if dauer_s <= 0:
            logger.warning("ventil.ungueltige_dauer", dauer=dauer_s)
            return False

        # T-0287: Frische-Gate (nur Automatik). Auf veraltetem Live-Zustand
        # (Laptop-Schlaf, WS-Verlust) NICHT giessen -- ein blindes Fenster
        # darf keine Fehlentscheidung ausloesen. Manuell/User-getriggert ist
        # bewusst ausgenommen; der Stop-Pfad (stoppe) bleibt immer erlaubt.
        if ausloser == Ausloser.AUTOMATIK and self._max_daten_alter is not None:
            blind, alter_min = await self._automatik_daten_blind(datetime.now())
            if blind:
                logger.warning(
                    "ventil.automatik_blind_blockiert",
                    kanal=kanal,
                    daten_alter_min=(
                        round(alter_min, 1) if alter_min is not None else None
                    ),
                    max_alter_min=int(self._max_daten_alter.total_seconds() // 60),
                )
                return False

        # T-0151: Hahn-Cluster-Budget pruefen (Backward-Compat: ohne Konfig
        # immer erlaubt). Hier nur loggen — die API-Schicht entscheidet
        # ueber Reject mit klarem Grund.
        lock = self.pruefe_hahn_cluster_budget(kanal)
        if not lock.erlaubt:
            logger.warning(
                "ventil.hahn_belegt",
                kanal=kanal,
                grund=lock.grund,
                aktive_zonen=list(lock.aktive_zonen),
                verbrauch_aktuell=lock.verbrauch_aktuell_lpm,
                verbrauch_neu=lock.verbrauch_neu_lpm,
                budget=lock.budget_lpm,
            )
            return False

        # valve_id aus Mapping resolven. Bei WaterControl bleibt das Mapping
        # leer und valve_id=None — der Client ignoriert ihn dann.
        valve_id = self._kanal_zu_valve_id.get(kanal)
        if not valve_id and self._kanal_zu_valve_id:
            # Mapping ist gefuellt, aber dieser Kanal fehlt -> Konfig-Luecke.
            logger.error(
                "ventil.kanal_nicht_gemappt",
                kanal=kanal,
                bekannte_kanaele=list(self._kanal_zu_valve_id.keys()),
                hinweis="ventil_name in Zonen-Konfig auf Gardena-App-Namen setzen.",
            )
            return False

        try:
            await self._client.ventil_oeffnen(
                self._ventil_geraet_id, dauer_s, valve_id=valve_id,
            )
        except Exception:
            logger.exception("ventil.oeffnen_fehlgeschlagen", kanal=kanal)
            return False

        aktiv = AktiveBewaesserung(
            kanal=kanal,
            geraet_id=self._ventil_geraet_id,
            zone_ids=zone_ids,
            dauer_s=dauer_s,
            ausloser=ausloser,
            valve_id=valve_id,
            lauf_gruppe=lauf_gruppe,
            phase=phase,
        )

        # Watchdog-Timer: schliesst nach dauer + Puffer
        loop = asyncio.get_event_loop()
        aktiv.timer_handle = loop.call_later(
            dauer_s + self._puffer_s,
            lambda k=kanal: asyncio.ensure_future(
                self._watchdog_schliessen(k)
            ),
        )
        self._aktiv[kanal] = aktiv

        # Event aufzeichnen (Dauer=0: steht erst beim SCHLIESSEN fest).
        # `ventil_id` = valve_id (matcht WS-Events fuer Paar-Lookup).
        for zid in zone_ids:
            await self._speicher.speichere_ventil_ereignis(VentilEreignis(
                zeitstempel=datetime.now(),
                zone_id=zid,
                ventil_id=aktiv.event_ventil_id,
                aktion=VentilAktion.OEFFNEN,
                dauer_sekunden=0,
                ausloser=ausloser,
                lauf_gruppe=lauf_gruppe,
                phase=phase,
            ))

        # T-0115: Persistierter State fuer Backend-Restart-Recovery.
        # Wir speichern dauer_s (statt nur OEFFNEN-Event mit dauer=0), damit
        # nach Restart Watchdog + verbleibend_s rekonstruiert werden koennen.
        try:
            await self._speicher.setze_live_lauf_state(
                kanal=kanal,
                valve_id=valve_id,
                geraet_id=self._ventil_geraet_id,
                zone_ids=zone_ids,
                dauer_sekunden=dauer_s,
                ausloser=ausloser.value,
                gestartet_am=aktiv.gestartet,
            )
        except Exception:
            logger.exception(
                "ventil.live_state_persist_fehler", kanal=kanal,
            )

        logger.info(
            "ventil.geoeffnet",
            kanal=kanal,
            zonen=zone_ids,
            dauer=dauer_s,
            ausloser=ausloser.value,
        )
        return True

    async def _automatik_daten_blind(
        self, jetzt: datetime,
    ) -> tuple[bool, float | None]:
        """T-0287: True wenn der Live-Zustand blind ist -- die neueste
        Gardena-Messung fehlt oder ist aelter als `_max_daten_alter`.
        Zweiter Wert = Alter in Minuten (None wenn kein Beat). Bei Gate-aus
        (`_max_daten_alter is None`) immer (False, None).
        """
        if self._max_daten_alter is None:
            return False, None
        beat = await self._speicher.letzter_gardena_beat()
        if beat is None:
            return True, None
        alter_min = (jetzt - beat).total_seconds() / 60.0
        return (jetzt - beat) > self._max_daten_alter, alter_min

    async def stoppe(self, kanal: int, ausloser: Ausloser) -> bool:
        """T-0299: Serialisiert das Schliessen pro Kanal (In-Flight-Guard).

        Verhindert, dass parallele Aufrufer (zweiter stoppe / notfall_stopp /
        bereits gefeuerter Watchdog-Task) denselben Lauf doppelt schliessen
        -> doppelte SCHLIESSEN-Writes + doppelte Cloud-Stops. Die eigentliche
        Schliess-Logik steht in `_stoppe_unlocked`.
        """
        lock = self._stop_locks.setdefault(kanal, asyncio.Lock())
        war_aktiv = kanal in self._aktiv
        async with lock:
            if war_aktiv and self._aktiv.get(kanal) is None:
                # Parallel-Aufrufer hat den Lauf geschlossen, waehrend wir auf
                # den Lock warteten -> kein zweites SCHLIESSEN/Cloud-Stop.
                logger.debug("ventil.stoppe_bereits_geschlossen", kanal=kanal)
                return True
            return await self._stoppe_unlocked(kanal, ausloser)

    async def _stoppe_unlocked(self, kanal: int, ausloser: Ausloser) -> bool:
        """Schliesst Ventil eines Kanals.

        Fail-safe:
        - Schreibt SCHLIESSEN-Events nur bei erfolgreichem Close.
        - Bei API-Fehler: _aktiv bleibt erhalten, Retry-Timer wird gesetzt.
        - ventil-status zeigt das Ventil weiterhin als offen.

        Externe Bewaesserungen (App / Schedule), die das Backend NICHT selbst
        gestartet hat, koennen ueber diesen Endpoint auch gestoppt werden —
        das ist eine explizite Manuell-User-Aktion (T-0113b). Es entsteht
        kein synthetisches SCHLIESSEN-Event aus VentilSicherung; die WS-
        Pipeline kriegt den realen Valve-State-Change und schreibt es.

        Returns:
            True bei erfolgreichem Schliessen, False bei Fehler.
        """
        aktiv = self._aktiv.get(kanal)
        if not aktiv:
            # Kein Backend-eigener Lauf — pruefen, ob wir ihn trotzdem
            # schliessen koennen (extern via App gestartet).
            valve_id = self._kanal_zu_valve_id.get(kanal)
            if not valve_id:
                logger.debug(
                    "ventil.nicht_aktiv_kein_mapping", kanal=kanal,
                )
                return True  # nichts zu tun
            logger.info(
                "ventil.stoppe_extern",
                kanal=kanal, valve_id=valve_id, ausloser=ausloser.value,
            )
            try:
                await self._client.ventil_schliessen(
                    self._ventil_geraet_id, valve_id=valve_id,
                )
            except Exception:
                logger.exception(
                    "ventil.stoppe_extern_fehlgeschlagen", kanal=kanal,
                )
                return False
            return True

        # Timer sofort canceln um Race zu verhindern (paralleles stoppe()
        # durch Watchdog waehrend await ventil_schliessen).
        # Bei Fehler wird ein Retry-Timer gesetzt.
        if aktiv.timer_handle:
            aktiv.timer_handle.cancel()
            aktiv.timer_handle = None

        # GardenaClient-Callback unterdruecken (wir schreiben selbst).
        # Key = valve_id (was die WS-Events tragen) statt geraet_id, damit bei
        # multi-valve-Geraeten nur der eigene Kanal-Callback unterdrueckt wird.
        self._unterdruecke_callback.add(aktiv.event_ventil_id)

        try:
            await self._client.ventil_schliessen(
                aktiv.geraet_id, valve_id=aktiv.valve_id,
            )
        except Exception:
            logger.exception("ventil.schliessen_fehlgeschlagen", kanal=kanal)
            # Callback-Unterdrueckung zuruecknehmen (Close kam nie an).
            self._unterdruecke_callback.discard(aktiv.event_ventil_id)
            # T-0380: Wenn fuer diesen Lauf bereits ein SCHLIESSEN existiert (die
            # WS-Pipeline hat den realen Cloud-Close nachgetragen), ist das Ventil
            # nachweislich ZU -- der Close-Befehl scheiterte, weil nichts zu
            # schliessen war. Dann State raeumen statt endlos gegen ein zu-Ventil
            # zu retryen (Realfall 02.07.: Watchdog-Close scheiterte am schon-zu-
            # Ventil -> _aktiv + live_lauf_state hingen 12h -> Geister-"laeuft" in
            # der UI, erst per Restart bereinigt). Sicher: raeumt NUR bei
            # nachgewiesenem Close-Event; ohne Beleg bleibt der Retry (Ventil
            # koennte real noch offen sein -> Safety erhalten). Kein neues Event.
            if await self._speicher.existiert_schliessen_seit(
                aktiv.event_ventil_id, aktiv.gestartet,
            ):
                logger.info(
                    "ventil.close_fehler_ventil_bereits_zu",
                    kanal=kanal, ausloser=ausloser.value,
                )
                self._aktiv.pop(kanal, None)
                try:
                    await self._speicher.loesche_live_lauf_state(
                        kanal, aktiv.geraet_id,
                    )
                except Exception:
                    logger.exception(
                        "ventil.live_state_loesch_fehler", kanal=kanal,
                    )
                return True
            # _aktiv bleibt: ventil-status zeigt Ventil weiter als offen.
            aktiv.retry_versuche += 1
            if aktiv.retry_versuche >= MAX_RETRY_VERSUCHE:
                # Keine weiteren Retries — manueller Eingriff noetig.
                logger.error(
                    "ventil.retry_aufgegeben",
                    kanal=kanal,
                    versuche=aktiv.retry_versuche,
                )
                return False
            # Neuen Retry-Timer setzen (Watchdog wurde oben gecancelt)
            self._setze_retry_timer(kanal)
            return False

        # Erfolg: aufräumen, Events schreiben
        self._aktiv.pop(kanal, None)
        dauer = int((datetime.now() - aktiv.gestartet).total_seconds())

        # T-0419: Ein WATCHDOG-Close misst gegen den EIGENEN Zustand, nicht
        # gegen die Realitaet. Wurde der Cloud-Close verpasst, laeuft unser
        # State weiter und der Watchdog schreibt die verstrichene Zeit --
        # die dann deutlich ueber der kommandierten Dauer liegt.
        # Realfall 21.07.: hecke lief laut Gardena-App 14:53-15:55 (3720 s,
        # exakt kommandiert). Der WS-Close wurde nicht gesehen, der Watchdog
        # schloss um 16:23:48 (= Start + max_dauer) und schrieb 5400 s.
        # Folge: 28 min Phantom-Wasser im Tagesbudget, voellig unbemerkt.
        #
        # Wir korrigieren den Wert hier bewusst NICHT: over-count ist die
        # sichere Richtung (die Engine giesst dann eher weniger), und ob der
        # Lauf wirklich frueher endete, weiss nur die Cloud. Aber es darf
        # nicht mehr STUMM passieren -- der DHS-Backfill soll es heilen, und
        # wenn er es nicht tut, muss man das im Log sehen koennen.
        if (
            ausloser == Ausloser.WATCHDOG
            and aktiv.dauer_s
            and dauer > aktiv.dauer_s + 60
        ):
            logger.warning(
                "ventil.watchdog_dauer_ueber_kommandiert",
                kanal=kanal,
                zone_ids=list(aktiv.zone_ids),
                kommandiert_s=aktiv.dauer_s,
                gemessen_s=dauer,
                differenz_s=dauer - aktiv.dauer_s,
                gestartet=aktiv.gestartet.isoformat(timespec="seconds"),
                hinweis=(
                    "Cloud-Close vermutlich verpasst; Dauer ist eine "
                    "Obergrenze, kein Messwert. DHS-Backfill sollte "
                    "korrigieren -- wenn nicht, siehe TASK.md T-0419."
                ),
            )

        # T-0331: Idempotenz-Guard. Hat der Puls (seit aktiv.gestartet) bereits
        # einen SCHLIESSEN, NICHT noch einen schreiben. Realfall 25.06.: der
        # Cloud-Timer schloss den Puls (06:37, sauberer Close), der zugehoerige
        # Watchdog-`call_later`-Timer fror waehrend Laptop-Schlaf ein und feuerte
        # erst beim Wake (08:47) -> stoppe(WATCHDOG) haette einen zweiten Close
        # mit dauer=Open->Wake (9164s) geschrieben (Doppel-Close, aufgeblaeht).
        # Der Cloud-Stop oben ist idempotent/harmlos; nur der DB-Write entfaellt.
        if await self._speicher.existiert_schliessen_seit(
            aktiv.event_ventil_id, aktiv.gestartet,
        ):
            logger.warning(
                "ventil.doppel_close_vermieden",
                kanal=kanal, ausloser=ausloser.value,
                gestartet=aktiv.gestartet.isoformat(timespec="minutes"),
                verworfene_dauer_s=dauer,
            )
        else:
            for zid in aktiv.zone_ids:
                await self._speicher.speichere_ventil_ereignis(VentilEreignis(
                    zeitstempel=datetime.now(),
                    zone_id=zid,
                    ventil_id=aktiv.event_ventil_id,
                    aktion=VentilAktion.SCHLIESSEN,
                    dauer_sekunden=dauer,
                    ausloser=ausloser,
                    lauf_gruppe=aktiv.lauf_gruppe,
                    phase=aktiv.phase,
                ))

        # T-0115: Persistierten State entfernen — Lauf ist sauber beendet.
        try:
            await self._speicher.loesche_live_lauf_state(
                kanal, aktiv.geraet_id,
            )
        except Exception:
            logger.exception(
                "ventil.live_state_loesch_fehler", kanal=kanal,
            )

        logger.info(
            "ventil.geschlossen",
            kanal=kanal,
            dauer=dauer,
            ausloser=ausloser.value,
        )
        return True

    def _setze_retry_timer(self, kanal: int) -> None:
        """Setzt einen Retry-Timer fuer fehlgeschlagene Close-Versuche."""
        aktiv = self._aktiv.get(kanal)
        if not aktiv:
            return
        # Bestehenden Timer canceln (idempotent falls abgelaufen)
        if aktiv.timer_handle:
            aktiv.timer_handle.cancel()
        loop = asyncio.get_event_loop()
        aktiv.timer_handle = loop.call_later(
            self._puffer_s,
            lambda k=kanal: asyncio.ensure_future(
                self._watchdog_schliessen(k)
            ),
        )
        logger.info(
            "ventil.retry_timer_gesetzt",
            kanal=kanal,
            in_sekunden=self._puffer_s,
        )

    async def verarbeite_callback(self, ereignis: VentilEreignis) -> bool:
        """Verarbeitet einen Gardena-Ventilcallback (WebSocket).

        Wird von main.py aufgerufen BEVOR das Event in die DB geschrieben wird.
        Synchronisiert _aktiv-Zustand und schreibt ggf. Events mit korrektem Ausloser.

        Returns:
            True wenn VentilSicherung das Event behandelt hat (nicht nochmal speichern).
            False wenn das Event extern ist und normal gespeichert werden soll.
        """
        if not self._event_gehoert_zu_sicherung(ereignis.ventil_id):
            return False

        # OEFFNEN nur unterdruecken wenn exakt DIE Zone bereits aktiv ist.
        # Bei Dual-Channel-Ventilen (ein Geraet, mehrere Kanaele) darf ein
        # OEFFNEN fuer einen anderen Kanal NICHT unterdrueckt werden.
        if ereignis.aktion == VentilAktion.OEFFNEN:
            if any(
                a.event_ventil_id == ereignis.ventil_id
                and ereignis.zone_id in a.zone_ids
                for a in self._aktiv.values()
            ):
                logger.debug("ventil.callback_oeffnen_unterdrueckt")
                return True
            return False  # Externes Oeffnen (anderer Kanal/Zone), normal speichern

        # SCHLIESSEN verarbeiten
        if ereignis.aktion == VentilAktion.SCHLIESSEN:
            # Wurde von stoppe() initiiert? → stoppe() hat Events bereits
            # geschrieben. Match auf valve_id (= event-ventil_id) UND auf
            # geraet_id (Backward-Compat fuer Setups vor Kanal-Mapping).
            unterdrueck_keys = {ereignis.ventil_id, self._ventil_geraet_id}
            if unterdrueck_keys & self._unterdruecke_callback:
                self._unterdruecke_callback -= unterdrueck_keys
                logger.debug("ventil.callback_schliessen_unterdrueckt")
                return True

            # Normales Schliessen (GardenaClient-Timer): _aktiv synchronisieren.
            # Nur den Kanal synchronisieren, dessen Zone sich wirklich schliesst
            # — bei Dual-Channel-Ventilen haengen mehrere Kanaele am gleichen Geraet.
            for kanal, aktiv in list(self._aktiv.items()):
                if (aktiv.event_ventil_id == ereignis.ventil_id
                        and ereignis.zone_id in aktiv.zone_ids):
                    if aktiv.timer_handle:
                        aktiv.timer_handle.cancel()

                    dauer = int((datetime.now() - aktiv.gestartet).total_seconds())
                    uebersprungene_zonen: list[str] = []
                    for zid in aktiv.zone_ids:
                        if await self._speicher.existiert_schliessen_seit(
                            aktiv.event_ventil_id, aktiv.gestartet, [zid],
                        ):
                            uebersprungene_zonen.append(zid)
                            continue
                        await self._speicher.speichere_ventil_ereignis(
                            VentilEreignis(
                                zeitstempel=datetime.now(),
                                zone_id=zid,
                                ventil_id=aktiv.event_ventil_id,
                                aktion=VentilAktion.SCHLIESSEN,
                                dauer_sekunden=dauer,
                                ausloser=aktiv.ausloser,
                                lauf_gruppe=aktiv.lauf_gruppe,
                                phase=aktiv.phase,
                            )
                        )
                    if uebersprungene_zonen:
                        logger.warning(
                            "ventil.callback_doppel_close_vermieden",
                            kanal=kanal,
                            gestartet=aktiv.gestartet.isoformat(timespec="minutes"),
                            verworfene_dauer_s=dauer,
                            zonen=uebersprungene_zonen,
                        )

                    del self._aktiv[kanal]
                    # T-0115: persistierten State auch hier loeschen
                    # (Cloud-Timer hat das Ventil regulaer geschlossen).
                    try:
                        await self._speicher.loesche_live_lauf_state(
                            kanal, aktiv.geraet_id,
                        )
                    except Exception:
                        logger.exception(
                            "ventil.live_state_loesch_fehler", kanal=kanal,
                        )
                    logger.info(
                        "ventil.normal_geschlossen",
                        kanal=kanal,
                        dauer_s=dauer,
                        ausloser=aktiv.ausloser.value,
                    )
                    return True  # Event behandelt

        return False  # Externes Event, normal speichern

    async def notfall_stopp(self) -> dict:
        """Schliesst ALLE offenen Ventile sofort.

        Returns:
            Dict mit 'geschlossen' (Anzahl) und 'fehler' (Liste fehlgeschlagener Kanaele).
        """
        kanaele = list(self._aktiv.keys())
        geschlossen = 0
        fehlgeschlagen: list[dict] = []
        backend_event_ids = {
            aktiv.event_ventil_id for aktiv in self._aktiv.values()
        }

        for kanal in kanaele:
            erfolg = await self.stoppe(kanal, Ausloser.NOTFALL_STOPP)
            if erfolg:
                geschlossen += 1
            else:
                fehlgeschlagen.append({
                    "geraet_id": self._ventil_geraet_id,
                    "kanal": kanal,
                    "quelle": "backend",
                })

        offene_valves = {}
        offene_func = getattr(self._client, "offene_valves", None)
        if callable(offene_func):
            try:
                offene_valves = offene_func() or {}
            except Exception:
                logger.exception("ventil.notfall_offene_valves_fehler")
        for kanal, valve_id in self._kanal_zu_valve_id.items():
            if valve_id in backend_event_ids:
                continue
            if valve_id not in offene_valves:
                continue
            erfolg = await self.stoppe(kanal, Ausloser.NOTFALL_STOPP)
            if erfolg:
                geschlossen += 1
            else:
                fehlgeschlagen.append({
                    "geraet_id": self._ventil_geraet_id,
                    "kanal": kanal,
                    "valve_id": valve_id,
                    "quelle": "extern",
                })

        if kanaele or geschlossen or fehlgeschlagen:
            logger.warning(
                "ventil.notfall_stopp",
                kanaele=kanaele,
                geschlossen=geschlossen,
                fehlgeschlagen=fehlgeschlagen,
            )

        return {"geschlossen": geschlossen, "fehlgeschlagen": fehlgeschlagen}

    async def startup_check(self) -> None:
        """Reconciliation persistierter Live-Laufe mit dem realen
        Cloud-Valve-State.

        T-0276 (28.05.): vorher hat diese Methode pauschal jeden
        persistierten Lauf geschlossen -- auch User-Lauefe, die regulaer
        weiterliefen. Realfall heute: Backend-Restart 08:38:39 waehrend
        eines manuellen 48-min-Laufs hat den Lauf nach 8:33 min abgewuergt
        (`ventil.startup_sicherheitsschliessung`). Codex' Original-
        Empfehlung in T-0274 F1 verlangte explizit "Startup-Close gegen
        realen Valve-State reconciliieren" -- das hatte ich uebersehen.

        Neuer Vertrag:
          - Cloud sagt "geschlossen"  -> State entsorgen + synthetisches
            SCHLIESSEN-Event (Ausloser=WATCHDOG, Cloud hat zugemacht
            waehrend Backend down war oder Crash-Recovery-Fall).
          - Cloud sagt "offen"        -> State BEHALTEN, `recover_aus_db()`
            rekonstruiert direkt danach `_aktiv` + Watchdog-Timer mit
            Restdauer. Lauf laeuft regulaer zu Ende.

        Initial-WS-Sync: py-smart-gardena kann deutlich laenger als 2 s
        brauchen, bis der erste Valve-Status im Client angekommen ist. Solange
        der Status einer Valve unbekannt ist, raeumen wir NICHT auf.
        """
        try:
            states = await self._speicher.hole_live_lauf_states(
                self._ventil_geraet_id,
            )
        except Exception:
            logger.exception("ventil.startup_state_laden_fehler")
            return
        if not states:
            return

        offen_states = self._client._VENTIL_OFFEN_STATES

        for s in states:
            kanal = int(s["kanal"])
            valve_id = s.get("valve_id") or self._kanal_zu_valve_id.get(kanal)
            event_ventil_id = valve_id or self._ventil_geraet_id
            status_bekannt = True
            warte_status = getattr(self._client, "warte_auf_ventil_status", None)
            if valve_id and callable(warte_status):
                status_bekannt = await warte_status(
                    valve_id, STARTUP_VALVE_STATUS_TIMEOUT_S,
                )
            if not status_bekannt:
                logger.warning(
                    "ventil.startup_state_unbekannt_behalten",
                    geraet=self._ventil_geraet_id,
                    kanal=kanal,
                    valve_id=valve_id,
                    timeout_s=STARTUP_VALVE_STATUS_TIMEOUT_S,
                )
                continue
            cloud_valves = self._client.offene_valves()
            cloud_info = cloud_valves.get(valve_id) if valve_id else None
            cloud_offen = bool(
                cloud_info and cloud_info.get("activity") in offen_states
            )

            if cloud_offen:
                # User-/Schedule-/Auto-Lauf laeuft regulaer.
                # recover_aus_db() rekonstruiert `_aktiv` direkt danach.
                logger.info(
                    "ventil.startup_lauf_aktiv_rekonstruiere",
                    geraet=self._ventil_geraet_id,
                    kanal=kanal,
                    valve_id=valve_id,
                    ausloser=s.get("ausloser"),
                    cloud_activity=cloud_info.get("activity") if cloud_info else None,
                )
                continue

            # Cloud sagt zu -> Crash-Recovery-Fall.
            # F7: synthetische Dauer auf die GEPLANTE Dauer cappen. Der Cloud-
            # Override schliesst spaetestens bei gestartet + dauer_sekunden
            # (hart bei 3600s). Ohne Cap wird bei langer Downtime die ganze
            # verstrichene Zeit (z.B. 5h auf einen 10-min-Lauf) als Wasserzeit
            # geloggt und vergiftet ML-Features/Budget/Bilanz.
            verstrichen = int(
                (datetime.now() - s["gestartet_am"]).total_seconds()
            )
            geplant = int(s.get("dauer_sekunden") or 0)
            cap = min(geplant, 3600) if geplant > 0 else 3600
            dauer = max(0, min(verstrichen, cap))
            # T-0338: Idempotenz. Wurde der Lauf zwischenzeitlich schon
            # geschlossen (Realfall: der DHS-Backfill trug den von der Live-WS
            # verpassten Close nach -- `dhs.fehlenden_close_eingefuegt` --,
            # loeschte aber `live_lauf_state` nicht), darf hier KEIN zweiter
            # Watchdog-Close entstehen (sonst Duplikat-Close + Bilanz-Doppel-
            # zaehlung). State trotzdem raeumen. Anderer Pfad als der T-0331-
            # Wake-Watchdog, gleicher Idempotenz-Guard.
            if await self._speicher.existiert_schliessen_seit(
                event_ventil_id, s["gestartet_am"],
            ):
                await self._speicher.loesche_live_lauf_state(
                    kanal, self._ventil_geraet_id,
                )
                logger.info(
                    "ventil.startup_state_bereits_geschlossen",
                    geraet=self._ventil_geraet_id,
                    kanal=kanal,
                    valve_id=valve_id,
                    ventil_id=event_ventil_id,
                )
                continue
            # T-0335 (bewusste Scope-Grenze): Dieser Startup-Recovery-Close zieht
            # aus `live_lauf_state`, das die Pre-Soak-Marker NICHT persistiert ->
            # lauf_gruppe/phase bleiben None. Greift nur, wenn das Backend GENAU
            # waehrend eines offenen Pulses neu startet (WATCHDOG-Notfall-Close
            # eines unterbrochenen Laufs). Dann erscheint dieser Close in der
            # Historie ungruppiert -- was korrekt zeigt, dass der Lauf nicht
            # sauber abgeschlossen wurde. Die regulaeren Close-Pfade (stoppe,
            # WS-cloud-close) tragen die Gruppe eindeutig.
            for zid in s["zone_ids"]:
                await self._speicher.speichere_ventil_ereignis(VentilEreignis(
                    zeitstempel=datetime.now(),
                    zone_id=zid,
                    ventil_id=event_ventil_id,
                    aktion=VentilAktion.SCHLIESSEN,
                    dauer_sekunden=max(0, dauer),
                    ausloser=Ausloser.WATCHDOG,
                ))
            await self._speicher.loesche_live_lauf_state(
                kanal, self._ventil_geraet_id,
            )
            logger.info(
                "ventil.startup_state_aufgeraeumt",
                geraet=self._ventil_geraet_id,
                kanal=kanal,
                valve_id=valve_id,
                dauer_s=dauer,
                grund="cloud_geschlossen",
            )

    def ist_aktiv(self, kanal: int) -> bool:
        """Prueft ob Kanal gerade bewaessert wird."""
        return kanal in self._aktiv

    def aktive_bewaesserungen(self) -> dict[int, dict]:
        """Gibt Status aller aktiven Bewaesserungen zurueck (fuer API)."""
        return {
            kanal: {
                "zone_ids": a.zone_ids,
                "dauer_s": a.dauer_s,
                # TZ-Info explizit (api-response-konsistent mit _iso() in api_server.py)
                "gestartet": a.gestartet.astimezone().isoformat(),
                "ausloser": a.ausloser.value,
                "verbleibend_s": max(
                    0,
                    a.dauer_s - int((datetime.now() - a.gestartet).total_seconds())
                ),
            }
            for kanal, a in self._aktiv.items()
        }

    async def _watchdog_schliessen(self, kanal: int) -> None:
        """Watchdog-Timer-Callback: Ventil zwangsschliessen."""
        if kanal not in self._aktiv:
            return
        logger.warning("ventil.watchdog_ausgeloest", kanal=kanal)
        await self.stoppe(kanal, Ausloser.WATCHDOG)
