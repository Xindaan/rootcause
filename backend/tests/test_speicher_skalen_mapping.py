"""Tests fuer T-0181: Sensor-Skalen-Mapping (Stub).

Heute KEIN Fit-Code, aber Schema + Loader + Aggregat-Hook stehen.
Tests verifizieren:
  1. Default-Verhalten (leere Tabelle) ist Identity — Aggregat unveraendert.
  2. Mit Mapping wird `boden_feuchte` linear transformiert vor Median.
  3. Mapping pro Quelle isoliert — Gardena-Mapping greift nicht fuer FYTA.

Trigger fuer das eigentliche Fitten: 4-6 Wochen nach T-0179-Inbetriebnahme
(parallele FYTA Terra + Gardena Messungen im Waldblumenhain).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from bewaesserung.modelle import DatenQuelle, SensorMessung
from bewaesserung.speicher import Speicher


async def _baue_speicher_mit_mehrsensoren(tmp_path) -> Speicher:
    """Fuellt zwei Sensoren (Gardena + FYTA) mit unterschiedlichen Werten
    in derselben Zone."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    jetzt = datetime(2026, 5, 13, 12, 0, 0)
    await s.speichere_messung(SensorMessung(
        zeitstempel=jetzt,
        zone_id="waldblumenhain",
        geraet_id="gardena-1",
        boden_feuchte=40.0,  # Gardena-Skala
        boden_temperatur=20.0,
        umgebungs_temperatur=22.0,
        licht_intensitaet=1000.0,
        batterie_prozent=80,
        quelle=DatenQuelle.GARDENA,
    ))
    await s.speichere_messung(SensorMessung(
        zeitstempel=jetzt,
        zone_id="waldblumenhain",
        geraet_id="fyta-1",
        boden_feuchte=60.0,  # FYTA-Skala (anderer Bezugswert)
        boden_temperatur=20.0,
        umgebungs_temperatur=22.0,
        licht_intensitaet=1000.0,
        batterie_prozent=80,
        quelle=DatenQuelle.FYTA,
    ))
    return s


@pytest.mark.asyncio
async def test_aggregat_identitaet_ohne_mapping(tmp_path):
    """T-0181: ohne Mapping-Eintraege ist `boden_feuchte` der Median
    der Roh-Werte (40, 60) = 50. Backward-Compat zur T-0179c-Logik."""
    s = await _baue_speicher_mit_mehrsensoren(tmp_path)
    fixierter_jetzt = datetime(2026, 5, 13, 13, 0, 0)
    try:
        agg = await s.letzte_messung_aggregiert(
            "waldblumenhain", jetzt=fixierter_jetzt,
        )
        assert agg is not None
        assert agg.boden_feuchte == 50.0
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_aggregat_mit_mapping_transformiert_vor_median(tmp_path):
    """T-0181: Wenn FYTA-Mapping `a=0.5, b=10` gesetzt ist, wird der
    FYTA-Roh-Wert 60 -> 0.5*60 + 10 = 40. Median(40, 40) = 40."""
    s = await _baue_speicher_mit_mehrsensoren(tmp_path)
    assert s._db is not None
    fixierter_jetzt = datetime(2026, 5, 13, 13, 0, 0)
    try:
        # Mapping fuer FYTA setzen (Gardena bleibt ohne Eintrag = Identity).
        await s._db.execute(
            "INSERT INTO sensor_skalen_mapping "
            "(zone_id, quelle, a, b, n_obs, gefittet_am) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("waldblumenhain", "fyta", 0.5, 10.0, 100, "2026-05-13T00:00:00"),
        )
        await s._db.commit()

        agg = await s.letzte_messung_aggregiert(
            "waldblumenhain", jetzt=fixierter_jetzt,
        )
        assert agg is not None
        # Gardena 40 (identity) + FYTA 60*0.5+10=40 -> Median = 40
        assert agg.boden_feuchte == 40.0
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_hole_skalen_mapping_returned_none_wenn_leer(tmp_path):
    """T-0181: Hilfsmethode liefert `None` wenn Tabelle leer ist."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        m = await s.hole_skalen_mapping("waldblumenhain", "fyta")
        assert m is None
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_hole_skalen_mapping_liefert_koeffizienten(tmp_path):
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    assert s._db is not None
    try:
        await s._db.execute(
            "INSERT INTO sensor_skalen_mapping "
            "(zone_id, quelle, a, b, n_obs, gefittet_am) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("waldblumenhain", "fyta", 0.5, 10.0, 100, "2026-05-13T00:00:00"),
        )
        await s._db.commit()
        m = await s.hole_skalen_mapping("waldblumenhain", "fyta")
        assert m == {
            "a": 0.5, "b": 10.0, "n_obs": 100,
            "gefittet_am": "2026-05-13T00:00:00",
        }
    finally:
        await s.schliessen()
