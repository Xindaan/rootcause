"""T-0576 E: Giessfenster aus der Verdunstung, Andres Entscheid 16.09.2026.

Drei Schichten, jeweils am Verhalten gemessen:
  A  die Urteilsfunktion `bewerte_giessfenster`, mit den gemessenen Werten
  B  die Config-Whitelist (`_parse_zonen`) -- ein neues Feld kam hier frueher
     still als None an (fehlerpattern_config_whitelist)
  C  die ECHTE Engine ueber `pruefe_zone`: aus / schatten / aktiv, Rueckfall
     ohne Daten (Entscheid 3) und der Kritisch-Bypass (Entscheid 4)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from bewaesserung.giessfenster import (
    GRUND_AUSSERHALB_KLAMMER,
    GRUND_KEINE_VORHERSAGE,
    GRUND_OK,
    GRUND_ZU_VIEL_SONNE,
    GRUND_ZU_WENIG_TROCKNUNG,
    bewerte_giessfenster,
    in_zeitfenstern,
)
from bewaesserung.modelle import (
    BlockerTyp,
    GiessfensterEt0Konfig,
    WetterStunde,
    WetterVorhersage,
    ZeitFenster,
    ZonenKonfig,
    ZonenModus,
)

TAG = datetime(2026, 4, 6)
KLAMMER = [ZeitFenster(von="04:00", bis="17:00")]


def _vorhersage(et0_pro_stunde: dict[int, float], tag: datetime = TAG):
    """Stuendliche Vorhersage fuer einen ganzen Tag + Folgetag."""
    stunden = []
    for d in (0, 1):
        for h in range(24):
            stunden.append(WetterStunde(
                zeitstempel=tag + timedelta(days=d, hours=h),
                temperatur=18.0, niederschlag_mm=0.0,
                niederschlag_wahrscheinlichkeit=0.0, wind_kmh=5.0,
                et0_mm=et0_pro_stunde.get(h, 0.0),
            ))
    return WetterVorhersage(abfrage_zeitstempel=tag, stunden=stunden)


# ECHTE Tagesgaenge aus `wetter_vorhersage` (Standort-A, jeweils letzte vor
# der Stunde bekannte Abfrage). Die erste Fassung dieser Tests nutzte einen
# ausgedachten "kuehlen Tag" mit schwachem Nachmittag -- und lag damit knapp
# unter der Trocknungsschwelle. Die echten Tage zeigen etwas anderes:
#
#   16.09. kuehl/bedeckt:  12-15 h 0,19 mm, danach 15-21 h 0,72 mm
#   08.09. warm/sonnig:    12-15 h 1,10 mm, danach 15-21 h 1,22 mm
#
# Am 16.09. zog es nachmittags auf (0,06 mm/h mittags, bis 0,20 um 17 h).
# Ueber 13 Tage lag die Trocknung nach einem 12-Uhr-Zyklus nie unter 0,50 mm.
KUEHL_1609 = {0: 0.03, 1: 0.02, 2: 0.02, 3: 0.01, 4: 0.01, 5: 0.01, 8: 0.03,
              9: 0.06, 10: 0.04, 11: 0.06, 12: 0.06, 13: 0.06, 14: 0.07,
              15: 0.15, 16: 0.18, 17: 0.20, 18: 0.12, 19: 0.06, 20: 0.01,
              21: 0.01, 22: 0.01, 23: 0.01}
HEISS_0809 = {0: 0.01, 1: 0.01, 7: 0.01, 8: 0.03, 9: 0.10, 10: 0.20, 11: 0.30,
              12: 0.38, 13: 0.38, 14: 0.34, 15: 0.29, 16: 0.35, 17: 0.30,
              18: 0.19, 19: 0.08, 20: 0.01}


def _urteil(stunde, et0, trocknung_min=0.3, sonne_max=0.6, zyklus=162):
    return bewerte_giessfenster(
        zeitpunkt=TAG.replace(hour=stunde), wetter=et0, klammer=KLAMMER,
        zyklus_min=zyklus, trocknung_fenster_h=6,
        trocknung_min_mm=trocknung_min, sonne_max_mm=sonne_max,
    )


# ============================================================ A: Urteil

def test_a_kuehler_mittag_ist_erlaubt():
    """Andres Punkt: bei kuehlem, bedecktem Wetter ist mittags giessen
    unkritisch. Echter 16.09.: 12-15 Uhr 0,19 mm."""
    u = _urteil(12, _vorhersage(KUEHL_1609))
    assert u.erlaubt is True, u
    assert u.grund == GRUND_OK
    assert u.et0_waehrend_mm == pytest.approx(0.19)
    assert u.et0_nach_mm == pytest.approx(0.72)


def test_a_heisser_mittag_ist_gesperrt():
    """Dasselbe Zeitfenster am echten 08.09. (27 °C): 1,10 mm."""
    u = _urteil(12, _vorhersage(HEISS_0809))
    assert u.erlaubt is False
    assert u.grund == GRUND_ZU_VIEL_SONNE
    assert u.et0_waehrend_mm == pytest.approx(1.10)


def test_a_spaeter_nachmittag_trocknet_nicht_mehr():
    """Ein Zyklus ab 16 Uhr endet nach 18:42; die sechs Stunden danach
    verdunsten fast nichts. Die Klammer laesst ihn zu, die Bedingung nicht."""
    u = _urteil(16, _vorhersage(KUEHL_1609))
    assert u.erlaubt is False
    assert u.grund == GRUND_ZU_WENIG_TROCKNUNG
    assert u.et0_nach_mm < 0.3


def test_a_ausserhalb_der_klammer_immer_nein():
    """19 Uhr liegt ausserhalb 04-17 -- ohne jede Datenabfrage."""
    u = _urteil(19, _vorhersage(KUEHL_1609))
    assert u.erlaubt is False
    assert u.grund == GRUND_AUSSERHALB_KLAMMER


def test_a_ohne_vorhersage_entscheidet_die_klammer():
    """Andres Entscheid 3: Rueckfall statt fail-closed. Eine gescheiterte
    Wetterabfrage liefert eine LEERE Vorhersage -- `sum([])` ist 0 und saehe
    sonst aus wie "keine Trocknung", die Zone wuerde gesperrt."""
    leer = WetterVorhersage(abfrage_zeitstempel=TAG, stunden=[])
    u = _urteil(12, leer)
    assert u.erlaubt is True
    assert u.daten_fehlen is True
    assert u.grund == GRUND_KEINE_VORHERSAGE


def test_a_unvollstaendige_vorhersage_ist_keine_aussage():
    """Endet die Vorhersage mitten im Trocknungsfenster, zaehlt das nicht als
    "wenig Trocknung" -- dieselbe Falle wie oben, nur schmaler."""
    kurz = _vorhersage(KUEHL_1609)
    kurz = WetterVorhersage(
        abfrage_zeitstempel=TAG,
        stunden=[s for s in kurz.stunden if s.zeitstempel < TAG.replace(hour=17)],
    )
    u = _urteil(12, kurz)
    assert u.daten_fehlen is True and u.erlaubt is True


def test_a_ohne_schwelle_wird_die_seite_nicht_geprueft():
    """Im Schattenbetrieb laesst sich jede Haelfte einzeln beobachten."""
    heiss = _vorhersage(HEISS_0809)
    assert _urteil(12, heiss, sonne_max=None).erlaubt is True
    assert _urteil(16, _vorhersage(KUEHL_1609), trocknung_min=None).erlaubt is True


def test_a_zeitfenster_ueber_mitternacht_unveraendert():
    """`in_zeitfenstern` ersetzt die alte Logik -- inklusive `von > bis`."""
    nacht = [ZeitFenster(von="22:00", bis="02:00")]
    assert in_zeitfenstern(nacht, TAG.replace(hour=23)) is True
    assert in_zeitfenstern(nacht, TAG.replace(hour=1)) is True
    assert in_zeitfenstern(nacht, TAG.replace(hour=12)) is False
    assert in_zeitfenstern([], TAG.replace(hour=12)) is True


# ======================================================= B: Whitelist

def test_b_yaml_block_kommt_an():
    """Ohne die Zeile in `_parse_zonen` waere der Block still None."""
    from bewaesserung.konfig import _parse_zonen

    zonen = _parse_zonen([{
        "zone_id": "waldblumenhain", "name": "Waldblumenhain",
        "bevorzugte_zeiten": ["04:00-08:00", "18:00-21:00"],
        "giessfenster_et0": {
            "modus": "schatten", "klammer": ["04:00-17:00"],
            "zyklus_min": 162, "trocknung_min_mm": 0.3,
        },
    }])
    gf = zonen[0].giessfenster_et0
    assert gf is not None, "Block wurde von der Whitelist verschluckt"
    assert gf.modus == "schatten"
    assert gf.zyklus_min == 162
    assert gf.trocknung_min_mm == 0.3
    assert [(k.von, k.bis) for k in gf.klammer] == [("04:00", "17:00")]


def test_b_ohne_block_bleibt_none():
    from bewaesserung.konfig import _parse_zonen

    z = _parse_zonen([{"zone_id": "x", "name": "X"}])
    assert z[0].giessfenster_et0 is None


def test_b_zyklus_min_ist_pflicht():
    """Kein Default, der still vom realen Zyklus abweicht."""
    with pytest.raises(Exception):
        GiessfensterEt0Konfig(klammer=KLAMMER)


# ==================================================== C: echte Engine

def _zone(modus: str | None, **gf_extra):
    gf = None
    if modus is not None:
        gf = GiessfensterEt0Konfig(
            modus=modus, klammer=KLAMMER, zyklus_min=162,
            trocknung_min_mm=0.3, sonne_max_mm=0.6, **gf_extra,
        )
    return ZonenKonfig(
        zone_id="zone-1", name="Waldblumen", modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=35.0, feuchte_kritisch=20.0,
        max_dauer_sekunden=1800, min_pause_minuten=120,
        tages_budget_sekunden=3600.0,
        bevorzugte_zeiten=[ZeitFenster(von="04:00", bis="08:00"),
                           ZeitFenster(von="18:00", bis="21:00")],
        giessfenster_et0=gf,
    )


async def _entscheide(zone, stunde, et0, feuchte=28.0):
    from test_entscheidung import baue_motor

    motor, _ = baue_motor(
        zone, feuchte=feuchte, jetzt=TAG.replace(hour=stunde), vorhersage=et0,
    )
    return await motor.pruefe_zone(zone.zone_id)


@pytest.mark.asyncio
async def test_c_ohne_block_mittags_gesperrt_wie_bisher():
    """Regression: ohne `giessfenster_et0` exakt das alte Verhalten."""
    e = await _entscheide(_zone(None), 12, _vorhersage(KUEHL_1609))
    assert e.soll_bewaessern is False
    assert e.blocker_typ == BlockerTyp.ZEITFENSTER


@pytest.mark.asyncio
async def test_c_schatten_aendert_nichts():
    """Schatten: die Bedingung SAGT ja (kuehler Mittag), das Verhalten bleibt
    das der alten Fenster -- gesperrt."""
    e = await _entscheide(_zone("schatten"), 12, _vorhersage(KUEHL_1609))
    assert e.soll_bewaessern is False
    assert e.blocker_typ == BlockerTyp.ZEITFENSTER


@pytest.mark.asyncio
async def test_c_aktiv_kuehler_mittag_wird_gegossen():
    """Der eigentliche Gewinn von E: der Bedarf entsteht tagsueber, und an
    einem kuehlen Tag darf die Zone dann auch tagsueber giessen."""
    e = await _entscheide(_zone("aktiv"), 12, _vorhersage(KUEHL_1609))
    assert e.soll_bewaessern is True, e.begruendung


@pytest.mark.asyncio
async def test_c_aktiv_heisser_mittag_bleibt_gesperrt():
    e = await _entscheide(_zone("aktiv"), 12, _vorhersage(HEISS_0809))
    assert e.soll_bewaessern is False
    assert e.blocker_typ == BlockerTyp.ZEITFENSTER


@pytest.mark.asyncio
async def test_c_aktiv_abend_ist_raus():
    """19 Uhr stand in den alten Fenstern, liegt aber ausserhalb der Klammer."""
    e = await _entscheide(_zone("aktiv"), 19, _vorhersage(KUEHL_1609))
    assert e.soll_bewaessern is False
    assert e.blocker_typ == BlockerTyp.ZEITFENSTER


@pytest.mark.asyncio
async def test_c_aktiv_ohne_vorhersage_entscheidet_die_klammer():
    """Entscheid 3 in der echten Engine: Datenausfall sperrt die Zone nicht."""
    leer = WetterVorhersage(abfrage_zeitstempel=TAG, stunden=[])
    e = await _entscheide(_zone("aktiv"), 12, leer)
    assert e.blocker_typ != BlockerTyp.ZEITFENSTER, e.begruendung


@pytest.mark.asyncio
async def test_c_kritisch_umgeht_auch_die_datenbedingung():
    """Entscheid 4: heisser Mittag, Bedingung sagt nein -- aber die Zone ist
    kritisch trocken. Der Bypass sitzt ausserhalb von `_pruefe_giessfenster`
    und muss deshalb unveraendert greifen."""
    e = await _entscheide(
        _zone("aktiv"), 12, _vorhersage(HEISS_0809), feuchte=18.0,
    )
    assert e.soll_bewaessern is True, e.begruendung


@pytest.mark.asyncio
async def test_c_schattenlog_flutet_nicht(capsys):
    """`vorhersage_zone` laeuft auch fuers Dashboard jede Minute. Gleiches
    Urteil in derselben Stunde -> genau EIN Logeintrag."""
    from test_entscheidung import baue_motor

    zone = _zone("schatten")
    motor, _ = baue_motor(
        zone, feuchte=28.0, jetzt=TAG.replace(hour=12),
        vorhersage=_vorhersage(KUEHL_1609),
    )
    for _ in range(10):
        await motor.pruefe_zone(zone.zone_id)
    aus = capsys.readouterr().out
    assert aus.count("giessfenster.schatten") == 1, aus.count("giessfenster.schatten")
    assert "weicht_ab=True" in aus, "Abweichung Schatten/Statik nicht sichtbar"


# ------------ C2: die beiden anderen Aufrufstellen (je eigene Verdrahtung)

@pytest.mark.asyncio
async def test_c_vorhersage_zone_sperrt_heissen_mittag():
    """`vorhersage_zone` (Dashboard, Tagesplan) reicht `wetter` SEPARAT durch.
    Vergaesse diese Stelle es, fiele sie still auf die Klammer zurueck und
    zeigte fuer einen heissen Mittag "wuerde giessen" an."""
    from test_entscheidung import baue_motor

    zone = _zone("aktiv")
    motor, _ = baue_motor(zone, feuchte=28.0, jetzt=TAG.replace(hour=12),
                          vorhersage=_vorhersage(HEISS_0809))
    empf = await motor.vorhersage_zone(zone.zone_id)
    assert empf.soll_bewaessern is False
    assert empf.blocker_typ == BlockerTyp.ZEITFENSTER


@pytest.mark.asyncio
async def test_c_vorhersage_zone_erlaubt_kuehlen_mittag():
    """Negativprobe zur Stelle oben: sonst waere sie auch gruen, wenn
    `vorhersage_zone` pauschal sperrte."""
    from test_entscheidung import baue_motor

    zone = _zone("aktiv")
    motor, _ = baue_motor(zone, feuchte=28.0, jetzt=TAG.replace(hour=12),
                          vorhersage=_vorhersage(KUEHL_1609))
    empf = await motor.vorhersage_zone(zone.zone_id)
    assert empf.blocker_typ != BlockerTyp.ZEITFENSTER, empf.grund


def _kanal_motor(vorhersage, stunde):
    from test_entscheidung import (
        SpeicherAttrappe,
        WetterClientAttrappe,
        WetterManagerAttrappe,
        baue_messung,
    )
    from bewaesserung.entscheidung import Entscheidungsmotor

    jetzt = TAG.replace(hour=stunde)
    a = _zone("aktiv").model_copy(update={"zone_id": "zone-a", "ventil_kanal": 1})
    b = a.model_copy(update={"zone_id": "zone-b"})
    speicher = SpeicherAttrappe()
    speicher.messungen["zone-a"] = [baue_messung(jetzt, 28.0, "zone-a")]
    speicher.messungen["zone-b"] = [baue_messung(jetzt, 48.0, "zone-b")]
    motor = Entscheidungsmotor(
        speicher, WetterManagerAttrappe(WetterClientAttrappe(vorhersage)), [a, b],
    )
    motor._jetzt = lambda: jetzt
    return motor, [a, b]


@pytest.mark.asyncio
async def test_c_kanalpfad_sperrt_heissen_mittag():
    """`pruefe_kanal` fragt ueber die Referenzzone und reicht `wetter` eigens
    durch -- dritte Aufrufstelle, eigener Test."""
    motor, zonen = _kanal_motor(_vorhersage(HEISS_0809), 12)
    e = await motor.pruefe_kanal(1, zonen)
    assert e.soll_bewaessern is False
    assert e.blocker_typ == BlockerTyp.ZEITFENSTER


@pytest.mark.asyncio
async def test_c_kanalpfad_erlaubt_kuehlen_mittag():
    motor, zonen = _kanal_motor(_vorhersage(KUEHL_1609), 12)
    e = await motor.pruefe_kanal(1, zonen)
    assert e.blocker_typ != BlockerTyp.ZEITFENSTER, e.begruendung


# ================================ D: Begruendung, Aktivlog, Anzeige (16.09.)
#
# Andre 16.09. nach der Umschaltung: (1) die Anzeige zeigte weiter die alten
# Uhrzeitfenster, (3) im Aktivmodus wurde das Urteil nicht mehr geloggt.
# Dazu die Begruendung: "ausserhalb bevorzugter Zeit" war mittags schlicht
# falsch -- gesperrt hat das Wetter, nicht die Uhr.

@pytest.mark.asyncio
async def test_d_begruendung_nennt_die_sonne():
    e = await _entscheide(_zone("aktiv"), 12, _vorhersage(HEISS_0809))
    assert e.blocker_typ == BlockerTyp.ZEITFENSTER
    assert "zu viel Sonne" in e.begruendung, e.begruendung
    assert "1,10 mm" in e.begruendung, e.begruendung   # 12-15 h am 08.09.


@pytest.mark.asyncio
async def test_d_begruendung_nennt_die_trocknung():
    # 16 h am kuehlen 16.09.: waehrend 0,50 (erlaubt), danach 0,11 (< 0,3)
    e = await _entscheide(_zone("aktiv"), 16, _vorhersage(KUEHL_1609))
    assert "trocknet danach nicht ab" in e.begruendung, e.begruendung


@pytest.mark.asyncio
async def test_d_begruendung_nennt_die_klammer():
    e = await _entscheide(_zone("aktiv"), 19, _vorhersage(KUEHL_1609))
    assert "ausserhalb Giessfenster 04:00-17:00" in e.begruendung, e.begruendung


@pytest.mark.asyncio
async def test_d_schatten_behaelt_den_alten_text():
    """Im Schatten sperren die alten Fenster -- dann stimmt der alte Text."""
    e = await _entscheide(_zone("schatten"), 12, _vorhersage(HEISS_0809))
    assert "ausserhalb bevorzugter Zeit" in e.begruendung, e.begruendung


@pytest.mark.asyncio
async def test_d_vorhersage_zone_nennt_die_sonne():
    """Eigene Aufrufstelle, eigener Text (das ist, was der Tagesplan zeigt)."""
    from test_entscheidung import baue_motor

    zone = _zone("aktiv")
    motor, _ = baue_motor(zone, feuchte=28.0, jetzt=TAG.replace(hour=12),
                          vorhersage=_vorhersage(HEISS_0809))
    empf = await motor.vorhersage_zone(zone.zone_id)
    assert "zu viel Sonne" in (empf.grund or ""), empf.grund


@pytest.mark.asyncio
async def test_d_kanalpfad_nennt_die_sonne():
    motor, zonen = _kanal_motor(_vorhersage(HEISS_0809), 12)
    e = await motor.pruefe_kanal(1, zonen)
    assert "zu viel Sonne" in e.begruendung, e.begruendung


@pytest.mark.asyncio
async def test_d_aktivlog_schreibt_und_flutet_nicht(capsys):
    """Andre 16.09.: das Urteil wird auch scharf gebraucht. Einmal pro Stunde
    bzw. Urteilswechsel, mit dem Vergleich zu den alten Fenstern."""
    from test_entscheidung import baue_motor

    zone = _zone("aktiv")
    motor, _ = baue_motor(
        zone, feuchte=28.0, jetzt=TAG.replace(hour=12),
        vorhersage=_vorhersage(HEISS_0809),
    )
    for _ in range(10):
        await motor.pruefe_zone(zone.zone_id)
    aus = capsys.readouterr().out
    assert aus.count("giessfenster.aktiv") == 1, aus.count("giessfenster.aktiv")
    assert "giessfenster.schatten" not in aus
    assert "grund=zu_viel_sonne" in aus
    assert "statisch_erlaubt=False" in aus


def test_d_naechster_start_ueberspringt_sonne_und_abend():
    """08.09. ab 12:07: mittags Sonne, 17 h Trocknung, danach ausserhalb der
    Klammer -- frei ist erst der naechste Morgen um 04:00. Raster 15 min."""
    from bewaesserung.giessfenster import naechster_erlaubter_start

    gf = _zone("aktiv").giessfenster_et0
    ab = TAG.replace(hour=12, minute=7)
    start, gesperrt = naechster_erlaubter_start(
        gf, ab=ab, bis=ab + timedelta(hours=24), wetter=_vorhersage(HEISS_0809),
    )
    assert start == TAG + timedelta(days=1, hours=4), start
    assert gesperrt is not None and gesperrt.grund == GRUND_ZU_VIEL_SONNE


def test_d_naechster_start_ist_jetzt_wenn_offen():
    """Negativprobe zur Suche: offen heisst sofort, und kein Sperrgrund."""
    from bewaesserung.giessfenster import naechster_erlaubter_start

    gf = _zone("aktiv").giessfenster_et0
    ab = TAG.replace(hour=12)
    start, gesperrt = naechster_erlaubter_start(
        gf, ab=ab, bis=ab + timedelta(hours=24), wetter=_vorhersage(KUEHL_1609),
    )
    assert start == ab and gesperrt is None


# ---- D2: /api/tagesplan -- was die Karte zeigt

def _tagesplan_client(tmp_path, monkeypatch, soll: dict[str, bool]):
    import asyncio
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    import bewaesserung.api_server as api
    from bewaesserung.modelle import (
        GardenaKonfig, GesamtKonfig, GiessEmpfehlung, SpeicherKonfig,
        StandortKonfig, WetterKonfig, WetterStandortKonfig,
    )
    from bewaesserung.speicher import Speicher

    jetzt = TAG.replace(hour=12, minute=7)

    class _FesteUhr(datetime):
        @classmethod
        def now(cls, tz=None):
            return jetzt

    monkeypatch.setattr(api, "datetime", _FesteUhr)

    aktiv = _zone("aktiv").model_copy(update={"zone_id": "wald", "ventil_kanal": 1})
    schatten = _zone("schatten").model_copy(
        update={"zone_id": "wiese", "ventil_kanal": 2},
    )
    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[aktiv, schatten],
        wetter=WetterKonfig(standorte=[
            WetterStandortKonfig(id="o", breite=52.5, laenge=13.4),
        ]),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[StandortKonfig(
            standort_id="garten", name="Garten", wetter_standort="o",
            zonen=["wald", "wiese"],
        )],
    )
    motor = MagicMock()

    async def _vorhersage_zone(zone_id, sicherheits_tage_override=None):
        b = soll.get(zone_id, False)
        return GiessEmpfehlung(
            zone_id=zone_id, zeitstempel=jetzt, soll_bewaessern=b,
            grund="x", empfehlungs_typ="praeventiv" if b else "akut",
            dauer_s_empfehlung=600 if b else 0, aktive_strategie="korridor",
        )

    motor.vorhersage_zone = _vorhersage_zone
    wetter = MagicMock()

    async def _hole(_sid):
        return _vorhersage(HEISS_0809)

    wetter.hole_vorhersage = _hole
    speicher = Speicher(str(tmp_path / "t.db"))
    asyncio.run(speicher.verbinden())
    api.konfiguriere_api(speicher, konfig, motor, MagicMock(),
                         wetter_manager=wetter)
    return TestClient(api.app), speicher, jetzt


def _plan(tmp_path, monkeypatch, soll):
    import asyncio

    client, speicher, jetzt = _tagesplan_client(tmp_path, monkeypatch, soll)
    try:
        body = client.get("/api/tagesplan").json()
    finally:
        client.close()
        asyncio.run(speicher.schliessen())
    return {e["zone_id"]: e for e in body["eintraege"]}, jetzt


def test_d_tagesplan_zeigt_die_klammer_statt_der_alten_fenster(
    tmp_path, monkeypatch,
):
    plan, _ = _plan(tmp_path, monkeypatch, {})
    assert plan["wald"]["bevorzugte_zeiten"] == ["04:00-17:00"]
    # Negativprobe im selben Aufruf: Schatten behaelt die alten Fenster.
    assert plan["wiese"]["bevorzugte_zeiten"] == ["04:00-08:00", "18:00-21:00"]
    assert plan["wiese"]["giessfenster"] is None


def test_d_tagesplan_nennt_naechsten_start_und_grund(tmp_path, monkeypatch):
    plan, _ = _plan(tmp_path, monkeypatch, {})
    gf = plan["wald"]["giessfenster"]
    assert gf["naechster_start"].startswith("2026-04-07T04:00"), gf
    assert "zu viel Sonne" in gf["sperrgrund"], gf
    assert gf["daten_fehlen"] is False


def test_d_tagesplan_geplante_zeit_ist_jetzt_nicht_das_alte_fenster(
    tmp_path, monkeypatch,
):
    """Vorher: erster Beginn der alten Fenster nach jetzt -> 18:00, ein Start,
    den die Engine im Aktivmodus gar nicht mehr kennt."""
    plan, jetzt = _plan(tmp_path, monkeypatch, {"wald": True, "wiese": True})
    assert plan["wald"]["geplante_zeit"].startswith("2026-04-06T12:07"), plan["wald"]
    # Negativprobe: im Schatten gilt weiter das alte Fenster.
    assert plan["wiese"]["geplante_zeit"].startswith("2026-04-06T18:00"), plan["wiese"]
