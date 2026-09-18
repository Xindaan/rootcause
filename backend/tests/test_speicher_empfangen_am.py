"""T-0445: Ankunftszeit (`empfangen_am`) auf `sensor_messung`.

`zeitstempel` ist die MESSzeit. Ob eine Zeile zum Urteilszeitpunkt schon in
der DB stand, stand bis T-0445 nirgends -- ein Nachtrag aus dem DHS-Backfill
sieht hinterher aus wie eine 30 min alte Messung. Erst mit der Ankunftszeit
laesst sich sagen: "seit Laufbeginn ist nichts NEU eingetroffen".
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import DatenQuelle, SensorMessung
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "empfangen.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        # Ohne schliessen() haengt pytest (fehlerpattern_aiosqlite_fixture_haenger).
        _run(s.schliessen())


def _m(
    sp: Speicher, zone: str, geraet: str, ts: datetime,
    feuchte: float | None = 55.0,
):
    _run(sp.speichere_messung(SensorMessung(
        zeitstempel=ts, zone_id=zone, geraet_id=geraet,
        boden_feuchte=feuchte, boden_temperatur=15.0,
        batterie_prozent=90.0, quelle=DatenQuelle.GARDENA,
    )))


def _rohzeilen(sp: Speicher, zone: str) -> list[dict]:
    async def _op():
        async with sp._db.execute(  # type: ignore[union-attr]
            "SELECT zeitstempel, empfangen_am FROM sensor_messung "
            "WHERE zone_id = ? ORDER BY id",
            (zone,),
        ) as cursor:
            return [dict(z) for z in await cursor.fetchall()]
    return _run(_op())


def test_empfangen_am_wird_beim_insert_gesetzt(speicher):
    """Jede ueber `speichere_messung` geschriebene Zeile traegt eine Ankunft.

    Das ist die einzige Schreibstelle -- Live-WebSocket, DHS-Backfill,
    FYTA-Import und beide FYTA-Backfills laufen alle hier durch.
    """
    vor = datetime.now()
    _m(speicher, "zone_a", "sensor_1", datetime(2026, 7, 28, 5, 9))
    nach = datetime.now()

    zeilen = _rohzeilen(speicher, "zone_a")
    assert len(zeilen) == 1
    empfangen = datetime.fromisoformat(zeilen[0]["empfangen_am"])
    assert vor <= empfangen <= nach


def test_empfangen_am_ist_iso_mit_t_separator(speicher):
    """Kein SQLite `datetime('now')`.

    Zwei dokumentierte Fallen dort: der localtime-Modifier verschiebt gegen
    die Beispielstadt-naiven Zeitstempel dieser DB, und die SQLite-Form nutzt ein
    Leerzeichen statt des T-Separators -- String-Vergleiche gegen die
    uebrigen Spalten brechen dann still.
    """
    _m(speicher, "zone_a", "sensor_1", datetime(2026, 7, 28, 5, 9))
    roh = _rohzeilen(speicher, "zone_a")[0]["empfangen_am"]
    assert "T" in roh and " " not in roh


def test_empfangen_am_weicht_von_der_messzeit_ab(speicher):
    """Der Kern des Befunds: Nachtrag != Ankunft.

    Eine Zeile mit Messzeit 05:09, die erst gegen 05:40 ankommt, sieht ueber
    `zeitstempel` aus wie eine 30 min alte Messung -- war waehrend des Laufs
    aber unsichtbar.
    """
    mess_ts = datetime.now() - timedelta(minutes=31)
    _m(speicher, "zone_a", "sensor_1", mess_ts)
    roh = _rohzeilen(speicher, "zone_a")[0]
    abstand = (
        datetime.fromisoformat(roh["empfangen_am"])
        - datetime.fromisoformat(roh["zeitstempel"])
    )
    assert abstand > timedelta(minutes=30)


def test_letzte_ankunft_feuchte_liefert_die_juengste_ankunft(speicher):
    """MAX(empfangen_am), nicht MAX(zeitstempel).

    Die zweite Zeile traegt eine AELTERE Messzeit, kommt aber spaeter an
    (Backfill-Nachtrag). Gefragt ist die Ankunft.
    """
    _m(speicher, "zone_a", "sensor_1", datetime(2026, 7, 28, 5, 30))
    _m(speicher, "zone_a", "sensor_1", datetime(2026, 7, 28, 4, 9))

    zeilen = _rohzeilen(speicher, "zone_a")
    erwartet = max(datetime.fromisoformat(z["empfangen_am"]) for z in zeilen)
    assert _run(speicher.letzte_ankunft_feuchte("zone_a")) == erwartet


def test_letzte_ankunft_feuchte_ohne_messung_ist_none(speicher):
    assert _run(speicher.letzte_ankunft_feuchte("gibt_es_nicht")) is None


def test_letzte_ankunft_feuchte_ignoriert_temperatur_only_beats(speicher):
    """Gardena sendet manchmal nur `temperature` ohne `humidity`.

    Ein solcher Beat macht den Max-Stop nicht sehend -- er speist ihn nicht.
    (Memory: fehlerpattern_gardena_temperatur_only_beats.)
    """
    _m(speicher, "zone_a", "sensor_1", datetime(2026, 7, 28, 4, 0), feuchte=None)
    assert _run(speicher.letzte_ankunft_feuchte("zone_a")) is None

    _m(speicher, "zone_a", "sensor_1", datetime(2026, 7, 28, 4, 30), feuchte=55.0)
    assert _run(speicher.letzte_ankunft_feuchte("zone_a")) is not None


def test_letzte_ankunft_feuchte_ignoriert_altzeilen_ohne_ankunft(speicher):
    """Altzeilen (vor der Migration) haben `empfangen_am IS NULL`.

    "Ankunft unbekannt" darf nicht zu "gerade angekommen" werden -- sonst
    wuerde die Frische-Pruefung des Max-Stops durch reine Bestandsdaten
    aufgehoben.
    """
    async def _alt_insert():
        await speicher._db.execute(  # type: ignore[union-attr]
            "INSERT INTO sensor_messung "
            "(zeitstempel, zone_id, geraet_id, boden_feuchte, quelle, empfangen_am) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            ("2026-07-28T04:00:00", "zone_alt", "sensor_1", 55.0, "gardena"),
        )
        await speicher._db.commit()  # type: ignore[union-attr]
    _run(_alt_insert())

    assert _run(speicher.letzte_ankunft_feuchte("zone_alt")) is None


def test_letzte_ankunft_feuchte_trennt_zonen(speicher):
    _m(speicher, "zone_a", "sensor_1", datetime(2026, 7, 28, 4, 0))
    assert _run(speicher.letzte_ankunft_feuchte("zone_b")) is None
    assert _run(speicher.letzte_ankunft_feuchte("zone_a")) is not None


def test_migration_ruestet_empfangen_am_auf_altdatenbank_nach(tmp_path):
    """Bestehende DBs bekommen die Spalte per ALTER TABLE nachgereicht."""
    db_pfad = str(tmp_path / "alt.db")

    async def _lege_alte_db_an():
        import aiosqlite
        db = await aiosqlite.connect(db_pfad)
        await db.execute(
            """CREATE TABLE sensor_messung (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   zeitstempel TEXT NOT NULL,
                   zone_id TEXT NOT NULL,
                   geraet_id TEXT NOT NULL DEFAULT '',
                   boden_feuchte REAL,
                   boden_temperatur REAL,
                   umgebungs_temperatur REAL,
                   licht_intensitaet REAL,
                   batterie_prozent REAL,
                   boden_fruchtbarkeit REAL,
                   licht REAL,
                   quelle TEXT NOT NULL DEFAULT 'gardena'
               )"""
        )
        await db.execute(
            "INSERT INTO sensor_messung (zeitstempel, zone_id, boden_feuchte) "
            "VALUES ('2026-07-01T10:00:00', 'zone_alt', 44.0)"
        )
        await db.commit()
        await db.close()
    _run(_lege_alte_db_an())

    s = Speicher(db_pfad)
    _run(s.verbinden())
    try:
        # Altzeile bleibt NULL, neue Zeile bekommt eine Ankunft.
        assert _run(s.letzte_ankunft_feuchte("zone_alt")) is None
        _m(s, "zone_alt", "sensor_1", datetime(2026, 7, 28, 4, 0))
        assert _run(s.letzte_ankunft_feuchte("zone_alt")) is not None
    finally:
        _run(s.schliessen())
