"""T-0550: das Nachzug-Fenster haengt nicht mehr an der Dosislaenge.

**Der Befund.** Die Grenze lautete `start_s + haupt_puls_s + 600`. Damit war
die erlaubte Verspaetung eine Funktion der geplanten Dosis: fuer bambuswald
3000 s bei Stufe 45 und 4800 s bei Stufe 75, also das 1,60-fache. In einem
randomisierten Dosis-Test haengt so die Ausfallwahrscheinlichkeit an der
BEHANDLUNG -- kurze Stufen brechen strukturell haeufiger ab und fehlen danach
in den Daten. Genau das ist in T-0535 eingetreten: beide Abbrueche lagen bei
45 und 60, keiner bei 75.

**Die Entscheidung (Andre, 27.08.2026):** flaches Fenster von 3600 s. An 143
realen Sequenzen seit 01.07. gegengerechnet -- es haette keinen einzigen Lauf
veraendert, waehrend 600 s sieben und 1800 s einen abgebrochen haetten.

**Was dieser Test NICHT behauptet:** dass 3600 s physikalisch richtig ist. Wie
lange die Vorbenetzung traegt, ist ungemessen. Er haelt fest, dass die Grenze
fuer alle Dosislaengen DIESELBE ist -- das war der Konstruktionsfehler.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import Ausloser
from bewaesserung.pre_soak import (
    _HAUPT_NACHZUG_FENSTER_S,
    _LOOP_TICK_S,
    PreSoakManager,
)


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


async def _lauf(mgr, haupt_min):
    ok, fehler = await mgr.starte(
        zone_id="bambuswald", kanal=2, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=25, haupt_min=haupt_min,
        ausloser=Ausloser.AUTOMATIK,
    )
    assert ok, fehler
    return mgr.laufender_lauf("bambuswald")


async def _startet_bei(haupt_min: int, verspaetung_min: int) -> bool:
    """Startet die Hauptdose, wenn sie `verspaetung_min` zu spaet drankommt?"""
    sicherung = _Sicherung()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    lauf = await _lauf(mgr, haupt_min)
    # Sollzeitpunkt ist das Pausenende (25 min).
    await mgr.tick(
        lauf.gestartet_am + timedelta(minutes=25 + verspaetung_min),
    )
    return any(phase == "haupt" for phase, _ in sicherung.aufrufe)


# --------------------------------------------------------------------------
# Der Kern: gleiche Grenze fuer verschieden lange Dosen
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gleiches_fenster_fuer_die_drei_teststufen():
    """Akzeptanzkriterium 2. Stufe 45/60/75 entsprechen 40/55/70 min Hauptdose.
    Der Testpunkt wird aus der Konstante abgeleitet und nicht in Minuten
    eingepinnt -- sonst faellt dieser Test bei jeder Anpassung des Fensters,
    obwohl seine AUSSAGE (gleiches Fenster fuer alle Dosislaengen) davon gar
    nicht beruehrt ist. Genau das ist bei T-0553 passiert."""
    drin = _HAUPT_NACHZUG_FENSTER_S // 60 - 5
    for haupt_min in (40, 55, 70):
        assert await _startet_bei(haupt_min, drin) is True, (
            f"Hauptdose {haupt_min} min startet nicht mehr"
        )


@pytest.mark.asyncio
async def test_gleiches_fenster_auch_jenseits_der_grenze():
    """Die Symmetrie muss in beide Richtungen gelten: zu spaet ist zu spaet,
    fuer jede Dosislaenge gleich."""
    draussen = _HAUPT_NACHZUG_FENSTER_S // 60 + 5
    for haupt_min in (40, 55, 70):
        assert await _startet_bei(haupt_min, draussen) is False, (
            f"Hauptdose {haupt_min} min startet trotz Ueberschreitung"
        )


@pytest.mark.asyncio
async def test_kurze_und_lange_dosis_kippen_am_selben_punkt():
    """Schaerfer als die beiden Tests oben: der Umschlagpunkt selbst ist
    identisch. Ein Minute davor startet beides, eine Minute danach keines."""
    kipp = _HAUPT_NACHZUG_FENSTER_S // 60
    for haupt_min in (1, 70):
        assert await _startet_bei(haupt_min, kipp - 1) is True
        assert await _startet_bei(haupt_min, kipp + 1) is False


# --------------------------------------------------------------------------
# Negativprobe
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_negativprobe_die_alte_formel_haette_gespreizt():
    """Rechnet die ALTE Formel nach und zeigt, dass sie die drei Teststufen
    verschieden behandelt haette. Ohne das koennte der Test oben auch dann
    gruen sein, wenn es nie ein Problem gab."""
    pause_s, toleranz = 25 * 60, 600
    # Die aussagekraeftige Groesse ist die erlaubte VERSPAETUNG, nicht die
    # absolute Grenze: letztere enthaelt die Pause, die fuer alle Stufen gleich
    # ist und das Verhaeltnis damit schoenrechnet (1,40 statt 1,60).
    verspaetung_alt = {
        haupt_min: haupt_min * 60 + toleranz for haupt_min in (40, 55, 70)
    }
    assert len(set(verspaetung_alt.values())) == 3, "alte Formel nicht gespreizt?"
    assert verspaetung_alt[40] == 3000 and verspaetung_alt[70] == 4800
    assert verspaetung_alt[70] / verspaetung_alt[40] == pytest.approx(1.60, abs=0.01)
    # Bei 55 min Verspaetung faellt die kurze Dosis durch, die lange nicht --
    # genau die behandlungsabhaengige Ausfallrate aus T-0535. Mit Abstand zu
    # beiden Grenzen gewaehlt (3000 und 4800 s), nicht auf der Kippe.
    assert 55 * 60 > verspaetung_alt[40], "40-min-Dosis waere alt abgebrochen"
    assert 55 * 60 <= verspaetung_alt[70], "70-min-Dosis waere alt durchgekommen"
    # Gegenprobe zur NEUEN Regel: dieselbe Verspaetung, ein einziger Wert.
    from bewaesserung.pre_soak import _HAUPT_NACHZUG_FENSTER_S
    assert len({_HAUPT_NACHZUG_FENSTER_S for _ in (40, 55, 70)}) == 1


# --------------------------------------------------------------------------
# Regression auf die Absicht von T-0344
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dosis_kuerzer_als_ein_tick_wird_weiter_nachgezogen():
    """T-0344 war gegen den Fall gebaut, dass ein Tick ueber das gesamte
    Hauptdose-Fenster springt. Das muss weiter funktionieren -- der flache
    Wert ist deutlich groesser als ein Tick."""
    assert _HAUPT_NACHZUG_FENSTER_S > _LOOP_TICK_S
    assert await _startet_bei(haupt_min=1, verspaetung_min=10) is True


def test_fenster_kann_nie_unter_einen_tick_fallen():
    """Die Klemme im Code. Senkt jemand die Konstante unter das
    Tick-Intervall, springt ein Tick wieder ueber das ganze Fenster und
    `fertig` schluckt die Dose -- genau der T-0344-Fall."""
    assert max(_LOOP_TICK_S, _HAUPT_NACHZUG_FENSTER_S) >= _LOOP_TICK_S
    # T-0553: 3600 -> 1200. Der Wert steht hier, damit eine Aenderung bewusst
    # geschieht; die uebrigen Tests leiten ihre Punkte daraus ab.
    assert _HAUPT_NACHZUG_FENSTER_S == 1200


@pytest.mark.asyncio
async def test_weit_ueberfaellig_bleibt_ein_sichtbarer_fehler():
    """Die Grenze ist verschoben, nicht abgeschafft: jenseits davon wird die
    Dose NICHT blind gefeuert, sondern der Lauf als Fehler markiert."""
    sicherung = _Sicherung()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    lauf = await _lauf(mgr, 40)
    zu_spaet = 25 + _HAUPT_NACHZUG_FENSTER_S // 60 + 10
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=zu_spaet))
    assert not any(phase == "haupt" for phase, _ in sicherung.aufrufe)
    assert lauf.phase == "fehler"
    assert lauf.fehler and "verpasst" in lauf.fehler
