"""T-0639: AquaBloom-Pumpendauer als datierte Historie.

Grenzen, die hier gesichert werden: Aufloesung `aquabloom_dauer_sekunden`
(vor/am/nach einem Eintrag, ohne Historie), Parser-Fehlerfaelle, Validator
"letzter Eintrag = Einzelwert", Job (Dauer, Liter und SCHLIESSEN-Zeitstempel
aus dem Pulszeitpunkt), Bilanz F19 (Event-Zeitstempel) und die ausgelieferte
Config (eine Historie fuer alle drei Zonen der Pumpe).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from bewaesserung.aquabloom_job import AquabloomJob
from bewaesserung.bilanz import ereignis_zu_liter
from bewaesserung.konfig import _parse_aquabloom_dauer_historie, lade_konfig
from bewaesserung.modelle import (
    Ausloser,
    BilanzKonfig,
    VentilAktion,
    VentilEreignis,
    ZonenKonfig,
)

from test_aquabloom_job import _konfig, _run, _schreibe_heuristik_paar, speicher  # noqa: F401
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent.parent / "config" / "default.yaml"
# Im oeffentlichen Snapshot fehlt die echte default.yaml (Muster wie in
# test_t0610_el4_unbekannte_zonen_schluessel.py).
OEFFENTLICHER_SNAPSHOT = not DEFAULT_CONFIG.exists()

HISTORIE = ((date(2026, 7, 13), 600), (date(2026, 8, 19), 300))


def _zone(historie=HISTORIE, einzelwert: int | None = 300, **kw) -> ZonenKonfig:
    werte = dict(
        zone_id="zitrus", name="Zitrus", ventil_kanal=None,
        aquabloom_pumpen_dauer_sekunden=einzelwert,
        aquabloom_pumpen_dauer_historie=historie,
        aquabloom_pumpen_intervall_stunden=48.0,
        aquabloom_tropfer_anzahl=2,
        aquabloom_tropfer_liter_pro_stunde=2.0,
    )
    werte.update(kw)
    return ZonenKonfig(**werte)


@pytest.mark.parametrize("zeitpunkt, erwartet", [
    (datetime(2026, 7, 12, 23, 59), 300),   # vor erstem Eintrag -> Einzelwert
    (datetime(2026, 7, 13, 0, 0), 600),     # genau am ab-Tag
    (datetime(2026, 8, 18, 23, 59), 600),   # danach, vor dem zweiten
    (datetime(2026, 8, 19, 6, 0), 300),     # zweiter Eintrag
])
def test_aufloesung_nach_pulstag(zeitpunkt, erwartet):
    assert _zone().aquabloom_dauer_sekunden(zeitpunkt) == erwartet


def test_aufloesung_ohne_historie_ist_einzelwert():
    z = _zone(historie=(), einzelwert=900)
    assert z.aquabloom_dauer_sekunden(datetime(2026, 7, 20)) == 900
    assert _zone(historie=(), einzelwert=None).aquabloom_dauer_sekunden(
        datetime(2026, 7, 20)) is None


def test_parser_gueltig_und_fehlend():
    assert _parse_aquabloom_dauer_historie(None, "z") == ()
    assert _parse_aquabloom_dauer_historie([], "z") == ()
    assert _parse_aquabloom_dauer_historie(
        [{"ab": "2026-07-13", "sekunden": 600}, {"ab": date(2026, 8, 19), "sekunden": 300}],
        "z",
    ) == HISTORIE


@pytest.mark.parametrize("roh, muster", [
    ({"ab": "2026-07-13", "sekunden": 600}, "Liste"),
    ([600], "kein Mapping"),
    ([{"ab": "2026-07-13", "sekunden": 600, "grund": "x"}], r"unbekannt \['grund'\]"),
    ([{"ab": "2026-07-13"}], r"fehlend \['sekunden'\]"),
    ([{"ab": "13.07.2026", "sekunden": 600}], "kein Datum"),
    ([{"ab": "2026-07-13", "sekunden": 0}], "ganze Zahl > 0"),
    ([{"ab": "2026-07-13", "sekunden": 600.5}], "ganze Zahl > 0"),
    ([{"ab": "2026-07-13", "sekunden": True}], "ganze Zahl > 0"),
])
def test_parser_bricht_bei_ungueltigem_eintrag_ab(roh, muster):
    with pytest.raises(ValueError, match=muster):
        _parse_aquabloom_dauer_historie(roh, "zitrus")


def test_validator_nicht_aufsteigend():
    with pytest.raises(ValidationError, match="nicht streng aufsteigend"):
        _zone(historie=((date(2026, 8, 19), 600), (date(2026, 8, 19), 300)))


def test_validator_letzter_eintrag_muss_einzelwert_gleichen():
    with pytest.raises(ValidationError, match=r"\(300 s\).*\(900 s\)"):
        _zone(einzelwert=900)


def test_job_bucht_dauer_zum_pulszeitpunkt(speicher):  # noqa: F811
    """Puls 20.07.2026: gueltig sind 600 s, nicht der Einzelwert 300 s.
    Liter 600/3600 * 2 * 2.0 = 0.6667; SCHLIESSEN = OEFFNEN + 600 s."""
    t_puls = datetime(2026, 7, 20, 8, 0)
    zone = _zone(aquabloom_anker_zeitstempel=t_puls - timedelta(hours=48))
    job = AquabloomJob(speicher, _konfig(zone), intervall_minuten=0)
    _schreibe_heuristik_paar(speicher, "zitrus", t_puls, dauer_s=180)

    assert _run(job.aktualisiere_wenn_faellig(t_puls + timedelta(hours=1))) == 1

    events = _run(speicher.hole_ventil_ereignisse("zitrus"))
    oeffnen = next(e for e in events if e.aktion == VentilAktion.OEFFNEN)
    schliessen = next(e for e in events if e.aktion == VentilAktion.SCHLIESSEN)
    assert schliessen.ausloser == Ausloser.AQUABLOOM
    assert schliessen.dauer_sekunden == 600
    assert schliessen.liter == pytest.approx(0.6667, abs=1e-4)
    assert schliessen.zeitstempel == oeffnen.zeitstempel + timedelta(seconds=600)


def test_bilanz_f19_bucht_dauer_zum_eventzeitpunkt():
    """Bulk-geflipptes AquaBloom-Event ohne liter am 01.08.2026 -> 600 s."""
    event = VentilEreignis(
        zeitstempel=datetime(2026, 8, 1, 8, 3),
        zone_id="zitrus", ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=180,
        ausloser=Ausloser.AQUABLOOM, liter=None,
    )
    liter = ereignis_zu_liter(event, _zone(), BilanzKonfig())
    assert liter == pytest.approx(600 / 3600 * 2 * 2.0)


@pytest.mark.skipif(OEFFENTLICHER_SNAPSHOT, reason="oeffentlicher Snapshot: config/default.yaml privat")
def test_ausgelieferte_config_eine_historie_fuer_die_pumpe():
    nach_id = {z.zone_id: z for z in lade_konfig(DEFAULT_CONFIG).zonen}
    historien = {
        zid: nach_id[zid].aquabloom_pumpen_dauer_historie
        for zid in ("zitrus", "mandevilla", "kasten_4")
    }
    # T-0551 Entscheid (a) 04.11.2026: 26.06.-12.07. gilt 600 s wie die
    # umgeschriebenen DB-Pulse; vor dem 13.07. darf die Bilanz nicht auf den
    # heutigen Einzelwert 300 fallen (zweite Wahrheit, Gate 04.11.).
    ausgeliefert = ((date(2026, 5, 10), 600), (date(2026, 8, 19), 300))
    assert set(historien.values()) == {ausgeliefert}, historien
    for zid in historien:
        zone = nach_id[zid]
        assert zone.aquabloom_pumpen_dauer_sekunden == ausgeliefert[-1][1]
        assert zone.aquabloom_dauer_sekunden(datetime(2026, 7, 1, 7, 0)) == 600
        assert zone.aquabloom_dauer_sekunden(datetime(2026, 8, 19, 7, 0)) == 300
