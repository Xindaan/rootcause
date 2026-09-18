"""T-0453: der Klassifikations-Pfad fuer Cross-Spray ueber die API.

Der Weg, den Andre im Banner geht: unklassifizierter Sensor-Sprung ->
"Fremdwasser" -> Event bleibt als Marker stehen, zaehlt aber nicht mehr als
eigenes Kanal-Wasser, und die Quell-Zone wird -- wo eindeutig -- gleich
mitgeschrieben.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.modelle import (
    Ausloser,
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    VentilAktion,
    VentilEreignis,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[
            # Bambus-Zonen teilen Kanal 2 desselben DSWC -- sie duerfen sich
            # gegenseitig NICHT als Fremdwasser-Quelle gelten (derselbe
            # physische Lauf).
            ZonenKonfig(
                zone_id="bambuswald", name="Bambus",
                ventil_geraet_id="dswc-1", ventil_kanal=2,
                versickerungs_karenz_stunden=3,
            ),
            ZonenKonfig(
                zone_id="bambuswald_yogaraum", name="Yoga",
                ventil_geraet_id="dswc-1", ventil_kanal=2,
            ),
            ZonenKonfig(
                zone_id="magerwiese", name="Magerwiese",
                ventil_geraet_id="dswc-2", ventil_kanal=1,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.5, laenge=13.4)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
    )


@pytest.fixture
def klient(tmp_path):
    speicher = Speicher(str(tmp_path / "klassifikation.db"))
    _run(speicher.verbinden())
    konfiguriere_api(
        speicher, _konfig(),
        MagicMock(),  # type: ignore[arg-type]
        MagicMock(),  # type: ignore[arg-type]
    )
    c = TestClient(app)
    try:
        yield c, speicher
    finally:
        c.close()
        _run(speicher.schliessen())


def _heuristik_sprung(speicher, zeit: datetime) -> int:
    """Unklassifizierter Sensor-Sprung, wie ihn sensor_backfill schreibt."""
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=zeit, zone_id="bambuswald",
        ventil_id="sensor_heuristik", aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=1800, ausloser=Ausloser.UNBEKANNT,
    )))
    events = _run(speicher.hole_ventil_ereignisse("bambuswald"))
    heuristik = [e for e in events if e.ventil_id == "sensor_heuristik"]
    assert heuristik and heuristik[-1].id is not None
    return heuristik[-1].id


def _regner_lauf(speicher, zone: str, zeit: datetime, ventil_id: str) -> None:
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=zeit, zone_id=zone, ventil_id=ventil_id,
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=3600,
        ausloser=Ausloser.MANUELL,
    )))


def test_flip_auf_fremdwasser_leitet_eindeutige_quelle_ab(klient):
    c, speicher = klient
    jetzt = datetime.now()
    _regner_lauf(speicher, "magerwiese", jetzt - timedelta(hours=1), "uuid-2:1")
    eid = _heuristik_sprung(speicher, jetzt)

    antwort = c.patch(
        f"/api/ventil-ereignis/{eid}", json={"ausloser": "fremdwasser"},
    )
    assert antwort.status_code == 200, antwort.text
    assert antwort.json().get("ok") is True

    nachher = _run(speicher.hole_ventil_ereignis(eid))
    assert nachher is not None
    assert nachher.ausloser is Ausloser.FREMDWASSER
    assert nachher.quell_zone == "magerwiese"


def test_zone_am_gleichen_kanal_gilt_nicht_als_fremdquelle(klient):
    """bambuswald + yogaraum teilen Kanal 2 -- ein Lauf dort ist DERSELBE
    physische Lauf, keine Fremdquelle. Wuerde er als Quelle durchgehen,
    stuende in der DB die Behauptung, das Wasser sei von nebenan gekommen.
    """
    c, speicher = klient
    jetzt = datetime.now()
    _regner_lauf(
        speicher, "bambuswald_yogaraum", jetzt - timedelta(hours=1), "uuid-1:2",
    )
    eid = _heuristik_sprung(speicher, jetzt)

    c.patch(f"/api/ventil-ereignis/{eid}", json={"ausloser": "fremdwasser"})
    nachher = _run(speicher.hole_ventil_ereignis(eid))
    assert nachher is not None
    assert nachher.ausloser is Ausloser.FREMDWASSER
    assert nachher.quell_zone is None


def test_ohne_passenden_lauf_bleibt_quelle_leer(klient):
    """Lieber keine Quelle als eine geratene -- ein Zonenname in der DB wird
    in einer spaeteren Analyse wie ein Messwert gelesen."""
    c, speicher = klient
    jetzt = datetime.now()
    # Regner lief, aber weit ausserhalb der 3-h-Karenz.
    _regner_lauf(speicher, "magerwiese", jetzt - timedelta(hours=9), "uuid-2:1")
    eid = _heuristik_sprung(speicher, jetzt)

    c.patch(f"/api/ventil-ereignis/{eid}", json={"ausloser": "fremdwasser"})
    nachher = _run(speicher.hole_ventil_ereignis(eid))
    assert nachher is not None and nachher.quell_zone is None


def test_explizite_quell_zone_gewinnt_gegen_ableitung(klient):
    c, speicher = klient
    jetzt = datetime.now()
    _regner_lauf(speicher, "magerwiese", jetzt - timedelta(hours=1), "uuid-2:1")
    eid = _heuristik_sprung(speicher, jetzt)

    c.patch(
        f"/api/ventil-ereignis/{eid}",
        json={"ausloser": "fremdwasser", "quell_zone": "hecke"},
    )
    nachher = _run(speicher.hole_ventil_ereignis(eid))
    assert nachher is not None and nachher.quell_zone == "hecke"


def test_bulk_flip_setzt_fremdwasser_auf_allen_events(klient):
    """Der Banner-Weg bei Mass-Aufkommen (T-0212a)."""
    c, speicher = klient
    jetzt = datetime.now()
    _regner_lauf(speicher, "magerwiese", jetzt - timedelta(hours=1), "uuid-2:1")
    ids = [
        _heuristik_sprung(speicher, jetzt - timedelta(minutes=m))
        for m in (0, 20, 40)
    ]

    antwort = c.post(
        "/api/ventil-ereignisse/klassifiziere-bulk",
        json={"ids": ids, "ausloser": "fremdwasser", "paar": False},
    )
    assert antwort.status_code == 200, antwort.text
    assert antwort.json().get("ok") is True

    for eid in ids:
        e = _run(speicher.hole_ventil_ereignis(eid))
        assert e is not None
        assert e.ausloser is Ausloser.FREMDWASSER
        assert e.quell_zone == "magerwiese"


def test_api_liefert_quell_zone_an_die_ui(klient):
    c, speicher = klient
    jetzt = datetime.now()
    _regner_lauf(speicher, "magerwiese", jetzt - timedelta(hours=1), "uuid-2:1")
    eid = _heuristik_sprung(speicher, jetzt)
    c.patch(f"/api/ventil-ereignis/{eid}", json={"ausloser": "fremdwasser"})

    antwort = c.get("/api/ventil-ereignisse", params={"zone_id": "bambuswald"})
    assert antwort.status_code == 200, antwort.text
    treffer = [e for e in antwort.json() if e["id"] == eid]
    assert treffer and treffer[0]["quell_zone"] == "magerwiese"
