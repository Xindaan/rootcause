"""T-0154: Tests fuer den py-smart-gardena start_ws delay-Bug-Patch.

Library-Bug: im Erfolgs-Pfad (`__launch_websocket_loop` clean zurueckgekehrt)
ist die Variable `delay` nie gesetzt, wird aber im darauffolgenden
Sleep-Block gelesen -> UnboundLocalError. Fix: `delay = 10` als Default
am Schleifen-Start.
"""

from __future__ import annotations

import asyncio

import pytest

# Wichtig: Import laed den Patch automatisch.
import bewaesserung.gardena_client  # noqa: F401
from gardena.smart_system import SmartSystem


class _FakeSmartSystem:
    """Minimaler SmartSystem-Stub fuer den Patch-Test.

    Nimmt nicht via `__init__` der echten Klasse, weil der Constructor
    Authentifizierungs-Argumente verlangt — wir interessieren uns nur
    fuer das `start_ws`-Verhalten, das via Method-Bind erreichbar ist.
    """

    def __init__(self, erfolg_dann_stop: bool = True):
        self.should_stop = False
        self._sleeps: list[float] = []
        self._erfolg_dann_stop = erfolg_dann_stop
        self._url_aufrufe = 0
        self._loop_aufrufe = 0
        self._ws_status_setzungen: list[bool] = []
        # logger-Stub
        self.logger = _StubLogger()

    def set_ws_status(self, status: bool) -> None:
        self._ws_status_setzungen.append(status)

    async def _SmartSystem__get_ws_url(self, location):  # name-mangled name
        self._url_aufrufe += 1
        return "ws://test"

    async def _SmartSystem__launch_websocket_loop(self, ws_url):
        self._loop_aufrufe += 1
        # Simuliert: WebSocket-Loop laeuft erfolgreich durch und kehrt
        # zurueck (genau der Bug-Pfad).
        if self._erfolg_dann_stop:
            self.should_stop = True
        return None  # kein Websocket-Objekt -> finally-close-Branch


class _StubLogger:
    def debug(self, *a, **kw): pass
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def error(self, *a, **kw): pass


@pytest.mark.asyncio
async def test_start_ws_erfolgs_pfad_kein_unboundlocalerror():
    """Erfolgs-Pfad (kein Exception): darf NICHT UnboundLocalError werfen.

    Vor T-0154 wuerde das hier mit `cannot access local variable 'delay'`
    crashen. Mit dem Patch laeuft es sauber durch (delay default 10).
    """
    fake = _FakeSmartSystem(erfolg_dann_stop=True)
    # bind die gepatchte Methode an die Fake-Instanz
    await SmartSystem.start_ws(fake, location=None)
    # Erfolgs-Pfad einmal durchgelaufen + sauber gestoppt.
    assert fake._loop_aufrufe == 1
    assert fake._url_aufrufe == 1


@pytest.mark.asyncio
async def test_start_ws_erfolgs_pfad_mit_kuenstlichem_reconnect(monkeypatch):
    """Variante: erster Loop laeuft erfolgreich durch, dann should_stop.

    Stellt sicher, dass nach erfolgreichem Loop der Sleep-Block
    `delay`-Default-Wert 10 nutzt und nicht crasht. Sleep monkey-gepatcht
    auf 0, damit der Test schnell laeuft.
    """
    fake = _FakeSmartSystem(erfolg_dann_stop=False)
    iter_zaehler = {"n": 0}

    async def kuenstlicher_loop(ws_url):
        iter_zaehler["n"] += 1
        if iter_zaehler["n"] >= 2:
            fake.should_stop = True
        return None

    fake._SmartSystem__launch_websocket_loop = kuenstlicher_loop
    # Sleep im Patch beschleunigen
    real_sleep = asyncio.sleep

    async def fast_sleep(_s):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    await SmartSystem.start_ws(fake, location=None)
    assert iter_zaehler["n"] == 2


def test_patch_quelle_enthaelt_default_delay():
    """Statisch verifizieren: gepatchte Quelle hat `delay = 10`-Default
    am Schleifen-Start (vor dem try-Block)."""
    import inspect
    quelle = inspect.getsource(SmartSystem.start_ws)
    assert "T-0154 FIX" in quelle
    # Default vor try
    while_block = quelle.split("while not self.should_stop:")[1]
    pre_try = while_block.split("try:")[0]
    assert "delay = 10" in pre_try, (
        "Default delay = 10 muss VOR dem try-Block stehen, sonst greift "
        "der Bugfix nicht."
    )
