"""T-0589: Notfall-Stopp beendet laufende Pre-Soak-Sequenzen.

Befund (Gesamtreview 25.09., HW-1/AT-1/FE-7): `/api/notfall-stopp` schloss nur
das gerade offene Ventil. Der PreSoakManager-Lauf blieb in `pre_soak` bzw.
`pause` stehen, und der naechste Tick oeffnete zur Sollzeit die Hauptdose von
selbst wieder. In der Pause-Phase bewirkte der Knopf gar nichts; nach einem
Neustart nahm `recover_aus_db` den Lauf wieder auf.

Aufbau (Wirkung statt Verkabelung): echte `VentilSicherung` mit Attrappen-
Client (`ClientAttrappe` aus test_ventil_sicherung.py -- kein Netz, keine
Hardware), echter `Speicher` auf einer temporaeren DB, echte PreSoakManager aus
`konfiguriere_api` und der echte Routen-Handler `api_server.notfall_stopp`.
Gemessen wird, was am Ende beim Client ankommt: ein weiteres
`ventil_oeffnen` nach dem Notfall-Stopp ist der Fehler.

Der Handler wird direkt im Test-Eventloop aufgerufen statt ueber TestClient:
TestClient faehrt die App in einem eigenen Loop, die Watchdog-Timer und Locks
der echten VentilSicherung wuerden dann ueber zwei Loops verteilt. Der
HTTP-Vertrag (Status 200, JSON-Form) ist in test_api_giessen.py abgedeckt.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import bewaesserung.api_server as api_server
from bewaesserung.api_server import konfiguriere_api
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    StandortKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
    Ausloser,
)
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import VentilSicherung
from test_ventil_sicherung import ClientAttrappe

DSWC = "dswc-1"
GERAET_ID = "ventil-001"
KANAL_BAMBUS = 1
KANAL_HECKE = 3
BAMBUS_ZONEN = ["bambuswald", "bambuswald_yogaraum"]


def _konfig() -> GesamtKonfig:
    # T-0594: Die Sequenzen hier sind AUTOMATIK-Sequenzen; die gibt es im
    # Betrieb nur bei scharfer Automatik. Seit T-0594 beendet der Manager eine
    # Auto-Sequenz, sobald die Scharfschaltung fehlt -- die Konfig muss also
    # scharf sein, damit diese Tests den Notfall-Pfad pruefen und nicht das
    # Scharf-Gate.
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        ventilsteuerung_aktiv=True,
        zonen=[
            ZonenKonfig(
                zone_id="bambuswald", name="Bambus", ventil_kanal=KANAL_BAMBUS,
                auto_loop_opt_in=True,
            ),
            ZonenKonfig(
                zone_id="bambuswald_yogaraum", name="Yogaraum",
                ventil_kanal=KANAL_BAMBUS, auto_loop_opt_in=True,
            ),
            ZonenKonfig(zone_id="hecke", name="Hecke", ventil_kanal=KANAL_HECKE),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten", wetter_standort="o",
                zonen=["bambuswald", "bambuswald_yogaraum", "hecke"],
            ),
        ],
    )


def _dienst(speicher: Speicher, client: ClientAttrappe):
    """Baut Sicherung + Manager wie der Dienst (konfiguriere_api)."""
    sicherung = VentilSicherung(
        client=client,  # type: ignore[arg-type]
        speicher=speicher,
        ventil_geraet_id=GERAET_ID,
    )
    konfiguriere_api(
        speicher, _konfig(), MagicMock(), MagicMock(),
        ventil_sicherungen={DSWC: sicherung},
    )
    return sicherung, api_server._laufzeit.pre_soak_managers[DSWC]


async def _starte_sequenz(mgr) -> datetime:
    """Automatik-Sequenz wie im Regelbetrieb: 5 min Puls, Hauptdose ab 30 min."""
    ok, fehler = await mgr.starte(
        zone_id="bambuswald", kanal=KANAL_BAMBUS, zone_ids_kanal=BAMBUS_ZONEN,
        pre_soak_min=5, pause_min=30, haupt_min=60,
        ausloser=Ausloser.AUTOMATIK,
    )
    assert ok, fehler
    return mgr.laufender_lauf("bambuswald").gestartet_am


async def _in_pause_bringen(sicherung, mgr, t0: datetime) -> None:
    """Puls-Ende wie in echt (Cloud schliesst nach 5 min), dann Pause-Tick."""
    assert await sicherung.stoppe(KANAL_BAMBUS, Ausloser.AUTOMATIK)
    await mgr.tick(t0 + timedelta(minutes=10))
    assert mgr.laufender_lauf("bambuswald").phase == "pause"
    assert not sicherung.ist_aktiv(KANAL_BAMBUS)


@pytest.fixture
async def speicher(tmp_path):
    sp = Speicher(str(tmp_path / "t0589.db"))
    await sp.verbinden()
    try:
        yield sp
    finally:
        await sp.schliessen()


# --- (a)+(b) Notfall in Puls- und Pause-Phase: keine Hauptdose danach ---

@pytest.mark.parametrize("phase", ["pre_soak", "pause"])
async def test_notfall_stopp_verhindert_hauptdose(speicher, phase):
    """Nach dem Notfall-Stopp darf der Tick nach Ablauf der Pause kein
    weiteres `ventil_oeffnen` erzeugen -- weder aus der Puls- noch aus der
    Pause-Phase heraus. Die Pause-Phase ist eigens drin, weil dort kein
    Ventil offen ist: ein Fix, der nur Sequenzen mit offenem Ventil beendet,
    faellt hier auf."""
    client = ClientAttrappe()
    sicherung, mgr = _dienst(speicher, client)
    t0 = await _starte_sequenz(mgr)
    if phase == "pause":
        await _in_pause_bringen(sicherung, mgr, t0)
    else:
        assert sicherung.ist_aktiv(KANAL_BAMBUS)
    oeffnen_vorher = len(client.oeffnen_aufrufe)
    assert oeffnen_vorher == 1  # nur der Puls

    antwort = await api_server.notfall_stopp()

    # Hauptdose waere bei t0+30 min faellig.
    await mgr.tick(t0 + timedelta(minutes=31))
    assert len(client.oeffnen_aufrufe) == oeffnen_vorher, (
        f"Hauptdose nach Notfall-Stopp geoeffnet: {client.oeffnen_aufrufe}"
    )
    assert not sicherung.ist_aktiv(KANAL_BAMBUS)
    lauf = mgr.laufender_lauf("bambuswald")
    assert lauf.phase == "fehler"
    assert lauf.fehler == "Notfall-Stopp: Sequenz abgebrochen"
    assert not mgr.ist_aktiv("bambuswald")
    assert antwort["ok"] is True
    assert antwort["pre_soak_abgebrochen"] == ["bambuswald"]
    assert antwort["geschlossen"] == (1 if phase == "pre_soak" else 0)


# --- (c) Neustart nach Notfall nimmt nichts wieder auf ---

async def test_neustart_nach_notfall_nimmt_sequenz_nicht_wieder_auf(speicher):
    """Nach dem Notfall-Stopp liegt kein Pre-Soak-State mehr in der DB; ein
    frisch gebauter Dienst auf derselben DB startet keine Hauptdose."""
    client = ClientAttrappe()
    sicherung, mgr = _dienst(speicher, client)
    t0 = await _starte_sequenz(mgr)
    await _in_pause_bringen(sicherung, mgr, t0)
    assert len(await speicher.hole_pre_soak_states()) == 1  # Vorbedingung

    await api_server.notfall_stopp()

    # "Neustart": neue Sicherung, neuer Client, neue Manager, selbe DB.
    client_neu = ClientAttrappe()
    _, mgr_neu = _dienst(speicher, client_neu)
    n = await mgr_neu.recover_aus_db()
    await mgr_neu.tick(t0 + timedelta(minutes=31))
    assert client_neu.oeffnen_aufrufe == [], (
        f"Neustart hat die Sequenz wieder aufgenommen: {client_neu.oeffnen_aufrufe}"
    )
    assert n == 0
    assert await speicher.hole_pre_soak_states() == []


# --- (d) Fehler im Abbruch verhindert das Schliessen nicht ---

async def test_abbruch_fehler_verhindert_schliessen_nicht(speicher, monkeypatch):
    """Wirft `notfall_abbrechen`, muessen die Ventile trotzdem zugehen und
    die Antwort den Teilfehler nennen (nicht als Erfolg melden)."""
    client = ClientAttrappe()
    sicherung, mgr = _dienst(speicher, client)
    await _starte_sequenz(mgr)
    assert sicherung.ist_aktiv(KANAL_BAMBUS)

    async def kaputt(grund: str = "notfall_stopp") -> list[str]:
        raise RuntimeError("Abbruch kaputt")

    monkeypatch.setattr(mgr, "notfall_abbrechen", kaputt)

    try:
        antwort = await api_server.notfall_stopp()
    except RuntimeError:
        antwort = None
    assert not sicherung.ist_aktiv(KANAL_BAMBUS), (
        "Ventil blieb offen, weil der Pre-Soak-Abbruch geworfen hat"
    )
    assert len(client.schliessen_aufrufe) == 1
    assert antwort is not None
    assert antwort["ok"] is False
    assert antwort["geschlossen"] == 1
    assert antwort["pre_soak_fehler"] == [{"fehler": "Abbruch kaputt"}]
    assert "Pre-Soak" in antwort["fehler"]


# --- (e) Reihenfolge: erst abbrechen, dann schliessen ---

class ClientMitTickImClose(ClientAttrappe):
    """Laesst waehrend eines Cloud-Close einmal den Pre-Soak-Ticker laufen.

    Im Dienst tickt der Pre-Soak-Ticker als eigener Task; waehrend der
    Notfall-Stopp auf die Cloud wartet, kann er drankommen.
    """

    def __init__(self) -> None:
        super().__init__()
        self.bei_schliessen = None

    async def ventil_schliessen(self, geraet_id, valve_id=None) -> None:
        await super().ventil_schliessen(geraet_id, valve_id=valve_id)
        hook, self.bei_schliessen = self.bei_schliessen, None
        if hook is not None:
            await hook()


async def test_tick_waehrend_notfall_close_startet_keine_hauptdose(speicher):
    """Faellige Hauptdose (Kanal 1, Pause) und ein offener Lauf auf Kanal 3.
    Tickt der Pre-Soak-Ticker, waehrend der Notfall-Stopp Kanal 3 schliesst,
    darf er Kanal 1 nicht mehr oeffnen. Schliesst die Route zuerst und bricht
    erst danach ab, oeffnet genau dieser Tick die Hauptdose -- und der
    Notfall-Stopp hat seine Kanalliste da schon gezogen."""
    client = ClientMitTickImClose()
    sicherung, mgr = _dienst(speicher, client)
    t0 = await _starte_sequenz(mgr)
    await _in_pause_bringen(sicherung, mgr, t0)
    assert await sicherung.bewaessere(
        KANAL_HECKE, ["hecke"], 600, Ausloser.MANUELL,
    )
    oeffnen_vorher = len(client.oeffnen_aufrufe)
    assert oeffnen_vorher == 2  # Puls + Hecke

    async def ticker() -> None:
        await mgr.tick(t0 + timedelta(minutes=31))

    client.bei_schliessen = ticker

    antwort = await api_server.notfall_stopp()

    assert client.bei_schliessen is None  # der Tick lief wirklich
    assert not sicherung.ist_aktiv(KANAL_BAMBUS), (
        "Hauptdose waehrend des Notfall-Stopps geoeffnet und offen geblieben"
    )
    assert len(client.oeffnen_aufrufe) == oeffnen_vorher
    assert not sicherung.ist_aktiv(KANAL_HECKE)
    assert antwort["ok"] is True
    assert antwort["geschlossen"] == 1
    assert antwort["pre_soak_abgebrochen"] == ["bambuswald"]


# --- (f) Notfall, waehrend der Tick auf die Cloud-Antwort der Hauptdose wartet ---

class ClientMitHaengendemOeffnen(ClientAttrappe):
    """Haelt ein Cloud-Open an, bis der Test die Antwort freigibt.

    Im Dienst wartet `tick()` in `bewaessere()` der Hauptdose auf die Cloud
    und haelt dabei den Manager-Lock; der Notfall-Knopf kann genau dann
    kommen. Das Kommando ist zu dem Zeitpunkt schon unterwegs.
    """

    def __init__(self) -> None:
        super().__init__()
        self.haengen = False
        self.open_angefragt = asyncio.Event()
        self.cloud_antwort = asyncio.Event()

    async def ventil_oeffnen(self, geraet_id, dauer_sekunden, valve_id=None) -> None:
        if self.haengen:
            self.open_angefragt.set()
            await self.cloud_antwort.wait()
        await super().ventil_oeffnen(geraet_id, dauer_sekunden, valve_id=valve_id)


async def test_notfall_waehrend_hauptdosen_open_laesst_kein_ventil_offen(speicher):
    """Der Tick haengt im Open der Hauptdose, der Notfall-Stopp kommt
    parallel. Richtig: der Abbruch wartet auf den Tick (Manager-Lock), danach
    schliesst die Route das gerade geoeffnete Ventil, der Lauf endet und
    nichts bleibt fuer `recover_aus_db` stehen. Ohne den Lock in
    `notfall_abbrechen` ist die Route fertig, bevor die Cloud antwortet: der
    Abbruch schreibt `fehler`, `notfall_stopp()` findet Kanal 1 noch nicht in
    `_aktiv`, und danach ueberschreibt der Tick mit `haupt`. Das Ventil
    bleibt nach dem Notfall-Stopp 60 min offen."""
    client = ClientMitHaengendemOeffnen()
    sicherung, mgr = _dienst(speicher, client)
    t0 = await _starte_sequenz(mgr)
    await _in_pause_bringen(sicherung, mgr, t0)
    client.haengen = True

    tick = asyncio.create_task(mgr.tick(t0 + timedelta(minutes=31)))
    await asyncio.wait_for(client.open_angefragt.wait(), 5)
    route = asyncio.create_task(api_server.notfall_stopp())
    # Richtig blockiert die Route am Manager-Lock und laeuft nie vor der
    # Cloud-Antwort durch; die Wartezeit gibt nur einer Route OHNE Lock die
    # Gelegenheit dazu (sie braucht dafuer wenige ms fuer den DB-Zugriff).
    await asyncio.wait({route}, timeout=0.3)
    client.cloud_antwort.set()
    try:
        antwort = await asyncio.wait_for(route, 5)
        await asyncio.wait_for(tick, 5)

        assert len(client.oeffnen_aufrufe) == 2  # Puls + Hauptdose kamen an
        assert not sicherung.ist_aktiv(KANAL_BAMBUS), (
            "Hauptdose nach dem Notfall-Stopp offen geblieben"
        )
        lauf = mgr.laufender_lauf("bambuswald")
        assert lauf.phase == "fehler", lauf.phase
        assert not mgr.ist_aktiv("bambuswald")
        assert await speicher.hole_pre_soak_states() == []
        assert antwort["ok"] is True
        assert antwort["pre_soak_abgebrochen"] == ["bambuswald"]
        assert antwort["geschlossen"] == 1
    finally:
        for aktiv in sicherung._aktiv.values():
            if aktiv.timer_handle:
                aktiv.timer_handle.cancel()


# --- Endphasen bleiben unberuehrt ---

async def test_notfall_abbrechen_laesst_stop_fehler_stehen():
    """`stop_fehler` heisst: Ventil evtl. noch offen. Der Notfall-Abbruch darf
    diese Warnung nicht in ein harmloses `fehler` umschreiben (Karte und
    Notfall-Knopf lesen sie) und meldet nur wirklich laufende Sequenzen als
    abgebrochen."""
    from bewaesserung.pre_soak import PreSoakManager
    from test_pre_soak import VentilSicherungAttrappe

    sicherung = VentilSicherungAttrappe(stoppe_erfolg=False, ist_aktiv=True)
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    for zone_id, kanal in (("bambuswald", KANAL_BAMBUS), ("hecke", KANAL_HECKE)):
        ok, fehler = await mgr.starte(
            zone_id=zone_id, kanal=kanal, zone_ids_kanal=[zone_id],
            pre_soak_min=5, pause_min=30, haupt_min=60,
        )
        assert ok, fehler
    ok, _ = await mgr.stoppe("bambuswald")
    assert not ok and mgr.laufender_lauf("bambuswald").phase == "stop_fehler"

    abgebrochen = await mgr.notfall_abbrechen("notfall_stopp")

    assert abgebrochen == ["hecke"]
    assert mgr.laufender_lauf("bambuswald").phase == "stop_fehler"
    assert mgr.laufender_lauf("hecke").phase == "fehler"
