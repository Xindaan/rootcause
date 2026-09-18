"""T-0422: FAO-56-Wasserbilanz (Shadow).

Die Zahlen orientieren sich am realen Standort: Sandboden unter Kiefern,
Flachwurzler, Zr ~0,25 m -> TAW ~22 mm, RAW ~9 mm. Bei Sommer-ET0 von
4,5 mm/Tag sind das unter zwei Tage Puffer -- das ist der Grund, warum
Regen-Skepsis hier teurer ist als auf Lehm.
"""

import pytest

from bewaesserung.wasserbilanz import (
    BODENART_DEFAULT,
    entscheidung_mit_ensemble,
    liter_zu_mm,
    schreibe_fort,
    speicher_fuer_zone,
)


# --------------------------------------------------------------------------
# Speichergroessen
# --------------------------------------------------------------------------

def test_taw_und_raw_fuer_den_realen_standort():
    """Lehmiger Sand, Zr 0,25 m, p 0,4 -> TAW 22,5 / RAW 9,0 mm."""
    s = speicher_fuer_zone()
    assert s.bodenart == BODENART_DEFAULT
    assert s.taw_mm == pytest.approx(22.5)
    assert s.raw_mm == pytest.approx(9.0)


def test_sand_hat_weniger_puffer_als_lehm():
    """Der quantitative Kern von T-0422: auf Sand ist der Puffer unter zwei
    Tagen, auf Lehm mehr als doppelt so lang. Deshalb ist ein uebersehener
    Giess-Tag hier nicht 'etwas trockener', sondern Stress."""
    sand = speicher_fuer_zone("sand")
    lehm = speicher_fuer_zone("lehm")
    assert sand.puffer_tage < 2.0
    assert lehm.puffer_tage > 2 * sand.puffer_tage


def test_tiefere_wurzeln_geben_mehr_puffer():
    flach = speicher_fuer_zone(wurzeltiefe_m=0.25)
    tief = speicher_fuer_zone(wurzeltiefe_m=0.60)
    assert tief.raw_mm > flach.raw_mm * 2


def test_unbekannte_bodenart_faellt_auf_default():
    assert speicher_fuer_zone("mondgestein").taw_mm == speicher_fuer_zone().taw_mm


# --------------------------------------------------------------------------
# Fortschreibung
# --------------------------------------------------------------------------

def test_verdunstung_erhoeht_die_auszehrung():
    s = speicher_fuer_zone()
    schritt = schreibe_fort(0.0, et0_mm=4.5, regen_mm=0, bewaesserung_mm=0,
                            speicher=s)
    assert schritt.dr_nachher_mm == pytest.approx(4.5)
    assert schritt.giessen is False, "4,5 mm liegen unter RAW 9,0"


def test_zwei_trockene_tage_loesen_aus():
    """Der Realfall auf Sand: nach zwei Tagen ohne Regen ist die nutzbare
    Reserve aufgebraucht."""
    s = speicher_fuer_zone()
    dr = 0.0
    for _ in range(2):
        schritt = schreibe_fort(dr, 4.5, 0, 0, s)
        dr = schritt.dr_nachher_mm
    assert dr == pytest.approx(9.0)
    schritt = schreibe_fort(dr, 4.5, 0, 0, s)
    assert schritt.giessen is True


def test_regen_baut_kein_guthaben_auf():
    """DER Test fuer die Dr>=0-Kappung. Ein Starkregen darf keine negative
    Auszehrung erzeugen -- sonst puffert er faelschlich die naechste
    Trockenphase ab. Auf Sand laeuft der Ueberschuss durch."""
    s = speicher_fuer_zone()
    schritt = schreibe_fort(2.0, et0_mm=0, regen_mm=30.0, bewaesserung_mm=0,
                            speicher=s)
    assert schritt.dr_nachher_mm == 0.0, "kein Guthaben unter Feldkapazitaet"


def test_auszehrung_wird_bei_taw_gedeckelt():
    """Unterhalb des Welkepunkts gibt es nichts mehr zu entnehmen. Ohne
    Deckel taeuschte die Bilanz nach Regen eine viel zu lange
    Wiederauffuell-Phase vor."""
    s = speicher_fuer_zone()
    schritt = schreibe_fort(s.taw_mm, et0_mm=50.0, regen_mm=0,
                            bewaesserung_mm=0, speicher=s)
    assert schritt.dr_nachher_mm == pytest.approx(s.taw_mm)


def test_bewaesserung_senkt_die_auszehrung():
    s = speicher_fuer_zone()
    schritt = schreibe_fort(9.0, et0_mm=0, regen_mm=0, bewaesserung_mm=6.0,
                            speicher=s)
    assert schritt.dr_nachher_mm == pytest.approx(3.0)
    assert schritt.giessen is False


def test_kc_skaliert_die_verdunstung():
    """Kc bildet ab, dass eine Zone mehr oder weniger verdunstet als die
    Referenz-Grasflaeche."""
    s = speicher_fuer_zone()
    normal = schreibe_fort(0, 4.0, 0, 0, s, kc=1.0)
    durstig = schreibe_fort(0, 4.0, 0, 0, s, kc=1.5)
    assert durstig.dr_nachher_mm > normal.dr_nachher_mm


# --------------------------------------------------------------------------
# Liter -> mm
# --------------------------------------------------------------------------

def test_liter_zu_mm():
    assert liter_zu_mm(20.0, 2.0) == pytest.approx(10.0)


def test_liter_zu_mm_ohne_flaeche_ist_null_statt_absturz():
    assert liter_zu_mm(20.0, None) == 0.0
    assert liter_zu_mm(20.0, 0) == 0.0


# --------------------------------------------------------------------------
# Die Entscheidung mit Ensemble-Regen (T-0423-Kopplung)
# --------------------------------------------------------------------------

def test_konservativer_regen_entscheidet_anders_als_der_median():
    """Der Kern der T-0422/T-0423-Kopplung: bei uneinigem Ensemble kollabiert
    p20, und dann wird gegossen -- obwohl der Median einen Skip nahelegen
    wuerde. Die Kosten sind asymmetrisch (unnoetiger Guss billig,
    ausgefallener auf 9 mm RAW teuer)."""
    s = speicher_fuer_zone()
    dr = 7.0
    giessen_p20, _ = entscheidung_mit_ensemble(dr, 4.5, 1.2, s)
    giessen_median, _ = entscheidung_mit_ensemble(dr, 4.5, 2.7, s)
    assert giessen_p20 is True, "p20 1,2 mm -> Reserve reicht nicht"
    assert giessen_median is False, "Median 2,7 mm -> Skip"


def test_einiges_ensemble_erlaubt_den_skip():
    """Landregen: p20 nahe Median -> die Bilanz darf den Regen einrechnen."""
    s = speicher_fuer_zone()
    giessen, dr = entscheidung_mit_ensemble(7.0, 4.5, 5.0, s)
    assert giessen is False
    assert dr == pytest.approx(6.5)


def test_prognose_wird_ebenfalls_gekappt():
    giessen, dr = entscheidung_mit_ensemble(2.0, 0.0, 30.0,
                                            speicher_fuer_zone())
    assert dr == 0.0
    assert giessen is False


# --------------------------------------------------------------------------
# T-0422 SCOPE-GRENZE: Tropf vs. Sprinkler
# --------------------------------------------------------------------------

def test_mm_naeherung_bricht_bei_tropfbewaesserung():
    """DER Befund aus dem Realdaten-Lauf (23.07.).

    Die mm-Bilanz unterstellt flaechige Verteilung. Bei Mikrodrip-Ringen um
    einzelne Horste ist die Config-`flaeche_m2` aber der WURZELBALLEN
    (0,58 m2 je Bambus), nicht die benetzte Flaeche -- und das Wasser geht
    punktuell hinein.

    Gerechnet gegen die echten Juli-Daten:
      bambuswald (Tropf, 1,74 m2):  20-47 mm je Gabe -> Dr klebt bei 0,0
      waldblumenhain (Sprinkler, 40 m2): 15 mm je Gabe -> Dr bewegt sich
      plausibel zwischen 0 und 7,1 bei RAW 9,0

    Fuer Tropf-Zonen liefert die Bilanz also systematisch "nie giessen".
    Sie ist dort ein Indikator, keine Entscheidungsgrundlage.
    """
    s = speicher_fuer_zone()
    # Ein realer Bambus-Tag: 60 L auf 1,74 m2 Ballenflaeche
    tropf_mm = liter_zu_mm(60.0, 1.74)
    # Ein realer Waldblumen-Tag: 610 L auf 40 m2
    sprinkler_mm = liter_zu_mm(610.0, 40.0)

    assert tropf_mm > s.taw_mm, (
        "Tropf-mm uebersteigt den gesamten Bodenspeicher -- die Groesse ist "
        "dort nicht interpretierbar"
    )
    assert sprinkler_mm < s.taw_mm, (
        "Sprinkler-mm bleibt im Bereich des Speichers und ist verwertbar"
    )


def test_tropf_zone_wuerde_nie_giessen():
    """Konkrete Folge: mit Tropf-mm faellt Dr nach jeder Gabe auf 0 und die
    Bedingung `Dr > RAW` wird nie wahr -- egal wie trocken es real ist."""
    s = speicher_fuer_zone()
    dr = s.raw_mm  # schon an der Stressgrenze
    schritt = schreibe_fort(dr, et0_mm=4.5, regen_mm=0,
                            bewaesserung_mm=liter_zu_mm(60.0, 1.74),
                            speicher=s)
    assert schritt.dr_nachher_mm == 0.0
    assert schritt.giessen is False


# --------------------------------------------------------------------------
# T-0422 Variante 2: benetzte Flaeche statt Ballenflaeche
# --------------------------------------------------------------------------

def test_benetzte_flaeche_ist_groesser_als_die_ballenflaeche():
    """Der Tropfring liegt 15-20 cm vom Halmfuss AUSSEN herum und benetzt
    mehr als den Wurzelballen. Die Config-`flaeche_m2` (Ballen) ist deshalb
    der falsche Nenner fuer die mm-Bilanz."""
    from bewaesserung.wasserbilanz import benetzte_flaeche_m2
    assert benetzte_flaeche_m2(25) > 1.74   # bambuswald Ballenflaeche
    assert benetzte_flaeche_m2(17) > 1.16   # yogaraum


def test_benetzte_flaeche_macht_die_tagesgabe_interpretierbar():
    """DER Fix. Mit Ballenflaeche ergab eine reale Tagesgabe 46 mm -- das
    Doppelte des gesamten Bodenspeichers, physikalisch sinnlos. Mit der
    benetzten Flaeche sind es ~24 mm, also 'Speicher etwa voll'."""
    from bewaesserung.wasserbilanz import benetzte_flaeche_m2
    s = speicher_fuer_zone()
    alt = liter_zu_mm(80.6, 1.74)
    neu = liter_zu_mm(80.6, benetzte_flaeche_m2(25))
    assert alt > 2 * s.taw_mm, "alte Rechnung: doppelter Speicherinhalt"
    assert neu <= s.taw_mm * 1.1, "neue Rechnung: etwa ein Speicher"


def test_streifen_statt_kreis():
    """Die Memory-Formel 0,0072 x N^2 unterstellt EINEN Kreis aus allen
    Tropfern und waechst quadratisch. Bei 25 Tropfern auf 3 Ringe
    ueberschaetzt sie die Flaeche stark. Der Streifen-Ansatz waechst linear
    und ist unabhaengig von der Ring-Aufteilung -- die hier unbekannt ist."""
    from bewaesserung.wasserbilanz import benetzte_flaeche_m2
    kreis_formel = 0.0072 * 25 ** 2
    assert benetzte_flaeche_m2(25) < kreis_formel
    # linear: doppelte Tropferzahl -> doppelte Flaeche
    assert benetzte_flaeche_m2(50) == pytest.approx(2 * benetzte_flaeche_m2(25))


def test_null_tropfer_gibt_null():
    from bewaesserung.wasserbilanz import benetzte_flaeche_m2
    assert benetzte_flaeche_m2(0) == 0.0
    assert liter_zu_mm(50.0, benetzte_flaeche_m2(0)) == 0.0


# --------------------------------------------------------------------------
# T-0422 Shadow-Job
# --------------------------------------------------------------------------

class _Zone:
    def __init__(self, zone_id, flaeche=None, tropfer=None):
        self.zone_id = zone_id
        self.flaeche_m2 = flaeche
        self.tropfer_anzahl = tropfer


def test_bezugsflaeche_tropf_vs_sprinkler():
    """DER Variante-2-Fix im Job: Tropf-Zonen rechnen ueber die benetzte
    Flaeche, Sprinkler ueber die Zonenflaeche. Mit der Ballenflaeche ergaeben
    sich bei Tropf 46 mm je Gabe -- doppelter Bodenspeicher."""
    from bewaesserung.wasserbilanz_job import bezugsflaeche_m2
    tropf = _Zone("bambuswald", flaeche=1.74, tropfer=25)
    sprinkler = _Zone("waldblumenhain", flaeche=40.0)
    assert bezugsflaeche_m2(tropf) > tropf.flaeche_m2, "benetzt > Ballen"
    assert bezugsflaeche_m2(sprinkler) == 40.0
    assert bezugsflaeche_m2(_Zone("topf")) is None


@pytest.mark.asyncio
async def test_job_schreibt_dr_fort_und_vergleicht_mit_der_engine():
    """Der Job ist eine Vergleichsreihe: er schreibt NEBEN dem Bilanz-Urteil
    mit, was die echte Engine getan hat. Ohne diesen Vergleich waere die
    Reihe wertlos -- man koennte hinterher nicht sagen, ob die Bilanz besser
    entschieden haette."""
    from datetime import datetime, timedelta

    from bewaesserung.wasserbilanz_job import WasserbilanzJob

    class _Sp:
        def __init__(self):
            self.eintraege = []

        async def hole_forecast_stunden(self, standort, von, bis):
            return {von: (0.0, 4.5)}   # kein Regen, 4,5 mm ET0

        async def hole_ventil_ereignisse(self, zone_id, von=None, bis=None):
            return []                   # die Engine hat NICHT gegossen

        async def hole_letzten_bilanz_zustand(self, zone_id):
            return {"dr_mm": 6.0}       # schon 6 mm ausgezehrt

        async def hole_regen_ensemble(self, standort_id=None, limit=200):
            # p20 = 1,2 mm konservativ, 24-h-Horizont, Abruf am Zieltag.
            return [{
                "abfrage_zeitstempel": "2026-07-22T19:00:00",
                "standort_id": standort_id, "horizont_stunden": 24,
                "p20": 1.2, "p50": 2.7,
            }]

        async def speichere_bilanz_zustand(self, zone_id, ts, schritt,
                                           wuerde_giessen, ist_entscheidung,
                                           quelle="",
                                           wuerde_giessen_ensemble=None,
                                           regen_p20_mm=None,
                                           et0_prognose_24h_mm=None):
            self.eintraege.append({
                "zone": zone_id, "dr": schritt.dr_nachher_mm,
                "wuerde": wuerde_giessen, "ist": ist_entscheidung,
                "ens": wuerde_giessen_ensemble, "p20": regen_p20_mm,
                "et0_prog": et0_prognose_24h_mm,
            })

    class _Konfig:
        bilanz = None

    sp = _Sp()
    job = WasserbilanzJob(sp, [_Zone("bambuswald", 1.74, 25)], _Konfig())
    n = await job.aktualisiere_wenn_faellig(datetime(2026, 7, 23, 6, 0))
    assert n == 1
    (e,) = sp.eintraege
    assert e["dr"] == pytest.approx(10.5), "6,0 + 4,5 ET0"
    assert e["wuerde"] is True, "10,5 > RAW 9,0"
    assert e["ist"] is False, "die Engine hat nicht gegossen"
    # T-0423-Konsument: die Ensemble-Regel wird jetzt ausgewertet und
    # NEBEN das Bilanz-Urteil geschrieben. Vorher rief sie nur der Test --
    # damit war T-0425 unbeantwortbar, obwohl die Daten liefen.
    # Rechnung: Dr 10,5 + ET0 4,5 - p20 1,2 = 13,8 > RAW 9,0 -> giessen.
    assert e["ens"] is True
    assert e["p20"] == pytest.approx(1.2)
    assert e["et0_prog"] == pytest.approx(4.5)


@pytest.mark.asyncio
async def test_ensemble_urteil_ist_none_ohne_abruf():
    """Kein Ensemble-Abruf zum Tag -> None, nicht False.

    "Keine Aussage" ist etwas anderes als "kein Regen". Ein False/0.0 haette
    die spaetere Auswertung (T-0425) still mit Phantom-Faellen gefuellt.
    """
    from datetime import datetime

    from bewaesserung.wasserbilanz import speicher_fuer_zone
    from bewaesserung.wasserbilanz_job import WasserbilanzJob

    class _Sp:
        async def hole_regen_ensemble(self, standort_id=None, limit=200):
            return []

    class _Konfig:
        bilanz = None

    job = WasserbilanzJob(_Sp(), [], _Konfig())
    urteil = await job._ensemble_urteil(
        "musterstadt", 10.5, speicher_fuer_zone(), datetime(2026, 7, 22), 4.5,
    )
    assert urteil == (None, None, None)


@pytest.mark.asyncio
async def test_job_intervall_gate():
    from datetime import datetime, timedelta

    from bewaesserung.wasserbilanz_job import WasserbilanzJob

    class _Sp:
        async def hole_forecast_stunden(self, *a): return {}
        async def hole_ventil_ereignisse(self, *a, **k): return []
        async def hole_letzten_bilanz_zustand(self, z): return None
        async def speichere_bilanz_zustand(self, *a, **k): pass

    class _Konfig:
        bilanz = None

    job = WasserbilanzJob(_Sp(), [_Zone("z", 10.0)], _Konfig())
    t0 = datetime(2026, 7, 23, 6, 0)
    assert await job.aktualisiere_wenn_faellig(t0) == 1
    assert await job.aktualisiere_wenn_faellig(t0 + timedelta(hours=2)) == 0
    assert await job.aktualisiere_wenn_faellig(t0 + timedelta(hours=25)) == 1


@pytest.mark.asyncio
async def test_t0488_jede_zone_bekommt_ihr_eigenes_standortwetter():
    """T-0488 (Audit A1): der Job las `wetter_standort` per getattr von der
    ZONE, wo das Feld nicht sitzt -- es liegt auf StandortKonfig. Der Aufruf
    lieferte deshalb immer None und fiel auf `standorte[0]` zurueck.

    Der Kommentar daneben hielt das fuer folgenlos, weil nur Freiland-Zonen
    durch den Flaechenfilter kaemen. Das stimmt nicht: `bezugsflaeche_m2`
    liefert auch fuer Balkon-Toepfe einen Wert (zitrus 0.05 m2, mandevilla
    0.05, kasten_4 0.10), und die liegen am Suedbalkon mit
    `wetter_standort: beispielstadt`. Gemessen am 02.08.: alle acht Bilanzzeilen
    trugen et0=1,8 / regen=7,0 aus Musterstadt, waehrend die neueste
    Beispielstadter Stundenprognose fuer denselben Tag auf 52,7 mm kam.

    Der Test hat deshalb bewusst je eine FLAECHENZONE pro Wetterstandort.
    """
    from datetime import datetime

    from bewaesserung.modelle import StandortKonfig
    from bewaesserung.wasserbilanz_job import WasserbilanzJob

    class _Sp:
        def __init__(self):
            self.gefragt: dict[str, str] = {}
            self._zone_im_lauf = None
        async def hole_forecast_stunden(self, standort_id, von, bis):
            self.gefragt[self._zone_im_lauf] = standort_id
            return {}
        async def hole_ventil_ereignisse(self, zone_id, **k):
            self._zone_im_lauf = zone_id
            return []
        async def hole_letzten_bilanz_zustand(self, z): return {"dr_mm": 5.0}
        async def speichere_bilanz_zustand(self, *a, **k): pass

    class _Konfig:
        bilanz = None
        standorte = [
            StandortKonfig(
                standort_id="garten_musterstadt", name="Garten",
                wetter_standort="musterstadt", zonen=["magerwiese"],
            ),
            StandortKonfig(
                standort_id="suedbalkon", name="Suedbalkon",
                wetter_standort="beispielstadt", zonen=["zitrus"],
            ),
        ]

    sp = _Sp()
    # hole_forecast_stunden laeuft VOR hole_ventil_ereignisse -- der Stub
    # merkt sich die Zone deshalb ueber den Ereignis-Aufruf. Damit die
    # Zuordnung stimmt, je Zone einzeln laufen lassen.
    for zone_id, flaeche, erwartet in (
        ("magerwiese", 40.0, "musterstadt"),
        ("zitrus", 0.05, "beispielstadt"),
    ):
        sp._zone_im_lauf = zone_id
        job = WasserbilanzJob(sp, [_Zone(zone_id, flaeche)], _Konfig())
        await job.aktualisiere_wenn_faellig(datetime(2026, 8, 2, 6, 0))
        assert sp.gefragt[zone_id] == erwartet, (
            f"{zone_id} wurde mit {sp.gefragt[zone_id]}-Wetter bilanziert"
        )


def test_t0488_wetterstandort_mapping_ist_die_eine_wahrheit():
    """Der Helper selbst: Fallback auf `standort_id`, wenn kein
    `wetter_standort` gesetzt ist -- so machen es Entscheidung, API und ML
    bereits. Eine Zone in keinem Block fehlt im Mapping, statt still auf den
    ersten Standort zu fallen."""
    from bewaesserung.modelle import StandortKonfig, wetterstandort_je_zone

    mapping = wetterstandort_je_zone([
        StandortKonfig(
            standort_id="garten", name="G", wetter_standort="musterstadt",
            zonen=["magerwiese", "hecke"],
        ),
        StandortKonfig(
            standort_id="wohnung", name="W", zonen=["avocado"],
        ),
    ])

    assert mapping["magerwiese"] == "musterstadt"
    assert mapping["hecke"] == "musterstadt"
    assert mapping["avocado"] == "wohnung"   # Fallback auf standort_id
    assert "unbekannte_zone" not in mapping
    assert wetterstandort_je_zone(None) == {}


@pytest.mark.asyncio
async def test_t0490_zeitplanwasser_ist_keine_engine_entscheidung():
    """T-0490 (Audit A2): `ist_entscheidung` kam aus `bew_mm > 0`, also aus
    "es floss Wasser". Zeitplan-, App- und AquaBloom-Wasser zaehlen dort mit,
    sind aber keine Engine-Entscheidung.

    Gemessen 02.08.: die Bilanzzeilen von bambuswald, bambuswald_yogaraum und
    hecke fuer den 01.08. trugen `ist_entscheidung=1`, obwohl saemtliche
    Ventil-Events dieser Zonen an dem Tag `ausloser=zeitplan` waren. Die
    geplante Shadow-Auswertung haette die Bilanz gegen ein falsches Label
    verglichen.
    """
    from datetime import datetime

    from bewaesserung.wasserbilanz_job import WasserbilanzJob

    class _Ereignis:
        def __init__(self, ausloser):
            self.aktion = type("A", (), {"value": "schliessen"})()
            self.ausloser = type("B", (), {"value": ausloser})()
            self.dauer_sekunden = 1800
            self.liter = 10.0
            self.ventil_id = "kanal"

    class _Sp:
        def __init__(self, ausloser):
            self.ausloser = ausloser
            self.bew = None
            self.ist = None
        async def hole_forecast_stunden(self, *a): return {}
        async def hole_ventil_ereignisse(self, *a, **k):
            return [_Ereignis(self.ausloser)]
        async def hole_letzten_bilanz_zustand(self, z): return {"dr_mm": 5.0}
        async def speichere_bilanz_zustand(self, zone_id, ts, schritt, **k):
            self.bew = schritt.bewaesserung_mm
            self.ist = k.get("ist_entscheidung")

    class _Konfig:
        bilanz = None
        standorte = []

    # Zeitplan: Wasser floss, die Engine hat nichts entschieden.
    sp = _Sp("zeitplan")
    job = WasserbilanzJob(sp, [_Zone("hecke", 9.6)], _Konfig())
    await job.aktualisiere_wenn_faellig(datetime(2026, 8, 2, 6, 0))
    assert sp.bew > 0, "Setup-Annahme: Zeitplanwasser zaehlt als Wasser"
    assert sp.ist is False, "Zeitplanwasser ist keine Engine-Entscheidung"

    # Gegenprobe: derselbe Lauf als automatik.
    sp2 = _Sp("automatik")
    job2 = WasserbilanzJob(sp2, [_Zone("hecke", 9.6)], _Konfig())
    await job2.aktualisiere_wenn_faellig(datetime(2026, 8, 2, 6, 0))
    assert sp2.ist is True


@pytest.mark.asyncio
async def test_ignorierte_laeufe_zaehlen_nicht_als_wasser():
    """Auf geteilten Kanaelen laufen Fremd-Bewaesserungen als `ignoriert`
    (Grass-Regime magerwiese). Sie oeffnen das Ventil, giessen diese Zone
    aber nicht -- als Wasser-Input gezaehlt wuerden sie die Bilanz
    faelschlich auffuellen."""
    from datetime import datetime

    from bewaesserung.wasserbilanz_job import WasserbilanzJob

    class _Ereignis:
        def __init__(self, ausloser):
            self.aktion = type("A", (), {"value": "schliessen"})()
            self.ausloser = type("B", (), {"value": ausloser})()
            self.dauer_sekunden = 1800

    class _Sp:
        def __init__(self):
            self.dr = None
        async def hole_forecast_stunden(self, *a): return {}
        async def hole_ventil_ereignisse(self, *a, **k):
            return [_Ereignis("ignoriert")]
        async def hole_letzten_bilanz_zustand(self, z): return {"dr_mm": 5.0}
        async def speichere_bilanz_zustand(self, zone_id, ts, schritt, **k):
            self.dr = schritt.bewaesserung_mm

    class _Konfig:
        bilanz = None

    sp = _Sp()
    job = WasserbilanzJob(sp, [_Zone("magerwiese", 40.0)], _Konfig())
    await job.aktualisiere_wenn_faellig(datetime(2026, 7, 23, 6, 0))
    assert sp.dr == 0.0, "ignoriert darf nicht als Wasser zaehlen"


@pytest.mark.asyncio
async def test_bilanz_zustand_rundreise_mit_ensemble_feldern(tmp_path):
    """Regression: neue Spalte im INSERT ohne Migrationseintrag.

    Am 05.08.2026 kannte `speichere_bilanz_zustand` die drei Ensemble-Spalten,
    die Tabelle aber nicht -- der Migrationseintrag war beim Edit verloren
    gegangen. Ergebnis: der Shadow-Job brach nach dem Deploy fuer 8 Zonen.
    Kein Wasser betroffen (Shadow entscheidet nichts), aber die Reihe hatte
    ein Loch, und gemerkt hat es nur die Nachkontrolle im Log.

    Die Stub-Tests konnten das nicht fangen -- sie faelschen den Speicher.
    Dieser Test geht ueber eine ECHTE Datenbank und damit ueber die Migration.
    Gleiche Klasse wie `fehlerpattern_config_whitelist`: ein Feld, das eine
    Ebene kennt und die andere nicht.
    """
    from datetime import datetime

    from bewaesserung.speicher import Speicher
    from bewaesserung.wasserbilanz import schreibe_fort, speicher_fuer_zone

    sp = Speicher(str(tmp_path / "bilanz.db"))
    await sp.verbinden()
    try:
        boden = speicher_fuer_zone()
        schritt = schreibe_fort(6.0, 4.5, 0.0, 0.0, boden)
        await sp.speichere_bilanz_zustand(
            "bambuswald", datetime(2026, 7, 23), schritt,
            wuerde_giessen=schritt.giessen, ist_entscheidung=False,
            quelle="shadow",
            wuerde_giessen_ensemble=True,
            regen_p20_mm=1.2,
            et0_prognose_24h_mm=4.5,
        )
        zeile = await sp.hole_letzten_bilanz_zustand("bambuswald")
        assert zeile["wuerde_giessen_ensemble"] == 1
        assert zeile["regen_p20_mm"] == pytest.approx(1.2)
        assert zeile["et0_prognose_24h_mm"] == pytest.approx(4.5)
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_t0563_zweiter_lauf_am_selben_tag_schreibt_nicht_fort():
    """Neustart darf denselben Bilanztag nicht ein zweites Mal auszehren.

    `_letzter_lauf` ist reiner Prozess-Zustand, nach jedem Neustart also
    leer. Weil die Zeile auf `bis` geschrieben wird und
    `hole_letzten_bilanz_zustand` die JUENGSTE liefert, las der zweite Lauf
    seine eigene Ausgabe als Vorzustand -- `INSERT OR REPLACE` ueberschrieb
    dieselbe Zeile mit einem weiteren ET0-Tag. Dr waechst, ohne dass eine
    Zeile dazukommt, und `wuerde_giessen` kippt allein durch Neustarts.
    """
    from datetime import datetime

    from bewaesserung.wasserbilanz_job import WasserbilanzJob

    class _Sp:
        def __init__(self):
            self.eintraege = []

        async def hole_forecast_stunden(self, standort, von, bis):
            return {von: (0.0, 6.0)}

        async def hole_ventil_ereignisse(self, zone_id, von=None, bis=None):
            return []

        async def hole_letzten_bilanz_zustand(self, zone_id):
            if not self.eintraege:
                return None
            letzter = self.eintraege[-1]
            return {"dr_mm": letzter["dr"], "zeitstempel": letzter["ts"]}

        async def hole_regen_ensemble(self, standort_id=None, limit=200):
            return []

        async def speichere_bilanz_zustand(self, zone_id, ts, schritt,
                                           wuerde_giessen, ist_entscheidung,
                                           quelle="",
                                           wuerde_giessen_ensemble=None,
                                           regen_p20_mm=None,
                                           et0_prognose_24h_mm=None):
            self.eintraege.append({
                "zone": zone_id, "dr": schritt.dr_nachher_mm,
                "ts": ts.isoformat(),
            })

    class _Konfig:
        bilanz = None

    sp = _Sp()
    zone = _Zone("bambuswald", 1.74, 25)
    jetzt = datetime(2026, 7, 23, 6, 0)

    erster = WasserbilanzJob(sp, [zone], _Konfig())
    assert await erster.aktualisiere_wenn_faellig(jetzt) == 1
    dr_nach_erstem = sp.eintraege[-1]["dr"]

    # Neustart: frischer Job, leerer Prozess-Anker, gleicher Tag.
    for runde in range(3):
        zweiter = WasserbilanzJob(sp, [zone], _Konfig())
        assert await zweiter.aktualisiere_wenn_faellig(jetzt) == 0, (
            f"Neustart {runde + 1} hat den Tag erneut fortgeschrieben"
        )

    assert len(sp.eintraege) == 1
    assert sp.eintraege[-1]["dr"] == dr_nach_erstem, (
        "Dr darf durch Neustarts nicht wachsen"
    )


@pytest.mark.asyncio
async def test_t0563_naechster_tag_wird_normal_fortgeschrieben():
    """Gegenprobe: die Idempotenz darf den Job nicht stilllegen."""
    from datetime import datetime, timedelta

    from bewaesserung.wasserbilanz_job import WasserbilanzJob

    class _Sp:
        def __init__(self):
            self.eintraege = []

        async def hole_forecast_stunden(self, standort, von, bis):
            return {von: (0.0, 3.0)}

        async def hole_ventil_ereignisse(self, zone_id, von=None, bis=None):
            return []

        async def hole_letzten_bilanz_zustand(self, zone_id):
            if not self.eintraege:
                return None
            letzter = self.eintraege[-1]
            return {"dr_mm": letzter["dr"], "zeitstempel": letzter["ts"]}

        async def hole_regen_ensemble(self, standort_id=None, limit=200):
            return []

        async def speichere_bilanz_zustand(self, zone_id, ts, schritt,
                                           wuerde_giessen, ist_entscheidung,
                                           quelle="",
                                           wuerde_giessen_ensemble=None,
                                           regen_p20_mm=None,
                                           et0_prognose_24h_mm=None):
            self.eintraege.append({
                "zone": zone_id, "dr": schritt.dr_nachher_mm,
                "ts": ts.isoformat(),
            })

    class _Konfig:
        bilanz = None

    sp = _Sp()
    zone = _Zone("bambuswald", 1.74, 25)
    tag1 = datetime(2026, 7, 23, 6, 0)

    assert await WasserbilanzJob(sp, [zone], _Konfig()).aktualisiere_wenn_faellig(tag1) == 1
    assert await WasserbilanzJob(sp, [zone], _Konfig()).aktualisiere_wenn_faellig(
        tag1 + timedelta(days=1),
    ) == 1
    assert len(sp.eintraege) == 2
    assert sp.eintraege[1]["dr"] > sp.eintraege[0]["dr"], (
        "am Folgetag muss weiter ausgezehrt werden"
    )
