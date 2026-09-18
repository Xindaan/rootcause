"""T-0610 PERS-1: jeder Laufzeit-Schreiber in `Speicher` laeuft unter dem
T-0321-Write-Lock.

Befund (Review 09.10.2026, gemessen 14.10.): 28 Schreibmethoden umgingen den
Lock. Zwei Fehlerklassen, beide auf derselben aiosqlite-Verbindung:
- verlorener Write: ein Schreiber mit `if not _in_transaktion: commit` lief
  waehrend der `transaktion()` eines anderen Tasks, sein Statement wurde Teil
  der fremden Tx und verschwand mit deren Rollback (so ging `live_lauf_state`
  verloren);
- halbe Tx: ein Schreiber mit unbedingtem `commit()` schrieb die fremde Tx
  mitten drin fest, deren Rollback griff nur noch fuer den Rest.

Die vier State-Writes im Ventilpfad (`live_lauf_state`, `pre_soak_state`)
nehmen den Lock mit Wartezeit-Schranke: `setze_pre_soak_state` steht vor dem
Oeffnen des ersten Pulses, ein Ventilstart darf nicht unbegrenzt warten.
Laeuft die Schranke ab, schreibt die Methode ohne Lock wie vorher.
"""

import ast
import asyncio
import re
import time
from datetime import datetime

import pytest

import bewaesserung.speicher as speicher_modul
from bewaesserung.speicher import Speicher

# --- AST-Waechter -----------------------------------------------------------

_SCHREIB_SQL = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|VACUUM"
    r"|SAVEPOINT|RELEASE|ROLLBACK)\b",
    re.IGNORECASE | re.MULTILINE,
)
_SCHREIB_AUFRUFE = {"commit", "rollback", "executescript"}
_LOCK_WRAPPER = {"_mit_lock_retry", "_mit_write_lock_ventilpfad"}

# Bewusst ohne Lock-Wrapper, je mit Begruendung.
AUSGENOMMEN = {
    "transaktion": "haelt den Write-Lock selbst fuer den ganzen Block",
    "verbinden": "Schema/WAL beim Start, vor jeder Nebenlaeufigkeit",
    "_indizes_nach_migration": "Teil von verbinden()",
    "_migriere": "Teil von verbinden()",
    "_migriere_live_lauf_state_primaerschluessel": "Teil von verbinden()",
    "_live_lauf_state_pk_umbau": "Teil von verbinden()",
    "_live_lauf_state_alt_uebernehmen": "Teil von verbinden()",
}

# Nur diese duerfen den Ventilpfad-Wrapper (Lock mit Schranke, ohne Retry)
# benutzen; alle anderen nehmen `_mit_lock_retry`.
VENTILPFAD = {
    "setze_live_lauf_state": "nach dem Hardware-Oeffnen in _bewaessere_ablauf",
    "loesche_live_lauf_state": "nach dem Hardware-Schliessen in stoppe/Callback",
    "setze_pre_soak_state": "pre_soak._persistiere, erster Puls VOR bewaessere",
    "loesche_pre_soak_state": "pre_soak._entferne_persistenz",
}


def _speicher_methoden():
    quelle = open(speicher_modul.__file__, encoding="utf-8").read()
    baum = ast.parse(quelle)
    klasse = next(
        n for n in baum.body
        if isinstance(n, ast.ClassDef) and n.name == "Speicher"
    )
    return [
        m for m in klasse.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _texte(ausdruck) -> list[str]:
    """Alle String-Literale (auch f-String-Teile) in einem Ausdruck."""
    texte = []
    for n in ast.walk(ausdruck):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            texte.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            texte.append("".join(
                v.value for v in n.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            ))
    return texte


def _schreibstellen(methode) -> list[int]:
    """Zeilen, an denen die Methode schreibt: `execute`/`executemany` mit
    Schreib-SQL (Literal oder Variable, der Schreib-SQL zugewiesen wird) sowie
    commit/rollback/executescript. Gezaehlt wird die AUSFUEHRUNG, nicht die
    Stelle, an der der SQL-Text steht."""
    zuweisungen: dict[str, list[str]] = {}
    for n in ast.walk(methode):
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            ziele = n.targets if isinstance(n, ast.Assign) else [n.target]
            for z in ziele:
                if isinstance(z, ast.Name) and n.value is not None:
                    zuweisungen.setdefault(z.id, []).extend(_texte(n.value))
    zeilen = []
    for n in ast.walk(methode):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
            continue
        if n.func.attr in _SCHREIB_AUFRUFE:
            zeilen.append(n.lineno)
        elif n.func.attr in ("execute", "executemany") and n.args:
            sql = n.args[0]
            texte = (
                zuweisungen.get(sql.id, []) if isinstance(sql, ast.Name)
                else _texte(sql)
            )
            if any(_SCHREIB_SQL.search(t) for t in texte):
                zeilen.append(n.lineno)
    return zeilen


def _geschuetzte_bereiche(methode) -> dict[str, list[tuple[int, int]]]:
    """Je Wrapper die Zeilenbereiche der verschachtelten Funktionen, die als
    erstes Argument an `self.<wrapper>(...)` gehen."""
    innere = {
        n.name: (n.lineno, n.end_lineno)
        for n in ast.walk(methode)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n is not methode
    }
    bereiche: dict[str, list[tuple[int, int]]] = {}
    for n in ast.walk(methode):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in _LOCK_WRAPPER
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "self"
            and n.args
            and isinstance(n.args[0], ast.Name)
            and n.args[0].id in innere
        ):
            bereiche.setdefault(n.func.attr, []).append(innere[n.args[0].id])
    return bereiche


def _befunde():
    ungeschuetzt, falscher_wrapper, schreiber = [], [], set()
    for m in _speicher_methoden():
        stellen = _schreibstellen(m)
        if not stellen:
            continue
        schreiber.add(m.name)
        if m.name in AUSGENOMMEN:
            continue
        bereiche = _geschuetzte_bereiche(m)
        if "_mit_write_lock_ventilpfad" in bereiche and m.name not in VENTILPFAD:
            falscher_wrapper.append(m.name)
        alle = [b for liste in bereiche.values() for b in liste]
        offen = [
            z for z in stellen
            if not any(von <= z <= bis for von, bis in alle)
        ]
        if offen:
            ungeschuetzt.append(f"{m.name} (Zeilen {offen})")
    return ungeschuetzt, falscher_wrapper, schreiber


def test_jede_schreibstelle_liegt_unter_dem_write_lock():
    """Jedes Schreib-SQL und jedes commit in `Speicher` liegt in einer
    Funktion, die `_mit_lock_retry` (bzw. fuer die Ventilpfad-States
    `_mit_write_lock_ventilpfad`) ausfuehrt. Neue Methoden ohne Lock fallen
    hier auf, bevor sie in eine fremde `transaktion()` schreiben."""
    ungeschuetzt, falscher_wrapper, _ = _befunde()
    assert ungeschuetzt == [], (
        "Schreibstellen ausserhalb des T-0321-Write-Locks (T-0610 PERS-1): "
        f"{ungeschuetzt}. Rumpf in `async def _op()` legen und "
        "`await self._mit_lock_retry(_op, label=...)` zurueckgeben."
    )
    assert falscher_wrapper == [], (
        "`_mit_write_lock_ventilpfad` nur fuer die Methoden in VENTILPFAD: "
        f"{falscher_wrapper}"
    )


def test_allowlists_sind_nicht_veraltet():
    """Jeder Eintrag der beiden Listen ist noch eine Schreibmethode; die
    Ventilpfad-Methoden nutzen wirklich den Ventilpfad-Wrapper."""
    _, _, schreiber = _befunde()
    veraltet = sorted((set(AUSGENOMMEN) | set(VENTILPFAD)) - schreiber)
    assert veraltet == []
    methoden = {m.name: m for m in _speicher_methoden()}
    ohne = [
        n for n in VENTILPFAD
        if "_mit_write_lock_ventilpfad" not in _geschuetzte_bereiche(methoden[n])
    ]
    assert ohne == []


# --- Verhalten: zwei gleichzeitige Schreiber --------------------------------


class _Abbruch(Exception):
    pass


async def _schreibe_plant_optimum(sp):
    await sp.speichere_plant_optimum("zone_probe", 30.0, 60.0)


async def _lies_plant_optimum(sp) -> int:
    return len(await sp.hole_plant_optima())


async def _schreibe_zuordnung(sp):
    await sp.speichere_zuordnung("geraet_probe", "zone_probe")


async def _lies_zuordnung(sp) -> int:
    return len(await sp.hole_zuordnungen())


async def _schreibe_live_state(sp):
    await sp.setze_live_lauf_state(
        kanal=1, valve_id="v:1", geraet_id="g", zone_ids=["zone_probe"],
        dauer_sekunden=60, ausloser="manuell",
        gestartet_am=datetime(2026, 10, 14, 6, 0),
    )


async def _lies_live_state(sp) -> int:
    return len(await sp.hole_live_lauf_states())


async def _marker_in_db(sp) -> int:
    async with sp._db.execute(
        "SELECT COUNT(*) FROM pre_soak_state WHERE zone_id = 'fremde_tx'"
    ) as cur:
        return (await cur.fetchone())[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schreibe", "lies"),
    [
        # `if not _in_transaktion: commit` -> verlorener Write
        (_schreibe_plant_optimum, _lies_plant_optimum),
        # unbedingter commit -> halbe fremde Tx
        (_schreibe_zuordnung, _lies_zuordnung),
        # Ventilpfad-Wrapper (Schranke 1 s, hier nicht erreicht)
        (_schreibe_live_state, _lies_live_state),
    ],
    ids=["flag_commit", "unbedingter_commit", "ventilpfad"],
)
async def test_schreiber_waehrend_fremder_rollback_tx(tmp_path, schreibe, lies):
    """Ein Task haelt eine `transaktion()` mit Marker-Zeile und rollt zurueck,
    waehrend ein zweiter Task schreibt. Erwartet: der Write des zweiten Tasks
    steht danach in der DB, die Marker-Zeile der fremden Tx nicht."""
    sp = Speicher(str(tmp_path / "pers1.db"))
    await sp.verbinden()
    try:
        drin, weiter = asyncio.Event(), asyncio.Event()

        async def fremde_tx():
            try:
                async with sp.transaktion():
                    await sp._db.execute(
                        "INSERT INTO pre_soak_state (zone_id, kanal, "
                        "zone_ids_kanal_json, pre_soak_s, pause_s, haupt_s, "
                        "gestartet_am, phase) VALUES ('fremde_tx', 9, '[]', "
                        "1, 1, 1, '2026-10-14T06:00:00', 'probe')"
                    )
                    drin.set()
                    await weiter.wait()
                    raise _Abbruch()
            except _Abbruch:
                pass

        tx = asyncio.create_task(fremde_tx())
        await drin.wait()
        schreiber = asyncio.create_task(schreibe(sp))
        await asyncio.sleep(0.05)  # Schreiber laeuft in die offene Tx
        weiter.set()
        await tx
        await schreiber

        assert await lies(sp) == 1, "Write ging mit der fremden Tx verloren"
        assert await _marker_in_db(sp) == 0, "fremde Tx halb festgeschrieben"
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_ventilpfad_write_wartet_hoechstens_die_schranke(tmp_path):
    """Haelt ein fremder Writer den Lock laenger als die Schranke, schreibt
    der Ventilpfad-State nach der Schranke ohne Lock weiter (Verhalten vor
    PERS-1): keine Exception, kein verlorener Write, keine unbegrenzte
    Wartezeit vor dem Oeffnen eines Pre-Soak-Pulses."""
    sp = Speicher(str(tmp_path / "schranke.db"))
    await sp.verbinden()
    sp._VENTILPFAD_LOCK_WARTE_S = 0.05
    try:
        drin = asyncio.Event()

        async def lange_tx():
            async with sp.transaktion():
                drin.set()
                await asyncio.sleep(0.5)

        tx = asyncio.create_task(lange_tx())
        await drin.wait()
        t0 = time.perf_counter()
        await _schreibe_live_state(sp)
        dauer = time.perf_counter() - t0
        await tx

        assert dauer < 0.3, f"Ventilpfad-Write wartete {dauer:.3f} s"
        assert await _lies_live_state(sp) == 1
    finally:
        await sp.schliessen()
