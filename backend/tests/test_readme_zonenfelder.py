"""T-0579: jedes Feld von `ZonenKonfig` steht in der README.

Anlass: beim Public-Sync 0.4.0 standen 30 von 72 Zonen-Feldern in keiner
README. Die Luecke wuchs still, weil nichts anschlug, wenn ein Feld dazukam.
Dieser Test prueft die KLASSE: jedes Feld, das `ZonenKonfig` kennt, muss als
`feldname` (in Backticks, wie in der Konfig-Referenz) vorkommen.

Geprueft werden `README.md` und, im privaten Repo, zusaetzlich
`tools/public/README.md` -- das ist die Datei, die im oeffentlichen Ableger
zu `README.md` wird. Im Ableger gibt es kein `tools/`; dort ist `README.md`
bereits die oeffentliche Fassung, also genuegt die eine Pruefung.

Was dieser Test NICHT prueft: ob die Beschreibung stimmt. Er faengt das
Vergessen, nicht den falschen Satz.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bewaesserung.modelle import ZonenKonfig

WURZEL = Path(__file__).resolve().parents[2]

READMES = [WURZEL / "README.md"]
if (WURZEL / "tools" / "public" / "README.md").exists():
    READMES.append(WURZEL / "tools" / "public" / "README.md")


def _steht_drin(feld: str, text: str) -> bool:
    """Zwei Formen zaehlen als dokumentiert: der Name als eigenes Wort innerhalb
    von Backticks (`feld`, auch in einer Formel wie `feld + 10`) oder als
    YAML-Schluessel am Zeilenanfang eines Beispielblocks (`feld: 48  # ...`).
    `\b` behandelt `_` als Wortzeichen -- `zone_id` in `zone_ids` zaehlt also
    nicht (fehlerpattern_wortgrenze_scrub_false_negative, andere Richtung)."""
    wort = rf"\b{re.escape(feld)}\b"
    return bool(
        re.search(rf"`[^`\n]*{wort}[^`\n]*`", text)
        or re.search(rf"^\s*-?\s*{re.escape(feld)}:", text, re.M)
    )


def _fehlende_felder(text: str) -> list[str]:
    return sorted(
        feld for feld in ZonenKonfig.model_fields if not _steht_drin(feld, text)
    )


@pytest.mark.parametrize("readme", READMES, ids=lambda p: str(p.relative_to(WURZEL)))
def test_jedes_zonenfeld_steht_in_der_readme(readme):
    fehlend = _fehlende_felder(readme.read_text(encoding="utf-8"))
    assert not fehlend, (
        f"{readme.relative_to(WURZEL)}: {len(fehlend)} ZonenKonfig-Feld(er) "
        f"ohne Eintrag in der Konfig-Referenz: {fehlend}"
    )


def test_pruefung_erkennt_ein_fehlendes_feld():
    """Negativprobe im Test selbst: ein Text ohne ein Feld wird gemeldet,
    und ein Feld nur als Wortteil (`zone_id` in `zone_ids`) zaehlt nicht."""
    alle = " ".join(f"`{f}`" for f in ZonenKonfig.model_fields)
    ohne = alle.replace("`min_pause_minuten`", "`min_pause_minuten_alt`")
    assert _fehlende_felder(alle) == []
    assert _fehlende_felder(ohne) == ["min_pause_minuten"]
