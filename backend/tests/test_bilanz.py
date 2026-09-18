import asyncio
from datetime import datetime

import pytest

from bewaesserung.bilanz import berechne_bilanz, ereignis_zu_liter
from bewaesserung.modelle import (
    Ausloser,
    BilanzKonfig,
    VentilAktion,
    VentilEreignis,
    WetterArchivStunde,
    WetterStunde,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "bilanz.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _zone(**kwargs) -> ZonenKonfig:
    defaults = dict(
        zone_id="bambuswald", name="Bambuswald",
        modus=ZonenModus.AUTOMATIK, ventil_kanal=2,
        feuchte_schwelle_min=65.0, feuchte_kritisch=50.0,
        flaeche_m2=1.74, anteil_kanal=0.357,
    )
    defaults.update(kwargs)
    return ZonenKonfig(**defaults)


def test_f19_aquabloom_bulk_geflippt_bekommt_liter_aus_konfig():
    """F19: Ein UI-bulk-geflipptes AquaBloom-SCHLIESSEN (ausloser=aquabloom,
    KEIN e.liter -- der Banner setzt nur den ausloser) muss in der Bilanz die
    fixe Tropfer-Dosis aus der Zonen-Konfig bekommen, nicht 'indikativ' (None).
    Pump-Zonen haben keine Kanal-Rate -> ohne Fix fiel das Event durch zu None.
    600s/3600 * 2 Tropfer * 2.0 L/h = 0.667 L."""
    zone = _zone(
        zone_id="zitrus", name="Zitrus", ventil_kanal=None,
        aquabloom_pumpen_dauer_sekunden=600,
        aquabloom_tropfer_anzahl=2,
        aquabloom_tropfer_liter_pro_stunde=2.0,
    )
    # Bulk-geflippt: ausloser=aquabloom, aber kein liter, dauer = Heuristik-Dauer.
    event = VentilEreignis(
        zeitstempel=datetime(2026, 6, 1, 8, 0),
        zone_id="zitrus", ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=180,
        ausloser=Ausloser.AQUABLOOM, liter=None,
    )
    assert ereignis_zu_liter(event, zone, BilanzKonfig()) == pytest.approx(0.667, abs=0.01)

    # Gegenprobe: Job-konvertiertes Event (e.liter gesetzt) dominiert weiter.
    event_mit_liter = VentilEreignis(
        zeitstempel=datetime(2026, 6, 1, 8, 0),
        zone_id="zitrus", ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
        ausloser=Ausloser.AQUABLOOM, liter=0.667,
    )
    assert ereignis_zu_liter(event_mit_liter, zone, BilanzKonfig()) == 0.667


@pytest.mark.parametrize(
    "ausloser", [Ausloser.IGNORIERT, Ausloser.FREMDWASSER, Ausloser.UNBEKANNT],
)
def test_t0487_kein_wasser_ausloeser_schlaegt_expliziten_literwert(ausloser):
    """T-0487 (Audit A9): der Ausschluss stand hinter dem `e.liter`-Return.

    Ein Ereignis, das spaeter als Phantom (IGNORIERT), als Cross-Spray einer
    Nachbarzone (FREMDWASSER) oder als unklassifiziert (UNBEKANNT) markiert
    wird, behaelt seinen Literwert aus der Zeit davor. Mit der alten
    Reihenfolge wurde es damit trotzdem als Zonenwasser bilanziert, entgegen
    dem zentralen Vertrag von `KEINE_WASSER_AUSLOESER`.

    Latent, nicht materialisiert: am 02.08. gab es 0 DB-Zeilen mit einem
    dieser drei Ausloeser und `liter IS NOT NULL`. Genau deshalb der Test --
    der Fall entsteht erst bei der naechsten Umklassifizierung.
    """
    zone = _zone()
    event = VentilEreignis(
        zeitstempel=datetime(2026, 8, 1, 8, 0),
        zone_id=zone.zone_id, ventil_id="manuell",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1800,
        ausloser=ausloser, liter=12.5,
    )

    assert ereignis_zu_liter(event, zone, BilanzKonfig()) is None


def test_t0487_echtes_wasser_behaelt_seinen_literwert():
    """Gegenprobe: der Ausschluss darf nur die drei Ausloeser treffen."""
    zone = _zone()
    event = VentilEreignis(
        zeitstempel=datetime(2026, 8, 1, 8, 0),
        zone_id=zone.zone_id, ventil_id="manuell",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1800,
        ausloser=Ausloser.MANUELL, liter=12.5,
    )

    assert ereignis_zu_liter(event, zone, BilanzKonfig()) == 12.5


def _konfig() -> BilanzKonfig:
    return BilanzKonfig(
        kanal_liter_pro_minute={1: 6.0, 2: 1.87},
        manuell_liter_pro_minute=10.0,
    )


def test_bilanz_ohne_flaeche_gibt_none(speicher):
    zone = _zone(flaeche_m2=None)
    ergebnis = _run(berechne_bilanz(
        zone, datetime(2026, 4, 10), datetime(2026, 4, 17),
        speicher, _konfig(), "standort_a",
    ))
    assert ergebnis is None


def test_bilanz_summiert_bewaesserung_nach_kanal_anteil(speicher):
    # Bambuswald (Anteil 35.7% an Kanal 2 mit 1.87 L/min)
    # 600 s Bewaesserung -> 10 min x 1.87 x 0.357 = 6.68 L
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=datetime(2026, 4, 15, 19, 0),
        zone_id="bambuswald", ventil_id="v1",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
        ausloser=Ausloser.AUTOMATIK,
    )))

    bilanz = _run(berechne_bilanz(
        _zone(), datetime(2026, 4, 15), datetime(2026, 4, 17),
        speicher, _konfig(), "standort_a",
    ))
    assert bilanz is not None
    assert bilanz.bewaesserung_liter == pytest.approx(6.7, abs=0.1)
    assert bilanz.quelle_niederschlag == "keine"


def test_bilanz_nutzt_geraet_kanal_rate_vor_legacy_kanalrate(speicher):
    """Multi-DSWC: gleicher Kanal kann je Geraet anderen Durchfluss haben."""
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=datetime(2026, 4, 15, 19, 0),
        zone_id="dswc2_zone", ventil_id="ventil-dswc-2:1",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
        ausloser=Ausloser.AUTOMATIK,
    )))
    zone = _zone(
        zone_id="dswc2_zone",
        ventil_kanal=1,
        ventil_geraet_id="dswc-2",
        anteil_kanal=1.0,
    )
    konfig = BilanzKonfig(
        kanal_liter_pro_minute={1: 6.0},
        geraet_kanal_liter_pro_minute={"dswc-2": {1: 1.4}},
    )

    bilanz = _run(berechne_bilanz(
        zone, datetime(2026, 4, 15), datetime(2026, 4, 17),
        speicher, konfig, "standort_a",
    ))

    assert bilanz is not None
    assert bilanz.bewaesserung_liter == pytest.approx(14.0)


def test_manueller_schlauch_mit_liter_override_dominiert(speicher):
    # Manuell 100 L angegeben - dauer_sekunden wird ignoriert
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=datetime(2026, 4, 17, 18),
        zone_id="waldblumenhain", ventil_id="manuell",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=600,
        ausloser=Ausloser.MANUELL, liter=100.0,
    )))

    zone = _zone(zone_id="waldblumenhain", ventil_kanal=1,
                 flaeche_m2=40.0, anteil_kanal=1.0)
    bilanz = _run(berechne_bilanz(
        zone, datetime(2026, 4, 17, 0), datetime(2026, 4, 17, 23),
        speicher, _konfig(), "standort_a",
    ))
    assert bilanz is not None
    # 100 L direkt, nicht 600s x kanal_rate
    assert bilanz.bewaesserung_liter == 100.0


def test_manuell_ohne_liter_nutzt_schlauch_default(speicher):
    # 10 min Dauer, kein liter-Wert -> 10 x 10 L/min x anteil_kanal (1.0) = 100 L
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=datetime(2026, 4, 17, 18),
        zone_id="waldblumenhain", ventil_id="manuell",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=600,
        ausloser=Ausloser.MANUELL,
    )))

    zone = _zone(zone_id="waldblumenhain", ventil_kanal=1,
                 flaeche_m2=40.0, anteil_kanal=1.0)
    bilanz = _run(berechne_bilanz(
        zone, datetime(2026, 4, 17, 0), datetime(2026, 4, 17, 23),
        speicher, _konfig(), "standort_a",
    ))
    assert bilanz is not None
    assert bilanz.bewaesserung_liter == pytest.approx(100.0)


def test_regen_aus_archiv_bevorzugt(speicher):
    # 5 mm Archiv + 2 mm Forecast (unterschiedliche Stunden) -> beide summiert
    stunden_archiv = [
        WetterArchivStunde(zeitstempel=datetime(2026, 4, 15, h),
                           niederschlag_mm=1.0, temperatur=10.0, et0_mm=0.0)
        for h in range(5)
    ]
    _run(speicher.upsert_wetter_archiv(stunden_archiv, "standort_a"))

    forecast_vorhersage = [
        WetterStunde(zeitstempel=datetime(2026, 4, 15, h),
                     temperatur=10.0, niederschlag_mm=0.5,
                     niederschlag_wahrscheinlichkeit=0.0,
                     wind_kmh=0.0, wind_richtung_grad=0.0, et0_mm=0.0)
        for h in range(5, 9)
    ]
    _run(speicher.speichere_wetter(datetime(2026, 4, 15, 0),
                                   forecast_vorhersage, "standort_a"))

    zone = _zone(flaeche_m2=10.0)
    bilanz = _run(berechne_bilanz(
        zone, datetime(2026, 4, 15, 0), datetime(2026, 4, 15, 10),
        speicher, _konfig(), "standort_a",
    ))
    assert bilanz is not None
    # Archiv: 5 Stunden x 1 mm + Forecast: 4 Stunden x 0.5 mm = 5 + 2 = 7 mm
    # 7 mm x 10 m2 = 70 L
    assert bilanz.regen_liter == pytest.approx(70.0, abs=0.5)
    assert bilanz.quelle_niederschlag == "gemischt"


def test_quelle_archiv_wenn_vollstaendig(speicher):
    stunden = [
        WetterArchivStunde(zeitstempel=datetime(2026, 4, 15, h),
                           niederschlag_mm=0.2, temperatur=10.0, et0_mm=0.1)
        for h in range(24)
    ]
    _run(speicher.upsert_wetter_archiv(stunden, "standort_a"))

    zone = _zone(flaeche_m2=10.0)
    bilanz = _run(berechne_bilanz(
        zone, datetime(2026, 4, 15, 0), datetime(2026, 4, 15, 23),
        speicher, _konfig(), "standort_a",
    ))
    assert bilanz is not None
    assert bilanz.quelle_niederschlag == "archiv"
    assert bilanz.indikativ is False


def test_nur_forecast_markiert_indikativ(speicher):
    forecast = [
        WetterStunde(zeitstempel=datetime(2026, 4, 17, h),
                     temperatur=15.0, niederschlag_mm=0.0,
                     niederschlag_wahrscheinlichkeit=0.0,
                     wind_kmh=5.0, wind_richtung_grad=180.0, et0_mm=0.1)
        for h in range(24)
    ]
    _run(speicher.speichere_wetter(datetime(2026, 4, 17, 0),
                                   forecast, "standort_a"))

    zone = _zone(flaeche_m2=10.0)
    bilanz = _run(berechne_bilanz(
        zone, datetime(2026, 4, 17, 0), datetime(2026, 4, 17, 23),
        speicher, _konfig(), "standort_a",
    ))
    assert bilanz is not None
    assert bilanz.quelle_niederschlag == "forecast"
    assert bilanz.indikativ is True
    assert bilanz.verdunstet_liter == pytest.approx(24.0, abs=0.5)  # 24 x 0.1 x 10


def test_gesamt_bilanz_negativ_bei_verdunstung_ueber_zufuhr(speicher):
    # ET0=0.2 mm/h ueber 24h * 1.74 m2 = 8.35 L Verdunstung
    # Keine Bewaesserung, kein Regen -> Bilanz = -8.35 L
    forecast = [
        WetterStunde(zeitstempel=datetime(2026, 4, 17, h),
                     temperatur=25.0, niederschlag_mm=0.0,
                     niederschlag_wahrscheinlichkeit=0.0,
                     wind_kmh=0.0, wind_richtung_grad=0.0, et0_mm=0.2)
        for h in range(24)
    ]
    _run(speicher.speichere_wetter(datetime(2026, 4, 17, 0),
                                   forecast, "standort_a"))

    zone = _zone()
    bilanz = _run(berechne_bilanz(
        zone, datetime(2026, 4, 17, 0), datetime(2026, 4, 17, 23),
        speicher, _konfig(), "standort_a",
    ))
    assert bilanz is not None
    assert bilanz.bewaesserung_liter == 0.0
    assert bilanz.bilanz_liter < 0
    assert bilanz.verdunstet_liter == pytest.approx(8.35, abs=0.2)


def test_unbekannt_ausloeser_wird_uebersprungen_und_indikativ_markiert(speicher):
    # Heuristik-Event mit UNBEKANNT-Ausloeser: zaehlt nicht zur Bewaesserung,
    # Bilanz markiert "indikativ" bis User klassifiziert (T-0055-B1).
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=datetime(2026, 4, 15, 19, 0),
        zone_id="bambuswald", ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
        ausloser=Ausloser.UNBEKANNT,
    )))
    bilanz = _run(berechne_bilanz(
        _zone(), datetime(2026, 4, 15), datetime(2026, 4, 17),
        speicher, _konfig(), "standort_a",
    ))
    assert bilanz is not None
    assert bilanz.bewaesserung_liter == 0.0       # UNBEKANNT zaehlt nicht
    assert bilanz.indikativ is True                # aber als unsicher markiert


def test_live_manuell_event_nutzt_kanal_rate_nicht_schlauch_rate(speicher):
    """Codex-Finding P1: MANUELL-Events vom Live-WebSocket (ventil_id=UUID)
    und DHS (ventil_id=gardena_web) laufen ueber den Gardena-Kanal und
    sollten Kanal-Rate nutzen, nicht manuell_liter_pro_minute (Schlauch).
    Nur `/api/giessen`-Eintraege mit ventil_id='manuell' nutzen Schlauch."""
    # Live-Event: ventil_id ist UUID, ausloser=MANUELL (App-Trigger)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=datetime(2026, 4, 15, 19, 0),
        zone_id="bambuswald", ventil_id="11111111-db27-4f24-xxxx",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
        ausloser=Ausloser.MANUELL,
    )))
    bilanz = _run(berechne_bilanz(
        _zone(), datetime(2026, 4, 15), datetime(2026, 4, 17),
        speicher, _konfig(), "standort_a",
    ))
    assert bilanz is not None
    # Kanal-2-Rate 1.87 L/min × 10 min × 0.357 anteil = 6.68 L
    # NICHT manuell-Rate 10 L/min × 10 × 0.357 = 35.7 L
    assert bilanz.bewaesserung_liter == pytest.approx(6.7, abs=0.1)


def test_dhs_gardena_web_event_nutzt_kanal_rate(speicher):
    """Codex-Finding P1 Gegenprobe: DHS-Events mit ausloser=MANUELL
    (z.B. EXECUTED_MANUAL aus DHS) laufen ebenfalls ueber den Kanal."""
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=datetime(2026, 4, 15, 19, 0),
        zone_id="bambuswald", ventil_id="gardena_web",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=300,
        ausloser=Ausloser.MANUELL,
    )))
    bilanz = _run(berechne_bilanz(
        _zone(), datetime(2026, 4, 15), datetime(2026, 4, 17),
        speicher, _konfig(), "standort_a",
    ))
    # 5 min × 1.87 × 0.357 = 3.34 L (Kanal-Rate, nicht Schlauch)
    assert bilanz is not None
    assert bilanz.bewaesserung_liter == pytest.approx(3.3, abs=0.1)


def test_manuell_ohne_kanal_rate_wird_uebernommen(speicher):
    # Zone hat ventil_kanal=None (z.B. rein Monitoring). Manuelle Giessung
    # mit liter-Override muss trotzdem gezaehlt werden.
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=datetime(2026, 4, 17, 18),
        zone_id="zitrus", ventil_id="manuell",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL, liter=0.5,
    )))
    zone = _zone(zone_id="zitrus", ventil_kanal=None, flaeche_m2=0.08,
                 anteil_kanal=1.0)
    bilanz = _run(berechne_bilanz(
        zone, datetime(2026, 4, 17, 0), datetime(2026, 4, 17, 23),
        speicher, _konfig(), "standort_a",
    ))
    assert bilanz is not None
    assert bilanz.bewaesserung_liter == 0.5
