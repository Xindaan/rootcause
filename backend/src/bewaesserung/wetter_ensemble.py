"""T-0423: Regen-Ensemble statt Punktwert.

**Warum.** Die Steuerung liest heute einen deterministischen Punktwert durch
harte Schwellen. Fuer den Zieltag 23.07. lieferte dieselbe Open-Meteo-Quelle am
22.07. innerhalb von 5,5 h 13,2 -> 8,8 -> 1,7 mm. Faktor 8. Ob eine Zone
gegossen wurde, haette davon abgehangen, welchen Modelllauf wir zufaellig
gelesen haben. Bei konvektivem Sommerregen ist das kein Modellfehler, sondern
die Natur der Sache: der Punktwert ist EINE Stichprobe aus einer breiten
Verteilung.

**Die Kernregel: erst pro Member summieren, dann das Perzentil.**
Ein Perzentil pro Stunde zu bilden und die Stunden zu addieren ergibt keinen
realen Verlauf -- man mischt Member, die zu verschiedenen Zeiten regnen, zu
einem Phantom-Member, den es nie gab. Deshalb `perzentil_der_membersummen`.

**Warum p20 und nicht ein fester Abschlag.** Cost-Loss: `p* = C/L` mit
C = unnoetiger Guss (billig) und L = ausgefallener Guss auf Sand mit ~8 mm RAW
(teuer) -> C/L ~0,1-0,2. Ein fester Multiplikator (`regen_mm * 0.5`) kann das
nicht leisten, weil er den SPREAD ignoriert. p20 ist verteilungssensitiv:
einiges Ensemble (Landregen) -> p20 nahe Median -> Skip erlaubt; uneiniges
Ensemble (Konvektion) -> p20 kollabiert -> es wird gegossen.

**Verifiziert 22.07.2026** (Standort aus der Konfig, 48 h, icon_d2_eps,
20 Member):
deterministisch 0,1 + 1,7 mm; Ensemble min 0,5 / p20 1,2 / Median 2,7 /
p75 4,4 / max 10,5 mm, P(>1 mm) = 0,80. Der deterministische Wert liegt
unterhalb des p25 des Ensembles.

**`precipitation`, nicht `rain`** -- die Begruendung hat sich geaendert:
Die Task-Notiz nannte "rain ist bei icon_d2_eps komplett None (Issue #457)".
Am 22.07. nachgeprueft: **das stimmt nicht mehr**, 0 von 20 rain-Spalten sind
None. `precipitation` bleibt trotzdem richtig, aus einem anderen Grund:
`rain` schliesst `showers` aus. Member 0 hatte rain 1,50 gegen precipitation
1,80, und nur 3 von 20 Membern waren identisch. Bei konvektivem Sommerregen
steckt ein erheblicher Teil in `showers` -- mit `rain` wuerden wir den Regen
systematisch unterschaetzen und zu oft giessen.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from datetime import datetime

import structlog

logger = structlog.get_logger()

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
# icon_d2_eps: 20 Member, ~48 h Horizont -- passt exakt auf unseren
# Entscheidungs-Horizont. Groessere Modelle (icon_eu_eps) haetten mehr
# Vorlauf, aber groebere Aufloesung fuer Konvektion.
MODELL = "icon_d2_eps"


@dataclass(frozen=True)
class RegenEnsemble:
    """Verteilung der 24-h-Regensummen ueber alle Ensemble-Member."""
    p10: float
    p20: float
    p50: float
    p90: float
    minimum: float
    maximum: float
    n_member: int
    wahrsch_ueber_1mm: float

    @property
    def konservativ_mm(self) -> float:
        """Der Wert, mit dem die Bilanz rechnet (T-0422)."""
        return self.p20

    @property
    def spread(self) -> float:
        """p90 - p10. Gross = uneiniges Ensemble = Konvektion."""
        return self.p90 - self.p10


def _perzentil(sortiert: list[float], p: float) -> float:
    """Nearest-Rank-Perzentil.

    T-0564: vorher `sortiert[int(p * n)]` -- das ist systematisch ein Rang
    zu hoch. An 20 Membern mit den Werten 1..20 gemessen: p10 lieferte 3,0
    statt 2,0, p20 5,0 statt 4,0, p90 19,0 statt 18,0.

    Fuer `p20` ist das die falsche Richtung. Der Wert wird ausdruecklich als
    der konservative Trocken-Fall benutzt; zu hoch heisst "mehr Regen
    erwartet" und damit "weniger giessen" -- gegen
    [[feedback_sand_lieber_zu_viel_giessen]]. Nearest-Rank ist
    `ceil(p * n) - 1`, mindestens 0.
    """
    if not sortiert:
        return 0.0
    n = len(sortiert)
    rang = math.ceil(p * n) - 1
    return sortiert[max(0, min(n - 1, rang))]


def zukunfts_indizes(
    hourly: dict, stunden: int, jetzt: datetime | None = None,
) -> list[int]:
    """Indizes der naechsten `stunden` Eintraege AB `jetzt`, chronologisch.

    **T-0425 (05.08.2026), sechster Fall der T-0508-Klasse.** Vorher stand
    hier `hourly[k][:stunden]` ohne jeden Zeitfilter. Open-Meteo liefert bei
    `forecast_days=2` ab **heute 00:00 Ortszeit** -- die ersten 24 Eintraege
    sind also der KALENDERTAG, unabhaengig von der Uhrzeit. Gemessen am
    05.08. um 20:42: `[:24]` endete um 23:00 desselben Tags, 20 der
    24 Stunden lagen in der Vergangenheit.

    Der Schaden traf nicht die Bewaesserung -- `entscheidung_mit_ensemble`
    laeuft ausschliesslich im Shadow (`wasserbilanz_job`) --, sondern die
    MESSUNG: der Job nimmt die letzte Abfrage des Bilanztags D und
    entscheidet damit ueber Tag D+1, waehrend das Fenster Tag D beschreibt.
    Die Prognose hinkte der Entscheidung um einen vollen Tag hinterher.
    Nachgewiesen an Bright Sky statt behauptet (T-0425-Auswertung): der p50
    dieser Abfragen traf den Regen des Abfragetags mit MAE 0,67 mm
    (Gartenstandort) bzw. 1,38 mm (Balkonstandort), den des Entscheidungstags
    nur mit 2,72 bzw. 6,87 mm.

    Der T-0508-Isomorphie-Check fand diese Stelle nicht, weil er auf
    `\\.stunden\\[` grepte -- hier wird auf dem ROHEN API-Block gesliced,
    nicht auf `WetterVorhersage`.

    **`> jetzt`, nicht `>= Stundenanfang`** -- dieselbe Konvention wie
    `WetterVorhersage.zukunftsstunden` (modelle.py:298). Die angebrochene
    Stunde faellt heraus; das unterschaetzt kommenden Regen um hoechstens
    eine Stunde, und das ist die sichere Richtung (zu wenig gesehener Regen
    laesst giessen -- billig; zu viel gesehener sperrt -- teuer).

    Ohne `time`-Spalte gibt es keinen Zeitfilter, dann bleibt der
    Listenanfang. Das ist bewusst: die Alternative waere, gar nichts zu
    liefern und damit das Ensemble-Signal ganz zu verlieren.
    """
    zeiten = hourly.get("time")
    if not zeiten:
        return list(range(stunden))
    jetzt = jetzt or datetime.now()
    passend: list[tuple[str, int]] = []
    for i, z in enumerate(zeiten):
        try:
            wann = datetime.fromisoformat(str(z))
        except ValueError:
            continue
        if wann > jetzt:
            passend.append((str(z), i))
    passend.sort()
    return [i for _, i in passend[:stunden]]


def brauchbare_indizes(
    hourly: dict, stunden: int, variable: str = "precipitation",
    jetzt: datetime | None = None,
) -> list[int] | None:
    """Wie `zukunfts_indizes`, aber `None`, sobald das Fenster nicht VOLL ist.

    Zwei Gruende, warum das eine eigene Pruefung braucht und kein Detail ist:

    1. **Slots.** Ab `jetzt` bleiben nur `Ende - jetzt` Stunden uebrig. Am
       Abend reicht das nicht mehr fuer 48 h.
    2. **Modellrand.** `icon_d2_eps` liefert bei `forecast_days=3` gemessen
       66 von 72 Stunden nicht-null (05.08.2026); der Rest ist None. Ein
       `sum(v or 0.0)` ueber diesen Rand zaehlt fehlende Stunden als 0 mm --
       also als "kein Regen".

    Beides erzeugt denselben Fehler: eine Summe, die WENIGER Stunden abdeckt
    als ihr Etikett behauptet, und die dadurch systematisch zu trocken ist --
    zu trocken heisst hier: sperrt seltener, giesst oefter. Billige Richtung,
    aber die Zahl waere falsch beschriftet und liefe als "48-h-Prognose" in
    die Auswertung. Lieber keine Zeile als eine falsch etikettierte
    ([[fehlerpattern_benachbartes_feld_als_messwert]]).

    Nullen stehen am Modellrand, nicht in der Mitte -- deshalb wird beim
    ersten None abgeschnitten und nicht herausgefiltert.
    """
    indizes = zukunfts_indizes(hourly, stunden, jetzt)
    basis = hourly.get(variable)
    if basis:
        gekuerzt: list[int] = []
        for i in indizes:
            if i >= len(basis) or basis[i] is None:
                break
            gekuerzt.append(i)
        indizes = gekuerzt
    return indizes if len(indizes) >= stunden else None


def perzentil_der_membersummen(
    hourly: dict, stunden: int = 24, variable: str = "precipitation",
    jetzt: datetime | None = None,
) -> RegenEnsemble | None:
    """Bildet PRO MEMBER die Summe ueber `stunden`, dann die Perzentile.

    `hourly` ist der Open-Meteo-Block; die Member stehen als Spalten
    `precipitation`, `precipitation_member01`, ... nebeneinander.

    Summiert werden die naechsten `stunden` Stunden AB `jetzt`, nicht die
    ersten `stunden` der Liste -- Begruendung in `zukunfts_indizes`.

    Reihenfolge ist wesentlich: erst summieren, dann Perzentil. Andersherum
    (Perzentil pro Stunde, dann summieren) entstuende ein Verlauf, den kein
    einziger Member vorhergesagt hat -- typischerweise deutlich zu nass, weil
    man in jeder Stunde den jeweils trockensten Member nimmt, aber ueber die
    Member hinweg springt.
    """
    spalten = [
        k for k in hourly
        if k == variable or k.startswith(f"{variable}_member")
    ]
    if not spalten:
        return None

    indizes = brauchbare_indizes(hourly, stunden, variable, jetzt)
    if indizes is None:
        return None

    summen: list[float] = []
    for k in spalten:
        spalte = hourly[k]
        werte = [spalte[i] for i in indizes if i < len(spalte)]
        # Ein Member, der NUR None liefert, ist nicht "0 mm" sondern
        # "keine Aussage" -- er darf das Perzentil nicht nach unten ziehen.
        if not werte or all(v is None for v in werte):
            continue
        summen.append(sum(v or 0.0 for v in werte))

    if not summen:
        return None

    summen.sort()
    n = len(summen)
    return RegenEnsemble(
        p10=_perzentil(summen, 0.10),
        p20=_perzentil(summen, 0.20),
        p50=_perzentil(summen, 0.50),
        p90=_perzentil(summen, 0.90),
        minimum=summen[0],
        maximum=summen[-1],
        n_member=n,
        wahrsch_ueber_1mm=sum(1 for s in summen if s > 1.0) / n,
    )


class EnsembleClient:
    """Holt das Regen-Ensemble. Read-only, keine Auth, kein API-Key."""

    def __init__(self, http_get) -> None:
        # `http_get(url, params) -> dict` wird injiziert (testbar, und der
        # bestehende Wetter-Client bringt Timeout/Retry schon mit).
        self._get = http_get

    async def hole(
        self, latitude: float, longitude: float, stunden: int = 24,
        jetzt: datetime | None = None,
    ) -> RegenEnsemble | None:
        try:
            daten = await self._get(ENSEMBLE_URL, {
                "latitude": latitude,
                "longitude": longitude,
                "hourly": "precipitation",
                "models": MODELL,
                # T-0425: 3 statt 2. Der Block beginnt bei heute 00:00, und
                # gezaehlt wird jetzt ab `jetzt` -- mit 2 Tagen blieben am
                # Abend keine 24 Vorwaerts-Stunden mehr uebrig, und fuer den
                # 48-h-Horizont zu keiner Tageszeit.
                "forecast_days": 3,
                "timezone": "Europe/Berlin",
            })
        except Exception as exc:  # noqa: BLE001
            # Ensemble ist ein ZUSATZ-Signal. Faellt es aus, darf die
            # Bewaesserung nicht stehenbleiben -- der Caller behandelt None
            # als "keine Ensemble-Information" und faellt auf den
            # deterministischen Wert zurueck.
            logger.warning(
                "wetter.ensemble_fehlgeschlagen", fehler=str(exc),
                latitude=latitude, longitude=longitude,
            )
            return None

        ens = perzentil_der_membersummen(
            daten.get("hourly") or {}, stunden=stunden, jetzt=jetzt,
        )
        if ens is None:
            logger.warning(
                "wetter.ensemble_ohne_member",
                latitude=latitude, longitude=longitude,
            )
        return ens
