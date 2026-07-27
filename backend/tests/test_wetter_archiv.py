import asyncio
from datetime import date, datetime, timedelta

import pytest

from bewaesserung.modelle import WetterArchivStunde
from bewaesserung.speicher import Speicher
from bewaesserung.wetter_archiv import (
    MAX_BACKFILL_TAGE,
    PUFFER_TAGE,
    WetterArchivJob,
    _parse_stunden,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "archiv.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


class ClientAttrappe:
    """Faelscht den HTTP-Aufruf. Liefert vordefinierte Stunden pro Zeitraum."""

    def __init__(self, standort_id: str, vordefiniert: list[WetterArchivStunde]):
        self.standort_id = standort_id
        self._vordefiniert = vordefiniert
        self.aufrufe: list[tuple[date, date]] = []

    async def hole_archiv(self, von: date, bis: date) -> list[WetterArchivStunde]:
        self.aufrufe.append((von, bis))
        return [
            s for s in self._vordefiniert
            if von <= s.zeitstempel.date() <= bis
        ]


def _baue_stunden(start: datetime, anzahl: int, regen_mm: float = 0.1) -> list[WetterArchivStunde]:
    return [
        WetterArchivStunde(
            zeitstempel=start + timedelta(hours=i),
            niederschlag_mm=regen_mm,
            temperatur=12.0,
            et0_mm=0.08,
        )
        for i in range(anzahl)
    ]


def test_parse_stunden_verarbeitet_open_meteo_format():
    daten = {
        "hourly": {
            "time": ["2026-04-10T00:00", "2026-04-10T01:00", "2026-04-10T02:00"],
            "temperature_2m": [8.5, 8.2, None],
            "precipitation": [0.0, 0.3, None],
            "et0_fao_evapotranspiration": [0.05, 0.05, 0.05],
        }
    }
    stunden = _parse_stunden(daten)
    # Die Stunde mit precipitation=None wird uebersprungen
    assert len(stunden) == 2
    assert stunden[0].zeitstempel == datetime(2026, 4, 10, 0)
    assert stunden[0].niederschlag_mm == 0.0
    assert stunden[1].niederschlag_mm == 0.3


def test_upsert_idempotent_ueberschreibt_werte(speicher):
    stunde = WetterArchivStunde(
        zeitstempel=datetime(2026, 4, 10, 12),
        niederschlag_mm=2.0, temperatur=14.0, et0_mm=0.1,
    )
    _run(speicher.upsert_wetter_archiv([stunde], "standort_a"))

    # Zweiter Aufruf mit abweichendem Wert -> wird aktualisiert (keine Duplikate)
    korrigiert = stunde.model_copy(update={"niederschlag_mm": 0.5})
    _run(speicher.upsert_wetter_archiv([korrigiert], "standort_a"))

    rows = _run(speicher.hole_wetter_archiv("standort_a"))
    assert len(rows) == 1
    assert rows[0].niederschlag_mm == 0.5


def test_juengster_zeitstempel_liefert_max(speicher):
    stunden = _baue_stunden(datetime(2026, 4, 10, 0), 5)
    _run(speicher.upsert_wetter_archiv(stunden, "standort_a"))

    jetzt_juengster = _run(speicher.juengster_archiv_zeitstempel("standort_a"))
    assert jetzt_juengster == datetime(2026, 4, 10, 4)


def test_juengster_zeitstempel_None_bei_leerer_tabelle(speicher):
    assert _run(speicher.juengster_archiv_zeitstempel("berlin")) is None


def test_job_ermittelt_korrektes_fenster_bei_leerer_db(speicher):
    # Leer -> MAX_BACKFILL_TAGE zurueckblicken ab (jetzt - puffer)
    jetzt = datetime(2026, 4, 17, 12)
    puffer = 5
    # Generiere Stunden fuer ein breites Fenster und lasse den Client filtern
    stunden_pool = _baue_stunden(datetime(2026, 1, 1, 0), 24 * 120)
    client = ClientAttrappe("standort_a", stunden_pool)
    job = WetterArchivJob(speicher, [client], puffer_tage=puffer)

    _run(job.aktualisiere_alle(jetzt))

    assert len(client.aufrufe) == 1
    von, bis = client.aufrufe[0]
    # ziel_bis = jetzt.date - puffer
    assert bis == date(2026, 4, 12)
    # Fenster ist exakt MAX_BACKFILL_TAGE Tage (bis inklusive)
    erwartete_von = bis - timedelta(days=MAX_BACKFILL_TAGE - 1)
    assert von == erwartete_von


def test_job_fuellt_nur_luecke_nach_juengstem_eintrag(speicher):
    # Vorlauf: ein Eintrag am 2026-04-05 12:00
    _run(speicher.upsert_wetter_archiv(
        [WetterArchivStunde(zeitstempel=datetime(2026, 4, 5, 12),
                             niederschlag_mm=1.0, temperatur=10.0, et0_mm=0.05)],
        "standort_a",
    ))
    jetzt = datetime(2026, 4, 17, 12)
    stunden_pool = _baue_stunden(datetime(2026, 4, 5, 13), 200)
    client = ClientAttrappe("standort_a", stunden_pool)
    job = WetterArchivJob(speicher, [client], puffer_tage=PUFFER_TAGE)

    _run(job.aktualisiere_alle(jetzt))

    von, bis = client.aufrufe[0]
    assert von == date(2026, 4, 5)  # Tag nach 04-05 12:00 faellt wieder auf 04-05
    assert bis == date(2026, 4, 12)  # ziel_bis = jetzt - 5


def test_job_skip_wenn_keine_luecke(speicher):
    # Juengster Eintrag liegt SCHON nach ziel_bis -> nichts holen
    _run(speicher.upsert_wetter_archiv(
        [WetterArchivStunde(zeitstempel=datetime(2026, 4, 16, 12),
                             niederschlag_mm=1.0, temperatur=10.0, et0_mm=0.05)],
        "standort_a",
    ))
    jetzt = datetime(2026, 4, 17, 12)
    client = ClientAttrappe("standort_a", [])
    job = WetterArchivJob(speicher, [client], puffer_tage=PUFFER_TAGE)

    _run(job.aktualisiere_alle(jetzt))

    assert client.aufrufe == []


def test_aktualisiere_wenn_faellig_respektiert_intervall(speicher):
    jetzt1 = datetime(2026, 4, 17, 12)
    client = ClientAttrappe("standort_a", _baue_stunden(datetime(2026, 4, 10, 0), 10))
    job = WetterArchivJob(speicher, [client], intervall_stunden=24, puffer_tage=PUFFER_TAGE)

    erste = _run(job.aktualisiere_wenn_faellig(jetzt1))
    assert erste is True
    assert len(client.aufrufe) == 1

    # 2 Stunden spaeter -> noch nicht faellig
    jetzt2 = jetzt1 + timedelta(hours=2)
    zweite = _run(job.aktualisiere_wenn_faellig(jetzt2))
    assert zweite is False
    assert len(client.aufrufe) == 1

    # 25 Stunden spaeter -> wieder faellig
    jetzt3 = jetzt1 + timedelta(hours=25)
    dritte = _run(job.aktualisiere_wenn_faellig(jetzt3))
    assert dritte is True
    assert len(client.aufrufe) == 2


def test_job_resilient_gegen_einzel_standort_fehler(speicher):
    class KaputterClient:
        standort_id = "berlin"
        async def hole_archiv(self, von, bis):
            raise RuntimeError("Open-Meteo down")

    gesunder = ClientAttrappe("standort_a", _baue_stunden(datetime(2026, 4, 10, 0), 10))
    job = WetterArchivJob(speicher, [KaputterClient(), gesunder], puffer_tage=PUFFER_TAGE)

    ergebnis = _run(job.aktualisiere_alle(datetime(2026, 4, 17, 12)))
    assert ergebnis["berlin"] == 0
    assert ergebnis["standort_a"] > 0


# --- T-0045: Luftfeuchte-Backfill ---


def test_backfill_luftfeuchte_fuellt_nur_null_zeilen(speicher):
    """backfill_luftfeuchte updated nur Zeilen mit NULL, bestehende bleiben."""
    from bewaesserung.modelle import WetterStunde

    abfrage = datetime(2026, 4, 10, 8, 0)

    # Zeile A: kein Luftfeuchte-Wert (Bestandsdaten)
    stunde_a = WetterStunde(
        zeitstempel=datetime(2026, 4, 10, 9, 0),
        temperatur=18.0, niederschlag_mm=0.0, wind_kmh=5.0,
        wind_richtung_grad=90.0, et0_mm=0.1,
        luftfeuchte_prozent=None,
    )
    # Zeile B: bereits mit Luftfeuchte (Live-Client nach T-0045)
    stunde_b = WetterStunde(
        zeitstempel=datetime(2026, 4, 10, 10, 0),
        temperatur=19.0, niederschlag_mm=0.0, wind_kmh=5.0,
        wind_richtung_grad=90.0, et0_mm=0.1,
        luftfeuchte_prozent=80.0,  # bewusst abweichend vom Archiv-Wert
    )
    _run(speicher.speichere_wetter(abfrage, [stunde_a, stunde_b], "standort_a"))

    rh_map = {
        "2026-04-10T09:00:00": 55.0,
        "2026-04-10T10:00:00": 60.0,  # muss ignoriert werden (nicht NULL)
    }
    n = _run(speicher.backfill_luftfeuchte("standort_a", rh_map))

    assert n == 1
    zeilen = _run(speicher.hole_wetter_vorhersagen(
        datetime(2026, 4, 10, 8, 0), datetime(2026, 4, 10, 11, 0),
    ))
    per_zeit = {z["vorhersage_zeitstempel"]: z["luftfeuchte"] for z in zeilen}
    assert per_zeit["2026-04-10T09:00:00"] == 55.0
    assert per_zeit["2026-04-10T10:00:00"] == 80.0


def test_backfill_luftfeuchte_respektiert_standort(speicher):
    """Updates wirken nur auf den angegebenen Standort."""
    from bewaesserung.modelle import WetterStunde

    abfrage = datetime(2026, 4, 10, 8, 0)
    stunde_ob = WetterStunde(
        zeitstempel=datetime(2026, 4, 10, 9, 0),
        temperatur=18.0, niederschlag_mm=0.0, wind_kmh=5.0,
        wind_richtung_grad=90.0, et0_mm=0.1,
        luftfeuchte_prozent=None,
    )
    stunde_bln = WetterStunde(
        zeitstempel=datetime(2026, 4, 10, 9, 0),
        temperatur=18.0, niederschlag_mm=0.0, wind_kmh=5.0,
        wind_richtung_grad=90.0, et0_mm=0.1,
        luftfeuchte_prozent=None,
    )
    _run(speicher.speichere_wetter(abfrage, [stunde_ob], "standort_a"))
    _run(speicher.speichere_wetter(abfrage, [stunde_bln], "berlin"))

    n = _run(speicher.backfill_luftfeuchte(
        "standort_a", {"2026-04-10T09:00:00": 55.0},
    ))
    assert n == 1

    ob_zeilen = _run(speicher.hole_wetter_vorhersagen(
        datetime(2026, 4, 10, 8, 0), datetime(2026, 4, 10, 11, 0),
        standort_id="standort_a",
    ))
    bln_zeilen = _run(speicher.hole_wetter_vorhersagen(
        datetime(2026, 4, 10, 8, 0), datetime(2026, 4, 10, 11, 0),
        standort_id="berlin",
    ))
    assert ob_zeilen[0]["luftfeuchte"] == 55.0
    assert bln_zeilen[0]["luftfeuchte"] is None
