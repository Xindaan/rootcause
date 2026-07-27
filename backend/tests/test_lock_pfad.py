"""Tests fuer T-0127 (H-3a) Single-Instance-Lock-Pfad.

Deckt ab:
- ENV-Override `BEWAESSERUNG_LOCK_PFAD` -> exakt der Pfad wird genutzt.
- Default ohne Override liegt unter ~/Library/Application Support/...
  (nicht unter /tmp -- Sleep/Wake-Resilienz).
- Erwerb erstellt das Verzeichnis bei Bedarf.
- Zweiter Erwerb mit demselben Pfad scheitert mit RuntimeError + zeigt
  die alte PID an + nennt den Pfad in der "rm ..."-Anweisung.
"""
from __future__ import annotations

import os

import pytest

from bewaesserung import main as bm


@pytest.fixture(autouse=True)
def reset_lock_globale():
    """Stellt sicher, dass jeder Test mit frischem Lock-State startet."""
    if bm._lock_handle is not None:
        try:
            bm._lock_handle.close()
        except Exception:
            pass
    bm._lock_handle = None
    yield
    if bm._lock_handle is not None:
        try:
            bm._lock_handle.close()
        except Exception:
            pass
    bm._lock_handle = None


def test_default_pfad_liegt_in_library_application_support(monkeypatch):
    monkeypatch.delenv("BEWAESSERUNG_LOCK_PFAD", raising=False)
    pfad = bm._ermittle_lock_pfad()
    teile = pfad.parts
    assert "Library" in teile
    assert "Application Support" in teile
    assert pfad.name == "bewaesserung.pid"
    # Der entscheidende Punkt: nicht /tmp, weil Sleep/Wake das aufraeumt.
    assert not str(pfad).startswith("/tmp/")


def test_env_override_wird_genutzt(tmp_path, monkeypatch):
    override = tmp_path / "custom.pid"
    monkeypatch.setenv("BEWAESSERUNG_LOCK_PFAD", str(override))
    assert bm._ermittle_lock_pfad() == override


def test_erwerb_legt_verzeichnis_an_und_schreibt_pid(tmp_path, monkeypatch):
    lock_pfad = tmp_path / "neuer_unterordner" / "bewaesserung.pid"
    assert not lock_pfad.parent.exists()
    monkeypatch.setenv("BEWAESSERUNG_LOCK_PFAD", str(lock_pfad))

    bm._erwerbe_single_instance_lock()

    assert lock_pfad.parent.is_dir()
    assert lock_pfad.exists()
    assert lock_pfad.read_text().strip() == str(os.getpid())


def test_zweiter_erwerb_mit_gleichem_pfad_wirft_runtime_error(tmp_path, monkeypatch):
    lock_pfad = tmp_path / "shared.pid"
    monkeypatch.setenv("BEWAESSERUNG_LOCK_PFAD", str(lock_pfad))

    # Erster Erwerb belegt das Lock. Wir SIMULIEREN einen zweiten Prozess,
    # indem wir das Modul-Globale-Handle erhalten lassen UND einen neuen
    # File-Descriptor auf denselben Pfad oeffnen + flock versuchen.
    bm._erwerbe_single_instance_lock()
    erstes_handle = bm._lock_handle
    assert erstes_handle is not None

    # Globale resetten, damit der zweite "Erwerb" einen neuen FD nimmt
    # (sonst greift flock auf demselben FD -- gleicher Owner, kein Konflikt).
    bm._lock_handle = None

    with pytest.raises(RuntimeError) as excinfo:
        bm._erwerbe_single_instance_lock()

    fehler = str(excinfo.value)
    assert str(os.getpid()) in fehler   # alte PID sichtbar
    assert str(lock_pfad) in fehler     # Pfad in rm-Hinweis korrekt

    # Aufraeumen: erstes Handle geben wir frei, damit autouse-Fixture
    # nicht versucht ein bereits geschlossenes File zu schliessen.
    erstes_handle.close()
