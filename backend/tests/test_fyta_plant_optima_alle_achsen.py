"""T-0196a: hole_plant_optima_alle_achsen — FYTA Plant-Detail-Response
in dict[achse, dict] umwandeln.

Roh-Sample basiert auf Live-Response gegen fyta_id=100002 (Mandevilla)
vom 16.05. (siehe docs/recherche_fyta_extra_felder.md, Korrektur-Sektion).
"""
from __future__ import annotations

import httpx
import pytest

from bewaesserung.fyta_client import FytaClient
from bewaesserung.modelle import FytaKonfig, FytaPflanzenKonfig


# Sample-Response, gekuerzt auf die relevanten Achsen.
SAMPLE_MANDEVILLA = {
    "plant": {
        "id": 100002,
        "nickname": "Mandevilla-Baer",
        "scientific_name": "Mandevilla sanderi",
        "measurements": {
            "moisture": {
                "type": "moisture",
                "status": 3,
                "values": {
                    "min_good": "30", "max_good": "70",
                    "min_acceptable": "20", "max_acceptable": "80",
                    "current": "62", "currentFormatted": "62",
                },
                "unit": "%/h",
            },
            "light": {
                "type": "light",
                "status": 2,
                "values": {
                    "min_good": "11.5", "max_good": "460",
                    "min_acceptable": "2.75", "max_acceptable": "690",
                    "current": "471", "currentFormatted": "471",
                    "optimal_hours": 0,
                },
                "dli_values": {
                    "min_good": "4", "max_good": "20",
                    "min_acceptable": "0.02", "max_acceptable": "30",
                },
                "unit": "μmol/h",
                "dli_unit": "mol/day",
            },
            "temperature": {
                "type": "temperature",
                "status": 3,
                "values": {
                    "min_good": "5", "max_good": "25",
                    "min_acceptable": "0", "max_acceptable": "30",
                    "current": "29", "currentFormatted": "29",
                },
                "unit": "°C/h",
            },
            "salinity": {
                "type": "salinity",
                "status": 0,
                "values": {
                    "min_good": "0.2", "max_good": "1",
                    "min_acceptable": "0.1", "max_acceptable": "1.3",
                    "current": "0", "currentFormatted": "0.16",
                },
                "unit": "mS/cm/h",
            },
            "air_humidity": {
                "type": "air_humidity",
                "status": None,
                "values": {
                    "min_good": "40", "max_good": "70",
                    "min_acceptable": "30", "max_acceptable": "80",
                    "current": None, "currentFormatted": None,
                },
                "unit": "%",
            },
            "ph": {
                "type": "ph",
                "status": None,
                "values": {
                    "min": "4", "max": "7", "current": None,
                },
                "unit": "pH",
            },
            "nutrients": {"type": "nutrients", "status": 3},
            "battery": 100,
        },
    }
}


def _client_mit_fake(monkeypatch, json_response: dict, status_code: int = 200):
    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url: str, headers: dict) -> httpx.Response:
            return httpx.Response(
                status_code,
                json=json_response,
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(
        "bewaesserung.fyta_client.httpx.AsyncClient", _FakeAsyncClient
    )
    return FytaClient(
        FytaKonfig(
            api_url="https://fyta.example/api",
            pflanzen=[
                FytaPflanzenKonfig(
                    fyta_id=100002, zone_id="mandevilla", name="Mandevilla",
                ),
            ],
        ),
        access_token="token",
    )


@pytest.mark.asyncio
async def test_alle_achsen_erkannt(monkeypatch):
    """Sample-Response liefert genau die 5 erwarteten internen Achsen."""
    client = _client_mit_fake(monkeypatch, SAMPLE_MANDEVILLA)
    ergebnis = await client.hole_plant_optima_alle_achsen(100002)
    assert ergebnis is not None
    assert set(ergebnis.keys()) == {
        "feuchte", "licht_ppfd", "licht_dli", "temperatur", "salinitaet",
    }


@pytest.mark.asyncio
async def test_schwellen_korrekt_geparst(monkeypatch):
    """Konkrete Werte aus dem Sample, defensive float-Konvertierung."""
    client = _client_mit_fake(monkeypatch, SAMPLE_MANDEVILLA)
    res = await client.hole_plant_optima_alle_achsen(100002)
    assert res["feuchte"]["min_good"] == 30.0
    assert res["feuchte"]["max_good"] == 70.0
    assert res["feuchte"]["min_akzeptabel"] == 20.0
    assert res["feuchte"]["max_akzeptabel"] == 80.0
    assert res["feuchte"]["current"] == 62.0
    assert res["feuchte"]["einheit"] == "%/h"

    assert res["licht_ppfd"]["min_good"] == 11.5
    assert res["licht_ppfd"]["max_good"] == 460.0
    assert res["licht_ppfd"]["current"] == 471.0
    assert res["licht_ppfd"]["einheit"] == "μmol/h"

    # DLI hat KEIN current, nur Schwellen
    assert res["licht_dli"]["min_good"] == 4.0
    assert res["licht_dli"]["max_good"] == 20.0
    assert res["licht_dli"]["current"] is None
    assert res["licht_dli"]["einheit"] == "mol/day"

    assert res["temperatur"]["max_good"] == 25.0
    assert res["temperatur"]["current"] == 29.0

    assert res["salinitaet"]["min_good"] == 0.2
    assert res["salinitaet"]["max_good"] == 1.0
    # Salinity current ist "0" (string), unsere defensive float-Konvertierung
    # gibt 0.0. currentFormatted "0.16" ist nicht der Roh-Wert, wir folgen
    # `current` als Quelle.
    assert res["salinitaet"]["current"] == 0.0


@pytest.mark.asyncio
async def test_air_humidity_und_ph_werden_ignoriert(monkeypatch):
    """air_humidity und ph haben current=None bei realen Pflanzen -> nicht im Ergebnis."""
    client = _client_mit_fake(monkeypatch, SAMPLE_MANDEVILLA)
    res = await client.hole_plant_optima_alle_achsen(100002)
    assert "air_humidity" not in res
    assert "ph" not in res
    assert "nutrients" not in res
    assert "battery" not in res


@pytest.mark.asyncio
async def test_http_fehler_liefert_none(monkeypatch):
    """500 vom Server -> sauber None, kein Crash."""
    client = _client_mit_fake(
        monkeypatch, {"error": "internal"}, status_code=500,
    )
    res = await client.hole_plant_optima_alle_achsen(100002)
    assert res is None


@pytest.mark.asyncio
async def test_fehlende_measurements_liefert_none(monkeypatch):
    """Response ohne plant.measurements -> None mit Warn-Log, kein Crash."""
    client = _client_mit_fake(
        monkeypatch, {"plant": {"id": 1, "nickname": "test"}},
    )
    res = await client.hole_plant_optima_alle_achsen(100002)
    assert res is None


@pytest.mark.asyncio
async def test_partielle_response_teilachsen(monkeypatch):
    """Wenn FYTA nur einzelne Achsen liefert (z.B. neue Pflanze ohne
    Sensor-Daten), kommen nur diese im Ergebnis -- nicht leeres Dict."""
    partial = {
        "plant": {
            "id": 1,
            "measurements": {
                "moisture": SAMPLE_MANDEVILLA["plant"]["measurements"]["moisture"],
            }
        }
    }
    client = _client_mit_fake(monkeypatch, partial)
    res = await client.hole_plant_optima_alle_achsen(1)
    assert res is not None
    assert set(res.keys()) == {"feuchte"}


@pytest.mark.asyncio
async def test_dli_aus_light_node_extrahiert(monkeypatch):
    """licht_ppfd und licht_dli kommen beide aus dem `light`-Knoten,
    aber aus unterschiedlichen Sub-Keys (values vs. dli_values).
    Wenn dli_values fehlt, soll trotzdem licht_ppfd erhalten bleiben."""
    response = {
        "plant": {
            "id": 1,
            "measurements": {
                "light": {
                    "values": {
                        "min_good": "100", "max_good": "500",
                        "min_acceptable": "50", "max_acceptable": "800",
                        "current": "200",
                    },
                    "unit": "μmol/h",
                }
            }
        }
    }
    client = _client_mit_fake(monkeypatch, response)
    res = await client.hole_plant_optima_alle_achsen(1)
    assert res is not None
    assert "licht_ppfd" in res
    assert "licht_dli" not in res
    assert res["licht_ppfd"]["min_good"] == 100.0
