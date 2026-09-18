"""T-0532: `aktuelle_feuchte` muss ihre Quelle nennen, wenn sie nicht vom
konfigurierten Aggregat-Lead stammt.

Realfall waldblumenhain 09.08.2026: das KRITISCH-Band meldete
"kritische Schwelle 25% unterschritten" auf 18 %, waehrend der Lead-Sensor
aaaa0001 bei 40 stand -- also UEBER der kritischen Schwelle. Die 18 kamen von
einem FYTA; genau von der Sorte Sensor, gegen die der Lead ueberhaupt gesetzt
wurde (Cross-Spray T-0332, FYTA-Skalenbruch T-0385).

Ursachenkette:
  1. Der Gardena-Lead funkt stuendlich, das Aggregat-Fenster war 90 min --
     ein verpasster Beat genuegte. Am 09.08. belegt: Luecken
     07:40->10:40 (180 min) und 16:40->18:40 (120 min).
     T-0476 (10.08.): Das Fenster ist jetzt an den Entscheidungs-Horizont
     gekoppelt (`AGGREGAT_FALLBACK_FENSTER_MIN`, 240 min) -- die Zahlen in
     den Fixtures unten wurden entsprechend nachgezogen, der gepruefte
     Vertrag ist unveraendert.
  2. `letzte_messung_aggregiert` gibt dann bewusst None zurueck (T-0384,
     KEIN stiller Median-Fallback auf die Nachbarsensoren).
  3. Aber BEIDE Aufrufer in `api_server` fingen dieses None mit
     `_verarbeiter.hole_letzten_wert(zone_id)` ab -- und dieser Cache ist
     nur nach zone_id verschluesselt (`sensordaten.py:_letzte_werte`):
     es gewinnt, wer zuletzt gefunkt hat. Der Fallback, den T-0384 im
     Aggregat geschlossen hat, war eine Ebene darueber wieder offen.

Der Test prueft den Endpoint, nicht den Helper -- der Bug entstand ja gerade
an der Naht zwischen Aggregat und Aufrufer.

Fehlerklasse: [[fehlerpattern_ausschluss_lead_fallback_bei_dropout]] --
stille Substitution der Datenquelle. Gegenprobe unten stellt sicher, dass der
Fix keine echte Kritisch-Meldung verschluckt.
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

LEAD = "aaaa0001-0000-4000-8000-000000000001"
FYTA_A = "fyta_900002"
FYTA_B = "fyta_900003"


def _run(coro):
    return asyncio.run(coro)


def _gesamt_konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[
            ZonenKonfig(
                zone_id="waldblumenhain",
                name="Waldblumenhain",
                ventil_kanal=1,
                feuchte_schwelle_min=35.0,
                feuchte_schwelle_max=70.0,
                feuchte_kritisch=25.0,
                aggregat_lead_geraet=LEAD,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten",
                wetter_standort="o", zonen=["waldblumenhain"],
            ),
        ],
    )


def _messung(gid: str, wert: float, ts: datetime, quelle: DatenQuelle):
    return SensorMessung(
        zeitstempel=ts, zone_id="waldblumenhain", geraet_id=gid,
        boden_feuchte=wert, boden_temperatur=18.0,
        batterie_prozent=85.0, quelle=quelle,
    )


def _baue_client(tmp_path, lead_alter_min: int):
    """Client mit den drei realen Sensoren der Zone.

    `lead_alter_min` steuert, ob der Lead noch im Aggregat-Fenster liegt
    (T-0476: Frischefenster an Entscheidungs-Horizont gekoppelt, 240 min).
    Der Verarbeiter-Cache liefert -- wie im Live-Betrieb -- den
    zuletzt eingegangenen Wert, und das ist bei 15-min-FYTA gegen
    stuendlichen Gardena praktisch immer ein FYTA.
    """
    speicher = Speicher(str(tmp_path / "t0532.db"))
    _run(speicher.verbinden())
    jetzt = datetime.now()

    _run(speicher.speichere_messung(
        _messung(LEAD, 40.0, jetzt - timedelta(minutes=lead_alter_min),
                 DatenQuelle.GARDENA),
    ))
    _run(speicher.speichere_messung(
        _messung(FYTA_A, 18.0, jetzt - timedelta(minutes=5), DatenQuelle.FYTA),
    ))
    _run(speicher.speichere_messung(
        _messung(FYTA_B, 11.0, jetzt - timedelta(minutes=8), DatenQuelle.FYTA),
    ))

    konfig = _gesamt_konfig()
    speicher.setze_aggregat_lead({"waldblumenhain": LEAD})

    motor = MagicMock()

    async def _empf(zone_id, sicherheits_tage_override=None):
        return GiessEmpfehlung(
            zone_id=zone_id, zeitstempel=jetzt,
            soll_bewaessern=False, grund="Feuchte ok",
            feuchte_aktuell=40.0, effektive_schwelle=35.0,
            ml_aktiv=False, ml_wirksam=False,
        )
    motor.vorhersage_zone = _empf

    verarbeiter = MagicMock()
    verarbeiter.hole_letzten_wert.return_value = _messung(
        FYTA_A, 18.0, jetzt - timedelta(minutes=5), DatenQuelle.FYTA,
    )

    konfiguriere_api(speicher, konfig, motor, verarbeiter)
    return TestClient(app), speicher


@pytest.fixture
def client_lead_ausgefallen(tmp_path):
    """Lead 300 min alt -- ausserhalb des Fensters.

    T-0476: Frischefenster an Entscheidungs-Horizont gekoppelt (240 statt
    90 min). Der Realfall 16:40->18:40 (120 min) faellt damit NICHT mehr
    unter "ausgefallen" -- das ist der Zweck der Kopplung, denn die
    Automatik giesst auf genau diesem Wert. Der gepruefte Zustand
    ("Lead ausserhalb des Frischefensters") braucht deshalb ein Alter
    jenseits der 240 min; 300 min = Luecke von fuenf Stunden-Beats.
    """
    client, speicher = _baue_client(tmp_path, lead_alter_min=300)
    try:
        yield client
    finally:
        client.close()
        _run(speicher.schliessen())


@pytest.fixture
def client_lead_frisch(tmp_path):
    """Gegenprobe: Lead 10 min alt, also im Fenster."""
    client, speicher = _baue_client(tmp_path, lead_alter_min=10)
    try:
        yield client
    finally:
        client.close()
        _run(speicher.schliessen())


def _zone(client) -> dict:
    antwort = client.get("/api/zonen")
    assert antwort.status_code == 200
    return antwort.json()[0]


def _zone_snapshot(client) -> dict:
    antwort = client.get("/api/dashboard-snapshot")
    assert antwort.status_code == 200
    return antwort.json()["zonen"][0]["zone"]


def test_t0532_lead_ausserhalb_fenster_wird_als_ausgefallen_gemeldet(
    client_lead_ausgefallen,
):
    """Der Kern: 18 % vom FYTA darf nicht als Zonen-Wahrheit durchgehen."""
    zone = _zone(client_lead_ausgefallen)

    # Der Wert bleibt sichtbar -- eine leere Karte waere nicht ehrlicher.
    assert zone["aktuelle_feuchte"] == 18.0
    # ... aber er ist als fremde Quelle markiert.
    assert zone["lead_ausgefallen"] is True
    assert zone["feuchte_geraet_id"] == FYTA_A
    assert zone["aggregat_lead_geraet"] == LEAD


def test_t0532_gegenprobe_lead_im_fenster_ist_nicht_ausgefallen(
    client_lead_frisch,
):
    """Lead vorhanden -> Wert kommt vom Lead, kein Sonderzustand.

    Wichtig gegen Ueberkorrektur: der Fix darf echte Messungen nicht
    generell entwerten. Hier steht der Lead bei 40 -- ueber kritisch (25),
    also gar kein Alarm, und genau das war am 09.08. die Wahrheit.
    """
    zone = _zone(client_lead_frisch)

    assert zone["aktuelle_feuchte"] == 40.0
    assert zone["lead_ausgefallen"] is False
    assert zone["feuchte_geraet_id"] == LEAD


def test_t0532_snapshot_und_zonen_endpoint_sind_einig(client_lead_ausgefallen):
    """Beide Endpoints bauen ueber `_baue_zone_dict` -- der Vertrag darf
    nicht auseinanderlaufen (T-0200-Zusage). Der Bulk-Pfad hat seinen
    EIGENEN Verarbeiter-Fallback (`_letzter_wert_fuer`); ohne diesen Test
    koennte genau einer der beiden repariert bleiben."""
    direkt = _zone(client_lead_ausgefallen)
    bulk = _zone_snapshot(client_lead_ausgefallen)

    for feld in ("aktuelle_feuchte", "lead_ausgefallen",
                 "feuchte_geraet_id", "aggregat_lead_geraet"):
        assert direkt[feld] == bulk[feld], f"Drift in '{feld}'"


def test_t0532_stale_lead_wert_aus_dem_cache_zaehlt_auch_als_ausgefallen(tmp_path):
    """Zweiter Weg in denselben Zustand: der Cache haelt den LEAD, aber alt.

    Tritt auf, wenn die uebrigen Sensoren der Zone still sind -- bei sechs
    dauerhaft ausser Hub-Reichweite liegenden FYTA kein exotischer Fall.
    Das Akzeptanzkriterium lautet "Lead ausserhalb des Frischefensters",
    nicht "Wert von fremdem Sensor": ohne diesen Zweig haette ein sehr alter
    Lead-Wert weiter eine Kritisch-Meldung getragen.

    T-0476: Frischefenster an Entscheidungs-Horizont gekoppelt -- 180 min
    sind jetzt bewusst NOCH verwendbar (die Automatik giesst darauf), daher
    hier 300 min.
    """
    speicher = Speicher(str(tmp_path / "t0532_stale.db"))
    _run(speicher.verbinden())
    jetzt = datetime.now()
    alt = jetzt - timedelta(minutes=300)
    _run(speicher.speichere_messung(_messung(LEAD, 18.0, alt, DatenQuelle.GARDENA)))

    konfig = _gesamt_konfig()
    speicher.setze_aggregat_lead({"waldblumenhain": LEAD})

    motor = MagicMock()

    async def _empf(zone_id, sicherheits_tage_override=None):
        return GiessEmpfehlung(
            zone_id=zone_id, zeitstempel=jetzt,
            soll_bewaessern=False, grund="Feuchte ok",
            feuchte_aktuell=18.0, effektive_schwelle=35.0,
            ml_aktiv=False, ml_wirksam=False,
        )
    motor.vorhersage_zone = _empf

    verarbeiter = MagicMock()
    verarbeiter.hole_letzten_wert.return_value = _messung(
        LEAD, 18.0, alt, DatenQuelle.GARDENA,
    )

    konfiguriere_api(speicher, konfig, motor, verarbeiter)
    client = TestClient(app)
    try:
        zone = _zone(client)
        # Quelle IST der Lead -- trotzdem ausgefallen, weil ausserhalb
        # des Frischefensters.
        assert zone["feuchte_geraet_id"] == LEAD
        assert zone["lead_ausgefallen"] is True
    finally:
        client.close()
        _run(speicher.schliessen())


def test_t0532_zone_ohne_lead_bleibt_unmarkiert(tmp_path):
    """Isomorphie-Guard: Zonen ohne konfigurierten Lead (die Mehrheit)
    duerfen das Flag NIE gesetzt bekommen -- dort ist der Median der
    vereinbarte Vertrag, kein Ausfall."""
    speicher = Speicher(str(tmp_path / "t0532_ohne.db"))
    _run(speicher.verbinden())
    jetzt = datetime.now()
    _run(speicher.speichere_messung(
        _messung(FYTA_A, 18.0, jetzt - timedelta(minutes=5), DatenQuelle.FYTA),
    ))
    _run(speicher.speichere_messung(
        _messung(FYTA_B, 11.0, jetzt - timedelta(minutes=8), DatenQuelle.FYTA),
    ))

    konfig = _gesamt_konfig()
    konfig.zonen[0].aggregat_lead_geraet = None
    speicher.setze_aggregat_lead({})

    motor = MagicMock()

    async def _empf(zone_id, sicherheits_tage_override=None):
        return GiessEmpfehlung(
            zone_id=zone_id, zeitstempel=jetzt,
            soll_bewaessern=False, grund="Feuchte ok",
            feuchte_aktuell=14.5, effektive_schwelle=35.0,
            ml_aktiv=False, ml_wirksam=False,
        )
    motor.vorhersage_zone = _empf

    konfiguriere_api(speicher, konfig, motor, MagicMock())
    client = TestClient(app)
    try:
        zone = _zone(client)
        assert zone["lead_ausgefallen"] is False
        assert zone["aggregat_lead_geraet"] is None
        # Median ueber beide FYTA -- geraet_id traegt den Aggregat-Marker.
        assert zone["feuchte_geraet_id"] == "aggregat:2"
    finally:
        client.close()
        _run(speicher.schliessen())


# --- T-0502: das 0.0-Urteil im Zone-Dict --------------------------------


def _zone_mit_null(tmp_path, verlauf):
    """Zone mit nur einem Gardena-Sensor, dessen Verlauf auf 0.0 endet."""
    speicher = Speicher(str(tmp_path / "t0502.db"))
    _run(speicher.verbinden())
    jetzt = datetime.now()
    for stunden_vor, wert in verlauf:
        _run(speicher.speichere_messung(_messung(
            LEAD, wert, jetzt - timedelta(hours=stunden_vor), DatenQuelle.GARDENA,
        )))
    konfig = _gesamt_konfig()
    konfig.zonen[0].aggregat_lead_geraet = None
    speicher.setze_aggregat_lead({})

    motor = MagicMock()

    async def _empf(zone_id, sicherheits_tage_override=None):
        return GiessEmpfehlung(
            zone_id=zone_id, zeitstempel=jetzt, soll_bewaessern=False,
            grund="x", feuchte_aktuell=0.0, effektive_schwelle=35.0,
            ml_aktiv=False, ml_wirksam=False,
        )
    motor.vorhersage_zone = _empf
    konfiguriere_api(speicher, konfig, motor, MagicMock())
    return TestClient(app), speicher


def test_t0502_sprung_auf_null_wird_im_zone_dict_als_defekt_gemeldet(tmp_path):
    """Der Lager-Fall: aus 50 binnen Stunden auf 0.

    Das Frontend darf das nicht selbst entscheiden -- die Trajektorie steckt
    in der DB. Deshalb liefert das Backend das Urteil fertig.
    """
    client, speicher = _zone_mit_null(tmp_path, [(20, 50.0), (8, 15.0), (0, 0.0)])
    try:
        zone = _zone(client)
        assert zone["aktuelle_feuchte"] == 0.0
        assert zone["null_ist_defekt"] is True
    finally:
        client.close()
        _run(speicher.schliessen())


def test_t0502_abstieg_ueber_tage_ist_im_zone_dict_kein_defekt(tmp_path):
    """Gegenprobe, gleicher Endwert: der Abstieg liegt ausserhalb der 24 h."""
    client, speicher = _zone_mit_null(tmp_path, [(96, 35.0), (48, 15.0), (2, 5.0), (0, 0.0)])
    try:
        zone = _zone(client)
        assert zone["aktuelle_feuchte"] == 0.0
        assert zone["null_ist_defekt"] is False
    finally:
        client.close()
        _run(speicher.schliessen())


def test_t0502_ohne_null_bleibt_das_feld_leer(tmp_path):
    """Kein 0.0 -> die Frage stellt sich nicht, und es laeuft keine Abfrage."""
    client, speicher = _zone_mit_null(tmp_path, [(2, 40.0), (0, 45.0)])
    try:
        assert _zone(client)["null_ist_defekt"] is None
    finally:
        client.close()
        _run(speicher.schliessen())
