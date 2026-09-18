"""T-0514: die ML-Bulk-Leser materialisieren blockweise statt in einem Zug.

Hintergrund (gemessen per Stack-Sample am laufenden Retrain): waehrend der
Datenextraktion wartete der Main-Thread zu 25-36 % auf den GIL, beim
LightGBM-Fit dagegen nur zu 0,1 %. Der Engpass ist das Lesen, nicht das
Rechnen: `fetchall()` laesst den aiosqlite-Worker 228.000 Zeilen am Stueck
materialisieren, und solange das laeuft, kommt der Entscheidungsloop nicht
dran.

Zwei Eigenschaften sind zu sichern, und die erste ist die wichtigere:

1. **Semantik unveraendert.** Gleiche Zeilen, gleiche Reihenfolge, gleiche
   Werte wie vorher. Ein Bulk-Leser, der beim Stueckeln Zeilen verliert,
   waere ein stiller Datenfehler im Trainingssatz.
2. **Es wird wirklich gestueckelt.** Sonst ist die Aenderung Kosmetik und
   der Loop haengt weiter.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung import speicher as speicher_modul
from bewaesserung.modelle import DatenQuelle, SensorMessung, VentilEreignis
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "bulk_bloecke.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


T0 = datetime(2026, 8, 1, 6, 0, 0)


def _fuelle_messungen(sp, n: int) -> None:
    for i in range(n):
        _run(sp.speichere_messung(SensorMessung(
            zeitstempel=T0 + timedelta(minutes=i),
            zone_id="bambuswald",
            geraet_id="11111111",
            boden_feuchte=float(40 + i),
            boden_temperatur=18.0,
            batterie_prozent=90.0,
            quelle=DatenQuelle.GARDENA,
        )))


def test_t0514_alle_zeilen_kommen_an_und_zwar_in_reihenfolge(speicher):
    """Semantik-Gegenprobe: nichts geht beim Stueckeln verloren.

    Sieben Zeilen bei Blockgroesse 2 heisst vier Bloecke, davon einer
    unvollstaendig -- genau der Fall, in dem eine Abbruchbedingung
    typischerweise eine Zeile verschluckt oder eine doppelt nimmt.
    """
    _fuelle_messungen(speicher, 7)
    speicher_modul.BULK_BLOCK_ZEILEN = 2
    try:
        messungen = _run(speicher.hole_alle_messungen(
            T0 - timedelta(hours=1), T0 + timedelta(hours=1)
        ))
    finally:
        speicher_modul.BULK_BLOCK_ZEILEN = 2000

    assert len(messungen) == 7, "Zeilen verloren oder doppelt"
    assert [m.boden_feuchte for m in messungen] == [
        40.0, 41.0, 42.0, 43.0, 44.0, 45.0, 46.0
    ], "Reihenfolge oder Werte verfaelscht"
    assert all(m.zone_id == "bambuswald" for m in messungen)
    assert messungen[0].quelle is DatenQuelle.GARDENA


def test_t0514_blockgroesse_aendert_das_ergebnis_nicht(speicher):
    """Dasselbe Ergebnis bei Blockgroesse 1, 3 und Default.

    Wenn die Blockgroesse das Ergebnis beeinflusst, ist die Schleife falsch.
    """
    _fuelle_messungen(speicher, 5)
    fenster = (T0 - timedelta(hours=1), T0 + timedelta(hours=1))

    ergebnisse = []
    for block in (1, 3, 2000):
        speicher_modul.BULK_BLOCK_ZEILEN = block
        try:
            ergebnisse.append(
                [m.boden_feuchte for m in _run(speicher.hole_alle_messungen(*fenster))]
            )
        finally:
            speicher_modul.BULK_BLOCK_ZEILEN = 2000

    assert ergebnisse[0] == ergebnisse[1] == ergebnisse[2]
    assert len(ergebnisse[0]) == 5


def test_t0514_es_wird_wirklich_gestueckelt(speicher, monkeypatch):
    """Gegenprobe zur Kosmetik: `fetchmany` wird mehrfach gerufen.

    Ohne diesen Test koennte jemand auf `fetchall()` zurueckbauen und die
    Tests oben blieben gruen -- die Semantik ist ja dieselbe. Genau das
    waere aber der Rueckfall in die gemessene Loop-Blockade.
    """
    _fuelle_messungen(speicher, 7)

    aufrufe = {"n": 0}
    echtes_lies = speicher_modul._lies_in_bloecken

    async def zaehlend(cursor, baue_zeile):
        echtes_fetchmany = cursor.fetchmany

        async def gezaehlt(groesse):
            aufrufe["n"] += 1
            return await echtes_fetchmany(groesse)

        cursor.fetchmany = gezaehlt
        return await echtes_lies(cursor, baue_zeile)

    monkeypatch.setattr(speicher_modul, "_lies_in_bloecken", zaehlend)
    speicher_modul.BULK_BLOCK_ZEILEN = 2
    try:
        messungen = _run(speicher.hole_alle_messungen(
            T0 - timedelta(hours=1), T0 + timedelta(hours=1)
        ))
    finally:
        speicher_modul.BULK_BLOCK_ZEILEN = 2000

    assert len(messungen) == 7
    # 7 Zeilen a 2 = 4 volle Abrufe, plus einer, der leer zurueckkommt.
    assert aufrufe["n"] == 5, f"nicht gestueckelt (fetchmany {aufrufe['n']}x)"


def test_t0514_alle_drei_bulk_leser_nutzen_den_helfer():
    """Isomorphie-Guard: die Fehlerklasse ist der Bulk-Leser, nicht eine Zeile.

    Alle drei ML-Bulk-Leser hatten dieselbe Form (`fetchall()` plus
    Objektbau im Coroutine-Rumpf). Bliebe einer zurueck, waere die Blockade
    nur verschoben -- und beim naechsten Datenwachstum wieder da.
    """
    import inspect

    for name in (
        "hole_alle_messungen",
        "hole_alle_ventil_ereignisse",
        "hole_wetter_vorhersagen",
    ):
        quelle = inspect.getsource(getattr(Speicher, name))
        assert "_lies_in_bloecken(" in quelle, f"{name} liest wieder am Stueck"
        assert "fetchall()" not in quelle, f"{name} nutzt wieder fetchall()"


def test_t0514_ventil_ereignisse_bleiben_vollstaendig(speicher):
    """Der Ventil-Leser sitzt auf dem Sicherheitspfad, deshalb eigene Probe."""
    for i in range(5):
        _run(speicher.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=T0 + timedelta(minutes=i),
            zone_id="hecke",
            ventil_id="v1",
            aktion="oeffnen" if i % 2 == 0 else "schliessen",
            dauer_sekunden=i * 60,
            ausloser="automatik",
        )))

    speicher_modul.BULK_BLOCK_ZEILEN = 2
    try:
        ereignisse = _run(speicher.hole_alle_ventil_ereignisse(
            T0 - timedelta(hours=1), T0 + timedelta(hours=1)
        ))
    finally:
        speicher_modul.BULK_BLOCK_ZEILEN = 2000

    assert len(ereignisse) == 5
    assert [e.dauer_sekunden for e in ereignisse] == [0, 60, 120, 180, 240]
