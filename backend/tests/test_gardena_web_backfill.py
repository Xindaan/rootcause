import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bewaesserung.gardena_web_backfill import (
    GardenaWebBackfillJob,
    _ist_ausgefuehrt,
    _parse_iso,
    _summary_zu_ausloser,
    baue_kanal_zu_zonen,
    baue_zone_dswc_kanal_map,
)
from bewaesserung.modelle import (
    Ausloser,
    VentilAktion,
    VentilEreignis,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "dhs.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _fake_auth():
    auth = MagicMock()
    auth.hole_gueltigen_token = AsyncMock(return_value="fake-token-123")
    return auth


def _dhs_response(events: list[dict]) -> dict:
    """Baut eine DHS-Antwort mit den gegebenen Events im `included`-Block."""
    return {
        "data": [{
            "type": "dh-action-serie",
            "id": "serie-test",
            "attributes": {"property-name": "action_1"},
            "relationships": {"dh-events": {"data": [
                {"type": "dh-action-event", "id": f"ev-{i}"}
                for i in range(len(events))
            ]}},
        }],
        "included": [
            {"type": "dh-action-event", "id": f"ev-{i}", "attributes": attr}
            for i, attr in enumerate(events)
        ],
    }


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
        "bewaesserung.gardena_web_backfill.httpx.AsyncClient",
        return_value=mock_client,
    ), mock_client


def test_summary_zu_ausloeser_mappt_echte_werte_vom_19_04():
    # Echte Summaries aus der DHS-Response
    assert _summary_zu_ausloser("EXECUTED_MANUAL", "MANUAL") == Ausloser.MANUELL
    assert _summary_zu_ausloser("MANUAL_START_MANUAL_STOP", "MANUAL") == Ausloser.MANUELL
    assert _summary_zu_ausloser("EXECUTED_SCHEDULE", "SINGLE") == Ausloser.AUTOMATIK
    # Fallback
    assert _summary_zu_ausloser("", "") == Ausloser.AUTOMATIK


def test_ist_ausgefuehrt_filtert_skipped_events():
    # Skipped wegen Sensor-Feuchte ueber Schwelle → NICHT ausgefuehrt
    assert not _ist_ausgefuehrt({"summary": "SKIPPED_SENSOR_CONTEXT", "decision": "SKIP", "duration": 56})
    assert not _ist_ausgefuehrt({"summary": "SKIPPED_RAIN", "decision": "SKIP", "duration": 56})
    # Decision=CANCELED → nicht ausgefuehrt
    assert not _ist_ausgefuehrt({"summary": "CANCELED", "decision": "CANCELED", "duration": 56})
    # Echte Bewaesserungen (duration gesetzt)
    assert _ist_ausgefuehrt({"summary": "EXECUTED_MANUAL", "decision": None, "duration": 297})
    assert _ist_ausgefuehrt({"summary": "EXECUTED_SCHEDULE", "decision": "NOOP", "duration": 56})
    # Whitelist: kein EXECUTED_-Prefix → nicht ausgefuehrt (z.B. altes MANUAL_START_MANUAL_STOP)
    assert not _ist_ausgefuehrt({"summary": "MANUAL_START_MANUAL_STOP", "decision": None, "duration": 60})


def test_ist_ausgefuehrt_filtert_valve_error_mit_leerer_duration():
    """Regression fuer Bug 21.04.2026: Frostschutz → VALVE_ERROR + duration=None.

    Gardena persistiert verhinderte Schedules als `summary=VALVE_ERROR,
    decision=NOOP, duration=null`. Der alte Blacklist-Filter liess sie
    durch, und der Parser-Fallback `stop-start` erfand 300s/1200s als
    Dauer → Phantom-Bewaesserungen in der DB. Whitelist faengt das jetzt.
    """
    # Der konkrete Frostschutz-Fall vom 21.04.
    assert not _ist_ausgefuehrt({
        "summary": "VALVE_ERROR", "decision": "NOOP", "duration": None,
        "action": "SINGLE",
    })
    # Weitere plausible Error-Codes (zukunftssicher via Whitelist)
    assert not _ist_ausgefuehrt({"summary": "MOTOR_ERROR", "decision": "NOOP", "duration": None})
    assert not _ist_ausgefuehrt({"summary": "TIMEOUT", "decision": None, "duration": None})
    # EXECUTED_* aber duration=None oder 0 → auch nicht akzeptieren
    assert not _ist_ausgefuehrt({"summary": "EXECUTED_MANUAL", "duration": None})
    assert not _ist_ausgefuehrt({"summary": "EXECUTED_MANUAL", "duration": 0})
    assert not _ist_ausgefuehrt({"summary": "EXECUTED_MANUAL", "duration": -1})
    # Kaputte duration-Werte duerfen nicht crashen
    assert not _ist_ausgefuehrt({"summary": "EXECUTED_MANUAL", "duration": "abc"})


def test_parse_iso_konvertiert_utc_zu_lokaler_naiver_zeit():
    # 2026-04-18T09:25:07Z UTC -> bei System-TZ CEST (UTC+2): 11:25:07 lokal
    dt = _parse_iso("2026-04-18T09:25:07Z")
    assert dt.tzinfo is None
    # Sommer in Berlin: 11:25:07. Wir testen nur Minuten/Sekunden (Stunde haengt von TZ ab).
    assert dt.minute == 25
    assert dt.second == 7


def test_baue_kanal_zu_zonen_gruppiert_nach_kanal():
    zonen = [
        ZonenKonfig(zone_id="bambu1", name="B1", ventil_kanal=2, modus=ZonenModus.AUTOMATIK),
        ZonenKonfig(zone_id="bambu2", name="B2", ventil_kanal=2, modus=ZonenModus.AUTOMATIK),
        ZonenKonfig(zone_id="wald", name="W", ventil_kanal=1, modus=ZonenModus.AUTOMATIK),
        ZonenKonfig(zone_id="topf", name="T", ventil_kanal=None, modus=ZonenModus.MONITORING),
    ]
    m = baue_kanal_zu_zonen(zonen)
    assert m == {1: ["wald"], 2: ["bambu1", "bambu2"]}


# --- T-0252: Multi-DSWC-Routing der Live-Event-Expansion ----------------

def test_baue_zone_dswc_kanal_map_disambiguiert_pro_dswc():
    """Zwei Zonen am gleichen Kanal aber an verschiedenen DSWCs duerfen
    sich NICHT vermischen — sonst landen Live-Events bei der falschen
    Zone (T-0252, Realfall: waldblumenhain DSWC1-Kanal1 erzeugte
    Phantom-Events fuer magerwiese DSWC2-Kanal1)."""
    dswc_1 = "11111111-1111-1111-1111-111111111111"
    dswc_2 = "22222222-2222-2222-2222-222222222222"
    zonen = [
        # DSWC 1
        ZonenKonfig(zone_id="waldblumenhain", name="Waldblumen",
                    ventil_kanal=1, ventil_geraet_id=dswc_1,
                    modus=ZonenModus.AUTOMATIK),
        ZonenKonfig(zone_id="bambuswald", name="Bambus",
                    ventil_kanal=2, ventil_geraet_id=dswc_1,
                    modus=ZonenModus.AUTOMATIK),
        ZonenKonfig(zone_id="bambuswald_yogaraum", name="Yoga",
                    ventil_kanal=2, ventil_geraet_id=dswc_1,
                    modus=ZonenModus.AUTOMATIK),
        # DSWC 2
        ZonenKonfig(zone_id="magerwiese", name="Magerwiese",
                    ventil_kanal=1, ventil_geraet_id=dswc_2,
                    modus=ZonenModus.AUTOMATIK),
        ZonenKonfig(zone_id="hecke", name="Hecke",
                    ventil_kanal=2, ventil_geraet_id=dswc_2,
                    modus=ZonenModus.AUTOMATIK),
        # Monitoring-Zone ohne Kanal -> wird ignoriert
        ZonenKonfig(zone_id="topf", name="Topf",
                    ventil_kanal=None, modus=ZonenModus.MONITORING),
    ]
    m = baue_zone_dswc_kanal_map(zonen)

    # Kanal 1 ist pro DSWC isoliert — keine Cross-Contamination.
    assert m[(dswc_1, 1)] == ["waldblumenhain"]
    assert m[(dswc_2, 1)] == ["magerwiese"]
    # Geschwister-Expansion auf demselben (DSWC, Kanal) bleibt erhalten.
    assert m[(dswc_1, 2)] == ["bambuswald", "bambuswald_yogaraum"]
    assert m[(dswc_2, 2)] == ["hecke"]
    # Monitoring-only Zone taucht nirgends auf.
    assert all("topf" not in zonen_liste for zonen_liste in m.values())


def test_baue_zone_dswc_kanal_map_fallback_primary_dswc():
    """Zonen ohne `ventil_geraet_id` fallen auf `primary_geraet_id` zurueck
    (Single-DSWC-Backward-Compat)."""
    primary = "primary-dswc-uuid"
    zonen = [
        ZonenKonfig(zone_id="a", name="A", ventil_kanal=1,
                    modus=ZonenModus.AUTOMATIK),
        ZonenKonfig(zone_id="b", name="B", ventil_kanal=2,
                    modus=ZonenModus.AUTOMATIK),
    ]
    m = baue_zone_dswc_kanal_map(zonen, primary_geraet_id=primary)
    assert m == {(primary, 1): ["a"], (primary, 2): ["b"]}


def test_baue_zone_dswc_kanal_map_ohne_primary_behaelt_none_key():
    """Ohne `primary_geraet_id` UND ohne explizite `ventil_geraet_id`
    bleibt der Geraete-Schluessel None (zulaessig fuer Test-Setups)."""
    zonen = [
        ZonenKonfig(zone_id="x", name="X", ventil_kanal=1,
                    modus=ZonenModus.AUTOMATIK),
    ]
    m = baue_zone_dswc_kanal_map(zonen)
    assert m == {(None, 1): ["x"]}


def test_parse_events_zieht_nur_vollstaendige_eintraege():
    # Echter DHS-Event aus der Beobachtung am 19.04.
    events_raw = [
        # Gueltiger Event
        {"start": "2026-04-18T09:25:07Z", "stop": "2026-04-18T09:26:03Z",
         "duration": 56, "action": "SINGLE", "summary": "EXECUTED_SCHEDULE",
         "firmware-action-id": 0},
        # Unvollstaendig (kein stop)
        {"start": "2026-04-18T10:00:00Z", "duration": 60,
         "summary": "EXECUTED_SCHEDULE", "firmware-action-id": 0},
        # Dauer 0 → uebersprungen
        {"start": "2026-04-18T11:00:00Z", "stop": "2026-04-18T11:00:00Z",
         "duration": 0, "action": "MANUAL", "summary": "EXECUTED_MANUAL",
         "firmware-action-id": 0},
        # SKIPPED → nicht uebernehmen (Bug vom 19.04.)
        {"start": "2026-04-19T03:37:00Z", "stop": "2026-04-19T03:42:00Z",
         "duration": 300, "action": "SINGLE",
         "summary": "SKIPPED_SENSOR_CONTEXT", "decision": "SKIP",
         "firmware-action-id": 1},
    ]
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=MagicMock(),
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["wald"]},
    )
    parsed = job._parse_events(_dhs_response(events_raw))
    assert len(parsed) == 1   # nur der erste, die anderen drei wurden gefiltert
    start, stop, dauer, kanal, aus = parsed[0]
    assert dauer == 56
    assert kanal == 1
    assert aus == Ausloser.AUTOMATIK


def test_aktualisiere_schreibt_events_pro_zone_am_kanal(speicher):
    # Echte 18.04.-Waldblumenhain-Events 11:25 + 11:29 (UTC 09:25/09:29)
    dhs = _dhs_response([
        {"start": "2026-04-18T09:25:07Z", "stop": "2026-04-18T09:26:03Z",
         "duration": 56, "action": "SCHEDULED", "summary": "EXECUTED_SCHEDULE",
         "firmware-action-id": 0},
        {"start": "2026-04-18T09:29:15Z", "stop": "2026-04-18T09:30:12Z",
         "duration": 57, "action": "SCHEDULED", "summary": "EXECUTED_SCHEDULE",
         "firmware-action-id": 0},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["waldblumenhain"]},
    )
    patcher, _ = _patch_httpx(dhs)
    with patcher:
        neu = _run(job.aktualisiere())
    assert neu == 2

    # Pruefe: beide Events mit OEFFNEN+SCHLIESSEN persistiert
    eingetragen = _run(speicher.hole_ventil_ereignisse("waldblumenhain"))
    assert len(eingetragen) == 4  # 2 Events × (OEFFNEN+SCHLIESSEN)
    assert all(e.ventil_id == "gardena_web" for e in eingetragen)
    assert all(e.ausloser == Ausloser.AUTOMATIK for e in eingetragen)


def test_kanal_expansion_auf_mehrere_zonen(speicher):
    dhs = _dhs_response([
        {"start": "2026-04-18T10:56:00Z", "stop": "2026-04-18T11:01:00Z",
         "duration": 300, "action": "SCHEDULED", "summary": "EXECUTED_SCHEDULE",
         "firmware-action-id": 1},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={2: ["bambuswald", "bambuswald_yogaraum"]},
    )
    patcher, _ = _patch_httpx(dhs)
    with patcher:
        neu = _run(job.aktualisiere())
    assert neu == 2  # 1 Event, 2 Zonen
    bambu = _run(speicher.hole_ventil_ereignisse("bambuswald"))
    yoga = _run(speicher.hole_ventil_ereignisse("bambuswald_yogaraum"))
    assert len(bambu) == 2  # OEFFNEN + SCHLIESSEN
    assert len(yoga) == 2


def test_dedup_gegen_existierendes_event(speicher):
    # Vorhandenes Live-Event 1 Min nach DHS-Start → DHS-Insert wird geskippt
    dhs_start_utc = datetime(2026, 4, 18, 9, 25, 7)  # UTC
    # lokal auf dem Rechner (Europa/Berlin Sommer CEST): 11:25
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=datetime(2026, 4, 18, 11, 25, 20),  # 13 s nach DHS-Start lokal
        zone_id="waldblumenhain", ventil_id="live-uuid:0",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.AUTOMATIK,
    )))

    dhs = _dhs_response([
        {"start": "2026-04-18T09:25:07Z", "stop": "2026-04-18T09:26:03Z",
         "duration": 56, "action": "SCHEDULED", "summary": "EXECUTED_SCHEDULE",
         "firmware-action-id": 0},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["waldblumenhain"]},
    )
    patcher, _ = _patch_httpx(dhs)
    with patcher:
        neu = _run(job.aktualisiere())
    assert neu == 0  # Live hatte Vorrang

    # Nach wie vor nur das Live-OEFFNEN-Event (kein SCHLIESSEN, weil Test-Setup)
    alle = _run(speicher.hole_ventil_ereignisse("waldblumenhain"))
    gardena_web = [e for e in alle if e.ventil_id == "gardena_web"]
    assert gardena_web == []


def test_f10_lone_schliessen_am_ende_blockt_dhs_doppelpaar(speicher):
    """F10: Hat der T-0288-Replay-Guard das OEFFNEN eines echten Laufs
    verschluckt, liegt nur ein lone SCHLIESSEN (am Lauf-ENDE) in der DB. Das
    ±60s-Fenster um den DHS-START sieht es nicht -> der DHS-Backfill schrieb
    ein zweites volles Paar (Doppelzaehlung). Fix: Spannen-Dedup gegen das
    Lauf-Ende (stop)."""
    # DHS-Lauf 11:25:07-11:26:03 lokal (CEST). Nur das echte SCHLIESSEN am
    # Ende existiert (Valve-UUID), das OEFFNEN wurde vom Replay-Guard
    # verschluckt -- 7s nach dem DHS-Stop, aber ~63s nach dem DHS-Start.
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=datetime(2026, 4, 18, 11, 26, 10),
        zone_id="waldblumenhain", ventil_id="live-uuid:0",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=56,
        ausloser=Ausloser.AUTOMATIK,
    )))

    dhs = _dhs_response([
        {"start": "2026-04-18T09:25:07Z", "stop": "2026-04-18T09:26:03Z",
         "duration": 56, "action": "SCHEDULED", "summary": "EXECUTED_SCHEDULE",
         "firmware-action-id": 0},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["waldblumenhain"]},
    )
    patcher, _ = _patch_httpx(dhs)
    with patcher:
        neu = _run(job.aktualisiere())
    assert neu == 0, "lone SCHLIESSEN am Lauf-Ende muss das DHS-Doppel-Paar blocken"

    alle = _run(speicher.hole_ventil_ereignisse("waldblumenhain"))
    gardena_web = [e for e in alle if e.ventil_id == "gardena_web"]
    assert gardena_web == []


def test_t0358_verwaister_close_wird_mit_dhs_ausloser_repariert(speicher):
    """T-0358: Replay-Guard hat das echte OEFFNEN verschluckt (WS-Gap zum
    Reconnect) oder ein Restart hat nur einen partiellen Close-Anker erzeugt.
    Der DHS-Backfill muss das fehlende OEFFNEN nachtragen und Dauer/Ausloeser
    aus DHS korrigieren."""
    # Nur ein verwaistes SCHLIESSEN nahe dem Lauf-Ende (Valve-UUID), KEIN
    # OEFFNEN. Dauer > 0 deckt die Restart-Variante mit Initial-State-Anker ab.
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=datetime(2026, 4, 18, 11, 26, 5),
        zone_id="waldblumenhain", ventil_id="live-uuid:0",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=17,
        ausloser=Ausloser.MANUELL,
    )))

    dhs = _dhs_response([
        {"start": "2026-04-18T09:25:07Z", "stop": "2026-04-18T09:26:03Z",
         "duration": 56, "action": "SCHEDULED", "summary": "EXECUTED_SCHEDULE",
         "firmware-action-id": 0},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["waldblumenhain"]},
    )
    patcher, _ = _patch_httpx(dhs)
    with patcher:
        neu = _run(job.aktualisiere())
    assert neu == 0, "Reparatur laeuft ueber den Korrektur-Pfad, kein neues Paar"

    alle = _run(speicher.hole_ventil_ereignisse("waldblumenhain"))
    # Kein gardena_web-Doppelpaar (F10-Dedup greift).
    assert [e for e in alle if e.ventil_id == "gardena_web"] == []
    # Fehlendes OEFFNEN nachgetragen, gepaart (ventil_id + DHS-Ausloeser).
    oeffnen = [e for e in alle if e.aktion == VentilAktion.OEFFNEN]
    assert len(oeffnen) == 1 and oeffnen[0].ventil_id == "live-uuid:0"
    assert oeffnen[0].ausloser == Ausloser.AUTOMATIK
    # Close auf DHS-Dauer und DHS-Ausloeser korrigiert.
    schliessen = [e for e in alle if e.aktion == VentilAktion.SCHLIESSEN]
    assert len(schliessen) == 1 and schliessen[0].dauer_sekunden == 56
    assert schliessen[0].ausloser == Ausloser.AUTOMATIK


def test_zweiter_lauf_ist_idempotent(speicher):
    dhs = _dhs_response([
        {"start": "2026-04-18T09:25:07Z", "stop": "2026-04-18T09:26:03Z",
         "duration": 56, "action": "MANUAL", "summary": "EXECUTED_MANUAL",
         "firmware-action-id": 0},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["wald"]},
    )

    patcher1, _ = _patch_httpx(dhs)
    with patcher1:
        erst = _run(job.aktualisiere())
    patcher2, _ = _patch_httpx(dhs)
    with patcher2:
        zweit = _run(job.aktualisiere())

    assert erst == 1
    assert zweit == 0  # zweiter Lauf dedupt gegen eigene Events


def test_intervall_gate_blockt_zu_haeufige_laeufe(speicher):
    dhs = _dhs_response([])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["wald"]},
        intervall_minuten=30,
    )

    jetzt1 = datetime(2026, 4, 19, 10, 0)
    patcher1, client1 = _patch_httpx(dhs)
    with patcher1:
        _run(job.aktualisiere_wenn_faellig(jetzt1))
    assert client1.get.call_count == 1

    # 10 Min spaeter → zu frueh, kein HTTP-Call
    patcher2, client2 = _patch_httpx(dhs)
    with patcher2:
        _run(job.aktualisiere_wenn_faellig(jetzt1 + timedelta(minutes=10)))
    assert client2.get.call_count == 0

    # 35 Min spaeter → wieder faellig
    patcher3, client3 = _patch_httpx(dhs)
    with patcher3:
        _run(job.aktualisiere_wenn_faellig(jetzt1 + timedelta(minutes=35)))
    assert client3.get.call_count == 1


def test_abruf_fehler_blockt_nicht_den_loop(speicher):
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["wald"]},
    )
    # httpx wirft bei uns RuntimeError
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=RuntimeError("gardena-down"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    with patch(
        "bewaesserung.gardena_web_backfill.httpx.AsyncClient",
        return_value=mock_client,
    ):
        neu = _run(job.aktualisiere())
    assert neu == 0  # keine Events, aber auch keine Exception


def test_dhs_ersetzt_heuristik_event_im_fenster(speicher):
    """Codex-Finding P2: Heuristik-Events sollen durch DHS-Ground-Truth ersetzt
    werden, nicht nebeneinander bleiben."""
    # Vorher: ein sensor_heuristik-Paar (OEFFNEN + SCHLIESSEN) fuer waldblumenhain
    # um 11:25 lokal (= 09:25 UTC).
    start_lokal = datetime(2026, 4, 18, 11, 25, 30)  # naive-lokal
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=start_lokal, zone_id="wald", ventil_id="sensor_heuristik",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.UNBEKANNT,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=start_lokal + timedelta(seconds=60),
        zone_id="wald", ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=60,
        ausloser=Ausloser.UNBEKANNT,
    )))

    # DHS liefert jetzt die Ground-Truth fuer dasselbe Fenster (11:25:07 UTC+2)
    dhs = _dhs_response([
        {"start": "2026-04-18T09:25:07Z", "stop": "2026-04-18T09:26:03Z",
         "duration": 56, "action": "SINGLE", "summary": "EXECUTED_SCHEDULE",
         "firmware-action-id": 0},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["wald"]},
    )
    patcher, _ = _patch_httpx(dhs)
    with patcher:
        neu = _run(job.aktualisiere())
    assert neu == 1

    alle = _run(speicher.hole_ventil_ereignisse("wald"))
    quellen = {e.ventil_id for e in alle}
    # Heuristik-Events sind entfernt, gardena_web-Events stehen
    assert "sensor_heuristik" not in quellen
    assert "gardena_web" in quellen


def test_dhs_blockiert_wenn_live_event_im_fenster(speicher):
    """Codex-Finding P2 Gegenprobe: Live-Events (ventil_id=UUID) blockieren
    DHS-Insert — Live hat hoechste Prioritaet."""
    start_lokal = datetime(2026, 4, 18, 11, 25, 30)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=start_lokal, zone_id="wald", ventil_id="live-uuid-xyz",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.AUTOMATIK,
    )))

    dhs = _dhs_response([
        {"start": "2026-04-18T09:25:07Z", "stop": "2026-04-18T09:26:03Z",
         "duration": 56, "action": "SINGLE", "summary": "EXECUTED_SCHEDULE",
         "firmware-action-id": 0},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["wald"]},
    )
    patcher, _ = _patch_httpx(dhs)
    with patcher:
        neu = _run(job.aktualisiere())
    assert neu == 0   # Live-Event hatte Vorrang, nichts neu geschrieben


def test_t0055_b4_korrigiert_live_ws_schliesszeit_bei_30min_abweichung(speicher):
    """T-0055-B4 Option C: Live-WS-SCHLIESSEN ist 30 min spaeter als DHS-
    Cloud-Wahrheit. Realfall 29.04.: App stoppte 10:11 (echt), Live-WS
    loggte 10:41 wegen py-smart-gardena-Reconnect-Bug. DHS-Backfill muss
    den Live-WS-SCHLIESSEN-Eintrag mit DHS-Werten ueberschreiben.
    """
    # Live-WS-Paar: OEFFNEN um 08:42, SCHLIESSEN um 10:41 (= 119 min, falsch)
    oeffnen_zeit = datetime(2026, 4, 29, 8, 42, 0)
    schliessen_zeit_falsch = datetime(2026, 4, 29, 10, 41, 0)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit, zone_id="wald",
        ventil_id="live-uuid-xyz",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=schliessen_zeit_falsch, zone_id="wald",
        ventil_id="live-uuid-xyz",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=7140,  # 119 min
        ausloser=Ausloser.MANUELL,
    )))
    # DHS sagt: SCHLIESSEN um 10:11, dauer 5400 s (= 90 min, korrekt)
    # 08:42 lokal (Berlin CEST = UTC+2 in April): 08:42-2h = 06:42 UTC
    # 10:11 lokal: 08:11 UTC
    dhs = _dhs_response([
        {"start": "2026-04-29T06:42:00Z", "stop": "2026-04-29T08:11:00Z",
         "duration": 5400, "action": "MANUAL", "summary": "EXECUTED_MANUAL",
         "firmware-action-id": 0},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["wald"]},
    )
    patcher, _ = _patch_httpx(dhs)
    with patcher:
        neu = _run(job.aktualisiere())
    # neu=0 weil kein DHS-Insert (Live-WS hatte Vorrang), aber UPDATE laeuft.
    assert neu == 0
    # Verifikation: Live-WS-SCHLIESSEN wurde auf DHS-Werte korrigiert
    ereignisse = _run(speicher.hole_ventil_ereignisse(
        "wald",
        von=oeffnen_zeit - timedelta(minutes=1),
        bis=oeffnen_zeit + timedelta(hours=4),
    ))
    schliessen_events = [
        e for e in ereignisse if e.aktion == VentilAktion.SCHLIESSEN
    ]
    assert len(schliessen_events) == 1
    korrigiert = schliessen_events[0]
    # Erwartete korrigierte Werte (aus DHS, lokale Zeit nach UTC-Konvertierung)
    # 08:11 UTC -> 10:11 lokal CEST
    assert korrigiert.zeitstempel == datetime(2026, 4, 29, 10, 11, 0)
    assert korrigiert.dauer_sekunden == 5400


def test_t0055_b4_keine_korrektur_wenn_schliesszeiten_konsistent(speicher):
    """T-0055-B4 Option C: Live-WS und DHS sind nahe genug (innerhalb
    KORREKTUR_TOLERANZ_SEKUNDEN = 30s) -> kein UPDATE, idempotent.
    """
    oeffnen_zeit = datetime(2026, 4, 29, 8, 42, 0)
    schliessen_zeit = datetime(2026, 4, 29, 10, 11, 5)  # 5s Abweichung von DHS
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit, zone_id="wald",
        ventil_id="live-uuid-xyz",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=schliessen_zeit, zone_id="wald",
        ventil_id="live-uuid-xyz",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=5395,  # 5s Differenz
        ausloser=Ausloser.MANUELL,
    )))
    dhs = _dhs_response([
        {"start": "2026-04-29T06:42:00Z", "stop": "2026-04-29T08:11:00Z",
         "duration": 5400, "action": "MANUAL", "summary": "EXECUTED_MANUAL",
         "firmware-action-id": 0},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["wald"]},
    )
    patcher, _ = _patch_httpx(dhs)
    with patcher:
        _run(job.aktualisiere())
    # Verifikation: SCHLIESSEN-Event unveraendert (Live-WS-Werte bleiben)
    ereignisse = _run(speicher.hole_ventil_ereignisse(
        "wald",
        von=oeffnen_zeit - timedelta(minutes=1),
        bis=oeffnen_zeit + timedelta(hours=4),
    ))
    schliessen = [e for e in ereignisse if e.aktion == VentilAktion.SCHLIESSEN][0]
    assert schliessen.zeitstempel == datetime(2026, 4, 29, 10, 11, 5)
    assert schliessen.dauer_sekunden == 5395


def test_t0321_fehlenden_close_aus_dhs_nachtragen(speicher):
    """T-0321: OEFFNEN aus Live-WS persistiert, aber SCHLIESSEN ging verloren
    (Live-Close-Verlust). Der Lauf ist laut DHS sicher vorbei (Stop weit in der
    Vergangenheit -> Karenz erfuellt) -> der fehlende Close wird aus der DHS-
    Ground-Truth NACHGETRAGEN, statt einen verwaisten Open zu hinterlassen
    (Safety-Net-Loch geschlossen). Vorher: kein UPDATE -> Orphan blieb.
    """
    oeffnen_zeit = datetime(2026, 4, 29, 8, 42, 0)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit, zone_id="wald",
        ventil_id="live-uuid-xyz",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    # Kein SCHLIESSEN!
    dhs = _dhs_response([
        {"start": "2026-04-29T06:42:00Z", "stop": "2026-04-29T08:11:00Z",
         "duration": 5400, "action": "MANUAL", "summary": "EXECUTED_MANUAL",
         "firmware-action-id": 0},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["wald"]},
    )
    patcher, _ = _patch_httpx(dhs)
    with patcher:
        _run(job.aktualisiere())
    ereignisse = _run(speicher.hole_ventil_ereignisse(
        "wald",
        von=oeffnen_zeit - timedelta(minutes=1),
        bis=oeffnen_zeit + timedelta(hours=4),
    ))
    aktionen = [e.aktion for e in ereignisse]
    assert aktionen.count(VentilAktion.OEFFNEN) == 1
    schliessen = [e for e in ereignisse if e.aktion == VentilAktion.SCHLIESSEN]
    assert len(schliessen) == 1, "fehlender Close muss nachgetragen werden"
    # Nachgetragen aus DHS: Stop 08:11Z -> 10:11 lokal, Dauer 5400, ventil_id
    # + ausloser vom OEFFNEN-Pendant uebernommen.
    assert schliessen[0].zeitstempel == datetime(2026, 4, 29, 10, 11, 0)
    assert schliessen[0].dauer_sekunden == 5400
    assert schliessen[0].ventil_id == "live-uuid-xyz"


def test_t0321_korrektur_klaut_nicht_folge_puls_close(speicher):
    """T-0321 Anti-Mis-Pairing: Puls A (Close verloren) + Puls B (Close da).
    Die DHS-Korrektur fuer A darf NICHT den Close von B greifen (frueher: 2h-
    Fenster -> erstbester Close -> B's Close auf A's Zeit zurueckgeschoben, B
    verwaist, Orphan wandert). Stattdessen wird A's eigener Close nachgetragen
    und B's Close bleibt unveraendert.
    """
    # Puls A: OEFFNEN 08:42, KEIN Close. Puls B: OEFFNEN 09:30, Close 09:42.
    a_open = datetime(2026, 4, 29, 8, 42, 0)
    b_open = datetime(2026, 4, 29, 9, 30, 0)
    b_close = datetime(2026, 4, 29, 9, 42, 0)
    for ev in (
        VentilEreignis(zeitstempel=a_open, zone_id="wald", ventil_id="live-uuid-xyz",
                       aktion=VentilAktion.OEFFNEN, dauer_sekunden=0, ausloser=Ausloser.MANUELL),
        VentilEreignis(zeitstempel=b_open, zone_id="wald", ventil_id="live-uuid-xyz",
                       aktion=VentilAktion.OEFFNEN, dauer_sekunden=0, ausloser=Ausloser.MANUELL),
        VentilEreignis(zeitstempel=b_close, zone_id="wald", ventil_id="live-uuid-xyz",
                       aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=720, ausloser=Ausloser.MANUELL),
    ):
        _run(speicher.speichere_ventil_ereignis(ev))
    # DHS meldet Lauf A (start 06:42Z -> 08:42 lokal, stop 06:54Z -> 08:54 lokal,
    # 720s) -- endet VOR Puls B (09:30). A's nachgetragener Close liegt im
    # eigenen Fenster (08:42..09:30), nicht bei B.
    dhs = _dhs_response([
        {"start": "2026-04-29T06:42:00Z", "stop": "2026-04-29T06:54:00Z",
         "duration": 720, "action": "MANUAL", "summary": "EXECUTED_MANUAL",
         "firmware-action-id": 0},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["wald"]},
    )
    patcher, _ = _patch_httpx(dhs)
    with patcher:
        _run(job.aktualisiere())
    ereignisse = _run(speicher.hole_ventil_ereignisse(
        "wald", von=a_open - timedelta(minutes=1), bis=b_close + timedelta(hours=2),
    ))
    schliessen = sorted(
        (e for e in ereignisse if e.aktion == VentilAktion.SCHLIESSEN),
        key=lambda e: e.zeitstempel,
    )
    # B's Close bleibt unveraendert (NICHT geklaut/zurueckgeschoben).
    assert any(
        e.zeitstempel == b_close and e.dauer_sekunden == 720 for e in schliessen
    ), "B's Close darf nicht veraendert werden"
    # A's eigener Close wurde nachgetragen (im eigenen Pulsfenster vor B-Open).
    assert any(
        a_open < e.zeitstempel < b_open for e in schliessen
    ), "A's fehlender Close muss im eigenen Fenster nachgetragen werden"


def test_t0408_doppel_oeffnen_erzeugt_keinen_duplikat_sturm(speicher):
    """T-0408 Kern-Regression: liegt ein ZWEITES OEFFNEN desselben Ventils
    zwischen Pendant und echtem Close, war das T-0321-Pulsfenster
    (bis `naechstes_oeffnen`) zu kurz -> der echte Close wurde NIE gefunden
    -> der Nachtrag-Insert feuerte bei JEDEM Lauf erneut, und die eigenen
    Kopien lagen selbst ausserhalb des Fensters (kein Fixpunkt).

    Realfall magerwiese 11.07.: Doppel-OEFFNEN 62 ms auseinander, 205 Kopien
    desselben Close. Hier drei Backfill-Laeufe -- es darf bei EINEM Close
    bleiben.
    """
    a_open = datetime(2026, 7, 11, 6, 54, 0)
    b_open = datetime(2026, 7, 11, 6, 54, 0, 62000)  # 62 ms spaeter
    echt_close = datetime(2026, 7, 11, 7, 2, 0)
    for ev in (
        VentilEreignis(zeitstempel=a_open, zone_id="wald", ventil_id="live-uuid-xyz",
                       aktion=VentilAktion.OEFFNEN, dauer_sekunden=0, ausloser=Ausloser.MANUELL),
        VentilEreignis(zeitstempel=b_open, zone_id="wald", ventil_id="live-uuid-xyz",
                       aktion=VentilAktion.OEFFNEN, dauer_sekunden=0, ausloser=Ausloser.MANUELL),
        VentilEreignis(zeitstempel=echt_close, zone_id="wald", ventil_id="live-uuid-xyz",
                       aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=480, ausloser=Ausloser.MANUELL),
    ):
        _run(speicher.speichere_ventil_ereignis(ev))
    # DHS: start 04:54Z -> 06:54 lokal, stop 05:02Z -> 07:02 lokal.
    dhs = _dhs_response([
        {"start": "2026-07-11T04:54:00Z", "stop": "2026-07-11T05:02:00Z",
         "duration": 480, "action": "MANUAL", "summary": "EXECUTED_MANUAL",
         "firmware-action-id": 0},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["wald"]},
    )
    for _ in range(3):
        patcher, _unused = _patch_httpx(dhs)
        with patcher:
            _run(job.aktualisiere())
    ereignisse = _run(speicher.hole_ventil_ereignisse(
        "wald", von=a_open - timedelta(minutes=1), bis=echt_close + timedelta(hours=2),
    ))
    schliessen = [e for e in ereignisse if e.aktion == VentilAktion.SCHLIESSEN]
    assert len(schliessen) == 1, (
        f"T-0408: Duplikat-Sturm -- {len(schliessen)} Closes nach 3 Laeufen"
    )
    assert schliessen[0].zeitstempel == echt_close


def test_t0408_guard_blockiert_echten_nachtrag_nicht(speicher):
    """T-0408 Gegenprobe: der Guard darf den legitimen T-0321-Nachtrag nicht
    verhindern. Fehlt der Close WIRKLICH, ist bei dhs_stop nichts -> Insert
    laeuft, und ein zweiter Backfill-Lauf legt keine Kopie nach.
    """
    oeffnen_zeit = datetime(2026, 4, 29, 8, 42, 0)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit, zone_id="wald", ventil_id="live-uuid-xyz",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0, ausloser=Ausloser.MANUELL,
    )))
    dhs = _dhs_response([
        {"start": "2026-04-29T06:42:00Z", "stop": "2026-04-29T08:11:00Z",
         "duration": 5400, "action": "MANUAL", "summary": "EXECUTED_MANUAL",
         "firmware-action-id": 0},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["wald"]},
    )
    for _ in range(2):
        patcher, _unused = _patch_httpx(dhs)
        with patcher:
            _run(job.aktualisiere())
    ereignisse = _run(speicher.hole_ventil_ereignisse(
        "wald", von=oeffnen_zeit - timedelta(minutes=1),
        bis=oeffnen_zeit + timedelta(hours=4),
    ))
    schliessen = [e for e in ereignisse if e.aktion == VentilAktion.SCHLIESSEN]
    assert len(schliessen) == 1, "Nachtrag muss laufen, aber genau einmal"
    assert schliessen[0].zeitstempel == datetime(2026, 4, 29, 10, 11, 0)
    assert schliessen[0].dauer_sekunden == 5400


def test_unbekannter_kanal_wird_ignoriert(speicher):
    # firmware-action-id 2 existiert in der Config nicht
    dhs = _dhs_response([
        {"start": "2026-04-18T09:25:07Z", "stop": "2026-04-18T09:26:03Z",
         "duration": 56, "action": "SCHEDULED", "summary": "EXECUTED_SCHEDULE",
         "firmware-action-id": 2},
    ])
    job = GardenaWebBackfillJob(
        auth=_fake_auth(), speicher=speicher,
        location_id="loc", water_control_geraet_id="dev",
        kanal_zu_zonen={1: ["wald"]},
    )
    patcher, _ = _patch_httpx(dhs)
    with patcher:
        neu = _run(job.aktualisiere())
    assert neu == 0


# --------------------------------------------------------------------------
# T-0419: Beobachtbarkeit des Backfills
# --------------------------------------------------------------------------

def test_t0419_keine_neuen_events_ist_info_nicht_debug():
    """T-0419: das 'ruhiger Pull'-Log muss INFO sein.

    Realfall: es stand auf `logger.debug`, das Log-Level der Anwendung ist
    aber INFO -- die Zeile wurde also NIE ausgegeben. Damit war der Job
    faktisch unbeobachtbar: als der hecke-Close vom 21.07. mit 5400 s statt
    3720 s stehenblieb, liess sich nicht unterscheiden zwischen
    (a) DHS liefert den Lauf nicht, (b) DHS liefert ihn und die Korrektur
    greift nicht, (c) der Job laeuft gar nicht.
    Genau diese Unterscheidung sollte das Log leisten.
    """
    import inspect

    from bewaesserung import gardena_web_backfill

    quelle = inspect.getsource(gardena_web_backfill.GardenaWebBackfillJob)
    i = quelle.index("dhs_backfill.keine_neuen_events")
    davor = quelle[max(0, i - 200):i]
    assert "logger.info" in davor, (
        "dhs_backfill.keine_neuen_events muss auf INFO loggen -- auf DEBUG "
        "ist der Job bei Log-Level INFO unsichtbar"
    )


def test_t0419_korrektur_wird_vor_dem_update_geloggt():
    """T-0419: bevor der Korrektor ein UPDATE fährt, muss er es loggen.

    Sonst laesst sich bei einem unkorrigierten Close nicht feststellen, an
    welcher Station der Kette er verlorenging -- kam er im Vergleich an
    (dann liegt es am UPDATE) oder gar nicht (dann fehlt das DHS-Event oder
    das OEFFNEN-Pendant)?
    """
    import inspect

    from bewaesserung import gardena_web_backfill

    quelle = inspect.getsource(
        gardena_web_backfill.GardenaWebBackfillJob
        ._korrigiere_schliesszeit_falls_abweichend
    )
    assert "dhs.korrektur_faellig" in quelle
    # Das Log muss VOR dem UPDATE stehen, sonst nuetzt es bei einem
    # fehlgeschlagenen UPDATE nichts.
    assert quelle.index("dhs.korrektur_faellig") < quelle.index(
        "aktualisiere_ventil_ereignis"
    )
