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


def test_t0471_keine_nackten_isoformat_auf_job_zeitstempeln():
    """T-0471: Job-Zeitstempel muessen ueber `_iso()` gehen, nicht roh raus.

    Gleiche Klasse wie T-0466 (`fehlerpattern_naive_utc_in_db`), anderer Pfad:
    `/api/betriebsstatus` lieferte `trainiert_am` und die `letzter_erfolg`-
    Felder als rohes `.isoformat()` auf naiver Server-Zeit -- am `_iso()`-
    Helfer vorbei, der genau dafuer existiert. Leser ist `relativDauer()` in
    `BetriebsstatusKachel.tsx`, die daraus "trainiert vor 3 h" rechnet; ohne
    Zeitzone verschiebt sich das um den UTC-Versatz.

    Bewusst ein statischer Guard statt eines Einzelfall-Tests: er deckt die
    ganze Klasse ab und faengt auch die naechste Stelle, die jemand ergaenzt.
    """
    import inspect
    import re

    from bewaesserung import api_server

    quelle = inspect.getsource(api_server)
    # Zeitstempel-Felder, die aus Job-Objekten in die API-Antwort wandern.
    muster = re.compile(
        r"(trainiert_am|letzter_erfolg|letzter_fehler_am)\s*\.isoformat\(\)"
    )
    treffer = muster.findall(quelle)
    assert treffer == [], (
        f"Nackte .isoformat()-Aufrufe auf Job-Zeitstempeln: {treffer}. "
        "Ueber _iso() leiten, sonst fehlt die Zeitzone und der Client "
        "rechnet die Altersangabe falsch."
    )
