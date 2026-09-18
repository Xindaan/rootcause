"""T-0544: die Plateau-Erkennung war richtungsblind.

Geprueft wurde nur `max - min`. Ein monotoner Anstieg und ein symmetrisches
Schwanken gleicher Amplitude waren damit ununterscheidbar -- fuer die Frage
"ist der Boden im Gleichgewicht" ist das aber der ganze Unterschied.

**Der Realfall** (kasten_4, 20.08.2026, FYTA-Sensor mit 1-pp-Aufloesung):
Fenster 12-24 h nach Regen, n=48, min 75, max 81. Der Alt-Test `> 6` liess
die Spanne 6 mit null Marge durch. Der Verlauf stieg in diesem Fenster
monoton und lief danach auf 81 weiter; der gespeicherte Median 79 war die
Momentaufnahme eines laufenden Anstiegs.

Die Werte in `KASTEN_4_20_08` sind die ECHTEN Messwerte aus der Produktiv-DB,
nicht nachgebaut.
"""

from __future__ import annotations

import random

from bewaesserung.kalibrierung import _ist_gerichtet, _netto_drift
from bewaesserung.modelle import AUFLOESUNG_PP_DEFAULT, aufloesung_pp


# Echte Messwerte, chronologisch, 20.08.2026 07:00-19:00
KASTEN_4_20_08 = [
    75.0, 75, 75, 75, 75, 75, 76, 75, 76, 75, 76, 75, 75, 75, 76, 75,
    75, 75, 76, 76, 76, 77, 76, 77, 77, 77, 77, 77, 77, 77, 77, 77,
    78, 77, 78, 78, 78, 78, 78, 78, 78, 79, 79, 78, 78, 80, 81, 80,
]

FYTA = aufloesung_pp("fyta")        # 1.0
GARDENA = aufloesung_pp("gardena")  # 5.0


# --------------------------------------------------------------------------
# Der Realfall
# --------------------------------------------------------------------------

def test_der_realfall_faellt_jetzt_durch():
    """DER Regression-Test: genau dieses Fenster hat die 79,0 erzeugt."""
    assert _ist_gerichtet(KASTEN_4_20_08, FYTA) is True


def test_realfall_haette_die_alte_spannenpruefung_bestanden():
    """Belegt, dass der alte Test hier nichts gefunden HAETTE -- sonst waere
    unklar, ob die neue Pruefung ueberhaupt noetig ist."""
    spanne = max(KASTEN_4_20_08) - min(KASTEN_4_20_08)
    plateau_max_delta = 3.0  # config/default.yaml
    assert spanne == 6.0
    assert not (spanne > 2 * plateau_max_delta), (
        "6 > 6 ist falsch -- der Alt-Test liess das Fenster durch."
    )


def test_realfall_drift_hat_marge_zur_schwelle():
    """Kein Gleichstand mehr: die Entscheidung faellt mit Abstand, nicht auf
    der Kippe wie beim Alt-Test."""
    assert abs(_netto_drift(KASTEN_4_20_08)) == 3.0
    assert 3.0 > FYTA * 2, "Drift sollte deutlich ueber der Aufloesung liegen"


# --------------------------------------------------------------------------
# Kontrollen: was ein Plateau bleiben MUSS
# --------------------------------------------------------------------------

def test_flaches_fenster_mit_rauschen_bleibt_plateau():
    random.seed(7)
    werte = [78 + random.choice([-1, 0, 0, 1]) for _ in range(48)]
    assert _ist_gerichtet(werte, FYTA) is False


def test_einzelne_gardena_stufe_ist_kein_trend():
    """Auf einem 5-pp-Raster ist ein einzelner Schritt die kleinste
    darstellbare Bewegung. Wer den verwirft, verwirft jedes Gardena-Plateau,
    das zufaellig eine Bin-Grenze kreuzt."""
    werte = [75.0] * 20 + [80.0] * 20
    assert _netto_drift(werte) == 5.0
    assert _ist_gerichtet(werte, GARDENA) is False


def test_pendeln_um_die_bin_grenze_ist_kein_trend():
    werte = [75.0, 80, 75, 80, 75, 80, 75, 80] * 3
    assert _ist_gerichtet(werte, GARDENA) is False


def test_konstantes_fenster_ist_kein_trend():
    assert _ist_gerichtet([80.0] * 10, FYTA) is False


def test_zu_wenige_werte_kippen_nichts():
    assert _netto_drift([80.0]) == 0.0
    assert _ist_gerichtet([80.0], FYTA) is False


# --------------------------------------------------------------------------
# Richtung und Robustheit
# --------------------------------------------------------------------------

def test_abfall_wird_genauso_erkannt_wie_anstieg():
    """Ein abtrocknendes Fenster ist genauso wenig ein Gleichgewicht."""
    fallend = list(reversed(KASTEN_4_20_08))
    assert _netto_drift(fallend) == -3.0
    assert _ist_gerichtet(fallend, FYTA) is True


def test_einzelner_ausreisser_am_rand_kippt_kein_plateau():
    """Deshalb Median der Haelften statt erstem gegen letztem Wert."""
    werte = [95.0] + [78.0] * 46 + [60.0]
    assert _ist_gerichtet(werte, FYTA) is False


def test_gardena_massstab_ist_lockerer_als_fyta():
    """Dieselbe Bewegung, zwei Sensortypen: auf dem groben Raster ist sie
    Quantisierung, auf dem feinen ein Trend."""
    werte = [75.0] * 24 + [78.0] * 24
    assert _ist_gerichtet(werte, FYTA) is True
    assert _ist_gerichtet(werte, GARDENA) is False


def test_unbekannte_quelle_faellt_auf_die_grobe_annahme():
    """Fail-safe: im Zweifel schweigen, nicht verwerfen."""
    assert aufloesung_pp(None) == AUFLOESUNG_PP_DEFAULT
    assert aufloesung_pp("irgendwas") == AUFLOESUNG_PP_DEFAULT


# --------------------------------------------------------------------------
# Negativprobe
# --------------------------------------------------------------------------

def test_negativprobe_ohne_richtungspruefung_kaeme_die_79_zurueck():
    """Baut den alten Zustand nach: nur Spannweite, keine Richtung. Dann
    passiert der Realfall wieder -- genau das soll oben verhindert werden."""
    def alt_ist_plateau(werte, plateau_max_delta=3.0):
        return (max(werte) - min(werte)) <= 2 * plateau_max_delta

    assert alt_ist_plateau(KASTEN_4_20_08) is True, (
        "Negativprobe: der Alt-Test muss den Realfall durchlassen, sonst "
        "testet test_der_realfall_faellt_jetzt_durch nichts."
    )
