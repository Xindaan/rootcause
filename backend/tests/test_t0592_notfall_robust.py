"""T-0592: Notfall-Stopp robust (Gesamtreview HW-3, LI-1, HW-5, LI-7) und
Nachtrag 30.09. (Oeffnen im Flug waehrend des Notfall-Stopps).

Alle Tests mit Attrappen-Client und Attrappen-Speicher, kein Netz, keine DB.
Jeder Testkoerper ist auch ohne den geprueften Schutz harmlos: es entstehen
nur Aufruflisten in der Attrappe.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from structlog.testing import capture_logs

from bewaesserung.modelle import Ausloser, VentilAktion, VentilEreignis
from bewaesserung.ventil_sicherung import VentilSicherung
from test_ventil_sicherung import ClientAttrappe, SpeicherAttrappe

GERAET = "dswc-1"
K1, K2 = 1, 2
V1, V2 = "dswc-1:1", "dswc-1:2"
Z1, Z2 = ["waldblumenhain"], ["bambuswald"]


class SpeicherSchliessenWirft(SpeicherAttrappe):
    """`database is locked` beim SCHLIESSEN-Write (Repro C/D/F)."""

    async def speichere_ventil_ereignis(self, ereignis: VentilEreignis) -> None:
        if ereignis.aktion == VentilAktion.SCHLIESSEN:
            raise RuntimeError("database is locked")
        await super().speichere_ventil_ereignis(ereignis)


class SpeicherBelegWirft(SpeicherAttrappe):
    async def existiert_schliessen_seit(self, *a, **kw) -> bool:
        raise RuntimeError("database is locked")


class ClientMitProtokoll(ClientAttrappe):
    """Haelt die Reihenfolge von Oeffnen und Schliessen fest; das Oeffnen
    kann an einem Event angehalten werden (Befehl unterwegs)."""

    def __init__(self):
        super().__init__()
        self.protokoll: list[tuple[str, str | None]] = []
        self.oeffnen_freigabe: asyncio.Event | None = None
        self.oeffnen_gestartet = asyncio.Event()

    async def ventil_oeffnen(self, geraet_id, dauer_sekunden, valve_id=None):
        self.oeffnen_gestartet.set()
        if self.oeffnen_freigabe is not None:
            await self.oeffnen_freigabe.wait()
        await super().ventil_oeffnen(geraet_id, dauer_sekunden, valve_id=valve_id)
        self.protokoll.append(("oeffnen", valve_id))

    async def ventil_schliessen(self, geraet_id, valve_id=None):
        await super().ventil_schliessen(geraet_id, valve_id=valve_id)
        self.protokoll.append(("schliessen", valve_id))


def _sicherung(client, speicher=None) -> VentilSicherung:
    return VentilSicherung(
        client=client, speicher=speicher or SpeicherAttrappe(),  # type: ignore[arg-type]
        ventil_geraet_id=GERAET,
        kanal_zu_valve_id={K1: V1, K2: V2},
    )


def _raeume(sicherung: VentilSicherung) -> None:
    for aktiv in sicherung._aktiv.values():
        if aktiv.timer_handle:
            aktiv.timer_handle.cancel()


# --- AK1: jedes gemappte Ventil ohne Vorbedingung ---------------------------


@pytest.mark.asyncio
async def test_notfall_schliesst_gemapptes_ventil_ohne_offen_beleg():
    """Repro E: Fremdlauf laeuft, aber der Replay-Guard hat sein OEFFNEN
    verworfen -- `offene_valves()` ist leer. Der Notfall-Stopp muss trotzdem
    einen Close-Befehl senden (heute: keiner, Rueckgabe "Erfolg")."""
    client = ClientAttrappe()
    client.offene = {}  # Guard-Luecke: Cache kennt den Fremdlauf nicht
    sicherung = _sicherung(client)

    ergebnis = await sicherung.notfall_stopp()

    assert sorted(client.schliessen_aufrufe) == [(GERAET, V1), (GERAET, V2)]
    assert ergebnis["fehlgeschlagen"] == []
    assert ergebnis["geschlossen"] == 0
    assert ergebnis["vorsorglich_geschlossen"] == 2


@pytest.mark.asyncio
async def test_rein_vorsorglicher_notfall_steht_im_log():
    """Gate B7: kein eigener Lauf, nichts bekannt offen, aber zwei Ventile
    vorsorglich geschlossen -- der Notfall-Stopp muss im Log stehen."""
    sicherung = _sicherung(ClientAttrappe())
    with capture_logs() as logs:
        ergebnis = await sicherung.notfall_stopp()
    assert ergebnis["vorsorglich_geschlossen"] == 2
    eintraege = [e for e in logs if e["event"] == "ventil.notfall_stopp"]
    assert len(eintraege) == 1
    assert eintraege[0]["vorsorglich_geschlossen"] == 2


# --- AK2/AK3: DB-Fehler im Schliesspfad -------------------------------------


@pytest.mark.asyncio
async def test_notfall_db_fehler_auf_kanal1_blockiert_kanal2_nicht():
    """Repro C/D: der SCHLIESSEN-Write von Kanal 1 wirft. Kanal 2 muss
    trotzdem geschlossen werden, und beide gelten als zu (die Cloud hat
    geschlossen). Heute: notfall_stopp wirft, Kanal 2 bleibt offen."""
    client = ClientAttrappe()
    sicherung = _sicherung(client, SpeicherSchliessenWirft())
    assert await sicherung.bewaessere(K1, Z1, 600, Ausloser.AUTOMATIK)
    assert await sicherung.bewaessere(K2, Z2, 600, Ausloser.AUTOMATIK)
    try:
        ergebnis = await sicherung.notfall_stopp()

        assert sorted(client.schliessen_aufrufe) == [(GERAET, V1), (GERAET, V2)]
        assert not sicherung.ist_aktiv(K1)
        assert not sicherung.ist_aktiv(K2)
        assert ergebnis["geschlossen"] == 2
        assert ergebnis["fehlgeschlagen"] == []
    finally:
        _raeume(sicherung)


@pytest.mark.asyncio
@pytest.mark.parametrize("eigene_laeufe", [True, False], ids=["eigen", "extern"])
async def test_notfall_isoliert_unerwartete_exception_je_kanal(
    monkeypatch, eigene_laeufe,
):
    """Zweite Schicht: wirft stoppe() auf Kanal 1 aus einem Grund, den der
    Schliesspfad selbst nicht abfaengt, wird Kanal 2 trotzdem geschlossen und
    Kanal 1 als Fehlschlag gemeldet statt die Schleife abzubrechen. Getrennt
    fuer die Schleife ueber eigene Laeufe und die ueber gemappte Ventile."""
    client = ClientAttrappe()
    sicherung = _sicherung(client)
    if eigene_laeufe:
        assert await sicherung.bewaessere(K1, Z1, 600, Ausloser.AUTOMATIK)
        assert await sicherung.bewaessere(K2, Z2, 600, Ausloser.AUTOMATIK)
    echtes_stoppe = sicherung.stoppe

    async def stoppe_k1_wirft(kanal, ausloser):
        if kanal == K1:
            raise RuntimeError("unerwartet")
        return await echtes_stoppe(kanal, ausloser)

    monkeypatch.setattr(sicherung, "stoppe", stoppe_k1_wirft)
    try:
        ergebnis = await sicherung.notfall_stopp()

        assert (GERAET, V2) in client.schliessen_aufrufe
        assert not sicherung.ist_aktiv(K2)
        assert [f["kanal"] for f in ergebnis["fehlgeschlagen"]] == [K1]
        assert ergebnis["fehlgeschlagen"][0]["fehler"] == "unerwartet"
    finally:
        _raeume(sicherung)


@pytest.mark.asyncio
async def test_close_fehler_mit_beleg_lesefehler_behaelt_retry_timer():
    """HW-5b: der Close scheitert, danach wirft die Beleg-Query. Der Watchdog
    ist schon gecancelt; ohne Retry-Timer haette der Lauf keinen Schutz mehr.
    Heute: stoppe wirft, kein Timer."""
    client = ClientAttrappe(schliessen_fehler=True)
    sicherung = _sicherung(client, SpeicherBelegWirft())
    assert await sicherung.bewaessere(K1, Z1, 600, Ausloser.AUTOMATIK)
    try:
        assert await sicherung.stoppe(K1, Ausloser.MANUELL) is False

        aktiv = sicherung._aktiv[K1]
        assert aktiv.timer_handle is not None
        assert not aktiv.timer_handle.cancelled()
    finally:
        _raeume(sicherung)


@pytest.mark.asyncio
async def test_callback_schreibfehler_raeumt_lauf_trotzdem():
    """Repro F: die Cloud meldet CLOSED, der SCHLIESSEN-Write wirft. Der
    Watchdog ist gecancelt; bleibt `_aktiv` stehen, lehnt bewaessere den
    Kanal bis zum Neustart ab. Heute: Kanal aktiv, naechster Start False."""
    client = ClientAttrappe()
    sicherung = _sicherung(client, SpeicherSchliessenWirft())
    assert await sicherung.bewaessere(K2, Z2, 600, Ausloser.AUTOMATIK)
    try:
        await sicherung.verarbeite_callback(VentilEreignis(
            zeitstempel=datetime.now(), zone_id=Z2[0], ventil_id=V2,
            aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
            ausloser=Ausloser.MANUELL,
        ))

        assert not sicherung.ist_aktiv(K2)
        assert await sicherung.bewaessere(K2, Z2, 600, Ausloser.AUTOMATIK)
    finally:
        _raeume(sicherung)


@pytest.mark.asyncio
async def test_watchdog_task_protokolliert_exception(monkeypatch):
    """LI-7b: der Watchdog-Task laeuft ohne Referenz. Eine Exception darf
    nicht nur als 'Task exception was never retrieved' im stderr landen."""
    sicherung = _sicherung(ClientAttrappe())
    assert await sicherung.bewaessere(K1, Z1, 600, Ausloser.AUTOMATIK)

    async def stoppe_wirft(kanal, ausloser):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sicherung, "stoppe", stoppe_wirft)
    try:
        with capture_logs() as logs:
            await sicherung._watchdog_schliessen(K1)

        assert any(
            e["event"] == "ventil.watchdog_schliessen_fehler" for e in logs
        )
    finally:
        _raeume(sicherung)


# --- Nachtrag 30.09.: Oeffnen im Flug waehrend des Notfall-Stopps -----------


@pytest.mark.asyncio
async def test_notfall_waehrend_oeffnen_schliesst_ventil_danach():
    """Der Oeffnen-Befehl ist unterwegs, als der Notfall-Stopp laeuft. Der
    Stopp sieht den Lauf nicht (`_aktiv` steht noch nicht), sein Close kann
    vor dem Oeffnen ankommen. Danach muss das Ventil trotzdem zu sein.
    Heute: bewaessere meldet True, der Kanal laeuft nach dem Notfall weiter."""
    client = ClientMitProtokoll()
    client.oeffnen_freigabe = asyncio.Event()
    sicherung = _sicherung(client)

    start = asyncio.create_task(
        sicherung.bewaessere(K1, Z1, 600, Ausloser.MANUELL)
    )
    await asyncio.wait_for(client.oeffnen_gestartet.wait(), 1)
    await sicherung.notfall_stopp()
    client.oeffnen_freigabe.set()
    try:
        gestartet = await asyncio.wait_for(start, 1)

        assert gestartet is False
        assert not sicherung.ist_aktiv(K1)
        k1 = [a for a, v in client.protokoll if v == V1]
        # Das letzte Kommando an dieses Ventil ist ein Schliessen NACH dem
        # Oeffnen (das Schliessen des Notfalls davor zaehlt nicht).
        assert k1[-1] == "schliessen"
        assert "oeffnen" in k1
    finally:
        _raeume(sicherung)


@pytest.mark.asyncio
async def test_notfall_vor_oeffnen_verhindert_den_oeffnen_befehl():
    """Der Start wartet noch auf die Reservierung (ein anderer Starter haelt
    sie), als der Notfall-Stopp laeuft. Danach darf er gar nicht mehr
    oeffnen -- auch nicht kurz, um gleich wieder zu schliessen."""
    client = ClientMitProtokoll()
    sicherung = _sicherung(client)
    reservierung = sicherung._reservierung(K1)
    await reservierung.acquire()

    start = asyncio.create_task(
        sicherung.bewaessere(K1, Z1, 600, Ausloser.MANUELL)
    )
    await asyncio.sleep(0)  # Start haengt jetzt an der Reservierung
    await sicherung.notfall_stopp()
    reservierung.release()
    try:
        gestartet = await asyncio.wait_for(start, 1)

        assert gestartet is False
        assert client.oeffnen_aufrufe == []
        assert not sicherung.ist_aktiv(K1)
    finally:
        _raeume(sicherung)


@pytest.mark.asyncio
async def test_start_nach_notfall_oeffnet_normal():
    """Gegenprobe: ein Start, der NACH dem Notfall-Stopp beginnt, ist eine
    neue Handlung und oeffnet wie gewohnt."""
    client = ClientMitProtokoll()
    sicherung = _sicherung(client)
    await sicherung.notfall_stopp()
    try:
        assert await sicherung.bewaessere(K1, Z1, 600, Ausloser.MANUELL)
        assert sicherung.ist_aktiv(K1)
    finally:
        _raeume(sicherung)
