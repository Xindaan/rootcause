"""T-0111 / T-0116 / T-0336: Pre-Soak-Sequenz als loop-getriebene State-Machine.

User-Praxis 29.04.: kurze Vorwaesserung (5 min) -> Pause (~30 min) -> Hauptdose
(60-90 min). Boden vorbenetzen, damit der Hauptpuls weniger Tiefensickerung
verursacht und (Mikrodrip) Hydrophobie bricht.

**Architektur (T-0336):** KEIN langlebiger `asyncio.sleep`-Task mehr. Eine
Sequenz ist ein persistierter State (`pre_soak_state`, DB) plus Wall-Clock. Die
Phasen-Uebergaenge treibt `tick(jetzt)` -- aufgerufen vom Entscheidungsloop
(5-min-Takt, `asyncio.wait_for`-getimt, wake-sicher) UND einmalig von `starte()`
(Puls sofort). Vorteile:
  - **Sleep-fest**: ein `asyncio.sleep` in der Pause wuerde einen Laptop-Sleep
    nicht zuverlaessig ueberleben (vgl. T-0331 eingefrorener call_later) -> die
    Hauptdose koennte nie feuern. Der wake-sichere Loop-Tick zieht den faelligen
    Uebergang per Wall-Clock nach.
  - **Eine Wahrheit**: DB-State + Wall-Clock, kein zweiter Treiber.
  - **Recovery == Normalbetrieb**: Restart und Sleep-Wake sind derselbe Pfad
    (`tick` macht den faelligen Uebergang). `recover_aus_db` laedt nur den DB-
    State zurueck in den Speicher und tickt.

Die Ventil-Schliesszeit bleibt cloud-genau (Cloud-Override schliesst Puls/Haupt
nach gesetzter Dauer); nur der Phasen-START ist auf den Tick-Takt genau.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

from bewaesserung.modelle import Ausloser
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import VentilSicherung

logger = structlog.get_logger()

# Phasen-Rang fuer den idempotenten Fortschritts-Check (tick darf eine Phase nie
# zurueckdrehen und keine schon erreichte Phase erneut starten).
# T-0437: "haupt_pause" (Soak-Pause ZWISCHEN zwei Haupt-Pulsen) bekommt den
# gleichen Rang wie "haupt". Der Rang darf nicht sinken, sonst wuerden die
# spaeteren Zweige die Phase auf "pause" zuruecksetzen oder gar den
# Pre-Soak-Puls erneut starten (`_PHASE_RANG.get(..., 0)` faellt sonst auf 0).
# Die Idempotenz der Haupt-Pulse haengt an `haupt_pulse_gestartet`, nicht am Rang.
_PHASE_RANG = {"neu": 0, "pre_soak": 1, "pause": 2, "haupt": 3, "haupt_pause": 3}
_END_PHASEN = ("fertig", "fehler", "stop_fehler")

# T-0344: Wie lange nach dem nominalen Sequenz-Ende (pause_s + haupt_s) eine noch
# nicht gelaufene Hauptdose vom Tick nachgezogen werden darf. Deckt ab, dass das
# Hauptdose-Fenster kuerzer als der Loop-Tick (~300s) ist (haupt_s < Tick -> Tick
# springt drueber, fertig schluckt die Dose) oder ein Laptop-Sleep es uebersprang.
# Groesser als ein Tick-Intervall (Nachzug beim ersten Tick nach Pausenende),
# aber begrenzt: bei groesserer Verspaetung ist die Vorbenetzung verpufft -> kein
# echter Pre-Soak mehr, dann als fehler markieren statt veraltete Dose zu feuern.
_HAUPT_NACHZUG_TOLERANZ_S = 600
# T-0363: Wenn die Hauptdose nach erfolgreichem Puls transient nicht startet
# (API/DNS/Netz), nicht sofort endgueltig fehlschlagen. Innerhalb der
# Nachzug-Toleranz retryt der Ticker in kurzem Abstand.
_HAUPT_RETRY_INTERVAL_S = 60


@dataclass
class PreSoakLauf:
    """In-Memory-State einer laufenden Pre-Soak-Sequenz."""
    zone_id: str
    kanal: int
    zone_ids_kanal: list[str]
    pre_soak_s: int
    pause_s: int
    haupt_s: int
    gestartet_am: datetime
    # neu = noch nichts gestartet; pre_soak/pause/haupt = hoechste erreichte
    # Stufe; fertig/fehler/stop_fehler = Endzustand.
    phase: str = "neu"
    # T-0335: gemeinsame Lauf-Gruppe fuer Puls + Haupt -> Giess-Historie
    # gruppiert beide bewaessere()-Phasen zu EINEM Pre-Soak-Lauf.
    lauf_gruppe: str = ""
    # T-0343: Ausloser des Laufs (MANUELL = Dashboard, AUTOMATIK = Auto-Loop).
    # Wird an bewaessere() durchgereicht, damit Auto-Pre-Soaks nicht faelschlich
    # als "manuell" geloggt werden.
    ausloser: Ausloser = Ausloser.MANUELL
    fehler: str | None = None
    letzter_haupt_fehler_am: datetime | None = None
    # T-0437: Cycle-and-Soak. `haupt_pulse` teilt `haupt_s` in n gleich lange
    # Pulse AUF (Gesamt-Wassermenge unveraendert), dazwischen `haupt_pause_s`
    # Einsickerzeit. Defaults 1/0 = bisheriges Verhalten (ein Lauf am Stueck).
    # `haupt_pulse_gestartet` ist der Idempotenz-Zaehler: der Tick startet
    # Puls i nur, wenn er noch nicht gestartet wurde. Er ersetzt fuer die
    # Haupt-Phase den `_PHASE_RANG`-Vergleich, weil die Phase zwischen
    # "haupt" und "haupt_pause" hin und her geht und der Rang dabei sinken
    # wuerde.
    haupt_pulse: int = 1
    haupt_pause_s: int = 0
    haupt_pulse_gestartet: int = 0

    @property
    def haupt_puls_s(self) -> int:
        """Dauer EINES Haupt-Pulses. Die Gesamtmenge bleibt `haupt_s`."""
        return max(1, self.haupt_s // max(1, self.haupt_pulse))

    def haupt_puls_start_s(self, index: int) -> int:
        """Offset ab `gestartet_am`, an dem Puls `index` (1-basiert) faellt."""
        return self.pause_s + (index - 1) * (self.haupt_puls_s + self.haupt_pause_s)

    @property
    def ende_s(self) -> int:
        """Offset, ab dem die gesamte Sequenz vorbei ist."""
        return self.haupt_puls_start_s(self.haupt_pulse) + self.haupt_puls_s

    def faelliger_puls(self, verstrichen: float) -> int:
        """Hoechster Puls-Index (1-basiert), dessen Startzeit erreicht ist.

        0 = noch keiner faellig. Wird gegen `haupt_pulse_gestartet` geprueft;
        ist er groesser, steht ein Start aus.
        """
        faellig = 0
        for i in range(1, self.haupt_pulse + 1):
            if verstrichen >= self.haupt_puls_start_s(i):
                faellig = i
            else:
                break
        return faellig

    def status_dict(self) -> dict:
        return {
            "zone_id": self.zone_id,
            "kanal": self.kanal,
            "phase": self.phase,
            "pre_soak_s": self.pre_soak_s,
            "pause_s": self.pause_s,
            "haupt_s": self.haupt_s,
            # T-0437: die UI muss "Puls 2 von 3" zeigen koennen, sonst liest
            # sich eine Soak-Pause wie ein haengender Lauf.
            "haupt_pulse": self.haupt_pulse,
            "haupt_pause_s": self.haupt_pause_s,
            "haupt_pulse_gestartet": self.haupt_pulse_gestartet,
            "gestartet_am": self.gestartet_am.isoformat(),
            "fehler": self.fehler,
            # T-0431: der Ausloeser gehoert in den Status. Die Karte
            # beschriftete jeden laufenden Pre-Soak als "Manuell aktiv",
            # weil sie ihn nicht kannte -- auch einen, den die Automatik
            # gestartet hat. Der Wert steht in der DB (`pre_soak_state.
            # ausloser`), kam aber nie im Frontend an.
            "ausloser": self.ausloser.value,
        }


class PreSoakManager:
    """Verwaltet Pre-Soak-Sequenzen pro Zone als loop-getriebene State-Machine."""

    def __init__(
        self,
        ventil_sicherung: VentilSicherung,
        speicher: Speicher | None = None,
        erlaubte_zone_ids: set[str] | None = None,
        puls_gate: Callable[
            [int, list[str]], Awaitable[tuple[bool, str | None]]
        ] | None = None,
    ):
        self._sicherung = ventil_sicherung
        self._speicher = speicher
        # T-0437: wird VOR jedem Folge-Puls (Index >= 2) gefragt: "darf dieser
        # Kanal noch giessen?". None = kein Gate (Bestandsverhalten, und bei
        # haupt_pulse=1 ohnehin nie erreicht). main.py verdrahtet es auf den
        # sensorbasierten Kanal-Max-Stop, der waehrend einer Soak-Pause sonst
        # nicht laeuft (dort ist das Ventil zu).
        self._puls_gate = puls_gate
        # Multi-DSWC: ein Manager bedient nur die Zonen seiner Sicherung.
        self._erlaubte_zone_ids = set(erlaubte_zone_ids) if erlaubte_zone_ids else None
        # Pro Zone hoechstens eine Sequenz.
        self._laeufe: dict[str, PreSoakLauf] = {}
        # Serialisiert starte()/stoppe()/tick() -> race-freier Idempotenz-Check
        # (Loop-Task und API-Handler koennen parallel ticken).
        self._lock = asyncio.Lock()

    def laufender_lauf(self, zone_id: str) -> PreSoakLauf | None:
        """Gibt den aktuellen Lauf einer Zone zurueck (oder None).

        ACHTUNG (T-0411): rein zone-gekeyt. Fuer die Frage "laeuft an DIESER
        Zone gerade Wasser?" ist das die falsche Frage, wenn mehrere Zonen am
        selben Ventil-Kanal haengen -- dann nutze `lauf_fuer_kanal_der_zone`.
        """
        return self._laeufe.get(zone_id)

    def lauf_fuer_kanal_der_zone(self, zone_id: str) -> PreSoakLauf | None:
        """T-0411: Lauf, der diese Zone PHYSISCH betrifft.

        Das Ventil haengt am Kanal, nicht an der Zone: bambuswald und
        bambuswald_yogaraum teilen sich (DSWC 1, Kanal 2). Ein Pre-Soak, den
        `bambuswald` gestartet hat, giesst den Yogaraum mit -- `_laeufe` ist
        aber `zone_id`-gekeyt, also fand die Yogaraum-Abfrage nichts und die
        UI meldete "ruhig", waehrend Wasser lief (User-Befund 19.07.).

        Der eigene Lauf hat Vorrang; sonst der aktive Lauf einer Geschwister-
        Zone (`zone_ids_kanal` traegt die Kanal-Zugehoerigkeit bereits, inkl.
        DSWC-Trennung -- s. `pre-soak-start`). Beendete Fremd-Laeufe werden
        NICHT geliefert: der eigene Endzustand ist fuer die Karte relevant,
        der abgeschlossene Lauf der Nachbarzone nicht.
        """
        eigen = self._laeufe.get(zone_id)
        if eigen is not None:
            return eigen
        for lauf in self._laeufe.values():
            if lauf.phase in _END_PHASEN:
                continue
            if zone_id in lauf.zone_ids_kanal:
                return lauf
        return None

    def aktiver_lauf_fuer_kanal_der_zone(self, zone_id: str) -> PreSoakLauf | None:
        """T-0411: wie `lauf_fuer_kanal_der_zone`, aber NUR laufende Sequenzen.

        Fuer Guards gedacht ("darf ich den Kanal jetzt anfassen?"): dort
        interessiert ein bereits beendeter eigener Lauf nicht, wohl aber ein
        laufender der Nachbarzone. Wichtig, weil die Pause-Phase das Ventil
        SCHLIESST -- `VentilSicherung.starte()` sieht den Kanal dann frei und
        wuerde einen manuellen Start mitten in die Sequenz lassen.
        """
        lauf = self.lauf_fuer_kanal_der_zone(zone_id)
        if lauf is None or lauf.phase in _END_PHASEN:
            return None
        return lauf

    def alle_laufenden(self) -> list[PreSoakLauf]:
        return [l for l in self._laeufe.values() if l.phase not in _END_PHASEN]

    def ist_aktiv(self, zone_id: str) -> bool:
        """True, wenn fuer die Zone eine Sequenz laeuft (inkl. Pause-Phase).

        T-0336: Der Auto-Loop nutzt das fuer Re-Entrancy -- in der Pause ist das
        Ventil zu (`VentilSicherung.ist_aktiv` False), die Sequenz laeuft aber.
        """
        lauf = self._laeufe.get(zone_id)
        return lauf is not None and lauf.phase not in _END_PHASEN

    def sekunden_bis_naechster_uebergang(
        self, jetzt: datetime | None = None
    ) -> float | None:
        """T-0346: Sekunden bis zur naechsten faelligen, ZEIT-getriebenen
        Phasen-Transition ueber alle aktiven Laeufe.

        Damit kann der feine Pre-Soak-Ticker exakt an der Wall-Clock-Grenze
        aufwachen und Phasenwechsel + Hauptdose auf die Sekunde feuern, statt
        bis zu einen Loop-Tick (~5 min) spaet. None, wenn kein Lauf eine
        zukuenftige zeit-getriebene Grenze hat (keine Laeufe oder nur End-
        phasen inkl. stop_fehler) -> der Ticker faellt auf sein Idle-Intervall
        zurueck, das den stop_fehler-Recheck (T-0342) mitnimmt.
        """
        jetzt = jetzt or datetime.now()
        bester: float | None = None
        for lauf in self._laeufe.values():
            if lauf.phase in _END_PHASEN:
                continue
            verstrichen = (jetzt - lauf.gestartet_am).total_seconds()
            # T-0437: faellig, aber noch nicht gestartet -> sofort ticken.
            # Frueher haing das an `phase == "pause"`; mit Mehrfach-Puls steht
            # zwischen zwei Pulsen "haupt_pause", also am Zaehler pruefen.
            if lauf.faelliger_puls(verstrichen) > lauf.haupt_pulse_gestartet:
                if lauf.letzter_haupt_fehler_am is None:
                    delta = 0.5
                else:
                    delta = (
                        _HAUPT_RETRY_INTERVAL_S
                        - (jetzt - lauf.letzter_haupt_fehler_am).total_seconds()
                    )
                if delta <= 0:
                    delta = 0.5
                bester = delta if bester is None else min(bester, delta)
                continue
            # Grenzen aufsteigend: pre_soak_s (->pause), jeder Puls-Start
            # (->haupt), jedes Puls-Ende (->haupt_pause) und ende_s (->fertig).
            # Ohne die Puls-Grenzen wacht der feine Ticker nur zum ersten Puls
            # auf und verschlaeft alle weiteren.
            grenzen = [lauf.pre_soak_s, lauf.ende_s]
            for i in range(1, lauf.haupt_pulse + 1):
                start_i = lauf.haupt_puls_start_s(i)
                grenzen.append(start_i)
                grenzen.append(start_i + lauf.haupt_puls_s)
            for grenze in sorted(set(grenzen)):
                delta = grenze - verstrichen
                if delta > 0:
                    bester = delta if bester is None else min(bester, delta)
                    break
        return bester

    async def _persistiere(self, lauf: PreSoakLauf) -> None:
        if self._speicher is None:
            return
        try:
            await self._speicher.setze_pre_soak_state(
                zone_id=lauf.zone_id,
                kanal=lauf.kanal,
                zone_ids_kanal=lauf.zone_ids_kanal,
                pre_soak_s=lauf.pre_soak_s,
                pause_s=lauf.pause_s,
                haupt_s=lauf.haupt_s,
                gestartet_am=lauf.gestartet_am,
                phase=lauf.phase,
                ausloser=lauf.ausloser.value,
                haupt_pulse=lauf.haupt_pulse,
                haupt_pause_s=lauf.haupt_pause_s,
                haupt_pulse_gestartet=lauf.haupt_pulse_gestartet,
            )
        except Exception:
            logger.exception("pre_soak.persist_fehler", zone_id=lauf.zone_id)

    async def _entferne_persistenz(self, zone_id: str) -> None:
        if self._speicher is None:
            return
        try:
            await self._speicher.loesche_pre_soak_state(zone_id)
        except Exception:
            logger.exception("pre_soak.persist_loesch_fehler", zone_id=zone_id)

    async def _fehler(self, lauf: PreSoakLauf, grund: str) -> None:
        lauf.phase = "fehler"
        lauf.fehler = grund
        await self._entferne_persistenz(lauf.zone_id)
        logger.error("pre_soak.fehler", zone_id=lauf.zone_id, grund=grund)

    async def starte(
        self,
        zone_id: str,
        kanal: int,
        zone_ids_kanal: list[str],
        pre_soak_min: int,
        pause_min: int,
        haupt_min: int,
        ausloser: Ausloser = Ausloser.MANUELL,
        haupt_pulse: int = 1,
        haupt_puls_pause_min: int = 0,
    ) -> tuple[bool, str | None]:
        """Startet eine Pre-Soak-Sequenz (legt State an + tickt den Puls sofort).

        `ausloser`: MANUELL (Dashboard, Default) oder AUTOMATIK (Auto-Loop,
        T-0343) -- wird in alle Ventil-Events des Laufs geschrieben.

        `pause_min` = Gesamt-Pause vom Pre-Soak-START bis Haupt-START. Die
        effektive Wartezeit ZWISCHEN Pre-Soak-Ende und Haupt-Start ist
        `pause_min - pre_soak_min`. Returns: (ok, fehlermeldung).
        """
        async with self._lock:
            # T-0411: kanal-weit pruefen, nicht nur zone-weit. Zwei Zonen am
            # selben Ventil-Kanal (bambuswald / bambuswald_yogaraum) teilen
            # sich die Hardware -- ein zweiter Pre-Soak auf der Geschwister-
            # Zone wuerde sonst durchkommen und dieselbe Sequenz doppelt
            # fahren. Der Belegt-Guard der VentilSicherung faengt das nicht:
            # in der Pause-Phase ist das Ventil zu.
            alter = self.aktiver_lauf_fuer_kanal_der_zone(zone_id)
            if alter is not None:
                woher = (
                    "" if alter.zone_id == zone_id
                    else f" auf Zone {alter.zone_id} am selben Kanal"
                )
                return False, f"Sequenz laeuft bereits{woher} (Phase {alter.phase})"
            if pre_soak_min <= 0 or pause_min <= 0 or haupt_min <= 0:
                return False, "Alle Dauern muessen > 0 sein"
            # T-0437: Aufteilung muss aufgehen. Ein Puls unter einer Minute ist
            # unter der Sensor-/Hardware-Aufloesung und waere nur Verschleiss.
            if haupt_pulse < 1:
                return False, "haupt_pulse muss >= 1 sein"
            if haupt_puls_pause_min < 0:
                return False, "haupt_puls_pause_min darf nicht negativ sein"
            if haupt_pulse > 1 and (haupt_min * 60) // haupt_pulse < 60:
                return False, (
                    f"Hauptdose {haupt_min} min auf {haupt_pulse} Pulse ergibt "
                    "unter 1 min pro Puls"
                )
            if pause_min < pre_soak_min:
                return False, (
                    f"pause_min ({pause_min}) muss >= pre_soak_min "
                    f"({pre_soak_min}) sein"
                )

            gestartet = datetime.now()
            lauf = PreSoakLauf(
                zone_id=zone_id, kanal=kanal,
                zone_ids_kanal=list(zone_ids_kanal),
                pre_soak_s=pre_soak_min * 60,
                pause_s=pause_min * 60,
                haupt_s=haupt_min * 60,
                haupt_pulse=haupt_pulse,
                haupt_pause_s=haupt_puls_pause_min * 60,
                gestartet_am=gestartet,
                lauf_gruppe=f"presoak_{zone_id}_{gestartet.strftime('%Y%m%d%H%M%S')}",
                ausloser=ausloser,
            )
            self._laeufe[zone_id] = lauf
            logger.info(
                "pre_soak.gestartet",
                zone_id=zone_id, kanal=kanal,
                pre_soak_min=pre_soak_min, pause_min=pause_min,
                haupt_min=haupt_min,
            )
            # Puls sofort starten (nicht erst beim naechsten Loop-Tick).
            await self._tick_lauf(lauf, gestartet)
            if lauf.phase == "fehler":
                return False, lauf.fehler
            return True, None

    async def stoppe(self, zone_id: str) -> tuple[bool, str | None]:
        """Bricht eine laufende Sequenz ab und stoppt ein evtl. offenes Ventil."""
        async with self._lock:
            lauf = self._laeufe.get(zone_id)
            if lauf is None or lauf.phase in _END_PHASEN:
                return False, "Keine laufende Sequenz fuer diese Zone"
            # Ventil nur stoppen, wenn es offen ist (pre_soak/haupt). In der
            # Pause ist nichts offen -> kein Stop noetig.
            if self._sicherung.ist_aktiv(lauf.kanal):
                try:
                    # T-0343: Close mit dem Lauf-Ausloser (konsistent mit den
                    # OEFFNEN-Events; ein Auto-Lauf bleibt durchgehend AUTOMATIK).
                    erfolg = await self._sicherung.stoppe(
                        lauf.kanal, lauf.ausloser,
                    )
                except Exception:
                    logger.exception("pre_soak.stop_fehler", zone_id=zone_id)
                    erfolg = False
                if not erfolg:
                    lauf.phase = "stop_fehler"
                    lauf.fehler = "Ventil-Stop fehlgeschlagen; Zustand unklar"
                    await self._persistiere(lauf)
                    logger.error("pre_soak.stop_fehlgeschlagen", zone_id=zone_id)
                    return False, lauf.fehler
            lauf.phase = "fehler"
            lauf.fehler = "Vom User abgebrochen"
            await self._entferne_persistenz(zone_id)
            logger.info("pre_soak.abgebrochen", zone_id=zone_id)
            return True, None

    async def tick(self, jetzt: datetime | None = None) -> None:
        """Treibt alle laufenden Sequenzen einen Schritt weiter (idempotent).

        Vom Entscheidungsloop pro Zyklus aufgerufen. Macht pro Lauf hoechstens
        einen faelligen Phasen-Uebergang (Wall-Clock-getrieben).
        """
        jetzt = jetzt or datetime.now()
        async with self._lock:
            for lauf in list(self._laeufe.values()):
                await self._tick_lauf(lauf, jetzt)

    async def _tick_lauf(self, lauf: PreSoakLauf, jetzt: datetime) -> None:
        """State-Machine fuer EINEN Lauf. Erwartet, dass `self._lock` gehalten
        wird. Macht nur den faelligen, noch nicht erfolgten Uebergang."""
        # T-0342: stop_fehler (User-Abbruch, Ventil-Stop fehlgeschlagen ->
        # Zustand unklar, Ventil evtl. noch offen) bleibt als Warnung sichtbar,
        # SOLANGE die Sicherung den Kanal noch aktiv meldet. Sobald Watchdog/
        # Cloud-Timer geschlossen haben (ist_aktiv False), ist der Zustand
        # geklaert -> auf benignes fehler aufloesen, damit die Per-Zone-Karte
        # die Warnung nicht ewig haelt -- ohne den echten offenen Zustand je zu
        # maskieren (offenes Ventil -> ist_aktiv True -> Warnung bleibt).
        if lauf.phase == "stop_fehler":
            if not self._sicherung.ist_aktiv(lauf.kanal):
                lauf.phase = "fehler"
                lauf.fehler = (
                    "Ventil-Stop war fehlgeschlagen; Ventil inzwischen geschlossen"
                )
                await self._entferne_persistenz(lauf.zone_id)
                logger.info("pre_soak.stop_fehler_aufgeloest", zone_id=lauf.zone_id)
            return
        if lauf.phase in _END_PHASEN:
            return
        verstrichen = (jetzt - lauf.gestartet_am).total_seconds()
        rang = _PHASE_RANG.get(lauf.phase, 0)
        # T-0437 SICHERHEIT: die Idempotenz der Haupt-Pulse haengt jetzt am
        # Zaehler statt am Phasen-Rang. Ein State, der VOR der Migration
        # geschrieben wurde (oder von einem Aufrufer ohne die neuen Felder),
        # hat phase="haupt" aber Zaehler 0 -- der Tick wuerde Puls 1 dann ein
        # ZWEITES MAL giessen. Deshalb den Zaehler aus der Phase nachziehen,
        # bevor irgendetwas ausgewertet wird. Regression:
        # `test_recover_in_haupt_ruft_kein_neues_open` hat genau das gefangen.
        if lauf.haupt_pulse_gestartet == 0 and rang >= _PHASE_RANG["haupt"]:
            lauf.haupt_pulse_gestartet = 1

        # Hauptdose faellig (verstrichen >= pause_s) und noch nicht gelaufen:
        # starten -- AUCH wenn das nominale Fenster [pause_s, pause_s+haupt_s]
        # schon ueberschritten ist (T-0344). Dieser Zweig MUSS vor dem fertig-
        # Zweig stehen, sonst verschluckt fertig eine Hauptdose, deren Fenster
        # kuerzer als der Loop-Tick ist (haupt_s < 300s) oder die ein Laptop-
        # Sleep uebersprang. Nur innerhalb der Nachzug-Toleranz starten; danach
        # ist die Vorbenetzung verpufft -> sichtbar als fehler markieren statt
        # eine veraltete Dose blind zu feuern.
        # T-0437: pro Puls statt einmalig. `faelliger_puls` liefert den
        # hoechsten Index, dessen Startzeit erreicht ist; groesser als
        # `haupt_pulse_gestartet` heisst "steht aus".
        faellig = lauf.faelliger_puls(verstrichen)
        if faellig > lauf.haupt_pulse_gestartet:
            index = lauf.haupt_pulse_gestartet + 1
            start_s = lauf.haupt_puls_start_s(index)
            grenze = start_s + lauf.haupt_puls_s + _HAUPT_NACHZUG_TOLERANZ_S
            if verstrichen <= grenze:
                if (
                    lauf.letzter_haupt_fehler_am is not None
                    and jetzt - lauf.letzter_haupt_fehler_am
                    < timedelta(seconds=_HAUPT_RETRY_INTERVAL_S)
                ):
                    return
                # T-0437: Sicherheits-Gate VOR jedem Folge-Puls. Waehrend einer
                # Soak-Pause ist das Ventil zu; der Kanal-Max-Stop im
                # Entscheidungsloop prueft dort NICHT (main.py: er verlangt
                # `ist_aktiv(kanal)` und loggt sonst `pre_soak_pause_skip`).
                # Bei einem Puls gab es ein blindes Fenster, bei drei sind es
                # zwei -- der Folge-Puls wuerde ungeprueft feuern, obwohl der
                # Sensor waehrend des Einsickerns ueber die Max-Schwelle
                # gestiegen sein kann. Der erste Puls braucht das nicht: dort
                # hat der Loop soeben entschieden.
                if index > 1 and self._puls_gate is not None:
                    try:
                        weiter, gate_grund = await self._puls_gate(
                            lauf.kanal, lauf.zone_ids_kanal,
                        )
                    except Exception:
                        # Gate-Fehler darf nicht giessen: konservativ abbrechen.
                        logger.exception(
                            "pre_soak.puls_gate_fehler", zone_id=lauf.zone_id,
                        )
                        weiter, gate_grund = False, "Puls-Gate nicht auswertbar"
                    if not weiter:
                        lauf.phase = "fertig"
                        lauf.fehler = None
                        await self._entferne_persistenz(lauf.zone_id)
                        logger.info(
                            "pre_soak.puls_gate_stopp",
                            zone_id=lauf.zone_id, kanal=lauf.kanal,
                            puls=index, von=lauf.haupt_pulse, grund=gate_grund,
                        )
                        return
                if rang < _PHASE_RANG["pause"]:
                    lauf.phase = "pause"
                erfolg = await self._sicherung.bewaessere(
                    lauf.kanal, lauf.zone_ids_kanal, lauf.haupt_puls_s,
                    lauf.ausloser,
                    lauf_gruppe=lauf.lauf_gruppe or None, phase="haupt",
                )
                if erfolg:
                    lauf.phase = "haupt"
                    lauf.haupt_pulse_gestartet = index
                    lauf.fehler = None
                    lauf.letzter_haupt_fehler_am = None
                    await self._persistiere(lauf)
                    if lauf.haupt_pulse > 1:
                        logger.info(
                            "pre_soak.haupt_puls_gestartet",
                            zone_id=lauf.zone_id, puls=index,
                            von=lauf.haupt_pulse, dauer_s=lauf.haupt_puls_s,
                        )
                else:
                    lauf.letzter_haupt_fehler_am = jetzt
                    lauf.fehler = "Hauptdose konnte nicht starten; Retry geplant"
                    await self._persistiere(lauf)
                    logger.warning(
                        "pre_soak.haupt_retry_geplant",
                        zone_id=lauf.zone_id,
                        kanal=lauf.kanal,
                        puls=index,
                        retry_in_s=_HAUPT_RETRY_INTERVAL_S,
                    )
            else:
                await self._fehler(
                    lauf,
                    f"Hauptdose-Fenster verpasst (Puls {index}/{lauf.haupt_pulse}, "
                    "Tick/Sleep > Toleranz)",
                )
            return

        # Sequenz komplett vorbei und alle Pulse liefen -> fertig.
        if verstrichen >= lauf.ende_s:
            lauf.phase = "fertig"
            await self._entferne_persistenz(lauf.zone_id)
            logger.info("pre_soak.fertig", zone_id=lauf.zone_id)
            return

        # T-0437: zwischen zwei Pulsen (Ventil zu) sichtbar als Soak-Pause.
        # Ohne das stuende die Karte auf "haupt", waehrend nichts laeuft.
        if (
            lauf.haupt_pulse_gestartet >= 1
            and lauf.haupt_pulse_gestartet < lauf.haupt_pulse
            and verstrichen >= (
                lauf.haupt_puls_start_s(lauf.haupt_pulse_gestartet)
                + lauf.haupt_puls_s
            )
        ):
            if lauf.phase != "haupt_pause":
                lauf.phase = "haupt_pause"
                await self._persistiere(lauf)
            return

        # Pause-Phase (Ventil zu): nur Phase markieren.
        if verstrichen >= lauf.pre_soak_s:
            if rang < _PHASE_RANG["pause"]:
                lauf.phase = "pause"
                await self._persistiere(lauf)
            return

        # Puls-Fenster: Puls starten, falls noch nicht.
        if rang < _PHASE_RANG["pre_soak"]:
            lauf.phase = "pre_soak"
            await self._persistiere(lauf)
            erfolg = await self._sicherung.bewaessere(
                lauf.kanal, lauf.zone_ids_kanal, lauf.pre_soak_s,
                lauf.ausloser,
                lauf_gruppe=lauf.lauf_gruppe or None, phase="pre_soak",
            )
            if not erfolg:
                await self._fehler(lauf, "Pre-Soak konnte nicht starten")

    async def recover_aus_db(self) -> int:
        """T-0116/T-0336: Laedt persistierte Sequenzen nach Restart zurueck in
        den Speicher und tickt sie (faellige Uebergaenge nachziehen).

        Returns: Anzahl wiederhergestellter (nicht abgelaufener) Sequenzen.
        """
        if self._speicher is None:
            return 0
        async with self._lock:
            states = await self._speicher.hole_pre_soak_states()
            jetzt = datetime.now()
            geladen: list[PreSoakLauf] = []
            for s in states:
                zone_id = s["zone_id"]
                if (self._erlaubte_zone_ids is not None
                        and zone_id not in self._erlaubte_zone_ids):
                    continue
                if zone_id in self._laeufe:
                    continue
                verstrichen_s = (jetzt - s["gestartet_am"]).total_seconds()
                # T-0344: KEINE eigene "abgelaufen"-Entscheidung mehr hier.
                # Frueher wurde ein State mit verstrichen >= pause_s+haupt_s
                # verworfen -- isomorph zum verschluckten Hauptdose-Bug (eine nie
                # gelaufene Hauptdose ging still verloren statt nachgezogen zu
                # werden) UND eine zweite Wahrheit ueber "fertig/verfehlt". Alle
                # erlaubten States laden; _tick_lauf unten ist die EINZIGE
                # Instanz, die nachzieht / fertig / verfehlt entscheidet.
                # T-0335 (dokumentierte Recovery-Grenze): lauf_gruppe wird nicht
                # in pre_soak_state persistiert -> fortgesetzte Phasen sind
                # ungruppiert (bewaessere() macht `or None`). Bewusst, konsistent
                # mit dem live_lauf_state-Recovery-Close.
                lauf = PreSoakLauf(
                    zone_id=zone_id, kanal=s["kanal"],
                    zone_ids_kanal=list(s["zone_ids_kanal"]),
                    pre_soak_s=s["pre_soak_s"],
                    pause_s=s["pause_s"],
                    haupt_s=s["haupt_s"],
                    gestartet_am=s["gestartet_am"],
                    phase=s["phase"],
                    # T-0343: Ausloser aus dem persistierten State -> ein Auto-
                    # Pre-Soak wird nach Restart als AUTOMATIK fortgesetzt.
                    ausloser=Ausloser(s.get("ausloser", "manuell")),
                    # T-0437: Puls-Zaehler mitnehmen. Ohne ihn faengt ein
                    # Restart mitten in der Sequenz wieder bei Puls 1 an und
                    # giesst bereits gelaufene Pulse erneut.
                    haupt_pulse=s.get("haupt_pulse", 1) or 1,
                    haupt_pause_s=s.get("haupt_pause_s", 0) or 0,
                    haupt_pulse_gestartet=s.get("haupt_pulse_gestartet", 0) or 0,
                )
                self._laeufe[zone_id] = lauf
                geladen.append(lauf)
                logger.info(
                    "pre_soak.recover",
                    zone_id=zone_id, phase=s["phase"],
                    verstrichen_s=int(verstrichen_s),
                )
            # Faellige Uebergaenge sofort nachziehen (z.B. Hauptdose aus Pause --
            # auch wenn das Fenster knapp ueberschritten ist, T-0344).
            for lauf in geladen:
                await self._tick_lauf(lauf, jetzt)
            # Beim Recovery sofort beendete Laeufe (fertig / verfehlt) nicht im
            # Speicher halten: der DB-State ist dann schon weg, der In-Memory-
            # Lauf waere ein Zombie in laufender_lauf. "Wiederhergestellt" =
            # danach noch aktiv (Pause/Haupt laeuft weiter).
            for lauf in geladen:
                if lauf.phase in _END_PHASEN:
                    self._laeufe.pop(lauf.zone_id, None)
            return sum(1 for lauf in geladen if lauf.phase not in _END_PHASEN)
