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
