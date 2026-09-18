"""T-0108: ML-Job-Crashes werden in /api/ml/status sichtbar.

Vorher loggten `MlRetrainJob`, `MlResponseRetrainJob` und
`KalibrationsJob` einen Crash nur via `logger.exception` — ein stiller
Ausfall blieb bis zur naechsten Log-Sichtung unentdeckt. Jetzt fuehrt
jeder Job ein `letzter_fehler`-Feld (`{zeit, typ, nachricht}`), das beim
naechsten erfolgreichen Lauf wieder geleert wird, und `/api/ml/status`
exponiert alle drei.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.kalibrierung import KalibrationsJob
from bewaesserung.ml.response_retrain_job import MlResponseRetrainJob
from bewaesserung.ml.retrain_job import MlRetrainJob
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    KalibrierungKonfig,
    MlBewaesserungsResponseKonfig,
    MlRetrainKonfig,
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
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=[ZonenKonfig(zone_id="testzone", name="Test", ventil_kanal=1)],
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
    s = Speicher(str(tmp_path / "ml_fehler.db"))
    _run(s.verbinden())
    yield s
    _run(s.schliessen())


# --- KalibrationsJob ---------------------------------------------------

def test_kalibrationsjob_erfasst_crash(speicher, monkeypatch):
    """Scan-Crash -> `letzter_fehler` gesetzt, vorher nur Log."""
    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig(aktiv=True))

    async def _crash(_jetzt):
        raise RuntimeError("kalibrier-scan kaputt")

    monkeypatch.setattr(job, "_scan_alle_zonen", _crash)
    erg = _run(job.aktualisiere_wenn_faellig(datetime(2026, 5, 21, 8, 0)))

    assert erg is False
    assert job.letzter_fehler is not None
    assert job.letzter_fehler["typ"] == "RuntimeError"
    assert "kaputt" in job.letzter_fehler["nachricht"]
    assert job.letzter_erfolg is None


def test_kalibrationsjob_erfolg_loescht_fehler(speicher, monkeypatch):
    """Nach einem Crash setzt der naechste erfolgreiche Scan alles zurueck."""
    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig(aktiv=True))

    async def _crash(_jetzt):
        raise RuntimeError("erster lauf kaputt")

    monkeypatch.setattr(job, "_scan_alle_zonen", _crash)
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 5, 21, 8, 0)))
    assert job.letzter_fehler is not None

    async def _ok(_jetzt):
        return None

    monkeypatch.setattr(job, "_scan_alle_zonen", _ok)
    erg = _run(job.aktualisiere_wenn_faellig(datetime(2026, 5, 21, 14, 0)))

    assert erg is True
    assert job.letzter_fehler is None
    assert job.letzter_erfolg == datetime(2026, 5, 21, 14, 0)


# --- MlResponseRetrainJob ---------------------------------------------

def test_response_retrain_erfasst_daten_fehler(speicher, monkeypatch, tmp_path):
    """Der globale Daten-Fehler war vorher still (nur Log)."""
    resp = MlBewaesserungsResponseKonfig(aktiv=True)
    job = MlResponseRetrainJob(
        speicher, _konfig(), resp, basis_verzeichnis=str(tmp_path),
    )

    async def _crash(_jetzt):
        raise ValueError("response-features kaputt")

    monkeypatch.setattr(job, "_baue_trainingsdaten", _crash)
    erg = _run(job.aktualisiere_wenn_faellig(datetime(2026, 5, 21, 8, 0)))

    assert erg is False
    assert job.letzter_fehler is not None
    assert job.letzter_fehler["typ"] == "ValueError"
    assert "kaputt" in job.letzter_fehler["nachricht"]


# --- MlRetrainJob ------------------------------------------------------

def test_ml_retrain_erfasst_crash_und_erfolg_loescht(speicher, monkeypatch, tmp_path):
    """Crash -> `letzter_fehler` gesetzt; erfolgreicher Lauf -> None."""
    retrain = MlRetrainKonfig(
        aktiv=True, intervall_tage=7, start_verzoegerung_minuten=0,
    )
    job = MlRetrainJob(speicher, _konfig(), retrain, ausgabe_pfad=str(tmp_path))

    async def _crash(_jetzt):
        raise RuntimeError("retrain kaputt")

    monkeypatch.setattr(job, "_fuehre_aus", _crash)
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 5, 21, 8, 0)))
    assert job.letzter_fehler is not None
    assert job.letzter_fehler["typ"] == "RuntimeError"
    assert job.letzter_erfolg is None

    async def _ok(_jetzt):
        return {"status": "uebernommen"}

    monkeypatch.setattr(job, "_fuehre_aus", _ok)
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 5, 29, 8, 0)))
    assert job.letzter_fehler is None
    assert job.letzter_erfolg == datetime(2026, 5, 29, 8, 0)


# --- /api/ml/status ----------------------------------------------------

def test_ml_status_endpoint_zeigt_alle_drei_job_bloecke(speicher, tmp_path):
    """Endpoint exponiert retrain + response_retrain + kalibrierung."""
    konfig = _konfig()
    retrain_job = MlRetrainJob(
        speicher, konfig,
        MlRetrainKonfig(aktiv=True, start_verzoegerung_minuten=0),
        ausgabe_pfad=str(tmp_path / "r"),
    )
    response_job = MlResponseRetrainJob(
        speicher, konfig, MlBewaesserungsResponseKonfig(aktiv=True),
        basis_verzeichnis=str(tmp_path / "resp"),
    )
    kalib_job = KalibrationsJob(speicher, konfig, KalibrierungKonfig(aktiv=True))
    # Einen Crash simulieren, damit der Fehler-Block im Response auftaucht.
    kalib_job._letzter_fehler = {
        "zeit": "2026-05-21T08:00:00", "typ": "RuntimeError",
        "nachricht": "scan kaputt",
    }

    konfiguriere_api(
        speicher, konfig, MagicMock(), MagicMock(),
        ml_retrain_job=retrain_job,
        ml_response_retrain_job=response_job,
        kalibrations_job=kalib_job,
    )
    client = TestClient(app)
    try:
        antwort = client.get("/api/ml/status")
        assert antwort.status_code == 200
        daten = antwort.json()
        assert "retrain" in daten
        assert "response_retrain" in daten
        assert "kalibrierung" in daten
        assert daten["retrain"]["letzter_fehler"] is None
        assert daten["kalibrierung"]["letzter_fehler"]["typ"] == "RuntimeError"
        assert daten["kalibrierung"]["letzter_fehler"]["nachricht"] == "scan kaputt"
    finally:
        client.close()
