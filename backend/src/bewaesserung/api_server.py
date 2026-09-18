"""FastAPI REST-Server fuer das Dashboard.

Stellt Sensor-, Ventil- und Entscheidungsdaten als JSON bereit.
Laeuft als Teil des Hauptprozesses (kein separater Server noetig).
"""

import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

logger = structlog.get_logger()

from bewaesserung.api_auth import auth_dependency, validiere_route_matrix
from bewaesserung.request_raten import zaehler as _request_raten
from bewaesserung.modelle import (
    ECHTES_KANAL_WASSER,
    Ausloser,
    BlockerTyp,
    EntscheidungsScope,
    GesamtKonfig,
    SensorMessung,
    SensorWarnungTyp,
    VentilAktion,
    VentilEreignis,
    WetterEreignisTyp,
    ZonenKonfig,
    ist_auto_loop_zone,
    null_ist_sensordefekt,
)
from bewaesserung.speicher import (
    AGGREGAT_FALLBACK_FENSTER_MIN,
    Speicher,
    ist_messung_verwendbar,
)
from bewaesserung.entscheidung import MAX_FALLBACK_ALTER_STUNDEN, Entscheidungsmotor
from bewaesserung.giessfenster import (
    MODUS_AKTIV as GF_MODUS_AKTIV,
    beschreibe_sperre,
    naechster_erlaubter_start,
)
from bewaesserung.sensordaten import SensorDatenVerarbeiter
from bewaesserung.ventil_sicherung import VentilSicherung
from bewaesserung.wetter import WetterManager
from bewaesserung import dosis_test

# ML (optional — graceful degradation)
try:
    from bewaesserung.ml.vorhersage import MLVorhersageService
except ImportError:
    MLVorhersageService = None  # type: ignore

app = FastAPI(
    title="Gardena Bewaesserung",
    version="0.1.0",
    # T-0140: globale Auth. Jede Route laeuft durch `auth_dependency`,
    # die anhand der ROUTE_ROLLEN-Matrix entscheidet (public/read/control)
    # und 401/403/429 bei Verstoessen wirft.
    dependencies=[Depends(auth_dependency)],
    # T-0140: Web-UI-Doku ausgeschaltet (Mobile-Backend, kein Bedarf an
    # /docs / /redoc). `/openapi.json` bleibt aktiv und ist read-
    # geschuetzt -- nuetzlich fuer Swift-Codegen via quicktype.
    docs_url=None,
    redoc_url=None,
)

# CORS: nur explizite Origins zulassen.
# In Prod wird das Frontend von FastAPI selbst ausgeliefert (Same-Origin, kein
# CORS noetig). Im Dev laeuft Vite auf 5173 und braucht Cross-Origin-Zugriff.
# "*" war offen fuer CSRF: jede Website im LAN konnte POST /api/giessen oder
# /api/notfall-stopp ausloesen.
# T-0140: `X-Api-Key` muss in `allow_headers`, sonst blockt der Browser
# den Vite-Dev-Frontend-Aufruf (Preflight-CORS).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8090",
        "http://127.0.0.1:8090",
    ],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-Api-Key"],
)


@app.middleware("http")
async def shutdown_race_guard(request: Request, call_next):
    """T-0171: faengt den Shutdown-Race beim Ctrl-C ab.

    Beim Beenden ueber `./start.sh` + Ctrl-C kann ein Inflight-Request
    (z. B. Frontend-Polling auf `/api/ml/vorhersage`) noch einen DB-Read
    starten, nachdem `speicher.schliessen()` die Connection bereits
    geschlossen hat. aiosqlite wirft dann `ProgrammingError: Cannot
    operate on a closed database` bzw. `ValueError: no active
    connection` — uvicorn loggt das als Stack-Trace. Funktional harmlos
    (der Prozess endet ohnehin, keine DB-Korruption), aber haesslich.

    Je nach Timing crasht ein Inflight-Read unterschiedlich:
    - mitten in `execute()` waehrend `close()` laeuft -> `sqlite3.Error`
      (`ProgrammingError: Cannot operate on a closed database`);
    - `_db` schon `None` nach vollem `schliessen()` -> `AssertionError`
      aus `assert self._db is not None` in den Speicher-Methoden;
    - aiosqlite-interne Connection weg -> `ValueError: no active
      connection`.

    Alle drei werden NUR abgefangen, wenn `_speicher._geschlossen`
    gesetzt ist — dann ist es eindeutig ein Shutdown-Race: leise 503
    statt Stack-Trace. Im Normalbetrieb ist das Flag False, dann ist
    so ein Fehler ein echter Bug und wird unveraendert hochgeworfen
    (regulaerer 500 + Log).
    """
    try:
        return await call_next(request)
    except (sqlite3.Error, ValueError, AssertionError):
        if _speicher is not None and _speicher._geschlossen:
            return JSONResponse(
                status_code=503,
                content={"detail": "Backend faehrt herunter"},
            )
        raise


# Nach `shutdown_race_guard` definiert und damit die AEUSSERE der beiden
# Middlewares: so wird auch der Request gezaehlt, den der Guard zu einem 503
# umbiegt. Ein Sturm hoert nicht auf, nur weil das Backend herunterfaehrt.
@app.middleware("http")
async def request_raten_zaehler(request: Request, call_next):
    """T-0463: Requests pro Route zaehlen und beim Deckel-Riss warnen.

    Rein beobachtend, kein Ratelimit -- Begruendung im Modul-Docstring von
    `request_raten`. Das Route-Template steht erst NACH dem Routing in
    `scope["route"]`, deshalb wird hinter `call_next` gezaehlt.
    """
    try:
        return await call_next(request)
    finally:
        # `finally`, damit auch ein Request gezaehlt wird, der mit einer
        # Exception endet -- eine Fehlerschleife im Frontend ist genau so ein
        # Sturm wie eine Erfolgsschleife, und sie wuerde sonst unsichtbar.
        route = request.scope.get("route")
        _request_raten.zaehle(request.method, getattr(route, "path", None))


def _iso(dt: datetime) -> str:
    """ISO-String mit expliziter TZ-Info fuer API-Antworten.

    Naive datetimes (Server-Lokalzeit) werden mit der System-TZ angereichert,
    damit Browser-Clients in anderer TZ die Zeiten korrekt interpretieren.
    """
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat()


# Diese werden beim Start von main.py gesetzt
_speicher: Speicher | None = None
_konfig: GesamtKonfig | None = None
_motor: Entscheidungsmotor | None = None
_verarbeiter: SensorDatenVerarbeiter | None = None
_wetter_manager: WetterManager | None = None
_ml_service: "MLVorhersageService | None" = None
_ventil_sicherung: VentilSicherung | None = None
# T-0203 (17.05.): Multi-DSWC-Support. Dict aller VentilSicherung-Instanzen
# pro DSWC-UUID. `_ventil_sicherung` (singular) bleibt als Backward-Compat-
# Alias fuer den primary-Eintrag.
_ventil_sicherungen: dict[str, VentilSicherung] = {}
# Zone-ID -> DSWC-UUID Mapping fuer Routing-Helper.
_zone_zu_dswc: dict[str, str] = {}
# T-0048: Referenz auf MlRetrainJob fuer /api/ml/status
_ml_retrain_job: "object | None" = None
# T-0108: Referenzen auf MlResponseRetrainJob + KalibrationsJob, damit
# /api/ml/status auch deren Fehler-/Erfolg-Status zeigt (vorher waren
# Crashes dieser beiden Jobs nur im Log sichtbar).
_ml_response_retrain_job: "object | None" = None
_kalibrations_job: "object | None" = None
# T-0181: Skalen-Mapping-Fit-Job (Crashes -> /api/ml/status).
_skalen_mapping_fit_job: "object | None" = None
# Hybrid Stufe 1: Physik-k_basis-Fit-Job (Crashes -> /api/ml/status).
_k_basis_fit_job: "object | None" = None
# T-0292 Stufe 2: Plateau-Wirkungs-Fit-Job (Crashes -> /api/ml/status).
_wirkung_fit_job: "object | None" = None
# T-0111: Pre-Soak-Manager (asyncio-Task-Orchestrator pro Zone)
_pre_soak_manager: "object | None" = None
_pre_soak_managers: dict[str, object] = {}
# T-0205: SensorBackfillJob-Referenz fuer Re-Scan-Trigger im PATCH/Bulk-
# Endpoint. Wenn ein Event auf `ignoriert` geflipt wird, soll die
# Heuristik die Karenz-Periode neu pruefen -- evtl. liegt dort ein
# Sensor-Sprung, der vorher von der Karenz blockiert war.
_sensor_backfill_job: "object | None" = None
# T-0297: LeckDetektor-Referenz, damit nach einem ignoriert-Flip die
# "Bewaesserung ohne Wirkung"-Warnung sofort re-evaluiert/geschlossen wird
# (sonst erst beim naechsten Detektor-Tick ~5 min spaeter via Self-Heal).
_leck_detektor: "object | None" = None


def _parse_zeitgrenze(roh: str | None) -> datetime | None:
    """T-0565: ISO-Zeitstempel von der API-Grenze auf DB-Konvention bringen.

    Die DB haelt **naive Lokalzeit**. Das Frontend schickt an mehreren
    Stellen `Date.toISOString()`, also einen `Z`-String. `fromisoformat`
    akzeptiert den seit Python 3.11 und erzeugt ein AWARE datetime; der
    Speicher bindet es dann als `'...+00:00'` und vergleicht es
    **lexikografisch** gegen naive Werte. Kein TypeError, kein Fehler --
    nur ein falsches Fenster.

    Gemessen (TZ=Europe/Berlin, Event 12:00, Messwerte alle 30 min): der
    "+/- 2 h um das Ventil-Event"-Chart lieferte 08:30-12:00 statt
    10:00-14:00. Genau die Sensor-Antwort NACH dem Giessen, wegen der man
    den Drawer oeffnet, fiel heraus. Die 08:00-Zeile fehlte zusaetzlich,
    weil `'...+00:00'` lexikografisch groesser ist als `'...'` -- der
    Beweis, dass hier Strings verglichen werden und keine Zeitpunkte.

    `/api/ventil-ereignisse` machte es seit jeher richtig; die Umrechnung
    war dort inline und wurde an den beiden anderen Endpoints vergessen.
    Deshalb jetzt EIN Helfer statt drei Handhabungen.
    """
    if not roh:
        return None
    zeitpunkt = datetime.fromisoformat(roh.replace("Z", "+00:00"))
    if zeitpunkt.tzinfo is not None:
        zeitpunkt = zeitpunkt.astimezone().replace(tzinfo=None)
    return zeitpunkt


def _sicherung_fuer_zone(zone_id: str) -> VentilSicherung | None:
    """T-0203: Routing-Helper. Liefert die VentilSicherung-Instanz fuer
    die DSWC der Zone. Backward-Compat: faellt auf `_ventil_sicherung`
    (primary) zurueck wenn die Zone kein explizites Mapping hat.
    """
    dswc_id = _zone_zu_dswc.get(zone_id)
    if dswc_id and dswc_id in _ventil_sicherungen:
        return _ventil_sicherungen[dswc_id]
    return _ventil_sicherung


def _pre_soak_manager_fuer_zone(zone_id: str):
    """Liefert den Pre-Soak-Manager der DSWC einer Zone."""
    dswc_id = _zone_zu_dswc.get(zone_id)
    if dswc_id and dswc_id in _pre_soak_managers:
        return _pre_soak_managers[dswc_id]
    return _pre_soak_manager


def _baue_budget_warnung(
    zone: ZonenKonfig, verbraucht_s: float, geplant_s: float,
) -> dict | None:
    """T-0444: ueberschreitet der geplante manuelle Lauf das Tagesbudget?

    Bewusst NUR eine Warnung, kein Block: der manuelle Pfad ist der Weg, auf
    dem Andre bewusst mehr giesst als die Automatik vorsieht (Realfall 28.07.:
    drei Laeufe in 14 h). Er soll es nur sehen, nicht daran gehindert werden.

    Gerechnet wird gegen das BASIS-Budget ohne `tages_budget_kritisch_faktor`
    -- der Faktor ist ein Automatik-Konzept (Runaway-Schutz bei kritischer
    Trockenheit) und hat im manuellen Pfad keine Entsprechung.

    T-0452: Die Warn-SCHWELLE ist `tages_advisory_anteil` x Budget, nicht das
    Budget selbst. Grund: `tages_budget_sekunden` ist in der Automatik ein
    harter Blocker und daher absichtlich grosszuegig ueber dem legitimen
    Bedarf gesetzt (bambuswald 300 min gegen ~270 min Hochsommer-Last) -- der
    Anlassfall vom 28.07. mit 194 min haette ihn nie erreicht. Abgesenkt
    wuerde er das normale Giessen sperren. Ein Wert kann nicht beides sein,
    also warnt das Advisory gegen den Anteil und das Budget bleibt Notbremse.
    Der ANLASS bleibt das Budget: der Warntext nennt beide Zahlen, damit die
    Meldung nicht wie ein Verbot des gedeckelten Werts aussieht.

    Reine Funktion, damit die Rechnung ohne HTTP testbar ist.
    """
    budget_s = float(zone.tages_budget_sekunden)
    if budget_s <= 0:
        return None
    # Anteil defensiv klemmen: 0 oder negativ wuerde bei JEDEM Start warnen
    # (Advisory-Muede -> genau die Abstumpfung, gegen die T-0445 gebaut hat),
    # >1 wuerde die Warnung hinter die Notbremse schieben.
    anteil = min(1.0, max(0.0, float(zone.tages_advisory_anteil)))
    schwelle_s = budget_s * anteil if anteil > 0 else budget_s
    summe_s = float(verbraucht_s) + float(geplant_s)
    if summe_s <= schwelle_s:
        return None
    # Der Kopf muss sagen, was WIRKLICH ueberschritten ist. Sonst meldet die
    # Zone bei Schwelle 180 / Budget 300 und 194 min ein "Tagesbudget
    # ueberschritten", das schlicht falsch ist -- und die naechste Diagnose
    # sucht einen Runaway, den es nicht gibt.
    if summe_s > budget_s:
        kopf = "Tagesbudget ueberschritten"
        bezug = f"gegen ein Budget von {budget_s / 60:.0f} min"
    else:
        kopf = "Warnschwelle erreicht"
        bezug = (
            f"gegen eine Warnschwelle von {schwelle_s / 60:.0f} min "
            f"({anteil * 100:.0f} % des Tagesbudgets von "
            f"{budget_s / 60:.0f} min, das noch nicht ausgeschoepft ist)"
        )
    return {
        "tages_budget_sekunden": budget_s,
        "advisory_schwelle_sekunden": schwelle_s,
        "budget_ueberschritten": summe_s > budget_s,
        "verbraucht_sekunden": float(verbraucht_s),
        "geplant_sekunden": float(geplant_s),
        # ASCII wie die uebrigen user-sichtbaren Backend-Strings dieser Datei
        # (s. titel_map in `_normalisiere_sensor_eintraege`).
        "text": (
            f"{kopf}: heute schon "
            f"{verbraucht_s / 60:.0f} min gelaufen, geplant sind weitere "
            f"{geplant_s / 60:.0f} min. Zusammen {summe_s / 60:.0f} min "
            f"{bezug}. Der Lauf startet trotzdem."
        ),
    }


async def _budget_warnung_fuer_start(
    zone: ZonenKonfig, geplant_s: float,
) -> dict | None:
    """Holt den Tagesverbrauch am Motor und baut daraus die Warnung.

    Der Verbrauch kommt aus `Entscheidungsmotor.tagesverbrauch` (T-0444),
    NICHT aus einer zweiten Query hier -- die Filterregel fuer
    Nicht-Wasser-Ausloeser darf nur an einer Stelle stehen.

    Faellt die Abfrage aus (kein Motor, Attrappe im Test, DB-Fehler), gibt es
    keine Warnung. Ein Advisory darf einen manuellen Start nie verhindern.
    """
    if _motor is None:
        return None
    try:
        verbraucht_s = float(await _motor.tagesverbrauch(zone.zone_id))
    except Exception:
        logger.warning(
            "api.ventil_tagesbudget_unbekannt", zone_id=zone.zone_id,
        )
        return None
    warnung = _baue_budget_warnung(zone, verbraucht_s, geplant_s)
    if warnung is not None:
        logger.warning(
            "api.ventil_tagesbudget_ueberschritten",
            zone_id=zone.zone_id,
            tages_budget_sekunden=warnung["tages_budget_sekunden"],
            # T-0452: getrennt mitloggen, sonst liest sich jede Advisory-Zeile
            # im Log wie ein erreichtes Budget.
            advisory_schwelle_sekunden=warnung["advisory_schwelle_sekunden"],
            budget_ueberschritten=warnung["budget_ueberschritten"],
            verbraucht_sekunden=warnung["verbraucht_sekunden"],
            geplant_sekunden=warnung["geplant_sekunden"],
        )
    return warnung


OPS_DEFAULT_SEVERITY = {"kritisch", "aktion", "wetter"}
OPS_ALLE_SEVERITY = OPS_DEFAULT_SEVERITY | {"routine"}


# T-0293: TTL-Cache fuer dashboard_snapshot. Das Frontend pollt alle 30 s;
# ohne Cache rechnet der Snapshot pro Poll vorhersage_zone (volle ML) fuer
# ALLE Zonen -> CPU-Saturierung + Event-Loop-Block -> Dashboard traege.
# Cache pro (zone_id, fenster, ml_details); Recompute nur ~alle TTL Sekunden.
# Moisture aendert sich langsam -> <=TTL stale ist unsichtbar. In
# konfiguriere_api geleert (Test-Isolation).
_SNAPSHOT_CACHE: dict[tuple, tuple[float, dict]] = {}
_SNAPSHOT_CACHE_TTL_S = 90.0
# T-0495 (Audit B1): Obergrenze als Backstop. Ohne sie war das Dict
# unbegrenzt und hielt jede Antwort vollstaendig -- ein kalter 7d/30d-
# Snapshot ist 9,9 MB gross, gemessen 40 -> 347 MB RSS ueber zwei Aufrufe.
# Der Hauptschutz ist die Expired-Eviction unten: abgelaufene Eintraege sind
# per Definition wertlos, ihr Speicher aber nicht. 32 gleichzeitig FRISCHE
# Schluessel (also innerhalb von 90 s angefragt) liegen weit ueber dem, was
# das Frontend erzeugt.
_SNAPSHOT_CACHE_MAX = 32


def _snapshot_cache_setze(key: tuple, content: dict) -> None:
    """Schreibt in den Snapshot-Cache und raeumt dabei auf.

    Erst alle abgelaufenen Eintraege weg (kostenlos, die wuerden ohnehin
    nie wieder ausgeliefert), dann als Backstop die aeltesten, bis die
    Obergrenze haelt.
    """
    jetzt = time.monotonic()
    for k in [
        k for k, (ts, _) in _SNAPSHOT_CACHE.items()
        if jetzt - ts >= _SNAPSHOT_CACHE_TTL_S
    ]:
        _SNAPSHOT_CACHE.pop(k, None)
    _SNAPSHOT_CACHE[key] = (jetzt, content)
    while len(_SNAPSHOT_CACHE) > _SNAPSHOT_CACHE_MAX:
        aeltester = min(_SNAPSHOT_CACHE, key=lambda k: _SNAPSHOT_CACHE[k][0])
        _SNAPSHOT_CACHE.pop(aeltester, None)


def konfiguriere_api(
    speicher: Speicher,
    konfig: GesamtKonfig,
    motor: Entscheidungsmotor,
    verarbeiter: SensorDatenVerarbeiter,
    wetter_manager: WetterManager | None = None,
    ml_service: "MLVorhersageService | None" = None,
    ventil_sicherung: VentilSicherung | None = None,
    ml_retrain_job: "object | None" = None,
    ventil_sicherungen: dict[str, VentilSicherung] | None = None,
    sensor_backfill_job: "object | None" = None,
    ml_response_retrain_job: "object | None" = None,
    kalibrations_job: "object | None" = None,
    skalen_mapping_fit_job: "object | None" = None,
    k_basis_fit_job: "object | None" = None,
    wirkung_fit_job: "object | None" = None,
    leck_detektor: "object | None" = None,
) -> None:
    """Injiziert Abhaengigkeiten in den API-Server.

    T-0203 (17.05.): `ventil_sicherungen`-Dict pro DSWC fuer Multi-DSWC-
    Routing. `ventil_sicherung` (Singular) bleibt als Primary-Alias fuer
    Backward-Compat-Callsites.
    """
    global _speicher, _konfig, _motor, _verarbeiter, _wetter_manager, _ml_service
    global _ventil_sicherung, _ventil_sicherungen, _zone_zu_dswc
    global _ml_retrain_job, _pre_soak_manager, _pre_soak_managers, _sensor_backfill_job
    global _ml_response_retrain_job, _kalibrations_job
    global _skalen_mapping_fit_job, _k_basis_fit_job, _wirkung_fit_job
    global _leck_detektor
    _speicher = speicher
    _konfig = konfig
    _motor = motor
    _verarbeiter = verarbeiter
    _wetter_manager = wetter_manager
    _ml_service = ml_service
    _ventil_sicherung = ventil_sicherung
    _ventil_sicherungen = dict(ventil_sicherungen or {})
    _ml_retrain_job = ml_retrain_job
    _sensor_backfill_job = sensor_backfill_job
    _ml_response_retrain_job = ml_response_retrain_job
    _kalibrations_job = kalibrations_job
    _skalen_mapping_fit_job = skalen_mapping_fit_job
    _k_basis_fit_job = k_basis_fit_job
    _wirkung_fit_job = wirkung_fit_job
    _leck_detektor = leck_detektor
    # T-0293: Snapshot-Cache bei (Re)Konfiguration leeren -- frische Deps +
    # Test-Isolation (jeder Test ruft konfiguriere_api).
    _SNAPSHOT_CACHE.clear()
    # T-0203: Zone -> DSWC-UUID Mapping aus der Konfig bauen.
    primary_dswc = (
        next(iter(_ventil_sicherungen)) if _ventil_sicherungen else None
    )
    _zone_zu_dswc = {}
    for z in konfig.zonen:
        if z.ventil_kanal is None:
            continue
        gid = z.ventil_geraet_id or primary_dswc
        if gid:
            _zone_zu_dswc[z.zone_id] = gid
    # T-0332: Aggregat-Lead pro Zone aus der Konfig in den Speicher spiegeln.
    # Beide Aggregat-Funktionen (Per-Zone + Bulk) lesen self._aggregat_lead ->
    # keine Decision-vs-UI-Inkonsistenz moeglich.
    speicher.setze_aggregat_lead({
        z.zone_id: z.aggregat_lead_geraet
        for z in konfig.zonen if z.aggregat_lead_geraet
    })
    # T-0111/T-0203: Pre-Soak-Manager pro DSWC. Sequenz-State ist per
    # zone_id persistiert; Recovery darf darum nur durch den Manager der
    # zuständigen Sicherung erfolgen.
    from bewaesserung.pre_soak import PreSoakManager

    async def _puls_gate(
        kanal: int, zone_ids: list[str]
    ) -> tuple[bool, str | None]:
        """T-0437: darf der naechste Haupt-Puls noch feuern?

        Waehrend einer Soak-Pause ist das Ventil zu; der Kanal-Max-Stop im
        Entscheidungsloop prueft dort NICHT (`main.py` verlangt
        `ist_aktiv(kanal)` und loggt sonst `pre_soak_pause_skip`). Bei einem
        Hauptlauf war das ein blindes Fenster, bei mehreren Pulsen wuerden die
        Folge-Pulse ungeprueft feuern, obwohl der Sensor waehrend des
        Einsickerns ueber `feuchte_schwelle_max + 10` gestiegen sein kann.
        """
        zonen_kanal = [
            z for z in konfig.zonen
            if z.zone_id in set(zone_ids) and z.ventil_kanal == kanal
        ]
        if not zonen_kanal:
            return True, None
        soll_stoppen, grund = await motor.pruefe_kanal_max_stop(
            kanal, zonen_kanal,
        )
        return (not soll_stoppen), grund

    async def _dosis_test_haupt_start(lauf) -> None:
        """T-0549: Testlauf verbuchen, sobald die HAUPTDOSE laeuft.

        Vorher hing die Buchung am Start der Pre-Soak-Sequenz. Zwei der 24
        Laeufe der T-0535-Reihe endeten aber nach dem Pre-Soak und haben ihren
        Slot trotzdem verbraucht (Blockbalance 7/7/8 statt 8/8/8).

        `lauf.haupt_s` ist die tatsaechlich kommandierte Gesamt-Hauptdose --
        nicht der Sollwert aus der Stufe, sonst stuende ein Planwert an der
        Stelle des Istwerts
        ([[fehlerpattern_benachbartes_feld_als_messwert]]).
        """
        zone = next(
            (z for z in konfig.zonen if z.zone_id == lauf.zone_id), None,
        )
        if zone is None:
            return
        await dosis_test.verbuche_lauf(
            speicher,
            konfig.dosis_test,
            zone,
            lauf.lauf_gruppe or None,
            datetime.now(),
            haupt_sekunden_ist=int(lauf.haupt_s),
        )

    _pre_soak_managers = {}
    if _ventil_sicherungen:
        for dswc_id, sicherung in _ventil_sicherungen.items():
            zone_ids = {
                z.zone_id for z in konfig.zonen
                if z.ventil_kanal is not None
                and _zone_zu_dswc.get(z.zone_id) == dswc_id
            }
            _pre_soak_managers[dswc_id] = PreSoakManager(
                sicherung, speicher, erlaubte_zone_ids=zone_ids,
                puls_gate=_puls_gate,
                haupt_start_callback=_dosis_test_haupt_start,
            )
        _pre_soak_manager = (
            _pre_soak_managers.get(primary_dswc)
            or next(iter(_pre_soak_managers.values()), None)
        )
    elif ventil_sicherung is not None:
        _pre_soak_manager = PreSoakManager(
            ventil_sicherung, speicher, puls_gate=_puls_gate,
            haupt_start_callback=_dosis_test_haupt_start,
        )
    else:
        _pre_soak_manager = None

    # T-0140: Route-Matrix-Vollstaendigkeit pruefen. Failt beim Startup,
    # falls eine FastAPI-Route nicht in ROUTE_ROLLEN steht -- damit
    # bleibt kein neuer Endpoint versehentlich offen.
    fehlt = validiere_route_matrix(app)
    if fehlt:
        liste = ", ".join(f"{m} {p}" for m, p in fehlt)
        raise RuntimeError(
            f"ROUTE_ROLLEN-Matrix unvollstaendig (T-0140). Fehlt: {liste}",
        )


def _parse_ops_severity(text: str | None) -> set[str]:
    """Normalisiert den Severity-Filter fuer den Ops-Tab."""
    if not text:
        return set(OPS_DEFAULT_SEVERITY)

    werte = {
        wert.strip().lower()
        for wert in text.split(",")
        if wert.strip()
    }
    gueltig = werte & OPS_ALLE_SEVERITY
    return gueltig or set(OPS_DEFAULT_SEVERITY)


def _zonen_map() -> dict[str, ZonenKonfig]:
    assert _konfig
    return {zone.zone_id: zone for zone in _konfig.zonen}


def _zone_name(zone_id: str | None) -> str | None:
    if not zone_id:
        return None
    zone = _zonen_map().get(zone_id)
    return zone.name if zone else zone_id


def _ist_autonom_scharf(zone_id: str | None) -> bool:
    """T-0388: gleiche Wahrheit wie das API-Feld `autonom_scharf`
    (_baue_zone_dict): globales `ventilsteuerung_aktiv` UND
    `ist_auto_loop_zone`. Entscheidet, ob eine Entscheidung real GESCHALTET
    hat (Indikativ) oder Shadow blieb (Konjunktiv) -- und ob eine Zone-Scope-
    Zeile das Duplikat einer KANAL-Zeile ist.
    """
    if not zone_id or not _konfig:
        return False
    zone = _zonen_map().get(zone_id)
    return bool(
        zone
        and _konfig.ventilsteuerung_aktiv
        and ist_auto_loop_zone(zone)
    )


def _kanal_geschwister_zonen(ref_zone_id: str) -> list[str]:
    """Zonen am selben (DSWC, Kanal) wie die Referenz-Zone.

    F15/T-0252: `scope_ref=str(kanal)` einer KANAL-Entscheidung ist bei
    Multi-DSWC NICHT eindeutig -- Kanal 1 existiert auf DSWC1 (waldblumen)
    UND DSWC2 (magerwiese). Die persistierte `zone_id` der Shadow-
    Entscheidung ist die ref_zone (`entscheidung.pruefe_kanal` setzt
    `zone_id=ref_zone.zone_id`) und disambiguiert das DSWC-Geraet. Vorher
    expandierte ein reiner Kanal-Lookup die Entscheidung faelschlich auf
    Zonen des anderen DSWC mit gleicher Kanal-Nummer.

    `_zone_zu_dswc` loest `ventil_geraet_id=None` (primary) bereits auf;
    im hardwarelosen Test-Setup ist die Map leer -> alle Zonen mappen auf
    None -> Gruppierung rein nach Kanal (= altes Verhalten, kein Regress).
    """
    assert _konfig
    ref = _zonen_map().get(ref_zone_id)
    if ref is None or ref.ventil_kanal is None:
        return [ref_zone_id]
    ref_dswc = _zone_zu_dswc.get(ref_zone_id)
    return [
        z.zone_id
        for z in _konfig.zonen
        if z.ventil_kanal == ref.ventil_kanal
        and _zone_zu_dswc.get(z.zone_id) == ref_dswc
    ]


def _zone_wetterstandort_map() -> dict[str, str]:
    assert _konfig
    mapping: dict[str, str] = {}
    for standort in (_konfig.standorte or []):
        wetter_standort = standort.wetter_standort or standort.standort_id
        for zone_id in standort.zonen:
            mapping[zone_id] = wetter_standort
    return mapping


def _zone_relevant(
    zone_filter: str | None,
    *,
    zone_id: str | None = None,
    betroffene_zonen: list[str] | None = None,
    wetter_standort: str | None = None,
) -> bool:
    """Prueft ob ein Ops-Eintrag zum gewaehlten Zonenfilter passt."""
    if not zone_filter:
        return True
    if zone_id == zone_filter:
        return True
    if betroffene_zonen and zone_filter in betroffene_zonen:
        return True
    if wetter_standort:
        return _zone_wetterstandort_map().get(zone_filter) == wetter_standort
    return False


def _blocker_label(blocker_typ: str | None) -> str:
    labels = {
        BlockerTyp.FEUCHTE_OK.value: "Keine Aktion noetig",
        BlockerTyp.KEINE_MESSUNG.value: "Blockiert wegen fehlender Messung",
        BlockerTyp.REGEN_ERWARTET.value: "Blockiert wegen Regen",
        BlockerTyp.ZEITFENSTER.value: "Blockiert wegen Zeitfenster",
        BlockerTyp.BUDGET_ERSCHOEPFT.value: "Blockiert wegen Budget",
        BlockerTyp.PAUSE_AKTIV.value: "Blockiert wegen Pause",
    }
    return labels.get(blocker_typ or "", blocker_typ or "Routine")


def _dauer_text(dauer_sekunden: int) -> str:
    minuten = round(dauer_sekunden / 60)
    if dauer_sekunden <= 0:
        return "0 min"
    if minuten <= 1:
        return "1 min"
    return f"{minuten} min"


def _normalisiere_shadow_eintraege(
    rohe_entscheidungen: list[dict],
    zone_filter: str | None,
) -> list[dict]:
    eintraege: list[dict] = []

    for roh in rohe_entscheidungen:
        scope = roh["scope"] or EntscheidungsScope.ZONE.value
        scope_ref = roh["scope_ref"] or roh["zone_id"]
        ist_kanal = scope == EntscheidungsScope.KANAL.value
        betroffene_zonen = (
            _kanal_geschwister_zonen(roh["zone_id"])
            if ist_kanal
            else [roh["zone_id"]]
        )
        # T-0388: Zone-Scope-Zeilen SCHARFER Zonen sind Duplikate ihrer
        # KANAL-Zeile -> ueberspringen. Shadow-Zonen (opt-out/monitoring)
        # erzeugen gar keine KANAL-Zeile (`pruefe_kanal` laeuft seit T-0334
        # nur fuer opt-in) und waren deshalb im Shadow-Feed unsichtbar.
        if not ist_kanal and _ist_autonom_scharf(roh["zone_id"]):
            continue
        if not _zone_relevant(
            zone_filter,
            zone_id=roh["zone_id"] if scope == EntscheidungsScope.ZONE.value else None,
            betroffene_zonen=betroffene_zonen,
        ):
            continue

        soll_bewaessern = bool(roh["soll_bewaessern"])
        # T-0388: Indikativ statt Konjunktiv, wenn die Zone scharf ist -- dann
        # hat das Ventil real geschaltet. Vorher las Andre "Wuerde bewaessern"
        # fuer hecke/bambuswald, obwohl echtes Wasser floss.
        scharf = any(_ist_autonom_scharf(z) for z in betroffene_zonen)
        # F11b (Andre 11.07.): eine SCHARFE "Bewaessert"-Zeile ist ein REALER
        # Lauf (wie das automatik-Ventil-Event) -> Giess-Historie / Live-Status,
        # KEINE Ops-Ausnahme. Der Live-"laeuft gerade"-Zustand steht im
        # TriageStrip/Karten-Badge/Betriebsstatus; ein anomaler Lauf faellt eh
        # als watchdog-Close bzw. bewaesserung_ohne_wirkung (KRITISCH) auf. Nur
        # die hypothetische Shadow-"Wuerde bewaessern" (nicht scharf) ist AKTION
        # (und wird zusaetzlich edge-getriggert).
        severity = "aktion" if (soll_bewaessern and not scharf) else "routine"
        dauer_text = _dauer_text(int(roh["dauer_sekunden"] or 0))
        titel = (
            (f"Bewaessert ({dauer_text})" if scharf
             else f"Wuerde bewaessern ({dauer_text})")
            if soll_bewaessern
            else _blocker_label(roh["blocker_typ"])
        )
        eintraege.append(
            {
                "id": f"shadow:{roh['id']}",
                "zeitstempel": roh["zeitstempel"],
                "typ": "SHADOW_ENTSCHEIDUNG",
                "severity": severity.upper(),
                "zone_id": roh["zone_id"] if scope == EntscheidungsScope.ZONE.value else None,
                "zone_name": _zone_name(roh["zone_id"]) if scope == EntscheidungsScope.ZONE.value else None,
                "scope": scope,
                "scope_ref": scope_ref,
                "betroffene_zonen": betroffene_zonen,
                "titel": titel,
                "details": roh["begruendung"],
                "meta": {
                    "soll_bewaessern": soll_bewaessern,
                    "dauer_sekunden": int(roh["dauer_sekunden"] or 0),
                    "blocker_typ": roh["blocker_typ"],
                    # T-0388: scharf vs. Shadow explizit -- das Frontend soll
                    # das nicht aus dem Titel-String raten muessen.
                    "autonom_scharf": scharf,
                },
            }
        )

    _edge_gate_shadow(eintraege)
    return eintraege


# F11b (T-0397, Andre 11.07. "nur bei Aenderung zeigen"): eine Shadow-Zone
# wiederholt fast identisch jeden Zyklus dieselbe Entscheidung ("Wuerde
# bewaessern (90 min)"). Das flutet den Ops-Default. Edge-Trigger: eine
# soll_bewaessern-Zeile bleibt nur AKTION, wenn sie sich vom zuletzt GEZEIGTEN
# Shadow-Zustand DERSELBEN scope_ref unterscheidet -- soll kippt (erstmals
# wuerde-bewaessern nach kein-Bedarf) ODER die Dauer aendert sich DEUTLICH
# (>= 15 min gegenueber dem zuletzt gezeigten Wert). Nicht-Edges -> ROUTINE
# (per Default unterdrueckt/aggregiert, per Toggle sichtbar). Kein Datenverlust.
#
# Anker = zuletzt gezeigter Zustand (nicht der unmittelbar vorherige): so
# fangen wir Dauer-JITTER ab (die Engine rechnet jede Zyklus-Dauer neu, 88 vs
# 90 vs 91 min ist keine "Aenderung"), zeigen aber echte Drift (90 -> 51 min),
# sobald sie 15 min ueberschreitet. Ein fixes Sekunden-Bucket scheiterte hier,
# weil 90 min = 5400 s genau auf einer Bucket-Grenze liegt.
_SHADOW_DAUER_DELTA_MIN = 15.0
# Selbst-Drossel-Blocker: die Zone WILL giessen, die Engine haelt sich nur
# selbst zurueck (min_pause / Tagesbudget). Zwischen zwei "Wuerde bewaessern"
# liegen typisch mehrere PAUSE_AKTIV-Zyklen (Realfall waldblumen: wb -> pause
# x N -> wb ...). Das ist KEINE Aenderung der Giess-Absicht -> darf den Edge-
# Anker nicht zuruecksetzen, sonst gilt jede Wiederholung als Edge.
_SHADOW_DROSSEL_BLOCKER = frozenset({"PAUSE_AKTIV", "BUDGET_ERSCHOEPFT"})


def _edge_gate_shadow(eintraege: list[dict]) -> None:
    nach_scope: dict[str, list[dict]] = defaultdict(list)
    for e in eintraege:
        if e["typ"] == "SHADOW_ENTSCHEIDUNG":
            nach_scope[e["scope_ref"]].append(e)
    for gruppe in nach_scope.values():
        gruppe.sort(key=lambda e: e["zeitstempel"])
        # Anker des zuletzt gezeigten Zustands: Klasse (will-giessen vs kein-
        # Bedarf) + zuletzt gezeigte Dauer. Drossel-Blocker zaehlen als
        # "will-giessen"-Fortsetzung, nicht als kein-Bedarf.
        anker_klasse: str | None = None
        anker_dauer_min: float | None = None
        for e in gruppe:
            soll = bool(e["meta"].get("soll_bewaessern"))
            blocker = e["meta"].get("blocker_typ")
            klasse = "giessen" if (soll or blocker in _SHADOW_DROSSEL_BLOCKER) else "kein_bedarf"
            dauer_min = int(e["meta"].get("dauer_sekunden") or 0) / 60.0
            if anker_klasse is None:
                ist_edge = True
            elif klasse != anker_klasse:
                ist_edge = True
            elif soll and (
                anker_dauer_min is None
                or abs(dauer_min - anker_dauer_min) >= _SHADOW_DAUER_DELTA_MIN
            ):
                # Erste echte "Wuerde bewaessern" im Giess-Lauf ODER deutliche
                # Dauer-Aenderung. Gedrosselte Zyklen (soll=False, dauer=0)
                # loesen hier NICHT aus (kein soll) und ueberschreiben die
                # Anker-Dauer nicht.
                ist_edge = True
            else:
                ist_edge = False
            if ist_edge:
                anker_klasse = klasse
                if soll:
                    anker_dauer_min = dauer_min
            # Nur die sichtbaren (soll_bewaessern) Wiederholungen beruhigen;
            # kein-Bedarf-/Drossel-Zeilen sind ohnehin schon ROUTINE.
            if e["severity"] == "AKTION" and not ist_edge:
                e["severity"] = "ROUTINE"
                e["meta"]["shadow_wiederholung"] = True


# F11 (T-0397, Andre 11.07.): Ops-Default = Ausnahme-Feed, kein Aktivitaets-Log.
# Ventil-Events nach Ausloser in Severity einordnen. Der Default-Filter
# (kritisch/aktion/wetter) zeigt so nur ANOMALES; alles Routine-/Erwartbare
# faellt in ROUTINE (per Default aggregiert/unterdrueckt, per Toggle sichtbar).
#   ignoriert / aquabloom       -> ROUTINE  (regime-ignoriert / Solar-Pumpe)
#   automatik / manuell         -> ROUTINE  (reale Laeufe -- Giess-Historie ist
#                                            dafuer die Wahrheit, F11b Andre 11.07.)
#   watchdog                    -> ROUTINE  (F11b-4, 13.07.: der Watchdog ist in
#                                            diesem System der NORMALE Schliess-
#                                            Fallback realer Laeufe -- 13/7T alle
#                                            an scharfen Zonen 30-61min, keine
#                                            Anomalie. Ein wirklich haengendes
#                                            Ventil faellt via bewaesserung_ohne_
#                                            wirkung/Dauer separat auf.)
#   notfall_stopp               -> KRITISCH (echter manueller Eingriff/Stopp)
#   unbekannt / rest            -> AKTION   (unklassifiziert -> braucht Blick)
#   zeitplan                    -> ROUTINE  (T-0455: Gardena-Cloud-Zeitplan,
#                                            Andres bewusster Fallback wenn der
#                                            Rechner aus ist -- realer Lauf,
#                                            keine Anomalie)
#   fremdwasser                 -> ROUTINE  (T-0453: Cross-Spray vom Nachbar-
#                                            Regner, vom User klassifiziert =
#                                            erklaert, nichts zu tun)
_VENTIL_SEVERITY_ROUTINE = frozenset({
    Ausloser.IGNORIERT.value,
    Ausloser.AQUABLOOM.value,
    Ausloser.AUTOMATIK.value,
    Ausloser.MANUELL.value,
    Ausloser.WATCHDOG.value,
    Ausloser.ZEITPLAN.value,
    Ausloser.FREMDWASSER.value,
})
_VENTIL_SEVERITY_KRITISCH = frozenset({Ausloser.NOTFALL_STOPP.value})


def _ventil_severity(ausloser: str | None) -> str:
    if ausloser in _VENTIL_SEVERITY_ROUTINE:
        return "ROUTINE"
    if ausloser in _VENTIL_SEVERITY_KRITISCH:
        return "KRITISCH"
    return "AKTION"


def _normalisiere_ventil_eintraege(
    rohe_ventile: list[dict],
    zone_filter: str | None,
) -> list[dict]:
    eintraege: list[dict] = []

    for roh in rohe_ventile:
        if not _zone_relevant(zone_filter, zone_id=roh["zone_id"]):
            continue

        dauer = int(roh["dauer_sekunden"] or 0)
        ausloser = roh["ausloser"]
        if roh["ventil_id"] == "manuell":
            titel = f"Manuell gegossen ({_dauer_text(dauer)})"
            details = "Im Dashboard als reales Ereignis geloggt"
        elif ausloser == Ausloser.MANUELL.value:
            titel = f"Manuell bewaessert ({_dauer_text(dauer)})"
            details = "Reales Ventilereignis"
        elif ausloser == Ausloser.AUTOMATIK.value:
            titel = f"Automatisch bewaessert ({_dauer_text(dauer)})"
            details = "Reales Ventilereignis"
        elif ausloser == Ausloser.ZEITPLAN.value:
            # T-0455: eigener Text, damit im Ops-Tab sichtbar ist, dass der
            # Lauf aus dem Gardena-Cloud-Zeitplan kam und NICHT aus der
            # Engine -- die beiden waren vorher nicht unterscheidbar.
            titel = f"Gardena-Zeitplan ({_dauer_text(dauer)})"
            details = "Realer Lauf aus dem Cloud-Zeitplan der Gardena-App"
        elif ausloser == Ausloser.FREMDWASSER.value:
            # T-0453: quell_zone kann fehlen (nicht ermittelbar), dann ohne.
            quelle = roh.get("quell_zone")
            titel = f"Fremdwasser ({_dauer_text(dauer)})"
            details = (
                f"Wasser aus Zone {quelle} (Cross-Spray), nicht aus diesem Kanal"
                if quelle else
                "Wasser aus einer Nachbar-Zone (Cross-Spray), nicht aus diesem Kanal"
            )
        else:
            titel = f"Ventilereignis ({_dauer_text(dauer)})"
            details = f"Ausloeser: {ausloser}"

        meta: dict[str, str | int | bool | None] = {
            "aktion": roh["aktion"],
            "ausloser": ausloser,
            "dauer_sekunden": dauer,
            # T-0084: Edit-Pfad braucht die rohe Event-ID + Korrektur-Marker.
            "ventil_event_id": roh["id"],
            "ausloeser_korrigiert": bool(roh.get("ausloser_korrektur")),
            # T-0453: Cross-Spray-Quelle, nur bei fremdwasser gesetzt.
            "quell_zone": roh.get("quell_zone"),
        }
        eintraege.append(
            {
                "id": f"ventil:{roh['id']}",
                "zeitstempel": roh["zeitstempel"],
                "typ": "VENTIL_EREIGNIS",
                "severity": _ventil_severity(ausloser),
                "zone_id": roh["zone_id"],
                "zone_name": _zone_name(roh["zone_id"]),
                "scope": "zone",
                "scope_ref": roh["zone_id"],
                "betroffene_zonen": [roh["zone_id"]],
                "titel": titel,
                "details": details,
                "meta": meta,
            }
        )

    return eintraege


def _normalisiere_wetter_eintraege(
    rohe_wetter: list[dict],
    zone_filter: str | None,
) -> list[dict]:
    titel_map = {
        WetterEreignisTyp.FROST.value: "Frostwarnung",
        WetterEreignisTyp.HITZE.value: "Hitzewarnung",
        WetterEreignisTyp.STARKREGEN.value: "Starkregenwarnung",
    }
    eintraege: list[dict] = []

    for roh in rohe_wetter:
        if not _zone_relevant(zone_filter, wetter_standort=roh["standort_id"]):
            continue

        eintraege.append(
            {
                "id": f"wetter:{roh['id']}",
                "zeitstempel": roh["zeitstempel"],
                "typ": "WETTER_EREIGNIS",
                "severity": "WETTER",
                "zone_id": None,
                "zone_name": None,
                "scope": "standort",
                "scope_ref": roh["standort_id"],
                "betroffene_zonen": [],
                "titel": titel_map.get(roh["typ"], roh["typ"]),
                "details": roh["details"],
                "meta": {
                    "typ": roh["typ"],
                    "standort_id": roh["standort_id"],
                    "beginn": roh["beginn"],
                    "ende": roh["ende"],
                },
            }
        )

    return eintraege


def _normalisiere_sensor_eintraege(
    rohe_warnungen: list[dict],
    zone_filter: str | None,
) -> list[dict]:
    titel_map = {
        SensorWarnungTyp.AUSFALL.value: "Sensor-Ausfall",
        SensorWarnungTyp.BATTERIE_NIEDRIG.value: "Batterie niedrig",
        SensorWarnungTyp.BATTERIE_KRITISCH.value: "Batterie kritisch",
        SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG.value: "Bewaesserung ohne Wirkung",
        SensorWarnungTyp.SENSOR_EINGEFROREN.value: "Sensor eingefroren",
        # T-0416: bewusst als Daten-Ereignis benannt, nicht als Sensor-Defekt --
        # der Sensor ist in Ordnung, der Hersteller hat die Kurve verschoben.
        SensorWarnungTyp.FYTA_KALIBRIER_PUSH.value: "FYTA-Kalibrierung verschoben",
        # T-0433: kein Sensor-Defekt, sondern ein Dissens zwischen zwei
        # Sensoren desselben Kanals, den der Trigger-Ausschluss stumm
        # zugunsten des Leads entscheidet.
        SensorWarnungTyp.LEAD_DIVERGENZ.value: "Ausgeschlossene Zone meldet kritisch",
        # T-0445: kein Sensor-Defekt -- waehrend des Laufs kam schlicht kein
        # neuer Wert an, der Max-Stop urteilte auf Daten von vor dem Giessen.
        SensorWarnungTyp.KEINE_ANKUNFT_IM_LAUF.value: (
            "Kein neuer Messwert waehrend des Laufs"
        ),
    }
    eintraege: list[dict] = []

    for roh in rohe_warnungen:
        if not _zone_relevant(zone_filter, zone_id=roh["zone_id"]):
            continue

        ist_behoben = bool(roh["behoben_um"])
        severity = "ROUTINE"
        if not ist_behoben and roh["typ"] in {
            SensorWarnungTyp.AUSFALL.value,
            SensorWarnungTyp.BATTERIE_KRITISCH.value,
            SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG.value,
            SensorWarnungTyp.SENSOR_EINGEFROREN.value,
            # T-0433: kritisch, weil hier eine Zone um Wasser bittet und
            # keins bekommt -- der Ausschluss aendert genau dann das
            # Ergebnis. Der Detektor feuert nur in dieser Konstellation,
            # nicht auf dem dauerhaften ~20-pp-Offset.
            SensorWarnungTyp.LEAD_DIVERGENZ.value,
            # T-0416 (Statuswechsel 23.07.): hochgestuft von ROUTINE.
            # ROUTINE war vertretbar, solange die Warnung nur eine Bruecke
            # bis zu einem Hersteller-Feld (`calibration_version`) war.
            # FYTA hat abgesagt -- kein Quick Fix, niedrige Prioritaet ->
            # unser Detektor ist die EINZIGE Quelle, dauerhaft.
            # Der Push vom 20.07. hat ueber den Median still den operativen
            # Feuchtewert gekippt (T-0421: aktuelle_feuchte 12.0 -> "akut"
            # -> 90-min-Empfehlung per iMessage). Ein Signal, das genau
            # diese Klasse verhindern soll, darf nicht im Ausnahme-Feed
            # untergehen.
            SensorWarnungTyp.FYTA_KALIBRIER_PUSH.value,
            # T-0445: die Warnung entsteht NUR zusammen mit einem erzwungenen
            # Stop -- ein Lauf wurde beendet, ohne dass ein Messwert das
            # gerechtfertigt haette. ROUTINE wuerde sie aus dem Default-Feed
            # filtern (OPS_DEFAULT_SEVERITY) und damit genau den Fall
            # unsichtbar machen, fuer den sie gebaut ist.
            SensorWarnungTyp.KEINE_ANKUNFT_IM_LAUF.value,
        }:
            severity = "KRITISCH"
        titel = titel_map.get(roh["typ"], roh["typ"])
        if ist_behoben:
            titel = f"{titel} behoben"

        eintraege.append(
            {
                "id": f"sensor:{roh['id']}",
                "zeitstempel": roh["zeitstempel"],
                "typ": "SENSOR_WARNUNG",
                "severity": severity,
                "zone_id": roh["zone_id"],
                "zone_name": _zone_name(roh["zone_id"]),
                "scope": "zone",
                "scope_ref": roh["zone_id"],
                "betroffene_zonen": [roh["zone_id"]],
                "titel": titel,
                "details": roh["details"],
                "meta": {
                    "typ": roh["typ"],
                    "behoben_um": roh["behoben_um"],
                },
            }
        )

    return eintraege


def _stunden_bucket(iso_text: str) -> str:
    zeit = datetime.fromisoformat(iso_text)
    return zeit.replace(minute=0, second=0, microsecond=0).isoformat()


def _aggregiere_routine_eintraege(eintraege: list[dict]) -> list[dict]:
    gruppen: dict[tuple[str | None, str, str], list[dict]] = {}
    for eintrag in eintraege:
        key = (
            eintrag["meta"].get("blocker_typ"),
            eintrag.get("scope_ref") or "",
            _stunden_bucket(eintrag["zeitstempel"]),
        )
        gruppen.setdefault(key, []).append(eintrag)

    aggregiert: list[dict] = []
    for (blocker_typ, scope_ref, bucket), gruppe in gruppen.items():
        basis = gruppe[0]
        aggregiert.append(
            {
                "id": f"routine:{blocker_typ or 'ohne'}:{scope_ref}:{bucket}",
                "zeitstempel": bucket,
                "typ": "SHADOW_ENTSCHEIDUNG",
                "severity": "ROUTINE",
                "zone_id": basis["zone_id"],
                "zone_name": basis["zone_name"],
                "scope": basis["scope"],
                "scope_ref": scope_ref,
                "betroffene_zonen": basis["betroffene_zonen"],
                "titel": f"{len(gruppe)}x {_blocker_label(blocker_typ)}",
                "details": (
                    "Zusammengefasst fuer "
                    f"{bucket[11:13]}:00-{bucket[11:13]}:59"
                ),
                "meta": {
                    "blocker_typ": blocker_typ,
                    "anzahl": len(gruppe),
                    "aggregiert": True,
                    # T-0388: Flag mitnehmen, sonst liest das Frontend fuer die
                    # ROUTINE-Zeilen einer SCHARFEN Zone `undefined` -> faelsch-
                    # lich "Shadow". Der Wert ist pro Gruppe konstant (gleiche
                    # scope_ref = gleiche Zonen).
                    "autonom_scharf": basis["meta"].get("autonom_scharf"),
                },
            }
        )

    return aggregiert


def _baue_ops_timeline(
    rohdaten: dict[str, list[dict]],
    severity_filter: set[str],
    zone_filter: str | None,
) -> dict:
    eintraege = []
    eintraege.extend(_normalisiere_shadow_eintraege(rohdaten["entscheidungen"], zone_filter))
    eintraege.extend(_normalisiere_ventil_eintraege(rohdaten["ventil_ereignisse"], zone_filter))
    eintraege.extend(_normalisiere_wetter_eintraege(rohdaten["wetter_ereignisse"], zone_filter))
    eintraege.extend(_normalisiere_sensor_eintraege(rohdaten["sensor_warnungen"], zone_filter))

    routine = [e for e in eintraege if e["severity"].lower() == "routine"]
    sichtbare = [e for e in eintraege if e["severity"].lower() != "routine"]

    if "routine" in severity_filter:
        routine_shadow = [e for e in routine if e["typ"] == "SHADOW_ENTSCHEIDUNG"]
        routine_andere = [e for e in routine if e["typ"] != "SHADOW_ENTSCHEIDUNG"]
        sichtbare.extend(_aggregiere_routine_eintraege(routine_shadow))
        sichtbare.extend(routine_andere)
        routine_unterdueckt = 0
    else:
        routine_unterdueckt = len(routine)

    sichtbare = [
        e for e in sichtbare
        if e["severity"].lower() in severity_filter
    ]
    sichtbare.sort(key=lambda e: e["zeitstempel"], reverse=True)

    return {
        "eintraege": sichtbare,
        "aggregiert": {
            "routine_unterdueckt": routine_unterdueckt,
        },
    }


def _ueberlagere_current_aus_messung(
    optima: dict[str, dict], letzter_wert: object | None,
) -> dict[str, dict]:
    """T-0199: legt den live-Sensor-Wert ueber das im 24h-Job-Cache
    persistierte `current` der `optima`-Achsen, damit das Frontend
    auch zwischen Job-Laeufen aktuelle Werte sieht.

    Mapping `sensor_messung` -> `plant_optimum_achse`:
    - feuchte       <- boden_feuchte
    - temperatur    <- boden_temperatur
    - licht_ppfd    <- licht
    - salinitaet    NICHT ueberlagert (T-0385, 08.07.2026): frueher
                    `salinitaet <- boden_fruchtbarkeit`. Das sind aber ZWEI
                    verschiedene FYTA-API-Felder -- `salinity` (EC, mS/cm; Quelle
                    des salinitaet-Achsen-Bands + `current` aus dem
                    PlantOptimumJob) vs. `soil_fertility` (Range 0-6, Einheit
                    noch NICHT verifiziert -- vermutlich Index, nicht mS/cm;
                    landet als boden_fruchtbarkeit in sensor_messung). Egal was
                    soil_fertility genau ist: es ist NICHT dieselbe Achse. ->
                    salinitaet
                    behaelt den korrekten Job-`current` (mS/cm), kein Live-
                    Overlay aus dem Fertility-Index.
    - licht_dli     bleibt unangetastet (kein direktes Sensor-Feld,
                    wird per PlantOptimumJob aus 24h-PPFD aggregiert).

    Nur Achsen, die im `optima`-Dict schon existieren, werden
    aktualisiert (= FYTA-Job hat fuer diese Zone irgendwann
    min_good/max_good gespeichert). Wenn der Live-Wert None ist,
    bleibt der Cache-Wert erhalten — kein Overwrite mit NULL.
    """
    if not optima or letzter_wert is None:
        return optima
    achsen_live = {
        "feuchte": getattr(letzter_wert, "boden_feuchte", None),
        "temperatur": getattr(letzter_wert, "boden_temperatur", None),
        "licht_ppfd": getattr(letzter_wert, "licht", None),
    }
    ergebnis: dict[str, dict] = {}
    for achse, eintrag in optima.items():
        live_wert = achsen_live.get(achse)
        if live_wert is not None and achse != "licht_dli":
            ergebnis[achse] = {**eintrag, "current": live_wert}
        else:
            ergebnis[achse] = eintrag
    return ergebnis


@app.get("/api/health")
async def health():
    """Public-minimaler Health-Check fuer Service-Script und Monitoring.

    T-0140: bewusst nur Liveness-Antwort, ohne Versions- oder Konfig-
    Details. Reichere Health-Daten unter `/api/health/detail` (read-
    geschuetzt).
    """
    return {"ok": True, "zeitstempel": _iso(datetime.now())}


@app.get("/api/health/detail")
async def health_detail():
    """Read-geschuetzter Detail-Health (T-0140).

    Liefert Versions- und Konfig-Status. Fuer Liveness-Probes (z.B.
    service.sh) reicht `/api/health` ohne Token.
    """
    return {
        "ok": True,
        "zeitstempel": _iso(datetime.now()),
        "version": app.version,
        "konfiguriert": _konfig is not None,
        "zonen_anzahl": len(_konfig.zonen) if _konfig else None,
    }


@app.get("/api/ops/request-raten")
async def ops_request_raten():
    """T-0463: Request-Rate pro Route -- Regressionsschutz gegen T-0459.

    Konsument ist `backend/skripte/pruefe_request_raten.py`: das Skript ruft
    zweimal ab und rechnet die Rate aus der Differenz von `gesamt`, also ueber
    sein eigenes Fenster statt ueber `fenster_s` des Servers.

    Rein lesend, veraendert keinen Zaehlerstand. Der Abruf selbst wird
    mitgezaehlt (zwei Requests pro Lauf -- vernachlaessigbar gegen den
    Deckel).
    """
    return _request_raten.schnappschuss()


def _cache_wert_wenn_frisch(
    wert: SensorMessung | None,
    api_jetzt: datetime,
) -> SensorMessung | None:
    """T-0476 (E4): Alterskappung fuer den In-Memory-Fallback.

    `_verarbeiter.hole_letzten_wert(zone_id)` liefert den zuletzt bekannten
    Wert der Zone OHNE jede Alterspruefung (`sensordaten.py:_letzte_werte`).
    Bei langem Prozesslauf kann das ein tagealter Wert sein; das
    Akzeptanzkriterium "nach 48 h verschwindet der Wert von der Karte" galt
    bisher nur aus Betriebszufall (haeufige Restarts leeren den Cache), nicht
    aus Design.

    Schwelle ist derselbe Backstop, den der Entscheidungspfad fuehrt
    (`entscheidung.MAX_FALLBACK_ALTER_STUNDEN`). Seit das Abruf-Fenster auf
    240 min steht, greift der Cache ohnehin nur noch, wenn die Zone > 4 h
    nichts geliefert hat.

    Bewusst EINE Stelle fuer alle drei Aufrufer (/api/zonen, Bulk-Snapshot,
    ML-Endpoint) -- dreimal inline ist dreimal Gelegenheit zum
    Auseinanderlaufen.
    """
    if wert is None:
        return None
    if not isinstance(wert.zeitstempel, datetime):
        # Ohne verwertbaren Zeitstempel ist das Alter unbekannt -- dann
        # nicht kappen, sondern durchreichen wie vor T-0476. Sonst wuerde
        # eine unvollstaendige Messung stumm zu "keine Daten".
        return wert
    if api_jetzt - wert.zeitstempel > timedelta(hours=MAX_FALLBACK_ALTER_STUNDEN):
        return None
    return wert


async def _null_ist_defekt_fuer(
    zone: ZonenKonfig,
    letzter_wert: SensorMessung | None,
) -> bool | None:
    """T-0502 (10.08.): Urteil des 0.0-Guards fuer die Anzeige.

    **Warum das Backend das liefert und nicht das Frontend selbst rechnet.**
    Bis heute trugen beide Seiten dieselbe Regel doppelt: `null_ist_sensordefekt`
    hier und `istWahrscheinlichSensorDefekt` in `sensor-defekt.ts`, bewusst als
    Spiegel gebaut. Mit der Trajektorien-Korrektur braucht das Urteil aber ein
    24-h-Fenster aus der DB -- das Frontend kann es nicht mehr nachbauen, ohne
    eine zweite Wahrheit zu erfinden. Also entscheidet das Backend, das
    Frontend liest nur noch ab (dieselbe Aufteilung wie bei
    `lead_ausgefallen`, T-0532).

    `None` heisst "Frage stellt sich nicht" -- der Wert ist nicht 0.0. Nur im
    Nullfall laeuft die Fenster-Abfrage, sie kostet also im Normalbetrieb
    nichts.
    """
    if letzter_wert is None or letzter_wert.boden_feuchte is None:
        return None
    if float(letzter_wert.boden_feuchte) != 0.0:
        return None
    assert _speicher is not None
    max_24h = await _speicher.max_feuchte_im_fenster(
        zone.zone_id,
        quelle=getattr(letzter_wert.quelle, "value", str(letzter_wert.quelle)),
        vor=letzter_wert.zeitstempel,
    )
    return null_ist_sensordefekt(zone, letzter_wert.quelle, max_24h)


def _ist_lead_ausgefallen(
    zone: ZonenKonfig,
    letzter_wert: SensorMessung | None,
    api_jetzt: datetime,
) -> bool:
    """T-0532: Hat die Zone einen Aggregat-Lead, taugt `letzter_wert` aber
    nicht als dessen Aussage?

    Zwei Wege in denselben Zustand -- das Akzeptanzkriterium lautet "Lead
    ausserhalb des Frischefensters", nicht "Wert von fremdem Sensor":

    (1) Der Wert stammt von einem ANDEREN Sensor. Der haeufige Fall: der
        Verarbeiter-Cache (`sensordaten.py:_letzte_werte`) ist nur nach
        zone_id verschluesselt, es gewinnt wer zuletzt gefunkt hat -- bei
        FYTA alle 15 min gegen stuendlichen Gardena-Lead also ein FYTA.
    (2) Der Wert stammt vom Lead, ist aber aelter als das Aggregat-Fenster.
        Tritt auf, wenn die uebrigen Sensoren der Zone still sind; bei sechs
        dauerhaft ausser Hub-Reichweite liegenden FYTA kein exotischer Fall.
        Ohne (2) haette ein 3 h alter Lead-Wert weiter eine Kritisch-Meldung
        getragen, nur eben ehrlich beschriftet.

    Der Lead-Check steht bewusst VORNE: fuer die grosse Mehrheit der Zonen
    (kein Lead konfiguriert) ist die Antwort ohne jede Rechnerei False.

    T-0476: Das Frischefenster in (2) ist nicht mehr ein eigener Vergleich,
    sondern `speicher.ist_messung_verwendbar` -- dieselbe Funktion, die auch
    das Abruf-Fenster der Anzeige setzt. Vorher verglichen beide Seiten
    getrennt gegen 90 min, und der Entscheidungspfad giesst seit T-0383 auf
    bis zu 240 min alten Werten: die Karte zeigte den Lead-Wert und meldete
    daneben "Lead ausgefallen", waehrend das Ventil auf genau diesem Wert
    oeffnete. Weg (1) bleibt unveraendert -- T-0532s Substanz ist "keine
    stille Sensor-Substitution", und die haengt nicht am Alter.
    """
    if not zone.aggregat_lead_geraet or letzter_wert is None:
        return False
    if letzter_wert.geraet_id != zone.aggregat_lead_geraet:
        return True
    return not ist_messung_verwendbar(letzter_wert.zeitstempel, api_jetzt)


# T-0573 AK6: Obergrenze fuer den ANZEIGE-Rueckfall, falls eine Zone kein
# eigenes `ausfall_schwelle_stunden` gesetzt hat. 48 h ist derselbe
# Backstop, den `_cache_wert_wenn_frisch`/`MAX_FALLBACK_ALTER_STUNDEN`
# fuer den Entscheidungspfad ziehen -- keine dritte Zahl erfinden.
ANZEIGE_RUECKFALL_STUNDEN_DEFAULT = 48


async def _letzter_bekannter_bulk(
    zonen: list, api_jetzt: datetime,
) -> dict:
    """T-0573 AK6: letzter bekannter Messwert je Zone, OHNE Frische-Fenster.

    Eine Query fuer alle Zonen (Bulk-Pattern), danach pro Zone gegen deren
    eigenes `ausfall_schwelle_stunden` gekappt. Rein fuer die Anzeige --
    kein Entscheidungspfad liest das Ergebnis.
    """
    if _speicher is None or not zonen:
        return {}
    grenzen = {
        z.zone_id: (
            z.ausfall_schwelle_stunden or ANZEIGE_RUECKFALL_STUNDEN_DEFAULT
        )
        for z in zonen
    }
    weitestes = max(grenzen.values())
    try:
        roh = await _speicher.letzte_messung_aggregiert_bulk(
            list(grenzen.keys()),
            fenster_minuten=int(weitestes * 60),
            jetzt=api_jetzt,
        )
    except Exception:  # noqa: BLE001
        logger.exception("anzeige.letzter_bekannter_fehlgeschlagen")
        return {}
    ergebnis: dict = {}
    for zid, m in roh.items():
        if m is None:
            continue
        alter_h = (api_jetzt - m.zeitstempel).total_seconds() / 3600.0
        # Jenseits der Zonen-Schwelle ist es ein echter Ausfall; dort ist
        # "keine Daten" die richtige Aussage, nicht ein Uralt-Wert.
        if alter_h <= grenzen.get(zid, ANZEIGE_RUECKFALL_STUNDEN_DEFAULT):
            ergebnis[zid] = m
    return ergebnis


def _letzter_bekannter_felder(
    letzter_wert: SensorMessung | None,
    letzter_bekannter: SensorMessung | None,
    api_jetzt: datetime,
) -> dict:
    """T-0573 AK6: Anzeige-Rueckfall auf den letzten bekannten Messwert.

    Nur belegt, wenn der frische Pfad NICHTS geliefert hat -- sonst waere
    es eine zweite Wahrheit neben `aktuelle_feuchte`.

    Die Zone entscheidet selbst, wie lange ein alter Wert noch etwas
    aussagt: `ausfall_schwelle_stunden` ist der Pro-Zone-Override, den
    T-0214 fuer genau diese schubweise syncenden Sensoren gesetzt hat
    (96 h fuer fuchsie/pilea/mandevilla_maxi). Jenseits davon ist es ein
    echter Ausfall, und "keine Daten" ist die richtige Aussage.
    """
    leer = {
        "letzter_bekannter_wert": None,
        "letzter_bekannter_zeit": None,
        "letzter_bekannter_alter_h": None,
        "letzter_bekannter_geraet_id": None,
    }
    if letzter_wert is not None or letzter_bekannter is None:
        return leer
    if letzter_bekannter.boden_feuchte is None:
        return leer
    alter_h = (
        api_jetzt - letzter_bekannter.zeitstempel
    ).total_seconds() / 3600.0
    return {
        "letzter_bekannter_wert": letzter_bekannter.boden_feuchte,
        "letzter_bekannter_zeit": _iso(letzter_bekannter.zeitstempel),
        "letzter_bekannter_alter_h": round(alter_h, 2),
        "letzter_bekannter_geraet_id": letzter_bekannter.geraet_id,
    }


def _baue_zone_dict(
    zone: ZonenKonfig,
    letzter_wert: SensorMessung | None,
    sensoren_pro_geraet: list[SensorMessung],
    warnungen: list[dict],
    optima_zone: dict[str, dict],
    api_jetzt: datetime,
    null_ist_defekt: bool | None = None,
    letzter_bekannter: SensorMessung | None = None,
) -> dict:
    """T-0200: Single-Source-of-Truth fuer das Zone-Dict in `/api/zonen`
    und `/api/dashboard-snapshot`. Vorher 1× pro Zone inlined — bei zwei
    Endpoints divergiert das schnell.

    Vertrag: das Dict-Format ist Teil der oeffentlichen API. Felder
    NICHT umbenennen oder weglassen ohne Frontend-Migration.
    """
    # T-0135 (H-4 Stufe 1b): Werte Regime-aware. Bei Zonen ohne
    # feuchte_regime sind die Werte identisch wie heute (Backward-
    # Compat). Sobald Regimes konfiguriert sind, liefert das Frontend
    # automatisch den aktuell gueltigen Wert.
    from bewaesserung.modelle import (
        aktives_regime as _aktives_regime,
        effektiv_feuchte_kritisch as _eff_kritisch,
        effektiv_optimum_max as _eff_opt_max,
        effektiv_optimum_min as _eff_opt_min,
        effektiv_schwelle_max as _eff_sch_max,
        effektiv_schwelle_min as _eff_sch_min,
    )
    # T-0221: AquaBloom-Saison-Logik wiederverwenden (SSoT) statt sie
    # hier zu duplizieren -- `_baue_zone_dict` exponiert nur das Ergebnis.
    from bewaesserung.aquabloom_job import (
        _in_saison as _ab_in_saison,
        _ist_konfiguriert as _ab_konfiguriert,
    )
    _aktiv = _aktives_regime(zone, api_jetzt)
    _ab_konf = _ab_konfiguriert(zone)
    # T-0532: Quelle von `aktuelle_feuchte` ehrlich exponieren.
    #
    # `letzte_messung_aggregiert(_bulk)` gibt bei konfiguriertem Lead
    # ausserhalb des Abruf-Fensters (seit T-0476: 240 min, davor 90) bewusst
    # None zurueck (T-0384, KEIN
    # stiller Median-Fallback). Beide Aufrufer fangen dieses None aber mit
    # `_verarbeiter.hole_letzten_wert(zone_id)` ab -- und dieser Cache ist
    # NUR nach zone_id verschluesselt (`sensordaten.py:_letzte_werte`):
    # es gewinnt, wer zuletzt gefunkt hat. Damit war der Fallback, den
    # T-0384 im Aggregat geschlossen hat, eine Ebene darueber wieder offen.
    #
    # Realfall 09.08. waldblumenhain: der Gardena-Lead aaaa0001 funkt
    # stuendlich und hatte zwei Luecken (07:40->10:40, 16:40->18:40, beide
    # > 90 min); die beiden FYTA funken alle 15 min. Ergebnis: das Band
    # meldete "kritische Schwelle 25% unterschritten" auf 18 % eines FYTA,
    # waehrend der Lead bei 40 stand. Genau die Sensoren, gegen die der
    # Lead gesetzt wurde (T-0332/T-0385-Skalenbruch), lieferten den Alarm.
    #
    # Der Wert bleibt sichtbar -- eine leere Karte waere auch nicht
    # ehrlicher. Aber die Karte sagt jetzt, WOHER er kommt, und das
    # Frontend darf daraus keine Schwellen-Aussage mehr ableiten.
    # Zwei Wege in denselben Zustand, beide muessen abgedeckt sein -- das
    # Akzeptanzkriterium lautet "Lead ausserhalb des Frischefensters", nicht
    # "Wert von fremdem Sensor":
    #   (1) Der Cache haelt einen FREMDEN Sensor. Der haeufige Fall (FYTA
    #       alle 15 min gegen stuendlichen Gardena-Lead).
    #   (2) Der Cache haelt den LEAD selbst, aber veraltet. Tritt auf, wenn
    #       die uebrigen Sensoren der Zone still sind -- bei sechs dauerhaft
    #       ausser Hub-Reichweite liegenden FYTA kein exotischer Fall.
    #       Ohne (2) haette ein alter Lead-Wert weiter "kritisch" ausgeloest,
    #       nur eben ehrlich beschriftet -- das waere am Kriterium vorbei.
    _feuchte_geraet_id = letzter_wert.geraet_id if letzter_wert else None
    _lead_ausgefallen = _ist_lead_ausgefallen(zone, letzter_wert, api_jetzt)
    return {
        "zone_id": zone.zone_id,
        "name": zone.name,
        "modus": zone.modus.value,
        # T-0370: scharf vs. Shadow ehrlich exponieren. 3-stufige Logik
        # (T-0334): globales ventilsteuerung_aktiv UND modus=automatik UND
        # auto_loop_opt_in. Zonen-Teil zentral in ist_auto_loop_zone --
        # gleiche Wahrheit wie der Auto-Loop-Filter (_baue_auto_loop_kanaele).
        # modus=automatik ohne autonom_scharf = Shadow-Entscheidungen.
        "autonom_scharf": bool(
            _konfig is not None
            and _konfig.ventilsteuerung_aktiv
            and ist_auto_loop_zone(zone)
        ),
        "feuchte_schwelle_min": _eff_sch_min(zone, api_jetzt),
        "feuchte_schwelle_max": _eff_sch_max(zone, api_jetzt),
        "feuchte_kritisch": _eff_kritisch(zone, api_jetzt),
        "ventil_kanal": zone.ventil_kanal,
        # T-0221: DSWC-Geraet (Multi-DSWC, T-0203). None = primaere DSWC.
        # Frontend leitet daraus "DSWC 1/2" + Bewaesserungs-Gruppierung ab.
        "ventil_geraet_id": zone.ventil_geraet_id,
        "optimum_feuchte_min": _eff_opt_min(zone, api_jetzt),
        "optimum_feuchte_max": _eff_opt_max(zone, api_jetzt),
        "feuchte_regime": [r.model_dump() for r in zone.feuchte_regime],
        "feuchte_regime_aktiv": _aktiv.name if _aktiv else None,
        "aktuelle_feuchte": letzter_wert.boden_feuchte if letzter_wert else None,
        "boden_temperatur": letzter_wert.boden_temperatur if letzter_wert else None,
        "batterie": letzter_wert.batterie_prozent if letzter_wert else None,
        "licht": letzter_wert.licht if letzter_wert else None,
        "licht_intensitaet": letzter_wert.licht_intensitaet if letzter_wert else None,
        "boden_fruchtbarkeit": letzter_wert.boden_fruchtbarkeit if letzter_wert else None,
        "quelle": letzter_wert.quelle.value if letzter_wert and letzter_wert.quelle else None,
        # T-0532: geraet_id, aus der `aktuelle_feuchte` stammt.
        # "aggregat:<n>" bei Median ueber mehrere Sensoren (Zone ohne Lead).
        # Frontend loest die ID ueber `sensor_namen` in Klartext auf.
        "feuchte_geraet_id": _feuchte_geraet_id,
        # T-0532: Zone hat einen konfigurierten Aggregat-Lead, der Wert
        # stammt aber NICHT von ihm. Frontend: kein KRITISCH ableiten,
        # sondern eigener Zustand mit Quellenangabe.
        "lead_ausgefallen": _lead_ausgefallen,
        # T-0502: Urteil des 0.0-Guards, vom Backend berechnet. None = der
        # Wert ist nicht 0.0, die Frage stellt sich nicht. Das Frontend darf
        # daraus KEINE eigene Regel ableiten -- die Trajektorie steckt in der
        # DB, nicht im Zone-Dict.
        "null_ist_defekt": null_ist_defekt,
        # T-0532: konfigurierter Lead der Zone (None = kein Lead). Damit
        # kann das Frontend den ausgefallenen Sensor benennen, nicht nur
        # den Ersatz.
        "aggregat_lead_geraet": zone.aggregat_lead_geraet,
        "letztes_update": _iso(letzter_wert.zeitstempel) if letzter_wert else None,
        # T-0573 AK6: der Gegenfall zur unterdrueckten Prognose. Faellt der
        # Wert aus dem Abruf-Fenster (`AGGREGAT_FALLBACK_FENSTER_MIN`,
        # 240 min), stand auf der Karte bisher nur "-" -- sie VERSCHWIEG
        # einen Messwert, den sie hat. Gemessen am 09.09.2026 07:16 traf
        # das fuenf Zonen: pilea 4,1 h / kroton 4,2 h / zitrus_ii 4,3 h /
        # fuchsie 10,1 h / avocado 10,3 h. Alles Bluetooth-only-FYTA, die
        # per Design schubweise syncen (T-0214).
        #
        # Diese Felder sind AUSDRUECKLICH nur fuer die Anzeige. Das
        # Abruf-Fenster bleibt unangetastet: T-0476 hat Anzeige und
        # Entscheidung bewusst gekoppelt, und ein 10 h alter Wert darf kein
        # Ventil oeffnen. `aktuelle_feuchte` bleibt deshalb None -- die
        # Zone bekommt weiter keine Empfehlung, sie zeigt nur nicht mehr
        # "-", wo sie "10 % · vor 10 h" sagen koennte.
        **_letzter_bekannter_felder(letzter_wert, letzter_bekannter, api_jetzt),
        "logging_einheit": zone.logging_einheit,
        "logging_optionen_ml": list(zone.logging_optionen_ml or []),
        "pre_soak_min": zone.pre_soak_min,
        "pre_soak_pause_min": zone.pre_soak_pause_min,
        "sensoren": [
            {
                "geraet_id": m.geraet_id,
                "quelle": m.quelle.value if m.quelle else None,
                # T-0204: Klartextname falls in `sensor_namen` gepflegt,
                # sonst None -> Frontend faellt auf gekuerzte ID zurueck.
                "name": (_konfig.sensor_namen.get(m.geraet_id)
                         if _konfig is not None else None),
                "boden_feuchte": m.boden_feuchte,
                "boden_temperatur": m.boden_temperatur,
                "zeitstempel": _iso(m.zeitstempel),
            }
            for m in sensoren_pro_geraet
        ],
        # T-0221-Folge: Klartextnamen ALLER konfigurierten Sensoren
        # (geraet_id -> Name). `sensoren` oben listet nur die in den
        # letzten 6h aktiven -- ein Sensor mit laengerer Funkstille
        # (FYTA-Cadence-Drift) faellt da raus, taucht aber im 48h-Chart
        # noch auf. Mit dieser Map loest das Frontend auch solche stale
        # Sensor-Linien auf einen Namen statt der rohen geraet_id auf.
        "sensor_namen": (
            dict(_konfig.sensor_namen) if _konfig is not None else {}
        ),
        "offene_warnungen": warnungen,
        # T-0199: `current` wird mit der juengsten sensor_messung live
        # ueberschrieben, damit das Frontend immer aktuelle Werte sieht
        # — unabhaengig vom 24h-Cadence des PlantOptimumJob.
        "optima": _ueberlagere_current_aus_messung(optima_zone, letzter_wert),
        # T-0211b: Ausschluss-Fenster pro Zone, damit das Frontend die
        # Phase im Chart visuell markieren kann (Schraffur + Tooltip).
        # Konfig kommt aus `ml_ausschluss_fenster` in default.yaml — heute
        # auch fuer Heuristik-Pause genutzt (T-0211a).
        "ausschluss_fenster": _ausschluss_fenster_pro_zone(zone.zone_id),
        # T-0221: Flaeche + Topf/Beet fuer die Karten-Sub-Zeile.
        "flaeche_m2": zone.flaeche_m2,
        "ist_topf": zone.ist_topf,
        # T-0221: AquaBloom-Konfig (T-0168). None wenn die Zone keine
        # AquaBloom-Pumpe hat. `aktiv` = konfiguriert UND in Saison.
        "aquabloom_konfig": (
            {
                "intervall_stunden": zone.aquabloom_pumpen_intervall_stunden,
                "dauer_sekunden": zone.aquabloom_pumpen_dauer_sekunden,
                "anker_zeitstempel": (
                    _iso(zone.aquabloom_anker_zeitstempel)
                    if zone.aquabloom_anker_zeitstempel else None
                ),
                "aktiv": _ab_in_saison(zone, api_jetzt),
            }
            if _ab_konf else None
        ),
    }


# T-0577: `_hole_sensor_historie` und `SENSOR_HISTORIE_TAGE` sind nach
# `ml/vorhersage.py` umgezogen (`_hole_guete_kontext`, `GUETE_HISTORIE_TAGE`) --
# das Guete-Urteil entsteht jetzt dort, fuer alle Konsumenten.


def _ml_eintrag_dict(v) -> dict:
    """T-0573: EINE Prognose als API-Dict, inklusive ihrer Guete.

    **T-0577: das Urteil wird hier nur noch GELESEN, nicht mehr gerechnet.**
    Bis 16.09. rief diese Funktion `bewerte_prognose` selbst auf. Damit hatte
    die ANZEIGE ein Guete-Urteil und die ENTSCHEIDUNGS-ENGINE keins -- sie
    rechnete mit genau der Zahl Trigger und Dosis, die die Karte bereits als
    veraltet ausblendete. Jetzt haengt `live_vorhersage` das Urteil einmal an,
    und API wie Engine lesen dasselbe. Eine Wahrheit statt zwei.

    Bewusst gemeinsam fuer `/api/ml/vorhersage/{id}` und den
    Dashboard-Snapshot: die beiden bauten dieses Dict bisher in zwei
    Kopien: eine zweite Wahrheit genau in dem Feld, das der T-0200-Vertrag
    als byte-identisch zusichert. Ein Frische-Urteil in nur einer der
    beiden Kopien haette die Karte je nach Endpoint anders antworten
    lassen.
    """
    eintrag: dict = {
        "feuchte_prognose": v.feuchte_prognose,
        "feuchte_aktuell": v.feuchte_aktuell,
    }
    # T-0046: Quantile-Baender, wenn verfuegbar.
    if v.q10 is not None:
        eintrag["q10"] = v.q10
    if v.q90 is not None:
        eintrag["q90"] = v.q90
    # T-0040: Top-Features, nur wenn angefordert + Modell geliefert
    if v.top_features is not None:
        eintrag["top_features"] = [
            {
                "name": f.name,
                "wert": f.wert,
                "beitrag": f.beitrag,
                "skala": f.skala,
            }
            for f in v.top_features
        ]

    feature_zeit = getattr(v, "feature_zeitstempel", None)
    eintrag["feature_zeitstempel"] = _iso(feature_zeit) if feature_zeit else None
    eintrag["feature_alter_h"] = getattr(v, "feature_alter_h", None)
    # T-0573: Rueckstand auf die juengste Messung -- die Groesse, an der
    # das Urteil haengt (siehe `PROGNOSE_MAX_RUECKSTAND_STUNDEN`).
    eintrag["feature_rueckstand_h"] = getattr(v, "feature_rueckstand_h", None)
    eintrag["geraet_id"] = getattr(v, "inferenz_geraet_id", None)
    eintrag["gueltig"] = getattr(v, "gueltig", True)
    eintrag["ungueltig_grund"] = getattr(v, "ungueltig_grund", None)
    return eintrag


def baue_bilanz_dict(bilanz, fenster: str) -> dict:
    """T-0575: die Bilanz-Response an einer pruefbaren Stelle.

    Vorher wurde dieses Dict inline im Endpoint gebaut. Ein Feld, das das
    Modell fuellt und die Response nicht weiterreicht, aendert an der Kachel
    nichts -- und genau das war der Zustand: `bewaesserung_indikativ` wurde
    berechnet und auf dem Weg zur Anzeige eingeebnet
    ([[fehlerpattern_detektor_ohne_konsument]]). Als eigene Funktion laesst
    sich die Naht testen, ohne den halben Server hochzufahren.
    """
    return {
        "zone_id": bilanz.zone_id,
        "fenster": fenster,
        "fenster_von": _iso(bilanz.fenster_von),
        "fenster_bis": _iso(bilanz.fenster_bis),
        "flaeche_m2": bilanz.flaeche_m2,
        "bewaesserung_liter": bilanz.bewaesserung_liter,
        "regen_liter": bilanz.regen_liter,
        "zugefuehrt_liter": bilanz.zugefuehrt_liter,
        "verdunstet_liter": bilanz.verdunstet_liter,
        "bilanz_liter": bilanz.bilanz_liter,
        "quelle_niederschlag": bilanz.quelle_niederschlag,
        # T-0575: WELCHE Seite unsicher ist, nicht nur DASS etwas unsicher ist.
        "indikativ": bilanz.indikativ,
        "bewaesserung_indikativ": bilanz.bewaesserung_indikativ,
    }


def _ausschluss_fenster_pro_zone(zone_id: str) -> list[dict]:
    """T-0211b: Liefert die ml_ausschluss_fenster fuer eine Zone als
    serialisierbares Dict-Liste. Frontend rendert Recharts-ReferenceArea
    mit Schraffur + Tooltip-Hint waehrend dieser Zeitraeume.
    """
    if _konfig is None:
        return []
    return [
        {
            "von": _iso(f.von),
            "bis": _iso(f.bis),
            "grund": f.grund,
            # T-0304: Zweck unterscheiden -- "sensor_kalibrierung" (Werte
            # unzuverlaessig) vs "event_ignore" (Sensor ok, nur Kanal-Events
            # ignoriert, z.B. Magerwiese-Gras T-0300). Frontend rendert Label
            # + Empfehlungs-Flag entsprechend.
            "zweck": f.effektiver_zweck,
            # T-0386: geraet_id != None -> das Fenster gilt nur fuer diesen
            # Sensor (nicht die ganze Zone). Frontend kann das im Tooltip
            # kennzeichnen; None = ganze Zone (Backward-Compat).
            "geraet_id": f.geraet_id,
        }
        for f in _konfig.ml_ausschluss_fenster
        if f.zone_id == zone_id
    ]


def _baue_warnungen_pro_zone(warnungen) -> dict[str, list[dict]]:
    """T-0200: Hilfsmapping (warnung_zeile -> dict) zentral, damit
    `/api/zonen` und `/api/dashboard-snapshot` byte-identische Strings
    rendern."""
    out: dict[str, list[dict]] = {}
    for w in warnungen:
        out.setdefault(w.zone_id, []).append({
            "typ": w.typ.value,
            "zeitstempel": _iso(w.zeitstempel),
            "details": w.details,
        })
    return out


def _messung_zu_dict(m: SensorMessung) -> dict:
    """T-0200: Sensor-Messung-Format aus `/api/zonen/{id}/messwerte`
    wiederverwenden, damit der Snapshot identische Felder liefert.

    T-0211c: `geraet_id` + `quelle` mitliefern, damit das Frontend
    pro-Sensor-Linien im Chart zeichnen kann (statt einer Misch-Linie
    durch die Roh-Liste aller Sensoren der Zone — bei Multi-Sensor-
    Zonen wie waldblumenhain wirkt das wie wilde Feuchte-Spruenge).
    """
    return {
        "zeitstempel": _iso(m.zeitstempel),
        "boden_feuchte": m.boden_feuchte,
        "boden_temperatur": m.boden_temperatur,
        "umgebungs_temperatur": m.umgebungs_temperatur,
        "licht_intensitaet": m.licht_intensitaet,
        # T-0190: FYTA-Felder im Verlaufs-Endpoint mitliefern.
        "licht": m.licht,
        "boden_fruchtbarkeit": m.boden_fruchtbarkeit,
        "batterie_prozent": m.batterie_prozent,
        # T-0211c: Pro-Sensor-Zuordnung fuer Multi-Linien-Chart.
        "geraet_id": m.geraet_id,
        "quelle": m.quelle.value if m.quelle else None,
    }


@app.get("/api/zonen")
async def zonen_uebersicht():
    """Alle Zonen mit aktuellem Status."""
    assert _konfig and _speicher and _verarbeiter
    # T-0125/T-0183: offene Sensor-Warnungen pro Zone gruppieren, damit
    # das Frontend pro Karte ein Badge rendern kann (z.B. SENSOR_EINGEFROREN,
    # Sensor-Ausfall) ohne separaten Endpoint-Call. Single-Query statt N+1.
    _warnungen_pro_zone = _baue_warnungen_pro_zone(
        await _speicher.offene_sensor_warnungen(),
    )
    # T-0196: Multi-Achsen-Optima pro Zone (Single-Query, kein N+1).
    # Liefert dict[zone, dict[achse, {min_good, max_good, min_akzeptabel,
    # max_akzeptabel, einheit, aktualisiert, quelle}]]. Frontend kann
    # daraus Optimum-Baender (z.B. Licht, Temperatur) im Chart rendern.
    _optima_pro_zone = await _speicher.hole_plant_optima_achsen()
    _api_jetzt = datetime.now()
    # T-0573 AK6: isomorph zum Snapshot -- beide Endpoints bauen ueber
    # `_baue_zone_dict` und duerfen nicht driften.
    _letzter_bekannter = await _letzter_bekannter_bulk(
        list(_konfig.zonen), _api_jetzt,
    )
    ergebnis = []
    for zone in _konfig.zonen:
        # T-0179c: Aggregat ueber alle Sensoren der Zone (Median).
        # Bei einem Sensor identisch zum Legacy-Verhalten.
        # In-Memory-Cache wird nur fuer Single-Sensor-Zonen genutzt;
        # bei Multi-Sensor-Zonen ist DB-Aggregat verlaesslicher als
        # der Verarbeiter-Cache (der hat keine Sensor-Streuung).
        # T-0476: Abruf-Fenster = derselbe Horizont, auf dem die Automatik
        # giesst (`AGGREGAT_FALLBACK_FENSTER_MIN`, 240 min). Mit dem alten
        # 90-min-Default genuegte EIN verpasster Stunden-Beat des Gardena-
        # Leads, um die Karte zu leeren (Realfall 01.08., bambuswald).
        letzter_wert = await _speicher.letzte_messung_aggregiert(
            zone.zone_id,
            fenster_minuten=AGGREGAT_FALLBACK_FENSTER_MIN,
            jetzt=_api_jetzt,
        )
        if not letzter_wert:
            # Fallback auf Verarbeiter-Cache (nur wenn DB leer), gekappt
            # auf 48 h -- siehe `_cache_wert_wenn_frisch`.
            letzter_wert = _cache_wert_wenn_frisch(
                _verarbeiter.hole_letzten_wert(zone.zone_id), _api_jetzt,
            )
        # T-0179c: pro-Geraet-Liste fuer Frontend-Diagnose
        sensoren_pro_geraet = await _speicher.letzte_messungen_pro_geraet(
            zone.zone_id,
        )
        ergebnis.append(_baue_zone_dict(
            zone,
            letzter_wert,
            sensoren_pro_geraet,
            _warnungen_pro_zone.get(zone.zone_id, []),
            _optima_pro_zone.get(zone.zone_id, {}),
            _api_jetzt,
            null_ist_defekt=await _null_ist_defekt_fuer(zone, letzter_wert),
            letzter_bekannter=_letzter_bekannter.get(zone.zone_id),
        ))
    return ergebnis


@app.get("/api/zonen/{zone_id}/messwerte")
async def messwerte(
    zone_id: str,
    # T-0557: Plausibilitaetsgrenze -- und bewusst NICHT als DoS-Schutz
    # verkauft. `?stunden=1000000` erzeugte vorher einen Scan ueber die
    # gesamte Messhistorie; das faengt die Grenze ab. Sie schuetzt aber
    # nicht gegen einen grossen Scan an sich, denn der `von`/`bis`-Modus
    # unten nimmt weiterhin jede Spanne entgegen, und die DB haelt ohnehin
    # nur wenige Monate. Der wirksame Schutz waere ein Zeilen-Limit fuer
    # BEIDE Modi -- das aendert den Antwort-Vertrag (stille Kuerzung der
    # Charts) und gehoert deshalb in einen eigenen Task, nicht hierher.
    # 3 Jahre laesst jeden realen Aufrufer durch (groesstes Frontend-Fenster
    # ist 720 h) und faengt nur die absurden Werte.
    stunden: int = Query(
        24, ge=1, le=26280,
        description="Zeitraum in Stunden (ignoriert wenn von/bis gesetzt)",
    ),
    von: str | None = Query(None, description="ISO-Zeitstempel Start (optional)"),
    bis: str | None = Query(None, description="ISO-Zeitstempel Ende (optional)"),
):
    """Sensormessungen fuer eine Zone.

    T-0184: Range-Modus (von + bis) zusaetzlich zum Sliding-Window
    (`stunden`), damit der DetailsDrawer Sensor-Verlauf ±2 h um ein
    Event laden kann.
    """
    assert _speicher
    if von or bis:
        von_dt = _parse_zeitgrenze(von)   # T-0565
        bis_dt = _parse_zeitgrenze(bis)
        messungen = await _speicher.hole_messungen(zone_id, von=von_dt, bis=bis_dt)
    else:
        von_dt = datetime.now() - timedelta(hours=stunden)
        messungen = await _speicher.hole_messungen(zone_id, von=von_dt)
    return [_messung_zu_dict(m) for m in reversed(messungen)]  # Chronologisch


@app.get("/api/zonen/{zone_id}/ereignisse")
async def ereignisse(
    zone_id: str,
    # T-0557: siehe `messwerte` -- gleiche Grenze, gleicher Grund.
    stunden: int = Query(24, ge=1, le=26280, description="Zeitraum in Stunden"),
):
    """Ventilereignisse fuer eine Zone."""
    assert _speicher
    jetzt = datetime.now()
    von = jetzt - timedelta(hours=stunden)
    ergs = await _speicher.hole_ventil_ereignisse(zone_id, von=von, bis=jetzt)
    return [
        {
            "zeitstempel": _iso(e.zeitstempel),
            "aktion": e.aktion.value,
            "dauer_sekunden": e.dauer_sekunden,
            "ausloser": e.ausloser.value,
        }
        for e in ergs
    ]


@app.get("/api/zonen/{zone_id}/giess-historie")
async def giess_historie(
    zone_id: str,
    tage: int = Query(14, ge=1, le=90, description="Zeitraum in Tagen"),
):
    """T-0335: Giess-Laeufe einer Zone (wann/wie-lange/wie/Ausloeser).

    Gruppiert rohe Ventil-Events zu Laeufen -- Pre-Soaks (Puls + Haupt) als EINEN
    Lauf (Backend-Marker `lauf_gruppe`), Einzellaeufe getrennt; ignoriert/Cross-
    Spray markiert (`zaehlt=false`). Single-Source-Gruppierung in
    `giess_historie.gruppiere_giess_laeufe` (pytest-getestet).
    """
    from .giess_historie import gruppiere_giess_laeufe
    assert _speicher
    jetzt = datetime.now()
    von = jetzt - timedelta(days=tage)
    ergs = await _speicher.hole_ventil_ereignisse(zone_id, von=von, bis=jetzt)
    return gruppiere_giess_laeufe(ergs)


@app.get("/api/giess-historie")
async def giess_historie_alle(
    tage: int = Query(14, ge=1, le=90, description="Zeitraum in Tagen"),
):
    """T-0335: Giess-Laeufe ALLER giessbaren Zonen, chronologisch gemischt.

    Standard-Ansicht (ohne Zonen-Filter). Pro Zone gruppiert, dann ueber
    `aggregiere_zonen_laeufe` zusammengefuehrt: ein serieller Strang (mehrere
    Zonen am selben Ventil, z.B. Bambus) erscheint als EIN Lauf mit mehreren
    `zone_ids` (Dedup-Anker = ventil_id + Start-Sekunde), keine Doppelzaehlung.
    """
    from .giess_historie import aggregiere_zonen_laeufe, gruppiere_giess_laeufe
    assert _speicher and _konfig
    jetzt = datetime.now()
    von = jetzt - timedelta(days=tage)
    alle: list[dict] = []
    for zone in _konfig.zonen:
        if zone.ventil_kanal is None:
            continue
        ergs = await _speicher.hole_ventil_ereignisse(
            zone.zone_id, von=von, bis=jetzt,
        )
        alle.extend(gruppiere_giess_laeufe(ergs))
    return aggregiere_zonen_laeufe(alle)


@app.get("/api/zonen/{zone_id}/empfehlung-jetzt")
async def empfehlung_jetzt(
    zone_id: str,
    sicherheits_tage: float | None = Query(
        default=None,
        ge=0.5, le=30.0,
        description=(
            "T-0075: Override fuer den Sicherheitsabstand-Tagebudget der "
            "kausalen Empfehlung. Beispiel: 'Ich bin 7 Tage weg' → groessere "
            "Dauer-Empfehlung, deckt 7 Tage statt Default 3."
        ),
    ),
):
    """T-0066/T-0075: Dry-Run-Empfehlung fuer eine Zone ohne DB-Writes.

    Liefert, was das System jetzt bewaessern wuerde (Heuristik-Dauer +
    Liter + Grund), kausale Aussage (Welkepunkt, Reserve-Tage,
    Empfehlungs-Typ, Erklaerungs-Text), optional ML-Empfehlung +
    Drift-Ampel. Liest nur — kein `entscheidung_log`, kein
    `ml_dauer_vorschlag`, kein `ventil_ereignis` wird dabei geschrieben.

    Hybrid Stufe 1: zusaetzlich physikalische Trocknungs-Prognose
    (`prognose_physik_*h`) als Read-only-Diagnose neben der ML-Prognose.
    Beeinflusst NICHT `soll_bewaessern`, `empfehlungs_typ`,
    `dauer_s_empfehlung` -- nur Beobachtung.

    Cache-Header: 60 s — Dashboard-Polling soll den Server nicht fluten.
    """
    assert _motor and _konfig
    zone_existiert = any(z.zone_id == zone_id for z in _konfig.zonen)
    if not zone_existiert:
        raise HTTPException(
            status_code=404, detail=f"Zone {zone_id} nicht gefunden",
        )
    empfehlung = await _motor.vorhersage_zone(
        zone_id, sicherheits_tage_override=sicherheits_tage,
    )
    # Hybrid Stufe 1: read-only Physik-Diagnose anhaengen.
    # T-0270 (28.05.): Helper lebt in `ml/physik_diagnose.py`, damit
    # AuditJob denselben Code aufrufen kann.
    from bewaesserung.ml.physik_diagnose import augmentiere_physik_prognose
    await augmentiere_physik_prognose(
        zone_id=zone_id, empfehlung=empfehlung,
        speicher=_speicher, konfig=_konfig,
        wetter_manager=_wetter_manager,
    )
    return JSONResponse(
        content=empfehlung.model_dump(mode="json"),
        headers={"Cache-Control": "max-age=60"},
    )


@app.get("/api/ml/physik-bias")
async def ml_physik_bias(
    tage: int = Query(
        default=14, ge=1, le=90,
        description="Rueckblick-Fenster in Tagen.",
    ),
    regime: str | None = Query(
        default=None,
        pattern=(
            "^(trocknung|giess_recovery|regen|regen_unbekannt|"
            "ausgeschlossen_crossspray|ausgeschlossen_mlausschluss)$"
        ),
        description=(
            "T-0349: optionaler Regime-Filter. 6h-Metriken werden auf "
            "regime_6h, 24h-Metriken auf regime_24h gefiltert. "
            "Ohne Param: Bestandsverhalten (alle Regimes gemischt)."
        ),
    ),
):
    """T-0270 Bias-Audit: pro Zone die MAE von ML/Heuristik-Prognose
    vs Physik-Prognose ueber die letzten N Tage.

    Liest aus `empfehlungs_audit` alle evaluierten Snapshots
    (`evaluiert_am IS NOT NULL`) und berechnet pro Zone:
      - n: Anzahl evaluierter Empfehlungen
      - mae_ml_6h:    mean |prognose_6h - ist_feuchte_6h| (pp)
      - mae_physik_6h: mean |prognose_physik_6h - ist_feuchte_6h| (pp)
      - mae_ml_24h:    analog
      - mae_physik_24h: analog
      - bias_pp:      systematischer Offset
                      mean(prognose_6h - prognose_physik_6h)
                      (positiv = ML prognostiziert hoeher als Physik)
      - T-0353/T-0351 additiv: mae_statespace_6h/24h, mae_heuristik_24h
        (None solange die Shadow-Spalten leer sind).

    Mit `regime=...` (T-0349) werden die 6h-Metriken auf `regime_6h`
    und die 24h-Metriken auf `regime_24h` eingeschraenkt; `n` bleibt
    die Gesamtzahl, `n_regime_6h`/`n_regime_24h` zaehlen den Filter.
    Ohne Param bleiben die Bestandsfelder unveraendert (Regressionstest).

    Wichtig: Die Werte sind erst aussagekraeftig nach ~1-2 Wochen
    Sammelphase. Vorher zeigt die API auch `n` < 10 -> Frontend kann
    "noch zu wenig Daten" anzeigen.
    """
    if _speicher is None:
        return {"fehler": "Speicher nicht initialisiert"}
    assert _speicher._db is not None
    von = (datetime.now() - timedelta(days=tage)).isoformat()
    if regime is None:
        gate_6h = ""
        gate_24h = ""
    else:
        # Konditionale Aggregation: NULL-Werte fallen aus AVG heraus.
        gate_6h = "CASE WHEN regime_6h = :regime THEN 1 END * "
        gate_24h = "CASE WHEN regime_24h = :regime THEN 1 END * "
    sql = f"""SELECT zone_id,
                    COUNT(*) AS n,
                    AVG({gate_6h}ABS(abweichung_6h))         AS mae_ml_6h,
                    AVG({gate_6h}ABS(abweichung_physik_6h))  AS mae_physik_6h,
                    AVG({gate_24h}ABS(abweichung_24h))       AS mae_ml_24h,
                    AVG({gate_24h}ABS(abweichung_physik_24h)) AS mae_physik_24h,
                    AVG({gate_6h}(prognose_6h - prognose_physik_6h))
                                                    AS bias_pp_6h,
                    SUM(CASE WHEN prognose_physik_6h IS NOT NULL
                        THEN 1 ELSE 0 END)          AS n_mit_physik,
                    AVG({gate_6h}ABS(abweichung_statespace_6h))
                                                    AS mae_statespace_6h,
                    AVG({gate_24h}ABS(abweichung_statespace_24h))
                                                    AS mae_statespace_24h,
                    AVG({gate_24h}ABS(abweichung_heuristik_24h))
                                                    AS mae_heuristik_24h,
                    SUM(CASE WHEN regime_6h = :regime THEN 1 ELSE 0 END)
                                                    AS n_regime_6h,
                    SUM(CASE WHEN regime_24h = :regime THEN 1 ELSE 0 END)
                                                    AS n_regime_24h
             FROM empfehlungs_audit
             WHERE zeitstempel >= :von
               AND evaluiert_am IS NOT NULL
             GROUP BY zone_id
             ORDER BY zone_id"""
    async with _speicher._db.execute(
        sql, {"von": von, "regime": regime},
    ) as cursor:
        zeilen = await cursor.fetchall()

    def _r2(wert):
        return round(wert, 2) if wert is not None else None

    pro_zone = {}
    for z in zeilen:
        eintrag = {
            "n": int(z["n"]),
            "n_mit_physik": int(z["n_mit_physik"] or 0),
            "mae_ml_6h": _r2(z["mae_ml_6h"]),
            "mae_physik_6h": _r2(z["mae_physik_6h"]),
            "mae_ml_24h": _r2(z["mae_ml_24h"]),
            "mae_physik_24h": _r2(z["mae_physik_24h"]),
            "bias_pp_6h": _r2(z["bias_pp_6h"]),
            # T-0353/T-0351: additive Shadow-Metriken.
            "mae_statespace_6h": _r2(z["mae_statespace_6h"]),
            "mae_statespace_24h": _r2(z["mae_statespace_24h"]),
            "mae_heuristik_24h": _r2(z["mae_heuristik_24h"]),
        }
        if regime is not None:
            eintrag["n_regime_6h"] = int(z["n_regime_6h"] or 0)
            eintrag["n_regime_24h"] = int(z["n_regime_24h"] or 0)
        pro_zone[z["zone_id"]] = eintrag
    antwort = {"tage": tage, "pro_zone": pro_zone}
    if regime is not None:
        antwort["regime"] = regime
    return antwort


def _wirksame_fenster(zone) -> list[str]:
    """T-0576: die Uhrzeitfenster, die fuer die Zone TATSAECHLICH gelten.

    Im Aktivmodus ersetzt die Klammer `bevorzugte_zeiten` -- die alten
    Fenster stehen nur noch als Rueckfall in der Config. Sie hier weiter
    auszugeben hiesse, der Anzeige ein Abendfenster zu melden, das die Engine
    nicht mehr kennt.
    """
    gf = getattr(zone, "giessfenster_et0", None)
    fenster = (
        gf.klammer if gf is not None and gf.modus == GF_MODUS_AKTIV
        else zone.bevorzugte_zeiten
    )
    return [f"{z.von}-{z.bis}" for z in fenster]


def _giessfenster_info(zone, jetzt: datetime, vorhersage) -> dict | None:
    """T-0576: Giessfenster-Zustand fuer die Anzeige, nur im Aktivmodus.

    `naechster_start`: erster freier Start in den naechsten 24 h (15-min-Raster),
    None wenn keiner. `sperrgrund`: Klartext, warum es JETZT zu ist, None wenn
    offen. Beides aus derselben Urteilsfunktion wie die Engine.

    Ohne Vorhersage (Abfrage gescheitert) sagt die Urteilsfunktion "Klammer
    entscheidet" -- genau wie in der Engine (Entscheid 3). `daten_fehlen`
    macht das sichtbar, statt eine datengetriebene Freigabe vorzutaeuschen.
    """
    gf = getattr(zone, "giessfenster_et0", None)
    if gf is None or gf.modus != GF_MODUS_AKTIV:
        return None
    start, gesperrt = naechster_erlaubter_start(
        gf, ab=jetzt, bis=jetzt + timedelta(hours=24), wetter=vorhersage,
    )
    return {
        "modus": gf.modus,
        "klammer": [f"{z.von}-{z.bis}" for z in gf.klammer],
        "naechster_start": _iso(start) if start is not None else None,
        "sperrgrund": (
            beschreibe_sperre(gf, gesperrt) if gesperrt is not None else None
        ),
        "daten_fehlen": vorhersage is None or not getattr(vorhersage, "stunden", None),
    }


@app.get("/api/tagesplan")
async def tagesplan(
    tag: str = Query(
        default="heute",
        pattern="^(heute|morgen)$",
        description="'heute' (Default) oder 'morgen'.",
    ),
):
    """T-0227: Tagesplan-Vorschau ueber alle Zonen.

    Aggregiert pro Zone:
    - kausale Empfehlung (vorhersage_zone, T-0066) -- was wuerde das
      System bewaessern + warum
    - bevorzugte Zeitfenster aus der Konfig (`bevorzugte_zeiten`)
    - Hahn-Cluster-Hinweis fuer Konflikt-Warnung (Sprinkler exklusiv)
    - Regen-Forecast fuer den Tag (Standort-spezifisch)
    - Tages-Wetter (Max-Temp, Regen-Summe)

    Sortiert nach geplantem Start-Zeit (bevorzugt morgens). Zonen
    ohne Bewaesserungs-Bedarf landen am Ende mit Status 'kein_bedarf'.

    Verwendung im Frontend: kompakte Gantt-Liste "was passiert
    heute/morgen" oben auf der Uebersicht.
    """
    if _motor is None or _konfig is None:
        return {"fehler": "Motor nicht initialisiert"}

    jetzt = datetime.now()
    if tag == "morgen":
        ziel_datum = (jetzt + timedelta(days=1)).date()
    else:
        ziel_datum = jetzt.date()
    tag_start = datetime.combine(ziel_datum, datetime.min.time())
    tag_ende = tag_start + timedelta(days=1)

    # Wetter pro Standort fuer den Tag holen
    wetter_pro_standort: dict[str, dict] = {}
    # T-0576: die Vorhersage selbst, fuer das Giessfenster je Zone.
    vorhersage_pro_standort: dict[str, object] = {}
    if _wetter_manager is not None:
        for standort in (_konfig.standorte or []):
            sid = standort.wetter_standort or standort.standort_id
            if not sid or sid in wetter_pro_standort:
                continue
            try:
                vorhersage = await _wetter_manager.hole_vorhersage(sid)
            except Exception:
                continue
            vorhersage_pro_standort[sid] = vorhersage
            tag_stunden = [
                s for s in vorhersage.stunden
                if tag_start <= s.zeitstempel < tag_ende
            ]
            # T-0575: `tag_stunden` ist leer, wenn die Wetterabfrage
            # gescheitert ist -- `wetter.hole_vorhersage` gibt dann eine
            # LEERE Vorhersage zurueck (T-0563-Negativ-Gate), keinen Fehler.
            # `sum([])` ist 0, also stand hier "0,0 mm Regen" und war von
            # einer echten Trockenprognose nicht zu unterscheiden. Temperatur
            # hatte den Guard bereits, Regen und ET0 nicht -- zwei von vier
            # Aggregaten ueber derselben Liste. Genau die beiden
            # ungeschuetzten steuern die Bewaesserung.
            wetter_pro_standort[sid] = {
                "standort_id": sid,
                # None = keine Wetterdaten, nicht "kein Regen".
                "regen_summe_mm": (
                    round(sum(s.niederschlag_mm for s in tag_stunden), 1)
                    if tag_stunden else None
                ),
                # T-0575: wie viele Stunden die Aggregate tragen. Erlaubt der
                # Anzeige, "keine Daten" von "wenige Daten" zu unterscheiden.
                "stunden_im_tag": len(tag_stunden),
                "max_temp_c": (
                    round(max(s.temperatur for s in tag_stunden), 1)
                    if tag_stunden else None
                ),
                "min_temp_c": (
                    round(min(s.temperatur for s in tag_stunden), 1)
                    if tag_stunden else None
                ),
                "et0_summe_mm": (
                    round(sum(s.et0_mm for s in tag_stunden), 2)
                    if tag_stunden else None
                ),
            }

    def _zone_standort(zone_id: str) -> str:
        for s in (_konfig.standorte or []):
            if zone_id in s.zonen:
                return s.wetter_standort or s.standort_id
        return ""

    eintraege: list[dict] = []
    for zone in _konfig.zonen:
        try:
            empf = await _motor.vorhersage_zone(zone.zone_id)
        except Exception as exc:
            logger.exception(
                "tagesplan.vorhersage_fehler", zone_id=zone.zone_id,
            )
            eintraege.append({
                "zone_id": zone.zone_id,
                "zone_name": zone.name,
                "modus": zone.modus.value,
                "soll_bewaessern": False,
                "empfehlungs_typ": "fehler",
                "grund": f"Vorhersage fehlgeschlagen: {exc!s}"[:200],
                "geplante_zeit": None,
                "dauer_min": None,
                "bevorzugte_zeiten": _wirksame_fenster(zone),
                "giessfenster": None,
                "aktive_strategie": zone.bewaesserungs_strategie.value,
                "hahn_cluster": zone.hahn_cluster,
                "exklusiv": zone.exklusiv,
                "standort_id": _zone_standort(zone.zone_id),
            })
            continue

        fenster_info = _giessfenster_info(
            zone, jetzt, vorhersage_pro_standort.get(_zone_standort(zone.zone_id)),
        )

        # Geplanter Zeit-Slot: erstes bevorzugtes Zeitfenster nach jetzt,
        # sonst Mitte des Tages.
        geplante_zeit: datetime | None = None
        if empf.soll_bewaessern and fenster_info is not None:
            # T-0576 aktiv: die alten Uhrzeitfenster gelten nicht mehr, ihr
            # Beginn waere hier z.B. "18:00" -- ein Start, den es nicht gibt.
            # `soll_bewaessern` ist das Urteil fuer JETZT (inkl. Kritisch-
            # Bypass bei geschlossenem Fenster), die Engine startet im
            # naechsten Zyklus. `fenster_start` waere beim Bypass falsch.
            geplante_zeit = jetzt
        elif empf.soll_bewaessern:
            slots = []
            for zf in zone.bevorzugte_zeiten:
                try:
                    h_v, m_v = map(int, zf.von.split(":"))
                    slots.append(
                        tag_start.replace(hour=h_v, minute=m_v),
                    )
                except (ValueError, IndexError):
                    continue
            # Bei "heute" nur Slots in der Zukunft, bei "morgen" alle.
            if tag == "heute":
                slots = [s for s in slots if s >= jetzt]
            slots.sort()
            geplante_zeit = slots[0] if slots else (
                tag_start.replace(hour=6, minute=0)
            )

        # T-0535: laeuft ein Dosis-Test, ist SEINE Stufe die Dauer, die
        # tatsaechlich gefahren wird -- der Tagesplan darf nicht die
        # berechnete Dosis zeigen, waehrend das Ventil eine andere faehrt.
        # Gleiche Hierarchie wie `dauerHauptSekunden` im Frontend.
        dauer_quelle_s = empf.dauer_s_dosis_test or empf.dauer_s_empfehlung
        dauer_min: int | None = None
        if dauer_quelle_s:
            dauer_min = int(round(dauer_quelle_s / 60))

        eintraege.append({
            "zone_id": zone.zone_id,
            "zone_name": zone.name,
            "modus": zone.modus.value,
            "soll_bewaessern": bool(empf.soll_bewaessern),
            "empfehlungs_typ": empf.empfehlungs_typ,
            "grund": (empf.erklarung_kurz or empf.grund or "")[:200],
            "geplante_zeit": _iso(geplante_zeit) if geplante_zeit else None,
            "dauer_min": dauer_min,
            "bevorzugte_zeiten": _wirksame_fenster(zone),
            "giessfenster": fenster_info,
            "aktive_strategie": zone.bewaesserungs_strategie.value,
            "hahn_cluster": zone.hahn_cluster,
            "exklusiv": zone.exklusiv,
            "standort_id": _zone_standort(zone.zone_id),
            "tage_bis_welkepunkt": empf.tage_bis_welkepunkt,
            # T-0295: Plateau-Transparenz (T-0291) durchreichen, damit der
            # Tagesplan "48 min erreicht ~X %, Ziel braucht ~N Dosen" zeigen
            # kann statt nur "48 min fuer N Tage Reserve" (irrefuehrend).
            "erwarteter_endwert_pp": empf.erwarteter_endwert_pp,
            "einzeldosis_max_pp": empf.einzeldosis_max_pp,
            "dosen_bis_ziel": empf.dosen_bis_ziel,
        })

    # Sortierung: zuerst die mit geplanter Zeit (chronologisch), danach
    # akut/praeventiv ohne Zeit, am Ende kein_bedarf.
    def _sort_key(e: dict) -> tuple:
        prio = (
            0 if e.get("geplante_zeit")
            else 1 if e.get("empfehlungs_typ") in ("akut", "praeventiv")
            else 2
        )
        return (prio, e.get("geplante_zeit") or "")
    eintraege.sort(key=_sort_key)

    return {
        "tag": tag,
        "datum": ziel_datum.isoformat(),
        "wetter_pro_standort": list(wetter_pro_standort.values()),
        "eintraege": eintraege,
    }


# T-0200: Dashboard-Snapshot — alle Daten pro Zone in einem Call.
# Erlaubte Sensor-Fenster fuer den `fenster`-Query-Param.
_SNAPSHOT_FENSTER_STUNDEN: dict[str, int] = {
    "24h": 24,
    "48h": 48,
    "7d": 24 * 7,
    "30d": 24 * 30,
}


@app.get("/api/dashboard-snapshot")
async def dashboard_snapshot(
    zone_id: str | None = Query(
        default=None,
        description=(
            "Optional: Nur eine bestimmte Zone. Default = alle konfigurierten "
            "Zonen."
        ),
    ),
    fenster: str = Query(
        default="24h,48h",
        description=(
            "Komma-getrennte Liste der Messwerte-Fenster. Erlaubt: 24h, 48h, "
            "7d, 30d. Default 24h,48h (deckt Karten-Chart 48h + Trend 24h ab)."
        ),
    ),
    ml_details: bool = Query(
        default=False,
        description=(
            "T-0040: `true` ergaenzt `top_features` (SHAP-Beitraege) je "
            "ML-Vorhersage. Default false, weil SHAP 10-30 ms pro Horizont "
            "kostet und das Dashboard ihn nur on-demand braucht."
        ),
    ),
):
    """T-0200: Aggregierter Snapshot fuer die Zonen-Karte v2.

    Ersetzt drei Per-Karte-Polls (`/messwerte`, `/empfehlung-jetzt`,
    `/ml/vorhersage/{id}`) pro Zone durch genau einen HTTP-Call mit
    bulk SQL je Daten-Typ.

    Response-Vertrag:
    - `zonen[*].zone` ist byte-identisch zu einem Element aus `/api/zonen`.
    - `zonen[*].messwerte[fenster]` ist byte-identisch zur Liste aus
      `/api/zonen/{id}/messwerte?stunden=<fenster>`.
    - `zonen[*].empfehlung` ist das `GiessEmpfehlung`-Modell aus
      `/api/zonen/{id}/empfehlung-jetzt`.
    - `zonen[*].ml_vorhersage` spiegelt `/api/ml/vorhersage/{id}` —
      Dict mit Horizont-Keys oder leeres Dict bei Fehler.
    - `zonen[*].ml_vorhersage_fehler` ist der Fehler-String aus der
      ML-API (`Keine aktuellen Sensordaten`, `ML-Modell nicht verfuegbar`,
      `Feature-Extraktion fehlgeschlagen`) oder None bei Erfolg.

    Performance: Sensor- und Mess-Daten kommen aus 3+N Bulk-SQLs
    (`letzte_messung_aggregiert_bulk`, `letzte_messungen_pro_geraet_bulk`,
    `offene_sensor_warnungen`, `hole_plant_optima_achsen`, je 1×
    `hole_messungen_bulk` pro angefragtem Fenster). Empfehlung +
    ML-Vorhersage sind pro Zone unabhaengige Berechnungen — sie laufen
    sequenziell, damit der ML-Live-Cache (45 s TTL) bei der ersten Zone
    den 48h-Feature-DF baut und alle weiteren Zonen daraus bedient
    werden. `asyncio.gather` waere hier eine Falle, weil 11 parallele
    Feature-Extraktionen aiosqlite-Last und Event-Loop-Blockierung
    erzeugen wuerden (siehe `live_vorhersage`-Docstring).

    Cache-Header: 30 s — kuerzer als bei Per-Zone-Endpoints, weil dieser
    Endpoint vom V2-Loader alle 60 s gepollt wird und der Browser nicht
    laenger einen veralteten Snapshot zeigen soll als das Refresh-
    Intervall.
    """
    assert _konfig and _speicher and _verarbeiter and _motor

    # Fenster-Liste parsen + validieren. Leere/unbekannte Werte werden
    # ignoriert, damit das Frontend defensiv mit fenster="" arbeiten kann.
    # T-0495: Das steht bewusst VOR der Cache-Abfrage. Vorher ging der ROHE
    # Query-String in den Schluessel -- "7d,30d", "30d,7d" und "7d,30d,quatsch"
    # meinen dasselbe, erzeugten aber drei Eintraege mit je einer vollen
    # Antwort. Normalisiert ist der Schluesselraum durch die erlaubten
    # Fenster begrenzt.
    fenster_keys: list[str] = []
    for f in (fenster or "").split(","):
        f = f.strip()
        if f and f in _SNAPSHOT_FENSTER_STUNDEN and f not in fenster_keys:
            fenster_keys.append(f)
    if not fenster_keys:
        fenster_keys = ["24h"]

    # T-0293: TTL-Cache. Bei frischem Treffer ohne ML-Recompute zurueck
    # (Frontend pollt 30 s, Recompute nur ~alle _SNAPSHOT_CACHE_TTL_S).
    # Bewusst NICHT sortiert: `fenster_keys` geht in derselben Reihenfolge in
    # die Antwort ("fenster": [...]). Ein sortierter Schluessel wuerde zwei
    # Reihenfolgen zusammenlegen und der zweite Aufrufer bekaeme die
    # Reihenfolge des ersten zurueck. Der Schluesselraum ist auch so
    # begrenzt -- nur erlaubte Fenster kommen durch.
    _cache_key = (zone_id, tuple(fenster_keys), ml_details)
    _cached = _SNAPSHOT_CACHE.get(_cache_key)
    if _cached is not None and (time.monotonic() - _cached[0]) < _SNAPSHOT_CACHE_TTL_S:
        return JSONResponse(
            content=_cached[1], headers={"Cache-Control": "max-age=30"},
        )

    # Zonen-Liste auswaehlen (single oder all).
    if zone_id is not None:
        gewaehlte_zonen = [z for z in _konfig.zonen if z.zone_id == zone_id]
        if not gewaehlte_zonen:
            raise HTTPException(
                status_code=404,
                detail=f"Zone {zone_id} nicht gefunden",
            )
    else:
        gewaehlte_zonen = list(_konfig.zonen)

    zone_ids = [z.zone_id for z in gewaehlte_zonen]
    api_jetzt = datetime.now()

    # Bulk-Pulls: jeder Datentyp einmal pro Request.
    warnungen_pro_zone = _baue_warnungen_pro_zone(
        await _speicher.offene_sensor_warnungen(),
    )
    optima_pro_zone = await _speicher.hole_plant_optima_achsen()
    # T-0476: gleiches Abruf-Fenster wie /api/zonen (Isomorphie -- beide
    # Endpoints bauen ueber `_baue_zone_dict` und duerfen nicht driften).
    letzte_pro_zone = await _speicher.letzte_messung_aggregiert_bulk(
        zone_ids,
        fenster_minuten=AGGREGAT_FALLBACK_FENSTER_MIN,
        jetzt=api_jetzt,
    )
    sensoren_pro_zone = await _speicher.letzte_messungen_pro_geraet_bulk(zone_ids)
    # T-0573 AK6: reiner Anzeige-Rueckfall, siehe `_letzter_bekannter_bulk`.
    letzter_bekannter_pro_zone = await _letzter_bekannter_bulk(
        gewaehlte_zonen, api_jetzt,
    )

    # Messwerte je Fenster (eine Query je Fenster, alle Zonen drin).
    messwerte_pro_fenster: dict[str, dict[str, list]] = {}
    for f in fenster_keys:
        stunden = _SNAPSHOT_FENSTER_STUNDEN[f]
        von_dt = api_jetzt - timedelta(hours=stunden)
        messwerte_pro_fenster[f] = await _speicher.hole_messungen_bulk(
            zone_ids, von=von_dt,
        )

    # ML-Verfuegbarkeit einmal pruefen — spart pro Zone den Check.
    ml_verfuegbar = (
        _ml_service is not None and _ml_service.ist_verfuegbar
    )

    # T-0200 Perf-Fix: ML in einem Bulk-Call, der den 48h-Feature-DF
    # genau EINMAL baut (statt pro Zone). Anderenfalls kostet der
    # Snapshot 14x DF-Build ~ 60 s — der TTL-Cache hilft nur bei
    # parallelen Calls auf dieselbe Zone, nicht beim sequenziellen
    # Snapshot. Nur Zonen mit aktueller Feuchte fragen wir an —
    # Zonen ohne Sensordaten liefert die Per-Zone-API mit "Keine
    # aktuellen Sensordaten" zurueck, der Bulk-Pfad spiegelt das.
    def _letzter_wert_fuer(zid: str):
        w = letzte_pro_zone.get(zid)
        if w:
            return w
        # T-0476: Cache-Fallback auf 48 h gekappt (wie /api/zonen).
        return _cache_wert_wenn_frisch(
            _verarbeiter.hole_letzten_wert(zid), api_jetzt,
        )

    ml_bulk: dict[str, dict[str, "object"]] = {}
    if ml_verfuegbar:
        ml_kandidaten = [
            zone.zone_id for zone in gewaehlte_zonen
            if (_letzter_wert_fuer(zone.zone_id) is not None
                and _letzter_wert_fuer(zone.zone_id).boden_feuchte is not None)
        ]
        if ml_kandidaten:
            try:
                ml_bulk = await _ml_service.live_vorhersage_bulk(
                    ml_kandidaten, _speicher, _konfig, details=ml_details,
                )
            except Exception:  # noqa: BLE001
                # T-0393: vorher still -> JEDER ML-Service-Crash erschien im
                # Frontend als harmloses "Feature-Extraktion fehlgeschlagen --
                # zu wenig Daten?". Der Snapshot bleibt bewusst tolerant
                # (leeres ml_bulk), aber der Fehler muss sichtbar sein.
                logger.exception(
                    "snapshot.ml_bulk_fehlgeschlagen",
                    zonen=len(ml_kandidaten),
                )
                ml_bulk = {}

    zonen_response: list[dict] = []
    for zone in gewaehlte_zonen:
        zid = zone.zone_id
        letzter_wert = _letzter_wert_fuer(zid)
        sensoren_pro_geraet = sensoren_pro_zone.get(zid, [])

        zone_dict = _baue_zone_dict(
            zone,
            letzter_wert,
            sensoren_pro_geraet,
            warnungen_pro_zone.get(zid, []),
            optima_pro_zone.get(zid, {}),
            api_jetzt,
            # T-0502: isomorph zu /api/zonen -- beide Endpoints muessen
            # dasselbe Urteil liefern (T-0200-Vertrag).
            null_ist_defekt=await _null_ist_defekt_fuer(zone, letzter_wert),
            letzter_bekannter=letzter_bekannter_pro_zone.get(zid),
        )

        # Empfehlung (Dry-Run, keine DB-Writes — siehe `vorhersage_zone`).
        empfehlung = await _motor.vorhersage_zone(zid)
        empfehlung_dict = empfehlung.model_dump(mode="json")

        # Messwerte je Fenster pro Zone (chronologisch wie /messwerte).
        messwerte_zone = {
            f: [_messung_zu_dict(m) for m in reversed(messwerte_pro_fenster[f].get(zid, []))]
            for f in fenster_keys
        }

        # ML-Vorhersage spiegelt `/api/ml/vorhersage/{id}`-Antwort.
        ml_dict: dict[str, dict] = {}
        ml_fehler: str | None = None
        if not ml_verfuegbar:
            ml_fehler = "ML-Modell nicht verfuegbar"
        elif not letzter_wert or letzter_wert.boden_feuchte is None:
            ml_fehler = "Keine aktuellen Sensordaten"
        else:
            ergebnisse_roh = ml_bulk.get(zid, {})
            if not ergebnisse_roh:
                ml_fehler = (
                    "Feature-Extraktion fehlgeschlagen — zu wenig Daten?"
                )
            for key, v in ergebnisse_roh.items():
                # T-0573: identischer Dict-Bauer wie /api/ml/vorhersage --
                # der T-0200-Vertrag verlangt dieselbe Antwort.
                ml_dict[key] = _ml_eintrag_dict(v)

        zonen_response.append({
            "zone": zone_dict,
            "empfehlung": empfehlung_dict,
            "messwerte": messwerte_zone,
            "ml_vorhersage": ml_dict,
            "ml_vorhersage_fehler": ml_fehler,
        })

    _content = {
        "zeitstempel": _iso(api_jetzt),
        "fenster": fenster_keys,
        "ml_verfuegbar": ml_verfuegbar,
        "zonen": zonen_response,
    }
    # T-0293: Ergebnis cachen, damit Folge-Polls ohne ML-Recompute bedient
    # werden (zone_id=None-Voll-Snapshot ist der teure Default-Poll).
    _snapshot_cache_setze(_cache_key, _content)
    return JSONResponse(
        content=_content,
        headers={"Cache-Control": "max-age=30"},
    )


@app.get("/api/entscheidungen")
async def letzte_entscheidungen(
    zone_id: str | None = Query(default=None, description="Nur Entscheidungen dieser Zone"),
    limit: int = Query(default=50, ge=1, le=5000),
    von: str | None = Query(default=None, description="ISO-Zeitstempel, inkl."),
    bis: str | None = Query(default=None, description="ISO-Zeitstempel, inkl."),
    blocker_typ: str | None = Query(default=None, description="BlockerTyp-Enum-Wert"),
    format: str = Query(default="json", pattern="^(json|csv)$"),
):
    """Letzte Entscheidungen aus dem Log (T-0037).

    Neue Filter `von`/`bis`/`blocker_typ` fuer Zeitraum- und Ursachen-
    Auswertung. `format=csv` liefert statt JSON ein CSV, damit Analysen
    in Excel/Pandas moeglich sind ohne UI-Scraping. `limit` bis 5000,
    damit 30-Tage-Export mit ~11 Zonen × 288 Entscheidungen/Tag moeglich.
    """
    assert _speicher
    von_dt = _parse_zeitgrenze(von)   # T-0565
    bis_dt = _parse_zeitgrenze(bis)
    entscheidungen = await _speicher.hole_entscheidungen(
        zone_id=zone_id,
        limit=limit,
        von=von_dt,
        bis=bis_dt,
        blocker_typ=blocker_typ,
    )
    if format == "csv":
        zeilen = ["zeitstempel,zone_id,scope,scope_ref,soll_bewaessern,dauer_sekunden,blocker_typ,begruendung"]
        for e in entscheidungen:
            # Semikolons in begruendung maskieren CSV-Semantik nicht, da wir
            # Komma-Trenner nutzen. Komma in begruendung via quoting absichern.
            begruendung_csv = (e.begruendung or "").replace('"', '""')
            zeilen.append(
                f"{_iso(e.zeitstempel)},{e.zone_id},{e.scope.value},"
                f"{e.scope_ref},{int(e.soll_bewaessern)},{e.dauer_sekunden},"
                f"{e.blocker_typ.value if e.blocker_typ else ''},"
                f"\"{begruendung_csv}\""
            )
        return PlainTextResponse(
            "\n".join(zeilen) + "\n",
            media_type="text/csv",
            headers={
                "Content-Disposition": (
                    "attachment; filename=entscheidungen_"
                    f"{datetime.now().strftime('%Y-%m-%d')}.csv"
                ),
            },
        )
    return [
        {
            "zeitstempel": _iso(e.zeitstempel),
            "zone_id": e.zone_id,
            "soll_bewaessern": e.soll_bewaessern,
            "dauer_sekunden": e.dauer_sekunden,
            "begruendung": e.begruendung,
            "blocker_typ": e.blocker_typ.value if e.blocker_typ else None,
            "scope": e.scope.value,
            "scope_ref": e.scope_ref,
        }
        for e in entscheidungen
    ]


@app.get("/api/kalibrierung/{zone_id}")
async def kalibrierung_zone(zone_id: str, limit: int = Query(default=50, ge=1, le=500)):
    """T-0063: Kalibrier-Kandidaten (Feldkapazitaet + Welkepunkt-Proxy)
    fuer eine Zone, neueste zuerst. Quelle: automatischer
    `KalibrationsJob`, der alle 6 h scannt.

    Antwort gibt zusaetzlich einen vorlaeufigen Median-Wert pro Typ.
    Bei < 3 Kandidaten pro Typ wird der Median als `null` geliefert
    (zu wenig Datenbasis).
    """
    assert _speicher
    kandidaten = await _speicher.hole_kalibrierungen(zone_id=zone_id, limit=limit)
    feldkap = [k["wert"] for k in kandidaten if k["typ"] == "feldkapazitaet"]
    welke = [k["wert"] for k in kandidaten if k["typ"] == "welkepunkt_proxy"]

    def _median_oder_none(werte: list) -> float | None:
        if len(werte) < 3:
            return None
        import statistics
        return round(statistics.median(werte), 1)

    return {
        "zone_id": zone_id,
        "kandidaten": kandidaten,
        "feldkapazitaet_median": _median_oder_none(feldkap),
        "welkepunkt_median": _median_oder_none(welke),
        "n_feldkapazitaet": len(feldkap),
        "n_welkepunkt": len(welke),
    }


@app.get("/api/schwellen-vorschlag")
async def schwellen_vorschlag(fenster_tage: int = Query(default=30, ge=7, le=90)):
    """T-0049: Datengetriebene Schwellen-Vorschlaege aus den letzten
    `fenster_tage` Tagen Sensor-Historie. Rein informativ — der User
    trifft die Entscheidung, nichts wird automatisch angewandt.
    """
    from bewaesserung.schwellen_vorschlag import berechne_vorschlaege_fuer_alle
    assert _speicher and _konfig
    vorschlaege = await berechne_vorschlaege_fuer_alle(
        _speicher, _konfig.zonen, fenster_tage=fenster_tage,
        ml_ausschluss_fenster=_konfig.ml_ausschluss_fenster,
    )
    return [v.model_dump() for v in vorschlaege]


FENSTER_STUNDEN = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}


def _zone_standort_id(zone_id: str) -> str:
    """Gibt den Wetter-Standort einer Zone zurueck (Fallback: Standard)."""
    assert _konfig
    for s in _konfig.standorte:
        if zone_id in s.zonen:
            return s.wetter_standort or s.standort_id
    return _konfig.standorte[0].wetter_standort if _konfig.standorte else "standard"


@app.get("/api/zonen/{zone_id}/bilanz")
async def zone_bilanz(zone_id: str, fenster: str = "24h"):
    """Wasser-Bilanz einer Zone fuer ein Fenster (24h, 7d, 30d)."""
    from bewaesserung.bilanz import berechne_bilanz
    assert _speicher and _konfig

    zone = next((z for z in _konfig.zonen if z.zone_id == zone_id), None)
    if zone is None:
        return {"fehler": f"Unbekannte Zone: {zone_id}"}
    if fenster not in FENSTER_STUNDEN:
        return {"fehler": f"Fenster '{fenster}' unbekannt; erlaubt: {list(FENSTER_STUNDEN)}"}
    if zone.flaeche_m2 is None:
        return {
            "fehler": "Zone hat keine flaeche_m2 konfiguriert — Bilanz nicht moeglich",
            "zone_id": zone_id,
        }

    bis = datetime.now()
    von = bis - timedelta(hours=FENSTER_STUNDEN[fenster])
    standort_id = _zone_standort_id(zone_id)

    bilanz = await berechne_bilanz(
        zone, von, bis, _speicher, _konfig.bilanz, standort_id,
    )
    assert bilanz is not None  # flaeche_m2 war oben gecheckt

    return baue_bilanz_dict(bilanz, fenster)


@app.get("/api/prognose")
async def prognose():
    """Predictive Watering Prognosen fuer alle Zonen."""
    assert _motor and _konfig
    ergebnis = []
    for zone in _konfig.zonen:
        zeitpunkt, grund = await _motor.prognostiziere_bewaesserung(zone.zone_id)
        ergebnis.append({
            "zone_id": zone.zone_id,
            "name": zone.name,
            "bewaesserung_erwartet": _iso(zeitpunkt) if zeitpunkt else None,
            "begruendung": grund,
        })
    return ergebnis


async def _wetter_antwort(standort_id: str | None = None) -> dict:
    """Baut die Wetter-Antwort fuer einen Standort (oder Standard-Standort)."""
    assert _wetter_manager
    vorhersage = await _wetter_manager.hole_vorhersage(standort_id)

    # Nur zukuenftige Stunden (ab aktueller Stunde)
    jetzt = datetime.now()
    zukuenftige = [s for s in vorhersage.stunden if s.zeitstempel >= jetzt.replace(minute=0, second=0, microsecond=0)]
    naechste_24h = zukuenftige[:24]

    # T-0241: aktive Wetter-Ereignisse (Frost/Hitze/Starkregen). Backend
    # erkannt + persistiert sie schon (wetter_ereignisse.py), aber sie
    # waren bisher nur via /api/ops/timeline sichtbar -- nicht im Default-
    # Tab "Uebersicht". Filter: persistiert seit letzten 24h UND
    # `ende >= jetzt` (oder kein Ende). Mehrere Frost/Hitze-Ereignisse
    # pro Tag werden deduplizert (per Typ den juengsten Eintrag nehmen),
    # damit die WetterKarte kein Banner-Stack zeigt.
    aktive_ereignisse: list[dict] = []
    if _speicher is not None:
        seit = jetzt - timedelta(hours=24)
        # Standort-Filter: bei None ALLE Standorte; bei gesetztem Wert
        # nur die fuer diesen Standort.
        rohe = await _speicher.hole_wetter_ereignisse(
            von=seit, standort_id=standort_id,
        )
        seen_typen: set[str] = set()
        for e in rohe:
            if e.ende is not None and e.ende < jetzt:
                continue  # schon vorbei
            schluessel = f"{e.typ.value}:{e.standort_id}"
            if schluessel in seen_typen:
                continue  # nur juengsten pro Typ+Standort
            seen_typen.add(schluessel)
            aktive_ereignisse.append({
                "typ": e.typ.value,
                "standort_id": e.standort_id,
                "details": e.details,
                "beginn": _iso(e.beginn) if e.beginn else None,
                "ende": _iso(e.ende) if e.ende else None,
                "zeitstempel": _iso(e.zeitstempel),
            })

    return {
        "abfrage_zeitstempel": _iso(vorhersage.abfrage_zeitstempel),
        "niederschlag_6h_mm": sum(s.niederschlag_mm for s in naechste_24h[:6]),
        "et0_6h_mm": sum(s.et0_mm for s in naechste_24h[:6]),
        "wind_6h_kmh": round(sum(s.wind_kmh for s in naechste_24h[:6]) / max(len(naechste_24h[:6]), 1), 1),
        # T-0241: aktive Wetter-Ereignisse (Frost/Hitze/Starkregen) fuer
        # WetterKarte-Banner. Leeres Array wenn nichts akut.
        "aktive_ereignisse": aktive_ereignisse,
        "stunden": [
            {
                "zeitstempel": _iso(s.zeitstempel),
                "temperatur": s.temperatur,
                "niederschlag_mm": s.niederschlag_mm,
                "niederschlag_wahrscheinlichkeit": s.niederschlag_wahrscheinlichkeit,
                "wind_kmh": s.wind_kmh,
                "wind_richtung_grad": s.wind_richtung_grad,
                "et0_mm": s.et0_mm,
            }
            for s in naechste_24h
        ],
    }


@app.get("/api/wetter")
async def wetter():
    """Aktuelle Wettervorhersage (Standard-Standort, ab jetzt, naechste 24h)."""
    return await _wetter_antwort()


@app.get("/api/wetter/{standort_id}")
async def wetter_standort(standort_id: str):
    """Wettervorhersage fuer einen bestimmten Standort."""
    assert _wetter_manager
    client = _wetter_manager.hole_client(standort_id)
    if client is None:
        return {"fehler": f"Standort '{standort_id}' nicht gefunden"}
    return await _wetter_antwort(standort_id)


@app.get("/api/standorte")
async def standorte():
    """Alle konfigurierten Standorte mit zugeordneten Zonen."""
    assert _konfig
    ergebnis = []
    for s in (_konfig.standorte or []):
        ergebnis.append({
            "standort_id": s.standort_id,
            "name": s.name,
            "zonen": s.zonen,
            "wetter_standort": s.wetter_standort or s.standort_id,
        })
    return ergebnis


# --- Ops-Tab ---

@app.get("/api/ops/summary")
async def ops_summary():
    """Aggregierte Kennzahlen fuer den Ops-Tab."""
    assert _speicher
    return await _speicher.hole_ops_summary()


@app.get("/api/ops/betriebsstatus")
async def ops_betriebsstatus():
    """T-0238: Betriebsstatus-Zentrale.

    Aggregiert die System-Health-Daten an einer Stelle, damit der User
    nicht zwischen SQLite-CLI, Logs und mehreren Tabs springen muss.
    Schliesst auch den T-0246-Scope ab (Version + Konfig-Status im UI).

    Felder:
    - `system`: version, zonen_anzahl, konfiguriert (T-0246-Scope).
    - `endpoints`: pro Eintrag in `endpoint_health` (DHS + FYTA) der
      letzte Status, die letzte Pruefung + der letzte Erfolg + Details.
    - `husqvarna`: letzter Sensor-Beat (Gardena-Pipeline) +
      Beat-Count letzte 24h. Stale wenn > 1 h ohne Beat.
    - `ml`: letzter Retrain-Stand aus dem MLVorhersageService
      (`trainiert_am`, ist_geladen). Heisst nicht: "alles ok", aber
      "Modell wurde wann zuletzt geladen".
    - `backup`: filesystem-Read auf
      `backend/daten/backup/taeglich/` -- juengstes Snapshot + Anzahl.
    - `watchdog_letzter_push`: letztes Watchdog-Event (`zuletzt_gesendet`-
      Zeitstempel + Trigger-Typ + Zone). Indikator "ist der Push-Kanal
      lebendig".
    - `offene_sensor_warnungen`: Count (analog OpsSummary).
    """
    if _speicher is None:
        return {"fehler": "Speicher nicht initialisiert"}
    jetzt = datetime.now()
    aggregate = await _speicher.hole_betriebsstatus_aggregate(jetzt)
    endpoints = await _speicher.hole_endpoint_health()
    ml_block: dict = {"ist_geladen": False, "trainiert_am": None}
    if _ml_service is not None and _ml_service.ist_verfuegbar:
        status = _ml_service.status()
        # `ModellStatus` hat kein dediziertes `modell_version`-Feld --
        # der Basename des modell_pfads ist der naechstbeste Proxy
        # (z. B. "modell_6h_q50_2026-05-24.lgbm"). Bei Pro-Zone-
        # Modellen ist der Pfad einer der geladenen, nicht der
        # einzelne -- ergaenzend Anzahl Cluster + Anzahl Horizonte.
        from pathlib import Path as _Path
        ml_block = {
            "ist_geladen": True,
            "trainiert_am": _iso(status.trainiert_am)
            if status.trainiert_am else None,
            "modell_version": (
                _Path(status.modell_pfad).name if status.modell_pfad else None
            ),
            "cluster_count": len(status.cluster_horizonte),
            "horizonte": status.horizonte,
        }

    # Backup-Filesystem-Read. Pfad aus Konfig, mit Tilde-Expansion.
    backup_block: dict = {
        "verzeichnis": None,
        "letzter_snapshot": None,
        "letzter_snapshot_zeit": None,
        "snapshot_count": 0,
        "spiegel_aktiv": False,
    }
    if _konfig is not None:
        from pathlib import Path
        verz_raw = _konfig.backup.verzeichnis
        verz = Path(verz_raw).expanduser()
        if not verz.is_absolute():
            # Relativ zum Projekt-Root (vom Working-Dir aus auflösen).
            verz = (Path.cwd() / verz).resolve()
        taeglich = verz / "taeglich"
        backup_block["verzeichnis"] = str(verz)
        backup_block["spiegel_aktiv"] = bool(_konfig.backup.spiegel_verzeichnis)
        if taeglich.is_dir():
            # T-0475: Snapshots liegen gzip-komprimiert (`.db.gz`); der
            # Alt-Bestand ist noch unkomprimiert (`.db`). Ein Glob auf
            # `*.db` allein wuerde nach der Umstellung 0 Snapshots
            # melden und der Status-Block behauptete "kein Backup".
            snapshots = sorted(
                (
                    p for p in taeglich.iterdir()
                    if p.is_file() and (
                        p.name.endswith(".db") or p.name.endswith(".db.gz")
                    )
                ),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            backup_block["snapshot_count"] = len(snapshots)
            if snapshots:
                juengster = snapshots[0]
                backup_block["letzter_snapshot"] = juengster.name
                backup_block["letzter_snapshot_zeit"] = _iso(
                    datetime.fromtimestamp(juengster.stat().st_mtime)
                )

    husqvarna_letzter = aggregate.get("husqvarna_letzter_beat")
    husqvarna_stale = True
    if husqvarna_letzter:
        try:
            letzter = datetime.fromisoformat(husqvarna_letzter)
            husqvarna_stale = (jetzt - letzter) > timedelta(hours=1)
        except ValueError:
            pass

    return {
        "zeitstempel": _iso(jetzt),
        "system": {
            "version": app.version,
            "konfiguriert": _konfig is not None,
            "zonen_anzahl": len(_konfig.zonen) if _konfig else None,
        },
        "endpoints": endpoints,
        "husqvarna": {
            "letzter_beat": husqvarna_letzter,
            "beats_24h": aggregate.get("husqvarna_beats_24h", 0),
            "stale": husqvarna_stale,
        },
        "ml": ml_block,
        "backup": backup_block,
        "watchdog_letzter_push": {
            "zeit": aggregate.get("letztes_watchdog_event_zeit"),
            "trigger": aggregate.get("letztes_watchdog_event_trigger"),
            "zone": aggregate.get("letztes_watchdog_event_zone"),
        },
        "offene_sensor_warnungen": aggregate.get(
            "offene_sensor_warnungen", 0,
        ),
    }


@app.get("/api/wartungs-fenster")
async def wartungs_fenster_liste(
    nur_offen: bool = Query(True),
    zone_id: str | None = Query(None),
):
    """T-0228 Stufe 2: Liste der Wartungs-Fenster pro Zone.

    UI-Toggle pro Karte: GET zeigt aktuell offenes Fenster (Badge
    "Wartung aktiv"), POST oeffnet eins, POST /{id}/beenden schliesst
    es. Aktive Fenster pausieren die Heuristik (sensor_backfill).
    Weitere Konsumenten (Leck-Detektor, ML-Training, Schwellen-
    Vorschlag) folgen in Stufe 2c.
    """
    if _speicher is None:
        return {"fehler": "Speicher nicht initialisiert"}
    eintraege = await _speicher.hole_wartungs_fenster(
        nur_offen=nur_offen, zone_id=zone_id,
    )
    return {"eintraege": eintraege}


@app.post("/api/wartungs-fenster")
async def wartungs_fenster_starten(daten: dict):
    """T-0228 Stufe 2: oeffnet ein Wartungs-Fenster fuer eine Zone.
    Body: {zone_id, grund?}."""
    if _speicher is None:
        return {"fehler": "Speicher nicht initialisiert"}
    zone_id = (daten.get("zone_id") or "").strip()
    if not zone_id:
        return {"ok": False, "fehler": "zone_id ist Pflicht"}
    # T-0418: `grund` ist Pflicht. Ein leeres Begruendungsfeld war die
    # Signatur des versehentlich angelegten Fensters (hecke id=2: sechs
    # Sekunden nach dem Schliessen von id=1 geoeffnet, ohne Grund, dann vier
    # Wochen offen). Ein Fenster setzt Sicherheitsfunktionen einer Zone aus --
    # das darf man nicht aus Versehen tun koennen.
    grund = (daten.get("grund") or "").strip()
    if len(grund) < 10:
        return {
            "ok": False,
            "fehler": (
                "grund ist Pflicht (mind. 10 Zeichen). Ein Wartungs-Fenster "
                "setzt Leck-Detektor, Backfill und Fit-Jobs fuer diese Zone "
                "aus -- bitte notieren, warum und wie lange."
            ),
        }
    fenster_id = await _speicher.starte_wartungs_fenster(
        zone_id=zone_id, grund=grund,
    )
    logger.info(
        "wartungs_fenster.gestartet",
        zone_id=zone_id, fenster_id=fenster_id, grund=grund[:120],
    )
    return {"ok": True, "id": fenster_id}


@app.post("/api/wartungs-fenster/{fenster_id}/beenden")
async def wartungs_fenster_beenden(fenster_id: int):
    """T-0228 Stufe 2: schliesst ein Wartungs-Fenster (`bis_am = jetzt`)."""
    if _speicher is None:
        return {"fehler": "Speicher nicht initialisiert"}
    geaendert = await _speicher.beende_wartungs_fenster(fenster_id)
    return {"ok": True, "geaendert": geaendert}


@app.get("/api/pflege-erinnerungen")
async def pflege_erinnerungen_liste(
    nur_offen: bool = Query(True),
    anstehend_tage: int | None = Query(
        None, ge=0, le=365,
        description="Cap auf faellig_am <= jetzt + N Tage. None = alle offenen.",
    ),
    zone_id: str | None = Query(None),
):
    """T-0228 Stufe 1: Liste der Pflege-Erinnerungen.

    Default: alle offenen, kein Datums-Cap. Fuer das Uebersicht-Widget
    typischerweise `?anstehend_tage=3` (3 Tage Vorlauf wie im Plan).
    """
    if _speicher is None:
        return {"fehler": "Speicher nicht initialisiert"}
    eintraege = await _speicher.hole_pflege_erinnerungen(
        nur_offen=nur_offen,
        anstehend_tage=anstehend_tage,
        zone_id=zone_id,
    )
    return {"eintraege": eintraege}


@app.post("/api/pflege-erinnerungen")
async def pflege_erinnerung_anlegen(daten: dict):
    """T-0228 Stufe 1: neue Pflege-Erinnerung anlegen.

    Body: {typ, faellig_am (ISO), beschreibung?, zone_id?,
    intervall_tage?, quelle?}
    """
    if _speicher is None:
        return {"fehler": "Speicher nicht initialisiert"}
    typ = (daten.get("typ") or "").strip()
    faellig_raw = (daten.get("faellig_am") or "").strip()
    if not typ or not faellig_raw:
        return {"ok": False, "fehler": "typ und faellig_am sind Pflichtfelder"}
    try:
        faellig = datetime.fromisoformat(faellig_raw)
    except ValueError:
        return {"ok": False, "fehler": f"faellig_am '{faellig_raw}' nicht ISO"}

    intervall = daten.get("intervall_tage")
    if intervall is not None:
        try:
            intervall = int(intervall)
            if intervall <= 0:
                return {"ok": False, "fehler": "intervall_tage muss > 0 sein"}
        except (TypeError, ValueError):
            return {"ok": False, "fehler": "intervall_tage muss Integer sein"}

    eintrag_id = await _speicher.speichere_pflege_erinnerung(
        typ=typ,
        faellig_am=faellig,
        beschreibung=(daten.get("beschreibung") or "").strip(),
        zone_id=(daten.get("zone_id") or None) or None,
        intervall_tage=intervall,
        quelle=(daten.get("quelle") or "manuell"),
    )
    return {"ok": True, "id": eintrag_id}


@app.post("/api/pflege-erinnerungen/{eintrag_id}/erledigen")
async def pflege_erinnerung_erledigen(eintrag_id: int):
    """T-0228 Stufe 1: Erinnerung als erledigt markieren.

    Bei wiederkehrenden Erinnerungen (`intervall_tage`) wird automatisch
    der naechste Eintrag angelegt -- als `folge` zurueckgeliefert.
    """
    if _speicher is None:
        return {"fehler": "Speicher nicht initialisiert"}
    folge = await _speicher.erledige_pflege_erinnerung(eintrag_id)
    return {"ok": True, "folge": folge}


@app.get("/api/ops/timeline")
async def ops_timeline(
    # T-0557: siehe `messwerte` -- gleiche Grenze, gleicher Grund.
    stunden: int = Query(24, ge=1, le=26280, description="Zeitraum in Stunden"),
    severity: str = Query(
        "kritisch,aktion,wetter",
        description="Comma-separated Severity-Filter",
    ),
    zone_id: str | None = Query(None, description="Optionaler Zonenfilter"),
):
    """Normalisierte Timeline fuer Shadow-Review und reale Ereignisse."""
    assert _speicher and _konfig
    von = datetime.now() - timedelta(hours=stunden)
    rohdaten = await _speicher.hole_ops_timeline(von)
    severity_filter = _parse_ops_severity(severity)
    return _baue_ops_timeline(rohdaten, severity_filter, zone_id)


# --- Manuelles Giessen loggen ---

@app.post("/api/giessen")
async def manuelles_giessen(daten: dict):
    """Loggt eine manuelle Bewaesserung (Giesskanne, Schlauch).

    Felder:
    - `zone_id` (Pflicht)
    - `dauer_sekunden` (optional): Sekunden-Variante (Schlauch).
    - `liter` (optional): Liter-Variante (Giesskanne, ml-Dosen). T-0169:
      Wenn nur `liter` kommt, wird intern eine Pseudo-`dauer_sekunden`
      via `bilanz.manuell_liter_pro_minute` errechnet (mind. 1 s, damit
      dauer-basierte ML-Features das Event nicht ausblenden).
    - Mindestens eines von beiden muss > 0 sein. `liter` ist kanonisch
      wenn beide gesetzt sind.
    - `zeitstempel` (optional, ISO 8601): tatsaechlicher Bewaesserungs-
      Zeitpunkt. Default = jetzt. Erlaubt nachtraegliches Loggen mit
      korrekter Historie.
    """
    assert _speicher and _konfig

    zone_id = daten.get("zone_id")
    if not zone_id:
        return {"fehler": "zone_id ist Pflichtfeld"}

    bekannte_zonen = {z.zone_id for z in _konfig.zonen}
    if zone_id not in bekannte_zonen:
        return {"fehler": f"Unbekannte Zone: {zone_id}"}

    # Eingabe parsen + Validation: mind. eines > 0.
    dauer_raw = daten.get("dauer_sekunden")
    liter_raw = daten.get("liter")
    dauer = int(dauer_raw) if dauer_raw is not None else 0
    liter = float(liter_raw) if liter_raw is not None else None
    if dauer < 0:
        return {"fehler": "dauer_sekunden darf nicht negativ sein"}
    if liter is not None and liter < 0:
        return {"fehler": "liter darf nicht negativ sein"}
    if dauer <= 0 and (liter is None or liter <= 0):
        return {"fehler": "dauer_sekunden oder liter > 0 erforderlich"}

    # T-0169 Pseudo-Dauer aus Liter berechnen, wenn nur `liter` kam.
    # `max(1, ...)` verhindert dass kleine Dosen (z. B. 50 ml) als
    # 0-Sekunden-Event durch dauer-basierte Features fallen.
    if liter is not None and liter > 0 and dauer == 0:
        rate = float(_konfig.bilanz.manuell_liter_pro_minute or 0)
        if rate > 0:
            dauer = max(1, int(round(liter * 60.0 / rate)))
        else:
            dauer = 1

    zeitstempel_raw = daten.get("zeitstempel")
    if zeitstempel_raw:
        try:
            # Sowohl Z-Suffix als auch +HH:MM akzeptieren; naive datetime ist lokal
            parse_str = zeitstempel_raw.replace("Z", "+00:00")
            zeitpunkt = datetime.fromisoformat(parse_str)
            if zeitpunkt.tzinfo is not None:
                zeitpunkt = zeitpunkt.astimezone().replace(tzinfo=None)
        except (ValueError, AttributeError):
            return {"fehler": f"zeitstempel muss ISO 8601 sein, war: {zeitstempel_raw!r}"}
    else:
        zeitpunkt = datetime.now()

    ereignis = VentilEreignis(
        zeitstempel=zeitpunkt,
        zone_id=zone_id,
        ventil_id="manuell",
        aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=dauer,
        ausloser=Ausloser.MANUELL,
        liter=liter,
    )
    await _speicher.speichere_ventil_ereignis(ereignis)

    return {
        "ok": True, "zeitstempel": _iso(zeitpunkt), "zone_id": zone_id,
        "liter": liter, "dauer_sekunden": dauer,
    }


@app.post("/api/ventil/manuell-start")
async def ventil_manuell_start(daten: dict):
    """T-0110: Live-Bewaesserung pro Zone starten — echter Ventilbefehl.

    Im Gegensatz zu `/api/giessen` (Logging-only) wird hier wirklich das
    Ventil geoeffnet via VentilSicherung. Funktioniert auch wenn
    `ventilsteuerung_aktiv: false` (das Flag steuert nur den Auto-Loop).

    Felder:
    - `zone_id` (Pflicht): Zone — Kanal wird aus Zone-Konfig abgeleitet
    - `dauer_sekunden` (Pflicht, > 0): wie lange laufen lassen
    """
    assert _konfig and _speicher
    zone_id = daten.get("zone_id")
    dauer_s = daten.get("dauer_sekunden")
    if not zone_id or not dauer_s or dauer_s <= 0:
        return {"ok": False, "fehler": "zone_id + dauer_sekunden > 0 noetig"}
    zone = next(
        (z for z in _konfig.zonen if z.zone_id == zone_id), None,
    )
    if zone is None or zone.ventil_kanal is None:
        return {"ok": False, "fehler": f"Zone {zone_id} hat keinen Ventilkanal"}
    # T-0203: Routing pro DSWC.
    sicherung = _sicherung_fuer_zone(zone_id)
    if not sicherung:
        return {
            "ok": False,
            "fehler": "VentilSicherung nicht verfuegbar (kein Ventil-Geraet)",
        }

    # Geschwister-Zonen am gleichen Kanal UND gleicher DSWC mitnehmen.
    # T-0203 (17.05.): bei Multi-DSWC ist nur (kanal, dswc) eindeutig —
    # vorher reichte der Kanal allein.
    zone_dswc = zone.ventil_geraet_id  # None bei primary
    zone_ids = [
        z.zone_id for z in _konfig.zonen
        if z.ventil_kanal == zone.ventil_kanal
        and z.ventil_geraet_id == zone_dswc
    ]

    # T-0411: laeuft am selben Kanal eine Pre-Soak-Sequenz? Dann NICHT
    # dazwischenfunken. Der Belegt-Guard in `VentilSicherung.starte()`
    # reicht dafuer nicht: in der Pause-Phase ist das Ventil ZU, der Kanal
    # steht also nicht in `_aktiv` -- ein manueller Start waehrend der Pause
    # kaeme durch und kollidierte mit der spaeteren Hauptdose. Betrifft
    # besonders Geschwister-Zonen am geteilten Kanal (bambuswald /
    # bambuswald_yogaraum), deren Karte den fremden Lauf frueher gar nicht
    # anzeigte.
    ps_manager = _pre_soak_manager_fuer_zone(zone_id)
    if ps_manager is not None:
        ps_lauf = ps_manager.aktiver_lauf_fuer_kanal_der_zone(zone_id)
        if ps_lauf is not None:
            return {
                "ok": False,
                "fehler": "PRE_SOAK_AKTIV",
                "grund": (
                    f"Auf Kanal {zone.ventil_kanal} laeuft eine Pre-Soak-Sequenz "
                    f"(Zone {ps_lauf.zone_id}, Phase {ps_lauf.phase}). "
                    "Erst abbrechen oder abwarten."
                ),
                "pre_soak_zone_id": ps_lauf.zone_id,
                "pre_soak_phase": ps_lauf.phase,
            }

    # T-0151: Pre-flight Hahn-Pruefung. Wenn der Hahn belegt ist, klare
    # Fehlermeldung statt generischer "verweigert". T-0536: geht ueber den
    # Arbiter, sieht also auch andere DSWCs und Fremdlaeufe aus der App.
    lock = await sicherung.pruefe_hahn_lock(zone.ventil_kanal)
    if not lock.erlaubt:
        return {
            "ok": False,
            "fehler": "HAHN_BELEGT",
            "grund": lock.grund,
            "aktive_zonen": list(lock.aktive_zonen),
            "verbrauch_aktuell_lpm": lock.verbrauch_aktuell_lpm,
            "verbrauch_neu_lpm": lock.verbrauch_neu_lpm,
            "budget_lpm": lock.budget_lpm,
        }

    # T-0444: Tagesbudget gegenrechnen. VOR dem Start, damit der eigene Lauf
    # nicht schon im Verbrauch steckt. Advisory -- kein Abbruch.
    budget_warnung = await _budget_warnung_fuer_start(zone, float(dauer_s))

    erfolg = await sicherung.bewaessere(
        zone.ventil_kanal, zone_ids, int(dauer_s), Ausloser.MANUELL,
    )
    if not erfolg:
        return {
            "ok": False,
            "fehler": "VentilSicherung verweigerte Start (Watchdog/Block?)",
        }
    antwort = {
        "ok": True,
        "zone_id": zone_id,
        "kanal": zone.ventil_kanal,
        "dauer_sekunden": int(dauer_s),
        "zone_ids_kanal": zone_ids,
    }
    if budget_warnung is not None:
        antwort["budget_warnung"] = budget_warnung
    return antwort


@app.post("/api/ventil/manuell-stop")
async def ventil_manuell_stop(daten: dict):
    """T-0110: Live-Bewaesserung sofort stoppen — echter Stop-Befehl.

    Felder:
    - `zone_id` (Pflicht): Kanal wird aus Zone-Konfig abgeleitet
    """
    assert _konfig
    zone_id = daten.get("zone_id")
    if not zone_id:
        return {"ok": False, "fehler": "zone_id noetig"}
    zone = next(
        (z for z in _konfig.zonen if z.zone_id == zone_id), None,
    )
    if zone is None or zone.ventil_kanal is None:
        return {"ok": False, "fehler": f"Zone {zone_id} hat keinen Ventilkanal"}
    # T-0203: Routing pro DSWC.
    sicherung = _sicherung_fuer_zone(zone_id)
    if not sicherung:
        return {
            "ok": False,
            "fehler": "VentilSicherung nicht verfuegbar",
        }

    erfolg = await sicherung.stoppe(
        zone.ventil_kanal, Ausloser.MANUELL,
    )
    return {
        "ok": bool(erfolg),
        "zone_id": zone_id,
        "kanal": zone.ventil_kanal,
    }


# --- T-0111: Pre-Soak-Sequenz ---

@app.post("/api/ventil/pre-soak-start")
async def ventil_pre_soak_start(daten: dict):
    """T-0111: Pre-Soak-Sequenz starten (Vorwasser + Pause + Hauptdose).

    Sequenz: bewaessere(pre_soak_min) -> sleep(pause_min - pre_soak_min)
    -> bewaessere(haupt_min). Laeuft als asyncio-Task im Hintergrund.

    Felder:
    - `zone_id` (Pflicht)
    - `pre_soak_min` (optional, default aus Zonenkonfig oder 5)
    - `pause_min` (optional, default aus Zonenkonfig.pre_soak_pause_min oder 30)
    - `haupt_min` (Pflicht): Dauer der Hauptdose in Minuten
    """
    assert _konfig
    pre_soak_manager = _pre_soak_manager_fuer_zone(str(daten.get("zone_id", "")))
    if pre_soak_manager is None:
        return {
            "ok": False,
            "fehler": "Pre-Soak-Manager nicht verfuegbar (kein Ventil-Geraet)",
        }
    zone_id = daten.get("zone_id")
    if not zone_id:
        return {"ok": False, "fehler": "zone_id noetig"}
    zone = next(
        (z for z in _konfig.zonen if z.zone_id == zone_id), None,
    )
    if zone is None or zone.ventil_kanal is None:
        return {"ok": False, "fehler": f"Zone {zone_id} hat keinen Ventilkanal"}

    # Defaults aus Zone-Konfig, dann aus Body, dann harte Defaults
    pre_soak_min = int(
        daten.get("pre_soak_min") or zone.pre_soak_min or 5
    )
    pause_min = int(
        daten.get("pause_min") or zone.pre_soak_pause_min or 30
    )
    haupt_min_raw = daten.get("haupt_min")
    if not haupt_min_raw or int(haupt_min_raw) <= 0:
        return {"ok": False, "fehler": "haupt_min > 0 noetig"}
    haupt_min = int(haupt_min_raw)

    # T-0203: Geschwister-Zonen am gleichen (Kanal + DSWC).
    zone_dswc = _zone_zu_dswc.get(zone.zone_id)
    zone_ids_kanal = [
        z.zone_id for z in _konfig.zonen
        if z.ventil_kanal == zone.ventil_kanal
        and _zone_zu_dswc.get(z.zone_id) == zone_dswc
    ]

    # T-0151: Pre-flight Hahn-Cluster-Pruefung (siehe manuell-start).
    pre_soak_sicherung = _sicherung_fuer_zone(zone.zone_id)
    if pre_soak_sicherung is not None:
        lock = await pre_soak_sicherung.pruefe_hahn_lock(zone.ventil_kanal)
        if not lock.erlaubt:
            return {
                "ok": False,
                "fehler": "HAHN_BELEGT",
                "grund": lock.grund,
                "aktive_zonen": list(lock.aktive_zonen),
                "verbrauch_aktuell_lpm": lock.verbrauch_aktuell_lpm,
                "verbrauch_neu_lpm": lock.verbrauch_neu_lpm,
                "budget_lpm": lock.budget_lpm,
            }

    # T-0444: Tagesbudget gegenrechnen. Geplant ist die WASSERZEIT der
    # Sequenz (Vorwaesser-Puls + Hauptdose); die Soak-Pause dazwischen ist
    # ventil-zu und verbraucht nichts.
    budget_warnung = await _budget_warnung_fuer_start(
        zone, float((pre_soak_min + haupt_min) * 60),
    )

    ok, fehler = await pre_soak_manager.starte(  # type: ignore[union-attr]
        zone_id=zone_id,
        kanal=zone.ventil_kanal,
        zone_ids_kanal=zone_ids_kanal,
        pre_soak_min=pre_soak_min,
        pause_min=pause_min,
        haupt_min=haupt_min,
    )
    if not ok:
        return {"ok": False, "fehler": fehler}
    antwort = {
        "ok": True,
        "zone_id": zone_id,
        "kanal": zone.ventil_kanal,
        "pre_soak_min": pre_soak_min,
        "pause_min": pause_min,
        "haupt_min": haupt_min,
    }
    if budget_warnung is not None:
        antwort["budget_warnung"] = budget_warnung
    return antwort


@app.post("/api/ventil/pre-soak-stop")
async def ventil_pre_soak_stop(daten: dict):
    """T-0111: Laufende Pre-Soak-Sequenz abbrechen + Ventil sofort stoppen."""
    zone_id = daten.get("zone_id")
    if not zone_id:
        return {"ok": False, "fehler": "zone_id noetig"}
    pre_soak_manager = _pre_soak_manager_fuer_zone(zone_id)
    if pre_soak_manager is None:
        return {"ok": False, "fehler": "Pre-Soak-Manager nicht verfuegbar"}
    ok, fehler = await pre_soak_manager.stoppe(zone_id)  # type: ignore[union-attr]
    if not ok:
        return {"ok": False, "fehler": fehler}
    return {"ok": True, "zone_id": zone_id}


@app.get("/api/ventil/pre-soak-status")
async def ventil_pre_soak_status(zone_id: str | None = Query(default=None)):
    """T-0111: Status laufender Pre-Soak-Sequenzen.

    Ohne `zone_id`: alle laufenden + zuletzt beendete Sequenzen.
    Mit `zone_id`: der Lauf, der diese Zone PHYSISCH betrifft -- also auch
    ein Lauf, den eine Geschwister-Zone am selben Ventil-Kanal gestartet hat
    (T-0411). Solche Laeufe sind mit `fremd: true` markiert, damit die UI
    "Nachbarzone giesst gerade" von "diese Zone giesst" unterscheiden kann.
    """
    if _pre_soak_manager is None and not _pre_soak_managers:
        return {"laeufe": []}
    if zone_id:
        manager = _pre_soak_manager_fuer_zone(zone_id)
        lauf = manager.lauf_fuer_kanal_der_zone(zone_id) if manager else None  # type: ignore[union-attr]
        if lauf is None:
            return {"laeufe": []}
        eintrag = lauf.status_dict()
        eintrag["fremd"] = lauf.zone_id != zone_id
        return {"laeufe": [eintrag]}
    # Alle bekannten Laeufe (auch fertig/fehler) liefern, damit das
    # Frontend abgeschlossene Phasen einmalig anzeigen kann.
    manager_liste = list(_pre_soak_managers.values()) or [_pre_soak_manager]
    all_laeufe = []
    for manager in manager_liste:
        if manager is None:
            continue
        all_laeufe.extend(manager._laeufe.values())  # type: ignore[attr-defined]
    return {"laeufe": [l.status_dict() for l in all_laeufe]}


def _ventil_ereignis_zu_dict(e) -> dict:
    """Serialisiert VentilEreignis fuer die API inkl. DB-ID."""
    return {
        "id": e.id,
        "zeitstempel": _iso(e.zeitstempel),
        "zone_id": e.zone_id,
        "ventil_id": e.ventil_id,
        "aktion": e.aktion.value,
        "dauer_sekunden": e.dauer_sekunden,
        "ausloser": e.ausloser.value,
        "liter": e.liter,
        "lauf_gruppe": getattr(e, "lauf_gruppe", None),
        "phase": getattr(e, "phase", None),
        # T-0453: Cross-Spray-Quelle bei ausloser='fremdwasser', sonst None.
        "quell_zone": getattr(e, "quell_zone", None),
    }


@app.get("/api/ventil-ereignisse")
async def ventil_ereignisse(
    von: str | None = Query(default=None, description="ISO 8601"),
    bis: str | None = Query(default=None, description="ISO 8601"),
    zone_id: str | None = Query(default=None),
):
    """Listet Ventil-Events fuer den Ops-Tab (T-0055-B2). Default: letzte 24h."""
    assert _speicher
    jetzt = datetime.now()
    # T-0565: derselbe Helfer wie an den anderen beiden Endpoints. Die
    # Umrechnung stand hier inline und war genau deshalb dort vergessen
    # worden.
    von_dt = _parse_zeitgrenze(von) or jetzt - timedelta(hours=24)
    bis_dt = _parse_zeitgrenze(bis) or jetzt
    zone_ids = [zone_id] if zone_id else None
    ereignisse = await _speicher.hole_ventil_ereignisse_fenster(von_dt, bis_dt, zone_ids)
    return [_ventil_ereignis_zu_dict(e) for e in ereignisse]


@app.patch("/api/ventil-ereignis/{ereignis_id}")
async def patch_ventil_ereignis(
    ereignis_id: int, daten: dict,
    paar: bool = Query(default=False, description="Auch das OEFFNEN/SCHLIESSEN-Paar aendern"),
):
    """Aendert einzelne Felder eines Ventil-Events (T-0055-B2).

    Akzeptierte Felder im Body:
    - `ausloser`: "automatik" | "manuell" | "unbekannt" (und andere Enum-Werte)
    - `zeitstempel`: ISO 8601 (korrigiert falsche Log-Zeit) — **NICHT** auf Paar anwenden
    - `liter`: explizite Liter-Angabe
    - `dauer_sekunden`: Dauer-Korrektur — **NICHT** auf Paar anwenden
    - `quell_zone`: T-0453, nur zusammen mit `ausloser="fremdwasser"` sinnvoll.
      Ohne Angabe wird die Quelle aus den echten Laeufen im Karenz-Fenster
      abgeleitet (nur bei Eindeutigkeit). Bei jedem anderen Ausloeser raeumt
      der Speicher eine vorhandene `quell_zone` weg.

    Mit `?paar=true` wird `ausloser`/`liter` auch auf das zugehoerige
    OEFFNEN/SCHLIESSEN-Pendant angewandt — damit die UI Heuristik-Events
    paarweise klassifizieren kann.
    """
    assert _speicher

    ausloser: Ausloser | None = None
    if "ausloser" in daten and daten["ausloser"] is not None:
        try:
            ausloser = Ausloser(daten["ausloser"])
        except ValueError:
            return {"fehler": f"Ungueltiger ausloser: {daten['ausloser']!r}"}

    zeitpunkt: datetime | None = None
    if "zeitstempel" in daten and daten["zeitstempel"] is not None:
        try:
            raw = daten["zeitstempel"].replace("Z", "+00:00")
            zeitpunkt = datetime.fromisoformat(raw)
            if zeitpunkt.tzinfo is not None:
                zeitpunkt = zeitpunkt.astimezone().replace(tzinfo=None)
        except (ValueError, AttributeError):
            return {"fehler": f"Ungueltiger zeitstempel: {daten['zeitstempel']!r}"}

    liter = float(daten["liter"]) if daten.get("liter") is not None else None
    dauer = int(daten["dauer_sekunden"]) if daten.get("dauer_sekunden") is not None else None

    # T-0453: Cross-Spray-Quelle. Explizite Angabe gewinnt; sonst einmal
    # ableiten (VOR dem Update, weil die Ableitung den alten Zustand liest)
    # und auf beide Haelften des Paars schreiben.
    quell_zone: str | None = daten.get("quell_zone") or None
    if ausloser is Ausloser.FREMDWASSER and quell_zone is None:
        quell_zone = await _ermittle_fremdwasser_quelle(ereignis_id)

    ziel_ids = [ereignis_id]
    if paar:
        ziel_ids = await _speicher.finde_ventil_paar(ereignis_id) or [ereignis_id]

    geaendert_anzahl = 0
    for zid in ziel_ids:
        # zeitstempel + dauer_sekunden nur auf das urspruengliche Event anwenden
        # (gegenueberliegendes Event hat anderen Zeitstempel/andere Dauer).
        if zid != ereignis_id:
            erfolg = await _speicher.aktualisiere_ventil_ereignis(
                zid, ausloser=ausloser, liter=liter, quell_zone=quell_zone,
            )
        else:
            erfolg = await _speicher.aktualisiere_ventil_ereignis(
                zid, ausloser=ausloser, zeitstempel=zeitpunkt,
                liter=liter, dauer_sekunden=dauer, quell_zone=quell_zone,
            )
        if erfolg:
            geaendert_anzahl += 1

    if geaendert_anzahl == 0:
        return {"fehler": f"Event {ereignis_id} nicht gefunden oder keine Aenderung"}

    # T-0205: nach erfolgreichem Flip auf IGNORIERT die Heuristik in der
    # Karenz-Periode neu scannen lassen. Vorher war die Karenz aktiv
    # (Live-Event existierte), Sensor-Spruenge in der Zeit wurden
    # unterdrueckt. Nach dem Flip sind diese Spruenge wieder als
    # UNBEKANNT-Heuristik-Events legitim.
    if ausloser == Ausloser.IGNORIERT and _sensor_backfill_job is not None:
        await _trigger_rescan_nach_flip(ziel_ids)

    return {"ok": True, "ids": ziel_ids, "geaendert": geaendert_anzahl}


async def _ermittle_fremdwasser_quelle(
    ereignis_id: int,
) -> str | None:
    """T-0453: Aus welcher Zone kam das Wasser dieses Cross-Spray-Sprungs?

    Sucht im Versickerungs-Karenz-Fenster VOR dem Ereignis nach echten
    Kanal-Laeufen anderer Zonen. Nur eine EINDEUTIGE Quelle wird
    zurueckgegeben; bei null oder mehreren Kandidaten None -- ein geratener
    Zonenname waere schlimmer als kein Name, weil er in einer spaeteren
    Cross-Spray-Analyse wie ein Messwert gelesen wuerde.

    Bewusst NICHT ueber `cross_spray_quell_zonen`: dieser Pfad greift genau
    dann, wenn der User ein `unbekannt`-Ereignis manuell auf `fremdwasser`
    flippt -- also fuer Zonen OHNE konfigurierte Quelle (heute bambuswald,
    yogaraum). Ist die Quelle konfiguriert, hat `sensor_backfill.
    _cross_spray_quelle` die `quell_zone` schon beim Schreiben gesetzt
    (T-0469) und es gibt hier nichts mehr abzuleiten. Beide Wege enden im
    selben Ereignis-Vertrag, nur die Quelle der Wahrheit ist eine andere:
    dort die gepflegte Topologie, hier die Rueckrechnung aus echten Laeufen.

    Zonen auf DEMSELBEN Kanal (bambuswald + bambuswald_yogaraum teilen
    Kanal 2) sind ausgeschlossen: deren Events beschreiben denselben
    physischen Lauf und waeren keine Fremdquelle.
    """
    if not _speicher or not _konfig:
        return None
    ereignis = await _speicher.hole_ventil_ereignis(ereignis_id)
    if ereignis is None:
        return None
    zone = next(
        (z for z in _konfig.zonen if z.zone_id == ereignis.zone_id), None,
    )
    if zone is None:
        return None
    eigener_kanal = (zone.ventil_geraet_id, zone.ventil_kanal)
    von = ereignis.zeitstempel - timedelta(
        hours=max(1, int(zone.versickerungs_karenz_stunden)),
    )

    kandidaten: set[str] = set()
    for andere in _konfig.zonen:
        if andere.zone_id == zone.zone_id or andere.ventil_kanal is None:
            continue
        if (andere.ventil_geraet_id, andere.ventil_kanal) == eigener_kanal:
            continue
        try:
            events = await _speicher.hole_ventil_ereignisse(
                andere.zone_id, von=von, bis=ereignis.zeitstempel,
            )
        except Exception:
            logger.exception(
                "api.fremdwasser_quelle_query_fehler",
                zone_id=andere.zone_id,
            )
            continue
        # Echter Lauf = kein Heuristik-/Schlauch-Pseudo-Event und ein
        # Ausloeser, der Kanal-Wasser bedeutet.
        if any(
            e.ventil_id not in ("sensor_heuristik", "manuell")
            and e.ausloser in ECHTES_KANAL_WASSER
            for e in events
        ):
            kandidaten.add(andere.zone_id)

    if len(kandidaten) == 1:
        return kandidaten.pop()
    logger.info(
        "api.fremdwasser_quelle_uneindeutig",
        ereignis_id=ereignis_id, kandidaten=sorted(kandidaten),
    )
    return None


async def _trigger_rescan_nach_flip(ereignis_ids: list[int]) -> None:
    """T-0205: Helper -- Re-Scan der Heuristik fuer jede Zone der
    geflipten Events. Wir gruppieren erst nach `zone_id`, damit pro
    Zone nur einmal gescannt wird (Bulk-Flip mit 23 Events der gleichen
    Zone -> 1 Scan, nicht 23).

    Errors werden gefangen und geloggt; der Flip selbst war schon
    erfolgreich, der Re-Scan ist Best-Effort.
    """
    # T-0297: _sensor_backfill_job darf None sein (Tests) -- der LeckDetektor-
    # Re-Check unten haengt nicht daran. Nur _speicher ist Pflicht (Event-Lookup).
    if _speicher is None:
        return
    pro_zone: dict[str, datetime] = {}
    for eid in ereignis_ids:
        ereignis = await _speicher.hole_ventil_ereignis(eid)
        if ereignis is None:
            continue
        # SCHLIESSEN-Zeitstempel ist der relevante "Mitte"-Punkt fuer
        # die Karenz. OEFFNEN-Events ueberspringen -- jedes Paar liefert
        # genau ein SCHLIESSEN.
        if ereignis.aktion != VentilAktion.SCHLIESSEN:
            continue
        prev = pro_zone.get(ereignis.zone_id)
        if prev is None or ereignis.zeitstempel > prev:
            pro_zone[ereignis.zone_id] = ereignis.zeitstempel

    if _sensor_backfill_job is not None:
        for zone_id, mitte in pro_zone.items():
            try:
                n = await _sensor_backfill_job.rescan_zone_nach_flip(zone_id, mitte)
                logger.info(
                    "api.flip_rescan",
                    zone_id=zone_id, neue_events=n,
                    mitte=mitte.isoformat(timespec="minutes"),
                )
            except Exception:
                logger.exception("api.flip_rescan.fehler", zone_id=zone_id)

    # T-0297: ignoriert-Flip eines Heuristik-Events kann eine offene
    # "Bewaesserung ohne Wirkung"-Warnung verwaisen lassen (Realfall
    # waldblumenhain: 5 Tage haengend). Detektor sofort fuer die
    # betroffenen Zonen re-evaluieren -> Self-Heal-Pfad schliesst die
    # Warnung jetzt statt erst beim naechsten ~5-min-Tick. Best-Effort.
    if _leck_detektor is not None and pro_zone:
        try:
            await _leck_detektor.pruefe_alle(list(pro_zone))
        except Exception:
            logger.exception("api.flip_leck_recheck.fehler")


@app.post("/api/ventil-ereignisse/klassifiziere-bulk")
async def klassifiziere_bulk(daten: dict):
    """T-0212a (2026-05-19): Bulk-Klassifikation mehrerer Ventil-Events.

    Body:
    - `ids` (Pflicht): Liste von Event-IDs
    - `ausloser` (Pflicht): Ziel-Status ("ignoriert", "manuell", ...)
    - `paar` (optional, default true): auch das OEFFNEN/SCHLIESSEN-
      Pendant mit-aktualisieren (analog Single-Endpoint)
    - `quell_zone` (optional, T-0453): nur mit `ausloser="fremdwasser"`.
      Ohne Angabe wird pro Event abgeleitet (nur bei Eindeutigkeit).

    Hintergrund: Sensor-Heuristik kann unter Phantom-Mass-Aufkommen
    (z.B. waehrend Bodenart-Reset-Phasen) 20+ UNBEKANNT-Events
    pro Nacht erzeugen. User klassifiziert sie alle als
    "ignoriert/regen-glitch" — pro Klick ein Rate-Limit-Token
    (5/min/key) waere ein 4-Minuten-Klick-Marathon. Mit Bulk-Endpoint
    EIN Request fuer N Events.

    Antwort enthaelt Anzahl tatsaechlich geaenderter Zeilen
    (paar-Pendant zaehlt dazu).
    """
    assert _speicher
    ids_raw = daten.get("ids")
    if not isinstance(ids_raw, list) or not ids_raw:
        return {"ok": False, "fehler": "ids: nicht-leere Liste noetig"}
    try:
        ids = [int(x) for x in ids_raw]
    except (TypeError, ValueError):
        return {"ok": False, "fehler": "ids muss Integer-Liste sein"}
    ausloser_str = daten.get("ausloser")
    if not ausloser_str:
        return {"ok": False, "fehler": "ausloser noetig"}
    try:
        ausloser = Ausloser(ausloser_str)
    except ValueError:
        return {"ok": False, "fehler": f"Ungueltiger ausloser: {ausloser_str!r}"}
    paar = bool(daten.get("paar", True))

    # T-0453: explizite Quelle gilt fuer alle Events des Requests; sonst
    # wird sie PRO URSPRUNGS-EVENT abgeleitet (jedes hat sein eigenes
    # Karenz-Fenster) und auf dessen Paar-Pendant mit-uebertragen.
    quell_zone_explizit: str | None = daten.get("quell_zone") or None
    ist_fremdwasser = ausloser is Ausloser.FREMDWASSER

    # Pro Event paar-Pendant ermitteln, Doppel-Update via Set vermeiden.
    alle_ziel_ids: set[int] = set()
    quelle_pro_id: dict[int, str | None] = {}
    for eid in ids:
        alle_ziel_ids.add(eid)
        gruppe = [eid]
        if paar:
            paar_ids = await _speicher.finde_ventil_paar(eid)
            if paar_ids:
                alle_ziel_ids.update(paar_ids)
                gruppe = list(paar_ids)
        if ist_fremdwasser:
            quelle = quell_zone_explizit
            if quelle is None:
                quelle = await _ermittle_fremdwasser_quelle(eid)
            for gid in gruppe:
                # Erster Treffer gewinnt: bei ueberlappenden Paaren soll
                # nicht das zuletzt betrachtete Event die Quelle ueberschreiben.
                quelle_pro_id.setdefault(gid, quelle)

    geaendert_anzahl = 0
    for zid in alle_ziel_ids:
        erfolg = await _speicher.aktualisiere_ventil_ereignis(
            zid, ausloser=ausloser, quell_zone=quelle_pro_id.get(zid),
        )
        if erfolg:
            geaendert_anzahl += 1

    # T-0205: analog Single-Endpoint -- nach Flip auf IGNORIERT die
    # Heuristik in den Karenz-Perioden der betroffenen Zonen neu
    # scannen lassen. Pro Zone nur ein Scan, auch wenn 23 Events
    # geflipt wurden (Trigger-Logik dedupliziert in `_trigger_rescan`).
    if ausloser == Ausloser.IGNORIERT and _sensor_backfill_job is not None:
        await _trigger_rescan_nach_flip(sorted(alle_ziel_ids))

    return {
        "ok": True,
        "ids_request": ids,
        "ids_geaendert": sorted(alle_ziel_ids),
        "geaendert": geaendert_anzahl,
    }


@app.delete("/api/ventil-ereignis/{ereignis_id}")
async def delete_ventil_ereignis(
    ereignis_id: int,
    paar: bool = Query(default=False, description="Auch das OEFFNEN/SCHLIESSEN-Paar loeschen"),
):
    """Loescht ein Ventil-Event (T-0055-B2: "war Regen" / "Fehleingabe").

    Mit `?paar=true` wird auch der zugehoerige OEFFNEN/SCHLIESSEN-Pendant
    geloescht — Heuristik-Events kommen immer als Paar, einzeln loeschen
    waere fachlich falsch.
    """
    assert _speicher
    ziel_ids = [ereignis_id]
    if paar:
        ziel_ids = await _speicher.finde_ventil_paar(ereignis_id) or [ereignis_id]

    geloescht_anzahl = 0
    for zid in ziel_ids:
        if await _speicher.loesche_ventil_ereignis(zid):
            geloescht_anzahl += 1
    if geloescht_anzahl == 0:
        return {"fehler": f"Event {ereignis_id} nicht gefunden"}
    return {"ok": True, "ids": ziel_ids, "geloescht": geloescht_anzahl}


# --- Ventilsteuerung ---

@app.post("/api/notfall-stopp")
async def notfall_stopp():
    """Schliesst ALLE offenen Ventile sofort.

    T-0203 (17.05.): Iteriert ueber alle DSWC-Sicherungen, damit auch
    die zweite Dual Water Control mitgestoppt wird.
    """
    sicherungen = list(_ventil_sicherungen.values()) or (
        [_ventil_sicherung] if _ventil_sicherung else []
    )
    if not sicherungen:
        return {"fehler": "Keine Ventilsteuerung verfuegbar"}
    geschlossen_alle = 0
    fehlgeschlagen_alle: list = []
    for sicherung in sicherungen:
        try:
            ergebnis = await sicherung.notfall_stopp()
            geschlossen_alle += int(ergebnis.get("geschlossen", 0) or 0)
            fehlgeschlagen_alle.extend(ergebnis.get("fehlgeschlagen", []))
        except Exception as exc:
            fehlgeschlagen_alle.append({
                "geraet_id": getattr(sicherung, "_ventil_geraet_id", "?"),
                "fehler": str(exc),
            })
    if fehlgeschlagen_alle:
        return {
            "ok": False,
            "fehler": f"{len(fehlgeschlagen_alle)} Kanaele konnten nicht geschlossen werden",
            "geschlossen": geschlossen_alle,
            "fehlgeschlagen": fehlgeschlagen_alle,
        }
    return {"ok": True, "geschlossen": geschlossen_alle}


@app.get("/api/ventil-status")
async def ventil_status():
    """Status aller aktiven Bewaesserungen.

    Liefert sowohl Backend-eigene (von VentilSicherung verwaltete) als auch
    externe Bewaesserungen (Gardena-App / Cloud-Schedule) — Letztere via
    Live-WebSocket-State des GardenaClient.

    Format:
    {
      "aktiv": {<kanal>: {zone_ids, dauer_s, gestartet, ausloser, verbleibend_s,
                          quelle: "backend"}},
      "extern": {<kanal>: {zone_ids, gestartet, activity, quelle: "extern",
                           stoppbar: false}}
    }

    `quelle="backend"` -> Stop-Button im Frontend aktiv.
    `quelle="extern"` -> nur Anzeige, Stop muss in der Gardena-App passieren.
    """
    # T-0203 (17.05.): Multi-DSWC. Iteriere ueber alle Sicherungen.
    sicherungen = list(_ventil_sicherungen.values()) or (
        [_ventil_sicherung] if _ventil_sicherung else []
    )
    if not sicherungen:
        return {"aktiv": {}, "extern": {}}

    aktiv: dict[str, dict] = {}
    backend_valve_ids: set = set()
    for sicherung in sicherungen:
        ergebnis = sicherung.aktive_bewaesserungen()
        for kanal_key, v in ergebnis.items():
            v["quelle"] = "backend"
            v["stoppbar"] = True
            # T-0203: bei Multi-DSWC ist Kanal-Key allein mehrdeutig.
            # Schluessel auf DSWC-prefix erweitern, falls Kollision.
            schluessel = (
                f"{sicherung._ventil_geraet_id[:8]}:{kanal_key}"
                if kanal_key in aktiv and len(sicherungen) > 1
                else kanal_key
            )
            aktiv[schluessel] = v
        backend_valve_ids.update(
            getattr(sicherung._aktiv[k], "valve_id", None)
            for k in sicherung._aktiv
        )

    # Externe Bewaesserungen: Live-WS-State zeigt offene Valves an, die NICHT
    # vom Backend stammen. Mapping valve_id -> kanal -> zone_ids pro DSWC.
    extern: dict[str, dict] = {}
    if _konfig is not None:
        # Client aus erster Sicherung holen (alle teilen denselben GardenaClient).
        client = getattr(sicherungen[0], "_client", None)
        offene = client.offene_valves() if client is not None else {}
        # valve_id -> (kanal, dswc_id) Mapping ueber alle Sicherungen.
        valve_zu_kanal_dswc: dict[str, tuple[int, str]] = {}
        for sicherung in sicherungen:
            kanal_zu_valve = getattr(
                sicherung, "_kanal_zu_valve_id", {},
            ) or {}
            dswc = getattr(sicherung, "_ventil_geraet_id", "")
            for k, v in kanal_zu_valve.items():
                valve_zu_kanal_dswc[v] = (k, dswc)
        for valve_id, daten in offene.items():
            if valve_id in backend_valve_ids:
                continue
            mapping = valve_zu_kanal_dswc.get(valve_id)
            if mapping is None:
                continue
            kanal, dswc = mapping
            # T-0203: Geschwister-Zonen am gleichen Kanal UND gleicher DSWC.
            zonen_ids = [
                z.zone_id for z in _konfig.zonen
                if z.ventil_kanal == kanal
                and (z.ventil_geraet_id or "") == (dswc or "")
            ]
            offen_seit = daten.get("offen_seit")
            schluessel = (
                f"{dswc[:8]}:{kanal}" if len(sicherungen) > 1 else str(kanal)
            )
            extern[schluessel] = {
                "zone_ids": zonen_ids,
                "gestartet": (
                    offen_seit.astimezone().isoformat() if offen_seit else None
                ),
                "activity": daten.get("activity"),
                "quelle": "extern",
                "stoppbar": False,
            }
    return {"aktiv": aktiv, "extern": extern}


# --- ML-Vorhersage ---

@app.get("/api/ml/status")
async def ml_status():
    """Status des ML-Modells + letzter Retrain-Lauf (T-0048)."""
    basis: dict = {"ist_geladen": False, "hinweis": "Kein ML-Modell geladen"}
    if _ml_service is not None and _ml_service.ist_verfuegbar:
        basis = _ml_service.status().model_dump(mode="json")

    # T-0048: Auto-Retrain-Statistik. T-0108: zusaetzlich `letzter_erfolg`
    # + `letzter_fehler`, damit das Frontend ein Banner zeigen kann, wenn
    # ein Trainingslauf still gecrasht ist.
    if _ml_retrain_job is not None:
        basis["retrain"] = {
            "aktiv": _ml_retrain_job._retrain.aktiv,
            "gate_faktor": _ml_retrain_job._retrain.gate_faktor,
            "intervall_tage": _ml_retrain_job._retrain.intervall_tage,
            "letzter_lauf": (
                _ml_retrain_job._letzte_aktualisierung.isoformat()
                if _ml_retrain_job._letzte_aktualisierung else None
            ),
            "letztes_ergebnis": _ml_retrain_job.letztes_ergebnis,
            "letzter_erfolg": (
                _iso(_ml_retrain_job.letzter_erfolg)
                if _ml_retrain_job.letzter_erfolg else None
            ),
            "letzter_fehler": _ml_retrain_job.letzter_fehler,
        }

    # T-0108: Response-Retrain-Job — Crash war vorher nur im Log.
    if _ml_response_retrain_job is not None:
        basis["response_retrain"] = {
            "aktiv": _ml_response_retrain_job._resp.aktiv,
            "letzter_erfolg": (
                _iso(_ml_response_retrain_job.letzter_erfolg)
                if _ml_response_retrain_job.letzter_erfolg else None
            ),
            "letzter_fehler": _ml_response_retrain_job.letzter_fehler,
        }

    # T-0108: Kalibrations-Job — Crash war vorher nur im Log.
    if _kalibrations_job is not None:
        basis["kalibrierung"] = {
            "aktiv": _kalibrations_job._kal.aktiv,
            "letzter_erfolg": (
                _iso(_kalibrations_job.letzter_erfolg)
                if _kalibrations_job.letzter_erfolg else None
            ),
            "letzter_fehler": _kalibrations_job.letzter_fehler,
        }

    # T-0181: Skalen-Mapping-Fit-Job. Greift erst nach Sammel-Phase
    # (min_spannweite_pp) -- `letzter_erfolg` heisst "Scan lief", nicht
    # "Fit wurde geschrieben". Die geschriebenen Mappings stehen in
    # Tabelle `sensor_skalen_mapping`.
    if _skalen_mapping_fit_job is not None:
        basis["skalen_mapping"] = {
            "aktiv": _skalen_mapping_fit_job._sm.aktiv,
            "letzter_erfolg": (
                _iso(_skalen_mapping_fit_job.letzter_erfolg)
                if _skalen_mapping_fit_job.letzter_erfolg else None
            ),
            "letzter_fehler": _skalen_mapping_fit_job.letzter_fehler,
        }

    # Hybrid Stufe 1: Physik-k_basis-Fit-Job. `letzter_erfolg` heisst
    # "Scan lief"; gefittete Werte stehen in Tabelle `physik_k_basis`.
    if _k_basis_fit_job is not None:
        basis["physik_diagnose"] = {
            "aktiv": _k_basis_fit_job._physik.aktiv,
            "letzter_erfolg": (
                _iso(_k_basis_fit_job.letzter_erfolg)
                if _k_basis_fit_job.letzter_erfolg else None
            ),
            "letzter_fehler": _k_basis_fit_job.letzter_fehler,
        }

    # T-0292 Stufe 2: Plateau-Wirkungs-Fit-Job. `aktiv` = Job laeuft;
    # `adoptieren` = Engine nutzt die Fits (Default False). `letzter_erfolg`
    # heisst "Scan lief"; die Fits stehen in Tabelle `wirkung_fit`.
    if _wirkung_fit_job is not None:
        basis["wirkung_fit"] = {
            "aktiv": _wirkung_fit_job._wf.aktiv,
            "adoptieren": _wirkung_fit_job._wf.adoptieren,
            "letzter_erfolg": (
                _iso(_wirkung_fit_job.letzter_erfolg)
                if _wirkung_fit_job.letzter_erfolg else None
            ),
            "letzter_fehler": _wirkung_fit_job.letzter_fehler,
        }
    return basis


@app.get("/api/ml/vorhersage/{zone_id}")
async def ml_vorhersage(
    zone_id: str,
    details: str | None = Query(default=None, pattern="^(top_features)$"),
):
    """ML-Feuchte-Vorhersage fuer eine Zone (6h/12h/24h).

    `details=top_features` (T-0040) liefert zusaetzlich die 5 wichtigsten
    Feature-Beitraege (SHAP/pred_contrib) pro Horizont. Default ohne
    Details, weil SHAP ca. 10-30 ms pro Horizont extra kostet und das
    Dashboard den Aufruf alle 60 s macht.
    """
    if _ml_service is None or not _ml_service.ist_verfuegbar:
        return {"fehler": "ML-Modell nicht verfuegbar"}

    assert _speicher and _konfig and _verarbeiter

    # T-0179c: Aggregat ueber alle Sensoren der Zone (Median).
    # T-0476: gleiches Abruf-Fenster + 48-h-Kappung wie die Zonen-Karte.
    _api_jetzt = datetime.now()
    letzter_wert = await _speicher.letzte_messung_aggregiert(
        zone_id,
        fenster_minuten=AGGREGAT_FALLBACK_FENSTER_MIN,
        jetzt=_api_jetzt,
    )
    if not letzter_wert:
        letzter_wert = _cache_wert_wenn_frisch(
            _verarbeiter.hole_letzten_wert(zone_id), _api_jetzt,
        )
    if not letzter_wert or letzter_wert.boden_feuchte is None:
        return {"fehler": "Keine aktuellen Sensordaten"}

    # Volle Feature-Extraktion aus DB (gleiche Features wie beim Training)
    ergebnisse_roh = await _ml_service.live_vorhersage(
        zone_id, _speicher, _konfig,
        details=(details == "top_features"),
    )

    # T-0573/T-0577: das Guete-Urteil haengt schon an der Prognose
    # (`live_vorhersage`), gemeinsamer Dict-Bauer mit dem Snapshot.
    ergebnisse = {key: _ml_eintrag_dict(v) for key, v in ergebnisse_roh.items()}

    if not ergebnisse:
        # Fallback: kein Feature-Vektor moeglich (z.B. zu wenig Daten)
        return {"fehler": "Feature-Extraktion fehlgeschlagen — zu wenig Daten?"}

    return ergebnisse


@app.get("/api/ml/drift")
async def ml_drift(zone_id: str | None = None, fenster: str = "7d"):
    """T-0047: rollender MAE pro Horizont gegen Baseline aus Training.

    fenster: "7d" | "30d" | Ntage (z. B. "3d").
    Ampel-Schwelle: aktueller_mae > 1.5 * baseline_mae → status "rot".
    """
    if _speicher is None:
        return {"fehler": "Speicher nicht initialisiert"}

    # Fenster parsen (Standard 7 Tage)
    tage = 7
    if fenster and fenster.endswith("d"):
        try:
            tage = max(1, int(fenster[:-1]))
        except ValueError:
            pass

    metriken = await _speicher.hole_drift_metriken(zone_id, tage)
    status_pro_horizont = await _speicher.hole_drift_status(zone_id, tage)

    baseline: dict[int, float] = {}
    baseline_quelle: str | None = None
    if _ml_service is not None and _ml_service.ist_verfuegbar:
        status = _ml_service.status()
        baseline_quelle = status.trainiert_am
        for m in status.metriken:
            baseline[int(m.horizont_stunden)] = float(m.mae)

    horizonte = []
    for h in sorted(set(list(metriken.keys()) + list(baseline.keys())
                         + list(status_pro_horizont.keys()))):
        aktueller_mae = metriken.get(h, {}).get("mae")
        basis_mae = baseline.get(h)
        n_eval = metriken.get(h, {}).get("n", 0)
        st = status_pro_horizont.get(h, {})
        n_offen = int(st.get("n_offen_im_fenster", 0))
        ampel = "keine_daten"
        if aktueller_mae is not None and basis_mae is not None:
            ampel = "rot" if aktueller_mae > 1.5 * basis_mae else "gruen"
        elif aktueller_mae is not None:
            ampel = "unbekannt"  # kein Baseline-Wert
        elif n_offen > 0:
            # Daten existieren, aber Drift-Job hat sie noch nicht
            # evaluiert. Klar abgrenzen von "wirklich nichts geloggt".
            ampel = "backlog"
        horizonte.append({
            "horizont_h": h,
            "mae_aktuell": aktueller_mae,
            "n": n_eval,
            "n_offen_im_fenster": n_offen,
            "n_offen_total": int(st.get("n_offen_total", 0)),
            "letzte_evaluierung": st.get("letzte_evaluierung"),
            "mae_baseline": basis_mae,
            "ampel": ampel,
        })

    return {
        "zone_id": zone_id,
        "fenster_tage": tage,
        "baseline_trainiert_am": baseline_quelle,
        "horizonte": horizonte,
    }


@app.get("/api/ml/drift/log")
async def ml_drift_log(
    zone_id: str | None = None,
    horizont: int | None = None,
    n: int = 50,
    nur_evaluiert: bool = True,
):
    """Inspektor-Sicht: rohe Drift-Log-Zeilen "Prognose vs. Ist".

    Liefert die letzten `n` Eintraege aus `ml_vorhersage_log`, sortiert
    nach Inferenz-Zeitstempel absteigend. Mit Quantil-Band (q10/q90)
    fuer Bandbreite-Visualisierung. `n` wird auf [1, 500] geclippt.

    Beispiele:
      /api/ml/drift/log?zone_id=bambuswald&horizont=6&n=20
        → die letzten 20 evaluierten 6h-Bambus-Prognosen
      /api/ml/drift/log?n=50&nur_evaluiert=false
        → die letzten 50 Inferenzen ueberhaupt (auch noch offene)
    """
    if _speicher is None:
        return {"fehler": "Speicher nicht initialisiert"}
    if horizont is not None and horizont not in (6, 12, 24):
        return {"fehler": "horizont muss 6, 12 oder 24 sein"}

    eintraege = await _speicher.hole_drift_log(
        zone_id=zone_id,
        horizont_h=horizont,
        n=n,
        nur_evaluiert=nur_evaluiert,
    )
    return {
        "zone_id": zone_id,
        "horizont": horizont,
        "n_zurueck": len(eintraege),
        "nur_evaluiert": nur_evaluiert,
        "eintraege": eintraege,
    }


@app.get("/api/ml/dauer-drift")
async def ml_dauer_drift(
    zone_id: str | None = None,
    fenster: str = "30d",
    jetzt: datetime | None = None,
):
    """T-0065: MAE-Vergleich Heuristik vs. Response-Modell pro Zone.

    T-0225: `jetzt` (optional, ISO-8601) ist der Anker fuer das
    `fenster`-Rueckblick-Fenster — Default `datetime.now()`. Der
    Produktiv-Aufruf laesst den Parameter weg; gesetzt wird er nur
    von deterministischen Tests, die mit festem Datum arbeiten
    (sonst faellt der Test-Datensatz mit der Zeit aus dem Fenster).

    Rueckgabe:
    ```
    {
      "fenster_tage": 30,
      "zonen": {
        "waldblumenhain": {
          "mae_heuristik": 25.5,   # MAE (absolut) der Heuristik-Dauer→Delta
          "mae_ml": 45.0,          # MAE des Inverse-Modells (falls evaluiert)
          "n_bewertet": 12,        # insgesamt bewertete Zeilen
          "n_ml_bewertet": 5,      # davon mit ML-Empfehlung
          "ampel": "gruen"        # rot, wenn mae_ml > 0.8 * mae_heuristik
        },
        ...
      }
    }
    ```
    Zonen ohne bewertete Zeilen erscheinen nicht.
    """
    if _speicher is None:
        return {"fehler": "Speicher nicht initialisiert"}

    tage = 30
    if fenster and fenster.endswith("d"):
        try:
            tage = max(1, int(fenster[:-1]))
        except ValueError:
            pass

    metriken = await _speicher.hole_dauer_drift_metriken(zone_id, tage, jetzt=jetzt)
    zonen_out = {}
    for zid, werte in metriken.items():
        mae_h = werte.get("mae_heuristik")
        mae_m = werte.get("mae_ml")
        ampel = "keine_daten"
        if mae_h is not None and mae_m is not None:
            # Gate-Kriterium gemaess Plan: ML gilt als "gruen", wenn
            # mae_ml <= 0.5 * mae_heuristik; dazwischen gelb; ueber 0.8 * mae_h
            # ist ML schlechter als 20 % Verbesserung — rot.
            if mae_m <= 0.5 * mae_h:
                ampel = "gruen"
            elif mae_m <= 0.8 * mae_h:
                ampel = "gelb"
            else:
                ampel = "rot"
        elif mae_h is not None:
            ampel = "nur_heuristik"
        zonen_out[zid] = {
            "mae_heuristik": round(mae_h, 2) if mae_h is not None else None,
            "mae_ml": round(mae_m, 2) if mae_m is not None else None,
            "n_bewertet": int(werte.get("n_bewertet") or 0),
            "n_ml_bewertet": int(werte.get("n_ml_bewertet") or 0),
            "ampel": ampel,
        }
    return {
        "zone_id": zone_id,
        "fenster_tage": tage,
        "zonen": zonen_out,
    }


@app.get("/api/empfehlungs-audit")
async def empfehlungs_audit(
    zone_id: str | None = Query(default=None),
    tage: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=500, ge=1, le=5000),
):
    """T-0122: Empfehlungs-Audit-Log.

    Liefert die letzten Snapshot-Eintraege der kausalen Gieß-Empfehlung
    pro Zone, plus die nach 6/24h evaluierten Sensor-Werte. Damit kann
    das Frontend (oder ein manueller Curl-Audit) auswerten:
    - Wie oft hat 'akut' tatsaechlich zu kritischer Feuchte gefuehrt?
    - Wie genau war die Reserve-Tage-Aussage?
    - Welche prognose_quelle (ml vs heuristik) war im jeweiligen Slot aktiv?

    Plus aggregierte Statistik (`stats`):
    - n: Gesamtzahl Eintraege im Fenster
    - n_evaluiert: davon mit ist-Werten
    - mae_6h / mae_24h: mittlere Abweichung |prog - ist| in pp
    - typ_verteilung: Anzahl pro empfehlungs_typ
    """
    if _speicher is None:
        return {"fehler": "Speicher nicht initialisiert"}

    eintraege = await _speicher.hole_empfehlungs_audit(
        zone_id=zone_id, tage=tage, limit=limit,
    )

    # T-0236-Bug-Fix (25.05.): pro-Zone-Aggregat per SQL ueber den
    # ganzen `tage`-Zeitraum, unabhaengig vom Eintrags-`limit`.
    # Vorher hat das Frontend die pro-Zone-MAE aus den paginierten
    # `eintraege` selbst gerechnet -- bei 14 Zonen reichte das 500er-
    # Limit nur fuer ~1.5 Tage und ignorierte den Fenster-Toggle.
    # Folge: bambuswald_yogaraum 30d-MAE 8.6 pp wurde als "Reif"
    # gelabelt, weil der Frontend-Filter nur die juengsten ~28
    # Eintraege sah (Mini-MAE 4.3 pp).
    pro_zone_stats = await _speicher.hole_empfehlungs_audit_stats_pro_zone(
        tage=tage,
    )
    # Bei zone_id-Filter nur die eine Zone im pro_zone_stats lassen.
    if zone_id is not None:
        pro_zone_stats = {
            zid: s for zid, s in pro_zone_stats.items() if zid == zone_id
        }

    # Gesamt-Stats: Aggregat ueber pro_zone_stats (gleicher Limit-Bug
    # bei eintraege-basierter Berechnung). Gewichteter MAE-Mittelwert
    # ueber die pro-Zone-Stichproben.
    n_total_alle = sum(s["n"] for s in pro_zone_stats.values())
    n_eval_alle = sum(s["n_evaluiert"] for s in pro_zone_stats.values())

    def _gewichtetes_mae(feld: str) -> float | None:
        zaehler, nenner = 0.0, 0
        for s in pro_zone_stats.values():
            if s.get(feld) is not None and s["n_evaluiert"] > 0:
                zaehler += s[feld] * s["n_evaluiert"]
                nenner += s["n_evaluiert"]
        return round(zaehler / nenner, 2) if nenner > 0 else None

    typ_verteilung_alle: dict[str, int] = {}
    for s in pro_zone_stats.values():
        for typ, n in s.get("typ_verteilung", {}).items():
            typ_verteilung_alle[typ] = typ_verteilung_alle.get(typ, 0) + n

    return {
        "zone_id": zone_id,
        "fenster_tage": tage,
        "stats": {
            "n": n_total_alle,
            "n_evaluiert": n_eval_alle,
            "mae_6h_pp": _gewichtetes_mae("mae_6h_pp"),
            "mae_24h_pp": _gewichtetes_mae("mae_24h_pp"),
            "typ_verteilung": typ_verteilung_alle,
        },
        "pro_zone_stats": pro_zone_stats,
        "eintraege": eintraege,
    }


# --- Dashboard (statische Dateien) ---

_static_dir = Path(__file__).resolve().parent.parent.parent / "static"
# T-0557: einmal aufgeloest fuer die Containment-Pruefung in `spa_fallback`.
# `_static_dir` selbst ist nur bis zum vorletzten Segment aufgeloest (das
# `resolve()` oben gilt `__file__`), ein Symlink auf `static/` wuerde den
# Praefix-Vergleich sonst immer scheitern lassen.
_static_dir_aufgeloest = _static_dir.resolve()

if _static_dir.is_dir():
    # SPA-Fallback: Alle nicht-API-Routen bekommen index.html
    @app.get("/{pfad:path}")
    async def spa_fallback(pfad: str):
        # T-0472: der Kommentar darueber hat immer schon "nicht-API"
        # behauptet, die Route aber `/{pfad:path}` registriert -- die faengt
        # `/api/...` mit. Ein Endpoint, der umbenannt wurde oder im
        # laufenden Prozess fehlt, antwortete damit **200 + text/html**
        # statt 404. `authFetch` (frontend/src/api.ts) prueft `r.ok`, das
        # ist dann `true`, und der Aufruf stirbt in `r.json()` mit einem
        # Parse-Fehler statt mit einer lesbaren 404. Am 05.08. live
        # gegengeprueft: `/api/gibt-es-nicht` -> 200 text/html, waehrend
        # `/api/zonen` korrekt 401 + JSON liefert.
        if pfad == "api" or pfad.startswith("api/"):
            raise HTTPException(status_code=404, detail="Unbekannter API-Pfad")
        # T-0557: Containment-Pruefung. Diese Route ist in ROUTE_ROLLEN als
        # `public` gefuehrt (das Dashboard muss ohne Key laden), und uvicorn
        # `unquote`t den Rohpfad -- prozentkodierte Punkte erreichen den
        # Handler also als echte Dot-Segmente, die kein Browser und kein
        # Proxy vorher wegnormalisiert. Ohne die Pruefung lieferte
        # `/%2e%2e/%2e%2e/config/default.yaml` die Konfiguration aus und
        # `/%2fetc%2fhosts` eine beliebige absolute Datei (pathlib ersetzt
        # bei absolutem rechten Operanden den linken). Der Auth-Layer hebelte
        # sich damit selbst aus: `frontend/.env.local` traegt den
        # control-Key im Klartext.
        #
        # `resolve()` auf BEIDEN Seiten, sonst schlaegt der Vergleich fehl,
        # sobald `static/` selbst ein Symlink ist. Ausserhalb -> wie ein
        # unbekannter Pfad behandeln (index.html), nicht 403: ein 403
        # verriete, dass die Datei existiert.
        datei = (_static_dir / pfad).resolve()
        index = _static_dir_aufgeloest / "index.html"
        if not datei.is_relative_to(_static_dir_aufgeloest):
            return FileResponse(index)
        if datei.is_file():
            return FileResponse(datei)
        return FileResponse(index)
else:
    @app.get("/")
    async def kein_dashboard():
        return {"hinweis": "Dashboard nicht gebaut. Fuehre 'npm run build' im frontend/-Ordner aus."}
