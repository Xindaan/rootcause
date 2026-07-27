"""Open-Meteo-Client fuer stuendliche Wettervorhersagen mit In-Memory-Cache.

Unterstuetzt Multi-Standort (z.B. Garten + Balkon).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx
import structlog

from bewaesserung.modelle import WetterKonfig, WetterStunde, WetterVorhersage

logger = structlog.get_logger()

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

HOURLY_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "precipitation_probability",
    "wind_speed_10m",
    "wind_direction_10m",
    "et0_fao_evapotranspiration",
)


class WetterClient:
    """Holt Wettervorhersagen von Open-Meteo fuer einen Standort."""

    def __init__(self, breite: float, laenge: float, cache_minuten: int = 30,
                 regen_schwelle_mm: float = 2.0, standort_id: str = "",
                 regen_wahrscheinlichkeit_schwelle_prozent: float = 80.0):
        self._breite = breite
        self._laenge = laenge
        self._cache_minuten = cache_minuten
        self._regen_schwelle_mm = regen_schwelle_mm
        self._regen_wahrscheinlichkeit_schwelle_prozent = (
            regen_wahrscheinlichkeit_schwelle_prozent
        )
        self._standort_id = standort_id
        self._cache: WetterVorhersage | None = None
        self._cache_gueltig_bis: datetime | None = None
        self._cache_lock = asyncio.Lock()

    @property
    def regen_schwelle_mm(self) -> float:
        return self._regen_schwelle_mm

    @property
    def regen_wahrscheinlichkeit_schwelle_prozent(self) -> float:
        return self._regen_wahrscheinlichkeit_schwelle_prozent

    @property
    def standort_id(self) -> str:
        return self._standort_id

    async def hole_vorhersage(self) -> WetterVorhersage:
        """Holt die aktuelle Wettervorhersage. Cached fuer cache_minuten."""
        jetzt = self._jetzt()
        if self._cache_ist_gueltig(jetzt):
            return self._cache  # type: ignore

        async with self._cache_lock:
            jetzt = self._jetzt()
            if self._cache_ist_gueltig(jetzt):
                return self._cache  # type: ignore

            try:
                vorhersage = await self._hole_von_api()
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "wetter.abfrage_fehlgeschlagen",
                    fehler=str(exc),
                    standort=self._standort_id,
                    breite=self._breite,
                    laenge=self._laenge,
                )
                return WetterVorhersage(abfrage_zeitstempel=jetzt, stunden=[])

            self._cache = vorhersage
            self._cache_gueltig_bis = jetzt + timedelta(
                minutes=max(self._cache_minuten, 0)
            )
            return vorhersage

    async def _hole_von_api(self) -> WetterVorhersage:
        params = {
            "latitude": self._breite,
            "longitude": self._laenge,
            "hourly": ",".join(HOURLY_FIELDS),
            "forecast_days": 2,
            "timezone": "auto",
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            antwort = await client.get(OPEN_METEO_URL, params=params)
            antwort.raise_for_status()
            daten = antwort.json()

        stunden = self._parse_stunden(daten)
        abfrage_zeit = self._jetzt()

        logger.info(
            "wetter.vorhersage_geholt",
            stunden=len(stunden),
            standort=self._standort_id,
        )

        return WetterVorhersage(abfrage_zeitstempel=abfrage_zeit, stunden=stunden)

    def _parse_stunden(self, daten: dict) -> list[WetterStunde]:
        stundendaten = daten["hourly"]
        zeitstempel_liste = stundendaten["time"]

        stunden: list[WetterStunde] = []
        for index, zeit_text in enumerate(zeitstempel_liste):
            stunden.append(
                WetterStunde(
                    zeitstempel=datetime.fromisoformat(zeit_text),
                    temperatur=self._hole_float(stundendaten.get("temperature_2m"), index),
                    niederschlag_mm=self._hole_float(
                        stundendaten.get("precipitation"), index
                    ),
                    niederschlag_wahrscheinlichkeit=self._hole_float(
                        stundendaten.get("precipitation_probability"), index
                    ),
                    wind_kmh=self._hole_float(stundendaten.get("wind_speed_10m"), index),
                    wind_richtung_grad=self._hole_float(
                        stundendaten.get("wind_direction_10m"), index
                    ),
                    et0_mm=self._hole_float(
                        stundendaten.get("et0_fao_evapotranspiration"), index
                    ),
                    # T-0045: nullable — wenn Open-Meteo das Feld ausnahmsweise
                    # nicht liefert, kein Default auf 0% (wuerde VPD verfaelschen).
                    luftfeuchte_prozent=self._hole_optional_float(
                        stundendaten.get("relative_humidity_2m"), index,
                    ),
                )
            )

        return stunden

    def _cache_ist_gueltig(self, jetzt: datetime) -> bool:
        return (
            self._cache is not None
            and self._cache_gueltig_bis is not None
            and jetzt < self._cache_gueltig_bis
        )

    @staticmethod
    def _hole_float(werte: list[float | None] | None, index: int) -> float:
        if werte is None or index >= len(werte):
            return 0.0
        wert = werte[index]
        return float(wert) if wert is not None else 0.0

    @staticmethod
    def _hole_optional_float(
        werte: list[float | None] | None, index: int,
    ) -> float | None:
        """Wie _hole_float, aber None bleibt None (kein 0.0-Default)."""
        if werte is None or index >= len(werte):
            return None
        wert = werte[index]
        return float(wert) if wert is not None else None

    def _jetzt(self) -> datetime:
        return datetime.now()


class WetterManager:
    """Verwaltet WetterClients fuer mehrere Standorte."""

    def __init__(self, konfig: WetterKonfig):
        self._konfig = konfig
        self._clients: dict[str, WetterClient] = {}

        # Multi-Standort aus Config
        if konfig.standorte:
            for s in konfig.standorte:
                self._clients[s.id] = WetterClient(
                    breite=s.breite,
                    laenge=s.laenge,
                    cache_minuten=konfig.cache_minuten,
                    regen_schwelle_mm=konfig.regen_schwelle_mm,
                    regen_wahrscheinlichkeit_schwelle_prozent=(
                        konfig.regen_wahrscheinlichkeit_schwelle_prozent
                    ),
                    standort_id=s.id,
                )
        # Fallback: einzelner Standort (Rueckwaertskompatibilitaet)
        if not self._clients and (konfig.breite or konfig.laenge):
            self._clients["default"] = WetterClient(
                breite=konfig.breite,
                laenge=konfig.laenge,
                cache_minuten=konfig.cache_minuten,
                regen_schwelle_mm=konfig.regen_schwelle_mm,
                regen_wahrscheinlichkeit_schwelle_prozent=(
                    konfig.regen_wahrscheinlichkeit_schwelle_prozent
                ),
                standort_id="default",
            )

    @property
    def standort_ids(self) -> list[str]:
        return list(self._clients.keys())

    def hole_client(self, standort_id: str) -> WetterClient | None:
        """Gibt den WetterClient fuer einen Standort zurueck."""
        return self._clients.get(standort_id)

    @property
    def standard_client(self) -> WetterClient:
        """Erster Client (fuer Rueckwaertskompatibilitaet mit Entscheidungsmotor)."""
        return next(iter(self._clients.values()))

    async def hole_vorhersage(self, standort_id: str | None = None) -> WetterVorhersage:
        """Vorhersage fuer einen Standort. Ohne standort_id: erster Standort."""
        if standort_id and standort_id in self._clients:
            return await self._clients[standort_id].hole_vorhersage()
        return await self.standard_client.hole_vorhersage()
