"""Tests fuer T-0068 SensorDhsBackfillJob.

Pattern uebernommen von `test_gardena_web_backfill.py`: in-memory
Speicher-Fixture, Fake-Auth, httpx-Patch fuer DHS-Response.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bewaesserung.modelle import DatenQuelle, SensorMessung
from bewaesserung.sensor_dhs_backfill import (
    CATCHUP_FENSTER_STUNDEN,
    DEDUP_FENSTER_MIN,
    STARTUP_FENSTER_STUNDEN,
    SensorDhsBackfillJob,
    _parse_iso,
    _parse_zahl,
    baue_sensor_zu_zone,
)
from bewaesserung.speicher import Speicher


SENSOR_UUID = "33333333-3333-3333-3333-333333333333"  # waldblumenhain
LOC_ID = "44444444-4444-4444-4444-444444444444"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "sensor_dhs.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _fake_auth():
    auth = MagicMock()
    auth.hole_gueltigen_token = AsyncMock(return_value="fake-token-xyz")
    return auth


def _dhs_response(
    events: list[tuple[str, str, str]],  # (zeit_iso_z, "humidity"|"temperature", wert_str)
) -> dict:
    """Baut eine DHS-sensor2-Antwort im echten Schema (Stand 2026-04-25).

    Zwei `dh-point-serie`-Eintraege mit `attributes['property-name']`
    und `relationships['dh-events'].data[]` als Liste der Event-IDs.
    Events im `included`-Block werden ueber die Serie-ID-Liste
    zugeordnet (NICHT ueber `relationships.serie` im Event selbst).
    """
    # Sammle Event-IDs pro Property in serie_events
    serie_events: dict[str, list[str]] = {"humidity": [], "temperature": []}
    included = []
    for i, (zeit, prop, wert) in enumerate(events):
        eid = f"ev-{i}-{prop}-{zeit}"
        serie_events[prop].append(eid)
        included.append({
            "type": "dh-point-event",
            "id": eid,
            "attributes": {"timestamp": zeit, "value": wert},
        })

    daten_block = [
        {
            "type": "dh-point-serie",
            "id": "serie-hum",
            "attributes": {"property-name": "humidity", "unit": "%"},
            "relationships": {
                "dh-events": {
                    "data": [
                        {"type": "dh-point-event", "id": eid}
                        for eid in serie_events["humidity"]
                    ],
                },
            },
        },
        {
            "type": "dh-point-serie",
            "id": "serie-temp",
            "attributes": {"property-name": "temperature", "unit": "C"},
            "relationships": {
                "dh-events": {
                    "data": [
                        {"type": "dh-point-event", "id": eid}
                        for eid in serie_events["temperature"]
                    ],
                },
            },
        },
    ]
    return {"data": daten_block, "included": included}


def _patch_httpx(response_json: dict):
    """Context-Manager-Helfer der httpx.AsyncClient.get mockt."""
    mock_antwort = MagicMock()
    mock_antwort.raise_for_status = MagicMock()
    mock_antwort.json = MagicMock(return_value=response_json)
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_antwort)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return patch(
        "bewaesserung.sensor_dhs_backfill.httpx.AsyncClient",
        return_value=mock_client,
    ), mock_client


# --- Helfer-Funktions-Tests ---


def test_parse_zahl_castet_string_integer_korrekt():
    assert _parse_zahl("55") == 55.0
    assert _parse_zahl("16") == 16.0
    assert _parse_zahl(55) == 55.0  # falls Gardena mal Zahl statt String liefert
    assert _parse_zahl("12.5") == 12.5


def test_parse_zahl_gibt_none_bei_unparsbar():
    assert _parse_zahl(None) is None
    assert _parse_zahl("") is None
    assert _parse_zahl("abc") is None


def test_parse_iso_zu_naive_lokalzeit():
    # 2026-04-23T12:00:00Z = 14:00 lokal (Europe/Berlin Sommerzeit)
    # Der Test ist unabhaengig von der TZ, weil wir auf Naivitaet pruefen
    dt = _parse_iso("2026-04-23T12:00:00Z")
    assert dt.tzinfo is None
    # In Europe/Berlin sind das 14:00; wir machen es weniger TZ-abhaengig
    # indem wir nur sicherstellen dass der Stundenwert "im Tagesbereich" liegt.
    assert dt.year == 2026 and dt.month == 4 and dt.day == 23


# --- Job-Tests ---


def test_parse_messungen_humidity_und_temperature_zusammenfuehren(speicher):
    """Beide Property-Serien am gleichen Zeitstempel -> eine Messung mit beiden Werten."""
    job = SensorDhsBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id=LOC_ID,
        sensor_zu_zone={SENSOR_UUID: "waldblumenhain"},
    )
    daten = _dhs_response([
        ("2026-04-23T10:00:00Z", "humidity", "55"),
        ("2026-04-23T10:00:00Z", "temperature", "16"),
    ])
    nicht_vor = datetime(2020, 1, 1)
    messungen = job._parse_messungen(daten, "waldblumenhain", SENSOR_UUID, nicht_vor)
    assert len(messungen) == 1
    assert messungen[0].boden_feuchte == 55.0
    assert messungen[0].boden_temperatur == 16.0
    assert messungen[0].zone_id == "waldblumenhain"
    assert messungen[0].quelle == DatenQuelle.GARDENA


def test_aktualisiere_schreibt_nur_neue_messungen(speicher):
    """Mehrere DHS-Events -> mehrere SensorMessung-Zeilen in der DB."""
    job = SensorDhsBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id=LOC_ID,
        sensor_zu_zone={SENSOR_UUID: "waldblumenhain"},
    )
    daten = _dhs_response([
        ("2026-04-23T10:00:00Z", "humidity", "55"),
        ("2026-04-23T11:00:00Z", "humidity", "60"),
    ])
    patcher, _ = _patch_httpx(daten)
    with patcher:
        # Fenster gross genug, dass beide Events durchkommen
        neu = _run(job.aktualisiere(jetzt=datetime(2026, 4, 23, 14, 0), von_stunden=24))
    assert neu == 2
    # Zweiter Lauf -> Dedup, nichts neu
    patcher2, _ = _patch_httpx(daten)
    with patcher2:
        neu2 = _run(job.aktualisiere(jetzt=datetime(2026, 4, 23, 14, 0), von_stunden=24))
    assert neu2 == 0


def test_dedup_toleranz_3min_blockt_nahen_treffer(speicher):
    """Existierende Messung um 10:01:23 verhindert DHS-Insert um 09:59:47 (ca. gleicher Punkt)."""
    # Vorhandene Live-WS-Messung
    bestehende_zeit = datetime(2026, 4, 23, 10, 1, 23)
    _run(speicher.speichere_messung(SensorMessung(
        zeitstempel=bestehende_zeit,
        zone_id="waldblumenhain",
        geraet_id=SENSOR_UUID,
        boden_feuchte=55.0,
        quelle=DatenQuelle.GARDENA,
    )))

    job = SensorDhsBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id=LOC_ID,
        sensor_zu_zone={SENSOR_UUID: "waldblumenhain"},
    )
    # DHS liefert "denselben" Punkt mit minutenversetztem Zeitstempel.
    # Wir gehen den _existiert_messung-Pfad direkt — das spart httpx-Mocks.
    naher_zeit = datetime(2026, 4, 23, 9, 59, 47)
    drift_min = abs((bestehende_zeit - naher_zeit).total_seconds()) / 60
    assert drift_min <= DEDUP_FENSTER_MIN

    # Selber Sensor (geraet_id), naher Zeitpunkt -> Treffer (dedupt).
    treffer = _run(job._existiert_messung("waldblumenhain", naher_zeit, SENSOR_UUID))
    assert treffer is True

    # F14/T-0224: ANDERER Sensor (z. B. FYTA) im selben Fenster darf NICHT
    # gegen den Gardena-Punkt dedupt werden -- sonst systematische Gardena-
    # Luecken in Multi-Sensor-Zonen (waldblumenhain: Gardena + 2x FYTA).
    treffer_anderer = _run(
        job._existiert_messung("waldblumenhain", naher_zeit, "fyta-sensor-x")
    )
    assert treffer_anderer is False

    # Jenseits des Fensters: kein Treffer (selber Sensor)
    weit_weg = datetime(2026, 4, 23, 9, 50, 0)
    drift_min2 = abs((bestehende_zeit - weit_weg).total_seconds()) / 60
    assert drift_min2 > DEDUP_FENSTER_MIN
    treffer2 = _run(job._existiert_messung("waldblumenhain", weit_weg, SENSOR_UUID))
    assert treffer2 is False


def test_fehler_bei_hole_dhs_keine_exception_nach_aussen(speicher):
    """HTTP-Fehler darf den Hauptloop nie blockieren."""
    job = SensorDhsBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id=LOC_ID,
        sensor_zu_zone={SENSOR_UUID: "waldblumenhain"},
    )
    # Fake-Client der bei .get() raised
    mock_antwort = MagicMock()
    mock_antwort.raise_for_status = MagicMock(side_effect=RuntimeError("500 Server Error"))
    mock_antwort.json = MagicMock(return_value={})
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_antwort)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    with patch(
        "bewaesserung.sensor_dhs_backfill.httpx.AsyncClient",
        return_value=mock_client,
    ):
        # Darf nicht werfen
        neu = _run(job.aktualisiere(jetzt=datetime(2026, 4, 23, 14, 0)))
    assert neu == 0


def test_adaptives_catchup_fenster_deckt_schlaf_luecke(speicher):
    """T-0287: nach langer Offline-Phase (Laptop-Schlaf) muss das Catch-up-
    Fenster die Luecke abdecken, nicht auf CATCHUP_FENSTER_STUNDEN sitzen."""
    job = SensorDhsBackfillJob(
        auth=_fake_auth(), speicher=speicher, location_id=LOC_ID,
        sensor_zu_zone={SENSOR_UUID: "waldblumenhain"}, intervall_minuten=30,
    )
    jetzt = datetime(2026, 6, 3, 16, 38)
    # Kein vorheriger Lauf -> normales Fenster (Startup-Task deckt Tiefe).
    assert job._adaptives_catchup_fenster(jetzt) == CATCHUP_FENSTER_STUNDEN
    # Normale 30-min-Cadence -> normales Fenster.
    job._letzte_aktualisierung = jetzt - timedelta(minutes=30)
    assert job._adaptives_catchup_fenster(jetzt) == CATCHUP_FENSTER_STUNDEN
    # 8.6 h Schlaf -> Fenster deckt die Luecke (>= verstrichene Zeit).
    job._letzte_aktualisierung = jetzt - timedelta(hours=8, minutes=36)
    fenster = job._adaptives_catchup_fenster(jetzt)
    assert fenster >= 9
    assert fenster <= STARTUP_FENSTER_STUNDEN
    # Sehr lange offline (>7 d) -> auf Endpoint-Grenze gedeckelt.
    job._letzte_aktualisierung = jetzt - timedelta(days=30)
    assert job._adaptives_catchup_fenster(jetzt) == STARTUP_FENSTER_STUNDEN


def test_aktualisiere_wenn_faellig_nutzt_breites_fenster_nach_schlaf(speicher):
    """T-0287: der periodische Lauf nach einer Schlaf-Luecke ruft aktualisiere
    mit dem breiten Fenster, nicht fix CATCHUP_FENSTER_STUNDEN."""
    job = SensorDhsBackfillJob(
        auth=_fake_auth(), speicher=speicher, location_id=LOC_ID,
        sensor_zu_zone={SENSOR_UUID: "waldblumenhain"}, intervall_minuten=30,
    )
    jetzt = datetime(2026, 6, 3, 16, 38)
    job._letzte_aktualisierung = jetzt - timedelta(hours=8, minutes=36)
    job.aktualisiere = AsyncMock(return_value=0)
    _run(job.aktualisiere_wenn_faellig(jetzt=jetzt))
    job.aktualisiere.assert_awaited_once()
    _, kwargs = job.aktualisiere.call_args
    assert kwargs["von_stunden"] >= 9


def test_intervall_gate_blockt_frueh(speicher):
    """Zwei Calls innerhalb von intervall_minuten -> zweiter Call ueberspringt."""
    job = SensorDhsBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id=LOC_ID,
        sensor_zu_zone={SENSOR_UUID: "waldblumenhain"},
        intervall_minuten=30,
    )
    daten = _dhs_response([("2026-04-23T13:30:00Z", "humidity", "55")])
    patcher, mock_client = _patch_httpx(daten)
    with patcher:
        _run(job.aktualisiere_wenn_faellig(jetzt=datetime(2026, 4, 23, 14, 0)))
        # 10 Min spaeter: zu frueh, sollte nichts machen
        ergebnis = _run(job.aktualisiere_wenn_faellig(jetzt=datetime(2026, 4, 23, 14, 10)))
    assert ergebnis == 0
    # Sicherstellen dass _hole_dhs nur einmal aufgerufen wurde
    assert mock_client.get.call_count == 1


def test_baue_sensor_zu_zone_filtert_nicht_sensoren_raus():
    """Helper-Funktion: nur in `sensor_geraete_ids` enthaltene UUIDs werden uebernommen."""
    zuordnungen = {
        "sensor-1": "waldblumenhain",
        "sensor-2": "bambuswald",
        "water-control": "bambuswald",  # Kein Sensor — soll raus
    }
    sensor_ids = ["sensor-1", "sensor-2"]
    ergebnis = baue_sensor_zu_zone(zuordnungen, sensor_ids)
    assert ergebnis == {
        "sensor-1": "waldblumenhain",
        "sensor-2": "bambuswald",
    }


def test_humidity_und_temperature_versetzt_werden_gemerged(speicher):
    """1-2 s versetzte Events fuer denselben Sensor-Tick -> eine Messung.

    Server-seitiges Reporting trennt Events oft um 1-3 s. Ohne
    Bucket-Merge entstehen zwei Zeilen mit jeweils nur halbem Wert.
    """
    job = SensorDhsBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id=LOC_ID,
        sensor_zu_zone={SENSOR_UUID: "waldblumenhain"},
    )
    daten = _dhs_response([
        ("2026-04-23T10:06:02Z", "humidity", "55"),
        ("2026-04-23T10:06:03Z", "temperature", "16"),
    ])
    nicht_vor = datetime(2020, 1, 1)
    messungen = job._parse_messungen(daten, "waldblumenhain", SENSOR_UUID, nicht_vor)
    assert len(messungen) == 1
    assert messungen[0].boden_feuchte == 55.0
    assert messungen[0].boden_temperatur == 16.0


def test_humidity_und_temperature_weit_versetzt_temperatur_only_geskippt(speicher):
    """Wenn ein Bucket nur temperature enthaelt, wird er geskippt.

    Cloud-Reporting-Artefakt: Gardena schickt zwischen Voll-Ticks
    gelegentlich nur einen temperature-Beat ohne humidity. Das in
    `sensor_messung` mit `boden_feuchte=None` zu schreiben wuerde
    nur das Frontend-Diagramm reissen lassen. Wir wollen genau
    eine Messung (die mit humidity), die temperature-only-Bucket
    wird verworfen.
    """
    job = SensorDhsBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id=LOC_ID,
        sensor_zu_zone={SENSOR_UUID: "waldblumenhain"},
    )
    daten = _dhs_response([
        ("2026-04-23T10:00:00Z", "humidity", "55"),
        ("2026-04-23T11:00:00Z", "temperature", "16"),  # 1 h spaeter, kein humidity
    ])
    nicht_vor = datetime(2020, 1, 1)
    messungen = job._parse_messungen(daten, "waldblumenhain", SENSOR_UUID, nicht_vor)
    assert len(messungen) == 1
    assert messungen[0].boden_feuchte == 55.0
    assert messungen[0].boden_temperatur is None


def test_temperatur_only_bucket_skip_zwischen_voll_ticks(speicher):
    """Reproduziert den 25.04.-Fall: humidity-Tick, dann nur Temperatur, dann humidity-Tick.

    DB-Befund 2026-04-25: bambuswald 17:14 50% / 18:14 NULL+temp / 19:14 55%.
    Der temperature-only-Bucket um 18:14 ist ein Cloud-Reporting-Artefakt,
    das zwischen zwei sauberen Voll-Ticks landete. Vor dem Fix entstand
    eine Zeile mit boden_feuchte=NULL und das Frontend-Diagramm zeigte
    eine Luecke. Nach dem Fix: zwei Zeilen mit Feuchte, der Beat-Bucket
    wird ignoriert.
    """
    job = SensorDhsBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id=LOC_ID,
        sensor_zu_zone={SENSOR_UUID: "waldblumenhain"},
    )
    daten = _dhs_response([
        ("2026-04-25T17:14:47Z", "humidity", "50"),
        ("2026-04-25T17:14:48Z", "temperature", "10"),
        ("2026-04-25T18:14:48Z", "temperature", "14"),  # NUR temperature
        ("2026-04-25T19:14:47Z", "humidity", "55"),
        ("2026-04-25T19:14:48Z", "temperature", "11"),
    ])
    nicht_vor = datetime(2020, 1, 1)
    messungen = job._parse_messungen(daten, "waldblumenhain", SENSOR_UUID, nicht_vor)
    assert len(messungen) == 2
    assert messungen[0].boden_feuchte == 50.0
    assert messungen[0].boden_temperatur == 10.0
    assert messungen[1].boden_feuchte == 55.0
    assert messungen[1].boden_temperatur == 11.0


def test_messungen_vor_nicht_vor_grenze_werden_ignoriert(speicher):
    """Events ausserhalb des Catch-up-Fensters fallen aus dem Parse-Schritt."""
    job = SensorDhsBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id=LOC_ID,
        sensor_zu_zone={SENSOR_UUID: "waldblumenhain"},
    )
    daten = _dhs_response([
        ("2026-04-23T10:00:00Z", "humidity", "55"),  # in Fenster
        ("2026-04-20T10:00:00Z", "humidity", "60"),  # vor Fenster
    ])
    nicht_vor = datetime(2026, 4, 23, 0, 0)
    messungen = job._parse_messungen(daten, "waldblumenhain", SENSOR_UUID, nicht_vor)
    assert len(messungen) == 1
    assert messungen[0].boden_feuchte == 55.0
