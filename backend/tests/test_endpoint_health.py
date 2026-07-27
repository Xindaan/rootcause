"""Tests fuer T-0132 (H-8) EndpointHealthJob.

Deckt ab:
- DHS-Schema-OK (humidity + temperature) -> status 'ok'
- DHS-Schema-Drift (property-name umbenannt) -> status 'schema_fehler'
- DHS-HTTP-Fehler -> connect_fehler
- DHS-Auth-Fehler -> auth_fehler
- FYTA Token vorhanden -> ok
- FYTA Login crasht -> auth_fehler
- Speicher-Persistenz (UPSERT, letzter_erfolg-Verhalten)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest

from bewaesserung.endpoint_health import (
    ENDPOINT_DHS,
    ENDPOINT_FYTA,
    EndpointHealthJob,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "health.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


# --- Stubs ------------------------------------------------------------------

class _AuthStub:
    def __init__(self, token: str = "fake-bearer", crash: bool = False):
        self._token = token
        self._crash = crash

    async def hole_gueltigen_token(self) -> str:
        if self._crash:
            raise RuntimeError("Auth-Server unerreichbar")
        return self._token


class _FytaStub:
    """Spiegelt das echte FytaClient-Interface: _stelle_token_sicher returnt
    bool, der Token-String liegt im Attribut `_token`.

    T-0233: optionaler `pflanzen` + `api_url` fuer den Schema-Probe-Pfad.
    Ohne `pflanzen` ueberspringt der Job die Schema-Probe (Token-only-OK)."""

    def __init__(
        self,
        token: str = "fyta-jwt",
        crash: bool = False,
        login_erfolg: bool = True,
        pflanzen: dict | None = None,
        api_url: str = "https://web.fyta.de/api",
    ):
        self._token = token
        self._crash = crash
        self._login_erfolg = login_erfolg
        self._pflanzen_map = pflanzen or {}
        # `_konfig` ist ein Dummy mit `api_url`-Attribut -- analog zu
        # FytaKonfig im Produktivcode.
        class _Konfig:
            pass
        self._konfig = _Konfig()
        self._konfig.api_url = api_url

    async def _stelle_token_sicher(self) -> bool:
        if self._crash:
            raise RuntimeError("FYTA-Login fehlgeschlagen")
        return self._login_erfolg


# --- DHS-Tests --------------------------------------------------------------

def _httpx_handler(antwort_factory):
    """Baut httpx.MockTransport mit der Antwort-Funktion."""
    def handler(request: httpx.Request) -> httpx.Response:
        return antwort_factory(request)
    return httpx.MockTransport(handler)


def test_dhs_schema_ok_speichert_status_ok(speicher, monkeypatch):
    """DHS liefert humidity+temperature-Serie -> status='ok' + letzter_erfolg gesetzt."""
    job = EndpointHealthJob(
        speicher=speicher,
        gardena_auth=_AuthStub(),
        gardena_location_id="loc1",
        gardena_probe_sensor_uuid="sensor-1",
    )

    def antwort(req):
        return httpx.Response(200, json={
            "data": [
                {"type": "dh-point-serie",
                 "attributes": {"property-name": "humidity"}},
                {"type": "dh-point-serie",
                 "attributes": {"property-name": "temperature"}},
            ],
        })

    transport = _httpx_handler(antwort)

    # Monkey-patch httpx.AsyncClient damit unser MockTransport genutzt wird
    import bewaesserung.endpoint_health as eh_mod
    original_client = eh_mod.httpx.AsyncClient

    class _Client(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw.pop("timeout", None)
            super().__init__(transport=transport, **kw)

    monkeypatch.setattr(eh_mod.httpx, "AsyncClient", _Client)

    jetzt = datetime(2026, 5, 4, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_DHS] == "ok"

    eintraege = _run(speicher.hole_endpoint_health(ENDPOINT_DHS))
    assert len(eintraege) == 1
    assert eintraege[0]["status"] == "ok"
    assert eintraege[0]["letzter_erfolg"] is not None


def test_dhs_schema_drift_setzt_status_schema_fehler(speicher, monkeypatch):
    """Wenn property-name zu propertyName umbenannt wird (oder leer ist),
    erkennt der Job das als schema_fehler."""
    job = EndpointHealthJob(
        speicher=speicher,
        gardena_auth=_AuthStub(),
        gardena_location_id="loc1",
        gardena_probe_sensor_uuid="sensor-1",
    )

    def antwort(req):
        return httpx.Response(200, json={
            "data": [
                {"type": "dh-point-serie",
                 "attributes": {"propertyName": "humidity"}},  # falsche Schreibweise
            ],
        })

    import bewaesserung.endpoint_health as eh_mod
    transport = _httpx_handler(antwort)

    class _Client(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw.pop("timeout", None)
            super().__init__(transport=transport, **kw)

    monkeypatch.setattr(eh_mod.httpx, "AsyncClient", _Client)

    jetzt = datetime(2026, 5, 4, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_DHS] == "schema_fehler"

    eintraege = _run(speicher.hole_endpoint_health(ENDPOINT_DHS))
    assert eintraege[0]["status"] == "schema_fehler"
    # letzter_erfolg bleibt None, weil dieser Probe nie 'ok' war
    assert eintraege[0]["letzter_erfolg"] is None


def test_dhs_auth_fehler_setzt_auth_fehler_status(speicher):
    """Auth-Stub crasht -> kein API-Call, status='auth_fehler'."""
    job = EndpointHealthJob(
        speicher=speicher,
        gardena_auth=_AuthStub(crash=True),
        gardena_location_id="loc1",
        gardena_probe_sensor_uuid="sensor-1",
    )
    jetzt = datetime(2026, 5, 4, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_DHS] == "auth_fehler"


def test_dhs_http_403_setzt_auth_fehler(speicher, monkeypatch):
    """403 wird als auth_fehler klassifiziert (Token abgelaufen / revoked)."""
    job = EndpointHealthJob(
        speicher=speicher,
        gardena_auth=_AuthStub(),
        gardena_location_id="loc1",
        gardena_probe_sensor_uuid="sensor-1",
    )

    def antwort(req):
        return httpx.Response(403, json={"error": "forbidden"})

    import bewaesserung.endpoint_health as eh_mod
    transport = _httpx_handler(antwort)

    class _Client(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw.pop("timeout", None)
            super().__init__(transport=transport, **kw)

    monkeypatch.setattr(eh_mod.httpx, "AsyncClient", _Client)

    jetzt = datetime(2026, 5, 4, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_DHS].startswith("http_") or stat[ENDPOINT_DHS] == "auth_fehler"
    eintraege = _run(speicher.hole_endpoint_health(ENDPOINT_DHS))
    assert eintraege[0]["status"] == "auth_fehler"


def test_ist_dns_fehler_erkennt_offline_varianten():
    """T-0290: DNS-/Namensaufloesungs-Fehler (macOS + Linux/Pi) werden als
    host-offline erkannt, echte Connect-Fehler nicht."""
    from bewaesserung.endpoint_health import _ist_dns_fehler
    assert _ist_dns_fehler(
        OSError("[Errno 8] nodename nor servname provided, or not known"))  # macOS
    assert _ist_dns_fehler(OSError("[Errno -2] Name or service not known"))  # Linux
    assert _ist_dns_fehler(OSError("Temporary failure in name resolution"))  # Linux
    assert not _ist_dns_fehler(OSError("[Errno 61] Connection refused"))
    assert not _ist_dns_fehler(TimeoutError("timed out"))


def test_dhs_dns_fehler_setzt_host_offline(speicher, monkeypatch):
    """T-0290: DNS-/Offline-Fehler beim Probe -> status 'host_offline'
    (kein Watchdog-Push), NICHT 'connect_fehler'."""
    job = EndpointHealthJob(
        speicher=speicher, gardena_auth=_AuthStub(),
        gardena_location_id="loc1", gardena_probe_sensor_uuid="sensor-1",
    )

    def antwort(req):
        raise httpx.ConnectError(
            "[Errno 8] nodename nor servname provided, or not known")

    import bewaesserung.endpoint_health as eh_mod
    transport = _httpx_handler(antwort)

    class _Client(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw.pop("timeout", None)
            super().__init__(transport=transport, **kw)

    monkeypatch.setattr(eh_mod.httpx, "AsyncClient", _Client)
    jetzt = datetime(2026, 5, 4, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_DHS] == "host_offline"
    eintraege = _run(speicher.hole_endpoint_health(ENDPOINT_DHS))
    assert eintraege[0]["status"] == "host_offline"


def test_dhs_echter_connect_fehler_bleibt_connect_fehler(speicher, monkeypatch):
    """T-0290: Nicht-DNS-Connect-Fehler (Connection refused) bleibt
    'connect_fehler' -- echtes Erreichbarkeits-Problem, soll alarmieren."""
    job = EndpointHealthJob(
        speicher=speicher, gardena_auth=_AuthStub(),
        gardena_location_id="loc1", gardena_probe_sensor_uuid="sensor-1",
    )

    def antwort(req):
        raise httpx.ConnectError("[Errno 61] Connection refused")

    import bewaesserung.endpoint_health as eh_mod
    transport = _httpx_handler(antwort)

    class _Client(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw.pop("timeout", None)
            super().__init__(transport=transport, **kw)

    monkeypatch.setattr(eh_mod.httpx, "AsyncClient", _Client)
    jetzt = datetime(2026, 5, 4, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_DHS] == "connect_fehler"


# --- FYTA-Tests -------------------------------------------------------------

def test_fyta_token_ok_setzt_status_ok(speicher):
    job = EndpointHealthJob(
        speicher=speicher,
        fyta_client=_FytaStub(token="abc.def.ghi"),
    )
    jetzt = datetime(2026, 5, 4, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_FYTA] == "ok"
    eintraege = _run(speicher.hole_endpoint_health(ENDPOINT_FYTA))
    assert eintraege[0]["status"] == "ok"


def test_fyta_login_crash_setzt_auth_fehler(speicher):
    job = EndpointHealthJob(
        speicher=speicher,
        fyta_client=_FytaStub(crash=True),
    )
    jetzt = datetime(2026, 5, 4, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_FYTA] == "auth_fehler"


def test_fyta_login_returnt_false_setzt_auth_fehler(speicher):
    """_stelle_token_sicher returnt bool. False = Login fehlgeschlagen."""
    job = EndpointHealthJob(
        speicher=speicher,
        fyta_client=_FytaStub(login_erfolg=False),
    )
    jetzt = datetime(2026, 5, 4, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_FYTA] == "auth_fehler"


def test_fyta_token_leer_setzt_schema_fehler(speicher):
    """Login meldet Erfolg (True), aber _token ist leer -> schema_fehler.
    Tatsaechlicher Realfall vom 2026-05-04 15:18: Stub gab 'True' zurueck,
    Code interpretierte das als Token-String."""
    job = EndpointHealthJob(
        speicher=speicher,
        fyta_client=_FytaStub(token=""),  # Login OK, _token leer
    )
    jetzt = datetime(2026, 5, 4, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_FYTA] == "schema_fehler"


# --- FYTA-Schema-Probe (T-0233) ----------------------------------------------

def _fyta_mock(monkeypatch, antwort_factory):
    """Patcht httpx.AsyncClient im endpoint_health-Modul auf MockTransport."""
    import bewaesserung.endpoint_health as eh_mod

    def handler(request: httpx.Request) -> httpx.Response:
        return antwort_factory(request)

    transport = httpx.MockTransport(handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw.pop("timeout", None)
            super().__init__(transport=transport, **kw)

    monkeypatch.setattr(eh_mod.httpx, "AsyncClient", _Client)


def test_fyta_schema_ok_alle_pflichtfelder(speicher, monkeypatch):
    """T-0233: FYTA list-measurements liefert measurement mit allen 4
    Pflicht-Feldern -> status='ok' (Schema-Probe statt nur Token)."""
    job = EndpointHealthJob(
        speicher=speicher,
        fyta_client=_FytaStub(token="abc", pflanzen={42: "name"}),
    )

    def antwort(req):
        return httpx.Response(200, json={
            "user_plants": [{
                "user_plant_id": 42,
                "measurements": [{
                    "soil_moisture": 45.0,
                    "temperature": 21.5,
                    "light": 1200.0,
                    "soil_fertility": 0.8,
                    "date_utc": "2026-05-25T10:00:00",
                }],
            }],
        })

    _fyta_mock(monkeypatch, antwort)
    jetzt = datetime(2026, 5, 25, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_FYTA] == "ok"
    eintraege = _run(speicher.hole_endpoint_health(ENDPOINT_FYTA))
    assert "Schema ok" in eintraege[0]["details"]


def test_fyta_schema_drift_setzt_schema_fehler(speicher, monkeypatch):
    """T-0233: FYTA benennt `soil_moisture` zu `soilMoisture` um (Schema-
    Drift) -> Pflicht-Feld fehlt -> schema_fehler."""
    job = EndpointHealthJob(
        speicher=speicher,
        fyta_client=_FytaStub(token="abc", pflanzen={42: "name"}),
    )

    def antwort(req):
        return httpx.Response(200, json={
            "user_plants": [{
                "user_plant_id": 42,
                "measurements": [{
                    "soilMoisture": 45.0,  # falsche Schreibweise
                    "temperature": 21.5,
                    "light": 1200.0,
                    "soil_fertility": 0.8,
                }],
            }],
        })

    _fyta_mock(monkeypatch, antwort)
    jetzt = datetime(2026, 5, 25, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_FYTA] == "schema_fehler"


def test_fyta_keine_pflanzen_konfiguriert_skipt_schema_probe(speicher):
    """T-0233: Ohne konfigurierte Pflanzen ueberspringt der Job die
    Schema-Probe (Token-only-OK) -- keine FYTA-Pflanze = nichts zu pruefen."""
    job = EndpointHealthJob(
        speicher=speicher,
        fyta_client=_FytaStub(token="abc"),  # _pflanzen_map leer
    )
    jetzt = datetime(2026, 5, 25, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_FYTA] == "ok"
    eintraege = _run(speicher.hole_endpoint_health(ENDPOINT_FYTA))
    assert "Schema-Probe uebersprungen" in eintraege[0]["details"]


def test_fyta_leeres_measurement_array_ist_ok(speicher, monkeypatch):
    """T-0233: 200 + leere measurements (Plant hat im 2-Tage-Fenster keine
    Werte gesendet) ist OK -- kein Schema-Drift-Beleg, Token + Endpoint
    sind valide."""
    job = EndpointHealthJob(
        speicher=speicher,
        fyta_client=_FytaStub(token="abc", pflanzen={42: "name"}),
    )

    def antwort(req):
        return httpx.Response(200, json={
            "user_plants": [{"user_plant_id": 42, "measurements": []}],
        })

    _fyta_mock(monkeypatch, antwort)
    jetzt = datetime(2026, 5, 25, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_FYTA] == "ok"


def test_fyta_http_401_setzt_auth_fehler(speicher, monkeypatch):
    """T-0233: list-measurements liefert 401 -> auth_fehler."""
    job = EndpointHealthJob(
        speicher=speicher,
        fyta_client=_FytaStub(token="abc", pflanzen={42: "name"}),
    )

    def antwort(req):
        return httpx.Response(401, json={"error": "unauthorized"})

    _fyta_mock(monkeypatch, antwort)
    jetzt = datetime(2026, 5, 25, 12, 0)
    stat = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert stat[ENDPOINT_FYTA] == "http_401"
    eintraege = _run(speicher.hole_endpoint_health(ENDPOINT_FYTA))
    assert eintraege[0]["status"] == "auth_fehler"


# --- Intervall-Gate ---------------------------------------------------------

def test_intervall_gate_blockiert_zweiten_tick(speicher):
    job = EndpointHealthJob(
        speicher=speicher, intervall_stunden=24,
        fyta_client=_FytaStub(),
    )
    basis = datetime(2026, 5, 4, 12, 0)
    _run(job.aktualisiere_wenn_faellig(basis))
    stat = _run(job.aktualisiere_wenn_faellig(basis + timedelta(hours=2)))
    assert stat == {"geprueft": 0}


# --- Persistenz: letzter_erfolg-Verhalten -----------------------------------

def test_letzter_erfolg_bleibt_bei_fehler_erhalten(speicher):
    """T-0132: nach erfolgreichem Probe + spaeterem Fehler bleibt
    letzter_erfolg auf dem alten Datum -- der Watchdog kann daraus
    'seit X Tagen broken' berechnen."""
    job = EndpointHealthJob(
        speicher=speicher, intervall_stunden=0,  # immer faellig
        fyta_client=_FytaStub(),
    )
    erst = datetime(2026, 5, 1, 12, 0)
    _run(job.aktualisiere_wenn_faellig(erst))
    eintraege = _run(speicher.hole_endpoint_health(ENDPOINT_FYTA))
    erster_erfolg = eintraege[0]["letzter_erfolg"]
    assert erster_erfolg is not None

    # FYTA-Stub auf Crash umstellen + erneut feuern
    job._fyta_client = _FytaStub(crash=True)
    _run(job.aktualisiere_wenn_faellig(erst + timedelta(days=2)))
    eintraege = _run(speicher.hole_endpoint_health(ENDPOINT_FYTA))
    assert eintraege[0]["status"] == "auth_fehler"
    # letzter_erfolg darf nicht ueberschrieben sein
    assert eintraege[0]["letzter_erfolg"] == erster_erfolg
