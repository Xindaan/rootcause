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
async def test_start_ws_erfolgs_pfad_mit_kuenstlichem_reconnect(monkeypatch):
    """Erster Loop laeuft erfolgreich durch, dann Reconnect, dann should_stop.

    Genau dieser Pfad erreicht den Sleep-Block nach einem Erfolg -- vor
    T-0154 warf er dort `UnboundLocalError`. Die Zahl der Sleeps haelt den
    Default `delay = 10` fest. Sleep monkey-gepatcht auf 0, damit der Test
    schnell laeuft.

    Bis 25.09.2026 standen daneben ein Test mit `should_stop` schon im ersten
    Loop (erreichte den Sleep-Block nie, blieb mit dem Bug gruen) und ein
    Quelltext-Test auf `delay = 10` vor `try:`.
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

    async def fast_sleep(s):
        fake._sleeps.append(s)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    await SmartSystem.start_ws(fake, location=None)
    assert iter_zaehler["n"] == 2
    # Ein Reconnect nach Erfolg: 10 x 1 s (Default-delay), nicht mehr.
    assert fake._sleeps == [1] * 10


@pytest.mark.asyncio
async def test_t0617_kurze_saubere_verbindungen_bremsen_eskalierend(monkeypatch):
    """Reproduziert 60-s-Close ohne Exception: bisher immer nur 10 s Pause."""
    import bewaesserung.gardena_client as modul
    fake = _FakeSmartSystem(erfolg_dann_stop=False)
    zeit = [0.0]
    pausen = []
    anfaenge = []

    async def loop(url):
        anfaenge.append(zeit[0])
        zeit[0] += 62
        if len(anfaenge) == 12:
            fake.should_stop = True

    async def sleep(s):
        pausen.append(s)
        zeit[0] += s

    monkeypatch.setattr(modul.time, "monotonic", lambda: zeit[0])
    monkeypatch.setattr(asyncio, "sleep", sleep)
    fake._SmartSystem__launch_websocket_loop = loop
    await SmartSystem.start_ws(fake, None)
    delays = [b - a - 62 for a, b in zip(anfaenge, anfaenge[1:])]
    assert delays == [10, 20, 40, 80, 160, 320, 640, 1280, 2560, 3600, 3600]


@pytest.mark.asyncio
async def test_t0617_stabile_verbindung_setzt_reconnect_backoff_zurueck(monkeypatch):
    import bewaesserung.gardena_client as modul
    fake = _FakeSmartSystem(erfolg_dann_stop=False)
    zeit = [0.0]
    anfaenge = []

    async def loop(url):
        anfaenge.append(zeit[0])
        zeit[0] += 600 if len(anfaenge) == 3 else 62
        if len(anfaenge) == 5:
            fake.should_stop = True

    async def sleep(s):
        zeit[0] += s

    monkeypatch.setattr(modul.time, "monotonic", lambda: zeit[0])
    monkeypatch.setattr(asyncio, "sleep", sleep)
    fake._SmartSystem__launch_websocket_loop = loop
    await SmartSystem.start_ws(fake, None)
    assert anfaenge == [0, 72, 154, 764, 836]


@pytest.mark.asyncio
async def test_t0617_ws_ping_und_cancel_cleanup(monkeypatch):
    """Parameter UND Lebenszyklus pruefen, inkl. Callback-Durchleitung."""
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    import gardena.smart_system as sdk
    fake = _FakeSmartSystem()
    fake._ssl_context = object()
    messages = []
    fake.on_message = messages.append
    wartet = asyncio.Event()

    async def recv():
        if not messages:
            return '{"test": true}'
        wartet.set()
        await asyncio.Future()

    ws = SimpleNamespace(recv=recv, close=AsyncMock())
    connect = AsyncMock(return_value=ws)
    monkeypatch.setattr(sdk, "connect", connect)
    task = asyncio.create_task(SmartSystem._SmartSystem__launch_websocket_loop(fake, "wss://test"))
    await wartet.wait()
    assert messages == ['{"test": true}']
    assert connect.call_args.kwargs["ping_interval"] == 30
    assert connect.call_args.kwargs["ping_timeout"] == 30
    assert fake._ws_status_setzungen == [True]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ws.close.assert_awaited_once()
    assert fake._ws_status_setzungen == [True, False]
