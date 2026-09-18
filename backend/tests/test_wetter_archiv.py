import asyncio
from datetime import date, datetime, timedelta

import pytest

from bewaesserung.modelle import WetterArchivStunde
from bewaesserung.speicher import Speicher
from bewaesserung.wetter_archiv import (
    BASIS_TAGE,
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
    assert _run(speicher.juengster_archiv_zeitstempel("beispielstadt")) is None


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


def test_job_holt_basisfenster_auch_ohne_luecke_am_ende(speicher):
    """T-0510: aktuelle Reihe heisst NICHT "nichts zu tun".

    Dieser Test hiess bis 05.08.2026 `test_job_skip_wenn_keine_luecke` und
    sicherte zu, dass gar nichts geholt wird, wenn der juengste Eintrag schon
    hinter `ziel_bis` liegt. Genau diese Zusicherung war das Problem: ein
    Fenster, das nur aus "wie weit sind wir gekommen?" abgeleitet wird, sieht
    ein Loch in der MITTE der Reihe nie wieder an.

    Neu wird immer mindestens `BASIS_TAGE` mitgeholt. Unbedenklich, weil der
    Upsert per `ON CONFLICT DO UPDATE` idempotent ist -- und nuetzlich, weil
    das Archiv seine Werte nachtraeglich korrigiert.
    """
    _run(speicher.upsert_wetter_archiv(
        [WetterArchivStunde(zeitstempel=datetime(2026, 4, 16, 12),
                             niederschlag_mm=1.0, temperatur=10.0, et0_mm=0.05)],
        "standort_a",
    ))
    jetzt = datetime(2026, 4, 17, 12)
    client = ClientAttrappe("standort_a", [])
    job = WetterArchivJob(speicher, [client], puffer_tage=PUFFER_TAGE)

    _run(job.aktualisiere_alle(jetzt))

    assert len(client.aufrufe) == 1
    von, bis = client.aufrufe[0]
    assert bis == date(2026, 4, 12)                      # jetzt - PUFFER_TAGE
    assert von == bis - timedelta(days=BASIS_TAGE - 1)   # 2026-04-06


def test_loch_in_der_mitte_wird_wieder_abgedeckt(speicher):
    """DER Regressionstest fuer die Fehlerklasse aus T-0504.

    Aufbau: die Reihe hat einen Eintrag von vorgestern (im Basis-Fenster) und
    ein Loch davor. Der juengste Eintrag ist also frisch -- nach der alten
    Regel haette der Job ab dahinter angesetzt und das Loch nie wieder
    beruehrt.

    Geprueft wird die Fenstergrenze, nicht das Ergebnis: ob die Luecke
    tatsaechlich gefuellt wird, haengt am Archiv, nicht an uns. Unsere Pflicht
    ist, sie ueberhaupt noch anzufragen.
    """
    _run(speicher.upsert_wetter_archiv(
        [WetterArchivStunde(zeitstempel=datetime(2026, 4, 11, 12),
                             niederschlag_mm=1.0, temperatur=10.0, et0_mm=0.05)],
        "standort_a",
    ))
    jetzt = datetime(2026, 4, 17, 12)
    client = ClientAttrappe("standort_a", _baue_stunden(datetime(2026, 4, 6, 0), 200))
    job = WetterArchivJob(speicher, [client], puffer_tage=PUFFER_TAGE)

    _run(job.aktualisiere_alle(jetzt))

    von, bis = client.aufrufe[0]
    assert von <= date(2026, 4, 6), (
        "Das Basis-Fenster muss hinter den juengsten Eintrag zurueckreichen, "
        "sonst bleibt ein Loch in der Mitte fuer immer stehen."
    )
    assert bis == date(2026, 4, 12)


def test_lange_luecke_gewinnt_gegen_das_basisfenster(speicher):
    """Ist die Luecke laenger als BASIS_TAGE, wird sie ganz geholt -- nicht gekuerzt."""
    _run(speicher.upsert_wetter_archiv(
        [WetterArchivStunde(zeitstempel=datetime(2026, 3, 1, 12),
                             niederschlag_mm=1.0, temperatur=10.0, et0_mm=0.05)],
        "standort_a",
    ))
    jetzt = datetime(2026, 4, 17, 12)
    client = ClientAttrappe("standort_a", _baue_stunden(datetime(2026, 3, 1, 13), 2000))
    job = WetterArchivJob(speicher, [client], puffer_tage=PUFFER_TAGE)

    _run(job.aktualisiere_alle(jetzt))

    von, _ = client.aufrufe[0]
    assert von == date(2026, 3, 1)


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
        standort_id = "beispielstadt"
        async def hole_archiv(self, von, bis):
            raise RuntimeError("Open-Meteo down")

    gesunder = ClientAttrappe("standort_a", _baue_stunden(datetime(2026, 4, 10, 0), 10))
    job = WetterArchivJob(speicher, [KaputterClient(), gesunder], puffer_tage=PUFFER_TAGE)

    ergebnis = _run(job.aktualisiere_alle(datetime(2026, 4, 17, 12)))
    assert ergebnis["beispielstadt"] == 0
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
    _run(speicher.speichere_wetter(abfrage, [stunde_bln], "beispielstadt"))

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
        standort_id="beispielstadt",
    ))
    assert ob_zeilen[0]["luftfeuchte"] == 55.0
    assert bln_zeilen[0]["luftfeuchte"] is None


# --------------------------------------------------------------------------
# T-0509: Archiv-Produkt explizit statt Archive-Default
# --------------------------------------------------------------------------

def test_archiv_modell_landet_als_models_parameter(monkeypatch):
    """Der Kern von T-0509: ohne `models` liefert die API `ecmwf_ifs`.

    Genau dieses stille Default war der Fehler -- die Quelle war nie gewaehlt,
    sondern zugefallen, und niemand hat es gemerkt, weil das Feld fehlte.
    """
    from bewaesserung import wetter_archiv as wa

    gesehen: dict = {}

    class _Antwort:
        @staticmethod
        def raise_for_status(): ...
        @staticmethod
        def json(): return {"hourly": {"time": [], "precipitation": []}}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            gesehen.update(params or {})
            return _Antwort()

    monkeypatch.setattr(wa.httpx, "AsyncClient", lambda **kw: _Client())

    client = wa.WetterArchivClient(52.52, 13.405, "musterstadt", "era5")
    _run(client.hole_archiv(date(2026, 7, 1), date(2026, 7, 2)))
    assert gesehen["models"] == "era5"


def test_leeres_archiv_modell_faellt_auf_api_default(monkeypatch):
    """Leerstring = bewusst zurueck auf `best_match`, kein `models` senden."""
    from bewaesserung import wetter_archiv as wa

    gesehen: dict = {}

    class _Antwort:
        @staticmethod
        def raise_for_status(): ...
        @staticmethod
        def json(): return {"hourly": {"time": [], "precipitation": []}}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            gesehen.update(params or {})
            return _Antwort()

    monkeypatch.setattr(wa.httpx, "AsyncClient", lambda **kw: _Client())

    client = wa.WetterArchivClient(52.52, 13.405, "musterstadt", "")
    _run(client.hole_archiv(date(2026, 7, 1), date(2026, 7, 2)))
    assert "models" not in gesehen


def test_konfig_reicht_archiv_modell_bis_zum_client_durch():
    """Whitelist-Drift-Schutz (`fehlerpattern_config_whitelist`).

    Ein neues Pydantic-Feld nuetzt nichts, wenn der Konfig-Parser oder die
    Client-Fabrik es nicht weiterreicht -- dann laeuft die Anlage stumm auf
    dem alten Default weiter. Dieser Test prueft die ganze Kette.
    """
    from bewaesserung.modelle import WetterKonfig, WetterStandortKonfig
    from bewaesserung.wetter_archiv import baue_clients_aus_konfig

    konfig = WetterKonfig(
        standorte=[WetterStandortKonfig(id="musterstadt", breite=52.52,
                                        laenge=13.405)],
        archiv_modell="era5",
    )
    clients = baue_clients_aus_konfig(konfig)
    assert clients[0]._modell == "era5"
