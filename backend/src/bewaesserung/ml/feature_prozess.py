"""T-0403: Feature-Bau in einem eigenen Prozess.

**Warum ein Prozess und kein Thread.** Der Feature-Bau lag seit T-0064 hinter
`asyncio.to_thread` -- und war trotzdem der Blockierer. Gemessen am 13.08.2026
ueber das echte 60-Tage-Fenster: `erstelle_trainingsdaten` lief 945 s, und ein
mitlaufender Ticker, der alle 10 ms drankommen wollte, bekam nur 11 % seiner
Wakeups; die Summe der Luecken ueber 100 ms betrug **82 % der Laufzeit**. Das
Stack-Sample zeigte den Main-Thread zu 52 % in `take_gil`.

Ein Thread isoliert nur Code, der den GIL abgibt. LightGBM tut das (T-0518 mass
0,1 %), die blockweise DB-Lesephase seit T-0514 auch (1,53 s fuer 320.000
Zeilen). Der DataFrame-Bau dagegen ist Zeile-fuer-Zeile-Python: pro Messung
mehrere Rueckwaerts-/Vorwaerts-Scans mit `timedelta`-Arithmetik, bei 65.000
Messungen also zweistellige Millionen Datetime-Operationen. Solcher Code haelt
den GIL durchgehend, und kein Executor der Welt aendert daran etwas -- nur ein
eigener Interpreter tut es.

**Warum nur der DF-Bau und nicht der ganze Retrain.** Das Training liegt
bereits hinter `to_thread` und blockiert nachweislich nicht. Und das
MAE-Gate, die Archiv-Rotation und der Deploy sollen im Parent bleiben: dort
liegt die Entscheidung, welches Modell scharf geht, und die gehoert nicht in
einen Wegwerf-Prozess.

**Uebergabe per Pickle, nicht Parquet.** `pyarrow` ist nicht installiert, und
eine neue Abhaengigkeit fuer eine prozessinterne Zwischenablage waere
unverhaeltnismaessig. Die Datei wird von genau diesem Programm geschrieben und
vom Elternprozess derselben Installation gelesen; sie verlaesst die Maschine
nie. Der Pfad kommt vom Parent (Temp-Verzeichnis), nicht von aussen.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path


async def _baue(von: datetime, bis: datetime, ziel: Path, konfig_pfad: Path) -> int:
    from bewaesserung.konfig import lade_konfig
    from bewaesserung.ml.features import FeatureExtraktor
    from bewaesserung.speicher import Speicher

    konfig = lade_konfig(konfig_pfad)
    # Read-only wie die ML-CLI (T-0162): der Subprozess liest nur, und eine
    # 60-Tage-Lesung gegen den schreibenden Hauptprozess wuerde sonst
    # `database is locked` provozieren. Schema-Setup und Migration entfallen
    # damit ebenfalls -- die gehoeren dem Parent.
    speicher = Speicher(konfig.speicher.db_pfad)
    await speicher.verbinden(modus="ro")
    try:
        extraktor = FeatureExtraktor(speicher, konfig)
        # Dieselben dynamischen Wartungs-Fenster wie im Elternprozess
        # (T-0228 Stufe 2c). Ohne sie traeniert der Subprozess auf Daten,
        # die der Parent bewusst ausschliesst -- eine zweite Wahrheit.
        try:
            fenster = await speicher.hole_wartungs_fenster(nur_offen=False)
            extraktor.setze_wartungs_fenster(fenster)
        except Exception as exc:  # noqa: BLE001
            print(f"wartungs_fenster_load_fehler: {exc}", file=sys.stderr)
        df = await extraktor.erstelle_trainingsdaten(von=von, bis=bis)
    finally:
        # Ohne schliessen() bleibt der aiosqlite-Thread und der Interpreter
        # kommt nie zum Shutdown -- der Parent wartet dann bis in sein
        # Timeout, obwohl die Arbeit laengst fertig ist.
        await speicher.schliessen()
    df.to_pickle(ziel)
    print(f"zeilen={len(df)}")
    return len(df)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Baut die ML-Trainingsdaten.")
    p.add_argument("--von", required=True)
    p.add_argument("--bis", required=True)
    p.add_argument("--ziel", required=True)
    p.add_argument("--konfig", required=True)
    args = p.parse_args(argv)
    asyncio.run(_baue(
        datetime.fromisoformat(args.von),
        datetime.fromisoformat(args.bis),
        Path(args.ziel),
        Path(args.konfig),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
