"""T-0632: Start darf nicht ewig auf einen Leser warten, der den Abbruch verschluckt.

Realfall 22.10.2026, 16:03: Nach `gardena.verbunden` brach `warte_auf_gardena`
DHS- und FYTA-Leser ab und wartete per `gather` auf beide. Der DHS-Leser
(`while True` + `sleep(300)`) lief trotz Abbruch weiter; der Start hing 16 min,
ohne Entscheidungsschleife. Ein zweiter Abbruch (Neustart 16:19) wirkte sofort.
Die Testleser bilden genau das nach: Abbruch abfangen und weiterlaufen.
"""

import asyncio
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from bewaesserung import start_monitoring as modul


@pytest.mark.asyncio
@pytest.mark.parametrize("schluckt", ["einmal", "immer"])
async def test_start_haengt_nicht_an_leser_der_abbruch_verschluckt(monkeypatch, schluckt):
    monkeypatch.setattr(modul, "LESER_JOIN_TIMEOUT_S", 0.05, raising=False)
    beendet = set()
    leser_tasks = []
    aufraeumen = False

    async def zaeher_leser(name):
        leser_tasks.append(asyncio.current_task())
        geschluckt = 0
        try:
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    if aufraeumen or (schluckt == "einmal" and geschluckt):
                        raise
                    geschluckt += 1
        finally:
            beendet.add(name)

    async def braver_leser(name):
        try:
            await asyncio.Event().wait()
        finally:
            beendet.add(name)

    async def verbinden():
        await asyncio.sleep(0)

    monkeypatch.setattr(modul, "_dhs_lesen", lambda *a: zaeher_leser("dhs"))
    monkeypatch.setattr(modul, "_fyta_lesen", lambda *a: braver_leser("fyta"))
    api_task = asyncio.create_task(asyncio.Event().wait())
    with capture_logs() as logs:
        task = asyncio.create_task(modul.warte_auf_gardena(
            SimpleNamespace(verbinden=verbinden), api_task,
            SimpleNamespace(fyta=SimpleNamespace(pflanzen=[1])), None, None,
        ))
        try:
            # Bewusst asyncio.wait statt wait_for: ein Abbruch des haengenden
            # Starts wuerde selbst wieder verschluckt und den Test aufhaengen.
            await asyncio.wait({task}, timeout=2)
            assert task.done(), "Start haengt am Leser (T-0632)"
            await task
            haenger = [e for e in logs if e["event"] == "start_monitoring.leser_haengt"]
            assert haenger, "Haenger muss geloggt werden"
            assert "zaeher_leser" in haenger[0]["stack"]
            if schluckt == "einmal":
                # Der zweite Abbruch muss den Leser wirklich beenden, sonst
                # liefe er neben den Lesern des Normalbetriebs weiter.
                assert beendet == {"dhs", "fyta"}
                assert not any(
                    e["event"] == "start_monitoring.leser_weiter_haengend" for e in logs
                )
            else:
                assert beendet == {"fyta"}
                assert any(
                    e["event"] == "start_monitoring.leser_weiter_haengend" for e in logs
                )
        finally:
            aufraeumen = True
            for t in [task, api_task, *leser_tasks]:
                t.cancel()
            await asyncio.gather(task, api_task, *leser_tasks, return_exceptions=True)
