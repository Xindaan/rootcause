"""T-0604 (PERS-5): DHS-Nachtrag und spaetes Live-SCHLIESSEN zaehlen einen
Lauf nur einmal.

Zahlen aus dem Repro der Gesamtreview (Realfall bambuswald 30.08.):
DHS 13:33:53-14:14:49 (2458 s); Live-OEFFNEN +240 s (Loop kroch, T-0556)
bzw. +27 s (App-/Zeitplan-Lauf); Live-SCHLIESSEN erst nach einer WS-Luecke.
Vorher: Summe 4916 s (gardena_web-Paar + Live-Close) bzw. 6625 s
(Nachtrag + WS-Close ohne Waechter).

Je Test eine Grenze:
- DHS-Lauf VOR dem Live-Close, Engine-Lauf mit Live-OEFFNEN +240 s: kein
  gardena_web-Paar, der Close kommt aus dem Nachtrag, der spaete Callback
  schreibt keinen zweiten (AK1, AK3).
- Dasselbe fuer einen App-Lauf (+27 s) ueber den externen Schreibpfad.
- Steht ein gardena_web-Paar schon da, legt das Live-SCHLIESSEN es still
  (AK2) -- je Schreibpfad (Engine, extern).

Harmlos ohne die Schutzbedingungen: DHS-HTTP ist gemockt, Cloud-Kommandos
sind Attrappen, temporaere Datenbank.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bewaesserung import main
from bewaesserung.gardena_web_backfill import GardenaWebBackfillJob
from bewaesserung.modelle import (
    KEINE_WASSER_AUSLOESER,
    Ausloser,
    VentilAktion,
    VentilEreignis,
)
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import VentilSicherung

DSWC = "bbbb0001-0000-4000-8000-000000000001"
KANAL = 1
VALVE = f"{DSWC}:{KANAL}"
ZONE = "bambuswald"
# 30.08. Sommerzeit: lokal = UTC + 2 h.
DHS_START = datetime(2026, 8, 30, 13, 33, 53)
DHS_STOP = datetime(2026, 8, 30, 14, 14, 49)
DHS_DAUER = 2458


class _Client:
    async def ventil_oeffnen(self, geraet_id, dauer_sekunden, valve_id=None):
        return None

    async def ventil_schliessen(self, geraet_id, valve_id=None):
        return None


@pytest.fixture
async def speicher(tmp_path):
    s = Speicher(str(tmp_path / "t0604.db"))
    await s.verbinden()
    try:
        yield s
    finally:
        await s.schliessen()


async def _dhs_lauf(speicher) -> None:
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
                "start": "2026-08-30T11:33:53Z", "stop": "2026-08-30T12:14:49Z",
                "duration": DHS_DAUER, "action": "MANUAL",
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
        water_control_geraet_id=DSWC, kanal_zu_zonen={KANAL: [ZONE]},
    )
    with patch(
        "bewaesserung.gardena_web_backfill.httpx.AsyncClient",
        return_value=http,
    ):
        await job.aktualisiere()


async def _zaehlende_schliessen(speicher) -> list[VentilEreignis]:
    return [
        e for e in await speicher.hole_ventil_ereignisse(ZONE)
        if e.aktion == VentilAktion.SCHLIESSEN
        and e.ausloser not in KEINE_WASSER_AUSLOESER
    ]


async def _engine_lauf(speicher, gestartet: datetime) -> VentilSicherung:
    """Eigener Lauf, dessen OEFFNEN `gestartet` traegt (Loop kroch)."""
    sicherung = VentilSicherung(
        client=_Client(), speicher=speicher, ventil_geraet_id=DSWC,
        kanal_zu_valve_id={KANAL: VALVE},
    )
    assert await sicherung.bewaessere(KANAL, [ZONE], 2400, Ausloser.AUTOMATIK)
    aktiv = sicherung._aktiv[KANAL]
    if aktiv.timer_handle:
        aktiv.timer_handle.cancel()
    aktiv.gestartet = gestartet
    await speicher._db.execute(
        "UPDATE ventil_ereignis SET zeitstempel = ? WHERE aktion = 'oeffnen'",
        (gestartet.isoformat(),),
    )
    await speicher._db.commit()
    return sicherung


def _live_close() -> VentilEreignis:
    return VentilEreignis(
        zeitstempel=datetime.now(), zone_id=ZONE, ventil_id=VALVE,
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )


# --- AK1 + AK3: DHS-Lauf vor dem Live-Close ----------------------------------

async def test_engine_lauf_dhs_vor_live_close_zaehlt_einmal(speicher):
    sicherung = await _engine_lauf(
        speicher, DHS_START + timedelta(seconds=240),
    )
    await _dhs_lauf(speicher)
    gardena_web = [
        e for e in await speicher.hole_ventil_ereignisse(ZONE)
        if e.ventil_id == "gardena_web"
    ]
    assert gardena_web == []

    # Das spaete echte SCHLIESSEN nach der WS-Luecke.
    assert await sicherung.verarbeite_callback(_live_close()) is True
    assert not sicherung.ist_aktiv(KANAL)

    schliessen = await _zaehlende_schliessen(speicher)
    assert sum(e.dauer_sekunden for e in schliessen) == DHS_DAUER
    assert [(e.zeitstempel, e.ventil_id) for e in schliessen] == [
        (DHS_STOP, VALVE),
    ]


async def test_app_lauf_nachtrag_und_spaetes_ws_schliessen_zaehlen_einmal(
    speicher,
):
    """App-Lauf, Live-OEFFNEN nur +27 s neben DHS: der Nachtrag traegt den
    Close ein, das spaete WS-SCHLIESSEN (14:47:20, 4167 s) lief bisher ohne
    jeden Waechter in die DB."""
    await main.speichere_externes_ventil_ereignis(speicher, VentilEreignis(
        zeitstempel=DHS_START + timedelta(seconds=27), zone_id=ZONE,
        ventil_id=VALVE, aktion=VentilAktion.OEFFNEN,
        ausloser=Ausloser.MANUELL,
    ), [ZONE])
    await _dhs_lauf(speicher)
    ws_close = DHS_STOP + timedelta(seconds=1951)
    await main.speichere_externes_ventil_ereignis(speicher, VentilEreignis(
        zeitstempel=ws_close, zone_id=ZONE, ventil_id=VALVE,
        aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=int((ws_close - DHS_START).total_seconds()) - 27,
        ausloser=Ausloser.MANUELL,
    ), [ZONE])

    schliessen = await _zaehlende_schliessen(speicher)
    assert sum(e.dauer_sekunden for e in schliessen) == DHS_DAUER


# --- AK2: vorhandenes gardena_web-Paar wird stillgelegt -----------------------

async def _gardena_web_paar(speicher) -> None:
    for aktion, zeit, dauer in (
        (VentilAktion.OEFFNEN, DHS_START, 0),
        (VentilAktion.SCHLIESSEN, DHS_STOP, DHS_DAUER),
    ):
        await speicher.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=zeit, zone_id=ZONE, ventil_id="gardena_web",
            aktion=aktion, dauer_sekunden=dauer, ausloser=Ausloser.MANUELL,
        ))


async def test_engine_live_close_legt_dhs_paar_still(speicher):
    sicherung = await _engine_lauf(
        speicher, DHS_START + timedelta(seconds=240),
    )
    await _gardena_web_paar(speicher)
    assert await sicherung.verarbeite_callback(_live_close()) is True

    schliessen = await _zaehlende_schliessen(speicher)
    assert [e.ventil_id for e in schliessen] == [VALVE]
    gardena_web = [
        e for e in await speicher.hole_ventil_ereignisse(ZONE)
        if e.ventil_id == "gardena_web"
    ]
    assert {e.ausloser for e in gardena_web} == {Ausloser.IGNORIERT}


async def test_engine_stoppe_legt_dhs_paar_still(speicher):
    """Zweiter Engine-Schreibpfad: der eigene Close ueber `stoppe` (Watchdog,
    manuell-stop, Notfall) statt ueber den Callback. Er schreibt ueber
    denselben Helfer, eine Mutante am Aufruf in `_stoppe_unlocked` blieb
    aber ohne diesen Test gruen (Integration, Mutante M7)."""
    sicherung = await _engine_lauf(
        speicher, DHS_START + timedelta(seconds=240),
    )
    await _gardena_web_paar(speicher)
    assert await sicherung.stoppe(KANAL, Ausloser.WATCHDOG) is True
    assert not sicherung.ist_aktiv(KANAL)

    schliessen = await _zaehlende_schliessen(speicher)
    assert [e.ventil_id for e in schliessen] == [VALVE]
    gardena_web = [
        e for e in await speicher.hole_ventil_ereignisse(ZONE)
        if e.ventil_id == "gardena_web"
    ]
    assert {e.ausloser for e in gardena_web} == {Ausloser.IGNORIERT}


async def test_externer_live_close_legt_dhs_paar_still(speicher):
    await _gardena_web_paar(speicher)
    live_start = DHS_START + timedelta(seconds=240)
    ws_close = DHS_STOP + timedelta(seconds=1951)
    await main.speichere_externes_ventil_ereignis(speicher, VentilEreignis(
        zeitstempel=ws_close, zone_id=ZONE, ventil_id=VALVE,
        aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=int((ws_close - live_start).total_seconds()),
        ausloser=Ausloser.MANUELL,
    ), [ZONE])

    schliessen = await _zaehlende_schliessen(speicher)
    assert [e.ventil_id for e in schliessen] == [VALVE]


async def test_dhs_paar_eines_anderen_laufs_bleibt(speicher):
    """Die Grenze der Stilllegung: ein DHS-Lauf, der VOR dem Live-OEFFNEN
    endete, ist ein anderer Lauf und zaehlt weiter."""
    await _gardena_web_paar(speicher)
    live_start = DHS_STOP + timedelta(minutes=10)
    ws_close = live_start + timedelta(seconds=600)
    await main.speichere_externes_ventil_ereignis(speicher, VentilEreignis(
        zeitstempel=ws_close, zone_id=ZONE, ventil_id=VALVE,
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
        ausloser=Ausloser.MANUELL,
    ), [ZONE])

    schliessen = await _zaehlende_schliessen(speicher)
    assert sorted(e.ventil_id for e in schliessen) == sorted(
        ["gardena_web", VALVE],
    )


@pytest.mark.parametrize("ventil_id,ausloser,quell_zone", [
    ("manuell", Ausloser.MANUELL, None),            # Giesskannen-Log
    (VALVE, Ausloser.FREMDWASSER, "magerwiese"),   # Cross-Spray-Zeile
])
async def test_kein_live_lauf_dieses_kanals_ist_kein_pendant(
    speicher, ventil_id, ausloser, quell_zone,
):
    """Die neue Ground-Truth-Regel zaehlt nur OEFFNEN, die Wasser AUS DIESEM
    Kanal belegen. Sonst verschluckte ein Giesskannen-Eintrag oder eine
    Fremdwasser-Zeile im DHS-Intervall den echten Lauf."""
    await speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=DHS_START + timedelta(seconds=240), zone_id=ZONE,
        ventil_id=ventil_id, aktion=VentilAktion.OEFFNEN,
        ausloser=ausloser, quell_zone=quell_zone,
    ))
    await _dhs_lauf(speicher)
    schliessen = await _zaehlende_schliessen(speicher)
    assert [(e.ventil_id, e.dauer_sekunden) for e in schliessen] == [
        ("gardena_web", DHS_DAUER),
    ]


async def test_externer_spaeter_close_ersetzt_synthetisches_schliessen(
    speicher,
):
    """Gate-W4-P2 (Probe P8): App-Lauf 3 h, der OrphanCloseJob hat nach
    max_dauer (3600 s) schon ein synthetisches SCHLIESSEN geschrieben. Das
    echte spaete WS-SCHLIESSEN (10800 s) ersetzt dessen Werte. Vorher hielt
    der Doppel-Close-Waechter den Schaetzwert fuer einen Beleg, verwarf den
    echten Close, und die DB behielt 3600 s watchdog."""
    from bewaesserung.modelle import ZonenKonfig, ZonenModus
    from bewaesserung.orphan_close_job import OrphanCloseJob

    start = datetime.now().replace(microsecond=0) - timedelta(hours=4)
    await main.speichere_externes_ventil_ereignis(speicher, VentilEreignis(
        zeitstempel=start, zone_id=ZONE, ventil_id=VALVE,
        aktion=VentilAktion.OEFFNEN, ausloser=Ausloser.MANUELL,
    ), [ZONE])
    zone = ZonenKonfig(
        zone_id=ZONE, name=ZONE, modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=35.0, max_dauer_sekunden=3600,
    )
    assert await OrphanCloseJob(speicher, [zone]).aktualisiere(
        jetzt=start + timedelta(hours=2),
    ) == 1

    geschrieben = await main.speichere_externes_ventil_ereignis(
        speicher, VentilEreignis(
            zeitstempel=start + timedelta(seconds=10800), zone_id=ZONE,
            ventil_id=VALVE, aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=10800, ausloser=Ausloser.MANUELL,
        ), [ZONE],
    )

    assert geschrieben == [ZONE]
    schliessen = [
        e for e in await speicher.hole_ventil_ereignisse(ZONE)
        if e.aktion == VentilAktion.SCHLIESSEN
    ]
    assert [(e.dauer_sekunden, e.ausloser) for e in schliessen] == [
        (10800, Ausloser.MANUELL),
    ]
    # Ersetzt, nicht nur ueberschrieben: die Zeile gilt jetzt als Beleg.
    assert await speicher.existiert_schliessen_seit(
        VALVE, start, [ZONE], ohne_synthetische=True,
    )


async def test_externer_close_ueber_die_zeitumstellung_verwirft_keinen_lauf(
    speicher,
):
    """Naht T-0604 x T-0593: Laufstart ueber UTC zurueckgerechnet. App-Lauf B
    01:50 Sommerzeit bis 02:20 Winterzeit am 25.10. (5400 s). Naiv lag der
    Start bei 00:50, das Fenster des Doppel-Close-Waechters fing den Close
    von Lauf A (01:20), und B fehlte in der DB."""
    for zeit, aktion, dauer in (
        (datetime(2026, 10, 25, 0, 40), VentilAktion.OEFFNEN, 0),
        (datetime(2026, 10, 25, 1, 20), VentilAktion.SCHLIESSEN, 2400),
        (datetime(2026, 10, 25, 1, 50), VentilAktion.OEFFNEN, 0),
        (datetime(2026, 10, 25, 2, 20, fold=1), VentilAktion.SCHLIESSEN, 5400),
    ):
        await main.speichere_externes_ventil_ereignis(speicher, VentilEreignis(
            zeitstempel=zeit, zone_id=ZONE, ventil_id=VALVE, aktion=aktion,
            dauer_sekunden=dauer, ausloser=Ausloser.MANUELL,
        ), [ZONE])

    schliessen = await _zaehlende_schliessen(speicher)
    assert [e.dauer_sekunden for e in schliessen] == [2400, 5400]


# T-0610 AT-3 (14.10.2026): die Verdrahtung in main.ausfuehren prueft jetzt ein
# echter Start-Lauf, test_t0610_at11_start_lauf.py::test_ws_callback_schreibt_externe_laeufe_ueber_den_geschuetzten_pfad
# (vorher AST-Test auf die Closure).
