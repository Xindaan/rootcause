"""T-0606: Host-Pruefung gegen DNS-Rebinding.

Gemessen 25.09. (Gesamtreview API-1): ein fremder Host-Header bekam auf '/'
und das JS-Bundle 200, das Bundle traegt den control-Key. Ein per
DNS-Rebinding auf 127.0.0.1 umgebogener Name schickt seinen eigenen Namen im
Host-Header -- der Dienst muss ihn deshalb vor jedem Routing ablehnen, fuer
API UND statische Auslieferung. Erlaubte Hosts (127.0.0.1, localhost, mit und
ohne Port) muessen weiter funktionieren.

Nur lesende GETs auf public-Routen ueber den In-Process-Client, keine
Steuerroute: der Test ist auch ohne die Host-Pruefung harmlos.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bewaesserung.api_server import app

FREMDE_HOSTS = ["rebind.example:8090", "evil.example", "127.0.0.1.nip.io:8090"]
ERLAUBTE_HOSTS = ["127.0.0.1:8090", "localhost:8090", "127.0.0.1", "localhost:5173"]


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.parametrize("host", FREMDE_HOSTS)
@pytest.mark.parametrize("pfad", ["/", "/assets/index-abc123.js", "/api/health"])
def test_fremder_host_wird_vor_dem_routing_abgelehnt(client, host, pfad):
    antwort = client.get(pfad, headers={"host": host})
    assert antwort.status_code == 400, (
        f"Host {host!r} auf {pfad} bekam {antwort.status_code} -- ein per "
        "DNS-Rebinding umgebundener Name koennte Dashboard und Bundle laden"
    )
    assert "text/html" not in antwort.headers.get("content-type", "")


@pytest.mark.parametrize("host", ERLAUBTE_HOSTS)
def test_erlaubter_host_bekommt_weiter_antwort(client, host):
    """Gegenprobe: die Pruefung darf den eigenen Zugriff nicht aussperren."""
    assert client.get("/", headers={"host": host}).status_code == 200
    assert client.get("/api/health", headers={"host": host}).status_code == 200
