"""T-0610 AT-5: Versionswaechter fuer die py-smart-gardena-Laufzeit-Patches.

Die Patches in `gardena_client.py` ersetzen Methoden der Lib 1.3.17 als
Ganzes. `pyproject.toml` erlaubt jede Version ab 0.7; ein Update koennte die
Patches still wirkungslos machen. Der Waechter blockiert nichts, er muss die
Abweichung aber laut melden -- und bei der geprueften Version still bleiben,
sonst wird die Warnung zum Rauschen.
"""
from __future__ import annotations

import importlib.metadata

import structlog

from bewaesserung import gardena_client


def _warnungen(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e.get("event") == "gardena.lib_version_ungeprueft"]


def test_andere_lib_version_wird_gemeldet(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.3.18")
    with structlog.testing.capture_logs() as logs:
        ergebnis = gardena_client.pruefe_gardena_lib_version()
    assert ergebnis is False
    w = _warnungen(logs)
    assert len(w) == 1
    assert w[0]["installiert"] == "1.3.18"
    assert w[0]["geprueft"] == gardena_client.GEPRUEFTE_GARDENA_LIB_VERSION


def test_fehlende_lib_wird_gemeldet(monkeypatch):
    def _fehlt(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _fehlt)
    with structlog.testing.capture_logs() as logs:
        assert gardena_client.pruefe_gardena_lib_version() is False
    assert [e["installiert"] for e in _warnungen(logs)] == [None]


def test_gepruefte_lib_version_bleibt_still(monkeypatch):
    monkeypatch.setattr(
        importlib.metadata, "version",
        lambda name: gardena_client.GEPRUEFTE_GARDENA_LIB_VERSION,
    )
    with structlog.testing.capture_logs() as logs:
        assert gardena_client.pruefe_gardena_lib_version() is True
    assert _warnungen(logs) == []


def test_unerwartete_ausnahme_beim_versionslesen_bricht_nicht(monkeypatch):
    """T-0610 BC-3: Der Waechter laeuft beim Modul-Import. Wirft
    importlib.metadata.version etwas anderes als PackageNotFoundError, muss er
    warnen statt die Ausnahme weiterzureichen (sonst bricht der Import des
    Clients und damit der Dienststart)."""
    def _kaputt(name):
        raise ValueError("defekte Metadaten")

    monkeypatch.setattr(importlib.metadata, "version", _kaputt)
    with structlog.testing.capture_logs() as logs:
        assert gardena_client.pruefe_gardena_lib_version() is False
    w = _warnungen(logs)
    assert len(w) == 1
    assert w[0]["installiert"] is None
    assert "defekte Metadaten" in w[0]["fehler"]
