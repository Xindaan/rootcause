"""Tests fuer VentilSicherung — Safety-Wrapper, Watchdog, Fehlerpfade."""

import asyncio
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from types import SimpleNamespace

from bewaesserung.gardena_client import GardenaClient
from bewaesserung.modelle import Ausloser, VentilAktion, VentilEreignis
from bewaesserung.ventil_sicherung import VentilSicherung


# --- Attrappen ---

class ClientAttrappe:
    """Minimaler GardenaClient-Ersatz."""

    # T-0276: Mirror der GardenaClient-Konstante. startup_check liest
    # `self._client._VENTIL_OFFEN_STATES` -- ohne diesen Mock crasht
    # die Reconciliation mit AttributeError.
    _VENTIL_OFFEN_STATES = {
        "OPEN", "OPENING",
        "MANUAL_WATERING", "SCHEDULED_WATERING",
    }

    def __init__(self, schliessen_fehler: bool = False):
        self.oeffnen_aufrufe: list[tuple[str, int, str | None]] = []
        self.schliessen_aufrufe: list[tuple[str, str | None]] = []
        self._schliessen_fehler = schliessen_fehler
        self.offene: dict[str, dict] = {}

    async def ventil_oeffnen(
        self, geraet_id: str, dauer_sekunden: int,
        valve_id: str | None = None,
    ) -> None:
        self.oeffnen_aufrufe.append((geraet_id, dauer_sekunden, valve_id))

    async def ventil_schliessen(
        self, geraet_id: str, valve_id: str | None = None,
    ) -> None:
        self.schliessen_aufrufe.append((geraet_id, valve_id))
        if self._schliessen_fehler:
            raise ConnectionError("Gardena nicht erreichbar")

    def offene_valves(self) -> dict[str, dict]:
        return dict(self.offene)


class ClientStartupUnbekannt(ClientAttrappe):
    async def warte_auf_ventil_status(self, valve_id: str, timeout_s: float) -> bool:
        return False


class SpeicherAttrappe:
    """Minimaler Speicher-Ersatz."""

    def __init__(self):
        self.ereignisse: list[VentilEreignis] = []
        self.geloeschte_states: list[tuple] = []

    async def speichere_ventil_ereignis(self, ereignis: VentilEreignis) -> None:
        self.ereignisse.append(ereignis)

    async def existiert_schliessen_seit(
        self, ventil_id, seit, zone_ids=None,
    ) -> bool:
        # T-0331: spiegelt die echte Speicher-Query.
        treffer = [
            e.zone_id for e in self.ereignisse
            if e.aktion == VentilAktion.SCHLIESSEN
            and e.ventil_id == ventil_id
            and e.zeitstempel > seit
        ]
        if zone_ids:
            return set(zone_ids).issubset(set(treffer))
        return bool(treffer)

    async def loesche_live_lauf_state(self, kanal, geraet_id) -> None:
        self.geloeschte_states.append((kanal, geraet_id))


GERAET_ID = "ventil-001"
KANAL = 1
ZONEN = ["bambuswald", "bambuswald_yogaraum"]


# --- Hilfsfunktionen ---

def erstelle_sicherung(
    schliessen_fehler: bool = False,
    puffer: int = 30,
) -> tuple[VentilSicherung, ClientAttrappe, SpeicherAttrappe]:
    client = ClientAttrappe(schliessen_fehler=schliessen_fehler)
    speicher = SpeicherAttrappe()
    sicherung = VentilSicherung(
        client=client,  # type: ignore[arg-type]
        speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
        puffer_sekunden=puffer,
    )
    return sicherung, client, speicher


# --- Tests: bewaessere() mit Kanal-Mapping (T-0112) ---

@pytest.mark.asyncio
async def test_bewaessere_smart_irrigation_reicht_valve_id_durch():
    """Mit Kanal-Mapping: bewaessere() ruft Client mit valve_id auf."""
    client = ClientAttrappe()
    speicher = SpeicherAttrappe()
    valve_uuid = "abcd-1234"
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
        kanal_zu_valve_id={KANAL: valve_uuid},
    )

    erfolg = await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL)

    assert erfolg is True
    assert client.oeffnen_aufrufe == [(GERAET_ID, 600, valve_uuid)]
    # OEFFNEN-Events tragen valve_id (matcht WS-Pipeline + finde_ventil_paar)
    for e in speicher.ereignisse:
        assert e.ventil_id == valve_uuid


@pytest.mark.asyncio
async def test_bewaessere_kanal_nicht_im_mapping_lehnt_ab():
    """Mapping befuellt, aber Kanal fehlt -> Ablehnung statt Crash."""
    client = ClientAttrappe()
    speicher = SpeicherAttrappe()
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
        kanal_zu_valve_id={1: "valve-fuer-kanal-1"},
    )

    erfolg = await sicherung.bewaessere(99, ZONEN, 600, Ausloser.MANUELL)

    assert erfolg is False
    assert client.oeffnen_aufrufe == []
    assert speicher.ereignisse == []


@pytest.mark.asyncio
async def test_stoppe_extern_app_lauf_nutzt_mapping():
    """T-0113b: Stop fuer externen App-Lauf — Mapping aufloesen, Client rufen,
    KEIN synthetisches SCHLIESSEN-Event vom Backend (WS-Pipeline schreibt).
    """
    client = ClientAttrappe()
    speicher = SpeicherAttrappe()
    valve_uuid = "valve-extern"
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
        kanal_zu_valve_id={KANAL: valve_uuid},
    )
    # Kein bewaessere() vorher -> _aktiv ist leer -> "extern"-Pfad

    erfolg = await sicherung.stoppe(KANAL, Ausloser.MANUELL)

    assert erfolg is True
    assert client.schliessen_aufrufe == [(GERAET_ID, valve_uuid)]
    # KEIN synthetisches SCHLIESSEN-Event — die WS-Pipeline schreibt das.
    assert speicher.ereignisse == []


@pytest.mark.asyncio
async def test_stoppe_extern_kanal_nicht_gemappt_meldet_fehler():
    """T-0449: Mapping gefuellt, aber DIESER Kanal fehlt -> "ging nicht".

    Bis 28.07. war das ein `return True` ("nichts zu tun"). Damit wurde ein
    extern gestarteter Lauf auf einem unkonfigurierten Kanal als erledigt
    quittiert, obwohl niemand nachgesehen hat. `bewaessere()` lehnt denselben
    Zustand seit je ab (`test_bewaessere_kanal_nicht_im_mapping_lehnt_ab`) --
    die beiden Pfade duerfen hier nicht auseinanderlaufen.
    """
    client = ClientAttrappe()
    speicher = SpeicherAttrappe()
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
        kanal_zu_valve_id={2: "valve-2"},  # KANAL ist 1, also nicht gemappt
    )

    erfolg = await sicherung.stoppe(KANAL, Ausloser.MANUELL)

    assert erfolg is False
    assert client.schliessen_aufrufe == []  # Kanal nicht ansprechbar


@pytest.mark.asyncio
async def test_stoppe_smart_irrigation_reicht_valve_id_durch():
    """stoppe() leitet valve_id aus _aktiv an Client weiter."""
    client = ClientAttrappe()
    speicher = SpeicherAttrappe()
    valve_uuid = "abcd-1234"
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
        kanal_zu_valve_id={KANAL: valve_uuid},
    )
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL)
    speicher.ereignisse.clear()

    erfolg = await sicherung.stoppe(KANAL, Ausloser.MANUELL)

    assert erfolg is True
    assert client.schliessen_aufrufe == [(GERAET_ID, valve_uuid)]
    for e in speicher.ereignisse:
        assert e.aktion == VentilAktion.SCHLIESSEN
        assert e.ventil_id == valve_uuid


@pytest.mark.asyncio
async def test_lauf_gruppe_wird_von_oeffnen_auf_schliessen_getragen():
    """T-0335: bewaessere(lauf_gruppe, phase) markiert das OEFFNEN, und das
    spaetere SCHLIESSEN traegt dieselbe Gruppe/Phase (via AktiveBewaesserung)
    -> die Giess-Historie gruppiert das Paar konsistent zu einem Pre-Soak-Lauf.
    """
    sicherung, _client, speicher = erstelle_sicherung()
    await sicherung.bewaessere(
        KANAL, ZONEN, 600, Ausloser.MANUELL,
        lauf_gruppe="presoak_x", phase="haupt",
    )
    await sicherung.stoppe(KANAL, Ausloser.MANUELL)

    oeffnen = [e for e in speicher.ereignisse if e.aktion == VentilAktion.OEFFNEN]
    schliessen = [e for e in speicher.ereignisse if e.aktion == VentilAktion.SCHLIESSEN]
    assert oeffnen and schliessen
    for e in oeffnen + schliessen:
        assert e.lauf_gruppe == "presoak_x"
        assert e.phase == "haupt"


@pytest.mark.asyncio
async def test_einzellauf_ohne_marker_bleibt_ungruppiert():
    """Normaler Lauf (Auto-Loop/Manuell, kein Pre-Soak): lauf_gruppe/phase None
    auf OEFFNEN und SCHLIESSEN -> erscheint als Einzel-Lauf in der Historie."""
    sicherung, _client, speicher = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    await sicherung.stoppe(KANAL, Ausloser.AUTOMATIK)

    assert speicher.ereignisse
    for e in speicher.ereignisse:
        assert e.lauf_gruppe is None
        assert e.phase is None


@pytest.mark.asyncio
async def test_paralleles_stoppe_schreibt_nur_ein_schliessen_pro_zone():
    """T-0299: Zwei gleichzeitige stoppe()-Aufrufe (Race) duerfen den Lauf
    nur EINMAL schliessen -- genau ein SCHLIESSEN pro Zone, ein Cloud-Stop.
    Regression fuer den 27.05.-Doppel-Write (DB ids 2552-2555: bambuswald +
    bambuswald_yogaraum je 2x2972s watchdog, 68 ms Abstand)."""
    sicherung, client, speicher = erstelle_sicherung()

    # Cloud-Close kuenstlich verlangsamen, damit beide stoppe() im await
    # ueberlappen -- sonst laeuft der erste komplett durch (inkl. _aktiv-pop),
    # bevor der zweite startet, und der Race tritt gar nicht auf.
    original_schliessen = client.ventil_schliessen

    async def langsames_schliessen(geraet_id, valve_id=None):
        await asyncio.sleep(0.05)
        await original_schliessen(geraet_id, valve_id=valve_id)

    client.ventil_schliessen = langsames_schliessen  # type: ignore[assignment]

    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL)

    ergebnisse = await asyncio.gather(
        sicherung.stoppe(KANAL, Ausloser.WATCHDOG),
        sicherung.stoppe(KANAL, Ausloser.WATCHDOG),
    )

    schliessen = [
        e for e in speicher.ereignisse if e.aktion == VentilAktion.SCHLIESSEN
    ]
    assert len(schliessen) == len(ZONEN), (
        f"erwartet {len(ZONEN)} SCHLIESSEN (1/Zone), war {len(schliessen)}"
    )
    assert len(client.schliessen_aufrufe) == 1, (
        f"erwartet 1 Cloud-Stop, war {len(client.schliessen_aufrufe)}"
    )
    assert all(ergebnisse), "beide stoppe() sollen idempotent True liefern"


@pytest.mark.asyncio
async def test_t0331_kein_doppel_close_wenn_puls_schon_geschlossen():
    """T-0331: Der Cloud-Timer schloss den Puls bereits (realer Close in der DB);
    der zugehoerige Watchdog-`call_later`-Timer fror im System-Schlaf ein und
    feuert erst beim Wake nach -> stoppe(WATCHDOG). Der Idempotenz-Guard darf
    dann KEINEN zweiten, aufgeblaehten (Open->Wake) Close schreiben.
    Realfall 25.06.: magerwiese id 3678 (06:37 echt) + Phantom 3679 (08:47,
    dauer=9164)."""
    sicherung, client, speicher = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL)
    aktiv = sicherung._aktiv[KANAL]

    # Cloud-Timer hat den Puls bereits sauber geschlossen -> realer Close in DB.
    speicher.ereignisse.append(VentilEreignis(
        zeitstempel=aktiv.gestartet + timedelta(minutes=20),
        zone_id=ZONEN[0], ventil_id=aktiv.event_ventil_id,
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1200,
        ausloser=Ausloser.MANUELL,
    ))
    vorher = sum(
        1 for e in speicher.ereignisse if e.aktion == VentilAktion.SCHLIESSEN
    )

    # Eingefrorener Watchdog feuert beim Wake:
    erfolg = await sicherung.stoppe(KANAL, Ausloser.WATCHDOG)

    schliessen = [
        e for e in speicher.ereignisse if e.aktion == VentilAktion.SCHLIESSEN
    ]
    assert erfolg is True
    assert len(schliessen) == vorher, (
        "kein zweiter Close fuer bereits geschlossenen Puls (Idempotenz)"
    )
    assert not any(e.ausloser == Ausloser.WATCHDOG for e in schliessen), (
        "kein aufgeblaehter WATCHDOG-Phantom-Close"
    )


# --- Tests: bewaessere() ---

@pytest.mark.asyncio
async def test_bewaessere_erfolg():
    """Ventil oeffnen: Client-Call, _aktiv gesetzt, OEFFNEN-Event mit Dauer=0."""
    sicherung, client, speicher = erstelle_sicherung()

    erfolg = await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)

    assert erfolg is True
    assert client.oeffnen_aufrufe == [(GERAET_ID, 600, None)]
    assert sicherung.ist_aktiv(KANAL)
    # OEFFNEN-Events: eines pro Zone, dauer_sekunden=0
    assert len(speicher.ereignisse) == 2
    for e in speicher.ereignisse:
        assert e.aktion == VentilAktion.OEFFNEN
        assert e.dauer_sekunden == 0
        assert e.ausloser == Ausloser.AUTOMATIK


@pytest.mark.asyncio
async def test_bewaessere_bereits_aktiv():
    """Zweites Oeffnen auf gleichem Kanal wird abgelehnt."""
    sicherung, _, _ = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)

    erfolg = await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)

    assert erfolg is False


@pytest.mark.asyncio
async def test_bewaessere_ungueltige_dauer():
    """Dauer <= 0 wird abgelehnt."""
    sicherung, _, _ = erstelle_sicherung()

    assert await sicherung.bewaessere(KANAL, ZONEN, 0, Ausloser.AUTOMATIK) is False
    assert await sicherung.bewaessere(KANAL, ZONEN, -1, Ausloser.AUTOMATIK) is False


@pytest.mark.asyncio
async def test_bewaessere_client_fehler():
    """Client-Fehler beim Oeffnen: kein _aktiv, kein Event."""
    client = ClientAttrappe()
    client.ventil_oeffnen = AsyncMock(side_effect=ConnectionError("Timeout"))  # type: ignore[method-assign]
    speicher = SpeicherAttrappe()
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
    )

    erfolg = await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)

    assert erfolg is False
    assert not sicherung.ist_aktiv(KANAL)
    assert len(speicher.ereignisse) == 0


# --- Tests: T-0287 Frische-Gate (Automatik schaltet nicht auf blindem Zustand) ---

def _sicherung_mit_gate(beat: datetime | None, max_min: int | None = 120):
    """VentilSicherung mit Frische-Gate; letzter_gardena_beat gemockt."""
    client = ClientAttrappe()
    speicher = SpeicherAttrappe()
    speicher.letzter_gardena_beat = AsyncMock(return_value=beat)  # type: ignore[attr-defined]
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
        max_daten_alter_minuten=max_min,
    )
    return sicherung, client, speicher


@pytest.mark.asyncio
async def test_automatik_daten_blind_logik():
    """Helper: blind bei fehlendem oder zu altem Beat, frisch sonst."""
    jetzt = datetime(2026, 6, 3, 16, 38)
    # Gate aus -> nie blind.
    s_off, _, _ = _sicherung_mit_gate(beat=None, max_min=None)
    assert await s_off._automatik_daten_blind(jetzt) == (False, None)
    # Frischer Beat (5 min alt) -> nicht blind.
    s_fr, _, _ = _sicherung_mit_gate(beat=jetzt - timedelta(minutes=5))
    blind, alter = await s_fr._automatik_daten_blind(jetzt)
    assert blind is False and 4.0 < alter < 6.0
    # Alter Beat (5 h, > 120 min) -> blind.
    s_st, _, _ = _sicherung_mit_gate(beat=jetzt - timedelta(hours=5))
    blind, alter = await s_st._automatik_daten_blind(jetzt)
    assert blind is True and alter > 120
    # Kein Beat (gar keine Gardena-Daten) -> blind.
    s_no, _, _ = _sicherung_mit_gate(beat=None)
    assert await s_no._automatik_daten_blind(jetzt) == (True, None)


@pytest.mark.asyncio
async def test_bewaessere_automatik_blockt_bei_stale_daten():
    """T-0287: Automatik giesst NICHT, wenn Live-Daten stale (Blind-Fenster)."""
    sicherung, client, speicher = _sicherung_mit_gate(
        beat=datetime.now() - timedelta(hours=9),  # 9 h alt > 120 min
    )
    erfolg = await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    assert erfolg is False
    assert client.oeffnen_aufrufe == []  # Ventil NICHT geoeffnet
    assert not sicherung.ist_aktiv(KANAL)
    assert len(speicher.ereignisse) == 0


@pytest.mark.asyncio
async def test_bewaessere_manuell_nicht_vom_frische_gate_blockiert():
    """T-0287: manuelles Giessen ist bewusst ausgenommen -- User-Wille."""
    sicherung, client, _ = _sicherung_mit_gate(
        beat=datetime.now() - timedelta(hours=9),  # gleich stale
    )
    erfolg = await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL)
    assert erfolg is True
    assert client.oeffnen_aufrufe == [(GERAET_ID, 600, None)]


@pytest.mark.asyncio
async def test_bewaessere_automatik_erlaubt_bei_frischen_daten():
    """T-0287: bei frischen Live-Daten giesst die Automatik normal."""
    sicherung, client, _ = _sicherung_mit_gate(
        beat=datetime.now() - timedelta(minutes=10),  # frisch < 120 min
    )
    erfolg = await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    assert erfolg is True
    assert client.oeffnen_aufrufe == [(GERAET_ID, 600, None)]


# --- Tests: stoppe() ---

@pytest.mark.asyncio
async def test_stoppe_erfolg():
    """Erfolgreiches Schliessen: _aktiv geraeumt, SCHLIESSEN-Events, return True."""
    sicherung, client, speicher = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    speicher.ereignisse.clear()  # Nur SCHLIESSEN-Events zaehlen

    erfolg = await sicherung.stoppe(KANAL, Ausloser.AUTOMATIK)

    assert erfolg is True
    assert not sicherung.ist_aktiv(KANAL)
    assert len(client.schliessen_aufrufe) == 1
    # SCHLIESSEN-Events: eines pro Zone
    assert len(speicher.ereignisse) == 2
    for e in speicher.ereignisse:
        assert e.aktion == VentilAktion.SCHLIESSEN
        assert e.dauer_sekunden >= 0
        assert e.ausloser == Ausloser.AUTOMATIK


@pytest.mark.asyncio
async def test_stoppe_nicht_aktiv():
    """Stoppe auf inaktivem Kanal: True, keine Events."""
    sicherung, _, speicher = erstelle_sicherung()

    erfolg = await sicherung.stoppe(KANAL, Ausloser.MANUELL)

    assert erfolg is True
    assert len(speicher.ereignisse) == 0


@pytest.mark.asyncio
async def test_stoppe_fehler_aktiv_bleibt():
    """Close-Fehler: _aktiv bleibt, kein SCHLIESSEN-Event, return False."""
    sicherung, _, speicher = erstelle_sicherung(schliessen_fehler=True)
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    speicher.ereignisse.clear()

    erfolg = await sicherung.stoppe(KANAL, Ausloser.AUTOMATIK)

    assert erfolg is False
    assert sicherung.ist_aktiv(KANAL)  # Nicht aus _aktiv entfernt
    assert len(speicher.ereignisse) == 0  # Kein SCHLIESSEN geschrieben


@pytest.mark.asyncio
async def test_stoppe_fehler_retry_timer():
    """Close-Fehler: Retry-Timer wird gesetzt (neuer timer_handle)."""
    sicherung, _, _ = erstelle_sicherung(schliessen_fehler=True)
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    alter_timer = sicherung._aktiv[KANAL].timer_handle

    await sicherung.stoppe(KANAL, Ausloser.WATCHDOG)

    neuer_timer = sicherung._aktiv[KANAL].timer_handle
    assert neuer_timer is not None
    # Timer wurde ersetzt (alter gecancelt, neuer gesetzt)
    assert neuer_timer is not alter_timer


@pytest.mark.asyncio
async def test_stoppe_fehler_callback_nicht_unterdrueckt():
    """Close-Fehler: _unterdruecke_callback wird zurueckgenommen."""
    sicherung, _, _ = erstelle_sicherung(schliessen_fehler=True)
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)

    await sicherung.stoppe(KANAL, Ausloser.AUTOMATIK)

    assert GERAET_ID not in sicherung._unterdruecke_callback


@pytest.mark.asyncio
async def test_stoppe_fehler_aber_ventil_bereits_zu_raeumt_state():
    """T-0380: Close-Befehl scheitert, ABER ein SCHLIESSEN existiert bereits (die
    WS-Pipeline trug den realen Cloud-Close nach) -> Ventil ist nachweislich zu ->
    State raeumen (kein endloser Retry gegen ein zu-Ventil). Realfall 02.07.:
    Watchdog-Close scheiterte am schon-zu-Ventil -> live_lauf_state hing 12h ->
    Geister-'laeuft' in der UI."""
    from datetime import timedelta

    sicherung, _, speicher = erstelle_sicherung(schliessen_fehler=True)
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    aktiv = sicherung._aktiv[KANAL]
    speicher.ereignisse.clear()
    speicher.geloeschte_states.clear()
    # WS-Pipeline hat den realen Cloud-Close nachgetragen (SCHLIESSEN seit Start):
    speicher.ereignisse.append(VentilEreignis(
        zeitstempel=aktiv.gestartet + timedelta(seconds=1),
        zone_id=ZONEN[0],
        ventil_id=aktiv.event_ventil_id,
        aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=600,
        ausloser=Ausloser.AUTOMATIK,
    ))

    erfolg = await sicherung.stoppe(KANAL, Ausloser.WATCHDOG)

    assert erfolg is True                    # Lauf als sauber beendet behandelt
    assert not sicherung.ist_aktiv(KANAL)    # _aktiv geraeumt -> kein Retry-Haenger
    assert (KANAL, GERAET_ID) in speicher.geloeschte_states  # live_lauf_state weg


@pytest.mark.asyncio
async def test_stoppe_fehler_ohne_close_beleg_bleibt_retry():
    """T-0380-Gegenprobe: Close-Fehler OHNE existierendes SCHLIESSEN -> Ventil
    koennte real noch offen sein -> Retry bleibt, State NICHT geraeumt (Safety)."""
    sicherung, _, speicher = erstelle_sicherung(schliessen_fehler=True)
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    speicher.ereignisse.clear()  # kein Close-Beleg
    speicher.geloeschte_states.clear()

    erfolg = await sicherung.stoppe(KANAL, Ausloser.WATCHDOG)

    assert erfolg is False
    assert sicherung.ist_aktiv(KANAL)        # bleibt aktiv (Ventil evtl. offen)
    assert (KANAL, GERAET_ID) not in speicher.geloeschte_states


# --- Tests: notfall_stopp() ---

@pytest.mark.asyncio
async def test_notfall_stopp_erfolg():
    """Alle Kanaele geschlossen: geschlossen > 0, fehlgeschlagen leer."""
    sicherung, _, _ = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)

    ergebnis = await sicherung.notfall_stopp()

    assert ergebnis["geschlossen"] == 1
    assert ergebnis["fehlgeschlagen"] == []
    assert not sicherung.ist_aktiv(KANAL)


@pytest.mark.asyncio
async def test_notfall_stopp_fehler():
    """Close-Fehler: fehlgeschlagen enthaelt Kanal, _aktiv bleibt."""
    sicherung, _, _ = erstelle_sicherung(schliessen_fehler=True)
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)

    ergebnis = await sicherung.notfall_stopp()

    assert ergebnis["geschlossen"] == 0
    assert ergebnis["fehlgeschlagen"] == [{
        "geraet_id": GERAET_ID,
        "kanal": KANAL,
        "quelle": "backend",
    }]
    assert sicherung.ist_aktiv(KANAL)  # Ventil bleibt als offen markiert


@pytest.mark.asyncio
async def test_notfall_stopp_schliesst_extern_offenes_mapped_valve():
    """Extern offene App-/Schedule-Valves werden beim Notfall mitgeschlossen."""
    client = ClientAttrappe()
    speicher = SpeicherAttrappe()
    client.offene = {"valve-extern": {"activity": "MANUAL_WATERING"}}
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
        kanal_zu_valve_id={KANAL: "valve-extern"},
    )

    ergebnis = await sicherung.notfall_stopp()

    assert ergebnis["geschlossen"] == 1
    assert ergebnis["fehlgeschlagen"] == []
    assert client.schliessen_aufrufe == [(GERAET_ID, "valve-extern")]


@pytest.mark.asyncio
async def test_notfall_stopp_leer():
    """Keine aktiven Kanaele: leer, kein Fehler."""
    sicherung, _, _ = erstelle_sicherung()

    ergebnis = await sicherung.notfall_stopp()

    assert ergebnis["geschlossen"] == 0
    assert ergebnis["fehlgeschlagen"] == []


# --- Tests: verarbeite_callback() ---

@pytest.mark.asyncio
async def test_callback_oeffnen_unterdrueckt():
    """OEFFNEN-Callback wird unterdrueckt wenn VentilSicherung aktiv ist."""
    sicherung, _, _ = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)

    ereignis = VentilEreignis(
        zeitstempel=datetime.now(), zone_id="bambuswald",
        ventil_id=GERAET_ID, aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=0, ausloser=Ausloser.MANUELL,
    )
    behandelt = await sicherung.verarbeite_callback(ereignis)

    assert behandelt is True


@pytest.mark.asyncio
async def test_callback_oeffnen_extern():
    """Externes OEFFNEN (kein aktiver Kanal): nicht behandelt."""
    sicherung, _, _ = erstelle_sicherung()

    ereignis = VentilEreignis(
        zeitstempel=datetime.now(), zone_id="bambuswald",
        ventil_id=GERAET_ID, aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=0, ausloser=Ausloser.MANUELL,
    )
    behandelt = await sicherung.verarbeite_callback(ereignis)

    assert behandelt is False


@pytest.mark.asyncio
async def test_callback_schliessen_normal():
    """Normales SCHLIESSEN (GardenaClient-Timer): _aktiv geraeumt, Events geschrieben."""
    sicherung, _, speicher = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    speicher.ereignisse.clear()

    ereignis = VentilEreignis(
        zeitstempel=datetime.now(), zone_id="bambuswald",
        ventil_id=GERAET_ID, aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=600, ausloser=Ausloser.MANUELL,
    )
    behandelt = await sicherung.verarbeite_callback(ereignis)

    assert behandelt is True
    assert not sicherung.ist_aktiv(KANAL)
    # SCHLIESSEN-Events mit AUTOMATIK-Ausloser (nicht MANUELL)
    assert len(speicher.ereignisse) == 2
    for e in speicher.ereignisse:
        assert e.aktion == VentilAktion.SCHLIESSEN
        assert e.ausloser == Ausloser.AUTOMATIK


@pytest.mark.asyncio
async def test_callback_schliessen_schreibt_keinen_duplikat_close():
    """T-0361: WS-Close nach bereits persistiertem Close darf nicht doppeln."""
    sicherung, _, speicher = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    aktiv = sicherung._aktiv[KANAL]
    for zone_id in ZONEN:
        speicher.ereignisse.append(VentilEreignis(
            zeitstempel=aktiv.gestartet + timedelta(minutes=10),
            zone_id=zone_id,
            ventil_id=aktiv.event_ventil_id,
            aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=600,
            ausloser=Ausloser.MANUELL,
        ))
    vorher = sum(
        1 for e in speicher.ereignisse if e.aktion == VentilAktion.SCHLIESSEN
    )

    ereignis = VentilEreignis(
        zeitstempel=datetime.now(), zone_id=ZONEN[0],
        ventil_id=GERAET_ID, aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=600, ausloser=Ausloser.MANUELL,
    )
    behandelt = await sicherung.verarbeite_callback(ereignis)

    nachher = sum(
        1 for e in speicher.ereignisse if e.aktion == VentilAktion.SCHLIESSEN
    )
    assert behandelt is True
    assert nachher == vorher
    assert not sicherung.ist_aktiv(KANAL)


@pytest.mark.asyncio
async def test_callback_schliessen_duplikat_guard_ist_zonenscharf():
    """T-0361: Ein Close fuer Zone A darf Zone B nicht verschlucken."""
    sicherung, _, speicher = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    aktiv = sicherung._aktiv[KANAL]
    speicher.ereignisse.append(VentilEreignis(
        zeitstempel=aktiv.gestartet + timedelta(minutes=10),
        zone_id=ZONEN[0],
        ventil_id=aktiv.event_ventil_id,
        aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=600,
        ausloser=Ausloser.MANUELL,
    ))
    vorher = sum(
        1 for e in speicher.ereignisse if e.aktion == VentilAktion.SCHLIESSEN
    )

    ereignis = VentilEreignis(
        zeitstempel=datetime.now(), zone_id=ZONEN[0],
        ventil_id=GERAET_ID, aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=600, ausloser=Ausloser.MANUELL,
    )
    behandelt = await sicherung.verarbeite_callback(ereignis)

    neue_closes = [
        e for e in speicher.ereignisse
        if e.aktion == VentilAktion.SCHLIESSEN
    ]
    assert behandelt is True
    assert len(neue_closes) == vorher + 1
    assert [e.zone_id for e in neue_closes].count(ZONEN[0]) == 1
    assert [e.zone_id for e in neue_closes].count(ZONEN[1]) == 1


@pytest.mark.asyncio
async def test_existiert_schliessen_seit_prueft_alle_zonen(tmp_path):
    """T-0361: Der echte SQL-Guard ist bei Multi-Zonen-Kanaelen zonenscharf."""
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "multi_close_guard.db"))
    await sp.verbinden()
    try:
        gestartet = datetime.now() - timedelta(minutes=20)
        await sp.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=gestartet + timedelta(minutes=10),
            zone_id=ZONEN[0],
            ventil_id="ventil-multi",
            aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=600,
            ausloser=Ausloser.MANUELL,
        ))

        assert not await sp.existiert_schliessen_seit(
            "ventil-multi", gestartet, ZONEN,
        )

        await sp.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=gestartet + timedelta(minutes=10),
            zone_id=ZONEN[1],
            ventil_id="ventil-multi",
            aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=600,
            ausloser=Ausloser.MANUELL,
        ))

        assert await sp.existiert_schliessen_seit(
            "ventil-multi", gestartet, ZONEN,
        )
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_callback_mapped_valve_id_unterdrueckt_und_synchronisiert():
    """Realvertrag: GardenaClient emittiert valve_id, nicht device_id."""
    client = ClientAttrappe()
    speicher = SpeicherAttrappe()
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
        kanal_zu_valve_id={KANAL: "valve-1"},
    )
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)

    oeffnen = VentilEreignis(
        zeitstempel=datetime.now(), zone_id="bambuswald",
        ventil_id="valve-1", aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=0, ausloser=Ausloser.MANUELL,
    )
    assert await sicherung.verarbeite_callback(oeffnen) is True

    speicher.ereignisse.clear()
    schliessen = VentilEreignis(
        zeitstempel=datetime.now(), zone_id="bambuswald",
        ventil_id="valve-1", aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=600, ausloser=Ausloser.MANUELL,
    )
    assert await sicherung.verarbeite_callback(schliessen) is True
    assert not sicherung.ist_aktiv(KANAL)
    assert {e.ventil_id for e in speicher.ereignisse} == {"valve-1"}


@pytest.mark.asyncio
async def test_callback_schliessen_nach_stoppe():
    """SCHLIESSEN-Callback nach stoppe(): unterdrueckt (stoppe hat Events geschrieben)."""
    sicherung, _, speicher = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    await sicherung.stoppe(KANAL, Ausloser.WATCHDOG)
    speicher.ereignisse.clear()

    ereignis = VentilEreignis(
        zeitstempel=datetime.now(), zone_id="bambuswald",
        ventil_id=GERAET_ID, aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=600, ausloser=Ausloser.MANUELL,
    )
    behandelt = await sicherung.verarbeite_callback(ereignis)

    assert behandelt is True
    assert len(speicher.ereignisse) == 0  # Kein Duplikat


@pytest.mark.asyncio
async def test_callback_schliessen_extern():
    """Externes SCHLIESSEN (kein aktiver Kanal): nicht behandelt."""
    sicherung, _, _ = erstelle_sicherung()

    ereignis = VentilEreignis(
        zeitstempel=datetime.now(), zone_id="bambuswald",
        ventil_id=GERAET_ID, aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=300, ausloser=Ausloser.MANUELL,
    )
    behandelt = await sicherung.verarbeite_callback(ereignis)

    assert behandelt is False


@pytest.mark.asyncio
async def test_callback_anderes_geraet():
    """Callback fuer anderes Geraet: immer False."""
    sicherung, _, _ = erstelle_sicherung()

    ereignis = VentilEreignis(
        zeitstempel=datetime.now(), zone_id="bambuswald",
        ventil_id="anderes-geraet", aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=300, ausloser=Ausloser.MANUELL,
    )
    behandelt = await sicherung.verarbeite_callback(ereignis)

    assert behandelt is False


# --- Tests: Dual-Channel-Sicherheit (Bug 1) ---

@pytest.mark.asyncio
async def test_callback_oeffnen_anderer_kanal_nicht_unterdrueckt():
    """Dual-Channel: Kanal 1 aktiv, OEFFNEN fuer Zone an Kanal 2 wird NICHT unterdrueckt.

    Smart Dual Water Control hat EIN Ventil mit 2 Kanaelen. Wenn VentilSicherung
    Kanal 1 verwaltet (Zonen A+B) und die Gardena-App Kanal 2 (Zone C) oeffnet,
    darf das OEFFNEN-Event fuer Zone C nicht unterdrueckt werden.
    """
    sicherung, _, _ = erstelle_sicherung()
    # Kanal 1 aktiv mit zwei Zonen (bambuswald + yogaraum)
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)

    # Externes OEFFNEN auf Kanal 2 (andere Zone, gleiches Ventil)
    ereignis_fremd = VentilEreignis(
        zeitstempel=datetime.now(), zone_id="waldblumenhain",
        ventil_id=GERAET_ID, aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=0, ausloser=Ausloser.MANUELL,
    )
    behandelt = await sicherung.verarbeite_callback(ereignis_fremd)
    assert behandelt is False  # main.py soll das Event speichern

    # OEFFNEN fuer eine von VentilSicherung verwaltete Zone bleibt unterdrueckt
    ereignis_eigen = VentilEreignis(
        zeitstempel=datetime.now(), zone_id="bambuswald",
        ventil_id=GERAET_ID, aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=0, ausloser=Ausloser.MANUELL,
    )
    behandelt = await sicherung.verarbeite_callback(ereignis_eigen)
    assert behandelt is True


@pytest.mark.asyncio
async def test_callback_schliessen_anderer_kanal_nicht_synchronisiert():
    """Dual-Channel: Kanal 1 aktiv, SCHLIESSEN fuer Kanal 2 laesst Kanal 1 in Ruhe."""
    sicherung, _, speicher = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    speicher.ereignisse.clear()

    # SCHLIESSEN fuer eine Zone, die nicht in _aktiv ist (anderer Kanal)
    ereignis = VentilEreignis(
        zeitstempel=datetime.now(), zone_id="waldblumenhain",
        ventil_id=GERAET_ID, aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=300, ausloser=Ausloser.MANUELL,
    )
    behandelt = await sicherung.verarbeite_callback(ereignis)

    assert behandelt is False  # main.py soll das Event speichern
    # Kanal 1 ist weiter aktiv — nicht faelschlich synchronisiert
    assert sicherung.ist_aktiv(KANAL)
    assert len(speicher.ereignisse) == 0


# --- Tests: Retry-Limit (Bug 2) ---

@pytest.mark.asyncio
async def test_stoppe_retry_limit_erreicht():
    """Nach MAX_RETRY_VERSUCHE Fehlversuchen wird kein neuer Timer gesetzt."""
    from bewaesserung.ventil_sicherung import MAX_RETRY_VERSUCHE

    sicherung, _, _ = erstelle_sicherung(schliessen_fehler=True)
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)

    # Erste Fehlversuche: Retry-Timer wird jeweils gesetzt
    for versuch in range(1, MAX_RETRY_VERSUCHE):
        erfolg = await sicherung.stoppe(KANAL, Ausloser.AUTOMATIK)
        assert erfolg is False
        assert sicherung.ist_aktiv(KANAL)
        assert sicherung._aktiv[KANAL].timer_handle is not None
        assert sicherung._aktiv[KANAL].retry_versuche == versuch

    # Letzter Versuch: Limit erreicht → kein neuer Timer
    erfolg = await sicherung.stoppe(KANAL, Ausloser.AUTOMATIK)
    assert erfolg is False
    assert sicherung.ist_aktiv(KANAL)  # _aktiv bleibt, Nutzer muss eingreifen
    assert sicherung._aktiv[KANAL].timer_handle is None
    assert sicherung._aktiv[KANAL].retry_versuche == MAX_RETRY_VERSUCHE


# --- Tests: aktive_bewaesserungen() ---

@pytest.mark.asyncio
async def test_aktive_bewaesserungen_format():
    """API-Dict hat korrekte Felder."""
    sicherung, _, _ = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)

    status = sicherung.aktive_bewaesserungen()

    assert KANAL in status
    info = status[KANAL]
    assert info["zone_ids"] == ZONEN
    assert info["dauer_s"] == 600
    assert info["ausloser"] == "automatik"
    assert "gestartet" in info
    assert "verbleibend_s" in info
    assert info["verbleibend_s"] <= 600


# --- T-0115: Live-Lauf-Recovery aus DB-State ---

@pytest.mark.asyncio
async def test_recover_aus_db_setzt_aktiv_und_watchdog(tmp_path):
    """Recovery: persistierter State -> _aktiv + Watchdog mit Restzeit."""
    from datetime import datetime, timedelta
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "s.db"))
    await sp.verbinden()
    try:
        # State: Bewaesserung lief vor 10 min, Dauer 90 min -> 80 min Rest
        gestartet = datetime.now() - timedelta(minutes=10)
        await sp.setze_live_lauf_state(
            kanal=1, valve_id="valve-X", geraet_id=GERAET_ID,
            zone_ids=["waldblumenhain"], dauer_sekunden=5400,
            ausloser="manuell", gestartet_am=gestartet,
        )
        client = ClientAttrappe()
        sicherung = VentilSicherung(
            client=client, speicher=sp,  # type: ignore[arg-type]
            ventil_geraet_id=GERAET_ID,
            kanal_zu_valve_id={1: "valve-X"},
        )
        n = await sicherung.recover_aus_db()
        assert n == 1
        assert sicherung.ist_aktiv(1)
        aktiv = sicherung._aktiv[1]
        assert aktiv.valve_id == "valve-X"
        assert aktiv.zone_ids == ["waldblumenhain"]
        assert aktiv.dauer_s == 5400
        assert aktiv.timer_handle is not None  # Watchdog gesetzt
        # Stop wieder, damit Cleanup laeuft (Timer canceln)
        if aktiv.timer_handle:
            aktiv.timer_handle.cancel()
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_recover_aus_db_skip_abgelaufen(tmp_path):
    """Wenn dauer schon ueberschritten (Cloud hat geschlossen), State weg."""
    from datetime import datetime, timedelta
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "s.db"))
    await sp.verbinden()
    try:
        gestartet = datetime.now() - timedelta(hours=2)
        await sp.setze_live_lauf_state(
            kanal=1, valve_id="vid", geraet_id=GERAET_ID,
            zone_ids=["waldblumenhain"], dauer_sekunden=300,  # 5 min
            ausloser="manuell", gestartet_am=gestartet,
        )
        client = ClientAttrappe()
        sicherung = VentilSicherung(
            client=client, speicher=sp,  # type: ignore[arg-type]
            ventil_geraet_id=GERAET_ID,
            kanal_zu_valve_id={1: "vid"},
        )
        n = await sicherung.recover_aus_db()
        assert n == 0
        # State wurde geloescht
        assert await sp.hole_live_lauf_states() == []
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_persistiert_bei_bewaessere_und_loescht_bei_stoppe(tmp_path):
    """End-to-End: bewaessere persistiert, stoppe loescht den State."""
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "s.db"))
    await sp.verbinden()
    try:
        client = ClientAttrappe()
        sicherung = VentilSicherung(
            client=client, speicher=sp,  # type: ignore[arg-type]
            ventil_geraet_id=GERAET_ID,
            kanal_zu_valve_id={1: "vid"},
        )
        await sicherung.bewaessere(1, ["waldblumenhain"], 600, Ausloser.MANUELL)
        states = await sp.hole_live_lauf_states()
        assert len(states) == 1
        assert states[0]["kanal"] == 1
        assert states[0]["valve_id"] == "vid"
        assert states[0]["dauer_sekunden"] == 600

        await sicherung.stoppe(1, Ausloser.MANUELL)
        states_nach = await sp.hole_live_lauf_states()
        assert states_nach == []
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_live_lauf_state_multi_dswc_gleicher_kanal_isoliert(tmp_path):
    """Gleiche Kanalnummern verschiedener DSWCs duerfen sich nicht ueberschreiben."""
    from datetime import datetime, timedelta
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "multi.db"))
    await sp.verbinden()
    try:
        gestartet = datetime.now() - timedelta(minutes=1)
        await sp.setze_live_lauf_state(
            kanal=1, valve_id="v-a", geraet_id="dswc-a",
            zone_ids=["zone-a"], dauer_sekunden=600,
            ausloser="manuell", gestartet_am=gestartet,
        )
        await sp.setze_live_lauf_state(
            kanal=1, valve_id="v-b", geraet_id="dswc-b",
            zone_ids=["zone-b"], dauer_sekunden=600,
            ausloser="manuell", gestartet_am=gestartet,
        )

        assert len(await sp.hole_live_lauf_states()) == 2
        assert [s["geraet_id"] for s in await sp.hole_live_lauf_states("dswc-a")] == ["dswc-a"]

        sicherung_a = VentilSicherung(
            client=ClientAttrappe(), speicher=sp,  # type: ignore[arg-type]
            ventil_geraet_id="dswc-a", kanal_zu_valve_id={1: "v-a"},
        )
        sicherung_b = VentilSicherung(
            client=ClientAttrappe(), speicher=sp,  # type: ignore[arg-type]
            ventil_geraet_id="dswc-b", kanal_zu_valve_id={1: "v-b"},
        )
        assert await sicherung_a.recover_aus_db() == 1
        assert await sicherung_b.recover_aus_db() == 1
        assert sicherung_a._aktiv[1].zone_ids == ["zone-a"]
        assert sicherung_b._aktiv[1].zone_ids == ["zone-b"]
        for s in (sicherung_a, sicherung_b):
            if s._aktiv[1].timer_handle:
                s._aktiv[1].timer_handle.cancel()
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_t0276_startup_check_belaesst_aktiven_cloud_lauf(tmp_path, monkeypatch):
    """T-0276: Wenn die Cloud das Ventil als OFFEN meldet (regulaer
    laufender User-/Schedule-Lauf), darf `startup_check()` NICHT
    schliessen. Der State bleibt erhalten und `recover_aus_db()`
    rekonstruiert `_aktiv` + Watchdog-Timer.
    """
    from datetime import datetime, timedelta
    from bewaesserung.speicher import Speicher

    # asyncio.sleep im Test instant -- vermeidet 2 s Echtwartezeit.
    import asyncio as _async
    _orig_sleep = _async.sleep
    monkeypatch.setattr(
        _async, "sleep", lambda *_a, **_kw: _orig_sleep(0),
    )

    sp = Speicher(str(tmp_path / "startup_cloud_offen.db"))
    await sp.verbinden()
    try:
        gestartet = datetime.now() - timedelta(minutes=10)
        await sp.setze_live_lauf_state(
            kanal=1, valve_id="valve-startup", geraet_id=GERAET_ID,
            zone_ids=["waldblumenhain"], dauer_sekunden=3600,
            ausloser="manuell", gestartet_am=gestartet,
        )
        client = ClientAttrappe()
        # Cloud meldet: laeuft noch (z.B. manueller App-Lauf).
        client.offene = {
            "valve-startup": {
                "activity": "MANUAL_WATERING",
                "offen_seit": gestartet,
            },
        }
        sicherung = VentilSicherung(
            client=client, speicher=sp,  # type: ignore[arg-type]
            ventil_geraet_id=GERAET_ID,
            kanal_zu_valve_id={1: "valve-startup"},
        )

        await sicherung.startup_check()
        # NICHT geschlossen.
        assert client.schliessen_aufrufe == []
        # State noch da -- recover_aus_db kann rekonstruieren.
        assert len(await sp.hole_live_lauf_states(GERAET_ID)) == 1
        assert await sicherung.recover_aus_db() == 1
        assert sicherung._aktiv[1].zone_ids == ["waldblumenhain"]
        # Aufraeumen.
        if sicherung._aktiv[1].timer_handle:
            sicherung._aktiv[1].timer_handle.cancel()
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_t0276_startup_check_raeumt_state_auf_wenn_cloud_zu(tmp_path, monkeypatch):
    """T-0276: Wenn die Cloud das Ventil als GESCHLOSSEN meldet (Lauf
    ist waehrend Backend-Down regulaer geendet), wird der State
    aufgeraeumt und ein synthetisches SCHLIESSEN-Event geschrieben --
    aber KEIN aktiver Close-Call ans Geraet.
    """
    from datetime import datetime, timedelta
    from bewaesserung.speicher import Speicher

    import asyncio as _async
    _orig_sleep = _async.sleep
    monkeypatch.setattr(
        _async, "sleep", lambda *_a, **_kw: _orig_sleep(0),
    )

    sp = Speicher(str(tmp_path / "startup_cloud_zu.db"))
    await sp.verbinden()
    try:
        gestartet = datetime.now() - timedelta(minutes=10)
        await sp.setze_live_lauf_state(
            kanal=1, valve_id="valve-startup", geraet_id=GERAET_ID,
            zone_ids=["waldblumenhain"], dauer_sekunden=3600,
            ausloser="manuell", gestartet_am=gestartet,
        )
        client = ClientAttrappe()
        # Cloud kennt die valve nicht als offen.
        client.offene = {}
        sicherung = VentilSicherung(
            client=client, speicher=sp,  # type: ignore[arg-type]
            ventil_geraet_id=GERAET_ID,
            kanal_zu_valve_id={1: "valve-startup"},
        )

        await sicherung.startup_check()
        # KEIN aktiver Schliess-Call -- Cloud ist eh zu.
        assert client.schliessen_aufrufe == []
        # State aufgeraeumt.
        assert await sp.hole_live_lauf_states(GERAET_ID) == []
        # recover_aus_db findet nichts mehr -> kein doppelter _aktiv.
        assert await sicherung.recover_aus_db() == 0
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_t0361_startup_check_unbekannt_behaelt_state(tmp_path):
    """T-0361: Kein Initialstatus ist unbekannt, nicht 'Cloud geschlossen'."""
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "startup_unbekannt.db"))
    await sp.verbinden()
    try:
        gestartet = datetime.now() - timedelta(minutes=10)
        await sp.setze_live_lauf_state(
            kanal=1, valve_id="valve-startup", geraet_id=GERAET_ID,
            zone_ids=["waldblumenhain"], dauer_sekunden=3600,
            ausloser="manuell", gestartet_am=gestartet,
        )
        client = ClientStartupUnbekannt()
        sicherung = VentilSicherung(
            client=client, speicher=sp,  # type: ignore[arg-type]
            ventil_geraet_id=GERAET_ID,
            kanal_zu_valve_id={1: "valve-startup"},
        )

        await sicherung.startup_check()

        assert await sp.hole_live_lauf_states(GERAET_ID)
        ereignisse = await sp.hole_ventil_ereignisse("waldblumenhain")
        assert [e for e in ereignisse if e.aktion == VentilAktion.SCHLIESSEN] == []
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_t0338_kein_duplikat_close_wenn_dhs_close_schon_da(tmp_path, monkeypatch):
    """T-0338: Der DHS-Backfill trug den von der Live-WS verpassten Close nach,
    loeschte aber live_lauf_state nicht. Beim Restart darf der Startup-Cleanup
    KEINEN zweiten (Watchdog-)Close schreiben (Duplikat + Bilanz-Doppelzaehlung),
    sondern nur den State raeumen -- Idempotenz-Guard existiert_schliessen_seit."""
    from bewaesserung.speicher import Speicher

    _orig_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_kw: _orig_sleep(0))

    sp = Speicher(str(tmp_path / "t0338.db"))
    await sp.verbinden()
    try:
        gestartet = datetime.now() - timedelta(minutes=50)
        await sp.setze_live_lauf_state(
            kanal=1, valve_id="valve-startup", geraet_id=GERAET_ID,
            zone_ids=["waldblumenhain"], dauer_sekunden=2700,
            ausloser="manuell", gestartet_am=gestartet,
        )
        # DHS-Backfill hat den fehlenden Close bereits nachgetragen (ventil_id =
        # valve-UUID, Zeitstempel nach dem Lauf-Start), live_lauf_state aber NICHT
        # geloescht -> der Startup-Cleanup wuerde ohne Guard doppelt schliessen.
        await sp.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=gestartet + timedelta(minutes=45),
            zone_id="waldblumenhain", ventil_id="valve-startup",
            aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=2700,
            ausloser=Ausloser.MANUELL,
        ))
        client = ClientAttrappe()
        client.offene = {}  # Cloud zu
        sicherung = VentilSicherung(
            client=client, speicher=sp,  # type: ignore[arg-type]
            ventil_geraet_id=GERAET_ID,
            kanal_zu_valve_id={1: "valve-startup"},
        )

        await sicherung.startup_check()

        # Genau EIN SCHLIESSEN (der DHS-Close), KEIN zweiter Watchdog-Close.
        alle = await sp.hole_ventil_ereignisse("waldblumenhain")
        closes = [e for e in alle if e.aktion == VentilAktion.SCHLIESSEN]
        assert len(closes) == 1, "Startup-Cleanup darf keinen Duplikat-Close schreiben"
        assert closes[0].ausloser == Ausloser.MANUELL  # der DHS-Close, nicht WATCHDOG
        # State trotzdem geraeumt.
        assert await sp.hole_live_lauf_states(GERAET_ID) == []
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_f7_startup_check_cappt_synthetische_dauer_auf_geplant(
    tmp_path, monkeypatch,
):
    """F7: Bei langer Downtime (5h) auf einem 10-min-Lauf darf das
    synthetische startup-SCHLIESSEN nicht die ganze verstrichene Zeit
    (18000s) als Dauer schreiben -- gecappt auf die geplante Dauer (600s).
    Sonst vergiftet der 18000s-Wert ML-Features/Budget/Bilanz."""
    from bewaesserung.speicher import Speicher

    import asyncio as _async
    _orig_sleep = _async.sleep
    monkeypatch.setattr(_async, "sleep", lambda *_a, **_kw: _orig_sleep(0))

    sp = Speicher(str(tmp_path / "startup_cap.db"))
    await sp.verbinden()
    try:
        gestartet = datetime.now() - timedelta(hours=5)
        await sp.setze_live_lauf_state(
            kanal=1, valve_id="valve-startup", geraet_id=GERAET_ID,
            zone_ids=["waldblumenhain"], dauer_sekunden=600,  # 10 min geplant
            ausloser="manuell", gestartet_am=gestartet,
        )
        client = ClientAttrappe()
        client.offene = {}  # Cloud zu -> synthetischer Close-Pfad
        sicherung = VentilSicherung(
            client=client, speicher=sp,  # type: ignore[arg-type]
            ventil_geraet_id=GERAET_ID,
            kanal_zu_valve_id={1: "valve-startup"},
        )

        await sicherung.startup_check()

        ereignisse = await sp.hole_ventil_ereignisse(
            "waldblumenhain",
            von=gestartet - timedelta(minutes=1),
            bis=datetime.now() + timedelta(minutes=1),
        )
        schliessen = [
            e for e in ereignisse if e.aktion == VentilAktion.SCHLIESSEN
        ]
        assert len(schliessen) == 1
        assert schliessen[0].dauer_sekunden <= 600, (
            "synthetische Dauer muss auf die geplante Dauer (600s) gecappt "
            f"sein, war {schliessen[0].dauer_sekunden}"
        )
    finally:
        await sp.schliessen()


# --- T-0151: Hahn-Cluster-Lock (Pre-flight Durchfluss-Budget) ---

def _baue_sicherung_mit_cluster(
    *,
    cluster_max_lpm: dict[str, float],
    kanal_profile: dict[int, list[tuple]],
    kanal_zu_valve_id: dict[int, str] | None = None,
) -> tuple[VentilSicherung, ClientAttrappe, SpeicherAttrappe]:
    """Hilfsfunktion: akzeptiert sowohl 3er-Tupel `(zone_id, cluster, lpm)`
    (Default exklusiv=False) als auch 4er-Tupel `(zone_id, cluster, lpm,
    exklusiv)` — vermeidet Boilerplate in den Tests, die ohne exklusiv
    auskommen.
    """
    profile_4er: dict[int, list[tuple[str, str | None, float | None, bool]]] = {}
    for kanal, eintraege in kanal_profile.items():
        normiert: list[tuple[str, str | None, float | None, bool]] = []
        for e in eintraege:
            if len(e) == 3:
                normiert.append((e[0], e[1], e[2], False))
            elif len(e) == 4:
                normiert.append(e)
            else:
                raise ValueError(f"Unerwartetes Lockprofile-Tupel: {e!r}")
        profile_4er[kanal] = normiert
    client = ClientAttrappe()
    speicher = SpeicherAttrappe()
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
        kanal_zu_valve_id=kanal_zu_valve_id,
        kanal_zu_zone_lockprofile=profile_4er,
        cluster_max_lpm=cluster_max_lpm,
    )
    return sicherung, client, speicher


@pytest.mark.asyncio
async def test_hahn_cluster_erlaubt_zwei_mikrodrip_parallel():
    """Bambus (1.87) + Hecke (1.5) = 3.37 < Budget 4.5 -> beide erlaubt."""
    sicherung, client, _ = _baue_sicherung_mit_cluster(
        cluster_max_lpm={"standort_a": 4.5},
        kanal_profile={
            2: [("bambuswald", "standort_a", 1.87)],
            3: [("hecke", "standort_a", 1.5)],
        },
        kanal_zu_valve_id={2: "vid-2", 3: "vid-3"},
    )
    ok1 = await sicherung.bewaessere(2, ["bambuswald"], 600, Ausloser.MANUELL)
    ok2 = await sicherung.bewaessere(3, ["hecke"], 600, Ausloser.MANUELL)
    assert ok1 is True
    assert ok2 is True
    assert sicherung.ist_aktiv(2)
    assert sicherung.ist_aktiv(3)


@pytest.mark.asyncio
async def test_hahn_cluster_blockt_sprinkler_gegen_mikrodrip():
    """Bambus (1.87) laeuft, Waldblumen (6.0) wuerde Budget sprengen -> Reject."""
    sicherung, client, _ = _baue_sicherung_mit_cluster(
        cluster_max_lpm={"standort_a": 4.5},
        kanal_profile={
            2: [("bambuswald", "standort_a", 1.87)],
            1: [("waldblumenhain", "standort_a", 6.0)],
        },
        kanal_zu_valve_id={1: "vid-1", 2: "vid-2"},
    )
    ok_bambus = await sicherung.bewaessere(2, ["bambuswald"], 600, Ausloser.MANUELL)
    assert ok_bambus is True
    ok_wald = await sicherung.bewaessere(
        1, ["waldblumenhain"], 600, Ausloser.MANUELL,
    )
    assert ok_wald is False
    # Bambus laeuft weiter, Waldblumen wurde abgewiesen.
    assert sicherung.ist_aktiv(2)
    assert not sicherung.ist_aktiv(1)
    # Client wurde fuer Waldblumen NICHT angerufen.
    assert len(client.oeffnen_aufrufe) == 1
    assert client.oeffnen_aufrufe[0][2] == "vid-2"


@pytest.mark.asyncio
async def test_hahn_cluster_blockt_sprinkler_solo_zu_gross():
    """Sprinkler (6.0) > Budget (4.5) -> auch alleine abgewiesen."""
    sicherung, client, _ = _baue_sicherung_mit_cluster(
        cluster_max_lpm={"standort_a": 4.5},
        kanal_profile={
            1: [("waldblumenhain", "standort_a", 6.0)],
        },
        kanal_zu_valve_id={1: "vid-1"},
    )
    ok = await sicherung.bewaessere(
        1, ["waldblumenhain"], 600, Ausloser.MANUELL,
    )
    assert ok is False
    assert not sicherung.ist_aktiv(1)
    assert client.oeffnen_aufrufe == []


@pytest.mark.asyncio
async def test_hahn_cluster_keine_konfig_kein_lock_check():
    """Ohne Cluster-Konfig: Backward-Compat — beide laufen ohne Lock."""
    sicherung, client, _ = _baue_sicherung_mit_cluster(
        cluster_max_lpm={},  # leer
        kanal_profile={
            1: [("waldblumenhain", None, None)],
            2: [("bambuswald", None, None)],
        },
        kanal_zu_valve_id={1: "vid-1", 2: "vid-2"},
    )
    ok1 = await sicherung.bewaessere(1, ["waldblumenhain"], 600, Ausloser.MANUELL)
    ok2 = await sicherung.bewaessere(2, ["bambuswald"], 600, Ausloser.MANUELL)
    assert ok1 is True
    assert ok2 is True


@pytest.mark.asyncio
async def test_hahn_cluster_nur_cluster_konfig_aber_kein_verbrauch():
    """Cluster definiert, Zonen ohne verbrauch_lpm -> Lock-Check uebersprungen."""
    sicherung, client, _ = _baue_sicherung_mit_cluster(
        cluster_max_lpm={"standort_a": 4.5},
        kanal_profile={
            1: [("waldblumenhain", "standort_a", None)],
            2: [("bambuswald", "standort_a", None)],
        },
        kanal_zu_valve_id={1: "vid-1", 2: "vid-2"},
    )
    ok1 = await sicherung.bewaessere(1, ["waldblumenhain"], 600, Ausloser.MANUELL)
    ok2 = await sicherung.bewaessere(2, ["bambuswald"], 600, Ausloser.MANUELL)
    assert ok1 is True
    assert ok2 is True


@pytest.mark.asyncio
async def test_pruefe_hahn_cluster_budget_liefert_grund():
    """Direkter Aufruf der Pruef-Methode liefert lesbaren Grund + aktive Zonen."""
    sicherung, _, _ = _baue_sicherung_mit_cluster(
        cluster_max_lpm={"standort_a": 4.5},
        kanal_profile={
            2: [("bambuswald", "standort_a", 1.87)],
            1: [("waldblumenhain", "standort_a", 6.0)],
        },
        kanal_zu_valve_id={1: "vid-1", 2: "vid-2"},
    )
    # Bambus laeuft schon, Waldblumen-Pruefung liefert Klartext.
    await sicherung.bewaessere(2, ["bambuswald"], 600, Ausloser.MANUELL)
    entscheidung = sicherung.pruefe_hahn_cluster_budget(1)
    assert entscheidung.erlaubt is False
    assert "standort_a" in entscheidung.grund
    assert "bambuswald" in entscheidung.aktive_zonen
    assert entscheidung.budget_lpm == 4.5
    assert entscheidung.verbrauch_aktuell_lpm == pytest.approx(1.87)
    assert entscheidung.verbrauch_neu_lpm == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_hahn_cluster_zwei_zonen_am_gleichen_kanal_zaehlen_einmal():
    """Bambus + Yogaraum auf K2 (1.87 max): zaehlt EINMAL, nicht doppelt."""
    sicherung, _, _ = _baue_sicherung_mit_cluster(
        cluster_max_lpm={"standort_a": 4.0},
        kanal_profile={
            # Beide Zonen am gleichen Kanal mit max(verbrauch) = 1.87.
            2: [
                ("bambuswald", "standort_a", 1.87),
                ("bambuswald_yogaraum", "standort_a", 1.87),
            ],
            3: [("hecke", "standort_a", 1.5)],
        },
        kanal_zu_valve_id={2: "vid-2", 3: "vid-3"},
    )
    await sicherung.bewaessere(
        2, ["bambuswald", "bambuswald_yogaraum"], 600, Ausloser.MANUELL,
    )
    # 1.87 + 1.5 = 3.37 < 4.0 -> Hecke erlaubt.
    entscheidung = sicherung.pruefe_hahn_cluster_budget(3)
    assert entscheidung.erlaubt is True
    assert entscheidung.verbrauch_aktuell_lpm == pytest.approx(1.87)


@pytest.mark.asyncio
async def test_hahn_cluster_isoliert_pro_cluster():
    """Beispielstadt-Yoga und Standort A unabhaengig -> Beispielstadt laeuft trotz Standort A-Vollast."""
    sicherung, _, _ = _baue_sicherung_mit_cluster(
        cluster_max_lpm={"standort_a": 2.0, "beispielstadt": 2.0},
        kanal_profile={
            2: [("bambuswald", "standort_a", 1.5)],
            5: [("yogapflanze", "beispielstadt", 1.0)],
        },
        kanal_zu_valve_id={2: "vid-2", 5: "vid-5"},
    )
    ok1 = await sicherung.bewaessere(2, ["bambuswald"], 600, Ausloser.MANUELL)
    ok2 = await sicherung.bewaessere(5, ["yogapflanze"], 600, Ausloser.MANUELL)
    assert ok1 is True
    assert ok2 is True


# --- T-0153: Druck-Exklusivitaet (Sprinkler etc.) ---


@pytest.mark.asyncio
async def test_exklusiv_blockt_obwohl_volumen_reicht():
    """Sprinkler exklusiv blockt Mikrodrip-Mitstart, auch wenn Budget reicht.

    Hahn-Maximum 10 L/min, Sprinkler 6 + Bambus 1.87 = 7.87 < 10 (Volumen ok).
    Aber Sprinkler ist `exklusiv=True` -> Mikrodrip-Start blockt trotzdem.
    """
    sicherung, _, _ = _baue_sicherung_mit_cluster(
        cluster_max_lpm={"standort_a": 10.0},
        kanal_profile={
            1: [("waldblumenhain", "standort_a", 6.0, True)],   # exklusiv
            2: [("bambuswald", "standort_a", 1.87, False)],
        },
        kanal_zu_valve_id={1: "vid-1", 2: "vid-2"},
    )
    ok_sprinkler = await sicherung.bewaessere(
        1, ["waldblumenhain"], 600, Ausloser.MANUELL,
    )
    assert ok_sprinkler is True
    # Bambus wuerde laut Volumen passen, aber Sprinkler-Exklusivitaet blockt.
    entscheidung = sicherung.pruefe_hahn_cluster_budget(2)
    assert entscheidung.erlaubt is False
    assert "exklusiv" in entscheidung.grund
    assert "waldblumenhain" in entscheidung.aktive_zonen


@pytest.mark.asyncio
async def test_exklusiv_neue_zone_blockt_bei_aktivem_cluster():
    """Bambus laeuft, dann will Sprinkler starten -> exklusiv blockt."""
    sicherung, _, _ = _baue_sicherung_mit_cluster(
        cluster_max_lpm={"standort_a": 10.0},
        kanal_profile={
            2: [("bambuswald", "standort_a", 1.87, False)],
            1: [("waldblumenhain", "standort_a", 6.0, True)],
        },
        kanal_zu_valve_id={1: "vid-1", 2: "vid-2"},
    )
    await sicherung.bewaessere(2, ["bambuswald"], 600, Ausloser.MANUELL)
    ok_sprinkler = await sicherung.bewaessere(
        1, ["waldblumenhain"], 600, Ausloser.MANUELL,
    )
    assert ok_sprinkler is False  # Bambus laeuft + Sprinkler will starten
    assert sicherung.ist_aktiv(2)
    assert not sicherung.ist_aktiv(1)


@pytest.mark.asyncio
async def test_exklusiv_solo_OK():
    """Exklusive Zone darf alleine starten, wenn nichts im Cluster laeuft."""
    sicherung, _, _ = _baue_sicherung_mit_cluster(
        cluster_max_lpm={"standort_a": 10.0},
        kanal_profile={
            1: [("waldblumenhain", "standort_a", 6.0, True)],
        },
        kanal_zu_valve_id={1: "vid-1"},
    )
    ok = await sicherung.bewaessere(
        1, ["waldblumenhain"], 600, Ausloser.MANUELL,
    )
    assert ok is True


@pytest.mark.asyncio
async def test_exklusiv_default_false_keine_blockade():
    """Backward-Compat: ohne exklusiv-Markierung greift nur Volumen-Check."""
    sicherung, _, _ = _baue_sicherung_mit_cluster(
        cluster_max_lpm={"standort_a": 10.0},
        kanal_profile={
            # Beide ohne exklusiv (3er-Tupel) -> Default False.
            1: [("waldblumenhain", "standort_a", 6.0)],
            2: [("bambuswald", "standort_a", 1.87)],
        },
        kanal_zu_valve_id={1: "vid-1", 2: "vid-2"},
    )
    ok1 = await sicherung.bewaessere(1, ["waldblumenhain"], 600, Ausloser.MANUELL)
    ok2 = await sicherung.bewaessere(2, ["bambuswald"], 600, Ausloser.MANUELL)
    assert ok1 is True
    assert ok2 is True  # Volumen reicht (6 + 1.87 = 7.87 < 10)


@pytest.mark.asyncio
async def test_exklusiv_zwei_mikrodrip_immer_noch_parallel():
    """Auch mit Sprinkler-im-Cluster duerfen Mikrodrip + Mikrodrip parallel."""
    sicherung, _, _ = _baue_sicherung_mit_cluster(
        cluster_max_lpm={"standort_a": 10.0},
        kanal_profile={
            1: [("waldblumenhain", "standort_a", 6.0, True)],   # nicht aktiv
            2: [("bambuswald", "standort_a", 1.87, False)],
            3: [("hecke", "standort_a", 1.5, False)],
        },
        kanal_zu_valve_id={1: "vid-1", 2: "vid-2", 3: "vid-3"},
    )
    ok_b = await sicherung.bewaessere(2, ["bambuswald"], 600, Ausloser.MANUELL)
    ok_h = await sicherung.bewaessere(3, ["hecke"], 600, Ausloser.MANUELL)
    assert ok_b is True
    assert ok_h is True


@pytest.mark.asyncio
async def test_exklusiv_kanal_mit_mehrzonen_an_einer_exklusiven():
    """Wenn IRGENDEINE Zone am Kanal exklusiv ist, ist der Kanal exklusiv."""
    sicherung, _, _ = _baue_sicherung_mit_cluster(
        cluster_max_lpm={"standort_a": 10.0},
        kanal_profile={
            # Kanal 1 hat zwei Zonen, eine ist exklusiv -> Kanal exklusiv.
            1: [
                ("zoneA", "standort_a", 3.0, False),
                ("zoneB", "standort_a", 3.0, True),
            ],
            2: [("bambuswald", "standort_a", 1.87, False)],
        },
        kanal_zu_valve_id={1: "vid-1", 2: "vid-2"},
    )
    await sicherung.bewaessere(1, ["zoneA", "zoneB"], 600, Ausloser.MANUELL)
    ok_b = await sicherung.bewaessere(
        2, ["bambuswald"], 600, Ausloser.MANUELL,
    )
    assert ok_b is False  # zoneB-Exklusivitaet blockt


# --- T-0448: leeres Kanal->Valve-Mapping an einem Multi-Valve-Geraet ---

class ClientMitGeraetetyp(ClientAttrappe):
    """Attrappe, die -- wie der echte Client -- den Geraetetyp beantworten kann.

    `multi=None` bildet den Fall ab, dass der Client die Frage nicht
    beantworten kann (noch nicht verbunden / Geraet unbekannt).
    """

    def __init__(self, multi: bool | None, **kwargs):
        super().__init__(**kwargs)
        self._multi = multi

    def ist_multi_valve_geraet(self, geraet_id: str) -> bool | None:
        return self._multi


def _sicherung_ohne_mapping(multi: bool | None):
    client = ClientMitGeraetetyp(multi=multi)
    speicher = SpeicherAttrappe()
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
        kanal_zu_valve_id={},  # Cloud lieferte beim Start keine Valves
    )
    return sicherung, client, speicher


@pytest.mark.asyncio
async def test_bewaessere_leeres_mapping_multi_valve_lehnt_ab():
    """Leeres Mapping am Multi-Valve-Geraet: gar nicht erst oeffnen.

    Der Client wuerde mit ValueError abbrechen; wir wollen den Fehler mit
    klarer Ursache VOR dem Call, nicht als generisches
    'oeffnen_fehlgeschlagen' danach.
    """
    sicherung, client, speicher = _sicherung_ohne_mapping(multi=True)

    erfolg = await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL)

    assert erfolg is False
    assert client.oeffnen_aufrufe == []
    assert speicher.ereignisse == []  # kein Lauf verbucht


@pytest.mark.asyncio
async def test_bewaessere_leeres_mapping_water_control_oeffnet_normal():
    """Einkanaliges WaterControl: leeres Mapping ist der Normalfall."""
    sicherung, client, _ = _sicherung_ohne_mapping(multi=False)

    erfolg = await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL)

    assert erfolg is True
    assert client.oeffnen_aufrufe == [(GERAET_ID, 600, None)]


@pytest.mark.asyncio
async def test_bewaessere_leeres_mapping_typ_unbekannt_bleibt_alt():
    """Client kann den Typ nicht sagen -> bisheriges Verhalten, kein Block."""
    sicherung, client, _ = _sicherung_ohne_mapping(multi=None)

    erfolg = await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL)

    assert erfolg is True
    assert client.oeffnen_aufrufe == [(GERAET_ID, 600, None)]


@pytest.mark.asyncio
async def test_stoppe_leeres_mapping_multi_valve_meldet_keinen_erfolg():
    """T-0448-Kern: 'kein Mapping' ist an einem Multi-Valve-Geraet NICHT
    'nichts zu tun' -- ein externer Lauf kann offen sein. Erfolg zu melden
    wuerde den echten offenen Zustand verstecken.
    """
    sicherung, client, _ = _sicherung_ohne_mapping(multi=True)

    erfolg = await sicherung.stoppe(KANAL, Ausloser.MANUELL)

    assert erfolg is False
    assert client.schliessen_aufrufe == []  # ohne valve_id nicht ansprechbar


@pytest.mark.asyncio
async def test_stoppe_leeres_mapping_water_control_bleibt_noop():
    """Einkanalig: leeres Mapping bleibt der dokumentierte Noop."""
    sicherung, client, _ = _sicherung_ohne_mapping(multi=False)

    erfolg = await sicherung.stoppe(KANAL, Ausloser.MANUELL)

    assert erfolg is True
    assert client.schliessen_aufrufe == []


@pytest.mark.asyncio
async def test_notfall_stopp_leeres_mapping_multi_valve_meldet_fehlschlag():
    """Der Extern-Zweig laeuft ohne Mapping wirkungslos durch. Er darf dann
    keinen sauberen Erfolg melden, sonst sieht ein Notfall-Stopp erledigt
    aus, waehrend extern gestartetes Wasser weiterlaeuft.
    """
    sicherung, client, _ = _sicherung_ohne_mapping(multi=True)
    client.offene = {"fremde-valve": {"activity": "MANUAL_WATERING"}}

    ergebnis = await sicherung.notfall_stopp()

    assert ergebnis["geschlossen"] == 0
    assert len(ergebnis["fehlgeschlagen"]) == 1
    eintrag = ergebnis["fehlgeschlagen"][0]
    assert eintrag["geraet_id"] == GERAET_ID
    assert eintrag["grund"] == "kanal_zu_valve_mapping_leer"


@pytest.mark.asyncio
async def test_notfall_stopp_leeres_mapping_water_control_bleibt_leise():
    """Einkanalig ohne offene Laeufe: kein kuenstlicher Fehleintrag."""
    sicherung, _, _ = _sicherung_ohne_mapping(multi=False)

    ergebnis = await sicherung.notfall_stopp()

    assert ergebnis == {"geschlossen": 0, "fehlgeschlagen": []}


# --- T-0449: Mapping-Refresh nach Reconnect ---

@pytest.mark.asyncio
async def test_setze_kanal_mapping_heilt_leeres_mapping():
    """Der T-0448-Startfall heilt sich beim naechsten Reconnect.

    Beim Start kam `{}` (Cloud lieferte keine Valves); der Reconnect-Hook
    baut es neu, und danach giesst der Kanal wieder.
    """
    sicherung, client, _ = _sicherung_ohne_mapping(multi=True)

    # Vorher: Multi-Valve ohne Mapping -> abgelehnt.
    assert await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL) is False

    assert sicherung.setze_kanal_mapping({KANAL: "valve-neu"}) is True

    assert await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL) is True
    assert client.oeffnen_aufrufe == [(GERAET_ID, 600, "valve-neu")]


@pytest.mark.asyncio
async def test_setze_kanal_mapping_verwirft_leeres_update():
    """Ein leeres Update darf ein funktionierendes Mapping nicht zerstoeren.

    `baue_kanal_zu_valve_id` liefert `{}` auch bei fehlender Valves-Liste.
    Ein Reconnect in genau dem Moment wuerde sonst den T-0448-Zustand
    herstellen, statt ihn zu heilen.
    """
    client = ClientMitGeraetetyp(multi=True)
    speicher = SpeicherAttrappe()
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
        kanal_zu_valve_id={KANAL: "valve-gut"},
    )

    assert sicherung.setze_kanal_mapping({}) is False

    # Das alte Mapping ist unveraendert benutzbar.
    assert await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL) is True
    assert client.oeffnen_aufrufe == [(GERAET_ID, 600, "valve-gut")]


@pytest.mark.asyncio
async def test_mapping_wechsel_zerreisst_laufenden_lauf_nicht():
    """T-0449-Designfrage: ein Mapping-Tausch waehrend eines Laufs darf die
    Zuordnung nicht kippen. `AktiveBewaesserung.valve_id` wird beim Oeffnen
    eingefroren -- der Stop muss die ALTE valve_id schliessen, nicht die neu
    gemappte, sonst bleibt das echte Ventil offen.
    """
    client = ClientMitGeraetetyp(multi=True)
    speicher = SpeicherAttrappe()
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
        kanal_zu_valve_id={KANAL: "valve-alt"},
    )
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL)

    # Reconnect liefert eine andere valve_id fuer denselben Kanal.
    sicherung.setze_kanal_mapping({KANAL: "valve-neu"})

    assert await sicherung.stoppe(KANAL, Ausloser.MANUELL) is True
    assert client.schliessen_aufrufe == [(GERAET_ID, "valve-alt")]


# --- T-0547: Replay-Guard vs. eigener Haupt-Puls (volle Kette) ---

@pytest.mark.asyncio
async def test_t0547_haelt_ventil_offen_nur_fuer_eigene_laufende_kanaele():
    """T-0547: Die Zustandsauskunft, auf der der Replay-Guard aufsetzt."""
    sicherung, _, _ = erstelle_sicherung()
    assert sicherung.haelt_ventil_offen(GERAET_ID) is False

    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    assert sicherung.haelt_ventil_offen(GERAET_ID) is True
    assert sicherung.haelt_ventil_offen("fremdes-ventil") is False

    await sicherung.stoppe(KANAL, Ausloser.MANUELL)
    assert sicherung.haelt_ventil_offen(GERAET_ID) is False


@pytest.mark.asyncio
async def test_t0547_haupt_puls_endet_automatik_statt_watchdog():
    """T-0547 Akzeptanz: ein 1800-s-Puls endet mit `ausloser=automatik` und
    echter Dauer, auch wenn KEIN Sensor-Event in den Puls faellt.

    Volle Kette wie in main.py verdrahtet: GardenaClient-Guard -> Praedikat
    aus der VentilSicherung -> Ventil-Callback -> verarbeite_callback. Vor
    T-0547 verwarf der Guard den Cloud-Close (30 min Stille = Gap), der
    Watchdog schrieb 30 s spaeter `watchdog`/1831 s.
    """
    sicherung, _, speicher = erstelle_sicherung()
    client = GardenaClient(client_id="test", client_secret="test")
    # valve_name -> zone (WATER_CONTROL: valve_name faellt auf device.name).
    client.registriere_zone_namen({"DWC1": "bambuswald"})
    # Der Puls laeuft -> Status offen (das OEFFNEN schreibt die Sicherung
    # selbst; der WS-Pfad sieht nur noch den Zustandswechsel).
    client._ventil_status[GERAET_ID] = "MANUAL_WATERING"
    # Verdrahtung exakt wie main.py.
    # T-0572: main.py injiziert seit 16.09. `lauf_plausibel_offen`.
    # Der Test bleibt damit die Kette, die produktiv laeuft -- und
    # belegt zugleich, dass die neue Schranke den T-0547-Fall NICHT
    # abschneidet (Puls laeuft seit 1800 s, Grenze 1800+30).
    client.setze_eigener_lauf_pruefer(sicherung.lauf_plausibel_offen)
    client.registriere_ventil_callback(sicherung.verarbeite_callback)

    await sicherung.bewaessere(KANAL, ZONEN, 1800, Ausloser.AUTOMATIK)
    aktiv = sicherung._aktiv[KANAL]
    timer = aktiv.timer_handle
    # Der Puls laeuft seit 1800 s; die WS war genau so lange still.
    aktiv.gestartet = datetime.now() - timedelta(seconds=1800)
    client._letzter_ws_event = datetime.now() - timedelta(seconds=1800)
    speicher.ereignisse.clear()

    callback = client._erstelle_ventil_callback(geraet_id=GERAET_ID)
    callback(SimpleNamespace(
        type="WATER_CONTROL", name="DWC1", valve_activity="CLOSED",
    ))
    for _ in range(5):
        if not client._aktive_tasks:
            break
        await asyncio.gather(*list(client._aktive_tasks), return_exceptions=True)

    schliessen = [
        e for e in speicher.ereignisse if e.aktion == VentilAktion.SCHLIESSEN
    ]
    assert len(schliessen) == len(ZONEN), "Cloud-Close vom Guard verschluckt"
    for e in schliessen:
        assert e.ausloser == Ausloser.AUTOMATIK
        assert 1795 <= e.dauer_sekunden <= 1805, e.dauer_sekunden
    # Der Watchdog wurde entwaffnet, kein zweiter Close mehr moeglich.
    assert not sicherung.ist_aktiv(KANAL)
    assert timer is not None and timer.cancelled()


# --- T-0572: Laptop-Schlaf macht den eigenen Zustand zum Falschzeugen ---

@pytest.mark.asyncio
async def test_t0572_lauf_plausibel_nur_innerhalb_der_kommandierten_dauer():
    """Die Schranke selbst. Sollwerte aus dem Realfall 15.09. (1797 s)."""
    sicherung, _, _ = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 1797, Ausloser.AUTOMATIK)
    aktiv = sicherung._aktiv[KANAL]
    start = aktiv.gestartet

    # Mitten im Lauf und exakt an der Watchdog-Grenze (dauer + puffer 30):
    # das ist der T-0547-Fall und muss durchgehen.
    assert sicherung.lauf_plausibel_offen(
        GERAET_ID, start + timedelta(seconds=1)) is True
    assert sicherung.lauf_plausibel_offen(
        GERAET_ID, start + timedelta(seconds=1827)) is True
    # Eine Sekunde danach haette der Watchdog laengst geschlossen.
    assert sicherung.lauf_plausibel_offen(
        GERAET_ID, start + timedelta(seconds=1828)) is False
    # Der Realfall: drei Stunden spaeter, Zustand eingefroren vom Schlaf.
    assert sicherung.lauf_plausibel_offen(
        GERAET_ID, start + timedelta(seconds=11652)) is False
    # Fremde Ventile bleiben unberuehrt (T-0547-Vertrag).
    assert sicherung.lauf_plausibel_offen("fremdes-ventil", start) is False


@pytest.mark.asyncio
async def test_t0572_ohne_kommandierte_dauer_bleibt_t0547_verhalten():
    """Ohne `dauer_s` gibt es keine Schranke -- dann NICHT stillschweigend
    zumachen, sonst waere der Riegel schlimmer als das Leck."""
    sicherung, _, _ = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    aktiv = sicherung._aktiv[KANAL]
    aktiv.dauer_s = 0
    spaet = aktiv.gestartet + timedelta(seconds=99999)
    assert sicherung.lauf_plausibel_offen(GERAET_ID, spaet) is True


@pytest.mark.asyncio
async def test_t0572_replayter_close_nach_schlafphase_erbt_keine_dauer():
    """Volle Kette, Realfall waldblumenhain 15.09.2026.

        19:19:52  eigener Lauf oeffnet, kommandiert 1797 s
        19:29:19  Deckel zu -> Watchdog-Timer friert ein, `_aktiv` bleibt
        ~19:49    die Cloud schliesst selbst (echtes Ende, ungesehen)
        22:34:05  Wake, WS-Luecke 11217 s -> Replay-Guard armiert
        22:34:05  replayter CLOSED -> vorher: SCHLIESSEN mit 11652 s

    Geprueft wird das ERGEBNIS (welche Events entstehen), nicht dass
    irgendein Praedikat aufgerufen wurde.
    """
    sicherung, _, speicher = erstelle_sicherung()
    client = GardenaClient(client_id="test", client_secret="test")
    client.registriere_zone_namen({"DWC1": "bambuswald"})
    client._ventil_status[GERAET_ID] = "MANUAL_WATERING"
    # Verdrahtung exakt wie main.py (T-0572).
    client.setze_eigener_lauf_pruefer(sicherung.lauf_plausibel_offen)
    client.registriere_ventil_callback(sicherung.verarbeite_callback)

    await sicherung.bewaessere(KANAL, ZONEN, 1797, Ausloser.AUTOMATIK)
    aktiv = sicherung._aktiv[KANAL]
    # Schlafphase: der Lauf startete vor 11652 s, der Timer ist nie gefeuert.
    aktiv.gestartet = datetime.now() - timedelta(seconds=11652)
    # Der WS-Pfad ankert sein eigenes `offen_seit` zum selben Zeitpunkt --
    # daraus entstuende die Phantom-Dauer.
    client._ventil_offen_seit[GERAET_ID] = aktiv.gestartet
    # WS war 11217 s still -> der Gap-Check armiert den Guard.
    client._letzter_ws_event = datetime.now() - timedelta(seconds=11217)
    speicher.ereignisse.clear()

    callback = client._erstelle_ventil_callback(geraet_id=GERAET_ID)
    callback(SimpleNamespace(
        type="WATER_CONTROL", name="DWC1", valve_activity="CLOSED",
    ))
    for _ in range(5):
        if not client._aktive_tasks:
            break
        await asyncio.gather(*list(client._aktive_tasks), return_exceptions=True)

    schliessen = [
        e for e in speicher.ereignisse if e.aktion == VentilAktion.SCHLIESSEN
    ]
    # Entscheidend ist nicht "kein Event", sondern: KEINE erfundene Dauer.
    # Die 11652 s waren der Schaden; sie sind das, was nicht wiederkommen darf.
    for e in schliessen:
        assert e.dauer_sekunden < 1900, (
            f"Phantom-Dauer aus der Schlafphase geerbt: {e.dauer_sekunden} s"
        )
    assert not any(e.dauer_sekunden > 10000 for e in schliessen)
    # Der Guard hat unterdrueckt, nicht durchgelassen. Getrennt geprueft,
    # weil "keine Phantom-Dauer" auch aus einem ganz anderen Grund wahr sein
    # koennte (z.B. gar kein Event) -- hier soll GENAU dieser Riegel greifen.
    snap = client.metriken.snapshot()
    assert snap.get("replay_guard_durchgelassen", {}) == {}
    assert snap.get("events_verworfen", {}).get("replay_guard:CLOSED") == 1


@pytest.mark.asyncio
async def test_t0572_produktive_verdrahtung_nutzt_die_zeitschranke():
    """Die NAHT: was `main.py` wirklich in den Guard injiziert.

    Ohne diesen Test bleibt die Suite gruen, wenn die Verdrahtung auf das
    alte `haelt_ventil_offen` zurueckfaellt -- gemessen am 16.09. Geprueft
    wird deshalb das Praedikat, das die Produktivfunktion BAUT, an einem
    Lauf, bei dem sich die beiden Auskuenfte unterscheiden.
    """
    from bewaesserung.main import baue_eigener_lauf_pruefer

    sicherung, _, _ = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 1797, Ausloser.AUTOMATIK)
    # Eingefrorener Zustand wie nach einer Schlafphase.
    sicherung._aktiv[KANAL].gestartet = (
        datetime.now() - timedelta(seconds=11652)
    )
    pruefer = baue_eigener_lauf_pruefer({1: sicherung})

    # Der reine Zustand sagt weiterhin "offen" -- das ist der Falschzeuge.
    assert sicherung.haelt_ventil_offen(GERAET_ID) is True
    # Die produktive Verdrahtung darf ihm nicht mehr folgen.
    assert pruefer(GERAET_ID) is False


@pytest.mark.asyncio
async def test_t0572b_dauer_wird_auf_die_kommandierte_gedeckelt():
    """Andres Entscheid 16.09.: T-0419 neu bewertet, over-count ist NICHT
    mehr die sichere Richtung. Sollwerte aus den drei belegten Faellen."""
    sicherung, _, speicher = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 1797, Ausloser.AUTOMATIK)
    aktiv = sicherung._aktiv[KANAL]
    start = aktiv.gestartet

    # Normalfall: verstrichene Zeit unter dem Deckel -> unveraendert messen.
    assert sicherung._gedeckelte_dauer(
        aktiv, start + timedelta(seconds=1500)) == 1500
    # Genau am Deckel (kommandiert + puffer 30).
    assert sicherung._gedeckelte_dauer(
        aktiv, start + timedelta(seconds=1827)) == 1827
    # Realfall waldblumenhain 15.09.: 11652 s -> auf 1827 s gedeckelt.
    assert sicherung._gedeckelte_dauer(
        aktiv, start + timedelta(seconds=11652)) == 1827


@pytest.mark.asyncio
async def test_t0572b_ohne_kommandierte_dauer_wird_nicht_gedeckelt():
    """Negativprobe: ohne Schranke darf nicht stillschweigend gekuerzt
    werden -- sonst erfindet der Deckel seinerseits Zahlen."""
    sicherung, _, _ = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
    aktiv = sicherung._aktiv[KANAL]
    aktiv.dauer_s = 0
    assert sicherung._gedeckelte_dauer(
        aktiv, aktiv.gestartet + timedelta(seconds=9999)) == 9999


@pytest.mark.asyncio
async def test_t0572b_geschriebenes_event_traegt_die_gedeckelte_dauer():
    """Die Wirkung, nicht die Rechnung: was landet im SCHLIESSEN-Event?

    Ohne diesen Fall koennte `_gedeckelte_dauer` korrekt rechnen und an
    den beiden Fundstellen trotzdem ungenutzt bleiben.
    """
    sicherung, _, speicher = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 1797, Ausloser.AUTOMATIK)
    sicherung._aktiv[KANAL].gestartet = (
        datetime.now() - timedelta(seconds=11652)
    )
    speicher.ereignisse.clear()
    await sicherung.stoppe(KANAL, Ausloser.AUTOMATIK)

    schliessen = [
        e for e in speicher.ereignisse if e.aktion == VentilAktion.SCHLIESSEN
    ]
    assert schliessen, "kein SCHLIESSEN geschrieben"
    for e in schliessen:
        assert e.dauer_sekunden == 1827, e.dauer_sekunden


@pytest.mark.asyncio
async def test_t0572b_callback_pfad_deckelt_ebenfalls():
    """Die ZWEITE Fundstelle. `verarbeite_callback` rechnet dieselbe Dauer
    aus demselben Anker -- ein Deckel nur in `stoppe()` waere der Klassiker
    "Fix in nur EINEM von zwei Fakten-Bauern".

    Die Negativprobe vom 16.09. hat genau das gezeigt: das Zuruecksetzen
    dieser Stelle liess die Suite gruen, solange nur `stoppe()` geprueft war.
    """
    sicherung, _, speicher = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 1797, Ausloser.AUTOMATIK)
    # Eingefrorener Zustand wie nach einer Schlafphase.
    sicherung._aktiv[KANAL].gestartet = (
        datetime.now() - timedelta(seconds=11652)
    )
    speicher.ereignisse.clear()

    ereignis = VentilEreignis(
        zeitstempel=datetime.now(), zone_id="bambuswald",
        ventil_id=GERAET_ID, aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=11652, ausloser=Ausloser.MANUELL,
    )
    behandelt = await sicherung.verarbeite_callback(ereignis)
    assert behandelt is True

    schliessen = [
        e for e in speicher.ereignisse if e.aktion == VentilAktion.SCHLIESSEN
    ]
    assert schliessen, "kein SCHLIESSEN ueber den Callback-Pfad geschrieben"
    for e in schliessen:
        assert e.dauer_sekunden == 1827, e.dauer_sekunden


# --- T-0558: Doppelstart-Race + Persistenzfehler nach dem Oeffnen ---

@pytest.mark.asyncio
async def test_t0558_paralleler_start_oeffnet_nur_einmal():
    """Zwei gleichzeitige `bewaessere()` auf demselben Kanal -> ein Lauf.

    Der Belegt-Guard am Anfang von `bewaessere` liegt VOR mehreren `await`
    (Frische-Gate, Hahn-Pruefung, Cloud-Roundtrip). Ohne Serialisierung
    laufen Auto-Loop und manueller Endpoint beide durch ihn hindurch, bevor
    einer `_aktiv` setzt: zwei Cloud-Befehle, zwei OEFFNEN-Events, und der
    Watchdog-Timer des ersten Laufs bleibt verwaist -- er schliesst spaeter
    den zweiten Lauf zu frueh.
    """
    client = ClientAttrappe()
    speicher = SpeicherAttrappe()

    langsam = asyncio.Event()

    async def langsames_oeffnen(geraet_id, dauer_sekunden, valve_id=None):
        # Cloud-Roundtrip: gibt die Kontrolle ab, waehrend `_aktiv` noch leer ist.
        await langsam.wait()
        client.oeffnen_aufrufe.append((geraet_id, dauer_sekunden, valve_id))

    client.ventil_oeffnen = langsames_oeffnen  # type: ignore[assignment]
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
    )

    aufgaben = [
        asyncio.create_task(
            sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.AUTOMATIK)
        ),
        asyncio.create_task(
            sicherung.bewaessere(KANAL, ZONEN, 60, Ausloser.MANUELL)
        ),
    ]
    await asyncio.sleep(0)
    langsam.set()
    ergebnisse = await asyncio.gather(*aufgaben)

    assert sorted(ergebnisse, key=bool) == [False, True], (
        f"genau ein Start darf gewinnen, war {ergebnisse}"
    )
    assert len(client.oeffnen_aufrufe) == 1, (
        f"zweiter Cloud-Befehl abgesetzt: {client.oeffnen_aufrufe}"
    )
    oeffnen = [
        e for e in speicher.ereignisse if e.aktion == VentilAktion.OEFFNEN
    ]
    assert len(oeffnen) == len(ZONEN), (
        f"erwartet ein OEFFNEN je Zone, war {len(oeffnen)}"
    )
    # Genau ein Watchdog-Timer -- der verwaiste des Verlierers waere der
    # eigentliche Schaden (schliesst den fremden Lauf zu frueh).
    assert sicherung._aktiv[KANAL].timer_handle is not None
    sicherung._aktiv[KANAL].timer_handle.cancel()


@pytest.mark.asyncio
async def test_t0558_db_fehler_nach_oeffnen_meldet_keinen_fehlschlag():
    """Persistenzfehler nach erfolgreichem Cloud-Oeffnen -> weiterhin True.

    Ab dem Cloud-Befehl ist das Ventil offen und der Watchdog scharf. Ein
    `database is locked` beim Protokollieren liess `bewaessere` vorher
    durchwerfen: die API meldete "Start fehlgeschlagen" und der Auto-Loop
    brach den Zyklus ab -- bei laufendem Wasser.
    """
    client = ClientAttrappe()
    speicher = SpeicherAttrappe()

    async def wirft(_ereignis):
        raise sqlite3.OperationalError("database is locked")

    speicher.speichere_ventil_ereignis = wirft  # type: ignore[assignment]
    sicherung = VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET_ID,
    )

    erfolg = await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL)

    assert erfolg is True, "Ventil ist offen -- das darf kein Fehlschlag sein"
    assert len(client.oeffnen_aufrufe) == 1
    assert KANAL in sicherung._aktiv, "Zustand muss den offenen Lauf kennen"
    sicherung._aktiv[KANAL].timer_handle.cancel()
