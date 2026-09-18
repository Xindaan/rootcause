"""T-0065: MlResponseRetrainJob — zyklischer Retrain der Response-Modelle.

Pro Zone triggert der Job entweder zeit- oder event-getrieben und traininert
Forward- + Inverse-Modell via `ResponseTrainingsPipeline.lauf()`. Nach
erfolgreichem Training wird gegen das aktuell deployte Modell verglichen
(Gate: `mae_neu_inverse <= gate_mae_faktor * mae_alt_inverse`); besteht der
Check, werden die Symlinks geswappt (das macht die Pipeline bereits inline
durch `_aktualisiere_symlinks` — dieser Job validiert nur und rollt bei
Gate-Rejection zurueck).

Das Modul nutzt stdlib `logging` — Regression-Tests
`caplog.set_level(INFO)` setzen (fehlerpattern_stdlib_structlog_mix.md).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from bewaesserung.aquabloom_job import (
    _ist_konfiguriert as _ist_aquabloom_konfiguriert,
)
from bewaesserung.modelle import GesamtKonfig, MlBewaesserungsResponseKonfig
from bewaesserung.speicher import Speicher

logger = logging.getLogger(__name__)

# T-0539: Obergrenze fuer den Feature-Subprozess. Der Bau ueber 365 Tage
# dauert gemessen 70-130 s; 900 s lassen reichlich Luft fuer eine wachsende
# Datenbasis und begrenzen trotzdem den Fall "Subprozess haengt". Ohne
# Schranke haengt mit ihm der ganze Wartungsloop.
SUBPROZESS_TIMEOUT_S = 900.0

# T-0458: Wie oft der Events-Trigger hoechstens geprueft werden darf.
#
# Historie: bis 01.08.2026 war die Pruefung nur ueber `_baue_trainingsdaten`
# zu haben (365-Tage-Scan, gemessen 69 s und mit der DB wachsend), weil sie
# die Zeilenzahl pro Zone braucht. Dieser Scan lief in JEDEM 5-Minuten-Zyklus
# des Entscheidungsloops -- 288x taeglich, um eine Frage zu beantworten, deren
# Antwort sich an einem Intervall in TAGEN bemisst. T-0458 hat ihn auf dieses
# Intervall gedrosselt, T-0473 ihn danach ganz ersetzt: die Zahl kommt jetzt
# aus `zaehle_response_event_kandidaten` (GROUP-BY-Zaehlung, gemessen 14 ms
# ueber 365 Tage), der Feature-Aufbau laeuft nur noch, wenn wirklich eine Zone
# retrainiert wird.
#
# Die Drossel bleibt trotzdem stehen -- ihre Begruendung hat sich nur
# verschoben. Sie kostet nichts mehr, sie deckelt aber weiterhin, wie oft ein
# event-getriebener Retrain ueberhaupt anspringen kann. Das ist relevant, weil
# der Zaehler eine OBERE Schranke ist (s. `zaehle_response_event_kandidaten`):
# er feuert eher zu frueh als zu spaet. Der Events-Trigger existiert, um auf
# einen Schwung neuer Daten frueher zu reagieren als das Zeit-Intervall
# (14 Tage in der Default-Konfig) -- ob das 1 h oder 6 h nach dem letzten
# Ereignis geschieht, ist dafuer bedeutungslos.
PRUEF_INTERVALL_EVENTS = timedelta(hours=6)


class MlResponseRetrainJob:
    """Pro Zone retrainen, Gate pruefen, Symlinks pflegen."""

    def __init__(
        self,
        speicher: Speicher,
        konfig: GesamtKonfig,
        response_konfig: MlBewaesserungsResponseKonfig,
        basis_verzeichnis: str | Path = "",
    ):
        self._speicher = speicher
        self._konfig = konfig
        self._resp = response_konfig
        if not basis_verzeichnis and not response_konfig.ausgabe_pfad:
            from bewaesserung.konfig import ML_DATEN_PFAD
            basis_verzeichnis = str(Path(ML_DATEN_PFAD) / "response")
        self._basis = Path(basis_verzeichnis or response_konfig.ausgabe_pfad)
        self._basis.mkdir(parents=True, exist_ok=True)
        self._intervall = timedelta(days=response_konfig.retrain_intervall_tage)
        # Pro Zone den letzten Lauf + Eventzaehler merken.
        self._letzter_lauf: dict[str, datetime] = {}
        self._letzter_event_zaehler: dict[str, int] = {}
        self._letztes_ergebnis: dict[str, dict] = {}
        # T-0108: Job-globale Fehler-/Erfolg-Sichtbarkeit fuer
        # /api/ml/status. `_letzter_fehler` deckt sowohl den globalen
        # Daten-Fehler (`_baue_trainingsdaten`) als auch einen
        # Zone-Retrain-Crash ab.
        self._letzter_erfolg: datetime | None = None
        self._letzter_fehler: dict | None = None
        # T-0458: wann zuletzt die (teuren) Trainingsdaten gebaut wurden, um
        # den Events-Trigger zu pruefen. None = noch nie, dann sofort.
        self._letzte_events_pruefung: datetime | None = None
        # T-0483: hat der letzte teure Feature-Aufbau nichts Verwertbares
        # geliefert (leer ODER Fehler)? Bewusst getrennt von `_letzter_lauf`,
        # damit "letzter Lauf" weiter "hat trainiert" heisst und nicht "hat es
        # versucht" -- daran haengen Zeit- UND Events-Trigger.
        self._letzter_scan_leer: bool = False

    @property
    def letztes_ergebnis(self) -> dict[str, dict]:
        return self._letztes_ergebnis

    @property
    def letzter_erfolg(self) -> datetime | None:
        """Zeitpunkt des letzten fehlerfreien Laufs (T-0108)."""
        return self._letzter_erfolg

    @property
    def letzter_fehler(self) -> dict | None:
        """`{zeit, typ, nachricht}` des letzten Crashs, sonst None (T-0108)."""
        return self._letzter_fehler

    # --- Trigger ---

    def _ist_faellig(self, zone_id: str, events_gesamt: int, jetzt: datetime) -> str | None:
        """Rueckgabe: Trigger-Grund ('zeit' | 'events' | None)."""
        letzter = self._letzter_lauf.get(zone_id)
        if letzter is None:
            return "initial"
        if jetzt - letzter >= self._intervall:
            return "zeit"
        seit = events_gesamt - self._letzter_event_zaehler.get(zone_id, events_gesamt)
        if seit >= self._resp.retrain_event_schwelle:
            return "events"
        return None

    def _koennte_faellig_sein(self, jetzt: datetime) -> bool:
        """T-0458: billiges Vorab-Gate, ohne die Trainingsdaten zu bauen.

        Von den drei Triggern in `_ist_faellig` brauchen zwei die Daten
        NICHT: `initial` (Zone noch nie gelaufen) und `zeit` (Intervall um).
        Nur `events` braucht die Zeilenzahl pro Zone -- der wird deshalb
        gedrosselt geprueft (`PRUEF_INTERVALL_EVENTS`).

        Bewusst konservativ: im Zweifel True. Ein False hier darf nie einen
        faelligen Retrain verschlucken, es darf ihn nur verzoegern.
        """
        # T-0483: Hat eine Zone nie trainiert, liefert `_ist_faellig` den
        # Trigger "initial" -- und der umgeht die Drossel unten. Lieferte der
        # teure Feature-Aufbau aber nichts Verwertbares, bleibt `_letzter_lauf`
        # ungesetzt, "initial" gilt weiter, und der Aufbau lief in JEDEM
        # 5-Minuten-Zyklus erneut. Also wieder die Event-Loop-Blockade, gegen
        # die T-0458/T-0473 gebaut wurden, nur unter anderer Vorbedingung.
        #
        # Die Sperre greift ausschliesslich nach einem erfolglosen Aufbau.
        # Sobald verwertbare Zeilen da sind, gelten die Trigger unveraendert.
        if self._letzter_scan_leer and self._letzte_events_pruefung is not None:
            if jetzt - self._letzte_events_pruefung < PRUEF_INTERVALL_EVENTS:
                return False
        for zone in self._konfig.zonen:
            letzter = self._letzter_lauf.get(zone.zone_id)
            if letzter is None:
                return True                              # Trigger "initial"
            if jetzt - letzter >= self._intervall:
                return True                              # Trigger "zeit"
        if self._letzte_events_pruefung is None:
            return True
        return jetzt - self._letzte_events_pruefung >= PRUEF_INTERVALL_EVENTS

    # --- Haupt-Zyklus ---

    async def aktualisiere_wenn_faellig(self, jetzt: datetime | None = None) -> bool:
        """Laeuft ueber alle Zonen, retrainiert Faellige. True wenn mind. einer lief."""
        if not self._resp.aktiv:
            return False
        jetzt = jetzt or datetime.now()

        # T-0458: erst fragen, ob ueberhaupt etwas zu tun ist. Vorher lief der
        # 365-Tage-Scan unbedingt -- auch wenn keine Zone faellig war.
        if not self._koennte_faellig_sein(jetzt):
            return False

        # T-0473: Der Events-Trigger braucht nur eine Zahl pro Zone, keinen
        # Feature-DataFrame. Die Zaehlabfrage kostet Millisekunden statt ~59 s.
        df = None
        zaehler = await self._hole_event_zaehler(jetzt)
        # T-0458: Zeitstempel setzen, BEVOR ueber "keine Zone faellig"
        # ausgestiegen wird. Sonst prueft der naechste 5-Minuten-Zyklus
        # dieselbe Frage erneut, und die Drossel haette nichts gebracht.
        self._letzte_events_pruefung = jetzt
        if zaehler is None:
            # Zaehlung nicht verfuegbar (kein Speicher oder DB-Fehler) --
            # zurueck auf den Alt-Pfad ueber die Trainingsdaten. Teuer, aber
            # der Events-Trigger darf nicht still ausfallen; ein fehlender
            # Zaehler wuerde ihn sonst dauerhaft auf 0 einfrieren.
            df = await self._baue_trainingsdaten_sicher(jetzt)
            if df is None or df.empty:
                # T-0483: erfolgloser Aufbau (leer ODER Exception, die
                # `_baue_trainingsdaten_sicher` zu None macht) -- merken,
                # damit "initial" die Drossel nicht umgeht.
                self._letzter_scan_leer = True
                return False
            zaehler = {
                zone.zone_id: int((df["zone_id"] == zone.zone_id).sum())
                for zone in self._konfig.zonen
            }

        trigger_pro_zone: dict[str, str] = {}
        for zone in self._konfig.zonen:
            trigger = self._ist_faellig(
                zone.zone_id, zaehler.get(zone.zone_id, 0), jetzt,
            )
            if trigger is not None:
                trigger_pro_zone[zone.zone_id] = trigger
        if not trigger_pro_zone:
            return False

        # Erst hier -- und nur hier -- lohnt der teure Feature-Aufbau.
        if df is None:
            df = await self._baue_trainingsdaten_sicher(jetzt)
        if df is None or df.empty:
            # T-0483: s. o. -- deckt auch den Fehlerfall ab, weil
            # `_baue_trainingsdaten_sicher` eine Exception zu None macht.
            self._letzter_scan_leer = True
            return False
        self._letzter_scan_leer = False

        irgendein_lauf = False
        fehler_zone: dict | None = None
        # T-0477: Die Referenzliste wird erst geholt, wenn wirklich eine Zone
        # laeuft -- und genau einmal pro Zyklus. `None` heisst "noch nicht
        # geholt", `_referenzen_unbekannt` heisst "geholt und fehlgeschlagen";
        # im zweiten Fall wird NICHT aufgeraeumt (leeres Ergebnis darf nie als
        # "nichts referenziert" durchgehen).
        referenzierte: dict[str, set[str]] | None = None
        referenzen_unbekannt = False
        for zone in self._konfig.zonen:
            trigger = trigger_pro_zone.get(zone.zone_id)
            if trigger is None:
                continue
            ergebnis = await self._retrain_zone(zone.zone_id, df, jetzt, trigger)
            if referenzierte is None and not referenzen_unbekannt:
                referenzierte = await self._hole_referenzierte_versionen()
                referenzen_unbekannt = referenzierte is None
            if referenzierte is not None:
                ergebnis["retention"] = await self._raeume_versionen_auf(
                    zone.zone_id, referenzierte.get(zone.zone_id, set()),
                )
            self._letztes_ergebnis[zone.zone_id] = ergebnis
            if ergebnis.get("status") == "fehler":
                fehler_zone = ergebnis
            self._letzter_lauf[zone.zone_id] = jetzt
            # T-0473: derselbe Zaehler, aus dem der Trigger gelesen hat.
            # Zwei verschiedene Quellen (Proxy jetzt, DF-Zeilen beim naechsten
            # Mal) wuerden eine Differenz erzeugen, die keine neuen Ereignisse
            # sind -- der Events-Trigger feuerte dann bei jeder Pruefung.
            self._letzter_event_zaehler[zone.zone_id] = zaehler.get(zone.zone_id, 0)
            irgendein_lauf = True
        # T-0108: Job-globalen Fehler-/Erfolg-Status nachziehen.
        if fehler_zone is not None:
            self._letzter_fehler = {
                "zeit": fehler_zone.get("zeit", jetzt.isoformat()),
                "typ": fehler_zone.get("fehler_typ", "Fehler"),
                "nachricht": (
                    f"Zone {fehler_zone.get('zone_id')}: "
                    f"{fehler_zone.get('fehler_nachricht', '')}"
                )[:300],
            }
        elif irgendein_lauf:
            self._letzter_erfolg = jetzt
            self._letzter_fehler = None
        return irgendein_lauf

    # --- Retention (T-0477) ---

    async def _hole_referenzierte_versionen(self) -> dict[str, set[str]] | None:
        """Modellversionen aus gespeicherten Entscheidungen, pro Zone.

        `None` bei jedem Fehler -- der Aufrufer raeumt dann nicht auf. Eine
        stille leere Menge waere hier der gefaehrlichste Fehler ueberhaupt:
        sie sieht aus wie "nichts ist referenziert" und wuerde genau die
        Versionen entfernen, die eine Entscheidung erklaeren.
        """
        if self._speicher is None:
            return None
        try:
            return await self._speicher.hole_referenzierte_response_modellversionen()
        except Exception:
            logger.exception("ml.response_retention.referenzen_fehler")
            return None

    async def _raeume_versionen_auf(
        self, zone_id: str, referenzierte: set[str],
    ) -> dict:
        """Retention fuer eine Zone, nach deren Retrain-Lauf."""
        from bewaesserung.ml.response_retention import raeume_zone_auf

        try:
            return await asyncio.to_thread(
                raeume_zone_auf,
                self._basis / zone_id,
                zone_id,
                referenzierte,
            )
        except Exception as exc:
            logger.exception("ml.response_retention.fehler zone=%s", zone_id)
            return {"zone_id": zone_id, "fehler": f"{type(exc).__name__}: {exc}"[:200]}

    async def _hole_event_zaehler(self, jetzt: datetime) -> dict[str, int] | None:
        """T-0473: Kandidaten-Events pro Zone zaehlen, ohne Features zu bauen.

        `None` heisst "nicht ermittelbar" (kein Speicher, DB-Fehler) -- der
        Aufrufer faellt dann auf den alten, teuren Weg zurueck. Ein stilles
        Null-Ergebnis waere hier der gefaehrliche Fehler: es sieht aus wie
        "keine neuen Ereignisse" und wuerde den Events-Trigger dauerhaft
        stilllegen, ohne dass irgendwo etwas fehlschlaegt.
        """
        if self._speicher is None:
            return None
        from bewaesserung.ml.response_features import (
            zaehle_response_event_kandidaten,
        )

        try:
            return await zaehle_response_event_kandidaten(
                self._speicher, self._konfig,
                von=jetzt - timedelta(days=365), bis=jetzt,
            )
        except Exception:
            logger.exception("ml.response_retrain.zaehler_fehler")
            return None

    async def _baue_trainingsdaten_sicher(self, jetzt: datetime):
        """`_baue_trainingsdaten` mit Fehlerablage; `None` statt Exception."""
        try:
            return await self._baue_trainingsdaten(jetzt)
        except Exception as exc:
            logger.exception("ml.response_retrain.daten_fehler")
            # T-0108: globaler Daten-Fehler war vorher still (nur Log).
            self._letzter_fehler = {
                "zeit": jetzt.isoformat(),
                "typ": type(exc).__name__,
                "nachricht": str(exc)[:300],
            }
            return None

    async def _baue_trainingsdaten(self, jetzt: datetime):
        """T-0539: Feature-Bau in einem eigenen Prozess, mit Rueckfallweg.

        Der Bau ist Zeile-fuer-Zeile-Python und haelt den GIL; hinter
        `asyncio.to_thread` blockierte er den Entscheidungsloop trotzdem zu
        ~100 % seiner Laufzeit (18 Fenster, 79-125 %, ohne Ausnahme -- Details
        in `docs/analyse/t0539_loop_stall/`). Nur ein eigener Interpreter
        loest das; derselbe Weg wie im Feuchte-Pfad (T-0403).

        **Faellt der Subprozess aus, wird in-process gebaut.** Ein Retrain,
        der gar nicht laeuft, ist schlechter als einer, der den Loop einmal
        ausbremst -- das Response-Modell veraltet sonst unbemerkt, und es
        entscheidet ueber Giessdauern. Der Rueckfall wird laut geloggt, damit
        er nicht zum stillen Dauerzustand wird.
        """
        von = jetzt - timedelta(days=365)
        if not self._resp.feature_bau_subprozess:
            return await self._trainingsdaten_in_process(von, jetzt)
        try:
            return await self._trainingsdaten_subprozess(von, jetzt)
        except Exception:
            logger.exception(
                "ml.response_retrain.subprozess_fehlgeschlagen -- Feature-Bau "
                "laeuft ersatzweise in-process, der Entscheidungsloop "
                "blockiert dabei (T-0539).",
            )
            return await self._trainingsdaten_in_process(von, jetzt)

    async def _trainingsdaten_in_process(self, von: datetime, bis: datetime):
        from bewaesserung.ml.response_features import erstelle_response_features

        return await erstelle_response_features(
            self._speicher, self._konfig, von=von, bis=bis,
        )

    async def _trainingsdaten_subprozess(self, von: datetime, bis: datetime):
        import pandas as pd
        from bewaesserung.konfig import STANDARD_KONFIG_PFAD

        with tempfile.TemporaryDirectory(prefix="ml_response_features_") as tmp:
            ziel = Path(tmp) / "response_features.pkl"
            prozess = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "bewaesserung.ml.response_feature_prozess",
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
                    f"Response-Feature-Subprozess ueberschritt "
                    f"{SUBPROZESS_TIMEOUT_S:.0f} s",
                ) from None
            if prozess.returncode != 0:
                raise RuntimeError(
                    f"Response-Feature-Subprozess Exit {prozess.returncode}: "
                    f"{roh_err.decode(errors='replace')[-500:]}",
                )
            if not ziel.exists():
                raise RuntimeError(
                    "Response-Feature-Subprozess lieferte keine Datei: "
                    f"{roh_out.decode(errors='replace')[-200:]}",
                )
            # Lesen ist ein einzelner C-Aufruf und gibt den GIL frei; der
            # DataFrame selbst ist wenige MB.
            df = await asyncio.to_thread(pd.read_pickle, ziel)
        logger.info(
            "ml.response_retrain.features_aus_subprozess zeilen=%d", len(df),
        )
        return df

    def _min_events_fuer(self, zone_id: str) -> int:
        """Mindestmenge Trainings-Rows — Pump-Zonen haben eine eigene.

        T-0482: AquaBloom-Zonen liefern seit dem F9-Fix Rows, aber mit
        schwaecherem Signal (kurzer, in der Dauer kaum variabler Puls;
        24h-Label durch den 12h-Takt oft entwertet). `min_events` von 3
        wuerde daraus ein Modell aus sechs Beobachtungen bauen.
        Greift eine Zone in beide Kategorien (Ventil UND Pump), gilt die
        strengere — ihr Trainingsbestand mischt dann beide Regimes.
        """
        zone = next(
            (z for z in self._konfig.zonen if z.zone_id == zone_id), None,
        )
        untergrenze = max(1, self._resp.min_events)
        if zone is not None and _ist_aquabloom_konfiguriert(zone):
            return max(untergrenze, self._resp.min_events_pump_zone)
        return untergrenze

    async def _retrain_zone(
        self, zone_id: str, df, jetzt: datetime, trigger: str,
    ) -> dict:
        from bewaesserung.ml.response_training import ResponseTrainingsPipeline
        from bewaesserung.ml.response_vorhersage import MLResponseService

        # Alten Zustand sichern — bei Gate-Rejection rollen wir zurueck.
        zone_dir = self._basis / zone_id
        alte_mae = _lade_inverse_mae(zone_dir, zone_id)

        pipeline = ResponseTrainingsPipeline(
            zone_id=zone_id,
            basis_verzeichnis=self._basis,
            min_events=self._min_events_fuer(zone_id),
        )

        # Snapshot der alten Symlinks + metadata (fuer Rollback).
        snapshot = _snapshot_aktuell(zone_dir, zone_id)

        try:
            erg = await asyncio.to_thread(pipeline.lauf, df, jetzt)
        except Exception as exc:
            logger.exception(
                "ml.response_retrain.fehler zone=%s", zone_id,
            )
            return {
                "status": "fehler", "zone_id": zone_id,
                "trigger": trigger,
                "fehler_typ": type(exc).__name__,
                "fehler_nachricht": str(exc)[:500],
                "zeit": jetzt.isoformat(),
            }

        if erg.status != "ok":
            return {
                "status": erg.status, "zone_id": zone_id, "trigger": trigger,
                "grund": erg.grund, "n_events": erg.n_events,
                "zeit": jetzt.isoformat(),
            }

        # Gate: nur pruefen, wenn es ein altes Modell gab.
        neue_mae = erg.inverse_mae_s
        if neue_mae is None:
            _rollback_aktuell(zone_dir, zone_id, snapshot)
            return {
                "status": "uebersprungen", "zone_id": zone_id,
                "trigger": trigger,
                "grund": "keine_inverse_mae",
                "n_events": erg.n_events,
                "zeit": jetzt.isoformat(),
            }
        if alte_mae is not None and neue_mae is not None:
            schwelle = self._resp.gate_mae_faktor * alte_mae
            if neue_mae > schwelle:
                # Rollback.
                _rollback_aktuell(zone_dir, zone_id, snapshot)
                logger.warning(
                    "ml.response_retrain.abgelehnt zone=%s mae_alt=%.2f "
                    "mae_neu=%.2f schwelle=%.2f",
                    zone_id, alte_mae, neue_mae, schwelle,
                )
                return {
                    "status": "abgelehnt", "zone_id": zone_id,
                    "trigger": trigger,
                    "mae_alt_inverse_s": round(alte_mae, 1),
                    "mae_neu_inverse_s": round(neue_mae, 1),
                    "gate_mae_faktor": self._resp.gate_mae_faktor,
                    "n_events": erg.n_events,
                    "zeit": jetzt.isoformat(),
                }

        # Service-Cache pingen, damit Live-Inferenz das neue Modell sieht.
        try:
            svc = MLResponseService.instanz(self._basis)
            svc.lade_zone(zone_id, force=True)
        except Exception:
            logger.exception("ml.response_retrain.service_reload_fehler zone=%s", zone_id)

        logger.info(
            "ml.response_retrain.uebernommen zone=%s version=%s "
            "mae_alt=%s mae_neu=%s n_events=%d trigger=%s",
            zone_id, erg.version, alte_mae, neue_mae, erg.n_events, trigger,
        )
        return {
            "status": "uebernommen", "zone_id": zone_id, "trigger": trigger,
            "mae_alt_inverse_s": round(alte_mae, 1) if alte_mae is not None else None,
            "mae_neu_inverse_s": round(neue_mae, 1) if neue_mae is not None else None,
            "forward_metriken": erg.forward_metriken,
            "version": erg.version, "n_events": erg.n_events,
            "zeit": jetzt.isoformat(),
        }


# --- Helper ---


def _lade_inverse_mae(zone_dir: Path, zone_id: str) -> float | None:
    """Liest `inverse_mae_s` aus der aktuellen Metadata-Datei."""
    meta_link = zone_dir / f"aktuell_{zone_id}_metadata.json"
    if not meta_link.exists():
        return None
    try:
        daten = json.loads(meta_link.read_text())
        mae = daten.get("inverse_mae_s")
        return float(mae) if mae is not None else None
    except (OSError, ValueError):
        return None


def _snapshot_aktuell(zone_dir: Path, zone_id: str) -> dict:
    """Merkt die aktuellen Symlink-Ziele fuer einen moeglichen Rollback."""
    snapshot: dict[str, str] = {}
    if not zone_dir.exists():
        return snapshot
    for kind in ("forward_q10", "forward_q50", "forward_q90", "inverse"):
        link = zone_dir / f"aktuell_{zone_id}_{kind}.lgbm"
        if link.is_symlink():
            try:
                snapshot[kind] = str(link.readlink())
            except OSError:
                pass
    meta_link = zone_dir / f"aktuell_{zone_id}_metadata.json"
    if meta_link.is_symlink():
        try:
            snapshot["metadata"] = str(meta_link.readlink())
        except OSError:
            pass
    return snapshot


def _rollback_aktuell(zone_dir: Path, zone_id: str, snapshot: dict) -> None:
    """Stellt alte Symlinks wieder her (nach Gate-Rejection)."""
    if not snapshot:
        return
    for kind in ("forward_q10", "forward_q50", "forward_q90", "inverse"):
        link = zone_dir / f"aktuell_{zone_id}_{kind}.lgbm"
        ziel = snapshot.get(kind)
        if ziel is None:
            continue
        try:
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(ziel)
        except OSError:
            logger.exception(
                "ml.response_retrain.rollback_fehler zone=%s kind=%s",
                zone_id, kind,
            )
    meta_ziel = snapshot.get("metadata")
    meta_link = zone_dir / f"aktuell_{zone_id}_metadata.json"
    if meta_ziel:
        try:
            if meta_link.exists() or meta_link.is_symlink():
                meta_link.unlink()
            meta_link.symlink_to(meta_ziel)
        except OSError:
            logger.exception(
                "ml.response_retrain.rollback_metadata_fehler zone=%s", zone_id,
            )
