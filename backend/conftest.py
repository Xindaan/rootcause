"""Root-conftest: Umgebung setzen, BEVOR irgendetwas importiert wird.

T-0517: Die ML-Tests trainieren LightGBM auf Mini-Datensaetzen (400-600
Zeilen Synthetik). LightGBM oeffnet dafuer per Default einen OpenMP-Pool
ueber alle Kerne (hier 10). Bei dieser Datenmenge kostet der Pool-Aufbau
plus die Barrier-Synchronisation ein Vielfaches des eigentlichen
Trainings -- gemessen an der vollen Suite:

    OMP-Default (10 Threads):   1801 passed in 103.06s
    OMP_NUM_THREADS=1:          1801 passed in  34.17s

Der Effekt ist nicht nur Tempo, sondern Stabilitaet. Unter Fremdlast
(40 konkurrierende Prozesse) spinnen die OMP-Threads in Barrieren statt
zu rechnen; dieselbe Datei brauchte dann 43,6 s mit Pool gegen 21,8 s
mit einem Thread, und die Systemzeit fiel von 8,7 s auf 0,37 s. Mit
einem Thread ist die Suite gegen Last auf der Maschine praktisch immun
-- genau das, was sie sein muss, damit sie vor jedem Commit auch
wirklich laeuft.

Betrifft AUSSCHLIESSLICH die Testlaeufe: der Produktivcode setzt
`num_threads` nicht und bleibt hier unangetastet.

KORREKTUR 07.08. (T-0518, gemessen): hier stand, der echte Retrain nutze
"weiterhin alle Kerne, dort sind die Datensaetze gross genug, dass sich
das lohnt". Das war aus dem Code hergeleitet, nie gemessen, und ist
falsch. Mitschnitt der kumulierten CPU-Zeit ueber ein volles
Retrain-Fenster: **1,36 Kerne** ueber 885 s. Ob der Pool sich im
Produktivbetrieb lohnt, ist damit offen und nicht mehr die Begruendung
dafuer, ihn dort zu lassen. Fuer die Testlaeufe aendert das nichts --
deren Nutzen ist direkt gemessen (103,06 s gegen 34,17 s).

`setdefault`: eine von aussen gesetzte Variable gewinnt, damit man den
Pool fuer eine gezielte Messung wieder aufdrehen kann, z. B.

    OMP_NUM_THREADS=10 ../.venv/bin/python -m pytest -q

T-0608 (Gesamtreview AT-4): Zeitzone der Testlaeufe hart auf Europe/Berlin.
Der Produktivcode rechnet bewusst in NAIVER LOKALZEIT (Projektkonvention):
DHS-/FYTA-/API-Zeitstempel in UTC werden per `.astimezone()` in die
Host-Zone geholt und naiv gespeichert, die Wetter-Abrufe fragen
`timezone=Europe/Berlin` an, und die Sommerzeit-Pruefung (T-0566) ist nur
in einer Zone mit Zeitumstellung sinnvoll. Die Tests schreiben deshalb
Berliner Wanduhrzeiten fest. Ohne Pinning war die Suite nur auf einem
Berliner Rechner gruen: unter TZ=UTC 18 Fehlschlaege, unter
America/Los_Angeles 18, unter Pacific/Kiritimati 19 (25.09.2026), alle aus
dieser Testannahme, kein Codefehler.

Hart zugewiesen, NICHT `setdefault`: ein von aussen gesetztes TZ=UTC (CI,
Reise-Laptop) soll die Suite gerade nicht umschalten. `time.tzset()` macht
die Zuweisung im laufenden Prozess wirksam; Unterprozesse (Feature-Bau
T-0403/T-0539) erben sie ueber `os.environ`. Wer das Verhalten unter einer
anderen Zone pruefen will, muss das im Test selbst tun (TZ setzen,
`time.tzset()`, danach zuruecksetzen).
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("OMP_NUM_THREADS", "1")

# T-0608: siehe Docstring. Vor jedem Import aus `bewaesserung`.
TEST_ZEITZONE = "Europe/Berlin"
os.environ["TZ"] = TEST_ZEITZONE
if hasattr(time, "tzset"):
    time.tzset()


# Tests, die die PRIVATE Live-Konfig (`config/default.yaml`) pruefen, tragen
# `@pytest.mark.live_config`. Im oeffentlichen Snapshot gibt es die Datei
# nicht (nur die Beispiel-Konfig); dort werden sie uebersprungen statt aus
# einem Grund zu scheitern, der mit ihrer Aussage nichts zu tun hat. Privat
# laufen sie unveraendert. Ein zentraler Marker statt je eines Waechters pro
# Datei: die Klasse ist bis 09/2026 dreimal aufgetreten.
_LIVE_CONFIG = (
    __import__("pathlib").Path(__file__).resolve().parents[1] / "config" / "default.yaml"
)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live_config: prueft die private config/default.yaml"
    )


def pytest_collection_modifyitems(config, items):
    if _LIVE_CONFIG.exists():
        return
    import pytest

    grund = pytest.mark.skip(
        reason="prueft die private Live-Config; im oeffentlichen Snapshot nicht vorhanden"
    )
    for item in items:
        if "live_config" in item.keywords:
            item.add_marker(grund)
