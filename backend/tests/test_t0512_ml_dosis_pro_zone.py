"""T-0512: die ML-Dosis wird pro Zone freigeschaltet, nicht global.

Die Shadow-Reihe faellt pro Zone gegensaetzlich aus -- bei bambuswald ist das
ML deutlich besser als die Heuristik, bei hecke deutlich schlechter. Ein
globales `wirksam: true` haette hecke sehenden Auges verschlechtert. Deshalb
dieselbe dreistufige Mechanik wie bei der Ventilsteuerung: globaler Not-Aus
plus Zonen-Opt-in.
"""

import pytest

from bewaesserung.entscheidung import Entscheidungsmotor


class _Resp:
    def __init__(self, wirksam: bool):
        self.wirksam = wirksam
        self.aktiv = True


class _Zone:
    def __init__(self, opt_in: bool):
        self.zone_id = "z"
        self.ml_dosis_opt_in = opt_in


def _motor(wirksam: bool) -> Entscheidungsmotor:
    motor = object.__new__(Entscheidungsmotor)
    motor._response_konfig = _Resp(wirksam)
    return motor


@pytest.mark.parametrize("global_an,zone_an,erwartet", [
    (False, False, False),
    (False, True, False),   # Not-Aus schlaegt Opt-in
    (True, False, False),   # Default: eine Zone ist NICHT dabei
    (True, True, True),
])
def test_beide_stufen_muessen_zustimmen(global_an, zone_an, erwartet):
    assert _motor(global_an)._ml_dosis_wirksam(_Zone(zone_an)) is erwartet


def test_globales_true_allein_aendert_nichts():
    """DER Sicherheitsfall.

    Wer nur `wirksam: true` setzt und die Zonen vergisst, bekommt das alte
    Verhalten -- nicht ML auf allen Zonen. Der teure Irrtum waere die
    Gegenrichtung.
    """
    motor = _motor(True)
    assert motor._ml_dosis_wirksam(_Zone(False)) is False


def test_zone_ohne_das_feld_gilt_als_nicht_freigeschaltet():
    """Fremde/alte Zonenobjekte ohne das Attribut duerfen nicht durchrutschen."""
    class _Alt:
        zone_id = "alt"
    assert _motor(True)._ml_dosis_wirksam(_Alt()) is False
