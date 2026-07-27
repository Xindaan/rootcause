"""T-0122: Periodischer Audit-Job fuer die kausale Gieß-Empfehlung.

Vier Phasen pro Tick:

1. **Snapshot**: Pro Zone wird `Entscheidungsmotor.vorhersage_zone()`
   aufgerufen und die zentralen Felder in `empfehlungs_audit` persistiert
   (alle 60 Min). Read-only fuer das Backend selbst — keine Auto-Loop-
   Wirkung, kein Ventil-Befehl. Datengrundlage fuer die Vertrauens-
   Aufbau-Phase vor T-0021 Scharfschalten. T-0353: zusaetzlich
   State-Space-Shadow (`prognose_statespace_*h`), Heuristik-Shadow
   (`prognose_heuristik_24h`, T-0351) und Routing-Wahl
   (`routing_quelle`) — alles Diagnose, kein Entscheidungs-Pfad.

2. **Eval**: Audit-Eintraege, die mindestens 6 h alt sind und noch nicht
   evaluiert wurden, werden mit dem realen Sensor-Wert nach 6 h und
   24 h verglichen. Felder `ist_feuchte_*` + `abweichung_*` werden
   geschrieben. Damit lassen sich im Frontend Auswertungen rechnen wie
   "Wie oft hat 'akut' wirklich zu kritischer Feuchte gefuehrt?" oder
   "MAE der Reserve-Tage-Aussage".

3. **24h-Nachzug (T-0349)**: Zeilen, die beim 6h-Eval abgeschlossen
   wurden (`evaluiert_am` gesetzt, T-0264-Semantik), bekommen ihren
   24h-Ist-Wert per eigenem UPDATE nachgetragen, der die 6h-Felder
   nicht anfasst. Ohne Nachzug bleibt `ist_feuchte_24h` fuer immer
   NULL (Befund 01.07.: waldblumenhain 0 Zeilen mit 24h-Ist).

4. **Regime-Stempel (T-0349)**: klassifiziert Snapshot-Fenster
   rueckwirkend in `regime_6h`/`regime_24h`, sobald das wetter_archiv
   (ERA5, ~5 Tage Lag) das jeweilige Fenster abdeckt. Bewusst vom Eval
   entkoppelt — zur Eval-Zeit (+6h/+24h) ist die Regen-Achse nie
   beurteilbar, jede Zeile wuerde als `regen_unbekannt` versteinern.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from bewaesserung.entscheidung import Entscheidungsmotor
from bewaesserung.modelle import GesamtKonfig
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

INTERVALL_MINUTEN_DEFAULT = 60
EVAL_TOLERANZ_MIN = 30   # Sensor-Wert +/- 30 min vom Ziel-Zeitpunkt zaehlt


class EmpfehlungsAuditJob:
    """Snapshot + Eval der kausalen Empfehlung pro Zone."""

    def __init__(
        self,
        speicher: Speicher,
        motor: Entscheidungsmotor,
        zone_ids: list[str],
        intervall_minuten: int = INTERVALL_MINUTEN_DEFAULT,
        # T-0270 (28.05.): Hybrid Stufe 1 Physik-Diagnose mitloggen.
        # Beide neu, beide Default None -- alte Aufrufer (Tests)
        # bleiben backward-kompatibel; ohne `konfig` faellt der
        # Augmentations-Helper still durch (`physik_quelle = None`).
        konfig: GesamtKonfig | None = None,
        wetter_manager=None,
    ) -> None:
        self._speicher = speicher
        self._motor = motor
        self._zone_ids = list(zone_ids)
        self._intervall = timedelta(minutes=intervall_minuten)
        self._letzte_aktualisierung: datetime | None = None
        self._konfig = konfig
        self._wetter_manager = wetter_manager

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> dict[str, int]:
        """Laeuft max. einmal pro `intervall_minuten`. Gibt Statistik
        `{snapshots, evals}` zurueck."""
        jetzt = jetzt or datetime.now()
        if (
            self._letzte_aktualisierung
            and (jetzt - self._letzte_aktualisierung) < self._intervall
        ):
            return {"snapshots": 0, "evals": 0}
        self._letzte_aktualisierung = jetzt

        snapshots = await self._snapshot_alle(jetzt)
        evals = await self._evaluiere_offene(jetzt)
        # T-0349: eigene Fehler-Baender — ein Fehler im Nachzug/Stempeln
        # darf Snapshot+Eval nicht mitreissen (und umgekehrt).
        try:
            nachzuege = await self._nachzug_24h(jetzt)
        except Exception:
            logger.exception("empfehlungs_audit.nachzug_fehler")
            nachzuege = 0
        try:
            regimes = await self._stemple_regimes(jetzt)
        except Exception:
            logger.exception("empfehlungs_audit.regime_stempel_fehler")
            regimes = 0
        logger.info(
            "empfehlungs_audit.tick",
            snapshots=snapshots, evals=evals,
            nachzuege_24h=nachzuege, regime_stempel=regimes,
        )
        return {
            "snapshots": snapshots, "evals": evals,
            "nachzuege_24h": nachzuege, "regime_stempel": regimes,
        }

    async def _snapshot_alle(self, jetzt: datetime) -> int:
        """Pro Zone: vorhersage_zone() rufen + Snapshot persistieren.

        T-0270 (28.05.): Ruft zusaetzlich `augmentiere_physik_prognose`,
        damit die Physik-Werte (`prognose_physik_*h`, `physik_quelle`,
        `k_basis_pro_h`) mit in `empfehlungs_audit` landen. Voraussetzung:
        `konfig` und `wetter_manager` sind beim Job-Bau gesetzt (Tests
        ohne Konfig laufen weiter, dort bleiben die Physik-Felder None).
        """
        anzahl = 0
        for zone_id in self._zone_ids:
            try:
                empf = await self._motor.vorhersage_zone(zone_id)
            except Exception:
                logger.exception(
                    "empfehlungs_audit.snapshot_fehler", zone_id=zone_id,
                )
                continue
            # T-0270: Physik-Diagnose nachziehen (Read-only).
            if self._konfig is not None:
                try:
                    from bewaesserung.ml.physik_diagnose import (
                        augmentiere_physik_prognose,
                    )
                    await augmentiere_physik_prognose(
                        zone_id=zone_id, empfehlung=empf,
                        speicher=self._speicher, konfig=self._konfig,
                        wetter_manager=self._wetter_manager,
                    )
                except Exception:
                    logger.exception(
                        "empfehlungs_audit.physik_augmentation_fehler",
                        zone_id=zone_id,
                    )
                # T-0353: State-Space-Shadow (hinter ml_state_space.aktiv).
                try:
                    from bewaesserung.ml.state_space_diagnose import (
                        augmentiere_statespace_prognose,
                    )
                    await augmentiere_statespace_prognose(
                        zone_id=zone_id, empfehlung=empf,
                        speicher=self._speicher, konfig=self._konfig,
                        wetter_manager=self._wetter_manager, jetzt=jetzt,
                    )
                except Exception:
                    logger.exception(
                        "empfehlungs_audit.statespace_augmentation_fehler",
                        zone_id=zone_id,
                    )
            # T-0351: Heuristik-Decay-Shadow — die rohe ET0*2-Rate ist
            # damit IMMER messbar, nicht nur wenn ML ausfaellt.
            prognose_heuristik_24h = None
            if (
                empf.feuchte_aktuell is not None
                and empf.decay_heuristik_pp_pro_tag is not None
            ):
                prognose_heuristik_24h = round(
                    max(0.0, min(
                        100.0,
                        empf.feuchte_aktuell - empf.decay_heuristik_pp_pro_tag,
                    )), 1,
                )
            # T-0353: Routing-Wahl loggen (Shadow — kein Entscheidungs-Pfad).
            routing_quelle = None
            if self._konfig is not None:
                try:
                    from bewaesserung.ml.forecast_router import route_prognose
                    routing_quelle = route_prognose(
                        zone_id, self._konfig.ml_forecast_routing, empf,
                    )
                except Exception:
                    logger.exception(
                        "empfehlungs_audit.routing_fehler", zone_id=zone_id,
                    )
            try:
                await self._speicher.setze_empfehlungs_audit(
                    zeitstempel=jetzt,
                    zone_id=zone_id,
                    empfehlungs_typ=empf.empfehlungs_typ or "unbekannt",
                    soll_bewaessern=empf.soll_bewaessern,
                    blocker_typ=(
                        empf.blocker_typ.value if empf.blocker_typ else None
                    ),
                    feuchte_aktuell=empf.feuchte_aktuell,
                    welkepunkt_wert=empf.welkepunkt_wert,
                    optimum_min=empf.optimum_min,
                    optimum_max=empf.optimum_max,
                    prognose_quelle=empf.prognose_quelle,
                    prognose_6h=empf.prognose_6h,
                    prognose_12h=empf.prognose_12h,
                    prognose_24h=empf.prognose_24h,
                    tage_bis_welkepunkt=empf.tage_bis_welkepunkt,
                    dauer_s_empfehlung=empf.dauer_s_empfehlung,
                    aktive_strategie=empf.aktive_strategie,
                    # T-0270: read-only Physik-Diagnose mitloggen.
                    prognose_physik_6h=empf.prognose_physik_6h,
                    prognose_physik_12h=empf.prognose_physik_12h,
                    prognose_physik_24h=empf.prognose_physik_24h,
                    physik_quelle=empf.physik_quelle if (
                        empf.physik_quelle and empf.physik_quelle != "keine"
                    ) else None,
                    k_basis_pro_h=empf.k_basis_pro_h,
                    # T-0353/T-0351: Shadow-Prognosen + Routing.
                    prognose_statespace_6h=empf.prognose_statespace_6h,
                    prognose_statespace_12h=empf.prognose_statespace_12h,
                    prognose_statespace_24h=empf.prognose_statespace_24h,
                    statespace_quelle=empf.statespace_quelle if (
                        empf.statespace_quelle
                        and empf.statespace_quelle != "keine"
                    ) else None,
                    prognose_heuristik_24h=prognose_heuristik_24h,
                    routing_quelle=routing_quelle,
                )
                anzahl += 1
            except Exception:
                logger.exception(
                    "empfehlungs_audit.persist_fehler", zone_id=zone_id,
                )
        return anzahl

    async def _evaluiere_offene(self, jetzt: datetime) -> int:
        """Holt offene Audit-Zeilen + traegt ist_feuchte_6h/24h ein.

        T-0264 (2026-05-26): `evaluiert_am` darf NICHT gesetzt werden,
        wenn beide ist-Werte None sind -- der Snapshot ist sonst als
        "abgehandelt" markiert und der naechste Tick laesst ihn liegen,
        obwohl die Sensor-Werte spaeter verfuegbar werden.
        Realfall yogaraum 11:49-Snapshot: Job tickte um 17:13:33
        (vor ziel=17:49), _sensor_nah_zeitpunkt returnte None weil
        ziel in der Zukunft lag, evaluiert_am wurde trotzdem auf
        17:13 gesetzt. Nachfolge-Tick 18:14 hat den Snapshot nicht
        mehr gefunden -> ist_feuchte_6h dauerhaft None obwohl ein
        Sensor-Wert 18:07:57 verfuegbar war.
        """
        offene = await self._speicher.hole_offene_empfehlungs_audits(jetzt)
        anzahl = 0
        for o in offene:
            ts = o["zeitstempel"]
            zone_id = o["zone_id"]
            ist_6h = await self._sensor_nah_zeitpunkt(
                zone_id, ts + timedelta(hours=6),
            )
            ist_24h = await self._sensor_nah_zeitpunkt(
                zone_id, ts + timedelta(hours=24),
            )
            # T-0264: solange beide ist-Werte None sind, Audit offen
            # lassen (Eval-Fenster noch nicht erreicht oder keine
            # Sensor-Messung im Toleranz-Bereich). Der naechste Job-
            # Tick versucht es erneut. Sobald >=1 ist-Wert messbar
            # ist, markieren wir als evaluiert (24h-Wert kann auch
            # spaeter noch None bleiben falls Sensor-Lueckenfall).
            if ist_6h is None and ist_24h is None:
                continue
            abw_6h = (
                o["prognose_6h"] - ist_6h
                if (o["prognose_6h"] is not None and ist_6h is not None)
                else None
            )
            abw_24h = (
                o["prognose_24h"] - ist_24h
                if (o["prognose_24h"] is not None and ist_24h is not None)
                else None
            )
            # T-0270: Physik-Abweichung parallel rechnen (Read-only-
            # Bias-Audit). Wenn Physik-Snapshot None (kein Welkepunkt
            # oder Konfig-Defekt zum Zeitpunkt), bleibt die Abweichung
            # None -- die Zeile ist trotzdem evaluiert.
            abw_phys_6h = (
                o.get("prognose_physik_6h") - ist_6h
                if (o.get("prognose_physik_6h") is not None
                    and ist_6h is not None)
                else None
            )
            abw_phys_24h = (
                o.get("prognose_physik_24h") - ist_24h
                if (o.get("prognose_physik_24h") is not None
                    and ist_24h is not None)
                else None
            )
            # T-0353/T-0351: Shadow-Abweichungen analog (None-tolerant).
            abw_ss_6h = (
                o.get("prognose_statespace_6h") - ist_6h
                if (o.get("prognose_statespace_6h") is not None
                    and ist_6h is not None)
                else None
            )
            abw_ss_24h = (
                o.get("prognose_statespace_24h") - ist_24h
                if (o.get("prognose_statespace_24h") is not None
                    and ist_24h is not None)
                else None
            )
            abw_heu_24h = (
                o.get("prognose_heuristik_24h") - ist_24h
                if (o.get("prognose_heuristik_24h") is not None
                    and ist_24h is not None)
                else None
            )
            try:
                await self._speicher.aktualisiere_empfehlungs_audit_eval(
                    audit_id=o["id"],
                    ist_feuchte_6h=ist_6h,
                    ist_feuchte_24h=ist_24h,
                    abweichung_6h=abw_6h,
                    abweichung_24h=abw_24h,
                    evaluiert_am=jetzt,
                    abweichung_physik_6h=abw_phys_6h,
                    abweichung_physik_24h=abw_phys_24h,
                    abweichung_statespace_6h=abw_ss_6h,
                    abweichung_statespace_24h=abw_ss_24h,
                    abweichung_heuristik_24h=abw_heu_24h,
                )
                anzahl += 1
            except Exception:
                logger.exception(
                    "empfehlungs_audit.eval_persist_fehler",
                    audit_id=o["id"],
                )
        return anzahl

    async def _nachzug_24h(self, jetzt: datetime) -> int:
        """T-0349 Phase 3: 24h-Ist-Werte fuer bereits (6h-)evaluierte
        Zeilen nachtragen. Eigener UPDATE-Pfad, 6h-Felder unangetastet."""
        offene = await self._speicher.hole_offene_24h_nachzuege(jetzt)
        anzahl = 0
        for o in offene:
            ist_24h = await self._sensor_nah_zeitpunkt(
                o["zone_id"], o["zeitstempel"] + timedelta(hours=24),
            )
            if ist_24h is None:
                # Keine Messung im Toleranz-Fenster — Zeile bleibt bis
                # max_alter_h im Kandidaten-Set, danach faellt sie raus.
                continue
            def _abw(schluessel: str) -> float | None:
                wert = o.get(schluessel)
                return wert - ist_24h if wert is not None else None
            try:
                await self._speicher.aktualisiere_empfehlungs_audit_eval_24h(
                    audit_id=o["id"],
                    ist_feuchte_24h=ist_24h,
                    abweichung_24h=_abw("prognose_24h"),
                    abweichung_physik_24h=_abw("prognose_physik_24h"),
                    abweichung_statespace_24h=_abw("prognose_statespace_24h"),
                    abweichung_heuristik_24h=_abw("prognose_heuristik_24h"),
                )
                anzahl += 1
            except Exception:
                logger.exception(
                    "empfehlungs_audit.nachzug_persist_fehler",
                    audit_id=o["id"],
                )
        return anzahl

    async def _stemple_regimes(self, jetzt: datetime) -> int:
        """T-0349 Phase 4: Regime-Spalten lag-gated nachstempeln.

        Pro Zone wird der Klassifikations-Kontext EINMAL geladen
        (Ventil-Events + Cross-Spray-Quellen + Wetter). Ein Horizont
        wird nur gestempelt, wenn die Klassifikation NICHT an fehlender
        Wetter-Abdeckung scheitert (`regen_unbekannt` bei wetter_max_ts
        < Fenster-Ende) — sonst naechster Tick. `regen_unbekannt` ist
        damit live praktisch nie gesetzt (offline-Backtest-Bucket).
        """
        if self._konfig is None:
            return 0
        from bewaesserung.ml.regime_klassifikation import (
            REGIME_REGEN_UNBEKANNT,
            klassifiziere_regime,
            lade_regime_kontext,
        )
        kandidaten = await self._speicher.hole_regime_stempel_kandidaten(jetzt)
        if not kandidaten:
            return 0
        anzahl = 0
        pro_zone: dict[str, list[dict]] = {}
        for k in kandidaten:
            pro_zone.setdefault(k["zone_id"], []).append(k)
        for zone_id, zeilen in pro_zone.items():
            von = min(z["zeitstempel"] for z in zeilen) - timedelta(hours=6)
            bis = max(z["zeitstempel"] for z in zeilen) + timedelta(hours=24)
            try:
                kontext = await lade_regime_kontext(
                    self._speicher, self._konfig, zone_id, von, bis,
                )
            except Exception:
                logger.exception(
                    "empfehlungs_audit.regime_kontext_fehler",
                    zone_id=zone_id,
                )
                continue
            for zeile in zeilen:
                updates: dict[str, str] = {}
                for horizont, spalte in ((6, "regime_6h"), (24, "regime_24h")):
                    if zeile.get(spalte) is not None:
                        continue
                    regime = klassifiziere_regime(
                        zeile["zeitstempel"], horizont, kontext,
                    )
                    if (
                        regime == REGIME_REGEN_UNBEKANNT
                        and (
                            kontext.wetter_max_ts is None
                            or kontext.wetter_max_ts
                            < zeile["zeitstempel"] + timedelta(hours=horizont)
                        )
                    ):
                        continue  # ERA5-Lag — naechster Tick.
                    updates[spalte] = regime
                if not updates:
                    continue
                try:
                    await self._speicher.setze_empfehlungs_audit_regime(
                        audit_id=zeile["id"],
                        regime_6h=updates.get("regime_6h"),
                        regime_24h=updates.get("regime_24h"),
                    )
                    anzahl += 1
                except Exception:
                    logger.exception(
                        "empfehlungs_audit.regime_persist_fehler",
                        audit_id=zeile["id"],
                    )
        return anzahl

    async def _sensor_nah_zeitpunkt(
        self, zone_id: str, ziel: datetime,
    ) -> float | None:
        """Sucht eine Sensor-Messung +/- EVAL_TOLERANZ_MIN um `ziel`."""
        if ziel > datetime.now():
            return None  # Zukunft — noch keine Messung
        von = ziel - timedelta(minutes=EVAL_TOLERANZ_MIN)
        bis = ziel + timedelta(minutes=EVAL_TOLERANZ_MIN)
        messungen = await self._speicher.hole_messungen(
            zone_id, von=von, bis=bis,
        )
        if not messungen:
            return None
        # Naehestes zur Zielzeit
        bestes = min(
            messungen, key=lambda m: abs((m.zeitstempel - ziel).total_seconds()),
        )
        if bestes.boden_feuchte is None:
            return None
        return float(bestes.boden_feuchte)
