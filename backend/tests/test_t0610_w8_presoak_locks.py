"""T-0610 E5: Verhaltenstests fuer die Manager-Locks im PreSoakManager.

`starte`, `stoppe` und `recover_aus_db` nehmen `self._lock`, damit keine
zweite Operation in einen Lauf greift, waehrend dessen `bewaessere()` auf die
Cloud wartet. Jeder Test haelt genau dieses Warten mit einem Ereignis an
(`ClientMitHaengendemOeffnen`) und laesst die Gegenoperation parallel laufen.
Ohne den jeweiligen Lock endet sie vor der Cloud-Antwort; danach steht ein
Ventil offen bzw. ein abgebrochener Lauf wieder in der DB, obwohl die Sequenz
als beendet gilt.

Der Lock in `notfall_abbrechen` ist in test_t0589_notfall_presoak.py (f)
abgedeckt. Testkoerper harmlos: Attrappen-Client, temporaere DB, kein Netz.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import bewaesserung.api_server as api_server
from bewaesserung.modelle import Ausloser
from test_t0589_notfall_presoak import (  # noqa: F401  (speicher = Fixture)
    BAMBUS_ZONEN,
    KANAL_BAMBUS,
    ClientMitHaengendemOeffnen,
    _dienst,
    _in_pause_bringen,
    _starte_sequenz,
    speicher,
)

# Gibt einer Gegenoperation OHNE Lock Gelegenheit, vor der Cloud-Antwort
# fertig zu werden (sie braucht wenige ms fuer den DB-Zugriff). Mit Lock
# blockiert sie bis zur Freigabe; das Ergebnis haengt nicht an dieser Zeit.
_GELEGENHEIT_S = 0.3


def _raeume(sicherung) -> None:
    for aktiv in sicherung._aktiv.values():
        if aktiv.timer_handle:
            aktiv.timer_handle.cancel()


async def test_e5_stoppe_waehrend_hauptdosen_open_laesst_kein_ventil_offen(speicher):
    """Tick haengt im Open der Hauptdose, der User stoppt die Sequenz.
    Richtig wartet `stoppe` auf den Tick und schliesst dann das gerade
    geoeffnete Ventil. Ohne Lock sieht `stoppe` noch kein aktives Ventil,
    meldet "abgebrochen", und der Tick setzt danach `haupt` -- 60 min Wasser
    nach dem Stopp."""
    client = ClientMitHaengendemOeffnen()
    sicherung, mgr = _dienst(speicher, client)
    t0 = await _starte_sequenz(mgr)
    await _in_pause_bringen(sicherung, mgr, t0)
    client.haengen = True
    try:
        tick = asyncio.create_task(mgr.tick(t0 + timedelta(minutes=31)))
        await asyncio.wait_for(client.open_angefragt.wait(), 5)
        stopp = asyncio.create_task(mgr.stoppe("bambuswald"))
        await asyncio.wait({stopp}, timeout=_GELEGENHEIT_S)
        client.cloud_antwort.set()
        ok, fehler = await asyncio.wait_for(stopp, 5)
        await asyncio.wait_for(tick, 5)

        assert len(client.oeffnen_aufrufe) == 2  # Puls + Hauptdose kamen an
        assert ok, fehler
        assert not sicherung.ist_aktiv(KANAL_BAMBUS), (
            "Hauptdose nach dem User-Stopp offen geblieben"
        )
        assert mgr.laufender_lauf("bambuswald").phase == "fehler"
        assert await speicher.hole_pre_soak_states() == []
    finally:
        client.cloud_antwort.set()
        _raeume(sicherung)


async def test_e5_stoppe_waehrend_puls_open_aus_starte_laesst_kein_ventil_offen(
    speicher,
):
    """`starte` haengt im Open des Pulses, der User stoppt sofort. Richtig
    wartet `stoppe` auf `starte` und schliesst den Puls. Ohne den Lock in
    `starte` findet `stoppe` den Lauf schon (er steht vor dem ersten await),
    aber noch kein aktives Ventil: "abgebrochen", danach oeffnet der Puls."""
    client = ClientMitHaengendemOeffnen()
    sicherung, mgr = _dienst(speicher, client)
    client.haengen = True
    try:
        start = asyncio.create_task(mgr.starte(
            zone_id="bambuswald", kanal=KANAL_BAMBUS,
            zone_ids_kanal=list(BAMBUS_ZONEN),
            pre_soak_min=5, pause_min=30, haupt_min=60,
            ausloser=Ausloser.AUTOMATIK,
        ))
        await asyncio.wait_for(client.open_angefragt.wait(), 5)
        stopp = asyncio.create_task(mgr.stoppe("bambuswald"))
        await asyncio.wait({stopp}, timeout=_GELEGENHEIT_S)
        client.cloud_antwort.set()
        await asyncio.wait_for(start, 5)
        ok, fehler = await asyncio.wait_for(stopp, 5)

        assert len(client.oeffnen_aufrufe) == 1  # der Puls kam an
        assert ok, fehler
        assert not sicherung.ist_aktiv(KANAL_BAMBUS), (
            "Puls nach dem User-Stopp offen geblieben"
        )
        assert mgr.laufender_lauf("bambuswald").phase == "fehler"
        assert await speicher.hole_pre_soak_states() == []
    finally:
        client.cloud_antwort.set()
        _raeume(sicherung)


async def test_e5_notfall_waehrend_recover_hauptdose_laesst_nichts_stehen(
    speicher,
):
    """Neustart: `recover_aus_db` zieht die faellige Hauptdose nach und haengt
    im Open, der Notfall-Stopp kommt parallel. Richtig wartet der Abbruch auf
    die Wiederaufnahme, danach schliesst die Route das Ventil und nichts
    bleibt fuer den naechsten Start stehen. Ohne den Lock in `recover_aus_db`
    bricht die Route vorher ab; das Ventil schliesst dann zwar der
    Notfall-Waechter in `bewaessere` (T-0592), aber der weiterlaufende Tick
    schreibt den abgebrochenen Lauf wieder in die DB (gemessen in der
    Negativprobe, W8)."""
    client = ClientMitHaengendemOeffnen()
    sicherung, mgr = _dienst(speicher, client)
    await speicher.setze_pre_soak_state(
        zone_id="bambuswald", kanal=KANAL_BAMBUS,
        zone_ids_kanal=list(BAMBUS_ZONEN),
        pre_soak_s=300, pause_s=1800, haupt_s=3600,
        gestartet_am=datetime.now() - timedelta(minutes=31), phase="pause",
        ausloser=Ausloser.AUTOMATIK.value, haupt_pulse=1, haupt_pause_s=0,
        haupt_pulse_gestartet=0,
    )
    client.haengen = True
    try:
        recover = asyncio.create_task(mgr.recover_aus_db())
        await asyncio.wait_for(client.open_angefragt.wait(), 5)
        route = asyncio.create_task(api_server.notfall_stopp())
        await asyncio.wait({route}, timeout=_GELEGENHEIT_S)
        client.cloud_antwort.set()
        antwort = await asyncio.wait_for(route, 5)
        await asyncio.wait_for(recover, 5)

        assert len(client.oeffnen_aufrufe) == 1  # Hauptdose kam an
        assert not sicherung.ist_aktiv(KANAL_BAMBUS), (
            "Hauptdose nach dem Notfall-Stopp offen geblieben"
        )
        assert await speicher.hole_pre_soak_states() == []
        assert antwort["ok"] is True
        assert antwort["pre_soak_abgebrochen"] == ["bambuswald"]
    finally:
        client.cloud_antwort.set()
        _raeume(sicherung)
