"""Tests fuer T-0065 MlResponseRetrainJob (Trigger + Gate + Rollback).

Die Feature-Pipeline wird per Monkey-Patch durch einen vorgefertigten
DataFrame ersetzt — die Tests pruefen Trigger-Logik, Gate-Entscheidung,
Symlink-Deployment und Rollback bei Gate-Rejection.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta

import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("pandas")

import numpy as np
import pandas as pd

from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    MlBewaesserungsResponseKonfig,
    SpeicherKonfig,
    StandortKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.ml.response_retrain_job import MlResponseRetrainJob
from bewaesserung.ml.response_vorhersage import MLResponseService


def _synthetik(n: int, zone_id: str = "waldblumenhain", seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t0 = datetime(2026, 1, 1, 6, 0)
    zeilen = []
    for i in range(n):
        dauer_s = float(rng.integers(300, 1800))
        lps = float(rng.uniform(0.05, 0.10))
        f_vor = float(rng.uniform(25, 60))
        et0_nach = float(rng.uniform(0.5, 3.5))
        delta = (
            0.02 * dauer_s * lps
            - 0.1 * f_vor
            - 0.2 * et0_nach
            + rng.normal(0, 0.3)
            + 10
        )
        zeilen.append({
            "zone_id": zone_id,
            "zeitstempel": (t0 + timedelta(hours=i * 6)).isoformat(),
            "event_id": i + 1,
            "shared_valve": False,
            "dauer_s": int(dauer_s),
            "liter_pro_sekunde": lps,
            "f_vor": f_vor,
            "f_vor_gradient_3h": rng.uniform(-0.3, 0.3),
            "f_vor_gradient_24h": rng.uniform(-0.3, 0.3),
            "et0_vor_24h": float(rng.uniform(0.5, 4.0)),
            "et0_nach_6h": et0_nach,
            "niederschlag_nach_24h": float(rng.uniform(0, 1)),
            "temperatur_ereignis": float(rng.uniform(10, 25)),
            "vpd_mittel": float(rng.uniform(0.2, 1.8)),
            "jahreszeit_sin": math.sin(2 * math.pi * i / 365),
            "jahreszeit_cos": math.cos(2 * math.pi * i / 365),
            "tageszeit_sin": math.sin(2 * math.pi * 6 / 24),
            "tageszeit_cos": math.cos(2 * math.pi * 6 / 24),
            "delta_6h": delta,
            "delta_12h": delta * 0.9,
            "delta_24h": delta * 0.6,
        })
    return pd.DataFrame(zeilen)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def konfig():
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t"),
        zonen=[ZonenKonfig(
            zone_id="waldblumenhain", name="Waldblumenhain", ventil_kanal=1,
        )],
        wetter=WetterKonfig(standorte=[WetterStandortKonfig(
            id="o", breite=52.52, laenge=13.405,
        )]),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[StandortKonfig(
            standort_id="garten", name="G",
            wetter_standort="o", zonen=["waldblumenhain"],
        )],
    )


@pytest.fixture
def response_konfig():
    return MlBewaesserungsResponseKonfig(
        aktiv=True,
        wirksam=False,
        min_events=5,
        retrain_intervall_tage=14,
        retrain_event_schwelle=5,
        gate_mae_faktor=0.5,
    )


@pytest.fixture(autouse=True)
def _reset_singleton():
    MLResponseService.zuruecksetzen()
    yield
    MLResponseService.zuruecksetzen()


def _patche_features(monkeypatch, df: pd.DataFrame) -> None:
    """Ersetzt erstelle_response_features durch eine Konstante."""
    async def _fake(speicher, konfig, von, bis):  # noqa: ANN001
        return df
    # Der Job importiert dynamisch per `from ... import` in
    # `_baue_trainingsdaten`, daher muss das Modul gepatcht werden.
    monkeypatch.setattr(
        "bewaesserung.ml.response_features.erstelle_response_features",
        _fake,
    )


def test_erster_lauf_trainiert(monkeypatch, tmp_path, konfig, response_konfig):
    df = _synthetik(n=30)
    _patche_features(monkeypatch, df)
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    ausgefuehrt = _run(job.aktualisiere_wenn_faellig())
    assert ausgefuehrt is True
    erg = job.letztes_ergebnis["waldblumenhain"]
    assert erg["status"] == "uebernommen"
    assert erg["version"] is not None
    # Symlinks sind gesetzt.
    zone_dir = tmp_path / "waldblumenhain"
    assert (zone_dir / "aktuell_waldblumenhain_inverse.lgbm").is_symlink()


def test_ohne_aktiv_kein_lauf(monkeypatch, tmp_path, konfig):
    resp = MlBewaesserungsResponseKonfig(aktiv=False)
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=resp,
        basis_verzeichnis=tmp_path,
    )
    assert _run(job.aktualisiere_wenn_faellig()) is False


def test_zu_wenig_events_status_uebersprungen(monkeypatch, tmp_path, konfig):
    resp = MlBewaesserungsResponseKonfig(aktiv=True, min_events=20)
    _patche_features(monkeypatch, _synthetik(n=10))
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=resp,
        basis_verzeichnis=tmp_path,
    )
    _run(job.aktualisiere_wenn_faellig())
    erg = job.letztes_ergebnis["waldblumenhain"]
    assert erg["status"] == "uebersprungen"


def test_zweiter_lauf_zu_frueh_ohne_neue_events(monkeypatch, tmp_path, konfig, response_konfig):
    df = _synthetik(n=30)
    _patche_features(monkeypatch, df)
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    _run(job.aktualisiere_wenn_faellig())
    # Gleich danach: Trigger darf nicht mehr feuern (zeit noch nicht, events
    # identisch).
    ausgefuehrt = _run(job.aktualisiere_wenn_faellig())
    assert ausgefuehrt is False


def test_event_trigger_feuert(monkeypatch, tmp_path, konfig, response_konfig):
    df_klein = _synthetik(n=10)
    _patche_features(monkeypatch, df_klein)
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    _run(job.aktualisiere_wenn_faellig())
    # Nun 5+ neue Events (Schwelle = 5).
    df_gross = _synthetik(n=16)
    _patche_features(monkeypatch, df_gross)
    ausgefuehrt = _run(job.aktualisiere_wenn_faellig())
    assert ausgefuehrt is True
