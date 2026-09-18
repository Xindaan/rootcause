"""T-0605: fairer Champion/Challenger-Vergleich fuer das Retrain-Gate.

Bis T-0605 verglich das Gate die CV-MAE des Kandidaten (neues Datenfenster)
mit der beim Training EINGEFROREN gespeicherten MAE des Amtsinhabers (altes,
oft leichteres Fenster, teils noch vor T-0564 mit Early Stopping auf dem
Testblock). Der Amtsinhaber wurde nie auf den aktuellen Daten bewertet;
kasten_4, zitrus_ii und hecke hingen dadurch monatelang fest
(Gesamtreview ML-2).

Jetzt: Der Amtsinhaber rechnet auf GENAU den CV-Testzeilen des Kandidaten,
mit derselben Feature-Aufbereitung wie die Live-Inferenz
(`bereite_features_fuer_modell`). Beide MAE stehen damit auf derselben
Zeilenmenge.

Walk-Forward-Gap: Der Amtsinhaber kann einen Teil dieser Zeilen im Training
gesehen haben (sein Fenster endet bei seinem Trainingszeitpunkt). Gewertet
werden deshalb nur Zeilen mit `zeitstempel >= trainiert_am + gap_stunden`.
Das ist dieselbe Regel, mit der die CV ihre Testbloecke vom Training trennt
(Gap >= groesster Label-Horizont). Ohne sie waere der Vergleich zu Gunsten
des Amtsinhabers verzerrt (In-Sample gegen Out-of-Sample).

Alles oder nichts: Fehlt fuer einen Horizont Modell, Metrik, Kandidaten-CV
oder reichen die Zeilen nicht, liefert `moeglich=False` mit Grund, und der
Aufrufer faellt auf das alte eingefrorene Gate zurueck (Status quo statt
eines gemischten Urteils).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

HORIZONTE = (6, 12, 24)


def punkt_ergebnisse(ergebnisse) -> dict[int, object]:
    """Punktmodell je Horizont aus dem Rueckgabewert der Pipeline.

    `trainiere()` liefert `{h: ergebnis}`, `trainiere_quantile()` liefert
    `{(h, alpha_int): ergebnis}`; das Punktmodell ist dort q50 (darauf zeigt
    auch der Live-Symlink `aktuell_{h}h.lgbm`).
    """
    out: dict[int, object] = {}
    if not isinstance(ergebnisse, dict):
        return out
    for schluessel, erg in ergebnisse.items():
        if isinstance(schluessel, tuple):
            if len(schluessel) == 2 and schluessel[1] == 50:
                out[int(schluessel[0])] = erg
        elif isinstance(schluessel, int):
            out[schluessel] = erg
    return out


def _lade_amtsinhaber_metadaten(
    verzeichnis: Path, horizont: int,
) -> tuple[Path, datetime, bool] | None:
    """Modellpfad, Trainingszeitpunkt und Delta-Flag des Live-Modells.

    Sucht die Metrik-Datei wie der Inferenz-Lader (`modell_` -> `metriken_`
    am aufgeloesten Symlink-Ziel) und wie `_lade_aktuelle_mae` (Datumssuffix,
    auch die q50-Variante).
    """
    symlink = verzeichnis / f"aktuell_{horizont}h.lgbm"
    if not symlink.exists():
        return None
    ziel = symlink.resolve()
    datum = ziel.stem.split("_")[-1]
    kandidaten = [
        verzeichnis / f"{ziel.stem.replace('modell_', 'metriken_')}.json",
        verzeichnis / f"metriken_{horizont}h_{datum}.json",
        verzeichnis / f"metriken_{horizont}h_q50_{datum}.json",
    ]
    for pfad in kandidaten:
        if not pfad.exists():
            continue
        try:
            with open(pfad) as f:
                daten = json.load(f)
            trainiert_am = datetime.fromisoformat(str(daten["zeitstempel"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        return ziel, trainiert_am, bool(daten.get("delta_ziel", False))
    return None


def vergleiche_auf_gleichen_testzeilen(
    amtsinhaber_verzeichnis: Path,
    kandidat: dict[int, object],
    gap_stunden: int,
    mindest_zeilen: int,
    spalten_pflicht: bool = False,
) -> dict:
    """Bewertet Amtsinhaber und Kandidat auf denselben CV-Testzeilen.

    `spalten_pflicht` (T-0610 ML-7, Amtsinhaber auf aelterem Feature-Schema):
    fehlt eine Feature-Spalte des Amtsinhabers in den Testzeilen, ist der
    Vergleich nicht moeglich (`grund` "spalten_fehlen"), statt die Spalte wie
    in der Live-Aufbereitung still als NaN zu fuellen.

    Rueckgabe:
      `moeglich` True: `mae_alt`/`mae_neu` je Horizont auf identischen
      Zeilen, `n` (Zeilen) und `ab` (erste gewertete Zeit) je Horizont.
      `moeglich` False: `grund` (+ `horizont_h`), der Aufrufer nutzt das
      eingefrorene Gate.
    """
    import lightgbm as lgb
    import numpy as np
    import pandas as pd

    from bewaesserung.ml.vorhersage import bereite_features_fuer_modell

    mae_alt: dict[int, float] = {}
    mae_neu: dict[int, float] = {}
    anzahl: dict[int, int] = {}
    ab: dict[int, str] = {}

    def _nicht(grund: str, h: int | None = None) -> dict:
        out = {"moeglich": False, "grund": grund}
        if h is not None:
            out["horizont_h"] = h
        return out

    for h in HORIZONTE:
        erg = kandidat.get(h)
        cv_test = getattr(erg, "cv_test", None)
        if cv_test is None or len(cv_test) == 0:
            return _nicht("kandidat_ohne_cv_testzeilen", h)
        meta = _lade_amtsinhaber_metadaten(amtsinhaber_verzeichnis, h)
        if meta is None:
            return _nicht("amtsinhaber_modell_oder_metrik_fehlt", h)
        modell_pfad, trainiert_am, delta_ziel = meta

        zeit = pd.to_datetime(cv_test["zeitstempel"], format="ISO8601")
        grenze = trainiert_am + timedelta(hours=gap_stunden)
        if getattr(zeit.dt, "tz", None) is not None and grenze.tzinfo is None:
            # Projektkonvention: `trainiert_am` ist naive Lokalzeit
            # (datetime.now() beim Training).
            grenze = grenze.astimezone()
        maske = (zeit >= pd.Timestamp(grenze)).to_numpy()
        n = int(maske.sum())
        if n < mindest_zeilen:
            return {
                **_nicht("zu_wenig_zeilen_nach_amtsinhaber_training", h),
                "n": n, "mindest_zeilen": mindest_zeilen,
                "ab": grenze.isoformat(),
            }
        zeilen = cv_test.loc[maske]

        booster = lgb.Booster(model_file=str(modell_pfad))
        if spalten_pflicht:
            fehlend = [c for c in booster.feature_name() if c not in zeilen.columns]
            if fehlend:
                return {**_nicht("spalten_fehlen", h), "spalten": fehlend}
        X = bereite_features_fuer_modell(zeilen, booster.feature_name())
        vorhersage = np.asarray(booster.predict(X), dtype=float)
        if delta_ziel:
            vorhersage = vorhersage + pd.to_numeric(
                zeilen["boden_feuchte_aktuell"], errors="coerce",
            ).to_numpy(dtype=float)
        ziel = zeilen["cv_ziel_abs"].to_numpy(dtype=float)
        kand = zeilen["cv_vorhersage_abs"].to_numpy(dtype=float)
        gueltig = np.isfinite(vorhersage) & np.isfinite(ziel) & np.isfinite(kand)
        if int(gueltig.sum()) < mindest_zeilen:
            return {
                **_nicht("zu_wenig_gueltige_zeilen", h),
                "n": int(gueltig.sum()), "mindest_zeilen": mindest_zeilen,
            }
        mae_alt[h] = round(float(np.mean(np.abs(ziel[gueltig] - vorhersage[gueltig]))), 4)
        mae_neu[h] = round(float(np.mean(np.abs(ziel[gueltig] - kand[gueltig]))), 4)
        anzahl[h] = int(gueltig.sum())
        ab[h] = grenze.isoformat()

    return {
        "moeglich": True,
        "mae_alt": mae_alt,
        "mae_neu": mae_neu,
        "n": anzahl,
        "ab": ab,
    }


def vergleiche_inverse_auf_gleichen_testzeilen(
    alt_modell: Path | None,
    alt_metadata: Path | None,
    cv_test,
    gap_stunden: int,
    mindest_zeilen: int,
) -> dict:
    """T-0610 E3: dasselbe faire Gate fuer das Response-Inverse-Modell.

    Bis hier verglich `MlResponseRetrainJob` die CV-MAE des Kandidaten mit der
    beim Training eingefrorenen `inverse_mae_s` des Amtsinhabers (gleiche
    Klasse wie T-0605; alle sieben Response-Modelle standen am 14.10.2026
    noch auf dem Stand 05.08.). Jetzt rechnet der Amtsinhaber auf den
    CV-Testzeilen des Kandidaten, gewertet ab `trainiert_am + gap_stunden`.

    `alt_modell`/`alt_metadata` sind die Dateien, auf die die `aktuell_*`-
    Symlinks VOR dem Lauf zeigten (der Lauf setzt sie schon um). Das
    Inverse-Modell wurde aus einer numpy-Matrix trainiert, die Spaltenfolge
    steht deshalb nur in `feature_cols_inverse` der Metadaten.

    Rueckgabe wie `vergleiche_auf_gleichen_testzeilen`, aber skalar:
    `moeglich`, `mae_alt`, `mae_neu` (Sekunden), `n`, `ab` bzw. `grund`.
    """
    import lightgbm as lgb
    import numpy as np
    import pandas as pd

    def _nicht(grund: str, **extra) -> dict:
        return {"moeglich": False, "grund": grund, **extra}

    if cv_test is None or len(cv_test) == 0:
        return _nicht("kandidat_ohne_cv_testzeilen")
    if alt_modell is None or alt_metadata is None:
        return _nicht("amtsinhaber_modell_oder_metrik_fehlt")
    try:
        with open(alt_metadata) as f:
            meta = json.load(f)
        trainiert_am = datetime.fromisoformat(str(meta["trainiert_am"]))
        spalten = list(meta["feature_cols_inverse"])
    except (OSError, ValueError, KeyError, TypeError):
        return _nicht("amtsinhaber_metadaten_unlesbar")
    if not Path(alt_modell).exists():
        return _nicht("amtsinhaber_modell_oder_metrik_fehlt")
    fehlend = [c for c in spalten + ["dauer_s", "cv_vorhersage"]
               if c not in cv_test.columns]
    if not spalten or fehlend:
        return _nicht("spalten_fehlen", spalten=fehlend)

    zeit = pd.to_datetime(cv_test["zeitstempel"], format="ISO8601")
    grenze = trainiert_am + timedelta(hours=gap_stunden)
    if getattr(zeit.dt, "tz", None) is not None and grenze.tzinfo is None:
        grenze = grenze.astimezone()
    zeilen = cv_test.loc[(zeit >= pd.Timestamp(grenze)).to_numpy()]
    if len(zeilen) < mindest_zeilen:
        return _nicht(
            "zu_wenig_zeilen_nach_amtsinhaber_training",
            n=int(len(zeilen)), mindest_zeilen=mindest_zeilen,
            ab=grenze.isoformat(),
        )

    booster = lgb.Booster(model_file=str(alt_modell))
    X = zeilen[spalten].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    vorhersage = np.asarray(booster.predict(X), dtype=float)
    ziel = pd.to_numeric(zeilen["dauer_s"], errors="coerce").to_numpy(dtype=float)
    kand = pd.to_numeric(zeilen["cv_vorhersage"], errors="coerce").to_numpy(dtype=float)
    gueltig = np.isfinite(vorhersage) & np.isfinite(ziel) & np.isfinite(kand)
    n = int(gueltig.sum())
    if n < mindest_zeilen:
        return _nicht("zu_wenig_gueltige_zeilen", n=n, mindest_zeilen=mindest_zeilen)
    return {
        "moeglich": True,
        "mae_alt": round(float(np.mean(np.abs(ziel[gueltig] - vorhersage[gueltig]))), 1),
        "mae_neu": round(float(np.mean(np.abs(ziel[gueltig] - kand[gueltig]))), 1),
        "n": n,
        "ab": grenze.isoformat(),
    }
