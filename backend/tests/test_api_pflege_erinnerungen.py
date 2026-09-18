"""T-0228 Stufe 1: Pflege-/Wartungs-Erinnerungen.

Ersetzt das `arbeitspattern_conditional_trigger_calendar.md`-Pattern
(heute Claude-Anweisung "scanne TASK.md gegen heute") durch
persistierten System-State.

Regression-Schutz:
- Anlegen + Auslesen.
- `anstehend_tage`-Filter (nur Eintraege mit faellig_am <= jetzt+N).
- `erledige_pflege_erinnerung` markiert + legt bei `intervall_tage`
  einen Folge-Eintrag an (Verschiebung um Intervall).
- API-CRUD: GET-Liste, POST-Anlegen, POST-/erledigen.
- Auth-Matrix: GET=read, POST=control.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    StandortKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[ZonenKonfig(zone_id="zitrus", name="Zitrus", ventil_kanal=1)],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="haus", name="Haus", wetter_standort="o",
                zonen=["zitrus"],
            ),
        ],
    )


# --- Speicher-Tests ---------------------------------------------------------

def test_speichere_und_hole_pflege_erinnerung(tmp_path):
    s = Speicher(str(tmp_path / "p.db"))
    _run(s.verbinden())
    try:
        eid = _run(s.speichere_pflege_erinnerung(
            typ="beobachtung",
            faellig_am=datetime(2026, 6, 6, 12, 0),
            beschreibung="zitrus Substrat-Stau Nachschau",
            zone_id="zitrus",
            quelle="memory",
        ))
        assert eid > 0
        liste = _run(s.hole_pflege_erinnerungen())
        assert len(liste) == 1
        assert liste[0]["zone_id"] == "zitrus"
        assert liste[0]["quelle"] == "memory"
        assert liste[0]["erledigt_am"] is None
    finally:
        _run(s.schliessen())


def test_anstehend_tage_filter(tmp_path):
    """Nur Eintraege mit faellig_am <= jetzt + N kommen durch."""
    s = Speicher(str(tmp_path / "p.db"))
    _run(s.verbinden())
    try:
        jetzt = datetime(2026, 5, 25, 12, 0)
        # In 2 Tagen
        _run(s.speichere_pflege_erinnerung(
            typ="kalibrierung", faellig_am=jetzt + timedelta(days=2),
            beschreibung="bald", jetzt=jetzt,
        ))
        # In 30 Tagen
        _run(s.speichere_pflege_erinnerung(
            typ="kalibrierung", faellig_am=jetzt + timedelta(days=30),
            beschreibung="spaeter", jetzt=jetzt,
        ))
        # Vorlauf 3 Tage -> nur der erste
        liste = _run(s.hole_pflege_erinnerungen(
            anstehend_tage=3, jetzt=jetzt,
        ))
        assert len(liste) == 1
        assert liste[0]["beschreibung"] == "bald"
        # Vorlauf 60 Tage -> beide
        liste_alle = _run(s.hole_pflege_erinnerungen(
            anstehend_tage=60, jetzt=jetzt,
        ))
        assert len(liste_alle) == 2
    finally:
        _run(s.schliessen())


def test_erledige_einmalig_keine_folge(tmp_path):
    s = Speicher(str(tmp_path / "p.db"))
    _run(s.verbinden())
    try:
        eid = _run(s.speichere_pflege_erinnerung(
            typ="beobachtung",
            faellig_am=datetime(2026, 6, 6, 12, 0),
            beschreibung="einmalig",
        ))
        folge = _run(s.erledige_pflege_erinnerung(eid))
        assert folge is None
        offen = _run(s.hole_pflege_erinnerungen())
        assert offen == []
        # Auch mit nur_offen=False zeigt sich der Eintrag mit erledigt_am
        alle = _run(s.hole_pflege_erinnerungen(nur_offen=False))
        assert len(alle) == 1
        assert alle[0]["erledigt_am"] is not None
    finally:
        _run(s.schliessen())


def test_erledige_wiederkehrend_legt_folge_an(tmp_path):
    """`intervall_tage=90` -> nach Erledigen taucht naechster Eintrag
    mit faellig_am += 90d auf."""
    s = Speicher(str(tmp_path / "p.db"))
    _run(s.verbinden())
    try:
        eid = _run(s.speichere_pflege_erinnerung(
            typ="batterie_check",
            faellig_am=datetime(2026, 6, 1, 12, 0),
            beschreibung="FYTA-Batterie",
            intervall_tage=90,
        ))
        folge = _run(s.erledige_pflege_erinnerung(eid))
        assert folge is not None
        assert folge["intervall_tage"] == 90
        # Folge-Datum = Original-faellig + 90 Tage
        folge_dt = datetime.fromisoformat(folge["faellig_am"])
        assert folge_dt.date() == datetime(2026, 8, 30).date()
        # Offene Liste hat nun den Folge-Eintrag
        offen = _run(s.hole_pflege_erinnerungen())
        assert len(offen) == 1
        assert offen[0]["id"] == folge["id"]
    finally:
        _run(s.schliessen())


def test_doppel_erledigen_idempotent(tmp_path):
    s = Speicher(str(tmp_path / "p.db"))
    _run(s.verbinden())
    try:
        eid = _run(s.speichere_pflege_erinnerung(
            typ="t", faellig_am=datetime(2026, 6, 6),
            beschreibung="x", intervall_tage=30,
        ))
        f1 = _run(s.erledige_pflege_erinnerung(eid))
        # Zweiter Erledigen-Call darf KEINEN neuen Folge anlegen
        f2 = _run(s.erledige_pflege_erinnerung(eid))
        assert f1 is not None
        assert f2 is None
        # Insgesamt 2 Eintraege (Original + 1 Folge), nicht 3
        alle = _run(s.hole_pflege_erinnerungen(nur_offen=False))
        assert len(alle) == 2
    finally:
        _run(s.schliessen())


# --- API-Tests --------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    s = Speicher(str(tmp_path / "api.db"))
    _run(s.verbinden())
    konfiguriere_api(s, _konfig(), MagicMock(), MagicMock())
    c = TestClient(app)
    try:
        yield c
    finally:
        c.close()
        _run(s.schliessen())


def test_api_liste_leer(client):
    r = client.get("/api/pflege-erinnerungen")
    assert r.status_code == 200
    assert r.json() == {"eintraege": []}


def test_api_anlegen_lesen_erledigen_roundtrip(client):
    # POST
    r = client.post("/api/pflege-erinnerungen", json={
        "typ": "beobachtung",
        "faellig_am": "2026-06-06T12:00:00",
        "beschreibung": "zitrus Substrat-Stau Nachschau",
        "zone_id": "zitrus",
        "quelle": "memory",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    eid = body["id"]
    # GET
    r = client.get("/api/pflege-erinnerungen?anstehend_tage=30")
    eintraege = r.json()["eintraege"]
    assert len(eintraege) == 1
    assert eintraege[0]["beschreibung"] == "zitrus Substrat-Stau Nachschau"
    # POST /erledigen
    r = client.post(f"/api/pflege-erinnerungen/{eid}/erledigen")
    assert r.status_code == 200
    assert r.json()["folge"] is None  # einmalig
    # Offene Liste leer
    r = client.get("/api/pflege-erinnerungen")
    assert r.json()["eintraege"] == []


def test_api_anlegen_validiert_pflichtfelder(client):
    r = client.post("/api/pflege-erinnerungen", json={"typ": ""})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "Pflichtfelder" in r.json()["fehler"]


def test_api_anlegen_validiert_iso_datum(client):
    r = client.post("/api/pflege-erinnerungen", json={
        "typ": "x", "faellig_am": "morgen vormittag",
    })
    assert r.json()["ok"] is False
    assert "ISO" in r.json()["fehler"]
