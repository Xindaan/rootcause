"""T-0624: ein ignoriertes Ereignis macht die Bilanz nicht mehr "indikativ".

`ignoriert` heisst "ist nie passiert" (Phantom, Watchdog-Doppel-SCHLIESSEN,
T-0362/T-0620) und zaehlt sicher 0 L. Vorher lieferte `ereignis_zu_liter`
None, und `_summe_bewaesserung` setzte daraufhin `bewaesserung_indikativ`
fuer den ganzen Tag. `unbekannt` (nicht klassifiziert) und `fremdwasser`
(Wasser kam an, Menge unbekannt) bleiben indikativ.

Geprueft wird `bewaesserung_indikativ`, nicht das Gesamt-`indikativ`: das
wird auch von der Wetterquelle gesetzt und waere hier kein sauberes Signal.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bewaesserung.bilanz import berechne_bilanz
from bewaesserung.modelle import Ausloser, VentilAktion, VentilEreignis
from bewaesserung.report import baue_wochen_report
from test_bilanz import _konfig, _run, _zone, speicher  # noqa: F401  (Fixture)
from test_report import _konfig as _report_konfig

TAG = datetime(2026, 4, 15)


def _schliessen(speicher, ausloser, stunde, **extra):  # noqa: F811
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=TAG.replace(hour=stunde),
        zone_id="bambuswald", ventil_id="ventil-2",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
        ausloser=ausloser, **extra,
    )))


@pytest.mark.parametrize(
    ("ausloser", "indikativ"),
    [
        (Ausloser.IGNORIERT, False),
        (Ausloser.UNBEKANNT, True),
        (Ausloser.FREMDWASSER, True),
    ],
)
def test_kein_wasser_ausloeser_und_indikativ_flag(speicher, ausloser, indikativ):  # noqa: F811
    _schliessen(speicher, Ausloser.AUTOMATIK, 6)   # 10 min x 1,87 L/min
    # Literfeld aus der Zeit vor der Umklassifizierung (T-0487) darf nie zaehlen.
    _schliessen(speicher, ausloser, 7, liter=12.5)

    bilanz = _run(berechne_bilanz(
        _zone(anteil_kanal=1.0), TAG, TAG + timedelta(days=1), speicher, _konfig(),
        "standort_a",
    ))

    assert bilanz is not None
    assert bilanz.bewaesserung_liter == pytest.approx(18.7)
    assert bilanz.bewaesserung_indikativ is indikativ


def test_wochen_report_zaehlt_ignoriert_nicht_als_indikativ(tmp_path):
    from bewaesserung.speicher import Speicher

    s = Speicher(str(tmp_path / "report.db"))
    _run(s.verbinden())
    try:
        jetzt = datetime(2026, 4, 19, 20, 0)
        for ausloser, dauer in ((Ausloser.AUTOMATIK, 600), (Ausloser.IGNORIERT, 300)):
            _run(s.speichere_ventil_ereignis(VentilEreignis(
                zeitstempel=jetzt - timedelta(days=1, minutes=dauer // 60),
                zone_id="rasen", ventil_id="v1",
                aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=dauer,
                ausloser=ausloser,
            )))
        text = _run(baue_wochen_report(s, _report_konfig(), jetzt))
    finally:
        _run(s.schliessen())

    zeile = next(z for z in text.splitlines() if "Rasen:" in z)
    assert zeile.strip() == "Rasen: 1x / 60L"
