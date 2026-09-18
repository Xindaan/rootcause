"""T-0478: Referenz-Schutz fuer die Archiv-Ordner der Feuchte-Modelle.

Ausgangslage (gemessen 01.08.2026): `_raeume_alte_archive` in
`retrain_job.py` behaelt pro `ml/feuchte/<cluster>_archiv/` nur die fuenf
juengsten Datums-Ordner (T-0219) und kennt keine Referenzen. Von den 1.473
in `ml_vorhersage_log.modell_version` referenzierten Versionen waren
dadurch bereits 879 (60 %) nicht mehr auffindbar. Der Verlust ist
eingetreten; dieses Modul stoppt den WEITEREN Verlust.

Die Schutzmenge ist ENTSCHEIDUNGSGEBUNDEN (Entscheidung Andre 01.08.,
Option 2). Geschuetzt wird nur, was an einem Tag aktiv war, an dem
DIESELBE Zone eine echte Dosier-Entscheidung getroffen hat:

  (a) `ml_vorhersage_log.modell_version` aller Zeilen, deren `zone_id`
      am selben Kalendertag in `ml_dauer_vorschlag` eine Entscheidung
      hat,
  (b) die aufgeloesten Ziele der `aktuell_*`-Symlinks des Live-
      Verzeichnisses,
  (c) die N juengsten Archiv-Ordner je Verzeichnis (Puffer, N =
      bisheriges `_ARCHIV_RETENTION`).

**Die `zone_id`-Bedingung in (a) ist nicht optional.** Nur 4 der 14 Zonen
in `ml_vorhersage_log` treffen ueberhaupt Dosier-Entscheidungen; die
uebrigen zehn prognostizieren, loesen aber nie Wasser aus. Ohne die
Bedingung steigt die Schutzquote von 26 % auf 95 % -- das waere ein
Fuer-immer-Archiv mit dem Etikett "Retention". Gemessen 02.08.2026:
387 geschuetzte Versionen zonengenau gegen 1.398 ohne Zonenbezug.

Das Zeitfenster ist bewusst ein Kalendertag-Vergleich und kein
+/- 2-h-Fenster: der Unterschied betraegt 13 % (387 gegen 336), dafuer
lohnt kein Fensterjoin mit Randunschaerfen.

Warum die Zuordnung Version -> Archiv-Ordner ueber den INHALT laeuft und
nicht ueber den Ordnernamen: ein Archiv-Ordner heisst nach dem
Swap-Zeitpunkt (`YYYY-MM-DD_HHMMSS`), die Modelldateien darin tragen ihr
TRAININGS-Datum. Beispiel live: `hecke_archiv/2026-07-29_111009/` enthaelt
`modell_24h_q50_2026-07-26.lgbm`. Ein Abgleich ueber den Ordnernamen
wuerde also systematisch danebengreifen.

Die Version in der DB ist `<verzeichnis>/<dateiname>` (Pro-Cluster-Pfad)
oder nur `<dateiname>` (Legacy-Wurzelverzeichnis vor T-0082, 12 Versionen
aus dem April 2026). Prefixlose Versionen lassen sich keinem Verzeichnis
zuordnen und schuetzen deshalb ihren Dateinamen in JEDEM Verzeichnis --
ein unklarer Zustand ist kein Loeschgrund.

Das Modul kommt bewusst mit der stdlib aus (kein pandas/lightgbm), damit
`tools/feuchte_modellkette_stichtag.py` dieselbe Logik importieren kann
statt sie zu duplizieren (Single Source of Truth).
"""
from __future__ import annotations

import os
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

# Praefix der Symlinks, die auf das jeweils aktive Modell zeigen.
AKTUELL_PRAEFIX = "aktuell_"


@dataclass(frozen=True)
class Schutzmenge:
    """Geschuetzte Modell-DATEINAMEN, aufgeschluesselt nach Verzeichnis.

    `pro_verzeichnis` bildet den Verzeichnisnamen unter `ml/feuchte/`
    (== `cluster_id`) auf die dort geschuetzten Dateinamen ab.
    `ueberall` sind Dateinamen aus prefixlosen Legacy-Versionen, die in
    jedem Verzeichnis geschuetzt sind (siehe Modul-Docstring).
    """

    pro_verzeichnis: Mapping[str, frozenset[str]] = field(
        default_factory=dict,
    )
    ueberall: frozenset[str] = frozenset()

    def fuer(self, verzeichnis: str | None) -> frozenset[str]:
        """Geschuetzte Dateinamen fuer ein Verzeichnis.

        `verzeichnis=None` ist das Legacy-Wurzelverzeichnis: dort greifen
        nur die prefixlosen Versionen.
        """
        if verzeichnis is None:
            return self.ueberall
        return frozenset(
            self.pro_verzeichnis.get(verzeichnis, frozenset()) | self.ueberall
        )

    @property
    def anzahl_versionen(self) -> int:
        """Anzahl geschuetzter (Verzeichnis, Datei)-Kombinationen."""
        return sum(len(v) for v in self.pro_verzeichnis.values()) + len(
            self.ueberall
        )


def zerlege_version(version: str) -> tuple[str | None, str]:
    """`'hecke/modell_6h_q50_2026-07-29.lgbm'` -> `('hecke', 'modell_...')`.

    Ohne `/` (Legacy-Wurzelverzeichnis) ist das Verzeichnis `None`.
    """
    version = version.strip()
    if "/" not in version:
        return None, version
    verzeichnis, _, datei = version.rpartition("/")
    # Bei `a/b/c.lgbm` ist das unmittelbar enthaltende Verzeichnis
    # massgeblich -- das ist der Ordner unter `ml/feuchte/`.
    return verzeichnis.rsplit("/", 1)[-1] or None, datei


def baue_schutzmenge(
    referenzen: Mapping[str, Iterable[str]],
) -> Schutzmenge:
    """Baut die Schutzmenge aus den DB-Referenzen.

    `referenzen` ist `zone_id -> Versionsstrings`, wie
    `Speicher.hole_entscheidungsgebundene_feuchte_versionen` es liefert.
    Der Verzeichnis-Schluessel kommt aus dem Praefix des Versionsstrings,
    NICHT aus der `zone_id`: der Praefix benennt das Verzeichnis, in dem
    die Datei wirklich liegt. Bei `cluster_id != zone_id` (mehrere Zonen
    teilen ein Modell) ist die `zone_id` der falsche Schluessel.

    Eine leere Abbildung heisst hier "nichts referenziert", nicht
    "unbekannt". Wer die DB nicht lesen konnte, darf das Ergebnis nicht
    zum Loeschen benutzen -- siehe `_raeume_alte_archive(... , None)`.
    """
    pro_verzeichnis: dict[str, set[str]] = {}
    ueberall: set[str] = set()
    for versionen in referenzen.values():
        for version in versionen:
            if not version:
                continue
            verzeichnis, datei = zerlege_version(str(version))
            if not datei:
                continue
            if verzeichnis is None:
                ueberall.add(datei)
            else:
                pro_verzeichnis.setdefault(verzeichnis, set()).add(datei)
    return Schutzmenge(
        pro_verzeichnis={k: frozenset(v) for k, v in pro_verzeichnis.items()},
        ueberall=frozenset(ueberall),
    )


def lies_live_ziele(live_verzeichnis: Path) -> frozenset[str]:
    """Dateinamen, auf die die `aktuell_*`-Symlinks zeigen (Schutz (b)).

    Das Ziel wird AUFGELOEST, nicht der Linkname verglichen -- der
    Linkname traegt den Horizont, nicht die Version. Muss VOR dem
    Archiv-Swap gerufen werden: danach ist das Live-Verzeichnis
    umbenannt.
    """
    ziele: set[str] = set()
    try:
        if not live_verzeichnis.is_dir():
            return frozenset()
        eintraege = sorted(live_verzeichnis.iterdir())
    except OSError:
        return frozenset()
    for eintrag in eintraege:
        if not eintrag.name.startswith(AKTUELL_PRAEFIX):
            continue
        try:
            if not eintrag.is_symlink():
                continue
            ziele.add(os.path.basename(os.readlink(str(eintrag))))
        except OSError:
            continue
    return frozenset(z for z in ziele if z)


def ordner_ist_geschuetzt(
    ordner: Path, geschuetzte_dateien: Collection[str],
) -> bool:
    """True, wenn `ordner` mindestens eine geschuetzte Datei enthaelt.

    Abgleich ueber den INHALT, nicht ueber den Ordnernamen (Begruendung
    im Modul-Docstring). Ist der Ordner nicht lesbar, gilt er als
    geschuetzt: was man nicht pruefen kann, loescht man nicht.
    """
    if not geschuetzte_dateien:
        return False
    try:
        return any(p.name in geschuetzte_dateien for p in ordner.iterdir())
    except OSError:
        return True


def waehle_loeschbar(
    archiv_basis: Path,
    behalten: int,
    geschuetzte_dateien: Collection[str],
) -> list[Path]:
    """Archiv-Ordner, die weder unter die N juengsten fallen noch eine
    referenzierte Modelldatei enthalten.

    Die Ordnernamen sind `YYYY-MM-DD_HHMMSS`, daher ist lexikografische
    Sortierung == chronologisch.
    """
    if behalten < 1:
        return []
    try:
        if not archiv_basis.is_dir():
            return []
        versionen = sorted(
            (p for p in archiv_basis.iterdir() if p.is_dir()),
            key=lambda p: p.name,
        )
    except OSError:
        return []
    return [
        alt
        for alt in versionen[:-behalten]
        if not ordner_ist_geschuetzt(alt, geschuetzte_dateien)
    ]
