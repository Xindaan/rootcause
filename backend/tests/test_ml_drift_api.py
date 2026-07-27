"""Integrationstest fuer GET /api/ml/drift (T-0047)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.ml.modelle_ml import FeatureBeitrag, MLVorhersage
from bewaesserung.modelle import (
    DatenQuelle,
    GardenaKonfig,
    GesamtKonfig,
    SensorMessung,
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
        zonen=[ZonenKonfig(zone_id="bambuswald", name="Bambus", ventil_kanal=2)],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten",
                name="Garten",
                wetter_standort="o",
                zonen=["bambuswald"],
            ),
        ],
    )


@pytest.fixture
def drift_client(tmp_path):
    speicher = Speicher(str(tmp_path / "drift_api.db"))
    _run(speicher.verbinden())

    inferenz = datetime.now() - timedelta(hours=8)
    ziel = inferenz + timedelta(hours=6)
    _run(speicher.logge_ml_vorhersage(
        zeitstempel=inferenz, zone_id="bambuswald", horizont_h=6,
        prognose_ziel_zeit=ziel, prognose_feuchte=40.0,
        modell_version="modell_6h_2026-04-19.lgbm",
    ))
    _run(speicher.speichere_messung(SensorMessung(
        zeitstempel=ziel, zone_id="bambuswald",
        boden_feuchte=44.0, quelle=DatenQuelle.GARDENA,
    )))
    _run(speicher.evaluiere_offene_vorhersagen(datetime.now()))

    # ML-Service attrappe mit Baseline-Metriken
    ml_service = MagicMock()
    ml_service.ist_verfuegbar = True
    ml_service.status.return_value = MagicMock(
        trainiert_am="2026-04-19T00:00:00",
        metriken=[
            MagicMock(horizont_stunden=6, mae=2.0),
            MagicMock(horizont_stunden=12, mae=3.0),
        ],
    )

    konfiguriere_api(
        speicher, _konfig(), MagicMock(), MagicMock(), ml_service=ml_service,
    )
    client = TestClient(app)
    try:
        yield client, speicher
    finally:
        client.close()
        _run(speicher.schliessen())


def test_ml_drift_liefert_horizonte_und_ampel(drift_client):
    client, _ = drift_client

    antwort = client.get("/api/ml/drift?zone_id=bambuswald&fenster=7d")
    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["zone_id"] == "bambuswald"
    assert daten["fenster_tage"] == 7
    # 6h ist evaluiert (MAE = 4.0), 12h hat nur Baseline aber keine Messung
    per_h = {h["horizont_h"]: h for h in daten["horizonte"]}
    assert per_h[6]["mae_aktuell"] == 4.0
    assert per_h[6]["n"] == 1
    assert per_h[6]["mae_baseline"] == 2.0
    # 4.0 > 1.5 * 2.0 = 3.0 → rot
    assert per_h[6]["ampel"] == "rot"
    # 12h hat keine aktuellen Daten → keine_daten
    assert per_h[12]["mae_aktuell"] is None
    assert per_h[12]["ampel"] == "keine_daten"


def test_ml_drift_parst_fenster_und_faellt_auf_7d_zurueck(drift_client):
    client, _ = drift_client

    antwort = client.get("/api/ml/drift?fenster=30d")
    assert antwort.status_code == 200
    assert antwort.json()["fenster_tage"] == 30

    antwort = client.get("/api/ml/drift?fenster=ungueltig")
    assert antwort.json()["fenster_tage"] == 7


def test_ml_drift_gruen_wenn_mae_unter_schwelle(drift_client, monkeypatch):
    """Wenn aktueller MAE unter 1.5 * baseline bleibt, Ampel 'gruen'."""
    client, speicher = drift_client

    async def gute_metriken(zone_id, fenster_tage, jetzt=None):
        return {6: {"mae": 2.5, "n": 5}}

    monkeypatch.setattr(speicher, "hole_drift_metriken", gute_metriken)

    antwort = client.get("/api/ml/drift?zone_id=bambuswald")
    per_h = {h["horizont_h"]: h for h in antwort.json()["horizonte"]}
    # 2.5 < 1.5 * 2.0 = 3.0 → gruen
    assert per_h[6]["ampel"] == "gruen"


def test_ml_drift_log_endpoint_liefert_eintraege(drift_client):
    """T-0080c: Inspektor-Endpoint liefert Prognose vs. Ist als JSON-Liste."""
    client, _ = drift_client
    antwort = client.get("/api/ml/drift/log?zone_id=bambuswald&horizont=6&n=10")
    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["zone_id"] == "bambuswald"
    assert daten["horizont"] == 6
    assert daten["n_zurueck"] == 1
    assert len(daten["eintraege"]) == 1
    e = daten["eintraege"][0]
    assert e["prognose_feuchte"] == 40.0
    assert e["ist_feuchte"] == 44.0
    assert e["abweichung"] == 4.0


def test_ml_drift_log_endpoint_lehnt_ungueltigen_horizont_ab(drift_client):
    client, _ = drift_client
    antwort = client.get("/api/ml/drift/log?horizont=99")
    assert "fehler" in antwort.json()


def test_ml_drift_ampel_backlog_wenn_offen_aber_unevaluiert(drift_client, monkeypatch):
    """T-0080: Wenn der Drift-Job mit Backlog hinterherhinkt — Prognosen
    sind im Fenster, aber unevaluiert — soll Ampel 'backlog' liefern statt
    'keine_daten'. So sieht man im UI, dass Daten KOMMEN, nicht fehlen."""
    client, speicher = drift_client

    async def keine_evaluierten(zone_id, fenster_tage, jetzt=None):
        return {}  # nichts evaluiert

    async def status_mit_backlog(zone_id, fenster_tage, jetzt=None,
                                  toleranz_minuten=30):
        return {
            6: {"n_offen_im_fenster": 423, "n_offen_total": 1000,
                "letzte_evaluierung": "2026-04-23T20:49:40"},
            12: {"n_offen_im_fenster": 0, "n_offen_total": 0,
                 "letzte_evaluierung": None},
            24: {"n_offen_im_fenster": 0, "n_offen_total": 0,
                 "letzte_evaluierung": None},
        }

    monkeypatch.setattr(speicher, "hole_drift_metriken", keine_evaluierten)
    monkeypatch.setattr(speicher, "hole_drift_status", status_mit_backlog)

    antwort = client.get("/api/ml/drift?zone_id=bambuswald")
    per_h = {h["horizont_h"]: h for h in antwort.json()["horizonte"]}
    # 6h: hat Backlog → ampel=backlog, n_offen sichtbar
    assert per_h[6]["ampel"] == "backlog"
    assert per_h[6]["n_offen_im_fenster"] == 423
    assert per_h[6]["letzte_evaluierung"] == "2026-04-23T20:49:40"
    # 12h: nichts offen, nichts evaluiert → klassisch keine_daten
    assert per_h[12]["ampel"] == "keine_daten"
    assert per_h[12]["n_offen_im_fenster"] == 0


def test_dauer_drift_endpoint_aggregiert_mae(tmp_path):
    """T-0065: /api/ml/dauer-drift gruppiert MAE-Heuristik vs. ML pro Zone
    und setzt die Ampel nach Gate-Kriterium (gruen/gelb/rot)."""
    speicher = Speicher(str(tmp_path / "dauer_drift_api.db"))
    _run(speicher.verbinden())
    try:
        zeit = datetime(2026, 4, 22, 6, 0)
        # Zone A (waldblumenhain): ML schlaegt Heuristik klar (gruen).
        #   heuristik_s=1560 -> prog=26, fehler=|26-5|=21
        #   ml_s=600         -> prog=10, fehler=|10-5|=5
        #   5 <= 0.5*21 = 10.5  -> gruen
        _run(speicher.speichere_dauer_vorschlag(
            zeitstempel=zeit, zone_id="waldblumenhain",
            f_vor=25.0, ziel_schwelle=45.0,
            heuristik_s=1560, ml_s=600,
            ml_modell_version="v-test",
            features_json="{}", modus="shadow",
        ))
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=zeit, zone_id="waldblumenhain",
            boden_feuchte=25.0, quelle=DatenQuelle.GARDENA,
        )))
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=zeit + timedelta(hours=6), zone_id="waldblumenhain",
            boden_feuchte=30.0, quelle=DatenQuelle.GARDENA,
        )))

        # Zone B (bambuswald): ML knapp besser (gelb) — ml_fehler = 4,
        #   heuristik_fehler = 6: 4 > 0.5*6=3 aber <= 0.8*6=4.8 -> gelb.
        _run(speicher.speichere_dauer_vorschlag(
            zeitstempel=zeit, zone_id="bambuswald",
            f_vor=50.0, ziel_schwelle=65.0,
            heuristik_s=660, ml_s=540,
            ml_modell_version="v-test",
            features_json="{}", modus="shadow",
        ))
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=zeit, zone_id="bambuswald",
            boden_feuchte=50.0, quelle=DatenQuelle.GARDENA,
        )))
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=zeit + timedelta(hours=6), zone_id="bambuswald",
            boden_feuchte=55.0, quelle=DatenQuelle.GARDENA,
        )))

        jetzt = zeit + timedelta(hours=6, minutes=30)
        _run(speicher.evaluiere_offene_dauer_vorschlaege(jetzt))

        konfiguriere_api(
            speicher, _konfig(), MagicMock(), MagicMock(),
        )
        client = TestClient(app)
        try:
            # T-0225: `jetzt` explizit setzen, sonst rechnet der Endpoint
            # gegen `datetime.now()` und der feste Test-Datensatz (22.04.)
            # faellt mit fortschreitender Zeit aus dem 30-Tage-Fenster.
            antwort = client.get(
                f"/api/ml/dauer-drift?fenster=30d&jetzt={jetzt.isoformat()}"
            )
            assert antwort.status_code == 200
            daten = antwort.json()
            assert daten["fenster_tage"] == 30
            zonen = daten["zonen"]
            assert "waldblumenhain" in zonen
            assert "bambuswald" in zonen

            wald = zonen["waldblumenhain"]
            assert wald["n_bewertet"] == 1
            assert wald["n_ml_bewertet"] == 1
            assert abs(wald["mae_heuristik"] - 21.0) < 0.01
            assert abs(wald["mae_ml"] - 5.0) < 0.01
            assert wald["ampel"] == "gruen"

            bambus = zonen["bambuswald"]
            assert bambus["ampel"] == "gelb"

            # Zone-Filter liefert nur die gefragte Zone (T-0225: `jetzt`
            # ebenfalls setzen, sonst leeres Fenster).
            antwort = client.get(
                f"/api/ml/dauer-drift?zone_id=waldblumenhain"
                f"&jetzt={jetzt.isoformat()}"
            )
            zonen = antwort.json()["zonen"]
            assert "bambuswald" not in zonen
            assert "waldblumenhain" in zonen
        finally:
            client.close()
    finally:
        _run(speicher.schliessen())


def test_ml_vorhersage_reicht_top_feature_skala_durch(tmp_path):
    speicher = Speicher(str(tmp_path / "vorhersage_api.db"))
    _run(speicher.verbinden())

    ml_service = MagicMock()
    ml_service.ist_verfuegbar = True
    ml_service.live_vorhersage = AsyncMock(return_value={
        "6h": MLVorhersage(
            zone_id="bambuswald",
            zeitstempel=datetime(2026, 4, 20, 10, 0),
            horizont_stunden=6,
            feuchte_aktuell=80.0,
            feuchte_prognose=75.0,
            top_features=[
                FeatureBeitrag(
                    name="boden_feuchte_aktuell",
                    wert=80.0,
                    beitrag=-5.0,
                    skala="delta",
                ),
            ],
        ),
    })

    verarbeiter = MagicMock()
    verarbeiter.hole_letzten_wert.return_value = SensorMessung(
        zeitstempel=datetime(2026, 4, 20, 10, 0),
        zone_id="bambuswald",
        boden_feuchte=80.0,
        quelle=DatenQuelle.GARDENA,
    )
    konfiguriere_api(
        speicher, _konfig(), MagicMock(), verarbeiter, ml_service=ml_service,
    )
    client = TestClient(app)
    try:
        antwort = client.get(
            "/api/ml/vorhersage/bambuswald?details=top_features",
        )
        assert antwort.status_code == 200
        daten = antwort.json()
        assert daten["6h"]["top_features"][0]["skala"] == "delta"
    finally:
        client.close()
        _run(speicher.schliessen())
