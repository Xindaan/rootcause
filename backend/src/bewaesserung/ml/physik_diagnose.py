"""Hybrid Stufe 1: Service-Layer fuer die read-only Physik-Diagnose.

Verdrahtet `physik_trocknung.prognose_physik` mit den Live-Quellen
(Speicher, Konfig, Wetter-Manager) und fuellt die Felder
`prognose_physik_*h`, `k_basis_pro_h`, `physik_quelle` auf einer
`GiessEmpfehlung`-Instanz auf.

Wird sowohl im API-Endpoint `/api/zonen/{id}/empfehlung-jetzt`
aufgerufen als auch im `EmpfehlungsAuditJob` -- damit der Bias-Audit
(T-0270) die Physik-Werte mitloggen kann.
"""

from __future__ import annotations

import structlog

from bewaesserung.ml.physik_trocknung import prognose_physik
from bewaesserung.modelle import GesamtKonfig, GiessEmpfehlung, ZonenKonfig
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()


async def loese_k_basis(
    *,
    zone: ZonenKonfig,
    speicher: Speicher | None,
    konfig: GesamtKonfig,
) -> tuple[float, float, str] | None:
    """T-0279 Phase 2: gemeinsame k_basis-Aufloesungs-Kaskade.

    Extrahiert aus `augmentiere_physik_prognose`, damit die Engine
    (proaktiver Trigger) und der API-Augmentations-Helper EINE Wahrheit
    fuer k_basis nutzen.

    Reihenfolge:
      1. `ZonenKonfig.k_basis_pro_h` (manueller Override) -> "konfig"
      2. `physik_k_basis`-Tabelle (gefittet)               -> "gefittet"
      3. `default_tau_stunden`-Fallback                    -> "default_tau"

    Return `(k_basis, et0_basis, quelle)` oder None wenn Physik-Diagnose
    inaktiv / k_basis nicht aufloesbar.
    """
    physik = konfig.ml_physik_diagnose
    if not physik.aktiv:
        return None
    k_basis: float | None = None
    et0_basis = physik.et0_basis_mm_pro_h
    quelle = "keine"
    if zone.k_basis_pro_h is not None:
        k_basis = float(zone.k_basis_pro_h)
        quelle = "konfig"
    elif speicher is not None:
        try:
            row = await speicher.hole_k_basis(zone.zone_id)
        except Exception:
            row = None
        if row is not None and row.get("k_basis"):
            k_basis = float(row["k_basis"])
            if row.get("et0_basis_mm_pro_h"):
                et0_basis = float(row["et0_basis_mm_pro_h"])
            quelle = "gefittet"
    if k_basis is None:
        tau = physik.default_tau_stunden
        if tau > 0:
            k_basis = 1.0 / tau
            quelle = "default_tau"
    if k_basis is None or k_basis <= 0:
        return None
    return k_basis, et0_basis, quelle


async def hole_et0_zukunft(
    *,
    zone_id: str,
    konfig: GesamtKonfig,
    wetter_manager,
) -> list[float]:
    """T-0279 Phase 2: stuendliche ET0-Vorhersage (24 h) fuer die Zone.

    Leere Liste wenn kein Wetter-Manager / Fehler -- `prognose_physik`
    fuellt dann mit `et0_basis` auf.
    """
    if wetter_manager is None:
        return []
    standort_id: str | None = None
    for st in (konfig.standorte or []):
        if zone_id in (st.zonen or []):
            standort_id = st.wetter_standort
            break
    try:
        if standort_id:
            client = wetter_manager.hole_client(standort_id)
        else:
            client = wetter_manager.standard_client
        if client is not None:
            vorhersage = await client.hole_vorhersage()
            return [s.et0_mm for s in vorhersage.stunden[:24]]
    except Exception:
        logger.exception("physik.et0_vorhersage_fehler", zone_id=zone_id)
    return []


async def augmentiere_physik_prognose(
    *,
    zone_id: str,
    empfehlung: GiessEmpfehlung,
    speicher: Speicher | None,
    konfig: GesamtKonfig | None,
    wetter_manager,
) -> None:
    """Fuellt die Felder `prognose_physik_*h`, `k_basis_pro_h` und
    `physik_quelle` auf einer `GiessEmpfehlung`-Instanz auf.

    **Read-only**: aendert KEINE Entscheidungs-Felder
    (`soll_bewaessern`, `empfehlungs_typ`, `dauer_s_empfehlung`, etc.).

    Reihenfolge der `k_basis`-Aufloesung:
      1. `ZonenKonfig.k_basis_pro_h` (manueller Override) -> "konfig"
      2. `physik_k_basis`-Tabelle (gefittet)               -> "gefittet"
      3. `default_tau_stunden`-Fallback                    -> "default_tau"
      4. Welkepunkt fehlt / Aggregat fehlt                 -> "keine"

    Robust gegen None-Dependencies: wenn `konfig` oder `speicher` None
    sind, gibt die Funktion still auf (read-only soll nichts kaputt
    machen).
    """
    if konfig is None:
        return
    physik = konfig.ml_physik_diagnose
    if not physik.aktiv:
        return
    zone = next((z for z in konfig.zonen if z.zone_id == zone_id), None)
    if zone is None:
        return
    f_start = empfehlung.feuchte_aktuell
    wp = empfehlung.welkepunkt_wert
    if f_start is None or wp is None:
        empfehlung.physik_quelle = "keine"
        return
    # T-0279 Phase 2: k_basis + ET0 ueber die gemeinsamen Helper, damit
    # API-Augmentation + Engine-Trigger dieselbe Wahrheit nutzen.
    aufloesung = await loese_k_basis(
        zone=zone, speicher=speicher, konfig=konfig,
    )
    if aufloesung is None:
        empfehlung.physik_quelle = "keine"
        return
    k_basis, et0_basis, quelle = aufloesung
    et0_pro_h = await hole_et0_zukunft(
        zone_id=zone_id, konfig=konfig, wetter_manager=wetter_manager,
    )
    empfehlung.k_basis_pro_h = k_basis
    empfehlung.physik_quelle = quelle
    for horizont, attr in [
        (6, "prognose_physik_6h"),
        (12, "prognose_physik_12h"),
        (24, "prognose_physik_24h"),
    ]:
        wert = prognose_physik(
            f_start=f_start, welkepunkt=wp,
            k_basis_pro_h=k_basis,
            et0_basis_mm_pro_h=et0_basis,
            et0_zukunft_pro_h=et0_pro_h,
            horizont_h=horizont,
        )
        if wert is not None:
            setattr(empfehlung, attr, round(wert, 1))
