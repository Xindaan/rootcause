"""Offline-Start ohne launchd-Schleife, ausschliesslich MockTransport."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from authlib.integrations.base_client.errors import OAuthError
from gardena.smart_system import SmartSystem

from bewaesserung.gardena_client import GardenaClient


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["auth", "locations", "devices"])
@pytest.mark.parametrize("fehler", ["dns", "timeout", "protokoll", "server"])
async def test_offline_start_erholt_sich_mit_begrenztem_backoff(monkeypatch, phase, fehler):
    clients, pausen, anfragen = [], [], []
    zeit = [1_800_000_000.0]
    monkeypatch.setattr("bewaesserung.gardena_api_kontingent.time.time", lambda: zeit[0])

    def transport(request):
        anfragen.append((len(clients), request.url.path))
        if request.url.path == "/" + phase and len(clients) <= 6:
            if fehler == "server":
                return httpx.Response(503)
            typ = {"dns": httpx.ConnectError, "timeout": httpx.ReadTimeout,
                   "protokoll": httpx.RemoteProtocolError}[fehler]
            raise typ("Test offline", request=request)
        return httpx.Response(200)

    async def auth(self):
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        clients.append(self.client)
        (await self.client.get("https://api.authentication.husqvarnagroup.dev/auth")).raise_for_status()

    async def locations(self):
        (await self.client.get("https://api.smart.gardena.dev/locations")).raise_for_status()
        self.locations = {"ort": SimpleNamespace(id="ort", name="Test", devices={})}

    async def devices(self, location):
        (await self.client.get("https://api.smart.gardena.dev/devices")).raise_for_status()

    async def sleep(delay):
        # Aufraeumen muss VOR dem Warten abgeschlossen sein.
        assert clients[-1].is_closed
        pausen.append(delay)
        zeit[0] += delay  # Rollende Request-Budgets sehen dieselbe virtuelle Uhr.

    monkeypatch.setattr(SmartSystem, "authenticate", auth)
    monkeypatch.setattr(SmartSystem, "update_locations", locations)
    monkeypatch.setattr(SmartSystem, "update_devices", devices)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    client = GardenaClient("test", "test")
    try:
        await client.verbinden()
        assert pausen == [30, 60, 120, 240, 300, 300]
        assert len(clients) == 7
        assert all(c.is_closed for c in clients[:-1])
        assert not clients[-1].is_closed
        assert client._location_id == "ort"
        assert client._api_sperre.stufe == 0  # Netzfehler sind keine 429-Sperre.
        assert [p for n, p in anfragen if n == 7] == ["/auth", "/locations", "/devices"]
    finally:
        for c in clients:
            await c.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fehler", [401, 403, "oauth", "daten"])
async def test_permanente_fehler_werden_nicht_als_offline_versteckt(monkeypatch, fehler):
    client = GardenaClient("test", "test")
    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    request = httpx.Request("GET", "https://example.invalid")
    if isinstance(fehler, int):
        exc = httpx.HTTPStatusError("Test", request=request, response=httpx.Response(fehler, request=request))
    else:
        exc = OAuthError("invalid_client") if fehler == "oauth" else ValueError("Test-Daten")

    async def auth(self):
        self.client = transport_client
        raise exc

    sleep = AsyncMock(side_effect=AssertionError("Kein Retry bei permanentem Fehler"))
    monkeypatch.setattr(SmartSystem, "authenticate", auth)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(type(exc)) as caught:
        await client.verbinden()
    assert caught.value is exc
    assert transport_client.is_closed
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["auth", "pause"])
async def test_cancel_schliesst_start_client_ohne_weiteren_versuch(monkeypatch, phase):
    client = GardenaClient("test", "test")
    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    wartet = asyncio.Event()
    aufrufe = []

    async def auth(self):
        aufrufe.append(1)
        self.client = transport_client
        if phase == "pause":
            raise httpx.ConnectError("DNS offline")
        wartet.set()
        await asyncio.Event().wait()

    async def sleep(delay):
        assert transport_client.is_closed
        wartet.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(SmartSystem, "authenticate", auth)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    task = asyncio.create_task(client.verbinden())
    await asyncio.wait_for(wartet.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport_client.is_closed
    assert aufrufe == [1]
