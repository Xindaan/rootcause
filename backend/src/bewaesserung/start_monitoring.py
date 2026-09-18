"""T-0617: Anzeige und Sensorabruf waehrend initialer Gardena-Discovery.

Der Cache enthaelt nur die zuletzt bestaetigten Sensor-Adressen, niemals
Ventilzustaende oder Steuer-Mappings. Zonen kommen weiter aus der DB.
"""

import asyncio
import hashlib
import json
from pathlib import Path
import time
from uuid import UUID

import structlog

from bewaesserung.fyta_client import FytaClient
from bewaesserung.gardena_customer_auth import baue_aus_env
from bewaesserung.sensor_dhs_backfill import SensorDhsBackfillJob, baue_sensor_zu_zone

logger = structlog.get_logger()
CACHE_MAX_ALTER_S = 7 * 86400
# T-0632: ein gesunder Leser endet sofort nach cancel(); 10 s je Runde sind Reserve.
LESER_JOIN_TIMEOUT_S = 10.0


def speichere_sensor_adressen(pfad, client_id, location_id, sensor_ids, *, stand=None):
    """Nur nach erfolgreicher Discovery aufrufen; keine Auth-/Messdaten."""
    daten = {
        "identitaet": hashlib.sha256(client_id.encode()).hexdigest(),
        "stand": time.time() if stand is None else stand,
        "location_id": str(UUID(location_id)),
        "sensor_ids": sorted(str(UUID(gid)) for gid in sensor_ids),
    }
    pfad = Path(pfad)
    temp = pfad.with_suffix(".tmp")
    temp.write_text(json.dumps(daten) + "\n")
    temp.replace(pfad)


def lade_sensor_adressen(pfad, client_id):
    """Fremder, alter oder defekter Cache deaktiviert nur den DHS-Abruf."""
    try:
        daten = json.loads(Path(pfad).read_text())
        if daten["identitaet"] != hashlib.sha256(client_id.encode()).hexdigest():
            return None
        if not 0 <= time.time() - float(daten["stand"]) <= CACHE_MAX_ALTER_S:
            return None
        return str(UUID(daten["location_id"])), {str(UUID(gid)) for gid in daten["sensor_ids"]}
    except (OSError, ValueError, TypeError, KeyError):
        return None


async def _fyta_lesen(konfig, speicher, verarbeiter):
    client = FytaClient(
        konfig.fyta,
        sperre_pfad=Path(konfig.speicher.db_pfad).resolve().parent
        / "fyta_login_sperre.json",   # T-0610 E23, dieselbe Datei wie main.py
    )
    try:
        client.registriere_callback(verarbeiter.verarbeite)
        await client.initialisiere_dedup_aus_db(speicher)
        await client.starte_polling()
    finally:
        client.stoppe()


async def _dhs_lesen(konfig, speicher):
    ordner = Path(konfig.speicher.db_pfad).resolve().parent
    adressen = lade_sensor_adressen(ordner / "gardena_sensor_adressen.json", konfig.gardena.client_id)
    auth = baue_aus_env(
        ordner / "gardena_customer_token.json",
        ordner / "gardena_customer_login_sperre.json",  # T-0621
    )
    if not adressen or auth is None:
        logger.warning("start_monitoring.dhs_ohne_adressen_oder_auth")
        return
    location, sensor_ids = adressen
    zuordnung = baue_sensor_zu_zone(await speicher.hole_zuordnungen(), sensor_ids)
    if not zuordnung:
        return
    job = SensorDhsBackfillJob(auth, speicher, location, zuordnung)
    while True:
        try:
            await job.aktualisiere_wenn_faellig()
        except Exception:
            logger.exception("start_monitoring.dhs_fehler")
        await asyncio.sleep(300)


async def warte_auf_gardena(client, api_task, konfig, speicher, verarbeiter):
    """Verbindung abwarten, unabhaengige Leser betreiben, sauber uebergeben.

    Erst nach dem Join aller Leser darf der normale Betrieb eigene Leser
    starten. Bei Stop/Fehler wird auch die wartende Discovery abgebrochen.
    """
    async def bewacht(name, aufruf):
        try:
            await aufruf()
        except Exception:
            logger.exception("start_monitoring.leser_fehler", quelle=name)

    verbindung = asyncio.create_task(client.verbinden())
    leser = [asyncio.create_task(bewacht("dhs", lambda: _dhs_lesen(konfig, speicher)))]
    if konfig.fyta and konfig.fyta.pflanzen:
        leser.append(asyncio.create_task(bewacht(
            "fyta", lambda: _fyta_lesen(konfig, speicher, verarbeiter),
        )))
    try:
        fertig, _ = await asyncio.wait([verbindung, api_task], return_when=asyncio.FIRST_COMPLETED)
        if api_task in fertig:
            await api_task
            raise RuntimeError("Dashboard-Server waehrend Gardena-Start beendet")
        await verbindung
    finally:
        await _beende_begrenzt([verbindung, *leser])


async def _beende_begrenzt(tasks):
    """T-0632: Abbrechen und joinen, aber nie unbegrenzt warten.

    Am 22.10.2026 verschluckte der DHS-Leser einen Abbruch (vermutlich in
    httpx/anyio) und lief in seiner Endlosschleife weiter; der Start hing
    16 min ohne Entscheidungsschleife. Ein zweiter Abbruch wirkte sofort.
    Daher: abbrechen, begrenzt warten, Haenger mit Stack loggen, ein zweites
    Mal abbrechen. Haengt er dann noch, startet der Betrieb trotzdem.
    """
    offen = set(tasks)
    for runde in (1, 2):
        for task in offen:
            task.cancel()
        _, offen = await asyncio.wait(offen, timeout=LESER_JOIN_TIMEOUT_S)
        if not offen:
            break
        for task in offen:
            logger.warning(
                "start_monitoring.leser_haengt", runde=runde,
                task=task.get_name(), stack=_stack_text(task),
            )
    for task in offen:
        logger.error("start_monitoring.leser_weiter_haengend", task=task.get_name())
    # Ergebnisse abholen, damit asyncio keine "never retrieved"-Warnung loggt.
    for task in tasks:
        if task.done() and not task.cancelled():
            task.exception()


def _stack_text(task):
    """Ganze await-Kette; Task.print_stack zeigt nur den obersten Rahmen."""
    zeilen = []
    obj = task.get_coro()
    while obj is not None:
        rahmen = getattr(obj, "cr_frame", None) or getattr(obj, "gi_frame", None)
        if rahmen is None:
            break
        zeilen.append(
            f"{rahmen.f_code.co_filename}:{rahmen.f_lineno} {rahmen.f_code.co_name}"
        )
        obj = getattr(obj, "cr_await", None) or getattr(obj, "gi_yieldfrom", None)
    return "\n".join(zeilen)


async def stoppe_api(server, task):
    """Uvicorn bis zum vollstaendigen Request-/Lifespan-Abschluss beenden."""
    server.should_exit = True
    if task.done():
        await asyncio.gather(task, return_exceptions=True)
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
