"""Tests fuer ML Training und Vorhersage-Service.

Testet TrainingsPipeline + MLVorhersageService mit synthetischen Daten.
"""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bewaesserung.modelle import (
    BalkonKonfig,
    DatenQuelle,
    GardenaKonfig,
    GesamtKonfig,
    SensorMessung,
    SpeicherKonfig,
    StandortKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.speicher import Speicher

# ML-Deps optional
pytest.importorskip("pandas")
pytest.importorskip("lightgbm")

import pandas as pd

from bewaesserung.ml.features import FeatureExtraktor
from bewaesserung.ml.training import TrainingsPipeline
from bewaesserung.ml.vorhersage import MLVorhersageService


@pytest.fixture
def konfig():
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="test"),
        zonen=[
            ZonenKonfig(
                zone_id="test_zone",
                name="Testzone",
                modus=ZonenModus.MONITORING,
                feuchte_schwelle_min=30,
                feuchte_schwelle_max=60,
                ist_topf=True,
                ist_indoor=False,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[
                WetterStandortKonfig(id="test_ort", breite=52.5, laenge=13.4),
            ]
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="test_standort",
                name="Test",
                wetter_standort="test_ort",
                zonen=["test_zone"],
            ),
        ],
        balkon_ausrichtung={
            "test_standort": BalkonKonfig(
                himmelsrichtung="sued",
                regen_wind=["sued"],
                sonnig=True,
            ),
        },
    )


@pytest.fixture
async def speicher():
    s = Speicher(":memory:")
    await s.verbinden()
    yield s
    await s.schliessen()


async def _fuege_lange_messreihe_ein(speicher, zone_id, tage=14):
    """Erzeugt stuendliche Messungen ueber mehrere Tage.

    Feuchte oszilliert zwischen 25 und 55% (simuliert Giessen + Trocknen).
    """
    import math
    start = datetime(2026, 1, 1, 0, 0)
    for h in range(tage * 24):
        t = start + timedelta(hours=h)
        # Sinus-Oszillation: ~40% ± 15%, Periode 48h
        feuchte = 40 + 15 * math.sin(2 * math.pi * h / 48)
        # Etwas Rauschen
        feuchte += (h % 7 - 3) * 0.5
        feuchte = max(5, min(95, feuchte))
        await speicher.speichere_messung(SensorMessung(
            zeitstempel=t,
            zone_id=zone_id,
            boden_feuchte=feuchte,
            boden_temperatur=18.0 + 3 * math.sin(2 * math.pi * h / 24),
            quelle=DatenQuelle.FYTA,
        ))


async def test_training_pipeline_erzeugt_modell(speicher, konfig):
    """TrainingsPipeline trainiert erfolgreich ein Modell."""
    await _fuege_lange_messreihe_ein(speicher, "test_zone", tage=14)

    extraktor = FeatureExtraktor(speicher, konfig)
    start = datetime(2026, 1, 1, 0, 0)
    df = await extraktor.erstelle_trainingsdaten(
        start + timedelta(days=1),
        start + timedelta(days=13),
    )

    assert len(df) > 100, f"Zu wenig Daten: {len(df)}"

    with tempfile.TemporaryDirectory() as tmp:
        pipeline = TrainingsPipeline(
            modell_verzeichnis=tmp,
            n_folds=2,
        )
        ergebnisse = pipeline.trainiere(df, horizonte=[6])

        assert 6 in ergebnisse
        ergebnis = ergebnisse[6]
        assert ergebnis.metriken.mae > 0
        assert ergebnis.metriken.mae < 30  # Nicht komplett daneben
        assert ergebnis.metriken.anzahl_samples > 100
        assert ergebnis.metriken.anzahl_features > 10

        # Modell-Datei existiert
        modell_dateien = list(Path(tmp).glob("modell_6h_*.lgbm"))
        assert len(modell_dateien) == 1

        # Symlink existiert
        assert (Path(tmp) / "aktuell_6h.lgbm").exists()

        # Feature Importances vorhanden
        assert len(ergebnis.metriken.feature_importances) > 0


async def test_vorhersage_service_laedt_modell(speicher, konfig):
    """MLVorhersageService laedt und nutzt trainiertes Modell."""
    await _fuege_lange_messreihe_ein(speicher, "test_zone", tage=14)

    extraktor = FeatureExtraktor(speicher, konfig)
    start = datetime(2026, 1, 1, 0, 0)
    df = await extraktor.erstelle_trainingsdaten(
        start + timedelta(days=1),
        start + timedelta(days=13),
    )

    with tempfile.TemporaryDirectory() as tmp:
        # Trainieren
        pipeline = TrainingsPipeline(
            modell_verzeichnis=tmp,
            n_folds=2,
        )
        pipeline.trainiere(df, horizonte=[6])

        # Laden
        service = MLVorhersageService(modell_verzeichnis=tmp)
        assert service.lade_modelle()
        assert service.ist_verfuegbar

        # Status pruefen
        status = service.status()
        assert status.ist_geladen
        assert 6 in status.horizonte

        # Vorhersage
        test_zeile = df.iloc[[0]]
        vorhersagen = service.vorhersage(test_zeile, horizont=6)
        assert vorhersagen is not None
        assert len(vorhersagen) == 1
        v = vorhersagen[0]
        assert 0 <= v.feuchte_prognose <= 100
        assert v.horizont_stunden == 6


async def test_vorhersage_ohne_modell_gibt_none(speicher):
    """MLVorhersageService gibt None zurueck wenn kein Modell vorhanden."""
    with tempfile.TemporaryDirectory() as tmp:
        service = MLVorhersageService(modell_verzeichnis=tmp)
        assert not service.lade_modelle()
        assert not service.ist_verfuegbar


async def test_training_mit_wenig_daten_warnt(speicher, konfig):
    """Training mit zu wenig Daten liefert leeres Ergebnis."""
    # Nur 10 Messungen
    start = datetime(2026, 1, 1, 0, 0)
    for h in range(10):
        await speicher.speichere_messung(SensorMessung(
            zeitstempel=start + timedelta(hours=h),
            zone_id="test_zone",
            boden_feuchte=50.0 - h,
            boden_temperatur=18.0,
        ))

    extraktor = FeatureExtraktor(speicher, konfig)
    df = await extraktor.erstelle_trainingsdaten(start, start + timedelta(hours=10))

    with tempfile.TemporaryDirectory() as tmp:
        pipeline = TrainingsPipeline(modell_verzeichnis=tmp, n_folds=2)
        ergebnisse = pipeline.trainiere(df, horizonte=[6])
        # Sollte entweder leer sein oder warnen
        # (weniger als 100 Zeilen mit gueltigem Ziel)
        assert 6 not in ergebnisse or ergebnisse[6].metriken.anzahl_samples < 50


def _synthetischer_monotone_df(n: int = 320) -> pd.DataFrame:
    start = datetime(2026, 1, 1, 0, 0)
    zeilen = []
    for i in range(n):
        zeit = start + timedelta(hours=i)
        aktuell = 50.0 + ((i % 24) - 12) * 0.25
        regen_24h = 12.0 if i >= n - 100 else float(i % 4)
        regen_6h = regen_24h / 4.0
        ziel_feuchte = max(0.0, min(100.0, aktuell - 1.5 + 0.25 * regen_24h))
        zeilen.append({
            "zone_id": "test_zone",
            "zeitstempel": zeit,
            "boden_feuchte_aktuell": aktuell,
            "feuchte_lag_1h": aktuell + 0.2,
            "feuchte_lag_3h": aktuell + 0.5,
            "feuchte_trend_6h": -0.1 + 0.02 * (i % 3),
            "niederschlag_summe_6h": regen_6h,
            "niederschlag_summe_24h": regen_24h,
            "et0_summe_6h": 0.4,
            "et0_summe_24h": 1.6,
            "bilanz_delta_24h": regen_24h - 1.6,
            "quelle": "fyta",
            "ziel_feuchte_6h": ziel_feuchte,
            "ziel_delta_6h": ziel_feuchte - aktuell,
        })
    return pd.DataFrame(zeilen)


def test_monotone_constraint_liste_markiert_nur_regen_summen():
    feature_cols = [
        "boden_feuchte_aktuell",
        "niederschlag_summe_6h",
        "niederschlag_summe_24h",
        "et0_summe_24h",
        "bilanz_delta_24h",
        "quelle",
    ]

    constraints, features = TrainingsPipeline._monotone_constraint_liste(feature_cols)

    assert constraints == [0, 1, 1, 0, 0, 0]
    assert features == ["niederschlag_summe_6h", "niederschlag_summe_24h"]


def test_feature_spalten_nutzen_neues_kategorieschema_und_ignorieren_legacy_quelle(tmp_path):
    df = pd.DataFrame([{
        "zone_id": "test_zone",
        "zeitstempel": datetime(2026, 1, 1, 0, 0).isoformat(),
        "boden_feuchte_aktuell": 40.0,
        "zone_kategorie": "test_zone",
        "sensor_quelle": "fyta",
        "quelle": "test_zone",
        "ziel_feuchte_6h": 41.0,
        "ziel_delta_6h": 1.0,
    }])
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path))

    feature_cols = pipeline._feature_spalten(df)

    assert "zone_kategorie" in feature_cols
    assert "sensor_quelle" in feature_cols
    assert "quelle" not in feature_cols


def test_training_ohne_cv_folds_speichert_kein_modell(tmp_path):
    start = datetime(2026, 1, 1, 0, 0)
    zeilen = []
    for i in range(150):
        aktuell = 40.0 + (i % 5)
        ziel = aktuell + 1.0
        zeilen.append({
            "zone_id": "test_zone",
            "zeitstempel": (start + timedelta(hours=i)).isoformat(),
            "boden_feuchte_aktuell": aktuell,
            "feuchte_trend_6h": 0.1,
            "zone_kategorie": "test_zone",
            "sensor_quelle": "fyta",
            "ziel_feuchte_6h": ziel,
            "ziel_delta_6h": ziel - aktuell,
        })
    df = pd.DataFrame(zeilen)
    pipeline = TrainingsPipeline(modell_verzeichnis=str(tmp_path), n_folds=3)

    ergebnisse = pipeline.trainiere(df, horizonte=[6])

    assert ergebnisse == {}
    assert not list(tmp_path.glob("modell_6h_*.lgbm"))
    assert not (tmp_path / "aktuell_6h.lgbm").exists()


def test_trainings_pipeline_filtert_auf_cluster_zonen(tmp_path):
    """T-0082: Wenn TrainingsPipeline mit cluster_zonen instanziiert
    wird, werden nur die angegebenen Zonen ins Training genommen.
    Kontrollziel: `mae_pro_zone` enthaelt nach Training nur die
    Cluster-Zonen, nicht die ausgeschlossene.
    """
    import math
    from bewaesserung.ml.training import TrainingsPipeline

    # 3 Zonen, gleiche Kurve, aber wir trainieren nur auf 2.
    rows = []
    start = datetime(2026, 1, 1, 0, 0)
    for zone_id in ("a", "b", "ausgeschlossen"):
        for h in range(24 * 30):
            t = start + timedelta(hours=h)
            feuchte = 40 + 12 * math.sin(2 * math.pi * h / 48)
            rows.append({
                "zone_id": zone_id,
                "zeitstempel": t,
                "boden_feuchte_aktuell": feuchte,
                "ziel_feuchte_6h": feuchte,
                "ziel_feuchte_12h": feuchte,
                "ziel_feuchte_24h": feuchte,
                "ziel_delta_6h": 0.0,
                "ziel_delta_12h": 0.0,
                "ziel_delta_24h": 0.0,
                "boden_temperatur": 18.0,
                "stunde_sin": math.sin(h * math.pi / 12),
                "stunde_cos": math.cos(h * math.pi / 12),
                "tag_im_jahr_sin": 0.0,
                "tag_im_jahr_cos": 1.0,
                "zone_kategorie": zone_id,
                "sensor_quelle": "fyta",
            })
    df = pd.DataFrame(rows)

    cluster_dir = tmp_path / "cluster_test"
    pipeline = TrainingsPipeline(
        modell_verzeichnis=str(cluster_dir),
        n_folds=2,
        cluster_id="ab_cluster",
        cluster_zonen=["a", "b"],
    )
    # Modell-Verzeichnis bekommt feuchte/<cluster>/-Suffix automatisch.
    assert pipeline._modell_dir == cluster_dir / "feuchte" / "ab_cluster"

    gefiltert = pipeline._filtere_cluster_zonen(df)
    assert set(gefiltert["zone_id"].unique()) == {"a", "b"}
    assert "ausgeschlossen" not in set(gefiltert["zone_id"])
    assert len(gefiltert) == 2 * 24 * 30  # 2 Zonen × 30 Tage × 24h


def test_vorhersage_service_resolved_cluster_per_zone(tmp_path):
    """T-0082: live_vorhersage(zone_id=...) nutzt das Cluster-Modell,
    wenn der Cluster geladen ist. Test legt zwei Cluster-Caches an mit
    unterschiedlichen Booster-Mocks und prueft ueber `vorhersage()` mit
    explizitem cluster_id, dass das richtige Modell genutzt wird.
    """
    from unittest.mock import MagicMock
    from bewaesserung.ml.vorhersage import _ClusterCache, MLVorhersageService

    service = MLVorhersageService(modell_verzeichnis=str(tmp_path))
    # Zwei Cluster mit jeweils einem Mock-Modell (konstanter Output).
    fake_a = MagicMock()
    fake_a.predict.return_value = [42.0]
    fake_b = MagicMock()
    fake_b.predict.return_value = [99.0]
    service._cluster_caches["cluster_a"] = _ClusterCache(
        modelle={6: fake_a},
        feature_cols={6: ["boden_feuchte_aktuell"]},
    )
    service._cluster_caches["cluster_b"] = _ClusterCache(
        modelle={6: fake_b},
        feature_cols={6: ["boden_feuchte_aktuell"]},
    )
    service.setze_cluster_zuordnung({
        "zone_a": "cluster_a", "zone_b": "cluster_b",
    })

    # Resolver liefert die richtige cluster_id pro Zone.
    assert service._cluster_fuer_zone("zone_a") == "cluster_a"
    assert service._cluster_fuer_zone("zone_b") == "cluster_b"
    assert service._cluster_fuer_zone("zone_ohne_cluster") is None

    # vorhersage() mit cluster_id nutzt das jeweilige Modell.
    df = pd.DataFrame([{"zone_id": "zone_a", "boden_feuchte_aktuell": 50.0}])
    erg_a = service.vorhersage(df, horizont=6, cluster_id="cluster_a")
    erg_b = service.vorhersage(df, horizont=6, cluster_id="cluster_b")
    assert erg_a is not None and erg_a[0].feuchte_prognose == 42.0
    assert erg_b is not None and erg_b[0].feuchte_prognose == 99.0
    # Ohne cluster_id (oder unbekannt) → Legacy-Fallback (kein Modell geladen).
    erg_legacy = service.vorhersage(df, horizont=6)
    assert erg_legacy is None


def test_retrain_job_pro_cluster_skippt_unzureichend_daten(tmp_path):
    """T-0082: bei cluster_strategie=pro_zone wird ein Cluster mit
    n_zeilen < mindest_zeilen_pro_cluster geskippt mit Status
    `unzureichend_daten` — keine Modell-Datei wird geschrieben.
    """
    import asyncio
    from unittest.mock import MagicMock
    from bewaesserung.ml.retrain_job import MlRetrainJob
    from bewaesserung.modelle import MlRetrainKonfig

    # 5 Zeilen — weit unter Schwelle 1500.
    df_klein = pd.DataFrame([
        {"zone_id": "rasen", "zeitstempel": datetime(2026, 1, 1, h),
         "ziel_feuchte_6h": 50.0}
        for h in range(5)
    ])

    konfig = MagicMock()
    z = MagicMock()
    z.zone_id = "rasen"
    z.cluster_id = None  # default = zone_id
    konfig.zonen = [z]
    speicher = MagicMock()

    retrain = MlRetrainKonfig(
        aktiv=True,
        cluster_strategie="pro_zone",
        mindest_zeilen_pro_cluster=1500,
    )
    job = MlRetrainJob(speicher, konfig, retrain, ausgabe_pfad=str(tmp_path))

    ergebnis = asyncio.run(
        job._fuehre_aus_pro_cluster(datetime(2026, 4, 27, 6, 40), df_klein)
    )
    assert ergebnis["status"] == "pro_cluster"
    assert ergebnis["cluster"]["rasen"]["status"] == "unzureichend_daten"
    assert ergebnis["cluster"]["rasen"]["n_zeilen"] == 5
    # Kein Modell-File geschrieben.
    assert not (tmp_path / "feuchte" / "rasen" / "aktuell_6h.lgbm").exists()


def test_monotone_training_schreibt_metriken_und_regen_slice(tmp_path):
    df = _synthetischer_monotone_df()
    pipeline = TrainingsPipeline(
        modell_verzeichnis=str(tmp_path),
        n_folds=2,
        max_rounds=20,
        monotone_constraints=True,
    )

    ergebnisse = pipeline.trainiere(df, horizonte=[6])

    metriken = ergebnisse[6].metriken
    assert metriken.monotone_constraints is True
    assert metriken.monotone_features == [
        "niederschlag_summe_6h",
        "niederschlag_summe_24h",
    ]
    assert metriken.regen_slice_mae is not None
    assert metriken.regen_slice_n > 0
