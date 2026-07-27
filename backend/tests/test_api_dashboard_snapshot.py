"""T-0200: API-Endpoint /api/dashboard-snapshot.

Aggregierter Bulk-Endpoint fuer die Zonen-Karte v2. Ersetzt drei
Per-Karte-Polls pro Zone durch einen Call. Tests verifizieren:

- Response-Shape (zonen[*].zone / .empfehlung / .messwerte / .ml_vorhersage)
- Konsistenz mit den Per-Zone-Endpoints: das Zone-Dict im Snapshot ist
  byte-identisch zur Per-Zone-Antwort aus /api/zonen
- Fenster-Filter (24h, 48h, 7d, 30d) liefert je Fenster eine getrennte
  Messwerte-Liste
- zone_id-Filter liefert nur die angefragte Zone, unbekannte Zone 404
- ML-Pfad: kein Service -> ml_vorhersage_fehler gesetzt, ml_vorhersage leer
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.modelle import (
    DatenQuelle,
    GardenaKonfig,
    GesamtKonfig,
    GiessEmpfehlung,
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


def _konfig(zone_ids: tuple[str, ...] = ("zone_a", "zone_b")) -> GesamtKonfig:
    zonen = [
        ZonenKonfig(
            zone_id=zid,
            name=zid.replace("_", " ").title(),
            ventil_kanal=i + 1,
            feuchte_schwelle_min=30.0,
            feuchte_schwelle_max=70.0,
            feuchte_kritisch=20.0,
        )
        for i, zid in enumerate(zone_ids)
    ]
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=zonen,
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten",
                wetter_standort="o", zonen=list(zone_ids),
            ),
        ],
    )


@pytest.fixture
def client_mit_daten(tmp_path):
    """TestClient mit DB, Mock-Motor und ML-Service deaktiviert (None).

    DB enthaelt:
    - 2 Messungen je Zone, eine "live" (10 min alt), eine 6 h alt
    """
    speicher = Speicher(str(tmp_path / "snapshot.db"))
    _run(speicher.verbinden())

    jetzt = datetime.now()
    for i, zid in enumerate(("zone_a", "zone_b")):
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=jetzt - timedelta(minutes=10),
            zone_id=zid, geraet_id=f"sensor_{zid}",
            boden_feuchte=40.0 + i * 5,
            boden_temperatur=18.0,
            batterie_prozent=85.0,
            quelle=DatenQuelle.GARDENA,
        )))
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=jetzt - timedelta(hours=6),
            zone_id=zid, geraet_id=f"sensor_{zid}",
            boden_feuchte=50.0 + i * 5,
            boden_temperatur=17.5,
            batterie_prozent=86.0,
            quelle=DatenQuelle.GARDENA,
        )))

    motor = MagicMock()

    async def _empf(zone_id, sicherheits_tage_override=None):
        return GiessEmpfehlung(
            zone_id=zone_id, zeitstempel=jetzt,
            soll_bewaessern=False, grund="Feuchte ok",
            feuchte_aktuell=40.0, effektive_schwelle=30.0,
            ml_aktiv=False, ml_wirksam=False,
        )
    motor.vorhersage_zone = _empf

    konfiguriere_api(speicher, _konfig(), motor, MagicMock())
    client = TestClient(app)
    try:
        yield client, speicher, jetzt
    finally:
        client.close()
        _run(speicher.schliessen())


# --- Response-Shape ------------------------------------------------------


def test_snapshot_liefert_alle_zonen(client_mit_daten):
    client, _, _ = client_mit_daten
    antwort = client.get("/api/dashboard-snapshot")
    assert antwort.status_code == 200
    daten = antwort.json()

    assert "zeitstempel" in daten
    assert "fenster" in daten
    assert "ml_verfuegbar" in daten
    assert isinstance(daten["zonen"], list)
    assert len(daten["zonen"]) == 2

    z = daten["zonen"][0]
    for feld in ("zone", "empfehlung", "messwerte", "ml_vorhersage", "ml_vorhersage_fehler"):
        assert feld in z, f"Pflichtfeld '{feld}' fehlt in zonen[0]"

    # T-0221: neue Felder fuer die V3-Karte muessen im Zone-Dict liegen.
    zone_dict = z["zone"]
    for feld in ("ventil_geraet_id", "flaeche_m2", "ist_topf", "aquabloom_konfig"):
        assert feld in zone_dict, f"T-0221-Feld '{feld}' fehlt im Zone-Dict"


def test_snapshot_zone_dict_ist_konsistent_zu_api_zonen(client_mit_daten):
    """Single Source of Truth: Snapshot-Zone-Dict == /api/zonen-Zone-Dict.

    Wenn jemand spaeter `/api/zonen` aendert ohne den Helper anzufassen,
    divergieren die Antworten — dieser Test schlaegt dann an.
    """
    client, _, _ = client_mit_daten
    snap = client.get("/api/dashboard-snapshot").json()
    zonen = client.get("/api/zonen").json()

    zonen_per_id = {z["zone_id"]: z for z in zonen}
    for eintrag in snap["zonen"]:
        zone_dict = eintrag["zone"]
        zid = zone_dict["zone_id"]
        per = zonen_per_id[zid]
        # `letztes_update` ist datetime.now() — kann zwischen den beiden
        # Calls um wenige Mikrosekunden abweichen. Alle anderen Felder
        # MUESSEN identisch sein.
        zd = dict(zone_dict); pd = dict(per)
        zd.pop("letztes_update", None); pd.pop("letztes_update", None)
        assert zd == pd, f"Zone {zid}: Snapshot-Dict != /api/zonen-Dict"


def test_snapshot_empfehlung_hat_pflichtfelder(client_mit_daten):
    client, _, _ = client_mit_daten
    daten = client.get("/api/dashboard-snapshot").json()
    empf = daten["zonen"][0]["empfehlung"]
    for feld in ("zone_id", "zeitstempel", "soll_bewaessern", "grund",
                 "feuchte_aktuell", "effektive_schwelle", "ml_aktiv", "ml_wirksam"):
        assert feld in empf, f"Empfehlung-Feld '{feld}' fehlt"
    assert empf["zone_id"] == "zone_a"


# --- Messwerte-Fenster ---------------------------------------------------


def test_snapshot_default_fenster_24h_48h(client_mit_daten):
    client, _, _ = client_mit_daten
    daten = client.get("/api/dashboard-snapshot").json()
    assert daten["fenster"] == ["24h", "48h"]
    mw = daten["zonen"][0]["messwerte"]
    assert "24h" in mw
    assert "48h" in mw
    # 2 Messungen pro Zone (10 min + 6 h alt) sind beide in 24h und 48h
    assert len(mw["24h"]) == 2
    assert len(mw["48h"]) == 2


def test_snapshot_fenster_param_filtert(client_mit_daten):
    client, _, _ = client_mit_daten
    daten = client.get("/api/dashboard-snapshot?fenster=7d,30d").json()
    assert daten["fenster"] == ["7d", "30d"]
    mw = daten["zonen"][0]["messwerte"]
    assert "7d" in mw and "30d" in mw
    assert "24h" not in mw


def test_snapshot_messwerte_konsistent_zu_per_zone_endpoint(client_mit_daten):
    """messwerte['24h'] im Snapshot == /api/zonen/{id}/messwerte?stunden=24."""
    client, _, _ = client_mit_daten
    snap = client.get("/api/dashboard-snapshot?fenster=24h").json()
    for eintrag in snap["zonen"]:
        zid = eintrag["zone"]["zone_id"]
        per = client.get(
            f"/api/zonen/{zid}/messwerte?stunden=24"
        ).json()
        snapshot_mw = eintrag["messwerte"]["24h"]
        assert snapshot_mw == per, f"Zone {zid}: Snapshot-Messwerte != /messwerte"


def test_snapshot_unbekanntes_fenster_faellt_auf_24h(client_mit_daten):
    """`fenster=quatsch` darf nicht 500en, sondern auf Default 24h fallen."""
    client, _, _ = client_mit_daten
    daten = client.get("/api/dashboard-snapshot?fenster=foo,bar").json()
    assert daten["fenster"] == ["24h"]


def test_snapshot_fenster_duplikate_werden_dedupliziert(client_mit_daten):
    client, _, _ = client_mit_daten
    daten = client.get("/api/dashboard-snapshot?fenster=24h,24h,48h").json()
    assert daten["fenster"] == ["24h", "48h"]


# --- zone_id-Filter ------------------------------------------------------


def test_snapshot_zone_id_filtert(client_mit_daten):
    client, _, _ = client_mit_daten
    daten = client.get("/api/dashboard-snapshot?zone_id=zone_b").json()
    assert len(daten["zonen"]) == 1
    assert daten["zonen"][0]["zone"]["zone_id"] == "zone_b"


def test_snapshot_unbekannte_zone_liefert_404(client_mit_daten):
    client, _, _ = client_mit_daten
    antwort = client.get("/api/dashboard-snapshot?zone_id=nicht-da")
    assert antwort.status_code == 404


# --- ML-Pfad -------------------------------------------------------------


def test_snapshot_ml_nicht_verfuegbar_setzt_fehler(client_mit_daten):
    """Kein ML-Service injiziert -> ml_verfuegbar=False, pro Zone Fehler."""
    client, _, _ = client_mit_daten
    daten = client.get("/api/dashboard-snapshot").json()
    assert daten["ml_verfuegbar"] is False
    for eintrag in daten["zonen"]:
        assert eintrag["ml_vorhersage"] == {}
        assert eintrag["ml_vorhersage_fehler"] == "ML-Modell nicht verfuegbar"


def test_snapshot_ml_service_mock_liefert_vorhersagen(tmp_path):
    """Mit Mock-ML-Service kommen Vorhersagen + Horizonte im Response."""
    from bewaesserung.ml.modelle_ml import MLVorhersage as MLEintrag

    speicher = Speicher(str(tmp_path / "snap_ml.db"))
    _run(speicher.verbinden())
    jetzt = datetime.now()
    _run(speicher.speichere_messung(SensorMessung(
        zeitstempel=jetzt - timedelta(minutes=5),
        zone_id="zone_a", geraet_id="s1",
        boden_feuchte=42.0, boden_temperatur=18.0,
        batterie_prozent=80.0, quelle=DatenQuelle.GARDENA,
    )))

    motor = MagicMock()
    async def _empf(zone_id, sicherheits_tage_override=None):
        return GiessEmpfehlung(
            zone_id=zone_id, zeitstempel=jetzt,
            soll_bewaessern=False, grund="ok",
            ml_aktiv=False, ml_wirksam=False,
        )
    motor.vorhersage_zone = _empf

    ml_service = MagicMock()
    ml_service.ist_verfuegbar = True

    # T-0200: Snapshot nutzt live_vorhersage_bulk (DF einmal pro Request).
    async def _live_bulk(zone_ids, sp, kf, details=False):
        return {
            zid: {
                "6h":  MLEintrag(zone_id=zid, zeitstempel=jetzt,
                                 horizont_stunden=6, feuchte_aktuell=42.0,
                                 feuchte_prognose=40.0, q10=38.0, q90=43.0),
                "24h": MLEintrag(zone_id=zid, zeitstempel=jetzt,
                                 horizont_stunden=24, feuchte_aktuell=42.0,
                                 feuchte_prognose=35.0),
            }
            for zid in zone_ids
        }
    ml_service.live_vorhersage_bulk = _live_bulk

    konfig = _konfig(zone_ids=("zone_a",))
    konfiguriere_api(speicher, konfig, motor, MagicMock(), ml_service=ml_service)
    client = TestClient(app)
    try:
        daten = client.get("/api/dashboard-snapshot").json()
        assert daten["ml_verfuegbar"] is True
        eintrag = daten["zonen"][0]
        assert eintrag["ml_vorhersage_fehler"] is None
        ml = eintrag["ml_vorhersage"]
        assert set(ml.keys()) == {"6h", "24h"}
        assert ml["6h"]["feuchte_prognose"] == 40.0
        assert ml["6h"]["q10"] == 38.0
        assert ml["6h"]["q90"] == 43.0
        # 24h hat keine Quantile -> Felder fehlen (nicht None)
        assert "q10" not in ml["24h"]
    finally:
        client.close()
        _run(speicher.schliessen())


def test_snapshot_ml_ohne_sensordaten_meldet_fehler(tmp_path):
    """Zone ohne Sensor-Messung -> 'Keine aktuellen Sensordaten'-Fehler.

    Bestaetigt, dass der Snapshot dieselbe Fehler-Semantik liefert wie
    `/api/ml/vorhersage/{id}` (per-Zone), damit das Frontend dieselbe
    Logik nutzen kann.
    """
    speicher = Speicher(str(tmp_path / "snap_leer.db"))
    _run(speicher.verbinden())

    motor = MagicMock()
    async def _empf(zone_id, sicherheits_tage_override=None):
        return GiessEmpfehlung(
            zone_id=zone_id, zeitstempel=datetime.now(),
            soll_bewaessern=False, grund="ok",
            ml_aktiv=False, ml_wirksam=False,
        )
    motor.vorhersage_zone = _empf

    ml_service = MagicMock()
    ml_service.ist_verfuegbar = True
    # Weder live_vorhersage noch live_vorhersage_bulk darf aufgerufen
    # werden, wenn keine einzige Zone Sensordaten hat (Bulk-Kandidaten-
    # Liste ist leer).
    ml_service.live_vorhersage = MagicMock(side_effect=AssertionError(
        "live_vorhersage darf nicht aufgerufen werden ohne Sensordaten",
    ))
    ml_service.live_vorhersage_bulk = MagicMock(side_effect=AssertionError(
        "live_vorhersage_bulk darf nicht aufgerufen werden ohne Sensordaten",
    ))

    # Verarbeiter-Mock muss `None` zurueckgeben, sonst spielt der
    # Fallback-Pfad einen MagicMock als "letzten Sensor-Wert" aus —
    # damit gilt im Endpoint `letzter_wert is None` als False.
    verarbeiter = MagicMock()
    verarbeiter.hole_letzten_wert.return_value = None

    konfig = _konfig(zone_ids=("zone_a",))
    konfiguriere_api(speicher, konfig, motor, verarbeiter, ml_service=ml_service)
    client = TestClient(app)
    try:
        daten = client.get("/api/dashboard-snapshot").json()
        eintrag = daten["zonen"][0]
        assert eintrag["ml_vorhersage"] == {}
        assert eintrag["ml_vorhersage_fehler"] == "Keine aktuellen Sensordaten"
    finally:
        client.close()
        _run(speicher.schliessen())


# --- Cache-Header --------------------------------------------------------


def test_snapshot_setzt_cache_header(client_mit_daten):
    """30 s Cache-Control — Frontend-V2-Loader pollt alle 60 s."""
    client, _, _ = client_mit_daten
    antwort = client.get("/api/dashboard-snapshot")
    assert "max-age=30" in antwort.headers.get("cache-control", "")


def test_snapshot_ttl_cache_serviert_folge_poll(client_mit_daten):
    """T-0293: zweiter Poll innerhalb TTL kommt aus dem Cache -> identischer
    Snapshot-zeitstempel = kein erneuter vorhersage_zone-Recompute. (Top-Level
    `zeitstempel` ist datetime.now() pro Compute; gleich nur bei Cache-Hit.)"""
    client, _, _ = client_mit_daten
    a = client.get("/api/dashboard-snapshot").json()
    b = client.get("/api/dashboard-snapshot").json()
    assert a["zeitstempel"] == b["zeitstempel"]
    # Anderer Key (anderes Fenster) -> eigener Compute, nicht aus a's Cache.
    c = client.get("/api/dashboard-snapshot?fenster=7d").json()
    assert c["fenster"] == ["7d"]


def test_snapshot_cache_wird_bei_konfiguriere_api_geleert(client_mit_daten):
    """T-0293: konfiguriere_api leert den Cache (Test-Isolation + frische
    Deps) -> nach Re-Konfiguration neuer Compute (neuer zeitstempel)."""
    from bewaesserung.api_server import _SNAPSHOT_CACHE
    client, speicher, _ = client_mit_daten
    client.get("/api/dashboard-snapshot")
    assert _SNAPSHOT_CACHE  # gefuellt
    konfiguriere_api(speicher, _konfig(), MagicMock(), MagicMock())
    assert not _SNAPSHOT_CACHE  # geleert
