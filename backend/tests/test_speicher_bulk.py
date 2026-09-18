"""T-0200: Tests fuer die Bulk-Speicher-Methoden, die der
Dashboard-Snapshot-Endpoint nutzt.

Kern-Eigenschaft: Bulk-Variante MUSS pro Zone exakt dieselbe Liste/
Messung liefern wie die Per-Zone-Variante. Anderenfalls divergieren
`/api/zonen` (nutzt Per-Zone) und `/api/dashboard-snapshot` (nutzt Bulk).
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import DatenQuelle, SensorMessung
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "bulk.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _m(
    sp, zone, geraet, ts, feuchte, temp=15.0,
    quelle=DatenQuelle.GARDENA, batterie=90.0,
):
    _run(sp.speichere_messung(SensorMessung(
        zeitstempel=ts, zone_id=zone, geraet_id=geraet,
        boden_feuchte=feuchte, boden_temperatur=temp,
        batterie_prozent=batterie, quelle=quelle,
    )))


# --- hole_messungen_bulk -------------------------------------------------


def test_hole_messungen_bulk_identisch_zu_per_zone(speicher):
    """Bulk-Variante liefert je Zone die gleiche Liste wie hole_messungen."""
    jetzt = datetime(2026, 5, 12, 12, 0)
    von = jetzt - timedelta(hours=48)
    _m(speicher, "a", "s1", jetzt - timedelta(hours=1), 50.0)
    _m(speicher, "a", "s1", jetzt - timedelta(hours=10), 60.0)
    _m(speicher, "b", "s2", jetzt - timedelta(hours=2), 40.0)
    _m(speicher, "c", "s3", jetzt - timedelta(hours=5), 30.0)

    bulk = _run(speicher.hole_messungen_bulk(["a", "b", "c"], von=von))

    for zid in ["a", "b", "c"]:
        per = _run(speicher.hole_messungen(zid, von=von))
        assert [m.zeitstempel for m in bulk[zid]] == [m.zeitstempel for m in per]
        assert [m.boden_feuchte for m in bulk[zid]] == [m.boden_feuchte for m in per]


def test_hole_messungen_bulk_leere_zone_liefert_leere_liste(speicher):
    """Zone ohne Messungen kommt mit leerer Liste zurueck, nicht fehlt."""
    jetzt = datetime(2026, 5, 12, 12, 0)
    _m(speicher, "a", "s1", jetzt - timedelta(hours=1), 50.0)
    bulk = _run(speicher.hole_messungen_bulk(
        ["a", "leere_zone"], von=jetzt - timedelta(hours=48),
    ))
    assert "leere_zone" in bulk
    assert bulk["leere_zone"] == []


def test_hole_messungen_bulk_leere_id_liste(speicher):
    assert _run(speicher.hole_messungen_bulk([])) == {}


# --- letzte_messung_aggregiert_bulk --------------------------------------


def test_letzte_messung_aggregiert_bulk_identisch_zu_per_zone_single(speicher):
    """Single-Sensor-Zone: Bulk == Per-Zone (gleicher Sensor-Wert)."""
    jetzt = datetime(2026, 5, 12, 12, 0)
    _m(speicher, "a", "s1", jetzt - timedelta(minutes=10), 55.0)
    _m(speicher, "b", "s2", jetzt - timedelta(minutes=20), 33.0)

    bulk = _run(speicher.letzte_messung_aggregiert_bulk(["a", "b"], jetzt=jetzt))
    per_a = _run(speicher.letzte_messung_aggregiert("a", jetzt=jetzt))
    per_b = _run(speicher.letzte_messung_aggregiert("b", jetzt=jetzt))

    assert bulk["a"].boden_feuchte == per_a.boden_feuchte == 55.0
    assert bulk["a"].geraet_id == per_a.geraet_id == "s1"
    assert bulk["b"].boden_feuchte == per_b.boden_feuchte == 33.0


def test_letzte_messung_aggregiert_bulk_multi_sensor_median(speicher):
    """Multi-Sensor-Zone: Bulk-Median == Per-Zone-Median."""
    jetzt = datetime(2026, 5, 12, 12, 0)
    _m(speicher, "wb", "gardena", jetzt - timedelta(minutes=10), 50.0)
    _m(speicher, "wb", "fyta_1", jetzt - timedelta(minutes=20), 30.0)
    _m(speicher, "wb", "fyta_2", jetzt - timedelta(minutes=5), 40.0)
    _m(speicher, "andere", "s_x", jetzt - timedelta(minutes=15), 70.0)

    bulk = _run(speicher.letzte_messung_aggregiert_bulk(
        ["wb", "andere"], jetzt=jetzt,
    ))
    per_wb = _run(speicher.letzte_messung_aggregiert("wb", jetzt=jetzt))

    assert bulk["wb"].boden_feuchte == per_wb.boden_feuchte == 40.0
    assert bulk["wb"].geraet_id == per_wb.geraet_id == "aggregat:3"
    assert bulk["andere"].boden_feuchte == 70.0
    assert bulk["andere"].geraet_id == "s_x"


def test_letzte_messung_aggregiert_bulk_kein_sensor_im_fenster_ist_none(speicher):
    """Zone ohne Messung im Fenster bekommt None — kein KeyError."""
    jetzt = datetime(2026, 5, 12, 12, 0)
    # Messung 200 min alt -> aussserhalb 90-min-Default-Fenster
    _m(speicher, "alt", "s1", jetzt - timedelta(minutes=200), 55.0)
    bulk = _run(speicher.letzte_messung_aggregiert_bulk(
        ["alt", "leer"], jetzt=jetzt,
    ))
    assert bulk["alt"] is None
    assert bulk["leer"] is None


# --- letzte_messungen_pro_geraet_bulk ------------------------------------


def test_letzte_messungen_pro_geraet_bulk_identisch_zu_per_zone(speicher):
    """Pro-Geraet-Bulk: je Zone identische Liste zu Per-Zone."""
    jetzt = datetime(2026, 5, 12, 12, 0)
    _m(speicher, "wb", "gardena", jetzt - timedelta(minutes=10), 50.0)
    _m(speicher, "wb", "fyta_1", jetzt - timedelta(minutes=20), 30.0)
    # Aelterer Eintrag derselben geraet_id darf nicht doppelt erscheinen
    _m(speicher, "wb", "fyta_1", jetzt - timedelta(minutes=60), 25.0)
    _m(speicher, "b", "single", jetzt - timedelta(minutes=15), 44.0)

    bulk = _run(speicher.letzte_messungen_pro_geraet_bulk(
        ["wb", "b"], jetzt=jetzt,
    ))
    per_wb = _run(speicher.letzte_messungen_pro_geraet("wb", jetzt=jetzt))
    per_b = _run(speicher.letzte_messungen_pro_geraet("b", jetzt=jetzt))

    assert {(m.geraet_id, m.boden_feuchte) for m in bulk["wb"]} == \
           {(m.geraet_id, m.boden_feuchte) for m in per_wb}
    # Die juengsten Werte pro Geraet, nicht der 60-min-Alteintrag
    assert len(bulk["wb"]) == 2
    assert {m.boden_feuchte for m in bulk["wb"]} == {50.0, 30.0}
    assert [m.geraet_id for m in bulk["b"]] == [m.geraet_id for m in per_b]


def test_letzte_messungen_pro_geraet_bulk_leere_zone(speicher):
    jetzt = datetime(2026, 5, 12, 12, 0)
    bulk = _run(speicher.letzte_messungen_pro_geraet_bulk(["x"], jetzt=jetzt))
    assert bulk["x"] == []
