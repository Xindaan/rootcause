"""T-0595: synthetisches SCHLIESSEN ist kein Hardware-Beleg; Paarung nach
Laufende; VK-1 (Replay-Guard laesst den echten CLOSED eines eigenen Laufs
durch).

Realfaelle:
- 24.09. (HW-6): Netz weg, drei gescheiterte Watchdog-Closes, 14:12:49
  schreibt der OrphanCloseJob 5400 s fuer einen 3600-s-Lauf, 14:17:49 raeumt
  der Close-Fehlerpfad (T-0380) darauf `_aktiv` und `live_lauf_state`, die WS
  kam erst 14:41:56 zurueck.
- 25.09. (VK-1): CLOSED um 18:24:56 vom Replay-Guard verworfen, weil der
  eigene Lauf ueberfaellig war; Kanal bis zum Neustart aus der Automatik.

Je Test eine Grenze:
- Close-Fehlerpfad: ein Orphan-SCHLIESSEN raeumt nicht (HW-6, Konsument 1).
- Orphan-Scan: laesst Laeufe einer Sicherung in Ruhe; Dauer aus
  `live_lauf_state` statt max_dauer (AK2).
- Spaeter echter Close ersetzt ein schon geschriebenes synthetisches
  SCHLIESSEN, statt als Doppel verworfen zu werden (AK4a, Konsument 2).
- Naht GardenaClient -> VentilSicherung mit der produktiven Verdrahtung:
  armierter Guard, ueberfaelliger Lauf, CLOSED raeumt; der Orphan-Scan
  danach schreibt keinen zweiten Close (VK-1 + VK-2).

Harmlos auch ohne die Schutzbedingungen: Cloud-Kommandos sind Attrappen
(kein Netz), nur temporaere Datenbanken.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung import main
from bewaesserung.gardena_client import GardenaClient
from bewaesserung.modelle import (
    Ausloser,
    VentilAktion,
    VentilEreignis,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.orphan_close_job import OrphanCloseJob
from bewaesserung.speicher import (
    HERKUNFT_DHS_KORREKTUR,
    HERKUNFT_ORPHAN_ZEITBASIERT,
    Speicher,
    ist_synthetisches_schliessen,
)
from bewaesserung.ventil_sicherung import MAX_RETRY_VERSUCHE, VentilSicherung

DSWC = "bbbb0001-0000-4000-8000-000000000001"
KANAL = 2
VALVE = f"{DSWC}:{KANAL}"
ZONEN = ["bambuswald", "bambuswald_yogaraum"]
DAUER_S = 600
PUFFER_S = 30
MAX_DAUER_S = 1800
GEDECKELT_S = DAUER_S + PUFFER_S


class _Client(GardenaClient):
    """Echter GardenaClient (Statuscache, WS-Pfad, Replay-Guard); nur die
    Cloud-Kommandos sind Attrappen."""

    def __init__(self, schliessen_fehler: bool = True):
        super().__init__(client_id="test", client_secret="test")
        self.schliessen_fehler = schliessen_fehler

    async def ventil_oeffnen(self, geraet_id, dauer_sekunden, valve_id=None):
        return None

    async def ventil_schliessen(self, geraet_id, valve_id=None):
        if self.schliessen_fehler:
            raise ConnectionError("Netz weg (Attrappe)")


@pytest.fixture
async def speicher(tmp_path):
    s = Speicher(str(tmp_path / "t0595.db"))
    await s.verbinden()
    try:
        yield s
    finally:
        await s.schliessen()


def _zonen() -> list[ZonenKonfig]:
    return [
        ZonenKonfig(
            zone_id=z, name=z, modus=ZonenModus.AUTOMATIK,
            feuchte_schwelle_min=35.0, max_dauer_sekunden=MAX_DAUER_S,
        )
        for z in ZONEN
    ]


def _ws(client: GardenaClient, aktivitaet: str) -> None:
    client._verarbeite_ventil_update(VALVE, "Bambus", aktivitaet, "DWC1")


async def _lauf_mit_gescheitertem_close(speicher, client=None):
    """Eigener Lauf, drei Close-Versuche am Netz gescheitert (langsamer
    Modus) -- der Zustand vom 24./25.09."""
    client = client or _Client()
    client.registriere_zone_namen({"Bambus": ZONEN[0]})
    _ws(client, "CLOSED")                 # Erstsichtung vor dem Lauf
    sicherung = VentilSicherung(
        client=client, speicher=speicher, ventil_geraet_id=DSWC,
        puffer_sekunden=PUFFER_S, kanal_zu_valve_id={KANAL: VALVE},
    )
    assert await sicherung.bewaessere(KANAL, ZONEN, DAUER_S, Ausloser.AUTOMATIK)
    _ws(client, "MANUAL_WATERING")
    for _ in range(MAX_RETRY_VERSUCHE):
        assert await sicherung.stoppe(KANAL, Ausloser.WATCHDOG) is False
    return client, sicherung, sicherung._aktiv[KANAL]


async def _verschiebe_lauf(speicher, aktiv, um: timedelta) -> None:
    """Lauf liegt `um` in der Vergangenheit (Schlafphase): Zustand, OEFFNEN
    und live_lauf_state gemeinsam zurueckdatieren."""
    alt = aktiv.gestartet
    aktiv.gestartet = alt - um
    await speicher._db.execute(
        "UPDATE ventil_ereignis SET zeitstempel = ? WHERE aktion = 'oeffnen'",
        ((alt - um).isoformat(),),
    )
    await speicher._db.execute(
        "UPDATE live_lauf_state SET gestartet_am = ?",
        ((alt - um).isoformat(),),
    )
    await speicher._db.commit()


def _raeume_timer(sicherung) -> None:
    for a in sicherung._aktiv.values():
        if a.timer_handle:
            a.timer_handle.cancel()


async def _schliessen(speicher) -> list[VentilEreignis]:
    ergebnis: list[VentilEreignis] = []
    for zid in ZONEN:
        ergebnis += [
            e for e in await speicher.hole_ventil_ereignisse(zid)
            if e.aktion == VentilAktion.SCHLIESSEN
        ]
    return ergebnis


async def _warte_callbacks(client: GardenaClient) -> None:
    for _ in range(5):
        if not client._aktive_tasks:
            break
        await asyncio.gather(*list(client._aktive_tasks), return_exceptions=True)


# --- Herkunftsvermerk (Speicher) -------------------------------------------

async def test_herkunft_letzter_eintrag_entscheidet(speicher):
    """Das Merkmal selbst: ein Orphan-SCHLIESSEN ist synthetisch, nach einer
    DHS-Korrektur nicht mehr -- sonst bliebe eine Zeile mit Cloud-Werten fuer
    immer als "kein Beleg" markiert."""
    start = datetime(2026, 9, 24, 12, 11, 32)
    await speicher.speichere_ventil_ereignis(
        VentilEreignis(
            zeitstempel=start + timedelta(seconds=MAX_DAUER_S),
            zone_id=ZONEN[0], ventil_id=VALVE,
            aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=MAX_DAUER_S,
            ausloser=Ausloser.WATCHDOG,
        ),
        herkunft=HERKUNFT_ORPHAN_ZEITBASIERT,
    )
    assert not await speicher.existiert_schliessen_seit(
        VALVE, start, ohne_synthetische=True,
    )
    # Als Idempotenz-Guard zaehlt es weiter (kein zweiter Close daneben).
    assert await speicher.existiert_schliessen_seit(VALVE, start)

    (zeile,) = await speicher.hole_synthetische_schliessen_seit(
        VALVE, start, ZONEN[0],
    )
    await speicher.aktualisiere_ventil_ereignis(
        zeile.id, dauer_sekunden=3600, herkunft=HERKUNFT_DHS_KORREKTUR,
    )
    assert await speicher.existiert_schliessen_seit(
        VALVE, start, ohne_synthetische=True,
    )
    assert await speicher.hole_synthetische_schliessen_seit(
        VALVE, start, ZONEN[0],
    ) == []
    assert not ist_synthetisches_schliessen(None)


# --- HW-6, Konsument 1: Close-Fehlerpfad (T-0380) --------------------------

async def test_close_fehlerpfad_raeumt_nicht_auf_orphan_schliessen(speicher):
    """Realfall 24.09.: der Orphan-Scan schreibt ein SCHLIESSEN (hier ohne
    Sicherungs-Verdrahtung, wie vor T-0595), danach scheitert der naechste
    Close. Ohne Hardware-Beleg darf der Pfad NICHT raeumen."""
    client, sicherung, aktiv = await _lauf_mit_gescheitertem_close(speicher)
    try:
        orphan = OrphanCloseJob(speicher, _zonen())
        scan = aktiv.gestartet + timedelta(hours=2)
        assert await orphan.aktualisiere(jetzt=scan) == len(ZONEN)

        assert await sicherung.stoppe(KANAL, Ausloser.WATCHDOG) is False
        assert sicherung.ist_aktiv(KANAL)
        assert await speicher.hole_live_lauf_states(DSWC) != []
    finally:
        _raeume_timer(sicherung)


# --- AK2: Orphan-Scan und Sicherung ----------------------------------------

async def test_orphan_scan_ueberspringt_lauf_einer_sicherung(speicher):
    client, sicherung, aktiv = await _lauf_mit_gescheitertem_close(speicher)
    try:
        orphan = OrphanCloseJob(speicher, _zonen())
        orphan.setze_ventil_sicherungen({DSWC: sicherung})
        scan = aktiv.gestartet + timedelta(hours=2)
        assert await orphan.aktualisiere(jetzt=scan) == 0
        assert await _schliessen(speicher) == []
    finally:
        _raeume_timer(sicherung)


async def test_orphan_dauer_ist_die_kommandierte_aus_live_lauf_state(speicher):
    """Lauf steht in live_lauf_state, aber keine Sicherung fuehrt ihn: die
    Cloud schliesst nach der KOMMANDIERTEN Dauer, nicht nach max_dauer
    (Realfall 24.09.: 5400 s fuer einen 3600-s-Lauf)."""
    start = datetime(2026, 9, 24, 12, 11, 32)
    await speicher.setze_live_lauf_state(
        kanal=KANAL, valve_id=VALVE, geraet_id=DSWC, zone_ids=ZONEN,
        dauer_sekunden=DAUER_S, ausloser="automatik", gestartet_am=start,
    )
    for zid in ZONEN:
        await speicher.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=start + timedelta(seconds=2), zone_id=zid,
            ventil_id=VALVE, aktion=VentilAktion.OEFFNEN,
            ausloser=Ausloser.AUTOMATIK,
        ))
    orphan = OrphanCloseJob(speicher, _zonen())
    assert await orphan.aktualisiere(jetzt=start + timedelta(hours=2)) == 2
    schliessen = await _schliessen(speicher)
    assert sorted(e.dauer_sekunden for e in schliessen) == [DAUER_S, DAUER_S]
    assert all(
        e.zeitstempel == start + timedelta(seconds=2 + DAUER_S)
        for e in schliessen
    )


# --- AK4a: spaeter echter Close ersetzt das synthetische -------------------

async def test_spaeter_echter_close_ersetzt_synthetisches_schliessen(speicher):
    """Close scheitert dreimal, der Orphan-Scan (ohne Verdrahtung) schreibt
    max_dauer; dann kommt der echte CLOSED nach WS-Luecke. Er darf nicht als
    Doppel verworfen werden, sondern muss die Dauer korrigieren -- und es
    bleibt EIN SCHLIESSEN je Zone."""
    client, sicherung, aktiv = await _lauf_mit_gescheitertem_close(speicher)
    main.verdrahte_replay_guard(client, {DSWC: sicherung})
    client.registriere_ventil_callback(sicherung.verarbeite_callback)
    try:
        await _verschiebe_lauf(speicher, aktiv, timedelta(hours=3))
        orphan = OrphanCloseJob(speicher, _zonen())
        assert await orphan.aktualisiere(jetzt=datetime.now()) == len(ZONEN)

        # WS war lange still -> der Gap-Check armiert den Guard.
        client._letzter_ws_event = datetime.now() - timedelta(hours=1)
        _ws(client, "CLOSED")
        await _warte_callbacks(client)

        schliessen = await _schliessen(speicher)
        assert len(schliessen) == len(ZONEN)
        for e in schliessen:
            assert e.dauer_sekunden == GEDECKELT_S, e.dauer_sekunden
            assert e.ausloser == Ausloser.AUTOMATIK
        assert not sicherung.ist_aktiv(KANAL)
        assert await speicher.hole_live_lauf_states(DSWC) == []
    finally:
        _raeume_timer(sicherung)


# --- Naht GardenaClient -> VentilSicherung (VK-1, vormals T-0590 AK4b) -------

async def test_naht_closed_eines_ueberfaelligen_laufs_raeumt_und_zaehlt_einmal(
    speicher,
):
    """Produktive Verdrahtung (`main.verdrahte_replay_guard`), Guard armiert,
    eigener Lauf seit 3 h ueberfaellig (Schlafphase). Der echte CLOSED muss
    `_aktiv` raeumen, die Dauer bleibt gedeckelt, und der Orphan-Scan danach
    schreibt keinen zweiten Close -- der Grund, VK-1 an VK-2 zu koppeln."""
    client, sicherung, aktiv = await _lauf_mit_gescheitertem_close(speicher)
    sicherungen = {DSWC: sicherung}
    main.verdrahte_replay_guard(client, sicherungen)
    client.registriere_ventil_callback(sicherung.verarbeite_callback)
    orphan = OrphanCloseJob(speicher, _zonen())
    orphan.setze_ventil_sicherungen(sicherungen)
    try:
        await _verschiebe_lauf(speicher, aktiv, timedelta(hours=3))
        # Verdrahtung in beide Richtungen: OFFEN bleibt zeitgebunden (T-0572),
        # ZU fragt den Zustand.
        assert client._haelt_eigener_lauf(VALVE, zu=False) is False
        assert client._haelt_eigener_lauf(VALVE, zu=True) is True

        client._letzter_ws_event = datetime.now() - timedelta(hours=1)
        _ws(client, "CLOSED")
        await _warte_callbacks(client)
        snap = client.metriken.snapshot()
        assert snap.get("events_verworfen", {}).get("replay_guard:CLOSED") is None
        assert not sicherung.ist_aktiv(KANAL)

        schliessen = await _schliessen(speicher)
        assert len(schliessen) == len(ZONEN)
        assert all(e.dauer_sekunden == GEDECKELT_S for e in schliessen)

        # Spaeter Close liegt hinter OEFFNEN + max_dauer + Grace.
        assert await orphan.aktualisiere(jetzt=datetime.now()) == 0
        assert len(await _schliessen(speicher)) == len(ZONEN)
    finally:
        _raeume_timer(sicherung)


async def test_gelungener_spaeter_close_ersetzt_synthetisches_schliessen(
    speicher,
):
    """Isomorphie zum Callback-Pfad: der langsame Close-Retry gelingt erst,
    nachdem der Orphan-Scan (ohne Verdrahtung) max_dauer geschrieben hat.
    Vorher meldete `stoppe` "doppel_close_vermieden", und die DB behielt die
    synthetische Dauer."""
    client, sicherung, aktiv = await _lauf_mit_gescheitertem_close(speicher)
    try:
        await _verschiebe_lauf(speicher, aktiv, timedelta(hours=3))
        orphan = OrphanCloseJob(speicher, _zonen())
        assert await orphan.aktualisiere(jetzt=datetime.now()) == len(ZONEN)

        client.schliessen_fehler = False
        assert await sicherung.stoppe(KANAL, Ausloser.WATCHDOG) is True

        schliessen = await _schliessen(speicher)
        assert len(schliessen) == len(ZONEN)
        assert all(e.dauer_sekunden == GEDECKELT_S for e in schliessen)
    finally:
        _raeume_timer(sicherung)


async def test_dhs_korrektur_macht_orphan_schliessen_zum_beleg(speicher):
    """Die DHS-Korrektur hebt ein Orphan-SCHLIESSEN auf Cloud-Werte; danach
    ist es kein Schaetzwert mehr (Herkunftsvermerk `dhs_korrektur`) und
    zaehlt als Beleg "Ventil zu". Ohne den Vermerk bliebe es dauerhaft
    ausgeschlossen, und ein spaeter Callback ueberschriebe die Cloud-Werte."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from bewaesserung.gardena_web_backfill import GardenaWebBackfillJob

    # 24.09. 12:11:32 Sommerzeit = 10:11:32 UTC, Lauf 3600 s laut DHS.
    start = datetime(2026, 9, 24, 12, 11, 32)
    await speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=start, zone_id=ZONEN[0], ventil_id=VALVE,
        aktion=VentilAktion.OEFFNEN, ausloser=Ausloser.AUTOMATIK,
    ))
    zone = ZonenKonfig(
        zone_id=ZONEN[0], name=ZONEN[0], modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=35.0, max_dauer_sekunden=5400,
    )
    assert await OrphanCloseJob(speicher, [zone]).aktualisiere(
        jetzt=start + timedelta(hours=3),
    ) == 1
    assert not await speicher.existiert_schliessen_seit(
        VALVE, start, ohne_synthetische=True,
    )

    antwort = MagicMock()
    antwort.raise_for_status = MagicMock()
    antwort.json = MagicMock(return_value={
        "data": [{
            "type": "dh-action-serie", "id": "s",
            "attributes": {"property-name": "action_1"},
            "relationships": {"dh-events": {"data": [
                {"type": "dh-action-event", "id": "ev-0"},
            ]}},
        }],
        "included": [{
            "type": "dh-action-event", "id": "ev-0",
            "attributes": {
                "start": "2026-09-24T10:11:32Z", "stop": "2026-09-24T11:11:32Z",
                "duration": 3600, "action": "MANUAL",
                "summary": "EXECUTED_MANUAL", "firmware-action-id": 0,
            },
        }],
    })
    http = AsyncMock()
    http.get = AsyncMock(return_value=antwort)
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=None)
    auth = MagicMock()
    auth.hole_gueltigen_token = AsyncMock(return_value="fake")
    job = GardenaWebBackfillJob(
        auth=auth, speicher=speicher, location_id="loc",
        water_control_geraet_id="dev", kanal_zu_zonen={1: [ZONEN[0]]},
    )
    with patch(
        "bewaesserung.gardena_web_backfill.httpx.AsyncClient",
        return_value=http,
    ):
        await job.aktualisiere()

    (close,) = [
        e for e in await speicher.hole_ventil_ereignisse(ZONEN[0])
        if e.aktion == VentilAktion.SCHLIESSEN
    ]
    assert close.dauer_sekunden == 3600          # Korrektur lief
    assert await speicher.existiert_schliessen_seit(
        VALVE, start, ohne_synthetische=True,
    )


# T-0610 AT-3 (14.10.2026): die Verdrahtung in main.ausfuehren prueft jetzt ein
# echter Start-Lauf, test_t0610_at11_start_lauf.py::test_replay_guard_fragt_alle_sicherungen_offen_und_zu
# und ::test_watchdog_und_orphan_scan_kennen_die_sicherungen (vorher AST-Test).
