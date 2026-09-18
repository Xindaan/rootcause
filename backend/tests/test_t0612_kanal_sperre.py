"""T-0612: Notfall-Stopp sperrt die Automatik der betroffenen Kanaele bis zur
manuellen Freigabe (Entscheidung Andre 30.09.2026, Folge aus T-0589).

Ohne Sperre startete die Automatik nach der Mindestpause von selbst wieder
(kritisch trockene Zone schon 1 h nach dem Stopp). Bei einem Leck floesse dann
wieder Wasser.

Aufbau (Wirkung statt Verkabelung), wie test_t0589_notfall_presoak.py und
test_t0607_kanal_block_loop.py: echte `VentilSicherung` mit `ClientAttrappe`
(kein Netz, keine Hardware), echter `Speicher` auf einer temporaeren DB, echte
PreSoakManager aus `konfiguriere_api`, der echte Routen-Kern
`api_server.notfall_stopp` und der ECHTE Entscheidungsloop fuer genau eine
Runde. Nur die Entscheidungsengine ist eine Attrappe (sie meldet Bedarf fuer
jeden Kanal). Gemessen wird, was am Client ankommt (`ventil_oeffnen` je
valve_id = Kanal) und was in der DB steht.

Absichtlich verschieden: der Dict-Schluessel der Sicherung (`DSWC`) und ihre
Geraete-ID (`GERAET`). Eine Stelle, die den falschen der beiden als
Sperr-Schluessel nimmt, faellt dadurch auf.

Harmlos auch ohne die Waechter: alle Cloud-Kommandos gehen an die Attrappe,
die DB liegt in tmp_path, die Push-Nachrichten an eine Attrappe.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import bewaesserung.api_server as api_server
from bewaesserung import main as M
from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.gardena_client import GardenaClient
from bewaesserung.modelle import (
    Ausloser,
    BewaesserungsEntscheidung,
    EntscheidungsScope,
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    WatchdogKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import VentilSicherung
from bewaesserung.watchdog import WatchdogJob
from test_ventil_sicherung import ClientAttrappe, SpeicherAttrappe

DSWC = "dswc-1"
GERAET = "ventil-001"
K_BAMBUS = 1
K_HECKE = 3
VALVES = {K_BAMBUS: "v-bambus", K_HECKE: "v-hecke"}
BAMBUS_ZONEN = ["bambuswald", "bambuswald_yogaraum"]


def _konfig(*, sperre_aktiv: bool = True) -> GesamtKonfig:
    """Zwei scharfe Kanaele (Automatik, Opt-in, Pre-Soak wie im Regelbetrieb)."""
    def zone(zone_id: str, name: str, kanal: int) -> ZonenKonfig:
        return ZonenKonfig(
            zone_id=zone_id, name=name, modus=ZonenModus.AUTOMATIK,
            ventil_kanal=kanal, ventil_geraet_id=DSWC, auto_loop_opt_in=True,
            pre_soak_modus="immer", pre_soak_min=5, pre_soak_pause_min=30,
        )

    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[
            zone("bambuswald", "Bambuswald", K_BAMBUS),
            zone("bambuswald_yogaraum", "Yogaraum", K_BAMBUS),
            zone("hecke", "Hecke", K_HECKE),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.5, laenge=13.4)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        ventilsteuerung_aktiv=True,
        notfall_kanal_sperre_aktiv=sperre_aktiv,
    )


class _Motor:
    """Meldet fuer jeden Kanal Bedarf (1 h Hauptdose) und zaehlt die Fragen.

    `pruefe_alle_zonen` laeuft direkt nach dem Kanal-Block; dort endet die
    Runde. Unbekannte Methoden fehlen absichtlich (AttributeError ->
    `entscheidungsloop.fehler` -> der Rundentest scheitert).
    """

    def __init__(self, stop: asyncio.Event):
        self._stop = stop
        self.kanal_fragen: list[int] = []

    async def pruefe_kanal(self, kanal, zonen):
        self.kanal_fragen.append(kanal)
        return BewaesserungsEntscheidung(
            zeitstempel=datetime.now(), zone_id=zonen[0].zone_id,
            soll_bewaessern=True, dauer_sekunden=3600,
            begruendung="Attrappe", scope=EntscheidungsScope.KANAL,
            scope_ref=str(kanal),
        )

    async def pruefe_kanal_max_stop(self, kanal, zonen, lauf_start=None):
        return False, "Attrappe: weiter"

    async def pruefe_alle_zonen(self):
        self._stop.set()
        return []

    async def prognostiziere_bewaesserung(self, zone_id):
        return None, ""


def _dienst(speicher: Speicher, client: ClientAttrappe, **konfig_kw):
    """Sicherung + Manager wie der Dienst (konfiguriere_api)."""
    sicherung = VentilSicherung(
        client=client,  # type: ignore[arg-type]
        speicher=speicher, ventil_geraet_id=GERAET,
        kanal_zu_valve_id=dict(VALVES),
    )
    konfig = _konfig(**konfig_kw)
    konfiguriere_api(
        speicher, konfig, MagicMock(), MagicMock(),
        ventil_sicherungen={DSWC: sicherung},
    )
    return konfig, sicherung, api_server._laufzeit.pre_soak_managers[DSWC]


def _raeume(sicherung: VentilSicherung) -> None:
    for aktiv in sicherung._aktiv.values():
        if aktiv.timer_handle:
            aktiv.timer_handle.cancel()


async def _loop_runde(konfig, speicher, sicherung, capsys) -> tuple[_Motor, str]:
    """Faehrt den echten Loop genau eine Runde und prueft, dass sie komplett
    lief (sonst waere jedes "nichts geoeffnet" wertlos)."""
    stop = asyncio.Event()
    motor = _Motor(stop)

    async def _notbremse():
        await asyncio.sleep(5)
        stop.set()

    notbremse = asyncio.create_task(_notbremse())
    try:
        await asyncio.wait_for(
            M._entscheidungsloop(
                motor=motor, konfig=konfig,
                benachrichtiger=MagicMock(), verarbeiter=MagicMock(),
                stop_event=stop, speicher=speicher,
                ventil_sicherungen={DSWC: sicherung},
            ),
            timeout=15,
        )
    finally:
        notbremse.cancel()
    aus = capsys.readouterr().out
    assert "entscheidungsloop.fehler" not in aus, aus[-1500:]
    assert "entscheidungsloop.heartbeat" in aus, "Runde nicht zu Ende gelaufen"
    return motor, aus


def _geoeffnete_valves(client: ClientAttrappe, ab: int = 0) -> list[str | None]:
    return [valve for _, _, valve in client.oeffnen_aufrufe[ab:]]


async def _starte_auto_sequenz(mgr, zone_id="bambuswald", kanal=K_BAMBUS,
                               zone_ids=None, ausloser=Ausloser.AUTOMATIK):
    ok, fehler = await mgr.starte(
        zone_id=zone_id, kanal=kanal,
        zone_ids_kanal=zone_ids or list(BAMBUS_ZONEN),
        pre_soak_min=5, pause_min=30, haupt_min=60, ausloser=ausloser,
    )
    assert ok, fehler
    return mgr.laufender_lauf(zone_id).gestartet_am


@pytest.fixture
async def speicher(tmp_path):
    sp = Speicher(str(tmp_path / "t0612.db"))
    await sp.verbinden()
    try:
        yield sp
    finally:
        await sp.schliessen()


@pytest.fixture(autouse=True)
def _schneller_loop(monkeypatch):
    monkeypatch.setattr(M, "ERSTE_PRUEFUNG_VERZOEGERUNG_S", 0.01)


# --- 1. Betroffener Kanal gesperrt, unbeteiligter giesst weiter --------------


async def test_notfall_sperrt_betroffenen_kanal_loop_giesst_nur_den_anderen(
    speicher, capsys,
):
    """Automatik-Sequenz auf Kanal 1 (Puls offen), Kanal 3 ruht. Nach dem
    Notfall-Stopp meldet die Engine fuer beide Kanaele Bedarf: der Loop darf
    nur Kanal 3 starten. Kanal 1 wird gar nicht erst bewertet, und es entsteht
    dort keine neue (gleich wieder scheiternde) Pre-Soak-Sequenz -- das ist die
    Wirkung des Loop-Waechters; der zentrale Waechter allein wuerde das Oeffnen
    zwar verhindern, aber eine Fehler-Sequenz hinterlassen."""
    client = ClientAttrappe()
    konfig, sicherung, mgr = _dienst(speicher, client)
    try:
        await _starte_auto_sequenz(mgr)
        assert _geoeffnete_valves(client) == [VALVES[K_BAMBUS]]  # nur der Puls

        antwort = await api_server.notfall_stopp()

        assert antwort["ok"] is True, antwort
        assert antwort["gesperrte_kanaele"] == [{
            "geraet_id": GERAET, "kanal": K_BAMBUS, "zone_ids": BAMBUS_ZONEN,
        }]
        assert not sicherung.ist_aktiv(K_BAMBUS)
        lauf_nach_notfall = mgr.laufender_lauf("bambuswald")
        oeffnen_vorher = len(client.oeffnen_aufrufe)

        motor, aus = await _loop_runde(konfig, speicher, sicherung, capsys)

        assert _geoeffnete_valves(client, oeffnen_vorher) == [VALVES[K_HECKE]]
        assert motor.kanal_fragen == [K_HECKE]
        assert mgr.laufender_lauf("bambuswald") is lauf_nach_notfall
        assert "entscheidungsloop.kanal_gesperrt" in aus
    finally:
        _raeume(sicherung)


async def test_nichts_betroffen_keine_sperre(speicher):
    """Kein Ventil offen, keine Sequenz: der Stopp sperrt nichts."""
    client = ClientAttrappe()
    _, sicherung, _ = _dienst(speicher, client)
    antwort = await api_server.notfall_stopp()
    assert antwort["ok"] is True
    assert antwort["gesperrte_kanaele"] == []
    assert await speicher.hole_offene_kanal_sperren() == []


async def test_pause_phase_wird_ueber_die_sequenz_gesperrt(speicher):
    """In der Pause ist das Ventil ZU: die Sperre entsteht nur ueber die vom
    Notfall abgebrochene Sequenz (Zone -> Kanal ueber die Config)."""
    client = ClientAttrappe()
    _, sicherung, mgr = _dienst(speicher, client)
    try:
        t0 = await _starte_auto_sequenz(mgr)
        assert await sicherung.stoppe(K_BAMBUS, Ausloser.AUTOMATIK)
        await mgr.tick(t0 + timedelta(minutes=10))
        assert mgr.laufender_lauf("bambuswald").phase == "pause"
        assert sicherung.notfall_betroffene_kanaele() == []

        antwort = await api_server.notfall_stopp()

        assert antwort["pre_soak_abgebrochen"] == ["bambuswald"]
        assert [(g["geraet_id"], g["kanal"]) for g in antwort["gesperrte_kanaele"]] \
            == [(GERAET, K_BAMBUS)]
    finally:
        _raeume(sicherung)


async def test_pause_phase_abbruch_wirft_sperre_entsteht_trotzdem(
    speicher, monkeypatch,
):
    """Gate G3-2: Pause-Phase (Ventil zu) und `notfall_abbrechen` wirft. Die
    Kanaele der laufenden Sequenzen liest die Route VOR dem Abbruch direkt vom
    Manager: die Sperre entsteht trotzdem, und der Tick nach der Pause oeffnet
    die Automatik-Hauptdose nicht (zentraler Waechter). Vorher hing die
    Kanalmenge am Rueckgabewert des Abbruchs: keine Sperre, Hauptdose auf."""
    client = ClientAttrappe()
    _, sicherung, mgr = _dienst(speicher, client)

    async def _abbruch_kaputt(grund="notfall_stopp"):
        raise RuntimeError("Abbruch gescheitert")

    try:
        t0 = await _starte_auto_sequenz(mgr)
        assert await sicherung.stoppe(K_BAMBUS, Ausloser.AUTOMATIK)
        await mgr.tick(t0 + timedelta(minutes=10))
        assert mgr.laufender_lauf("bambuswald").phase == "pause"
        assert sicherung.notfall_betroffene_kanaele() == []
        monkeypatch.setattr(mgr, "notfall_abbrechen", _abbruch_kaputt)

        antwort = await api_server.notfall_stopp()

        assert antwort["pre_soak_fehler"]
        assert "sperre_fehler" not in antwort, antwort
        assert [(g["geraet_id"], g["kanal"]) for g in antwort["gesperrte_kanaele"]] \
            == [(GERAET, K_BAMBUS)]
        assert [(s["geraet_id"], s["kanal"])
                for s in await speicher.hole_offene_kanal_sperren()] \
            == [(GERAET, K_BAMBUS)]
        oeffnen_vorher = len(client.oeffnen_aufrufe)

        await mgr.tick(t0 + timedelta(minutes=31))

        assert _geoeffnete_valves(client, oeffnen_vorher) == []
        assert not sicherung.ist_aktiv(K_BAMBUS)
    finally:
        _raeume(sicherung)


async def test_pre_soak_kanaele_weder_lesbar_noch_abbrechbar_meldet_sperrfehler(
    speicher, monkeypatch,
):
    """Gate G3-2, Restfall: scheitern das Lesen der Kanaele UND der Abbruch
    desselben Managers, kennt die Sperre diesen Kanal nicht. Das ist eine
    unvollstaendige Sperre und muss als `sperre_fehler` gemeldet werden,
    nicht nur als Pre-Soak-Fehler."""
    client = ClientAttrappe()
    _, sicherung, mgr = _dienst(speicher, client)

    async def _abbruch_kaputt(grund="notfall_stopp"):
        raise RuntimeError("Abbruch gescheitert")

    def _lesen_kaputt():
        raise RuntimeError("Lesen gescheitert")

    try:
        t0 = await _starte_auto_sequenz(mgr)
        assert await sicherung.stoppe(K_BAMBUS, Ausloser.AUTOMATIK)
        await mgr.tick(t0 + timedelta(minutes=10))
        monkeypatch.setattr(mgr, "notfall_abbrechen", _abbruch_kaputt)
        monkeypatch.setattr(mgr, "notfall_betroffene_kanaele", _lesen_kaputt)

        antwort = await api_server.notfall_stopp()

        assert antwort["ok"] is False
        assert antwort["gesperrte_kanaele"] == []
        assert antwort["sperre_fehler"] == [{
            "fehler": "Kanaele laufender Pre-Soak-Sequenzen nicht ermittelbar",
        }]
    finally:
        _raeume(sicherung)


async def test_extern_offener_kanal_wird_gesperrt(speicher):
    """Ein fremder Lauf (Gardena-App/Zeitplan) auf Kanal 3, den der Stopp
    schliesst, ist ebenfalls betroffen."""
    client = ClientAttrappe()
    client.offene = {VALVES[K_HECKE]: {"activity": "SCHEDULED_WATERING"}}
    _, sicherung, _ = _dienst(speicher, client)

    antwort = await api_server.notfall_stopp()

    assert (GERAET, VALVES[K_HECKE]) in client.schliessen_aufrufe
    assert [(g["kanal"], g["zone_ids"]) for g in antwort["gesperrte_kanaele"]] \
        == [(K_HECKE, ["hecke"])]



async def test_sperre_gilt_nur_fuer_ihr_geraet(tmp_path, capsys):
    """Zwei Dual Water Controls mit je einem Kanal 1 (wie im Garten). Eine
    Sperre auf Geraet A darf Kanal 1 von Geraet B nicht treffen -- weder im
    Loop noch im Waechter in `bewaessere`."""
    sp = Speicher(str(tmp_path / "zwei.db"))
    await sp.verbinden()
    client_a, client_b = ClientAttrappe(), ClientAttrappe()
    sich_a = VentilSicherung(
        client=client_a, speicher=sp, ventil_geraet_id="ventil-A",  # type: ignore[arg-type]
    )
    sich_b = VentilSicherung(
        client=client_b, speicher=sp, ventil_geraet_id="ventil-B",  # type: ignore[arg-type]
    )

    def zone(zone_id: str, dswc: str) -> ZonenKonfig:
        return ZonenKonfig(
            zone_id=zone_id, name=zone_id.title(), modus=ZonenModus.AUTOMATIK,
            ventil_kanal=1, ventil_geraet_id=dswc, auto_loop_opt_in=True,
        )

    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[zone("zone_a", "dswc-A"), zone("zone_b", "dswc-B")],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.5, laenge=13.4)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        ventilsteuerung_aktiv=True,
    )
    sicherungen = {"dswc-A": sich_a, "dswc-B": sich_b}
    konfiguriere_api(
        sp, konfig, MagicMock(), MagicMock(), ventil_sicherungen=sicherungen,
    )
    await sp.setze_kanal_sperre(
        "ventil-A", 1, ["zone_a"], grund="Notfall-Stopp", gesetzt_durch="test",
    )
    stop = asyncio.Event()
    motor = _Motor(stop)
    try:
        await asyncio.wait_for(
            M._entscheidungsloop(
                motor=motor, konfig=konfig,
                benachrichtiger=MagicMock(), verarbeiter=MagicMock(),
                stop_event=stop, speicher=sp, ventil_sicherungen=sicherungen,
            ),
            timeout=15,
        )
        aus = capsys.readouterr().out
        assert "entscheidungsloop.heartbeat" in aus
        assert client_a.oeffnen_aufrufe == []
        assert [(g, d) for g, d, _ in client_b.oeffnen_aufrufe] == [("ventil-B", 3600)]
    finally:
        _raeume(sich_a)
        _raeume(sich_b)
        await sp.schliessen()

# --- 2. Sperre ueberlebt einen Neustart -------------------------------------


async def test_sperre_ueberlebt_neuen_speicher_und_manager(tmp_path, capsys):
    """Nach dem Stopp: Speicher schliessen, neuen Speicher, neue Sicherung und
    neue Manager auf derselben DB aufbauen (= Neustart). Der Loop giesst
    weiterhin nur Kanal 3."""
    pfad = str(tmp_path / "neustart.db")
    sp1 = Speicher(pfad)
    await sp1.verbinden()
    client1 = ClientAttrappe()
    _, sicherung1, mgr1 = _dienst(sp1, client1)
    try:
        await _starte_auto_sequenz(mgr1)
        antwort = await api_server.notfall_stopp()
        assert [g["kanal"] for g in antwort["gesperrte_kanaele"]] == [K_BAMBUS]
    finally:
        _raeume(sicherung1)
        await sp1.schliessen()

    sp2 = Speicher(pfad)
    await sp2.verbinden()
    client2 = ClientAttrappe()
    konfig, sicherung2, _ = _dienst(sp2, client2)
    try:
        offen = await sp2.hole_offene_kanal_sperren()
        assert [(s["geraet_id"], s["kanal"], s["zone_ids"]) for s in offen] \
            == [(GERAET, K_BAMBUS, BAMBUS_ZONEN)]

        await _loop_runde(konfig, sp2, sicherung2, capsys)

        assert _geoeffnete_valves(client2) == [VALVES[K_HECKE]]
    finally:
        _raeume(sicherung2)
        await sp2.schliessen()


# --- 3. Freigabe hebt die Sperre auf ----------------------------------------


async def test_freigabe_hebt_sperre_auf_und_loop_giesst_wieder(speicher, capsys):
    client = ClientAttrappe()
    konfig, sicherung, mgr = _dienst(speicher, client)
    try:
        await _starte_auto_sequenz(mgr)
        await api_server.notfall_stopp()
        [sperre] = (await api_server.kanal_sperren_liste())["sperren"]

        antwort = await api_server.kanal_sperre_aufheben(
            sperre["id"], {"durch": "dashboard"},
        )

        assert antwort == {"ok": True, "aufgehoben": True}
        assert (await api_server.kanal_sperren_liste())["sperren"] == []
        # idempotent: ein zweiter Klick aendert nichts mehr
        assert await api_server.kanal_sperre_aufheben(sperre["id"], None) \
            == {"ok": True, "aufgehoben": False}
        oeffnen_vorher = len(client.oeffnen_aufrufe)

        motor, _ = await _loop_runde(konfig, speicher, sicherung, capsys)

        assert sorted(motor.kanal_fragen) == [K_BAMBUS, K_HECKE]
        assert sorted(_geoeffnete_valves(client, oeffnen_vorher)) \
            == sorted([VALVES[K_BAMBUS], VALVES[K_HECKE]])
    finally:
        _raeume(sicherung)


# --- 4. Haekchen, Schalter, HTTP-Body ---------------------------------------


async def test_ohne_haekchen_keine_sperre_ventile_trotzdem_zu(speicher):
    client = ClientAttrappe()
    _, sicherung, mgr = _dienst(speicher, client)
    try:
        await _starte_auto_sequenz(mgr)
        antwort = await api_server.notfall_stopp(sperren=False)
        assert antwort["ok"] is True
        assert antwort["gesperrte_kanaele"] == []
        assert await speicher.hole_offene_kanal_sperren() == []
        assert not sicherung.ist_aktiv(K_BAMBUS)
    finally:
        _raeume(sicherung)


@pytest.mark.parametrize("schalter", [False, True], ids=["aus", "gegenfall_an"])
async def test_konfig_schalter_aus_setzt_keine_sperre(speicher, schalter):
    """`notfall_kanal_sperre_aktiv: false`: Verhalten wie nach T-0589.
    Gegenfall mit identischem Aufbau: der Schalter an sperrt."""
    client = ClientAttrappe()
    _, sicherung, mgr = _dienst(speicher, client, sperre_aktiv=schalter)
    try:
        await _starte_auto_sequenz(mgr)
        antwort = await api_server.notfall_stopp()
        assert not sicherung.ist_aktiv(K_BAMBUS)
        assert bool(antwort["gesperrte_kanaele"]) is schalter
        assert bool(await speicher.hole_offene_kanal_sperren()) is schalter
        assert (await api_server.kanal_sperren_liste())["schalter_aktiv"] is schalter
    finally:
        _raeume(sicherung)


class _HttpSicherung:
    """Minimale Sicherung fuer die HTTP-Huelle (TestClient faehrt einen
    eigenen Eventloop, deshalb ohne Timer): Kanal 1 ist offen."""
    geraet_id = GERAET

    def __init__(self):
        self.stopps = 0

    def notfall_betroffene_kanaele(self):
        return [K_BAMBUS]

    async def notfall_stopp(self):
        self.stopps += 1
        return {"geschlossen": 1, "fehlgeschlagen": []}


@pytest.mark.parametrize(
    "body,sperre_erwartet",
    [
        (None, True),                        # heutiger Frontend-Aufruf
        ({"sperren": True}, True),
        ({"sperren": False}, False),         # Haekchen abgewaehlt
        ("{kaputt", True),                   # nie 422: Stopp muss durch
        ({"sperren": "nein"}, True),         # nur echtes false schaltet ab
    ],
    ids=["ohne_body", "true", "false", "kaputt", "kein_bool"],
)
def test_http_body_steuert_sperre_und_stopp_laeuft_immer(
    tmp_path, body, sperre_erwartet,
):
    sp = Speicher(str(tmp_path / "http.db"))
    asyncio.run(sp.verbinden())
    sicherung = _HttpSicherung()
    konfiguriere_api(
        sp, _konfig(), MagicMock(), MagicMock(),
        ventil_sicherungen={DSWC: sicherung},  # type: ignore[dict-item]
    )
    c = TestClient(app)
    try:
        if body is None:
            r = c.post("/api/notfall-stopp")
        elif isinstance(body, str):
            r = c.post(
                "/api/notfall-stopp", content=body,
                headers={"Content-Type": "application/json"},
            )
        else:
            r = c.post("/api/notfall-stopp", json=body)
        assert r.status_code == 200, r.text
        assert sicherung.stopps == 1
        sperren = asyncio.run(sp.hole_offene_kanal_sperren())
        assert bool(sperren) is sperre_erwartet
        assert bool(r.json()["gesperrte_kanaele"]) is sperre_erwartet
    finally:
        c.close()
        asyncio.run(sp.schliessen())


def test_get_kanal_sperren_meldet_lesefehler_mit_200_statt_500(tmp_path):
    """Gate G3-1(b): sind die Sperren nicht lesbar, liefert GET
    /api/kanal-sperren 200 mit `fehler` und leerer Liste statt HTTP 500. Nur
    so kann das Dashboard "Sperrstatus unklar" zeigen (die Automatik steht
    dann fail-closed) statt nur einen gescheiterten Abruf."""
    sp = Speicher(str(tmp_path / "get.db"))
    asyncio.run(sp.verbinden())

    async def _unlesbar():
        raise sqlite3.OperationalError("disk I/O error")

    sp.hole_offene_kanal_sperren = _unlesbar  # type: ignore[method-assign]
    konfiguriere_api(
        sp, _konfig(), MagicMock(), MagicMock(),
        ventil_sicherungen={DSWC: _HttpSicherung()},  # type: ignore[dict-item]
    )
    c = TestClient(app, raise_server_exceptions=False)
    try:
        r = c.get("/api/kanal-sperren")
        assert r.status_code == 200, r.text
        daten = r.json()
        assert daten["sperren"] == []
        assert daten["schalter_aktiv"] is True
        assert "nicht lesbar" in daten["fehler"]
    finally:
        c.close()
        asyncio.run(sp.schliessen())


# --- 5. Fehler beim Sperren verhindert das Schliessen nicht -----------------


async def test_fehler_je_kanal_sperrt_die_uebrigen_und_schliesst(
    speicher, monkeypatch,
):
    """Beide Kanaele offen, das Sperren scheitert nur fuer Kanal 1: Kanal 3
    wird trotzdem gesperrt, beide Ventile gehen zu, die Antwort meldet den
    Teilfehler (kein Erfolg -- Andre verliesse sich sonst auf eine Sperre,
    die es nicht gibt)."""
    client = ClientAttrappe()
    _, sicherung, _ = _dienst(speicher, client)
    echt = speicher.setze_kanal_sperre

    async def _setze(geraet_id, kanal, *a, **kw):
        if kanal == K_BAMBUS:
            raise sqlite3.OperationalError("disk I/O error")
        return await echt(geraet_id, kanal, *a, **kw)

    monkeypatch.setattr(speicher, "setze_kanal_sperre", _setze)
    try:
        assert await sicherung.bewaessere(K_BAMBUS, BAMBUS_ZONEN, 600, Ausloser.MANUELL)
        assert await sicherung.bewaessere(K_HECKE, ["hecke"], 600, Ausloser.MANUELL)

        antwort = await api_server.notfall_stopp()

        assert not sicherung.ist_aktiv(K_BAMBUS)
        assert not sicherung.ist_aktiv(K_HECKE)
        assert antwort["ok"] is False
        assert "Automatik-Sperre unvollstaendig" in antwort["fehler"]
        assert [f.get("kanal") for f in antwort["sperre_fehler"]] == [K_BAMBUS]
        assert [g["kanal"] for g in antwort["gesperrte_kanaele"]] == [K_HECKE]
    finally:
        _raeume(sicherung)


async def test_unerwarteter_fehler_im_sperrschritt_schliesst_trotzdem(
    speicher, monkeypatch,
):
    client = ClientAttrappe()
    _, sicherung, _ = _dienst(speicher, client)

    async def _kaputt(*a, **kw):
        raise RuntimeError("unerwartet")

    monkeypatch.setattr(api_server, "_notfall_kanaele_sperren", _kaputt)
    try:
        assert await sicherung.bewaessere(K_BAMBUS, BAMBUS_ZONEN, 600, Ausloser.MANUELL)

        antwort = await api_server.notfall_stopp()

        assert not sicherung.ist_aktiv(K_BAMBUS)
        # T-0592: der eigene Lauf zuerst, danach jedes weitere gemappte
        # Ventil vorsorglich (ohne Offen-Beleg).
        assert client.schliessen_aufrufe == [
            (GERAET, VALVES[K_BAMBUS]), (GERAET, VALVES[K_HECKE]),
        ]
        assert antwort["ok"] is False
        assert antwort["sperre_fehler"]
    finally:
        _raeume(sicherung)


# --- 6. Zentraler Waechter: Automatik-Sequenz, deren Abbruch scheiterte -----


@pytest.mark.parametrize(
    "ausloser", [Ausloser.AUTOMATIK, Ausloser.MANUELL],
    ids=["automatik_gesperrt", "manuell_bleibt_offen"],
)
async def test_pre_soak_tick_oeffnet_keine_automatik_hauptdose_auf_gesperrtem_kanal(
    speicher, monkeypatch, ausloser,
):
    """Scheitert im Notfall der Abbruch eines Managers (T-0589 meldet das nur
    als Teilfehler), laeuft die Sequenz weiter, und der Tick oeffnet nach der
    Pause die Hauptdose. Fuer eine Automatik-Sequenz blockt das der Waechter
    in `VentilSicherung.bewaessere`, weil der Kanal jetzt gesperrt ist.
    Gegenfall mit identischem Aufbau: eine MANUELLE Sequenz (Andres eigene
    Handlung) giesst auf dem gesperrten Kanal weiter."""
    client = ClientAttrappe()
    _, sicherung, mgr = _dienst(speicher, client)

    async def _abbruch_kaputt(grund="notfall_stopp"):
        raise RuntimeError("Abbruch gescheitert")

    try:
        t0 = await _starte_auto_sequenz(mgr, ausloser=ausloser)
        monkeypatch.setattr(mgr, "notfall_abbrechen", _abbruch_kaputt)

        antwort = await api_server.notfall_stopp()

        assert antwort["pre_soak_fehler"]
        assert [g["kanal"] for g in antwort["gesperrte_kanaele"]] == [K_BAMBUS]
        oeffnen_vorher = len(client.oeffnen_aufrufe)

        await mgr.tick(t0 + timedelta(minutes=31))

        neu = _geoeffnete_valves(client, oeffnen_vorher)
        if ausloser == Ausloser.AUTOMATIK:
            assert neu == []
            assert not sicherung.ist_aktiv(K_BAMBUS)
        else:
            assert neu == [VALVES[K_BAMBUS]]
            assert sicherung.ist_aktiv(K_BAMBUS)
    finally:
        _raeume(sicherung)


async def test_manueller_start_auf_gesperrtem_kanal_bleibt_moeglich(speicher):
    """Die manuelle Route giesst auf einem gesperrten Kanal (Andres Handlung)."""
    client = ClientAttrappe()
    _, sicherung, mgr = _dienst(speicher, client)
    try:
        await _starte_auto_sequenz(mgr)
        await api_server.notfall_stopp()
        assert await speicher.hole_offene_kanal_sperren()
        oeffnen_vorher = len(client.oeffnen_aufrufe)

        antwort = await api_server.ventil_manuell_start(
            {"zone_id": "bambuswald", "dauer_sekunden": 120},
        )

        assert antwort["ok"] is True, antwort
        assert _geoeffnete_valves(client, oeffnen_vorher) == [VALVES[K_BAMBUS]]
    finally:
        _raeume(sicherung)


# --- 7. Fail-closed, wenn die Sperren nicht lesbar sind ---------------------


async def test_loop_startet_nichts_wenn_sperren_unlesbar(speicher, monkeypatch, capsys):
    client = ClientAttrappe()
    konfig, sicherung, _ = _dienst(speicher, client)

    async def _unlesbar():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(speicher, "hole_offene_kanal_sperren", _unlesbar)
    try:
        motor, aus = await _loop_runde(konfig, speicher, sicherung, capsys)
        assert client.oeffnen_aufrufe == []
        assert motor.kanal_fragen == []
        assert "entscheidungsloop.kanal_sperre_lesefehler" in aus
    finally:
        _raeume(sicherung)


async def test_zentraler_waechter_lehnt_automatik_ab_wenn_sperren_unlesbar(
    speicher, monkeypatch,
):
    client = ClientAttrappe()
    _, sicherung, _ = _dienst(speicher, client)

    async def _unlesbar():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(speicher, "hole_offene_kanal_sperren", _unlesbar)
    try:
        assert not await sicherung.bewaessere(
            K_HECKE, ["hecke"], 600, Ausloser.AUTOMATIK,
        )
        assert client.oeffnen_aufrufe == []
        # Manuell bleibt offen, auch ohne lesbare Sperren.
        assert await sicherung.bewaessere(K_HECKE, ["hecke"], 600, Ausloser.MANUELL)
    finally:
        _raeume(sicherung)


# --- 8. Idempotenz: eine offene Sperre je Kanal -----------------------------


async def test_zweiter_notfall_auf_gesperrtem_kanal_erzeugt_keine_zweite_sperre(
    speicher,
):
    """Andre giesst auf dem gesperrten Kanal von Hand und drueckt wieder den
    Notfall-Stopp: es bleibt EINE Sperre mit dem Zeitpunkt des ersten Stopps.
    Eine zweite waere nach einer Freigabe noch da -- der Kanal bliebe
    gesperrt, obwohl das Banner "freigegeben" gezeigt haette."""
    client = ClientAttrappe()
    _, sicherung, _ = _dienst(speicher, client)
    try:
        assert await sicherung.bewaessere(K_BAMBUS, BAMBUS_ZONEN, 600, Ausloser.MANUELL)
        await api_server.notfall_stopp()
        [erste] = await speicher.hole_offene_kanal_sperren()
        assert await sicherung.bewaessere(K_BAMBUS, BAMBUS_ZONEN, 600, Ausloser.MANUELL)

        antwort = await api_server.notfall_stopp()

        assert antwort["ok"] is True, antwort
        assert await speicher.hole_offene_kanal_sperren() == [erste]
    finally:
        _raeume(sicherung)


async def test_db_haelt_hoechstens_eine_offene_sperre_je_kanal(speicher):
    """Der partielle UNIQUE-Index greift auch fuer Schreiber, die an
    `setze_kanal_sperre` vorbeigehen; eine aufgehobene Sperre zaehlt nicht."""
    sql = (
        "INSERT INTO kanal_sperre (geraet_id, kanal, zone_ids_json, grund, "
        "gesetzt_am, gesetzt_durch, aufgehoben_am) VALUES (?, ?, '[]', 'x', ?, 'x', ?)"
    )
    jetzt = datetime.now().isoformat()
    await speicher._db.execute(sql, (GERAET, K_BAMBUS, jetzt, jetzt))  # aufgehoben
    await speicher._db.execute(sql, (GERAET, K_BAMBUS, jetzt, None))   # offen
    with pytest.raises(sqlite3.IntegrityError):
        await speicher._db.execute(sql, (GERAET, K_BAMBUS, jetzt, None))


# --- 9. Taegliche Erinnerung per Push ---------------------------------------


class _Bote:
    def __init__(self):
        self.gesendet: list[str] = []

    async def sende_text(self, empfaenger: str, text: str) -> bool:
        self.gesendet.append(text)
        return True


async def test_push_erinnert_einmal_taeglich_mit_klartextnamen(speicher):
    sperre_id, neu = await speicher.setze_kanal_sperre(
        GERAET, K_BAMBUS, BAMBUS_ZONEN, grund="Notfall-Stopp",
        gesetzt_durch="notfall-stopp", jetzt=datetime(2026, 9, 30, 14, 2),
    )
    assert neu
    bote = _Bote()
    job = WatchdogJob(
        speicher=speicher, benachrichtiger=bote,
        konfig=WatchdogKonfig(
            aktiv=True, empfaenger="+490000", intervall_minuten=1,
            throttle_stunden=1,   # der Trigger drosselt trotzdem fest auf 24 h
        ),
        zonen=_konfig().zonen, gardena_zone_ids=[],
    )
    t = datetime(2026, 9, 30, 14, 10)

    def sperr_meldungen():
        return [m for m in bote.gesendet if "gesperrt" in m]

    await job.pruefe_und_sende_wenn_faellig(jetzt=t)
    assert len(sperr_meldungen()) == 1
    text = sperr_meldungen()[0]
    assert "Bambuswald" in text and "Yogaraum" in text
    assert "bambuswald" not in text and GERAET not in text   # keine IDs
    assert "30.09. 14:02" in text

    await job.pruefe_und_sende_wenn_faellig(jetzt=t + timedelta(hours=3))
    await job.pruefe_und_sende_wenn_faellig(jetzt=t + timedelta(hours=23))
    assert len(sperr_meldungen()) == 1                       # gedrosselt

    await job.pruefe_und_sende_wenn_faellig(jetzt=t + timedelta(hours=24, minutes=1))
    assert len(sperr_meldungen()) == 2                       # naechster Tag

    assert await speicher.hebe_kanal_sperre_auf(sperre_id, "dashboard")
    await job.pruefe_und_sende_wenn_faellig(jetzt=t + timedelta(hours=49))
    assert len(sperr_meldungen()) == 2                       # freigegeben: Ruhe


async def test_push_drossel_schluessel_ist_zone_und_neue_sperre_meldet_sofort(
    speicher,
):
    """Gate G3-3: der Drossel-Schluessel ist die erste Zone des Kanals (wie
    T-0590), damit `letztes_watchdog_event_zone` im Ops-Feld lesbar bleibt;
    vorher stand dort `sperre:<id>`. Eindeutig je Sperre bleibt die Drossel
    trotzdem: eine NEUE Sperre auf demselben Kanal (nach Freigabe) meldet sich
    sofort, nicht erst 24 h nach der Erinnerung an die alte."""
    erste, _ = await speicher.setze_kanal_sperre(
        GERAET, K_BAMBUS, BAMBUS_ZONEN, grund="Notfall-Stopp",
        gesetzt_durch="notfall-stopp", jetzt=datetime(2026, 9, 30, 14, 2),
    )
    bote = _Bote()
    job = WatchdogJob(
        speicher=speicher, benachrichtiger=bote,
        konfig=WatchdogKonfig(aktiv=True, empfaenger="+490000", intervall_minuten=1),
        zonen=_konfig().zonen, gardena_zone_ids=[],
    )
    t = datetime(2026, 9, 30, 14, 10)

    def sperr_meldungen():
        return [m for m in bote.gesendet if "gesperrt" in m]

    await job.pruefe_und_sende_wenn_faellig(jetzt=t)
    assert len(sperr_meldungen()) == 1
    ops = await speicher.hole_betriebsstatus_aggregate(jetzt=t)
    assert ops["letztes_watchdog_event_trigger"] == "kanal_sperre"
    assert ops["letztes_watchdog_event_zone"] == "bambuswald"

    assert await speicher.hebe_kanal_sperre_auf(erste, "dashboard")
    await speicher.setze_kanal_sperre(
        GERAET, K_BAMBUS, BAMBUS_ZONEN, grund="Notfall-Stopp",
        gesetzt_durch="notfall-stopp", jetzt=t + timedelta(hours=2),
    )
    await job.pruefe_und_sende_wenn_faellig(jetzt=t + timedelta(hours=2, minutes=5))
    assert len(sperr_meldungen()) == 2                        # neue Sperre: sofort
    await job.pruefe_und_sende_wenn_faellig(jetzt=t + timedelta(hours=5))
    assert len(sperr_meldungen()) == 2                        # dann wieder 24 h


async def test_push_meldet_unlesbare_sperren_gedrosselt(speicher, monkeypatch):
    """Gate G3-1(a): sind die Sperren nicht lesbar, steht die Automatik
    fail-closed auf allen Kanaelen. Das darf nicht stumm sein: Push mit
    eigener Drossel (fest 24 h), Text ohne Kennungen."""
    async def _unlesbar():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(speicher, "hole_offene_kanal_sperren", _unlesbar)
    bote = _Bote()
    job = WatchdogJob(
        speicher=speicher, benachrichtiger=bote,
        konfig=WatchdogKonfig(
            aktiv=True, empfaenger="+490000", intervall_minuten=1,
            throttle_stunden=1,   # der Trigger drosselt trotzdem fest auf 24 h
        ),
        zonen=_konfig().zonen, gardena_zone_ids=[],
    )
    t = datetime(2026, 9, 30, 15, 0)

    def meldungen():
        return [m for m in bote.gesendet if "Kanal-Sperren" in m]

    await job.pruefe_und_sende_wenn_faellig(jetzt=t)
    assert len(meldungen()) == 1
    text = meldungen()[0]
    assert "nicht lesen" in text and "aus Vorsicht" in text
    assert GERAET not in text and DSWC not in text

    await job.pruefe_und_sende_wenn_faellig(jetzt=t + timedelta(hours=3))
    await job.pruefe_und_sende_wenn_faellig(jetzt=t + timedelta(hours=23))
    assert len(meldungen()) == 1                             # gedrosselt
    await job.pruefe_und_sende_wenn_faellig(jetzt=t + timedelta(hours=24, minutes=1))
    assert len(meldungen()) == 2                             # naechster Tag


async def test_push_meldet_unlesbare_sperren_auch_ohne_db_drossel(
    speicher, monkeypatch,
):
    """Gate G3-1(a), ganze DB kaputt: auch die DB-Drossel ist weder lesbar
    noch schreibbar. Dann meldet der Watchdog trotzdem (sonst waere der
    Stillstand wieder stumm) und drosselt im Prozess, statt bei jedem Tick."""
    async def _kaputt(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(speicher, "hole_offene_kanal_sperren", _kaputt)
    monkeypatch.setattr(speicher, "hole_letzten_watchdog_push", _kaputt)
    monkeypatch.setattr(speicher, "setze_watchdog_push", _kaputt)
    bote = _Bote()
    job = WatchdogJob(
        speicher=speicher, benachrichtiger=bote,
        konfig=WatchdogKonfig(aktiv=True, empfaenger="+490000", intervall_minuten=1),
        zonen=_konfig().zonen, gardena_zone_ids=[],
    )
    t = datetime(2026, 9, 30, 15, 0)

    def meldungen():
        return [m for m in bote.gesendet if "Kanal-Sperren" in m]

    await job.pruefe_und_sende_wenn_faellig(jetzt=t)
    assert len(meldungen()) == 1
    await job.pruefe_und_sende_wenn_faellig(jetzt=t + timedelta(hours=3))
    assert len(meldungen()) == 1


# --- Gate B1: Notfall waehrend eines Starts / Fremdlauf im Replay-Guard ------


class _ClientOeffnenHaengt(ClientAttrappe):
    """Der Oeffnen-Befehl haengt, bis der Test ihn freigibt (Befehl
    unterwegs, als der Notfall kommt)."""

    def __init__(self):
        super().__init__()
        self.freigabe = asyncio.Event()
        self.unterwegs = asyncio.Event()

    async def ventil_oeffnen(self, geraet_id, dauer_sekunden, valve_id=None):
        self.unterwegs.set()
        await self.freigabe.wait()
        await super().ventil_oeffnen(geraet_id, dauer_sekunden, valve_id=valve_id)


async def _notfall_waehrend_auto_start(client, sicherung):
    start = asyncio.create_task(sicherung.bewaessere(
        K_BAMBUS, list(BAMBUS_ZONEN), 600, Ausloser.AUTOMATIK,
    ))
    await asyncio.wait_for(client.unterwegs.wait(), 1)
    antwort = await api_server.notfall_stopp()
    client.freigabe.set()
    assert await asyncio.wait_for(start, 1) is False  # Epoche: sofort wieder zu
    assert not sicherung.ist_aktiv(K_BAMBUS)
    return antwort


async def test_notfall_waehrend_haengendem_oeffnen_sperrt_kanal_loop_oeffnet_nicht(
    speicher, capsys,
):
    """Gate B1: ein Automatik-Start hat den Oeffnen-Befehl unterwegs, als der
    Notfall-Stopp kommt. Die Epoche schliesst das Ventil gleich wieder; der
    Kanal muss aber auch gesperrt sein -- vorher stand er nirgends
    (`_aktiv` noch leer, Cloud meldet nichts offen), und der naechste
    Auto-Tick oeffnete dort wieder."""
    client = _ClientOeffnenHaengt()
    konfig, sicherung, _ = _dienst(speicher, client)
    try:
        antwort = await _notfall_waehrend_auto_start(client, sicherung)

        assert [g["kanal"] for g in antwort["gesperrte_kanaele"]] == [K_BAMBUS]
        oeffnen_vorher = len(client.oeffnen_aufrufe)
        motor, _ = await _loop_runde(konfig, speicher, sicherung, capsys)
        assert _geoeffnete_valves(client, oeffnen_vorher) == [VALVES[K_HECKE]]
        assert motor.kanal_fragen == [K_HECKE]
    finally:
        _raeume(sicherung)


async def test_laufender_start_steht_vor_und_nach_dem_schliessen_in_der_sperrmenge():
    """Gate B1, beide Quellen der Route einzeln: `notfall_betroffene_kanaele`
    (vor dem Schliessen gelesen) und `sperr_kanaele` aus `notfall_stopp`
    (Nachsperren) kennen den Kanal mit laufendem Start."""
    client = _ClientOeffnenHaengt()
    sicherung = VentilSicherung(
        client=client, speicher=SpeicherAttrappe(),  # type: ignore[arg-type]
        ventil_geraet_id=GERAET, kanal_zu_valve_id=dict(VALVES),
    )
    start = asyncio.create_task(sicherung.bewaessere(
        K_BAMBUS, list(BAMBUS_ZONEN), 600, Ausloser.MANUELL,
    ))
    await asyncio.wait_for(client.unterwegs.wait(), 1)
    try:
        assert sicherung.notfall_betroffene_kanaele() == [K_BAMBUS]
        ergebnis = await sicherung.notfall_stopp()
        assert ergebnis["sperr_kanaele"] == [K_BAMBUS]
    finally:
        client.freigabe.set()
        await asyncio.wait_for(start, 1)
        _raeume(sicherung)
    assert sicherung.notfall_betroffene_kanaele() == []   # Start abgeschlossen


async def test_route_sperrt_nach_was_der_stopp_als_betroffen_meldet(
    speicher, monkeypatch,
):
    """Gate B1, Nachsperren: begann der Start erst NACH dem Vorab-Lesen (hier
    simuliert: das Vorab-Lesen findet nichts), sperrt die Route den Kanal
    aus der Rueckgabe des Stopps."""
    client = _ClientOeffnenHaengt()
    _, sicherung, _ = _dienst(speicher, client)
    monkeypatch.setattr(sicherung, "notfall_betroffene_kanaele", lambda: [])
    try:
        antwort = await _notfall_waehrend_auto_start(client, sicherung)

        assert antwort["ok"] is True, antwort
        assert [g["kanal"] for g in antwort["gesperrte_kanaele"]] == [K_BAMBUS]
        sperren = await speicher.hole_offene_kanal_sperren()
        assert [(s["geraet_id"], s["kanal"]) for s in sperren] == [
            (GERAET, K_BAMBUS),
        ]
    finally:
        _raeume(sicherung)


class _EchterClientOhneNetz(GardenaClient):
    """Echter GardenaClient (Statuscache, Replay-Guard), Cloud-Kommandos als
    Attrappen -- kein Netz."""

    def __init__(self):
        super().__init__(client_id="t", client_secret="t")
        self.schliessen_aufrufe: list = []

    async def ventil_oeffnen(self, geraet_id, dauer_sekunden, valve_id=None):
        return None

    async def ventil_schliessen(self, geraet_id, valve_id=None):
        self.schliessen_aufrufe.append((geraet_id, valve_id))


async def test_fremdlauf_im_replay_guard_wird_geschlossen_und_gesperrt(speicher):
    """Gate B1 (Isomorphie T-0592): der Replay-Guard verwarf das OEFFNEN eines
    Fremdlaufs auf Kanal 3 (Realfall 06.09. magerwiese). `offene_valves()`
    kennt ihn nicht, der Statuscache schon. Der Stopp schliesst ihn
    vorsorglich -- gesperrt werden muss er auch."""
    client = _EchterClientOhneNetz()
    client._verarbeite_ventil_update(VALVES[K_HECKE], "Hecke", "CLOSED", "D")
    client._replay_guard_bis = datetime.now() + timedelta(minutes=5)
    client._verarbeite_ventil_update(
        VALVES[K_HECKE], "Hecke", "SCHEDULED_WATERING", "D",
    )
    assert client.offene_valves() == {}           # Guard hat verworfen
    _, sicherung, _ = _dienst(speicher, client)

    antwort = await api_server.notfall_stopp()

    assert (GERAET, VALVES[K_HECKE]) in client.schliessen_aufrufe
    assert [g["kanal"] for g in antwort["gesperrte_kanaele"]] == [K_HECKE]


async def test_statuscache_offen_steht_vor_und_nach_dem_schliessen_in_der_sperrmenge():
    """Gate B1: der Lauf-Beleg aus dem Statuscache zaehlt in beiden Quellen
    (vor dem Schliessen und Rueckgabe des Stopps) und bleibt in der
    Zaehlung "vorsorglich" (kein Eintrag in `offene_valves`)."""
    client = _EchterClientOhneNetz()
    client._verarbeite_ventil_update(VALVES[K_HECKE], "Hecke", "CLOSED", "D")
    client._replay_guard_bis = datetime.now() + timedelta(minutes=5)
    client._verarbeite_ventil_update(
        VALVES[K_HECKE], "Hecke", "SCHEDULED_WATERING", "D",
    )
    sicherung = VentilSicherung(
        client=client, speicher=SpeicherAttrappe(),  # type: ignore[arg-type]
        ventil_geraet_id=GERAET, kanal_zu_valve_id=dict(VALVES),
    )
    assert sicherung.notfall_betroffene_kanaele() == [K_HECKE]
    ergebnis = await sicherung.notfall_stopp()
    assert ergebnis["sperr_kanaele"] == [K_HECKE]
    assert ergebnis["vorsorglich_geschlossen"] == 2
    assert ergebnis["geschlossen"] == 0
