"""Tests fuer T-0126 (H-2) WatchdogJob.

Deckt die zwei MVP-Trigger ab:
- AKUT_IN_FOLGE: empfehlungs_typ='akut' an N Tagen in Folge -> Push.
- HUSQVARNA_BLOCK: < M Gardena-Sensor-Messungen in F Min -> Push.

Plus Throttle (max 1 Push pro Klasse + Zone pro throttle_stunden) und
Feature-Schalter (aktiv=False / leerer Empfaenger -> kein Push).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import (
    DatenQuelle,
    SensorMessung,
    WatchdogKonfig,
    ZeitFenster,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.speicher import Speicher
from bewaesserung.watchdog import (
    GLOBAL_ZONE_ID,
    TYP_HUSQVARNA_BLOCK,
    WatchdogJob,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "watchdog.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


class _BenachrichtigerStub:
    """Sammelt Aufrufe von sende_text statt iMessage zu feuern."""

    def __init__(self, *, erfolg: bool = True):
        self.gesendet: list[tuple[str, str]] = []
        self._erfolg = erfolg

    async def sende_text(self, empfaenger: str, text: str) -> bool:
        self.gesendet.append((empfaenger, text))
        return self._erfolg


def _baue_zone(zone_id: str, name: str = "Test") -> ZonenKonfig:
    return ZonenKonfig(
        zone_id=zone_id,
        name=name,
        modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=35.0,
        feuchte_kritisch=20.0,
        max_dauer_sekunden=1800,
        min_pause_minuten=120,
        tages_budget_sekunden=120.0,
        bevorzugte_zeiten=[ZeitFenster(von="05:00", bis="07:00")],
    )


async def _audit_typ(speicher: Speicher, zone_id: str, ts, typ: str) -> None:
    await speicher.setze_empfehlungs_audit(
        zeitstempel=ts,
        zone_id=zone_id,
        empfehlungs_typ=typ,
        soll_bewaessern=(typ == "akut"),
        blocker_typ=None,
        feuchte_aktuell=30.0,
        welkepunkt_wert=20.0,
        optimum_min=35.0,
        optimum_max=65.0,
        prognose_quelle="heuristik",
        prognose_6h=28.0,
        prognose_12h=25.0,
        prognose_24h=22.0,
        tage_bis_welkepunkt=1.0,
        dauer_s_empfehlung=600,
        aktive_strategie="standard",
    )


async def _audit_akut(speicher: Speicher, zone_id: str, ts) -> None:
    await _audit_typ(speicher, zone_id, ts, "akut")


async def _flute_sensor(
    speicher: Speicher, zone_id: str, jetzt: datetime, anzahl: int = 6,
) -> None:
    """Sorgt fuer eine frische Gardena-Messung (juenger als 90 min), damit
    der Husqvarna-Trigger bei nicht-husqvarna-Tests ruhig bleibt. Die
    juengste Messung ist 5 min alt -- klar unter `husqvarna_max_alter_minuten`."""
    for i in range(anzahl):
        await speicher.speichere_messung(SensorMessung(
            zeitstempel=jetzt - timedelta(minutes=5 + i * 8),
            zone_id=zone_id,
            boden_feuchte=42.0,
            quelle=DatenQuelle.GARDENA,
        ))


def _baue_konfig(**overrides) -> WatchdogKonfig:
    defaults = dict(
        aktiv=True,
        empfaenger="+491234567890",
        intervall_minuten=1,         # damit der Zweittick nicht throttled
        throttle_stunden=24,
        akut_in_folge_tage=3,
        husqvarna_max_alter_minuten=90,
    )
    defaults.update(overrides)
    return WatchdogKonfig(**defaults)


# --- Trigger A: AKUT_IN_FOLGE -----------------------------------------------

def test_akut_3_tage_in_folge_loest_push_aus(speicher):
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    konfig = _baue_konfig()
    jetzt = datetime(2026, 5, 4, 18, 0)

    _run(_flute_sensor(speicher, zone.zone_id, jetzt))
    # 3 Snapshots: heute, gestern, vorgestern -- jeweils 'akut'.
    for tage_zurueck in (0, 1, 2):
        _run(_audit_akut(speicher, zone.zone_id, jetzt - timedelta(days=tage_zurueck, hours=1)))

    job = WatchdogJob(
        speicher=speicher,
        benachrichtiger=benach,
        konfig=konfig,
        zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))

    assert stat["gesendet"] == 1
    assert len(benach.gesendet) == 1
    empfaenger, text = benach.gesendet[0]
    assert empfaenger == konfig.empfaenger
    assert "akut" in text.lower()
    assert "test" in text.lower() or "bambus" in text.lower()


def test_akut_nur_2_tage_kein_push(speicher):
    # Wichtig: 1-2 Tage faengt der User selbst beim Dashboard-Check ab,
    # erst der 3. Tag triggert (Memory feedback_app_check_gewohnheit.md).
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)

    _run(_flute_sensor(speicher, zone.zone_id, jetzt))
    for tage_zurueck in (0, 1):
        _run(_audit_akut(speicher, zone.zone_id, jetzt - timedelta(days=tage_zurueck, hours=1)))

    job = WatchdogJob(
        speicher=speicher,
        benachrichtiger=benach,
        konfig=_baue_konfig(),
        zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))

    assert stat["gesendet"] == 0
    assert benach.gesendet == []


def test_akut_throttle_verhindert_zweiten_push(speicher):
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)

    _run(_flute_sensor(speicher, zone.zone_id, jetzt))
    for tage_zurueck in (0, 1, 2):
        _run(_audit_akut(speicher, zone.zone_id, jetzt - timedelta(days=tage_zurueck, hours=1)))

    job = WatchdogJob(
        speicher=speicher,
        benachrichtiger=benach,
        konfig=_baue_konfig(),
        zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
    )
    # Erster Tick: feuert
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    # Zweiter Tick 2 h spaeter: Throttle 24 h -> kein zweiter Push
    spaeter = jetzt + timedelta(hours=2)
    _run(_flute_sensor(speicher, zone.zone_id, spaeter))  # Husqvarna ruhig halten
    job._letzte_aktualisierung = None  # simuliere Restart-Reset
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=spaeter))
    assert stat["gesendet"] == 0
    assert len(benach.gesendet) == 1


# --- Trigger B: HUSQVARNA_BLOCK ---------------------------------------------

def test_husqvarna_block_loest_push_aus(speicher):
    """Juengste Messung > 90 min alt (Schwelle) -> Block-Push."""
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)

    # Eine Messung 120 min alt -- ueber 90-min-Schwelle = Block.
    _run(speicher.speichere_messung(SensorMessung(
        zeitstempel=jetzt - timedelta(minutes=120),
        zone_id=zone.zone_id,
        boden_feuchte=42.0,
        quelle=DatenQuelle.GARDENA,
    )))

    job = WatchdogJob(
        speicher=speicher,
        benachrichtiger=benach,
        konfig=_baue_konfig(),
        zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))

    assert stat["gesendet"] == 1
    assert "husqvarna" in benach.gesendet[0][1].lower()
    assert "120 min" in benach.gesendet[0][1]
    # Throttle persistiert
    zuletzt = _run(speicher.hole_letzten_watchdog_push(
        TYP_HUSQVARNA_BLOCK, GLOBAL_ZONE_ID,
    ))
    assert zuletzt == jetzt


def test_husqvarna_normaler_betrieb_kein_push(speicher):
    """Realdaten-Cadence: 1 Messung pro Sensor pro Stunde reicht --
    juengste Messung ist 30 min alt = unter Schwelle, kein Push."""
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)

    # Nur 2 Messungen, juengste 30 min alt -- weit unter 90 min Schwelle.
    for vor_min in (30, 95):
        _run(speicher.speichere_messung(SensorMessung(
            zeitstempel=jetzt - timedelta(minutes=vor_min),
            zone_id=zone.zone_id,
            boden_feuchte=42.0,
            quelle=DatenQuelle.GARDENA,
        )))

    job = WatchdogJob(
        speicher=speicher,
        benachrichtiger=benach,
        konfig=_baue_konfig(),
        zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    assert stat["gesendet"] == 0


def test_husqvarna_keine_messung_ueberhaupt_kein_push(speicher):
    """Erstinstall: keine Gardena-Messung jemals -> kein Push (nicht spammen)."""
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)
    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach, konfig=_baue_konfig(),
        zonen=[zone], gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    assert stat["gesendet"] == 0


def test_husqvarna_keine_gardena_zonen_kein_push(speicher):
    # Nur FYTA-Zonen in der Config -> Husqvarna-Trigger irrelevant.
    zone = _baue_zone("zitrus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)

    job = WatchdogJob(
        speicher=speicher,
        benachrichtiger=benach,
        konfig=_baue_konfig(),
        zonen=[zone],
        gardena_zone_ids=[],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    assert stat["gesendet"] == 0


# --- Feature-Schalter -------------------------------------------------------

def test_aktiv_false_macht_keinen_tick(speicher):
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)
    job = WatchdogJob(
        speicher=speicher,
        benachrichtiger=benach,
        konfig=_baue_konfig(aktiv=False),
        zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    assert stat == {"geprueft": 0, "gesendet": 0}
    assert benach.gesendet == []


def test_leerer_empfaenger_macht_keinen_tick(speicher):
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)
    job = WatchdogJob(
        speicher=speicher,
        benachrichtiger=benach,
        konfig=_baue_konfig(empfaenger=""),
        zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    assert stat == {"geprueft": 0, "gesendet": 0}
    assert benach.gesendet == []


def test_endpoint_health_alt_genug_loest_push_aus(speicher):
    """T-0132 (H-8): Endpoint mit status='schema_fehler' und letzter_erfolg
    > 24 h zurueck -> Watchdog-Push."""
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    # Endpoint vor 3 Tagen erfolgreich, jetzt seit 2 Tagen schema_fehler
    erfolg = jetzt - timedelta(days=3)
    fehler_seit = jetzt - timedelta(days=2)
    _run(speicher.setze_endpoint_health(
        "gardena_dhs", "ok", erfolg, details="vorher ok",
    ))
    _run(speicher.setze_endpoint_health(
        "gardena_dhs", "schema_fehler", fehler_seit,
        details="property-name fehlt",
    ))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(),
        zonen=[zone], gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    assert stat["gesendet"] == 1
    text = benach.gesendet[0][1]
    assert "gardena_dhs" in text
    assert "schema_fehler" in text


def test_endpoint_health_unter_24h_kein_push(speicher):
    """T-0132 (H-8): Status frisch nicht-ok, aber letzter_erfolg < 24 h ->
    transient, kein Push."""
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    # Endpoint vor 6 h erfolgreich, jetzt schema_fehler -- kein Push (transient)
    _run(speicher.setze_endpoint_health(
        "gardena_dhs", "ok", jetzt - timedelta(hours=6),
    ))
    _run(speicher.setze_endpoint_health(
        "gardena_dhs", "schema_fehler", jetzt, details="seit 6h kaputt",
    ))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(),
        zonen=[zone], gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    assert stat["gesendet"] == 0


def test_endpoint_health_host_offline_kein_push(speicher):
    """T-0290: status 'host_offline' (DNS-/Offline-Probe auf mobilem Host)
    loest KEINEN Push aus, auch wenn letzter_erfolg > 24 h zurueck -- der
    Endpoint ist nicht kaputt, nur der Rechner war zur Probe-Zeit offline."""
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    _run(speicher.setze_endpoint_health(
        "fyta", "ok", jetzt - timedelta(days=3),
    ))
    _run(speicher.setze_endpoint_health(
        "fyta", "host_offline", jetzt - timedelta(days=1),
        details="[Errno 8] nodename nor servname provided, or not known",
    ))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(),
        zonen=[zone], gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    assert stat["gesendet"] == 0


def test_endpoint_health_nie_erfolg_kein_push(speicher):
    """T-0132 (H-8): Endpoint nie erfolgreich (Erstinstall) -> kein Push,
    weil 'seit X Tagen broken' nicht berechnen kann."""
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    _run(speicher.setze_endpoint_health(
        "fyta", "auth_fehler", jetzt, details="erster Versuch",
    ))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(),
        zonen=[zone], gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    assert stat["gesendet"] == 0


def test_intervall_throttle_zweiter_tick_skip(speicher):
    # intervall_minuten=30: zweiter Tick nach 5 min wird gar nicht ausgefuehrt.
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)
    job = WatchdogJob(
        speicher=speicher,
        benachrichtiger=benach,
        konfig=_baue_konfig(intervall_minuten=30),
        zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
    )
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt + timedelta(minutes=5)))
    assert stat == {"geprueft": 0, "gesendet": 0}


# --- Trigger D: PFLEGE_FAELLIG (T-0228 Stufe 2b) ----------------------------

def test_pflege_faelligkeit_loest_push_aus(speicher):
    """Faellige Pflege-Erinnerung -> ein iMessage-Push."""
    zone = _baue_zone("zitrus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 6, 6, 12, 0)
    # Erinnerung wurde gestern angelegt, faellig heute 12:00.
    _run(speicher.speichere_pflege_erinnerung(
        typ="beobachtung", faellig_am=jetzt,
        beschreibung="zitrus Substrat-Stau", zone_id="zitrus",
        jetzt=jetzt - timedelta(days=1),
    ))
    _run(_flute_sensor(speicher, "zitrus", jetzt))  # Husqvarna stumm
    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    assert stat["gesendet"] == 1
    assert "Pflege-Erinnerung faellig" in benach.gesendet[0][1]
    assert "zitrus" in benach.gesendet[0][1]


def test_pflege_zukuenftig_kein_push(speicher):
    """Erinnerung erst in 5 Tagen -> kein Push."""
    zone = _baue_zone("zitrus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 6, 6, 12, 0)
    _run(speicher.speichere_pflege_erinnerung(
        typ="kalibrierung", faellig_am=jetzt + timedelta(days=5),
        zone_id="zitrus", jetzt=jetzt,
    ))
    _run(_flute_sensor(speicher, "zitrus", jetzt))
    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    assert stat["gesendet"] == 0


def test_pflege_throttle_zweiter_push_skip(speicher):
    """Throttle pro Erinnerungs-ID: zweiter Tick innerhalb von
    `throttle_stunden` schickt keinen weiteren Push."""
    zone = _baue_zone("zitrus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 6, 6, 12, 0)
    spaeter = jetzt + timedelta(hours=2)
    _run(speicher.speichere_pflege_erinnerung(
        typ="t", faellig_am=jetzt, zone_id="zitrus", jetzt=jetzt,
    ))
    _run(_flute_sensor(speicher, "zitrus", jetzt))
    # Beim zweiten Tick muss eine frische Sensor-Messung da sein,
    # sonst feuert Husqvarna-Block-Trigger und macht den Assert kaputt.
    _run(_flute_sensor(speicher, "zitrus", spaeter))
    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(intervall_minuten=1), zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
    )
    s1 = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    # Push-Zaehler zwischen Ticks merken, weil Husqvarna-Trigger auch
    # schon beim ersten Tick gefeuert haben koennte (Schwelle wurde
    # hier nicht ueberschritten, aber sicher ist sicher).
    pushes_nach_t1 = len(benach.gesendet)
    s2 = _run(job.pruefe_und_sende_wenn_faellig(jetzt=spaeter))
    assert s1["gesendet"] >= 1
    # Pflege-Push darf nicht erneut auftauchen (Throttle 24h).
    pflege_pushes_t2 = [
        t for _, t in benach.gesendet[pushes_nach_t1:]
        if "Pflege-Erinnerung" in t
    ]
    assert pflege_pushes_t2 == [], pflege_pushes_t2


# --- Trigger E: UNBEKANNT_EVENT (T-0094) ------------------------------------

async def _schreibe_unbekannt_event(
    speicher: Speicher, zone_id: str, jetzt: datetime,
) -> int:
    """Hilfsfunktion: OEFFNEN-Event mit ausloeser=unbekannt."""
    from bewaesserung.modelle import (
        Ausloser, VentilAktion, VentilEreignis,
    )
    await speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=jetzt - timedelta(minutes=20),
        zone_id=zone_id, ventil_id="kanal-1",
        aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=0,
        ausloser=Ausloser.UNBEKANNT,
    ))


def test_unbekannt_event_loest_push_aus(speicher):
    """Offener UNBEKANNT-Event innerhalb 24h Fenster -> Push."""
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)

    _run(_flute_sensor(speicher, zone.zone_id, jetzt))
    _run(_schreibe_unbekannt_event(speicher, zone.zone_id, jetzt))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))

    assert stat["gesendet"] == 1
    text = benach.gesendet[0][1]
    assert "Sensor" in text
    assert "klassifizieren" in text or "gegossen" in text


def test_unbekannt_event_throttle_zweiter_skip(speicher):
    """Zweiter Push innerhalb von 24h Throttle wird unterdrueckt."""
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)
    spaeter = jetzt + timedelta(hours=2)

    _run(_flute_sensor(speicher, zone.zone_id, jetzt))
    _run(_flute_sensor(speicher, zone.zone_id, spaeter))
    _run(_schreibe_unbekannt_event(speicher, zone.zone_id, jetzt))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(intervall_minuten=1), zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
    )
    s1 = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    s2 = _run(job.pruefe_und_sende_wenn_faellig(jetzt=spaeter))
    assert s1["gesendet"] >= 1
    # Zweiter Tick: Pflege-Event-Push darf NICHT erneut feuern.
    unbekannt_pushes_t2 = [
        t for _, t in benach.gesendet[s1["gesendet"]:]
        if "Sensor" in t and "klassifizieren" in t
    ]
    assert unbekannt_pushes_t2 == []


def test_unbekannt_event_fyta_zone_kein_push(speicher):
    """FYTA-Zone (nicht in gardena_zone_ids) wird ausgeklammert.
    Die Beam-Cadence + Heuristik dort sind anders -- T-0094 ist auf
    Gardena-Sensor-Spuenge zugeschnitten."""
    zone = _baue_zone("zitrus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)

    _run(_flute_sensor(speicher, zone.zone_id, jetzt))
    _run(_schreibe_unbekannt_event(speicher, zone.zone_id, jetzt))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[],  # keine Gardena-Zonen -> Trigger E skip
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    unbekannt_pushes = [
        t for _, t in benach.gesendet
        if "klassifizieren" in t
    ]
    assert unbekannt_pushes == []


def test_unbekannt_event_klassifiziert_kein_push(speicher):
    """Nach Klassifikation (PATCH ausloser='manuell'/'ignoriert') ist
    der Event nicht mehr im Filter. UnbekanntEventBanner +
    T-0094-Watchdog teilen sich diese Logik."""
    from bewaesserung.modelle import (
        Ausloser, VentilAktion, VentilEreignis,
    )
    zone = _baue_zone("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 4, 18, 0)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))
    # Event jetzt schon als 'manuell' (User hat klassifiziert).
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=jetzt - timedelta(minutes=20),
        zone_id=zone.zone_id, ventil_id="kanal-1",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    unbekannt_pushes = [
        t for _, t in benach.gesendet if "klassifizieren" in t
    ]
    assert unbekannt_pushes == []


# ----- T-0256 Phase 1: Shadow-Empfehlungs-Push -----

from bewaesserung.watchdog import TYP_SHADOW_EMPFEHLUNG
from bewaesserung.modelle import BewaesserungsStrategie, BlockerTyp, GiessEmpfehlung


def _baue_zone_mit_plateau(zone_id: str = "bambus") -> ZonenKonfig:
    # name = zone_id, damit Push-Text-Asserts den Zonen-Namen direkt
    # pruefen koennen (Default-name "Test" waere mehrdeutig).
    z = _baue_zone(zone_id, name=zone_id)
    # Plateau-Modell-Parameter, damit _erwartete_wirkung_pp einen
    # endlichen Wert liefert (sonst 0.0).
    z.wirkung_max_pp = 20.0
    z.wirkungsrate_initial = 1.0
    z.ventil_kanal = 2
    z.ventil_geraet_id = "dswc1"
    return z


class _MotorStubT0256:
    """Liefert vordefinierte Empfehlungen pro Zone-Aufruf."""

    def __init__(self, empfehlungen: dict[str, GiessEmpfehlung]):
        self._empf = empfehlungen
        self.aufrufe: list[str] = []

    async def vorhersage_zone(self, zone_id: str) -> GiessEmpfehlung:
        self.aufrufe.append(zone_id)
        return self._empf[zone_id]


def _baue_empfehlung_gieße(zone_id: str, dauer_s: int = 480) -> GiessEmpfehlung:
    return GiessEmpfehlung(
        zone_id=zone_id,
        zeitstempel=datetime.now(),
        soll_bewaessern=True,
        blocker_typ=None,
        grund="6h-Prognose unter feuchte_min",
        feuchte_aktuell=55.0,
        welkepunkt_wert=40.0,
        optimum_min=60.0,
        optimum_max=75.0,
        prognose_quelle="heuristik",
        prognose_6h=51.0,
        prognose_12h=49.0,
        prognose_24h=47.0,
        tage_bis_welkepunkt=3.0,
        empfehlungs_typ="praeventiv",
        aktive_strategie=BewaesserungsStrategie.HAEUFIG_KLEIN.value,
        dauer_s_empfehlung=dauer_s,
        erklarung_kurz="6h-Prognose unter Schwelle",
        erklarung_lang="",
    )


def _baue_empfehlung_keinbedarf(zone_id: str) -> GiessEmpfehlung:
    return GiessEmpfehlung(
        zone_id=zone_id,
        zeitstempel=datetime.now(),
        soll_bewaessern=False,
        blocker_typ=BlockerTyp.ZEITFENSTER,
        grund="kein Bedarf",
        feuchte_aktuell=70.0,
        welkepunkt_wert=40.0,
        optimum_min=60.0,
        optimum_max=75.0,
        prognose_quelle="heuristik",
        prognose_6h=68.0,
        prognose_12h=66.0,
        prognose_24h=64.0,
        tage_bis_welkepunkt=5.0,
        empfehlungs_typ="kein_bedarf",
        aktive_strategie=BewaesserungsStrategie.HAEUFIG_KLEIN.value,
        dauer_s_empfehlung=0,
        erklarung_kurz="Feuchte ausreichend",
        erklarung_lang="",
    )


def test_t0256_soll_bewaessern_loest_shadow_push(speicher):
    """Trigger F feuert einen Shadow-Push, wenn der Motor 'gieße'
    empfiehlt UND kein vorheriger Push im min_pause-Fenster lag."""
    zone = _baue_zone_mit_plateau("bambus")
    motor = _MotorStubT0256({zone.zone_id: _baue_empfehlung_gieße(zone.zone_id, dauer_s=480)})
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 26, 9, 30)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
        motor=motor,
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))

    shadow_pushes = [t for _, t in benach.gesendet if "Shadow-Empfehlung" in t]
    assert len(shadow_pushes) == 1, f"erwartet 1 Push, gesendet: {benach.gesendet}"
    text = shadow_pushes[0]
    assert "bambus" in text.lower() or "Test" in text
    assert "8 min" in text  # 480s / 60 = 8 min
    assert "Aktuell: 55%" in text
    assert "Nach 6h:" in text and "mit" in text and "ohne" in text
    assert "Strategie:" in text
    # Pro Zone wurde Motor genau einmal pro Tick aufgerufen.
    assert motor.aufrufe == [zone.zone_id]
    # DB-Throttle-Eintrag liegt vor.
    letzter = _run(speicher.hole_letzten_watchdog_push(
        TYP_SHADOW_EMPFEHLUNG, zone.zone_id,
    ))
    assert letzter is not None


def test_t0256_keine_empfehlung_kein_push(speicher):
    """Wenn Motor 'kein Bedarf' empfiehlt: kein Push."""
    zone = _baue_zone_mit_plateau("bambus")
    motor = _MotorStubT0256({zone.zone_id: _baue_empfehlung_keinbedarf(zone.zone_id)})
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 26, 9, 30)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id], motor=motor,
    )
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))

    shadow_pushes = [t for _, t in benach.gesendet if "Shadow-Empfehlung" in t]
    assert shadow_pushes == []


def test_t0256_throttle_blockt_zweiten_push_in_min_pause(speicher):
    """Innerhalb von min_pause_minuten kein zweiter Push."""
    zone = _baue_zone_mit_plateau("bambus")  # min_pause_minuten=120
    motor = _MotorStubT0256({zone.zone_id: _baue_empfehlung_gieße(zone.zone_id)})
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 26, 9, 30)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id], motor=motor,
    )
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    # 60 min spaeter (innerhalb 120 min min_pause).
    job._letzte_aktualisierung = None  # Intervall-Gate aufheben
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt + timedelta(minutes=60)))

    shadow_pushes = [t for _, t in benach.gesendet if "Shadow-Empfehlung" in t]
    assert len(shadow_pushes) == 1, (
        "Zweiter Push innerhalb min_pause_minuten muss durch Throttle "
        "blockiert werden, war aber: " + repr(shadow_pushes)
    )


def test_t0256_throttle_lockert_nach_min_pause(speicher):
    """Nach min_pause_minuten + epsilon feuert ein zweiter Push."""
    zone = _baue_zone_mit_plateau("bambus")
    motor = _MotorStubT0256({zone.zone_id: _baue_empfehlung_gieße(zone.zone_id)})
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 26, 9, 30)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id], motor=motor,
    )
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    job._letzte_aktualisierung = None
    # 121 min spaeter -- ueber min_pause_minuten=120 hinaus.
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt + timedelta(minutes=121)))

    shadow_pushes = [t for _, t in benach.gesendet if "Shadow-Empfehlung" in t]
    assert len(shadow_pushes) == 2, (
        "Nach min_pause_minuten muss der Throttle freigeben. "
        "Gesendet: " + repr(shadow_pushes)
    )


def test_t0256_monitoring_zone_uebersprungen(speicher):
    """Zonen im MONITORING-Modus duerfen keinen Shadow-Push erzeugen."""
    zone = _baue_zone_mit_plateau("monitoring_zone")
    zone.modus = ZonenModus.MONITORING
    motor = _MotorStubT0256({zone.zone_id: _baue_empfehlung_gieße(zone.zone_id)})
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 26, 9, 30)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id], motor=motor,
    )
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))

    shadow_pushes = [t for _, t in benach.gesendet if "Shadow-Empfehlung" in t]
    assert shadow_pushes == []
    # Motor wurde fuer Monitoring-Zone gar nicht erst gerufen
    # (Performance + keine Empfehlungs-Berechnung fuer Monitoring).
    assert motor.aufrufe == []


def test_t0256_ohne_ventil_kanal_uebersprungen(speicher):
    """Zonen ohne ventil_kanal (Indoor/Topf-Setups) duerfen nicht
    von Trigger F angefasst werden."""
    zone = _baue_zone_mit_plateau("indoor")
    zone.ventil_kanal = None
    motor = _MotorStubT0256({zone.zone_id: _baue_empfehlung_gieße(zone.zone_id)})
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 26, 9, 30)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id], motor=motor,
    )
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))

    shadow_pushes = [t for _, t in benach.gesendet if "Shadow-Empfehlung" in t]
    assert shadow_pushes == []
    assert motor.aufrufe == []


def test_t0256_ohne_motor_no_op(speicher):
    """Watchdog ohne motor-Ref: Trigger F ist still No-Op, alte Tests
    + Konfig-Pfade ohne Motor-Wiring brechen nicht."""
    zone = _baue_zone_mit_plateau("bambus")
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 26, 9, 30)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id],
        # motor=None per Default
    )
    stat = _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))
    shadow_pushes = [t for _, t in benach.gesendet if "Shadow-Empfehlung" in t]
    assert shadow_pushes == []


def test_t0256_motor_fehler_kein_crash(speicher):
    """Wenn vorhersage_zone fuer eine Zone crasht, blockt das die
    anderen Zonen nicht."""
    zone_a = _baue_zone_mit_plateau("kaputt")
    zone_b = _baue_zone_mit_plateau("ok")

    class _CrashMotor:
        def __init__(self):
            self.aufrufe = []

        async def vorhersage_zone(self, zone_id: str):
            self.aufrufe.append(zone_id)
            if zone_id == "kaputt":
                raise RuntimeError("boom")
            return _baue_empfehlung_gieße(zone_id)

    motor = _CrashMotor()
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 26, 9, 30)
    _run(_flute_sensor(speicher, zone_a.zone_id, jetzt))
    _run(_flute_sensor(speicher, zone_b.zone_id, jetzt))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone_a, zone_b],
        gardena_zone_ids=[zone_a.zone_id, zone_b.zone_id],
        motor=motor,
    )
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))

    shadow_pushes = [t for _, t in benach.gesendet if "Shadow-Empfehlung" in t]
    assert len(shadow_pushes) == 1
    assert "ok" in shadow_pushes[0].lower()
    # Beide Zonen wurden versucht; Crash-Zone hat den Loop nicht gesprengt.
    assert "kaputt" in motor.aufrufe
    assert "ok" in motor.aufrufe


def _baue_empfehlung_pause_aktiv_blockiert(zone_id: str) -> GiessEmpfehlung:
    """T-0261-Regression: Empfehlung mit Pre-Pause-Bedarf (praeventiv)
    aber soll_bewaessern=False weil PAUSE_AKTIV greift. Genau das, was
    der Motor liefert, wenn der AuditJob im selben Master-Tick gerade
    eben einen entscheidung_log-Eintrag geschrieben hat."""
    return GiessEmpfehlung(
        zone_id=zone_id,
        zeitstempel=datetime.now(),
        soll_bewaessern=False,                       # blockiert durch Pause
        blocker_typ=BlockerTyp.PAUSE_AKTIV,
        grund="Min-Pause noch nicht eingehalten",
        feuchte_aktuell=55.0,
        welkepunkt_wert=40.0,
        optimum_min=60.0,
        optimum_max=75.0,
        prognose_quelle="heuristik",
        prognose_6h=None,                            # PAUSE-Branch hat keine Prognose
        prognose_12h=None,
        prognose_24h=None,
        tage_bis_welkepunkt=6.6,
        empfehlungs_typ="praeventiv",                # Bedarf ist real
        aktive_strategie=BewaesserungsStrategie.HAEUFIG_KLEIN.value,
        dauer_s_empfehlung=0,                        # Dauer in PAUSE-Branch nicht berechnet
        erklarung_kurz="Pause aktiv",
        erklarung_lang="",
    )


def test_t0261_race_fix_push_trotz_selbst_pause_beim_ersten_lauf(speicher):
    """T-0261 (2026-05-26): EmpfehlungsAuditJob laeuft VOR WatchdogJob
    im gleichen Master-Tick. AuditJob schreibt soll_bewaessern=1
    -> WatchdogJob sieht PAUSE_AKTIV durch selbstgesetzten Anker
    -> kein Push.

    Regression: Wenn (a) Motor liefert PAUSE_AKTIV mit empfehlungs_typ
    in {akut, praeventiv}, (b) wir noch nie fuer diese Zone gepusht
    haben (letzter_push=None), (c) ein Pre-Pause-empfehlungs_audit-
    Snapshot mit soll_bewaessern=1 + dauer>0 existiert, DANN feuert
    der Watchdog trotzdem einen Push mit den Audit-Snapshot-Werten
    und einem race_hinweis im Text.
    """
    zone = _baue_zone_mit_plateau("bambus_yogaraum")
    motor = _MotorStubT0256({
        zone.zone_id: _baue_empfehlung_pause_aktiv_blockiert(zone.zone_id),
    })
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 26, 11, 49)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    # Pre-Pause-Snapshot in empfehlungs_audit schreiben (so wie es der
    # AuditJob 5 Sekunden vor dem Watchdog-Tick getan hat).
    _run(speicher.setze_empfehlungs_audit(
        zeitstempel=jetzt - timedelta(seconds=5),
        zone_id=zone.zone_id,
        empfehlungs_typ="praeventiv",
        soll_bewaessern=True,
        blocker_typ=None,
        feuchte_aktuell=55.0,
        welkepunkt_wert=40.0,
        optimum_min=60.0,
        optimum_max=75.0,
        prognose_quelle="heuristik",
        prognose_6h=62.7,
        prognose_12h=60.0,
        prognose_24h=59.4,
        tage_bis_welkepunkt=6.6,
        dauer_s_empfehlung=2876,                      # 48 min
        aktive_strategie=BewaesserungsStrategie.HAEUFIG_KLEIN.value,
    ))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id], motor=motor,
    )
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))

    shadow_pushes = [t for _, t in benach.gesendet if "Shadow-Empfehlung" in t]
    assert len(shadow_pushes) == 1, (
        f"T-0261 Race-Fix: erwartet 1 Push trotz PAUSE_AKTIV "
        f"(letzter_push=None), gesendet: {benach.gesendet}"
    )
    text = shadow_pushes[0]
    assert "48 min" in text, f"Dauer aus Audit-Snapshot fehlt: {text}"
    assert "Aktuell: 55%" in text
    assert "race" in text.lower() or "selbst auf pause" in text.lower(), (
        f"Klartext-Hinweis 'erster Push trotz Selbst-Pause' fehlt: {text}"
    )


def test_t0261_kein_override_wenn_schon_gepusht(speicher):
    """T-0261 Sanity: wenn fuer diese Zone schon ein Watchdog-Push
    geloggt ist (letzter_push != None), greift der Race-Fix NICHT.
    Sonst wuerde das System nach jedem Restart noch einmal pushen,
    obwohl der User die Empfehlung schon einmal bekommen hat.
    """
    zone = _baue_zone_mit_plateau("bambus")
    motor = _MotorStubT0256({
        zone.zone_id: _baue_empfehlung_pause_aktiv_blockiert(zone.zone_id),
    })
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 26, 11, 49)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    # Pre-Pause-Snapshot vorhanden ...
    _run(speicher.setze_empfehlungs_audit(
        zeitstempel=jetzt - timedelta(seconds=5),
        zone_id=zone.zone_id,
        empfehlungs_typ="praeventiv", soll_bewaessern=True,
        blocker_typ=None, feuchte_aktuell=55.0, welkepunkt_wert=40.0,
        optimum_min=60.0, optimum_max=75.0,
        prognose_quelle="heuristik",
        prognose_6h=62.7, prognose_12h=60.0, prognose_24h=59.4,
        tage_bis_welkepunkt=6.6, dauer_s_empfehlung=2876,
        aktive_strategie=BewaesserungsStrategie.HAEUFIG_KLEIN.value,
    ))
    # ... aber wir haben schon vor 30 min gepusht.
    _run(speicher.setze_watchdog_push(
        TYP_SHADOW_EMPFEHLUNG, zone.zone_id, jetzt - timedelta(minutes=30),
    ))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id], motor=motor,
    )
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))

    shadow_pushes = [t for _, t in benach.gesendet if "Shadow-Empfehlung" in t]
    assert shadow_pushes == [], (
        "T-0261: bei vorhandenem letzter_push darf der Race-Fix NICHT "
        "greifen -- sonst kommen Doppel-Pushes nach Restart. Gesendet: "
        + repr(shadow_pushes)
    )


def test_t0261_realfall_snapshot_mit_pause_blocker_aber_dauer(speicher):
    """T-0261 Realfall 26.05.: nach Pause-Anker schreibt der AuditJob
    Snapshots mit `soll_bewaessern=0 + blocker=PAUSE_AKTIV`, aber die
    `dauer_s_empfehlung` ist trotzdem die echte (z.B. 2876s/48min).
    Der Watchdog-Fallback muss diese Snapshots erkennen -- nicht nur
    die selten-vorkommenden Pre-Pause-Snapshots mit soll=1.

    Test: NUR einen Snapshot mit `soll=0 + PAUSE_AKTIV + dauer=2876`
    vorhanden -> Watchdog muss trotzdem pushen.
    """
    zone = _baue_zone_mit_plateau("bambus_yogaraum")
    motor = _MotorStubT0256({
        zone.zone_id: _baue_empfehlung_pause_aktiv_blockiert(zone.zone_id),
    })
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 26, 12, 10)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    # Snapshot des Realfalls: AuditJob hat NACH Pause-Anker einen
    # PAUSE_AKTIV-Eintrag geschrieben, aber dauer_s_empfehlung steckt
    # drin (Backend setzt das auch im Block-Branch).
    _run(speicher.setze_empfehlungs_audit(
        zeitstempel=jetzt - timedelta(minutes=20),
        zone_id=zone.zone_id,
        empfehlungs_typ="praeventiv",
        soll_bewaessern=False,                       # <-- block-Pfad
        blocker_typ="PAUSE_AKTIV",
        feuchte_aktuell=55.0,
        welkepunkt_wert=40.0,
        optimum_min=60.0, optimum_max=75.0,
        prognose_quelle="heuristik",
        prognose_6h=62.7, prognose_12h=60.0, prognose_24h=59.4,
        tage_bis_welkepunkt=6.6,
        dauer_s_empfehlung=2876,                      # echte Dauer
        aktive_strategie=BewaesserungsStrategie.HAEUFIG_KLEIN.value,
    ))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id], motor=motor,
    )
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))

    shadow_pushes = [t for _, t in benach.gesendet if "Shadow-Empfehlung" in t]
    assert len(shadow_pushes) == 1, (
        f"T-0261 Realfall: PAUSE_AKTIV-Snapshot mit Dauer muss "
        f"reichen fuer Push, gesendet: {benach.gesendet}"
    )
    assert "48 min" in shadow_pushes[0]


def test_t0263_ml_vergleich_im_push_mit_drift_ampel(speicher):
    """T-0263 (2026-05-26): Shadow-Push enthaelt ML-Dauer + Drift-Ampel
    als Diagnose-Zeile. Setup: Heuristik 48 min, ML 27 min, MAE
    Heuristik 13.4 / ML 19.0 -> Ampel rot.
    """
    from datetime import timedelta as _td

    zone = _baue_zone_mit_plateau("yogaraum")
    motor = _MotorStubT0256({zone.zone_id: _baue_empfehlung_gieße(zone.zone_id, dauer_s=2880)})
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 26, 9, 30)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    # ML-Dauer-Vorschlag einkippen (juengster Eintrag wird vom Push gelesen).
    _run(speicher.speichere_dauer_vorschlag(
        zeitstempel=jetzt - _td(minutes=5),
        zone_id=zone.zone_id,
        f_vor=55.0, ziel_schwelle=60.0,
        heuristik_s=2880, ml_s=1627,
        ml_modell_version="v20260423_180538",
        features_json="{}", modus="shadow",
    ))

    # Drift-Metriken-Eintraege (mehrere) damit hole_dauer_drift_metriken
    # nicht-None mae_heuristik + mae_ml liefert. Wir brauchen mind. 1
    # bewerteten Eintrag, damit AVG(ABS(...)) feuert.
    for i in range(3):
        _run(speicher.speichere_dauer_vorschlag(
            zeitstempel=jetzt - _td(days=i+1),
            zone_id=zone.zone_id,
            f_vor=55.0, ziel_schwelle=60.0,
            heuristik_s=2400, ml_s=1500,
            ml_modell_version="v20260423_180538",
            features_json="{}", modus="shadow",
        ))
    # Bewertung schreiben fuer alle 3 vergangenen Eintraege:
    # heuristik_fehler 13.4, ml_fehler 19.0 -> ml > 0.8 * h -> rot.
    rohe_eintraege = _run(speicher.hole_dauer_vorschlaege_unbewertet(
        bis_zeitstempel=jetzt, limit=50,
    ))
    # hole_dauer_vorschlaege_unbewertet liefert schon nur Eintraege
    # die mindestens 6 h alt sind, also alle markierbar.
    bewertbar = rohe_eintraege
    for e in bewertbar:
        _run(speicher.markiere_dauer_vorschlag_bewertet(
            row_id=e["id"], bewertet_am=jetzt,
            ist_delta_6h=5.0,
            heuristik_prognose_delta=18.4,
            ml_prognose_delta=24.0,
            heuristik_fehler=13.4,
            ml_fehler=19.0,
        ))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id], motor=motor,
    )
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))

    shadow_pushes = [t for _, t in benach.gesendet if "Shadow-Empfehlung" in t]
    assert len(shadow_pushes) == 1, f"erwartet 1 Push: {benach.gesendet}"
    text = shadow_pushes[0]
    assert "ML-Vergleich:" in text, f"ML-Vergleichs-Zeile fehlt: {text}"
    assert "27 min" in text, f"ML-Dauer fehlt: {text}"
    assert "rot" in text.lower(), f"Drift-Ampel rot fehlt: {text}"


def test_t0261_kein_override_bei_kein_bedarf_pause(speicher):
    """T-0261 Sanity: PAUSE_AKTIV bei empfehlungs_typ=kein_bedarf
    triggert KEINEN Override. Nur akut/praeventiv qualifiziert.
    """
    zone = _baue_zone_mit_plateau("bambus")
    motor = _MotorStubT0256({
        zone.zone_id: GiessEmpfehlung(
            zone_id=zone.zone_id,
            zeitstempel=datetime.now(),
            soll_bewaessern=False, blocker_typ=BlockerTyp.PAUSE_AKTIV,
            grund="kein bedarf + pause", feuchte_aktuell=70.0,
            welkepunkt_wert=40.0, optimum_min=60.0, optimum_max=75.0,
            prognose_quelle="heuristik",
            prognose_6h=68.0, prognose_12h=66.0, prognose_24h=64.0,
            tage_bis_welkepunkt=5.0,
            empfehlungs_typ="kein_bedarf",
            aktive_strategie=BewaesserungsStrategie.HAEUFIG_KLEIN.value,
            dauer_s_empfehlung=0,
            erklarung_kurz="", erklarung_lang="",
        ),
    })
    benach = _BenachrichtigerStub()
    jetzt = datetime(2026, 5, 26, 11, 49)
    _run(_flute_sensor(speicher, zone.zone_id, jetzt))

    job = WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=_baue_konfig(), zonen=[zone],
        gardena_zone_ids=[zone.zone_id], motor=motor,
    )
    _run(job.pruefe_und_sende_wenn_faellig(jetzt=jetzt))

    shadow_pushes = [t for _, t in benach.gesendet if "Shadow-Empfehlung" in t]
    assert shadow_pushes == [], (
        "T-0261: kein_bedarf+PAUSE_AKTIV darf KEINEN Push triggern. "
        "Gesendet: " + repr(shadow_pushes)
    )
