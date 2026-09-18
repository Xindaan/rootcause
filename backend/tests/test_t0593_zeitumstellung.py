"""T-0593: Laufdauern der Ventilpfade ueber die Zeitumstellung am 25.10.2026.

Am 25.10. springt die Uhr um 03:00 Sommerzeit auf 02:00 Winterzeit zurueck.
Naive Lokalzeiten ziehen sich dabei gegenseitig falsch voneinander ab: ein
Lauf von 02:40 Sommerzeit bis 02:10 Winterzeit dauert real 30 min, naiv
gerechnet -1800 s. Diese Zahl landete als `dauer_sekunden` im SCHLIESSEN
(Bilanz, ML), und der Pre-Soak zog seine Hauptdose eine Stunde zu spaet nach
(Gesamtreview HW-8). Die Praemisse "nachts um 2 wird nicht gegossen" aus
T-0566 widerlegt die DB: seit 25.07. acht Automatik-OEFFNEN zwischen 01:00
und 03:00 auf Mikrodrip-Kanaelen.

Die Zeitpunkte werden hier so erzeugt, wie `datetime.now()` sie im Betrieb
liefert: naiv-lokal aus einem echten UTC-Zeitpunkt, also mit `fold=1` im
zweiten Durchlauf der Wiederholungsstunde. Die Zeitzone wird fest auf
Europe/Berlin gestellt, damit der Test nicht von der Maschine abhaengt.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import Ausloser
from bewaesserung.pre_soak import PreSoakManager
from test_ventil_sicherung import KANAL, ZONEN, erstelle_sicherung


@pytest.fixture(autouse=True)
def _berlin():
    alt = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    yield
    if alt is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = alt
    time.tzset()


def _lokal(utc_iso: str) -> datetime:
    """Naive Lokalzeit wie aus `datetime.now()` zum angegebenen UTC-Moment."""
    return datetime.fromtimestamp(datetime.fromisoformat(utc_iso).timestamp())


def _start_und_close() -> tuple[datetime, datetime]:
    start = _lokal("2026-10-25T00:40:00+00:00")   # 02:40 Sommerzeit
    close = _lokal("2026-10-25T01:10:00+00:00")   # 02:10 Winterzeit
    # Vorbedingung: genau die Falle aus HW-8 liegt in den Eingaben.
    assert (start.hour, start.minute) == (2, 40)
    assert (close.hour, close.minute) == (2, 10)
    assert (close - start).total_seconds() == -1800
    return start, close


@pytest.mark.asyncio
async def test_cloud_close_nach_der_umstellung_liefert_die_echte_dauer():
    """AK4: Start 02:40 Sommerzeit, Cloud-Close 02:10 Winterzeit.

    Mit dem alten Code -1800 s. Eine reine Untergrenze ergaebe 0 s und
    waere ebenso falsch: real lief das Ventil 30 min.
    """
    start, close = _start_und_close()
    sicherung, _, _ = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 1797, Ausloser.AUTOMATIK)
    aktiv = sicherung._aktiv[KANAL]
    aktiv.gestartet = start

    assert sicherung._gedeckelte_dauer(aktiv, close) == 1800


@pytest.mark.asyncio
async def test_gedeckelte_dauer_wird_nie_negativ():
    """AK2: ein Startzeitpunkt NACH `jetzt` (Uhr zurueckgestellt, oder ein
    aus der DB gelesener Start, dessen `fold` verloren ging) darf keine
    negative `dauer_sekunden` erzeugen."""
    sicherung, _, _ = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 1797, Ausloser.AUTOMATIK)
    aktiv = sicherung._aktiv[KANAL]

    assert sicherung._gedeckelte_dauer(
        aktiv, aktiv.gestartet - timedelta(seconds=60)) == 0


@pytest.mark.asyncio
async def test_lauf_gilt_nach_der_umstellung_rechtzeitig_als_ueberfaellig():
    """Dieselbe Rechnung in der Verfallsregel (Replay-Guard, Heilung,
    HahnArbiter). Naiv lag die Grenze 02:40 + 1827 s = 03:10:27 und wurde
    erst eine Stunde spaeter erreicht; der Hahn galt so lange als belegt."""
    start, _ = _start_und_close()
    sicherung, _, _ = erstelle_sicherung()
    await sicherung.bewaessere(KANAL, ZONEN, 1797, Ausloser.AUTOMATIK)
    sicherung._aktiv[KANAL].gestartet = start

    # Real 1900 s nach dem Start, also 73 s ueber dem Deckel 1797 + 30.
    jetzt = _lokal("2026-10-25T01:11:40+00:00")
    assert sicherung.kanal_lauf_ueberfaellig(KANAL, jetzt) is True


class _Sicherung:
    def __init__(self) -> None:
        self.aufrufe: list[tuple] = []

    async def bewaessere(self, kanal, zone_ids, dauer_s, ausloser,
                         *, lauf_gruppe=None, phase=None) -> bool:
        self.aufrufe.append((phase, dauer_s))
        return True

    async def stoppe(self, kanal, ausloser) -> bool:
        return True

    def ist_aktiv(self, kanal) -> bool:
        return False


@pytest.mark.asyncio
async def test_pre_soak_hauptdose_kommt_puenktlich_ueber_die_umstellung():
    """AK2 (Pre-Soak): Start 02:50 Sommerzeit, Pause 25 min. Faellig ist die
    Hauptdose real 25 min spaeter, um 02:15 Winterzeit. Naiv waren das
    -2100 s, und die Hauptdose kam eine Stunde spaeter auf eine 80 min alte
    Vorbenetzung (T-0553 erlaubt hoechstens 40 min)."""
    sicherung = _Sicherung()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    ok, fehler = await mgr.starte(
        zone_id="bambuswald", kanal=2, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=25, haupt_min=30,
        ausloser=Ausloser.AUTOMATIK,
    )
    assert ok, fehler
    lauf = mgr.laufender_lauf("bambuswald")
    lauf.gestartet_am = _lokal("2026-10-25T00:50:00+00:00")   # 02:50 CEST
    jetzt = _lokal("2026-10-25T01:15:30+00:00")               # 02:15:30 CET
    assert (jetzt.hour, jetzt.minute) == (2, 15)

    await mgr.tick(jetzt)

    assert ("haupt", 1800) in sicherung.aufrufe


@pytest.mark.asyncio
async def test_ws_close_eines_fremden_laufs_ueber_die_umstellung(monkeypatch):
    """AK2, Isomorphie: ein App- oder Zeitplan-Lauf geht nicht durch die
    VentilSicherung. Seine `dauer_sekunden` rechnet der WS-Pfad selbst und
    schreibt sie direkt in die Bilanz; naiv waren das hier -1800 s."""
    from types import SimpleNamespace

    import bewaesserung.gardena_client as gc_mod
    from bewaesserung.gardena_client import GardenaClient
    from bewaesserung.modelle import VentilAktion

    start, close = _start_und_close()
    uhr = {"jetzt": start}

    class _Uhr(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return uhr["jetzt"]

    monkeypatch.setattr(gc_mod, "datetime", _Uhr)

    client = GardenaClient(client_id="test", client_secret="test")
    client.registriere_zone_namen({"Bambuswald": "bambuswald"})
    events: list = []

    async def cb(e) -> None:
        events.append(e)

    client.registriere_ventil_callback(cb)
    client._ventil_status["v-1"] = "CLOSED"
    callback = client._erstelle_ventil_callback(geraet_id="dev-1")

    def _melde(aktivitaet: str) -> None:
        callback(SimpleNamespace(
            type="SMART_IRRIGATION_CONTROL", name="Dual WC",
            valves={"v-1": {"name": "Bambuswald", "activity": aktivitaet}},
        ))

    _melde("MANUAL_WATERING")
    # T-0593b: Die WS lebt waehrend des Laufs (Sensor-Events alle paar
    # Minuten). Ohne diese Lebenszeichen waeren 30 min Stille eine
    # Reconnect-Luecke und der Replay-Guard wuerde das CLOSED schlucken --
    # in jeder Nacht. Bis T-0593b blieb das hier nur unbemerkt, weil die
    # naive Luecke ueber die Umstellung negativ war.
    for utc in ("2026-10-25T00:50:00+00:00", "2026-10-25T01:00:00+00:00"):
        client._pruefe_ws_gap_und_armiere(_lokal(utc))
    uhr["jetzt"] = close
    _melde("CLOSED")
    for _ in range(5):
        if not client._aktive_tasks:
            break
        await asyncio.gather(*list(client._aktive_tasks), return_exceptions=True)

    schliessen = [e for e in events if e.aktion == VentilAktion.SCHLIESSEN]
    assert [e.dauer_sekunden for e in schliessen] == [1800]


# =====================================================================
# T-0593b: Nachtrag -- restliche naive Wanduhr-Differenzen
# =====================================================================
#
# Gespeicherte Startzeitpunkte (DB) sind naiv OHNE fold. Hier deshalb als
# `datetime(...)` gebaut (fold=0), `jetzt` dagegen ueber `_lokal` wie aus
# `datetime.now()`.

@pytest.mark.parametrize("name,seit,jetzt_utc,soll_s", [
    # 1. Durchlauf gestartet, 2. Durchlauf gelesen: naiv -1800.
    ("okt_negativ", datetime(2026, 10, 25, 2, 40), "2026-10-25T01:10:00+00:00", 1800),
    # Start im 2. Durchlauf aus der DB: `verstrichen_s` haette 3900 gesagt.
    ("okt_zweiter_durchlauf", datetime(2026, 10, 25, 2, 10), "2026-10-25T01:15:00+00:00", 300),
    # Eindeutiger Start vor der Wiederholungsstunde: naiv 1200.
    ("okt_eindeutig", datetime(2026, 10, 25, 1, 50), "2026-10-25T01:10:00+00:00", 4800),
    # Maerz, 01:50 Winterzeit -> 03:10 Sommerzeit: naiv 4800.
    ("maerz", datetime(2027, 3, 28, 1, 50), "2027-03-28T01:10:00+00:00", 1200),
])
def test_verstrichen_s_gespeichert_deutet_db_stempel(name, seit, jetzt_utc, soll_s):
    from bewaesserung.modelle import verstrichen_s_gespeichert

    assert verstrichen_s_gespeichert(seit, _lokal(jetzt_utc)) == soll_s, name


@pytest.mark.parametrize("name,gestartet,jetzt_utc", [
    # Maerz: Lauf 01:50 Winterzeit, 30 min geplant, jetzt 03:10 Sommerzeit
    # (real 20 min). Naiv 80 min > 30 + 15 -> "veraltet", Kanal frei.
    ("maerz", datetime(2027, 3, 28, 1, 50), "2027-03-28T01:10:00+00:00"),
    # Oktober: Lauf 02:10 Winterzeit (DB, fold verloren), jetzt 02:15
    # Winterzeit. Mit blossem `verstrichen_s` 65 min -> "veraltet".
    ("okt_zweiter_durchlauf", datetime(2026, 10, 25, 2, 10), "2026-10-25T01:15:00+00:00"),
])
def test_kanal_zustand_laufender_lauf_bleibt_ueber_die_umstellung_belegt(
    name, gestartet, jetzt_utc,
):
    from bewaesserung.kanal_zustand import kanal_vorgang_laeuft

    class _Speicher:
        async def hole_live_lauf_states(self, geraet_id=None):  # noqa: ARG002
            return [{
                "kanal": 1, "geraet_id": "dswc-1", "zone_ids": ["zone-1"],
                "dauer_sekunden": 1800, "gestartet_am": gestartet,
            }]

        async def hole_pre_soak_states(self):
            return []

    laeuft = asyncio.run(kanal_vorgang_laeuft(
        _Speicher(), zone_id="zone-1", kanal=1, geraet_id="dswc-1",
        jetzt=_lokal(jetzt_utc),
    ))
    assert laeuft is True, name


@pytest.mark.asyncio
@pytest.mark.parametrize("pfad", ["ohne_neue_ankunft", "ohne_jede_messung"])
async def test_frische_stop_zaehlt_die_echte_laufzeit_ueber_die_umstellung(pfad):
    """Maerz: Lauf ab 01:00 Winterzeit, jetzt 03:30 Sommerzeit = real 90 min,
    also innerhalb des 120-min-Fensters. Naiv 150 min -> Stop eines Laufs,
    der seine Augen noch gar nicht verloren haben kann. Beide Stellen, die
    `lauf_minuten` rechnen: Frische-Pruefung und Blackout-Zweig."""
    from test_entscheidung import (
        _kanal_zonen_mit_max_75, baue_kanal_motor, baue_messung,
    )

    jetzt = _lokal("2027-03-28T01:30:00+00:00")
    lauf_start = datetime(2027, 3, 28, 1, 0)
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    motor, speicher = baue_kanal_motor(zone_a, zone_b, 30.0, 40.0)
    motor._jetzt = lambda: jetzt
    speicher.messungen = {
        z.zone_id: [baue_messung(jetzt, f, z.zone_id)]
        for z, f in ((zone_a, 30.0), (zone_b, 40.0))
    } if pfad == "ohne_neue_ankunft" else {}
    speicher.ankuenfte = {
        z.zone_id: lauf_start - timedelta(minutes=5) for z in (zone_a, zone_b)
    }

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b], lauf_start=lauf_start,
    )

    assert soll_stoppen is False, begruendung
    assert speicher.geoeffnete_warnungen == []


@pytest.mark.asyncio
async def test_orphan_sync_close_bekommt_die_echte_dauer(tmp_path):
    """Maerz: OEFFNEN 01:40 Winterzeit, Reconnect-Sync 03:10 Sommerzeit =
    real 30 min. Naiv 90 min -> gedeckelt auf 3600 s, doppelt so viel Wasser
    in der Bilanz wie geflossen."""
    from bewaesserung.modelle import (
        VentilAktion, VentilEreignis, ZonenKonfig, ZonenModus,
    )
    from bewaesserung.orphan_close_job import OrphanCloseJob
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "orphan_dst.db"))
    await sp.verbinden()
    try:
        oeffnen = datetime(2027, 3, 28, 1, 40)
        await sp.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=oeffnen, zone_id="bambuswald",
            ventil_id="11111111-uuid:2", aktion=VentilAktion.OEFFNEN,
            ausloser=Ausloser.MANUELL,
        ))
        zone = ZonenKonfig(
            zone_id="bambuswald", name="bambuswald",
            modus=ZonenModus.MONITORING,
            feuchte_schwelle_min=30, feuchte_schwelle_max=70,
            max_dauer_sekunden=5400,
        )
        jetzt = _lokal("2027-03-28T01:10:00+00:00")
        n = await OrphanCloseJob(sp, [zone]).sync_aus_live_state(
            live_state={"bambuswald": "zu"}, jetzt=jetzt,
        )
        assert n == 1
        ereignisse = await sp.hole_ventil_ereignisse("bambuswald")
        dauern = [
            e.dauer_sekunden for e in ereignisse
            if e.aktion == VentilAktion.SCHLIESSEN
        ]
        assert dauern == [1800]
    finally:
        await sp.schliessen()


def test_ws_gap_sprung_im_maerz_armiert_keinen_replay_guard():
    """Maerz: letztes WS-Event 01:59:30 Winterzeit, naechstes 03:00:30
    Sommerzeit = real 60 s. Naiv 3660 s >= REPLAY_GAP_SEKUNDEN -> Guard
    armiert ohne Reconnect und unterdrueckt echte Ventil-Events."""
    from bewaesserung.gardena_client import GardenaClient

    client = GardenaClient(client_id="test", client_secret="test")
    client._pruefe_ws_gap_und_armiere(_lokal("2027-03-28T00:59:30+00:00"))
    client._pruefe_ws_gap_und_armiere(_lokal("2027-03-28T01:00:30+00:00"))

    assert client._replay_guard_bis is None
