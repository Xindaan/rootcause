"""T-0535: randomisierter Dosis-Test -- Plan, Klemmung, Hook, Persistenz.

Der Test ist standardmaessig INAKTIV; die wichtigste Zusicherung hier ist
deshalb, dass ohne `aktiv: true` nichts vom Bestandsverhalten abweicht.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta

import pytest

from bewaesserung import dosis_test
from bewaesserung.dosis_test import (
    DosisTestKonfig,
    MIN_DAUER_SEKUNDEN,
    haupt_sekunden,
    plan_sequenz,
    stufe_fuer_lauf,
)
from bewaesserung.entscheidung import Entscheidungsmotor
from bewaesserung.modelle import (
    GesamtKonfig,
    GardenaKonfig,
    WetterKonfig,
    WetterStunde,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.wetter import WetterVorhersage

JETZT = datetime(2026, 8, 12, 6, 0)
GERAET = "bbbb0001-0000-4000-8000-000000000001"


def _konfig(**over) -> DosisTestKonfig:
    basis = DosisTestKonfig(
        aktiv=True,
        ventil_geraet_id=GERAET,
        ventil_kanal=2,
        stufen_gesamt_min=(45, 60, 75),
        wiederholungen=8,
        seed=20260812,
        bis=date(2026, 9, 30),
    )
    return dataclasses.replace(basis, **over)


# --- Plan: Blockrandomisierung -------------------------------------------

def test_sequenz_ist_blockweise_balanciert():
    """Jeder Block enthaelt jede Stufe genau einmal."""
    seq = plan_sequenz(_konfig())
    assert len(seq) == 24
    for start in range(0, 24, 3):
        assert sorted(seq[start:start + 3]) == [45, 60, 75]


def test_sequenz_ist_seed_deterministisch_und_seed_abhaengig():
    assert plan_sequenz(_konfig()) == plan_sequenz(_konfig())
    anders = plan_sequenz(_konfig(seed=1))
    # Andere Saat -> andere Reihenfolge, aber gleiche Zusammensetzung.
    assert anders != plan_sequenz(_konfig())
    assert sorted(anders) == sorted(plan_sequenz(_konfig()))


def test_sequenz_nutzt_nicht_das_globale_random():
    """Fremde Wuerfel im Prozess duerfen den Plan nicht verschieben."""
    import random as _random

    _random.seed(1)
    a = plan_sequenz(_konfig())
    _random.seed(999)
    [_random.random() for _ in range(50)]
    b = plan_sequenz(_konfig())
    assert a == b


def test_sequenz_leer_ohne_stufen_oder_wiederholungen():
    assert plan_sequenz(_konfig(stufen_gesamt_min=())) == []
    assert plan_sequenz(_konfig(wiederholungen=0)) == []


# --- stufe_fuer_lauf ------------------------------------------------------

def test_stufe_none_wenn_inaktiv():
    assert stufe_fuer_lauf(_konfig(aktiv=False), 0, date(2026, 8, 12)) is None


def test_stufe_none_wenn_plan_erschoepft():
    k = _konfig()
    assert stufe_fuer_lauf(k, 23, date(2026, 8, 12)) is not None
    assert stufe_fuer_lauf(k, 24, date(2026, 8, 12)) is None
    assert stufe_fuer_lauf(k, -1, date(2026, 8, 12)) is None


def test_stufe_none_nach_bis_datum():
    k = _konfig()
    assert stufe_fuer_lauf(k, 0, date(2026, 9, 30)) is not None  # bis inkl.
    assert stufe_fuer_lauf(k, 0, date(2026, 10, 1)) is None


def test_stufe_ohne_bis_laeuft_weiter():
    assert stufe_fuer_lauf(_konfig(bis=None), 0, date(2027, 1, 1)) is not None


# --- haupt_sekunden -------------------------------------------------------

def test_haupt_sekunden_zieht_pre_soak_ab():
    assert haupt_sekunden(45, 5, 5400) == 40 * 60
    assert haupt_sekunden(60, 5, 5400) == 55 * 60
    # 75 - 5 = 70 min = 4200 s muss unter bambuswalds max 5400 durchpassen.
    assert haupt_sekunden(75, 5, 5400) == 4200


def test_haupt_sekunden_klemmt_oben_und_warnt():
    from structlog.testing import capture_logs

    with capture_logs() as eintraege:
        assert haupt_sekunden(75, 5, 1800) == 1800
    treffer = [e for e in eintraege if e["event"] == "dosis_test.stufe_geklemmt"]
    assert treffer, (
        "Eine still gekuerzte Teststufe macht den Messpunkt wertlos -- "
        "die Klemmung MUSS geloggt werden"
    )
    # Beide Werte muessen im Log stehen, sonst ist die Warnung nicht
    # auswertbar.
    assert treffer[0]["gewuenscht_s"] == 4200
    assert treffer[0]["gefahren_s"] == 1800


def test_haupt_sekunden_klemmt_unten():
    # Stufe kleiner als der Pre-Soak: nie unter die Engine-Mindestdauer.
    assert haupt_sekunden(5, 5, 5400) == MIN_DAUER_SEKUNDEN
    assert haupt_sekunden(3, 5, 5400) == MIN_DAUER_SEKUNDEN


def test_haupt_sekunden_ohne_klemmung_loggt_nicht():
    from structlog.testing import capture_logs

    with capture_logs() as eintraege:
        haupt_sekunden(60, 5, 5400)
    assert not [
        e for e in eintraege if e["event"] == "dosis_test.stufe_geklemmt"
    ]


# --- gilt_fuer_zone: Kanal, nicht Zone -----------------------------------

def _zone(zone_id: str, *, geraet: str = GERAET, kanal: int = 2) -> ZonenKonfig:
    return ZonenKonfig(
        zone_id=zone_id, name=zone_id, modus=ZonenModus.AUTOMATIK,
        ventil_geraet_id=geraet, ventil_kanal=kanal,
        feuchte_schwelle_min=60.0, feuchte_kritisch=45.0,
        max_dauer_sekunden=5400, min_pause_minuten=120,
        tages_budget_sekunden=7200.0,
        pre_soak_min=5, pre_soak_pause_min=25, pre_soak_modus="immer",
    )


def test_gilt_fuer_zone_matcht_hardware_nicht_zone_id():
    k = _konfig()
    # Beide Bambus-Zonen haengen am selben Ventil -> beide treffen.
    assert dosis_test.gilt_fuer_zone(k, _zone("bambuswald")) is True
    assert dosis_test.gilt_fuer_zone(k, _zone("bambuswald_yogaraum")) is True
    # Anderer Kanal / anderes Geraet -> nicht.
    assert dosis_test.gilt_fuer_zone(k, _zone("hecke", kanal=1)) is False
    assert dosis_test.gilt_fuer_zone(
        k, _zone("hecke", geraet="dswc2"),
    ) is False
    # Inaktiv -> nie.
    assert dosis_test.gilt_fuer_zone(
        _konfig(aktiv=False), _zone("bambuswald"),
    ) is False


# --- Hook in _dauer_mit_ml_weiche ----------------------------------------

class SpeicherAttrappe:
    """Nur was `_dauer_mit_ml_weiche` + der Hook anfassen."""

    def __init__(self, laeufe: int = 0):
        self.laeufe = laeufe
        self.vorschlaege: list[dict] = []
        self.zaehl_aufrufe = 0

    async def zaehle_dosis_test_laeufe(self, geraet_id, kanal):
        self.zaehl_aufrufe += 1
        return self.laeufe

    async def speichere_dauer_vorschlag(self, **kw) -> int:
        self.vorschlaege.append(kw)
        return len(self.vorschlaege)

    async def hole_wirkung_fit(self, zone_id):
        return None

    async def hole_kalibrierung(self, *a, **kw):
        return []


class WetterManagerAttrappe:
    class _Client:
        regen_schwelle_mm = 2.0

        async def hole_vorhersage(self):
            return WetterVorhersage(
                abfrage_zeitstempel=JETZT,
                stunden=[
                    WetterStunde(
                        zeitstempel=JETZT + timedelta(hours=i + 1),
                        temperatur=18.0, niederschlag_mm=0.0,
                        niederschlag_wahrscheinlichkeit=0.0,
                        wind_kmh=8.0, et0_mm=0.3,
                    )
                    for i in range(48)
                ],
            )

    def __init__(self):
        self._c = self._Client()

    @property
    def standard_client(self):
        return self._c

    def hole_client(self, standort_id):
        return self._c


def _gesamt_konfig(dt_konfig: DosisTestKonfig) -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=[],
        wetter=WetterKonfig(breite=52.52, laenge=13.40),
        dosis_test=dt_konfig,
    )


def _motor(speicher, dt_konfig: DosisTestKonfig, zonen):
    return Entscheidungsmotor(
        speicher, WetterManagerAttrappe(), zonen,
        konfig=_gesamt_konfig(dt_konfig),
    )


async def _dauer(motor, zone):
    return await motor._dauer_mit_ml_weiche(
        zone, JETZT, aktuelle_feuchte=40.0, et0_6h=1.0, ziel_schwelle=70.0,
    )


@pytest.mark.asyncio
async def test_hook_greift_nicht_bei_inaktivem_test():
    """Default-Zustand: die Dosis kommt weiter aus der Heuristik."""
    zone = _zone("bambuswald")
    sp_aus = SpeicherAttrappe()
    dauer_aus = await _dauer(_motor(sp_aus, _konfig(aktiv=False), [zone]), zone)
    sp_an = SpeicherAttrappe()
    dauer_an = await _dauer(_motor(sp_an, _konfig(), [zone]), zone)

    assert sp_aus.zaehl_aufrufe == 0, "Inaktiv darf nicht mal zaehlen"
    assert dauer_an != dauer_aus
    assert dauer_an in (40 * 60, 55 * 60, 70 * 60)


@pytest.mark.asyncio
async def test_hook_matcht_kanal_nicht_zone_id():
    """Die Kanal-Entscheidung laeuft unter start_zone -- das kann die
    Geschwister-Zone am selben Ventil sein."""
    geschwister = _zone("bambuswald_yogaraum")
    sp = SpeicherAttrappe()
    dauer = await _dauer(_motor(sp, _konfig(), [geschwister]), geschwister)
    erwartet = haupt_sekunden(
        plan_sequenz(_konfig())[0], 5, 5400,
    )
    assert dauer == erwartet
    assert sp.vorschlaege == []


@pytest.mark.asyncio
async def test_hook_greift_nicht_bei_fremder_zone():
    fremd = _zone("hecke", kanal=1)
    sp = SpeicherAttrappe()
    dauer = await _dauer(_motor(sp, _konfig(), [fremd]), fremd)
    sp_aus = SpeicherAttrappe()
    heuristik = await _dauer(_motor(sp_aus, _konfig(aktiv=False), [fremd]), fremd)
    assert sp.zaehl_aufrufe == 0, "Fremdes Ventil darf nicht mal zaehlen"
    assert dauer == heuristik


@pytest.mark.asyncio
async def test_mehrfacher_aufruf_ohne_verbuchten_lauf_liefert_dieselbe_stufe():
    """Vertrag (a): der Hook liest nur, er zaehlt nicht.

    Beide Aufrufer von `_dauer_mit_ml_weiche` (pruefe_zone + pruefe_kanal)
    koennen im selben Zyklus laufen; eine Entscheidung ist noch kein
    Wasser. Wuerde hier hochgezaehlt, verbrauchte der Plan Stufen ohne
    Messwert.
    """
    zone = _zone("bambuswald")
    sp = SpeicherAttrappe()
    motor = _motor(sp, _konfig(), [zone])
    werte = {await _dauer(motor, zone) for _ in range(5)}
    assert len(werte) == 1
    assert sp.zaehl_aufrufe == 5, "Index kommt jedes Mal frisch aus der DB"


@pytest.mark.asyncio
async def test_stufe_wechselt_erst_mit_verbuchtem_lauf():
    zone = _zone("bambuswald")
    seq = plan_sequenz(_konfig())
    for index in range(4):
        sp = SpeicherAttrappe(laeufe=index)
        dauer = await _dauer(_motor(sp, _konfig(), [zone]), zone)
        assert dauer == haupt_sekunden(seq[index], 5, 5400)


@pytest.mark.asyncio
async def test_hook_schreibt_keinen_dauer_vorschlag():
    """Eine ml_dauer_vorschlag-Zeile wuerde eine Heuristik-Provenienz
    behaupten, die nicht gelaufen ist."""
    from bewaesserung.modelle import MlBewaesserungsResponseKonfig

    zone = _zone("bambuswald")
    sp = SpeicherAttrappe()
    motor = Entscheidungsmotor(
        sp, WetterManagerAttrappe(), [zone],
        response_konfig=MlBewaesserungsResponseKonfig(aktiv=True),
        konfig=_gesamt_konfig(_konfig()),
    )
    await _dauer(motor, zone)
    assert sp.vorschlaege == []


@pytest.mark.asyncio
async def test_hook_faellt_bei_zaehl_fehler_auf_heuristik_zurueck():
    class KaputterSpeicher(SpeicherAttrappe):
        async def zaehle_dosis_test_laeufe(self, geraet_id, kanal):
            raise RuntimeError("DB weg")

    zone = _zone("bambuswald")
    sp_ok = SpeicherAttrappe()
    heuristik = await _dauer(_motor(sp_ok, _konfig(aktiv=False), [zone]), zone)
    sp_kaputt = KaputterSpeicher()
    assert await _dauer(_motor(sp_kaputt, _konfig(), [zone]), zone) == heuristik


# --- Persistenz + Verbuchen ----------------------------------------------

@pytest.mark.asyncio
async def test_persistenz_zaehlt_pro_hardware(tmp_path):
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "dt.db"))
    await sp.verbinden()
    try:
        assert await sp.zaehle_dosis_test_laeufe(GERAET, 2) == 0
        await sp.speichere_dosis_test_lauf(
            zeitstempel=JETZT, ventil_geraet_id=GERAET, ventil_kanal=2,
            stufe_gesamt_min=60, haupt_sekunden=3300,
            lauf_gruppe="presoak_bambuswald_20260812060000",
            zone_id="bambuswald",
        )
        # Zweiter Lauf unter der GESCHWISTER-Zone -- selbe Hardware,
        # muss mitzaehlen.
        await sp.speichere_dosis_test_lauf(
            zeitstempel=JETZT + timedelta(days=1), ventil_geraet_id=GERAET,
            ventil_kanal=2, stufe_gesamt_min=45, haupt_sekunden=2400,
            lauf_gruppe="presoak_bambuswald_yogaraum_20260813060000",
            zone_id="bambuswald_yogaraum",
        )
        assert await sp.zaehle_dosis_test_laeufe(GERAET, 2) == 2
        # Anderer Kanal / anderes Geraet zaehlt getrennt.
        assert await sp.zaehle_dosis_test_laeufe(GERAET, 1) == 0
        assert await sp.zaehle_dosis_test_laeufe("dswc2", 2) == 0
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_verbuche_lauf_genau_einmal_und_schreitet_fort(tmp_path):
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "dt2.db"))
    await sp.verbinden()
    try:
        zone = _zone("bambuswald")
        k = _konfig()
        seq = plan_sequenz(k)
        for i in range(3):
            ok = await dosis_test.verbuche_lauf(
                sp, k, zone, f"presoak_{i}", JETZT + timedelta(days=i),
                haupt_sekunden_ist=1234,
            )
            assert ok is True
        assert await sp.zaehle_dosis_test_laeufe(GERAET, 2) == 3

        zeilen = await sp._db.execute_fetchall(
            "SELECT stufe_gesamt_min, haupt_sekunden, lauf_gruppe, zone_id "
            "FROM dosis_test_lauf ORDER BY id",
        )
        assert [z[0] for z in zeilen] == seq[:3]
        # Ist-Dauer wird uebernommen, nicht der rekonstruierte Sollwert.
        assert [z[1] for z in zeilen] == [1234, 1234, 1234]
        assert [z[2] for z in zeilen] == ["presoak_0", "presoak_1", "presoak_2"]
        assert {z[3] for z in zeilen} == {"bambuswald"}
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_verbuche_lauf_ignoriert_fremde_zone_und_inaktiv(tmp_path):
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "dt3.db"))
    await sp.verbinden()
    try:
        assert await dosis_test.verbuche_lauf(
            sp, _konfig(aktiv=False), _zone("bambuswald"), "g", JETZT,
        ) is False
        assert await dosis_test.verbuche_lauf(
            sp, _konfig(), _zone("hecke", kanal=1), "g", JETZT,
        ) is False
        assert await sp.zaehle_dosis_test_laeufe(GERAET, 2) == 0
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_verbuche_lauf_stoppt_am_planende(tmp_path):
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "dt4.db"))
    await sp.verbinden()
    try:
        k = _konfig(wiederholungen=1)  # nur 3 Laeufe im Plan
        zone = _zone("bambuswald")
        for i in range(3):
            assert await dosis_test.verbuche_lauf(
                sp, k, zone, f"g{i}", JETZT,
            ) is True
        assert await dosis_test.verbuche_lauf(sp, k, zone, "g3", JETZT) is False
        assert await sp.zaehle_dosis_test_laeufe(GERAET, 2) == 3
    finally:
        await sp.schliessen()


# --- Config-Whitelist-Drift ----------------------------------------------

def test_config_block_kommt_am_verbraucher_an(tmp_path):
    """Der YAML-Block muss durch ALLE Registrierungsstellen durchkommen:
    Dataclass, Parser in `lade_konfig`, Feld in `GesamtKonfig`.

    Faellt eine davon weg, kommen die Werte hier als Default an -- genau
    der stille Ausfall aus fehlerpattern_config_whitelist.
    """
    from bewaesserung.konfig import lade_konfig

    pfad = tmp_path / "konf.yaml"
    pfad.write_text(
        "gardena:\n"
        "  client_id: a\n"
        "  client_secret: b\n"
        "zonen: []\n"
        "wetter:\n"
        "  breite: 52.52\n"
        "  laenge: 13.0\n"
        "dosis_test:\n"
        "  aktiv: true\n"
        f'  ventil_geraet_id: "{GERAET}"\n'
        "  ventil_kanal: 2\n"
        "  stufen_gesamt_min: [45, 60, 75]\n"
        "  wiederholungen: 8\n"
        "  seed: 20260812\n"
        '  bis: "2026-09-30"\n',
        encoding="utf-8",
    )
    k = lade_konfig(pfad).dosis_test
    assert k.aktiv is True
    assert k.ventil_geraet_id == GERAET
    assert k.ventil_kanal == 2
    assert k.stufen_gesamt_min == (45, 60, 75)
    assert k.wiederholungen == 8
    assert k.seed == 20260812
    assert k.bis == date(2026, 9, 30)


def test_config_ohne_block_ist_inaktiv(tmp_path):
    from bewaesserung.konfig import lade_konfig

    pfad = tmp_path / "leer.yaml"
    pfad.write_text(
        "gardena:\n  client_id: a\n  client_secret: b\n"
        "zonen: []\nwetter:\n  breite: 52.52\n  laenge: 13.40\n",
        encoding="utf-8",
    )
    k = lade_konfig(pfad).dosis_test
    assert k.aktiv is False
    assert plan_sequenz(k) == []


@pytest.mark.live_config
def test_ausgelieferte_konfig_ist_scharf_und_plausibel():
    """Der Test ist seit 11.08.2026 bewusst SCHARF -- er faehrt echtes Wasser.

    Bis dahin stand hier `aktiv is False` ("nicht versehentlich scharf
    ausliefern"). Nach Andres Entscheid ist das Gegenteil der Sollzustand,
    und die Zusicherung waere sonst wertlos. Geprueft wird deshalb, dass
    die scharfe Konfig auf die RICHTIGE Hardware zeigt und keine Stufe
    still geklemmt wird -- ein Tippfehler in Geraete-ID oder Kanal wuerde
    sonst eine fremde Zone bewaessern.
    """
    from bewaesserung.konfig import lade_konfig

    konfig = lade_konfig()
    dt = konfig.dosis_test
    assert dt.aktiv is True
    # Die Hardware wird ueber die Konfig selbst geprueft, nicht ueber eine
    # hartkodierte Geraete-ID: das Ventil (Geraet + Kanal) des Dosis-Tests
    # muss genau den Bambus-Zonen gehoeren. Ein Tippfehler in ID oder Kanal
    # liefert eine andere oder leere Menge und schlaegt hier an.
    zonen_am_ventil = {
        z.zone_id for z in konfig.zonen
        if z.ventil_geraet_id == dt.ventil_geraet_id
        and z.ventil_kanal == dt.ventil_kanal
    }
    assert zonen_am_ventil == {"bambuswald", "bambuswald_yogaraum"}
    # Der Plan muss aufgehen: 3 Stufen x 8 Wiederholungen, balanciert.
    seq = plan_sequenz(dt)
    assert len(seq) == 24
    assert sorted(set(seq)) == [45, 60, 75]
    # Keine Stufe darf an max_dauer_sekunden haengenbleiben -- eine still
    # gekuerzte Stufe macht ihren Messpunkt wertlos. Gilt fuer JEDE Zone am
    # Testkanal, weil die Kanal-Entscheidung unter jeder von ihnen laufen
    # kann.
    am_kanal = [
        z for z in konfig.zonen
        if z.ventil_geraet_id == dt.ventil_geraet_id
        and z.ventil_kanal == dt.ventil_kanal
    ]
    assert am_kanal
    for z in am_kanal:
        for stufe in dt.stufen_gesamt_min:
            assert haupt_sekunden(
                stufe, z.pre_soak_min, z.max_dauer_sekunden,
            ) == (stufe - z.pre_soak_min) * 60, (
                f"Stufe {stufe} min wird bei {z.zone_id} geklemmt"
            )


@pytest.mark.live_config
def test_alle_zonen_am_testkanal_haben_pre_soak():
    """Vorbedingung des Verbuchens -- bricht sie, stallt die Messreihe still.

    Verbucht wird ausschliesslich im Pre-Soak-Zweig des Auto-Loops
    (`main.py`, `_pre_soak_policy_aktiv(ref_zone) and ps_mgr is not None`).
    Der Else-Zweig (`kanal_sicherung.bewaessere`) ueberschreibt die Dosis
    weiter, zaehlt aber NICHT -- der Test bliebe dann fuer immer auf
    Stufe 1 und produzierte eine Messreihe ohne Variation.

    Welche Zone `ref_zone` ist, entscheidet die Kanal-Reihenfolge; es kann
    jede Zone am Kanal sein (so auch im Config-Kommentar von
    `bambuswald_yogaraum` vermerkt). Die Vorbedingung muss deshalb fuer
    ALLE Zonen am Testkanal gelten, nicht nur fuer bambuswald.

    Faellt dieser Test, ist die Konsequenz NICHT "Pre-Soak wieder
    anschalten", sondern: das Verbuchen im Else-Zweig nachziehen, bevor
    `dosis_test.aktiv` auf true geht.
    """
    from bewaesserung.konfig import lade_konfig

    konfig = lade_konfig()
    dt = konfig.dosis_test
    am_kanal = [
        z for z in konfig.zonen
        if z.ventil_geraet_id == dt.ventil_geraet_id
        and z.ventil_kanal == dt.ventil_kanal
    ]
    assert am_kanal, "Testkanal ohne Zonen -- Konfig zeigt ins Leere"
    ohne_pre_soak = [
        z.zone_id for z in am_kanal
        if not (z.pre_soak_modus == "immer" and z.pre_soak_min)
    ]
    assert not ohne_pre_soak, (
        f"Zonen am Testkanal ohne Pre-Soak-Policy: {ohne_pre_soak}. "
        "Diese Laeufe wuerden die Dosis ueberschrieben bekommen, aber "
        "nicht verbucht -- die Messreihe stallt auf Stufe 1."
    )


# --- Anzeige-Pfad: vorhersage_zone zeigt die gefahrene Dauer --------------
#
# Folgeaufgabe zu T-0535. `vorhersage_zone` (Dashboard) und `pruefe_kanal`
# (Auto-Loop) muessen dieselbe Dauer melden -- laeuft der Dosis-Test scharf,
# faehrt der Loop die Teststufe, waehrend der Anzeige-Pfad ohne diesen Hook
# weiter heuristisch rechnete. Der Kern-Test hier ist deshalb die GLEICHHEIT
# gegen den scharfen Pfad, nicht ein Erwartungswert aus einer zweiten Formel.

from test_entscheidung import JETZT as JETZT_VZ  # noqa: E402
from test_vorhersage_zone import _motor as _basis_motor  # noqa: E402


def _anzeige_zone(zone_id: str = "bambuswald", **over) -> ZonenKonfig:
    """Zone am Testventil, vollstaendig genug fuer `vorhersage_zone`."""
    from bewaesserung.modelle import ZeitFenster

    basis = dict(
        zone_id=zone_id, name=zone_id, modus=ZonenModus.AUTOMATIK,
        ventil_geraet_id=GERAET, ventil_kanal=2,
        feuchte_schwelle_min=60.0, feuchte_schwelle_max=80.0,
        feuchte_kritisch=45.0,
        max_dauer_sekunden=5400, min_pause_minuten=120,
        tages_budget_sekunden=7200.0,
        pre_soak_min=5, pre_soak_pause_min=25, pre_soak_modus="immer",
        bevorzugte_zeiten=[ZeitFenster(von="05:00", bis="07:00")],
        flaeche_m2=40.0, anteil_kanal=1.0,
    )
    basis.update(over)
    return ZonenKonfig(**basis)


def _anzeige_motor(zone, dt_konfig, *, laeufe: int = 0, feuchte: float = 40.0):
    """`vorhersage_zone`-Motor mit Dosis-Test-Konfig + Zaehl-Stub.

    Der Zaehl-Stub protokolliert Lese- UND Schreibzugriffe, damit der
    Seiteneffekt-Test nicht nur "gleicher Wert" prueft, sondern belegt, dass
    ueberhaupt kein Lauf verbucht wurde.
    """
    motor, speicher = _basis_motor(zone, feuchte=feuchte, jetzt=JETZT_VZ)
    motor._konfig = _gesamt_konfig(dt_konfig)
    speicher.dosis_test_zaehl_aufrufe = 0
    speicher.dosis_test_verbucht = []

    async def _zaehle(geraet_id, kanal):
        speicher.dosis_test_zaehl_aufrufe += 1
        return laeufe

    async def _speichere(**kw):
        speicher.dosis_test_verbucht.append(kw)

    speicher.zaehle_dosis_test_laeufe = _zaehle
    speicher.speichere_dosis_test_lauf = _speichere
    return motor, speicher


@pytest.mark.asyncio
async def test_anzeige_ohne_test_liefert_none():
    """Default-Zustand: das neue Feld bleibt leer, alles andere unveraendert."""
    zone = _anzeige_zone()
    motor, speicher = _anzeige_motor(zone, _konfig(aktiv=False))
    empf = await motor.vorhersage_zone(zone.zone_id)

    assert empf.dauer_s_dosis_test is None
    assert speicher.dosis_test_zaehl_aufrufe == 0, "Inaktiv darf nicht zaehlen"
    assert empf.dauer_s_heuristik is not None


@pytest.mark.asyncio
async def test_anzeige_ist_identisch_mit_dem_scharfen_pfad():
    """Kern der Aufgabe: Dashboard-Zahl == vom Auto-Loop gefahrene Dauer.

    Verglichen wird gegen `_dauer_mit_ml_weiche` -- die Funktion, die
    `pruefe_zone`/`pruefe_kanal` benutzen. Ein Erwartungswert aus einer
    nachgebauten Formel wuerde genau die Divergenz nicht bemerken, um die
    es hier geht.
    """
    zone = _anzeige_zone()
    motor, _ = _anzeige_motor(zone, _konfig())
    empf = await motor.vorhersage_zone(zone.zone_id)
    scharf = await motor._dauer_mit_ml_weiche(
        zone, JETZT_VZ, aktuelle_feuchte=40.0, et0_6h=1.0, ziel_schwelle=70.0,
    )

    assert empf.dauer_s_dosis_test == scharf
    # Gegenprobe: die Gleichheit ist nicht trivial, weil der Testwert von
    # der Heuristik abweicht -- sonst koennte der Hook auch fehlen.
    assert empf.dauer_s_dosis_test != empf.dauer_s_heuristik
    assert empf.dauer_s_dosis_test in (40 * 60, 55 * 60, 70 * 60)


@pytest.mark.asyncio
async def test_anzeige_ueberschreibt_die_heuristik_nicht():
    """`dauer_s_heuristik` bleibt die echte Heuristik (Drift-Ampel/MAE)."""
    zone = _anzeige_zone()
    motor_an, _ = _anzeige_motor(zone, _konfig())
    motor_aus, _ = _anzeige_motor(zone, _konfig(aktiv=False))
    an = await motor_an.vorhersage_zone(zone.zone_id)
    aus = await motor_aus.vorhersage_zone(zone.zone_id)

    assert an.dauer_s_heuristik == aus.dauer_s_heuristik
    assert an.liter_heuristik == aus.liter_heuristik


@pytest.mark.asyncio
async def test_anzeige_liter_haupt_folgt_der_testdosis():
    """Die angezeigte Wassermenge muss dem entsprechen, was fliesst."""
    zone = _anzeige_zone()
    motor, _ = _anzeige_motor(zone, _konfig())
    empf = await motor.vorhersage_zone(zone.zone_id)

    erwartet = motor._liter_fuer_dauer(zone, empf.dauer_s_dosis_test)
    assert empf.liter_haupt == erwartet
    assert empf.liter_haupt != empf.liter_heuristik


@pytest.mark.asyncio
async def test_anzeige_verbraucht_keinen_testlauf():
    """Vertrag (a): ein Dashboard-Poll darf den Testplan nicht verbrennen.

    `vorhersage_zone` laeuft bei jedem Poll. Wuerde der Hook zaehlen, waeren
    die Stufen aufgebraucht, bevor je Wasser lief.
    """
    zone = _anzeige_zone()
    motor, speicher = _anzeige_motor(zone, _konfig())
    werte = {
        (await motor.vorhersage_zone(zone.zone_id)).dauer_s_dosis_test
        for _ in range(5)
    }

    assert len(werte) == 1, "Stufe darf sich ohne verbuchten Lauf nicht drehen"
    assert speicher.dosis_test_verbucht == []
    assert speicher.dosis_test_zaehl_aufrufe == 5, (
        "Index kommt jedes Mal frisch aus der DB (kein Cache)"
    )


@pytest.mark.asyncio
async def test_anzeige_greift_nicht_bei_fremdem_ventil():
    zone = _anzeige_zone("hecke", ventil_kanal=1)
    motor, speicher = _anzeige_motor(zone, _konfig())
    empf = await motor.vorhersage_zone(zone.zone_id)

    assert empf.dauer_s_dosis_test is None
    assert speicher.dosis_test_zaehl_aufrufe == 0
