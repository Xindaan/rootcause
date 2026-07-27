"""T-0353: pro-Zone-Routing der Feuchte-Prognose (Shadow).

T-0348-Verdikt: Physik-vs-ML spaltet sich nach Datendichte pro Zone
(waldblumenhain physik-dominant, Mikrodrip-Zonen ML-dominant) — der
richtige Hebel ist pro-Zone-Routing, kein Monolith-Umbau.

HEUTE nur Beobachtung: `route_prognose` waehlt pro Zone die Quelle und
der `EmpfehlungsAuditJob` loggt sie als `routing_quelle` in
`empfehlungs_audit`. `entscheidung.py` ruft den Router NICHT auf.
Die spaetere Umschaltung des Entscheidungs-Pfads (Stufe 5) ist ein
separater, user-gated Schritt nach mehrwoechigem Shadow-Beweis.
"""

from __future__ import annotations

from bewaesserung.modelle import GiessEmpfehlung, MlForecastRoutingKonfig

QUELLE_ML = "ml"
QUELLE_PHYSIK = "physik"
QUELLE_STATESPACE = "statespace"

# Fallback-Ketten: gewuenschte Quelle zuerst, dann naechst-robustere.
# "ml" faellt auf die Heuristik-Prognose zurueck (prognose_*h ist dann
# bereits die Heuristik, prognose_quelle="heuristik").
_FALLBACK = {
    QUELLE_STATESPACE: [QUELLE_STATESPACE, QUELLE_PHYSIK, QUELLE_ML],
    QUELLE_PHYSIK: [QUELLE_PHYSIK, QUELLE_ML],
    QUELLE_ML: [QUELLE_ML],
}


def _verfuegbar(empfehlung: GiessEmpfehlung, quelle: str) -> bool:
    if quelle == QUELLE_STATESPACE:
        return empfehlung.prognose_statespace_6h is not None
    if quelle == QUELLE_PHYSIK:
        return empfehlung.prognose_physik_6h is not None
    # ML/Heuristik-Prognose ist immer da, sobald prognose_6h gesetzt ist.
    return empfehlung.prognose_6h is not None


def route_prognose(
    zone_id: str,
    routing: MlForecastRoutingKonfig,
    empfehlung: GiessEmpfehlung,
) -> str | None:
    """Liefert die geroutete Prognose-Quelle fuer die Zone (oder None
    wenn Routing inaktiv / keine Quelle verfuegbar).

    Rueckgabe-Format: Quelle, bei Fallback mit Herkunft, z. B.
    "physik(statt statespace)" — damit der Audit-Trail zeigt, wie oft
    die Wunsch-Quelle ausfiel.
    """
    if not routing.aktiv:
        return None
    wunsch = routing.zonen.get(zone_id, routing.default_quelle)
    kette = _FALLBACK.get(wunsch, [QUELLE_ML])
    for quelle in kette:
        if _verfuegbar(empfehlung, quelle):
            if quelle == wunsch:
                return quelle
            return f"{quelle}(statt {wunsch})"
    return None
