"""T-0199: Live-Overlay `current` aus juengster sensor_messung im
`/api/zonen`-Endpoint.

Unit-Tests fuer die Helper-Funktion `_ueberlagere_current_aus_messung`.
"""
from __future__ import annotations

from types import SimpleNamespace

from bewaesserung.api_server import _ueberlagere_current_aus_messung


def _messung(**fields) -> SimpleNamespace:
    """Minimaler Stand-In fuer SensorMessung (nur die Felder die der
    Overlay konsumiert)."""
    defaults = dict(
        boden_feuchte=None,
        boden_temperatur=None,
        licht=None,
        boden_fruchtbarkeit=None,
    )
    defaults.update(fields)
    return SimpleNamespace(**defaults)


def _optima_komplett() -> dict[str, dict]:
    """Realistisches Optima-Dict wie es aus `plant_optimum_achse` kommt."""
    base = {
        "min_good": 20.0,
        "max_good": 70.0,
        "min_akzeptabel": 10.0,
        "max_akzeptabel": 80.0,
        "current": 50.0,  # vom Job persistiert, evtl. veraltet
        "einheit": "x",
        "aktualisiert": "2026-05-16T11:30:00",
        "quelle": "fyta",
    }
    return {
        "feuchte": dict(base, einheit="%/h"),
        "licht_ppfd": dict(base, current=85.0, einheit="μmol/h"),
        "licht_dli": dict(base, current=3.5, einheit="mol/day", quelle="aggregat"),
        "temperatur": dict(base, current=17.0, einheit="°C/h"),
        "salinitaet": dict(base, current=0.5, einheit="mS/cm/h"),
    }


def test_t0199_ueberlagert_die_3_direkten_achsen():
    """Sensor-Werte ersetzen current fuer feuchte/temperatur/ppfd.

    T-0385 (08.07.2026): salinitaet wird NICHT mehr aus boden_fruchtbarkeit
    ueberlagert -- `salinity` (EC mS/cm, salinitaet-Achse) und `soil_fertility`
    (boden_fruchtbarkeit, Range 0-6) sind verschiedene FYTA-Felder. salinitaet
    behaelt den Job-`current` (hier 0.5), auch wenn eine Messung
    boden_fruchtbarkeit traegt.
    """
    optima = _optima_komplett()
    messung = _messung(
        boden_feuchte=78.0,
        boden_temperatur=22.5,
        licht=120.0,
        boden_fruchtbarkeit=0.7,
    )
    out = _ueberlagere_current_aus_messung(optima, messung)
    assert out["feuchte"]["current"] == 78.0
    assert out["temperatur"]["current"] == 22.5
    assert out["licht_ppfd"]["current"] == 120.0
    # T-0385: salinitaet NICHT ueberlagert -> Job-current bleibt.
    assert out["salinitaet"]["current"] == 0.5


def test_t0199_licht_dli_bleibt_aus_job_aggregat():
    """DLI ist Tagesintegral aus 24h-PPFD — der Live-PPFD-Beat ist
    keine sinnvolle DLI-Naeherung, deshalb DLI nicht ueberschreiben.
    """
    optima = _optima_komplett()
    messung = _messung(licht=120.0)  # nur PPFD live
    out = _ueberlagere_current_aus_messung(optima, messung)
    # licht_ppfd wird ueberlagert, licht_dli bleibt beim Job-Wert
    assert out["licht_ppfd"]["current"] == 120.0
    assert out["licht_dli"]["current"] == 3.5  # unveraendert


def test_t0199_messung_none_laesst_alles_unangetastet():
    """Kein Live-Wert -> Cache-Werte bleiben."""
    optima = _optima_komplett()
    out = _ueberlagere_current_aus_messung(optima, None)
    assert out == optima


def test_t0199_leeres_optima_bleibt_leer():
    """Zone ohne FYTA-Profil (kein Job-Lauf) -> Overlay ist No-Op."""
    messung = _messung(boden_feuchte=50.0)
    assert _ueberlagere_current_aus_messung({}, messung) == {}


def test_t0199_live_wert_none_haelt_cache_wert():
    """Sensor-Wert None (z.B. Salinitaets-Sensor liefert nichts) ->
    Cache-Wert bleibt erhalten, kein Overwrite mit NULL.
    """
    optima = _optima_komplett()
    # boden_fruchtbarkeit nicht gesetzt -> None
    messung = _messung(boden_feuchte=78.0)
    out = _ueberlagere_current_aus_messung(optima, messung)
    assert out["feuchte"]["current"] == 78.0
    assert out["salinitaet"]["current"] == 0.5  # Cache-Wert bleibt


def test_t0199_partielles_optima_nur_was_drin_ist():
    """Achsen, die der Job nicht persistiert hat, kommen nicht aus dem
    Overlay dazu — nur vorhandene Achsen werden aktualisiert.
    """
    optima = {
        "feuchte": {
            "min_good": 20.0, "max_good": 70.0,
            "min_akzeptabel": None, "max_akzeptabel": None,
            "current": 50.0, "einheit": "%/h",
            "aktualisiert": "x", "quelle": "fyta",
        },
        # licht_ppfd fehlt im Cache -> Overlay darf es nicht erfinden
    }
    messung = _messung(boden_feuchte=78.0, licht=120.0)
    out = _ueberlagere_current_aus_messung(optima, messung)
    assert "licht_ppfd" not in out
    assert out["feuchte"]["current"] == 78.0


def test_t0199_overlay_ist_unabhaengig_vom_input(
):
    """Der zurueckgegebene Dict darf das Original-Optima nicht mutieren
    (FastAPI-Response-Layer haengt aus dem In-Memory-Cache von
    `hole_plant_optima_achsen` -> mutating waere kritisch).
    """
    optima = _optima_komplett()
    original_feuchte = dict(optima["feuchte"])
    messung = _messung(boden_feuchte=99.0)
    _ueberlagere_current_aus_messung(optima, messung)
    assert optima["feuchte"] == original_feuchte
