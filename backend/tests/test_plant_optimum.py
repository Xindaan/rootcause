"""T-0050b: FYTA-Plant-Optimum-Cache Tests."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from bewaesserung.modelle import (
    FytaKonfig,
    FytaPflanzenKonfig,
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    StandortKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.plant_optimum_job import PlantOptimumJob
from bewaesserung.schwellen_vorschlag import berechne_vorschlaege_fuer_alle
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


def _konfig_mit_fyta() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=[
            ZonenKonfig(
                zone_id="mandevilla", name="Mandevilla",
                modus=ZonenModus.MONITORING, ventil_kanal=None,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="berlin", breite=52.5, laenge=13.4)]
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="balkon", name="Balkon",
                wetter_standort="berlin", zonen=["mandevilla"],
            ),
        ],
        fyta=FytaKonfig(pflanzen=[
            FytaPflanzenKonfig(fyta_id=100002, zone_id="mandevilla", name="Mandevilla"),
        ]),
    )


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "po.db"))
    _run(s.verbinden())
    yield s
    _run(s.schliessen())


def test_plant_optimum_speichern_und_lesen(speicher):
    """UPSERT + hole_plant_optima roundtrip."""
    _run(speicher.speichere_plant_optimum(
        zone_id="mandevilla", feuchte_min=30.0, feuchte_max=70.0,
        feuchte_min_akzeptabel=20.0, feuchte_max_akzeptabel=80.0,
    ))
    cache = _run(speicher.hole_plant_optima())
    assert "mandevilla" in cache
    assert cache["mandevilla"]["feuchte_min"] == 30.0
    assert cache["mandevilla"]["feuchte_max"] == 70.0
    assert cache["mandevilla"]["quelle"] == "fyta"


def test_plant_optimum_upsert_ueberschreibt(speicher):
    """Zweiter Aufruf fuer gleiche Zone aktualisiert Werte."""
    _run(speicher.speichere_plant_optimum(
        zone_id="mandevilla", feuchte_min=30.0, feuchte_max=70.0,
    ))
    _run(speicher.speichere_plant_optimum(
        zone_id="mandevilla", feuchte_min=35.0, feuchte_max=75.0,
    ))
    cache = _run(speicher.hole_plant_optima())
    assert cache["mandevilla"]["feuchte_min"] == 35.0
    assert cache["mandevilla"]["feuchte_max"] == 75.0


def test_job_ruht_ohne_fyta_konfig(speicher):
    """Ohne konfigurierte FYTA-Pflanzen macht der Job nichts."""
    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=[], wetter=WetterKonfig(standorte=[]),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
    )
    job = PlantOptimumJob(speicher, konfig, fyta_client=None)
    assert _run(job.aktualisiere_wenn_faellig()) is False


def test_job_persistiert_fyta_werte(speicher):
    """Mock FYTA-Client liefert Optimum → Job schreibt in DB.

    T-0196: Job nutzt jetzt hole_plant_optima_alle_achsen (Multi-Achse).
    Mock liefert nur feuchte-Achse; Backward-Kompat-Pfad schreibt in
    plant_optimum, Multi-Achsen-Pfad in plant_optimum_achse.
    """
    konfig = _konfig_mit_fyta()
    mock_client = AsyncMock()
    mock_client.hole_plant_optima_alle_achsen.return_value = {
        "feuchte": {
            "min_good": 30.0, "max_good": 70.0,
            "min_akzeptabel": 20.0, "max_akzeptabel": 80.0,
            "einheit": "%/h", "current": 62.0,
        },
    }
    job = PlantOptimumJob(speicher, konfig, mock_client, intervall_stunden=24)
    assert _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 20, 12, 0))) is True
    mock_client.hole_plant_optima_alle_achsen.assert_called_once_with(100002)
    # Backward-Kompat: plant_optimum bekommt die Feuchte-Werte
    cache = _run(speicher.hole_plant_optima())
    assert cache["mandevilla"]["feuchte_min"] == 30.0
    # T-0196: plant_optimum_achse bekommt die EAV-Zeile
    achsen = _run(speicher.hole_plant_optima_achsen())
    assert achsen["mandevilla"]["feuchte"]["min_good"] == 30.0
    assert achsen["mandevilla"]["feuchte"]["einheit"] == "%/h"


def test_job_intervall_gate_blockiert_zweiten_lauf(speicher):
    """24h-Intervall — zweiter Aufruf am selben Tag: kein Call."""
    konfig = _konfig_mit_fyta()
    mock_client = AsyncMock()
    mock_client.hole_plant_optima_alle_achsen.return_value = {
        "feuchte": {
            "min_good": 30.0, "max_good": 70.0,
            "min_akzeptabel": None, "max_akzeptabel": None,
            "einheit": "%/h", "current": None,
        },
    }
    job = PlantOptimumJob(speicher, konfig, mock_client, intervall_stunden=24)
    t0 = datetime(2026, 4, 20, 12, 0)
    _run(job.aktualisiere_wenn_faellig(t0))
    _run(job.aktualisiere_wenn_faellig(t0 + timedelta(hours=10)))
    assert mock_client.hole_plant_optima_alle_achsen.call_count == 1  # nur einmal!
    # Nach 24h wieder faellig
    _run(job.aktualisiere_wenn_faellig(t0 + timedelta(hours=25)))
    assert mock_client.hole_plant_optima_alle_achsen.call_count == 2


def test_job_api_fehler_ignoriert_zone(speicher):
    """Wenn FYTA-API None liefert (Fehler), wird nichts persistiert."""
    konfig = _konfig_mit_fyta()
    mock_client = AsyncMock()
    mock_client.hole_plant_optima_alle_achsen.return_value = None
    job = PlantOptimumJob(speicher, konfig, mock_client)
    _run(job.aktualisiere_wenn_faellig(datetime.now()))
    cache = _run(speicher.hole_plant_optima())
    assert cache == {}
    achsen = _run(speicher.hole_plant_optima_achsen())
    assert achsen == {}


def test_job_persistiert_alle_achsen(speicher):
    """T-0196: Mock liefert mehrere Achsen -> alle landen in plant_optimum_achse."""
    konfig = _konfig_mit_fyta()
    mock_client = AsyncMock()
    mock_client.hole_plant_optima_alle_achsen.return_value = {
        "feuchte": {
            "min_good": 30.0, "max_good": 70.0,
            "min_akzeptabel": 20.0, "max_akzeptabel": 80.0,
            "einheit": "%/h", "current": 62.0,
        },
        "licht_ppfd": {
            "min_good": 11.5, "max_good": 460.0,
            "min_akzeptabel": 2.75, "max_akzeptabel": 690.0,
            "einheit": "μmol/h", "current": 471.0,
        },
        "licht_dli": {
            "min_good": 4.0, "max_good": 20.0,
            "min_akzeptabel": 0.02, "max_akzeptabel": 30.0,
            "einheit": "mol/day", "current": None,
        },
        "temperatur": {
            "min_good": 5.0, "max_good": 25.0,
            "min_akzeptabel": 0.0, "max_akzeptabel": 30.0,
            "einheit": "°C/h", "current": 29.0,
        },
        "salinitaet": {
            "min_good": 0.2, "max_good": 1.0,
            "min_akzeptabel": 0.1, "max_akzeptabel": 1.3,
            "einheit": "mS/cm/h", "current": 0.16,
        },
    }
    job = PlantOptimumJob(speicher, konfig, mock_client)
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 5, 16, 12, 0)))
    achsen = _run(speicher.hole_plant_optima_achsen("mandevilla"))
    assert set(achsen["mandevilla"].keys()) == {
        "feuchte", "licht_ppfd", "licht_dli", "temperatur", "salinitaet",
    }
    assert achsen["mandevilla"]["licht_dli"]["einheit"] == "mol/day"
    assert achsen["mandevilla"]["salinitaet"]["max_good"] == 1.0


def test_schwellen_vorschlag_nutzt_plant_optimum_cache(speicher):
    """T-0049 x T-0050b: wenn Zone keinen Config-Optimum hat, aber Cache
    fuer sie existiert, wird Cache als Optimum verwendet.
    """
    # Zone ohne Config-Optimum
    zonen = [ZonenKonfig(
        zone_id="mandevilla", name="Mandevilla",
        feuchte_schwelle_min=30, feuchte_schwelle_max=70,
    )]
    # Cache setzen
    _run(speicher.speichere_plant_optimum(
        zone_id="mandevilla", feuchte_min=45.0, feuchte_max=65.0,
    ))
    # Dummy-Feuchte-Werte mit breitem Bereich, damit Perzentil-Spanne
    # ausreichend ist und Optimum den min_vorschlag nach oben drueckt.
    from bewaesserung.modelle import DatenQuelle, SensorMessung
    jetzt = datetime(2026, 4, 20, 12, 0)
    for i in range(300):
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=jetzt - timedelta(minutes=i * 10),
            zone_id="mandevilla", geraet_id="fyta_test",
            boden_feuchte=float(20 + (i % 60)),  # 20-79, breit genug
            boden_temperatur=18.0, quelle=DatenQuelle.FYTA,
        )))
    ergebnisse = _run(berechne_vorschlaege_fuer_alle(
        speicher, zonen, jetzt=jetzt,
    ))
    assert len(ergebnisse) == 1
    e = ergebnisse[0]
    # Cache-Optimum 45/65 sollte den empirischen Vorschlag nach oben ziehen
    assert e.quelle == "optimum_dominiert"
    # optimum_min/max im Ergebnis kommt aus Cache
    assert e.optimum_min == 45.0
    assert e.optimum_max == 65.0


def test_schwellen_vorschlag_config_optimum_hat_vorrang_vor_cache(speicher):
    """Wenn Zone einen Config-Optimum hat, wird Cache ignoriert."""
    zonen = [ZonenKonfig(
        zone_id="mandevilla", name="Mandevilla",
        feuchte_schwelle_min=30, feuchte_schwelle_max=70,
        optimum_feuchte_min=50.0, optimum_feuchte_max=75.0,  # Config-Override
    )]
    _run(speicher.speichere_plant_optimum(
        zone_id="mandevilla", feuchte_min=30.0, feuchte_max=60.0,  # Cache hat andere Werte
    ))
    from bewaesserung.modelle import DatenQuelle, SensorMessung
    jetzt = datetime(2026, 4, 20, 12, 0)
    for i in range(300):
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=jetzt - timedelta(minutes=i * 10),
            zone_id="mandevilla", geraet_id="fyta_test",
            boden_feuchte=float(30 + (i % 40)), boden_temperatur=18.0,
            quelle=DatenQuelle.FYTA,
        )))
    ergebnisse = _run(berechne_vorschlaege_fuer_alle(
        speicher, zonen, jetzt=jetzt,
    ))
    e = ergebnisse[0]
    # Config hat Vorrang → optimum_min/max kommen aus Config
    assert e.optimum_min == 50.0
    assert e.optimum_max == 75.0


def test_job_persistiert_current_pro_achse(speicher):
    """T-0196d: PlantOptimumJob speichert `current` aus FYTA-Response
    nicht nur Schwellen. Salinity-Wert ist nur via Plant-Detail-API
    verfuegbar — der Job-Pfad ist die einzige Stelle, wo der gespeichert
    werden kann (Verlaufs-Endpoint liefert salinity nicht)."""
    konfig = _konfig_mit_fyta()
    mock_client = AsyncMock()
    mock_client.hole_plant_optima_alle_achsen.return_value = {
        "salinitaet": {
            "min_good": 0.2, "max_good": 1.0,
            "min_akzeptabel": 0.1, "max_akzeptabel": 1.3,
            "einheit": "mS/cm/h", "current": 0.16,
        },
        "temperatur": {
            "min_good": 5.0, "max_good": 25.0,
            "min_akzeptabel": 0.0, "max_akzeptabel": 30.0,
            "einheit": "°C/h", "current": 29.0,
        },
    }
    job = PlantOptimumJob(speicher, konfig, mock_client)
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 5, 16, 12, 0)))
    achsen = _run(speicher.hole_plant_optima_achsen("mandevilla"))
    assert achsen["mandevilla"]["salinitaet"]["current"] == 0.16
    assert achsen["mandevilla"]["temperatur"]["current"] == 29.0


def test_job_dli_aggregat_aus_ppfd_werten(speicher):
    """T-0196e: DLI (mol/day) wird aus den letzten 24 h `licht`-Werten
    berechnet (Trapez-Regel). Konstantes PPFD=100 μmol/h über 24 h gibt
    DLI = 100 × 86400 / 1e6 = 8.64 mol/day."""
    from bewaesserung.modelle import DatenQuelle, SensorMessung
    konfig = _konfig_mit_fyta()
    jetzt = datetime(2026, 5, 16, 12, 0)
    # 24 stuendliche PPFD=100-Messungen
    for i in range(25):
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=jetzt - timedelta(hours=i),
            zone_id="mandevilla", geraet_id="fyta_test",
            boden_feuchte=50.0, licht=100.0,
            quelle=DatenQuelle.FYTA,
        )))
    mock_client = AsyncMock()
    mock_client.hole_plant_optima_alle_achsen.return_value = {
        "licht_dli": {
            "min_good": 4.0, "max_good": 20.0,
            "min_akzeptabel": 0.02, "max_akzeptabel": 30.0,
            "einheit": "mol/day", "current": None,
        },
    }
    job = PlantOptimumJob(speicher, konfig, mock_client)
    _run(job.aktualisiere_wenn_faellig(jetzt))
    achsen = _run(speicher.hole_plant_optima_achsen("mandevilla"))
    dli = achsen["mandevilla"]["licht_dli"]["current"]
    assert dli is not None
    # PPFD=100 ueber 24 h -> ~8.64 mol/day, tolerant gegen Trapez-Diskretisierung
    assert 8.0 < dli < 9.0
    assert achsen["mandevilla"]["licht_dli"]["quelle"] == "aggregat"


def test_job_dli_keine_werte_speichert_nichts(speicher):
    """T-0196e: Wenn weniger als 2 PPFD-Werte in 24 h, DLI=None und
    keine Speicherung — sonst wuerde der Job einen unsinnigen DLI
    schreiben (gleich 0 oder partial)."""
    konfig = _konfig_mit_fyta()
    mock_client = AsyncMock()
    mock_client.hole_plant_optima_alle_achsen.return_value = {
        "licht_dli": {
            "min_good": 4.0, "max_good": 20.0,
            "min_akzeptabel": None, "max_akzeptabel": None,
            "einheit": "mol/day", "current": None,
        },
    }
    job = PlantOptimumJob(speicher, konfig, mock_client)
    _run(job.aktualisiere_wenn_faellig(datetime(2026, 5, 16, 12, 0)))
    achsen = _run(speicher.hole_plant_optima_achsen("mandevilla"))
    # Schwellen sind da, aber current = None weil kein Aggregat gespeichert
    # (oder noch ueberhaupt nicht eingetragen — beides ok).
    if "licht_dli" in achsen.get("mandevilla", {}):
        assert achsen["mandevilla"]["licht_dli"]["current"] is None
