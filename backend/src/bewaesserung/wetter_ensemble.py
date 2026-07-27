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
    if not sortiert:
        return 0.0
    return sortiert[min(len(sortiert) - 1, int(p * len(sortiert)))]


def perzentil_der_membersummen(
    hourly: dict, stunden: int = 24, variable: str = "precipitation",
) -> RegenEnsemble | None:
    """Bildet PRO MEMBER die Summe ueber `stunden`, dann die Perzentile.

    `hourly` ist der Open-Meteo-Block; die Member stehen als Spalten
    `precipitation`, `precipitation_member01`, ... nebeneinander.

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

    summen: list[float] = []
    for k in spalten:
        werte = hourly[k][:stunden]
        # Ein Member, der NUR None liefert, ist nicht "0 mm" sondern
        # "keine Aussage" -- er darf das Perzentil nicht nach unten ziehen.
        if all(v is None for v in werte):
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
    ) -> RegenEnsemble | None:
        try:
            daten = await self._get(ENSEMBLE_URL, {
                "latitude": latitude,
                "longitude": longitude,
                "hourly": "precipitation",
                "models": MODELL,
                "forecast_days": 2,
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
            daten.get("hourly") or {}, stunden=stunden,
        )
        if ens is None:
            logger.warning(
                "wetter.ensemble_ohne_member",
                latitude=latitude, longitude=longitude,
            )
        return ens
