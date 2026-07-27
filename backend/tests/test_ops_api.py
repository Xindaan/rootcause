import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bewaesserung.api_server import app, konfiguriere_api
from bewaesserung.modelle import (
    Ausloser,
    BewaesserungsEntscheidung,
    BlockerTyp,
    EntscheidungsScope,
    GardenaKonfig,
    GesamtKonfig,
    SensorWarnung,
    SensorWarnungTyp,
    SpeicherKonfig,
    StandortKonfig,
    VentilAktion,
    VentilEreignis,
    WetterEreignis,
    WetterEreignisTyp,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


# T-0317: Feste Tagesmitte als "jetzt" fuer die Ops-Tests. Die Fixture legt
# Events bis zu 35 min vor "jetzt" an; die Ops-Summary aggregiert auf
# `>= heute_start`. Mit echtem datetime.now() rutschen die Events in den ersten
# ~35 min nach Mitternacht auf den Vortag und fallen aus den 'heute'-Zaehlern
# (Mitternachts-Flaky). Loesung: Fixture UND Endpoint-Uhr auf denselben fixen
# Tagespunkt setzen, sodass die Zeitfenster deterministisch sind.
_FIXZEIT = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)


class _FixDatetime(datetime):
    """datetime-Subklasse, deren now() den fixen _FIXZEIT liefert; alle anderen
    Methoden (fromisoformat etc.) erbt sie unveraendert."""

    @classmethod
    def now(cls, tz=None):  # noqa: A002 - Signatur wie datetime.now
        return _FIXZEIT


def _erstelle_konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="test-client", client_secret="test-secret"),
        zonen=[
            ZonenKonfig(zone_id="rasen", name="Rasen Garten", ventil_kanal=1),
            ZonenKonfig(zone_id="hecke", name="Hecke Nord", ventil_kanal=1),
            ZonenKonfig(zone_id="balkon", name="Balkon West", ventil_kanal=2),
        ],
        wetter=WetterKonfig(
            standorte=[
                WetterStandortKonfig(id="standort_a", breite=52.52, laenge=13.405),
            ]
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten",
                name="Garten",
                wetter_standort="standort_a",
                zonen=["rasen", "hecke"],
            ),
            StandortKonfig(
                standort_id="balkon",
                name="Balkon",
                wetter_standort="standort_a",
                zonen=["balkon"],
            ),
        ],
    )


async def _fuelle_ops_daten(speicher: Speicher) -> None:
    # T-0317 Mitternachts-Schutz: feste Tagesmitte statt datetime.now(). Die
    # Ops-Summary aggregiert auf `>= heute_start`; mit `now()` rutschen die
    # Events (bis -35 min) in den ersten ~35 min nach Mitternacht auf den Vortag
    # und fallen aus den 'heute'-Zaehlern (Flaky). Der ops_client-Fixture friert
    # datetime.now() in den Endpoints auf denselben _FIXZEIT ein.
    jetzt = _FIXZEIT

    await speicher.speichere_entscheidung(
        BewaesserungsEntscheidung(
            zeitstempel=jetzt - timedelta(minutes=15),
            zone_id="rasen",
            soll_bewaessern=True,
            dauer_sekunden=720,
            begruendung="Kanal 1: Durchschnitt 28% unter 30%, Dauer 720s",
            scope=EntscheidungsScope.KANAL,
            scope_ref="1",
        )
    )
    await speicher.speichere_entscheidung(
        BewaesserungsEntscheidung(
            zeitstempel=jetzt - timedelta(minutes=35),
            zone_id="rasen",
            soll_bewaessern=False,
            dauer_sekunden=0,
            begruendung="Kanal 1: Durchschnitt 41% nicht unter Schwelle 30%",
            blocker_typ=BlockerTyp.FEUCHTE_OK,
            scope=EntscheidungsScope.KANAL,
            scope_ref="1",
        )
    )
    await speicher.speichere_entscheidung(
        BewaesserungsEntscheidung(
            zeitstempel=jetzt - timedelta(minutes=25),
            zone_id="rasen",
            soll_bewaessern=False,
            dauer_sekunden=0,
            begruendung="Kanal 1: Durchschnitt 40% nicht unter Schwelle 30%",
            blocker_typ=BlockerTyp.FEUCHTE_OK,
            scope=EntscheidungsScope.KANAL,
            scope_ref="1",
        )
    )

    await speicher.speichere_ventil_ereignis(
        VentilEreignis(
            zeitstempel=jetzt - timedelta(minutes=10),
            zone_id="rasen",
            ventil_id="ventil-1",
            aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=300,
            ausloser=Ausloser.AUTOMATIK,
        )
    )

    await speicher.speichere_wetter_ereignis(
        WetterEreignis(
            zeitstempel=jetzt - timedelta(minutes=5),
            typ=WetterEreignisTyp.FROST,
            standort_id="standort_a",
            details="Min -1.5C gegen 04:00",
        )
    )

    await speicher.oeffne_sensor_warnung(
        SensorWarnung(
            zeitstempel=jetzt - timedelta(days=2),
            zone_id="hecke",
            typ=SensorWarnungTyp.AUSFALL,
            details="Letztes Update vor 49.0h",
        )
    )
    await speicher.oeffne_sensor_warnung(
        SensorWarnung(
            zeitstempel=jetzt - timedelta(minutes=50),
            zone_id="balkon",
            typ=SensorWarnungTyp.AUSFALL,
            details="Letztes Update vor 4.5h",
        )
    )
    await speicher.schliesse_sensor_warnung(
        "balkon",
        SensorWarnungTyp.AUSFALL,
        jetzt - timedelta(minutes=20),
    )


@pytest.fixture
def ops_client(tmp_path, monkeypatch):
    # T-0317: Endpoint-Uhr auf denselben _FIXZEIT wie die Fixture-Daten frieren
    # (Mitternachts-Stabilitaet der 'heute'-Aggregate + Fenster-Filter).
    monkeypatch.setattr("bewaesserung.speicher.datetime", _FixDatetime)
    monkeypatch.setattr("bewaesserung.api_server.datetime", _FixDatetime)

    speicher = Speicher(str(tmp_path / "ops_api.db"))
    _run(speicher.verbinden())
    _run(_fuelle_ops_daten(speicher))

    konfiguriere_api(
        speicher,
        _erstelle_konfig(),
        MagicMock(),
        MagicMock(),
    )
    client = TestClient(app)

    try:
        yield client, speicher
    finally:
        client.close()
        _run(speicher.schliessen())


def test_ops_summary_aggregiert_shadow_wetter_sensor_und_bewaesserungen(ops_client):
    client, _ = ops_client

    antwort = client.get("/api/ops/summary")

    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["bewaesserungen_heute"] == 1
    assert daten["shadow_vorschlaege_heute"] == 1
    assert daten["blocker_verteilung"] == {"FEUCHTE_OK": 2}
    assert daten["wetter_warnungen"] == 1
    assert daten["sensor_warnungen"] == 1
    assert set(daten["zeitraum"]) == {"von", "bis"}


def test_health_endpoint_liefert_service_status(ops_client):
    """T-0140: `/api/health` ist public-minimal, Detail liegt unter
    `/api/health/detail`."""
    client, _ = ops_client

    minimal = client.get("/api/health")
    assert minimal.status_code == 200
    minimal_d = minimal.json()
    assert minimal_d["ok"] is True
    assert "zeitstempel" in minimal_d

    detail = client.get("/api/health/detail")
    assert detail.status_code == 200
    daten = detail.json()
    assert daten["ok"] is True
    assert daten["version"] == "0.1.0"
    assert daten["konfiguriert"] is True
    assert daten["zonen_anzahl"] == 3
    assert "zeitstempel" in daten


def test_zonen_uebersicht_enthaelt_ventil_kanal_und_optimum(ops_client):
    """/api/zonen liefert ventil_kanal + Pflanzen-Optimum + kritische Schwelle
    durch, damit Frontend-Warum-Panel Nachbar-Zonen am Kanal finden kann."""
    client, _ = ops_client

    antwort = client.get("/api/zonen")

    assert antwort.status_code == 200
    daten = antwort.json()
    zone_ids = {z["zone_id"] for z in daten}
    assert zone_ids == {"rasen", "hecke", "balkon"}
    rasen = next(z for z in daten if z["zone_id"] == "rasen")
    # Kernfelder fuer neuen Warum-Panel + SchwellenRange
    assert rasen["ventil_kanal"] == 1
    assert "feuchte_kritisch" in rasen
    assert "optimum_feuchte_min" in rasen
    assert "optimum_feuchte_max" in rasen


def test_ops_timeline_filtert_zone_und_reichert_kanal_shadow_an(ops_client):
    client, _ = ops_client

    antwort = client.get(
        "/api/ops/timeline?stunden=24&severity=aktion&zone_id=hecke"
    )

    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["aggregiert"]["routine_unterdueckt"] == 2
    assert len(daten["eintraege"]) == 1

    eintrag = daten["eintraege"][0]
    assert eintrag["id"].startswith("shadow:")
    assert eintrag["typ"] == "SHADOW_ENTSCHEIDUNG"
    assert eintrag["severity"] == "AKTION"
    assert eintrag["zone_id"] is None
    assert eintrag["scope"] == "kanal"
    assert eintrag["scope_ref"] == "1"
    assert eintrag["betroffene_zonen"] == ["rasen", "hecke"]


def test_ventil_severity_ordnet_ausloser_ein():
    """F11 (T-0397): Ops-Default zeigt Auffaelliges. Regime-ignorierte Laeufe
    und Solar-Pumpen-Zyklen fallen in ROUTINE (per Default unterdrueckt),
    anomale Closes in KRITISCH, reale/unklassifizierte Events in AKTION.
    Isomorphie: JEDER Ausloser-Wert bekommt eine definierte Severity."""
    from bewaesserung.api_server import _ventil_severity

    erwartung = {
        # F11b (Andre 11.07.): reale Laeufe (automatik/manuell) sind Routine --
        # Giess-Historie ist dafuer die Wahrheit. Ops-Default = Ausnahmen.
        Ausloser.IGNORIERT: "ROUTINE",
        Ausloser.AQUABLOOM: "ROUTINE",
        Ausloser.AUTOMATIK: "ROUTINE",
        Ausloser.MANUELL: "ROUTINE",
        # F11b-4 (13.07.): watchdog ist der normale Schliess-Fallback realer
        # Laeufe (Timer/Sleep), keine Anomalie -> ROUTINE. Nur notfall_stopp
        # (echter Eingriff) bleibt KRITISCH.
        Ausloser.WATCHDOG: "ROUTINE",
        Ausloser.NOTFALL_STOPP: "KRITISCH",
        Ausloser.UNBEKANNT: "AKTION",
    }
    # Vollstaendigkeit: kein Enum-Wert bleibt unklassifiziert.
    assert set(erwartung) == set(Ausloser)
    for ausloser, severity in erwartung.items():
        assert _ventil_severity(ausloser.value) == severity, ausloser
    # None (fehlender Ausloser) faellt sicher auf AKTION, nicht auf Crash.
    assert _ventil_severity(None) == "AKTION"


def test_f11b_shadow_edge_trigger_beruhigt_wiederholungen():
    """F11b (Andre 11.07. "nur bei Aenderung zeigen"): wiederholt identische
    Shadow-"Wuerde bewaessern"-Entscheidungen einer scope_ref bleiben nur beim
    ERSTEN Mal / bei deutlicher Dauer-Aenderung AKTION; unveraenderte Wieder-
    holungen werden zu ROUTINE degradiert (per Toggle weiter sichtbar)."""
    from bewaesserung.api_server import _normalisiere_shadow_eintraege

    def _sh(eid, stunde, soll, dauer_s, blocker=None):
        return {
            "id": eid,
            "zeitstempel": f"2026-07-11T{stunde:02d}:00:00",
            "zone_id": "magerwiese",
            "soll_bewaessern": soll,
            "dauer_sekunden": dauer_s,
            "begruendung": "test",
            "blocker_typ": blocker,
            "scope": EntscheidungsScope.KANAL.value,
            "scope_ref": "9",
        }

    roh = [
        _sh(1, 7, False, 0, blocker="FEUCHTE_OK"),   # kein Bedarf -> ROUTINE
        _sh(2, 8, True, 5400),   # erstmals wuerde-bewaessern (90 min) -> EDGE
        _sh(3, 9, True, 5280),   # 88 min: Jitter < 15 vs Anker 90 -> ROUTINE
        _sh(4, 10, False, 0, blocker="PAUSE_AKTIV"),  # Selbst-Drossel -> ROUTINE,
                                                      # darf Anker NICHT reset.
        _sh(5, 11, True, 5400),  # 90 min NACH Pause: gleiche Absicht -> ROUTINE
        _sh(6, 12, True, 3060),  # 51 min: >= 15 vs Anker 90 -> EDGE
    ]
    e = _normalisiere_shadow_eintraege(roh, zone_filter=None)
    nach_id = {int(x["id"].split(":")[1]): x for x in e}
    assert nach_id[1]["severity"] == "ROUTINE"
    assert nach_id[2]["severity"] == "AKTION"
    # Jitter (88/90 min) kollabiert gegen den zuletzt gezeigten Anker (90).
    assert nach_id[3]["severity"] == "ROUTINE"
    assert nach_id[3]["meta"].get("shadow_wiederholung") is True
    # PAUSE_AKTIV ist Selbst-Drossel -> zaehlt als Giess-Fortsetzung, resettet
    # den Anker nicht -> die naechste identische wuerde-bewaessern kollabiert.
    assert nach_id[5]["severity"] == "ROUTINE"
    # Deutliche Aenderung (51 min) -> wieder sichtbar.
    assert nach_id[6]["severity"] == "AKTION"


def test_shadow_kanal_disambiguiert_dswc_bei_gleicher_kanal_nummer():
    """F15/T-0252: Eine KANAL-Shadow-Entscheidung darf NICHT auf Zonen
    eines ANDEREN DSWC mit gleicher Kanal-Nummer expandieren.

    Realfall: waldblumen (DSWC1, Kanal 1) und magerwiese (DSWC2, Kanal 1)
    teilen die Kanal-Nummer, sind aber physisch getrennte Kreise. Vorher
    schluesselte `_kanal_zonen_map` rein nach `str(kanal)` -> eine
    Entscheidung fuer magerwiese listete faelschlich waldblumen mit.
    Die persistierte `zone_id` (ref_zone) disambiguiert das DSWC-Geraet.
    """
    from bewaesserung.api_server import _normalisiere_shadow_eintraege

    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="c", client_secret="s"),
        zonen=[
            ZonenKonfig(zone_id="waldblumen", name="Waldblumen",
                        ventil_kanal=1, ventil_geraet_id="dswc-1"),
            ZonenKonfig(zone_id="hecke", name="Hecke",
                        ventil_kanal=1, ventil_geraet_id="dswc-1"),
            ZonenKonfig(zone_id="magerwiese", name="Magerwiese",
                        ventil_kanal=1, ventil_geraet_id="dswc-2"),
        ],
        wetter=WetterKonfig(
            standorte=[
                WetterStandortKonfig(id="standort_a", breite=52.52, laenge=13.405),
            ]
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
    )
    konfiguriere_api(MagicMock(), konfig, MagicMock(), MagicMock())

    def _roh(zone_id: str, eid: int) -> dict:
        return {
            "id": eid,
            "zeitstempel": datetime.now(),
            "zone_id": zone_id,
            "soll_bewaessern": True,
            "dauer_sekunden": 600,
            "begruendung": f"Kanal 1: {zone_id} trocken",
            "blocker_typ": None,
            "scope": EntscheidungsScope.KANAL.value,
            "scope_ref": "1",
        }

    # DSWC-2-Entscheidung: nur magerwiese, NICHT die DSWC-1-Zonen.
    dswc2 = _normalisiere_shadow_eintraege([_roh("magerwiese", 1)], zone_filter=None)
    assert len(dswc2) == 1
    assert dswc2[0]["betroffene_zonen"] == ["magerwiese"]
    assert "waldblumen" not in dswc2[0]["betroffene_zonen"]

    # Gegenprobe: DSWC-1-Entscheidung listet beide DSWC-1-Zonen, nicht magerwiese.
    dswc1 = _normalisiere_shadow_eintraege([_roh("waldblumen", 2)], zone_filter=None)
    assert dswc1[0]["betroffene_zonen"] == ["waldblumen", "hecke"]
    assert "magerwiese" not in dswc1[0]["betroffene_zonen"]

    # Zonenfilter respektiert die Disambiguierung: Filter auf waldblumen
    # darf die magerwiese-Entscheidung NICHT durchlassen.
    gefiltert = _normalisiere_shadow_eintraege(
        [_roh("magerwiese", 3)], zone_filter="waldblumen",
    )
    assert gefiltert == []


def test_ops_timeline_scharf_indikativ_shadow_sichtbar_zone_scope_dedupe():
    """T-0388: Der Shadow-Feed log drei Dinge falsch.

    (a) Zone-Scope-Zeilen SCHARFER Zonen sind Duplikate ihrer KANAL-Zeile -> raus.
    (b) Shadow-Zonen erzeugen seit T-0334 GAR KEINE KANAL-Zeile (`pruefe_kanal`
        laeuft nur fuer opt-in) -- sie muessen ueber ihre Zone-Zeile sichtbar
        bleiben. Realfall: waldblumenhain, 0 KANAL-Zeilen seit 26.06. -> der Feed
        zeigte fuer die EINZIGE echte Shadow-Zone nichts.
    (c) Label: Indikativ ("Bewaessert") wenn die Zone scharf ist -- vorher stand
        fuer hecke/bambuswald "Wuerde bewaessern", obwohl echtes Wasser floss.
    """
    from bewaesserung.api_server import _normalisiere_shadow_eintraege

    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="c", client_secret="s"),
        ventilsteuerung_aktiv=True,          # globaler Master-Switch an
        zonen=[
            # scharf: automatik + kanal + opt-in
            ZonenKonfig(zone_id="hecke", name="Hecke", ventil_kanal=2,
                        ventil_geraet_id="dswc-1", modus=ZonenModus.AUTOMATIK,
                        auto_loop_opt_in=True),
            # Shadow: automatik + kanal, aber opt-OUT
            ZonenKonfig(zone_id="waldblumen", name="Waldblumen", ventil_kanal=1,
                        ventil_geraet_id="dswc-1", modus=ZonenModus.AUTOMATIK,
                        auto_loop_opt_in=False),
        ],
        wetter=WetterKonfig(
            standorte=[
                WetterStandortKonfig(id="standort_a", breite=52.52, laenge=13.405),
            ]
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
    )
    konfiguriere_api(MagicMock(), konfig, MagicMock(), MagicMock())

    def _roh(zone_id: str, eid: int, scope: str, scope_ref: str = "1") -> dict:
        return {
            "id": eid,
            "zeitstempel": datetime.now(),
            "zone_id": zone_id,
            "soll_bewaessern": True,
            "dauer_sekunden": 600,
            "begruendung": f"{zone_id} trocken",
            "blocker_typ": None,
            "scope": scope,
            "scope_ref": scope_ref,
        }

    # (a) Zone-Scope einer SCHARFEN Zone -> Duplikat, faellt raus
    dupe = _normalisiere_shadow_eintraege(
        [_roh("hecke", 1, EntscheidungsScope.ZONE.value)], zone_filter=None,
    )
    assert dupe == [], "Zone-Zeile einer scharfen Zone dupliziert ihre KANAL-Zeile"

    # (b) Zone-Scope der SHADOW-Zone -> sichtbar, Konjunktiv
    shadow = _normalisiere_shadow_eintraege(
        [_roh("waldblumen", 2, EntscheidungsScope.ZONE.value)], zone_filter=None,
    )
    assert len(shadow) == 1, "Shadow-Zone muss im Feed erscheinen"
    assert shadow[0]["titel"].startswith("Wuerde bewaessern")
    assert shadow[0]["meta"]["autonom_scharf"] is False

    # (c) KANAL-Zeile der SCHARFEN Zone -> Indikativ
    scharf = _normalisiere_shadow_eintraege(
        [_roh("hecke", 3, EntscheidungsScope.KANAL.value, scope_ref="2")],
        zone_filter=None,
    )
    assert len(scharf) == 1
    assert scharf[0]["titel"].startswith("Bewaessert"), scharf[0]["titel"]
    assert scharf[0]["meta"]["autonom_scharf"] is True
    # F11b (Andre 11.07.): eine scharfe "Bewaessert"-Zeile ist ein realer Lauf
    # -> ROUTINE (Giess-Historie/Live-Status), keine Ops-Ausnahme. Label +
    # autonom_scharf-Flag (T-0388) bleiben, nur die Severity faellt auf ROUTINE.
    assert scharf[0]["severity"] == "ROUTINE"

    # (d) Die ROUTINE-Aggregation darf das Flag nicht verlieren -- sonst liest
    #     das Frontend fuer eine scharfe Zone `undefined` -> faelschlich Shadow.
    from bewaesserung.api_server import _aggregiere_routine_eintraege

    routine_roh = _roh("hecke", 4, EntscheidungsScope.KANAL.value, scope_ref="2")
    routine_roh["soll_bewaessern"] = False
    routine_roh["blocker_typ"] = "FEUCHTE_OK"
    # `_aggregiere_routine_eintraege` bucketet ueber `_stunden_bucket`, das den
    # ISO-String erwartet, den SQLite liefert (nicht das datetime aus `_roh`).
    routine_roh["zeitstempel"] = "2026-07-10T07:12:00"
    routine = _normalisiere_shadow_eintraege([routine_roh], zone_filter=None)
    assert routine[0]["severity"] == "ROUTINE"
    aggregiert = _aggregiere_routine_eintraege(routine)
    assert len(aggregiert) == 1
    assert aggregiert[0]["meta"]["autonom_scharf"] is True


def test_ops_timeline_aggregiert_routine_und_zaehlt_unterdrueckte_eintraege(ops_client):
    client, _ = ops_client

    standard = client.get("/api/ops/timeline?stunden=24")
    routine = client.get("/api/ops/timeline?stunden=24&severity=routine")

    assert standard.status_code == 200
    # F11b: die automatik-Bewaesserung der Fixture ist jetzt ROUTINE (reale
    # Laeufe -> Giess-Historie), zaehlt also mit zu den unterdrueckten.
    assert standard.json()["aggregiert"]["routine_unterdueckt"] == 4

    assert routine.status_code == 200
    daten = routine.json()
    assert daten["aggregiert"]["routine_unterdueckt"] == 0

    shadow_eintraege = [
        eintrag for eintrag in daten["eintraege"]
        if eintrag["id"].startswith("routine:")
    ]
    sensor_eintraege = [
        eintrag for eintrag in daten["eintraege"]
        if eintrag["typ"] == "SENSOR_WARNUNG"
    ]

    assert len(sensor_eintraege) == 1
    assert len(shadow_eintraege) in {1, 2}
    assert sum(eintrag["meta"]["anzahl"] for eintrag in shadow_eintraege) == 2
    assert all(eintrag["severity"] == "ROUTINE" for eintrag in shadow_eintraege)
    assert all(eintrag["meta"]["aggregiert"] is True for eintrag in shadow_eintraege)


def test_ventil_ereignisse_endpoint_filtert_zeitraum(ops_client):
    client, _ = ops_client
    # Fixture schreibt 1 Bewaesserung fuer 'rasen' (10 min vor jetzt)
    r = client.get("/api/ventil-ereignisse")
    assert r.status_code == 200
    daten = r.json()
    # Mindestens das 1 SCHLIESSEN aus _fuelle_ops_daten
    assert len(daten) >= 1
    # Alle Eintraege haben id + ausloser + zone_id
    for eintrag in daten:
        assert "id" in eintrag and eintrag["id"] is not None
        assert "ausloser" in eintrag
        assert "zone_id" in eintrag


def test_patch_ventil_ereignis_aendert_ausloser_und_zeitstempel(ops_client):
    client, _ = ops_client
    # Aktuelle Liste holen, erstes Element aendern
    liste = client.get("/api/ventil-ereignisse").json()
    assert len(liste) >= 1
    e = liste[0]
    eid = e["id"]

    # Ausloser + Zeitstempel korrigieren
    r = client.patch(
        f"/api/ventil-ereignis/{eid}",
        json={"ausloser": "unbekannt", "zeitstempel": "2026-04-19T05:37:00+02:00"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Verifikation
    liste2 = client.get("/api/ventil-ereignisse?von=2026-04-19T00:00").json()
    geaendert = [x for x in liste2 if x["id"] == eid]
    assert len(geaendert) == 1
    assert geaendert[0]["ausloser"] == "unbekannt"
    # Zeitstempel nach Normalisierung (lokal, tz-naive)
    assert geaendert[0]["zeitstempel"].startswith("2026-04-19T05:37:00")


def test_patch_ventil_ereignis_ablehnt_ungueltigen_ausloser(ops_client):
    client, _ = ops_client
    liste = client.get("/api/ventil-ereignisse").json()
    eid = liste[0]["id"]
    r = client.patch(f"/api/ventil-ereignis/{eid}", json={"ausloser": "quatsch"})
    assert r.status_code == 200
    assert "fehler" in r.json()


def test_delete_ventil_ereignis_entfernt_eintrag(ops_client):
    client, _ = ops_client
    liste = client.get("/api/ventil-ereignisse").json()
    eid = liste[0]["id"]
    r = client.delete(f"/api/ventil-ereignis/{eid}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # Nicht mehr da
    liste_neu = client.get("/api/ventil-ereignisse").json()
    assert not any(x["id"] == eid for x in liste_neu)


def test_patch_mit_paar_flag_aendert_oeffnen_und_schliessen(ops_client):
    """Codex-Finding P2: Heuristik-Events kommen paarweise; PATCH mit
    ?paar=true muss beide Enden klassifizieren."""
    client, speicher = ops_client
    # Paar aus OEFFNEN + SCHLIESSEN mit gleichem ventil_id + ausloser=UNBEKANNT.
    # Beide Zeitstempel in der Vergangenheit, damit default-bis=jetzt sie findet.
    jetzt = _FIXZEIT  # T-0317: konsistent mit gefrorener Endpoint-Uhr
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=jetzt - timedelta(seconds=90),
        zone_id="rasen", ventil_id="sensor_heuristik",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser("unbekannt"),
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=jetzt - timedelta(seconds=30),
        zone_id="rasen", ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=60,
        ausloser=Ausloser("unbekannt"),
    )))
    liste = client.get("/api/ventil-ereignisse?zone_id=rasen").json()
    heuristik = [e for e in liste if e["ventil_id"] == "sensor_heuristik"]
    assert len(heuristik) == 2
    oeffnen_id = next(e["id"] for e in heuristik if e["aktion"] == "oeffnen")

    # PATCH mit paar=true
    r = client.patch(
        f"/api/ventil-ereignis/{oeffnen_id}?paar=true",
        json={"ausloser": "automatik"},
    )
    assert r.status_code == 200
    assert r.json()["geaendert"] == 2

    nach = client.get("/api/ventil-ereignisse?zone_id=rasen").json()
    heuristik_nach = [e for e in nach if e["ventil_id"] == "sensor_heuristik"]
    assert all(e["ausloser"] == "automatik" for e in heuristik_nach)


def test_patch_ventil_ereignis_schreibt_audit_in_ausloser_korrektur(ops_client):
    """T-0084: Bei Aenderung des Auslosers wird `ausloser_korrektur` mit
    `{alt, neu, geaendert_am}` gefuellt. Bei Same-Value-Patch nicht."""
    client, speicher = ops_client
    liste = client.get("/api/ventil-ereignisse").json()
    eid = liste[0]["id"]
    alt_ausloser = liste[0]["ausloser"]
    neu_ausloser = "manuell" if alt_ausloser != "manuell" else "automatik"

    r = client.patch(f"/api/ventil-ereignis/{eid}", json={"ausloser": neu_ausloser})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    async def _audit():
        async with speicher._db.execute(
            "SELECT ausloser, ausloser_korrektur FROM ventil_ereignis WHERE id = ?",
            (eid,),
        ) as cursor:
            return await cursor.fetchone()

    zeile = _run(_audit())
    assert zeile["ausloser"] == neu_ausloser
    assert zeile["ausloser_korrektur"] is not None
    import json as _json
    audit = _json.loads(zeile["ausloser_korrektur"])
    assert audit["alt"] == alt_ausloser
    assert audit["neu"] == neu_ausloser
    assert "geaendert_am" in audit


def test_patch_ventil_ereignis_kein_audit_bei_unveraendertem_ausloser(ops_client):
    """T-0084: Patch mit identischem Ausloser-Wert schreibt KEINEN Audit."""
    client, speicher = ops_client
    liste = client.get("/api/ventil-ereignisse").json()
    eid = liste[0]["id"]
    alt_ausloser = liste[0]["ausloser"]

    r = client.patch(f"/api/ventil-ereignis/{eid}", json={"ausloser": alt_ausloser})
    assert r.status_code == 200

    async def _audit():
        async with speicher._db.execute(
            "SELECT ausloser_korrektur FROM ventil_ereignis WHERE id = ?",
            (eid,),
        ) as cursor:
            return await cursor.fetchone()

    zeile = _run(_audit())
    assert zeile["ausloser_korrektur"] is None


def test_ops_timeline_meta_enthaelt_ventil_event_id_und_korrektur_marker(ops_client):
    """T-0084: Frontend braucht numerische ID + Korrektur-Flag in meta,
    sonst kein Edit-Button im OpsTimelineEintrag."""
    client, _ = ops_client

    # F11b: die automatik-Bewaesserung der Fixture ist ROUTINE -> Routine-Filter
    # mitgeben, damit das VENTIL_EREIGNIS (mit Edit-Meta) im View auftaucht.
    antwort = client.get(
        "/api/ops/timeline?stunden=24&severity=kritisch,aktion,wetter,routine"
    )
    assert antwort.status_code == 200
    eintraege = antwort.json()["eintraege"]
    ventil = [e for e in eintraege if e["typ"] == "VENTIL_EREIGNIS"]
    assert len(ventil) >= 1
    e = ventil[0]
    assert isinstance(e["meta"]["ventil_event_id"], int)
    assert e["meta"]["ausloeser_korrigiert"] is False  # noch keine Korrektur


def test_delete_mit_paar_flag_entfernt_beide(ops_client):
    """Codex-Finding P2 Gegenprobe: DELETE?paar=true entfernt OEFFNEN+SCHLIESSEN."""
    client, speicher = ops_client
    jetzt = _FIXZEIT  # T-0317: konsistent mit gefrorener Endpoint-Uhr
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=jetzt - timedelta(seconds=90),
        zone_id="hecke", ventil_id="sensor_heuristik",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser("unbekannt"),
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=jetzt - timedelta(seconds=30),
        zone_id="hecke", ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=60,
        ausloser=Ausloser("unbekannt"),
    )))
    liste = client.get("/api/ventil-ereignisse?zone_id=hecke").json()
    heuristik = [e for e in liste if e["ventil_id"] == "sensor_heuristik"]
    assert len(heuristik) == 2
    oeffnen_id = next(e["id"] for e in heuristik if e["aktion"] == "oeffnen")

    r = client.delete(f"/api/ventil-ereignis/{oeffnen_id}?paar=true")
    assert r.status_code == 200
    assert r.json()["geloescht"] == 2

    nach = client.get("/api/ventil-ereignisse?zone_id=hecke").json()
    heuristik_nach = [e for e in nach if e["ventil_id"] == "sensor_heuristik"]
    assert len(heuristik_nach) == 0


def test_delete_unbekannte_id_gibt_fehler(ops_client):
    client, _ = ops_client
    r = client.delete("/api/ventil-ereignis/99999999")
    assert r.status_code == 200
    assert "fehler" in r.json()


def test_entscheidungen_endpoint_filtert_zone_und_enthaelt_blocker_typ(ops_client):
    client, _ = ops_client

    # Ohne Filter: alle drei Fixture-Entscheidungen
    alle = client.get("/api/entscheidungen")
    assert alle.status_code == 200
    assert len(alle.json()) == 3

    # Zone-Filter: nur rasen-Entscheidungen (alle in Fixtures sind rasen,
    # aber der Filter muss explizit durchgereicht werden)
    nur_rasen = client.get("/api/entscheidungen?zone_id=rasen&limit=2")
    assert nur_rasen.status_code == 200
    daten_rasen = nur_rasen.json()
    assert len(daten_rasen) == 2
    assert all(e["zone_id"] == "rasen" for e in daten_rasen)

    # Unbekannte Zone liefert leere Liste statt 500
    leer = client.get("/api/entscheidungen?zone_id=gibt_es_nicht")
    assert leer.status_code == 200
    assert leer.json() == []

    # Strukturierte Felder: blocker_typ + scope muessen dabei sein,
    # damit das "Warum"-Panel (T-0028) gezielt rendern kann.
    erste = daten_rasen[0]
    assert set(["zeitstempel", "zone_id", "soll_bewaessern", "dauer_sekunden",
                "begruendung", "blocker_typ", "scope", "scope_ref"]).issubset(erste.keys())
    # Mindestens ein Eintrag hat blocker_typ=FEUCHTE_OK (Fixture)
    blocker_typen = {e["blocker_typ"] for e in daten_rasen}
    assert "FEUCHTE_OK" in blocker_typen


def test_entscheidungen_endpoint_blocker_filter_und_zeitraum(ops_client):
    """T-0037: von/bis/blocker_typ filtern serverseitig, CSV-Format liefert Text."""
    client, _ = ops_client

    # blocker_typ=FEUCHTE_OK liefert nur die beiden Fixture-Eintraege mit diesem Typ.
    feuchte = client.get("/api/entscheidungen?blocker_typ=FEUCHTE_OK")
    assert feuchte.status_code == 200
    daten = feuchte.json()
    assert len(daten) == 2
    assert all(e["blocker_typ"] == "FEUCHTE_OK" for e in daten)

    # Zeitraum-Filter: `von` in der nahen Vergangenheit laesst den aeltesten
    # Eintrag (-35 min) aus und behaelt die juengeren.
    seit_10_min = (_FIXZEIT - timedelta(minutes=30)).isoformat()
    juengere = client.get(f"/api/entscheidungen?von={seit_10_min}")
    assert juengere.status_code == 200
    assert len(juengere.json()) == 2

    # CSV-Format: Content-Type + Header + wenigstens eine Datenzeile.
    csv = client.get("/api/entscheidungen?format=csv")
    assert csv.status_code == 200
    assert csv.headers["content-type"].startswith("text/csv")
    zeilen = csv.text.strip().splitlines()
    assert zeilen[0].startswith("zeitstempel,zone_id,scope")
    assert len(zeilen) == 1 + 3  # Header + 3 Fixture-Eintraege


def test_ops_timeline_zeigt_behobene_sensorwarnung_nur_in_routine(ops_client):
    client, _ = ops_client

    kritisch = client.get("/api/ops/timeline?stunden=24&severity=kritisch")
    routine = client.get("/api/ops/timeline?stunden=24&severity=routine")

    assert kritisch.status_code == 200
    kritische_sensoren = [
        eintrag for eintrag in kritisch.json()["eintraege"]
        if eintrag["typ"] == "SENSOR_WARNUNG"
    ]
    assert len(kritische_sensoren) == 1
    assert kritische_sensoren[0]["zone_id"] == "hecke"

    assert routine.status_code == 200
    routine_sensoren = [
        eintrag for eintrag in routine.json()["eintraege"]
        if eintrag["typ"] == "SENSOR_WARNUNG"
    ]
    assert len(routine_sensoren) == 1
    assert routine_sensoren[0]["zone_id"] == "balkon"
    assert routine_sensoren[0]["severity"] == "ROUTINE"
    assert routine_sensoren[0]["titel"] == "Sensor-Ausfall behoben"
    assert routine_sensoren[0]["meta"]["behoben_um"] is not None
