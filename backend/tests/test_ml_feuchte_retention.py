"""Tests fuer T-0478: entscheidungsgebundener Schutz der Feuchte-Archive.

Der Kern ist die ZONENGENAUIGKEIT (`test_zone_ohne_entscheidung_*`): eine
Zone, die prognostiziert aber nie dosiert, darf ihre Alt-Modelle NICHT
geschuetzt bekommen. Ohne diese Eigenschaft kippt die Schutzquote von
26 % auf 95 % und die Retention waere ein Fuer-immer-Archiv mit falschem
Etikett -- deshalb wird sie hier gegen echtes SQL festgenagelt, nicht
gegen einen Stub.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from bewaesserung.ml.feuchte_retention import (
    Schutzmenge,
    baue_schutzmenge,
    lies_live_ziele,
    ordner_ist_geschuetzt,
    waehle_loeschbar,
    zerlege_version,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "feuchte_retention.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _log(speicher, zeit: datetime, zone: str, version: str) -> None:
    _run(speicher.logge_ml_vorhersage(
        zeitstempel=zeit,
        zone_id=zone,
        horizont_h=6,
        prognose_ziel_zeit=zeit,
        prognose_feuchte=42.0,
        modell_version=version,
    ))


def _entscheidung(speicher, zeit: datetime, zone: str) -> None:
    _run(speicher.speichere_dauer_vorschlag(
        zeitstempel=zeit,
        zone_id=zone,
        f_vor=32.0,
        ziel_schwelle=55.0,
        heuristik_s=1800,
        ml_s=3600,
        ml_modell_version="v1",
        features_json=json.dumps({"f_vor": 32.0}),
        modus="wirksam",
    ))


# --------------------------------------------------------------------
# Zonengenauigkeit -- die Eigenschaft, die das Ergebnis kippt
# --------------------------------------------------------------------

def test_zone_ohne_entscheidung_wird_nicht_geschuetzt(speicher):
    """Die entscheidende Eigenschaft (T-0478).

    `hecke` dosiert am 01.06., `pilea` prognostiziert am selben Tag,
    dosiert aber nie. Nur die hecke-Version darf geschuetzt sein. Ohne
    die `zone_id`-Bedingung im Join waere auch pilea drin -- das ist
    exakt der Unterschied zwischen 26 % und 95 % Schutzquote.
    """
    tag = datetime(2026, 6, 1, 8, 0)
    _log(speicher, tag, "hecke", "hecke/modell_6h_q50_2026-05-30.lgbm")
    _log(speicher, tag, "pilea", "pilea/modell_6h_q50_2026-05-30.lgbm")
    _entscheidung(speicher, datetime(2026, 6, 1, 9, 30), "hecke")

    referenzen = _run(
        speicher.hole_entscheidungsgebundene_feuchte_versionen()
    )

    assert set(referenzen) == {"hecke"}, (
        f"Nur die dosierende Zone darf referenziert sein, war {referenzen}"
    )
    assert referenzen["hecke"] == {"hecke/modell_6h_q50_2026-05-30.lgbm"}
    assert "pilea" not in referenzen

    # Und damit auch keine Datei im pilea-Verzeichnis:
    schutz = baue_schutzmenge(referenzen)
    assert schutz.fuer("pilea") == frozenset()
    assert schutz.fuer("hecke") == {"modell_6h_q50_2026-05-30.lgbm"}


def test_entscheidung_schuetzt_nur_denselben_tag(speicher):
    """Tagesraster: eine Entscheidung am 01.06. schuetzt nicht den 02.06."""
    _log(speicher, datetime(2026, 6, 1, 8, 0), "hecke", "hecke/a.lgbm")
    _log(speicher, datetime(2026, 6, 2, 8, 0), "hecke", "hecke/b.lgbm")
    _entscheidung(speicher, datetime(2026, 6, 1, 23, 59), "hecke")

    referenzen = _run(
        speicher.hole_entscheidungsgebundene_feuchte_versionen()
    )
    assert referenzen == {"hecke": {"hecke/a.lgbm"}}


def test_tagesraster_ignoriert_uhrzeit_abstand(speicher):
    """Bewusst KEIN +/-2-h-Fenster: 00:05 und 23:50 desselben Tages
    zaehlen beide (Entscheidung 01.08., 13 % Unterschied lohnt den
    Fensterjoin nicht)."""
    _log(speicher, datetime(2026, 6, 1, 0, 5), "hecke", "hecke/frueh.lgbm")
    _log(speicher, datetime(2026, 6, 1, 23, 50), "hecke", "hecke/spaet.lgbm")
    _entscheidung(speicher, datetime(2026, 6, 1, 12, 0), "hecke")

    referenzen = _run(
        speicher.hole_entscheidungsgebundene_feuchte_versionen()
    )
    assert referenzen["hecke"] == {"hecke/frueh.lgbm", "hecke/spaet.lgbm"}


def test_ohne_entscheidungen_ist_die_schutzmenge_leer(speicher):
    """Kein `ml_dauer_vorschlag`-Eintrag -> nichts geschuetzt (nicht:
    alles). Der Aufrufer unterscheidet leer von unbekannt selbst."""
    _log(speicher, datetime(2026, 6, 1, 8, 0), "hecke", "hecke/a.lgbm")
    assert _run(
        speicher.hole_entscheidungsgebundene_feuchte_versionen()
    ) == {}


# --------------------------------------------------------------------
# Zerlegung + Vereinigungslogik
# --------------------------------------------------------------------

def test_zerlege_version_trennt_verzeichnis_und_datei():
    assert zerlege_version("hecke/modell_6h_q50_2026-07-29.lgbm") == (
        "hecke", "modell_6h_q50_2026-07-29.lgbm",
    )


def test_zerlege_version_ohne_praefix_ist_verzeichnislos():
    """Legacy-Wurzelverzeichnis vor T-0082 (12 Versionen aus 04/2026)."""
    assert zerlege_version("modell_6h_2026-04-19.lgbm") == (
        None, "modell_6h_2026-04-19.lgbm",
    )


def test_praefixlose_version_schuetzt_in_jedem_verzeichnis():
    """Unzuordenbar -> ueberall schuetzen. Ein unklarer Zustand ist kein
    Loeschgrund."""
    schutz = baue_schutzmenge({"hecke": ["legacy.lgbm", "hecke/neu.lgbm"]})
    assert "legacy.lgbm" in schutz.fuer("hecke")
    assert "legacy.lgbm" in schutz.fuer("pilea")
    assert "neu.lgbm" in schutz.fuer("hecke")
    assert "neu.lgbm" not in schutz.fuer("pilea")


def test_schutzmenge_schluessel_ist_der_praefix_nicht_die_zone():
    """Bei `cluster_id != zone_id` teilen sich Zonen ein Modell. Das
    Verzeichnis steht im Praefix, nicht in der `zone_id` -- sonst wuerde
    im falschen `<name>_archiv` geschuetzt."""
    schutz = baue_schutzmenge({
        "zone_a": ["gemeinsam/modell_6h.lgbm"],
        "zone_b": ["gemeinsam/modell_6h.lgbm"],
    })
    assert schutz.pro_verzeichnis == {
        "gemeinsam": frozenset({"modell_6h.lgbm"}),
    }
    assert schutz.fuer("zone_a") == frozenset()
    assert schutz.fuer("gemeinsam") == {"modell_6h.lgbm"}


def test_leere_und_none_referenzen_werden_uebersprungen():
    schutz = baue_schutzmenge({"hecke": ["", "hecke/echt.lgbm"]})
    assert schutz.fuer("hecke") == {"echt.lgbm"}


# --------------------------------------------------------------------
# Symlink-Ziele (Schutz b)
# --------------------------------------------------------------------

def test_lies_live_ziele_loest_symlinks_auf(tmp_path):
    live = tmp_path / "hecke"
    live.mkdir()
    (live / "modell_6h_q50_2026-07-29.lgbm").write_text("x")
    (live / "aktuell_6h.lgbm").symlink_to("modell_6h_q50_2026-07-29.lgbm")
    (live / "band_scale_6h.json").write_text("{}")

    assert lies_live_ziele(live) == {"modell_6h_q50_2026-07-29.lgbm"}


def test_lies_live_ziele_bei_fehlendem_verzeichnis(tmp_path):
    assert lies_live_ziele(tmp_path / "gibt_es_nicht") == frozenset()


# --------------------------------------------------------------------
# Ordner-Auswahl: Inhalt schlaegt Ordnernamen
# --------------------------------------------------------------------

def _archiv(basis, name, dateien):
    ordner = basis / name
    ordner.mkdir(parents=True)
    for d in dateien:
        (ordner / d).write_text("x")
    return ordner


def test_ordner_wird_ueber_inhalt_erkannt_nicht_ueber_namen(tmp_path):
    """Realfall: `hecke_archiv/2026-07-29_111009/` enthaelt Modelle vom
    2026-07-26. Der Ordnername ist der Swap-Zeitpunkt, die Datei traegt
    das Trainingsdatum -- ein Namensabgleich griffe systematisch daneben.
    """
    basis = tmp_path / "hecke_archiv"
    ordner = _archiv(
        basis, "2026-07-29_111009", ["modell_24h_q50_2026-07-26.lgbm"],
    )
    assert ordner_ist_geschuetzt(
        ordner, {"modell_24h_q50_2026-07-26.lgbm"},
    )
    assert not ordner_ist_geschuetzt(
        ordner, {"modell_24h_q50_2026-07-29.lgbm"},
    )


def test_waehle_loeschbar_schuetzt_referenzierten_alten_ordner(tmp_path):
    """Die Kernanforderung: ein alter Ordner ausserhalb der juengsten N
    bleibt liegen, wenn er eine referenzierte Datei enthaelt."""
    basis = tmp_path / "hecke_archiv"
    for i in range(1, 9):
        _archiv(basis, f"2026-05-{10 + i:02d}_080000", [f"modell_{i}.lgbm"])

    loeschbar = waehle_loeschbar(basis, behalten=5, geschuetzte_dateien={
        "modell_1.lgbm",
    })

    namen = sorted(p.name for p in loeschbar)
    # Ohne Schutz waeren es die drei aeltesten (Index 1,2,3).
    assert namen == ["2026-05-12_080000", "2026-05-13_080000"]


def test_waehle_loeschbar_ohne_schutz_wie_vor_t0478(tmp_path):
    """Leere Schutzmenge = altes T-0219-Verhalten."""
    basis = tmp_path / "kroton_archiv"
    for i in range(1, 9):
        _archiv(basis, f"2026-05-{10 + i:02d}_080000", ["modell.lgbm"])

    loeschbar = waehle_loeschbar(basis, behalten=5, geschuetzte_dateien=set())
    assert len(loeschbar) == 3


def test_waehle_loeschbar_bei_fehlendem_archiv(tmp_path):
    """Fehlendes Archiv darf nicht crashen (Erst-Retrain einer Zone)."""
    assert waehle_loeschbar(
        tmp_path / "gibt_es_nicht", behalten=5, geschuetzte_dateien={"a"},
    ) == []


def test_waehle_loeschbar_bei_leerem_archiv(tmp_path):
    basis = tmp_path / "leer_archiv"
    basis.mkdir()
    assert waehle_loeschbar(basis, behalten=5, geschuetzte_dateien=set()) == []


def test_waehle_loeschbar_ignoriert_dateien_neben_den_ordnern(tmp_path):
    basis = tmp_path / "hecke_archiv"
    for i in range(1, 9):
        _archiv(basis, f"2026-05-{10 + i:02d}_080000", ["modell.lgbm"])
    (basis / "notiz.txt").write_text("kein Versionsordner")

    loeschbar = waehle_loeschbar(basis, behalten=5, geschuetzte_dateien=set())
    assert all(p.is_dir() for p in loeschbar)
    assert len(loeschbar) == 3


def test_schutzmenge_anzahl_zaehlt_kombinationen():
    schutz = baue_schutzmenge({
        "hecke": ["hecke/a.lgbm", "hecke/b.lgbm", "legacy.lgbm"],
        "pilea": ["pilea/c.lgbm"],
    })
    assert schutz.anzahl_versionen == 4


def test_leere_schutzmenge_ist_konstruierbar():
    assert Schutzmenge().fuer("hecke") == frozenset()
    assert Schutzmenge().fuer(None) == frozenset()
