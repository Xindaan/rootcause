"""T-0610 EL-4: unbekannte Zonen-Schluessel werden gemeldet, nicht still verworfen.

Ein Tippfehler wie `kanal_trigger_ausschlus` liess den T-0337-Schutz lautlos
auf dem Default (False). Der Loader warnt jetzt; das Parse-Ergebnis bleibt
gleich (kein Abbruch, kein Raten).
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bewaesserung.konfig import lade_konfig

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent.parent / "config" / "default.yaml"
# Im oeffentlichen Snapshot fehlt die echte default.yaml (wie in
# test_t0590_haengender_kanal.py am Dateifehlen erkannt).
OEFFENTLICHER_SNAPSHOT = not DEFAULT_CONFIG.exists()

_YAML = """
gardena:
  client_id: x
  client_secret: y
zonen:
  - zone_id: z1
    name: Z1
    modus: automatik
    feuchte_schwelle_min: 30
    feuchte_schwelle_max: 60
{extra}wetter:
  standorte:
    - id: o
      breite: 52.52
      laenge: 13.405
"""


def _lade(tmp_path, extra: str):
    pfad = tmp_path / "k.yaml"
    pfad.write_text(_YAML.format(extra=extra), encoding="utf-8")
    return lade_konfig(pfad)


def _el4_meldungen(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "EL-4" in r.getMessage()]


def test_tippfehler_im_zonen_schluessel_wird_gemeldet(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    konfig = _lade(tmp_path, "    kanal_trigger_ausschlus: true\n")

    meldungen = _el4_meldungen(caplog)
    assert len(meldungen) == 1
    assert "z1" in meldungen[0] and "kanal_trigger_ausschlus" in meldungen[0]
    # Parse-Ergebnis unveraendert: der Tippfehler wirkt weiterhin nicht.
    assert konfig.zonen[0].kanal_trigger_ausschluss is False


@pytest.mark.skipif(OEFFENTLICHER_SNAPSHOT, reason="oeffentlicher Snapshot: config/default.yaml privat")
def test_bekannte_schluessel_erzeugen_keine_meldung(tmp_path, caplog):
    """Gegenprobe gegen Fehlalarm: korrekt geschriebener Schluessel, und die
    ausgelieferte default.yaml (jeder Tippfehler dort faellt hier auf)."""
    caplog.set_level(logging.WARNING)
    konfig = _lade(tmp_path, "    kanal_trigger_ausschluss: true\n")
    assert konfig.zonen[0].kanal_trigger_ausschluss is True
    lade_konfig(DEFAULT_CONFIG)

    assert _el4_meldungen(caplog) == []
