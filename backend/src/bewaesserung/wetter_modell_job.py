"""T-0505: taeglicher Mehrmodell-Mitschnitt (Shadow, read-only).

**Der Job entscheidet nichts.** Er schreibt mit, was fuenf unabhaengige
Wettermodelle fuer unsere Standorte sagen, damit T-0506 beantworten kann, ob
die Vorhersage die Giess-Entscheidung falsch stellt und ob ein zweites Modell
hilft. Erst wenn das belegt ist, kaeme ein Umbau der Entscheidungslogik in
Frage -- nicht vorher.

**Warum ueberhaupt.** `wetter.py` ruft `/v1/forecast` ohne `models`-Parameter.
Das ist `best_match` und liefert an diesem Standort ICON. Jeder Guss haengt
damit an einer einzigen Modellfamilie, ohne Zweitmeinung und ohne jede
Rueckmeldung, ob sie hier recht hatte. Am 05.08.2026 lagen die 48-h-Summen
EINES Abrufs bei 13,9 / 0,2 / 0,9 / 2,4 / 0,6 mm (ICON-D2 / ICON-EU / IFS /
AIFS / GFS). Faktor 70 -- und der Produktivpfad las den nassesten Wert.

**Ein Abruf, fuenf Modelle.** Open-Meteo nimmt eine komma-separierte
`models`-Liste und liefert `precipitation_<modell>` nebeneinander. Der
komplette Multi-Modell-Abruf war 461 Bytes; fuenf Einzel-Abrufe waeren
fuenfmal so viel Last auf einer fremden Gratis-API ohne jeden Gegenwert.

**Der Modelllauf-Zeitstempel ist ein Zusatz-Call.** `/v1/forecast` liefert
ihn nicht -- die Antwort enthaelt nur `generationtime_ms`, nicht die
Initialisierungszeit. Die Single-Runs-API kennt ihn, verlangt aber `run=` als
Eingabe, sagt einem also nicht von sich aus, welcher Lauf der neueste ist.
Deshalb tasten wir uns im 3-h-Raster rueckwaerts, bis ein Lauf antwortet.
Alle fuenf Modelle laufen auf diesem Raster (ICON alle 3 h, IFS/AIFS/GFS alle
6 h), ein 6-h-Modell kostet dabei hoechstens einen Fehlversuch mehr.

Ohne diesen Zeitstempel sieht eine unveraenderte Prognose aus wie eine
bestaetigte: man weiss nicht, ob das Modell bei seiner Aussage geblieben ist
oder ob wir zweimal denselben Lauf gelesen haben. Faellt die Aufloesung aus,
wird trotzdem geschrieben -- der Mitschnitt ist wichtiger als seine
Herkunftsangabe.

**Cadence.** Einmal taeglich, wie beauftragt. Das reicht fuer die Frage "was
wusste man gestern ueber heute". Es verschenkt bewusst die Revisionen
INNERHALB eines Tages -- fuer den 23.07. lieferte dieselbe Quelle binnen
5,5 h 13,2 -> 8,8 -> 1,7 mm (T-0423). Wer das messen will, stellt
`INTERVALL_STUNDEN` herunter; die Tabelle traegt es ohne Aenderung, weil der
Abfragezeitpunkt im Primary Key steht.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import structlog

logger = structlog.get_logger()

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
SINGLE_RUN_URL = "https://single-runs-api.open-meteo.com/v1/forecast"

# Fuenf bewusst verschiedene Familien, nicht fuenf Varianten derselben:
# zwei DWD-ICON-Aufloesungen, das physikalische ECMWF-IFS, das
# ML-basierte ECMWF-AIFS und das amerikanische GFS. Zweitmeinung heisst
# unabhaengige Fehlerquellen, nicht mehr Nachkommastellen.
MODELLE = (
    "icon_d2",
    "icon_eu",
    "ecmwf_ifs025",
    "ecmwf_aifs025_single",
    "gfs_seamless",
)

HORIZONT_TAGE = 2          # 48 h -- deckt den 6-h-Gate- und 24-h-Bilanzbedarf
INTERVALL_STUNDEN = 24
HTTP_TIMEOUT_S = 20.0
# Wie weit im 3-h-Raster rueckwaerts nach dem juengsten Lauf gesucht wird.
# 8 Schritte = 24 h; findet auch nach einem laengeren Modellausfall noch etwas.
MAX_LAUF_SCHRITTE = 8


def _raster_3h(jetzt_utc: datetime) -> datetime:
    """Rundet auf das naechstniedrigere 3-h-Raster (00, 03, 06, ... UTC)."""
    return jetzt_utc.replace(
        hour=(jetzt_utc.hour // 3) * 3, minute=0, second=0, microsecond=0,
    )


class WetterModellJob:
    """Holt die 48-h-Regenprognose aller Modelle je Standort und schreibt fort."""

    def __init__(
        self,
        speicher,
        standorte: list[tuple[str, float, float]],
        intervall_stunden: int = INTERVALL_STUNDEN,
        modelle: tuple[str, ...] = MODELLE,
    ):
        # `standorte`: [(standort_id, breite, laenge)]
        self._speicher = speicher
        self._standorte = standorte
        self._intervall = timedelta(hours=intervall_stunden)
        self._modelle = modelle
        self._letzter_lauf: datetime | None = None

    async def _hole_prognosen(self, breite: float, laenge: float) -> dict | None:
        """Ein GET fuer alle Modelle. None bei Fehler -- der Job ist Shadow."""
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                antwort = await client.get(FORECAST_URL, params={
                    "latitude": breite,
                    "longitude": laenge,
                    "hourly": "precipitation",
                    "models": ",".join(self._modelle),
                    "forecast_days": HORIZONT_TAGE,
                    "timezone": "Europe/Berlin",
                })
                antwort.raise_for_status()
                return antwort.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("wetter_modell.abruf_fehlgeschlagen",
                           fehler=str(exc), breite=breite, laenge=laenge)
            return None

    async def _bestimme_lauf(
        self, modell: str, breite: float, laenge: float,
        jetzt_utc: datetime | None = None,
    ) -> datetime | None:
        """Juengster verfuegbarer Modelllauf, oder None.

        Die Single-Runs-API antwortet auf einen nicht vorhandenen Lauf mit
        HTTP 400 und `reason`, nicht mit einem leeren Ergebnis. Der erste
        Lauf, der 200 liefert, ist der aktuellste -- wir gehen vom jetzigen
        3-h-Raster rueckwaerts.
        """
        jetzt_utc = jetzt_utc or datetime.now(timezone.utc)
        kandidat = _raster_3h(jetzt_utc)
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                for _ in range(MAX_LAUF_SCHRITTE):
                    antwort = await client.get(SINGLE_RUN_URL, params={
                        "latitude": breite,
                        "longitude": laenge,
                        "hourly": "precipitation",
                        "models": modell,
                        "run": kandidat.strftime("%Y-%m-%dT%H:%M"),
                        "forecast_hours": 1,
                        "timezone": "Europe/Berlin",
                    })
                    if antwort.status_code == 200:
                        return kandidat
                    kandidat -= timedelta(hours=3)
        except Exception as exc:  # noqa: BLE001
            # Bewusst nur eine Warnung: ohne Lauf-Zeitstempel ist der
            # Mitschnitt schlechter, aber nicht wertlos.
            logger.warning("wetter_modell.lauf_unbestimmbar",
                           fehler=str(exc), modell=modell)
            return None
        logger.warning("wetter_modell.kein_lauf_gefunden", modell=modell,
                       zurueck_bis=kandidat.isoformat())
        return None

    @staticmethod
    def _stunden_fuer_modell(
        hourly: dict, modell: str,
    ) -> list[tuple[datetime, float | None]]:
        """Extrahiert (Zielzeit, mm) fuer ein Modell aus dem hourly-Block.

        Open-Meteo haengt bei Multi-Modell-Abfragen den Modellnamen an den
        Spaltennamen. Fehlt die Spalte, liefert das Modell an diesem Standort
        nichts -- das ist kein Fehler, sondern eine leere Liste.
        """
        spalte = f"precipitation_{modell}"
        werte = hourly.get(spalte)
        zeiten = hourly.get("time") or []
        if not werte:
            return []
        return [
            (datetime.fromisoformat(t), werte[i] if i < len(werte) else None)
            for i, t in enumerate(zeiten)
        ]

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> int:
        """Laeuft hoechstens alle `intervall_stunden`. Gibt Zeilen zurueck."""
        jetzt = jetzt or datetime.now()
        if self._letzter_lauf is not None:
            if jetzt - self._letzter_lauf < self._intervall:
                return 0
        # Vor dem Abruf setzen, nicht danach: ein haengender oder
        # fehlschlagender Abruf darf nicht dazu fuehren, dass der Job im
        # naechsten 5-min-Tick sofort wieder gegen dieselbe API laeuft.
        self._letzter_lauf = jetzt

        geschrieben = 0
        for standort_id, breite, laenge in self._standorte:
            daten = await self._hole_prognosen(breite, laenge)
            if not daten:
                continue
            hourly = daten.get("hourly") or {}
            for modell in self._modelle:
                stunden = self._stunden_fuer_modell(hourly, modell)
                if not stunden:
                    logger.warning("wetter_modell.modell_ohne_daten",
                                   modell=modell, standort=standort_id)
                    continue
                lauf = await self._bestimme_lauf(modell, breite, laenge)
                n = await self._speicher.speichere_modell_prognosen(
                    jetzt, standort_id, modell, stunden, lauf,
                )
                geschrieben += n
                summe = sum(mm or 0.0 for _, mm in stunden)
                logger.info(
                    "wetter_modell.gespeichert",
                    standort=standort_id, modell=modell, stunden=n,
                    summe_48h_mm=round(summe, 2),
                    modell_lauf=lauf.isoformat() if lauf else None,
                )
        return geschrieben
