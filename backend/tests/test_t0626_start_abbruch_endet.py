"""T-0626: Ein Startfehler nach dem Oeffnen der Datenbank beendet den Prozess.

**Warum es diesen Test gibt.** `Speicher.verbinden` startet mit
`aiosqlite.connect` einen eigenen, nicht-daemonischen Worker-Thread. Brach der
Start danach ab, bevor ein Schutzblock den Speicher schloss (Fehler in
`_migriere`, in `_baue_dienste`), endete `asyncio.run`, der Prozess aber
nicht: der Interpreter wartete beim Shutdown auf den Thread. launchd
(`KeepAlive={SuccessfulExit:false}`) startet nur nach einem Exit mit rc != 0
neu, ein haengender Prozess bleibt stehen -- stiller Totalausfall.

Gemessen wird deshalb im echten Kindprozess ueber `main.main()`: Exit-Code
und Zeit vom Startbeginn bis zum Prozessende. Der Start-Lauf-Test
(`test_t0610_at11_start_lauf.py`) kann das nicht sehen, er schliesst den
Speicher in seinem eigenen `finally`, damit die Suite nicht haengt.

Harmlos auch ohne den geprueften Schutz:
- DB und Lock-Datei liegen in `tmp_path`; `.env` wird nicht geladen;
- Gardena-Client, uvicorn-Server, `warte_auf_gardena`, FYTA-Client,
  Customer-Auth und Benachrichtiger sind Attrappen, die bei Benutzung mit
  "NETZ_VERBOTEN" abbrechen -- greift die Fehlerinjektion nicht, endet der
  Kindprozess dort statt Netz oder Port 8090 zu beruehren;
- ein haengender Kindprozess wird nach dem Zeitlimit getoetet.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

import bewaesserung

SRC = Path(bewaesserung.__file__).resolve().parent.parent
# Importpfad-Beleg: der Test laeuft gegen den Checkout, in dem er liegt.
assert SRC.parent == Path(__file__).resolve().parents[1], (
    f"falscher Checkout importiert: {bewaesserung.__file__}"
)

_KIND = r'''
import sys, time
from pathlib import Path
from unittest.mock import MagicMock

src, db, fehler, start_datei = sys.argv[1:5]
sys.path.insert(0, src)
if fehler == "import":
    # Gate W12 G12-1: ein Modul, das der Start braucht, fehlt oder bricht
    # (halb synchronisierter Deploy). Darf nicht erst nach dem Oeffnen der DB
    # auffallen.
    sys.modules["bewaesserung.start_monitoring"] = None
import bewaesserung
assert Path(bewaesserung.__file__).resolve().parent == (Path(src) / "bewaesserung").resolve(), bewaesserung.__file__
Path(start_datei).write_text(repr(time.time()))
from bewaesserung import main
from bewaesserung.modelle import GardenaKonfig, GesamtKonfig, SpeicherKonfig, WetterKonfig

konfig = GesamtKonfig(
    gardena=GardenaKonfig(client_id="t", client_secret="t"), zonen=[],
    wetter=WetterKonfig(), speicher=SpeicherKonfig(db_pfad=db),
)

def _verboten(*a, **kw):
    raise SystemExit("NETZ_VERBOTEN")

async def _verboten_async(*a, **kw):
    raise SystemExit("NETZ_VERBOTEN")

main.dotenv.load_dotenv = lambda *a, **kw: None
main.lade_konfig = lambda pfad: konfig
main._konfiguriere_logging = lambda *a: None
main.GardenaClient = _verboten
main.FytaClient = _verboten
main.Benachrichtiger = MagicMock
main.baue_gardena_customer_auth = lambda *a: None
main.uvicorn.Server = _verboten
if fehler != "import":
    from bewaesserung import start_monitoring
    start_monitoring.warte_auf_gardena = _verboten_async

if fehler == "import":
    pass
elif fehler == "migriere":
    import sqlite3
    async def _kaputte_migration(self):
        raise sqlite3.OperationalError("INJIZIERT_MIGRATION")
    main.Speicher._migriere = _kaputte_migration
elif fehler == "dienste":
    async def _kaputte_dienste(*a, **kw):
        raise RuntimeError("INJIZIERT_DIENSTE")
    main._baue_dienste = _kaputte_dienste
elif fehler == "heilung":
    # Abbruch (Ctrl-C im Start) waehrend der Paar-Heilung in Startschritt 1.
    import asyncio
    async def _abgebrochene_heilung(self):
        print("INJIZIERT_HEILUNG", file=sys.stderr, flush=True)
        raise asyncio.CancelledError()
    main.Speicher.heile_verwaiste_paar_klassifikation = _abgebrochene_heilung
elif fehler == "trennen":
    # Startfehler im try, und das Aufraeumen scheitert vor dem Schliessen.
    from types import SimpleNamespace
    class _Client:
        async def trennen(self):
            raise RuntimeError("INJIZIERT_TRENNEN")
    async def _dienste(*a, **kw):
        return SimpleNamespace(client=_Client(), motor=None, ml_retrain_job=None)
    def _kaputter_ml_service(*a, **kw):
        raise RuntimeError("INJIZIERT_ML")
    main._baue_dienste = _dienste
    main._lade_ml_service = _kaputter_ml_service
else:
    raise SystemExit(f"unbekannte Injektion {fehler}")

sys.argv = ["bewaesserung.main"]
main.main()
'''


@pytest.mark.parametrize(
    ("fehler", "marke"),
    [
        # sqlite3-Fehler: main() faengt ihn nicht, Exit ueber den Traceback.
        ("migriere", "INJIZIERT_MIGRATION"),
        # RuntimeError: main() druckt "[FEHLER]" und ruft sys.exit(1).
        ("dienste", "INJIZIERT_DIENSTE"),
        # Abbruch in Startschritt 1 nach dem Oeffnen (vor dem try in ausfuehren).
        ("heilung", "INJIZIERT_HEILUNG"),
        # Aufraeumen scheitert an trennen(): Speicher muss trotzdem zu.
        ("trennen", "INJIZIERT_TRENNEN"),
        # Gate W12 G12-1: Startmodul fehlt -> Abbruch, bevor die DB offen ist.
        ("import", "bewaesserung.start_monitoring halted"),
    ],
)
def test_startfehler_nach_db_oeffnen_beendet_prozess_mit_fehlercode(
    tmp_path, fehler, marke,
):
    start_datei = tmp_path / "start.txt"
    proc = subprocess.Popen(
        [sys.executable, "-B", "-c", _KIND, str(SRC), str(tmp_path / "t0626.db"),
         fehler, str(start_datei)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={"BEWAESSERUNG_LOCK_PFAD": str(tmp_path / "t0626.pid"),
             "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
    )
    try:
        out, err = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        pytest.fail(f"Kindprozess haengt nach Startfehler ({fehler}): {err[-2000:]}")
    ende = time.time()

    assert "NETZ_VERBOTEN" not in err, err[-2000:]
    assert marke in err, f"injizierter Fehler kam nicht an: {err[-2000:]}"
    assert proc.returncode != 0, f"rc=0 nach Startfehler: {err[-2000:]}"
    dauer = ende - float(start_datei.read_text())
    assert dauer < 5, f"Prozessende {dauer:.1f} s nach Startbeginn"
