"""T-0539: Response-Feature-Bau im Subprozess, mit Rueckfallweg.

Gemessen ueber 18 Wartungsfenster vom 05.08. bis 13.08.2026: der
Response-Retrain blockierte den Entscheidungsloop im Median **111 % seiner
Laufzeit** (Spanne 79-125 %, ohne eine einzige Ausnahme) -- OBWOHL der Bau
seit T-0403 hinter `asyncio.to_thread` liegt. Ein Thread isoliert nur Code,
der den GIL abgibt; Zeile-fuer-Zeile-Python tut das nicht.
Auswertung: `docs/analyse/t0539_loop_stall/`.

Diese Tests sichern die Schaltlogik drumherum -- aufgebaut wie
`test_t0403_feature_subprozess.py`, weil beide Pfade dasselbe Muster
benutzen sollen und ein abweichender Test genau das verdecken wuerde.
Der Subprozess selbst wird gemockt, damit die Suite weder die Produktiv-DB
liest noch Minuten braucht.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pandas as pd
import pytest

from bewaesserung.ml.response_retrain_job import MlResponseRetrainJob
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    MlBewaesserungsResponseKonfig,
    SpeicherKonfig,
    StandortKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)

JETZT = datetime(2026, 8, 13, 17, 0, 0)
VON = datetime(2025, 8, 13, 17, 0, 0)  # jetzt - 365 Tage


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


def _job(tmp_path, **resp_kwargs) -> MlResponseRetrainJob:
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
    resp = MlBewaesserungsResponseKonfig(aktiv=True, **resp_kwargs)
    return MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=resp,
        basis_verzeichnis=str(tmp_path),
    )


def _in_process_zaehler(job, monkeypatch) -> dict:
    """Ersetzt den In-Process-Weg durch einen Zaehler."""
    zaehler = {"n": 0}

    async def fake(von, bis):
        zaehler["n"] += 1
        return pd.DataFrame({"zone_id": ["a"], "quelle": ["in_process"]})

    monkeypatch.setattr(job, "_trainingsdaten_in_process", fake)
    return zaehler


def _run(coro):
    return asyncio.run(coro)


def test_default_baut_in_process(tmp_path, monkeypatch):
    """Ohne den Schalter bleibt alles wie vorher -- auch fuer Tests, die den
    Job ohne Konfig bauen. Sonst laese die Suite die Produktiv-DB."""
    job = _job(tmp_path)
    zaehler = _in_process_zaehler(job, monkeypatch)
    df = _run(job._baue_trainingsdaten(JETZT))
    assert zaehler["n"] == 1
    assert df["quelle"][0] == "in_process"


def test_subprozess_liefert_den_dataframe(tmp_path, monkeypatch):
    job = _job(tmp_path, feature_bau_subprozess=True)
    zaehler = _in_process_zaehler(job, monkeypatch)
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
    df = _run(job._baue_trainingsdaten(JETZT))

    assert df["quelle"][0] == "subprozess"
    assert zaehler["n"] == 0, "in-process darf nicht zusaetzlich laufen"
    # Das richtige Modul, nicht das des Feuchte-Pfads.
    assert "bewaesserung.ml.response_feature_prozess" in gerufen["args"]
    # Zeitraum wird durchgereicht, nicht neu erfunden: 365 Tage zurueck.
    assert VON.isoformat() in gerufen["args"]
    assert JETZT.isoformat() in gerufen["args"]


def test_subprozess_fehler_faellt_auf_in_process_zurueck(tmp_path, monkeypatch):
    """Ein Retrain, der gar nicht laeuft, waere schlimmer als einer, der den
    Loop einmal ausbremst -- das Modell entscheidet ueber Giessdauern."""
    job = _job(tmp_path, feature_bau_subprozess=True)
    zaehler = _in_process_zaehler(job, monkeypatch)

    async def fake_exec(*args, **kwargs):
        return _ProzessAttrappe(returncode=1, stderr=b"ImportError: kaputt")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    df = _run(job._baue_trainingsdaten(JETZT))

    assert zaehler["n"] == 1
    assert df["quelle"][0] == "in_process"


def test_subprozess_ohne_datei_faellt_zurueck(tmp_path, monkeypatch):
    """Exit 0, aber nichts geschrieben -- der stillste denkbare Ausfall."""
    job = _job(tmp_path, feature_bau_subprozess=True)
    zaehler = _in_process_zaehler(job, monkeypatch)

    async def fake_exec(*args, **kwargs):
        return _ProzessAttrappe(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    df = _run(job._baue_trainingsdaten(JETZT))

    assert zaehler["n"] == 1
    assert df["quelle"][0] == "in_process"


def test_haengender_subprozess_wird_gekillt(tmp_path, monkeypatch):
    """Ohne Timeout haengt der Retrain fuer immer -- und mit ihm der
    Wartungs-Loop, in dem er sitzt."""
    import bewaesserung.ml.response_retrain_job as rj

    monkeypatch.setattr(rj, "SUBPROZESS_TIMEOUT_S", 0.05)
    job = _job(tmp_path, feature_bau_subprozess=True)
    zaehler = _in_process_zaehler(job, monkeypatch)
    prozesse: list[_ProzessAttrappe] = []

    async def fake_exec(*args, **kwargs):
        p = _ProzessAttrappe(haengt=True)
        prozesse.append(p)
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    df = _run(job._baue_trainingsdaten(JETZT))

    assert prozesse[0].gekillt is True
    assert zaehler["n"] == 1
    assert df["quelle"][0] == "in_process"


def test_temp_datei_wird_aufgeraeumt(tmp_path, monkeypatch):
    job = _job(tmp_path, feature_bau_subprozess=True)
    _in_process_zaehler(job, monkeypatch)
    pfade: list[str] = []

    async def fake_exec(*args, **kwargs):
        ziel = args[args.index("--ziel") + 1]
        pfade.append(ziel)
        return _ProzessAttrappe(
            schreibt=lambda: pd.DataFrame({"zone_id": ["a"]}).to_pickle(ziel),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    _run(job._baue_trainingsdaten(JETZT))

    from pathlib import Path
    assert pfade and not Path(pfade[0]).exists(), "Temp-Datei blieb liegen"


@pytest.mark.parametrize("flag", [True, False])
def test_schalter_wirkt_in_beide_richtungen(tmp_path, monkeypatch, flag):
    job = _job(tmp_path, feature_bau_subprozess=flag)
    zaehler = _in_process_zaehler(job, monkeypatch)
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
    _run(job._baue_trainingsdaten(JETZT))

    assert gespawnt["n"] == (1 if flag else 0)
    assert zaehler["n"] == (0 if flag else 1)


def test_fehler_im_bau_bleibt_ein_fehler(tmp_path, monkeypatch):
    """Der Rueckfall darf nur den SUBPROZESS abfangen, nicht den Bau selbst.

    Sonst verschluckt der `except`-Zweig einen echten Datenfehler und der
    Aufrufer (`_baue_trainingsdaten_sicher`) legt ihn nie in
    `_letzter_fehler` ab -- ein stiller Ausfall genau in der Ablage, die
    T-0108 dafuer gebaut hat.
    """
    job = _job(tmp_path, feature_bau_subprozess=True)

    async def kaputt(von, bis):
        raise ValueError("Rohdaten unbrauchbar")

    async def fake_exec(*args, **kwargs):
        return _ProzessAttrappe(returncode=1, stderr=b"weg")

    monkeypatch.setattr(job, "_trainingsdaten_in_process", kaputt)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ValueError):
        _run(job._baue_trainingsdaten(JETZT))
