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

    assert warnungen == [
        {
            "zone_id": "rasen",
            "typ": SensorWarnungTyp.AUSFALL.value,
            "details": "Noch nie Daten empfangen",
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

    assert erste_warnungen == [
        {
            "zone_id": zone_id,
            "typ": SensorWarnungTyp.BATTERIE_NIEDRIG.value,
            "details": "Batterie 15%",
        }
    ]
    assert zweite_warnungen == [
        {
            "zone_id": zone_id,
            "typ": SensorWarnungTyp.BATTERIE_KRITISCH.value,
            "details": "Batterie 5% \u2014 Sofort wechseln!",
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
