"""SQLite-Zeitreihenspeicher fuer Sensor-, Ventil- und Entscheidungsdaten.

Nutzt aiosqlite fuer async Zugriff. Die Datenbank ist eine einzelne Datei —
kein Server, kein Docker. Schema wird beim Start automatisch erstellt.
"""

import asyncio
import json
import os
import random
import re
import sqlite3
from typing import Awaitable, Callable

import aiosqlite
import structlog
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, Iterable

logger = structlog.get_logger()

from bewaesserung.modelle import (
    Ausloser,
    BlockerTyp,
    BewaesserungsEntscheidung,
    DatenQuelle,
    EntscheidungsScope,
    SensorMessung,
    SensorWarnung,
    SensorWarnungTyp,
    VentilAktion,
    VentilEreignis,
    WetterEreignis,
    WetterEreignisTyp,
    WetterStunde,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sensor_messung (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zeitstempel TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    geraet_id TEXT NOT NULL DEFAULT '',
    boden_feuchte REAL,
    boden_temperatur REAL,
    umgebungs_temperatur REAL,
    licht_intensitaet REAL,
    batterie_prozent REAL,
    boden_fruchtbarkeit REAL,
    licht REAL,
    quelle TEXT NOT NULL DEFAULT 'gardena'
);

CREATE TABLE IF NOT EXISTS ventil_ereignis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zeitstempel TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    ventil_id TEXT NOT NULL,
    aktion TEXT NOT NULL,
    dauer_sekunden INTEGER DEFAULT 0,
    ausloser TEXT NOT NULL,
    liter REAL
);

CREATE TABLE IF NOT EXISTS wetter_vorhersage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    abfrage_zeitstempel TEXT NOT NULL,
    vorhersage_zeitstempel TEXT NOT NULL,
    temperatur REAL,
    niederschlag_mm REAL DEFAULT 0,
    niederschlag_wahrscheinlichkeit REAL DEFAULT 0,
    wind_kmh REAL DEFAULT 0,
    wind_richtung_grad REAL DEFAULT 0,
    et0_mm REAL DEFAULT 0,
    standort_id TEXT NOT NULL DEFAULT 'standard',
    luftfeuchte REAL                     -- T-0045: relative Feuchte in %, nullable (Bestandsdaten)
);

CREATE TABLE IF NOT EXISTS entscheidung_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zeitstempel TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    soll_bewaessern INTEGER NOT NULL,
    dauer_sekunden INTEGER DEFAULT 0,
    begruendung TEXT,
    blocker_typ TEXT,
    scope TEXT NOT NULL DEFAULT 'zone',
    scope_ref TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS wetter_ereignis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zeitstempel TEXT NOT NULL,
    typ TEXT NOT NULL,
    standort_id TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '',
    beginn TEXT,
    ende TEXT
);

CREATE TABLE IF NOT EXISTS geraete_zuordnung (
    geraet_id TEXT PRIMARY KEY,
    zone_id TEXT NOT NULL,
    geraet_name TEXT,
    quelle TEXT NOT NULL DEFAULT 'discovery',
    aktualisiert TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sensor_warnung (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zeitstempel TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    typ TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    behoben_um TEXT
);

-- Historische Wetter-Ground-Truth (Open-Meteo Archive).
-- Kein Mutieren von wetter_vorhersage — separate Tabelle fuer Real-Werte.
-- Primary Key (zeitstempel, standort_id) erlaubt saubere Upserts bei
-- ueberlappenden Backfill-Fenstern.
CREATE TABLE IF NOT EXISTS wetter_archiv (
    zeitstempel TEXT NOT NULL,
    standort_id TEXT NOT NULL,
    niederschlag_mm REAL DEFAULT 0,
    temperatur REAL,
    et0_mm REAL DEFAULT 0,
    abgerufen_am TEXT NOT NULL,
    PRIMARY KEY (zeitstempel, standort_id)
);

-- T-0422: Auszehrung der Wurzelzone (FAO-56 `Dr`) pro Zone, in mm.
-- Pro Zone EIN aktueller Stand plus Historie fuer die Shadow-Auswertung.
-- `Dr` ist ein FORTGESCHRIEBENER Zustand, kein Messwert: er haengt vom
-- Startpunkt und jedem Zwischenschritt ab. Deshalb persistiert -- nach
-- einem Restart waere er sonst weg und muesste geraten werden.
-- `quelle_regen`/`quelle_et0` mitschreiben, damit spaeter unterscheidbar
-- ist, ob ein Schritt auf Archiv-Ground-Truth oder auf Forecast beruhte.
CREATE TABLE IF NOT EXISTS wasserbilanz_zustand (
    zone_id TEXT NOT NULL,
    zeitstempel TEXT NOT NULL,
    dr_mm REAL NOT NULL,
    taw_mm REAL NOT NULL,
    raw_mm REAL NOT NULL,
    et0_mm REAL DEFAULT 0,
    regen_mm REAL DEFAULT 0,
    bewaesserung_mm REAL DEFAULT 0,
    wuerde_giessen INTEGER DEFAULT 0,
    ist_entscheidung INTEGER,
    quelle TEXT DEFAULT '',
    PRIMARY KEY (zone_id, zeitstempel)
);

-- T-0416 Stufe 2: automatisch gesetzte Ausschluss-Fenster.
-- Bewusst eine EIGENE Tabelle, nicht `config/default.yaml`: die YAML ist
-- handgepflegt und kommentiert, ein Job der sie umschreibt zerstoert
-- Kommentare und kaempft mit den Edits des Users.
-- Beim Start werden diese Fenster in `konfig.ml_ausschluss_fenster`
-- gemergt -- damit erreichen sie ALLE sieben Konsumenten, die diese Liste
-- lesen (regime_klassifikation, k_basis_fit_job, skalen_mapping_fit_job,
-- kalibrierung, schwellen_vorschlag, api_server, features), ohne dass eine
-- einzige Call-Site angefasst werden muss.
-- `geraet_id` ist Pflicht: ein FYTA-Push betrifft die FYTA-Sensoren, nicht
-- den Gardena in derselben Zone -- der ist bei hecke/waldblumenhain sogar
-- der Aggregat-Lead (T-0421) und muss gueltig bleiben.
CREATE TABLE IF NOT EXISTS auto_ausschluss_fenster (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL,
    geraet_id TEXT NOT NULL,
    von TEXT NOT NULL,
    bis TEXT NOT NULL,
    grund TEXT NOT NULL DEFAULT '',
    quelle TEXT NOT NULL DEFAULT 'fyta_sprung_detektor',
    angelegt_am TEXT NOT NULL,
    UNIQUE (zone_id, geraet_id, von)
);

-- T-0423: Regen-Ensemble (icon_d2_eps, 20 Member). Pro Abfrage EIN Eintrag
-- je Standort/Horizont. Der Sinn ist die SPREAD-Historie: ohne sie laesst
-- sich in T-0425 nichts kalibrieren -- man kann hinterher nicht mehr
-- rekonstruieren, ob ein Fehlgriff aus einem einigen oder einem uneinigen
-- Ensemble kam. Deshalb p10/p50/p90 + Member-Zahl mitschreiben, nicht nur
-- den p20, mit dem gerechnet wird.
-- `horizont_stunden` ist Teil des Schluessels, weil dieselbe Abfrage ueber
-- 24 h und 48 h voellig verschiedene Verteilungen liefert (22.07.: p20 0,0
-- gegen 1,2) -- ein Eintrag ohne Horizont waere nicht interpretierbar.
CREATE TABLE IF NOT EXISTS regen_ensemble (
    abfrage_zeitstempel TEXT NOT NULL,
    standort_id TEXT NOT NULL,
    horizont_stunden INTEGER NOT NULL,
    p10 REAL, p20 REAL, p50 REAL, p90 REAL,
    minimum REAL, maximum REAL,
    n_member INTEGER,
    wahrsch_ueber_1mm REAL,
    deterministisch_mm REAL,
    PRIMARY KEY (abfrage_zeitstempel, standort_id, horizont_stunden)
);

-- T-0050b: Automatischer Cache fuer Pflanzen-Optimum aus FYTA-API.
-- Pro Zone ein Eintrag (UPSERT), tagesaktuelle Werte aus
-- `/user-plant/{id}.plant.measurements.moisture.values`.
-- `zone.optimum_feuchte_*` (Config-Override) hat Vorrang, dieser
-- Cache ist Fallback fuer nicht-konfigurierte Zonen.
CREATE TABLE IF NOT EXISTS plant_optimum (
    zone_id TEXT PRIMARY KEY,
    feuchte_min REAL NOT NULL,
    feuchte_max REAL NOT NULL,
    feuchte_min_akzeptabel REAL,
    feuchte_max_akzeptabel REAL,
    aktualisiert TEXT NOT NULL,
    quelle TEXT NOT NULL DEFAULT 'fyta'
);

-- T-0196: Multi-Achsen-Optimum aus FYTA Plant-Detail-API (`/user-plant/{id}`).
-- EAV-Pattern: eine Zeile pro (zone_id, achse), weil FYTA mehrere
-- Einheiten + mehrere Typen pro Achse liefert (Licht hat PPFD + DLI).
-- Bestehende plant_optimum-Tabelle bleibt fuer moisture-Kompat erhalten.
-- Achsen-Identifier (deutsch, intern): feuchte, licht_ppfd, licht_dli,
-- temperatur, salinitaet. einheit als string (μmol/h, mol/day, °C/h,
-- mS/cm/h, %/h). FYTA-Werte sind defensiv float-konvertiert.
-- T-0196d/e: `current` ist der zuletzt beobachtete Wert dieser Achse —
-- FYTA `values.current` bei feuchte/licht_ppfd/temperatur/salinitaet,
-- Backend-Aggregat (Tagessumme PPFD -> mol/day) bei licht_dli.
CREATE TABLE IF NOT EXISTS plant_optimum_achse (
    zone_id TEXT NOT NULL,
    achse TEXT NOT NULL,
    min_good REAL,
    max_good REAL,
    min_akzeptabel REAL,
    max_akzeptabel REAL,
    current REAL,
    einheit TEXT NOT NULL,
    aktualisiert TEXT NOT NULL,
    quelle TEXT NOT NULL DEFAULT 'fyta',
    PRIMARY KEY (zone_id, achse)
);

-- T-0063: Automatische Kalibrier-Messungen der Gardena-Sensor-Skala
-- gegen empirische Referenzpunkte.
--   typ='feldkapazitaet': Sensor-Plateau 12-24h nach Regen > basis_mm.
--   typ='welkepunkt_proxy': Sensor-Minimum waehrend >basis_mm-Tagen
--       ohne Regen (Saison Mai-September).
-- `basis_mm`: Regen-Summe oder Trocken-Tage, die zum Kandidaten fuehrten.
CREATE TABLE IF NOT EXISTS feldkapazitaet_messung (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zeitstempel TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    typ TEXT NOT NULL,
    wert REAL NOT NULL,
    basis_mm REAL,
    notizen TEXT
);
CREATE INDEX IF NOT EXISTS idx_feldkap_zone_typ_zeit
    ON feldkapazitaet_messung(zone_id, typ, zeitstempel DESC);

-- T-0115: Persistenter State laufender Live-Manuell-Bewaesserungen.
-- Nach Backend-Restart kann VentilSicherung den `_aktiv`-Zustand pro
-- Kanal rekonstruieren — Watchdog-Timer wird neu aufgesetzt, Banner
-- zeigt wieder Backend-Quelle + Countdown statt extern.
-- Eine Zeile pro aktivem Kanal; UPSERT bei Start, DELETE bei
-- erfolgreichem Stop.
CREATE TABLE IF NOT EXISTS live_lauf_state (
    geraet_id TEXT NOT NULL,
    kanal INTEGER NOT NULL,
    valve_id TEXT,
    zone_ids_json TEXT NOT NULL,
    dauer_sekunden INTEGER NOT NULL,
    ausloser TEXT NOT NULL,
    gestartet_am TEXT NOT NULL,
    PRIMARY KEY (geraet_id, kanal)
);

-- T-0122: Empfehlungs-Audit-Log. Bei jedem Snapshot-Tick wird die kausale
-- Empfehlung pro Zone aufgezeichnet (1×/h pro Zone). Nach 6/24 h wird
-- der reale Sensor-Wert eingetragen (`ist_feuchte_*`-Spalten), damit
-- spaeter ausgewertet werden kann, ob die Klassifikation (akut/praeventiv/
-- kein_bedarf) und die Reserve-Tage realistisch waren. Der Audit-Pfad ist
-- read-only fuer das Backend selbst (keine Auto-Loop-Wirkung) — Datengrund-
-- lage fuer die Vertrauens-Aufbau-Phase vor T-0021 Scharfschalten.
CREATE TABLE IF NOT EXISTS empfehlungs_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zeitstempel TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    empfehlungs_typ TEXT NOT NULL,            -- akut|praeventiv|wohlfuehl_grenze|kein_bedarf
    soll_bewaessern INTEGER NOT NULL,
    blocker_typ TEXT,                          -- FEUCHTE_OK|REGEN_ERWARTET|...
    feuchte_aktuell REAL,
    welkepunkt_wert REAL,
    optimum_min REAL,
    optimum_max REAL,
    prognose_quelle TEXT,                      -- ml|heuristik|keine
    prognose_6h REAL,
    prognose_12h REAL,
    prognose_24h REAL,
    tage_bis_welkepunkt REAL,
    dauer_s_empfehlung INTEGER,
    aktive_strategie TEXT,
    -- T-0270 (28.05.): Hybrid Stufe 1 Physik-Diagnose-Werte zum
    -- Zeitpunkt der Empfehlung. Read-only neben prognose_*h.
    prognose_physik_6h REAL,
    prognose_physik_12h REAL,
    prognose_physik_24h REAL,
    physik_quelle TEXT,                        -- konfig|gefittet|default_tau|keine
    k_basis_pro_h REAL,
    -- Audit-Felder (werden beim Eval-Tick ausgefuellt)
    ist_feuchte_6h REAL,
    ist_feuchte_24h REAL,
    abweichung_6h REAL,                        -- prognose_6h - ist_feuchte_6h
    abweichung_24h REAL,                       -- prognose_24h - ist_feuchte_24h
    -- T-0270: zusaetzliche Physik-Abweichung fuer Bias-Audit.
    abweichung_physik_6h REAL,
    abweichung_physik_24h REAL,
    evaluiert_am TEXT
);
CREATE INDEX IF NOT EXISTS idx_empfehlungs_audit_zone_zeit
    ON empfehlungs_audit(zone_id, zeitstempel DESC);
CREATE INDEX IF NOT EXISTS idx_empfehlungs_audit_offen
    ON empfehlungs_audit(evaluiert_am, zeitstempel)
    WHERE evaluiert_am IS NULL;

-- T-0116: Persistenter State laufender Pre-Soak-Sequenzen.
-- Nach Backend-Restart kann PreSoakManager die Sequenz fortsetzen —
-- besonders kritisch in Pause/Haupt-Phase, weil sonst die Hauptdose
-- ausfaellt. Eine Zeile pro Zone, UPSERT bei Start + nach Phasen-
-- Uebergang, DELETE bei fertig/fehler.
CREATE TABLE IF NOT EXISTS pre_soak_state (
    zone_id TEXT PRIMARY KEY,
    kanal INTEGER NOT NULL,
    zone_ids_kanal_json TEXT NOT NULL,
    pre_soak_s INTEGER NOT NULL,
    pause_s INTEGER NOT NULL,
    haupt_s INTEGER NOT NULL,
    gestartet_am TEXT NOT NULL,
    phase TEXT NOT NULL
);

-- T-0126 (H-2): Watchdog-Throttle. Pro Trigger-Klasse + Zone wird der
-- letzte Push-Zeitstempel persistiert, damit ein Restart nicht erneut
-- alarmiert. zone_id='_global' fuer flotten-weite Trigger (Husqvarna-Block).
CREATE TABLE IF NOT EXISTS watchdog_event (
    typ TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    zuletzt_gesendet TEXT NOT NULL,
    PRIMARY KEY (typ, zone_id)
);

-- T-0132 (H-8): Endpoint-Health-Status fuer inoffizielle Schnittstellen
-- (Gardena DHS + FYTA). EndpointHealthJob schreibt 1x/Tag pro Endpoint
-- den letzten Status; Watchdog liest und triggert Push bei laenger
-- anhaltendem Fehler. Status: 'ok' / 'schema_fehler' / 'auth_fehler' /
-- 'connect_fehler' / 'unbekannt'.
CREATE TABLE IF NOT EXISTS endpoint_health (
    endpoint TEXT PRIMARY KEY,
    letzte_pruefung TEXT NOT NULL,
    letzter_erfolg TEXT,
    status TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT ''
);

-- T-0228 Stufe 2: Wartungs-Fenster pro Zone. UI-startbar
-- (Sensor-Reset, Wiedereinsetzen, Fremdnutzung). Aktive Fenster
-- pausieren Heuristik (sensor_backfill), Leck-Detektor, ML-Training-
-- Filter und Schwellen-Vorschlag -- analog zu `ml_ausschluss_fenster`
-- aus der YAML, aber zur Laufzeit per UI/API.
--
-- `bis_am=NULL` = aktuell laufendes Fenster, Konsumenten behandeln
-- es als "bis jetzt + 1 Tag" (Sicherheits-Cap, damit ein vergessenes
-- "Beenden" nicht ewig pausiert). Beim Beenden wird `bis_am` auf
-- den realen Stop-Zeitpunkt gesetzt.
CREATE TABLE IF NOT EXISTS wartungs_fenster (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL,
    von_am TEXT NOT NULL,
    bis_am TEXT,                       -- NULL = laeuft noch
    grund TEXT NOT NULL DEFAULT '',
    angelegt_am TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wartungs_fenster_offen
    ON wartungs_fenster(zone_id, von_am)
    WHERE bis_am IS NULL;

-- T-0228 Stufe 1: Pflege-/Wartungs-Erinnerungen. Ersetzt
-- `arbeitspattern_conditional_trigger_calendar.md`-Pattern (heute
-- Claude-Anweisung "scanne TASK.md gegen heute") durch persistierten
-- System-State. Beispiele: zitrus-Stau-Nachschau am 06.06., T-0179a
-- Plateau-Re-Fit Mitte Juni, T-0163/T-0159 ab Mai 2027.
--
-- `intervall_tage`: bei wiederkehrenden Erinnerungen (z. B. FYTA-
-- Batterie-Check alle 90 Tage) -- nach `erledige_pflege_erinnerung`
-- wird automatisch ein Folge-Eintrag mit `faellig_am + intervall_tage`
-- angelegt. NULL = einmalig.
-- `quelle`: 'manuell' (User-Aktion) / 'memory' (aus
-- `*_conditional_trigger_*`-Memory-Files seed-importiert) /
-- 'wirkungsrate_cleanup' (T-0247-Scope, vorerst Stub).
CREATE TABLE IF NOT EXISTS pflege_erinnerung (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT,                       -- NULL = global / mehrere Zonen
    typ TEXT NOT NULL,                  -- z. B. 'kalibrierung', 'beobachtung'
    faellig_am TEXT NOT NULL,           -- ISO-Datum
    intervall_tage INTEGER,             -- NULL = einmalig
    beschreibung TEXT NOT NULL DEFAULT '',
    quelle TEXT NOT NULL DEFAULT 'manuell',
    angelegt_am TEXT NOT NULL,
    erledigt_am TEXT                    -- NULL = noch offen
);
CREATE INDEX IF NOT EXISTS idx_pflege_erinnerung_faellig
    ON pflege_erinnerung(faellig_am)
    WHERE erledigt_am IS NULL;
CREATE INDEX IF NOT EXISTS idx_pflege_erinnerung_zone
    ON pflege_erinnerung(zone_id, faellig_am)
    WHERE erledigt_am IS NULL;

CREATE INDEX IF NOT EXISTS idx_sensor_zone_zeit
    ON sensor_messung(zone_id, zeitstempel);
CREATE INDEX IF NOT EXISTS idx_ventil_zone_zeit
    ON ventil_ereignis(zone_id, zeitstempel);
CREATE INDEX IF NOT EXISTS idx_entscheidung_zone_zeit
    ON entscheidung_log(zone_id, zeitstempel);
CREATE INDEX IF NOT EXISTS idx_wetter_ereignis_typ_zeit
    ON wetter_ereignis(typ, zeitstempel);
CREATE INDEX IF NOT EXISTS idx_sensor_warnung_zone_zeit
    ON sensor_warnung(zone_id, zeitstempel);
CREATE INDEX IF NOT EXISTS idx_sensor_warnung_offen
    ON sensor_warnung(zone_id, typ, behoben_um);
CREATE INDEX IF NOT EXISTS idx_wetter_archiv_standort_zeit
    ON wetter_archiv(standort_id, zeitstempel);

-- T-0047 ML-Drift-Log: jede Live-Inferenz landet hier; der Drift-Job
-- fuellt ist_feuchte/abweichung nach, sobald eine Sensor-Messung zum
-- prognose_ziel_zeitstempel verfuegbar ist.
CREATE TABLE IF NOT EXISTS ml_vorhersage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zeitstempel TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    horizont_h INTEGER NOT NULL,
    prognose_ziel_zeit TEXT NOT NULL,
    prognose_feuchte REAL NOT NULL,
    modell_version TEXT NOT NULL,
    ist_feuchte REAL,
    abweichung REAL,
    evaluiert_am TEXT,
    feature_zeitstempel TEXT,
    prognose_q10 REAL,                    -- T-0046 Quantile q10
    prognose_q90 REAL                     -- T-0046 Quantile q90
);
CREATE INDEX IF NOT EXISTS idx_ml_log_zone_zeit
    ON ml_vorhersage_log(zone_id, zeitstempel);
CREATE INDEX IF NOT EXISTS idx_ml_log_unevaluiert
    ON ml_vorhersage_log(evaluiert_am) WHERE evaluiert_am IS NULL;

-- T-0065 Response-Modell: pro produktiver Entscheidung wird die heuristische
-- Dauer + (falls aktiv) die ML-Empfehlung + die Feature-Snapshot persistiert.
-- 6h nach zeitstempel fuellt der drift_job aus den Sensor-Messungen
-- `ist_delta_6h` und berechnet die Fehler beider Modelle — das ist die
-- Basis fuer den MAE-Shadow-Report unter /api/ml/dauer-drift.
-- `modus`: 'shadow' (aktiv=true, wirksam=false) oder 'wirksam' (ML-Dauer
--          wirklich zurueckgegeben von _berechne_dauer).
CREATE TABLE IF NOT EXISTS ml_dauer_vorschlag (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zeitstempel TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    f_vor REAL NOT NULL,
    ziel_schwelle REAL NOT NULL,
    heuristik_s INTEGER NOT NULL,
    ml_s INTEGER,
    ml_modell_version TEXT,
    features_json TEXT NOT NULL,
    modus TEXT NOT NULL CHECK(modus IN ('shadow','wirksam')),
    bewertet_am TEXT,
    ist_delta_6h REAL,
    heuristik_prognose_delta REAL,
    ml_prognose_delta REAL,
    heuristik_fehler REAL,
    ml_fehler REAL
);
CREATE INDEX IF NOT EXISTS idx_dauer_vorschlag_zone_zeit
    ON ml_dauer_vorschlag(zone_id, zeitstempel);
CREATE INDEX IF NOT EXISTS idx_dauer_vorschlag_unbewertet
    ON ml_dauer_vorschlag(bewertet_am) WHERE bewertet_am IS NULL;

-- T-0181 (Stub, Trigger: 4-6 Wochen nach T-0179-Inbetriebnahme):
-- Lineares Skalen-Mapping pro (zone_id, quelle), das vor dem Median-
-- Aggregat angewendet wird. Heute KEIN Fit-Code -- die Tabelle bleibt
-- leer, `letzte_messung_aggregiert` verhaelt sich identisch (Identity).
-- Sobald parallele Messungen vorliegen, schreibt ein separater Job
-- (T-0181-Folge) die Koeffizienten und der Aggregat-Pfad nutzt sie
-- transparent.
--   feuchte_mapped = a * feuchte_raw + b
--   a = 1.0, b = 0.0 entspricht Identity (Default wenn Zeile fehlt).
-- Primary Key (zone_id, quelle) erlaubt Upsert pro Sensor-Quelle.
CREATE TABLE IF NOT EXISTS sensor_skalen_mapping (
    zone_id TEXT NOT NULL,
    quelle TEXT NOT NULL,
    a REAL NOT NULL DEFAULT 1.0,
    b REAL NOT NULL DEFAULT 0.0,
    n_obs INTEGER NOT NULL DEFAULT 0,
    gefittet_am TEXT,
    PRIMARY KEY (zone_id, quelle)
);

-- Hybrid Stufe 1: pro Zone die gefittete Trocknungs-Konstante des
-- Physik-Moduls (`physik_trocknung.py`). EINE Zeile pro Zone (Upsert);
-- bewusst NICHT `feldkapazitaet_messung` (das ist append-only mit
-- Stunden-Bucket-Dedup, hier wollen wir den aktuellen besten Wert).
-- `k_basis` ist 1/h, `et0_basis_mm_pro_h` ist die mittlere ET0 der
-- Fit-Phasen (zur ET0-Skalierung in der Live-Prognose). `mae` ist die
-- mittlere absolute Vorhersage-Abweichung in pp ueber die Fit-Phasen.
CREATE TABLE IF NOT EXISTS physik_k_basis (
    zone_id TEXT PRIMARY KEY,
    k_basis REAL NOT NULL,
    et0_basis_mm_pro_h REAL NOT NULL,
    n_phasen INTEGER NOT NULL DEFAULT 0,
    mae REAL,
    gefittet_am TEXT
);

CREATE TABLE IF NOT EXISTS wirkung_fit (
    zone_id TEXT PRIMARY KEY,
    wmax REAL NOT NULL,
    r0 REAL NOT NULL,
    tau REAL NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    r2 REAL NOT NULL DEFAULT 0,
    mse REAL NOT NULL DEFAULT 0,
    angenommen INTEGER NOT NULL DEFAULT 0,
    grund TEXT,
    gefittet_am TEXT
);
"""


class Speicher:
    """Async SQLite-Speicher fuer alle Zeitreihendaten."""

    def __init__(self, db_pfad: str):
        self._db_pfad = db_pfad
        self._db: aiosqlite.Connection | None = None
        # T-0332: Aggregat-Lead pro Zone (zone_id -> geraet_id). Wenn gesetzt,
        # nutzt `letzte_messung_aggregiert(_bulk)` NUR diesen Sensor statt des
        # Median. Wird beim Startup aus der Konfig gesetzt (setze_aggregat_lead).
        self._aggregat_lead: dict[str, str] = {}
        # Wenn aktiv: Schreib-Methoden verzichten auf `commit()`, sodass
        # Aufrufer mehrere Inserts/Deletes atomar zusammenfassen koennen
        # (z. B. DHS-Replace: DELETE Heuristik + INSERT OEFFNEN/SCHLIESSEN).
        self._in_transaktion = False
        # T-0321: Schreib-Serialisierung. Eine geteilte aiosqlite-Connection +
        # globales `_in_transaktion`-Flag erlaubte sonst, dass ein nebenlaeufiger
        # Einzel-Write (z.B. Ventil-Close-Callback) in den offenen
        # `transaktion()`-Block eines Hintergrund-Jobs absorbiert und bei dessen
        # Rollback verloren geht. Der Write-Lock macht jeden Write/jede Tx atomar
        # gegeneinander. Task-reentrant via `_write_owner` (kein Deadlock bei
        # Same-Task-Verschachtelung). Lazy pro Event-Loop (Tests: asyncio.run je
        # Call = neuer Loop).
        self._write_lock: "asyncio.Lock | None" = None
        self._write_lock_loop: object | None = None
        self._write_owner: object | None = None
        # T-0171: True sobald `schliessen()` lief. Der `shutdown_race_guard`-
        # Middleware in `api_server.py` nutzt das Flag, um einen Inflight-
        # Request, der waehrend des Ctrl-C-Shutdowns noch einen DB-Read
        # startet, leise mit 503 zu beantworten statt mit einem
        # ProgrammingError-Stacktrace.
        self._geschlossen = False

    def _hole_write_lock(self) -> "asyncio.Lock":
        """T-0321: Schreib-Lock lazy pro Event-Loop holen. In Produktion einmalig
        (ein Loop); in Tests (asyncio.run je Call) pro Loop neu, sonst wuerde ein
        Loop-gebundener Lock ueber Loop-Grenzen brechen."""
        loop = asyncio.get_running_loop()
        if self._write_lock is None or self._write_lock_loop is not loop:
            self._write_lock = asyncio.Lock()
            self._write_lock_loop = loop
            self._write_owner = None
        return self._write_lock

    @asynccontextmanager
    async def transaktion(self) -> "AsyncIterator[None]":
        """Gruppiert mehrere Schreib-Aufrufe in eine atomare Transaktion.

        Per Default committen die Methoden nach jedem Statement. Innerhalb
        dieses Blocks werden commits unterdrueckt — am Ende wird einmal
        committed; bei Exception rollback und re-raise. Re-entrant: bei
        verschachtelten Aufrufen managed der aeussere Block.

        T-0321: Haelt fuer die gesamte Dauer den Write-Lock -> nebenlaeufige
        Einzel-Writes (anderer Task) koennen NICHT in diese Tx absorbiert werden.
        """
        assert self._db is not None
        cur = asyncio.current_task()
        if self._write_owner is cur:
            # Re-entrant: dieser Task haelt den Write-Lock bereits (verschachtelte
            # transaktion oder Tx innerhalb eines Writes) -> nur yield, der
            # aeussere Block committet/rollt zurueck.
            yield
            return
        lock = self._hole_write_lock()
        await lock.acquire()
        self._write_owner = cur
        self._in_transaktion = True
        try:
            yield
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
        finally:
            self._in_transaktion = False
            self._write_owner = None
            lock.release()

    # T-0210: BUSY/LOCKED-Retry fuer Schreib-Operationen. Ergaenzt den
    # `busy_timeout=15000`-Pragma; greift erst, wenn die 15 s vom SQLite-
    # Treiber bereits ueberschritten waren. Backoff: 200 ms, 400 ms,
    # 800 ms, 1600 ms (jeweils +/-50 % Jitter). Andere `OperationalError`
    # (Syntax, Schema, IO) werden NICHT gefangen — die muessen
    # weiterhin sofort hochkommen.
    _LOCK_RETRY_MAX_VERSUCHE = 4
    _LOCK_RETRY_BASIS_S = 0.2

    async def _mit_lock_retry(
        self,
        op: Callable[[], Awaitable],
        *,
        label: str,
    ):
        """Fuehrt `op()` aus, retried bei `database is locked/busy`.

        Returnt den Wert von `op()` durch (frueher: `-> None`, d.h. Werte
        wurden verworfen). T-0228 Stufe 1: Pflege-Erinnerungen brauchen
        die neue `lastrowid`, ohne den Pass-Through wuerde der API-Caller
        keine ID zurueckbekommen.

        T-0321: Serialisiert den Write ueber den Write-Lock. Haelt ein Task
        den Lock bereits (innerhalb `transaktion()` oder eines aeusseren
        Writes), laeuft `op` direkt (task-reentrant, kein Deadlock). So kann
        ein nebenlaeufiger Write nicht in eine fremde offene Tx absorbiert
        werden.
        """
        cur = asyncio.current_task()
        if self._write_owner is cur and cur is not None:
            return await self._fuehre_mit_retry_aus(op, label=label)
        lock = self._hole_write_lock()
        await lock.acquire()
        self._write_owner = cur
        try:
            return await self._fuehre_mit_retry_aus(op, label=label)
        finally:
            self._write_owner = None
            lock.release()

    async def _fuehre_mit_retry_aus(
        self,
        op: Callable[[], Awaitable],
        *,
        label: str,
    ):
        """T-0210-Retry-Schleife (ohne Lock-Handling -- das macht der Aufrufer
        `_mit_lock_retry`)."""
        versuch = 0
        while True:
            try:
                return await op()
            except sqlite3.OperationalError as fehler:
                text = str(fehler).lower()
                if "locked" not in text and "busy" not in text:
                    raise
                versuch += 1
                if versuch >= self._LOCK_RETRY_MAX_VERSUCHE:
                    logger.error(
                        "speicher.lock_persistent",
                        stelle=label,
                        versuche=versuch,
                    )
                    raise
                wartezeit = (
                    self._LOCK_RETRY_BASIS_S * (2 ** (versuch - 1))
                ) * (0.5 + random.random())
                logger.warning(
                    "speicher.lock_retry",
                    stelle=label,
                    versuch=versuch,
                    wartezeit_ms=int(wartezeit * 1000),
                )
                await asyncio.sleep(wartezeit)

    async def verbinden(self, modus: str = "rw") -> None:
        """Oeffnet die Datenbank und erstellt das Schema.

        Setzt vor dem Schema-Setup zwei kritische Pragmas (T-0098):
        - `journal_mode=WAL`: erlaubt einen Writer + viele Readers
          gleichzeitig. SQLite-Default `delete` blockt jeden Reader,
          sobald ein Writer aktiv ist — das produzierte die "database
          is locked"-Loops in den Logs (08:34, 17 mal).
        - `busy_timeout=15000` (T-0210): wartet bis zu 15 s auf Lock-
          Freigabe statt sofort `OperationalError` zu werfen. Faengt
          kurze Schreib-Kollisionen sauber ab (z. B. ML-Retrain
          ueberlappt mit Sensor-Tick). Hochgezogen von 5 s, nachdem am
          18.05.2026 (siehe `_entscheidungsloop`-Stack) lange Lese-
          Bursts (60-Tage-Feature-Scan, Backup) parallele Writer in
          eine sichtbare Lock-Loop-Schleife getrieben haben. Zusaetzlich
          haengen die Schreib-Methoden an `_mit_lock_retry`, das
          BUSY/LOCKED-Fehler mit exponentialem Backoff erneut versucht.
        - `synchronous=NORMAL`: sicher fuer WAL (FULL nicht noetig),
          deutlich schneller bei Schreib-Bursts.

        WAL persistiert ueber connect/close hinweg im DB-Header, das
        explizite SETting ist trotzdem idempotent + dokumentierend.

        T-0162: `modus="ro"` oeffnet die DB als Read-Only-Connection
        (SQLite-URI `file:<pfad>?mode=ro`). Schema-Setup + Migration
        werden uebersprungen — das vermeidet "database is locked"-
        Konflikte, wenn die CLI neben einem laufenden Backend mit
        langen Lese-Bursts (60-Tage-Trainings-Daten) arbeitet.
        """
        # T-0171: Reconnect setzt das Shutdown-Flag zurueck.
        self._geschlossen = False
        pfad = Path(self._db_pfad)
        pfad.parent.mkdir(parents=True, exist_ok=True)
        if modus == "ro":
            uri = f"file:{pfad}?mode=ro"
            self._db = await aiosqlite.connect(uri, uri=True)
            self._db.row_factory = aiosqlite.Row
            # Read-Only-Connection darf KEIN journal_mode/SCHEMA_SQL
            # ausfuehren (alles Schreibzugriffe). busy_timeout ist OK,
            # weil das ein Connection-lokales Pragma ist.
            await self._db.execute("PRAGMA busy_timeout = 15000")
            return
        if modus != "rw":
            raise ValueError(f"Speicher.verbinden modus muss 'rw' oder 'ro' sein, nicht {modus!r}")
        self._db = await aiosqlite.connect(str(pfad))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA busy_timeout = 5000")
        await self._db.execute("PRAGMA synchronous = NORMAL")
        await self._db.executescript(SCHEMA_SQL)
        await self._migriere()
        await self._db.commit()

    async def _migriere(self) -> None:
        """Fuegt neue Spalten zu bestehenden Tabellen hinzu (idempotent)."""
        assert self._db is not None
        migrationen = [
            ("sensor_messung", "boden_fruchtbarkeit", "REAL"),
            ("sensor_messung", "licht", "REAL"),
            ("sensor_messung", "quelle", "TEXT NOT NULL DEFAULT 'gardena'"),
            ("wetter_vorhersage", "wind_richtung_grad", "REAL DEFAULT 0"),
            ("wetter_vorhersage", "standort_id", "TEXT NOT NULL DEFAULT 'standard'"),
            # T-0045 VPD: Luftfeuchte (RH in %) nachziehen.
            ("wetter_vorhersage", "luftfeuchte", "REAL"),
            # T-0046 Quantile: q10/q90 Baender ins Drift-Log.
            ("ml_vorhersage_log", "prognose_q10", "REAL"),
            ("ml_vorhersage_log", "prognose_q90", "REAL"),
            # T-0062 Review-Fix: Dashboard-Polling-Dedupe pro Feature-Zeit.
            ("ml_vorhersage_log", "feature_zeitstempel", "TEXT"),
            ("entscheidung_log", "blocker_typ", "TEXT"),
            ("entscheidung_log", "scope", "TEXT NOT NULL DEFAULT 'zone'"),
            ("entscheidung_log", "scope_ref", "TEXT NOT NULL DEFAULT ''"),
            ("ventil_ereignis", "liter", "REAL"),
            # T-0084 Ops-Edit: Audit-Trail beim Nachklassifizieren des Auslosers.
            # JSON-Blob {alt, neu, geaendert_am, paar_id?} pro Aenderung.
            ("ventil_ereignis", "ausloser_korrektur", "TEXT"),
            # T-0335: Pre-Soak-Lauf-Gruppierung. `lauf_gruppe` = gemeinsame ID
            # aller Events eines orchestrierten Pre-Soak (Puls + Haupt), damit
            # die Giess-Historie sie als EINEN Lauf zeigt statt 2 Einzellaeufe.
            # `phase` = pre_soak | haupt (Anzeige Vorwaessern/Hauptdose). Beide
            # NULL fuer normale Einzellaeufe (Auto-Loop, manuell, watchdog).
            ("ventil_ereignis", "lauf_gruppe", "TEXT"),
            ("ventil_ereignis", "phase", "TEXT"),
            # T-0343: Ausloser einer Pre-Soak-Sequenz persistieren, damit ein
            # Auto-Pre-Soak nach Restart als AUTOMATIK fortgesetzt wird (sonst
            # Default 'manuell' = falsches Label, der Bug dieser Runde).
            ("pre_soak_state", "ausloser", "TEXT NOT NULL DEFAULT 'manuell'"),
            # T-0437: Cycle-and-Soak. `haupt_pulse_gestartet` ist der
            # Idempotenz-Zaehler -- ohne ihn wuerde ein Restart mitten in der
            # Sequenz bereits gelaufene Pulse erneut giessen. Defaults 1/0/0
            # bilden Bestandszeilen exakt auf das alte Verhalten ab.
            ("pre_soak_state", "haupt_pulse", "INTEGER NOT NULL DEFAULT 1"),
            ("pre_soak_state", "haupt_pause_s", "INTEGER NOT NULL DEFAULT 0"),
            (
                "pre_soak_state",
                "haupt_pulse_gestartet",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            # T-0196d/e: Aktueller Wert pro Achse (FYTA values.current
            # bzw. Backend-DLI-Aggregat). Ohne Default, NULL erlaubt
            # weil bei Erstanlage einer Zone der Wert noch nicht da ist.
            ("plant_optimum_achse", "current", "REAL"),
            # T-0270 (28.05.): Hybrid Stufe 1 Physik-Diagnose-Werte fuer
            # das Bias-Audit (Physik vs ML pro Empfehlung). Read-only-
            # Daten zum Zeitpunkt der Empfehlung + Auswertungs-Fehler.
            ("empfehlungs_audit", "prognose_physik_6h", "REAL"),
            ("empfehlungs_audit", "prognose_physik_12h", "REAL"),
            ("empfehlungs_audit", "prognose_physik_24h", "REAL"),
            ("empfehlungs_audit", "physik_quelle", "TEXT"),
            ("empfehlungs_audit", "k_basis_pro_h", "REAL"),
            ("empfehlungs_audit", "abweichung_physik_6h", "REAL"),
            ("empfehlungs_audit", "abweichung_physik_24h", "REAL"),
            # T-0349/T-0351/T-0353: State-Space-Shadow + Heuristik-
            # Shadow + Regime-Klassifikation + Routing-Wahl. Alles
            # additiv, read-only-Diagnose (kein Entscheidungs-Pfad).
            ("empfehlungs_audit", "prognose_statespace_6h", "REAL"),
            ("empfehlungs_audit", "prognose_statespace_12h", "REAL"),
            ("empfehlungs_audit", "prognose_statespace_24h", "REAL"),
            ("empfehlungs_audit", "statespace_quelle", "TEXT"),
            ("empfehlungs_audit", "abweichung_statespace_6h", "REAL"),
            ("empfehlungs_audit", "abweichung_statespace_24h", "REAL"),
            ("empfehlungs_audit", "prognose_heuristik_24h", "REAL"),
            ("empfehlungs_audit", "abweichung_heuristik_24h", "REAL"),
            ("empfehlungs_audit", "regime_6h", "TEXT"),
            ("empfehlungs_audit", "regime_24h", "TEXT"),
            ("empfehlungs_audit", "routing_quelle", "TEXT"),
        ]
        # Defensives Identifier-Quoting: SQLite erlaubt in ALTER TABLE keine
        # Parameter-Binding fuer Tabellen-/Spaltennamen, daher Whitelist-Regex.
        ident_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        for tabelle, spalte, typ in migrationen:
            if not (ident_re.match(tabelle) and ident_re.match(spalte)):
                raise ValueError(f"Ungueltiger Identifier: {tabelle}.{spalte}")
            try:
                await self._db.execute(
                    f'ALTER TABLE "{tabelle}" ADD COLUMN "{spalte}" {typ}'
                )
            except Exception as exc:
                if "duplicate column name" in str(exc).lower():
                    pass  # Spalte existiert bereits — erwarteter Fall
                else:
                    raise  # Unerwarteter Fehler — nicht verschlucken

        indexe = [
            """CREATE INDEX IF NOT EXISTS idx_entscheidung_scope_zeit
               ON entscheidung_log(scope, zeitstempel)""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_ml_log_feature_dedupe
               ON ml_vorhersage_log(
                   zone_id, horizont_h, modell_version, feature_zeitstempel
               )
               WHERE feature_zeitstempel IS NOT NULL""",
            # T-0408: Duplikat-Sturm-Backstop. Zusammen mit INSERT OR IGNORE
            # in `speichere_ventil_ereignis`. Bewusst OHNE dauer_sekunden und
            # ausloser im Schluessel: derselbe Ventil-Vorgang darf nicht
            # zweimal stehen, auch wenn zwei Pfade unterschiedliche Dauer
            # oder einen anderen Ausloser errechnen (genau so entstanden die
            # T-0408-Kopien -- der Ausloser wurde vom Pendant geerbt).
            # Migration der Altdaten: 20.07.2026, 545 Zeilen, Backup
            # backend/daten/backups/bewaesserung_vor_cleanup_t0408_2026-07-20.db.
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_ventil_ereignis_dedupe
               ON ventil_ereignis(zone_id, ventil_id, aktion, zeitstempel)""",
        ]
        for sql in indexe:
            await self._db.execute(sql)

        await self._migriere_live_lauf_state_primaerschluessel()

        # T-0100: Vor Anlage des UNIQUE-Index altbestand-Duplikate
        # zusammenfuehren — sonst schlaegt CREATE UNIQUE INDEX fehl. Wir
        # behalten pro (abfrage, vorhersage, standort) die kleinste id
        # (juengster Insert reicht; Werte sind identisch). Idempotent: laeuft
        # bei jedem Backend-Start, ist aber nach Erstlauf ein No-Op.
        await self._db.execute(
            """DELETE FROM wetter_vorhersage
               WHERE id NOT IN (
                   SELECT MIN(id) FROM wetter_vorhersage
                   GROUP BY abfrage_zeitstempel, vorhersage_zeitstempel,
                            standort_id
               )"""
        )
        await self._db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_wetter_vorhersage_uniq
               ON wetter_vorhersage(
                   abfrage_zeitstempel, vorhersage_zeitstempel, standort_id
               )"""
        )

        # F20: Enum-Validierung fuer ventil_ereignis.ausloser ueber Trigger.
        # SQLite erlaubt kein ALTER TABLE ADD CONSTRAINT; wir setzen statt
        # dessen zwei Trigger (BEFORE INSERT/UPDATE). Liste muss mit
        # `modelle.Ausloser`-Enum synchron bleiben — bei Erweiterung hier
        # nachziehen, sonst werfen legitime Inserts "ungueltiger ausloser".
        erlaubte_ausloser = (
            "'automatik','manuell','notfall_stopp','watchdog','unbekannt',"
            "'ignoriert','aquabloom'"
        )
        fehler_msg = (
            "ventil_ereignis.ausloser ungueltig - erlaubt: "
            "automatik, manuell, notfall_stopp, watchdog, unbekannt, ignoriert, "
            "aquabloom"
        )
        for aktion in ("INSERT", "UPDATE"):
            # Bei Enum-Erweiterung muss der Trigger neu gebaut werden, sonst
            # blockiert die alte Erlaubnisliste neue Werte. DROP-then-CREATE
            # ist idempotent und nicht teurer als IF NOT EXISTS.
            await self._db.execute(
                f"DROP TRIGGER IF EXISTS "
                f"trg_ventil_ereignis_ausloser_{aktion.lower()}"
            )
            await self._db.execute(
                f"""CREATE TRIGGER
                    trg_ventil_ereignis_ausloser_{aktion.lower()}
                    BEFORE {aktion} ON ventil_ereignis
                    FOR EACH ROW
                    WHEN NEW.ausloser NOT IN ({erlaubte_ausloser})
                    BEGIN
                        SELECT RAISE(ABORT, '{fehler_msg}');
                    END"""
            )

    async def _migriere_live_lauf_state_primaerschluessel(self) -> None:
        """Migriert live_lauf_state von kanal-PK auf (geraet_id, kanal).

        T-0203-Folge: Bei zwei DSWCs ist `kanal` allein keine physische
        Ventilidentitaet. Alte Datenbanken haben noch `kanal INTEGER PRIMARY
        KEY`; SQLite kann Primaerschluessel nicht per ALTER TABLE aendern,
        daher rename + copy. Idempotent: neue Schemas bleiben unveraendert.
        """
        assert self._db is not None
        async with self._db.execute("PRAGMA table_info(live_lauf_state)") as cursor:
            spalten = await cursor.fetchall()
        pk_spalten = [
            str(z["name"]) for z in sorted(spalten, key=lambda row: row["pk"])
            if z["pk"]
        ]
        if pk_spalten == ["geraet_id", "kanal"]:
            return
        if pk_spalten != ["kanal"]:
            raise RuntimeError(
                "Unerwartetes live_lauf_state-Schema: "
                f"Primaerschluessel={pk_spalten!r}"
            )

        await self._db.execute("ALTER TABLE live_lauf_state RENAME TO live_lauf_state_alt")
        await self._db.execute(
            """CREATE TABLE live_lauf_state (
                   geraet_id TEXT NOT NULL,
                   kanal INTEGER NOT NULL,
                   valve_id TEXT,
                   zone_ids_json TEXT NOT NULL,
                   dauer_sekunden INTEGER NOT NULL,
                   ausloser TEXT NOT NULL,
                   gestartet_am TEXT NOT NULL,
                   PRIMARY KEY (geraet_id, kanal)
               )"""
        )
        await self._db.execute(
            """INSERT OR REPLACE INTO live_lauf_state
               (geraet_id, kanal, valve_id, zone_ids_json, dauer_sekunden,
                ausloser, gestartet_am)
               SELECT geraet_id, kanal, valve_id, zone_ids_json,
                      dauer_sekunden, ausloser, gestartet_am
               FROM live_lauf_state_alt
               WHERE geraet_id IS NOT NULL"""
        )
        await self._db.execute("DROP TABLE live_lauf_state_alt")

    async def schliessen(self) -> None:
        """Schliesst die Datenbankverbindung. Idempotent.

        T-0171: `_geschlossen` wird VOR `close()` gesetzt. Ein Inflight-
        Request, der waehrend des Shutdowns noch einen DB-Read startet,
        wird so ueber das Flag als Shutdown-Race erkannt und vom
        `shutdown_race_guard`-Middleware leise abgefangen.
        """
        self._geschlossen = True
        if self._db:
            await self._db.close()
            self._db = None

    async def backup(self, ziel_pfad: Path) -> None:
        """Erzeugt konsistenten Snapshot via SQLite-Backup-API.

        Ablauf: schreibt nach <ziel>.tmp, prueft integrity_check, dann
        atomares os.replace nach <ziel>. Bei Fehler wird <ziel>.tmp
        entfernt und die Exception propagiert; <ziel> bleibt unberuehrt.

        T-0218: Das Backup laeuft ueber eine EIGENE, kurzlebige
        Read-Only-Quell-Connection statt ueber `self._db`. Die SQLite-
        Online-Backup-API kopiert die DB Page fuer Page; bei der
        ~84-MB-DB dauert das ~50 s. Liefe das ueber `self._db`, waere
        der aiosqlite-Worker-Thread der Produktiv-Connection die ganze
        Zeit belegt — jeder API-Read und der Entscheidungsloop haengen
        so lange (gemessen 20.05.: `/api/zonen` 41-53 s waehrend des
        Backups). WAL erlaubt parallele Reader; die separate Connection
        liest denselben konsistenten Commit-Snapshot.

        Bei `:memory:`-DB (Tests) gibt es keine zweite Connection zur
        selben DB — dort wird auf `self._db` zurueckgefallen.
        """
        assert self._db is not None
        ziel_pfad = Path(ziel_pfad)
        ziel_pfad.parent.mkdir(parents=True, exist_ok=True)
        tmp_pfad = ziel_pfad.with_name(ziel_pfad.name + ".tmp")

        if tmp_pfad.exists():
            tmp_pfad.unlink()

        # T-0218: eigene Read-Connection als Backup-Quelle, ausser bei
        # :memory: (dort ist eine zweite Connection eine separate DB).
        quelle_extern: aiosqlite.Connection | None = None
        if self._db_pfad != ":memory:":
            quelle_extern = await aiosqlite.connect(
                f"file:{Path(self._db_pfad)}?mode=ro", uri=True,
            )
        quelle = quelle_extern if quelle_extern is not None else self._db

        try:
            async with aiosqlite.connect(str(tmp_pfad)) as ziel:
                await quelle.backup(ziel)
                async with ziel.execute("PRAGMA integrity_check") as cur:
                    zeile = await cur.fetchone()
                status = zeile[0] if zeile else None
                if status != "ok":
                    raise RuntimeError(
                        f"integrity_check fehlgeschlagen: {status!r}"
                    )
            os.replace(str(tmp_pfad), str(ziel_pfad))
        except BaseException:
            if tmp_pfad.exists():
                try:
                    tmp_pfad.unlink()
                except OSError:
                    pass
            raise
        finally:
            if quelle_extern is not None:
                await quelle_extern.close()

    # --- Sensordaten ---

    async def speichere_messung(self, messung: SensorMessung) -> None:
        """Speichert eine einzelne Sensormessung.

        T-0186 (13.05.2026): Dedup gegen Cloud-WebSocket-Echo. Manche
        Gardena-Sensoren senden zwei Beats binnen Millisekunden mit
        unterschiedlich gerundeten Werten (z.B. 45 + 50 bei einem
        Real-Wert von 47-48). Heuristik (T-0055-B1) wertet das als
        Sprung -> Phantom-Bewaesserungs-Event. Symptom-Beispiel:
        Waldblumen 12.05. 09:37:35 zwei Werte 45 + 50 innerhalb 108 ms.

        Filter: wenn fuer dieselbe (zone_id, geraet_id) ein Eintrag
        binnen 2 Sekunden vor dem neuen Beat existiert -> Doppelung,
        Insert skippen. Erster Wert bleibt erhalten (mehr "frueh
        ankommend" Daten haben Vorzug).
        """
        assert self._db is not None
        # T-0186 Dedup-Check (nur bei vorhandener geraet_id, sonst
        # nicht eindeutig zuordenbar)
        if messung.geraet_id:
            fenster_anfang = (
                messung.zeitstempel - timedelta(seconds=2)
            ).isoformat()
            fenster_ende = messung.zeitstempel.isoformat()
            async with self._db.execute(
                """SELECT 1 FROM sensor_messung
                   WHERE zone_id = ? AND geraet_id = ?
                     AND zeitstempel >= ? AND zeitstempel < ?
                   LIMIT 1""",
                (
                    messung.zone_id,
                    messung.geraet_id,
                    fenster_anfang,
                    fenster_ende,
                ),
            ) as cursor:
                doppelung = await cursor.fetchone()
            if doppelung is not None:
                logger.debug(
                    "speicher.messung_dedup_skip",
                    zone_id=messung.zone_id,
                    geraet_id=messung.geraet_id,
                    zeitstempel=messung.zeitstempel.isoformat(),
                )
                return

        async def _op() -> None:
            await self._db.execute(
                """INSERT INTO sensor_messung
                   (zeitstempel, zone_id, geraet_id, boden_feuchte, boden_temperatur,
                    umgebungs_temperatur, licht_intensitaet, batterie_prozent,
                    boden_fruchtbarkeit, licht, quelle)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    messung.zeitstempel.isoformat(),
                    messung.zone_id,
                    messung.geraet_id,
                    messung.boden_feuchte,
                    messung.boden_temperatur,
                    messung.umgebungs_temperatur,
                    messung.licht_intensitaet,
                    messung.batterie_prozent,
                    messung.boden_fruchtbarkeit,
                    messung.licht,
                    messung.quelle.value,
                ),
            )
            if not self._in_transaktion:
                await self._db.commit()

        await self._mit_lock_retry(_op, label="speichere_messung")

    async def hole_messungen(
        self, zone_id: str, von: datetime | None = None, bis: datetime | None = None
    ) -> list[SensorMessung]:
        """Liest Sensormessungen fuer eine Zone in einem Zeitraum."""
        assert self._db is not None
        bedingungen = ["zone_id = ?"]
        params: list = [zone_id]

        if von:
            bedingungen.append("zeitstempel >= ?")
            params.append(von.isoformat())
        if bis:
            bedingungen.append("zeitstempel <= ?")
            params.append(bis.isoformat())

        sql = f"""SELECT * FROM sensor_messung
                  WHERE {' AND '.join(bedingungen)}
                  ORDER BY zeitstempel DESC"""

        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()

        return [
            SensorMessung(
                zeitstempel=datetime.fromisoformat(z["zeitstempel"]),
                zone_id=z["zone_id"],
                geraet_id=z["geraet_id"],
                boden_feuchte=z["boden_feuchte"],
                boden_temperatur=z["boden_temperatur"],
                umgebungs_temperatur=z["umgebungs_temperatur"],
                licht_intensitaet=z["licht_intensitaet"],
                batterie_prozent=z["batterie_prozent"],
                boden_fruchtbarkeit=z["boden_fruchtbarkeit"],
                licht=z["licht"],
                quelle=DatenQuelle(z["quelle"]),
            )
            for z in zeilen
        ]

    async def hole_messungen_bulk(
        self,
        zone_ids: list[str],
        von: datetime | None = None,
        bis: datetime | None = None,
    ) -> dict[str, list[SensorMessung]]:
        """T-0200: Bulk-Variante von `hole_messungen` fuer den Dashboard-
        Snapshot-Endpoint. Eine einzige SQL-Query mit `zone_id IN (...)`
        ersetzt 14× pro Zone, ohne dass die Per-Zone-Methode geaendert
        werden muss. Reihenfolge je Zone identisch zu `hole_messungen`
        (chronologisch via reversed(DESC)).

        Garantiert fuer jeden uebergebenen `zone_id` einen Key im
        Result-Dict (leere Liste wenn keine Messungen). Damit kann der
        Aufrufer ohne `.get(zid, [])` iterieren.
        """
        assert self._db is not None
        if not zone_ids:
            return {}

        platzhalter = ",".join("?" for _ in zone_ids)
        bedingungen = [f"zone_id IN ({platzhalter})"]
        params: list = list(zone_ids)
        if von:
            bedingungen.append("zeitstempel >= ?")
            params.append(von.isoformat())
        if bis:
            bedingungen.append("zeitstempel <= ?")
            params.append(bis.isoformat())

        sql = (
            "SELECT * FROM sensor_messung WHERE "
            + " AND ".join(bedingungen)
            + " ORDER BY zeitstempel DESC"
        )

        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()

        gruppen: dict[str, list[SensorMessung]] = {zid: [] for zid in zone_ids}
        for z in zeilen:
            gruppen[z["zone_id"]].append(SensorMessung(
                zeitstempel=datetime.fromisoformat(z["zeitstempel"]),
                zone_id=z["zone_id"],
                geraet_id=z["geraet_id"],
                boden_feuchte=z["boden_feuchte"],
                boden_temperatur=z["boden_temperatur"],
                umgebungs_temperatur=z["umgebungs_temperatur"],
                licht_intensitaet=z["licht_intensitaet"],
                batterie_prozent=z["batterie_prozent"],
                boden_fruchtbarkeit=z["boden_fruchtbarkeit"],
                licht=z["licht"],
                quelle=DatenQuelle(z["quelle"]),
            ))
        return gruppen

    async def hole_feuchte_werte(
        self, zone_id: str, von: datetime, bis: datetime,
        ausschluss_fenster: (
            list[tuple[datetime, datetime, str | None]] | None
        ) = None,
    ) -> list[float]:
        """T-0049: Nur `boden_feuchte`-Werte in einem Zeitraum.

        Schlanker als `hole_messungen` — fuer Perzentil-Analysen brauchen
        wir nur den Feuchte-Wert, nicht die komplette Row (30 Tage ×
        11 Zonen × 288 Messungen/Tag = ~95k Zeilen).

        `ausschluss_fenster`: Liste von (von, bis, geraet_id)-Tripeln (z. B.
        aus `config.ml_ausschluss_fenster` fuer eine Zone), die aus dem
        Ergebnis herausgefiltert werden. Nuetzlich fuer Sensor-Umzuege oder
        Initial-Setup-Phasen mit pathologischen Werten.

        T-0386: `geraet_id is None` -> das Fenster schneidet ALLE Sensoren der
        Zone weg; ist eine geraet_id gesetzt, NUR die Rows dieses Sensors (der
        gesunde Nachbarsensor bleibt im Ergebnis -- das war der Zweck des
        T-0267-Feldes, wurde aber vom frueheren zone-weiten Filter ignoriert).
        """
        assert self._db is not None
        bedingungen = [
            "zone_id = ?", "zeitstempel >= ?", "zeitstempel <= ?",
            "boden_feuchte IS NOT NULL",
        ]
        params: list = [zone_id, von.isoformat(), bis.isoformat()]
        if ausschluss_fenster:
            for a_von, a_bis, a_geraet in ausschluss_fenster:
                if a_geraet is None:
                    bedingungen.append(
                        "NOT (zeitstempel >= ? AND zeitstempel <= ?)"
                    )
                    params.extend([a_von.isoformat(), a_bis.isoformat()])
                else:
                    bedingungen.append(
                        "NOT (zeitstempel >= ? AND zeitstempel <= ? "
                        "AND geraet_id = ?)"
                    )
                    params.extend(
                        [a_von.isoformat(), a_bis.isoformat(), a_geraet],
                    )
        sql = (
            "SELECT boden_feuchte FROM sensor_messung WHERE "
            + " AND ".join(bedingungen)
        )
        async with self._db.execute(sql, tuple(params)) as cursor:
            zeilen = await cursor.fetchall()
        return [float(z["boden_feuchte"]) for z in zeilen]

    async def hole_skalen_mapping(
        self, zone_id: str, quelle: str
    ) -> dict | None:
        """T-0181: liefert (a, b)-Koeffizienten fuer das lineare Mapping
        `feuchte_mapped = a * feuchte_raw + b` einer Sensor-Quelle.

        Heute Stub: die Tabelle wird (noch) nicht gefuellt — Fitten kommt
        als T-0181-Folge, sobald 4-6 Wochen Parallel-Daten vorliegen.
        Return `None` heisst "Identity" und ist Default-Verhalten.
        """
        assert self._db is not None
        async with self._db.execute(
            "SELECT a, b, n_obs, gefittet_am "
            "FROM sensor_skalen_mapping "
            "WHERE zone_id = ? AND quelle = ?",
            (zone_id, quelle),
        ) as cursor:
            zeile = await cursor.fetchone()
        if not zeile:
            return None
        return {
            "a": float(zeile["a"]),
            "b": float(zeile["b"]),
            "n_obs": int(zeile["n_obs"]),
            "gefittet_am": zeile["gefittet_am"],
        }

    async def _hole_skalen_mappings_zonen(
        self, zone_ids: list[str],
    ) -> dict[str, dict[str, dict]]:
        """T-0200: Bulk-Variante fuer den Dashboard-Snapshot. Liefert
        dict[zone_id, dict[quelle, mapping]]. Zonen ohne Mapping-Eintrag
        fehlen im Ergebnis (Aufrufer muss `.get(zone_id, {})` nutzen).
        """
        assert self._db is not None
        if not zone_ids:
            return {}
        platzhalter = ",".join("?" for _ in zone_ids)
        async with self._db.execute(
            "SELECT zone_id, quelle, a, b, n_obs, gefittet_am "
            f"FROM sensor_skalen_mapping WHERE zone_id IN ({platzhalter})",
            tuple(zone_ids),
        ) as cursor:
            zeilen = await cursor.fetchall()
        out: dict[str, dict[str, dict]] = {}
        for z in zeilen:
            out.setdefault(z["zone_id"], {})[z["quelle"]] = {
                "a": float(z["a"]),
                "b": float(z["b"]),
                "n_obs": int(z["n_obs"]),
                "gefittet_am": z["gefittet_am"],
            }
        return out

    async def _hole_skalen_mappings_zone(
        self, zone_id: str
    ) -> dict[str, dict]:
        """T-0181: alle (quelle -> mapping)-Eintraege einer Zone in einem
        Query. Hot-Path-Hilfe fuer `letzte_messung_aggregiert`."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT quelle, a, b, n_obs, gefittet_am "
            "FROM sensor_skalen_mapping WHERE zone_id = ?",
            (zone_id,),
        ) as cursor:
            zeilen = await cursor.fetchall()
        return {
            z["quelle"]: {
                "a": float(z["a"]),
                "b": float(z["b"]),
                "n_obs": int(z["n_obs"]),
                "gefittet_am": z["gefittet_am"],
            }
            for z in zeilen
        }

    async def upsert_skalen_mapping(
        self,
        zone_id: str,
        quelle: str,
        a: float,
        b: float,
        n_obs: int,
        gefittet_am: datetime | None = None,
    ) -> None:
        """T-0181-Folge: Schreibseite des Skalen-Mappings.

        Persistiert die lineare Skalen-Transformation
        `feuchte_mapped = a * feuchte_raw + b` pro (Zone, Sensor-Quelle).
        Pattern analog `upsert_wetter_archiv` / `speichere_plant_optimum`
        (ON CONFLICT-UPDATE), via `_mit_lock_retry` gewrappt — damit
        kollidiert der Fit-Job sauber mit ML-Retrain/Backup-Bursts
        (T-0210).
        """
        assert self._db is not None
        zeitstempel = (gefittet_am or datetime.now()).isoformat()

        async def _op() -> None:
            assert self._db is not None
            await self._db.execute(
                """INSERT INTO sensor_skalen_mapping
                   (zone_id, quelle, a, b, n_obs, gefittet_am)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(zone_id, quelle) DO UPDATE SET
                     a = excluded.a,
                     b = excluded.b,
                     n_obs = excluded.n_obs,
                     gefittet_am = excluded.gefittet_am""",
                (zone_id, quelle, float(a), float(b), int(n_obs), zeitstempel),
            )
            if not self._in_transaktion:
                await self._db.commit()

        await self._mit_lock_retry(_op, label="upsert_skalen_mapping")

    async def loesche_skalen_mapping(self, zone_id: str, quelle: str) -> None:
        """T-0285: entfernt ein gespeichertes Skalen-Mapping.

        Wird vom SkalenMappingFitJob aufgerufen, wenn bei AUSREICHENDER
        Datenlage (genug Obs + Spannweite) kein valides lineares Mapping
        mehr besteht (zu schwache Korrelation / zu hohes Residuum). Sonst
        wuerde ein einmal gefittetes Fehl-Mapping (Rausch-Slope) dauerhaft
        die Feuchte verzerren -- Realfall waldblumen: FYTA<->Gardena
        unkorreliert (r~0), Fit a=1.666 zog FYTA 58->51. Idempotent.
        """
        assert self._db is not None

        async def _op() -> None:
            assert self._db is not None
            await self._db.execute(
                "DELETE FROM sensor_skalen_mapping "
                "WHERE zone_id = ? AND quelle = ?",
                (zone_id, quelle),
            )
            if not self._in_transaktion:
                await self._db.commit()

        await self._mit_lock_retry(_op, label="loesche_skalen_mapping")

    async def hole_k_basis(self, zone_id: str) -> dict | None:
        """Hybrid Stufe 1: liefert das gefittete `k_basis` einer Zone.
        Return None heisst "kein Fit verfuegbar"; Aufrufer faellt auf
        `default_tau_stunden` zurueck.
        """
        assert self._db is not None
        async with self._db.execute(
            "SELECT k_basis, et0_basis_mm_pro_h, n_phasen, mae, gefittet_am "
            "FROM physik_k_basis WHERE zone_id = ?",
            (zone_id,),
        ) as cursor:
            zeile = await cursor.fetchone()
        if not zeile:
            return None
        return {
            "k_basis": float(zeile["k_basis"]),
            "et0_basis_mm_pro_h": float(zeile["et0_basis_mm_pro_h"]),
            "n_phasen": int(zeile["n_phasen"]),
            "mae": float(zeile["mae"]) if zeile["mae"] is not None else None,
            "gefittet_am": zeile["gefittet_am"],
        }

    async def _hole_k_basis_zonen(
        self, zone_ids: list[str],
    ) -> dict[str, dict]:
        """Bulk-Reader fuer den Snapshot-Endpoint. Liefert
        `{zone_id: {...}}`. Zonen ohne Fit fehlen im Dict."""
        assert self._db is not None
        if not zone_ids:
            return {}
        platzhalter = ",".join("?" for _ in zone_ids)
        async with self._db.execute(
            "SELECT zone_id, k_basis, et0_basis_mm_pro_h, n_phasen, "
            "mae, gefittet_am "
            f"FROM physik_k_basis WHERE zone_id IN ({platzhalter})",
            tuple(zone_ids),
        ) as cursor:
            zeilen = await cursor.fetchall()
        return {
            z["zone_id"]: {
                "k_basis": float(z["k_basis"]),
                "et0_basis_mm_pro_h": float(z["et0_basis_mm_pro_h"]),
                "n_phasen": int(z["n_phasen"]),
                "mae": float(z["mae"]) if z["mae"] is not None else None,
                "gefittet_am": z["gefittet_am"],
            }
            for z in zeilen
        }

    async def upsert_k_basis(
        self,
        zone_id: str,
        k_basis: float,
        et0_basis_mm_pro_h: float,
        n_phasen: int,
        mae: float | None,
        gefittet_am: datetime | None = None,
    ) -> None:
        """Hybrid Stufe 1: Schreibseite des `physik_k_basis`-Fits."""
        assert self._db is not None
        zeitstempel = (gefittet_am or datetime.now()).isoformat()

        async def _op() -> None:
            assert self._db is not None
            await self._db.execute(
                """INSERT INTO physik_k_basis
                   (zone_id, k_basis, et0_basis_mm_pro_h, n_phasen, mae,
                    gefittet_am)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(zone_id) DO UPDATE SET
                     k_basis = excluded.k_basis,
                     et0_basis_mm_pro_h = excluded.et0_basis_mm_pro_h,
                     n_phasen = excluded.n_phasen,
                     mae = excluded.mae,
                     gefittet_am = excluded.gefittet_am""",
                (
                    zone_id, float(k_basis), float(et0_basis_mm_pro_h),
                    int(n_phasen),
                    float(mae) if mae is not None else None,
                    zeitstempel,
                ),
            )
            if not self._in_transaktion:
                await self._db.commit()

        await self._mit_lock_retry(_op, label="upsert_k_basis")

    async def upsert_wirkung_fit(
        self,
        zone_id: str,
        wmax: float,
        r0: float,
        tau: float,
        n: int,
        r2: float,
        mse: float,
        angenommen: bool,
        grund: str,
        gefittet_am: datetime | None = None,
    ) -> None:
        """T-0292 Stufe 2: Schreibseite des Plateau-Wirkungs-Fits.

        Persistiert pro Zone IMMER den letzten Fit (auch `angenommen=False`,
        damit `/api/ml/status` den Ablehnungsgrund zeigen kann). Die
        Adoption in `_berechne_dauer` liest nur Records mit
        `angenommen=1`, die juenger als `max_fit_alter_tage` sind.
        """
        assert self._db is not None
        zeitstempel = (gefittet_am or datetime.now()).isoformat()

        async def _op() -> None:
            assert self._db is not None
            await self._db.execute(
                """INSERT INTO wirkung_fit
                   (zone_id, wmax, r0, tau, n, r2, mse, angenommen, grund,
                    gefittet_am)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(zone_id) DO UPDATE SET
                     wmax = excluded.wmax,
                     r0 = excluded.r0,
                     tau = excluded.tau,
                     n = excluded.n,
                     r2 = excluded.r2,
                     mse = excluded.mse,
                     angenommen = excluded.angenommen,
                     grund = excluded.grund,
                     gefittet_am = excluded.gefittet_am""",
                (
                    zone_id, float(wmax), float(r0), float(tau), int(n),
                    float(r2), float(mse), 1 if angenommen else 0, grund,
                    zeitstempel,
                ),
            )
            if not self._in_transaktion:
                await self._db.commit()

        await self._mit_lock_retry(_op, label="upsert_wirkung_fit")

    async def hole_wirkung_fit(self, zone_id: str) -> dict | None:
        """T-0292 Stufe 2: liefert den letzten Plateau-Fit einer Zone.

        Return None heisst "kein Fit". `angenommen` ist bool. Aufrufer
        (entscheidung._aufgeloeste_wirkung) prueft `angenommen` + Alter
        selbst und faellt sonst auf die Konfig-Werte zurueck.
        """
        assert self._db is not None
        async with self._db.execute(
            "SELECT wmax, r0, tau, n, r2, mse, angenommen, grund, gefittet_am "
            "FROM wirkung_fit WHERE zone_id = ?",
            (zone_id,),
        ) as cursor:
            zeile = await cursor.fetchone()
        if not zeile:
            return None
        return {
            "wmax": float(zeile["wmax"]),
            "r0": float(zeile["r0"]),
            "tau": float(zeile["tau"]),
            "n": int(zeile["n"]),
            "r2": float(zeile["r2"]),
            "mse": float(zeile["mse"]),
            "angenommen": bool(zeile["angenommen"]),
            "grund": zeile["grund"],
            "gefittet_am": zeile["gefittet_am"],
        }

    async def hole_messungen_pro_quelle(
        self,
        zone_id: str,
        quellen: list[str],
        von: datetime,
        bis: datetime,
    ) -> dict[str, list[SensorMessung]]:
        """T-0181-Folge: Bulk-Reader fuer den Skalen-Mapping-Fit.

        Liest in einer einzigen Query alle Messungen einer Zone, die
        in `quellen` aufgefuehrt sind (z. B. `["gardena", "fyta"]`),
        und gruppiert sie nach `quelle`. Reihenfolge je Quelle
        chronologisch absteigend (analog `hole_messungen`).
        Aufrufer kriegt fuer jede angefragte Quelle einen Key, ggf.
        mit leerer Liste.
        """
        assert self._db is not None
        if not quellen:
            return {}
        platzhalter = ",".join("?" for _ in quellen)
        sql = (
            "SELECT * FROM sensor_messung WHERE zone_id = ? "
            f"AND quelle IN ({platzhalter}) "
            "AND zeitstempel >= ? AND zeitstempel <= ? "
            "AND boden_feuchte IS NOT NULL "
            "ORDER BY zeitstempel DESC"
        )
        params: list = [zone_id, *quellen, von.isoformat(), bis.isoformat()]
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()
        gruppen: dict[str, list[SensorMessung]] = {q: [] for q in quellen}
        for z in zeilen:
            gruppen.setdefault(z["quelle"], []).append(SensorMessung(
                zeitstempel=datetime.fromisoformat(z["zeitstempel"]),
                zone_id=z["zone_id"],
                geraet_id=z["geraet_id"],
                boden_feuchte=z["boden_feuchte"],
                boden_temperatur=z["boden_temperatur"],
                umgebungs_temperatur=z["umgebungs_temperatur"],
                licht_intensitaet=z["licht_intensitaet"],
                batterie_prozent=z["batterie_prozent"],
                boden_fruchtbarkeit=z["boden_fruchtbarkeit"],
                licht=z["licht"],
                quelle=DatenQuelle(z["quelle"]),
            ))
        return gruppen

    def setze_aggregat_lead(self, mapping: dict[str, str]) -> None:
        """T-0332: Setzt pro Zone den Aggregat-Lead-Sensor (zone_id ->
        geraet_id). Beim Startup aus der Konfig (`aggregat_lead_geraet`)
        gespeist. Eine Zone mit Lead nutzt fuer das Aggregat NUR diesen
        Sensor (statt Median ueber alle Zonen-Sensoren). Liefert der Lead im
        Fenster keinen Wert -> KEINE Messung (T-0384), nicht Median."""
        self._aggregat_lead = dict(mapping)

    def _wende_aggregat_lead_an(
        self, zone_id: str, pro_geraet: dict[str, dict],
    ) -> dict[str, dict]:
        """T-0332: Reduziert `pro_geraet` auf den konfigurierten Lead-Sensor.

        T-0384: Hat die Zone einen Lead, dieser aber im Fenster KEINEN Wert,
        wird NICHT mehr still auf den Median der uebrigen Sensoren
        zurueckgefallen. Genau dieser Median ist bei `hecke` die cross-spray-
        inflationierte Groesse, gegen die der Lead ueberhaupt gesetzt wurde
        (Lead ~45 vs. FYTA-Median ~76) -> ein Lead-Dropout haette die scharfe
        KORRIDOR-Hecke still unter-waessert. Stattdessen leer -> Aufrufer
        sieht "keine Messung" und kann bewusst + geloggt auf ein laengeres
        Fenster erweitern (Entscheidungspfad: `_robuste_feuchte`). Die
        Sensor-IDENTITAET wechselt nie still; nur der Zeithorizont waechst.
        """
        lead = self._aggregat_lead.get(zone_id)
        if not lead:
            return pro_geraet
        if lead in pro_geraet:
            return {lead: pro_geraet[lead]}
        return {}

    async def letzte_messung_aggregiert(
        self,
        zone_id: str,
        fenster_minuten: int = 90,
        jetzt: datetime | None = None,
    ) -> SensorMessung | None:
        """T-0179c: Median-Aggregat fuer mehrere Sensoren in derselben Zone.

        Hintergrund: Mit FYTA Terra fuer Waldblumen sind 3 Sensoren in
        derselben Zone (1× Gardena + 2× FYTA Terra). Bewaesserungs-Logik
        soll **ein** repraesentativer Wert nutzen (siehe Memory
        `arbeitspattern_sensor_platzierung.md` Option C).

        Algorithmus:
        1. Hole alle Messungen der Zone der letzten `fenster_minuten`.
        2. Pro `geraet_id` den juengsten Wert nehmen.
        3. Median ueber die Sensor-Werte als kanonischer Zonen-Wert.
        4. Boden_Temperatur, Batterie etc. werden ebenfalls per Median
           aggregiert; Zeitstempel = neuester der beruecksichtigten
           Sensoren; geraet_id = "aggregat:<n>" als Marker.

        Bei nur einem Sensor in der Zone: identisch zu `letzte_messung`.
        Bei null Sensoren in der Fenster-Periode: None (analog
        `letzte_messung`).
        """
        from statistics import median
        assert self._db is not None
        jetzt = jetzt or datetime.now()
        von = (jetzt - timedelta(minutes=fenster_minuten)).isoformat()
        async with self._db.execute(
            """SELECT * FROM sensor_messung
               WHERE zone_id = ? AND zeitstempel >= ?
               ORDER BY zeitstempel DESC""",
            (zone_id, von),
        ) as cursor:
            zeilen = await cursor.fetchall()
        if not zeilen:
            return None

        # Pro geraet_id den juengsten Wert (DESC sortiert -> first wins)
        pro_geraet: dict[str, dict] = {}
        for z in zeilen:
            gid = z["geraet_id"] or ""
            if gid not in pro_geraet:
                pro_geraet[gid] = dict(z)
        if not pro_geraet:
            return None

        # T-0332: Aggregat-Lead -- Zone liest nur den Lead-Sensor (Cross-Spray).
        pro_geraet = self._wende_aggregat_lead_an(zone_id, pro_geraet)
        # T-0384: Lead konfiguriert, aber im Fenster ohne Wert -> keine Messung
        # (KEIN stiller Median-Fallback auf die kontaminierten Nachbarsensoren).
        if not pro_geraet:
            return None

        # T-0181: pro Sensor-Quelle Skalen-Mapping anwenden, BEVOR Median
        # berechnet wird. Default = Identity (Mapping-Tabelle leer).
        # Nur `boden_feuchte` wird transformiert — FYTA Terra und Gardena
        # nutzen den gleichen Profil-Code aber strukturell verschiedene
        # 0-100-Skalen. Temperatur etc. bleibt unveraendert.
        mappings = await self._hole_skalen_mappings_zone(zone_id)
        if mappings:
            for z in pro_geraet.values():
                if z["boden_feuchte"] is None:
                    continue
                m = mappings.get(z["quelle"])
                if m is not None:
                    z["boden_feuchte"] = m["a"] * z["boden_feuchte"] + m["b"]

        # Bei einem Sensor: direkt zurueckgeben (kein Aggregat noetig)
        if len(pro_geraet) == 1:
            z = next(iter(pro_geraet.values()))
            return SensorMessung(
                zeitstempel=datetime.fromisoformat(z["zeitstempel"]),
                zone_id=z["zone_id"], geraet_id=z["geraet_id"],
                boden_feuchte=z["boden_feuchte"],
                boden_temperatur=z["boden_temperatur"],
                umgebungs_temperatur=z["umgebungs_temperatur"],
                licht_intensitaet=z["licht_intensitaet"],
                batterie_prozent=z["batterie_prozent"],
                boden_fruchtbarkeit=z["boden_fruchtbarkeit"],
                licht=z["licht"],
                quelle=DatenQuelle(z["quelle"]),
            )

        # Mehrere Sensoren: Median pro numerisches Feld, neuester Zeitstempel
        werte = list(pro_geraet.values())

        def med(spalte: str) -> float | None:
            vals = [v[spalte] for v in werte if v[spalte] is not None]
            return float(median(vals)) if vals else None

        neuester_ts = max(datetime.fromisoformat(v["zeitstempel"]) for v in werte)
        # Quelle wird vom Sensor mit dem neuesten Wert geerbt (kosmetisch)
        neueste = max(werte, key=lambda v: v["zeitstempel"])

        return SensorMessung(
            zeitstempel=neuester_ts,
            zone_id=zone_id,
            geraet_id=f"aggregat:{len(pro_geraet)}",
            boden_feuchte=med("boden_feuchte"),
            boden_temperatur=med("boden_temperatur"),
            umgebungs_temperatur=med("umgebungs_temperatur"),
            licht_intensitaet=med("licht_intensitaet"),
            batterie_prozent=med("batterie_prozent"),
            boden_fruchtbarkeit=med("boden_fruchtbarkeit"),
            licht=med("licht"),
            quelle=DatenQuelle(neueste["quelle"]),
        )

    async def min_gemappte_feuchte(
        self,
        zone_id: str,
        fenster_minuten: int = 90,
        jetzt: datetime | None = None,
    ) -> float | None:
        """T-0279 Phase 2: trockenste (= minimale) Sensor-Lesung der Zone
        NACH Skalen-Mapping.

        Hintergrund: das Median-Aggregat (`letzte_messung_aggregiert`)
        verwaessert bei Multi-Sensor-Zonen das konservative Warnsignal
        des trockensten Sensors. Realfall waldblumen 30.05.: Gardena 30
        (unter Welkepunkt 32!), FYTA-Paar 47/52 -> Median ~44, kein
        Trigger. Der proaktive Bewaesserungs-Trigger (feucht-liebende
        Zonen) soll dem TROCKENSTEN gemappten Sensor folgen.

        Mapping wird wie in `letzte_messung_aggregiert` angewendet (FYTA
        -> Gardena-Referenzskala), dann das Minimum genommen. Bei einem
        Sensor identisch zu dessen gemapptem Wert. None wenn keine
        Messung im Fenster.
        """
        assert self._db is not None
        jetzt = jetzt or datetime.now()
        von = (jetzt - timedelta(minutes=fenster_minuten)).isoformat()
        async with self._db.execute(
            """SELECT geraet_id, quelle, boden_feuchte, zeitstempel
               FROM sensor_messung
               WHERE zone_id = ? AND zeitstempel >= ?
                 AND boden_feuchte IS NOT NULL
               ORDER BY zeitstempel DESC""",
            (zone_id, von),
        ) as cursor:
            zeilen = await cursor.fetchall()
        if not zeilen:
            return None
        # Pro geraet_id den juengsten Wert.
        pro_geraet: dict[str, dict] = {}
        for z in zeilen:
            gid = z["geraet_id"] or ""
            if gid not in pro_geraet:
                pro_geraet[gid] = dict(z)
        # T-0383-Restfund: den aggregat_lead auch hier anwenden -- sonst zweite
        # Wahrheit (die Median-/Aggregat-Pfade lesen bei gesetztem Lead NUR den
        # Lead-Sensor, dieser Min-Pfad wuerde sonst weiter ueber alle Sensoren
        # minimieren). Lead gesetzt aber im Fenster ohne Wert -> keine Messung
        # (T-0384: KEIN stiller Fallback auf die uebrigen Sensoren). Heute
        # ohne Live-Effekt (proaktiv_min_sensor=waldblumen ohne Lead,
        # aggregat_lead=hecke ohne min-Sensor), aber schliesst die Falle, falls
        # je beides auf einer Zone gesetzt wird.
        pro_geraet = self._wende_aggregat_lead_an(zone_id, pro_geraet)
        if not pro_geraet:
            return None
        mappings = await self._hole_skalen_mappings_zone(zone_id)
        werte: list[float] = []
        for z in pro_geraet.values():
            f = float(z["boden_feuchte"])
            m = mappings.get(z["quelle"]) if mappings else None
            if m is not None:
                f = m["a"] * f + m["b"]
            werte.append(f)
        return min(werte) if werte else None

    async def sensoren_auf_null_seit(
        self, zone_id: str, mindest_stunden: float = 24.0,
        jetzt: datetime | None = None,
    ) -> list[dict]:
        """T-0426: Sensoren einer Zone, die durchgehend EXAKT 0.0 lesen.

        Der Gardena-Bodensensor faellt bei Kontaktverlust in der
        Trockenphase auf exakt 0 und bleibt dort -- er ist dann nicht
        "sehr trocken", sondern liefert keinen Messwert mehr
        (Memory fehlerpattern_gardena_kontaktverlust_trockenphase).
        Realfall magerwiese 23.07.: seit 19.07. 20:44 durchgehend 0.0
        (96 Messungen), waehrend derselbe Sensortyp an der Hecke stabil 35
        lieferte. Der Watchdog meldete daraufhin "akut, bitte giessen" --
        auf Basis einer Zahl, die keine Feuchte ist.

        Bewusst `= 0.0` und nicht `< schwelle`: ein Sensor, der 2 oder 5
        liest, misst noch. Die exakte Null ueber lange Zeit ist die
        Ausfall-Signatur.

        Rueckgabe je Geraet: geraet_id, seit (erster 0.0-Wert der Serie),
        stunden, n_messungen.
        """
        assert self._db is not None
        jetzt = jetzt or datetime.now()
        async with self._db.execute(
            """SELECT geraet_id, MAX(zeitstempel) AS letzte
               FROM sensor_messung
               WHERE zone_id = ? AND boden_feuchte IS NOT NULL
               GROUP BY geraet_id""",
            (zone_id,),
        ) as cursor:
            geraete = await cursor.fetchall()

        treffer: list[dict] = []
        for g in geraete:
            geraet_id = g["geraet_id"]
            # Letzter Wert > 0 -- alles danach ist die 0.0-Serie.
            async with self._db.execute(
                """SELECT MAX(zeitstempel) AS letzter_echt
                   FROM sensor_messung
                   WHERE zone_id = ? AND geraet_id = ? AND boden_feuchte > 0""",
                (zone_id, geraet_id),
            ) as cursor:
                zeile = await cursor.fetchone()
            letzter_echt = zeile["letzter_echt"] if zeile else None
            if letzter_echt is None:
                # Nie ein Wert > 0 -- kein Ausfall-BEFUND, sondern ein Sensor
                # ohne jede Historie. Nicht melden, sonst schlaegt jeder neu
                # angelegte Sensor sofort an.
                continue
            async with self._db.execute(
                """SELECT MIN(zeitstempel) AS seit, COUNT(*) AS n
                   FROM sensor_messung
                   WHERE zone_id = ? AND geraet_id = ? AND boden_feuchte = 0
                     AND zeitstempel > ?""",
                (zone_id, geraet_id, letzter_echt),
            ) as cursor:
                serie = await cursor.fetchone()
            if not serie or not serie["seit"] or not serie["n"]:
                continue
            seit = datetime.fromisoformat(serie["seit"])
            stunden = (jetzt - seit).total_seconds() / 3600.0
            if stunden >= mindest_stunden:
                treffer.append({
                    "geraet_id": geraet_id,
                    "seit": seit,
                    "stunden": stunden,
                    "n_messungen": serie["n"],
                })
        return treffer

    async def letzte_messungen_pro_geraet(
        self, zone_id: str, fenster_minuten: int = 360,
        jetzt: datetime | None = None,
    ) -> list[SensorMessung]:
        """T-0179c: Liefert pro geraet_id den juengsten Wert (fuer Diagnose-
        Anzeige im Frontend). Leere Liste wenn keine Messung im Fenster.

        T-0215 (2026-05-19): Default-Fenster von 90 auf 360 min (6 h)
        erhoeht. FYTA-Beam-Cadence ist 3-4 h -- mit 90 min-Fenster fielen
        FYTA-Sensoren regelmaessig aus der Diagnose-Liste raus
        (Realfall waldblumenhain 19.05. 20:00: 3 Sensoren in DB, nur 2
        angezeigt, weil ein FYTA-Sensor letzten Wert 17:21 hatte). 6 h ist
        ein Trade-off: zeigt alle aktiven Sensoren, blendet aber wirklich
        ausgefallene aus (Stale-Detector greift ab ~12 h via separater
        AUSFALL-Warnung).

        **Wichtig**: das Aggregations-Fenster fuer `letzte_messung_
        aggregiert` (Median fuer ML/Entscheidung) bleibt bei 90 min --
        dort wollen wir frische Werte, nicht stale FYTA-Werte mixed.
        """
        assert self._db is not None
        jetzt = jetzt or datetime.now()
        von = (jetzt - timedelta(minutes=fenster_minuten)).isoformat()
        async with self._db.execute(
            """SELECT * FROM sensor_messung
               WHERE zone_id = ? AND zeitstempel >= ?
               ORDER BY zeitstempel DESC""",
            (zone_id, von),
        ) as cursor:
            zeilen = await cursor.fetchall()
        seen: set[str] = set()
        ergebnis: list[SensorMessung] = []
        for z in zeilen:
            gid = z["geraet_id"] or ""
            if gid in seen:
                continue
            seen.add(gid)
            ergebnis.append(SensorMessung(
                zeitstempel=datetime.fromisoformat(z["zeitstempel"]),
                zone_id=z["zone_id"], geraet_id=z["geraet_id"],
                boden_feuchte=z["boden_feuchte"],
                boden_temperatur=z["boden_temperatur"],
                umgebungs_temperatur=z["umgebungs_temperatur"],
                licht_intensitaet=z["licht_intensitaet"],
                batterie_prozent=z["batterie_prozent"],
                boden_fruchtbarkeit=z["boden_fruchtbarkeit"],
                licht=z["licht"],
                quelle=DatenQuelle(z["quelle"]),
            ))
        return ergebnis

    async def letzte_messung_aggregiert_bulk(
        self,
        zone_ids: list[str],
        fenster_minuten: int = 90,
        jetzt: datetime | None = None,
    ) -> dict[str, SensorMessung | None]:
        """T-0200: Bulk-Variante von `letzte_messung_aggregiert` fuer den
        Dashboard-Snapshot. EINE SQL-Query holt alle Messungen aller
        Zonen im Fenster; pro Zone wird der gleiche Median-Algorithmus
        angewendet (Per-Geraet juengster Wert, dann Median).

        Vertrag identisch zur Per-Zone-Variante: Skalen-Mappings werden
        VOR der Median-Bildung angewendet, geraet_id="aggregat:<n>" bei
        Multi-Sensor-Zonen, Single-Sensor liefert die Original-Messung.

        Zonen ohne Messungen im Fenster bekommen `None` als Wert.
        """
        from statistics import median
        assert self._db is not None
        if not zone_ids:
            return {}
        jetzt = jetzt or datetime.now()
        von = (jetzt - timedelta(minutes=fenster_minuten)).isoformat()

        platzhalter = ",".join("?" for _ in zone_ids)
        async with self._db.execute(
            f"""SELECT * FROM sensor_messung
                WHERE zone_id IN ({platzhalter}) AND zeitstempel >= ?
                ORDER BY zeitstempel DESC""",
            (*zone_ids, von),
        ) as cursor:
            zeilen = await cursor.fetchall()

        mappings_alle = await self._hole_skalen_mappings_zonen(zone_ids)

        pro_zone: dict[str, dict[str, dict]] = {zid: {} for zid in zone_ids}
        for z in zeilen:
            gid = z["geraet_id"] or ""
            zone_pro_geraet = pro_zone[z["zone_id"]]
            if gid not in zone_pro_geraet:
                zone_pro_geraet[gid] = dict(z)

        ergebnis: dict[str, SensorMessung | None] = {}
        for zid in zone_ids:
            zone_pro_geraet = pro_zone[zid]
            if not zone_pro_geraet:
                ergebnis[zid] = None
                continue

            # T-0332: Aggregat-Lead (isomorph zur Per-Zone-Variante).
            zone_pro_geraet = self._wende_aggregat_lead_an(zid, zone_pro_geraet)
            # T-0384: Lead ohne Wert im Fenster -> keine Messung (isomorph).
            if not zone_pro_geraet:
                ergebnis[zid] = None
                continue

            mappings = mappings_alle.get(zid, {})
            if mappings:
                for z in zone_pro_geraet.values():
                    if z["boden_feuchte"] is None:
                        continue
                    m = mappings.get(z["quelle"])
                    if m is not None:
                        z["boden_feuchte"] = m["a"] * z["boden_feuchte"] + m["b"]

            if len(zone_pro_geraet) == 1:
                z = next(iter(zone_pro_geraet.values()))
                ergebnis[zid] = SensorMessung(
                    zeitstempel=datetime.fromisoformat(z["zeitstempel"]),
                    zone_id=z["zone_id"], geraet_id=z["geraet_id"],
                    boden_feuchte=z["boden_feuchte"],
                    boden_temperatur=z["boden_temperatur"],
                    umgebungs_temperatur=z["umgebungs_temperatur"],
                    licht_intensitaet=z["licht_intensitaet"],
                    batterie_prozent=z["batterie_prozent"],
                    boden_fruchtbarkeit=z["boden_fruchtbarkeit"],
                    licht=z["licht"],
                    quelle=DatenQuelle(z["quelle"]),
                )
                continue

            werte = list(zone_pro_geraet.values())

            def med(spalte: str, vals=werte) -> float | None:
                gefiltert = [v[spalte] for v in vals if v[spalte] is not None]
                return float(median(gefiltert)) if gefiltert else None

            neuester_ts = max(
                datetime.fromisoformat(v["zeitstempel"]) for v in werte
            )
            neueste = max(werte, key=lambda v: v["zeitstempel"])
            ergebnis[zid] = SensorMessung(
                zeitstempel=neuester_ts,
                zone_id=zid,
                geraet_id=f"aggregat:{len(zone_pro_geraet)}",
                boden_feuchte=med("boden_feuchte"),
                boden_temperatur=med("boden_temperatur"),
                umgebungs_temperatur=med("umgebungs_temperatur"),
                licht_intensitaet=med("licht_intensitaet"),
                batterie_prozent=med("batterie_prozent"),
                boden_fruchtbarkeit=med("boden_fruchtbarkeit"),
                licht=med("licht"),
                quelle=DatenQuelle(neueste["quelle"]),
            )
        return ergebnis

    async def letzte_messungen_pro_geraet_bulk(
        self,
        zone_ids: list[str],
        fenster_minuten: int = 360,
        jetzt: datetime | None = None,
    ) -> dict[str, list[SensorMessung]]:
        """T-0200: Bulk-Variante von `letzte_messungen_pro_geraet`.
        Pro `geraet_id` der juengste Wert je Zone, in einer SQL-Query.

        Jeder uebergebene zone_id bekommt einen Key im Result-Dict
        (leere Liste wenn keine Messungen im Fenster).

        T-0215: Default-Fenster 360 min (6 h), analog zur Per-Zone-
        Variante. Verhindert Drift zwischen Direkt-Endpoint und Bulk-
        Snapshot.
        """
        assert self._db is not None
        if not zone_ids:
            return {}
        jetzt = jetzt or datetime.now()
        von = (jetzt - timedelta(minutes=fenster_minuten)).isoformat()

        platzhalter = ",".join("?" for _ in zone_ids)
        async with self._db.execute(
            f"""SELECT * FROM sensor_messung
                WHERE zone_id IN ({platzhalter}) AND zeitstempel >= ?
                ORDER BY zeitstempel DESC""",
            (*zone_ids, von),
        ) as cursor:
            zeilen = await cursor.fetchall()

        ergebnis: dict[str, list[SensorMessung]] = {zid: [] for zid in zone_ids}
        gesehen_pro_zone: dict[str, set[str]] = {zid: set() for zid in zone_ids}
        for z in zeilen:
            zid = z["zone_id"]
            gid = z["geraet_id"] or ""
            if gid in gesehen_pro_zone[zid]:
                continue
            gesehen_pro_zone[zid].add(gid)
            ergebnis[zid].append(SensorMessung(
                zeitstempel=datetime.fromisoformat(z["zeitstempel"]),
                zone_id=z["zone_id"], geraet_id=z["geraet_id"],
                boden_feuchte=z["boden_feuchte"],
                boden_temperatur=z["boden_temperatur"],
                umgebungs_temperatur=z["umgebungs_temperatur"],
                licht_intensitaet=z["licht_intensitaet"],
                batterie_prozent=z["batterie_prozent"],
                boden_fruchtbarkeit=z["boden_fruchtbarkeit"],
                licht=z["licht"],
                quelle=DatenQuelle(z["quelle"]),
            ))
        return ergebnis

    async def letzte_messung(self, zone_id: str) -> SensorMessung | None:
        """Gibt die neueste Messung fuer eine Zone zurueck (LIMIT 1).

        **Hinweis T-0179c**: Bei Zonen mit mehreren Sensoren (z.B.
        Waldblumen mit Gardena + FYTA Terra) gibt diese Methode nur einen
        einzelnen Sensor-Wert zurueck — den juengsten. Fuer Bewaesserungs-
        und ML-Logik nutze `letzte_messung_aggregiert`. Diese Legacy-
        Methode bleibt fuer Stale-Check (sensor_health), Dedup-Init
        (fyta_client), Status-Logging (main.py) - alle drei brauchen
        explizit eine konkrete Messung, kein Aggregat.
        """
        assert self._db is not None
        async with self._db.execute(
            """SELECT * FROM sensor_messung
               WHERE zone_id = ?
               ORDER BY zeitstempel DESC
               LIMIT 1""",
            (zone_id,),
        ) as cursor:
            z = await cursor.fetchone()

        if not z:
            return None

        return SensorMessung(
            zeitstempel=datetime.fromisoformat(z["zeitstempel"]),
            zone_id=z["zone_id"],
            geraet_id=z["geraet_id"],
            boden_feuchte=z["boden_feuchte"],
            boden_temperatur=z["boden_temperatur"],
            umgebungs_temperatur=z["umgebungs_temperatur"],
            licht_intensitaet=z["licht_intensitaet"],
            batterie_prozent=z["batterie_prozent"],
            boden_fruchtbarkeit=z["boden_fruchtbarkeit"],
            licht=z["licht"],
            quelle=DatenQuelle(z["quelle"]),
        )

    async def letzte_messung_geraet(
        self, geraet_id: str,
    ) -> SensorMessung | None:
        """T-0224: Neueste Messung fuer ein konkretes Sensor-Geraet
        (`geraet_id`), ueber die gesamte Historie.

        Abgrenzung: `letzte_messung` arbeitet pro `zone_id`,
        `letzte_messungen_pro_geraet` pro Zone in einem 6h-Fenster.
        Diese Methode braucht der FYTA-Dedup-Init: eine Zone kann
        mehrere FYTA-Sensoren haben (waldblumenhain: 2x) -- die
        Dedup-Schwelle muss pro Sensor gefuehrt werden, sonst hungert
        der Sensor mit den spaeteren Zeitstempeln den anderen aus.
        """
        assert self._db is not None
        async with self._db.execute(
            """SELECT * FROM sensor_messung
               WHERE geraet_id = ?
               ORDER BY zeitstempel DESC
               LIMIT 1""",
            (geraet_id,),
        ) as cursor:
            z = await cursor.fetchone()

        if not z:
            return None

        return SensorMessung(
            zeitstempel=datetime.fromisoformat(z["zeitstempel"]),
            zone_id=z["zone_id"],
            geraet_id=z["geraet_id"],
            boden_feuchte=z["boden_feuchte"],
            boden_temperatur=z["boden_temperatur"],
            umgebungs_temperatur=z["umgebungs_temperatur"],
            licht_intensitaet=z["licht_intensitaet"],
            batterie_prozent=z["batterie_prozent"],
            boden_fruchtbarkeit=z["boden_fruchtbarkeit"],
            licht=z["licht"],
            quelle=DatenQuelle(z["quelle"]),
        )

    # --- Ventilereignisse ---

    async def speichere_ventil_ereignis(self, ereignis: VentilEreignis) -> None:
        """Speichert ein Ventilereignis.

        T-0408: `INSERT OR IGNORE` gegen den UNIQUE-Index
        `idx_ventil_ereignis_dedupe` (zone_id, ventil_id, aktion, zeitstempel)
        als LETZTE Verteidigungslinie gegen Duplikat-Stuerme -- die
        eigentlichen Guards sitzen bei den synthetisierenden Aufrufern
        (gardena_web_backfill, orphan_close_job). Bewusst OR IGNORE statt
        einer Exception: ein Duplikat ist hier kein Fehler, den ein Aufrufer
        behandeln koennte, sondern per Definition ein No-op -- und ein
        IntegrityError wuerde sonst Live-Pfade (WS-Callback, Watchdog-Close)
        zum Absturz bringen, die heute stillschweigend auf Erfolg bauen.
        """
        assert self._db is not None

        async def _op() -> None:
            await self._db.execute(
                """INSERT OR IGNORE INTO ventil_ereignis
                   (zeitstempel, zone_id, ventil_id, aktion, dauer_sekunden,
                    ausloser, liter, lauf_gruppe, phase)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ereignis.zeitstempel.isoformat(),
                    ereignis.zone_id,
                    ereignis.ventil_id,
                    ereignis.aktion.value,
                    ereignis.dauer_sekunden,
                    ereignis.ausloser.value,
                    ereignis.liter,
                    ereignis.lauf_gruppe,
                    ereignis.phase,
                ),
            )
            if not self._in_transaktion:
                await self._db.commit()

        await self._mit_lock_retry(_op, label="speichere_ventil_ereignis")

    async def letztes_ventil_ereignis(self, zone_id: str) -> VentilEreignis | None:
        """Gibt das letzte Ventilereignis fuer eine Zone zurueck."""
        assert self._db is not None
        async with self._db.execute(
            """SELECT * FROM ventil_ereignis
               WHERE zone_id = ?
               ORDER BY zeitstempel DESC LIMIT 1""",
            (zone_id,),
        ) as cursor:
            zeile = await cursor.fetchone()

        if not zeile:
            return None

        return _zeile_zu_ventil_ereignis(zeile)

    async def hole_ventil_ereignis(
        self, ereignis_id: int,
    ) -> VentilEreignis | None:
        """T-0205: Einzelnes Ventil-Event ueber Primary-Key lesen.
        Genutzt vom API-PATCH-Endpoint, um nach einem Flip die
        relevanten Zone-/Zeitstempel-Infos fuer den Heuristik-Rescan
        zu bekommen."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM ventil_ereignis WHERE id = ?",
            (ereignis_id,),
        ) as cursor:
            zeile = await cursor.fetchone()
        if not zeile:
            return None
        return _zeile_zu_ventil_ereignis(zeile)

    async def flippe_events_zu_ignoriert(
        self,
        zone_id: str,
        von: datetime,
        bis: datetime,
        ausloeser_in: tuple[str, ...] = ("manuell", "watchdog"),
    ) -> int:
        """T-0250: Bulk-Flip aller Events einer Zone im Zeitraum auf
        `ausloser='ignoriert'`. Genutzt vom AutoIgnorierenJob, wenn die
        Zone in einem `ml_ausschluss_fenster` mit
        `events_auto_ignorieren=True` liegt (z.B. Ventilkanal wird
        temporaer fuer Fremd-Zweck genutzt -- Hecke-Kanal beregnet
        Gras-Aussaat, nicht die Hecke selbst).

        Default-Filter: nur `manuell` + `watchdog`-Events flippen.
        `aquabloom`, `automatik`, `unbekannt` werden NICHT angefasst:
        - `automatik`/`unbekannt` sollte das Backend in einem aktiven
          Fenster eh nicht erzeugen (T-0211a Heuristik-Pause).
        - `aquabloom` ist eine eigene Quelle und hat eigene Semantik.
        - `ignoriert` ist schon der Ziel-Status, kein Re-Flip noetig.

        Returns: Anzahl tatsaechlich aktualisierter Zeilen.
        """
        assert self._db is not None
        if not ausloeser_in:
            return 0
        platzhalter = ",".join("?" for _ in ausloeser_in)
        sql = f"""
            UPDATE ventil_ereignis
            SET ausloser = 'ignoriert'
            WHERE zone_id = ?
              AND zeitstempel >= ?
              AND zeitstempel <= ?
              AND ausloser IN ({platzhalter})
        """
        params = (zone_id, von.isoformat(), bis.isoformat(), *ausloeser_in)

        async def _exec() -> int:
            async with self._db.execute(sql, params) as cursor:
                anzahl = cursor.rowcount
            if not self._in_transaktion:
                await self._db.commit()
            return anzahl

        return await self._mit_lock_retry(_exec, label="flippe_events_zu_ignoriert")

    async def hole_orphan_oeffnen(
        self,
        zone_id: str,
        cutoff: datetime,
        suchfenster: timedelta,
    ) -> list[VentilEreignis]:
        """T-0210: Liefert OEFFNEN-Events einer Zone, deren `cutoff` vor
        `jetzt - max_dauer - grace` liegt und die kein SCHLIESSEN-Paar
        innerhalb `suchfenster` Stunden danach in der DB haben.

        Schritt 1: alle OEFFNEN aelter als cutoff.
        Schritt 2: pro Kandidat pruefen ob ein SCHLIESSEN-Event in
                   (oeffnen_zeit, oeffnen_zeit + suchfenster) existiert.
        Schritt 3: zurueck nur die ohne Pendant.

        `cutoff` ist ein absoluter Zeitpunkt (von Aufrufer gerechnet
        aus jetzt - max_dauer - grace). `suchfenster` ist die Dauer
        ab OEFFNEN in der ein SCHLIESSEN noch gelten wuerde.
        """
        assert self._db is not None
        # 1) Alle OEFFNEN vor cutoff
        # F1/T-0301 + F21: Nur ECHTE Gardena-Ventil-Laeufe koennen verwaisen.
        # Giesskannen-Logs (ventil_id='manuell', /api/giessen) und Heuristik-
        # Pseudo-Paare (ventil_id='sensor_heuristik') sind Single-Event-
        # Vertraege OHNE Gardena-SCHLIESSEN -- fuer die darf KEIN synthetisches
        # WATCHDOG-SCHLIESSEN (max_dauer) entstehen, sonst vergiftet es
        # ML-Features/Budget/Bilanz/Leck. `ignoriert`-OEFFNEN hat der User
        # bewusst verworfen (kein Orphan-Resurrect).
        async with self._db.execute(
            """SELECT * FROM ventil_ereignis
               WHERE zone_id = ?
                 AND aktion = 'oeffnen'
                 AND zeitstempel <= ?
                 AND ventil_id NOT IN ('manuell', 'sensor_heuristik')
                 AND ausloser != 'ignoriert'
               ORDER BY zeitstempel ASC""",
            (zone_id, cutoff.isoformat()),
        ) as cursor:
            zeilen = await cursor.fetchall()

        orphans: list[VentilEreignis] = []
        for z in zeilen:
            oeffnen_ts = datetime.fromisoformat(z["zeitstempel"])
            bis_ts = oeffnen_ts + suchfenster
            # 2) gibt es ein SCHLIESSEN im Suchfenster?
            async with self._db.execute(
                """SELECT 1 FROM ventil_ereignis
                   WHERE zone_id = ?
                     AND aktion = 'schliessen'
                     AND zeitstempel > ?
                     AND zeitstempel <= ?
                   LIMIT 1""",
                (zone_id, oeffnen_ts.isoformat(), bis_ts.isoformat()),
            ) as c2:
                pendant = await c2.fetchone()
            if pendant is None:
                orphans.append(_zeile_zu_ventil_ereignis(z))
        return orphans

    async def existiert_schliessen_seit(
        self,
        ventil_id: str,
        seit: datetime,
        zone_ids: Iterable[str] | None = None,
    ) -> bool:
        """T-0331: True wenn fuer dieses Ventil bereits ein SCHLIESSEN seit
        `seit` (= Lauf-Start) in der DB liegt. Idempotenz-Guard gegen Doppel-
        Close: ein eingefrorener Watchdog-`call_later`-Timer feuert beim System-
        Wake nach und wuerde sonst einen zweiten, aufgeblaehten (Open->Wake)
        Close fuer einen laengst geschlossenen Puls schreiben (Realfall 25.06.)."""
        assert self._db is not None
        zonen = tuple(dict.fromkeys(zone_ids or ()))
        if zonen:
            platzhalter = ",".join("?" * len(zonen))
            async with self._db.execute(
                f"""SELECT COUNT(DISTINCT zone_id) FROM ventil_ereignis
                    WHERE ventil_id = ?
                      AND aktion = 'schliessen'
                      AND zeitstempel > ?
                      AND zone_id IN ({platzhalter})""",
                (ventil_id, seit.isoformat(), *zonen),
            ) as cursor:
                zeile = await cursor.fetchone()
            return bool(zeile and int(zeile[0]) >= len(zonen))
        async with self._db.execute(
            """SELECT 1 FROM ventil_ereignis
               WHERE ventil_id = ?
                 AND aktion = 'schliessen'
                 AND zeitstempel > ?
               LIMIT 1""",
            (ventil_id, seit.isoformat()),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def juengstes_aquabloom_event(
        self, zone_id: str,
    ) -> datetime | None:
        """T-0168: Letzter SCHLIESSEN-Aquabloom-Puls als Anker-Quelle
        fuer den AquabloomJob. Liefert None wenn die Zone noch nie
        AquaBloom-Events hatte (erster Lauf nach Konfig-Aktivierung).
        """
        assert self._db is not None
        async with self._db.execute(
            """SELECT zeitstempel FROM ventil_ereignis
               WHERE zone_id = ? AND ausloser = 'aquabloom'
                     AND aktion = 'schliessen'
               ORDER BY zeitstempel DESC LIMIT 1""",
            (zone_id,),
        ) as cursor:
            zeile = await cursor.fetchone()
        if not zeile:
            return None
        return datetime.fromisoformat(zeile[0])

    async def ventil_ereignisse_heute(self, zone_id: str) -> list[VentilEreignis]:
        """Gibt alle Ventilereignisse von heute fuer eine Zone zurueck."""
        heute_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return await self.hole_ventil_ereignisse(zone_id, von=heute_start)

    async def hole_ventil_ereignisse(
        self, zone_id: str,
        von: datetime | None = None,
        bis: datetime | None = None,
    ) -> list[VentilEreignis]:
        """Gibt Ventilereignisse fuer eine Zone im optionalen Zeitfenster zurueck.

        `bis` wurde in T-0058 (F6) nachgezogen, weil Auto-Betrieb im Loop
        ueber Monate stumm immer mehr Events laed — z. B. Leck-Detektor,
        Bilanz, DHS-Dedup-Fenster. Der Index `idx_ventil_zone_zeit` deckt
        beide Bedingungen ab.
        """
        assert self._db is not None
        bedingungen = ["zone_id = ?"]
        params: list = [zone_id]
        if von:
            bedingungen.append("zeitstempel >= ?")
            params.append(von.isoformat())
        if bis:
            bedingungen.append("zeitstempel <= ?")
            params.append(bis.isoformat())
        sql = (
            "SELECT * FROM ventil_ereignis WHERE "
            + " AND ".join(bedingungen)
            + " ORDER BY zeitstempel"
        )
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()

        return [_zeile_zu_ventil_ereignis(z) for z in zeilen]

    async def hole_offene_unbekannt_events(
        self,
        seit: datetime,
        zone_ids: list[str] | None = None,
        nur_juengster_pro_zone: bool = True,
    ) -> list[dict]:
        """T-0094: liefert ventil_ereignis-Eintraege mit ausloser='unbekannt'
        (= noch nicht klassifiziert), aktion='oeffnen' (das OEFFNEN ist
        der Anker fuer die Frage), zeitstempel >= `seit`.

        Wenn `nur_juengster_pro_zone=True` (Default): pro Zone den
        juengsten Eintrag. Das verhindert dass mehrere Spuenge derselben
        Zone separate iMessages erzeugen -- der User klassifiziert in
        der UI ohnehin oft mehrere auf einmal.

        Wenn `zone_ids` gesetzt: nur diese Zonen (z. B. nur Gardena-Zonen,
        FYTA-Indoor hat selten echte unklassifizierte Spuenge).
        """
        assert self._db is not None
        bedingungen = ["ausloser = 'unbekannt'", "aktion = 'oeffnen'",
                       "zeitstempel >= ?"]
        params: list = [seit.isoformat()]
        if zone_ids is not None:
            # Explizit-leere Liste = "keine Zone passt", explizit-None =
            # "alle Zonen". SQLite-Trick: IN (NULL) matched nichts.
            if not zone_ids:
                bedingungen.append("zone_id IN (NULL)")
            else:
                platzhalter = ",".join("?" * len(zone_ids))
                bedingungen.append(f"zone_id IN ({platzhalter})")
                params.extend(zone_ids)
        sql = (
            "SELECT id, zone_id, zeitstempel, aktion, dauer_sekunden, "
            "ventil_id FROM ventil_ereignis WHERE "
            + " AND ".join(bedingungen)
            + " ORDER BY zeitstempel DESC"
        )
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()

        eintraege = [dict(z) for z in zeilen]
        if not nur_juengster_pro_zone:
            return eintraege
        gesehen: set[str] = set()
        gefiltert: list[dict] = []
        for e in eintraege:
            zid = e["zone_id"]
            if zid in gesehen:
                continue
            gesehen.add(zid)
            gefiltert.append(e)
        return gefiltert

    async def max_ventil_zeitstempel(
        self, zone_id: str, ventil_id: str,
    ) -> datetime | None:
        """MAX(zeitstempel) fuer Events einer Zone mit bestimmtem `ventil_id`.

        Ersetzt den Pattern "hole alles, filtere in Python" — hilft dem
        Sensor-Backfill beim Bestimmen des letzten Heuristik-Events ohne
        Historien-Scan.
        """
        assert self._db is not None
        async with self._db.execute(
            """SELECT MAX(zeitstempel) AS ts
                 FROM ventil_ereignis
                WHERE zone_id = ? AND ventil_id = ?""",
            (zone_id, ventil_id),
        ) as cursor:
            zeile = await cursor.fetchone()
        if not zeile or not zeile[0]:
            return None
        return datetime.fromisoformat(zeile[0])

    async def letztes_bestaetigtes_ventil_ereignis(
        self, zone_id: str,
        ausloser_ausser: Ausloser | Iterable[Ausloser],
    ) -> VentilEreignis | None:
        """Juengstes Event dieser Zone, dessen Ausloser nicht in
        `ausloser_ausser` ist (akzeptiert Einzelwert oder Set/Liste).

        Ersetzt den Pattern "hole_ventil_ereignisse(zone_id) + Python-
        reversed-Scan nach Ausloser" aus entscheidung.py — SQL kann das
        mit ORDER BY + LIMIT 1 in O(log n) statt O(n) pro Zyklus.
        """
        assert self._db is not None
        if isinstance(ausloser_ausser, Ausloser):
            ausgeschlossen = (ausloser_ausser.value,)
        else:
            ausgeschlossen = tuple(a.value for a in ausloser_ausser)
        if not ausgeschlossen:
            return None
        platzhalter = ",".join("?" * len(ausgeschlossen))
        async with self._db.execute(
            f"""SELECT * FROM ventil_ereignis
                WHERE zone_id = ? AND ausloser NOT IN ({platzhalter})
                ORDER BY zeitstempel DESC LIMIT 1""",
            (zone_id, *ausgeschlossen),
        ) as cursor:
            zeile = await cursor.fetchone()
        if not zeile:
            return None
        return _zeile_zu_ventil_ereignis(zeile)

    async def letzter_bestaetigter_wasser_anker(
        self, zone_id: str,
        ausloser_ausser: Ausloser | Iterable[Ausloser],
    ) -> VentilEreignis | None:
        """Juengster Pause-Anker, der echte Wasserzufuhr repraesentiert.

        T-0363: Ein erfolgreicher Pre-Soak-Puls darf die volle `min_pause`
        nicht blockieren, wenn die Hauptdose nie startete. Solche reinen
        `phase='pre_soak'`-Gruppen werden uebersprungen, solange in derselben
        `lauf_gruppe` kein `phase='haupt'`-Event existiert.
        """
        assert self._db is not None
        if isinstance(ausloser_ausser, Ausloser):
            ausgeschlossen = (ausloser_ausser.value,)
        else:
            ausgeschlossen = tuple(a.value for a in ausloser_ausser)
        if not ausgeschlossen:
            return None
        platzhalter = ",".join("?" * len(ausgeschlossen))
        sql = f"""SELECT * FROM ventil_ereignis AS v
                  WHERE v.zone_id = ?
                    AND v.ausloser NOT IN ({platzhalter})
                    AND NOT (
                      v.phase = 'pre_soak'
                      AND v.lauf_gruppe IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM ventil_ereignis AS h
                        WHERE h.zone_id = v.zone_id
                          AND h.lauf_gruppe = v.lauf_gruppe
                          AND h.phase = 'haupt'
                          AND h.ausloser NOT IN ({platzhalter})
                      )
                    )
                  ORDER BY v.zeitstempel DESC LIMIT 1"""
        params = (zone_id, *ausgeschlossen, *ausgeschlossen)
        async with self._db.execute(sql, params) as cursor:
            zeile = await cursor.fetchone()
        if not zeile:
            return None
        return _zeile_zu_ventil_ereignis(zeile)

    async def hole_ventil_ereignisse_fenster(
        self, von: datetime, bis: datetime,
        zone_ids: list[str] | None = None,
    ) -> list[VentilEreignis]:
        """Alle Events im Zeitraum, optional gefiltert auf Zonen (fuer Ops-Tab-UI)."""
        assert self._db is not None
        bedingungen = ["zeitstempel >= ?", "zeitstempel <= ?"]
        params: list = [von.isoformat(), bis.isoformat()]
        if zone_ids:
            platzhalter = ",".join("?" * len(zone_ids))
            bedingungen.append(f"zone_id IN ({platzhalter})")
            params.extend(zone_ids)
        sql = (
            "SELECT * FROM ventil_ereignis WHERE "
            + " AND ".join(bedingungen)
            + " ORDER BY zeitstempel"
        )
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()
        return [_zeile_zu_ventil_ereignis(z) for z in zeilen]

    async def aktualisiere_ventil_ereignis(
        self, ereignis_id: int,
        ausloser: Ausloser | None = None,
        zeitstempel: datetime | None = None,
        liter: float | None = None,
        dauer_sekunden: int | None = None,
        jetzt: datetime | None = None,
    ) -> bool:
        """Patch einer oder mehrerer Felder. True wenn eine Zeile geaendert wurde.

        Bei Aenderung des Ausloeser-Werts wird zusaetzlich `ausloser_korrektur`
        als JSON-Audit `{alt, neu, geaendert_am}` mitgeschrieben (T-0084).
        """
        assert self._db is not None
        felder: list[str] = []
        params: list = []
        # Audit nur wenn Ausloser tatsaechlich wechselt; alten Wert vorab lesen.
        if ausloser is not None:
            async with self._db.execute(
                "SELECT ausloser FROM ventil_ereignis WHERE id = ?",
                (ereignis_id,),
            ) as cursor:
                alt_zeile = await cursor.fetchone()
            if alt_zeile and alt_zeile["ausloser"] != ausloser.value:
                audit = json.dumps({
                    "alt": alt_zeile["ausloser"],
                    "neu": ausloser.value,
                    "geaendert_am": (jetzt or datetime.now()).isoformat(),
                }, ensure_ascii=False)
                felder.append("ausloser_korrektur = ?")
                params.append(audit)
            felder.append("ausloser = ?")
            params.append(ausloser.value)
        if zeitstempel is not None:
            felder.append("zeitstempel = ?")
            params.append(zeitstempel.isoformat())
        if liter is not None:
            felder.append("liter = ?")
            params.append(liter)
        if dauer_sekunden is not None:
            felder.append("dauer_sekunden = ?")
            params.append(dauer_sekunden)
        if not felder:
            return False
        params.append(ereignis_id)
        rowcount = 0

        async def _op() -> None:
            nonlocal rowcount
            cursor = await self._db.execute(
                f"UPDATE ventil_ereignis SET {', '.join(felder)} WHERE id = ?",
                params,
            )
            if not self._in_transaktion:
                await self._db.commit()
            rowcount = cursor.rowcount

        await self._mit_lock_retry(_op, label="aktualisiere_ventil_ereignis")
        return rowcount > 0

    async def loesche_ventil_ereignis(self, ereignis_id: int) -> bool:
        """Loescht einen Eintrag per Primary-Key. True wenn Zeile betroffen."""
        assert self._db is not None
        cursor = await self._db.execute(
            "DELETE FROM ventil_ereignis WHERE id = ?", (ereignis_id,),
        )
        if not self._in_transaktion:
            await self._db.commit()
        return cursor.rowcount > 0

    # T-0296: Paarung ueber Start-Anker statt starrem Zeitfenster.
    # Frueher (T-0114) ein ±900s-Fenster -- das verfehlt aber Laeufe > 15 min
    # (z.B. waldblumenhain 1260s): OEFFNEN/SCHLIESSEN fielen aus dem Fenster,
    # Bulk-Klassifikation verwaiste die SCHLIESSEN-Haelfte. Isomorph zu T-0283
    # (Dedup-Fenster < Event-Dauer). Jetzt: ein OEFFNEN (Start-Anker = sein
    # Zeitstempel) und ein SCHLIESSEN (Start-Anker = Zeitstempel - dauer)
    # gehoeren zusammen, wenn ihre Start-Anker innerhalb der Toleranz liegen.
    PAAR_ANKER_TOLERANZ_SEKUNDEN = 300   # Jitter/Rundung Start<->Stop, << Lauf-Abstand
    PAAR_SUCH_FENSTER_SEKUNDEN = 43200   # 12 h Scan-Grenze (deckt auch lange Manuell-Laeufe)

    async def finde_ventil_paar(self, ereignis_id: int) -> list[int]:
        """Findet das OEFFNEN+SCHLIESSEN-Paar eines Heuristik-/Backfill-Events.

        Paarung ueber den impliziten Start-Anker, NICHT ueber ein starres
        Zeitfenster: das SCHLIESSEN traegt die echte `dauer_sekunden`, sein
        Start ist also `zeitstempel - dauer`. Ein OEFFNEN (Anker = sein
        Zeitstempel) und ein SCHLIESSEN gehoeren zusammen, wenn ihre
        Start-Anker innerhalb PAAR_ANKER_TOLERANZ_SEKUNDEN liegen -- bei
        gleicher zone_id + ventil_id, anderer aktion. Multi-Zyklus-sicher:
        jeder Lauf hat seinen eigenen Anker, auch bei stuendlicher Cadence.

        Gibt [eigene_id, partner_id] zurueck (oder nur [eigene_id] ohne
        Partner; [] wenn das Event selbst fehlt).

        T-0296: ersetzt das fruehere ±900s-Fenster (T-0114), das Laeufe
        > 15 min (z.B. 1260s) verfehlte -> nach Bulk-Klassifikation blieb das
        SCHLIESSEN verwaist 'unbekannt' und im Banner unsichtbar. Genutzt von
        PATCH/DELETE/Bulk mit `?paar=true` aus Ops-Tab/Banner (T-0055-B2).
        """
        assert self._db is not None
        async with self._db.execute(
            "SELECT zone_id, ventil_id, zeitstempel, aktion, dauer_sekunden "
            "FROM ventil_ereignis WHERE id = ?",
            (ereignis_id,),
        ) as cursor:
            zeile = await cursor.fetchone()
        if not zeile:
            return []
        zone = zeile["zone_id"]
        ventil = zeile["ventil_id"]
        zeit = datetime.fromisoformat(zeile["zeitstempel"])
        # Start-Anker des gegebenen Events (OEFFNEN: dauer per Vertrag 0).
        if zeile["aktion"] == "oeffnen":
            anker = zeit
            andere_aktion = "schliessen"
        else:
            anker = zeit - timedelta(seconds=zeile["dauer_sekunden"] or 0)
            andere_aktion = "oeffnen"
        # Gegen-Aktion grob via Scan-Fenster eingrenzen, dann ueber den
        # Start-Anker exakt das eine zugehoerige Event waehlen.
        fenster_von = (anker - timedelta(seconds=self.PAAR_SUCH_FENSTER_SEKUNDEN)).isoformat()
        fenster_bis = (anker + timedelta(seconds=self.PAAR_SUCH_FENSTER_SEKUNDEN)).isoformat()
        async with self._db.execute(
            """SELECT id, zeitstempel, dauer_sekunden FROM ventil_ereignis
               WHERE zone_id = ? AND ventil_id = ?
                 AND aktion = ?
                 AND zeitstempel >= ? AND zeitstempel <= ?""",
            (zone, ventil, andere_aktion, fenster_von, fenster_bis),
        ) as cursor:
            kandidaten = await cursor.fetchall()
        bester_id: int | None = None
        beste_abw = timedelta(seconds=self.PAAR_ANKER_TOLERANZ_SEKUNDEN)
        for k in kandidaten:
            k_zeit = datetime.fromisoformat(k["zeitstempel"])
            if andere_aktion == "oeffnen":
                k_anker = k_zeit
            else:
                k_anker = k_zeit - timedelta(seconds=k["dauer_sekunden"] or 0)
            abw = abs(k_anker - anker)
            if abw <= beste_abw:
                beste_abw = abw
                bester_id = k["id"]
        if bester_id is None:
            return [ereignis_id]
        return [ereignis_id, bester_id]

    async def heile_verwaiste_paar_klassifikation(
        self, jetzt: datetime | None = None,
    ) -> int:
        """T-0296: Einmal-Reparatur verwaister SCHLIESSEN-Klassifikation.

        Folge des frueheren ±900s-Paarungs-Bugs: bei Laeufen > 15 min flippte
        die Bulk-Klassifikation nur das OEFFNEN auf den Ziel-Ausloeser, das
        SCHLIESSEN blieb 'unbekannt' (verwaist) und damit im Banner
        unsichtbar. Gleicht jedes verwaiste SCHLIESSEN an den (jetzt korrekt
        gepaarten) OEFFNEN-Ausloeser an.

        Idempotent: heilt nur SCHLIESSEN mit ausloser='unbekannt', deren
        gepaartes OEFFNEN bereits klassifiziert ist (!= 'unbekannt'). Echte
        ungeklasste Paare (beide 'unbekannt') bleiben unberuehrt. Gibt die
        Anzahl geheilter SCHLIESSEN zurueck.
        """
        assert self._db is not None
        async with self._db.execute(
            "SELECT id FROM ventil_ereignis "
            "WHERE aktion = 'schliessen' AND ausloser = ?",
            (Ausloser.UNBEKANNT.value,),
        ) as cursor:
            kandidaten = [z["id"] for z in await cursor.fetchall()]
        geheilt = 0
        for sid in kandidaten:
            paar = await self.finde_ventil_paar(sid)
            partner_ids = [pid for pid in paar if pid != sid]
            if not partner_ids:
                continue
            async with self._db.execute(
                "SELECT ausloser FROM ventil_ereignis WHERE id = ?",
                (partner_ids[0],),
            ) as cursor:
                pz = await cursor.fetchone()
            if pz is None or pz["ausloser"] == Ausloser.UNBEKANNT.value:
                continue  # OEFFNEN auch unbekannt -> echtes pending Paar
            await self.aktualisiere_ventil_ereignis(
                sid, ausloser=Ausloser(pz["ausloser"]), jetzt=jetzt,
            )
            geheilt += 1
        return geheilt

    # --- Entscheidungen ---

    async def speichere_entscheidung(self, entscheidung: BewaesserungsEntscheidung) -> None:
        """Speichert eine Bewaesserungsentscheidung im Log."""
        assert self._db is not None

        async def _op() -> None:
            await self._db.execute(
                """INSERT INTO entscheidung_log
                   (zeitstempel, zone_id, soll_bewaessern, dauer_sekunden, begruendung,
                    blocker_typ, scope, scope_ref)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entscheidung.zeitstempel.isoformat(),
                    entscheidung.zone_id,
                    1 if entscheidung.soll_bewaessern else 0,
                    entscheidung.dauer_sekunden,
                    entscheidung.begruendung,
                    entscheidung.blocker_typ.value if entscheidung.blocker_typ else None,
                    entscheidung.scope.value,
                    entscheidung.scope_ref,
                ),
            )
            if not self._in_transaktion:
                await self._db.commit()

        await self._mit_lock_retry(_op, label="speichere_entscheidung")

    async def hole_entscheidungen(
        self,
        zone_id: str | None = None,
        limit: int = 50,
        von: datetime | None = None,
        bis: datetime | None = None,
        blocker_typ: str | None = None,
    ) -> list[BewaesserungsEntscheidung]:
        """Liest die letzten Entscheidungen aus dem Log.

        `von`/`bis` filtern nach `zeitstempel` (inklusive). `blocker_typ`
        filtert nach strukturiertem Blocker (siehe `BlockerTyp`-Enum).
        Wenn alle Filter None sind, liefert der Aufruf die `limit` letzten
        Entscheidungen (Backward-Kompat).
        """
        assert self._db is not None
        bedingungen: list[str] = []
        params: list = []
        if zone_id:
            bedingungen.append("zone_id = ?")
            params.append(zone_id)
        if von is not None:
            bedingungen.append("zeitstempel >= ?")
            params.append(von.isoformat())
        if bis is not None:
            bedingungen.append("zeitstempel <= ?")
            params.append(bis.isoformat())
        if blocker_typ is not None:
            bedingungen.append("blocker_typ = ?")
            params.append(blocker_typ)
        where = f"WHERE {' AND '.join(bedingungen)}" if bedingungen else ""
        sql = (
            f"SELECT * FROM entscheidung_log {where} "
            "ORDER BY zeitstempel DESC LIMIT ?"
        )
        params.append(limit)

        async with self._db.execute(sql, tuple(params)) as cursor:
            zeilen = await cursor.fetchall()

        return [
            BewaesserungsEntscheidung(
                zeitstempel=datetime.fromisoformat(z["zeitstempel"]),
                zone_id=z["zone_id"],
                soll_bewaessern=bool(z["soll_bewaessern"]),
                dauer_sekunden=z["dauer_sekunden"],
                begruendung=z["begruendung"] or "",
                blocker_typ=BlockerTyp(z["blocker_typ"]) if z["blocker_typ"] else None,
                scope=EntscheidungsScope(z["scope"] or "zone"),
                scope_ref=z["scope_ref"] or z["zone_id"],
            )
            for z in zeilen
        ]

    # --- Wetter-Ereignisse ---

    async def speichere_wetter_ereignis(self, ereignis: WetterEreignis) -> None:
        """Speichert ein Wetter-Ereignis (Frost, Hitze, Starkregen)."""
        assert self._db is not None

        async def _op() -> None:
            await self._db.execute(
                """INSERT INTO wetter_ereignis
                   (zeitstempel, typ, standort_id, details, beginn, ende)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    ereignis.zeitstempel.isoformat(),
                    ereignis.typ.value,
                    ereignis.standort_id,
                    ereignis.details,
                    ereignis.beginn.isoformat() if ereignis.beginn else None,
                    ereignis.ende.isoformat() if ereignis.ende else None,
                ),
            )
            if not self._in_transaktion:
                await self._db.commit()

        await self._mit_lock_retry(_op, label="speichere_wetter_ereignis")

    async def wetter_ereignis_existiert(
        self, typ: WetterEreignisTyp, standort_id: str, seit: datetime
    ) -> bool:
        """Prueft ob ein Wetter-Ereignis dieses Typs seit dem Zeitpunkt schon gespeichert ist."""
        assert self._db is not None
        async with self._db.execute(
            """SELECT COUNT(*) FROM wetter_ereignis
               WHERE typ = ? AND standort_id = ? AND zeitstempel >= ?""",
            (typ.value, standort_id, seit.isoformat()),
        ) as cursor:
            zeile = await cursor.fetchone()
        return (zeile[0] or 0) > 0

    async def hole_wetter_ereignisse(
        self,
        von: datetime | None = None,
        standort_id: str | None = None,
    ) -> list[WetterEreignis]:
        """Liest Wetter-Ereignisse fuer Ops-Timeline."""
        assert self._db is not None
        bedingungen: list[str] = []
        params: list[str] = []

        if von:
            bedingungen.append("zeitstempel >= ?")
            params.append(von.isoformat())
        if standort_id:
            bedingungen.append("standort_id = ?")
            params.append(standort_id)

        sql = """SELECT * FROM wetter_ereignis"""
        if bedingungen:
            sql += f" WHERE {' AND '.join(bedingungen)}"
        sql += " ORDER BY zeitstempel DESC"

        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()

        return [
            WetterEreignis(
                zeitstempel=datetime.fromisoformat(z["zeitstempel"]),
                typ=WetterEreignisTyp(z["typ"]),
                standort_id=z["standort_id"],
                details=z["details"] or "",
                beginn=datetime.fromisoformat(z["beginn"]) if z["beginn"] else None,
                ende=datetime.fromisoformat(z["ende"]) if z["ende"] else None,
            )
            for z in zeilen
        ]

    # --- Sensor-Warnungen ---

    async def oeffne_sensor_warnung(self, warnung: SensorWarnung) -> bool:
        """Oeffnet eine neue Sensorwarnung, falls noch keine gleiche offen ist."""
        assert self._db is not None
        async with self._db.execute(
            """SELECT id FROM sensor_warnung
               WHERE zone_id = ? AND typ = ? AND behoben_um IS NULL
               LIMIT 1""",
            (warnung.zone_id, warnung.typ.value),
        ) as cursor:
            offen = await cursor.fetchone()

        if offen:
            return False

        async def _op() -> None:
            await self._db.execute(
                """INSERT INTO sensor_warnung
                   (zeitstempel, zone_id, typ, details, behoben_um)
                   VALUES (?, ?, ?, ?, NULL)""",
                (
                    warnung.zeitstempel.isoformat(),
                    warnung.zone_id,
                    warnung.typ.value,
                    warnung.details,
                ),
            )
            if not self._in_transaktion:
                await self._db.commit()

        await self._mit_lock_retry(_op, label="oeffne_sensor_warnung")
        return True

    async def aktualisiere_offene_warn_details(
        self, zone_id: str, typ: SensorWarnungTyp, details: str,
    ) -> bool:
        """T-0397 (F4): `details` einer bereits OFFENEN Warnung auffrischen.

        `oeffne_sensor_warnung` legt eine Warnung genau einmal an und laesst
        `details` danach unveraendert -- eine Ausfall-Warnung behielt so die
        "vor Xh"-Angabe vom Erstellzeitpunkt und zeigte tagelang denselben zu
        kleinen Wert. `sensor_health` ruft dies bei jedem Tick, solange die
        Warnung offen ist. `zeitstempel` (Erstellzeit) + `behoben_um` bleiben
        unberuehrt -- nur der Text wird aktuell gehalten.
        """
        assert self._db is not None

        async def _op() -> None:
            await self._db.execute(
                """UPDATE sensor_warnung SET details = ?
                   WHERE zone_id = ? AND typ = ? AND behoben_um IS NULL""",
                (details, zone_id, typ.value),
            )
            if not self._in_transaktion:
                await self._db.commit()

        await self._mit_lock_retry(_op, label="aktualisiere_offene_warn_details")
        return True

    async def schliesse_sensor_warnung(
        self,
        zone_id: str,
        typ: SensorWarnungTyp,
        behoben_um: datetime | None = None,
    ) -> int:
        """Schliesst offene Sensorwarnungen eines Typs fuer eine Zone."""
        assert self._db is not None
        zeitpunkt = (behoben_um or datetime.now()).isoformat()
        rowcount = 0

        async def _op() -> None:
            nonlocal rowcount
            cursor = await self._db.execute(
                """UPDATE sensor_warnung
                   SET behoben_um = ?
                   WHERE zone_id = ? AND typ = ? AND behoben_um IS NULL""",
                (zeitpunkt, zone_id, typ.value),
            )
            if not self._in_transaktion:
                await self._db.commit()
            rowcount = cursor.rowcount

        await self._mit_lock_retry(_op, label="schliesse_sensor_warnung")
        return rowcount

    async def offene_sensor_warnungen(
        self, zone_id: str | None = None
    ) -> list[SensorWarnung]:
        """Liest aktuell offene Sensorwarnungen."""
        assert self._db is not None
        if zone_id:
            sql = """SELECT * FROM sensor_warnung
                     WHERE zone_id = ? AND behoben_um IS NULL
                     ORDER BY zeitstempel DESC"""
            params: tuple = (zone_id,)
        else:
            sql = """SELECT * FROM sensor_warnung
                     WHERE behoben_um IS NULL
                     ORDER BY zeitstempel DESC"""
            params = ()

        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()

        return [self._sensor_warnung_aus_zeile(z) for z in zeilen]

    async def hole_sensor_warnungen(
        self,
        von: datetime | None = None,
        zone_id: str | None = None,
    ) -> list[SensorWarnung]:
        """Liest Sensorwarnungen fuer Ops-Timeline."""
        assert self._db is not None
        bedingungen: list[str] = []
        params: list[str] = []
        if von:
            bedingungen.append("zeitstempel >= ?")
            params.append(von.isoformat())
        if zone_id:
            bedingungen.append("zone_id = ?")
            params.append(zone_id)

        sql = """SELECT * FROM sensor_warnung"""
        if bedingungen:
            sql += f" WHERE {' AND '.join(bedingungen)}"
        sql += " ORDER BY zeitstempel DESC"

        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()

        return [self._sensor_warnung_aus_zeile(z) for z in zeilen]

    @staticmethod
    def _sensor_warnung_aus_zeile(zeile: aiosqlite.Row) -> SensorWarnung:
        return SensorWarnung(
            id=zeile["id"],
            zeitstempel=datetime.fromisoformat(zeile["zeitstempel"]),
            zone_id=zeile["zone_id"],
            typ=SensorWarnungTyp(zeile["typ"]),
            details=zeile["details"] or "",
            behoben_um=datetime.fromisoformat(zeile["behoben_um"]) if zeile["behoben_um"] else None,
        )

    # --- Ops-Queries ---

    async def hole_ops_summary(self, jetzt: datetime | None = None) -> dict:
        """Aggregierte Kennzahlen fuer den Ops-Tab."""
        assert self._db is not None
        ende = jetzt or datetime.now()
        heute_start = ende.replace(hour=0, minute=0, second=0, microsecond=0)

        async with self._db.execute(
            """SELECT COUNT(*) AS anzahl
               FROM ventil_ereignis
               WHERE zeitstempel >= ?
                 AND (
                   aktion = 'schliessen'
                   OR (aktion = 'oeffnen' AND dauer_sekunden > 0)
                 )""",
            (heute_start.isoformat(),),
        ) as cursor:
            bewaesserungen_heute = int((await cursor.fetchone())["anzahl"] or 0)

        async with self._db.execute(
            """SELECT COUNT(*) AS anzahl
               FROM entscheidung_log
               WHERE zeitstempel >= ?
                 AND scope = ?
                 AND soll_bewaessern = 1""",
            (heute_start.isoformat(), EntscheidungsScope.KANAL.value),
        ) as cursor:
            shadow_vorschlaege_heute = int((await cursor.fetchone())["anzahl"] or 0)

        async with self._db.execute(
            """SELECT blocker_typ, COUNT(*) AS anzahl
               FROM entscheidung_log
               WHERE zeitstempel >= ?
                 AND scope = ?
                 AND soll_bewaessern = 0
                 AND blocker_typ IS NOT NULL
               GROUP BY blocker_typ""",
            (heute_start.isoformat(), EntscheidungsScope.KANAL.value),
        ) as cursor:
            blocker_zeilen = await cursor.fetchall()

        blocker_verteilung = {
            z["blocker_typ"]: int(z["anzahl"] or 0)
            for z in blocker_zeilen
        }

        async with self._db.execute(
            """SELECT COUNT(*) AS anzahl
               FROM wetter_ereignis
               WHERE zeitstempel >= ?""",
            (heute_start.isoformat(),),
        ) as cursor:
            wetter_warnungen = int((await cursor.fetchone())["anzahl"] or 0)

        async with self._db.execute(
            """SELECT COUNT(*) AS anzahl
               FROM sensor_warnung
               WHERE behoben_um IS NULL"""
        ) as cursor:
            sensor_warnungen = int((await cursor.fetchone())["anzahl"] or 0)

        return {
            "zeitraum": {
                "von": heute_start.isoformat(),
                "bis": ende.isoformat(),
            },
            "bewaesserungen_heute": bewaesserungen_heute,
            "shadow_vorschlaege_heute": shadow_vorschlaege_heute,
            "blocker_verteilung": blocker_verteilung,
            "wetter_warnungen": wetter_warnungen,
            "sensor_warnungen": sensor_warnungen,
        }

    async def hole_ops_timeline(self, von: datetime) -> dict[str, list[dict]]:
        """Laedt rohe Datenquellen fuer die Ops-Timeline."""
        assert self._db is not None

        # T-0388: BEIDE Scopes laden. Vorher nur KANAL -- aber `pruefe_kanal`
        # laeuft seit T-0334 nur fuer opt-in-Zonen, d.h. die einzige echte
        # Shadow-Zone (waldblumenhain) erzeugte gar keine KANAL-Zeile und war
        # im Shadow-Feed unsichtbar (0 KANAL-Zeilen seit 26.06.), waehrend die
        # scharfen Zonen dort konjunktivisch als "wuerde bewaessern" standen.
        # Dedupe (Zone-Zeile scharfer Zonen == Duplikat ihrer KANAL-Zeile) macht
        # der Konsument, der die scharf/Shadow-Wahrheit kennt
        # (api_server._ist_autonom_scharf). ROUTINE-Zeilen werden dort pro
        # (blocker, scope_ref, Stunde) aggregiert und sind nicht im Default-
        # Severity-Set -- kein Flut-Risiko.
        async with self._db.execute(
            """SELECT * FROM entscheidung_log
               WHERE zeitstempel >= ?
               ORDER BY zeitstempel DESC""",
            (von.isoformat(),),
        ) as cursor:
            entscheidungs_zeilen = await cursor.fetchall()

        async with self._db.execute(
            """SELECT * FROM ventil_ereignis
               WHERE zeitstempel >= ?
                 AND (
                   aktion = 'schliessen'
                   OR (aktion = 'oeffnen' AND dauer_sekunden > 0)
                 )
               ORDER BY zeitstempel DESC""",
            (von.isoformat(),),
        ) as cursor:
            ventil_zeilen = await cursor.fetchall()

        async with self._db.execute(
            """SELECT * FROM wetter_ereignis
               WHERE zeitstempel >= ?
               ORDER BY zeitstempel DESC""",
            (von.isoformat(),),
        ) as cursor:
            wetter_zeilen = await cursor.fetchall()

        async with self._db.execute(
            """SELECT * FROM sensor_warnung
               WHERE zeitstempel >= ?
                  OR behoben_um IS NULL
               ORDER BY zeitstempel DESC""",
            (von.isoformat(),),
        ) as cursor:
            sensor_zeilen = await cursor.fetchall()

        return {
            "entscheidungen": [
                {
                    "id": z["id"],
                    "zeitstempel": z["zeitstempel"],
                    "zone_id": z["zone_id"],
                    "soll_bewaessern": bool(z["soll_bewaessern"]),
                    "dauer_sekunden": z["dauer_sekunden"],
                    "begruendung": z["begruendung"] or "",
                    "blocker_typ": z["blocker_typ"],
                    "scope": z["scope"] or EntscheidungsScope.ZONE.value,
                    "scope_ref": z["scope_ref"] or z["zone_id"],
                }
                for z in entscheidungs_zeilen
            ],
            "ventil_ereignisse": [
                {
                    "id": z["id"],
                    "zeitstempel": z["zeitstempel"],
                    "zone_id": z["zone_id"],
                    "ventil_id": z["ventil_id"],
                    "aktion": z["aktion"],
                    "dauer_sekunden": z["dauer_sekunden"],
                    "ausloser": z["ausloser"],
                    # Defensive: Spalte erst per Migration angelegt (T-0084).
                    # Falls altes Schema (Backend ohne Restart), None liefern.
                    "ausloser_korrektur": (
                        z["ausloser_korrektur"]
                        if "ausloser_korrektur" in z.keys() else None
                    ),
                }
                for z in ventil_zeilen
            ],
            "wetter_ereignisse": [
                {
                    "id": z["id"],
                    "zeitstempel": z["zeitstempel"],
                    "typ": z["typ"],
                    "standort_id": z["standort_id"],
                    "details": z["details"] or "",
                    "beginn": z["beginn"],
                    "ende": z["ende"],
                }
                for z in wetter_zeilen
            ],
            "sensor_warnungen": [
                {
                    "id": z["id"],
                    "zeitstempel": z["zeitstempel"],
                    "zone_id": z["zone_id"],
                    "typ": z["typ"],
                    "details": z["details"] or "",
                    "behoben_um": z["behoben_um"],
                }
                for z in sensor_zeilen
            ],
        }

    # --- Bulk-Queries (ML Feature Engineering) ---

    async def hole_alle_messungen(
        self, von: datetime, bis: datetime,
    ) -> list[SensorMessung]:
        """Liest Sensormessungen fuer ALLE Zonen in einem Zeitraum (ML-Bulk)."""
        assert self._db is not None
        sql = """SELECT * FROM sensor_messung
                 WHERE zeitstempel >= ? AND zeitstempel <= ?
                 ORDER BY zone_id, zeitstempel"""
        async with self._db.execute(sql, (von.isoformat(), bis.isoformat())) as cursor:
            zeilen = await cursor.fetchall()
        return [
            SensorMessung(
                zeitstempel=datetime.fromisoformat(z["zeitstempel"]),
                zone_id=z["zone_id"],
                geraet_id=z["geraet_id"],
                boden_feuchte=z["boden_feuchte"],
                boden_temperatur=z["boden_temperatur"],
                umgebungs_temperatur=z["umgebungs_temperatur"],
                licht_intensitaet=z["licht_intensitaet"],
                batterie_prozent=z["batterie_prozent"],
                boden_fruchtbarkeit=z["boden_fruchtbarkeit"],
                licht=z["licht"],
                quelle=DatenQuelle(z["quelle"]),
            )
            for z in zeilen
        ]

    async def hole_alle_ventil_ereignisse(
        self, von: datetime, bis: datetime,
    ) -> list[VentilEreignis]:
        """Liest Ventilereignisse fuer ALLE Zonen in einem Zeitraum (ML-Bulk)."""
        assert self._db is not None
        sql = """SELECT * FROM ventil_ereignis
                 WHERE zeitstempel >= ? AND zeitstempel <= ?
                 ORDER BY zone_id, zeitstempel"""
        async with self._db.execute(sql, (von.isoformat(), bis.isoformat())) as cursor:
            zeilen = await cursor.fetchall()
        return [_zeile_zu_ventil_ereignis(z) for z in zeilen]

    async def hole_forecast_stunden(
        self, standort_id: str, von: datetime, bis: datetime,
    ) -> dict[datetime, float]:
        """Liefert {vorhersage_zeitstempel -> (niederschlag_mm, et0_mm)} aus der
        juengsten Forecast-Abfrage, die zum Zeitpunkt `bis` verfuegbar war.

        Fuer die Bilanz-Rechnung gedacht: wenn Archiv-Daten fehlen, fallen
        wir auf Forecast zurueck. Wir nehmen pro Stunde die Zeile mit dem
        juengsten `abfrage_zeitstempel` <= bis.
        """
        assert self._db is not None
        sql = """SELECT vorhersage_zeitstempel, niederschlag_mm, et0_mm,
                        abfrage_zeitstempel
                 FROM wetter_vorhersage
                 WHERE standort_id = ?
                   AND vorhersage_zeitstempel >= ?
                   AND vorhersage_zeitstempel <= ?
                   AND abfrage_zeitstempel <= ?
                 ORDER BY vorhersage_zeitstempel, abfrage_zeitstempel DESC"""
        params = (standort_id, von.isoformat(), bis.isoformat(), bis.isoformat())
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()
        # pro vorhersage_zeitstempel die ERSTE Zeile (juengste Abfrage dank DESC)
        aus: dict[datetime, tuple[float, float]] = {}
        for z in zeilen:
            ts = datetime.fromisoformat(z["vorhersage_zeitstempel"])
            if ts not in aus:
                aus[ts] = (
                    float(z["niederschlag_mm"] or 0.0),
                    float(z["et0_mm"] or 0.0),
                )
        return aus

    async def hole_wetter_vorhersagen(
        self, von: datetime, bis: datetime,
        standort_id: str | None = None,
    ) -> list[dict]:
        """Liest gespeicherte Wetter-Vorhersagen (ML-Bulk, leakage-sicher).

        Gibt Rohdaten zurueck: jede Zeile ist ein Vorhersage-Stundenwert
        mit abfrage_zeitstempel (wann der Forecast geholt wurde).
        Der FeatureExtraktor filtert dann: nur Forecasts verwenden,
        die zum Zeitpunkt t bereits verfuegbar waren.
        """
        assert self._db is not None
        bedingungen = ["abfrage_zeitstempel >= ?", "abfrage_zeitstempel <= ?"]
        params: list = [von.isoformat(), bis.isoformat()]
        if standort_id:
            bedingungen.append("standort_id = ?")
            params.append(standort_id)

        sql = f"""SELECT * FROM wetter_vorhersage
                  WHERE {' AND '.join(bedingungen)}
                  ORDER BY standort_id, abfrage_zeitstempel, vorhersage_zeitstempel"""
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()
        return [
            {
                "abfrage_zeitstempel": z["abfrage_zeitstempel"],
                "vorhersage_zeitstempel": z["vorhersage_zeitstempel"],
                "temperatur": z["temperatur"],
                "niederschlag_mm": z["niederschlag_mm"],
                "niederschlag_wahrscheinlichkeit": z["niederschlag_wahrscheinlichkeit"],
                "wind_kmh": z["wind_kmh"],
                "wind_richtung_grad": z["wind_richtung_grad"],
                "et0_mm": z["et0_mm"],
                "standort_id": z["standort_id"],
                "luftfeuchte": z["luftfeuchte"],
            }
            for z in zeilen
        ]

    # --- Wetter-Vorhersagen ---

    async def speichere_bilanz_zustand(
        self, zone_id: str, zeitstempel: datetime, schritt,
        wuerde_giessen: bool, ist_entscheidung: bool | None = None,
        quelle: str = "",
    ) -> None:
        """T-0422: Bilanz-Schritt fortschreiben (Shadow)."""
        assert self._db is not None
        await self._db.execute(
            """INSERT OR REPLACE INTO wasserbilanz_zustand
               (zone_id, zeitstempel, dr_mm, taw_mm, raw_mm, et0_mm,
                regen_mm, bewaesserung_mm, wuerde_giessen, ist_entscheidung,
                quelle)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                zone_id, zeitstempel.isoformat(),
                schritt.dr_nachher_mm, schritt.speicher.taw_mm,
                schritt.speicher.raw_mm, schritt.et0_mm, schritt.regen_mm,
                schritt.bewaesserung_mm, int(wuerde_giessen),
                None if ist_entscheidung is None else int(ist_entscheidung),
                quelle,
            ),
        )
        await self._db.commit()

    async def hole_letzten_bilanz_zustand(
        self, zone_id: str,
    ) -> dict | None:
        """Juengster Dr-Stand einer Zone (fuer die Fortschreibung)."""
        assert self._db is not None
        async with self._db.execute(
            """SELECT * FROM wasserbilanz_zustand WHERE zone_id = ?
               ORDER BY zeitstempel DESC LIMIT 1""",
            (zone_id,),
        ) as cursor:
            zeile = await cursor.fetchone()
        return dict(zeile) if zeile else None

    async def hole_bilanz_historie(
        self, zone_id: str | None = None, limit: int = 500,
    ) -> list[dict]:
        """Historie fuer die T-0422-Shadow-Auswertung."""
        assert self._db is not None
        sql = "SELECT * FROM wasserbilanz_zustand"
        params: list = []
        if zone_id:
            sql += " WHERE zone_id = ?"
            params.append(zone_id)
        sql += " ORDER BY zeitstempel DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()
        return [dict(z) for z in zeilen]

    async def speichere_auto_ausschluss(
        self, zone_id: str, geraet_id: str, von: datetime, bis: datetime,
        grund: str, quelle: str = "fyta_sprung_detektor",
    ) -> bool:
        """T-0416 Stufe 2: automatisch erkanntes Ausschluss-Fenster ablegen.

        Rueckgabe True, wenn NEU angelegt. Der UNIQUE-Index auf
        (zone_id, geraet_id, von) macht den Aufruf idempotent -- der
        Detektor laeuft stuendlich mit 90-min-Rueckblick und sieht denselben
        Sprung mehrfach.
        """
        assert self._db is not None
        cursor = await self._db.execute(
            """INSERT OR IGNORE INTO auto_ausschluss_fenster
               (zone_id, geraet_id, von, bis, grund, quelle, angelegt_am)
               VALUES (?,?,?,?,?,?,?)""",
            (
                zone_id, geraet_id, von.isoformat(), bis.isoformat(),
                grund, quelle, datetime.now().isoformat(),
            ),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def hole_auto_ausschluss(self) -> list[dict]:
        """Alle automatisch gesetzten Fenster (fuer den Start-Merge)."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM auto_ausschluss_fenster ORDER BY von"
        ) as cursor:
            zeilen = await cursor.fetchall()
        return [dict(z) for z in zeilen]

    async def speichere_regen_ensemble(
        self, abfrage_zeit: datetime, standort_id: str,
        horizont_stunden: int, ensemble, deterministisch_mm: float | None = None,
    ) -> None:
        """T-0423: schreibt eine Ensemble-Verteilung fort.

        Bewusst die volle Verteilung, nicht nur der p20, mit dem gerechnet
        wird: T-0425 soll spaeter beantworten, ob ein Fehlgriff aus einem
        EINIGEN oder einem UNEINIGEN Ensemble kam. Ohne p10/p50/p90 ist das
        nicht rekonstruierbar.

        `deterministisch_mm` daneben, damit sich der Kernbefund von T-0422/23
        laufend nachpruefen laesst -- am 22.07. lag der deterministische Wert
        UNTER dem p25 des Ensembles.

        INSERT OR REPLACE: derselbe Modelllauf kann mehrfach abgefragt werden
        (5-min-Loop), das ist kein neuer Datenpunkt.
        """
        assert self._db is not None
        await self._db.execute(
            """INSERT OR REPLACE INTO regen_ensemble
               (abfrage_zeitstempel, standort_id, horizont_stunden,
                p10, p20, p50, p90, minimum, maximum, n_member,
                wahrsch_ueber_1mm, deterministisch_mm)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                abfrage_zeit.isoformat(), standort_id, horizont_stunden,
                ensemble.p10, ensemble.p20, ensemble.p50, ensemble.p90,
                ensemble.minimum, ensemble.maximum, ensemble.n_member,
                ensemble.wahrsch_ueber_1mm, deterministisch_mm,
            ),
        )
        await self._db.commit()

    async def hole_regen_ensemble(
        self, standort_id: str | None = None, limit: int = 200,
    ) -> list[dict]:
        """Liest die Ensemble-Historie (fuer T-0425 und die Ops-Ansicht)."""
        assert self._db is not None
        sql = "SELECT * FROM regen_ensemble"
        params: list = []
        if standort_id:
            sql += " WHERE standort_id = ?"
            params.append(standort_id)
        sql += " ORDER BY abfrage_zeitstempel DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()
        return [dict(z) for z in zeilen]

    async def speichere_wetter(
        self, abfrage_zeit: datetime, stunden: list[WetterStunde],
        standort_id: str = "standard",
    ) -> None:
        """Speichert Wettervorhersage-Stunden."""
        assert self._db is not None
        for s in stunden:
            # T-0100: INSERT OR IGNORE statt INSERT — der UNIQUE-Index
            # idx_wetter_vorhersage_uniq verhindert Dubletten, wir wollen
            # bei einem Re-Import nicht mit IntegrityError abbrechen.
            await self._db.execute(
                """INSERT OR IGNORE INTO wetter_vorhersage
                   (abfrage_zeitstempel, vorhersage_zeitstempel, temperatur,
                    niederschlag_mm, niederschlag_wahrscheinlichkeit,
                    wind_kmh, wind_richtung_grad, et0_mm, standort_id,
                    luftfeuchte)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    abfrage_zeit.isoformat(),
                    s.zeitstempel.isoformat(),
                    s.temperatur,
                    s.niederschlag_mm,
                    s.niederschlag_wahrscheinlichkeit,
                    s.wind_kmh,
                    s.wind_richtung_grad,
                    s.et0_mm,
                    standort_id,
                    s.luftfeuchte_prozent,
                ),
            )
        await self._db.commit()

    async def backfill_luftfeuchte(
        self, standort_id: str, luftfeuchte_pro_stunde: dict[str, float],
    ) -> int:
        """T-0045: Setzt `luftfeuchte` fuer Bestandsdaten aus Archive-API.

        Aktualisiert nur Zeilen, in denen luftfeuchte bisher NULL ist —
        lebende Forecasts mit bereits eingetragener Luftfeuchte bleiben
        unberuehrt. `luftfeuchte_pro_stunde` mappt ISO-Zeitstempel der
        Vorhersage-Stunde auf Prozentwert.
        """
        assert self._db is not None
        n = 0
        for zeitstempel_iso, rh in luftfeuchte_pro_stunde.items():
            cursor = await self._db.execute(
                """UPDATE wetter_vorhersage
                   SET luftfeuchte = ?
                   WHERE vorhersage_zeitstempel = ?
                     AND standort_id = ?
                     AND luftfeuchte IS NULL""",
                (rh, zeitstempel_iso, standort_id),
            )
            n += cursor.rowcount or 0
        await self._db.commit()
        return n

    # --- Wetter-Archiv (Real-Ground-Truth, Open-Meteo Archive) ---

    async def upsert_wetter_archiv(
        self, stunden: list["WetterArchivStunde"], standort_id: str,
    ) -> int:
        """Schreibt/aktualisiert Archiv-Stunden. Gibt Anzahl geschriebener Zeilen zurueck."""
        assert self._db is not None
        jetzt_iso = datetime.now().isoformat()
        n = 0
        for s in stunden:
            await self._db.execute(
                """INSERT INTO wetter_archiv
                   (zeitstempel, standort_id, niederschlag_mm, temperatur, et0_mm, abgerufen_am)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(zeitstempel, standort_id) DO UPDATE SET
                     niederschlag_mm = excluded.niederschlag_mm,
                     temperatur = excluded.temperatur,
                     et0_mm = excluded.et0_mm,
                     abgerufen_am = excluded.abgerufen_am""",
                (
                    s.zeitstempel.isoformat(),
                    standort_id,
                    s.niederschlag_mm,
                    s.temperatur,
                    s.et0_mm,
                    jetzt_iso,
                ),
            )
            n += 1
        await self._db.commit()
        return n

    # --- T-0050b Plant-Optimum-Cache (FYTA) ---

    async def speichere_plant_optimum(
        self, zone_id: str, feuchte_min: float, feuchte_max: float,
        feuchte_min_akzeptabel: float | None = None,
        feuchte_max_akzeptabel: float | None = None,
        quelle: str = "fyta",
    ) -> None:
        """T-0050b: UPSERT eines Plant-Optimum-Werts pro Zone."""
        assert self._db is not None
        await self._db.execute(
            """INSERT INTO plant_optimum
               (zone_id, feuchte_min, feuchte_max,
                feuchte_min_akzeptabel, feuchte_max_akzeptabel,
                aktualisiert, quelle)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(zone_id) DO UPDATE SET
                 feuchte_min = excluded.feuchte_min,
                 feuchte_max = excluded.feuchte_max,
                 feuchte_min_akzeptabel = excluded.feuchte_min_akzeptabel,
                 feuchte_max_akzeptabel = excluded.feuchte_max_akzeptabel,
                 aktualisiert = excluded.aktualisiert,
                 quelle = excluded.quelle""",
            (
                zone_id, float(feuchte_min), float(feuchte_max),
                float(feuchte_min_akzeptabel) if feuchte_min_akzeptabel is not None else None,
                float(feuchte_max_akzeptabel) if feuchte_max_akzeptabel is not None else None,
                datetime.now().isoformat(), quelle,
            ),
        )
        if not self._in_transaktion:
            await self._db.commit()

    async def hole_plant_optima(self) -> dict[str, dict]:
        """Liefert alle gecachten Plant-Optima als Dict {zone_id: {min, max, ...}}."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT zone_id, feuchte_min, feuchte_max, "
            "feuchte_min_akzeptabel, feuchte_max_akzeptabel, "
            "aktualisiert, quelle FROM plant_optimum"
        ) as cursor:
            zeilen = await cursor.fetchall()
        return {
            z["zone_id"]: {
                "feuchte_min": z["feuchte_min"],
                "feuchte_max": z["feuchte_max"],
                "feuchte_min_akzeptabel": z["feuchte_min_akzeptabel"],
                "feuchte_max_akzeptabel": z["feuchte_max_akzeptabel"],
                "aktualisiert": z["aktualisiert"],
                "quelle": z["quelle"],
            }
            for z in zeilen
        }

    # --- T-0196 Plant-Optimum Multi-Achse ---

    async def speichere_plant_optimum_achse(
        self,
        zone_id: str,
        achse: str,
        einheit: str,
        min_good: float | None = None,
        max_good: float | None = None,
        min_akzeptabel: float | None = None,
        max_akzeptabel: float | None = None,
        current: float | None = None,
        quelle: str = "fyta",
    ) -> None:
        """T-0196: UPSERT einer Optimum-Schwelle pro (zone_id, achse).

        EAV-Pattern. achse ist deutscher Identifier (feuchte, licht_ppfd,
        licht_dli, temperatur, salinitaet). Alle Schwellen koennen None
        sein wenn FYTA fuer diese Achse nur partiell Werte liefert.
        einheit ist Pflicht (z.B. 'mol/day', '°C/h').

        T-0196d/e: `current` ist der zuletzt beobachtete Wert. Bei
        UPSERT auf bestehender Zeile mit current=None bleibt das
        bestehende current erhalten (kein Overwrite mit NULL), damit
        Schwellen-Updates ohne Live-Wert nicht den zuletzt
        gespeicherten current loeschen.
        """
        assert self._db is not None
        await self._db.execute(
            """INSERT INTO plant_optimum_achse
               (zone_id, achse, min_good, max_good,
                min_akzeptabel, max_akzeptabel, current,
                einheit, aktualisiert, quelle)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(zone_id, achse) DO UPDATE SET
                 min_good = excluded.min_good,
                 max_good = excluded.max_good,
                 min_akzeptabel = excluded.min_akzeptabel,
                 max_akzeptabel = excluded.max_akzeptabel,
                 current = COALESCE(excluded.current, plant_optimum_achse.current),
                 einheit = excluded.einheit,
                 aktualisiert = excluded.aktualisiert,
                 quelle = excluded.quelle""",
            (
                zone_id, achse,
                float(min_good) if min_good is not None else None,
                float(max_good) if max_good is not None else None,
                float(min_akzeptabel) if min_akzeptabel is not None else None,
                float(max_akzeptabel) if max_akzeptabel is not None else None,
                float(current) if current is not None else None,
                einheit,
                datetime.now().isoformat(),
                quelle,
            ),
        )
        if not self._in_transaktion:
            await self._db.commit()

    async def hole_plant_optima_achsen(
        self, zone_id: str | None = None,
    ) -> dict[str, dict[str, dict]]:
        """T-0196: Liefert Multi-Achsen-Optimum.

        Returns: dict[zone_id, dict[achse, dict[bound, value]]].
        Wenn `zone_id` gesetzt, nur diese Zone (Dict mit 1 Key oder leer).

        Felder pro Achse: min_good, max_good, min_akzeptabel,
        max_akzeptabel, current, einheit, aktualisiert, quelle.
        """
        assert self._db is not None
        sql = (
            "SELECT zone_id, achse, min_good, max_good, "
            "min_akzeptabel, max_akzeptabel, current, "
            "einheit, aktualisiert, quelle "
            "FROM plant_optimum_achse"
        )
        params: tuple = ()
        if zone_id is not None:
            sql += " WHERE zone_id = ?"
            params = (zone_id,)
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()
        ergebnis: dict[str, dict[str, dict]] = {}
        for z in zeilen:
            zone = z["zone_id"]
            ergebnis.setdefault(zone, {})[z["achse"]] = {
                "min_good": z["min_good"],
                "max_good": z["max_good"],
                "min_akzeptabel": z["min_akzeptabel"],
                "max_akzeptabel": z["max_akzeptabel"],
                "current": z["current"],
                "einheit": z["einheit"],
                "aktualisiert": z["aktualisiert"],
                "quelle": z["quelle"],
            }
        return ergebnis

    # --- T-0063 Kalibrier-Messungen ---

    async def speichere_kalibrierung(
        self, zeitstempel: datetime, zone_id: str, typ: str,
        wert: float, basis_mm: float | None = None,
        notizen: str | None = None,
    ) -> None:
        """T-0063: speichert einen Feldkapazitaets- oder Welkepunkt-Kandidaten.

        Idempotenz: haben wir schon einen Eintrag mit gleichem
        zone_id+typ+zeitstempel (auf die Stunde gerundet)? → skip.
        Sonst landen bei wiederholten Job-Laeufen Duplikate in der DB.
        """
        assert self._db is not None
        stunde_iso = zeitstempel.replace(minute=0, second=0, microsecond=0).isoformat()
        async with self._db.execute(
            "SELECT 1 FROM feldkapazitaet_messung "
            "WHERE zone_id = ? AND typ = ? AND zeitstempel = ?",
            (zone_id, typ, stunde_iso),
        ) as cursor:
            if await cursor.fetchone():
                return
        await self._db.execute(
            "INSERT INTO feldkapazitaet_messung "
            "(zeitstempel, zone_id, typ, wert, basis_mm, notizen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (stunde_iso, zone_id, typ, float(wert),
             float(basis_mm) if basis_mm is not None else None, notizen),
        )
        if not self._in_transaktion:
            await self._db.commit()

    async def hole_kalibrierungen(
        self, zone_id: str | None = None, typ: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """T-0063: liest gespeicherte Kalibrier-Kandidaten (neueste zuerst)."""
        assert self._db is not None
        bedingungen: list[str] = []
        params: list = []
        if zone_id:
            bedingungen.append("zone_id = ?")
            params.append(zone_id)
        if typ:
            bedingungen.append("typ = ?")
            params.append(typ)
        where = f"WHERE {' AND '.join(bedingungen)}" if bedingungen else ""
        sql = (
            "SELECT id, zeitstempel, zone_id, typ, wert, basis_mm, notizen "
            f"FROM feldkapazitaet_messung {where} "
            "ORDER BY zeitstempel DESC LIMIT ?"
        )
        params.append(limit)
        async with self._db.execute(sql, tuple(params)) as cursor:
            zeilen = await cursor.fetchall()
        return [
            {
                "id": z["id"],
                "zeitstempel": z["zeitstempel"],
                "zone_id": z["zone_id"],
                "typ": z["typ"],
                "wert": z["wert"],
                "basis_mm": z["basis_mm"],
                "notizen": z["notizen"],
            }
            for z in zeilen
        ]

    async def hole_wetter_archiv(
        self, standort_id: str,
        von: datetime | None = None, bis: datetime | None = None,
    ) -> list["WetterArchivStunde"]:
        """Liest Archiv-Stunden fuer einen Standort."""
        assert self._db is not None
        bedingungen = ["standort_id = ?"]
        params: list = [standort_id]
        if von:
            bedingungen.append("zeitstempel >= ?")
            params.append(von.isoformat())
        if bis:
            bedingungen.append("zeitstempel <= ?")
            params.append(bis.isoformat())
        sql = (
            "SELECT zeitstempel, niederschlag_mm, temperatur, et0_mm "
            "FROM wetter_archiv WHERE "
            + " AND ".join(bedingungen)
            + " ORDER BY zeitstempel"
        )
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()
        from bewaesserung.modelle import WetterArchivStunde
        return [
            WetterArchivStunde(
                zeitstempel=datetime.fromisoformat(z["zeitstempel"]),
                niederschlag_mm=z["niederschlag_mm"] or 0.0,
                temperatur=z["temperatur"],
                et0_mm=z["et0_mm"] or 0.0,
            )
            for z in zeilen
        ]

    async def juengster_archiv_zeitstempel(
        self, standort_id: str,
    ) -> datetime | None:
        """Gibt den Zeitstempel der juengsten Archiv-Stunde zurueck (oder None)."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT MAX(zeitstempel) AS m FROM wetter_archiv WHERE standort_id = ?",
            (standort_id,),
        ) as cursor:
            zeile = await cursor.fetchone()
        if not zeile or not zeile["m"]:
            return None
        return datetime.fromisoformat(zeile["m"])

    async def hole_wetter_kombiniert(
        self, standort_id: str,
        von: datetime, bis: datetime,
    ) -> list["WetterArchivStunde"]:
        """T-0063a: Wetter-Zeitreihe mit Vorhersage-Fallback.

        Lueckenhafte Bereiche in `wetter_archiv` (ERA5 hat 3-5 Tage Latenz)
        werden aus `wetter_vorhersage` ergaenzt. Pro fehlender Stunde wird
        die **neueste** Abfrage genommen (aktuellster Forecast bzw. Nowcast).
        Ergebnis ist eine vollstaendige stuendliche Zeitreihe als
        `WetterArchivStunde`, damit der Kalibrierungs-Job (T-0063) den
        aktuellen Regen-Event nicht erst 5-7 Tage spaeter sieht.

        Archiv-Werte haben Vorrang (= Ground truth, ERA5-korrigiert).
        """
        assert self._db is not None
        # Archiv zuerst — ist die verlaessliche Quelle
        archiv = await self.hole_wetter_archiv(standort_id, von=von, bis=bis)
        vorhanden: dict[str, "WetterArchivStunde"] = {
            a.zeitstempel.replace(minute=0, second=0, microsecond=0)
             .isoformat(): a for a in archiv
        }
        # Vorhersage-Fallback fuer fehlende Stunden; pro Zeitstempel
        # die aktuellste Abfrage ("neueste Wahrheit ueber Stunde X")
        sql = (
            "SELECT vorhersage_zeitstempel, niederschlag_mm, temperatur, "
            "et0_mm, abfrage_zeitstempel FROM wetter_vorhersage "
            "WHERE standort_id = ? "
            "AND vorhersage_zeitstempel >= ? AND vorhersage_zeitstempel <= ? "
            "ORDER BY vorhersage_zeitstempel, abfrage_zeitstempel DESC"
        )
        async with self._db.execute(
            sql, (standort_id, von.isoformat(), bis.isoformat()),
        ) as cursor:
            zeilen = await cursor.fetchall()
        from bewaesserung.modelle import WetterArchivStunde
        neueste: dict[str, tuple[str, dict]] = {}
        for z in zeilen:
            schluessel = z["vorhersage_zeitstempel"]
            abf = z["abfrage_zeitstempel"]
            if schluessel in vorhanden:
                continue  # Archiv schon vorhanden
            if schluessel not in neueste or abf > neueste[schluessel][0]:
                neueste[schluessel] = (abf, dict(z))
        for schluessel, (_, z) in neueste.items():
            vorhanden[schluessel] = WetterArchivStunde(
                zeitstempel=datetime.fromisoformat(schluessel),
                niederschlag_mm=z["niederschlag_mm"] or 0.0,
                temperatur=z["temperatur"],
                et0_mm=z["et0_mm"] or 0.0,
            )
        return sorted(vorhanden.values(), key=lambda s: s.zeitstempel)

    # --- T-0047 ML-Drift-Log ---

    async def logge_ml_vorhersage(
        self,
        zeitstempel: datetime,
        zone_id: str,
        horizont_h: int,
        prognose_ziel_zeit: datetime,
        prognose_feuchte: float,
        modell_version: str,
        q10: float | None = None,
        q90: float | None = None,
        feature_zeitstempel: datetime | None = None,
    ) -> None:
        """Loggt eine Live-Inferenz — ist_feuchte/abweichung bleiben NULL.

        `q10`/`q90` (T-0046) sind optional; ohne Quantile-Modelle bleiben
        sie NULL. `feature_zeitstempel` dedupliziert Dashboard-Polling fuer
        dieselbe fachliche Feature-Zeile.
        """
        assert self._db is not None

        async def _op() -> None:
            await self._db.execute(
                """INSERT OR IGNORE INTO ml_vorhersage_log
                   (zeitstempel, zone_id, horizont_h, prognose_ziel_zeit,
                    prognose_feuchte, modell_version, prognose_q10, prognose_q90,
                    feature_zeitstempel)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    zeitstempel.isoformat(),
                    zone_id,
                    horizont_h,
                    prognose_ziel_zeit.isoformat(),
                    float(prognose_feuchte),
                    modell_version,
                    float(q10) if q10 is not None else None,
                    float(q90) if q90 is not None else None,
                    (
                        feature_zeitstempel.isoformat()
                        if feature_zeitstempel is not None else None
                    ),
                ),
            )
            if not self._in_transaktion:
                await self._db.commit()

        await self._mit_lock_retry(_op, label="logge_ml_vorhersage")

    async def evaluiere_offene_vorhersagen(
        self,
        jetzt: datetime,
        toleranz_minuten: int = 30,
        limit: int = 5000,
        nach_ziel_zeit: str | None = None,
    ) -> tuple[int, str | None]:
        """Gleicht offene Prognosen mit der echten Sensor-Messung ab.

        Fuer jede Zeile mit `evaluiert_am IS NULL`, deren
        `prognose_ziel_zeit + toleranz` <= jetzt ist, wird die naechste
        Sensor-Messung innerhalb +/- toleranz gesucht. Falls gefunden
        werden ist_feuchte, abweichung (ist - prognose) und evaluiert_am
        gesetzt. Rueckgabe: `(anzahl_aktualisiert, max_gesehene_ziel_zeit)`.

        `limit` begrenzt die pro Aufruf bearbeiteten Zeilen — Schutz
        gegen Runaway-Blockade im Async-Loop. 5000 Zeilen = ca. 5 s
        auf einer Mac-SSD.

        `nach_ziel_zeit` ist der Cursor: nur Zeilen mit
        `prognose_ziel_zeit > nach_ziel_zeit` werden gefetcht. Wichtig
        fuer den Catchup-Loop in `drift_job.aktualisiere_wenn_faellig`:
        ohne den Cursor wuerden unevaluierbare Zeilen (= Zielzeit ohne
        passende Sensor-Messung in Toleranz, z. B. wenn der Sensor
        kurzzeitig ausgefallen war) bei jedem Aufruf erneut die Top-N
        der ORDER-BY-Queue belegen und neuere evaluierbare Zeilen
        nie erreichbar machen. Mit Cursor schiebt sich der naechste
        Aufruf an die unevaluierbaren Zeilen vorbei — sie bleiben
        zwar `evaluiert_am IS NULL`, blockieren den Loop aber nicht.
        """
        assert self._db is not None
        schwelle = (jetzt - timedelta(minutes=toleranz_minuten)).isoformat()
        offen: list = []
        sql = """SELECT id, zone_id, prognose_ziel_zeit, prognose_feuchte
                   FROM ml_vorhersage_log
                  WHERE evaluiert_am IS NULL
                    AND prognose_ziel_zeit <= ?"""
        params: list = [schwelle]
        if nach_ziel_zeit is not None:
            sql += " AND prognose_ziel_zeit > ?"
            params.append(nach_ziel_zeit)
        sql += " ORDER BY prognose_ziel_zeit LIMIT ?"
        params.append(int(limit))
        async with self._db.execute(sql, params) as cursor:
            async for zeile in cursor:
                offen.append(dict(zeile))

        if not offen:
            return 0, nach_ziel_zeit

        jetzt_iso = jetzt.isoformat()
        tol = timedelta(minutes=toleranz_minuten)
        aktualisiert = 0
        max_gesehen = nach_ziel_zeit
        for z in offen:
            # Cursor weiterschieben — auch wenn unten kein Match gefunden
            # wird, soll der naechste Catchup-Aufruf nicht wieder bei
            # dieser Zeile starten.
            ziel_str = z["prognose_ziel_zeit"]
            if max_gesehen is None or ziel_str > max_gesehen:
                max_gesehen = ziel_str

            ziel = datetime.fromisoformat(ziel_str)
            von_iso = (ziel - tol).isoformat()
            bis_iso = (ziel + tol).isoformat()
            async with self._db.execute(
                """SELECT boden_feuchte, zeitstempel
                     FROM sensor_messung
                    WHERE zone_id = ?
                      AND boden_feuchte IS NOT NULL
                      AND zeitstempel BETWEEN ? AND ?
                 ORDER BY ABS(strftime('%s', zeitstempel) - strftime('%s', ?))
                    LIMIT 1""",
                (z["zone_id"], von_iso, bis_iso, ziel.isoformat()),
            ) as cursor:
                match = await cursor.fetchone()

            if match is None or match["boden_feuchte"] is None:
                continue

            ist = float(match["boden_feuchte"])
            abweichung = ist - float(z["prognose_feuchte"])
            await self._db.execute(
                """UPDATE ml_vorhersage_log
                      SET ist_feuchte = ?,
                          abweichung = ?,
                          evaluiert_am = ?
                    WHERE id = ?""",
                (ist, abweichung, jetzt_iso, z["id"]),
            )
            aktualisiert += 1

        if aktualisiert:
            await self._db.commit()
        return aktualisiert, max_gesehen

    async def hole_drift_metriken(
        self,
        zone_id: str | None,
        fenster_tage: int,
        jetzt: datetime | None = None,
    ) -> dict[int, dict]:
        """Liefert rollenden MAE pro Horizont im Fenster der letzten Tage.

        Rueckgabe: {6: {mae, n}, 12: {mae, n}, 24: {mae, n}}. Horizonte
        ohne evaluierte Eintraege fehlen im Dict.
        """
        assert self._db is not None
        jetzt = jetzt or datetime.now()
        von_iso = (jetzt - timedelta(days=fenster_tage)).isoformat()

        bedingungen = ["abweichung IS NOT NULL", "zeitstempel >= ?"]
        params: list = [von_iso]
        if zone_id:
            bedingungen.append("zone_id = ?")
            params.append(zone_id)

        sql = f"""SELECT horizont_h,
                         AVG(ABS(abweichung)) AS mae,
                         COUNT(*) AS n
                    FROM ml_vorhersage_log
                   WHERE {' AND '.join(bedingungen)}
                GROUP BY horizont_h"""
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()

        return {
            int(z["horizont_h"]): {
                "mae": round(float(z["mae"]), 3),
                "n": int(z["n"]),
            }
            for z in zeilen
        }

    async def hole_drift_status(
        self,
        zone_id: str | None,
        fenster_tage: int,
        jetzt: datetime | None = None,
        toleranz_minuten: int = 30,
    ) -> dict[int, dict]:
        """Pro Horizont: wieviele Prognosen im Fenster sind noch offen,
        wann lief die letzte Drift-Job-Evaluierung.

        Komplementaer zu `hole_drift_metriken`: das liefert MAE *fuer
        evaluierte Zeilen*. Diese Methode beantwortet "wieviel ist im
        Backlog" und "ist der Drift-Job aktuell". Damit kann das UI
        zwischen "keine Daten je geloggt" (n_offen=0, letzte_eval=None)
        und "Backlog noch nicht aufgeholt" (n_offen>0) unterscheiden —
        bisher rendert beides als `n=0, mae=null`.

        Rueckgabe: {h: {n_offen_im_fenster, n_offen_total, letzte_evaluierung}}
        fuer h in {6, 12, 24}.
        """
        assert self._db is not None
        jetzt = jetzt or datetime.now()
        von_iso = (jetzt - timedelta(days=fenster_tage)).isoformat()
        schwelle = (jetzt - timedelta(minutes=toleranz_minuten)).isoformat()

        zone_filter = ""
        zone_param: list = []
        if zone_id:
            zone_filter = " AND zone_id = ?"
            zone_param = [zone_id]

        # Offen + faellig im Fenster (Zielzeit innerhalb fenster_tage,
        # noch nicht evaluiert, aber Zielzeit + Toleranz schon vorbei).
        sql_offen_im_fenster = f"""
            SELECT horizont_h, COUNT(*) AS n
              FROM ml_vorhersage_log
             WHERE evaluiert_am IS NULL
               AND prognose_ziel_zeit <= ?
               AND zeitstempel >= ?{zone_filter}
          GROUP BY horizont_h
        """
        async with self._db.execute(
            sql_offen_im_fenster, [schwelle, von_iso, *zone_param]
        ) as cursor:
            offen_im_fenster = {
                int(z["horizont_h"]): int(z["n"])
                for z in await cursor.fetchall()
            }

        # Offen total (gesamter Backlog, auch ausserhalb Fenster).
        sql_offen_total = f"""
            SELECT horizont_h, COUNT(*) AS n
              FROM ml_vorhersage_log
             WHERE evaluiert_am IS NULL
               AND prognose_ziel_zeit <= ?{zone_filter}
          GROUP BY horizont_h
        """
        async with self._db.execute(
            sql_offen_total, [schwelle, *zone_param]
        ) as cursor:
            offen_total = {
                int(z["horizont_h"]): int(z["n"])
                for z in await cursor.fetchall()
            }

        # Letzte Evaluierung pro Horizont — Indikator ob Drift-Job lebt.
        sql_letzte = f"""
            SELECT horizont_h, MAX(evaluiert_am) AS letzte
              FROM ml_vorhersage_log
             WHERE evaluiert_am IS NOT NULL{zone_filter}
          GROUP BY horizont_h
        """
        async with self._db.execute(
            sql_letzte, zone_param
        ) as cursor:
            letzte_eval = {
                int(z["horizont_h"]): z["letzte"]
                for z in await cursor.fetchall()
            }

        out: dict[int, dict] = {}
        for h in (6, 12, 24):
            out[h] = {
                "n_offen_im_fenster": offen_im_fenster.get(h, 0),
                "n_offen_total": offen_total.get(h, 0),
                "letzte_evaluierung": letzte_eval.get(h),
            }
        return out

    async def hole_drift_log(
        self,
        zone_id: str | None = None,
        horizont_h: int | None = None,
        n: int = 50,
        nur_evaluiert: bool = True,
    ) -> list[dict]:
        """Liefert die letzten `n` Zeilen aus `ml_vorhersage_log` als Dict-
        Liste — Inspektor-Sicht "Prognose vs. Ist" pro Zone+Horizont.

        Sortierung: nach Inferenz-Zeitstempel absteigend (= neueste zuerst),
        damit das UI die letzte Drift-Sicht sofort hat.

        `nur_evaluiert=True` (Default): nur Zeilen mit `abweichung IS NOT
        NULL`. Praktisch fuers UI — unevaluierte Zeilen haben keine
        Vergleichswerte. `False`: alle Zeilen, auch offene (z. B. fuer
        Diagnose 'wieviele warten noch').

        `n` wird hart auf [1, 500] geclippt — Schutz gegen UI-Versehen.
        """
        assert self._db is not None
        n = max(1, min(int(n), 500))

        bedingungen: list[str] = []
        params: list = []
        if nur_evaluiert:
            bedingungen.append("abweichung IS NOT NULL")
        if zone_id:
            bedingungen.append("zone_id = ?")
            params.append(zone_id)
        if horizont_h is not None:
            bedingungen.append("horizont_h = ?")
            params.append(int(horizont_h))

        where = ""
        if bedingungen:
            where = "WHERE " + " AND ".join(bedingungen)

        sql = f"""SELECT zeitstempel, zone_id, horizont_h,
                         prognose_ziel_zeit, prognose_feuchte,
                         prognose_q10, prognose_q90,
                         ist_feuchte, abweichung,
                         modell_version, evaluiert_am
                    FROM ml_vorhersage_log
                    {where}
                ORDER BY zeitstempel DESC
                   LIMIT ?"""
        params.append(n)

        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()

        return [
            {
                "zeitstempel": z["zeitstempel"],
                "zone_id": z["zone_id"],
                "horizont_h": int(z["horizont_h"]),
                "prognose_ziel_zeit": z["prognose_ziel_zeit"],
                "prognose_feuchte": float(z["prognose_feuchte"]),
                "prognose_q10": (
                    float(z["prognose_q10"]) if z["prognose_q10"] is not None else None
                ),
                "prognose_q90": (
                    float(z["prognose_q90"]) if z["prognose_q90"] is not None else None
                ),
                "ist_feuchte": (
                    float(z["ist_feuchte"]) if z["ist_feuchte"] is not None else None
                ),
                "abweichung": (
                    float(z["abweichung"]) if z["abweichung"] is not None else None
                ),
                "modell_version": z["modell_version"],
                "evaluiert_am": z["evaluiert_am"],
            }
            for z in zeilen
        ]

    # --- T-0065 Dauer-Vorschlag (Response-Modell Shadow-Log) ---

    async def speichere_dauer_vorschlag(
        self,
        zeitstempel: datetime,
        zone_id: str,
        f_vor: float,
        ziel_schwelle: float,
        heuristik_s: int,
        ml_s: int | None,
        ml_modell_version: str | None,
        features_json: str,
        modus: str,
    ) -> int:
        """Loggt eine produktive _berechne_dauer-Entscheidung.

        `modus` ist 'shadow' (ML-Empfehlung parallel, aber Heuristik wird
        zurueckgegeben) oder 'wirksam' (ML-Dauer ist die zurueckgegebene).
        Der drift_job fuellt nach 6 h `ist_delta_6h` + Fehler-Spalten.
        Rueckgabe: row id, damit Aufrufer das Feature-Snapshot referenzieren
        koennen (selten gebraucht, aber billig).
        """
        assert self._db is not None
        if modus not in ("shadow", "wirksam"):
            raise ValueError(f"modus muss shadow|wirksam sein, war {modus!r}")
        cursor = await self._db.execute(
            """INSERT INTO ml_dauer_vorschlag
               (zeitstempel, zone_id, f_vor, ziel_schwelle,
                heuristik_s, ml_s, ml_modell_version, features_json, modus)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                zeitstempel.isoformat(),
                zone_id,
                float(f_vor),
                float(ziel_schwelle),
                int(heuristik_s),
                int(ml_s) if ml_s is not None else None,
                ml_modell_version,
                features_json,
                modus,
            ),
        )
        if not self._in_transaktion:
            await self._db.commit()
        return int(cursor.lastrowid or 0)

    async def hole_dauer_vorschlaege_unbewertet(
        self,
        bis_zeitstempel: datetime,
        limit: int = 500,
    ) -> list[dict]:
        """Liefert unbewertete Vorschlaege, deren zeitstempel + 6 h <= `bis`.

        Rueckgabe ist eine Liste von dicts mit den Spalten, die der drift_job
        fuer die 6h-Delta-Evaluation braucht. LIMIT analog Drift-Job der
        Vorhersagen, damit ein einzelner Zyklus nicht ewig blockiert.
        """
        assert self._db is not None
        schwelle_iso = (bis_zeitstempel - timedelta(hours=6)).isoformat()
        async with self._db.execute(
            """SELECT id, zeitstempel, zone_id, f_vor, ziel_schwelle,
                      heuristik_s, ml_s, ml_modell_version, features_json, modus
                 FROM ml_dauer_vorschlag
                WHERE bewertet_am IS NULL
                  AND zeitstempel <= ?
             ORDER BY zeitstempel
                LIMIT ?""",
            (schwelle_iso, int(limit)),
        ) as cursor:
            zeilen = await cursor.fetchall()
        return [dict(z) for z in zeilen]

    async def hole_letzten_ml_dauer_vorschlag(
        self, zone_id: str,
    ) -> dict | None:
        """T-0263 (2026-05-26): juengster ml_dauer_vorschlag pro Zone,
        unabhaengig vom bewertet_am-Status. Wird vom Shadow-Push-
        Format (watchdog.py) genutzt, um Heuristik vs ML im selben
        Push zu zeigen.
        """
        assert self._db is not None
        async with self._db.execute(
            """SELECT zeitstempel, zone_id, f_vor, ziel_schwelle,
                      heuristik_s, ml_s, ml_modell_version
                 FROM ml_dauer_vorschlag
                WHERE zone_id = ?
             ORDER BY zeitstempel DESC LIMIT 1""",
            (zone_id,),
        ) as cursor:
            zeile = await cursor.fetchone()
        return dict(zeile) if zeile is not None else None

    async def markiere_dauer_vorschlag_bewertet(
        self,
        row_id: int,
        bewertet_am: datetime,
        ist_delta_6h: float,
        heuristik_prognose_delta: float,
        ml_prognose_delta: float | None,
        heuristik_fehler: float,
        ml_fehler: float | None,
    ) -> None:
        """Schreibt die 6h-Sensor-Ist-Delta-Evaluation zurueck."""
        assert self._db is not None
        await self._db.execute(
            """UPDATE ml_dauer_vorschlag
                  SET bewertet_am = ?,
                      ist_delta_6h = ?,
                      heuristik_prognose_delta = ?,
                      ml_prognose_delta = ?,
                      heuristik_fehler = ?,
                      ml_fehler = ?
                WHERE id = ?""",
            (
                bewertet_am.isoformat(),
                float(ist_delta_6h),
                float(heuristik_prognose_delta),
                float(ml_prognose_delta) if ml_prognose_delta is not None else None,
                float(heuristik_fehler),
                float(ml_fehler) if ml_fehler is not None else None,
                int(row_id),
            ),
        )
        if not self._in_transaktion:
            await self._db.commit()

    async def evaluiere_offene_dauer_vorschlaege(
        self,
        jetzt: datetime,
        toleranz_minuten: int = 45,
    ) -> int:
        """T-0065: Fuellt nach >=6 h pro Vorschlag ist_delta_6h + Fehler.

        Sucht pro offener Zeile die naechste Sensor-Messung bei
        zeitstempel+6h (+/- toleranz). Rueckgabe: Anzahl aktualisierter
        Zeilen. Setzt NICHT committet, wenn nichts zu tun war.
        """
        assert self._db is not None
        offen = await self.hole_dauer_vorschlaege_unbewertet(
            bis_zeitstempel=jetzt, limit=500,
        )
        if not offen:
            return 0
        tol = timedelta(minutes=toleranz_minuten)
        aktualisiert = 0
        for zeile in offen:
            zs = datetime.fromisoformat(zeile["zeitstempel"])
            ziel = zs + timedelta(hours=6)
            von_iso = (ziel - tol).isoformat()
            bis_iso = (ziel + tol).isoformat()
            # Ist-Feuchte bei +6h
            async with self._db.execute(
                """SELECT boden_feuchte FROM sensor_messung
                    WHERE zone_id = ?
                      AND boden_feuchte IS NOT NULL
                      AND zeitstempel BETWEEN ? AND ?
                 ORDER BY ABS(strftime('%s', zeitstempel)
                            - strftime('%s', ?))
                    LIMIT 1""",
                (zeile["zone_id"], von_iso, bis_iso, ziel.isoformat()),
            ) as cursor:
                match_ist = await cursor.fetchone()
            if match_ist is None or match_ist["boden_feuchte"] is None:
                continue
            # Ist-Feuchte bei t (so nah wie moeglich am Vorschlag), fuer
            # delta = ist6h - istt.
            tol_vor = timedelta(minutes=30)
            async with self._db.execute(
                """SELECT boden_feuchte FROM sensor_messung
                    WHERE zone_id = ?
                      AND boden_feuchte IS NOT NULL
                      AND zeitstempel BETWEEN ? AND ?
                 ORDER BY ABS(strftime('%s', zeitstempel)
                            - strftime('%s', ?))
                    LIMIT 1""",
                (
                    zeile["zone_id"],
                    (zs - tol_vor).isoformat(),
                    (zs + tol_vor).isoformat(),
                    zs.isoformat(),
                ),
            ) as cursor:
                match_vor = await cursor.fetchone()
            if match_vor is None or match_vor["boden_feuchte"] is None:
                # Fallback: f_vor aus dem Vorschlag selbst (war die Basis
                # zum Entscheidungszeitpunkt).
                f_vor_ist = float(zeile["f_vor"])
            else:
                f_vor_ist = float(match_vor["boden_feuchte"])
            ist_delta = float(match_ist["boden_feuchte"]) - f_vor_ist
            # Prognose-Delta der Heuristik: sekunden / 60 (Inverse der
            # Kern-Formel) — ein naeherungsweises Shadow-Delta, damit der
            # MAE vergleichbar zum ML-Modell ist.
            heur_prog = float(zeile["heuristik_s"]) / 60.0
            heur_fehler = abs(heur_prog - ist_delta)
            ml_prog = None
            ml_fehler = None
            if zeile.get("ml_s") is not None:
                ml_prog = float(zeile["ml_s"]) / 60.0
                ml_fehler = abs(ml_prog - ist_delta)
            await self.markiere_dauer_vorschlag_bewertet(
                row_id=int(zeile["id"]),
                bewertet_am=jetzt,
                ist_delta_6h=ist_delta,
                heuristik_prognose_delta=heur_prog,
                ml_prognose_delta=ml_prog,
                heuristik_fehler=heur_fehler,
                ml_fehler=ml_fehler,
            )
            aktualisiert += 1
        if aktualisiert and not self._in_transaktion:
            await self._db.commit()
        return aktualisiert

    async def hole_dauer_drift_metriken(
        self,
        zone_id: str | None,
        fenster_tage: int,
        jetzt: datetime | None = None,
    ) -> dict[str, dict]:
        """MAE-Vergleich Heuristik vs. ML je Zone im Fenster der letzten Tage.

        Rueckgabe: {zone_id: {mae_heuristik, mae_ml, n_bewertet}}.
        Zonen ohne bewertete Vorschlaege fehlen im Dict.
        """
        assert self._db is not None
        jetzt = jetzt or datetime.now()
        von_iso = (jetzt - timedelta(days=fenster_tage)).isoformat()

        bedingungen = ["bewertet_am IS NOT NULL", "zeitstempel >= ?"]
        params: list = [von_iso]
        if zone_id:
            bedingungen.append("zone_id = ?")
            params.append(zone_id)

        sql = f"""SELECT zone_id,
                         AVG(ABS(heuristik_fehler)) AS mae_heuristik,
                         AVG(ABS(ml_fehler)) AS mae_ml,
                         COUNT(*) AS n_bewertet,
                         SUM(CASE WHEN ml_fehler IS NOT NULL THEN 1 ELSE 0 END)
                           AS n_ml_bewertet
                    FROM ml_dauer_vorschlag
                   WHERE {' AND '.join(bedingungen)}
                GROUP BY zone_id"""
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()

        ergebnis: dict[str, dict] = {}
        for z in zeilen:
            mae_ml_raw = z["mae_ml"]
            ergebnis[str(z["zone_id"])] = {
                "mae_heuristik": round(float(z["mae_heuristik"]), 2),
                "mae_ml": round(float(mae_ml_raw), 2) if mae_ml_raw is not None else None,
                "n_bewertet": int(z["n_bewertet"]),
                "n_ml_bewertet": int(z["n_ml_bewertet"]),
            }
        return ergebnis

    # --- Geraete-Zuordnung ---

    async def speichere_zuordnung(
        self, geraet_id: str, zone_id: str,
        geraet_name: str | None = None, quelle: str = "discovery",
    ) -> None:
        """Speichert oder aktualisiert eine Geraet-Zone-Zuordnung (UPSERT)."""
        assert self._db is not None
        await self._db.execute(
            """INSERT INTO geraete_zuordnung
               (geraet_id, zone_id, geraet_name, quelle, aktualisiert)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(geraet_id) DO UPDATE SET
                 zone_id = excluded.zone_id,
                 geraet_name = excluded.geraet_name,
                 quelle = excluded.quelle,
                 aktualisiert = excluded.aktualisiert""",
            (geraet_id, zone_id, geraet_name, quelle,
             datetime.now().isoformat()),
        )
        await self._db.commit()

    async def hole_zuordnungen(self) -> dict[str, str]:
        """Laedt alle gespeicherten Geraet-Zone-Zuordnungen."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT geraet_id, zone_id FROM geraete_zuordnung"
        ) as cursor:
            return {row[0]: row[1] async for row in cursor}

    # --- T-0115: Live-Lauf-State (VentilSicherung-Recovery) ---

    async def setze_live_lauf_state(
        self,
        kanal: int,
        valve_id: str | None,
        geraet_id: str,
        zone_ids: list[str],
        dauer_sekunden: int,
        ausloser: str,
        gestartet_am: datetime,
    ) -> None:
        """Persistiert den State einer aktiven Live-Bewaesserung (UPSERT)."""
        import json
        assert self._db is not None
        await self._db.execute(
            """INSERT INTO live_lauf_state
               (geraet_id, kanal, valve_id, zone_ids_json, dauer_sekunden,
                ausloser, gestartet_am)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(geraet_id, kanal) DO UPDATE SET
                 valve_id = excluded.valve_id,
                 zone_ids_json = excluded.zone_ids_json,
                 dauer_sekunden = excluded.dauer_sekunden,
                 ausloser = excluded.ausloser,
                 gestartet_am = excluded.gestartet_am""",
            (geraet_id, kanal, valve_id, json.dumps(zone_ids),
             dauer_sekunden, ausloser, gestartet_am.isoformat()),
        )
        if not self._in_transaktion:
            await self._db.commit()

    async def loesche_live_lauf_state(
        self, kanal: int, geraet_id: str | None = None,
    ) -> None:
        """Entfernt den State eines physischen Kanals.

        `geraet_id` ist fuer Multi-DSWC der fachliche Schluessel. Ohne
        Geraet bleibt ein Backward-Compat-Pfad fuer alte Tests/Mocks.
        Produktivcode soll immer `(geraet_id, kanal)` loeschen.
        """
        assert self._db is not None
        if geraet_id is None:
            await self._db.execute(
                "DELETE FROM live_lauf_state WHERE kanal = ?", (kanal,),
            )
        else:
            await self._db.execute(
                "DELETE FROM live_lauf_state WHERE geraet_id = ? AND kanal = ?",
                (geraet_id, kanal),
            )
        if not self._in_transaktion:
            await self._db.commit()

    async def hole_live_lauf_states(
        self, geraet_id: str | None = None,
    ) -> list[dict]:
        """Laedt persistierte Live-Lauf-States (fuer Recovery/Safety).

        Mit `geraet_id` wird exakt eine DSWC gelesen. Ohne Filter bleibt die
        globale Sicht fuer Diagnose- und Heuristik-Pfade erhalten.
        """
        import json
        assert self._db is not None
        if geraet_id is None:
            sql = """SELECT kanal, valve_id, geraet_id, zone_ids_json,
                            dauer_sekunden, ausloser, gestartet_am
                     FROM live_lauf_state"""
            params: tuple = ()
        else:
            sql = """SELECT kanal, valve_id, geraet_id, zone_ids_json,
                            dauer_sekunden, ausloser, gestartet_am
                     FROM live_lauf_state
                     WHERE geraet_id = ?"""
            params = (geraet_id,)
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()
        return [
            {
                "kanal": z["kanal"],
                "valve_id": z["valve_id"],
                "geraet_id": z["geraet_id"],
                "zone_ids": json.loads(z["zone_ids_json"]),
                "dauer_sekunden": z["dauer_sekunden"],
                "ausloser": z["ausloser"],
                "gestartet_am": datetime.fromisoformat(z["gestartet_am"]),
            }
            for z in zeilen
        ]

    # --- T-0116: Pre-Soak-State (PreSoakManager-Recovery) ---

    async def setze_pre_soak_state(
        self,
        zone_id: str,
        kanal: int,
        zone_ids_kanal: list[str],
        pre_soak_s: int,
        pause_s: int,
        haupt_s: int,
        gestartet_am: datetime,
        phase: str,
        ausloser: str = "manuell",
        haupt_pulse: int = 1,
        haupt_pause_s: int = 0,
        haupt_pulse_gestartet: int = 0,
    ) -> None:
        """Persistiert den State einer aktiven Pre-Soak-Sequenz (UPSERT).

        T-0437: `haupt_pulse`/`haupt_pause_s`/`haupt_pulse_gestartet` gehoeren
        zwingend dazu -- ohne den Zaehler wuerde ein Restart mitten in der
        Sequenz alle bereits gelaufenen Pulse erneut giessen.
        """
        import json
        assert self._db is not None
        await self._db.execute(
            """INSERT INTO pre_soak_state
               (zone_id, kanal, zone_ids_kanal_json, pre_soak_s, pause_s,
                haupt_s, gestartet_am, phase, ausloser,
                haupt_pulse, haupt_pause_s, haupt_pulse_gestartet)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(zone_id) DO UPDATE SET
                 kanal = excluded.kanal,
                 zone_ids_kanal_json = excluded.zone_ids_kanal_json,
                 pre_soak_s = excluded.pre_soak_s,
                 pause_s = excluded.pause_s,
                 haupt_s = excluded.haupt_s,
                 gestartet_am = excluded.gestartet_am,
                 phase = excluded.phase,
                 ausloser = excluded.ausloser,
                 haupt_pulse = excluded.haupt_pulse,
                 haupt_pause_s = excluded.haupt_pause_s,
                 haupt_pulse_gestartet = excluded.haupt_pulse_gestartet""",
            (zone_id, kanal, json.dumps(zone_ids_kanal),
             pre_soak_s, pause_s, haupt_s, gestartet_am.isoformat(), phase,
             ausloser, haupt_pulse, haupt_pause_s, haupt_pulse_gestartet),
        )
        if not self._in_transaktion:
            await self._db.commit()

    async def loesche_pre_soak_state(self, zone_id: str) -> None:
        """Entfernt den State einer Zone (nach fertig/fehler)."""
        assert self._db is not None
        await self._db.execute(
            "DELETE FROM pre_soak_state WHERE zone_id = ?", (zone_id,),
        )
        if not self._in_transaktion:
            await self._db.commit()

    # --- T-0122: Empfehlungs-Audit-Log ---

    async def setze_empfehlungs_audit(
        self,
        zeitstempel: datetime,
        zone_id: str,
        empfehlungs_typ: str,
        soll_bewaessern: bool,
        blocker_typ: str | None,
        feuchte_aktuell: float | None,
        welkepunkt_wert: float | None,
        optimum_min: float | None,
        optimum_max: float | None,
        prognose_quelle: str | None,
        prognose_6h: float | None,
        prognose_12h: float | None,
        prognose_24h: float | None,
        tage_bis_welkepunkt: float | None,
        dauer_s_empfehlung: int | None,
        aktive_strategie: str | None,
        # T-0270 (28.05.): Hybrid Stufe 1 Physik-Diagnose mitloggen.
        # Optional und default None, damit alle bestehenden Aufrufer
        # backward-kompatibel bleiben.
        prognose_physik_6h: float | None = None,
        prognose_physik_12h: float | None = None,
        prognose_physik_24h: float | None = None,
        physik_quelle: str | None = None,
        k_basis_pro_h: float | None = None,
        # T-0353/T-0351: State-Space-Shadow + Heuristik-Shadow + Routing.
        # Optional + default None (Backward-Compat wie die Physik-Felder).
        prognose_statespace_6h: float | None = None,
        prognose_statespace_12h: float | None = None,
        prognose_statespace_24h: float | None = None,
        statespace_quelle: str | None = None,
        prognose_heuristik_24h: float | None = None,
        routing_quelle: str | None = None,
    ) -> None:
        """Schreibt einen Empfehlungs-Snapshot. Audit-Felder bleiben NULL
        bis der Eval-Tick sie nach 6 / 24 h ausfuellt."""
        assert self._db is not None
        await self._db.execute(
            """INSERT INTO empfehlungs_audit
               (zeitstempel, zone_id, empfehlungs_typ, soll_bewaessern,
                blocker_typ, feuchte_aktuell, welkepunkt_wert,
                optimum_min, optimum_max, prognose_quelle,
                prognose_6h, prognose_12h, prognose_24h,
                tage_bis_welkepunkt, dauer_s_empfehlung, aktive_strategie,
                prognose_physik_6h, prognose_physik_12h,
                prognose_physik_24h, physik_quelle, k_basis_pro_h,
                prognose_statespace_6h, prognose_statespace_12h,
                prognose_statespace_24h, statespace_quelle,
                prognose_heuristik_24h, routing_quelle)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                zeitstempel.isoformat(), zone_id, empfehlungs_typ,
                1 if soll_bewaessern else 0, blocker_typ,
                feuchte_aktuell, welkepunkt_wert, optimum_min, optimum_max,
                prognose_quelle, prognose_6h, prognose_12h, prognose_24h,
                tage_bis_welkepunkt, dauer_s_empfehlung, aktive_strategie,
                prognose_physik_6h, prognose_physik_12h,
                prognose_physik_24h, physik_quelle, k_basis_pro_h,
                prognose_statespace_6h, prognose_statespace_12h,
                prognose_statespace_24h, statespace_quelle,
                prognose_heuristik_24h, routing_quelle,
            ),
        )
        if not self._in_transaktion:
            await self._db.commit()

    async def hole_offene_empfehlungs_audits(
        self, jetzt: datetime, max_alter_h: int = 36,
    ) -> list[dict]:
        """Liefert Audit-Eintraege, die noch evaluiert werden muessen.

        Filter: `evaluiert_am IS NULL` UND zeitstempel zwischen
        `jetzt - max_alter_h` und `jetzt - 6h` (= mindestens der 6h-Wert
        ist bereits messbar). Audits aelter als max_alter_h werden
        ueber den 24h-Horizont evaluiert oder mit ist_*=NULL geschlossen.
        """
        assert self._db is not None
        von = (jetzt - timedelta(hours=max_alter_h)).isoformat()
        bis = (jetzt - timedelta(hours=6)).isoformat()
        async with self._db.execute(
            """SELECT id, zeitstempel, zone_id, prognose_6h, prognose_24h,
                      prognose_physik_6h, prognose_physik_24h,
                      prognose_statespace_6h, prognose_statespace_24h,
                      prognose_heuristik_24h
               FROM empfehlungs_audit
               WHERE evaluiert_am IS NULL
                 AND zeitstempel >= ? AND zeitstempel <= ?
               ORDER BY zeitstempel ASC""",
            (von, bis),
        ) as cursor:
            zeilen = await cursor.fetchall()
        return [
            {
                "id": z["id"],
                "zeitstempel": datetime.fromisoformat(z["zeitstempel"]),
                "zone_id": z["zone_id"],
                "prognose_6h": z["prognose_6h"],
                "prognose_24h": z["prognose_24h"],
                # T-0270: Physik-Prognose mitreichen, damit der Eval-Tick
                # die Physik-Abweichung mitberechnen kann.
                "prognose_physik_6h": z["prognose_physik_6h"],
                "prognose_physik_24h": z["prognose_physik_24h"],
                # T-0353/T-0351: Shadow-Prognosen fuer die Abweichungs-
                # Berechnung im Eval-Tick.
                "prognose_statespace_6h": z["prognose_statespace_6h"],
                "prognose_statespace_24h": z["prognose_statespace_24h"],
                "prognose_heuristik_24h": z["prognose_heuristik_24h"],
            }
            for z in zeilen
        ]

    async def aktualisiere_empfehlungs_audit_eval(
        self, audit_id: int,
        ist_feuchte_6h: float | None,
        ist_feuchte_24h: float | None,
        abweichung_6h: float | None,
        abweichung_24h: float | None,
        evaluiert_am: datetime,
        # T-0270: optionale Physik-Abweichungen.
        abweichung_physik_6h: float | None = None,
        abweichung_physik_24h: float | None = None,
        # T-0353/T-0351: optionale Shadow-Abweichungen.
        abweichung_statespace_6h: float | None = None,
        abweichung_statespace_24h: float | None = None,
        abweichung_heuristik_24h: float | None = None,
    ) -> None:
        """Schreibt die Audit-Felder. Wenn 24h noch nicht messbar:
        ist_feuchte_24h=None, evaluiert_am bleibt gesetzt — der
        24h-Nachzug (`hole_offene_24h_nachzuege`) holt die Zeile spaeter
        noch einmal ab."""
        assert self._db is not None
        await self._db.execute(
            """UPDATE empfehlungs_audit
               SET ist_feuchte_6h = ?, ist_feuchte_24h = ?,
                   abweichung_6h = ?, abweichung_24h = ?,
                   abweichung_physik_6h = ?, abweichung_physik_24h = ?,
                   abweichung_statespace_6h = ?, abweichung_statespace_24h = ?,
                   abweichung_heuristik_24h = ?,
                   evaluiert_am = ?
               WHERE id = ?""",
            (
                ist_feuchte_6h, ist_feuchte_24h,
                abweichung_6h, abweichung_24h,
                abweichung_physik_6h, abweichung_physik_24h,
                abweichung_statespace_6h, abweichung_statespace_24h,
                abweichung_heuristik_24h,
                evaluiert_am.isoformat(), audit_id,
            ),
        )
        if not self._in_transaktion:
            await self._db.commit()

    async def hole_offene_24h_nachzuege(
        self, jetzt: datetime, max_alter_h: int = 72,
    ) -> list[dict]:
        """T-0349-Nachzug: Zeilen, die beim 6h-Eval abgeschlossen wurden
        (T-0264-Semantik: evaluiert_am gesetzt), deren 24h-Ist-Wert aber
        noch fehlt. Ohne diesen Nachzug bleibt `ist_feuchte_24h` fuer
        immer NULL (Befund 01.07.: 0 Zeilen mit 24h-Ist bei
        waldblumenhain) und der 24h-Vergleich ML/Physik/State-Space ist
        strukturell unmoeglich.

        Bewusst eigene Query + eigener UPDATE
        (`aktualisiere_empfehlungs_audit_eval_24h`) statt Wiederverwendung
        des 6h-Eval-Pfads — der wuerde die 6h-Felder ueberschreiben.
        """
        assert self._db is not None
        von = (jetzt - timedelta(hours=max_alter_h)).isoformat()
        bis = (jetzt - timedelta(hours=24)).isoformat()
        async with self._db.execute(
            """SELECT id, zeitstempel, zone_id, prognose_24h,
                      prognose_physik_24h, prognose_statespace_24h,
                      prognose_heuristik_24h
               FROM empfehlungs_audit
               WHERE evaluiert_am IS NOT NULL
                 AND ist_feuchte_24h IS NULL
                 AND zeitstempel >= ? AND zeitstempel <= ?
               ORDER BY zeitstempel ASC""",
            (von, bis),
        ) as cursor:
            zeilen = await cursor.fetchall()
        return [
            {
                "id": z["id"],
                "zeitstempel": datetime.fromisoformat(z["zeitstempel"]),
                "zone_id": z["zone_id"],
                "prognose_24h": z["prognose_24h"],
                "prognose_physik_24h": z["prognose_physik_24h"],
                "prognose_statespace_24h": z["prognose_statespace_24h"],
                "prognose_heuristik_24h": z["prognose_heuristik_24h"],
            }
            for z in zeilen
        ]

    async def aktualisiere_empfehlungs_audit_eval_24h(
        self, audit_id: int,
        ist_feuchte_24h: float,
        abweichung_24h: float | None,
        abweichung_physik_24h: float | None,
        abweichung_statespace_24h: float | None,
        abweichung_heuristik_24h: float | None,
    ) -> None:
        """24h-Nachzug: schreibt NUR die 24h-Felder. `evaluiert_am` und
        alle 6h-Felder bleiben unangetastet (Verifier-Finding 01.07.:
        der 6h-Eval-UPDATE wuerde sie auf NULL ueberschreiben)."""
        assert self._db is not None
        await self._db.execute(
            """UPDATE empfehlungs_audit
               SET ist_feuchte_24h = ?, abweichung_24h = ?,
                   abweichung_physik_24h = ?, abweichung_statespace_24h = ?,
                   abweichung_heuristik_24h = ?
               WHERE id = ?""",
            (
                ist_feuchte_24h, abweichung_24h, abweichung_physik_24h,
                abweichung_statespace_24h, abweichung_heuristik_24h,
                audit_id,
            ),
        )
        if not self._in_transaktion:
            await self._db.commit()

    async def hole_regime_stempel_kandidaten(
        self, jetzt: datetime, max_alter_tage: int = 14,
    ) -> list[dict]:
        """T-0349: Audit-Zeilen, deren Regime-Spalten noch fehlen.

        Regime-Stempeln ist vom Ist-Eval ENTKOPPELT (Verifier-Finding
        01.07.): das wetter_archiv (ERA5) laeuft ~5 Tage hinterher —
        zur Eval-Zeit (+6h/+24h) waere die Regen-Achse nie beurteilbar
        und jede Zeile wuerde als `regen_unbekannt` versteinern. Der
        Stempel-Pass klassifiziert daher rueckwirkend, sobald das Archiv
        das jeweilige Fenster abdeckt; Zeilen aelter `max_alter_tage`
        bleiben ungestempelt (bounded work, im Backtest offline
        klassifizierbar)."""
        assert self._db is not None
        von = (jetzt - timedelta(days=max_alter_tage)).isoformat()
        bis = (jetzt - timedelta(hours=6)).isoformat()
        async with self._db.execute(
            """SELECT id, zeitstempel, zone_id, regime_6h, regime_24h
               FROM empfehlungs_audit
               WHERE (regime_6h IS NULL OR regime_24h IS NULL)
                 AND zeitstempel >= ? AND zeitstempel <= ?
               ORDER BY zone_id, zeitstempel ASC""",
            (von, bis),
        ) as cursor:
            zeilen = await cursor.fetchall()
        return [
            {
                "id": z["id"],
                "zeitstempel": datetime.fromisoformat(z["zeitstempel"]),
                "zone_id": z["zone_id"],
                "regime_6h": z["regime_6h"],
                "regime_24h": z["regime_24h"],
            }
            for z in zeilen
        ]

    async def setze_empfehlungs_audit_regime(
        self, audit_id: int,
        regime_6h: str | None = None,
        regime_24h: str | None = None,
    ) -> None:
        """Stempelt Regime-Spalten (nur die uebergebenen, None = lassen)."""
        assert self._db is not None
        sets: list[str] = []
        params: list = []
        if regime_6h is not None:
            sets.append("regime_6h = ?")
            params.append(regime_6h)
        if regime_24h is not None:
            sets.append("regime_24h = ?")
            params.append(regime_24h)
        if not sets:
            return
        params.append(audit_id)
        await self._db.execute(
            f"UPDATE empfehlungs_audit SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        if not self._in_transaktion:
            await self._db.commit()

    async def hole_empfehlungs_audit(
        self, zone_id: str | None = None, tage: int = 7, limit: int = 500,
        jetzt: datetime | None = None,
    ) -> list[dict]:
        """API-Endpoint-Helfer: liefert die letzten Audit-Zeilen.

        T-0166-Fix (2026-05-13): `jetzt` als optionales Argument, damit
        Tests Tageszeit-deterministisch laufen und Watchdog/andere
        Aufrufer mit konsistenter Referenzzeit arbeiten. Default
        `datetime.now()` fuer den API-Endpoint-Pfad.
        """
        assert self._db is not None
        von = ((jetzt or datetime.now()) - timedelta(days=tage)).isoformat()
        if zone_id:
            sql = """SELECT * FROM empfehlungs_audit
                     WHERE zone_id = ? AND zeitstempel >= ?
                     ORDER BY zeitstempel DESC LIMIT ?"""
            params: tuple = (zone_id, von, limit)
        else:
            sql = """SELECT * FROM empfehlungs_audit
                     WHERE zeitstempel >= ?
                     ORDER BY zeitstempel DESC LIMIT ?"""
            params = (von, limit)
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()
        return [dict(z) for z in zeilen]

    async def hole_empfehlungs_audit_stats_pro_zone(
        self, tage: int = 30, jetzt: datetime | None = None,
    ) -> dict[str, dict]:
        """T-0236-Bug-Fix (2026-05-25): pro-Zone-Aggregat unabhaengig
        vom Eintrags-Limit von `hole_empfehlungs_audit`.

        Frontend brauchte vorher die selbst-berechnete MAE aus dem
        500er-Eintrags-Limit -- bei 14 Zonen reichte das nur fuer ~1.5
        Tage und ignorierte den Fenster-Toggle. Diese Methode macht
        das Aggregat per SQL ohne Limit, korrekt ueber den ganzen
        `tage`-Zeitraum.

        Rueckgabe: {zone_id: {n, n_evaluiert, mae_6h_pp, mae_24h_pp,
        typ_verteilung}}. typ_verteilung ist {empfehlungs_typ: count}.
        """
        assert self._db is not None
        von = ((jetzt or datetime.now()) - timedelta(days=tage)).isoformat()

        sql = """SELECT zone_id,
                        COUNT(*) AS n,
                        SUM(CASE WHEN evaluiert_am IS NOT NULL
                            THEN 1 ELSE 0 END) AS n_evaluiert,
                        AVG(CASE WHEN abweichung_6h IS NOT NULL
                            THEN ABS(abweichung_6h) END) AS mae_6h_pp,
                        AVG(CASE WHEN abweichung_24h IS NOT NULL
                            THEN ABS(abweichung_24h) END) AS mae_24h_pp
                   FROM empfehlungs_audit
                  WHERE zeitstempel >= ?
                  GROUP BY zone_id"""
        async with self._db.execute(sql, (von,)) as cursor:
            agg_zeilen = await cursor.fetchall()

        # typ_verteilung separat (sonst nicht in einem GROUP-BY-Schritt).
        sql_typ = """SELECT zone_id, empfehlungs_typ, COUNT(*) AS n
                       FROM empfehlungs_audit
                      WHERE zeitstempel >= ?
                   GROUP BY zone_id, empfehlungs_typ"""
        async with self._db.execute(sql_typ, (von,)) as cursor:
            typ_zeilen = await cursor.fetchall()

        typ_pro_zone: dict[str, dict[str, int]] = {}
        for z in typ_zeilen:
            zid = str(z["zone_id"])
            typ = str(z["empfehlungs_typ"] or "unbekannt")
            typ_pro_zone.setdefault(zid, {})[typ] = int(z["n"])

        ergebnis: dict[str, dict] = {}
        for z in agg_zeilen:
            zid = str(z["zone_id"])
            mae6 = z["mae_6h_pp"]
            mae24 = z["mae_24h_pp"]
            ergebnis[zid] = {
                "n": int(z["n"]),
                "n_evaluiert": int(z["n_evaluiert"] or 0),
                "mae_6h_pp": round(float(mae6), 2) if mae6 is not None else None,
                "mae_24h_pp": round(float(mae24), 2) if mae24 is not None else None,
                "typ_verteilung": typ_pro_zone.get(zid, {}),
            }
        return ergebnis

    async def hole_letzten_watchdog_push(
        self, typ: str, zone_id: str = "_global",
    ) -> datetime | None:
        """T-0126 (H-2): liefert den Zeitstempel des letzten Pushs fuer
        eine Trigger-Klasse + Zone. None wenn nie gesendet.
        """
        assert self._db is not None
        async with self._db.execute(
            "SELECT zuletzt_gesendet FROM watchdog_event WHERE typ = ? AND zone_id = ?",
            (typ, zone_id),
        ) as cursor:
            zeile = await cursor.fetchone()
        if zeile is None:
            return None
        return datetime.fromisoformat(zeile["zuletzt_gesendet"])

    async def setze_watchdog_push(
        self, typ: str, zone_id: str, jetzt: datetime,
    ) -> None:
        """T-0126 (H-2): UPSERT fuer Throttle-State."""
        assert self._db is not None
        await self._db.execute(
            """INSERT INTO watchdog_event (typ, zone_id, zuletzt_gesendet)
               VALUES (?, ?, ?)
               ON CONFLICT(typ, zone_id) DO UPDATE SET
                   zuletzt_gesendet = excluded.zuletzt_gesendet""",
            (typ, zone_id, jetzt.isoformat()),
        )
        await self._db.commit()

    async def setze_endpoint_health(
        self,
        endpoint: str,
        status: str,
        jetzt: datetime,
        details: str = "",
        letzter_erfolg: datetime | None = None,
    ) -> None:
        """T-0132 (H-8): UPSERT Endpoint-Health-Status.

        Bei status='ok' wird `letzter_erfolg` immer auf `jetzt` gesetzt --
        Caller darf das Param weglassen. Bei Fehler-Status wird der
        bestehende `letzter_erfolg` beibehalten (oder explizit ueberschrieben).
        """
        assert self._db is not None
        if status == "ok":
            le = jetzt.isoformat()
        elif letzter_erfolg is not None:
            le = letzter_erfolg.isoformat()
        else:
            # Bestehenden Wert beibehalten
            async with self._db.execute(
                "SELECT letzter_erfolg FROM endpoint_health WHERE endpoint = ?",
                (endpoint,),
            ) as cursor:
                zeile = await cursor.fetchone()
            le = zeile["letzter_erfolg"] if zeile and zeile["letzter_erfolg"] else None
        await self._db.execute(
            """INSERT INTO endpoint_health
               (endpoint, letzte_pruefung, letzter_erfolg, status, details)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET
                   letzte_pruefung = excluded.letzte_pruefung,
                   letzter_erfolg = COALESCE(excluded.letzter_erfolg,
                                             endpoint_health.letzter_erfolg),
                   status = excluded.status,
                   details = excluded.details""",
            (endpoint, jetzt.isoformat(), le, status, details),
        )
        await self._db.commit()

    async def hole_endpoint_health(
        self, endpoint: str | None = None,
    ) -> list[dict]:
        """T-0132 (H-8): liefert Endpoint-Health-Eintraege als Dicts."""
        assert self._db is not None
        if endpoint:
            sql = "SELECT * FROM endpoint_health WHERE endpoint = ?"
            params: tuple = (endpoint,)
        else:
            sql = "SELECT * FROM endpoint_health"
            params = ()
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()
        return [dict(z) for z in zeilen]

    async def hole_betriebsstatus_aggregate(
        self, jetzt: datetime | None = None,
    ) -> dict:
        """T-0238: Aggregierte Status-Metriken fuer die Betriebsstatus-Zentrale.

        Bundelt drei Read-Pfade in einer Methode:
        - Husqvarna-Cadence: MAX(zeitstempel) aus sensor_messung mit
          quelle='gardena' + Count letzte 24h (zeigt ob die DHS-Pipeline
          live ist + wie aktiv sie liefert).
        - Letztes Watchdog-Event: MAX(zeitstempel) aus watchdog_event +
          trigger-Typ (zeigt ob Push-Kanal in letzter Zeit gefeuert hat).
        - Zaehlung offener Sensor-Warnungen.

        Liefert dict; das `/api/ops/betriebsstatus`-Endpoint kombiniert
        das mit dem Endpoint-Health + Filesystem-Backup-Info + ML-Service-
        Status.
        """
        assert self._db is not None
        ende = jetzt or datetime.now()
        seit_24h = (ende - timedelta(hours=24)).isoformat()

        # Husqvarna-Cadence
        async with self._db.execute(
            """SELECT MAX(zeitstempel) AS letzter,
                      COUNT(*) FILTER (WHERE zeitstempel >= ?) AS letzte_24h
               FROM sensor_messung
               WHERE quelle = 'gardena'""",
            (seit_24h,),
        ) as cursor:
            husqvarna = await cursor.fetchone()

        # Letztes Watchdog-Event (egal welcher Trigger -- Push hat
        # stattgefunden, wenn ueberhaupt ein Eintrag da ist).
        # Tabellen-Schema: (typ, zone_id, zuletzt_gesendet).
        async with self._db.execute(
            """SELECT zuletzt_gesendet, typ, zone_id
               FROM watchdog_event
               ORDER BY zuletzt_gesendet DESC LIMIT 1"""
        ) as cursor:
            wd_row = await cursor.fetchone()

        # Offene Sensor-Warnungen
        async with self._db.execute(
            """SELECT COUNT(*) AS n
               FROM sensor_warnung
               WHERE behoben_um IS NULL"""
        ) as cursor:
            offene_warnungen = int((await cursor.fetchone())["n"] or 0)

        return {
            "husqvarna_letzter_beat": husqvarna["letzter"],
            "husqvarna_beats_24h": int(husqvarna["letzte_24h"] or 0),
            "letztes_watchdog_event_zeit": (
                wd_row["zuletzt_gesendet"] if wd_row else None
            ),
            "letztes_watchdog_event_trigger": (
                wd_row["typ"] if wd_row else None
            ),
            "letztes_watchdog_event_zone": (
                wd_row["zone_id"] if wd_row else None
            ),
            "offene_sensor_warnungen": offene_warnungen,
        }

    async def letzter_gardena_beat(self) -> datetime | None:
        """T-0287: Zeitstempel der neuesten Gardena-Messung (live ODER DHS-
        Backfill -- beide `quelle='gardena'`). Proxy fuer 'sind wir mit
        Gardena in Kontakt'. Gleiche Query-Semantik wie der
        `husqvarna_letzter_beat` aus `hole_betriebsstatus_aggregate`
        (Single Source).

        Genutzt vom Frische-Gate in `VentilSicherung.bewaessere` (nur
        Automatik): ist der Wert zu alt (Laptop-Schlaf, WS-Verlust), darf
        der Auto-Loop NICHT auf blinden Live-Zustand schalten.
        """
        assert self._db is not None
        async with self._db.execute(
            "SELECT MAX(zeitstempel) AS letzter FROM sensor_messung "
            "WHERE quelle = 'gardena'"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["letzter"] is None:
            return None
        try:
            return datetime.fromisoformat(row["letzter"])
        except (ValueError, TypeError):
            return None

    # --- T-0228 Stufe 2 Wartungs-Fenster -------------------------------------

    async def starte_wartungs_fenster(
        self,
        zone_id: str,
        grund: str = "",
        jetzt: datetime | None = None,
    ) -> int:
        """Eroffnet ein Wartungs-Fenster fuer eine Zone. Returnt die id.

        Wenn die Zone schon ein offenes Fenster hat (`bis_am IS NULL`),
        wird KEIN neues angelegt -- die existierende id wird zurueck-
        gegeben (idempotenter Start, schuetzt vor Doppel-Klicks).
        """
        assert self._db is not None
        jetzt = jetzt or datetime.now()

        async def _op():
            assert self._db is not None
            async with self._db.execute(
                """SELECT id FROM wartungs_fenster
                   WHERE zone_id = ? AND bis_am IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (zone_id,),
            ) as cur:
                offen = await cur.fetchone()
            if offen is not None:
                return int(offen["id"])
            cursor = await self._db.execute(
                """INSERT INTO wartungs_fenster
                   (zone_id, von_am, grund, angelegt_am)
                   VALUES (?, ?, ?, ?)""",
                (zone_id, jetzt.isoformat(), grund, jetzt.isoformat()),
            )
            neue_id = int(cursor.lastrowid or 0)
            await self._db.commit()
            return neue_id

        return await self._mit_lock_retry(
            _op, label="starte_wartungs_fenster",
        )

    async def beende_wartungs_fenster(
        self,
        fenster_id: int,
        jetzt: datetime | None = None,
    ) -> bool:
        """Schliesst ein Wartungs-Fenster (`bis_am = jetzt`). Returnt
        True wenn es ein offenes Fenster war, False wenn schon zu /
        nicht gefunden (idempotenter Stop)."""
        assert self._db is not None
        jetzt = jetzt or datetime.now()

        async def _op():
            assert self._db is not None
            cursor = await self._db.execute(
                """UPDATE wartungs_fenster SET bis_am = ?
                   WHERE id = ? AND bis_am IS NULL""",
                (jetzt.isoformat(), fenster_id),
            )
            geaendert = cursor.rowcount > 0
            await self._db.commit()
            return geaendert

        return await self._mit_lock_retry(
            _op, label="beende_wartungs_fenster",
        )

    async def hole_wartungs_fenster(
        self,
        nur_offen: bool = True,
        zone_id: str | None = None,
    ) -> list[dict]:
        """Liefert Wartungs-Fenster. Default: nur offene (bis_am IS NULL)."""
        assert self._db is not None
        bedingungen: list[str] = []
        params: list = []
        if nur_offen:
            bedingungen.append("bis_am IS NULL")
        if zone_id is not None:
            bedingungen.append("zone_id = ?")
            params.append(zone_id)
        sql = "SELECT * FROM wartungs_fenster"
        if bedingungen:
            sql += " WHERE " + " AND ".join(bedingungen)
        sql += " ORDER BY von_am DESC"
        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()
        return [dict(z) for z in zeilen]

    # --- T-0228 Pflege-/Wartungs-Erinnerungen --------------------------------

    async def speichere_pflege_erinnerung(
        self,
        typ: str,
        faellig_am: datetime,
        beschreibung: str = "",
        zone_id: str | None = None,
        intervall_tage: int | None = None,
        quelle: str = "manuell",
        jetzt: datetime | None = None,
    ) -> int:
        """Legt eine neue Pflege-Erinnerung an. Returnt die DB-id."""
        assert self._db is not None
        jetzt = jetzt or datetime.now()

        async def _op():
            assert self._db is not None
            cursor = await self._db.execute(
                """INSERT INTO pflege_erinnerung
                   (zone_id, typ, faellig_am, intervall_tage, beschreibung,
                    quelle, angelegt_am)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    zone_id, typ, faellig_am.isoformat(), intervall_tage,
                    beschreibung, quelle, jetzt.isoformat(),
                ),
            )
            # lastrowid VOR commit lesen -- in aiosqlite ist der
            # Wert nach commit nicht mehr verfuegbar (siehe
            # speicher.py:3105-Pattern).
            neue_id = int(cursor.lastrowid or 0)
            await self._db.commit()
            return neue_id

        return await self._mit_lock_retry(
            _op, label="speichere_pflege_erinnerung",
        )

    async def hole_pflege_erinnerungen(
        self,
        nur_offen: bool = True,
        anstehend_tage: int | None = None,
        zone_id: str | None = None,
        jetzt: datetime | None = None,
    ) -> list[dict]:
        """Liefert Pflege-Erinnerungen.

        - `nur_offen=True` (Default): erledigte ausblenden.
        - `anstehend_tage=N`: nur Erinnerungen mit `faellig_am <= jetzt + N`.
          None = alle offenen ohne Datums-Cap. Sinnvoll fuer das
          "Anstehend"-Widget mit z. B. 3 Tagen Vorlauf.
        - `zone_id`: optional einschraenken.
        """
        assert self._db is not None
        jetzt = jetzt or datetime.now()
        bedingungen: list[str] = []
        params: list = []
        if nur_offen:
            bedingungen.append("erledigt_am IS NULL")
        if anstehend_tage is not None:
            from datetime import timedelta as _td
            cap = (jetzt + _td(days=anstehend_tage)).isoformat()
            bedingungen.append("faellig_am <= ?")
            params.append(cap)
        if zone_id is not None:
            bedingungen.append("zone_id = ?")
            params.append(zone_id)

        sql = "SELECT * FROM pflege_erinnerung"
        if bedingungen:
            sql += " WHERE " + " AND ".join(bedingungen)
        sql += " ORDER BY faellig_am ASC"

        async with self._db.execute(sql, params) as cursor:
            zeilen = await cursor.fetchall()
        return [dict(z) for z in zeilen]

    async def erledige_pflege_erinnerung(
        self,
        eintrag_id: int,
        jetzt: datetime | None = None,
    ) -> dict | None:
        """Markiert eine Pflege-Erinnerung als erledigt. Bei
        wiederkehrenden Erinnerungen (`intervall_tage != NULL`) wird
        automatisch ein Folge-Eintrag mit `faellig_am += intervall_tage`
        angelegt. Returnt den Folge-Eintrag (oder None bei einmalig).
        """
        assert self._db is not None
        jetzt = jetzt or datetime.now()

        async def _op():
            assert self._db is not None
            async with self._db.execute(
                "SELECT * FROM pflege_erinnerung WHERE id = ?",
                (eintrag_id,),
            ) as cur:
                zeile = await cur.fetchone()
            if zeile is None:
                return None
            if zeile["erledigt_am"] is not None:
                return None  # schon erledigt, kein Folge-Eintrag

            await self._db.execute(
                "UPDATE pflege_erinnerung SET erledigt_am = ? WHERE id = ?",
                (jetzt.isoformat(), eintrag_id),
            )

            folge_id = None
            if zeile["intervall_tage"]:
                from datetime import timedelta as _td
                naechstes_datum = datetime.fromisoformat(
                    zeile["faellig_am"],
                ) + _td(days=int(zeile["intervall_tage"]))
                cursor = await self._db.execute(
                    """INSERT INTO pflege_erinnerung
                       (zone_id, typ, faellig_am, intervall_tage,
                        beschreibung, quelle, angelegt_am)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        zeile["zone_id"], zeile["typ"],
                        naechstes_datum.isoformat(),
                        zeile["intervall_tage"], zeile["beschreibung"],
                        zeile["quelle"], jetzt.isoformat(),
                    ),
                )
                folge_id = int(cursor.lastrowid or 0) or None

            await self._db.commit()
            if folge_id is None:
                return None
            async with self._db.execute(
                "SELECT * FROM pflege_erinnerung WHERE id = ?",
                (folge_id,),
            ) as cur:
                folge = await cur.fetchone()
            return dict(folge) if folge else None

        return await self._mit_lock_retry(
            _op, label="erledige_pflege_erinnerung",
        )

    async def hole_pre_soak_states(self) -> list[dict]:
        """Laedt alle persistierten Pre-Soak-States (fuer Recovery)."""
        import json
        assert self._db is not None
        async with self._db.execute(
            """SELECT zone_id, kanal, zone_ids_kanal_json, pre_soak_s,
                      pause_s, haupt_s, gestartet_am, phase, ausloser,
                      haupt_pulse, haupt_pause_s, haupt_pulse_gestartet
               FROM pre_soak_state"""
        ) as cursor:
            zeilen = await cursor.fetchall()
        return [
            {
                "zone_id": z["zone_id"],
                "kanal": z["kanal"],
                "zone_ids_kanal": json.loads(z["zone_ids_kanal_json"]),
                "pre_soak_s": z["pre_soak_s"],
                "pause_s": z["pause_s"],
                "haupt_s": z["haupt_s"],
                "gestartet_am": datetime.fromisoformat(z["gestartet_am"]),
                "phase": z["phase"],
                "ausloser": z["ausloser"],
                # T-0437: Defaults fuer Zeilen aus der Zeit vor der Migration.
                "haupt_pulse": z["haupt_pulse"] or 1,
                "haupt_pause_s": z["haupt_pause_s"] or 0,
                "haupt_pulse_gestartet": z["haupt_pulse_gestartet"] or 0,
            }
            for z in zeilen
        ]


def _zeile_zu_ventil_ereignis(z) -> VentilEreignis:
    """Parst eine SQLite-Row zu VentilEreignis. `liter`/`lauf_gruppe`/`phase`
    sind optional (Alt-DB vor der jeweiligen Migration)."""
    def _opt(spalte):
        try:
            return z[spalte]
        except (IndexError, KeyError):
            return None  # Alt-DB ohne Spalte (migriert kurz danach)
    return VentilEreignis(
        id=z["id"],
        zeitstempel=datetime.fromisoformat(z["zeitstempel"]),
        zone_id=z["zone_id"],
        ventil_id=z["ventil_id"],
        aktion=VentilAktion(z["aktion"]),
        dauer_sekunden=z["dauer_sekunden"],
        ausloser=Ausloser(z["ausloser"]),
        liter=_opt("liter"),
        lauf_gruppe=_opt("lauf_gruppe"),
        phase=_opt("phase"),
    )
