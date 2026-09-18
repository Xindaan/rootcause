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
"""

import ast
import builtins
import inspect

from bewaesserung import main


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
# sind. Die Liste ist der Vertrag des Umbaus.
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


def test_wartungsjobs_sind_im_wartungsloop_verdrahtet():
    """Jeder gewanderte Job muss dort ankommen, wo er jetzt hingehoert.

    Das ist die gefaehrliche Richtung des T-0403-Umbaus: waere ein Job aus dem
    Entscheidungs-Loop entfernt, im Wartungs-Loop aber nicht eingehaengt
    worden, liefe er NIE wieder -- kein Backup, kein Retrain, keine
    Kalibrierung, und nichts davon wuerde einen Fehler werfen.
    """
    signatur = inspect.signature(main._wartungs_loop)
    quelle = inspect.getsource(main._wartungs_loop)
    for job in WARTUNGS_JOBS:
        assert job in signatur.parameters, (
            f"`{job}` fehlt in der Signatur von _wartungs_loop"
        )
        assert f"{job}.aktualisiere_wenn_faellig" in quelle, (
            f"`{job}` ist Parameter, wird im Wartungs-Loop aber nicht gerufen "
            "-> der Job laeuft nirgends mehr"
        )


def test_wartungsjobs_nicht_mehr_im_entscheidungsloop():
    """Regressionsschutz fuer den eigentlichen Fix (T-0403).

    Realfall 02.08.2026: `phasen={'ml_retrain': 1421.06}` in einem Zyklus von
    1444 s. Der Retrain steht an Position 14, die Kanalpruefung an Position 2 --
    ein langer Job verzoegert also nicht seinen eigenen Durchlauf, sondern den
    START des naechsten. Die Hecke stand dadurch 24 Minuten unter Schwelle,
    ohne dass etwas hinsah. Wer einen dieser Jobs zurueckschiebt, stellt genau
    das wieder her.
    """
    quelle = inspect.getsource(main._entscheidungsloop)
    zurueck = [j for j in WARTUNGS_JOBS if f"{j}.aktualisiere_wenn_faellig" in quelle]
    assert zurueck == [], (
        f"Wartungsjobs wieder im Entscheidungs-Loop: {zurueck}. "
        "Damit taktet die Kanalpruefung wieder hinter ihrer Laufzeit her."
    )


def test_wartungs_task_wird_gestartet_und_beendet():
    """Der Loop selbst muss verdrahtet sein, sonst laeuft gar nichts mehr.

    Ein `_wartungs_loop`, den niemand als Task startet, ist der stillste
    denkbare Ausfall: die Jobs sind sauber implementiert, getestet und
    parametrisiert -- und werden nie gerufen. Ebenso muss er beim Shutdown in
    `tasks_zu_beenden` stehen, sonst haengt das Herunterfahren an einem Task,
    den niemand canceled.
    """
    quelle = inspect.getsource(main.ausfuehren)
    assert "_wartungs_loop(" in quelle, "Wartungs-Loop wird nie gestartet"
    assert "wartungs_task = asyncio.create_task" in quelle
    assert "wartungs_task," in quelle.split("tasks_zu_beenden")[1][:300], (
        "wartungs_task fehlt in tasks_zu_beenden -> haengt beim Shutdown"
    )


def test_beide_loops_haben_db_lock_backoff():
    """T-0403 mit T-0210: der Lock-Backoff muss in BEIDEN Loops stehen.

    Der Umbau hat die Konstellation vom 18.05.2026 wiederhergestellt: ML-Drift
    und Backup liefen bis 05.08. sequentiell im Entscheidungstakt, jetzt echt
    parallel dazu. Ein langer Lese-Burst gegen einen parallelen Schreiber
    ergibt `database is locked`; ohne Backoff laeuft der betroffene Loop im
    Folgezyklus sofort in denselben Konflikt.
    """
    for fn in (main._entscheidungsloop, main._wartungs_loop):
        quelle = inspect.getsource(fn)
        assert "sqlite3.OperationalError" in quelle, (
            f"{fn.__name__} faengt den Lock-Fall nicht gesondert"
        )
        assert "db_lock_backoff" in quelle, (
            f"{fn.__name__} ohne Lock-Backoff -> Endlos-Konflikt moeglich"
        )


def test_detektor_ist_im_loop_verdrahtet():
    """Konkrete Gegenprobe fuer T-0416: der Detektor muss Parameter sein.

    Ohne ihn laeuft der Push-Detektor nie -- und ein unbemerkter
    FYTA-Kalibrier-Push verfaelscht still das ML-Training.
    """
    signatur = inspect.signature(main._entscheidungsloop)
    assert "fyta_sprung_detektor" in signatur.parameters

    quelle = inspect.getsource(main._entscheidungsloop)
    assert "fyta_sprung_detektor.aktualisiere_wenn_faellig" in quelle, (
        "Detektor ist Parameter, wird im Loop aber nicht aufgerufen"
    )


def test_setup_reihenfolge_in_ausfuehren():
    """T-0416 (23.07.): Guard gegen `UnboundLocalError` beim Start.

    Realfall: die FytaSprungDetektor-Instanziierung wurde VOR
    `benachrichtiger = Benachrichtiger()` eingefuegt und griff auf die
    Variable zu -> UnboundLocalError, launchd-Crash-Loop. Weder der
    Import-Test noch der Loop-Guard fangen das: der Import laeuft sauber
    durch, und der Fehler liegt im Setup, nicht im Loop.

    Bewusst ein GEZIELTER Reihenfolge-Test statt einer generischen
    "use before assignment"-Analyse. Ein erster Versuch mit AST-Walk
    meldete 15 Fehlalarme (Comprehension-Variablen, try-Block-Zuweisungen,
    lokale Imports) -- ein korrekter Check braeuchte echte
    Datenflussanalyse. Ein Guard, der bei jedem Lauf Rauschen produziert,
    wird ignoriert und ist damit wertlos.

    Wer hier eine Abhaengigkeit ergaenzt: Paar eintragen, fertig.
    """
    quelle = inspect.getsource(main.ausfuehren)
    # (was gebraucht wird, wer es braucht)
    paare = [
        ("benachrichtiger = Benachrichtiger()",
         "fyta_sprung_detektor = FytaSprungDetektor"),
        ("speicher = Speicher(", "fyta_sprung_detektor = FytaSprungDetektor"),
        ("speicher = Speicher(", "leck_detektor = LeckDetektor"),
        ("speicher = Speicher(", "wasserbilanz_job = WasserbilanzJob"),
    ]
    for zuerst, danach in paare:
        assert zuerst in quelle, f"Anker verschwunden: {zuerst}"
        assert danach in quelle, f"Anker verschwunden: {danach}"
        assert quelle.index(zuerst) < quelle.index(danach), (
            f"`{danach}` steht VOR `{zuerst}` -> UnboundLocalError beim "
            f"Start (launchd-Crash-Loop). Instanziierung nach unten schieben."
        )


def test_t0404_ws_task_hat_done_callback():
    """T-0404: Der WS-Task darf nicht lautlos sterben koennen.

    Realfall 10.07.: nach 13 fehlgeschlagenen Reconnects (DNS weg) endete
    das Log ohne Traceback und ohne Exit-Grund. Ein `create_task`-Ergebnis,
    das niemand ansieht, verschluckt die Exception. launchd startete per
    KeepAlive neu -- und deployte dabei still den damaligen Working-Tree,
    also einen halben Deploy, den niemand beauftragt hatte.

    Statischer Guard: der Task muss einen done-Callback bekommen, und der
    muss laut sein (`critical`). Reine Anwesenheitspruefung, weil der
    Startup-Pfad zu I/O-nah fuer einen Unit-Test ist.
    """
    quelle = inspect.getsource(main.ausfuehren)
    assert "ws_task.add_done_callback" in quelle, (
        "WS-Task ohne done-Callback -- ein stiller Tod waere wieder moeglich"
    )
    assert "websocket_task_gestorben" in quelle
    assert "logger.critical" in quelle, (
        "Der Exit-Pfad muss laut sein; warning geht im Normalbetrieb unter"
    )
