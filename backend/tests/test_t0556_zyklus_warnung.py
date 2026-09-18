"""T-0556: ein kriechender Entscheidungszyklus muss auffallen.

Am 30.08.2026 lief statt zwoelf Zyklen pro Stunde EINER; die Luecken
reichten bis 143 min. Gemerkt hat das niemand, weil die Laufzeit nur eine
Zahl im INFO-Heartbeat war. Die Folge ist keine Kleinigkeit: solange der
Loop kriecht, sind ALLE Zeitstempel, die beim Verarbeiten aus
`datetime.now()` entstehen, systematisch zu spaet -- und darauf ruht die
halbe Ereignis-Erfassung. Vier Stunden Symptom-Reparatur und zwei
Phantom-Zeilen (#7200/#7201) gehen auf diese blinde Stelle zurueck.

Die Schwelle ist an gemessenen Werten gewaehlt, nicht geraten (16.09.2026,
122 Zyklen): Median 0,2-2,2 s, Maximum 25,7 s. 60 s liegt klar darueber und
meldet damit Starvation statt teurer Regelzyklen.
"""
from __future__ import annotations

import asyncio

import pytest

from bewaesserung.main import (
    ZYKLUS_WARNSCHWELLE_S,
    logge_zyklus_ende,
    warne_bei_kriechendem_zyklus,
)


def test_normaler_zyklus_warnt_nicht():
    """Negativprobe: die real gemessenen Zyklen duerfen NICHT warnen.

    Ohne diesen Fall koennte die Schwelle auf 0 stehen und jeder Zyklus
    eine Warnung erzeugen -- der Waechter waere dann sofort wieder blind,
    nur diesmal durch Rauschen statt durch Schweigen.
    """
    for laufzeit in (0.2, 2.2, 21.0, 25.7):
        assert warne_bei_kriechendem_zyklus(laufzeit, None) is False, laufzeit


def test_kriechender_zyklus_warnt():
    """Die 30.08.-Werte: 98,8 / 151,4 / 177,5 / 182,7 s."""
    for laufzeit in (98.8, 151.41, 177.49, 182.7):
        assert warne_bei_kriechendem_zyklus(laufzeit, None) is True, laufzeit


def test_schwelle_ist_exklusiv_nach_unten():
    """Genau auf der Schwelle wird gewarnt, knapp darunter nicht.

    Sonst koennte `>=` gegen `>` getauscht werden, ohne dass ein Test es
    bemerkt.
    """
    assert warne_bei_kriechendem_zyklus(ZYKLUS_WARNSCHWELLE_S - 0.01, None) is False
    assert warne_bei_kriechendem_zyklus(ZYKLUS_WARNSCHWELLE_S, None) is True


def test_schwelle_liegt_ueber_dem_teuersten_regelzyklus():
    """Die Schwelle darf nicht unter die Taktkost des Sensor-Backfills
    rutschen: der laeuft auf einem Intervall und kostet dabei IMMER 13-17 s
    (16.09. gemessen, 19 von 122 Zyklen, Minimum 13,23 s). Eine Schwelle
    darunter wuerde regulaeren Betrieb als Stoerung melden."""
    assert ZYKLUS_WARNSCHWELLE_S > 25.7


def test_phasen_werden_mitgegeben(capsys):
    """Die Warnung ohne die teuerste Phase waere wertlos -- man muesste
    danach erst im Log suchen, was den Zyklus gefressen hat.

    Geprueft wird auf STDOUT, nicht ueber `caplog`: structlog schreibt an
    der stdlib-Logging-Kette vorbei, `caplog.records` bleibt leer obwohl
    die Zeile erscheint ([[fehlerpattern_stdlib_structlog_mix]]).
    """
    assert warne_bei_kriechendem_zyklus(
        153.75, {"sensor_backfill": 153.75},
    ) is True
    ausgabe = capsys.readouterr().out
    assert "zyklus_kriecht" in ausgabe, ausgabe
    assert "sensor_backfill" in ausgabe, "die teuerste Phase fehlt"
    assert "pmset" in ausgabe, "der Hinweis auf die Erstpruefung fehlt"


def test_leise_bei_normalem_zyklus(capsys):
    """Negativprobe zur Ausgabe: unter der Schwelle darf NICHTS erscheinen."""
    assert warne_bei_kriechendem_zyklus(2.2, {"kanal_block": 2.0}) is False
    assert "zyklus_kriecht" not in capsys.readouterr().out


# --- T-0556b: Warnung und Heartbeat sind gekoppelt ---

def test_t0556b_ein_aufruf_liefert_beides(capsys):
    """Die Kopplung ist der eigentliche Schutz.

    Vorher standen Warnung und Heartbeat als zwei Zeilen im Loop; die
    Negativprobe "entferne den Warnungs-Aufruf" liess die Suite gruen. Jetzt
    kann die Warnung nicht mehr still verschwinden, ohne den Heartbeat
    mitzunehmen -- und dessen Fehlen faellt im Log sofort auf.
    """
    assert logge_zyklus_ende(
        laufzeit_s=153.75, phasen={"sensor_backfill": 153.75},
        kanaele_geprueft=2, zonen_geprueft=14,
    ) is True
    aus = capsys.readouterr().out
    assert "zyklus_kriecht" in aus, "Warnung fehlt"
    assert "entscheidungsloop.heartbeat" in aus, "Heartbeat fehlt"
    # Beide tragen die teuerste Phase.
    assert aus.count("sensor_backfill") >= 2, aus


def test_t0556b_normaler_zyklus_nur_heartbeat(capsys):
    """Negativprobe: unter der Schwelle NUR der Heartbeat, keine Warnung.

    Sonst waere die Kopplung erkauft, indem jeder Zyklus warnt.
    """
    assert logge_zyklus_ende(
        laufzeit_s=2.2, phasen={"kanal_block": 2.0}, kanaele_geprueft=2,
    ) is False
    aus = capsys.readouterr().out
    assert "entscheidungsloop.heartbeat" in aus
    assert "zyklus_kriecht" not in aus


def test_t0556b_heartbeat_felder_kommen_durch(capsys):
    """Der Heartbeat darf durch die Kopplung keine Felder verlieren -- sonst
    waere die Diagnose von T-0556 selbst nicht mehr moeglich."""
    logge_zyklus_ende(
        laufzeit_s=1.0, phasen=None, kanaele_geprueft=2, zonen_geprueft=14,
        sensor_health_checks=3, naechster_lauf="2026-09-16 10:30",
    )
    aus = capsys.readouterr().out
    for feld in ("kanaele_geprueft", "zonen_geprueft", "sensor_health_checks",
                 "naechster_lauf", "laufzeit_s"):
        assert feld in aus, feld


# --- T-0556b: die VERDRAHTUNG im Entscheidungsloop ---

class _AsyncAttrappe:
    """Jedes Attribut ist eine async-Funktion mit festem Rueckgabewert.

    Bewusst keine echte DB und kein Hardware-Client: der Test fuehrt den
    Produktions-Loop aus, darf dabei aber nichts anfassen.
    """

    def __init__(self, rueck=None):
        self._r = rueck

    def __getattr__(self, name):
        async def _f(*a, **kw):
            return self._r
        return _f


def _motor_attrappe():
    from unittest.mock import MagicMock
    m = MagicMock()

    async def _leer(*a, **kw):
        return None

    async def _liste(*a, **kw):
        return []

    async def _paar(*a, **kw):
        return (None, "")

    m.pruefe_alle_zonen = _liste
    m.pruefe_zone = _leer
    m.prognostiziere_bewaesserung = _paar
    for name in ("kanal_aktiv_bewaesserung", "hole_offene_ventil_kanaele",
                 "pruefe_max_stop", "pruefe_min_start"):
        setattr(m, name, _leer)
    return m


@pytest.mark.asyncio
async def test_t0556b_loop_schreibt_den_heartbeat(capsys, monkeypatch):
    """Die NAHT, endlich als Verhalten geprueft.

    Vorher liess sich der Aufruf aus dem Entscheidungsloop entfernen, ohne
    dass ein Test es merkte -- die Negativprobe blieb gruen. Dieser Test
    faehrt den echten Loop eine Runde und prueft, dass der Heartbeat
    herauskommt. Weil Warnung und Heartbeat seit T-0556b in EINEM Aufruf
    stecken, deckt er beide ab.

    Moeglich wurde er erst dadurch, dass der 5-Sekunden-Vorlauf eine
    Konstante ist (`ERSTE_PRUEFUNG_VERZOEGERUNG_S`).
    """
    from unittest.mock import MagicMock

    from bewaesserung import main as M
    from bewaesserung.modelle import (
        GardenaKonfig,
        GesamtKonfig,
        SpeicherKonfig,
        WetterKonfig,
        WetterStandortKonfig,
        ZonenKonfig,
        ZonenModus,
    )

    monkeypatch.setattr(M, "ERSTE_PRUEFUNG_VERZOEGERUNG_S", 0.01)
    konfig = GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[ZonenKonfig(zone_id="z1", name="Z1", modus=ZonenModus.MONITORING)],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.5, laenge=13.4)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
    )
    stop = asyncio.Event()

    async def _stopper():
        await asyncio.sleep(0.4)
        stop.set()

    aufgabe = asyncio.create_task(_stopper())
    await asyncio.wait_for(
        M._entscheidungsloop(
            motor=_motor_attrappe(), konfig=konfig,
            benachrichtiger=MagicMock(), verarbeiter=MagicMock(),
            stop_event=stop, speicher=_AsyncAttrappe(),
        ),
        timeout=15,
    )
    await aufgabe

    aus = capsys.readouterr().out
    assert "entscheidungsloop.heartbeat" in aus, (
        "Der Loop hat keinen Heartbeat geschrieben -- die Verdrahtung von "
        "`logge_zyklus_ende` fehlt"
    )
    assert "entscheidungsloop.fehler" not in aus, (
        f"Der Testlauf hat eine Ausnahme provoziert:\n{aus[-1200:]}"
    )
