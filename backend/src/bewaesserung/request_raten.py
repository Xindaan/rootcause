"""Rollierender Request-Zaehler pro Route (T-0463).

Regressionsschutz gegen die Effekt-Rueckkopplungsklasse aus T-0459: ein
`useEffect`, dessen Dependency-Array einen State enthaelt, den der Effekt
selbst als neues Objekt schreibt, baut sich nach jedem Fetch neu auf und
feuert sofort wieder. Gemessen wurden 35-119 Requests/Sekunde auf einem
einzigen Endpoint, erwartet waren 0,2.

**Warum ein Zaehler und keine Lint-Regel:** `react-hooks/set-state-in-effect`
feuert nur bei synchronem `setState` im Effekt-Body; in beiden T-0459-Faellen
stand der Aufruf in einem async-Callback nach `await` und wurde deshalb nicht
gemeldet. Eine Frontend-Testsuite gibt es bewusst nicht. Der Zaehler prueft
statt der Ursache die Wirkung -- und faengt damit auch Rueckkopplungen, die
gar keine Dependency-Frage sind (Poller doppelt montiert, Retry-Schleife ohne
Backoff, N+1-Fetch pro Zone).

**Was er ausdruecklich NICHT tut:** drosseln, blocken, 429 werfen. Ein
gerissener Deckel ist ein Bug im eigenen Frontend, kein Angriff. Er soll
sichtbar werden, nicht maskiert -- der Sturm aus T-0459 lief wochenlang
unbemerkt, weil die UI dabei voellig korrekt aussah und nichts geloggt hat.
Ratelimiting fuer `control`-Routen macht weiterhin `api_auth`.

Aggregationsebene ist `(Methode, Route-Template)`, nicht der konkrete Pfad:
`GET /api/zonen/{zone_id}/messwerte` statt einmal pro Zone. Sonst waechst die
Schluesselmenge mit den Zonen und ein Sturm auf einer einzelnen Zone
verschwindet im Rauschen (in T-0459 kam der gesamte Sturm aus genau einer).

Zeitbasis ist `time.monotonic()`: fuer eine Rate ist die Wanduhr die falsche
Quelle, ein NTP-Sprung wuerde sie verfaelschen. `jetzt` ist ueberall
injizierbar, damit Tests keine echte Zeit brauchen.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

# Normal pollt das Dashboard im 5-Sekunden-Raster, also 0,2 req/s pro
# Endpoint und Tab. Der Deckel liegt bewusst eine Groessenordnung darueber:
# mehrere offene Tabs, ein Reload-Burst oder ein manueller Klick sollen nicht
# warnen. Die Stuerme aus T-0459 lagen bei 40,5 und 118,9 req/s -- also noch
# einmal eine Groessenordnung ueber dem Deckel. Zwischen "normal" und
# "kaputt" liegen zwei Zehnerpotenzen; der Deckel steht in der Mitte.
DEFAULT_DECKEL_RPS = 2.0

# Routen mit legitim hoeherem Aufkommen.
DECKEL_RPS: dict[str, float] = {
    # SPA-Fallback: liefert die statischen Assets aus `backend/static/`.
    # Ein Full-Reload zieht ein Dutzend Dateien in unter einer Sekunde --
    # das ist kein Poll-Sturm, sondern ein einmaliger Burst.
    "GET /{pfad:path}": 20.0,
}

# Schluessel fuer Requests, die keine Route getroffen haben (404, abgelehnte
# CORS-Preflights). Bewusst EIN Sammel-Eimer: der rohe Pfad kaeme aus einer
# fremden Quelle und wuerde die Schluesselmenge unbegrenzt aufblaehen.
UNBEKANNT = "<unbekannt>"


@dataclass(frozen=True)
class Ueberschreitung:
    """Eine Route ueber ihrem Deckel."""

    route: str
    rate_rps: float
    deckel_rps: float
    anzahl: int
    fenster_s: float

    def als_dict(self) -> dict:
        return {
            "route": self.route,
            "rate_rps": round(self.rate_rps, 2),
            "deckel_rps": self.deckel_rps,
            "anzahl": self.anzahl,
            "fenster_s": round(self.fenster_s, 1),
        }


class RequestRatenZaehler:
    """Zaehlt Requests pro Route in 1-Sekunden-Eimern.

    Nicht threadsicher, und das ist Absicht: die FastAPI-Middleware laeuft im
    Event-Loop, also single-threaded. Ein Lock waere Overhead auf dem
    heissesten Pfad des Servers.
    """

    def __init__(
        self,
        fenster_s: float = 60.0,
        default_deckel_rps: float = DEFAULT_DECKEL_RPS,
        deckel_rps: dict[str, float] | None = None,
        mindest_beobachtung_s: float = 10.0,
        warn_intervall_s: float = 300.0,
        start: float | None = None,
    ) -> None:
        self.fenster_s = fenster_s
        self.default_deckel_rps = default_deckel_rps
        self.deckel_rps = dict(deckel_rps if deckel_rps is not None else DECKEL_RPS)
        # Unter dieser Beobachtungsdauer wird NICHT gemeldet. Sonst reisst ein
        # einzelner Doppelklick in der ersten Sekunde nach dem Start jeden
        # Deckel -- 2 Requests / 0,1 s sind rechnerisch 20 req/s.
        self.mindest_beobachtung_s = mindest_beobachtung_s
        self.warn_intervall_s = warn_intervall_s

        self._start = start if start is not None else time.monotonic()
        # route -> {sekunden_index: anzahl}
        self._eimer: dict[str, dict[int, int]] = {}
        # route -> Requests seit Prozessstart. Monoton steigend, wird nie
        # gekuerzt: darauf misst das Smoke-Check-Skript sein eigenes Fenster
        # per Differenz zweier Abrufe, unabhaengig von `fenster_s`.
        self._gesamt: dict[str, int] = {}
        self._letzte_pruefung: float = self._start
        self._letzte_warnung: dict[str, float] = {}

    # -- Schreiben ---------------------------------------------------------

    def zaehle(self, methode: str, route: str | None, jetzt: float | None = None) -> None:
        """Einen Request verbuchen und hoechstens einmal pro Sekunde pruefen."""
        jetzt = time.monotonic() if jetzt is None else jetzt
        schluessel = f"{methode} {route or UNBEKANNT}"

        eimer = self._eimer.setdefault(schluessel, {})
        index = int(jetzt)
        eimer[index] = eimer.get(index, 0) + 1
        self._gesamt[schluessel] = self._gesamt.get(schluessel, 0) + 1

        # Die Deckel-Pruefung ist O(Routen). Bei 119 req/s waere sie pro
        # Request reine Verschwendung -- einmal pro Sekunde reicht, um einen
        # Sturm zu sehen, der ueber Minuten laeuft.
        if jetzt - self._letzte_pruefung >= 1.0:
            self._letzte_pruefung = jetzt
            self._aufraeumen(jetzt)
            for u in self.ueberschreitungen(jetzt):
                self._warne(u, jetzt)

    def _aufraeumen(self, jetzt: float) -> None:
        grenze = int(jetzt) - int(self.fenster_s)
        for schluessel, eimer in list(self._eimer.items()):
            for index in [i for i in eimer if i <= grenze]:
                del eimer[index]
            if not eimer:
                del self._eimer[schluessel]

    def _warne(self, u: Ueberschreitung, jetzt: float) -> None:
        letzte = self._letzte_warnung.get(u.route)
        if letzte is not None and jetzt - letzte < self.warn_intervall_s:
            return
        self._letzte_warnung[u.route] = jetzt
        # Gedrosselt, weil ein Sturm sonst genau das Log flutet, das ohnehin
        # unbegrenzt waechst (T-0461).
        logger.warning(
            "request_raten.deckel_gerissen",
            route=u.route,
            rate_rps=round(u.rate_rps, 2),
            deckel_rps=u.deckel_rps,
            anzahl=u.anzahl,
            fenster_s=round(u.fenster_s, 1),
            hinweis=(
                "Verdacht auf Frontend-Rueckkopplung (Klasse T-0459): ein "
                "Effekt schreibt State, der in seinen eigenen Dependencies "
                "steht. Pruefen mit backend/skripte/pruefe_request_raten.py"
            ),
        )

    # -- Lesen -------------------------------------------------------------

    def _beobachtungsdauer(self, jetzt: float) -> float:
        """Nenner der Rate.

        Frisch nach dem Start ist das Fenster noch nicht voll. Dann durch
        `fenster_s` zu teilen, wuerde einen laufenden Sturm um bis zu Faktor
        60 kleinrechnen -- also genau in dem Moment blind sein, in dem ein
        Reload das Frontend gerade neu montiert hat.
        """
        return min(self.fenster_s, max(jetzt - self._start, 0.0))

    def deckel_fuer(self, route: str) -> float:
        return self.deckel_rps.get(route, self.default_deckel_rps)

    def raten(self, jetzt: float | None = None) -> dict[str, dict]:
        jetzt = time.monotonic() if jetzt is None else jetzt
        self._aufraeumen(jetzt)
        dauer = self._beobachtungsdauer(jetzt)
        ergebnis: dict[str, dict] = {}
        for schluessel, eimer in self._eimer.items():
            anzahl = sum(eimer.values())
            ergebnis[schluessel] = {
                "anzahl": anzahl,
                "rate_rps": (anzahl / dauer) if dauer > 0 else 0.0,
                "deckel_rps": self.deckel_fuer(schluessel),
                "gesamt": self._gesamt.get(schluessel, 0),
            }
        return ergebnis

    def ueberschreitungen(self, jetzt: float | None = None) -> list[Ueberschreitung]:
        jetzt = time.monotonic() if jetzt is None else jetzt
        dauer = self._beobachtungsdauer(jetzt)
        if dauer < self.mindest_beobachtung_s:
            return []
        treffer = [
            Ueberschreitung(
                route=schluessel,
                rate_rps=werte["rate_rps"],
                deckel_rps=werte["deckel_rps"],
                anzahl=werte["anzahl"],
                fenster_s=dauer,
            )
            for schluessel, werte in self.raten(jetzt).items()
            if werte["rate_rps"] > werte["deckel_rps"]
        ]
        return sorted(treffer, key=lambda u: u.rate_rps, reverse=True)

    def schnappschuss(self, jetzt: float | None = None) -> dict:
        """Antwortkoerper fuer `/api/ops/request-raten`."""
        jetzt = time.monotonic() if jetzt is None else jetzt
        raten = self.raten(jetzt)
        ueber = self.ueberschreitungen(jetzt)
        return {
            "fenster_s": self.fenster_s,
            "beobachtet_s": round(self._beobachtungsdauer(jetzt), 1),
            "laufzeit_s": round(jetzt - self._start, 1),
            "default_deckel_rps": self.default_deckel_rps,
            "mindest_beobachtung_s": self.mindest_beobachtung_s,
            # `gesamt` ist die Basis fuer die Differenzmessung im Skript --
            # `rate_rps` haengt am Serverfenster, `gesamt` an gar nichts.
            "routen": {
                schluessel: {
                    "anzahl": werte["anzahl"],
                    "rate_rps": round(werte["rate_rps"], 3),
                    "deckel_rps": werte["deckel_rps"],
                    "gesamt": werte["gesamt"],
                }
                for schluessel, werte in sorted(
                    raten.items(), key=lambda p: p[1]["rate_rps"], reverse=True
                )
            },
            "ueberschreitungen": [u.als_dict() for u in ueber],
            "ok": not ueber,
        }


# Prozessweiter Zaehler. Tests bauen sich eigene Instanzen.
zaehler = RequestRatenZaehler()
