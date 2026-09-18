"""CLI fuer ML-Feature-Extraktion und Evaluation.

Nutzung:
    python -m bewaesserung.ml.cli exportiere --von 2026-04-07 --bis 2026-05-07
    python -m bewaesserung.ml.cli baseline --von 2026-04-07 --bis 2026-05-07
    python -m bewaesserung.ml.cli info
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    import pandas as pd  # noqa: F401 -- bewusster ML-Dependency-Guard (pd selbst ungenutzt)
except ImportError:
    print("ML-Abhaengigkeiten fehlen. Installiere mit: pip install -e '.[ml]'")
    sys.exit(1)


def _lade_konfig():
    """Laedt die Konfiguration."""
    # Projekt-Root: 4 Ebenen hoch von ml/cli.py
    from bewaesserung.konfig import lade_konfig
    return lade_konfig()


async def _erstelle_speicher(konfig, modus: str = "ro"):
    """Erstellt und verbindet den Speicher.

    T-0162: CLI nutzt **read-only** als Default, damit Manual-Retrain
    neben einem laufenden Backend keine Schreib-Locks produziert (lange
    60-Tage-Trainings-Lesungen vs. Backend-Schreibtask = `database is
    locked`). Schema-Setup + `_migriere()` werden bei `modus="ro"`
    uebersprungen. Backwards-kompatibel: `modus="rw"` macht das Alte
    Verhalten.
    """
    from bewaesserung.speicher import Speicher
    speicher = Speicher(konfig.speicher.db_pfad)
    await speicher.verbinden(modus=modus)
    return speicher


async def _mit_wartungs_fenster(extraktor, speicher) -> None:
    """T-0393: dynamische Wartungs-Fenster aus der DB in den Feature-Filter
    einspielen -- wie `retrain_job` es tut (T-0228 Stufe 2c).

    Vorher lud NUR der Auto-Retrain die DB-Fenster; manuelle CLI-Trainings
    (`trainiere`/`baseline`/`exportiere`) trainierten still auf Wartungs-
    kontaminierten Daten (Batterietausch, Einschlaemmen, Sensor-Umzug).
    Fehler bleiben nicht-fatal: lieber ohne DB-Fenster weiterrechnen (die
    YAML-`ml_ausschluss_fenster` greifen ohnehin) als den Lauf abbrechen.
    """
    import structlog
    try:
        wartung = await speicher.hole_wartungs_fenster(nur_offen=False)
        extraktor.setze_wartungs_fenster(wartung)
    except Exception:
        structlog.get_logger().exception("ml.cli.wartungs_fenster_load_fehler")


async def cmd_exportiere(args):
    """Exportiert Feature-DataFrame als CSV."""
    from bewaesserung.ml.features import FeatureExtraktor

    konfig = _lade_konfig()
    speicher = await _erstelle_speicher(konfig)

    try:
        extraktor = FeatureExtraktor(speicher, konfig)
        await _mit_wartungs_fenster(extraktor, speicher)
        df = await extraktor.erstelle_trainingsdaten(args.von, args.bis)

        if df.empty:
            print("Keine Daten im angegebenen Zeitraum.")
            return

        # Ausgabe
        ausgabe = Path(args.ausgabe)
        ausgabe.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(ausgabe, index=False)
        print(f"Exportiert: {len(df)} Zeilen, {len(df.columns)} Spalten → {ausgabe}")
        print(f"Zonen: {sorted(df['zone_id'].unique())}")
        print(f"Zeitraum: {df['zeitstempel'].min()} bis {df['zeitstempel'].max()}")

        # Zielvariablen-Statistik
        for h in [6, 12, 24]:
            col = f"ziel_feuchte_{h}h"
            if col in df.columns:
                n_valid = df[col].notna().sum()
                print(f"  ziel_{h}h: {n_valid}/{len(df)} gueltig "
                      f"({n_valid/len(df)*100:.0f}%)")
    finally:
        await speicher.schliessen()


async def cmd_baseline(args):
    """Evaluiert die regelbasierte Baseline."""
    from bewaesserung.ml.features import FeatureExtraktor
    from bewaesserung.ml.evaluation import evaluiere_baseline

    konfig = _lade_konfig()
    speicher = await _erstelle_speicher(konfig)

    try:
        extraktor = FeatureExtraktor(speicher, konfig)
        await _mit_wartungs_fenster(extraktor, speicher)
        df = await extraktor.erstelle_trainingsdaten(args.von, args.bis)

        if df.empty:
            print("Keine Daten im angegebenen Zeitraum.")
            return

        print(f"Daten: {len(df)} Zeilen, {sorted(df['zone_id'].unique())}")
        print()

        ergebnisse = evaluiere_baseline(df)
        for horizont, metriken in ergebnisse.items():
            print(f"=== Baseline {horizont} ===")
            print(f"  MAE:  {metriken['mae']:.2f}%")
            print(f"  RMSE: {metriken['rmse']:.2f}%")
            print(f"  R²:   {metriken['r2']:.4f}")
            print(f"  N:    {metriken['n_samples']}")

            if "schwellen_praezision" in metriken:
                print(f"  Schwellen-Praezision: {metriken['schwellen_praezision']:.2%}")
                print(f"  Schwellen-Recall:     {metriken['schwellen_recall']:.2%}")

            if "mae_pro_zone" in metriken:
                print("  MAE pro Zone:")
                for zone_id, zone_mae in sorted(metriken["mae_pro_zone"].items()):
                    print(f"    {zone_id:20s} {zone_mae:.2f}%")
            print()
    finally:
        await speicher.schliessen()


def _lade_ab_metriken(verzeichnis: Path) -> dict[int, dict]:
    """Laedt q50- oder Punkt-Metriken fuer den Monotone-A/B-Vergleich."""
    daten: dict[int, dict] = {}
    for horizont in [6, 12, 24]:
        kandidaten = sorted(
            verzeichnis.glob(f"metriken_{horizont}h_q50_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not kandidaten:
            kandidaten = [
                p for p in sorted(
                    verzeichnis.glob(f"metriken_{horizont}h_*.json"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if "_q10_" not in p.name and "_q90_" not in p.name
            ]
        if not kandidaten:
            continue
        with open(kandidaten[0]) as f:
            rohdaten = json.load(f)
        daten[horizont] = {
            "mae": rohdaten.get("mae"),
            "regen_slice_mae": rohdaten.get("regen_slice_mae"),
            "regen_slice_n": int(rohdaten.get("regen_slice_n") or 0),
        }
    return daten


def _bewerte_monotone_ab(default: dict[int, dict], monotone: dict[int, dict]) -> str:
    """Bewertet A/B nach T-0062c-Kriterien."""
    horizonte = sorted(set(default) & set(monotone))
    if not horizonte:
        return "keine_empfehlung: keine vergleichbaren Horizonte"

    gesamt_ok = all(
        monotone[h]["mae"] is not None
        and default[h]["mae"] is not None
        and float(monotone[h]["mae"]) <= float(default[h]["mae"]) * 1.01
        for h in horizonte
    )

    vergleichbare_regen = []
    for h in horizonte:
        default_slice = default[h].get("regen_slice_mae")
        monotone_slice = monotone[h].get("regen_slice_mae")
        n = min(
            int(default[h].get("regen_slice_n") or 0),
            int(monotone[h].get("regen_slice_n") or 0),
        )
        if default_slice is None or monotone_slice is None or n < 20:
            continue
        vergleichbare_regen.append((h, float(default_slice), float(monotone_slice), n))

    regen_besser = sum(1 for _, alt, neu, _ in vergleichbare_regen if neu <= alt)
    regen_nicht_deutlich_schlechter = all(
        neu <= alt * 1.05
        for _, alt, neu, _ in vergleichbare_regen
    )
    if gesamt_ok and regen_besser >= 2 and regen_nicht_deutlich_schlechter:
        return "uebernehmen"
    if len(vergleichbare_regen) < 2:
        return "nicht_uebernehmen: zu wenig Regen-Slices mit n>=20"
    return "nicht_uebernehmen"


def _drucke_monotone_ab(default: dict[int, dict], monotone: dict[int, dict]) -> None:
    """Druckt Gesamt-MAE und Regen-Slice-MAE side-by-side."""
    print()
    print("=== Monotone-A/B (Dry-Run) ===")
    print("Horizont | MAE default | MAE monotone | Regen default | Regen monotone")
    for h in [6, 12, 24]:
        d = default.get(h, {})
        m = monotone.get(h, {})
        d_mae = d.get("mae")
        m_mae = m.get("mae")
        d_regen = d.get("regen_slice_mae")
        m_regen = m.get("regen_slice_mae")
        d_n = int(d.get("regen_slice_n") or 0)
        m_n = int(m.get("regen_slice_n") or 0)
        print(
            f"{h:>7}h | "
            f"{d_mae if d_mae is not None else '-':>11} | "
            f"{m_mae if m_mae is not None else '-':>12} | "
            f"{d_regen if d_regen is not None else '-':>12} (n={d_n}) | "
            f"{m_regen if m_regen is not None else '-':>13} (n={m_n})"
        )
    print(f"Empfehlung: {_bewerte_monotone_ab(default, monotone)}")


async def cmd_trainiere(args):
    """Trainiert LightGBM-Modelle mit Walk-Forward CV."""
    from bewaesserung.ml.features import FeatureExtraktor
    from bewaesserung.ml.training import TrainingsPipeline

    konfig = _lade_konfig()
    speicher = await _erstelle_speicher(konfig)

    try:
        extraktor = FeatureExtraktor(speicher, konfig)
        await _mit_wartungs_fenster(extraktor, speicher)
        df = await extraktor.erstelle_trainingsdaten(args.von, args.bis)

        if df.empty:
            print("Keine Daten im angegebenen Zeitraum.")
            return

        print(f"Trainingsdaten: {len(df)} Zeilen, "
              f"{sorted(df['zone_id'].unique())}")
        print(f"Zeitraum: {df['zeitstempel'].min()} bis "
              f"{df['zeitstempel'].max()}")
        print()

        if args.monotone_ab:
            import shutil
            ab_root = Path(args.ausgabe).parent / (
                Path(args.ausgabe).name + "_monotone_ab"
            )
            default_dir = ab_root / "default"
            monotone_dir = ab_root / "monotone"
            if ab_root.exists():
                shutil.rmtree(ab_root)
            default_dir.mkdir(parents=True, exist_ok=True)
            monotone_dir.mkdir(parents=True, exist_ok=True)

            print(f"Monotone-A/B Dry-Run: {ab_root}")
            pipeline_default = TrainingsPipeline(
                modell_verzeichnis=str(default_dir),
                n_folds=args.folds,
                monotone_constraints=False,
            )
            pipeline_monotone = TrainingsPipeline(
                modell_verzeichnis=str(monotone_dir),
                n_folds=args.folds,
                monotone_constraints=True,
            )
            if args.quantile:
                pipeline_default.trainiere_quantile(df)
                pipeline_monotone.trainiere_quantile(df)
            else:
                pipeline_default.trainiere(df)
                pipeline_monotone.trainiere(df)
            default_metriken = _lade_ab_metriken(default_dir)
            monotone_metriken = _lade_ab_metriken(monotone_dir)
            _drucke_monotone_ab(default_metriken, monotone_metriken)
            print(f"tmp-Verzeichnis: {ab_root}")
            print("Live-Modelle wurden NICHT geaendert.")
            return

        # T-0082: Cluster-Filter via --cluster <zone_id>. Modelle landen
        # unter <ausgabe>/feuchte/<cluster>/. Ohne --cluster bleibt das
        # alte Verhalten (alle Zonen, flach in <ausgabe>/).
        # T-0161: Resolution VOR Gate-Check, damit der Dry-Run die
        # gleiche Cluster-Awareness hat wie der Hauptlauf und der
        # MlRetrainJob (sonst werden falsche Vergleichswerte gelesen).
        cluster_id = getattr(args, "cluster", None)
        cluster_zonen: list[str] | None = None
        if cluster_id:
            cluster_zonen = [
                z.zone_id for z in konfig.zonen
                if (z.cluster_id or z.zone_id) == cluster_id
            ]
            if not cluster_zonen:
                print(f"Keine Zonen mit cluster_id='{cluster_id}' "
                      "gefunden (default cluster_id == zone_id).")
                return
            print(f"Cluster-Filter: {cluster_id} "
                  f"({len(cluster_zonen)} Zone(n): {cluster_zonen})")

        # T-0048 + T-0161: Dry-Run — temporaer ins <ausgabe>_gatecheck/
        # trainieren und gegen die aktuellen Live-Metriken pruefen.
        # gate_faktor + Vergleichspfad spiegeln den MlRetrainJob (Live).
        if args.gate_check:
            from bewaesserung.ml.retrain_job import (
                _lade_aktuelle_mae,
                _pruefe_gate,
            )
            tmp = Path(args.ausgabe).parent / (Path(args.ausgabe).name + "_gatecheck")
            if tmp.exists():
                import shutil
                shutil.rmtree(tmp)
            tmp.mkdir(parents=True, exist_ok=True)
            pipeline = TrainingsPipeline(
                modell_verzeichnis=str(tmp), n_folds=args.folds,
                monotone_constraints=args.monotone_constraints,
                cluster_id=cluster_id,
                cluster_zonen=cluster_zonen,
            )
            if args.quantile:
                pipeline.trainiere_quantile(df)
            else:
                pipeline.trainiere(df)
            # T-0161 Bug 2: bei --cluster landen Modelle unter
            # <pfad>/feuchte/<cluster>/, der Alt-Pfad muss analog
            # in den Cluster-Subordner zeigen — sonst werden globale
            # gegen Cluster-MAEs verglichen (faktisch zufaellig).
            if cluster_id:
                alt_pfad = Path(args.ausgabe) / "feuchte" / cluster_id
                neu_pfad = tmp / "feuchte" / cluster_id
                gate_faktor = konfig.ml_retrain.gate_faktor_pro_cluster.get(
                    cluster_id, konfig.ml_retrain.gate_faktor,
                )
            else:
                alt_pfad = Path(args.ausgabe)
                neu_pfad = tmp
                gate_faktor = konfig.ml_retrain.gate_faktor
            alt = _lade_aktuelle_mae(alt_pfad)
            neu = _lade_aktuelle_mae(neu_pfad)
            # T-0161 Bug 1: gate_faktor aus Konfig statt Hardcode 0.95.
            ergebnis = _pruefe_gate(alt, neu, gate_faktor=gate_faktor)
            print()
            print("=== Gate-Check (Dry-Run) ===")
            print(f"  Cluster:        {cluster_id or '(global)'}")
            print(f"  gate_faktor:    {gate_faktor:.3f}  (aus Konfig)")
            print(f"  Alt-Pfad:       {alt_pfad}")
            print(f"  Neu-Pfad:       {neu_pfad}")
            print(f"  aktuelle MAE:   {alt}")
            print(f"  neue MAE:       {neu}")
            print(f"  Gate passiert:  {ergebnis['passed']}")
            if not ergebnis["passed"]:
                for f in ergebnis["fehlschlaege"]:
                    print(f"    ❌ {f}")
            print(f"  tmp-Verzeichnis: {tmp}")
            print("  (Live-Modelle wurden NICHT geaendert.)")
            return

        pipeline = TrainingsPipeline(
            modell_verzeichnis=args.ausgabe,
            n_folds=args.folds,
            monotone_constraints=args.monotone_constraints,
            cluster_id=cluster_id,
            cluster_zonen=cluster_zonen,
        )

        if args.quantile:
            ergebnisse_q = pipeline.trainiere_quantile(df)
            print()
            for (horizont, alpha_int), ergebnis in ergebnisse_q.items():
                m = ergebnis.metriken
                print(f"=== Quantile-Modell {horizont}h q{alpha_int} ===")
                print(f"  ML  MAE: {m.mae:.3f}%  RMSE: {m.rmse:.2f}%")
                print(f"  BL  MAE: {m.baseline_mae:.3f}%  "
                      f"RMSE: {m.baseline_rmse:.2f}%")
                if m.pinball_loss is not None:
                    baseline_pinball = (
                        f"{m.baseline_pinball_loss:.3f}%"
                        if m.baseline_pinball_loss is not None else "n/a"
                    )
                    print(
                        f"  ML  Pinball: {m.pinball_loss:.3f}%  "
                        f"BL Pinball: {baseline_pinball}"
                    )
                if m.monotone_constraints:
                    print(f"  Monotone Regen-Features: {len(m.monotone_features)}")
                if m.regen_slice_mae is not None:
                    print(
                        f"  Regen-Slice MAE: {m.regen_slice_mae:.3f}% "
                        f"(n={m.regen_slice_n})"
                    )
                if m.feature_importances:
                    print("  Top Features:")
                    for feat, imp in list(m.feature_importances.items())[:5]:
                        print(f"    {feat:35s} {imp:.2%}")
                print()
            print(f"9 Quantile-Modelle gespeichert in: {args.ausgabe}")
            return

        ergebnisse = pipeline.trainiere(df)

        print()
        for horizont, ergebnis in ergebnisse.items():
            m = ergebnis.metriken
            print(f"=== Modell {horizont}h ===")
            print(f"  ML  MAE:  {m.mae:.2f}%  RMSE: {m.rmse:.2f}%  "
                  f"R²: {m.r2:.4f}")
            print(f"  BL  MAE:  {m.baseline_mae:.2f}%  "
                  f"RMSE: {m.baseline_rmse:.2f}%  R²: {m.baseline_r2:.4f}")
            if m.monotone_constraints:
                print(f"  Monotone Regen-Features: {len(m.monotone_features)}")
            if m.regen_slice_mae is not None:
                print(
                    f"  Regen-Slice MAE: {m.regen_slice_mae:.3f}% "
                    f"(n={m.regen_slice_n})"
                )
            verbesserung = (
                (m.baseline_mae - m.mae) / m.baseline_mae * 100
                if m.baseline_mae > 0 else 0
            )
            print(f"  Verbesserung: {verbesserung:+.1f}% MAE")

            if m.feature_importances:
                print("  Top Features:")
                for feat, imp in list(m.feature_importances.items())[:10]:
                    print(f"    {feat:35s} {imp:.2%}")

            if m.mae_pro_zone:
                print("  MAE pro Zone:")
                for zone_id, zone_mae in sorted(m.mae_pro_zone.items()):
                    print(f"    {zone_id:20s} {zone_mae:.2f}%")
            print()

        print(f"Modelle gespeichert in: {args.ausgabe}")
    finally:
        await speicher.schliessen()


def _effektive_min_events(cli_wert, konfig) -> int:
    """T-0070: CLI-Argument hat Vorrang, sonst Config-Default.

    `--min-events` ist auf argparse-Ebene optional. Wenn nicht gesetzt,
    faellt der Wert auf `konfig.ml.bewaesserungs_response.min_events`
    zurueck — dieselbe Schwelle, die auch der MlResponseRetrainJob im
    Service-Loop nutzt. Sonst driften CLI und Service auseinander.
    """
    if cli_wert is not None:
        return int(cli_wert)
    return int(konfig.ml_bewaesserungs_response.min_events)


async def cmd_trainiere_response(args):
    """T-0065: Trainiert Response-Modelle (Forward + Inverse) pro Zone."""
    from bewaesserung.ml.response_features import erstelle_response_features
    from bewaesserung.ml.response_training import ResponseTrainingsPipeline

    konfig = _lade_konfig()
    speicher = await _erstelle_speicher(konfig)

    min_events = _effektive_min_events(args.min_events, konfig)
    if args.min_events is None:
        print(f"min-events aus Config: {min_events}")
    else:
        print(f"min-events aus CLI:    {min_events}")

    try:
        von = args.seit or (datetime.now() - timedelta(days=365))
        bis = datetime.now()
        print(f"Event-Fenster: {von.isoformat()} .. {bis.isoformat()}")
        df = await erstelle_response_features(speicher, konfig, von=von, bis=bis)
        if df.empty:
            print("Keine Events im Fenster — nichts zu trainieren.")
            return
        print(f"Events gesamt: {len(df)}")
        for z in sorted(df["zone_id"].unique()):
            n = int((df["zone_id"] == z).sum())
            print(f"  {z:30s} {n:4d} Events")

        ausgabe = Path(args.ausgabe)
        ausgabe.mkdir(parents=True, exist_ok=True)

        gefilterte_zonen: list[str] = (
            [args.zone] if args.zone else sorted(df["zone_id"].unique())
        )
        for zone_id in gefilterte_zonen:
            n_zone = int((df["zone_id"] == zone_id).sum())
            if n_zone == 0:
                print(f"\n=== {zone_id}: keine Events ===")
                continue
            print(f"\n=== {zone_id} (n={n_zone}) ===")
            pipeline = ResponseTrainingsPipeline(
                zone_id=zone_id,
                basis_verzeichnis=ausgabe,
                min_events=min_events,
            )
            erg = pipeline.lauf(df)
            print(f"  Status:  {erg.status}")
            if erg.grund:
                print(f"  Grund:   {erg.grund}")
            if erg.status == "ok":
                print(f"  Version: {erg.version}")
                print(f"  MAE Forward q10/q50/q90: {erg.forward_metriken}")
                print(f"  MAE Inverse (Sekunden):  {erg.inverse_mae_s}")
                print(f"  MAPE Inverse:            {erg.inverse_mape}")
                print(f"  Liter/s Median:          {erg.liter_pro_sekunde_median}")
        if args.dry_run:
            print("\n(Dry-Run-Hinweis: Pipeline schreibt trotzdem Modelle + Symlinks "
                  "ins Verzeichnis. Fuer echten Dry-Run ein temp-Verzeichnis via "
                  "--ausgabe angeben.)")
    finally:
        await speicher.schliessen()


async def cmd_giessen(args):
    """Loggt eine manuelle Bewaesserung."""
    from bewaesserung.modelle import Ausloser, VentilAktion, VentilEreignis

    konfig = _lade_konfig()
    # T-0162: cmd_giessen schreibt das Ventil-Event in die DB -> RW.
    speicher = await _erstelle_speicher(konfig, modus="rw")

    try:
        bekannte_zonen = {z.zone_id for z in konfig.zonen}
        if args.zone not in bekannte_zonen:
            print(f"Unbekannte Zone: {args.zone}")
            print(f"Verfuegbar: {sorted(bekannte_zonen)}")
            return

        jetzt = datetime.now()
        ereignis = VentilEreignis(
            zeitstempel=jetzt,
            zone_id=args.zone,
            ventil_id="manuell",
            aktion=VentilAktion.OEFFNEN,
            dauer_sekunden=args.dauer,
            ausloser=Ausloser.MANUELL,
        )
        await speicher.speichere_ventil_ereignis(ereignis)
        print(f"Manuelles Giessen geloggt: {args.zone} "
              f"({args.dauer}s) um {jetzt.strftime('%H:%M:%S')}")
    finally:
        await speicher.schliessen()


async def cmd_info(args):
    """Zeigt Datenbank-Statistiken fuer ML-Relevanz."""
    konfig = _lade_konfig()
    speicher = await _erstelle_speicher(konfig)

    try:
        assert speicher._db is not None
        # Messungen pro Zone
        async with speicher._db.execute(
            """SELECT zone_id, COUNT(*) as n,
                      MIN(zeitstempel) as von, MAX(zeitstempel) as bis
               FROM sensor_messung
               WHERE boden_feuchte IS NOT NULL
               GROUP BY zone_id ORDER BY zone_id"""
        ) as cursor:
            zeilen = await cursor.fetchall()

        print("=== Sensordaten ===")
        gesamt = 0
        for z in zeilen:
            print(f"  {z['zone_id']:20s} {z['n']:6d} Messungen "
                  f"({z['von'][:10]} bis {z['bis'][:10]})")
            gesamt += z["n"]
        print(f"  {'GESAMT':20s} {gesamt:6d}")

        # Ventilereignisse
        async with speicher._db.execute(
            "SELECT COUNT(*) as n FROM ventil_ereignis"
        ) as cursor:
            zeile = await cursor.fetchone()
        print(f"\n=== Ventilereignisse: {zeile['n']} ===")

        # Wetter-Vorhersagen
        async with speicher._db.execute(
            """SELECT standort_id, COUNT(*) as n,
                      MIN(abfrage_zeitstempel) as von
               FROM wetter_vorhersage
               GROUP BY standort_id"""
        ) as cursor:
            zeilen = await cursor.fetchall()
        print("\n=== Wetter-Vorhersagen ===")
        for z in zeilen:
            print(f"  {z['standort_id']:20s} {z['n']:6d} Stunden (ab {z['von'][:10]})")

        # Trainingsbereitschaft
        print("\n=== ML-Status ===")
        if gesamt < 5000:
            wochen_noetig = max(1, (5000 - gesamt) // (gesamt // max(1, len(zeilen)) * 7))
            print(f"  Empfehlung: Noch ~{wochen_noetig} Wochen Daten sammeln "
                  f"(aktuell {gesamt}, Ziel ~5000+)")
        else:
            print(f"  {gesamt} Messungen verfuegbar — Training moeglich!")

    finally:
        await speicher.schliessen()


def _parse_datum(s: str) -> datetime:
    """Parst Datum im Format YYYY-MM-DD."""
    return datetime.strptime(s, "%Y-%m-%d")


def main():
    parser = argparse.ArgumentParser(
        description="ML-Tools fuer Bodenfeuchte-Vorhersage"
    )
    sub = parser.add_subparsers(dest="befehl", required=True)

    # exportiere
    p_exp = sub.add_parser("exportiere", help="Feature-DataFrame als CSV exportieren")
    p_exp.add_argument("--von", type=_parse_datum, required=True)
    p_exp.add_argument("--bis", type=_parse_datum, required=True)
    from bewaesserung.konfig import ML_DATEN_PFAD
    p_exp.add_argument("--ausgabe", default=str(Path(ML_DATEN_PFAD) / "features.csv"))

    # trainiere
    p_train = sub.add_parser("trainiere", help="LightGBM-Modelle trainieren")
    p_train.add_argument("--von", type=_parse_datum, required=True)
    p_train.add_argument("--bis", type=_parse_datum, required=True)
    p_train.add_argument("--ausgabe", default=ML_DATEN_PFAD)
    p_train.add_argument("--folds", type=int, default=3)
    p_train.add_argument(
        "--quantile", action="store_true",
        help="T-0046: 9 Quantile-Modelle (3 Horizonte x q10/q50/q90) trainieren",
    )
    p_train.add_argument(
        "--gate-check", action="store_true",
        help="T-0048: Dry-Run — trainiert ins tmp-Verzeichnis und pruefe Gate, "
             "ohne Live-Modelle zu veraendern",
    )
    p_train.add_argument(
        "--monotone-constraints", action="store_true",
        help="T-0062c: Regen-Summen monoton steigend constrainen (Opt-In)",
    )
    p_train.add_argument(
        "--monotone-ab", action="store_true",
        help="T-0062c: Default vs. Monotone in Temp-Verzeichnissen vergleichen",
    )
    p_train.add_argument(
        "--cluster", default=None,
        help=(
            "T-0082: Pro-Cluster-Training — wenn gesetzt, werden Daten "
            "auf die zum Cluster gehoerigen Zonen gefiltert und Modelle "
            "unter <ausgabe>/feuchte/<cluster>/ abgelegt. Standard: "
            "kein Filter (= heutiges Backward-Compat-Verhalten)."
        ),
    )

    # trainiere-response (T-0065)
    p_resp = sub.add_parser(
        "trainiere-response",
        help="T-0065: Response-Modelle (Forward + Inverse) pro Zone trainieren",
    )
    p_resp.add_argument(
        "--zone", default=None,
        help="Nur diese Zone trainieren (sonst alle Zonen aus der Config).",
    )
    p_resp.add_argument(
        "--min-events", type=int, default=None,
        help="Minimale Event-Anzahl pro Zone (sonst Pipeline.status=uebersprungen). "
             "Default: Wert aus config/default.yaml -> ml.bewaesserungs_response.min_events.",
    )
    p_resp.add_argument(
        "--seit", type=_parse_datum, default=None,
        help="Ab diesem Datum Events beruecksichtigen (Default: jetzt-365d).",
    )
    p_resp.add_argument(
        "--ausgabe",
        default=str(Path(ML_DATEN_PFAD) / "response"),
        help="Basis-Verzeichnis fuer Modelle + Symlinks.",
    )
    p_resp.add_argument(
        "--dry-run", action="store_true",
        help="Hinweis: Pipeline schreibt immer. Fuer reines Diagnose-Training eine "
             "Temp-Ausgabe setzen.",
    )

    # baseline
    p_base = sub.add_parser("baseline", help="Regelbasierte Baseline evaluieren")
    p_base.add_argument("--von", type=_parse_datum, required=True)
    p_base.add_argument("--bis", type=_parse_datum, required=True)

    # giessen
    p_giessen = sub.add_parser("giessen", help="Manuelle Bewaesserung loggen")
    p_giessen.add_argument("--zone", required=True, help="Zone-ID")
    p_giessen.add_argument("--dauer", type=int, default=0, help="Dauer in Sekunden")

    # info
    sub.add_parser("info", help="Datenbank-Statistiken anzeigen")

    args = parser.parse_args()

    if args.befehl == "exportiere":
        asyncio.run(cmd_exportiere(args))
    elif args.befehl == "trainiere":
        asyncio.run(cmd_trainiere(args))
    elif args.befehl == "trainiere-response":
        asyncio.run(cmd_trainiere_response(args))
    elif args.befehl == "baseline":
        asyncio.run(cmd_baseline(args))
    elif args.befehl == "giessen":
        asyncio.run(cmd_giessen(args))
    elif args.befehl == "info":
        asyncio.run(cmd_info(args))


if __name__ == "__main__":
    main()
