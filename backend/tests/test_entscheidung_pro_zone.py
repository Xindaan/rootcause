"""T-0231 Phase 1: Tests fuer die gemeinsame Pro-Zone-Entscheidungs-Logik.

Deckt die 4 Strategien (KORRIDOR/HAEUFIG_KLEIN/SELTEN_GROSS/
KONSTANT_NIEDRIG) gegen unterschiedliche Kontext-Kombinationen
(Welkepunkt-Reserve, Wohl-Min, Prognose, Feldkapazitaet).

Phase 1 baut die Funktion isoliert -- Phase 2 verdrahtet sie in
vorhersage_zone, Phase 3 in pruefe_kanal. Diese Tests sichern das
Verhaltens-Vertrag ab BEVOR die Konsumenten umgestellt werden.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from bewaesserung.entscheidung_pro_zone import (
    EMPFEHLUNG_TRIGGERT_BEWAESSERUNG,
    ProZoneKontext,
    ProZoneAuswertung,
    entscheide_pro_zone,
)
from bewaesserung.modelle import BewaesserungsStrategie, ZonenKonfig


JETZT = datetime(2026, 6, 15, 10, 0)


def _zone(
    strategie: BewaesserungsStrategie = BewaesserungsStrategie.KORRIDOR,
    sicherheits_tage: float = 3.0,
    optimum_min: float | None = None,
    optimum_max: float | None = None,
    proaktiv_tage_vor_optimum_min: float | None = None,
) -> ZonenKonfig:
    return ZonenKonfig(
        zone_id="test", name="Test", ventil_kanal=1,
        feuchte_schwelle_min=35.0, feuchte_schwelle_max=65.0,
        feuchte_kritisch=20.0,
        bewaesserungs_strategie=strategie,
        sicherheits_tage=sicherheits_tage,
        optimum_feuchte_min=optimum_min,
        optimum_feuchte_max=optimum_max,
        proaktiv_tage_vor_optimum_min=proaktiv_tage_vor_optimum_min,
    )


def _ktx(
    feuchte: float = 50.0,
    welkepunkt: float | None = 20.0,
    tage_bis_welke: float | None = 5.0,
    fk: float | None = None,
    prognose: dict[int, float] | None = None,
    sicherheits_tage_konfig: float = 3.0,
    decay_pp_pro_tag: float = 0.0,
    tage_bis_proaktiv: float | None = None,
    kuerzlich_gegossen: bool = False,
) -> ProZoneKontext:
    return ProZoneKontext(
        aktuelle_feuchte=feuchte,
        prognose=prognose,
        welkepunkt_wert=welkepunkt,
        tage_bis_welke=tage_bis_welke,
        fk_wert=fk,
        sicherheits_tage_konfig=sicherheits_tage_konfig,
        decay_pp_pro_tag=decay_pp_pro_tag,
        tage_bis_proaktiv=tage_bis_proaktiv,
        kuerzlich_gegossen=kuerzlich_gegossen,
    )


# --- KORRIDOR (Default) -----------------------------------------------------

def test_korridor_welkepunkt_unbekannt_praeventiv():
    """Ohne tage_bis_welke faellt Korridor sicher auf 'praeventiv'."""
    a = entscheide_pro_zone(_zone(), _ktx(tage_bis_welke=None), JETZT)
    assert a.empfehlungs_typ == "praeventiv"
    assert a.soll_bewaessern is True


def test_korridor_welke_akut_innerhalb_1_5d():
    """Tage_bis_welke <= 1.5 -> 'akut' in jeder Strategie."""
    a = entscheide_pro_zone(_zone(), _ktx(tage_bis_welke=1.0), JETZT)
    assert a.empfehlungs_typ == "akut"
    assert a.soll_bewaessern is True


def test_korridor_reserve_ueber_sicherheits_tage_kein_bedarf():
    """Reserve 5d > sicherheits_tage 3d, kein Wohl-Min gesetzt -> kein_bedarf."""
    a = entscheide_pro_zone(
        _zone(sicherheits_tage=3.0),
        _ktx(tage_bis_welke=5.0, sicherheits_tage_konfig=3.0),
        JETZT,
    )
    assert a.empfehlungs_typ == "kein_bedarf"
    assert a.soll_bewaessern is False
    assert a.ziel_feuchte is None


def test_korridor_reserve_unter_sicherheits_tage_praeventiv():
    a = entscheide_pro_zone(
        _zone(sicherheits_tage=3.0),
        _ktx(tage_bis_welke=2.0, sicherheits_tage_konfig=3.0),
        JETZT,
    )
    assert a.empfehlungs_typ == "praeventiv"


def test_korridor_wohlfuehl_grenze_kein_trigger():
    """Welkepunkt-Reserve ok, Sensor unter optimum_min -> 'wohlfuehl_grenze'.
    KEIN soll_bewaessern (sanfter Hinweis, nicht Trigger)."""
    a = entscheide_pro_zone(
        _zone(optimum_min=45.0, optimum_max=65.0, sicherheits_tage=3.0),
        _ktx(feuchte=40.0, tage_bis_welke=5.0),
        JETZT,
    )
    assert a.empfehlungs_typ == "wohlfuehl_grenze"
    assert a.soll_bewaessern is False


def test_korridor_ziel_reserve_kann_optimum_max_ueberschreiten():
    """KORRIDOR-Ziel = max(welkepunkt+5+decay*st_eff, opt_max). Bei
    hohem Welkepunkt + Decay kann die Reserve ueber opt_max gehen.
    Beispiel: welke=50, decay=5pp/d, st_eff=3 -> reserve=70 > opt_max=60."""
    a = entscheide_pro_zone(
        _zone(optimum_max=60.0, sicherheits_tage=3.0),
        _ktx(
            feuchte=55.0, welkepunkt=50.0, tage_bis_welke=10.0,
            decay_pp_pro_tag=5.0, sicherheits_tage_konfig=3.0,
        ),
        JETZT,
    )
    # KORRIDOR-kein_bedarf weil Reserve gross + ueber Wohl
    # Reserve = 50 + 5 + 5*3 = 70, opt_max = 60 -> ziel waere 70
    # bei kein_bedarf wird ziel auf None gesetzt -> Test fuer den
    # Trigger-Fall:
    b = entscheide_pro_zone(
        _zone(optimum_max=60.0, sicherheits_tage=3.0),
        _ktx(
            feuchte=53.0, welkepunkt=50.0, tage_bis_welke=2.0,
            decay_pp_pro_tag=5.0, sicherheits_tage_konfig=3.0,
        ),
        JETZT,
    )
    assert b.empfehlungs_typ == "praeventiv"
    assert b.ziel_feuchte == 70.0  # reserve schlaegt opt_max


def test_korridor_ziel_ohne_welkepunkt_faellt_auf_optimum_max():
    a = entscheide_pro_zone(
        _zone(optimum_max=65.0, sicherheits_tage=3.0),
        _ktx(welkepunkt=None, tage_bis_welke=None, feuchte=40.0),
        JETZT,
    )
    # Ohne welkepunkt: 'praeventiv'-Default, ziel = opt_max
    assert a.empfehlungs_typ == "praeventiv"
    assert a.ziel_feuchte == 65.0


def test_korridor_prognose_min_unter_wohl_triggert_wohlfuehl():
    """Aktuelle Feuchte ok, aber Prognose-Min unter Wohl-Min ->
    wohlfuehl_grenze (Multi-Horizont T-0103-Folge)."""
    a = entscheide_pro_zone(
        _zone(optimum_min=45.0, sicherheits_tage=3.0),
        _ktx(
            feuchte=50.0, tage_bis_welke=5.0,
            prognose={6: 48.0, 12: 42.0, 24: 40.0},
        ),
        JETZT,
    )
    assert a.empfehlungs_typ == "wohlfuehl_grenze"


# --- HAEUFIG_KLEIN ----------------------------------------------------------

def test_haeufig_klein_wohlmin_primaer_trigger():
    """Sensor unter Wohl-Min -> direkt 'praeventiv' (auch wenn
    Welkepunkt-Reserve gross)."""
    a = entscheide_pro_zone(
        _zone(
            strategie=BewaesserungsStrategie.HAEUFIG_KLEIN,
            optimum_min=45.0,
        ),
        _ktx(feuchte=40.0, tage_bis_welke=10.0),
        JETZT,
    )
    assert a.empfehlungs_typ == "praeventiv"
    assert a.soll_bewaessern is True


def test_haeufig_klein_st_eff_1_tag():
    """HAEUFIG_KLEIN nutzt internen st_eff=1.0 statt Konfig."""
    a = entscheide_pro_zone(
        _zone(
            strategie=BewaesserungsStrategie.HAEUFIG_KLEIN,
            sicherheits_tage=5.0,  # wird ueberschrieben
        ),
        _ktx(feuchte=50.0, tage_bis_welke=2.0),
        JETZT,
    )
    assert a.effektive_sicherheits_tage == 1.0
    assert a.empfehlungs_typ == "kein_bedarf"  # 2d > 1d


def test_haeufig_klein_ziel_optimum_max():
    a = entscheide_pro_zone(
        _zone(
            strategie=BewaesserungsStrategie.HAEUFIG_KLEIN,
            optimum_min=45.0, optimum_max=65.0,
        ),
        _ktx(feuchte=40.0, tage_bis_welke=10.0),
        JETZT,
    )
    assert a.ziel_feuchte == 65.0


# --- SELTEN_GROSS -----------------------------------------------------------

def test_selten_gross_nur_akut_oder_kein_bedarf():
    """SELTEN_GROSS hat KEIN praeventiv -- nur akut bei Welke oder
    'kein_bedarf'."""
    a = entscheide_pro_zone(
        _zone(strategie=BewaesserungsStrategie.SELTEN_GROSS),
        _ktx(feuchte=30.0, tage_bis_welke=3.0),  # waere KORRIDOR-praeventiv
        JETZT,
    )
    assert a.empfehlungs_typ == "kein_bedarf"
    assert a.soll_bewaessern is False


def test_selten_gross_welke_akut_triggert():
    a = entscheide_pro_zone(
        _zone(strategie=BewaesserungsStrategie.SELTEN_GROSS),
        _ktx(feuchte=25.0, tage_bis_welke=1.0),
        JETZT,
    )
    assert a.empfehlungs_typ == "akut"
    assert a.soll_bewaessern is True


def test_selten_gross_ziel_feldkapazitaet():
    """Ziel = Feldkapazitaet (Tiefen-Dose), Fallback optimum_max + 5."""
    a = entscheide_pro_zone(
        _zone(strategie=BewaesserungsStrategie.SELTEN_GROSS),
        _ktx(feuchte=25.0, tage_bis_welke=1.0, fk=85.0),
        JETZT,
    )
    assert a.ziel_feuchte == 85.0


def test_selten_gross_ziel_fallback_optimum_max_plus_5():
    a = entscheide_pro_zone(
        _zone(
            strategie=BewaesserungsStrategie.SELTEN_GROSS,
            optimum_max=70.0,
        ),
        _ktx(feuchte=25.0, tage_bis_welke=1.0, fk=None),
        JETZT,
    )
    assert a.ziel_feuchte == 75.0


# --- T-0279 Phase 2: proaktiver SELTEN_GROSS-Trigger -----------------------

def test_t0279_selten_gross_proaktiv_praeventiv():
    """Realfall waldblumen: feuchte ueber Schwelle, ML-tage_bis_welke
    weit weg (kein akut), aber Physik-Reserve <= proaktiv-Schwelle ->
    `praeventiv` (proaktiver Tiefenlauf), nicht kein_bedarf."""
    a = entscheide_pro_zone(
        _zone(
            strategie=BewaesserungsStrategie.SELTEN_GROSS,
            optimum_max=60.0,
            proaktiv_tage_vor_optimum_min=6.0,
        ),
        _ktx(
            feuchte=44.0, welkepunkt=32.0,
            tage_bis_welke=9.5,            # ML/Heuristik: weit weg
            tage_bis_proaktiv=5.0,   # Physik: unter 6d-Schwelle
            fk=80.0,
        ),
        JETZT,
    )
    assert a.empfehlungs_typ == "praeventiv"
    assert a.soll_bewaessern is True
    assert a.ziel_feuchte == 80.0  # Feldkapazitaet (Tiefen-Dose)


def test_t0286_proaktiv_unterdrueckt_nach_kuerzlichem_giessen():
    """T-0286 Recency-Guard: identische Lage wie oben (Physik-Reserve <=
    Schwelle -> wuerde proaktiv feuern), aber ein Ground-Truth-Lauf endete
    gerade im Sensor-Nachlauf-Fenster (kuerzlich_gegossen=True) -> KEIN
    proaktiver Trigger (die traegen Multi-Sensor hinken dem frisch
    gegossenen Gardena nach). Verhindert 'grad gegossen, trotzdem giess-
    Empfehlung'."""
    a = entscheide_pro_zone(
        _zone(
            strategie=BewaesserungsStrategie.SELTEN_GROSS,
            optimum_max=60.0,
            proaktiv_tage_vor_optimum_min=6.0,
        ),
        _ktx(
            feuchte=44.0, welkepunkt=32.0,
            tage_bis_welke=9.5,
            tage_bis_proaktiv=5.0,        # <= 6d -> wuerde proaktiv feuern
            kuerzlich_gegossen=True,      # ... aber gerade gegossen
            fk=80.0,
        ),
        JETZT,
    )
    assert a.empfehlungs_typ == "kein_bedarf"
    assert a.soll_bewaessern is False


def test_t0286_akut_ignoriert_recency_guard():
    """T-0286: der Recency-Guard betrifft NUR den proaktiven Trigger.
    Akut (Welke <= 1.5d, Notfall-Schutz) feuert auch direkt nach einem
    Lauf -- sonst wuerde ein echter Notfall unterdrueckt."""
    a = entscheide_pro_zone(
        _zone(
            strategie=BewaesserungsStrategie.SELTEN_GROSS,
            proaktiv_tage_vor_optimum_min=6.0,
        ),
        _ktx(
            feuchte=33.0, tage_bis_welke=1.0,   # akut
            tage_bis_proaktiv=5.0,
            kuerzlich_gegossen=True,
        ),
        JETZT,
    )
    assert a.empfehlungs_typ == "akut"


def test_t0279_selten_gross_proaktiv_reserve_ueber_schwelle_kein_bedarf():
    """Physik-Reserve noch ueber der Schwelle -> kein proaktiver
    Trigger (kein_bedarf)."""
    a = entscheide_pro_zone(
        _zone(
            strategie=BewaesserungsStrategie.SELTEN_GROSS,
            proaktiv_tage_vor_optimum_min=6.0,
        ),
        _ktx(
            feuchte=50.0, welkepunkt=32.0,
            tage_bis_welke=12.0,
            tage_bis_proaktiv=9.0,   # > 6d
        ),
        JETZT,
    )
    assert a.empfehlungs_typ == "kein_bedarf"


def test_t0279_selten_gross_ohne_proaktiv_feld_altes_verhalten():
    """Ohne proaktiv_tage_vor_optimum_min (Default None) bleibt das alte
    Verhalten: nur akut bei <= 1.5d, sonst kein_bedarf -- auch wenn eine
    Physik-Reserve vorliegt."""
    a = entscheide_pro_zone(
        _zone(strategie=BewaesserungsStrategie.SELTEN_GROSS),
        _ktx(
            feuchte=44.0, tage_bis_welke=9.5,
            tage_bis_proaktiv=3.0,   # waere unter Schwelle, aber
                                            # kein proaktiv-Feld gesetzt
        ),
        JETZT,
    )
    assert a.empfehlungs_typ == "kein_bedarf"


def test_t0279_selten_gross_akut_hat_vorrang_vor_proaktiv():
    """Welke <= 1.5d -> akut, auch wenn proaktiv-Schwelle ebenfalls
    greift (akut ist die staerkere Aussage + Notfall-Schutz)."""
    a = entscheide_pro_zone(
        _zone(
            strategie=BewaesserungsStrategie.SELTEN_GROSS,
            proaktiv_tage_vor_optimum_min=6.0,
        ),
        _ktx(
            feuchte=33.0, tage_bis_welke=1.0,
            tage_bis_proaktiv=1.0,
        ),
        JETZT,
    )
    assert a.empfehlungs_typ == "akut"


def test_t0279_proaktiv_keine_physik_reserve_kein_trigger():
    """Physik nicht gefittet (tage_bis_proaktiv None) -> proaktiver
    Trigger inaktiv, Fallback altes Verhalten."""
    a = entscheide_pro_zone(
        _zone(
            strategie=BewaesserungsStrategie.SELTEN_GROSS,
            proaktiv_tage_vor_optimum_min=6.0,
        ),
        _ktx(
            feuchte=44.0, tage_bis_welke=9.5,
            tage_bis_proaktiv=None,   # keine Physik
        ),
        JETZT,
    )
    assert a.empfehlungs_typ == "kein_bedarf"


def test_t0279_korridor_proaktiv_isomorph():
    """Isomorphie: KORRIDOR mit proaktiv-Feld triggert ebenfalls
    praeventiv auf Physik-Reserve (vor dem sicherheits_tage-Zweig)."""
    a = entscheide_pro_zone(
        _zone(
            strategie=BewaesserungsStrategie.KORRIDOR,
            sicherheits_tage=3.0,
            optimum_min=40.0, optimum_max=60.0,
            proaktiv_tage_vor_optimum_min=6.0,
        ),
        _ktx(
            feuchte=55.0, welkepunkt=32.0,
            tage_bis_welke=10.0,           # > sicherheits_tage 3
            tage_bis_proaktiv=5.0,   # <= 6d Physik
            sicherheits_tage_konfig=3.0,
        ),
        JETZT,
    )
    assert a.empfehlungs_typ == "praeventiv"
    assert a.soll_bewaessern is True


# --- KONSTANT_NIEDRIG -------------------------------------------------------

def test_konstant_niedrig_welkepunkt_reserve_5pp_triggert_akut():
    """Sensor <= Welkepunkt + 5pp -> akut (Notfall-Schutz fuer
    Trockenphasen-Strategie)."""
    a = entscheide_pro_zone(
        _zone(strategie=BewaesserungsStrategie.KONSTANT_NIEDRIG),
        _ktx(feuchte=24.0, welkepunkt=20.0, tage_bis_welke=10.0),
        JETZT,
    )
    assert a.empfehlungs_typ == "akut"


def test_konstant_niedrig_ueber_reserve_kein_bedarf():
    """Sensor klar ueber Welkepunkt -> bewusste Trockenphase, kein
    Trigger. Wuerde in KORRIDOR praeventiv triggern."""
    a = entscheide_pro_zone(
        _zone(strategie=BewaesserungsStrategie.KONSTANT_NIEDRIG),
        _ktx(feuchte=40.0, welkepunkt=20.0, tage_bis_welke=4.0),
        JETZT,
    )
    assert a.empfehlungs_typ == "kein_bedarf"
    assert a.soll_bewaessern is False


def test_konstant_niedrig_ziel_optimum_min():
    """Ziel = optimum_min (bewusst niedrig)."""
    a = entscheide_pro_zone(
        _zone(
            strategie=BewaesserungsStrategie.KONSTANT_NIEDRIG,
            optimum_min=30.0, optimum_max=50.0,
        ),
        _ktx(feuchte=24.0, welkepunkt=20.0),
        JETZT,
    )
    assert a.ziel_feuchte == 30.0


# --- Vertragstests (sammeln Querschnitts-Verhaltens) ----------------------

@pytest.mark.parametrize("strategie", list(BewaesserungsStrategie))
def test_alle_strategien_welke_in_1_tag_triggern_akut(strategie):
    """Notfall-Schutz: tage_bis_welke <= 1.5 -> akut in jeder Strategie."""
    a = entscheide_pro_zone(
        _zone(strategie=strategie),
        _ktx(feuchte=22.0, tage_bis_welke=1.0),
        JETZT,
    )
    assert a.empfehlungs_typ == "akut"
    assert a.soll_bewaessern is True


@pytest.mark.parametrize("strategie", list(BewaesserungsStrategie))
def test_alle_strategien_kein_bedarf_setzt_ziel_none(strategie):
    """kein_bedarf -> ziel_feuchte None, soll_bewaessern False."""
    a = entscheide_pro_zone(
        _zone(strategie=strategie),
        _ktx(feuchte=80.0, welkepunkt=20.0, tage_bis_welke=20.0),
        JETZT,
    )
    if a.empfehlungs_typ == "kein_bedarf":
        assert a.ziel_feuchte is None
        assert a.soll_bewaessern is False


def test_empfehlung_triggert_bewaesserung_set_minimal():
    """Vertrag: nur 'akut' + 'praeventiv' triggern Bewaesserung.
    'wohlfuehl_grenze' ist Hinweis, 'kein_bedarf' ist Stop."""
    assert "akut" in EMPFEHLUNG_TRIGGERT_BEWAESSERUNG
    assert "praeventiv" in EMPFEHLUNG_TRIGGERT_BEWAESSERUNG
    assert "wohlfuehl_grenze" not in EMPFEHLUNG_TRIGGERT_BEWAESSERUNG
    assert "kein_bedarf" not in EMPFEHLUNG_TRIGGERT_BEWAESSERUNG


def test_auswertung_grund_immer_nicht_leer():
    """Sanity: jede Auswertung hat einen nicht-leeren Grund."""
    for strategie in BewaesserungsStrategie:
        for feuchte in (10, 30, 50, 80):
            for tbw in (None, 0.5, 2.0, 5.0, 20.0):
                a = entscheide_pro_zone(
                    _zone(strategie=strategie),
                    _ktx(feuchte=feuchte, tage_bis_welke=tbw),
                    JETZT,
                )
                assert a.grund, f"Leerer Grund: {strategie} f={feuchte} tbw={tbw}"
                assert isinstance(a, ProZoneAuswertung)
