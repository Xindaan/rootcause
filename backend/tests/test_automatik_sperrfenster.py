"""Automatik-Sperrfenster (Andres Entscheid zur Zeitumstellung 25.10.2026).

In der Nacht der Umstellung (01:30 bis 03:30 Ortszeit) startet die Automatik
nichts; manuelle Starts bleiben offen. Allgemein als Konfig-Liste
`automatik_sperrfenster` mit {von, bis, grund}. Geprueft wird an den Grenzen,
an denen die Sperre wirkt:

1. Konfig-Whitelist (sonst bliebe der YAML-Eintrag stumm) und Fenstergrenzen,
2. der zentrale Waechter in `VentilSicherung.bewaessere` (auch Pre-Soak-Dosen),
3. der Auto-Loop: kein Start, Grund im Entscheidungslog.

Harmlos auch ohne die Waechter: Attrappen-Client (kein Netz), temporaere DB.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bewaesserung.konfig import lade_konfig
from bewaesserung.modelle import (
    Ausloser,
    AutomatikSperrfenster,
    BlockerTyp,
    aktives_sperrfenster,
)
from test_konfig_gesamt_whitelist import _BASIS
from test_t0612_kanal_sperre import (  # noqa: F401  (speicher = Fixture)
    BAMBUS_ZONEN,
    K_BAMBUS,
    VALVES,
    _dienst,
    _geoeffnete_valves,
    _loop_runde,
    _raeume,
    speicher,
)
from test_ventil_sicherung import ClientAttrappe
from bewaesserung import main as M


@pytest.fixture(autouse=True)
def _schneller_loop(monkeypatch):
    """Wie in test_t0612: erste Pruefung sofort (autouse-Fixtures gelten nur
    im eigenen Modul)."""
    monkeypatch.setattr(M, "ERSTE_PRUEFUNG_VERZOEGERUNG_S", 0.01)

NACHT = AutomatikSperrfenster(
    von=datetime(2026, 10, 25, 1, 30), bis=datetime(2026, 10, 25, 3, 30),
    grund="Zeitumstellung",
)


def _jetzt_fenster(aktiv: bool) -> AutomatikSperrfenster:
    jetzt = datetime.now()
    if aktiv:
        return AutomatikSperrfenster(
            von=jetzt - timedelta(minutes=5), bis=jetzt + timedelta(hours=1),
            grund="Testfenster",
        )
    return AutomatikSperrfenster(
        von=jetzt - timedelta(hours=3), bis=jetzt - timedelta(hours=1),
        grund="vorbei",
    )


# --- 1. Konfig und Grenzen ---------------------------------------------------


def test_sperrfenster_kommt_aus_der_yaml(tmp_path):
    pfad = tmp_path / "k.yaml"
    pfad.write_text(_BASIS + (
        "automatik_sperrfenster:\n"
        "  - von: \"2026-10-25T01:30:00\"\n"
        "    bis: \"2026-10-25T03:30:00\"\n"
        "    grund: \"Zeitumstellung\"\n"
    ))
    assert lade_konfig(pfad).automatik_sperrfenster == [NACHT]
    pfad.write_text(_BASIS)
    assert lade_konfig(pfad).automatik_sperrfenster == []


@pytest.mark.parametrize(
    ("zeit", "gesperrt"),
    [
        ("2026-10-25T01:29:59", False),
        ("2026-10-25T01:30:00", True),    # von inklusiv
        ("2026-10-25T02:30:00", True),    # doppelte Stunde (naiv beide Male)
        ("2026-10-25T03:29:59", True),
        ("2026-10-25T03:30:00", False),   # bis exklusiv
    ],
)
def test_fenstergrenzen(zeit, gesperrt):
    treffer = aktives_sperrfenster([NACHT], datetime.fromisoformat(zeit))
    assert (treffer is not None) is gesperrt


@pytest.mark.parametrize(
    ("von", "bis"),
    [
        ("2026-10-25T03:30:00", "2026-10-25T01:30:00"),          # bis vor von
        ("2026-10-25T01:30:00+02:00", "2026-10-25T03:30:00+01:00"),  # Zeitzone
    ],
    ids=["bis_vor_von", "mit_zeitzone"],
)
def test_konfigfehler_werden_abgelehnt(von, bis):
    """Ein falsches Fenster faellt beim Laden auf, statt still nie (oder
    zur falschen Stunde) zu greifen."""
    with pytest.raises(ValueError):
        AutomatikSperrfenster(von=von, bis=bis)


# --- 2. Zentraler Waechter ---------------------------------------------------


@pytest.mark.parametrize(
    ("fenster_aktiv", "ausloser", "oeffnet"),
    [
        (True, Ausloser.AUTOMATIK, False),
        (True, Ausloser.MANUELL, True),
        (False, Ausloser.AUTOMATIK, True),
    ],
    ids=["automatik_gesperrt", "manuell_bleibt_offen", "fenster_vorbei"],
)
async def test_zentraler_waechter_sperrt_nur_die_automatik_im_fenster(
    speicher, fenster_aktiv, ausloser, oeffnet,
):
    client = ClientAttrappe()
    _, sicherung, _ = _dienst(speicher, client)
    sicherung.setze_automatik_sperrfenster([_jetzt_fenster(fenster_aktiv)])
    try:
        ok = await sicherung.bewaessere(
            K_BAMBUS, list(BAMBUS_ZONEN), 600, ausloser,
        )
        assert ok is oeffnet
        assert (_geoeffnete_valves(client) == [VALVES[K_BAMBUS]]) is oeffnet
    finally:
        _raeume(sicherung)


# --- 3. Auto-Loop -------------------------------------------------------------


@pytest.mark.parametrize("fenster_aktiv", [True, False], ids=["aktiv", "vorbei"])
async def test_loop_startet_im_fenster_nichts_und_nennt_den_grund(
    speicher, capsys, fenster_aktiv,
):
    """Die Engine meldet fuer beide Kanaele Bedarf. Im Fenster fragt der
    Loop sie gar nicht erst, oeffnet nichts und schreibt je Kanal eine
    Entscheidungslog-Zeile mit dem Grund. Ausserhalb giesst er normal."""
    client = ClientAttrappe()
    konfig, sicherung, _ = _dienst(speicher, client)
    konfig.automatik_sperrfenster = [_jetzt_fenster(fenster_aktiv)]
    try:
        motor, aus = await _loop_runde(konfig, speicher, sicherung, capsys)
        zeilen = await speicher.hole_entscheidungen(
            blocker_typ=BlockerTyp.AUTOMATIK_SPERRFENSTER.value,
        )
        if fenster_aktiv:
            assert client.oeffnen_aufrufe == []
            assert motor.kanal_fragen == []
            assert "entscheidungsloop.automatik_sperrfenster" in aus
            assert len(zeilen) == 2
            assert all("Testfenster" in z.begruendung for z in zeilen)
            assert all(z.soll_bewaessern is False for z in zeilen)
        else:
            assert client.oeffnen_aufrufe != []
            assert zeilen == []
    finally:
        _raeume(sicherung)
