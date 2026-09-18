"""T-0545: die Aufloesungs-Untergrenze las eine nie befuellte Quelle.

`LeckDetektor.__init__` nimmt seit T-0428 einen Parameter `zone_quellen`.
**Keine einzige Stelle im Repo hat ihn je uebergeben** -- main.py nicht, kein
Test, kein Skript. `_mindest_erwartung_pp` fragte damit ein leeres Dict und
bekam fuer jede Zone `None` zurueck, also den konservativen Gardena-Default:
5 pp Raster mal Sicherheitsfaktor 2 = 10 pp.

Fuer die elf FYTA-Zonen ist die richtige Grenze 2 pp. Der Detektor hat dort
also seit Juli praktisch jede Auswertung als "unter Sensor-Aufloesung"
verworfen -- er war nicht falsch, sondern stumm.

Zwei andere Aufrufer derselben Funktion im selben Modul leiten die Quelle
seit jeher aus der `geraet_id` ab. Es war also nie eine Wissensluecke,
sondern eine unverbundene Naht ([[fehlerpattern_detektor_ohne_konsument]]).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from bewaesserung.leck_detektor import (
    AUFLOESUNG_SICHERHEITSFAKTOR,
    LeckDetektor,
    _wirkungs_paar_gleiches_geraet,
    aufloesungs_min_pp,
)


@dataclass
class _M:
    zeitstempel: datetime
    geraet_id: str
    boden_feuchte: float | None


T0 = datetime(2026, 8, 22, 8, 0)


def _detektor(zone_quellen=None):
    d = object.__new__(LeckDetektor)
    d._zone_quellen = dict(zone_quellen or {})
    return d


# --------------------------------------------------------------------------
# Der Kern: die Grenze folgt dem Geraet
# --------------------------------------------------------------------------

def test_fyta_geraet_bekommt_die_feine_grenze():
    d = _detektor()
    assert d._mindest_erwartung_pp("pilea", "fyta_900006") == 2.0


def test_gardena_geraet_bekommt_die_grobe_grenze():
    d = _detektor()
    uuid = "aaaa0002-0000-0000-0000-000000000000"
    assert d._mindest_erwartung_pp("bambuswald", uuid) == 10.0


def test_ohne_geraet_bleibt_es_konservativ():
    """Fail-safe: kein Geraet bekannt -> lieber schweigen als falsch
    alarmieren."""
    d = _detektor()
    assert d._mindest_erwartung_pp("pilea") == 10.0


def test_zone_quellen_bleibt_als_uebersteuerung_erhalten():
    """Der Parameter wird nicht entfernt, nur entwertet als ALLEINIGE
    Quelle -- ein Aufrufer darf die Ableitung weiterhin uebersteuern."""
    d = _detektor({"pilea": "fyta"})
    assert d._mindest_erwartung_pp("pilea") == 2.0


def test_geraet_schlaegt_zonen_mapping():
    """Bei gemischten Zonen entscheidet der Sensor, der das Delta geliefert
    hat -- nicht eine Zuordnung, die nur eine Bauart kennen kann."""
    d = _detektor({"hecke": "fyta"})
    uuid = "aaaaaaaa-0000-0000-0000-000000000000"
    assert d._mindest_erwartung_pp("hecke", uuid) == 10.0


# --------------------------------------------------------------------------
# Die Naht: das Geraet kommt aus der Paarung
# --------------------------------------------------------------------------

def test_paarung_liefert_das_geraet_mit():
    vor = [_M(T0, "fyta_1", 50.0), _M(T0, "uuid-2", 70.0)]
    nach = [_M(T0 + timedelta(hours=1), "fyta_1", 62.0)]
    v, n, g = _wirkungs_paar_gleiches_geraet(vor, nach)
    assert (v, n, g) == (50.0, 62.0, "fyta_1")


def test_paarung_ohne_treffer_gibt_dreimal_none():
    vor = [_M(T0, "fyta_1", 50.0)]
    nach = [_M(T0 + timedelta(hours=1), "uuid-9", 62.0)]
    assert _wirkungs_paar_gleiches_geraet(vor, nach) == (None, None, None)


def test_gemischte_zone_die_grenze_folgt_dem_gewinner():
    """hecke hat einen Gardena UND einen FYTA. Gewinnt der FYTA das Paar,
    muss auch seine feine Grenze gelten."""
    vor = [_M(T0, "fyta_9", 40.0), _M(T0, "uuid-a", 55.0)]
    nach = [_M(T0 + timedelta(hours=1), "fyta_9", 44.0)]
    _v, _n, g = _wirkungs_paar_gleiches_geraet(vor, nach)
    assert _detektor()._mindest_erwartung_pp("hecke", g) == 2.0


# --------------------------------------------------------------------------
# Negativprobe
# --------------------------------------------------------------------------

def test_negativprobe_alter_pfad_liefert_fuer_alle_zonen_dasselbe():
    """Baut den alten Zustand nach: leeres `zone_quellen`, kein Geraet. Dann
    bekommt eine FYTA-Zone dieselbe grobe Grenze wie eine Gardena-Zone -- und
    genau das war der Defekt."""
    d = _detektor()
    grob = d._mindest_erwartung_pp("pilea")
    assert grob == d._mindest_erwartung_pp("bambuswald")
    assert grob == aufloesungs_min_pp(None) == 5.0 * AUFLOESUNG_SICHERHEITSFAKTOR


# T-0610 AT-3 (14.10.2026): die Verdrahtung in main.ausfuehren prueft jetzt ein
# echter Start-Lauf, test_t0610_at11_start_lauf.py::test_leck_detektor_bekommt_die_alarm_abwahl_der_zonen
# (der Dienst baut den LeckDetektor nur dort; vorher Textsuche ueber alle Module).
