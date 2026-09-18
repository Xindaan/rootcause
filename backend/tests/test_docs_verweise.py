"""Produktivcode zitiert `docs/`-Dateien als Begruendung -- die muessen existieren.

Der Anlass: `docs/analyse/` sieht aus wie Ablage, ist aber an mehreren
Stellen die Begruendung fuer einen laufenden Produktionswert.
`ml/training.py` schreibt neben `num_threads: 1` ausdruecklich "Nachmessen mit
`docs/analyse/t0518_num_threads/benchmark.py`, nicht schaetzen"; `speicher.py`
und `modelle.py` stuetzen eine Schwelle auf `t0502_null_trajektorie_datenlage.md`.

Wer in `docs/analyse/` aufraeumt, entfernt damit die Begruendung von aktivem
Code -- und heute schlaegt nichts an. Dieser Test schlaegt an.

Er prueft die KLASSE, nicht die drei bekannten Faelle: jeder `docs/...`-Pfad
mit Dateiendung, der irgendwo im Produktivcode auftaucht, muss existieren.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[2]

# Im oeffentlichen Snapshot ist docs/ bewusst nicht enthalten. Erkannt wird
# der Snapshot am fehlenden `config/default.yaml` -- NICHT am fehlenden
# docs/: sonst wuerde genau das Aufraeumen von
# docs/analyse/, das dieser Test fangen soll, ihn still abschalten.
if not (WURZEL / "config" / "default.yaml").exists():
    pytest.skip("oeffentlicher Snapshot: docs/ ist privat", allow_module_level=True)

# Wo nach Verweisen gesucht wird. Bewusst ohne `backend/tests` und `docs`
# selbst: in Tests sind erfundene Pfade legitime Testdaten (siehe AUSNAHMEN),
# und Querverweise zwischen Analysen sind keine Code-Begruendung.
QUELL_VERZEICHNISSE = (
    "backend/src",
    "frontend/src",
    "tools",
    "scripts",
    "backend/skripte",
    "config",
)

# Dateien, in denen ein `docs/`-Pfad Testdatum ist und kein Verweis.
# `tools/test_sync_allowlist.sh:63` prueft mit `docs/interner_bericht.md`, dass
# `ist_privat()` einen solchen Pfad als privat einstuft -- die Datei soll es
# gerade NICHT geben.
AUSNAHMEN = ("test_sync_allowlist.sh", "test_leak_markers.sh")

# Kompilate und Binaeres tragen keine Verweise, die ein Mensch gepflegt haette.
UEBERSPRINGE_SUFFIX = (".pyc", ".png", ".svg", ".zip", ".lock")

VERWEIS = re.compile(r"docs/[A-Za-z0-9_./-]+\.(?:md|py|json|csv|tsv|txt|mjs|html|ya?ml)")

# Untergrenze gegen den still leerlaufenden Waechter: findet der Scanner
# ploetzlich fast nichts mehr (Verzeichnis umbenannt, Regex kaputt), ist der
# Test gruen, ohne irgendetwas geprueft zu haben. Stand 16.09.2026: 17 echte
# Verweise. Die Grenze ist bewusst niedrig -- sie faengt den Totalausfall,
# nicht jede einzelne geloeschte Zeile.
MIN_ERWARTETE_VERWEISE = 10


def _sammle_verweise() -> dict[str, list[str]]:
    """Alle `docs/...`-Pfade aus dem Produktivcode, je Ziel die Fundstellen."""
    treffer: dict[str, list[str]] = {}
    for verzeichnis in QUELL_VERZEICHNISSE:
        wurzel = WURZEL / verzeichnis
        if not wurzel.exists():
            continue
        for pfad in wurzel.rglob("*"):
            if not pfad.is_file() or pfad.suffix in UEBERSPRINGE_SUFFIX:
                continue
            if "__pycache__" in pfad.parts or "node_modules" in pfad.parts:
                continue
            if pfad.name in AUSNAHMEN:
                continue
            try:
                text = pfad.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for ziel in set(VERWEIS.findall(text)):
                treffer.setdefault(ziel, []).append(
                    str(pfad.relative_to(WURZEL))
                )
    return treffer


def test_scanner_findet_ueberhaupt_verweise() -> None:
    """Ohne diesen Test waere ein leerer Scan stillschweigend 'alles gruen'."""
    verweise = _sammle_verweise()
    assert len(verweise) >= MIN_ERWARTETE_VERWEISE, (
        f"Nur {len(verweise)} docs-Verweise gefunden, erwartet mindestens "
        f"{MIN_ERWARTETE_VERWEISE}. Wahrscheinlich ist der Scanner kaputt "
        f"(Verzeichnis umbenannt, Regex getroffen), nicht das Repo sauber."
    )


def test_zitierte_docs_existieren() -> None:
    """Jede aus Produktivcode zitierte `docs/`-Datei muss vorhanden sein."""
    fehlend = {
        ziel: quellen
        for ziel, quellen in sorted(_sammle_verweise().items())
        if not (WURZEL / ziel).exists()
    }
    if fehlend:
        zeilen = [
            f"  {ziel}\n      zitiert in: {', '.join(sorted(quellen))}"
            for ziel, quellen in fehlend.items()
        ]
        pytest.fail(
            "Produktivcode zitiert docs-Dateien, die es nicht gibt.\n"
            "Entweder die Datei ist geloescht worden (dann fehlt jetzt die\n"
            "Begruendung fuer laufenden Code) oder der Verweis ist ein Tippfehler:\n"
            + "\n".join(zeilen)
        )
