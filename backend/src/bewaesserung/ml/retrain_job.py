"""T-0048: In-Service ML-Retrain mit Deployment-Gate.

Ablauf des Jobs (Muster wie BackupJob/WetterArchivJob):
  1. Intervall-Gate (Default 7 Tage).
  2. Feature-Extraktion ueber das konfigurierte Zeitfenster.
  3. Training im Temporaer-Verzeichnis (`<ausgabe>_tmp/`).
  4. Vergleich: fuer JEDEN Horizont muss `mae_neu < gate_faktor * mae_alt`
     gelten, sonst Rollback ohne Deploy. T-0605: `mae_alt`/`mae_neu` sind
     Amtsinhaber und Kandidat auf DENSELBEN CV-Testzeilen (nur Zeilen nach
     dem Training des Amtsinhabers + Gap, siehe `gate_vergleich`); nur wenn
     das nicht geht, die eingefrorene Trainings-MAE wie vorher.
     T-0610 ML-7: hat der Amtsinhaber eine aeltere `feature_schema_version`,
     laeuft immer der faire Vergleich; ist er unmoeglich, bleibt der Amtsinhaber.
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
import sys
import tempfile
from collections.abc import Collection
from datetime import datetime, timedelta
from pathlib import Path

import structlog

from bewaesserung.ml.feuchte_retention import (
    Schutzmenge,
    baue_schutzmenge,
    lies_live_ziele,
    waehle_loeschbar,
)
from bewaesserung.ml.gate_vergleich import (
    punkt_ergebnisse,
    vergleiche_auf_gleichen_testzeilen,
)
from bewaesserung.ml.modelle_ml import FEATURE_SCHEMA_VERSION
from bewaesserung.modelle import GesamtKonfig, MlRetrainKonfig
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

# T-0610 ML-7: `gate_modus`, wenn der Amtsinhaber ein aelteres Feature-Schema
# hat UND der faire Vergleich nicht moeglich war (siehe `_gate_mae`): kein
# Ersatz. Steht im Lauf-Ergebnis und in /api/ml/status.
GATE_MODUS_SCHEMA_VERALTET = "schema_veraltet"

# T-0403: Obergrenze fuer den Feature-Subprozess. Gemessen wurden ~945 s
# (mit konkurrierendem Mess-Ticker) bzw. ~700 s im Produktivlauf; 40 min
# lassen reichlich Luft und verhindern trotzdem, dass ein haengender
# Subprozess den Retrain dauerhaft blockiert.
SUBPROZESS_TIMEOUT_S = 2400.0


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
        # T-0564: Inferenz-Service, damit ein Deploy auch im laufenden
        # Prozess wirkt. Wird von `main.py` nachgereicht (der Service
        # entsteht spaeter als dieser Job). Siehe `_service_nachladen`.
        self._vorhersage_service = None
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

    def setze_vorhersage_service(self, service) -> None:
        """T-0564: den laufenden `MLVorhersageService` einhaengen."""
        self._vorhersage_service = service

    def _service_nachladen(self, was: str) -> None:
        """Laedt die Booster im laufenden Prozess neu.

        **Ohne das rechnete nach einem Deploy weiter das ALTE Modell.**
        `lade_modelle()` ruft nur `main.py` beim Start; `_modell_version()`
        liest den Symlink dagegen bei jeder Inferenz frisch von Platte. Nach
        einem Retrain im laufenden Betrieb passten Booster und Versionsstring
        also nicht mehr zusammen -- das Drift-Log trug die neue Version zu
        Prognosen des alten Modells.

        Zweite Folge, unauffaelliger: `hole_entscheidungsgebundene_feuchte_
        versionen` speist die Retention. Die schuetzte damit Archiv-Dateien,
        die nie gerechnet haben, und liess die tatsaechlich benutzten
        ungeschuetzt (Kontext T-0478, dort gingen 879 Versionen verloren).

        Der Response-Pfad macht das seit jeher richtig
        (`response_retrain_job` ruft `lade_zone(..., force=True)`); dem
        Feuchte-Pfad fehlte das Gegenstueck.
        """
        service = self._vorhersage_service
        if service is None:
            return
        try:
            geladen = service.lade_modelle()
            logger.info(
                "ml.retrain.service_nachgeladen", was=was, erfolg=bool(geladen),
            )
        except Exception:
            # Ein Fehler beim Nachladen darf den Deploy nicht ruecknehmen --
            # die Dateien liegen richtig, nur der Prozess ist noch alt. Der
            # naechste Neustart heilt es.
            logger.exception("ml.retrain.service_nachladen_fehler", was=was)

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

    async def _trainingsdaten(self, extraktor, von: datetime, bis: datetime):
        """T-0403: Feature-Bau in einem eigenen Prozess, mit Rueckfallweg.

        Der Bau ist Zeile-fuer-Zeile-Python und haelt den GIL; hinter
        `asyncio.to_thread` blockierte er den Event-Loop trotzdem zu 82 % der
        Laufzeit (gemessen 13.08.2026, Details im T-0403-Block). Nur ein
        eigener Interpreter loest das.

        **Faellt der Subprozess aus, wird in-process gebaut.** Ein Retrain,
        der gar nicht laeuft, ist schlechter als einer, der das Dashboard
        einmal alle drei Tage ausbremst -- das Modell veraltet sonst
        unbemerkt. Der Rueckfall wird laut geloggt, damit er nicht zum
        stillen Dauerzustand wird.
        """
        if not self._retrain.feature_bau_subprozess:
            return await extraktor.erstelle_trainingsdaten(von=von, bis=bis)
        try:
            return await self._trainingsdaten_subprozess(von, bis)
        except Exception:
            logger.exception(
                "ml.retrain.subprozess_fehlgeschlagen",
                hinweis="Feature-Bau laeuft ersatzweise in-process -- der "
                        "Event-Loop blockiert dabei (T-0403).",
            )
            return await extraktor.erstelle_trainingsdaten(von=von, bis=bis)

    async def _trainingsdaten_subprozess(self, von: datetime, bis: datetime):
        import pandas as pd
        from bewaesserung.konfig import STANDARD_KONFIG_PFAD

        with tempfile.TemporaryDirectory(prefix="ml_features_") as tmp:
            ziel = Path(tmp) / "trainingsdaten.pkl"
            prozess = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "bewaesserung.ml.feature_prozess",
                "--von", von.isoformat(), "--bis", bis.isoformat(),
                "--ziel", str(ziel), "--konfig", str(STANDARD_KONFIG_PFAD),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                roh_out, roh_err = await asyncio.wait_for(
                    prozess.communicate(), timeout=SUBPROZESS_TIMEOUT_S,
                )
            except (TimeoutError, asyncio.TimeoutError):
                prozess.kill()
                await prozess.wait()
                raise RuntimeError(
                    f"Feature-Subprozess ueberschritt "
                    f"{SUBPROZESS_TIMEOUT_S:.0f} s",
                ) from None
            if prozess.returncode != 0:
                raise RuntimeError(
                    f"Feature-Subprozess Exit {prozess.returncode}: "
                    f"{roh_err.decode(errors='replace')[-500:]}",
                )
            if not ziel.exists():
                raise RuntimeError(
                    "Feature-Subprozess lieferte keine Datei: "
                    f"{roh_out.decode(errors='replace')[-200:]}",
                )
            # Lesen ist ein einzelner C-Aufruf und gibt den GIL frei; der
            # DataFrame selbst ist wenige MB.
            df = await asyncio.to_thread(pd.read_pickle, ziel)
        logger.info("ml.retrain.features_aus_subprozess", zeilen=len(df))
        return df

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
        df = await self._trainingsdaten(extraktor, von, jetzt)
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

        # T-0478: Schutzmenge EINMAL pro Lauf aus der DB holen, nicht pro
        # Archiv-Ordner. `_raeume_alte_archive` laeuft synchron im
        # Deploy-Pfad; ein DB-Zugriff dort waere teure Vorarbeit hinter
        # dem Gate (T-0458) und in einer sync-Funktion ohnehin falsch.
        schutz = await self._lade_schutzmenge()

        # T-0082: Pro-Cluster-Iteration. Bei `cluster_strategie=global`
        # (Default-Backward-Compat) wird der alte Pfad ausgefuehrt.
        if self._retrain.cluster_strategie == "pro_zone":
            return await self._fuehre_aus_pro_cluster(jetzt, df, schutz)

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
            trainiert = await asyncio.to_thread(pipeline.trainiere_quantile, df)
        else:
            trainiert = await asyncio.to_thread(pipeline.trainiere, df)

        alte_mae, neue_mae, gate_info = await asyncio.to_thread(
            self._gate_mae, self._ausgabe, tmp_pfad, trainiert, pipeline,
        )

        gate_details = _pruefe_gate(
            alte_mae, neue_mae, self._retrain.gate_faktor,
            alte_modelle=_modell_horizonte(self._ausgabe),
            schema_vergleich_unmoeglich=(
                gate_info.get("gate_modus") == GATE_MODUS_SCHEMA_VERALTET
            ),
        )

        if not gate_details["passed"]:
            logger.warning(
                "ml.retrain.abgelehnt",
                mae_alt=alte_mae, mae_neu=neue_mae,
                gate_faktor=self._retrain.gate_faktor,
                fehlschlaege=gate_details["fehlschlaege"],
                **gate_info,
            )
            shutil.rmtree(tmp_pfad, ignore_errors=True)
            return {
                "status": "abgelehnt",
                "mae_alt": alte_mae, "mae_neu": neue_mae,
                "gate_faktor": self._retrain.gate_faktor,
                "fehlschlaege": gate_details["fehlschlaege"],
                **gate_info,
                "zeit": jetzt.isoformat(),
            }

        archiv_pfad = (
            self._ausgabe.parent
            / f"{self._ausgabe.name}{self._retrain.archiv_suffix}"
            / jetzt.strftime("%Y-%m-%d_%H%M%S")
        )
        # Legacy-Wurzelverzeichnis: dort greifen nur die prefixlosen
        # Versionsstrings (`fuer(None)`), siehe feuchte_retention.
        _swap_live_mit_tmp(
            self._ausgabe, tmp_pfad, archiv_pfad,
            archiv_behalten=_ARCHIV_RETENTION,
            geschuetzte_dateien=None if schutz is None else schutz.fuer(None),
        )
        self._service_nachladen("legacy")

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
            **gate_info,
        )
        return {
            "status": "uebernommen",
            "mae_alt": alte_mae, "mae_neu": neue_mae,
            "archiv": str(archiv_pfad),
            **gate_info,
            "zeit": jetzt.isoformat(),
        }

    def _gate_mae(
        self, live_dir: Path, tmp_dir: Path, trainiert, pipeline,
    ) -> tuple[dict[int, float], dict[int, float], dict]:
        """T-0605: MAE-Paar fuer `_pruefe_gate` plus Protokoll-Felder.

        Bevorzugt den fairen Vergleich: Amtsinhaber und Kandidat auf denselben
        CV-Testzeilen (`gate_vergleich`). Geht das nicht (Erstlauf, Metrik
        fehlt, Kandidat ohne CV-Zeilen, zu wenige Zeilen nach dem Training des
        Amtsinhabers, Schalter `gate_gleiches_fenster` aus, Fehler), bleibt es
        beim bisherigen eingefrorenen Vergleich -- der Grund steht in
        `gate_grund`. Die Erstlauf- und T-0399-Pfade von `_pruefe_gate`
        greifen dann unveraendert, weil sie dieselben eingefrorenen Werte
        sehen wie vorher.

        Synchron (laedt Booster und rechnet Vorhersagen); der Aufrufer legt
        es per `asyncio.to_thread` neben den Event-Loop.
        """
        alte_mae = _lade_aktuelle_mae(live_dir) if live_dir.exists() else {}
        neue_mae = _lade_aktuelle_mae(tmp_dir)
        # T-0610 ML-7 (Entscheid Andre 14.10.): ein Amtsinhaber auf aelterem
        # Feature-Schema wird NICHT blind ersetzt. Seine eingefrorene MAE
        # stammt aus dem alten Feature-Bau und taugt nicht als Massstab;
        # deshalb laeuft fuer ihn immer der faire Vergleich auf den neu
        # gebauten CV-Testzeilen (auch bei abgeschaltetem
        # `gate_gleiches_fenster`). Dass sich die Semantik einzelner Features
        # geaendert hat, ist genau der Preis, den dieser Vergleich misst.
        # Geht der Vergleich nicht (Spalten fehlen, zu wenige Zeilen, Fehler),
        # bleibt der Amtsinhaber (`_pruefe_gate`, `schema_vergleich_unmoeglich`).
        alte_version = (
            _schema_version_amtsinhaber(live_dir) if live_dir.exists() else None
        )
        schema_veraltet = (
            alte_version is not None and alte_version < FEATURE_SCHEMA_VERSION
        )
        schema_info = (
            {"schema_alt": alte_version, "schema_neu": FEATURE_SCHEMA_VERSION}
            if schema_veraltet else {}
        )

        def _unmoeglich(grund, **extra) -> tuple[dict, dict, dict]:
            logger.warning(
                "ml.retrain.schema_vergleich_unmoeglich",
                verzeichnis=str(live_dir), grund=grund, **schema_info, **extra,
            )
            return alte_mae, neue_mae, {
                "gate_modus": GATE_MODUS_SCHEMA_VERALTET,
                "gate_grund": grund, **schema_info, **extra,
            }

        info: dict = {"gate_modus": "eingefroren"}
        if not self._retrain.gate_gleiches_fenster and not schema_veraltet:
            info["gate_grund"] = "abgeschaltet"
            return alte_mae, neue_mae, info
        if not alte_mae:
            info["gate_grund"] = "keine_alte_metrik"
            return alte_mae, neue_mae, info
        try:
            vergleich = vergleiche_auf_gleichen_testzeilen(
                live_dir,
                punkt_ergebnisse(trainiert),
                gap_stunden=int(getattr(pipeline, "gap_stunden", 24)),
                mindest_zeilen=self._retrain.gate_vergleich_mindest_zeilen,
                spalten_pflicht=schema_veraltet,
            )
        except Exception as exc:
            logger.exception("ml.retrain.gate_vergleich_fehler")
            if schema_veraltet:
                return _unmoeglich(f"fehler:{type(exc).__name__}")
            info["gate_grund"] = f"fehler:{type(exc).__name__}"
            return alte_mae, neue_mae, info
        if not vergleich.get("moeglich"):
            extra = {
                k: vergleich[k]
                for k in ("horizont_h", "n", "spalten") if k in vergleich
            }
            if schema_veraltet:
                return _unmoeglich(vergleich.get("grund"), **extra)
            info["gate_grund"] = vergleich.get("grund")
            if "horizont_h" in vergleich:
                info["gate_grund_horizont_h"] = vergleich["horizont_h"]
            if "n" in vergleich:
                info["gate_vergleich_n"] = vergleich["n"]
            return alte_mae, neue_mae, info
        info = {
            "gate_modus": "gleiches_fenster",
            "gate_vergleich_n": vergleich["n"],
            "gate_vergleich_ab": vergleich["ab"],
            # Nur zur Einordnung: die alten Gate-Zahlen, die NICHT mehr
            # entscheiden.
            "mae_alt_eingefroren": alte_mae,
            "mae_neu_cv": neue_mae,
            **schema_info,
        }
        return vergleich["mae_alt"], vergleich["mae_neu"], info


    async def _lade_schutzmenge(self) -> Schutzmenge | None:
        """T-0478: entscheidungsgebundene Schutzmenge fuer die Archive.

        Rueckgabe `None`, wenn die DB nicht gelesen werden konnte. Der
        Deploy laeuft dann trotzdem, aber die Archive werden NICHT
        aufgeraeumt -- ein leeres Ergebnis darf nie als "nichts ist
        referenziert" durchgehen (dieser Kurzschluss ist die Fehlerklasse
        aus T-0477/T-0478).
        """
        try:
            referenzen = (
                await self._speicher.hole_entscheidungsgebundene_feuchte_versionen()
            )
        except Exception:
            logger.exception("ml.retrain.schutzmenge_fehler")
            return None
        schutz = baue_schutzmenge(referenzen)
        logger.info(
            "ml.retrain.schutzmenge",
            versionen=schutz.anzahl_versionen,
            verzeichnisse=len(schutz.pro_verzeichnis),
            praefixlos=len(schutz.ueberall),
        )
        return schutz

    async def _fuehre_aus_pro_cluster(
        self, jetzt: datetime, df, schutz: Schutzmenge | None = None,
    ) -> dict:
        """T-0082: pro Cluster eine Trainings-Pipeline. Iteriert ueber
        alle distinkten `cluster_id`s aus der Konfig (default = zone_id),
        prueft Mindest-Zeilenzahl, trainiert + gate-prueft + deployed
        pro Cluster atomar.

        T-0478: `schutz` ist die einmal pro Lauf berechnete Schutzmenge
        fuer die Archiv-Retention; `None` unterdrueckt das Aufraeumen.
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
                    trainiert = await asyncio.to_thread(
                        pipeline.trainiere_quantile, zonen_df,
                    )
                else:
                    trainiert = await asyncio.to_thread(
                        pipeline.trainiere, zonen_df,
                    )
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

            # T-0605: Amtsinhaber auf denselben CV-Testzeilen bewerten.
            alte_mae, neue_mae, gate_info = await asyncio.to_thread(
                self._gate_mae, cluster_dir, tmp_dir, trainiert, pipeline,
            )
            alte_modelle = (
                _modell_horizonte(cluster_dir) if cluster_dir.exists() else set()
            )
            gate_faktor = self._retrain.gate_faktor_pro_cluster.get(
                cluster_id, self._retrain.gate_faktor,
            )
            gate_details = _pruefe_gate(
                alte_mae, neue_mae, gate_faktor, alte_modelle=alte_modelle,
                schema_vergleich_unmoeglich=(
                    gate_info.get("gate_modus") == GATE_MODUS_SCHEMA_VERALTET
                ),
            )

            if not gate_details["passed"]:
                logger.warning(
                    "ml.retrain.cluster.abgelehnt cluster=%s "
                    "mae_alt=%s mae_neu=%s gate=%.2f fehlschlaege=%s "
                    "gate_info=%s",
                    cluster_id, alte_mae, neue_mae,
                    gate_faktor, gate_details["fehlschlaege"], gate_info,
                )
                shutil.rmtree(tmp_dir, ignore_errors=True)
                ergebnisse[cluster_id] = {
                    "status": "abgelehnt",
                    "mae_alt": alte_mae,
                    "mae_neu": neue_mae,
                    "gate_faktor": gate_faktor,
                    "fehlschlaege": gate_details["fehlschlaege"],
                    **gate_info,
                    "n_zeilen": n,
                }
                continue

            # Atomic-Swap pro Cluster.
            archiv_pfad = (
                feuchte_basis
                / f"{cluster_id}{self._retrain.archiv_suffix}"
                / jetzt.strftime("%Y-%m-%d_%H%M%S")
            )
            # T-0478: Schluessel ist die `cluster_id` -- so heisst das
            # Verzeichnis unter `ml/feuchte/`, und genau dieser Name steht
            # als Praefix in `ml_vorhersage_log.modell_version`.
            _swap_live_mit_tmp(
                cluster_dir, tmp_dir, archiv_pfad,
                archiv_behalten=_ARCHIV_RETENTION,
                geschuetzte_dateien=(
                    None if schutz is None else schutz.fuer(cluster_id)
                ),
            )
            self._service_nachladen(cluster_id)
            quoten = {
                h: round(neue_mae[h] / alte_mae[h], 3)
                for h in (6, 12, 24)
                if h in alte_mae and h in neue_mae and alte_mae[h] > 0
            }
            logger.info(
                "ml.retrain.cluster.uebernommen cluster=%s "
                "mae_alt=%s mae_neu=%s quoten=%s gate=%.2f archiv=%s "
                "gate_info=%s",
                cluster_id, alte_mae, neue_mae, quoten,
                gate_faktor, archiv_pfad, gate_info,
            )
            ergebnisse[cluster_id] = {
                "status": "uebernommen",
                "mae_alt": alte_mae,
                "mae_neu": neue_mae,
                "gate_faktor": gate_faktor,
                "quoten_neu_zu_alt": quoten,
                "archiv": str(archiv_pfad),
                **gate_info,
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
    return {
        h: float(daten["mae"])
        for h, daten in _lade_aktuelle_metriken(verzeichnis).items()
    }


def _schema_version_amtsinhaber(verzeichnis: Path) -> int | None:
    """T-0610 ML-7: kleinste `feature_schema_version` der Live-Modelle.

    Fehlt das Feld (Modelle vor T-0076b), gilt 1 wie beim Laden in
    `vorhersage.py`. None, wenn keine Metrik lesbar ist -- diesen Fall
    entscheidet weiter T-0399 (`metrik_fehlt`), nicht diese Pruefung.
    """
    versionen = [
        int(daten.get("feature_schema_version", 1))
        for daten in _lade_aktuelle_metriken(verzeichnis).values()
    ]
    return min(versionen) if versionen else None


def _lade_aktuelle_metriken(verzeichnis: Path) -> dict[int, dict]:
    """Metriken-JSON je Horizont neben den aktuell_Xh.lgbm-Symlinks.

    Pro Horizont die erste Kandidatendatei, die sich lesen laesst und eine
    MAE als Zahl traegt (Punkt- vor q50-Metrik). Gemeinsame Quelle fuer
    `_lade_aktuelle_mae` und `_schema_version_amtsinhaber`.
    """
    out: dict[int, dict] = {}
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
                float(daten["mae"])
            except (OSError, ValueError, KeyError):
                continue
            out[h] = daten
            break
    return out


def _pruefe_gate(
    alte_mae: dict[int, float],
    neue_mae: dict[int, float],
    gate_faktor: float,
    alte_modelle: set[int] | None = None,
    schema_vergleich_unmoeglich: bool = False,
) -> dict:
    """Pro Horizont: mae_neu < gate_faktor * mae_alt. Alle 3 muessen bestehen.

    `alte_modelle` (T-0399): Horizonte mit vorhandenem Modell-Symlink, aus
    `_modell_horizonte()`. Ohne dieses Argument ist "Modelle da, Metriken
    unlesbar" von "Erstlauf" nicht unterscheidbar -- Default `None` erhaelt das
    alte Verhalten fuer Aufrufer, die keine Modelle haben koennen (Tests).

    `schema_vergleich_unmoeglich` (T-0610 ML-7): der Amtsinhaber stammt aus
    einem aelteren Feature-Schema und der faire Vergleich auf den neuen
    Testzeilen war nicht moeglich. Seine eingefrorene MAE ist kein Massstab,
    also kein Urteil: abgelehnt, der Amtsinhaber bleibt (sichere Richtung).
    """
    fehlschlaege: list[dict] = []
    if schema_vergleich_unmoeglich:
        return {
            "passed": False,
            "fehlschlaege": [{"grund": "schema_vergleich_unmoeglich"}],
        }
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


def _raeume_alte_archive(
    archiv_basis: Path,
    behalten: int,
    geschuetzte_dateien: Collection[str] | None,
) -> None:
    """T-0219/T-0478: Loescht in `archiv_basis` die Datums-Unterordner, die
    weder unter die juengsten `behalten` fallen noch eine referenzierte
    Modelldatei enthalten.

    `geschuetzte_dateien` sind die Dateinamen, die fuer GENAU DIESES
    Verzeichnis geschuetzt sind (T-0478, siehe `ml/feuchte_retention.py`).
    Ein leeres Set heisst "nichts referenziert" und loescht wie vor
    T-0478; `None` heisst "unbekannt" -- dann wird NICHT aufgeraeumt.
    Der Unterschied ist der ganze Punkt: die Schutzmenge kommt aus der DB,
    und ein DB-Fehler darf nicht als "nichts ist referenziert" durchgehen.
    Ein zu grosses Archiv ist reparabel, ein geloeschtes Modell nicht.

    Die Funktion ist synchron und best-effort: der DB-Zugriff gehoert VOR
    den Aufruf (einmal pro Retrain-Lauf, nicht pro Ordner -- Muster
    "teure Vorarbeit vor dem Gate", T-0458). Fehler beim Loeschen werden
    geloggt, nicht propagiert -- der Retrain selbst war erfolgreich.
    """
    if behalten < 1 or not archiv_basis.is_dir():
        return
    if geschuetzte_dateien is None:
        logger.warning(
            "ml.archiv_cleanup_uebersprungen",
            ordner=str(archiv_basis),
            grund="schutzmenge_unbekannt",
        )
        return
    for alt in waehle_loeschbar(archiv_basis, behalten, geschuetzte_dateien):
        try:
            shutil.rmtree(alt)
        except OSError:
            logger.exception("ml.archiv_cleanup_fehler", ordner=str(alt))


def _swap_live_mit_tmp(
    live: Path,
    tmp: Path,
    archiv: Path,
    archiv_behalten: int = 0,
    geschuetzte_dateien: Collection[str] | None = None,
) -> None:
    """Alte Modelle nach `archiv`, tmp nach `live`.

    Auf derselben Partition sind die Operationen sehr schnell (inode-Move).
    `live` danach eine leere Umbenennung von `tmp`.

    T-0219: Ist `archiv_behalten` > 0, werden nach dem Move alte
    Archiv-Versionen aufgeraeumt — pro `_archiv`-Ordner bleiben nur die
    juengsten N `<datum>`-Unterordner.

    T-0478: Zusaetzlich bleiben Ordner erhalten, die eine in
    `geschuetzte_dateien` gelistete Modelldatei enthalten. `None` heisst
    "Schutzmenge unbekannt" und unterdrueckt das Aufraeumen ganz.
    Die `aktuell_*`-Symlinkziele werden hier aufgeloest, weil sie NACH
    dem Rename nicht mehr unter `live` stehen.
    """
    live_ziele = lies_live_ziele(live)
    archiv.parent.mkdir(parents=True, exist_ok=True)
    # T-0564: Rollback fuer den zweiten Rename. Der Docstring des Jobs
    # verspricht "atomarer Deploy"; zwei `rename` hintereinander sind das
    # nicht. Schlaegt der zweite fehl, ist das Live-Verzeichnis WEG -- und
    # der `finally`-Block des Jobs loescht danach `<ausgabe>_tmp` mit den
    # neuen Modellen gleich mit. Uebrig bleibt nur das Archiv.
    #
    # Die Folgerunde ist die eigentliche Gefahr: `_modell_horizonte()` und
    # `_lade_aktuelle_mae()` finden nichts, der Job nimmt den Erstlauf-Pfad
    # und deployt JEDEN Kandidaten ohne MAE-Vergleich. Das ist genau die
    # Luecke, die T-0399 geschlossen hat, hier ueber einen anderen Eingang.
    #
    # Ausgeloest per erzwungenem EXDEV reproduziert. Real liegen live, tmp
    # und archiv unter demselben `backend/daten/ml/`, ein
    # Cross-Device-Fehler ist dort unwahrscheinlich -- ENOSPC und EACCES
    # bleiben moeglich, und der Schaden waere derselbe.
    live_verschoben = False
    if live.exists():
        live.rename(archiv)
        live_verschoben = True
    try:
        tmp.rename(live)
    except OSError:
        if live_verschoben and not live.exists():
            archiv.rename(live)
            logger.error(
                "ml.retrain.deploy_zurueckgerollt",
                live=str(live),
                hinweis="tmp->live fehlgeschlagen, Altstand wiederhergestellt",
            )
        raise
    if archiv_behalten > 0:
        geschuetzt = (
            None
            if geschuetzte_dateien is None
            else set(geschuetzte_dateien) | set(live_ziele)
        )
        _raeume_alte_archive(archiv.parent, archiv_behalten, geschuetzt)
