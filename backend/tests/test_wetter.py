from datetime import datetime, timedelta

import httpx
import pytest

from bewaesserung.wetter import WetterClient


@pytest.fixture
def open_meteo_antwort() -> dict:
    return {
        "hourly": {
            "time": [
                "2026-04-06T06:00",
                "2026-04-06T07:00",
                "2026-04-06T08:00",
            ],
            "temperature_2m": [12.3, 13.1, 14.0],
            "precipitation": [0.0, 0.2, 1.5],
            "precipitation_probability": [5, 15, 80],
            "wind_speed_10m": [8.2, 7.9, 10.1],
            "wind_direction_10m": [180.0, 200.0, 220.0],
            "et0_fao_evapotranspiration": [0.05, 0.08, 0.11],
        }
    }


class FakeAsyncClient:
    antworten: list[httpx.Response] = []
    aufrufe = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url: str, params: dict) -> httpx.Response:
        FakeAsyncClient.aufrufe += 1
        return FakeAsyncClient.antworten.pop(0)


@pytest.mark.asyncio
async def test_hole_vorhersage_parst_open_meteo_antwort(monkeypatch, open_meteo_antwort):
    FakeAsyncClient.aufrufe = 0
    FakeAsyncClient.antworten = [
        httpx.Response(
            200,
            json=open_meteo_antwort,
            request=httpx.Request("GET", "https://api.open-meteo.com/v1/forecast"),
        )
    ]
    monkeypatch.setattr("bewaesserung.wetter.httpx.AsyncClient", FakeAsyncClient)

    client = WetterClient(breite=52.52, laenge=13.4, cache_minuten=30)

    vorhersage = await client.hole_vorhersage()

    assert len(vorhersage.stunden) == 3
    assert vorhersage.stunden[0].zeitstempel == datetime(2026, 4, 6, 6, 0)
    assert vorhersage.stunden[0].temperatur == 12.3
    assert vorhersage.stunden[1].niederschlag_mm == 0.2
    assert vorhersage.stunden[2].niederschlag_wahrscheinlichkeit == 80
    assert vorhersage.stunden[2].wind_kmh == 10.1
    assert vorhersage.stunden[2].et0_mm == 0.11
    assert vorhersage.stunden[0].wind_richtung_grad == 180.0


@pytest.mark.asyncio
async def test_hole_vorhersage_nutzt_cache(monkeypatch, open_meteo_antwort):
    FakeAsyncClient.aufrufe = 0
    FakeAsyncClient.antworten = [
        httpx.Response(
            200,
            json=open_meteo_antwort,
            request=httpx.Request("GET", "https://api.open-meteo.com/v1/forecast"),
        )
    ]
    monkeypatch.setattr("bewaesserung.wetter.httpx.AsyncClient", FakeAsyncClient)

    client = WetterClient(breite=52.52, laenge=13.4, cache_minuten=30)

    erste = await client.hole_vorhersage()
    zweite = await client.hole_vorhersage()

    assert FakeAsyncClient.aufrufe == 1
    assert erste is zweite


@pytest.mark.asyncio
async def test_hole_vorhersage_gibt_bei_http_fehler_leere_vorhersage_zurueck(monkeypatch):
    FakeAsyncClient.aufrufe = 0
    FakeAsyncClient.antworten = [
        httpx.Response(
            503,
            json={"error": "kaputt"},
            request=httpx.Request("GET", "https://api.open-meteo.com/v1/forecast"),
        )
    ]
    monkeypatch.setattr("bewaesserung.wetter.httpx.AsyncClient", FakeAsyncClient)

    client = WetterClient(breite=52.52, laenge=13.4, cache_minuten=30)

    vorhersage = await client.hole_vorhersage()

    assert FakeAsyncClient.aufrufe == 1
    assert vorhersage.stunden == []


@pytest.mark.asyncio
async def test_t0563_fehlerpfad_setzt_kurzes_negativ_gate(monkeypatch):
    """Ein fehlgeschlagener Abruf darf nicht in einen Hot-Retry laufen.

    Vorher blieb `_cache_gueltig_bis` im Fehlerfall ungesetzt, also ging
    JEDER Folgeaufruf erneut gegen Open-Meteo -- Retry ohne Backoff,
    waehrend die Entscheidungsschleife pro Zyklus mehrfach fragt.
    """
    FakeAsyncClient.aufrufe = 0
    FakeAsyncClient.antworten = [
        httpx.Response(
            503, json={"error": "kaputt"},
            request=httpx.Request("GET", "https://api.open-meteo.com/v1/forecast"),
        ),
    ] * 5
    monkeypatch.setattr("bewaesserung.wetter.httpx.AsyncClient", FakeAsyncClient)
    client = WetterClient(breite=52.52, laenge=13.4, cache_minuten=30)

    for _ in range(3):
        assert (await client.hole_vorhersage()).stunden == []

    assert FakeAsyncClient.aufrufe == 1, (
        f"nur der erste Aufruf darf raus, waren {FakeAsyncClient.aufrufe}"
    )


@pytest.mark.asyncio
async def test_t0563_negativ_gate_laeuft_ab(monkeypatch):
    """Gegenprobe: die Sperre ist kurz, nicht dauerhaft.

    Ohne diesen Fall koennte das Gate auf Stunden gesetzt werden und eine
    kurze Stoerung wuerde die Wetterdaten fuer den Rest des Tages
    einfrieren.
    """
    from bewaesserung.wetter import FEHLER_CACHE_DAUER

    FakeAsyncClient.aufrufe = 0
    FakeAsyncClient.antworten = [
        httpx.Response(
            503, json={"error": "kaputt"},
            request=httpx.Request("GET", "https://api.open-meteo.com/v1/forecast"),
        ),
    ] * 5
    monkeypatch.setattr("bewaesserung.wetter.httpx.AsyncClient", FakeAsyncClient)
    client = WetterClient(breite=52.52, laenge=13.4, cache_minuten=30)

    await client.hole_vorhersage()
    # Uhr ueber die Sperrzeit hinaus stellen.
    client._jetzt = lambda: datetime.now() + FEHLER_CACHE_DAUER + timedelta(seconds=1)
    await client.hole_vorhersage()

    assert FakeAsyncClient.aufrufe == 2
    assert FEHLER_CACHE_DAUER < timedelta(minutes=10), (
        "die Sperre muss deutlich kuerzer sein als der Erfolgs-Cache"
    )
