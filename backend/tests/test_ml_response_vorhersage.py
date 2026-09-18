"""Tests fuer T-0065 MLResponseService (Inverse-Inferenz)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("pandas")

import numpy as np
import pandas as pd

from bewaesserung.ml.response_training import ResponseTrainingsPipeline
from bewaesserung.ml.response_vorhersage import (
    MLResponseService,
    MIN_ZIEL_DELTA,
    MAX_ZIEL_DELTA,
)


def _synthetik_events(n: int, seed: int = 11) -> pd.DataFrame:
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
            "zone_id": "waldblumenhain",
            "zeitstempel": (t0 + timedelta(hours=i * 6)).isoformat(),
            "event_id": i + 1,
            "shared_valve": False,
            "dauer_s": int(dauer_s),
            "liter_pro_sekunde": lps,
            "f_vor": f_vor,
            "f_vor_gradient_3h": rng.uniform(-0.4, 0.4),
            "f_vor_gradient_24h": rng.uniform(-0.4, 0.4),
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


@pytest.fixture(autouse=True)
def _reset_singleton():
    MLResponseService.zuruecksetzen()
    yield
    MLResponseService.zuruecksetzen()


def _trainiere(tmp_path: Path, n: int = 40) -> None:
    pipe = ResponseTrainingsPipeline(
        zone_id="waldblumenhain",
        basis_verzeichnis=tmp_path,
        min_events=5,
    )
    erg = pipe.lauf(_synthetik_events(n=n))
    assert erg.status == "ok", erg.grund


def test_keine_zone_geladen_gibt_none(tmp_path):
    svc = MLResponseService.instanz(tmp_path)
    assert svc.inverse_dauer(
        zone_id="waldblumenhain", f_vor=30, ziel_schwelle=55,
    ) is None


def test_laden_und_inferieren_liefert_dauer(tmp_path):
    _trainiere(tmp_path, n=40)
    svc = MLResponseService.instanz(tmp_path)
    assert svc.lade_zone("waldblumenhain") is True
    assert svc.ist_geladen("waldblumenhain")
    dauer = svc.inverse_dauer(
        zone_id="waldblumenhain", f_vor=30.0, ziel_schwelle=55.0,
        et0_nach_6h=2.0, vpd_mittel=0.9,
    )
    assert dauer is not None
    assert 0 < dauer < 30_000


def test_ziel_delta_null_gibt_none(tmp_path):
    _trainiere(tmp_path, n=30)
    svc = MLResponseService.instanz(tmp_path)
    svc.lade_zone("waldblumenhain")
    # Boden schon ueber Ziel → keine Empfehlung
    assert svc.inverse_dauer(
        zone_id="waldblumenhain", f_vor=60.0, ziel_schwelle=55.0,
    ) is None


def test_clip_bei_extremem_delta(tmp_path):
    """Sehr grosse f_vor-/Schwellen-Differenz wird geclippt, kein Crash."""
    _trainiere(tmp_path, n=30)
    svc = MLResponseService.instanz(tmp_path)
    svc.lade_zone("waldblumenhain")
    dauer_klein = svc.inverse_dauer(
        zone_id="waldblumenhain", f_vor=30, ziel_schwelle=30 + MIN_ZIEL_DELTA,
    )
    dauer_gross = svc.inverse_dauer(
        zone_id="waldblumenhain", f_vor=10, ziel_schwelle=10 + MAX_ZIEL_DELTA + 50,
    )
    # Beide liefern einen Wert, trotz Extrapolation.
    assert dauer_klein is not None
    assert dauer_gross is not None
    # Monotonie nicht erzwungen (Clip wirkt), aber Aufruf war erfolgreich.


def test_reload_bei_neuem_symlink(tmp_path):
    """Wenn der Symlink auf ein neues Modell zeigt, laedt force=True neu."""
    _trainiere(tmp_path, n=30)
    svc = MLResponseService.instanz(tmp_path)
    svc.lade_zone("waldblumenhain")
    v1 = svc.version("waldblumenhain")
    # Zweites Training mit anderem Timestamp.
    import time
    time.sleep(1.1)
    _trainiere(tmp_path, n=30)
    svc.lade_zone("waldblumenhain", force=True)
    v2 = svc.version("waldblumenhain")
    assert v1 != v2


def test_singleton_erster_aufruf_braucht_pfad():
    MLResponseService.zuruecksetzen()
    with pytest.raises(RuntimeError):
        MLResponseService.instanz(None)


def test_t0564_unbekannte_feature_spalten_geben_none(tmp_path):
    """Schema-Bruch ist kein fehlender Messwert.

    Unbekannte Spalten wurden mit 0.0 imputiert -- das erzeugt eine Dauer,
    die wie ein Ergebnis aussieht (reproduziert: 928 s bei zwei von drei
    unbekannten Features). Der Aufrufer clippt nur auf
    `[MIN_DAUER, max_dauer_sekunden]` und wuerde das bei aktivem
    `ml_dosis_wirksam` als echtes Wasser fahren. Der Docstring nannte den
    Fall seit jeher als None-Fall; der Code tat es nicht.
    """
    _trainiere(tmp_path, n=40)
    svc = MLResponseService.instanz(tmp_path)
    assert svc.lade_zone("waldblumenhain") is True

    # Eine BEKANNTE Spalte durch eine unbekannte ersetzen -- die Anzahl
    # bleibt gleich, der Frame passt also weiter zum Booster. Genau das ist
    # der gefaehrliche Fall: ohne Guard wird der fehlende Wert mit 0.0
    # imputiert und das Modell liefert eine Dauer, die wie ein Ergebnis
    # aussieht. Haengt man stattdessen Spalten AN, scheitert schon die
    # Form-Pruefung von LightGBM -- der Guard waere dann nicht geprueft.
    eintrag = svc._modelle["waldblumenhain"]
    spalten = list(eintrag["feature_cols"])
    spalten[0] = "gibt_es_nicht_a"
    eintrag["feature_cols"] = spalten

    assert svc.inverse_dauer(
        zone_id="waldblumenhain", f_vor=30.0, ziel_schwelle=55.0,
        et0_nach_6h=2.0, vpd_mittel=0.9,
    ) is None


def test_t0564_bekanntes_schema_liefert_weiterhin_eine_dauer(tmp_path):
    """Gegenprobe: der Guard darf nicht jede Inferenz abschalten."""
    _trainiere(tmp_path, n=40)
    svc = MLResponseService.instanz(tmp_path)
    svc.lade_zone("waldblumenhain")

    dauer = svc.inverse_dauer(
        zone_id="waldblumenhain", f_vor=30.0, ziel_schwelle=55.0,
        et0_nach_6h=2.0, vpd_mittel=0.9,
    )
    assert dauer is not None and dauer > 0
