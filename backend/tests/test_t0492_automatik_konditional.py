"""T-0492: AUTOMATIK-Filter im Response-Training konditional machen.

Der Filter schloss `Ausloser.AUTOMATIK` bedingungslos aus, mit dem
Kommentar "wenn T-0021 scharf ist, muss dieser Filter konditional
werden". Der Fall ist eingetreten: vier Zonen giessen autonom.

Der Fix darf aber NICHT pauschal alle Alt-Labels umdeuten. Dieselben
Zonen liefen vorher im Shadow und loggten dabei `automatik`, OHNE dass
Wasser floss -- eine Dosis mit garantiert null Wirkung waere ein
erfundener Nullpunkt im Training. Deshalb eine positive Provenienzregel:
`auto_loop_scharf_seit` pro Zone, belegt aus der Git-Historie.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

# Einige Tests pruefen die REALE `config/default.yaml` (Smoke-Test der echten
# Anlage). Die Datei ist bewusst nicht Teil des oeffentlichen Ablegers -- dort
# liegt nur `config/default.example.yaml`. Sie tragen deshalb `@pytest.mark.live_config`
# (Mechanik in `backend/conftest.py`): im Snapshot uebersprungen, privat
# unveraendert.


from bewaesserung.ml.response_features import (
    AUSGESCHLOSSENE_AUSLOSER,
    _automatik_scharf_ab,
    _baue_pulse,
    _ist_trainingsfaehig,
)
from bewaesserung.modelle import (
    Ausloser,
    VentilAktion,
    VentilEreignis,
    ZonenKonfig,
)

GO_LIVE = datetime(2026, 6, 26, 13, 45)


def _zone(zone_id: str, scharf_seit: datetime | None) -> ZonenKonfig:
    return ZonenKonfig(
        zone_id=zone_id, name=zone_id, auto_loop_scharf_seit=scharf_seit,
    )


def _event(
    zeitstempel: datetime,
    ausloser: Ausloser = Ausloser.AUTOMATIK,
    dauer: int = 300,
) -> VentilEreignis:
    return VentilEreignis(
        zeitstempel=zeitstempel,
        zone_id="bambuswald",
        ventil_id="uuid:2",
        aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=dauer,
        ausloser=ausloser,
    )


# --------------------------------------------------------------------
# Scharf-Zeitpunkt eines Kanals
# --------------------------------------------------------------------

def test_scharf_ab_ist_der_frueheste_zeitpunkt_am_kanal():
    """Ein Kanal naesst alle Zonen daran. Sobald EINE scharf ist, oeffnet
    die Engine real -- das Wasser erreicht auch die andere."""
    spaet = GO_LIVE + timedelta(days=31)
    assert _automatik_scharf_ab([
        _zone("bambuswald", GO_LIVE), _zone("yogaraum", spaet),
    ]) == GO_LIVE


def test_scharf_ab_ignoriert_nie_scharfe_zonen():
    """Eine Zone ohne Datum darf den Kanal nicht freischalten, aber auch
    nicht blockieren, wenn eine andere nachweislich scharf ist."""
    assert _automatik_scharf_ab([
        _zone("magerwiese", None), _zone("hecke", GO_LIVE),
    ]) == GO_LIVE


def test_scharf_ab_none_wenn_keine_zone_je_scharf_war():
    assert _automatik_scharf_ab([_zone("magerwiese", None)]) is None
    assert _automatik_scharf_ab([]) is None


# --------------------------------------------------------------------
# Die Filterentscheidung
# --------------------------------------------------------------------

def test_automatik_vor_der_scharfschaltung_bleibt_draussen():
    """DER Kern: Shadow-Events tragen `automatik`, aber es floss kein
    Wasser. Sie als Response zu lesen hiesse, eine Dosis mit garantiert
    null Wirkung ins Training zu geben."""
    assert not _ist_trainingsfaehig(
        Ausloser.AUTOMATIK, GO_LIVE - timedelta(minutes=1), GO_LIVE,
    )


def test_automatik_ab_der_scharfschaltung_kommt_rein():
    assert _ist_trainingsfaehig(Ausloser.AUTOMATIK, GO_LIVE, GO_LIVE)
    assert _ist_trainingsfaehig(
        Ausloser.AUTOMATIK, GO_LIVE + timedelta(days=40), GO_LIVE,
    )


def test_automatik_bleibt_draussen_wenn_zone_nie_scharf_war():
    """magerwiese: `automatik` entsteht dort durch den Positiv-Opt-in
    (T-0300), nicht durch einen Engine-Lauf. Ohne belegte Scharfschaltung
    gilt die konservative Regel."""
    assert not _ist_trainingsfaehig(
        Ausloser.AUTOMATIK, GO_LIVE + timedelta(days=1), None,
    )


@pytest.mark.parametrize(
    "ausloser", [Ausloser.UNBEKANNT, Ausloser.IGNORIERT, Ausloser.FREMDWASSER],
)
def test_andere_ausschluesse_bleiben_unbedingt(ausloser):
    """Isomorphie-Guard: der konditionale Pfad gilt NUR fuer AUTOMATIK.
    UNBEKANNT/IGNORIERT/FREMDWASSER haben eigene Gruende (kein Wasser bzw.
    Wasser aus fremdem Kanal) und duerfen davon nicht profitieren."""
    assert not _ist_trainingsfaehig(
        ausloser, GO_LIVE + timedelta(days=1), GO_LIVE,
    )


@pytest.mark.parametrize(
    "ausloser", [Ausloser.MANUELL, Ausloser.ZEITPLAN, Ausloser.AQUABLOOM],
)
def test_nicht_ausgeschlossene_ausloser_unveraendert(ausloser):
    assert ausloser not in AUSGESCHLOSSENE_AUSLOSER
    assert _ist_trainingsfaehig(ausloser, GO_LIVE, None)


def test_unbekannter_ausloser_wird_nicht_gefiltert():
    """`None` = Enum-Wert, den wir nicht kennen. Die Pipeline filtert ihn
    nicht, also darf der Proxy es auch nicht -- sonst entsteht die stille
    Unterzaehlung aus fehlerpattern_neuer_enumwert_faellt_aus_positivfilter.
    """
    assert _ist_trainingsfaehig(None, GO_LIVE, None)


# --------------------------------------------------------------------
# Wirkung in der Puls-Bildung
# --------------------------------------------------------------------

def test_baue_pulse_trennt_shadow_von_scharf():
    """Realfall bambuswald: Events aus April (Shadow) und ab Ende Juni
    (scharf) liegen in derselben Serie."""
    events = [
        _event(datetime(2026, 4, 12, 10, 0)),   # Shadow
        _event(datetime(2026, 6, 20, 10, 0)),   # Shadow
        _event(datetime(2026, 6, 26, 19, 15)),  # scharf (nach 13:45)
        _event(datetime(2026, 7, 3, 10, 0)),    # scharf
    ]

    pulse = _baue_pulse(events, cluster_gap_min=60, automatik_scharf_ab=GO_LIVE)

    assert [p.t_end for p in pulse] == [
        datetime(2026, 6, 26, 19, 15), datetime(2026, 7, 3, 10, 0),
    ]


def test_baue_pulse_default_haelt_altes_verhalten():
    """Gegenprobe: ohne den neuen Parameter faellt `automatik` weiterhin
    komplett raus. Belegt, dass die Aufweitung am Parameter haengt und
    nicht versehentlich global gilt."""
    events = [_event(datetime(2026, 7, 3, 10, 0))]
    assert _baue_pulse(events, cluster_gap_min=60) == []


def test_baue_pulse_laesst_manuell_immer_durch():
    events = [_event(datetime(2026, 4, 1, 10, 0), ausloser=Ausloser.MANUELL)]
    assert len(_baue_pulse(events, cluster_gap_min=60)) == 1


# --------------------------------------------------------------------
# Config-Kette (fehlerpattern_config_whitelist)
# --------------------------------------------------------------------

@pytest.mark.live_config
def test_scharf_seit_kommt_aus_der_echten_config_an():
    """Ohne die Whitelist-Zeile in `konfig.py` bliebe das Feld trotz
    YAML-Eintrag None -- und der Fix waere wirkungslos, ohne dass etwas
    fehlschlaegt."""
    from bewaesserung.konfig import lade_konfig

    zonen = {z.zone_id: z for z in lade_konfig().zonen}
    scharf = {
        zid: z.auto_loop_scharf_seit
        for zid, z in zonen.items() if z.auto_loop_opt_in
    }
    assert scharf, "keine Zone mit auto_loop_opt_in in der Config"
    assert all(v is not None for v in scharf.values()), (
        f"opt-in-Zone ohne auto_loop_scharf_seit: {scharf}"
    )
    # Belegt aus der Git-Historie, s. Feldkommentar in modelle.py.
    assert scharf["bambuswald"] == GO_LIVE
    assert scharf["waldblumenhain"] == datetime(2026, 7, 27, 15, 15)
