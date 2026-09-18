"""T-0152: Akut-Sortierung fuer Auto-Loop-Bewaesserungs-Kandidaten.

Hintergrund: mehrere Zonen am gleichen Hahn-Cluster konkurrieren um das
Durchfluss-Budget (siehe T-0151). Ohne Priorisierung gewinnt im Auto-Loop
die Konfig-Reihenfolge zufaellig — Akut-Faelle koennen blockiert werden,
weil ein nicht-akuter Bedarf zuerst startet und das Budget belegt.

Loesung: vor dem Lock-Check eine deterministische Akut-Sortierung der
Bedarfs-Kandidaten. Score = (Sensor - feuchte_kritisch). Kleiner Score =
naeher am Welkepunkt = akuter. Zonen ohne Sensor-Wert bekommen einen
neutralen Score (kommen ans Ende).

Die Sortier-Logik ist absichtlich datenarm und deterministisch — sie
entscheidet NICHT, ob bewaessert wird, sondern nur in welcher Reihenfolge
die ohnehin als bedarfs-positiv erkannten Kandidaten dem Lock prozentuell
angeboten werden. Bei Cluster-Konflikt blockt T-0151, der Kandidat kommt
im naechsten Loop-Tick wieder dran.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KanalKandidat:
    """Ein Kanal mit Bedarfs-Entscheidung + Akut-Indikator.

    `akut_score`: niedriger = akuter. None = neutral (ans Ende sortiert).
    `konfig_index`: Zonen-Reihenfolge in der Konfig (deterministischer
    Tie-Break, identisch zur heutigen Reihenfolge).
    """
    kanal: int
    akut_score: float | None
    konfig_index: int

    def _sort_key(self) -> tuple[int, float, int]:
        # Tuple-Sortierung:
        # 1. ohne Score (None) ans Ende: 1 statt 0
        # 2. nach akut_score aufsteigend
        # 3. Konfig-Index als Tie-Break
        if self.akut_score is None:
            return (1, 0.0, self.konfig_index)
        return (0, float(self.akut_score), self.konfig_index)


def sortiere_kanal_kandidaten(
    kandidaten: list[KanalKandidat],
) -> list[KanalKandidat]:
    """Sortiert Kandidaten von akut nach unkritisch.

    Stabil, deterministisch. Konfig-Index als Tie-Break, damit der
    Verhalten ohne `hahn_cluster` exakt der heutige bleibt (Lock-Check
    laeuft eh durch -> akut_score keine Wirkung).
    """
    return sorted(kandidaten, key=lambda k: k._sort_key())
