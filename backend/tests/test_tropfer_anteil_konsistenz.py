"""T-0641: `anteil_kanal` muss zu `tropfer_anzahl` / Tropfer-Gesamtzahl passen.

Beide Werte spiegeln dieselbe Hardware (Tropfer am Mikrodrip-Strang). Wird
nur einer nachgezogen, teilt die Bilanz das Kanalwasser still falsch auf --
deshalb bricht das Laden ab.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from bewaesserung.konfig import lade_konfig
from bewaesserung.modelle import (
    BilanzKonfig,
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent.parent / "config" / "default.yaml"
OEFFENTLICHER_SNAPSHOT = not DEFAULT_CONFIG.exists()
GERAET = "geraet-a"


def _zone(zone_id: str, tropfer: int | None, anteil: float, kanal: int = 2) -> ZonenKonfig:
    return ZonenKonfig(
        zone_id=zone_id, name=zone_id, sensor_geraet_id=f"s-{zone_id}",
        ventil_geraet_id=GERAET, ventil_kanal=kanal,
        tropfer_anzahl=tropfer, anteil_kanal=anteil,
    )


def _konfig(*zonen: ZonenKonfig, gesamt: dict | None = None) -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=list(zonen),
        wetter=WetterKonfig(standorte=[WetterStandortKonfig(id="a", breite=52.5, laenge=13.4)]),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        bilanz=BilanzKonfig(geraet_kanal_tropfer_anzahl=gesamt if gesamt is not None else {GERAET: {2: 70}}),
    )


def test_passender_anteil_laedt():
    _konfig(_zone("a", 25, 0.357), _zone("b", 17, 0.243))


def test_anteil_nicht_nachgezogen_bricht_das_laden_ab():
    # Ein Tropfer nachgesteckt (25 -> 26), Anteil auf altem Stand 0.357:
    # Abweichung 1/70 = 0.014, der kleinste reale Pflegefehler.
    with pytest.raises(ValidationError, match=r"a: anteil_kanal 0.357 passt nicht zu 26/70"):
        _konfig(_zone("a", 26, 0.357), _zone("b", 17, 0.243))


def test_summe_ueber_gesamtzahl_bricht_das_laden_ab():
    with pytest.raises(ValidationError, match=r"Summe tropfer_anzahl 80 > Gesamtzahl 70"):
        _konfig(_zone("a", 50, 50 / 70), _zone("b", 30, 30 / 70))
    # Grenzfall: alle Tropfer verteilt ist zulaessig.
    _konfig(_zone("a", 40, 40 / 70), _zone("b", 30, 30 / 70))


def test_gesamtzahl_null_ist_ladefehler_statt_division_durch_null():
    with pytest.raises(ValidationError, match="Tropfer-Gesamtzahl 0 <= 0"):
        _konfig(_zone("a", 25, 0.357), gesamt={GERAET: {2: 0}})


@pytest.mark.skipif(OEFFENTLICHER_SNAPSHOT, reason="oeffentlicher Snapshot: config/default.yaml privat")
def test_bruchzahl_als_gesamtzahl_wird_beim_laden_abgelehnt(tmp_path):
    # Ueber den Parser: 70.5 darf nicht still zu 70 abgeschnitten werden.
    roh = yaml.safe_load(DEFAULT_CONFIG.read_text())
    tabelle = roh["bilanz"]["geraet_kanal_tropfer_anzahl"]
    geraet = next(iter(tabelle))
    tabelle[geraet] = {2: 70.5}
    pfad = tmp_path / "konfig.yaml"
    pfad.write_text(yaml.safe_dump(roh))
    with pytest.raises(ValidationError):
        lade_konfig(pfad)


def test_unbekanntes_ventil_bricht_das_laden_ab():
    with pytest.raises(ValidationError, match="kein Zonen-Ventil"):
        _konfig(_zone("a", 25, 0.357), gesamt={GERAET: {1: 70}})


def test_ohne_gesamtzahl_oder_tropferangabe_keine_pruefung():
    # Kein Eintrag fuer das Ventil -> frei wie bisher.
    _konfig(_zone("a", 30, 0.357), gesamt={})
    # Zone ohne tropfer_anzahl am geprueften Ventil -> ihr Anteil bleibt frei.
    _konfig(_zone("a", None, 0.9), _zone("b", 17, 0.243))


@pytest.mark.skipif(OEFFENTLICHER_SNAPSHOT, reason="oeffentlicher Snapshot: config/default.yaml privat")
def test_ausgelieferte_config_bambus_strang_konsistent():
    k = lade_konfig(DEFAULT_CONFIG)
    # Ohne Geraete-UUID: die Datei geht in den oeffentlichen Snapshot.
    assert list(k.bilanz.geraet_kanal_tropfer_anzahl.values()) == [{2: 70}]
