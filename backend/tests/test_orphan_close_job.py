"""T-0210: OrphanCloseJob — synthetisches SCHLIESSEN fuer verpasste Events.

Realfall 18.05. 09:38: Bambus-Bewaesserung 09:08-09:38 (30 min, manuell
via Gardena-App). Backend-Restart 09:22:18 (PID 80713), WebSocket-
Reconnect verpasste das SCHLIESSEN-Event. Folge: OEFFNEN ohne Pendant
in der DB, VentilSicherung._aktiv haengt, Bilanz verzerrt.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import (
    Ausloser,
    VentilAktion,
    VentilEreignis,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.orphan_close_job import OrphanCloseJob
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


def _zone(zone_id: str = "bambuswald", max_dauer_s: int = 1800) -> ZonenKonfig:
    return ZonenKonfig(
        zone_id=zone_id,
        name=zone_id,
        modus=ZonenModus.MONITORING,
        feuchte_schwelle_min=30, feuchte_schwelle_max=70,
        max_dauer_sekunden=max_dauer_s,
    )


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "orphan.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def test_t0210_orphan_oeffnen_bekommt_synthetisches_schliessen(speicher):
    """Realfall: OEFFNEN ohne SCHLIESSEN > max_dauer + grace -> Job
    schreibt synthetisches SCHLIESSEN.
    """
    oeffnen_zeit = datetime(2026, 5, 18, 9, 8)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit,
        zone_id="bambuswald", ventil_id="11111111-uuid:2",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    job = OrphanCloseJob(speicher, [_zone()])
    # jetzt = oeffnen + 3h (max_dauer 30min + grace 30min = 1h, also
    # 3h ist klar im orphan-Bereich)
    jetzt = oeffnen_zeit + timedelta(hours=3)
    n = _run(job.aktualisiere(jetzt=jetzt))
    assert n == 1, "Erwartet 1 synthetisches SCHLIESSEN"

    # SCHLIESSEN-Event in DB?
    ereignisse = _run(speicher.hole_ventil_ereignisse(
        "bambuswald",
        von=oeffnen_zeit, bis=jetzt + timedelta(hours=1),
    ))
    schliessen = [e for e in ereignisse if e.aktion == VentilAktion.SCHLIESSEN]
    assert len(schliessen) == 1
    s = schliessen[0]
    assert s.ausloser == Ausloser.WATCHDOG, "Synthetisches Close mit WATCHDOG markieren"
    assert s.dauer_sekunden == 1800, "Dauer = max_dauer aus Konfig"
    # SCHLIESSEN-Zeitstempel = OEFFNEN + max_dauer
    assert s.zeitstempel == oeffnen_zeit + timedelta(seconds=1800)


def test_f1_manuell_giesskannen_log_bekommt_kein_phantom_schliessen(speicher):
    """F1/T-0301: Ein /api/giessen-Log (ventil_id='manuell', kein Gardena-
    Ventil-Lauf) darf NICHT als Orphan gelten -> kein synthetisches
    max_dauer-SCHLIESSEN, das ML-Features/Budget/Bilanz vergiftet."""
    oeffnen_zeit = datetime(2026, 5, 10, 12, 27)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit,
        zone_id="bambuswald", ventil_id="manuell",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=30,
        ausloser=Ausloser.MANUELL,
    )))
    job = OrphanCloseJob(speicher, [_zone()])
    jetzt = oeffnen_zeit + timedelta(hours=3)
    n = _run(job.aktualisiere(jetzt=jetzt))
    assert n == 0, "Giesskannen-Log darf kein Phantom-SCHLIESSEN erzeugen"

    ereignisse = _run(speicher.hole_ventil_ereignisse(
        "bambuswald", von=oeffnen_zeit, bis=jetzt + timedelta(hours=1),
    ))
    schliessen = [e for e in ereignisse if e.aktion == VentilAktion.SCHLIESSEN]
    assert schliessen == [], "kein synthetisches SCHLIESSEN fuer manuell-Log"


def test_f21_ignoriert_geflipptes_oeffnen_wird_nicht_resurrected(speicher):
    """F21: Ein vom User auf `ignoriert` geflipptes OEFFNEN darf der
    OrphanCloseJob nicht wieder mit einem Phantom-Paar beleben."""
    oeffnen_zeit = datetime(2026, 5, 18, 9, 8)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit,
        zone_id="bambuswald", ventil_id="11111111-uuid:2",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.IGNORIERT,
    )))
    job = OrphanCloseJob(speicher, [_zone()])
    n = _run(job.aktualisiere(jetzt=oeffnen_zeit + timedelta(hours=3)))
    assert n == 0, "ignoriert-OEFFNEN darf nicht resurrected werden"


def test_t0210_orphan_close_idempotent(speicher):
    """Zweiter Aufruf schreibt KEIN weiteres SCHLIESSEN, weil das
    Pendant jetzt existiert."""
    oeffnen_zeit = datetime(2026, 5, 18, 9, 8)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit,
        zone_id="bambuswald", ventil_id="11111111-uuid:2",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    job = OrphanCloseJob(speicher, [_zone()])
    jetzt = oeffnen_zeit + timedelta(hours=3)
    n1 = _run(job.aktualisiere(jetzt=jetzt))
    n2 = _run(job.aktualisiere(jetzt=jetzt))
    assert n1 == 1 and n2 == 0, "Zweiter Lauf: keine Doppel-Erzeugung"


def test_t0210_aktiver_lauf_bleibt_in_ruhe(speicher):
    """OEFFNEN das noch innerhalb max_dauer + grace liegt darf NICHT
    geschlossen werden -- waere ein Eingriff in den laufenden Lauf.
    """
    oeffnen_zeit = datetime(2026, 5, 18, 9, 8)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit,
        zone_id="bambuswald", ventil_id="11111111-uuid:2",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    job = OrphanCloseJob(speicher, [_zone(max_dauer_s=1800)])
    # jetzt = oeffnen + 25 min: max_dauer 30 min nicht erreicht
    jetzt = oeffnen_zeit + timedelta(minutes=25)
    n = _run(job.aktualisiere(jetzt=jetzt))
    assert n == 0, "Aktive Bewaesserung darf nicht abgewuergt werden"


def test_t0210_kein_eingriff_wenn_schliessen_existiert(speicher):
    """OEFFNEN + SCHLIESSEN-Paar -> Job tut nichts."""
    oeffnen_zeit = datetime(2026, 5, 18, 9, 8)
    schliessen_zeit = oeffnen_zeit + timedelta(minutes=20)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit,
        zone_id="bambuswald", ventil_id="11111111-uuid:2",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=schliessen_zeit,
        zone_id="bambuswald", ventil_id="11111111-uuid:2",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1200,
        ausloser=Ausloser.MANUELL,
    )))
    job = OrphanCloseJob(speicher, [_zone()])
    jetzt = oeffnen_zeit + timedelta(hours=3)
    n = _run(job.aktualisiere(jetzt=jetzt))
    assert n == 0, "Existierendes SCHLIESSEN-Paar nicht doppelt"


def test_t0210_heuristik_orphan_wird_nicht_geschlossen(speicher):
    """sensor_heuristik-Events schreiben ihre OEFFNEN+SCHLIESSEN
    atomar. Falls trotzdem mal nur OEFFNEN da steht (DB-Fehler),
    soll der Orphan-Job sie NICHT anfassen -- das ist ein anderer
    Bug-Bereich (T-0114). Sonst kaskadiert die Heuristik in
    synthetische Watchdog-Events.
    """
    oeffnen_zeit = datetime(2026, 5, 18, 9, 8)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit,
        zone_id="bambuswald", ventil_id="sensor_heuristik",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.UNBEKANNT,
    )))
    job = OrphanCloseJob(speicher, [_zone()])
    jetzt = oeffnen_zeit + timedelta(hours=3)
    n = _run(job.aktualisiere(jetzt=jetzt))
    assert n == 0, "Heuristik-Events sind nicht der T-0210-Fall"


def test_t0210_sync_aus_live_state_schliesst_diskrepanz(speicher):
    """Reconnect-Sync: Gardena sagt 'zu', DB hat OEFFNEN ohne SCHLIESSEN
    -> synthetisches SCHLIESSEN mit `jetzt` als Zeitstempel und
    Dauer gecappt auf die geplante max_dauer. Realfall 18.05. 09:38.
    """
    oeffnen_zeit = datetime(2026, 5, 18, 9, 8)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit,
        zone_id="bambuswald", ventil_id="11111111-uuid:2",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    job = OrphanCloseJob(speicher, [_zone()])
    # Reconnect bei 10:00 -- 52 min nach OEFFNEN. Cloud sagt "zu".
    jetzt = datetime(2026, 5, 18, 10, 0)
    n = _run(job.sync_aus_live_state(
        live_state={"bambuswald": "zu"}, jetzt=jetzt,
    ))
    assert n == 1

    ereignisse = _run(speicher.hole_ventil_ereignisse(
        "bambuswald", von=oeffnen_zeit, bis=jetzt + timedelta(hours=1),
    ))
    schliessen = [e for e in ereignisse if e.aktion == VentilAktion.SCHLIESSEN]
    assert len(schliessen) == 1
    s = schliessen[0]
    assert s.ausloser == Ausloser.WATCHDOG
    # T-0367: Dauer wird auf zone.max_dauer_sekunden gecappt, nicht auf die
    # komplette Offline-Zeit.
    assert s.dauer_sekunden == 1800
    assert s.zeitstempel == jetzt


def test_t0210_sync_aus_live_state_kein_eingriff_wenn_live_offen(speicher):
    """Gardena sagt 'offen' -> Job laesst es in Ruhe (laufende
    Bewaesserung). Kein synthetisches SCHLIESSEN.
    """
    oeffnen_zeit = datetime(2026, 5, 18, 9, 8)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit,
        zone_id="bambuswald", ventil_id="11111111-uuid:2",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    job = OrphanCloseJob(speicher, [_zone()])
    jetzt = datetime(2026, 5, 18, 10, 0)
    n = _run(job.sync_aus_live_state(
        live_state={"bambuswald": "offen"}, jetzt=jetzt,
    ))
    assert n == 0, "Live=offen -> nicht eingreifen"


def test_t0210_sync_aus_live_state_unbekannt_ist_safe(speicher):
    """Status 'unbekannt' -> kein Eingriff (lieber nichts tun als
    falsch schliessen).
    """
    oeffnen_zeit = datetime(2026, 5, 18, 9, 8)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit,
        zone_id="bambuswald", ventil_id="11111111-uuid:2",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    job = OrphanCloseJob(speicher, [_zone()])
    n = _run(job.sync_aus_live_state(
        live_state={"bambuswald": "unbekannt"},
        jetzt=datetime(2026, 5, 18, 10, 0),
    ))
    assert n == 0


def test_t0210_sync_idempotent(speicher):
    """Zweiter Reconnect-Sync findet das jetzt vorhandene SCHLIESSEN
    und tut nichts mehr."""
    oeffnen_zeit = datetime(2026, 5, 18, 9, 8)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit,
        zone_id="bambuswald", ventil_id="11111111-uuid:2",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    job = OrphanCloseJob(speicher, [_zone()])
    jetzt = datetime(2026, 5, 18, 10, 0)
    n1 = _run(job.sync_aus_live_state(
        live_state={"bambuswald": "zu"}, jetzt=jetzt,
    ))
    n2 = _run(job.sync_aus_live_state(
        live_state={"bambuswald": "zu"}, jetzt=jetzt + timedelta(minutes=1),
    ))
    assert n1 == 1 and n2 == 0


@pytest.mark.parametrize("abstand_h", [0.87, 5, 12])
def test_t0408_iso_sync_idempotent_auch_jenseits_des_suchfensters(
    speicher, abstand_h,
):
    """T-0408-Isomorphie: der Reconnect-Sync muss idempotent bleiben, AUCH
    wenn das OEFFNEN laenger zurueckliegt als das 4-h-Pendant-Suchfenster.

    Bug (gefunden 20.07. beim Isomorphie-Check zu T-0408): der synthetische
    Close wird bei `jetzt` geschrieben, das Pendant aber nur in
    (oeffnen, oeffnen+4h] gesucht. Bei Backend-Downtime > 4 h fiel die
    eigene Schreibung aus dem Fenster -> jeder weitere Reconnect legte
    einen Close nach. Gemessen: 3 Reconnects -> 3 Closes bei 5 h Abstand,
    korrekt 1 Close bei 52 min. Strukturell derselbe Fehler wie der
    DHS-Duplikat-Sturm (Fenster am falschen Punkt verankert).

    `abstand_h=0.87` (52 min) ist der frueher schon abgedeckte Fall und
    muss weiter gruen sein -- er beweist, dass der Guard den legitimen
    Erst-Close nicht blockiert.
    """
    oeffnen_zeit = datetime(2026, 5, 18, 9, 8)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit,
        zone_id="bambuswald", ventil_id="11111111-uuid:2",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    job = OrphanCloseJob(speicher, [_zone()])
    jetzt = oeffnen_zeit + timedelta(hours=abstand_h)
    rueckgaben = [
        _run(job.sync_aus_live_state(
            live_state={"bambuswald": "zu"},
            jetzt=jetzt + timedelta(minutes=i),
        ))
        for i in range(3)
    ]
    assert rueckgaben == [1, 0, 0], (
        f"Abstand {abstand_h}h: nur der erste Sync darf schliessen, "
        f"bekam {rueckgaben}"
    )
    ereignisse = _run(speicher.hole_ventil_ereignisse(
        "bambuswald",
        von=oeffnen_zeit - timedelta(hours=1),
        bis=jetzt + timedelta(hours=2),
    ))
    closes = [e for e in ereignisse if e.aktion == VentilAktion.SCHLIESSEN]
    assert len(closes) == 1, f"Duplikat-Sturm: {len(closes)} Closes"


def test_t0210_aktualisiere_wenn_faellig_throttled(speicher):
    """Erster Aufruf scant; zweiter Aufruf direkt danach scant NICHT,
    weil das Intervall (10 min default) nicht erreicht ist."""
    oeffnen_zeit = datetime(2026, 5, 18, 9, 8)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit,
        zone_id="bambuswald", ventil_id="11111111-uuid:2",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    job = OrphanCloseJob(speicher, [_zone()])
    jetzt = oeffnen_zeit + timedelta(hours=3)
    n1 = _run(job.aktualisiere_wenn_faellig(jetzt=jetzt))
    n2 = _run(job.aktualisiere_wenn_faellig(jetzt=jetzt + timedelta(minutes=1)))
    assert n1 == 1
    assert n2 == 0, "Throttle: scant nicht im 1-min-Folgeaufruf"


# --- T-0559: Gegenrichtung -- Cloud offen, DB kennt den Lauf nicht ---

def _zone_mit_kanal(
    zone_id: str = "bambuswald",
    geraet: str = "bbbb0001-uuid",
    kanal: int = 2,
) -> ZonenKonfig:
    """Zone MIT physischem Kanal -- nur die kann einen Hahn belegen."""
    return ZonenKonfig(
        zone_id=zone_id, name=zone_id, modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=30, feuchte_schwelle_max=70,
        max_dauer_sekunden=1800,
        ventil_geraet_id=geraet, ventil_kanal=kanal,
    )


def test_t0559_live_offen_ohne_db_lauf_schreibt_synthetisches_oeffnen(speicher):
    """Der Ablauf aus T-0554: Handguss startet, waehrend die WS weg ist.

    Es entsteht kein OEFFNEN-Event. Nach dem Reconnect meldet die Cloud
    "offen", die DB kennt nichts -- und der HahnArbiter haelt den Kanal
    fuer frei, weil alle seine drei Quellen leer sind. Die Automatik
    oeffnet dann ueber den laufenden Guss.
    """
    job = OrphanCloseJob(speicher, [_zone_mit_kanal()])
    jetzt = datetime(2026, 8, 29, 14, 0)

    n = _run(job.sync_aus_live_state(
        live_state={"bambuswald": "offen"}, jetzt=jetzt,
    ))

    assert n == 1, "Der unbekannte Fremdlauf muss sichtbar werden"
    offen = _run(speicher.hole_offene_ventil_kanaele(
        seit=jetzt - timedelta(hours=1), bis=jetzt + timedelta(minutes=1),
    ))
    assert [z["ventil_id"] for z in offen] == ["bbbb0001-uuid:2"], (
        "der Kanal muss jetzt in genau der Sicht stehen, die der Arbiter liest"
    )


def test_t0559_bekannter_lauf_wird_nicht_verdoppelt(speicher):
    """Gegenprobe: kennt die DB den Lauf schon, passiert nichts.

    Ohne diesen Fall wuerde jeder Reconnect waehrend eines regulaeren
    eigenen Laufs ein zweites OEFFNEN schreiben -- der Kanal saehe dann
    doppelt belegt aus und die Paarung liefe auseinander.
    """
    jetzt = datetime(2026, 8, 29, 14, 0)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=jetzt - timedelta(minutes=20),
        zone_id="bambuswald", ventil_id="bbbb0001-uuid:2",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.AUTOMATIK,
    )))
    job = OrphanCloseJob(speicher, [_zone_mit_kanal()])

    n = _run(job.sync_aus_live_state(
        live_state={"bambuswald": "offen"}, jetzt=jetzt,
    ))

    assert n == 0
    offen = _run(speicher.hole_offene_ventil_kanaele(
        seit=jetzt - timedelta(hours=1), bis=jetzt + timedelta(minutes=1),
    ))
    assert len(offen) == 1, "kein zweites OEFFNEN auf demselben Kanal"


def test_t0559_zone_ohne_kanal_bleibt_unberuehrt(speicher):
    """Eine Zone ohne `ventil_kanal` hat keinen Hahn, den sie sperren
    koennte -- fuer sie darf kein Ventil-Event entstehen.
    """
    job = OrphanCloseJob(speicher, [_zone("magerwiese")])

    n = _run(job.sync_aus_live_state(
        live_state={"magerwiese": "offen"},
        jetzt=datetime(2026, 8, 29, 14, 0),
    ))

    assert n == 0
    assert _run(speicher.hole_ventil_ereignisse("magerwiese")) == []
