"""Tests fuer T-0162: Speicher.verbinden(modus='ro').

Vorgeschichte: Manual-Retrain via `bewaesserung.ml.cli trainiere` liest
60 Tage Sensor-Daten (lange Lese-Bursts > 5 s). Bei laufendem Backend
fuehrt das zu `sqlite3.OperationalError: database is locked`. WAL +
busy_timeout=5000 reichen nicht.

T-0162 fuehrt einen Read-Only-Modus ein, der `executescript(SCHEMA_SQL)`
und `_migriere()` skippt und SQLite-URI `?mode=ro` nutzt — damit ist die
CLI-Connection garantiert non-writing.
"""
from __future__ import annotations

import sqlite3

import pytest

from bewaesserung.speicher import Speicher


@pytest.mark.asyncio
async def test_speicher_ro_modus_blockt_schreiben(tmp_path):
    """Eine Read-Only-Connection darf nicht schreiben koennen."""
    db_pfad = tmp_path / "test.db"

    # Erst RW-Connection: Schema anlegen + ein paar Daten.
    s_rw = Speicher(str(db_pfad))
    await s_rw.verbinden()  # default rw
    await s_rw.schliessen()

    # Jetzt Read-Only.
    s_ro = Speicher(str(db_pfad))
    await s_ro.verbinden(modus="ro")
    try:
        # Versuche INSERT — muss fehlschlagen (readonly database).
        assert s_ro._db is not None
        with pytest.raises((sqlite3.OperationalError, Exception)) as exc_info:
            await s_ro._db.execute(
                "INSERT INTO sensor_messung "
                "(zeitstempel, zone_id, geraet_id, boden_feuchte, "
                " boden_temperatur, umgebungs_temperatur, licht_intensitaet, "
                " batterie_prozent, quelle) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-01-01T00:00:00", "zone-test", "ger-1",
                 50.0, 20.0, 22.0, 1000.0, 80, "gardena"),
            )
            await s_ro._db.commit()
        assert "readonly" in str(exc_info.value).lower() or \
               "read-only" in str(exc_info.value).lower() or \
               "attempt to write" in str(exc_info.value).lower()
    finally:
        await s_ro.schliessen()


@pytest.mark.asyncio
async def test_speicher_ro_modus_liest_bestehende_daten(tmp_path):
    """Read-Only-Connection muss vorhandene Tabellen lesen koennen."""
    db_pfad = tmp_path / "test.db"

    # Vorab Schema + 1 Zeile via RW-Connection.
    from datetime import datetime
    from bewaesserung.modelle import SensorMessung, DatenQuelle

    s_rw = Speicher(str(db_pfad))
    await s_rw.verbinden()
    await s_rw.speichere_messung(SensorMessung(
        zeitstempel=datetime(2026, 1, 1, 12, 0, 0),
        zone_id="zone-test",
        geraet_id="ger-1",
        boden_feuchte=42.0,
        boden_temperatur=20.0,
        umgebungs_temperatur=22.0,
        licht_intensitaet=1000.0,
        batterie_prozent=80,
        quelle=DatenQuelle.GARDENA,
    ))
    await s_rw.schliessen()

    # Read-Only-Connection muss die Zeile lesen.
    s_ro = Speicher(str(db_pfad))
    await s_ro.verbinden(modus="ro")
    try:
        messungen = await s_ro.hole_messungen("zone-test")
        assert len(messungen) == 1
        assert messungen[0].boden_feuchte == 42.0
    finally:
        await s_ro.schliessen()


@pytest.mark.asyncio
async def test_speicher_ro_modus_ohne_schema_setup(tmp_path, monkeypatch):
    """Bei modus='ro' darf _migriere() NICHT aufgerufen werden — das
    schreibt ALTER TABLE und wuerde den Lock-Vorteil zerstoeren."""
    db_pfad = tmp_path / "test.db"

    # Schema vorab anlegen (sonst gibt's ohne SCHEMA_SQL kein Schema).
    s_rw = Speicher(str(db_pfad))
    await s_rw.verbinden()
    await s_rw.schliessen()

    s_ro = Speicher(str(db_pfad))
    aufrufe = {"migriere": 0}

    original = s_ro._migriere

    async def gezaehlt():
        aufrufe["migriere"] += 1
        await original()

    monkeypatch.setattr(s_ro, "_migriere", gezaehlt)
    await s_ro.verbinden(modus="ro")
    try:
        assert aufrufe["migriere"] == 0, (
            "T-0162: _migriere() muss bei modus='ro' uebersprungen werden."
        )
    finally:
        await s_ro.schliessen()


@pytest.mark.asyncio
async def test_speicher_verbinden_ungueltiger_modus_wirft(tmp_path):
    """Defensive Validation: nur 'rw'/'ro' erlaubt."""
    s = Speicher(str(tmp_path / "test.db"))
    with pytest.raises(ValueError):
        await s.verbinden(modus="readwrite")
