"""T-0622 Regel 2 (Entscheid Andre 13.10.2026): Ausnahme vom T-0618-Vorrang.

T-0618: eine kritisch trockene Sequenz ohne Fenster (Bambus) behaelt in ihrer
Soak-Pause den Hahn gegen eine nicht kritische Zone mit offenem Fenster
(waldblumenhain). NEU: AUSSER die Vorhersage sagt, dass die Fenster-Zone
kritisch wird, bevor sie nach dem Ende der wartenden Sequenz wieder giessen
kann. Dann verdraengt sie wie vor T-0618. Nur AUTOMATIK, nie Schliessen; jede
Unsicherheit (kein Urteil, Fehler, kein Sequenzende) = T-0618.

Schichten, jeweils am Verhalten gemessen (Code ausgefuehrt, Attrappen fuer
Uhr, Wetter, Prognose und Speicher; kein Netz, kein Ventil):
  A  reine Rechnung: gleiche Rechnung wie T-0578, Bezug kritisch
  B  Fensterlogik: `_pruefe_giessfenster` nach dem Umbau unveraendert
  C  der echte Motor `fenster_zone_wird_kritisch`
  D  der echte HahnArbiter mit Attrappen-Pruefer
  E  Durchstich: Arbiter + Motor + Pre-Soak-Zustand aus einer Speicher-Attrappe

Tagesgaenge aus `test_t0576_giessfenster` (echte Vorhersagen). Klammer 04-17,
Sonne 0,6, Trocknung 0,3: am heissen 08.09. ist ab 09:30 bis morgen 04:00
alles gesperrt (Sonne), obwohl die Klammer offen ist; am kuehlen 16.09. geht
um 09:45 noch ein Start. Massgeblich ist die Prognose zum naechsten erlaubten
Start nach dem Sequenzende, egal ob der heute oder morgen liegt.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from structlog.testing import capture_logs

import bewaesserung.hahn_arbiter as mod
from bewaesserung.entscheidung import Entscheidungsmotor
from bewaesserung.giessfenster import (
    MODUS_AUS,
    MODUS_SCHATTEN,
    beschreibe_sperre,
    in_zeitfenstern,
    urteil_fuer_zone,
)
from bewaesserung.hahn_arbiter import HahnArbiter, baue_kanalprofile
from bewaesserung.modelle import (
    Ausloser,
    FeuchteRegime,
    VentilAktion,
    VentilEreignis,
    WetterStunde,
    WetterVorhersage,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.vorausschau import (
    FensterSchluss,
    bewerte_vorausschau,
    wird_kritisch_bis,
)
from test_entscheidung import (
    SpeicherAttrappe,
    WetterClientAttrappe,
    WetterManagerAttrappe,
    baue_messung,
)
from test_t0576_giessfenster import (
    HEISS_0809,
    KUEHL_1609,
    TAG,
    _vorhersage,
    _zone,
)

GERAET = "dswc-test"
NEUN = TAG.replace(hour=9)
# Bambus-Sequenz: Start 08:45, Pause 25 min (inkl. Vorbenetzung), Hauptdose
# 30 min -> Ende 09:40.
SEQ_START = TAG.replace(hour=8, minute=45)
FREI_AB = TAG.replace(hour=9, minute=40)
MORGEN_VIER = TAG + timedelta(days=1, hours=4)

# Prognosen fuer waldblumenhain (jetzt 45 %, kritisch 20, Schwelle 35).
# Wert morgen 04:00 (19 h): FALLEND 33,3 (unter Schwelle, ueber kritisch),
# STEIL 17,0 (unter kritisch).
FALLEND = {6: 42.0, 12: 38.0, 24: 30.0}
STEIL = {6: 32.0, 12: 24.0, 24: 12.0}


# ===================================================== A: reine Rechnung

@pytest.mark.parametrize("stunden", [0.0, 3.0, 6.0, 9.5, 19.0, 24.0, 40.0])
@pytest.mark.parametrize("grenze", [20.0, 30.0, 33.3, 33.4, 41.0])
@pytest.mark.parametrize("prognose", [FALLEND, STEIL, {6: 44.0}])
def test_a_paritaet_mit_t0578(stunden, grenze, prognose):
    """Dieselbe Rechnung wie `bewerte_vorausschau`, nur gegen eine andere
    Grenze. Laeuft eine der beiden je anders (Interpolation, Decay hinter dem
    letzten Horizont, Stundenbasis), faellt das hier auf."""
    bis = NEUN + timedelta(hours=stunden)
    t0578 = bewerte_vorausschau(
        schluss=FensterSchluss(zu_ab=NEUN, wieder_auf=bis), jetzt=NEUN,
        messwert=45.0, schwelle=grenze, prognose=prognose,
        decay_pp_pro_tag=15.0,
    )
    t0622 = wird_kritisch_bis(
        jetzt=NEUN, bis=bis, messwert=45.0, kritisch=grenze,
        prognose=prognose, decay_pp_pro_tag=15.0,
    )
    assert t0622 is (t0578 is not None)


# ============================================ B: Fensterlogik unveraendert

def _alt_pruefe_giessfenster(zone, zeitpunkt, wetter, log):
    """Wortgleiche Kopie von `_pruefe_giessfenster` vor T-0622 (main 5cf17fd),
    nur `self._logge_giessfenster` durch `log` ersetzt."""
    statisch = in_zeitfenstern(zone.bevorzugte_zeiten, zeitpunkt)
    gf = getattr(zone, "giessfenster_et0", None)
    if gf is None or gf.modus == MODUS_AUS:
        return statisch, "ausserhalb bevorzugter Zeit"
    urteil = urteil_fuer_zone(gf, zeitpunkt, wetter)
    log(zone, gf.modus, zeitpunkt, statisch, urteil)
    if gf.modus == MODUS_SCHATTEN:
        return statisch, "ausserhalb bevorzugter Zeit"
    if urteil.erlaubt:
        return True, ""
    return False, beschreibe_sperre(gf, urteil)


@pytest.mark.parametrize("modus", [None, "aus", "schatten", "aktiv"])
@pytest.mark.parametrize(
    "wetter", [HEISS_0809, KUEHL_1609, None], ids=["heiss", "kuehl", "leer"],
)
def test_b_pruefe_giessfenster_paritaet(modus, wetter):
    """Der Motor rechnet nach dem Umbau ueber `giessfenster.pruefe_start` --
    Ergebnis, Text UND Log je Viertelstunde eines ganzen Tages wie vorher.
    `leer` = Vorhersage fehlt (Rueckfall auf die Klammer, daten_fehlen)."""
    zone = _zone(modus)
    vorhersage = _vorhersage(wetter) if wetter is not None else None
    motor = object.__new__(Entscheidungsmotor)
    neu_log: list = []
    motor._logge_giessfenster = lambda *a: neu_log.append(a)
    alt_log: list = []
    for viertel in range(24 * 4):
        t = TAG + timedelta(minutes=15 * viertel)
        alt = _alt_pruefe_giessfenster(
            zone, t, vorhersage, lambda *a: alt_log.append(a),
        )
        assert motor._pruefe_giessfenster(zone, t, vorhersage) == alt, t
    assert neu_log == alt_log


# ============================================== C: echter Motor (Prognose)

def _wald(**update) -> ZonenKonfig:
    """waldblumenhain-artig: scharfe Loop-Zone, Aktivmodus, kritisch 20."""
    return _zone("aktiv").model_copy(update={
        "zone_id": "waldblumenhain", "ventil_kanal": 1, "exklusiv": True,
        "auto_loop_opt_in": True, **update,
    })


def _bambus(**update) -> ZonenKonfig:
    return ZonenKonfig(**{
        "zone_id": "bambuswald", "name": "Bambus", "modus": ZonenModus.AUTOMATIK,
        "ventil_kanal": 2, "auto_loop_opt_in": True,
        "feuchte_schwelle_min": 55.0, "feuchte_kritisch": 50.0, **update,
    })


def _motor(
    wetter=HEISS_0809, prognose=STEIL, feuchte=None, zonen=None,
    jetzt=NEUN, speicher=None,
):
    """Echter Entscheidungsmotor; nur Speicher, Wetter und die Prognose
    (`_prognose_ml_oder_heuristik`) sind Attrappen. Liefert auch die Liste
    der Prognose-Aufrufe (Kostenordnung: erst Fenster, dann Prognose)."""
    zonen = zonen or [_wald(), _bambus()]
    feuchte = feuchte if feuchte is not None else {
        "waldblumenhain": 45.0, "bambuswald": 45.0,
    }
    speicher = speicher or SpeicherAttrappe()
    for zid, wert in feuchte.items():
        if wert is not None:
            speicher.messungen[zid] = [baue_messung(jetzt, wert, zid)]
    motor = Entscheidungsmotor(
        speicher,
        WetterManagerAttrappe(WetterClientAttrappe(_vorhersage(wetter))),
        zonen,
    )
    motor._jetzt = lambda: jetzt
    aufrufe: list = []

    async def _prognose(zone_id, aktuelle_feuchte, decay, horizonte_h):
        aufrufe.append((zone_id, aktuelle_feuchte))
        return dict(prognose), "test", 15.0

    motor._prognose_ml_oder_heuristik = _prognose
    return motor, aufrufe


@pytest.mark.asyncio
async def test_c_fenster_direkt_nach_der_sequenz_offen_kurzer_horizont():
    """Kuehler Tag: nach der Sequenz (09:40) geht um 09:45 ein Start. Kein
    Sonderzweig -- dieselbe Rechnung, nur bis 09:45: dort 43,4 % (STEIL),
    weit ueber kritisch. Wer bis morgen rechnete, kaeme auf 5 % und gaebe
    faelschlich Vorrang."""
    motor, aufrufe = _motor(wetter=KUEHL_1609, prognose=STEIL)

    urteil = await motor.fenster_zone_wird_kritisch(
        ("waldblumenhain",), NEUN, FREI_AB,
    )

    assert urteil is False
    assert aufrufe == [("waldblumenhain", 45.0)]


MITTAGSSONNE = {h: (0.5 if 11 <= h <= 13 else 0.12) for h in range(24)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prognose", "erwartet"),
    [({6: 15.0, 12: 10.0, 24: 5.0}, True), (FALLEND, False)],
    ids=["vorher_kritisch_vorrang", "bleibt_ueber_kritisch_kein_vorrang"],
)
async def test_c_fenster_oeffnet_heute_spaeter(prognose, erwartet):
    """Entscheid Hauptsitzung 13.10. nach Andres Wortlaut ("kritisch, ohne
    dass gegossen werden kann, weil ausserhalb Fenster"): die Mittagssonne
    sperrt 11-13 h, der naechste erlaubte Start nach der Sequenz ist 14:00.
    Faellt die Zone bis dahin unter kritisch (6 h: 15 %), Vorrang jetzt;
    bleibt sie darueber (6 h: 42 %), wartet sie bis 14:00."""
    acht = TAG.replace(hour=8)
    motor, _ = _motor(wetter=MITTAGSSONNE, prognose=prognose, jetzt=acht)

    assert await motor.fenster_zone_wird_kritisch(
        ("waldblumenhain",), acht, FREI_AB,
    ) is erwartet


@pytest.mark.asyncio
async def test_c_fenster_heute_zu_prognose_unter_kritisch():
    """Heisser Tag: die Klammer ist bis 17:00 offen, die Sonnen-Bedingung
    sperrt aber ab 09:30 bis morgen 04:00 -- dieselbe Fensterlogik wie der
    echte Start, nicht nur die Klammer. Bis dahin faellt die Zone auf 17 %."""
    motor, aufrufe = _motor(wetter=HEISS_0809, prognose=STEIL)

    urteil = await motor.fenster_zone_wird_kritisch(
        ("waldblumenhain",), NEUN, FREI_AB,
    )

    assert urteil is True
    assert aufrufe == [("waldblumenhain", 45.0)]


@pytest.mark.asyncio
async def test_c_bezug_ist_kritisch_nicht_die_schwelle():
    """33,3 % morgen 04:00 liegt unter der Schwelle 35, aber ueber kritisch
    20: kein Vorrang. Die Schwelle ist T-0578 (vorgezogen giessen), hier geht
    es um die Notlage."""
    motor, _ = _motor(prognose=FALLEND)

    assert await motor.fenster_zone_wird_kritisch(
        ("waldblumenhain",), NEUN, FREI_AB,
    ) is False


@pytest.mark.asyncio
async def test_c_kritisch_gilt_zum_zeitpunkt_der_oeffnung():
    """T-0560-Klasse: `effektiv_feuchte_kritisch` (Regime-aware) zum
    Zeitpunkt, an dem die Zone wieder darf. Ein Regime ab morgen hebt
    kritisch auf 34 -> die 33,3 % von morgen 04:00 sind dann kritisch."""
    regime = [FeuchteRegime(
        name="test", von_mm_dd=f"{MORGEN_VIER:%m-%d}",
        bis_mm_dd=f"{MORGEN_VIER:%m-%d}", feuchte_kritisch=34.0,
    )]
    motor, _ = _motor(
        prognose=FALLEND, zonen=[_wald(feuchte_regime=regime), _bambus()],
    )

    assert await motor.fenster_zone_wird_kritisch(
        ("waldblumenhain",), NEUN, FREI_AB,
    ) is True


@pytest.mark.asyncio
async def test_c_ueberfaellige_sequenz_zaehlt_ab_jetzt():
    """Die Pre-Soak-Zeile gilt bis 15 min nach ihrem Ende (Frische-Schranke).
    Ein Start VOR jetzt ist keine Gelegenheit: 09:30, Sequenzende 09:00 --
    um 09:00 war das Fenster noch offen, ab 09:30 ist es heute zu."""
    halb_zehn = TAG.replace(hour=9, minute=30)
    motor, _ = _motor(jetzt=halb_zehn)

    assert await motor.fenster_zone_wird_kritisch(
        ("waldblumenhain",), halb_zehn, NEUN,
    ) is True


def _vier_tage(et0_mm: float) -> WetterVorhersage:
    """Vorhersage ueber vier Tage, damit im 36-h-Suchfenster nirgends Daten
    fehlen (sonst entscheidet die Klammer allein, `daten_fehlen`)."""
    return WetterVorhersage(abfrage_zeitstempel=TAG, stunden=[
        WetterStunde(
            zeitstempel=TAG + timedelta(hours=h), temperatur=25.0,
            niederschlag_mm=0.0, niederschlag_wahrscheinlichkeit=0.0,
            wind_kmh=5.0, et0_mm=et0_mm,
        )
        for h in range(4 * 24)
    ])


@pytest.mark.asyncio
async def test_c_keine_oeffnung_im_suchhorizont():
    """Tagelang zu viel Sonne: keine Oeffnung in 36 h. Dann gilt der
    Suchhorizont als Wartezeit (wie T-0578) statt eines Fehlers."""
    motor, _ = _motor()
    motor._wetter_manager.standard_client.vorhersage = _vier_tage(1.0)

    assert await motor.fenster_zone_wird_kritisch(
        ("waldblumenhain",), NEUN, FREI_AB,
    ) is True


def _schliessen_vor(stunden: float) -> VentilEreignis:
    return VentilEreignis(
        zeitstempel=NEUN - timedelta(hours=stunden), zone_id="waldblumenhain",
        ventil_id=f"{GERAET}:1", aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=1800, ausloser=Ausloser.AUTOMATIK,
    )


def _fall_kein_urteil(name):
    """(Motor, Frager-Zonen) je Fall ohne Urteil. Prognose immer STEIL und
    naechster Start erst morgen 04:00 -- ohne den jeweiligen Riegel kaeme
    also True heraus."""
    if name == "kein_messwert":
        return _motor(feuchte={"waldblumenhain": None})[0], ("waldblumenhain",)
    if name == "nicht_scharf":
        zonen = [_wald(modus=ZonenModus.MONITORING), _bambus()]
        return _motor(zonen=zonen)[0], ("waldblumenhain",)
    if name == "ohne_fenster":
        return _motor()[0], ("bambuswald",)
    if name == "unbekannt":
        return _motor()[0], ("gibt_es_nicht",)
    if name == "zwei_fenster_zonen":
        zonen = [_wald(), _wald(zone_id="zweite"), _bambus()]
        feuchte = {"waldblumenhain": 45.0, "zweite": 45.0}
        return (
            _motor(zonen=zonen, feuchte=feuchte)[0],
            ("waldblumenhain", "zweite"),
        )
    if name == "kuerzlich_gegossen":
        speicher = SpeicherAttrappe()
        speicher.heutige_ereignisse["waldblumenhain"] = [_schliessen_vor(1)]
        return _motor(speicher=speicher)[0], ("waldblumenhain",)
    if name == "wetter_fehler":
        motor, _ = _motor()

        async def _kaputt():
            raise ConnectionError("Wetter weg (Attrappe)")
        motor._wetter_manager.standard_client.hole_vorhersage = _kaputt
        return motor, ("waldblumenhain",)
    raise AssertionError(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("fall", [
    "kein_messwert", "nicht_scharf", "ohne_fenster", "unbekannt",
    "zwei_fenster_zonen", "kuerzlich_gegossen", "wetter_fehler",
])
async def test_c_kein_urteil_heisst_none(fall):
    """None = der Arbiter bleibt bei T-0618. `kuerzlich_gegossen` wie T-0578:
    der Messwert hat die letzte Gabe noch nicht gesehen."""
    motor, zonen = _fall_kein_urteil(fall)

    with capture_logs() as logs:
        urteil = await motor.fenster_zone_wird_kritisch(zonen, NEUN, FREI_AB)

    assert urteil is None
    # Fehlende Daten sind ein Zustand, kein Fehler: nur der echte Ausfall
    # schreibt einen Fehler-Eintrag ins Log.
    fehler = [l for l in logs
              if l["event"] == "entscheidung.fenster_zone_kritisch_fehler"]
    assert len(fehler) == (1 if fall == "wetter_fehler" else 0)


# ===================================================== D: echter Arbiter

ARB_ZONEN = [_wald(), _bambus(), _bambus(zone_id="bambuswald_yogaraum")]
WARTEND = ("bambuswald", "bambuswald_yogaraum")
FRAGER = ("waldblumenhain",)
WALD_K, BAMBUS_K = 1, 2


def _pre_soak_zeile(gestartet_am=SEQ_START, **kw) -> dict:
    """Wie `Speicher.hole_pre_soak_states`; Default 25 min Pause + 30 min."""
    return {
        "zone_id": "bambuswald", "kanal": BAMBUS_K,
        "zone_ids_kanal": list(WARTEND), "pre_soak_s": 300, "pause_s": 1500,
        "haupt_s": 1800, "gestartet_am": gestartet_am, "phase": "pause",
        "ausloser": "automatik", "haupt_pulse": 1, "haupt_pause_s": 0,
        "haupt_pulse_gestartet": 0, **kw,
    }


class _Speicher:
    """Nur die Pre-Soak-Sicht; beide Arbiter-Leser (laufende Zonen und ihr
    Ende) lesen dieselbe Zeile ueber `kanal_zustand`."""

    def __init__(self, zeilen):
        self.zeilen = zeilen

    async def hole_offene_ventil_kanaele(self, **_):
        return []

    async def hole_pre_soak_states(self):
        return list(self.zeilen)


def _arbiter(fenster_urteil, zeilen=None, kritisch=None):
    """Kritisch-Pruefer: wartend True, Frager False (der T-0618-Fall), sofern
    nicht anders angegeben. Fenster-Pruefer: Attrappe mit Aufrufliste."""
    kritisch = kritisch or {WARTEND: True, FRAGER: False}
    aufrufe: list = []

    async def kritisch_pruefer(zone_ids, jetzt):
        return kritisch[tuple(zone_ids)]

    async def fenster_pruefer(zone_ids, jetzt, frei_ab):
        aufrufe.append((tuple(zone_ids), jetzt, frei_ab))
        if isinstance(fenster_urteil, Exception):
            raise fenster_urteil
        return fenster_urteil

    arb = HahnArbiter(
        _Speicher([_pre_soak_zeile()] if zeilen is None else zeilen),
        baue_kanalprofile(ARB_ZONEN, geraet_id=GERAET), {},
        kritisch_pruefer=kritisch_pruefer,
        fenster_kritisch_pruefer=fenster_pruefer,
    )
    return arb, aufrufe


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fenster_urteil", "frager_darf"),
    [
        (True, True),
        (False, False),
        (None, False),
        ("ja", False),            # nur ein echtes bool zaehlt
        (RuntimeError("Attrappe"), False),
    ],
    ids=["wird_kritisch", "bleibt_ueber_kritisch", "kein_urteil",
         "kein_bool", "fehler"],
)
async def test_d_ausnahme_nur_bei_echtem_true(fenster_urteil, frager_darf):
    arb, aufrufe = _arbiter(fenster_urteil)

    with capture_logs() as logs:
        e = await arb.pruefe(GERAET, WALD_K, jetzt=NEUN, ausloser=Ausloser.AUTOMATIK)
    arb.vermerke_verdraengung(e)

    assert len(aufrufe) == 1
    assert e.erlaubt is frager_darf, e.grund
    verdraengt = arb.verdraengung("bambuswald")
    if frager_darf:
        # Vermerk fuer die Karte wie bisher, Log mit dem neuen Grund.
        assert verdraengt is not None and verdraengt.durch == FRAGER
        weicht = [l for l in logs
                  if l["event"] == "hahn_arbiter.sequenz_weicht_zeitfenster"]
        assert [l["grund"] for l in weicht] == ["vorhersage_kritisch"]
    else:
        assert "bambuswald" in e.aktive_zonen
        assert verdraengt is None


@pytest.mark.asyncio
async def test_d_frei_ab_ist_das_ende_der_letzten_hauptdose():
    """Quelle `pre_soak_state` wie die Pre-Soak-Sicht: Start + Pause +
    alle Haupt-Pulse samt Pausen (hier 3x30 min mit 21 min Pause, wie die
    Bambus-Konfig). 08:45 + 25 + 3*30 + 2*21 min = 11:22."""
    zeile = _pre_soak_zeile(
        haupt_s=5400, haupt_pulse=3, haupt_pause_s=1260,
    )
    arb, aufrufe = _arbiter(False, zeilen=[zeile])

    await arb.pruefe(GERAET, WALD_K, jetzt=NEUN, ausloser=Ausloser.AUTOMATIK)

    assert aufrufe == [(FRAGER, NEUN, TAG.replace(hour=11, minute=22))]


@pytest.mark.asyncio
async def test_d_ohne_sequenzende_t0618(monkeypatch):
    """Zeile ohne verwertbaren Startzeitpunkt: die Sequenz reserviert (wie
    bisher), ein Ende gibt es nicht -> kein Pruefer-Aufruf, T-0618."""
    arb, aufrufe = _arbiter(True, zeilen=[_pre_soak_zeile(gestartet_am=None)])

    e = await arb.pruefe(GERAET, WALD_K, jetzt=NEUN, ausloser=Ausloser.AUTOMATIK)

    assert aufrufe == []
    assert e.erlaubt is False


@pytest.mark.asyncio
async def test_d_sequenzende_nicht_lesbar_t0618(monkeypatch):
    async def _kaputt(speicher, jetzt=None):
        raise RuntimeError("Attrappe")
    monkeypatch.setattr(mod, "laufende_pre_soak_enden", _kaputt)
    arb, aufrufe = _arbiter(True)

    e = await arb.pruefe(GERAET, WALD_K, jetzt=NEUN, ausloser=Ausloser.AUTOMATIK)

    assert aufrufe == []
    assert e.erlaubt is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kritisch", "ausloser"),
    [
        ({WARTEND: True, FRAGER: False}, Ausloser.MANUELL),
        ({WARTEND: True, FRAGER: True}, Ausloser.AUTOMATIK),
        ({WARTEND: False, FRAGER: False}, Ausloser.AUTOMATIK),
        ({WARTEND: True, FRAGER: None}, Ausloser.AUTOMATIK),
    ],
    ids=["manuell", "beide_kritisch", "wartend_nicht_kritisch",
         "frager_unbekannt"],
)
async def test_d_vorhersage_nur_im_behalte_zweig(kritisch, ausloser):
    """Ausserhalb des T-0618-Behalte-Zweigs darf der Frager ohnehin (wie
    vorher) -- dann keine Prognose, schon gar keine ML-Rechnung unter der
    Hahn-Reservierung. Manuell bleibt exakt wie vor T-0618."""
    arb, aufrufe = _arbiter(True, kritisch=kritisch)

    e = await arb.pruefe(GERAET, WALD_K, jetzt=NEUN, ausloser=ausloser)

    assert aufrufe == []
    assert e.erlaubt is True


# T-0610 AT-3 (14.10.2026): die Verdrahtung in main.ausfuehren prueft jetzt ein
# echter Start-Lauf, test_t0610_at11_start_lauf.py::test_arbiter_bekommt_die_pruefer_des_motors (vorher AST-Test).


# ================================================== E: Durchstich

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wetter", "frager_darf"),
    [(HEISS_0809, True), (KUEHL_1609, False)],
    ids=["heute_zu_verdraengt", "heute_noch_offen_behaelt"],
)
async def test_e_arbiter_motor_und_pre_soak_zustand(wetter, frager_darf):
    """Echter Arbiter, echter Motor (Kritisch-Begriff UND Vorhersage), echte
    `kanal_zustand`-Leser auf derselben Speicher-Attrappe. Bambus 45 % bei
    kritisch 50 wartet in der Soak-Pause bis 09:40; waldblumenhain 45 % bei
    kritisch 20 will um 09:00 starten, Prognose STEIL."""
    speicher = SpeicherAttrappe()
    speicher.pre_soak_states = [_pre_soak_zeile()]
    zonen = ARB_ZONEN
    motor, _ = _motor(
        wetter=wetter, zonen=zonen, speicher=speicher,
        feuchte={"waldblumenhain": 45.0, "bambuswald": 45.0,
                 "bambuswald_yogaraum": 60.0},
    )
    arb = HahnArbiter(
        speicher, baue_kanalprofile(zonen, geraet_id=GERAET), {},
        kritisch_pruefer=motor.kanal_kritisch,
        fenster_kritisch_pruefer=motor.fenster_zone_wird_kritisch,
    )

    e = await arb.pruefe(GERAET, WALD_K, jetzt=NEUN, ausloser=Ausloser.AUTOMATIK)

    assert e.erlaubt is frager_darf, e.grund
    assert (e.verdraengt != ()) is frager_darf
