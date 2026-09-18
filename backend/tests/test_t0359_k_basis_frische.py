"""T-0359: ein alter Physik-Fit soll nicht aussehen wie ein frischer.

**Der Realfall (13.08.2026).** `physik_k_basis` trug fuer hecke und
waldblumenhain noch die Fits vom **02.07.** -- hecke mit MAE 27,9. Der
Fit-Job hatte beide seither jedes Mal uebersprungen
(`k_basis_fit.skip_kein_welkepunkt`), und `hole_k_basis` liefert die Zeile
ohne jede Frische-Angabe: sechs Wochen alt kommt an wie heute frisch. Die
Read-Only-Reihe der Stufe 1 rechnete also mit einem Parameter, den niemand
mehr bestaetigt hatte -- und genau auf dieser Reihe soll spaeter die
Stufe-2-Entscheidung fussen.

**Was hier NICHT passiert:** der Wert wird nicht verworfen. Ein alter Fit ist
immer noch besser als der `default_tau`-Pauschalwert, und ein Sprung in der
Groessenordnung mitten in der Beobachtungsreihe waere schlimmer als ein
alter Wert. Sichtbar wird es ueber `physik_quelle`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.ml.physik_diagnose import (
    K_BASIS_MAX_ALTER_TAGE,
    QUELLE_GEFITTET,
    QUELLE_GEFITTET_VERALTET,
    _alter_in_tagen,
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

JETZT = datetime(2026, 8, 13, 19, 0, 0)


class _SpeicherAttrappe:
    def __init__(self, gefittet_am, k_basis=0.005):
        self.zeile = {
            "k_basis": k_basis,
            "et0_basis_mm_pro_h": 0.1042,
            "n_phasen": 9,
            "mae": 27.9,
            "gefittet_am": gefittet_am,
        }

    async def hole_k_basis(self, zone_id: str):
        return dict(self.zeile)


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t"),
        zonen=[ZonenKonfig(zone_id="hecke", name="Hecke")],
        wetter=WetterKonfig(standorte=[WetterStandortKonfig(
            id="o", breite=52.52, laenge=13.405,
        )]),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[StandortKonfig(
            standort_id="g", name="G", wetter_standort="o", zonen=["hecke"],
        )],
        ml_physik_diagnose=MlPhysikDiagnoseKonfig(aktiv=True),
    )


def _run(coro):
    return asyncio.run(coro)


def _quelle(gefittet_am, jetzt=JETZT) -> str:
    zone = ZonenKonfig(zone_id="hecke", name="Hecke")
    ergebnis = _run(loese_k_basis(
        zone=zone, speicher=_SpeicherAttrappe(gefittet_am),
        konfig=_konfig(), jetzt=jetzt,
    ))
    assert ergebnis is not None
    return ergebnis[2]


def test_frischer_fit_bleibt_gefittet():
    gestern = (JETZT - timedelta(days=1)).isoformat()
    assert _quelle(gestern) == QUELLE_GEFITTET


def test_alter_fit_wird_als_veraltet_markiert():
    """Der Realfall: Fit vom 02.07., gelesen am 13.08."""
    assert _quelle("2026-07-02T08:15:33") == QUELLE_GEFITTET_VERALTET


def test_der_wert_selbst_bleibt_erhalten():
    """Nicht verwerfen -- ein alter Fit schlaegt den Pauschalwert, und ein
    Sprung in der Groessenordnung wuerde die Beobachtungsreihe brechen."""
    zone = ZonenKonfig(zone_id="hecke", name="Hecke")
    k_alt, et0_alt, quelle = _run(loese_k_basis(
        zone=zone, speicher=_SpeicherAttrappe("2026-07-02T08:15:33"),
        konfig=_konfig(), jetzt=JETZT,
    ))
    assert quelle == QUELLE_GEFITTET_VERALTET
    assert k_alt == pytest.approx(0.005)
    assert et0_alt == pytest.approx(0.1042)


@pytest.mark.parametrize("tage,erwartet", [
    (K_BASIS_MAX_ALTER_TAGE - 0.1, QUELLE_GEFITTET),
    (K_BASIS_MAX_ALTER_TAGE + 0.1, QUELLE_GEFITTET_VERALTET),
])
def test_schwelle_greift_an_der_richtigen_stelle(tage, erwartet):
    assert _quelle((JETZT - timedelta(days=tage)).isoformat()) == erwartet


def test_unlesbarer_zeitstempel_ist_kein_alarm():
    """`None` heisst "kein Urteil", nicht "alt" -- eine Warnung aus Unwissen
    waere ein Fehlalarm, und Fehlalarme entwerten die echte Meldung."""
    assert _alter_in_tagen("kaputt", JETZT) is None
    assert _alter_in_tagen(None, JETZT) is None
    assert _quelle("kaputt") == QUELLE_GEFITTET


def test_konfig_override_schlaegt_die_tabelle_weiterhin():
    """Die Kaskade bleibt unveraendert: ein manueller Wert gewinnt, und die
    Frische-Frage stellt sich dort gar nicht."""
    zone = ZonenKonfig(zone_id="hecke", name="Hecke", k_basis_pro_h=0.03)
    k, _, quelle = _run(loese_k_basis(
        zone=zone, speicher=_SpeicherAttrappe("2026-07-02T08:15:33"),
        konfig=_konfig(), jetzt=JETZT,
    ))
    assert quelle == "konfig"
    assert k == pytest.approx(0.03)


def test_ohne_jetzt_laeuft_es_gegen_die_uhr():
    """Regression gegen [[fehlerpattern_jetzt_nicht_durchgereicht]]: der
    Parameter ist optional, aber er muss wirken, wenn er gesetzt ist."""
    vor_zehn_tagen = (datetime.now() - timedelta(days=10)).isoformat()
    assert _quelle(vor_zehn_tagen, jetzt=None) == QUELLE_GEFITTET
