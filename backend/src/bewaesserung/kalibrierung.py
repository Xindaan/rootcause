"""T-0063: Automatische Kalibrierung der Gardena-Sensor-Skala.

Die Gardena-Sensor-Skala ist kein VWC, sondern ein lokal kalibrierter
Relativ-Index. Um den Regelbereich (optimum_feuchte_min/max) datengetrieben
zu schaerfen, brauchen wir zwei empirische Referenzpunkte pro Zone:

1. **Feldkapazitaet**: Sensor-Wert, den das Beet 12-24 h nach einem
   durchdringenden Regen (> `regen_min_mm`) annimmt. Das ist der
   obere sinnvolle Regelpunkt — darueber ist Staunaesse.
2. **Welkepunkt-Proxy**: Sensor-Minimum waehrend einer Trockenphase
   (`welkepunkt_min_tage` ohne > 0.5 mm Regen, nur in Saison
   Mai-September). Das ist der untere Referenzpunkt.

Dieser Job laeuft periodisch (Default 6 h) und scannt die letzten
`rueckblick_tage` Tage. Neue Kandidaten werden in Tabelle
`feldkapazitaet_messung` persistiert. Idempotent (kein Duplikat bei
wiederholtem Lauf auf gleichen Daten).

Doku: `docs/recherche_feuchte_waldstauden.md` fuer Hintergrund,
`docs/feldkapazitaet_check_prompt.md` fuer manuelle Auswertung.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from bewaesserung.modelle import (
    Ausloser,
    GesamtKonfig,
    KalibrierungKonfig,
    VentilAktion,
    VentilEreignis,
)
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

TYP_FELDKAPAZITAET = "feldkapazitaet"
TYP_WELKEPUNKT = "welkepunkt_proxy"
# T-0085: Sensor-Anstieg pro Bewaesserungs-Minute, gemessen aus echten
# SCHLIESSEN-Events. Nutzt dieselbe `feldkapazitaet_messung`-Tabelle
# (Schema generisch genug). `wert` = pp/min, `basis_mm` = Dauer in min.
TYP_WIRKUNGSRATE = "wirkungsrate"
# Eval-Fenster nach Bewaesserungs-Ende, in dem die Sensor-Antwort
# stabilisiert sein sollte. 6 h matcht den Standard-Horizont von
# T-0067 / Drift-Eval und reicht fuer Sandboden-Versickerung.
WIRKUNGSRATE_EVAL_OFFSET_H = 6
# Toleranz fuer Sensor-Suche vor (rueckwaerts) und nach Eval-Zeit
# (bidirektional). 75 min deckt die echte Gardena-Cadence ~60 min plus
# Jitter ab — analog T-0069 (`F_VOR_TOLERANZ_MIN 30→75`) fuer Response-
# Features. Mit `F_VOR_VORWAERTS_MIN=5` werden Boundary-Messungen knapp
# nach Bewaesserungs-Start (< 5 min) noch als Pre-Event gewertet
# (Sensor sieht Wasser nicht innerhalb der ersten 5 min, weil 1×/h-
# Cadence den Tick erst spaeter trifft). 45 min nach Eval-Zeit reichen
# bidirektional, weil das Eval-Fenster bei 6h-Offset breiter ist.
WIRKUNGSRATE_F_VOR_TOL_MIN = 75
WIRKUNGSRATE_F_VOR_VORWAERTS_MIN = 5
WIRKUNGSRATE_F_NACH_TOL_MIN = 45
# Filter-Schwellen
WIRKUNGSRATE_MIN_DAUER_S = 60        # < 1 min: kein verwertbares Event
# T-0129 (H-5): Obergrenze gegen Stale-CLOSED-Inflation (Pre-Mortem Akt 4).
# Ohne diese Schwelle landet eine 119-min-Phantom-Dauer (echte 90 min +
# 30 min py-smart-gardena-Reconnect-Delay, Pattern T-0055-B4) im
# Kalibrierungs-Median und korrumpiert die Wirkungsrate-Empfehlung
# (15 pp / 119 min = 0.126 statt korrekt 0.167). Hard-Cap 120 min deckt
# auch lange manuelle Schlauch-Sessions ab; AUTOMATIK-Events werden
# zusaetzlich gegen den konfigurierten zone.max_dauer_sekunden gegateted
# (siehe `_dauer_plausibel`).
WIRKUNGSRATE_MAX_DAUER_S = 120 * 60
WIRKUNGSRATE_AUTOMATIK_TOLERANZ = 1.10   # 10 % ueber zone.max_dauer ok
WIRKUNGSRATE_F_VOR_MAX = 75.0        # bei f_vor > 75 ist Sensor saturiert
# T-0085 Variante B (28.04.): post-Hoc-Saturierungs-Filter. Mikrodrip
# erreicht nie 85+ Sensor-Anzeige (Boden-Aufnahmekapazitaet < 85 %), also
# deutet f_nach >= 85 auf externe Wasserzufuhr im Eval-Fenster hin
# (User-Schlauch, Starkregen, lokales Gewitter). Realdaten-Belege:
# Bambus + Yogaraum 18.04. 13:00, Yogaraum 09.04. 07:00 — alle hatten
# 5 min Bewaesserung mit f_nach in [85, 95].
WIRKUNGSRATE_F_NACH_MAX = 85.0
WIRKUNGSRATE_DELTA_MIN = 5.0         # Quantisierung 5 pp Gardena
WIRKUNGSRATE_DELTA_MAX = 30.0        # darueber: Mehrfach-Event/Regen vermutet
# Wetter-Querpruefung: wenn im Eval-Fenster mehr als WIRKUNGSRATE_REGEN_MAX_MM
# Niederschlag faellt, ist die Sensor-Antwort durch Regen mitverursacht und
# nicht der Bewaesserung zurechenbar. 1 mm/6h ist konservativ — schon ein
# leichter Niesel-Tag (12.04. mit 0.1-0.4 mm/h) akkumuliert mehr.
WIRKUNGSRATE_REGEN_MAX_MM = 1.0

# Fenster fuer Plateau-Erkennung nach Regen: 12-24 h nach Regen-Ende
# ist der Boden noch gesaettigt, aber Ueberstand hat versickert.
FELDKAP_OFFSET_MIN_H = 12
FELDKAP_OFFSET_MAX_H = 24
# Plateau: 3 aufeinanderfolgende Messungen mit |delta| < plateau_max_delta
PLATEAU_MIN_MESSUNGEN = 3
# Regen-Schwelle pro Stunde fuer "Trocken"-Tag: darunter gilt Tag als trocken
TROCKEN_MM_PRO_TAG = 0.5


def _dauer_plausibel(
    event: VentilEreignis,
    zone_max_dauer_s: int | None,
    zone_id: str,
) -> bool:
    """T-0129 (H-5): Garbage-Filter fuer Wirkungsrate-Kalibrierung.

    Lehnt Events mit unplausibler Dauer ab, damit Stale-CLOSED-Inflation
    (T-0055-B4 Pattern) den Kalibrierungs-Median nicht korrumpiert.

    Zwei Schichten:
    1. Hard-Cap `WIRKUNGSRATE_MAX_DAUER_S` -- gilt fuer alle Ausloeser
       (auch MANUELL). 120 min ist grosszuegig fuer Schlauch-Sessions.
    2. Per-Zone-Cap nur fuer AUTOMATIK: `zone.max_dauer_sekunden *
       WIRKUNGSRATE_AUTOMATIK_TOLERANZ`. Watchdog-Timer schliesst nach
       max_dauer + 30 s, also sind 10 % Toleranz mehr als safe; alles
       darueber muss Phantom-Dauer sein.
    """
    dauer = event.dauer_sekunden
    if dauer > WIRKUNGSRATE_MAX_DAUER_S:
        logger.info(
            "kalibrierung.wirkungsrate.dauer_hard_cap",
            zone_id=zone_id,
            dauer_min=round(dauer / 60.0, 1),
            cap_min=WIRKUNGSRATE_MAX_DAUER_S // 60,
            ausloeser=event.ausloser.value,
        )
        return False
    if (
        event.ausloser == Ausloser.AUTOMATIK
        and zone_max_dauer_s is not None
        and dauer > zone_max_dauer_s * WIRKUNGSRATE_AUTOMATIK_TOLERANZ
    ):
        logger.info(
            "kalibrierung.wirkungsrate.dauer_zone_cap",
            zone_id=zone_id,
            dauer_min=round(dauer / 60.0, 1),
            zone_max_min=round(zone_max_dauer_s / 60.0, 1),
            toleranz=WIRKUNGSRATE_AUTOMATIK_TOLERANZ,
        )
        return False
    return True


def _wirkungsrate_kandidaten_aus_schliessen(
    schliessen: list[VentilEreignis],
) -> list[VentilEreignis]:
    """T-0366: Pre-Soak-Puls + Hauptdose als EINEN Kandidaten behandeln.

    Reine `phase='pre_soak'`-Gruppen ohne Hauptdose liefern kein belastbares
    Lernsignal und werden verworfen. Ungruppierte Einzellaeufe bleiben
    unveraendert.
    """
    gruppen: dict[str, list[VentilEreignis]] = {}
    kandidaten: list[VentilEreignis] = []
    for e in schliessen:
        if e.lauf_gruppe:
            gruppen.setdefault(e.lauf_gruppe, []).append(e)
        elif e.phase == "pre_soak":
            continue
        else:
            kandidaten.append(e)

    for gruppe in gruppen.values():
        gruppe = sorted(gruppe, key=lambda e: e.zeitstempel)
        if not any(e.phase == "haupt" for e in gruppe):
            continue
        letzter = gruppe[-1]
        dauer = sum(max(0, e.dauer_sekunden) for e in gruppe)
        if dauer < 600:
            continue
        kandidaten.append(
            letzter.model_copy(update={
                "dauer_sekunden": dauer,
                "phase": None,
            })
        )
    return sorted(kandidaten, key=lambda e: e.zeitstempel)


class KalibrationsJob:
    """Periodisch: scannt Sensor+Wetter auf Feldkapazitaets- und
    Welkepunkt-Kandidaten, persistiert neue Werte.
    """

    def __init__(
        self,
        speicher: Speicher,
        konfig: GesamtKonfig,
        kal_konfig: KalibrierungKonfig,
    ) -> None:
        self._speicher = speicher
        self._konfig = konfig
        self._kal = kal_konfig
        self._intervall = timedelta(hours=kal_konfig.intervall_stunden)
        self._letzte_aktualisierung: datetime | None = None
        # T-0108: Fehler-/Erfolg-Sichtbarkeit fuer /api/ml/status.
        # Vorher war ein Scan-Crash still (nur logger.exception).
        self._letzter_erfolg: datetime | None = None
        self._letzter_fehler: dict | None = None

    @property
    def letzter_erfolg(self) -> datetime | None:
        """Zeitpunkt des letzten erfolgreichen Scans (T-0108)."""
        return self._letzter_erfolg

    @property
    def letzter_fehler(self) -> dict | None:
        """`{zeit, typ, nachricht}` des letzten Crashs, sonst None (T-0108)."""
        return self._letzter_fehler

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> bool:
        """Laeuft hoechstens einmal pro `intervall_stunden`. True wenn gelaufen."""
        if not self._kal.aktiv:
            return False
        jetzt = jetzt or datetime.now()
        if (self._letzte_aktualisierung
                and jetzt - self._letzte_aktualisierung < self._intervall):
            return False
        try:
            await self._scan_alle_zonen(jetzt)
        except Exception as exc:
            logger.exception("kalibrierung.fehler")
            # T-0108: Crash war vorher unsichtbar — jetzt in /api/ml/status.
            self._letzter_fehler = {
                "zeit": jetzt.isoformat(),
                "typ": type(exc).__name__,
                "nachricht": str(exc)[:300],
            }
            return False
        self._letzte_aktualisierung = jetzt
        # T-0108: erfolgreicher Scan — Fehler-Banner wieder aufloesen.
        self._letzter_erfolg = jetzt
        self._letzter_fehler = None
        return True

    async def _scan_alle_zonen(self, jetzt: datetime) -> None:
        """Hauptschleife: pro Zone (mit Ventil-Kanal) Sensor-Daten holen,
        Regen-Peaks finden, Feldkapazitaet + Welkepunkt ableiten.
        """
        von = jetzt - timedelta(days=self._kal.rueckblick_tage)
        # Pro Zone ein Standort fuer Regen-Daten (aus config.standorte)
        zone_zu_standort = self._zone_standort_mapping()
        zonen = [z for z in self._konfig.zonen if z.ventil_kanal is not None]
        if not zonen:
            return

        # Wetter-Archiv pro Standort einmalig laden (viel weniger Queries).
        # T-0063a: kombiniert Archiv + Vorhersage-Fallback, damit der Job
        # den aktuellen Regen-Event nicht erst nach ERA5-Latenz (3-5 Tage)
        # sieht.
        wetter_cache: dict[str, list] = {}
        for standort_id in set(zone_zu_standort.values()):
            if not standort_id:
                continue
            wetter_cache[standort_id] = await self._speicher.hole_wetter_kombiniert(
                standort_id, von=von, bis=jetzt,
            )

        # T-0085: Bewaesserungs-Events einmalig pro Lauf laden (alle Zonen),
        # damit nicht pro Zone separate Queries.
        ventil_events_alle = await self._speicher.hole_alle_ventil_ereignisse(
            von, jetzt,
        )

        # T-0150: Cluster-Geschwister-Map: zone_id -> Liste der zone_ids im
        # selben Hahn-Cluster (ohne sich selbst). Druckverlust durch
        # paralleler Bewaesserung im selben Hahn verfaelscht die Wirkungs-
        # rate (Sprinkler-Topologie kollabiert, Mikrodrip-Druckminderer
        # geraet aus dem Arbeitsbereich). Events aus Geschwister-Zonen
        # werden im Eval-Fenster als Konkurrenz gewertet.
        cluster_geschwister = self._cluster_geschwister_map()

        # T-0387: ml_ausschluss_fenster pro Zone gruppieren (3-Tupel wie
        # main._baue_ausschluss_fenster_pro_zone). Die Feldkapazitaets-/
        # Welkepunkt-Scans reichten die Fenster bisher NICHT durch -> Werte aus
        # Sensor-Umzug/Lager-Phasen kontaminierten persistente Referenzen
        # (hecke: Lager-Phase -> welkepunkt=0.0 live). geraet_id=None schneidet
        # alle Sensoren der Zone weg, sonst nur den betroffenen (T-0386-Semantik).
        ausschluss_pro_zone: dict[
            str, list[tuple[datetime, datetime, str | None]]
        ] = {}
        for f in self._konfig.ml_ausschluss_fenster:
            ausschluss_pro_zone.setdefault(f.zone_id, []).append(
                (f.von, f.bis, f.geraet_id)
            )

        gesamt_neu = 0
        gesamt_neu_wirkung = 0
        for zone in zonen:
            standort_id = zone_zu_standort.get(zone.zone_id)
            if not standort_id or standort_id not in wetter_cache:
                continue
            wetter = wetter_cache[standort_id]
            ausschluss = ausschluss_pro_zone.get(zone.zone_id)
            neu_f = await self._scan_feldkapazitaet(
                zone.zone_id, von, jetzt, wetter, ausschluss,
            )
            neu_w = await self._scan_welkepunkt(
                zone.zone_id, von, jetzt, wetter, ausschluss,
            )
            # T-0085: Wirkungsrate aus realen Bewaesserungs-Events.
            zone_events = [e for e in ventil_events_alle if e.zone_id == zone.zone_id]
            # T-0150: SCHLIESSEN-Events der Cluster-Geschwister fuer
            # Druckverlust-Filter durchreichen.
            geschwister_ids = cluster_geschwister.get(zone.zone_id, set())
            geschwister_events = [
                e for e in ventil_events_alle
                if e.zone_id in geschwister_ids
                and e.aktion == VentilAktion.SCHLIESSEN
                and e.dauer_sekunden > 0
            ] if geschwister_ids else []
            neu_r = await self._scan_wirkungsrate(
                zone.zone_id, von, jetzt, zone_events, wetter=wetter,
                cluster_geschwister_events=geschwister_events,
            )
            if neu_f or neu_w or neu_r:
                logger.info(
                    "kalibrierung.zone_gescannt",
                    zone_id=zone.zone_id,
                    neu_feldkap=neu_f, neu_welke=neu_w,
                    neu_wirkungsrate=neu_r,
                )
            gesamt_neu += neu_f + neu_w
            gesamt_neu_wirkung += neu_r

        if gesamt_neu > 0 or gesamt_neu_wirkung > 0:
            logger.info(
                "kalibrierung.abgeschlossen",
                gesamt_neu=gesamt_neu,
                gesamt_neu_wirkung=gesamt_neu_wirkung,
            )

        # T-0228 Stufe 2b (T-0247-Scope): nach jedem Kalibrierungs-
        # Scan pruefen, ob fuer Zonen mit manuellem
        # `delta_pp_pro_minute`-Override genug stabile Auto-Kalibrierungs-
        # Werte da sind -- dann eine `pflege_erinnerung` mit
        # typ='wirkungsrate_cleanup' anlegen. Vermeidet Konfig-Drift
        # (statischer Override vs. gelernter Median).
        try:
            await self._pruefe_wirkungsrate_cleanup(jetzt)
        except Exception:
            logger.exception("kalibrierung.cleanup_check_fehler")

    async def _pruefe_wirkungsrate_cleanup(self, jetzt: datetime) -> None:
        """T-0247: Wirkungsrate-Cleanup-Hinweis als Pflege-Erinnerung.

        Trigger pro Zone:
        - `zone.delta_pp_pro_minute` ist gesetzt (= manueller Override).
        - >= 10 Wirkungsrate-Kalibrier-Events letzte 90 Tage.
        - Standard-Abweichung der Median-Werte < 0.05 pp/min.

        Wenn alle drei erfuellt + noch keine offene Cleanup-Erinnerung
        existiert: legt eine an (faellig sofort), Quelle
        'wirkungsrate_cleanup'. Watchdog-Trigger D feuert iMessage
        beim naechsten Tick.
        """
        import statistics
        from datetime import timedelta as _td

        fenster_tage = 90
        von = jetzt - _td(days=fenster_tage)

        # Bestehende offene Cleanup-Erinnerungen einmalig holen, damit
        # wir nicht pro Zone eine eigene Query feuern.
        try:
            offene = await self._speicher.hole_pflege_erinnerungen(
                nur_offen=True,
            )
        except Exception:
            return
        offen_pro_zone: set[str] = {
            e["zone_id"]
            for e in offene
            if e.get("typ") == "wirkungsrate_cleanup" and e.get("zone_id")
        }

        for zone in self._konfig.zonen:
            if zone.delta_pp_pro_minute is None:
                continue  # kein manueller Override -> nichts zu cleanen
            if zone.zone_id in offen_pro_zone:
                continue  # schon eine offene Erinnerung

            try:
                kal = await self._speicher.hole_kalibrierungen(
                    zone_id=zone.zone_id, limit=200,
                )
            except Exception:
                continue
            werte = [
                k["wert"] for k in kal
                if k.get("typ") == "wirkungsrate"
                and k.get("zeitstempel")
                and datetime.fromisoformat(k["zeitstempel"]) >= von
            ]
            if len(werte) < 10:
                continue
            try:
                std = statistics.stdev(werte)
            except statistics.StatisticsError:
                continue
            if std >= 0.05:
                continue

            median = statistics.median(werte)
            try:
                await self._speicher.speichere_pflege_erinnerung(
                    typ="wirkungsrate_cleanup",
                    faellig_am=jetzt,
                    beschreibung=(
                        f"T-0247: Override `delta_pp_pro_minute="
                        f"{zone.delta_pp_pro_minute}` kann entfernt werden -- "
                        f"Auto-Kalibrierungs-Median ({median:.3f} pp/min, "
                        f"std {std:.3f}, n={len(werte)}) ist stabil. "
                        f"YAML-Konfig pruefen, Override loeschen, "
                        f"Backend-Restart."
                    ),
                    zone_id=zone.zone_id,
                    quelle="wirkungsrate_cleanup",
                    jetzt=jetzt,
                )
                logger.info(
                    "kalibrierung.cleanup_hinweis_angelegt",
                    zone_id=zone.zone_id, median=median, std=std, n=len(werte),
                )
            except Exception:
                logger.exception(
                    "kalibrierung.cleanup_hinweis_fehler",
                    zone_id=zone.zone_id,
                )

    def _zone_standort_mapping(self) -> dict[str, str]:
        """Aus `konfig.standorte[]`: zone_id → wetter_standort."""
        mapping: dict[str, str] = {}
        for standort in self._konfig.standorte:
            for zid in standort.zonen:
                if standort.wetter_standort:
                    mapping[zid] = standort.wetter_standort
        return mapping

    def _cluster_geschwister_map(self) -> dict[str, set[str]]:
        """T-0150: zone_id -> Set der zone_ids im selben Hahn-Cluster, aber
        an einem ANDEREN Ventil-Kanal.

        Druckverlust-Konflikt entsteht nur, wenn zwei verschiedene Ventile
        gleichzeitig oeffnen und sich den Wasserhahn teilen. Zonen am
        SELBEN Ventil-Kanal (z.B. bambuswald + bambuswald_yogaraum auf K2)
        sind hydraulisch ein Lauf — kein Konkurrenz-Eintrag.

        Zonen ohne `hahn_cluster` (Default None) haben keine Geschwister.
        """
        cluster_zonen: dict[str, list[tuple[str, int | None]]] = {}
        for zone in self._konfig.zonen:
            if zone.hahn_cluster is None:
                continue
            cluster_zonen.setdefault(zone.hahn_cluster, []).append(
                (zone.zone_id, zone.ventil_kanal),
            )
        geschwister: dict[str, set[str]] = {}
        for cluster_id, paare in cluster_zonen.items():
            for zid, kanal in paare:
                geschwister[zid] = {
                    andere_id for andere_id, andere_kanal in paare
                    if andere_id != zid and andere_kanal != kanal
                }
        return geschwister

    async def _scan_feldkapazitaet(
        self, zone_id: str, von: datetime, bis: datetime, wetter: list,
        ausschluss_fenster: (
            list[tuple[datetime, datetime, str | None]] | None
        ) = None,
    ) -> int:
        """Finde Regen-Peaks > regen_min_mm in 24-h-Fenstern, und je Peak
        einen Sensor-Plateau-Wert 12-24 h spaeter. Gibt Anzahl neuer
        persistierter Kandidaten zurueck.
        """
        # Stunden-Niederschlag sortiert
        stunden = sorted(wetter, key=lambda w: w.zeitstempel)
        if len(stunden) < 24:
            return 0

        # Gleitende 24-h-Summe, finde lokale Maxima ueber Schwelle
        regen_peaks: list[tuple[datetime, float]] = []  # (peak_ende, summe_mm)
        bereits_verarbeitet: set[int] = set()  # Index-Schutz gegen doppelte Peaks
        for i in range(24, len(stunden)):
            summe = sum(
                stunden[j].niederschlag_mm or 0.0
                for j in range(i - 24, i)
            )
            if summe < self._kal.regen_min_mm:
                continue
            # Peak erst abschliessen, wenn mindestens 6 h kein Regen mehr
            # hinterher kommt (sonst mitten im Dauerregen messen)
            if any(
                (stunden[j].niederschlag_mm or 0.0) > 0.5
                for j in range(i, min(i + 6, len(stunden)))
            ):
                continue
            # Dedup: wenn Peak schon innerhalb 24h vorher war, ueberspringen
            if any(idx in bereits_verarbeitet for idx in range(max(0, i - 24), i)):
                continue
            bereits_verarbeitet.add(i)
            regen_peaks.append((stunden[i - 1].zeitstempel, summe))

        if not regen_peaks:
            return 0

        neu = 0
        for peak_ende, summe_mm in regen_peaks:
            # Plateau 12-24 h nach Peak-Ende suchen
            plateau_von = peak_ende + timedelta(hours=FELDKAP_OFFSET_MIN_H)
            plateau_bis = peak_ende + timedelta(hours=FELDKAP_OFFSET_MAX_H)
            if plateau_bis > bis:
                continue  # Fenster reicht in die Zukunft
            werte = await self._speicher.hole_feuchte_werte(
                zone_id, plateau_von, plateau_bis, ausschluss_fenster,
            )
            # T-0387: 0.0 = Sensor-Ausfall/Lager, kein real messbares Plateau
            # nach Regen. Analog zum 0-Artefakt-Filter in
            # entscheidung._p10_tagesmin_schaetzung. Ein reines 0.0-"Plateau"
            # (hecke 07.-14.05., Sensor im Lager) faellt so unter die Mindest-
            # zahl und wird nicht als Feldkapazitaet 0.0 persistiert. Deckt auch
            # Faelle ab, die vor dem fruehesten ml_ausschluss_fenster liegen.
            werte = [w for w in werte if w > 0]
            if len(werte) < PLATEAU_MIN_MESSUNGEN:
                continue
            # Plateau: im Zeitraum 12-24 h nach Regen sollte die Feuchte
            # kaum variieren. Nehme Median der Werte als Feldkapazitaets-
            # Kandidat. Variation per Max-Min; zu hoch → kein stabiles
            # Plateau, skip.
            # T-0063a: Default `plateau_max_delta` auf 5.0 erhoeht (s.
            # KalibrierungKonfig) — der Gardena-Sensor springt in
            # 5-%-Stufen, das fruehere Limit 3.0 war unterhalb der
            # Sensor-Aufloesung und verwarf auch saubere Plateaus.
            if (max(werte) - min(werte)) > 2 * self._kal.plateau_max_delta:
                continue
            import statistics
            wert = round(statistics.median(werte), 1)
            # T-0063a: Bodentemperatur im Plateau-Fenster mitloggen — bei
            # kuehlen Boeden (< 5 C) misst der Gardena-Sensor systematisch
            # niedriger, damit spaetere Saison-/Temperatur-Korrektur
            # moeglich ist.
            boden_temp_median = await self._median_bodentemperatur(
                zone_id, plateau_von, plateau_bis,
            )
            notizen = f"Regen 24h vor: {summe_mm:.1f} mm"
            if boden_temp_median is not None:
                notizen += f", Boden-T median={boden_temp_median:.1f}C"
            await self._speicher.speichere_kalibrierung(
                zeitstempel=plateau_von + (plateau_bis - plateau_von) / 2,
                zone_id=zone_id, typ=TYP_FELDKAPAZITAET,
                wert=wert, basis_mm=summe_mm,
                notizen=notizen,
            )
            neu += 1
        return neu

    async def _median_bodentemperatur(
        self, zone_id: str, von: datetime, bis: datetime,
    ) -> float | None:
        """Hilfsmethode: Median Boden-Temperatur im Plateau-Fenster.
        Gibt None zurueck, wenn keine Messungen oder alle temp=None.
        """
        import statistics
        messungen = await self._speicher.hole_messungen(zone_id, von, bis)
        temps = [
            m.boden_temperatur for m in messungen
            if m.boden_temperatur is not None
        ]
        if not temps:
            return None
        return statistics.median(temps)

    async def _scan_welkepunkt(
        self, zone_id: str, von: datetime, bis: datetime, wetter: list,
        ausschluss_fenster: (
            list[tuple[datetime, datetime, str | None]] | None
        ) = None,
    ) -> int:
        """Finde Trockenphasen > welkepunkt_min_tage in Saison-Monaten,
        je Phase Sensor-Minimum als Welkepunkt-Proxy.
        """
        # Tages-Niederschlags-Summen
        tages_regen: dict[str, float] = {}
        for w in wetter:
            tag = w.zeitstempel.strftime("%Y-%m-%d")
            tages_regen[tag] = tages_regen.get(tag, 0.0) + (w.niederschlag_mm or 0.0)

        # Finde zusammenhaengende Trocken-Sequenzen (Tag-Niederschlag <= 0.5 mm)
        trocken_phasen: list[tuple[datetime, datetime]] = []
        aktuell_start: datetime | None = None
        for tag_str in sorted(tages_regen.keys()):
            tag_dt = datetime.fromisoformat(tag_str)
            regen = tages_regen.get(tag_str, 0.0)
            if regen <= TROCKEN_MM_PRO_TAG:
                if aktuell_start is None:
                    aktuell_start = tag_dt
            else:
                if aktuell_start is not None:
                    phase_ende = tag_dt - timedelta(days=1)
                    dauer = (phase_ende - aktuell_start).days + 1
                    if dauer >= self._kal.welkepunkt_min_tage:
                        trocken_phasen.append((aktuell_start, phase_ende))
                    aktuell_start = None
        # Abschliessende offene Phase
        if aktuell_start is not None:
            phase_ende = datetime.fromisoformat(max(tages_regen.keys()))
            dauer = (phase_ende - aktuell_start).days + 1
            if dauer >= self._kal.welkepunkt_min_tage:
                trocken_phasen.append((aktuell_start, phase_ende))

        if not trocken_phasen:
            return 0

        neu = 0
        for phase_start, phase_ende in trocken_phasen:
            # Saison-Filter: Phase muss innerhalb eines Saison-Monats enden
            if phase_ende.month not in self._kal.saison_monate:
                continue
            werte = await self._speicher.hole_feuchte_werte(
                zone_id, phase_start, phase_ende + timedelta(days=1),
                ausschluss_fenster,
            )
            # T-0387: 0.0-Artefakte (Sensor im Lager/Ausfall) raus, sonst wird
            # der Welkepunkt-Proxy faelschlich 0.0 (hecke 25.-27.05.).
            werte = [w for w in werte if w > 0]
            if len(werte) < 10:  # zu wenige Messungen fuer Minimum
                continue
            min_wert = round(min(werte), 1)
            phase_mitte = phase_start + (phase_ende - phase_start) / 2
            dauer_tage = (phase_ende - phase_start).days + 1
            await self._speicher.speichere_kalibrierung(
                zeitstempel=phase_mitte,
                zone_id=zone_id, typ=TYP_WELKEPUNKT,
                wert=min_wert, basis_mm=float(dauer_tage),
                notizen=f"Trockenphase: {dauer_tage} Tage",
            )
            neu += 1
        return neu

    async def _scan_wirkungsrate(
        self, zone_id: str, von: datetime, bis: datetime,
        ventil_events: list[VentilEreignis],
        wetter: list | None = None,
        cluster_geschwister_events: list[VentilEreignis] | None = None,
    ) -> int:
        """T-0085: Findet abgeschlossene Bewaesserungs-Events und berechnet
        die empirische Wirkungsrate (delta_pp / dauer_min). Pro qualifiziertem
        Event ein Eintrag in `feldkapazitaet_messung` als typ='wirkungsrate'.

        Filter:
          - aktion=SCHLIESSEN, dauer_sekunden >= WIRKUNGSRATE_MIN_DAUER_S
          - ausloeser in {MANUELL, AUTOMATIK} (Heuristik-Phantoms raus)
          - keine ANDERE protokollierte SCHLIESSEN-Bewaesserung im Eval-
            Fenster. `unbekannt`-Heuristik-Events zaehlen erst NACH
            `versickerungs_karenz_stunden` als Konkurrenz — innerhalb der
            Karenz sind sie Heuristik-Phantome der eigenen Wirkung
            (Sand-Boden hat 4-6 h verzoegerte Sensor-Antwort).
          - f_vor + f_nach beide vorhanden (mit Toleranzen)
          - f_vor < WIRKUNGSRATE_F_VOR_MAX (Pre-Saturierung)
          - f_nach < WIRKUNGSRATE_F_NACH_MAX (Post-Saturierung — externe
            Wasserzufuhr im Eval-Fenster, z. B. Starkregen oder Schlauch)
          - WIRKUNGSRATE_DELTA_MIN <= delta < WIRKUNGSRATE_DELTA_MAX
            (Quantisierung 5 pp; Outlier-Cap)
          - Niederschlag im Eval-Fenster <= WIRKUNGSRATE_REGEN_MAX_MM
            (Wetter-Querpruefung gegen `wetter_archiv`)
          - T-0150: keine zeitliche Ueberlappung mit SCHLIESSEN-Events
            aus Cluster-Geschwister-Zonen (anderer Ventil-Kanal am
            gleichen Hahn). Druck-Konflikt verfaelscht die Sensor-
            Antwort der eigenen Wirkung.

        Idempotent: speichere_kalibrierung() macht Stunden-Dedup.
        Returns: Anzahl neu gespeicherter Eintraege.
        """
        # Versickerungs-Karenz pro Zone (T-0071): bei Sand laeuft die
        # Sensor-Antwort 4-6 h nach. Heuristik-Events innerhalb der
        # Karenz sind Phantome der eigenen Bewaesserungs-Wirkung.
        karenz_h = 3
        zone_konfig = None
        for z in self._konfig.zonen:
            if z.zone_id == zone_id:
                zone_konfig = z
                karenz_h = z.versickerungs_karenz_stunden
                break
        karenz_dt = timedelta(hours=karenz_h)

        # ALLE SCHLIESSEN-Events fuer den Konkurrenz-Filter (auch
        # 'unbekannt'-Heuristik = nicht protokollierte Wasserzufuhr).
        alle_schliessen_roh = sorted(
            [
                e for e in ventil_events
                if e.aktion == VentilAktion.SCHLIESSEN
                and e.dauer_sekunden >= WIRKUNGSRATE_MIN_DAUER_S
            ],
            key=lambda e: e.zeitstempel,
        )
        alle_schliessen = _wirkungsrate_kandidaten_aus_schliessen(
            alle_schliessen_roh,
        )
        # Kandidaten-Events fuer Wirkungsrate-Berechnung (nur protokolliert).
        # T-0168: AQUABLOOM bewusst NICHT in der Allowlist — Aquabloom-Pulse
        # sind 10 min × 0.083 L = winzig, Sensor-Antwort liegt bei 5-pp-
        # Quantisierung unter dem Rauschen. Wuerde den Wirkungsraten-Median
        # verzerren ohne sinnvolles Lernsignal. AQUABLOOM landet stattdessen
        # in den ML-Response-Features mit `delta_6h > 1.0`-Filter (siehe
        # `ml/response_features.py`).
        schliessen_events = [
            e for e in alle_schliessen
            if e.ausloser in (Ausloser.MANUELL, Ausloser.AUTOMATIK)
            # F3: Heuristik-Events tragen eine aus dem Feuchte-delta
            # KONSTRUIERTE Pseudo-Dauer (sensor_backfill `dauer = delta x
            # EICHUNG`). `rate = delta / dauer_min` waere dann zirkulaer
            # (≡ konstant ~1/EICHUNG, kein Lernsignal) -- auch nachdem der
            # User das Event auf `manuell` geflippt hat. Per ventil_id raus.
            and e.ventil_id != "sensor_heuristik"
        ]
        # H-5: Phantom-Dauer-Filter (Pre-Mortem Akt 4). Stale-CLOSED-Pattern
        # T-0055-B4 inflationiert die Dauer um ~30 min bei py-smart-gardena-
        # Reconnects. Hard-Cap + Per-Zone-Cap fuer AUTOMATIK fangen das.
        zone_max_dauer_s: int | None = (
            int(zone_konfig.max_dauer_sekunden) if zone_konfig else None
        )
        lead_geraet = (
            getattr(zone_konfig, "aggregat_lead_geraet", None)
            if zone_konfig is not None else None
        )
        schliessen_events = [
            e for e in schliessen_events
            if _dauer_plausibel(e, zone_max_dauer_s, zone_id)
        ]
        if not schliessen_events:
            return 0

        # Sensor-Messungen vor & nach jedem Event in einem Bulk-Read.
        # `f_vor` wird bei `t_start = close - dauer` gesucht, daher muss
        # sensor_von das frueheste t_start abdecken, nicht das frueheste
        # close-Zeitstempel.
        sensor_von = (
            min(
                e.zeitstempel - timedelta(seconds=e.dauer_sekunden)
                for e in schliessen_events
            )
            - timedelta(minutes=WIRKUNGSRATE_F_VOR_TOL_MIN)
        )
        sensor_bis = (
            max(e.zeitstempel for e in schliessen_events)
            + timedelta(hours=WIRKUNGSRATE_EVAL_OFFSET_H)
            + timedelta(minutes=WIRKUNGSRATE_F_NACH_TOL_MIN)
        )
        if sensor_von > bis or sensor_bis < von:
            return 0
        sensor_von = max(sensor_von, von)
        sensor_bis = min(sensor_bis, bis)
        messungen = await self._speicher.hole_messungen(
            zone_id, sensor_von, sensor_bis,
        )
        if not messungen:
            return 0
        # `hole_messungen` sortiert DESC, `_finde_naechste_messung`
        # erwartet aber ASC (break-Logik).
        messungen = list(reversed(messungen))

        # T-0366/F9-Paar-Logik lebt jetzt am Ursprung (response_features);
        # Import bleibt lazy, damit kalibrierung ohne ML-Extras importierbar
        # bleibt (response_features braucht pandas).
        from bewaesserung.ml.response_features import (
            _feuchte_paar_gleiches_geraet,
        )

        eval_offset = timedelta(hours=WIRKUNGSRATE_EVAL_OFFSET_H)
        neu = 0
        for event in schliessen_events:
            t_end = event.zeitstempel
            t_start = t_end - timedelta(seconds=event.dauer_sekunden)
            t_eval = t_end + eval_offset
            if t_eval > bis:
                # Eval-Fenster ragt in die Zukunft — beim naechsten Lauf
                # erneut versuchen. Idempotenz schuetzt vor Doppelung.
                continue
            # Andere SCHLIESSEN-Events im Eval-Fenster:
            # - protokollierte (manuell/automatik): immer Konkurrenz
            # - `unbekannt` (Heuristik): nur Konkurrenz wenn NACH der
            #   Versickerungs-Karenz (sonst Phantom der eigenen Wirkung)
            karenz_grenze = event.zeitstempel + karenz_dt
            # IGNORIERT-Events sind vom User als Phantom markiert (Regen/
            # Glitch) und gelten nie als Konkurrenz. UNBEKANNT nur, wenn
            # nach der Versickerungs-Karenz (sonst Phantom der eigenen Wirkung).
            ueberlappung = any(
                event.zeitstempel < other.zeitstempel <= t_eval
                and other.ausloser != Ausloser.IGNORIERT
                and (
                    other.ausloser != Ausloser.UNBEKANNT
                    or other.zeitstempel > karenz_grenze
                )
                for other in alle_schliessen
                if other is not event
            )
            if ueberlappung:
                continue
            # T-0150: Cluster-Geschwister-Pruefung (Druckverlust durch
            # parallelen Lauf am gleichen Hahn). Wenn ein anderes Ventil
            # im selben Hahn-Cluster im Zeitraum [t_start, t_eval]
            # geoeffnet/geschlossen war, wird die eigene Sensor-Antwort
            # durch den Druckverlust verfaelscht (Sprinkler-Reichweite,
            # Mikrodrip-Druckminderer-Arbeitsbereich). Fenster bewusst
            # weiter als die reine Bewaesserungs-Dauer, weil Sensor-
            # Antwort verzoegert ankommt.
            if cluster_geschwister_events:
                geschwister_konflikt = any(
                    # Geschwister-Event hatte ueberlappende Bewaesserungs-Phase
                    # (von other_start bis other_zeitstempel = SCHLIESSEN)
                    # mit unserem [t_start, t_eval]-Fenster.
                    max(
                        t_start,
                        g.zeitstempel - timedelta(seconds=g.dauer_sekunden),
                    ) <= min(t_eval, g.zeitstempel)
                    for g in cluster_geschwister_events
                )
                if geschwister_konflikt:
                    logger.info(
                        "kalibrierung.wirkungsrate.cluster_konflikt",
                        zone_id=zone_id,
                        event_zeit=event.zeitstempel.isoformat(),
                    )
                    continue
            # Wetter-Querpruefung: Regen im Eval-Fenster (Variante B,
            # 28.04.). Standort-spezifischer Wetter-Cache aus
            # `_scan_alle_zonen` durchgereicht; bei None wird die
            # Pruefung uebersprungen (Tests ohne Wetter-Setup).
            if wetter is not None:
                regen = sum(
                    (w.niederschlag_mm or 0.0) for w in wetter
                    if t_end <= w.zeitstempel <= t_eval
                )
                if regen > WIRKUNGSRATE_REGEN_MAX_MM:
                    continue
            f_vor, f_nach, sensor_geraet = _feuchte_paar_gleiches_geraet(
                messungen, t_start, t_eval, lead_geraet,
                f_vor_toleranz_min=WIRKUNGSRATE_F_VOR_TOL_MIN,
                f_vor_vorwaerts_min=WIRKUNGSRATE_F_VOR_VORWAERTS_MIN,
                f_nach_toleranz_min=WIRKUNGSRATE_F_NACH_TOL_MIN,
            )
            if f_vor is None or f_nach is None:
                continue
            if f_vor >= WIRKUNGSRATE_F_VOR_MAX:
                continue
            # Post-Saturierung: f_nach >= 85 deutet auf externe Wasserzufuhr.
            if f_nach >= WIRKUNGSRATE_F_NACH_MAX:
                continue
            delta = f_nach - f_vor
            if delta < WIRKUNGSRATE_DELTA_MIN or delta >= WIRKUNGSRATE_DELTA_MAX:
                continue
            dauer_min = event.dauer_sekunden / 60.0
            if dauer_min <= 0:
                continue
            rate = round(delta / dauer_min, 3)
            await self._speicher.speichere_kalibrierung(
                zeitstempel=t_end,
                zone_id=zone_id, typ=TYP_WIRKUNGSRATE,
                wert=rate, basis_mm=round(dauer_min, 1),
                notizen=(
                    f"delta={delta:.1f}pp f_vor={f_vor:.1f} "
                    f"f_nach={f_nach:.1f} ausloeser={event.ausloser.value} "
                    f"sensor={sensor_geraet or ''}"
                ),
            )
            neu += 1
        return neu
