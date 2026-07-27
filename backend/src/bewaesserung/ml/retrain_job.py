"""T-0048: In-Service ML-Retrain mit Deployment-Gate.

Ablauf des Jobs (Muster wie BackupJob/WetterArchivJob):
  1. Intervall-Gate (Default 7 Tage).
  2. Feature-Extraktion ueber das konfigurierte Zeitfenster.
  3. Training im Temporaer-Verzeichnis (`<ausgabe>_tmp/`).
  4. Vergleich: fuer JEDEN Horizont muss `mae_neu < gate_faktor * mae_alt`
     gelten, sonst Rollback ohne Deploy.
  5. Atomarer Deploy: alte Modelle nach `<ausgabe>_archiv/<datum>/`
     verschieben, tmp nach `<ausgabe>/` verschieben, Symlinks bestehen
     bleiben (wurden bereits beim Training gesetzt).

Fehler-Isolation: ein Exception-Pfad bleibt ohne Wirkung (kein Deploy,
`_letzte_aktualisierung` bleibt ungesetzt, naechster Zyklus retry't).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import structlog

from bewaesserung.modelle import GesamtKonfig, MlRetrainKonfig
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()


class MlRetrainJob:
    """Woechentlicher ML-Retrain im Entscheidungs-Loop mit Gate."""

    def __init__(
        self,
        speicher: Speicher,
        konfig: GesamtKonfig,
        retrain_konfig: MlRetrainKonfig,
        ausgabe_pfad: str = "",
    ):
        self._speicher = speicher
        self._konfig = konfig
        self._retrain = retrain_konfig
        if not ausgabe_pfad and not retrain_konfig.ausgabe_pfad:
            from bewaesserung.konfig import ML_DATEN_PFAD
            ausgabe_pfad = ML_DATEN_PFAD
        self._ausgabe = Path(ausgabe_pfad or retrain_konfig.ausgabe_pfad)
        self._intervall = timedelta(days=retrain_konfig.intervall_tage)
        self._start_delay = timedelta(minutes=retrain_konfig.start_verzoegerung_minuten)
        # Start-Anker so legen, dass der erste Lauf erst nach `start_delay`
        # Minuten faellig ist. Sonst wuerde jeder Service-Restart die 2 Min
        # CPU-Last direkt mit der Inferenz-Welle vom Browser kollidieren
        # lassen und das Dashboard fuehlt sich 5+ Min traege an.
        self._letzte_aktualisierung: datetime | None = (
            datetime.now() - self._intervall + self._start_delay
            if retrain_konfig.start_verzoegerung_minuten > 0
            else None
        )
        self._letztes_ergebnis: dict | None = None
        # T-0108: Fehler-/Erfolg-Sichtbarkeit fuer /api/ml/status.
        # `_letzter_erfolg` ist der echte letzte erfolgreiche Lauf —
        # NICHT `_letzte_aktualisierung`, das ist ein Timing-Anker, der
        # im Fehlerpfad bewusst zurueckdatiert wird (Retry-Steuerung).
        self._letzter_erfolg: datetime | None = None
        self._letzter_fehler: dict | None = None

    @property
    def letztes_ergebnis(self) -> dict | None:
        """Kurzstatus des letzten Laufs — fuer /api/ml/status."""
        return self._letztes_ergebnis

    @property
    def letzter_erfolg(self) -> datetime | None:
        """Zeitpunkt des letzten erfolgreichen Laufs (T-0108)."""
        return self._letzter_erfolg

    @property
    def letzter_fehler(self) -> dict | None:
        """`{zeit, typ, nachricht}` des letzten Crashs, sonst None (T-0108).

        Wird beim naechsten erfolgreichen Lauf auf None zurueckgesetzt,
        damit das Frontend-Banner automatisch verschwindet.
        """
        return self._letzter_fehler

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> bool:
        """Laeuft hoechstens einmal pro Intervall. True wenn ausgefuehrt."""
        if not self._retrain.aktiv:
            return False
        jetzt = jetzt or datetime.now()
        if (self._letzte_aktualisierung
                and jetzt - self._letzte_aktualisierung < self._intervall):
            return False
        # F1: tmp-Dir-Cleanup MUSS in jedem Pfad laufen — auch wenn das
        # Training mit einer unerwarteten Exception abbricht (dann koennen
        # dort halbgeschriebene Checkpoints liegen, die den naechsten Lauf
        # verwirren). Bei Fehler bleibt `_letzte_aktualisierung` aber
        # bewusst ungesetzt, damit der Loop beim naechsten Tick sofort
        # retry't — ein hard-failing Retrain ist seltener als ein flaky
        # Datenzugriff.
        try:
            try:
                ergebnis = await self._fuehre_aus(jetzt)
            except Exception as exc:
                logger.exception("ml.retrain.fehler")
                self._letztes_ergebnis = {
                    "status": "fehler",
                    "fehler_typ": type(exc).__name__,
                    "fehler_nachricht": str(exc)[:500],
                    "zeit": jetzt.isoformat(),
                }
                # T-0108: einheitliches Fehler-Feld fuer /api/ml/status.
                self._letzter_fehler = {
                    "zeit": jetzt.isoformat(),
                    "typ": type(exc).__name__,
                    "nachricht": str(exc)[:300],
                }
                # Nach Fehler: Anker nach vorne setzen, damit naechster
                # Versuch in `start_delay` Minuten laeuft (nicht erst in
                # 7 Tagen, aber auch nicht im naechsten 60-s-Loop-Tick).
                if self._start_delay.total_seconds() > 0:
                    self._letzte_aktualisierung = jetzt - self._intervall + self._start_delay
                return False
        finally:
            tmp_pfad = (
                self._ausgabe.parent
                / f"{self._ausgabe.name}{self._retrain.tmp_suffix}"
            )
            if tmp_pfad.exists():
                shutil.rmtree(tmp_pfad, ignore_errors=True)
        self._letzte_aktualisierung = jetzt
        self._letztes_ergebnis = ergebnis
        # T-0108: erfolgreicher Lauf — Fehler-Banner wieder aufloesen.
        self._letzter_erfolg = jetzt
        self._letzter_fehler = None
        return True

    async def _fuehre_aus(self, jetzt: datetime) -> dict:
        from bewaesserung.ml.features import FeatureExtraktor
        from bewaesserung.ml.training import TrainingsPipeline

        von = jetzt - timedelta(days=self._retrain.trainings_fenster_tage)

        extraktor = FeatureExtraktor(self._speicher, self._konfig)
        # T-0228 Stufe 2c: dynamische Wartungs-Fenster aus DB in den
        # Feature-Filter einspielen (zusaetzlich zu YAML-`ml_ausschluss_
        # fenster`). Verhindert dass Wartungs-Zeitraeume ins Training
        # einfliessen, wenn der User per UI/API ein Fenster gesetzt hat.
        try:
            wartung = await self._speicher.hole_wartungs_fenster(nur_offen=False)
            extraktor.setze_wartungs_fenster(wartung)
        except Exception:
            logger.exception("ml.retrain.wartungs_fenster_load_fehler")
        df = await extraktor.erstelle_trainingsdaten(von=von, bis=jetzt)
        if df.empty or len(df) < 200:
            logger.warning(
                "ml.retrain.zu_wenig_daten",
                zeilen=len(df), fenster_tage=self._retrain.trainings_fenster_tage,
            )
            return {
                "status": "abgebrochen",
                "grund": "zu_wenig_daten",
                "zeilen": len(df),
                "zeit": jetzt.isoformat(),
            }

        # T-0082: Pro-Cluster-Iteration. Bei `cluster_strategie=global`
        # (Default-Backward-Compat) wird der alte Pfad ausgefuehrt.
        if self._retrain.cluster_strategie == "pro_zone":
            return await self._fuehre_aus_pro_cluster(jetzt, df)

        tmp_pfad = self._ausgabe.parent / f"{self._ausgabe.name}{self._retrain.tmp_suffix}"
        if tmp_pfad.exists():
            shutil.rmtree(tmp_pfad)
        tmp_pfad.mkdir(parents=True, exist_ok=True)

        pipeline = TrainingsPipeline(
            modell_verzeichnis=str(tmp_pfad),
            n_folds=self._retrain.folds,
            monotone_constraints=self._retrain.monotone_constraints,
        )
        # LightGBM-Training ist CPU-heavy und synchron — im Event-Loop
        # wuerde es API + WebSocket fuer mehrere Minuten blockieren. In
        # einen Worker-Thread auslagern, damit der Loop Requests weiter
        # bedienen kann.
        if self._retrain.quantile:
            await asyncio.to_thread(pipeline.trainiere_quantile, df)
        else:
            await asyncio.to_thread(pipeline.trainiere, df)

        alte_mae = _lade_aktuelle_mae(self._ausgabe)
        neue_mae = _lade_aktuelle_mae(tmp_pfad)

        gate_details = _pruefe_gate(
            alte_mae, neue_mae, self._retrain.gate_faktor,
            alte_modelle=_modell_horizonte(self._ausgabe),
        )

        if not gate_details["passed"]:
            logger.warning(
                "ml.retrain.abgelehnt",
                mae_alt=alte_mae, mae_neu=neue_mae,
                gate_faktor=self._retrain.gate_faktor,
                fehlschlaege=gate_details["fehlschlaege"],
            )
            shutil.rmtree(tmp_pfad, ignore_errors=True)
            return {
                "status": "abgelehnt",
                "mae_alt": alte_mae, "mae_neu": neue_mae,
                "gate_faktor": self._retrain.gate_faktor,
                "fehlschlaege": gate_details["fehlschlaege"],
                "zeit": jetzt.isoformat(),
            }

        archiv_pfad = (
            self._ausgabe.parent
            / f"{self._ausgabe.name}{self._retrain.archiv_suffix}"
            / jetzt.strftime("%Y-%m-%d_%H%M%S")
        )
        _swap_live_mit_tmp(
            self._ausgabe, tmp_pfad, archiv_pfad,
            archiv_behalten=_ARCHIV_RETENTION,
        )

        # F8: Delta-Log um langfristige Drift-Trends lesbar zu machen.
        # quote = mae_neu / mae_alt; 0.94 passiert knapp durch Gate 0.95.
        quoten = {
            h: round(neue_mae[h] / alte_mae[h], 3)
            for h in (6, 12, 24)
            if h in alte_mae and h in neue_mae and alte_mae[h] > 0
        }
        logger.info(
            "ml.retrain.uebernommen",
            mae_alt=alte_mae, mae_neu=neue_mae,
            quoten_neu_zu_alt=quoten,
            gate_faktor=self._retrain.gate_faktor,
            archiv=str(archiv_pfad),
        )
        return {
            "status": "uebernommen",
            "mae_alt": alte_mae, "mae_neu": neue_mae,
            "archiv": str(archiv_pfad),
            "zeit": jetzt.isoformat(),
        }


    async def _fuehre_aus_pro_cluster(
        self, jetzt: datetime, df,
    ) -> dict:
        """T-0082: pro Cluster eine Trainings-Pipeline. Iteriert ueber
        alle distinkten `cluster_id`s aus der Konfig (default = zone_id),
        prueft Mindest-Zeilenzahl, trainiert + gate-prueft + deployed
        pro Cluster atomar.
        """
        from bewaesserung.ml.training import TrainingsPipeline

        # Cluster → list[zone_id], abgeleitet aus den Zonen-Konfigs.
        cluster_zonen: dict[str, list[str]] = {}
        for zone in self._konfig.zonen:
            cid = zone.cluster_id or zone.zone_id
            cluster_zonen.setdefault(cid, []).append(zone.zone_id)

        ergebnisse: dict[str, dict] = {}
        # Wurzel-Verzeichnis fuer Pro-Cluster-Modelle: <ausgabe>/feuchte/.
        feuchte_basis = self._ausgabe / "feuchte"
        feuchte_basis.mkdir(parents=True, exist_ok=True)

        for cluster_id, zone_ids in sorted(cluster_zonen.items()):
            zonen_df = df[df["zone_id"].isin(zone_ids)]
            n = len(zonen_df)
            if n < self._retrain.mindest_zeilen_pro_cluster:
                logger.info(
                    "ml.retrain.cluster.unzureichend_daten "
                    "cluster=%s n=%d schwelle=%d",
                    cluster_id, n, self._retrain.mindest_zeilen_pro_cluster,
                )
                ergebnisse[cluster_id] = {
                    "status": "unzureichend_daten",
                    "n_zeilen": n,
                    "schwelle": self._retrain.mindest_zeilen_pro_cluster,
                }
                continue

            cluster_dir = feuchte_basis / cluster_id
            tmp_dir = (
                feuchte_basis / f"{cluster_id}{self._retrain.tmp_suffix}"
            )
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)
            tmp_dir.mkdir(parents=True, exist_ok=True)

            pipeline = TrainingsPipeline(
                modell_verzeichnis=str(tmp_dir),
                n_folds=self._retrain.folds,
                monotone_constraints=self._retrain.monotone_constraints,
                # cluster_id NICHT setzen — wir nutzen tmp_dir direkt als
                # Wurzel; sonst wuerde Pipeline zusaetzlich `feuchte/<cid>`
                # darunter anlegen (Doppel-Verschachtelung).
            )
            try:
                if self._retrain.quantile:
                    await asyncio.to_thread(
                        pipeline.trainiere_quantile, zonen_df,
                    )
                else:
                    await asyncio.to_thread(pipeline.trainiere, zonen_df)
            except Exception as exc:
                logger.exception(
                    "ml.retrain.cluster.fehler cluster=%s", cluster_id,
                )
                ergebnisse[cluster_id] = {
                    "status": "fehler",
                    "fehler_typ": type(exc).__name__,
                    "fehler_nachricht": str(exc)[:300],
                    "n_zeilen": n,
                }
                shutil.rmtree(tmp_dir, ignore_errors=True)
                continue

            alte_mae = _lade_aktuelle_mae(cluster_dir) if cluster_dir.exists() else {}
            neue_mae = _lade_aktuelle_mae(tmp_dir)
            alte_modelle = (
                _modell_horizonte(cluster_dir) if cluster_dir.exists() else set()
            )
            gate_faktor = self._retrain.gate_faktor_pro_cluster.get(
                cluster_id, self._retrain.gate_faktor,
            )
            gate_details = _pruefe_gate(
                alte_mae, neue_mae, gate_faktor, alte_modelle=alte_modelle,
            )

            if not gate_details["passed"]:
                logger.warning(
                    "ml.retrain.cluster.abgelehnt cluster=%s "
                    "mae_alt=%s mae_neu=%s gate=%.2f fehlschlaege=%s",
                    cluster_id, alte_mae, neue_mae,
                    gate_faktor, gate_details["fehlschlaege"],
                )
                shutil.rmtree(tmp_dir, ignore_errors=True)
                ergebnisse[cluster_id] = {
                    "status": "abgelehnt",
                    "mae_alt": alte_mae,
                    "mae_neu": neue_mae,
                    "gate_faktor": gate_faktor,
                    "fehlschlaege": gate_details["fehlschlaege"],
                    "n_zeilen": n,
                }
                continue

            # Atomic-Swap pro Cluster.
            archiv_pfad = (
                feuchte_basis
                / f"{cluster_id}{self._retrain.archiv_suffix}"
                / jetzt.strftime("%Y-%m-%d_%H%M%S")
            )
            _swap_live_mit_tmp(
                cluster_dir, tmp_dir, archiv_pfad,
                archiv_behalten=_ARCHIV_RETENTION,
            )
            quoten = {
                h: round(neue_mae[h] / alte_mae[h], 3)
                for h in (6, 12, 24)
                if h in alte_mae and h in neue_mae and alte_mae[h] > 0
            }
            logger.info(
                "ml.retrain.cluster.uebernommen cluster=%s "
                "mae_alt=%s mae_neu=%s quoten=%s gate=%.2f archiv=%s",
                cluster_id, alte_mae, neue_mae, quoten,
                gate_faktor, archiv_pfad,
            )
            ergebnisse[cluster_id] = {
                "status": "uebernommen",
                "mae_alt": alte_mae,
                "mae_neu": neue_mae,
                "gate_faktor": gate_faktor,
                "quoten_neu_zu_alt": quoten,
                "archiv": str(archiv_pfad),
                "n_zeilen": n,
            }
        return {
            "status": "pro_cluster",
            "cluster": ergebnisse,
            "zeit": jetzt.isoformat(),
        }


def _modell_horizonte(verzeichnis: Path) -> set[int]:
    """T-0399: Horizonte, fuer die ein `aktuell_Xh.lgbm`-Symlink existiert.

    `_lade_aktuelle_mae` kann "kein Modell da" und "Modell da, Metrik unlesbar"
    nicht unterscheiden -- beides liefert ein leeres Dict. `_pruefe_gate` deutete
    das als Erstlauf und akzeptierte JEDEN vollstaendig trainierten Kandidaten
    ohne MAE-Vergleich. Diese Menge macht den Unterschied explizit, damit das
    Gate nicht still aufgeht, wenn eine metriken_*.json fehlt oder korrupt ist.
    """
    return {
        h for h in (6, 12, 24)
        if (verzeichnis / f"aktuell_{h}h.lgbm").exists()
    }


def _lade_aktuelle_mae(verzeichnis: Path) -> dict[int, float]:
    """Liest MAE aus den Metriken-Dateien neben den aktuell_Xh.lgbm-Symlinks.

    Leeres Dict wenn keine Modelle vorhanden ODER Metriken nicht lesbar. Diese
    beiden Faelle sind hier NICHT unterscheidbar -- der Aufrufer muss zusaetzlich
    `_modell_horizonte()` heranziehen (T-0399), sonst deutet das Gate einen
    Metriken-Verlust als Erstlauf.
    """
    out: dict[int, float] = {}
    for h in (6, 12, 24):
        symlink = verzeichnis / f"aktuell_{h}h.lgbm"
        if not symlink.exists():
            continue
        try:
            modell_stem = symlink.resolve().stem
            datum_suffix = modell_stem.split("_")[-1]  # YYYY-MM-DD
        except (OSError, IndexError):
            continue
        metriken_kandidaten = [
            verzeichnis / f"metriken_{h}h_{datum_suffix}.json",
            verzeichnis / f"metriken_{h}h_q50_{datum_suffix}.json",
        ]
        for pfad in metriken_kandidaten:
            if not pfad.exists():
                continue
            try:
                with open(pfad) as f:
                    daten = json.load(f)
                out[h] = float(daten["mae"])
                break
            except (OSError, ValueError, KeyError):
                continue
    return out


def _pruefe_gate(
    alte_mae: dict[int, float],
    neue_mae: dict[int, float],
    gate_faktor: float,
    alte_modelle: set[int] | None = None,
) -> dict:
    """Pro Horizont: mae_neu < gate_faktor * mae_alt. Alle 3 muessen bestehen.

    `alte_modelle` (T-0399): Horizonte mit vorhandenem Modell-Symlink, aus
    `_modell_horizonte()`. Ohne dieses Argument ist "Modelle da, Metriken
    unlesbar" von "Erstlauf" nicht unterscheidbar -- Default `None` erhaelt das
    alte Verhalten fuer Aufrufer, die keine Modelle haben koennen (Tests).
    """
    fehlschlaege: list[dict] = []
    vorhandene_modelle = set(alte_modelle or ())

    # T-0399: Der Erstlauf-Pfad gilt NUR, wenn wirklich kein Modell existiert.
    # Gibt es Modelle, deren Metriken aber fehlen/korrupt sind, ist das ein
    # Fehlschlag -- sonst deployt ein beliebiger Kandidat am Gate vorbei, und
    # ein Metriken-Verlust waere von einem sauberen Erstlauf ununterscheidbar.
    if not alte_mae and vorhandene_modelle:
        return {
            "passed": False,
            "fehlschlaege": [
                {
                    "horizont_h": h,
                    "mae_alt": None,
                    "mae_neu": neue_mae.get(h),
                    "grund": "metrik_fehlt",
                }
                for h in sorted(vorhandene_modelle)
            ],
        }

    # Wenn ueberhaupt keine alten MAE-Werte: erster Retrain → only accept,
    # wenn ALLE erwarteten Horizonte (6/12/24) trainiert wurden. T-0101:
    # vorher reichte irgendein neuer Wert, was beim Cluster-Initial-Deploy
    # einen teiltrainierten Cluster (z. B. nur 6 h) produktiv setzen konnte.
    if not alte_mae:
        if not neue_mae:
            return {
                "passed": False,
                "fehlschlaege": [{"grund": "keine_neue_metriken"}],
            }
        fehlende = [h for h in (6, 12, 24) if h not in neue_mae]
        if fehlende:
            return {
                "passed": False,
                "fehlschlaege": [{
                    "grund": "initial_unvollstaendig",
                    "fehlende_horizonte_h": fehlende,
                }],
            }
        return {"passed": True, "fehlschlaege": []}

    for h in (6, 12, 24):
        alt = alte_mae.get(h)
        neu = neue_mae.get(h)
        if alt is None or neu is None:
            fehlschlaege.append({
                "horizont_h": h, "mae_alt": alt, "mae_neu": neu,
                "grund": "metrik_fehlt",
            })
            continue
        schwelle = gate_faktor * alt
        if neu >= schwelle:
            fehlschlaege.append({
                "horizont_h": h, "mae_alt": round(alt, 3),
                "mae_neu": round(neu, 3), "schwelle": round(schwelle, 3),
                "grund": "ueber_gate",
            })
    return {"passed": len(fehlschlaege) == 0, "fehlschlaege": fehlschlaege}


# T-0219: Anzahl Archiv-Versionen, die pro `_archiv`-Ordner behalten
# werden. Jeder uebernommene Retrain schiebt die alten Modelle in einen
# neuen `<datum>`-Unterordner; ohne Retention wuchsen die Archive
# unbegrenzt (~900 MB in `ml/feuchte/*_archiv/` bis 20.05.2026).
# 5 Versionen reichen als Rollback-Reserve; aeltere werden geloescht.
_ARCHIV_RETENTION = 5


def _raeume_alte_archive(archiv_basis: Path, behalten: int) -> None:
    """T-0219: Loescht in `archiv_basis` alle Datums-Unterordner ausser
    den juengsten `behalten`.

    Die Ordnernamen sind `YYYY-MM-DD_HHMMSS` (siehe `_swap_live_mit_tmp`-
    Aufrufer), daher ist lexikografische Sortierung == chronologisch.
    Best-Effort: Fehler beim Loeschen werden geloggt, nicht propagiert
    — der Retrain selbst war erfolgreich.
    """
    if behalten < 1 or not archiv_basis.is_dir():
        return
    versionen = sorted(
        (p for p in archiv_basis.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    )
    for alt in versionen[:-behalten]:
        try:
            shutil.rmtree(alt)
        except OSError:
            logger.exception("ml.archiv_cleanup_fehler", ordner=str(alt))


def _swap_live_mit_tmp(
    live: Path, tmp: Path, archiv: Path, archiv_behalten: int = 0,
) -> None:
    """Alte Modelle nach `archiv`, tmp nach `live`.

    Auf derselben Partition sind die Operationen sehr schnell (inode-Move).
    `live` danach eine leere Umbenennung von `tmp`.

    T-0219: Ist `archiv_behalten` > 0, werden nach dem Move alte
    Archiv-Versionen aufgeraeumt — pro `_archiv`-Ordner bleiben nur die
    juengsten N `<datum>`-Unterordner.
    """
    archiv.parent.mkdir(parents=True, exist_ok=True)
    if live.exists():
        live.rename(archiv)
    tmp.rename(live)
    if archiv_behalten > 0:
        _raeume_alte_archive(archiv.parent, archiv_behalten)
