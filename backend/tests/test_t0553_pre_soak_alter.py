"""T-0553: die Hauptdose darf keinen toten Pre-Soak mehr nutzen.

**Andres Einwand, 29.08.2026:** wird eine Mikrodrip-Sequenz von einer
Zeitfenster-Zone verdraengt (T-0552), muss sie hinterher komplett neu
ansetzen, Pre-Soak eingeschlossen -- "weil der ja ansonsten verpufft".

Das passiert nur, wenn die alte Sequenz vorher SCHEITERT. Mit dem
T-0550-Fenster von 3600 s hing das am Zufall: waldblumenhain belegt den Hahn
im Median 95 min, aber in 2 von 19 Sequenzen unter 60 min -- dann waere die
alte Hauptdose doch noch gefeuert worden.

**Der Denkfehler war die Bezugsgroesse.** Gemessen wurde die Verspaetung
gegenueber dem SOLL-START der Hauptdose. Entscheidend ist das ALTER DES
PRE-SOAK, denn dessen Vorbenetzung soll die Hauptdose nutzen. Zwischen
Pre-Soak-Ende und Soll-Start liegen 20 min; ein Fenster von 3600 erlaubte
also einen 80 Minuten alten Pre-Soak.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from bewaesserung.modelle import Ausloser
from bewaesserung.pre_soak import _HAUPT_NACHZUG_FENSTER_S, PreSoakManager


PRE_SOAK_MIN, PAUSE_MIN = 5, 25
EINSICKERN_MIN = PAUSE_MIN - PRE_SOAK_MIN   # Pre-Soak-ENDE bis Soll-Start


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


async def _starte(mgr):
    ok, fehler = await mgr.starte(
        zone_id="bambuswald", kanal=2, zone_ids_kanal=["bambuswald"],
        pre_soak_min=PRE_SOAK_MIN, pause_min=PAUSE_MIN, haupt_min=33,
        ausloser=Ausloser.AUTOMATIK,
    )
    assert ok, fehler
    return mgr.laufender_lauf("bambuswald")


async def _pre_soak_alter_bei_hauptdose(verspaetung_min: int):
    """Alter des Pre-Soak beim Start der Hauptdose; None = kein Start."""
    sicherung = _Sicherung()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    lauf = await _starte(mgr)
    await mgr.tick(
        lauf.gestartet_am + timedelta(minutes=PAUSE_MIN + verspaetung_min),
    )
    if not any(phase == "haupt" for phase, _ in sicherung.aufrufe):
        return None
    return EINSICKERN_MIN + verspaetung_min


def test_maximales_pre_soak_alter_ist_das_doppelte_der_einsickerzeit():
    """Die Kernaussage in einer Zeile: 20 min Einsickern plus 20 min Fenster."""
    max_alter = EINSICKERN_MIN + _HAUPT_NACHZUG_FENSTER_S // 60
    assert max_alter == 40
    assert max_alter == 2 * EINSICKERN_MIN


@pytest.mark.asyncio
async def test_puenktliche_hauptdose_nutzt_frischen_pre_soak():
    assert await _pre_soak_alter_bei_hauptdose(0) == EINSICKERN_MIN


@pytest.mark.asyncio
async def test_hauptdose_am_fensterrand_ist_noch_erlaubt():
    rand = _HAUPT_NACHZUG_FENSTER_S // 60
    assert await _pre_soak_alter_bei_hauptdose(rand - 1) == 39


@pytest.mark.asyncio
async def test_zu_alter_pre_soak_feuert_NICHT_mehr():
    """Der eigentliche Fix. Frueher lief das bis 80 min Pre-Soak-Alter."""
    zu_spaet = _HAUPT_NACHZUG_FENSTER_S // 60 + 1
    assert await _pre_soak_alter_bei_hauptdose(zu_spaet) is None


@pytest.mark.asyncio
async def test_der_realfall_verdraengung_durch_waldblumenhain():
    """waldblumenhain belegt den Hahn im Median 95 min. Nach so einer
    Verdraengung darf die alte Hauptdose nicht mehr starten."""
    assert await _pre_soak_alter_bei_hauptdose(95) is None


@pytest.mark.asyncio
async def test_auch_die_kurze_verdraengung_faellt_jetzt_durch():
    """Die 2 von 19 kurzen waldblumenhain-Sequenzen (unter 60 min) waren
    unter dem alten Fenster genau der Fall, in dem eine tote Dose doch noch
    gefeuert haette."""
    assert await _pre_soak_alter_bei_hauptdose(50) is None


@pytest.mark.asyncio
async def test_gescheiterte_sequenz_gibt_die_zone_frei_und_beginnt_neu_mit_pre_soak():
    """Andres eigentliche Forderung: nach der Verdraengung ein VOLLSTAENDIGER
    Neustart, Pre-Soak eingeschlossen."""
    sicherung = _Sicherung()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    lauf = await _starte(mgr)
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=PAUSE_MIN + 95))
    assert lauf.phase == "fehler"
    assert mgr.aktiver_lauf_fuer_kanal_der_zone("bambuswald") is None

    sicherung.aufrufe.clear()
    await _starte(mgr)
    assert sicherung.aufrufe and sicherung.aufrufe[0][0] == "pre_soak", (
        "die neue Sequenz muss wieder mit einem Pre-Soak beginnen"
    )


@pytest.mark.asyncio
async def test_negativprobe_altes_fenster_haette_die_tote_dose_gefeuert():
    """Baut das 3600er-Fenster nach: die Verdraengung von 50 min waere
    durchgegangen, mit einem 70 Minuten alten Pre-Soak."""
    import bewaesserung.pre_soak as mod

    original = mod._HAUPT_NACHZUG_FENSTER_S
    mod._HAUPT_NACHZUG_FENSTER_S = 3600
    try:
        alter = await _pre_soak_alter_bei_hauptdose(50)
    finally:
        mod._HAUPT_NACHZUG_FENSTER_S = original
    assert alter == 70, (
        "Negativprobe: mit dem alten Fenster startet die Hauptdose mit einem "
        "70 Minuten alten Pre-Soak -- genau der Fall, den T-0553 verhindert."
    )
