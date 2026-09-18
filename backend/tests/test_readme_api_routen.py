"""Jede `/api/...`-Route, die eine README nennt, muss in der App registriert sein.

Anlass (18.09.2026): die API-Tabelle nannte `POST /api/manuell/start`. Die
Route hiess laengst `/api/ventil/manuell-start`, und Pre-Soak lief ueber eine
eigene Route -- wer der README folgte, bekam 404. Geprueft wird gegen die
REGISTRIERTEN Routen der App, nicht gegen eine Textsuche im Quellcode.

Beide READMEs: `README.md` (im oeffentlichen Snapshot ist das der Override)
und, falls vorhanden, `tools/public/README.md` (nur privat).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from bewaesserung.api_server import app

WURZEL = Path(__file__).resolve().parents[2]
READMES = [p for p in (WURZEL / "README.md", WURZEL / "tools" / "public" / "README.md") if p.exists()]

# `/api/...` in Backticks; Pfadparameter in beiden Schreibweisen ({id}, <id>).
_ROUTE = re.compile(r"`(/api/[A-Za-z0-9_/{}<>.-]+)")


def _normiere(pfad: str) -> str:
    pfad = pfad.split("?")[0].rstrip("/.")
    return re.sub(r"\{[^}]+\}|<[^>]+>", "{}", pfad)


def _registriert() -> set[str]:
    return {_normiere(r.path) for r in app.routes if getattr(r, "path", "").startswith("/api/")}


@pytest.mark.parametrize("readme", READMES, ids=lambda p: str(p.relative_to(WURZEL)))
def test_readme_nennt_nur_registrierte_routen(readme):
    genannt = {_normiere(r) for r in _ROUTE.findall(readme.read_text(encoding="utf-8"))}
    # Untergrenze gegen den still leerlaufenden Waechter (Regex kaputt,
    # Tabelle umformatiert): die README nennt Dutzende Routen.
    assert len(genannt) >= 20, f"nur {len(genannt)} Routen gefunden -- Regex pruefen"
    fehlend = sorted(genannt - _registriert())
    assert not fehlend, f"{readme.name} nennt nicht registrierte Routen: {fehlend}"
