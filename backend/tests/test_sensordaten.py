"""Tests fuer SensorDatenVerarbeiter.

Fokus: Plausibilitaets-Filter, insbesondere der gardena+humidity-None-
Filter (Cloud-Reporting-Artefakt) — Spiegel-Pendant zum DHS-Backfill-
Skip in `sensor_dhs_backfill._parse_messungen`. Beide Pfade muessen
identisch verwerfen, damit kein NULL-Feuchte-Punkt mehr ins
`sensor_messung`-Schema gelangt und das Frontend-Diagramm reisst.
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from bewaesserung.modelle import DatenQuelle, SensorMessung
from bewaesserung.sensordaten import SensorDatenVerarbeiter
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "sensordaten.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _messung(
    *,
    quelle: DatenQuelle = DatenQuelle.GARDENA,
    feuchte: float | None = 55.0,
    temp: float | None = 18.0,
    zone: str = "bambuswald",
) -> SensorMessung:
    return SensorMessung(
        zeitstempel=datetime(2026, 4, 26, 18, 14, 0),
        zone_id=zone,
        geraet_id="dev-1",
        boden_feuchte=feuchte,
        boden_temperatur=temp,
        quelle=quelle,
    )


def test_plausibel_normalfall(speicher):
    v = SensorDatenVerarbeiter(speicher)
    assert v._ist_plausibel(_messung()) is True


def test_unplausibel_gardena_feuchte_none_temperatur_only(speicher):
    """Reproduziert den 25.04.-Fall: gardena schickt nur Temperatur ohne Feuchte.

    Solche Cloud-Reporting-Artefakte entstanden bisher 1x/Sensor/Tag und
    fuehrten zu boden_feuchte=NULL-Zeilen, die das Frontend-Feuchte-
    Diagramm als Luecke gerendert hat. Filter wirft sie raus, der
    naechste Voll-Tick (humidity vorhanden) landet sauber in der DB.
    """
    v = SensorDatenVerarbeiter(speicher)
    m = _messung(quelle=DatenQuelle.GARDENA, feuchte=None, temp=14.0)
    assert v._ist_plausibel(m) is False


def test_plausibel_fyta_feuchte_none_durchgelassen(speicher):
    """FYTA-Quelle bleibt unangetastet — dort liefert die API immer Feuchte.

    Falls FYTA jemals doch eine Telemetrie ohne Feuchte schickt, soll der
    Datensatz nicht still verworfen werden, sondern erstmal landen. Die
    Filter-Regel zielt explizit auf das gardena-Cloud-Verhalten.
    """
    v = SensorDatenVerarbeiter(speicher)
    m = _messung(quelle=DatenQuelle.FYTA, feuchte=None, temp=18.0)
    assert v._ist_plausibel(m) is True


def test_verarbeite_skipt_speichern_bei_unplausibler_messung(speicher):
    """End-to-End: temperature-only Update von Gardena landet NICHT in der DB."""
    v = SensorDatenVerarbeiter(speicher)
    speicher.speichere_messung = AsyncMock()
    m = _messung(quelle=DatenQuelle.GARDENA, feuchte=None, temp=12.0)
    _run(v.verarbeite(m))
    speicher.speichere_messung.assert_not_called()


def test_verarbeite_speichert_bei_plausibler_messung(speicher):
    """End-to-End: vollstaendige Messung landet in der DB."""
    v = SensorDatenVerarbeiter(speicher)
    speicher.speichere_messung = AsyncMock()
    m = _messung()
    _run(v.verarbeite(m))
    speicher.speichere_messung.assert_called_once()
