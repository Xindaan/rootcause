"""T-0527: Tests fuer die Geraete-Status-Zeitreihe der FYTA-Sensoren.

Der Mangel, den das behebt: die Messreihe fuehrt nur soil_moisture,
temperature, light und soil_fertility. Akkustand, Online-Flag und
Firmware haengen am Pflanzen-Objekt und sind dort eine Momentaufnahme --
bei einem toten Geraet eingefroren auf dem letzten Kontakt. Bei der
Faulbaum-Diagnose (08.08.2026) war deshalb nicht mehr feststellbar, wie
der Akku VOR dem Ausfall verlief.

Getestet wird:
- das Zusammenfuehren von Listen- und Detail-Endpoint im Client
  (`battery_level` steht NUR im Detail)
- dass ein fehlgeschlagener Detail-Call den Rest nicht wegwirft
- Faelligkeits- und Fehlerverhalten des Jobs
- die Speicher-Rundreise
- die Verdrahtung in den Entscheidungsloop
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest

from bewaesserung.fyta_status_job import FytaStatusJob
from bewaesserung.modelle import FytaGeraeteStatus
from bewaesserung.speicher import Speicher


@pytest.fixture
async def speicher(tmp_path):
    s = Speicher(str(tmp_path / "status.db"))
    await s.verbinden()
    try:
        yield s
    finally:
        await s.schliessen()


class _FakeClient:
    def __init__(self, saetze=None, fehler: Exception | None = None):
        self._saetze = saetze or []
        self._fehler = fehler
        self.aufrufe = 0

    async def hole_geraete_status(self):
        self.aufrufe += 1
        if self._fehler:
            raise self._fehler
        return self._saetze


def _status(geraet_id="fyta_100004", **kwargs) -> FytaGeraeteStatus:
    basis = dict(
        zeitstempel=datetime(2026, 8, 8, 12, 0),
        geraet_id=geraet_id,
        plant_id=int(geraet_id.split("_")[1]),
        sensor_id="mac-sensor-1",
        zone_id="hecke",
        battery_level=100.0,
        is_battery_low=False,
        sensor_status=2,
        wifi_status=2,
        hub_status=2,
        is_outdated=True,
        firmware="0.9.14",
        last_data_received_at=datetime(2026, 8, 4, 14, 20, 47),
    )
    basis.update(kwargs)
    return FytaGeraeteStatus(**basis)


# --- Speicher-Rundreise ---------------------------------------------------


@pytest.mark.asyncio
async def test_speicher_rundreise_haelt_alle_felder(speicher: Speicher):
    await speicher.speichere_fyta_geraete_status(_status())
    letzte = await speicher.letzter_fyta_geraete_status()

    s = letzte["fyta_100004"]
    assert s.battery_level == 100.0
    assert s.is_battery_low is False
    assert s.is_outdated is True
    assert s.sensor_status == 2
    assert s.firmware == "0.9.14"
    assert s.sensor_id == "mac-sensor-1"
    assert s.last_data_received_at == datetime(2026, 8, 4, 14, 20, 47)


@pytest.mark.asyncio
async def test_letzter_status_nimmt_den_juengsten_satz(speicher: Speicher):
    await speicher.speichere_fyta_geraete_status(
        _status(zeitstempel=datetime(2026, 8, 7, 12, 0), battery_level=100.0)
    )
    await speicher.speichere_fyta_geraete_status(
        _status(zeitstempel=datetime(2026, 8, 8, 12, 0), battery_level=85.0)
    )
    letzte = await speicher.letzter_fyta_geraete_status()
    assert letzte["fyta_100004"].battery_level == 85.0


@pytest.mark.asyncio
async def test_zeitreihe_bleibt_erhalten(speicher: Speicher):
    """Der Punkt der ganzen Tabelle: ein einzelner Wert beweist nichts,
    erst die Reihe zeigt, ob der Akku faellt, springt oder feststeckt."""
    for tag, wert in ((5, 100.0), (6, 95.0), (7, 90.0)):
        await speicher.speichere_fyta_geraete_status(
            _status(zeitstempel=datetime(2026, 8, tag, 12, 0), battery_level=wert)
        )
    assert speicher._db is not None
    async with speicher._db.execute(
        "SELECT battery_level FROM fyta_geraete_status "
        "WHERE geraet_id = ? ORDER BY zeitstempel", ("fyta_100004",),
    ) as cursor:
        werte = [z["battery_level"] async for z in cursor]
    assert werte == [100.0, 95.0, 90.0]


@pytest.mark.asyncio
async def test_zwei_pflanzen_an_einer_mac_bleiben_getrennt(speicher: Speicher):
    """Faulbaum (100004) und Stechpalme (133731) teilen sich die Hardware
    mac-sensor-1. Der Primaerschluessel liegt deshalb auf geraet_id,
    nicht auf sensor_id -- sonst wuerde die eine die andere ueberschreiben.
    """
    await speicher.speichere_fyta_geraete_status(_status("fyta_100004"))
    await speicher.speichere_fyta_geraete_status(_status("fyta_133731"))
    letzte = await speicher.letzter_fyta_geraete_status()
    assert set(letzte) == {"fyta_100004", "fyta_133731"}
    assert {s.sensor_id for s in letzte.values()} == {"mac-sensor-1"}


# --- Job-Verhalten --------------------------------------------------------


@pytest.mark.asyncio
async def test_job_schreibt_und_respektiert_das_intervall(speicher: Speicher):
    client = _FakeClient([_status()])
    job = FytaStatusJob(speicher, client, intervall_stunden=6)
    t0 = datetime(2026, 8, 8, 12, 0)

    assert await job.aktualisiere_wenn_faellig(t0) == 1
    # Noch nicht faellig
    assert await job.aktualisiere_wenn_faellig(t0 + timedelta(hours=1)) == 0
    assert client.aufrufe == 1
    # Wieder faellig
    assert await job.aktualisiere_wenn_faellig(t0 + timedelta(hours=7)) == 1
    assert client.aufrufe == 2


@pytest.mark.asyncio
async def test_job_wirft_nicht_bei_client_fehler(speicher: Speicher):
    job = FytaStatusJob(speicher, _FakeClient(fehler=RuntimeError("API weg")))
    assert await job.aktualisiere_wenn_faellig(datetime(2026, 8, 8, 12, 0)) == 0
    assert job.letzter_fehler == "API weg"


@pytest.mark.asyncio
async def test_fehler_schickt_den_job_nicht_in_die_dauerschleife(speicher: Speicher):
    """`fehlerpattern_teure_vorarbeit_vor_dem_gate`: die Faelligkeit wird
    VOR der Arbeit gestempelt, ein Fehler darf sie nicht zuruecksetzen."""
    client = _FakeClient(fehler=RuntimeError("boom"))
    job = FytaStatusJob(speicher, client, intervall_stunden=6)
    t0 = datetime(2026, 8, 8, 12, 0)

    await job.aktualisiere_wenn_faellig(t0)
    await job.aktualisiere_wenn_faellig(t0 + timedelta(minutes=5))
    assert client.aufrufe == 1


# --- Client: Listen- und Detail-Endpoint zusammenfuehren -------------------


@pytest.mark.asyncio
async def test_client_holt_battery_level_aus_dem_detail_endpoint():
    """`battery_level` steht NUR unter /user-plant/<id>. Die Liste kennt
    es nicht -- ein Client, der nur sie liest, liefert immer None."""
    from bewaesserung.fyta_client import FytaClient
    from bewaesserung.modelle import FytaKonfig, FytaPflanzenKonfig

    pflanzen = [FytaPflanzenKonfig(fyta_id=100004, zone_id="hecke", name="Faulbaum")]
    client = FytaClient(FytaKonfig(api_url="https://x/api", pflanzen=pflanzen))

    async def _fake_retry(ruf):
        class _A:
            def __init__(self, daten):
                self._daten = daten

            def raise_for_status(self):
                return None

            def json(self):
                return self._daten

        # Der erste Aufruf ist die Liste, danach das Detail.
        if not getattr(_fake_retry, "liste_geholt", False):
            _fake_retry.liste_geholt = True
            return _A({"plants": [{
                "id": 100004,
                "wifi_status": 2,
                "isOutdated": True,
                "hub": {"status": 2},
                "sensor": {
                    "id": "mac-sensor-1", "status": 2,
                    "version": "0.9.14", "is_battery_low": False,
                    "received_data_at": "2026-08-04 12:21:00",
                },
            }]})
        return _A({"plant": {"sensors": [{"battery_level": 100}]}})

    client._mit_auth_retry = _fake_retry
    saetze = await client.hole_geraete_status()

    assert len(saetze) == 1
    s = saetze[0]
    assert s.geraet_id == "fyta_100004"
    assert s.battery_level == 100.0
    assert s.is_outdated is True
    assert s.sensor_status == 2
    assert s.wifi_status == 2
    assert s.firmware == "0.9.14"
    assert s.zone_id == "hecke"
    # received_data_at ist UTC, die DB ist Beispielstadt-naiv -> +2 h im Sommer
    assert s.last_data_received_at == datetime(2026, 8, 4, 14, 21, 0)


@pytest.mark.asyncio
async def test_client_behaelt_den_status_wenn_der_detail_call_scheitert():
    """Ein fehlender Akkuwert ist kein Grund, Status und Zeitstempel
    wegzuwerfen -- gerade sie sind bei einem toten Geraet die Evidenz."""
    from bewaesserung.fyta_client import FytaClient
    from bewaesserung.modelle import FytaKonfig, FytaPflanzenKonfig

    pflanzen = [FytaPflanzenKonfig(fyta_id=100004, zone_id="hecke", name="Faulbaum")]
    client = FytaClient(FytaKonfig(api_url="https://x/api", pflanzen=pflanzen))

    async def _fake_retry(ruf):
        class _A:
            def raise_for_status(self):
                return None

            def json(self):
                return {"plants": [{
                    "id": 100004, "wifi_status": 2,
                    "sensor": {"id": "MAC", "status": 2, "version": "0.9.14"},
                }]}

        if not getattr(_fake_retry, "liste_geholt", False):
            _fake_retry.liste_geholt = True
            return _A()
        raise RuntimeError("Detail-Endpoint down")

    client._mit_auth_retry = _fake_retry
    saetze = await client.hole_geraete_status()

    assert len(saetze) == 1
    assert saetze[0].battery_level is None
    assert saetze[0].sensor_status == 2
    assert saetze[0].firmware == "0.9.14"


@pytest.mark.asyncio
async def test_client_ignoriert_nicht_konfigurierte_pflanzen():
    """Der Account fuehrt 15 Pflanzen, die Konfig kennt nur einen Teil --
    unkonfigurierte gehoeren nicht in unsere Zeitreihe (Klasse
    `fehlerpattern_fallback_an_messwert_statt_konfig`: welche Geraete
    zaehlen, beantwortet die Konfig)."""
    from bewaesserung.fyta_client import FytaClient
    from bewaesserung.modelle import FytaKonfig, FytaPflanzenKonfig

    pflanzen = [FytaPflanzenKonfig(fyta_id=100004, zone_id="hecke", name="Faulbaum")]
    client = FytaClient(FytaKonfig(api_url="https://x/api", pflanzen=pflanzen))

    async def _fake_retry(ruf):
        class _A:
            def raise_for_status(self):
                return None

            def json(self):
                if not getattr(_fake_retry, "liste_geholt", False):
                    _fake_retry.liste_geholt = True
                    return {"plants": [
                        {"id": 100004, "sensor": {"id": "A"}},
                        {"id": 133731, "sensor": {"id": "A"}},  # Stechpalme
                        {"id": 100005, "sensor": {"id": "B"}},  # Zitrus
                    ]}
                return {"plant": {"sensors": [{"battery_level": 90}]}}

        return _A()

    client._mit_auth_retry = _fake_retry
    saetze = await client.hole_geraete_status()

    assert [s.geraet_id for s in saetze] == ["fyta_100004"]


# --- Verdrahtung ----------------------------------------------------------


def test_job_ist_im_entscheidungsloop_verdrahtet():
    """Regression gegen `fehlerpattern_job_nicht_in_loop_verdrahtet`."""
    from bewaesserung import main as main_modul

    quelle = inspect.getsource(main_modul._entscheidungsloop)
    assert "fyta_status_job" in inspect.signature(
        main_modul._entscheidungsloop
    ).parameters
    assert "await fyta_status_job.aktualisiere_wenn_faellig()" in quelle

    main_quelle = inspect.getsource(main_modul)
    assert "fyta_status_job = None" in main_quelle
    assert "fyta_status_job=fyta_status_job" in main_quelle
