"""T-0520: `lauf_gruppe`/`phase` am SCHLIESSEN aus dem OEFFNEN nachtragen.

Die Dauer eines Laufs sitzt laut Ventil-Event-Vertrag am SCHLIESSEN. Fehlt
dort die Gruppe, ist der Lauf nicht mehr zusammensetzbar und sieht in jeder
Auswertung aus wie "gestartet, nie beendet". Gemessen am 06.08.2026: 9
automatik- und 24 watchdog-Closes seit dem 01.07. ohne Gruppe, und daraus
entstand die falsche Behauptung "in 3 von 6 Laeufen kam die Hauptgabe nicht".

Der Nachtrag sitzt bewusst in `speichere_ventil_ereignis`, weil die
verlierenden Pfade (WS-Close ohne aktiven State, Watchdog auf geraeumtem
State, DHS-Backfill) nichts gemeinsam haben ausser dieser Stelle.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import VentilAktion, Ausloser, VentilEreignis
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "lauf_gruppe.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        # Ohne schliessen() haengt pytest (fehlerpattern_aiosqlite_fixture_haenger).
        _run(s.schliessen())


VENTIL = "geraet-1:2"
ZONE = "hecke"


def _ereignis(
    ts: datetime, aktion: VentilAktion, *,
    gruppe: str | None = None, phase: str | None = None,
    dauer: int = 0, zone: str = ZONE, ventil: str = VENTIL,
    ausloser: Ausloser = Ausloser.AUTOMATIK,
) -> VentilEreignis:
    return VentilEreignis(
        zeitstempel=ts, zone_id=zone, ventil_id=ventil, aktion=aktion,
        dauer_sekunden=dauer, ausloser=ausloser,
        lauf_gruppe=gruppe, phase=phase,
    )


def _gelesen(sp: Speicher, aktion: str = "schliessen"):
    async def _lies():
        async with sp._db.execute(
            "SELECT zeitstempel, lauf_gruppe, phase FROM ventil_ereignis "
            "WHERE aktion = ? ORDER BY zeitstempel",
            (aktion,),
        ) as cur:
            return await cur.fetchall()
    return _run(_lies())


def test_close_ohne_gruppe_erbt_sie_vom_offenen_lauf(speicher):
    """Der Regelfall: Watchdog-Close auf geraeumtem State."""
    start = datetime(2026, 8, 11, 13, 4, 10)
    _run(speicher.speichere_ventil_ereignis(
        _ereignis(start, VentilAktion.OEFFNEN,
                  gruppe="presoak_hecke_20260811130410", phase="haupt")))
    # So schreibt der Watchdog heute: er kennt die Gruppe nicht mehr.
    _run(speicher.speichere_ventil_ereignis(
        _ereignis(start + timedelta(minutes=44), VentilAktion.SCHLIESSEN,
                  dauer=2640, ausloser=Ausloser.WATCHDOG)))

    (zeile,) = _gelesen(speicher)
    assert zeile[1] == "presoak_hecke_20260811130410"
    assert zeile[2] == "haupt"


def test_bereits_gepaartes_oeffnen_zieht_keinen_zweiten_close_an(speicher):
    """Die Bedingung, ohne die der Fix schlimmer waere als das Problem.

    Ein verwaistes OEFFNEN (verlorenes SCHLIESSEN) wuerde sonst jeden
    spaeteren Close an sich ziehen und die Fehlpaarung ueber die ganze
    Historie weiterschieben.
    """
    start = datetime(2026, 8, 11, 13, 0, 0)
    _run(speicher.speichere_ventil_ereignis(
        _ereignis(start, VentilAktion.OEFFNEN, gruppe="lauf_a", phase="haupt")))
    _run(speicher.speichere_ventil_ereignis(
        _ereignis(start + timedelta(minutes=20), VentilAktion.SCHLIESSEN,
                  dauer=1200, gruppe="lauf_a", phase="haupt")))
    # Zweiter, unabhaengiger Close ohne eigenes OEFFNEN (etwa ein Nachtrag
    # aus dem DHS-Backfill fuer einen App-Lauf).
    _run(speicher.speichere_ventil_ereignis(
        _ereignis(start + timedelta(minutes=90), VentilAktion.SCHLIESSEN,
                  dauer=600, ausloser=Ausloser.ZEITPLAN)))

    zeilen = _gelesen(speicher)
    assert len(zeilen) == 2
    assert zeilen[0][1] == "lauf_a"
    assert zeilen[1][1] is None, "zweiter Close darf lauf_a nicht erben"


def test_alter_lauf_ausserhalb_des_fensters_wird_nicht_zugeordnet(speicher):
    """6 h Fenster: darueber ist die Zuordnung geraten, nicht belegt."""
    start = datetime(2026, 8, 11, 4, 0, 0)
    _run(speicher.speichere_ventil_ereignis(
        _ereignis(start, VentilAktion.OEFFNEN, gruppe="lauf_alt", phase="haupt")))
    _run(speicher.speichere_ventil_ereignis(
        _ereignis(start + timedelta(hours=7), VentilAktion.SCHLIESSEN,
                  dauer=900, ausloser=Ausloser.WATCHDOG)))

    (zeile,) = _gelesen(speicher)
    assert zeile[1] is None


def test_fremde_zone_am_selben_ventil_erbt_nicht(speicher):
    """Dual-Channel: zwei Zonen teilen ein Geraet, nicht aber den Lauf."""
    start = datetime(2026, 8, 11, 8, 0, 0)
    _run(speicher.speichere_ventil_ereignis(
        _ereignis(start, VentilAktion.OEFFNEN, gruppe="lauf_bambus",
                  phase="haupt", zone="bambuswald")))
    _run(speicher.speichere_ventil_ereignis(
        _ereignis(start + timedelta(minutes=25), VentilAktion.SCHLIESSEN,
                  dauer=1500, zone="bambuswald_yogaraum")))

    zeilen = _gelesen(speicher)
    assert zeilen[0][1] is None


def test_mitgegebene_gruppe_wird_nicht_ueberschrieben(speicher):
    """Der Live-Pfad kennt die Gruppe -- der Nachschlag darf nicht dazwischen."""
    start = datetime(2026, 8, 11, 9, 0, 0)
    _run(speicher.speichere_ventil_ereignis(
        _ereignis(start, VentilAktion.OEFFNEN, gruppe="lauf_x", phase="pre_soak")))
    _run(speicher.speichere_ventil_ereignis(
        _ereignis(start + timedelta(minutes=5), VentilAktion.SCHLIESSEN,
                  dauer=300, gruppe="lauf_x", phase="pre_soak")))

    (zeile,) = _gelesen(speicher)
    assert zeile[1] == "lauf_x"
    assert zeile[2] == "pre_soak"


def test_oeffnen_ohne_gruppe_bleibt_unberuehrt(speicher):
    """Einzellaeufe (zeitplan, aquabloom, ignoriert) haben bewusst keine Gruppe."""
    start = datetime(2026, 8, 11, 10, 0, 0)
    _run(speicher.speichere_ventil_ereignis(
        _ereignis(start, VentilAktion.OEFFNEN,
                  ausloser=Ausloser.ZEITPLAN)))
    _run(speicher.speichere_ventil_ereignis(
        _ereignis(start + timedelta(minutes=10), VentilAktion.SCHLIESSEN,
                  dauer=600, ausloser=Ausloser.ZEITPLAN)))

    (zeile,) = _gelesen(speicher)
    assert zeile[1] is None
    assert zeile[2] is None
