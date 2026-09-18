"""T-0602 (Gesamtreview EL-9): Prognose und Trend lesen den Aggregat-Lead.

Realfall waldblumenhain 25./26.09.2026: Gardena-Lead 45 % gegen Schwelle 40,
die Engine meldete FEUCHTE_OK; die FYTA-Sensoren derselben Zone (11-14, andere
Skala) lieferten den JUENGSTEN Wert, und `prognostiziere_bewaesserung` meldete
"Schwelle bereits erreicht oder unterschritten" (Triage-Strip, HeutePlan, Log).

Echter `Speicher` mit temporaerer DB, damit der Geraete-Filter tatsaechlich in
der Abfrage wirkt und nicht in einer Attrappe.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from bewaesserung.entscheidung import Entscheidungsmotor
from bewaesserung.modelle import (
    SensorMessung,
    WetterStunde,
    WetterVorhersage,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.speicher import Speicher

JETZT = datetime(2026, 9, 25, 19, 35)
ZONE = "waldblumenhain"
LEAD = "aaaa0001-lead"
FYTA = "fyta_900001"


class _Wetter:
    def __init__(self):
        self.regen_schwelle_mm = 2.0
        self.standard_client = self

    def hole_client(self, standort_id):
        return self

    async def hole_vorhersage(self):
        return WetterVorhersage(
            abfrage_zeitstempel=JETZT,
            stunden=[
                WetterStunde(
                    zeitstempel=JETZT + timedelta(hours=i + 1),
                    temperatur=15.0, niederschlag_mm=0.0,
                    niederschlag_wahrscheinlichkeit=0.0, wind_kmh=5.0,
                    et0_mm=0.0,
                )
                for i in range(48)
            ],
        )


def _messung(vor_min: int, feuchte: float, geraet: str) -> SensorMessung:
    return SensorMessung(
        zeitstempel=JETZT - timedelta(minutes=vor_min),
        zone_id=ZONE, geraet_id=geraet, boden_feuchte=feuchte,
    )


async def _prognose(tmp_path):
    speicher = Speicher(str(tmp_path / "t0602.db"))
    await speicher.verbinden()
    try:
        # Lead faellt langsam (46 -> 45 in 5 h), FYTA liegt auf eigener
        # Skala bei 12 und ist JUENGER als der letzte Lead-Wert.
        for vor_min, wert in ((360, 46.0), (180, 45.5), (60, 45.0)):
            await speicher.speichere_messung(_messung(vor_min, wert, LEAD))
        for vor_min in (300, 120, 45, 10):
            await speicher.speichere_messung(_messung(vor_min, 12.0, FYTA))
        speicher.setze_aggregat_lead({ZONE: LEAD})
        zone = ZonenKonfig(
            zone_id=ZONE, name="Waldblumenhain", modus=ZonenModus.AUTOMATIK,
            feuchte_schwelle_min=40.0, feuchte_kritisch=25.0,
        )
        motor = Entscheidungsmotor(speicher, _Wetter(), [zone])
        motor._jetzt = lambda: JETZT
        return await motor.prognostiziere_bewaesserung(ZONE)
    finally:
        await speicher.schliessen()


def test_prognose_folgt_dem_lead_nicht_dem_juengsten_fremdsensor(tmp_path):
    zeitpunkt, begruendung = asyncio.run(_prognose(tmp_path))
    assert begruendung != "Schwelle bereits erreicht oder unterschritten"
    # Lead 45 %, Trend -0,2 %/h, Schwelle 40 -> rund 25 h. Ein mit dem
    # FYTA gemischter Trend waere steil fallend und laege weit darunter.
    assert zeitpunkt is not None and zeitpunkt > JETZT + timedelta(hours=12), (
        zeitpunkt, begruendung,
    )
