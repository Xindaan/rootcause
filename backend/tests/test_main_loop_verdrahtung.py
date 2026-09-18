"""Guard gegen unverdrahtete Namen im Entscheidungs-Loop.

**Warum es diesen Test gibt (Realfall 21.07.2026, T-0416).**
Beim Einhaengen des FYTA-Sprung-Detektors wurde das Objekt in der Setup-
Funktion instanziiert, im Loop aber benutzt -- der Loop bekommt seine Jobs
jedoch als PARAMETER. Ergebnis: `NameError` in Produktion, jeden Zyklus.
Der Loop hat einen aeusseren Except-Handler, ueberlebte also; aber **alles
nach der Fehlerstelle wurde uebersprungen** (Wetter-Archiv, DB-Backup,
ML-Drift). Ein still degradierter Loop, der aussieht als liefe er.

Die gesamte Test-Suite (1313 gruen) hat das nicht gesehen, weil kein Test die
Verdrahtung von `_entscheidungsloop` beruehrt -- die Funktion ist zu gross und
zu I/O-nah, um sie in einem Unit-Test aufzurufen. Statt sie auszufuehren,
prueft dieser Test sie STATISCH: jeder gelesene Name muss aufloesbar sein
(Parameter, lokale Bindung, Modul-Global oder Builtin).

Das faengt die ganze Fehlerklasse, nicht nur den einen Fall: jeder kuenftige
Job, der im Loop benutzt aber nicht durchgereicht wird, faellt hier auf.

**T-0610 AT-3 (14.10.2026): Rest der Datei als Verhaltenstests.** Die beiden
Namens-Guards bleiben statisch (begruendet an den Tests). Die frueheren
Quelltext-Matches ("steht `job.aktualisiere_wenn_faellig` im Loop?") fahren
jetzt den echten Loop eine Runde und messen, was er aufruft. Die Verdrahtung
in `ausfuehren` (welcher Loop welchen Job bekommt, Wartungs-Task gestartet und
beim Shutdown beendet, Setup-Reihenfolge, WS-Done-Callback) prueft
`test_t0610_at11_start_lauf.py` ueber einen echten Start-Lauf.

Harmlos auch ohne die geprueften Aufrufe: alle Jobs sind Spione ohne
Wirkung, Motor und Speicher sind Attrappen (kein Netz, keine Hardware, keine
DB-Datei), und jeder Lauf endet ueber das Stop-Event bzw. ein Zeitlimit.
"""

import ast
import asyncio
import builtins
import inspect
import sqlite3
import time
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from bewaesserung import main
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
    ZonenModus,
)
from test_t0556_zyklus_warnung import _AsyncAttrappe, _motor_attrappe


def _gebundene_namen(fndef: ast.AST) -> set[str]:
    """Alle Namen, die innerhalb der Funktion gebunden werden."""
    gebunden: set[str] = set()

    def args_von(node) -> set[str]:
        a = node.args
        namen = {x.arg for x in a.args + a.kwonlyargs + a.posonlyargs}
        if a.vararg:
            namen.add(a.vararg.arg)
        if a.kwarg:
            namen.add(a.kwarg.arg)
        return namen

    gebunden |= args_von(fndef)
    for n in ast.walk(fndef):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            gebunden.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for alias in n.names:
                gebunden.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            gebunden.add(n.name)
        elif isinstance(n, ast.Lambda):
            # Lambda-Parameter (z.B. `key=lambda paar: ...`).
            gebunden |= args_von(n)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n is not fndef:
                gebunden.add(n.name)
                gebunden |= args_von(n)
        elif isinstance(n, ast.comprehension):
            for t in ast.walk(n.target):
                if isinstance(t, ast.Name):
                    gebunden.add(t.id)
    return gebunden


def _ungebundene(fn, modul) -> list[str]:
    baum = ast.parse(inspect.getsource(fn).lstrip())
    fndef = baum.body[0]
    gelesen = {
        n.id for n in ast.walk(fndef)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    aufloesbar = (
        _gebundene_namen(fndef) | set(dir(modul)) | set(dir(builtins))
    )
    return sorted(gelesen - aufloesbar)


def test_entscheidungsloop_hat_keine_ungebundenen_namen():
    """Jeder im Loop gelesene Name muss aufloesbar sein.

    Bewusst statisch behalten (T-0610 AT-3): ein Lauf erreicht nur die
    Zweige, die seine Eingaben ausloesen; der Guard prueft JEDEN Zweig des
    Loops, auch die seltenen (Notfall, Max-Stop, Pre-Soak). Er matcht keine
    Zeichenkette, sondern loest Namen auf -- eine Umbenennung uebersteht er.

    Schlaegt dieser Test an, wurde meist ein Job im Loop benutzt, aber nicht
    als Parameter durchgereicht -- genau der T-0416-Fehler. Fix: Parameter in
    die Signatur von `_entscheidungsloop` UND an beide Aufruf-Stellen.
    """
    offen = _ungebundene(main._entscheidungsloop, main)
    assert offen == [], (
        f"Ungebundene Namen im Entscheidungs-Loop: {offen}. "
        "Wahrscheinlich ein Job, der instanziiert, aber nicht als Parameter "
        "durchgereicht wurde -> NameError zur Laufzeit, und alles NACH der "
        "Stelle wird jeden Zyklus uebersprungen."
    )


def test_wartungsloop_hat_keine_ungebundenen_namen():
    """Derselbe Guard fuer den zweiten Loop (T-0403).

    Der Wartungs-Loop hat die gleiche Bauform wie der Entscheidungs-Loop und
    damit die gleiche Schwachstelle: Jobs kommen als Parameter, und ein
    vergessener Parameter faellt erst zur Laufzeit auf. Ohne diesen Test
    haette der T-0403-Umbau die Fehlerklasse einfach mitgenommen.
    """
    offen = _ungebundene(main._wartungs_loop, main)
    assert offen == [], (
        f"Ungebundene Namen im Wartungs-Loop: {offen}. "
        "Wahrscheinlich ein Job, der benutzt, aber nicht als Parameter "
        "durchgereicht wurde."
    )


# T-0403: Jobs, die aus dem Entscheidungs-Loop in den Wartungs-Loop gewandert
# sind. Die Liste ist der Vertrag des Umbaus (dieselbe Liste prueft der
# Start-Lauf in test_t0610_at11_start_lauf.py auf der Seite von `ausfuehren`).
WARTUNGS_JOBS = [
    "backup_job",
    "ml_drift_job",
    "ml_retrain_job",
    "ml_response_retrain_job",
    "wochen_report_job",
    "kalibrations_job",
    "skalen_mapping_fit_job",
    "k_basis_fit_job",
    "wirkung_fit_job",
    "plant_optimum_job",
    # T-0480: nicht gewandert, sondern neu -- gehoert aber in dieselbe
    # Kategorie (laeuft nach Groessencheck, nie zeitkritisch).
    "log_rotation_job",
]


class _JobSpion:
    """Job ohne Wirkung; haelt fest, dass der Loop ihn getickt hat."""

    def __init__(self, name: str, gerufen: list[str]):
        self._name = name
        self._gerufen = gerufen

    async def aktualisiere_wenn_faellig(self):
        self._gerufen.append(self._name)


async def _warte_bis(bedingung, sekunden: float = 5.0) -> bool:
    ende = time.monotonic() + sekunden
    while time.monotonic() < ende:
        if bedingung():
            return True
        await asyncio.sleep(0.01)
    return bedingung()


def _loop_konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[ZonenKonfig(zone_id="z1", name="Z1", modus=ZonenModus.MONITORING)],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.5, laenge=13.4)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
    )


def _starte_entscheidungsloop(stop: asyncio.Event, **jobs) -> asyncio.Task:
    return asyncio.create_task(main._entscheidungsloop(
        motor=_motor_attrappe(), konfig=_loop_konfig(),
        benachrichtiger=MagicMock(), verarbeiter=MagicMock(),
        stop_event=stop, speicher=_AsyncAttrappe(), **jobs,
    ))


async def _beende(task: asyncio.Task, stop: asyncio.Event) -> None:
    stop.set()
    await asyncio.wait_for(task, timeout=10)


async def test_wartungsloop_tickt_jeden_wartungsjob():
    """Jeder gewanderte Job muss im Wartungs-Loop laufen (T-0403).

    Das ist die gefaehrliche Richtung des Umbaus: waere ein Job aus dem
    Entscheidungs-Loop entfernt, im Wartungs-Loop aber nicht eingehaengt
    worden, liefe er NIE wieder -- kein Backup, kein Retrain, keine
    Kalibrierung, und nichts davon wuerde einen Fehler werfen.
    """
    gerufen: list[str] = []
    stop = asyncio.Event()
    task = asyncio.create_task(main._wartungs_loop(
        stop, **{job: _JobSpion(job, gerufen) for job in WARTUNGS_JOBS},
    ))
    try:
        await _warte_bis(lambda: set(gerufen) >= set(WARTUNGS_JOBS))
    finally:
        await _beende(task, stop)
    assert sorted(set(gerufen)) == sorted(WARTUNGS_JOBS)


@pytest.mark.parametrize(
    "job", ["fyta_sprung_detektor", "fyta_luecken_job", "fyta_status_job"],
)
async def test_entscheidungsloop_tickt_seine_jobs(monkeypatch, job):
    """T-0416 (FYTA-Kalibrier-Push), T-0504 (FYTA-Luecken), T-0527
    (FYTA-Geraetestatus): ein Job, den niemand tickt, ist toter Code
    ([[fehlerpattern_job_nicht_in_loop_verdrahtet]]). Ersetzt die
    Quelltext-Tests hier sowie in test_fyta_luecken_job.py und
    test_fyta_status_job.py (Teil "Loop")."""
    monkeypatch.setattr(main, "ERSTE_PRUEFUNG_VERZOEGERUNG_S", 0.01)
    gerufen: list[str] = []
    stop = asyncio.Event()
    task = _starte_entscheidungsloop(stop, **{job: _JobSpion(job, gerufen)})
    try:
        await _warte_bis(lambda: job in gerufen)
    finally:
        await _beende(task, stop)
    assert gerufen == [job]


@pytest.mark.parametrize("loop", ["entscheidungsloop", "wartungsloop"])
async def test_beide_loops_haben_db_lock_backoff(monkeypatch, loop):
    """T-0403 mit T-0210: der Lock-Backoff muss in BEIDEN Loops greifen.

    Der Umbau hat die Konstellation vom 18.05.2026 wiederhergestellt: ML-Drift
    und Backup laufen echt parallel zum Entscheidungstakt. Ein langer
    Lese-Burst gegen einen parallelen Schreiber ergibt `database is locked`;
    ohne Backoff laeuft der betroffene Loop im Folgezyklus sofort in
    denselben Konflikt. Ausgeloest wird der Lock-Fehler im Zyklusrumpf (an
    der ersten Phasenmarke), gemessen wird, welcher Zweig ihn behandelt.
    """
    monkeypatch.setattr(main, "ERSTE_PRUEFUNG_VERZOEGERUNG_S", 0.01)

    class _GesperrterTimer(main._PhasenTimer):
        geworfen = False

        def mark(self, name):
            if not _GesperrterTimer.geworfen:
                _GesperrterTimer.geworfen = True
                raise sqlite3.OperationalError("database is locked")
            super().mark(name)

    monkeypatch.setattr(main, "_PhasenTimer", _GesperrterTimer)
    stop = asyncio.Event()
    with capture_logs() as logs:
        if loop == "wartungsloop":
            task = asyncio.create_task(main._wartungs_loop(stop))
        else:
            task = _starte_entscheidungsloop(stop)
        try:
            await _warte_bis(lambda: any(
                e["event"] in (f"{loop}.db_lock_backoff", f"{loop}.fehler")
                for e in logs
            ))
        finally:
            await _beende(task, stop)
    ereignisse = [e["event"] for e in logs]
    assert _GesperrterTimer.geworfen
    assert f"{loop}.db_lock_backoff" in ereignisse, ereignisse
    assert f"{loop}.fehler" not in ereignisse, ereignisse
