"""T-0370: API-Feld `autonom_scharf` in /api/zonen.

Das Frontend (V4-Karte) unterscheidet damit ehrlich zwischen
autonom-scharfen Zonen (AUTOMATIK) und Shadow-Zonen (modus=automatik,
aber nicht im Auto-Loop). Die 3-stufige Wahrheit (T-0334):
global ventilsteuerung_aktiv UND modus=automatik UND auto_loop_opt_in.
Zonen-Teil zentral in modelle.ist_auto_loop_zone -- derselbe Filter,
den der Auto-Loop nutzt (_baue_auto_loop_kanaele).
"""
from __future__ import annotations

import asyncio

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
    ZonenModus,
    ist_auto_loop_zone,
)
from bewaesserung.speicher import Speicher
from unittest.mock import MagicMock


def _run(coro):
    return asyncio.run(coro)


def _zone(zone_id, *, modus=ZonenModus.AUTOMATIK, kanal=1, opt_in=False):
    return ZonenKonfig(
        zone_id=zone_id, name=zone_id, modus=modus,
        ventil_kanal=kanal, auto_loop_opt_in=opt_in,
    )


def _gesamt_konfig(zonen, *, ventilsteuerung_aktiv):
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=zonen,
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten",
                wetter_standort="o", zonen=[z.zone_id for z in zonen],
            ),
        ],
        ventilsteuerung_aktiv=ventilsteuerung_aktiv,
    )


def test_ist_auto_loop_zone_dreistufig():
    """Zonen-Teil der Scharf-Logik: automatik + kanal + opt_in."""
    assert ist_auto_loop_zone(_zone("scharf", opt_in=True)) is True
    assert ist_auto_loop_zone(_zone("shadow", opt_in=False)) is False
    assert ist_auto_loop_zone(
        _zone("monitoring", modus=ZonenModus.MONITORING, opt_in=True)
    ) is False
    assert ist_auto_loop_zone(_zone("ohne_kanal", kanal=None, opt_in=True)) is False


@pytest.fixture
def client_faktory(tmp_path):
    """Baut einen TestClient fuer eine gegebene GesamtKonfig."""
    speicher = Speicher(str(tmp_path / "scharf.db"))
    _run(speicher.verbinden())
    clients: list[TestClient] = []

    def _mach(konfig):
        konfiguriere_api(speicher, konfig, MagicMock(), MagicMock())
        c = TestClient(app)
        clients.append(c)
        return c

    try:
        yield _mach
    finally:
        for c in clients:
            c.close()
        _run(speicher.schliessen())


def test_api_zonen_autonom_scharf_nur_opt_in(client_faktory):
    """Global scharf: nur die Opt-in-Automatik-Zone ist autonom_scharf."""
    zonen = [
        _zone("bambuswald", opt_in=True),
        _zone("waldblumenhain", opt_in=False),          # Shadow
        _zone("magerwiese", modus=ZonenModus.MONITORING),
    ]
    client = client_faktory(_gesamt_konfig(zonen, ventilsteuerung_aktiv=True))
    antwort = client.get("/api/zonen")
    assert antwort.status_code == 200
    scharf = {z["zone_id"]: z["autonom_scharf"] for z in antwort.json()}
    assert scharf == {
        "bambuswald": True,
        "waldblumenhain": False,
        "magerwiese": False,
    }


def test_api_zonen_autonom_scharf_global_aus(client_faktory):
    """Master-Switch aus: KEINE Zone autonom_scharf, auch Opt-in nicht."""
    zonen = [_zone("bambuswald", opt_in=True)]
    client = client_faktory(_gesamt_konfig(zonen, ventilsteuerung_aktiv=False))
    antwort = client.get("/api/zonen")
    assert antwort.status_code == 200
    assert antwort.json()[0]["autonom_scharf"] is False
