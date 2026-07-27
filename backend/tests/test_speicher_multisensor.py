"""T-0179c: Tests fuer Multi-Sensor-Aggregation pro Zone.

Deckt `Speicher.letzte_messung_aggregiert` und
`Speicher.letzte_messungen_pro_geraet` ab.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import DatenQuelle, SensorMessung
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "multi.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _m(
    sp: Speicher, zone: str, geraet: str, ts: datetime, feuchte: float,
    quelle: DatenQuelle = DatenQuelle.GARDENA,
):
    _run(sp.speichere_messung(SensorMessung(
        zeitstempel=ts, zone_id=zone, geraet_id=geraet,
        boden_feuchte=feuchte, boden_temperatur=15.0,
        batterie_prozent=90.0, quelle=quelle,
    )))


# --- letzte_messung_aggregiert -------------------------------------------


def test_aggregat_bei_einem_sensor_ist_identisch_zu_legacy(speicher):
    """Bei genau einem Sensor pro Zone liefert das Aggregat denselben Wert."""
    jetzt = datetime(2026, 5, 12, 12, 0)
    _m(speicher, "zone_a", "sensor_1", jetzt - timedelta(minutes=30), 55.0)
    agg = _run(speicher.letzte_messung_aggregiert("zone_a", jetzt=jetzt))
    legacy = _run(speicher.letzte_messung("zone_a"))
    assert agg is not None and legacy is not None
    assert agg.boden_feuchte == legacy.boden_feuchte == 55.0
    assert agg.geraet_id == legacy.geraet_id == "sensor_1"


def test_aggregat_drei_sensoren_median(speicher):
    """Drei Sensoren in derselben Zone -> Median der Feuchte-Werte."""
    jetzt = datetime(2026, 5, 12, 12, 0)
    _m(speicher, "wb", "gardena", jetzt - timedelta(minutes=10), 50.0)
    _m(speicher, "wb", "fyta_1", jetzt - timedelta(minutes=20), 30.0)
    _m(speicher, "wb", "fyta_2", jetzt - timedelta(minutes=5), 40.0)
    agg = _run(speicher.letzte_messung_aggregiert("wb", jetzt=jetzt))
    assert agg is not None
    # Median(30, 40, 50) = 40
    assert agg.boden_feuchte == 40.0
    assert agg.geraet_id == "aggregat:3"
    # Zeitstempel = neuester (fyta_2 vor 5 min)
    assert agg.zeitstempel == jetzt - timedelta(minutes=5)


def test_aggregat_lead_liest_nur_lead_sensor(speicher):
    """T-0332: Mit gesetztem Lead nutzt die Zone NUR den Lead-Sensor statt
    Median (Cross-Spray-Regime: FYTA hoch, Gardena niedrig = Wahrheit)."""
    jetzt = datetime(2026, 6, 25, 12, 0)
    _m(speicher, "hecke", "gardena", jetzt - timedelta(minutes=10), 30.0)
    _m(speicher, "hecke", "fyta_1", jetzt - timedelta(minutes=8), 84.0)
    _m(speicher, "hecke", "fyta_2", jetzt - timedelta(minutes=5), 69.0)
    # Ohne Lead: Median(30, 69, 84) = 69 -> ueber-liest die echte Feuchte
    ohne = _run(speicher.letzte_messung_aggregiert("hecke", jetzt=jetzt))
    assert ohne.boden_feuchte == 69.0
    # Mit Lead auf gardena: nur 30 (Single-Sensor-Pfad, echte geraet_id)
    speicher.setze_aggregat_lead({"hecke": "gardena"})
    mit = _run(speicher.letzte_messung_aggregiert("hecke", jetzt=jetzt))
    assert mit.boden_feuchte == 30.0
    assert mit.geraet_id == "gardena"


def test_t0384_aggregat_lead_fehlt_im_fenster_gibt_keine_messung(speicher):
    """T-0384 (ersetzt den frueheren "faellt auf Median"-Vertrag): Lead
    konfiguriert, aber ohne Wert im Fenster -> KEINE Messung, NICHT der Median
    der uebrigen Sensoren. Genau dieser Median (hier 77) ist bei der Hecke die
    cross-spray-inflationierte Groesse, gegen die der Gardena-Lead (~45) gesetzt
    wurde -- ein Lead-Dropout haette die scharfe KORRIDOR-Hecke still
    unter-waessert. Der Entscheidungspfad erweitert stattdessen bewusst +
    geloggt das Fenster (T-0383), die Sensor-Identitaet wechselt nie still."""
    jetzt = datetime(2026, 6, 25, 12, 0)
    _m(speicher, "hecke", "fyta_1", jetzt - timedelta(minutes=8), 84.0)
    _m(speicher, "hecke", "fyta_2", jetzt - timedelta(minutes=5), 70.0)
    speicher.setze_aggregat_lead({"hecke": "gardena"})  # gardena fehlt
    agg = _run(speicher.letzte_messung_aggregiert("hecke", jetzt=jetzt))
    assert agg is None


def test_t0384_aggregat_lead_fehlt_bulk_isomorph_gibt_none(speicher):
    """T-0384: Die Bulk-Variante (UI/Snapshot) muss identisch reagieren --
    sonst Decision-vs-UI-Drift (UI zeigt den inflationierten Median als
    'aktuellen' Wert, waehrend die Engine keine Messung sieht)."""
    jetzt = datetime(2026, 6, 25, 12, 0)
    _m(speicher, "hecke", "fyta_1", jetzt - timedelta(minutes=8), 84.0)
    _m(speicher, "hecke", "fyta_2", jetzt - timedelta(minutes=5), 70.0)
    speicher.setze_aggregat_lead({"hecke": "gardena"})  # gardena fehlt
    bulk = _run(speicher.letzte_messung_aggregiert_bulk(["hecke"], jetzt=jetzt))
    assert bulk["hecke"] is None


def test_t0384_aggregat_lead_im_laengeren_fenster_wieder_da(speicher):
    """T-0384/T-0383: Der Lead ist nur AUS DEM FENSTER gefallen, nicht weg.
    Mit dem groesseren Fallback-Fenster (Entscheidungspfad, 240 min) liefert
    dieselbe Zone wieder den LEAD-Wert -- nicht den Nachbar-Median."""
    jetzt = datetime(2026, 6, 25, 12, 0)
    _m(speicher, "hecke", "gardena", jetzt - timedelta(minutes=100), 45.0)
    _m(speicher, "hecke", "fyta_1", jetzt - timedelta(minutes=8), 84.0)
    _m(speicher, "hecke", "fyta_2", jetzt - timedelta(minutes=5), 70.0)
    speicher.setze_aggregat_lead({"hecke": "gardena"})
    eng = _run(speicher.letzte_messung_aggregiert("hecke", jetzt=jetzt))
    assert eng is None  # 90-min-Fenster: Lead raus
    weit = _run(speicher.letzte_messung_aggregiert(
        "hecke", fenster_minuten=240, jetzt=jetzt,
    ))
    assert weit.boden_feuchte == 45.0  # Lead, nicht Median(84,70)=77
    assert weit.geraet_id == "gardena"


def test_t0386_hole_feuchte_werte_geraet_scoped_schneidet_nur_diesen_sensor(speicher):
    """T-0386: Ein ml_ausschluss_fenster MIT geraet_id schneidet nur die Rows
    DIESES Sensors im Fenster weg -- der gesunde Nachbarsensor bleibt drin. Das
    ist der Zweck des T-0267-Feldes, den der frueher zone-weite Filter ignorierte
    (Realfall: FYTA-Einbett/Skalenbruch, Gardena bleibt im Training)."""
    jetzt = datetime(2026, 6, 25, 12, 0)
    _m(speicher, "hecke", "gardena", jetzt - timedelta(minutes=60), 30.0)
    _m(speicher, "hecke", "gardena", jetzt - timedelta(minutes=10), 32.0)
    _m(speicher, "hecke", "fyta_1", jetzt - timedelta(minutes=60), 80.0)  # im Fenster
    _m(speicher, "hecke", "fyta_1", jetzt - timedelta(minutes=10), 82.0)  # ausserhalb
    fenster = [(jetzt - timedelta(minutes=70), jetzt - timedelta(minutes=30), "fyta_1")]
    werte = _run(speicher.hole_feuchte_werte(
        "hecke", jetzt - timedelta(minutes=120), jetzt, ausschluss_fenster=fenster,
    ))
    # fyta_1@-60 (80) faellt raus; alle Gardena + fyta_1@-10 bleiben.
    assert sorted(werte) == [30.0, 32.0, 82.0]


def test_t0386_hole_feuchte_werte_zone_weit_schneidet_alle_sensoren(speicher):
    """T-0386: Ein Fenster OHNE geraet_id (None) wirkt wie bisher zone-weit --
    schneidet alle Sensoren im Fenster weg (Backward-Compat)."""
    jetzt = datetime(2026, 6, 25, 12, 0)
    _m(speicher, "hecke", "gardena", jetzt - timedelta(minutes=60), 30.0)  # im Fenster
    _m(speicher, "hecke", "gardena", jetzt - timedelta(minutes=10), 32.0)
    _m(speicher, "hecke", "fyta_1", jetzt - timedelta(minutes=60), 80.0)   # im Fenster
    _m(speicher, "hecke", "fyta_1", jetzt - timedelta(minutes=10), 82.0)
    fenster = [(jetzt - timedelta(minutes=70), jetzt - timedelta(minutes=30), None)]
    werte = _run(speicher.hole_feuchte_werte(
        "hecke", jetzt - timedelta(minutes=120), jetzt, ausschluss_fenster=fenster,
    ))
    # Beide -60-Rows raus, beide -10-Rows bleiben.
    assert sorted(werte) == [32.0, 82.0]


def test_aggregat_lead_bulk_isomorph_zur_per_zone(speicher):
    """T-0332: Die Bulk-Variante wendet denselben Lead an (kein Decision-
    vs-UI-Drift)."""
    jetzt = datetime(2026, 6, 25, 12, 0)
    _m(speicher, "hecke", "gardena", jetzt - timedelta(minutes=10), 30.0)
    _m(speicher, "hecke", "fyta_1", jetzt - timedelta(minutes=5), 84.0)
    speicher.setze_aggregat_lead({"hecke": "gardena"})
    bulk = _run(speicher.letzte_messung_aggregiert_bulk(["hecke"], jetzt=jetzt))
    pz = _run(speicher.letzte_messung_aggregiert("hecke", jetzt=jetzt))
    assert bulk["hecke"].boden_feuchte == 30.0
    assert pz.boden_feuchte == bulk["hecke"].boden_feuchte


def test_aggregat_zwei_sensoren_median_gemittelt(speicher):
    """Zwei Sensoren: Median = Durchschnitt der zwei Werte (statistics.median)."""
    jetzt = datetime(2026, 5, 12, 12, 0)
    _m(speicher, "wb", "fyta_a", jetzt - timedelta(minutes=10), 30.0)
    _m(speicher, "wb", "fyta_b", jetzt - timedelta(minutes=5), 50.0)
    agg = _run(speicher.letzte_messung_aggregiert("wb", jetzt=jetzt))
    assert agg.boden_feuchte == 40.0
    assert agg.geraet_id == "aggregat:2"


def test_aggregat_pro_geraet_nimmt_juengsten_wert(speicher):
    """Pro `geraet_id` nimmt das Aggregat den juengsten Wert,
    nicht den Mittelwert ueber alle Geraet-Messungen.
    """
    jetzt = datetime(2026, 5, 12, 12, 0)
    # gardena hat 3 Messungen, der juengste zaehlt
    _m(speicher, "wb", "gardena", jetzt - timedelta(minutes=60), 30.0)
    _m(speicher, "wb", "gardena", jetzt - timedelta(minutes=30), 40.0)
    _m(speicher, "wb", "gardena", jetzt - timedelta(minutes=5), 50.0)
    _m(speicher, "wb", "fyta", jetzt - timedelta(minutes=10), 70.0)
    agg = _run(speicher.letzte_messung_aggregiert("wb", jetzt=jetzt))
    # Pro Geraet: gardena=50 (juengster), fyta=70
    # Median(50, 70) = 60
    assert agg.boden_feuchte == 60.0


def test_aggregat_fenster_filtert_alte_messungen(speicher):
    """Messungen aelter als `fenster_minuten` zaehlen nicht."""
    jetzt = datetime(2026, 5, 12, 12, 0)
    _m(speicher, "wb", "alt", jetzt - timedelta(hours=3), 99.0)
    _m(speicher, "wb", "neu", jetzt - timedelta(minutes=10), 40.0)
    agg = _run(speicher.letzte_messung_aggregiert(
        "wb", fenster_minuten=90, jetzt=jetzt,
    ))
    # alt-Sensor ist 180 min zurueck, faellt aus 90-min-Fenster
    assert agg.boden_feuchte == 40.0
    assert agg.geraet_id == "neu"


def test_aggregat_leer_wenn_keine_messung_im_fenster(speicher):
    jetzt = datetime(2026, 5, 12, 12, 0)
    _m(speicher, "wb", "alt", jetzt - timedelta(hours=5), 50.0)
    agg = _run(speicher.letzte_messung_aggregiert(
        "wb", fenster_minuten=90, jetzt=jetzt,
    ))
    assert agg is None


def test_aggregat_ignoriert_andere_zone(speicher):
    """Sensoren in anderer Zone werden nicht beruecksichtigt."""
    jetzt = datetime(2026, 5, 12, 12, 0)
    _m(speicher, "wb", "fyta_a", jetzt - timedelta(minutes=10), 50.0)
    _m(speicher, "bambus", "gardena", jetzt - timedelta(minutes=5), 80.0)
    agg = _run(speicher.letzte_messung_aggregiert("wb", jetzt=jetzt))
    assert agg.boden_feuchte == 50.0


# --- letzte_messungen_pro_geraet -----------------------------------------


def test_pro_geraet_liefert_alle_aktiven_sensoren(speicher):
    jetzt = datetime(2026, 5, 12, 12, 0)
    _m(speicher, "wb", "gardena", jetzt - timedelta(minutes=10), 50.0)
    _m(speicher, "wb", "fyta_a", jetzt - timedelta(minutes=20), 30.0)
    _m(speicher, "wb", "fyta_b", jetzt - timedelta(minutes=5), 40.0)
    liste = _run(speicher.letzte_messungen_pro_geraet("wb", jetzt=jetzt))
    assert len(liste) == 3
    nach_id = {m.geraet_id: m.boden_feuchte for m in liste}
    assert nach_id == {"gardena": 50.0, "fyta_a": 30.0, "fyta_b": 40.0}


def test_pro_geraet_je_geraet_juengster_wert(speicher):
    jetzt = datetime(2026, 5, 12, 12, 0)
    _m(speicher, "wb", "gardena", jetzt - timedelta(minutes=60), 30.0)
    _m(speicher, "wb", "gardena", jetzt - timedelta(minutes=5), 55.0)
    liste = _run(speicher.letzte_messungen_pro_geraet("wb", jetzt=jetzt))
    assert len(liste) == 1
    assert liste[0].boden_feuchte == 55.0


def test_pro_geraet_leer_ohne_messungen(speicher):
    liste = _run(speicher.letzte_messungen_pro_geraet(
        "leer", jetzt=datetime(2026, 5, 12, 12, 0),
    ))
    assert liste == []


def test_t0215_default_fenster_360min_zeigt_fyta_mit_3h_cadence(speicher):
    """T-0215: FYTA-Beam reportet alle 3-4 h. Default-Fenster 360 min
    (6 h) muss FYTA-Sensoren mit Wert vor ~3 h noch zeigen.
    Realfall waldblumenhain 19.05. 20:00: fyta_100004 letzten Wert
    17:21 (2.8 h alt). Mit altem 90-min-Fenster fiel der raus.
    """
    jetzt = datetime(2026, 5, 19, 20, 0)
    _m(speicher, "wb", "gardena", jetzt - timedelta(minutes=30), 60.0)
    _m(speicher, "wb", "fyta_a", jetzt - timedelta(minutes=20), 49.0,
       quelle=DatenQuelle.FYTA)
    # FYTA-B mit 168 min Alter (= 2 h 48 min) -- innerhalb 6h-Default,
    # ausserhalb 90-min.
    _m(speicher, "wb", "fyta_b", jetzt - timedelta(minutes=168), 52.0,
       quelle=DatenQuelle.FYTA)
    liste = _run(speicher.letzte_messungen_pro_geraet("wb", jetzt=jetzt))
    nach_id = {m.geraet_id: m.boden_feuchte for m in liste}
    assert nach_id == {"gardena": 60.0, "fyta_a": 49.0, "fyta_b": 52.0}, (
        "Default-Fenster 6h muss alle 3 Sensoren zeigen, auch FYTA mit "
        "3h-Cadence"
    )


def test_t0215_default_fenster_zeigt_keinen_stale_ueber_6h(speicher):
    """Negativ-Kontrolle: 7 h alter Sensor faellt aus dem 6h-Default-
    Fenster raus. AUSFALL-Warnung greift ohnehin separat ab 12 h."""
    jetzt = datetime(2026, 5, 19, 20, 0)
    _m(speicher, "wb", "fresh", jetzt - timedelta(minutes=30), 60.0)
    _m(speicher, "wb", "stale", jetzt - timedelta(hours=7), 40.0)
    liste = _run(speicher.letzte_messungen_pro_geraet("wb", jetzt=jetzt))
    nach_id = {m.geraet_id for m in liste}
    assert nach_id == {"fresh"}, "7h alter Sensor faellt aus 6h-Fenster"


# --- T-0186 Dedup gegen Cloud-WebSocket-Echo ------------------------------


def test_t0186_dedup_doppel_insert_innerhalb_2s(speicher):
    """T-0186: Bei zwei nahezu gleichzeitigen Messungen fuer selben
    (zone_id, geraet_id) -> zweiter Insert wird skipt.
    Realfall 12.05.2026: Waldblumen 09:37:35 zwei Werte 45 + 50 binnen
    108 ms -> Heuristik wertete als Sprung -> Phantom-Event.
    """
    t = datetime(2026, 5, 12, 9, 37, 35, 233_182)
    _m(speicher, "waldblumenhain", "gardena-uuid", t, 45.0)
    # Zweiter Insert 108 ms spaeter mit anderem Wert
    t2 = datetime(2026, 5, 12, 9, 37, 35, 341_435)
    _m(speicher, "waldblumenhain", "gardena-uuid", t2, 50.0)

    # Nur erster Eintrag bleibt in der DB
    pro = _run(speicher.letzte_messungen_pro_geraet(
        "waldblumenhain", jetzt=t + timedelta(hours=1),
    ))
    assert len(pro) == 1
    assert pro[0].boden_feuchte == 45.0


def test_t0186_dedup_erlaubt_unterschiedliche_geraete(speicher):
    """T-0186: Doppelter Zeitstempel-Cluster ist nur Dedup fuer GLEICHE
    geraet_id. Verschiedene Sensoren in derselben Zone duerfen
    gleichzeitig schreiben.
    """
    t = datetime(2026, 5, 12, 9, 37, 35, 233_182)
    _m(speicher, "waldblumenhain", "gardena", t, 45.0)
    t2 = datetime(2026, 5, 12, 9, 37, 35, 341_435)
    _m(speicher, "waldblumenhain", "fyta", t2, 30.0,
       quelle=DatenQuelle.FYTA)

    pro = _run(speicher.letzte_messungen_pro_geraet(
        "waldblumenhain", jetzt=t + timedelta(hours=1),
    ))
    assert len(pro) == 2


def test_t0186_dedup_normaler_polling_abstand_ok(speicher):
    """T-0186: regulaerer Sensor-Polling (~1×/h fuer Gardena) bleibt
    unangetastet — nur Echo-Doppelungen binnen 2 Sekunden werden
    geblockt.
    """
    t1 = datetime(2026, 5, 12, 9, 0, 0)
    t2 = datetime(2026, 5, 12, 10, 0, 0)
    _m(speicher, "waldblumenhain", "gardena", t1, 45.0)
    _m(speicher, "waldblumenhain", "gardena", t2, 50.0)

    pro = _run(speicher.letzte_messungen_pro_geraet(
        "waldblumenhain", jetzt=t2 + timedelta(hours=1),
        fenster_minuten=180,
    ))
    assert len(pro) == 1  # pro Geraet juengster Wert
    assert pro[0].boden_feuchte == 50.0  # neuerer Wert sichtbar


def test_t0186_dedup_ohne_geraet_id_kein_filter(speicher):
    """T-0186: ohne geraet_id (= leerer String) wird nicht dedupliziert,
    weil nicht eindeutig zuordenbar (legacy / Test-Daten).
    """
    t1 = datetime(2026, 5, 12, 9, 37, 35, 0)
    t2 = datetime(2026, 5, 12, 9, 37, 35, 500_000)
    _m(speicher, "waldblumenhain", "", t1, 45.0)
    _m(speicher, "waldblumenhain", "", t2, 50.0)
    cur = _run(speicher.hole_messungen(
        "waldblumenhain", von=t1 - timedelta(seconds=1),
        bis=t2 + timedelta(seconds=1),
    ))
    assert len(cur) == 2


# --- letzte_messung_geraet (T-0224) --------------------------------------


def test_t0224_letzte_messung_geraet_pro_sensor_nicht_pro_zone(speicher):
    """T-0224: `letzte_messung_geraet` liefert den juengsten Wert fuer
    EIN Geraet -- auch wenn ein anderer Sensor derselben Zone neuere
    Werte hat. Gegenprobe: `letzte_messung` (pro zone_id) gaebe den
    fremden, neueren Sensor zurueck. Genau diese Verwechslung liess
    den FYTA-Dedup-Init den langsameren Sensor aushungern."""
    jetzt = datetime(2026, 5, 22, 6, 0)
    _m(speicher, "waldblumenhain", "fyta_100003",
       jetzt - timedelta(minutes=10), 51.0, DatenQuelle.FYTA)
    _m(speicher, "waldblumenhain", "fyta_100004",
       jetzt - timedelta(hours=9), 46.0, DatenQuelle.FYTA)
    _m(speicher, "waldblumenhain", "fyta_100004",
       jetzt - timedelta(hours=8), 47.0, DatenQuelle.FYTA)

    d_rand = _run(speicher.letzte_messung_geraet("fyta_100004"))
    assert d_rand is not None
    assert d_rand.geraet_id == "fyta_100004"
    assert d_rand.boden_feuchte == 47.0
    assert d_rand.zeitstempel == jetzt - timedelta(hours=8)
    # Zone-weite Abfrage gaebe den anderen, neueren Sensor.
    zone = _run(speicher.letzte_messung("waldblumenhain"))
    assert zone is not None and zone.geraet_id == "fyta_100003"


def test_t0224_letzte_messung_geraet_unbekannt_liefert_none(speicher):
    """T-0224: Unbekannte geraet_id -> None statt Crash."""
    assert _run(speicher.letzte_messung_geraet("fyta_999999")) is None


# --- T-0279 Phase 2: min_gemappte_feuchte ----------------------------------


def test_t0279_min_gemappte_feuchte_ohne_mapping(speicher):
    """Ohne Skalen-Mapping: Minimum der Roh-Werte."""
    jetzt = datetime(2026, 5, 31, 9, 0)
    _m(speicher, "wb", "gardena", jetzt - timedelta(minutes=10), 30.0,
       DatenQuelle.GARDENA)
    _m(speicher, "wb", "fyta_a", jetzt - timedelta(minutes=5), 52.0,
       DatenQuelle.FYTA)
    _m(speicher, "wb", "fyta_b", jetzt - timedelta(minutes=8), 47.0,
       DatenQuelle.FYTA)
    out = _run(speicher.min_gemappte_feuchte("wb", jetzt=jetzt))
    assert out == 30.0  # Gardena ist der trockenste


def test_t0279_min_gemappte_feuchte_mit_mapping(speicher):
    """Mit FYTA->Gardena-Mapping: FYTA-Rohwerte runter-transformiert,
    dann Minimum. Realfall-Nachbau waldblumen 30.05."""
    jetzt = datetime(2026, 5, 31, 9, 0)
    # Mapping feuchte_gemappt = 1.0*roh - 10 fuer fyta.
    _run(speicher.upsert_skalen_mapping(
        zone_id="wb", quelle="fyta", a=1.0, b=-10.0, n_obs=100,
    ))
    _m(speicher, "wb", "gardena", jetzt - timedelta(minutes=10), 30.0,
       DatenQuelle.GARDENA)
    _m(speicher, "wb", "fyta_a", jetzt - timedelta(minutes=5), 52.0,
       DatenQuelle.FYTA)  # gemappt: 42
    _m(speicher, "wb", "fyta_b", jetzt - timedelta(minutes=8), 47.0,
       DatenQuelle.FYTA)  # gemappt: 37
    out = _run(speicher.min_gemappte_feuchte("wb", jetzt=jetzt))
    # min(30, 42, 37) = 30 (Gardena bleibt trockenster, unter Welkepunkt!)
    assert out == 30.0


def test_t0279_min_gemappte_feuchte_keine_messung(speicher):
    """Keine Messung im Fenster -> None."""
    jetzt = datetime(2026, 5, 31, 9, 0)
    assert _run(speicher.min_gemappte_feuchte("leer", jetzt=jetzt)) is None
