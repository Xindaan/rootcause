"""T-0349: Tests fuer GET /api/ml/physik-bias (Regime-Filter + Regression).

Der Endpoint bestand vor T-0349 ohne eigenen Test; die Regression hier
fixiert das Bestandsverhalten OHNE regime-Param (gemischte Aggregate)
und prueft den neuen Filter + die additiven Shadow-Felder.
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
        zonen=[ZonenKonfig(zone_id="z1", name="Z1", ventil_kanal=1)],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten",
                wetter_standort="o", zonen=["z1"],
            ),
        ],
    )


async def _zeile(
    speicher: Speicher, ts: datetime, abw_6h: float, abw_phys_6h: float,
    regime_6h: str, abw_ss_6h: float | None = None,
) -> None:
    await speicher.setze_empfehlungs_audit(
        zeitstempel=ts, zone_id="z1", empfehlungs_typ="kein_bedarf",
        soll_bewaessern=False, blocker_typ=None, feuchte_aktuell=50.0,
        welkepunkt_wert=30.0, optimum_min=40.0, optimum_max=60.0,
        prognose_quelle="ml", prognose_6h=48.0, prognose_12h=45.0,
        prognose_24h=42.0, tage_bis_welkepunkt=3.0,
        dauer_s_empfehlung=None, aktive_strategie="korridor",
        prognose_physik_6h=47.0,
        prognose_statespace_6h=46.5 if abw_ss_6h is not None else None,
    )
    zeilen = await speicher.hole_empfehlungs_audit(tage=7)
    audit_id = next(
        z["id"] for z in zeilen if z["zeitstempel"] == ts.isoformat()
    )
    await speicher.aktualisiere_empfehlungs_audit_eval(
        audit_id=audit_id, ist_feuchte_6h=48.0 - abw_6h,
        ist_feuchte_24h=None, abweichung_6h=abw_6h, abweichung_24h=None,
        evaluiert_am=ts + timedelta(hours=7),
        abweichung_physik_6h=abw_phys_6h,
        abweichung_statespace_6h=abw_ss_6h,
    )
    await speicher.setze_empfehlungs_audit_regime(
        audit_id=audit_id, regime_6h=regime_6h,
    )


@pytest.fixture
def bias_client(tmp_path):
    speicher = Speicher(str(tmp_path / "bias_api.db"))
    _run(speicher.verbinden())
    jetzt = datetime.now()
    # Zwei Zeilen: trocknung (abw 2.0/1.0) + giess_recovery (abw 6.0/9.0).
    _run(_zeile(
        speicher, jetzt - timedelta(hours=10), 2.0, 1.0, "trocknung",
        abw_ss_6h=0.5,
    ))
    _run(_zeile(
        speicher, jetzt - timedelta(hours=20), -6.0, -9.0, "giess_recovery",
        abw_ss_6h=-4.0,
    ))
    konfiguriere_api(speicher, _konfig(), MagicMock(), MagicMock())
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        _run(speicher.schliessen())


def test_ohne_regime_param_bestandsverhalten(bias_client):
    """Regression: ohne regime-Param mischen die Aggregate alle Regimes
    (Bestandsfelder identisch zur Vor-T-0349-Implementierung)."""
    antwort = bias_client.get("/api/ml/physik-bias?tage=14")
    assert antwort.status_code == 200
    daten = antwort.json()
    assert "regime" not in daten
    z1 = daten["pro_zone"]["z1"]
    assert z1["n"] == 2
    assert z1["n_mit_physik"] == 2
    # (|2.0| + |-6.0|) / 2 = 4.0 ; (|1.0| + |-9.0|) / 2 = 5.0
    assert z1["mae_ml_6h"] == 4.0
    assert z1["mae_physik_6h"] == 5.0
    # bias = mean(48.0 - 47.0) = 1.0
    assert z1["bias_pp_6h"] == 1.0
    # Additive Shadow-Felder vorhanden: (0.5 + 4.0) / 2 = 2.25.
    assert z1["mae_statespace_6h"] == 2.25
    assert z1["mae_heuristik_24h"] is None
    # Kein Filter-Feld ohne Param.
    assert "n_regime_6h" not in z1


def test_regime_filter_trennt_buckets(bias_client):
    antwort = bias_client.get("/api/ml/physik-bias?tage=14&regime=trocknung")
    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["regime"] == "trocknung"
    z1 = daten["pro_zone"]["z1"]
    # Nur die trocknung-Zeile zaehlt in die 6h-Metriken.
    assert z1["mae_ml_6h"] == 2.0
    assert z1["mae_physik_6h"] == 1.0
    assert z1["mae_statespace_6h"] == 0.5
    assert z1["n_regime_6h"] == 1
    # n bleibt Gesamtzahl (Transparenz ueber den Filter-Anteil).
    assert z1["n"] == 2


def test_regime_param_validierung(bias_client):
    antwort = bias_client.get("/api/ml/physik-bias?regime=quatsch")
    assert antwort.status_code == 422
