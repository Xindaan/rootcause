"""T-0610 E3: Das Response-Retrain-Gate vergleicht fair.

Bis hier stand die CV-MAE des Kandidaten gegen die beim Training
eingefrorene `inverse_mae_s` des Amtsinhabers (gleiche Klasse wie T-0605).
Jetzt rechnet der Amtsinhaber auf den CV-Testzeilen des Kandidaten.

Die eingefrorene Zahl wird in den Tests bewusst verfaelscht: haengt die
Entscheidung noch an ihr, kippt das Ergebnis.
"""
from __future__ import annotations

import asyncio
import json
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
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.ml import response_retrain_job as job_modul
from bewaesserung.ml.response_retrain_job import MlResponseRetrainJob
from bewaesserung.ml.response_vorhersage import MLResponseService

ZONE = "waldblumenhain"


def _pulse(n: int, dauer_faktor_ab: int | None = None, seed: int = 3) -> pd.DataFrame:
    """n Giess-Pulse alle 6 h. Ab Index `dauer_faktor_ab` braucht dieselbe
    Wirkung die vierfache Dauer (Regimewechsel, z. B. neuer Tropfer)."""
    rng = np.random.default_rng(seed)
    t0 = datetime(2026, 1, 1, 6, 0)
    zeilen = []
    for i in range(n):
        dauer_s = float(rng.integers(300, 1800))
        lps = float(rng.uniform(0.05, 0.10))
        f_vor = float(rng.uniform(25, 60))
        et0 = float(rng.uniform(0.5, 3.5))
        delta = 0.02 * dauer_s * lps - 0.1 * f_vor - 0.2 * et0 + 10 + rng.normal(0, 0.3)
        if dauer_faktor_ab is not None and i >= dauer_faktor_ab:
            dauer_s *= 4
        zeilen.append({
            "zone_id": ZONE,
            "zeitstempel": (t0 + timedelta(hours=i * 6)).isoformat(),
            "event_id": i + 1, "shared_valve": False,
            "dauer_s": int(dauer_s), "liter_pro_sekunde": lps,
            "f_vor": f_vor, "f_vor_gradient_3h": 0.0, "f_vor_gradient_24h": 0.0,
            "et0_nach_6h": et0, "niederschlag_nach_24h": 0.0,
            "temperatur_ereignis": 15.0, "vpd_mittel": 1.0,
            "jahreszeit_sin": math.sin(2 * math.pi * i / 365),
            "jahreszeit_cos": math.cos(2 * math.pi * i / 365),
            "tageszeit_sin": 1.0, "tageszeit_cos": 0.0,
            "delta_6h": delta, "delta_12h": delta * 0.9, "delta_24h": delta * 0.6,
        })
    return pd.DataFrame(zeilen)


@pytest.fixture(autouse=True)
def _reset_singleton():
    MLResponseService.zuruecksetzen()
    yield
    MLResponseService.zuruecksetzen()


def _job(tmp_path) -> MlResponseRetrainJob:
    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="t"),
        zonen=[ZonenKonfig(zone_id=ZONE, name="W", ventil_kanal=1)],
        wetter=WetterKonfig(standorte=[WetterStandortKonfig(
            id="o", breite=52.52, laenge=13.405,
        )]),
    )
    resp = MlBewaesserungsResponseKonfig(aktiv=True, min_events=5, gate_mae_faktor=0.5)
    return MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=resp,
        basis_verzeichnis=tmp_path,
    )


def _amtsinhaber(tmp_path, job, eingefrorene_mae: float) -> str:
    """Trainiert den Amtsinhaber auf 30 Pulsen (bis 08.01.) und setzt seine
    eingefrorene MAE auf einen falschen Wert."""
    df = _pulse(30)
    erg = asyncio.run(job._retrain_zone(ZONE, df, datetime(2026, 1, 9), "initial"))
    assert erg["status"] == "uebernommen"
    meta = (tmp_path / ZONE / f"aktuell_{ZONE}_metadata.json").resolve()
    daten = json.loads(meta.read_text())
    daten["inverse_mae_s"] = eingefrorene_mae
    meta.write_text(json.dumps(daten))
    return erg["version"]


def test_t0610_e3_veralteter_amtsinhaber_wird_ersetzt(tmp_path):
    """Regimewechsel ab Puls 30: der Amtsinhaber liegt auf den neuen Pulsen
    weit daneben. Eingefroren behauptet er 1 s MAE -- das alte Gate haette
    jeden Kandidaten abgelehnt."""
    job = _job(tmp_path)
    alt_version = _amtsinhaber(tmp_path, job, eingefrorene_mae=1.0)

    erg = asyncio.run(job._retrain_zone(
        ZONE, _pulse(90, dauer_faktor_ab=30), datetime(2026, 1, 24), "zeit",
    ))

    assert erg["gate_modus"] == "gleiche_testzeilen"
    assert erg["gate_vergleich_n"] >= job_modul.GATE_VERGLEICH_MINDEST_ZEILEN
    assert erg["mae_alt_eingefroren_inverse_s"] == 1.0
    assert erg["status"] == "uebernommen"
    assert erg["mae_neu_inverse_s"] <= 0.5 * erg["mae_alt_inverse_s"]
    assert erg["version"] != alt_version


def test_t0610_e3_kandidat_nicht_besser_wird_abgelehnt(tmp_path):
    """Kein Regimewechsel: der Kandidat ist auf denselben Zeilen nicht doppelt
    so gut. Eingefroren behauptet der Amtsinhaber 1e6 s -- das alte Gate
    haette jeden Kandidaten durchgewunken."""
    job = _job(tmp_path)
    alt_version = _amtsinhaber(tmp_path, job, eingefrorene_mae=1e6)

    erg = asyncio.run(job._retrain_zone(
        ZONE, _pulse(90), datetime(2026, 1, 24), "zeit",
    ))

    assert erg["gate_modus"] == "gleiche_testzeilen"
    assert erg["status"] == "abgelehnt"
    assert erg["mae_alt_inverse_s"] < 1e5
    link = tmp_path / ZONE / f"aktuell_{ZONE}_inverse.lgbm"
    assert link.resolve().parent.name == alt_version  # Rollback


def test_t0610_e3_zu_wenig_zeilen_faellt_auf_eingefrorenes_gate(tmp_path):
    """Liegen zu wenige Testzeilen nach dem Amtsinhaber-Training, gilt das
    alte Gate (Status quo), mit Grund im Ergebnis."""
    job = _job(tmp_path)
    _amtsinhaber(tmp_path, job, eingefrorene_mae=1e6)

    # Nur 6 Pulse nach dem 09.01. + 24 h.
    erg = asyncio.run(job._retrain_zone(
        ZONE, _pulse(42), datetime(2026, 1, 12), "zeit",
    ))

    assert erg["gate_modus"] == "eingefroren"
    assert erg["gate_grund"] == "zu_wenig_zeilen_nach_amtsinhaber_training"
    assert erg["status"] == "uebernommen"
    assert erg["mae_alt_inverse_s"] == 1e6
