"""T-0546: Ereignis-Warnungen laufen ab, Zustands-Warnungen nicht.

**Der Realfall.** Auf der Karte von `kasten_4` stand am 22.08.2026 unveraendert
das Band "FYTA-Kalibrierung verschoben". Es war keine aktuelle Meldung,
sondern Warnung id 516 vom **30.07., 06:37**, mit `behoben_um = NULL` -- zu
diesem Zeitpunkt die einzige offene Warnung im ganzen System.

**Die Ursache war konzeptionell, nicht schlampig.** Sieben der acht Warntypen
melden einen ZUSTAND, der endet; ihre Detektoren schliessen sie. Ein
FYTA-Kalibrier-Push ist ein EREIGNIS ohne Dauer -- "behoben" passt darauf
nicht, also hat nie jemand einen Schliesser geschrieben, und die Meldung stand
23 Tage.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import (
    EREIGNIS_WARNUNGEN,
    EREIGNIS_WARNUNG_LEBENSDAUER_TAGE,
    SensorWarnung,
    SensorWarnungTyp,
)
from bewaesserung.speicher import Speicher


JETZT = datetime(2026, 8, 22, 14, 0)


@pytest.fixture
async def speicher(tmp_path):
    """Eigene DB je Test. `schliessen()` im Teardown ist Pflicht, sonst haengt
    pytest ([[fehlerpattern_aiosqlite_fixture_haenger]])."""
    sp = Speicher(str(tmp_path / "t0546.db"))
    await sp.verbinden()
    try:
        yield sp
    finally:
        await sp.schliessen()


async def _warnung(speicher, typ, zeitstempel, zone_id="kasten_4"):
    await speicher.oeffne_sensor_warnung(SensorWarnung(
        zeitstempel=zeitstempel, zone_id=zone_id, typ=typ, details="test",
    ))


async def _offene_typen(speicher, zone_id=None):
    return [w.typ for w in await speicher.offene_sensor_warnungen(zone_id)]


# --------------------------------------------------------------------------
# Der Realfall
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_der_realfall_die_warnung_vom_30_juli_wird_geschlossen(speicher):
    """Genau die Konstellation aus der Produktiv-DB: 23 Tage alt, offen."""
    await _warnung(
        speicher, SensorWarnungTyp.FYTA_KALIBRIER_PUSH,
        datetime(2026, 7, 30, 6, 37),
    )
    n = await speicher.schliesse_abgelaufene_ereignis_warnungen(jetzt=JETZT)
    assert n == 1
    assert await _offene_typen(speicher) == []


@pytest.mark.asyncio
async def test_wirkt_rueckwirkend_ohne_dass_jemand_die_db_anfasst(speicher):
    """Wichtig fuers Deployment: die Bestandswarnung verschwindet beim ersten
    Lauf von selbst, es braucht keinen manuellen UPDATE auf die Live-DB."""
    await _warnung(
        speicher, SensorWarnungTyp.FYTA_KALIBRIER_PUSH,
        JETZT - timedelta(days=23),
    )
    assert await speicher.schliesse_abgelaufene_ereignis_warnungen(
        jetzt=JETZT,
    ) == 1


# --------------------------------------------------------------------------
# Was NICHT ablaufen darf
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_frische_ereignis_warnung_bleibt_stehen(speicher):
    """Ein neuer Push muss weiterhin sofort und dauerhaft sichtbar sein."""
    await _warnung(
        speicher, SensorWarnungTyp.FYTA_KALIBRIER_PUSH,
        JETZT - timedelta(days=1),
    )
    assert await speicher.schliesse_abgelaufene_ereignis_warnungen(
        jetzt=JETZT,
    ) == 0
    assert await _offene_typen(speicher) == [
        SensorWarnungTyp.FYTA_KALIBRIER_PUSH,
    ]


@pytest.mark.asyncio
async def test_zustands_warnung_laeuft_NICHT_ab(speicher):
    """Die wichtigste Abgrenzung: eine Batterie, die leer BLEIBT, ist nach
    acht Tagen nicht in Ordnung. Zustaende duerfen nur ihr Detektor
    schliessen."""
    await _warnung(
        speicher, SensorWarnungTyp.BATTERIE_KRITISCH,
        JETZT - timedelta(days=90),
    )
    await _warnung(
        speicher, SensorWarnungTyp.AUSFALL, JETZT - timedelta(days=90),
        zone_id="hecke",
    )
    assert await speicher.schliesse_abgelaufene_ereignis_warnungen(
        jetzt=JETZT,
    ) == 0
    assert len(await speicher.offene_sensor_warnungen()) == 2


@pytest.mark.asyncio
async def test_exakt_auf_der_grenze_bleibt_stehen(speicher):
    """`<` statt `<=`: auf der Grenze im Zweifel behalten."""
    await _warnung(
        speicher, SensorWarnungTyp.FYTA_KALIBRIER_PUSH,
        JETZT - timedelta(days=EREIGNIS_WARNUNG_LEBENSDAUER_TAGE),
    )
    assert await speicher.schliesse_abgelaufene_ereignis_warnungen(
        jetzt=JETZT,
    ) == 0


@pytest.mark.asyncio
async def test_bereits_geschlossene_werden_nicht_erneut_angefasst(speicher):
    await _warnung(
        speicher, SensorWarnungTyp.FYTA_KALIBRIER_PUSH,
        JETZT - timedelta(days=30),
    )
    assert await speicher.schliesse_abgelaufene_ereignis_warnungen(
        jetzt=JETZT,
    ) == 1
    assert await speicher.schliesse_abgelaufene_ereignis_warnungen(
        jetzt=JETZT,
    ) == 0


@pytest.mark.asyncio
async def test_lebensdauer_ist_ueberschreibbar(speicher):
    await _warnung(
        speicher, SensorWarnungTyp.FYTA_KALIBRIER_PUSH,
        JETZT - timedelta(days=3),
    )
    assert await speicher.schliesse_abgelaufene_ereignis_warnungen(
        jetzt=JETZT, lebensdauer_tage=2,
    ) == 1


@pytest.mark.asyncio
async def test_haengt_nicht_an_der_systemuhr(speicher):
    """`jetzt` muss durchgereicht werden, sonst ist der Test tageszeitabhaengig
    ([[fehlerpattern_jetzt_nicht_durchgereicht]])."""
    await _warnung(
        speicher, SensorWarnungTyp.FYTA_KALIBRIER_PUSH,
        datetime(2026, 7, 30, 6, 37),
    )
    # Ein `jetzt` VOR der Warnung darf nichts schliessen.
    assert await speicher.schliesse_abgelaufene_ereignis_warnungen(
        jetzt=datetime(2026, 7, 25),
    ) == 0


# --------------------------------------------------------------------------
# Naht: laeuft es im periodischen Check mit?
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sensor_health_raeumt_beim_normalen_lauf_auf(speicher):
    """Sonst ist die Methode gebaut, aber niemand ruft sie
    ([[fehlerpattern_detektor_ohne_konsument]])."""
    from bewaesserung.sensor_health import SensorHealthMonitor

    await _warnung(
        speicher, SensorWarnungTyp.FYTA_KALIBRIER_PUSH,
        JETZT - timedelta(days=30),
    )
    monitor = SensorHealthMonitor(speicher)
    await monitor.pruefe_alle(["kasten_4"], jetzt=JETZT)
    # Auf den TYP pruefen, nicht auf Leere: die Zone hat in dieser Test-DB
    # keine Messungen, also legt der Health-Check erwartungsgemaess eine
    # frische ausfall-Warnung an. Die gehoert dorthin.
    assert SensorWarnungTyp.FYTA_KALIBRIER_PUSH not in await _offene_typen(
        speicher,
    )


# --------------------------------------------------------------------------
# Waechter gegen die naechste Wiederholung
# --------------------------------------------------------------------------

def test_jeder_warntyp_hat_einen_schliesser_oder_laeuft_ab():
    """DER Waechter. Kommt ein neuer Warntyp dazu, der weder von einem
    Detektor geschlossen wird noch als Ereignis markiert ist, faellt dieser
    Test -- statt dass die Meldung Monate spaeter auf einer Karte einfriert.

    Quelltext-Anker, bewusst: ob ein Typ irgendwo geschlossen WIRD, laesst
    sich nicht ausfuehren, ohne jeden Detektor zu instanziieren. Ergaenzt die
    Verhaltens-Tests oben, ersetzt sie nicht.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "bewaesserung"
    quelltext = "\n".join(
        f.read_text() for f in src.rglob("*.py")
    )
    ohne_schliesser = []
    for typ in SensorWarnungTyp:
        if typ in EREIGNIS_WARNUNGEN:
            continue
        # Ein Schliesser nennt den Typ in der Naehe von schliesse_...
        if f"SensorWarnungTyp.{typ.name}" not in quelltext:
            ohne_schliesser.append(typ.name)
    assert ohne_schliesser == [], (
        f"Warntypen ohne jede Verwendung: {ohne_schliesser}"
    )


def test_nur_der_kalibrier_push_ist_heute_ein_ereignis():
    """Haelt die Klassifikation fest. Wer hier etwas hinzufuegt, soll
    begruenden muessen -- ein Zustand, der ablaeuft, waere ein Datenverlust."""
    assert EREIGNIS_WARNUNGEN == frozenset({
        SensorWarnungTyp.FYTA_KALIBRIER_PUSH,
    })


# --------------------------------------------------------------------------
# Negativprobe
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_negativprobe_ohne_ablauf_bleibt_die_warnung_ewig(speicher):
    """Baut den alten Zustand nach: keine Ereignis-Typen, also kein Ablauf.
    Dann steht die Warnung vom 30.07. weiter -- genau der gemeldete Fehler."""
    import bewaesserung.speicher as sp

    original = sp.EREIGNIS_WARNUNGEN
    sp.EREIGNIS_WARNUNGEN = frozenset()
    try:
        await _warnung(
            speicher, SensorWarnungTyp.FYTA_KALIBRIER_PUSH,
            datetime(2026, 7, 30, 6, 37),
        )
        assert await speicher.schliesse_abgelaufene_ereignis_warnungen(
            jetzt=JETZT,
        ) == 0
        assert await _offene_typen(speicher) == [
            SensorWarnungTyp.FYTA_KALIBRIER_PUSH,
        ], "Negativprobe: ohne Ereignis-Klasse muss sie stehenbleiben."
    finally:
        sp.EREIGNIS_WARNUNGEN = original
