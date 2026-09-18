"""T-0619-Restpunkt: jedes Feld von `MlRetrainKonfig` muss aus der YAML ankommen.

Der Bestandstest `test_ml_retrain_jeder_yaml_schluessel_kommt_an` prueft nur
die Schluessel, die in `config/default.yaml` stehen. `gate_gleiches_fenster`
und `gate_vergleich_mindest_zeilen` stehen dort nicht (Modell-Default gilt),
also fiel ihre fehlende Whitelist-Zeile in `konfig.py` nicht auf, obwohl die
README sie als Schluessel nennt. Dieser Test setzt JEDES Modellfeld auf einen
Nicht-Default-Wert und prueft, dass er ankommt (Fehlerklasse
"Config-Whitelist-Drift"); ein kuenftiges Feld ohne Loader-Zeile wird rot.
"""
from __future__ import annotations

import yaml

from bewaesserung.konfig import lade_konfig
from bewaesserung.modelle import MlRetrainKonfig

_BASIS = {"zonen": [], "wetter": {"breite": 52.52, "laenge": 13.40}}


def _anderer_wert(name: str, default):
    if name == "cluster_strategie":
        return "pro_zone" if default == "global" else "global"
    if isinstance(default, bool):
        return not default
    if isinstance(default, int):
        return default + 1
    if isinstance(default, float):
        return round(default + 0.25, 2)
    if isinstance(default, dict):
        return {"bambuswald": 1.25}
    if isinstance(default, str):
        return default + "_x"
    raise AssertionError(f"Feld {name}: Typ {type(default)} im Test nachtragen")


def test_jedes_retrain_feld_kommt_aus_der_yaml(tmp_path):
    werte = {
        name: _anderer_wert(name, feld.default)
        for name, feld in MlRetrainKonfig.model_fields.items()
        if name != "ausgabe_pfad"  # Loader macht daraus einen absoluten Pfad
    }
    pfad = tmp_path / "k.yaml"
    pfad.write_text(yaml.safe_dump({**_BASIS, "ml": {"retrain": werte}}))

    geladen = lade_konfig(pfad).ml_retrain

    falsch = {
        name: (wert, getattr(geladen, name))
        for name, wert in werte.items() if getattr(geladen, name) != wert
    }
    assert falsch == {}, f"YAML-Wert kommt nicht an (Whitelist-Zeile fehlt): {falsch}"
