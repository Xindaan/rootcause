"""T-0598 (Gesamtreview EL-5): Fenster-Vorrang nur bei JETZT offenem Fenster.

T-0552 laesst eine Zone mit Zeitfenster in die Soak-Pause einer wartenden
Mikrodrip-Sequenz, weil "ihr Fenster heute verfaellt". Das Merkmal war aber
statisch aus `bevorzugte_zeiten` abgeleitet. Realfall 22.09.2026: waldblumenhain
startete per Kritisch-Bypass um 21:13, ausserhalb seiner Klammer 04-17, und
verdraengte bambuswald/yogaraum; deren Hauptdose kam 2 h 38 min spaeter.

Ausserhalb des Fensters verfaellt nichts, also gibt es keinen Vorrang: die
wartende Sequenz behaelt den Hahn.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import bewaesserung.hahn_arbiter as mod
from bewaesserung.hahn_arbiter import HahnArbiter, baue_kanalprofile
from bewaesserung.modelle import ZeitFenster

GERAET = "dswc1"
K1 = (GERAET, 1)   # waldblumenhain: Sprinkler mit Fenster
K2 = (GERAET, 2)   # bambuswald: Mikrodrip, kein Fenster


def _zf(text: str) -> ZeitFenster:
    von, bis = text.split("-")
    return ZeitFenster(von=von, bis=bis)


class _Zone:
    def __init__(self, zone_id, kanal, exklusiv, fenster, klammer=None):
        self.zone_id = zone_id
        self.ventil_kanal = kanal
        self.ventil_geraet_id = GERAET
        self.exklusiv = exklusiv
        self.bevorzugte_zeiten = [_zf(f) for f in fenster]
        self.hahn_cluster = "haupthahn"
        self.verbrauch_lpm = 10.0
        self.giessfenster_et0 = (
            None if klammer is None
            else SimpleNamespace(modus="aktiv", klammer=[_zf(f) for f in klammer])
        )


def _zonen(klammer):
    # Wie die echte Konfig: alte Fenster 04-08 und 18-21, im Aktivmodus
    # ersetzt durch die Klammer.
    return [
        _Zone("waldblumenhain", 1, True, ["04:00-08:00", "18:00-21:00"],
              klammer=klammer),
        _Zone("bambuswald", 2, False, []),
    ]


async def _aktive_bei(zonen, jetzt):
    profile = baue_kanalprofile(zonen, geraet_id=GERAET)
    arb = object.__new__(HahnArbiter)
    arb._profile = profile
    arb._sicherungen = {}
    arb._speicher = SimpleNamespace(
        hole_offene_ventil_kanaele=lambda **_: _leer(),
    )
    arb._zone_zu_kanal = {z.zone_id: (GERAET, z.ventil_kanal) for z in zonen}
    arb._cluster_max_lpm = {"haupthahn": 100.0}
    arb._max_offen = timedelta(hours=3)
    arb._wartend = {}
    arb._verdraengt = {}
    arb._kritisch_pruefer = None  # T-0618: ohne Urteil wie bisher

    async def _pre_soak(speicher, jetzt=None):
        return {"bambuswald"}

    original = mod.laufende_pre_soak_zonen
    mod.laufende_pre_soak_zonen = _pre_soak
    try:
        aktive, _ = await arb._aktive_kanaele(
            jetzt, ausser=K1, frager=profile[K1],
        )
    finally:
        mod.laufende_pre_soak_zonen = original
    return {tuple(a.zone_ids) for a in aktive}


async def _leer():
    return []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("klammer", "jetzt"),
    [
        # Realfall 22.09.: Kritisch-Bypass um 21:13, Klammer 04-17.
        (["04:00-17:00"], datetime(2026, 9, 22, 21, 13)),
        # Klammer ERSETZT die alten Fenster: um 19:00 waere 18-21 offen, die
        # wirksame Klammer 04-17 ist es nicht.
        (["04:00-17:00"], datetime(2026, 9, 22, 19, 0)),
        # Aktivmodus mit leerer Klammer: kein wirksames Fenster, also verfaellt
        # nichts (Falle: in_zeitfenstern([]) heisst "immer erlaubt").
        ([], datetime(2026, 9, 22, 10, 0)),
    ],
    ids=["realfall_2113", "klammer_ersetzt_altes_fenster", "leere_klammer"],
)
async def test_ausserhalb_des_fensters_behaelt_wartende_sequenz_den_hahn(
    klammer, jetzt,
):
    aktive = await _aktive_bei(_zonen(klammer), jetzt)
    assert aktive == {("bambuswald",)}, (
        "Frager ausserhalb seines wirksamen Fensters darf die wartende "
        "Sequenz nicht verdraengen"
    )


@pytest.mark.asyncio
async def test_innerhalb_der_klammer_gilt_der_vorrang_weiter():
    """Gegenstueck: in der Klammer verfaellt das Fenster, T-0552 gilt."""
    aktive = await _aktive_bei(
        _zonen(["04:00-17:00"]), datetime(2026, 9, 22, 16, 30),
    )
    assert aktive == set()
