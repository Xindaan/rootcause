"""T-0610 W8 (AUFWAND, Sicherheits-/Startpfad): Verhaltenstests.

E4: `startup_check` und `recover_aus_db` behandeln jeden Kanal einzeln. Ein
DB-Schreibfehler bei einem Kanal darf die uebrigen Kanaele nicht von der
Behandlung ausschliessen (Muster T-0592 im Notfall-Stopp).

E14: Der Pre-Soak-Tick laedt die Frost-Vorhersage VOR dem Manager-Lock. Haengt
Open-Meteo (Timeout 10 s je Standort), darf `notfall_abbrechen` nicht darauf
warten. Die Frostsperre selbst prueft weiter unter dem Lock vor dem Oeffnen
(abgedeckt durch test_t0611_frostsperre).
"""

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta

import httpx
import pytest

from bewaesserung.frostsperre import FrostPruefer
from bewaesserung.modelle import (
    Ausloser,
    VentilAktion,
    WetterStunde,
    WetterVorhersage,
)
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import VentilSicherung
from bewaesserung.wetter import WetterClient

from test_t0612_kanal_sperre import (  # noqa: F401  (speicher = Fixture)
    K_BAMBUS,
    VALVES,
    _dienst,
    _geoeffnete_valves,
    _raeume,
    _starte_auto_sequenz,
    speicher,
)
from test_ventil_sicherung import ClientAttrappe

GERAET = "dswc-w8"


async def _zwei_states(sp: Speicher, gestartet: datetime, dauer_s: int) -> None:
    for kanal in (1, 2):
        await sp.setze_live_lauf_state(
            kanal=kanal, valve_id=f"valve-{kanal}", geraet_id=GERAET,
            zone_ids=[f"zone-{kanal}"], dauer_sekunden=dauer_s,
            ausloser="manuell", gestartet_am=gestartet,
        )


def _erster_schreibversuch_scheitert(sp: Speicher) -> list[str]:
    """Erster `speichere_ventil_ereignis`-Aufruf wirft (DB gesperrt).

    Gibt die Liste zurueck, in die die Zone des gescheiterten Aufrufs
    eingetragen wird -- unabhaengig von der Reihenfolge der States.
    """
    original = sp.speichere_ventil_ereignis
    gescheitert: list[str] = []

    async def _schreiben(ereignis):
        if not gescheitert:
            gescheitert.append(ereignis.zone_id)
            raise sqlite3.OperationalError("database is locked")
        return await original(ereignis)

    sp.speichere_ventil_ereignis = _schreiben  # type: ignore[method-assign]
    return gescheitert


async def _schliessen_der_zone(sp: Speicher, zone_id: str) -> list:
    return [
        e for e in await sp.hole_ventil_ereignisse(zone_id)
        if e.aktion == VentilAktion.SCHLIESSEN
    ]


def _sicherung(sp: Speicher) -> VentilSicherung:
    return VentilSicherung(
        client=ClientAttrappe(), speicher=sp,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET,
        kanal_zu_valve_id={1: "valve-1", 2: "valve-2"},
    )


@pytest.mark.asyncio
async def test_e4_startup_check_db_fehler_kanal_a_behandelt_kanal_b(tmp_path):
    sp = Speicher(str(tmp_path / "e4_startup.db"))
    await sp.verbinden()
    try:
        # Cloud meldet beide Ventile zu (ClientAttrappe.offene leer) ->
        # Crash-Recovery-Zweig: synthetisches SCHLIESSEN + State raeumen.
        await _zwei_states(sp, datetime.now() - timedelta(minutes=10), 600)
        gescheitert = _erster_schreibversuch_scheitert(sp)

        await _sicherung(sp).startup_check()   # darf nicht werfen

        assert len(gescheitert) == 1
        andere = "zone-2" if gescheitert[0] == "zone-1" else "zone-1"
        assert len(await _schliessen_der_zone(sp, andere)) == 1
        rest = await sp.hole_live_lauf_states(GERAET)
        # Nur der State des gescheiterten Kanals bleibt fuer den naechsten Start.
        assert [s["zone_ids"] for s in rest] == [[gescheitert[0]]]
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_e4_recover_aus_db_db_fehler_kanal_a_behandelt_kanal_b(tmp_path):
    sp = Speicher(str(tmp_path / "e4_recover.db"))
    await sp.verbinden()
    try:
        # Beide States abgelaufen -> recover_aus_db traegt das SCHLIESSEN nach.
        await _zwei_states(sp, datetime.now() - timedelta(hours=2), 300)
        gescheitert = _erster_schreibversuch_scheitert(sp)

        n = await _sicherung(sp).recover_aus_db()   # darf nicht werfen

        assert n == 0
        assert len(gescheitert) == 1
        andere = "zone-2" if gescheitert[0] == "zone-1" else "zone-1"
        assert len(await _schliessen_der_zone(sp, andere)) == 1
        rest = await sp.hole_live_lauf_states(GERAET)
        assert [s["zone_ids"] for s in rest] == [[gescheitert[0]]]
    finally:
        await sp.schliessen()


# --- E14: Frost-Abruf nicht unter dem Pre-Soak-Lock ---------------------------


@pytest.mark.asyncio
async def test_e14_notfall_abbrechen_wartet_nicht_auf_haengenden_frostabruf(speicher):
    """Hauptdose faellig, Wetter-Cache abgelaufen, Open-Meteo haengt. Der
    Notfall-Abbruch der Sequenz muss sofort durchgehen (< 1 s) und danach
    darf die Hauptdose nicht mehr oeffnen."""
    client = ClientAttrappe()
    _, sicherung, mgr = _dienst(speicher, client)
    jetzt0 = datetime.now()
    basis = jetzt0.replace(minute=0, second=0, microsecond=0)
    wetter = WetterClient(52.0, 13.0, standort_id="w8-test")
    # Warmer, gueltiger Cache: der Puls beim Start oeffnet ohne Netz.
    wetter._cache = WetterVorhersage(
        abfrage_zeitstempel=jetzt0,
        stunden=[
            WetterStunde(zeitstempel=basis + timedelta(hours=h), temperatur=10.0)
            for h in range(-1, 48)
        ],
    )
    wetter._cache_gueltig_bis = jetzt0 + timedelta(minutes=30)
    sicherung.setze_frost_pruefer(FrostPruefer(lambda _zid: wetter, 2.0))

    abruf_laeuft = asyncio.Event()
    freigabe = asyncio.Event()

    async def _haengender_abruf():
        abruf_laeuft.set()
        await freigabe.wait()
        raise httpx.ConnectTimeout("Open-Meteo haengt (Test)")

    try:
        t0 = await _starte_auto_sequenz(mgr)
        assert _geoeffnete_valves(client) == [VALVES[K_BAMBUS]]  # Puls lief
        assert await sicherung.stoppe(K_BAMBUS, Ausloser.AUTOMATIK)
        vorher = len(client.oeffnen_aufrufe)

        wetter._hole_von_api = _haengender_abruf  # type: ignore[method-assign]
        wetter._cache_gueltig_bis = datetime.now() - timedelta(seconds=1)
        tick = asyncio.create_task(mgr.tick(t0 + timedelta(minutes=31)))
        await asyncio.wait_for(abruf_laeuft.wait(), 2)

        start = time.monotonic()
        try:
            abgebrochen = await asyncio.wait_for(
                mgr.notfall_abbrechen("test"), timeout=1.0,
            )
        finally:
            dauer = time.monotonic() - start
            freigabe.set()
            await asyncio.wait_for(tick, 5)
        assert dauer < 1.0
        assert abgebrochen == ["bambuswald"]
        # Nach dem Abbruch oeffnet der Tick nichts mehr.
        assert _geoeffnete_valves(client, vorher) == []
        assert not sicherung.ist_aktiv(K_BAMBUS)
    finally:
        freigabe.set()
        _raeume(sicherung)


# --- E25: kein 3600-s-Deckel im Startup-Close ---------------------------------


@pytest.mark.asyncio
async def test_e25_startup_close_bucht_langen_lauf_mit_geplanter_dauer(tmp_path):
    """90-min-Lauf, Dienst 2 h weg, Cloud meldet zu: die Cloud hat nach der
    kommandierten Dauer geschlossen (Log 29.08.: 5400 s -> Close nach 5396 s).
    Das synthetische SCHLIESSEN muss 5400 s tragen, nicht 3600 s."""
    sp = Speicher(str(tmp_path / "e25.db"))
    await sp.verbinden()
    try:
        await sp.setze_live_lauf_state(
            kanal=1, valve_id="valve-1", geraet_id=GERAET,
            zone_ids=["zone-1"], dauer_sekunden=5400,
            ausloser="manuell",
            gestartet_am=datetime.now() - timedelta(hours=2),
        )
        await _sicherung(sp).startup_check()
        dauern = [e.dauer_sekunden for e in await _schliessen_der_zone(sp, "zone-1")]
        assert dauern == [5400]
    finally:
        await sp.schliessen()
