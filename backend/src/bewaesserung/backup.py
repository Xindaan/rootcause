"""Periodischer DB-Backup-Job mit Rotation (T-0043).

Wofuer:
  Schuetzt die SQLite-DB gegen Datei-Korruption, fehlgeschlagene
  Migrationen, versehentlichem Loeschen und SD-Kartenfehler (Pi-Umzug).
  Time-Machine allein reicht nicht, weil Snapshot-Zeiten nicht
  kontrollierbar sind und bei offener DB inkonsistente Dateien liefern.

Layout:
  <verzeichnis>/taeglich/   bewaesserung_YYYY-MM-DD.db   (letzte N Tage)
  <verzeichnis>/monatlich/  bewaesserung_YYYY-MM.db      (erster je Monat, dauerhaft)

Trigger:
  Intervall-Gate (Default 24h) analog zu WetterArchivJob. Wird aus dem
  Entscheidungs-Loop in main.py aufgerufen.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import structlog

from bewaesserung.modelle import BackupKonfig
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()


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

        taeglich_name = f"bewaesserung_{jetzt:%Y-%m-%d}.db"
        taeglich_pfad = taeglich_dir / taeglich_name

        await self._speicher.backup(taeglich_pfad)

        monatlich_erstellt = False
        if self._konfig.monatlich_aktiv:
            monatlich_name = f"bewaesserung_{jetzt:%Y-%m}.db"
            monatlich_pfad = monatlich_dir / monatlich_name
            if not monatlich_pfad.exists():
                # Den frischen Taeglich-Snapshot kopieren — kein zweiter
                # Datenbank-Zugriff noetig.
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
                monatlich_name = f"bewaesserung_{jetzt:%Y-%m}.db"
                spiegel_monatlich_pfad = spiegel_monatlich / monatlich_name
                if monatlich_erstellt or not spiegel_monatlich_pfad.exists():
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
        """
        geloescht = 0
        taeglich = sorted(
            (p for p in taeglich_dir.glob("bewaesserung_*.db")),
            key=lambda p: p.name,
            reverse=True,
        )
        behalten = self._konfig.retention_taeglich_tage
        for alt in taeglich[behalten:]:
            try:
                alt.unlink()
                geloescht += 1
            except OSError:
                logger.exception("backup.rotation_fehler", datei=str(alt))

        if self._konfig.monatlich_aktiv and monatlich_dir.exists():
            monatlich_anzahl = sum(1 for _ in monatlich_dir.glob("bewaesserung_*.db"))
            if monatlich_anzahl > self._konfig.max_dateien:
                logger.warning(
                    "backup.monatlich_ueberlauf",
                    anzahl=monatlich_anzahl,
                    limit=self._konfig.max_dateien,
                )
        return geloescht


def _kopiere_datei(quelle: Path, ziel: Path) -> None:
    """Kopiert Datei 1:1. Eigener Helfer statt shutil, damit wir in Tests
    ohne Seiteneffekte mocken koennen."""
    ziel.write_bytes(quelle.read_bytes())
