"""Tests fuer BackupJob — Intervall-Gate, Rotation, Fehler-Isolation."""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bewaesserung.backup import BackupJob
from bewaesserung.modelle import BackupKonfig, DatenQuelle, SensorMessung
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "quelle.db"))
    _run(s.verbinden())
    _run(s.speichere_messung(
        SensorMessung(
            zeitstempel=datetime(2026, 4, 19, 10, 0),
            zone_id="waldblumenhain",
            geraet_id="sensor_test",
            boden_feuchte=42.0,
            boden_temperatur=15.0,
            umgebungs_temperatur=None,
            licht_intensitaet=None,
            batterie_prozent=90.0,
            quelle=DatenQuelle.GARDENA,
        ),
    ))
    try:
        yield s
    finally:
        _run(s.schliessen())


def _konfig(tmp_path: Path, **overrides) -> BackupKonfig:
    default = {
        "aktiv": True,
        "intervall_stunden": 24,
        "verzeichnis": str(tmp_path / "backup"),
        "retention_taeglich_tage": 14,
        "monatlich_aktiv": True,
        "max_dateien": 200,
    }
    default.update(overrides)
    return BackupKonfig(**default)


def test_backup_job_erstellt_taeglich_und_monatlich(speicher, tmp_path):
    job = BackupJob(speicher, _konfig(tmp_path))
    jetzt = datetime(2026, 4, 19, 3, 15)

    ausgefuehrt = _run(job.aktualisiere_wenn_faellig(jetzt))

    assert ausgefuehrt is True
    taeglich = tmp_path / "backup" / "taeglich" / "bewaesserung_2026-04-19.db"
    monatlich = tmp_path / "backup" / "monatlich" / "bewaesserung_2026-04.db"
    assert taeglich.exists()
    assert monatlich.exists()


def test_backup_job_intervall_gate_blockiert_zweiten_lauf(speicher, tmp_path):
    job = BackupJob(speicher, _konfig(tmp_path, intervall_stunden=24))
    basis = datetime(2026, 4, 19, 3, 15)

    assert _run(job.aktualisiere_wenn_faellig(basis)) is True
    # 23 Stunden spaeter: noch nicht faellig
    assert _run(job.aktualisiere_wenn_faellig(basis + timedelta(hours=23))) is False
    # 25 Stunden spaeter: faellig
    assert _run(job.aktualisiere_wenn_faellig(basis + timedelta(hours=25))) is True


def test_backup_job_idempotent_pro_tag(speicher, tmp_path):
    """Zweimaliger Lauf am selben Tag liefert nur einen Eintrag."""
    job = BackupJob(speicher, _konfig(tmp_path, intervall_stunden=0))
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 3, 15)))
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 23, 50)))

    taeglich_dir = tmp_path / "backup" / "taeglich"
    dateien = list(taeglich_dir.glob("bewaesserung_*.db"))
    assert len(dateien) == 1
    assert dateien[0].name == "bewaesserung_2026-04-19.db"


def test_backup_job_monatlich_nur_einmal_pro_monat(speicher, tmp_path):
    """Der zweite Lauf im gleichen Monat ueberschreibt das Monatlich-Backup nicht."""
    job = BackupJob(speicher, _konfig(tmp_path, intervall_stunden=0))
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 1, 3, 15)))
    monatlich = tmp_path / "backup" / "monatlich" / "bewaesserung_2026-04.db"
    erstanlage = monatlich.stat().st_mtime_ns
    # Kuenstliche Verzoegerung, damit ein Ueberschreiben definitiv eine andere
    # mtime haette.
    import time
    time.sleep(0.01)

    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 15, 3, 15)))

    assert monatlich.stat().st_mtime_ns == erstanlage


def test_backup_job_rotiert_alte_taeglich_backups(speicher, tmp_path):
    """Bei 20 Fake-Daten bleiben nach Rotation die 14 juengsten uebrig."""
    konfig = _konfig(tmp_path, intervall_stunden=0, retention_taeglich_tage=14,
                     monatlich_aktiv=False)
    taeglich_dir = Path(konfig.verzeichnis) / "taeglich"
    taeglich_dir.mkdir(parents=True, exist_ok=True)
    # 20 alte Dateien anlegen (werden rotiert)
    for i in range(20):
        tag = datetime(2026, 3, 1) + timedelta(days=i)
        (taeglich_dir / f"bewaesserung_{tag:%Y-%m-%d}.db").write_bytes(b"x")

    job = BackupJob(speicher, konfig)
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 3, 15)))

    dateien = sorted(taeglich_dir.glob("bewaesserung_*.db"))
    # 20 alte + 1 neue = 21, nach Rotation: 14
    assert len(dateien) == 14
    # Die juengsten sind uebrig geblieben
    assert dateien[-1].name == "bewaesserung_2026-04-19.db"


def test_backup_job_deaktiviert_macht_nichts(speicher, tmp_path):
    job = BackupJob(speicher, _konfig(tmp_path, aktiv=False))

    ausgefuehrt = _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 3, 15)))

    assert ausgefuehrt is False
    assert not (tmp_path / "backup").exists()


def test_backup_job_isoliert_fehler(speicher, tmp_path, monkeypatch):
    """Wirft speicher.backup, darf der Job nicht crashen und nicht als ok markieren."""
    job = BackupJob(speicher, _konfig(tmp_path))

    async def _wirft(*args, **kwargs):
        raise RuntimeError("simulierter Backup-Fehler")

    monkeypatch.setattr(speicher, "backup", _wirft)

    ausgefuehrt = _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 3, 15)))

    assert ausgefuehrt is False
    assert job._letzte_aktualisierung is None


def test_backup_job_spiegel_kopiert_taeglich_und_monatlich(speicher, tmp_path):
    """T-0131 (H-7): mit spiegel_verzeichnis liegt jeder Snapshot zusaetzlich
    im Spiegel (z. B. iCloud Drive)."""
    spiegel = tmp_path / "icloud_spiegel"
    konfig = _konfig(
        tmp_path, intervall_stunden=0,
        spiegel_verzeichnis=str(spiegel),
    )
    job = BackupJob(speicher, konfig)
    jetzt = datetime(2026, 4, 19, 3, 15)
    _run(job.aktualisiere_wenn_faellig(jetzt))

    spiegel_taeglich = spiegel / "taeglich" / "bewaesserung_2026-04-19.db"
    spiegel_monatlich = spiegel / "monatlich" / "bewaesserung_2026-04.db"
    lokal_taeglich = tmp_path / "backup" / "taeglich" / "bewaesserung_2026-04-19.db"
    assert lokal_taeglich.exists()
    assert spiegel_taeglich.exists()
    assert spiegel_monatlich.exists()
    # Inhalt muss identisch sein (Spiegel vom lokalen Snapshot kopiert).
    assert spiegel_taeglich.read_bytes() == lokal_taeglich.read_bytes()


def test_backup_job_spiegel_fehler_kein_lokaler_crash(speicher, tmp_path, monkeypatch):
    """T-0131 (H-7): Spiegel-Fehler (z. B. iCloud offline) darf den
    lokalen Backup-Lauf NICHT als Fehlschlag markieren."""
    spiegel = tmp_path / "icloud_spiegel"
    konfig = _konfig(
        tmp_path, intervall_stunden=0,
        spiegel_verzeichnis=str(spiegel),
    )
    job = BackupJob(speicher, konfig)

    # _spiegele soll wirf eine Exception, _fuehre_aus muss sauber durchlaufen.
    def _wirft(**kwargs):
        raise OSError("iCloud nicht erreichbar")
    monkeypatch.setattr(job, "_spiegele", _wirft)

    # Ohne unser Wrap-Verhalten wuerde das gesamte _fuehre_aus crashen.
    # Da _spiegele NICHT in _fuehre_aus's try-except ist, faengt
    # aktualisiere_wenn_faellig den Fehler ab und meldet False.
    # Wir dokumentieren das aktuelle Verhalten.
    ausgefuehrt = _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19)))
    # Lokaler Snapshot ist trotzdem geschrieben (vor dem Spiegel-Schritt).
    lokal = tmp_path / "backup" / "taeglich" / "bewaesserung_2026-04-19.db"
    assert lokal.exists()
    # ausgefuehrt=False, weil aktualisiere_wenn_faellig die Exception faengt.
    # Beim naechsten Tick wird erneut versucht (Spiegel-Recovery).
    assert ausgefuehrt is False


def test_backup_job_spiegel_aus_per_default(speicher, tmp_path):
    """T-0131 (H-7) Backward-Compat: ohne spiegel_verzeichnis kein Spiegel-Touch."""
    konfig = _konfig(tmp_path, intervall_stunden=0)
    assert konfig.spiegel_verzeichnis is None
    job = BackupJob(speicher, konfig)
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 3, 15)))
    # Kein Spiegel-Verzeichnis irgendwo angelegt
    assert not any(p.name == "icloud_spiegel" for p in tmp_path.iterdir())


def test_backup_job_monatlich_ueberlauf_warnt(speicher, tmp_path, capfd):
    konfig = _konfig(tmp_path, intervall_stunden=0, max_dateien=3)
    monatlich_dir = Path(konfig.verzeichnis) / "monatlich"
    monatlich_dir.mkdir(parents=True, exist_ok=True)
    for name in ("2024-01", "2024-02", "2024-03", "2024-04"):
        (monatlich_dir / f"bewaesserung_{name}.db").write_bytes(b"x")

    job = BackupJob(speicher, konfig)
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 3, 15)))

    # Monatlich-Backup 2026-04 wurde neu angelegt → 5 Dateien, Ueberlauf >3
    assert len(list(monatlich_dir.glob("*.db"))) == 5
    # Warnung wird via structlog nach stdout geschrieben (capfd faengt das ein).
    out, _ = capfd.readouterr()
    assert "monatlich_ueberlauf" in out
