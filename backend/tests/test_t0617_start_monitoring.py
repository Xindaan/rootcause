"""Start trotz gesperrter Developer-API, nur gemockte Clouds + temporaere DB."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from bewaesserung import api_server, start_monitoring as modul
from bewaesserung.gardena_client import GardenaClient
from bewaesserung.modelle import GardenaKonfig, GesamtKonfig, SpeicherKonfig, WetterKonfig, ZonenKonfig
from bewaesserung.speicher import Speicher

LOC = "00000000-0000-0000-0000-000000000001"
SENSOR = "00000000-0000-0000-0000-000000000002"


@pytest.mark.parametrize("defekt", [None, "alt", "fremd", "kaputt", "uuid", "zukunft", "nan"])
def test_adressen_cache_ist_nur_fuer_bestaetigte_sensoren(tmp_path, defekt):
    pfad = tmp_path / "adressen.json"
    modul.speichere_sensor_adressen(pfad, "app", LOC, [SENSOR])
    daten = json.loads(pfad.read_text())
    assert set(daten) == {"identitaet", "stand", "location_id", "sensor_ids"}
    if defekt == "alt":
        daten["stand"] -= 8 * 86400
    elif defekt == "fremd":
        daten["identitaet"] = "andere-app"
    elif defekt == "uuid":
        daten["sensor_ids"] = ["ventil-ohne-valide-id"]
    elif defekt == "zukunft":
        daten["stand"] += 86400
    elif defekt == "nan":
        daten["stand"] = "nan"
    pfad.write_text("{" if defekt == "kaputt" else json.dumps(daten))
    assert modul.lade_sensor_adressen(pfad, "app") == (
        (LOC, {SENSOR}) if defekt is None else None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ende", ["erfolg", "cancel", "api_fehler", "discovery_fehler"])
async def test_leser_laufen_waehrend_discovery_und_werden_vor_uebergabe_gejoint(monkeypatch, ende):
    frei = asyncio.Event()
    gestartet = set()
    beendet = set()

    async def verbinden():
        await frei.wait()
        if ende == "discovery_fehler":
            raise ValueError("Test-Discovery")

    async def leser(name):
        gestartet.add(name)
        try:
            await asyncio.Event().wait()
        finally:
            beendet.add(name)

    monkeypatch.setattr(modul, "_dhs_lesen", lambda *a: leser("dhs"))
    monkeypatch.setattr(modul, "_fyta_lesen", lambda *a: leser("fyta"))
    api_task = asyncio.create_task(asyncio.Event().wait())
    task = asyncio.create_task(modul.warte_auf_gardena(
        SimpleNamespace(verbinden=verbinden), api_task,
        SimpleNamespace(fyta=SimpleNamespace(pflanzen=[1])), None, None,
    ))
    try:
        for _ in range(10):
            await asyncio.sleep(0)
            if len(gestartet) == 2:
                break
        assert gestartet == {"dhs", "fyta"}
        assert not task.done()
        if ende == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif ende == "api_fehler":
            api_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            frei.set()
            if ende == "discovery_fehler":
                with pytest.raises(ValueError, match="Test-Discovery"):
                    await task
            else:
                await task
        assert beendet == gestartet
    finally:
        task.cancel()
        api_task.cancel()
        await asyncio.gather(task, api_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_dhs_nutzt_cache_adressen_aber_aktuelle_db_zuordnung(tmp_path, monkeypatch):
    modul.speichere_sensor_adressen(tmp_path / "gardena_sensor_adressen.json", "app", LOC, [SENSOR])
    speicher = SimpleNamespace(hole_zuordnungen=AsyncMock(return_value={
        SENSOR: "neue_zone", "unbekannter_sensor": "andere_zone",
    }))
    gesehen = {}
    auth = object()
    pfade = []
    monkeypatch.setattr(modul, "baue_aus_env", lambda *p: pfade.append(p) or auth)

    class Job:
        def __init__(self, a, s, loc, zuordnung):
            gesehen.update(auth=a, speicher=s, location=loc, zuordnung=zuordnung)

        async def aktualisiere_wenn_faellig(self):
            raise asyncio.CancelledError

    monkeypatch.setattr(modul, "SensorDhsBackfillJob", Job)
    konfig = SimpleNamespace(speicher=SimpleNamespace(db_pfad=str(tmp_path / "test.db")),
                             gardena=SimpleNamespace(client_id="app"))
    with pytest.raises(asyncio.CancelledError):
        await modul._dhs_lesen(konfig, speicher)
    assert gesehen == dict(auth=auth, speicher=speicher, location=LOC, zuordnung={SENSOR: "neue_zone"})
    # T-0621: Login-Sperre liegt neben der DB wie im Hauptdienst.
    assert pfade == [(tmp_path / "gardena_customer_token.json",
                      tmp_path / "gardena_customer_login_sperre.json")]


@pytest.mark.asyncio
async def test_echte_sperre_blockiert_nur_discovery_nicht_dashboard(tmp_path, monkeypatch):
    speicher = Speicher(str(tmp_path / "test.db"))
    konfig = GesamtKonfig(gardena=GardenaKonfig(client_id="test", client_secret="test"),
                         zonen=[ZonenKonfig(zone_id="bambus", name="Bambus")],
                         wetter=WetterKonfig(),
                         speicher=SpeicherKonfig(db_pfad=str(tmp_path / "test.db")))
    await speicher.verbinden()
    api_server.konfiguriere_api(speicher, konfig, MagicMock(), MagicMock())
    monkeypatch.setattr(api_server.app.state, "gardena_startphase", True, raising=False)
    client = GardenaClient("test", "test")
    client._api_sperre.sperre()
    authentifiziere = AsyncMock(side_effect=AssertionError("Keine Cloud waehrend Pause"))
    monkeypatch.setattr(client, "_verbinden_einmal", authentifiziere)
    monkeypatch.setattr(modul, "_dhs_lesen", AsyncMock())
    api_task = asyncio.create_task(asyncio.Event().wait())
    wartend = asyncio.create_task(modul.warte_auf_gardena(client, api_task, konfig, speicher, None))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_server.app), base_url="http://localhost") as api:
            assert (await api.get("/api/health")).json()["ok"] is True
            assert (await api.get("/api/health/detail")).json()["gardena_startphase"] is True
            r = await api.get("/api/zonen")
            assert r.status_code == 200
            assert "bambus" in r.text
            assert (await api.post("/api/ventil/manuell-start", json={})).status_code == 503
            assert not wartend.done()
            authentifiziere.assert_not_awaited()
            # Nach Uebergabe greift wieder der normale Endpoint (Payload-Validierung).
            api_server.app.state.gardena_startphase = False
            assert (await api.post("/api/ventil/manuell-start", json={})).status_code == 422
    finally:
        wartend.cancel()
        api_task.cancel()
        await asyncio.gather(wartend, api_task, return_exceptions=True)
        await speicher.schliessen()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["sperre", "dns"])
@pytest.mark.parametrize("ml_geladen", [True, False])
async def test_main_startet_api_vor_wartender_discovery_und_schliesst_db(tmp_path, monkeypatch, blocker, ml_geladen):
    from bewaesserung import main
    from bewaesserung.ml import vorhersage
    from bewaesserung.ml.modelle_ml import MLVorhersage, ModellStatus
    from bewaesserung.modelle import SensorMessung

    ml = MagicMock()
    ml.lade_modelle.return_value = ml_geladen
    ml.ist_verfuegbar = ml_geladen
    ml.status.return_value = ModellStatus(ist_geladen=ml_geladen, horizonte=[6])
    ml.live_vorhersage = AsyncMock(return_value={"6h": MLVorhersage(
        zone_id="bambus", zeitstempel=datetime.now(), horizont_stunden=6,
        feuchte_aktuell=50, feuchte_prognose=48,
    )})
    monkeypatch.setattr(vorhersage, "MLVorhersageService", lambda **kw: ml)
    konfig = GesamtKonfig(gardena=GardenaKonfig(client_id="test", client_secret="test"),
                         zonen=[ZonenKonfig(zone_id="bambus", name="Bambus")],
                         wetter=WetterKonfig(),
                         speicher=SpeicherKonfig(db_pfad=str(tmp_path / "main.db")))
    konfig.ml_retrain.cluster_strategie = "pro_zone"
    monkeypatch.setattr(main, "lade_konfig", lambda p: konfig)
    monkeypatch.setattr(main.dotenv, "load_dotenv", lambda *a: None)
    monkeypatch.setattr(main, "_konfiguriere_logging", lambda *a: None)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *a: None)
    monkeypatch.setattr(modul, "_dhs_lesen", AsyncMock())
    monkeypatch.setattr(api_server.app.state, "gardena_startphase", False, raising=False)
    client = GardenaClient("test", "test")
    auth_versucht = asyncio.Event()
    if blocker == "sperre":
        client._api_sperre.sperre()
    else:
        from gardena.smart_system import SmartSystem

        async def auth(self):
            self.client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200),
            ))
            auth_versucht.set()
            raise httpx.ConnectError("DNS offline")

        monkeypatch.setattr(SmartSystem, "authenticate", auth)
    monkeypatch.setattr(main, "GardenaClient", lambda **kw: client)
    server_gestartet = asyncio.Event()
    server_beendet = asyncio.Event()

    class Server:
        should_exit = False

        def __init__(self, config):
            pass

        async def serve(self):
            server_gestartet.set()
            try:
                while not self.should_exit:
                    await asyncio.sleep(0.01)
            finally:
                server_beendet.set()

    monkeypatch.setattr(main.uvicorn, "Server", Server)
    task = asyncio.create_task(main.ausfuehren())
    try:
        await asyncio.wait_for(server_gestartet.wait(), timeout=3)
        assert not task.done()
        if blocker == "sperre":
            assert client._smart_system is None  # keinerlei Auth/Discovery
        else:
            await asyncio.wait_for(auth_versucht.wait(), timeout=2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_server.app), base_url="http://localhost") as api:
            assert (await api.get("/api/zonen")).status_code == 200
            assert (await api.get("/api/health/detail")).json()["gardena_startphase"] is True
            assert (await api.post("/api/ventil/manuell-start", json={})).status_code == 503
            assert (await api.get("/api/ml/status")).json()["ist_geladen"] is ml_geladen
            ml.lade_modelle.assert_called_once_with()
            assert api_server._laufzeit.motor._ml_vorhersage_service is (ml if ml_geladen else None)
            await api_server._laufzeit.speicher.speichere_messung(SensorMessung(
                zeitstempel=datetime.now(), zone_id="bambus", boden_feuchte=50,
            ))
            prognose = (await api.get("/api/ml/vorhersage/bambus")).json()
            if ml_geladen:
                ml.setze_cluster_zuordnung.assert_called_once_with({"bambus": "bambus"})
                assert prognose["6h"]["feuchte_prognose"] == 48
                ml.live_vorhersage.assert_awaited_once_with(
                    "bambus", api_server._laufzeit.speicher, konfig, details=False,
                )
            else:
                assert prognose == {"fehler": "ML-Modell nicht verfuegbar"}
                ml.live_vorhersage.assert_not_awaited()
    finally:
        task.cancel()
        ergebnis = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(ergebnis[0], asyncio.CancelledError)
    assert server_beendet.is_set()
    assert api_server._laufzeit.speicher._geschlossen
