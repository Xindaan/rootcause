"""Tests fuer Speicher.backup() — native SQLite-Backup-API mit Integrity-Check."""
import asyncio
import sqlite3
from datetime import datetime

import aiosqlite
import pytest

from bewaesserung.modelle import DatenQuelle, SensorMessung
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "quelle.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _fuelle_quelle(speicher: Speicher, anzahl: int = 3) -> None:
    """Schreibt ein paar Messungen, damit das Backup nicht leer ist."""
    async def _schreibe():
        for i in range(anzahl):
            await speicher.speichere_messung(
                SensorMessung(
                    zeitstempel=datetime(2026, 4, 19, 10, i),
                    zone_id="waldblumenhain",
                    geraet_id="sensor_test",
                    boden_feuchte=40.0 + i,
                    boden_temperatur=15.0,
                    umgebungs_temperatur=None,
                    licht_intensitaet=None,
                    batterie_prozent=90.0,
                    quelle=DatenQuelle.GARDENA,
                )
            )
    _run(_schreibe())


def test_backup_erzeugt_datei_mit_gleichen_zeilen(speicher, tmp_path):
    _fuelle_quelle(speicher, anzahl=3)
    ziel = tmp_path / "snapshot" / "bewaesserung_2026-04-19.db"

    _run(speicher.backup(ziel))

    assert ziel.exists()
    # tmp wurde aufgeraeumt
    assert not ziel.with_name(ziel.name + ".tmp").exists()

    conn = sqlite3.connect(str(ziel))
    try:
        cur = conn.execute("SELECT COUNT(*) FROM sensor_messung")
        (anzahl,) = cur.fetchone()
    finally:
        conn.close()
    assert anzahl == 3


def test_backup_ueberschreibt_existierendes_ziel_atomar(speicher, tmp_path):
    _fuelle_quelle(speicher, anzahl=2)
    ziel = tmp_path / "snapshot.db"
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_bytes(b"alter-inhalt")

    _run(speicher.backup(ziel))

    conn = sqlite3.connect(str(ziel))
    try:
        cur = conn.execute("SELECT COUNT(*) FROM sensor_messung")
        (anzahl,) = cur.fetchone()
    finally:
        conn.close()
    assert anzahl == 2


def test_backup_haelt_ziel_unberuehrt_bei_fehler(speicher, tmp_path, monkeypatch):
    _fuelle_quelle(speicher, anzahl=1)
    ziel = tmp_path / "snapshot.db"
    ziel.write_bytes(b"alter-inhalt")

    async def _wirft_fehler(*args, **kwargs):
        raise RuntimeError("simulierter Backup-Fehler")

    # Wir mocken den backup-Aufruf der Quell-Connection, damit die
    # Fehlerbehandlung (tmp-Cleanup + Ziel-Schutz) deterministisch testbar
    # ist. T-0218: Patch auf Klassen-Ebene, da `backup()` die Quelle ueber
    # eine eigene Connection oeffnet (nicht mehr `speicher._db`).
    monkeypatch.setattr(aiosqlite.Connection, "backup", _wirft_fehler)

    with pytest.raises(RuntimeError, match="simulierter Backup-Fehler"):
        _run(speicher.backup(ziel))

    # Ziel wurde nicht ueberschrieben
    assert ziel.read_bytes() == b"alter-inhalt"
    # tmp wurde aufgeraeumt
    assert not ziel.with_name(ziel.name + ".tmp").exists()


def test_backup_meldet_integrity_fehler(speicher, tmp_path, monkeypatch):
    _fuelle_quelle(speicher, anzahl=1)
    ziel = tmp_path / "snapshot.db"

    async def _backup_ohne_inhalt(self, target):
        # Kein echter Copy — target bleibt leere DB. Auf leerer SQLite-DB
        # liefert PRAGMA integrity_check zwar "ok", also muessen wir die
        # Pruefung selbst faelschen.
        return None

    # T-0218: Patch auf Klassen-Ebene (Backup-Quelle ist eine eigene
    # Connection, nicht mehr `speicher._db`).
    monkeypatch.setattr(aiosqlite.Connection, "backup", _backup_ohne_inhalt)

    # Zusatz: integrity_check liefert "malformed" — wir patchen das ueber die
    # Connection-Klasse der Ziel-Verbindung.
    import aiosqlite as _aio

    original_execute = _aio.Connection.execute

    def patched_execute(self, sql, *args, **kwargs):
        if isinstance(sql, str) and "integrity_check" in sql.lower():
            class FakeCursorCtx:
                async def __aenter__(self_):
                    return self_

                async def __aexit__(self_, *a):
                    return False

                async def fetchone(self_):
                    return ("malformed",)

            return FakeCursorCtx()
        return original_execute(self, sql, *args, **kwargs)

    monkeypatch.setattr(_aio.Connection, "execute", patched_execute)

    with pytest.raises(RuntimeError, match="integrity_check"):
        _run(speicher.backup(ziel))

    assert not ziel.exists()
    assert not ziel.with_name(ziel.name + ".tmp").exists()


def test_backup_nutzt_separate_readonly_quell_connection(
    speicher, tmp_path, monkeypatch,
):
    """T-0218: backup() oeffnet eine eigene mode=ro-Connection als
    Quelle statt die Produktiv-Connection `self._db` zu belegen.

    Hintergrund: der Online-Backup-Copy (~50 s bei 84-MB-DB) wuerde
    sonst den aiosqlite-Worker-Thread der Produktiv-Connection
    blockieren -> alle API-Reads + Entscheidungsloop haengen.
    """
    _fuelle_quelle(speicher, anzahl=2)
    ziel = tmp_path / "snap.db"

    geoeffnete_uris: list[str] = []
    orig_connect = aiosqlite.connect

    def _spy_connect(*args, **kwargs):
        if args:
            geoeffnete_uris.append(str(args[0]))
        return orig_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", _spy_connect)
    _run(speicher.backup(ziel))

    ro_quellen = [u for u in geoeffnete_uris if "mode=ro" in u]
    assert len(ro_quellen) == 1, (
        f"backup() soll genau 1 ro-Quell-Connection oeffnen, "
        f"geoeffnet wurde: {geoeffnete_uris}"
    )
    # Produktiv-Connection bleibt nach dem Backup nutzbar.
    nachher = _run(speicher.hole_messungen("waldblumenhain"))
    assert len(nachher) == 2
