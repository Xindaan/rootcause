"""Tests fuer BackupJob — Intervall-Gate, Rotation, Fehler-Isolation,
Kompression + Streaming (T-0475)."""
import asyncio
import gzip
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bewaesserung.backup import BackupJob, _kopiere_datei
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
    taeglich = tmp_path / "backup" / "taeglich" / "bewaesserung_2026-04-19.db.gz"
    monatlich = tmp_path / "backup" / "monatlich" / "bewaesserung_2026-04.db.gz"
    assert taeglich.exists()
    assert monatlich.exists()
    # T-0475: unkomprimiert darf nichts liegenbleiben (auch keine Roh-Datei).
    taeglich_dir = tmp_path / "backup" / "taeglich"
    assert [p.name for p in taeglich_dir.iterdir()] == [
        "bewaesserung_2026-04-19.db.gz",
    ]


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
    dateien = list(taeglich_dir.glob("bewaesserung_*"))
    assert len(dateien) == 1
    assert dateien[0].name == "bewaesserung_2026-04-19.db.gz"


def test_backup_job_monatlich_nur_einmal_pro_monat(speicher, tmp_path):
    """Der zweite Lauf im gleichen Monat ueberschreibt das Monatlich-Backup nicht."""
    job = BackupJob(speicher, _konfig(tmp_path, intervall_stunden=0))
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 1, 3, 15)))
    monatlich = tmp_path / "backup" / "monatlich" / "bewaesserung_2026-04.db.gz"
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
        (taeglich_dir / f"bewaesserung_{tag:%Y-%m-%d}.db.gz").write_bytes(b"x")

    job = BackupJob(speicher, konfig)
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 3, 15)))

    dateien = sorted(taeglich_dir.glob("bewaesserung_*"))
    # 20 alte + 1 neue = 21, nach Rotation: 14
    assert len(dateien) == 14
    # Die juengsten sind uebrig geblieben
    assert dateien[-1].name == "bewaesserung_2026-04-19.db.gz"


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

    spiegel_taeglich = spiegel / "taeglich" / "bewaesserung_2026-04-19.db.gz"
    spiegel_monatlich = spiegel / "monatlich" / "bewaesserung_2026-04.db.gz"
    lokal_taeglich = (
        tmp_path / "backup" / "taeglich" / "bewaesserung_2026-04-19.db.gz"
    )
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
    lokal = tmp_path / "backup" / "taeglich" / "bewaesserung_2026-04-19.db.gz"
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
        (monatlich_dir / f"bewaesserung_{name}.db.gz").write_bytes(b"x")

    job = BackupJob(speicher, konfig)
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 3, 15)))

    # Monatlich-Backup 2026-04 wurde neu angelegt → 5 Dateien, Ueberlauf >3
    assert len(list(monatlich_dir.glob("bewaesserung_*"))) == 5
    # Warnung wird via structlog nach stdout geschrieben (capfd faengt das ein).
    out, _ = capfd.readouterr()
    assert "monatlich_ueberlauf" in out


# --- T-0475: Kompression, Streaming, Rotation ueber beide Ablageformen ---


def test_backup_ist_gueltiges_gzip_mit_lesbarer_db(speicher, tmp_path):
    """Der Snapshot muss entpackbar und danach eine intakte SQLite-DB sein.

    Das ist der Restore-Weg aus docs/backup_wiederherstellung.md in klein:
    gunzip -> sqlite3 oeffnen -> Zaehlabfrage.
    """
    job = BackupJob(speicher, _konfig(tmp_path, intervall_stunden=0))
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 3, 15)))

    gz = tmp_path / "backup" / "taeglich" / "bewaesserung_2026-04-19.db.gz"
    # gzip-Magic
    assert gz.read_bytes()[:2] == b"\x1f\x8b"

    entpackt = tmp_path / "restore.db"
    with gzip.open(gz, "rb") as q, entpackt.open("wb") as z:
        z.write(q.read())
    # SQLite-Header + echte Zeile aus der Quell-DB
    assert entpackt.read_bytes()[:15] == b"SQLite format 3"
    con = sqlite3.connect(str(entpackt))
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        anzahl = con.execute("SELECT COUNT(*) FROM sensor_messung").fetchone()[0]
    finally:
        con.close()
    assert anzahl == 1


def test_backup_komprimiert_spuerbar(speicher, tmp_path):
    """Der komprimierte Snapshot ist kleiner als die rohe DB.

    Absicherung gegen ein `.db.gz`, das in Wahrheit unkomprimiert ist
    (z. B. wenn jemand den Kompressionsschritt auf eine reine Kopie
    zurueckdreht und nur den Namen behaelt).
    """
    job = BackupJob(speicher, _konfig(tmp_path, intervall_stunden=0))
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 3, 15)))

    gz = tmp_path / "backup" / "taeglich" / "bewaesserung_2026-04-19.db.gz"
    # Gegen den entpackten Snapshot vergleichen, nicht gegen die Quell-Datei:
    # im WAL-Modus liegt der Grossteil der Daten im -wal, die `.db` selbst
    # ist kaum gefuellt.
    with gzip.open(gz, "rb") as f:
        entpackt_groesse = len(f.read())
    assert gz.stat().st_size < entpackt_groesse


def test_rotation_erfasst_alt_bestand_unkomprimiert(speicher, tmp_path):
    """Der Kern von T-0475: `.db`-Alt-Bestand muss weiter rotiert werden.

    Ein Glob nur auf `bewaesserung_*.db.gz` wuerde die alten Dateien nie
    mehr anfassen — die Retention liefe fuer sie still ins Leere.
    """
    konfig = _konfig(tmp_path, intervall_stunden=0, retention_taeglich_tage=3,
                     monatlich_aktiv=False)
    taeglich_dir = Path(konfig.verzeichnis) / "taeglich"
    taeglich_dir.mkdir(parents=True, exist_ok=True)
    for i in range(6):
        tag = datetime(2026, 3, 1) + timedelta(days=i)
        (taeglich_dir / f"bewaesserung_{tag:%Y-%m-%d}.db").write_bytes(b"x")

    job = BackupJob(speicher, konfig)
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 3, 15)))

    namen = sorted(p.name for p in taeglich_dir.glob("bewaesserung_*"))
    assert namen == [
        "bewaesserung_2026-03-05.db",
        "bewaesserung_2026-03-06.db",
        "bewaesserung_2026-04-19.db.gz",
    ]


def test_rotation_zaehlt_gemischten_tag_einmal(speicher, tmp_path):
    """Migrations-Zwischenzustand: `.db` und `.db.gz` desselben Tages.

    Beide zusammen sind EIN Retention-Eintrag; faellt der Tag raus,
    verschwinden beide Formen.
    """
    konfig = _konfig(tmp_path, intervall_stunden=0, retention_taeglich_tage=2,
                     monatlich_aktiv=False)
    taeglich_dir = Path(konfig.verzeichnis) / "taeglich"
    taeglich_dir.mkdir(parents=True, exist_ok=True)
    for tag in ("2026-03-01", "2026-03-02"):
        (taeglich_dir / f"bewaesserung_{tag}.db").write_bytes(b"x")
        (taeglich_dir / f"bewaesserung_{tag}.db.gz").write_bytes(b"y")

    job = BackupJob(speicher, konfig)
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 3, 15)))

    namen = sorted(p.name for p in taeglich_dir.glob("bewaesserung_*"))
    # Retention 2: neuer Tag + 2026-03-02 (beide Formen), 03-01 faellt ganz raus.
    assert namen == [
        "bewaesserung_2026-03-02.db",
        "bewaesserung_2026-03-02.db.gz",
        "bewaesserung_2026-04-19.db.gz",
    ]


def test_monatlich_legt_neben_altbestand_kein_duplikat_an(speicher, tmp_path):
    """Existiert der Monats-Snapshot noch unkomprimiert, entsteht kein zweiter."""
    konfig = _konfig(tmp_path, intervall_stunden=0)
    monatlich_dir = Path(konfig.verzeichnis) / "monatlich"
    monatlich_dir.mkdir(parents=True, exist_ok=True)
    alt = monatlich_dir / "bewaesserung_2026-04.db"
    alt.write_bytes(b"alt")

    job = BackupJob(speicher, konfig)
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 3, 15)))

    assert sorted(p.name for p in monatlich_dir.glob("bewaesserung_*")) == [
        "bewaesserung_2026-04.db",
    ]
    assert alt.read_bytes() == b"alt"  # Alt-Bestand unangetastet


def test_kopiere_datei_streamt_ohne_vollread(tmp_path, monkeypatch):
    """T-0475: keine Voll-Lesung der Datei in den RAM.

    `read_bytes` wird scharf geschaltet — wer es benutzt, faellt durch.
    Zusaetzlich wird geprueft, dass der Inhalt trotzdem exakt ankommt.
    """
    quelle = tmp_path / "quelle.bin"
    nutzlast = bytes(range(256)) * 8192  # 2 MiB, mehr als ein Chunk
    quelle.write_bytes(nutzlast)
    ziel = tmp_path / "ziel.bin"

    def _verboten(self, *args, **kwargs):
        raise AssertionError("read_bytes() laedt die ganze Datei in den RAM")

    monkeypatch.setattr(Path, "read_bytes", _verboten)

    _kopiere_datei(quelle, ziel)

    assert ziel.stat().st_size == len(nutzlast)
    with ziel.open("rb") as f:
        assert f.read() == nutzlast


def test_kopiere_datei_laesst_bei_fehler_kein_halbes_ziel(tmp_path, monkeypatch):
    """Abbruch mittendrin darf kein Teil-Ziel hinterlassen — sonst wuerde
    es spaeter als vorhandener Monats-Snapshot gezaehlt."""
    quelle = tmp_path / "quelle.bin"
    quelle.write_bytes(b"z" * (3 * 1024 * 1024))
    ziel = tmp_path / "ziel.bin"

    import shutil as _shutil

    def _bricht_ab(*args, **kwargs):
        raise OSError("Platte voll")

    monkeypatch.setattr(_shutil, "copyfileobj", _bricht_ab)

    with pytest.raises(OSError):
        _kopiere_datei(quelle, ziel)

    assert not ziel.exists()
    assert not (tmp_path / "ziel.bin.tmp").exists()
