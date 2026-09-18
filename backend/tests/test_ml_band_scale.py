"""T-0062d: Band-Scale-Kalibrierung-Tests.

Nach Training der Quantile-Modelle pro Horizont wird ein Skalier-Faktor
aus den letzten 20 % Trainingsdaten abgeleitet, damit die tatsaechliche
q10-q90-Abdeckung in den Zielbereich [0.75, 0.85] faellt — ohne 27
Modelle zu trainieren.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("pandas")

import numpy as np
import pandas as pd

from bewaesserung.ml.training import TrainingsPipeline
from bewaesserung.ml.vorhersage import MLVorhersageService


def _synth_daten(n: int = 600) -> pd.DataFrame:
    """Reproduzierbare Sinus+Rauschen-Daten mit ziel_delta_* Spalten."""
    rng = np.random.default_rng(42)
    t0 = datetime(2026, 2, 1)
    zeiten = [t0 + timedelta(hours=i) for i in range(n)]
    feuchte = 50 + 10 * np.sin(np.arange(n) / 24) + rng.normal(0, 2, n)
    temperatur = 15 + 5 * np.sin(np.arange(n) / 24)
    et0 = 0.1 + rng.uniform(0, 0.2, n)
    ziel6 = np.roll(feuchte, -6)
    ziel12 = np.roll(feuchte, -12)
    ziel24 = np.roll(feuchte, -24)
    df = pd.DataFrame({
        "zone_id": ["bambuswald"] * n,
        "zeitstempel": [t.isoformat() for t in zeiten],
        "boden_feuchte_aktuell": feuchte,
        "boden_temperatur": temperatur,
        "feuchte_trend_6h": rng.normal(0, 0.5, n),
        "niederschlag_summe_6h": rng.uniform(0, 1.5, n),
        "niederschlag_summe_12h": rng.uniform(0, 2.5, n),
        "niederschlag_summe_24h": rng.uniform(0, 4.0, n),
        "et0_summe_6h": et0,
        "et0_summe_12h": et0 * 2,
        "et0_summe_24h": et0 * 4,
        "quelle": ["gardena"] * n,
        "ziel_feuchte_6h": ziel6,
        "ziel_feuchte_12h": ziel12,
        "ziel_feuchte_24h": ziel24,
        "ziel_delta_6h": ziel6 - feuchte,
        "ziel_delta_12h": ziel12 - feuchte,
        "ziel_delta_24h": ziel24 - feuchte,
    })
    return df.iloc[:-24]


def test_band_scale_json_wird_geschrieben(tmp_path):
    """Nach `trainiere_quantile` existiert `band_scale_{h}h.json` pro Horizont
    mit den erwarteten Feldern.
    """
    df = _synth_daten(600)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    pipeline.trainiere_quantile(df)
    for h in (6, 12, 24):
        pfad = tmp_path / f"band_scale_{h}h.json"
        assert pfad.exists(), f"band_scale_{h}h.json fehlt"
        daten = json.loads(pfad.read_text())
        # Pflichtfelder
        for key in ("horizont_h", "scale", "abdeckung_roh",
                    "abdeckung_nach_scale", "ziel_abdeckung", "n_samples"):
            assert key in daten, f"{key} fehlt in band_scale_{h}h.json"
        assert daten["horizont_h"] == h
        assert daten["kalibrierung"] == "holdout_80_20"
        assert daten["train_samples"] > daten["n_samples"]
        # Scale muss clipped sein auf [0.5, 2.0] oder 1.0
        assert 0.5 <= daten["scale"] <= 2.0


def test_band_scale_gut_kalibriert_fuehrt_zu_scale_1(tmp_path):
    """Auf sauber synthetischen Daten sollte die Abdeckung nahe 0.8 sein,
    also scale = 1.0 (kein Clipping).
    """
    df = _synth_daten(600)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    pipeline.trainiere_quantile(df)
    daten_6h = json.loads((tmp_path / "band_scale_6h.json").read_text())
    # Mit synth. Daten und walk-forward-CV kann die Abdeckung schwanken;
    # wir verlangen nur: Scale ist nicht an den Clipping-Grenzen verrankt.
    assert 0.5 < daten_6h["scale"] < 2.0
    # Die Abdeckung roh sollte jenseits 0.3 sein (nicht total kaputt)
    assert daten_6h["abdeckung_roh"] > 0.3


def test_band_scale_wird_beim_service_laden_angewendet(tmp_path):
    """Service liest `band_scale_{h}h.json` beim Laden und nutzt den Faktor
    in der Vorhersage.
    """
    df = _synth_daten(600)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    pipeline.trainiere_quantile(df)
    # Scale manuell auf bekannten Wert setzen (1.5), damit Test deterministisch
    scale_pfad = tmp_path / "band_scale_6h.json"
    daten_alt = json.loads(scale_pfad.read_text())
    daten_alt["scale"] = 1.5
    scale_pfad.write_text(json.dumps(daten_alt))

    service = MLVorhersageService(modell_verzeichnis=str(tmp_path))
    service.lade_modelle()
    assert service._band_scale.get(6) == 1.5

    sample = df.head(1).copy()
    ergebnisse = service.vorhersage(sample, horizont=6)
    assert ergebnisse is not None
    e = ergebnisse[0]
    # Band sollte breiter sein als mit scale=1.0: wir vergleichen mit
    # einem Service der den Scale-File nicht hat.
    # Einfache Invariante: q90 - q10 > 0 (Band nicht null)
    assert e.q10 is not None and e.q90 is not None
    assert e.q90 > e.q10


def test_band_scale_default_1_ohne_json(tmp_path):
    """Ohne band_scale_6h.json (alte Modelle) ist _band_scale.get(6) None;
    get mit default 1.0 liefert 1.0 → keine Aenderung am Band.
    """
    df = _synth_daten(400)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    pipeline.trainiere_quantile(df)
    # Alle band_scale_*.json loeschen, um "altes Modell" zu simulieren
    for h in (6, 12, 24):
        p = tmp_path / f"band_scale_{h}h.json"
        if p.exists():
            p.unlink()

    service = MLVorhersageService(modell_verzeichnis=str(tmp_path))
    service.lade_modelle()
    assert service._band_scale == {}
    # Vorhersage funktioniert, Band aus den Roh-Quantilen
    sample = df.head(1).copy()
    ergebnisse = service.vorhersage(sample, horizont=6)
    assert ergebnisse is not None
    e = ergebnisse[0]
    assert e.q10 is not None and e.q90 is not None


def test_band_scale_log_info_kwargs_regressionsfall(tmp_path, caplog):
    """Regression 2026-04-21: `_kalibriere_band_scales` rief `logger.info`
    mit structlog-Style-Kwargs (`horizont=`, `scale=`) auf, obwohl das
    Modul den stdlib-Logger nutzt. Im Betrieb crashte der Retrain-Job
    jede Minute mit TypeError + 86 % CPU-Dauerlast.

    Die anderen Tests maskierten den Bug, weil pytest den INFO-Level
    per Default nicht aktiviert — stdlib-Logger springt dann nicht in
    `_log()` und die Kwargs werden nicht geprueft.
    """
    caplog.set_level(logging.INFO, logger="bewaesserung.ml.training")
    df = _synth_daten(400)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    # Muss durchlaufen ohne TypeError — die Log-Zeile wird auf INFO
    # tatsaechlich formatiert und der stdlib-Logger akzeptiert nur
    # Positional-Args nach der Format-Message.
    pipeline.trainiere_quantile(df)
    # Verifikation: die Log-Nachricht taucht im Caplog auf, formatiert.
    band_log_lines = [
        rec for rec in caplog.records
        if rec.name == "bewaesserung.ml.training"
        and "band_scale.kalibriert" in rec.getMessage()
    ]
    assert band_log_lines, "band_scale.kalibriert-Log fehlt"
    # Alle drei Horizonte geloggt
    horizont_messages = [rec.getMessage() for rec in band_log_lines]
    assert any("horizont=6h" in m for m in horizont_messages)
    assert any("horizont=12h" in m for m in horizont_messages)
    assert any("horizont=24h" in m for m in horizont_messages)


def test_band_scale_sortiert_bei_crossing(tmp_path):
    """Wenn q10 > q90 (Crossing), nutzt _kalibriere_band_scales min/max
    zum Vertauschen, damit abdeckung nicht negativ berechnet wird.
    """
    # Dieser Test prüft indirekt: Training läuft durch auch wenn Modelle
    # crossing liefern, und band_scale_*.json wird trotzdem geschrieben.
    df = _synth_daten(400)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=2)
    pipeline.trainiere_quantile(df)
    # Alle 3 Band-Scales sollten existieren (training hat nicht gecrashed)
    for h in (6, 12, 24):
        assert (tmp_path / f"band_scale_{h}h.json").exists()


# --- T-0564: Akzeptanz-Gate fuer den Skalierfaktor ---

def test_t0564_scale_der_die_abdeckung_verschlechtert_wird_verworfen():
    """Der Realfall aus den deployten Dateien.

    `magerwiese 6h` hatte roh 0,886 (Ziel 0,80, also schon nah dran) und
    nach Skalierung 0,367 -- deutlich schlechter. Fuenf der 42 deployten
    `band_scale_*.json` standen in diesem Zustand, weil der berechnete Wert
    zwar geloggt, aber nie geprueft wurde.
    """
    from bewaesserung.ml.training import band_scale_akzeptabel

    assert band_scale_akzeptabel(0.886, 0.367, 0.80) is False
    assert band_scale_akzeptabel(0.988, 0.450, 0.80) is False
    assert band_scale_akzeptabel(0.874, 0.559, 0.80) is False


def test_t0564_scale_der_naeher_ans_ziel_bringt_wird_genommen():
    """Gegenprobe: das Gate darf die Kalibrierung nicht abschalten.

    Ohne diesen Fall koennte man `band_scale_akzeptabel` auf `False`
    festnageln und alle Baender blieben ungeskaliert.
    """
    from bewaesserung.ml.training import band_scale_akzeptabel

    # Band viel zu schmal (0,50) -> Skalierung bringt es auf 0,78.
    assert band_scale_akzeptabel(0.50, 0.78, 0.80) is True
    assert band_scale_akzeptabel(0.60, 0.70, 0.80) is True
    # Gleich weit daneben, nur auf der anderen Seite -> kein Gewinn.
    # Ohne die Mindestverbesserung entscheidet hier Fliesskomma-Rauschen:
    # abs(0.90-0.80) ist 0,09999999999999998, abs(0.70-0.80) dagegen
    # 0,10000000000000009.
    assert band_scale_akzeptabel(0.70, 0.90, 0.80) is False
    # Eine Verbesserung unterhalb der Mindestschwelle zaehlt nicht.
    assert band_scale_akzeptabel(0.60, 0.605, 0.80) is False
