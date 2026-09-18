"""Top-Level-Felder der Gesamtkonfig muessen aus der YAML ankommen.

Anlass (18.09.2026): `plant_optimum_intervall_stunden` war ein Feld von
`GesamtKonfig` und in der README dokumentiert, `lade_konfig` reichte es aber
nie durch -- ein gesetzter Wert wurde still ignoriert, es galt immer der
Modell-Default. Fehlerklasse "Config-Whitelist-Drift".
"""
from __future__ import annotations

from bewaesserung.konfig import lade_konfig
from bewaesserung.modelle import GesamtKonfig

_BASIS = "zonen: []\nwetter:\n  breite: 52.52\n  laenge: 13.40\n"


def test_plant_optimum_intervall_kommt_aus_der_yaml(tmp_path):
    pfad = tmp_path / "k.yaml"
    pfad.write_text(_BASIS + "plant_optimum_intervall_stunden: 6\n")
    assert lade_konfig(pfad).plant_optimum_intervall_stunden == 6


def test_plant_optimum_intervall_ohne_eintrag_bleibt_modell_default(tmp_path):
    pfad = tmp_path / "k.yaml"
    pfad.write_text(_BASIS)
    erwartet = GesamtKonfig.model_fields["plant_optimum_intervall_stunden"].default
    assert lade_konfig(pfad).plant_optimum_intervall_stunden == erwartet
