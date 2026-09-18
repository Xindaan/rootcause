"""Tests fuer GardenaClient Ventil-Callback-Pfad.

Regressions-Sicherung fuer den Silent-Logging-Bug (Smart Irrigation Control
mit valves-Dict statt valve_activity). Verifiziert beide Device-Typen:
WATER_CONTROL (Einzel-Ventil) und SMART_IRRIGATION_CONTROL (Multi-Ventil).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from bewaesserung.gardena_client import GardenaClient
from bewaesserung.modelle import (
    Ausloser, SensorMessung, VentilAktion, VentilEreignis,
)


def _neuer_client() -> GardenaClient:
    return GardenaClient(client_id="test", client_secret="test")


async def _sammle_events(client: GardenaClient) -> list[VentilEreignis]:
    events: list[VentilEreignis] = []

    async def cb(e: VentilEreignis) -> None:
        events.append(e)

    client.registriere_ventil_callback(cb)
    return events


async def _warte_tasks(client: GardenaClient) -> None:
    """Wartet bis alle Fire-and-Forget Callback-Tasks fertig sind."""
    # Mehrere Iterationen: neue Tasks koennen durch das Warten entstehen
    for _ in range(5):
        if not client._aktive_tasks:
            return
        await asyncio.gather(*list(client._aktive_tasks), return_exceptions=True)


def _init_sic(client: GardenaClient, *valve_ids: str) -> None:
    """Setzt valve-State = CLOSED (Erst-Sichtung, kein Event), damit folgende
    Transitionen echte Events erzeugen."""
    for vid in valve_ids:
        client._ventil_status[vid] = "CLOSED"


@pytest.mark.asyncio
async def test_smart_irrigation_control_initial_state_kein_phantom_event():
    """Erstsichtung eines Ventils emittiert KEIN Event, nur State-Init.

    Andernfalls schreibt jeder Prozess-Start ein Phantom-SCHLIESSEN (dauer_s=0)
    fuer jedes ruhende Ventil — das verunreinigt die DB. Events duerfen nur bei
    echten Transitionen feuern.
    """
    client = _neuer_client()
    client.registriere_zone_namen({
        "Waldblumenhain": "waldblumenhain",
        "Bambuswald": "bambuswald",
    })
    events = await _sammle_events(client)

    device = SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL",
        name="Dual WC",
        valves={
            "v-1": {"name": "Waldblumenhain", "activity": "CLOSED"},
            "v-2": {"name": "Bambuswald", "activity": "CLOSED"},
        },
    )
    callback = client._erstelle_ventil_callback(geraet_id="dev-1")
    callback(device)
    await _warte_tasks(client)

    # Erstsichtung in CLOSED: keine Events, aber State initialisiert
    assert events == []
    assert client._ventil_status == {"v-1": "CLOSED", "v-2": "CLOSED"}

    # Jetzt echte Transition CLOSED -> MANUAL_WATERING
    device.valves["v-1"]["activity"] = "MANUAL_WATERING"
    callback(device)
    await _warte_tasks(client)

    oeffnen_events = [e for e in events if e.aktion == VentilAktion.OEFFNEN]
    assert len(oeffnen_events) == 1
    assert oeffnen_events[0].zone_id == "waldblumenhain"
    assert oeffnen_events[0].ventil_id == "v-1"
    assert oeffnen_events[0].ausloser == Ausloser.MANUELL


@pytest.mark.asyncio
async def test_t0361_warte_auf_ventil_status_wird_durch_initial_state_geweckt():
    client = _neuer_client()
    assert await client.warte_auf_ventil_status("v-1", timeout_s=0.001) is False

    device = SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL",
        name="Dual WC",
        valves={"v-1": {"name": "Bambuswald", "activity": "CLOSED"}},
    )
    callback = client._erstelle_ventil_callback(geraet_id="dev-1")
    callback(device)
    await _warte_tasks(client)

    assert await client.warte_auf_ventil_status("v-1", timeout_s=0.001) is True


def test_t0302_live_state_pro_zone_liest_dswc_dict_valves():
    """T-0302: _live_state_pro_zone muss DSWC-valves (dict-von-dicts) lesen.

    Vor dem Fix las `getattr(v, "activity")` auf dem dict -> immer None ->
    leeres Ergebnis -> der T-0210-Reconnect-Sync war fuer DSWC (die einzige
    Hardware im Bestand) ein kompletter No-op (0 Sync-Events je).
    """
    client = _neuer_client()
    client.registriere_zone_namen({"Bambus": "bambuswald"})

    def _baue_state(activity: str) -> None:
        device = SimpleNamespace(
            type="SMART_IRRIGATION_CONTROL",
            name="Dual WC",
            valves={"v-1": {"id": "v-1", "name": "Bambus", "activity": activity}},
        )
        location = SimpleNamespace(devices={"dev-1": device})
        client._smart_system = SimpleNamespace(locations={"loc-1": location})
        client._location_id = "loc-1"

    _baue_state("CLOSED")
    assert client._live_state_pro_zone() == {"bambuswald": "zu"}

    _baue_state("MANUAL_WATERING")
    assert client._live_state_pro_zone() == {"bambuswald": "offen"}


@pytest.mark.asyncio
async def test_t0300_schliess_timer_wird_beim_close_gecancelt():
    """T-0300: Der lokale Schliess-Timer aus ventil_oeffnen muss beim Close
    gecancelt werden, sonst schliesst ein stale Timer einen Folge-Lauf
    vorzeitig (Stop 10:05 + Neustart 10:07 -> Alt-Timer schliesst 10:10)."""
    client = _neuer_client()

    async def _open(dauer, valve_id=None):
        return None

    async def _close(valve_id=None):
        return None

    device = SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL",
        name="Dual WC",
        start_seconds_to_override=_open,
        stop_until_next_task=_close,
    )
    location = SimpleNamespace(devices={"dev-1": device})
    client._smart_system = SimpleNamespace(locations={"loc-1": location})
    client._location_id = "loc-1"
    key = ("dev-1", "v-1")

    # 1. Lauf oeffnen -> Timer1 aktiv
    await client.ventil_oeffnen("dev-1", 600, valve_id="v-1")
    timer1 = client._schliess_timer[key]
    assert not timer1.cancelled()

    # Frueh stoppen -> Timer1 muss gecancelt + aus dem Dict entfernt sein
    await client.ventil_schliessen("dev-1", valve_id="v-1")
    assert timer1.cancelled(), "Close muss den lokalen Schliess-Timer canceln"
    assert key not in client._schliess_timer

    # 2. Lauf oeffnen -> neuer Timer2, NICHT gecancelt
    await client.ventil_oeffnen("dev-1", 600, valve_id="v-1")
    timer2 = client._schliess_timer[key]
    assert timer2 is not timer1
    assert not timer2.cancelled(), "Folge-Lauf-Timer darf nicht gecancelt sein"

    timer2.cancel()  # Cleanup: nicht spaeter feuern lassen


@pytest.mark.asyncio
async def test_initial_state_offen_dauer_ab_prozess_start():
    """Service startet mitten in laufender Bewaesserung: SCHLIESSEN bekommt Dauer."""
    client = _neuer_client()
    client.registriere_zone_namen({"Waldblumenhain": "waldblumenhain"})
    events = await _sammle_events(client)

    callback = client._erstelle_ventil_callback(geraet_id="dev-1")

    # Erst-Sichtung: Ventil ist offen (Service neu gestartet waehrend Bewaesserung)
    device_offen = SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL",
        name="Dual WC",
        valves={"v-1": {"name": "Waldblumenhain", "activity": "MANUAL_WATERING"}},
    )
    callback(device_offen)
    await _warte_tasks(client)
    assert events == []  # Erst-Sicht: kein Event, aber offen_seit gemerkt
    assert "v-1" in client._ventil_offen_seit

    # Jetzt schliesst das Ventil
    device_zu = SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL",
        name="Dual WC",
        valves={"v-1": {"name": "Waldblumenhain", "activity": "CLOSED"}},
    )
    callback(device_zu)
    await _warte_tasks(client)

    assert len(events) == 1
    assert events[0].aktion == VentilAktion.SCHLIESSEN
    assert events[0].dauer_sekunden >= 0  # Dauer ab Prozess-Start


@pytest.mark.asyncio
async def test_smart_irrigation_control_open_close_sequenz():
    """OEFFNEN + SCHLIESSEN erzeugt zwei Events mit korrekter Dauer."""
    client = _neuer_client()
    client.registriere_zone_namen({"Bambuswald": "bambuswald"})
    _init_sic(client, "v-1")
    events = await _sammle_events(client)

    callback = client._erstelle_ventil_callback(geraet_id="dev-1")

    # Oeffnen
    device_offen = SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL",
        name="Dual WC",
        valves={"v-1": {"name": "Bambuswald", "activity": "SCHEDULED_WATERING"}},
    )
    callback(device_offen)
    await _warte_tasks(client)

    # Schliessen
    device_zu = SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL",
        name="Dual WC",
        valves={"v-1": {"name": "Bambuswald", "activity": "CLOSED"}},
    )
    callback(device_zu)
    await _warte_tasks(client)

    assert len(events) == 2
    assert events[0].aktion == VentilAktion.OEFFNEN
    assert events[0].zone_id == "bambuswald"
    assert events[1].aktion == VentilAktion.SCHLIESSEN
    assert events[1].zone_id == "bambuswald"
    # Dauer >= 0 (Zeit zwischen den Calls ist minimal, muss aber int sein)
    assert events[1].dauer_sekunden >= 0


@pytest.mark.asyncio
async def test_keine_duplikate_bei_gleicher_activity():
    """Gleicher activity-String feuert kein zweites Event."""
    client = _neuer_client()
    client.registriere_zone_namen({"Waldblumenhain": "waldblumenhain"})
    _init_sic(client, "v-1")
    events = await _sammle_events(client)

    callback = client._erstelle_ventil_callback(geraet_id="dev-1")
    device = SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL",
        name="Dual WC",
        valves={"v-1": {"name": "Waldblumenhain", "activity": "MANUAL_WATERING"}},
    )

    callback(device)
    await _warte_tasks(client)
    callback(device)  # gleiche activity — kein neues Event
    await _warte_tasks(client)

    assert len(events) == 1


@pytest.mark.asyncio
async def test_t0261_intra_offen_wechsel_dedupliziert():
    """T-0261: Wechsel innerhalb _VENTIL_OFFEN_STATES (z.B.
    MANUAL_WATERING -> OPEN) bei einem manuellen Gardena-App-Start
    darf NICHT zwei OEFFNEN-Events erzeugen. Realfall 27.05.07:39
    bambuswald: 0.879 s zwischen den beiden Activity-Wechseln, beide
    landeten vor T-0261 als getrennte OEFFNEN-Events in der DB."""
    client = _neuer_client()
    client.registriere_zone_namen({"Bambuswald": "bambuswald"})
    _init_sic(client, "v-1")
    events = await _sammle_events(client)

    callback = client._erstelle_ventil_callback(geraet_id="dev-1")
    # 1. Welle: MANUAL_WATERING -> OEFFNEN-Event erwartet
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual WC",
        valves={"v-1": {"name": "Bambuswald", "activity": "MANUAL_WATERING"}},
    ))
    await _warte_tasks(client)
    # 2. Welle: OPEN -> KEIN zweites OEFFNEN-Event (gleiche Bewaesserung)
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual WC",
        valves={"v-1": {"name": "Bambuswald", "activity": "OPEN"}},
    ))
    await _warte_tasks(client)

    assert len(events) == 1
    assert events[0].aktion == VentilAktion.OEFFNEN
    # State trotzdem aktualisiert -> Folge-CLOSED kriegt korrekte Dauer
    assert client._ventil_status["v-1"] == "OPEN"
    # Metrik-Spur fuer Diagnose
    snap = client.metriken.snapshot()
    assert snap["events_verworfen"].get("intra_offen:OPEN") == 1


@pytest.mark.asyncio
async def test_t0261_intra_offen_dann_close_korrekt():
    """Nach intra-Offen-Dedup muss ein echtes CLOSED-Event weiterhin
    ein SCHLIESSEN ausloesen (Dauer berechnet aus erstem OEFFNEN)."""
    client = _neuer_client()
    client.registriere_zone_namen({"Bambuswald": "bambuswald"})
    _init_sic(client, "v-1")
    events = await _sammle_events(client)

    callback = client._erstelle_ventil_callback(geraet_id="dev-1")
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual WC",
        valves={"v-1": {"name": "Bambuswald", "activity": "MANUAL_WATERING"}},
    ))
    await _warte_tasks(client)
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual WC",
        valves={"v-1": {"name": "Bambuswald", "activity": "OPEN"}},
    ))
    await _warte_tasks(client)
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual WC",
        valves={"v-1": {"name": "Bambuswald", "activity": "CLOSED"}},
    ))
    await _warte_tasks(client)

    assert len(events) == 2
    assert events[0].aktion == VentilAktion.OEFFNEN
    assert events[1].aktion == VentilAktion.SCHLIESSEN
    assert events[1].dauer_sekunden >= 0


@pytest.mark.asyncio
async def test_t0261_intra_zu_wechsel_dedupliziert():
    """Symmetrisch: Wechsel innerhalb _VENTIL_ZU_STATES (z.B.
    CLOSING -> CLOSED) erzeugt KEIN zweites SCHLIESSEN-Event."""
    client = _neuer_client()
    client.registriere_zone_namen({"Bambuswald": "bambuswald"})
    _init_sic(client, "v-1")
    events = await _sammle_events(client)

    callback = client._erstelle_ventil_callback(geraet_id="dev-1")
    # Offen -> Zu
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual WC",
        valves={"v-1": {"name": "Bambuswald", "activity": "MANUAL_WATERING"}},
    ))
    await _warte_tasks(client)
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual WC",
        valves={"v-1": {"name": "Bambuswald", "activity": "CLOSING"}},
    ))
    await _warte_tasks(client)
    # Zweite Welle: CLOSED -> KEIN zweites SCHLIESSEN
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual WC",
        valves={"v-1": {"name": "Bambuswald", "activity": "CLOSED"}},
    ))
    await _warte_tasks(client)

    assert len(events) == 2  # nur 1x OEFFNEN + 1x SCHLIESSEN
    assert [e.aktion for e in events] == [
        VentilAktion.OEFFNEN, VentilAktion.SCHLIESSEN,
    ]
    snap = client.metriken.snapshot()
    assert snap["events_verworfen"].get("intra_zu:CLOSED") == 1


@pytest.mark.asyncio
async def test_t0288_replay_guard_unterdrueckt_phantom_paar():
    """T-0288: nach einem (Re)Connect spielt die py-smart-gardena-Lib
    gepufferte Alt-Events erneut ein. Im Replay-Guard-Fenster duerfen
    Cross-State-Wechsel (CLOSED->OPEN->CLOSED) KEINE Phantom-Events
    erzeugen -- nur State nachfuehren. Realfall 03.06. nach Laptop-Schlaf:
    4 Ventile, je ein Phantom-OEFFNEN/SCHLIESSEN-Paar."""
    client = _neuer_client()
    client.registriere_zone_namen({"Bambuswald": "bambuswald"})
    _init_sic(client, "v-1")  # State CLOSED (kein Initial-Phantom)
    events = await _sammle_events(client)
    client._replay_guard_bis = datetime.now() + timedelta(seconds=120)

    callback = client._erstelle_ventil_callback(geraet_id="dev-1")
    # Replay: CLOSED -> MANUAL_WATERING (OPEN-Klasse) -- waere sonst OEFFNEN
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual WC",
        valves={"v-1": {"name": "Bambuswald", "activity": "MANUAL_WATERING"}},
    ))
    await _warte_tasks(client)
    # Replay: -> CLOSED (Cross-State zurueck) -- waere sonst SCHLIESSEN
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual WC",
        valves={"v-1": {"name": "Bambuswald", "activity": "CLOSED"}},
    ))
    await _warte_tasks(client)

    assert events == []  # KEIN Phantom-Event im Guard-Fenster
    assert client._ventil_status["v-1"] == "CLOSED"  # State trotzdem nachgefuehrt
    snap = client.metriken.snapshot()
    assert snap["events_verworfen"].get("replay_guard:MANUAL_WATERING") == 1
    assert snap["events_verworfen"].get("replay_guard:CLOSED") == 1


@pytest.mark.asyncio
async def test_t0288_nach_guard_echte_transition_emittiert():
    """Nach Ablauf des Guard-Fensters loest ein echter Wechsel wieder ein
    OEFFNEN-Event aus -- der Guard darf den Normalbetrieb nicht blockieren."""
    client = _neuer_client()
    client.registriere_zone_namen({"Bambuswald": "bambuswald"})
    _init_sic(client, "v-1")
    events = await _sammle_events(client)
    client._replay_guard_bis = datetime.now() - timedelta(seconds=1)  # abgelaufen

    callback = client._erstelle_ventil_callback(geraet_id="dev-1")
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual WC",
        valves={"v-1": {"name": "Bambuswald", "activity": "MANUAL_WATERING"}},
    ))
    await _warte_tasks(client)

    assert len(events) == 1
    assert events[0].aktion == VentilAktion.OEFFNEN


@pytest.mark.asyncio
async def test_t0306_ws_gap_armiert_guard_kein_phantom_oeffnen():
    """T-0306: py-smart-gardena reconnectet INTERN -> der Backend-WS-Loop
    armiert den Guard nicht. Eine WS-Luecke ueber alle Events
    (>= REPLAY_GAP_SEKUNDEN) muss den Guard gap-getriggert armieren, sodass
    der Replay-Burst KEIN Phantom-OEFFNEN erzeugt. Realfall 16.06.: 43-min-
    Luecke -> Reconnect-Burst -> Hecken-Phantom."""
    client = _neuer_client()
    client.registriere_zone_namen({"Hecke": "hecke"})
    _init_sic(client, "v-h")  # status CLOSED
    events = await _sammle_events(client)
    # WS war 20 min still (Offline-Phase), Guard NICHT armiert.
    client._letzter_ws_event = datetime.now() - timedelta(minutes=20)
    assert client._replay_guard_bis is None

    callback = client._erstelle_ventil_callback(geraet_id="dev-h")
    # Replay-Burst nach Gap: CLOSED -> SCHEDULED_WATERING (waere sonst OEFFNEN).
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="DWC2",
        valves={"v-h": {"name": "Hecke", "activity": "SCHEDULED_WATERING"}},
    ))
    await _warte_tasks(client)

    assert client._replay_guard_bis is not None  # Gap-Detection hat armiert
    assert events == []  # kein Phantom-OEFFNEN
    # T-0306: KEIN offen_seit-Anker -> spaeterer CLOSE kann keine Phantom-Dauer rechnen.
    assert "v-h" not in client._ventil_offen_seit
    snap = client.metriken.snapshot()
    assert snap["replay_guard_armiert"].get("ws_gap") == 1


@pytest.mark.asyncio
async def test_t0306_split_burst_close_bekommt_dauer_null():
    """T-0306: Der Phantom-CLOSE aus einem ZWEITEN Replay-Schwall (ausserhalb
    des 120s-Guard-Fensters) darf keine riesige Dauer gegen den replayten
    OEFFNEN rechnen. Ohne offen_seit-Anker bekommt er dauer=0 und faellt durch
    die MIN_DAUER-Filter (statt 3882 s wie im Realfall 16.06.)."""
    client = _neuer_client()
    client.registriere_zone_namen({"Hecke": "hecke"})
    _init_sic(client, "v-h")
    events = await _sammle_events(client)
    client._letzter_ws_event = datetime.now() - timedelta(minutes=20)

    callback = client._erstelle_ventil_callback(geraet_id="dev-h")
    # 1. Schwall (gap-armiert): OEFFNEN unterdrueckt, status nachgefuehrt, KEIN Anker.
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="DWC2",
        valves={"v-h": {"name": "Hecke", "activity": "SCHEDULED_WATERING"}},
    ))
    await _warte_tasks(client)
    assert events == []
    assert client._ventil_status["v-h"] == "SCHEDULED_WATERING"

    # Guard ablaufen lassen, WS lebt (kein Gap mehr) -> 2. Schwall replayt CLOSED.
    client._replay_guard_bis = datetime.now() - timedelta(seconds=1)
    client._letzter_ws_event = datetime.now()
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="DWC2",
        valves={"v-h": {"name": "Hecke", "activity": "CLOSED"}},
    ))
    await _warte_tasks(client)

    # CLOSE wird emittiert, aber mit dauer=0 (kein Anker) -> kein wirkungsrate-Phantom.
    assert len(events) == 1
    assert events[0].aktion == VentilAktion.SCHLIESSEN
    assert events[0].dauer_sekunden == 0


@pytest.mark.asyncio
async def test_t0306_normaler_event_kein_gap_kein_guard():
    """Regression: ein Event nach normaler Cadence (kurze Luecke) armiert den
    Guard NICHT -- echte Transitionen muessen weiter durchgehen."""
    client = _neuer_client()
    client.registriere_zone_namen({"Hecke": "hecke"})
    _init_sic(client, "v-h")
    events = await _sammle_events(client)
    client._letzter_ws_event = datetime.now() - timedelta(minutes=3)  # < 15 min

    callback = client._erstelle_ventil_callback(geraet_id="dev-h")
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="DWC2",
        valves={"v-h": {"name": "Hecke", "activity": "MANUAL_WATERING"}},
    ))
    await _warte_tasks(client)

    assert client._replay_guard_bis is None  # kein Gap -> nicht armiert
    assert len(events) == 1
    assert events[0].aktion == VentilAktion.OEFFNEN


@pytest.mark.asyncio
async def test_t0288_replay_guard_unterdrueckt_sensor_replay():
    """T-0288-Folge (Isomorphie): im Replay-Guard-Fenster werden auch
    Sensor-Messungen verworfen (Realfall: Morgen-Peak mit Reconnect-
    Stempel -> Phantom-Spike). Nach Ablauf flieszen Sensorwerte wieder."""
    client = _neuer_client()
    sensor_msgs = []

    async def scb(m):
        sensor_msgs.append(m)

    client.registriere_sensor_callback(scb)
    callback = client._erstelle_sensor_callback(geraet_id="sensor-1")

    # Im Guard-Fenster: replayter Alt-Wert -> verworfen, kein Dispatch.
    client._replay_guard_bis = datetime.now() + timedelta(seconds=120)
    callback(SimpleNamespace(soil_humidity=85, soil_temperature=20))
    await _warte_tasks(client)
    assert sensor_msgs == []
    snap = client.metriken.snapshot()
    assert snap["events_verworfen"].get("sensor_replay_guard") == 1

    # Nach Ablauf des Fensters: echter Wert wird durchgereicht.
    client._replay_guard_bis = datetime.now() - timedelta(seconds=1)
    callback(SimpleNamespace(soil_humidity=66, soil_temperature=20))
    await _warte_tasks(client)
    assert len(sensor_msgs) == 1
    assert sensor_msgs[0].boden_feuchte == 66


@pytest.mark.asyncio
async def test_water_control_einzelventil():
    """WATER_CONTROL: Einzel-Ventil-Pfad (valve_activity direkt am Device)."""
    client = _neuer_client()
    client.registriere_zone_namen({"Hecke": "hecke"})
    _init_sic(client, "wc-valve-1")
    events = await _sammle_events(client)

    device = SimpleNamespace(
        type="WATER_CONTROL",
        name="Water Control",
        valve_id="wc-valve-1",
        valve_name="Hecke",
        valve_activity="MANUAL_WATERING",
    )
    callback = client._erstelle_ventil_callback(geraet_id="dev-2")
    callback(device)
    await _warte_tasks(client)

    assert len(events) == 1
    assert events[0].zone_id == "hecke"
    assert events[0].ventil_id == "wc-valve-1"
    assert events[0].aktion == VentilAktion.OEFFNEN


@pytest.mark.asyncio
async def test_valve_name_fallback_bei_unbekannter_zone():
    """Unbekannter valve_name -> Fallback auf valve_id, Event wird trotzdem geloggt."""
    client = _neuer_client()
    # Keine zone_name-Registrierung
    _init_sic(client, "v-99")
    events = await _sammle_events(client)

    device = SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL",
        name="Dual WC",
        valves={"v-99": {"name": "FremderKanal", "activity": "OPEN"}},
    )
    callback = client._erstelle_ventil_callback(geraet_id="dev-1")
    callback(device)
    await _warte_tasks(client)

    assert len(events) == 1
    # Fallback: zone_id == valve_id
    assert events[0].zone_id == "v-99"
    assert events[0].ventil_id == "v-99"


@pytest.mark.asyncio
async def test_multi_valve_parallel_events():
    """Beide Ventile eines Dual WC feuern unabhaengig voneinander."""
    client = _neuer_client()
    client.registriere_zone_namen({
        "Waldblumenhain": "waldblumenhain",
        "Bambuswald": "bambuswald",
    })
    _init_sic(client, "v-1", "v-2")
    events = await _sammle_events(client)

    callback = client._erstelle_ventil_callback(geraet_id="dev-1")

    # Beide offen
    device = SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL",
        name="Dual WC",
        valves={
            "v-1": {"name": "Waldblumenhain", "activity": "MANUAL_WATERING"},
            "v-2": {"name": "Bambuswald", "activity": "SCHEDULED_WATERING"},
        },
    )
    callback(device)
    await _warte_tasks(client)

    assert len(events) == 2
    zone_ids = {e.zone_id for e in events}
    assert zone_ids == {"waldblumenhain", "bambuswald"}


@pytest.mark.asyncio
async def test_ventil_name_override_via_registriere_zone_namen():
    """Wenn zone.ventil_name != zone.name, muss main.py beide Namen registrieren.

    Der Fix besteht darin, beide Mappings ins zone_name_map zu setzen. Hier
    simulieren wir das direkt per Aufruf von registriere_zone_namen.
    """
    client = _neuer_client()
    # Simuliert: zone.name='Bambuswald', zone.ventil_name='Bambus'
    client.registriere_zone_namen({"Bambuswald": "bambuswald", "Bambus": "bambuswald"})
    _init_sic(client, "v-2")
    events = await _sammle_events(client)

    device = SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL",
        name="Dual WC",
        valves={"v-2": {"name": "Bambus", "activity": "SCHEDULED_WATERING"}},
    )
    callback = client._erstelle_ventil_callback(geraet_id="dev-1")
    callback(device)
    await _warte_tasks(client)

    assert len(events) == 1
    assert events[0].zone_id == "bambuswald"


# --- T-0055-B4: WebSocket-Diagnose-Metriken ---

def test_metriken_inc_und_snapshot():
    from bewaesserung.gardena_client import WebSocketMetriken
    m = WebSocketMetriken()
    m.inc("events_empfangen")
    m.inc("events_empfangen")
    m.inc("events_verworfen", "unveraendert:CLOSED")
    m.inc("events_verworfen", "initial_state")
    m.inc("events_verworfen", "initial_state")

    snap = m.snapshot()
    assert snap == {
        "events_empfangen": {"": 2},
        "events_verworfen": {"unveraendert:CLOSED": 1, "initial_state": 2},
    }


def test_metriken_diff_seit_snapshot():
    from bewaesserung.gardena_client import WebSocketMetriken
    m = WebSocketMetriken()
    m.inc("events_empfangen", "MANUAL_WATERING")
    alt = m.snapshot()
    m.inc("events_empfangen", "MANUAL_WATERING")
    m.inc("events_empfangen", "CLOSED")
    m.inc("events_verworfen", "initial_state")
    diff = m.diff_seit_snapshot(alt)
    assert diff == {
        "events_empfangen": {"MANUAL_WATERING": 1, "CLOSED": 1},
        "events_verworfen": {"initial_state": 1},
    }


@pytest.mark.asyncio
async def test_metriken_zaehlen_emittierte_events():
    """Open + Close -> 1x empfangen + 1x empfangen (initial geloggt) + 2x emittiert."""
    client = _neuer_client()
    client.registriere_zone_namen({"Bambuswald": "bambuswald"})
    _init_sic(client, "v-1")  # Initial-State-Call ist drin → zaehlt 1x empfangen + 1x initial_state verworfen
    _ = await _sammle_events(client)

    callback = client._erstelle_ventil_callback(geraet_id="dev-1")
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual",
        valves={"v-1": {"name": "Bambuswald", "activity": "SCHEDULED_WATERING"}},
    ))
    await _warte_tasks(client)
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual",
        valves={"v-1": {"name": "Bambuswald", "activity": "CLOSED"}},
    ))
    await _warte_tasks(client)

    snap = client.metriken.snapshot()
    assert snap["events_emittiert"] == {"oeffnen": 1, "schliessen": 1}
    # _init_sic setzt Status direkt (kein _verarbeite_ventil_update-Call)
    # → nur die 2 echten Callbacks zaehlen als empfangen, jetzt mit Activity-Sublabel
    assert snap["events_empfangen"] == {"SCHEDULED_WATERING": 1, "CLOSED": 1}
    # Keine Verwerfungen erwartet (vorheriger Status ist gesetzt, beide Transitionen gueltig)
    assert "events_verworfen" not in snap or not snap["events_verworfen"]


@pytest.mark.asyncio
async def test_metriken_zaehlt_unbekannten_status():
    """Aktivitaet ausserhalb OFFEN/ZU-Whitelist → Warning + Counter."""
    client = _neuer_client()
    client.registriere_zone_namen({"Bambuswald": "bambuswald"})
    _init_sic(client, "v-1")
    _ = await _sammle_events(client)

    callback = client._erstelle_ventil_callback(geraet_id="dev-1")
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual",
        valves={"v-1": {"name": "Bambuswald", "activity": "FOOBAR"}},
    ))
    await _warte_tasks(client)

    snap = client.metriken.snapshot()
    # Unbekannter Status wird mit Label "unbekannter_status:FOOBAR" gezaehlt
    verworfen = snap.get("events_verworfen", {})
    assert any(k.startswith("unbekannter_status:") for k in verworfen), verworfen
    # Kein echtes Event emittiert
    assert snap.get("events_emittiert", {}).get("oeffnen", 0) == 0


@pytest.mark.asyncio
async def test_metriken_zaehlt_kein_zone_match():
    """Valve-Name passt zu keiner Zone → Counter + Fallback auf valve_id."""
    client = _neuer_client()
    client.registriere_zone_namen({"Bambuswald": "bambuswald"})
    _init_sic(client, "v-1")
    _ = await _sammle_events(client)

    callback = client._erstelle_ventil_callback(geraet_id="dev-1")
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="Dual",
        valves={"v-1": {"name": "Unbekannte-Zone", "activity": "SCHEDULED_WATERING"}},
    ))
    await _warte_tasks(client)

    snap = client.metriken.snapshot()
    verworfen = snap.get("events_verworfen", {})
    assert verworfen.get("kein_zone_match", 0) == 1
    # Event wird trotzdem emittiert (mit valve_id als zone_id)
    assert snap.get("events_emittiert", {}).get("oeffnen", 0) == 1


def test_websockets_closed_monkey_patch_vorhanden():
    """Regression-Schutz: websockets 13.x hat kein .closed — wir patchen.

    F10 aus Code-Review 2026-04-19. py-smart-gardena liest im
    Reconnect-Loop `client.closed`. Fehlt das Attribut, endet der Loop
    mit Exception-Flood. Bei websockets-Upgrade (>=14) wuerde der Patch
    zwar nicht greifen, aber das `closed`-Attribut existiert dann schon
    nativ — in beiden Faellen muss `hasattr(ClientConnection, "closed")`
    True sein. Der Test scheitert, wenn jemand den Monkey-Patch entfernt
    ohne die neue Version mit nativer Property.
    """
    from websockets.asyncio.client import ClientConnection

    # Import von gardena_client zieht den Patch (idempotent).
    import bewaesserung.gardena_client  # noqa: F401

    assert hasattr(ClientConnection, "closed"), (
        "ClientConnection.closed fehlt — py-smart-gardena Reconnect wuerde "
        "mit AttributeError brechen. Patch in gardena_client.py pruefen."
    )


@pytest.mark.asyncio
async def test_t0324_ws_gap_triggert_sensor_catchup_callback():
    """T-0324: Eine WS-Luecke >= REPLAY_GAP_SEKUNDEN feuert die registrierten
    ws-gap-Callbacks (Sensor-DHS-Catch-up), damit Gardena-Sensorwerte nach einem
    Reconnect ohne manuellen Restart aufholen."""
    client = _neuer_client()
    client.registriere_zone_namen({"Hecke": "hecke"})
    _init_sic(client, "v-h")
    await _sammle_events(client)

    gerufen: list[float] = []

    async def _cb(luecke_s: float) -> None:
        gerufen.append(luecke_s)

    client.registriere_ws_gap_callback(_cb)
    # WS war 20 min still (Offline-Phase).
    client._letzter_ws_event = datetime.now() - timedelta(minutes=20)

    callback = client._erstelle_ventil_callback(geraet_id="dev-h")
    # Unveraenderter Event (CLOSED->CLOSED) reicht: die Gap-Detection laeuft VOR
    # dem unveraendert-Filter (T-0306).
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="DWC2",
        valves={"v-h": {"name": "Hecke", "activity": "CLOSED"}},
    ))
    await _warte_tasks(client)

    assert len(gerufen) == 1
    assert gerufen[0] >= 900  # >= REPLAY_GAP_SEKUNDEN


@pytest.mark.asyncio
async def test_t0324_kurzer_gap_kein_catchup():
    """Kurze Luecke (< Schwelle) -> kein Catch-up-Callback (kein Spam im
    Normalbetrieb)."""
    client = _neuer_client()
    client.registriere_zone_namen({"Hecke": "hecke"})
    _init_sic(client, "v-h")
    await _sammle_events(client)

    gerufen: list[float] = []

    async def _cb(luecke_s: float) -> None:
        gerufen.append(luecke_s)

    client.registriere_ws_gap_callback(_cb)
    client._letzter_ws_event = datetime.now() - timedelta(minutes=3)  # < 15 min

    callback = client._erstelle_ventil_callback(geraet_id="dev-h")
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="DWC2",
        valves={"v-h": {"name": "Hecke", "activity": "CLOSED"}},
    ))
    await _warte_tasks(client)

    assert gerufen == []


def test_t0448_ist_multi_valve_geraet_spiegelt_die_oeffnen_weiche():
    """T-0448: Die Typ-Auskunft muss exakt die Weiche aus ventil_oeffnen /
    ventil_schliessen spiegeln (`type == SMART_IRRIGATION_CONTROL`) -- sonst
    entscheidet ein Aufrufer nach einer anderen Wahrheit, als der Call
    danach anwendet.

    None heisst 'nicht beantwortbar' und darf NICHT als 'einkanalig' gelesen
    werden -- genau daran haengt, ob VentilSicherung blockiert.
    """
    client = _neuer_client()

    # Nicht verbunden -> keine Auskunft moeglich.
    assert client.ist_multi_valve_geraet("dev-1") is None

    location = SimpleNamespace(devices={
        "dswc": SimpleNamespace(type="SMART_IRRIGATION_CONTROL", name="Dual WC"),
        "wc": SimpleNamespace(type="WATER_CONTROL", name="Water Control"),
    })
    client._smart_system = SimpleNamespace(locations={"loc-1": location})
    client._location_id = "loc-1"

    assert client.ist_multi_valve_geraet("dswc") is True
    assert client.ist_multi_valve_geraet("wc") is False
    # Unbekanntes Geraet: wieder None, nicht False.
    assert client.ist_multi_valve_geraet("gibt-es-nicht") is None


@pytest.mark.asyncio
async def test_t0547_eigener_puls_close_ueberlebt_guard():
    """T-0547: Ein Haupt-Puls >= REPLAY_GAP_SEKUNDEN erzeugt die WS-Stille
    SELBST -- das erste Event danach ist sein echter Cloud-Close.

    Realfall 20.08.: OEFFNEN 04:27:31 (1800 s kommandiert), 30 min WS-Stille,
    04:57:29 CLOSED -> `replay_guard_armiert_nach_ws_gap luecke_s=1796.7` ->
    `replay_guard_unterdrueckt activity=CLOSED` -> Watchdog schloss 04:58:03
    mit `ausloser=watchdog dauer=1831`. Seit 17.08. bei ALLEN haupt-Pulsen.

    Fuer ein Ventil, das unsere eigene VentilSicherung offen fuehrt, muss der
    Guard das Event durchlassen. NEGATIVPROBE im selben Test: ein FREMDES
    Ventil im selben Guard-Fenster bleibt unterdrueckt (T-0288/T-0306).
    """
    client = _neuer_client()
    client.registriere_zone_namen({
        "Waldblumenhain": "waldblumenhain", "Hecke": "hecke",
    })
    _init_sic(client, "v-f")
    # v-w laeuft: unser eigener Puls, Status entsprechend offen.
    client._ventil_status["v-w"] = "MANUAL_WATERING"
    events = await _sammle_events(client)
    # v-w fuehren wir selbst offen, v-f gehoert uns nicht.
    client.setze_eigener_lauf_pruefer(lambda vid: vid == "v-w")
    # 30 min Stille = die Dauer unseres eigenen Pulses.
    client._letzter_ws_event = datetime.now() - timedelta(minutes=30)

    callback = client._erstelle_ventil_callback(geraet_id="dev-w")
    callback(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="DWC1",
        valves={
            # Unser Cloud-Close ...
            "v-w": {"name": "Waldblumenhain", "activity": "CLOSED"},
            # ... und im selben Burst ein fremdes Phantom-OEFFNEN (T-0306).
            "v-f": {"name": "Hecke", "activity": "SCHEDULED_WATERING"},
        },
    ))
    await _warte_tasks(client)

    # Der Gap hat armiert (Guard bleibt fuer fremde Ventile scharf) ...
    assert client._replay_guard_bis is not None
    snap = client.metriken.snapshot()
    assert snap["replay_guard_armiert"].get("ws_gap") == 1
    # ... aber unser eigener Close kam durch, und NUR unserer.
    assert [e.zone_id for e in events] == ["waldblumenhain"]
    assert events[0].aktion == VentilAktion.SCHLIESSEN
    assert snap["replay_guard_durchgelassen"].get("CLOSED") == 1
    assert snap["events_verworfen"].get("replay_guard:SCHEDULED_WATERING") == 1
    # Negativprobe-Haertung: das fremde Phantom-OEFFNEN ankert auch keine
    # Dauer fuer einen spaeteren Phantom-CLOSE (T-0306).
    assert "v-f" not in client._ventil_offen_seit


@pytest.mark.asyncio
async def test_t0547_kein_pruefer_verhaelt_sich_wie_vor_t0547():
    """T-0547: Ohne injizierten Pruefer (und wenn er wirft) bleibt der Guard
    exakt so scharf wie vor der Aenderung -- der Fix darf sich nicht auf
    Pfade auswirken, die keine VentilSicherung haben."""
    for pruefer in (None, lambda vid: 1 / 0):
        client = _neuer_client()
        client.registriere_zone_namen({"Hecke": "hecke"})
        _init_sic(client, "v-h")
        events = await _sammle_events(client)
        if pruefer is not None:
            client.setze_eigener_lauf_pruefer(pruefer)
        client._letzter_ws_event = datetime.now() - timedelta(minutes=30)

        callback = client._erstelle_ventil_callback(geraet_id="dev-h")
        callback(SimpleNamespace(
            type="SMART_IRRIGATION_CONTROL", name="DWC2",
            valves={"v-h": {"name": "Hecke", "activity": "SCHEDULED_WATERING"}},
        ))
        await _warte_tasks(client)

        assert events == []
        assert "v-h" not in client._ventil_offen_seit


def _sensor_device(feuchte: float):
    return SimpleNamespace(
        soil_humidity=feuchte, soil_temperature=18.0,
        ambient_temperature=None, light_intensity=None, battery_level=90,
    )


@pytest.mark.asyncio
async def test_t0548_sensorwert_nach_30s_kommt_durch_ventil_bleibt_geschuetzt():
    """T-0548: Der Sensor-Guard endet nach REPLAY_GUARD_SENSOR_SEKUNDEN, der
    Ventil-Guard laeuft die vollen REPLAY_GUARD_SEKUNDEN weiter.

    Messung 03.06.-23.08.: ein echter Replay-Schwall ist nach 1 s durch
    (11/16 Faellen), 30 s decken 14/16 ab. Die 120 s kosteten dagegen ~2
    Sensor-Beats pro Tag -- endgueltig, weil fuer Sensoren kein Backfill
    nachtraegt (Realfall waldblumenhain 20.08., Luecke 23:40 -> 01:40).
    """
    client = _neuer_client()
    client.registriere_geraet_zone("sens-1", "waldblumenhain")
    client.registriere_zone_namen({"Hecke": "hecke"})
    _init_sic(client, "v-h")
    messungen: list[SensorMessung] = []

    async def sensor_cb(m: SensorMessung) -> None:
        messungen.append(m)

    client.registriere_sensor_callback(sensor_cb)
    events = await _sammle_events(client)

    # Guard vor 45 s armiert: Sensor-Fenster (30 s) ist vorbei,
    # Ventil-Fenster (120 s) laeuft noch.
    armiert = datetime.now() - timedelta(seconds=45)
    client._replay_guard_armiert_am = armiert
    client._replay_guard_bis = armiert + timedelta(seconds=120)

    client._erstelle_sensor_callback("sens-1")(_sensor_device(41.0))
    client._erstelle_ventil_callback(geraet_id="dev-h")(SimpleNamespace(
        type="SMART_IRRIGATION_CONTROL", name="DWC2",
        valves={"v-h": {"name": "Hecke", "activity": "SCHEDULED_WATERING"}},
    ))
    await _warte_tasks(client)

    assert [m.boden_feuchte for m in messungen] == [41.0]
    assert events == []  # Ventil weiter geschuetzt
    assert "v-h" not in client._ventil_offen_seit


@pytest.mark.asyncio
async def test_t0548_sensorwert_im_ersten_fenster_bleibt_unterdrueckt():
    """T-0548 Negativprobe: INNERHALB der 30 s greift der Sensor-Guard
    unveraendert -- der Realfall 03.06. (Morgen-Peak 85 mit Abend-Stempel)
    darf nicht wieder durchkommen."""
    client = _neuer_client()
    client.registriere_geraet_zone("sens-1", "waldblumenhain")
    messungen: list[SensorMessung] = []

    async def sensor_cb(m: SensorMessung) -> None:
        messungen.append(m)

    client.registriere_sensor_callback(sensor_cb)
    armiert = datetime.now() - timedelta(seconds=5)
    client._replay_guard_armiert_am = armiert
    client._replay_guard_bis = armiert + timedelta(seconds=120)

    client._erstelle_sensor_callback("sens-1")(_sensor_device(85.0))
    await _warte_tasks(client)

    assert messungen == []
    assert client.metriken.snapshot()["events_verworfen"].get(
        "sensor_replay_guard") == 1


@pytest.mark.asyncio
async def test_t0548_gap_armierung_setzt_beide_fenster():
    """T-0548: Die Gap-Armierung muss den Armierungs-Zeitpunkt mitfuehren,
    sonst faellt der Sensor-Pfad still auf das alte 120-s-Fenster zurueck."""
    client = _neuer_client()
    client.registriere_geraet_zone("sens-1", "waldblumenhain")
    client.registriere_sensor_callback(_noop_sensor_cb)
    client._letzter_ws_event = datetime.now() - timedelta(minutes=30)

    client._erstelle_sensor_callback("sens-1")(_sensor_device(40.0))
    await _warte_tasks(client)

    assert client._replay_guard_armiert_am is not None
    spanne = (
        client._replay_guard_bis - client._replay_guard_armiert_am
    ).total_seconds()
    assert spanne == pytest.approx(120, abs=1)
    # Direkt nach dem Armieren ist auch der Sensor-Guard aktiv.
    assert client._sensor_guard_aktiv(datetime.now()) is True
    assert client._sensor_guard_aktiv(
        datetime.now() + timedelta(seconds=31)) is False


async def _noop_sensor_cb(m: SensorMessung) -> None:
    return None
