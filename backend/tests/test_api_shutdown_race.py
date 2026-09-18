"""T-0171: Shutdown-Race-Guard.

Beim Beenden ueber `./start.sh` + Ctrl-C kann ein Inflight-Request noch
einen DB-Read starten, nachdem `speicher.schliessen()` die Connection
geschlossen hat. Vorher crashte das mit `sqlite3.ProgrammingError` /
`ValueError: no active connection` / `AssertionError` und uvicorn loggte
einen Stack-Trace. Der `shutdown_race_guard`-Middleware faengt das ab,
solange `_speicher._geschlossen` gesetzt ist, und antwortet leise mit
503. Im Normalbetrieb (Flag False) bleibt ein echter Fehler ein 500.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    StandortKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[ZonenKonfig(zone_id="testzone", name="Testzone", ventil_kanal=1)],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten",
                wetter_standort="o", zonen=["testzone"],
            ),
        ],
    )


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "shutdown.db"))
    _run(s.verbinden())
    yield s
    # Teardown MUSS schliessen() aufrufen — sonst bleibt der aiosqlite-
    # Worker-Thread am Leben und blockiert den pytest-Prozess-Exit.
    # `schliessen()` ist idempotent (T-0171), Tests die selbst schon
    # geschlossen haben, stoeren das nicht.
    _run(s.schliessen())


def test_schliessen_ist_idempotent_und_setzt_flag(speicher):
    """`schliessen()` setzt `_geschlossen` und darf doppelt laufen."""
    assert speicher._geschlossen is False
    _run(speicher.schliessen())
    assert speicher._geschlossen is True
    # Zweiter Aufruf darf nicht crashen.
    _run(speicher.schliessen())
    assert speicher._geschlossen is True


def test_request_nach_schliessen_gibt_leise_503(speicher):
    """Inflight-Request nach Shutdown -> 503, kein 500/Stacktrace."""
    konfiguriere_api(speicher, _konfig(), MagicMock(), MagicMock())
    client = TestClient(app)
    try:
        # Normalbetrieb: Request kommt sauber durch.
        assert client.get("/api/zonen").status_code == 200
        # Shutdown simulieren.
        _run(speicher.schliessen())
        antwort = client.get("/api/zonen")
        assert antwort.status_code == 503
        assert "herunter" in antwort.json()["detail"].lower()
    finally:
        client.close()


def test_echter_fehler_im_normalbetrieb_bleibt_500(speicher, monkeypatch):
    """Flag False -> ein echter Fehler wird NICHT als 503 verschluckt."""
    konfiguriere_api(speicher, _konfig(), MagicMock(), MagicMock())

    async def _explodiere():
        raise ValueError("kuenstlicher bug im normalbetrieb")

    # `/api/zonen` ruft als erstes `offene_sensor_warnungen()`.
    monkeypatch.setattr(speicher, "offene_sensor_warnungen", _explodiere)
    # raise_server_exceptions=False: der 500-Pfad statt Re-Raise im Test.
    client = TestClient(app, raise_server_exceptions=False)
    try:
        antwort = client.get("/api/zonen")
        assert antwort.status_code == 500  # echter Bug bleibt sichtbar
        assert speicher._geschlossen is False
    finally:
        client.close()
