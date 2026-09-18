"""T-0605 (Gesamtreview ML-2): Retrain-Gate vergleicht Amtsinhaber und
Kandidat auf denselben CV-Testzeilen statt gegen eine eingefrorene MAE.

Alles synthetisch: echte LightGBM-Trainings auf erfundenen Daten in
tmp_path, Attrappen-Extraktor, keine DB-Inhalte, kein Live-Modell.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

lgb = pytest.importorskip("lightgbm")
np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")

from bewaesserung.ml.gate_vergleich import vergleiche_auf_gleichen_testzeilen
from bewaesserung.ml.modelle_ml import FEATURE_SCHEMA_VERSION
from bewaesserung.ml.retrain_job import MlRetrainJob
from bewaesserung.ml.training import NICHT_FEATURES, TrainingsErgebnis
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

HORIZONTE = (6, 12, 24)
START = datetime(2026, 8, 1, 0, 0)
AMTSINHABER_DATUM = "2026-07-29"


def _daten(n: int = 700) -> pd.DataFrame:
    """Lernbares Signal `x1`: Ziel = aktuell + x1 * h/6 (+ kleines Rauschen)."""
    rng = np.random.default_rng(605)
    zeilen = []
    for i in range(n):
        aktuell = 40.0 + 10.0 * np.sin(i / 20.0)
        x1 = 3.0 * np.sin(i / 7.0)
        zeile = {
            "zone_id": "hecke",
            "zeitstempel": (START + timedelta(hours=i)).isoformat(),
            "boden_feuchte_aktuell": aktuell,
            "feuchte_trend_6h": 0.0,
            "x1": x1,
        }
        for h in HORIZONTE:
            ziel = aktuell + x1 * h / 6.0 + float(rng.normal(0, 0.1))
            zeile[f"ziel_feuchte_{h}h"] = ziel
            zeile[f"ziel_delta_{h}h"] = ziel - aktuell
        zeilen.append(zeile)
    return pd.DataFrame(zeilen)


def _feature_spalten(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in NICHT_FEATURES]


def _lege_amtsinhaber_an(
    cluster_dir: Path, df: pd.DataFrame, *, gut: bool, mae_eingefroren: float,
    trainiert_am: str = f"{AMTSINHABER_DATUM}T03:00:00",
    schema_version: int = FEATURE_SCHEMA_VERSION,
    extra_spalte: str | None = None,
) -> None:
    """Live-Modell im Layout des Quantile-Deploys (q50 + Punkt-Symlink).

    gut=False: lernt Delta 0 (Persistenz), liegt also um |x1*h/6| daneben.
    gut=True: auf ALLEN Zeilen mit dem echten Delta trainiert, also auf den
    Testzeilen fast fehlerfrei. Die eingefrorene MAE ist bewusst gelogen,
    damit das alte Gate das Gegenteil des fairen Urteils faellt.
    """
    cluster_dir.mkdir(parents=True, exist_ok=True)
    if extra_spalte:
        df = df.assign(**{extra_spalte: 0.0})
    spalten = _feature_spalten(df)
    for h in HORIZONTE:
        label = df[f"ziel_delta_{h}h"].to_numpy() if gut else np.zeros(len(df))
        booster = lgb.train(
            {"objective": "regression", "verbose": -1, "min_data_in_leaf": 5},
            lgb.Dataset(df[spalten], label=label),
            num_boost_round=300 if gut else 5,
        )
        name = f"modell_{h}h_q50_{AMTSINHABER_DATUM}.lgbm"
        booster.save_model(str(cluster_dir / name))
        with open(cluster_dir / f"metriken_{h}h_q50_{AMTSINHABER_DATUM}.json", "w") as f:
            json.dump({
                "zeitstempel": trainiert_am,
                "horizont_stunden": h, "anzahl_samples": len(df),
                "anzahl_features": len(spalten),
                "mae": mae_eingefroren, "rmse": 1.0, "r2": 0.0,
                "baseline_mae": 9.0, "baseline_rmse": 9.0, "baseline_r2": 0.0,
                "delta_ziel": True,
                "feature_schema_version": schema_version,
            }, f)
        (cluster_dir / f"aktuell_{h}h.lgbm").symlink_to(name)


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t"),
        zonen=[ZonenKonfig(zone_id="hecke", name="H", ventil_kanal=1)],
        wetter=WetterKonfig(standorte=[WetterStandortKonfig(
            id="o", breite=52.52, laenge=13.405,
        )]),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[StandortKonfig(
            standort_id="garten", name="G", wetter_standort="o", zonen=["hecke"],
        )],
    )


class _Speicher:
    """Attrappe: nur die zwei Lesezugriffe, die der Retrain vor dem Gate macht."""

    async def hole_wartungs_fenster(self, nur_offen=False):
        return []

    async def hole_entscheidungsgebundene_feuchte_versionen(self):
        return {}


def _retrain(
    tmp_path: Path, df: pd.DataFrame, monkeypatch, strategie: str = "pro_zone",
    **retrain_kw,
) -> dict:
    import bewaesserung.ml.features as features_mod

    class FakeExtraktor:
        def __init__(self, _speicher, _konfig):
            pass

        def setze_wartungs_fenster(self, _fenster):
            pass

        async def erstelle_trainingsdaten(self, von, bis):
            return df

    monkeypatch.setattr(features_mod, "FeatureExtraktor", FakeExtraktor)
    retrain = MlRetrainKonfig(
        aktiv=True, quantile=True, cluster_strategie=strategie,
        mindest_zeilen_pro_cluster=100, start_verzoegerung_minuten=0,
        gate_faktor_pro_cluster={"hecke": 0.99}, gate_faktor=0.99,
        gate_vergleich_mindest_zeilen=50,
        **retrain_kw,
    )
    job = MlRetrainJob(
        _Speicher(), _konfig(), retrain, ausgabe_pfad=str(tmp_path / "ml"),
    )
    ergebnis = asyncio.run(job._fuehre_aus(datetime(2026, 9, 10, 12)))
    return ergebnis["cluster"]["hecke"] if strategie == "pro_zone" else ergebnis


def test_t0605_besserer_kandidat_scheitert_nicht_an_eingefrorener_baseline(
    tmp_path, monkeypatch,
):
    """Akzeptanzkriterium 3a. Der Amtsinhaber ist auf den Testzeilen klar
    schlechter, seine gespeicherte Trainings-MAE (0,05) ist aber kaum zu
    unterbieten. Das alte Gate lehnt ab, das faire uebernimmt."""
    df = _daten()
    _lege_amtsinhaber_an(
        tmp_path / "ml" / "feuchte" / "hecke", df,
        gut=False, mae_eingefroren=0.05,
    )

    r = _retrain(tmp_path, df, monkeypatch)

    assert r["status"] == "uebernommen", r
    assert r["gate_modus"] == "gleiches_fenster"
    for h in HORIZONTE:
        assert r["mae_neu"][h] < 0.99 * r["mae_alt"][h]
        assert r["mae_alt_eingefroren"][h] == 0.05


def test_t0605_schalter_aus_faellt_auf_eingefrorenes_gate_zurueck(
    tmp_path, monkeypatch,
):
    """Ruecknahme-Schalter: gleiche Lage, gate_gleiches_fenster=False ->
    das alte Urteil (abgelehnt gegen 0,05)."""
    df = _daten()
    _lege_amtsinhaber_an(
        tmp_path / "ml" / "feuchte" / "hecke", df,
        gut=False, mae_eingefroren=0.05,
    )

    r = _retrain(tmp_path, df, monkeypatch, gate_gleiches_fenster=False)

    assert r["status"] == "abgelehnt", r
    assert r["gate_modus"] == "eingefroren"
    assert r["gate_grund"] == "abgeschaltet"


def test_t0605_faires_gate_lehnt_schlechteren_kandidaten_weiter_ab(
    tmp_path, monkeypatch,
):
    """Gegenrichtung: der Amtsinhaber ist auf den Testzeilen besser (Delta-
    Modell, Ausgabe + aktuell). Die eingefrorene MAE 99 wuerde jeden
    Kandidaten durchlassen; das faire Gate darf das nicht."""
    df = _daten()
    _lege_amtsinhaber_an(
        tmp_path / "ml" / "feuchte" / "hecke", df,
        gut=True, mae_eingefroren=99.0,
    )

    r = _retrain(tmp_path, df, monkeypatch)

    assert r["status"] == "abgelehnt", r
    assert r["gate_modus"] == "gleiches_fenster"


# --- T-0610 ML-7: Amtsinhaber auf aelterem Feature-Schema ------------------
# Entscheid Andre 14.10.: kein blinder Austausch. Der faire Vergleich auf den
# NEU gebauten Testzeilen entscheidet; ist er unmoeglich, bleibt der Alte.
# Jeder Fall laeuft im Pro-Cluster- und im globalen Pfad.

ALT = FEATURE_SCHEMA_VERSION - 1


def _amtsinhaber_dir(tmp_path: Path, strategie: str) -> Path:
    basis = tmp_path / "ml"
    return basis / "feuchte" / "hecke" if strategie == "pro_zone" else basis


@pytest.mark.parametrize("strategie", ["pro_zone", "global"])
def test_t0610_ml7_alter_amtsinhaber_besserer_kandidat_ersetzt(
    tmp_path, monkeypatch, strategie,
):
    """Amtsinhaber (altes Schema) liegt auf den Testzeilen daneben, seine
    eingefrorene MAE 0,01 ist aber nicht zu unterbieten, und der Schalter
    fuer den fairen Vergleich ist AUS: nur der fuer alte Schemata erzwungene
    faire Vergleich laesst den Kandidaten durch. Das neue Modell traegt die
    aktuelle Schema-Version, damit der naechste Lauf normal vergleicht."""
    df = _daten()
    d = _amtsinhaber_dir(tmp_path, strategie)
    _lege_amtsinhaber_an(
        d, df, gut=False, mae_eingefroren=0.01, schema_version=ALT,
    )

    r = _retrain(tmp_path, df, monkeypatch, strategie, gate_gleiches_fenster=False)

    assert r["status"] == "uebernommen", r
    assert r["gate_modus"] == "gleiches_fenster"
    assert r["schema_alt"] == ALT
    for h in HORIZONTE:
        assert r["mae_neu"][h] < 0.99 * r["mae_alt"][h]
        ziel = (d / f"aktuell_{h}h.lgbm").resolve()
        metrik = ziel.parent / ziel.name.replace("modell_", "metriken_").replace(
            ".lgbm", ".json",
        )
        with open(metrik) as f:
            assert json.load(f)["feature_schema_version"] == FEATURE_SCHEMA_VERSION


@pytest.mark.parametrize("strategie", ["pro_zone", "global"])
def test_t0610_ml7_alter_amtsinhaber_schlechterer_kandidat_bleibt_draussen(
    tmp_path, monkeypatch, strategie,
):
    """Amtsinhaber (altes Schema) ist auf den neuen Testzeilen besser. Die
    eingefrorene MAE 99 wuerde jeden Kandidaten durchlassen, ein blinder
    Austausch ebenso; der faire Vergleich lehnt ab, der Alte bleibt."""
    df = _daten()
    d = _amtsinhaber_dir(tmp_path, strategie)
    _lege_amtsinhaber_an(
        d, df, gut=True, mae_eingefroren=99.0, schema_version=ALT,
    )

    r = _retrain(tmp_path, df, monkeypatch, strategie)

    assert r["status"] == "abgelehnt", r
    assert r["gate_modus"] == "gleiches_fenster"
    assert r["schema_alt"] == ALT
    assert (d / "aktuell_6h.lgbm").resolve().name.endswith(f"{AMTSINHABER_DATUM}.lgbm")


@pytest.mark.parametrize("strategie", ["pro_zone", "global"])
@pytest.mark.parametrize(
    "fall", ["spalte_fehlt", "zu_junger_amtsinhaber", "modell_unlesbar"],
)
def test_t0610_ml7_alter_amtsinhaber_vergleich_unmoeglich_bleibt(
    tmp_path, monkeypatch, strategie, fall,
):
    """Faire Bewertung technisch unmoeglich: dem neuen Feature-Bau fehlt eine
    Spalte des Amtsinhabers (wuerde live still als NaN gefuellt), es gibt
    nach seinem Training zu wenige Testzeilen, oder sein Modell laesst sich
    nicht laden (Fehlerpfad). Die eingefrorene MAE 99 liesse
    den Kandidaten durch; bei altem Schema zaehlt sie nicht -> kein Ersatz."""
    df = _daten()
    d = _amtsinhaber_dir(tmp_path, strategie)
    kw = (
        {"extra_spalte": "x_alt"} if fall == "spalte_fehlt"
        else {"trainiert_am": (START + timedelta(days=60)).isoformat()}
    )
    if fall == "modell_unlesbar":
        kw = {}
    _lege_amtsinhaber_an(
        d, df, gut=False, mae_eingefroren=99.0, schema_version=ALT, **kw,
    )
    if fall == "modell_unlesbar":
        (d / "aktuell_6h.lgbm").resolve().write_text("kein lightgbm")

    r = _retrain(tmp_path, df, monkeypatch, strategie)

    assert r["status"] == "abgelehnt", r
    assert r["gate_modus"] == "schema_veraltet"
    assert r["fehlschlaege"] == [{"grund": "schema_vergleich_unmoeglich"}]
    assert r["gate_grund"].startswith({
        "spalte_fehlt": "spalten_fehlen",
        "zu_junger_amtsinhaber": "zu_wenig_zeilen_nach_amtsinhaber_training",
        "modell_unlesbar": "fehler:",
    }[fall])
    assert (d / "aktuell_6h.lgbm").resolve().name.endswith(f"{AMTSINHABER_DATUM}.lgbm")


def _kandidat_mit_cv_test(df: pd.DataFrame) -> dict[int, TrainingsErgebnis]:
    """Kandidat-Attrappe: CV-Testzeilen = alle Zeilen, Vorhersage = Ziel."""
    out = {}
    for h in HORIZONTE:
        cv = df.copy()
        cv["cv_fold"] = 0
        cv["cv_ziel_abs"] = cv[f"ziel_feuchte_{h}h"]
        cv["cv_vorhersage_abs"] = cv[f"ziel_feuchte_{h}h"]
        out[h] = TrainingsErgebnis(modell=None, metriken=None, feature_cols=[], cv_test=cv)
    return out


def test_t0605_amtsinhaber_wird_nur_nach_seinem_training_plus_gap_gewertet(tmp_path):
    """Walk-Forward-Gap: Zeilen vor `trainiert_am + gap` hat der Amtsinhaber
    womoeglich im Training gesehen; sie zaehlen nicht."""
    df = _daten(n=200)  # 01.08. 00:00 bis 09.08. 07:00, stuendlich
    trainiert = START + timedelta(hours=100)
    _lege_amtsinhaber_an(
        tmp_path / "hecke", df, gut=False, mae_eingefroren=1.0,
        trainiert_am=trainiert.isoformat(),
    )

    v = vergleiche_auf_gleichen_testzeilen(
        tmp_path / "hecke", _kandidat_mit_cv_test(df),
        gap_stunden=24, mindest_zeilen=10,
    )

    assert v["moeglich"] is True, v
    # Zeilen ab Stunde 124 (100 + 24) bis 199: 76 Stueck.
    assert v["n"] == {6: 76, 12: 76, 24: 76}
    assert v["ab"][6] == (trainiert + timedelta(hours=24)).isoformat()


def test_t0605_zu_wenige_zeilen_nach_dem_amtsinhaber_kein_fairer_vergleich(tmp_path):
    """Junger Amtsinhaber: weniger als mindest_zeilen nach Training + Gap ->
    kein fairer Vergleich (Aufrufer nutzt das eingefrorene Gate)."""
    df = _daten(n=200)
    _lege_amtsinhaber_an(
        tmp_path / "hecke", df, gut=False, mae_eingefroren=1.0,
        trainiert_am=(START + timedelta(hours=100)).isoformat(),
    )

    v = vergleiche_auf_gleichen_testzeilen(
        tmp_path / "hecke", _kandidat_mit_cv_test(df),
        gap_stunden=24, mindest_zeilen=77,
    )

    assert v["moeglich"] is False
    assert v["grund"] == "zu_wenig_zeilen_nach_amtsinhaber_training"
    assert v["n"] == 76


# --- T-0619 (Andre 13.10.2026): Mindestzeilen 100 -> 50 --------------------


def _amtsinhaber_jung(tmp_path: Path, df: pd.DataFrame) -> Path:
    """Amtsinhaber trainiert bei Stunde 100: gewertet wird ab Stunde 124."""
    verzeichnis = tmp_path / "hecke"
    _lege_amtsinhaber_an(
        verzeichnis, df, gut=False, mae_eingefroren=1.0,
        trainiert_am=(START + timedelta(hours=100)).isoformat(),
    )
    return verzeichnis


@pytest.mark.parametrize(
    ("n_daten", "fair"),
    [(174, True), (173, False)],   # 50 bzw. 49 Zeilen ab Stunde 124
    ids=["50_zeilen_fair", "49_zeilen_eingefroren"],
)
def test_t0619_default_mindestzeilen_50(tmp_path, n_daten, fair):
    """Mit dem Modell-Default: 50 Zeilen je Horizont reichen fuer den fairen
    Vergleich, 49 fallen auf das eingefrorene Gate zurueck."""
    df = _daten(n=n_daten)
    v = vergleiche_auf_gleichen_testzeilen(
        _amtsinhaber_jung(tmp_path, df), _kandidat_mit_cv_test(df),
        gap_stunden=24,
        mindest_zeilen=MlRetrainKonfig().gate_vergleich_mindest_zeilen,
    )

    assert v["moeglich"] is fair, v
    if fair:
        assert v["n"] == {6: 50, 12: 50, 24: 50}
    else:
        assert v["grund"] == "zu_wenig_zeilen_nach_amtsinhaber_training"
        assert v["n"] == 49


def test_t0619_alle_horizonte_muessen_die_grenze_schaffen(tmp_path):
    """Die Regel bleibt: 6 h und 12 h haben 50 Zeilen, 24 h nur 49 -> kein
    fairer Vergleich (kein Teil-Gate aus den bestandenen Horizonten)."""
    df = _daten(n=174)
    kandidat = _kandidat_mit_cv_test(df)
    kandidat[24].cv_test = kandidat[24].cv_test.iloc[:-1]

    v = vergleiche_auf_gleichen_testzeilen(
        _amtsinhaber_jung(tmp_path, df), kandidat,
        gap_stunden=24,
        mindest_zeilen=MlRetrainKonfig().gate_vergleich_mindest_zeilen,
    )

    assert v["moeglich"] is False
    assert v["horizont_h"] == 24
    assert v["n"] == 49
