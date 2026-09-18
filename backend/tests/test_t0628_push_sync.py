"""T-0628: iMessage-Pushes in Sync mit echten Giesslaeufen.

Abgleich 14.09.-21.10.2026 (Andre, Garten-Review 21.10.): 170 von 229
Shadow-Pushes "giesse N min" ohne echten Lauf, 28 von 98 Akut-Pushes nach
dem Giessen, Wochen-Report zaehlte Schliess-Pulse statt Laeufe.

A  scharfe Zone -> kein Shadow-Push
B  Wochen-Report zaehlt Laeufe (Giess-Historie), Watchdog-Close ohne OEFFNEN
   ist kein eigener Lauf
C  Akut-Push nur, wenn juengster Snapshot noch 'akut' und seit Serienstart
   kein echtes Wasser; scharfe Zone bekommt keinen "manuell giessen"-Rat
D  Quittierliste `watchdog.quittiert`
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import test_watchdog as tw
from bewaesserung.modelle import (
    Ausloser,
    VentilAktion,
    VentilEreignis,
    WatchdogKonfig,
    WatchdogQuittung,
)
from bewaesserung.watchdog import WatchdogJob

speicher = tw.speicher  # Fixture wiederverwenden
_run = tw._run

JETZT = datetime(2026, 10, 21, 18, 0)


def _job(speicher, benach, zonen, konfig=None, motor=None):
    return WatchdogJob(
        speicher=speicher, benachrichtiger=benach,
        konfig=konfig or tw._baue_konfig(), zonen=zonen,
        gardena_zone_ids=[z.zone_id for z in zonen], motor=motor,
    )


def _ventil(speicher, zone_id, ts, aktion, ausloser, dauer=0, gruppe=None,
            ventil_id="dswc1:2"):
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=ts, zone_id=zone_id, ventil_id=ventil_id, aktion=aktion,
        dauer_sekunden=dauer, ausloser=ausloser, lauf_gruppe=gruppe,
    )))


def _akut_serie(speicher, zone_id, juengster_typ="akut"):
    for tage in (2, 1):
        _run(tw._audit_akut(speicher, zone_id, JETZT - timedelta(days=tage, hours=1)))
    _run(tw._audit_typ(speicher, zone_id, JETZT - timedelta(hours=1), "akut"))
    if juengster_typ != "akut":
        _run(tw._audit_typ(
            speicher, zone_id, JETZT - timedelta(minutes=10), juengster_typ,
        ))


def _akut_pushes(benach):
    return [t for _, t in benach.gesendet if "'akut'" in t]


# --- A ------------------------------------------------------------------------

def test_a_scharfe_zone_bekommt_keinen_shadow_push(speicher):
    zone = tw._baue_zone_mit_plateau("bambus")
    zone.auto_loop_opt_in = True
    motor = tw._MotorStubT0256({"bambus": tw._baue_empfehlung_gieße("bambus")})
    benach = tw._BenachrichtigerStub()
    _run(tw._flute_sensor(speicher, "bambus", JETZT))

    _run(_job(speicher, benach, [zone], motor=motor)
         .pruefe_und_sende_wenn_faellig(jetzt=JETZT))

    assert [t for _, t in benach.gesendet if "Shadow-Empfehlung" in t] == []
    assert motor.aufrufe == []


# --- C ------------------------------------------------------------------------

def test_c_akut_serie_vorbei_kein_push(speicher):
    """Juengster Snapshot 'kein_bedarf' (z.B. nach Handguss) -> kein Push."""
    zone = tw._baue_zone("kroton")
    benach = tw._BenachrichtigerStub()
    _run(tw._flute_sensor(speicher, "kroton", JETZT))
    _akut_serie(speicher, "kroton", juengster_typ="kein_bedarf")

    _run(_job(speicher, benach, [zone]).pruefe_und_sende_wenn_faellig(jetzt=JETZT))

    assert _akut_pushes(benach) == []


@pytest.mark.parametrize("ausloser, erwartet_push", [
    (Ausloser.AUTOMATIK, False),
    (Ausloser.MANUELL, False),
    # Unbestaetigter Sprung ist kein belegtes Wasser -> Push bleibt.
    (Ausloser.UNBEKANNT, True),
])
def test_c_akut_nach_echtem_wasser_kein_push(speicher, ausloser, erwartet_push):
    zone = tw._baue_zone("waldblumenhain")
    benach = tw._BenachrichtigerStub()
    _run(tw._flute_sensor(speicher, "waldblumenhain", JETZT))
    _akut_serie(speicher, "waldblumenhain")
    lauf = JETZT - timedelta(days=1, hours=5)
    _ventil(speicher, "waldblumenhain", lauf, VentilAktion.OEFFNEN, ausloser)
    _ventil(speicher, "waldblumenhain", lauf + timedelta(minutes=30),
            VentilAktion.SCHLIESSEN, ausloser, dauer=1800)

    _run(_job(speicher, benach, [zone]).pruefe_und_sende_wenn_faellig(jetzt=JETZT))

    assert bool(_akut_pushes(benach)) is erwartet_push


def test_c_wasser_vor_serienstart_unterdrueckt_nicht(speicher):
    """Gegossen VOR dem ersten Serientag -> Serie ist danach entstanden."""
    zone = tw._baue_zone("kroton")
    benach = tw._BenachrichtigerStub()
    _run(tw._flute_sensor(speicher, "kroton", JETZT))
    _akut_serie(speicher, "kroton")
    alt = JETZT - timedelta(days=3, hours=2)
    _ventil(speicher, "kroton", alt, VentilAktion.OEFFNEN, Ausloser.MANUELL)
    _ventil(speicher, "kroton", alt + timedelta(minutes=5),
            VentilAktion.SCHLIESSEN, Ausloser.MANUELL, dauer=300)

    _run(_job(speicher, benach, [zone]).pruefe_und_sende_wenn_faellig(jetzt=JETZT))

    assert len(_akut_pushes(benach)) == 1


def test_c_scharfe_zone_kein_manuell_giessen_rat(speicher):
    zone = tw._baue_zone_mit_plateau("hecke")
    zone.auto_loop_opt_in = True
    benach = tw._BenachrichtigerStub()
    _run(tw._flute_sensor(speicher, "hecke", JETZT))
    _akut_serie(speicher, "hecke")

    _run(_job(speicher, benach, [zone]).pruefe_und_sende_wenn_faellig(jetzt=JETZT))

    texte = _akut_pushes(benach)
    assert len(texte) == 1
    assert "Automatik" in texte[0]
    assert "manuell giessen" not in texte[0]


# --- D ------------------------------------------------------------------------

def _konfig_mit(*quittungen):
    return tw._baue_konfig(quittiert=[dict(q) for q in quittungen])


_Q_ZITRUS = dict(typ="akut_in_folge", ziel="zitrus_ii", grund="Topf bewusst trocken",
                 seit=date(2026, 10, 21))


def test_d_quittung_unterdrueckt_akut_push(speicher):
    zone = tw._baue_zone("zitrus_ii")
    benach = tw._BenachrichtigerStub()
    _run(tw._flute_sensor(speicher, "zitrus_ii", JETZT))
    _akut_serie(speicher, "zitrus_ii")

    _run(_job(speicher, benach, [zone], konfig=_konfig_mit(_Q_ZITRUS))
         .pruefe_und_sende_wenn_faellig(jetzt=JETZT))

    assert _akut_pushes(benach) == []


@pytest.mark.parametrize("abweichung", [
    {"ziel": "avocado"},
    {"typ": "pflege_faellig"},
    {"seit": date(2026, 10, 22)},
    {"seit": date(2026, 10, 1), "bis": date(2026, 10, 20)},
])
def test_d_unpassende_quittung_laesst_push_durch(speicher, abweichung):
    zone = tw._baue_zone("zitrus_ii")
    benach = tw._BenachrichtigerStub()
    _run(tw._flute_sensor(speicher, "zitrus_ii", JETZT))
    _akut_serie(speicher, "zitrus_ii")

    _run(_job(speicher, benach, [zone], konfig=_konfig_mit({**_Q_ZITRUS, **abweichung}))
         .pruefe_und_sende_wenn_faellig(jetzt=JETZT))

    assert len(_akut_pushes(benach)) == 1


def test_d_sicherheitsalarm_nicht_quittierbar():
    with pytest.raises(ValidationError, match="nicht quittierbar"):
        WatchdogKonfig(quittiert=[{**_Q_ZITRUS, "typ": "husqvarna_block"}])


def test_d_quittung_wirkt_auf_pflege_unbekannt_und_shadow(speicher):
    """Die drei anderen quittierbaren Trigger lesen dieselbe Liste."""
    shadow_zone = tw._baue_zone_mit_plateau("bambus")  # nicht scharf
    unbekannt_zone = tw._baue_zone("magerwiese")
    benach = tw._BenachrichtigerStub()
    for z in ("bambus", "magerwiese"):
        _run(tw._flute_sensor(speicher, z, JETZT))
    eid = _run(speicher.speichere_pflege_erinnerung(
        typ="wirkungsrate_cleanup", faellig_am=JETZT, beschreibung="x",
        zone_id="hecke", jetzt=JETZT - timedelta(days=1),
    ))
    _run(tw._schreibe_unbekannt_event(speicher, "magerwiese", JETZT))
    motor = tw._MotorStubT0256({
        "bambus": tw._baue_empfehlung_gieße("bambus"),
        "magerwiese": tw._baue_empfehlung_keinbedarf("magerwiese"),
    })
    konfig = _konfig_mit(
        {**_Q_ZITRUS, "typ": "pflege_faellig", "ziel": f"pflege:{eid}"},
        {**_Q_ZITRUS, "typ": "unbekannt_event", "ziel": "magerwiese"},
        {**_Q_ZITRUS, "typ": "shadow_empfehlung", "ziel": "bambus"},
    )

    _run(_job(speicher, benach, [shadow_zone, unbekannt_zone], konfig=konfig,
              motor=motor).pruefe_und_sende_wenn_faellig(jetzt=JETZT))

    assert benach.gesendet == []


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "config" / "default.yaml").exists(),
    reason="oeffentlicher Snapshot: config/default.yaml privat",
)
def test_d_config_default_yaml_laedt_quittierliste():
    """Der ausgelieferte Config-Block parst; jede Quittung traegt einen Grund
    und einen quittierbaren Typ (sonst wirft schon das Laden)."""
    from pathlib import Path

    from bewaesserung.konfig import lade_konfig

    pfad = Path(__file__).resolve().parents[2] / "config" / "default.yaml"
    k = lade_konfig(pfad)
    assert k.watchdog.quittiert, "Quittierliste leer -- Block nicht geladen?"
    assert all(isinstance(q, WatchdogQuittung) for q in k.watchdog.quittiert)
    assert WatchdogQuittung(**_Q_ZITRUS).gilt("akut_in_folge", "zitrus_ii", JETZT)


# --- B ------------------------------------------------------------------------

def _report_text(speicher):
    from bewaesserung.report import baue_wochen_report
    from test_report import _konfig as _report_konfig

    return _run(baue_wochen_report(speicher, _report_konfig(), JETZT))


def test_b_report_zaehlt_laeufe_nicht_pulse(speicher):
    """Pre-Soak (Vorpuls + Hauptpuls) = 1 Lauf; nachgetragener Watchdog-
    Close ohne OEFFNEN = kein weiterer Lauf; Handguss-Close ohne OEFFNEN
    (OEFFNEN verloren) = eigener Lauf."""
    t = JETZT - timedelta(days=2)
    g = "presoak_rasen_1"
    _ventil(speicher, "rasen", t, VentilAktion.OEFFNEN, Ausloser.AUTOMATIK, gruppe=g, ventil_id="v1")
    _ventil(speicher, "rasen", t + timedelta(minutes=5), VentilAktion.SCHLIESSEN,
            Ausloser.AUTOMATIK, dauer=300, gruppe=g, ventil_id="v1")
    _ventil(speicher, "rasen", t + timedelta(minutes=25), VentilAktion.OEFFNEN,
            Ausloser.AUTOMATIK, gruppe=g, ventil_id="v1")
    _ventil(speicher, "rasen", t + timedelta(minutes=55), VentilAktion.SCHLIESSEN,
            Ausloser.AUTOMATIK, dauer=1800, gruppe=g, ventil_id="v1")
    _ventil(speicher, "rasen", t + timedelta(hours=19), VentilAktion.SCHLIESSEN,
            Ausloser.WATCHDOG, dauer=600, ventil_id="v1")
    _ventil(speicher, "rasen", t + timedelta(days=1), VentilAktion.SCHLIESSEN,
            Ausloser.MANUELL, dauer=600, ventil_id="v1")

    zeile = next(z for z in _report_text(speicher).splitlines() if "Rasen:" in z)

    # Liter unveraendert je Ereignis: 300+1800+600+600 s = 55 min x 6 L/min
    assert zeile.strip() == "Rasen: 2x / 330L"
