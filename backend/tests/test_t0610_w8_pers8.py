"""T-0610 PERS-8: Neubau von `live_lauf_state` (Primaerschluessel kanal ->
(geraet_id, kanal)) ist atomar, und eine liegengebliebene
`live_lauf_state_alt` wird beim Start erkannt und zurueckgeholt.

Betrifft nur Altschemata (Restore eines alten Snapshots). Vorher liefen
RENAME und CREATE im Autocommit; ein Abbruch danach liess die Daten in
`live_lauf_state_alt` liegen, der naechste Start sah das neue Schema und
fasste sie nie wieder an. Testkoerper: temporaere DB-Dateien.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import aiosqlite
import pytest

from bewaesserung.speicher import Speicher

ALT_SCHEMA = """CREATE TABLE live_lauf_state (
    kanal INTEGER PRIMARY KEY,
    valve_id TEXT,
    geraet_id TEXT NOT NULL,
    zone_ids_json TEXT NOT NULL,
    dauer_sekunden INTEGER NOT NULL,
    ausloser TEXT NOT NULL,
    gestartet_am TEXT NOT NULL
)"""


def _alt_db(
    pfad: str, tabelle: str = "live_lauf_state",
    zeilen: tuple[tuple[int, str], ...] = ((2, "zone-2"),),
) -> None:
    """DB mit dem Schema vor T-0203-Folge und laufenden States (kanal, zone)."""
    con = sqlite3.connect(pfad)
    con.execute(ALT_SCHEMA.replace("live_lauf_state", tabelle, 1))
    for kanal, zone in zeilen:
        con.execute(
            f"INSERT INTO {tabelle} VALUES (?, ?, 'dswc-1', ?, 1800, "
            "'manuell', ?)",
            (kanal, f"valve-{kanal}", f'["{zone}"]',
             (datetime.now() - timedelta(minutes=5)).isoformat()),
        )
    con.commit()
    con.close()


def _tabellen(pfad: str) -> set[str]:
    con = sqlite3.connect(pfad)
    try:
        return {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        con.close()


async def _states(pfad: str) -> list[dict]:
    sp = Speicher(pfad)
    await sp.verbinden()
    try:
        return await sp.hole_live_lauf_states()
    finally:
        await sp.schliessen()


async def test_pers8_abbruch_im_neubau_naechster_start_konsistent(
    tmp_path, monkeypatch,
):
    """Abbruch nach RENAME und CREATE, beim Kopieren der Zeilen. Der naechste
    Start muss den State noch haben und darf keine `_alt`-Tabelle behalten."""
    pfad = str(tmp_path / "pers8_abbruch.db")
    _alt_db(pfad)

    original = aiosqlite.Connection.execute

    def _execute(self, sql, *args, **kwargs):
        if " ".join(str(sql).split()).startswith(
            "INSERT OR REPLACE INTO live_lauf_state ("
        ) and "live_lauf_state_alt" in str(sql):
            raise sqlite3.OperationalError("Abbruch im Neubau (Test)")
        return original(self, sql, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "execute", _execute)
    sp = Speicher(pfad)
    with pytest.raises(sqlite3.OperationalError, match="Abbruch im Neubau"):
        await sp.verbinden()
    monkeypatch.setattr(aiosqlite.Connection, "execute", original)
    try:
        # Atomar: nach dem Abbruch ist nichts vom Neubau festgeschrieben,
        # die Alttabelle steht unveraendert (Primaerschluessel kanal).
        assert "live_lauf_state_alt" not in _tabellen(pfad)
        con = sqlite3.connect(pfad)
        try:
            pk = [r[1] for r in con.execute("PRAGMA table_info(live_lauf_state)")
                  if r[5]]
        finally:
            con.close()
        assert pk == ["kanal"]
        # Die abgebrochene, noch offene Verbindung haelt keine Schreibsperre
        # (Savepoint zurueckgerollt): ein anderer Schreiber kommt sofort dran.
        fremd = sqlite3.connect(pfad, timeout=0.5)
        try:
            fremd.execute("BEGIN IMMEDIATE")
            fremd.execute("ROLLBACK")
        finally:
            fremd.close()
    finally:
        await sp.schliessen()
    states = await _states(pfad)
    assert [(s["geraet_id"], s["kanal"]) for s in states] == [("dswc-1", 2)]
    assert "live_lauf_state_alt" not in _tabellen(pfad)


async def test_pers8_liegengebliebene_alt_tabelle_wird_zurueckgeholt(tmp_path):
    """Zustand nach einem Abbruch unter dem alten Code: neues Schema (vom
    naechsten Start angelegt, mit neueren States auf Kanal 1 und 2) plus
    `live_lauf_state_alt` mit alten States auf Kanal 2 und 3. Der Start holt
    Kanal 3 zurueck, laesst die neueren States stehen (Kanal 2 wird NICHT
    vom alten ueberschrieben) und raeumt `_alt` weg."""
    pfad = str(tmp_path / "pers8_rest.db")
    sp = Speicher(pfad)
    await sp.verbinden()
    try:
        for kanal, zone in ((1, "zone-1"), (2, "zone-2-neu")):
            await sp.setze_live_lauf_state(
                kanal=kanal, valve_id=f"valve-{kanal}", geraet_id="dswc-1",
                zone_ids=[zone], dauer_sekunden=600, ausloser="manuell",
                gestartet_am=datetime.now(),
            )
    finally:
        await sp.schliessen()
    _alt_db(
        pfad, tabelle="live_lauf_state_alt",
        zeilen=((2, "zone-2-alt"), (3, "zone-3")),
    )

    states = await _states(pfad)

    assert sorted((s["kanal"], s["zone_ids"]) for s in states) == [
        (1, ["zone-1"]), (2, ["zone-2-neu"]), (3, ["zone-3"]),
    ]
    assert "live_lauf_state_alt" not in _tabellen(pfad)


async def test_pers8_neubau_behaelt_lauf_gruppe_und_phase(tmp_path):
    """Nach dem Neubau muss die Tabelle die T-0537-Spalten haben: vorher
    fehlten `lauf_gruppe`/`phase` bis zum naechsten Start, und jedes
    `setze_live_lauf_state` scheiterte (State nicht persistiert)."""
    pfad = str(tmp_path / "pers8_spalten.db")
    _alt_db(pfad)
    sp = Speicher(pfad)
    await sp.verbinden()
    try:
        await sp.setze_live_lauf_state(
            kanal=3, valve_id="valve-3", geraet_id="dswc-1",
            zone_ids=["zone-3"], dauer_sekunden=600, ausloser="automatik",
            gestartet_am=datetime.now(), lauf_gruppe="presoak_zone-3_x",
            phase="haupt",
        )
        states = {s["kanal"]: s for s in await sp.hole_live_lauf_states()}
    finally:
        await sp.schliessen()
    assert sorted(states) == [2, 3]
    assert states[3]["lauf_gruppe"] == "presoak_zone-3_x"
    assert states[3]["phase"] == "haupt"
