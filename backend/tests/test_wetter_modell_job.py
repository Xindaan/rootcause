"""T-0505: Mehrmodell-Mitschnitt + gemessene Referenz.

Die Zahlen stammen aus den Live-Abrufen vom 05.08.2026 (Musterstadt,
52.52/13.405): derselbe Abruf lieferte ueber 48 h 13,9 mm (icon_d2) gegen
0,2 mm (icon_eu) -- das ist der Anlass fuer den ganzen Mitschnitt.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from bewaesserung.speicher import Speicher
from bewaesserung.station_messung_job import (
    BASIS_TAGE, StationMessungJob, _parse_stunden,
)
from bewaesserung.wetter_modell_job import WetterModellJob, _raster_3h


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "modell.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _hourly(spalten: dict[str, list[float | None]], n: int = 4) -> dict:
    h: dict = {"time": [f"2026-08-05T{i:02d}:00" for i in range(n)]}
    h.update(spalten)
    return h


# --------------------------------------------------------------------------
# Spalten-Extraktion
# --------------------------------------------------------------------------

def test_stunden_fuer_modell_liest_die_richtige_spalte():
    """Multi-Modell-Antworten haengen den Modellnamen an den Spaltennamen.

    Die echten Zahlen vom 05.08.: icon_d2 nass, icon_eu praktisch trocken.
    Ein Vertauschen der Spalten wuerde genau die Divergenz unsichtbar
    machen, die gemessen werden soll.
    """
    hourly = _hourly({
        "precipitation_icon_d2": [1.0, 2.0, 3.0, 7.9],
        "precipitation_icon_eu": [0.0, 0.1, 0.1, 0.0],
    })
    d2 = WetterModellJob._stunden_fuer_modell(hourly, "icon_d2")
    eu = WetterModellJob._stunden_fuer_modell(hourly, "icon_eu")

    assert [mm for _, mm in d2] == [1.0, 2.0, 3.0, 7.9]
    assert [mm for _, mm in eu] == [0.0, 0.1, 0.1, 0.0]
    assert d2[0][0] == datetime(2026, 8, 5, 0, 0)


def test_fehlende_modellspalte_ist_leer_kein_absturz():
    """Ein Modell ohne Abdeckung ist kein Fehler, sondern liefert nichts."""
    hourly = _hourly({"precipitation_icon_d2": [1.0, 1.0, 1.0, 1.0]})
    assert WetterModellJob._stunden_fuer_modell(hourly, "gfs_seamless") == []


def test_raster_3h_rundet_nach_unten():
    """Der Lauf-Suchlauf startet auf dem 3-h-Raster, nie in der Zukunft."""
    assert _raster_3h(datetime(2026, 8, 5, 7, 10, tzinfo=timezone.utc)) == (
        datetime(2026, 8, 5, 6, 0, tzinfo=timezone.utc)
    )
    assert _raster_3h(datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc)) == (
        datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc)
    )


# --------------------------------------------------------------------------
# Append-only: die Eigenschaft, auf der die ganze Auswertung steht
# --------------------------------------------------------------------------

def test_zwei_abrufe_ueberschreiben_sich_nicht(speicher):
    """DER Test fuer den Mitschnitt.

    Zweimal derselbe Zieltermin, zu verschiedenen Zeitpunkten abgerufen, mit
    verschiedenen Werten -- das ist eine Prognose-Revision. Beide Zeilen
    muessen stehenbleiben, sonst laesst sich "was wusste man am Donnerstag
    ueber Samstag" nicht mehr rekonstruieren.
    """
    ziel = datetime(2026, 8, 7, 12, 0)
    _run(speicher.speichere_modell_prognosen(
        datetime(2026, 8, 5, 6, 0), "musterstadt", "icon_d2", [(ziel, 13.2)],
    ))
    _run(speicher.speichere_modell_prognosen(
        datetime(2026, 8, 5, 11, 30), "musterstadt", "icon_d2", [(ziel, 1.7)],
    ))

    zeilen = _run(speicher.hole_modell_prognosen(standort_id="musterstadt"))
    assert len(zeilen) == 2
    assert sorted(z["niederschlag_mm"] for z in zeilen) == [1.7, 13.2]


def test_gleicher_abruf_zweimal_bleibt_beim_ersten_wert(speicher):
    """Derselbe Schluessel ist derselbe Abruf, keine neue Information.

    INSERT OR IGNORE statt REPLACE: ein zweiter Schreibversuch darf den
    Mitschnitt nicht nachtraeglich veraendern.
    """
    abfrage = datetime(2026, 8, 5, 6, 0)
    ziel = datetime(2026, 8, 7, 12, 0)
    _run(speicher.speichere_modell_prognosen(
        abfrage, "musterstadt", "icon_d2", [(ziel, 13.2)],
    ))
    _run(speicher.speichere_modell_prognosen(
        abfrage, "musterstadt", "icon_d2", [(ziel, 99.9)],
    ))

    zeilen = _run(speicher.hole_modell_prognosen())
    assert len(zeilen) == 1
    assert zeilen[0]["niederschlag_mm"] == 13.2


def test_modelle_trennen_sich_sauber(speicher):
    """Gleicher Abruf, gleicher Zieltermin, verschiedene Modelle -> 5 Zeilen."""
    abfrage = datetime(2026, 8, 5, 6, 0)
    ziel = datetime(2026, 8, 7, 12, 0)
    for modell, mm in [
        ("icon_d2", 13.9), ("icon_eu", 0.2), ("ecmwf_ifs025", 0.9),
        ("ecmwf_aifs025_single", 2.4), ("gfs_seamless", 0.6),
    ]:
        _run(speicher.speichere_modell_prognosen(
            abfrage, "musterstadt", modell, [(ziel, mm)],
        ))

    assert len(_run(speicher.hole_modell_prognosen())) == 5
    nur_d2 = _run(speicher.hole_modell_prognosen(modell="icon_d2"))
    assert len(nur_d2) == 1 and nur_d2[0]["niederschlag_mm"] == 13.9


def test_lauf_zeitstempel_bleibt_utc_und_ist_optional(speicher):
    """Zielzeiten naiv lokal, Modelllauf aware UTC -- am Offset unterscheidbar.

    Und: faellt die Lauf-Aufloesung aus, wird trotzdem geschrieben. Der
    Mitschnitt ist wichtiger als seine Herkunftsangabe.
    """
    ziel = datetime(2026, 8, 7, 12, 0)
    lauf = datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc)
    _run(speicher.speichere_modell_prognosen(
        datetime(2026, 8, 5, 6, 0), "musterstadt", "icon_d2",
        [(ziel, 13.9)], lauf,
    ))
    _run(speicher.speichere_modell_prognosen(
        datetime(2026, 8, 5, 7, 0), "musterstadt", "icon_d2",
        [(ziel, 13.9)], None,
    ))

    zeilen = sorted(
        _run(speicher.hole_modell_prognosen()),
        key=lambda z: z["abfrage_zeitstempel"],
    )
    assert zeilen[0]["modell_lauf_zeitstempel"].endswith("+00:00")
    assert "+" not in zeilen[0]["ziel_zeitstempel"]
    assert zeilen[1]["modell_lauf_zeitstempel"] is None


# --------------------------------------------------------------------------
# Intervall-Gate
# --------------------------------------------------------------------------

def test_intervall_gate_sperrt_den_zweiten_lauf(speicher):
    """Der 5-min-Entscheidungsloop darf die API nicht 288x taeglich treffen."""
    job = WetterModellJob(speicher, [("musterstadt", 52.52, 13.405)])
    aufrufe: list = []

    async def _kein_netz(breite, laenge):
        aufrufe.append((breite, laenge))
        return None

    job._hole_prognosen = _kein_netz  # type: ignore[method-assign]

    start = datetime(2026, 8, 5, 6, 0)
    _run(job.aktualisiere_wenn_faellig(start))
    _run(job.aktualisiere_wenn_faellig(start + timedelta(hours=3)))
    assert len(aufrufe) == 1

    _run(job.aktualisiere_wenn_faellig(start + timedelta(hours=25)))
    assert len(aufrufe) == 2


def test_fehlgeschlagener_abruf_blockt_trotzdem_das_intervall(speicher):
    """Ein Fehler darf keinen Retry-Sturm gegen eine fremde Gratis-API ausloesen."""
    job = WetterModellJob(speicher, [("musterstadt", 52.52, 13.405)])
    aufrufe: list = []

    async def _fehler(breite, laenge):
        aufrufe.append(1)
        return None

    job._hole_prognosen = _fehler  # type: ignore[method-assign]

    start = datetime(2026, 8, 5, 6, 0)
    _run(job.aktualisiere_wenn_faellig(start))
    _run(job.aktualisiere_wenn_faellig(start + timedelta(minutes=5)))
    _run(job.aktualisiere_wenn_faellig(start + timedelta(minutes=10)))
    assert len(aufrufe) == 1


# --------------------------------------------------------------------------
# Bright-Sky-Parsing
# --------------------------------------------------------------------------

def _bs_antwort(eintraege: list[tuple[str, float | None]]) -> dict:
    return {
        "sources": [{
            "id": 8178, "dwd_station_id": "03205",
            "station_name": "Oberkraemer-Marwitz (Wasserwerk)",
            "distance": 12100, "observation_type": "historical",
        }],
        "weather": [
            {"timestamp": t, "precipitation": p, "source_id": 8178}
            for t, p in eintraege
        ],
    }


def test_mosmix_vorhersagequelle_wird_verworfen():
    """DER Test gegen den teuersten Fehler dieser Auswertung.

    Bright Sky mischt MOSMIX-VORHERSAGEN unter die Beobachtungen -- und die
    naechstgelegene Quelle unseres Standorts ist genau so eine (ORANIENBURG,
    2,9 km, `observation_type: forecast`, ohne `dwd_station_id`, mit Werten
    bis in den Folgetag). Sie ist naeher als jede echte Messstation und
    damit besonders verfuehrerisch.

    Wuerde sie als Messung durchgehen, pruefte die Auswertung Vorhersage
    gegen Vorhersage und bescheinigte jedem Modell eine Treffsicherheit, die
    nur die Aehnlichkeit zweier Modelle misst.
    """
    antwort = {
        "sources": [
            {"id": 8178, "dwd_station_id": "03205", "station_name": "Marwitz",
             "distance": 12100, "observation_type": "historical"},
            {"id": 4637, "station_name": "ORANIENBURG",
             "distance": 2900, "observation_type": "forecast"},
        ],
        "weather": [
            {"timestamp": "2026-08-04T12:00:00+02:00",
             "precipitation": 1.7, "source_id": 8178},
            {"timestamp": "2026-08-06T12:00:00+02:00",
             "precipitation": 9.9, "source_id": 4637},
        ],
    }
    stunden = _parse_stunden(antwort)

    assert len(stunden) == 1
    assert stunden[0]["station_name"] == "Marwitz"
    assert stunden[0]["beobachtungs_typ"] == "historical"
    assert all(s["niederschlag_mm"] != 9.9 for s in stunden)


def test_synop_und_current_gelten_als_messung():
    """`current` ist die frische SYNOP-Beobachtung vor der Qualitaetspruefung.

    Sie auszuschliessen wuerde die juengsten Tage blind machen -- genau die,
    fuer die der Mitschnitt taeglich laeuft.
    """
    antwort = {
        "sources": [
            {"id": 1, "station_name": "TEGEL", "distance": 21800,
             "observation_type": "current"},
            {"id": 2, "station_name": "X", "distance": 30000,
             "observation_type": "synop"},
        ],
        "weather": [
            {"timestamp": "2026-08-04T12:00:00+02:00",
             "precipitation": 0.4, "source_id": 1},
            {"timestamp": "2026-08-04T13:00:00+02:00",
             "precipitation": 0.6, "source_id": 2},
        ],
    }
    assert len(_parse_stunden(antwort)) == 2


def test_parse_station_wandelt_offset_in_naive_lokalzeit():
    """Bright Sky liefert '+02:00'. Die DB haelt naiv lokal -- sonst kein Join."""
    stunden = _parse_stunden(_bs_antwort([
        ("2026-08-01T12:00:00+02:00", 1.7),
    ]))
    assert stunden[0]["zeitstempel"] == "2026-08-01T12:00:00"
    assert stunden[0]["station_id"] == "03205"
    assert stunden[0]["distanz_km"] == 12.1


def test_fehlender_messwert_wird_nicht_als_null_mm_gebucht():
    """None ist Stationsausfall, nicht Trockenheit.

    Wuerde eine ausgefallene Stunde als 0 mm gespeichert, bestraeft die
    Auswertung jedes Modell, das dort korrekt Regen vorhergesagt hat.
    """
    stunden = _parse_stunden(_bs_antwort([
        ("2026-08-01T12:00:00+02:00", None),
        ("2026-08-01T13:00:00+02:00", 0.0),
        ("2026-08-01T14:00:00+02:00", 2.4),
    ]))
    assert [s["zeitstempel"][-8:] for s in stunden] == ["13:00:00", "14:00:00"]


def test_station_upsert_nimmt_den_nachgelieferten_wert(speicher):
    """DWD liefert erst `current`, spaeter geprueft `historical` nach.

    Hier ist Ueberschreiben richtig -- anders als beim Prognose-Mitschnitt.
    Wir halten eine Messung fort, keine Aussage ueber fruehes Wissen.
    """
    zeile = {
        "zeitstempel": "2026-08-01T12:00:00", "niederschlag_mm": 1.7,
        "station_id": "03205", "station_name": "Marwitz", "distanz_km": 12.1,
    }
    _run(speicher.upsert_station_messung(
        "musterstadt", [zeile], datetime(2026, 8, 1, 13, 0),
    ))
    _run(speicher.upsert_station_messung(
        "musterstadt", [{**zeile, "niederschlag_mm": 2.1}],
        datetime(2026, 8, 6, 3, 0),
    ))

    juengste = _run(speicher.juengste_station_messung("musterstadt"))
    assert juengste == datetime(2026, 8, 1, 12, 0)
    zeilen = _run(speicher.hole_modell_prognosen())  # andere Tabelle, leer
    assert zeilen == []


# --------------------------------------------------------------------------
# T-0504-Regression: Nachholfenster darf nicht am Ende festkleben
# --------------------------------------------------------------------------

def test_fenster_faehrt_basis_mit_trotz_junger_letzter_stunde(speicher):
    """DER Regressionstest fuer die Fehlerklasse aus T-0504.

    Ein Nachholfenster, das nur aus "seit wann sind wir stumm?" abgeleitet
    wird, ist nach einem TEILWEISEN Nachlauf blind: die letzte Stunde ist
    jung, das Loch davor bleibt fuer immer stehen. Hier ist die juengste
    Messung eine Stunde alt -- das Fenster muss trotzdem `BASIS_TAGE`
    zurueckreichen.
    """
    job = StationMessungJob(speicher, [("musterstadt", 52.52, 13.405)])
    jetzt = datetime(2026, 8, 5, 9, 0)

    von, bis = job._fenster(jetzt - timedelta(hours=1), jetzt)

    assert bis == jetzt
    assert von <= jetzt - timedelta(days=BASIS_TAGE)


def test_fenster_dehnt_sich_bei_langer_luecke(speicher):
    """Laengere Stille als das Basis-Fenster -> es wird laenger, nicht kuerzer."""
    job = StationMessungJob(speicher, [("musterstadt", 52.52, 13.405)])
    jetzt = datetime(2026, 8, 5, 9, 0)

    von, _ = job._fenster(jetzt - timedelta(days=30), jetzt)
    assert von <= jetzt - timedelta(days=30)


def test_fenster_ohne_vorbestand_ist_gedeckelt(speicher):
    """Leere Tabelle holt viel, aber nicht unbegrenzt."""
    job = StationMessungJob(speicher, [("musterstadt", 52.52, 13.405)])
    jetzt = datetime(2026, 8, 5, 9, 0)

    von, _ = job._fenster(None, jetzt)
    assert von >= jetzt - timedelta(days=91)
