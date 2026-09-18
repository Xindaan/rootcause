"""Gate W12 G12-2/G12-3: Kanten des geordneten Beendens in `main`.

G12-2: Seit Gate W11 Befund 4 bricht das Beenden den Sensor-DHS-Startup-
Backfill ab. Sein Done-Callback rief `t.result()` und fing nur `Exception`;
CancelledError entkam, asyncio loggte bei jedem Stop "Exception in callback"
auf ERROR.

G12-3: `_beende_laufende` wartete ohne Zeitlimit auf die abgebrochenen Tasks.
Ein Task, der CancelledError verschluckt, blockierte damit das Beenden und
das Schliessen des Speichers unbegrenzt.

Harmlos: nur asyncio-Tasks im Testprozess, kein Netz, keine DB, keine
Hardware. Der sture Task wird am Ende freigegeben, auch wenn der Schutz fehlt.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from structlog.testing import capture_logs

import bewaesserung
from bewaesserung import main

assert Path(bewaesserung.__file__).resolve().parents[2] == (
    Path(__file__).resolve().parents[1]
), f"falscher Checkout importiert: {bewaesserung.__file__}"


async def test_abgebrochener_dhs_startup_backfill_meldet_keinen_callback_fehler():
    loop = asyncio.get_running_loop()
    callback_fehler: list[dict] = []
    alter_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: callback_fehler.append(ctx))
    try:
        with capture_logs() as logs:
            task = asyncio.create_task(asyncio.Event().wait())
            task.add_done_callback(main._melde_dhs_startup_ende)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)  # Done-Callbacks laufen im naechsten Schritt
    finally:
        loop.set_exception_handler(alter_handler)

    assert callback_fehler == [], [c.get("message") for c in callback_fehler]
    ereignisse = [e["event"] for e in logs]
    assert "sensor_dhs_backfill.startup_abgebrochen" in ereignisse, ereignisse
    assert "sensor_dhs_backfill.startup_fehler" not in ereignisse


async def test_beenden_wartet_begrenzt_auf_einen_task_der_den_abbruch_verschluckt(
    monkeypatch,
):
    monkeypatch.setattr(main, "BEENDEN_TASKS_TIMEOUT_S", 0.2)
    frei = asyncio.Event()

    async def _stur() -> None:
        while not frei.is_set():
            try:
                await frei.wait()
            except asyncio.CancelledError:
                pass  # verschluckt den Abbruch

    stur = asyncio.create_task(_stur())
    await asyncio.sleep(0)
    try:
        with capture_logs() as logs:
            beenden = asyncio.create_task(
                main._beende_laufende(None, stur, None, None, None),
            )
            # asyncio.wait bricht nicht ab: haengt das Beenden, bleibt es
            # pending, und der Test wird rot statt selbst zu haengen.
            await asyncio.wait({beenden}, timeout=5)
            assert beenden.done(), "Beenden haengt am sturen Task"
            beenden.result()
        assert not stur.done()
        warnungen = [e for e in logs if e["event"] == "shutdown.tasks_haengen"]
        assert len(warnungen) == 1, [e["event"] for e in logs]
        assert warnungen[0]["log_level"] == "warning"
        assert any("_stur" in name for name in warnungen[0]["tasks"])
    finally:
        frei.set()
        await asyncio.wait_for(stur, timeout=5)
        if not beenden.done():
            await asyncio.wait_for(beenden, timeout=5)
