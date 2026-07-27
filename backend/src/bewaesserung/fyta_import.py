"""Einmaliger Import von FYTA-CSV-Historien in die SQLite-Datenbank.

Liest fyta_export_full_latest.csv und schreibt SensorMessung-Eintraege.
Dedupliziert gegen bestehende Eintraege (zone_id + zeitstempel).

Aufruf: python -m bewaesserung.fyta_import [--csv PFAD] [--db PFAD]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from pathlib import Path

import structlog

from bewaesserung.fyta_client import parse_fyta_zeitstempel
from bewaesserung.konfig import lade_konfig
from bewaesserung.modelle import DatenQuelle, SensorMessung
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

# Standard-Pfade
STANDARD_CSV = Path.home() / "src" / "FYTA" / "exports" / "fyta_export_full_latest.csv"
STANDARD_DB = Path(__file__).resolve().parent.parent.parent / "daten" / "bewaesserung.db"

# Mapping: FYTA plant_name -> zone_id
# Wird auch in config/default.yaml gepflegt
PFLANZEN_MAP: dict[int, str] = {
    # Wird aus Config oder CLI-Argument geladen
}


def _parse_zeile(zeile: dict, pflanzen_map: dict[int, str]) -> SensorMessung | None:
    """Wandelt eine CSV-Zeile in eine SensorMessung um."""
    try:
        plant_id = int(zeile["user_plant_id"])
    except (ValueError, KeyError):
        return None

    zone_id = pflanzen_map.get(plant_id)
    if not zone_id:
        return None

    try:
        # F18/T-0176: `date_utc` ist naiv-UTC -> ueber den Helper in lokale
        # Zeit wandeln (sonst 2h zu frueh im Sommer + Cross-Source-Duplikate
        # gegen den API-Backfill, der parse_fyta_zeitstempel nutzt).
        zeitstempel = parse_fyta_zeitstempel(zeile["date_utc"])
    except (ValueError, KeyError):
        return None

    def safe_float(key: str) -> float | None:
        val = zeile.get(key, "")
        if val == "" or val is None:
            return None
        try:
            return float(val)
        except ValueError:
            return None

    return SensorMessung(
        zeitstempel=zeitstempel,
        zone_id=zone_id,
        geraet_id=f"fyta_{plant_id}",
        boden_feuchte=safe_float("soil_moisture"),
        boden_temperatur=safe_float("temperature"),
        licht=safe_float("light"),
        boden_fruchtbarkeit=safe_float("soil_fertility"),
        quelle=DatenQuelle.FYTA,
    )


async def importiere(
    csv_pfad: Path,
    db_pfad: Path,
    pflanzen_map: dict[int, str],
) -> int:
    """Importiert CSV-Daten in die Datenbank. Gibt Anzahl importierter Zeilen zurueck."""
    speicher = Speicher(str(db_pfad))
    await speicher.verbinden()

    importiert = 0
    uebersprungen = 0
    duplikate = 0

    with open(csv_pfad, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for zeile in reader:
            messung = _parse_zeile(zeile, pflanzen_map)
            if messung is None:
                uebersprungen += 1
                continue

            # Deduplizierung: pruefen ob (zone_id, zeitstempel, geraet_id) existiert
            if await _existiert_bereits(speicher, messung):
                duplikate += 1
                continue

            await speicher.speichere_messung(messung)
            importiert += 1

            if importiert % 1000 == 0:
                logger.info("fyta_import.fortschritt", importiert=importiert)

    await speicher.schliessen()

    logger.info(
        "fyta_import.fertig",
        importiert=importiert,
        duplikate=duplikate,
        uebersprungen=uebersprungen,
        csv=str(csv_pfad),
    )
    return importiert


async def _existiert_bereits(speicher: Speicher, messung: SensorMessung) -> bool:
    """Prueft ob eine Messung mit gleicher (zone_id, zeitstempel, geraet_id) existiert."""
    assert speicher._db is not None
    async with speicher._db.execute(
        """SELECT 1 FROM sensor_messung
           WHERE zone_id = ? AND zeitstempel = ? AND geraet_id = ?
           LIMIT 1""",
        (messung.zone_id, messung.zeitstempel.isoformat(), messung.geraet_id),
    ) as cursor:
        return await cursor.fetchone() is not None


def main():
    parser = argparse.ArgumentParser(description="FYTA CSV-Historien nach SQLite importieren")
    parser.add_argument("--csv", type=Path, default=STANDARD_CSV, help="Pfad zur CSV-Datei")
    parser.add_argument("--db", type=Path, default=STANDARD_DB, help="Pfad zur SQLite-DB")
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"CSV nicht gefunden: {args.csv}")
        return

    # Pflanzen-Map aus Produktkonfiguration laden (Single Source of Truth).
    # Frueher stand hier ein hartcodierter Fallback mit den realen fyta_ids ->
    # zweite Wahrheit neben der Konfig (verstiess gegen die Single-Source-Regel)
    # und lieferte bei geaenderter Zuordnung still FALSCHE zone_ids. Ausserdem
    # verschluckte ein `except: pass` jeden Konfig-Fehler. Beides entfernt:
    # ohne Map wird abgebrochen statt auf Stale-Daten zu importieren.
    pflanzen_map: dict[int, str] = {}
    try:
        konfig = lade_konfig()
        if konfig.fyta:
            pflanzen_map = {p.fyta_id: p.zone_id for p in konfig.fyta.pflanzen}
    except Exception as exc:
        print(f"FEHLER: Konfig nicht ladbar ({type(exc).__name__}: {exc})")

    if not pflanzen_map:
        print(
            "ABBRUCH: keine FYTA-Pflanzen-Map aus der Konfig "
            "(config/default.yaml, Abschnitt `fyta.pflanzen`). Ohne die Map "
            "wuerden die Messwerte unter falschen zone_ids importiert."
        )
        return

    asyncio.run(importiere(args.csv, args.db, pflanzen_map))


if __name__ == "__main__":
    main()
