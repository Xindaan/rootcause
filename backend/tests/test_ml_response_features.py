"""Tests fuer T-0065 response_features (Event-basierte Feature-Extraktion).

Deckt die kritischen Filter-/Label-Verhalten ab:
- AUTOMATIK- und UNBEKANNT-Events werden gefiltert
- Schlauch-Events (ventil_id='manuell') raus
- ml_ausschluss_fenster respektiert
- Label-Berechnung (f_vor, delta_6h/12h/24h)
- Bambus-shared-valve: ein Kanal-2-Event erzeugt zwei Rows
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import (
    Ausloser,
    BilanzKonfig,
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
)
from bewaesserung.speicher import Speicher

pytest.importorskip("pandas")

from bewaesserung.ml.response_features import (
    erstelle_response_features,
    _kanal_aus_ventil_id,
    _wetter_fenster,
    _finde_naechste_messung,
    F_VOR_TOLERANZ_MIN,
    F_VOR_VORWAERTS_MIN,
    FORWARD_FEATURES,
    INVERSE_FEATURES,
    monotone_vektor,
    FORWARD_MONOTONIE,
    INVERSE_MONOTONIE,
    _circuit_fuer_event,
    _circuit_zuordnung,
)


def _run(coro):
    return asyncio.run(coro)


def test_f9_circuit_fuer_aquabloom_event_gibt_pseudo_kreis():
    """F9: Ein konvertiertes AquaBloom-Event (ventil_id='sensor_heuristik',
    Zone ohne ventil_kanal) muss einen zonen-eindeutigen Pseudo-Kreis
    bekommen -- sonst faellt es VOR der Puls-Bildung raus und der AQUABLOOM-
    delta_6h-Zweig ist toter Code (entgegen Design + Wochen-Review)."""
    aqua = VentilEreignis(
        zeitstempel=datetime(2026, 5, 9, 8, 0),
        zone_id="zitrus", ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
        ausloser=Ausloser.AQUABLOOM,
    )
    # Zone ohne ventil_kanal (Pump-Zone) -> nur der ausloser-Sonderfall rettet.
    assert _circuit_fuer_event(aqua, {}) == ("aquabloom:zitrus", 0)

    # Gegenprobe: noch UNBEKANNTES Heuristik-Event bleibt None (kein Kreis).
    heuristik = VentilEreignis(
        zeitstempel=datetime(2026, 5, 9, 8, 0),
        zone_id="zitrus", ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
        ausloser=Ausloser.UNBEKANNT,
    )
    assert _circuit_fuer_event(heuristik, {}) is None


def test_t0482_circuit_zuordnung_kennt_jeden_erzeugten_aquabloom_kreis():
    """T-0482: Die beiden Haelften muessen sich treffen.

    `_circuit_fuer_event` erzeugte den Pseudo-Kreis seit F9, aber
    `_circuit_zuordnung` baute ihn nie als Key -- der Puls fiel in
    `erstelle_response_features` unter `kanal_ohne_zone`. Dieser Test
    prueft die Naht selbst, nicht eine der beiden Seiten.
    """
    konfig = _konfig_mit_pump_zone()
    zuordnung = _circuit_zuordnung(konfig)
    zonen_nach_id = {z.zone_id: z for z in konfig.zonen}

    ereignis = VentilEreignis(
        zeitstempel=datetime(2026, 5, 9, 8, 22),
        zone_id="zitrus", ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=900,
        ausloser=Ausloser.AQUABLOOM,
    )
    kreis = _circuit_fuer_event(ereignis, zonen_nach_id)
    assert kreis in zuordnung, (
        f"Kreis {kreis} erzeugt, aber nicht in circuit_zonen -> "
        "Puls faellt als kanal_ohne_zone raus"
    )
    assert zuordnung[kreis] == ["zitrus"]

    # Der Pump-Kreis ist von den Ventil-Kreisen getrennt: die Ventil-Zone
    # darf nicht mit in den AquaBloom-Kreis rutschen.
    assert "waldblumenhain" not in zuordnung[kreis]


# --- Hilfsstrukturen ---

def _konfig() -> GesamtKonfig:
    """Drei Zonen: Waldblumenhain (Kanal 1), bambuswald + yogaraum (Kanal 2)."""
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="test"),
        zonen=[
            ZonenKonfig(
                zone_id="waldblumenhain",
                name="Waldblumenhain",
                modus=ZonenModus.AUTOMATIK,
                feuchte_schwelle_min=40,
                feuchte_schwelle_max=60,
                ventil_kanal=1,
                tages_budget_sekunden=1800,
            ),
            ZonenKonfig(
                zone_id="bambuswald",
                name="Bambuswald",
                modus=ZonenModus.AUTOMATIK,
                feuchte_schwelle_min=60,
                feuchte_schwelle_max=80,
                ventil_kanal=2,
                tages_budget_sekunden=3600,
            ),
            ZonenKonfig(
                zone_id="bambuswald_yogaraum",
                name="Bambus Yogaraum",
                modus=ZonenModus.AUTOMATIK,
                feuchte_schwelle_min=60,
                feuchte_schwelle_max=80,
                ventil_kanal=2,
                tages_budget_sekunden=3600,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="ob", breite=52.5, laenge=13.4)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten",
                name="Garten",
                wetter_standort="ob",
                zonen=["waldblumenhain", "bambuswald", "bambuswald_yogaraum"],
            ),
        ],
        bilanz=BilanzKonfig(
            kanal_liter_pro_minute={1: 6.0, 2: 1.87},
            manuell_liter_pro_minute=10.0,
        ),
    )


def _konfig_mit_pump_zone() -> GesamtKonfig:
    """Standard-Konfig plus `zitrus` als AquaBloom-Pump-Zone.

    Pump-Zone heisst: kein `ventil_kanal` (kein Gardena-Ventil), dafuer
    die vier AquaBloom-Pflichtfelder. Genau die Kombination, an der der
    Kreis-Lookup bis T-0482 scheiterte.
    """
    konfig = _konfig()
    konfig.zonen.append(ZonenKonfig(
        zone_id="zitrus",
        name="Zitrus",
        modus=ZonenModus.MONITORING,
        feuchte_schwelle_min=30,
        feuchte_schwelle_max=55,
        ventil_kanal=None,
        tages_budget_sekunden=0,
        aquabloom_pumpen_dauer_sekunden=900,
        aquabloom_pumpen_intervall_stunden=12.0,
        aquabloom_tropfer_anzahl=2,
        aquabloom_tropfer_liter_pro_stunde=1.6,
    ))
    konfig.standorte[0].zonen.append("zitrus")
    return konfig


async def _setup_standard_wetter(speicher: Speicher, tag: datetime) -> None:
    """Basisausstattung an Wetter-Forecasts fuer die Testtage."""
    stunden = [
        WetterStunde(
            zeitstempel=tag + timedelta(hours=h - 30),
            temperatur=18.0,
            niederschlag_mm=0.0,
            wind_kmh=5.0,
            et0_mm=0.15,
            luftfeuchte_prozent=55.0,
        )
        for h in range(0, 60)  # 30 h rueckwaerts bis 30 h vorwaerts
    ]
    await speicher.speichere_wetter(
        abfrage_zeit=tag - timedelta(hours=30),
        stunden=stunden,
        standort_id="ob",
    )


async def _logge_event(
    speicher: Speicher,
    t_schliessen: datetime,
    dauer_s: int,
    zone_id: str,
    ventil_id: str,
    ausloser: Ausloser,
    liter: float | None = None,
) -> None:
    await speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=t_schliessen,
        zone_id=zone_id,
        ventil_id=ventil_id,
        aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=dauer_s,
        ausloser=ausloser,
        liter=liter,
    ))


async def _setze_messung(
    speicher: Speicher,
    t: datetime,
    zone_id: str,
    feuchte: float,
    geraet_id: str = "",
) -> None:
    await speicher.speichere_messung(SensorMessung(
        zeitstempel=t,
        zone_id=zone_id,
        geraet_id=geraet_id,
        boden_feuchte=feuchte,
        quelle=DatenQuelle.GARDENA,
    ))


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "resp.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


# --- Tests ---


def test_kanal_aus_ventil_id():
    assert _kanal_aus_ventil_id("abc-uuid:1") == 1
    assert _kanal_aus_ventil_id("abc-uuid:2") == 2
    assert _kanal_aus_ventil_id("manuell") is None
    assert _kanal_aus_ventil_id("") is None
    assert _kanal_aus_ventil_id(None) is None  # type: ignore[arg-type]
    assert _kanal_aus_ventil_id("abc-uuid") is None
    assert _kanal_aus_ventil_id("abc-uuid:nope") is None
    # backfill_app/gardena_web haben kein ':K' — der *String*-Parser
    # liefert None; die Zone-Aufloesung passiert in _kanal_fuer_event.
    assert _kanal_aus_ventil_id("backfill_app") is None
    assert _kanal_aus_ventil_id("gardena_web") is None


def test_kanal_fuer_event_loest_backfill_und_gardena_web_auf():
    """Regression: ohne diesen Fallback fielen ~15/39 manuelle Events raus
    (backfill_app + gardena_web in der Produktions-DB)."""
    from bewaesserung.ml.response_features import _kanal_fuer_event

    konfig = _konfig()
    zonen_nach_id = {z.zone_id: z for z in konfig.zonen}
    t = datetime(2026, 4, 10, 12, 0)

    def _ev(zone_id: str, ventil_id: str) -> VentilEreignis:
        return VentilEreignis(
            zeitstempel=t, zone_id=zone_id, ventil_id=ventil_id,
            aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
            ausloser=Ausloser.MANUELL,
        )

    # '<UUID>:K'-Events: direkt aus ID.
    assert _kanal_fuer_event(_ev("waldblumenhain", "uuid-A:1"), zonen_nach_id) == 1
    assert _kanal_fuer_event(_ev("bambuswald", "uuid-B:2"), zonen_nach_id) == 2
    # backfill_app/gardena_web: Kanal aus zone.ventil_kanal.
    assert _kanal_fuer_event(_ev("waldblumenhain", "backfill_app"), zonen_nach_id) == 1
    assert _kanal_fuer_event(_ev("bambuswald", "gardena_web"), zonen_nach_id) == 2
    assert _kanal_fuer_event(_ev("bambuswald_yogaraum", "backfill_app"), zonen_nach_id) == 2
    # Schlauch / leer / unbekannte ID → None.
    assert _kanal_fuer_event(_ev("waldblumenhain", "manuell"), zonen_nach_id) is None
    assert _kanal_fuer_event(_ev("waldblumenhain", ""), zonen_nach_id) is None
    assert _kanal_fuer_event(_ev("waldblumenhain", "sensor_heuristik"), zonen_nach_id) is None
    # Unbekannte zone_id mit backfill_app → None (nicht mappable).
    assert _kanal_fuer_event(_ev("unbekannt", "backfill_app"), zonen_nach_id) is None


def test_filtert_automatik_und_unbekannt(speicher, caplog):
    caplog.set_level(logging.INFO, logger="bewaesserung.ml.response_features")
    konfig = _konfig()
    tag = datetime(2026, 4, 10, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))

    # Sensor-Historie im 15-Minuten-Raster — Waldblumenhain steigt von 25
    # auf 49 (gibt verlaessliche f_vor/f_nach-Messungen in Toleranz).
    for quarter in range(0, 100):
        t = tag + timedelta(minutes=15 * quarter)
        _run(_setze_messung(
            speicher, t, "waldblumenhain", 25.0 + quarter * 0.25,
        ))

    # Drei Events: MANUELL (behalten), UNBEKANNT (raus), AUTOMATIK (raus).
    _run(_logge_event(
        speicher, tag + timedelta(hours=2), 1200,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL, liter=120.0,
    ))
    _run(_logge_event(
        speicher, tag + timedelta(hours=16), 600,
        "waldblumenhain", "uuid-A:1", Ausloser.UNBEKANNT,
    ))
    _run(_logge_event(
        speicher, tag + timedelta(hours=20), 900,
        "waldblumenhain", "uuid-A:1", Ausloser.AUTOMATIK,
    ))

    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=22),
    ))

    assert len(df) == 1
    row = df.iloc[0]
    assert row["zone_id"] == "waldblumenhain"
    assert int(row["dauer_s"]) == 1200


def test_filtert_schlauch_ventil_id(speicher):
    konfig = _konfig()
    tag = datetime(2026, 4, 10, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))
    for q in range(0, 100):
        _run(_setze_messung(
            speicher, tag + timedelta(minutes=15 * q),
            "waldblumenhain", 25.0 + q * 0.25,
        ))

    # Schlauch-Event: ventil_id='manuell' → variable Rate, nicht lernbar.
    _run(_logge_event(
        speicher, tag + timedelta(hours=2), 900,
        "waldblumenhain", "manuell", Ausloser.MANUELL, liter=100.0,
    ))
    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=10),
    ))
    assert df.empty


def test_respektiert_ausschluss_fenster(speicher):
    """T-0063 Regression: Events im ml_ausschluss_fenster nicht im DataFrame."""
    konfig = _konfig()
    tag = datetime(2026, 4, 15, 16, 0)  # Sensor-Umzug-Fenster
    _run(_setup_standard_wetter(speicher, tag))
    for h in range(-4, 25):
        _run(_setze_messung(
            speicher, tag + timedelta(hours=h), "waldblumenhain",
            30.0 + h * 0.5,
        ))
    konfig.ml_ausschluss_fenster = [
        MlAusschlussFenster(
            zone_id="waldblumenhain",
            von=tag, bis=tag + timedelta(hours=2),
        ),
    ]
    # Event-Zeitstempel innerhalb des Ausschluss-Fensters.
    _run(_logge_event(
        speicher, tag + timedelta(hours=1), 600,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL,
    ))
    # Und ein Sensor-Umzug-Fenster auf den Event-Zeitraum legen (20h-23h).
    konfig.ml_ausschluss_fenster = [
        MlAusschlussFenster(
            zone_id="waldblumenhain",
            von=tag - timedelta(minutes=30),
            bis=tag + timedelta(hours=3),
        ),
    ]
    df = _run(erstelle_response_features(
        speicher, konfig, von=tag - timedelta(hours=1),
        bis=tag + timedelta(hours=4),
    ))
    assert df.empty


def test_label_berechnung(speicher):
    konfig = _konfig()
    tag = datetime(2026, 4, 10, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))
    t_event = tag + timedelta(hours=2)  # SCHLIESSEN-Zeitpunkt

    # f_vor ≈ 40 (Messung -15 min vor t_start=event-20min)
    _run(_setze_messung(
        speicher, t_event - timedelta(minutes=35), "waldblumenhain", 40.0,
    ))
    # direkte Nachmessungen 6h/12h/24h spaeter
    _run(_setze_messung(
        speicher, t_event + timedelta(hours=6), "waldblumenhain", 55.0,
    ))
    _run(_setze_messung(
        speicher, t_event + timedelta(hours=12), "waldblumenhain", 50.0,
    ))
    _run(_setze_messung(
        speicher, t_event + timedelta(hours=24), "waldblumenhain", 45.0,
    ))
    _run(_logge_event(
        speicher, t_event, 1200,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL, liter=120.0,
    ))

    df = _run(erstelle_response_features(
        speicher, konfig,
        von=tag, bis=tag + timedelta(hours=4),
    ))
    assert len(df) == 1
    row = df.iloc[0]
    # f_vor ≈ 40
    assert abs(float(row["f_vor"]) - 40.0) < 0.5
    # delta_6h = 55-40 = 15
    assert abs(float(row["delta_6h"]) - 15.0) < 0.5
    # delta_12h = 50-40 = 10
    assert abs(float(row["delta_12h"]) - 10.0) < 0.5
    # delta_24h = 45-40 = 5
    assert abs(float(row["delta_24h"]) - 5.0) < 0.5
    # liter/s = 120/1200 = 0.1
    assert abs(float(row["liter_pro_sekunde"]) - 0.1) < 0.001


def test_bambus_shared_valve_erzeugt_zwei_rows(speicher):
    konfig = _konfig()
    tag = datetime(2026, 4, 10, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))
    t_event = tag + timedelta(hours=2)

    # f_vor-Messung muss VOR t_start=t_event-30min liegen — wir legen sie
    # 45 min vor t_event (also 15 min vor t_start → innerhalb Toleranz).
    _run(_setze_messung(
        speicher, t_event - timedelta(minutes=45), "bambuswald", 45.0,
    ))
    _run(_setze_messung(
        speicher, t_event + timedelta(hours=6), "bambuswald", 60.0,
    ))
    _run(_setze_messung(
        speicher, t_event - timedelta(minutes=45), "bambuswald_yogaraum", 60.0,
    ))
    _run(_setze_messung(
        speicher, t_event + timedelta(hours=6), "bambuswald_yogaraum", 70.0,
    ))

    # Event kommt auf Kanal 2 mit zone_id=bambuswald (muss aber fuer beide
    # zones gespiegelt werden).
    _run(_logge_event(
        speicher, t_event, 1800,
        "bambuswald", "uuid-B:2", Ausloser.MANUELL, liter=56.1,
    ))

    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=4),
    ))
    assert len(df) == 2
    zone_ids = sorted(df["zone_id"].tolist())
    assert zone_ids == ["bambuswald", "bambuswald_yogaraum"]
    assert all(df["shared_valve"])
    # f_vor unterschiedlich
    f_vor_bam = float(df[df["zone_id"] == "bambuswald"].iloc[0]["f_vor"])
    f_vor_yoga = float(
        df[df["zone_id"] == "bambuswald_yogaraum"].iloc[0]["f_vor"]
    )
    assert abs(f_vor_bam - 45.0) < 0.5
    assert abs(f_vor_yoga - 60.0) < 0.5
    # liter/s identisch (56.1/1800 ≈ 0.031)
    assert all(abs(float(v) - 56.1 / 1800) < 0.001 for v in df["liter_pro_sekunde"])


def test_gleicher_kanal_auf_zwei_dswc_wird_nicht_gespiegelt(speicher):
    """Multi-DSWC: Kanal 1 von Geraet A ist nicht Kanal 1 von Geraet B."""
    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="test"),
        zonen=[
            ZonenKonfig(
                zone_id="dswc1_zone", name="DSWC1", modus=ZonenModus.AUTOMATIK,
                feuchte_schwelle_min=40, feuchte_schwelle_max=60,
                ventil_geraet_id="dswc-1", ventil_kanal=1,
            ),
            ZonenKonfig(
                zone_id="dswc2_zone", name="DSWC2", modus=ZonenModus.AUTOMATIK,
                feuchte_schwelle_min=40, feuchte_schwelle_max=60,
                ventil_geraet_id="dswc-2", ventil_kanal=1,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="ob", breite=52.5, laenge=13.4)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten", wetter_standort="ob",
                zonen=["dswc1_zone", "dswc2_zone"],
            ),
        ],
        bilanz=BilanzKonfig(
            kanal_liter_pro_minute={1: 6.0},
            geraet_kanal_liter_pro_minute={
                "dswc-1": {1: 6.0},
                "dswc-2": {1: 1.4},
            },
        ),
    )
    tag = datetime(2026, 4, 10, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))
    t_event = tag + timedelta(hours=2)
    for zone_id, f0 in (("dswc1_zone", 40.0), ("dswc2_zone", 55.0)):
        _run(_setze_messung(
            speicher, t_event - timedelta(minutes=30), zone_id, f0,
        ))
        _run(_setze_messung(
            speicher, t_event + timedelta(hours=6), zone_id, f0 + 10.0,
        ))
    _run(_logge_event(
        speicher, t_event, 600,
        "dswc1_zone", "dswc-1:1", Ausloser.MANUELL,
    ))

    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=4),
    ))

    assert len(df) == 1
    assert df.iloc[0]["zone_id"] == "dswc1_zone"


def test_wetter_fenster_nutzt_as_of_statt_zukunftsforecast():
    """Response-Training darf Forecasts nach Puls-Start nicht leaken."""
    basis = datetime(2026, 4, 10, 12, 0)
    wetter_roh = [
        {
            "standort_id": "ob",
            "abfrage_zeitstempel": (basis - timedelta(hours=1)).isoformat(),
            "vorhersage_zeitstempel": (basis + timedelta(hours=2)).isoformat(),
            "niederschlag_mm": 0.0,
        },
        {
            "standort_id": "ob",
            "abfrage_zeitstempel": (basis + timedelta(hours=1)).isoformat(),
            "vorhersage_zeitstempel": (basis + timedelta(hours=2)).isoformat(),
            "niederschlag_mm": 12.0,
        },
    ]

    stunden = _wetter_fenster(
        wetter_roh,
        "ob",
        basis,
        basis + timedelta(hours=6),
        as_of=basis,
    )

    assert len(stunden) == 1
    assert stunden[0]["niederschlag_mm"] == 0.0


def test_ueberlappendes_schliessen_event_raus(speicher):
    """Pro-Horizont-Entwertung: 4h Abstand, kein Cluster (Default-Gap 60 min).

    Puls A und Puls B sind 4h auseinander → zwei separate Pulse (>60 min Gap).
    Fuer Puls A liegt Puls B im 6h/12h/24h-Label-Fenster → alle Labels None
    → Row wird verworfen.

    Fuer Puls B gibt es keinen nachfolgenden Puls. Damit die Row trotzdem
    als "ueberlappungs-bedingt leer" faellt, setzen wir vor Puls B keine
    f_vor-faehigen Messungen (nur am Tag-Anfang) — so liefert die
    f_vor-Suche None und die Row wird ebenfalls verworfen.
    """
    konfig = _konfig()
    tag = datetime(2026, 4, 10, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))
    # Messungen nur bis tag+3h: Puls A (t_start=tag+1h50min) findet f_vor
    # bei tag+1h (ca. 50 min davor, in 75-min-Toleranz). Puls B
    # (t_start=tag+5h50min) hat die letzte Messung bei tag+3h (2h50min
    # davor, ausserhalb der 75-min-Toleranz) → f_vor=None → Row dropped.
    for h in range(0, 4):
        _run(_setze_messung(
            speicher, tag + timedelta(hours=h), "waldblumenhain", 30.0 + h,
        ))
    # Event A bei +2h, Event B bei +6h — 4h auseinander, weit mehr als
    # cluster_gap_min=60 → zwei separate Pulse, aber B liegt im
    # 6h-Label-Fenster von A.
    _run(_logge_event(
        speicher, tag + timedelta(hours=2), 600,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL,
    ))
    _run(_logge_event(
        speicher, tag + timedelta(hours=6), 600,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL,
    ))
    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=10),
    ))
    # Puls A: alle Labels entwertet → dropped.
    # Puls B: kein f_vor → dropped.
    assert df.empty


def test_pulse_zwei_kurze_events_in_cluster_gap_werden_ein_puls(speicher):
    """Anpuls + 30min Pause + Hauptgiessen: genau 1 Puls, dauer=Summe."""
    konfig = _konfig()
    tag = datetime(2026, 4, 10, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))

    # f_vor vor dem ersten Event, f_nach_6h nach dem letzten.
    t_anpuls_ende = tag + timedelta(hours=2)          # 2:00h
    t_haupt_ende = tag + timedelta(hours=2, minutes=45)  # 2:45h
    _run(_setze_messung(
        speicher, t_anpuls_ende - timedelta(minutes=10), "waldblumenhain", 40.0,
    ))
    _run(_setze_messung(
        speicher, t_haupt_ende + timedelta(hours=6), "waldblumenhain", 60.0,
    ))
    # Anpuls: 300s, 10 min spaeter Start, endet 2:00h
    _run(_logge_event(
        speicher, t_anpuls_ende, 300,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL, liter=30.0,
    ))
    # Hauptgiessen: 1800s (30 min), endet 2:45h  → Gap zwischen Anpuls-Ende
    # (2:00h) und Haupt-Start (2:15h) = 15 min < 60 min Default → merge.
    _run(_logge_event(
        speicher, t_haupt_ende, 1800,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL, liter=180.0,
    ))

    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=5),
    ))
    assert len(df) == 1
    row = df.iloc[0]
    # dauer = 300 + 1800 = 2100s (Netto-Wasserzeit, Pause zaehlt nicht)
    assert int(row["dauer_s"]) == 2100
    # Beide Events sind im Puls gelandet
    assert int(row["puls_event_count"]) == 2
    # Liter: 30 + 180 = 210 → 210 / 2100 = 0.1 L/s
    assert abs(float(row["liter_pro_sekunde"]) - 0.1) < 0.001
    # delta_6h = 60 - 40 = 20 (Sensor nach 6h nach Puls-Ende)
    assert abs(float(row["delta_6h"]) - 20.0) < 0.5


def test_pulse_events_mit_grossem_gap_bleiben_getrennt(speicher):
    """Zwei Bewaesserungen an verschiedenen Tagen: 2 Pulse, jeder eigene Row.

    Abstand 30h > Cluster-Gap → 2 getrennte Pulse. delta_24h-Fenster
    (jeweils 24h ab Puls-Ende) schneidet den anderen nicht.
    """
    konfig = _konfig()
    tag = datetime(2026, 4, 1, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))
    _run(_setup_standard_wetter(speicher, tag + timedelta(hours=30)))
    # Sensor-Historie im 15-min-Raster (damit f_vor-Rueckwaertssuche
    # innerhalb 30min-Toleranz vor jedem t_start fuendig wird).
    for q in range(-4, 60 * 4):
        t = tag + timedelta(minutes=15 * q)
        _run(_setze_messung(
            speicher, t, "waldblumenhain", 30.0 + (q % 40) * 0.5,
        ))
    # Puls 1 (t+2h) und Puls 2 (t+32h = 30h spaeter)
    _run(_logge_event(
        speicher, tag + timedelta(hours=2), 600,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL,
    ))
    _run(_logge_event(
        speicher, tag + timedelta(hours=32), 600,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL,
    ))
    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=40),
    ))
    assert len(df) == 2
    # Beide Pulse haben `puls_event_count=1`
    assert set(df["puls_event_count"].tolist()) == {1}


def test_pulse_label_horizonte_einzeln_entwertet(speicher):
    """Puls A, 8h spaeter Puls B: A hat delta_6h, aber nicht delta_12h/24h.

    Damit bleibt A als Row erhalten (mind. ein Label gueltig), waehrend
    mit der alten globalen 24h-Ueberlappungs-Logik A rausgefallen waere.
    """
    konfig = _konfig()
    tag = datetime(2026, 4, 10, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))
    # Sensor-Raster fuer f_vor + Labels an t+6h, t+12h, t+24h
    t_a = tag + timedelta(hours=2)
    t_b = tag + timedelta(hours=10)  # 8h nach A
    # f_vor vor A (35 min vor t_start=A-10min → innerhalb Toleranz)
    _run(_setze_messung(
        speicher, t_a - timedelta(minutes=25), "waldblumenhain", 40.0,
    ))
    # 6h nach A → 55 (delta_6h = 15, gueltig)
    _run(_setze_messung(
        speicher, t_a + timedelta(hours=6), "waldblumenhain", 55.0,
    ))
    # 12h nach A → 52 (wuerde delta_12h=12 geben, aber B ist im Fenster)
    _run(_setze_messung(
        speicher, t_a + timedelta(hours=12), "waldblumenhain", 52.0,
    ))
    # 24h nach A → 50
    _run(_setze_messung(
        speicher, t_a + timedelta(hours=24), "waldblumenhain", 50.0,
    ))
    # f_vor von B (35 min vor t_start)
    _run(_setze_messung(
        speicher, t_b - timedelta(minutes=25), "waldblumenhain", 45.0,
    ))
    _run(_setze_messung(
        speicher, t_b + timedelta(hours=6), "waldblumenhain", 60.0,
    ))

    _run(_logge_event(
        speicher, t_a, 600, "waldblumenhain", "uuid-A:1", Ausloser.MANUELL,
    ))
    _run(_logge_event(
        speicher, t_b, 600, "waldblumenhain", "uuid-A:1", Ausloser.MANUELL,
    ))

    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=14),
    ))
    assert len(df) == 2
    puls_a = df.iloc[0]  # chronologisch sortiert
    # delta_6h fuer Puls A: 6h-Fenster endet bei A+6h=08:00, B beginnt
    # bei 14:00 → kein Puls in [t_a_start, t_a_end+6h]. Valide.
    assert puls_a["delta_6h"] is not None
    assert abs(float(puls_a["delta_6h"]) - 15.0) < 0.5
    # delta_12h Fenster bis A+12h=14:00 → Puls B startet bei 14:00 minus
    # 10 min = ca. 13:50 → innerhalb Fenster, delta_12h entwertet.
    import pandas as pd
    assert pd.isna(puls_a["delta_12h"])
    # delta_24h auch entwertet (Puls B ist drin)
    assert pd.isna(puls_a["delta_24h"])


def test_pulse_shared_valve_dedupliziert_und_spiegelt(speicher):
    """Ein backfill_app-Event auf Kanal 2 wird mit zone_id fuer bambuswald
    UND bambuswald_yogaraum geschrieben. Puls-Bildung darf die beiden
    Zeilen als einen Puls behandeln (Fingerprint = zeitstempel + dauer
    + ventil_id), aber anschliessend in beide Zonen spiegeln.
    """
    konfig = _konfig()
    tag = datetime(2026, 4, 10, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))
    t_event = tag + timedelta(hours=2)

    # t_start = t_event - 20min (1200s). f_vor-Messung muss vor t_start
    # liegen + innerhalb der 30min-Rueckwaerts-Toleranz.
    for zone, f0 in (("bambuswald", 50.0), ("bambuswald_yogaraum", 55.0)):
        _run(_setze_messung(
            speicher, t_event - timedelta(minutes=25), zone, f0,
        ))
        _run(_setze_messung(
            speicher, t_event + timedelta(hours=6), zone, f0 + 10.0,
        ))

    # Gleicher Event fuer beide Zonen (wie DHS-Backfill es schreibt)
    _run(_logge_event(
        speicher, t_event, 1200,
        "bambuswald", "backfill_app", Ausloser.MANUELL, liter=37.4,
    ))
    _run(_logge_event(
        speicher, t_event, 1200,
        "bambuswald_yogaraum", "backfill_app", Ausloser.MANUELL, liter=37.4,
    ))

    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=4),
    ))
    # Zwei Rows (gespiegelt), aber dauer_s jeweils 1200 (nicht 2400!)
    assert len(df) == 2
    assert sorted(df["zone_id"].tolist()) == ["bambuswald", "bambuswald_yogaraum"]
    assert set(df["dauer_s"].tolist()) == {1200}
    assert all(df["shared_valve"])


# --- T-0482: AquaBloom-Pulse erreichen das Response-Training ---


def _aquabloom_szenario(speicher, delta_6h: float) -> "object":
    """Ein gespeichertes AquaBloom-Paar + Messungen, Rueckgabe = DataFrame.

    Bewusst durch die volle Pipeline (`erstelle_response_features`), nicht
    nur bis zum Kreis: der alte F9-Test endete beim Kreis und blieb
    deshalb gruen, waehrend die Zeile nie entstand.
    """
    konfig = _konfig_mit_pump_zone()
    tag = datetime(2026, 5, 9, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))
    t_event = tag + timedelta(hours=2)

    f_vor = 30.0
    _run(_setze_messung(
        speicher, t_event - timedelta(minutes=35), "zitrus", f_vor,
    ))
    _run(_setze_messung(
        speicher, t_event + timedelta(hours=6), "zitrus", f_vor + delta_6h,
    ))
    _run(_setze_messung(
        speicher, t_event + timedelta(hours=24), "zitrus", f_vor + 1.0,
    ))
    # So legt der Konversions-Job die Pulse ab: ventil_id='sensor_heuristik'
    # (in AUSGESCHLOSSENE_VENTIL_IDS), ausloser=aquabloom, liter gesetzt.
    _run(_logge_event(
        speicher, t_event, 900,
        "zitrus", "sensor_heuristik", Ausloser.AQUABLOOM, liter=0.8,
    ))
    return _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=4),
    ))


def test_t0482_aquabloom_event_wird_zur_dataframe_zeile(speicher):
    """Der Regression-Test, den Audit-Befund F9 verlangt hatte.

    Vom gespeicherten AQUABLOOM-Event bis zur fertigen Zeile — nicht nur
    bis zum Pseudo-Kreis. Vor T-0482: 0 Rows.
    """
    df = _aquabloom_szenario(speicher, delta_6h=3.0)

    assert len(df) == 1, "AquaBloom-Puls erreicht das Training nicht"
    row = df.iloc[0]
    assert row["zone_id"] == "zitrus"
    assert int(row["dauer_s"]) == 900
    assert abs(float(row["delta_6h"]) - 3.0) < 0.5
    # liter/s aus dem Event (0.8 L / 900 s) — die Pump-Zone hat keine
    # Kanal-Rate in der BilanzKonfig, der Fallback wuerde None liefern.
    assert abs(float(row["liter_pro_sekunde"]) - 0.8 / 900) < 0.0001
    # Ein Pump ist ein eigener Kreis, nie ein geteiltes Ventil.
    assert not bool(row["shared_valve"])


def test_t0482_aquabloom_delta_schwelle_bleibt_scharf(speicher, tmp_path):
    """Der delta_6h>1.0-Filter muss weiter greifen.

    Gegenprobe: der Fix macht den AQUABLOOM-Zweig lebendig, er darf ihn
    nicht wirkungslos machen. Bewusst als Grenzpaar (1.0 raus / 1.5 rein)
    statt als einzelnes `len(df) == 0` — ein reiner Null-Assert waere
    auch VOR dem Fix gruen gewesen und haette nichts bewiesen.
    """
    assert len(_aquabloom_szenario(speicher, delta_6h=1.0)) == 0

    # Zweiter Speicher, damit das erste Szenario nicht nachwirkt.
    speicher_b = Speicher(str(tmp_path / "resp_b.db"))
    _run(speicher_b.verbinden())
    try:
        assert len(_aquabloom_szenario(speicher_b, delta_6h=1.5)) == 1
    finally:
        _run(speicher_b.schliessen())


# --- T-0069: f_vor-Suche robust gegen Gardena-Stunden-Cadence ---


def _sm(t: datetime, wert: float) -> SensorMessung:
    """Helper fuer die _finde_naechste_messung-Micro-Tests — keine DB noetig."""
    return SensorMessung(
        zeitstempel=t, zone_id="x", boden_feuchte=wert,
        quelle=DatenQuelle.GARDENA,
    )


def test_f_vor_toleriert_stunden_cadence():
    """T-0069 Regression: Gardena Smart Sensor II liefert ~1 Messung/h (8-120 min
    Variation). Die alte 30-min-Toleranz riss jeden Puls raus, dessen letzte
    Messung 31-60 min davor lag — z. B. 23.04. Waldblumenhain: Puls 10:33,
    Sensor 09:33 (60 min davor) fiel stumm raus.

    Mit F_VOR_TOLERANZ_MIN=75 muss diese Messung als f_vor akzeptiert werden.
    """
    t_puls = datetime(2026, 4, 23, 10, 33, 38)
    messungen = [
        _sm(t_puls - timedelta(minutes=60), 45.0),  # Stunden-Cadence: 09:33
    ]
    assert _finde_naechste_messung(
        messungen, t_puls,
        richtung="rueckwaerts", toleranz_min=F_VOR_TOLERANZ_MIN,
    ) == 45.0

    # Gegenprobe: 76 min davor liegt gerade ausserhalb — kein Treffer.
    messungen_zu_alt = [_sm(t_puls - timedelta(minutes=76), 45.0)]
    assert _finde_naechste_messung(
        messungen_zu_alt, t_puls,
        richtung="rueckwaerts", toleranz_min=F_VOR_TOLERANZ_MIN,
    ) is None


def test_f_vor_akzeptiert_messung_knapp_nach_puls_start():
    """T-0069 Regression: Puls-Start 10:33:38, Sensor-Messung 10:33:54
    (16 s spaeter) soll als f_vor gelten — das Wasser hat sich in 16 s noch
    nicht auf den Sensor ausgewirkt, aber die alte strikte Rueckwaerts-Suche
    haette diese Messung ausgelassen.

    Mit F_VOR_VORWAERTS_MIN=5 min als kleinem Vorwaerts-Fenster faengt das.
    """
    t_puls = datetime(2026, 4, 23, 10, 33, 38)
    messungen = [
        _sm(t_puls + timedelta(seconds=16), 42.5),  # 10:33:54
    ]
    assert _finde_naechste_messung(
        messungen, t_puls,
        richtung="rueckwaerts",
        toleranz_min=F_VOR_TOLERANZ_MIN,
        vorwaerts_toleranz_min=F_VOR_VORWAERTS_MIN,
    ) == 42.5


def test_f_vor_vorwaerts_fenster_bleibt_klein():
    """Die Vorwaerts-Toleranz darf nicht so gross sein, dass Messungen MITTEN
    im Puls (also bereits mit Wasser-Reaktion) als f_vor durchrutschen.
    F_VOR_VORWAERTS_MIN=5 min muss deutlich kleiner als typische Puls-Dauer
    bleiben.
    """
    t_puls = datetime(2026, 4, 23, 10, 33, 38)
    # Messung 10 Minuten nach Start — darf NICHT als f_vor gelten
    messungen = [_sm(t_puls + timedelta(minutes=10), 55.0)]
    assert _finde_naechste_messung(
        messungen, t_puls,
        richtung="rueckwaerts",
        toleranz_min=F_VOR_TOLERANZ_MIN,
        vorwaerts_toleranz_min=F_VOR_VORWAERTS_MIN,
    ) is None
    # Sanity: F_VOR_VORWAERTS_MIN bleibt klein (<= 10 min).
    assert F_VOR_VORWAERTS_MIN <= 10


def test_f_vor_bevorzugt_naehere_rueckwaerts_messung_bei_gleichzeitigen_kandidaten():
    """Wenn es sowohl eine Messung davor als auch knapp danach gibt, muss
    die zeitlich naeheste gewinnen — egal welche Richtung.
    """
    t_puls = datetime(2026, 4, 23, 10, 33, 38)
    messungen = [
        _sm(t_puls - timedelta(minutes=30), 40.0),
        _sm(t_puls + timedelta(seconds=20), 42.0),  # 20 s nach — naeher dran
    ]
    messungen.sort(key=lambda m: m.zeitstempel)
    assert _finde_naechste_messung(
        messungen, t_puls,
        richtung="rueckwaerts",
        toleranz_min=F_VOR_TOLERANZ_MIN,
        vorwaerts_toleranz_min=F_VOR_VORWAERTS_MIN,
    ) == 42.0


def test_feature_konstanten_und_monotonie_konsistent():
    """Feature-Listen + Monotonie-Maps passen zueinander."""
    assert set(FORWARD_FEATURES).issuperset({"dauer_s", "f_vor", "liter_pro_sekunde"})
    assert set(INVERSE_FEATURES).issuperset({"ziel_delta", "f_vor"})
    # Monotonie-Keys muessen in den jeweiligen Feature-Listen vorkommen.
    for k in FORWARD_MONOTONIE:
        assert k in FORWARD_FEATURES, f"{k!r} fehlt in FORWARD_FEATURES"
    for k in INVERSE_MONOTONIE:
        assert k in INVERSE_FEATURES, f"{k!r} fehlt in INVERSE_FEATURES"

    vec = monotone_vektor(list(INVERSE_FEATURES), INVERSE_MONOTONIE)
    assert len(vec) == len(INVERSE_FEATURES)
    # ziel_delta -> +1, f_vor -> +1, liter_pro_sekunde -> -1
    idx_delta = list(INVERSE_FEATURES).index("ziel_delta")
    idx_fvor = list(INVERSE_FEATURES).index("f_vor")
    idx_lps = list(INVERSE_FEATURES).index("liter_pro_sekunde")
    assert vec[idx_delta] == 1
    assert vec[idx_fvor] == 1
    assert vec[idx_lps] == -1


# --- T-0366/F9 an den Ursprung: f_vor + f_nach vom selben Sensor ---


def test_multi_sensor_mischt_keine_geraete(speicher):
    """f_vor + delta_6h von Geraet A; 12h/24h-Messungen gibt es NUR von
    Geraet B -> delta_12h/24h bleiben None statt auf B auszuweichen
    (Gardena 0-100 vs FYTA 0-65 % sind nicht vergleichbar)."""
    import pandas as pd

    konfig = _konfig()
    tag = datetime(2026, 4, 10, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))
    t_event = tag + timedelta(hours=2)

    # Geraet A: f_vor + 6h-Nachmessung.
    _run(_setze_messung(
        speicher, t_event - timedelta(minutes=35), "waldblumenhain", 40.0,
        geraet_id="gardena-a",
    ))
    _run(_setze_messung(
        speicher, t_event + timedelta(hours=6), "waldblumenhain", 55.0,
        geraet_id="gardena-a",
    ))
    # Geraet B: NUR 12h- und 24h-Nachmessungen (andere Skala).
    # Sortiert kaeme "fyta-b" vor "gardena-a" — B hat aber kein f_vor
    # und darf deshalb nicht gewinnen.
    _run(_setze_messung(
        speicher, t_event + timedelta(hours=12), "waldblumenhain", 20.0,
        geraet_id="fyta-b",
    ))
    _run(_setze_messung(
        speicher, t_event + timedelta(hours=24), "waldblumenhain", 18.0,
        geraet_id="fyta-b",
    ))
    _run(_logge_event(
        speicher, t_event, 1200,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL, liter=120.0,
    ))

    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=4),
    ))
    assert len(df) == 1
    row = df.iloc[0]
    assert row["geraet_id"] == "gardena-a"
    assert abs(float(row["delta_6h"]) - 15.0) < 0.5
    # Kein Ausweichen auf Geraet B: waere gemischt, staende hier 20-40=-20.
    assert pd.isna(row["delta_12h"])
    assert pd.isna(row["delta_24h"])


def test_multi_sensor_ohne_gemeinsames_paar_verwirft_row(speicher):
    """f_vor nur von Geraet A, f_nach nur von Geraet B -> kein Geraet
    liefert ein Paar -> Row faellt raus statt gemischt zu werden."""
    konfig = _konfig()
    tag = datetime(2026, 4, 10, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))
    t_event = tag + timedelta(hours=2)

    _run(_setze_messung(
        speicher, t_event - timedelta(minutes=35), "waldblumenhain", 40.0,
        geraet_id="gardena-a",
    ))
    _run(_setze_messung(
        speicher, t_event + timedelta(hours=6), "waldblumenhain", 25.0,
        geraet_id="fyta-b",
    ))
    _run(_logge_event(
        speicher, t_event, 1200,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL, liter=120.0,
    ))

    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=4),
    ))
    assert df.empty


def test_lead_geraet_gewinnt_bei_zwei_vollstaendigen_paaren(speicher):
    """Liefern zwei Geraete ein vollstaendiges Paar, gewinnt das
    aggregat_lead_geraet der Zone — nicht das alphabetisch erste."""
    konfig = _konfig()
    konfig.zonen[0].aggregat_lead_geraet = "gardena-lead"  # waldblumenhain
    tag = datetime(2026, 4, 10, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))
    t_event = tag + timedelta(hours=2)

    # "a-sensor" sortiert VOR "gardena-lead" — ohne Lead-Praeferenz
    # wuerde a-sensor gewinnen.
    for geraet, f_vor, f_nach in (
        ("a-sensor", 20.0, 30.0),
        ("gardena-lead", 40.0, 55.0),
    ):
        _run(_setze_messung(
            speicher, t_event - timedelta(minutes=35), "waldblumenhain",
            f_vor, geraet_id=geraet,
        ))
        _run(_setze_messung(
            speicher, t_event + timedelta(hours=6), "waldblumenhain",
            f_nach, geraet_id=geraet,
        ))
    _run(_logge_event(
        speicher, t_event, 1200,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL, liter=120.0,
    ))

    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=4),
    ))
    assert len(df) == 1
    row = df.iloc[0]
    assert row["geraet_id"] == "gardena-lead"
    assert abs(float(row["f_vor"]) - 40.0) < 0.5
    assert abs(float(row["delta_6h"]) - 15.0) < 0.5


def test_geraet_scoped_ausschluss_fenster(speicher):
    """T-0386-Semantik: ein Fenster mit geraet_id=B schneidet eine Zeile
    mit geraet_id=A NICHT weg; ein Fenster mit geraet_id=None schneidet
    jede Zeile der Zone."""
    tag = datetime(2026, 4, 10, 6, 0)
    t_event = tag + timedelta(hours=2)

    _run(_setup_standard_wetter(speicher, tag))
    # Nur Geraet A liefert ein Paar — die Zeile traegt geraet_id=A.
    _run(_setze_messung(
        speicher, t_event - timedelta(minutes=35), "waldblumenhain", 40.0,
        geraet_id="gardena-a",
    ))
    _run(_setze_messung(
        speicher, t_event + timedelta(hours=6), "waldblumenhain", 55.0,
        geraet_id="gardena-a",
    ))
    _run(_logge_event(
        speicher, t_event, 1200,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL, liter=120.0,
    ))

    # Fenster auf Geraet B -> Zeile (geraet_id=A) bleibt drin.
    konfig = _konfig()
    konfig.ml_ausschluss_fenster = [
        MlAusschlussFenster(
            zone_id="waldblumenhain",
            von=tag, bis=tag + timedelta(hours=30),
            geraet_id="fyta-b",
        ),
    ]
    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=4),
    ))
    assert len(df) == 1
    assert df.iloc[0]["geraet_id"] == "gardena-a"

    # Gleiches Fenster auf Geraet A -> Zeile faellt raus.
    konfig.ml_ausschluss_fenster[0].geraet_id = "gardena-a"
    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=4),
    ))
    assert df.empty

    # Fenster ohne geraet_id (None) -> gilt fuer alle Sensoren der Zone.
    konfig.ml_ausschluss_fenster[0].geraet_id = None
    df = _run(erstelle_response_features(
        speicher, konfig, von=tag, bis=tag + timedelta(hours=4),
    ))
    assert df.empty


# --- T-0473: Zaehl-Proxy statt Feature-Aufbau ---


def _zaehle(speicher, konfig, von, bis):
    from bewaesserung.ml.response_features import (
        zaehle_response_event_kandidaten,
    )
    return _run(zaehle_response_event_kandidaten(
        speicher, konfig, von=von, bis=bis,
    ))


def test_t0473_proxy_zaehlt_nie_unter_dem_echten_wert(speicher):
    """Die einzige Zusage des Proxys: er liegt NIE unter der DF-Zeilenzahl.

    Ueberzaehlen laesst den Retrain frueher feuern (harmlos). Unterzaehlen
    wuerde den Events-Trigger still entwerten -- eine Zone retrainierte nie
    wieder event-getrieben, ohne dass irgendwo ein Fehler auftaucht.

    Das Szenario mischt bewusst alles, was die Pipeline zusaetzlich filtert:
    geteilter Kanal (zwei Zeilen aus einem Event), zwei eng getaktete Events
    (Puls-Clustering: zwei Events, eine Zeile), ein Schlauch-Event, ein
    IGNORIERT-Event und ein Event ohne f_vor-Messung.
    """
    konfig = _konfig()
    tag = datetime(2026, 4, 10, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))
    bis = tag + timedelta(hours=5)
    t_event = tag + timedelta(hours=2)

    for zone in ("bambuswald", "bambuswald_yogaraum"):
        _run(_setze_messung(speicher, t_event - timedelta(minutes=45), zone, 45.0))
        _run(_setze_messung(speicher, t_event + timedelta(hours=6), zone, 60.0))

    # Geteilter Kanal 2, zwei eng getaktete Events -> EIN Puls, zwei Zeilen.
    _run(_logge_event(
        speicher, t_event, 900, "bambuswald", "uuid-B:2", Ausloser.MANUELL,
    ))
    _run(_logge_event(
        speicher, t_event + timedelta(minutes=20), 900,
        "bambuswald", "uuid-B:2", Ausloser.MANUELL,
    ))
    # Schlauch (variable Rate) -- die Pipeline wirft ihn weg.
    _run(_logge_event(
        speicher, t_event + timedelta(hours=1), 600,
        "waldblumenhain", "manuell", Ausloser.MANUELL,
    ))
    # Vom User als Phantom markiert -- beide Wege werfen ihn weg.
    _run(_logge_event(
        speicher, t_event + timedelta(hours=1), 600,
        "waldblumenhain", "uuid-A:1", Ausloser.IGNORIERT,
    ))
    # Echtes Kanal-Event ohne f_vor-Messung -- nur die Pipeline wirft es weg.
    _run(_logge_event(
        speicher, t_event + timedelta(hours=2), 600,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL,
    ))

    df = _run(erstelle_response_features(speicher, konfig, von=tag, bis=bis))
    echt = {
        z.zone_id: (0 if df.empty else int((df["zone_id"] == z.zone_id).sum()))
        for z in konfig.zonen
    }
    proxy = _zaehle(speicher, konfig, tag, bis)

    assert echt["bambuswald"] == 1 and echt["bambuswald_yogaraum"] == 1
    assert echt["waldblumenhain"] == 0
    for zone_id, n_echt in echt.items():
        assert proxy[zone_id] >= n_echt, (
            f"{zone_id}: Proxy {proxy[zone_id]} < echt {n_echt} -- "
            "Unterzaehlung entwertet den Events-Trigger still"
        )
    # Der geteilte Kanal muss auf BEIDE Zonen gespiegelt werden. Wuerde nur
    # nach zone_id gezaehlt, bekaeme yogaraum 0 -- und die Pipeline liefert
    # fuer yogaraum trotzdem eine Zeile. Genau das waere die Unterzaehlung.
    assert proxy["bambuswald_yogaraum"] == 2
    assert proxy["bambuswald"] == 2
    # IGNORIERT und Schlauch zaehlen nicht mit, das echte Kanal-Event schon.
    assert proxy["waldblumenhain"] == 1


def test_t0473_proxy_ignoriert_ausgeschlossene_ausloser(speicher):
    """AUTOMATIK/UNBEKANNT/IGNORIERT/FREMDWASSER sind kein Kandidaten-Wasser.

    Ohne diesen Filter wuerde der Zaehler bei jedem Cross-Spray-Event der
    Nachbarzone wachsen und einen Retrain auf Daten ausloesen, die gar nicht
    ins Training gehen.
    """
    konfig = _konfig()
    tag = datetime(2026, 4, 10, 6, 0)
    bis = tag + timedelta(hours=5)
    for ausloser in (
        Ausloser.AUTOMATIK, Ausloser.UNBEKANNT,
        Ausloser.IGNORIERT, Ausloser.FREMDWASSER,
    ):
        _run(_logge_event(
            speicher, tag + timedelta(hours=1), 600,
            "waldblumenhain", "uuid-A:1", ausloser,
        ))
    assert _zaehle(speicher, konfig, tag, bis)["waldblumenhain"] == 0

    # Gegenprobe: ZEITPLAN ist echtes Kanal-Wasser und muss zaehlen (T-0455).
    _run(_logge_event(
        speicher, tag + timedelta(hours=2), 600,
        "waldblumenhain", "uuid-A:1", Ausloser.ZEITPLAN,
    ))
    assert _zaehle(speicher, konfig, tag, bis)["waldblumenhain"] == 1


def test_t0473_proxy_ignoriert_oeffnen_und_dauerlose_events(speicher):
    """Nur SCHLIESSEN mit dauer>0 ist ein Kandidat (wie `_baue_pulse`).

    Ohne die Vorauswahl wuerde jedes OEFFNEN mitzaehlen -- der Zaehler
    verdoppelte sich, ohne dass eine einzige Trainingszeile dazukaeme.
    """
    konfig = _konfig()
    tag = datetime(2026, 4, 10, 6, 0)
    bis = tag + timedelta(hours=5)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=tag + timedelta(hours=1), zone_id="waldblumenhain",
        ventil_id="uuid-A:1", aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=0, ausloser=Ausloser.MANUELL,
    )))
    _run(_logge_event(
        speicher, tag + timedelta(hours=1, minutes=10), 0,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL,
    ))
    assert _zaehle(speicher, konfig, tag, bis)["waldblumenhain"] == 0


def test_t0403_response_feature_bau_blockiert_den_event_loop_nicht(speicher):
    """T-0403: der CPU-Teil laeuft im Thread, der Loop bleibt bedienbar.

    Vorher lief der komplette Feature-Aufbau -- verschachtelte Schleifen ueber
    Kanaele, Pulse und Zonen plus der DataFrame-Bau -- direkt im async-Rumpf.
    Ueber 365 Tage sind das laut Messung aus T-0458 rund 59 Sekunden, in
    denen weder HTTP-Requests noch der Entscheidungsloop drankommen. Genau
    das ist der Kern von T-0403 ("Retrain blockiert das Dashboard").

    Der Test misst die Nebenlaeufigkeit, statt sie zu behaupten: waehrend
    der Sync-Teil kuenstlich 300 ms braucht, muss ein paralleler Ticker mit
    20-ms-Takt weiterlaufen. Blockierte der Loop, bliebe er bei 0 Ticks
    stehen. Gleiches Muster wie `test_feature_build_blockiert_event_loop_nicht`
    im Feuchte-Pfad (T-0064).
    """
    import asyncio
    import time

    from bewaesserung.ml import response_features as rf

    konfig = _konfig()
    tag = datetime(2026, 4, 10, 6, 0)
    _run(_setup_standard_wetter(speicher, tag))
    for quarter in range(0, 100):
        _run(_setze_messung(
            speicher, tag + timedelta(minutes=15 * quarter),
            "waldblumenhain", 25.0 + quarter * 0.25,
        ))
    _run(_logge_event(
        speicher, tag + timedelta(hours=2), 1200,
        "waldblumenhain", "uuid-A:1", Ausloser.MANUELL, liter=120.0,
    ))

    original = rf._baue_response_df_sync

    def verzoegert(*args, **kwargs):
        time.sleep(0.3)
        return original(*args, **kwargs)

    rf._baue_response_df_sync = verzoegert
    try:
        async def szenario():
            ticks = 0

            async def ticker():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.02)
                    ticks += 1

            ticker_task = asyncio.create_task(ticker())
            df = await rf.erstelle_response_features(
                speicher, konfig, von=tag, bis=tag + timedelta(hours=22),
            )
            ticker_task.cancel()
            return ticks, df

        ticks, df = _run(szenario())
    finally:
        rf._baue_response_df_sync = original

    assert len(df) == 1, "Setup-Annahme: genau ein Puls kommt durch"
    assert ticks >= 8, (
        f"Event-Loop war blockiert -- nur {ticks} Ticks waehrend 300 ms "
        f"Sync-Phase (erwartet >= 8)"
    )
