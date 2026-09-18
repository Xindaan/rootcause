"""T-0203: Multi-DSWC-Support Tests.

Unit-Tests fuer die Multi-Device-Helper:
- `baue_kanal_zu_zonen_pro_geraet`: pro DSWC-UUID das Mapping
- Backward-Compat: Zonen ohne `ventil_geraet_id` landen beim primary
"""
from __future__ import annotations


from bewaesserung.gardena_web_backfill import (
    baue_kanal_zu_zonen,
    baue_kanal_zu_zonen_pro_geraet,
)
from bewaesserung.modelle import ZonenKonfig


def _zone(zone_id: str, kanal: int | None, dswc: str | None = None) -> ZonenKonfig:
    return ZonenKonfig(
        zone_id=zone_id, name=zone_id, ventil_kanal=kanal,
        ventil_geraet_id=dswc,
    )


def test_t0203_pro_geraet_mapping_zwei_dswcs():
    """Mapping geraet_id -> kanal -> zonen mit zwei DSWCs."""
    zonen = [
        _zone("waldblumenhain", 1, "DSWC-1"),
        _zone("bambuswald", 2, "DSWC-1"),
        _zone("bambuswald_yogaraum", 2, "DSWC-1"),
        _zone("magerwiese", 1, "DSWC-2"),
        _zone("hecke", 2, "DSWC-2"),
    ]
    out = baue_kanal_zu_zonen_pro_geraet(zonen, primary_geraet_id=None)
    assert set(out.keys()) == {"DSWC-1", "DSWC-2"}
    assert out["DSWC-1"][1] == ["waldblumenhain"]
    assert set(out["DSWC-1"][2]) == {"bambuswald", "bambuswald_yogaraum"}
    assert out["DSWC-2"][1] == ["magerwiese"]
    assert out["DSWC-2"][2] == ["hecke"]


def test_t0203_backward_compat_primary_fallback():
    """Zonen ohne ventil_geraet_id landen beim primary."""
    zonen = [
        _zone("a", 1, None),  # legacy: kein DSWC
        _zone("b", 2, None),
    ]
    out = baue_kanal_zu_zonen_pro_geraet(zonen, primary_geraet_id="PRIMARY")
    assert out == {"PRIMARY": {1: ["a"], 2: ["b"]}}


def test_t0203_kein_primary_keine_dswc_setzt_zone_ignoriert():
    """Ohne primary + ohne explizites DSWC: Zone wird uebersprungen."""
    zonen = [_zone("ghost", 1, None)]
    out = baue_kanal_zu_zonen_pro_geraet(zonen, primary_geraet_id=None)
    assert out == {}


def test_t0203_zonen_ohne_kanal_werden_ignoriert():
    """Monitoring-Zonen ohne ventil_kanal kommen nicht ins Mapping."""
    zonen = [
        _zone("monitoring", None, "DSWC-1"),
        _zone("aktiv", 1, "DSWC-1"),
    ]
    out = baue_kanal_zu_zonen_pro_geraet(zonen, primary_geraet_id=None)
    assert out == {"DSWC-1": {1: ["aktiv"]}}


def test_t0203_legacy_baue_kanal_zu_zonen_bleibt_unchanged():
    """Backward-Compat: alte Funktion liefert weiter kanal -> zonen flach.

    Bei Multi-DSWC werden Zonen zusammengeworfen — das ist die Mehrdeutigkeits-
    Falle, vor der die neue `_pro_geraet`-Variante schuetzt.
    """
    zonen = [
        _zone("waldblumenhain", 1, "DSWC-1"),
        _zone("magerwiese", 1, "DSWC-2"),
    ]
    out = baue_kanal_zu_zonen(zonen)
    # Mehrdeutig: beide Zonen unter Kanal 1, obwohl an verschiedenen DSWCs.
    assert set(out[1]) == {"waldblumenhain", "magerwiese"}


def test_t0203_zonen_konfig_default_ventil_geraet_id_none():
    """Backward-Compat: alte Konfig ohne ventil_geraet_id-Feld bleibt valide."""
    z = ZonenKonfig(zone_id="alt", name="Alt", ventil_kanal=1)
    assert z.ventil_geraet_id is None
