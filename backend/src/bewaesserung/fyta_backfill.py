"""FYTA-Backfill — Historische Daten per API nachziehen.

Fuellt die Luecke zwischen CSV-Import (endet Feb 2026) und Live-Polling (ab April 2026).
Iteriert Tag fuer Tag und holt alle Messungen pro Pflanze.

Aufruf: python -m bewaesserung.fyta_backfill --von 2026-02-22 --bis 2026-04-06 [--db PFAD]
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import structlog

from bewaesserung.fyta_client import parse_fyta_zeitstempel
from bewaesserung.fyta_import import STANDARD_DB, _existiert_bereits
from bewaesserung.konfig import lade_konfig
from bewaesserung.modelle import DatenQuelle, SensorMessung
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()


def _lade_pflanzen_map() -> tuple[str, dict[int, str]]:
    """Laedt Pflanzen-Mapping und API-URL aus config/default.yaml."""
    konfig = lade_konfig()
    if not konfig.fyta:
        raise RuntimeError("Keine FYTA-Konfiguration in default.yaml gefunden")
    api_url = konfig.fyta.api_url
    pflanzen_map = {p.fyta_id: p.zone_id for p in konfig.fyta.pflanzen}
    return api_url, pflanzen_map


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _safe_float(wert) -> float | None:
    if wert is None:
        return None
    try:
        return float(wert)
    except (ValueError, TypeError):
        return None


async def hole_tag(
    client: httpx.AsyncClient,
    token: str,
    tag: date,
    api_url: str,
    pflanzen_map: dict[int, str],
) -> list[SensorMessung]:
    """Holt alle Messungen fuer einen einzelnen Tag."""
    tag_str = tag.isoformat()
    payload = {
        "userPlantIds": list(pflanzen_map.keys()),
        "scanFromDate": tag_str,
        "scanToDate": tag_str,
    }

    antwort = await client.post(
        f"{api_url}/user-plant/list-measurements",
        headers=_headers(token),
        json=payload,
    )
    antwort.raise_for_status()
    daten = antwort.json()

    messungen: list[SensorMessung] = []

    for pflanze in daten.get("user_plants", []):
        plant_id = pflanze.get("user_plant_id") or pflanze.get("id")
        zone_id = pflanzen_map.get(plant_id)
        if not zone_id:
            continue

        for eintrag in pflanze.get("measurements", []):
            try:
                # T-0176: UTC-konvertiert
                zeitstempel = parse_fyta_zeitstempel(
                    eintrag.get("date_utc", f"{tag_str}T00:00:00")
                )
            except (ValueError, TypeError):
                continue

            messungen.append(SensorMessung(
                zeitstempel=zeitstempel,
                zone_id=zone_id,
                geraet_id=f"fyta_{plant_id}",
                boden_feuchte=_safe_float(eintrag.get("soil_moisture")),
                boden_temperatur=_safe_float(eintrag.get("temperature")),
                licht=_safe_float(eintrag.get("light")),
                boden_fruchtbarkeit=_safe_float(eintrag.get("soil_fertility")),
                quelle=DatenQuelle.FYTA,
            ))

    return messungen


async def _hole_tag_mit_auth_retry(
    client: httpx.AsyncClient,
    fyta_client,
    tag: date,
    api_url: str,
    pflanzen_map: dict[int, str],
):
    """T-0167b: Wrapper um `hole_tag` mit T-0072-konformer 401/403-
    Behandlung.

    Verhalten:
    - Token frisch aus `fyta_client._token` lesen (kein lokales Caching
      ueber den Loop hinweg, sonst Race Condition mit parallelem
      Polling-Loop, der den Token mit refreshen kann).
    - Bei 401/403 Token-Cache invalidieren + `_stelle_token_sicher`
      aufrufen (T-0072 Lock-serialisiert) + einmal retry.
    - Bei Erfolg returnt list[SensorMessung] (wie hole_tag).
    """
    payload = {
        "userPlantIds": list(pflanzen_map.keys()),
        "scanFromDate": tag.isoformat(),
        "scanToDate": tag.isoformat(),
    }
    url = f"{api_url}/user-plant/list-measurements"

    async def _post():
        return await client.post(url, headers=_headers(fyta_client._token), json=payload)

    antwort = await _post()
    if antwort.status_code in (401, 403):
        logger.info("backfill.lueckenfuellung.token_reauth", tag=tag.isoformat())
        # Token-Cache invalidieren via FytaClient — _stelle_token_sicher
        # macht dann den Login (lock-serialisiert mit dem parallelen
        # Polling-Loop).
        fyta_client._token_ablauf = datetime.now() - timedelta(seconds=1)
        if not await fyta_client._stelle_token_sicher():
            antwort.raise_for_status()  # raise mit klarer Meldung
        antwort = await _post()
    antwort.raise_for_status()

    daten = antwort.json()
    messungen: list[SensorMessung] = []
    for pflanze in daten.get("user_plants", []):
        plant_id = pflanze.get("user_plant_id") or pflanze.get("id")
        zone_id = pflanzen_map.get(plant_id)
        if not zone_id:
            continue
        for eintrag in pflanze.get("measurements", []):
            try:
                # T-0176: UTC-konvertiert
                zeitstempel = parse_fyta_zeitstempel(
                    eintrag.get("date_utc", f"{tag.isoformat()}T00:00:00")
                )
            except (ValueError, TypeError):
                continue
            messungen.append(SensorMessung(
                zeitstempel=zeitstempel,
                zone_id=zone_id,
                geraet_id=f"fyta_{plant_id}",
                boden_feuchte=_safe_float(eintrag.get("soil_moisture")),
                boden_temperatur=_safe_float(eintrag.get("temperature")),
                licht=_safe_float(eintrag.get("light")),
                boden_fruchtbarkeit=_safe_float(eintrag.get("soil_fertility")),
                quelle=DatenQuelle.FYTA,
            ))
    return messungen


async def backfill_lueckenfuellung(
    speicher: Speicher,
    fyta_client,  # FytaClient — runtime-Type-Hint vermeidet Zirkel-Import
    tage_zurueck: int = 7,
) -> tuple[int, int]:
    """T-0167: Service-Start-Hook fuer FYTA-Lueckenfuellung.

    Fuellt die letzten `tage_zurueck` Tage Sensor-Daten nach. Soll beim
    Backend-Start im Hintergrund laufen, damit Restart-Luecken (z.B.
    durch DB-Lock-Konflikte oder Auth-Issues) nicht zu permanenten
    Datenluecken werden.

    Wichtig — verwendet die BESTEHENDE `speicher`- und `fyta_client`-
    Instanz, nicht eigene. Damit kein Single-Writer-Lock-Konflikt mit
    dem laufenden Backend-Polling (siehe T-0166-Erkenntnis: paralleler
    CLI-Backfill kollidierte mit live Backend).

    Token wird via `fyta_client._stelle_token_sicher` aus dem Cache
    geholt (T-0072 Auto-Refresh). Kein env-Token noetig.

    Returns: (importiert, duplikate). Niemals crashen — Fehler werden
    geloggt und das Polling laeuft danach normal weiter.
    """
    if not fyta_client._pflanzen_map:
        logger.info("backfill.lueckenfuellung.keine_pflanzen")
        return 0, 0

    # T-0072-Auth via FytaClient
    if not await fyta_client._stelle_token_sicher():
        logger.warning("backfill.lueckenfuellung.kein_token")
        return 0, 0

    api_url = fyta_client._konfig.api_url
    pflanzen_map = {
        fid: konfig.zone_id
        for fid, konfig in fyta_client._pflanzen_map.items()
    }
    bis = date.today()
    von = bis - timedelta(days=tage_zurueck)
    logger.info(
        "backfill.lueckenfuellung.start",
        von=von.isoformat(), bis=bis.isoformat(),
        tage=tage_zurueck, pflanzen=len(pflanzen_map),
    )

    importiert_total = 0
    duplikate_total = 0
    tag = von
    abgeschlossen_normal = False
    # T-0167c: try/finally garantiert das `fertig`-Log auch bei
    # ungeloggten Exceptions oder Cancellation. User-Bug-Befund 10.05.:
    # Hook hatte fertig-Log nie produziert -> Diagnose unmoeglich.
    try:
      async with httpx.AsyncClient(timeout=30.0) as client:
        while tag <= bis:
            # T-0167b: Token IMMER frisch aus fyta_client lesen (kein
            # lokales Caching), und bei 401/403 einmal Auth-Retry.
            # Andernfalls Race Condition: paralleler Polling-Loop kann
            # den Token zwischenzeitlich refreshen, der Backfill nutzt
            # aber weiter den alten Wert -> ganzer Range scheitert.
            try:
                messungen = await _hole_tag_mit_auth_retry(
                    client, fyta_client, tag, api_url, pflanzen_map,
                )
            except Exception as exc:
                logger.warning(
                    "backfill.lueckenfuellung.tag_fehlgeschlagen",
                    tag=tag.isoformat(), fehler=str(exc),
                )
                tag += timedelta(days=1)
                continue

            tag_importiert = 0
            tag_duplikate = 0
            for messung in messungen:
                try:
                    if await _existiert_bereits(speicher, messung):
                        tag_duplikate += 1
                        continue
                    await speicher.speichere_messung(messung)
                    tag_importiert += 1
                except Exception:
                    # Niemals den Service-Start crashen lassen.
                    logger.exception(
                        "backfill.lueckenfuellung.persist_fehler",
                        tag=tag.isoformat(),
                    )
                    continue
            importiert_total += tag_importiert
            duplikate_total += tag_duplikate
            # T-0167c: pro-Tag-Log, damit der Fortschritt sichtbar ist
            # und ggf. erkennbar wo der Hook haengt/abbricht.
            logger.info(
                "backfill.lueckenfuellung.tag_fertig",
                tag=tag.isoformat(),
                importiert=tag_importiert,
                duplikate=tag_duplikate,
                gesamt_importiert=importiert_total,
            )
            tag += timedelta(days=1)
      abgeschlossen_normal = True
    finally:
        # T-0167c: Pflicht-Log am Ende, damit der User immer sieht was
        # passiert ist — auch bei Exception/Cancellation. Plus print()
        # garantiert die Anzeige auf der CLI-Console (User-Befund 10.05.:
        # `fertig`-Log war im structlog-Output nie zu sehen).
        status = "fertig" if abgeschlossen_normal else "abgebrochen"
        logger.info(
            f"backfill.lueckenfuellung.{status}",
            tage=tage_zurueck,
            importiert=importiert_total,
            duplikate=duplikate_total,
        )
        print(
            f"[Backfill-Hook] {status}: "
            f"{importiert_total} importiert, {duplikate_total} duplikate, "
            f"{tage_zurueck} Tage",
            flush=True,
        )
    return importiert_total, duplikate_total


async def backfill(von: date, bis: date, db_pfad: Path) -> None:
    """Hauptfunktion: iteriert Tag fuer Tag und fuellt die DB."""
    api_url, pflanzen_map = _lade_pflanzen_map()
    token = os.environ.get("FYTA_ACCESS_TOKEN", "")
    if not token:
        print("FEHLER: FYTA_ACCESS_TOKEN nicht gesetzt.")
        return

    speicher = Speicher(str(db_pfad))
    await speicher.verbinden()

    gesamt_importiert = 0
    gesamt_duplikate = 0
    gesamt_tage = 0
    tag = von

    async with httpx.AsyncClient(timeout=30.0) as client:
        while tag <= bis:
            try:
                messungen = await hole_tag(client, token, tag, api_url, pflanzen_map)
            except httpx.HTTPError as exc:
                logger.warning("backfill.tag_fehlgeschlagen", tag=tag.isoformat(), fehler=str(exc))
                tag += timedelta(days=1)
                continue

            importiert = 0
            duplikate = 0

            for messung in messungen:
                if await _existiert_bereits(speicher, messung):
                    duplikate += 1
                    continue
                await speicher.speichere_messung(messung)
                importiert += 1

            gesamt_importiert += importiert
            gesamt_duplikate += duplikate
            gesamt_tage += 1

            logger.info(
                "backfill.tag_fertig",
                tag=tag.isoformat(),
                messungen=len(messungen),
                importiert=importiert,
                duplikate=duplikate,
            )

            tag += timedelta(days=1)

    await speicher.schliessen()

    logger.info(
        "backfill.fertig",
        tage=gesamt_tage,
        importiert=gesamt_importiert,
        duplikate=gesamt_duplikate,
        von=von.isoformat(),
        bis=bis.isoformat(),
    )
    print(f"Fertig: {gesamt_importiert} Messungen importiert, {gesamt_duplikate} Duplikate uebersprungen ({gesamt_tage} Tage)")


def main():
    parser = argparse.ArgumentParser(description="FYTA-Backfill: Historische Daten per API nachziehen")
    parser.add_argument("--von", type=date.fromisoformat, required=True, help="Startdatum (YYYY-MM-DD)")
    parser.add_argument("--bis", type=date.fromisoformat, required=True, help="Enddatum (YYYY-MM-DD)")
    parser.add_argument("--db", type=Path, default=STANDARD_DB, help="Pfad zur SQLite-DB")
    args = parser.parse_args()

    if args.von > args.bis:
        print("FEHLER: --von muss vor --bis liegen.")
        return

    tage = (args.bis - args.von).days + 1
    print(f"FYTA-Backfill: {args.von} bis {args.bis} ({tage} Tage)")
    asyncio.run(backfill(args.von, args.bis, args.db))


if __name__ == "__main__":
    main()
