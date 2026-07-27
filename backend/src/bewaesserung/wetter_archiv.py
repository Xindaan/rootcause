"""Open-Meteo Archive-API Client fuer historische Regen-/ET0-Daten.

**Wofuer**: Unsere Wetter-Vorhersagen werden als Forecast gespeichert. Wenn
der Forecast 5 mm Regen sagte, die Realitaet aber nur 0.3 mm lieferte,
kennen wir die Abweichung nicht. Diese Ground-Truth brauchen wir fuer
fundierte T-0025-Diagnose und fuer ML-Evaluation (T-0047).

**Design**: Eigene Tabelle `wetter_archiv` statt Mutieren der Forecast-Zeilen
— Forecast-Historie bleibt unveraendert, Real-Werte sind separat. Ein Job
laeuft einmal pro Tag und fuellt Luecken bis `jetzt - puffer_tage`.

**Puffer**: Open-Meteo Archive (ERA5) hat typisch 3-5 Tage Verzoegerung.
Standard `PUFFER_TAGE = 5` — wir holen nichts innerhalb der letzten 5 Tage,
um nicht preliminaere Daten mit spaeteren Korrekturen zu ueberschreiben.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import httpx
import structlog

from bewaesserung.modelle import WetterArchivStunde, WetterKonfig
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY_FIELDS = ("temperature_2m", "precipitation", "et0_fao_evapotranspiration")
# T-0045: Eigener Feldsatz fuer Luftfeuchte-Backfill — getrennt vom Haupt-Archiv,
# weil wir nur die `luftfeuchte`-Spalte in wetter_vorhersage fuellen, nicht die
# wetter_archiv-Tabelle mutieren.
HUMIDITY_FIELDS = ("relative_humidity_2m",)

PUFFER_TAGE = 5                    # Nicht innerhalb dieses Fensters abrufen
MAX_BACKFILL_TAGE = 60             # Auf einmal hoechstens 60 Tage holen
AKTUALISIERUNGS_INTERVALL_STD = 24 # Job-Cadence
HTTP_TIMEOUT_S = 30.0


class WetterArchivClient:
    """Ruft stuendliche Archiv-Daten fuer einen Standort."""

    def __init__(self, breite: float, laenge: float, standort_id: str):
        self._breite = breite
        self._laenge = laenge
        self._standort_id = standort_id

    @property
    def standort_id(self) -> str:
        return self._standort_id

    async def hole_archiv(
        self, von: date, bis: date,
    ) -> list[WetterArchivStunde]:
        """Holt Archiv-Daten im Bereich [von, bis], inklusive.

        Open-Meteo liefert bei zu jungem Zeitraum leere oder None-Werte —
        Aufrufer muss PUFFER_TAGE beachten.
        """
        if bis < von:
            return []
        params = {
            "latitude": self._breite,
            "longitude": self._laenge,
            "start_date": von.isoformat(),
            "end_date": bis.isoformat(),
            "hourly": ",".join(HOURLY_FIELDS),
            "timezone": "auto",
        }
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            antwort = await client.get(ARCHIVE_URL, params=params)
            antwort.raise_for_status()
            daten = antwort.json()

        return _parse_stunden(daten)

    async def hole_luftfeuchte(
        self, von: date, bis: date,
    ) -> dict[str, float]:
        """T-0045: Holt stuendliche relative Luftfeuchte aus dem Archiv.

        Rueckgabe: ISO-Zeitstempel → Prozent. Fehlende Stunden werden
        ausgelassen (kein 0.0-Default — das waere unplausibel).
        """
        if bis < von:
            return {}
        params = {
            "latitude": self._breite,
            "longitude": self._laenge,
            "start_date": von.isoformat(),
            "end_date": bis.isoformat(),
            "hourly": ",".join(HUMIDITY_FIELDS),
            "timezone": "auto",
        }
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            antwort = await client.get(ARCHIVE_URL, params=params)
            antwort.raise_for_status()
            daten = antwort.json()

        hourly = daten.get("hourly") or {}
        zeit_liste = hourly.get("time") or []
        rh_liste = hourly.get("relative_humidity_2m") or []

        out: dict[str, float] = {}
        for i, zeit_text in enumerate(zeit_liste):
            if i >= len(rh_liste) or rh_liste[i] is None:
                continue
            # Archive liefert "YYYY-MM-DDTHH:MM", DB speichert mit Sekunden.
            # Normalisieren, damit der UPDATE-Match greift.
            iso_normiert = datetime.fromisoformat(zeit_text).isoformat()
            out[iso_normiert] = float(rh_liste[i])
        return out


def _parse_stunden(daten: dict) -> list[WetterArchivStunde]:
    hourly = daten.get("hourly") or {}
    zeit_liste = hourly.get("time") or []
    temp = hourly.get("temperature_2m") or []
    regen = hourly.get("precipitation") or []
    et0 = hourly.get("et0_fao_evapotranspiration") or []

    out: list[WetterArchivStunde] = []
    for i, zeit_text in enumerate(zeit_liste):
        # Manche Werte koennen null sein (z.B. sehr aktuelle Stunden) — ueberspringen
        if i < len(regen) and regen[i] is None:
            continue
        out.append(WetterArchivStunde(
            zeitstempel=datetime.fromisoformat(zeit_text),
            niederschlag_mm=float(regen[i]) if i < len(regen) and regen[i] is not None else 0.0,
            temperatur=float(temp[i]) if i < len(temp) and temp[i] is not None else None,
            et0_mm=float(et0[i]) if i < len(et0) and et0[i] is not None else 0.0,
        ))
    return out


class WetterArchivJob:
    """Periodischer Job, der Luecken in `wetter_archiv` fuellt.

    `letzte_aktualisierung` schuetzt vor zu haeufigem Aufruf (z.B. im
    5-min-Entscheidungs-Loop). Der eigentliche HTTP-Call laeuft
    hoechstens alle `intervall_stunden` Stunden.
    """

    def __init__(
        self,
        speicher: Speicher,
        clients: list[WetterArchivClient],
        intervall_stunden: int = AKTUALISIERUNGS_INTERVALL_STD,
        puffer_tage: int = PUFFER_TAGE,
    ):
        self._speicher = speicher
        self._clients = clients
        self._intervall = timedelta(hours=intervall_stunden)
        self._puffer = timedelta(days=puffer_tage)
        self._letzte_aktualisierung: datetime | None = None

    async def aktualisiere_wenn_faellig(self, jetzt: datetime | None = None) -> bool:
        """Laeuft hoechstens einmal pro `intervall_stunden`. True wenn gelaufen."""
        jetzt = jetzt or datetime.now()
        if (self._letzte_aktualisierung
                and jetzt - self._letzte_aktualisierung < self._intervall):
            return False
        await self.aktualisiere_alle(jetzt)
        self._letzte_aktualisierung = jetzt
        return True

    async def aktualisiere_alle(self, jetzt: datetime | None = None) -> dict[str, int]:
        """Fuellt Luecken fuer alle konfigurierten Standorte. Gibt {standort: count}."""
        jetzt = jetzt or datetime.now()
        ziel_bis: date = (jetzt - self._puffer).date()
        ergebnis: dict[str, int] = {}

        for client in self._clients:
            try:
                n = await self._aktualisiere_standort(client, ziel_bis)
                ergebnis[client.standort_id] = n
            except Exception:
                logger.exception("wetter_archiv.standort_fehler",
                                 standort=client.standort_id)
                ergebnis[client.standort_id] = 0
        return ergebnis

    async def _aktualisiere_standort(
        self, client: WetterArchivClient, ziel_bis: date,
    ) -> int:
        juengster = await self._speicher.juengster_archiv_zeitstempel(client.standort_id)
        # Wenn noch nichts vorhanden: MAX_BACKFILL_TAGE zurueckblicken.
        # Sonst: ab Tag NACH dem juengsten Eintrag.
        if juengster is None:
            # bis inklusive -> MAX_BACKFILL_TAGE Tage brutto
            von = ziel_bis - timedelta(days=MAX_BACKFILL_TAGE - 1)
        else:
            von = (juengster + timedelta(hours=1)).date()

        if von > ziel_bis:
            logger.info("wetter_archiv.keine_luecke",
                        standort=client.standort_id,
                        juengster=juengster.isoformat() if juengster else None)
            return 0

        # Cap auf MAX_BACKFILL_TAGE pro Aufruf, damit wir Open-Meteo nicht hammern
        spanne_tage = (ziel_bis - von).days + 1
        if spanne_tage > MAX_BACKFILL_TAGE:
            ziel_bis = von + timedelta(days=MAX_BACKFILL_TAGE - 1)

        stunden = await client.hole_archiv(von, ziel_bis)
        n = await self._speicher.upsert_wetter_archiv(stunden, client.standort_id)
        logger.info("wetter_archiv.aktualisiert",
                    standort=client.standort_id, von=von.isoformat(),
                    bis=ziel_bis.isoformat(), stunden=n)
        return n


def baue_clients_aus_konfig(wetter: WetterKonfig) -> list[WetterArchivClient]:
    """Erzeugt Archiv-Clients aus derselben Standort-Konfiguration wie der Live-Client."""
    return [
        WetterArchivClient(s.breite, s.laenge, s.id)
        for s in wetter.standorte
    ]


# --- CLI-Eintrittspunkt fuer Backfill ---

async def _cli_main(args) -> None:
    from pathlib import Path

    from bewaesserung.konfig import lade_konfig

    konfig = lade_konfig(Path(args.konfig) if args.konfig else None)
    speicher = Speicher(konfig.speicher.db_pfad)
    await speicher.verbinden()
    try:
        clients = baue_clients_aus_konfig(konfig.wetter)

        if args.luftfeuchte_backfill:
            if not (args.von and args.bis):
                raise SystemExit("--luftfeuchte-backfill benoetigt --von und --bis")
            von = datetime.fromisoformat(args.von).date()
            bis = datetime.fromisoformat(args.bis).date()
            for client in clients:
                rh_map = await client.hole_luftfeuchte(von, bis)
                n = await speicher.backfill_luftfeuchte(client.standort_id, rh_map)
                print(
                    f"{client.standort_id}: {n} Zeilen mit Luftfeuchte aktualisiert "
                    f"({len(rh_map)} Stunden aus Archiv, {von} - {bis})"
                )
            return

        job = WetterArchivJob(speicher, clients, puffer_tage=args.puffer)

        if args.von and args.bis:
            von = datetime.fromisoformat(args.von).date()
            bis = datetime.fromisoformat(args.bis).date()
            for client in clients:
                stunden = await client.hole_archiv(von, bis)
                n = await speicher.upsert_wetter_archiv(stunden, client.standort_id)
                print(f"{client.standort_id}: {n} Stunden ({von} - {bis})")
        else:
            ergebnis = await job.aktualisiere_alle()
            for sid, n in ergebnis.items():
                print(f"{sid}: {n} Stunden aktualisiert")
    finally:
        await speicher.schliessen()


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Wetter-Archiv Backfill (Open-Meteo Archive)")
    p.add_argument("--konfig", default="config/default.yaml")
    p.add_argument("--von", help="Start-Datum YYYY-MM-DD (optional, sonst auto-Luecke)")
    p.add_argument("--bis", help="End-Datum YYYY-MM-DD (optional)")
    p.add_argument("--puffer", type=int, default=PUFFER_TAGE,
                   help=f"Puffer-Tage (default {PUFFER_TAGE})")
    p.add_argument("--luftfeuchte-backfill", action="store_true",
                   help="T-0045: befuellt wetter_vorhersage.luftfeuchte statt wetter_archiv")
    args = p.parse_args()
    asyncio.run(_cli_main(args))


if __name__ == "__main__":
    main()
