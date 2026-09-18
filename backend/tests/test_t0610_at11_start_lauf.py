"""T-0610 AT-11/AT-3: Start-Lauf von `main.ausfuehren` als Verhaltenstest.

**Warum es diesen Test gibt.** Die Verdrahtung in `ausfuehren` (Sicherungen,
Arbiter, Frostsperre, Sperrfenster, Recovery, Replay-Guard, Jobs fuer die
Loops) war bis W11 nur ueber Quelltext-/AST-Tests abgesichert: Sie lasen,
ob ein Aufruf HINGESCHRIEBEN steht, nicht ob er beim Start passiert -- und
sie brachen bei jedem verhaltensgleichen Umbau (Zerlegung AT-11).

Hier faehrt der ECHTE Start bis "Loops laufen" und protokolliert, was in
welcher Reihenfolge passiert. Gemessen wird Zustand zum Zeitpunkt des
Ereignisses (Schnappschuss), nicht die Zeilennummer eines Aufrufs.

Harmlos auch ohne die geprueften Schutzmechanismen:
- kein Netz: `GardenaClient`, `FytaClient`, uvicorn-Server, Customer-Auth und
  `warte_auf_gardena` sind Attrappen; die Client-Attrappe zaehlt jedes
  Oeffnen/Schliessen, und der Test verlangt null davon;
- kein Live-Pfad: DB in `tmp_path`, `.env` wird nicht geladen, Signal-Handler
  werden nicht installiert, `Benachrichtiger` ist eine Attrappe (kein iMessage);
- die beiden Loops, der Pre-Soak-Ticker und der Laufstatus-Heartbeat sind
  Rekorder: sie halten nur fest, dass und mit welchen Argumenten sie gestartet
  wurden, und warten auf ihren Abbruch;
- der Laufzeit-Zustand von `api_server` (T-0610 AT-8: `api_server._laufzeit`,
  den `konfiguriere_api` befuellt) ist fuer den Test ein frisches Objekt; das
  urspruengliche kommt danach unveraendert zurueck.
"""
from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

import bewaesserung
import bewaesserung.api_server as api_server
from bewaesserung import main
from bewaesserung.fyta_luecken_job import FytaLueckenJob
from bewaesserung.fyta_status_job import FytaStatusJob
from bewaesserung.hahn_arbiter import HahnArbiter
from bewaesserung.modelle import (
    Ausloser,
    AutomatikSperrfenster,
    FytaKonfig,
    FytaPflanzenKonfig,
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    VentilAktion,
    VentilEreignis,
    WetterKonfig,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.pre_soak import PreSoakManager
from bewaesserung.ventil_sicherung import VentilSicherung

# Importpfad-Beleg: der Test laeuft gegen den Checkout, in dem er liegt.
assert Path(bewaesserung.__file__).resolve().parents[2] == (
    Path(__file__).resolve().parents[1]
), f"falscher Checkout importiert: {bewaesserung.__file__}"

DSWC_A = "aaaa0610-0000-4000-8000-00000000000a"
DSWC_B = "bbbb0610-0000-4000-8000-00000000000b"
SENSOR = "dddd0610-0000-4000-8000-00000000000d"

# Die Jobs des Wartungs-Loops (T-0403). Liste = Vertrag des Umbaus.
WARTUNGS_JOBS = (
    "backup_job", "ml_drift_job", "ml_retrain_job", "ml_response_retrain_job",
    "wochen_report_job", "kalibrations_job", "skalen_mapping_fit_job",
    "k_basis_fit_job", "wirkung_fit_job", "plant_optimum_job",
    "log_rotation_job",
)

def _konfig(tmp_path: Path, *, fyta: bool) -> GesamtKonfig:
    """Zwei DSWCs, drei Ventilzonen, ein Sperrfenster, Frostsperre an
    (Default). Zwei Zonen haben je einen Alarm abgewaehlt (T-0540/T-0574)."""
    zonen = [
        ZonenKonfig(
            zone_id="bambus", name="Bambus", modus=ZonenModus.AUTOMATIK,
            ventil_kanal=1, ventil_geraet_id=DSWC_A,
            wirkungs_alarm_aktiv=False,
        ),
        ZonenKonfig(
            zone_id="hecke", name="Hecke", modus=ZonenModus.AUTOMATIK,
            ventil_kanal=2, ventil_geraet_id=DSWC_A, exklusiv=True,
        ),
        ZonenKonfig(
            zone_id="magerwiese", name="Magerwiese",
            modus=ZonenModus.MONITORING, ventil_kanal=1,
            ventil_geraet_id=DSWC_B, eingefroren_alarm_aktiv=False,
        ),
    ]
    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=zonen,
        wetter=WetterKonfig(),
        speicher=SpeicherKonfig(db_pfad=str(tmp_path / "start.db")),
        automatik_sperrfenster=[AutomatikSperrfenster(
            von=datetime(2026, 10, 25, 1, 30), bis=datetime(2026, 10, 25, 3, 30),
            grund="Zeitumstellung",
        )],
        fyta=FytaKonfig(pflanzen=[
            FytaPflanzenKonfig(fyta_id=1, zone_id="bambus", name="Bambus"),
        ]) if fyta else None,
    )
    konfig.ml_bewaesserungs_response.aktiv = False
    return konfig


class _GardenaAttrappe:
    """GardenaClient ohne Netz. Zaehlt Hardware-Kommandos (Soll: keins) und
    haelt die registrierten Callbacks fest."""

    _VENTIL_OFFEN_STATES = {
        "OPEN", "OPENING", "MANUAL_WATERING", "SCHEDULED_WATERING",
    }

    def __init__(self, protokoll: list, ws_fehler: Exception | None = None):
        self._protokoll = protokoll
        self._ws_fehler = ws_fehler
        self.location_id = "cccc0610-0000-4000-8000-000000000011"
        self.hardware_kommandos: list[tuple] = []
        self.ventil_callback = None
        self.sensor_callback = None
        self.reconnect_sync = None
        self.eigener_lauf_pruefer = None
        self.eigener_lauf_pruefer_zu = None
        self.getrennt = False

    def hole_geraete(self) -> dict[str, dict]:
        return {
            DSWC_A: {"name": "Strang A", "typ": "SMART_IRRIGATION_CONTROL"},
            DSWC_B: {"name": "Strang B", "typ": "SMART_IRRIGATION_CONTROL"},
            SENSOR: {"name": "Bambus", "typ": "SOIL_SENSOR"},
        }

    def registriere_geraet_zone(self, geraet_id, zone_id) -> None:
        pass

    def registriere_zone_namen(self, mapping) -> None:
        pass

    def registriere_sensor_callback(self, cb) -> None:
        self.sensor_callback = cb

    def registriere_ventil_callback(self, cb) -> None:
        self.ventil_callback = cb

    def setze_reconnect_sync_callback(self, cb) -> None:
        self.reconnect_sync = cb

    def setze_eigener_lauf_pruefer(self, pruefer, pruefer_zu=None) -> None:
        self._protokoll.append(("replay_guard", None))
        self.eigener_lauf_pruefer = pruefer
        self.eigener_lauf_pruefer_zu = pruefer_zu

    def baue_kanal_zu_valve_id(self, geraet_id, zuordnung) -> dict[int, str]:
        return {kanal: f"{geraet_id}:{kanal}" for kanal, *_ in zuordnung}

    def offene_valves(self) -> dict[str, dict]:
        return {}

    async def ventil_oeffnen(self, *a, **kw) -> None:
        self.hardware_kommandos.append(("oeffnen", a, kw))

    async def ventil_schliessen(self, *a, **kw) -> None:
        self.hardware_kommandos.append(("schliessen", a, kw))

    async def starte_websocket(self) -> None:
        self._protokoll.append(("ws_start", None))
        if self._ws_fehler is not None:
            raise self._ws_fehler
        await asyncio.Event().wait()

    async def trennen(self) -> None:
        self.getrennt = True


class _FytaAttrappe:
    def __init__(self, konfig, sperre_pfad=None):
        self.gestoppt = False

    def registriere_callback(self, cb) -> None:
        pass

    async def initialisiere_dedup_aus_db(self, speicher) -> None:
        pass

    async def _stelle_token_sicher(self) -> None:
        pass

    async def starte_polling(self) -> None:
        await asyncio.Event().wait()

    def stoppe(self) -> None:
        self.gestoppt = True


class _ServerAttrappe:
    should_exit = False

    def __init__(self, config):
        pass

    async def serve(self) -> None:
        while not self.should_exit:
            await asyncio.sleep(0.005)


class _ProtokollState(type(api_server.app.state)):
    """`app.state`, das das Ende der Startphase protokolliert."""

    def __setattr__(self, name, wert):
        melder = self.__dict__.get("_bei_startphase")
        if name == "gardena_startphase" and melder is not None:
            melder(wert)
        super().__setattr__(name, wert)


@dataclass
class StartLauf:
    """Was der Start hinterlassen hat, waehrend die Loops laufen."""

    protokoll: list
    client: _GardenaAttrappe
    konfig: GesamtKonfig
    api_aufrufe: list[dict] = field(default_factory=list)
    loop_kwargs: dict[str, dict] = field(default_factory=dict)
    loop_ende: dict[str, str] = field(default_factory=dict)
    sicherungen: dict[str, VentilSicherung] = field(default_factory=dict)
    logs: list[dict] = field(default_factory=list)
    # Gate W11 Befund 4: Tasks, die beim ersten speicher.schliessen() noch
    # liefen (ohne die Tasks des Testtreibers), und wie ausfuehren endete.
    lebend_bei_schliessen: list[str] | None = None
    eigene_tasks: set = field(default_factory=set)
    ende: object = None

    def ereignisse(self, art: str) -> list[tuple]:
        return [e for e in self.protokoll if e[0] == art]

    def index(self, art: str) -> int:
        for i, e in enumerate(self.protokoll):
            if e[0] == art:
                return i
        raise AssertionError(f"Ereignis {art!r} nie protokolliert: {self.namen()}")

    def namen(self) -> list[str]:
        return [e[0] for e in self.protokoll]

    @property
    def motor(self):
        return self.loop_kwargs["entscheidungsloop"]["motor"]

    @property
    def arbiter(self) -> HahnArbiter:
        arbiter = {id(s._hahn_arbiter): s._hahn_arbiter for s in self.sicherungen.values()}
        assert len(arbiter) == 1, "nicht genau ein Arbiter an den Sicherungen"
        return next(iter(arbiter.values()))


def _schnappschuss(sicherungen: dict) -> dict[str, dict]:
    return {
        gid: {
            "frost": s._frost_pruefer,
            "sperrfenster": list(s._automatik_sperrfenster),
            "arbiter": s._hahn_arbiter,
        }
        for gid, s in sicherungen.items()
    }


@asynccontextmanager
async def start_lauf(tmp_path, monkeypatch, *, fyta: bool = False,
                     ws_fehler: Exception | None = None, ende: str = "stop",
                     backfill_haengt: bool = False):
    """Faehrt `main.ausfuehren` bis beide Loops laufen, gibt den Zustand
    heraus und beendet danach regulaer ueber das Stop-Event.

    `ende="cancel"` bricht stattdessen den Haupttask ab (Gate W11 Befund 4);
    `ende="startfehler"` laesst `_baue_ueberwachung` scheitern, nachdem API,
    WebSocket und FYTA schon laufen, und gibt den Zustand nach dem Ende
    heraus. `backfill_haengt` laesst die FYTA-Lueckenfuellung bis zu ihrem
    Abbruch laufen statt sofort zurueckzukehren."""
    protokoll: list[tuple] = []
    konfig = _konfig(tmp_path, fyta=fyta)
    client = _GardenaAttrappe(protokoll, ws_fehler=ws_fehler)
    lauf = StartLauf(protokoll=protokoll, client=client, konfig=konfig)
    sicherungen = lauf.sicherungen
    beide_loops = asyncio.Event()
    stop_ref: dict[str, asyncio.Event] = {}

    monkeypatch.setattr(api_server, "_laufzeit", api_server.Laufzeit())
    state = _ProtokollState()
    state.__dict__["_bei_startphase"] = lambda wert: protokoll.append(
        ("startphase", wert, _schnappschuss(sicherungen)),
    )
    monkeypatch.setattr(api_server.app, "state", state)

    monkeypatch.setattr(main, "lade_konfig", lambda pfad: konfig)
    monkeypatch.setattr(main.dotenv, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setattr(main, "_konfiguriere_logging", lambda *a: None)
    monkeypatch.setattr(main, "GardenaClient", lambda **kw: client)
    monkeypatch.setattr(main, "FytaClient", _FytaAttrappe)
    monkeypatch.setattr(main, "Benachrichtiger", MagicMock)
    monkeypatch.setattr(main, "baue_gardena_customer_auth", lambda *a: None)
    monkeypatch.setattr(main.uvicorn, "Server", _ServerAttrappe)
    monkeypatch.setattr(
        asyncio.get_running_loop(), "add_signal_handler", lambda *a: None,
    )

    from bewaesserung import fyta_backfill, start_monitoring
    from bewaesserung.ml import vorhersage

    async def _warte_auf_gardena(*a, **kw):
        protokoll.append(("warte_auf_gardena", None))

    async def _kein_backfill(*a, **kw):
        if not backfill_haengt:
            return 0
        protokoll.append(("fyta_backfill_start", None))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            lauf.loop_ende["fyta_backfill"] = "abgebrochen"
            raise

    ml = MagicMock()
    ml.lade_modelle.return_value = False
    monkeypatch.setattr(vorhersage, "MLVorhersageService", lambda **kw: ml)
    monkeypatch.setattr(start_monitoring, "warte_auf_gardena", _warte_auf_gardena)
    monkeypatch.setattr(fyta_backfill, "backfill_lueckenfuellung", _kein_backfill)

    # Der 5-s-Vorlauf vor den Loops ("initiale Sensordaten") ohne Wartezeit.
    echtes_sleep = asyncio.sleep

    async def _schnelles_sleep(delay, *a, **kw):
        return await echtes_sleep(0 if delay >= 1 else delay, *a, **kw)

    monkeypatch.setattr(asyncio, "sleep", _schnelles_sleep)

    class _SpionSicherung(VentilSicherung):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            sicherungen[self._ventil_geraet_id] = self

        async def startup_check(self):
            protokoll.append(("startup_check", self._ventil_geraet_id,
                              _schnappschuss({self._ventil_geraet_id: self})))
            await super().startup_check()

        async def recover_aus_db(self):
            protokoll.append(("recover", self._ventil_geraet_id,
                              _schnappschuss({self._ventil_geraet_id: self})))
            try:
                return await super().recover_aus_db()
            finally:
                protokoll.append(("recover_fertig", self._ventil_geraet_id))

    monkeypatch.setattr(main, "VentilSicherung", _SpionSicherung)

    speicher_instanzen: list = []

    class _Speicher(main.Speicher):
        """Echter Speicher; nur gemerkt, damit ein im Setup abgebrochener
        Start die DB nicht offen haelt (der aiosqlite-Thread liesse sonst
        den Testprozess haengen statt rot werden)."""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            speicher_instanzen.append(self)

        async def schliessen(self):
            if lauf.lebend_bei_schliessen is None:
                ich = asyncio.current_task()
                lauf.lebend_bei_schliessen = sorted(
                    t.get_coro().__qualname__ for t in asyncio.all_tasks()
                    if not t.done() and t is not ich
                    and t not in lauf.eigene_tasks
                )
            await super().schliessen()

    monkeypatch.setattr(main, "Speicher", _Speicher)

    echte_pre_soak_recovery = PreSoakManager.recover_aus_db

    async def _pre_soak_recovery(self):
        protokoll.append(("pre_soak_recover", None, _schnappschuss(sicherungen)))
        try:
            return await echte_pre_soak_recovery(self)
        finally:
            protokoll.append(("pre_soak_recover_fertig", None))

    monkeypatch.setattr(PreSoakManager, "recover_aus_db", _pre_soak_recovery)

    echtes_konfiguriere_api = main.konfiguriere_api

    def _konfiguriere_api(*a, **kw):
        lauf.api_aufrufe.append(kw)
        protokoll.append(("konfiguriere_api", sorted(kw)))
        return echtes_konfiguriere_api(*a, **kw)

    monkeypatch.setattr(main, "konfiguriere_api", _konfiguriere_api)

    def _rekorder(name: str, echt):
        signatur = inspect.signature(echt)

        async def _laeuft(*a, **kw):
            argumente = signatur.bind(*a, **kw).arguments
            lauf.loop_kwargs[name] = argumente
            protokoll.append((name, None, _schnappschuss(sicherungen)))
            if "stop_event" in argumente:
                stop_ref.setdefault("stop", argumente["stop_event"])
            if {"entscheidungsloop", "wartungsloop"} <= set(lauf.loop_kwargs):
                beide_loops.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                lauf.loop_ende[name] = "abgebrochen"
                raise
        return _laeuft

    if ende == "startfehler":
        def _kaputte_ueberwachung(*a, **kw):
            raise RuntimeError("INJIZIERT_UEBERWACHUNG")

        monkeypatch.setattr(main, "_baue_ueberwachung", _kaputte_ueberwachung)

    for name, attr in (
        ("entscheidungsloop", "_entscheidungsloop"),
        ("wartungsloop", "_wartungs_loop"),
        ("pre_soak_tick", "_pre_soak_tick_loop"),
        ("laufstatus", "_laufstatus_heartbeat"),
    ):
        monkeypatch.setattr(main, attr, _rekorder(name, getattr(main, attr)))

    with capture_logs() as logs:
        lauf.logs = logs
        try:
            async with _fahre(lauf, beide_loops, stop_ref, ende) as gestartet:
                yield gestartet
        finally:
            for sp in speicher_instanzen:
                if not sp._geschlossen:
                    await sp.schliessen()
    assert client.getrennt, "Aufraeumen hat den Client nicht getrennt"
    assert client.hardware_kommandos == [], client.hardware_kommandos


@asynccontextmanager
async def _fahre(lauf: StartLauf, beide_loops: asyncio.Event, stop_ref: dict,
                 ende: str = "stop"):
    lauf.eigene_tasks.add(asyncio.current_task())
    task = asyncio.create_task(main.ausfuehren())
    try:
        warte = asyncio.create_task(beide_loops.wait())
        lauf.eigene_tasks.add(warte)
        await asyncio.wait({task, warte}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if ende == "startfehler":
            warte.cancel()
            assert task.done(), f"Start lief trotz Startfehler weiter: {lauf.namen()}"
            lauf.ende = (await asyncio.gather(task, return_exceptions=True))[0]
            yield lauf
            return
        if task.done():
            warte.cancel()
            task.result()  # Startfehler sichtbar machen
            raise AssertionError(f"ausfuehren endete vor den Loops: {lauf.namen()}")
        assert beide_loops.is_set(), f"Loops nie gestartet: {lauf.namen()}"
        yield lauf
        if ende == "cancel":
            task.cancel()
            erg = await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), timeout=10,
            )
            lauf.ende = erg[0]
        else:
            stop_ref["stop"].set()
            await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


# --- Der Start-Smoke-Test --------------------------------------------------


async def test_start_laeuft_bis_zu_den_loops_in_sicherer_reihenfolge(tmp_path, monkeypatch):
    """Der Start erreicht die Loops (faengt UnboundLocalError/NameError im
    Setup, T-0416), und die Sicherheits-Verdrahtung steht in der Reihenfolge,
    die den ersten Tick schuetzt:

    1. Frostsperre (T-0611) und Automatik-Sperrfenster (T-0593/T-0616) haengen
       an jeder Sicherung, BEVOR `startup_check`/`recover_aus_db` laufen.
    2. Jede Sicherung kennt den einen Hahn-Arbiter (T-0536/T-0616), bevor
       irgendetwas oeffnen kann: Pre-Soak-Recovery, Ende der API-Startphase,
       Pre-Soak-Ticker, Entscheidungsloop.
    3. Die Recovery (Sicherungen und Pre-Soak) laeuft vor Ticker und Loop.
    4. Die API bekommt die Sicherungen, bevor die Startphase endet (T-0617).
    5. Der Replay-Guard ist gesetzt, bevor die WebSocket startet (T-0547).
    """
    async with start_lauf(tmp_path, monkeypatch) as lauf:
        motor = lauf.motor
        assert set(lauf.sicherungen) == {DSWC_A, DSWC_B}
        assert motor.frost_pruefer is not None  # sonst prueft (1) nichts

        # (1) Frost/Sperrfenster vor jeder Recovery-Stufe.
        for art in ("startup_check", "recover"):
            gesehen = {e[1] for e in lauf.ereignisse(art)}
            assert gesehen == {DSWC_A, DSWC_B}, (art, gesehen)
            for _, gid, schnapp in lauf.ereignisse(art):
                assert schnapp[gid]["frost"] is motor.frost_pruefer, (art, gid)
                assert schnapp[gid]["sperrfenster"] == lauf.konfig.automatik_sperrfenster, (art, gid)

        # (2) Arbiter an allen Sicherungen vor jedem moeglichen Oeffnen.
        arbiter = lauf.arbiter
        assert isinstance(arbiter, HahnArbiter)
        assert set(arbiter._sicherungen) == {DSWC_A, DSWC_B}
        assert lauf.ereignisse("pre_soak_recover"), "Pre-Soak-Recovery lief nicht"
        for art in ("pre_soak_recover", "pre_soak_tick", "entscheidungsloop"):
            for e in lauf.ereignisse(art):
                assert all(s["arbiter"] is arbiter for s in e[2].values()), art
        startphase = [e for e in lauf.ereignisse("startphase")]
        assert [e[1] for e in startphase] == [True, False], startphase
        assert all(s["arbiter"] is arbiter for s in startphase[1][2].values())
        ende_startphase = lauf.protokoll.index(startphase[1])

        # (3) Recovery ABGESCHLOSSEN vor Ticker und Loop (ein Task, der
        # waehrend einer laufenden Recovery startet, liefe parallel zu ihr).
        # Jede begonnene Recovery muss auch beendet sein -- sonst laeuft sie
        # noch, waehrend die Loops schon arbeiten (und fehlt hier im Protokoll).
        assert len(lauf.ereignisse("recover_fertig")) == 2
        assert len(lauf.ereignisse("pre_soak_recover_fertig")) == len(
            lauf.ereignisse("pre_soak_recover")
        ), "Pre-Soak-Recovery bei Loop-Start noch nicht abgeschlossen"
        letzte_recovery = max(
            i for i, e in enumerate(lauf.protokoll)
            if e[0] in ("recover_fertig", "pre_soak_recover_fertig")
        )
        assert letzte_recovery < lauf.index("pre_soak_tick")
        assert letzte_recovery < lauf.index("entscheidungsloop")

        # (4) API: erst ohne, dann mit den Sicherungen, und zwar vor dem Ende
        # der Startphase.
        assert lauf.protokoll.index(startphase[0]) < ende_startphase
        mit_sicherungen = [
            i for i, e in enumerate(lauf.protokoll)
            if e[0] == "konfiguriere_api" and "ventil_sicherungen" in e[1]
        ]
        assert mit_sicherungen and mit_sicherungen[-1] < ende_startphase
        assert lauf.api_aufrufe[-1]["ventil_sicherungen"] == lauf.sicherungen
        assert api_server._laufzeit.ventil_sicherungen == lauf.sicherungen
        assert ende_startphase < lauf.index("entscheidungsloop")

        # (5) Replay-Guard vor der WebSocket.
        assert lauf.index("replay_guard") < lauf.index("ws_start")

    # Regulaeres Ende: beide Loops wurden abgebrochen (T-0403: der
    # Wartungs-Task steht in `tasks_zu_beenden`).
    assert lauf.loop_ende == {
        "entscheidungsloop": "abgebrochen", "wartungsloop": "abgebrochen",
        "pre_soak_tick": "abgebrochen", "laufstatus": "abgebrochen",
    }
    # Kein verschluckter Fehler im Start (jeder Schritt ist best effort und
    # loggt nur -- ein stiller Ausfall saehe sonst wie ein gruener Start aus).
    fehler = [e for e in lauf.logs if e["log_level"] in ("error", "critical")]
    assert fehler == [], fehler


# --- Ersatz fuer die frueheren Quelltext-/AST-Tests auf `ausfuehren` -------


async def test_loops_bekommen_ihre_jobs(tmp_path, monkeypatch):
    """T-0403/T-0416/T-0504/T-0527: jeder Wartungsjob kommt im Wartungs-Loop
    an und keiner im Entscheidungs-Loop (sonst taktet die Kanalpruefung wieder
    hinter einem 24-min-Retrain her). Ohne FYTA-Konfiguration bekommt der
    Entscheidungs-Loop die FYTA-Jobs ausdruecklich als None (die Vorbelegung
    vor dem `if konfig.fyta`-Zweig; ohne sie endete der Start mit
    UnboundLocalError). Ersetzt die Quelltext-Tests in
    test_main_loop_verdrahtung.py, test_fyta_luecken_job.py und
    test_fyta_status_job.py (Teil "main")."""
    async with start_lauf(tmp_path, monkeypatch) as lauf:
        entscheidung = lauf.loop_kwargs["entscheidungsloop"]
        wartung = lauf.loop_kwargs["wartungsloop"]
        for job in WARTUNGS_JOBS:
            assert wartung.get(job) is not None, f"{job} fehlt im Wartungs-Loop"
            assert job not in entscheidung, f"{job} wieder im Entscheidungs-Loop"
        assert entscheidung["fyta_luecken_job"] is None
        assert entscheidung["fyta_status_job"] is None
        assert entscheidung["fyta_sprung_detektor"] is not None


async def test_fyta_jobs_kommen_mit_dem_fyta_client_im_loop_an(tmp_path, monkeypatch):
    """Mit FYTA-Konfiguration: der Loop bekommt die echten Jobs, gebaut auf
    dem einen FytaClient des Dienstes (T-0504, T-0527)."""
    async with start_lauf(tmp_path, monkeypatch, fyta=True) as lauf:
        entscheidung = lauf.loop_kwargs["entscheidungsloop"]
        luecken = entscheidung["fyta_luecken_job"]
        status = entscheidung["fyta_status_job"]
        assert isinstance(luecken, FytaLueckenJob)
        assert isinstance(status, FytaStatusJob)
        assert isinstance(luecken._fyta_client, _FytaAttrappe)
        assert status._fyta_client is luecken._fyta_client


async def test_watchdog_und_orphan_scan_kennen_die_sicherungen(tmp_path, monkeypatch):
    """T-0590 (HW-9, Trigger I) und T-0595 (HW-6): ohne die Sicherungen ist
    der Close-Retry-Push ein stiller No-Op, und der Orphan-Scan schliesst
    Laeufe, die eine Sicherung noch fuehrt. Der Watchdog liest ausserdem den
    Arbiter (T-0537). Ersetzt die AST-Tests in test_t0590 (Test 11) und
    test_t0595."""
    async with start_lauf(tmp_path, monkeypatch) as lauf:
        kw = lauf.loop_kwargs["entscheidungsloop"]
        watchdog = kw["watchdog_job"]
        assert watchdog is not None
        assert watchdog._ventil_sicherungen == lauf.sicherungen
        assert watchdog._hahn_arbiter is lauf.arbiter
        assert kw["ventil_sicherungen"] == lauf.sicherungen
        assert kw["orphan_close_job"]._ventil_sicherungen is kw["ventil_sicherungen"]


async def test_replay_guard_fragt_alle_sicherungen_offen_und_zu(tmp_path, monkeypatch):
    """T-0547/T-0572/T-0595 (VK-1): der Guard bekommt zwei Praedikate, OFFEN
    mit Zeitschranke (`lauf_plausibel_offen`) und ZU mit Zustand
    (`haelt_ventil_offen`), und beide fragen JEDE Sicherung. Ersetzt den
    AST-Test in test_t0595 (der verlangte `verdrahte_replay_guard` statt des
    alten Einzel-Pruefers)."""
    async with start_lauf(tmp_path, monkeypatch) as lauf:
        offen, zu = lauf.client.eigener_lauf_pruefer, lauf.client.eigener_lauf_pruefer_zu
        assert offen is not None and zu is not None
        valve = f"{DSWC_B}:1"
        assert offen(valve) is False and zu(valve) is False
        sicherung_b = lauf.sicherungen[DSWC_B]
        # Nur die zweite Sicherung fuehrt den Lauf: je ein Praedikat wahr.
        monkeypatch.setattr(sicherung_b, "lauf_plausibel_offen", lambda v: v == valve)
        assert offen(valve) is True and zu(valve) is False
        monkeypatch.setattr(sicherung_b, "haelt_ventil_offen", lambda v: v == valve)
        assert zu(valve) is True


async def test_ws_callback_schreibt_externe_laeufe_ueber_den_geschuetzten_pfad(
    tmp_path, monkeypatch,
):
    """T-0604 (PERS-5): ein App-/Zeitplan-Lauf, dessen SCHLIESSEN zweimal
    ankommt (DHS-Nachtrag, dann spaetes WS-Event), zaehlt einmal. Der
    WS-Callback ist eine Closure in `ausfuehren`; hier wird er so aufgerufen,
    wie die WebSocket ihn ruft. Ersetzt den AST-Test in test_t0604."""
    async with start_lauf(tmp_path, monkeypatch) as lauf:
        speicher = api_server._laufzeit.speicher
        valve = f"{DSWC_A}:1"
        start = datetime.now() - timedelta(minutes=20)
        await lauf.client.ventil_callback(VentilEreignis(
            zeitstempel=start, zone_id="bambus", ventil_id=valve,
            aktion=VentilAktion.OEFFNEN, ausloser=Ausloser.MANUELL,
        ))
        # Erstes SCHLIESSEN nach 10 min, dann ein spaetes zweites Event fuer
        # denselben Lauf (Ende 15 min, Dauer 900 s): sein Laufintervall
        # enthaelt das erste -- Waechter 1 von `speichere_externes_ventil_ereignis`.
        for minuten in (10, 15):
            await lauf.client.ventil_callback(VentilEreignis(
                zeitstempel=start + timedelta(minutes=minuten), zone_id="bambus",
                ventil_id=valve, aktion=VentilAktion.SCHLIESSEN,
                dauer_sekunden=minuten * 60, ausloser=Ausloser.MANUELL,
            ))
        ereignisse = await speicher.hole_ventil_ereignisse("bambus")
        schliessen = [e.dauer_sekunden for e in ereignisse
                      if e.aktion == VentilAktion.SCHLIESSEN]
        assert schliessen == [600], schliessen
        assert [e.aktion for e in ereignisse].count(VentilAktion.OEFFNEN) == 1


async def test_arbiter_bekommt_die_pruefer_des_motors(tmp_path, monkeypatch):
    """T-0618 (Pflanzenwohl-Vorrang) und T-0622 (Ausnahme bei Vorhersage
    kritisch): ohne die Pruefer faellt der Arbiter still auf das Verhalten
    davor zurueck. Ersetzt die AST-Tests in test_t0618 und test_t0622."""
    async with start_lauf(tmp_path, monkeypatch) as lauf:
        arbiter, motor = lauf.arbiter, lauf.motor
        assert arbiter._kritisch_pruefer == motor.kanal_kritisch
        assert arbiter._fenster_kritisch_pruefer == motor.fenster_zone_wird_kritisch


async def test_leck_detektor_bekommt_die_alarm_abwahl_der_zonen(tmp_path, monkeypatch):
    """T-0540/T-0574: das Opt-out darf nicht geparst, aber nie uebergeben
    werden ([[fehlerpattern_detektor_ohne_konsument]]). T-0545: der Dienst
    uebergibt kein `zone_quellen`-Mapping -- die Aufloesung kommt aus der
    geraet_id. Ersetzt die Quelltext-Tests in test_t0540, test_t0574 und
    test_t0545 (LeckDetektor wird nur in `ausfuehren` gebaut)."""
    async with start_lauf(tmp_path, monkeypatch) as lauf:
        detektor = lauf.loop_kwargs["entscheidungsloop"]["leck_detektor"]
        assert detektor._wirkungs_alarm_aus == frozenset({"bambus"})
        assert detektor._eingefroren_alarm_aus == frozenset({"magerwiese"})
        assert detektor._zone_quellen == {}


async def test_gestorbene_websocket_meldet_sich_laut(tmp_path, monkeypatch):
    """T-0404: endet der WS-Task mit einer Ausnahme, muss das als `critical`
    im Log stehen (Realfall 10.07.: Log endete ohne Traceback). Ersetzt den
    Quelltext-Test in test_main_loop_verdrahtung.py."""
    async with start_lauf(
        tmp_path, monkeypatch, ws_fehler=RuntimeError("DNS weg (Attrappe)"),
    ) as lauf:
        for _ in range(50):
            if any(e["event"] == "gardena.websocket_task_gestorben" for e in lauf.logs):
                break
            await asyncio.sleep(0.01)
        laut = [e for e in lauf.logs if e["event"] == "gardena.websocket_task_gestorben"]
        assert len(laut) == 1
        assert laut[0]["log_level"] == "critical"
        assert laut[0]["typ"] == "RuntimeError"


# --- Beenden: nichts laeuft mehr, wenn der Speicher schliesst --------------


@pytest.mark.parametrize("ende", ["stop", "cancel", "startfehler"])
async def test_beenden_stoppt_alle_tasks_bevor_der_speicher_schliesst(
    tmp_path, monkeypatch, ende,
):
    """Gate W11 Befund 4: Ein Abbruch des Haupttasks (CancelledError aus dem
    Warten auf das Stop-Event) uebersprang das geordnete Beenden; der
    Speicher schloss, waehrend Loops, WebSocket und FYTA noch liefen (erst
    asyncio.run raeumte sie danach ab). Dasselbe bei einem Startfehler, nachdem
    API, WebSocket und FYTA schon liefen. Die FYTA-Lueckenfuellung hielt
    niemand und brach auch das regulaere Beenden nicht ab.

    Gemessen: welche Tasks beim ersten `speicher.schliessen()` noch laufen."""
    async with start_lauf(
        tmp_path, monkeypatch, fyta=True, backfill_haengt=True, ende=ende,
    ) as lauf:
        assert lauf.ereignisse("fyta_backfill_start"), lauf.namen()

    assert lauf.lebend_bei_schliessen == [], lauf.lebend_bei_schliessen
    assert lauf.loop_ende.get("fyta_backfill") == "abgebrochen", lauf.loop_ende
    if ende == "startfehler":
        assert isinstance(lauf.ende, RuntimeError), lauf.ende
        assert "INJIZIERT_UEBERWACHUNG" in str(lauf.ende)
    else:
        assert {k: v for k, v in lauf.loop_ende.items() if k != "fyta_backfill"} == {
            "entscheidungsloop": "abgebrochen", "wartungsloop": "abgebrochen",
            "pre_soak_tick": "abgebrochen", "laufstatus": "abgebrochen",
        }
        if ende == "cancel":
            assert isinstance(lauf.ende, asyncio.CancelledError), lauf.ende
