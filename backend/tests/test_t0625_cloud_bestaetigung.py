"""T-0625: Cloud meldet "offen" fuer einen bekannten Lauf -> Bestaetigung statt zweitem OEFFNEN.

Ablauf (Gate W7, zur Laufzeit belegt): App-Lauf auf dem exklusiven
magerwiese-Kanal, OEFFNEN bei T0, kein SCHLIESSEN, das Backend schlief. Beim
Reconnect r Minuten spaeter meldet die Cloud "offen".
- Bis T-0625 hielt der Sync den Lauf im 4-h-Fenster fuer bekannt und schrieb
  nichts: Arbiter und kanal_zustand (MAX_OFFEN 150 min) sahen den Kanal ab
  T0 + 150 frei, waehrend Wasser lief.
- Ein zweites OEFFNEN (so lief es schon, wenn der Orphan-Scan vor dem Sync
  lief) zerlegte den Lauf: der Scan schaetzte fuer beide Haelften ein
  SCHLIESSEN, die Bilanz zaehlte 5400 s doppelt.
Jetzt bekommt das bekannte OEFFNEN eine Cloud-Bestaetigung; die Ereignis-Sicht
haelt den Kanal bis r + MAX_OFFEN oder bis zu einem echten SCHLIESSEN.

Echter Speicher (tmp-DB), echter Orphan-Job, echter Arbiter, echter
WS-Schreibpfad (`main.speichere_externes_ventil_ereignis`); keine Hardware,
keine Netzaufrufe.
"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from bewaesserung.hahn_arbiter import HahnArbiter, Kanalprofil
from bewaesserung.kanal_zustand import kanal_vorgang_laeuft
from bewaesserung.main import speichere_externes_ventil_ereignis
from bewaesserung.modelle import (
    Ausloser,
    LaufAusloser,
    VentilAktion,
    VentilEreignis,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.orphan_close_job import OrphanCloseJob
from bewaesserung.speicher import (
    KORREKTUR_FELD_CLOUD_OFFEN,
    Speicher,
    letzte_cloud_offen_bestaetigung,
    letzte_cloud_zu_bestaetigung,
    letzte_herkunft,
)

DSWC1 = "bbbb0001-0000-4000-8000-000000000001"
DSWC2 = "bbbb0002-0000-4000-8000-000000000002"
MAGERWIESE = f"{DSWC2}:1"
CLUSTER = "garten-haupthahn"
PROFILE = {
    (DSWC1, 1): Kanalprofil(("waldblumenhain",), CLUSTER, 6.0, True),
    (DSWC2, 1): Kanalprofil(("magerwiese",), CLUSTER, 6.0, True),
}
T0 = datetime(2026, 10, 13, 9, 0)
WS_CLOSE_NACH = timedelta(minutes=20)   # echter Close 20 min nach dem Reconnect


class _OhneEngineLauf:
    def aktive_kanaele(self) -> dict[int, tuple[str, ...]]:
        return {}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "t0625.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _job(speicher) -> OrphanCloseJob:
    return OrphanCloseJob(speicher, [ZonenKonfig(
        zone_id="magerwiese", name="magerwiese", modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=30, feuchte_schwelle_max=70,
        max_dauer_sekunden=5400,
        ventil_geraet_id=DSWC2, ventil_kanal=1,
    )])


def _oeffnen(speicher, zeit: datetime, ausloser=Ausloser.AUTOMATIK) -> None:
    # AUTOMATIK, damit eine Umklassierung durch ein fremdes OEFFNEN (MANUELL)
    # im LaufAusloser sichtbar wuerde.
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=zeit, zone_id="magerwiese", ventil_id=MAGERWIESE,
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0, ausloser=ausloser,
    )))


def _darf_waldblumen(speicher, jetzt: datetime) -> bool:
    arbiter = HahnArbiter(
        speicher=speicher, kanalprofile=PROFILE,
        cluster_max_lpm={CLUSTER: 10.0},
    )
    arbiter.registriere_sicherung(DSWC1, _OhneEngineLauf())
    arbiter.registriere_sicherung(DSWC2, _OhneEngineLauf())
    return _run(arbiter.pruefe(DSWC1, 1, jetzt=jetzt)).erlaubt


def _belegt(speicher, jetzt: datetime) -> bool:
    """Beide Sichten muessen dasselbe sagen: Arbiter und kanal_zustand."""
    arbiter_belegt = not _darf_waldblumen(speicher, jetzt)
    zustand_belegt = _run(kanal_vorgang_laeuft(
        speicher, zone_id="magerwiese", kanal=1, geraet_id=DSWC2, jetzt=jetzt,
    ))
    assert arbiter_belegt == zustand_belegt, (jetzt, arbiter_belegt, zustand_belegt)
    return arbiter_belegt


def _ereignisse(speicher) -> list[VentilEreignis]:
    return _run(speicher.hole_ventil_ereignisse(
        "magerwiese", von=T0 - timedelta(hours=1), bis=T0 + timedelta(hours=20),
    ))


def _scans(job, von: datetime, bis: datetime) -> None:
    t = von
    while t < bis:
        _run(job.aktualisiere(jetzt=t))
        t += timedelta(minutes=5)


def _reconnect(speicher, job, r_min: int, reihenfolge: str) -> datetime:
    """Reconnect bei T0 + r mit Cloud "offen"; Scan davor oder danach."""
    jr = T0 + timedelta(minutes=r_min)
    if reihenfolge == "scan_zuerst":
        _run(job.aktualisiere(jetzt=jr))
    n = _run(job.sync_aus_live_state(
        live_state={"magerwiese": "offen"}, jetzt=jr,
    ))
    assert n == 0, "bekannter Lauf: kein synthetisches Ereignis"
    return jr


R_WERTE = [60, 130, 160, 200, 240, 300]
REIHENFOLGEN = ["sync_zuerst", "scan_zuerst"]


@pytest.mark.parametrize("reihenfolge", REIHENFOLGEN)
@pytest.mark.parametrize("r_min", R_WERTE)
def test_cloud_offen_haelt_kanal_bis_meldung_plus_max_offen(
    speicher, r_min, reihenfolge,
):
    _oeffnen(speicher, T0)
    job = _job(speicher)
    jr = _reconnect(speicher, job, r_min, reihenfolge)
    _scans(job, jr + timedelta(minutes=5), jr + timedelta(hours=4))

    assert _belegt(speicher, jr + timedelta(minutes=1)) is True
    assert _belegt(speicher, jr + timedelta(minutes=149)) is True, (
        "geschaetzte SCHLIESSEN der Scans duerfen die Meldung nicht aufheben"
    )
    assert _belegt(speicher, jr + timedelta(minutes=151)) is False, (
        "die Bestaetigung ist begrenzt: MAX_OFFEN ab der Meldung"
    )
    oeffnen = [e for e in _ereignisse(speicher) if e.aktion == VentilAktion.OEFFNEN]
    assert [e.zeitstempel for e in oeffnen] == [T0], "kein zweites OEFFNEN"


def _erwartete_summe(r_min: int, ws_close: str | None) -> int:
    """Werte der Basis in der Reihenfolge "Sync zuerst" (dort ohne Doppelzaehlung)."""
    if ws_close is None:
        return 5400                     # eine Schaetzung (max_dauer)
    if ws_close == "ohne_dauer":
        # Close bei r+20 ohne Dauer: liegt er vor dem ersten Orphan-Cutoff
        # (T0 + 120), paart er o1 und es gibt keine Schaetzung.
        return 0 if r_min + 20 < 120 else 5400
    return int((timedelta(minutes=r_min) + WS_CLOSE_NACH).total_seconds())


@pytest.mark.parametrize("ws_close", [None, "ohne_dauer", "mit_dauer"])
@pytest.mark.parametrize("reihenfolge", REIHENFOLGEN)
@pytest.mark.parametrize("r_min", R_WERTE)
def test_bilanz_ohne_doppelzaehlung_und_lauf_ausloser_unveraendert(
    speicher, r_min, reihenfolge, ws_close,
):
    _oeffnen(speicher, T0)
    job = _job(speicher)
    jr = _reconnect(speicher, job, r_min, reihenfolge)
    c = jr + WS_CLOSE_NACH
    _scans(job, jr + timedelta(minutes=5), c)
    if ws_close is not None:
        dauer = int((c - T0).total_seconds()) if ws_close == "mit_dauer" else 0
        _run(speichere_externes_ventil_ereignis(speicher, VentilEreignis(
            zeitstempel=c, zone_id="magerwiese", ventil_id=MAGERWIESE,
            aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=dauer,
            ausloser=Ausloser.MANUELL,
        ), ["magerwiese"]))
        assert _belegt(speicher, c + timedelta(minutes=1)) is False, (
            "echtes SCHLIESSEN nach der Meldung gibt frei"
        )
    # Weitere Reconnects mit "offen" und 6 h Scans: nichts darf nachwachsen.
    if ws_close is None:
        _run(job.sync_aus_live_state(
            live_state={"magerwiese": "offen"}, jetzt=jr + timedelta(minutes=30),
        ))
    _scans(job, c, c + timedelta(hours=6))

    ereignisse = _ereignisse(speicher)
    schliessen = [e for e in ereignisse if e.aktion == VentilAktion.SCHLIESSEN]
    assert sum(e.dauer_sekunden for e in schliessen) == _erwartete_summe(
        r_min, ws_close,
    )
    assert [e.zeitstempel for e in ereignisse
            if e.aktion == VentilAktion.OEFFNEN] == [T0]
    # LaufAusloser (T-0609): jeder Close, der einem OEFFNEN zugeordnet wird,
    # gehoert zum Automatik-Lauf bei T0 -- kein fremdes MANUELL-OEFFNEN.
    lauf = LaufAusloser(ereignisse)
    zuordnung = [lauf.oeffnen_zu(e) for e in schliessen]
    assert all(z in (None, (T0, Ausloser.AUTOMATIK)) for z in zuordnung), zuordnung
    if ws_close == "mit_dauer":
        (echt,) = [e for e in schliessen if e.zeitstempel == c]
        assert lauf.fuer(echt) == Ausloser.AUTOMATIK


@pytest.mark.parametrize("r_min", [160, 200])
@pytest.mark.parametrize("reihenfolge", REIHENFOLGEN)
def test_cloud_zu_gibt_frei(speicher, r_min, reihenfolge):
    _oeffnen(speicher, T0)
    job = _job(speicher)
    jr = T0 + timedelta(minutes=r_min)
    if reihenfolge == "scan_zuerst":
        _run(job.aktualisiere(jetzt=jr))
    _run(job.sync_aus_live_state(live_state={"magerwiese": "zu"}, jetzt=jr))

    assert _belegt(speicher, jr + timedelta(minutes=1)) is False
    (o1,) = [e for e in _ereignisse(speicher) if e.aktion == VentilAktion.OEFFNEN]
    roh = _run(_korrektur_log(speicher, o1.id))
    assert letzte_cloud_offen_bestaetigung(roh) is None


def test_rueckblick_grenze_neuer_lauf_bekommt_eigenes_oeffnen(speicher):
    """Ein OEFFNEN aelter als CLOUD_OFFEN_RUECKBLICK (6 h) ist nicht der Lauf,
    den die Cloud jetzt meldet: neuer Lauf, eigenes OEFFNEN (T-0559)."""
    _oeffnen(speicher, T0)
    job = _job(speicher)
    jr = T0 + timedelta(hours=7)

    n = _run(job.sync_aus_live_state(live_state={"magerwiese": "offen"}, jetzt=jr))

    assert n == 1
    oeffnen = [e for e in _ereignisse(speicher) if e.aktion == VentilAktion.OEFFNEN]
    assert [e.zeitstempel for e in oeffnen] == [T0, jr]
    assert _belegt(speicher, jr + timedelta(minutes=1)) is True


def test_echt_geschlossener_lauf_ist_nicht_bekannt(speicher):
    """Nach einem echten SCHLIESSEN ist "offen" ein neuer Lauf (T-0559)."""
    _oeffnen(speicher, T0)
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=T0 + timedelta(minutes=30), zone_id="magerwiese",
        ventil_id=MAGERWIESE, aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=1800, ausloser=Ausloser.AUTOMATIK,
    )))
    jr = T0 + timedelta(minutes=60)

    n = _run(_job(speicher).sync_aus_live_state(
        live_state={"magerwiese": "offen"}, jetzt=jr,
    ))

    assert n == 1
    assert _belegt(speicher, jr + timedelta(minutes=1)) is True


def test_frueher_datierter_echter_close_hebt_die_meldung_nicht_auf(speicher):
    """Ein echtes SCHLIESSEN, das VOR der Meldung datiert (DHS-Nachtrag mit
    Stoppzeit), beendet den Lauf von T0 -- die Cloud hat danach aber "offen"
    gemeldet. Bis Meldung + MAX_OFFEN bleibt der Kanal belegt."""
    _oeffnen(speicher, T0)
    job = _job(speicher)
    jr = _reconnect(speicher, job, 160, "sync_zuerst")
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=T0 + timedelta(minutes=100), zone_id="magerwiese",
        ventil_id=MAGERWIESE, aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=6000, ausloser=Ausloser.MANUELL,
    )))

    assert _belegt(speicher, jr + timedelta(minutes=10)) is True


def test_bestaetigung_nach_dem_stichtag_zaehlt_nicht(speicher):
    """`bis` ist der Betrachtungszeitpunkt (T-0536): eine spaetere Meldung darf
    die Frage "war der Kanal damals offen?" nicht rueckwirkend aendern."""
    _oeffnen(speicher, T0)
    job = _job(speicher)
    _reconnect(speicher, job, 200, "sync_zuerst")

    offen = _run(speicher.hole_offene_ventil_kanaele(
        seit=T0 + timedelta(minutes=170), bis=T0 + timedelta(minutes=180),
    ))
    assert offen == []


async def _korrektur_log(speicher, ereignis_id: int) -> str | None:
    async with speicher._db.execute(
        "SELECT korrektur_log FROM ventil_ereignis WHERE id = ?", (ereignis_id,),
    ) as cursor:
        zeile = await cursor.fetchone()
    return zeile[0]


def test_bestaetigung_ist_kein_herkunftsvermerk(speicher):
    """Eigener `feld`-Wert: `letzte_herkunft` und `orphan_synthetisch`
    bleiben, wie sie waren; die letzte Meldung gewinnt."""
    _oeffnen(speicher, T0)
    job = _job(speicher)
    jr = _reconnect(speicher, job, 160, "sync_zuerst")
    _run(job.sync_aus_live_state(
        live_state={"magerwiese": "offen"}, jetzt=jr + timedelta(minutes=30),
    ))

    (o1,) = [e for e in _ereignisse(speicher) if e.aktion == VentilAktion.OEFFNEN]
    roh = _run(_korrektur_log(speicher, o1.id))
    eintraege = json.loads(roh)
    assert [e["feld"] for e in eintraege] == [KORREKTUR_FELD_CLOUD_OFFEN] * 2
    assert letzte_herkunft(roh) is None
    assert o1.orphan_synthetisch is False
    assert letzte_cloud_offen_bestaetigung(roh) == (
        jr + timedelta(minutes=30)
    ).isoformat()
    assert _belegt(speicher, jr + timedelta(minutes=170)) is True


# --- Gate T-0625: Cloud "zu" beendet die Offen-Bestaetigung --------------------


def _zu(speicher, job, jetzt: datetime) -> None:
    _run(job.sync_aus_live_state(live_state={"magerwiese": "zu"}, jetzt=jetzt))


@pytest.mark.parametrize("reihenfolge", REIHENFOLGEN)
@pytest.mark.parametrize("r_offen,r_zu", [(160, 200), (130, 140)])
def test_cloud_zu_nach_bestaetigung_gibt_frei(speicher, r_offen, r_zu, reihenfolge):
    """Geschaetztes SCHLIESSEN liegt vor (Scans nach der Meldung): der
    Close-Nachtrag des "zu"-Pfads sieht den Lauf als gepaart. Ohne Vermerk
    blieb der Kanal bis r_offen + 150 belegt (Gate: 311 bzw. 281)."""
    _oeffnen(speicher, T0)
    job = _job(speicher)
    jr = _reconnect(speicher, job, r_offen, reihenfolge)
    jz = T0 + timedelta(minutes=r_zu)
    _scans(job, jr + timedelta(minutes=5), jz)
    assert any(
        e.orphan_synthetisch for e in _ereignisse(speicher)
    ), "Voraussetzung: geschaetztes SCHLIESSEN vorhanden"
    assert _belegt(speicher, jz - timedelta(minutes=1)) is True

    _zu(speicher, job, jz)

    assert _belegt(speicher, jz + timedelta(minutes=1)) is False
    ereignisse = _ereignisse(speicher)
    assert [e.aktion for e in ereignisse].count(VentilAktion.OEFFNEN) == 1
    assert sum(e.dauer_sekunden for e in ereignisse) == 5400, (
        "der Vermerk ist kein Ereignis"
    )
    (o1,) = [e for e in ereignisse if e.aktion == VentilAktion.OEFFNEN]
    roh = _run(_korrektur_log(speicher, o1.id))
    assert letzte_cloud_zu_bestaetigung(roh) == jz.isoformat()
    assert letzte_herkunft(roh) is None


def test_cloud_zu_ohne_bestaetigung_unveraendert(speicher):
    """Ohne vorherige Offen-Bestaetigung kein Vermerk: es bleibt bei T-0616 4b
    (geschaetzter Close sperrt bis T0 + MAX_OFFEN)."""
    _oeffnen(speicher, T0)
    job = _job(speicher)
    _run(job.aktualisiere(jetzt=T0 + timedelta(minutes=130)))
    jz = T0 + timedelta(minutes=140)

    _zu(speicher, job, jz)

    assert _belegt(speicher, jz + timedelta(minutes=1)) is True
    assert _belegt(speicher, T0 + timedelta(minutes=151)) is False
    (o1,) = [e for e in _ereignisse(speicher) if e.aktion == VentilAktion.OEFFNEN]
    assert _run(_korrektur_log(speicher, o1.id)) is None


def test_erneut_offen_nach_zu_ist_wieder_belegt(speicher):
    """Nach "zu" ist eine neue Meldung "offen" ein neuer Lauf (T-0559)."""
    _oeffnen(speicher, T0)
    job = _job(speicher)
    jr = _reconnect(speicher, job, 160, "scan_zuerst")
    _zu(speicher, job, jr + timedelta(minutes=40))
    jo = jr + timedelta(minutes=70)

    n = _run(job.sync_aus_live_state(live_state={"magerwiese": "offen"}, jetzt=jo))

    assert n == 1
    assert _belegt(speicher, jo + timedelta(minutes=1)) is True
    assert _belegt(speicher, jo + timedelta(minutes=149)) is True


def test_offen_juenger_als_zu_gilt_wieder(speicher):
    """Regel am Speicher selbst: massgeblich ist, welche Meldung juenger ist."""
    _oeffnen(speicher, T0)
    (o1,) = [e for e in _ereignisse(speicher) if e.aktion == VentilAktion.OEFFNEN]
    for minuten, feld in ((160, "cloud_offen_bestaetigt"), (200, "cloud_zu_bestaetigt"),
                          (230, "cloud_offen_bestaetigt")):
        _run(speicher.aktualisiere_ventil_ereignis(
            o1.id, jetzt=T0 + timedelta(minutes=minuten),
            **{feld: T0 + timedelta(minutes=minuten)},
        ))

    def offen(minuten: int) -> list:
        jetzt = T0 + timedelta(minutes=minuten)
        return _run(speicher.hole_offene_ventil_kanaele(
            seit=jetzt - timedelta(minutes=150), bis=jetzt,
        ))

    assert offen(210) == []
    assert [z["ventil_id"] for z in offen(240)] == [MAGERWIESE]
