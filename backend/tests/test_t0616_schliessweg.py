"""T-0616 (Punkte 2-5): Restpunkte am Ventil-Schliessweg aus T-0591, T-0592,
T-0595 und T-0604.

Je Test eine Grenze; die Testkoerper sind auch ohne den geprueften Schutz
harmlos: Cloud-Kommandos sind Attrappen (kein Netz), nur temporaere
Datenbanken.

Punkt 2 (T-0592, Mutanten n10/n11): zwei Identitaetspruefungen
`self._aktiv.get(kanal) is aktiv` schuetzen einen NEUEN Lauf auf demselben
Kanal davor, dass ein noch laufender Pfad des VORLAUFS ihn abraeumt bzw.
schliesst. Ausgeloest wird das nur, wenn zwischen zwei `await` des alten
Pfads der Vorlauf beendet und ein neuer Lauf gestartet wird -- die Tests
halten den alten Pfad dazu an einer Speicher-Pause an.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import (
    KEINE_WASSER_AUSLOESER,
    Ausloser,
    VentilAktion,
    VentilEreignis,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.orphan_close_job import OrphanCloseJob
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import VentilSicherung
import test_t0590_haengender_kanal as t0590
from test_t0591_echo_unterdrueckung import GERAET as GERAET_0591
from test_t0591_echo_unterdrueckung import KANAL as KANAL_0591
from test_t0591_echo_unterdrueckung import VALVE as VALVE_0591
from test_t0591_echo_unterdrueckung import ZONEN as ZONEN_0591
from test_t0591_echo_unterdrueckung import _EchterClientOhneNetz
from test_t0591_echo_unterdrueckung import _ws as _ws_0591
from test_ventil_sicherung import ClientAttrappe

GERAET = "dswc-1"
KANAL = 1
VALVE = "dswc-1:1"
ZONEN = ["waldblumenhain"]


class _SpeicherMitPause(Speicher):
    """Echter Speicher (temporaere DB); haelt genau einen Aufruf an.

    `pause_bei` = ("schliessen_beleg" | "oeffnen_write"); der erste passende
    Aufruf wartet auf `weiter`, `angehalten` meldet das Anhalten."""

    def __init__(self, pfad: str):
        super().__init__(pfad)
        self.pause_bei: str | None = None
        self.angehalten = asyncio.Event()
        self.weiter = asyncio.Event()
        # Gate W6-1: so viele SCHLIESSEN-Writes scheitern noch.
        self.schliessen_fehler_offen = 0

    async def _vielleicht_anhalten(self, art: str) -> None:
        if self.pause_bei == art and not self.angehalten.is_set():
            self.angehalten.set()
            await self.weiter.wait()

    async def existiert_schliessen_seit(self, *a, **kw) -> bool:
        await self._vielleicht_anhalten("schliessen_beleg")
        return await super().existiert_schliessen_seit(*a, **kw)

    async def speichere_ventil_ereignis(self, ereignis: VentilEreignis) -> None:
        if ereignis.aktion == VentilAktion.OEFFNEN:
            await self._vielleicht_anhalten("oeffnen_write")
        if (ereignis.aktion == VentilAktion.SCHLIESSEN
                and self.schliessen_fehler_offen > 0):
            self.schliessen_fehler_offen -= 1
            raise RuntimeError("database is locked (Attrappe)")
        await super().speichere_ventil_ereignis(ereignis)


class _ClientMitProtokoll(ClientAttrappe):
    def __init__(self):
        super().__init__()
        self.protokoll: list[str] = []

    async def ventil_oeffnen(self, geraet_id, dauer_sekunden, valve_id=None):
        await super().ventil_oeffnen(geraet_id, dauer_sekunden, valve_id=valve_id)
        self.protokoll.append("oeffnen")

    async def ventil_schliessen(self, geraet_id, valve_id=None):
        await super().ventil_schliessen(geraet_id, valve_id=valve_id)
        self.protokoll.append("schliessen")


@pytest.fixture
async def speicher(tmp_path):
    s = _SpeicherMitPause(str(tmp_path / "t0616.db"))
    await s.verbinden()
    try:
        yield s
    finally:
        await s.schliessen()


def _sicherung(client, speicher) -> VentilSicherung:
    return VentilSicherung(
        client=client, speicher=speicher,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET, kanal_zu_valve_id={KANAL: VALVE},
    )


def _raeume(sicherung: VentilSicherung) -> None:
    for aktiv in sicherung._aktiv.values():
        if aktiv.timer_handle:
            aktiv.timer_handle.cancel()


def _closed() -> VentilEreignis:
    return VentilEreignis(
        zeitstempel=datetime.now(), zone_id=ZONEN[0], ventil_id=VALVE,
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=300,
        ausloser=Ausloser.MANUELL,
    )


# --- Punkt 2 (n10): Callback des Vorlaufs raeumt den neuen Lauf nicht ab ---


@pytest.mark.asyncio
async def test_callback_des_vorlaufs_raeumt_neuen_lauf_nicht_ab(speicher):
    """Der Cloud-Close von Lauf A wird verarbeitet; waehrend der Callback auf
    die DB wartet, nimmt ein manueller Stopp Lauf A aus `_aktiv` und Lauf B
    startet auf demselben Kanal. Danach darf der Callback B nicht aus `_aktiv` nehmen --
    sonst liefe B ohne Watchdog (`_watchdog_schliessen` kehrt bei fehlendem
    Eintrag sofort zurueck) und sein Close gaelte als fremd."""
    client = _ClientMitProtokoll()
    sicherung = _sicherung(client, speicher)
    try:
        assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.MANUELL)
        speicher.pause_bei = "schliessen_beleg"
        callback = asyncio.create_task(sicherung.verarbeite_callback(_closed()))
        await asyncio.wait_for(speicher.angehalten.wait(), 2)

        # stoppe nimmt A aus `_aktiv` und wartet dann auf das Ergebnis des
        # Callbacks, der das SCHLIESSEN beansprucht hat (T-0616, Gate W6-1).
        stopp = asyncio.create_task(sicherung.stoppe(KANAL, Ausloser.MANUELL))
        for _ in range(20):
            if not sicherung.ist_aktiv(KANAL):
                break
            await asyncio.sleep(0)
        assert not sicherung.ist_aktiv(KANAL)
        assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.MANUELL)
        lauf_b = sicherung._aktiv[KANAL]

        speicher.weiter.set()
        assert await asyncio.wait_for(callback, 2) is True
        assert await asyncio.wait_for(stopp, 2) is True

        assert sicherung._aktiv.get(KANAL) is lauf_b
        # Isomorphie: auch die persistierte Zeile (je Geraet+Kanal) gehoert
        # B; geloescht kennte ein Neustart den offenen Lauf nicht.
        states = await speicher.hole_live_lauf_states(GERAET)
        assert [s["gestartet_am"] for s in states] == [lauf_b.gestartet]
    finally:
        _raeume(sicherung)


# --- Punkt 2 (n11): Notfall im Flug schliesst nicht den neuen Lauf ---------


@pytest.mark.asyncio
async def test_notfall_im_oeffnen_pfad_schliesst_nicht_den_folgelauf(speicher):
    """Lauf A steht schon in `_aktiv`, sein OEFFNEN wird noch geschrieben, da
    kommt der Notfall-Stopp (schliesst A) und danach startet Andre B von Hand.
    Erst dann sieht der Oeffnen-Pfad von A die neue Notfall-Epoche. Er darf
    nur A schliessen (schon geschehen), nicht B."""
    client = _ClientMitProtokoll()
    sicherung = _sicherung(client, speicher)
    try:
        speicher.pause_bei = "oeffnen_write"
        start_a = asyncio.create_task(
            sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.MANUELL)
        )
        await asyncio.wait_for(speicher.angehalten.wait(), 2)
        await sicherung.notfall_stopp()
        assert not sicherung.ist_aktiv(KANAL)
        assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.MANUELL)
        lauf_b = sicherung._aktiv[KANAL]

        speicher.weiter.set()
        assert await asyncio.wait_for(start_a, 2) is False

        assert sicherung._aktiv.get(KANAL) is lauf_b
        # Letztes Kommando an das Ventil ist das Oeffnen von B.
        assert client.protokoll[-1] == "oeffnen"
        # Isomorphie: der Oeffnen-Pfad von A ueberschreibt die Zeile von B
        # nicht mit seinen Werten (Start, Dauer, Ausloeser).
        states = await speicher.hole_live_lauf_states(GERAET)
        assert [s["gestartet_am"] for s in states] == [lauf_b.gestartet]
    finally:
        _raeume(sicherung)


# --- Punkt 3 (T-0604): Recovery- und Startup-Close legen das DHS-Paar still ---
#
# Ein gardena_web-Paar neben einem eigenen Lauf entsteht nur, wenn kein
# Live-OEFFNEN im DHS-Intervall liegt -- etwa weil der OEFFNEN-Write
# scheiterte (T-0558: Ventil offen, Ereignis fehlt). Endet der Lauf dann
# nicht ueber Callback oder stoppe, sondern ueber den Close aus
# `live_lauf_state` nach einem Neustart, zaehlte er doppelt.

DHS_START = datetime(2026, 8, 30, 13, 33, 53)
DHS_STOP = datetime(2026, 8, 30, 14, 14, 49)
DHS_DAUER = 2458
LIVE_START = DHS_START + timedelta(seconds=10)
LIVE_DAUER = 2400


async def _lauf_ohne_oeffnen_mit_dhs_paar(speicher) -> None:
    """live_lauf_state eines eigenen Laufs (OEFFNEN fehlt) plus das
    gardena_web-Paar, das der DHS-Nachtrag dafuer schrieb."""
    await speicher.setze_live_lauf_state(
        kanal=KANAL, valve_id=VALVE, geraet_id=GERAET, zone_ids=ZONEN,
        dauer_sekunden=LIVE_DAUER, ausloser=Ausloser.AUTOMATIK.value,
        gestartet_am=LIVE_START,
    )
    for aktion, zeit, dauer in (
        (VentilAktion.OEFFNEN, DHS_START, 0),
        (VentilAktion.SCHLIESSEN, DHS_STOP, DHS_DAUER),
    ):
        await speicher.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=zeit, zone_id=ZONEN[0], ventil_id="gardena_web",
            aktion=aktion, dauer_sekunden=dauer, ausloser=Ausloser.AUTOMATIK,
        ))


async def _zaehlende_schliessen(speicher) -> list[VentilEreignis]:
    return [
        e for e in await speicher.hole_ventil_ereignisse(ZONEN[0])
        if e.aktion == VentilAktion.SCHLIESSEN
        and e.ausloser not in KEINE_WASSER_AUSLOESER
    ]


@pytest.mark.asyncio
async def test_recovery_close_legt_dhs_paar_still(speicher):
    """Neustart nach Laufende: `recover_aus_db` traegt das SCHLIESSEN aus
    `live_lauf_state` nach. Der Lauf darf danach nur einmal zaehlen."""
    await _lauf_ohne_oeffnen_mit_dhs_paar(speicher)
    sicherung = _sicherung(ClientAttrappe(), speicher)

    assert await sicherung.recover_aus_db() == 0

    zaehlend = await _zaehlende_schliessen(speicher)
    assert [(e.ventil_id, e.dauer_sekunden) for e in zaehlend] == [
        (VALVE, LIVE_DAUER),
    ]


@pytest.mark.asyncio
async def test_startup_close_legt_dhs_paar_still(speicher):
    """Neustart, die Cloud meldet das Ventil zu: `startup_check` schreibt
    das SCHLIESSEN. Der Lauf darf danach nur einmal zaehlen."""
    await _lauf_ohne_oeffnen_mit_dhs_paar(speicher)
    sicherung = _sicherung(ClientAttrappe(), speicher)   # offene_valves leer

    await sicherung.startup_check()

    zaehlend = await _zaehlende_schliessen(speicher)
    assert [e.ventil_id for e in zaehlend] == [VALVE]


# --- Punkt 4 (T-0595): Reconnect-Sync laesst Laeufe einer Sicherung in Ruhe ---


@pytest.mark.asyncio
async def test_reconnect_sync_schliesst_keinen_lauf_einer_sicherung(speicher):
    """Die Cloud meldet beim Reconnect "zu", die Sicherung fuehrt den Lauf
    noch in `_aktiv`. Der Sync darf dafuer kein SCHLIESSEN schreiben: seine
    Dauer (`jetzt - OEFFNEN`, gedeckelt auf max_dauer bzw. 3600 s) umgeht
    den Deckel `dauer + puffer` aus T-0572, und weil sein SCHLIESSEN als
    Beleg zaehlt, verwirft der spaetere Watchdog-Close seinen richtigen Wert
    als Doppel. Den Close schreibt die Sicherung selbst (Watchdog, Callback,
    Heilung), wie beim zeitbasierten Scan (T-0595 AK2)."""
    sicherung = _sicherung(ClientAttrappe(), speicher)
    job = OrphanCloseJob(speicher=speicher, zonen=[ZonenKonfig(
        zone_id=ZONEN[0], name=ZONEN[0], modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=35.0, max_dauer_sekunden=3600,
    )])
    job.setze_ventil_sicherungen({GERAET: sicherung})
    try:
        assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.AUTOMATIK)
        gestartet = sicherung._aktiv[KANAL].gestartet

        n = await job.sync_aus_live_state(
            {ZONEN[0]: "zu"}, jetzt=gestartet + timedelta(minutes=50),
        )

        assert n == 0
        assert await _zaehlende_schliessen(speicher) == []
    finally:
        _raeume(sicherung)


# --- Punkt 5 (T-0591): Cache ZU + Watchdog im ms-Abstand -------------------


class _EchterClientMitNetzpause(_EchterClientOhneNetz):
    """Echter Statuscache; Cloud-Kommandos sind Attrappen, die wie ein
    HTTP-Aufruf einmal an die Ereignisschleife abgeben."""

    async def ventil_schliessen(self, geraet_id, valve_id=None):
        await asyncio.sleep(0)


async def _schliessen_aller_zonen(speicher, zonen) -> list[VentilEreignis]:
    ergebnis: list[VentilEreignis] = []
    for zid in zonen:
        ergebnis += [
            e for e in await speicher.hole_ventil_ereignisse(zid)
            if e.aktion == VentilAktion.SCHLIESSEN
        ]
    return ergebnis


@pytest.mark.asyncio
async def test_watchdog_im_ms_abstand_zum_cloud_close_schreibt_ein_schliessen(
    speicher,
):
    """Die Cloud schliesst den Lauf; der Client hat das CLOSED schon im
    Statuscache, der Callback-Task laeuft aber erst beim naechsten await.
    Genau jetzt feuert der Watchdog: `stoppe` setzt keinen Echo-Schluessel
    (Cache ZU, T-0591 AK2), und der Callback findet den Lauf noch in
    `_aktiv`. Beide pruefen "schon ein SCHLIESSEN?" vor ihrem Write -- ohne
    Beanspruchung schrieben beide (automatik + watchdog, doppelt in der
    Bilanz). Es darf genau ein SCHLIESSEN entstehen."""
    client = _EchterClientMitNetzpause()
    client.registriere_zone_namen({"Bambus": ZONEN_0591[0]})
    _ws_0591(client, "CLOSED")                      # Erstsichtung
    sicherung = VentilSicherung(
        client=client, speicher=speicher, ventil_geraet_id=GERAET_0591,
        kanal_zu_valve_id={KANAL_0591: VALVE_0591},
    )
    try:
        assert await sicherung.bewaessere(
            KANAL_0591, ZONEN_0591, 300, Ausloser.AUTOMATIK,
        )
        _ws_0591(client, "MANUAL_WATERING")
        _ws_0591(client, "CLOSED")                  # Cloud-Close im Cache
        callback = asyncio.create_task(sicherung.verarbeite_callback(
            VentilEreignis(
                zeitstempel=datetime.now(), zone_id=ZONEN_0591[0],
                ventil_id=VALVE_0591, aktion=VentilAktion.SCHLIESSEN,
                dauer_sekunden=1, ausloser=Ausloser.MANUELL,
            ),
        ))
        assert await sicherung.stoppe(KANAL_0591, Ausloser.WATCHDOG)
        assert await asyncio.wait_for(callback, 2) is True

        schliessen = await _schliessen_aller_zonen(speicher, ZONEN_0591)
        assert len(schliessen) == 1
        assert not sicherung.ist_aktiv(KANAL_0591)
    finally:
        _raeume(sicherung)


@pytest.mark.asyncio
async def test_heilung_neben_laufendem_callback_schreibt_kein_zweites_schliessen(
    speicher,
):
    """T-0590-Heilung und echter Cloud-Close treffen zusammen: der Callback
    hat den Lauf gefunden und steckt im ersten DB-await, da raeumt die
    Heilung (Beleg: Statuscache ZU). Je Zone darf nur ein SCHLIESSEN stehen."""
    client, sicherung, aktiv = await t0590._haengender_lauf(speicher)
    try:
        t0590._ws(client, "CLOSED")
        callback = asyncio.create_task(sicherung.verarbeite_callback(
            VentilEreignis(
                zeitstempel=datetime.now(), zone_id=t0590.ZONEN[0],
                ventil_id=t0590.VALVE, aktion=VentilAktion.SCHLIESSEN,
                dauer_sekunden=60, ausloser=Ausloser.MANUELL,
            ),
        ))
        await asyncio.sleep(0)          # Callback laeuft bis zum ersten await
        assert await sicherung.heile_haengenden_lauf(
            t0590.KANAL, jetzt=t0590._ueberfaellig(aktiv),
        )
        assert await asyncio.wait_for(callback, 2) is True

        schliessen = await _schliessen_aller_zonen(speicher, t0590.ZONEN)
        assert sorted(e.zone_id for e in schliessen) == sorted(t0590.ZONEN)
    finally:
        t0590._raeume_timer(sicherung)


# --- Gate W6-1: ein gescheiterter Callback-Write sperrt den Fallback nicht ---


@pytest.mark.asyncio
async def test_watchdog_schreibt_fallback_wenn_callback_write_scheitert(
    speicher,
):
    """Der Callback beansprucht das SCHLIESSEN, sein DB-Write scheitert.
    stoppe (Watchdog), das im Close-Befehl hing, muss dann selbst schreiben
    -- sonst steht ein OEFFNEN ohne SCHLIESSEN, bis der Orphan-Scan kommt
    (vor d75efd9 schrieb stoppe hier ein SCHLIESSEN)."""
    client = _EchterClientMitNetzpause()
    client.registriere_zone_namen({"Bambus": ZONEN_0591[0]})
    _ws_0591(client, "CLOSED")
    sicherung = VentilSicherung(
        client=client, speicher=speicher, ventil_geraet_id=GERAET_0591,
        kanal_zu_valve_id={KANAL_0591: VALVE_0591},
    )
    try:
        assert await sicherung.bewaessere(
            KANAL_0591, ZONEN_0591, 300, Ausloser.AUTOMATIK,
        )
        _ws_0591(client, "MANUAL_WATERING")
        _ws_0591(client, "CLOSED")
        speicher.schliessen_fehler_offen = 1        # Callback-Write scheitert
        callback = asyncio.create_task(sicherung.verarbeite_callback(
            VentilEreignis(
                zeitstempel=datetime.now(), zone_id=ZONEN_0591[0],
                ventil_id=VALVE_0591, aktion=VentilAktion.SCHLIESSEN,
                dauer_sekunden=1, ausloser=Ausloser.MANUELL,
            ),
        ))
        assert await sicherung.stoppe(KANAL_0591, Ausloser.WATCHDOG)
        assert await asyncio.wait_for(callback, 2) is True

        schliessen = await _schliessen_aller_zonen(speicher, ZONEN_0591)
        assert [e.ausloser for e in schliessen] == [Ausloser.WATCHDOG]
    finally:
        _raeume(sicherung)


@pytest.mark.asyncio
async def test_heilung_schreibt_fallback_wenn_callback_write_scheitert(
    speicher,
):
    """Wie oben fuer die Heilung: der Callback steckt im Write und scheitert;
    die Heilung muss das SCHLIESSEN dann nachtragen."""
    client, sicherung, aktiv = await t0590._haengender_lauf(speicher)
    try:
        t0590._ws(client, "CLOSED")
        speicher.schliessen_fehler_offen = 1
        callback = asyncio.create_task(sicherung.verarbeite_callback(
            VentilEreignis(
                zeitstempel=datetime.now(), zone_id=t0590.ZONEN[0],
                ventil_id=t0590.VALVE, aktion=VentilAktion.SCHLIESSEN,
                dauer_sekunden=60, ausloser=Ausloser.MANUELL,
            ),
        ))
        await asyncio.sleep(0)
        assert await sicherung.heile_haengenden_lauf(
            t0590.KANAL, jetzt=t0590._ueberfaellig(aktiv),
        )
        assert await asyncio.wait_for(callback, 2) is True

        schliessen = await _schliessen_aller_zonen(speicher, t0590.ZONEN)
        assert sorted(e.zone_id for e in schliessen) == sorted(t0590.ZONEN)
    finally:
        t0590._raeume_timer(sicherung)


# --- Gate W6-2: Schliess-Pfade raeumen keinen Folgelauf ab -------------------


class _ClientZuMitPause(ClientAttrappe):
    """Statuscache meldet "zu seit Laufstart" (kein Echo-Schluessel, T-0591
    AK2); der Close-Befehl haelt an, bis `weiter` gesetzt ist."""

    def __init__(self):
        super().__init__()
        self.angehalten = asyncio.Event()
        self.weiter = asyncio.Event()

    def meldet_ventil_zu_seit(self, valve_id, seit) -> bool:
        return True

    async def ventil_schliessen(self, geraet_id, valve_id=None):
        await super().ventil_schliessen(geraet_id, valve_id=valve_id)
        self.angehalten.set()
        await self.weiter.wait()


async def _live_state_starts(speicher, geraet=GERAET) -> list[datetime]:
    return [s["gestartet_am"] for s in await speicher.hole_live_lauf_states(geraet)]


@pytest.mark.asyncio
async def test_stoppe_erfolg_raeumt_keinen_folgelauf_ab(speicher):
    """Gate-Repro W6-2: stoppe haengt im Close-Befehl, der Cloud-Close-
    Callback raeumt A, Lauf B startet. Danach darf stoppe weder B aus
    `_aktiv` nehmen noch dessen live_lauf_state-Zeile loeschen -- B liefe
    sonst ohne Watchdog und ohne Recovery."""
    client = _ClientZuMitPause()
    sicherung = _sicherung(client, speicher)
    try:
        assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.MANUELL)
        stopp = asyncio.create_task(sicherung.stoppe(KANAL, Ausloser.WATCHDOG))
        await asyncio.wait_for(client.angehalten.wait(), 2)
        assert await sicherung.verarbeite_callback(_closed()) is True
        assert await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL)
        lauf_b = sicherung._aktiv[KANAL]

        client.weiter.set()
        assert await asyncio.wait_for(stopp, 2) is True

        assert sicherung._aktiv.get(KANAL) is lauf_b
        assert await _live_state_starts(speicher) == [lauf_b.gestartet]
    finally:
        _raeume(sicherung)


@pytest.mark.asyncio
async def test_close_fehlerpfad_mit_beleg_raeumt_keinen_folgelauf_ab(speicher):
    """T-0380-Pfad: der Close-Befehl scheitert, die Beleg-Abfrage haengt;
    derweil raeumt der Cloud-Close-Callback A und B startet. Der Beleg
    (A's SCHLIESSEN) darf B nicht mit abraeumen."""
    client = ClientAttrappe(schliessen_fehler=True)
    sicherung = _sicherung(client, speicher)
    try:
        assert await sicherung.bewaessere(KANAL, ZONEN, 300, Ausloser.MANUELL)
        speicher.pause_bei = "schliessen_beleg"
        stopp = asyncio.create_task(sicherung.stoppe(KANAL, Ausloser.WATCHDOG))
        await asyncio.wait_for(speicher.angehalten.wait(), 2)
        assert await sicherung.verarbeite_callback(_closed()) is True
        client._schliessen_fehler = False
        assert await sicherung.bewaessere(KANAL, ZONEN, 600, Ausloser.MANUELL)
        lauf_b = sicherung._aktiv[KANAL]

        speicher.weiter.set()
        assert await asyncio.wait_for(stopp, 2) is True   # Beleg: zu

        assert sicherung._aktiv.get(KANAL) is lauf_b
        assert await _live_state_starts(speicher) == [lauf_b.gestartet]
    finally:
        _raeume(sicherung)


@pytest.mark.asyncio
async def test_heilung_raeumt_keinen_folgelauf_ab(speicher):
    """Heilung: die Beleg-Abfrage in der DB haengt; derweil raeumt der
    Cloud-Close-Callback A und B startet. Die Heilung darf B nicht
    entfernen (vorher: pop ohne Identitaetspruefung nach dem await)."""
    client, sicherung, aktiv = await t0590._haengender_lauf(speicher)
    try:
        speicher.pause_bei = "schliessen_beleg"   # Statuscache meldet offen
        heilung = asyncio.create_task(sicherung.heile_haengenden_lauf(
            t0590.KANAL, jetzt=t0590._ueberfaellig(aktiv),
        ))
        await asyncio.wait_for(speicher.angehalten.wait(), 2)
        assert await sicherung.verarbeite_callback(VentilEreignis(
            zeitstempel=datetime.now(), zone_id=t0590.ZONEN[0],
            ventil_id=t0590.VALVE, aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=60, ausloser=Ausloser.MANUELL,
        )) is True
        assert await sicherung.bewaessere(
            t0590.KANAL, t0590.ZONEN, 600, Ausloser.MANUELL,
        )
        lauf_b = sicherung._aktiv[t0590.KANAL]

        speicher.weiter.set()
        assert await asyncio.wait_for(heilung, 2) is False

        assert sicherung._aktiv.get(t0590.KANAL) is lauf_b
        assert await _live_state_starts(speicher, t0590.DSWC) == [
            lauf_b.gestartet,
        ]
    finally:
        t0590._raeume_timer(sicherung)


@pytest.mark.asyncio
async def test_heilung_loescht_live_state_eines_folgelaufs_nicht(speicher):
    """Heilung mit Statuscache-Beleg nimmt A sofort aus `_aktiv`; waehrend
    sie das SCHLIESSEN nachtraegt, startet B. Die live_lauf_state-Zeile
    gehoert danach B und darf nicht geloescht werden."""
    client, sicherung, aktiv = await t0590._haengender_lauf(speicher)
    try:
        t0590._ws(client, "CLOSED")               # Beleg ohne await
        speicher.pause_bei = "schliessen_beleg"   # haengt im Nachtragen
        heilung = asyncio.create_task(sicherung.heile_haengenden_lauf(
            t0590.KANAL, jetzt=t0590._ueberfaellig(aktiv),
        ))
        await asyncio.wait_for(speicher.angehalten.wait(), 2)
        assert not sicherung.ist_aktiv(t0590.KANAL)
        assert await sicherung.bewaessere(
            t0590.KANAL, t0590.ZONEN, 600, Ausloser.MANUELL,
        )
        lauf_b = sicherung._aktiv[t0590.KANAL]

        speicher.weiter.set()
        assert await asyncio.wait_for(heilung, 2) is True

        assert sicherung._aktiv.get(t0590.KANAL) is lauf_b
        assert await _live_state_starts(speicher, t0590.DSWC) == [
            lauf_b.gestartet,
        ]
    finally:
        t0590._raeume_timer(sicherung)


# --- Gate W6-7: Notfall-Stopp wartet nicht auf den Schliess-Anspruch --------


class _SpeicherHaengtImCallbackWrite(Speicher):
    """Der ERSTE SCHLIESSEN-Write der Zone `haenge_zone` kehrt nie zurueck
    (der Callback haengt in der DB); alles andere normal."""

    def __init__(self, pfad: str, haenge_zone: str):
        super().__init__(pfad)
        self.haenge_zone: str | None = haenge_zone
        self.haengt = asyncio.Event()

    async def speichere_ventil_ereignis(self, ereignis: VentilEreignis) -> None:
        if (ereignis.aktion == VentilAktion.SCHLIESSEN
                and ereignis.zone_id == self.haenge_zone):
            self.haenge_zone = None
            self.haengt.set()
            await asyncio.Event().wait()
        await super().speichere_ventil_ereignis(ereignis)


class _ClientMitZeiten(ClientAttrappe):
    def __init__(self):
        super().__init__()
        self.t_schliessen: dict[str | None, float] = {}

    async def ventil_schliessen(self, geraet_id, valve_id=None):
        self.t_schliessen.setdefault(valve_id, time.monotonic())
        await super().ventil_schliessen(geraet_id, valve_id=valve_id)


@pytest.mark.asyncio
async def test_notfall_stopp_wartet_nicht_auf_haengenden_callback_write(tmp_path):
    """Gate-Repro W6-7 (Szenario N): der Callback von Kanal 1 hat das
    SCHLIESSEN beansprucht und haengt im DB-Write. Der Notfall-Stopp muss
    Kanal 2 trotzdem sofort schliessen -- stoppe darf unter NOTFALL_STOPP
    nicht bis zu SCHLIESSEN_ANSPRUCH_WARTE_S auf den Anspruch warten
    (gemessen vorher: Close von Kanal 2 erst nach +10,00 s)."""
    zonen = {1: ["waldblumenhain"], 2: ["bambuswald"]}
    ventile = {1: "dswc-1:1", 2: "dswc-1:2"}
    sp = _SpeicherHaengtImCallbackWrite(
        str(tmp_path / "w6_7.db"), haenge_zone=zonen[1][0],
    )
    await sp.verbinden()
    client = _ClientMitZeiten()
    sicherung = VentilSicherung(
        client=client, speicher=sp, ventil_geraet_id="dswc-1",
        kanal_zu_valve_id=dict(ventile),
    )
    callback = None
    try:
        for k in (1, 2):
            assert await sicherung.bewaessere(k, zonen[k], 300, Ausloser.AUTOMATIK)
        callback = asyncio.create_task(sicherung.verarbeite_callback(
            VentilEreignis(
                zeitstempel=datetime.now(), zone_id=zonen[1][0],
                ventil_id=ventile[1], aktion=VentilAktion.SCHLIESSEN,
                dauer_sekunden=300, ausloser=Ausloser.MANUELL,
            ),
        ))
        await asyncio.wait_for(sp.haengt.wait(), 2)

        t0 = time.monotonic()
        await asyncio.wait_for(sicherung.notfall_stopp(), 15)

        assert client.t_schliessen[ventile[2]] - t0 < 1.0
    finally:
        _raeume(sicherung)
        if callback is not None:
            callback.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await callback
        await sp.schliessen()
