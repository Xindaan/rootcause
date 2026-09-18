"""T-0597: Manueller Pre-Soak-Start uebernimmt haupt_pulse/haupt_puls_pause_min.

Befund (Gesamtreview API-3): `/api/ventil/pre-soak-start` gab die Puls-Felder
der Zone nicht an `PreSoakManager.starte()` weiter, der Manager fiel auf
1 Puls zurueck. Fuer waldblumenhain (3 Pulse, 21 min Pause) lief die
90-min-Hauptdose am Stueck. Die Automatik (main.py) uebergab sie schon.

Aufbau (Wirkung statt Verkabelung): echte `VentilSicherung` mit Attrappen-
Client (kein Netz), echter `Speicher` auf temporaerer DB, echter
PreSoakManager aus `konfiguriere_api`, echter Routen-Handler im Test-
Eventloop. Gemessen wird die Dauer, mit der der erste Haupt-Puls beim Client
ankommt: 1800 s (90 min / 3) statt 5400 s am Stueck.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

import bewaesserung.api_server as api_server
from bewaesserung.api_server import konfiguriere_api
from bewaesserung.modelle import (
    Ausloser,
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import VentilSicherung
from test_ventil_sicherung import ClientAttrappe

DSWC = "dswc-1"
KANAL = 1


class _MotorAttrappe:
    """Nur was der Pfad braucht: Tagesverbrauch (Budget-Advisory) und den
    Max-Stop fuer das Puls-Gate (nie stoppen)."""

    async def tagesverbrauch(self, _zone_id: str) -> float:
        return 0.0

    async def pruefe_kanal_max_stop(self, _kanal, _zonen, **_kw):
        return False, None


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[
            ZonenKonfig(
                zone_id="waldblumenhain", name="Waldblumen", ventil_kanal=KANAL,
                pre_soak_min=5, pre_soak_pause_min=25,
                haupt_pulse=3, haupt_puls_pause_min=21,
                max_dauer_sekunden=5400,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
    )


@pytest.fixture
async def speicher(tmp_path):
    sp = Speicher(str(tmp_path / "t0597.db"))
    await sp.verbinden()
    try:
        yield sp
    finally:
        await sp.schliessen()


@pytest.mark.parametrize(
    ("body_extra", "pulse", "pause_s", "erster_haupt_s"),
    [
        # Default aus der Zone: 3 Pulse a 30 min, 21 min Soak-Pause.
        ({}, 3, 21 * 60, 1800),
        # Per Body ueberschrieben: am Stueck.
        ({"haupt_pulse": 1, "haupt_puls_pause_min": 0}, 1, 0, 5400),
    ],
    ids=["zone_default", "body_override"],
)
async def test_manueller_pre_soak_teilt_hauptdose_wie_die_zone(
    speicher, body_extra, pulse, pause_s, erster_haupt_s,
):
    client = ClientAttrappe()
    sicherung = VentilSicherung(
        client=client,  # type: ignore[arg-type]
        speicher=speicher,
        ventil_geraet_id="ventil-001",
    )
    konfiguriere_api(
        speicher, _konfig(), _MotorAttrappe(), MagicMock(),  # type: ignore[arg-type]
        ventil_sicherungen={DSWC: sicherung},
    )
    mgr = api_server._laufzeit.pre_soak_managers[DSWC]

    antwort = await api_server.ventil_pre_soak_start(
        {"zone_id": "waldblumenhain", "haupt_min": 90, **body_extra},
    )
    assert antwort["ok"] is True, antwort
    assert antwort["haupt_pulse"] == pulse

    lauf = mgr.laufender_lauf("waldblumenhain")
    assert lauf.haupt_pulse == pulse
    assert lauf.haupt_pause_s == pause_s
    # Puls-Ende wie in echt (Cloud schliesst), dann Hauptdose faellig ab 25 min.
    assert await sicherung.stoppe(KANAL, Ausloser.MANUELL)
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=26))

    dauern = [d for _, d, _ in client.oeffnen_aufrufe]
    assert dauern == [300, erster_haupt_s], dauern
