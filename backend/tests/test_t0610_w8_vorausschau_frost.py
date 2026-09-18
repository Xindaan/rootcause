"""T-0610 E10: Begruendung bei vorgezogenem Bedarf (T-0578) unter einer
Sperre nennt die Vorausschau statt den Prognosewert als Messwert.

`pruefe_zone` ersetzt bei vorgezogenem Bedarf die Feuchte durch die Prognose
(bis zum naechsten Fenster). Die Sperr-Begruendungen sagten dann "Feuchte
33,3% unter Schwelle, aber Frostsperre ..." -- 33,3 ist aber die Prognose,
gemessen sind 45 %, und dass vorgezogen wurde, stand nirgends. Nur Text,
Entscheidung und Blocker bleiben gleich. Kanalpfad und Dashboard-Karte
(`vorhersage_zone`) nennen die Vorausschau schon (T-0578-Tests).
"""
from __future__ import annotations

import pytest

from bewaesserung.frostsperre import FrostBefund
from bewaesserung.modelle import BlockerTyp
from test_t0578_vorausschau import FALLEND, _einzel_motor

FROST = "Frostsperre: 1,4 °C um 06:00"


class _FrostAttrappe:
    async def pruefe(self, zone_ids, zyklus_s, jetzt=None):
        return FrostBefund(gesperrt=True, grund=FROST)


@pytest.mark.asyncio
async def test_e10_frostsperre_bei_vorgezogenem_bedarf_nennt_die_vorausschau():
    motor, zone = _einzel_motor(FALLEND)
    motor.setze_frost_pruefer(_FrostAttrappe())

    e = await motor.pruefe_zone(zone.zone_id)

    assert e.soll_bewaessern is False
    assert e.blocker_typ == BlockerTyp.FROSTSPERRE, e.begruendung
    assert "vorgezogen: jetzt 45%" in e.begruendung, e.begruendung
    assert FROST in e.begruendung
    assert "Feuchte 45% noch ueber Schwelle" in e.begruendung, e.begruendung


@pytest.mark.asyncio
async def test_e10_regensperre_bei_vorgezogenem_bedarf_nennt_die_vorausschau():
    """Isomorph zur Frostsperre: dieselbe Textstelle im Regen-Gate."""
    motor, zone = _einzel_motor(FALLEND)
    motor._regen_gate = lambda *a, **kw: (True, 5.0)

    e = await motor.pruefe_zone(zone.zone_id)

    assert e.blocker_typ == BlockerTyp.REGEN_ERWARTET, e.begruendung
    assert "vorgezogen: jetzt 45%" in e.begruendung, e.begruendung
    assert "Feuchte 45% noch ueber Schwelle" in e.begruendung, e.begruendung
