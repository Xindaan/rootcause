"""T-0403: Feature-Bau im Subprozess, mit Rueckfallweg.

Gemessen am 13.08.2026: `erstelle_trainingsdaten` lief 945 s und blockierte
den Event-Loop dabei zu 82 % der Laufzeit laenger als 100 ms am Stueck --
OBWOHL der Aufruf seit T-0064 hinter `asyncio.to_thread` liegt. Ein Thread
isoliert nur Code, der den GIL abgibt; Zeile-fuer-Zeile-Python tut das nicht.

Diese Tests sichern die Schaltlogik drumherum. Dass der Subprozess selbst
den DataFrame korrekt baut, ist am 13.08. end-to-end gegen die echte DB
geprueft worden (3-Tage-Fenster, 2281 Zeilen); hier wird er gemockt, damit
die Suite weder die Produktiv-DB liest noch Minuten braucht.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pandas as pd
import pytest

from bewaesserung.ml.retrain_job import MlRetrainJob
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    MlRetrainKonfig,
    SpeicherKonfig,
    StandortKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)

VON = datetime(2026, 6, 14)
BIS = datetime(2026, 8, 13)


class _ExtraktorAttrappe:
    """Der In-Process-Weg. Zaehlt, ob er benutzt wurde."""

    def __init__(self):
        self.aufrufe = 0

    async def erstelle_trainingsdaten(self, von, bis):
        self.aufrufe += 1
        return pd.DataFrame({"zone_id": ["a"], "quelle": ["in_process"]})


class _ProzessAttrappe:
    def __init__(self, returncode=0, stdout=b"zeilen=3", stderr=b"",
                 schreibt=None, haengt=False):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._schreibt = schreibt
        self._haengt = haengt
        self.gekillt = False

    async def communicate(self):
        if self._haengt:
            await asyncio.sleep(30)
        if self._schreibt is not None:
            self._schreibt()
        return self._stdout, self._stderr

    def kill(self):
        self.gekillt = True
        self._haengt = False

    async def wait(self):
        return self.returncode


def _job(tmp_path, **retrain_kwargs) -> MlRetrainJob:
    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="t"),
        zonen=[ZonenKonfig(zone_id="a", name="A")],
        wetter=WetterKonfig(standorte=[WetterStandortKonfig(
            id="o", breite=52.52, laenge=13.405,
        )]),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[StandortKonfig(
            standort_id="garten", name="G", wetter_standort="o", zonen=["a"],
        )],
    )
    retrain = MlRetrainKonfig(aktiv=True, **retrain_kwargs)
    return MlRetrainJob(None, konfig, retrain, ausgabe_pfad=str(tmp_path))


def _run(coro):
    return asyncio.run(coro)


def test_default_baut_in_process(tmp_path):
    """Ohne den Schalter bleibt alles wie vorher -- auch in Tests, die den
    Job ohne Konfig bauen. Sonst laese die Suite die Produktiv-DB."""
    job = _job(tmp_path)
    extraktor = _ExtraktorAttrappe()
    df = _run(job._trainingsdaten(extraktor, VON, BIS))
    assert extraktor.aufrufe == 1
    assert df["quelle"][0] == "in_process"


def test_subprozess_liefert_den_dataframe(tmp_path, monkeypatch):
    job = _job(tmp_path, feature_bau_subprozess=True)
    extraktor = _ExtraktorAttrappe()
    gerufen: dict = {}

    async def fake_exec(*args, **kwargs):
        gerufen["args"] = args
        ziel = args[args.index("--ziel") + 1]
        return _ProzessAttrappe(
            schreibt=lambda: pd.DataFrame(
                {"zone_id": ["a"], "quelle": ["subprozess"]},
            ).to_pickle(ziel),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    df = _run(job._trainingsdaten(extraktor, VON, BIS))

    assert df["quelle"][0] == "subprozess"
    assert extraktor.aufrufe == 0, "in-process darf nicht zusaetzlich laufen"
    # Zeitraum wird durchgereicht, nicht neu erfunden.
    assert VON.isoformat() in gerufen["args"]
    assert BIS.isoformat() in gerufen["args"]


def test_subprozess_fehler_faellt_auf_in_process_zurueck(tmp_path, monkeypatch):
    """Ein Retrain, der gar nicht laeuft, waere schlimmer als einer, der
    einmal alle drei Tage das Dashboard ausbremst."""
    job = _job(tmp_path, feature_bau_subprozess=True)
    extraktor = _ExtraktorAttrappe()

    async def fake_exec(*args, **kwargs):
        return _ProzessAttrappe(returncode=1, stderr=b"ImportError: kaputt")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    df = _run(job._trainingsdaten(extraktor, VON, BIS))

    assert extraktor.aufrufe == 1
    assert df["quelle"][0] == "in_process"


def test_subprozess_ohne_datei_faellt_zurueck(tmp_path, monkeypatch):
    """Exit 0, aber nichts geschrieben -- der stillste denkbare Ausfall."""
    job = _job(tmp_path, feature_bau_subprozess=True)
    extraktor = _ExtraktorAttrappe()

    async def fake_exec(*args, **kwargs):
        return _ProzessAttrappe(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    df = _run(job._trainingsdaten(extraktor, VON, BIS))

    assert extraktor.aufrufe == 1
    assert df["quelle"][0] == "in_process"


def test_haengender_subprozess_wird_gekillt(tmp_path, monkeypatch):
    """Ohne Timeout haengt der Retrain fuer immer -- und mit ihm der
    Wartungs-Loop, in dem er sitzt."""
    import bewaesserung.ml.retrain_job as rj

    monkeypatch.setattr(rj, "SUBPROZESS_TIMEOUT_S", 0.05)
    job = _job(tmp_path, feature_bau_subprozess=True)
    extraktor = _ExtraktorAttrappe()
    prozesse: list[_ProzessAttrappe] = []

    async def fake_exec(*args, **kwargs):
        p = _ProzessAttrappe(haengt=True)
        prozesse.append(p)
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    df = _run(job._trainingsdaten(extraktor, VON, BIS))

    assert prozesse[0].gekillt is True
    assert extraktor.aufrufe == 1
    assert df["quelle"][0] == "in_process"


def test_temp_datei_wird_aufgeraeumt(tmp_path, monkeypatch):
    job = _job(tmp_path, feature_bau_subprozess=True)
    pfade: list[str] = []

    async def fake_exec(*args, **kwargs):
        ziel = args[args.index("--ziel") + 1]
        pfade.append(ziel)
        return _ProzessAttrappe(
            schreibt=lambda: pd.DataFrame({"zone_id": ["a"]}).to_pickle(ziel),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    _run(job._trainingsdaten(_ExtraktorAttrappe(), VON, BIS))

    from pathlib import Path
    assert pfade and not Path(pfade[0]).exists(), "Temp-Datei blieb liegen"


@pytest.mark.parametrize("flag", [True, False])
def test_schalter_wirkt_in_beide_richtungen(tmp_path, monkeypatch, flag):
    job = _job(tmp_path, feature_bau_subprozess=flag)
    extraktor = _ExtraktorAttrappe()
    gespawnt = {"n": 0}

    async def fake_exec(*args, **kwargs):
        gespawnt["n"] += 1
        ziel = args[args.index("--ziel") + 1]
        return _ProzessAttrappe(
            schreibt=lambda: pd.DataFrame(
                {"zone_id": ["a"], "quelle": ["subprozess"]},
            ).to_pickle(ziel),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    _run(job._trainingsdaten(extraktor, VON, BIS))

    assert gespawnt["n"] == (1 if flag else 0)
    assert extraktor.aufrufe == (0 if flag else 1)
