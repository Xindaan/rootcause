"""T-0494: Orphan-Paarung auf `zone_id + ventil_id`, Positivfilter, ein Statement.

Drei Befunde an einer Abfrage (`speicher.hole_orphan_oeffnen`):

1. Sie paarte nur nach Zone. Ein SCHLIESSEN von Kanal 1 konnte damit ein
   OEFFNEN von Kanal 2 derselben Zone "paaren" -- der echte Orphan blieb
   unsichtbar und das Ventil ohne synthetisches Close.
2. Der Vorfilter war eine Negativliste und liess `aquabloom_solar`,
   `gardena_web` und `backfill_app` in den Kandidatenraum, obwohl alle
   drei einen eigenen Abschluss-Vertrag haben.
3. Sie war ein N+1 ueber die wachsende Gesamthistorie.

Diese Tests decken 1 und 2 ab, plus die Zeitformat-Falle aus 3.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import Ausloser, VentilAktion, VentilEreignis
from bewaesserung.speicher import Speicher

KANAL_1 = "77777777-15a9-43ff-bbb9-f9ae51784897:1"
KANAL_2 = "77777777-15a9-43ff-bbb9-f9ae51784897:2"
T0 = datetime(2026, 8, 1, 10, 0)
SUCHFENSTER = timedelta(hours=2)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "orphan_t0494.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _schreibe(speicher, aktion, ventil_id, zeitstempel, zone="bambuswald",
              ausloser=Ausloser.MANUELL, dauer=0):
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=zeitstempel, zone_id=zone, ventil_id=ventil_id,
        aktion=aktion, dauer_sekunden=dauer, ausloser=ausloser,
    )))


def _orphans(speicher, cutoff=None):
    return _run(speicher.hole_orphan_oeffnen(
        zone_id="bambuswald",
        cutoff=cutoff or (T0 + timedelta(hours=1)),
        suchfenster=SUCHFENSTER,
    ))


# --------------------------------------------------------------------
# 1) Paarung auf zone_id + ventil_id
# --------------------------------------------------------------------

def test_fremder_kanal_paart_nicht(speicher):
    """DER Sicherheitsfund: Kanal 2 offen, Kanal 1 schliesst kurz darauf.
    Vorher galt Kanal 2 als gepaart und blieb ohne synthetisches Close --
    obwohl das Ventil real offen war."""
    _schreibe(speicher, VentilAktion.OEFFNEN, KANAL_2, T0)
    _schreibe(speicher, VentilAktion.SCHLIESSEN, KANAL_1,
              T0 + timedelta(minutes=10), dauer=600)

    orphans = _orphans(speicher)

    assert [o.ventil_id for o in orphans] == [KANAL_2]


def test_eigener_kanal_paart(speicher):
    _schreibe(speicher, VentilAktion.OEFFNEN, KANAL_2, T0)
    _schreibe(speicher, VentilAktion.SCHLIESSEN, KANAL_2,
              T0 + timedelta(minutes=10), dauer=600)

    assert _orphans(speicher) == []


def test_schliessen_ausserhalb_des_suchfensters_paart_nicht(speicher):
    """Grenzverhalten unveraendert: nur ein SCHLIESSEN INNERHALB des
    Fensters paart."""
    _schreibe(speicher, VentilAktion.OEFFNEN, KANAL_2, T0)
    _schreibe(speicher, VentilAktion.SCHLIESSEN, KANAL_2,
              T0 + SUCHFENSTER + timedelta(minutes=1), dauer=60)

    assert [o.ventil_id for o in _orphans(speicher)] == [KANAL_2]


# --------------------------------------------------------------------
# 2) Positivfilter statt Negativliste
# --------------------------------------------------------------------

@pytest.mark.parametrize(
    "ventil_id",
    ["aquabloom_solar", "gardena_web", "backfill_app", "manuell",
     "sensor_heuristik"],
)
def test_quellen_ohne_kanal_suffix_werden_nie_orphan(speicher, ventil_id):
    """Alle fuenf haben einen eigenen Abschluss-Vertrag. Ein synthetisches
    WATCHDOG-SCHLIESSEN darauf wuerde ML-Features, Budget und Bilanz
    vergiften. `aquabloom_solar` rutschte durch die alte Negativliste."""
    _schreibe(speicher, VentilAktion.OEFFNEN, ventil_id, T0)

    assert _orphans(speicher) == []


def test_echter_kanal_wird_weiterhin_erkannt(speicher):
    """Gegenprobe zum Positivfilter: er darf nicht so eng sein, dass er
    echte Kanaele aussperrt -- das waere der gefaehrliche Irrtum
    (kein Close fuer ein real offenes Ventil)."""
    _schreibe(speicher, VentilAktion.OEFFNEN, KANAL_1, T0)

    assert [o.ventil_id for o in _orphans(speicher)] == [KANAL_1]


def test_ignoriert_bleibt_ausgeschlossen(speicher):
    """Vom User verworfene OEFFNEN duerfen nicht per Orphan-Close
    wiederauferstehen."""
    _schreibe(speicher, VentilAktion.OEFFNEN, KANAL_2, T0,
              ausloser=Ausloser.IGNORIERT)

    assert _orphans(speicher) == []


# --------------------------------------------------------------------
# 3) Zeitformat -- Regression gegen die T-Separator-Falle
# --------------------------------------------------------------------

def test_zeitfenster_vergleicht_gegen_T_separator(speicher):
    """`datetime()` liefert ein Leerzeichen als Datums-Trennung, die
    Spalte nutzt `T`. Weil `'T' > ' '`, wuerde ein Vergleich gegen das
    Leerzeichen-Format JEDES SCHLIESSEN aussperren und jedes Open zum
    Orphan erklaeren -- lautlos und mit maximalem Schaden (synthetische
    Closes auf lauter gepaarte Laeufe).

    Genau dieser Fehler ist beim Nachmessen zu diesem Task passiert und
    meldete 1.856 statt 2 Orphans, deshalb steht er als Test da.
    Mikrosekunden im Zeitstempel sind hier Absicht: sie sind der Fall,
    an dem eine `datetime()`-Loesung zusaetzlich Genauigkeit verliert.
    """
    oeffnen = datetime(2026, 8, 1, 10, 0, 0, 500000)
    _schreibe(speicher, VentilAktion.OEFFNEN, KANAL_2, oeffnen)
    _schreibe(speicher, VentilAktion.SCHLIESSEN, KANAL_2,
              oeffnen + timedelta(minutes=30), dauer=1800)

    assert _orphans(speicher) == [], (
        "gepaarter Lauf wurde als Orphan gemeldet -- Zeitformat-Vergleich"
    )


def test_mehrere_zonen_bleiben_getrennt(speicher):
    """Der Zonenfilter darf durch die ventil_id-Verschaerfung nicht
    verlorengehen."""
    _schreibe(speicher, VentilAktion.OEFFNEN, KANAL_2, T0, zone="bambuswald")
    _schreibe(speicher, VentilAktion.SCHLIESSEN, KANAL_2,
              T0 + timedelta(minutes=10), zone="hecke", dauer=600)

    assert [o.ventil_id for o in _orphans(speicher)] == [KANAL_2]
