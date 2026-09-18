"""T-0353: Tests fuer den Shadow-Prognose-Router."""
from __future__ import annotations

from datetime import datetime

from bewaesserung.ml.forecast_router import route_prognose
from bewaesserung.modelle import GiessEmpfehlung, MlForecastRoutingKonfig


def _empf(ml=True, physik=False, statespace=False) -> GiessEmpfehlung:
    return GiessEmpfehlung(
        zone_id="z1", zeitstempel=datetime(2026, 7, 1, 12, 0),
        soll_bewaessern=False, grund="Test",
        prognose_6h=48.0 if ml else None,
        prognose_physik_6h=47.0 if physik else None,
        prognose_statespace_6h=46.0 if statespace else None,
    )


def test_inaktiv_liefert_none():
    routing = MlForecastRoutingKonfig(aktiv=False, zonen={"z1": "statespace"})
    assert route_prognose("z1", routing, _empf(statespace=True)) is None


def test_wunsch_quelle_verfuegbar():
    routing = MlForecastRoutingKonfig(aktiv=True, zonen={"z1": "statespace"})
    assert route_prognose(
        "z1", routing, _empf(statespace=True, physik=True),
    ) == "statespace"


def test_fallback_kette_statespace_zu_physik_zu_ml():
    routing = MlForecastRoutingKonfig(aktiv=True, zonen={"z1": "statespace"})
    assert route_prognose(
        "z1", routing, _empf(physik=True),
    ) == "physik(statt statespace)"
    assert route_prognose(
        "z1", routing, _empf(),
    ) == "ml(statt statespace)"


def test_default_quelle_fuer_unbekannte_zone():
    routing = MlForecastRoutingKonfig(aktiv=True, default_quelle="ml")
    assert route_prognose("fremd", routing, _empf()) == "ml"


def test_keine_quelle_verfuegbar():
    routing = MlForecastRoutingKonfig(aktiv=True, zonen={"z1": "ml"})
    leer = _empf(ml=False)
    assert route_prognose("z1", routing, leer) is None
