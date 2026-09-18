"""T-0196b: plant_optimum_achse-Tabelle (EAV) UPSERT + Read.

Pruefungen:
- UPSERT pro (zone_id, achse): zweites Schreiben ueberschreibt.
- Multi-Achse pro Zone: feuchte, licht_ppfd, licht_dli, temperatur,
  salinitaet bleiben getrennte Zeilen.
- hole_plant_optima_achsen liefert dict[zone, dict[achse, dict]] —
  optional gefiltert auf zone_id.
- Schwellen-None ist erlaubt (FYTA liefert manche Felder leer).
"""
from __future__ import annotations

import asyncio
import pytest

from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "opt_achse.db"))
    _run(s.verbinden())
    yield s
    _run(s.schliessen())


def test_speichere_und_lese_einzelne_achse(speicher):
    _run(speicher.speichere_plant_optimum_achse(
        zone_id="mandevilla", achse="feuchte", einheit="%/h",
        min_good=30.0, max_good=70.0,
        min_akzeptabel=20.0, max_akzeptabel=80.0,
    ))
    res = _run(speicher.hole_plant_optima_achsen())
    assert "mandevilla" in res
    assert "feuchte" in res["mandevilla"]
    f = res["mandevilla"]["feuchte"]
    assert f["min_good"] == 30.0
    assert f["max_good"] == 70.0
    assert f["min_akzeptabel"] == 20.0
    assert f["max_akzeptabel"] == 80.0
    assert f["einheit"] == "%/h"
    assert f["quelle"] == "fyta"
    assert f["aktualisiert"]  # Zeitstempel gesetzt


def test_upsert_ueberschreibt(speicher):
    """Zweiter Aufruf fuer (zone, achse) ueberschreibt Werte + Zeitstempel."""
    _run(speicher.speichere_plant_optimum_achse(
        zone_id="mandevilla", achse="feuchte", einheit="%/h",
        min_good=30.0, max_good=70.0,
    ))
    erstes_ts = _run(speicher.hole_plant_optima_achsen())["mandevilla"]["feuchte"]["aktualisiert"]

    _run(speicher.speichere_plant_optimum_achse(
        zone_id="mandevilla", achse="feuchte", einheit="%/h",
        min_good=35.0, max_good=75.0,
    ))
    nach_upsert = _run(speicher.hole_plant_optima_achsen())["mandevilla"]["feuchte"]
    assert nach_upsert["min_good"] == 35.0
    assert nach_upsert["max_good"] == 75.0
    assert nach_upsert["aktualisiert"] >= erstes_ts


def test_multi_achse_pro_zone(speicher):
    """Mehrere Achsen pro Zone bleiben getrennte Zeilen, alle lesbar."""
    achsen_daten = [
        ("feuchte", "%/h", 30.0, 70.0),
        ("licht_ppfd", "μmol/h", 11.5, 460.0),
        ("licht_dli", "mol/day", 4.0, 20.0),
        ("temperatur", "°C/h", 5.0, 25.0),
        ("salinitaet", "mS/cm/h", 0.2, 1.0),
    ]
    for achse, einheit, min_g, max_g in achsen_daten:
        _run(speicher.speichere_plant_optimum_achse(
            zone_id="mandevilla", achse=achse, einheit=einheit,
            min_good=min_g, max_good=max_g,
        ))
    res = _run(speicher.hole_plant_optima_achsen())
    zone = res["mandevilla"]
    assert set(zone.keys()) == {
        "feuchte", "licht_ppfd", "licht_dli", "temperatur", "salinitaet",
    }
    assert zone["licht_dli"]["einheit"] == "mol/day"
    assert zone["licht_ppfd"]["min_good"] == 11.5


def test_zone_filter(speicher):
    """hole_plant_optima_achsen(zone_id=...) liefert nur diese Zone."""
    for zone in ("mandevilla", "kroton"):
        _run(speicher.speichere_plant_optimum_achse(
            zone_id=zone, achse="feuchte", einheit="%/h",
            min_good=30.0, max_good=70.0,
        ))
    nur_mandevilla = _run(speicher.hole_plant_optima_achsen(zone_id="mandevilla"))
    assert set(nur_mandevilla.keys()) == {"mandevilla"}
    assert "kroton" not in nur_mandevilla


def test_none_schwellen_erlaubt(speicher):
    """FYTA liefert manchmal nur partielle Schwellen (z.B. nur max_good).
    Speicher muss None-Werte akzeptieren ohne Fehler."""
    _run(speicher.speichere_plant_optimum_achse(
        zone_id="testzone", achse="licht_dli", einheit="mol/day",
        min_good=None, max_good=20.0,
        min_akzeptabel=None, max_akzeptabel=None,
    ))
    res = _run(speicher.hole_plant_optima_achsen())["testzone"]["licht_dli"]
    assert res["min_good"] is None
    assert res["max_good"] == 20.0
    assert res["min_akzeptabel"] is None


def test_bestehende_plant_optimum_tabelle_ungestoert(speicher):
    """Bestehende plant_optimum-Tabelle (moisture-only) muss unabhaengig
    bleiben. Beide koexistieren, kein Cross-Effect."""
    _run(speicher.speichere_plant_optimum(
        zone_id="zoneX", feuchte_min=25.0, feuchte_max=75.0,
    ))
    _run(speicher.speichere_plant_optimum_achse(
        zone_id="zoneX", achse="feuchte", einheit="%/h",
        min_good=30.0, max_good=70.0,
    ))
    alt = _run(speicher.hole_plant_optima())
    neu = _run(speicher.hole_plant_optima_achsen())
    # Alte Tabelle hat die alten Werte
    assert alt["zoneX"]["feuchte_min"] == 25.0
    assert alt["zoneX"]["feuchte_max"] == 75.0
    # Neue Tabelle hat die neuen Werte
    assert neu["zoneX"]["feuchte"]["min_good"] == 30.0
    assert neu["zoneX"]["feuchte"]["max_good"] == 70.0


def test_current_wird_gespeichert_und_gelesen(speicher):
    """T-0196d/e: `current` ist der zuletzt beobachtete Wert pro Achse."""
    _run(speicher.speichere_plant_optimum_achse(
        zone_id="mandevilla", achse="salinitaet", einheit="mS/cm/h",
        min_good=0.2, max_good=1.0, current=0.16,
    ))
    res = _run(speicher.hole_plant_optima_achsen("mandevilla"))
    assert res["mandevilla"]["salinitaet"]["current"] == 0.16


def test_current_coalesce_bleibt_erhalten(speicher):
    """T-0196d/e: UPSERT ohne current (None) ueberschreibt den
    gespeicherten Wert NICHT (COALESCE-Semantik). Sonst wuerde ein
    PlantOptimumJob-Schwellen-Update den letzten Live-Wert loeschen,
    wenn FYTA momentan kein current liefert (z.B. Sensor offline)."""
    _run(speicher.speichere_plant_optimum_achse(
        zone_id="m", achse="salinitaet", einheit="mS/cm/h",
        min_good=0.2, max_good=1.0, current=0.16,
    ))
    # Erneutes UPSERT ohne current
    _run(speicher.speichere_plant_optimum_achse(
        zone_id="m", achse="salinitaet", einheit="mS/cm/h",
        min_good=0.2, max_good=1.0,  # kein current!
    ))
    res = _run(speicher.hole_plant_optima_achsen("m"))
    # current bleibt erhalten trotz Schwellen-Update
    assert res["m"]["salinitaet"]["current"] == 0.16


def test_current_kann_aktualisiert_werden(speicher):
    """T-0196d/e: Mit current=neuer_wert wird der alte Wert ueberschrieben."""
    _run(speicher.speichere_plant_optimum_achse(
        zone_id="m", achse="temperatur", einheit="°C/h",
        min_good=5.0, max_good=25.0, current=20.0,
    ))
    _run(speicher.speichere_plant_optimum_achse(
        zone_id="m", achse="temperatur", einheit="°C/h",
        min_good=5.0, max_good=25.0, current=22.5,
    ))
    res = _run(speicher.hole_plant_optima_achsen("m"))
    assert res["m"]["temperatur"]["current"] == 22.5
