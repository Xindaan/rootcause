#!/usr/bin/env python3
"""Smoke-Check: Request-Rate pro Route gegen die laufende App (T-0463).

Regressionsschutz gegen die Effekt-Rueckkopplungsklasse aus T-0459 (35-119
req/s aus einem einzigen `useEffect`). Hintergrund im Modul-Docstring von
`bewaesserung/request_raten.py`.

Ablauf: zweimal `GET /api/ops/request-raten` im Abstand von `--dauer`
Sekunden, Rate je Route aus der Differenz der `gesamt`-Zaehler. Der Server
liefert zwar auch eine eigene `rate_rps`, aber die haengt an seinem
60-Sekunden-Fenster; die Differenzmessung misst genau das Fenster, das hier
beobachtet wird, und ist damit gegen Fenster-Aenderungen im Server immun.

**Nur GET.** Das Skript startet kein Wasser und fasst kein Ventil an
(Tabu-Liste in CLAUDE.md).

Damit es etwas sieht, muss waehrend der Messung das Dashboard offen sein --
sonst pollt niemand und alle Raten sind null. Genau dafuer ist `--mindest-
requests` da: ohne Last bricht der Lauf mit Exit 2 ab, statt gruen zu melden
und damit eine Regression zu verstecken.

Beispiel:

    ../../.venv/bin/python pruefe_request_raten.py --dauer 30 \\
        --api-key "$GARDENA_READ_KEY"

Exit-Codes: 0 = alle Routen unter dem Deckel, 1 = Deckel gerissen,
2 = Messung nicht aussagekraeftig (Backend weg, keine Last, Auth-Fehler).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

PFAD = "/api/ops/request-raten"


class EndpointFehlt(Exception):
    """Backend antwortet, kennt den Endpoint aber nicht."""


def hole(basis_url: str, api_key: str | None, timeout: float) -> tuple[dict, float]:
    """Schnappschuss abrufen. Zweiter Rueckgabewert ist der Empfangszeitpunkt."""
    anfrage = urllib.request.Request(basis_url.rstrip("/") + PFAD, method="GET")
    if api_key:
        anfrage.add_header("X-Api-Key", api_key)
    with urllib.request.urlopen(anfrage, timeout=timeout) as antwort:
        roh = antwort.read().decode("utf-8")
    # Nach dem Lesen stempeln: die Netzlatenz gehoert nicht ins Messfenster.
    jetzt = time.monotonic()

    # Ein Backend mit altem Code antwortet hier NICHT mit 404: der
    # SPA-Fallback (`GET /{pfad:path}`) faengt jeden unbekannten Pfad ab und
    # liefert 200 + index.html. Ohne diesen Guard scheitert das Skript mit
    # einem JSONDecodeError statt zu sagen, was los ist.
    try:
        daten = json.loads(roh)
    except json.JSONDecodeError:
        raise EndpointFehlt("Antwort ist kein JSON (vermutlich der SPA-Fallback)") from None
    if not isinstance(daten, dict) or "routen" not in daten:
        raise EndpointFehlt("Antwort hat kein Feld `routen`")
    return daten, jetzt


def rate_je_route(vorher: dict, nachher: dict, dauer_s: float) -> dict[str, float]:
    """Rate aus der Differenz der kumulativen Zaehler.

    Routen, die im ersten Schnappschuss fehlen, starten bei 0 -- ein Endpoint,
    der erst waehrend der Messung zum ersten Mal getroffen wird, faellt sonst
    genau dann durchs Raster, wenn sein Sturm gerade anlaeuft.
    """
    a = {k: v["gesamt"] for k, v in vorher.get("routen", {}).items()}
    b = {k: v["gesamt"] for k, v in nachher.get("routen", {}).items()}
    return {
        route: max(gesamt - a.get(route, 0), 0) / dauer_s
        for route, gesamt in b.items()
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--basis-url", default="http://127.0.0.1:8090")
    p.add_argument("--dauer", type=float, default=30.0, help="Messfenster in Sekunden (Default 30)")
    p.add_argument(
        "--api-key",
        default=os.environ.get("GARDENA_READ_KEY") or os.environ.get("GARDENA_API_KEY"),
        help="X-Api-Key mit Rolle read. Default aus GARDENA_READ_KEY / GARDENA_API_KEY.",
    )
    p.add_argument(
        "--mindest-requests",
        type=int,
        default=5,
        help="Unter so vielen Requests im Fenster gilt die Messung als leer (Exit 2).",
    )
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--json", action="store_true", help="Ergebnis als JSON statt als Tabelle.")
    args = p.parse_args()

    try:
        vorher, t0 = hole(args.basis_url, args.api_key, args.timeout)
    except EndpointFehlt as e:
        print(
            f"FEHLER: {PFAD} liefert keinen Schnappschuss ({e}). Das laufende Backend "
            "kennt den Endpoint nicht -- Neustart mit dem aktuellen Code noetig (T-0463).",
            file=sys.stderr,
        )
        return 2
    except urllib.error.HTTPError as e:
        print(f"FEHLER: {PFAD} antwortet {e.code} ({e.reason}). Read-Token gesetzt?", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError) as e:
        print(f"FEHLER: Backend unter {args.basis_url} nicht erreichbar: {e}", file=sys.stderr)
        return 2

    print(f"Messe {args.dauer:.0f} s ... (Dashboard-Tab muss offen sein, sonst pollt niemand)", file=sys.stderr)
    time.sleep(args.dauer)

    try:
        nachher, t1 = hole(args.basis_url, args.api_key, args.timeout)
    except (EndpointFehlt, urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        print(f"FEHLER: zweiter Abruf fehlgeschlagen: {e}", file=sys.stderr)
        return 2

    dauer = max(t1 - t0, 0.001)
    raten = rate_je_route(vorher, nachher, dauer)
    deckel = {k: v["deckel_rps"] for k, v in nachher.get("routen", {}).items()}
    default_deckel = nachher.get("default_deckel_rps", 2.0)

    # Die zwei eigenen Abrufe rausrechnen -- sonst misst das Skript sich selbst.
    eigener_schluessel = f"GET {PFAD}"
    if eigener_schluessel in raten:
        raten[eigener_schluessel] = max(raten[eigener_schluessel] - 1.0 / dauer, 0.0)

    gesamt_requests = sum(round(r * dauer) for r in raten.values())
    risse = sorted(
        (
            (route, rate, deckel.get(route, default_deckel))
            for route, rate in raten.items()
            if rate > deckel.get(route, default_deckel)
        ),
        key=lambda t: t[1],
        reverse=True,
    )

    if args.json:
        print(json.dumps({
            "dauer_s": round(dauer, 2),
            "gesamt_requests": gesamt_requests,
            "raten_rps": {k: round(v, 3) for k, v in sorted(raten.items(), key=lambda p: -p[1])},
            "ueberschreitungen": [
                {"route": r, "rate_rps": round(v, 2), "deckel_rps": d} for r, v, d in risse
            ],
            "ok": not risse,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"\nFenster {dauer:.1f} s, {gesamt_requests} Requests gesamt\n")
        print(f"{'Route':<50} {'req/s':>8} {'Deckel':>8}")
        for route, rate in sorted(raten.items(), key=lambda p: -p[1]):
            if rate <= 0:
                continue
            d = deckel.get(route, default_deckel)
            marke = "  <-- DECKEL GERISSEN" if rate > d else ""
            print(f"{route:<50} {rate:>8.2f} {d:>8.1f}{marke}")

    if gesamt_requests < args.mindest_requests:
        print(
            f"\nUNKLAR: nur {gesamt_requests} Requests im Fenster. Ohne Last sagt der Lauf "
            "nichts aus -- Dashboard oeffnen und wiederholen.",
            file=sys.stderr,
        )
        return 2

    if risse:
        print(
            f"\nFEHLGESCHLAGEN: {len(risse)} Route(n) ueber dem Deckel. Verdacht auf "
            "Rueckkopplung der Klasse T-0459 (Effekt schreibt State, der in seinen "
            "eigenen Dependencies steht).",
            file=sys.stderr,
        )
        return 1

    print("\nOK: alle Routen unter ihrem Deckel.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
