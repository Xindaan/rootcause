from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import DatenQuelle, SensorMessung, SensorWarnungTyp
from bewaesserung.sensor_health import SensorHealthMonitor
from bewaesserung.speicher import Speicher


@pytest.fixture
async def speicher(tmp_path):
    db_pfad = tmp_path / "sensor_health.db"
    speicher = Speicher(str(db_pfad))
    await speicher.verbinden()
    try:
        yield speicher
    finally:
        await speicher.schliessen()


async def _speichere_messung(
    speicher: Speicher,
    *,
    zone_id: str,
    zeitstempel: datetime,
    boden_feuchte: float = 42.0,
    batterie_prozent: float | None = 80.0,
    quelle: DatenQuelle = DatenQuelle.GARDENA,
) -> None:
    await speicher.speichere_messung(
        SensorMessung(
            zeitstempel=zeitstempel,
            zone_id=zone_id,
            geraet_id=f"sensor-{zone_id}",
            boden_feuchte=boden_feuchte,
            batterie_prozent=batterie_prozent,
            quelle=quelle,
        )
    )


@pytest.mark.asyncio
async def test_ops_schema_migration_legt_neue_spalten_und_sensor_warnung_an(speicher: Speicher):
    assert speicher._db is not None

    async with speicher._db.execute("PRAGMA table_info(entscheidung_log)") as cursor:
        spalten = {zeile["name"] async for zeile in cursor}

    async with speicher._db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sensor_warnung'"
    ) as cursor:
        tabelle = await cursor.fetchone()

    assert {"blocker_typ", "scope", "scope_ref"} <= spalten
    assert tabelle is not None


@pytest.mark.asyncio
async def test_pruefe_alle_oeffnet_neue_ausfall_warnung(speicher: Speicher):
    monitor = SensorHealthMonitor(speicher)

    warnungen = await monitor.pruefe_alle(["rasen"])
    offen = await speicher.offene_sensor_warnungen("rasen")

    # T-0526: die Zone hat kein einziges Geraet -> Zonen-Warnung mit leerer
    # geraet_id. Nur dieser Fall bleibt zonenweit.
    assert warnungen == [
        {
            "zone_id": "rasen",
            "typ": SensorWarnungTyp.AUSFALL.value,
            "details": "Noch nie Daten empfangen",
            "geraet_id": "",
        }
    ]
    assert len(offen) == 1
    assert offen[0].typ == SensorWarnungTyp.AUSFALL
    assert offen[0].behoben_um is None


@pytest.mark.asyncio
async def test_pruefe_alle_legt_bei_gleichem_problem_keine_duplikate_an(speicher: Speicher):
    monitor = SensorHealthMonitor(speicher)

    erste = await monitor.pruefe_alle(["rasen"])
    zweite = await monitor.pruefe_alle(["rasen"])
    offen = await speicher.offene_sensor_warnungen("rasen")
    historie = await speicher.hole_sensor_warnungen(zone_id="rasen")

    assert len(erste) == 1
    assert zweite == []
    assert len(offen) == 1
    assert len(historie) == 1


@pytest.mark.asyncio
async def test_offene_warn_details_werden_aufgefrischt(speicher: Speicher):
    """T-0397 (F4): eine offene Warnung behaelt `details` NICHT eingefroren.
    Vorher zeigte die Ausfall-Warnung tagelang die 'vor Xh'-Angabe vom
    Erstellzeitpunkt; der Health-Tick frischt sie jetzt bei jedem Lauf auf.
    Erstellzeit + behoben_um bleiben unveraendert."""
    from bewaesserung.modelle import SensorWarnung
    t0 = datetime(2026, 7, 8, 22, 46)
    await speicher.oeffne_sensor_warnung(SensorWarnung(
        zeitstempel=t0, zone_id="rasen",
        typ=SensorWarnungTyp.AUSFALL,
        details="Letztes Update vor 12.0h (08.07. 22:46)",
    ))
    geaendert = await speicher.aktualisiere_offene_warn_details(
        "rasen", SensorWarnungTyp.AUSFALL,
        "Letztes Update vor 56.0h (08.07. 22:46)",
    )
    offen = await speicher.offene_sensor_warnungen("rasen")
    assert geaendert is True
    assert len(offen) == 1
    assert "56.0h" in offen[0].details
    assert offen[0].zeitstempel == t0
    assert offen[0].behoben_um is None


@pytest.mark.asyncio
async def test_offene_warn_details_ruehrt_behobene_nicht_an(speicher: Speicher):
    """Nur OFFENE Warnungen werden aufgefrischt -- eine behobene bleibt, wie
    sie war (der WHERE-Guard behoben_um IS NULL)."""
    from bewaesserung.modelle import SensorWarnung
    await speicher.oeffne_sensor_warnung(SensorWarnung(
        zeitstempel=datetime(2026, 7, 8, 22, 46), zone_id="rasen",
        typ=SensorWarnungTyp.AUSFALL, details="alt",
    ))
    await speicher.schliesse_sensor_warnung(
        "rasen", SensorWarnungTyp.AUSFALL, datetime(2026, 7, 9, 0, 0),
    )
    await speicher.aktualisiere_offene_warn_details(
        "rasen", SensorWarnungTyp.AUSFALL, "neu",
    )
    historie = await speicher.hole_sensor_warnungen(zone_id="rasen")
    assert len(historie) == 1
    assert historie[0].details == "alt"
    assert historie[0].behoben_um is not None


@pytest.mark.asyncio
async def test_pruefe_alle_setzt_behoben_um_nach_erholung(speicher: Speicher):
    monitor = SensorHealthMonitor(speicher)
    zone_id = "rasen"

    await _speichere_messung(
        speicher,
        zone_id=zone_id,
        zeitstempel=datetime.now() - timedelta(hours=4),
    )
    await monitor.pruefe_alle([zone_id])

    await _speichere_messung(
        speicher,
        zone_id=zone_id,
        zeitstempel=datetime.now(),
    )
    warnungen = await monitor.pruefe_alle([zone_id])
    offen = await speicher.offene_sensor_warnungen(zone_id)
    historie = await speicher.hole_sensor_warnungen(zone_id=zone_id)

    assert warnungen == []
    assert offen == []
    assert len(historie) == 1
    assert historie[0].typ == SensorWarnungTyp.AUSFALL
    assert historie[0].behoben_um is not None


@pytest.mark.asyncio
async def test_pruefe_alle_eskaliert_batterie_von_niedrig_zu_kritisch(speicher: Speicher):
    monitor = SensorHealthMonitor(speicher)
    zone_id = "rasen"

    await _speichere_messung(
        speicher,
        zone_id=zone_id,
        zeitstempel=datetime.now(),
        batterie_prozent=15.0,
    )
    erste_warnungen = await monitor.pruefe_alle([zone_id])

    await _speichere_messung(
        speicher,
        zone_id=zone_id,
        zeitstempel=datetime.now() + timedelta(minutes=1),
        batterie_prozent=5.0,
    )
    zweite_warnungen = await monitor.pruefe_alle([zone_id])

    offen = await speicher.offene_sensor_warnungen(zone_id)
    historie = await speicher.hole_sensor_warnungen(zone_id=zone_id)
    niedrig = next(w for w in historie if w.typ == SensorWarnungTyp.BATTERIE_NIEDRIG)
    kritisch = next(w for w in historie if w.typ == SensorWarnungTyp.BATTERIE_KRITISCH)

    # T-0526: Warnungen sind jetzt geraetescharf -- die geraet_id steht im
    # Rueckgabe-Dict und im Text, damit bei mehreren Sensoren pro Zone
    # erkennbar bleibt, WELCHER gemeint ist.
    assert erste_warnungen == [
        {
            "zone_id": zone_id,
            "typ": SensorWarnungTyp.BATTERIE_NIEDRIG.value,
            "details": "sensor-rasen: Batterie 15%",
            "geraet_id": "sensor-rasen",
        }
    ]
    assert zweite_warnungen == [
        {
            "zone_id": zone_id,
            "typ": SensorWarnungTyp.BATTERIE_KRITISCH.value,
            "details": "sensor-rasen: Batterie 5% \u2014 Sofort wechseln!",
            "geraet_id": "sensor-rasen",
        }
    ]
    assert [warnung.typ for warnung in offen] == [SensorWarnungTyp.BATTERIE_KRITISCH]
    assert niedrig.behoben_um is not None
    assert kritisch.behoben_um is None


@pytest.mark.asyncio
async def test_fyta_sensor_hat_laengeren_stale_timeout(speicher: Speicher):
    monitor = SensorHealthMonitor(speicher)
    zone_id = "pilea"

    await _speichere_messung(
        speicher,
        zone_id=zone_id,
        zeitstempel=datetime.now() - timedelta(hours=8),
        batterie_prozent=None,
        quelle=DatenQuelle.FYTA,
    )

    warnungen = await monitor.pruefe_alle([zone_id])
    offen = await speicher.offene_sensor_warnungen(zone_id)

    assert warnungen == []
    assert offen == []


@pytest.mark.asyncio
async def test_fyta_sensor_wird_erst_nach_zwolf_stunden_als_ausfall_markiert(speicher: Speicher):
    monitor = SensorHealthMonitor(speicher)
    zone_id = "pilea"

    await _speichere_messung(
        speicher,
        zone_id=zone_id,
        zeitstempel=datetime.now() - timedelta(hours=13),
        batterie_prozent=None,
        quelle=DatenQuelle.FYTA,
    )

    warnungen = await monitor.pruefe_alle([zone_id])
    offen = await speicher.offene_sensor_warnungen(zone_id)

    assert len(warnungen) == 1
    assert warnungen[0]["typ"] == SensorWarnungTyp.AUSFALL.value
    assert len(offen) == 1
    assert offen[0].typ == SensorWarnungTyp.AUSFALL


@pytest.mark.asyncio
async def test_t0214_pro_zone_ausfall_schwelle_blockt_fruehe_warnung(speicher):
    """T-0214: pro-Zone-override (96h) verhindert dass FYTA-Bluetooth-
    Only-Pflanze nach 13 h als 'ausfall' markiert wird."""
    monitor = SensorHealthMonitor(
        speicher,
        ausfall_schwelle_pro_zone={"pilea": 96},
    )
    await _speichere_messung(
        speicher,
        zone_id="pilea",
        zeitstempel=datetime.now() - timedelta(hours=13),
        batterie_prozent=None,
        quelle=DatenQuelle.FYTA,
    )
    warnungen = await monitor.pruefe_alle(["pilea"])
    assert warnungen == []  # 13h < 96h Override
    assert await speicher.offene_sensor_warnungen("pilea") == []


@pytest.mark.asyncio
async def test_t0214_pro_zone_ausfall_schwelle_triggert_nach_5_tagen(speicher):
    """T-0214: nach 5 Tagen ohne Sync triggert auch der 96h-Override."""
    monitor = SensorHealthMonitor(
        speicher,
        ausfall_schwelle_pro_zone={"mandevilla_maxi": 96},
    )
    await _speichere_messung(
        speicher,
        zone_id="mandevilla_maxi",
        zeitstempel=datetime.now() - timedelta(hours=120),
        batterie_prozent=None,
        quelle=DatenQuelle.FYTA,
    )
    warnungen = await monitor.pruefe_alle(["mandevilla_maxi"])
    assert len(warnungen) == 1
    assert warnungen[0]["typ"] == SensorWarnungTyp.AUSFALL.value


@pytest.mark.asyncio
async def test_t0214_ohne_override_default_schwellen(speicher):
    """Ohne override greifen die globalen Defaults (3h Gardena, 12h FYTA)."""
    monitor = SensorHealthMonitor(speicher)  # leeres override-dict
    await _speichere_messung(
        speicher, zone_id="bambus",
        zeitstempel=datetime.now() - timedelta(hours=4),  # > 3h Gardena-Default
        batterie_prozent=80,
        quelle=DatenQuelle.GARDENA,
    )
    warnungen = await monitor.pruefe_alle(["bambus"])
    assert len(warnungen) == 1
    assert warnungen[0]["typ"] == SensorWarnungTyp.AUSFALL.value


@pytest.mark.asyncio
async def test_t0445_keine_ankunft_warnung_wird_bei_neuer_ankunft_geschlossen(
    speicher: Speicher,
):
    """Auto-Resolve fuer `KEINE_ANKUNFT_IM_LAUF`.

    Die Warnung entsteht im Entscheidungsmotor beim Frische-Stop und hatte
    dort keinen Gegenpart -- ohne Schliesser bliebe nach dem ersten Stop
    dauerhaft ein KRITISCH-Eintrag im Default-Ops-Feed stehen.
    """
    from bewaesserung.modelle import SensorWarnung

    jetzt = datetime.now()
    await speicher.oeffne_sensor_warnung(SensorWarnung(
        zeitstempel=jetzt - timedelta(minutes=30),
        zone_id="zone_a",
        typ=SensorWarnungTyp.KEINE_ANKUNFT_IM_LAUF,
        details="kein neu eingetroffener Messwert seit Laufbeginn",
    ))
    # Ankunft NACH dem Warnungs-Zeitstempel (speichere_messung setzt
    # `empfangen_am` auf jetzt).
    await _speichere_messung(
        speicher, zone_id="zone_a", zeitstempel=jetzt - timedelta(minutes=5),
    )

    monitor = SensorHealthMonitor(speicher)
    await monitor.pruefe_alle(["zone_a"])

    offen = await speicher.offene_sensor_warnungen("zone_a")
    assert not [
        w for w in offen if w.typ == SensorWarnungTyp.KEINE_ANKUNFT_IM_LAUF
    ]


@pytest.mark.asyncio
async def test_t0445_keine_ankunft_warnung_bleibt_ohne_neue_ankunft_offen(
    speicher: Speicher,
):
    """Gegenprobe: kommt nichts an, bleibt die Warnung stehen.

    Die Messung ist hier AELTER als die Warnung angelegt (Ankunft vor dem
    Warnungs-Zeitstempel) -- genau der Fall, den die Warnung beschreibt.
    """
    from bewaesserung.modelle import SensorWarnung

    jetzt = datetime.now()
    await _speichere_messung(
        speicher, zone_id="zone_a", zeitstempel=jetzt - timedelta(minutes=90),
    )
    await speicher.oeffne_sensor_warnung(SensorWarnung(
        zeitstempel=jetzt + timedelta(minutes=5),
        zone_id="zone_a",
        typ=SensorWarnungTyp.KEINE_ANKUNFT_IM_LAUF,
        details="kein neu eingetroffener Messwert seit Laufbeginn",
    ))

    monitor = SensorHealthMonitor(speicher)
    await monitor.pruefe_alle(["zone_a"])

    offen = await speicher.offene_sensor_warnungen("zone_a")
    assert [
        w for w in offen if w.typ == SensorWarnungTyp.KEINE_ANKUNFT_IM_LAUF
    ]


@pytest.mark.asyncio
async def test_t0445_auto_resolve_beruehrt_andere_warnungstypen_nicht(
    speicher: Speicher,
):
    """Der Schliesser ist typ-scharf -- eine offene Ausfall-Warnung derselben
    Zone darf er nicht mitnehmen."""
    from bewaesserung.modelle import SensorWarnung

    jetzt = datetime.now()
    await speicher.oeffne_sensor_warnung(SensorWarnung(
        zeitstempel=jetzt - timedelta(minutes=30),
        zone_id="zone_a",
        typ=SensorWarnungTyp.KEINE_ANKUNFT_IM_LAUF,
        details="x",
    ))
    await speicher.oeffne_sensor_warnung(SensorWarnung(
        zeitstempel=jetzt - timedelta(minutes=30),
        zone_id="zone_a",
        typ=SensorWarnungTyp.BATTERIE_NIEDRIG,
        details="y",
    ))
    # Batterie bleibt unter der Warnschwelle (20 %), die Batterie-Warnung
    # muss also offen bleiben.
    await _speichere_messung(
        speicher, zone_id="zone_a", zeitstempel=jetzt - timedelta(minutes=5),
        batterie_prozent=15.0,
    )

    monitor = SensorHealthMonitor(speicher)
    await monitor.pruefe_alle(["zone_a"])

    typen = {w.typ for w in await speicher.offene_sensor_warnungen("zone_a")}
    assert SensorWarnungTyp.KEINE_ANKUNFT_IM_LAUF not in typen
    assert SensorWarnungTyp.BATTERIE_NIEDRIG in typen


# --- T-0526: Ausfall pro Geraet statt pro Zone -------------------------------


async def _messung_geraet(
    speicher: Speicher, *, zone_id: str, geraet_id: str,
    zeitstempel: datetime, batterie_prozent: float | None = None,
    quelle: DatenQuelle = DatenQuelle.FYTA,
) -> None:
    await speicher.speichere_messung(
        SensorMessung(
            zeitstempel=zeitstempel, zone_id=zone_id, geraet_id=geraet_id,
            boden_feuchte=30.0, batterie_prozent=batterie_prozent,
            quelle=quelle,
        )
    )


@pytest.mark.asyncio
async def test_toter_sensor_faellt_auf_obwohl_nachbar_in_derselben_zone_liefert(
    speicher: Speicher,
):
    """Der Faulbaum-Fall (08.08.2026), als roter Test.

    Zone `hecke` hat mehrere Sensoren. Einer ist seit vier Tagen stumm, die
    anderen liefern weiter. Vor T-0526 hielt `letzte_messung(zone_id)` die
    Zone frisch und es entstand KEINE Warnung -- der Ausfall lief vier Tage
    unbemerkt.
    """
    jetzt = datetime.now()
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_100004",
        zeitstempel=jetzt - timedelta(days=4),
    )
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_100001",
        zeitstempel=jetzt - timedelta(minutes=10),
    )
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="gardena_44444444",
        zeitstempel=jetzt - timedelta(minutes=5),
        quelle=DatenQuelle.GARDENA,
    )

    monitor = SensorHealthMonitor(speicher)
    warnungen = await monitor.pruefe_alle(["hecke"])

    ausfaelle = [w for w in warnungen if w["typ"] == SensorWarnungTyp.AUSFALL.value]
    assert len(ausfaelle) == 1
    assert ausfaelle[0]["geraet_id"] == "fyta_100004"
    assert "fyta_100004" in ausfaelle[0]["details"]

    offen = await speicher.offene_sensor_warnungen("hecke")
    offene_ausfaelle = [w for w in offen if w.typ == SensorWarnungTyp.AUSFALL]
    assert [w.geraet_id for w in offene_ausfaelle] == ["fyta_100004"]


@pytest.mark.asyncio
async def test_zwei_tote_sensoren_erzeugen_zwei_warnungen(speicher: Speicher):
    """Vorher unmoeglich: der Offen-Schluessel (zone_id, typ) liess nur EINE
    Ausfall-Warnung pro Zone zu, die zweite wurde als Duplikat verworfen."""
    jetzt = datetime.now()
    for geraet in ("fyta_100004", "fyta_100001"):
        await _messung_geraet(
            speicher, zone_id="hecke", geraet_id=geraet,
            zeitstempel=jetzt - timedelta(days=3),
        )

    monitor = SensorHealthMonitor(speicher)
    await monitor.pruefe_alle(["hecke"])

    offen = await speicher.offene_sensor_warnungen("hecke")
    ausfaelle = sorted(
        w.geraet_id for w in offen if w.typ == SensorWarnungTyp.AUSFALL
    )
    assert ausfaelle == ["fyta_100001", "fyta_100004"]


@pytest.mark.asyncio
async def test_lebender_sensor_schliesst_die_warnung_des_toten_nicht(
    speicher: Speicher,
):
    """Der eigentliche Mechanismus des Bugs: ein zonenweiter Schliesser hat
    die Warnung des toten Nachbarn mit weggeraeumt."""
    jetzt = datetime.now()
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_100004",
        zeitstempel=jetzt - timedelta(days=3),
    )
    monitor = SensorHealthMonitor(speicher)
    await monitor.pruefe_alle(["hecke"])

    # Der Nachbar kommt (wieder) und liefert frisch.
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_100001",
        zeitstempel=jetzt - timedelta(minutes=2),
    )
    await monitor.pruefe_alle(["hecke"])

    offen = await speicher.offene_sensor_warnungen("hecke")
    ausfaelle = [w for w in offen if w.typ == SensorWarnungTyp.AUSFALL]
    assert [w.geraet_id for w in ausfaelle] == ["fyta_100004"]


@pytest.mark.asyncio
async def test_wiederbelebter_sensor_schliesst_nur_seine_eigene_warnung(
    speicher: Speicher,
):
    jetzt = datetime.now()
    for geraet in ("fyta_100004", "fyta_100001"):
        await _messung_geraet(
            speicher, zone_id="hecke", geraet_id=geraet,
            zeitstempel=jetzt - timedelta(days=3),
        )
    monitor = SensorHealthMonitor(speicher)
    await monitor.pruefe_alle(["hecke"])

    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_100001",
        zeitstempel=jetzt,
    )
    await monitor.pruefe_alle(["hecke"])

    offen = await speicher.offene_sensor_warnungen("hecke")
    ausfaelle = [w for w in offen if w.typ == SensorWarnungTyp.AUSFALL]
    assert [w.geraet_id for w in ausfaelle] == ["fyta_100004"]


@pytest.mark.asyncio
async def test_geraet_ausserhalb_des_erwartet_fensters_faellt_aus_der_ueberwachung(
    speicher: Speicher,
):
    """Ausgebaute Sensoren duerfen nicht ewig eine Warnung tragen
    (Signal-zu-Noise, gleiche Ueberlegung wie T-0214)."""
    from bewaesserung.sensor_health import GERAET_ERWARTET_TAGE

    jetzt = datetime.now()
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_alt",
        zeitstempel=jetzt - timedelta(days=GERAET_ERWARTET_TAGE + 5),
    )

    monitor = SensorHealthMonitor(speicher)
    warnungen = await monitor.pruefe_alle(["hecke"])

    # Zonen-Warnung (geraet_id ''), nicht eine dauerhaft offene Geraete-Warnung
    assert len(warnungen) == 1
    assert warnungen[0]["geraet_id"] == ""
    assert "ausserhalb" in warnungen[0]["details"]


@pytest.mark.asyncio
async def test_alte_zonenweite_ausfallwarnung_wird_geschlossen(speicher: Speicher):
    """Migrations-Pfad: eine vor T-0526 entstandene Warnung hat geraet_id ''
    und wuerde von den Geraete-Pfaden nie wieder angefasst."""
    from bewaesserung.modelle import SensorWarnung

    jetzt = datetime.now()
    await speicher.oeffne_sensor_warnung(SensorWarnung(
        zeitstempel=jetzt - timedelta(days=2),
        zone_id="hecke",
        typ=SensorWarnungTyp.AUSFALL,
        details="Letztes Update vor 50.0h (06.08. 12:00)",
    ))
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_100001", zeitstempel=jetzt,
    )

    monitor = SensorHealthMonitor(speicher)
    await monitor.pruefe_alle(["hecke"])

    offen = await speicher.offene_sensor_warnungen("hecke")
    assert [w for w in offen if w.typ == SensorWarnungTyp.AUSFALL] == []


@pytest.mark.asyncio
async def test_fyta_batteriewert_kommt_aus_dem_geraetestatus(speicher: Speicher):
    """T-0527: FYTA-Messungen fuehren kein Batteriefeld. Vor dieser Aenderung
    fielen FYTA-Sensoren still aus der Batterie-Ueberwachung."""
    from bewaesserung.modelle import FytaGeraeteStatus

    jetzt = datetime.now()
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_100001",
        zeitstempel=jetzt, batterie_prozent=None,
    )
    await speicher.speichere_fyta_geraete_status(FytaGeraeteStatus(
        zeitstempel=jetzt, geraet_id="fyta_100001", plant_id=100001,
        sensor_id="mac-sensor-2", zone_id="hecke", battery_level=8.0,
    ))

    monitor = SensorHealthMonitor(speicher)
    warnungen = await monitor.pruefe_alle(["hecke"])

    kritisch = [
        w for w in warnungen
        if w["typ"] == SensorWarnungTyp.BATTERIE_KRITISCH.value
    ]
    assert len(kritisch) == 1
    assert kritisch[0]["geraet_id"] == "fyta_100001"
    assert "8%" in kritisch[0]["details"]


@pytest.mark.asyncio
async def test_migration_auf_bestands_db_ohne_geraet_id_spalte(tmp_path):
    """T-0526 Regression gegen `fehlerpattern_schema_ddl_ist_unbeaufsichtigte_migration`.

    Eine frische DB bekommt `geraet_id` per CREATE TABLE -- dort faellt nie
    auf, wenn ein Index in `SCHEMA_SQL` auf die Spalte zeigt. Auf einer
    BESTANDS-DB ist das CREATE TABLE ein No-op, der Index laeuft vor
    `_migriere()` und der Start bricht mit "no such column: geraet_id".

    Genau so ist es am 08.08.2026 gegen eine Kopie der Live-DB passiert,
    waehrend die ganze Suite gruen war. Dieser Test baut den Altzustand
    nach: Tabelle ohne die Spalte, dann verbinden.
    """
    import aiosqlite

    db_pfad = tmp_path / "bestand.db"
    async with aiosqlite.connect(str(db_pfad)) as db:
        await db.execute("""
            CREATE TABLE sensor_warnung (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zeitstempel TEXT NOT NULL,
                zone_id TEXT NOT NULL,
                typ TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                behoben_um TEXT
            )""")
        await db.execute(
            "INSERT INTO sensor_warnung (zeitstempel, zone_id, typ, details) "
            "VALUES ('2026-08-01T10:00:00', 'hecke', 'ausfall', 'alt')"
        )
        await db.commit()

    speicher = Speicher(str(db_pfad))
    await speicher.verbinden()          # darf nicht werfen
    try:
        assert speicher._db is not None
        async with speicher._db.execute("PRAGMA table_info(sensor_warnung)") as c:
            spalten = {z["name"] async for z in c}
        assert "geraet_id" in spalten

        async with speicher._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_sensor_warnung_offen_geraet'"
        ) as c:
            assert await c.fetchone() is not None, "Index fehlt nach Migration"

        # Bestandszeile ist jetzt eine Zonen-Warnung, wie sie es immer war.
        offen = await speicher.offene_sensor_warnungen("hecke")
        assert len(offen) == 1
        assert offen[0].geraet_id == ""
    finally:
        await speicher.schliessen()


# --- T-0571: Pflanze ohne Geraet ist kein Sensor-Ausfall ---------------------


async def _fyta_status(
    speicher: Speicher, *, geraet_id: str, plant_id: int, zone_id: str,
    sensor_id: str, zeitstempel: datetime, battery_level: float | None = 80.0,
) -> None:
    from bewaesserung.modelle import FytaGeraeteStatus

    await speicher.speichere_fyta_geraete_status(FytaGeraeteStatus(
        zeitstempel=zeitstempel, geraet_id=geraet_id, plant_id=plant_id,
        sensor_id=sensor_id, zone_id=zone_id, battery_level=battery_level,
    ))


@pytest.mark.asyncio
async def test_t0571_pflanze_ohne_geraet_erzeugt_keine_ausfall_warnung(
    speicher: Speicher,
):
    """Realfall 09.09.2026 (Hecke/Faulbaum).

    Der Sensor wurde in der FYTA-App einer anderen Pflanze zugeordnet. Die
    alte Pflanze meldet seither eine LEERE `sensor_id`, und ihr letzter
    Messwert altert vor sich hin -- die Karte zeigte 397,9 h "Sensor-Ausfall"
    und markierte die ganze Hecke als "Zustand unbekannt", obwohl Gardena-Lead
    und Liguster sauber messen.
    """
    jetzt = datetime.now()
    # Lebender Nachbar in derselben Zone: die Zone darf nicht in den
    # "gar kein Geraet"-Zweig fallen, sonst prueft der Test etwas anderes.
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_900004", zeitstempel=jetzt,
    )
    await _fyta_status(
        speicher, geraet_id="fyta_900004", plant_id=900004, zone_id="hecke",
        sensor_id="mac-terra-2", zeitstempel=jetzt,
    )
    # Die verwaiste Pflanze: alter Messwert, kein Geraet mehr dran.
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_900005",
        zeitstempel=jetzt - timedelta(hours=397),
    )
    await _fyta_status(
        speicher, geraet_id="fyta_900005", plant_id=900005, zone_id="hecke",
        sensor_id="", zeitstempel=jetzt, battery_level=None,
    )

    monitor = SensorHealthMonitor(speicher)
    warnungen = await monitor.pruefe_alle(["hecke"], jetzt=jetzt)

    ausfaelle = [
        w for w in warnungen if w["typ"] == SensorWarnungTyp.AUSFALL.value
    ]
    assert ausfaelle == [], ausfaelle
    offen = await speicher.offene_sensor_warnungen("hecke")
    assert [w for w in offen if w.typ == SensorWarnungTyp.AUSFALL] == []


@pytest.mark.asyncio
async def test_t0571_pflanze_MIT_geraet_warnt_weiter(speicher: Speicher):
    """Gegenprobe zum Test darueber -- sonst waere "keine Warnung" auch dann
    erfuellt, wenn die Ausfall-Erkennung insgesamt kaputt ist. Identischer
    Aufbau, einziger Unterschied: `sensor_id` ist gesetzt.
    """
    jetzt = datetime.now()
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_900004", zeitstempel=jetzt,
    )
    await _fyta_status(
        speicher, geraet_id="fyta_900004", plant_id=900004, zone_id="hecke",
        sensor_id="mac-terra-2", zeitstempel=jetzt,
    )
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_900005",
        zeitstempel=jetzt - timedelta(hours=397),
    )
    await _fyta_status(
        speicher, geraet_id="fyta_900005", plant_id=900005, zone_id="hecke",
        sensor_id="mac-terra-1", zeitstempel=jetzt,
    )

    monitor = SensorHealthMonitor(speicher)
    warnungen = await monitor.pruefe_alle(["hecke"], jetzt=jetzt)

    ausfaelle = [
        w for w in warnungen if w["typ"] == SensorWarnungTyp.AUSFALL.value
    ]
    assert len(ausfaelle) == 1
    assert ausfaelle[0]["geraet_id"] == "fyta_900005"


@pytest.mark.asyncio
async def test_t0571_verwaiste_ausfall_warnung_wird_geschlossen(
    speicher: Speicher,
):
    """Ein Geraet verlaesst die Zone (Umzug oder 30-Tage-Fenster) -- seine
    offene Ausfall-Warnung fasste danach niemand mehr an. `GERAET_ERWARTET_TAGE`
    verspricht im eigenen Kommentar genau dieses Schliessen, umgesetzt war es
    nur fuer den Fall "Zone hat GAR KEIN Geraet mehr".
    """
    from bewaesserung.modelle import SensorWarnung

    jetzt = datetime.now()
    await speicher.oeffne_sensor_warnung(SensorWarnung(
        zeitstempel=jetzt - timedelta(days=2), zone_id="mandevilla_maxi",
        typ=SensorWarnungTyp.AUSFALL, details="fyta_900005: letztes Update vor 397.9h",
        geraet_id="fyta_900005",
    ))
    # Die Zone hat ein anderes, lebendes Geraet -- also NICHT der
    # "gar kein Geraet"-Zweig.
    await _messung_geraet(
        speicher, zone_id="mandevilla_maxi", geraet_id="fyta_900001",
        zeitstempel=jetzt,
    )
    await _fyta_status(
        speicher, geraet_id="fyta_900001", plant_id=900001,
        zone_id="mandevilla_maxi", sensor_id="mac-terra-1",
        zeitstempel=jetzt,
    )

    monitor = SensorHealthMonitor(speicher)
    await monitor.pruefe_alle(["mandevilla_maxi"], jetzt=jetzt)

    offen = await speicher.offene_sensor_warnungen("mandevilla_maxi")
    assert [w for w in offen if w.typ == SensorWarnungTyp.AUSFALL] == []


@pytest.mark.asyncio
async def test_t0571_lebendes_geraet_behaelt_seine_ausfall_warnung(
    speicher: Speicher,
):
    """Gegenprobe zum Verwaisten-Schliesser: er darf NUR Warnungen von
    Geraeten schliessen, die nicht mehr ueberwacht werden. Die Warnung eines
    weiterhin ueberwachten, aber stummen Geraets bleibt offen.
    """
    jetzt = datetime.now()
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_900004", zeitstempel=jetzt,
    )
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_900005",
        zeitstempel=jetzt - timedelta(hours=397),
    )

    monitor = SensorHealthMonitor(speicher)
    await monitor.pruefe_alle(["hecke"], jetzt=jetzt)

    offen = await speicher.offene_sensor_warnungen("hecke")
    ausfaelle = [w for w in offen if w.typ == SensorWarnungTyp.AUSFALL]
    assert len(ausfaelle) == 1
    assert ausfaelle[0].geraet_id == "fyta_900005"


@pytest.mark.asyncio
async def test_t0571_jetzt_parameter_wird_durchgereicht(speicher: Speicher):
    """`pruefe_alle(jetzt=...)` ueberschrieb den Parameter mit
    `datetime.now()`. Der Test setzt eine Uhr WEIT in der Zukunft: mit
    durchgereichtem `jetzt` ist der Sensor 100 Tage stumm und faellt aus dem
    30-Tage-Fenster (Zonen-Warnung ohne geraet_id); ohne Durchreichung ist er
    frisch und es entsteht gar keine Warnung.
    """
    echt_jetzt = datetime.now()
    await _messung_geraet(
        speicher, zone_id="hecke", geraet_id="fyta_900004",
        zeitstempel=echt_jetzt,
    )

    monitor = SensorHealthMonitor(speicher)
    warnungen = await monitor.pruefe_alle(
        ["hecke"], jetzt=echt_jetzt + timedelta(days=100),
    )

    ausfaelle = [
        w for w in warnungen if w["typ"] == SensorWarnungTyp.AUSFALL.value
    ]
    assert len(ausfaelle) == 1
    assert "Kein ueberwachtes Geraet mehr" in ausfaelle[0]["details"]
