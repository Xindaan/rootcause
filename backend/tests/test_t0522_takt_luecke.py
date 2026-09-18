"""T-0522: der Loop meldet Wanduhr-Luecken, statt sie stillschweigend zu schlucken.

Ausgangslage: `pmset` weist fuer den Steuerrechner 159-300 min Schlaf pro Tag
aus. Ob die Anlage in dieser Zeit wirklich nicht entscheidet, war NICHT
feststellbar -- der Loop taktet nach dem Aufwachen einfach weiter und schreibt
keine Zeile darueber. Die Frage liess sich nur nachtraeglich und indirekt aus
`empfehlungs_audit` rekonstruieren (Ergebnis 07.08.: der Dienst lief in
DarkWake durch). Genau diese Rekonstruktion soll kuenftig entfallen.

Geprueft wird die Detektor-Logik, nicht der Schlaf: `_melde_takt_luecke` misst
die WANDUHR-Dauer des Wartens. Die asyncio-Uhr steht im Schlaf still, deshalb
kehrt ein `wait_for(timeout=300)` danach erst viel spaeter zurueck.

`caplog` taugt hier nicht -- structlog haengt nicht an der stdlib. Deshalb der
Log-Spion, wie in `test_request_raten.py`.
"""
from __future__ import annotations

import pytest

from bewaesserung.main import _melde_takt_luecke


class _LogSpion:
    """Ersetzt den Modul-Logger und sammelt die Warnungen strukturiert."""

    def __init__(self) -> None:
        self.warnungen: list[dict] = []

    def warning(self, ereignis: str, **kwargs) -> None:
        self.warnungen.append({"ereignis": ereignis, **kwargs})

    # Der Detektor ruft nur `warning`; die uebrigen Level sind No-ops,
    # damit der Spion bei einer spaeteren Erweiterung nicht crasht.
    def info(self, ereignis: str, **kwargs) -> None:
        pass


@pytest.fixture
def spion(monkeypatch) -> _LogSpion:
    s = _LogSpion()
    monkeypatch.setattr("bewaesserung.main.logger", s)
    return s


def _friere_uhr(monkeypatch, jetzt: float) -> None:
    monkeypatch.setattr("bewaesserung.main.time.time", lambda: jetzt)


def test_t0522_normaler_takt_meldet_nichts(spion, monkeypatch):
    """Ein Takt in der erwarteten Dauer ist keine Luecke."""
    _friere_uhr(monkeypatch, 1000.0)
    gewartet = _melde_takt_luecke("entscheidungsloop", 695.0, 300)

    assert gewartet == pytest.approx(305.0)
    assert spion.warnungen == [], "normaler Takt darf nicht warnen"


def test_t0522_einzelner_verspaeteter_tick_meldet_nichts(spion, monkeypatch):
    """Knapp unter zwei Takten bleibt still -- die Schwelle ist bewusst 2x.

    Ein einzelner verspaeteter Tick ist Scheduling-Jitter. Wuerde schon der
    warnen, ginge die Meldung im Rauschen unter und der echte Fall (mehrere
    ausgefallene Entscheidungen) waere nicht mehr auffindbar.
    """
    _friere_uhr(monkeypatch, 1000.0)
    _melde_takt_luecke("entscheidungsloop", 1000.0 - 599.0, 300)

    assert spion.warnungen == []


def test_t0522_verschlafenes_fenster_wird_gemeldet(spion, monkeypatch):
    """Der Realfall: 289 min statt 5 min Warten -> Warnung mit Zahl.

    Nachgebaut ist das laengste Fenster aus dem T-0522-Befund
    (05.08., 03:26-08:15).
    """
    _friere_uhr(monkeypatch, 100_000.0)
    gewartet = _melde_takt_luecke("entscheidungsloop", 100_000.0 - 289 * 60, 300)

    assert gewartet == pytest.approx(17_340.0)
    assert len(spion.warnungen) == 1
    w = spion.warnungen[0]
    assert w["ereignis"] == "entscheidungsloop.takt_luecke"
    assert w["gewartet_s"] == pytest.approx(17_340.0)
    assert w["erwartet_s"] == 300
    # 17340 // 300 = 57 Takte verstrichen, 56 davon sind ausgefallen.
    assert w["ausgefallene_takte"] == 56


def test_t0522_gilt_auch_fuer_den_wartungsloop(spion, monkeypatch):
    """Isomorphie-Check: beide Loops haben dieselbe Warte-Struktur.

    Waere nur der Entscheidungsloop instrumentiert, blieben Ausfaelle des
    Wartungsloops (Backup, Retrain, Drift) weiter unsichtbar -- genau die
    halbe Verdrahtung aus `fehlerpattern_detektor_ohne_konsument`.
    """
    _friere_uhr(monkeypatch, 50_000.0)
    _melde_takt_luecke("wartungsloop", 50_000.0 - 3600, 300)

    assert len(spion.warnungen) == 1
    assert spion.warnungen[0]["ereignis"] == "wartungsloop.takt_luecke"
    assert spion.warnungen[0]["ausgefallene_takte"] == 11


@pytest.mark.parametrize("loop", ["entscheidungsloop", "wartungsloop"])
async def test_t0522_beide_loops_messen_ihr_warten(monkeypatch, loop):
    """Verdrahtung: der Detektor haengt an BEIDEN Warte-Stellen, und der
    Wanduhr-Startpunkt liegt VOR dem Warten (sonst misst er nichts).

    T-0610 AT-3: frueher ein Quelltext-Match auf `_melde_takt_luecke(` und
    `warte_start = time.time()`. Jetzt laeuft der echte Loop mit kurzem Takt
    in seine zweite Runde; gemessen wird, dass er nach dem Warten den
    Detektor mit seinem Namen und einem Startpunkt aufruft, der mindestens
    einen Takt zurueckliegt.
    """
    import asyncio
    import time
    from unittest.mock import MagicMock

    from bewaesserung import main
    from test_main_loop_verdrahtung import _loop_konfig
    from test_t0556_zyklus_warnung import _AsyncAttrappe, _motor_attrappe

    takt = 0.05
    monkeypatch.setattr(main, "ENTSCHEIDUNGSINTERVALL_SEKUNDEN", takt)
    monkeypatch.setattr(main, "WARTUNGSINTERVALL_SEKUNDEN", takt)
    monkeypatch.setattr(main, "ERSTE_PRUEFUNG_VERZOEGERUNG_S", 0.01)
    aufrufe: list[tuple[str, float, float]] = []

    def _spion(name, warte_start, intervall_s):
        aufrufe.append((name, time.time() - warte_start, intervall_s))
        return 0.0

    monkeypatch.setattr(main, "_melde_takt_luecke", _spion)
    stop = asyncio.Event()
    if loop == "wartungsloop":
        task = asyncio.create_task(main._wartungs_loop(stop))
    else:
        task = asyncio.create_task(main._entscheidungsloop(
            motor=_motor_attrappe(), konfig=_loop_konfig(),
            benachrichtiger=MagicMock(), verarbeiter=MagicMock(),
            stop_event=stop, speicher=_AsyncAttrappe(),
        ))
    try:
        for _ in range(500):
            if aufrufe:
                break
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    assert aufrufe, f"{loop} misst seine Wanduhr-Luecke nicht mehr"
    name, gewartet, intervall = aufrufe[0]
    assert name == loop and intervall == takt
    assert gewartet >= takt * 0.9, (
        f"{loop}: Startpunkt liegt nicht vor dem Warten ({gewartet:.3f} s)"
    )
