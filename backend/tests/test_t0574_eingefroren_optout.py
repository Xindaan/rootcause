"""T-0574: Opt-out fuer die "Sensor eingefroren"-Pruefung.

Anlass (09.09.2026, Andre): die Magerwiese trug eine offene Warnung
"50 Messungen in 48h, Spanne 0.00%" ueber einen Zustand, der laengst geprueft
und dokumentiert ist -- der Sensor liegt auf trockenem Sand unter der
Aufloesung seiner Skala und meldet dort konstant 0,0. Am 05.09. sprang
derselbe Sensor nach Regen 0 -> 30 und fiel zurueck, er misst also.

In dieser Totzone hat der Detektor keine Trennschaerfe: ein toter und ein
lebender Sensor sehen beide gleich aus. Aufbau bewusst identisch zu
`test_t0540_warn_drossel_und_optout.py` -- gleiche Klasse, zweiter Fall.
"""

from datetime import datetime

import pytest

T0 = datetime(2026, 9, 9, 8, 0, 0)


class _WarnSpeicher:
    def __init__(self):
        self.geschlossen: list[tuple] = []
        self.geoeffnet: list = []

    async def schliesse_sensor_warnung(self, zone_id, typ, jetzt):
        self.geschlossen.append((zone_id, typ))

    async def oeffne_sensor_warnung(self, warnung):
        self.geoeffnet.append(warnung)
        return True


def _detektor(aus=()):
    from bewaesserung.leck_detektor import LeckDetektor

    d = object.__new__(LeckDetektor)
    d._speicher = _WarnSpeicher()
    d._eingefroren_alarm_aus = frozenset(aus)
    return d


@pytest.mark.asyncio
async def test_optout_zone_wird_nicht_geprueft_und_warnung_geschlossen():
    from bewaesserung.modelle import SensorWarnungTyp

    d = _detektor(aus={"magerwiese"})
    await d._pruefe_sensor_eingefroren("magerwiese", T0)
    assert d._speicher.geschlossen == [
        ("magerwiese", SensorWarnungTyp.SENSOR_EINGEFROREN),
    ]
    assert d._speicher.geoeffnet == []


@pytest.mark.asyncio
async def test_negativprobe_ohne_optout_greift_das_gate_nicht():
    """Ohne Opt-out muss dieselbe Zone in die echte Pruefung laufen und dort
    am unvollstaendigen Stub scheitern. Ohne diesen Test waere "keine Warnung"
    auch dann erfuellt, wenn das Gate gar nichts tut."""
    d = _detektor(aus=())
    with pytest.raises(AttributeError):
        await d._pruefe_sensor_eingefroren("magerwiese", T0)
    assert d._speicher.geschlossen == [], (
        "Negativprobe: ohne Opt-out darf das Gate nicht greifen."
    )


@pytest.mark.asyncio
async def test_andere_zone_laeuft_weiter_in_die_pruefung():
    """Abdeckung darf nur fuer die eine Zone verschwinden."""
    d = _detektor(aus={"magerwiese"})
    with pytest.raises(AttributeError):
        await d._pruefe_sensor_eingefroren("hecke", T0)
    assert d._speicher.geschlossen == []


# --------------------------------------------------------------------------
# Config-Kette: YAML -> ZonenKonfig -> Detektor-Set
# (fehlerpattern_config_whitelist: neues Feld wird sonst stumm ignoriert)
# --------------------------------------------------------------------------

def test_default_ist_pruefen():
    from bewaesserung.modelle import ZonenKonfig

    assert ZonenKonfig(zone_id="x", name="X").eingefroren_alarm_aktiv is True


@pytest.mark.live_config
def test_yaml_wert_kommt_durch_die_whitelist():
    from bewaesserung.konfig import lade_konfig

    konfig = lade_konfig()
    zonen = {z.zone_id: z for z in konfig.zonen}
    assert zonen["magerwiese"].eingefroren_alarm_aktiv is False, (
        "config/default.yaml setzt eingefroren_alarm_aktiv: false fuer "
        "magerwiese -- kommt der Wert nicht an, wurde er still verschluckt."
    )
    assert zonen["hecke"].eingefroren_alarm_aktiv is True


@pytest.mark.live_config
def test_die_beiden_optouts_sind_unabhaengig():
    """bambuswald hat den WIRKUNGS-Alarm ab und muss weiterhin auf
    eingefrorene Sensoren geprueft werden; magerwiese umgekehrt. Ein
    gemeinsames Set haette beides vermischt."""
    from bewaesserung.konfig import lade_konfig

    zonen = {z.zone_id: z for z in lade_konfig().zonen}
    assert zonen["bambuswald"].wirkungs_alarm_aktiv is False
    assert zonen["bambuswald"].eingefroren_alarm_aktiv is True
    assert zonen["magerwiese"].wirkungs_alarm_aktiv is True
    assert zonen["magerwiese"].eingefroren_alarm_aktiv is False


@pytest.mark.live_config
def test_ableitung_aus_der_echten_konfig_ergibt_genau_magerwiese():
    from bewaesserung.konfig import lade_konfig

    aus = frozenset(
        z.zone_id for z in lade_konfig().zonen
        if not z.eingefroren_alarm_aktiv
    )
    assert aus == {"magerwiese"}, f"unerwartetes Opt-out-Set: {sorted(aus)}"


def test_main_reicht_das_set_an_den_detektor():
    """Naht-Test gegen `fehlerpattern_detektor_ohne_konsument`. Nur als
    ERGAENZUNG zu den Verhaltenstests oben -- ein Quelltext-Match beweist
    fuer sich genommen nichts."""
    from pathlib import Path

    quelle = (
        Path(__file__).resolve().parents[1]
        / "src" / "bewaesserung" / "main.py"
    ).read_text()
    assert "eingefroren_alarm_aus=frozenset(" in quelle
    assert "if not z.eingefroren_alarm_aktiv" in quelle
