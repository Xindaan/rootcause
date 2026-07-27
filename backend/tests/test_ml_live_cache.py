"""Tests fuer den live_vorhersage-Cache (2026-04-21).

Hintergrund: Ohne Cache baute jede live_vorhersage den kompletten
48-h-Feature-DataFrame fuer alle Zonen neu auf — bei 11 parallelen
Frontend-Requests 11× synchron im Event-Loop. Das liess den Backend
bei ~96 % CPU dauerlasten und andere API-Endpoints verhungern.

Mit Cache: der zweite Call fuer dieselbe (zone, details) innerhalb der
TTL liefert das gemerkte Ergebnis ohne Rebuild.
"""
from __future__ import annotations

import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("pandas")

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd

from bewaesserung.ml.modelle_ml import MLVorhersage
from bewaesserung.ml.vorhersage import MLVorhersageService


def _df_mit_einer_zone(zone_id: str = "rasen") -> pd.DataFrame:
    """Mini-DataFrame mit einer Zeile — ausreichend fuer vorhersage()-Mock."""
    return pd.DataFrame([{
        "zone_id": zone_id,
        "zeitstempel": datetime.now().isoformat(),
        "boden_feuchte_aktuell": 50.0,
    }])


@pytest.mark.asyncio
async def test_live_cache_verhindert_doppelten_feature_build():
    """Zwei aufeinanderfolgende Calls fuer dieselbe Zone bauen den
    Feature-DataFrame nur einmal. Der zweite Call ist ein Cache-Hit.
    """
    service = MLVorhersageService.__new__(MLVorhersageService)
    # Minimalzustand — lade_modelle() wird nicht gerufen, wir mocken
    service._modelle = {6: MagicMock()}
    service._metriken = {}
    service._feature_cols = {}
    service._quantile_modelle = {}
    service._quantile_feature_cols = {}
    service._delta_ziel = {}
    service._quantile_delta_ziel = {}
    service._band_scale = {}
    service._letzte_missing_quote = 0.0
    from pathlib import Path
    service._modell_dir = Path("/tmp")
    service._live_cache = {}
    service._live_cache_ttl_s = 45

    # Stub vorhersage(): liefert dummy MLVorhersage
    dummy = MLVorhersage(
        zone_id="rasen",
        zeitstempel=datetime.now(),
        horizont_stunden=6,
        feuchte_aktuell=50.0,
        feuchte_prognose=48.0,
    )
    service.vorhersage = MagicMock(return_value=[dummy])

    speicher = MagicMock()
    speicher.logge_ml_vorhersage = AsyncMock()
    konfig = MagicMock()

    # Feature-Extraktor patchen — erstelle_trainingsdaten soll zaehlbar sein.
    # Wir zaehlen wie oft der Feature-Extraktor gerufen wird.
    baue_aufrufe = {"n": 0}

    async def fake_erstelle(*args, **kwargs):
        baue_aufrufe["n"] += 1
        return _df_mit_einer_zone("rasen")

    with patch("bewaesserung.ml.features.FeatureExtraktor") as FakeExtraktor:
        FakeExtraktor.return_value.erstelle_trainingsdaten = fake_erstelle

        # Erster Call — baut Features neu
        ergebnis1 = await service.live_vorhersage("rasen", speicher, konfig)
        inserts_nach_erstem = speicher.logge_ml_vorhersage.await_count
        # Zweiter Call direkt danach — Cache-Hit, kein Rebuild
        ergebnis2 = await service.live_vorhersage("rasen", speicher, konfig)
        inserts_nach_zweitem = speicher.logge_ml_vorhersage.await_count

    assert baue_aufrufe["n"] == 1, (
        f"Feature-Extraktor soll nur 1× gerufen werden, wurde {baue_aufrufe['n']}× gerufen"
    )
    assert ergebnis1 == ergebnis2
    # Drift-Log-Inserts nur beim ersten Call; Cache-Hit triggert keine neuen.
    assert inserts_nach_zweitem == inserts_nach_erstem, (
        f"Cache-Hit soll keine neuen Drift-Inserts ausloesen. "
        f"Vorher {inserts_nach_erstem}, nachher {inserts_nach_zweitem}."
    )


@pytest.mark.asyncio
async def test_live_cache_details_key_getrennt():
    """Cache-Eintraege fuer details=True und details=False sind getrennt
    (Top-Features unterschiedlich). T-0314: der zugrundeliegende 48h-DF
    wird dabei via gemeinsamem DF-Cache geteilt -> nur EIN Build, aber
    zwei getrennte Ergebnis-Cache-Eintraege.
    """
    service = MLVorhersageService.__new__(MLVorhersageService)
    service._modelle = {6: MagicMock()}
    service._metriken = {}
    service._feature_cols = {}
    service._quantile_modelle = {}
    service._quantile_feature_cols = {}
    service._delta_ziel = {}
    service._quantile_delta_ziel = {}
    service._band_scale = {}
    service._letzte_missing_quote = 0.0
    from pathlib import Path
    service._modell_dir = Path("/tmp")
    service._live_cache = {}
    service._live_cache_ttl_s = 45

    dummy = MLVorhersage(
        zone_id="rasen",
        zeitstempel=datetime.now(),
        horizont_stunden=6,
        feuchte_aktuell=50.0,
        feuchte_prognose=48.0,
    )
    service.vorhersage = MagicMock(return_value=[dummy])

    speicher = MagicMock()
    speicher.logge_ml_vorhersage = AsyncMock()
    konfig = MagicMock()

    baue_aufrufe = {"n": 0}

    async def fake_erstelle(*args, **kwargs):
        baue_aufrufe["n"] += 1
        return _df_mit_einer_zone("rasen")

    with patch("bewaesserung.ml.features.FeatureExtraktor") as FakeExtraktor:
        FakeExtraktor.return_value.erstelle_trainingsdaten = fake_erstelle

        await service.live_vorhersage("rasen", speicher, konfig, details=False)
        await service.live_vorhersage("rasen", speicher, konfig, details=True)

    # T-0314: getrennte Ergebnis-Cache-Eintraege bleiben (Top-Features
    # unterschiedlich), aber der 48h-DF wird via gemeinsamem DF-Cache
    # geteilt -> nur EIN Build statt zwei.
    assert ("rasen", False) in service._live_cache
    assert ("rasen", True) in service._live_cache
    assert baue_aufrufe["n"] == 1, (
        f"Geteilter DF-Cache -> 1 Build erwartet, tatsaechlich {baue_aufrufe['n']}."
    )


@pytest.mark.asyncio
async def test_live_cache_abgelaufen_laedt_neu():
    """Nach Ablauf der TTL wird der Cache verworfen und neu aufgebaut."""
    service = MLVorhersageService.__new__(MLVorhersageService)
    service._modelle = {6: MagicMock()}
    service._metriken = {}
    service._feature_cols = {}
    service._quantile_modelle = {}
    service._quantile_feature_cols = {}
    service._delta_ziel = {}
    service._quantile_delta_ziel = {}
    service._band_scale = {}
    service._letzte_missing_quote = 0.0
    from pathlib import Path
    service._modell_dir = Path("/tmp")
    service._live_cache = {}
    # Sehr kurze TTL fuer Test -- BEIDE Caches (Ergebnis + T-0314 DF)
    # sofort ablaufen lassen, damit ein voller Rebuild getestet wird.
    service._live_cache_ttl_s = 0
    service._df_cache = None
    service._df_cache_ttl_s = 0

    dummy = MLVorhersage(
        zone_id="rasen",
        zeitstempel=datetime.now(),
        horizont_stunden=6,
        feuchte_aktuell=50.0,
        feuchte_prognose=48.0,
    )
    service.vorhersage = MagicMock(return_value=[dummy])

    speicher = MagicMock()
    speicher.logge_ml_vorhersage = AsyncMock()
    konfig = MagicMock()

    baue_aufrufe = {"n": 0}

    async def fake_erstelle(*args, **kwargs):
        baue_aufrufe["n"] += 1
        return _df_mit_einer_zone("rasen")

    with patch("bewaesserung.ml.features.FeatureExtraktor") as FakeExtraktor:
        FakeExtraktor.return_value.erstelle_trainingsdaten = fake_erstelle

        await service.live_vorhersage("rasen", speicher, konfig)
        # Cache-Ablauf simulieren, indem wir den Timestamp zurueckdatieren
        key = ("rasen", False)
        zeit, wert = service._live_cache[key]
        service._live_cache[key] = (zeit - timedelta(seconds=1), wert)
        await service.live_vorhersage("rasen", speicher, konfig)

    assert baue_aufrufe["n"] == 2


@pytest.mark.asyncio
async def test_df_cache_ueberlebt_ergebnis_cache_ablauf():
    """T-0314 Regression-Guard: Der DF-Cache hat eine eigene TTL und
    ueberlebt den Ablauf des Ergebnis-Caches. Folge: bei Ergebnis-Cache-
    Miss wird zwar neu inferiert, aber der teure 48h-DF NICHT neu gebaut.
    Frueher loeste jeder Ergebnis-Cache-Miss einen vollen DF-Rebuild aus
    (gemessen 5x/Entscheidungszyklus bzw. 15x/Snapshot details=true).
    """
    service = MLVorhersageService.__new__(MLVorhersageService)
    service._modelle = {6: MagicMock()}
    service._metriken = {}
    service._feature_cols = {}
    service._quantile_modelle = {}
    service._quantile_feature_cols = {}
    service._delta_ziel = {}
    service._quantile_delta_ziel = {}
    service._band_scale = {}
    service._letzte_missing_quote = 0.0
    from pathlib import Path
    service._modell_dir = Path("/tmp")
    service._live_cache = {}
    service._live_cache_ttl_s = 0   # Ergebnis-Cache immer abgelaufen
    service._df_cache = None
    service._df_cache_ttl_s = 45    # DF-Cache lebt weiter

    dummy = MLVorhersage(
        zone_id="rasen",
        zeitstempel=datetime.now(),
        horizont_stunden=6,
        feuchte_aktuell=50.0,
        feuchte_prognose=48.0,
    )
    service.vorhersage = MagicMock(return_value=[dummy])

    speicher = MagicMock()
    speicher.logge_ml_vorhersage = AsyncMock()
    konfig = MagicMock()

    baue_aufrufe = {"n": 0}

    async def fake_erstelle(*args, **kwargs):
        baue_aufrufe["n"] += 1
        return _df_mit_einer_zone("rasen")

    with patch("bewaesserung.ml.features.FeatureExtraktor") as FakeExtraktor:
        FakeExtraktor.return_value.erstelle_trainingsdaten = fake_erstelle

        await service.live_vorhersage("rasen", speicher, konfig)
        await service.live_vorhersage("rasen", speicher, konfig)

    # Ergebnis-Cache lief jeweils ab -> 2 Inferenzen, aber DF nur 1x gebaut.
    assert baue_aufrufe["n"] == 1, (
        f"DF-Cache sollte Ergebnis-Cache-Ablauf ueberleben -> 1 Build, "
        f"tatsaechlich {baue_aufrufe['n']}."
    )
    assert service.vorhersage.call_count >= 2
