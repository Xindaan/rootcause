"""T-0573: eine ML-Prognose muss ihre eigene Vertrauenswuerdigkeit tragen.

Realfall Maxibaer, Screenshot 09.09.2026: auf EINER Karte standen der
Istwert 30 und daneben "24h: 66". Beide Zahlen waren "richtig gerechnet" --
nur gehoerten sie zu verschiedenen Sensoren. Unter `fyta_900001` steckt
seit dem 08.09. 06:00 ein FYTA Terra statt des vorherigen Beam; das
`ml_ausschluss_fenster` schneidet alle Messungen ab diesem Zeitpunkt aus
dem Feature-Frame, also blieb die juengste verwertbare Zeile fuer immer
der 08.09. 05:55 -- eine Beam-Zeile mit 66 %.

**Gemessen am 09.09. 07:16, gegen die Vermutung im Task:** der ML-Pfad
steht NICHT still. `live_vorhersage` rechnet bei jedem Poll weiter, nur
eben ewig auf derselben eingefrorenen Zeile. Dass `ml_vorhersage_log`
seit dem 08.09. 09:22 keine neue Zeile hat, ist die Folge des
`INSERT OR IGNORE` auf `feature_zeitstempel` (Dedup gegen Polling), kein
zweiter Defekt. Der Fix gehoert deshalb an die Auslieferung, nicht an den
Job.

Fehlerklasse: [[fehlerpattern_detektor_ohne_konsument]] -- das
Ausschlussfenster steht in der Konfig, die Pipeline respektiert es, und
die Anzeige erfaehrt nie davon.

Geprueft wird pro Akzeptanzkriterium des Tasks, jeweils mit Negativprobe
(die den EINZELNEN Waechter entfernt, nicht "den Fix"):

  AK2 Alter        -> test_veraltete_prognose_*  / test_frische_prognose_*
  AK3 Sensortausch -> test_geraetewechsel_*      / test_gleicher_sensor_*

Zusaetzlich die NAHT: `_ml_eintrag_dict` ist das Dict, das die Karte
wirklich bekommt. Ein Test nur auf `bewerte_prognose` waere ein
"kennt das Modul den Helfer"-Test und wuerde nicht zeigen, ob das Urteil
je beim Frontend ankommt.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.ml.modelle_ml import MLVorhersage
from bewaesserung.ml.prognose_guete import (
    GRUND_GERAETEWECHSEL,
    GRUND_HERKUNFT_UNBEKANNT,
    GRUND_VERALTET,
    PROGNOSE_MAX_RUECKSTAND_STUNDEN,
    bewerte_prognose,
    geraet_gewechselt,
    sensor_id_zum_zeitpunkt,
)
from bewaesserung.modelle import FytaGeraeteStatus
from bewaesserung.speicher import Speicher

# Die echten Werte aus dem Realfall -- nicht erfunden, sondern am
# 09.09.2026 aus `fyta_geraete_status` gelesen.
JETZT = datetime(2026, 9, 9, 7, 16)
FEATURE_ZEIT = datetime(2026, 9, 8, 5, 55, 15)   # letzte Beam-Zeile
MAC_BEAM = "mac-beam-1"
MAC_TERRA = "mac-terra-1"
GERAET = "fyta_900001"

HISTORIE_MIT_WECHSEL = {
    GERAET: [
        (datetime(2026, 9, 7, 4, 23, 10), MAC_BEAM),
        (datetime(2026, 9, 8, 6, 38, 38), MAC_TERRA),
    ],
}
HISTORIE_OHNE_WECHSEL = {
    GERAET: [(datetime(2026, 9, 7, 4, 23, 10), MAC_BEAM)],
}


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- AK2

# Die juengste Messung der Zone am Stichtag: der Terra meldete um 06:57,
# also 25 h NACH der Feature-Zeile, auf der die 66 gerechnet wurde.
LETZTE_MESSUNG = datetime(2026, 9, 9, 6, 57, 43)


def test_veraltete_prognose_ist_ungueltig():
    """AK2: die Prognose ignoriert Messungen, die laengst vorliegen.

    Hier bewusst OHNE Geraetewechsel-Historie, sonst wuerde der Test
    auch dann gruen bleiben, wenn nur der Wechsel-Waechter greift --
    die Falle "zwei Ursachen liefern dasselbe Signal".
    """
    g = bewerte_prognose(
        FEATURE_ZEIT, GERAET, JETZT, HISTORIE_OHNE_WECHSEL, LETZTE_MESSUNG,
    )
    assert g.gueltig is False
    assert g.grund == GRUND_VERALTET
    assert g.rueckstand_stunden == pytest.approx(25.04, abs=0.05)
    # Das Wanduhr-Alter bleibt als Tooltip-Information erhalten.
    assert g.alter_stunden == pytest.approx(25.35, abs=0.05)


def test_frische_prognose_bleibt_gueltig():
    """Negativprobe zu AK2: der Waechter darf gesunde Zonen nicht kippen."""
    zeit = JETZT - timedelta(minutes=21)
    g = bewerte_prognose(
        zeit, GERAET, JETZT, HISTORIE_OHNE_WECHSEL, zeit,
    )
    assert g.gueltig is True
    assert g.grund is None


def test_langsame_zone_bleibt_gueltig():
    """Kontrollbedingung: langsame Cadence ist keine Luege.

    Gemessen am 09.09.2026 07:16 gegen die Produktions-DB: avocado
    (10,3 h), fuchsie (10,1 h), kroton, pilea und zitrus_ii (je ~4,1 h)
    rechnen auf Zeilen, die alt sind -- aber es sind ihre JUENGSTEN.
    Bluetooth-only-FYTA, User-Sync alle 2-4 Tage (T-0214). Eine
    Wanduhr-Schwelle von 3 h haette 5 von 14 Zonen stumm geschaltet,
    ohne dass eine einzige von ihnen etwas Falsches behauptet.

    Bricht dieser Test, ist die Schwelle wieder gegen die Wanduhr
    gemessen -- genau der Fehlgriff, den die Messung ausgeschlossen hat.
    """
    alt = JETZT - timedelta(hours=10, minutes=18)
    g = bewerte_prognose(alt, "fyta_120000", JETZT, {}, alt)
    assert g.gueltig is True
    assert g.rueckstand_stunden == pytest.approx(0.0, abs=0.01)
    assert g.alter_stunden == pytest.approx(10.3, abs=0.05)


def test_grenze_ist_exklusiv():
    """Genau auf dem Horizont noch gueltig, eine Minute darueber nicht.

    Ohne diesen Fall koennte `>` gegen `>=` getauscht werden, ohne dass
    ein Test es merkt.
    """
    h = PROGNOSE_MAX_RUECKSTAND_STUNDEN
    feature = JETZT - timedelta(hours=12)
    auf_grenze = bewerte_prognose(
        feature, GERAET, JETZT, HISTORIE_OHNE_WECHSEL,
        feature + timedelta(hours=h),
    )
    knapp_drueber = bewerte_prognose(
        feature, GERAET, JETZT, HISTORIE_OHNE_WECHSEL,
        feature + timedelta(hours=h, minutes=1),
    )
    assert auf_grenze.gueltig is True
    assert knapp_drueber.gueltig is False


def test_ohne_messzeit_faellt_auf_wanduhr_zurueck():
    """Fehlt die Bezugsmessung, darf der Waechter nicht stumm werden."""
    g = bewerte_prognose(FEATURE_ZEIT, GERAET, JETZT, HISTORIE_OHNE_WECHSEL)
    assert g.gueltig is False
    assert g.grund == GRUND_VERALTET


# ---------------------------------------------------------------- AK3

def test_geraetewechsel_macht_prognose_ungueltig():
    """AK3: unter derselben `geraet_id` steckt inzwischen ein anderes Geraet.

    Der Test prueft auf `GRUND_GERAETEWECHSEL`, nicht nur auf
    `gueltig is False` -- die Prognose ist hier NAEMLICH AUCH veraltet,
    ein Test auf das blosse Flag koennte also aus dem falschen Grund
    gruen sein.
    """
    g = bewerte_prognose(
        FEATURE_ZEIT, GERAET, JETZT, HISTORIE_MIT_WECHSEL, LETZTE_MESSUNG,
    )
    assert g.gueltig is False
    assert g.grund == GRUND_GERAETEWECHSEL


def test_geraetewechsel_greift_auch_bei_frischer_zeile():
    """Der Wechsel-Waechter braucht das Alter NICHT.

    Sonst waere er nur eine zweite Formulierung der Alterspruefung. Ein
    Sensortausch vor 10 Minuten macht die Prognose sofort ungueltig,
    auch wenn die Feature-Zeile taufrisch ist.
    """
    feature = JETZT - timedelta(minutes=30)
    historie = {
        GERAET: [
            (JETZT - timedelta(hours=2), MAC_BEAM),
            (JETZT - timedelta(minutes=10), MAC_TERRA),
        ],
    }
    g = bewerte_prognose(feature, GERAET, JETZT, historie, JETZT)
    assert g.gueltig is False
    assert g.grund == GRUND_GERAETEWECHSEL


def test_gleicher_sensor_bleibt_gueltig():
    """Negativprobe zu AK3: kein Wechsel -> kein Veto."""
    zeit = JETZT - timedelta(minutes=20)
    g = bewerte_prognose(
        zeit, GERAET, JETZT, HISTORIE_MIT_WECHSEL, zeit,
    )
    # Die Feature-Zeile liegt NACH dem Wechsel, also ist der Sensor
    # derselbe wie jetzt.
    assert g.gueltig is True


def test_pflanze_ohne_geraet_zaehlt_als_wechsel():
    """T-0571-Anschluss: leere `sensor_id` = kein Geraet mehr dran.

    Das ist eine echte Aussage, kein fehlender Wert -- eine Prognose aus
    der Zeit davor gehoert zu einem Sensor, der nicht mehr in der Zone
    ist.
    """
    historie = {
        GERAET: [
            (JETZT - timedelta(hours=5), MAC_BEAM),
            (JETZT - timedelta(hours=1), ""),
        ],
    }
    g = bewerte_prognose(
        JETZT - timedelta(hours=4), GERAET, JETZT, historie, JETZT,
    )
    assert g.grund == GRUND_GERAETEWECHSEL


def test_ohne_historie_kein_behaupteter_wechsel():
    """Konservativ: unbekannt ist kein Beleg.

    Wenn die Historie nicht bis zur Feature-Zeile zurueckreicht, darf der
    Waechter keinen Wechsel BEHAUPTEN -- sonst wuerde jede Zone ohne
    FYTA-Statusdaten (alle Gardena-Zonen) dauerhaft stumm.
    """
    assert geraet_gewechselt(None, FEATURE_ZEIT) is False
    assert geraet_gewechselt([], FEATURE_ZEIT) is False
    # Historie beginnt NACH der Feature-Zeile -> kein Eintrag davor.
    spaet = [(JETZT - timedelta(minutes=5), MAC_TERRA)]
    assert geraet_gewechselt(spaet, FEATURE_ZEIT) is False
    zeit2 = JETZT - timedelta(minutes=20)
    g = bewerte_prognose(zeit2, GERAET, JETZT, {}, zeit2)
    assert g.gueltig is True


def test_sensor_id_zum_zeitpunkt():
    """Der Nachschlag selbst: juengster Eintrag VOR dem Zeitpunkt."""
    h = HISTORIE_MIT_WECHSEL[GERAET]
    assert sensor_id_zum_zeitpunkt(h, FEATURE_ZEIT) == MAC_BEAM
    assert sensor_id_zum_zeitpunkt(h, JETZT) == MAC_TERRA
    assert sensor_id_zum_zeitpunkt(h, datetime(2026, 9, 1)) is None


def test_fehlende_herkunft_ist_ungueltig():
    """Ohne `feature_zeitstempel` ist keine Pruefung moeglich.

    Die Zahl trotzdem zu zeigen hiesse, genau die Luecke offenzulassen,
    die dieser Task schliesst.
    """
    g = bewerte_prognose(None, GERAET, JETZT, {})
    assert g.gueltig is False
    assert g.grund == GRUND_HERKUNFT_UNBEKANNT
    assert g.alter_stunden is None


# ------------------------------------------------- Naht zur Auslieferung

def _mlv(feature_zeit, geraet):
    return MLVorhersage(
        zone_id="mandevilla_maxi",
        zeitstempel=JETZT,
        horizont_stunden=24,
        feuchte_aktuell=66.0,
        feuchte_prognose=66.23,
        feature_zeitstempel=feature_zeit,
        inferenz_geraet_id=geraet,
    )


def _urteile(mlv, historie, letzte):
    """T-0577: das Urteil entsteht seit 16.09. an EINER Stelle,
    `MLVorhersageService._mit_guete`. Die Tests laufen ueber genau diese
    Stelle, damit sie die echte Kette pruefen: Urteil -> Modell -> API-Dict."""
    from bewaesserung.ml.vorhersage import MLVorhersageService

    svc = MLVorhersageService.__new__(MLVorhersageService)
    return svc._mit_guete(mlv, "mandevilla_maxi", JETZT, historie,
                          {"mandevilla_maxi": letzte})


def test_api_dict_traegt_das_urteil():
    """Die Naht: kommt das Urteil im Dict an, das die Karte bekommt?

    T-0577: das Dict RECHNET das Urteil nicht mehr, es LIEST es von der
    Prognose. Geprueft wird deshalb die ganze Kette ab der Urteilsstelle --
    sonst koennte `_mit_guete` perfekt funktionieren und das Dict es trotzdem
    fallen lassen.
    """
    from bewaesserung.api_server import _ml_eintrag_dict

    d = _ml_eintrag_dict(
        _urteile(_mlv(FEATURE_ZEIT, GERAET), HISTORIE_MIT_WECHSEL,
                 LETZTE_MESSUNG),
    )
    assert d["gueltig"] is False
    assert d["ungueltig_grund"] == GRUND_GERAETEWECHSEL
    assert d["feature_alter_h"] == pytest.approx(25.35, abs=0.05)
    assert d["geraet_id"] == GERAET
    # Der Wert selbst bleibt im Dict -- der Inspektor darf ihn sehen,
    # nur die Karte zeigt ihn nicht mehr als Prognose.
    assert d["feuchte_prognose"] == 66.23


def test_api_dict_frische_prognose():
    """Negativprobe zur Naht: gesunde Prognose bleibt unveraendert nutzbar."""
    from bewaesserung.api_server import _ml_eintrag_dict

    frisch = JETZT - timedelta(minutes=15)
    d = _ml_eintrag_dict(
        _urteile(_mlv(frisch, GERAET), HISTORIE_OHNE_WECHSEL, frisch),
    )
    assert d["gueltig"] is True
    assert d["ungueltig_grund"] is None
    assert d["feuchte_prognose"] == 66.23


def test_inferenz_setzt_herkunft_am_modell():
    """T-0567/T-0573: `MLVorhersage` traegt die Felder ueberhaupt.

    Wuerde `live_vorhersage` sie nicht setzen, waere jede Prognose
    `herkunft_unbekannt` -- das faellt hier auf, bevor es in der UI
    auffaellt.
    """
    v = _mlv(FEATURE_ZEIT, GERAET)
    assert v.feature_zeitstempel == FEATURE_ZEIT
    assert v.inferenz_geraet_id == GERAET
    leer = MLVorhersage(
        zone_id="z", zeitstempel=JETZT, horizont_stunden=6,
        feuchte_aktuell=1.0, feuchte_prognose=1.0,
    )
    assert leer.feature_zeitstempel is None


# ------------------------------------------------------ Speicher-Historie

@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "t0573.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _status(sp, geraet, ts, sensor_id, zone="mandevilla_maxi"):
    _run(sp.speichere_fyta_geraete_status(FytaGeraeteStatus(
        zeitstempel=ts, geraet_id=geraet, plant_id=900001,
        sensor_id=sensor_id, zone_id=zone, battery_level=90.0,
        is_battery_low=False, sensor_status=1, wifi_status=1,
        hub_status=1, is_outdated=False, firmware="1.0",
        last_data_received_at=ts,
    )))


def test_historie_liefert_nur_wechselpunkte(speicher):
    """Die Tabelle schreibt alle paar Stunden eine Momentaufnahme; fuer
    die Pruefung zaehlen nur die Punkte, an denen sich die MAC aendert."""
    _status(speicher, GERAET, datetime(2026, 9, 7, 4, 23), MAC_BEAM)
    _status(speicher, GERAET, datetime(2026, 9, 7, 10, 25), MAC_BEAM)
    _status(speicher, GERAET, datetime(2026, 9, 8, 4, 39), MAC_BEAM)
    _status(speicher, GERAET, datetime(2026, 9, 8, 6, 38), MAC_TERRA)
    _status(speicher, GERAET, datetime(2026, 9, 8, 13, 3), MAC_TERRA)

    h = _run(speicher.hole_sensor_id_historie(datetime(2026, 9, 1)))
    assert [x[1] for x in h[GERAET]] == [MAC_BEAM, MAC_TERRA]
    assert h[GERAET][1][0] == datetime(2026, 9, 8, 6, 38)
    # Und das Urteil daraus stimmt mit dem Realfall ueberein.
    assert geraet_gewechselt(h[GERAET], FEATURE_ZEIT) is True


def test_historie_respektiert_seit(speicher):
    """`seit` schneidet aeltere Zeilen ab -- sonst waechst der Lookup mit
    der Tabelle."""
    _status(speicher, GERAET, datetime(2026, 8, 1, 12, 0), MAC_BEAM)
    _status(speicher, GERAET, datetime(2026, 9, 8, 6, 38), MAC_TERRA)
    h = _run(speicher.hole_sensor_id_historie(datetime(2026, 9, 1)))
    assert [x[1] for x in h[GERAET]] == [MAC_TERRA]


# ------------------------------------------------ AK6: der Gegenfall
#
# Die andere Haelfte desselben Defekts: die Karte VERSCHWEIGT einen
# Messwert, den sie hat. Gemessen am 09.09.2026 07:16 traf das fuenf
# Zonen -- pilea 4,1 h / kroton 4,2 h / zitrus_ii 4,3 h / fuchsie 10,1 h /
# avocado 10,3 h. Alle jenseits von `AGGREGAT_FALLBACK_FENSTER_MIN`
# (240 min), alle Bluetooth-only-FYTA mit schubweisem Sync (T-0214).
#
# Das Abruf-Fenster darf dabei NICHT aufgeweitet werden: T-0476 koppelt
# Anzeige und Entscheidung bewusst, und ein 10 h alter Wert darf kein
# Ventil oeffnen. Deshalb bleibt `aktuelle_feuchte` None -- getestet wird
# genau diese Trennung.

def _messung(ts, feuchte=10.0, geraet="fyta_129000"):
    from bewaesserung.modelle import DatenQuelle, SensorMessung
    return SensorMessung(
        zeitstempel=ts, zone_id="pilea", geraet_id=geraet,
        boden_feuchte=feuchte, boden_temperatur=19.0,
        batterie_prozent=88.0, quelle=DatenQuelle.FYTA,
    )


def test_ak6_alter_wert_wird_gezeigt_statt_verschwiegen():
    """AK6: kein frischer Wert, aber ein bekannter -> Zahl + Alter."""
    from bewaesserung.api_server import _letzter_bekannter_felder

    alt = JETZT - timedelta(hours=10, minutes=18)
    f = _letzter_bekannter_felder(None, _messung(alt), JETZT)
    assert f["letzter_bekannter_wert"] == 10.0
    assert f["letzter_bekannter_alter_h"] == pytest.approx(10.3, abs=0.05)
    assert f["letzter_bekannter_geraet_id"] == "fyta_129000"


def test_ak6_frischer_wert_hat_vorrang():
    """Negativprobe zu AK6: solange ein frischer Wert existiert, bleiben
    die Rueckfall-Felder LEER.

    Sonst stuenden zwei Wahrheiten nebeneinander und die Karte muesste
    raten, welche sie zeigt.
    """
    from bewaesserung.api_server import _letzter_bekannter_felder

    frisch = _messung(JETZT - timedelta(minutes=10))
    f = _letzter_bekannter_felder(frisch, _messung(JETZT - timedelta(hours=9)), JETZT)
    assert f["letzter_bekannter_wert"] is None
    assert f["letzter_bekannter_alter_h"] is None


def test_ak6_ohne_messwert_bleibt_leer():
    """Eine Messung ohne `boden_feuchte` ist kein anzeigbarer Wert.

    Geprueft wird auf ZEIT und GERAET, nicht auf den Wert: der waere auch
    ohne den Waechter None (er ist ja None), der Test kann also aus dem
    falschen Grund gruen sein. Ohne den Waechter wuerden Zeitstempel und
    Alter gesetzt -- die Karte behauptete dann "zuletzt vor 5 h" zu einem
    Wert, den es nicht gibt. Genau die Fehlerklasse dieses Tasks.
    """
    from bewaesserung.api_server import _letzter_bekannter_felder

    ohne = _messung(JETZT - timedelta(hours=5))
    ohne = ohne.model_copy(update={"boden_feuchte": None})
    f = _letzter_bekannter_felder(None, ohne, JETZT)
    assert f["letzter_bekannter_wert"] is None
    assert f["letzter_bekannter_zeit"] is None
    assert f["letzter_bekannter_alter_h"] is None
    assert f["letzter_bekannter_geraet_id"] is None

    f2 = _letzter_bekannter_felder(None, None, JETZT)
    assert f2["letzter_bekannter_zeit"] is None


def test_ak6_kappt_an_der_zonen_schwelle():
    """Jenseits von `ausfall_schwelle_stunden` ist es ein echter Ausfall.

    Der Rueckfall soll ehrlich machen, nicht einen Uralt-Wert
    konservieren. Gekappt wird in `_letzter_bekannter_bulk`; hier der
    Vertrag der Kappungs-Rechnung.
    """
    from bewaesserung.api_server import ANZEIGE_RUECKFALL_STUNDEN_DEFAULT

    # Der Default ist derselbe Backstop wie im Entscheidungspfad -- keine
    # dritte Zahl im System.
    from bewaesserung.entscheidung import MAX_FALLBACK_ALTER_STUNDEN
    assert ANZEIGE_RUECKFALL_STUNDEN_DEFAULT == MAX_FALLBACK_ALTER_STUNDEN
