"""T-0539: Feature-Bau des Response-Modells in einem eigenen Prozess.

**Warum es das gibt -- gemessen, nicht hergeleitet.** `_baue_response_df_sync`
lag seit T-0403 hinter `asyncio.to_thread` und blockierte den Entscheidungsloop
trotzdem. Auswertung ueber 18 Wartungsfenster vom 05.08. bis 13.08.2026
(`docs/analyse/t0539_loop_stall/`): der laengste ueberlappende
Entscheidungszyklus dauerte im Median **111 % der Retrain-Laufzeit**, Spanne
79-125 %, **ohne eine einzige Ausnahme**. Der Job blockiert also praktisch
seine ganze Laufzeit.

**Der Anteil ist die Diagnose.** Derselbe Lauf zeigt fuer den Feuchte-Retrain
nur ~35 % -- der besteht aus GIL-haltendem Feature-Bau PLUS GIL-freiem
LightGBM-Fit. Der Response-Retrain ist fast nur Feature-Bau: verschachtelte
Schleifen ueber Kanaele, Pulse und Zonen mit `timedelta`-Arithmetik, also
Zeile-fuer-Zeile-Python. Solcher Code haelt den GIL durchgehend, und kein
Executor aendert daran etwas -- nur ein eigener Interpreter tut es.

**Die Kontrollbedingung, die `backup` ausschliesst:** zwei der 18 Fenster
liefen ohne Backup und stockten genauso (79,5 % / 111,3 %); `backup` allein
kostet 0,5-20 s. Es ist keine DB-Kontention.

Aufbau bewusst identisch zu `ml/feature_prozess.py` (Feuchte-Pfad) -- gleiche
Argumente, gleiche Read-Only-Regel, gleiche Pickle-Uebergabe. Zwei Muster fuer
dieselbe Sache waeren die zweite Wahrheit, die dieses Projekt vermeidet.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path


async def _baue(von: datetime, bis: datetime, ziel: Path, konfig_pfad: Path) -> int:
    from bewaesserung.konfig import lade_konfig
    from bewaesserung.ml.response_features import erstelle_response_features
    from bewaesserung.speicher import Speicher

    konfig = lade_konfig(konfig_pfad)
    # Read-only wie im Feuchte-Pfad (T-0403/T-0162): der Subprozess liest nur,
    # und eine 365-Tage-Lesung gegen den schreibenden Hauptprozess wuerde sonst
    # `database is locked` provozieren. Schema-Setup und Migration gehoeren dem
    # Parent.
    speicher = Speicher(konfig.speicher.db_pfad)
    await speicher.verbinden(modus="ro")
    try:
        df = await erstelle_response_features(
            speicher, konfig, von=von, bis=bis,
        )
    finally:
        # Ohne schliessen() bleibt der aiosqlite-Thread stehen und der
        # Interpreter kommt nie zum Shutdown -- der Parent wartet dann bis in
        # sein Timeout, obwohl die Arbeit laengst fertig ist.
        await speicher.schliessen()
    df.to_pickle(ziel)
    print(f"zeilen={len(df)}")
    return len(df)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Baut die Trainingsdaten des Response-Modells.",
    )
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
