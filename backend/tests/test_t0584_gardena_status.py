"""T-0584: Gardena-Ventilstatus lesen, mitschreiben, bei Fehlern warnen.

Anlass: die Gardena-App zeigte fuer die Hecke-Einheit "Steuerteil geloest",
und wir holten diesen Zustand gar nicht ab -- weder fuer eine Warnung noch,
um ihn im Nachhinein zu datieren.

Drei Schichten, jeweils am Verhalten gemessen:
  A  `GardenaClient.geraete_status_snapshot` gegen Geraete-Attrappen, die
     GENAU so gebaut sind wie py-smart-gardena sie liefert: Einkanal
     (`WATER_CONTROL`, Zustand am Geraet) und Mehrkanal
     (`SMART_IRRIGATION_CONTROL`, Zustand je Ventil im dict + `valve_set_*`)
  B  der Job gegen einen ECHTEN `Speicher` (temporaere DB): schreibt bei
     Aenderung, schweigt sonst, oeffnet und schliesst Warnungen
  C  die Leitplanken: kein Fehler bei "OK"/"NO_MESSAGE"/"N/A", Geraeteebene
     trifft alle Zonen, keine Doppelwarnung
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bewaesserung.gardena_client import GardenaClient
from bewaesserung.gardena_status_job import (
    GardenaStatusJob,
    _ist_fehler,
)
from bewaesserung.modelle import SensorWarnungTyp
from bewaesserung.speicher import Speicher

JETZT = datetime(2026, 9, 22, 10, 0)


# ------------------------------------------------------------ Attrappen

class _Geraet:
    """py-smart-gardena-Geraet: Attribute liegen flach am Objekt."""

    def __init__(self, **werte):
        self.id = werte.pop("id", "geraet-1")
        self.type = werte.pop("type", "WATER_CONTROL")
        self.name = werte.pop("name", "Ventil Hecke")
        # Die Bibliothek setzt "N/A", wenn die API das Attribut nicht liefert.
        vorgaben = dict(
            battery_level="N/A", battery_state="N/A", rf_link_level="N/A",
            rf_link_state="N/A", serial="N/A", model_type="N/A",
        )
        vorgaben.update(werte)
        for k, v in vorgaben.items():
            setattr(self, k, v)


class _Location:
    def __init__(self, geraete):
        self.devices = {g.id: g for g in geraete}


class _SmartSystem:
    def __init__(self, geraete):
        self.locations = {"loc-1": _Location(geraete)}


def _client(geraete, zone_namen=None) -> GardenaClient:
    c = GardenaClient("test")
    c._smart_system = _SmartSystem(geraete)
    c._location_id = "loc-1"
    c.registriere_zone_namen(zone_namen or {})
    return c


# ============================================== A: Snapshot aus dem WS-State

def test_a_einkanal_zustand_haengt_am_geraet():
    """`WATER_CONTROL`: `valve_state`/`last_error_code` liegen am Geraet."""
    g = _Geraet(
        id="wc-1", type="WATER_CONTROL", name="Ventil Hecke",
        valve_id="valve-1", valve_name="Hecke", valve_activity="CLOSED",
        valve_state="ERROR", last_error_code="VALVE_DETACHED",
        battery_level=77, battery_state="OK", rf_link_level=90,
        rf_link_state="ONLINE", serial="S-1", model_type="GARDENA smart",
    )
    saetze = _client([g], {"hecke": "hecke"}).geraete_status_snapshot(JETZT)

    geraeteebene = [s for s in saetze if not s.ventil_id]
    ventile = [s for s in saetze if s.ventil_id]
    assert len(geraeteebene) == 1 and len(ventile) == 1
    v = ventile[0]
    assert (v.valve_state, v.last_error_code) == ("ERROR", "VALVE_DETACHED")
    assert v.valve_activity == "CLOSED"
    assert v.zone_id == "hecke"          # ueber `valve_name` aufgeloest
    # AK2: Geraeteebene traegt Akku und Funk.
    assert geraeteebene[0].battery_level == 77.0
    assert geraeteebene[0].rf_link_state == "ONLINE"
    assert geraeteebene[0].model_type == "GARDENA smart"


def test_a_mehrkanal_zustand_liegt_je_ventil_im_dict():
    """`SMART_IRRIGATION_CONTROL` (unsere Hecke-Einheit): py-smart-gardena
    legt Ventile als dict-von-dicts ab -- `getattr` darauf liefert immer None
    (T-0302). Zusaetzlich traegt das Geraet den Ventilblock `valve_set_*`."""
    g = _Geraet(
        id="dswc-2", type="SMART_IRRIGATION_CONTROL", name="Z Magerwiese und Hecke",
        valve_set_state="WARNING", valve_set_last_error_code="CONTROL_UNIT_DETACHED",
        battery_level=64, battery_state="OK",
        valves={
            "v-k1": {"id": "v-k1", "name": "Magerwiese", "activity": "CLOSED",
                     "state": "OK", "last_error_code": "NO_MESSAGE"},
            "v-k2": {"id": "v-k2", "name": "Hecke", "activity": "CLOSED",
                     "state": "WARNING", "last_error_code": "CONTROL_UNIT_DETACHED"},
        },
    )
    saetze = _client(
        [g], {"magerwiese": "magerwiese", "hecke": "hecke"},
    ).geraete_status_snapshot(JETZT)

    nach_ventil = {s.ventil_id: s for s in saetze}
    assert set(nach_ventil) == {"", "v-k1", "v-k2"}
    assert nach_ventil["v-k1"].valve_state == "OK"
    assert nach_ventil["v-k2"].valve_state == "WARNING"
    assert nach_ventil["v-k2"].last_error_code == "CONTROL_UNIT_DETACHED"
    assert nach_ventil["v-k1"].zone_id == "magerwiese"
    assert nach_ventil["v-k2"].zone_id == "hecke"
    # Ventilblock nur auf der Geraeteebene, nicht am einzelnen Ventil.
    assert nach_ventil[""].valve_set_state == "WARNING"
    assert nach_ventil["v-k2"].valve_set_state == ""


def test_a_fehlende_werte_werden_leer_nicht_na():
    """"N/A" heisst "nicht geliefert" und darf nicht als Zustand durchgehen --
    sonst waere jedes Geraet dauerhaft im Fehler."""
    g = _Geraet(id="wc-2", valve_id="valve-2", valve_name="Hecke",
                valve_activity="CLOSED", valve_state="N/A",
                last_error_code="N/A")
    v = [s for s in _client([g]).geraete_status_snapshot(JETZT) if s.ventil_id][0]
    assert v.valve_state == "" and v.last_error_code == ""
    assert v.battery_level is None and v.rf_link_level is None


def test_a_ohne_verbindung_leere_liste():
    """Vor dem ersten `verbinden()` gibt es keinen State -- kein Absturz."""
    assert GardenaClient("test").geraete_status_snapshot(JETZT) == []


def test_a_fremde_geraetetypen_bleiben_draussen():
    g = _Geraet(id="mower-1", type="MOWER", name="Maeher")
    assert _client([g]).geraete_status_snapshot(JETZT) == []


# ================================================== C: Fehlerurteil (rein)

@pytest.mark.parametrize("state,code", [
    ("OK", "NO_MESSAGE"), ("OK", ""), ("", ""), ("", "NO_MESSAGE"),
])
def test_c_kein_fehler(state, code):
    assert _ist_fehler(state, code) is False


@pytest.mark.parametrize("state,code", [
    ("WARNING", "NO_MESSAGE"), ("ERROR", ""), ("UNAVAILABLE", ""),
    ("OK", "CONTROL_UNIT_DETACHED"), ("OK", "VALVE_CURRENT_MAX_EXCEEDED"),
])
def test_c_fehler(state, code):
    """AK5: jeder nicht-leere Code gilt, nicht nur ein geratener."""
    assert _ist_fehler(state, code) is True


# ========================================= B: Job gegen echten Speicher

@pytest.fixture
async def speicher(tmp_path):
    sp = Speicher(str(tmp_path / "t0584.db"))
    await sp.verbinden()
    try:
        yield sp
    finally:
        await sp.schliessen()


def _dswc(valve_state="OK", code="NO_MESSAGE", set_state="OK", set_code="NO_MESSAGE"):
    return _Geraet(
        id="dswc-2", type="SMART_IRRIGATION_CONTROL",
        name="Z Magerwiese und Hecke", battery_level=64, battery_state="OK",
        valve_set_state=set_state, valve_set_last_error_code=set_code,
        valves={
            "v-k1": {"id": "v-k1", "name": "Magerwiese", "activity": "CLOSED",
                     "state": "OK", "last_error_code": "NO_MESSAGE"},
            "v-k2": {"id": "v-k2", "name": "Hecke", "activity": "CLOSED",
                     "state": valve_state, "last_error_code": code},
        },
    )


def _job(speicher, geraet, heartbeat=30):
    client = _client([geraet], {"magerwiese": "magerwiese", "hecke": "hecke"})
    return GardenaStatusJob(speicher, client, heartbeat_minuten=heartbeat)


async def _warnungen(speicher, zone_id=None):
    offen = await speicher.offene_sensor_warnungen()
    return [
        w for w in offen
        if w.typ == SensorWarnungTyp.GERAETE_FEHLER
        and (zone_id is None or w.zone_id == zone_id)
    ]


@pytest.mark.asyncio
async def test_b_erster_lauf_schreibt_alle_zeilen(speicher):
    geschrieben = await _job(speicher, _dswc()).aktualisiere(JETZT)
    assert geschrieben == 3                      # Geraet + zwei Ventile
    zeile = await speicher.letzter_gardena_geraete_status("dswc-2", "v-k2")
    assert zeile is not None and zeile.zone_id == "hecke"
    assert zeile.valve_state == "OK"
    # AK1: die Reihe ist datierbar, der Zeitstempel kommt vom Lauf.
    assert zeile.zeitstempel == JETZT


@pytest.mark.asyncio
async def test_b_unveraendert_schreibt_nichts_neues(speicher):
    job = _job(speicher, _dswc())
    await job.aktualisiere(JETZT)
    assert await job.aktualisiere(JETZT + timedelta(minutes=5)) == 0


@pytest.mark.asyncio
async def test_b_heartbeat_schreibt_auch_ohne_aenderung(speicher):
    """Ohne Heartbeat waere nach einem Fehler nicht unterscheidbar, ob der
    Zustand stabil war oder der Job stillstand."""
    job = _job(speicher, _dswc())
    await job.aktualisiere(JETZT)
    assert await job.aktualisiere(JETZT + timedelta(minutes=31)) == 3


@pytest.mark.asyncio
async def test_b_aenderung_schreibt_sofort(speicher):
    geraet = _dswc()
    job = _job(speicher, geraet)
    await job.aktualisiere(JETZT)
    geraet.valves["v-k2"]["state"] = "WARNING"
    assert await job.aktualisiere(JETZT + timedelta(minutes=5)) == 1
    zeile = await speicher.letzter_gardena_geraete_status("dswc-2", "v-k2")
    assert zeile.valve_state == "WARNING"


@pytest.mark.asyncio
async def test_b_ventilzustand_warnt_die_zone_des_ventils(speicher):
    """AK3, Waechter 1: `state` != OK -> Warnung der Zone an diesem Ventil."""
    await _job(speicher, _dswc(valve_state="WARNING")).aktualisiere(JETZT)
    warnungen = await _warnungen(speicher)
    assert [w.zone_id for w in warnungen] == ["hecke"]
    assert "Zustand WARNING" in warnungen[0].details
    assert warnungen[0].geraet_id == "v-k2"      # je Ventil eigene Warnung


@pytest.mark.asyncio
async def test_b_fehlercode_warnt_auch_bei_zustand_ok(speicher):
    """Waechter 2: ein gesetzter Code genuegt, auch wenn `state` OK meldet."""
    await _job(
        speicher, _dswc(code="CONTROL_UNIT_DETACHED"),
    ).aktualisiere(JETZT)
    warnungen = await _warnungen(speicher, "hecke")
    assert len(warnungen) == 1
    assert "Fehlercode CONTROL_UNIT_DETACHED" in warnungen[0].details


@pytest.mark.asyncio
async def test_b_ventilblock_warnt_alle_zonen_des_geraets(speicher):
    """Waechter 3: `valve_set_state` haengt am Geraet, nicht an einem Ventil --
    betroffen sind beide Zonen (AK3 "Zone(n)"). `magerwiese` lief seit dem
    10.09. nicht; ueber ihren Kanal sagen die Messdaten also nichts."""
    await _job(speicher, _dswc(set_state="WARNING")).aktualisiere(JETZT)
    zonen = sorted(w.zone_id for w in await _warnungen(speicher))
    assert zonen == ["hecke", "magerwiese"]
    detail = (await _warnungen(speicher, "hecke"))[0].details
    assert "Ventilblock-Zustand WARNING" in detail
    assert "Akku 64 %" in detail


@pytest.mark.asyncio
async def test_b_ventilblock_fehlercode_warnt_ebenfalls(speicher):
    """Waechter 4: eigener Code auf Geraeteebene."""
    await _job(
        speicher, _dswc(set_code="CONTROL_UNIT_DETACHED"),
    ).aktualisiere(JETZT)
    assert len(await _warnungen(speicher)) == 2


@pytest.mark.asyncio
async def test_b_alles_ok_keine_warnung(speicher):
    """Kontrolle zu allen vier Waechtern: im Normalfall bleibt es still."""
    await _job(speicher, _dswc()).aktualisiere(JETZT)
    assert await _warnungen(speicher) == []


@pytest.mark.asyncio
async def test_b_warnung_wird_geschlossen_wenn_wieder_ok(speicher):
    geraet = _dswc(valve_state="WARNING")
    job = _job(speicher, geraet)
    await job.aktualisiere(JETZT)
    assert len(await _warnungen(speicher)) == 1
    geraet.valves["v-k2"]["state"] = "OK"
    await job.aktualisiere(JETZT + timedelta(minutes=5))
    assert await _warnungen(speicher) == []


@pytest.mark.asyncio
async def test_b_zweiter_lauf_erzeugt_keine_zweite_warnung(speicher):
    """Der Job laeuft jeden Zyklus; eine Warnung darf nur einmal offen sein.
    Der Text wird dabei aktuell gehalten (T-0397)."""
    geraet = _dswc(code="ERSTER_CODE")
    job = _job(speicher, geraet)
    await job.aktualisiere(JETZT)
    geraet.valves["v-k2"]["last_error_code"] = "ZWEITER_CODE"
    await job.aktualisiere(JETZT + timedelta(minutes=5))
    warnungen = await _warnungen(speicher, "hecke")
    assert len(warnungen) == 1
    assert "ZWEITER_CODE" in warnungen[0].details


@pytest.mark.asyncio
async def test_b_snapshot_fehler_kippt_den_zyklus_nicht(speicher):
    """Der Aufrufer ist der Entscheidungsloop -- der Job wirft nie."""
    class _Kaputt:
        def geraete_status_snapshot(self, jetzt=None):
            raise RuntimeError("WS weg")

    job = GardenaStatusJob(speicher, _Kaputt())
    assert await job.aktualisiere(JETZT) == 0
    assert job.letzter_fehler == "WS weg"


# ===================================== D: Anzeige-Pfade, die ein neuer
# Enum-Wert erfahrungsgemaess still verlaesst
# (fehlerpattern_neuer_enumwert_faellt_aus_positivfilter). Bei T-0416 hing
# genau das zwei Task-Generationen nach: Backend lieferte den Typ, die Karte
# zeigte den rohen Enum-String.

def test_d_ops_timeline_nennt_den_typ(monkeypatch):
    """Die Einstufung (KRITISCH) prueft seit 25.09.2026
    `test_ops_api.py::test_sensor_severity_ordnet_warntyp_ein` fuer alle
    Warntypen gemeinsam."""
    import bewaesserung.api_server as api
    from bewaesserung.api_server import _normalisiere_sensor_eintraege

    # `_zone_name` liest die Modul-Konfig; fuer den Titel genuegt eine
    # leere Zonenliste (dann gilt die zone_id als Name).
    monkeypatch.setattr(api._laufzeit, "konfig", type("K", (), {"zonen": []})())

    roh = [{
        "id": 1, "zeitstempel": JETZT.isoformat(), "zone_id": "hecke",
        "typ": SensorWarnungTyp.GERAETE_FEHLER.value,
        "details": "Gardena meldet fuer Ventil Hecke: Zustand WARNING",
        "behoben_um": None, "geraet_id": "v-k2",
    }]
    eintrag = _normalisiere_sensor_eintraege(roh, None)[0]
    # Ohne Eintrag in der Titel-Map stand hier der rohe Enum-String.
    assert eintrag["titel"] == "Gardena meldet Geraetefehler"


def test_d_frontend_karten_kennen_jeden_warntyp():
    """Beide Label-Maps der Zonen-Karte sind HANDkopien des Backend-Enums.

    Geprueft wird die Vollstaendigkeit der Zuordnung, nicht eine Rechnung:
    fehlt ein Typ, rendert die Karte `geraete_fehler` statt eines Satzes.
    """
    from pathlib import Path

    wurzel = Path(__file__).resolve().parents[2] / "frontend/src/komponenten/zonen-karte-v4"
    for datei in ("ZonenKarteV4Action.tsx", "ZonenKarteV4Drawer.tsx"):
        text = (wurzel / datei).read_text(encoding="utf-8")
        fehlend = [
            t.value for t in SensorWarnungTyp if f"{t.value}:" not in text
        ]
        assert not fehlend, f"{datei}: ohne Klartext {fehlend}"


def test_a_bodensensor_liefert_akku_und_funk_ohne_ventilzeile():
    """Isomorphie-Check (AK6): die Gardena-Bodensensoren tragen ueber dieselbe
    Basisklasse `battery_state`/`rf_link_*`. Bisher lasen wir von ihnen nur
    `battery_level` in `sensor_messung` -- der Funkzustand fehlte ganz.

    Ein Fehlerfeld haben sie nicht, es entsteht also nur die Reihe."""
    g = _Geraet(
        id="sensor-1", type="SOIL_SENSOR", name="Bodensensor Hecke",
        soil_humidity=42, battery_level=55, battery_state="LOW",
        rf_link_level=40, rf_link_state="ONLINE",
    )
    c = _client([g])
    c.registriere_geraet_zone("sensor-1", "hecke")
    saetze = c.geraete_status_snapshot(JETZT)

    assert len(saetze) == 1                     # keine Ventilzeile
    satz = saetze[0]
    assert satz.ventil_id == "" and satz.zone_id == "hecke"
    assert (satz.battery_state, satz.rf_link_state) == ("LOW", "ONLINE")
    assert satz.rf_link_level == 40.0
    assert _ist_fehler(satz.valve_set_state, satz.valve_set_last_error_code) is False
