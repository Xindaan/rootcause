"""Periodischer DB-Backup-Job mit Rotation (T-0043).

Wofuer:
  Schuetzt die SQLite-DB gegen Datei-Korruption, fehlgeschlagene
  Migrationen, versehentlichem Loeschen und SD-Kartenfehler (Pi-Umzug).
  Time-Machine allein reicht nicht, weil Snapshot-Zeiten nicht
  kontrollierbar sind und bei offener DB inkonsistente Dateien liefern.

Layout (T-0475: gzip-komprimiert):
  <verzeichnis>/taeglich/   bewaesserung_YYYY-MM-DD.db.gz   (letzte N Tage)
  <verzeichnis>/monatlich/  bewaesserung_YYYY-MM.db.gz      (erster je Monat, dauerhaft)

Trigger:
  Intervall-Gate (Default 24h) analog zu WetterArchivJob. Wird aus dem
  Entscheidungs-Loop in main.py aufgerufen.

T-0475 (01.08.2026) — Kompression + Streaming:
  Gemessen an `bewaesserung_2026-07-25.db`: 195,3 MB -> 38,4 MB mit
  `gzip -6` (Faktor 5,1). Es wird nichts geloescht und nichts
  ausgeduennt; das Ziel wird ausschliesslich ueber die Ablageform
  erreicht, alle Messdaten bleiben vollstaendig ueberpruefbar.

  Ablauf: SQLite schreibt seinen Snapshot (Backup-API) in eine
  temporaere Roh-Datei mit fuehrendem Punkt (matcht die Rotations-Globs
  bewusst NICHT), die anschliessend chunkweise nach `.db.gz`
  komprimiert und danach geloescht wird. Kopien (monatlich, Spiegel)
  laufen ueber `shutil.copyfileobj`, also ohne Voll-Read in den RAM —
  vorher lud `ziel.write_bytes(quelle.read_bytes())` die komplette
  210-MB-Datei in den Speicher des laufenden Dienstes.

  Rotation zaehlt Alt-Bestand (`.db`) UND Neu-Bestand (`.db.gz`) und
  gruppiert beide ueber denselben logischen Schluessel — sonst wuerde
  die taegliche Rotation nach der Umstellung still nichts mehr treffen
  und `retention_taeglich_tage` liefe ins Leere.

  Restore: siehe `docs/backup_wiederherstellung.md`.
"""

from __future__ import annotations

import gzip
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import structlog

from bewaesserung.modelle import BackupKonfig
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

# T-0475: Ablageform der Snapshots. `.db` bleibt lesbarer Alt-Bestand.
KOMPRESS_SUFFIX = ".gz"
KOMPRESS_LEVEL = 6          # gemessener Sweet Spot: Faktor 5,1
_CHUNK = 1024 * 1024        # 1 MiB Streaming-Puffer


class BackupJob:
    """Erzeugt taeglich einen DB-Snapshot, rotiert alte Dateien."""

    def __init__(self, speicher: Speicher, konfig: BackupKonfig):
        self._speicher = speicher
        self._konfig = konfig
        self._intervall = timedelta(hours=konfig.intervall_stunden)
        self._letzte_aktualisierung: datetime | None = None

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> bool:
        """Laeuft hoechstens einmal pro Intervall. True wenn ausgefuehrt."""
        if not self._konfig.aktiv:
            return False
        jetzt = jetzt or datetime.now()
        if (self._letzte_aktualisierung
                and jetzt - self._letzte_aktualisierung < self._intervall):
            return False
        try:
            await self._fuehre_aus(jetzt)
        except Exception:
            # Fehler-Isolation: Backup-Ausfall darf den Service nicht kippen.
            # _letzte_aktualisierung bleibt ungesetzt, damit der naechste
            # Zyklus es erneut versucht.
            logger.exception("backup.fehler")
            return False
        self._letzte_aktualisierung = jetzt
        return True

    async def _fuehre_aus(self, jetzt: datetime) -> None:
        """Erstellt Taeglich-Snapshot, optional Monatlich-Kopie, rotiert.

        T-0131 (H-7): zusaetzliche Spiegelung in `spiegel_verzeichnis`
        (z. B. iCloud Drive), damit Mac-Disk-Defekt nicht alle Backups
        gleichzeitig mitnimmt. Spiegel folgt derselben Layout-Konvention
        (taeglich/ + monatlich/) und wird mitrotiert.
        """
        verzeichnis = Path(self._konfig.verzeichnis)
        taeglich_dir = verzeichnis / "taeglich"
        monatlich_dir = verzeichnis / "monatlich"
        taeglich_dir.mkdir(parents=True, exist_ok=True)
        if self._konfig.monatlich_aktiv:
            monatlich_dir.mkdir(parents=True, exist_ok=True)

        taeglich_basis = f"bewaesserung_{jetzt:%Y-%m-%d}.db"
        taeglich_name = taeglich_basis + KOMPRESS_SUFFIX
        taeglich_pfad = taeglich_dir / taeglich_name

        # T-0475: SQLite muss seinen Snapshot als echte DB-Datei schreiben.
        # Die Roh-Datei bekommt einen fuehrenden Punkt, damit sie von den
        # Rotations- und Status-Globs (`bewaesserung_*`) nicht getroffen
        # wird, und verschwindet nach dem Komprimieren wieder.
        roh_pfad = taeglich_dir / f".{taeglich_basis}.roh"
        try:
            await self._speicher.backup(roh_pfad)
            _komprimiere_datei(roh_pfad, taeglich_pfad)
        finally:
            try:
                roh_pfad.unlink(missing_ok=True)
            except OSError:
                logger.exception("backup.roh_aufraeumen_fehler", datei=str(roh_pfad))

        monatlich_erstellt = False
        if self._konfig.monatlich_aktiv:
            monatlich_basis = f"bewaesserung_{jetzt:%Y-%m}.db"
            monatlich_pfad = monatlich_dir / (monatlich_basis + KOMPRESS_SUFFIX)
            if not _snapshot_vorhanden(monatlich_dir, monatlich_basis):
                # Den frischen Taeglich-Snapshot kopieren — kein zweiter
                # Datenbank-Zugriff noetig, und er ist bereits komprimiert.
                _kopiere_datei(taeglich_pfad, monatlich_pfad)
                monatlich_erstellt = True

        geloescht = self._rotiere(taeglich_dir, monatlich_dir)

        # H-7: Spiegelung. Fehler isolieren -- iCloud-Drive-Hickup darf
        # den lokalen Backup-Lauf nicht als Fehlschlag markieren.
        spiegel_resultat: dict | None = None
        if self._konfig.spiegel_verzeichnis:
            spiegel_resultat = self._spiegele(
                taeglich_pfad=taeglich_pfad,
                taeglich_name=taeglich_name,
                monatlich_erstellt=monatlich_erstellt,
                jetzt=jetzt,
            )

        logger.info(
            "backup.erstellt",
            taeglich=str(taeglich_pfad),
            monatlich_erstellt=monatlich_erstellt,
            taeglich_geloescht=geloescht,
            spiegel=spiegel_resultat,
        )

    def _spiegele(
        self, *, taeglich_pfad: Path, taeglich_name: str,
        monatlich_erstellt: bool, jetzt: datetime,
    ) -> dict:
        """T-0131 (H-7): Spiegel-Kopie + Rotation. Fehler isoliert geloggt,
        eigene Stat-Dict zurueckgegeben fuer Diagnose-Logging.
        """
        try:
            spiegel_root = Path(
                self._konfig.spiegel_verzeichnis or ""
            ).expanduser()
            spiegel_taeglich = spiegel_root / "taeglich"
            spiegel_monatlich = spiegel_root / "monatlich"
            spiegel_taeglich.mkdir(parents=True, exist_ok=True)
            if self._konfig.monatlich_aktiv:
                spiegel_monatlich.mkdir(parents=True, exist_ok=True)

            ziel_taeglich = spiegel_taeglich / taeglich_name
            _kopiere_datei(taeglich_pfad, ziel_taeglich)

            spiegel_monatlich_kopie = False
            if self._konfig.monatlich_aktiv:
                monatlich_basis = f"bewaesserung_{jetzt:%Y-%m}.db"
                spiegel_monatlich_pfad = (
                    spiegel_monatlich / (monatlich_basis + KOMPRESS_SUFFIX)
                )
                if monatlich_erstellt or not _snapshot_vorhanden(
                    spiegel_monatlich, monatlich_basis,
                ):
                    _kopiere_datei(taeglich_pfad, spiegel_monatlich_pfad)
                    spiegel_monatlich_kopie = True

            spiegel_geloescht = self._rotiere(
                spiegel_taeglich, spiegel_monatlich,
            )
            return {
                "verzeichnis": str(spiegel_root),
                "taeglich_geloescht": spiegel_geloescht,
                "monatlich_kopie": spiegel_monatlich_kopie,
            }
        except Exception:
            logger.exception(
                "backup.spiegel_fehler",
                spiegel_verzeichnis=self._konfig.spiegel_verzeichnis,
            )
            return {"fehler": True}

    def _rotiere(
        self, taeglich_dir: Path, monatlich_dir: Path,
    ) -> int:
        """Haelt letzte N Taeglich-Backups, warnt bei Monatlich-Ueberlauf.

        Gibt die Anzahl geloeschter taeglicher Backups zurueck.

        T-0475: gezaehlt wird pro *Tag*, nicht pro Datei — komprimierter
        und unkomprimierter Snapshot desselben Tages sind ein Eintrag
        (relevant waehrend der Migration des Alt-Bestands). Faellt ein
        Tag aus der Retention, verschwinden beide Formen.
        """
        geloescht = 0
        nach_tag = _snapshots_nach_schluessel(taeglich_dir)
        behalten = self._konfig.retention_taeglich_tage
        for alt_tag in sorted(nach_tag, reverse=True)[behalten:]:
            for alt in nach_tag[alt_tag]:
                try:
                    alt.unlink()
                    geloescht += 1
                except OSError:
                    logger.exception("backup.rotation_fehler", datei=str(alt))

        if self._konfig.monatlich_aktiv and monatlich_dir.exists():
            monatlich_anzahl = len(_snapshots_nach_schluessel(monatlich_dir))
            if monatlich_anzahl > self._konfig.max_dateien:
                logger.warning(
                    "backup.monatlich_ueberlauf",
                    anzahl=monatlich_anzahl,
                    limit=self._konfig.max_dateien,
                )
        return geloescht


def snapshot_schluessel(pfad: Path) -> str:
    """Logischer Snapshot-Name ohne Kompressions-Suffix.

    `bewaesserung_2026-08-01.db` und `bewaesserung_2026-08-01.db.gz`
    bezeichnen denselben Tag und muessen in Rotation und Zaehlung als
    EIN Eintrag gelten.
    """
    name = pfad.name
    if name.endswith(KOMPRESS_SUFFIX):
        name = name[: -len(KOMPRESS_SUFFIX)]
    return name


def _snapshots_nach_schluessel(verzeichnis: Path) -> dict[str, list[Path]]:
    """Alle Snapshot-Dateien eines Verzeichnisses, gruppiert pro Tag/Monat.

    T-0475: sammelt BEIDE Ablageformen. Ein Glob auf `bewaesserung_*.db`
    allein wuerde nach der Umstellung auf `.db.gz` nichts mehr treffen —
    die Rotation liefe still leer, `retention_taeglich_tage` waere
    wirkungslos.
    """
    gruppen: dict[str, list[Path]] = {}
    if not verzeichnis.is_dir():
        return gruppen
    for muster in ("bewaesserung_*.db", f"bewaesserung_*.db{KOMPRESS_SUFFIX}"):
        for pfad in verzeichnis.glob(muster):
            if pfad.is_file():
                gruppen.setdefault(snapshot_schluessel(pfad), []).append(pfad)
    return gruppen


def _snapshot_vorhanden(verzeichnis: Path, basis_name: str) -> bool:
    """True, wenn der Snapshot in irgendeiner Ablageform existiert.

    Verhindert, dass beim Umstieg neben einem vorhandenen
    unkomprimierten Monats-Snapshot ein zweiter komprimierter entsteht.
    """
    if (verzeichnis / basis_name).exists():
        return True
    return (verzeichnis / (basis_name + KOMPRESS_SUFFIX)).exists()


def _kopiere_datei(quelle: Path, ziel: Path) -> None:
    """Kopiert Datei 1:1 — streamend, nicht ueber den RAM.

    T-0475: vorher `ziel.write_bytes(quelle.read_bytes())`, was die
    komplette DB (210 MB und wachsend) in den Speicher des laufenden
    Dienstes zog. `copyfileobj` arbeitet chunkweise mit konstantem
    Speicherbedarf. Schreibt nach `.tmp` und wechselt atomar, damit ein
    Abbruch keine halbe Datei hinterlaesst (die z. B. als "Monats-
    Snapshot existiert bereits" gezaehlt wuerde).

    Eigener Helfer statt direkt `shutil.copy`, damit Tests ohne
    Seiteneffekte mocken koennen.
    """
    tmp = ziel.with_name(ziel.name + ".tmp")
    try:
        with quelle.open("rb") as q, tmp.open("wb") as z:
            shutil.copyfileobj(q, z, _CHUNK)
        os.replace(str(tmp), str(ziel))
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _komprimiere_datei(quelle: Path, ziel: Path) -> None:
    """Schreibt `quelle` gzip-komprimiert nach `ziel` — streamend.

    Gleiche tmp+replace-Mechanik wie `_kopiere_datei`: ein abgebrochener
    Lauf darf kein halbes `.db.gz` hinterlassen, das spaeter wie ein
    gueltiger Snapshot aussieht.
    """
    tmp = ziel.with_name(ziel.name + ".tmp")
    try:
        with quelle.open("rb") as q, gzip.open(
            str(tmp), "wb", compresslevel=KOMPRESS_LEVEL,
        ) as z:
            shutil.copyfileobj(q, z, _CHUNK)
        os.replace(str(tmp), str(ziel))
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
