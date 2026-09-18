"""T-0480: Rotation der Service-Logs.

Der wichtigste Test hier ist `test_laufender_schreiber_...`: er bildet den
Fallstrick nach, den das Akzeptanzkriterium ausdruecklich nennt. Ein
logrotate, das die Datei umbenennt oder loescht, laesst den laufenden
Prozess in die alte Inode weiterschreiben -- die neue Datei bleibt fuer
immer leer, und niemand merkt es, weil alles "erfolgreich rotiert" aussieht.
Deshalb wird hier mit einem echten offenen File-Descriptor im
`O_APPEND`-Modus getestet, so wie launchd ihn haelt, und nicht mit einer
Mock-Datei.
"""
from __future__ import annotations

import gzip
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bewaesserung.log_rotation_job import LogRotationJob

JETZT = datetime(2026, 8, 5, 15, 0, 0)


def _schreibe(pfad: Path, zeilen: int, praefix: str = "zeile") -> None:
    with pfad.open("a") as f:
        for i in range(zeilen):
            f.write(f"{praefix}-{i:05d}\n")


@pytest.mark.asyncio
async def test_rotiert_erst_ab_der_schwelle(tmp_path):
    log = tmp_path / "service-stdout.log"
    _schreibe(log, 10)
    job = LogRotationJob(tmp_path, max_bytes=10_000)

    await job.aktualisiere_wenn_faellig(JETZT)

    assert log.exists() and log.stat().st_size > 0
    assert not (tmp_path / "archiv").exists(), (
        "unter der Schwelle darf nichts passieren"
    )


@pytest.mark.asyncio
async def test_archiviert_komprimiert_und_kuerzt(tmp_path):
    log = tmp_path / "service-stdout.log"
    _schreibe(log, 2000)
    original = log.read_text()
    job = LogRotationJob(tmp_path, max_bytes=1000)

    await job.aktualisiere_wenn_faellig(JETZT)

    archive = list((tmp_path / "archiv").glob("service-stdout-*.log.gz"))
    assert len(archive) == 1
    with gzip.open(archive[0], "rt") as f:
        assert f.read() == original, "Archiv muss den Inhalt 1:1 tragen"
    assert log.stat().st_size == 0, "Quelle muss gekuerzt sein"


@pytest.mark.asyncio
async def test_laufender_schreiber_schreibt_ohne_neustart_weiter(tmp_path):
    """Der Kern von Akzeptanzkriterium 2 (T-0480).

    Simuliert launchd: ein offener FD im Append-Modus, der die Rotation
    ueberdauert. Wuerde der Job die Datei umbenennen oder ersetzen, landete
    die Zeile danach in einer verwaisten Inode und die Logdatei bliebe leer.
    """
    log = tmp_path / "service-stdout.log"
    _schreibe(log, 2000, "vorher")
    job = LogRotationJob(tmp_path, max_bytes=1000)

    fd = os.open(log, os.O_WRONLY | os.O_APPEND)
    try:
        await job.aktualisiere_wenn_faellig(JETZT)
        os.write(fd, b"nachher-0001\n")
    finally:
        os.close(fd)

    inhalt = log.read_text()
    assert "nachher-0001" in inhalt, (
        "der laufende Schreiber hat die neue Datei nicht erreicht -- "
        "klassischer logrotate-Fallstrick (Inode ersetzt statt gekuerzt)"
    )
    assert "vorher" not in inhalt, "Altbestand muss weg sein"
    assert log.stat().st_size < 1000


@pytest.mark.asyncio
async def test_angebrochene_zeile_ueberlebt_die_kuerzung(tmp_path):
    """Geschnitten wird an der letzten vollstaendigen Zeilengrenze.

    Eine halbe Zeile im Archiv und die andere Haelfte in der frischen Datei
    waere beim Auswerten schlimmer als der Umbruch selbst.
    """
    log = tmp_path / "service-stdout.log"
    _schreibe(log, 2000)
    with log.open("a") as f:
        f.write("angebrochen-ohne-umbruch")
    job = LogRotationJob(tmp_path, max_bytes=1000)

    await job.aktualisiere_wenn_faellig(JETZT)

    assert log.read_text() == "angebrochen-ohne-umbruch"
    archiv = next((tmp_path / "archiv").glob("*.log.gz"))
    with gzip.open(archiv, "rt") as f:
        assert "angebrochen" not in f.read()


@pytest.mark.asyncio
async def test_nichts_wird_verworfen(tmp_path):
    """Andres Entscheidung 02.08.: Generationen bleiben erhalten.

    Gegenprobe zum `RotatingFileHandler`, der ab `backupCount` die aelteste
    Generation loescht. Hier muss jede Rotation eine eigene Datei
    hinterlassen.
    """
    log = tmp_path / "service-stdout.log"
    job = LogRotationJob(tmp_path, max_bytes=1000, intervall_stunden=0)

    for runde in range(3):
        _schreibe(log, 2000, f"runde{runde}")
        await job.aktualisiere_wenn_faellig(JETZT + timedelta(hours=runde))

    archive = sorted((tmp_path / "archiv").glob("*.log.gz"))
    assert len(archive) == 3, "jede Rotation braucht ihre eigene Generation"
    zusammen = ""
    for a in archive:
        with gzip.open(a, "rt") as f:
            zusammen += f.read()
    for runde in range(3):
        assert f"runde{runde}-00000" in zusammen, (
            f"Generation {runde} wurde verworfen"
        )


@pytest.mark.asyncio
async def test_intervall_gate_haelt(tmp_path):
    log = tmp_path / "service-stdout.log"
    _schreibe(log, 2000)
    job = LogRotationJob(tmp_path, max_bytes=1000)

    await job.aktualisiere_wenn_faellig(JETZT)
    _schreibe(log, 2000)
    await job.aktualisiere_wenn_faellig(JETZT + timedelta(minutes=5))

    assert len(list((tmp_path / "archiv").glob("*.log.gz"))) == 1, (
        "innerhalb des Intervalls darf kein zweiter Lauf stattfinden"
    )


@pytest.mark.asyncio
async def test_einzelne_ueberlange_zeile_wird_nicht_zerrissen(tmp_path):
    """Ohne Zeilenumbruch gibt es keine gueltige Schnittstelle.

    Lieber gar nicht rotieren als eine Zeile in zwei Dateien zerlegen; beim
    naechsten Lauf ist die Chance gut, dass ein Umbruch dazugekommen ist.
    """
    log = tmp_path / "service-stdout.log"
    log.write_text("x" * 5000)
    job = LogRotationJob(tmp_path, max_bytes=1000)

    await job.aktualisiere_wenn_faellig(JETZT)

    assert log.stat().st_size == 5000, "Datei darf nicht angetastet werden"
    assert not list((tmp_path / "archiv").glob("*.log.gz"))


@pytest.mark.asyncio
async def test_fehlendes_verzeichnis_ist_kein_fehler(tmp_path):
    job = LogRotationJob(tmp_path / "gibt-es-nicht")
    await job.aktualisiere_wenn_faellig(JETZT)
    assert job.letzter_fehler is None
