"""T-0636: Winter-Trockenwarnung im Automatik-Sperrfenster.

Je Waechter ein Test, der genau ihn prueft (Negativproben im Bericht zu
T-0636). Temporaere Test-DB, Sender-Attrappe; es wird nichts versendet.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from structlog.testing import capture_logs

from bewaesserung.modelle import (
    AutomatikSperrfenster,
    DatenQuelle,
    GardenaKonfig,
    GesamtKonfig,
    SensorMessung,
    SpeicherKonfig,
    WatchdogKonfig,
    WatchdogQuittung,
    WetterKonfig,
    WetterStandortKonfig,
    WinterTrockenwarnungKonfig,
    ZonenKonfig,
)
from bewaesserung.report import baue_wochen_report
from bewaesserung.speicher import Speicher
from bewaesserung.watchdog import WatchdogJob


def _run(coro):
    return asyncio.run(coro)


JETZT = datetime(2027, 1, 15, 12, 0)
LEAD_HECKE = "gardena-hecke-lead"
SPERRE = [AutomatikSperrfenster(
    von=datetime(2026, 10, 31, 19, 0), bis=datetime(2027, 4, 1, 0, 0),
    grund="Winterpause Test",
)]


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "winter.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


class _SenderAttrappe:
    def __init__(self):
        self.gesendet: list[tuple[str, str]] = []

    async def sende_text(self, empfaenger: str, text: str) -> bool:
        self.gesendet.append((empfaenger, text))
        return True


def _zonen() -> list[ZonenKonfig]:
    return [
        ZonenKonfig(zone_id="hecke", name="Hecke",
                    aggregat_lead_geraet=LEAD_HECKE),
        ZonenKonfig(zone_id="bambuswald", name="Bambuswald"),
        ZonenKonfig(zone_id="magerwiese", name="Magerwiese"),
    ]


def _winter(**kw) -> WinterTrockenwarnungKonfig:
    basis = dict(aktiv=True, schwellen={"hecke": 22}, nur_ausfall=[])
    basis.update(kw)
    return WinterTrockenwarnungKonfig(**basis)


def _job(sender, speicher, *, winter=None, sperrfenster=SPERRE,
         quittiert=None) -> WatchdogJob:
    return WatchdogJob(
        speicher=speicher,
        benachrichtiger=sender,
        konfig=WatchdogKonfig(
            aktiv=True, empfaenger="+49test", throttle_stunden=24,
            quittiert=quittiert or [],
        ),
        zonen=_zonen(),
        gardena_zone_ids=[],
        sperrfenster=sperrfenster,
        winter_trockenwarnung=winter or _winter(),
    )


def _reihe(speicher, zone_id, geraet_id, von, bis, feuchte, *,
           temp=4.0, quelle=DatenQuelle.GARDENA, schritt_h=1.0):
    """Messungen von `von` bis `bis` (inkl.) im Abstand `schritt_h`."""
    async def _go():
        t = von
        while t <= bis:
            await speicher.speichere_messung(SensorMessung(
                zeitstempel=t, zone_id=zone_id, geraet_id=geraet_id,
                boden_feuchte=feuchte(t) if callable(feuchte) else feuchte,
                boden_temperatur=temp(t) if callable(temp) else temp,
                quelle=quelle,
            ))
            t += timedelta(hours=schritt_h)
    _run(_go())


def _tick(job, jetzt=JETZT) -> int:
    return _run(job.pruefe_und_sende_wenn_faellig(jetzt))["gesendet"]


def _trocken_hecke(speicher, stunden=14, ende=JETZT - timedelta(minutes=10),
                   **kw):
    _reihe(speicher, "hecke", LEAD_HECKE, ende - timedelta(hours=stunden),
           ende, 15.0, **kw)


# --- Positivtest ---------------------------------------------------------

def test_trocken_frostfrei_12h_gehalten_genau_ein_push(speicher):
    _trocken_hecke(speicher)
    sender = _SenderAttrappe()
    assert _tick(_job(sender, speicher)) == 1
    assert sender.gesendet == [(
        "+49test",
        "Winterpause Hecke: Bodenfeuchte 15 % seit mind. 12 h unter 22 % "
        "(Messwert 15.01. 11:50, Boden 4 °C). Winterpause: keine Automatik. "
        "Bei frostfreiem Boden Fingerprobe, ggf. Kanne.",
    )]


# --- (a) Sperrfenster ----------------------------------------------------

def test_a_ohne_aktives_sperrfenster_kein_push(speicher):
    _trocken_hecke(speicher)
    vorbei = [AutomatikSperrfenster(
        von=datetime(2026, 10, 25, 1, 30), bis=datetime(2026, 10, 25, 3, 30),
    )]
    for fenster in ([], vorbei):
        sender = _SenderAttrappe()
        assert _tick(_job(sender, speicher, sperrfenster=fenster)) == 0
        assert sender.gesendet == []


def test_a2_block_inaktiv_kein_push_trotz_sperrfenster(speicher):
    _trocken_hecke(speicher)
    sender = _SenderAttrappe()
    assert _tick(_job(sender, speicher, winter=_winter(aktiv=False))) == 0


# --- (b) Mindestdauer ----------------------------------------------------

def test_b_unterschreitung_erst_seit_10h_kein_push(speicher):
    ende = JETZT - timedelta(minutes=10)
    _reihe(speicher, "hecke", LEAD_HECKE, ende - timedelta(hours=30), ende,
           lambda t: 15.0 if t > ende - timedelta(hours=10) else 30.0)
    sender = _SenderAttrappe()
    assert _tick(_job(sender, speicher)) == 0


def test_b2_letzter_wert_vor_fensteranfang_feucht_kein_push(speicher):
    # Messluecke: im Fenster nur trockene Werte, der Wert davor (13 h) feucht.
    _reihe(speicher, "hecke", LEAD_HECKE, JETZT - timedelta(hours=13),
           JETZT - timedelta(hours=13), 40.0)
    _reihe(speicher, "hecke", LEAD_HECKE, JETZT - timedelta(hours=11),
           JETZT - timedelta(minutes=10), 15.0)
    sender = _SenderAttrappe()
    assert _tick(_job(sender, speicher)) == 0


def test_b3_nur_ein_wert_im_fenster_kein_push(speicher):
    _reihe(speicher, "hecke", LEAD_HECKE, JETZT - timedelta(hours=20),
           JETZT - timedelta(hours=13), 15.0)
    _reihe(speicher, "hecke", LEAD_HECKE, JETZT - timedelta(hours=1),
           JETZT - timedelta(hours=1), 15.0)
    sender = _SenderAttrappe()
    assert _tick(_job(sender, speicher)) == 0


# --- (c) Frost-Nachlauf --------------------------------------------------

def test_c_frost_in_den_letzten_24h_kein_push_nur_log(speicher):
    ende = JETZT - timedelta(minutes=10)
    frost_t = ende - timedelta(hours=20)
    _trocken_hecke(speicher, stunden=30,
                   temp=lambda t: 0.5 if t == frost_t else 4.0)
    sender = _SenderAttrappe()
    with capture_logs() as logs:
        assert _tick(_job(sender, speicher)) == 0
    assert sender.gesendet == []
    assert any(e["event"] == "watchdog.winter_frost_unterdrueckt" for e in logs)


def test_c2_frost_vor_mehr_als_24h_meldet_wieder(speicher):
    ende = JETZT - timedelta(minutes=10)
    frost_t = ende - timedelta(hours=25)
    _trocken_hecke(speicher, stunden=30,
                   temp=lambda t: 0.5 if t == frost_t else 4.0)
    sender = _SenderAttrappe()
    assert _tick(_job(sender, speicher)) == 1


# --- (d) 72-h-Drossel ----------------------------------------------------

def test_d_drossel_72h_aus_eigener_config(speicher):
    # Trocken bis weit nach JETZT; der Watchdog-Throttle (24 h) darf nicht gelten.
    _reihe(speicher, "hecke", LEAD_HECKE, JETZT - timedelta(hours=14),
           JETZT + timedelta(hours=80), 15.0)
    sender = _SenderAttrappe()
    job = _job(sender, speicher)
    assert _tick(job, JETZT) == 1
    assert _tick(job, JETZT + timedelta(hours=24)) == 0
    assert _tick(job, JETZT + timedelta(hours=73)) == 1
    assert len(sender.gesendet) == 2


def test_d2_quittung_unterdrueckt_trockenwarnung(speicher):
    _trocken_hecke(speicher)
    sender = _SenderAttrappe()
    q = WatchdogQuittung(typ="winter_trocken", ziel="hecke",
                         grund="Test", seit=JETZT.date())
    assert _tick(_job(sender, speicher, quittiert=[q])) == 0


# --- (e) nie FYTA --------------------------------------------------------

def test_e_fyta_trocken_bei_feuchtem_gardena_lead_kein_push(speicher):
    ende = JETZT - timedelta(minutes=10)
    _reihe(speicher, "hecke", LEAD_HECKE, ende - timedelta(hours=14), ende, 60.0)
    for fid in ("fyta_1", "fyta_2"):
        _reihe(speicher, "hecke", fid, ende - timedelta(hours=14), ende, 5.0,
               quelle=DatenQuelle.FYTA)
    sender = _SenderAttrappe()
    assert _tick(_job(sender, speicher)) == 0


def test_e2_ohne_lead_fyta_feucht_verhindert_warnung_nicht(speicher):
    # bambuswald hat keinen Lead: alle GARDENA-Geraete zaehlen, FYTA nie.
    ende = JETZT - timedelta(minutes=10)
    _reihe(speicher, "bambuswald", "gardena-bambus", ende - timedelta(hours=14),
           ende, 30.0)
    _reihe(speicher, "bambuswald", "fyta_9", ende - timedelta(hours=14),
           ende + timedelta(minutes=5), 80.0, quelle=DatenQuelle.FYTA)
    sender = _SenderAttrappe()
    job = _job(sender, speicher, winter=_winter(schwellen={"bambuswald": 45}))
    assert _tick(job) == 1
    assert sender.gesendet[0][1].startswith(
        "Winterpause Bambuswald: Bodenfeuchte 30 %")


def test_e3_lead_trocken_nicht_lead_feucht_meldet(speicher):
    _trocken_hecke(speicher)
    ende = JETZT - timedelta(minutes=10)
    _reihe(speicher, "hecke", "gardena-hecke-anderes", ende - timedelta(hours=14),
           ende, 60.0)
    sender = _SenderAttrappe()
    assert _tick(_job(sender, speicher)) == 1


def test_e4_ohne_lead_alle_gardena_geraete_muessen_trocken_sein(speicher):
    ende = JETZT - timedelta(minutes=10)
    _reihe(speicher, "bambuswald", "gardena-a", ende - timedelta(hours=14),
           ende, 30.0)
    _reihe(speicher, "bambuswald", "gardena-b", ende - timedelta(hours=14),
           ende, 60.0)
    sender = _SenderAttrappe()
    job = _job(sender, speicher, winter=_winter(schwellen={"bambuswald": 45}))
    assert _tick(job) == 0


# --- (f) Magerwiese nur Ausfall ------------------------------------------

def test_f_magerwiese_null_keine_trockenwarnung(speicher):
    ende = JETZT - timedelta(minutes=10)
    _reihe(speicher, "magerwiese", "gardena-mw", ende - timedelta(hours=14),
           ende, 0.0)
    sender = _SenderAttrappe()
    job = _job(sender, speicher,
               winter=_winter(schwellen={}, nur_ausfall=["magerwiese"]))
    assert _tick(job) == 0


# --- (g) Ausfall ---------------------------------------------------------

def test_g_ausfall_ueber_48h_push(speicher):
    letzte = JETZT - timedelta(hours=50)
    _reihe(speicher, "magerwiese", "gardena-mw", letzte - timedelta(hours=5),
           letzte, 40.0)
    sender = _SenderAttrappe()
    job = _job(sender, speicher,
               winter=_winter(schwellen={}, nur_ausfall=["magerwiese"]))
    assert _tick(job) == 1
    assert sender.gesendet[0][1] == (
        "Winterpause: Magerwiese liefert seit 50 h keinen Messwert "
        "(letzter 13.01. 10:00). Sensor/Gateway prüfen."
    )
    # Drossel 72 h auch hier, eigener Typ.
    assert _tick(job, JETZT + timedelta(hours=24)) == 0


def test_g2_ausfall_47h_kein_push_und_fyta_verdeckt_lead_ausfall_nicht(speicher):
    _reihe(speicher, "magerwiese", "gardena-mw", JETZT - timedelta(hours=50),
           JETZT - timedelta(hours=47), 40.0)
    # Hecke: Lead 50 h stumm, FYTA frisch -> Ausfall muss trotzdem melden.
    _reihe(speicher, "hecke", LEAD_HECKE, JETZT - timedelta(hours=52),
           JETZT - timedelta(hours=50), 40.0)
    _reihe(speicher, "hecke", "fyta_1", JETZT - timedelta(hours=3),
           JETZT - timedelta(minutes=5), 40.0, quelle=DatenQuelle.FYTA)
    sender = _SenderAttrappe()
    job = _job(sender, speicher,
               winter=_winter(schwellen={"hecke": 22}, nur_ausfall=["magerwiese"]))
    assert _tick(job) == 1
    assert sender.gesendet[0][1].startswith("Winterpause: Hecke liefert seit 50 h")


# --- (h) Wochenbericht ---------------------------------------------------

def _gesamt(winter=None, sperrfenster=None) -> GesamtKonfig:
    kw = {}
    if winter is not None:
        kw["winter_trockenwarnung"] = winter
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=_zonen(),
        wetter=WetterKonfig(standorte=[
            WetterStandortKonfig(id="s", breite=52.5, laenge=13.4),
        ]),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        automatik_sperrfenster=sperrfenster or [],
        **kw,
    )


def _bericht_daten(speicher):
    _trocken_hecke(speicher, ende=JETZT - timedelta(minutes=20))
    _reihe(speicher, "magerwiese", "gardena-mw", JETZT - timedelta(hours=52),
           JETZT - timedelta(hours=50), 0.0, temp=None)


WINTER_ALLE = dict(schwellen={"hecke": 22, "bambuswald": 45},
                   nur_ausfall=["magerwiese"])


def test_h_wochenbericht_ohne_sperrfenster_byte_gleich(speicher):
    _bericht_daten(speicher)
    bisher = _run(baue_wochen_report(speicher, _gesamt(), JETZT))
    ohne_fenster = _run(baue_wochen_report(
        speicher, _gesamt(winter=_winter(**WINTER_ALLE)), JETZT))
    fenster_vorbei = _run(baue_wochen_report(
        speicher, _gesamt(winter=_winter(**WINTER_ALLE), sperrfenster=SPERRE),
        datetime(2027, 4, 4, 20, 0)))
    bisher_april = _run(baue_wochen_report(
        speicher, _gesamt(), datetime(2027, 4, 4, 20, 0)))
    assert ohne_fenster == bisher
    assert fenster_vorbei == bisher_april
    assert "Winterpause" not in bisher


def test_h2_wochenbericht_mit_sperrfenster_enthaelt_zeilen(speicher):
    _bericht_daten(speicher)
    bisher = _run(baue_wochen_report(speicher, _gesamt(), JETZT))
    text = _run(baue_wochen_report(
        speicher, _gesamt(winter=_winter(**WINTER_ALLE), sperrfenster=SPERRE),
        JETZT))
    assert text == bisher + (
        "\n\nGarten (Winterpause):"
        "\n  Hecke: 15 %, Boden 4 °C, Messwert vor 0 h"
        "\n  Bambuswald: keine Daten"
        "\n  Magerwiese: kein Messwert seit 50 h"
    )


# --- Config --------------------------------------------------------------

def test_config_block_kommt_aus_der_yaml(tmp_path):
    """Config-Whitelist-Drift: `lade_konfig` muss den Block durchreichen."""
    from bewaesserung.konfig import lade_konfig
    pfad = tmp_path / "k.yaml"
    pfad.write_text(
        "zonen: []\nwetter:\n  breite: 52.52\n  laenge: 13.40\n"
        "winter_trockenwarnung:\n  aktiv: true\n  throttle_stunden: 5\n"
    )
    winter = lade_konfig(pfad).winter_trockenwarnung
    assert winter.aktiv is True and winter.throttle_stunden == 5


def test_config_default_aus_und_tippfehler_scheitert():
    assert _gesamt().winter_trockenwarnung.aktiv is False
    with pytest.raises(ValueError, match="unbekannte Zone"):
        _gesamt(winter=_winter(schwellen={"heckee": 22}))
    with pytest.raises(ValueError, match="zugleich"):
        _winter(schwellen={"hecke": 22}, nur_ausfall=["hecke"])
