"""T-0238: /api/ops/betriebsstatus aggregiert System-Health-Daten an einer Stelle.

Vor T-0238 musste der User zwischen SQLite-CLI, Logs und mehreren Tabs
springen, um zu sehen, ob Endpoint-Health/Backup/Push/Husqvarna/ML-Retrain
in Ordnung sind. Plus T-0246-Scope: Backend-Version sichtbar im UI.

Regression-Schutz:
- Endpoint liefert alle erwarteten Top-Level-Schluessel.
- Endpoint-Health-Eintraege werden durchgereicht.
- Husqvarna-Cadence wird aus sensor_messung berechnet (stale-Flag bei
  > 1 h ohne Beat).
- Backup-Filesystem-Read findet juengsten Snapshot + Count.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.modelle import (
    BackupKonfig,
    DatenQuelle,
    GardenaKonfig,
    GesamtKonfig,
    SensorMessung,
    SpeicherKonfig,
    StandortKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


def _konfig(backup_dir: str | None = None) -> GesamtKonfig:
    backup = BackupKonfig(
        verzeichnis=backup_dir or "./daten/backup",
        spiegel_verzeichnis="~/iCloud/Backup",
    )
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[
            ZonenKonfig(zone_id="z1", name="Z1", ventil_kanal=1),
            ZonenKonfig(zone_id="z2", name="Z2", ventil_kanal=2),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten",
                wetter_standort="o", zonen=["z1", "z2"],
            ),
        ],
        backup=backup,
    )


@pytest.fixture
def client_mit_status(tmp_path):
    speicher = Speicher(str(tmp_path / "status.db"))
    _run(speicher.verbinden())

    # Husqvarna-Beat vor 30 min (= nicht stale)
    _run(speicher.speichere_messung(SensorMessung(
        zeitstempel=datetime.now() - timedelta(minutes=30),
        zone_id="z1",
        geraet_id="g1",
        boden_feuchte=55.0,
        quelle=DatenQuelle.GARDENA,
    )))
    # Endpoint-Health-Eintrag
    _run(speicher.setze_endpoint_health(
        "fyta", "ok", datetime.now(), details="Token + Schema ok",
    ))

    # Backup-Snapshot anlegen
    backup_dir = tmp_path / "backup" / "taeglich"
    backup_dir.mkdir(parents=True)
    # T-0475: gemischter Bestand — Alt-Snapshot unkomprimiert, neuer als .db.gz.
    # mtime explizit setzen, sonst haengt "juengster" an der Aufloesung der Uhr.
    import os as _os
    alt = backup_dir / "bewaesserung_2026-05-24.db"
    neu = backup_dir / "bewaesserung_2026-05-25.db.gz"
    alt.write_bytes(b"y")
    neu.write_bytes(b"x")
    _os.utime(alt, (1_700_000_000, 1_700_000_000))
    _os.utime(neu, (1_700_086_400, 1_700_086_400))

    konfig = _konfig(backup_dir=str(tmp_path / "backup"))
    konfiguriere_api(speicher, konfig, MagicMock(), MagicMock())
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        _run(speicher.schliessen())


def test_betriebsstatus_alle_top_level_felder(client_mit_status):
    """Endpoint muss alle dokumentierten Schluessel liefern, auch ohne
    ML-Service (graceful)."""
    r = client_mit_status.get("/api/ops/betriebsstatus")
    assert r.status_code == 200
    body = r.json()
    erwartet = {
        "zeitstempel", "system", "endpoints", "husqvarna", "ml",
        "backup", "watchdog_letzter_push", "offene_sensor_warnungen",
    }
    assert set(body.keys()) >= erwartet


def test_betriebsstatus_system_block(client_mit_status):
    """T-0246-Scope: Version + zonen_anzahl + konfiguriert."""
    body = client_mit_status.get("/api/ops/betriebsstatus").json()
    assert body["system"]["konfiguriert"] is True
    assert body["system"]["zonen_anzahl"] == 2
    assert body["system"]["version"]  # FastAPI default


def test_betriebsstatus_endpoints_aus_db(client_mit_status):
    """Endpoint-Health-Eintraege werden durchgereicht."""
    body = client_mit_status.get("/api/ops/betriebsstatus").json()
    namen = [e["endpoint"] for e in body["endpoints"]]
    assert "fyta" in namen


def test_betriebsstatus_husqvarna_frisch_nicht_stale(client_mit_status):
    """Beat vor 30 min -> stale=False, beats_24h=1."""
    body = client_mit_status.get("/api/ops/betriebsstatus").json()
    assert body["husqvarna"]["stale"] is False
    assert body["husqvarna"]["beats_24h"] == 1
    assert body["husqvarna"]["letzter_beat"] is not None


def test_betriebsstatus_backup_findet_snapshots(client_mit_status):
    """Backup-Filesystem-Read liefert juengsten Snapshot + Count.

    T-0475: zaehlt beide Ablageformen. Ein Glob nur auf `*.db` haette
    nach der Umstellung auf `.db.gz` "kein Backup vorhanden" gemeldet.
    """
    body = client_mit_status.get("/api/ops/betriebsstatus").json()
    assert body["backup"]["snapshot_count"] == 2
    assert body["backup"]["letzter_snapshot"] == "bewaesserung_2026-05-25.db.gz"
    assert body["backup"]["spiegel_aktiv"] is True


def test_betriebsstatus_husqvarna_stale_bei_altem_beat(tmp_path):
    """Beat vor > 1 h -> stale=True."""
    speicher = Speicher(str(tmp_path / "stale.db"))
    _run(speicher.verbinden())
    _run(speicher.speichere_messung(SensorMessung(
        zeitstempel=datetime.now() - timedelta(hours=3),
        zone_id="z1", geraet_id="g1",
        boden_feuchte=55.0, quelle=DatenQuelle.GARDENA,
    )))
    konfiguriere_api(speicher, _konfig(), MagicMock(), MagicMock())
    client = TestClient(app)
    try:
        body = client.get("/api/ops/betriebsstatus").json()
        assert body["husqvarna"]["stale"] is True
        # Beat von vor 3 h -> nicht im 24h-Fenster gezaehlt?
        # Doch, 3 h liegt im 24h-Fenster.
        assert body["husqvarna"]["beats_24h"] == 1
    finally:
        client.close()
        _run(speicher.schliessen())


def test_betriebsstatus_ohne_ml_service_graceful(client_mit_status):
    """Ohne ML-Service liefert das `ml`-Feld `ist_geladen=False` ohne
    zu crashen."""
    body = client_mit_status.get("/api/ops/betriebsstatus").json()
    assert body["ml"]["ist_geladen"] is False


@pytest.fixture
def zone_berlin(monkeypatch):
    """Lokale Zone mit Versatz zu UTC erzwingen, sonst faellt ein als UTC
    etikettierter Lokalwert auf einer UTC-Maschine nicht auf (W13b)."""
    import time
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_t0471_job_zeitstempel_tragen_die_zeitzone(
    client_mit_status, tmp_path, zone_berlin,
):
    """T-0471: Job-Zeitstempel muessen ueber `_iso()` gehen, nicht roh raus.

    Gleiche Klasse wie T-0466 (`fehlerpattern_naive_utc_in_db`), anderer Pfad:
    `/api/ops/betriebsstatus` und `/api/ml/status` lieferten `trainiert_am` und
    die `letzter_erfolg`-Felder als rohes `.isoformat()` auf naiver
    Server-Zeit. Leser ist `relativDauer()` in `BetriebsstatusKachel.tsx`, die
    daraus "trainiert vor 3 h" rechnet; ohne Zeitzone verschiebt sich das um
    den UTC-Versatz.

    T-0610 AT-3: frueher ein Regex ueber den Quelltext von `api_server`. Jetzt
    bekommen ML-Service und alle sechs Jobs naive Zeitstempel, und JEDES
    Feld dieser Namen in beiden Antworten muss eine Zeitzone tragen.

    T-0610 W13b: zusaetzlich `/api/ml/status.trainiert_am`,
    `retrain.letzter_lauf` und `endpoints[].letzte_pruefung/letzter_erfolg`
    (Befund W11a). Fuer die naiv gesetzten Werte muss der Zeitpunkt der
    naiven Server-Lokalzeit entsprechen (nicht nur irgendeine Zone).
    """
    from types import SimpleNamespace

    from bewaesserung.ml.modelle_ml import ModellStatus

    naiv = datetime(2026, 7, 1, 12, 0)
    from bewaesserung import api_server
    _run(api_server._laufzeit.speicher.setze_endpoint_health(
        "dhs", "ok", naiv, details="W13b",
    ))
    ml = MagicMock()
    ml.ist_verfuegbar = True
    ml.status.return_value = ModellStatus(
        ist_geladen=True, trainiert_am=naiv, horizonte=[6],
    )
    cfg = SimpleNamespace(aktiv=True, gate_faktor=1.0, intervall_tage=7, adoptieren=False)

    def _job():
        return SimpleNamespace(
            _retrain=cfg, _resp=cfg, _kal=cfg, _sm=cfg, _physik=cfg, _wf=cfg,
            letzter_erfolg=naiv, letzter_fehler=None, letztes_ergebnis=None,
            _letzte_aktualisierung=naiv,
        )

    konfiguriere_api(
        api_server._laufzeit.speicher, api_server._laufzeit.konfig, MagicMock(), MagicMock(),
        None, ml,
        ml_retrain_job=_job(), ml_response_retrain_job=_job(),
        kalibrations_job=_job(), skalen_mapping_fit_job=_job(),
        k_basis_fit_job=_job(), wirkung_fit_job=_job(),
    )
    antworten = {}
    for route in ("/api/ops/betriebsstatus", "/api/ml/status"):
        antwort = client_mit_status.get(route)
        assert antwort.status_code == 200, (route, antwort.text[:300])
        antworten[route] = antwort.json()
    # Modell-Alter in der Betriebsstatus-Kachel, `letzter_erfolg` aller sechs
    # Jobs (frueherer Quelltext-Guard) und die drei W11a-Funde samt
    # `letzte_pruefung` aus demselben Endpoint-Health-Eintrag (W13b).
    status = antworten["/api/ml/status"]
    dhs = [e for e in antworten["/api/ops/betriebsstatus"]["endpoints"]
           if e["endpoint"] == "dhs"]
    assert len(dhs) == 1, antworten["/api/ops/betriebsstatus"]["endpoints"]
    felder = [("betriebsstatus.ml.trainiert_am",
               antworten["/api/ops/betriebsstatus"]["ml"]["trainiert_am"]),
              ("ml_status.trainiert_am", status["trainiert_am"]),
              ("ml_status.retrain.letzter_lauf", status["retrain"]["letzter_lauf"]),
              ("betriebsstatus.endpoints[dhs].letzte_pruefung",
               dhs[0]["letzte_pruefung"]),
              ("betriebsstatus.endpoints[dhs].letzter_erfolg",
               dhs[0]["letzter_erfolg"])]
    for block in ("retrain", "response_retrain", "kalibrierung",
                  "skalen_mapping", "physik_diagnose", "wirkung_fit"):
        felder.append((f"ml_status.{block}.letzter_erfolg",
                       status[block]["letzter_erfolg"]))
    assert all(wert is not None for _, wert in felder), felder
    ohne_zone = [(pfad, wert) for pfad, wert in felder
                 if datetime.fromisoformat(wert).tzinfo is None]
    assert ohne_zone == [], (
        f"Job-Zeitstempel ohne Zeitzone: {ohne_zone}. Ueber _iso() leiten, "
        "sonst rechnet der Client die Altersangabe falsch."
    )
    falscher_zeitpunkt = [(pfad, wert) for pfad, wert in felder
                          if datetime.fromisoformat(wert) != naiv.astimezone()]
    assert falscher_zeitpunkt == [], (
        f"Zeitstempel mit falscher Zone (naiv = Server-Lokalzeit): "
        f"{falscher_zeitpunkt}"
    )
    # Der Fixture-Eintrag `fyta` (datetime.now()) bekommt ebenfalls eine Zone.
    fyta = [e for e in antworten["/api/ops/betriebsstatus"]["endpoints"]
            if e["endpoint"] == "fyta"]
    assert len(fyta) == 1
    assert datetime.fromisoformat(fyta[0]["letzter_erfolg"]).tzinfo is not None
