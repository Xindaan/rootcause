"""T-0152: Tests fuer die Akut-Sortierung im Auto-Loop.

Der Vertrag steckt in `KanalKandidat._sort_key`: `main.py` sortiert die
Kanal-Kandidaten inline mit genau diesem Schluessel
(`kandidaten.sort(key=lambda paar: paar[0]._sort_key())`). Bis 25.09.2026
liefen diese Tests ueber die Huelle `sortiere_kanal_kandidaten`, die seit dem
Aufraeumen vom 20.06. niemand mehr aufrief.
"""

from bewaesserung.hahn_scheduler import KanalKandidat


def _sortiert(kandidaten: list[KanalKandidat]) -> list[KanalKandidat]:
    return sorted(kandidaten, key=lambda k: k._sort_key())


def test_akut_score_kleiner_kommt_zuerst():
    """-3 (Sensor 32 % bei kritisch 35) ist akuter als +5."""
    a = KanalKandidat(kanal=1, akut_score=-3.0, konfig_index=1)
    b = KanalKandidat(kanal=2, akut_score=5.0, konfig_index=0)
    sortiert = _sortiert([b, a])
    assert sortiert[0].kanal == 1
    assert sortiert[1].kanal == 2


def test_zonen_ohne_score_kommen_ans_ende():
    """None-Score landet hinter allen mit Score (auch positiven)."""
    kein_msg = KanalKandidat(kanal=3, akut_score=None, konfig_index=0)
    positiv = KanalKandidat(kanal=2, akut_score=20.0, konfig_index=1)
    akut = KanalKandidat(kanal=1, akut_score=-1.0, konfig_index=2)
    sortiert = _sortiert([kein_msg, positiv, akut])
    assert [k.kanal for k in sortiert] == [1, 2, 3]


def test_konfig_index_als_tie_break():
    """Gleicher Score: Konfig-Reihenfolge entscheidet (deterministisch)."""
    a = KanalKandidat(kanal=10, akut_score=2.0, konfig_index=2)
    b = KanalKandidat(kanal=11, akut_score=2.0, konfig_index=0)
    c = KanalKandidat(kanal=12, akut_score=2.0, konfig_index=1)
    sortiert = _sortiert([a, b, c])
    assert [k.konfig_index for k in sortiert] == [0, 1, 2]


def test_alle_none_keine_aenderung_in_konfig_reihenfolge():
    """Backward-Compat: ohne Sensor-Daten = heutiges Verhalten."""
    a = KanalKandidat(kanal=1, akut_score=None, konfig_index=2)
    b = KanalKandidat(kanal=2, akut_score=None, konfig_index=0)
    c = KanalKandidat(kanal=3, akut_score=None, konfig_index=1)
    sortiert = _sortiert([a, b, c])
    assert [k.kanal for k in sortiert] == [2, 3, 1]
