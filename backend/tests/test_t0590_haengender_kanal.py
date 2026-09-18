"""T-0590: Kanal haengt nach gescheitertem Close bis zum Neustart.

Realfall 25.09.2026, Kanal 2 (bambuswald/yogaraum): drei Watchdog-Closes
scheitern direkt nach dem Aufwachen am Netz (17:54:33-17:55:33), danach
`ventil.retry_aufgegeben`; der echte Cloud-CLOSED um 18:24:56 wird vom
Replay-Guard verworfen (VK-1). `_aktiv` und `live_lauf_state` bleiben stehen,
der Entscheidungsloop ruft fuer den Kanal nie wieder `pruefe_kanal` auf, und
der Arbiter sperrt ueber dieselbe Zeile auch den exklusiven waldblumenhain
(VK-4). Geraeumt erst durch Andres Neustart am 26.09. 05:49.

Die Datei sichert je Waechter eine Grenze:

- Heilung (`heile_haengenden_lauf`): nur ueberfaellig, nur mit Hardware-Beleg,
  kein Orphan-Close als Beleg, kein Statuscache von vor dem Laufstart, nicht
  unter einem laufenden Close (Stop-Lock).
- Entscheidungsloop: nach der Heilung faellt der Kanal in die normale Pruefung.
- Arbiter: ein ueberfaelliger Engine-Lauf sperrt keine exklusive Zone mehr.
- Watchdog-Push beim Wechsel in den langsamen Close-Modus (HW-9).
- `tools/restart_wenn_ruhig.sh`: eine haengende live_lauf_state-Zeile gilt
  nicht mehr als laufend.

Der langsame Close-Retry selbst steht in `test_ventil_sicherung.py`
(`test_stoppe_retry_limit_erreicht`).

Harmlos auch ohne die Schutzbedingungen: kein Netz (Cloud-Kommandos sind
Attrappen), nur temporaere Datenbanken, das Restart-Skript laeuft als Kopie in
tmp_path mit `--trocken` (kein service.sh daneben).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bewaesserung import main
from bewaesserung.gardena_client import GardenaClient
from bewaesserung.hahn_arbiter import HahnArbiter, Kanalprofil
from bewaesserung.modelle import (
    Ausloser,
    VentilAktion,
    VentilEreignis,
    WatchdogKonfig,
    ZeitFenster,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.orphan_close_job import OrphanCloseJob
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import MAX_RETRY_VERSUCHE, VentilSicherung
from bewaesserung.watchdog import WatchdogJob

DSWC = "bbbb0001-0000-4000-8000-000000000001"
KANAL = 2
VALVE = f"{DSWC}:{KANAL}"
ZONEN = ["bambuswald", "bambuswald_yogaraum"]
DAUER_S = 600
PUFFER_S = 30


class _ClientOhneNetz(GardenaClient):
    """Echter GardenaClient (Statuscache, WS-Pfad), nur die Cloud-Kommandos
    sind Attrappen. Der Close scheitert wie am 25.09. am Netz."""

    def __init__(self, schliessen_fehler: bool = True):
        super().__init__(client_id="test", client_secret="test")
        self.schliessen_fehler = schliessen_fehler
        self.schliessen_aufrufe: list[str | None] = []

    async def ventil_oeffnen(self, geraet_id, dauer_sekunden, valve_id=None):
        return None

    async def ventil_schliessen(self, geraet_id, valve_id=None):
        self.schliessen_aufrufe.append(valve_id)
        if self.schliessen_fehler:
            raise ConnectionError("Netz weg (Attrappe)")


@pytest.fixture
async def speicher(tmp_path):
    s = Speicher(str(tmp_path / "t0590.db"))
    await s.verbinden()
    try:
        yield s
    finally:
        await s.schliessen()


def _ws(client: GardenaClient, aktivitaet: str) -> None:
    """Eine WS-Meldung fuer das Bambus-Ventil ueber den echten Pfad."""
    client._verarbeite_ventil_update(VALVE, "Bambus", aktivitaet, "DWC1")


async def _haengender_lauf(
    speicher, *, cache_vor_start: str = "CLOSED",
    client: _ClientOhneNetz | None = None,
):
    """Baut den Zustand nach `ventil.retry_aufgegeben` auf: Lauf offen,
    WS meldet ihn offen, drei Close-Versuche gescheitert."""
    client = client or _ClientOhneNetz()
    _ws(client, cache_vor_start)          # Erstsichtung vor dem Lauf
    sicherung = VentilSicherung(
        client=client, speicher=speicher, ventil_geraet_id=DSWC,
        puffer_sekunden=PUFFER_S, kanal_zu_valve_id={KANAL: VALVE},
    )
    assert await sicherung.bewaessere(KANAL, ZONEN, DAUER_S, Ausloser.AUTOMATIK)
    _ws(client, "MANUAL_WATERING")        # unser eigener Lauf, WS lebt
    for _ in range(MAX_RETRY_VERSUCHE):
        assert await sicherung.stoppe(KANAL, Ausloser.WATCHDOG) is False
    aktiv = sicherung._aktiv[KANAL]
    assert aktiv.close_aufgegeben_seit is not None   # langsamer Modus
    return client, sicherung, aktiv


def _ueberfaellig(aktiv) -> datetime:
    return aktiv.gestartet + timedelta(seconds=DAUER_S + PUFFER_S + 60)


def _raeume_timer(sicherung) -> None:
    for a in sicherung._aktiv.values():
        if a.timer_handle:
            a.timer_handle.cancel()


async def _schliessen(speicher) -> list[VentilEreignis]:
    ereignisse: list[VentilEreignis] = []
    for zid in ZONEN:
        ereignisse += [
            e for e in await speicher.hole_ventil_ereignisse(zid)
            if e.aktion == VentilAktion.SCHLIESSEN
        ]
    return ereignisse


# --- (a) Heilung im Entscheidungsloop --------------------------------------


async def test_heilung_raeumt_haengenden_lauf_mit_statuscache_beleg(speicher):
    """Der Realfall: Close scheitert dreimal, danach meldet die WS das Ventil
    zu (der Callback ging verloren -- Replay-Guard, VK-1). Der naechste
    Loop-Durchlauf muss den Kanal freigeben, damit `pruefe_kanal` ihn wieder
    sieht; das SCHLIESSEN wird genau einmal mit dem geplanten Laufende
    nachgetragen (wie beim Neustart, `recover_aus_db`)."""
    client, sicherung, aktiv = await _haengender_lauf(speicher)
    _ws(client, "CLOSED")                 # echter Cloud-Close, nur im Cache
    assert sicherung.ist_aktiv(KANAL)     # Vorbedingung: Kanal haengt
    versuche_vorher = len(client.schliessen_aufrufe)

    laeuft = await main.kanal_laeuft_nach_heilung(
        sicherung, KANAL, jetzt=_ueberfaellig(aktiv),
    )

    assert laeuft is False                # Loop prueft den Kanal wieder
    assert not sicherung.ist_aktiv(KANAL)
    assert await speicher.hole_live_lauf_states(DSWC) == []
    assert aktiv.timer_handle is None     # kein weiterer Close-Versuch
    assert len(client.schliessen_aufrufe) == versuche_vorher
    geschlossen = await _schliessen(speicher)
    assert sorted(e.zone_id for e in geschlossen) == sorted(ZONEN)
    for e in geschlossen:
        assert e.dauer_sekunden == DAUER_S
        assert e.zeitstempel == aktiv.gestartet + timedelta(seconds=DAUER_S)
        assert e.ausloser == Ausloser.AUTOMATIK


async def test_heilung_ohne_beleg_raeumt_nicht(speicher):
    """Ohne Beleg "Ventil zu" koennte das Ventil offen sein: der Lauf bleibt,
    der langsame Close-Retry laeuft weiter."""
    client, sicherung, aktiv = await _haengender_lauf(speicher)
    # Cache zeigt weiter unseren Lauf offen, DB hat kein SCHLIESSEN.
    try:
        laeuft = await main.kanal_laeuft_nach_heilung(
            sicherung, KANAL, jetzt=_ueberfaellig(aktiv),
        )
        assert laeuft is True
        assert sicherung.ist_aktiv(KANAL)
        assert len(await speicher.hole_live_lauf_states(DSWC)) == 1
        assert aktiv.timer_handle is not None   # Retry bleibt geplant
        assert await _schliessen(speicher) == []
    finally:
        _raeume_timer(sicherung)


async def test_heilung_nur_fuer_ueberfaelligen_lauf(speicher):
    """Vor gestartet + dauer + puffer ist der Lauf planmaessig offen --
    dort raeumt die Heilung nicht, auch wenn der Cache zu meldet."""
    client, sicherung, aktiv = await _haengender_lauf(speicher)
    _ws(client, "CLOSED")
    noch_planmaessig = aktiv.gestartet + timedelta(seconds=DAUER_S)
    try:
        assert await sicherung.heile_haengenden_lauf(
            KANAL, jetzt=noch_planmaessig,
        ) is False
        assert sicherung.ist_aktiv(KANAL)
    finally:
        _raeume_timer(sicherung)


async def test_statuscache_von_vor_dem_laufstart_ist_kein_beleg(speicher):
    """Ein CLOSED aus der Zeit VOR dem Oeffnen (danach kam keine WS-Meldung
    mehr -- WS waehrend des ganzen Laufs tot) sagt nichts ueber diesen Lauf."""
    client = _ClientOhneNetz()
    _ws(client, "CLOSED")
    sicherung = VentilSicherung(
        client=client, speicher=speicher, ventil_geraet_id=DSWC,
        puffer_sekunden=PUFFER_S, kanal_zu_valve_id={KANAL: VALVE},
    )
    assert await sicherung.bewaessere(KANAL, ZONEN, DAUER_S, Ausloser.AUTOMATIK)
    for _ in range(MAX_RETRY_VERSUCHE):
        await sicherung.stoppe(KANAL, Ausloser.WATCHDOG)
    aktiv = sicherung._aktiv[KANAL]
    assert client._ventil_status[VALVE] == "CLOSED"   # Cache sagt "zu" ...
    try:
        assert await sicherung.heile_haengenden_lauf(
            KANAL, jetzt=_ueberfaellig(aktiv),
        ) is False                                     # ... belegt aber nichts
        assert sicherung.ist_aktiv(KANAL)
    finally:
        _raeume_timer(sicherung)


async def test_dhs_nachtrag_ist_beleg_ohne_doppel_schliessen(speicher):
    """Der Beleg kommt spaeter aus der DB: der DHS-Backfill traegt den Close
    mit dem Ausloeser des OEFFNEN nach. Die Heilung raeumt und schreibt KEIN
    zweites SCHLIESSEN."""
    client, sicherung, aktiv = await _haengender_lauf(speicher)
    dhs_stop = aktiv.gestartet + timedelta(seconds=DAUER_S - 3)
    for zid in ZONEN:
        await speicher.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=dhs_stop, zone_id=zid, ventil_id=VALVE,
            aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=DAUER_S - 3,
            ausloser=Ausloser.AUTOMATIK,
        ))

    assert await sicherung.heile_haengenden_lauf(
        KANAL, jetzt=_ueberfaellig(aktiv),
    ) is True
    assert not sicherung.ist_aktiv(KANAL)
    geschlossen = await _schliessen(speicher)
    assert len(geschlossen) == len(ZONEN)
    assert {e.dauer_sekunden for e in geschlossen} == {DAUER_S - 3}


class _ClientMitHaengendemClose(_ClientOhneNetz):
    """Netz ist zurueck, die Cloud-Antwort auf den Close steht aber noch aus,
    bis der Test sie freigibt."""

    def __init__(self):
        super().__init__()
        self.haengen = False
        self.close_angefragt = asyncio.Event()
        self.close_frei = asyncio.Event()

    async def ventil_schliessen(self, geraet_id, valve_id=None):
        if self.haengen:
            self.close_angefragt.set()
            await self.close_frei.wait()
        await super().ventil_schliessen(geraet_id, valve_id=valve_id)


class _SpeicherMitSchreibPause(Speicher):
    """Haelt das erste SCHLIESSEN an, das geschrieben wird, WAEHREND ein
    Close-Befehl noch auf die Cloud wartet. Richtig schreibt in diesem Fenster
    niemand. Schreibt dort doch jemand (die Heilung ohne Stop-Lock), laesst die
    Pause die Cloud-Antwort dazwischenkommen, wie es die echte DB-Latenz
    erlaubt."""

    def __init__(self, pfad: str):
        super().__init__(pfad)
        self.close_im_flug = False
        self.angehalten = asyncio.Event()
        self.weiter = asyncio.Event()

    async def speichere_ventil_ereignis(self, ereignis: VentilEreignis) -> None:
        if (
            self.close_im_flug
            and ereignis.aktion == VentilAktion.SCHLIESSEN
            and not self.angehalten.is_set()
        ):
            self.angehalten.set()
            await self.weiter.wait()
        await super().speichere_ventil_ereignis(ereignis)


async def test_heilung_waehrend_laufendem_close_raeumt_nicht_und_blockiert_nicht(
    tmp_path,
):
    """Der langsame Close-Retry (Watchdog-Timer) laeuft, das Netz ist zurueck,
    die Cloud-Antwort steht noch aus. In diesem Moment fragt der
    Entscheidungsloop die Heilung. Der Beleg "Ventil zu" liegt vor, ohne laufenden Close wuerde also
    geraeumt.

    Richtig (Stop-Lock belegt): die Heilung tut in dieser Runde nichts und
    kehrt sofort zurueck, der Close schreibt genau ein SCHLIESSEN je Zone.
    Gemessen ohne `lock.locked()` UND ohne `async with lock`: die Heilung
    raeumt unter dem laufenden Close weg; kommt die Cloud-Antwort, waehrend
    sie ihr SCHLIESSEN schreibt, stehen zwei SCHLIESSEN je Zone in der DB
    (automatik + watchdog; Doppelzaehlung im Wasserbudget). Nur ohne
    `lock.locked()`: kein Doppel, aber die Heilung wartet den ganzen
    Close-Versuch ab und haelt damit den Loop an."""
    sp = _SpeicherMitSchreibPause(str(tmp_path / "t0590_lock.db"))
    await sp.verbinden()
    client = _ClientMitHaengendemClose()
    sicherung = None
    try:
        _, sicherung, aktiv = await _haengender_lauf(sp, client=client)
        _ws(client, "CLOSED")                 # Beleg liegt vor
        client.schliessen_fehler = False      # Netz zurueck ...
        client.haengen = True                 # ... Antwort steht noch aus
        sp.close_im_flug = True
        stop = asyncio.create_task(sicherung.stoppe(KANAL, Ausloser.WATCHDOG))
        await asyncio.wait_for(client.close_angefragt.wait(), 5)

        heilung = asyncio.create_task(main.kanal_laeuft_nach_heilung(
            sicherung, KANAL, jetzt=_ueberfaellig(aktiv),
        ))
        angehalten = asyncio.create_task(sp.angehalten.wait())
        await asyncio.wait(
            {heilung, angehalten}, timeout=0.5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        heilung_vor_cloud_antwort = heilung.done()
        sp.close_im_flug = False
        client.close_frei.set()               # Cloud antwortet: Close ok
        await asyncio.wait({stop}, timeout=2)
        sp.weiter.set()
        stop_ok, laeuft = await asyncio.wait_for(
            asyncio.gather(stop, heilung), 5,
        )
        angehalten.cancel()

        geschlossen = await _schliessen(sp)
        assert sorted((e.zone_id, e.ausloser) for e in geschlossen) == sorted(
            (z, Ausloser.WATCHDOG) for z in ZONEN
        ), f"Heilung hat unter dem laufenden Close geraeumt: {geschlossen}"
        assert heilung_vor_cloud_antwort, (
            "Heilung wartet auf den laufenden Close und haelt den Loop an"
        )
        assert laeuft is True                 # diese Runde: Kanal laeuft noch
        assert stop_ok is True
        assert not sicherung.ist_aktiv(KANAL)
        assert await sp.hole_live_lauf_states(DSWC) == []
        assert aktiv.timer_handle is None     # kein Retry-Timer uebrig
    finally:
        if sicherung is not None:
            _raeume_timer(sicherung)
        await sp.schliessen()


def _zone(zone_id: str, name: str) -> ZonenKonfig:
    return ZonenKonfig(
        zone_id=zone_id, name=name, modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=35.0, feuchte_kritisch=20.0,
        max_dauer_sekunden=1800, min_pause_minuten=120,
        tages_budget_sekunden=3600.0,
        bevorzugte_zeiten=[ZeitFenster(von="05:00", bis="07:00")],
    )


async def test_orphan_close_ist_kein_beleg(speicher):
    """Der OrphanCloseJob schreibt rein zeitbasiert ein SCHLIESSEN, ohne die
    Hardware zu fragen (HW-6). Das darf die Heilung nicht als "Ventil zu"
    werten -- sonst raeumt sie bei einem womoeglich offenen Ventil."""
    client, sicherung, aktiv = await _haengender_lauf(speicher)
    orphan = OrphanCloseJob(
        speicher, [_zone("bambuswald", "Bambuswald"),
                   _zone("bambuswald_yogaraum", "Yogaraum")],
    )
    scan = aktiv.gestartet + timedelta(hours=2)
    assert await orphan.aktualisiere(jetzt=scan) == len(ZONEN)
    # Vorbedingung: es liegt ein SCHLIESSEN seit Laufstart vor.
    assert await speicher.existiert_schliessen_seit(VALVE, aktiv.gestartet)
    try:
        assert await sicherung.heile_haengenden_lauf(KANAL, jetzt=scan) is False
        assert sicherung.ist_aktiv(KANAL)
    finally:
        _raeume_timer(sicherung)


@pytest.mark.parametrize("cloud", ["CLOSED", "MANUAL_WATERING"],
                         ids=["geheilt", "gegenfall_ohne_beleg"])
async def test_entscheidungsloop_prueft_den_geheilten_kanal_in_derselben_runde(
    speicher, monkeypatch, capsys, cloud,
):
    """Die Naht in `_entscheidungsloop` (T-0610 E6, vorher AST-Test): der
    Zweig "Kanal laeuft" geht ueber `kanal_laeuft_nach_heilung`. Der echte
    Loop faehrt eine Runde mit dem haengenden Lauf des Realfalls. Ist das
    Ventil laut WS zu, wird der Kanal geheilt und in DERSELBEN Runde normal
    geprueft (`pruefe_kanal`), statt als laufend den Max-Stop zu fragen.
    Gegenfall ohne Hardware-Beleg: der Kanal bleibt laufend (Max-Stop-Frage),
    damit der geheilt-Fall nicht aus einem anderen Grund gruen ist.

    Harmlos auch ohne die Naht: die Motor-Attrappe meldet keinen Bedarf und
    keinen Max-Stop, die Cloud-Kommandos sind Attrappen, die DB liegt in
    tmp_path."""
    import bewaesserung.api_server as api_server
    from bewaesserung.modelle import (
        GardenaKonfig, GesamtKonfig, SpeicherKonfig, WetterKonfig,
        WetterStandortKonfig,
    )
    from bewaesserung.pre_soak import PreSoakManager
    from unittest.mock import MagicMock
    from test_t0607_kanal_block_loop import _MotorAttrappe

    client, sicherung, aktiv = await _haengender_lauf(speicher)
    _ws(client, cloud)
    # Ueberfaellig nach der Wanduhr (der Loop fragt ohne `jetzt`).
    aktiv.gestartet = datetime.now() - timedelta(seconds=DAUER_S + PUFFER_S + 60)
    mgr = PreSoakManager(sicherung, speicher=speicher)
    monkeypatch.setattr(api_server._laufzeit, "pre_soak_managers", {DSWC: mgr})
    monkeypatch.setattr(api_server._laufzeit, "pre_soak_manager", mgr)
    monkeypatch.setattr(main, "ERSTE_PRUEFUNG_VERZOEGERUNG_S", 0.01)
    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[
            ZonenKonfig(
                zone_id=zid, name=zid, modus=ZonenModus.AUTOMATIK,
                ventil_kanal=KANAL, ventil_geraet_id=DSWC, auto_loop_opt_in=True,
            )
            for zid in ZONEN
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.5, laenge=13.4)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        ventilsteuerung_aktiv=True,
    )
    stop = asyncio.Event()
    motor = _MotorAttrappe(stop)
    try:
        await asyncio.wait_for(main._entscheidungsloop(
            motor=motor, konfig=konfig, benachrichtiger=MagicMock(),
            verarbeiter=MagicMock(), stop_event=stop, speicher=speicher,
            ventil_sicherungen={DSWC: sicherung},
        ), timeout=15)
    finally:
        _raeume_timer(sicherung)
    aus = capsys.readouterr().out
    assert "entscheidungsloop.fehler" not in aus, aus[-1500:]
    assert "entscheidungsloop.heartbeat" in aus, "Runde nicht zu Ende gelaufen"
    if cloud == "CLOSED":
        assert not sicherung.ist_aktiv(KANAL)
        assert motor.kanal_fragen == [KANAL]
        assert motor.max_stop_fragen == []
    else:
        assert sicherung.ist_aktiv(KANAL)
        assert motor.kanal_fragen == []
        assert motor.max_stop_fragen == [KANAL]


# --- (c) Arbiter -------------------------------------------------------------


class _SpeicherOhneEreignisse:
    """Isoliert die Engine-Quelle des Arbiters: keine Ereignis-Sicht (die
    sperrt ein OEFFNEN ohne SCHLIESSEN ohnehin nur MAX_OFFEN lang), keine
    Pre-Soak-Zustaende."""

    async def hole_offene_ventil_kanaele(self, seit, bis=None):
        return []

    async def hole_pre_soak_states(self):
        return []


PROFILE = {
    (DSWC, 1): Kanalprofil(("waldblumenhain",), "garten-haupthahn", 6.0, True),
    (DSWC, 2): Kanalprofil(tuple(ZONEN), "garten-haupthahn", 1.87, False),
}


async def _arbiter_mit_bambuslauf(speicher):
    client = _ClientOhneNetz(schliessen_fehler=False)
    sicherung = VentilSicherung(
        client=client, speicher=speicher, ventil_geraet_id=DSWC,
        puffer_sekunden=PUFFER_S, kanal_zu_valve_id={KANAL: VALVE},
    )
    assert await sicherung.bewaessere(KANAL, ZONEN, DAUER_S, Ausloser.AUTOMATIK)
    arbiter = HahnArbiter(
        speicher=_SpeicherOhneEreignisse(), kanalprofile=PROFILE,
        cluster_max_lpm={"garten-haupthahn": 10.0},
    )
    arbiter.registriere_sicherung(DSWC, sicherung)
    return arbiter, sicherung


async def test_arbiter_ueberfaelliger_engine_lauf_sperrt_exklusive_zone_nicht(
    speicher,
):
    """VK-4: der haengende Bambus-Lauf (Kanal 2) sperrte den exklusiven
    waldblumenhain (Kanal 1) bis zum Neustart."""
    arbiter, sicherung = await _arbiter_mit_bambuslauf(speicher)
    aktiv = sicherung._aktiv[KANAL]
    try:
        entscheidung = await arbiter.pruefe(DSWC, 1, jetzt=_ueberfaellig(aktiv))
        assert entscheidung.erlaubt is True, entscheidung.grund
    finally:
        _raeume_timer(sicherung)


async def test_arbiter_frischer_engine_lauf_sperrt_exklusive_zone_weiter(
    speicher,
):
    """Gegenprobe: waehrend der Bambus-Lauf planmaessig laeuft, bleibt die
    exklusive Zone gesperrt."""
    arbiter, sicherung = await _arbiter_mit_bambuslauf(speicher)
    aktiv = sicherung._aktiv[KANAL]
    try:
        entscheidung = await arbiter.pruefe(
            DSWC, 1, jetzt=aktiv.gestartet + timedelta(seconds=60),
        )
        assert entscheidung.erlaubt is False
        assert "bambuswald" in entscheidung.aktive_zonen
    finally:
        _raeume_timer(sicherung)


# --- HW-9: Push beim Wechsel in den langsamen Close-Modus --------------------


class _Benachrichtiger:
    def __init__(self):
        self.gesendet: list[str] = []

    async def sende_text(self, empfaenger: str, text: str) -> bool:
        self.gesendet.append(text)
        return True


async def test_watchdog_meldet_aufgegebenen_close_einmal_mit_klartextnamen(
    speicher,
):
    _, sicherung, aktiv = await _haengender_lauf(speicher)
    bote = _Benachrichtiger()
    job = WatchdogJob(
        speicher=speicher, benachrichtiger=bote,
        konfig=WatchdogKonfig(
            aktiv=True, empfaenger="+490000", intervall_minuten=1,
            throttle_stunden=24,
        ),
        zonen=[_zone("bambuswald", "Bambuswald"),
               _zone("bambuswald_yogaraum", "Yogaraum")],
        gardena_zone_ids=[],
        ventil_sicherungen={DSWC: sicherung},
    )
    jetzt = aktiv.close_aufgegeben_seit + timedelta(minutes=2)
    try:
        await job.pruefe_und_sende_wenn_faellig(jetzt=jetzt)
        await job.pruefe_und_sende_wenn_faellig(
            jetzt=jetzt + timedelta(minutes=30),
        )
    finally:
        _raeume_timer(sicherung)

    meldungen = [t for t in bote.gesendet if "nicht schließen" in t]
    assert len(meldungen) == 1            # gedrosselt: nur einmal
    assert "Bambuswald" in meldungen[0] and "Yogaraum" in meldungen[0]
    assert DSWC not in meldungen[0]       # keine UUID im Text


# T-0610 AT-3 (14.10.2026): die Verdrahtung in main.ausfuehren prueft jetzt ein
# echter Start-Lauf, test_t0610_at11_start_lauf.py::test_watchdog_und_orphan_scan_kennen_die_sicherungen
# (vorher AST-Test 11 aus dem Nachtrag 30.09., Triage E6).


# --- (e) tools/restart_wenn_ruhig.sh -----------------------------------------


WURZEL = Path(__file__).resolve().parents[2]
SKRIPT = WURZEL / "tools" / "restart_wenn_ruhig.sh"
# `tools/` ist privat und fehlt im oeffentlichen Snapshot. Erkannt wird der
# Snapshot wie in test_ui_pruefserver.py am fehlenden `config/default.yaml` --
# NICHT am fehlenden Skript, sonst wuerde ein privat geloeschtes Skript seinen
# Test still ueberspringen statt ihn scheitern zu lassen.
OEFFENTLICHER_SNAPSHOT = not (WURZEL / "config" / "default.yaml").exists()
SKRIPT_TEST = pytest.mark.skipif(
    OEFFENTLICHER_SNAPSHOT or shutil.which("sqlite3") is None,
    reason="oeffentlicher Snapshot (tools/ privat) oder sqlite3-CLI fehlt",
)


async def _ruhe_check(tmp_path, gestartet: datetime) -> subprocess.CompletedProcess:
    """Ruhe-Check gegen eine Temp-DB mit EINER live_lauf_state-Zeile.

    Die DB bleibt waehrend des Skriptlaufs geoeffnet wie im Betrieb (der
    Dienst haelt sie offen): im WAL-Modus kann `sqlite3 -readonly` eine
    geschlossene DB ohne -shm-Datei nicht oeffnen.

    Kopie des Skripts in tmp_path: REPO zeigt dorthin, das Log landet dort,
    und ohne service.sh daneben koennte selbst ein versehentlicher Restart
    nichts anrichten. `--trocken` startet ohnehin nie neu."""
    pfad = tmp_path / "ruhe.db"
    s = Speicher(str(pfad))
    await s.verbinden()
    try:
        await s.setze_live_lauf_state(
            kanal=KANAL, valve_id=VALVE, geraet_id=DSWC, zone_ids=ZONEN,
            dauer_sekunden=3240, ausloser="automatik", gestartet_am=gestartet,
        )
        ziel = tmp_path / "repo" / "tools" / SKRIPT.name
        ziel.parent.mkdir(parents=True)
        shutil.copy2(SKRIPT, ziel)
        env = dict(os.environ, DB=str(pfad), INTERVALL_S="0",
                   MAX_WARTEN_MIN="0", NOETIGE_TREFFER="1")
        return subprocess.run(
            ["bash", str(ziel), "--trocken"], env=env, capture_output=True,
            text=True, timeout=60,
        )
    finally:
        await s.schliessen()


@SKRIPT_TEST
async def test_restart_skript_haengende_zeile_gilt_als_ruhig(tmp_path):
    """Realfall 25./26.09.: Lauf 16:44:49, 3240 s geplant, Zeile hing bis zum
    Neustart. Das Skript wartete vergeblich und brach nach MAX_WARTEN_MIN ab."""
    ergebnis = await _ruhe_check(tmp_path, datetime.now() - timedelta(hours=3))
    assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr
    assert "Ruhefenster erreicht" in ergebnis.stdout
    assert "haengender Lauf" in ergebnis.stdout   # im Log sichtbar


@SKRIPT_TEST
async def test_restart_skript_laufende_zeile_haelt_den_restart_auf(tmp_path):
    ergebnis = await _ruhe_check(tmp_path, datetime.now() - timedelta(minutes=10))
    assert ergebnis.returncode == 2, ergebnis.stdout + ergebnis.stderr
    assert "aktiver Lauf" in ergebnis.stdout
