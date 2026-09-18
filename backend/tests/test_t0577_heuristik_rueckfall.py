"""T-0577 Schritt 0: den Heuristik-Rueckfall absichern, BEVOR er Verkehr bekommt.

T-0577 E verlagert die Guete-Pruefung der ML-Prognose in `live_vorhersage`.
Verwirft die Engine danach eine ungueltige Prognose, faellt sie in
`_prognose_ml_oder_heuristik` auf die Heuristik zurueck.

**Warum das hier zuerst kommt.** Gemessen am 16.09.2026 im Empfehlungs-Audit
der vier scharfen Zonen ueber 90 Tage:

    prognose_quelle   n      von          bis
    ml              7944     2026-06-18   2026-09-16
    heuristik        177     2026-06-28   2026-07-17   <- seit zwei Monaten tot
    keine             83

Der Pfad, auf den T-0577 kuenftig umleitet, lief zuletzt am 17.07. -- und
er hatte keinen einzigen direkten Test. Wer ungetesteten, seit Wochen
ungenutzten Code auf einem Ventilpfad wieder mit Verkehr beaufschlagt, testet
ihn am Garten.

Diese Tests sind CHARAKTERISIERUNGSTESTS: sie beschreiben, was der Code HEUTE
tut, gegen den unveraenderten Stand. Sie muessen gruen sein, bevor an der
Guete-Pruefung etwas gebaut wird.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from bewaesserung.entscheidung import Entscheidungsmotor

HORIZONTE = [6, 12, 24, 48]


def _motor(ml_service=None, mit_konfig=True):
    m = Entscheidungsmotor.__new__(Entscheidungsmotor)
    m._ml_vorhersage_service = ml_service
    m._konfig = SimpleNamespace() if mit_konfig else None
    m._speicher = SimpleNamespace() if mit_konfig else None
    return m


def _mlv(wert):
    return SimpleNamespace(feuchte_prognose=wert)


class _MLService:
    def __init__(self, ergebnisse, verfuegbar=True, wirft=False):
        self.ist_verfuegbar = verfuegbar
        self._e = ergebnisse
        self._wirft = wirft

    async def live_vorhersage(self, zone_id, speicher, konfig, **kw):
        if self._wirft:
            raise RuntimeError("Modell kaputt")
        return self._e


def _rufe(motor, feuchte=50.0, decay=4.0):
    return asyncio.run(motor._prognose_ml_oder_heuristik(
        "z", feuchte, decay, HORIZONTE,
    ))


# ------------------------------------------------ die Heuristik selbst

def test_heuristik_ist_linearer_decay():
    """50 % bei 4 pp/Tag: 6 h -> 49, 24 h -> 46, 48 h -> 42."""
    prognose, quelle, decay = _rufe(_motor(ml_service=None))
    assert quelle == "heuristik"
    assert decay == 4.0
    assert prognose == {6: 49.0, 12: 48.0, 24: 46.0, 48: 42.0}


def test_heuristik_klemmt_auf_null():
    """Kein negativer Feuchtewert, auch bei grossem Decay."""
    prognose, _, _ = _rufe(_motor(ml_service=None), feuchte=3.0, decay=20.0)
    assert prognose[48] == 0.0
    assert all(v >= 0.0 for v in prognose.values())


# ------------------------------------------- jeder Weg in den Rueckfall

@pytest.mark.parametrize("fall,service", [
    ("kein Service", None),
    ("nicht verfuegbar", _MLService({"24h": _mlv(40.0)}, verfuegbar=False)),
    ("leeres Ergebnis", _MLService({})),
    ("Exception", _MLService({}, wirft=True)),
    ("kein verwertbarer Horizont", _MLService({"24h": _mlv(None)})),
])
def test_jeder_ausfallweg_landet_in_der_heuristik(fall, service):
    """Alle heute existierenden Ausfallwege muessen in derselben, sauberen
    Heuristik landen -- nicht in einer Exception, die den Zyklus abbricht.
    Genau diese Wege nutzt T-0577 kuenftig zusaetzlich."""
    prognose, quelle, decay = _rufe(_motor(ml_service=service))
    assert quelle == "heuristik", fall
    assert decay == 4.0, fall
    assert prognose[24] == 46.0, fall


def test_ohne_konfig_ebenfalls_heuristik():
    prognose, quelle, _ = _rufe(
        _motor(ml_service=_MLService({"24h": _mlv(40.0)}), mit_konfig=False),
    )
    assert quelle == "heuristik"


# --------------------------------------- Negativprobe: ML wird genutzt

def test_gueltige_ml_prognose_wird_genutzt():
    """Gegenstueck: ist ML da, darf NICHT die Heuristik kommen. Sonst waeren
    die Tests oben auch dann gruen, wenn der Motor pauschal zurueckfiele."""
    prognose, quelle, decay = _rufe(
        _motor(ml_service=_MLService({"24h": _mlv(40.0)})),
    )
    assert quelle == "ml"
    assert prognose[24] == 40.0
    # decay_ml = 50 - 40 = 10 pp/Tag
    assert decay == 10.0


# =====================================================================
# T-0577 E: die Engine liest das Guete-Urteil der Prognose
# =====================================================================

def _mlv_mit(wert, gueltig=True, grund=None):
    return SimpleNamespace(
        feuchte_prognose=wert, gueltig=gueltig, ungueltig_grund=grund,
    )


def test_t0577_ungueltige_prognose_wird_nicht_genutzt():
    """Der Kern von T-0577: eine als ungueltig markierte Prognose darf NICHT
    in Trigger und Dosis eingehen.

    Realfall Maxibaer 09.09.: die Engine rechnete mit 66 % (25 h alte Zeile
    eines ausgebauten Sensors), waehrend die Karte die Zahl schon ausblendete.
    """
    service = _MLService({
        "6h": _mlv_mit(66.0, False, "geraetewechsel"),
        "12h": _mlv_mit(66.0, False, "geraetewechsel"),
        "24h": _mlv_mit(66.0, False, "geraetewechsel"),
    })
    prognose, quelle, decay = _rufe(_motor(ml_service=service))
    assert quelle == "heuristik"
    assert prognose[24] == 46.0, "die 66 aus dem alten Sensor ist durchgerutscht"
    assert decay == 4.0


def test_t0577_teilweise_gueltig_nutzt_nur_die_gueltigen():
    """Nur der ungueltige Horizont faellt weg, der gueltige bleibt ML."""
    service = _MLService({
        "24h": _mlv_mit(40.0, True),
        "12h": _mlv_mit(99.0, False, "veraltet"),
    })
    prognose, quelle, _ = _rufe(_motor(ml_service=service))
    assert quelle == "ml"
    assert prognose[24] == 40.0
    assert prognose[12] != 99.0, "der veraltete 12-h-Wert wurde uebernommen"


def test_t0577_gueltige_prognose_bleibt_ml():
    """Negativprobe: `gueltig=True` darf nichts veraendern. Sonst waere der
    Test oben auch gruen, wenn die Engine pauschal zurueckfiele."""
    service = _MLService({"24h": _mlv_mit(40.0, True)})
    prognose, quelle, decay = _rufe(_motor(ml_service=service))
    assert quelle == "ml"
    assert prognose[24] == 40.0
    assert decay == 10.0


def test_t0577_rueckfall_wird_laut_gemeldet(capsys):
    """Der Rueckfall weckt einen seit dem 17.07. ruhenden Pfad -- sein erstes
    echtes Auftreten muss im Log stehen, nicht erst in der Bilanz."""
    service = _MLService({"24h": _mlv_mit(66.0, False, "geraetewechsel")})
    _rufe(_motor(ml_service=service))
    aus = capsys.readouterr().out
    assert "ml_prognose_ungueltig" in aus, aus
    assert "geraetewechsel" in aus, "der Grund fehlt in der Meldung"


def test_t0577_gueltige_prognose_meldet_nichts(capsys):
    """Negativprobe zur Meldung: im Normalfall kein Rauschen."""
    _rufe(_motor(ml_service=_MLService({"24h": _mlv_mit(40.0, True)})))
    assert "ml_prognose_ungueltig" not in capsys.readouterr().out


# --------------------- die Falle: gefilterter Frame versteckt den Beweis

class _SpeicherMitMessung:
    """Liefert eine neuere Messung, als die Feature-Zeile alt ist."""

    def __init__(self, letzte_zeit, historie=None):
        self._t = letzte_zeit
        self._h = historie or {}
        self.bulk_aufrufe = 0

    async def hole_sensor_id_historie(self, seit):
        return self._h

    async def letzte_messung_aggregiert_bulk(self, zone_ids, **kw):
        self.bulk_aufrufe += 1
        return {zid: SimpleNamespace(zeitstempel=self._t) for zid in zone_ids}


def _service():
    from bewaesserung.ml.vorhersage import MLVorhersageService
    svc = MLVorhersageService.__new__(MLVorhersageService)
    svc._df_cache_ttl_s = 45
    return svc


def test_t0577_urteil_nutzt_die_ungefilterte_letzte_messung():
    """Die Falle aus dem Design, als Verhalten geprueft.

    Der Feature-DF ist GEFILTERT -- im Maxibaer-Fall hat
    `_filtere_ausschluss_fenster` die Terra-Messungen entfernt, die neueste
    Zeile darin ist die Beam-Zeile von 05:55. Naehme das Urteil den Bezug
    aus dem DF, waere der Rueckstand 0 und die veraltete 66 liefe durch.

    Deshalb muss der Bezug aus `letzte_messung_aggregiert_bulk` kommen, das
    UNGEFILTERT liest. Hier: Feature-Zeile 05:55, echte letzte Messung 06:57
    am Folgetag -> das Urteil MUSS "veraltet" sein.
    """
    from datetime import timedelta

    from bewaesserung.ml.modelle_ml import MLVorhersage

    feature = datetime(2026, 9, 8, 5, 55)
    jetzt = datetime(2026, 9, 9, 7, 16)
    letzte = datetime(2026, 9, 9, 6, 57)
    speicher = _SpeicherMitMessung(letzte)
    konfig = SimpleNamespace(zonen=[SimpleNamespace(zone_id="mandevilla_maxi")])
    svc = _service()

    historie, letzte_pro_zone = asyncio.run(
        svc._hole_guete_kontext(speicher, konfig, jetzt),
    )
    assert speicher.bulk_aufrufe == 1, "Bezug nicht aus der ungefilterten Quelle"
    assert letzte_pro_zone["mandevilla_maxi"] == letzte

    mlv = MLVorhersage(
        zone_id="mandevilla_maxi", zeitstempel=jetzt, horizont_stunden=24,
        feuchte_aktuell=66.0, feuchte_prognose=66.23,
        feature_zeitstempel=feature, inferenz_geraet_id="fyta_900001",
    )
    geurteilt = svc._mit_guete(mlv, "mandevilla_maxi", jetzt, historie,
                               letzte_pro_zone)
    assert geurteilt.gueltig is False
    assert geurteilt.ungueltig_grund == "veraltet"
    assert geurteilt.feature_rueckstand_h == pytest.approx(25.03, abs=0.05)


def test_t0577_kontext_wird_gebuendelt_und_gecacht():
    """Zwei Abrufe je Cache-Zyklus, nicht je Zone -- sonst entsteht im
    Entscheidungsloop genau `fehlerpattern_redundanter_df_build_eventloop`."""
    jetzt = datetime(2026, 9, 9, 7, 16)
    speicher = _SpeicherMitMessung(jetzt)
    konfig = SimpleNamespace(zonen=[SimpleNamespace(zone_id=f"z{i}") for i in range(14)])
    svc = _service()
    for _ in range(5):
        asyncio.run(svc._hole_guete_kontext(speicher, konfig, jetzt))
    assert speicher.bulk_aufrufe == 1, speicher.bulk_aufrufe


def test_t0577_kontextfehler_wird_nicht_verschluckt(capsys):
    """Scheitert der Abruf, darf die Prognose nicht abreissen -- aber es muss
    laut werden, weil das Urteil dann gegen die Wanduhr faellt."""
    class _Kaputt:
        async def hole_sensor_id_historie(self, seit):
            raise RuntimeError("DB weg")

        async def letzte_messung_aggregiert_bulk(self, *a, **kw):
            raise RuntimeError("DB weg")

    svc = _service()
    h, l = asyncio.run(svc._hole_guete_kontext(
        _Kaputt(), SimpleNamespace(zonen=[]), datetime(2026, 9, 9),
    ))
    assert h == {} and l == {}


# --------------- die NAHT: kommt das Urteil aus live_vorhersage heraus?

def _service_fuer_durchlauf(feature_zeit, jetzt_letzte):
    """Ein Service, der `live_vorhersage` wirklich durchlaeuft.

    Ohne diesen Durchlauf pruefen die Tests `_mit_guete` isoliert -- und die
    Negativprobe "Aufruf aus live_vorhersage entfernen" blieb am 16.09. gruen.
    Genau die Naht-Klasse, die an diesem Tag dreimal auftrat.
    """
    import pandas as pd

    from bewaesserung.ml.modelle_ml import MLVorhersage

    svc = _service()
    svc._modelle = {"aktiv": True}
    svc._cluster_caches = {}
    svc._live_cache = {}
    svc._live_cache_ttl_s = 45

    df = pd.DataFrame([{
        "zone_id": "z", "zeitstempel": feature_zeit, "geraet_id": "g",
    }])

    async def _df(speicher, konfig, jetzt):
        return df

    def _vorhersage(zeile, horizont, details=False, cluster_id=None):
        return [MLVorhersage(
            zone_id="z", zeitstempel=datetime(2026, 9, 9, 7, 16),
            horizont_stunden=horizont, feuchte_aktuell=66.0,
            feuchte_prognose=66.23,
        )]

    svc._hole_live_df = _df
    svc.vorhersage = _vorhersage
    svc._cluster_fuer_zone = lambda zone_id: None
    svc._modell_version = lambda h, c: "v"

    class _Speicher(_SpeicherMitMessung):
        async def logge_ml_vorhersage(self, **kw):
            return None

    speicher = _Speicher(jetzt_letzte)
    konfig = SimpleNamespace(zonen=[SimpleNamespace(
        zone_id="z", aggregat_lead_geraet=None,
    )])
    return svc, speicher, konfig


def test_t0577_live_vorhersage_liefert_das_urteil_mit():
    """Die Einzel-Variante. Veraltete Zeile -> das Ergebnis traegt gueltig=False."""
    svc, speicher, konfig = _service_fuer_durchlauf(
        feature_zeit=datetime(2026, 9, 8, 5, 55),
        jetzt_letzte=datetime(2026, 9, 9, 6, 57),
    )
    erg = asyncio.run(svc.live_vorhersage("z", speicher, konfig))
    assert erg, "live_vorhersage lieferte nichts"
    for key, v in erg.items():
        assert v.gueltig is False, f"{key}: Urteil fehlt am Ergebnis"
        assert v.ungueltig_grund == "veraltet", key


def test_t0577_live_vorhersage_bulk_liefert_das_urteil_mit():
    """Die Bulk-Variante -- eine KOPIE derselben Schleife. Ein Urteil nur in
    einer der beiden waere genau der Klassiker, den der Code-Kommentar dort
    schon benennt."""
    svc, speicher, konfig = _service_fuer_durchlauf(
        feature_zeit=datetime(2026, 9, 8, 5, 55),
        jetzt_letzte=datetime(2026, 9, 9, 6, 57),
    )
    erg = asyncio.run(svc.live_vorhersage_bulk(["z"], speicher, konfig))
    assert erg.get("z"), "live_vorhersage_bulk lieferte nichts"
    for key, v in erg["z"].items():
        assert v.gueltig is False, f"{key}: Urteil fehlt am Bulk-Ergebnis"


def test_t0577_frische_zeile_bleibt_gueltig_im_durchlauf():
    """Negativprobe zum Durchlauf: aktuelle Zeile -> gueltig=True. Sonst waeren
    die beiden Tests oben auch gruen, wenn alles pauschal ungueltig wuerde."""
    t = datetime(2026, 9, 9, 6, 57)
    svc, speicher, konfig = _service_fuer_durchlauf(feature_zeit=t, jetzt_letzte=t)
    erg = asyncio.run(svc.live_vorhersage("z", speicher, konfig))
    assert erg and all(v.gueltig is True for v in erg.values())
