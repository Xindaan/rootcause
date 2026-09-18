"""Tests fuer T-0128 (H-4) Stufe 1a: Saisonale Feuchte-Regimes.

Deckt ab:
- Datum-Range-Logik inkl. Wraparound ueber Jahreswechsel
- aktives_regime: erstes passendes Regime gewinnt, None bei leerer Liste
- effektiv_*-Helper: Regime-Override vs. Zone-Default
- Backward-Compat: ohne Regime-Liste verhalten sich Zonen unveraendert
- Magerwiese-Szenario: bei sommerlicher Trockenphase wird die
  Empfehlungs-Logik nicht "akut" trotz niedriger Sensor-Feuchte
"""
from __future__ import annotations

from datetime import datetime


from bewaesserung.modelle import (
    FeuchteRegime,
    ZonenKonfig,
    ZonenModus,
    _datum_im_regime,
    aktives_regime,
    effektiv_feuchte_kritisch,
    effektiv_optimum_max,
    effektiv_optimum_min,
    effektiv_schwelle_max,
    effektiv_schwelle_min,
)


def _zone(**overrides) -> ZonenKonfig:
    defaults = dict(
        zone_id="z",
        name="Zone",
        modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=35.0,
        feuchte_schwelle_max=65.0,
        feuchte_kritisch=20.0,
        optimum_feuchte_min=40.0,
        optimum_feuchte_max=60.0,
    )
    defaults.update(overrides)
    return ZonenKonfig(**defaults)


# --- Datum-Range-Logik ------------------------------------------------------

def test_datum_im_regime_normaler_range():
    r = FeuchteRegime(von_mm_dd="04-01", bis_mm_dd="06-30", grund="Anwachs")
    assert _datum_im_regime(r, datetime(2026, 4, 1, 0, 0))
    assert _datum_im_regime(r, datetime(2026, 5, 15, 0, 0))
    assert _datum_im_regime(r, datetime(2026, 6, 30, 23, 59))
    assert not _datum_im_regime(r, datetime(2026, 3, 31, 23, 59))
    assert not _datum_im_regime(r, datetime(2026, 7, 1, 0, 0))


def test_datum_im_regime_wraparound_ueber_jahreswechsel():
    """Winter-Regime 10-15 .. 03-15 deckt November bis Mitte Maerz ab."""
    r = FeuchteRegime(von_mm_dd="10-15", bis_mm_dd="03-15", grund="Winter")
    assert _datum_im_regime(r, datetime(2026, 12, 31, 0, 0))
    assert _datum_im_regime(r, datetime(2026, 1, 5, 0, 0))
    assert _datum_im_regime(r, datetime(2026, 10, 15, 0, 0))
    assert _datum_im_regime(r, datetime(2026, 3, 15, 23, 59))
    assert not _datum_im_regime(r, datetime(2026, 5, 1, 0, 0))
    assert not _datum_im_regime(r, datetime(2026, 8, 30, 0, 0))


# --- aktives_regime: Liste-Logik --------------------------------------------

def test_aktives_regime_leere_liste_gibt_none():
    zone = _zone()
    assert aktives_regime(zone, datetime(2026, 7, 15)) is None


def test_aktives_regime_erstes_match_gewinnt():
    """Wenn zwei Regimes ueberlappen, gewinnt das erste in der Liste."""
    r_anwachs = FeuchteRegime(von_mm_dd="04-01", bis_mm_dd="06-30", optimum_min=50.0)
    r_breit = FeuchteRegime(von_mm_dd="03-01", bis_mm_dd="10-31", optimum_min=40.0)
    zone = _zone(feuchte_regime=[r_anwachs, r_breit])
    aktiv = aktives_regime(zone, datetime(2026, 5, 15))
    assert aktiv is r_anwachs


def test_aktives_regime_konfig_luecke_gibt_none():
    """Kein Regime fuer den aktuellen Zeitpunkt -> None (Fallback auf Defaults)."""
    sommer = FeuchteRegime(von_mm_dd="07-01", bis_mm_dd="09-30", optimum_min=15.0)
    zone = _zone(feuchte_regime=[sommer])
    # Im Mai ist kein Regime aktiv
    assert aktives_regime(zone, datetime(2026, 5, 15)) is None


# --- effektiv_*-Helper ------------------------------------------------------

def test_effektiv_optimum_min_regime_override_wins():
    sommer = FeuchteRegime(von_mm_dd="07-01", bis_mm_dd="09-30", optimum_min=15.0)
    zone = _zone(feuchte_regime=[sommer])
    assert effektiv_optimum_min(zone, datetime(2026, 8, 15)) == 15.0
    # Im Mai (kein Regime aktiv) faellt auf Zone-Default zurueck
    assert effektiv_optimum_min(zone, datetime(2026, 5, 15)) == 40.0


def test_effektiv_helper_mit_partiellem_regime_faellt_auf_zone_default():
    """Regime setzt nur optimum_min -> alle anderen Felder kommen aus Zone."""
    r = FeuchteRegime(von_mm_dd="01-01", bis_mm_dd="12-31", optimum_min=15.0)
    zone = _zone(feuchte_regime=[r])
    jetzt = datetime(2026, 7, 15)
    assert effektiv_optimum_min(zone, jetzt) == 15.0          # Regime
    assert effektiv_optimum_max(zone, jetzt) == 60.0          # Zone-Default
    assert effektiv_schwelle_min(zone, jetzt) == 35.0         # Zone-Default
    assert effektiv_schwelle_max(zone, jetzt) == 65.0         # Zone-Default
    assert effektiv_feuchte_kritisch(zone, jetzt) == 20.0     # Zone-Default


def test_effektiv_alle_felder_per_regime_uebersteuert():
    sommer = FeuchteRegime(
        von_mm_dd="07-01", bis_mm_dd="09-30",
        feuchte_schwelle_min=12.0, feuchte_schwelle_max=30.0,
        feuchte_kritisch=5.0, optimum_min=15.0, optimum_max=25.0,
        grund="Magerwiese-Sommer-Trockenphase",
    )
    zone = _zone(feuchte_regime=[sommer])
    jetzt = datetime(2026, 8, 15)
    assert effektiv_schwelle_min(zone, jetzt) == 12.0
    assert effektiv_schwelle_max(zone, jetzt) == 30.0
    assert effektiv_feuchte_kritisch(zone, jetzt) == 5.0
    assert effektiv_optimum_min(zone, jetzt) == 15.0
    assert effektiv_optimum_max(zone, jetzt) == 25.0


# --- Backward-Compat --------------------------------------------------------

def test_backward_compat_ohne_regime_liste():
    """Zonen ohne feuchte_regime: alle effektiv_*-Werte == Zone-Defaults."""
    zone = _zone()
    jetzt = datetime(2026, 7, 15)
    assert effektiv_optimum_min(zone, jetzt) == 40.0
    assert effektiv_optimum_max(zone, jetzt) == 60.0
    assert effektiv_schwelle_min(zone, jetzt) == 35.0
    assert effektiv_schwelle_max(zone, jetzt) == 65.0
    assert effektiv_feuchte_kritisch(zone, jetzt) == 20.0


# --- Stufe 1b: Welkepunkt per Regime overrideable --------------------------

def test_effektiv_welkepunkt_regime_override():
    """T-0135 (H-4 Stufe 1b): Magerwiese-Sommer-Trockenphase darf einen
    deutlich niedrigeren Welkepunkt haben als Bambus."""
    from bewaesserung.modelle import effektiv_welkepunkt
    sommer = FeuchteRegime(
        von_mm_dd="07-01", bis_mm_dd="09-30", welkepunkt=8.0,
        grund="Magerstauden tolerieren niedrigeren Stress-Punkt",
    )
    zone = _zone(feuchte_regime=[sommer], welkepunkt=20.0)
    # Im Juli: Regime gewinnt
    assert effektiv_welkepunkt(zone, datetime(2026, 8, 15)) == 8.0
    # Im Mai (kein Regime): Helper liefert None -> Aufloesungs-Kette
    # in entscheidung._hole_kalibrier_referenzen faellt zurueck auf
    # zone.welkepunkt (20.0), und das ist Stufe 1 der Kette.
    assert effektiv_welkepunkt(zone, datetime(2026, 5, 15)) is None


def test_effektiv_welkepunkt_ohne_regime_override_gibt_none():
    """Backward-Compat: ohne welkepunkt im Regime liefert effektiv_welkepunkt None.
    Stufe 1 (zone.welkepunkt) wird dann von der Aufloesungs-Kette
    in entscheidung.py uebernommen."""
    from bewaesserung.modelle import effektiv_welkepunkt
    sommer = FeuchteRegime(
        von_mm_dd="07-01", bis_mm_dd="09-30", optimum_min=15.0,
    )
    zone = _zone(feuchte_regime=[sommer], welkepunkt=20.0)
    # Auch im Juli: Regime hat kein welkepunkt -> None
    assert effektiv_welkepunkt(zone, datetime(2026, 8, 15)) is None
