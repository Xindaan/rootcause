"""T-0609 (Gesamtreview VK-3, Option A): Konsumenten lesen den Lauf-Ausloeser.

`ausloser` am SCHLIESSEN ist bei stoppe() und OrphanCloseJob der STOPP-Grund
(watchdog), nicht der Ausloeser des Laufs. Wirkungsfit, ML-Pulse und
Leck-Detektor fragen aber "war das echtes Wasser eines Laufs?". Vor T-0609
schloss der Fit Watchdog-Closes von Automatik-Laeufen aus, das ML nahm sie
(und umging dabei T-0492), der Leck-Re-Check schloss die vom Hauptcheck
geoeffnete Warnung als verwaist.

Ausgefuehrt gegen die echten Konsumenten mit temporaerer DB; keine Netzaufrufe,
kein Ventil. Die Tabelle aus dem Befund (W4_events_t0609_befund.md) ist hier
mit OEFFNEN-Pendant nachgebaut -- ohne OEFFNEN gibt es keinen Lauf-Beleg.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.kalibrierung import KalibrationsJob
from bewaesserung.leck_detektor import LeckDetektor
from bewaesserung.ml.response_features import _baue_pulse
from bewaesserung.modelle import (
    Ausloser,
    KalibrierungKonfig,
    LaufAusloser,
    SensorMessung,
    SensorWarnung,
    SensorWarnungTyp,
    VentilAktion,
    VentilEreignis,
)
from bewaesserung.speicher import HERKUNFT_ORPHAN_ZEITBASIERT, Speicher

from test_kalibrierung import _baue_messung_async, _konfig

JULI = datetime(2026, 7, 15, 8, 0)  # Saison aktiv (Leck-Detektor)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "t0609.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _ev(t, aktion, ausloser, dauer=0, zone="z", ventil="g:2", gruppe=None,
        phase=None, synthetisch=False):
    return VentilEreignis(
        zeitstempel=t, zone_id=zone, ventil_id=ventil, aktion=aktion,
        dauer_sekunden=dauer, ausloser=ausloser, lauf_gruppe=gruppe,
        phase=phase, orphan_synthetisch=synthetisch,
    )


async def _lauf_speichern(sp, *, zone, ventil, close, dauer, oeffnen_ausloser,
                          schliessen_ausloser, herkunft=None, gruppe=None,
                          phase=None):
    await sp.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=close - timedelta(seconds=dauer), zone_id=zone,
        ventil_id=ventil, aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=oeffnen_ausloser, lauf_gruppe=gruppe, phase=phase,
    ))
    await sp.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=close, zone_id=zone, ventil_id=ventil,
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=dauer,
        ausloser=schliessen_ausloser, lauf_gruppe=gruppe, phase=phase,
    ), herkunft=herkunft)


# --- Gemeinsame Zuordnung (modelle.LaufAusloser) ---------------------------

T0 = datetime(2026, 8, 9, 6, 0)


def test_lauf_ausloser_kein_pendant_hinter_einem_frueheren_close():
    """Ein Close ohne eigenes OEFFNEN erbt nicht das OEFFNEN eines Laufs, der
    schon geschlossen ist (Fehlpaarung ueber ein verwaistes Paar)."""
    evs = [
        _ev(T0, VentilAktion.OEFFNEN, Ausloser.AUTOMATIK),
        _ev(T0 + timedelta(minutes=30), VentilAktion.SCHLIESSEN,
            Ausloser.AUTOMATIK, 1800),
    ]
    spaet = _ev(T0 + timedelta(minutes=60), VentilAktion.SCHLIESSEN,
                Ausloser.WATCHDOG, 600)
    assert LaufAusloser(evs + [spaet]).fuer(spaet) == Ausloser.WATCHDOG


def test_lauf_ausloser_oeffnen_ausserhalb_des_paarungsfensters():
    close = _ev(T0 + timedelta(hours=7), VentilAktion.SCHLIESSEN,
                Ausloser.WATCHDOG, 600)
    evs = [_ev(T0, VentilAktion.OEFFNEN, Ausloser.AUTOMATIK), close]
    assert LaufAusloser(evs).fuer(close) == Ausloser.WATCHDOG


def test_lauf_ausloser_ueber_lauf_gruppe_wenn_ventil_abweicht():
    close = _ev(T0 + timedelta(minutes=30), VentilAktion.SCHLIESSEN,
                Ausloser.WATCHDOG, 1800, ventil="gardena_web",
                gruppe="presoak_x")
    evs = [
        _ev(T0, VentilAktion.OEFFNEN, Ausloser.AUTOMATIK, gruppe="presoak_x"),
        close,
    ]
    assert LaufAusloser(evs).fuer(close) == Ausloser.AUTOMATIK


def test_lauf_ausloser_klassifikation_am_close_bleibt():
    """Eine User-Klassifikation am SCHLIESSEN (fremdwasser) ist kein
    Stopp-Grund und wird nicht vom OEFFNEN ueberschrieben."""
    close = _ev(T0 + timedelta(minutes=30), VentilAktion.SCHLIESSEN,
                Ausloser.FREMDWASSER, 1800)
    evs = [_ev(T0, VentilAktion.OEFFNEN, Ausloser.AUTOMATIK), close]
    assert LaufAusloser(evs).fuer(close) == Ausloser.FREMDWASSER


# --- Wirkungsfit (kalibrierung._scan_wirkungsrate) --------------------------

T_CLOSE_FIT = datetime(2026, 4, 27, 12, 35)


async def _fit(sp, *, dauer=1800, schliessen_ausloser=Ausloser.WATCHDOG,
               herkunft=None, mit_presoak_orphan=False):
    t_close = T_CLOSE_FIT
    await _baue_messung_async(sp, t_close - timedelta(seconds=dauer), 30.0)
    await _baue_messung_async(sp, t_close + timedelta(hours=6), 50.0)
    if mit_presoak_orphan:
        # Pre-Soak-Puls (synthetisch geschlossen) + echte Hauptdose, eine Gruppe.
        puls_close = t_close - timedelta(seconds=dauer - 300)
        await _lauf_speichern(
            sp, zone="testzone", ventil="testkanal", close=puls_close,
            dauer=300, oeffnen_ausloser=Ausloser.AUTOMATIK,
            schliessen_ausloser=Ausloser.WATCHDOG,
            herkunft=HERKUNFT_ORPHAN_ZEITBASIERT, gruppe="presoak_1",
            phase="pre_soak",
        )
        await _lauf_speichern(
            sp, zone="testzone", ventil="testkanal", close=t_close,
            dauer=dauer - 300, oeffnen_ausloser=Ausloser.AUTOMATIK,
            schliessen_ausloser=Ausloser.AUTOMATIK, gruppe="presoak_1",
            phase="haupt",
        )
    else:
        await _lauf_speichern(
            sp, zone="testzone", ventil="testkanal", close=t_close,
            dauer=dauer, oeffnen_ausloser=Ausloser.AUTOMATIK,
            schliessen_ausloser=schliessen_ausloser, herkunft=herkunft,
        )
    von = t_close - timedelta(days=2)
    bis = t_close + timedelta(hours=12)
    events = await sp.hole_alle_ventil_ereignisse(von, bis)
    job = KalibrationsJob(sp, _konfig(), KalibrierungKonfig())
    return await job._scan_wirkungsrate(
        "testzone", von=von, bis=bis, ventil_events=events,
    )


@pytest.mark.parametrize("schliessen", [Ausloser.AUTOMATIK, Ausloser.WATCHDOG])
def test_fit_watchdog_close_eines_automatik_laufs_zaehlt(speicher, schliessen):
    assert _run(_fit(speicher, schliessen_ausloser=schliessen)) == 1


def test_fit_synthetischer_orphan_close_zaehlt_nicht(speicher):
    assert _run(_fit(speicher, herkunft=HERKUNFT_ORPHAN_ZEITBASIERT)) == 0


def test_fit_synthetischer_puls_macht_die_lauf_gruppe_unbrauchbar(speicher):
    assert _run(_fit(speicher, mit_presoak_orphan=True)) == 0


def test_fit_zonen_obergrenze_gilt_fuer_watchdog_close_eines_automatik_laufs(
    speicher,
):
    """Zone max_dauer 1800 s, Toleranz 10 %: 2400 s ist Phantom-Dauer, auch
    wenn der Watchdog geschlossen hat (vor T-0572b war das typisch)."""
    assert _run(_fit(speicher, dauer=2400)) == 0


# --- ML-Pulse (response_features._baue_pulse) -------------------------------

T_ML = datetime(2026, 8, 9, 6, 47)


def _ml_lauf(schliessen, *, oeffnen_vor_s=2817, synthetisch=False):
    return [
        _ev(T_ML - timedelta(seconds=oeffnen_vor_s), VentilAktion.OEFFNEN,
            Ausloser.AUTOMATIK),
        _ev(T_ML, VentilAktion.SCHLIESSEN, schliessen, oeffnen_vor_s,
            synthetisch=synthetisch),
    ]


@pytest.mark.parametrize("schliessen", [
    Ausloser.AUTOMATIK, Ausloser.WATCHDOG, Ausloser.NOTFALL_STOPP,
    Ausloser.MANUELL,
])
def test_ml_t0492_gilt_fuer_den_lauf_nicht_den_stopp_grund(schliessen):
    """Zone nie scharf: kein Wasser, egal wer schloss. Scharf: ein Puls."""
    assert _baue_pulse(_ml_lauf(schliessen), 30, automatik_scharf_ab=None) == []
    scharf = datetime(2026, 7, 1)
    assert len(_baue_pulse(_ml_lauf(schliessen), 30,
                           automatik_scharf_ab=scharf)) == 1


def test_ml_synthetischer_orphan_close_bleibt_draussen():
    scharf = datetime(2026, 7, 1)
    evs = _ml_lauf(Ausloser.WATCHDOG, synthetisch=True)
    assert _baue_pulse(evs, 30, automatik_scharf_ab=scharf) == []


def test_ml_t0492_prueft_den_laufbeginn():
    """OEFFNEN 10 min vor der Scharfschaltung, Close danach: der Lauf begann
    im Shadow, also kein Wasser."""
    scharf = T_ML - timedelta(minutes=30)
    evs = _ml_lauf(Ausloser.WATCHDOG, oeffnen_vor_s=40 * 60)
    assert _baue_pulse(evs, 30, automatik_scharf_ab=scharf) == []


# --- Leck-Detektor (Hauptcheck + spaeter Re-Check) --------------------------

def _messung(t, feuchte, zone="bambus"):
    return SensorMessung(
        zeitstempel=t, zone_id=zone, boden_feuchte=feuchte,
        boden_temperatur=15.0, batterie_prozent=95.0,
    )


async def _leck_lauf(sp, *, close, dauer=3600, oeffnen=Ausloser.AUTOMATIK,
                     schliessen=Ausloser.WATCHDOG, herkunft=None,
                     feuchte=35.0):
    start = close - timedelta(seconds=dauer)
    await sp.speichere_messung(_messung(start - timedelta(minutes=10), feuchte))
    await sp.speichere_messung(_messung(start - timedelta(minutes=1), feuchte))
    await _lauf_speichern(
        sp, zone="bambus", ventil="v1", close=close, dauer=dauer,
        oeffnen_ausloser=oeffnen, schliessen_ausloser=schliessen,
        herkunft=herkunft,
    )
    await sp.speichere_messung(_messung(close + timedelta(minutes=25), feuchte))


async def _warnung_offen(sp) -> bool:
    return any(
        w.typ == SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG
        for w in await sp.offene_sensor_warnungen("bambus")
    )


async def _warnung_setzen(sp, t):
    await sp.oeffne_sensor_warnung(SensorWarnung(
        zeitstempel=t, zone_id="bambus",
        typ=SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG, details="vorher",
    ))


@pytest.mark.parametrize("schliessen", [Ausloser.AUTOMATIK, Ausloser.WATCHDOG])
def test_leck_recheck_schliesst_warnung_nicht_wegen_watchdog(speicher, schliessen):
    """Tabelle aus dem Befund: wirkungsloser 60-min-Automatik-Lauf. Die
    Warnung bleibt nach dem spaeten Re-Check offen, auch bei Watchdog-Close."""
    async def ablauf():
        await _leck_lauf(speicher, close=JULI - timedelta(minutes=30),
                         schliessen=schliessen)
        det = LeckDetektor(speicher)
        await det.pruefe_alle(["bambus"], jetzt=JULI)
        nach_haupt = await _warnung_offen(speicher)
        await det.pruefe_alle(["bambus"], jetzt=JULI + timedelta(hours=2))
        return nach_haupt, await _warnung_offen(speicher)
    assert _run(ablauf()) == (True, True)


def test_leck_recheck_wertet_spaete_wirkung_eines_watchdog_close_aus(speicher):
    """Der Re-Check nimmt den Watchdog-Close als Kandidaten und schliesst die
    Warnung, weil der Sensor verspaetet reagiert hat."""
    async def ablauf():
        close = JULI - timedelta(minutes=30)
        await _leck_lauf(speicher, close=close)
        det = LeckDetektor(speicher)
        await det.pruefe_alle(["bambus"], jetzt=JULI)
        assert await _warnung_offen(speicher)
        await speicher.speichere_messung(
            _messung(close + timedelta(minutes=100), 60.0),
        )
        await det.pruefe_alle(["bambus"], jetzt=JULI + timedelta(hours=2))
        return await _warnung_offen(speicher)
    assert _run(ablauf()) is False


def test_leck_junger_watchdog_close_rechtfertigt_offene_warnung(speicher):
    """Close erst 10 min alt (weder Haupt- noch Re-Check-Kandidat): die
    offene Warnung ist durch ihn gerechtfertigt und wird nicht als verwaist
    geschlossen."""
    async def ablauf():
        await _warnung_setzen(speicher, JULI - timedelta(hours=3))
        await _leck_lauf(speicher, close=JULI - timedelta(minutes=10))
        await LeckDetektor(speicher).pruefe_alle(["bambus"], jetzt=JULI)
        return await _warnung_offen(speicher)
    assert _run(ablauf()) is True


def test_leck_hauptcheck_ignoriert_synthetischen_orphan_close(speicher):
    async def ablauf():
        await _leck_lauf(speicher, close=JULI - timedelta(minutes=30),
                         herkunft=HERKUNFT_ORPHAN_ZEITBASIERT)
        await LeckDetektor(speicher).pruefe_alle(["bambus"], jetzt=JULI)
        return await _warnung_offen(speicher)
    assert _run(ablauf()) is False


def test_leck_recheck_nimmt_synthetischen_close_nicht_als_kandidat(speicher):
    async def ablauf():
        await _warnung_setzen(speicher, JULI - timedelta(hours=3))
        await _leck_lauf(speicher, close=JULI - timedelta(minutes=150),
                         herkunft=HERKUNFT_ORPHAN_ZEITBASIERT)
        await LeckDetektor(speicher).pruefe_alle(["bambus"], jetzt=JULI)
        return await _warnung_offen(speicher)
    assert _run(ablauf()) is False


def test_leck_synthetischer_close_rechtfertigt_keine_warnung(speicher):
    async def ablauf():
        await _warnung_setzen(speicher, JULI - timedelta(hours=3))
        await _leck_lauf(speicher, close=JULI - timedelta(minutes=10),
                         herkunft=HERKUNFT_ORPHAN_ZEITBASIERT)
        await LeckDetektor(speicher).pruefe_alle(["bambus"], jetzt=JULI)
        return await _warnung_offen(speicher)
    assert _run(ablauf()) is False


def test_leck_hauptcheck_ignorierter_lauf_ist_phantom(speicher):
    """OEFFNEN vom User als ignoriert markiert, Close traegt den Stopp-Grund:
    der Lauf ist ein Phantom, die offene Warnung wird geschlossen."""
    async def ablauf():
        await _warnung_setzen(speicher, JULI - timedelta(hours=3))
        await _leck_lauf(speicher, close=JULI - timedelta(minutes=30),
                         oeffnen=Ausloser.IGNORIERT)
        await LeckDetektor(speicher).pruefe_alle(["bambus"], jetzt=JULI)
        return await _warnung_offen(speicher)
    assert _run(ablauf()) is False


def test_leck_recheck_findet_oeffnen_vor_dem_4h_fenster(speicher):
    """90-min-Lauf, Re-Check 3 h nach dem Close: das OEFFNEN liegt 4,5 h
    zurueck, also vor dem 4-h-Auswertefenster. Ohne Vorlauf beim Laden fehlt
    der Lauf-Beleg, und die Warnung wird als verwaist geschlossen."""
    async def ablauf():
        close = JULI - timedelta(minutes=30)
        await _leck_lauf(speicher, close=close, dauer=5400)
        det = LeckDetektor(speicher)
        await det.pruefe_alle(["bambus"], jetzt=JULI)
        assert await _warnung_offen(speicher)
        await det.pruefe_alle(["bambus"], jetzt=close + timedelta(hours=3))
        return await _warnung_offen(speicher)
    assert _run(ablauf()) is True
