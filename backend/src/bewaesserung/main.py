"""Haupteinstiegspunkt — Verdrahtet alle Module und startet den Prozess.

Starten mit:
    cd backend && python -m bewaesserung.main

Oder nach Installation:
    bewaesserung
"""

import asyncio
import errno
import fcntl
from datetime import datetime, timedelta
import os
import signal
import sqlite3
from typing import Any
import sys
import time
from pathlib import Path

import dotenv
import structlog

import uvicorn

# T-0098: Single-Instance-Lock. Verhindert dass zwei `bewaesserung.main`
# parallel laufen (z. B. launchd-Daemon + Manual-Start), was im April
# 2026 zu Endlos-`database is locked`-Loops gefuehrt hat.
# fcntl.LOCK_EX|LOCK_NB faellt sofort raus statt zu blockieren.
#
# T-0127 (H-3a): Pfad von `/tmp/bewaesserung.pid` nach
# `~/Library/Application Support/de.xindaan.pflanzen-dashboard/bewaesserung.pid`
# verschoben. Hintergrund: macOS raeumt `/tmp` bei laengerem Sleep
# gelegentlich auf -- der Lock-File verschwindet, waehrend die alte
# Prozess-Instanz noch laeuft. Ein zweiter `start.sh` denkt dann "kein
# Backend laeuft" und startet parallel -> DB-Lock-Konflikt. Persistent-
# Verzeichnis ueberlebt Sleep/Wake.
#
# ENV-Override `BEWAESSERUNG_LOCK_PFAD` fuer Tests + Spezial-Setups.
def _ermittle_lock_pfad() -> Path:
    override = os.environ.get("BEWAESSERUNG_LOCK_PFAD")
    if override:
        return Path(override)
    return (
        Path.home() / "Library" / "Application Support"
        / "de.xindaan.pflanzen-dashboard" / "bewaesserung.pid"
    )


_lock_handle = None  # Modul-Globale, sonst gibt GC das FD frei.

from bewaesserung.api_server import app as api_app, konfiguriere_api
from bewaesserung.benachrichtigung import Benachrichtiger
from bewaesserung import dosis_test
from bewaesserung.entscheidung import Entscheidungsmotor
from bewaesserung.fyta_client import FytaClient
from bewaesserung.gardena_client import GardenaClient
from bewaesserung.hahn_arbiter import HahnArbiter, Kanalprofil, baue_kanalprofile
from bewaesserung.konfig import lade_konfig
from bewaesserung.modelle import Ausloser, GesamtKonfig, MlAusschlussFenster, VentilEreignis, ZonenKonfig, ZonenModus, ist_auto_loop_zone, null_ist_sensordefekt
from bewaesserung.ventil_sicherung import (
    AUTOMATIK_MAX_DATEN_ALTER_MINUTEN,
    VentilSicherung,
)
from bewaesserung.sensordaten import SensorDatenVerarbeiter
from bewaesserung.speicher import AGGREGAT_FALLBACK_FENSTER_MIN, Speicher
from bewaesserung.sensor_health import SensorHealthMonitor
from bewaesserung.fyta_sprung_detektor import FytaSprungDetektor
from bewaesserung.regen_ensemble_job import RegenEnsembleJob
from bewaesserung.station_messung_job import StationMessungJob
from bewaesserung.wetter_modell_job import WetterModellJob
from bewaesserung.wasserbilanz_job import WasserbilanzJob
from bewaesserung.leck_detektor import KanalRolle, LeckDetektor, WirkungsProfil
from bewaesserung.gardena_customer_auth import baue_aus_env as baue_gardena_customer_auth
from bewaesserung.gardena_web_backfill import (
    GardenaWebBackfillJob, baue_zone_dswc_kanal_map,
)
from bewaesserung.sensor_backfill import SensorBackfillJob
from bewaesserung.sensor_dhs_backfill import (
    STARTUP_FENSTER_STUNDEN,
    SensorDhsBackfillJob,
    baue_sensor_zu_zone,
)
from bewaesserung.backup import BackupJob
from bewaesserung.ml.drift_job import MlDriftJob
from bewaesserung.ml.retrain_job import MlRetrainJob
from bewaesserung.ml.response_retrain_job import MlResponseRetrainJob
from bewaesserung.wetter_archiv import WetterArchivJob, baue_clients_aus_konfig
from bewaesserung.wetter import WetterManager
from bewaesserung.wetter_ereignisse import pruefe_wetter_ereignisse

logger = structlog.get_logger()

API_URL = "http://127.0.0.1:8090"
ENTSCHEIDUNGSINTERVALL_SEKUNDEN = 300
# T-0403: Takt des Wartungs-Loops. Gleicher Wert wie der Entscheidungstakt --
# die Jobs dort gaten sich ohnehin selbst (1 h bis 14 Tage), der Takt bestimmt
# nur, wie fein ihre Faelligkeit geprueft wird. Bewusst NICHT groesser gewaehlt:
# das haette die Job-Haeufigkeit mitgeaendert und den Fix mit einer zweiten
# Verhaltensaenderung vermischt.
WARTUNGSINTERVALL_SEKUNDEN = 300
LAUFSTATUS_INTERVALL_SEKUNDEN = 3600
# T-0346: Max. Wartezeit des feinen Pre-Soak-Tickers, wenn KEINE zeit-getriebene
# Phasengrenze ansteht (Idle / stop_fehler-Recheck). Naehert sich eine Grenze,
# wacht der Ticker exakt dort auf (Wall-Clock, sub-Sekunde) -> Hauptdose + Phasen-
# wechsel feuern praezise statt bis zu einen Loop-Tick (~5 min) spaet.
PRE_SOAK_TICK_IDLE_SEKUNDEN = 30


def _konfiguriere_logging(level: str) -> None:
    """Richtet strukturiertes Logging ein."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            # utc=False: alle Logs in Server-Lokalzeit, konsistent mit
            # _format_zeitpunkt (sonst mischt der Heartbeat UTC-zeit mit
            # lokal-formatierten naechster_lauf — verwirrt beim Lesen).
            structlog.processors.TimeStamper(
                fmt="%Y-%m-%d %H:%M:%S",
                key="zeit",
                utc=False,
            ),
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            structlog.stdlib.NAME_TO_LEVEL.get(level.lower(), 20)
        ),
    )


def _baue_ausschluss_fenster_pro_zone(
    fenster: list,
) -> dict[str, list[tuple[datetime, datetime, str | None]]]:
    """T-0211a: Gruppiert `ml_ausschluss_fenster`-Konfig nach zone_id.

    T-0386: Das Tupel traegt jetzt zusaetzlich `geraet_id` (3-Tupel). `None` =
    Fenster gilt fuer ALLE Sensoren der Zone; sonst nur fuer diesen Sensor.
    Frueher wurde geraet_id hier verworfen -> geraet-scoped Fenster wirkten
    still zone-weit. Jede Detektor-Konsumstelle entscheidet, wie sie das
    Tripel nutzt (per-Sensor-Heuristik honoriert, aggregat-Safety konservativ).
    """
    out: dict[str, list[tuple[datetime, datetime, str | None]]] = {}
    for f in fenster:
        out.setdefault(f.zone_id, []).append((f.von, f.bis, f.geraet_id))
    return out


# T-0556: ab hier gilt ein Entscheidungszyklus als kriechend und wird als
# WARNUNG geloggt statt nur als Zahl im Heartbeat. 60 s ist bewusst weit
# ueber dem Normalfall gewaehlt (16.09. gemessen: Median 0,2-2,2 s, Maximum
# 25,7 s ueber 122 Zyklen) -- die Schwelle soll Starvation melden, nicht
# den teuersten regulaeren Zyklus. Der Sensor-Backfill laeuft auf einem
# Intervall und kostet dabei immer 13-17 s; das ist Taktkost, kein Defekt.
ZYKLUS_WARNSCHWELLE_S = 60.0

# T-0556b: Vorlauf vor dem ersten Zyklus. Nur fuer Tests herabsetzbar.
ERSTE_PRUEFUNG_VERZOEGERUNG_S = 5.0


def logge_zyklus_ende(
    *, laufzeit_s: float, phasen: dict | None, **heartbeat_felder,
) -> bool:
    """T-0556: Heartbeat UND Starvation-Warnung in EINEM Aufruf.

    **Warum zusammen und nicht nebeneinander.** Erst standen beide als zwei
    Zeilen im Loop. Die Negativprobe "entferne den Warnungs-Aufruf" liess die
    Suite gruen -- der Waechter war da, seine Verdrahtung ungeprueft, und wer
    die Zeile geloescht haette, haette es nicht gemerkt
    ([[fehlerpattern_detektor_ohne_konsument]], Naht-Variante).

    Mit einem gemeinsamen Aufruf ist das konstruktiv unmoeglich: wer die
    Warnung entfernen will, muss den Heartbeat mitnehmen, und dessen Fehlen
    faellt im Log sofort auf. Der Waechter kann nicht mehr still verschwinden.

    Gibt True zurueck, wenn gewarnt wurde (fuer Tests).
    """
    gewarnt = warne_bei_kriechendem_zyklus(laufzeit_s, phasen)
    logger.info(
        "entscheidungsloop.heartbeat",
        laufzeit_s=laufzeit_s,
        # T-0458: teuerste Abschnitte des Zyklus. Leer = alles unter 0,5 s.
        phasen=phasen,
        **heartbeat_felder,
    )
    return gewarnt


def warne_bei_kriechendem_zyklus(
    laufzeit_s: float, phasen: dict | None,
    schwelle_s: float = ZYKLUS_WARNSCHWELLE_S,
) -> bool:
    """T-0556: meldet einen kriechenden Entscheidungszyklus als WARNUNG.

    Nicht direkt aus dem Loop aufrufen -- dafuer gibt es `logge_zyklus_ende`,
    das Warnung und Heartbeat koppelt (Begruendung dort).

    Gibt True zurueck, wenn gewarnt wurde.
    """
    if laufzeit_s < schwelle_s:
        return False
    logger.warning(
        "entscheidungsloop.zyklus_kriecht",
        laufzeit_s=laufzeit_s,
        schwelle_s=schwelle_s,
        phasen=phasen,
        hinweis=(
            "Zeitstempel dieses Zyklus sind zu spaet. Bei einer LUECKE "
            "zuerst `pmset -g log` pruefen (Laptop-Schlaf ist "
            "Normalbetrieb, T-0556), erst danach Code verdaechtigen."
        ),
    )
    return True


def baue_eigener_lauf_pruefer(ventil_sicherungen: dict):
    """T-0547/T-0572: das Praedikat, mit dem der Replay-Guard entscheidet, ob
    ein Event im Guard-Fenster zu unserem eigenen Lauf gehoert.

    Eigene Modul-Funktion statt Closure in `ausfuehren()`, damit die NAHT
    pruefbar ist. Solange die Verdrahtung in der grossen Startfunktion steckt,
    kann ein Test `lauf_plausibel_offen` luekenlos absichern und trotzdem
    nicht bemerken, wenn produktiv weiter das alte `haelt_ventil_offen`
    injiziert wird -- genau das zeigte die Negativprobe vom 16.09.: das
    Zuruecksetzen der Verdrahtung liess die gesamte Suite gruen.
    """
    def _eigener_lauf_offen(valve_id: str) -> bool:
        return any(
            sicherung.lauf_plausibel_offen(valve_id)
            for sicherung in ventil_sicherungen.values()
        )
    return _eigener_lauf_offen


def _format_zeitpunkt(zeitpunkt: datetime | None) -> str | None:
    """Formatiert Zeitpunkte kompakt fuer CLI- und Service-Logs (Lokalzeit)."""
    if not zeitpunkt:
        return None
    # TZ-aware datetimes auf lokale Zeit normalisieren, damit das Logging
    # konsistent mit dem structlog-TimeStamper ist (siehe konfiguriere_logging).
    if zeitpunkt.tzinfo is not None:
        zeitpunkt = zeitpunkt.astimezone()
    return zeitpunkt.strftime("%Y-%m-%d %H:%M:%S")


async def _laufstatus_heartbeat(
    stop_event: asyncio.Event,
    laufstatus: dict[str, datetime | None],
    modus: str,
) -> None:
    """Schreibt ein sparsames Lebenszeichen fuer lange laufende Prozesse."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=LAUFSTATUS_INTERVALL_SEKUNDEN,
            )
            return
        except asyncio.TimeoutError:
            pass

        logger.info(
            "laufstatus.heartbeat",
            modus=modus,
            api=API_URL,
            jetzt=_format_zeitpunkt(datetime.now()),
            letzter_entscheidungszyklus=_format_zeitpunkt(
                laufstatus.get("letzter_entscheidungszyklus")
            ),
            naechster_entscheidungszyklus=_format_zeitpunkt(
                laufstatus.get("naechster_entscheidungszyklus")
            ),
        )


async def _pre_soak_tick_loop(stop_event: asyncio.Event) -> None:
    """T-0346: Treibt die Pre-Soak-State-Machine fein-granular, entkoppelt vom
    5-min-Entscheidungsloop.

    Der Entscheidungsloop tickt die Sequenzen weiterhin (idempotenter Backup),
    aber die Aktion -- Phasenwechsel + Hauptdose-Ventiloeffnung -- lagte bis zu
    einen Loop-Tick (~300s) hinter der echtzeitigen Phasengrenze (Realfall: 97s).
    Dieser Ticker wacht exakt an der naechsten Grenze auf (Wall-Clock) -> die
    Hauptdose feuert auf die Sekunde, der Frontend-Countdown (echtzeit) und die
    Realitaet bleiben deckungsgleich.

    Wake-sicher (T-0336): `asyncio.wait_for`-Timeout, jeder Tick rechnet
    `verstrichen` frisch aus `datetime.now()` -> ein Laptop-Sleep kann keinen
    Timer einfrieren; ein uebersprungener Uebergang wird beim naechsten Tick
    per T-0344-Nachzug nachgezogen. Idle/Endphasen (inkl. stop_fehler-Recheck,
    T-0342) -> Wartezeit auf PRE_SOAK_TICK_IDLE_SEKUNDEN gedeckelt.
    """
    while not stop_event.is_set():
        from bewaesserung.api_server import (
            _pre_soak_manager as _ps_primary,
            _pre_soak_managers as _ps_managers_dict,
        )
        managers = list(_ps_managers_dict.values()) or (
            [_ps_primary] if _ps_primary is not None else []
        )
        sek: float | None = None
        for mgr in managers:
            s = mgr.sekunden_bis_naechster_uebergang()
            if s is not None:
                sek = s if sek is None else min(sek, s)
        timeout = (
            PRE_SOAK_TICK_IDLE_SEKUNDEN if sek is None
            else max(0.5, min(sek, PRE_SOAK_TICK_IDLE_SEKUNDEN))
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=timeout)
            return  # stop_event gesetzt -> sauber beenden
        except asyncio.TimeoutError:
            pass
        jetzt = datetime.now()
        for mgr in managers:
            try:
                await mgr.tick(jetzt)
            except Exception:
                logger.exception("pre_soak_tick_loop.fehler")


async def _geraete_discovery(
    client: GardenaClient, konfig: GesamtKonfig,
    speicher: Speicher | None = None,
) -> dict[str, str]:
    """Zeigt gefundene Geraete und erstellt Zone-Zuordnung.

    Gibt ein Dict zurueck: geraet_id -> zone_id.
    Persistiert Zuordnungen in der DB (wenn Speicher vorhanden).
    Bei API-Fehler: Fallback auf gespeicherte Zuordnungen.
    """
    try:
        geraete = client.hole_geraete()
    except Exception:
        logger.warning("discovery.api_fehler_fallback")
        if speicher:
            gespeichert = await speicher.hole_zuordnungen()
            if gespeichert:
                logger.info("discovery.fallback_geladen", anzahl=len(gespeichert))
                return gespeichert
        raise

    logger.info("discovery.geraete_gefunden", anzahl=len(geraete))
    for gid, info in geraete.items():
        logger.info(
            "discovery.geraet",
            id=gid,
            name=info["name"],
            typ=info["typ"],
        )
        if info["typ"] in ("SENSOR", "SOIL_SENSOR"):
            logger.info(
                "discovery.sensor_werte",
                id=gid,
                feuchte=info.get("boden_feuchte"),
                temp=info.get("boden_temperatur"),
            )

    # Zuordnung: Geraete-Name (lowercase) wird gegen Zone-ID und Zone-Name gematcht
    sensoren = {
        gid: info for gid, info in geraete.items()
        if info["typ"] in ("SENSOR", "SOIL_SENSOR")
    }
    zuordnung = {}

    for gid, info in sensoren.items():
        geraet_name = info["name"].lower()

        geraet_norm = geraet_name.replace(" ", "_")

        # Zwei Durchlaeufe: erst exakte Matches, dann Teilstring-Matches
        # Exakt: Geraetename == Zone-Name oder Zone-ID (mit Leerzeichen-Normalisierung)
        bester_match: str | None = None
        for zone in konfig.zonen:
            zone_name = zone.name.lower()
            zone_name_norm = zone_name.replace(" ", "_")
            zone_id = zone.zone_id.lower()
            if geraet_norm in (zone_name, zone_name_norm, zone_id):
                bester_match = zone.zone_id
                break

        # Teilstring: laengster Match gewinnt (verhindert dass "bambuswald"
        # faelschlich auf "bambuswald_yogaraum" matcht)
        if not bester_match:
            beste_laenge = 0
            for zone in konfig.zonen:
                zone_name = zone.name.lower()
                zone_id = zone.zone_id.lower()
                if (zone_id in geraet_name or zone_name in geraet_name
                        or geraet_name in zone_id or geraet_name in zone_name):
                    match_laenge = max(len(zone_name), len(zone_id))
                    if match_laenge > beste_laenge:
                        beste_laenge = match_laenge
                        bester_match = zone.zone_id

        if bester_match:
            zuordnung[gid] = bester_match
            logger.info(
                "discovery.zuordnung",
                sensor_id=gid,
                sensor_name=info["name"],
                zone=bester_match,
            )
        else:
            logger.warning(
                "discovery.keine_zuordnung",
                sensor_id=gid,
                sensor_name=info["name"],
                hinweis="Sensor keiner Zone zugeordnet — wird trotzdem geloggt",
            )
            # Sensor ohne Zone: verwende Geraete-Namen als Zone-ID
            zuordnung[gid] = info["name"].lower().replace(" ", "_")

    # Zuordnungen persistieren
    if speicher and zuordnung:
        for gid, zid in zuordnung.items():
            geraet_name = sensoren.get(gid, {}).get("name")
            await speicher.speichere_zuordnung(gid, zid, geraet_name)
        logger.info("discovery.zuordnungen_gespeichert", anzahl=len(zuordnung))

    return zuordnung


async def ausfuehren(konfig_pfad: Path | None = None) -> None:
    """Hauptschleife — verbindet, sammelt Daten, laeuft bis Ctrl+C."""
    # .env laden (idempotent, falls schon von main() geladen)
    projekt_root = Path(__file__).resolve().parent.parent.parent.parent
    dotenv.load_dotenv(projekt_root / ".env")

    konfig = lade_konfig(konfig_pfad)
    _konfiguriere_logging(konfig.log_level)

    start_modus = os.environ.get("GARDENA_START_MODUS", "interaktiv")
    if start_modus not in {"service", "interaktiv"}:
        start_modus = "interaktiv"

    logger.info("start", zonen=[z.zone_id for z in konfig.zonen], modus=start_modus)

    # Speicher initialisieren
    speicher = Speicher(konfig.speicher.db_pfad)
    await speicher.verbinden()
    logger.info("speicher.verbunden", db=konfig.speicher.db_pfad)

    # T-0296: Einmal-Reparatur verwaister SCHLIESSEN-Klassifikation beim Start
    # (Folge des frueheren ±900s-Paarungs-Fensters; idempotent -- nach Heilung
    # findet der Lauf nichts mehr). Best-Effort: ein Fehler darf den Start
    # nicht blockieren.
    try:
        geheilt = await speicher.heile_verwaiste_paar_klassifikation()
        if geheilt:
            logger.info("ventil_paar.heilung", geheilt=geheilt)
    except Exception:
        logger.exception("ventil_paar.heilung_fehler")

    # Sensordaten-Verarbeiter
    verarbeiter = SensorDatenVerarbeiter(speicher)

    # Gardena-Client
    client = GardenaClient(
        client_id=konfig.gardena.client_id,
        client_secret=konfig.gardena.client_secret,
    )

    # Wetter-Manager (Multi-Standort)
    wetter_manager = WetterManager(konfig.wetter)
    logger.info(
        "wetter.standorte",
        ids=wetter_manager.standort_ids,
    )

    # T-0065: Response-Service (Inverse-Dauer-Empfehlung). Nur instanzieren,
    # wenn das Feature aktiv ist — sonst bleibt der Service None und der
    # Motor faellt stumm auf die Heuristik zurueck.
    ml_response_service = None
    if konfig.ml_bewaesserungs_response.aktiv:
        try:
            from bewaesserung.ml.response_vorhersage import MLResponseService
            from pathlib import Path as _Path
            _ausgabe = (
                konfig.ml_bewaesserungs_response.ausgabe_pfad
                or str(_Path(__import__("bewaesserung.konfig", fromlist=["ML_DATEN_PFAD"]).ML_DATEN_PFAD) / "response")
            )
            ml_response_service = MLResponseService.instanz(_ausgabe)
        except Exception:
            logger.exception("ml.response.service_init_fehler")

    # Entscheidungsmotor (Log-Only — steuert noch kein Ventil).
    # T-0105: GesamtKonfig direkt mitgeben; ML-Service wird unten via
    # Setter nachtraeglich gesetzt (er ist hier noch nicht initialisiert).
    motor = Entscheidungsmotor(
        speicher, wetter_manager, konfig.zonen, konfig.standorte,
        schwellen_adaption=konfig.schwellen_adaption,
        response_konfig=konfig.ml_bewaesserungs_response,
        response_service=ml_response_service,
        bilanz_konfig=konfig.bilanz,  # T-0066: Liter-Umrechnung in vorhersage_zone
        konfig=konfig,
        # T-0292 Stufe 2: Plateau-Wirkungs-Fit-Adoption (Default adoptieren=False).
        wirkung_fit_konfig=konfig.ml_wirkung_fit,
    )

    # Sensor-Gesundheitsmonitor
    # T-0214: Pro-Zone-Override fuer SENSOR_AUSFALL-Schwelle (Bluetooth-
    # Only-Sensoren wie mandevilla_maxi/pilea brauchen 96h statt 12h).
    ausfall_schwelle_pro_zone = {
        z.zone_id: int(z.ausfall_schwelle_stunden)
        for z in konfig.zonen
        if z.ausfall_schwelle_stunden is not None
    }
    health_monitor = SensorHealthMonitor(
        speicher,
        ausfall_schwelle_pro_zone=ausfall_schwelle_pro_zone,
    )

    # Leck-/Dauerlauf-Detektor (saisonal Mai-Sept)
    # T-0416: Detektor fuer serverseitige FYTA-Kalibrier-Pushes.
    # Read-only gegenueber der Bewaesserung -- schreibt nur eine Warnung und
    # loest bewusst KEINE Giess-Reaktion aus (ein Skalen-Sprung ist ein
    # Daten-Artefakt; wer darauf giesst, giesst wegen eines Server-Deploys).
    # Eigenes Intervall-Gate (60 min), damit der Zonen-Scan nicht in jedem
    # 5-min-Zyklus laeuft.
    # T-0416 Stufe 2: automatisch gesetzte Ausschluss-Fenster aus der DB in
    # die LEBENDE Konfig-Liste mergen. Damit erreichen sie alle sieben
    # Konsumenten (regime_klassifikation, k_basis_fit_job,
    # skalen_mapping_fit_job, kalibrierung, schwellen_vorschlag, api_server,
    # features), ohne eine einzige Call-Site anzufassen -- sie alle lesen
    # `konfig.ml_ausschluss_fenster`.
    try:
        auto_fenster = await speicher.hole_auto_ausschluss()
        for af in auto_fenster:
            konfig.ml_ausschluss_fenster.append(MlAusschlussFenster(
                zone_id=af["zone_id"], geraet_id=af["geraet_id"],
                von=datetime.fromisoformat(af["von"]),
                bis=datetime.fromisoformat(af["bis"]),
                grund=af["grund"],
            ))
        if auto_fenster:
            logger.info(
                "fyta_ausschluss.gemergt", anzahl=len(auto_fenster),
            )
    except Exception:
        logger.exception("fyta_ausschluss.merge_fehler")


    # T-0423: Regen-Ensemble (Shadow, read-only). Sammelt die Verteilung
    # (p10/p20/p50/p90 + Spread), damit T-0425 spaeter belegen kann, ob der
    # p20 besser entscheidet als der deterministische Punktwert. Der Job
    # entscheidet NICHTS -- kein Einfluss auf die Bewaesserung.
    # T-0422: FAO-56-Bilanz, Shadow. Schreibt taeglich Dr fort und
    # vergleicht mit der echten Engine-Entscheidung. Entscheidet nichts.
    wasserbilanz_job = WasserbilanzJob(speicher, konfig.zonen, konfig)

    regen_ensemble_job = RegenEnsembleJob(
        speicher,
        [(s.id, s.breite, s.laenge) for s in konfig.wetter.standorte],
    ) if konfig.wetter.standorte else None

    # T-0505: Mehrmodell-Mitschnitt + gemessene Referenz. Beide Shadow, beide
    # nur GET, beide ohne jeden Einfluss auf die Entscheidung. Sie sammeln
    # die Grundlage fuer T-0506 (stellt die Vorhersage die Giess-Entscheidung
    # falsch, und hilft ein zweites Modell?).
    _standort_tupel = [
        (s.id, s.breite, s.laenge) for s in konfig.wetter.standorte
    ]
    wetter_modell_job = WetterModellJob(
        speicher, _standort_tupel,
    ) if konfig.wetter.standorte else None
    station_messung_job = StationMessungJob(
        speicher, _standort_tupel,
    ) if konfig.wetter.standorte else None

    # T-0197: pro-Zone-Wirkungs-Profil aus der Konfig durchreichen,
    # damit der Detektor Plateau-Substrate (Bambus, Topf) nicht
    # faelschlich als "Bewaesserung ohne Wirkung" markiert.
    leck_detektor = LeckDetektor(
        speicher,
        wirkungs_profile={
            z.zone_id: WirkungsProfil.aus_zone(z) for z in konfig.zonen
        },
        # T-0213: ml_ausschluss_fenster auch im Leck-Detektor
        # respektieren -- analog T-0211a fuer die Heuristik. In
        # Bodenart-Reset-/Sensor-Neueinbau-Phasen sind die Werte
        # volatil und produzieren False-Positives.
        # T-0428: Saettigungs-Gate + Aufloesungs-Untergrenze.
        # `feuchte_schwelle_max` pro Zone -- in gesaettigten Boden zu
        # giessen kann den Messwert nicht heben. Quelle pro Zone bleibt
        # leer -> konservativer Gardena-Default (5 pp Raster).
        max_feuchte_pro_zone={
            z.zone_id: z.feuchte_schwelle_max for z in konfig.zonen
            if z.feuchte_schwelle_max is not None
        },
        ausschluss_fenster_pro_zone=_baue_ausschluss_fenster_pro_zone(
            konfig.ml_ausschluss_fenster
        ),
        # T-0251: per-Zone-Override fuers Detektor-Auswerte-Fenster-
        # Ende. Substrate mit langsamer Sensor-Antwort (Bambus,
        # Magerwiese) brauchen 180 min statt der Default-90 min.
        fenster_ende_min_pro_zone={
            z.zone_id: z.detektor_fenster_ende_min
            for z in konfig.zonen if z.detektor_fenster_ende_min is not None
        },
        # T-0540: Zonen, die die "Bewaesserung ohne Wirkung"-Pruefung
        # abgewaehlt haben. Nur diese eine Pruefung -- sensor_eingefroren und
        # lead_divergenz laufen fuer dieselben Zonen weiter.
        wirkungs_alarm_aus=frozenset(
            z.zone_id for z in konfig.zonen if not z.wirkungs_alarm_aktiv
        ),
        # T-0574: analog fuer die "Sensor eingefroren"-Pruefung.
        eingefroren_alarm_aus=frozenset(
            z.zone_id for z in konfig.zonen if not z.eingefroren_alarm_aktiv
        ),
        # T-0433: Kanal-Topologie fuer den Lead-Divergenz-Check. Bis hierher
        # war `kanal_trigger_ausschluss` write-only -- ausserhalb der
        # Trigger-Filterung in entscheidung.py las das Feld niemand, eine
        # ausgeschlossene Zone war damit unsichtbar statt bloss
        # nicht-triggernd. Nur Zonen MIT Ventil-Kanal; ohne Kanal gibt es
        # keine Lead-Beziehung.
        kanal_topologie={
            z.zone_id: KanalRolle(
                kanal=z.ventil_kanal,
                ausgeschlossen=z.kanal_trigger_ausschluss,
                feuchte_kritisch=z.feuchte_kritisch,
                # T-0438: Schliess-Kriterium braucht die Giess-Schwelle.
                feuchte_schwelle_min=z.feuchte_schwelle_min,
                # Pflicht: zwei DSWCs haben beide einen "Kanal 2".
                geraet_id=z.ventil_geraet_id,
            )
            for z in konfig.zonen if z.ventil_kanal is not None
        },
    )

    # Wetter-Archiv (Ground-Truth Regen/ET0) — laeuft einmal pro Tag
    wetter_archiv_job = WetterArchivJob(
        speicher, baue_clients_aus_konfig(konfig.wetter),
    )

    # DB-Backup (T-0043) — taeglicher Snapshot mit Rotation
    backup_job = BackupJob(speicher, konfig.backup)

    # T-0480: Service-Log-Rotation. Der Pfad folgt der Pfad-Konvention aus
    # CLAUDE.md -- Service-Logs liegen im Top-Level-`daten/logs/`, NICHT
    # unter `backend/daten/` (das sind die backup-pflichtigen Runtime-Daten).
    # Absolut aus `__file__`, nie relativ: ein relatives `daten/logs` haengt
    # am CWD und faengt sich je nach Start-Art am falschen Verzeichnis fest.
    from bewaesserung.log_rotation_job import LogRotationJob
    log_rotation_job = LogRotationJob(
        Path(__file__).resolve().parents[3] / "daten" / "logs",
    )

    # T-0210: Orphan-Close-Job — schreibt synthetisches SCHLIESSEN fuer
    # OEFFNEN-Events ohne Pendant in der DB (WS-Reconnect-Loss). 10-min-
    # Tick, max_dauer + 30 min Grace pro Zone.
    from bewaesserung.orphan_close_job import OrphanCloseJob
    orphan_close_job = OrphanCloseJob(speicher, list(konfig.zonen))

    # T-0250: Auto-Ignorieren-Job -- flippt `manuell`/`watchdog`-Events
    # in opt-in `ml_ausschluss_fenster`-Eintraegen (mit
    # `events_auto_ignorieren: true`) auf `ignoriert`. Use-Case:
    # Ventilkanal temporaer fuer Fremd-Zweck (z.B. Hecke-Kanal beregnet
    # Gras-Aussaat). Tick alle 10 min.
    from bewaesserung.auto_ignorieren_job import AutoIgnorierenJob
    auto_ignorieren_job = AutoIgnorierenJob(
        speicher, list(konfig.ml_ausschluss_fenster),
    )

    # T-0047: ML-Drift-Job evaluiert Prognosen gegen Sensor-Messungen
    ml_drift_job = MlDriftJob(speicher)

    # T-0048: woechentlicher ML-Retrain mit Deploy-Gate (Opt-In via Konfig)
    ml_retrain_job = MlRetrainJob(speicher, konfig, konfig.ml_retrain)

    # T-0065: Response-Modell-Retrain (Dauer-Empfehlung pro Zone)
    ml_response_retrain_job = MlResponseRetrainJob(
        speicher, konfig, konfig.ml_bewaesserungs_response,
    )

    # T-0055-B1: Sensor-Heuristik-Fallback.
    # Bis T-0175 lief der Job nur fuer Gardena-Zonen mit ventil_kanal —
    # FYTA-Topfpflanzen waren ausgenommen, weil Phantom-Risiko bei FYTA-
    # "veraltete Daten"-Bursts hoch ist und kein DB-Event-Fallback
    # benoetigt wurde.
    #
    # T-0175 (10.05.): jetzt alle Zonen mit FYTA-Sensor mit-aktiviert,
    # damit der User per Sensor-Sprung-Erkennung + Klassifikations-UI im
    # Ops-Tab seine Indoor-Bewaesserungen nachtragen kann statt jeden Puls
    # manuell via "Gegossen"-Button zu loggen. Indoor-Flag schaltet den
    # Regen-Check ab (siehe `sensor_backfill._schreibe_wenn_kandidat`).
    _heuristik_zonen = list(konfig.zonen)
    sensor_backfill_job = SensorBackfillJob(
        speicher,
        zone_ids=[z.zone_id for z in _heuristik_zonen],
        karenz_stunden_pro_zone={
            z.zone_id: z.versickerungs_karenz_stunden for z in _heuristik_zonen
        },
        # T-0123: Mapping fuer Live-Lauf-Check — verhindert Phantom-Events
        # waehrend Backend-Bewaesserungen. Nur fuer Zonen mit Kanal sinnvoll.
        zone_zu_kanal={
            z.zone_id: z.ventil_kanal
            for z in _heuristik_zonen if z.ventil_kanal is not None
        },
        zone_zu_geraet={
            z.zone_id: z.ventil_geraet_id
            for z in _heuristik_zonen
            if z.ventil_kanal is not None and z.ventil_geraet_id is not None
        },
        # T-0175: Indoor-Flag, damit Regen-Check fuer Wohnung-Pflanzen
        # ausgeschaltet wird (ist_indoor=true).
        indoor_zone_ids={
            z.zone_id for z in _heuristik_zonen if z.ist_indoor
        },
        # F13: zone_id -> wetter_standort, sonst pruefte der Regen-Check
        # Outdoor-Balkon-Zonen (wetter_standort=berlin) gegen den falschen
        # Standort. Der Fallback fuer ungemappte Zonen kommt aus der Konfig
        # (erster Standort) statt aus einem hartkodierten Ortsnamen.
        standort_default=(
            konfig.standorte[0].wetter_standort if konfig.standorte else None
        ),
        standort_pro_zone={
            zid: s.wetter_standort
            for s in konfig.standorte
            for zid in s.zonen
            if s.wetter_standort
        },
        # T-0187: Pro-Zone-Heuristik-Schwelle (FYTA-Indoor-Toepfe
        # brauchen niedrigere Schwellen als globale 5 pp).
        min_delta_pp_pro_zone={
            z.zone_id: float(z.heuristik_min_delta_pp)
            for z in _heuristik_zonen
            if z.heuristik_min_delta_pp is not None
        },
        # T-0187: Roll-Up-Heuristik fuer langsame Sicker-Cadence
        # (Mandevilla-Pattern: 1-2 pp/Beat ueber Stunden).
        rollup_pro_zone={
            z.zone_id: (
                int(z.heuristik_rollup_fenster_min),
                float(z.heuristik_rollup_schwelle_pp),
            )
            for z in _heuristik_zonen
            if z.heuristik_rollup_fenster_min
            and z.heuristik_rollup_schwelle_pp
        },
        # T-0211a: Ausschluss-Fenster pro Zone gruppieren. Heuristik
        # setzt waehrend dieser Phase aus (Bodenart-Reset, Sensor-
        # Versetzung). Verhindert Phantom-UNBEKANNT-Mass-Aufkommen.
        ausschluss_fenster_pro_zone=_baue_ausschluss_fenster_pro_zone(
            konfig.ml_ausschluss_fenster,
        ),
        # T-0312: Cross-Spray-Quell-Zonen (Sprinkler einer Nachbarzone trifft
        # den Sensor dieser Zone -> Sprung ist keine eigene Bewaesserung).
        cross_spray_quellen={
            z.zone_id: z.cross_spray_quell_zonen
            for z in _heuristik_zonen if z.cross_spray_quell_zonen
        },
    )

    # iMessage-Benachrichtigung fuer Monitoring-Zonen
    benachrichtiger = Benachrichtiger()

    fyta_sprung_detektor = FytaSprungDetektor(
        speicher, konfig.zonen,
        # T-0416 (23.07.): iMessage-Push. Hochgestuft, weil FYTA ein
        # `calibration_version`-Feld abgesagt hat -- dieser Detektor ist
        # dauerhaft die einzige Quelle. Throttle 24 h im Detektor.
        benachrichtiger=benachrichtiger,
        empfaenger=os.environ.get("IMESSAGE_EMPFAENGER", ""),
        # Die lebende Liste -- ein neu erkanntes Fenster wirkt sofort,
        # nicht erst nach dem naechsten Neustart.
        konfig_fenster=konfig.ml_ausschluss_fenster,
    )

    # T-0038: Wochen-Report per iMessage (Opt-In via Konfig)
    from bewaesserung.report import WochenReportJob
    wochen_report_job = WochenReportJob(
        speicher, konfig, konfig.wochen_report, benachrichtiger,
    )

    # T-0063: Automatische Feldkapazitaets-/Welkepunkt-Kalibrierung
    from bewaesserung.kalibrierung import KalibrationsJob
    kalibrations_job = KalibrationsJob(
        speicher, konfig, konfig.kalibrierung,
    )

    # T-0181: Skalen-Mapping-Fit-Job (fittet pro Sensor-Quelle die
    # lineare Transformation gegen Gardena als Referenz). Wirkt live
    # ueber `letzte_messung_aggregiert`, sobald `(a, b)` persistiert
    # sind. Greift erst nach `min_spannweite_pp` echter Schwankung.
    from bewaesserung.skalen_mapping_fit_job import SkalenMappingFitJob
    skalen_mapping_fit_job = SkalenMappingFitJob(
        speicher, konfig, konfig.ml_skalen_mapping,
    )

    # Hybrid Stufe 1: Physik-Trocknungs-Fit-Job. Fittet `k_basis_pro_h`
    # pro Zone aus historischen Trockenphasen. Read-only -- die Werte
    # erscheinen nur als Diagnose-Prognose in `empfehlung-jetzt`,
    # greifen NICHT in pruefe_zone/vorhersage_zone ein.
    from bewaesserung.ml.k_basis_fit_job import KbasisFitJob
    k_basis_fit_job = KbasisFitJob(
        speicher, konfig, konfig.ml_physik_diagnose,
    )

    # T-0292 Stufe 2: Plateau-Wirkungs-Fit-Job. Fittet wmax/r0 pro Zone aus
    # den quality-gefilterten wirkungsrate-Kalibrierungs-Records + persistiert
    # nach `wirkung_fit`. Mit `adoptieren=False` (Default) read-only bzgl.
    # der Entscheidung -- nur Beobachtung in /api/ml/status.
    from bewaesserung.ml.wirkung_fit_job import WirkungFitJob
    wirkung_fit_job = WirkungFitJob(
        speicher, konfig, konfig.ml_wirkung_fit,
    )

    # T-0168: AquaBloom-Auto-Logging fuer Solar-Mini-Pumpen an FYTA-
    # Topfpflanzen ohne Ventil. Schreibt synthetische Pulse pro
    # konfigurierter Zone (siehe ZonenKonfig.aquabloom_*-Felder +
    # config/default.yaml fuer zitrus + kasten_4 als Beispiele).
    from bewaesserung.aquabloom_job import AquabloomJob
    aquabloom_job = AquabloomJob(speicher, konfig)

    try:
        await client.verbinden()

        # Geraete entdecken und Zonen zuordnen (mit DB-Fallback)
        zuordnung = await _geraete_discovery(client, konfig, speicher)
        for geraet_id, zone_id in zuordnung.items():
            client.registriere_geraet_zone(geraet_id, zone_id)

        # Zone-Namen fuer Valve-Matching registrieren (SMART_IRRIGATION_CONTROL)
        # Valve-Name (wie in Gardena-App vergeben) -> zone_id.
        # Bei abweichendem Gardena-App-Namen kann z.ventil_name als Override gesetzt
        # werden. Zonen am gleichen ventil_kanal teilen sich den Bewaesserungskreis,
        # daher reicht ein Treffer pro Kanal (main.py expandiert spaeter).
        # T-0204 (17.05.): Filter `ventil_kanal is None` entfernt, weil Monitoring-
        # Zonen wie magerwiese physisch sehr wohl an einem Ventil haengen koennen
        # (Multi-DSWC-Setup, Auto-Bewaesserung aus statt aufgrund Sensor-Kalibrier-
        # Phase). Ohne den Eintrag im zone_namen_map landet der Live-Event mit
        # `zone_id = valve_uuid` statt `zone_id = magerwiese` -> Dashboard sieht
        # den Event nicht in der Magerwiese-Karte.
        zone_namen_map: dict[str, str] = {}
        for z in konfig.zonen:
            zone_namen_map[z.name] = z.zone_id
            if z.ventil_name:
                zone_namen_map[z.ventil_name] = z.zone_id
        client.registriere_zone_namen(zone_namen_map)

        # T-0055-B3: DHS-Backfill-Job (nur aktiv wenn Customer-Credentials in .env)
        # T-0203 (17.05.): Multi-DSWC-Support — ein Backfill-Job pro DSWC.
        # Legacy-Variable `gardena_web_backfill_job` zeigt auf den ersten
        # Job (primary DSWC) fuer Backward-Compat.
        gardena_web_backfill_jobs: list[GardenaWebBackfillJob] = []
        gardena_web_backfill_job: GardenaWebBackfillJob | None = None
        sensor_dhs_backfill_job: SensorDhsBackfillJob | None = None
        sensor_dhs_startup_task: asyncio.Task | None = None
        token_datei = Path(konfig.speicher.db_pfad).parent / "gardena_customer_token.json"
        customer_auth = baue_gardena_customer_auth(token_datei)
        if customer_auth is not None:
            loc = client.location_id
            wc_ids = client.water_control_geraet_ids()
            if loc and wc_ids:
                from bewaesserung.gardena_web_backfill import (
                    baue_kanal_zu_zonen_pro_geraet,
                )
                kanal_pro_geraet = baue_kanal_zu_zonen_pro_geraet(
                    konfig.zonen, primary_geraet_id=wc_ids[0],
                )
                for wc_id in wc_ids:
                    kanal_zu_zonen_lokal = kanal_pro_geraet.get(wc_id, {})
                    if not kanal_zu_zonen_lokal:
                        # DSWC ohne konfigurierte Zonen — Backfill ueberspringen
                        # (sonst replay-ed der Job leer und produziert Log-Noise).
                        logger.info(
                            "dhs_backfill.dswc_ohne_zonen",
                            location=loc, device=wc_id[:8] + "...",
                        )
                        continue
                    job = GardenaWebBackfillJob(
                        auth=customer_auth, speicher=speicher,
                        location_id=loc, water_control_geraet_id=wc_id,
                        kanal_zu_zonen=kanal_zu_zonen_lokal,
                    )
                    gardena_web_backfill_jobs.append(job)
                    logger.info(
                        "dhs_backfill.aktiviert",
                        location=loc, device=wc_id[:8] + "...",
                        zonen=sum(len(v) for v in kanal_zu_zonen_lokal.values()),
                    )
                gardena_web_backfill_job = (
                    gardena_web_backfill_jobs[0]
                    if gardena_web_backfill_jobs else None
                )
            else:
                logger.warning(
                    "dhs_backfill.kein_geraet",
                    location=loc, wcs=wc_ids,
                    hinweis="Kein Smart Water Control in Discovery — DHS-Backfill inaktiv",
                )

            # T-0068: Sensor-DHS-Backfill — zieht fehlende Bodenfeuchte-
            # Messungen aus offline-Phasen nach. Ueber dieselbe Auth wie
            # der Ventil-Backfill, eigener Endpoint-Preset (`sensor2`).
            if loc:
                try:
                    geraete = client.hole_geraete()
                except Exception:
                    logger.exception("sensor_dhs_backfill.discovery_fehler")
                    geraete = {}
                sensor_uuids = {
                    gid for gid, info in geraete.items()
                    if info.get("typ") in ("SENSOR", "SOIL_SENSOR")
                }
                sensor_zu_zone = baue_sensor_zu_zone(zuordnung, sensor_uuids)
                if sensor_zu_zone:
                    sensor_dhs_backfill_job = SensorDhsBackfillJob(
                        auth=customer_auth, speicher=speicher,
                        location_id=loc, sensor_zu_zone=sensor_zu_zone,
                    )
                    logger.info(
                        "sensor_dhs_backfill.aktiviert",
                        location=loc, sensoren=len(sensor_zu_zone),
                    )
                    # Startup-Gap-Fill als Background-Task: blockiert
                    # den Service-Start nicht, holt aber direkt das
                    # 7-Tage-Fenster nach Crash/Offline-Phasen.
                    sensor_dhs_startup_task = asyncio.create_task(
                        sensor_dhs_backfill_job.aktualisiere(
                            von_stunden=STARTUP_FENSTER_STUNDEN,
                        )
                    )

                    def _startup_fertig(t: asyncio.Task) -> None:
                        try:
                            n = t.result()
                            logger.info(
                                "sensor_dhs_backfill.startup_fertig", neu=n,
                            )
                        except Exception:
                            logger.exception(
                                "sensor_dhs_backfill.startup_fehler",
                            )

                    sensor_dhs_startup_task.add_done_callback(_startup_fertig)

                    # T-0324: WS-Gap-Catch-up. Reconnectet die Husqvarna-WS nach
                    # einer Offline-/Sleep-Phase selbst, holen die Gardena-
                    # Sensorwerte sonst erst bei der naechsten (langsamen) Cadence
                    # oder einem manuellen Restart auf. Den Sensor-DHS-Catch-up
                    # daher direkt an die WS-Gap-Erkennung koppeln (T-0306/T-0288).
                    # `aktualisiere_wenn_faellig` nutzt das adaptive Catch-up-
                    # Fenster (T-0287) + Intervall-Gate (natuerlicher Cooldown
                    # gegen flappende Reconnects).
                    _sdb_job = sensor_dhs_backfill_job

                    async def _ws_gap_sensor_catchup(luecke_s: float) -> None:
                        try:
                            neu = await _sdb_job.aktualisiere_wenn_faellig()
                            logger.info(
                                "sensor_dhs_backfill.ws_gap_catchup",
                                luecke_s=round(luecke_s, 1), neu=neu,
                            )
                        except Exception:
                            logger.exception(
                                "sensor_dhs_backfill.ws_gap_catchup_fehler",
                            )

                    client.registriere_ws_gap_callback(_ws_gap_sensor_catchup)
                else:
                    logger.info(
                        "sensor_dhs_backfill.keine_sensoren_zugeordnet",
                        hinweis="Sensor-Discovery leer oder keine Zone-Mappings",
                    )

        # Sensor-Callback registrieren
        client.registriere_sensor_callback(verarbeiter.verarbeite)

        # VentilSicherung: Safety-Wrapper fuer Ventilsteuerung.
        # T-0110 (29.04.): VentilSicherung wird IMMER initialisiert wenn
        # Ventil-Hardware verfuegbar ist — wird sowohl fuer Live-Manuell-
        # Endpoints (POST /api/ventil/manuell-{start,stop}) als auch
        # fuer den Auto-Loop benutzt.
        # Das `ventilsteuerung_aktiv: false`-Flag steuert nur, ob der
        # AUTO-LOOP eigenstaendig Ventile schaltet. Manuelle Aktionen vom
        # Dashboard funktionieren unabhaengig davon (Hand-am-Ruder-Prinzip).
        # T-0203 (2026-05-17): Multi-DSWC-Support. Eine VentilSicherung-
        # Instanz pro entdecktem Water-Control-Geraet. Routing pro Zone
        # via `ZonenKonfig.ventil_geraet_id`; Zonen ohne explizites Feld
        # laufen auf der ersten entdeckten DSWC (Backward-Compat).
        ventil_sicherungen: dict[str, VentilSicherung] = {}
        ventil_sicherung: VentilSicherung | None = None  # primary (backward-compat)
        # T-0537: der Arbiter entsteht erst weiter unten und NUR, wenn es
        # Ventilgeraete gibt. Der Watchdog liest ihn (Trigger H) ausserhalb
        # dieses `if` -- ohne die Vorbelegung waere das ein NameError, der
        # alles danach still killt ([[fehlerpattern_job_nicht_in_loop_verdrahtet]]).
        hahn_arbiter: HahnArbiter | None = None
        # T-0449: dieselbe Zonen-Kanal-Zuordnung, aus der das Mapping unten
        # gebaut wird -- aufgehoben, damit der Reconnect-Hook es ohne zweite
        # Wahrheit neu bauen kann.
        kanal_zuordnung_pro_dswc: dict[str, list[tuple]] = {}
        geraete = client.hole_geraete()
        ventil_geraete_ids = [
            gid for gid, info in geraete.items()
            if info["typ"] in ("WATER_CONTROL", "SMART_IRRIGATION_CONTROL")
        ]
        if ventil_geraete_ids:
            primary_geraet_id = ventil_geraete_ids[0]
            # T-0151 + T-0153 + T-0536: Hahn-Cluster-Lockprofile sind GLOBAL
            # (zonen- UND geraeteuebergreifend). Bis T-0536 wurden sie pro
            # DSWC repliziert, mit der Bedingung "solange keine Cluster ueber
            # DSWCs spannen: keine Aktion" -- die gilt seit dem
            # Magerwiese-Anschluss (13.06.2026) nicht mehr, und am 12.08.
            # liefen zwei exklusive Sprinkler an verschiedenen DSWCs parallel.
            # Seitdem entscheidet der zentrale `HahnArbiter` (s. dort), der
            # zusaetzlich Fremdlaeufe aus `ventil_ereignis` liest.
            cluster_max_lpm_lokal = {
                c.cluster_id: c.max_durchfluss_lpm for c in konfig.hahn_cluster
            }
            kanalprofile_global: dict[tuple[str, int], Kanalprofil] = {}
            for dswc_id in ventil_geraete_ids:
                # Zonen, die dieser DSWC zugeordnet sind (ventil_geraet_id
                # explizit gesetzt ODER Backward-Compat: primary).
                zonen_an_dieser_dswc = [
                    z for z in konfig.zonen
                    if z.ventil_kanal is not None and (
                        z.ventil_geraet_id == dswc_id
                        or (z.ventil_geraet_id is None and dswc_id == primary_geraet_id)
                    )
                ]
                if not zonen_an_dieser_dswc:
                    # DSWC entdeckt aber keine Zonen konfiguriert — Sicherung
                    # trotzdem anlegen, damit unbenannte Live-Events sauber
                    # geschlossen werden koennen via Notfall-Stopp.
                    logger.info(
                        "ventil_sicherung.dswc_ohne_zonen", geraet=dswc_id,
                    )
                # T-0112: Kanal -> valve_id-Mapping pro DSWC.
                zonen_kanal_zuordnung = [
                    (z.ventil_kanal, z.ventil_name, z.name)
                    for z in zonen_an_dieser_dswc
                ]
                kanal_zuordnung_pro_dswc[dswc_id] = zonen_kanal_zuordnung
                kanal_zu_valve_id = client.baue_kanal_zu_valve_id(
                    dswc_id, zonen_kanal_zuordnung,
                )
                # T-0151: Lockprofile pro Kanal — auch nur die Zonen
                # dieser DSWC. Bleibt als Fallback, wenn kein Arbiter
                # gesetzt ist (Tests, Setup ohne Speicher).
                kanal_zu_lockprofile_lokal: dict[
                    int, list[tuple[str, str | None, float | None, bool]]
                ] = {}
                for z in zonen_an_dieser_dswc:
                    kanal_zu_lockprofile_lokal.setdefault(z.ventil_kanal, []).append(
                        (z.zone_id, z.hahn_cluster, z.verbrauch_lpm, z.exklusiv),
                    )
                # T-0536: dieselben Zonen, aber mit AUFGELOESTER Geraete-ID
                # (Backward-Compat-Zonen ohne `ventil_geraet_id` haengen an
                # der primary DSWC) — daraus baut der Arbiter seine globale
                # Sicht.
                kanalprofile_global.update(
                    baue_kanalprofile(zonen_an_dieser_dswc, geraet_id=dswc_id),
                )
                sicherung = VentilSicherung(
                    client=client,
                    speicher=speicher,
                    ventil_geraet_id=dswc_id,
                    kanal_zu_valve_id=kanal_zu_valve_id,
                    kanal_zu_zone_lockprofile=kanal_zu_lockprofile_lokal,
                    cluster_max_lpm=cluster_max_lpm_lokal,
                    # T-0287: Frische-Gate scharf -- Auto-Loop giesst nicht
                    # auf veraltetem Live-Zustand (Schlaf/WS-Verlust).
                    max_daten_alter_minuten=AUTOMATIK_MAX_DATEN_ALTER_MINUTEN,
                )
                ventil_sicherungen[dswc_id] = sicherung
                await sicherung.startup_check()
                try:
                    n_recovered = await sicherung.recover_aus_db()
                    if n_recovered > 0:
                        logger.info(
                            "ventil_sicherung.recovered",
                            anzahl=n_recovered, geraet=dswc_id,
                        )
                except Exception:
                    logger.exception(
                        "ventil_sicherung.recover_fehler", geraet=dswc_id,
                    )
                if konfig.ventilsteuerung_aktiv:
                    logger.info(
                        "ventil_sicherung.aktiv",
                        geraet=dswc_id, zonen=len(zonen_an_dieser_dswc),
                    )
                else:
                    logger.info(
                        "ventil_sicherung.manuell_only",
                        geraet=dswc_id, zonen=len(zonen_an_dieser_dswc),
                        hinweis="ventilsteuerung_aktiv: false — Auto-Loop aus, "
                                "Live-Manuell-Endpoints aktiv",
                    )
            # T-0536: EIN Arbiter fuer alle Sicherungen. Er kennt die Kanaele
            # aller Geraete und liest zusaetzlich `ventil_ereignis` -- damit
            # sieht er auch Laeufe, die nicht von dieser Engine stammen
            # (Gardena-App, Cloud-Zeitplan, Abwesenheits-Cron).
            hahn_arbiter = HahnArbiter(
                speicher=speicher,
                kanalprofile=kanalprofile_global,
                cluster_max_lpm=cluster_max_lpm_lokal,
            )
            for dswc_id, sicherung in ventil_sicherungen.items():
                hahn_arbiter.registriere_sicherung(dswc_id, sicherung)
                sicherung.setze_hahn_arbiter(hahn_arbiter)
            logger.info(
                "hahn_arbiter.aktiv",
                geraete=sorted(ventil_sicherungen.keys()),
                kanaele=len(kanalprofile_global),
                exklusive_kanaele=sorted(
                    f"{gid}:{k}"
                    for (gid, k), p in kanalprofile_global.items() if p.exklusiv
                ),
                cluster=sorted(cluster_max_lpm_lokal.keys()),
            )
            # Primary fuer Backward-Compat-Callsites (legacy).
            ventil_sicherung = ventil_sicherungen.get(primary_geraet_id)
        else:
            logger.warning("ventil_sicherung.kein_ventil_gefunden")

        # Kanal -> [zone_ids] fuer Event-Expansion auf Geschwister-Zonen.
        # T-0203 (17.05.): Zone -> DSWC-UUID Mapping fuer Multi-Device-Routing.
        # Pro Zone die richtige VentilSicherung-Instanz finden.
        primary_dswc_id = (
            ventil_geraete_ids[0] if ventil_geraete_ids else None
        )
        zone_zu_dswc: dict[str, str] = {}
        for z in konfig.zonen:
            if z.ventil_kanal is None:
                continue
            gid = z.ventil_geraet_id or primary_dswc_id
            if gid:
                zone_zu_dswc[z.zone_id] = gid

        # Zonen am gleichen (DSWC, ventil_kanal) teilen sich denselben
        # Bewaesserungskreis (linear als Microdrip-Kette oder via Y-Stueck)
        # und erhalten gleichzeitig Wasser. Ein physisches Ventil-Event wird
        # darum auf alle diese Zonen expandiert, damit ML-Features und
        # Zeitreihen pro Zone vollstaendig sind. T-0252 (25.05.): die Map
        # MUSS nach `(geraet_id, kanal)` schluesseln — Helper-Funktion in
        # gardena_web_backfill.py.
        kanal_zonen_map = baue_zone_dswc_kanal_map(
            konfig.zonen, primary_geraet_id=primary_dswc_id,
        )
        zone_zu_kanal: dict[str, int] = {
            z.zone_id: z.ventil_kanal
            for z in konfig.zonen
            if z.ventil_kanal is not None
        }

        def _sicherung_fuer_zone(zone_id: str) -> "VentilSicherung | None":
            """T-0203: Routing-Helper. Liefert die VentilSicherung-Instanz
            fuer die DSWC der Zone, oder None wenn keine bekannt."""
            dswc_id = zone_zu_dswc.get(zone_id)
            if dswc_id:
                return ventil_sicherungen.get(dswc_id)
            return ventil_sicherung  # primary fallback

        # Ventil-Callback: Externe Ventilaktionen (App, Schedule) mitspeichern.
        # VentilSicherung-verwaltete Events werden von verarbeite_callback behandelt.
        async def _ventil_ereignis_speichern(ereignis: VentilEreignis) -> None:
            # T-0203: pro Zone die richtige Sicherung aufrufen.
            sicherung = _sicherung_fuer_zone(ereignis.zone_id)
            if sicherung:
                behandelt = await sicherung.verarbeite_callback(ereignis)
                if behandelt:
                    return

            # Event kommt mit zone_id aus valve_name-Matching (SMART_IRRIGATION_CONTROL)
            # oder direkt aus der Zone des WATER_CONTROL-Geraets.
            # Auf Geschwister-Zonen am gleichen (DSWC, Kanal) expandieren —
            # T-0252: ohne DSWC-Disambiguierung wuerden Zonen anderer DSWCs
            # mit identischer Kanal-Nummer falsche Phantom-Events bekommen.
            kanal = zone_zu_kanal.get(ereignis.zone_id)
            if kanal is not None:
                gid = zone_zu_dswc.get(ereignis.zone_id)
                zonen = kanal_zonen_map.get(
                    (gid, kanal), [ereignis.zone_id],
                )
            else:
                zonen = [ereignis.zone_id]

            for zid in zonen:
                erw_ereignis = VentilEreignis(
                    zeitstempel=ereignis.zeitstempel,
                    zone_id=zid,
                    ventil_id=ereignis.ventil_id,
                    aktion=ereignis.aktion,
                    dauer_sekunden=ereignis.dauer_sekunden,
                    ausloser=ereignis.ausloser,
                )
                await speicher.speichere_ventil_ereignis(erw_ereignis)
            logger.info(
                "ventil.ereignis_gespeichert",
                zonen=zonen,
                ventil_id=ereignis.ventil_id,
                aktion=ereignis.aktion.value,
                dauer_s=ereignis.dauer_sekunden,
            )
        client.registriere_ventil_callback(_ventil_ereignis_speichern)

        # T-0547: Der Replay-Guard (T-0288/T-0306) armiert nach >= 15 min
        # WS-Stille. Ein eigener Haupt-Puls (30 min) erzeugt diese Stille
        # SELBST -- das erste Event danach ist sein echter Cloud-Close, den
        # der Guard dann verwarf; der Watchdog schloss 30 s spaeter mit
        # `ausloser=watchdog` und aufgeblaehter Dauer (Realfall 20.08.:
        # 1831 s statt 1800 s, seit 17.08. bei ALLEN haupt-Pulsen). Mit
        # diesem Praedikat prueft der Guard gegen den eigenen Zustand statt
        # gegen die Uhr: Events fuer ein Ventil, das wir selbst offen fuehren,
        # sind kein Replay. Fremde Ventile bleiben voll geschuetzt.
        # T-0572: `haelt_ventil_offen` -> `lauf_plausibel_offen`. Der reine
        # Zustand genuegt nicht: schlaeft der Laptop mitten im Lauf, friert
        # der Watchdog-Timer ein, `_aktiv` bleibt stehen, und beim Aufwachen
        # laesst die T-0547-Ausnahme den replayten CLOSED durch -- mit der
        # ganzen Schlafphase als Giessdauer (15.09.: 11652 s statt 1797 s).
        # Die neue Auskunft beschraenkt die Ausnahme auf Laeufe, die laut
        # kommandierter Dauer ueberhaupt noch laufen koennen.
        client.setze_eigener_lauf_pruefer(
            baue_eigener_lauf_pruefer(ventil_sicherungen),
        )

        # T-0210-Folge: Reconnect-Sync-Callback registrieren. Vor jedem
        # WS-Start (Initial + Reconnect) vergleicht der Helper den
        # Gardena-Live-State mit offenen DB-OEFFNEN-Events und traegt
        # synthetische SCHLIESSEN nach, falls die Cloud sagt "zu" und
        # die DB "offen" -- Realfall 18.05. 09:38 Bambus-Lauf, dessen
        # SCHLIESSEN durch WS-Reconnect verloren ging.
        async def _reconnect_sync(live_state: dict[str, str]) -> int:
            # T-0449: Kanal->Valve-Mapping mitziehen. Der Hook feuert vor
            # JEDEM start_ws (Initial + jeder Reconnect) mit frischem
            # `update_devices`-State -- genau das Fenster, in dem ein beim
            # Start leer gebliebenes Mapping (T-0448) sich selbst heilen
            # kann. `setze_kanal_mapping` verwirft ein leeres Ergebnis,
            # ueberschreibt ein funktionierendes Mapping also nicht.
            for dswc_id, zuordnung in kanal_zuordnung_pro_dswc.items():
                sicherung_fuer_refresh = ventil_sicherungen.get(dswc_id)
                if sicherung_fuer_refresh is None:
                    continue
                try:
                    sicherung_fuer_refresh.setze_kanal_mapping(
                        client.baue_kanal_zu_valve_id(dswc_id, zuordnung),
                    )
                except Exception:
                    # Der Sync-Callback darf den WS-Start nicht verhindern.
                    logger.exception(
                        "ventil.mapping_refresh_fehler", geraet=dswc_id,
                    )
            return await orphan_close_job.sync_aus_live_state(live_state)

        client.setze_reconnect_sync_callback(_reconnect_sync)

        # WebSocket in Background-Task starten (blockiert sonst)
        ws_task = asyncio.create_task(client.starte_websocket())

        # T-0404: Ein `create_task`-Ergebnis, das niemand ansieht, verschwindet
        # lautlos. Am 10.07. endete das Log nach 13 fehlgeschlagenen
        # WS-Reconnects (DNS weg) ohne Traceback und ohne Exit-Grund; launchd
        # startete per KeepAlive neu, und dieser Neustart deployte still den
        # damaligen Working-Tree-Stand -- ein halber Deploy, den niemand
        # beauftragt hatte. Der Reconnect-Loop selbst laeuft mit
        # `max_retries=0`, also unbegrenzt; er kann nur ueber eine
        # unerwartete Exception enden. Genau die muss sichtbar sein.
        def _ws_task_ende(task: asyncio.Task) -> None:
            if task.cancelled():
                logger.info("gardena.websocket_task_abgebrochen")
                return
            exc = task.exception()
            if exc is not None:
                logger.critical(
                    "gardena.websocket_task_gestorben",
                    fehler=str(exc), typ=type(exc).__name__,
                    exc_info=exc,
                    hinweis="WS-Loop ist beendet -- keine Live-Events mehr. "
                            "Backend braucht Neustart.",
                )
            else:
                logger.critical(
                    "gardena.websocket_task_beendet_ohne_fehler",
                    hinweis="starte_websocket() ist zurueckgekehrt, obwohl der "
                            "Loop unbegrenzt laufen soll.",
                )

        ws_task.add_done_callback(_ws_task_ende)

        # FYTA-Client starten (falls konfiguriert)
        fyta_task = None
        fyta_client = None
        # T-0504: vor dem if-Zweig initialisieren -- der Entscheidungsloop
        # referenziert den Job unbedingt, und ein NameError dort liesse
        # alle nachfolgenden Job-Aufrufe still ausfallen
        # (fehlerpattern_job_nicht_in_loop_verdrahtet).
        fyta_luecken_job = None
        fyta_status_job = None
        if konfig.fyta and konfig.fyta.pflanzen:
            fyta_client = FytaClient(konfig.fyta)
            fyta_client.registriere_callback(verarbeiter.verarbeite)
            # T-0166: Dedup-Schwellen aus DB laden, bevor Polling startet.
            # Verhindert dass nach Restart die letzten 1-2 Tage erneut
            # geschrieben werden.
            await fyta_client.initialisiere_dedup_aus_db(speicher)
            # T-0167: Lueckenfuellung im Hintergrund. Faellt der
            # FytaClient-Polling-Loop kurz aus (Auth-Lag, DB-Lock,
            # Crash-Restart) -> die fehlenden Datenpunkte muessen sonst
            # manuell ueber das CLI nachgefuellt werden. Dieser Hook
            # zieht die letzten 7 Tage automatisch nach, mit Dedup.
            # Laeuft mit derselben Speicher-Instanz wie das Backend
            # (kein Single-Writer-Lock-Konflikt, anders als CLI-Backfill).
            #
            # T-0167b: Erst Token sicherstellen + warten, BEVOR das
            # parallele Polling startet. Vermeidet die Race Condition,
            # bei der beide Tasks gleichzeitig den abgelaufenen Cache-
            # Token sehen und beide refreshen wollen, aber der eine
            # weiter mit dem alten Wert arbeitet. Der `_login_lock`
            # serialisiert den Login, aber ein bereits geholter Token-
            # String in einem Async-Loop wird dadurch nicht aktualisiert.
            # Mit serieller Init: Hook bekommt zuerst frischen Token,
            # Polling startet erst danach.
            await fyta_client._stelle_token_sicher()
            from bewaesserung.fyta_backfill import backfill_lueckenfuellung
            asyncio.create_task(
                backfill_lueckenfuellung(
                    speicher, fyta_client, tage_zurueck=7,
                )
            )
            fyta_task = asyncio.create_task(fyta_client.starte_polling())
            # T-0504: Der Startup-Hook oben rettet nur, wer innerhalb von
            # sieben Tagen nach seinem Sync einen Neustart erwischt. Die
            # sechs Hub-fernen Sensoren syncen aber nur per Handy-Zufall
            # (Memory fyta_sensor_offline_hub_distanz) -- fuer sie ist
            # spaete Historie der Regelfall. Dieser Job zieht sie
            # periodisch nach, mit einem Fenster aus dem DB-Zustand.
            from bewaesserung.fyta_luecken_job import FytaLueckenJob
            fyta_luecken_job = FytaLueckenJob(speicher, fyta_client)
            # T-0527: Geraete-Zustand (Akkustand, Online-Flag, Firmware) als
            # Zeitreihe mitschreiben. Die API kennt ihn nur als
            # Momentaufnahme; ohne diesen Job ist nach einem Ausfall nicht
            # mehr rekonstruierbar, wie der Akku davor lief.
            from bewaesserung.fyta_status_job import FytaStatusJob
            fyta_status_job = FytaStatusJob(speicher, fyta_client)
            logger.info(
                "fyta.gestartet",
                pflanzen=len(konfig.fyta.pflanzen),
                intervall_s=konfig.fyta.poll_intervall_sekunden,
            )
        else:
            logger.info("fyta.nicht_konfiguriert")

        # T-0050b: Plant-Optimum-Cache aus FYTA (24h-Intervall).
        # Der Client wird geteilt — getrennter httpx.AsyncClient pro
        # Request, keine Shared-State-Kollision mit dem Polling.
        from bewaesserung.plant_optimum_job import PlantOptimumJob
        plant_optimum_job = PlantOptimumJob(
            speicher, konfig, fyta_client,
            intervall_stunden=konfig.plant_optimum_intervall_stunden,
        )

        # Kurz warten bis initiale Sensordaten reinkommen
        await asyncio.sleep(5)

        # ML-Vorhersage-Service laden (optional)
        ml_service = None
        try:
            from bewaesserung.ml.vorhersage import MLVorhersageService
            from bewaesserung.konfig import ML_DATEN_PFAD
            ml_service = MLVorhersageService(modell_verzeichnis=ML_DATEN_PFAD)
            if ml_service.lade_modelle():
                logger.info("ml.geladen", horizonte=ml_service.status().horizonte)
                # T-0082: Cluster-Mapping nur aktivieren, wenn
                # `cluster_strategie=pro_zone`. Bei `global` (Default,
                # Backward-Compat) bleibt `_zone_zu_cluster` leer und
                # alle Inferenzen gehen weiter durch das Legacy-Modell —
                # auch wenn `feuchte/<cluster>/`-Subdirs existieren.
                # Damit ist der Schalter „Pro-Zone-Modelle aktiv?"
                # einzig die Konfig-Zeile.
                if konfig.ml_retrain.cluster_strategie == "pro_zone":
                    zone_zu_cluster = {
                        z.zone_id: (z.cluster_id or z.zone_id)
                        for z in konfig.zonen
                    }
                    ml_service.setze_cluster_zuordnung(zone_zu_cluster)
                    logger.info(
                        "ml.cluster_strategie_aktiv",
                        anzahl_zonen=len(zone_zu_cluster),
                    )
            else:
                ml_service = None
                logger.info("ml.nicht_verfuegbar")
        except ImportError:
            logger.info("ml.nicht_installiert")

        # T-0105: ML-Service jetzt am Motor nachreichen — die kausale
        # Empfehlung nutzt ihn fuer die Decay-Prognose (Niederschlag/ET0
        # als Features statt der vereinfachten Heuristik). Faellt auf
        # Heuristik zurueck wenn None oder nicht verfuegbar.
        motor.setze_ml_vorhersage_service(ml_service)

        # T-0564: derselbe Service auch am Retrain-Job. `lade_modelle()`
        # lief bisher NUR hier beim Start -- nach einem Deploy im laufenden
        # Betrieb rechnete also weiter der alte Booster, waehrend
        # `_modell_version()` den frischen Symlink las und das Drift-Log
        # damit die neue Version zu alten Prognosen schrieb. Der
        # Response-Pfad laedt seit jeher nach (`lade_zone(force=True)`).
        if ml_retrain_job is not None:
            ml_retrain_job.setze_vorhersage_service(ml_service)

        # T-0122: Empfehlungs-Audit-Job — pro Zone Snapshot + Eval gegen
        # realen Sensor-Wert nach 6 / 24 h. Nur fuer Zonen mit Ventil-
        # Kanal sinnvoll (FYTA-only-Zonen haben keine kausale
        # Bewaesserungs-Empfehlung in dem Sinne).
        from bewaesserung.empfehlungs_audit_job import EmpfehlungsAuditJob
        empfehlungs_audit_job: EmpfehlungsAuditJob | None = None
        try:
            empfehlungs_audit_job = EmpfehlungsAuditJob(
                speicher=speicher,
                motor=motor,
                zone_ids=[z.zone_id for z in konfig.zonen],
                # T-0270: Physik-Diagnose mitloggen (Read-only).
                konfig=konfig,
                wetter_manager=wetter_manager,
            )
            logger.info(
                "empfehlungs_audit.aktiv",
                zonen=len(konfig.zonen),
            )
        except Exception:
            logger.exception("empfehlungs_audit.init_fehler")

        # T-0132 (H-8): Endpoint-Health-Check fuer DHS + FYTA. 1x/24h.
        # Watchdog konsumiert den Status (Trigger C) und feuert iMessage
        # bei laenger anhaltender Schema-Drift.
        from bewaesserung.endpoint_health import EndpointHealthJob
        endpoint_health_job: EndpointHealthJob | None = None
        if konfig.endpoint_health.aktiv:
            try:
                # Probe-Sensor: erstes Sensor-zu-Zone-Mapping aus dem
                # bestehenden DHS-Backfill (sensor_zu_zone). Wenn nichts
                # konfiguriert ist, kein DHS-Probe; FYTA bleibt davon
                # unabhaengig.
                probe_sensor_uuid = None
                probe_location = None
                if sensor_dhs_backfill_job is not None:
                    probe_sensor_uuid = next(
                        iter(sensor_dhs_backfill_job._sensor_zu_zone), None,
                    )
                    probe_location = sensor_dhs_backfill_job._location_id
                endpoint_health_job = EndpointHealthJob(
                    speicher=speicher,
                    intervall_stunden=konfig.endpoint_health.intervall_stunden,
                    gardena_auth=customer_auth if probe_sensor_uuid else None,
                    gardena_location_id=probe_location,
                    gardena_probe_sensor_uuid=probe_sensor_uuid,
                    fyta_client=fyta_client,
                )
                logger.info(
                    "endpoint_health.aktiv",
                    dhs=probe_sensor_uuid is not None,
                    fyta=fyta_client is not None,
                )
            except Exception:
                logger.exception("endpoint_health.init_fehler")

        # T-0126 (H-2): Watchdog mit iMessage-Push bei akut-3-Tage / Husqvarna-
        # Block. Opt-In via konfig.watchdog.aktiv. FYTA-Zonen werden vom
        # Husqvarna-Trigger ausgeklammert (sonst kaschiert die FYTA-Pause die
        # Cadence-Messung der Gardena-Flotte oder umgekehrt).
        from bewaesserung.watchdog import WatchdogJob
        watchdog_job: WatchdogJob | None = None
        try:
            fyta_zone_ids = {
                p.zone_id for p in (konfig.fyta.pflanzen if konfig.fyta else [])
            }
            gardena_zone_ids = [
                z.zone_id for z in konfig.zonen if z.zone_id not in fyta_zone_ids
            ]
            watchdog_empfaenger_default = os.environ.get("IMESSAGE_EMPFAENGER", "")
            watchdog_job = WatchdogJob(
                speicher=speicher,
                benachrichtiger=benachrichtiger,
                konfig=konfig.watchdog,
                zonen=konfig.zonen,
                gardena_zone_ids=gardena_zone_ids,
                empfaenger_default=watchdog_empfaenger_default,
                # T-0256 Phase 1: Shadow-Empfehlungs-Push braucht den
                # gleichen Motor wie EmpfehlungsAuditJob (Konvergenz
                # mit T-0231).
                motor=motor,
                # T-0426: Regime-Fenster durchreichen, damit der Watchdog
                # nicht "bitte giessen" meldet fuer Zonen, bei denen genau
                # dieser Zustand dokumentiert erwartet wird.
                ausschluss_fenster=konfig.ml_ausschluss_fenster,
                # T-0537 Punkt 3: Wartezeit-Wache am Haupthahn. Der Arbiter
                # fuehrt den Zustand, der Watchdog ist sein Leser.
                hahn_arbiter=hahn_arbiter,
            )
            logger.info(
                "watchdog.aktiv" if konfig.watchdog.aktiv else "watchdog.inaktiv",
                gardena_zonen=len(gardena_zone_ids),
                fyta_zonen=len(fyta_zone_ids),
            )
        except Exception:
            logger.exception("watchdog.init_fehler")

        # API-Server konfigurieren und starten
        # T-0203 (17.05.): Multi-DSWC-Routing — `ventil_sicherungen`-Dict
        # zusaetzlich zur Singular-Sicherung uebergeben.
        konfiguriere_api(
            speicher, konfig, motor, verarbeiter, wetter_manager, ml_service,
            ventil_sicherung, ml_retrain_job=ml_retrain_job,
            ventil_sicherungen=ventil_sicherungen,
            # T-0205: PATCH/Bulk-Endpoints triggern nach Flip auf
            # IGNORIERT einen Heuristik-Rescan in der Karenz-Periode.
            sensor_backfill_job=sensor_backfill_job,
            # T-0108: damit /api/ml/status auch Crashes dieser beiden
            # Jobs zeigt (vorher nur im Log sichtbar).
            ml_response_retrain_job=ml_response_retrain_job,
            kalibrations_job=kalibrations_job,
            # T-0181: Skalen-Mapping-Fit-Job-Status in /api/ml/status.
            skalen_mapping_fit_job=skalen_mapping_fit_job,
            # Hybrid Stufe 1: Physik-Fit-Job-Status in /api/ml/status.
            k_basis_fit_job=k_basis_fit_job,
            # T-0292 Stufe 2: Plateau-Wirkungs-Fit-Job-Status.
            wirkung_fit_job=wirkung_fit_job,
            # T-0297: sofortiges Schliessen verwaister "ohne Wirkung"-
            # Warnungen nach ignoriert-Flip (statt erst beim Detektor-Tick).
            leck_detektor=leck_detektor,
        )

        # T-0116: Pre-Soak-Recovery — laufende Sequenzen aus DB
        # fortsetzen (z. B. Pause-Phase ueber Backend-Restart hinweg).
        if ventil_sicherung is not None:
            from bewaesserung.api_server import _pre_soak_manager, _pre_soak_managers
            managers = list(_pre_soak_managers.values()) or (
                [_pre_soak_manager] if _pre_soak_manager is not None else []
            )
            for manager in managers:
                try:
                    n_recovered = await manager.recover_aus_db()
                    if n_recovered > 0:
                        logger.info(
                            "pre_soak.recovered", anzahl=n_recovered,
                        )
                except Exception:
                    logger.exception("pre_soak.recover_fehler")

        api_config = uvicorn.Config(api_app, host="127.0.0.1", port=8090, log_level="warning")
        api_server = uvicorn.Server(api_config)
        api_task = asyncio.create_task(api_server.serve())

        logger.info("bereit", api=API_URL, dashboard=API_URL, modus=start_modus)

        # Signal-Handler fuer sauberes Beenden
        stop_event = asyncio.Event()
        laufstatus: dict[str, datetime | None] = {
            "letzter_entscheidungszyklus": None,
            "naechster_entscheidungszyklus": (
                datetime.now() + timedelta(seconds=5)
            ),
        }

        def _signal_handler():
            logger.info("beenden", signal="SIGINT/SIGTERM")
            stop_event.set()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

        laufstatus_task = asyncio.create_task(
            _laufstatus_heartbeat(stop_event, laufstatus, start_modus)
        )

        # T-0346: Feiner Pre-Soak-Ticker (entkoppelt vom 5-min-Entscheidungsloop)
        # -> Phasenwechsel + Hauptdose feuern praezise an der Wall-Clock-Grenze.
        pre_soak_tick_task = asyncio.create_task(
            _pre_soak_tick_loop(stop_event)
        )

        # Entscheidungsloop starten (parallel zum WebSocket)
        entscheidungs_task = asyncio.create_task(
            _entscheidungsloop(
                motor, konfig, benachrichtiger, verarbeiter, stop_event,
                wetter_manager=wetter_manager, speicher=speicher,
                health_monitor=health_monitor,
                leck_detektor=leck_detektor,
                fyta_sprung_detektor=fyta_sprung_detektor,
                regen_ensemble_job=regen_ensemble_job,
                wetter_modell_job=wetter_modell_job,
                station_messung_job=station_messung_job,
                wasserbilanz_job=wasserbilanz_job,
                wetter_archiv_job=wetter_archiv_job,
                aquabloom_job=aquabloom_job,
                gardena_web_backfill_job=gardena_web_backfill_job,
                gardena_web_backfill_jobs=gardena_web_backfill_jobs,
                sensor_backfill_job=sensor_backfill_job,
                sensor_dhs_backfill_job=sensor_dhs_backfill_job,
                fyta_luecken_job=fyta_luecken_job,
                fyta_status_job=fyta_status_job,
                orphan_close_job=orphan_close_job,
                auto_ignorieren_job=auto_ignorieren_job,
                gardena_client=client,
                ventil_sicherung=ventil_sicherung,
                ventil_sicherungen=ventil_sicherungen,
                laufstatus=laufstatus,
                empfehlungs_audit_job=empfehlungs_audit_job,
                watchdog_job=watchdog_job,
                endpoint_health_job=endpoint_health_job,
            )
        )

        # T-0403: Wartungs- und Analysejobs in einem eigenen Takt, damit ein
        # 24-Minuten-Retrain nicht den naechsten Entscheidungszyklus schiebt.
        wartungs_task = asyncio.create_task(
            _wartungs_loop(
                stop_event,
                backup_job=backup_job,
                ml_drift_job=ml_drift_job,
                ml_retrain_job=ml_retrain_job,
                ml_response_retrain_job=ml_response_retrain_job,
                wochen_report_job=wochen_report_job,
                kalibrations_job=kalibrations_job,
                skalen_mapping_fit_job=skalen_mapping_fit_job,
                k_basis_fit_job=k_basis_fit_job,
                wirkung_fit_job=wirkung_fit_job,
                plant_optimum_job=plant_optimum_job,
                log_rotation_job=log_rotation_job,
            )
        )

        await stop_event.wait()

        # Sauberes Beenden: Tasks canceln. Aktive Bewaesserungen werden
        # NICHT geschlossen — sie laufen via Cloud-Override weiter und
        # werden beim naechsten Backend-Start aus dem persistierten State
        # (T-0115/T-0116) wieder uebernommen.
        # Der frueher hier stehende notfall_stopp()-Aufruf hat aktive
        # User-Bewaesserungen abgebrochen, sobald jemand Ctrl-C drueckte
        # — vor T-0115 alternativlos (Watchdog-Timer war eh weg), heute
        # falsch (Recovery laedt den State zuverlaessig nach).
        logger.info("shutdown.tasks_beenden")
        api_server.should_exit = True
        if fyta_task and fyta_client:
            fyta_client.stoppe()

        tasks_zu_beenden = [
            entscheidungs_task, ws_task, laufstatus_task, pre_soak_tick_task,
            wartungs_task,
        ]
        if fyta_task:
            tasks_zu_beenden.append(fyta_task)

        for task in tasks_zu_beenden:
            task.cancel()

        # Auf tatsaechliches Ende warten (max 10s)
        await asyncio.gather(*tasks_zu_beenden, return_exceptions=True)
        try:
            await asyncio.wait_for(api_task, timeout=10)
        except asyncio.TimeoutError:
            logger.warning("shutdown.api_timeout")
            api_task.cancel()
            await asyncio.gather(api_task, return_exceptions=True)
        logger.info("shutdown.tasks_beendet")

    except KeyboardInterrupt:
        pass
    finally:
        logger.info("aufraumen")
        await client.trennen()
        await speicher.schliessen()
        logger.info("beendet")


def _baue_auto_loop_kanaele(
    zonen: list[ZonenKonfig],
) -> dict[tuple[str | None, int], list[ZonenKonfig]]:
    """Gruppiert die vom Auto-Loop autonom schaltbaren Zonen pro (geraet, kanal).

    T-0203: Schluessel ist (ventil_geraet_id, kanal) statt nur Kanal, weil bei
    Multi-DSWC zwei Zonen den gleichen Kanal-Wert an unterschiedlichen Geraeten
    haben koennen. `geraet_id=None` -> primary-Verhalten (Backward-Compat).

    T-0334: Eine Zone wird NUR aufgenommen, wenn `modus=AUTOMATIK`,
    `ventil_kanal` gesetzt UND `auto_loop_opt_in=true` (Pro-Strang-
    Scharfschaltung). Das isoliert, WELCHE Zonen der Auto-Loop autonom giesst
    (z.B. nur der Bambus-Strang) -- waldblumenhain/hecke bleiben automatik-
    Shadow, obwohl sie modus=AUTOMATIK + Kanal haben. Reine Funktion, damit die
    Opt-out-Garantie ohne die Loop-Infrastruktur testbar ist.
    """
    kanaele: dict[tuple[str | None, int], list[ZonenKonfig]] = {}
    for zone in zonen:
        # T-0370: Bedingung zentral in modelle.ist_auto_loop_zone -- gleiche
        # Wahrheit wie das API-Feld `autonom_scharf` (_baue_zone_dict).
        if ist_auto_loop_zone(zone):
            schluessel = (zone.ventil_geraet_id, zone.ventil_kanal)
            kanaele.setdefault(schluessel, []).append(zone)
    return kanaele


def _pre_soak_policy_aktiv(zone: ZonenKonfig) -> bool:
    """T-0336: Soll der Auto-Loop diese Zone als Pre-Soak giessen (statt
    Einzellauf)? `pre_soak_modus="immer"` + gesetzte `pre_soak_min`. Rein/
    testbar."""
    return zone.pre_soak_modus == "immer" and bool(zone.pre_soak_min)


def _melde_takt_luecke(loop_name: str, warte_start: float, intervall_s: float) -> float:
    """T-0522: meldet, wenn ein Loop-Takt laenger gedauert hat als er sollte.

    **Warum Wanduhr und nicht `perf_counter`:** die asyncio-Uhr steht
    waehrend eines System-Schlafs still, die Wanduhr nicht. Ein
    `wait_for(timeout=300)` kehrt nach einem Schlaf also erst nach viel mehr
    als 300 s Wanduhr zurueck. Genau diese Differenz ist die Zeit, in der die
    Anlage NICHT entschieden hat -- und sie ist bisher spurlos verschwunden,
    weil der Loop nach dem Aufwachen einfach weitertaktet.

    Gemessen wird ausschliesslich das WARTEN, nicht der Zyklus davor. Sonst
    zaehlte ein langsamer Zyklus (Retrain-Kontention, T-0518: 216-408 s) als
    Luecke, und die beiden Ursachen waeren im Log nicht mehr trennbar.

    Schwelle sind zwei Takte: ein einzelner verspaeteter Tick ist normaler
    Scheduling-Jitter, ab dem zweiten fehlt nachweislich eine Entscheidung.

    Gibt die gewartete Zeit zurueck, damit Aufrufer sie in den Heartbeat
    legen koennen.
    """
    gewartet_s = time.time() - warte_start
    if gewartet_s > 2 * intervall_s:
        logger.warning(
            f"{loop_name}.takt_luecke",
            gewartet_s=round(gewartet_s, 1),
            erwartet_s=intervall_s,
            ausgefallene_takte=int(gewartet_s // intervall_s) - 1,
        )
    return gewartet_s


class _PhasenTimer:
    """T-0458: misst die Dauer der Abschnitte eines Entscheidungszyklus.

    Der Loop blockiert den Event-Loop 54-110 s pro 5-Minuten-Takt und loggt
    dabei ueber die gesamte Strecke keine einzige Zeile -- die Blockade war
    deshalb nur auf den Bereich "irgendwo zwischen Zonen-Loop und Heartbeat"
    eingrenzbar. `mark()` schliesst den jeweils laufenden Abschnitt ab; der
    Heartbeat zeigt die teuersten.

    Bewusst nur `perf_counter`-Differenzen und ein Dict: das Ding laeuft in
    jedem Zyklus mit und darf selbst nichts kosten.
    """

    def __init__(self) -> None:
        self._letzter = time.perf_counter()
        self.dauern: dict[str, float] = {}

    def mark(self, name: str) -> None:
        jetzt = time.perf_counter()
        # Additiv, damit Abschnitte in Schleifen (Wetter-Standorte,
        # DHS-Jobs pro Geraet) nicht ihren jeweils letzten Durchlauf melden.
        self.dauern[name] = self.dauern.get(name, 0.0) + (jetzt - self._letzter)
        self._letzter = jetzt

    def top(self, anzahl: int = 6, mindest_s: float = 0.5) -> dict[str, float]:
        """Die teuersten Abschnitte. Alles unter `mindest_s` ist Rauschen
        und wuerde die Heartbeat-Zeile nur unlesbar machen."""
        sortiert = sorted(self.dauern.items(), key=lambda p: p[1], reverse=True)
        return {
            name: round(dauer, 2)
            for name, dauer in sortiert[:anzahl]
            if dauer >= mindest_s
        }


async def _wartungs_loop(
    stop_event: asyncio.Event,
    backup_job: BackupJob | None = None,
    ml_drift_job: MlDriftJob | None = None,
    ml_retrain_job: MlRetrainJob | None = None,
    ml_response_retrain_job: MlResponseRetrainJob | None = None,
    wochen_report_job: "WochenReportJob | None" = None,
    kalibrations_job: "KalibrationsJob | None" = None,
    skalen_mapping_fit_job: "Any | None" = None,
    k_basis_fit_job: "Any | None" = None,
    wirkung_fit_job: "Any | None" = None,
    plant_optimum_job: "PlantOptimumJob | None" = None,
    log_rotation_job: "Any | None" = None,
) -> None:
    """T-0403: Wartungs- und Analysejobs, entkoppelt vom Entscheidungstakt.

    **Warum es diesen Loop gibt (Realfall 02.08.2026).** Der Entscheidungsloop
    arbeitet seine rund dreissig Jobs streng nacheinander ab und beginnt erst
    danach von vorn. Die Kanalpruefung steht an Position 2, der ML-Retrain an
    Position 14. Ein Retrain von 23,7 Minuten verzoegert also nicht die
    Entscheidungen seines eigenen Durchlaufs, sondern schiebt den START des
    naechsten um seine volle Laufzeit. Gemessen am 02.08.:

        laufzeit_s = 1444.17   phasen = {'ml_retrain': 1421.06, ...}

    Der Hecke-Lead fiel um 12:01 unter die Schwelle, die letzte Pruefung war
    von 11:39, die naechste kam 12:08 -- die Zone stand 24 Minuten unter
    Schwelle, ohne dass irgendetwas hinsah. Alle Blocker waren frei. Fuer den
    Max-Stop ist das die gefaehrlichere Richtung: dort geht es um das
    Abschalten eines LAUFENDEN Vorgangs, und der laeuft in 24 Minuten weiter.

    **Was hierher gehoert und was nicht.** Hier laufen nur Jobs ohne Bezug zum
    aktuellen Ventil- oder Sensorzustand: Backup, ML-Drift/Retrain, Report,
    Kalibrier- und Fit-Jobs, Plant-Optimum-Cache. Bewusst NICHT gewandert sind
    die Backfill-Jobs (DHS, Sensor, FYTA-Luecken), Orphan-Close und
    Auto-Ignorieren -- sie liefern den Zustand, auf dem entschieden wird, und
    Orphan-Close ist ein Sicherheitspfad. Wer hier einen Job ergaenzt, prueft
    zuerst diese Frage, nicht die Laufzeit.

    Wake-sicher wie die Geschwister-Loops: `asyncio.wait_for` auf das
    `stop_event` statt `asyncio.sleep`, damit ein Laptop-Schlaf den Takt nicht
    einfriert ([[fehlerpattern_asyncio_timer_laptop_sleep]]).
    """
    while not stop_event.is_set():
        zyklus_start = time.perf_counter()
        phasen = _PhasenTimer()
        try:
            # DB-Backup (T-0043) — Intervall-Gate (Default 24 h)
            if backup_job:
                try:
                    await backup_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("backup.fehler")
            phasen.mark("backup")

            # T-0047: ML-Drift evaluieren — Intervall-Gate (Default 1 h)
            if ml_drift_job:
                try:
                    await ml_drift_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("ml.drift_job.fehler")
            phasen.mark("ml_drift")

            # T-0048: ML-Retrain mit Deploy-Gate (Default 7 Tage, Opt-In)
            if ml_retrain_job:
                try:
                    await ml_retrain_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("ml.retrain_job.fehler")
            phasen.mark("ml_retrain")

            # T-0065: Response-Modell-Retrain (Opt-In via Konfig)
            if ml_response_retrain_job:
                try:
                    await ml_response_retrain_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("ml.response_retrain_job.fehler")
            phasen.mark("ml_response_retrain")

            # T-0038: Wochen-Report per iMessage (Opt-In via Konfig)
            if wochen_report_job:
                try:
                    await wochen_report_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("wochen_report.fehler")
            phasen.mark("wochen_report")

            # T-0063: Automatische Kalibrier-Erkennung (6h-Intervall)
            if kalibrations_job:
                try:
                    await kalibrations_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("kalibrierung.job_fehler")
            phasen.mark("kalibrierung")

            # T-0181: Skalen-Mapping-Fit (24h-Intervall, Default).
            if skalen_mapping_fit_job:
                try:
                    await skalen_mapping_fit_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("skalen_mapping.job_fehler")
            phasen.mark("skalen_mapping")

            # Hybrid Stufe 1: Physik-Trocknungs-Fit (24h-Intervall).
            if k_basis_fit_job:
                try:
                    await k_basis_fit_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("k_basis_fit.job_fehler")
            phasen.mark("k_basis_fit")

            # T-0292 Stufe 2: Plateau-Wirkungs-Fit (24h-Intervall).
            if wirkung_fit_job:
                try:
                    await wirkung_fit_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("wirkung_fit.job_fehler")
            phasen.mark("wirkung_fit")

            # T-0050b: FYTA-Plant-Optimum-Cache (24h-Intervall)
            if plant_optimum_job:
                try:
                    await plant_optimum_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("plant_optimum.job_fehler")
            phasen.mark("plant_optimum")

            # T-0480: Service-Logs rotieren (stuendlicher Groessencheck).
            if log_rotation_job:
                try:
                    await log_rotation_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("log_rotation.job_fehler")
            phasen.mark("log_rotation")

            # Eigener Heartbeat: ohne ihn waere die teuerste Strecke des
            # Prozesses ab jetzt unbeobachtet -- genau der Zustand, den
            # T-0458 im Entscheidungsloop beseitigt hat.
            logger.info(
                "wartungsloop.heartbeat",
                laufzeit_s=round(time.perf_counter() - zyklus_start, 2),
                phasen=phasen.top() or None,
            )
        except asyncio.CancelledError:
            raise
        except sqlite3.OperationalError as fehler:
            # T-0403 mit T-0210: derselbe Lock-Backoff wie im
            # Entscheidungsloop, hier in der Gegenrichtung.
            #
            # **Das ist eine Folge dieses Umbaus, kein Erbstueck.** Bis 05.08.
            # liefen ML-Drift, Backup und die Fit-Jobs im SELBEN Loop wie die
            # Entscheidung, also nacheinander -- zwischen ihnen konnte es
            # keine Lock-Kontention geben. Jetzt laufen sie echt parallel, und
            # damit ist genau die Konstellation vom 18.05.2026 wieder da:
            # langer Lese-Burst des Drift-Scans gegen parallele Schreiber,
            # `database is locked` ([[fehlerpattern_sqlite_lock_loop]]).
            # Ohne diesen Zweig liefe der Wartungsloop im Folgezyklus sofort
            # in denselben Konflikt.
            text = str(fehler).lower()
            if "locked" in text or "busy" in text:
                logger.warning("wartungsloop.db_lock_backoff", fehler=str(fehler))
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=30)
                    return
                except asyncio.TimeoutError:
                    continue
            logger.exception("wartungsloop.fehler")
        except Exception:
            logger.exception("wartungsloop.fehler")

        warte_start = time.time()
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=WARTUNGSINTERVALL_SEKUNDEN
            )
            return  # stop_event gesetzt -> sauber beenden
        except asyncio.TimeoutError:
            pass
        _melde_takt_luecke(
            "wartungsloop", warte_start, WARTUNGSINTERVALL_SEKUNDEN
        )


async def _entscheidungsloop(
    motor: Entscheidungsmotor,
    konfig: GesamtKonfig,
    benachrichtiger: Benachrichtiger,
    verarbeiter: SensorDatenVerarbeiter,
    stop_event: asyncio.Event,
    wetter_manager: WetterManager | None = None,
    speicher: Speicher | None = None,
    health_monitor: SensorHealthMonitor | None = None,
    leck_detektor: LeckDetektor | None = None,
    fyta_sprung_detektor: FytaSprungDetektor | None = None,
    regen_ensemble_job: RegenEnsembleJob | None = None,
    wetter_modell_job: WetterModellJob | None = None,
    station_messung_job: StationMessungJob | None = None,
    wasserbilanz_job: WasserbilanzJob | None = None,
    wetter_archiv_job: WetterArchivJob | None = None,
    # T-0403: Backup, ML-Drift/Retrain, Report, Kalibrier- und Fit-Jobs sowie
    # Plant-Optimum sind Parameter von `_wartungs_loop`, nicht mehr von hier.
    aquabloom_job: "Any | None" = None,
    gardena_web_backfill_job: GardenaWebBackfillJob | None = None,
    gardena_web_backfill_jobs: list[GardenaWebBackfillJob] | None = None,
    sensor_backfill_job: SensorBackfillJob | None = None,
    sensor_dhs_backfill_job: SensorDhsBackfillJob | None = None,
    fyta_luecken_job: "Any | None" = None,
    fyta_status_job: "Any | None" = None,
    orphan_close_job: "Any | None" = None,
    auto_ignorieren_job: "Any | None" = None,
    gardena_client: GardenaClient | None = None,
    ventil_sicherung: VentilSicherung | None = None,
    ventil_sicherungen: dict[str, VentilSicherung] | None = None,
    laufstatus: dict[str, datetime | None] | None = None,
    empfehlungs_audit_job: "Any | None" = None,
    watchdog_job: "Any | None" = None,
    endpoint_health_job: "Any | None" = None,
) -> None:
    """Periodischer Entscheidungsloop — prueft alle Zonen alle 5 Minuten.

    Automatik-Zonen mit Ventilkanal: Min-Start-/Max-Stop-Entscheidung pro Kanal,
    Ausfuehrung ueber VentilSicherung.
    Monitoring-Zonen: Nur Logging und Benachrichtigungen.
    """
    # Erste Pruefung nach 5s (Sensordaten sollten schon da sein).
    # T-0556b: als Konstante, damit ein Test die Schleife ueberhaupt fahren
    # kann -- vorher war die Verdrahtung des Heartbeats nicht pruefbar.
    try:
        await asyncio.wait_for(
            stop_event.wait(), timeout=ERSTE_PRUEFUNG_VERZOEGERUNG_S,
        )
        return
    except asyncio.TimeoutError:
        pass

    # Trackt bereits gespeicherte Forecast-Zeitstempel um Duplikate zu vermeiden.
    # Dict statt Set: key=(standort, abfrage_ts), value=Einfuege-Zaehler fuer LRU-Eviction.
    letzter_wetter_ts: dict[tuple[str, str], int] = {}
    wetter_zaehler = 0
    # T-0055-B4: letzter Metriken-Snapshot fuer Heartbeat-Diff
    letzter_ws_snapshot: dict[str, dict[str, int]] = {}

    while not stop_event.is_set():
        zyklus_start = time.perf_counter()
        phasen = _PhasenTimer()
        try:
            logger.info("entscheidungsloop.start")

            # T-0336: Pre-Soak-State-Machine treiben. FLAG-UNABHAENGIG -- auch
            # manuelle Pre-Soaks laufen unabhaengig von ventilsteuerung_aktiv.
            # Der wake-sichere Loop-Tick (asyncio.wait_for-getimt) zieht faellige
            # Phasen-Uebergaenge per Wall-Clock nach -> sleep-fest (kein
            # einfrierender asyncio.sleep mehr).
            from bewaesserung.api_server import (
                _pre_soak_manager as _ps_primary,
                _pre_soak_managers as _ps_managers_dict,
            )
            _ps_managers = list(_ps_managers_dict.values()) or (
                [_ps_primary] if _ps_primary is not None else []
            )
            _jetzt_tick = datetime.now()
            for _mgr in _ps_managers:
                try:
                    await _mgr.tick(_jetzt_tick)
                except Exception:
                    logger.exception("entscheidungsloop.pre_soak_tick_fehler")
            phasen.mark("pre_soak_tick")

            def _pre_soak_lauf_fuer_kanal(
                zonen_des_kanals: list[ZonenKonfig],
            ):
                """Re-Entrancy: findet einen laufenden Pre-Soak des Kanals.

                Liefert (manager, lauf), damit offene Puls-/Haupt-Phasen den
                Max-Stop ueber den Pre-Soak-Manager ausloesen koennen.
                """
                for z in zonen_des_kanals:
                    for mgr in _ps_managers:
                        lauf = mgr.laufender_lauf(z.zone_id)
                        if lauf is not None and mgr.ist_aktiv(z.zone_id):
                            return mgr, lauf
                return None

            # Automatik-Zonen mit Ventilkanal -> Kanal-Entscheidung.
            # Gruppierung pro (ventil_geraet_id, kanal) + Pro-Strang-Opt-In-
            # Filter: siehe _baue_auto_loop_kanaele (rein, testbar).
            kanaele = _baue_auto_loop_kanaele(konfig.zonen)

            # T-0110: Auto-Loop laeuft nur wenn ventilsteuerung_aktiv=true.
            # Manuelle Aktionen ueber API-Endpoints koennen unabhaengig
            # die VentilSicherung nutzen.
            _sicherungen_dict: dict[str, VentilSicherung] = ventil_sicherungen or {}
            auto_loop_aktiv = bool(konfig.ventilsteuerung_aktiv) and bool(_sicherungen_dict)
            # Aktive Kanaele zuerst (Stop-Pruefung — kein Lock-Konflikt).
            # Inaktive Kanaele anschliessend mit Akut-Sortierung (T-0152).
            inaktive_kanaele: list[tuple[tuple[str | None, int], list[ZonenKonfig]]] = []
            primary_dswc_for_loop = (
                next(iter(_sicherungen_dict)) if _sicherungen_dict else None
            )

            def _sicherung_fuer_kanal(dswc: str | None) -> VentilSicherung | None:
                gid = dswc or primary_dswc_for_loop
                return _sicherungen_dict.get(gid) if gid else None

            def _ps_manager_fuer_dswc(dswc: str | None):
                gid = dswc or primary_dswc_for_loop
                if gid and gid in _ps_managers_dict:
                    return _ps_managers_dict[gid]
                return _ps_primary

            async def _lauf_start_fuer_kanal(
                dswc: str | None, kanal_nr: int,
            ) -> datetime | None:
                """T-0445: Startzeit des laufenden Vorgangs auf (Geraet, Kanal).

                Quelle ist `live_lauf_state` -- die einzige Stelle, die den
                Start eines offenen Ventil-Vorgangs persistiert und damit
                restart-fest haelt. Ohne Speicher, ohne Geraet oder ohne Zeile
                None -> Frische-Pruefung inaktiv, Verhalten wie vor T-0445.
                """
                gid = dswc or primary_dswc_for_loop
                if speicher is None or not gid:
                    return None
                try:
                    states = await speicher.hole_live_lauf_states(gid)
                except Exception:
                    logger.exception(
                        "entscheidungsloop.lauf_start_lookup_fehler",
                        kanal=kanal_nr,
                    )
                    return None
                for state in states:
                    if state.get("kanal") == kanal_nr:
                        return state.get("gestartet_am")
                return None

            for (dswc, kanal), zonen in kanaele.items():
                if not auto_loop_aktiv:
                    continue
                kanal_sicherung = _sicherung_fuer_kanal(dswc)
                pre_soak_lauf = _pre_soak_lauf_fuer_kanal(zonen)
                # T-0364: Pre-Soak blockiert weiterhin neue Starts, aber offene
                # Puls-/Haupt-Phasen muessen den sensorbasierten Max-Stop sehen.
                if pre_soak_lauf is not None:
                    ps_mgr, lauf = pre_soak_lauf
                    if kanal_sicherung and kanal_sicherung.ist_aktiv(kanal):
                        # T-0445/T-0451: Frische-Fenster zaehlt ab PULS-Start
                        # (live_lauf_state, jede Ventil-Oeffnung schreibt ein
                        # frisches gestartet_am), NICHT ab Sequenz-Start
                        # (lauf.gestartet_am). Andres Entscheidung 28.07.:
                        # Soak-Pausen zaehlen nicht mit -- sonst kappt ein
                        # Beat-Ausfall das Ende jeder Cycle-and-Soak-Sequenz
                        # ueber 120 min (z. B. 3x30 min mit 25-min-Pausen).
                        puls_start = await _lauf_start_fuer_kanal(dswc, kanal)
                        soll_stoppen, stop_grund = await motor.pruefe_kanal_max_stop(
                            kanal, zonen, lauf_start=puls_start,
                        )
                        if soll_stoppen:
                            erfolg, fehler = await ps_mgr.stoppe(lauf.zone_id)
                            if erfolg:
                                logger.info(
                                    "entscheidungsloop.pre_soak_max_stop",
                                    kanal=kanal,
                                    zone_id=lauf.zone_id,
                                    grund=stop_grund,
                                )
                            else:
                                logger.warning(
                                    "entscheidungsloop.pre_soak_max_stop_blockiert",
                                    kanal=kanal,
                                    zone_id=lauf.zone_id,
                                    grund=stop_grund,
                                    fehler=fehler,
                                )
                        else:
                            logger.debug(
                                "entscheidungsloop.pre_soak_aktiv",
                                kanal=kanal,
                                phase=lauf.phase,
                                grund=stop_grund,
                            )
                    else:
                        logger.debug(
                            "entscheidungsloop.pre_soak_pause_skip",
                            kanal=kanal,
                            phase=lauf.phase,
                        )
                    continue
                if kanal_sicherung and kanal_sicherung.ist_aktiv(kanal):
                    # T-0445: Laufstart aus `live_lauf_state` -> Frische-Check.
                    kanal_lauf_start = await _lauf_start_fuer_kanal(dswc, kanal)
                    soll_stoppen, stop_grund = await motor.pruefe_kanal_max_stop(
                        kanal, zonen, lauf_start=kanal_lauf_start,
                    )
                    if soll_stoppen:
                        erfolg = await kanal_sicherung.stoppe(
                            kanal,
                            Ausloser.AUTOMATIK,
                        )
                        if erfolg:
                            logger.info(
                                "entscheidungsloop.kanal_max_stop",
                                kanal=kanal,
                                grund=stop_grund,
                            )
                        else:
                            logger.warning(
                                "entscheidungsloop.kanal_max_stop_blockiert",
                                kanal=kanal,
                                grund=stop_grund,
                            )
                    else:
                        logger.debug(
                            "entscheidungsloop.kanal_aktiv",
                            kanal=kanal,
                            grund=stop_grund,
                        )
                    continue
                inaktive_kanaele.append(((dswc, kanal), zonen))

            # T-0152: Akut-Sortierung der inaktiven Kanal-Kandidaten.
            # Kleiner `akut_score` (Sensor - feuchte_kritisch) = akuter.
            # Zonen ohne Messwert: neutral, ans Ende.
            from bewaesserung.hahn_scheduler import (
                KanalKandidat,
            )
            kandidaten: list[tuple[KanalKandidat, list[ZonenKonfig], str | None]] = []
            for idx, ((dswc, kanal), zonen) in enumerate(inaktive_kanaele):
                # Niedrigsten Score der Kanal-Zonen nehmen (akutester gewinnt).
                score: float | None = None
                for z in zonen:
                    # T-0393: `letzte_messung_aggregiert` statt `letzte_messung`.
                    # Der rohe juengste EINZELsensor war hier eine zweite
                    # Wahrheit -- api_server + entscheidung nutzen laengst das
                    # Zonen-Aggregat (T-0179c). Bei Single-Sensor-Zonen
                    # identisch; bei Multi-Sensor-Zonen entscheidet jetzt
                    # derselbe Wert ueber Reihenfolge wie ueber das Giessen.
                    # Nebeneffekt (gewollt): ein > 90 min veralteter Sensor
                    # liefert None -> Zone sortiert neutral ans Ende, statt die
                    # Reihenfolge auf einem stale Wert zu priorisieren.
                    msg = await speicher.letzte_messung_aggregiert(z.zone_id)
                    if msg is None or msg.boden_feuchte is None:
                        continue
                    # T-0434 (Isomorphie-Check zum Guard in
                    # `_robuste_feuchte`): exakt 0.0 ist die Kontaktverlust-
                    # Signatur, kein Messwert. Ungefiltert ergibt sie den
                    # kleinstmoeglichen Score (0 - feuchte_kritisch) und
                    # katapultiert damit ausgerechnet den Kanal mit dem TOTEN
                    # Sensor an die Spitze der Giess-Reihenfolge. Gewaessert
                    # wird er zwar nicht -- `motor.pruefe_kanal` faellt gleich
                    # darunter auf denselben Guard --, aber er draengt sich in
                    # jedem Loop-Durchlauf vor und schiebt echte Zonen nach
                    # hinten. Gleiche Behandlung wie ein veralteter Sensor:
                    # ueberspringen, Zone sortiert neutral ans Ende.
                    # T-0489/T-0502: derselbe quellen- UND trajektorien-
                    # bewusste Guard wie in `_robuste_feuchte`. Eine echte
                    # FYTA-Null darf die Zone nicht aus der Reihenfolge
                    # werfen, und eine heruntergetrocknete Gardena-Null auch
                    # nicht -- sonst sortiert sich ausgerechnet die trockenste
                    # Zone selbst aus der Giess-Reihenfolge.
                    if float(msg.boden_feuchte) == 0.0:
                        # T-0502 (Korrektur 10.08.): Fenster-Maximum statt
                        # Vorgaengerwert -- isomorph zu `_robuste_feuchte`.
                        # Beide Guards muessen dasselbe Urteil faellen, sonst
                        # sortiert die Reihenfolge eine Zone aus, die die
                        # Entscheidung gleich darauf giessen will.
                        max_24h = await speicher.max_feuchte_im_fenster(
                            z.zone_id,
                            quelle=getattr(
                                msg.quelle, "value", str(msg.quelle),
                            ),
                            vor=msg.zeitstempel,
                        )
                        if null_ist_sensordefekt(z, msg.quelle, max_24h):
                            continue
                    s = float(msg.boden_feuchte) - float(z.feuchte_kritisch)
                    if score is None or s < score:
                        score = s
                kandidaten.append((
                    KanalKandidat(
                        kanal=kanal, akut_score=score, konfig_index=idx,
                    ),
                    zonen,
                    dswc,
                ))
            kandidaten.sort(key=lambda paar: paar[0]._sort_key())

            for kk, zonen, dswc in kandidaten:
                kanal = kk.kanal
                kanal_sicherung = _sicherung_fuer_kanal(dswc)
                e = await motor.pruefe_kanal(kanal, zonen)
                if e.soll_bewaessern and kanal_sicherung:
                    zone_ids = [z.zone_id for z in zonen]
                    ref_zone = zonen[0]
                    ps_mgr = _ps_manager_fuer_dswc(dswc)
                    if _pre_soak_policy_aktiv(ref_zone) and ps_mgr is not None:
                        # T-0336: Pre-Soak statt Einzellauf. e.dauer_sekunden =
                        # Hauptdose; Puls + Pause aus Zonen-Config. starte()
                        # tickt den Puls sofort; der Loop treibt Pause -> Haupt.
                        # T-0344: KEIN Mindest-Dosis-Gate hier -- die Mini-Dosen
                        # der Hecke kamen aus der falschen Strategie (korridor
                        # statt selten_gross), nicht aus dem Pre-Soak. Ein Gate
                        # wuerde die kleinen Gaben der haeufig_klein-Zonen (Bambus)
                        # faelschlich blocken. Steuerung gehoert in die Strategie.
                        haupt_min = max(1, (e.dauer_sekunden + 59) // 60)
                        ok, fehler = await ps_mgr.starte(
                            ref_zone.zone_id, kanal, zone_ids,
                            pre_soak_min=ref_zone.pre_soak_min,
                            pause_min=ref_zone.pre_soak_pause_min,
                            haupt_min=haupt_min,
                            ausloser=Ausloser.AUTOMATIK,
                            # T-0437: Cycle-and-Soak. Teilt haupt_min auf n
                            # Pulse AUF (gleiche Gesamtmenge), Defaults 1/0
                            # lassen Bestandszonen unveraendert.
                            haupt_pulse=ref_zone.haupt_pulse,
                            haupt_puls_pause_min=ref_zone.haupt_puls_pause_min,
                        )
                        if ok:
                            logger.info(
                                "entscheidungsloop.pre_soak_gestartet",
                                kanal=kanal, haupt_min=haupt_min,
                                grund=e.begruendung, akut_score=kk.akut_score,
                            )
                            # T-0549: hier wird NICHT mehr verbucht. Der
                            # Pre-Soak laeuft zwar, aber die Hauptdose kann
                            # noch ausbleiben (Hahn belegt, Zyklus-Abbruch) --
                            # zweimal real passiert. Die Buchung haengt jetzt
                            # am `haupt_start_callback` des PreSoakManagers
                            # und faellt mit dem ersten Haupt-Puls.
                        else:
                            logger.warning(
                                "entscheidungsloop.pre_soak_blockiert",
                                kanal=kanal, grund=fehler,
                            )
                    else:
                        erfolg = await kanal_sicherung.bewaessere(
                            kanal, zone_ids, e.dauer_sekunden, Ausloser.AUTOMATIK,
                        )
                        if erfolg:
                            logger.info(
                                "entscheidungsloop.bewaesserung_gestartet",
                                kanal=kanal,
                                dauer_s=e.dauer_sekunden,
                                grund=e.begruendung,
                                akut_score=kk.akut_score,
                            )
                        else:
                            # T-0151: Kann durch Hahn-Cluster-Lock geblockt sein —
                            # bewaessere() loggt den Grund bereits intern.
                            logger.warning(
                                "entscheidungsloop.bewaesserung_blockiert",
                                kanal=kanal,
                                grund="Ventil bereits aktiv, Hahn belegt oder Fehler",
                            )
                elif e.soll_bewaessern:
                    logger.info(
                        "entscheidungsloop.WUERDE_BEWAESSERN",
                        kanal=kanal,
                        dauer_s=e.dauer_sekunden,
                        grund=e.begruendung,
                    )
                else:
                    logger.info(
                        "entscheidungsloop.kanal_kein_bedarf",
                        kanal=kanal,
                        grund=e.begruendung,
                    )

            # Alle Zonen einzeln pruefen (fuer Logging + Monitoring-Zonen)
            entscheidungen = await motor.pruefe_alle_zonen()
            for e in entscheidungen:
                if e.soll_bewaessern:
                    logger.info(
                        "entscheidungsloop.zone_bewaessern",
                        zone=e.zone_id,
                        dauer_s=e.dauer_sekunden,
                        grund=e.begruendung,
                    )
                else:
                    logger.debug(
                        "entscheidungsloop.kein_bedarf",
                        zone=e.zone_id,
                        grund=e.begruendung,
                    )

            phasen.mark("kanal_block")
            # Predictive Watering + Benachrichtigungen fuer alle Zonen
            for zone in konfig.zonen:
                # Prognose
                zeitpunkt, grund = await motor.prognostiziere_bewaesserung(zone.zone_id)
                if zeitpunkt:
                    logger.info(
                        "prognose.bewaesserung_erwartet",
                        zone=zone.zone_id,
                        wann=zeitpunkt.strftime("%Y-%m-%d %H:%M"),
                        grund=grund,
                    )
                    # Proaktive iMessage fuer Monitoring-Zonen
                    await benachrichtiger.sende_prognose(zone, zeitpunkt, grund)
                else:
                    logger.debug("prognose.keine", zone=zone.zone_id, grund=grund)

                # Sofort-Benachrichtigung wenn Feuchte unter Schwelle
                # T-0532 (Isomorphie-Check zu T-0393 oben): vorher der rohe
                # Verarbeiter-Cache. Der ist nur nach zone_id verschluesselt
                # -- es gewinnt, wer zuletzt gefunkt hat, nicht der
                # Aggregat-Lead. In einer Multi-Sensor-Zone mit Lead haette
                # das eine iMessage "Bodenfeuchte bei 18%" auf einem Sensor
                # ausgeloest, der fuer genau diesen Zweck ausgeschlossen ist
                # (T-0332/T-0384). Heute latent -- der Pfad feuert nur bei
                # modus=monitoring, und beide Lead-Zonen (waldblumenhain,
                # hecke) sind automatik. Ein Modus-Wechsel haette den Bug
                # scharf gemacht, ohne dass jemand hier hinsieht.
                # `letzte_messung_aggregiert` liefert bei Lead-Dropout None
                # -> keine Nachricht, statt einer auf fremder Quelle.
                #
                # T-0533 (10.08.): Fenster explizit auf den Entscheidungs-
                # Horizont gesetzt statt auf den 90-min-Default. Diese
                # Benachrichtigung ist inhaltlich eine WARNUNG an den
                # Menschen -- fuer sie gilt dasselbe Argument wie fuer die
                # Anzeige (T-0476/E3): ein System, das auf einem 3 h alten
                # Wert ein Ventil oeffnet, darf auf demselben Wert auch
                # schreiben. Der zweite Aufrufer oben (Kanal-Priorisierung,
                # Z. 2074) behaelt den 90-min-Default BEWUSST: dort ist der
                # Ausfall erwuenscht, eine stale Zone soll neutral ans Ende
                # sortieren statt die Reihenfolge zu bestimmen.
                letzter_wert = await speicher.letzte_messung_aggregiert(
                    zone.zone_id,
                    fenster_minuten=AGGREGAT_FALLBACK_FENSTER_MIN,
                )
                if letzter_wert:
                    await benachrichtiger.pruefe_und_sende(
                        zone, letzter_wert.boden_feuchte
                    )

            phasen.mark("zonen_prognose")
            # Sensor-Gesundheit pruefen (Ausfall, Batterie)
            sensor_health_checks = 0
            if health_monitor:
                try:
                    zone_ids = [z.zone_id for z in konfig.zonen]
                    sensor_health_checks = len(zone_ids)
                    await health_monitor.pruefe_alle(zone_ids)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("sensor_health.fehler")
            phasen.mark("sensor_health")

            # Leck-/Dauerlauf-Detektor (saisonal: Mai-Sept)
            if leck_detektor:
                try:
                    zone_ids = [z.zone_id for z in konfig.zonen]
                    await leck_detektor.pruefe_alle(zone_ids)
                    # T-0418: lange offene Wartungs-Fenster melden. Ein
                    # Fenster setzt Leck-Detektor, Backfill und Fit-Jobs fuer
                    # die Zone aus -- und waechst mit jedem Tag mit. Das
                    # hecke-Fenster lief vier Wochen unbemerkt auf einer
                    # SCHARFEN Zone; in der Zeit gab es dort null
                    # "Bewaesserung ohne Wirkung"-Warnungen, waehrend andere
                    # Zonen zweistellig meldeten. Diese Warnung haette den
                    # Fall nach einer Woche sichtbar gemacht.
                    for wf in await speicher.hole_wartungs_fenster(
                        nur_offen=True,
                    ):
                        offen_seit = datetime.now() - datetime.fromisoformat(
                            wf["von_am"]
                        )
                        if offen_seit > timedelta(days=7):
                            logger.warning(
                                "wartungs_fenster.lange_offen",
                                zone_id=wf["zone_id"],
                                fenster_id=wf["id"],
                                offen_tage=offen_seit.days,
                                grund=(wf["grund"] or "")[:120] or "(leer)",
                                hinweis=(
                                    "Sicherheitsfunktionen dieser Zone sind "
                                    "ausgesetzt (Leck-Detektor, Backfill, "
                                    "Fit-Jobs). Schliessen oder begruenden."
                                ),
                            )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("leck_detektor.fehler")
            phasen.mark("leck_detektor")

            # T-0416: FYTA-Kalibrier-Push-Detektor — eigenes Interval (60 min).
            # Meldet nur; keine Giess-Reaktion.
            if fyta_sprung_detektor:
                try:
                    await fyta_sprung_detektor.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("fyta_sprung_detektor.fehler")
            phasen.mark("fyta_sprung")

            # T-0422: Wasserbilanz fortschreiben — 24-h-Gate, Shadow.
            if wasserbilanz_job:
                try:
                    await wasserbilanz_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("wasserbilanz.fehler")
            phasen.mark("wasserbilanz")

            # T-0423: Regen-Ensemble — eigenes Interval (60 min, icon_d2_eps
            # laeuft 8x taeglich). Shadow: schreibt nur die Verteilung fort.
            if regen_ensemble_job:
                try:
                    await regen_ensemble_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("regen_ensemble.fehler")
            phasen.mark("regen_ensemble")

            # T-0505: Mehrmodell-Mitschnitt (taeglich). Shadow, nur GET.
            if wetter_modell_job:
                try:
                    await wetter_modell_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("wetter_modell.fehler")
            phasen.mark("wetter_modell")

            # T-0505: gemessene Referenz von der DWD-Station (taeglich).
            if station_messung_job:
                try:
                    await station_messung_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("station_messung.fehler")
            phasen.mark("station_messung")

            # Wetter-Archiv (Ground-Truth) — eigenes Interval, nicht jeden 5-min-Zyklus
            if wetter_archiv_job:
                try:
                    await wetter_archiv_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("wetter_archiv.fehler")
            phasen.mark("wetter_archiv")

            # T-0403: Backup, ML-Drift, ML-Retrain, Response-Retrain,
            # Wochen-Report, Kalibrierung, Skalen-Mapping, k-Basis- und
            # Wirkungs-Fit liefen bis 05.08.2026 hier. Sie stehen jetzt im
            # `_wartungs_loop`. Grund: der Retrain brauchte am 02.08. 1421 s
            # und hat damit nicht seinen eigenen Zyklus verzoegert, sondern
            # den START des naechsten -- 24 Minuten ohne Kanalpruefung, bei
            # laufendem Max-Stop-Bedarf. Wer hier einen langlaufenden Job
            # ergaenzen will: er gehoert nur dann in DIESEN Loop, wenn sein
            # Ergebnis in die Entscheidung DESSELBEN Zyklus eingeht.

            # T-0168: AquaBloom-Auto-Logging (1h-Intervall)
            if aquabloom_job:
                try:
                    await aquabloom_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("aquabloom.job_fehler")
            phasen.mark("aquabloom")

            # T-0403: Plant-Optimum-Cache steht jetzt im `_wartungs_loop`.

            # DHS-Backfill (Gardena-Web) — eigenes Interval (alle 30 Min default),
            # T-0055-B3: holt verpasste App-Bewaesserungen auch nach Offline-Phasen.
            # T-0203 (17.05.): Multi-DSWC — alle Jobs via
            # `gardena_web_backfill_jobs`-Liste ticken (der Singular
            # `gardena_web_backfill_job` = jobs[0] wird damit mitgetickt).
            # F22: toter `elif gardena_web_backfill_job`-Zweig entfernt -- bei
            # leerer Liste ist der Singular None, bei nicht-leerer laeuft der if.
            if gardena_web_backfill_jobs:
                for _job in gardena_web_backfill_jobs:
                    try:
                        await _job.aktualisiere_wenn_faellig()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("dhs_backfill.fehler")
            phasen.mark("dhs_backfill")

            # Sensor-Heuristik (T-0055-B1) — Fallback wenn DHS ausfaellt.
            if sensor_backfill_job:
                try:
                    await sensor_backfill_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("sensor_backfill.fehler")
            phasen.mark("sensor_backfill")

            # T-0504 FYTA-Lueckenfuellung — holt Historie nach, die ein
            # spaet synchronisierender Sensor ausserhalb des Zwei-Tage-
            # Poll-Fensters abgeliefert hat. Taeglicher Tick, Fenster aus
            # dem DB-Zustand.
            if fyta_luecken_job:
                try:
                    await fyta_luecken_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("fyta_luecken.fehler")
            phasen.mark("fyta_luecken")

            # T-0527 FYTA-Geraetestatus (Akkustand, Online-Flag, Firmware).
            # Eigenes 6-h-Gate im Job; hier unbedingt aufrufen, damit ein
            # NameError nicht alles Nachfolgende still killt
            # (fehlerpattern_job_nicht_in_loop_verdrahtet).
            if fyta_status_job:
                try:
                    await fyta_status_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("fyta_status.fehler")
            phasen.mark("fyta_status")

            # T-0210 Orphan-Close — synthetisches SCHLIESSEN fuer
            # OEFFNEN-Events ohne Pendant (WS-Reconnect-Loss).
            if orphan_close_job:
                try:
                    await orphan_close_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("orphan_close.fehler")
            phasen.mark("orphan_close")

            # T-0250 Auto-Ignorieren — flippt manuell/watchdog-Events in
            # opt-in ml_ausschluss_fenster auf `ignoriert`. 10-min-Tick.
            if auto_ignorieren_job:
                try:
                    await auto_ignorieren_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("auto_ignorieren.fehler")
            phasen.mark("auto_ignorieren")

            # Sensor-DHS-Backfill (T-0068) — zieht verpasste Bodenfeuchte-
            # Messungen aus Offline-Phasen nach (eigenes 30-min-Intervall).
            if sensor_dhs_backfill_job:
                try:
                    await sensor_dhs_backfill_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("sensor_dhs_backfill.fehler")
            phasen.mark("sensor_dhs_backfill")

            # T-0122: Empfehlungs-Audit-Snapshot pro Zone + Eval offener
            # Audits gegen reale Sensor-Werte (1×/h, eigenes Intervall).
            if empfehlungs_audit_job:
                try:
                    await empfehlungs_audit_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("empfehlungs_audit.fehler")
            phasen.mark("empfehlungs_audit")

            # T-0132 (H-8): Endpoint-Health-Tick MUSS vor dem Watchdog-Tick
            # laufen, damit der Watchdog-Trigger C im selben Zyklus den
            # frischen Status liest.
            if endpoint_health_job:
                try:
                    await endpoint_health_job.aktualisiere_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("endpoint_health.fehler")
            phasen.mark("endpoint_health")

            # T-0126 (H-2): Watchdog-Tick — feuert iMessage bei akut-3-Tage,
            # Husqvarna-Cadence-Block oder Endpoint-Schema-Drift. Throttle
            # ueber DB persistiert.
            if watchdog_job:
                try:
                    await watchdog_job.pruefe_und_sende_wenn_faellig()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("watchdog.fehler")
            phasen.mark("watchdog")

            # Wetter-Ereignisse pruefen (Frost, Hitze, Starkregen)
            wetter_standorte_geprueft = 0
            if wetter_manager and speicher:
                for sid in wetter_manager.standort_ids:
                    wetter_standorte_geprueft += 1
                    try:
                        vorhersage = await wetter_manager.hole_vorhersage(sid)
                        await pruefe_wetter_ereignisse(vorhersage, speicher, sid)
                        # Wetter fuer ML-Features persistieren (nur bei neuem Forecast)
                        abfrage_key = (sid, vorhersage.abfrage_zeitstempel.isoformat())
                        if vorhersage.stunden and abfrage_key not in letzter_wetter_ts:
                            await speicher.speichere_wetter(
                                vorhersage.abfrage_zeitstempel, vorhersage.stunden, sid
                            )
                            wetter_zaehler += 1
                            letzter_wetter_ts[abfrage_key] = wetter_zaehler
                            # Aelteste Haelfte entfernen wenn Limit erreicht
                            if len(letzter_wetter_ts) > 200:
                                grenze = sorted(letzter_wetter_ts.values())[100]
                                letzter_wetter_ts = {
                                    k: v for k, v in letzter_wetter_ts.items()
                                    if v > grenze
                                }
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("wetter_ereignisse.fehler", standort=sid)
            phasen.mark("wetter_block")

            jetzt = datetime.now()
            naechster_lauf = jetzt + timedelta(
                seconds=ENTSCHEIDUNGSINTERVALL_SEKUNDEN
            )
            if laufstatus is not None:
                laufstatus["letzter_entscheidungszyklus"] = jetzt
                laufstatus["naechster_entscheidungszyklus"] = naechster_lauf

            # T-0055-B4: Diff der WS-Metriken seit letztem Heartbeat
            ws_diff: dict[str, dict[str, int]] | None = None
            if gardena_client is not None:
                ws_diff = gardena_client.metriken.diff_seit_snapshot(letzter_ws_snapshot)
                letzter_ws_snapshot = gardena_client.metriken.snapshot()

            # T-0556: ein kriechender Zyklus war bisher nur eine Zahl im
            # Heartbeat auf INFO -- man musste ihn suchen, um ihn zu finden.
            # Am 30.08. lief statt zwoelf Zyklen pro Stunde EINER, und das
            # fiel erst auf, als die Datenfehler schon repariert wurden.
            # Solange der Loop kriecht, sind ALLE Zeitstempel aus
            # `datetime.now()` beim Verarbeiten systematisch zu spaet, und
            # jede Reparatur weiter unten behandelt nur Symptome.
            logge_zyklus_ende(
                laufzeit_s=round(time.perf_counter() - zyklus_start, 2),
                phasen=phasen.top() or None,
                kanaele_geprueft=len(kanaele),
                zonen_geprueft=len(entscheidungen),
                sensor_health_checks=sensor_health_checks,
                wetter_standorte_geprueft=wetter_standorte_geprueft,
                ws_events=ws_diff if ws_diff else None,
                naechster_lauf=_format_zeitpunkt(naechster_lauf),
            )

        except asyncio.CancelledError:
            raise
        except sqlite3.OperationalError as fehler:
            # T-0210: Lock-spezifischer Backoff. Der Retry-Wrapper in
            # `speicher._mit_lock_retry` faengt kurze BUSY/LOCKED-Bursts
            # schon ab; wenn die Exception trotzdem hier landet, lief
            # eine langlaufende konkurrierende Operation (z. B. ML-Drift-
            # Scan ueber 60 Tage, Backup-Snapshot). Statt im naechsten
            # 5-min-Zyklus sofort denselben Lock-Konflikt zu erzwingen,
            # 30 s ruhen — typischerweise ist der Konkurrent danach
            # durch und der Loop kommt im Folgezyklus sauber durch.
            text = str(fehler).lower()
            if "locked" in text or "busy" in text:
                logger.warning(
                    "entscheidungsloop.db_lock_backoff",
                    fehler=str(fehler),
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=30)
                    return
                except asyncio.TimeoutError:
                    continue
            logger.exception("entscheidungsloop.fehler")
        except Exception:
            logger.exception("entscheidungsloop.fehler")

        # 5 Minuten warten oder bis Stop-Signal
        warte_start = time.time()
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=ENTSCHEIDUNGSINTERVALL_SEKUNDEN,
            )
            return
        except asyncio.TimeoutError:
            pass
        _melde_takt_luecke(
            "entscheidungsloop", warte_start, ENTSCHEIDUNGSINTERVALL_SEKUNDEN
        )


def _erwerbe_single_instance_lock() -> None:
    """Sichert dass nur eine `bewaesserung.main`-Instanz laeuft (T-0098).

    Oeffnet die Lock-Datei (siehe `_ermittle_lock_pfad`) und versucht ein
    nicht-blockierendes exklusives flock. Schlaegt fehl, wenn bereits ein
    anderer Prozess das Lock haelt — wir lesen die PID aus der Datei und
    melden klar: "Backend laeuft schon als PID X". Bei Erfolg wird die
    eigene PID geschrieben und das FD bleibt offen (bis Prozess-Ende =
    automatischer Lock-Release durch das OS).

    T-0127 (H-3a): legt das Verzeichnis bei Bedarf an, weil
    `~/Library/Application Support/...` auf frischen Setups noch nicht
    existiert.

    Wirft RuntimeError mit User-freundlicher Meldung statt OperationalError-
    Loops, wenn ein Konflikt erkannt wird.
    """
    global _lock_handle
    lock_pfad = _ermittle_lock_pfad()
    lock_pfad.parent.mkdir(parents=True, exist_ok=True)
    _lock_handle = open(lock_pfad, "a+")
    try:
        fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        if getattr(exc, "errno", None) not in (errno.EAGAIN, errno.EACCES):
            raise
        _lock_handle.seek(0)
        andere_pid = _lock_handle.read().strip() or "unbekannt"
        _lock_handle.close()
        _lock_handle = None
        raise RuntimeError(
            f"Backend laeuft schon als PID {andere_pid}. "
            f"Wenn das nicht stimmt: `rm {lock_pfad}` und erneut starten. "
            "Sonst: bestehenden Prozess beenden ('pkill -f bewaesserung.main' "
            "oder 'launchctl unload ~/Library/LaunchAgents/"
            "de.xindaan.pflanzen-dashboard.plist')."
        ) from exc
    _lock_handle.seek(0)
    _lock_handle.truncate()
    _lock_handle.write(str(os.getpid()))
    _lock_handle.flush()


def main() -> None:
    """CLI-Einstiegspunkt."""
    # .env laden aus Projekt-Root (Gardena/.env)
    projekt_root = Path(__file__).resolve().parent.parent.parent.parent
    dotenv.load_dotenv(projekt_root / ".env")

    try:
        _erwerbe_single_instance_lock()
    except RuntimeError as exc:
        print(f"\n[FEHLER] {exc}\n", file=sys.stderr)
        sys.exit(1)

    konfig_pfad = None
    if len(sys.argv) > 1:
        konfig_pfad = Path(sys.argv[1])

    try:
        asyncio.run(ausfuehren(konfig_pfad))
    except RuntimeError as exc:
        # Backend hat eine selbst-verfasste Fehlermeldung weitergegeben
        # (z.B. Husqvarna-Verbindung weg). Klar darstellen ohne 60-zeilige
        # httpx-Stack-Trace.
        print(f"\n[FEHLER] {exc}\n", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
