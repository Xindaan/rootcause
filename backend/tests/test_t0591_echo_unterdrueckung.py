"""T-0591: die Echo-Unterdrueckung nach dem eigenen Close ist an den Lauf
gebunden, befristet und entfaellt, wenn das Ventil laut Statuscache schon zu
ist.

Bis T-0591 setzte `_stoppe_unlocked` einen unbefristeten Schluessel auf die
valve_id. Schloss der Watchdog ein Ventil, das schon zu war (Cloud-Close vom
Replay-Guard verschluckt), kam kein Echo, der Schluessel blieb stehen und
verschluckte den echten Cloud-Close des naechsten Laufs (Gesamtreview HW-2,
Repro A). Jeder Test unten bildet einen Weg ab, auf dem ein ECHTES SCHLIESSEN
faelschlich als eigenes Echo verworfen wurde; der Testkoerper ist ohne den
jeweiligen Schutz harmlos (Attrappen-Client, Attrappen-Speicher, kein Netz).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from bewaesserung import ventil_sicherung as vs_modul
from bewaesserung.gardena_client import GardenaClient
from bewaesserung.modelle import Ausloser, VentilAktion, VentilEreignis
from bewaesserung.ventil_sicherung import VentilSicherung
from test_ventil_sicherung import ClientAttrappe, SpeicherAttrappe

GERAET = "dswc-1"
KANAL = 2
VALVE = "dswc-1:2"
ZONEN = ["bambuswald"]


class ClientMitStatuscache(ClientAttrappe):
    """Attrappe mit der schmalen Statuscache-Sicht aus T-0590."""

    def __init__(self, meldet_zu: bool, meldet_offen: bool = False):
        super().__init__()
        self.meldet_zu = meldet_zu
        self.meldet_offen = meldet_offen

    def meldet_ventil_zu_seit(self, valve_id: str, seit: datetime) -> bool:
        return self.meldet_zu

    def meldet_ventil_offen_seit(self, valve_id: str, seit: datetime) -> bool:
        return self.meldet_offen


class _EchterClientOhneNetz(GardenaClient):
    """Echter GardenaClient (Statuscache, WS-Pfad), nur die Cloud-Kommandos
    sind Attrappen -- kein Netz."""

    def __init__(self):
        super().__init__(client_id="test", client_secret="test")

    async def ventil_oeffnen(self, geraet_id, dauer_sekunden, valve_id=None):
        return None

    async def ventil_schliessen(self, geraet_id, valve_id=None):
        return None


def _ws(client: GardenaClient, aktivitaet: str) -> None:
    """Eine WS-Meldung ueber den echten Client-Pfad (ohne Callbacks)."""
    client._verarbeite_ventil_update(VALVE, "Bambus", aktivitaet, "DSWC")


def _sicherung(client) -> tuple[VentilSicherung, SpeicherAttrappe]:
    speicher = SpeicherAttrappe()
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET,
        kanal_zu_valve_id={KANAL: VALVE},
    )
    return sicherung, speicher


def _closed() -> VentilEreignis:
    return VentilEreignis(
        zeitstempel=datetime.now(), zone_id=ZONEN[0], ventil_id=VALVE,
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=300,
        ausloser=Ausloser.MANUELL,
    )


def _schliessen_events(speicher: SpeicherAttrappe) -> list[VentilEreignis]:
    return [e for e in speicher.ereignisse if e.aktion == VentilAktion.SCHLIESSEN]


@pytest.mark.asyncio
async def test_watchdog_close_auf_zues_ventil_verschluckt_folgelauf_nicht():
    """Repro A: Watchdog-Close ohne Echo, danach Folgelauf mit echtem CLOSED.

    Schuetzt die Lauf-Bindung: der Schluessel des Vorlaufs darf das SCHLIESSEN
    eines neuen eigenen Laufs nicht verschlucken (heute: Kanal bleibt aktiv,
    0 SCHLIESSEN, 30 s spaeter der naechste Watchdog).

    Gate B3: der Client hat das OPEN von Lauf 2 gemeldet -- nur dann gehoert
    das CLOSED Lauf 2 (sonst waere es das spaete Echo des Vorlaufs)."""
    client = ClientMitStatuscache(meldet_zu=False, meldet_offen=True)
    sicherung, speicher = _sicherung(client)

    # Lauf 1: Cloud-Close vom Replay-Guard verschluckt (kein Callback), der
    # Watchdog schliesst das schon zue Ventil -> kein Echo.
    assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.AUTOMATIK)
    assert await sicherung.stoppe(KANAL, Ausloser.WATCHDOG)

    # Lauf 2 startet, die Cloud schliesst ihn regulaer.
    assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.AUTOMATIK)
    speicher.ereignisse.clear()

    behandelt = await sicherung.verarbeite_callback(_closed())

    assert behandelt is True
    assert not sicherung.ist_aktiv(KANAL)
    schliessen = _schliessen_events(speicher)
    assert [e.ausloser for e in schliessen] == [Ausloser.AUTOMATIK]


@pytest.mark.asyncio
async def test_unterdrueckung_laeuft_ab_fremder_close_wird_gespeichert(monkeypatch):
    """Schuetzt die Ablaufzeit: ohne eigenen Folgelauf haelt die Lauf-Bindung
    nichts auf. Ein Fremdlauf (Gardena-App) auf demselben Ventil endet nach
    Ablauf der Frist mit CLOSED -- das muss als externes Event durchgehen
    (False = main.py speichert es), statt verworfen zu werden."""
    client = ClientAttrappe()
    sicherung, _ = _sicherung(client)
    assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.AUTOMATIK)
    assert await sicherung.stoppe(KANAL, Ausloser.WATCHDOG)

    basis = vs_modul.time.monotonic()
    monkeypatch.setattr(
        vs_modul.time, "monotonic",
        lambda: basis + vs_modul.CALLBACK_UNTERDRUECKUNG_S + 1,
    )

    assert await sicherung.verarbeite_callback(_closed()) is False


@pytest.mark.asyncio
async def test_statuscache_zu_setzt_keinen_schluessel():
    """Schuetzt AK2: meldet der Statuscache das Ventil seit Laufstart als zu,
    erzeugt der eigene Close kein Echo -- dann darf auch innerhalb der Frist
    kein fremder CLOSED desselben Ventils verschluckt werden."""
    client = ClientMitStatuscache(meldet_zu=True)
    sicherung, _ = _sicherung(client)
    assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.AUTOMATIK)
    assert await sicherung.stoppe(KANAL, Ausloser.WATCHDOG)

    assert await sicherung.verarbeite_callback(_closed()) is False


@pytest.mark.asyncio
async def test_echo_des_eigenen_close_bleibt_unterdrueckt():
    """Gegenstueck: meldet der Cache das Ventil noch offen, kommt das Echo
    innerhalb der Frist und wird wie bisher verworfen (kein Doppel-SCHLIESSEN)."""
    client = ClientMitStatuscache(meldet_zu=False)
    sicherung, speicher = _sicherung(client)
    assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.MANUELL)
    assert await sicherung.stoppe(KANAL, Ausloser.MANUELL)
    speicher.ereignisse.clear()

    assert await sicherung.verarbeite_callback(_closed()) is True
    assert speicher.ereignisse == []


@pytest.mark.asyncio
@pytest.mark.parametrize("wiederholung_vor_echo", [False, True])
async def test_spaetes_echo_nach_neustart_verschluckt_neuen_lauf_nicht(
    wiederholung_vor_echo,
):
    """Gate B3: bewaessere -> stoppe -> sofort bewaessere -> CLOSED.

    Das CLOSED kommt, bevor der Client das OPEN von Lauf 2 gemeldet hat: es
    ist das Echo des eigenen Close am Vorlauf. Vorher wurde es Lauf 2
    zugeschrieben -- `_aktiv` weg, Watchdog gecancelt, SCHLIESSEN mit ~0 s,
    das Ventil lief real ohne Watchdog weiter. Echter Client-Statuscache.
    `wiederholung_vor_echo`: eine unveraenderte Offen-Meldung (Attribut-
    Update des Vorlaufs) ist kein OPEN des neuen Laufs."""
    client = _EchterClientOhneNetz()
    sicherung, speicher = _sicherung(client)
    _ws(client, "CLOSED")                  # Erstsichtung
    assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.MANUELL)
    _ws(client, "MANUAL_WATERING")         # OPEN von Lauf 1
    assert await sicherung.stoppe(KANAL, Ausloser.MANUELL)
    assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.MANUELL)
    lauf2 = sicherung._aktiv[KANAL]
    if wiederholung_vor_echo:
        _ws(client, "MANUAL_WATERING")     # unveraendert, Vorlauf-Zustand
    speicher.ereignisse.clear()

    _ws(client, "CLOSED")                  # spaetes Echo von Lauf 1
    behandelt = await sicherung.verarbeite_callback(_closed())

    try:
        assert behandelt is True
        assert sicherung._aktiv.get(KANAL) is lauf2
        assert lauf2.timer_handle is not None
        assert not lauf2.timer_handle.cancelled()
        assert _schliessen_events(speicher) == []
    finally:
        if lauf2.timer_handle:
            lauf2.timer_handle.cancel()


@pytest.mark.asyncio
async def test_closed_nach_gemeldetem_open_gehoert_neuem_lauf():
    """Gegenstueck zu B3 (HW-2 bleibt behoben): hat der Client das OPEN von
    Lauf 2 gemeldet, ist ein CLOSED danach das Ende von Lauf 2, auch wenn der
    Schluessel des Vorlaufs noch steht. Echter Client-Statuscache: belegt,
    dass `meldet_ventil_offen_seit` den Wechsel nach dem Laufstart erkennt."""
    client = _EchterClientOhneNetz()
    sicherung, speicher = _sicherung(client)
    _ws(client, "CLOSED")
    assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.AUTOMATIK)
    _ws(client, "MANUAL_WATERING")         # OPEN von Lauf 1
    assert await sicherung.stoppe(KANAL, Ausloser.MANUELL)
    _ws(client, "CLOSED")                  # Echo kommt -- aber ohne Callback
    assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.AUTOMATIK)
    _ws(client, "MANUAL_WATERING")         # OPEN von Lauf 2
    speicher.ereignisse.clear()

    _ws(client, "CLOSED")                  # Cloud schliesst Lauf 2
    behandelt = await sicherung.verarbeite_callback(_closed())

    assert behandelt is True
    assert not sicherung.ist_aktiv(KANAL)
    assert [e.ausloser for e in _schliessen_events(speicher)] == [
        Ausloser.AUTOMATIK,
    ]


class _ClientOffenWirft(ClientAttrappe):
    def meldet_ventil_offen_seit(self, valve_id: str, seit: datetime) -> bool:
        raise TypeError("naive vs. aware (Attrappe)")


@pytest.mark.asyncio
@pytest.mark.parametrize("client_klasse", [ClientAttrappe, _ClientOffenWirft])
async def test_ohne_offen_beleg_wird_verschluckt(client_klasse):
    """Gate B3, Fehlerrichtung: fehlt die Statuscache-Sicht oder wirft sie,
    gilt das CLOSED als Echo des Vorlaufs. Der neue Lauf behaelt Watchdog
    und `_aktiv` (ein echter Close heilt ueber den Watchdog)."""
    sicherung, speicher = _sicherung(client_klasse())
    assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.MANUELL)
    assert await sicherung.stoppe(KANAL, Ausloser.MANUELL)
    assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.MANUELL)
    lauf2 = sicherung._aktiv[KANAL]
    try:
        assert await sicherung.verarbeite_callback(_closed()) is True
        assert sicherung._aktiv.get(KANAL) is lauf2
    finally:
        lauf2.timer_handle.cancel()
