"""API-Auth-Layer (T-0140).

Header `X-Api-Key` mit Rollen `read`/`control`. Schluessel werden
gehasht (scrypt) auf Disk persistiert. Eine zentrale Route-Matrix
(ROUTE_ROLLEN) ist Source-of-Truth: jede FastAPI-Route muss dort einen
Eintrag haben, sonst failt `validiere_route_matrix(app)` beim Startup
und die Auth-Dependency wirft 500 fuer Matrix-Luecken zur Laufzeit.

Auth-Fehler:
- fehlt Header / unbekannter Key  -> 401
- Key vorhanden, aber falsche Rolle -> 403
- Ratelimit (control) ueberschritten -> 429

CLI: ``python -m bewaesserung.api_auth schluessel-erstellen
--rolle control --label iphone-andre``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request

Rolle = Literal["public", "read", "control"]

# Route-Matrix (Method, Pfad-Pattern) -> Rolle. Source-of-Truth.
# `validiere_route_matrix(app)` prueft beim Startup ob jede Route hier
# steht und blockt den Start sonst -- damit kann ein neuer Endpoint
# nicht versehentlich offen bleiben (Codex-Empfehlung Reg-Schutz).
ROUTE_ROLLEN: dict[tuple[str, str], Rolle] = {
    # --- Public (kein Token noetig) ---
    ("GET", "/api/health"): "public",
    ("GET", "/{pfad:path}"): "public",  # SPA-Fallback bei vorhandenem static-Dir
    ("GET", "/"): "public",  # Kein-Dashboard-Fallback
    # FastAPI-Defaults (docs/redoc abgeschaltet -- /openapi.json bleibt
    # fuer Mobile-Codegen erreichbar, daher read-geschuetzt).
    ("GET", "/openapi.json"): "read",
    # --- Read (alle fachlichen GETs) ---
    ("GET", "/api/health/detail"): "read",
    ("GET", "/api/zonen"): "read",
    ("GET", "/api/zonen/{zone_id}/messwerte"): "read",
    ("GET", "/api/zonen/{zone_id}/ereignisse"): "read",
    ("GET", "/api/zonen/{zone_id}/giess-historie"): "read",
    ("GET", "/api/giess-historie"): "read",
    ("GET", "/api/zonen/{zone_id}/empfehlung-jetzt"): "read",
    # T-0200: aggregierter Bulk-Endpoint fuer den V2-Loader.
    ("GET", "/api/dashboard-snapshot"): "read",
    ("GET", "/api/entscheidungen"): "read",
    ("GET", "/api/kalibrierung/{zone_id}"): "read",
    ("GET", "/api/schwellen-vorschlag"): "read",
    ("GET", "/api/zonen/{zone_id}/bilanz"): "read",
    ("GET", "/api/prognose"): "read",
    ("GET", "/api/wetter"): "read",
    ("GET", "/api/wetter/{standort_id}"): "read",
    ("GET", "/api/standorte"): "read",
    ("GET", "/api/ops/summary"): "read",
    ("GET", "/api/ops/betriebsstatus"): "read",  # T-0238
    ("GET", "/api/ops/timeline"): "read",
    ("GET", "/api/pflege-erinnerungen"): "read",  # T-0228 Stufe 1
    ("POST", "/api/pflege-erinnerungen"): "control",
    ("POST", "/api/pflege-erinnerungen/{eintrag_id}/erledigen"): "control",
    ("GET", "/api/tagesplan"): "read",  # T-0227
    ("GET", "/api/wartungs-fenster"): "read",  # T-0228 Stufe 2
    ("POST", "/api/wartungs-fenster"): "control",
    ("POST", "/api/wartungs-fenster/{fenster_id}/beenden"): "control",
    ("GET", "/api/ventil/pre-soak-status"): "read",
    ("GET", "/api/ventil-ereignisse"): "read",
    ("GET", "/api/ventil-status"): "read",
    ("GET", "/api/ml/status"): "read",
    ("GET", "/api/ml/vorhersage/{zone_id}"): "read",
    ("GET", "/api/ml/drift"): "read",
    ("GET", "/api/ml/drift/log"): "read",
    ("GET", "/api/ml/dauer-drift"): "read",
    ("GET", "/api/ml/physik-bias"): "read",
    ("GET", "/api/empfehlungs-audit"): "read",
    # --- Control (alle mutierenden Routen) ---
    ("POST", "/api/giessen"): "control",
    ("POST", "/api/ventil/manuell-start"): "control",
    ("POST", "/api/ventil/manuell-stop"): "control",
    ("POST", "/api/ventil/pre-soak-start"): "control",
    ("POST", "/api/ventil/pre-soak-stop"): "control",
    ("PATCH", "/api/ventil-ereignis/{ereignis_id}"): "control",
    ("DELETE", "/api/ventil-ereignis/{ereignis_id}"): "control",
    ("POST", "/api/ventil-ereignisse/klassifiziere-bulk"): "control",
    ("POST", "/api/notfall-stopp"): "control",
}


def _ermittle_schluessel_pfad() -> Path:
    override = os.environ.get("BEWAESSERUNG_API_KEYS_PFAD")
    if override:
        return Path(override)
    return (
        Path.home() / "Library" / "Application Support"
        / "de.xindaan.pflanzen-dashboard" / "api_keys.json"
    )


# scrypt-Parameter (Python-Doku-Default).
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _hashe(klartext: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        klartext.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )


@dataclass
class SchluesselEintrag:
    id: str
    rolle: Rolle
    salt: bytes
    hash: bytes
    erstellt: datetime

    def passt_zu(self, kandidat: str) -> bool:
        kandidat_hash = _hashe(kandidat, self.salt)
        return secrets.compare_digest(kandidat_hash, self.hash)


@dataclass
class SchluesselSpeicher:
    pfad: Path
    eintraege: list[SchluesselEintrag] = field(default_factory=list)

    @classmethod
    def laden(cls, pfad: Path) -> "SchluesselSpeicher":
        if not pfad.exists():
            return cls(pfad=pfad)
        with pfad.open("r", encoding="utf-8") as f:
            roh = json.load(f)
        eintraege = [
            SchluesselEintrag(
                id=e["id"],
                rolle=e["rolle"],
                salt=bytes.fromhex(e["salt"]),
                hash=bytes.fromhex(e["hash"]),
                erstellt=datetime.fromisoformat(e["erstellt"]),
            )
            for e in roh.get("schluessel", [])
        ]
        return cls(pfad=pfad, eintraege=eintraege)

    def speichere(self) -> None:
        self.pfad.parent.mkdir(parents=True, exist_ok=True)
        roh = {
            "version": 1,
            "schluessel": [
                {
                    "id": e.id,
                    "rolle": e.rolle,
                    "salt": e.salt.hex(),
                    "hash": e.hash.hex(),
                    "erstellt": e.erstellt.isoformat(),
                }
                for e in self.eintraege
            ],
        }
        # atomic write + 0600
        tmp = self.pfad.with_suffix(self.pfad.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(roh, f, indent=2)
        os.chmod(tmp, 0o600)
        tmp.replace(self.pfad)

    def finde(self, kandidat: str) -> SchluesselEintrag | None:
        for e in self.eintraege:
            if e.passt_zu(kandidat):
                return e
        return None

    def hinzufuegen(self, label: str, rolle: Rolle, klartext: str) -> None:
        if rolle not in ("read", "control"):
            raise ValueError(f"Rolle muss 'read' oder 'control' sein, war {rolle!r}")
        salt = secrets.token_bytes(16)
        self.eintraege.append(
            SchluesselEintrag(
                id=label,
                rolle=rolle,
                salt=salt,
                hash=_hashe(klartext, salt),
                erstellt=datetime.now(),
            )
        )


# Modul-globale Caches (lazy)
_speicher: SchluesselSpeicher | None = None


def _hole_speicher() -> SchluesselSpeicher:
    global _speicher
    if _speicher is None:
        _speicher = SchluesselSpeicher.laden(_ermittle_schluessel_pfad())
    return _speicher


# --- Auth-Validierungs-Cache (T-0217) -------------------------------
#
# Problem: `SchluesselSpeicher.finde()` ruft `passt_zu()` -> scrypt
# (N=2^14). Gemessen ~36 ms/Hash. Ohne Cache lief das bei JEDEM
# API-Request — synchron im asyncio-Event-Loop. Bei einem pollenden
# Dashboard (Dutzende Requests/min) saturierte das die CPU und
# blockierte den Event-Loop dauerhaft; `/api/zonen` (28 sequentielle
# DB-awaits) stieg dadurch auf 30-390 s Latenz.
#
# Fix zweistufig:
#  1. Positiv-Cache `klartext-key -> SchluesselEintrag`. Der erste
#     Request pro Key hasht, jeder folgende trifft den Cache. Den
#     Klartext-Key im RAM zu halten ist unkritisch — er steht ohnehin
#     im Request-Header und im Frontend-`.env.local`; scrypt-at-rest
#     schuetzt nur die `api_keys.json`-Datei, nicht den Laufzeit-RAM.
#  2. Cache-Miss laeuft via `run_in_executor` in einem Thread —
#     scrypt gibt den GIL waehrend der OpenSSL-Berechnung frei, der
#     Event-Loop bleibt also frei. Plus Negativ-Cache mit TTL gegen
#     wiederholte Misses (z.B. ein falsch konfigurierter Client).
_auth_cache: dict[str, SchluesselEintrag] = {}
_auth_negativ_cache: dict[str, float] = {}
_NEGATIV_TTL_S = 60.0
_NEGATIV_MAX = 256


def _leere_auth_cache() -> None:
    _auth_cache.clear()
    _auth_negativ_cache.clear()


async def _finde_mit_cache(api_key: str) -> SchluesselEintrag | None:
    """Cache-gestuetzter Key-Lookup. scrypt nur bei echtem Cache-Miss,
    und dann im Thread-Executor (blockiert den Event-Loop nicht)."""
    treffer = _auth_cache.get(api_key)
    if treffer is not None:
        return treffer
    jetzt = time.monotonic()
    ablauf = _auth_negativ_cache.get(api_key)
    if ablauf is not None and ablauf > jetzt:
        return None
    # Cache-Miss: scrypt-Loop in den Default-ThreadPool auslagern.
    speicher = _hole_speicher()
    loop = asyncio.get_running_loop()
    eintrag = await loop.run_in_executor(None, speicher.finde, api_key)
    if eintrag is not None:
        _auth_cache[api_key] = eintrag
    else:
        # Negativ-Cache begrenzen: bei Ueberlauf komplett leeren
        # (einfach + ausreichend, echte Keys sind im Positiv-Cache).
        if len(_auth_negativ_cache) >= _NEGATIV_MAX:
            _auth_negativ_cache.clear()
        _auth_negativ_cache[api_key] = jetzt + _NEGATIV_TTL_S
    return eintrag


def setze_speicher_fuer_tests(speicher: SchluesselSpeicher | None) -> None:
    """Test-Hook. Setzt den globalen Speicher und leert den Auth-Cache."""
    global _speicher
    _speicher = speicher
    _leere_auth_cache()


# Ratelimiter: Sliding-Window per Schluessel-ID. Default 5/min/key.
_RATELIMIT_FENSTER_S = 60.0
_RATELIMIT_MAX_CALLS = 5


@dataclass
class Ratelimiter:
    fenster_s: float = _RATELIMIT_FENSTER_S
    max_calls: int = _RATELIMIT_MAX_CALLS
    historie: dict[str, deque[float]] = field(default_factory=dict)

    def akzeptiere(self, schluessel_id: str, jetzt: float | None = None) -> bool:
        if jetzt is None:
            jetzt = time.monotonic()
        h = self.historie.setdefault(schluessel_id, deque())
        while h and h[0] < jetzt - self.fenster_s:
            h.popleft()
        if len(h) >= self.max_calls:
            return False
        h.append(jetzt)
        return True

    def reset(self) -> None:
        self.historie.clear()


_ratelimiter = Ratelimiter()


def setze_ratelimiter_fuer_tests(rl: Ratelimiter) -> None:
    global _ratelimiter
    _ratelimiter = rl


def _route_aus_request(request: Request) -> tuple[str, str] | None:
    route = request.scope.get("route")
    if route is None:
        return None
    pfad = getattr(route, "path", None)
    if not pfad:
        return None
    return (request.method.upper(), pfad)


async def auth_dependency(request: Request) -> None:
    """Globale FastAPI-Dependency, die jede Route ueber ROUTE_ROLLEN gatet.

    Wird in `api_server.py` als `dependencies=[Depends(auth_dependency)]`
    am `FastAPI`-Konstruktor registriert. Greift damit fuer alle Routen
    der App.
    """
    method = request.method.upper()
    # OPTIONS-Preflight (CORS) und HEAD ohne Matrix-Check durchlassen --
    # die Matrix listet nur fachliche GET/POST/PATCH/DELETE.
    if method in ("OPTIONS", "HEAD"):
        return
    schluessel = _route_aus_request(request)
    if schluessel is None:
        return  # kein matched Route -> FastAPI 404
    rolle_noetig = ROUTE_ROLLEN.get(schluessel)
    if rolle_noetig is None:
        method, pfad = schluessel
        raise HTTPException(
            status_code=500,
            detail=f"auth_matrix_luecke: ({method} {pfad}) nicht in ROUTE_ROLLEN",
        )
    if rolle_noetig == "public":
        return
    api_key = request.headers.get("X-Api-Key") or request.headers.get("x-api-key")
    if not api_key:
        raise HTTPException(status_code=401, detail="X-Api-Key fehlt")
    # T-0217: cache-gestuetzt + scrypt im Executor statt synchron im
    # Event-Loop — siehe `_finde_mit_cache`.
    eintrag = await _finde_mit_cache(api_key)
    if eintrag is None:
        raise HTTPException(status_code=401, detail="API-Key ungueltig")
    if rolle_noetig == "control" and eintrag.rolle != "control":
        # read-Key auf control-Endpoint -> 403
        raise HTTPException(status_code=403, detail="rolle_unzureichend")
    # read-Endpoints akzeptieren read+control gleichermassen.
    if rolle_noetig == "control":
        if not _ratelimiter.akzeptiere(eintrag.id):
            raise HTTPException(status_code=429, detail="ratelimit_ueberschritten")


def validiere_route_matrix(app: FastAPI) -> list[tuple[str, str]]:
    """Prueft, ob alle FastAPI-Routes in ROUTE_ROLLEN stehen.

    Liefert die fehlenden (method, path)-Paare. Beim Startup ruft
    `konfiguriere_api` das auf und failed wenn die Liste nicht leer ist.
    """
    fehlt: list[tuple[str, str]] = []
    for route in app.routes:
        pfad = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not pfad or not methods:
            continue
        for method in methods:
            method = method.upper()
            if method in ("HEAD", "OPTIONS"):
                continue
            if (method, pfad) not in ROUTE_ROLLEN:
                fehlt.append((method, pfad))
    return fehlt


# --- CLI ---

def _cli_schluessel_erstellen(rolle: str, label: str) -> str:
    if rolle not in ("read", "control"):
        raise SystemExit(f"Rolle muss 'read' oder 'control' sein, war: {rolle!r}")
    pfad = _ermittle_schluessel_pfad()
    speicher = SchluesselSpeicher.laden(pfad)
    if any(e.id == label for e in speicher.eintraege):
        raise SystemExit(f"Label '{label}' existiert bereits.")
    klartext = secrets.token_urlsafe(32)
    speicher.hinzufuegen(label=label, rolle=rolle, klartext=klartext)
    speicher.speichere()
    return klartext


def _cli_liste() -> None:
    speicher = SchluesselSpeicher.laden(_ermittle_schluessel_pfad())
    if not speicher.eintraege:
        print("(keine Schluessel registriert)")
        return
    print(f"{'LABEL':<24} {'ROLLE':<10} {'ERSTELLT':<20}")
    for e in speicher.eintraege:
        print(f"{e.id:<24} {e.rolle:<10} {e.erstellt.isoformat()}")


def _cli_loeschen(label: str) -> None:
    pfad = _ermittle_schluessel_pfad()
    speicher = SchluesselSpeicher.laden(pfad)
    vorher = len(speicher.eintraege)
    speicher.eintraege = [e for e in speicher.eintraege if e.id != label]
    nachher = len(speicher.eintraege)
    if vorher == nachher:
        raise SystemExit(f"Kein Schluessel mit Label '{label}' gefunden.")
    speicher.speichere()
    print(f"Schluessel '{label}' geloescht.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m bewaesserung.api_auth",
        description="API-Schluessel verwalten (T-0140 Auth-Layer).",
    )
    sub = parser.add_subparsers(dest="befehl", required=True)
    erstellen = sub.add_parser(
        "schluessel-erstellen", help="Neuen API-Key erzeugen",
    )
    erstellen.add_argument("--rolle", required=True, choices=["read", "control"])
    erstellen.add_argument(
        "--label", required=True,
        help="Identifier z.B. iphone-andre, dev-frontend",
    )
    sub.add_parser("liste", help="Alle registrierten Schluessel anzeigen")
    loeschen = sub.add_parser("loeschen", help="Schluessel per Label entfernen")
    loeschen.add_argument("--label", required=True)

    args = parser.parse_args()
    if args.befehl == "schluessel-erstellen":
        klartext = _cli_schluessel_erstellen(args.rolle, args.label)
        print(f"Neuer API-Key fuer '{args.label}' (rolle={args.rolle}):")
        print(f"  {klartext}")
        print()
        print("WICHTIG: dieser Schluessel wird NUR EINMAL angezeigt.")
        print("Speichere ihn jetzt im Keychain / Passwort-Manager.")
        print(f"Datei: {_ermittle_schluessel_pfad()}")
    elif args.befehl == "liste":
        _cli_liste()
    elif args.befehl == "loeschen":
        _cli_loeschen(args.label)


if __name__ == "__main__":
    main()
