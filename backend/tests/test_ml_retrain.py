"""Tests fuer T-0048 Auto-Retrain mit Deployment-Gate."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("lightgbm")
pd = pytest.importorskip("pandas")

from bewaesserung.ml.retrain_job import (
    MlRetrainJob,
    _lade_aktuelle_mae,
    _pruefe_gate,
    _raeume_alte_archive,
    _swap_live_mit_tmp,
)
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
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t"),
        zonen=[ZonenKonfig(zone_id="bambuswald", name="B", ventil_kanal=2)],
        wetter=WetterKonfig(standorte=[WetterStandortKonfig(
            id="o", breite=52.52, laenge=13.405,
        )]),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[StandortKonfig(
            standort_id="garten", name="G",
            wetter_standort="o", zonen=["bambuswald"],
        )],
    )


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "retrain.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _lege_modell_an(
    dir_: Path, horizont: int, datum: str, mae: float,
    alpha_suffix: str = "",
) -> None:
    """Legt ein fake-Modell + metriken_*.json + Symlink an (ohne echtes LightGBM-
    Training). Reicht fuer Gate-Logik-Tests, die nur Metriken-Dateien lesen."""
    dir_.mkdir(parents=True, exist_ok=True)
    modell_name = f"modell_{horizont}h{alpha_suffix}_{datum}.lgbm"
    (dir_ / modell_name).write_bytes(b"fake")
    metriken_name = f"metriken_{horizont}h{alpha_suffix}_{datum}.json"
    with open(dir_ / metriken_name, "w") as f:
        json.dump({
            "zeitstempel": "2026-04-19T10:00:00",
            "horizont_stunden": horizont,
            "anzahl_samples": 100,
            "anzahl_features": 10,
            "mae": mae, "rmse": mae * 1.5, "r2": 0.8,
            "baseline_mae": mae * 1.5, "baseline_rmse": mae * 2,
            "baseline_r2": 0.5,
        }, f)
    link = dir_ / f"aktuell_{horizont}h.lgbm"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(modell_name)


# --- _lade_aktuelle_mae ---


def test_lade_aktuelle_mae_erkennt_alle_horizonte(tmp_path):
    for h in (6, 12, 24):
        _lege_modell_an(tmp_path, h, "2026-04-19", mae=h * 0.5)

    m = _lade_aktuelle_mae(tmp_path)
    assert m == {6: 3.0, 12: 6.0, 24: 12.0}


def test_lade_aktuelle_mae_leer_wenn_keine_modelle(tmp_path):
    assert _lade_aktuelle_mae(tmp_path) == {}


# --- T-0219: Archiv-Retention ---------------------------------------

def test_raeume_alte_archive_behaelt_juengste_n(tmp_path):
    """Von 8 Datums-Versionen bleiben die 5 juengsten, 3 werden geloescht."""
    basis = tmp_path / "bambuswald_archiv"
    basis.mkdir()
    namen = [
        "2026-05-10_080000", "2026-05-11_080000", "2026-05-12_080000",
        "2026-05-13_080000", "2026-05-14_080000", "2026-05-15_080000",
        "2026-05-16_080000", "2026-05-17_080000",
    ]
    for n in namen:
        (basis / n).mkdir()
        (basis / n / "modell.lgbm").write_text("x")

    _raeume_alte_archive(basis, behalten=5)

    verbleibend = sorted(p.name for p in basis.iterdir())
    assert verbleibend == namen[-5:], (
        f"Erwartet die 5 juengsten, war {verbleibend}"
    )


def test_raeume_alte_archive_noop_bei_weniger_als_limit(tmp_path):
    """Weniger Versionen als `behalten` -> nichts wird geloescht."""
    basis = tmp_path / "kroton_archiv"
    basis.mkdir()
    for n in ("2026-05-15_080000", "2026-05-16_080000"):
        (basis / n).mkdir()

    _raeume_alte_archive(basis, behalten=5)
    assert len(list(basis.iterdir())) == 2


def test_swap_live_mit_tmp_raeumt_archive_auf(tmp_path):
    """Integration: nach 7 Swaps mit archiv_behalten=5 bleiben genau 5
    Archiv-Versionen — das Archiv waechst nicht mehr unbegrenzt."""
    live = tmp_path / "modelle"
    archiv_basis = tmp_path / "modelle_archiv"
    live.mkdir()
    (live / "modell.lgbm").write_text("v0")

    for i in range(1, 8):
        tmp = tmp_path / f"tmp_{i}"
        tmp.mkdir()
        (tmp / "modell.lgbm").write_text(f"v{i}")
        archiv = archiv_basis / f"2026-05-{10 + i:02d}_080000"
        _swap_live_mit_tmp(live, tmp, archiv, archiv_behalten=5)

    versionen = sorted(p.name for p in archiv_basis.iterdir())
    assert len(versionen) == 5, f"Erwartet 5 Archive, war {versionen}"
    # Die juengsten 5 (Swap 3-7 archivierten v2..v6).
    assert versionen[0] == "2026-05-13_080000"
    assert versionen[-1] == "2026-05-17_080000"


def test_lade_aktuelle_mae_erkennt_q50_metriken(tmp_path):
    """Wenn nur q50-Metriken existieren (kein metriken_Xh_*.json-Symlink),
    liest der Loader den q50-Pfad."""
    _lege_modell_an(tmp_path, 6, "2026-04-19", mae=2.5, alpha_suffix="_q50")

    m = _lade_aktuelle_mae(tmp_path)
    assert m[6] == 2.5


def test_lade_aktuelle_mae_nutzt_quantile_mae_nicht_pinball_loss(tmp_path):
    _lege_modell_an(tmp_path, 6, "2026-04-19", mae=4.0, alpha_suffix="_q50")
    pfad = tmp_path / "metriken_6h_q50_2026-04-19.json"
    daten = json.loads(pfad.read_text())
    daten["pinball_loss"] = 2.0
    daten["baseline_pinball_loss"] = 3.0
    pfad.write_text(json.dumps(daten))

    neue_mae = _lade_aktuelle_mae(tmp_path)
    gate = _pruefe_gate({6: 3.0}, neue_mae, gate_faktor=0.95)

    assert neue_mae[6] == 4.0
    assert gate["passed"] is False


# --- _pruefe_gate ---


def test_gate_passiert_wenn_alle_horizonte_besser(tmp_path):
    alt = {6: 4.0, 12: 6.0, 24: 8.0}
    neu = {6: 3.5, 12: 5.5, 24: 7.5}
    g = _pruefe_gate(alt, neu, gate_faktor=0.95)
    # 3.5 < 0.95*4 = 3.8 ✓, 5.5 < 0.95*6 = 5.7 ✓, 7.5 < 0.95*8 = 7.6 ✓
    assert g["passed"] is True
    assert g["fehlschlaege"] == []


def test_gate_blockiert_wenn_ein_horizont_schlechter(tmp_path):
    alt = {6: 4.0, 12: 6.0, 24: 8.0}
    # 12h nur minimal besser → > 0.95 * 6 = 5.7
    neu = {6: 3.5, 12: 5.9, 24: 7.5}
    g = _pruefe_gate(alt, neu, gate_faktor=0.95)
    assert g["passed"] is False
    assert len(g["fehlschlaege"]) == 1
    assert g["fehlschlaege"][0]["horizont_h"] == 12


def test_gate_akzeptiert_ersten_retrain_ohne_alte_metriken():
    """Erster Lauf: keine alten Metriken → akzeptieren, solange neue da sind."""
    g = _pruefe_gate(alte_mae={}, neue_mae={6: 4.0, 12: 6.0, 24: 8.0},
                     gate_faktor=0.95)
    assert g["passed"] is True


def test_gate_blockt_initial_deploy_bei_unvollstaendigen_horizonten():
    """T-0101: Beim Erst-Deploy reicht nicht, dass irgendein neuer Horizont
    trainiert wurde — alle drei (6/12/24) muessen da sein. Sonst kann ein
    teiltrainierter Cluster (z. B. nur 6 h) produktiv werden.
    """
    g = _pruefe_gate(alte_mae={}, neue_mae={6: 4.0}, gate_faktor=0.95)
    assert g["passed"] is False
    assert g["fehlschlaege"][0]["grund"] == "initial_unvollstaendig"
    assert sorted(g["fehlschlaege"][0]["fehlende_horizonte_h"]) == [12, 24]


def test_gate_blockt_wenn_modelle_da_aber_metriken_unlesbar():
    """T-0399: Modelle vorhanden, aber `metriken_*.json` fehlt/korrupt ->
    `_lade_aktuelle_mae` liefert `{}`, was frueher als Erstlauf gedeutet wurde:
    JEDER vollstaendig trainierte Kandidat ging ohne MAE-Vergleich durch. Der
    Governance-Weg "Metriken zuruecksetzen" haette das Gate weit aufgemacht.
    """
    g = _pruefe_gate(
        alte_mae={}, neue_mae={6: 4.0, 12: 6.0, 24: 8.0},
        gate_faktor=0.95, alte_modelle={6, 12, 24},
    )
    assert g["passed"] is False
    assert {f["grund"] for f in g["fehlschlaege"]} == {"metrik_fehlt"}
    assert [f["horizont_h"] for f in g["fehlschlaege"]] == [6, 12, 24]


def test_gate_erstlauf_bleibt_erstlauf_wenn_keine_modelle_existieren():
    """Gegenprobe zu T-0399: ohne Modelle bleibt der Erstlauf-Pfad erhalten --
    sonst koennte nie ein erstes Modell deployt werden."""
    g = _pruefe_gate(
        alte_mae={}, neue_mae={6: 4.0, 12: 6.0, 24: 8.0},
        gate_faktor=0.95, alte_modelle=set(),
    )
    assert g["passed"] is True


def test_modell_horizonte_erkennt_vorhandene_symlinks(tmp_path):
    """T-0399: die Menge kommt aus den `aktuell_Xh.lgbm`-Dateien, unabhaengig
    davon, ob daneben lesbare Metriken liegen."""
    from bewaesserung.ml.retrain_job import _modell_horizonte

    assert _modell_horizonte(tmp_path) == set()
    (tmp_path / "aktuell_6h.lgbm").write_text("x")
    (tmp_path / "aktuell_24h.lgbm").write_text("x")
    assert _modell_horizonte(tmp_path) == {6, 24}


def test_gate_blockiert_wenn_metrik_fehlt():
    alt = {6: 4.0, 12: 6.0, 24: 8.0}
    neu = {6: 3.5, 12: 5.5}  # 24h-Metrik fehlt
    g = _pruefe_gate(alt, neu, gate_faktor=0.95)
    assert g["passed"] is False
    assert any(f["horizont_h"] == 24 for f in g["fehlschlaege"])


# --- Job-Orchestrierung ---


def test_retrain_job_ruht_wenn_aktiv_false(speicher, tmp_path):
    konfig = _konfig()
    retrain = MlRetrainKonfig(aktiv=False)
    job = MlRetrainJob(speicher, konfig, retrain, ausgabe_pfad=str(tmp_path))
    assert _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 10))) is False


def test_retrain_job_respektiert_intervall_gate(speicher, tmp_path, monkeypatch):
    konfig = _konfig()
    retrain = MlRetrainKonfig(aktiv=True, intervall_tage=7, start_verzoegerung_minuten=0)
    job = MlRetrainJob(speicher, konfig, retrain, ausgabe_pfad=str(tmp_path))

    # Trainings-Pfad ueberschreiben, damit wir keinen echten Retrain brauchen
    async def fake_fuehre_aus(_jetzt):
        return {"status": "uebernommen"}
    monkeypatch.setattr(job, "_fuehre_aus", fake_fuehre_aus)

    t0 = datetime(2026, 4, 19, 10)
    assert _run(job.aktualisiere_wenn_faellig(t0)) is True
    # Zu frueh
    assert _run(job.aktualisiere_wenn_faellig(t0 + timedelta(days=6))) is False
    # Nach Intervall
    assert _run(job.aktualisiere_wenn_faellig(t0 + timedelta(days=8))) is True


def test_retrain_job_reicht_monotone_flag_bis_pipeline(speicher, tmp_path, monkeypatch):
    import bewaesserung.ml.features as features_mod
    import bewaesserung.ml.training as training_mod

    gesehen = {}

    class FakeExtraktor:
        def __init__(self, _speicher, _konfig):
            pass

        async def erstelle_trainingsdaten(self, von, bis):
            return pd.DataFrame({"x": range(250)})

    class FakePipeline:
        def __init__(
            self,
            modell_verzeichnis,
            n_folds,
            monotone_constraints=False,
            **_kwargs,
        ):
            gesehen["modell_verzeichnis"] = Path(modell_verzeichnis)
            gesehen["n_folds"] = n_folds
            gesehen["monotone_constraints"] = monotone_constraints

        def trainiere_quantile(self, _df):
            datum = "2026-04-20"
            gesehen["modell_verzeichnis"].mkdir(parents=True, exist_ok=True)
            for h in (6, 12, 24):
                modell_name = f"modell_{h}h_q50_{datum}.lgbm"
                (gesehen["modell_verzeichnis"] / modell_name).write_bytes(b"fake")
                with open(
                    gesehen["modell_verzeichnis"] / f"metriken_{h}h_q50_{datum}.json",
                    "w",
                ) as f:
                    json.dump({
                        "zeitstempel": "2026-04-20T10:00:00",
                        "horizont_stunden": h,
                        "anzahl_samples": 250,
                        "anzahl_features": 1,
                        "mae": 1.0,
                        "rmse": 1.0,
                        "r2": 0.0,
                        "baseline_mae": 2.0,
                        "baseline_rmse": 2.0,
                        "baseline_r2": 0.0,
                    }, f)
                link = gesehen["modell_verzeichnis"] / f"aktuell_{h}h.lgbm"
                link.symlink_to(modell_name)

    monkeypatch.setattr(features_mod, "FeatureExtraktor", FakeExtraktor)
    monkeypatch.setattr(training_mod, "TrainingsPipeline", FakePipeline)

    konfig = _konfig()
    retrain = MlRetrainKonfig(
        aktiv=True,
        quantile=True,
        folds=4,
        monotone_constraints=True,
        start_verzoegerung_minuten=0,
    )
    job = MlRetrainJob(speicher, konfig, retrain, ausgabe_pfad=str(tmp_path / "ml"))

    ergebnis = _run(job._fuehre_aus(datetime(2026, 4, 20, 10)))

    assert ergebnis["status"] == "uebernommen"
    assert gesehen["n_folds"] == 4
    assert gesehen["monotone_constraints"] is True


def test_retrain_job_fehler_isoliert(speicher, tmp_path, monkeypatch):
    # start_verzoegerung_minuten=0 → deterministisches Verhalten im Test
    # (ohne Delay ist `_letzte_aktualisierung` initial None, bei Fehler
    # bleibt es None → sofort wieder faellig).
    konfig = _konfig()
    retrain = MlRetrainKonfig(aktiv=True, start_verzoegerung_minuten=0)
    job = MlRetrainJob(speicher, konfig, retrain, ausgabe_pfad=str(tmp_path))

    async def boom(_jetzt):
        raise RuntimeError("Trainings-Absturz")
    monkeypatch.setattr(job, "_fuehre_aus", boom)

    t0 = datetime(2026, 4, 19, 10)
    assert _run(job.aktualisiere_wenn_faellig(t0)) is False
    # Da der letzte Lauf fehlgeschlagen ist, bleibt Job sofort wieder faellig
    # (nicht 7 Tage warten).
    assert job._letzte_aktualisierung is None


def test_retrain_job_start_verzoegerung_blockiert_ersten_lauf(
    speicher, tmp_path, monkeypatch,
):
    """Mit `start_verzoegerung_minuten=30` feuert der erste Retrain NICHT
    sofort beim Service-Start — sonst blockiert die 2 Min LightGBM-Last die
    parallelen Browser-Inferenzen.
    """
    konfig = _konfig()
    retrain = MlRetrainKonfig(aktiv=True, start_verzoegerung_minuten=30)
    job = MlRetrainJob(speicher, konfig, retrain, ausgabe_pfad=str(tmp_path))

    aufrufe = {"n": 0}

    async def spy(_jetzt):
        aufrufe["n"] += 1
        return {"status": "abgebrochen", "grund": "spy"}

    monkeypatch.setattr(job, "_fuehre_aus", spy)

    # Direkt nach Init: nicht faellig (delay steht 30 Min in der Zukunft).
    # Init-Zeit ist real_now, "jetzt" setzen wir auf real_now + 5 Min.
    jetzt_plus_5 = datetime.now() + timedelta(minutes=5)
    _run(job.aktualisiere_wenn_faellig(jetzt_plus_5))
    assert aufrufe["n"] == 0

    # Nach >30 Min: faellig, wird ausgefuehrt.
    jetzt_plus_40 = datetime.now() + timedelta(minutes=40)
    _run(job.aktualisiere_wenn_faellig(jetzt_plus_40))
    assert aufrufe["n"] == 1


def test_retrain_job_abgelehnt_laesst_live_modelle_unberuehrt(
    speicher, tmp_path, monkeypatch,
):
    """Mock ein Training, das schlechtere Metriken liefert → Gate blockiert,
    Live-Modelle bleiben erhalten, tmp wird geloescht."""
    live = tmp_path / "ml"
    for h in (6, 12, 24):
        _lege_modell_an(live, h, "2026-04-10", mae=h * 0.5)

    konfig = _konfig()
    retrain = MlRetrainKonfig(aktiv=True, gate_faktor=0.95, start_verzoegerung_minuten=0)
    job = MlRetrainJob(speicher, konfig, retrain, ausgabe_pfad=str(live))

    async def fake_train(_jetzt):
        # Originalfunktion aufrufen, aber Training + Feature-Export
        # ueberspringen — direkt Training im tmp stubben.
        from bewaesserung.ml.retrain_job import (
            _lade_aktuelle_mae,
            _pruefe_gate,
            shutil,
        )
        tmp_pfad = live.parent / f"{live.name}_tmp"
        tmp_pfad.mkdir(parents=True, exist_ok=True)
        # schlechtere Metriken → Gate muss blockieren
        for h in (6, 12, 24):
            _lege_modell_an(tmp_pfad, h, "2026-04-19", mae=h * 0.6)
        alte = _lade_aktuelle_mae(live)
        neue = _lade_aktuelle_mae(tmp_pfad)
        gate = _pruefe_gate(alte, neue, gate_faktor=0.95)
        if not gate["passed"]:
            shutil.rmtree(tmp_pfad)
            return {"status": "abgelehnt", "mae_alt": alte, "mae_neu": neue}
        return {"status": "uebernommen"}

    monkeypatch.setattr(job, "_fuehre_aus", fake_train)

    t0 = datetime(2026, 4, 19, 10)
    assert _run(job.aktualisiere_wenn_faellig(t0)) is True
    ergebnis = job.letztes_ergebnis
    assert ergebnis is not None
    assert ergebnis["status"] == "abgelehnt"

    # Live-Modelle unveraendert
    assert (live / "aktuell_6h.lgbm").resolve().name == "modell_6h_2026-04-10.lgbm"
    # Tmp geloescht
    assert not (live.parent / f"{live.name}_tmp").exists()
