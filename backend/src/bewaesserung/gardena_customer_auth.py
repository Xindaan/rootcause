"""Husqvarna-IAM Customer-Login fuer das smart.gardena.com-Webfrontend.

**Wofuer**: Der Developer-API-Key (CLIENT_ID/SECRET) gilt nur fuer
`api.smart.gardena.dev`. Fuer den DHS-Endpoint (inoffizieller Device-History-
Service unter `smart.gardena.com/v1/dhs/...`) brauchen wir einen separaten
OAuth2-Access-Token via Customer-Login (derselbe Account wie in der
Gardena-Mobile-App).

**Ablauf**:
1. Password-Grant einmalig bei erstem Start oder bei abgelaufenem
   Refresh-Token: `POST /v1/auth/oauth2/token` mit `grant_type=password`
2. Folge-Aufrufe: refresh_token-Grant (falls der Endpoint das akzeptiert —
   erste Pruefungen zeigten "client not found"; wir starten pragmatisch
   mit Password-Grant und Re-Login bei Bedarf).
3. Token + Expiry werden in einer JSON-Datei persistiert; Refresh erfolgt
   automatisch ~1 Tag vor Ablauf.

**Sicherheit**: Die Token-Datei wird mit 0600 geschrieben, liegt in
`daten/gardena_customer_token.json` (gitignored via `daten/*.json`-Regel).
Credentials kommen aus `.env` als `GARDENA_CUSTOMER_EMAIL/_PASSWORD`.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import structlog

logger = structlog.get_logger()

TOKEN_URL = "https://smart.gardena.com/v1/auth/oauth2/token"
# client_id aus der Ember-App-Config ausgelesen (smartgarden-web/authenticators/
# smart-garden, Instanz-Feld `clientId`). Vorher raten war irrefuehrend —
# `smart-garden-web` antwortet mit "client.not.found".
CLIENT_ID = "smartgarden-jwt-client"

# Refresh-Puffer: hole neuen Token 24 h vor Ablauf.
REFRESH_PUFFER = timedelta(hours=24)

HTTP_TIMEOUT_S = 30.0


class GardenaCustomerAuthError(Exception):
    """Fehler bei Customer-Login (Credentials falsch, Server nicht erreichbar, etc.)."""


class GardenaCustomerAuth:
    """Haelt einen gueltigen Bearer-Token fuer smart.gardena.com bereit."""

    def __init__(
        self,
        email: str,
        password: str,
        token_datei: Path,
    ) -> None:
        if not email or not password:
            raise GardenaCustomerAuthError(
                "GARDENA_CUSTOMER_EMAIL / _PASSWORD fehlen — bitte .env pruefen"
            )
        self._email = email
        self._password = password
        self._token_datei = Path(token_datei)
        self._cache: dict | None = None
        self._lock = asyncio.Lock()

    async def hole_gueltigen_token(self) -> str:
        """Gibt einen gueltigen access_token zurueck. Refresht oder re-logt bei Bedarf."""
        async with self._lock:
            self._cache = self._cache or self._lade_cache()
            if self._cache and not self._ist_bald_abgelaufen(self._cache):
                return self._cache["access_token"]

            # Cache leer oder bald abgelaufen → neu holen
            try:
                self._cache = await self._password_grant()
                self._speichere_cache(self._cache)
                return self._cache["access_token"]
            except GardenaCustomerAuthError:
                self._cache = None
                raise

    def _ist_bald_abgelaufen(self, cache: dict) -> bool:
        expires_at = datetime.fromisoformat(cache["expires_at"])
        return datetime.now(timezone.utc) >= (expires_at - REFRESH_PUFFER)

    def _lade_cache(self) -> dict | None:
        if not self._token_datei.exists():
            return None
        try:
            with self._token_datei.open("r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("gardena_auth.cache_lesen_fehlgeschlagen", fehler=str(exc))
            return None

    def _speichere_cache(self, cache: dict) -> None:
        self._token_datei.parent.mkdir(parents=True, exist_ok=True)
        # Atomar schreiben + 0600-Permissions
        tmp = self._token_datei.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(cache, f, indent=2)
        os.chmod(tmp, 0o600)
        tmp.replace(self._token_datei)

    async def _password_grant(self) -> dict:
        """Holt einen neuen Token via password-grant. Gibt Cache-Dict zurueck."""
        body = {
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "username": self._email,
            "password": self._password,
        }
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            antwort = await client.post(
                TOKEN_URL,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if antwort.status_code != 200:
            # Keine sensiblen Daten ins Log! Nur Status + error_code
            try:
                fehler_daten = antwort.json()
            except ValueError:
                fehler_daten = {"body": antwort.text[:200]}
            code = fehler_daten.get("error_code") or fehler_daten.get("error") or "unbekannt"
            logger.error(
                "gardena_auth.login_fehlgeschlagen",
                status=antwort.status_code,
                error_code=code,
            )
            raise GardenaCustomerAuthError(
                f"Gardena-Login fehlgeschlagen ({antwort.status_code}, {code})"
            )

        daten = antwort.json()
        jetzt = datetime.now(timezone.utc)
        expires_in = int(daten.get("expires_in") or 3600)
        cache = {
            "access_token": daten["access_token"],
            "refresh_token": daten.get("refresh_token"),
            "token_type": daten.get("token_type", "Bearer"),
            "scope": daten.get("scope", ""),
            "expires_at": (jetzt + timedelta(seconds=expires_in)).isoformat(),
            "abgerufen_am": jetzt.isoformat(),
        }
        logger.info(
            "gardena_auth.login_erfolgreich",
            scope=cache["scope"],
            expires_in_stunden=round(expires_in / 3600, 1),
        )
        return cache


def baue_aus_env(token_datei: Path) -> GardenaCustomerAuth | None:
    """Liest Credentials aus Umgebungsvariablen. None wenn nicht konfiguriert."""
    email = os.environ.get("GARDENA_CUSTOMER_EMAIL")
    password = os.environ.get("GARDENA_CUSTOMER_PASSWORD")
    if not email or not password:
        logger.info("gardena_auth.nicht_konfiguriert",
                    hinweis="GARDENA_CUSTOMER_EMAIL/PASSWORD fehlen in .env — DHS-Backfill inaktiv")
        return None
    return GardenaCustomerAuth(email, password, token_datei)
