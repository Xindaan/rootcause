"""T-0610 EL-2 (Andres Entscheid Q2, 09.10.2026): eine kritisch trockene Zone
umgeht den Konvektions-/Starkregen-Guard (T-0322) in allen drei Kaskaden.

Der Guard sperrt bei >= 80 % Regenwahrscheinlichkeit in 6 h auch ohne
mm-Prognose. Er stand bisher VOR der Kritisch-Pruefung, also blieb eine Zone
unter `feuchte_kritisch` bei 0,6 mm Prognose trocken. Kritisch heisst hier
dasselbe wie im Loop: `effektiv_feuchte_kritisch` am Messwert (T-0560/T-0578).

Je Kaskade zwei Faelle: kritisch -> Guard umgangen; nicht kritisch -> Guard
sperrt weiter. Der zweite Fall haelt die Umgehung eng (ein `or True` oder ein
Vertauschen der Bedingung faellt auf). Fuer `pruefe_zone` und
`vorhersage_zone` liegt die Gegenprobe schon in `test_entscheidung.py`
(`test_t0322_starkregen_wahrscheinlich_bewaessert_nicht`) bzw.
`test_vorhersage_zone.py` (`test_t0560_konvektions_guard_greift_auch_im_panel`);
`pruefe_kanal` hatte gar keinen Test fuer den Guard.
"""
from __future__ import annotations

import pytest

from bewaesserung.modelle import BlockerTyp
from test_entscheidung import (
    WetterClientAttrappe,
    WetterManagerAttrappe,
    baue_kanal_motor,
    baue_kanal_zonen,
    baue_motor,
    baue_vorhersage,
    zone_automatik,  # noqa: F401  (Fixture)
)
from test_vorhersage_zone import _motor, _zone

# 0,1 mm/h -> 0,6 mm in 6 h: unter der mm-Schwelle des Regen-Gates (T-0439),
# allein die Wahrscheinlichkeit entscheidet.
_KONVEKTION = dict(regen_pro_stunde=0.1, regen_wahrscheinlichkeit=90.0)


@pytest.mark.asyncio
async def test_pruefe_zone_kritisch_umgeht_konvektions_guard(zone_automatik):  # noqa: F811
    # kritisch 20, Messwert 15 -> kritisch
    motor, _ = baue_motor(
        zone_automatik, feuchte=15.0, vorhersage=baue_vorhersage(**_KONVEKTION),
    )

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is True, entscheidung.begruendung
    assert entscheidung.blocker_typ is None
    assert "Starkregen" not in entscheidung.begruendung


def _kanal_motor_mit_konvektion(feuchte_a: float, feuchte_b: float):
    zone_a, zone_b = baue_kanal_zonen()  # kritisch 20, Schwelle 35
    motor, _ = baue_kanal_motor(zone_a, zone_b, feuchte_a, feuchte_b)
    motor._wetter_manager = WetterManagerAttrappe(
        WetterClientAttrappe(baue_vorhersage(**_KONVEKTION)),
    )
    return motor, [zone_a, zone_b]


@pytest.mark.asyncio
async def test_pruefe_kanal_kritisch_umgeht_konvektions_guard():
    motor, zonen = _kanal_motor_mit_konvektion(15.0, 30.0)

    entscheidung = await motor.pruefe_kanal(1, zonen)

    assert entscheidung.soll_bewaessern is True, entscheidung.begruendung
    assert entscheidung.blocker_typ is None
    assert "Starkregen" not in entscheidung.begruendung


@pytest.mark.asyncio
async def test_pruefe_kanal_nicht_kritisch_bleibt_vom_guard_gesperrt():
    motor, zonen = _kanal_motor_mit_konvektion(28.0, 30.0)

    entscheidung = await motor.pruefe_kanal(1, zonen)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.REGEN_ERWARTET
    assert "Starkregen" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_vorhersage_zone_kritisch_umgeht_konvektions_guard():
    zone = _zone(ventil_kanal=1, optimum_min=40.0)  # kritisch 20
    motor, _ = _motor(
        zone, feuchte=15.0, vorhersage=baue_vorhersage(**_KONVEKTION),
    )

    empf = await motor.vorhersage_zone("zone-1")

    assert empf.blocker_typ is None, empf.grund
    assert empf.soll_bewaessern is True
    assert "Starkregen" not in (empf.grund or "")
