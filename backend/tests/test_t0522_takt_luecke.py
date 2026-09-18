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


def test_t0522_beide_loops_rufen_den_detektor():
    """Verdrahtungs-Guard: der Detektor haengt an BEIDEN Warte-Stellen.

    Ohne diesen Test koennte jemand eine der beiden `wait_for`-Stellen
    umbauen und die Meldung faellt still aus -- der Ausfall waere dann
    wieder genau so unsichtbar wie vor T-0522.
    """
    import inspect

    from bewaesserung import main

    for loop_name in ("_entscheidungsloop", "_wartungs_loop"):
        quelle = inspect.getsource(getattr(main, loop_name))
        assert "_melde_takt_luecke(" in quelle, (
            f"{loop_name} misst seine Wanduhr-Luecke nicht mehr"
        )
        assert "warte_start = time.time()" in quelle, (
            f"{loop_name} nimmt keinen Wanduhr-Startpunkt mehr"
        )
