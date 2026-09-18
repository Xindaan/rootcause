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

from datetime import datetime

import structlog

from bewaesserung.ml.physik_trocknung import prognose_physik
from bewaesserung.modelle import GesamtKonfig, GiessEmpfehlung, ZonenKonfig
from bewaesserung.speicher import Speicher
from bewaesserung.warn_drossel import (
    DROSSEL_INTERVALL_DEFAULT,
    WarnDrossel,
)

logger = structlog.get_logger()


# T-0359 (13.08.2026): ab diesem Alter gilt ein gefitteter `k_basis` als
# veraltet. Der Fit-Job laeuft alle 24 h; ueberspringt er eine Zone zwei Wochen
# am Stueck, hat sie in der Zeit keine einzige auswertbare Trocknungsphase
# geliefert -- der gespeicherte Wert beschreibt dann einen Zustand, den es so
# nicht mehr geben muss.
#
# **Warum die Zahl trotzdem WEITER BENUTZT wird und nur die Quelle wechselt:**
# ein alter Fit ist immer noch besser als der `default_tau`-Pauschalwert, und
# die Read-Only-Reihe (Stufe 1) darf nicht mitten in der Beobachtung ihre
# Groessenordnung wechseln -- das waere ein Bruch in genau der Reihe, auf der
# die Stufe-2-Entscheidung fusst. Sichtbar gemacht wird es ueber
# `physik_quelle`, entschieden wird beim naechsten T-0359-Trigger.
#
# Realfall, der das ausgeloest hat: hecke und waldblumenhain standen am
# 13.08. noch auf ihrem Fit vom 02.07. (hecke MAE 27,9), weil der Job sie
# seither jedes Mal mit `skip_kein_welkepunkt` uebersprungen hat -- ohne dass
# das irgendwo ankam ([[fehlerpattern_datei_statt_prozesszustand]]).
#
# **Verhaeltnis zur bestehenden Schranke im Projekt.** `wirkung_fit` kennt
# dieselbe Frage seit T-0292 und loest sie strenger: `max_fit_alter_tage: 30`,
# und dort wird der Fit bei Ueberschreitung VERWORFEN (Konfig-Fallback,
# `entscheidung._aufgeloeste_wirkung`). Der Unterschied ist beabsichtigt und
# hat zwei Gruende:
#   1. **Pfad.** `wirkung_fit` entscheidet ueber echte Giessdauern, hier geht
#      es um eine Read-Only-Diagnose. Auf dem scharfen Pfad ist Verwerfen die
#      richtige Fehlerrichtung, auf dem diagnostischen waere es ein Bruch in
#      der Beobachtungsreihe.
#   2. **Taktung.** Der k_basis-Job laeuft alle 24 h, der Wirkungs-Fit nur mit
#      neuen Giesslaeufen. 14 Tage sind hier ~14 ausgelassene Laeufe, dort
#      waeren sie nichts Auffaelliges.
K_BASIS_MAX_ALTER_TAGE = 14.0

QUELLE_GEFITTET = "gefittet"
QUELLE_GEFITTET_VERALTET = "gefittet_veraltet"

# T-0359 (22.08.2026): so selten wird derselbe veraltete Fit gemeldet.
#
# **Warum ueberhaupt gedrosselt.** Der Marker oben hat getan, was er sollte:
# waldblumenhain heilte sich selbst (frischer Fit am 21.08., MAE 0,96 statt
# 8,67) und meldet nicht mehr. `hecke` dagegen steht seit dem 02.07. auf
# demselben Fit, weil der Job sie jedes Mal mit `skip_kein_welkepunkt`
# ueberspringt -- und erzeugte damit **608 Warnungen in drei Tagen**, rund
# acht pro Stunde, eine je Aufruf.
#
# Eine Meldung, die achtmal pro Stunde kommt, wird nicht gelesen. Damit
# verdeckt der Marker genau die Faelle, fuer die er gebaut wurde: eine Zone,
# die NEU veraltet, geht im Dauerrauschen der einen bekannten unter. Gleiche
# Klasse wie T-0540 (Leck-Detektor, 88 Meldungen fuer einen dokumentierten
# Zustand) und die Lehre aus T-0426.
#
# **Gedrosselt wird NUR das Log, nicht die Aussage.** `physik_quelle` bleibt
# bei JEDEM Aufruf auf `gefittet_veraltet`; wer den Zustand programmatisch
# braucht, sieht ihn unveraendert. Verschwiegen wird lediglich die
# Wiederholung derselben Nachricht.
#
# 24 h ist die Taktung des Fit-Jobs: frueher zu melden koennte gar nichts
# Neues bringen, weil sich zwischen zwei Laeufen nichts aendern kann.
K_BASIS_WARN_INTERVALL = DROSSEL_INTERVALL_DEFAULT

# T-0540: dieselbe Drossel wie in `entscheidung.py`, gemeinsame Implementierung
# in `warn_drossel.py`. Vorher stand hier eine eigene Kopie aus T-0359 -- bei
# drei weiteren Fundstellen derselben Klasse waeren daraus vier Kopien
# geworden. Modul-Zustand, weil `loese_k_basis` eine freie Funktion ohne
# Lebensdauer ist; er ueberlebt bewusst nur den Prozess (nach einem Neustart
# darf einmal gemeldet werden, siehe Modul-Docstring von warn_drossel).
_k_basis_drossel = WarnDrossel(K_BASIS_WARN_INTERVALL)


def _warnung_faellig(zone_id: str, jetzt: datetime) -> bool:
    """Ist die Veraltet-Warnung fuer diese Zone jetzt wieder dran?

    Setzt bei True zugleich den Zeitstempel -- der Aufrufer soll sich nicht
    merken muessen, dass er das noch tun muss.
    """
    return _k_basis_drossel.faellig(zone_id, jetzt)


def _warnungs_drossel_zuruecksetzen() -> None:
    """Nur fuer Tests: Modul-Zustand leeren."""
    _k_basis_drossel.zuruecksetzen()


def _alter_in_tagen(
    gefittet_am: object, jetzt: datetime | None = None,
) -> float | None:
    """Alter eines Fit-Zeitstempels in Tagen; `None` wenn unlesbar.

    `None` heisst bewusst "kein Urteil" und nicht "alt": ein Zeitstempel, den
    wir nicht parsen koennen, ist kein Beleg fuer Veralterung -- daraus eine
    Warnung zu bauen waere ein Fehlalarm aus Unwissen. `jetzt` ist
    durchgereicht, damit Tests nicht von der Uhr abhaengen
    ([[fehlerpattern_jetzt_nicht_durchgereicht]]).
    """
    if not gefittet_am:
        return None
    try:
        gefittet = datetime.fromisoformat(str(gefittet_am))
    except ValueError:
        return None
    return ((jetzt or datetime.now()) - gefittet).total_seconds() / 86400.0


async def loese_k_basis(
    *,
    zone: ZonenKonfig,
    speicher: Speicher | None,
    konfig: GesamtKonfig,
    jetzt: datetime | None = None,
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
            quelle = QUELLE_GEFITTET
            alter = _alter_in_tagen(row.get("gefittet_am"), jetzt)
            if alter is not None and alter > K_BASIS_MAX_ALTER_TAGE:
                # Die QUELLE wird immer gesetzt -- gedrosselt ist nur das Log.
                quelle = QUELLE_GEFITTET_VERALTET
                if _warnung_faellig(zone.zone_id, jetzt or datetime.now()):
                    logger.warning(
                        "physik.k_basis_veraltet",
                        zone_id=zone.zone_id,
                        alter_tage=round(alter, 1),
                        schwelle_tage=K_BASIS_MAX_ALTER_TAGE,
                        gefittet_am=row.get("gefittet_am"),
                        mae=row.get("mae"),
                        drossel_stunden=K_BASIS_WARN_INTERVALL.total_seconds() / 3600,
                        hinweis="Der Fit-Job hat die Zone seither jedes Mal "
                                "uebersprungen -- Grund steht als "
                                "k_basis_fit.skip_* im Log. Wert wird weiter "
                                "benutzt, aber als veraltet markiert (T-0359). "
                                "Diese Meldung ist gedrosselt.",
                    )
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
    jetzt: datetime | None = None,
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
            # T-0508: `stunden[:24]` waere der heutige KALENDERTAG ab 00:00,
            # nicht die naechsten 24 h -- am Abend also fast reine
            # Vergangenheit in einer Trocknungs-Vorhersage.
            return [s.et0_mm for s in vorhersage.zukunftsstunden(24, jetzt)]
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
