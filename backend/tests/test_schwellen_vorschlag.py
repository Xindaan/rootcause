"""T-0049: Schwellen-Vorschlag-Tests."""

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import (
    DatenQuelle, MlAusschlussFenster, SensorMessung, ZonenKonfig,
)
from bewaesserung.schwellen_vorschlag import (
    _perzentil,
    berechne_vorschlag,
    berechne_vorschlaege_fuer_alle,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


# --- _perzentil ---


def test_perzentil_gibt_median_bei_50():
    werte = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert _perzentil(werte, 50) == pytest.approx(30.0)


def test_perzentil_10_und_90_auf_bekannten_werten():
    werte = [float(i) for i in range(1, 101)]  # 1..100
    assert _perzentil(werte, 10) == pytest.approx(10.9, abs=0.1)
    assert _perzentil(werte, 90) == pytest.approx(90.1, abs=0.1)


# --- berechne_vorschlag ---


def test_berechne_vorschlag_bei_zu_wenig_daten():
    werte = [50.0] * 50  # < 200
    mi, ma, basis, quelle = berechne_vorschlag(werte, 40, 75)
    assert basis == "zu_wenig_daten"
    assert mi is None and ma is None
    assert quelle == "empirisch"


def test_berechne_vorschlag_nutzt_perzentile_mit_puffer():
    # Synthetische Werte mit bekannter Verteilung: 0..99, 3x dupliziert = 300 Werte.
    werte = [float(i) for i in range(100)] * 3
    mi, ma, basis, quelle = berechne_vorschlag(werte, min_aktuell=30, max_aktuell=70)
    assert basis == "berechnet"
    # 10-Perzentil ~9.9 + 5 = ~15, 90-Perzentil ~89.1 - 5 = ~84
    assert mi == pytest.approx(15, abs=1)
    assert ma == pytest.approx(84, abs=1)
    assert quelle == "empirisch"


def test_berechne_vorschlag_lehnt_enge_spanne_ab():
    # Alle Werte nahe 60 → Perzentil-Spanne zu klein → min_v >= max_v
    werte = [60.0, 61.0, 59.0] * 100
    mi, ma, basis, _ = berechne_vorschlag(werte, min_aktuell=30, max_aktuell=70)
    assert basis == "zu_wenig_daten"


def test_optimum_dominiert_empirisch_zu_niedrig():
    """T-0050a: Empirisch 15/84 %, aber Pflanze will 65-80 %. Vorschlag muss nach oben.

    Werte sind 0..99 (also viele trockene Samples). Pflanze (Bambus)
    haette gern Feuchte 65-80. Vorschlag soll Pflanze respektieren.
    """
    werte = [float(i) for i in range(100)] * 3
    mi, ma, basis, quelle = berechne_vorschlag(
        werte, min_aktuell=30, max_aktuell=70,
        optimum_min=65.0, optimum_max=80.0,
    )
    assert basis == "berechnet"
    assert quelle == "optimum_dominiert"
    # Optimum_min 65 - 2 Puffer = 63, deutlich ueber empirisch 15
    assert mi == pytest.approx(63, abs=1)
    # Optimum_max 80 + 2 Puffer = 82, unter empirisch 84
    assert ma == pytest.approx(82, abs=1)


def test_optimum_wirkungslos_wenn_empirisch_schon_hoeher():
    """Wenn die Historie bereits im Optimum-Bereich liegt, kein Override."""
    # Werte zentriert um 70, Range 60..80
    werte = [60.0 + (i % 20) for i in range(300)]
    mi, ma, basis, quelle = berechne_vorschlag(
        werte, min_aktuell=40, max_aktuell=75,
        optimum_min=50.0, optimum_max=85.0,
    )
    assert basis == "berechnet"
    # Optimum_min 50 - 2 = 48 < empirisch 65ish → empirisch gewinnt
    # Optimum_max 85 + 2 = 87 > empirisch 77ish → empirisch gewinnt
    assert quelle == "empirisch"


# --- berechne_vorschlaege_fuer_alle mit DB ---


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "b.db"))
    _run(s.verbinden())
    yield s
    _run(s.schliessen())


def test_vorschlag_liefert_eintrag_pro_zone_auch_bei_leer(speicher):
    zonen = [
        ZonenKonfig(zone_id="rasen", name="Rasen", feuchte_schwelle_min=40, feuchte_schwelle_max=75),
        ZonenKonfig(zone_id="hecke", name="Hecke", feuchte_schwelle_min=35, feuchte_schwelle_max=70),
    ]
    jetzt = datetime(2026, 4, 20, 12, 0)
    ergebnisse = _run(berechne_vorschlaege_fuer_alle(speicher, zonen, jetzt=jetzt))
    assert len(ergebnisse) == 2
    # Beide Zonen haben keine Messungen → "zu_wenig_daten"
    assert all(e.basis == "zu_wenig_daten" for e in ergebnisse)
    assert all(e.n_messungen == 0 for e in ergebnisse)
    assert ergebnisse[0].min_aktuell == 40
    assert ergebnisse[1].min_aktuell == 35


def test_vorschlag_liefert_berechnet_mit_300_messungen(speicher):
    zonen = [
        ZonenKonfig(zone_id="rasen", name="Rasen", feuchte_schwelle_min=40, feuchte_schwelle_max=75),
    ]
    jetzt = datetime(2026, 4, 20, 12, 0)
    # 300 Messungen mit Verteilung 20..80 (mitten im plausiblen Feuchte-Bereich)
    for i, feuchte in enumerate([20.0 + (i % 60) for i in range(300)]):
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=jetzt - timedelta(minutes=i * 10),
            zone_id="rasen",
            geraet_id="test",
            boden_feuchte=feuchte,
            boden_temperatur=15.0,
            quelle=DatenQuelle.GARDENA,
        )))
    ergebnisse = _run(berechne_vorschlaege_fuer_alle(speicher, zonen, jetzt=jetzt))
    assert len(ergebnisse) == 1
    e = ergebnisse[0]
    assert e.basis == "berechnet"
    assert e.n_messungen == 300
    assert e.min_vorschlag is not None and e.max_vorschlag is not None
    # Mit Verteilung 20..79, 10/90-Perzentil ergibt ~26/73 → +5/-5 → ~31/68
    assert 25 <= e.min_vorschlag <= 35
    assert 63 <= e.max_vorschlag <= 73


def test_ml_ausschluss_fenster_filtert_initial_null_werte(speicher):
    """Bug-Fix: bambuswald_yogaraum hatte 94 x 0.0% am 6./7.4. (Sensor
    nicht eingeschlemmt) → 10-Perzentil wurde 0 → min_vorschlag 5.
    Mit `ml_ausschluss_fenster` werden diese Zeilen vor der Perzentil-
    Berechnung entfernt, der Vorschlag ist wieder sinnvoll.
    """
    zonen = [
        ZonenKonfig(zone_id="bambus", name="Bambus",
                    feuchte_schwelle_min=65, feuchte_schwelle_max=80),
    ]
    jetzt = datetime(2026, 4, 20, 12, 0)
    # 100 "schlechte" Messungen mit 0.0% (waere pathologisch)
    for i in range(100):
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=datetime(2026, 4, 6, 18, 0) + timedelta(minutes=i),
            zone_id="bambus", geraet_id="test",
            boden_feuchte=0.0, boden_temperatur=15.0,
            quelle=DatenQuelle.GARDENA,
        )))
    # 250 valide Messungen spaeter
    for i in range(250):
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=jetzt - timedelta(hours=i),
            zone_id="bambus", geraet_id="test",
            boden_feuchte=60.0 + (i % 20),  # 60-79
            boden_temperatur=15.0, quelle=DatenQuelle.GARDENA,
        )))

    # Ohne Ausschluss → 0-Werte verzerren 10-Perzentil nach unten
    ohne = _run(berechne_vorschlaege_fuer_alle(speicher, zonen, jetzt=jetzt))
    # 10-Perzentil ist ca. 0 (weil 100 von 350 Werten = 0.0), + 5 Puffer = 5
    assert ohne[0].min_vorschlag is not None
    assert ohne[0].min_vorschlag < 10, (
        f"Erwartet schlechter Vorschlag, got {ohne[0].min_vorschlag}"
    )

    # Mit Ausschluss → 0-Werte sind raus, Vorschlag aus validen 60-79
    ausschluss = [MlAusschlussFenster(
        zone_id="bambus",
        von=datetime(2026, 4, 6, 0, 0),
        bis=datetime(2026, 4, 7, 23, 59),
        grund="Initial Setup",
    )]
    mit = _run(berechne_vorschlaege_fuer_alle(
        speicher, zonen, jetzt=jetzt,
        ml_ausschluss_fenster=ausschluss,
    ))
    # 10-Perzentil jetzt ca. 62, + 5 = 67 (oder aehnlich plausibel)
    assert mit[0].min_vorschlag is not None
    assert mit[0].min_vorschlag > 55, (
        f"Mit Ausschluss sollte Min plausibel hoch sein, got {mit[0].min_vorschlag}"
    )
