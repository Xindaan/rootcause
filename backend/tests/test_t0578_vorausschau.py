"""T-0578 B: vorausschauend giessen, bevor das Giessfenster zugeht.

Andres Entscheid 16.09.2026. Drei Schichten, jeweils am Verhalten gemessen:
  A  reine Rechnung: Feuchte zur Zeit, Fensterschluss, Urteil
  B  die ECHTE Engine an allen drei Aufrufstellen (`pruefe_kanal` steuert
     die Ventile, `vorhersage_zone` Dashboard/Watchdog, `pruefe_zone`)
  C  die Leitplanken: Kritisch-Bypass am Messwert, kein Vorgriff bei offenem
     Fenster, geschlossenem Fenster, Nicht-Aktivmodus

Tagesgang: der echte 08.09. (warm) aus `test_t0576_giessfenster`. Mit Klammer
04-17, Sonne 0,6 und Trocknung 0,3 gilt dort: 09:00 offen, 09:30 zu (Sonne),
wieder frei erst am Folgetag 04:00 -- 19 h Wartezeit ab 09:00.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from bewaesserung.modelle import BlockerTyp
from bewaesserung.vorausschau import (
    FensterSchluss,
    bewerte_vorausschau,
    feuchte_nach_stunden,
    fenster_schliesst_bald,
)
from test_t0576_giessfenster import (
    HEISS_0809,
    KUEHL_1609,
    TAG,
    _vorhersage,
    _zone,
)

NEUN = TAG.replace(hour=9)
FALLEND = {6: 42.0, 12: 38.0, 24: 30.0}     # 45 -> 30 in 24 h
FLACH = {6: 44.8, 12: 44.6, 24: 44.2}


# ================================================== A: reine Rechnung

def test_a_feuchte_zwischen_den_horizonten_linear():
    p = {6: 44.0, 12: 43.0, 24: 41.0}
    assert feuchte_nach_stunden(45.0, p, 4.0, 3) == pytest.approx(44.5)
    assert feuchte_nach_stunden(45.0, p, 4.0, 18) == pytest.approx(42.0)


def test_a_feuchte_hinter_dem_letzten_horizont_mit_decay():
    p = {6: 44.0, 12: 43.0, 24: 41.0}
    assert feuchte_nach_stunden(45.0, p, 4.0, 36) == pytest.approx(39.0)


def test_a_fenster_schliesst_um_halb_zehn_und_oeffnet_morgen_frueh():
    s = fenster_schliesst_bald(_zone("aktiv"), NEUN, _vorhersage(HEISS_0809))
    assert s is not None
    assert s.zu_ab == TAG.replace(hour=9, minute=30)
    assert s.wieder_auf == TAG + timedelta(days=1, hours=4)


def test_a_fenster_lange_offen_kein_schluss():
    """Kuehler Tag, 09:00: bis 09:30 bleibt es offen -> noch nichts zu tun."""
    assert fenster_schliesst_bald(
        _zone("aktiv"), NEUN, _vorhersage(KUEHL_1609),
    ) is None


def test_a_fenster_schon_zu_kein_schluss():
    """11:00 am 08.09. ist schon gesperrt -- vorgreifen geht nur aus offenem
    Fenster heraus, sonst hebelte die Prognose die Sonnen-Sperre aus."""
    assert fenster_schliesst_bald(
        _zone("aktiv"), TAG.replace(hour=11), _vorhersage(HEISS_0809),
    ) is None


@pytest.mark.parametrize("modus", [None, "aus", "schatten"])
def test_a_nur_im_aktivmodus(modus):
    assert fenster_schliesst_bald(
        _zone(modus), NEUN, _vorhersage(HEISS_0809),
    ) is None


def _schluss():
    return FensterSchluss(
        zu_ab=TAG.replace(hour=9, minute=30),
        wieder_auf=TAG + timedelta(days=1, hours=4),
    )


def test_a_faellt_bis_zur_oeffnung_unter_schwelle():
    # 19 h: 38 + (30 - 38) * 7/12 = 33,3 < 35
    v = bewerte_vorausschau(
        schluss=_schluss(), jetzt=NEUN, messwert=45.0, schwelle=35.0,
        prognose=FALLEND, decay_pp_pro_tag=15.0,
    )
    assert v is not None
    assert v.prognose_feuchte == pytest.approx(33.33, abs=0.01)
    assert v.stunden == 19.0
    assert "vorgezogen: jetzt 45%, Prognose 33.3%" in v.text()


def test_a_bleibt_ueber_schwelle_kein_vorgriff():
    assert bewerte_vorausschau(
        schluss=_schluss(), jetzt=NEUN, messwert=45.0, schwelle=35.0,
        prognose=FLACH, decay_pp_pro_tag=1.0,
    ) is None


def test_a_horizont_ist_die_oeffnung_nicht_24h():
    """Oeffnet das Fenster schon nach 6 h wieder, zaehlt der 6-h-Wert (42),
    nicht der 24-h-Wert (30). Negativprobe gegen "immer 24 h"."""
    frueh = FensterSchluss(
        zu_ab=TAG.replace(hour=9, minute=30), wieder_auf=TAG.replace(hour=15),
    )
    assert bewerte_vorausschau(
        schluss=frueh, jetzt=NEUN, messwert=45.0, schwelle=35.0,
        prognose=FALLEND, decay_pp_pro_tag=15.0,
    ) is None


def test_a_schon_unter_schwelle_ist_regulaerer_bedarf():
    assert bewerte_vorausschau(
        schluss=_schluss(), jetzt=NEUN, messwert=30.0, schwelle=35.0,
        prognose=FALLEND, decay_pp_pro_tag=15.0,
    ) is None


# ============================================ B: echte Engine, drei Stellen

def _prognose_stub(motor, prognose, decay_pp_pro_tag=15.0):
    aufrufe = []

    async def _stub(zone_id, aktuelle_feuchte, decay, horizonte_h):
        aufrufe.append(zone_id)
        return dict(prognose), "test", decay_pp_pro_tag

    motor._prognose_ml_oder_heuristik = _stub
    return aufrufe


def _einzel_motor(prognose, stunde=9, feuchte=45.0, zone=None, **kw):
    from test_entscheidung import baue_motor

    zone = zone or _zone("aktiv")
    motor, speicher = baue_motor(
        zone, feuchte=feuchte, jetzt=TAG.replace(hour=stunde),
        vorhersage=_vorhersage(HEISS_0809), **kw,
    )
    _prognose_stub(motor, prognose)
    return motor, zone


def _kanal_motor(prognose, stunde=9, feuchte=45.0, zone=None, ereignisse=None):
    from test_entscheidung import (
        SpeicherAttrappe,
        WetterClientAttrappe,
        WetterManagerAttrappe,
        baue_messung,
    )
    from bewaesserung.entscheidung import Entscheidungsmotor

    jetzt = TAG.replace(hour=stunde)
    a = (zone or _zone("aktiv")).model_copy(
        update={"zone_id": "zone-a", "ventil_kanal": 1},
    )
    speicher = SpeicherAttrappe()
    speicher.messungen["zone-a"] = [baue_messung(jetzt, feuchte, "zone-a")]
    if ereignisse:
        speicher.heutige_ereignisse["zone-a"] = ereignisse
    motor = Entscheidungsmotor(
        speicher,
        WetterManagerAttrappe(WetterClientAttrappe(_vorhersage(HEISS_0809))),
        [a],
    )
    motor._jetzt = lambda: jetzt
    _prognose_stub(motor, prognose)
    return motor, [a]


@pytest.mark.asyncio
async def test_b_kanalpfad_giesst_vor_dem_fensterschluss():
    """Die Stelle, die die Ventile steuert. 45 % liegt ueber der Schwelle 35,
    aber bis morgen 04:00 faellt die Zone laut Prognose auf 33 %."""
    motor, zonen = _kanal_motor(FALLEND)
    e = await motor.pruefe_kanal(1, zonen)
    assert e.soll_bewaessern is True, e.begruendung
    assert "vorgezogen: jetzt 45%" in e.begruendung, e.begruendung


@pytest.mark.asyncio
async def test_b_kanalpfad_flache_prognose_kein_bedarf():
    motor, zonen = _kanal_motor(FLACH)
    e = await motor.pruefe_kanal(1, zonen)
    assert e.soll_bewaessern is False
    assert e.blocker_typ == BlockerTyp.FEUCHTE_OK, e.begruendung


@pytest.mark.asyncio
async def test_b_kanalpfad_ohne_schluss_fragt_keine_prognose():
    """Kosten-Leitplanke: 07:00 bleibt das Fenster offen -> kein ML-Aufruf."""
    motor, zonen = _kanal_motor(FALLEND, stunde=7)
    aufrufe = _prognose_stub(motor, FALLEND)
    e = await motor.pruefe_kanal(1, zonen)
    assert e.blocker_typ == BlockerTyp.FEUCHTE_OK, e.begruendung
    assert aufrufe == []


@pytest.mark.asyncio
async def test_b_vorhersage_zone_zeigt_dasselbe():
    """UI-Vertrag: die Karte darf nicht "kein Bedarf" sagen, waehrend die
    Engine vorgreift."""
    motor, zone = _einzel_motor(FALLEND)
    empf = await motor.vorhersage_zone(zone.zone_id)
    assert empf.soll_bewaessern is True, empf.grund
    assert "vorgezogen: jetzt 45%" in (empf.grund or ""), empf.grund
    # Der Sensorwert bleibt der Sensorwert.
    assert empf.feuchte_aktuell == 45.0


@pytest.mark.asyncio
async def test_b_vorhersage_zone_flache_prognose_kein_bedarf():
    motor, zone = _einzel_motor(FLACH)
    empf = await motor.vorhersage_zone(zone.zone_id)
    assert empf.soll_bewaessern is False
    assert empf.blocker_typ == BlockerTyp.FEUCHTE_OK


@pytest.mark.asyncio
async def test_b_pruefe_zone_giesst_vor_dem_fensterschluss():
    motor, zone = _einzel_motor(FALLEND)
    e = await motor.pruefe_zone(zone.zone_id)
    assert e.soll_bewaessern is True, e.begruendung
    assert "vorgezogen" in e.begruendung


@pytest.mark.asyncio
async def test_b_pruefe_zone_flache_prognose_kein_bedarf():
    motor, zone = _einzel_motor(FLACH)
    e = await motor.pruefe_zone(zone.zone_id)
    assert e.blocker_typ == BlockerTyp.FEUCHTE_OK


# ====================================================== C: Leitplanken

TIEF = {6: 30.0, 12: 20.0, 24: 5.0}   # Prognose weit unter kritisch 20


def _budget_zone():
    """Budget 120 s, bei Kritisch x3. Heute schon 200 s gegossen (nachts,
    ausserhalb Karenz und Mindestpause)."""
    return _zone("aktiv").model_copy(update={
        "tages_budget_sekunden": 120.0, "tages_budget_kritisch_faktor": 3.0,
    })


def _nachtlauf(zone_id):
    from test_entscheidung import baue_ventil_ereignis

    return [baue_ventil_ereignis(TAG.replace(hour=1), 200, zone_id=zone_id)]


@pytest.mark.asyncio
async def test_c_kanalpfad_kritisch_haengt_am_messwert():
    """Die Prognose (5 %) liegt unter kritisch, der Sensor (45 %) nicht. Die
    Kritisch-Notreserve darf NICHT aufgehen -- sonst oeffnet eine Vorhersage
    die Schranke fuer echte Not."""
    motor, zonen = _kanal_motor(
        TIEF, zone=_budget_zone(), ereignisse=_nachtlauf("zone-a"),
    )
    e = await motor.pruefe_kanal(1, zonen)
    assert e.soll_bewaessern is False, e.begruendung
    assert e.blocker_typ == BlockerTyp.BUDGET_ERSCHOEPFT, e.begruendung


@pytest.mark.asyncio
async def test_c_vorhersage_zone_kritisch_haengt_am_messwert():
    zone = _budget_zone()
    motor, _ = _einzel_motor(TIEF, zone=zone, heutige_ereignisse=_nachtlauf(zone.zone_id))
    empf = await motor.vorhersage_zone(zone.zone_id)
    assert empf.blocker_typ == BlockerTyp.BUDGET_ERSCHOEPFT, empf.grund


@pytest.mark.asyncio
async def test_c_pruefe_zone_kritisch_haengt_am_messwert():
    zone = _budget_zone()
    motor, _ = _einzel_motor(TIEF, zone=zone, heutige_ereignisse=_nachtlauf(zone.zone_id))
    e = await motor.pruefe_zone(zone.zone_id)
    assert e.blocker_typ == BlockerTyp.BUDGET_ERSCHOEPFT, e.begruendung


@pytest.mark.asyncio
async def test_c_kurz_nach_einem_lauf_kein_vorgriff():
    """Sensor-Nachlauf (T-0286): 1 h nach einem Lauf hat der Messwert die
    Gabe noch nicht gesehen. Die Prognose darauf wuerde doppelt giessen."""
    from test_entscheidung import baue_ventil_ereignis

    motor, zonen = _kanal_motor(FALLEND)
    lauf = baue_ventil_ereignis(TAG.replace(hour=8), 600, zone_id="zone-a")
    from bewaesserung.modelle import VentilAktion
    lauf = lauf.model_copy(update={"aktion": VentilAktion.SCHLIESSEN})
    motor._speicher.letzte_ereignisse["zone-a"] = lauf
    e = await motor.pruefe_kanal(1, zonen)
    assert e.blocker_typ == BlockerTyp.FEUCHTE_OK, e.begruendung


# ------------------------- B2: Dosis aus dem vorhergesagten Wert (Andre: B)

def _ungekappt():
    """Ohne hohe Obergrenze landen alle Dosen bei `max_dauer_sekunden` (1800)
    -- dann waeren "gleich" und "verschieden" nicht unterscheidbar (so in der
    ersten Fassung dieses Tests passiert, die Kontrollzeile schlug an)."""
    return _zone("aktiv").model_copy(update={"max_dauer_sekunden": 36000})

@pytest.mark.asyncio
async def test_b_kanal_dosis_wie_bei_gemessenem_prognosewert():
    """Vorgezogen giesst die Dosis, die die Zone beim vorhergesagten Stand
    bekaeme -- nicht die kleinere fuer 45 %. Vergleich: derselbe Zeitpunkt,
    dieselbe Wetterlage, der Sensor zeigt direkt den Prognosewert."""
    vor, zonen = _kanal_motor(FALLEND, zone=_ungekappt())
    e_vor = await vor.pruefe_kanal(1, zonen)
    wert = feuchte_nach_stunden(45.0, FALLEND, 15.0, 19.0)   # exakt, nicht der Text
    direkt, zonen_d = _kanal_motor(FALLEND, feuchte=wert, zone=_ungekappt())
    e_direkt = await direkt.pruefe_kanal(1, zonen_d)
    assert e_vor.soll_bewaessern and e_direkt.soll_bewaessern
    assert e_vor.dauer_sekunden == e_direkt.dauer_sekunden, (
        e_vor.dauer_sekunden, e_direkt.dauer_sekunden,
    )
    # Und die Dosis haengt ueberhaupt am Wert (sonst beweist der Vergleich nichts).
    hoch, zonen_h = _kanal_motor(FALLEND, feuchte=20.0, zone=_ungekappt())
    e_hoch = await hoch.pruefe_kanal(1, zonen_h)
    assert e_hoch.dauer_sekunden != e_direkt.dauer_sekunden


@pytest.mark.asyncio
async def test_b_vorhersage_zone_dosis_wie_bei_gemessenem_prognosewert():
    vor, zone = _einzel_motor(FALLEND, zone=_ungekappt())
    empf_vor = await vor.vorhersage_zone(zone.zone_id)
    wert = feuchte_nach_stunden(45.0, FALLEND, 15.0, 19.0)   # exakt, nicht der Text
    direkt, _ = _einzel_motor(FALLEND, feuchte=wert, zone=_ungekappt())
    empf_direkt = await direkt.vorhersage_zone(zone.zone_id)
    assert empf_vor.dauer_s_empfehlung == empf_direkt.dauer_s_empfehlung
    assert empf_vor.dauer_s_heuristik == empf_direkt.dauer_s_heuristik
    hoch, _ = _einzel_motor(FALLEND, feuchte=20.0, zone=_ungekappt())
    assert (await hoch.vorhersage_zone(zone.zone_id)).dauer_s_empfehlung \
        != empf_direkt.dauer_s_empfehlung


# ------------- B3: Strategie "selten, dafuer viel" (waldblumenhain) prueft mit

def _selten_gross_motor(motor):
    """Welkepunkt 20, Feldkapazitaet 60. Mit Decay 15 pp/Tag: gemessen 45 %
    -> 1,67 Tage bis Welkepunkt (kein Bedarf, Grenze 1,5), vorhergesagt
    33,3 % -> 0,89 Tage (akut). Die Strategie muss den VORHERGESAGTEN Wert
    sehen, sonst verwirft sie den Vorgriff."""
    async def _kalib(zone, jetzt):
        return 20.0, "test", 60.0, "test"

    async def _physik(zone, feuchte, welkepunkt, jetzt):
        return None

    motor._hole_kalibrier_referenzen = _kalib
    motor._physik_reserve_tage = _physik
    return motor


def _selten_gross_zone():
    from bewaesserung.modelle import BewaesserungsStrategie

    return _zone("aktiv").model_copy(update={
        "bewaesserungs_strategie": BewaesserungsStrategie.SELTEN_GROSS,
    })


@pytest.mark.asyncio
async def test_b_selten_gross_kanalpfad_strategie_sieht_die_prognose():
    motor, zonen = _kanal_motor(FALLEND, zone=_selten_gross_zone())
    e = await _selten_gross_motor(motor).pruefe_kanal(1, zonen)
    assert e.soll_bewaessern is True, e.begruendung


@pytest.mark.asyncio
async def test_b_selten_gross_vorhersage_zone_strategie_sieht_die_prognose():
    motor, zone = _einzel_motor(FALLEND, zone=_selten_gross_zone())
    empf = await _selten_gross_motor(motor).vorhersage_zone(zone.zone_id)
    assert empf.soll_bewaessern is True, (empf.empfehlungs_typ, empf.grund)


@pytest.mark.asyncio
async def test_b_selten_gross_ohne_vorgriff_kein_bedarf():
    """Kontrolle: bei flacher Prognose sagt die Strategie weiter nein -- der
    Test oben beweist also den Vorgriff, nicht eine ohnehin giessende Zone."""
    motor, zone = _einzel_motor(FLACH, zone=_selten_gross_zone())
    empf = await _selten_gross_motor(motor).vorhersage_zone(zone.zone_id)
    assert empf.soll_bewaessern is False


@pytest.mark.asyncio
async def test_b_konstant_niedrig_klassifikation_sieht_die_prognose():
    """KONSTANT_NIEDRIG loest am Feuchtewert selbst aus (<= Welkepunkt + 5).
    Welkepunkt 30, langsamer Decay (1 pp/Tag, damit die Welkepunkt-Reserve
    NICHT akut ist): gemessen 45 % -> kein Bedarf, vorhergesagt 33,3 % ->
    akut. Die Klassifikation muss den vorhergesagten Wert bekommen."""
    from bewaesserung.modelle import BewaesserungsStrategie

    zone = _zone("aktiv").model_copy(update={
        "bewaesserungs_strategie": BewaesserungsStrategie.KONSTANT_NIEDRIG,
    })
    motor, _ = _einzel_motor(FALLEND, zone=zone)
    _prognose_stub(motor, FALLEND, decay_pp_pro_tag=1.0)

    async def _kalib(zone, jetzt):
        return 30.0, "test", 60.0, "test"

    motor._hole_kalibrier_referenzen = _kalib
    empf = await motor.vorhersage_zone(zone.zone_id)
    assert empf.soll_bewaessern is True, (empf.empfehlungs_typ, empf.grund)
