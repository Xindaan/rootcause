"""T-0334: Pro-Strang-Opt-In-Filter des Auto-Loops.

`_baue_auto_loop_kanaele` ist die reine Filter-/Gruppierungslogik, die
entscheidet, WELCHE Zonen der autonome Auto-Loop schalten darf. Der Test
zementiert das Sicherheitsversprechen: eine automatik-Zone mit Ventilkanal
wird NUR autonom gegossen, wenn sie explizit `auto_loop_opt_in=true` hat --
sonst bleibt sie Shadow, auch nach globalem Scharfschalten.
"""

from bewaesserung.main import _baue_auto_loop_kanaele, _pre_soak_policy_aktiv
from bewaesserung.modelle import ZonenKonfig, ZonenModus


def _zone(zone_id, *, modus=ZonenModus.AUTOMATIK, kanal=2,
          geraet="dswc1", opt_in=False, pre_soak_modus="nie",
          pre_soak_min=None):
    return ZonenKonfig(
        zone_id=zone_id,
        name=zone_id,
        modus=modus,
        ventil_kanal=kanal,
        ventil_geraet_id=geraet,
        auto_loop_opt_in=opt_in,
        pre_soak_modus=pre_soak_modus,
        pre_soak_min=pre_soak_min,
    )


def test_pre_soak_policy_aktiv():
    """T-0336: Auto-Loop macht Pre-Soak nur bei modus=immer UND pre_soak_min."""
    # immer + pre_soak_min gesetzt -> Pre-Soak
    assert _pre_soak_policy_aktiv(
        _zone("magerwiese", pre_soak_modus="immer", pre_soak_min=5)
    ) is True
    # Default (nie) -> Einzellauf
    assert _pre_soak_policy_aktiv(_zone("waldblumenhain")) is False
    # immer, aber kein pre_soak_min -> kein Pre-Soak (Einzellauf)
    assert _pre_soak_policy_aktiv(
        _zone("x", pre_soak_modus="immer", pre_soak_min=None)
    ) is False


def test_nur_opt_in_zonen_kommen_in_kanaele():
    """Opt-out-Garantie: automatik+Kanal allein reicht NICHT."""
    zonen = [
        _zone("bambuswald", kanal=2, geraet="dswc1", opt_in=True),
        _zone("waldblumenhain", kanal=1, geraet="dswc1", opt_in=False),
        _zone("hecke", kanal=2, geraet="dswc2", opt_in=False),
    ]
    kanaele = _baue_auto_loop_kanaele(zonen)
    # Nur der Bambus-Kanal ist scharf.
    assert kanaele == {("dswc1", 2): [zonen[0]]}
    # waldblumenhain (dswc1/K1) und hecke (dswc2/K2) sind NICHT enthalten.
    assert ("dswc1", 1) not in kanaele
    assert ("dswc2", 2) not in kanaele


def test_serieller_strang_wird_unter_einem_kanal_gruppiert():
    """bambuswald + bambuswald_yogaraum teilen (geraet, kanal) -> ein Eintrag."""
    zonen = [
        _zone("bambuswald", kanal=2, geraet="dswc1", opt_in=True),
        _zone("bambuswald_yogaraum", kanal=2, geraet="dswc1", opt_in=True),
    ]
    kanaele = _baue_auto_loop_kanaele(zonen)
    assert list(kanaele.keys()) == [("dswc1", 2)]
    assert [z.zone_id for z in kanaele[("dswc1", 2)]] == [
        "bambuswald", "bambuswald_yogaraum",
    ]


def test_monitoring_zone_nie_im_auto_loop_trotz_opt_in():
    """modus=monitoring schlaegt opt_in -- keine autonome Schaltung."""
    zonen = [_zone("magerwiese", modus=ZonenModus.MONITORING, opt_in=True)]
    assert _baue_auto_loop_kanaele(zonen) == {}


def test_zone_ohne_ventilkanal_wird_ignoriert():
    """Ohne Ventilkanal kann der Auto-Loop nichts schalten."""
    zonen = [_zone("topfpflanze", kanal=None, opt_in=True)]
    assert _baue_auto_loop_kanaele(zonen) == {}
