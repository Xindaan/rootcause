"""T-0607 (Gesamtreview AT-2): Verhaltenstests fuer den Kanal-Block des
Entscheidungsloops.

Befund: Die vier Sicherheits-Waechter im Kanal-Block von `_entscheidungsloop`
waren nirgends an der Stelle getestet, an der sie verdrahtet sind. Jeder
einzeln entfernt liess die volle Suite gruen:

- (a) globaler Aus-Schalter `if not auto_loop_aktiv: continue`,
- (b) Gate `if e.soll_bewaessern and kanal_sicherung:` vor dem Oeffnen,
- (c) sensorbasierter Max-Stop eines laufenden Einzellaufs,
- (d) sensorbasierter Max-Stop einer offenen Pre-Soak-Phase.

Aufbau (Wirkung statt Verkabelung): der ECHTE Loop faehrt genau eine Runde mit
einer scharfen Zone (modus automatik, ventil_kanal, auto_loop_opt_in).
Echte `VentilSicherung` mit `ClientAttrappe` (kein Netz, keine Hardware),
echter `Speicher` auf einer temporaeren DB, echter `PreSoakManager` in den
Modul-Globals von `api_server`, wie ihn der Dienst verdrahtet. Nur die
Entscheidungsengine ist eine Attrappe: sie liefert die Eingaben (Bedarf,
Max-Stop-Urteil), das Oeffnen und Schliessen passiert im echten Code.
Gemessen wird, was am Client ankommt und welchen Zustand Sicherung und
Pre-Soak-Manager danach haben.

Jeder Waechter-Fall hat einen Gegenfall mit sonst identischem Aufbau. Er
belegt, dass die Assertion im Waechter-Fall nicht aus einem anderen Grund
haelt (Zone nicht scharf, Sicherung lehnt ab, Max-Stop nie gefragt).

Harmlos auch ohne die Waechter: die Cloud-Kommandos sind Attrappen, die DB
liegt in tmp_path, und `pruefe_alle_zonen` der Attrappe setzt das Stop-Event,
sodass der Loop nach einer Runde endet.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import bewaesserung.api_server as api_server
from bewaesserung import main as M
from bewaesserung.modelle import (
    Ausloser,
    BewaesserungsEntscheidung,
    EntscheidungsScope,
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.pre_soak import PreSoakManager
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import VentilSicherung
from test_ventil_sicherung import ClientAttrappe

DSWC = "dswc-t0607"
KANAL = 2
ZONE = "strang"


def _konfig(*, ventilsteuerung_aktiv: bool, pre_soak: bool) -> GesamtKonfig:
    """Eine scharfe Zone: alle drei Stufen der Scharfschaltung gesetzt."""
    extra = (
        {"pre_soak_modus": "immer", "pre_soak_min": 5, "pre_soak_pause_min": 30}
        if pre_soak else {}
    )
    zone = ZonenKonfig(
        zone_id=ZONE, name="Strang", modus=ZonenModus.AUTOMATIK,
        ventil_kanal=KANAL, ventil_geraet_id=DSWC, auto_loop_opt_in=True,
        **extra,
    )
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[zone],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.5, laenge=13.4)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        ventilsteuerung_aktiv=ventilsteuerung_aktiv,
    )


class _MotorAttrappe:
    """Liefert nur die Eingaben des Kanal-Blocks und zaehlt die Fragen.

    `pruefe_alle_zonen` laeuft direkt NACH dem Kanal-Block; dort wird das
    Stop-Event gesetzt, damit der Loop nach dieser einen Runde endet.
    Unbekannte Methoden fehlen absichtlich: ein AttributeError landet als
    `entscheidungsloop.fehler` im Log und laesst den Test scheitern.
    """

    def __init__(
        self, stop: asyncio.Event, *, soll_bewaessern: bool = False,
        dauer_s: int = 0, max_stop: bool = False,
    ):
        self._stop = stop
        self._soll = soll_bewaessern
        self._dauer = dauer_s
        self._max_stop = max_stop
        self.kanal_fragen: list[int] = []
        self.max_stop_fragen: list[int] = []

    async def pruefe_kanal(self, kanal, zonen):
        self.kanal_fragen.append(kanal)
        return BewaesserungsEntscheidung(
            zeitstempel=datetime.now(), zone_id=zonen[0].zone_id,
            soll_bewaessern=self._soll, dauer_sekunden=self._dauer,
            begruendung="Attrappe", scope=EntscheidungsScope.KANAL,
            scope_ref=str(kanal),
        )

    async def pruefe_kanal_max_stop(self, kanal, zonen, lauf_start=None):
        self.max_stop_fragen.append(kanal)
        if self._max_stop:
            return True, "Attrappe: Sensor ueber Max"
        return False, "Attrappe: weiter"

    async def pruefe_alle_zonen(self):
        self._stop.set()
        return []

    async def prognostiziere_bewaesserung(self, zone_id):
        return None, ""


@pytest.fixture
async def speicher(tmp_path):
    sp = Speicher(str(tmp_path / "t0607.db"))
    await sp.verbinden()
    try:
        yield sp
    finally:
        await sp.schliessen()


@pytest.fixture
def anlage(speicher, monkeypatch):
    """Sicherung und Pre-Soak-Manager wie im Dienst: ein Manager je DSWC in
    `api_server._laufzeit.pre_soak_managers`, der erste zugleich als Primary."""
    client = ClientAttrappe()
    sicherung = VentilSicherung(
        client=client,  # type: ignore[arg-type]
        speicher=speicher, ventil_geraet_id=DSWC,
    )
    mgr = PreSoakManager(sicherung, speicher=speicher)
    monkeypatch.setattr(api_server._laufzeit, "pre_soak_managers", {DSWC: mgr})
    monkeypatch.setattr(api_server._laufzeit, "pre_soak_manager", mgr)
    monkeypatch.setattr(M, "ERSTE_PRUEFUNG_VERZOEGERUNG_S", 0.01)
    yield client, sicherung, mgr
    for aktiv in sicherung._aktiv.values():
        if aktiv.timer_handle:
            aktiv.timer_handle.cancel()


async def _eine_runde(konfig, motor, speicher, sicherung, stop, capsys) -> str:
    """Faehrt den echten Loop genau eine Runde und prueft, dass sie komplett
    und ohne Ausnahme durchlief (sonst waere jedes "nichts passiert" wertlos)."""
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
    return aus


# --- (a) globaler Aus-Schalter ---------------------------------------------


@pytest.mark.parametrize("aktiv", [False, True], ids=["aus", "gegenfall_an"])
async def test_aus_schalter_oeffnet_nichts(anlage, speicher, capsys, aktiv):
    """`ventilsteuerung_aktiv=false`: die Engine meldet Bedarf, die Zone ist
    scharf, eine Sicherung ist da (sie wird fuer manuelle Aktionen immer
    gebaut) -- trotzdem darf der Auto-Loop nichts oeffnen. Gegenfall: mit
    Schalter an oeffnet derselbe Aufbau genau einmal."""
    client, sicherung, _ = anlage
    stop = asyncio.Event()
    motor = _MotorAttrappe(stop, soll_bewaessern=True, dauer_s=600)

    await _eine_runde(
        _konfig(ventilsteuerung_aktiv=aktiv, pre_soak=False),
        motor, speicher, sicherung, stop, capsys,
    )

    if aktiv:
        assert [d for _, d, _ in client.oeffnen_aufrufe] == [600]
        assert sicherung.ist_aktiv(KANAL)
    else:
        assert client.oeffnen_aufrufe == []
        assert not sicherung.ist_aktiv(KANAL)


# --- (b) soll_bewaessern-Gate ------------------------------------------------


@pytest.mark.parametrize(
    "bedarf", [False, True], ids=["kein_bedarf", "gegenfall_bedarf"],
)
async def test_ohne_bedarf_startet_kein_pre_soak(anlage, speicher, capsys, bedarf):
    """Engine sagt "kein Bedarf" (wie im Regelbetrieb mit Dauer 0): der Loop
    darf weder oeffnen noch eine Pre-Soak-Sequenz starten.

    Bewusst eine Pre-Soak-Zone: ohne das Gate rundet der Loop die Dauer 0 auf
    eine Hauptdose von 1 min auf und der Puls oeffnet. Im Einzellauf faengt
    `VentilSicherung.bewaessere` die Dauer 0 zusaetzlich ab. Gegenfall: mit
    Bedarf startet derselbe Aufbau die Sequenz und der Puls oeffnet."""
    client, sicherung, mgr = anlage
    stop = asyncio.Event()
    motor = _MotorAttrappe(
        stop, soll_bewaessern=bedarf, dauer_s=3600 if bedarf else 0,
    )

    await _eine_runde(
        _konfig(ventilsteuerung_aktiv=True, pre_soak=True),
        motor, speicher, sicherung, stop, capsys,
    )

    assert motor.kanal_fragen == [KANAL]     # das Gate hatte etwas zu urteilen
    if bedarf:
        assert [d for _, d, _ in client.oeffnen_aufrufe] == [5 * 60]
        assert mgr.ist_aktiv(ZONE)
    else:
        assert client.oeffnen_aufrufe == []
        assert mgr.laufender_lauf(ZONE) is None
        assert not sicherung.ist_aktiv(KANAL)


# --- (c) Max-Stop eines laufenden Einzellaufs --------------------------------


@pytest.mark.parametrize(
    "max_stop", [True, False], ids=["max_stop", "gegenfall_weiter"],
)
async def test_einzellauf_max_stop_schliesst(anlage, speicher, capsys, max_stop):
    """Ein laufender Einzellauf und der Sensor meldet "ueber Max": der Loop
    muss das Ventil schliessen. Gegenfall: ohne Max-Stop bleibt es offen --
    die Runde schliesst also nicht aus einem anderen Grund."""
    client, sicherung, _ = anlage
    assert await sicherung.bewaessere(KANAL, [ZONE], 1800, Ausloser.AUTOMATIK)
    stop = asyncio.Event()
    motor = _MotorAttrappe(stop, max_stop=max_stop)

    await _eine_runde(
        _konfig(ventilsteuerung_aktiv=True, pre_soak=False),
        motor, speicher, sicherung, stop, capsys,
    )

    assert motor.max_stop_fragen == [KANAL]
    assert len(client.oeffnen_aufrufe) == 1  # nur der Start oben
    if max_stop:
        assert len(client.schliessen_aufrufe) == 1
        assert not sicherung.ist_aktiv(KANAL)
    else:
        assert client.schliessen_aufrufe == []
        assert sicherung.ist_aktiv(KANAL)


# --- (d) Max-Stop einer offenen Pre-Soak-Phase -------------------------------


@pytest.mark.parametrize(
    "max_stop", [True, False], ids=["max_stop", "gegenfall_weiter"],
)
async def test_pre_soak_max_stop_bricht_sequenz_ab(
    anlage, speicher, capsys, max_stop,
):
    """Automatik-Sequenz im offenen Puls und der Sensor meldet "ueber Max":
    der Loop muss die Sequenz ueber den Pre-Soak-Manager abbrechen (sonst
    folgt die Hauptdose) und das Ventil schliessen. Gegenfall: ohne Max-Stop
    laufen Sequenz und Puls weiter."""
    client, sicherung, mgr = anlage
    ok, fehler = await mgr.starte(
        ZONE, KANAL, [ZONE], pre_soak_min=5, pause_min=30, haupt_min=60,
        ausloser=Ausloser.AUTOMATIK,
    )
    assert ok, fehler
    assert sicherung.ist_aktiv(KANAL)            # Puls offen
    stop = asyncio.Event()
    motor = _MotorAttrappe(stop, max_stop=max_stop)

    await _eine_runde(
        _konfig(ventilsteuerung_aktiv=True, pre_soak=True),
        motor, speicher, sicherung, stop, capsys,
    )

    assert motor.max_stop_fragen == [KANAL]
    assert motor.kanal_fragen == []              # laufender Kanal: kein Neustart
    if max_stop:
        assert not mgr.ist_aktiv(ZONE)
        assert len(client.schliessen_aufrufe) == 1
        assert not sicherung.ist_aktiv(KANAL)
    else:
        assert mgr.ist_aktiv(ZONE)
        assert client.schliessen_aufrufe == []
        assert sicherung.ist_aktiv(KANAL)
