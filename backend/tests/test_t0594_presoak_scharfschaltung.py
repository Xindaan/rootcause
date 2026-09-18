"""T-0594: Pre-Soak-Recovery und -Tick respektieren die Scharfschaltung.

Befund (Gesamtreview HW-4, Repro D): `recover_aus_db` lud eine gespeicherte
AUTOMATIK-Sequenz nach einem Neustart, ohne `ventilsteuerung_aktiv` oder
`ist_auto_loop_zone` zu pruefen, und der Tick oeffnete nach der Pause die
Hauptdose. Abschalten heisst Config aendern plus Neustart -- genau dann greift
die Recovery. Manuelle Sequenzen bleiben bewusst unabhaengig vom Schalter.

Aufbau wie test_t0589_notfall_presoak.py: echte `VentilSicherung` mit
Attrappen-Client (kein Netz), echter `Speicher` auf temporaerer DB, echte
PreSoakManager aus `konfiguriere_api`. Gemessen wird, ob beim Client ein
`ventil_oeffnen` ankommt.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import bewaesserung.api_server as api_server
from bewaesserung.api_server import konfiguriere_api
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.pre_soak import AUTOMATIK_ABGESCHALTET
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import VentilSicherung
from test_ventil_sicherung import ClientAttrappe

DSWC = "dswc-1"
KANAL = 2
ZONEN = ["bambuswald", "bambuswald_yogaraum"]

# Die drei Stufen der Scharfschaltung (T-0334) einzeln abgeschaltet, plus der
# scharfe Fall als Kontrolle.
SCHARF = dict(master=True, opt_in=True, modus=ZonenModus.AUTOMATIK)
UNSCHARF = {
    "master_aus": dict(SCHARF, master=False),
    "opt_in_aus": dict(SCHARF, opt_in=False),
    "monitoring": dict(SCHARF, modus=ZonenModus.MONITORING),
}


def _konfig(master: bool, opt_in: bool, modus: ZonenModus) -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        ventilsteuerung_aktiv=master,
        zonen=[
            ZonenKonfig(
                zone_id=z, name=z, ventil_kanal=KANAL,
                auto_loop_opt_in=opt_in, modus=modus,
            )
            for z in ZONEN
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
    )


def _dienst(speicher: Speicher, client: ClientAttrappe, **schalter):
    """Baut Sicherung + Manager wie der Dienst beim Start (konfiguriere_api)."""
    sicherung = VentilSicherung(
        client=client,  # type: ignore[arg-type]
        speicher=speicher,
        ventil_geraet_id="ventil-001",
    )
    konfiguriere_api(
        speicher, _konfig(**schalter), MagicMock(), MagicMock(),
        ventil_sicherungen={DSWC: sicherung},
    )
    return sicherung, api_server._laufzeit.pre_soak_managers[DSWC]


async def _gespeicherte_sequenz(speicher: Speicher, ausloser: str) -> datetime:
    """Repro D: Sequenz in der Pause, 31 min alt, Hauptdose ab 30 min faellig."""
    gestartet = datetime.now() - timedelta(minutes=31)
    await speicher.setze_pre_soak_state(
        zone_id="bambuswald", kanal=KANAL, zone_ids_kanal=ZONEN,
        pre_soak_s=300, pause_s=1800, haupt_s=3600,
        gestartet_am=gestartet, phase="pause", ausloser=ausloser,
    )
    return gestartet


@pytest.fixture
async def speicher(tmp_path):
    sp = Speicher(str(tmp_path / "t0594.db"))
    await sp.verbinden()
    try:
        yield sp
    finally:
        await sp.schliessen()


@pytest.mark.parametrize("schalter", list(UNSCHARF), ids=list(UNSCHARF))
async def test_auto_sequenz_giesst_nach_neustart_ohne_scharfschaltung_nicht(
    speicher, schalter,
):
    """Neustart mit abgeschalteter Automatik: weder die Recovery noch ein
    spaeterer Tick darf die Hauptdose oeffnen; die Sequenz endet sichtbar."""
    t0 = await _gespeicherte_sequenz(speicher, "automatik")
    client = ClientAttrappe()
    _, mgr = _dienst(speicher, client, **UNSCHARF[schalter])

    n = await mgr.recover_aus_db()
    await mgr.tick(t0 + timedelta(minutes=35))

    assert client.oeffnen_aufrufe == [], (
        f"Auto-Sequenz trotz abgeschalteter Automatik fortgesetzt: "
        f"{client.oeffnen_aufrufe}"
    )
    assert n == 0
    lauf = mgr.laufender_lauf("bambuswald")
    assert lauf is not None, "Abbruch muss im Status sichtbar bleiben"
    assert lauf.phase == "fehler"
    assert lauf.fehler == AUTOMATIK_ABGESCHALTET
    assert await speicher.hole_pre_soak_states() == []


async def test_auto_sequenz_laeuft_bei_scharfer_automatik_weiter(speicher):
    """Kontrolle: derselbe Aufbau mit scharfer Automatik oeffnet die
    Hauptdose. Ohne diesen Fall waere ein Gate, das alles blockt, gruen."""
    await _gespeicherte_sequenz(speicher, "automatik")
    client = ClientAttrappe()
    _, mgr = _dienst(speicher, client, **SCHARF)

    n = await mgr.recover_aus_db()

    assert [d for _, d, _ in client.oeffnen_aufrufe] == [3600]
    assert n == 1


async def test_manuelle_sequenz_bleibt_unabhaengig_vom_schalter(speicher):
    """Manuelle Pre-Soaks laufen bewusst auch bei abgeschalteter Automatik
    weiter (T-0110: das Flag steuert nur den Auto-Loop)."""
    await _gespeicherte_sequenz(speicher, "manuell")
    client = ClientAttrappe()
    _, mgr = _dienst(speicher, client, **UNSCHARF["master_aus"])

    n = await mgr.recover_aus_db()

    assert [d for _, d, _ in client.oeffnen_aufrufe] == [3600]
    assert n == 1
