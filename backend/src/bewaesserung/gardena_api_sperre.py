"""T-0617: Gemeinsame, persistente Ruhezeit nach Gardena-HTTP-429.

Gilt nur fuer die Developer-API. DHS/FYTA haben eigene Clients. Schliessen
bleibt moeglich; ein Close-Erfolg hebt die Sperre fuer Starts nicht auf.
"""

import asyncio
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import time

import httpx
import structlog

from bewaesserung.gardena_api_kontingent import GardenaApiKontingent

logger = structlog.get_logger()
SPERRE_BASIS_S = 4 * 3600
SPERRE_MAX_S = 24 * 3600


class GardenaApiGesperrt(RuntimeError):
    """Lokale Ablehnung ohne HTTP-Aufruf; kein SDK-HTTP-Retry."""

    status_code = 429
    ist_soft_ban = True

    def __init__(self, bis: float, detail: str = "lokale Ruhezeit, keine Anfrage gesendet"):
        self.bis = bis
        self.detail = detail
        stand = datetime.fromtimestamp(bis, timezone.utc).astimezone().isoformat()
        super().__init__(f"Gardena-API nach HTTP 429 gesperrt bis {stand}: {detail}")


class GardenaApiSperre:
    def __init__(self, client_id: str, pfad: Path | None = None):
        self.pfad = pfad
        self.identitaet = hashlib.sha256(client_id.encode()).hexdigest()
        self.kontingent = GardenaApiKontingent(
            client_id, pfad.with_name(pfad.stem + "_kontingent.json") if pfad else None,
        )
        self.bis = 0.0
        self.stufe = 0
        if pfad is not None and pfad.exists():
            # Unlesbarer/ungueltiger Zustand darf keinen neuen API-Burst erlauben.
            daten = json.loads(pfad.read_text())
            if daten["identitaet"] == self.identitaet:
                self.bis = float(daten["bis"])
                self.stufe = int(daten["stufe"])
                if not math.isfinite(self.bis) or self.bis < 0 or not 0 <= self.stufe <= 4:
                    raise ValueError("Ungueltige Gardena-API-Sperre")

    def _speichere(self) -> None:
        if self.pfad is None:
            return
        self.pfad.parent.mkdir(parents=True, exist_ok=True)
        temp = self.pfad.with_suffix(".tmp")
        temp.write_text(json.dumps({
            "identitaet": self.identitaet, "bis": self.bis, "stufe": self.stufe,
        }) + "\n")
        temp.replace(self.pfad)

    def pruefe(self) -> None:
        if self.bis > time.time():
            raise GardenaApiGesperrt(self.bis)

    async def warte(self) -> None:
        while self.bis > time.time():
            await asyncio.sleep(min(60, self.bis - time.time()))

    def sperre(self, retry_after: str | None = None) -> None:
        jetzt = time.time()
        # Bereits fliegende Requests/Schliess-Retries eskalieren nicht mehrfach.
        if self.bis <= jetzt:
            self.stufe = min(self.stufe + 1, 4)
            self.bis = jetzt + min(SPERRE_MAX_S, SPERRE_BASIS_S * 2 ** (self.stufe - 1))
        if retry_after:
            from email.utils import parsedate_to_datetime
            try:
                sekunden = float(retry_after)
                deadline = jetzt + sekunden
            except ValueError:
                try:
                    deadline = parsedate_to_datetime(retry_after).timestamp()
                except (ValueError, TypeError, OverflowError):
                    deadline = 0
            if math.isfinite(deadline):
                self.bis = max(self.bis, deadline)
        self._speichere()
        logger.error("gardena.api_sperre", bis=self.bis, stufe=self.stufe)

    @staticmethod
    def ist_schliesskommando(daten: dict) -> bool:
        return (
            daten.get("type") == "VALVE_CONTROL"
            and daten.get("attributes", {}).get("command") == "STOP_UNTIL_NEXT_TASK"
        )

    @staticmethod
    def _ist_schliessen(request: httpx.Request) -> bool:
        if request.method != "PUT" or not request.url.path.startswith("/v2/command/"):
            return False
        try:
            daten = json.loads(request.content)["data"]
            return GardenaApiSperre.ist_schliesskommando(daten)
        except (ValueError, KeyError, TypeError, AttributeError):
            return False

    async def vor_anfrage(self, request: httpx.Request) -> None:
        if request.url.host == "api.smart.gardena.dev" and not self._ist_schliessen(request):
            self.pruefe()
            # fsync/Dateisystem duerfen den Ereignisloop fuer Closes nicht
            # aufhalten. Ohne erfolgreiche Reservierung kein HTTP-Versuch.
            await asyncio.to_thread(
                self.kontingent.reserviere,
                websocket=request.url.path.rstrip("/") == "/v2/websocket",
            )
            self.pruefe()  # Ein anderer Request kann waehrend fsync 429 melden.

    async def nach_antwort(self, response: httpx.Response) -> None:
        if response.request.url.host != "api.smart.gardena.dev":
            return
        if response.status_code == 429:
            self.sperre(response.headers.get("Retry-After"))
            try:
                await response.aread()
                detail = response.text[:300]
            except httpx.HTTPError:
                detail = "HTTP 429, Antworttext nicht lesbar"
            # SDK-Backoff-Decorator faengt nur HTTPStatusError. Die Sperre
            # muss nach dem ERSTEN 429 greifen, vor dessen schnellen Retries.
            raise GardenaApiGesperrt(self.bis, detail)
        if 200 <= response.status_code < 300 and self.bis <= time.time() and self.stufe:
            self.bis = 0.0
            self.stufe = 0
            self._speichere()

    def installiere(self, client: httpx.AsyncClient) -> None:
        if self.vor_anfrage not in client.event_hooks["request"]:
            client.event_hooks["request"].append(self.vor_anfrage)
            client.event_hooks["response"].append(self.nach_antwort)
