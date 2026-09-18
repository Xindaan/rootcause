"""T-0447 / T-0446: der gemeinsame Kanal-Zustands-Check.

Der Kern von T-0447 ist nicht der Check selbst, sondern dass es ihn nur noch
EINMAL gibt. Deshalb prueft dieses Modul ausdruecklich, dass die
Pre-Soak-Dauerformel dieselbe ist wie die des `PreSoakLauf` -- eine zweite
Kopie der Formel waere der Rueckfall in genau den Zustand, den der Task
aufloest.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.kanal_zustand import (
    TOLERANZ,
    kanal_vorgang_laeuft,
    pre_soak_ende_s,
)
from bewaesserung.modelle import Ausloser
from bewaesserung.pre_soak import PreSoakLauf

JETZT = datetime(2026, 4, 19, 12, 0)


class _SpeicherStub:
    def __init__(self, live=None, pre_soak=None):
        self.live = live or []
        self.pre_soak = pre_soak or []

    async def hole_live_lauf_states(self, geraet_id=None):
        return list(self.live)

    async def hole_pre_soak_states(self):
        return list(self.pre_soak)


def _run(coro):
    return asyncio.run(coro)


def _live(*, vor_minuten, dauer_minuten, kanal=1, geraet_id="dswc-1"):
    return {
        "kanal": kanal, "valve_id": f"{geraet_id}:{kanal}",
        "geraet_id": geraet_id, "zone_ids": ["zone-1"],
        "dauer_sekunden": dauer_minuten * 60,
        "ausloser": "manuell",
        "gestartet_am": JETZT - timedelta(minutes=vor_minuten),
    }


def _soak(*, vor_minuten, pause_min=30, haupt_min=60, phase="pause",
          zone_ids=None, haupt_pulse=1, haupt_pause_min=0):
    return {
        "zone_id": "zone-1", "kanal": 1,
        "zone_ids_kanal": zone_ids if zone_ids is not None else ["zone-1"],
        "pre_soak_s": 300, "pause_s": pause_min * 60,
        "haupt_s": haupt_min * 60,
        "gestartet_am": JETZT - timedelta(minutes=vor_minuten),
        "phase": phase, "ausloser": "manuell",
        "haupt_pulse": haupt_pulse, "haupt_pause_s": haupt_pause_min * 60,
        "haupt_pulse_gestartet": 0,
    }


# ---------- Die Formel ist wirklich geteilt ----------

@pytest.mark.parametrize("pause_s,haupt_s,pulse,pause_zw", [
    (1800, 3600, 1, 0),          # klassisch: ein Hauptlauf am Stueck
    (1800, 5400, 3, 900),        # T-0437 Cycle-and-Soak: 3x30 min + 15 min
    (300, 60, 1, 0),             # Mini-Sequenz
    (1800, 100, 7, 60),          # Rest bei ganzzahliger Teilung
    (1800, 0, 1, 0),             # entartete Haupt-Dauer
])
def test_t0447_formel_identisch_zu_presoaklauf(pause_s, haupt_s, pulse, pause_zw):
    """Wenn jemand eine der beiden Seiten aendert, schlaegt das hier an.
    Das ist der eigentliche Regressionsschutz von T-0447: nicht dass die
    Formel stimmt, sondern dass es nur eine gibt.
    """
    lauf = PreSoakLauf(
        zone_id="z", kanal=1, zone_ids_kanal=["z"],
        pre_soak_s=300, pause_s=pause_s, haupt_s=haupt_s,
        gestartet_am=JETZT, ausloser=Ausloser.MANUELL,
        haupt_pulse=pulse, haupt_pause_s=pause_zw,
    )
    assert pre_soak_ende_s(pause_s, haupt_s, pulse, pause_zw) == lauf.ende_s


# ---------- Ventil offen ----------

def test_offenes_ventil_laeuft():
    sp = _SpeicherStub(live=[_live(vor_minuten=10, dauer_minuten=90)])
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is True


def test_anderer_kanal_laeuft_nicht():
    sp = _SpeicherStub(live=[_live(vor_minuten=10, dauer_minuten=90, kanal=2)])
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is False


def test_gleicher_kanal_anderes_geraet_laeuft_nicht():
    sp = _SpeicherStub(
        live=[_live(vor_minuten=10, dauer_minuten=90, geraet_id="dswc-2")],
    )
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is False


def test_zone_ohne_kanal_laeuft_nicht():
    sp = _SpeicherStub(live=[_live(vor_minuten=10, dauer_minuten=90)])
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=None, geraet_id="dswc-1", jetzt=JETZT,
    )) is False


# ---------- Frische-Schranke ----------

def test_veraltete_live_zeile_gilt_nicht_mehr():
    """Geister-Zeile (Realfall 02.07., 12 h). Ohne Schranke wuerde sie die
    Notpfade, die diesen Check konsultieren, dauerhaft stumm schalten."""
    sp = _SpeicherStub(live=[_live(vor_minuten=720, dauer_minuten=30)])
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is False


def test_knapp_innerhalb_der_toleranz_gilt_noch():
    """Gegenprobe: die Schranke ist eine Begrenzung, kein Loch. Kurz nach
    dem planmaessigen Ende (Cloud-Latenz, Loop-Tick) gilt die Zeile."""
    minuten = 30 + int(TOLERANZ.total_seconds() // 60) - 1
    sp = _SpeicherStub(live=[_live(vor_minuten=minuten, dauer_minuten=30)])
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is True


def test_zeile_ohne_startzeit_gilt_als_laufend():
    """Ohne verwertbaren Startzeitpunkt ist die Existenz der Zeile die
    einzige Aussage, die wir haben -- dann konservativ werten."""
    zeile = _live(vor_minuten=10, dauer_minuten=30)
    zeile["gestartet_am"] = None
    sp = _SpeicherStub(live=[zeile])
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is True


# ---------- Pre-Soak (T-0446) ----------

def test_soak_pause_laeuft_obwohl_ventil_zu():
    sp = _SpeicherStub(live=[], pre_soak=[_soak(vor_minuten=20)])
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is True


def test_soak_endphase_laeuft_nicht():
    sp = _SpeicherStub(pre_soak=[_soak(vor_minuten=20, phase="fertig")])
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is False


def test_soak_nach_sequenzende_laeuft_nicht():
    sp = _SpeicherStub(pre_soak=[_soak(vor_minuten=200)])
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is False


def test_soak_fremde_zone_laeuft_nicht():
    sp = _SpeicherStub(pre_soak=[_soak(vor_minuten=20, zone_ids=["zone-9"])])
    sp.pre_soak[0]["zone_id"] = "zone-9"
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is False


def test_soak_cycle_and_soak_laeuft_bis_zum_letzten_puls():
    """T-0437: 3 Pulse a 30 min mit 15 min Pause -> Ende bei
    30 + 2*(30+15) + 30 = 150 min. Bei 120 min laeuft die Sequenz noch,
    obwohl eine naive Rechnung (pause + haupt = 120) sie fuer beendet
    hielte."""
    zustand = _soak(
        vor_minuten=120, pause_min=30, haupt_min=90,
        haupt_pulse=3, haupt_pause_min=15,
    )
    sp = _SpeicherStub(pre_soak=[zustand])
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is True


# ---------- Robustheit ----------

def test_speicher_ohne_accessoren_faellt_auf_false():
    class _Alt:
        pass
    assert _run(kanal_vorgang_laeuft(
        _Alt(), zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is False


def test_db_fehler_faellt_auf_false():
    class _Kaputt:
        async def hole_live_lauf_states(self, geraet_id=None):
            raise RuntimeError("db weg")

        async def hole_pre_soak_states(self):
            raise RuntimeError("db weg")

    assert _run(kanal_vorgang_laeuft(
        _Kaputt(), zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is False


def test_aware_jetzt_sprengt_den_vergleich_nicht():
    """Die DB speichert Beispielstadt-naiv. Ein aware `jetzt` wuerde den Vergleich
    mit einem TypeError sprengen, der oben im `except` als "kein Vorgang"
    verschwaende -- fail-open an der Stelle, die schuetzen soll."""
    from datetime import timezone
    sp = _SpeicherStub(live=[_live(vor_minuten=10, dauer_minuten=90)])
    aware = JETZT.replace(tzinfo=timezone.utc)
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=aware,
    )) is True


# ---------- T-0537: Fremdlaeufe aus der Ereignis-Tabelle ----------
#
# `live_lauf_state` und `pre_soak_state` kennen nur Laeufe DIESER Engine.
# Der taegliche Magerwiese-Lauf kommt aus der Gardena-App und steht nur in
# `ventil_ereignis` -- fuer beide Konsumenten (Shadow-Push-Unterdrueckung,
# Sensor-Backfill) sah der Kanal damit frei aus, obwohl Wasser lief.


class _SpeicherMitEreignissen(_SpeicherStub):
    def __init__(self, offene=None, **kwargs):
        super().__init__(**kwargs)
        self.offene = offene or []
        self.abfragen: list[tuple] = []

    async def hole_offene_ventil_kanaele(self, seit, bis=None):
        self.abfragen.append((seit, bis))
        return list(self.offene)


def _offen(ventil_id="dswc-1:1", zonen=("magerwiese",), ausloser="ignoriert"):
    return {"ventil_id": ventil_id, "zone_ids": list(zonen),
            "ausloser": ausloser, "seit": None}


def test_t0537_fremdlauf_aus_ereignissen_zaehlt():
    """Der Realfall: App-Lauf, nichts im Engine-Zustand."""
    sp = _SpeicherMitEreignissen(offene=[_offen()])
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is True


def test_t0537_fremdlauf_auf_anderem_kanal_zaehlt_nicht():
    sp = _SpeicherMitEreignissen(offene=[_offen(ventil_id="dswc-1:2")])
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is False


def test_t0537_fremdlauf_auf_anderem_geraet_zaehlt_nicht():
    """Gleiche Kanalnummer, anderes Geraet -- das ist ein anderer Kanal."""
    sp = _SpeicherMitEreignissen(offene=[_offen(ventil_id="dswc-2:1")])
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is False


def test_t0537_ereignisse_ohne_kanalsuffix_zaehlen_nicht():
    """`sensor_heuristik` ist kein Ventil, das Wasser fuehrt."""
    sp = _SpeicherMitEreignissen(offene=[_offen(ventil_id="sensor_heuristik")])
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is False


def test_t0537_frische_schranke_wird_durchgereicht():
    """Ein verlorenes SCHLIESSEN darf den Kanal nicht dauerhaft belegen."""
    from bewaesserung.kanal_zustand import MAX_OFFEN
    sp = _SpeicherMitEreignissen()
    _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    ))
    assert sp.abfragen == [(JETZT - MAX_OFFEN, JETZT)]


def test_t0537_speicher_ohne_ereignis_sicht_bleibt_still():
    """Alte Attrappen ohne den Accessor: Bestandsverhalten, kein Crash."""
    sp = _SpeicherStub()
    assert _run(kanal_vorgang_laeuft(
        sp, zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is False


def test_t0537_db_fehler_bleibt_fail_open():
    """Ein Zustands-Check, der selbst ausfaellt, blockiert nicht die Anlage."""
    class _Kaputt(_SpeicherStub):
        async def hole_offene_ventil_kanaele(self, seit, bis=None):
            raise RuntimeError("database is locked")

    assert _run(kanal_vorgang_laeuft(
        _Kaputt(), zone_id="zone-1", kanal=1, geraet_id="dswc-1", jetzt=JETZT,
    )) is False
