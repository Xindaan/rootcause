"""T-0517: Regression-Guard fuer die Thread-Deckelung der Suite.

Die ML-Tests trainieren LightGBM auf 400-600 Zeilen Synthetik. Ohne
Deckelung oeffnet LightGBM dafuer einen OpenMP-Pool ueber alle Kerne;
bei dieser Datenmenge kostet die Barrier-Synchronisation ein Vielfaches
des Trainings (volle Suite: 103 s ungedeckelt gegen 34 s gedeckelt) und
explodiert unter Fremdlast auf der Maschine.

Der Guard faengt die Fehlerklasse ab, nicht den Einzelfall: verschwindet
`backend/conftest.py` oder wird die Zuweisung dort entfernt, wird die
Suite still wieder langsam und lastabhaengig. "Still" ist das Problem --
eine langsame Suite schlaegt nicht fehl, sie wird nur nicht mehr
gefahren, und dann faellt die Definition of Done aus.

Der Test prueft NICHT auf einen bestimmten Wert: eine bewusste
Ueberschreibung von aussen (`OMP_NUM_THREADS=10 pytest ...`, um den
Effekt nachzumessen) bleibt erlaubt.
"""
from __future__ import annotations

import os


def test_t0517_omp_thread_limit_ist_gesetzt():
    """`backend/conftest.py` muss `OMP_NUM_THREADS` gesetzt haben."""
    wert = os.environ.get("OMP_NUM_THREADS")
    assert wert is not None, (
        "OMP_NUM_THREADS ist nicht gesetzt -- backend/conftest.py fehlt "
        "oder setzt die Variable nicht mehr. Ohne die Deckelung faellt "
        "die Suite auf ~103 s zurueck und wird unter Fremdlast um ein "
        "Vielfaches langsamer (T-0517)."
    )
    assert wert.isdigit() and int(wert) >= 1, (
        f"OMP_NUM_THREADS={wert!r} ist keine positive Ganzzahl."
    )
