"""T-0616 4b (Option B): ein geschaetztes Orphan-SCHLIESSEN gibt den Hahn nicht frei.

Ablauf: ein Fremdlauf (Gardena-App) auf dem exklusiven magerwiese-Kanal, das
SCHLIESSEN geht verloren. Der Orphan-Scan schreibt nach max_dauer + Grace
(90 + 30 min) ein zeitbasiertes SCHLIESSEN bei OEFFNEN + 90 min, ohne dass
jemand die Hardware gefragt hat (Herkunft `orphan_zeitbasiert`, T-0595 AK1).
Bis W6 beendete dieses SCHLIESSEN "offen" in der Ereignis-Sicht: der Arbiter
liess waldblumenhain parallel oeffnen, waehrend der App-Lauf (bis 180 min
moeglich) noch Wasser zog.

Geprueft mit echtem Speicher (tmp-DB), echtem Orphan-Job und echtem Arbiter;
kein Hardware-Client, keine Netzaufrufe.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.hahn_arbiter import HahnArbiter, Kanalprofil
from bewaesserung.kanal_zustand import kanal_vorgang_laeuft
from bewaesserung.modelle import (
    Ausloser,
    VentilAktion,
    VentilEreignis,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.orphan_close_job import OrphanCloseJob
from bewaesserung.speicher import HERKUNFT_WS_CALLBACK, Speicher

DSWC1 = "bbbb0001-0000-4000-8000-000000000001"
DSWC2 = "bbbb0002-0000-4000-8000-000000000002"
MAGERWIESE = f"{DSWC2}:1"
CLUSTER = "garten-haupthahn"
PROFILE = {
    (DSWC1, 1): Kanalprofil(("waldblumenhain",), CLUSTER, 6.0, True),
    (DSWC2, 1): Kanalprofil(("magerwiese",), CLUSTER, 6.0, True),
}
T0 = datetime(2026, 10, 13, 9, 0)


class _OhneEngineLauf:
    def aktive_kanaele(self) -> dict[int, tuple[str, ...]]:
        return {}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "t0616_4b.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _magerwiese() -> ZonenKonfig:
    return ZonenKonfig(
        zone_id="magerwiese", name="magerwiese", modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=30, feuchte_schwelle_max=70,
        max_dauer_sekunden=5400,
        ventil_geraet_id=DSWC2, ventil_kanal=1,
    )


def _arbiter(speicher) -> HahnArbiter:
    arbiter = HahnArbiter(
        speicher=speicher, kanalprofile=PROFILE,
        cluster_max_lpm={CLUSTER: 10.0},
    )
    arbiter.registriere_sicherung(DSWC1, _OhneEngineLauf())
    arbiter.registriere_sicherung(DSWC2, _OhneEngineLauf())
    return arbiter


def _schliessen(speicher) -> list[VentilEreignis]:
    return [
        e for e in _run(speicher.hole_ventil_ereignisse(
            "magerwiese", von=T0, bis=T0 + timedelta(hours=6),
        ))
        if e.aktion == VentilAktion.SCHLIESSEN
    ]


def _app_lauf_mit_orphan_close(speicher) -> OrphanCloseJob:
    """App-OEFFNEN bei T0, kein SCHLIESSEN; Orphan-Scan bei T0+125 min.

    `ausloser=manuell`: `ignoriert`-Zeilen fasst der Orphan-Scan bewusst nicht
    an (er wuerde ein vom User verworfenes OEFFNEN wiederbeleben)."""
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=T0, zone_id="magerwiese", ventil_id=MAGERWIESE,
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )))
    job = OrphanCloseJob(speicher, [_magerwiese()])
    assert _run(job.aktualisiere(jetzt=T0 + timedelta(minutes=125))) == 1
    return job


def _darf_waldblumen(speicher, jetzt: datetime) -> bool:
    return _run(_arbiter(speicher).pruefe(DSWC1, 1, jetzt=jetzt)).erlaubt


def _magerwiese_belegt(speicher, jetzt: datetime) -> bool:
    return _run(kanal_vorgang_laeuft(
        speicher, zone_id="magerwiese", kanal=1, geraet_id=DSWC2, jetzt=jetzt,
    ))


def test_geschaetztes_schliessen_sperrt_weiter_und_wird_nicht_wiederholt(speicher):
    job = _app_lauf_mit_orphan_close(speicher)
    jetzt = T0 + timedelta(minutes=125)

    assert _darf_waldblumen(speicher, jetzt) is False, (
        "geschaetztes SCHLIESSEN ist kein Beleg: der App-Lauf kann noch laufen"
    )
    assert _magerwiese_belegt(speicher, jetzt) is True

    # Der Scan darf sich nicht selbst neu ausloesen: fuer denselben Lauf kein
    # zweites geschaetztes SCHLIESSEN, auch wenn "offen" jetzt laenger gilt.
    assert _run(job.aktualisiere(jetzt=T0 + timedelta(minutes=135))) == 0
    assert _run(job.aktualisiere(jetzt=T0 + timedelta(minutes=200))) == 0
    assert len(_schliessen(speicher)) == 1


@pytest.mark.parametrize("weg", ["neue_zeile", "ersetzt"])
def test_echtes_schliessen_gibt_frei(speicher, weg):
    _app_lauf_mit_orphan_close(speicher)
    if weg == "neue_zeile":
        _run(speicher.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=T0 + timedelta(minutes=100), zone_id="magerwiese",
            ventil_id=MAGERWIESE, aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=6000, ausloser=Ausloser.MANUELL,
        )))
    else:
        # Wie main.speichere_externes_ventil_ereignis: der echte Close ersetzt
        # die geschaetzten Werte und haengt einen neuen Herkunftsvermerk an.
        (synth,) = _schliessen(speicher)
        assert _run(speicher.aktualisiere_ventil_ereignis(
            synth.id, zeitstempel=T0 + timedelta(minutes=100),
            dauer_sekunden=6000, herkunft=HERKUNFT_WS_CALLBACK,
        ))
    jetzt = T0 + timedelta(minutes=125)

    assert _darf_waldblumen(speicher, jetzt) is True
    assert _magerwiese_belegt(speicher, jetzt) is False


def test_nach_max_offen_frei(speicher):
    _app_lauf_mit_orphan_close(speicher)

    assert _darf_waldblumen(speicher, T0 + timedelta(minutes=149)) is False
    assert _darf_waldblumen(speicher, T0 + timedelta(minutes=151)) is True
    assert _magerwiese_belegt(speicher, T0 + timedelta(minutes=151)) is False


# Der Reconnect-Fall (Cloud meldet offen nach geschaetztem Close) steht seit
# T-0625 in test_t0625_cloud_bestaetigung.py: kein frisches OEFFNEN mehr,
# sondern eine Bestaetigung am bekannten OEFFNEN.
