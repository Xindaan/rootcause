"""T-0477: Retention fuer die Versionsverzeichnisse der Response-Modelle.

Ausgangslage (gemessen 01.08.2026): `backend/daten/ml/response/` hielt 980
Versionsverzeichnisse `v<YYYYMMDD_HHMMSS>/` in 516 MB, Wachstum ~12 MB/Tag.
Es gab keine Retention -- jeder Retrain-Lauf legte ein Verzeichnis an, auch
wenn das Deploy-Gate (T-0048) das Modell danach ablehnte. Nur 8 Versionen
waren je in einer gespeicherten Entscheidung referenziert.

Der Feuchte-Pfad loest dasselbe Problem seit T-0219 mit `_ARCHIV_RETENTION`
in `retrain_job.py` (juengste N behalten, Rest loeschen). Dieses Modul ist
das fehlende Gegenstueck fuer den Response-Pfad -- mit einer zusaetzlichen
Sicherung, weil hier Versionen in Entscheidungen referenziert sind.

Schutzmenge (Vereinigung, IMMER dynamisch berechnet -- nie eine
eingefrorene Versionsliste, sonst loescht der naechste Retrain das gerade
aktivierte Modell):

  (a) alle Versionen, die fuer diese Zone in
      `ml_dauer_vorschlag.ml_modell_version` stehen. Ohne sie ist "warum
      hat es damals so vorgeschlagen" nicht mehr beantwortbar.
  (b) alle Ziele der `aktuell_*`-Symlinks der Zone. Das Ziel wird
      AUFGELOEST, nicht der Name verglichen.
  (c) die N juengsten Versionen der Zone als Puffer, damit ein parallel
      laufender Retrain und ein Rollback nach Gate-Ablehnung nicht ins
      Leere greifen.

Konservativ im Zweifel: findet dieses Modul einen defekten oder aus dem
Zonenverzeichnis herauszeigenden `aktuell_*`-Symlink, raeumt es die Zone
GAR NICHT auf. Ein unklarer Zustand ist kein Loeschgrund.

Warum der automatische Pfad gedeckelt ist (`MAX_ENTFERNUNGEN_PRO_LAUF`):
Er ist fuer den Dauerbetrieb gedacht -- pro Retrain entsteht ein
Verzeichnis, also muss pro Retrain hoechstens eine Handvoll verschwinden.
Die einmalige Altlast (~972 Verzeichnisse) laeuft bewusst NICHT hier
durch, sondern ueber `tools/bereinige_response_modelle.py`, das in eine
Quarantaene verschiebt statt zu loeschen und damit umkehrbar bleibt. Der
Deckel macht den automatischen Pfad unfaehig, diese Massenoperation still
und unumkehrbar vorwegzunehmen.

Das Modul kommt bewusst mit der stdlib aus (kein pandas/lightgbm), damit
`tools/bereinige_response_modelle.py` dieselbe Schutzmengen-Logik
importieren kann statt sie zu duplizieren (Single Source of Truth).

Es nutzt stdlib `logging` -- in Regression-Tests `caplog.set_level(INFO)`
setzen (fehlerpattern_stdlib_structlog_mix.md).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Verzeichnisnamen, die eine Modellversion sind. Alles andere im
# Zonenverzeichnis (Symlinks, Staging-Dateien, fremde Ordner) ist tabu.
VERSION_MUSTER = re.compile(r"^v\d{8}_\d{6}$")

# Puffer: so viele der juengsten Versionen bleiben pro Zone immer liegen,
# unabhaengig von DB-Referenz und Symlink.
STANDARD_N_JUENGSTE = 5

# Deckel fuer den automatischen Pfad, siehe Modul-Docstring.
MAX_ENTFERNUNGEN_PRO_LAUF = 5

AKTUELL_PRAEFIX = "aktuell_"


@dataclass
class ZonenBefund:
    """Was in einer Zone geschuetzt ist und was entfernt werden koennte."""

    zone_id: str
    alle_versionen: list[str] = field(default_factory=list)
    referenziert: set[str] = field(default_factory=set)
    symlink_ziele: set[str] = field(default_factory=set)
    juengste: set[str] = field(default_factory=set)
    geschuetzt: set[str] = field(default_factory=set)
    # Aufsteigend sortiert: aelteste Kandidaten zuerst.
    kandidaten: list[str] = field(default_factory=list)
    # `aktuell_*`-Symlinks, deren Ziel nicht existiert.
    defekte_symlinks: list[str] = field(default_factory=list)
    # `aktuell_*`-Symlinks, die nicht auf `<zone>/v.../datei` zeigen.
    fremde_symlinks: list[str] = field(default_factory=list)

    @property
    def aufraeumbar(self) -> bool:
        """False, wenn der Symlink-Zustand unklar ist -- dann nichts tun."""
        return not self.defekte_symlinks and not self.fremde_symlinks

    @property
    def blockade_grund(self) -> str | None:
        if self.defekte_symlinks:
            return f"defekte_symlinks:{','.join(sorted(self.defekte_symlinks))}"
        if self.fremde_symlinks:
            return f"fremde_symlinks:{','.join(sorted(self.fremde_symlinks))}"
        return None


def liste_versionen(zone_dir: Path) -> list[str]:
    """Alle Versionsverzeichnisse der Zone, aufsteigend sortiert.

    Der Name `v<YYYYMMDD_HHMMSS>` sortiert lexikografisch == chronologisch.
    Symlinks werden ausgeschlossen: sie sind nie ein Versionsverzeichnis,
    und ein Symlink-Ziel darf nie ueber seinen Linknamen geloescht werden.
    """
    if not zone_dir.is_dir():
        return []
    namen = [
        p.name
        for p in zone_dir.iterdir()
        if not p.is_symlink() and p.is_dir() and VERSION_MUSTER.match(p.name)
    ]
    return sorted(namen)


def lies_symlink_ziele(zone_dir: Path) -> tuple[set[str], list[str], list[str]]:
    """Loest die `aktuell_*`-Symlinks der Zone auf.

    Rueckgabe: (`versionen`, `defekte_links`, `fremde_links`).

    Der Vergleich laeuft ueber den aufgeloesten Pfad, nicht ueber den
    Linknamen -- der Linkname enthaelt die Zone, aber nicht die Version.
    """
    ziele: set[str] = set()
    defekt: list[str] = []
    fremd: list[str] = []
    if not zone_dir.is_dir():
        return ziele, defekt, fremd
    basis = zone_dir.resolve()
    for link in sorted(zone_dir.iterdir()):
        if not link.name.startswith(AKTUELL_PRAEFIX):
            continue
        if not link.is_symlink():
            # Echte Datei statt Link: zeigt auf nichts, schuetzt nichts.
            continue
        roh = Path(os.readlink(link))
        ziel = roh if roh.is_absolute() else (zone_dir / roh)
        ziel = Path(os.path.normpath(str(ziel)))
        if not ziel.exists():
            defekt.append(link.name)
            continue
        try:
            rel = ziel.resolve().relative_to(basis)
        except ValueError:
            fremd.append(link.name)
            continue
        if len(rel.parts) < 2 or not VERSION_MUSTER.match(rel.parts[0]):
            fremd.append(link.name)
            continue
        ziele.add(rel.parts[0])
    return ziele, defekt, fremd


def bewerte_zone(
    zone_dir: Path,
    zone_id: str,
    referenzierte: Iterable[str] = (),
    n_juengste: int = STANDARD_N_JUENGSTE,
) -> ZonenBefund:
    """Berechnet Schutzmenge und Entfernungs-Kandidaten einer Zone.

    `referenzierte` sind die Versionen, die fuer GENAU DIESE Zone in
    `ml_dauer_vorschlag` stehen. Der Aufrufer muss sicherstellen, dass die
    Liste vollstaendig ist -- eine leere Liste heisst hier "nichts
    referenziert", nicht "unbekannt". Wer die DB nicht lesen konnte, darf
    diese Funktion nicht zum Loeschen benutzen.
    """
    befund = ZonenBefund(zone_id=zone_id)
    befund.alle_versionen = liste_versionen(zone_dir)
    ziele, defekt, fremd = lies_symlink_ziele(zone_dir)
    befund.symlink_ziele = ziele
    befund.defekte_symlinks = defekt
    befund.fremde_symlinks = fremd

    # (a) DB-Referenzen -- auch solche, deren Verzeichnis schon fehlt,
    #     bleiben in der Schutzmenge (sie schaden nicht und machen den
    #     Fehlbestand in der Ausgabe sichtbar).
    befund.referenziert = {str(v) for v in referenzierte if v}
    # (c) Puffer
    n = max(0, int(n_juengste))
    befund.juengste = set(befund.alle_versionen[-n:]) if n else set()

    befund.geschuetzt = (
        befund.referenziert | befund.symlink_ziele | befund.juengste
    )
    befund.kandidaten = [
        v for v in befund.alle_versionen if v not in befund.geschuetzt
    ]
    return befund


def raeume_zone_auf(
    zone_dir: Path,
    zone_id: str,
    referenzierte: Iterable[str] = (),
    n_juengste: int = STANDARD_N_JUENGSTE,
    max_entfernungen: int = MAX_ENTFERNUNGEN_PRO_LAUF,
) -> dict:
    """Entfernt alte Versionsverzeichnisse einer Zone. Best-Effort.

    Loescht die AELTESTEN Kandidaten zuerst und hoechstens
    `max_entfernungen` Stueck pro Aufruf (Begruendung im Modul-Docstring).
    Fehler beim Loeschen werden geloggt, nicht propagiert -- der Retrain
    selbst war erfolgreich und darf daran nicht scheitern.

    Rueckgabe: Kennzahlen fuer Log und `/api/ml/status`.
    """
    befund = bewerte_zone(zone_dir, zone_id, referenzierte, n_juengste)
    bericht = {
        "zone_id": zone_id,
        "versionen_vorher": len(befund.alle_versionen),
        "geschuetzt": len(befund.geschuetzt & set(befund.alle_versionen)),
        "kandidaten": len(befund.kandidaten),
        "entfernt": 0,
        "uebersprungen": None,
    }
    if not befund.aufraeumbar:
        bericht["uebersprungen"] = befund.blockade_grund
        logger.warning(
            "ml.response_retention.uebersprungen zone=%s grund=%s",
            zone_id, befund.blockade_grund,
        )
        return bericht
    if not befund.kandidaten:
        return bericht

    for version in befund.kandidaten[: max(0, int(max_entfernungen))]:
        pfad = zone_dir / version
        try:
            shutil.rmtree(pfad)
        except OSError:
            logger.exception(
                "ml.response_retention.loeschfehler zone=%s version=%s",
                zone_id, version,
            )
            continue
        bericht["entfernt"] += 1
    if bericht["entfernt"]:
        logger.info(
            "ml.response_retention.aufgeraeumt zone=%s entfernt=%d "
            "verbleibend=%d geschuetzt=%d",
            zone_id, bericht["entfernt"],
            len(befund.alle_versionen) - bericht["entfernt"],
            bericht["geschuetzt"],
        )
    return bericht
