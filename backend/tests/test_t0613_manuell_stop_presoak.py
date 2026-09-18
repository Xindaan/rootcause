"""T-0613: manuell-stop beendet eine laufende Pre-Soak-Sequenz.

Befund (Gate zu T-0589, B2): `POST /api/ventil/manuell-stop` schloss nur das
Ventil des aktuellen Pulses. `PreSoakManager._tick_lauf` schaltete rein
zeitbasiert weiter und oeffnete zur Sollzeit die Hauptdose. Gleiche
Fehlerklasse wie T-0589 (Notfall-Stopp). Entscheidung: manuell-stop beendet
die Sequenz wie pre-soak-stop ("Stopp heisst Stopp").

Aufbau wie test_t0589_notfall_presoak.py (Wirkung statt Verkabelung): echte
`VentilSicherung` mit Attrappen-Client (kein Netz, keine Hardware), echter
`Speicher` auf temporaerer DB, echte PreSoakManager aus `konfiguriere_api`
und der echte Routen-Handler, direkt im Test-Eventloop aufgerufen. Gemessen
wird, was beim Client ankommt: ein `ventil_oeffnen` nach dem Stopp ist der
Fehler.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import bewaesserung.api_server as api_server
from bewaesserung.modelle import Ausloser
from bewaesserung.speicher import Speicher
from test_t0589_notfall_presoak import (
    BAMBUS_ZONEN,
    KANAL_BAMBUS,
    _dienst,
    _in_pause_bringen,
    _starte_sequenz,
)
from test_ventil_sicherung import ClientAttrappe


@pytest.fixture
async def speicher(tmp_path):
    sp = Speicher(str(tmp_path / "t0613.db"))
    await sp.verbinden()
    try:
        yield sp
    finally:
        await sp.schliessen()


@pytest.mark.parametrize(
    ("phase", "stop_zone"),
    [
        ("pre_soak", "bambuswald"),
        ("pause", "bambuswald"),
        # Geschwister-Zone am selben Kanal: dasselbe Ventil, dieselbe Sequenz.
        ("pre_soak", "bambuswald_yogaraum"),
    ],
)
async def test_manuell_stop_verhindert_hauptdose(speicher, phase, stop_zone):
    """Nach manuell-stop darf der Tick nach Ablauf der Pause kein weiteres
    `ventil_oeffnen` erzeugen. Die Pause-Phase ist drin, weil dort kein
    Ventil offen ist: ein Fix, der nur den offenen Puls beendet, faellt dort
    auf; die Geschwister-Zone, weil der Lauf an `bambuswald` haengt."""
    client = ClientAttrappe()
    sicherung, mgr = _dienst(speicher, client)
    # Mit Kanal-Mapping wie im Betrieb: sonst quittiert ein Stopp auf einen
    # nicht aktiven Kanal still mit True und ein zweiter Cloud-Close bliebe
    # unsichtbar.
    sicherung.setze_kanal_mapping({KANAL_BAMBUS: "valve-bambus"})
    t0 = await _starte_sequenz(mgr)
    if phase == "pause":
        await _in_pause_bringen(sicherung, mgr, t0)
    else:
        assert sicherung.ist_aktiv(KANAL_BAMBUS)
    assert len(client.oeffnen_aufrufe) == 1  # nur der Puls

    antwort = await api_server.ventil_manuell_stop({"zone_id": stop_zone})

    # Hauptdose waere bei t0+30 min faellig.
    await mgr.tick(t0 + timedelta(minutes=31))
    assert len(client.oeffnen_aufrufe) == 1, (
        f"Hauptdose nach manuell-stop geoeffnet: {client.oeffnen_aufrufe}"
    )
    assert not sicherung.ist_aktiv(KANAL_BAMBUS)
    # Genau EIN Close je Puls (im Pause-Fall der Pulsende-Close aus
    # `_in_pause_bringen`): kein zweiter Cloud-Stopp auf den schon
    # geschlossenen Kanal.
    assert len(client.schliessen_aufrufe) == 1, client.schliessen_aufrufe
    assert not mgr.ist_aktiv("bambuswald")
    assert mgr.laufender_lauf("bambuswald").fehler == "Vom User abgebrochen"
    assert antwort["ok"] is True
    assert antwort["pre_soak_beendet"] is True
    assert antwort["pre_soak_zone_id"] == "bambuswald"
    # Kein Neustart-Rest: der persistierte State ist weg.
    assert await speicher.hole_pre_soak_states() == []


async def _yogaraum_lauf_beendet(mgr):
    """Geschwister-Zone faehrt eine eigene Sequenz und bricht sie ab: der
    beendete Lauf bleibt im Manager (`phase == "fehler"`)."""
    ok, fehler = await mgr.starte(
        zone_id="bambuswald_yogaraum", kanal=KANAL_BAMBUS,
        zone_ids_kanal=BAMBUS_ZONEN, pre_soak_min=5, pause_min=30,
        haupt_min=60, ausloser=Ausloser.MANUELL,
    )
    assert ok, fehler
    assert (await mgr.stoppe("bambuswald_yogaraum"))[0]
    assert mgr.laufender_lauf("bambuswald_yogaraum").phase == "fehler"


async def test_manuell_stop_geschwister_mit_eigenem_beendeten_lauf(speicher):
    """Gate W3 B1: Der eigene BEENDETE Lauf der Geschwister-Zone verdeckte die
    laufende Sequenz am selben Kanal. manuell-stop ueber den Yogaraum beendete
    sie nicht, die Hauptdose oeffnete; die Statuskarte zeigte den alten
    Endzustand, waehrend Wasser lief."""
    client = ClientAttrappe()
    sicherung, mgr = _dienst(speicher, client)
    sicherung.setze_kanal_mapping({KANAL_BAMBUS: "valve-bambus"})
    await _yogaraum_lauf_beendet(mgr)
    n_oeffnen = len(client.oeffnen_aufrufe)
    t0 = await _starte_sequenz(mgr)
    assert sicherung.ist_aktiv(KANAL_BAMBUS)

    status = await api_server.ventil_pre_soak_status(zone_id="bambuswald_yogaraum")
    assert [(l["zone_id"], l["fremd"]) for l in status["laeufe"]] == [
        ("bambuswald", True)
    ]

    antwort = await api_server.ventil_manuell_stop({"zone_id": "bambuswald_yogaraum"})
    await mgr.tick(t0 + timedelta(minutes=31))
    assert len(client.oeffnen_aufrufe) == n_oeffnen + 1, (
        f"Hauptdose nach manuell-stop geoeffnet: {client.oeffnen_aufrufe}"
    )
    assert not mgr.ist_aktiv("bambuswald")
    assert antwort["pre_soak_zone_id"] == "bambuswald"


async def test_doppelstart_geschwister_mit_eigenem_beendeten_lauf(speicher):
    """Gate W3 B1, Isomorphie: derselbe Lookup schuetzt `starte` gegen eine
    zweite Sequenz am Kanal (T-0411). In der Pause ist das Ventil zu, der
    Belegt-Guard der VentilSicherung greift dort nicht."""
    client = ClientAttrappe()
    sicherung, mgr = _dienst(speicher, client)
    await _yogaraum_lauf_beendet(mgr)
    t0 = await _starte_sequenz(mgr)
    await _in_pause_bringen(sicherung, mgr, t0)
    ok, fehler = await mgr.starte(
        zone_id="bambuswald_yogaraum", kanal=KANAL_BAMBUS,
        zone_ids_kanal=BAMBUS_ZONEN, pre_soak_min=5, pause_min=30,
        haupt_min=60, ausloser=Ausloser.MANUELL,
    )
    assert not ok, "Zweite Sequenz am selben Kanal waehrend der Pause gestartet"
    assert "auf Zone bambuswald am selben Kanal" in fehler
