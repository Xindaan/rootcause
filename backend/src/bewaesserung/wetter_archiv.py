"""Open-Meteo Archive-API Client fuer historische Regen-/ET0-Daten.

**Wofuer**: Unsere Wetter-Vorhersagen werden als Forecast gespeichert. Wenn
der Forecast 5 mm Regen sagte, die Realitaet aber nur 0.3 mm lieferte,
kennen wir die Abweichung nicht. Diese Ground-Truth brauchen wir fuer
fundierte T-0025-Diagnose und fuer ML-Evaluation (T-0047).

**Design**: Eigene Tabelle `wetter_archiv` statt Mutieren der Forecast-Zeilen
— Forecast-Historie bleibt unveraendert, Real-Werte sind separat. Ein Job
laeuft einmal pro Tag und fuellt Luecken bis `jetzt - puffer_tage`.

**Puffer**: Das Archiv hat typisch 3-5 Tage Verzoegerung. Standard
`PUFFER_TAGE = 5` — wir holen nichts innerhalb der letzten 5 Tage, um nicht
preliminaere Daten mit spaeteren Korrekturen zu ueberschreiben.

**Was hier tatsaechlich abgerufen wird — KORREKTUR T-0507 (05.08.2026).**
Diese Datei hat bis dahin behauptet, die Quelle sei ERA5. Das ist falsch, und
zwar nicht nur im Namen. `hole_archiv` ruft die Archive-API OHNE
`models`-Parameter; der Default ist `best_match`, und der ist an diesem
Standort `ecmwf_ifs` — die ECMWF-IFS-Analyse, nicht die ERA5-Reanalyse.

Nachgemessen am Referenzstandort, Juli 2026, Tagessummen tagegenau
verglichen:

  * unsere `wetter_archiv`-Tabelle   55,0 mm  (624 Stunden)
  * `models=ecmwf_ifs`               55,0 mm  (624 Stunden)  <- identisch
  * `models=era5`                    30,5 mm  (602 Stunden)
  * DWD-Station Marwitz (12,1 km)    25,2 mm

Die Pipeline ist also korrekt — sie speichert exakt, was die API liefert.
Falsch war die Annahme darueber, WAS sie liefert. Zwei Folgen, die man
kennen muss, bevor man mit diesen Werten argumentiert:

1. **Der Wert ist rund doppelt so nass wie die Stationsmessung.** Einzeltage
   laufen weit auseinander (20.07.: 8,2 / 1,8 / 0,2 mm fuer Archiv / era5 /
   Station). Die Wasserbilanz rechnet heute mit der nassen Variante.
2. **Als Bewertungsmassstab fuer ECMWF-Vorhersagen ist er zirkulaer** —
   IFS gegen IFS misst nichts. Deshalb nutzt der Modellvergleich (T-0506)
   die DWD-Station und `models=era5`, nicht diese Tabelle.

**ENTSCHIEDEN T-0509 (05.08.2026): die Quelle ist jetzt `era5`.**
`wetter.archiv_modell` in der Konfig setzt das Produkt explizit; Leerstring
faellt auf den Archive-Default (also `ecmwf_ifs`) zurueck.

Entschieden wurde nicht durch Vergleich der Quellen untereinander -- der sagt
nur, DASS sie sich unterscheiden -- sondern gegen die eigenen Bodensensoren.
Die stehen im Garten und messen, ob dort Wasser ankam; das ist die Groesse,
an der die Bilanz haengt. Ausgewertet ueber 86 Tage, 65 auswertbare Zone-Tage
ohne Ventil-Ereignis (`docs/analyse/t0509_regenquelle_gegen_sensoren.py`):

| Quelle | "Regen angesagt, Boden bleibt trocken" | Fehlerquote |
|---|---|---|
| ecmwf_ifs (bisher) | 26 | 40,0 % |
| era5 | 16 | 24,6 % |
| DWD-Station | 13 | 21,5 % |

Der Fehler ist EINSEITIG: der umgekehrte Fall ("kein Regen angesagt, Boden
steigt trotzdem") kommt ueber alle Quellen zusammen genau 1x vor. Keine
Quelle uebersieht Regen; `ecmwf_ifs` erfindet ihn.

Und er sitzt nicht an der Aufloesungsgrenze, sondern gerade bei den grossen
Mengen. An den fuenf Tagen mit >= 8 mm IFS-Regen: IFS 49,7 mm, era5 13,6 mm,
Station 6,7 mm -- und kein Sensor reagierte. Am 20.07. sagte IFS 8,2 mm, die
Station 0,2 mm.

`era5` und nicht die Station, obwohl die minimal besser trifft: era5 liegt
auf UNSEREN Koordinaten statt 12 km entfernt, ist lueckenlos und ein
Ein-Parameter-Wechsel an derselben API mit derselben Latenz. Die Station
laeuft seit T-0505 ohnehin als unabhaengige Gegenprobe mit
(`wetter_messung_station`).

**Beim Umstellen zwingend:** die Tabelle darf keine zwei Produkte mischen
(`fehlerpattern_skalen_mix_multisensor_aggregat`). Nach jeder Aenderung von
`archiv_modell` die volle Historie nachziehen:

    .venv/bin/python -m bewaesserung.wetter_archiv --von 2026-02-13 --bis 2026-07-31

Der Upsert ist idempotent und der Schritt umkehrbar -- derselbe Lauf mit
`archiv_modell: ecmwf_ifs` stellt den alten Stand wieder her.
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
# T-0510: wird bei JEDEM Lauf mitgeholt, auch wenn die Reihe aktuell aussieht.
# Siehe `_aktualisiere_standort`. 7 Tage decken den PUFFER_TAGE-Rand plus zwei
# Tage Reserve ab und kosten einen Abruf, den der Job ohnehin macht.
BASIS_TAGE = 7
AKTUALISIERUNGS_INTERVALL_STD = 24 # Job-Cadence
HTTP_TIMEOUT_S = 30.0


class WetterArchivClient:
    """Ruft stuendliche Archiv-Daten fuer einen Standort."""

    def __init__(self, breite: float, laenge: float, standort_id: str,
                 modell: str = "era5"):
        self._breite = breite
        self._laenge = laenge
        self._standort_id = standort_id
        # T-0509: leer = Archive-Default (best_match, hier ecmwf_ifs).
        self._modell = modell or ""

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
        if self._modell:
            params["models"] = self._modell
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
        if self._modell:
            params["models"] = self._modell
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
        fehler = 0

        for client in self._clients:
            try:
                n = await self._aktualisiere_standort(client, ziel_bis)
                ergebnis[client.standort_id] = n
            except Exception:
                # T-0563: der Rueckgabewert bleibt bewusst 0 -- ein
                # Bestandstest sichert das zu, und Konsumenten unterscheiden
                # heute nicht zwischen "Fehler" und "nichts nachzutragen".
                # Behoben wird die SICHTBARKEIT: faellt jeder Standort aus,
                # meldete `aktualisiere_wenn_faellig` trotzdem True und
                # sperrte den Job still fuer `intervall_stunden`. Laut
                # [[fehlerpattern_archiv_lag_als_kein_regen]] ist das die
                # Regen-Referenz -- ein stummer 24-h-Blackout ist hier kein
                # kosmetisches Problem.
                fehler += 1
                logger.exception("wetter_archiv.standort_fehler",
                                 standort=client.standort_id)
                ergebnis[client.standort_id] = 0
        if fehler and fehler == len(self._clients):
            logger.error(
                "wetter_archiv.komplettausfall",
                standorte=len(self._clients),
                naechster_versuch_in_h=(
                    self._intervall.total_seconds() / 3600
                ),
            )
        return ergebnis

    async def _aktualisiere_standort(
        self, client: WetterArchivClient, ziel_bis: date,
    ) -> int:
        juengster = await self._speicher.juengster_archiv_zeitstempel(client.standort_id)
        # Wenn noch nichts vorhanden: MAX_BACKFILL_TAGE zurueckblicken.
        if juengster is None:
            # bis inklusive -> MAX_BACKFILL_TAGE Tage brutto
            von = ziel_bis - timedelta(days=MAX_BACKFILL_TAGE - 1)
        else:
            # T-0510: NICHT einfach `juengster + 1h`. Ein Fenster, das
            # ausschliesslich aus "wie weit sind wir gekommen?" abgeleitet
            # wird, kann ein Loch in der MITTE der Reihe nie mehr schliessen:
            # sobald der letzte Eintrag jung ist, faengt der naechste Lauf
            # dahinter an und die Luecke davor bleibt fuer immer stehen.
            # Genau diese Klasse hat T-0504 im FYTA-Poll erwischt
            # (`fehlerpattern_luecke_am_ende_statt_loch_in_der_mitte`), und
            # der StationMessungJob aus T-0505 faehrt aus demselben Grund ein
            # Basis-Fenster mit.
            #
            # Unbedenklich, weil `upsert_wetter_archiv` per
            # `ON CONFLICT DO UPDATE` idempotent ist -- ein erneuter Abruf
            # ueberschreibt gleiche Werte mit gleichen Werten und holt
            # nebenbei spaetere Korrekturen des Archivs mit.
            von = min(
                (juengster + timedelta(hours=1)).date(),
                ziel_bis - timedelta(days=BASIS_TAGE - 1),
            )

        if von > ziel_bis:
            # Nur noch erreichbar, wenn das Archiv weiter reicht als
            # `ziel_bis` -- also bei sehr grossem PUFFER_TAGE oder einer
            # zurueckgedrehten Uhr.
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
        WetterArchivClient(s.breite, s.laenge, s.id,
                           getattr(wetter, "archiv_modell", "era5"))
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
