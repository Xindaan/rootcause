import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import (
    Ausloser,
    SensorMessung,
    VentilAktion,
    VentilEreignis,
    WetterArchivStunde,
    WetterStunde,
)
from bewaesserung.sensor_backfill import (
    SensorBackfillJob,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "sh.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _m(zeit: datetime, feuchte: float, zone: str = "bambuswald") -> SensorMessung:
    return SensorMessung(
        zeitstempel=zeit, zone_id=zone,
        boden_feuchte=feuchte, boden_temperatur=15.0, batterie_prozent=95.0,
    )


def _speichere_messungen(sp: Speicher, messungen: list[SensorMessung]) -> None:
    for m in messungen:
        _run(sp.speichere_messung(m))


def test_spike_wird_als_kandidat_erkannt(speicher):
    # Zwei Messungen 30 min auseinander: 50 % → 65 % (delta=15)
    basis = datetime(2026, 4, 19, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0),
        _m(basis + timedelta(minutes=30), 65.0),
    ])
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 1

    eingetragen = _run(speicher.hole_ventil_ereignisse("bambuswald"))
    heuristik = [e for e in eingetragen if e.ventil_id == "sensor_heuristik"]
    assert len(heuristik) == 2  # OEFFNEN + SCHLIESSEN
    assert all(e.ausloser == Ausloser.UNBEKANNT for e in heuristik)


def test_kein_trigger_bei_kleinem_delta(speicher):
    # Delta 3 (< 5) → kein Event
    basis = datetime(2026, 4, 19, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0),
        _m(basis + timedelta(minutes=30), 53.0),
    ])
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 0


def test_kein_trigger_bei_grossem_zeitabstand(speicher):
    # 15 %-Sprung, aber ueber 3 Stunden → nicht "Bewaesserung" sondern eher natuerlich
    basis = datetime(2026, 4, 19, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0),
        _m(basis + timedelta(hours=3), 65.0),
    ])
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=4)))
    assert neu == 0


def test_regen_im_fenster_verhindert_event(speicher):
    # Regen 20 mm im Fenster, Delta 15 % → Regen > 0.3 * 15 = 4.5 mm → skip
    basis = datetime(2026, 4, 19, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0),
        _m(basis + timedelta(minutes=30), 65.0),
    ])
    _run(speicher.upsert_wetter_archiv(
        [WetterArchivStunde(zeitstempel=basis, niederschlag_mm=20.0,
                             temperatur=10.0, et0_mm=0.0)],
        "standort_a",
    ))
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"], standort_default="standort_a")
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 0


def test_f13_regen_check_nutzt_zone_standort_statt_default(speicher):
    """F13: Eine Outdoor-Zone mit wetter_standort=berlin muss den Regen-Check
    gegen BERLIN fahren, nicht hartkodiert standort_a. Regen NUR im Berlin-
    Archiv (standort_a trocken) -> Sensor-Sprung wird als Regen erkannt ->
    kein Phantom-Event. Ohne Fix wuerde standort_a (0 mm) geprueft und der
    Sprung faelschlich als Bewaesserung geschrieben (neu>0)."""
    basis = datetime(2026, 4, 19, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0),
        _m(basis + timedelta(minutes=30), 65.0),
    ])
    _run(speicher.upsert_wetter_archiv(
        [WetterArchivStunde(zeitstempel=basis, niederschlag_mm=20.0,
                            temperatur=10.0, et0_mm=0.0)],
        "berlin",
    ))
    _run(speicher.upsert_wetter_archiv(
        [WetterArchivStunde(zeitstempel=basis, niederschlag_mm=0.0,
                            temperatur=10.0, et0_mm=0.0)],
        "standort_a",
    ))
    job = SensorBackfillJob(
        speicher, zone_ids=["bambuswald"],
        standort_pro_zone={"bambuswald": "berlin"},
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 0


def test_regen_ausschluss_nutzt_forecast_wenn_archiv_luecke(speicher):
    """Regression fuer Bug 19.04.2026: Heuristik-Regen-False-Positive.

    **Szenario**: Am 19.04. 15:00 beginnt der Regen. Archiv (ERA5-Latenz
    3-5 Tage) hat bereits Daten fuer 00:00-14:00 des Tages (0 mm), aber
    NOCH NICHT fuer 15:00+. Der Forecast hat korrekt 2-4 mm/h ab 15:00
    gemeldet. Der alte Filter (`if archiv: nur Archiv, sonst Forecast`)
    griff all-or-nothing: weil Archiv irgendwelche Zeilen hatte, wurde
    der Forecast ignoriert → 0 mm Regen angenommen → 3 Phantom-Events.

    **Erwartet**: Neuer Filter kombiniert per Stunde. Archiv fuer
    00:00-14:00 = 0 mm, Forecast fuer 15:00-16:00 = 3 mm (zusammen).
    Sensor-Delta 5 % im Fenster → Regen 3 mm > 0.3 × 5 mm = 1.5 mm
    → Heuristik skippt korrekt.
    """
    basis = datetime(2026, 4, 19, 14, 30)
    # Sensor-Sprung +5 pp in dem Fenster, der durch Regen erklaert wird
    _speichere_messungen(speicher, [
        _m(basis, 55.0),
        _m(basis + timedelta(hours=1), 60.0),
    ])
    # Archiv hat fruehe Stunden des Tages (0 mm), NICHT den Regen-Zeitraum
    archiv_stunden = [
        WetterArchivStunde(
            zeitstempel=datetime(2026, 4, 19, h),
            niederschlag_mm=0.0, temperatur=12.0, et0_mm=0.1,
        )
        for h in range(0, 14)
    ]
    _run(speicher.upsert_wetter_archiv(archiv_stunden, "standort_a"))
    # Forecast hat Regen ab 15:00 (3 mm)
    _run(speicher.speichere_wetter(
        abfrage_zeit=datetime(2026, 4, 19, 14, 0),
        stunden=[
            WetterStunde(
                zeitstempel=datetime(2026, 4, 19, 15),
                temperatur=13.0, niederschlag_mm=3.0, et0_mm=0.1,
            ),
        ],
        standort_id="standort_a",
    ))
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"], standort_default="standort_a")
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=3)))
    assert neu == 0, (
        "Heuristik haette den Regen aus dem Forecast-Fallback sehen muessen "
        "und den Feuchte-Sprung als Regen interpretieren"
    )


def test_live_event_im_fenster_hat_vorrang(speicher):
    # Bereits ein Live-Event +/- 30 min → Heuristik skip
    basis = datetime(2026, 4, 19, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0),
        _m(basis + timedelta(minutes=30), 65.0),
    ])
    # Live OEFFNEN-Event 15 min nach Mitte = 10:15 + 15min = 10:30
    mitte = basis + timedelta(minutes=15)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=mitte + timedelta(minutes=5),
        zone_id="bambuswald", ventil_id="live-uuid",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.AUTOMATIK,
    )))
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 0


def test_aktive_bewaesserung_auf_kanal_trennt_dswc_geraet(speicher):
    """Gleicher Kanal auf anderem DSWC darf Heuristik nicht blocken."""
    _run(speicher.setze_live_lauf_state(
        kanal=1,
        valve_id="valve-a",
        geraet_id="dswc-1",
        zone_ids=["zone-a"],
        dauer_sekunden=600,
        ausloser=Ausloser.MANUELL.value,
        gestartet_am=datetime(2026, 4, 19, 10, 0),
    ))
    job = SensorBackfillJob(
        speicher,
        zone_ids=["zone-a", "zone-b"],
        zone_zu_kanal={"zone-a": 1, "zone-b": 1},
        zone_zu_geraet={"zone-a": "dswc-1", "zone-b": "dswc-2"},
    )

    assert _run(job._aktive_bewaesserung_auf_kanal("zone-a")) is True
    assert _run(job._aktive_bewaesserung_auf_kanal("zone-b")) is False


def test_idempotent_zweiter_lauf(speicher):
    basis = datetime(2026, 4, 19, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0),
        _m(basis + timedelta(minutes=30), 65.0),
    ])
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    erst = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    # Intervall-Gate: zweiter Lauf zu frueh → 0 neue
    zweit = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2, minutes=10)))
    # Intervall ueberschreiten → Heuristik-Event vorhanden, dedup gegen eigenes Event
    dritt = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=3)))
    assert erst == 1
    assert zweit == 0
    assert dritt == 0


def test_t0283_langes_event_wird_nicht_redetektiert(speicher):
    """T-0283: Ein langes Heuristik-Event (delta=70 -> dauer 70min)
    legt OEFFNEN/SCHLIESSEN auf mitte +/- 35min -- ausserhalb des alten
    +/-30min-Dedup-Fensters um `mitte`. Vor dem Fix fand das Dedup das
    eigene Paar nicht wieder -> Re-Detektion + Duplikat in JEDEM Lauf
    (Realfall pilea 31.05.: 50 identische Phantom-Events). Nach dem Fix
    deckt das Fenster die volle Event-Spanne ab -> zweiter Lauf 0, genau
    ein Paar in der DB."""
    basis = datetime(2026, 4, 19, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 10.0),
        _m(basis + timedelta(minutes=30), 80.0),   # +70pp -> 70min-Event
    ])
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    erst = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    # Intervall ueberschreiten -> echter zweiter Scan, der das eigene
    # (lange) Event wiederfinden + dedupen muss.
    zweit = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=3)))
    assert erst == 1
    assert zweit == 0
    # Genau EIN Paar in der DB (kein Duplikat-Flood).
    ereignisse = _run(speicher.hole_ventil_ereignisse(
        "bambuswald", von=basis - timedelta(hours=2),
        bis=basis + timedelta(hours=5),
    ))
    heuristik = [e for e in ereignisse if e.ventil_id == "sensor_heuristik"]
    assert len(heuristik) == 2  # 1x OEFFNEN + 1x SCHLIESSEN


def test_mehrere_zonen_werden_getrennt_ausgewertet(speicher):
    basis = datetime(2026, 4, 19, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0, "bambuswald"),
        _m(basis + timedelta(minutes=30), 70.0, "bambuswald"),
        _m(basis, 40.0, "waldblumenhain"),
        _m(basis + timedelta(minutes=30), 42.0, "waldblumenhain"),   # nur 2 % → skip
    ])
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald", "waldblumenhain"])
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 1  # nur Bambus


def test_versickerungs_karenz_nach_ground_truth_event(speicher):
    """Regression fuer Bug 21.04.2026: Microdrip-Versickerung 1-2 h spaeter.

    Szenario: 09:41-10:01 Live-Event (20 min Microdrip), Sensor bleibt
    zunaechst unveraendert bei 60, springt erst um 11:45 auf 65
    (Wasser erreicht Sensor erst nach Versickerung). Ohne Karenz wuerde
    die Heuristik einen Phantom-Event erzeugen. Mit 3-h-Karenz nach dem
    Ground-Truth-SCHLIESSEN wird der Spike korrekt ignoriert.
    """
    basis_tag = datetime(2026, 4, 21)
    # Ground-Truth Live-Event 09:41-10:01
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis_tag.replace(hour=9, minute=41, second=33),
        zone_id="bambuswald", ventil_id="11111111-1111-1111-1111-111111111111:2",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis_tag.replace(hour=10, minute=1, second=31),
        zone_id="bambuswald", ventil_id="11111111-1111-1111-1111-111111111111:2",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1197,
        ausloser=Ausloser.MANUELL,
    )))
    # Sensor-Spike 1 h 44 min nach SCHLIESSEN - innerhalb der 3-h-Karenz
    _speichere_messungen(speicher, [
        _m(basis_tag.replace(hour=10, minute=9, second=41), 60.0),
        _m(basis_tag.replace(hour=11, minute=45, second=43), 65.0),
    ])
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    neu = _run(job.aktualisiere_wenn_faellig(
        jetzt=basis_tag.replace(hour=12, minute=30),
    ))
    assert neu == 0, "Heuristik haette den Phantom-Event durch Karenz filtern muessen"

    # Kontroll-Szenario: Spike VOR dem Live-Event → keine Karenz → Trigger OK
    # Wir loeschen die vorherigen Events und Messungen fuer sauberen Zweittest.
    # (neue Zone um Sauberkeit zu erhalten)
    basis = datetime(2026, 4, 22, 8, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0, "waldblumenhain"),
        _m(basis + timedelta(minutes=30), 65.0, "waldblumenhain"),
    ])
    job2 = SensorBackfillJob(speicher, zone_ids=["waldblumenhain"])
    neu2 = _run(job2.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu2 == 1, "ohne Karenz-Vorlaeufer sollte Heuristik normal triggern"


def test_versickerungs_karenz_ignoriert_heuristik_events(speicher):
    """Vorherige Heuristik-Events duerfen die Karenz NICHT triggern.

    Sonst kaskadiert sich ein False Positive dauerhaft, weil die Heuristik
    sich selbst als Ground-Truth interpretiert. Nur echte Quellen (Live-UUID,
    manuell, gardena_web) blockieren.
    """
    basis = datetime(2026, 4, 21, 10, 0)
    # Alter Heuristik-SCHLIESSEN liegt innerhalb der 3-h-Karenz
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis - timedelta(hours=2),
        zone_id="bambuswald", ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=300,
        ausloser=Ausloser.UNBEKANNT,
    )))
    _speichere_messungen(speicher, [
        _m(basis, 50.0),
        _m(basis + timedelta(minutes=30), 65.0),
    ])
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 1, "Heuristik-Event darf eigene Folge-Heuristik nicht blockieren"


def test_karenz_pro_zone_override_deckt_sandboden_nachlauf_ab(speicher):
    """T-0071: Waldblumenhain-Karenz=6h filtert den 23.04.-Phantom-Event.

    Szenario aus TASK/STATE: Ground-Truth-Close 12:04 (Gardena-Web-Lauf
    60 min), Sensor-Sprung 45 -> 60 erst um 16:54. Globaler Default 3 h
    ist abgelaufen (Karenz laeuft 12:04 + 3h = 15:04 aus), Heuristik
    wuerde Phantom-Event schreiben. Mit Per-Zone-Karenz=6h greift sie
    bis 18:04 - der Sprung faellt rein und wird korrekt ignoriert.
    """
    basis_tag = datetime(2026, 4, 23)
    # Ground-Truth-Lauf 11:04-12:04, ventil_id = Kanal 1 (UUID-Form)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis_tag.replace(hour=11, minute=4),
        zone_id="waldblumenhain", ventil_id="gardena_web",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis_tag.replace(hour=12, minute=4),
        zone_id="waldblumenhain", ventil_id="gardena_web",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=3600,
        ausloser=Ausloser.MANUELL,
    )))
    # Sensor-Sprung 45 -> 60 erst um 16:54 = Close+4h50min (Sandboden-Nachlauf).
    _speichere_messungen(speicher, [
        _m(basis_tag.replace(hour=16, minute=30), 45.0, zone="waldblumenhain"),
        _m(basis_tag.replace(hour=16, minute=54), 60.0, zone="waldblumenhain"),
    ])
    # Mit Karenz=6h greift der Filter, Phantom wird nicht geschrieben.
    job = SensorBackfillJob(
        speicher, zone_ids=["waldblumenhain"],
        karenz_stunden_pro_zone={"waldblumenhain": 6},
    )
    neu = _run(job.aktualisiere_wenn_faellig(
        jetzt=basis_tag.replace(hour=17, minute=30),
    ))
    assert neu == 0, (
        "Karenz=6h muss den Sandboden-Nachlauf-Sprung (Close+4h50min) "
        "als Versickerungs-Echo filtern"
    )

    # Kontroll-Szenario: identische Daten, aber Default-Karenz (3 h) → Phantom.
    basis2 = datetime(2026, 4, 24)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis2.replace(hour=11, minute=4),
        zone_id="bambuswald", ventil_id="gardena_web",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis2.replace(hour=12, minute=4),
        zone_id="bambuswald", ventil_id="gardena_web",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=3600,
        ausloser=Ausloser.MANUELL,
    )))
    _speichere_messungen(speicher, [
        _m(basis2.replace(hour=16, minute=30), 45.0, zone="bambuswald"),
        _m(basis2.replace(hour=16, minute=54), 60.0, zone="bambuswald"),
    ])
    # Kein Mapping uebergeben → Default 3h → Karenz abgelaufen, Phantom greift.
    job2 = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    neu2 = _run(job2.aktualisiere_wenn_faellig(
        jetzt=basis2.replace(hour=17, minute=30),
    ))
    assert neu2 == 1, (
        "Default-Karenz=3h muss bei Close+4h50min ausgelaufen sein — "
        "Sprung wird wie vor T-0071 als Event erkannt (Sanity-Check)"
    )


def test_fehler_in_einer_zone_blockt_nicht_die_anderen(speicher):
    basis = datetime(2026, 4, 19, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0, "bambuswald"),
        _m(basis + timedelta(minutes=30), 65.0, "bambuswald"),
    ])
    # "zone-kaputt" existiert nicht in speicher; hole_messungen gibt leer → kein Error.
    # Wir simulieren Fehler indem wir `hole_messungen` durch einen Wurf ersetzen.
    orig = speicher.hole_messungen

    async def kaputte_zone_oder_orig(zone_id, von=None, bis=None):
        if zone_id == "kaputt":
            raise RuntimeError("db-lost")
        return await orig(zone_id, von=von, bis=bis)
    speicher.hole_messungen = kaputte_zone_oder_orig  # type: ignore

    job = SensorBackfillJob(speicher, zone_ids=["kaputt", "bambuswald"])
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 1   # bambuswald hat getriggert, kaputt wurde still geloggt


# T-0114: Klassifizierte Heuristik-Events blockieren Re-Erzeugung
# auch wenn der Sensor-Sprung in den Daten weiterhin sichtbar ist.

def test_ignorierte_events_blockieren_neuerzeugung(speicher):
    """Wenn der User ein Heuristik-Event als 'ignoriert' (Regen/Glitch)
    markiert hat, darf der naechste Heuristik-Lauf den selben Sprung
    NICHT erneut als Paar schreiben — sonst kommt das Phantom zurueck.
    """
    basis = datetime(2026, 4, 19, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0),
        _m(basis + timedelta(minutes=30), 65.0),
    ])
    # Erster Lauf: Paar wird geschrieben
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    assert _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2))) == 1

    # User markiert beide Events als ignoriert (Regen/Glitch-Klick)
    events = _run(speicher.hole_ventil_ereignisse("bambuswald"))
    heuristik = [e for e in events if e.ventil_id == "sensor_heuristik"]
    for e in heuristik:
        _run(speicher.aktualisiere_ventil_ereignis(
            e.id, ausloser=Ausloser.IGNORIERT,
        ))

    # Zweiter Lauf: SOLLTE NICHT erneut schreiben
    # Mit fortgeschrittener "letzte Heuristik-Zeit" durch jetzt-Param
    job2 = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    neu = _run(job2.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=4)))
    assert neu == 0, (
        "T-0114: ignorierte Heuristik-Events muessen Re-Erzeugung blocken"
    )


def test_orphan_schliessen_blockt_neues_paar(speicher):
    """Wenn nur ein SCHLIESSEN ohne OEFFNEN existiert (Crash-Loop-
    Artefakt), darf der naechste Heuristik-Lauf nicht ein neues Paar
    schreiben — _event_im_fenster muss auch SCHLIESSEN als Treffer
    werten (T-0114).
    """
    basis = datetime(2026, 4, 19, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0),
        _m(basis + timedelta(minutes=30), 65.0),
    ])
    # Orphan-SCHLIESSEN simulieren
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis + timedelta(minutes=20),
        zone_id="bambuswald",
        ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=300,
        ausloser=Ausloser.UNBEKANNT,
    )))

    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 0, "Orphan-SCHLIESSEN muss neues Paar blocken"


def test_finde_ventil_paar_toleranz_15min_deckt_heuristik(speicher):
    """T-0114: finde_ventil_paar musste von 120s auf 900s erhoeht werden,
    sonst findet es Heuristik-Paare (5 min Differenz) nicht."""
    basis = datetime(2026, 4, 29, 10, 35, 11)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis, zone_id="waldblumenhain",
        ventil_id="sensor_heuristik",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.UNBEKANNT,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis + timedelta(minutes=5),
        zone_id="waldblumenhain", ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=300,
        ausloser=Ausloser.UNBEKANNT,
    )))
    # IDs aus DB lesen — speichere_ventil_ereignis returnt None.
    events = _run(speicher.hole_ventil_ereignisse("waldblumenhain"))
    o_event = next(e for e in events if e.aktion == VentilAktion.OEFFNEN)
    s_event = next(e for e in events if e.aktion == VentilAktion.SCHLIESSEN)
    paar = _run(speicher.finde_ventil_paar(o_event.id))
    assert s_event.id in paar, (
        "Pendant in 5-min-Distanz muss gefunden werden (war mit alter "
        "120s-Toleranz unsichtbar)"
    )


def test_finde_ventil_paar_deckt_lange_events_t0296(speicher):
    """T-0296: Laeufe > 15 min (waldblumenhain 1260s) muessen gepaart werden.
    Mit dem alten ±900s-Fenster fiel das SCHLIESSEN raus -> nach Bulk-
    Klassifikation verwaiste die SCHLIESSEN-Haelfte (unsichtbar im Banner)."""
    basis = datetime(2026, 6, 7, 17, 15, 12)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis, zone_id="waldblumenhain",
        ventil_id="sensor_heuristik", aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=0, ausloser=Ausloser.UNBEKANNT,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis + timedelta(seconds=1260),
        zone_id="waldblumenhain", ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1260,
        ausloser=Ausloser.UNBEKANNT,
    )))
    events = _run(speicher.hole_ventil_ereignisse("waldblumenhain"))
    o = next(e for e in events if e.aktion == VentilAktion.OEFFNEN)
    s = next(e for e in events if e.aktion == VentilAktion.SCHLIESSEN)
    assert s.id in _run(speicher.finde_ventil_paar(o.id)), \
        "OEFFNEN muss das 21-min-entfernte SCHLIESSEN finden"
    assert o.id in _run(speicher.finde_ventil_paar(s.id)), \
        "SCHLIESSEN muss sein OEFFNEN ueber den Start-Anker finden"


def test_finde_ventil_paar_multi_zyklus_diskriminiert_t0296(speicher):
    """T-0296: bei stuendlicher Cadence darf ein OEFFNEN NICHT das
    SCHLIESSEN eines anderen Zyklus greifen -- der Start-Anker
    (close.ts - dauer) diskriminiert pro Lauf."""
    z = "waldblumenhain"
    for start in (datetime(2026, 6, 7, 17, 15, 0), datetime(2026, 6, 7, 18, 26, 0)):
        _run(speicher.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=start, zone_id=z, ventil_id="sensor_heuristik",
            aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
            ausloser=Ausloser.UNBEKANNT,
        )))
        _run(speicher.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=start + timedelta(seconds=1260), zone_id=z,
            ventil_id="sensor_heuristik", aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=1260, ausloser=Ausloser.UNBEKANNT,
        )))
    events = _run(speicher.hole_ventil_ereignisse(z))
    oeffnen = sorted((e for e in events if e.aktion == VentilAktion.OEFFNEN),
                     key=lambda e: e.zeitstempel)
    schliessen = sorted((e for e in events if e.aktion == VentilAktion.SCHLIESSEN),
                        key=lambda e: e.zeitstempel)
    o1, o2 = oeffnen
    s1, s2 = schliessen
    paar_o1 = _run(speicher.finde_ventil_paar(o1.id))
    assert s1.id in paar_o1 and s2.id not in paar_o1, \
        "Zyklus 1 OEFFNEN -> nur Zyklus 1 SCHLIESSEN"
    paar_s2 = _run(speicher.finde_ventil_paar(s2.id))
    assert o2.id in paar_s2 and o1.id not in paar_s2, \
        "Zyklus 2 SCHLIESSEN -> nur Zyklus 2 OEFFNEN"


def test_heile_verwaiste_paar_klassifikation_t0296(speicher):
    """T-0296: verwaiste SCHLIESSEN (OEFFNEN bereits ignoriert, SCHLIESSEN
    blieb unbekannt) werden an den OEFFNEN-Ausloeser angeglichen. Idempotent;
    echte beidseitig-unbekannte Paare bleiben unberuehrt."""
    jetzt = datetime(2026, 6, 8, 9, 0, 0)
    # (1) Verwaist: OEFFNEN ignoriert, SCHLIESSEN unbekannt, 21 min auseinander.
    start = datetime(2026, 6, 7, 17, 15, 0)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=start, zone_id="waldblumenhain",
        ventil_id="sensor_heuristik", aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=0, ausloser=Ausloser.IGNORIERT,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=start + timedelta(seconds=1260), zone_id="waldblumenhain",
        ventil_id="sensor_heuristik", aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=1260, ausloser=Ausloser.UNBEKANNT,
    )))
    # (2) Echtes pending Paar: beide unbekannt -> NICHT heilen.
    start2 = datetime(2026, 6, 7, 20, 0, 0)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=start2, zone_id="kroton", ventil_id="sensor_heuristik",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.UNBEKANNT,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=start2 + timedelta(seconds=1260), zone_id="kroton",
        ventil_id="sensor_heuristik", aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=1260, ausloser=Ausloser.UNBEKANNT,
    )))

    geheilt = _run(speicher.heile_verwaiste_paar_klassifikation(jetzt=jetzt))
    assert geheilt == 1, "genau das verwaiste SCHLIESSEN muss geheilt werden"

    wb = _run(speicher.hole_ventil_ereignisse("waldblumenhain"))
    s_wb = next(e for e in wb if e.aktion == VentilAktion.SCHLIESSEN)
    assert s_wb.ausloser == Ausloser.IGNORIERT, \
        "verwaistes SCHLIESSEN auf OEFFNEN-Ausloeser (ignoriert) angeglichen"

    kr = _run(speicher.hole_ventil_ereignisse("kroton"))
    s_kr = next(e for e in kr if e.aktion == VentilAktion.SCHLIESSEN)
    assert s_kr.ausloser == Ausloser.UNBEKANNT, \
        "beidseitig-unbekanntes Paar bleibt unberuehrt"

    # Idempotenz: zweiter Lauf heilt nichts mehr.
    assert _run(speicher.heile_verwaiste_paar_klassifikation(jetzt=jetzt)) == 0


# T-0123: Heuristik darf wahrend aktivem Backend-Lauf kein Phantom schreiben.

def test_aktive_bewaesserung_blockt_phantom_event(speicher):
    """Realfall 02.05.: Live-Manuell Waldblumen lief 07:43-09:13. Sensor
    sprang waehrenddessen, Heuristik schrieb 08:31-08:41 ein Phantom-Paar.
    Mit T-0123 muss der Heuristik-Job das Live-Lauf-State-Flag respektieren.
    """
    basis = datetime(2026, 5, 2, 8, 0)
    _speichere_messungen(speicher, [
        _m(basis, 40.0, "waldblumenhain"),
        _m(basis + timedelta(minutes=30), 50.0, "waldblumenhain"),
    ])
    # Aktiver Live-Lauf auf Kanal 1 (= Waldblumen)
    _run(speicher.setze_live_lauf_state(
        kanal=1, valve_id="vid", geraet_id="gid",
        zone_ids=["waldblumenhain"], dauer_sekunden=5400,
        ausloser="manuell", gestartet_am=basis - timedelta(minutes=20),
    ))
    job = SensorBackfillJob(
        speicher, zone_ids=["waldblumenhain"],
        zone_zu_kanal={"waldblumenhain": 1},
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=1)))
    assert neu == 0, "Heuristik-Phantom waehrend Live-Lauf muss unterdrueckt sein"

    events = _run(speicher.hole_ventil_ereignisse("waldblumenhain"))
    heuristik = [e for e in events if e.ventil_id == "sensor_heuristik"]
    assert heuristik == []


def test_ohne_aktiven_lauf_schreibt_normal(speicher):
    """Sicherheitsnetz: ohne live_lauf_state laeuft die Heuristik wie
    bisher (Sprung wird als Kandidat geschrieben)."""
    basis = datetime(2026, 5, 2, 8, 0)
    _speichere_messungen(speicher, [
        _m(basis, 40.0, "waldblumenhain"),
        _m(basis + timedelta(minutes=30), 50.0, "waldblumenhain"),
    ])
    job = SensorBackfillJob(
        speicher, zone_ids=["waldblumenhain"],
        zone_zu_kanal={"waldblumenhain": 1},
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=1)))
    assert neu == 1


# T-0126: Erweiterter Regen-Lookback (Sand-Lag)

def test_t0126_regen_im_6h_lookback_blockt_phantom(speicher):
    """Realfall 05.05.: Sensor-Sprung 06:24 (+10 pp). Regen begann 04:00
    mit nur 0.1 mm — der enge 30-min-Filter sah das nicht. Sand reagiert
    mit 4-6 h Lag — 6h-Lookback faengt das."""
    basis = datetime(2026, 5, 5, 6, 0)
    _speichere_messungen(speicher, [
        _m(basis, 55.0, "waldblumenhain"),
        _m(basis + timedelta(minutes=24), 65.0, "waldblumenhain"),
    ])
    # Regen 04:00-06:00 mit 0.1, 0.4, 1.2, 0.9 mm/h (= 2.6 mm in 4h)
    archiv = [
        WetterArchivStunde(
            zeitstempel=datetime(2026, 5, 5, h),
            niederschlag_mm=mm, temperatur=12.0, et0_mm=0.1,
        )
        for h, mm in [(2, 0.1), (3, 0.4), (4, 1.2), (5, 0.9)]
    ]
    _run(speicher.upsert_wetter_archiv(archiv, "standort_a"))
    job = SensorBackfillJob(speicher, zone_ids=["waldblumenhain"], standort_default="standort_a")
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 0, "Regen-Lookback (6h) muss Phantom-Event blockieren"


def test_t0126_kein_regen_im_lookback_schreibt_normal(speicher):
    """Sicherheitsnetz: ohne Regen im 6h-Lookback verhalten wie bisher."""
    basis = datetime(2026, 5, 5, 14, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0, "waldblumenhain"),
        _m(basis + timedelta(minutes=24), 65.0, "waldblumenhain"),
    ])
    job = SensorBackfillJob(speicher, zone_ids=["waldblumenhain"])
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 1


# T-0175 Indoor-Regen-Skip ------------------------------------------------


def test_t0175_indoor_zone_skip_regen_check(speicher):
    """T-0175: Bei FYTA-Indoor (z.B. Avocado) wird der Regen-Check
    uebersprungen. Selbst wenn die DB Regen am Standort zeigt (z.B.
    weil Avocado-Wohnung am gleichen Standort liegt wie ein Outdoor-
    FYTA-Sensor), darf das den Sprung nicht blockieren — die Pflanze
    ist drinnen.
    """
    basis = datetime(2026, 5, 10, 8, 0)
    _speichere_messungen(speicher, [
        _m(basis, 30.0, "avocado"),
        _m(basis + timedelta(minutes=30), 62.0, "avocado"),
    ])
    # Kraeftiger Regen waere normalerweise Regen-Lookback-Trigger
    archiv = [
        WetterArchivStunde(
            zeitstempel=datetime(2026, 5, 10, h),
            niederschlag_mm=mm, temperatur=12.0, et0_mm=0.1,
        )
        for h, mm in [(4, 1.5), (5, 2.0), (6, 1.0), (7, 0.5)]
    ]
    _run(speicher.upsert_wetter_archiv(archiv, "berlin"))

    # Indoor-Markierung -> Regen wird ignoriert
    job = SensorBackfillJob(
        speicher,
        zone_ids=["avocado"],
        indoor_zone_ids={"avocado"},
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 1, "Indoor-Sprung muss trotz Regen am Standort als Event geschrieben werden"

    eingetragen = _run(speicher.hole_ventil_ereignisse("avocado"))
    heuristik = [e for e in eingetragen if e.ventil_id == "sensor_heuristik"]
    assert len(heuristik) == 2  # OEFFNEN + SCHLIESSEN
    assert all(e.ausloser == Ausloser.UNBEKANNT for e in heuristik)


def test_t0175_outdoor_zone_regen_check_aktiv(speicher):
    """Negativ-Kontrolle: Outdoor-Zone (kein indoor_zone_ids-Eintrag)
    bleibt vom Regen-Check geblockt — sonst koennte T-0126
    versehentlich umgangen werden.
    """
    basis = datetime(2026, 5, 5, 6, 0)
    _speichere_messungen(speicher, [
        _m(basis, 55.0, "waldblumenhain"),
        _m(basis + timedelta(minutes=24), 65.0, "waldblumenhain"),
    ])
    archiv = [
        WetterArchivStunde(
            zeitstempel=datetime(2026, 5, 5, h),
            niederschlag_mm=mm, temperatur=12.0, et0_mm=0.1,
        )
        for h, mm in [(2, 0.1), (3, 0.4), (4, 1.2), (5, 0.9)]
    ]
    _run(speicher.upsert_wetter_archiv(archiv, "standort_a"))
    # KEIN indoor_zone_ids -> Outdoor-Verhalten
    job = SensorBackfillJob(speicher, zone_ids=["waldblumenhain"], standort_default="standort_a")
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 0, "Outdoor-Regen-Lookback muss weiterhin blocken"


# --- T-0187: Pro-Zone-Heuristik-Schwelle + Roll-Up -----------------------


def test_t0187_pro_zone_schwelle_niedriger_erkennt_kleine_spruenge(speicher):
    """FYTA-Indoor-Topf: 3 pp Sprung -> mit Override 3.0 erkannt,
    obwohl globale Schwelle (5.0) nicht ueberschritten ist.
    """
    basis = datetime(2026, 5, 15, 6, 0)
    _speichere_messungen(speicher, [
        _m(basis, 70.0, "zitrus"),
        _m(basis + timedelta(minutes=15), 73.0, "zitrus"),
    ])
    job = SensorBackfillJob(
        speicher,
        zone_ids=["zitrus"],
        indoor_zone_ids={"zitrus"},
        min_delta_pp_pro_zone={"zitrus": 3.0},
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 1


def test_t0187_ohne_override_bleibt_globale_schwelle_aktiv(speicher):
    """Negativ-Kontrolle: ohne Pro-Zone-Override blockt 5 pp wie bisher
    einen 3-pp-Sprung -> kein Event.
    """
    basis = datetime(2026, 5, 15, 6, 0)
    _speichere_messungen(speicher, [
        _m(basis, 70.0, "zitrus"),
        _m(basis + timedelta(minutes=15), 73.0, "zitrus"),
    ])
    job = SensorBackfillJob(
        speicher,
        zone_ids=["zitrus"],
        indoor_zone_ids={"zitrus"},
        # kein min_delta_pp_pro_zone -> globale MIN_DELTA_PROZENT = 5.0
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 0


def test_t0187_rollup_erkennt_langsame_sicker_cadence(speicher):
    """Mandevilla-Pattern: 41 -> 43 -> 45 -> 47 -> 49 ueber 60 min,
    kein einzelner Beat ueber Beat-Schwelle (3 pp). Roll-Up (60 min /
    6 pp) erkennt den Gesamtsprung.
    """
    basis = datetime(2026, 5, 15, 6, 15)
    _speichere_messungen(speicher, [
        _m(basis, 41.0, "mandevilla"),
        _m(basis + timedelta(minutes=15), 43.0, "mandevilla"),
        _m(basis + timedelta(minutes=30), 45.0, "mandevilla"),
        _m(basis + timedelta(minutes=45), 47.0, "mandevilla"),
        _m(basis + timedelta(minutes=60), 49.0, "mandevilla"),
    ])
    job = SensorBackfillJob(
        speicher,
        zone_ids=["mandevilla"],
        indoor_zone_ids={"mandevilla"},
        min_delta_pp_pro_zone={"mandevilla": 3.0},  # Beat schweigt (2pp/Beat)
        rollup_pro_zone={"mandevilla": (60, 6.0)},
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=3)))
    assert neu == 1
    eingetragen = _run(speicher.hole_ventil_ereignisse("mandevilla"))
    heuristik = [e for e in eingetragen if e.ventil_id == "sensor_heuristik"]
    assert len(heuristik) == 2  # OEFFNEN + SCHLIESSEN


def test_t0187_rollup_ohne_konfig_kein_event(speicher):
    """Negativ-Kontrolle: gleicher 1-2-pp-Anstieg ohne Roll-Up-Konfig
    bleibt unerkannt (Backward-Compat fuer Gardena-Zonen).
    """
    basis = datetime(2026, 5, 15, 6, 15)
    _speichere_messungen(speicher, [
        _m(basis, 41.0, "mandevilla"),
        _m(basis + timedelta(minutes=15), 43.0, "mandevilla"),
        _m(basis + timedelta(minutes=30), 45.0, "mandevilla"),
        _m(basis + timedelta(minutes=45), 47.0, "mandevilla"),
        _m(basis + timedelta(minutes=60), 49.0, "mandevilla"),
    ])
    job = SensorBackfillJob(
        speicher,
        zone_ids=["mandevilla"],
        indoor_zone_ids={"mandevilla"},
        min_delta_pp_pro_zone={"mandevilla": 3.0},  # Beat schweigt
        # KEIN rollup_pro_zone -> kein Roll-Up-Pfad
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=3)))
    assert neu == 0


def test_t0187_beat_und_rollup_dedup_kein_doppel_event(speicher):
    """Wenn Beat-Logik schon ein Event geschrieben hat (z.B. 6 pp in einem
    Beat), darf Roll-Up nicht im 30-min-Dedup-Fenster nochmal triggern.
    """
    basis = datetime(2026, 5, 15, 6, 15)
    _speichere_messungen(speicher, [
        _m(basis, 68.0, "kasten_4"),
        _m(basis + timedelta(minutes=15), 74.0, "kasten_4"),  # +6 pp Beat
        _m(basis + timedelta(minutes=30), 75.0, "kasten_4"),  # Plateau
        _m(basis + timedelta(minutes=45), 75.0, "kasten_4"),
    ])
    job = SensorBackfillJob(
        speicher,
        zone_ids=["kasten_4"],
        indoor_zone_ids={"kasten_4"},
        min_delta_pp_pro_zone={"kasten_4": 3.0},
        rollup_pro_zone={"kasten_4": (60, 6.0)},
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    # Genau ein Event-Paar (Beat-Logik), Roll-Up blockt im Dedup-Fenster
    assert neu == 1
    eingetragen = _run(speicher.hole_ventil_ereignisse("kasten_4"))
    heuristik = [e for e in eingetragen if e.ventil_id == "sensor_heuristik"]
    assert len(heuristik) == 2  # nur ein OEFFNEN + SCHLIESSEN, kein Doppel


# --- T-0211a: Heuristik aussetzen waehrend ml_ausschluss_fenster --------


def test_t0211a_heuristik_pausiert_im_ausschluss_fenster(speicher):
    """Sensor-Sprung +20 pp waehrend Bodenart-Reset-Phase -> KEIN Event.

    Schuetzt vor Phantom-Mass-Aufkommen wenn der Sensor in Phase 2
    Modell-Wechsel-Spruenge produziert (T-0211a).
    """
    basis = datetime(2026, 5, 19, 6, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0, "waldblumenhain"),
        _m(basis + timedelta(minutes=30), 70.0, "waldblumenhain"),  # +20 pp!
    ])
    # Ausschluss-Fenster deckt den Sprung ab.
    job = SensorBackfillJob(
        speicher,
        zone_ids=["waldblumenhain"],
        ausschluss_fenster_pro_zone={
            "waldblumenhain": [
                (datetime(2026, 5, 18, 9, 48), datetime(2026, 5, 20, 20, 0), None),
            ],
        },
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 0, "Heuristik soll waehrend Ausschluss-Fenster aussetzen"

    eingetragen = _run(speicher.hole_ventil_ereignisse("waldblumenhain"))
    heuristik = [e for e in eingetragen if e.ventil_id == "sensor_heuristik"]
    assert heuristik == []


def test_t0211a_heuristik_laeuft_normal_ausserhalb_fenster(speicher):
    """Negativ-Kontrolle: gleicher Sprung NACH dem Fenster -> Event."""
    basis = datetime(2026, 5, 22, 6, 0)  # nach Fenster-Ende 20.05.
    _speichere_messungen(speicher, [
        _m(basis, 50.0, "waldblumenhain"),
        _m(basis + timedelta(minutes=30), 70.0, "waldblumenhain"),
    ])
    job = SensorBackfillJob(
        speicher,
        zone_ids=["waldblumenhain"],
        ausschluss_fenster_pro_zone={
            "waldblumenhain": [
                (datetime(2026, 5, 18, 9, 48), datetime(2026, 5, 20, 20, 0), None),
            ],
        },
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    # Hier sollte die normale Heuristik feuern, weil Sprung > 5 pp und
    # ausserhalb des Ausschluss-Fensters.
    assert neu == 1


def test_t0211a_mehrere_fenster_pro_zone(speicher):
    """Eine Zone kann mehrere Ausschluss-Fenster haben (z.B. mehrfache
    Resets ueber Wochen). Jedes deckt seinen Zeitraum.
    """
    basis = datetime(2026, 5, 19, 6, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0, "waldblumenhain"),
        _m(basis + timedelta(minutes=30), 70.0, "waldblumenhain"),
    ])
    job = SensorBackfillJob(
        speicher,
        zone_ids=["waldblumenhain"],
        ausschluss_fenster_pro_zone={
            "waldblumenhain": [
                (datetime(2026, 4, 15, 16, 0), datetime(2026, 4, 15, 18, 0), None),
                (datetime(2026, 5, 18, 9, 48), datetime(2026, 5, 20, 20, 0), None),
            ],
        },
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 0, "Zweites Fenster sollte greifen"


def test_t0211a_andere_zone_unbeeinflusst(speicher):
    """Ausschluss-Fenster fuer Zone A blockiert NICHT Heuristik fuer Zone B."""
    basis = datetime(2026, 5, 19, 6, 0)
    _speichere_messungen(speicher, [
        _m(basis, 50.0, "bambuswald"),
        _m(basis + timedelta(minutes=30), 70.0, "bambuswald"),
    ])
    job = SensorBackfillJob(
        speicher,
        zone_ids=["bambuswald"],
        ausschluss_fenster_pro_zone={
            # Fenster nur fuer waldblumenhain, nicht bambuswald.
            "waldblumenhain": [
                (datetime(2026, 5, 18, 9, 48), datetime(2026, 5, 20, 20, 0), None),
            ],
        },
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 1, "Bambuswald hat kein Ausschluss-Fenster, Heuristik laeuft"


# ---------------------------------------------------------------------------
# T-0205: Heuristik-Re-Check nach Event-Flip auf `ignoriert`
# ---------------------------------------------------------------------------


def test_t0205_ignorierter_event_blockiert_karenz_nicht(speicher):
    """Nach Flip auf IGNORIERT zaehlt das Event nicht mehr als Ground-
    Truth. Der zugehoerige Sensor-Sprung wird vom regulaeren Scan
    wieder als UNBEKANNT-Event geschrieben.

    Szenario: Bambus 14.05. 08:36-09:06 als manuell gespeichert, danach
    User flipt auf ignoriert. Sensor-Sprung 09:30 +20 pp (Handwerker
    haben den Schlauch abgeklemmt -> Restwasser). Vor T-0205 wuerde
    die 3h-Karenz das blockieren, weil der Manuell-Event zaehlt; mit
    T-0205 wird IGNORIERT explizit ausgeschlossen.
    """
    basis = datetime(2026, 5, 14, 9, 0)
    # Ehemals manueller Lauf 08:36-09:06, jetzt geflipt auf IGNORIERT
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis - timedelta(minutes=24),  # 08:36
        zone_id="bambuswald", ventil_id="manuell",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.IGNORIERT,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis + timedelta(minutes=6),  # 09:06
        zone_id="bambuswald", ventil_id="manuell",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1800,
        ausloser=Ausloser.IGNORIERT,
    )))
    # Sensor-Sprung 09:30 (24 min nach SCHLIESSEN, innerhalb Karenz)
    _speichere_messungen(speicher, [
        _m(basis - timedelta(minutes=24), 50.0, "bambuswald"),
        _m(basis + timedelta(minutes=30), 70.0, "bambuswald"),  # 09:30
    ])
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    neu = _run(job.aktualisiere_wenn_faellig(
        jetzt=basis + timedelta(hours=2),
    ))
    assert neu == 1, "IGNORIERT darf Karenz nicht triggern -> Heuristik schreibt"


def test_t0205_manueller_event_blockiert_karenz_weiter(speicher):
    """Negativ-Kontrolle: ein MANUELL-Event (nicht geflipt) zaehlt
    weiterhin als Ground-Truth und blockiert die Karenz.
    """
    basis = datetime(2026, 5, 14, 9, 0)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis - timedelta(minutes=24),
        zone_id="bambuswald", ventil_id="manuell",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis + timedelta(minutes=6),
        zone_id="bambuswald", ventil_id="manuell",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1800,
        ausloser=Ausloser.MANUELL,
    )))
    _speichere_messungen(speicher, [
        _m(basis - timedelta(minutes=24), 50.0, "bambuswald"),
        _m(basis + timedelta(minutes=30), 70.0, "bambuswald"),
    ])
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    neu = _run(job.aktualisiere_wenn_faellig(
        jetzt=basis + timedelta(hours=2),
    ))
    assert neu == 0, "MANUELL bleibt Ground-Truth, Karenz blockt"


def test_t0205_rescan_zone_nach_flip_schreibt_event(speicher):
    """`rescan_zone_nach_flip` scannt unabhaengig vom letzte_heuristik_
    zeit-Anker und schreibt verzoegerte Spruenge.
    """
    basis = datetime(2026, 5, 14, 9, 0)
    # Sensor-Sprung, kein Live-Event davor
    _speichere_messungen(speicher, [
        _m(basis - timedelta(minutes=30), 50.0, "bambuswald"),
        _m(basis + timedelta(minutes=30), 70.0, "bambuswald"),
    ])
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    neu = _run(job.rescan_zone_nach_flip("bambuswald", basis))
    assert neu == 1, "Re-Scan findet den Sensor-Sprung"


def test_t0205_rescan_idempotent_bei_doppelaufruf(speicher):
    """Zweite rescan_zone_nach_flip-Aufruf schreibt nichts mehr,
    weil das Event von oben schon im 30-min-Dedup-Fenster liegt."""
    basis = datetime(2026, 5, 14, 9, 0)
    _speichere_messungen(speicher, [
        _m(basis - timedelta(minutes=30), 50.0, "bambuswald"),
        _m(basis + timedelta(minutes=30), 70.0, "bambuswald"),
    ])
    job = SensorBackfillJob(speicher, zone_ids=["bambuswald"])
    _run(job.rescan_zone_nach_flip("bambuswald", basis))
    neu2 = _run(job.rescan_zone_nach_flip("bambuswald", basis))
    assert neu2 == 0, "zweiter Rescan ist idempotent (Dedup-Fenster)"


def _m_sensor(zeit: datetime, feuchte: float, sensor_id: str,
              zone: str = "waldblumenhain") -> SensorMessung:
    """T-0265-Test-Helper: Multi-Sensor-Messung mit geraet_id."""
    return SensorMessung(
        zeitstempel=zeit, zone_id=zone, geraet_id=sensor_id,
        boden_feuchte=feuchte, boden_temperatur=15.0, batterie_prozent=95.0,
    )


def test_t0265_multi_sensor_mix_erzeugt_keine_phantom_events(speicher):
    """T-0265 (2026-05-26): Realfall waldblumenhain hat 3 Sensoren mit
    stabilen aber unterschiedlichen Niveau-Werten (Gardena 35 %, FYTA-A
    57 %, FYTA-D 45 %). Die zonen-weite Iteration vor T-0265-Fix sah
    jeden Sensor-Wechsel als +12 bis +22 pp-Sprung -> Phantom-UNBEKANNT-
    Events. 24.-26.05.: 54 Phantom-Events in DB.

    Erwartetes Verhalten nach Fix: pro geraet_id getrennt iterieren,
    jeder Sensor stabil -> 0 Events.
    """
    basis = datetime(2026, 5, 26, 10, 0)
    # 6 Messungen ueber 30 min, interleaved zwischen 3 Sensoren,
    # jeder Sensor stabil auf seinem Niveau.
    _speichere_messungen(speicher, [
        # Gardena Sensor stabil 35
        _m_sensor(basis,                          35.0, "gardena_A"),
        _m_sensor(basis + timedelta(minutes=10),  35.0, "gardena_A"),
        _m_sensor(basis + timedelta(minutes=20),  35.0, "gardena_A"),
        # FYTA-A stabil 57 (15 min Cadence)
        _m_sensor(basis + timedelta(minutes=5),   57.0, "fyta_A"),
        _m_sensor(basis + timedelta(minutes=20),  57.0, "fyta_A"),
        # FYTA-D stabil 45
        _m_sensor(basis + timedelta(minutes=2),   45.0, "fyta_D"),
        _m_sensor(basis + timedelta(minutes=17),  45.0, "fyta_D"),
    ])

    job = SensorBackfillJob(speicher, zone_ids=["waldblumenhain"])
    neu = _run(job.aktualisiere_wenn_faellig(
        jetzt=basis + timedelta(hours=2),
    ))
    assert neu == 0, (
        f"T-0265: Multi-Sensor-Mix darf KEINE Phantom-Events erzeugen "
        f"wenn jeder Sensor stabil ist. Erzeugt: {neu}"
    )


def test_t0386_geraet_scoped_fenster_ueberspringt_nur_diesen_sensor(speicher):
    """T-0386: Ein geraet-scoped ml_ausschluss_fenster pausiert NUR diesen
    Sensor -- der gesunde Nachbarsensor derselben Zone wird weiter erkannt.
    Vor T-0386 (geraet_id verworfen) pausierte es die GANZE Zone -> beide
    Spruenge unterdrueckt. Jetzt: fyta_A ausgeschlossen -> nur gardena_A."""
    basis = datetime(2026, 5, 26, 10, 0)
    _speichere_messungen(speicher, [
        _m_sensor(basis,                          50.0, "gardena_A"),
        _m_sensor(basis + timedelta(minutes=30),  70.0, "gardena_A"),  # +20
        _m_sensor(basis,                          40.0, "fyta_A"),
        _m_sensor(basis + timedelta(minutes=30),  60.0, "fyta_A"),     # +20
    ])
    jetzt = basis + timedelta(hours=2)
    job = SensorBackfillJob(
        speicher,
        zone_ids=["waldblumenhain"],
        ausschluss_fenster_pro_zone={
            "waldblumenhain": [
                # geraet-scoped auf fyta_A, deckt den Job-Zeitpunkt ab.
                (basis - timedelta(hours=1), jetzt + timedelta(hours=1), "fyta_A"),
            ],
        },
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=jetzt))
    # Ohne Ausschluss waeren es 2 (beide Sensoren). Der geraet-scoped
    # Ausschluss nimmt NUR fyta_A raus -> genau 1 (gardena_A).
    assert neu == 1


def test_t0386_zone_weites_fenster_ueberspringt_alle_sensoren(speicher):
    """T-0386-Gegenprobe: ein Fenster OHNE geraet_id (None) pausiert weiter die
    ganze Zone -- beide Sensor-Spruenge unterdrueckt (Backward-Compat)."""
    basis = datetime(2026, 5, 26, 10, 0)
    _speichere_messungen(speicher, [
        _m_sensor(basis,                          50.0, "gardena_A"),
        _m_sensor(basis + timedelta(minutes=30),  70.0, "gardena_A"),
        _m_sensor(basis,                          40.0, "fyta_A"),
        _m_sensor(basis + timedelta(minutes=30),  60.0, "fyta_A"),
    ])
    jetzt = basis + timedelta(hours=2)
    job = SensorBackfillJob(
        speicher,
        zone_ids=["waldblumenhain"],
        ausschluss_fenster_pro_zone={
            "waldblumenhain": [
                (basis - timedelta(hours=1), jetzt + timedelta(hours=1), None),
            ],
        },
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=jetzt))
    assert neu == 0


def test_f12_rescan_nach_flip_multi_sensor_keine_phantom_events(speicher):
    """F12: rescan_zone_nach_flip mischte Multi-Sensor-Zonen (der T-0265-Fix
    war nur in _pruefe_zone). Stabile Sensoren auf verschiedenen Niveaus
    (Gardena 35 / FYTA-A 57 / FYTA-D 45) duerfen beim Rescan KEINE Phantom-
    UNBEKANNT-Events erzeugen -- sonst genau die, die der User per Flip gerade
    weggeraeumt hat (Banner-Loop)."""
    basis = datetime(2026, 5, 26, 10, 0)
    _speichere_messungen(speicher, [
        _m_sensor(basis,                          35.0, "gardena_A"),
        _m_sensor(basis + timedelta(minutes=10),  35.0, "gardena_A"),
        _m_sensor(basis + timedelta(minutes=20),  35.0, "gardena_A"),
        _m_sensor(basis + timedelta(minutes=5),   57.0, "fyta_A"),
        _m_sensor(basis + timedelta(minutes=20),  57.0, "fyta_A"),
        _m_sensor(basis + timedelta(minutes=2),   45.0, "fyta_D"),
        _m_sensor(basis + timedelta(minutes=17),  45.0, "fyta_D"),
    ])
    job = SensorBackfillJob(speicher, zone_ids=["waldblumenhain"])
    neu = _run(job.rescan_zone_nach_flip("waldblumenhain", basis))
    assert neu == 0, (
        f"F12: Multi-Sensor-Mix darf beim Rescan keine Phantom-Events "
        f"erzeugen. Erzeugt: {neu}"
    )


def test_t0265_echter_sprung_auf_einem_sensor_wird_erkannt(speicher):
    """T-0265 Sanity: wenn EIN Sensor in einer Multi-Sensor-Zone einen
    echten Bewaesserungs-Sprung zeigt, muss er weiterhin als unbekannt-
    Event erkannt werden (sonst tot, der Detektor).
    """
    basis = datetime(2026, 5, 26, 10, 0)
    _speichere_messungen(speicher, [
        # Gardena Sensor: echter Sprung 35 -> 55 (Bewaesserung)
        _m_sensor(basis,                          35.0, "gardena_A"),
        _m_sensor(basis + timedelta(minutes=30),  55.0, "gardena_A"),
        # FYTA-A stabil 57 -- soll Gardena-Sprung NICHT blocken
        _m_sensor(basis + timedelta(minutes=5),   57.0, "fyta_A"),
        _m_sensor(basis + timedelta(minutes=25),  57.0, "fyta_A"),
    ])

    job = SensorBackfillJob(speicher, zone_ids=["waldblumenhain"])
    neu = _run(job.aktualisiere_wenn_faellig(
        jetzt=basis + timedelta(hours=2),
    ))
    assert neu == 1, (
        f"Echter Sprung auf einem Sensor (gardena_A 35->55) muss "
        f"weiter als unbekannt-Event geschrieben werden. Erzeugt: {neu}"
    )


# --- T-0312: Cross-Spray-Guard -------------------------------------------

def _mw_lauf(speicher, zeit, ausloser=Ausloser.IGNORIERT):
    """Echter Magerwiesenkanal-Lauf (Viereckregner) -- ventil_id != heuristik.
    Realistisch ausloeser=ignoriert (T-0300-Auto-Flip), zaehlt trotzdem als
    physischer Lauf fuer den Cross-Spray-Guard."""
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=zeit, zone_id="magerwiese", ventil_id="dswc1:1",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0, ausloser=ausloser,
    )))


def test_t0312_cross_spray_wird_nicht_geschrieben(speicher):
    """Hecke-Sprung waehrend magerwiese-Lauf OHNE eigenen Heckenlauf ->
    KEIN Hecke-Event (Cross-Spray des Viereckregners auf den Sensor)."""
    basis = datetime(2026, 6, 20, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 45.0, "hecke"),
        _m(basis + timedelta(minutes=30), 60.0, "hecke"),
    ])
    _mw_lauf(speicher, basis + timedelta(minutes=5))
    job = SensorBackfillJob(
        speicher, zone_ids=["hecke"],
        cross_spray_quellen={"hecke": ["magerwiese"]},
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 0
    heuristik = [
        e for e in _run(speicher.hole_ventil_ereignisse("hecke"))
        if e.ventil_id == "sensor_heuristik"
    ]
    assert heuristik == []


def test_t0312_ohne_quell_lauf_normaler_phantom(speicher):
    """Cross-Spray konfiguriert, aber KEIN magerwiese-Lauf im Fenster ->
    Sprung zaehlt normal weiter (Guard feuert nicht spurious)."""
    basis = datetime(2026, 6, 20, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 45.0, "hecke"),
        _m(basis + timedelta(minutes=30), 60.0, "hecke"),
    ])
    job = SensorBackfillJob(
        speicher, zone_ids=["hecke"],
        cross_spray_quellen={"hecke": ["magerwiese"]},
    )
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 1


def test_t0312_ohne_config_kein_guard(speicher):
    """Backward-Compat: Zone ohne cross_spray_quell_zonen wertet den Sprung
    normal, auch wenn magerwiese lief (Guard nur fuer konfigurierte Zonen)."""
    basis = datetime(2026, 6, 20, 10, 0)
    _speichere_messungen(speicher, [
        _m(basis, 45.0, "hecke"),
        _m(basis + timedelta(minutes=30), 60.0, "hecke"),
    ])
    _mw_lauf(speicher, basis + timedelta(minutes=5))
    job = SensorBackfillJob(speicher, zone_ids=["hecke"])
    neu = _run(job.aktualisiere_wenn_faellig(jetzt=basis + timedelta(hours=2)))
    assert neu == 1
