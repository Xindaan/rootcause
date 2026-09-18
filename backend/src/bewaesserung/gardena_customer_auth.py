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
import hashlib
import json
import math
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

# T-0600 (Gesamtreview LI-5): Backoff nach abgelehntem Login. Ohne ihn loeste
# jeder Sensor und jeder Backfill-Job einen eigenen password-grant aus (Repro:
# 5 POSTs pro DHS-Lauf, hochgerechnet ~330 Fehllogins/Tag) -- gegen eine
# inoffizielle API am selben Konto wie die Gardena-App. Bei 429 verlaengert
# genau das die Sperre (Klasse fehlerpattern_husqvarna_softban).
# Exponentiell: Basis * 2^(Fehlversuche-1), gedeckelt. Werte = Vorschlag aus
# T-0600 AK1 (30 min, bei 429 2-4 h), keine Messung.
LOGIN_SPERRE_BASIS = timedelta(minutes=30)
LOGIN_SPERRE_BASIS_429 = timedelta(hours=2)
LOGIN_SPERRE_MAX = timedelta(hours=4)
# T-0621: Exponent kappen. Ohne Kappung wirft timedelta * 2**(n-1) ab dem
# 37. Fehlversuch (401, Basis 30 min) bzw. 35. (429, Basis 2 h) OverflowError, die Sperre wird nicht mehr gesetzt und jeder
# Aufruf loggt sich wieder ein. 2**8 * 30 min liegt weit ueber dem Deckel.
LOGIN_SPERRE_EXPONENT_MAX = 8


def _jetzt_utc() -> datetime:
    return datetime.now(timezone.utc)


class GardenaCustomerAuthError(Exception):
    """Fehler bei Customer-Login (Credentials falsch, Server nicht erreichbar, etc.)."""

    def __init__(self, nachricht: str, status: int | None = None) -> None:
        super().__init__(nachricht)
        self.status = status


class GardenaCustomerAuth:
    """Haelt einen gueltigen Bearer-Token fuer smart.gardena.com bereit."""

    def __init__(
        self,
        email: str,
        password: str,
        token_datei: Path,
        sperre_datei: Path | None = None,
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
        # T-0600: EIN Zustand fuer alle Aufrufer (main.py teilt die Instanz
        # zwischen DHS-Sensor-Backfill, Web-Backfill-Jobs und Endpoint-Probe).
        self._fehlversuche = 0
        self._gesperrt_bis: datetime | None = None
        self._letzte_ablehnung: str = ""
        # T-0621: Sperre ueberlebt Neustarts (launchd startete am 12.10. nach
        # DNS-Fehlern mehrfach neu). Muster gardena_api_sperre.py: eigene
        # Datei, Schluessel = sha256 des Login-Namens, nie Klartext.
        self._sperre_datei = Path(sperre_datei) if sperre_datei is not None else None
        self._identitaet = hashlib.sha256(email.encode()).hexdigest()
        self._lade_sperre()

    async def hole_gueltigen_token(self) -> str:
        """Gibt einen gueltigen access_token zurueck. Refresht oder re-logt bei Bedarf."""
        async with self._lock:
            self._cache = self._cache or self._lade_cache()
            if self._cache and not self._ist_bald_abgelaufen(self._cache):
                return self._cache["access_token"]

            # T-0600: nach einer Ablehnung bis zum Ende der Sperre KEIN
            # weiterer Login-Versuch -- sofort dieselbe Exception, ohne HTTP.
            jetzt = _jetzt_utc()
            if self._gesperrt_bis is not None and jetzt < self._gesperrt_bis:
                logger.debug(
                    "gardena_auth.login_gesperrt",
                    bis=self._gesperrt_bis.isoformat(timespec="seconds"),
                )
                raise GardenaCustomerAuthError(
                    f"Gardena-Login gesperrt bis "
                    f"{self._gesperrt_bis.isoformat(timespec='seconds')} "
                    f"nach Ablehnung ({self._letzte_ablehnung}, "
                    f"{self._fehlversuche}. Fehlversuch, T-0600)"
                )

            # Cache leer oder bald abgelaufen → neu holen
            try:
                self._cache = await self._password_grant()
                self._speichere_cache(self._cache)
                if self._fehlversuche or self._gesperrt_bis is not None:
                    self._loesche_sperre()
                self._fehlversuche = 0
                self._gesperrt_bis = None
                return self._cache["access_token"]
            except GardenaCustomerAuthError as exc:
                self._cache = None
                self._sperre_nach_ablehnung(exc, jetzt)
                raise

    def _sperre_nach_ablehnung(
        self, exc: GardenaCustomerAuthError, jetzt: datetime,
    ) -> None:
        """T-0600: Sperre setzen und EINMAL pro Ablehnung warnen (gedrosselt:
        die Aufrufe waehrend der Sperre loggen nur auf debug)."""
        self._fehlversuche += 1
        basis = (
            LOGIN_SPERRE_BASIS_429 if exc.status == 429 else LOGIN_SPERRE_BASIS
        )
        exponent = min(self._fehlversuche - 1, LOGIN_SPERRE_EXPONENT_MAX)
        dauer = min(basis * (2 ** exponent), LOGIN_SPERRE_MAX)
        self._gesperrt_bis = jetzt + dauer
        self._letzte_ablehnung = str(exc)
        self._speichere_sperre(exc.status)
        logger.warning(
            "gardena_auth.login_backoff",
            fehlversuche=self._fehlversuche,
            sperre_min=round(dauer.total_seconds() / 60),
            bis=self._gesperrt_bis.isoformat(timespec="seconds"),
            status=exc.status,
            hinweis="Kein weiterer Customer-Login bis zum Ende der Sperre "
                    "(Soft-Ban-Schutz, T-0600). DHS-Nachtraege pausieren.",
        )

    def _lade_sperre(self) -> None:
        """T-0621: laufende Sperre aus der Datei uebernehmen.

        Kaputte oder fremde Datei -> keine Sperre, Warnung (bewusst anders
        als gardena_api_sperre, das dort fail-closed wirft: hier haengt der
        Dienststart nicht am Login). Abgelaufene Sperre -> keine Sperre, der
        Fehlversuchszaehler bleibt aber erhalten, damit die Eskalation wie im
        Speicher weiterlaeuft; erst ein Erfolg setzt ihn zurueck.
        """
        pfad = self._sperre_datei
        if pfad is None or not pfad.exists():
            return
        try:
            daten = json.loads(pfad.read_text())
            identitaet = daten["identitaet"]
            bis = float(daten["bis"])
            fehlversuche = int(daten["fehlversuche"])
            status = daten.get("status")
            if not math.isfinite(bis) or fehlversuche < 1:
                raise ValueError("bis/fehlversuche ausserhalb des Wertebereichs")
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            logger.warning(
                "gardena_auth.login_sperre_ungueltig",
                datei=str(pfad), fehler=type(exc).__name__,
                hinweis="Datei ignoriert, keine Login-Sperre aktiv (T-0621)",
            )
            return
        if identitaet != self._identitaet:
            logger.warning(
                "gardena_auth.login_sperre_fremd",
                datei=str(pfad),
                hinweis="Sperre gehoert zu einem anderen Login, ignoriert (T-0621)",
            )
            return
        jetzt = _jetzt_utc()
        try:
            gesperrt_bis = datetime.fromtimestamp(bis, timezone.utc)
        except (OverflowError, OSError, ValueError):
            gesperrt_bis = jetzt + LOGIN_SPERRE_MAX
        # Weiter als der Deckel in der Zukunft kann der Code nicht schreiben
        # (Uhrsprung, Handedit): kappen statt ewig sperren.
        gesperrt_bis = min(gesperrt_bis, jetzt + LOGIN_SPERRE_MAX)
        self._fehlversuche = fehlversuche
        self._letzte_ablehnung = f"HTTP {status}, aus {pfad.name}"
        if jetzt < gesperrt_bis:
            self._gesperrt_bis = gesperrt_bis
            logger.warning(
                "gardena_auth.login_sperre_geladen",
                bis=gesperrt_bis.isoformat(timespec="seconds"),
                fehlversuche=fehlversuche, status=status,
                hinweis="Kein Customer-Login bis zum Ende der Sperre (T-0621)",
            )
        else:
            logger.warning(
                "gardena_auth.login_sperre_abgelaufen",
                bis=gesperrt_bis.isoformat(timespec="seconds"),
                fehlversuche=fehlversuche,
                hinweis="Sperre abgelaufen, naechster Login erlaubt; "
                        "Eskalationsstufe bleibt bis zum Erfolg (T-0621)",
            )

    def _speichere_sperre(self, status: int | None) -> None:
        """T-0621: Sperre atomar schreiben. Fehler -> Warnung, die Sperre im
        Speicher gilt unveraendert weiter."""
        pfad = self._sperre_datei
        if pfad is None or self._gesperrt_bis is None:
            return
        try:
            pfad.parent.mkdir(parents=True, exist_ok=True)
            temp = pfad.with_suffix(".tmp")
            temp.write_text(json.dumps({
                "identitaet": self._identitaet,
                "bis": self._gesperrt_bis.timestamp(),
                "bis_utc": self._gesperrt_bis.isoformat(timespec="seconds"),
                "fehlversuche": self._fehlversuche,
                "status": status,
            }) + "\n")
            # wie Token-Cache: nur der Besitzer liest (Gate W5 G4)
            os.chmod(temp, 0o600)
            temp.replace(pfad)
        except OSError as exc:
            logger.warning(
                "gardena_auth.login_sperre_schreiben_fehlgeschlagen",
                datei=str(pfad), fehler=type(exc).__name__,
                hinweis="Sperre gilt nur bis zum naechsten Neustart (T-0621)",
            )

    def _loesche_sperre(self) -> None:
        pfad = self._sperre_datei
        if pfad is None:
            return
        try:
            pfad.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "gardena_auth.login_sperre_loeschen_fehlgeschlagen",
                datei=str(pfad), fehler=type(exc).__name__,
            )

    def _ist_bald_abgelaufen(self, cache: dict) -> bool:
        expires_at = datetime.fromisoformat(cache["expires_at"])
        return _jetzt_utc() >= (expires_at - REFRESH_PUFFER)

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
                f"Gardena-Login fehlgeschlagen ({antwort.status_code}, {code})",
                status=antwort.status_code,
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


def baue_aus_env(
    token_datei: Path, sperre_datei: Path | None = None,
) -> GardenaCustomerAuth | None:
    """Liest Credentials aus Umgebungsvariablen. None wenn nicht konfiguriert."""
    email = os.environ.get("GARDENA_CUSTOMER_EMAIL")
    password = os.environ.get("GARDENA_CUSTOMER_PASSWORD")
    if not email or not password:
        logger.info("gardena_auth.nicht_konfiguriert",
                    hinweis="GARDENA_CUSTOMER_EMAIL/PASSWORD fehlen in .env — DHS-Backfill inaktiv")
        return None
    return GardenaCustomerAuth(email, password, token_datei, sperre_datei)
