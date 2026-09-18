"""T-0610 LI-8: ein Open-Meteo-Fehler wirft die letzte gueltige Vorhersage
nicht mehr weg, solange sie hoechstens 2 h alt ist.

Vorher setzte der Fehlerzweig den Cache auf `stunden=[]`. Ein einziger Fehler
31 min nach dem letzten Abruf liess das Regen-Gate auf "0 mm" fallen und die
Automatik in angekuendigten Regen giessen. Nach 2 h faellt der Client weiter
auf die leere Vorhersage (Frostsperre T-0611 bleibt dann fail-safe).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from bewaesserung.wetter import FEHLER_CACHE_DAUER, WetterClient
from test_wetter import FakeAsyncClient, open_meteo_antwort  # noqa: F401  (Fixture)

_REQ = httpx.Request("GET", "https://api.open-meteo.com/v1/forecast")
ABRUF = datetime(2026, 4, 6, 5, 30)  # Antwort: Regen 07:00 (0,2) und 08:00 (1,5)


def _client(monkeypatch, antwort: dict) -> WetterClient:
    FakeAsyncClient.aufrufe = 0
    FakeAsyncClient.antworten = [httpx.Response(200, json=antwort, request=_REQ)] + [
        httpx.Response(503, json={"error": "kaputt"}, request=_REQ),
    ] * 5
    monkeypatch.setattr("bewaesserung.wetter.httpx.AsyncClient", FakeAsyncClient)
    client = WetterClient(breite=52.52, laenge=13.4, cache_minuten=30)
    client._jetzt = lambda: ABRUF
    return client


@pytest.mark.asyncio
async def test_fehler_behaelt_junge_vorhersage(monkeypatch, open_meteo_antwort):  # noqa: F811
    client = _client(monkeypatch, open_meteo_antwort)
    erste = await client.hole_vorhersage()
    assert len(erste.stunden) == 3

    # Erfolgs-Cache (30 min) abgelaufen, der naechste Abruf scheitert.
    fehler_zeit = ABRUF + timedelta(minutes=31)
    client._jetzt = lambda: fehler_zeit
    nach_fehler = await client.hole_vorhersage()

    assert FakeAsyncClient.aufrufe == 2
    assert nach_fehler.stunden == erste.stunden
    assert nach_fehler.abfrage_zeitstempel == ABRUF  # Alter bleibt ehrlich
    assert nach_fehler.niederschlag_naechste_stunden(6, fehler_zeit) > 0

    # Das kurze Negativ-Gate (T-0563) gilt auch hier: kein Hot-Retry.
    await client.hole_vorhersage()
    assert FakeAsyncClient.aufrufe == 2


@pytest.mark.asyncio
async def test_fehler_nach_zwei_stunden_faellt_auf_leer(monkeypatch, open_meteo_antwort):  # noqa: F811
    """Gegenprobe: der Ersatz ist begrenzt, nicht beliebig alt."""
    client = _client(monkeypatch, open_meteo_antwort)
    await client.hole_vorhersage()

    # Erst ein Fehler innerhalb der 2 h (behaelt), dann einer danach.
    client._jetzt = lambda: ABRUF + timedelta(minutes=31)
    assert (await client.hole_vorhersage()).stunden
    spaet = ABRUF + timedelta(hours=2, minutes=1) + FEHLER_CACHE_DAUER
    client._jetzt = lambda: spaet
    vorhersage = await client.hole_vorhersage()

    assert FakeAsyncClient.aufrufe == 3
    assert vorhersage.stunden == []
