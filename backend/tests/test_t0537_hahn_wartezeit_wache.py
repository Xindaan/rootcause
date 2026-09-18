"""T-0537 Punkt 3: Wartezeit-Wache am geteilten Haupthahn.

Andres Regel (13.08.2026): waldblumenhain, hecke und bambuswald tragen die
teuren Pflanzen und duerfen nicht einen ganzen Tag hinter einer
Dauer-Rasenberegnung stehen; ein Verzug von rund zwei Stunden ist in Ordnung,
ab 6 h will er es wissen. `magerwiese` ist der flexible Rasensprinkler und
laeuft ungestoert fertig.

Die Datei testet BEIDE Haelften der Naht und die Naht selbst:

1. `HahnArbiter` fuehrt den Wartezustand (wer wartet seit wann, wer blockiert)
2. `WatchdogJob` liest ihn und meldet ueber den bestehenden Push-Pfad
3. der Durchstich mit echtem Arbiter UND echtem Watchdog -- ohne den waere
   ein gruener Zustand-Test wertlos
   ([[fehlerpattern_detektor_ohne_konsument]], sechs Faelle im Projekt).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.hahn_arbiter import (
    WARTEN_FRISCHE,
    HahnArbiter,
    Kanalprofil,
)
from bewaesserung.modelle import (
    HahnWacheKonfig,
    WatchdogKonfig,
    ZeitFenster,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.speicher import Speicher
from bewaesserung.watchdog import TYP_HAHN_WARTEZEIT, WatchdogJob

DSWC1 = "bbbb0001-0000-4000-8000-000000000001"
DSWC2 = "bbbb0002-0000-4000-8000-000000000002"
CLUSTER = "garten-haupthahn"

# Reale Topologie (config/default.yaml, Stand 13.08.2026).
PROFILE = {
    (DSWC1, 1): Kanalprofil(("waldblumenhain",), CLUSTER, 6.0, True),
    (DSWC1, 2): Kanalprofil(
        ("bambuswald", "bambuswald_yogaraum"), CLUSTER, 1.87, False,
    ),
    (DSWC2, 1): Kanalprofil(("magerwiese",), CLUSTER, 6.0, True),
    (DSWC2, 2): Kanalprofil(("hecke",), CLUSTER, 1.4, False),
}
BUDGET = {CLUSTER: 10.0}

T0 = datetime(2026, 8, 12, 8, 0, 0)


def _run(coro):
    return asyncio.run(coro)


class _SpeicherOhneEreignisse:
    async def hole_pre_soak_states(self) -> list[dict]:
        return []


class _SicherungAttrappe:
    def __init__(self, aktiv: dict[int, tuple[str, ...]] | None = None):
        self._aktiv = dict(aktiv or {})

    def aktive_kanaele(self) -> dict[int, tuple[str, ...]]:
        return dict(self._aktiv)


def _arbiter(rasen_laeuft: bool = True) -> HahnArbiter:
    """Arbiter in der Lage, die den Vorfall erzeugt: magerwiese laeuft."""
    a = HahnArbiter(
        speicher=_SpeicherOhneEreignisse(),
        kanalprofile=PROFILE,
        cluster_max_lpm=BUDGET,
    )
    a.registriere_sicherung(DSWC1, _SicherungAttrappe())
    a.registriere_sicherung(
        DSWC2, _SicherungAttrappe({1: ("magerwiese",)} if rasen_laeuft else {}),
    )
    return a


# --- Haelfte 1: der Zustand im Arbiter ------------------------------------


def test_ablehnung_startet_die_wartestrecke():
    arbiter = _arbiter()
    entscheidung = _run(arbiter.pruefe(DSWC1, 1, jetzt=T0))
    assert entscheidung.erlaubt is False

    zustand = arbiter.wartezustand("waldblumenhain", jetzt=T0)
    assert zustand is not None
    assert zustand.seit == T0
    # Die Meldung soll sagen koennen, WER blockiert.
    assert "magerwiese" in zustand.aktive_zonen


def test_weitere_ablehnungen_verschieben_den_beginn_nicht():
    """Sonst waere die gemessene Wartezeit immer 0 -- der Loop fragt ja neu."""
    arbiter = _arbiter()
    for minuten in (0, 5, 10, 15):
        _run(arbiter.pruefe(DSWC1, 1, jetzt=T0 + timedelta(minutes=minuten)))

    zustand = arbiter.wartezustand("waldblumenhain", jetzt=T0 + timedelta(minutes=15))
    assert zustand is not None
    assert zustand.seit == T0
    assert zustand.zuletzt == T0 + timedelta(minutes=15)


def test_erlaubnis_loescht_die_wartestrecke():
    """Der Kanal laeuft jetzt -- er wartet nicht mehr."""
    arbiter = _arbiter()
    _run(arbiter.pruefe(DSWC1, 1, jetzt=T0))
    assert arbiter.wartezustand("waldblumenhain", jetzt=T0) is not None

    frei = _arbiter(rasen_laeuft=False)
    frei._wartend = arbiter._wartend  # gleiche Wartestrecke, Rasen ist aus
    entscheidung = _run(frei.pruefe(DSWC1, 1, jetzt=T0 + timedelta(minutes=5)))
    assert entscheidung.erlaubt is True
    assert frei.wartezustand("waldblumenhain", jetzt=T0 + timedelta(minutes=5)) is None


def test_ohne_neue_anfrage_laeuft_die_wartezeit_nicht_weiter():
    """Kein Bedarf mehr = kein Warten.

    Der Loop fragt alle 5 min neu an, solange die Zone giessen will. Hoert er
    auf, ist der Bedarf weg (Regen, Sensor erholt). Ohne diese Schranke wuerde
    eine einzelne Ablehnung um 08:00 die Wache um 14:00 ausloesen, obwohl seit
    08:05 niemand mehr starten wollte.
    """
    arbiter = _arbiter()
    _run(arbiter.pruefe(DSWC1, 1, jetzt=T0))

    kurz_danach = T0 + WARTEN_FRISCHE - timedelta(minutes=1)
    assert arbiter.wartezustand("waldblumenhain", jetzt=kurz_danach) is not None

    zu_alt = T0 + WARTEN_FRISCHE + timedelta(minutes=1)
    assert arbiter.wartezustand("waldblumenhain", jetzt=zu_alt) is None


def test_ablehnung_nach_langer_stille_beginnt_neu():
    """Sonst meldete die Wache "wartet seit heute frueh" nach dem ersten
    Blocker des Tages -- die Pause dazwischen war aber kein Warten."""
    arbiter = _arbiter()
    _run(arbiter.pruefe(DSWC1, 1, jetzt=T0))
    spaet = T0 + timedelta(hours=5)
    _run(arbiter.pruefe(DSWC1, 1, jetzt=spaet))

    zustand = arbiter.wartezustand("waldblumenhain", jetzt=spaet)
    assert zustand is not None
    assert zustand.seit == spaet


def test_zone_ohne_eigenen_kanal_wartet_nie():
    arbiter = _arbiter()
    _run(arbiter.pruefe(DSWC1, 1, jetzt=T0))
    assert arbiter.wartezustand("zitrus", jetzt=T0) is None


def test_geschwister_zone_teilt_die_wartestrecke_des_kanals():
    """bambuswald + bambuswald_yogaraum haengen an einem Ventil."""
    arbiter = _arbiter()
    _run(arbiter.pruefe(DSWC1, 2, jetzt=T0))
    assert arbiter.wartezustand("bambuswald", jetzt=T0) is not None
    assert arbiter.wartezustand("bambuswald_yogaraum", jetzt=T0) is not None


# --- Haelfte 2: der Konsument im Watchdog ---------------------------------


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "hahn_wache.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


class _BenachrichtigerStub:
    def __init__(self, *, erfolg: bool = True):
        self.gesendet: list[tuple[str, str]] = []
        self._erfolg = erfolg

    async def sende_text(self, empfaenger: str, text: str) -> bool:
        self.gesendet.append((empfaenger, text))
        return self._erfolg


class _ArbiterStub:
    """Nur der Leser-Vertrag, den der Watchdog braucht."""

    def __init__(self, zustaende: dict):
        self._zustaende = dict(zustaende)
        self.abfragen: list[tuple[str, datetime]] = []

    def wartezustand(self, zone_id: str, jetzt=None, frische=None):
        self.abfragen.append((zone_id, jetzt))
        return self._zustaende.get(zone_id)


class _Zustand:
    def __init__(self, seit: datetime, blocker: tuple[str, ...] = ("magerwiese",)):
        self.seit = seit
        self.zuletzt = seit
        self.grund = "Rasen laeuft"
        self.aktive_zonen = blocker


def _zone(zone_id: str, name: str) -> ZonenKonfig:
    return ZonenKonfig(
        zone_id=zone_id,
        name=name,
        modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=35.0,
        feuchte_kritisch=20.0,
        max_dauer_sekunden=1800,
        min_pause_minuten=120,
        tages_budget_sekunden=3600.0,
        bevorzugte_zeiten=[ZeitFenster(von="05:00", bis="07:00")],
    )


def _watchdog(speicher, arbiter, *, benachrichtiger=None, wache=None) -> WatchdogJob:
    return WatchdogJob(
        speicher=speicher,
        benachrichtiger=benachrichtiger or _BenachrichtigerStub(),
        konfig=WatchdogKonfig(
            aktiv=True,
            empfaenger="+490000",
            intervall_minuten=1,
            throttle_stunden=24,
            hahn_wache=wache or HahnWacheKonfig(
                aktiv=True, schwelle_stunden=6,
                zonen=["waldblumenhain", "hecke", "bambuswald"],
            ),
        ),
        zonen=[
            _zone("waldblumenhain", "Waldblumenhain"),
            _zone("hecke", "Hecke"),
            _zone("bambuswald", "Bambuswald"),
            _zone("magerwiese", "Magerwiese"),
        ],
        gardena_zone_ids=[],
        hahn_arbiter=arbiter,
    )


def test_watchdog_meldet_erst_ab_der_schwelle(speicher):
    jetzt = T0 + timedelta(hours=6)
    arbiter = _ArbiterStub(
        {"waldblumenhain": _Zustand(jetzt - timedelta(hours=5, minutes=55))},
    )
    push = _BenachrichtigerStub()
    job = _watchdog(speicher, arbiter, benachrichtiger=push)

    assert _run(job._pruefe_hahn_wartezeit(jetzt)) == 0
    assert push.gesendet == []


def test_watchdog_meldet_ueber_der_schwelle_mit_blocker(speicher):
    jetzt = T0 + timedelta(hours=7)
    arbiter = _ArbiterStub(
        {"waldblumenhain": _Zustand(jetzt - timedelta(hours=6, minutes=30))},
    )
    push = _BenachrichtigerStub()
    job = _watchdog(speicher, arbiter, benachrichtiger=push)

    assert _run(job._pruefe_hahn_wartezeit(jetzt)) == 1
    (empfaenger, text), = push.gesendet
    assert empfaenger == "+490000"
    assert "Waldblumenhain" in text
    assert "6.5 h" in text
    assert "magerwiese" in text


def test_watchdog_throttelt_pro_zone(speicher):
    jetzt = T0 + timedelta(hours=7)
    arbiter = _ArbiterStub({"hecke": _Zustand(jetzt - timedelta(hours=8))})
    push = _BenachrichtigerStub()
    job = _watchdog(speicher, arbiter, benachrichtiger=push)

    assert _run(job._pruefe_hahn_wartezeit(jetzt)) == 1
    assert _run(job._pruefe_hahn_wartezeit(jetzt + timedelta(hours=2))) == 0
    assert len(push.gesendet) == 1
    # Nach Ablauf des Throttles wieder.
    assert _run(job._pruefe_hahn_wartezeit(jetzt + timedelta(hours=25))) == 1


def test_watchdog_meldet_nur_die_konfigurierten_zonen(speicher):
    """magerwiese darf warten -- sie ist der flexible Rasensprinkler."""
    jetzt = T0 + timedelta(hours=12)
    arbiter = _ArbiterStub({"magerwiese": _Zustand(jetzt - timedelta(hours=10))})
    push = _BenachrichtigerStub()
    job = _watchdog(speicher, arbiter, benachrichtiger=push)

    assert _run(job._pruefe_hahn_wartezeit(jetzt)) == 0
    assert push.gesendet == []


def test_watchdog_wache_inaktiv_ist_no_op(speicher):
    jetzt = T0 + timedelta(hours=12)
    arbiter = _ArbiterStub({"hecke": _Zustand(jetzt - timedelta(hours=10))})
    push = _BenachrichtigerStub()
    job = _watchdog(
        speicher, arbiter, benachrichtiger=push,
        wache=HahnWacheKonfig(aktiv=False, zonen=["hecke"]),
    )

    assert _run(job._pruefe_hahn_wartezeit(jetzt)) == 0
    assert arbiter.abfragen == []


def test_watchdog_ohne_arbiter_ist_no_op(speicher):
    """Setup ohne Ventilgeraete: still, nicht kaputt."""
    job = _watchdog(speicher, arbiter=None)
    assert _run(job._pruefe_hahn_wartezeit(T0 + timedelta(hours=12))) == 0


def test_watchdog_tick_ruft_trigger_h_mit(speicher):
    """Der Trigger muss im Tick verdrahtet sein, nicht nur existieren."""
    jetzt = T0 + timedelta(hours=7)
    arbiter = _ArbiterStub({"hecke": _Zustand(jetzt - timedelta(hours=8))})
    push = _BenachrichtigerStub()
    job = _watchdog(speicher, arbiter, benachrichtiger=push)

    ergebnis = _run(job.pruefe_und_sende_wenn_faellig(jetzt))
    assert ergebnis["gesendet"] >= 1
    assert any("Haupthahn" in text for _, text in push.gesendet)


# --- Die Naht: echter Arbiter, echter Watchdog ----------------------------


def test_durchstich_rasentag_erzeugt_push(speicher):
    """Der Fall vom 12.08.: Rasen belegt den Hahn, waldblumenhain will giessen.

    Kein Zustands-Stub -- die Wartezeit entsteht hier ausschliesslich aus
    echten Arbiter-Ablehnungen, so wie sie der Auto-Loop alle 5 min erzeugt.
    """
    arbiter = _arbiter()
    push = _BenachrichtigerStub()
    job = _watchdog(speicher, arbiter, benachrichtiger=push)

    jetzt = T0
    gesendet = 0
    while jetzt <= T0 + timedelta(hours=7):
        entscheidung = _run(arbiter.pruefe(DSWC1, 1, jetzt=jetzt))
        assert entscheidung.erlaubt is False
        gesendet += _run(job._pruefe_hahn_wartezeit(jetzt))
        jetzt += timedelta(minutes=5)

    assert gesendet == 1, "genau einmal melden, nicht bei jedem Loop-Tick"
    (_, text), = push.gesendet
    assert "Waldblumenhain" in text
    assert "magerwiese" in text
    # Gemeldet wird kurz nach 6 h, nicht nach 7.
    zuletzt = _run(
        speicher.hole_letzten_watchdog_push(TYP_HAHN_WARTEZEIT, "waldblumenhain"),
    )
    assert timedelta(hours=6) <= (zuletzt - T0) < timedelta(hours=6, minutes=10)


def test_durchstich_morgenfenster_meldet_nicht(speicher):
    """Kontrollbedingung: an den echten Rasentagen (08.08., 12.08.) lag vor
    dem Rasenbeginn ein freier Block von 181 bzw. 200 min. Dort kommt die
    Zone dran -- und dann darf die Wache nicht mehr ausloesen."""
    arbiter = _arbiter()
    push = _BenachrichtigerStub()
    job = _watchdog(speicher, arbiter, benachrichtiger=push)

    # 04:00-08:00 frei: der Lauf startet, danach blockiert der Rasen den Tag.
    frei = _arbiter(rasen_laeuft=False)
    frei._wartend = arbiter._wartend
    assert _run(frei.pruefe(DSWC1, 1, jetzt=T0 - timedelta(hours=4))).erlaubt is True

    jetzt = T0
    gesendet = 0
    while jetzt <= T0 + timedelta(hours=5):
        _run(arbiter.pruefe(DSWC1, 1, jetzt=jetzt))
        gesendet += _run(job._pruefe_hahn_wartezeit(jetzt))
        jetzt += timedelta(minutes=5)

    assert gesendet == 0
    assert push.gesendet == []
