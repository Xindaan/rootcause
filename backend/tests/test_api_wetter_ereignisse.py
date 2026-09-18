"""T-0241: /api/wetter und /api/wetter/{standort_id} liefern die aktiven
Frost/Hitze/Starkregen-Warnungen im Feld `aktive_ereignisse`.

Vor T-0241 waren WetterEreignisse nur im Ops-Tab via /api/ops/timeline
sichtbar. Die WetterKarte im Default-Tab Uebersicht zeigte sie nicht.
Backend erkennt + persistiert sie schon (wetter_ereignisse.py), Fix war
nur das Lifting des Felds in _wetter_antwort.

Regression-Schutz:
- Aktive Ereignisse (ende > jetzt oder ende=None) erscheinen.
- Abgelaufene Ereignisse (ende < jetzt) werden gefiltert.
- Pro (typ, standort_id) nur der juengste Eintrag (Dedup).
- Backward-Compat: leeres Array bei keinen Ereignissen.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    StandortKonfig,
    WetterEreignis,
    WetterEreignisTyp,
    WetterKonfig,
    WetterStandortKonfig,
    WetterStunde,
    WetterVorhersage,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[ZonenKonfig(zone_id="z", name="Z", ventil_kanal=1)],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten",
                wetter_standort="o", zonen=["z"],
            ),
        ],
    )


def _mock_wetter_manager() -> MagicMock:
    """WetterManager, der eine leere 24h-Vorhersage zurueckgibt."""
    jetzt = datetime.now().replace(minute=0, second=0, microsecond=0)
    stunden = [
        WetterStunde(
            zeitstempel=jetzt + timedelta(hours=i),
            temperatur=10.0, niederschlag_mm=0.0,
            niederschlag_wahrscheinlichkeit=0.0,
            wind_kmh=0.0, wind_richtung_grad=0.0, et0_mm=0.1,
        )
        for i in range(24)
    ]

    async def _hole(standort_id=None):
        return WetterVorhersage(
            abfrage_zeitstempel=datetime.now(),
            stunden=stunden,
        )

    mgr = MagicMock()
    mgr.hole_vorhersage.side_effect = _hole
    mgr.hole_client.return_value = MagicMock()
    return mgr


@pytest.fixture
def client_mit_ereignissen(tmp_path):
    speicher = Speicher(str(tmp_path / "wetter.db"))
    _run(speicher.verbinden())

    jetzt = datetime.now()

    # Aktive Frost-Warnung (heute Nacht): ende noch in Zukunft.
    _run(speicher.speichere_wetter_ereignis(WetterEreignis(
        zeitstempel=jetzt - timedelta(hours=2),
        typ=WetterEreignisTyp.FROST,
        standort_id="o",
        details="Min -2.3 C um 04:00",
        beginn=jetzt + timedelta(hours=8),
        ende=jetzt + timedelta(hours=14),
    )))
    # Abgelaufene Hitze-Warnung von gestern: ende ist Vergangenheit.
    _run(speicher.speichere_wetter_ereignis(WetterEreignis(
        zeitstempel=jetzt - timedelta(hours=20),
        typ=WetterEreignisTyp.HITZE,
        standort_id="o",
        details="Max 36 C",
        beginn=jetzt - timedelta(hours=18),
        ende=jetzt - timedelta(hours=4),
    )))
    # Zwei Starkregen-Eintraege fuer den gleichen Tag/Standort -> Dedup
    # behaelt den juengeren.
    _run(speicher.speichere_wetter_ereignis(WetterEreignis(
        zeitstempel=jetzt - timedelta(hours=6),
        typ=WetterEreignisTyp.STARKREGEN,
        standort_id="o",
        details="20 mm/3h erwartet",
        beginn=jetzt + timedelta(hours=2),
        ende=jetzt + timedelta(hours=5),
    )))
    _run(speicher.speichere_wetter_ereignis(WetterEreignis(
        zeitstempel=jetzt - timedelta(hours=1),
        typ=WetterEreignisTyp.STARKREGEN,
        standort_id="o",
        details="25 mm/3h erwartet (Update)",
        beginn=jetzt + timedelta(hours=3),
        ende=jetzt + timedelta(hours=6),
    )))

    konfiguriere_api(
        speicher, _konfig(), MagicMock(), MagicMock(),
        wetter_manager=_mock_wetter_manager(),
    )
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        _run(speicher.schliessen())


def test_wetter_endpoint_liefert_aktive_ereignisse(client_mit_ereignissen):
    """Frost (aktiv) muss im Response sein, Hitze (abgelaufen) nicht."""
    r = client_mit_ereignissen.get("/api/wetter/o")
    assert r.status_code == 200
    body = r.json()
    assert "aktive_ereignisse" in body
    typen = [e["typ"] for e in body["aktive_ereignisse"]]
    assert "frost" in typen
    assert "hitze" not in typen  # abgelaufen
    assert "starkregen" in typen


def test_wetter_endpoint_dedupliziert_pro_typ_und_standort(
    client_mit_ereignissen,
):
    """Bei zwei Starkregen-Eintraegen fuer denselben Standort muss nur
    der juengere durchkommen."""
    r = client_mit_ereignissen.get("/api/wetter/o")
    starkregen = [
        e for e in r.json()["aktive_ereignisse"] if e["typ"] == "starkregen"
    ]
    assert len(starkregen) == 1
    assert starkregen[0]["details"] == "25 mm/3h erwartet (Update)"


def test_wetter_endpoint_ohne_ereignisse_leeres_array(tmp_path):
    """Backward-Compat: leeres Array statt fehlendem Feld."""
    speicher = Speicher(str(tmp_path / "leer.db"))
    _run(speicher.verbinden())
    konfiguriere_api(
        speicher, _konfig(), MagicMock(), MagicMock(),
        wetter_manager=_mock_wetter_manager(),
    )
    client = TestClient(app)
    try:
        r = client.get("/api/wetter/o")
        assert r.status_code == 200
        body = r.json()
        assert body.get("aktive_ereignisse") == []
    finally:
        client.close()
        _run(speicher.schliessen())
