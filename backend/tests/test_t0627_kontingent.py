"""Vorbeugende Quoten mit echter HTTP-Hook-/SDK-Kette, ohne Cloud/Hardware."""

import asyncio
import fcntl
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from gardena.smart_system import SmartSystem

import bewaesserung.gardena_api_kontingent as modul
from bewaesserung.gardena_api_kontingent import GardenaApiKontingent, GardenaKontingentErschoepft
from bewaesserung.gardena_api_sperre import GardenaApiGesperrt, GardenaApiSperre
from bewaesserung.gardena_client import GardenaClient


@pytest.fixture
def uhr(monkeypatch):
    zeit = [1_800_000_000.0]
    monkeypatch.setattr(modul.time, "time", lambda: zeit[0])
    return zeit


@pytest.mark.parametrize("budget", modul.BUDGETS)
def test_jedes_rollende_fenster_inkl_exakter_grenze(budget, monkeypatch, uhr):
    monkeypatch.setattr(modul, "BUDGETS", (budget,))
    name, fenster, limit, ws = budget
    gate = GardenaApiKontingent("test")
    for _ in range(limit):
        gate.reserviere(websocket=ws)
    with pytest.raises(GardenaKontingentErschoepft) as err:
        gate.reserviere(websocket=ws)
    assert err.value.bis == uhr[0] + fenster
    assert err.value.grund == name
    uhr[0] += fenster - 0.01
    with pytest.raises(GardenaKontingentErschoepft):
        gate.reserviere(websocket=ws)
    uhr[0] += 0.01
    gate.reserviere(websocket=ws)


def test_neustart_mehrere_clients_und_uhr_zurueck(tmp_path, uhr):
    pfad = tmp_path / "quota.json"
    for _ in range(2):
        GardenaApiKontingent("test", pfad).reserviere(websocket=True)
    uhr[0] -= 3600
    with pytest.raises(GardenaKontingentErschoepft) as err:
        GardenaApiKontingent("test", pfad).reserviere(websocket=True)
    assert err.value.bis == uhr[0] + 7200
    # Der engere WS-Topf blockiert kein Ventil-Startbudget.
    GardenaApiKontingent("test", pfad).reserviere(websocket=False)


def test_konkurrierende_reservierungen_verlieren_keinen_zaehler(tmp_path, uhr):
    pfad = tmp_path / "quota.json"

    def versuch(_):
        try:
            GardenaApiKontingent("test", pfad).reserviere(websocket=False)
            return True
        except GardenaKontingentErschoepft:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        erlaubt = sum(pool.map(versuch, range(120)))
    daten = json.loads(pfad.read_text())
    assert 1 <= erlaubt <= 8
    assert len(daten["versuche"]) == erlaubt


@pytest.mark.asyncio
@pytest.mark.parametrize("defekt", ["voll", "json", "schema", "identitaet", "schreiben", "lock"])
async def test_close_umgeht_auch_defekte_persistenz(defekt, tmp_path, monkeypatch, uhr):
    gate = GardenaApiSperre("test", tmp_path / "sperre.json")
    quota = gate.kontingent
    if defekt == "voll":
        for _ in range(8):
            quota.reserviere(websocket=False)
    elif defekt == "json":
        quota.pfad.write_text("kaputt")
    elif defekt == "schema":
        quota.pfad.write_text(json.dumps({"version": 1, "identitaet": quota.identitaet, "versuche": [[float('nan'), True]]}))
    elif defekt == "identitaet":
        GardenaApiKontingent("andere", quota.pfad).reserviere(websocket=False)
    elif defekt == "schreiben":
        monkeypatch.setattr(modul.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("disk full")))
    lock = quota.pfad.with_suffix(".lock").open("a")
    if defekt == "lock":
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    aufrufe = []

    def transport(request):
        aufrufe.append(request)
        return httpx.Response(202)

    system = SmartSystem("test", "test")
    system._gardena_api_sperre = gate
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            system.client = client
            gate.installiere(client)
            with pytest.raises(GardenaKontingentErschoepft):
                await system.call_smart_system_service("g:2", {
                    "type": "VALVE_CONTROL", "attributes": {"command": "START_SECONDS_TO_OVERRIDE"},
                })
            assert not aufrufe
            await system.call_smart_system_service("g:2", {
                "type": "VALVE_CONTROL", "attributes": {"command": "STOP_UNTIL_NEXT_TASK"},
            })
            assert len(aufrufe) == 1
    finally:
        lock.close()


@pytest.mark.asyncio
async def test_echte_sdk_ws_route_und_geteiltes_http_budget(uhr):
    gate = GardenaApiSperre("test")
    aufrufe = []

    def transport(request):
        aufrufe.append(request.url.path)
        return httpx.Response(201, json={"data": {"attributes": {"url": "wss://example.invalid"}}})

    system = SmartSystem("test", "test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        system.client = client
        gate.installiere(client)
        for _ in range(2):
            await system._SmartSystem__get_ws_url(SimpleNamespace(id="ort"))
        with pytest.raises(GardenaKontingentErschoepft, match="ws_stunde"):
            await system._SmartSystem__get_ws_url(SimpleNamespace(id="ort"))
        for _ in range(6):
            await client.get("https://api.smart.gardena.dev/v1/locations")
        with pytest.raises(GardenaKontingentErschoepft, match="gesamt_10s"):
            await client.put("https://api.smart.gardena.dev/v2/command/g:2", json={"data": {}})
        assert len(aufrufe) == 8
        assert aufrufe[:2] == ["/v2/websocket"] * 2
        # Andere Dienste werden weder gezaehlt noch blockiert.
        await client.get("https://smart.gardena.com/test")
        assert len(aufrufe) == 9


@pytest.mark.asyncio
async def test_versuche_auch_bei_dns_fehler_reserviert(uhr):
    gate = GardenaApiSperre("test")
    aufrufe = []

    def transport(request):
        aufrufe.append(request)
        raise httpx.ConnectError("DNS", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        gate.installiere(client)
        for _ in range(2):
            with pytest.raises(httpx.ConnectError):
                await client.post("https://api.smart.gardena.dev/v2/websocket")
        with pytest.raises(GardenaKontingentErschoepft):
            await client.post("https://api.smart.gardena.dev/v2/websocket")
    assert len(aufrufe) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("zwischenzeitlich_429", [False, True])
async def test_langsames_dateisystem_blockiert_keinen_close(monkeypatch, uhr, zwischenzeitlich_429):
    gate = GardenaApiSperre("test")
    betreten, freigabe = threading.Event(), threading.Event()
    reserviere = gate.kontingent.reserviere
    aufrufe = []

    def langsam(**kwargs):
        betreten.set()
        assert freigabe.wait(timeout=5)
        reserviere(**kwargs)

    def transport(request):
        aufrufe.append(request.method)
        return httpx.Response(202)

    monkeypatch.setattr(gate.kontingent, "reserviere", langsam)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        gate.installiere(client)
        start = asyncio.create_task(client.get("https://api.smart.gardena.dev/v1/locations"))
        try:
            assert await asyncio.to_thread(betreten.wait, 2)
            await asyncio.wait_for(client.put("https://api.smart.gardena.dev/v2/command/g:2", json={
                "data": {"type": "VALVE_CONTROL", "attributes": {"command": "STOP_UNTIL_NEXT_TASK"}},
            }), timeout=1)
            assert aufrufe == ["PUT"]
            assert not start.done()
            if zwischenzeitlich_429:
                gate.sperre()
        finally:
            freigabe.set()
            if zwischenzeitlich_429:
                with pytest.raises(GardenaApiGesperrt):
                    await start
            else:
                await start
        assert aufrufe == (["PUT"] if zwischenzeitlich_429 else ["PUT", "GET"])


@pytest.mark.asyncio
async def test_start_wartet_schliesst_client_und_ist_abbrechbar(monkeypatch, uhr):
    client = GardenaClient("test")
    http = SimpleNamespace(aclose=AsyncMock())
    client._smart_system = SimpleNamespace(client=http)
    verbinden = AsyncMock(side_effect=GardenaKontingentErschoepft(uhr[0] + 3600, "ws_stunde"))
    monkeypatch.setattr(client, "_verbinden_einmal", verbinden)

    async def sleep(delay):
        assert delay == 3600
        http.aclose.assert_awaited_once()
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await client.verbinden()
    verbinden.assert_awaited_once()


@pytest.mark.asyncio
async def test_ws_stabile_sechs_minuten_umgehen_quota_nicht(monkeypatch, uhr):
    from test_gardena_start_ws_patch import _FakeSmartSystem
    fake = _FakeSmartSystem(erfolg_dann_stop=False)
    quota = GardenaApiKontingent("test")
    gestartet = []
    pausen = []

    async def url(location):
        quota.reserviere(websocket=True)
        gestartet.append(uhr[0])
        return "wss://example.invalid"

    async def loop(url):
        uhr[0] += 360  # Setzt bestehenden Backoff zurueck, nicht das Budget.
        if len(gestartet) == 3:
            fake.should_stop = True

    async def sleep(delay):
        pausen.append(delay)
        uhr[0] += delay

    fake._SmartSystem__get_ws_url = url
    fake._SmartSystem__launch_websocket_loop = loop
    monkeypatch.setattr(modul.time, "monotonic", lambda: uhr[0])
    monkeypatch.setattr(asyncio, "sleep", sleep)
    await SmartSystem.start_ws(fake, None)
    assert len(gestartet) == 3
    assert gestartet[2] - gestartet[0] >= 3600
    assert max(pausen) == 1  # should_stop wird weiterhin sekundenweise geprueft.


@pytest.mark.asyncio
async def test_safety_keine_start_queue_und_close_retry_trotz_vollem_budget(monkeypatch, uhr):
    from test_ventil_sicherung import erstelle_sicherung, KANAL, ZONEN
    from bewaesserung.modelle import Ausloser
    sicherung, client, speicher = erstelle_sicherung()
    gate = GardenaApiSperre("test")
    system = SmartSystem("test", "test")
    system._gardena_api_sperre = gate
    status = [202]
    aufrufe = []

    def transport(request):
        aufrufe.append(request)
        return httpx.Response(status[0], json={"message": "test"})

    async def kommando(command):
        await system.call_smart_system_service("g:1", {
            "type": "VALVE_CONTROL", "attributes": {"command": command},
        })

    async def oeffnen(*args, **kwargs):
        await kommando("START_SECONDS_TO_OVERRIDE")

    async def schliessen(*args, **kwargs):
        await kommando("STOP_UNTIL_NEXT_TASK")

    monkeypatch.setattr(client, "ventil_oeffnen", oeffnen)
    monkeypatch.setattr(client, "ventil_schliessen", schliessen)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        system.client = http
        gate.installiere(http)
        try:
            assert await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
            timer = sicherung._aktiv[KANAL].timer_handle
            for _ in range(7):
                gate.kontingent.reserviere(websocket=False)
            status[0] = 503
            speicher.ereignisse.clear()
            assert not await sicherung.stoppe(KANAL, Ausloser.WATCHDOG)
            assert sicherung.ist_aktiv(KANAL)
            assert sicherung._aktiv[KANAL].timer_handle is not timer
            assert not sicherung._aktiv[KANAL].timer_handle.cancelled()
            assert speicher.ereignisse == []
            status[0] = 202
            assert await sicherung.stoppe(KANAL, Ausloser.WATCHDOG)
            assert not sicherung.ist_aktiv(KANAL)
            speicher.ereignisse.clear()
            assert not await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
            assert not sicherung.ist_aktiv(KANAL)
            assert speicher.ereignisse == []
            assert len(aufrufe) == 3  # Ein Start, zwei echte Close-Versuche.
            uhr[0] += 10
            await asyncio.sleep(0)
            assert len(aufrufe) == 3  # Ablauf erzeugt keinen nachgeholten Start.
            assert not sicherung.ist_aktiv(KANAL)
        finally:
            for aktiv in sicherung._aktiv.values():
                aktiv.timer_handle.cancel()
