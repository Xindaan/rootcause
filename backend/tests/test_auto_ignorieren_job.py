"""T-0250: Tests fuer AutoIgnorierenJob.

Realfall hecke 24./25.05.: hecke-Ventilkanal wird temporaer fuer
Gras-Aussaat-Beregnung genutzt. Events kommen als `manuell` rein,
landen in der Bilanz als Hecke-Liter -- semantisch falsch.

Loesung: `ml_ausschluss_fenster` mit `events_auto_ignorieren: true`,
Job flippt manuell/watchdog -> ignoriert.
"""

import asyncio
from datetime import datetime

import pytest

from bewaesserung.auto_ignorieren_job import AutoIgnorierenJob
from bewaesserung.modelle import (
    Ausloser,
    MlAusschlussFenster,
    VentilAktion,
    VentilEreignis,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


def _event(
    speicher: Speicher,
    zone_id: str,
    zeit: datetime,
    aktion: VentilAktion,
    ausloser: Ausloser,
    dauer: int = 0,
) -> None:
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=zeit, zone_id=zone_id, ventil_id="test",
        aktion=aktion, dauer_sekunden=dauer, ausloser=ausloser,
    )))


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "auto_ign.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _hole_ausloeser(speicher: Speicher, zone_id: str) -> list[str]:
    erg = _run(speicher.hole_ventil_ereignisse(zone_id))
    return [e.ausloser.value for e in erg]


# ------------------------------------------------------------------
# Hauptpfad
# ------------------------------------------------------------------

def test_t0250_manuell_und_watchdog_werden_geflipt(speicher):
    """manuell + watchdog im Fenster -> ignoriert."""
    von = datetime(2026, 5, 24, 12, 0)
    bis = datetime(2026, 6, 21, 20, 0)
    _event(speicher, "hecke", datetime(2026, 5, 24, 13, 0),
           VentilAktion.OEFFNEN, Ausloser.MANUELL)
    _event(speicher, "hecke", datetime(2026, 5, 24, 13, 1),
           VentilAktion.SCHLIESSEN, Ausloser.MANUELL, dauer=60)
    _event(speicher, "hecke", datetime(2026, 5, 24, 13, 5),
           VentilAktion.SCHLIESSEN, Ausloser.WATCHDOG, dauer=300)

    fenster = MlAusschlussFenster(
        zone_id="hecke", von=von, bis=bis, events_auto_ignorieren=True,
    )
    job = AutoIgnorierenJob(speicher, [fenster])
    n = _run(job.aktualisiere(jetzt=datetime(2026, 5, 24, 14, 0)))
    assert n == 3
    assert _hole_ausloeser(speicher, "hecke") == ["ignoriert"] * 3


def test_t0250_opt_in_default_aus(speicher):
    """Ohne `events_auto_ignorieren: True` greift der Job NICHT."""
    von = datetime(2026, 5, 24, 12, 0)
    bis = datetime(2026, 6, 21, 20, 0)
    _event(speicher, "waldblumenhain", datetime(2026, 5, 24, 13, 0),
           VentilAktion.OEFFNEN, Ausloser.MANUELL)
    fenster = MlAusschlussFenster(
        zone_id="waldblumenhain", von=von, bis=bis,
        # events_auto_ignorieren default False
    )
    job = AutoIgnorierenJob(speicher, [fenster])
    n = _run(job.aktualisiere(jetzt=datetime(2026, 5, 24, 14, 0)))
    assert n == 0
    assert _hole_ausloeser(speicher, "waldblumenhain") == ["manuell"]


def test_t0300_opt_in_automatik_ueberlebt_zweiten_lauf(speicher):
    """T-0300 Magerwiese-Opt-in: ein per Frontend ('Zaehlt / echte
    Bewaesserung') auf `automatik` gehobener echter Lauf wird vom Job NICHT
    re-geflippt -- auch beim naechsten 10-min-Scan nicht. Grass-only-Laeufe
    bleiben default ignoriert (anderer, nicht gehobener Event)."""
    von = datetime(2026, 6, 13, 13, 46)
    bis = datetime(2026, 6, 21, 20, 0)
    # Echter Magerwiese-Lauf (von der Live-Pipeline als manuell geloggt).
    _event(speicher, "magerwiese", datetime(2026, 6, 14, 6, 0),
           VentilAktion.OEFFNEN, Ausloser.MANUELL)
    _event(speicher, "magerwiese", datetime(2026, 6, 14, 6, 10),
           VentilAktion.SCHLIESSEN, Ausloser.MANUELL, dauer=600)
    # Grass-only-Lauf am selben Tag (soll ignoriert bleiben).
    _event(speicher, "magerwiese", datetime(2026, 6, 14, 18, 0),
           VentilAktion.SCHLIESSEN, Ausloser.MANUELL, dauer=900)
    fenster = MlAusschlussFenster(
        zone_id="magerwiese", von=von, bis=bis, events_auto_ignorieren=True,
    )
    job = AutoIgnorierenJob(speicher, [fenster])

    # 1. Lauf: alles default auf ignoriert.
    _run(job.aktualisiere(jetzt=datetime(2026, 6, 14, 19, 0)))
    assert _hole_ausloeser(speicher, "magerwiese") == ["ignoriert"] * 3

    # Positiv-Opt-in (Frontend-Button): NUR den echten 6:00-Lauf (OEFFNEN +
    # SCHLIESSEN-Paar) auf automatik heben.
    events = _run(speicher.hole_ventil_ereignisse("magerwiese"))
    for e in events:
        if e.id is not None and e.zeitstempel.hour == 6:
            _run(speicher.aktualisiere_ventil_ereignis(
                e.id, ausloser=Ausloser.AUTOMATIK,
            ))
    assert sorted(_hole_ausloeser(speicher, "magerwiese")) == [
        "automatik", "automatik", "ignoriert",
    ]

    # 2. Lauf: automatik wird NICHT re-geflippt, ignoriert bleibt ignoriert.
    n2 = _run(job.aktualisiere(jetzt=datetime(2026, 6, 14, 19, 30)))
    assert n2 == 0
    assert sorted(_hole_ausloeser(speicher, "magerwiese")) == [
        "automatik", "automatik", "ignoriert",
    ]


def test_t0277_unbekannt_auf_nicht_optin_zone_bleibt(speicher):
    """T-0277-Regression: Der Realfall, der die Fehl-These ausloeste.

    waldblumenhain/bambuswald_yogaraum hatten 28./29.05.
    `unbekannt`->`ignoriert`-Flips OHNE `events_auto_ignorieren`-Fenster.
    Verdacht war "Job-Zone-Filter zu lasch". Diagnose: der Job ruehrt
    `unbekannt` NIEMALS an (Filter manuell/watchdog), und ohne Opt-In
    laeuft er gar nicht -- die Flips kamen aus dem Frontend-Bulk-Klick.

    Dieser Test friert beide Schutzschichten fuer den Realfall ein:
    eine Zone OHNE Opt-In mit `unbekannt`-Heuristik-Events bleibt
    vollstaendig unberuehrt, selbst wenn ein hecke-Fenster aktiv ist.
    """
    von = datetime(2026, 5, 24, 12, 0)
    bis = datetime(2026, 6, 21, 20, 0)
    # waldblumen: Heuristik-unbekannt-Events (kein Opt-In-Fenster).
    _event(speicher, "waldblumenhain", datetime(2026, 5, 28, 2, 37),
           VentilAktion.OEFFNEN, Ausloser.UNBEKANNT)
    _event(speicher, "waldblumenhain", datetime(2026, 5, 28, 2, 46),
           VentilAktion.SCHLIESSEN, Ausloser.UNBEKANNT)
    # hecke: aktives Opt-In-Fenster (das einzige in der echten Konfig).
    _event(speicher, "hecke", datetime(2026, 5, 24, 13, 0),
           VentilAktion.OEFFNEN, Ausloser.MANUELL)
    fenster_hecke = MlAusschlussFenster(
        zone_id="hecke", von=von, bis=bis, events_auto_ignorieren=True,
    )
    job = AutoIgnorierenJob(speicher, [fenster_hecke])
    _run(job.aktualisiere(jetzt=datetime(2026, 5, 28, 14, 0)))
    # waldblumen-unbekannt voellig unberuehrt (beide Events).
    assert sorted(_hole_ausloeser(speicher, "waldblumenhain")) == sorted([
        "unbekannt", "unbekannt",
    ])
    # hecke-manuell wurde regulaer geflipt (Opt-In greift dort).
    assert _hole_ausloeser(speicher, "hecke") == ["ignoriert"]


def test_t0250_andere_ausloeser_unberuehrt(speicher):
    """aquabloom / automatik / unbekannt bleiben unangetastet."""
    von = datetime(2026, 5, 24, 12, 0)
    bis = datetime(2026, 6, 21, 20, 0)
    _event(speicher, "hecke", datetime(2026, 5, 24, 13, 0),
           VentilAktion.OEFFNEN, Ausloser.AQUABLOOM)
    _event(speicher, "hecke", datetime(2026, 5, 24, 13, 1),
           VentilAktion.OEFFNEN, Ausloser.AUTOMATIK)
    _event(speicher, "hecke", datetime(2026, 5, 24, 13, 2),
           VentilAktion.OEFFNEN, Ausloser.UNBEKANNT)
    fenster = MlAusschlussFenster(
        zone_id="hecke", von=von, bis=bis, events_auto_ignorieren=True,
    )
    job = AutoIgnorierenJob(speicher, [fenster])
    n = _run(job.aktualisiere(jetzt=datetime(2026, 5, 24, 14, 0)))
    assert n == 0
    # alle drei bleiben mit ihren urspruenglichen Auslosern
    assert sorted(_hole_ausloeser(speicher, "hecke")) == sorted([
        "aquabloom", "automatik", "unbekannt",
    ])


def test_t0250_events_ausserhalb_fenster_unberuehrt(speicher):
    """Events vor `von` oder nach `bis` werden nicht geflipt."""
    von = datetime(2026, 5, 24, 12, 0)
    bis = datetime(2026, 5, 24, 14, 0)
    _event(speicher, "hecke", datetime(2026, 5, 24, 10, 0),
           VentilAktion.OEFFNEN, Ausloser.MANUELL)  # vor von
    _event(speicher, "hecke", datetime(2026, 5, 24, 13, 0),
           VentilAktion.OEFFNEN, Ausloser.MANUELL)  # drin
    _event(speicher, "hecke", datetime(2026, 5, 24, 16, 0),
           VentilAktion.OEFFNEN, Ausloser.MANUELL)  # nach bis
    fenster = MlAusschlussFenster(
        zone_id="hecke", von=von, bis=bis, events_auto_ignorieren=True,
    )
    job = AutoIgnorierenJob(speicher, [fenster])
    n = _run(job.aktualisiere(jetzt=datetime(2026, 5, 24, 17, 0)))
    assert n == 1
    ausloeser = _hole_ausloeser(speicher, "hecke")
    assert ausloeser.count("manuell") == 2
    assert ausloeser.count("ignoriert") == 1


def test_t0250_idempotent(speicher):
    """Zweiter Lauf flippt nichts mehr."""
    von = datetime(2026, 5, 24, 12, 0)
    bis = datetime(2026, 6, 21, 20, 0)
    _event(speicher, "hecke", datetime(2026, 5, 24, 13, 0),
           VentilAktion.OEFFNEN, Ausloser.MANUELL)
    fenster = MlAusschlussFenster(
        zone_id="hecke", von=von, bis=bis, events_auto_ignorieren=True,
    )
    job = AutoIgnorierenJob(speicher, [fenster])
    n1 = _run(job.aktualisiere(jetzt=datetime(2026, 5, 24, 14, 0)))
    n2 = _run(job.aktualisiere(jetzt=datetime(2026, 5, 24, 14, 1)))
    assert n1 == 1
    assert n2 == 0


def test_t0250_andere_zone_unbeeinflusst(speicher):
    """Fenster fuer hecke flippt NICHT bambuswald-Events."""
    von = datetime(2026, 5, 24, 12, 0)
    bis = datetime(2026, 6, 21, 20, 0)
    _event(speicher, "hecke", datetime(2026, 5, 24, 13, 0),
           VentilAktion.OEFFNEN, Ausloser.MANUELL)
    _event(speicher, "bambuswald", datetime(2026, 5, 24, 13, 0),
           VentilAktion.OEFFNEN, Ausloser.MANUELL)
    fenster = MlAusschlussFenster(
        zone_id="hecke", von=von, bis=bis, events_auto_ignorieren=True,
    )
    job = AutoIgnorierenJob(speicher, [fenster])
    _run(job.aktualisiere(jetzt=datetime(2026, 5, 24, 14, 0)))
    assert _hole_ausloeser(speicher, "hecke") == ["ignoriert"]
    assert _hole_ausloeser(speicher, "bambuswald") == ["manuell"]


def test_t0250_throttle_10min_intervall(speicher):
    """`aktualisiere_wenn_faellig` ruft `aktualisiere` nicht oefter
    als alle 10 min auf."""
    von = datetime(2026, 5, 24, 12, 0)
    bis = datetime(2026, 6, 21, 20, 0)
    _event(speicher, "hecke", datetime(2026, 5, 24, 13, 0),
           VentilAktion.OEFFNEN, Ausloser.MANUELL)
    fenster = MlAusschlussFenster(
        zone_id="hecke", von=von, bis=bis, events_auto_ignorieren=True,
    )
    job = AutoIgnorierenJob(speicher, [fenster])

    # 1. Lauf: flippt 1 Event.
    n1 = _run(job.aktualisiere_wenn_faellig(
        jetzt=datetime(2026, 5, 24, 14, 0),
    ))
    assert n1 == 1
    # 2. Lauf 5 min spaeter: noch im Throttle, kein Re-Scan.
    n2 = _run(job.aktualisiere_wenn_faellig(
        jetzt=datetime(2026, 5, 24, 14, 5),
    ))
    assert n2 == 0
    # Neuer manuell-Event nach Throttle-Fenster.
    _event(speicher, "hecke", datetime(2026, 5, 24, 14, 8),
           VentilAktion.OEFFNEN, Ausloser.MANUELL)
    # 3. Lauf 11 min nach erstem: Throttle abgelaufen, Re-Scan flippt.
    n3 = _run(job.aktualisiere_wenn_faellig(
        jetzt=datetime(2026, 5, 24, 14, 11),
    ))
    assert n3 == 1


def test_t0250_leere_fenster_liste(speicher):
    """Job ohne Opt-In-Fenster macht nichts."""
    fenster_kein_optin = MlAusschlussFenster(
        zone_id="hecke",
        von=datetime(2026, 5, 24, 12, 0),
        bis=datetime(2026, 6, 21, 20, 0),
        events_auto_ignorieren=False,
    )
    job = AutoIgnorierenJob(speicher, [fenster_kein_optin])
    n = _run(job.aktualisiere(jetzt=datetime(2026, 5, 24, 14, 0)))
    assert n == 0
