"""T-0476: Die Zonen-Karte darf bei einem verpassten Sensor-Beat nicht leeren.

Realfall 01.08.2026, bambuswald: die Karte zeigte "keine Sensordaten",
waehrend die Gardena-App einen Wert hatte. Zwei fuer sich harmlose Dinge
fielen zusammen:

  1. Der Gardena-Lead funkt stuendlich (:25); der 21:25-Beat fiel aus, der
     letzte Wert war damit 95 min alt -- und der Anzeige-Pfad fragte den
     Speicher mit dem 90-min-Default (`AGGREGAT_FENSTER_MIN`). Bei
     stuendlichem Takt hat dieses Fenster 30 min Reserve, vertraegt also
     keinen einzigen Aussetzer.
  2. Backend-Neustart vier Minuten vorher -> der zweite Fallback
     (`_verarbeiter.hole_letzten_wert`) ist ein In-Memory-Cache und nach
     einem Restart leer.

Entscheid 10.08.: Der Anzeige-Pfad laeuft auf demselben Horizont wie der
Entscheidungspfad (`AGGREGAT_FALLBACK_FENSTER_MIN`, 240 min, seit T-0383).
Begruendung ueber die Zahl hinaus: die Automatik oeffnet auf einem bis zu
240 min alten Wert ein VENTIL. Ein System, das darauf giesst, aber denselben
Wert nicht anzeigen oder auf ihm warnen darf, ist in sich widerspruechlich.

Die Kopplung selbst ist das eigentliche Schutzgut dieses Files: Abruf-Fenster
(Anzeige) und Frischefenster (Warnung `lead_ausgefallen`) muessen EINE Quelle
haben. Zwei getrennte Vergleiche gegen dieselbe Zahl driften wieder
auseinander -- genau so entstand die Doppel-Botschaft "Wert von vor 1.6 h"
plus "Lead ausgefallen".

Gegenrichtung (E4): der In-Memory-Cache hatte gar keine Alterskappung. Das
Akzeptanzkriterium "nach 48 h verschwindet der Wert" galt nur aus
Betriebszufall (haeufige Restarts). Jetzt aus Design:
`MAX_FALLBACK_ALTER_STUNDEN`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import bewaesserung.api_server as api_server
from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.entscheidung import MAX_FALLBACK_ALTER_STUNDEN
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
from bewaesserung.speicher import (
    AGGREGAT_FALLBACK_FENSTER_MIN,
    Speicher,
    ist_messung_verwendbar,
)

LEAD = "aaaa0002-0000-4000-8000-000000000001"
ZONE = "bambuswald"


def _run(coro):
    return asyncio.run(coro)


def _gesamt_konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[
            ZonenKonfig(
                zone_id=ZONE,
                name="Bambuswald",
                ventil_kanal=2,
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
                wetter_standort="o", zonen=[ZONE],
            ),
        ],
    )


def _messung(wert: float, ts: datetime, gid: str = LEAD) -> SensorMessung:
    return SensorMessung(
        zeitstempel=ts, zone_id=ZONE, geraet_id=gid,
        boden_feuchte=wert, boden_temperatur=18.0,
        batterie_prozent=85.0, quelle=DatenQuelle.GARDENA,
    )


def _motor(jetzt: datetime) -> MagicMock:
    motor = MagicMock()

    async def _empf(zone_id, sicherheits_tage_override=None):
        return GiessEmpfehlung(
            zone_id=zone_id, zeitstempel=jetzt,
            soll_bewaessern=False, grund="Feuchte ok",
            feuchte_aktuell=50.0, effektive_schwelle=35.0,
            ml_aktiv=False, ml_wirksam=False,
        )
    motor.vorhersage_zone = _empf
    return motor


def _baue_client(
    tmp_path,
    db_alter_min: int | None,
    cache_wert: SensorMessung | None,
    name: str,
):
    """Client mit genau einem Lead-Sensor.

    `db_alter_min=None` heisst: die DB ist leer (nichts persistiert).
    `cache_wert=None` heisst: der In-Memory-Cache ist leer -- exakt der
    Zustand direkt nach einem Backend-Neustart.

    Alle Zeiten relativ zu `datetime.now()`: die Tests duerfen nicht
    tageszeitabhaengig werden (siehe Mitternachts-Flaky-Pattern).
    """
    speicher = Speicher(str(tmp_path / f"{name}.db"))
    _run(speicher.verbinden())
    jetzt = datetime.now()

    if db_alter_min is not None:
        _run(speicher.speichere_messung(
            _messung(50.0, jetzt - timedelta(minutes=db_alter_min)),
        ))

    konfig = _gesamt_konfig()
    speicher.setze_aggregat_lead({ZONE: LEAD})

    verarbeiter = MagicMock()
    verarbeiter.hole_letzten_wert.return_value = cache_wert

    konfiguriere_api(speicher, konfig, _motor(jetzt), verarbeiter)
    return TestClient(app), speicher, jetzt


def _zone(client) -> dict:
    antwort = client.get("/api/zonen")
    assert antwort.status_code == 200
    return antwort.json()[0]


def _zone_snapshot(client) -> dict:
    antwort = client.get("/api/dashboard-snapshot")
    assert antwort.status_code == 200
    return antwort.json()["zonen"][0]["zone"]


# --- (a) verpasster Beat -------------------------------------------------

def test_t0476_verpasster_beat_bleibt_sichtbar(tmp_path):
    """95 min alter Lead-Wert (ein verpasster Stunden-Beat), Cache leer.

    Mit dem alten 90-min-Abruffenster war das eine leere Karte. Der Wert ist
    95 min alt, nicht falsch -- Bodenfeuchte aendert sich mit ~1-3 pp/Tag.
    """
    client, speicher, _ = _baue_client(
        tmp_path, db_alter_min=95, cache_wert=None, name="t0476_beat",
    )
    try:
        zone = _zone(client)
        assert zone["aktuelle_feuchte"] == 50.0
        assert zone["feuchte_geraet_id"] == LEAD
        # Und die Warnung widerspricht der Anzeige nicht.
        assert zone["lead_ausgefallen"] is False
    finally:
        client.close()
        _run(speicher.schliessen())


# --- (b) 48-h-Kappung des Cache-Fallbacks --------------------------------

def test_t0476_cache_wert_aelter_als_48h_verschwindet(tmp_path):
    """DB leer, nur der In-Memory-Cache haelt noch einen Uralt-Wert.

    Vorher hatte `hole_letzten_wert` KEINE Alterspruefung: ein Prozess, der
    lange laeuft, konnte eine tagealte Messung als "aktuelle Feuchte" auf die
    Karte schreiben. Das Akzeptanzkriterium aus T-0476 verlangt das Gegenteil.
    """
    jetzt_ref = datetime.now()
    uralt = _messung(
        50.0, jetzt_ref - timedelta(hours=MAX_FALLBACK_ALTER_STUNDEN + 1),
    )
    client, speicher, _ = _baue_client(
        tmp_path, db_alter_min=None, cache_wert=uralt, name="t0476_48h",
    )
    try:
        zone = _zone(client)
        assert zone["aktuelle_feuchte"] is None
        assert zone["letztes_update"] is None
        # Gegenprobe: knapp INNERHALB der Kappung bleibt der Wert stehen.
        frisch_genug = _messung(
            50.0, jetzt_ref - timedelta(hours=MAX_FALLBACK_ALTER_STUNDEN - 1),
        )
        assert api_server._cache_wert_wenn_frisch(
            frisch_genug, jetzt_ref,
        ) is frisch_genug
        assert api_server._cache_wert_wenn_frisch(uralt, jetzt_ref) is None
    finally:
        client.close()
        _run(speicher.schliessen())


# --- (c) Realfall 01.08.: Restart PLUS verpasster Beat --------------------

def test_t0476_restart_plus_verpasster_beat_leert_die_karte_nicht(tmp_path):
    """Der einzige Test, der beide Fallbacks zugleich prueft.

    Nach dem Neustart ist der Cache leer (`cache_wert=None`), und der
    juengste persistierte Beat ist 95 min alt. Genau diese Kombination
    erzeugte am 01.08. die leere Karte -- einzeln haette jeder der beiden
    Mechanismen sie aufgefangen.

    Zusaetzlich beide Endpoints: `/api/zonen` und der Bulk-Snapshot haben je
    einen EIGENEN Cache-Fallback; ohne den Vergleich koennte genau einer der
    beiden repariert bleiben (T-0200-Zusage, vgl. T-0532).
    """
    client, speicher, _ = _baue_client(
        tmp_path, db_alter_min=95, cache_wert=None, name="t0476_restart",
    )
    try:
        direkt = _zone(client)
        bulk = _zone_snapshot(client)

        assert direkt["aktuelle_feuchte"] == 50.0
        for feld in ("aktuelle_feuchte", "lead_ausgefallen",
                     "feuchte_geraet_id", "letztes_update"):
            assert direkt[feld] == bulk[feld], f"Drift in '{feld}'"
    finally:
        client.close()
        _run(speicher.schliessen())


# --- (d) Kopplung: EINE Quelle fuer Abruf-Fenster und Warnung -------------

def test_t0476_grenzfall_knapp_innerhalb_ist_sichtbar_und_unmarkiert(tmp_path):
    """239 min: eine Minute innerhalb des Horizonts -> Wert UND kein Alarm."""
    client, speicher, _ = _baue_client(
        tmp_path,
        db_alter_min=AGGREGAT_FALLBACK_FENSTER_MIN - 1,
        cache_wert=None,
        name="t0476_239",
    )
    try:
        zone = _zone(client)
        assert zone["aktuelle_feuchte"] == 50.0
        assert zone["lead_ausgefallen"] is False
    finally:
        client.close()
        _run(speicher.schliessen())


def test_t0476_grenzfall_knapp_ausserhalb_faellt_aus_dem_fenster(tmp_path):
    """241 min: eine Minute jenseits des Horizonts -> aus dem Abruf-Fenster.

    Der Cache ist leer, also bleibt nichts uebrig. Das ist der Gegenpol zum
    Test darueber: das Fenster ist wirklich ein Fenster, nicht "immer alles".
    """
    client, speicher, _ = _baue_client(
        tmp_path,
        db_alter_min=AGGREGAT_FALLBACK_FENSTER_MIN + 1,
        cache_wert=None,
        name="t0476_241",
    )
    try:
        zone = _zone(client)
        assert zone["aktuelle_feuchte"] is None
    finally:
        client.close()
        _run(speicher.schliessen())


def test_t0476_warnung_fragt_die_zentrale_verwendbarkeits_funktion(monkeypatch):
    """Die Kopplung selbst, nicht die Zahl.

    `_ist_lead_ausgefallen` darf das Alter NICHT selbst ausrechnen. Der Spion
    faellt aus, sobald jemand hier wieder einen eigenen Vergleich einbaut --
    auch wenn der zufaellig dieselbe Zahl trifft.
    """
    aufrufe: list[tuple] = []

    def _spion(zeitstempel, jetzt):
        aufrufe.append((zeitstempel, jetzt))
        return ist_messung_verwendbar(zeitstempel, jetzt)

    monkeypatch.setattr(api_server, "ist_messung_verwendbar", _spion)

    zone = _gesamt_konfig().zonen[0]
    jetzt = datetime.now()

    innerhalb = _messung(
        50.0, jetzt - timedelta(minutes=AGGREGAT_FALLBACK_FENSTER_MIN - 1),
    )
    ausserhalb = _messung(
        50.0, jetzt - timedelta(minutes=AGGREGAT_FALLBACK_FENSTER_MIN + 1),
    )

    assert api_server._ist_lead_ausgefallen(zone, innerhalb, jetzt) is False
    assert api_server._ist_lead_ausgefallen(zone, ausserhalb, jetzt) is True
    assert len(aufrufe) == 2, (
        "Die Warnung rechnet selbst, statt die zentrale Funktion zu fragen"
    )


def test_t0476_abruf_fenster_nutzt_dieselbe_schwelle_wie_die_warnung(tmp_path):
    """Statische Absicherung: die Anzeige fragt den Speicher mit genau der
    Konstante, auf der auch `ist_messung_verwendbar` steht.

    Ohne diesen Test koennte jemand das Abruf-Fenster hochsetzen und die
    Warnschwelle stehen lassen -- die Karte zeigte den Wert und meldete
    daneben "Lead ausgefallen". Das war der Ausgangszustand.
    """
    speicher = Speicher(str(tmp_path / "t0476_kopplung.db"))
    _run(speicher.verbinden())
    jetzt = datetime.now()
    _run(speicher.speichere_messung(_messung(50.0, jetzt - timedelta(minutes=95))))
    speicher.setze_aggregat_lead({ZONE: LEAD})

    fenster_argumente: list[int | None] = []
    original = speicher.letzte_messung_aggregiert

    async def _spion(zone_id, fenster_minuten=None, jetzt=None):
        fenster_argumente.append(fenster_minuten)
        if fenster_minuten is None:
            return await original(zone_id, jetzt=jetzt)
        return await original(
            zone_id, fenster_minuten=fenster_minuten, jetzt=jetzt,
        )

    speicher.letzte_messung_aggregiert = _spion

    verarbeiter = MagicMock()
    verarbeiter.hole_letzten_wert.return_value = None
    konfiguriere_api(speicher, _gesamt_konfig(), _motor(jetzt), verarbeiter)

    client = TestClient(app)
    try:
        _zone(client)
        assert fenster_argumente, "Anzeige-Pfad fragte das Aggregat gar nicht"
        assert all(
            f == AGGREGAT_FALLBACK_FENSTER_MIN for f in fenster_argumente
        ), f"Abruf-Fenster driftet: {fenster_argumente}"
        # ... und die Warnschwelle steht auf derselben Zahl.
        assert ist_messung_verwendbar(
            jetzt - timedelta(minutes=AGGREGAT_FALLBACK_FENSTER_MIN), jetzt,
        ) is True
        assert ist_messung_verwendbar(
            jetzt - timedelta(minutes=AGGREGAT_FALLBACK_FENSTER_MIN + 1), jetzt,
        ) is False
    finally:
        client.close()
        _run(speicher.schliessen())


@pytest.mark.parametrize("alter_min,erwartet", [(0, True), (239, True),
                                                (240, True), (241, False)])
def test_t0476_ist_messung_verwendbar_grenzen(alter_min, erwartet):
    """Die zentrale Funktion selbst -- inklusive der Gleichheits-Grenze.

    `<=`, nicht `<`: eine exakt 240 min alte Messung ist noch verwendbar.
    """
    jetzt = datetime(2026, 8, 10, 12, 0)
    ts = jetzt - timedelta(minutes=alter_min)
    assert ist_messung_verwendbar(ts, jetzt) is erwartet
