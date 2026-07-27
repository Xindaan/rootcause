"""T-0066: API-Endpoint /api/zonen/{zone_id}/empfehlung-jetzt.

Smoke-Tests fuer den Dry-Run-Endpoint:
- Happy Path: 200 + Schema (alle Pflichtfelder vorhanden).
- Unbekannte Zone: 404.
- Cache-Header gesetzt (damit das Panel-Polling den Server nicht flutet).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    GiessEmpfehlung,
    MlPhysikDiagnoseKonfig,
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
        zonen=[
            ZonenKonfig(zone_id="bambuswald", name="Bambus", ventil_kanal=2),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten",
                wetter_standort="o", zonen=["bambuswald"],
            ),
        ],
    )


@pytest.fixture
def client_mit_mock_motor(tmp_path):
    speicher = Speicher(str(tmp_path / "empfehlung_api.db"))
    _run(speicher.verbinden())

    # Motor-Attrappe: vorhersage_zone liefert konstantes GiessEmpfehlung-Objekt.
    motor = MagicMock()

    async def _empf(
        zone_id: str, sicherheits_tage_override: float | None = None,
    ) -> GiessEmpfehlung:
        return GiessEmpfehlung(
            zone_id=zone_id,
            zeitstempel=datetime(2026, 4, 23, 18, 0),
            soll_bewaessern=True,
            grund="Feuchte 25% unter Schwelle 35%",
            feuchte_aktuell=25.0,
            effektive_schwelle=35.0,
            dauer_s_heuristik=600,
            liter_heuristik=18.7,
            ml_aktiv=False,
            ml_wirksam=False,
        )
    motor.vorhersage_zone = _empf

    konfiguriere_api(speicher, _konfig(), motor, MagicMock())
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        _run(speicher.schliessen())


def test_empfehlung_jetzt_happy_path(client_mit_mock_motor):
    antwort = client_mit_mock_motor.get("/api/zonen/bambuswald/empfehlung-jetzt")
    assert antwort.status_code == 200
    daten = antwort.json()
    # Alle Pflichtfelder vorhanden (Schema-Check)
    for feld in (
        "zone_id", "zeitstempel", "soll_bewaessern", "grund",
        "feuchte_aktuell", "effektive_schwelle",
        "dauer_s_heuristik", "liter_heuristik",
        "ml_aktiv", "ml_wirksam",
    ):
        assert feld in daten, f"Pflichtfeld '{feld}' fehlt im Response"
    assert daten["zone_id"] == "bambuswald"
    assert daten["soll_bewaessern"] is True
    assert daten["dauer_s_heuristik"] == 600
    assert daten["liter_heuristik"] == 18.7


def test_empfehlung_jetzt_unbekannte_zone_liefert_404(client_mit_mock_motor):
    antwort = client_mit_mock_motor.get("/api/zonen/nicht-existent/empfehlung-jetzt")
    assert antwort.status_code == 404


def test_empfehlung_jetzt_setzt_cache_header(client_mit_mock_motor):
    """Polling alle 60 s → Cache-Control sollte gesetzt sein, damit das
    Panel den Server nicht unnoetig haemmert."""
    antwort = client_mit_mock_motor.get("/api/zonen/bambuswald/empfehlung-jetzt")
    assert antwort.status_code == 200
    cache = antwort.headers.get("cache-control", "")
    assert "max-age=60" in cache


def test_empfehlung_jetzt_akzeptiert_sicherheits_tage_override(client_mit_mock_motor):
    """T-0075: ?sicherheits_tage=N wird akzeptiert (FastAPI parst+validiert)."""
    antwort = client_mit_mock_motor.get(
        "/api/zonen/bambuswald/empfehlung-jetzt?sicherheits_tage=7",
    )
    assert antwort.status_code == 200


def test_empfehlung_jetzt_clipt_sicherheits_tage_grenzen(client_mit_mock_motor):
    """Werte ausserhalb [0.5, 30] werden von FastAPI als 422 abgelehnt."""
    antwort = client_mit_mock_motor.get(
        "/api/zonen/bambuswald/empfehlung-jetzt?sicherheits_tage=100",
    )
    assert antwort.status_code == 422
    antwort = client_mit_mock_motor.get(
        "/api/zonen/bambuswald/empfehlung-jetzt?sicherheits_tage=-1",
    )
    assert antwort.status_code == 422


# --- Hybrid Stufe 1: read-only Physik-Diagnose ---


@pytest.fixture
def client_mit_physik_motor(tmp_path):
    """Variante des Fixtures, die `welkepunkt_wert` setzt und die
    Konfig mit `ml_physik_diagnose.aktiv=True` ausstattet."""
    speicher = Speicher(str(tmp_path / "empfehlung_api_physik.db"))
    _run(speicher.verbinden())

    motor = MagicMock()

    async def _empf(zone_id, sicherheits_tage_override=None):
        return GiessEmpfehlung(
            zone_id=zone_id,
            zeitstempel=datetime(2026, 5, 27, 18, 0),
            soll_bewaessern=False,
            grund="im Bereich",
            feuchte_aktuell=60.0,
            welkepunkt_wert=20.0,
            welkepunkt_quelle="kalibrierung",
            ml_aktiv=False,
            ml_wirksam=False,
        )
    motor.vorhersage_zone = _empf

    konf = _konfig()
    konf.ml_physik_diagnose = MlPhysikDiagnoseKonfig(aktiv=True)
    konfiguriere_api(speicher, konf, motor, MagicMock())
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        _run(speicher.schliessen())


def test_empfehlung_jetzt_default_tau_fallback(client_mit_physik_motor):
    """Ohne gefittetes `k_basis` -> `physik_quelle == "default_tau"`,
    Prognose-Felder sind nicht None und liegen zwischen Welkepunkt
    und Startwert."""
    antwort = client_mit_physik_motor.get(
        "/api/zonen/bambuswald/empfehlung-jetzt",
    )
    assert antwort.status_code == 200
    d = antwort.json()
    assert d["physik_quelle"] == "default_tau"
    assert d["k_basis_pro_h"] is not None
    assert d["k_basis_pro_h"] > 0
    # f_start=60, wp=20 -> Prognose monoton fallend, ueber Welkepunkt.
    assert d["prognose_physik_6h"] is not None
    assert 20.0 <= d["prognose_physik_24h"] < 60.0
    assert d["prognose_physik_6h"] >= d["prognose_physik_24h"]
    # Read-only: Entscheidungs-Felder bleiben unveraendert.
    assert d["soll_bewaessern"] is False


def test_empfehlung_jetzt_physik_aus_speicher(client_mit_physik_motor, tmp_path):
    """Wenn `physik_k_basis` einen Eintrag enthaelt -> Quelle "gefittet"
    und `k_basis_pro_h` matched den Wert in der Tabelle."""
    # Speicher des Clients holen + UPSERT machen.
    from bewaesserung import api_server
    speicher = api_server._speicher
    assert speicher is not None
    _run(speicher.upsert_k_basis(
        zone_id="bambuswald",
        k_basis=0.02, et0_basis_mm_pro_h=0.1042,
        n_phasen=4, mae=0.5,
    ))
    antwort = client_mit_physik_motor.get(
        "/api/zonen/bambuswald/empfehlung-jetzt",
    )
    assert antwort.status_code == 200
    d = antwort.json()
    assert d["physik_quelle"] == "gefittet"
    assert abs(d["k_basis_pro_h"] - 0.02) < 1e-9
