"""T-0063: Tests fuer automatische Feldkapazitaets-/Welkepunkt-Erkennung."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from bewaesserung.kalibrierung import (
    TYP_FELDKAPAZITAET,
    TYP_WELKEPUNKT,
    KalibrationsJob,
)
from bewaesserung.modelle import (
    DatenQuelle,
    GardenaKonfig,
    GesamtKonfig,
    KalibrierungKonfig,
    MlAusschlussFenster,
    SensorMessung,
    SpeicherKonfig,
    StandortKonfig,
    WetterArchivStunde,
    WetterKonfig,
    WetterStandortKonfig,
    WetterStunde,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=[
            ZonenKonfig(zone_id="testzone", name="Test", ventil_kanal=1),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="standort_a", breite=52.52, laenge=13.405)]
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten",
                wetter_standort="standort_a", zonen=["testzone"],
            ),
        ],
    )


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "kal.db"))
    _run(s.verbinden())
    yield s
    _run(s.schliessen())


def test_job_ruht_wenn_aktiv_false(speicher):
    konfig = _konfig()
    kal_konfig = KalibrierungKonfig(aktiv=False)
    job = KalibrationsJob(speicher, konfig, kal_konfig)
    assert _run(job.aktualisiere_wenn_faellig(datetime.now())) is False


def test_job_intervall_gate_blockiert_zweiten_lauf(speicher):
    """Zweiter Lauf innerhalb Intervall → False (kein Re-Scan)."""
    konfig = _konfig()
    kal_konfig = KalibrierungKonfig(aktiv=True, intervall_stunden=6)
    job = KalibrationsJob(speicher, konfig, kal_konfig)
    t0 = datetime(2026, 4, 20, 12, 0)
    assert _run(job.aktualisiere_wenn_faellig(t0)) is True  # erster Lauf
    assert _run(job.aktualisiere_wenn_faellig(t0 + timedelta(hours=3))) is False
    assert _run(job.aktualisiere_wenn_faellig(t0 + timedelta(hours=7))) is True


def test_feldkapazitaet_kandidat_wird_erkannt(speicher):
    """Regen-Peak > 10 mm in 24 h, 12-24 h spaeter stabiles Sensor-Plateau
    → Feldkapazitaets-Eintrag.
    """
    konfig = _konfig()
    jetzt = datetime(2026, 4, 20, 12, 0)

    # Regen-Stunden: 25 mm verteilt ueber 12 h, dann 12 h trocken
    peak_start = jetzt - timedelta(days=3)
    for h in range(24):
        t = peak_start + timedelta(hours=h)
        _run(speicher.upsert_wetter_archiv([WetterArchivStunde(
            zeitstempel=t, niederschlag_mm=(2.0 if h < 12 else 0.0),
            temperatur=12.0, et0_mm=0.1,
        )], standort_id="standort_a"))
    # 12 h trocken nach Regen-Ende
    for h in range(24, 36):
        t = peak_start + timedelta(hours=h)
        _run(speicher.upsert_wetter_archiv([WetterArchivStunde(
            zeitstempel=t, niederschlag_mm=0.0, temperatur=14.0, et0_mm=0.2,
        )], standort_id="standort_a"))

    # Sensor-Werte: Plateau bei 75 % von t+12 h bis t+24 h nach Regen-Ende.
    # Regen-Peak endet bei peak_start + 12h, Plateau-Fenster 12-24h spaeter.
    regen_ende = peak_start + timedelta(hours=12)
    for i in range(30):  # reichlich Messungen ueber das Fenster
        t = regen_ende + timedelta(hours=12) + timedelta(minutes=i * 30)
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=t, zone_id="testzone", geraet_id="test",
            boden_feuchte=75.0, boden_temperatur=12.0, quelle=DatenQuelle.GARDENA,
        )))

    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig(aktiv=True))
    _run(job.aktualisiere_wenn_faellig(jetzt))

    kandidaten = _run(speicher.hole_kalibrierungen(
        zone_id="testzone", typ=TYP_FELDKAPAZITAET,
    ))
    assert len(kandidaten) >= 1
    assert kandidaten[0]["wert"] == pytest.approx(75.0, abs=1)
    assert kandidaten[0]["basis_mm"] is not None
    assert kandidaten[0]["basis_mm"] >= 10.0


def test_feldkapazitaet_plateau_muss_stabil_sein(speicher):
    """Ist das Sensor-Fenster zu volatil (max-min > 2×plateau_delta),
    wird KEIN Kandidat erzeugt.
    """
    konfig = _konfig()
    jetzt = datetime(2026, 4, 20, 12, 0)
    peak_start = jetzt - timedelta(days=3)
    for h in range(24):
        t = peak_start + timedelta(hours=h)
        _run(speicher.upsert_wetter_archiv([WetterArchivStunde(
            zeitstempel=t, niederschlag_mm=(2.0 if h < 12 else 0.0),
            temperatur=12.0, et0_mm=0.1,
        )], standort_id="standort_a"))
    for h in range(24, 36):
        t = peak_start + timedelta(hours=h)
        _run(speicher.upsert_wetter_archiv([WetterArchivStunde(
            zeitstempel=t, niederschlag_mm=0.0, temperatur=14.0, et0_mm=0.2,
        )], standort_id="standort_a"))

    regen_ende = peak_start + timedelta(hours=12)
    # Volatil: 60-80 im Fenster
    for i, wert in enumerate([60.0, 80.0, 65.0, 75.0, 70.0, 85.0] * 5):
        t = regen_ende + timedelta(hours=12) + timedelta(minutes=i * 15)
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=t, zone_id="testzone", geraet_id="test",
            boden_feuchte=wert, boden_temperatur=12.0, quelle=DatenQuelle.GARDENA,
        )))

    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig(aktiv=True))
    _run(job.aktualisiere_wenn_faellig(jetzt))
    kandidaten = _run(speicher.hole_kalibrierungen(
        zone_id="testzone", typ=TYP_FELDKAPAZITAET,
    ))
    assert len(kandidaten) == 0, (
        "Volatiles Fenster sollte kein Plateau-Kandidat liefern"
    )


def test_welkepunkt_nur_in_saison(speicher):
    """Trockenphase im April (ausser Saison) → kein Kandidat."""
    konfig = _konfig()
    # April = Monat 4, nicht in Default-Saison [5-9]
    trocken_start = datetime(2026, 3, 20, 0, 0)  # 10+ Tage trocken im Maerz
    for tag in range(15):
        # Tag-Mitte-Messung, kein Regen
        t = trocken_start + timedelta(days=tag, hours=12)
        _run(speicher.upsert_wetter_archiv([WetterArchivStunde(
            zeitstempel=t, niederschlag_mm=0.0, temperatur=10.0, et0_mm=0.3,
        )], standort_id="standort_a"))
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=t, zone_id="testzone", geraet_id="test",
            boden_feuchte=35.0, boden_temperatur=10.0, quelle=DatenQuelle.GARDENA,
        )))

    jetzt = trocken_start + timedelta(days=20)
    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig(aktiv=True))
    _run(job.aktualisiere_wenn_faellig(jetzt))
    kandidaten = _run(speicher.hole_kalibrierungen(
        zone_id="testzone", typ=TYP_WELKEPUNKT,
    ))
    assert len(kandidaten) == 0


def test_feldkapazitaet_null_plateau_wird_verworfen(speicher):
    """T-0387: Ein reines 0.0-"Plateau" (Sensor im Lager/Ausfall) darf keine
    Feldkapazitaet=0.0 persistieren. Realfall hecke 07.-14.05.: Sensor lag im
    Lager, lieferte konstant 0.0 -> 26x Feldkapazitaet=0.0 -> welkepunkt=0.0.
    Der 0.0-Fall liegt VOR dem fruehesten ml_ausschluss_fenster (19.05.), also
    traegt hier allein der Wert-Ebenen-Filter.
    """
    konfig = _konfig()
    jetzt = datetime(2026, 4, 20, 12, 0)
    peak_start = jetzt - timedelta(days=3)
    for h in range(24):
        t = peak_start + timedelta(hours=h)
        _run(speicher.upsert_wetter_archiv([WetterArchivStunde(
            zeitstempel=t, niederschlag_mm=(2.0 if h < 12 else 0.0),
            temperatur=12.0, et0_mm=0.1,
        )], standort_id="standort_a"))
    for h in range(24, 36):
        t = peak_start + timedelta(hours=h)
        _run(speicher.upsert_wetter_archiv([WetterArchivStunde(
            zeitstempel=t, niederschlag_mm=0.0, temperatur=14.0, et0_mm=0.2,
        )], standort_id="standort_a"))
    regen_ende = peak_start + timedelta(hours=12)
    # Sensor liefert konstant 0.0 (im Lager) -> waere ein perfekt "stabiles"
    # Plateau und wuerde ohne Filter als Feldkapazitaet 0.0 persistiert.
    for i in range(30):
        t = regen_ende + timedelta(hours=12) + timedelta(minutes=i * 30)
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=t, zone_id="testzone", geraet_id="test",
            boden_feuchte=0.0, boden_temperatur=12.0, quelle=DatenQuelle.GARDENA,
        )))
    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig(aktiv=True))
    _run(job.aktualisiere_wenn_faellig(jetzt))
    kandidaten = _run(speicher.hole_kalibrierungen(
        zone_id="testzone", typ=TYP_FELDKAPAZITAET,
    ))
    assert len(kandidaten) == 0, "0.0-Plateau darf keine Feldkapazitaet erzeugen"


def test_welkepunkt_null_proxy_wird_verworfen(speicher):
    """T-0387: In-Saison-Trockenphase, aber Sensor liefert konstant 0.0
    (Lager/Ausfall) -> kein Welkepunkt-Proxy 0.0 (Realfall hecke 25.-27.05.).
    """
    konfig = _konfig()
    trocken_start = datetime(2026, 5, 5, 0, 0)  # Mai = Saison-Monat
    for tag in range(15):
        t = trocken_start + timedelta(days=tag, hours=12)
        _run(speicher.upsert_wetter_archiv([WetterArchivStunde(
            zeitstempel=t, niederschlag_mm=0.0, temperatur=18.0, et0_mm=0.3,
        )], standort_id="standort_a"))
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=t, zone_id="testzone", geraet_id="test",
            boden_feuchte=0.0, boden_temperatur=18.0, quelle=DatenQuelle.GARDENA,
        )))
    jetzt = trocken_start + timedelta(days=20)
    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig(aktiv=True))
    _run(job.aktualisiere_wenn_faellig(jetzt))
    kandidaten = _run(speicher.hole_kalibrierungen(
        zone_id="testzone", typ=TYP_WELKEPUNKT,
    ))
    assert len(kandidaten) == 0, "0.0-Werte duerfen keinen Welkepunkt-Proxy erzeugen"


def test_kalibrierung_ehrt_ausschluss_fenster(speicher):
    """T-0387: Werte in einem ml_ausschluss_fenster (Sensor-Umzug, Skalenbruch)
    duerfen keine Kalibrier-Referenz erzeugen -- auch als valides Plateau.
    Deckt den Forward-Fall (T-0385: kaputte FYTAs 0.3, die der 0-Filter NICHT
    faengt, aber das Fenster). Vorher reichten die Scans die Fenster nicht durch.
    """
    jetzt = datetime(2026, 4, 20, 12, 0)
    peak_start = jetzt - timedelta(days=3)
    regen_ende = peak_start + timedelta(hours=12)
    plateau_von = regen_ende + timedelta(hours=12)
    konfig = _konfig()
    konfig.ml_ausschluss_fenster = [
        MlAusschlussFenster(
            zone_id="testzone",
            von=plateau_von - timedelta(hours=1),
            bis=plateau_von + timedelta(hours=24),
        )
    ]
    for h in range(24):
        t = peak_start + timedelta(hours=h)
        _run(speicher.upsert_wetter_archiv([WetterArchivStunde(
            zeitstempel=t, niederschlag_mm=(2.0 if h < 12 else 0.0),
            temperatur=12.0, et0_mm=0.1,
        )], standort_id="standort_a"))
    for h in range(24, 36):
        t = peak_start + timedelta(hours=h)
        _run(speicher.upsert_wetter_archiv([WetterArchivStunde(
            zeitstempel=t, niederschlag_mm=0.0, temperatur=14.0, et0_mm=0.2,
        )], standort_id="standort_a"))
    # Valides 75%-Plateau, aber komplett im Ausschluss-Fenster.
    for i in range(30):
        t = plateau_von + timedelta(minutes=i * 30)
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=t, zone_id="testzone", geraet_id="test",
            boden_feuchte=75.0, boden_temperatur=12.0, quelle=DatenQuelle.GARDENA,
        )))
    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig(aktiv=True))
    _run(job.aktualisiere_wenn_faellig(jetzt))
    kandidaten = _run(speicher.hole_kalibrierungen(
        zone_id="testzone", typ=TYP_FELDKAPAZITAET,
    ))
    assert len(kandidaten) == 0, "Werte im Ausschluss-Fenster duerfen nicht kalibrieren"


def test_feldkapazitaet_fallback_auf_vorhersage(speicher):
    """T-0063a: Wenn `wetter_archiv` leer ist (ERA5-Latenz), greift der
    Job ueber `hole_wetter_kombiniert` auf `wetter_vorhersage` zurueck
    und findet den Peak trotzdem.
    """
    konfig = _konfig()
    jetzt = datetime(2026, 4, 20, 12, 0)
    peak_start = jetzt - timedelta(days=3)

    # Absichtlich KEIN Archiv-Upsert, nur Vorhersage-Speicherung.
    # speichere_wetter verlangt eine `abfrage_zeit` + Liste von
    # WetterStunde-Objekten (Vorhersagen pro Stunde).
    stunden_regen = []
    for h in range(24):
        t = peak_start + timedelta(hours=h)
        stunden_regen.append(WetterStunde(
            zeitstempel=t,
            temperatur=12.0,
            niederschlag_mm=(2.0 if h < 12 else 0.0),
            niederschlag_wahrscheinlichkeit=100.0,
            wind_kmh=5.0, wind_richtung_grad=180.0, et0_mm=0.1,
        ))
    for h in range(24, 36):
        t = peak_start + timedelta(hours=h)
        stunden_regen.append(WetterStunde(
            zeitstempel=t,
            temperatur=14.0,
            niederschlag_mm=0.0, niederschlag_wahrscheinlichkeit=0.0,
            wind_kmh=5.0, wind_richtung_grad=180.0, et0_mm=0.2,
        ))
    _run(speicher.speichere_wetter(
        abfrage_zeit=peak_start - timedelta(hours=1),
        stunden=stunden_regen, standort_id="standort_a",
    ))

    # Plateau-Sensor-Daten: 70/75/70 (spread 5, knapp unter Default 10)
    regen_ende = peak_start + timedelta(hours=12)
    for i, wert in enumerate([70.0, 75.0, 70.0, 75.0, 70.0] * 6):
        t = regen_ende + timedelta(hours=12) + timedelta(minutes=i * 30)
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=t, zone_id="testzone", geraet_id="test",
            boden_feuchte=wert, boden_temperatur=3.0,
            quelle=DatenQuelle.GARDENA,
        )))

    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig(aktiv=True))
    _run(job.aktualisiere_wenn_faellig(jetzt))

    kandidaten = _run(speicher.hole_kalibrierungen(
        zone_id="testzone", typ=TYP_FELDKAPAZITAET,
    ))
    assert len(kandidaten) >= 1, (
        "Mit Vorhersage-Fallback muss Peak gefunden werden"
    )
    # Boden-T-3.0 sollte in notizen stehen (neu in T-0063a)
    assert "Boden-T" in (kandidaten[0]["notizen"] or "")


def test_feldkapazitaet_plateau_akzeptiert_2_sensor_stufen(speicher):
    """T-0063a Regression: Gardena-Sensor 5%-Stufen, in 12h typischerweise
    1-2 Stufen Abfall (z. B. 75 -> 70 -> 65). Mit Default
    `plateau_max_delta=5.0` ist Spread 10 noch akzeptabel; ein FK-Kandidat
    soll entstehen (vor T-0063a wurde das verworfen, weil 2*delta=6).
    """
    konfig = _konfig()
    jetzt = datetime(2026, 4, 20, 12, 0)
    peak_start = jetzt - timedelta(days=3)
    for h in range(24):
        t = peak_start + timedelta(hours=h)
        _run(speicher.upsert_wetter_archiv([WetterArchivStunde(
            zeitstempel=t, niederschlag_mm=(2.0 if h < 12 else 0.0),
            temperatur=12.0, et0_mm=0.1,
        )], standort_id="standort_a"))
    for h in range(24, 36):
        t = peak_start + timedelta(hours=h)
        _run(speicher.upsert_wetter_archiv([WetterArchivStunde(
            zeitstempel=t, niederschlag_mm=0.0, temperatur=14.0, et0_mm=0.2,
        )], standort_id="standort_a"))

    regen_ende = peak_start + timedelta(hours=12)
    # Plateau mit 2 Sensor-Stufen: 75 -> 70 -> 65 ueber 12h, spread = 10
    werte = [75.0] * 8 + [70.0] * 12 + [65.0] * 8
    for i, wert in enumerate(werte):
        t = regen_ende + timedelta(hours=12) + timedelta(minutes=i * 30)
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=t, zone_id="testzone", geraet_id="test",
            boden_feuchte=wert, boden_temperatur=3.0,
            quelle=DatenQuelle.GARDENA,
        )))

    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig(aktiv=True))
    _run(job.aktualisiere_wenn_faellig(jetzt))

    kandidaten = _run(speicher.hole_kalibrierungen(
        zone_id="testzone", typ=TYP_FELDKAPAZITAET,
    ))
    assert len(kandidaten) == 1
    # Kandidat liegt im Plateau-Bereich 65-75 (je nachdem wie das
    # 12h-Plateau-Fenster die 30-Min-Samples anschneidet).
    assert 65.0 <= kandidaten[0]["wert"] <= 75.0


def test_speichere_kalibrierung_idempotent(speicher):
    """Gleicher Eintrag (zone+typ+stunde) landet nur einmal in DB."""
    t = datetime(2026, 4, 20, 12, 30)  # wird auf 12:00 gerundet
    for _ in range(3):
        _run(speicher.speichere_kalibrierung(
            zeitstempel=t, zone_id="testzone", typ=TYP_FELDKAPAZITAET,
            wert=75.0, basis_mm=15.0,
        ))
    kandidaten = _run(speicher.hole_kalibrierungen(zone_id="testzone"))
    assert len(kandidaten) == 1


# T-0085: Wirkungsrate-Auto-Kalibrierung -------------------------------------


def _baue_event(
    zeitstempel: datetime, zone_id: str, dauer_s: int,
    aktion: str = "schliessen", ausloser: str = "manuell",
    ventil_id: str = "testkanal",
    lauf_gruppe: str | None = None,
    phase: str | None = None,
):
    from bewaesserung.modelle import VentilEreignis, VentilAktion, Ausloser
    return VentilEreignis(
        zeitstempel=zeitstempel,
        zone_id=zone_id,
        ventil_id=ventil_id,
        aktion=VentilAktion(aktion),
        dauer_sekunden=dauer_s,
        ausloser=Ausloser(ausloser),
        lauf_gruppe=lauf_gruppe,
        phase=phase,
    )


async def _baue_messung_async(
    speicher, zeitstempel, feuchte, zone="testzone", geraet_id="",
):
    await speicher.speichere_messung(SensorMessung(
        zeitstempel=zeitstempel, zone_id=zone, geraet_id=geraet_id,
        boden_feuchte=feuchte, quelle=DatenQuelle.GARDENA,
    ))


def test_wirkungsrate_happy_path_event_und_sensoren_qualifizieren(speicher):
    """T-0085: Standardfall — 90 min SCHLIESSEN, 30 % vorher, 50 % nachher
    → 0.222 pp/min wird in feldkapazitaet_messung gespeichert."""
    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig())
    t_close = datetime(2026, 4, 27, 12, 35)
    t_start = t_close - timedelta(minutes=90)
    # Sensor 30 min vor Start (= Sensor zum Beginn) und 6h nach Close.
    _run(_baue_messung_async(speicher, t_start, 30.0))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 50.0))
    event = _baue_event(t_close, "testzone", 5400)

    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event],
    ))
    assert neu == 1
    eintraege = _run(speicher.hole_kalibrierungen("testzone", typ="wirkungsrate"))
    assert len(eintraege) == 1
    e = eintraege[0]
    # 20 pp / 90 min = 0.222 pp/min
    assert abs(e["wert"] - 0.222) < 0.01
    assert e["basis_mm"] == 90.0


def test_wirkungsrate_pre_soak_gruppe_wird_aggregiert(speicher):
    """T-0366: Pre-Soak-Puls + Hauptdose zaehlen als ein Kandidat."""
    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig())
    t_haupt_close = datetime(2026, 6, 30, 12, 35)
    t_start = t_haupt_close - timedelta(minutes=35)
    _run(_baue_messung_async(speicher, t_start, 30.0, geraet_id="sensor-a"))
    _run(_baue_messung_async(
        speicher, t_haupt_close + timedelta(hours=6), 45.0, geraet_id="sensor-a",
    ))
    puls = _baue_event(
        t_start + timedelta(minutes=5), "testzone", 300,
        lauf_gruppe="presoak-1", phase="pre_soak",
    )
    haupt = _baue_event(
        t_haupt_close, "testzone", 1800,
        lauf_gruppe="presoak-1", phase="haupt",
    )

    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_start - timedelta(days=1),
        bis=t_haupt_close + timedelta(hours=12),
        ventil_events=[puls, haupt],
    ))

    assert neu == 1
    eintraege = _run(speicher.hole_kalibrierungen("testzone", typ="wirkungsrate"))
    assert eintraege[0]["basis_mm"] == 35.0


def test_wirkungsrate_pre_soak_minigruppe_wird_verworfen(speicher):
    """T-0366: 5-min-Puls + 5-min-Mini-Hauptdose erzeugen keinen 1.0-Record."""
    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig())
    t_haupt_close = datetime(2026, 6, 30, 1, 44)
    t_start = t_haupt_close - timedelta(seconds=597)
    _run(_baue_messung_async(speicher, t_start, 50.0, geraet_id="sensor-a"))
    _run(_baue_messung_async(
        speicher, t_haupt_close + timedelta(hours=6), 55.0, geraet_id="sensor-a",
    ))
    puls = _baue_event(
        t_start + timedelta(seconds=300), "testzone", 300,
        lauf_gruppe="presoak-mini", phase="pre_soak",
    )
    haupt = _baue_event(
        t_haupt_close, "testzone", 297,
        lauf_gruppe="presoak-mini", phase="haupt",
    )

    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_start - timedelta(days=1),
        bis=t_haupt_close + timedelta(hours=12),
        ventil_events=[puls, haupt],
    ))

    assert neu == 0


def test_wirkungsrate_mischt_f_vor_f_nach_nicht_zwischen_sensoren(speicher):
    """T-0366/F9: f_vor von Sensor A + f_nach von Sensor B ist kein Paar."""
    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig())
    t_close = datetime(2026, 6, 30, 12, 35)
    t_start = t_close - timedelta(minutes=90)
    _run(_baue_messung_async(speicher, t_start, 30.0, geraet_id="sensor-a"))
    _run(_baue_messung_async(
        speicher, t_close + timedelta(hours=6), 50.0, geraet_id="sensor-b",
    ))
    event = _baue_event(t_close, "testzone", 5400)

    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event],
    ))

    assert neu == 0


def test_wirkungsrate_phantom_ausloser_geskippt(speicher):
    """sensor_heuristik-Phantom-Events fliessen NICHT in die Kalibrierung."""
    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig())
    t_close = datetime(2026, 4, 27, 12, 35)
    _run(_baue_messung_async(speicher, t_close - timedelta(minutes=90), 30.0))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 50.0))
    # Wir nutzen "unbekannt" als Ausloser — entspricht sensor_heuristik-Phantom.
    event = _baue_event(t_close, "testzone", 5400, ausloser="unbekannt")
    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event],
    ))
    assert neu == 0


def test_wirkungsrate_f3_geflipptes_heuristik_event_geskippt(speicher):
    """F3: Ein auf `manuell` geflipptes sensor_heuristik-Event hat eine aus
    dem Feuchte-delta KONSTRUIERTE Pseudo-Dauer -> `rate = delta/dauer` waere
    zirkulaer (≡ konstant, kein Lernsignal). Trotz ausloser=manuell (passt die
    Allowlist) muss es per ventil_id='sensor_heuristik' raus. Gleiche Daten wie
    der Happy-Path, nur die ventil_id unterscheidet (-> neu==0 statt 1)."""
    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig())
    t_close = datetime(2026, 4, 27, 12, 35)
    _run(_baue_messung_async(speicher, t_close - timedelta(minutes=90), 30.0))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 50.0))
    event = _baue_event(
        t_close, "testzone", 5400,
        ausloser="manuell", ventil_id="sensor_heuristik",
    )
    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event],
    ))
    assert neu == 0


def test_wirkungsrate_aquabloom_ausloser_geskippt(speicher):
    """T-0168: AQUABLOOM-Events sind zu klein (10 min × 0.083 L); Sensor-
    Antwort liegt unter der 5pp-Quantisierung. Filter `_scan_wirkungsrate`
    laesst nur MANUELL/AUTOMATIK durch — AQUABLOOM bleibt analog zu
    UNBEKANNT/IGNORIERT aussen vor.
    """
    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig())
    t_close = datetime(2026, 6, 15, 8, 10)  # in der AquaBloom-Saison
    # Pseudo-Daten mit grossem Delta (waere reizvoll fuer Median),
    # aber Filter muss trotzdem blocken.
    _run(_baue_messung_async(speicher, t_close - timedelta(minutes=15), 30.0))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 50.0))
    event = _baue_event(t_close, "testzone", 600, ausloser="aquabloom")
    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event],
    ))
    assert neu == 0


def test_wirkungsrate_saturierungs_filter(speicher):
    """f_vor >= 75 % -> Sensor saturiert, keine sinnvolle Messung."""
    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig())
    t_close = datetime(2026, 4, 27, 12, 35)
    _run(_baue_messung_async(speicher, t_close - timedelta(minutes=90), 80.0))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 90.0))
    event = _baue_event(t_close, "testzone", 5400)
    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event],
    ))
    assert neu == 0


def test_wirkungsrate_stunden_cadence_sensor_findet_f_vor(speicher):
    """T-0085-Bugfix: 1×/h-Sensor-Cadence darf f_vor nicht ausfiltern.

    Realfall waldblumenhain 27.04.: 89-min-Bewaesserung start 11:05,
    Sensor-Ticks 10:34 (35 %, 31 min vor Start) und 11:35 (35 %, 30 min
    nach Start). Mit der originalen 30-min-rueckwaerts-Toleranz fiel der
    10:34-Tick aus dem Fenster, der 11:35-Tick wurde durch
    'rueckwaerts'-Strict abgelehnt → f_vor=None → Event verworfen.

    Fix: 75 min Toleranz + 5-min-Vorwaerts-Fenster (analog T-0069 fuer
    Response-Features). Der 11:35-Tick liegt dann zwar nicht mehr in
    der Vorwaerts-Toleranz, aber 10:34 ist im 75-min-Rueckwaerts-Fenster.
    """
    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig())
    t_close = datetime(2026, 4, 27, 12, 35)
    t_start = t_close - timedelta(minutes=89, seconds=23)  # 11:05:37
    # Sensor 31 min VOR t_start (genau wie 27.04.: 10:34 vs. 11:05).
    _run(_baue_messung_async(
        speicher, t_start - timedelta(minutes=31), 35.0,
    ))
    # Sensor 30 min NACH t_start (Bewaesserung schon laufend, sieht
    # Wasser noch nicht im 1xh-Tick → muss als Pre-Event gewertet werden,
    # falls der 31-min-Vor-Tick nicht reicht).
    _run(_baue_messung_async(
        speicher, t_start + timedelta(minutes=30), 35.0,
    ))
    # Sensor 6h nach Close → effektive Wirkung gemessen.
    _run(_baue_messung_async(
        speicher, t_close + timedelta(hours=6), 50.0,
    ))
    event = _baue_event(t_close, "testzone", 89 * 60 + 23)

    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event],
    ))
    assert neu == 1, (
        "1xh-Sensor-Cadence muss durch 75-min-Toleranz aufgefangen werden — "
        "sonst geht der echte 89-min-Test 27.04. verloren."
    )
    eintraege = _run(speicher.hole_kalibrierungen("testzone", typ="wirkungsrate"))
    assert len(eintraege) == 1
    # 15 pp / 89.4 min ≈ 0.168 pp/min
    assert abs(eintraege[0]["wert"] - 0.168) < 0.01


def test_wirkungsrate_post_saturierung_filter(speicher):
    """T-0085 Variante B: f_nach >= 85 -> externe Wasserzufuhr im
    Eval-Fenster (User-Schlauch / Starkregen). Realfall Bambus 18.04.:
    5 min Bewaesserung, f_vor=65, f_nach=90 → faelschlich 5 pp/min.
    Nach Filter wird das Event verworfen.
    """
    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig())
    t_close = datetime(2026, 4, 18, 13, 1)
    _run(_baue_messung_async(speicher, t_close - timedelta(minutes=30), 65.0))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 90.0))
    event = _baue_event(t_close, "testzone", 300, ausloser="automatik")
    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event],
    ))
    assert neu == 0


def test_wirkungsrate_unbekannt_innerhalb_karenz_ist_phantom(speicher):
    """T-0085 Variante B: Heuristik-Event INNERHALB der Versickerungs-
    Karenz ist die verspaetete Sensor-Antwort der eigenen Bewaesserung
    (Phantom), nicht externer Eingriff. Hauptevent darf NICHT verworfen
    werden. Realfall Waldblumen 23.04.: 12:04-Bewaesserung (59 min),
    Sensor-Heuristik triggerte um 17:02 (= 4h58 spaeter, Karenz 6h).
    """
    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig())
    t_close = datetime(2026, 4, 23, 12, 4)
    _run(_baue_messung_async(speicher, t_close - timedelta(minutes=59), 35.0))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 50.0))
    haupt = _baue_event(t_close, "testzone", 59 * 60, ausloser="manuell")
    # 4h58 nach Close — innerhalb der 6h-Karenz von Waldblumenhain
    phantom = _baue_event(
        t_close + timedelta(hours=4, minutes=58), "testzone", 900,
        ausloser="unbekannt",
    )
    # Konfig mit 6h-Karenz fuer testzone
    konfig = _konfig()
    konfig.zonen[0].versickerungs_karenz_stunden = 6
    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig())
    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[haupt, phantom],
    ))
    assert neu == 1, "Heuristik-Phantom innerhalb Karenz darf Hauptevent nicht blocken"


def test_wirkungsrate_unbekannt_nach_karenz_ist_extern(speicher):
    """T-0085 Variante B: Heuristik-Event AUSSERHALB der Versickerungs-
    Karenz ist nicht protokollierte externe Wasserzufuhr (User-Schlauch).
    Hauptevent muss verworfen werden. Realfall Bambus 18.04.: 13:01-
    Bewaesserung (5 min), User-Schlauch-Heuristik um 16:22 (= 3h21
    spaeter, Karenz 3h).
    """
    konfig = _konfig()
    konfig.zonen[0].versickerungs_karenz_stunden = 3
    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig())
    t_close = datetime(2026, 4, 18, 13, 1)
    _run(_baue_messung_async(speicher, t_close - timedelta(minutes=30), 65.0))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 75.0))
    haupt = _baue_event(t_close, "testzone", 300, ausloser="automatik")
    # 3h21 nach Close — knapp ausserhalb der 3h-Karenz von Bambus
    schlauch = _baue_event(
        t_close + timedelta(hours=3, minutes=21), "testzone", 300,
        ausloser="unbekannt",
    )
    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[haupt, schlauch],
    ))
    assert neu == 0, "Heuristik-Event nach Karenz = externer Eingriff, blockt"


def test_wirkungsrate_regen_querpruefung_blockt(speicher):
    """T-0085 Variante B: Regen > 1 mm im Eval-Fenster -> Event verworfen.
    Realfall Yogaraum 12.04.: Niesel-Tag mit ~3 mm akkumuliert lasst
    Sensor scheinbar durch 5 min Bewaesserung um 5 pp steigen.
    """
    from bewaesserung.modelle import WetterArchivStunde

    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig())
    t_close = datetime(2026, 4, 12, 14, 0)
    _run(_baue_messung_async(speicher, t_close - timedelta(minutes=30), 65.0))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 70.0))
    event = _baue_event(t_close, "testzone", 300, ausloser="manuell")
    # 4 h × 0.4 mm/h = 1.6 mm im Eval-Fenster — ueber 1.0 mm Schwelle
    wetter = [
        WetterArchivStunde(
            zeitstempel=t_close + timedelta(hours=h),
            niederschlag_mm=0.4, temperatur=10.0, et0_mm=0.05,
        )
        for h in range(1, 5)
    ]
    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event],
        wetter=wetter,
    ))
    assert neu == 0
    # Gegenprobe: ohne Regen wuerde der Event durchgehen
    neu_ohne_regen = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event],
        wetter=[],
    ))
    assert neu_ohne_regen == 1


def test_wirkungsrate_dauer_hard_cap(speicher):
    """T-0129 (H-5): Dauer > 120 min wird abgelehnt -- selbst MANUELL.
    Schuetzt vor Phantom-Inflation, die Hard-Cap-Schwelle ueberschreitet."""
    konfig = _konfig()
    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig())
    t_close = datetime(2026, 4, 27, 12, 35)
    # 130 min -- ueber Hard-Cap 120 min
    _run(_baue_messung_async(speicher, t_close - timedelta(minutes=130), 20.0))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 35.0))
    event = _baue_event(t_close, "testzone", 130 * 60, ausloser="manuell")
    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event],
    ))
    assert neu == 0


def test_wirkungsrate_zone_cap_blockt_stale_close_inflation(speicher):
    """T-0129 (H-5): AUTOMATIK-Event mit Stale-CLOSED-Inflation
    (5400+1800=7200s = 120 min, aber Zone-Cap 1800*1.10=1980s) wird
    abgelehnt. Das ist der konkrete Akt-4-Fall vom 29.04.2026."""
    konfig = _konfig()
    # Default-ZonenKonfig hat max_dauer_sekunden=1800 (30 min).
    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig())
    t_close = datetime(2026, 4, 27, 12, 35)
    # 60 min nominale Dauer + 30 min Stale-Inflation = 90 min, AUTOMATIK.
    # zone_max=1800s * 1.10 = 1980s. Inflation = 5400s = 90 min weit drueber.
    _run(_baue_messung_async(speicher, t_close - timedelta(minutes=90), 30.0))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 50.0))
    event = _baue_event(t_close, "testzone", 90 * 60, ausloser="automatik")
    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event],
    ))
    assert neu == 0


def test_wirkungsrate_manuell_ueber_zone_cap_aber_unter_hard_cap_akzeptiert(speicher):
    """T-0129 (H-5) Negativ-Kontrolle: MANUELL wird NICHT vom Per-Zone-Cap
    eingeschraenkt -- nur AUTOMATIK. User-Schlauchsession 100 min ist
    legitim, auch wenn zone.max_dauer = 30 min ist."""
    konfig = _konfig()
    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig())
    t_close = datetime(2026, 4, 27, 12, 35)
    _run(_baue_messung_async(speicher, t_close - timedelta(minutes=100), 30.0))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 50.0))
    # 100 min < 120 min Hard-Cap, MANUELL umgeht Per-Zone-Cap.
    event = _baue_event(t_close, "testzone", 100 * 60, ausloser="manuell")
    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event],
    ))
    assert neu == 1


def test_wirkungsrate_outlier_filter_bei_grossem_delta(speicher):
    """delta >= 30 pp deutet auf Mehrfach-Event/Regen hin → ausgeschlossen."""
    job = KalibrationsJob(speicher, _konfig(), KalibrierungKonfig())
    t_close = datetime(2026, 4, 27, 12, 35)
    _run(_baue_messung_async(speicher, t_close - timedelta(minutes=90), 20.0))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 80.0))  # +60 pp
    event = _baue_event(t_close, "testzone", 5400)
    neu = _run(job._scan_wirkungsrate(
        "testzone",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event],
    ))
    assert neu == 0


# T-0085 _aufgeloeste_wirkungsrate Tests -------------------------------------


def test_aufgeloeste_wirkungsrate_konfig_override_wins(speicher):
    """Konfig-Override (zone.delta_pp_pro_minute) hat absoluten Vorrang."""
    from bewaesserung.entscheidung import Entscheidungsmotor
    from bewaesserung.modelle import ZonenKonfig
    zone = ZonenKonfig(
        zone_id="z", name="Z", delta_pp_pro_minute=0.5,
    )
    motor = Entscheidungsmotor(speicher, MagicMock(), [zone])
    wert, quelle = _run(motor._aufgeloeste_wirkungsrate(zone))
    assert wert == 0.5
    assert quelle == "manuell"


def test_aufgeloeste_wirkungsrate_kalibrierung_bei_n3_plus(speicher):
    """Ohne Konfig-Override: Median aus letzten >=3 Kalibrierungs-Eintraegen."""
    from bewaesserung.entscheidung import Entscheidungsmotor
    from bewaesserung.modelle import ZonenKonfig
    zone = ZonenKonfig(
        zone_id="z2", name="Z2", delta_pp_pro_minute=None,
    )
    jetzt = datetime(2026, 4, 28, 8, 0)
    # 3 Kalibrierungs-Werte einspeichern.
    for i, w in enumerate([0.15, 0.20, 0.25]):
        _run(speicher.speichere_kalibrierung(
            zeitstempel=jetzt - timedelta(days=i + 1),
            zone_id="z2", typ="wirkungsrate", wert=w,
            basis_mm=90.0, notizen=f"test {i}",
        ))
    motor = Entscheidungsmotor(speicher, MagicMock(), [zone])
    wert, quelle = _run(motor._aufgeloeste_wirkungsrate(zone, jetzt=jetzt))
    assert wert == 0.20  # Median
    assert quelle == "kalibrierung"


def test_aufgeloeste_wirkungsrate_default_bei_n_unter_3(speicher):
    """n<3 -> Default 1.0 pp/min, quelle='default'."""
    from bewaesserung.entscheidung import (
        DEFAULT_DELTA_PP_PRO_MINUTE, Entscheidungsmotor,
    )
    from bewaesserung.modelle import ZonenKonfig
    zone = ZonenKonfig(
        zone_id="z3", name="Z3", delta_pp_pro_minute=None,
    )
    jetzt = datetime(2026, 4, 28, 8, 0)
    # Nur 2 Werte, weniger als min_n=3.
    for i, w in enumerate([0.15, 0.20]):
        _run(speicher.speichere_kalibrierung(
            zeitstempel=jetzt - timedelta(days=i + 1),
            zone_id="z3", typ="wirkungsrate", wert=w,
            basis_mm=90.0, notizen=f"test {i}",
        ))
    motor = Entscheidungsmotor(speicher, MagicMock(), [zone])
    wert, quelle = _run(motor._aufgeloeste_wirkungsrate(zone, jetzt=jetzt))
    assert wert == DEFAULT_DELTA_PP_PRO_MINUTE
    assert quelle == "default"


# T-0150: Cluster-Geschwister-Filter (Druckverlust durch parallelen Lauf) ----


def _konfig_cluster() -> GesamtKonfig:
    """Konfig mit zwei Zonen (zoneA + zoneB) im selben Hahn-Cluster, aber
    auf unterschiedlichen Ventil-Kanaelen — also Druck-Konkurrenz beim
    parallelen Lauf.
    """
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=[
            ZonenKonfig(
                zone_id="zoneA", name="A", ventil_kanal=1,
                hahn_cluster="standort_a",
            ),
            ZonenKonfig(
                zone_id="zoneB", name="B", ventil_kanal=2,
                hahn_cluster="standort_a",
            ),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="standort_a", breite=52.52, laenge=13.405)]
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten",
                wetter_standort="standort_a", zonen=["zoneA", "zoneB"],
            ),
        ],
    )


def test_wirkungsrate_cluster_geschwister_blockt_event(speicher):
    """T-0150: Wenn waehrend des Eval-Fensters ein Geschwister-Event lief,
    wird der Datenpunkt verworfen (Druck-Konkurrenz)."""
    konfig = _konfig_cluster()
    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig())
    t_close = datetime(2026, 4, 27, 12, 35)
    # Sensor-Daten fuer zoneA waeren prinzipiell qualifiziert.
    _run(_baue_messung_async(speicher, t_close - timedelta(minutes=90), 30.0, zone="zoneA"))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 50.0, zone="zoneA"))
    # zoneA-Bewaesserung 90 min (11:05-12:35).
    event_a = _baue_event(t_close, "zoneA", 5400)
    # zoneB lief gleichzeitig (11:30-12:00) — Druck-Konflikt.
    event_b = _baue_event(
        t_close - timedelta(minutes=35),  # SCHLIESSEN um 12:00
        "zoneB", 1800,
    )

    neu = _run(job._scan_wirkungsrate(
        "zoneA",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event_a],
        cluster_geschwister_events=[event_b],
    ))
    assert neu == 0


def test_wirkungsrate_cluster_geschwister_ok_wenn_kein_konflikt(speicher):
    """T-0150: Geschwister-Event vor t_start (ausserhalb Fenster) blockt nichts."""
    konfig = _konfig_cluster()
    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig())
    t_close = datetime(2026, 4, 27, 12, 35)
    _run(_baue_messung_async(speicher, t_close - timedelta(minutes=90), 30.0, zone="zoneA"))
    _run(_baue_messung_async(speicher, t_close + timedelta(hours=6), 50.0, zone="zoneA"))
    event_a = _baue_event(t_close, "zoneA", 5400)
    # zoneB lief 24 h frueher — kein Konflikt.
    event_b = _baue_event(
        t_close - timedelta(hours=24), "zoneB", 1800,
    )

    neu = _run(job._scan_wirkungsrate(
        "zoneA",
        von=t_close - timedelta(days=2),
        bis=t_close + timedelta(hours=12),
        ventil_events=[event_a],
        cluster_geschwister_events=[event_b],
    ))
    assert neu == 1


def test_wirkungsrate_cluster_geschwister_map_filter_gleichkanal_aus(speicher):
    """Zonen am SELBEN Ventil-Kanal sind keine Cluster-Geschwister.

    Bambus + Yogaraum auf K2 teilen sich physisch den Lauf — kein
    Druck-Konflikt. Die Geschwister-Map muss leer sein fuer beide.
    """
    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=[
            ZonenKonfig(
                zone_id="zoneA", name="A", ventil_kanal=2,
                hahn_cluster="standort_a",
            ),
            ZonenKonfig(
                zone_id="zoneA2", name="A2", ventil_kanal=2,  # gleicher Kanal
                hahn_cluster="standort_a",
            ),
            ZonenKonfig(
                zone_id="zoneB", name="B", ventil_kanal=1,  # anderer Kanal
                hahn_cluster="standort_a",
            ),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="standort_a", breite=52.52, laenge=13.405)]
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
    )
    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig())
    geschw = job._cluster_geschwister_map()
    # zoneA und zoneA2 sehen sich gegenseitig NICHT als Geschwister
    # (gleicher Kanal). Aber beide sehen zoneB.
    assert geschw["zoneA"] == {"zoneB"}
    assert geschw["zoneA2"] == {"zoneB"}
    assert geschw["zoneB"] == {"zoneA", "zoneA2"}


def test_wirkungsrate_ohne_cluster_keine_geschwister(speicher):
    """Backward-Compat: Konfig ohne hahn_cluster -> map ist leer."""
    konfig = _konfig()  # kein hahn_cluster gesetzt
    job = KalibrationsJob(speicher, konfig, KalibrierungKonfig())
    assert job._cluster_geschwister_map() == {}


def test_t0484_zonen_gate_fragt_nach_regen_nicht_nach_ventil():
    """T-0484 Teil 2: das Gate der Kalibrierung ist fachlich, nicht technisch.

    Vorher `z.ventil_kanal is not None`. Das schloss Topf-Zonen aus, aber aus
    dem falschen Grund -- und haette sie beim Aufheben des Gates alle
    eingeschlossen. Die Kalibrierung leitet die FELDKAPAZITAET daraus ab,
    dass der Boden 12-24 h nach durchdringendem Regen gesaettigt ist; fuer
    einen ueberdachten Topf ist diese Beobachtung erfunden.

    Statischer Guard: der Scan darf nicht wieder am Ventil haengen.
    """
    import inspect

    from bewaesserung import kalibrierung

    quelle = inspect.getsource(kalibrierung.KalibrationsJob._scan_alle_zonen)
    # Nur echte Code-Zeilen: der erklaerende Kommentar darueber zitiert die
    # alte Bedingung absichtlich, und ein Guard, der am eigenen Kommentar
    # scheitert, waere ein Fehlalarm-Generator.
    code = "\n".join(
        z for z in quelle.split("\n") if not z.strip().startswith("#")
    )
    assert "if z.regen_erreicht_ballen" in code, (
        "Zonen-Gate fragt nicht mehr nach der Regen-Exposition"
    )
    assert "z.ventil_kanal is not None" not in code, (
        "Ventil-Gate ist zurueck -- damit haengt die Feldkapazitaet wieder "
        "an der Hardware statt an der Frage, ob Regen ankommt"
    )
