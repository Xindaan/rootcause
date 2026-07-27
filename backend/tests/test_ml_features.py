"""Tests fuer ML Feature Engineering.

Testet FeatureExtraktor mit Mock-Daten (in-memory SQLite).
"""

import math
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from bewaesserung.modelle import (
    BalkonKonfig,
    DatenQuelle,
    GardenaKonfig,
    GesamtKonfig,
    MlAusschlussFenster,
    SensorMessung,
    SpeicherKonfig,
    StandortKonfig,
    VentilAktion,
    VentilEreignis,
    WetterKonfig,
    WetterStandortKonfig,
    WetterStunde,
    ZonenKonfig,
    ZonenModus,
    Ausloser,
)
from bewaesserung.speicher import Speicher

# ML-Deps optional — Test ueberspringen wenn nicht installiert
pytest.importorskip("pandas")
pytest.importorskip("numpy")

from bewaesserung.ml.features import (
    FeatureExtraktor,
    _berechne_regen_faktor,
    _vpd_kpa,
    _wind_match_ordinal,
)
from bewaesserung.ml.evaluation import (
    BaselineVorhersage,
    mae,
    rmse,
    r2,
)

import numpy as np


# --- Fixtures ---

@pytest.fixture
def konfig() -> GesamtKonfig:
    """Minimale Testkonfiguration mit 2 Zonen."""
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="test"),
        zonen=[
            ZonenKonfig(
                zone_id="garten_test",
                name="Testgarten",
                modus=ZonenModus.AUTOMATIK,
                feuchte_schwelle_min=30,
                feuchte_schwelle_max=60,
                ist_topf=False,
                ist_indoor=False,
            ),
            ZonenKonfig(
                zone_id="topf_test",
                name="Testtopf",
                modus=ZonenModus.MONITORING,
                feuchte_schwelle_min=25,
                feuchte_schwelle_max=45,
                ist_topf=True,
                ist_indoor=False,
            ),
            ZonenKonfig(
                zone_id="indoor_test",
                name="Testpflanze Indoor",
                modus=ZonenModus.MONITORING,
                feuchte_schwelle_min=22,
                feuchte_schwelle_max=40,
                ist_topf=True,
                ist_indoor=True,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[
                WetterStandortKonfig(id="test_ort", breite=52.5, laenge=13.4),
            ]
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten",
                name="Testgarten",
                wetter_standort="test_ort",
                zonen=["garten_test"],
            ),
            StandortKonfig(
                standort_id="suedbalkon",
                name="Suedbalkon",
                wetter_standort="test_ort",
                zonen=["topf_test"],
            ),
            StandortKonfig(
                standort_id="wohnung",
                name="Wohnung",
                wetter_standort="test_ort",
                zonen=["indoor_test"],
            ),
        ],
        balkon_ausrichtung={
            "suedbalkon": BalkonKonfig(
                himmelsrichtung="sued",
                regen_wind=["sued", "ost"],
                sonnig=True,
            ),
        },
    )


@pytest.fixture
async def speicher():
    """In-Memory SQLite Speicher."""
    s = Speicher(":memory:")
    await s.verbinden()
    yield s
    await s.schliessen()


async def _fuege_messreihe_ein(
    speicher: Speicher,
    zone_id: str,
    start: datetime,
    stunden: int,
    feuchte_start: float = 50.0,
    feuchte_drift: float = -0.5,  # % pro Stunde
    quelle: DatenQuelle = DatenQuelle.GARDENA,
):
    """Erzeugt stuendliche Messungen mit linearem Feuchte-Drift."""
    for h in range(stunden):
        t = start + timedelta(hours=h)
        feuchte = feuchte_start + feuchte_drift * h
        feuchte = max(0, min(100, feuchte))
        await speicher.speichere_messung(SensorMessung(
            zeitstempel=t,
            zone_id=zone_id,
            boden_feuchte=feuchte,
            boden_temperatur=18.0,
            quelle=quelle,
        ))


async def _fuege_wetter_ein(
    speicher: Speicher,
    abfrage_zeit: datetime,
    stunden: int = 48,
    standort_id: str = "test_ort",
    niederschlag_mm: float = 0.0,
    wind_kmh: float = 10.0,
    wind_richtung_grad: float = 180.0,
    et0_mm: float = 0.1,
    temperatur: float = 20.0,
    luftfeuchte_prozent: float | None = None,
):
    """Erzeugt Wetter-Vorhersage-Stunden."""
    stunden_liste = []
    for h in range(stunden):
        stunden_liste.append(WetterStunde(
            zeitstempel=abfrage_zeit + timedelta(hours=h),
            temperatur=temperatur,
            niederschlag_mm=niederschlag_mm,
            wind_kmh=wind_kmh,
            wind_richtung_grad=wind_richtung_grad,
            et0_mm=et0_mm,
            luftfeuchte_prozent=luftfeuchte_prozent,
        ))
    await speicher.speichere_wetter(abfrage_zeit, stunden_liste, standort_id)


# --- Tests: Feature-Extraktion ---

async def test_feature_extraktion_grundstruktur(speicher, konfig):
    """Feature-DataFrame hat erwartete Spalten und Zeilen."""
    start = datetime(2026, 4, 10, 0, 0)
    # 48h Daten → Features fuer mittlere 24h (Puffer fuer Lags + Ziel)
    await _fuege_messreihe_ein(speicher, "garten_test", start, 72)
    await _fuege_wetter_ein(speicher, start, standort_id="test_ort")

    extraktor = FeatureExtraktor(speicher, konfig)
    von = start + timedelta(hours=24)
    bis = start + timedelta(hours=48)
    df = await extraktor.erstelle_trainingsdaten(von, bis)

    assert not df.empty
    # Erwartete Spalten pruefen
    assert "zone_id" in df.columns
    assert "boden_feuchte_aktuell" in df.columns
    assert "feuchte_t_minus_1h" in df.columns
    assert "feuchte_t_minus_24h" in df.columns
    assert "feuchte_diff_1h" in df.columns
    assert "feuchte_rolling_6h" in df.columns
    assert "feuchte_trend_6h" in df.columns
    assert "stunden_seit_letzter_bewaesserung" in df.columns
    assert "niederschlag_summe_6h" in df.columns
    assert "niederschlag_roh_6h" in df.columns
    assert "effektiver_niederschlag_6h" in df.columns
    assert "wind_match" in df.columns
    assert "stunde_sin" in df.columns
    assert "tag_im_jahr_sin" in df.columns
    assert "ist_topf" in df.columns
    assert "ist_indoor" in df.columns
    assert "ziel_feuchte_6h" in df.columns
    assert "ziel_feuchte_24h" in df.columns


async def test_wind_match_ordinal(konfig):
    """wind_match Ordinalwerte sind korrekt."""
    indoor = konfig.zonen[2]  # indoor_test
    topf = konfig.zonen[1]    # topf_test (suedbalkon)
    garten = konfig.zonen[0]  # garten_test

    assert _wind_match_ordinal(indoor, "wohnung", konfig) == 0
    assert _wind_match_ordinal(topf, "suedbalkon", konfig) == 2
    assert _wind_match_ordinal(garten, "garten", konfig) == 3


async def test_regen_faktor(konfig):
    """Regen-Faktor korrekt fuer verschiedene Szenarien."""
    indoor = konfig.zonen[2]
    topf = konfig.zonen[1]
    garten = konfig.zonen[0]

    # Indoor → 0.0
    assert _berechne_regen_faktor(180, indoor, "wohnung", konfig) == 0.0
    # Garten → 1.0
    assert _berechne_regen_faktor(180, garten, "garten", konfig) == 1.0
    # Suedbalkon bei Suedwind → 1.0 (passender Wind)
    assert _berechne_regen_faktor(180, topf, "suedbalkon", konfig) == 1.0
    # Suedbalkon bei Nordwind → 0.2 (falscher Wind)
    assert _berechne_regen_faktor(0, topf, "suedbalkon", konfig) == 0.2


async def test_lag_features_korrekt(speicher, konfig):
    """Lag-Features zeigen korrekte historische Werte."""
    start = datetime(2026, 4, 10, 0, 0)
    # Feuchte sinkt von 60% um 1%/h
    await _fuege_messreihe_ein(
        speicher, "garten_test", start, 50,
        feuchte_start=60.0, feuchte_drift=-1.0
    )

    extraktor = FeatureExtraktor(speicher, konfig)
    # Zeitpunkt: start + 25h → Feuchte sollte ~35% sein
    von = start + timedelta(hours=25)
    bis = start + timedelta(hours=25, minutes=30)
    df = await extraktor.erstelle_trainingsdaten(von, bis)

    assert len(df) >= 1
    zeile = df.iloc[0]
    # Aktuelle Feuchte bei h=25: 60 - 25 = 35
    assert abs(zeile["boden_feuchte_aktuell"] - 35.0) < 1.0
    # Lag 1h: bei h=24: 60 - 24 = 36
    assert zeile["feuchte_t_minus_1h"] is not None
    assert abs(zeile["feuchte_t_minus_1h"] - 36.0) < 1.5
    # Differenz 1h sollte ca. -1.0 sein
    assert zeile["feuchte_diff_1h"] is not None
    assert abs(zeile["feuchte_diff_1h"] - (-1.0)) < 0.5


async def test_bewaesserungs_features(speicher, konfig):
    """Bewaesserungs-Features korrekt berechnet."""
    start = datetime(2026, 4, 10, 0, 0)
    await _fuege_messreihe_ein(speicher, "garten_test", start, 50)

    # Ventil-Event 3h vor Messzeitpunkt
    await speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=start + timedelta(hours=22),
        zone_id="garten_test",
        ventil_id="test_ventil",
        aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=600,
        ausloser=Ausloser.AUTOMATIK,
    ))

    extraktor = FeatureExtraktor(speicher, konfig)
    von = start + timedelta(hours=25)
    bis = start + timedelta(hours=25, minutes=30)
    df = await extraktor.erstelle_trainingsdaten(von, bis)

    assert len(df) >= 1
    zeile = df.iloc[0]
    # 3h seit letzter Bewaesserung
    assert zeile["stunden_seit_letzter_bewaesserung"] is not None
    assert abs(zeile["stunden_seit_letzter_bewaesserung"] - 3.0) < 0.5
    assert zeile["letzte_bewaesserung_dauer_s"] == 600
    assert zeile["bewaesserung_summe_6h"] == 600
    assert zeile["bewaesserung_anzahl_24h"] == 1


async def test_bewaesserungs_features_paarweise_koppelt_dauer(speicher, konfig):
    """Gardena-Paar (OEFFNEN ohne Dauer + SCHLIESSEN mit Dauer) koppelt korrekt."""
    start = datetime(2026, 4, 10, 0, 0)
    await _fuege_messreihe_ein(speicher, "garten_test", start, 50)

    # OEFFNEN ohne Dauer (wie Gardena-WebSocket es liefert)
    await speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=start + timedelta(hours=22),
        zone_id="garten_test",
        ventil_id="test_ventil",
        aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=0,
        ausloser=Ausloser.AUTOMATIK,
    ))
    # SCHLIESSEN mit Dauer 420s, kurz danach
    await speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=start + timedelta(hours=22, minutes=7),
        zone_id="garten_test",
        ventil_id="test_ventil",
        aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=420,
        ausloser=Ausloser.AUTOMATIK,
    ))

    extraktor = FeatureExtraktor(speicher, konfig)
    von = start + timedelta(hours=25)
    bis = start + timedelta(hours=25, minutes=30)
    df = await extraktor.erstelle_trainingsdaten(von, bis)

    zeile = df.iloc[0]
    # stunden_seit_letzter muss an OEFFNEN-Zeit haengen (22:00), nicht an SCHLIESSEN (22:07)
    assert abs(zeile["stunden_seit_letzter_bewaesserung"] - 3.0) < 0.5
    # Dauer kommt aus dem gepaarten SCHLIESSEN
    assert zeile["letzte_bewaesserung_dauer_s"] == 420


async def test_bewaesserungs_features_verwaistes_schliessen_ignoriert(speicher, konfig):
    """Verwaistes SCHLIESSEN ohne vorheriges OEFFNEN darf Dauer nicht falsch zuordnen."""
    start = datetime(2026, 4, 10, 0, 0)
    await _fuege_messreihe_ein(speicher, "garten_test", start, 50)

    # Nur SCHLIESSEN (z.B. Phantom-Event, Backfill-Luecke)
    await speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=start + timedelta(hours=22),
        zone_id="garten_test",
        ventil_id="test_ventil",
        aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=999,
        ausloser=Ausloser.AUTOMATIK,
    ))

    extraktor = FeatureExtraktor(speicher, konfig)
    von = start + timedelta(hours=25)
    bis = start + timedelta(hours=25, minutes=30)
    df = await extraktor.erstelle_trainingsdaten(von, bis)

    zeile = df.iloc[0]
    # Kein OEFFNEN gesehen → kein valides "letzte Bewaesserung"-Signal
    assert zeile["stunden_seit_letzter_bewaesserung"] is None
    assert zeile["letzte_bewaesserung_dauer_s"] is None
    # Aber die Summen-Features zaehlen das Event trotzdem (Wassermenge ist angekommen)
    assert zeile["bewaesserung_anzahl_24h"] == 1
    assert zeile["bewaesserung_summe_24h"] == 999


async def test_bewaesserungs_features_ignoriert_keine_wasser_ausloeser(konfig):
    """UNBEKANNT/IGNORIERT duerfen ML-Features nicht als Wasser sehen."""
    start = datetime(2026, 4, 10, 0, 0)
    zeitpunkt = start + timedelta(hours=25)
    ventile = [
        VentilEreignis(
            zeitstempel=start + timedelta(hours=22),
            zone_id="garten_test",
            ventil_id="sensor_heuristik",
            aktion=VentilAktion.OEFFNEN,
            dauer_sekunden=0,
            ausloser=Ausloser.UNBEKANNT,
        ),
        VentilEreignis(
            zeitstempel=start + timedelta(hours=22, minutes=10),
            zone_id="garten_test",
            ventil_id="sensor_heuristik",
            aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=600,
            ausloser=Ausloser.UNBEKANNT,
        ),
        VentilEreignis(
            zeitstempel=start + timedelta(hours=23),
            zone_id="garten_test",
            ventil_id="manuell",
            aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=300,
            ausloser=Ausloser.IGNORIERT,
        ),
    ]

    extraktor = FeatureExtraktor(MagicMock(), konfig)  # type: ignore[arg-type]
    features = extraktor._bewaesserungs_features(ventile, zeitpunkt)

    assert features["stunden_seit_letzter_bewaesserung"] is None
    assert features["letzte_bewaesserung_dauer_s"] is None
    assert features["bewaesserung_anzahl_24h"] == 0
    assert features["bewaesserung_summe_24h"] == 0


async def test_zirkulaere_zeitfeatures(speicher, konfig):
    """Zirkulaere Zeit-Features korrekt kodiert."""
    start = datetime(2026, 4, 10, 0, 0)
    await _fuege_messreihe_ein(speicher, "garten_test", start, 50)

    extraktor = FeatureExtraktor(speicher, konfig)
    # Zeitpunkt 06:00
    von = start + timedelta(hours=30)  # 06:00 am naechsten Tag
    bis = start + timedelta(hours=30, minutes=30)
    df = await extraktor.erstelle_trainingsdaten(von, bis)

    assert len(df) >= 1
    zeile = df.iloc[0]
    # sin(2*pi*6/24) = sin(pi/2) = 1.0
    assert abs(zeile["stunde_sin"] - math.sin(2 * math.pi * 6 / 24)) < 0.01


async def test_zonen_features_korrekt(speicher, konfig):
    """Statische Zonen-Features korrekt gesetzt."""
    start = datetime(2026, 4, 10, 0, 0)
    await _fuege_messreihe_ein(speicher, "garten_test", start, 50)
    await _fuege_messreihe_ein(speicher, "indoor_test", start, 50,
                                quelle=DatenQuelle.FYTA)

    extraktor = FeatureExtraktor(speicher, konfig)
    von = start + timedelta(hours=25)
    bis = start + timedelta(hours=25, minutes=30)
    df = await extraktor.erstelle_trainingsdaten(von, bis)

    garten = df[df["zone_id"] == "garten_test"].iloc[0]
    indoor = df[df["zone_id"] == "indoor_test"].iloc[0]

    assert garten["ist_topf"] == 0
    assert garten["ist_indoor"] == 0
    assert garten["hat_regen_exposition"] == 1
    assert garten["feuchte_schwelle_min"] == 30

    assert indoor["ist_topf"] == 1
    assert indoor["ist_indoor"] == 1
    assert indoor["hat_regen_exposition"] == 0
    assert indoor["feuchte_schwelle_min"] == 22
    assert garten["zone_kategorie"] == "garten_test"
    assert garten["sensor_quelle"] == "gardena"
    assert garten["quelle"] == "garten_test"  # Legacy fuer alte Modelle
    assert indoor["zone_kategorie"] == "indoor_test"
    assert indoor["sensor_quelle"] == "fyta"


async def test_leere_daten_geben_leeren_dataframe(speicher, konfig):
    """Keine Daten → leerer DataFrame (kein Crash)."""
    extraktor = FeatureExtraktor(speicher, konfig)
    df = await extraktor.erstelle_trainingsdaten(
        datetime(2026, 1, 1), datetime(2026, 1, 2)
    )
    assert df.empty


# --- Tests: Evaluation ---

def test_mae_berechnung():
    y_true = np.array([10.0, 20.0, 30.0])
    y_pred = np.array([12.0, 18.0, 33.0])
    assert abs(mae(y_true, y_pred) - 2.333) < 0.01


def test_rmse_berechnung():
    y_true = np.array([10.0, 20.0, 30.0])
    y_pred = np.array([12.0, 18.0, 33.0])
    # sqrt((4+4+9)/3) = sqrt(17/3) ≈ 2.38
    assert abs(rmse(y_true, y_pred) - 2.38) < 0.1


def test_r2_perfekt():
    y = np.array([10.0, 20.0, 30.0])
    assert abs(r2(y, y) - 1.0) < 0.001


# --- Tests: ML-Ausschluss-Fenster ---

def test_filter_entfernt_zeilen_im_fenster(konfig):
    """Zeilen von waldblumenhain zwischen 16:00 und 18:00 fliegen raus."""
    import pandas as pd
    konfig.ml_ausschluss_fenster = [MlAusschlussFenster(
        zone_id="waldblumenhain",
        von=datetime(2026, 4, 15, 16, 0, 0),
        bis=datetime(2026, 4, 15, 18, 0, 0),
        grund="Sensor-Umzug",
    )]
    extraktor = FeatureExtraktor(speicher=None, konfig=konfig)  # type: ignore[arg-type]

    df = pd.DataFrame([
        {"zone_id": "waldblumenhain", "zeitstempel": "2026-04-15T15:30:00"},  # bleibt
        {"zone_id": "waldblumenhain", "zeitstempel": "2026-04-15T16:30:00"},  # raus
        {"zone_id": "waldblumenhain", "zeitstempel": "2026-04-15T17:59:59"},  # raus
        {"zone_id": "waldblumenhain", "zeitstempel": "2026-04-15T18:30:00"},  # bleibt
    ])
    ergebnis = extraktor._filtere_ausschluss_fenster(df)
    assert ergebnis["zeitstempel"].tolist() == [
        "2026-04-15T15:30:00", "2026-04-15T18:30:00",
    ]


def test_filter_ignoriert_andere_zonen(konfig):
    """Ausschluss gilt nur fuer die genannte zone_id."""
    import pandas as pd
    konfig.ml_ausschluss_fenster = [MlAusschlussFenster(
        zone_id="waldblumenhain",
        von=datetime(2026, 4, 15, 16, 0, 0),
        bis=datetime(2026, 4, 15, 18, 0, 0),
    )]
    extraktor = FeatureExtraktor(speicher=None, konfig=konfig)  # type: ignore[arg-type]

    df = pd.DataFrame([
        {"zone_id": "bambuswald", "zeitstempel": "2026-04-15T16:30:00"},
        {"zone_id": "bambuswald", "zeitstempel": "2026-04-15T17:00:00"},
    ])
    ergebnis = extraktor._filtere_ausschluss_fenster(df)
    assert len(ergebnis) == 2


def test_filter_ohne_fenster_ist_identitaet(konfig):
    """Leere Fenster-Liste aendert den DataFrame nicht."""
    import pandas as pd
    konfig.ml_ausschluss_fenster = []
    extraktor = FeatureExtraktor(speicher=None, konfig=konfig)  # type: ignore[arg-type]
    df = pd.DataFrame([
        {"zone_id": "waldblumenhain", "zeitstempel": "2026-04-15T16:30:00"},
    ])
    ergebnis = extraktor._filtere_ausschluss_fenster(df)
    assert len(ergebnis) == 1


def test_filter_mit_geraet_id_trifft_nur_diesen_sensor(konfig):
    """T-0267: Fenster mit geraet_id filtert nur diesen Sensor — andere
    Sensoren derselben Zone im selben Zeitraum bleiben im Trainings-Set.
    """
    import pandas as pd
    konfig.ml_ausschluss_fenster = [MlAusschlussFenster(
        zone_id="waldblumenhain",
        von=datetime(2026, 5, 23, 14, 0, 0),
        bis=datetime(2026, 5, 26, 20, 0, 0),
        grund="FYTA-Akku-Wechsel Phase 3",
        geraet_id="fyta_100003",  # nur FYTA-A
    )]
    extraktor = FeatureExtraktor(speicher=None, konfig=konfig)  # type: ignore[arg-type]

    df = pd.DataFrame([
        # FYTA-A im Fenster -> wird ausgeschlossen
        {"zone_id": "waldblumenhain", "zeitstempel": "2026-05-24T10:00:00",
         "geraet_id": "fyta_100003"},
        # FYTA-D im Fenster -> bleibt (anderer Sensor)
        {"zone_id": "waldblumenhain", "zeitstempel": "2026-05-24T10:00:00",
         "geraet_id": "fyta_100004"},
        # Gardena im Fenster -> bleibt (anderer Sensor)
        {"zone_id": "waldblumenhain", "zeitstempel": "2026-05-24T10:00:00",
         "geraet_id": "33333333-3333-3333-3333-333333333333"},
        # FYTA-A ausserhalb des Fensters -> bleibt
        {"zone_id": "waldblumenhain", "zeitstempel": "2026-05-27T10:00:00",
         "geraet_id": "fyta_100003"},
    ])
    ergebnis = extraktor._filtere_ausschluss_fenster(df)
    assert len(ergebnis) == 3
    assert "fyta_100003" not in [
        r["geraet_id"]
        for r in ergebnis.to_dict("records")
        if r["zeitstempel"] == "2026-05-24T10:00:00"
    ]


def test_filter_ohne_geraet_id_bleibt_zonen_weit(konfig):
    """T-0267 Backward-Compat: ohne `geraet_id` gilt der Filter zonen-
    weit (alle Sensoren). Bestehende Fenster ohne Sensor-Spezifizierung
    duerfen sich nicht im Verhalten aendern."""
    import pandas as pd
    konfig.ml_ausschluss_fenster = [MlAusschlussFenster(
        zone_id="waldblumenhain",
        von=datetime(2026, 4, 15, 16, 0, 0),
        bis=datetime(2026, 4, 15, 18, 0, 0),
        grund="Sensor-Umzug, alle Sensoren betroffen",
        # geraet_id bewusst None / nicht gesetzt
    )]
    extraktor = FeatureExtraktor(speicher=None, konfig=konfig)  # type: ignore[arg-type]
    df = pd.DataFrame([
        {"zone_id": "waldblumenhain", "zeitstempel": "2026-04-15T17:00:00",
         "geraet_id": "fyta_100003"},
        {"zone_id": "waldblumenhain", "zeitstempel": "2026-04-15T17:00:00",
         "geraet_id": "gardena-uuid"},
    ])
    ergebnis = extraktor._filtere_ausschluss_fenster(df)
    assert len(ergebnis) == 0  # beide Sensoren ausgeschlossen


# --- Tests: Evaluation ---


def test_baseline_vorhersage():
    """Baseline extrapoliert Trend + Wetter korrekt."""
    import pandas as pd

    df = pd.DataFrame({
        "boden_feuchte_aktuell": [50.0, 40.0],
        "feuchte_trend_6h": [-1.0, -0.5],
        "niederschlag_summe_6h": [2.0, 0.0],
        "et0_summe_6h": [0.5, 0.3],
    })

    baseline = BaselineVorhersage()
    pred = baseline.vorhersage(df, 6)

    # Zeile 0: 50 + (-1)*6 + 2*4 - 0.5*2 = 50 - 6 + 8 - 1 = 51
    assert abs(pred[0] - 51.0) < 0.1
    # Zeile 1: 40 + (-0.5)*6 + 0*4 - 0.3*2 = 40 - 3 - 0.6 = 36.4
    assert abs(pred[1] - 36.4) < 0.1


# --- Tests: VPD (T-0045) ---


def test_vpd_kpa_bekannte_werte():
    """Magnus-Formel liefert bekannte Referenzwerte."""
    # 20 °C / 50 % RH → ~1.17 kPa (haeufig zitiertes Lehrbuch-Beispiel)
    assert abs(_vpd_kpa(20.0, 50.0) - 1.17) < 0.02
    # 25 °C / 100 % RH → 0 (Saettigung)
    assert abs(_vpd_kpa(25.0, 100.0)) < 0.001
    # 30 °C / 30 % RH → ~2.97 kPa (Hitze + trocken)
    assert abs(_vpd_kpa(30.0, 30.0) - 2.97) < 0.05
    # 0 °C / 50 % RH → ~0.306 kPa (kalt → niedriger VPD)
    assert abs(_vpd_kpa(0.0, 50.0) - 0.306) < 0.01


def test_vpd_kpa_clampt_unplausible_werte():
    """RH < 0 oder > 100 wird defensiv auf [0, 100] geclamped."""
    # Bei 20 °C ist e_s ~2.34 kPa, also VPD(RH=0) ~ e_s, VPD(RH=100) = 0
    assert abs(_vpd_kpa(20.0, -10.0) - _vpd_kpa(20.0, 0.0)) < 1e-9
    assert abs(_vpd_kpa(20.0, 150.0) - _vpd_kpa(20.0, 100.0)) < 1e-9


async def test_feature_vpd_mittel_gefuellt(speicher, konfig):
    """vpd_mittel_{6,12,24}h sind im Feature-Set und plausibel befuellt."""
    start = datetime(2026, 4, 10, 0, 0)
    await _fuege_messreihe_ein(speicher, "garten_test", start, 72)
    await _fuege_wetter_ein(
        speicher, start, standort_id="test_ort",
        temperatur=20.0, luftfeuchte_prozent=50.0,
    )

    extraktor = FeatureExtraktor(speicher, konfig)
    von = start + timedelta(hours=24)
    bis = start + timedelta(hours=25)
    df = await extraktor.erstelle_trainingsdaten(von, bis)

    assert not df.empty
    for horizont in (6, 12, 24):
        spalte = f"vpd_mittel_{horizont}h"
        assert spalte in df.columns, f"{spalte} fehlt im Feature-Set"
        wert = df.iloc[0][spalte]
        # 20 °C / 50 % ≈ 1.17 kPa ± Toleranz
        assert wert is not None and not (isinstance(wert, float) and math.isnan(wert))
        assert abs(float(wert) - 1.17) < 0.05


async def test_bilanz_features_gefuellt_fuer_zone_mit_flaeche(speicher):
    """T-0056: bilanz_liter_* und bilanz_diff_24h sind fuer eine Zone mit
    flaeche_m2 berechnet, der Kanal-Durchfluss wird korrekt integriert."""
    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="test"),
        zonen=[
            ZonenKonfig(
                zone_id="bambus",
                name="Bambus",
                modus=ZonenModus.MONITORING,
                feuchte_schwelle_min=50,
                feuchte_schwelle_max=85,
                ventil_kanal=2,
                flaeche_m2=1.0,
                anteil_kanal=1.0,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="test_ort", breite=52.5, laenge=13.4)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten",
                name="Testgarten",
                wetter_standort="test_ort",
                zonen=["bambus"],
            ),
        ],
    )
    # Kanal-Durchfluss: 2.0 L/min (pro Minute) fuer Kanal 2, damit rechenbar.
    konfig.bilanz.kanal_liter_pro_minute = {2: 2.0}

    start = datetime(2026, 4, 10, 0, 0)
    await _fuege_messreihe_ein(speicher, "bambus", start, 72,
                                feuchte_start=60.0, feuchte_drift=-0.5)
    await _fuege_wetter_ein(speicher, start, standort_id="test_ort")

    # Ventil-Event 6h vor Messzeitpunkt (t = start + 25h): 10 Minuten giessen
    # → 10 * 2.0 L/min * 1.0 Anteil = 20 L
    await speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=start + timedelta(hours=19),
        zone_id="bambus",
        ventil_id="test_ventil",
        aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=600,
        ausloser=Ausloser.AUTOMATIK,
    ))

    extraktor = FeatureExtraktor(speicher, konfig)
    von = start + timedelta(hours=25)
    bis = start + timedelta(hours=25, minutes=30)
    df = await extraktor.erstelle_trainingsdaten(von, bis)

    assert not df.empty
    zeile = df.iloc[0]
    # 24h-Fenster umfasst das 20L-Ereignis; kein Regen (0mm); ET0 pro Stunde
    # 0.1mm × 24h × 1m² = 2.4 L. Also bilanz_24h ≈ 20 - 2.4 = 17.6 L (± etwas
    # je nachdem welcher Puffer).
    assert zeile["bilanz_liter_24h"] is not None
    assert 15.0 < float(zeile["bilanz_liter_24h"]) < 22.0
    # 6h-Fenster (t-6 bis t): das Event war bei t-6h, fällt ins Fenster.
    assert zeile["bilanz_liter_6h"] is not None
    assert 18.0 < float(zeile["bilanz_liter_6h"]) < 22.0
    # bilanz_diff_24h existiert und ist finit
    assert zeile["bilanz_diff_24h"] is not None
    assert abs(float(zeile["bilanz_diff_24h"])) < 50  # sanity


async def test_bilanz_forecast_nutzt_nur_zum_feature_zeitpunkt_verfuegbares_wetter(speicher):
    """Regression: spaetere Forecast-Abfragen duerfen historische Bilanz-
    Features nicht ueberschreiben.
    """
    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="test"),
        zonen=[
            ZonenKonfig(
                zone_id="bambus",
                name="Bambus",
                modus=ZonenModus.MONITORING,
                feuchte_schwelle_min=50,
                feuchte_schwelle_max=85,
                flaeche_m2=1.0,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="test_ort", breite=52.5, laenge=13.4)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten",
                name="Testgarten",
                wetter_standort="test_ort",
                zonen=["bambus"],
            ),
        ],
    )
    start = datetime(2026, 4, 10, 0, 0)
    feature_t = start + timedelta(hours=30)
    await _fuege_messreihe_ein(speicher, "bambus", start, 72)

    ziel_stunden = [
        feature_t - timedelta(hours=6) + timedelta(hours=i)
        for i in range(7)
    ]
    await speicher.speichere_wetter(
        feature_t - timedelta(hours=12),
        [
            WetterStunde(
                zeitstempel=t,
                temperatur=18.0,
                niederschlag_mm=1.0,
                et0_mm=0.0,
            )
            for t in ziel_stunden
        ],
        standort_id="test_ort",
    )
    # Dieser Forecast ist erst NACH feature_t verfuegbar. Ein leaky
    # Global-Cache wuerde ihn wegen des spaeteren puffer_bis verwenden.
    await speicher.speichere_wetter(
        feature_t + timedelta(hours=1),
        [
            WetterStunde(
                zeitstempel=t,
                temperatur=18.0,
                niederschlag_mm=10.0,
                et0_mm=0.0,
            )
            for t in ziel_stunden
        ],
        standort_id="test_ort",
    )

    extraktor = FeatureExtraktor(speicher, konfig)
    df = await extraktor.erstelle_trainingsdaten(feature_t, feature_t)

    assert not df.empty
    bilanz_6h = float(df.iloc[0]["bilanz_liter_6h"])
    assert bilanz_6h == pytest.approx(7.0)


async def test_bilanz_features_nan_ohne_flaeche(speicher, konfig):
    """T-0056: Zonen ohne flaeche_m2 (FYTA-Indoor etc.) bekommen NaN."""
    start = datetime(2026, 4, 10, 0, 0)
    await _fuege_messreihe_ein(speicher, "garten_test", start, 72)
    await _fuege_wetter_ein(speicher, start, standort_id="test_ort")

    extraktor = FeatureExtraktor(speicher, konfig)
    von = start + timedelta(hours=24)
    bis = start + timedelta(hours=25)
    df = await extraktor.erstelle_trainingsdaten(von, bis)

    assert not df.empty
    zeile = df.iloc[0]
    for horizont in (6, 12, 24):
        wert = zeile[f"bilanz_liter_{horizont}h"]
        ist_fehlend = wert is None or (
            isinstance(wert, float) and math.isnan(wert)
        )
        assert ist_fehlend, f"bilanz_liter_{horizont}h sollte NaN sein"
    delta = zeile["bilanz_diff_24h"]
    assert delta is None or (isinstance(delta, float) and math.isnan(delta))


async def test_feature_vpd_mittel_nan_ohne_luftfeuchte(speicher, konfig):
    """Ohne gespeicherte Luftfeuchte bleibt vpd_mittel_* NaN/None."""
    start = datetime(2026, 4, 10, 0, 0)
    await _fuege_messreihe_ein(speicher, "garten_test", start, 72)
    # luftfeuchte_prozent=None → Bestandsdaten vor dem Backfill
    await _fuege_wetter_ein(
        speicher, start, standort_id="test_ort",
        temperatur=20.0, luftfeuchte_prozent=None,
    )

    extraktor = FeatureExtraktor(speicher, konfig)
    von = start + timedelta(hours=24)
    bis = start + timedelta(hours=25)
    df = await extraktor.erstelle_trainingsdaten(von, bis)

    assert not df.empty
    for horizont in (6, 12, 24):
        spalte = f"vpd_mittel_{horizont}h"
        assert spalte in df.columns
        wert = df.iloc[0][spalte]
        # None → NaN in pandas; beide Varianten akzeptieren
        ist_fehlend = wert is None or (
            isinstance(wert, float) and math.isnan(wert)
        )
        assert ist_fehlend, f"{spalte} sollte NaN sein, war {wert!r}"


# --- T-0064: CPU-Phase in asyncio.to_thread ---

async def test_feature_build_blockiert_event_loop_nicht(speicher, konfig):
    """T-0064: Waehrend die sync-CPU-Phase laeuft, bleibt der Event-Loop responsiv.

    Vorher blockierte der Pandas-Build den Loop komplett — parallele
    Sensor-/Ventil-Endpoints hungerten, Dashboard verzoegert.
    Nach T-0064: `_baue_dataframe_sync` laeuft im Default-ThreadPool via
    `asyncio.to_thread`. Test simuliert eine teure CPU-Phase durch ein
    `time.sleep` im Sync-Teil und prueft, dass parallele
    `asyncio.sleep`-Ticks waehrenddessen weiterlaufen.
    """
    import asyncio
    import time

    start = datetime(2026, 4, 10, 0, 0)
    await _fuege_messreihe_ein(speicher, "garten_test", start, 72)
    await _fuege_wetter_ein(speicher, start, standort_id="test_ort")

    extraktor = FeatureExtraktor(speicher, konfig)

    # CPU-Phase kuenstlich auf 300 ms strecken
    original = extraktor._baue_dataframe_sync

    def verzoegert(*args, **kwargs):
        time.sleep(0.3)
        return original(*args, **kwargs)

    extraktor._baue_dataframe_sync = verzoegert  # type: ignore[assignment]

    von = start + timedelta(hours=24)
    bis = start + timedelta(hours=48)

    # Feature-Build + Tick-Counter parallel starten. Ein kooperativer Loop
    # tickt alle 20 ms; bei 300 ms Sync-Phase erwarten wir mindestens 8 Ticks.
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    ticker_task = asyncio.create_task(ticker())
    try:
        df = await extraktor.erstelle_trainingsdaten(von, bis)
    finally:
        ticker_task.cancel()
        try:
            await ticker_task
        except asyncio.CancelledError:
            pass

    assert not df.empty, "Feature-Build muss trotz to_thread ein Ergebnis liefern"
    assert ticks >= 5, (
        f"T-0064: Event-Loop war waehrend Feature-Build blockiert "
        f"(nur {ticks} Ticks in 300 ms; to_thread greift vermutlich nicht)."
    )


async def test_feature_build_sync_helper_liefert_gleiches_ergebnis(speicher, konfig):
    """T-0064-Regression: Sync-Helper + async Fassade liefern identischen DF."""
    start = datetime(2026, 4, 10, 0, 0)
    await _fuege_messreihe_ein(speicher, "garten_test", start, 72)
    await _fuege_wetter_ein(speicher, start, standort_id="test_ort")

    extraktor = FeatureExtraktor(speicher, konfig)
    von = start + timedelta(hours=24)
    bis = start + timedelta(hours=48)

    df_async = await extraktor.erstelle_trainingsdaten(von, bis)
    # Der Sync-Helper wird sonst nicht direkt aufgerufen; als Doppel-Check
    # rufen wir ihn mit denselben geladenen Rohdaten nochmal auf.
    puffer_von = von - timedelta(hours=max(extraktor.LAG_STUNDEN) + 1)
    puffer_bis = bis + timedelta(hours=max(extraktor.HORIZONTE) + 1)
    messungen = await speicher.hole_alle_messungen(puffer_von, puffer_bis)
    ventile = await speicher.hole_alle_ventil_ereignisse(puffer_von, puffer_bis)
    wetter_roh = await speicher.hole_wetter_vorhersagen(
        puffer_von - timedelta(hours=max(extraktor.HORIZONTE)), puffer_bis,
    )
    wetter_index = extraktor._baue_wetter_index(wetter_roh)
    archiv = {}
    for wsid in wetter_index.keys():
        archiv[wsid] = await speicher.hole_wetter_archiv(wsid, von=puffer_von, bis=puffer_bis)

    df_sync = extraktor._baue_dataframe_sync(
        messungen, ventile, wetter_index, archiv, von, bis,
    )
    assert len(df_async) == len(df_sync)
    assert list(df_async.columns) == list(df_sync.columns)


def test_wetter_features_dedupliziert_dubletten(konfig):
    """T-0100: Bestandsdubletten in `wetter_vorhersage` duerfen
    `niederschlag_summe` nicht verdoppeln. Defensiver Dedup im
    `_hole_wetter_fuer_horizont` — auch wenn der UNIQUE-Index in der DB
    fehlt, bleibt die Feature-Summe korrekt.
    """
    extraktor = FeatureExtraktor(speicher=None, konfig=konfig)  # type: ignore[arg-type]
    abfrage = "2026-04-09T23:44:19"
    rohdaten = []
    for h in range(3):
        eintrag = {
            "abfrage_zeitstempel": abfrage,
            "vorhersage_zeitstempel": f"2026-04-10T0{h}:00:00",
            "standort_id": "test_ort",
            "niederschlag_mm": 2.0,
            "et0_mm": 0.1,
            "temperatur": 18.0,
            "wind_kmh": 5.0,
            "wind_richtung_grad": 180.0,
            "luftfeuchte": 70.0,
        }
        # Doppelt einfuegen — simuliert Bestandsdublette.
        rohdaten.append(eintrag)
        rohdaten.append(dict(eintrag))

    wetter_index = extraktor._baue_wetter_index(rohdaten)
    features = extraktor._hole_wetter_fuer_horizont(
        zeitpunkt=datetime(2026, 4, 10, 0, 0),
        horizont_stunden=3,
        wetter_index=wetter_index,
        wetter_standort="test_ort",
    )
    # 3 Stunden x 2.0 mm = 6.0 mm. Ohne Dedup wuerde 12.0 herauskommen.
    assert features["niederschlag_summe_3h"] == pytest.approx(6.0)
    assert features["et0_summe_3h"] == pytest.approx(0.3)


def test_t0316_forecast_scan_filter_aequivalent_zu_direktaufruf(konfig):
    """T-0316: Die DF-Build-Optimierung baut den Forecast-Scan EINMAL pro
    Feature-Zeitpunkt (`_scan_beste_forecast`, weitestes Fenster) und filtert
    pro Horizont (`_forecast_aus_beste`). Das MUSS exakt dasselbe liefern wie
    der fruehere direkte `_hole_bilanz_forecast_bis_zeitpunkt`-Aufruf pro
    Horizont -- inkl. Leakage-Schutz (Abfragen nach `zeitpunkt` fliessen nicht
    ein) und Latest-Abfrage-gewinnt-Logik.
    """
    extraktor = FeatureExtraktor(speicher=None, konfig=konfig)  # type: ignore[arg-type]
    t = datetime(2026, 4, 10, 12, 0)
    rohdaten = []
    # Zwei verfuegbare Abfragen fuer dieselben ziel-Stunden -> spaetere (10:00,
    # regen=9.0) muss die fruehere (06:00, regen=1.0) ueberschreiben.
    for abfrage_iso, regen in [("2026-04-10T06:00:00", 1.0),
                               ("2026-04-10T10:00:00", 9.0)]:
        for dh in range(0, 25):  # ziel-Stunden t-24h .. t
            ziel = t - timedelta(hours=dh)
            rohdaten.append({
                "abfrage_zeitstempel": abfrage_iso,
                "vorhersage_zeitstempel": ziel.isoformat(),
                "standort_id": "test_ort",
                "niederschlag_mm": regen, "et0_mm": 0.2, "temperatur": 18.0,
                "wind_kmh": 5.0, "wind_richtung_grad": 180.0, "luftfeuchte": 70.0,
            })
    # Abfrage NACH `t` -> darf wegen Leakage-Schutz NICHT einfliessen.
    for dh in range(0, 25):
        ziel = t - timedelta(hours=dh)
        rohdaten.append({
            "abfrage_zeitstempel": "2026-04-10T18:00:00",
            "vorhersage_zeitstempel": ziel.isoformat(),
            "standort_id": "test_ort",
            "niederschlag_mm": 99.0, "et0_mm": 9.9, "temperatur": 18.0,
            "wind_kmh": 5.0, "wind_richtung_grad": 180.0, "luftfeuchte": 70.0,
        })
    wetter_index = extraktor._baue_wetter_index(rohdaten)

    beste_weit = extraktor._scan_beste_forecast(
        t, t - timedelta(hours=24), t, wetter_index, "test_ort",
    )
    for horizont in extraktor.HORIZONTE:
        von = t - timedelta(hours=horizont)
        direkt = extraktor._hole_bilanz_forecast_bis_zeitpunkt(
            zeitpunkt=t, von=von, bis=t,
            wetter_index=wetter_index, wetter_standort="test_ort",
        )
        optimiert = extraktor._forecast_aus_beste(
            beste_weit, von.isoformat(), t.isoformat(),
        )
        assert optimiert == direkt, f"Horizont {horizont}h divergiert"
        # Latest-gewinnt (9.0) + Leakage-Schutz (kein 99.0).
        assert {n for n, _ in direkt.values()} == {9.0}


async def test_speichere_wetter_blockt_dubletten_per_unique_index(speicher):
    """T-0100: Zweimal denselben Forecast schreiben — UNIQUE-Index +
    INSERT OR IGNORE muessen den zweiten Schreibvorgang als No-Op
    behandeln, sodass nur ein Eintrag in der DB landet.
    """
    abfrage = datetime(2026, 4, 10, 0, 0)
    stunden = [
        WetterStunde(
            zeitstempel=abfrage + timedelta(hours=1),
            temperatur=18.0,
            niederschlag_mm=2.0,
            wind_kmh=5.0,
            wind_richtung_grad=180.0,
            et0_mm=0.1,
            luftfeuchte_prozent=70.0,
        ),
    ]
    await speicher.speichere_wetter(abfrage, stunden, standort_id="test_ort")
    await speicher.speichere_wetter(abfrage, stunden, standort_id="test_ort")

    rohdaten = await speicher.hole_wetter_vorhersagen(
        abfrage - timedelta(hours=1), abfrage + timedelta(hours=2),
    )
    eintraege = [
        w for w in rohdaten
        if w["standort_id"] == "test_ort"
        and w["abfrage_zeitstempel"] == abfrage.isoformat()
    ]
    assert len(eintraege) == 1, f"Erwartet 1 Eintrag, gefunden {len(eintraege)}"
