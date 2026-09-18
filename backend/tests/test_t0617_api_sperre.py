"""T-0617: 429, SDK-Retries, Neustart und Schliess-Safety ohne Netzwerk."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import bewaesserung.gardena_api_sperre as modul
from bewaesserung.gardena_api_sperre import GardenaApiGesperrt, GardenaApiSperre
from bewaesserung.gardena_client import GardenaClient
from gardena.smart_system import SmartSystem


@pytest.fixture
def uhr(monkeypatch):
    zeit = [1_800_000_000.0]
    monkeypatch.setattr(modul.time, "time", lambda: zeit[0])
    return zeit


def _daten(command="START_SECONDS_TO_OVERRIDE"):
    return {"type": "VALVE_CONTROL", "attributes": {"command": command, "seconds": 300}}


def _system(gate, transport):
    system = SmartSystem("test", "test")
    system.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    system._gardena_api_sperre = gate
    gate.installiere(system.client)
    return system


@pytest.mark.asyncio
@pytest.mark.parametrize("erster_pfad", ["kommando", "websocket", "discovery"])
async def test_ein_429_sperrt_alle_start_und_lesepfade(erster_pfad, uhr):
    aufrufe = []

    def transport(request):
        aufrufe.append(request.url.path)
        return httpx.Response(429, json={"message": "explicit deny"})

    system = _system(GardenaApiSperre("test"), transport)
    try:
        with pytest.raises(GardenaApiGesperrt):
            if erster_pfad == "kommando":
                await system.call_smart_system_service("g:2", _daten())
            elif erster_pfad == "websocket":
                await system._SmartSystem__get_ws_url(SimpleNamespace(id="ort"))
            else:
                await system.update_locations()
        for _ in range(5):
            for call in [
                lambda: system.call_smart_system_service("g:2", _daten()),
                lambda: system._SmartSystem__get_ws_url(SimpleNamespace(id="ort")),
                system.update_locations,
            ]:
                with pytest.raises(GardenaApiGesperrt):
                    await call()
        assert len(aufrufe) == 1  # auch kein versteckter SDK-Backoff-Retry
    finally:
        await system.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [202, 429, 503])
async def test_schliessen_bleibt_moeglich_und_fehler_bleibt_fehler(status, uhr):
    from bewaesserung.gardena_client import GardenaKommandoFehler
    gate = GardenaApiSperre("test")
    gate.sperre()
    bis = gate.bis
    aufrufe = []

    def transport(request):
        aufrufe.append(json.loads(request.content))
        return httpx.Response(status, json={"message": "test"})

    system = _system(gate, transport)
    try:
        if status == 202:
            await system.call_smart_system_service("g:2", _daten("STOP_UNTIL_NEXT_TASK"))
        else:
            with pytest.raises((GardenaApiGesperrt, GardenaKommandoFehler)):
                await system.call_smart_system_service("g:2", _daten("STOP_UNTIL_NEXT_TASK"))
        assert len(aufrufe) == 1
        assert gate.bis == bis  # Close hebt Pause nicht auf/eskaliert nicht
        with pytest.raises(GardenaApiGesperrt):
            await system.call_smart_system_service("g:2", _daten())
        assert len(aufrufe) == 1
    finally:
        await system.client.aclose()


def test_sperre_uebersteht_neustart_und_eskaliert_nur_nach_neuer_probe(tmp_path, uhr):
    pfad = tmp_path / "sperre.json"
    gate = GardenaApiSperre("test", pfad)
    gate.sperre()
    bis = gate.bis
    assert bis == uhr[0] + 4 * 3600
    gate.sperre()
    assert gate.bis == bis and gate.stufe == 1
    neu = GardenaApiSperre("test", pfad)
    with pytest.raises(GardenaApiGesperrt):
        neu.pruefe()
    uhr[0] = bis
    neu.pruefe()
    neu.sperre()
    assert neu.bis == bis + 8 * 3600
    assert "test" not in pfad.read_text()


@pytest.mark.parametrize("header", ["20000", "Sat, 16 Jan 2027 08:00:00 GMT"])
def test_retry_after_wird_nicht_unterschritten(header, uhr):
    gate = GardenaApiSperre("test")
    gate.sperre(header)
    if header.isdigit():
        assert gate.bis == uhr[0] + int(header)
    else:
        from email.utils import parsedate_to_datetime
        assert gate.bis == parsedate_to_datetime(header).timestamp()


@pytest.mark.parametrize("header", ["unbekannt", "nan", "inf", "-1"])
def test_ungueltiges_retry_after_verkuerzt_pause_nicht(header, uhr):
    gate = GardenaApiSperre("test")
    gate.sperre(header)
    assert gate.bis == uhr[0] + 4 * 3600


def test_kaputter_persistenter_zustand_fail_closed(tmp_path):
    pfad = tmp_path / "sperre.json"
    pfad.write_text("kaputt")
    with pytest.raises(ValueError):
        GardenaApiSperre("test", pfad)


@pytest.mark.asyncio
async def test_erfolg_nach_pause_setzt_eskalation_zurueck(tmp_path, uhr):
    gate = GardenaApiSperre("test", tmp_path / "sperre.json")
    gate.sperre()
    uhr[0] = gate.bis
    system = _system(gate, lambda r: httpx.Response(202))
    try:
        await system.call_smart_system_service("g:2", _daten())
        assert gate.stufe == 0 and gate.bis == 0
        assert GardenaApiSperre("test", gate.pfad).bis == 0
    finally:
        await system.client.aclose()


@pytest.mark.asyncio
async def test_customer_api_bleibt_unberuehrt(uhr):
    gate = GardenaApiSperre("test")
    gate.sperre()
    system = _system(gate, lambda r: httpx.Response(200))
    try:
        r = await system.client.get("https://smart.gardena.com/v1/dhs/test")
        assert r.status_code == 200
        assert gate.stufe == 1
    finally:
        await system.client.aclose()


@pytest.mark.asyncio
async def test_startup_wartet_vor_auth_und_ist_cancelbar(tmp_path, uhr, monkeypatch):
    client = GardenaClient("test", api_sperre_pfad=tmp_path / "sperre.json")
    client._api_sperre.sperre()
    einmal = AsyncMock()
    monkeypatch.setattr(client, "_verbinden_einmal", einmal)
    task = asyncio.create_task(client.verbinden())
    await asyncio.sleep(0)
    assert not task.done()
    einmal.assert_not_awaited()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_startup_429_wartet_statt_crash_loop(uhr, monkeypatch):
    client = GardenaClient("test")
    client._smart_system = SimpleNamespace(client=SimpleNamespace(aclose=AsyncMock()))
    calls = []

    async def einmal():
        calls.append(uhr[0])
        if len(calls) == 1:
            client._api_sperre.sperre()
            raise GardenaApiGesperrt(client._api_sperre.bis)

    async def warte():
        uhr[0] = max(uhr[0], client._api_sperre.bis)

    monkeypatch.setattr(client, "_verbinden_einmal", einmal)
    monkeypatch.setattr(client._api_sperre, "warte", warte)
    await client.verbinden()
    assert len(calls) == 2 and calls[1] - calls[0] == 4 * 3600
    client._smart_system.client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_echte_verbinden_verdrahtung_installiert_sperre_vor_discovery(monkeypatch, uhr):
    aufrufe = []

    def transport(request):
        aufrufe.append(request.url.path)
        return httpx.Response(429, json={"message": "explicit deny"})

    async def auth(self):
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))

    monkeypatch.setattr(SmartSystem, "authenticate", auth)
    client = GardenaClient("test")
    try:
        with pytest.raises(GardenaApiGesperrt, match="explicit deny"):
            await client._verbinden_einmal()
        assert aufrufe == ["/v2/locations"]
        assert client._api_sperre.bis == uhr[0] + 4 * 3600
        assert client._smart_system._gardena_api_sperre is client._api_sperre
    finally:
        await client._smart_system.client.aclose()


@pytest.mark.asyncio
async def test_429_mit_defektem_antwortstrom_sperrt_trotzdem(uhr):
    gate = GardenaApiSperre("test")
    response = httpx.Response(429, request=httpx.Request("GET", "https://api.smart.gardena.dev/v2/locations"))
    response.aread = AsyncMock(side_effect=httpx.ReadError("kaputt"))
    with pytest.raises(GardenaApiGesperrt, match="Antworttext nicht lesbar"):
        await gate.nach_antwort(response)
    with pytest.raises(GardenaApiGesperrt):
        gate.pruefe()
