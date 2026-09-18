"""T-0575: Die Bilanz muss sagen, WELCHE ihrer beiden Seiten unsicher ist.

`indikativ` faltete zwei verschiedene Ursachen in ein Bool: eine
Bewaesserung ohne Literwert und ein Wetter aus der Vorhersage. Die Kachel
zeigte daraufhin bei JEDEM `indikativ` einen Tooltip "Wetter-Quelle: ..."
-- also regelmaessig die falsche Ursache, wenn die Unsicherheit von der
Bewaesserungsseite kam. Sichtbar war ohnehin nichts: ein `title`-Attribut
ist auf dem Touchscreen unerreichbar.

Die Anzeige-Seite dieser Aenderung prueft `frontend/tests/pruefe_bilanz_frische.mjs`
(fuehrt die echte TS-Funktion aus). Hier geht es um die Datenseite: liefert
das Backend die Unterscheidung ueberhaupt?

Fehlerklasse: [[fehlerpattern_detektor_ohne_konsument]] -- die Information
wurde berechnet und auf dem Weg zur Anzeige eingeebnet.
"""
import asyncio
from datetime import datetime

import pytest

from bewaesserung.bilanz import berechne_bilanz
from bewaesserung.modelle import (
    Ausloser,
    BilanzKonfig,
    VentilAktion,
    VentilEreignis,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "t0575.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _zone(**kwargs) -> ZonenKonfig:
    defaults = dict(
        zone_id="bambuswald", name="Bambuswald",
        modus=ZonenModus.AUTOMATIK, ventil_kanal=2,
        feuchte_schwelle_min=65.0, feuchte_kritisch=50.0,
        flaeche_m2=1.74, anteil_kanal=0.357,
    )
    defaults.update(kwargs)
    return ZonenKonfig(**defaults)


def _konfig() -> BilanzKonfig:
    return BilanzKonfig(
        kanal_liter_pro_minute={1: 6.0, 2: 1.87},
        manuell_liter_pro_minute=10.0,
    )


def _schliessen(speicher, **kwargs):
    felder = dict(
        zeitstempel=datetime(2026, 4, 15, 19, 0),
        zone_id="bambuswald", ventil_id="v1",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
        ausloser=Ausloser.AUTOMATIK,
    )
    felder.update(kwargs)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(**felder)))


def test_bewaesserung_mit_literwert_ist_nicht_indikativ(speicher):
    """Negativprobe: ein sauber bilanzierbarer Lauf darf nichts flaggen.

    Ohne diesen Fall koennte `bewaesserung_indikativ` fest auf True stehen
    und alle anderen Tests blieben gruen -- die Kachel wuerde dann jede
    Zone dauerhaft als geschaetzt markieren.
    """
    _schliessen(speicher)
    b = _run(berechne_bilanz(
        _zone(), datetime(2026, 4, 15), datetime(2026, 4, 17),
        speicher, _konfig(), "standort_a",
    ))
    assert b is not None
    assert b.bewaesserung_liter == pytest.approx(6.7, abs=0.1)
    assert b.bewaesserung_indikativ is False


def test_bewaesserung_ohne_literwert_setzt_eigenes_flag(speicher):
    """Der Fall, den die Kachel bisher dem Wetter angelastet hat.

    Eine Zone ohne Kanal und ohne Pumpen-Konfig kann die Menge nicht
    ableiten -- `ereignis_zu_liter` liefert None. Geprueft wird, dass das
    GETRENNT sichtbar wird, nicht nur im gemeinsamen `indikativ`.
    """
    zone = _zone(zone_id="pilea", name="Pilea", ventil_kanal=None)
    _schliessen(speicher, zone_id="pilea", ausloser=Ausloser.AUTOMATIK)
    b = _run(berechne_bilanz(
        zone, datetime(2026, 4, 15), datetime(2026, 4, 17),
        speicher, _konfig(), "standort_a",
    ))
    assert b is not None
    assert b.bewaesserung_indikativ is True
    # `indikativ` bleibt der gemeinsame Trigger -- der alte Vertrag gilt weiter.
    assert b.indikativ is True
    # Und die Wetterseite ist hier NICHT die Ursache. Genau diese
    # Unterscheidung fehlte: ohne sie sind beide Faelle ununterscheidbar.
    assert b.quelle_niederschlag == "keine"


def test_api_reicht_die_unterscheidung_durch(speicher):
    """Die NAHT, als Verhalten geprueft statt als Quelltext-Match.

    Ein Feld, das das Modell fuellt und die Response einebnet, aendert an
    der Kachel nichts -- genau so war es vorher. Geprueft wird deshalb das
    Dict, das wirklich rausgeht.
    """
    from bewaesserung.api_server import baue_bilanz_dict

    zone = _zone(zone_id="pilea", name="Pilea", ventil_kanal=None)
    _schliessen(speicher, zone_id="pilea")
    b = _run(berechne_bilanz(
        zone, datetime(2026, 4, 15), datetime(2026, 4, 17),
        speicher, _konfig(), "standort_a",
    ))
    d = baue_bilanz_dict(b, "24h")
    assert d["bewaesserung_indikativ"] is True
    assert d["indikativ"] is True
    # Der bestehende Vertrag bleibt unveraendert (T-0200-Linie: die
    # Response-Form ist zugesichert).
    for feld in ("zone_id", "fenster", "fenster_von", "fenster_bis",
                 "flaeche_m2", "bewaesserung_liter", "regen_liter",
                 "zugefuehrt_liter", "verdunstet_liter", "bilanz_liter",
                 "quelle_niederschlag", "indikativ"):
        assert feld in d, feld


def test_api_dict_markiert_sauberen_lauf_nicht(speicher):
    """Negativprobe zur Naht: sonst koennte das Feld fest True sein."""
    from bewaesserung.api_server import baue_bilanz_dict

    _schliessen(speicher)
    b = _run(berechne_bilanz(
        _zone(), datetime(2026, 4, 15), datetime(2026, 4, 17),
        speicher, _konfig(), "standort_a",
    ))
    assert baue_bilanz_dict(b, "24h")["bewaesserung_indikativ"] is False
