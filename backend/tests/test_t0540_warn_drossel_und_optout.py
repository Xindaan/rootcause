"""T-0540: Warnungen fuer dokumentierte Dauerzustaende.

Zwei Massnahmen, eine Entscheidung (Andre, 22.08.2026):

1. **Drosseln als Regel.** Die drei Meldungen aus `_robuste_feuchte` erzeugen
   zusammen ueber 90.000 Log-Eintraege, praktisch alle fuer einen bekannten
   Zustand. Gedrosselt wird die MELDUNG, nicht das Verhalten darunter.
2. **Stummschalten nur dort, wo die Meldung sachlich falsch ist.** Der
   Leck-Detektor erwartet von `aaaa0002` (bambuswald) eine Reaktion, die
   dieser Sensor an seiner Position nicht liefern kann.

Die Tests fuehren den Code aus und pruefen das Ergebnis; die Negativprobe je
Block baut den alten Zustand nach und weist nach, dass der Test dann anschlaegt
(globale Regel "Ein Test ohne Negativprobe ist kein Test").
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import structlog

from bewaesserung.warn_drossel import DROSSEL_INTERVALL_DEFAULT, WarnDrossel


T0 = datetime(2026, 8, 22, 8, 0, 0)


# --------------------------------------------------------------------------
# 1. Die Drossel selbst
# --------------------------------------------------------------------------

def test_erste_meldung_kommt_immer_durch():
    d = WarnDrossel()
    assert d.faellig("zone_a", T0) is True


def test_zweite_meldung_im_intervall_wird_geschluckt():
    d = WarnDrossel()
    d.faellig("zone_a", T0)
    assert d.faellig("zone_a", T0 + timedelta(minutes=5)) is False
    assert d.faellig("zone_a", T0 + timedelta(hours=23, minutes=59)) is False


def test_nach_ablauf_wieder_faellig():
    d = WarnDrossel()
    d.faellig("zone_a", T0)
    assert d.faellig("zone_a", T0 + timedelta(hours=24)) is True


def test_intervall_laeuft_ab_der_MELDUNG_nicht_ab_dem_versuch():
    """Sonst schoebe jeder unterdrueckte Versuch die naechste Meldung nach
    hinten -- bei 5-Minuten-Takt kaeme dann nie wieder eine."""
    d = WarnDrossel()
    d.faellig("zone_a", T0)
    for minute in range(5, 24 * 60, 5):
        d.faellig("zone_a", T0 + timedelta(minutes=minute))
    assert d.faellig("zone_a", T0 + timedelta(hours=24)) is True


def test_zonen_drosseln_sich_nicht_gegenseitig():
    """fehlerpattern_dedup_pro_zone_multisensor: ein Schluessel ohne Zone
    wuerde die zweite Zone mit wegdrosseln."""
    d = WarnDrossel()
    assert d.faellig("meldung:zone_a", T0) is True
    assert d.faellig("meldung:zone_b", T0) is True


def test_verschiedene_meldungen_derselben_zone_sind_getrennt():
    d = WarnDrossel()
    assert d.faellig("festklemmend:hecke", T0) is True
    assert d.faellig("fallback:hecke", T0) is True


def test_zuruecksetzen_gibt_alles_wieder_frei():
    d = WarnDrossel()
    d.faellig("zone_a", T0)
    d.zuruecksetzen()
    assert d.faellig("zone_a", T0) is True


def test_intervall_stunden_fuer_das_logfeld():
    assert WarnDrossel().intervall_stunden == 24.0
    assert WarnDrossel(timedelta(hours=6)).intervall_stunden == 6.0


def test_negativprobe_drossel_ohne_gedaechtnis_faellt_durch():
    """Der alte Zustand: jeder Aufruf meldet. Baut man ihn nach, muss der
    Kern-Test oben anschlagen."""
    class OhneGedaechtnis(WarnDrossel):
        def faellig(self, schluessel, jetzt):  # noqa: ARG002
            return True

    d = OhneGedaechtnis()
    d.faellig("zone_a", T0)
    assert d.faellig("zone_a", T0 + timedelta(minutes=5)) is True, (
        "Negativprobe: ohne Gedaechtnis meldet jeder Aufruf -- genau das "
        "soll test_zweite_meldung_im_intervall_wird_geschluckt verhindern."
    )


def test_default_intervall_ist_ein_tag():
    assert DROSSEL_INTERVALL_DEFAULT == timedelta(hours=24)


# --------------------------------------------------------------------------
# 2. Verdrahtung in entscheidung.py -- ausgefuehrt, nicht auf Quelltext gematcht
# --------------------------------------------------------------------------

class _SpeicherStub:
    """Liefert nie eine Messung -> beide Fallback-Pfade greifen."""

    def __init__(self, festklemmend: bool = False, messung=None):
        self._festklemmend = festklemmend
        self._messung = messung

    async def letzte_messung_aggregiert(self, zone_id, **kw):
        return self._messung


def _motor(festklemmend=False, messung=None):
    from bewaesserung import entscheidung as e

    motor = object.__new__(e.Entscheidungsmotor)
    motor._speicher = _SpeicherStub(festklemmend, messung)

    async def _ist_festklemmend(zone_id):
        return festklemmend

    motor._ist_sensor_festklemmend = _ist_festklemmend
    return motor


@pytest.fixture(autouse=True)
def _drossel_frisch():
    from bewaesserung import entscheidung as e
    e._feuchte_warn_drossel_zuruecksetzen()
    yield
    e._feuchte_warn_drossel_zuruecksetzen()


async def _rufe(motor, jetzt):
    return await motor._robuste_feuchte("pilea", jetzt)


@pytest.mark.asyncio
async def test_festklemmend_meldet_einmal_und_blockiert_weiter():
    """Kernpunkt der Entscheidung: das Verhalten bleibt, nur das Log wird
    leiser. `None` muss bei JEDEM Aufruf zurueckkommen."""
    motor = _motor(festklemmend=True)
    with structlog.testing.capture_logs() as logs:
        for minute in (0, 5, 10, 15):
            assert await _rufe(motor, T0 + timedelta(minutes=minute)) is None
    treffer = [x for x in logs
               if x["event"] == "entscheidung.sensor_festklemmend_blockiert"]
    assert len(treffer) == 1, f"erwartet 1 Meldung, bekam {len(treffer)}"
    assert treffer[0]["drossel_stunden"] == 24.0


@pytest.mark.asyncio
async def test_festklemmend_meldet_nach_24h_wieder():
    motor = _motor(festklemmend=True)
    with structlog.testing.capture_logs() as logs:
        await _rufe(motor, T0)
        await _rufe(motor, T0 + timedelta(hours=24))
    treffer = [x for x in logs
               if x["event"] == "entscheidung.sensor_festklemmend_blockiert"]
    assert len(treffer) == 2


@pytest.mark.asyncio
async def test_fallback_horizont_wird_gedrosselt_aber_blockiert_weiter():
    motor = _motor(messung=None)
    with structlog.testing.capture_logs() as logs:
        for minute in (0, 5, 10):
            assert await _rufe(motor, T0 + timedelta(minutes=minute)) is None
    treffer = [
        x for x in logs
        if x["event"] == "entscheidung.keine_messung_im_fallback_horizont"
    ]
    assert len(treffer) == 1


@pytest.mark.asyncio
async def test_zwei_zonen_melden_beide():
    """Die Drossel darf nicht die zweite Zone mit wegnehmen."""
    motor = _motor(festklemmend=True)
    with structlog.testing.capture_logs() as logs:
        await motor._robuste_feuchte("pilea", T0)
        await motor._robuste_feuchte("zitrus_ii", T0)
    treffer = [x for x in logs
               if x["event"] == "entscheidung.sensor_festklemmend_blockiert"]
    assert {x["zone_id"] for x in treffer} == {"pilea", "zitrus_ii"}


# --------------------------------------------------------------------------
# 3. Opt-out des Leck-Detektors (Fall A)
# --------------------------------------------------------------------------

class _WarnSpeicher:
    """Merkt sich nur, was der Detektor mit der Warnung macht."""

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
    d._wirkungs_alarm_aus = frozenset(aus)
    return d


@pytest.mark.asyncio
async def test_optout_zone_wird_nicht_geprueft_und_warnung_geschlossen():
    """Abgeschalteter Detektor darf keinen offenen Zustand hinterlassen --
    sonst haengt die Warnung fuer immer in DB und UI."""
    from bewaesserung.modelle import SensorWarnungTyp

    d = _detektor(aus={"bambuswald"})
    await d._pruefe_bewaesserung_ohne_wirkung("bambuswald", T0)
    assert d._speicher.geschlossen == [
        ("bambuswald", SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG),
    ]
    assert d._speicher.geoeffnet == []


@pytest.mark.asyncio
async def test_andere_zone_laeuft_weiter_in_die_pruefung():
    """Gegenprobe zur Abdeckung: bambuswald_yogaraum haengt am selben Ventil
    und muss weiter alarmieren koennen -- sonst ist die Kanal-Abdeckung weg,
    auf die sich die Entscheidung stuetzt."""
    d = _detektor(aus={"bambuswald"})
    with pytest.raises(AttributeError):
        # laeuft ueber das Gate hinaus in die echte Pruefung und scheitert
        # erst am unvollstaendigen Stub -- genau das ist der Nachweis.
        await d._pruefe_bewaesserung_ohne_wirkung("bambuswald_yogaraum", T0)
    assert d._speicher.geschlossen == []


@pytest.mark.asyncio
async def test_negativprobe_ohne_optout_greift_das_gate_nicht():
    """Baut den alten Zustand nach (leeres Opt-out): dann muss auch
    bambuswald in die Pruefung laufen."""
    d = _detektor(aus=())
    with pytest.raises(AttributeError):
        await d._pruefe_bewaesserung_ohne_wirkung("bambuswald", T0)
    assert d._speicher.geschlossen == [], (
        "Negativprobe: ohne Opt-out darf das Gate nicht greifen -- greift es "
        "doch, testet test_optout_zone_wird_nicht_geprueft nichts."
    )


# --------------------------------------------------------------------------
# 4. Config-Kette: YAML -> ZonenKonfig -> Detektor-Set
#    (fehlerpattern_config_whitelist: neues Feld wird sonst stumm ignoriert)
# --------------------------------------------------------------------------

def test_default_ist_pruefen():
    from bewaesserung.modelle import ZonenKonfig

    z = ZonenKonfig(zone_id="x", name="X")
    assert z.wirkungs_alarm_aktiv is True


@pytest.mark.live_config
def test_yaml_wert_kommt_durch_die_whitelist():
    """Der eigentliche Whitelist-Test: ein Feld, das in `_parse_zonen` fehlt,
    wird stumm ignoriert und der Default gewinnt."""
    from bewaesserung.konfig import lade_konfig

    konfig = lade_konfig()
    zonen = {z.zone_id: z for z in konfig.zonen}
    assert zonen["bambuswald"].wirkungs_alarm_aktiv is False, (
        "config/default.yaml setzt wirkungs_alarm_aktiv: false fuer "
        "bambuswald -- kommt der Wert nicht an, ist die Whitelist in "
        "konfig.py._parse_zonen unvollstaendig."
    )
    assert zonen["bambuswald_yogaraum"].wirkungs_alarm_aktiv is True, (
        "Die Deckung am selben Kanal muss aktiv bleiben."
    )


@pytest.mark.live_config
def test_ableitung_aus_der_echten_konfig_ergibt_genau_bambuswald():
    """Fuehrt die Ableitung aus, die main.py macht, gegen die ECHTE Konfig.

    Was der Test beweist: aus `config/default.yaml` faellt genau eine Zone ins
    Opt-out. Was er NICHT beweist: dass main.py das Set auch uebergibt -- die
    Konstruktion steht inline in `ausfuehren()` und ist nicht aufrufbar. Diese
    Luecke deckt `test_main_reicht_das_set_an_den_detektor` ab, bewusst als
    Quelltext-Anker und nur als ERGAENZUNG (ein Test, der bloss auf Quelltext
    matcht, beweist fuer sich genommen nichts)."""
    from bewaesserung.konfig import lade_konfig

    konfig = lade_konfig()
    aus = frozenset(
        z.zone_id for z in konfig.zonen if not z.wirkungs_alarm_aktiv
    )
    assert aus == {"bambuswald"}, f"unerwartetes Opt-out-Set: {sorted(aus)}"


def test_main_reicht_das_set_an_den_detektor():
    """Naht-Test gegen `fehlerpattern_detektor_ohne_konsument`: das Feld darf
    nicht geparst, aber nie uebergeben werden. Faellt diese Zeile aus main.py
    heraus, wird der Detektor still wieder alle Zonen pruefen -- und KEIN
    anderer Test im Repo merkt es, weil beide Seiten fuer sich korrekt sind."""
    from pathlib import Path

    quelle = (
        Path(__file__).resolve().parents[1]
        / "src" / "bewaesserung" / "main.py"
    ).read_text()
    assert "wirkungs_alarm_aus=frozenset(" in quelle
    assert "if not z.wirkungs_alarm_aktiv" in quelle
