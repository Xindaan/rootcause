"""T-0627: Lokale rollende Budgets, reserviert VOR dem HTTP-Versuch.

Konservative Betriebsgrenzen, keine Abbildung einer zugesicherten Cloud-Quote.
Historische Anfragen vor Einfuehrung und fremde Clients sind nicht erfasst.
Schliessbefehle werden bereits vom Aufrufer ausgenommen (auch von Datei-I/O).
"""

import fcntl
import hashlib
import json
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog

logger = structlog.get_logger()
WOCHE_S = 7 * 86400
# Name, Fenster in Sekunden, Anzahl, nur WebSocket-Reservierungen
BUDGETS = (
    ("gesamt_10s", 10, 8, False),
    ("gesamt_tag", 86400, 80, False),
    ("gesamt_woche", WOCHE_S, 500, False),
    ("ws_stunde", 3600, 2, True),
    ("ws_tag", 86400, 24, True),
)


class GardenaKontingentErschoepft(RuntimeError):
    """Lokale Ablehnung, kein Cloud-429, kein SDK-Retry und keine Start-Queue."""

    def __init__(self, bis: float, grund: str):
        self.bis = bis
        self.grund = grund
        stand = datetime.fromtimestamp(bis, timezone.utc).astimezone().isoformat()
        super().__init__(f"Gardena-Anfrage lokal begrenzt bis {stand}: {grund}; keine Anfrage gesendet")


class GardenaApiKontingent:
    def __init__(self, client_id: str, pfad: Path | None = None):
        self.pfad = pfad
        self.identitaet = hashlib.sha256(client_id.encode()).hexdigest()
        self._versuche: list[list] = []
        self._lock = threading.Lock()

    def _reserviere(self, versuche: list, jetzt: float, websocket: bool) -> list:
        if not isinstance(versuche, list):
            raise ValueError("Ungueltige Versuchsliste")
        for eintrag in versuche:
            if (not isinstance(eintrag, list) or len(eintrag) != 2
                    or type(eintrag[0]) not in (int, float)
                    or not math.isfinite(eintrag[0]) or eintrag[0] < 0
                    or type(eintrag[1]) is not bool):
                raise ValueError("Ungueltiger Versuch")
        # Zukunftszeiten behalten: Rueckstellen der Uhr darf nichts freigeben.
        aktuell = sorted((e for e in versuche if e[0] > jetzt - WOCHE_S), key=lambda e: e[0])
        sperren = []
        for name, fenster, limit, nur_ws in BUDGETS:
            if nur_ws and not websocket:
                continue
            zeiten = [t for t, ws in aktuell if t > jetzt - fenster and (not nur_ws or ws)]
            if len(zeiten) >= limit:
                sperren.append((zeiten[-limit] + fenster, name))
        if sperren:
            bis, grund = max(sperren)
            raise GardenaKontingentErschoepft(bis, grund)
        return aktuell + [[jetzt, websocket]]

    def reserviere(self, *, websocket: bool) -> None:
        jetzt = time.time()
        try:
            if self.pfad is None:
                with self._lock:
                    self._versuche = self._reserviere(self._versuche, jetzt, websocket)
                return
            self.pfad.parent.mkdir(parents=True, exist_ok=True)
            # Separate Lock-Datei bleibt beim atomaren Ersetzen unveraendert.
            # Nicht warten: Konkurrenz oder I/O-Fehler bremsen nur Nicht-Closes.
            with self.pfad.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if self.pfad.exists():
                    daten = json.loads(self.pfad.read_text())
                    if (daten["version"] != 1 or daten["identitaet"] != self.identitaet):
                        raise ValueError("Fremdes oder ungueltiges Kontingent")
                    versuche = daten["versuche"]
                else:
                    versuche = []
                neu = self._reserviere(versuche, jetzt, websocket)
                temp = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", dir=self.pfad.parent,
                                                     prefix=self.pfad.name + ".", delete=False) as datei:
                        temp = Path(datei.name)
                        json.dump({"version": 1, "identitaet": self.identitaet, "versuche": neu}, datei)
                        datei.write("\n")
                        datei.flush()
                        os.fsync(datei.fileno())
                    temp.replace(self.pfad)
                finally:
                    if temp is not None:
                        temp.unlink(missing_ok=True)
        except GardenaKontingentErschoepft as exc:
            logger.warning("gardena.kontingent_pause", bis=exc.bis, grund=exc.grund)
            raise
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            logger.error("gardena.kontingent_dateifehler", fehler_typ=type(exc).__name__)
            raise GardenaKontingentErschoepft(jetzt + 60, "Kontingent nicht sicher les-/speicherbar") from exc
