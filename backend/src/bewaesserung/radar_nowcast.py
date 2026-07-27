"""T-0424: Radar-Nowcast als Veto direkt vor dem Ventiloeffnen.

**Zweck, eng gefasst.** Das ist KEIN Planungswerkzeug. Es ist das Veto gegen
"20 Minuten giessen, dann kommt das Gewitter". Auf Sand mit Auswaschung lohnt
sich genau dieser eine Blick, mehr nicht.

Quelle: DWD-RV-Composite ueber Bright Sky (1 km, 5 min, inkl. 2-h-Nowcast,
kein API-Key). Open-Meteos `minutely_15` ist **kein** Ersatz -- das ist reines
NWP ohne dokumentiertes Radar-Blending.

**Drei Fallstricke, alle am 22.07.2026 live verifiziert:**

1. **gzip ist Pflicht.** Ohne `Accept-Encoding: gzip` (bzw. `--compressed`)
   antwortet der Endpoint mit
   `{"detail":"Requests to the radar endpoint with format 'plain' or 'bytes'
   must accept br, zstd, or gzip encoding"}` -- also kein Netzwerkfehler,
   sondern eine JSON-Fehlermeldung, die ein naiver Parser als Datensatz
   missverstehen koennte.

2. **`distance` ist nicht optional, sondern ueberlebenswichtig.** Ohne den
   Parameter liefert der Endpoint ein 401x401-Gitter pro Zeitschritt --
   gemessen **7,8 MB** pro Abruf. Mit `distance=10000` sind es 21x21 und
   **26 KB**. Faktor 300. Ein 5-minuetlicher Abruf ohne `distance` waere
   grober Unfug gegenueber einer fremden, kostenlosen API.

3. **`precipitation_5` ist ein GITTER, kein Skalar.** Der Punktwert steht
   unter `[y][x]` mit den Koordinaten aus `latlon_position`.

**Warum das Umfeld zaehlt, nicht nur der Punkt.** Am 22.07. ergab der reine
Punktwert 0,25 mm ueber 2 h -- unterhalb jeder Veto-Schwelle. Das Maximum im
5x5-Umfeld (also ~5 km) lag bei **1,97 mm**: die Zelle zog knapp vorbei.
Radar-Nowcasts haben Advektionsfehler von einigen Kilometern; wer nur das
Pixel liest, uebersieht die Zelle, die in 20 Minuten darueber ist. Deshalb
liefert `nowcast_2h` beides, und das Veto greift auf das Umfeld-Maximum.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

RADAR_URL = "https://api.brightsky.dev/radar"
# Kantenlaenge des angeforderten Ausschnitts in Metern (s. Fallstrick 2).
DISTANZ_M = 10000
# Werte kommen in 0,01 mm pro 5 min.
EINHEIT_MM = 0.01
# Radius in Pixeln (1 km/Pixel) fuer das Umfeld-Maximum.
UMFELD_RADIUS = 2


@dataclass(frozen=True)
class Nowcast:
    punkt_mm: float
    umfeld_max_mm: float
    schritte: int

    def veto(self, schwelle_mm: float = 1.0) -> bool:
        """Guss vertagen? Bewusst auf dem UMFELD, nicht auf dem Punkt."""
        return self.umfeld_max_mm > schwelle_mm


def werte_aus_gitter(radar: list[dict], x: int, y: int) -> Nowcast:
    """Extrahiert Punkt- und Umfeld-Summe aus der Bright-Sky-Antwort.

    Reine Funktion -- der Netzwerkteil bleibt aussen, damit die Kernlogik
    ohne HTTP testbar ist.
    """
    punkt = 0.0
    umfeld = 0.0
    schritte = 0
    for schritt in radar:
        gitter = schritt.get("precipitation_5")
        if not gitter:
            continue
        schritte += 1
        if 0 <= y < len(gitter) and 0 <= x < len(gitter[y]):
            punkt += gitter[y][x] or 0
        nachbarn = [
            gitter[j][i] or 0
            for j in range(max(0, y - UMFELD_RADIUS),
                           min(len(gitter), y + UMFELD_RADIUS + 1))
            for i in range(max(0, x - UMFELD_RADIUS),
                           min(len(gitter[j]), x + UMFELD_RADIUS + 1))
        ]
        if nachbarn:
            umfeld += max(nachbarn)
    return Nowcast(
        punkt_mm=punkt * EINHEIT_MM,
        umfeld_max_mm=umfeld * EINHEIT_MM,
        schritte=schritte,
    )


class RadarClient:
    """Holt den 2-h-Nowcast. Read-only, kein API-Key."""

    def __init__(self, http_get) -> None:
        # `http_get(url, params, headers) -> dict`. Der Aufrufer MUSS
        # gzip akzeptieren (s. Fallstrick 1); httpx/aiohttp tun das per
        # Default, blankes curl nicht.
        self._get = http_get

    async def nowcast_2h(
        self, latitude: float, longitude: float,
    ) -> Nowcast | None:
        try:
            daten = await self._get(
                RADAR_URL,
                {
                    "lat": latitude, "lon": longitude,
                    "format": "plain", "distance": DISTANZ_M,
                },
                {"Accept-Encoding": "gzip"},
            )
        except Exception as exc:  # noqa: BLE001
            # Ein ausgefallener Nowcast darf NIE das Giessen blockieren.
            # None heisst "kein Veto", nicht "Veto" -- die Zone ist trocken,
            # das ist der belegte Zustand; der Regen ist die Vermutung.
            logger.warning("radar.nowcast_fehlgeschlagen", fehler=str(exc))
            return None

        if daten.get("detail"):
            # Fallstrick 1: JSON-Fehlermeldung statt Daten.
            logger.warning(
                "radar.nowcast_abgelehnt", detail=str(daten["detail"])[:200],
            )
            return None

        pos = daten.get("latlon_position") or {}
        radar = daten.get("radar") or []
        if not radar or "x" not in pos:
            logger.warning("radar.nowcast_leer", schritte=len(radar))
            return None

        nc = werte_aus_gitter(radar, round(pos["x"]), round(pos["y"]))
        logger.info(
            "radar.nowcast",
            punkt_mm=round(nc.punkt_mm, 2),
            umfeld_max_mm=round(nc.umfeld_max_mm, 2),
            schritte=nc.schritte,
        )
        return nc
