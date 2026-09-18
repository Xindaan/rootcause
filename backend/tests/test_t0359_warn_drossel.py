"""T-0359: die Veraltet-Warnung wird gedrosselt, die Aussage nicht.

**Anlass (22.08.2026).** Der Frische-Marker hat getan, wofuer er gebaut war:
`waldblumenhain` heilte sich selbst (frischer Fit am 21.08., MAE 0,96 statt
8,67) und meldet nicht mehr. `hecke` dagegen steht seit dem 02.07. auf
demselben Fit -- der Job ueberspringt sie jedes Mal mit
`skip_kein_welkepunkt` -- und erzeugte **608 Warnungen in drei Tagen**, rund
acht pro Stunde.

Eine Meldung, die achtmal pro Stunde kommt, wird nicht gelesen. Damit
verdeckt der Marker genau den Fall, fuer den er da ist: eine Zone, die NEU
veraltet, geht im Dauerrauschen der bekannten unter.

**Der Kern dieser Tests:** gedrosselt wird das LOG, nicht die AUSSAGE.
`physik_quelle` muss bei jedem einzelnen Aufruf `gefittet_veraltet` bleiben.
Ein Test, der nur die Log-Zahl prueft, wuerde eine Drosselung durchgehen
lassen, die den Zustand mit verschluckt.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.ml import physik_diagnose as pd_mod
from bewaesserung.ml.physik_diagnose import (
    K_BASIS_WARN_INTERVALL,
    QUELLE_GEFITTET,
    QUELLE_GEFITTET_VERALTET,
    loese_k_basis,
)
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    MlPhysikDiagnoseKonfig,
    SpeicherKonfig,
    StandortKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)

JETZT = datetime(2026, 8, 22, 10, 0, 0)
ALT = "2026-07-02T08:15:33"          # der reale hecke-Fit, ~51 Tage alt
FRISCH = "2026-08-21T20:09:00"


class _Speicher:
    def __init__(self, gefittet_am: str):
        self.gefittet_am = gefittet_am

    async def hole_k_basis(self, zone_id: str):
        return {
            "k_basis": 0.005,
            "et0_basis_mm_pro_h": 0.1042,
            "n_phasen": 9,
            "mae": 27.9,
            "gefittet_am": self.gefittet_am,
        }


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t"),
        zonen=[ZonenKonfig(zone_id="hecke", name="Hecke"),
               ZonenKonfig(zone_id="bambuswald", name="Bambus")],
        wetter=WetterKonfig(standorte=[WetterStandortKonfig(
            id="o", breite=52.52, laenge=13.405)]),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[StandortKonfig(standort_id="g", name="G",
                                  wetter_standort="o", zonen=["hecke"])],
        ml_physik_diagnose=MlPhysikDiagnoseKonfig(aktiv=True),
    )


@pytest.fixture(autouse=True)
def _drossel_leeren():
    """Modul-Zustand ist prozessweit -- ohne Reset faerben Tests einander."""
    pd_mod._warnungs_drossel_zuruecksetzen()
    yield
    pd_mod._warnungs_drossel_zuruecksetzen()


def _ruf(zone_id: str, jetzt: datetime, gefittet_am: str = ALT) -> str:
    zone = ZonenKonfig(zone_id=zone_id, name=zone_id)
    erg = asyncio.run(loese_k_basis(
        zone=zone, speicher=_Speicher(gefittet_am),
        konfig=_konfig(), jetzt=jetzt,
    ))
    assert erg is not None
    return erg[2]


def _warnungen(eintraege) -> list[dict]:
    return [e for e in eintraege if e.get("event") == "physik.k_basis_veraltet"]


def test_die_aussage_bleibt_bei_jedem_aufruf():
    """Der eigentliche Punkt: gedrosselt wird das Log, nicht die Quelle."""
    quellen = [_ruf("hecke", JETZT + timedelta(minutes=7 * i)) for i in range(10)]
    assert quellen == [QUELLE_GEFITTET_VERALTET] * 10, (
        "physik_quelle darf durch die Drosselung nicht verlorengehen"
    )


def test_nur_die_erste_von_zehn_meldungen_wird_geloggt():
    from structlog.testing import capture_logs

    with capture_logs() as eintraege:
        for i in range(10):
            _ruf("hecke", JETZT + timedelta(minutes=7 * i))
    treffer = _warnungen(eintraege)
    assert len(treffer) == 1, f"erwartet 1 Warnung, bekam {len(treffer)}"


def test_nach_dem_intervall_wird_wieder_gemeldet():
    from structlog.testing import capture_logs

    with capture_logs() as eintraege:
        _ruf("hecke", JETZT)
        _ruf("hecke", JETZT + K_BASIS_WARN_INTERVALL - timedelta(minutes=1))
        _ruf("hecke", JETZT + K_BASIS_WARN_INTERVALL + timedelta(minutes=1))
    assert len(_warnungen(eintraege)) == 2


def test_zonen_drosseln_sich_nicht_gegenseitig():
    """Der Fall, den die Drosselung schuetzen soll: hecke rauscht seit Wochen,
    eine ANDERE Zone veraltet neu -- die muss trotzdem sofort melden."""
    from structlog.testing import capture_logs

    with capture_logs() as eintraege:
        _ruf("hecke", JETZT)
        _ruf("bambuswald", JETZT + timedelta(minutes=1))
    assert {e["zone_id"] for e in _warnungen(eintraege)} == {"hecke", "bambuswald"}


def test_frischer_fit_loest_nichts_aus():
    from structlog.testing import capture_logs

    with capture_logs() as eintraege:
        assert _ruf("hecke", JETZT, gefittet_am=FRISCH) == QUELLE_GEFITTET
    assert not _warnungen(eintraege)


def test_drossel_haelt_die_zonen_getrennt_ueber_die_zeit():
    """Reine Zustandslogik, ohne Logger: zwei Zonen, versetzte Takte."""
    pd_mod._warnungs_drossel_zuruecksetzen()
    assert pd_mod._warnung_faellig("a", JETZT) is True
    assert pd_mod._warnung_faellig("b", JETZT) is True
    assert pd_mod._warnung_faellig("a", JETZT + timedelta(hours=23)) is False
    assert pd_mod._warnung_faellig("b", JETZT + timedelta(hours=25)) is True
