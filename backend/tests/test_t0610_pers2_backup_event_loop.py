"""T-0610 PERS-2: Kompression und Kopien des Backup-Jobs blockieren den
Event-Loop nicht mehr.

Gemessen wird die Wirkung, nicht die Verdrahtung: ein Ticker laeuft neben
`_fuehre_aus` und misst die groesste Luecke zwischen zwei Ticks, waehrend
jeweils ein Schritt (Kompression, Monats-Kopie, Spiegel) 0,4 s blockierend
arbeitet. Synchron im Loop waere die Luecke >= 0,4 s.

Harmlos: Attrappen-Speicher, tmp_path, keine echte DB.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path

import pytest

import bewaesserung.backup as backup
from bewaesserung.modelle import BackupKonfig

BLOCK_S = 0.4


class _SpeicherAttrappe:
    async def backup(self, ziel: Path) -> None:
        Path(ziel).write_bytes(b"SQLite format 3\x00" + b"x" * 64)


def _blockierend(original):
    def _f(*args, **kwargs):
        time.sleep(BLOCK_S)
        return original(*args, **kwargs)
    return _f


async def _max_luecke_waehrend(coro) -> float:
    fertig = asyncio.Event()
    max_luecke = 0.0

    async def _ticker():
        nonlocal max_luecke
        letzter = time.monotonic()
        while not fertig.is_set():
            await asyncio.sleep(0.01)
            jetzt = time.monotonic()
            max_luecke = max(max_luecke, jetzt - letzter)
            letzter = jetzt

    ticker = asyncio.create_task(_ticker())
    await asyncio.sleep(0)  # Ticker einmal anlaufen lassen
    try:
        await coro
    finally:
        fertig.set()
        await ticker
    return max_luecke


@pytest.mark.parametrize("schritt", ["kompression", "monatskopie", "spiegel"])
@pytest.mark.asyncio
async def test_backup_schritte_blockieren_event_loop_nicht(tmp_path, monkeypatch, schritt):
    konfig = BackupKonfig(
        aktiv=True, intervall_stunden=24,
        verzeichnis=str(tmp_path / "backup"),
        retention_taeglich_tage=14, monatlich_aktiv=True, max_dateien=200,
        spiegel_verzeichnis=str(tmp_path / "spiegel") if schritt == "spiegel" else None,
    )
    job = backup.BackupJob(_SpeicherAttrappe(), konfig)
    if schritt == "kompression":
        monkeypatch.setattr(backup, "_komprimiere_datei",
                            _blockierend(backup._komprimiere_datei))
    elif schritt == "monatskopie":
        monkeypatch.setattr(backup, "_kopiere_datei",
                            _blockierend(backup._kopiere_datei))
    else:
        monkeypatch.setattr(job, "_spiegele", _blockierend(job._spiegele))

    luecke = await _max_luecke_waehrend(job._fuehre_aus(datetime(2026, 4, 19, 3, 15)))

    assert (tmp_path / "backup" / "taeglich" / "bewaesserung_2026-04-19.db.gz").exists()
    assert luecke < BLOCK_S / 2, f"Event-Loop {luecke:.2f} s blockiert ({schritt})"
