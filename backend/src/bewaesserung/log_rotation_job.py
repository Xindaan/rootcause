"""T-0480: groessenbasierte Rotation der Service-Logs, verlustfrei archiviert.

**Warum nicht `RotatingFileHandler`.** Der Task schlug ihn vor, aber er kann
hier nichts ausrichten: die Logdateien entstehen NICHT durch einen Python-
Handler, sondern durch launchd. `service/de.xindaan.pflanzen-dashboard.plist`
setzt `StandardOutPath`/`StandardErrorPath`, und der Kernel haengt jeden
stdout-Write des Prozesses an diese Datei an. Ein `RotatingFileHandler` im
Backend wuerde eine ZWEITE, parallele Datei schreiben -- die launchd-Dateien
waeren weiterhin unbegrenzt, nur haetten wir jetzt zwei Kopien derselben
Zeilen. Ausserdem faengt stdout auch das, was gar nicht ueber den Logger
laeuft: uvicorn-Startmeldungen, Tracebacks eines Absturzes, alles vor der
Logger-Initialisierung. Genau die braucht man bei einer Diagnose.

**Deshalb copytruncate.** Der laufende Prozess haelt einen offenen
File-Descriptor. Wuerde man die Datei umbenennen oder loeschen, schriebe er
stumm in die umbenannte bzw. geloeschte Inode weiter, und die neue Datei
bliebe fuer immer leer -- der klassische logrotate-Fallstrick, den das
Akzeptanzkriterium ausdruecklich nennt. Stattdessen wird der Inhalt
weggeschrieben und dieselbe Inode auf Laenge 0 gekuerzt. launchd oeffnet mit
`O_APPEND`, der naechste Write landet also sauber am (neuen) Dateianfang.
Kein Neustart noetig.

**Nichts wird verworfen.** `RotatingFileHandler` loescht die aelteste
Generation, sobald `backupCount` erreicht ist. Das waere hier falsch: Andres
Entscheidung vom 02.08. lautet "wer weiss, wofuer das mal gut ist fuer
Analyse". Rotierte Generationen wandern deshalb gzip-komprimiert nach
`logs/archiv/` und bleiben dort. Log-Text komprimiert grob Faktor 10-20, aus
147 MB werden rund 10 MB. Dieselbe Linie wie bei den Backups aus T-0475:
Platz ueber die Ablageform loesen, nie ueber den Inhalt
([[feedback_messdaten_vollstaendig_behalten]]).

**Restfenster, ehrlich benannt.** Zwischen dem Lesen des Rests und dem
`truncate` kann der Prozess theoretisch eine Zeile schreiben, die dann
verloren geht. Das Fenster liegt im Millisekundenbereich und laesst sich
ohne Kooperation des schreibenden Prozesses nicht schliessen (er muesste
seinen FD neu oeffnen). Alles, was vor dem Rotationsstart in der Datei
stand, ist dagegen garantiert im Archiv: es wird an der letzten
vollstaendigen Zeilengrenze geschnitten, der angebrochene Schwanz wandert
zurueck in die frische Datei.
"""
from __future__ import annotations

import asyncio
import gzip
import os
from datetime import datetime, timedelta
from pathlib import Path

import structlog

from bewaesserung.backup import _CHUNK, KOMPRESS_LEVEL

logger = structlog.get_logger()

# 20 MB pro Stream. Groessenbasiert statt zeitbasiert: das Wachstum ist zwar
# gleichmaessig (~2,5 MB/Tag), aber ein einzelner Fehler-Burst kippt das --
# und genau dann will man die Rotation haben.
LOG_MAX_BYTES = 20 * 1024 * 1024

# Stuendlich pruefen. Der Check ist ein `stat()` pro Datei und kostet nichts;
# die teure Arbeit passiert nur beim tatsaechlichen Ueberschreiten.
LOG_INTERVALL_STUNDEN = 1


class LogRotationJob:
    """Rotiert alle `*.log` eines Verzeichnisses ab `max_bytes`."""

    def __init__(
        self,
        log_verzeichnis: Path,
        max_bytes: int = LOG_MAX_BYTES,
        intervall_stunden: int = LOG_INTERVALL_STUNDEN,
    ) -> None:
        self._verzeichnis = Path(log_verzeichnis)
        self._max_bytes = max_bytes
        self._intervall = timedelta(hours=intervall_stunden)
        self._letzte_aktualisierung: datetime | None = None
        self.letzter_erfolg: datetime | None = None
        self.letzter_fehler: str | None = None
        self.letztes_ergebnis: dict | None = None

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> None:
        jetzt = jetzt or datetime.now()
        if (self._letzte_aktualisierung
                and jetzt - self._letzte_aktualisierung < self._intervall):
            return
        try:
            # T-0403: gzip ueber 147 MB ist CPU- und I/O-schwer. Der
            # Wartungsloop haengt zwar nicht mehr am Entscheidungstakt, aber
            # er teilt sich den Event-Loop mit dem API-Server -- also in
            # einen Thread, nicht in den Rumpf.
            ergebnis = await asyncio.to_thread(self._rotiere_alle, jetzt)
        except Exception as fehler:
            # `_letzte_aktualisierung` bleibt ungesetzt, damit der naechste
            # Zyklus es erneut versucht.
            self.letzter_fehler = str(fehler)
            raise
        self._letzte_aktualisierung = jetzt
        self.letzter_erfolg = jetzt
        self.letzter_fehler = None
        if ergebnis:
            self.letztes_ergebnis = ergebnis
            logger.info("log_rotation.rotiert", **ergebnis)

    def _rotiere_alle(self, jetzt: datetime) -> dict:
        if not self._verzeichnis.is_dir():
            return {}
        rotiert: dict[str, int] = {}
        for pfad in sorted(self._verzeichnis.glob("*.log")):
            try:
                if pfad.stat().st_size <= self._max_bytes:
                    continue
            except OSError:
                continue
            ziel = self._rotiere_datei(pfad, jetzt)
            if ziel is not None:
                rotiert[pfad.name] = ziel
        return rotiert

    def _rotiere_datei(self, pfad: Path, jetzt: datetime) -> int | None:
        """Archiviert den Inhalt und kuerzt die Datei auf 0.

        Gibt die Zahl der archivierten Bytes zurueck, oder None wenn nichts
        zu tun war. Schneidet an der letzten vollstaendigen Zeilengrenze --
        eine halbe Zeile im Archiv und die andere Haelfte in der frischen
        Datei waere beim Auswerten schlimmer als der Umbruch selbst.
        """
        archiv = self._verzeichnis / "archiv"
        archiv.mkdir(parents=True, exist_ok=True)
        stempel = jetzt.strftime("%Y%m%d-%H%M%S")
        ziel = archiv / f"{pfad.stem}-{stempel}.log.gz"
        tmp = ziel.with_name(ziel.name + ".tmp")

        with pfad.open("rb") as quelle:
            groesse = os.fstat(quelle.fileno()).st_size
            grenze = self._letzte_zeilengrenze(quelle, groesse)
            if grenze <= 0:
                # Eine einzige Zeile groesser als das Limit: nichts zu
                # schneiden, sonst wuerde sie zerrissen. Beim naechsten Lauf
                # erneut versuchen.
                return None
            quelle.seek(0)
            try:
                with gzip.open(
                    str(tmp), "wb", compresslevel=KOMPRESS_LEVEL,
                ) as z:
                    self._kopiere_bereich(quelle, z, grenze)
                os.replace(str(tmp), str(ziel))
            except BaseException:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            # Alles ab der Grenze ist noch nicht archiviert und muss die
            # Kuerzung ueberleben.
            quelle.seek(grenze)
            rest = quelle.read()

        # Dieselbe Inode kuerzen, NICHT ersetzen -- sonst schreibt der
        # laufende Prozess in die alte Inode weiter (siehe Modul-Docstring).
        with pfad.open("r+b") as f:
            f.truncate(0)
            if rest:
                f.write(rest)
        return grenze

    @staticmethod
    def _letzte_zeilengrenze(datei, groesse: int) -> int:
        """Position nach dem letzten `\\n` innerhalb von `groesse`."""
        fenster = min(_CHUNK, groesse)
        if fenster <= 0:
            return 0
        datei.seek(groesse - fenster)
        block = datei.read(fenster)
        pos = block.rfind(b"\n")
        if pos < 0:
            return 0
        return groesse - fenster + pos + 1

    @staticmethod
    def _kopiere_bereich(quelle, ziel, bis: int) -> None:
        """Streamt `bis` Bytes ab der aktuellen Position, chunkweise."""
        offen = bis
        while offen > 0:
            block = quelle.read(min(_CHUNK, offen))
            if not block:
                break
            ziel.write(block)
            offen -= len(block)


__all__ = ["LogRotationJob", "LOG_MAX_BYTES", "LOG_INTERVALL_STUNDEN"]
