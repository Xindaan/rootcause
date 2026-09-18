"""T-0038 Wochen-Report-Tests."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from bewaesserung.benachrichtigung import Benachrichtiger
from bewaesserung.modelle import (
    Ausloser,
    BenachrichtigungsKonfig,
    BilanzKonfig,
    GardenaKonfig,
    GesamtKonfig,
    SensorWarnung,
    SensorWarnungTyp,
    SpeicherKonfig,
    StandortKonfig,
    VentilAktion,
    VentilEreignis,
    WetterKonfig,
    WetterStandortKonfig,
    WochenReportKonfig,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.report import WochenReportJob, baue_wochen_report
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=[
            ZonenKonfig(
                zone_id="rasen", name="Rasen", ventil_kanal=1,
                anteil_kanal=1.0, flaeche_m2=40.0,
            ),
            ZonenKonfig(
                zone_id="bambus", name="Bambus", ventil_kanal=2,
                anteil_kanal=0.5, flaeche_m2=2.0,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[
                WetterStandortKonfig(id="standort_a", breite=52.52, laenge=13.405),
            ]
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[
            StandortKonfig(
                standort_id="garten", name="Garten",
                wetter_standort="standort_a", zonen=["rasen", "bambus"],
            ),
        ],
        bilanz=BilanzKonfig(kanal_liter_pro_minute={1: 6.0, 2: 2.0}),
    )


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "bew.db"))
    _run(s.verbinden())
    yield s
    _run(s.schliessen())


def test_wochen_report_enthaelt_alle_sektionen(speicher):
    """Report enthaelt Kopf, Bewaesserungs-Block, Wetter, ML-Drift, Warnungen."""
    konfig = _konfig()
    jetzt = datetime(2026, 4, 19, 20, 0)

    # Ein klassifiziertes OEFFNEN+SCHLIESSEN-Paar + ein UNBEKANNT-Schliessen
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=jetzt - timedelta(days=2),
        zone_id="rasen", ventil_id="gardena_web",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0, ausloser=Ausloser.AUTOMATIK,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=jetzt - timedelta(days=2) + timedelta(minutes=10),
        zone_id="rasen", ventil_id="gardena_web",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
        ausloser=Ausloser.AUTOMATIK,
    )))
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=jetzt - timedelta(days=1),
        zone_id="bambus", ventil_id="sensor_heuristik",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=300,
        ausloser=Ausloser.UNBEKANNT,
    )))

    # Eine offene Sensor-Warnung
    _run(speicher.oeffne_sensor_warnung(SensorWarnung(
        zeitstempel=jetzt - timedelta(days=1),
        zone_id="rasen", typ=SensorWarnungTyp.AUSFALL,
        details="Test-Warnung",
    )))

    text = _run(baue_wochen_report(speicher, konfig, jetzt))

    assert "Gardena-Wochen-Report" in text
    assert "KW16/2026" in text  # 2026-04-19 ist KW16
    assert "Bewaesserungen:" in text
    assert "Rasen:" in text
    assert "1x / 60L" in text  # 600s * 6 L/min ist 60 L (konfig-default + anteil 1.0)
    assert "Bambus:" in text
    assert "+1 indikativ" in text
    assert "Warnungen:" in text
    assert "Sensor offen: 1" in text


def test_wochen_report_ohne_events_schreibt_keine_bewaesserungen(speicher):
    konfig = _konfig()
    text = _run(baue_wochen_report(speicher, konfig, datetime(2026, 4, 19, 20, 0)))
    assert "Bewaesserungen: keine" in text
    assert "Warnungen: keine" in text


def test_wochen_report_job_feuert_nur_am_konfigurierten_tag(speicher):
    konfig = _konfig()
    report = WochenReportKonfig(
        aktiv=True, empfaenger="test@example.com",
        tag_der_woche=6, stunde=20,
    )
    benachrichtiger = Benachrichtiger()
    # iMessage-Senden mocken, damit kein osascript laeuft
    benachrichtiger.sende_text = AsyncMock(return_value=True)
    job = WochenReportJob(speicher, konfig, report, benachrichtiger)

    # Montag 20:00 → kein Feuer
    mo_2000 = datetime(2026, 4, 13, 20, 0)
    assert _run(job.aktualisiere_wenn_faellig(mo_2000)) is False
    benachrichtiger.sende_text.assert_not_called()

    # Sonntag 20:00 → feuert einmal
    so_2000 = datetime(2026, 4, 19, 20, 0)
    assert _run(job.aktualisiere_wenn_faellig(so_2000)) is True
    benachrichtiger.sende_text.assert_called_once()

    # Gleicher Sonntag, anderer Takt im 60-s-Loop → NICHT erneut feuern
    so_2000_30 = datetime(2026, 4, 19, 20, 30)
    assert _run(job.aktualisiere_wenn_faellig(so_2000_30)) is False
    assert benachrichtiger.sende_text.call_count == 1


def test_wochen_report_job_ohne_empfaenger_ruht(speicher):
    # Konfig ohne Zonen-Benachrichtigung UND ohne report.empfaenger → Fallback
    # findet nichts → Job ruht trotz aktiv=true.
    konfig = _konfig()
    report = WochenReportKonfig(aktiv=True, empfaenger="", tag_der_woche=6, stunde=20)
    benachrichtiger = Benachrichtiger()
    benachrichtiger.sende_text = AsyncMock(return_value=True)
    job = WochenReportJob(speicher, konfig, report, benachrichtiger)

    assert _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 20, 0))) is False
    benachrichtiger.sende_text.assert_not_called()


def test_wochen_report_job_fallback_auf_zonen_benachrichtigung(speicher):
    """`wochen_report.empfaenger` leer → erster Zonen-Benachrichtigungs-
    Empfaenger wird verwendet (User konfiguriert das sonst doppelt)."""
    konfig = _konfig()
    konfig.zonen[0].modus = ZonenModus.MONITORING
    konfig.zonen[0].benachrichtigung = BenachrichtigungsKonfig(
        empfaenger="+491111111111", cooldown_stunden=12,
    )
    report = WochenReportKonfig(aktiv=True, empfaenger="", tag_der_woche=6, stunde=20)
    benachrichtiger = Benachrichtiger()
    benachrichtiger.sende_text = AsyncMock(return_value=True)
    job = WochenReportJob(speicher, konfig, report, benachrichtiger)

    assert _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 19, 20, 0))) is True
    # Empfaenger kommt aus Zone, nicht aus report-Konfig
    args = benachrichtiger.sende_text.call_args[0]
    assert args[0] == "+491111111111"


def test_t0566_manueller_handguss_erscheint_im_report(speicher):
    """`POST /api/giessen` schreibt EIN OEFFNEN mit Dauer, kein SCHLIESSEN.

    Der Report filterte hart auf SCHLIESSEN und verlor dieses Wasser
    komplett -- waehrend `bilanz.py` es ueber eine Sonderregel mitnahm.
    Drei Verbraucher, zwei Zaehlregeln; jetzt teilen sie sich
    `ist_wasser_ereignis`.
    """
    konfig = _konfig()
    jetzt = datetime(2026, 4, 19, 20, 0)

    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=jetzt - timedelta(days=2),
        zone_id="rasen", ventil_id="manuell",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=600,
        ausloser=Ausloser.MANUELL,
    )))

    text = _run(baue_wochen_report(speicher, konfig, jetzt))

    assert "Bewaesserungen: keine" not in text
    assert "Rasen:" in text, text


def test_t0566_reines_oeffnen_ohne_dauer_zaehlt_weiterhin_nicht(speicher):
    """Gegenprobe: das OEFFNEN eines echten Paares bleibt aussen vor.

    Ohne diesen Fall koennte die neue Regel jedes OEFFNEN mitzaehlen und
    jede Gardena-Bewaesserung doppelt ausweisen.
    """
    konfig = _konfig()
    jetzt = datetime(2026, 4, 19, 20, 0)

    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=jetzt - timedelta(days=2),
        zone_id="rasen", ventil_id="dswc-1:1",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.AUTOMATIK,
    )))

    text = _run(baue_wochen_report(speicher, konfig, jetzt))

    assert "Bewaesserungen: keine" in text
