"""T-0535 Option A (Entscheid Andre 04.11.2026): feste Regeldosis je Ventil.

Grenzen, die hier gesichert werden: Parser (fehlt / gueltig / ungueltig),
scharfer Pfad `_dauer_mit_ml_weiche` (Regeldosis, keine Vorschlagszeile,
Rangfolge hinter der Teststufe) und Anzeige `vorhersage_zone`
(gleiche Zahl wie der scharfe Pfad, `liter_haupt` folgt ihr).
"""
from __future__ import annotations

from datetime import date

import pytest

from bewaesserung.dosis_test import RegeldosisFest
from bewaesserung.entscheidung import Entscheidungsmotor
from bewaesserung.konfig import lade_konfig
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    MlBewaesserungsResponseKonfig,
    WetterKonfig,
)

from test_dosis_test import (
    GERAET,
    JETZT_VZ,
    SpeicherAttrappe,
    WetterManagerAttrappe,
    _anzeige_motor,
    _anzeige_zone,
    _dauer,
    _konfig,
    _zone,
)

REGEL = RegeldosisFest(
    ventil_geraet_id=GERAET, ventil_kanal=2, gesamt_min=75,
    seit=date(2026, 11, 4), grund="Test",
)
# (75 - 5 min Pre-Soak) * 60
ERWARTET_S = 4200


def _gesamt(*, regel=(REGEL,), dosis=None) -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        # Der Validator verlangt eine Zone am Regeldosis-Ventil.
        zonen=[_zone("bambuswald")],
        wetter=WetterKonfig(breite=52.52, laenge=13.40),
        dosis_test=dosis if dosis is not None else _konfig(aktiv=False),
        regeldosis_fest=regel,
    )


def _motor(speicher, zone, **kw) -> Entscheidungsmotor:
    return Entscheidungsmotor(
        speicher, WetterManagerAttrappe(), [zone],
        response_konfig=MlBewaesserungsResponseKonfig(aktiv=True),
        konfig=_gesamt(**kw),
    )


# --- Parser ---------------------------------------------------------------

_KOPF = "wetter:\n  breite: 52.52\n  laenge: 13.40\n"
_ZONE = (
    "zonen:\n"
    "  - zone_id: bambus\n    name: Bambus\n    modus: automatik\n"
    f"    ventil_geraet_id: \"{GERAET}\"\n    ventil_kanal: 2\n"
)
_EINTRAG = (
    f"  - ventil_geraet_id: \"{GERAET}\"\n    ventil_kanal: 2\n"
    "    gesamt_min: 75\n    seit: \"2026-11-04\"\n    grund: Beleg\n"
)


def _lade(tmp_path, block: str):
    pfad = tmp_path / "k.yaml"
    pfad.write_text(_KOPF + _ZONE + block, encoding="utf-8")
    return lade_konfig(pfad)


def test_parser_block_fehlt_heisst_keine_regeldosis(tmp_path):
    assert _lade(tmp_path, "").regeldosis_fest == ()


def test_parser_gueltiger_eintrag_kommt_an(tmp_path):
    k = _lade(tmp_path, "regeldosis_fest:\n" + _EINTRAG)
    assert k.regeldosis_fest == (RegeldosisFest(
        ventil_geraet_id=GERAET, ventil_kanal=2, gesamt_min=75,
        seit=date(2026, 11, 4), grund="Beleg",
    ),)


@pytest.mark.parametrize("block", [
    # fehlender Schluessel
    _EINTRAG.replace("    grund: Beleg\n", ""),
    # Tippfehler im Schluessel
    _EINTRAG.replace("gesamt_min", "gesamt_minuten"),
    # keine positive Zahl
    _EINTRAG.replace("gesamt_min: 75", "gesamt_min: 0"),
    _EINTRAG.replace("gesamt_min: 75", "gesamt_min: true"),
    # dasselbe Ventil doppelt
    _EINTRAG + _EINTRAG,
    # Ventil ohne Zone (Tippfehler in der Geraet-ID)
    _EINTRAG.replace(GERAET, "tippfehler"),
], ids=["fehlt", "unbekannt", "null", "bool", "doppelt", "ventil_ohne_zone"])
def test_parser_lehnt_ungueltige_eintraege_laut_ab(tmp_path, block):
    with pytest.raises(ValueError, match="regeldosis_fest"):
        _lade(tmp_path, "regeldosis_fest:\n" + block)


# --- Scharfer Pfad --------------------------------------------------------

@pytest.mark.asyncio
async def test_regeldosis_gilt_am_kanal_und_schreibt_keinen_vorschlag():
    """Geschwister-Zone am selben Ventil: Kanal, nicht Zone. Keine
    ml_dauer_vorschlag-Zeile, sonst bewertete das Audit eine Dosis, die
    nicht lief (T-0513)."""
    zone = _zone("bambuswald_yogaraum")
    sp = SpeicherAttrappe()
    assert await _dauer(_motor(sp, zone), zone) == ERWARTET_S
    assert sp.vorschlaege == []


@pytest.mark.asyncio
async def test_fremdes_ventil_rechnet_weiter_frei():
    fremd = _zone("hecke", kanal=1)
    mit = await _dauer(_motor(SpeicherAttrappe(), fremd), fremd)
    ohne = await _dauer(_motor(SpeicherAttrappe(), fremd, regel=()), fremd)
    assert mit == ohne
    assert mit != ERWARTET_S


@pytest.mark.asyncio
async def test_aktive_teststufe_schlaegt_regeldosis():
    zone = _zone("bambuswald")
    sp = SpeicherAttrappe()
    dauer = await _dauer(_motor(sp, zone, dosis=_konfig()), zone)
    assert dauer in (40 * 60, 55 * 60)  # erste Stufe ist nicht die 75er
    assert dauer != ERWARTET_S


# --- Anzeige --------------------------------------------------------------

@pytest.mark.asyncio
async def test_anzeige_zeigt_regeldosis_und_liter_folgen_ihr():
    zone = _anzeige_zone()
    motor, _ = _anzeige_motor(zone, _konfig(aktiv=False))
    motor._konfig = _gesamt()
    empf = await motor.vorhersage_zone(zone.zone_id)
    scharf = await motor._dauer_mit_ml_weiche(
        zone, JETZT_VZ, aktuelle_feuchte=40.0, et0_6h=1.0, ziel_schwelle=70.0,
    )

    assert empf.dauer_s_regeldosis == scharf == ERWARTET_S
    assert empf.dauer_s_dosis_test is None
    assert empf.dauer_s_fest == ERWARTET_S
    assert empf.liter_haupt == motor._liter_fuer_dauer(zone, ERWARTET_S)
    # Nicht trivial: die Heuristik liegt woanders und bleibt unberuehrt.
    assert empf.dauer_s_heuristik != ERWARTET_S
    assert empf.liter_haupt != empf.liter_heuristik
