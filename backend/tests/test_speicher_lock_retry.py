"""T-0210: Regression-Test fuer den BUSY/LOCKED-Retry in `Speicher`.

Hintergrund: Am 18.05.2026 liefen `_entscheidungsloop` und ML-Drift-Job
parallel gegen dieselbe aiosqlite-Connection. Lange Lese-Bursts (60-Tage-
Feature-Scan) hielten die Connection laenger als `busy_timeout=5000`
fest, parallele Schreiber crashten mit `sqlite3.OperationalError:
database is locked`. Der Loop fing das nur via `logger.exception` ab und
lief sofort im naechsten Zyklus in denselben Konflikt.

Der Fix: `_mit_lock_retry` retried Schreib-Operationen mit exponentialem
Backoff; `busy_timeout` von 5 s auf 15 s; im `_entscheidungsloop` ein
30-s-Cool-Off bei DB-Lock-Exception. Diese Tests halten den Wrapper-
Pfad fest.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import (
    Ausloser,
    BewaesserungsEntscheidung,
    EntscheidungsScope,
    VentilAktion,
    VentilEreignis,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "lock_retry.db"))
    _run(s.verbinden())
    # Backoff-Basis im Test auf 1 ms ziehen, damit die Suite schnell bleibt.
    s._LOCK_RETRY_BASIS_S = 0.001
    try:
        yield s
    finally:
        _run(s.schliessen())


def _entscheidung(zone_id: str = "testzone") -> BewaesserungsEntscheidung:
    return BewaesserungsEntscheidung(
        zeitstempel=datetime(2026, 5, 18, 13, 41),
        zone_id=zone_id,
        soll_bewaessern=False,
        begruendung="lock-retry-test",
        scope=EntscheidungsScope.ZONE,
    )


# --- Unit: _mit_lock_retry direkt -------------------------------------

def test_mit_lock_retry_retried_bei_lock_dann_erfolg(speicher):
    """Zwei Lock-Errors, dritter Versuch geht durch — kein Re-Raise."""
    versuche = {"n": 0}

    async def _op():
        if versuche["n"] < 2:
            versuche["n"] += 1
            raise sqlite3.OperationalError("database is locked")

    _run(speicher._mit_lock_retry(_op, label="unit_lock_retry"))
    assert versuche["n"] == 2


def test_mit_lock_retry_gibt_auf_nach_max_versuchen(speicher):
    """Persistente Locks: nach 4 Versuchen Re-Raise, keine Endlosschleife."""
    versuche = {"n": 0}

    async def _op():
        versuche["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        _run(speicher._mit_lock_retry(_op, label="unit_lock_persistent"))

    assert versuche["n"] == speicher._LOCK_RETRY_MAX_VERSUCHE


def test_mit_lock_retry_nicht_lock_fehler_sofort_hoch(speicher):
    """Nicht-Lock-Fehler (Syntax, Schema): kein Retry, sofort hoch."""
    versuche = {"n": 0}

    async def _op():
        versuche["n"] += 1
        raise sqlite3.OperationalError("no such column: foo")

    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        _run(speicher._mit_lock_retry(_op, label="unit_anderer_fehler"))

    assert versuche["n"] == 1


def test_mit_lock_retry_busy_keyword_wird_auch_retried(speicher):
    """SQLite meldet sowohl 'locked' als auch 'busy' — beide retrybar."""
    versuche = {"n": 0}

    async def _op():
        if versuche["n"] < 1:
            versuche["n"] += 1
            raise sqlite3.OperationalError("database is busy")

    _run(speicher._mit_lock_retry(_op, label="unit_busy"))
    assert versuche["n"] == 1


# --- Integration: echte Lock-Konkurrenz ------------------------------

def test_speichere_entscheidung_kommt_durch_bei_kurzem_writer_lock(
    tmp_path, monkeypatch
):
    """End-to-end: eine zweite Connection haelt kurz einen Write-Lock,
    `speichere_entscheidung` retried und persistiert sobald frei.

    Verifiziert die ganze Kette `speichere_entscheidung` → `_mit_lock_retry`
    → aiosqlite, inkl. dass der Eintrag am Ende tatsaechlich in der DB
    landet.
    """
    db_pfad = tmp_path / "lock_integration.db"
    s = Speicher(str(db_pfad))
    _run(s.verbinden())
    s._LOCK_RETRY_BASIS_S = 0.05  # 50 ms Backoff, kurz aber sichtbar

    # Zweite, blockierende Connection (klassisch sqlite3, nicht aiosqlite).
    # `BEGIN IMMEDIATE` haelt den reserved-write-Lock — jeder andere
    # Writer kriegt SQLITE_BUSY, bis wir committen/rollback machen.
    blocker = sqlite3.connect(str(db_pfad), timeout=0.1)
    blocker.isolation_level = None
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute(
        "INSERT INTO entscheidung_log "
        "(zeitstempel, zone_id, soll_bewaessern, dauer_sekunden, begruendung, "
        " scope, scope_ref) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-18T13:00:00", "blocker", 0, 0, "blocker", "zone", "blocker"),
    )

    async def _zugleich():
        # Den Blocker zeitversetzt loesen, waehrend speichere_entscheidung
        # auf den Lock wartet. 100 ms reichen: aiosqlite's busy_timeout=15s
        # ueberbrueckt das problemlos, der Retry-Wrapper wird hoechstens
        # einmal greifen.
        async def _release():
            await asyncio.sleep(0.1)
            blocker.commit()
            blocker.close()

        release_task = asyncio.create_task(_release())
        await s.speichere_entscheidung(_entscheidung())
        await release_task

    _run(_zugleich())

    eintraege = _run(s.hole_entscheidungen(zone_id="testzone"))
    assert len(eintraege) == 1
    assert eintraege[0].begruendung == "lock-retry-test"
    _run(s.schliessen())


def test_t0321_nebenlaeufiger_write_geht_nicht_in_fremde_tx_verloren(speicher):
    """T-0321: Ein nebenlaeufiger Einzel-Write (Realfall: Ventil-Close-Callback)
    darf NICHT in einen offenen `transaktion()`-Block eines ANDEREN Tasks
    absorbiert werden und bei dessen Rollback verloren gehen. Der Write-Lock
    serialisiert: der Einzel-Write wartet, bis die Tx fertig ist, und committet
    eigenstaendig. Vor dem Fix: Einzel-Write in die fremde Tx absorbiert ->
    Rollback verschluckt ihn (der T-0321-Datenverlust)."""
    basis = datetime(2026, 6, 21, 12, 0, 0)
    tx_event = VentilEreignis(
        zeitstempel=basis, zone_id="zone_tx", ventil_id="v",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0, ausloser=Ausloser.MANUELL,
    )
    einzel_event = VentilEreignis(
        zeitstempel=basis + timedelta(minutes=1), zone_id="zone_einzel",
        ventil_id="v", aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=60,
        ausloser=Ausloser.MANUELL,
    )

    async def main():
        async def task_tx_rollback():
            try:
                async with speicher.transaktion():
                    await speicher.speichere_ventil_ereignis(tx_event)
                    # Fenster, in dem der nebenlaeufige Einzel-Write reinlaeuft.
                    await asyncio.sleep(0.05)
                    raise RuntimeError("erzwungener Rollback")
            except RuntimeError:
                pass

        async def task_einzel_write():
            await asyncio.sleep(0.01)  # startet WAEHREND der offenen Tx
            await speicher.speichere_ventil_ereignis(einzel_event)

        await asyncio.gather(task_tx_rollback(), task_einzel_write())
        tx_rows = await speicher.hole_ventil_ereignisse("zone_tx")
        einzel_rows = await speicher.hole_ventil_ereignisse("zone_einzel")
        return tx_rows, einzel_rows

    tx_rows, einzel_rows = _run(main())
    # Tx-Write durch Rollback verschwunden:
    assert len(tx_rows) == 0
    # Nebenlaeufiger Einzel-Write NICHT verloren (Kern-Regression T-0321):
    assert len(einzel_rows) == 1
    assert einzel_rows[0].aktion == VentilAktion.SCHLIESSEN
