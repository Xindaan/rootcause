"""T-0353: Service-Layer fuer den State-Space-Shadow-Forecaster.

Verdrahtet `state_space.prognose_statespace` mit den Live-Quellen
(Speicher, Konfig, Wetter-Manager) und fuellt `prognose_statespace_*h` +
`statespace_quelle` auf einer `GiessEmpfehlung`-Instanz auf — analog zu
`physik_diagnose.augmentiere_physik_prognose` (T-0270).

**Shadow-only**: aendert KEINE Entscheidungs-Felder. Einziger Aufrufer
ist der `EmpfehlungsAuditJob` (hinter `ml_state_space.aktiv`).

Bekannte Grenze (Verifier-Review 01.07., dokumentiert): Ein Lauf, den
der AutoIgnorierenJob SPAETER auf `ignoriert` flippt (zeitversetzter
Job), kann zum Snapshot-Zeitpunkt noch als echter Puls einfliessen.
Betroffen sind nur Zonen mit auto-ignore-Regime (magerwiese), deren
Snapshots ohnehin als `ausgeschlossen_mlausschluss` klassifiziert werden.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from bewaesserung.ml.physik_diagnose import loese_k_basis
from bewaesserung.ml.state_space import (
    GiessPuls,
    StateSpaceParams,
    prognose_statespace,
    puls_magnitude_pp,
)
from bewaesserung.modelle import (
    GesamtKonfig,
    GiessEmpfehlung,
    KEINE_WASSER_AUSLOESER,
    VentilAktion,
    VentilEreignis,
)
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()


def gruppiere_pulse(
    events: list[VentilEreignis],
) -> list[GiessPuls]:
    """Echte Wasser-Laeufe (SCHLIESSEN dauer>0, ausloser nicht in
    KEINE_WASSER_AUSLOESER) zu Pulsen gruppieren.

    Events mit gleicher `lauf_gruppe` (Pre-Soak: Anpuls + Hauptdose,
    T-0335) werden zu EINEM Puls zusammengefasst (Netto-Dauer = Summe,
    t_ende = letztes Close) — sonst ueberzeichnet das Plateau-Modell
    die Summe zweier Teil-Dosen leicht (Verifier-Review 01.07.).
    """
    einzel: list[GiessPuls] = []
    gruppen: dict[str, list[VentilEreignis]] = {}
    for e in events:
        if e.aktion != VentilAktion.SCHLIESSEN or (e.dauer_sekunden or 0) <= 0:
            continue
        if e.ausloser in KEINE_WASSER_AUSLOESER:
            continue
        if e.lauf_gruppe:
            gruppen.setdefault(e.lauf_gruppe, []).append(e)
        else:
            einzel.append(GiessPuls(
                t_ende=e.zeitstempel, dauer_s=float(e.dauer_sekunden),
            ))
    for mitglieder in gruppen.values():
        einzel.append(GiessPuls(
            t_ende=max(m.zeitstempel for m in mitglieder),
            dauer_s=float(sum(m.dauer_sekunden for m in mitglieder)),
        ))
    return sorted(einzel, key=lambda p: p.t_ende)


async def hole_wetter_stunden_zukunft(
    *,
    zone_id: str,
    konfig: GesamtKonfig,
    wetter_manager,
    stunden: int = 24,
) -> tuple[list[float], list[float]]:
    """(et0_pro_h, regen_pro_h) der Wetter-Vorhersage fuer die Zone.

    Leere Listen bei fehlendem Manager/Fehler — `prognose_statespace`
    fuellt dann mit et0_basis bzw. 0.0 auf (Vertrag wie
    `physik_diagnose.hole_et0_zukunft`).
    """
    if wetter_manager is None:
        return [], []
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
            st_liste = vorhersage.stunden[:stunden]
            return (
                [s.et0_mm for s in st_liste],
                [s.niederschlag_mm for s in st_liste],
            )
    except Exception:
        logger.exception("statespace.wetter_vorhersage_fehler", zone_id=zone_id)
    return [], []


async def augmentiere_statespace_prognose(
    *,
    zone_id: str,
    empfehlung: GiessEmpfehlung,
    speicher: Speicher | None,
    konfig: GesamtKonfig | None,
    wetter_manager,
    jetzt: datetime | None = None,
) -> None:
    """Fuellt `prognose_statespace_*h` + `statespace_quelle` auf.

    Quelle-Format: "<k_quelle>+wirkung" wenn Puls-Parameter der Zone
    konfiguriert sind, "<k_quelle>+ohne_wirkung" wenn der Forecaster
    mangels Wirkungs-Parametern zu Physik+Regen degeneriert (bewusst
    KEIN stiller Default — fehlerpattern_default_wirkungsrate_zu_kurz).
    """
    if konfig is None or speicher is None:
        return
    ss_konfig = konfig.ml_state_space
    if not ss_konfig.aktiv:
        return
    if ss_konfig.zonen and zone_id not in ss_konfig.zonen:
        return
    zone = next((z for z in konfig.zonen if z.zone_id == zone_id), None)
    if zone is None:
        return
    f_start = empfehlung.feuchte_aktuell
    wp = empfehlung.welkepunkt_wert
    if f_start is None or wp is None:
        empfehlung.statespace_quelle = "keine"
        return
    aufloesung = await loese_k_basis(
        zone=zone, speicher=speicher, konfig=konfig,
    )
    if aufloesung is None:
        empfehlung.statespace_quelle = "keine"
        return
    k_basis, et0_basis, k_quelle = aufloesung

    t_start = jetzt or empfehlung.zeitstempel
    et0_pro_h, regen_pro_h = await hole_wetter_stunden_zukunft(
        zone_id=zone_id, konfig=konfig, wetter_manager=wetter_manager,
    )
    # Pulse: juengste echte Laeufe, deren Sensor-Ramp noch in die
    # Trajektorie hineinwirkt. Lookback grosszuegig (+6h fuer die
    # Netto-Dauer langer Laeufe vor dem Close).
    puls_von = t_start - timedelta(
        hours=ss_konfig.puls_lookback_stunden + 6,
    )
    try:
        events = await speicher.hole_ventil_ereignisse(
            zone_id, von=puls_von, bis=t_start,
        )
    except Exception:
        logger.exception("statespace.puls_lookup_fehler", zone_id=zone_id)
        events = []
    pulse = [
        p for p in gruppiere_pulse(events)
        if p.t_ende >= t_start - timedelta(hours=ss_konfig.puls_lookback_stunden)
    ]

    params = StateSpaceParams(
        welkepunkt=float(wp),
        k_basis_pro_h=k_basis,
        et0_basis_mm_pro_h=et0_basis,
        wirkung_max_pp=zone.wirkung_max_pp,
        wirkungsrate_initial=zone.wirkungsrate_initial,
        delta_pp_pro_minute=zone.delta_pp_pro_minute,
        regen_faktor_pp_pro_mm=ss_konfig.regen_faktor_pp_pro_mm,
        ramp_stunden=ss_konfig.ramp_stunden,
        obergrenze=(
            float(empfehlung.feldkapazitaet_wert)
            if empfehlung.feldkapazitaet_wert else 100.0
        ),
    )
    hat_wirkung = puls_magnitude_pp(params, 3600.0) > 0
    for horizont, attr in [
        (6, "prognose_statespace_6h"),
        (12, "prognose_statespace_12h"),
        (24, "prognose_statespace_24h"),
    ]:
        wert = prognose_statespace(
            f_start=f_start, t_start=t_start, horizont_h=horizont,
            params=params, et0_zukunft_pro_h=et0_pro_h,
            regen_zukunft_pro_h=regen_pro_h, pulse=pulse,
        )
        if wert is not None:
            setattr(empfehlung, attr, round(wert, 1))
    empfehlung.statespace_quelle = (
        f"{k_quelle}+wirkung" if hat_wirkung else f"{k_quelle}+ohne_wirkung"
    )
