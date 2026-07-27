"""T-0168/T-0177: AquaBloom-Auto-Klassifikations-Job.

Synthetisches Bewaesserungs-Logging fuer Solar-Mini-Pumpen (Gardena
AquaBloom) an FYTA-Topfpflanzen ohne Smart-Ventil.

**Strategie (T-0177 Refactor 10.05.2026)**:

Statt blind nach Intervall Pulse zu schreiben, **konvertiert** dieser
Job die echten Sensor-Heuristik-Spruenge (UNBEKANNT-Events) in
AQUABLOOM-Events:

1. Sensor-Heuristik (T-0055-B1) erkennt automatisch jeden grossen
   Sensor-Sprung (≥5 pp) und schreibt UNBEKANNT-OEFFNEN+SCHLIESSEN-Paare.
2. AquabloomJob laeuft 1×/h, schaut pro AquaBloom-konfigurierter Zone:
   - Gibt es UNBEKANNT-OEFFNEN-Events seit `basis + 0.5×Intervall`?
   - Wenn ja, frueheste konvertieren: `ausloser = AQUABLOOM`, Liter via
     Tropfer-Formel mit Konfig-Dauer (nicht Heuristik-Dauer).

**Warum kein blindes Intervall**: AquaBloom-Pumpe driftet — User-
Beobachtung 11.05.: 46h-Intervall statt konfigurierter 48h (Real-Welt-
Drift ~4%). Blindes Schreiben wuerde aus dem Sync laufen, doppelte
Events erzeugen, plus Heuristik-UNBEKANNT-Phantom bleibt.

**Plausibilitaets-Fenster**: `[basis + 0.5×intervall, basis + 1.5×intervall]`
- Zu frueh (< 0.5×) → vermutlich nicht AquaBloom, sondern Glitch oder
  manueller Backup-Guss. Bleibt UNBEKANNT, User klassifiziert manuell.
- Zu spaet (> 1.5×) → Pumpe wahrscheinlich ausgefallen, kein Anker-Reset.
  User soll im Frontend "Manuell gegossen" oder Konfig-Anker neu setzen.

**Anker-Logik** (unveraendert von T-0168):

`basis = max(letzter_db_aquabloom_event, anker_aus_konfig)`. Bei leerer
DB + leerem Anker: Job ruht still und loggt Warnung.

**ML-Behandlung**:
- Wirkungsrate-Median (`kalibrierung._scan_wirkungsrate`) ignoriert
  AQUABLOOM (zu kleine Pulse fuer 5-pp-Sensor-Quantisierung).
- Response-Features (`ml/response_features.py`) nehmen AquaBloom-Rows
  nur bei `delta_6h > 1.0` ins Training.
- Feuchte-/Bilanz-Features (T-0048 + bilanz.py) sehen AquaBloom als
  echte Wasserquelle.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from bewaesserung.modelle import (
    Ausloser,
    GesamtKonfig,
    VentilAktion,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()


# T-0202 (2026-05-17): Cadence von 60 min auf 5 min reduziert.
# Begruendung: Heuristik schreibt UNBEKANNT-Events synchron mit der
# Sensor-Beat-Cadence (FYTA ~15 min). Wenn der Backend-Service kurz
# vor einem AquaBloom-Puls neu startet, laeuft der Initial-AquabloomJob
# einmal ohne neue Events (alles schon konvertiert), setzt
# `_letzte_aktualisierung = jetzt` und blockt sich dann fuer 60 min.
# Der Heuristik-Event landet im Frontend als "Was war das?"-Dialog,
# bis der naechste Job-Tick kommt — frustrierend fuer den User.
# Realfall 17.05.: Backend-Restart 06:17, AquaBloom-Puls 06:22 ->
# 55 min Latenz bis Konvertierung. Mit 5-min-Cadence statt 60: max.
# 5 min Latenz. Plus: 5 min entspricht dem Haupt-Loop-Tick
# (ENTSCHEIDUNGSINTERVALL_SEKUNDEN = 300), also keine zusaetzlichen
# Aufrufe — alles laeuft im bestehenden Tick. SQL-Cost pro Lauf: ein
# UPDATE pro Heuristik-Event, in 99% der Faelle nichts (kein Event
# im Plausibilitaets-Fenster).
INTERVALL_MINUTEN_DEFAULT = 5

# Plausibilitaets-Fenster relativ zum Intervall.
# 0.5 deckt 24h-Drift bei 48h-Intervall ab. 1.5 deckt verspaeteten
# Puls + 24h-Ausfall der Pumpe.
FENSTER_FRUEH_FAKTOR = 0.5
FENSTER_SPAET_FAKTOR = 1.5

# T-0188: Multi-Zyklus-Fenster. Wenn ein Puls verpasst wurde (Heuristik
# schwieg, User klassifizierte den vorigen Puls als `manuell`, Backend-
# Restart waehrend Puls etc.), driftet das Single-Zyklus-Fenster
# [0.5×N, 1.5×N] permanent weg. Multi-Zyklus prueft fuer
# N ∈ {1, 2, ..., MAX_ZYKLEN} jeweils [N×0.75, N×1.25] und konvertiert
# pro Zyklus maximal einen Sprung. So fangen wir auch verspaetete Pulse
# auf (Realfall 15.05.: letzter konvertierter Puls 11.05., naechster
# tatsaechlich 15.05. = 96h = 2 Zyklen weiter).
MAX_ZYKLEN = 4   # 4 × 48h = 8 Tage Lookback genug fuer Pumpenausfall
ZYKLUS_TOLERANZ = 0.25   # ±25% pro Zyklus

# Toleranz zwischen UNBEKANNT-OEFFNEN und zugehoerigem SCHLIESSEN.
# Sensor-Heuristik schreibt Paare aus zwei aufeinanderfolgenden
# Messungen (~15 min Cadence) → 30 min Fenster reicht.
PAAR_TOLERANZ_MIN = 30

# T-0339: Cadence aus den Sensor-Anstiegsflanken inferieren statt aus der
# (hand-verstellbaren, driftenden) Config. Der reale Pump-Takt steht im
# Median-Abstand aufeinanderfolgender UNBEKANNT/sensor_heuristik-OEFFNEN.
MIN_FLANKEN_FUER_INFERENZ = 3   # >= 3 Flanken -> >= 2 Abstaende fuer Median
MIN_INFER_INTERVALL_H = 4.0     # unter 4h unplausibel fuer Solar-Tropfpumpe
MAX_INFER_INTERVALL_H = 48.0    # ueber 48h -> Config vertrauen (zu wenig Signal)


class AquabloomJob:
    """T-0177: AquaBloom-Auto-Klassifikation aus Sensor-Heuristik."""

    def __init__(
        self,
        speicher: Speicher,
        konfig: GesamtKonfig,
        intervall_minuten: int = INTERVALL_MINUTEN_DEFAULT,
    ) -> None:
        self._speicher = speicher
        self._konfig = konfig
        self._intervall = timedelta(minutes=intervall_minuten)
        self._letzte_aktualisierung: datetime | None = None
        # T-0294a: Throttle fuer die Cadence-Drift-Warnung (1x/24h/Zone),
        # damit gestrandete UNBEKANNT-Events nicht jeden 5-min-Lauf warnen.
        self._drift_warnung_zuletzt: dict[str, datetime] = {}

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> int:
        """Faellig-Gate + Hauptloop. Gibt Anzahl konvertierter Pulse zurueck."""
        jetzt = jetzt or datetime.now()
        if (
            self._letzte_aktualisierung
            and jetzt - self._letzte_aktualisierung < self._intervall
        ):
            return 0
        gesamt = 0
        for zone in self._konfig.zonen:
            try:
                gesamt += await self._scan_zone(zone, jetzt)
            except Exception:
                logger.exception(
                    "aquabloom.zone_fehler", zone_id=zone.zone_id,
                )
        self._letzte_aktualisierung = jetzt
        if gesamt > 0:
            logger.info(
                "aquabloom.heuristik_konvertiert_gesamt", anzahl=gesamt,
            )
        return gesamt

    async def _scan_zone(self, zone: ZonenKonfig, jetzt: datetime) -> int:
        """Pro Zone: Heuristik-Spruenge in Multi-Zyklus-Fenstern zu AQUABLOOM.

        T-0188: Iteriert N ∈ {1..MAX_ZYKLEN}, jedes Fenster
        [basis + N×(1-ZYKLUS_TOLERANZ)×intervall, basis + N×(1+ZYKLUS_TOLERANZ)×intervall].
        Pro Zyklus wird maximal ein UNBEKANNT-Sprung zu AQUABLOOM konvertiert
        (frueheste Position). Dadurch werden verpasste Pulse auch nachtraeglich
        eingefangen, ohne dass der Anker permanent driftet.
        """
        if not _ist_konfiguriert(zone):
            return 0
        if not _in_saison(zone, jetzt):
            return 0

        letzter_db_puls = await self._speicher.juengstes_aquabloom_event(
            zone.zone_id,
        )
        konfig_anker = zone.aquabloom_anker_zeitstempel

        kandidaten = [t for t in (letzter_db_puls, konfig_anker) if t is not None]
        if not kandidaten:
            logger.warning(
                "aquabloom.kein_anker",
                zone_id=zone.zone_id,
                hinweis="Setze aquabloom_anker_zeitstempel in der Konfig.",
            )
            return 0
        basis = max(kandidaten)

        # T-0339: reale Cadence aus den Sensor-Flanken inferieren; Config nur
        # Fallback. So muss der User beim Verstellen der Solarpumpe nichts mehr
        # in der Config nachziehen (der frueher stille T-0294-Miss: Config 24h,
        # Pumpe real 12h -> die 12h-Pulse fielen aus den 24h-Fenstern).
        konfig_intervall_h = float(zone.aquabloom_pumpen_intervall_stunden)
        intervall_h = await self._inferiere_intervall_h(
            zone, jetzt, konfig_intervall_h,
        )
        gesamt_konvertiert = 0

        for n in range(1, MAX_ZYKLEN + 1):
            fruehestens = basis + timedelta(
                hours=intervall_h * n * (1.0 - ZYKLUS_TOLERANZ),
            )
            spaetestens = basis + timedelta(
                hours=intervall_h * n * (1.0 + ZYKLUS_TOLERANZ),
            )
            if jetzt < fruehestens:
                # Aktueller Zyklus liegt noch in der Zukunft -> alle
                # weiteren Zyklen liegen noch weiter weg. Abbruch.
                break
            bis = min(jetzt, spaetestens)

            # UNBEKANNT-Heuristik-Events der Zone im plausiblen Fenster holen.
            events = await self._speicher.hole_ventil_ereignisse(
                zone.zone_id, von=fruehestens, bis=bis,
            )
            oeffnen_kandidaten = [
                e for e in events
                if e.ausloser == Ausloser.UNBEKANNT
                and e.aktion == VentilAktion.OEFFNEN
                and e.ventil_id == "sensor_heuristik"
            ]
            if not oeffnen_kandidaten:
                continue

            # Den fruehesten Sprung als AquaBloom-Puls werten — bei
            # mehreren nah beieinander ist der erste die Bewaesserung.
            oeffnen = min(oeffnen_kandidaten, key=lambda e: e.zeitstempel)
            schliessen = self._finde_paar_schliessen(events, oeffnen)

            konvertiert = await self._konvertiere_zu_aquabloom(
                zone, oeffnen, schliessen,
            )
            gesamt_konvertiert += konvertiert
            if konvertiert > 0:
                logger.info(
                    "aquabloom.zyklus_konvertiert",
                    zone_id=zone.zone_id,
                    zyklus=n,
                    zeit=oeffnen.zeitstempel.isoformat(),
                )

        # T-0294a: Defense-in-Depth -- Cadence-Drift sichtbar machen. Bekommt
        # das EFFEKTIVE Intervall: hat die Inferenz gegriffen, matcht es die
        # Realitaet -> keine Falsch-Warnung; fiel sie auf Config zurueck,
        # warnt der Detektor wie bisher.
        await self._pruefe_cadence_drift(zone, jetzt, intervall_h)
        return gesamt_konvertiert

    async def _inferiere_intervall_h(
        self, zone: ZonenKonfig, jetzt: datetime, konfig_intervall_h: float,
    ) -> float:
        """T-0339: schaetzt den realen Pump-Takt (Stunden) aus dem Median-
        Abstand aufeinanderfolgender Sensor-Anstiegsflanken.

        Flanken = UNBEKANNT/`sensor_heuristik`-OEFFNEN der Zone (dieselben, die
        `_scan_zone` konvertiert). Bei AquaBloom-Topfpflanzen (indoor, kein
        Regen) ist praktisch jede Flanke ein Pump-Puls -> der Median-Abstand
        ist ein robuster Takt-Schaetzer, auch wenn einzelne Pulse fehlen (der
        Doppel-Abstand ist der Ausreisser, den der Median wegdrueckt).

        Fallback auf `konfig_intervall_h`, wenn zu wenige Flanken (< 3) oder
        der Median ausserhalb [MIN_INFER, MAX_INFER] liegt (zu wenig Signal /
        implausibel). Der Median bleibt bewusst UNGERUNDET -- die Zyklus-
        Fenster tragen ihre eigene +-25%-Toleranz.
        """
        from statistics import median

        # Grosszuegiger Lookback: genug Zyklen fuer einen stabilen Median, auch
        # bei laengerem Takt. An der Config-Erwartung orientiert, mit Deckel.
        lookback_h = min(konfig_intervall_h, MAX_INFER_INTERVALL_H) * (
            MAX_ZYKLEN + 2
        )
        try:
            events = await self._speicher.hole_ventil_ereignisse(
                zone.zone_id,
                von=jetzt - timedelta(hours=lookback_h),
                bis=jetzt,
            )
        except Exception:
            logger.exception(
                "aquabloom.intervall_inferenz_fehler", zone_id=zone.zone_id,
            )
            return konfig_intervall_h

        # T-0392: AUCH bereits konvertierte AQUABLOOM-Flanken zaehlen.
        # Vorher sampelte die Inferenz nur `UNBEKANNT` -- konvertiert werden
        # aber genau die Flanken, die in die Config-Vielfachen-Fenster fallen.
        # Nach einer Takt-Verstellung (z.B. 12h -> 6h) blieben nur die
        # GESTRANDETEN Flanken in der Stichprobe, deren Median die falsche
        # Config dauerhaft bestaetigte (Survivorship-Bias): jeder zweite Puls
        # blieb UNBEKANNT, die Wasser-Bilanz halbierte sich.
        # `_konvertiere` aendert nur `ausloser` (+ Liter), nicht `ventil_id` --
        # die Flanken-Identitaet bleibt also `sensor_heuristik`.
        flanken = sorted(
            e.zeitstempel for e in events
            if e.ausloser in (Ausloser.UNBEKANNT, Ausloser.AQUABLOOM)
            and e.aktion == VentilAktion.OEFFNEN
            and e.ventil_id == "sensor_heuristik"
        )
        if len(flanken) < MIN_FLANKEN_FUER_INFERENZ:
            return konfig_intervall_h

        abstaende_h = [
            (flanken[i + 1] - flanken[i]).total_seconds() / 3600.0
            for i in range(len(flanken) - 1)
        ]
        median_h = float(median(abstaende_h))
        if not (MIN_INFER_INTERVALL_H <= median_h <= MAX_INFER_INTERVALL_H):
            return konfig_intervall_h

        if abs(median_h - konfig_intervall_h) >= 1.0:
            logger.info(
                "aquabloom.intervall_inferiert",
                zone_id=zone.zone_id,
                intervall_inferiert_h=round(median_h, 1),
                intervall_konfig_h=konfig_intervall_h,
                flanken=len(flanken),
            )
        return median_h

    async def _pruefe_cadence_drift(
        self, zone: ZonenKonfig, jetzt: datetime, intervall_h: float,
    ) -> None:
        """T-0294a: macht einen Config-vs-Realitaet-Cadence-Mismatch laut.

        Wenn mehrere UNBEKANNT-Morgen-OEFFNEN-Events alt genug waren, um in
        einem Zyklus-Fenster gefangen zu werden, es aber nicht wurden,
        laeuft die reale Pumpe wahrscheinlich oefter als
        `aquabloom_pumpen_intervall_stunden`. Die (bewusst konservativen,
        T-0188) Zyklus-Fenster verfehlen sie dann STILL -- genau der
        T-0294-Bug (Config 48h, Pumpe real 1x/Tag). Statt die Logik zu
        lockern (das wuerde die gewollte "implausibel getimter Sprung ->
        unbekannt"-Safety brechen), surfacen wir den Verdacht: der User
        prueft die Config oder markiert die Events im UI als AquaBloom
        (T-0294b). Throttle 1x/24h/Zone.
        """
        lookback = timedelta(hours=intervall_h * MAX_ZYKLEN)
        # Nur Events, die alt genug sind, um in einem Fenster gefangen zu
        # werden -- die juengste halbe Intervall-Spanne kann legitim noch
        # pending sein.
        catchable_bis = jetzt - timedelta(hours=intervall_h * 0.5)
        if catchable_bis <= jetzt - lookback:
            return
        try:
            events = await self._speicher.hole_ventil_ereignisse(
                zone.zone_id, von=jetzt - lookback, bis=catchable_bis,
            )
        except Exception:
            logger.exception(
                "aquabloom.cadence_drift_query_fehler", zone_id=zone.zone_id,
            )
            return
        gestrandet = [
            e for e in events
            if e.ausloser == Ausloser.UNBEKANNT
            and e.aktion == VentilAktion.OEFFNEN
            and e.ventil_id == "sensor_heuristik"
        ]
        if len(gestrandet) < 2:
            return
        zuletzt = self._drift_warnung_zuletzt.get(zone.zone_id)
        if zuletzt is not None and (jetzt - zuletzt) < timedelta(hours=24):
            return
        self._drift_warnung_zuletzt[zone.zone_id] = jetzt
        logger.warning(
            "aquabloom.cadence_drift_verdacht",
            zone_id=zone.zone_id,
            gestrandete_events=len(gestrandet),
            intervall_konfig_h=intervall_h,
            hinweis=(
                "Mehrere UNBEKANNT-Morgen-Spruenge nicht zu AquaBloom "
                "konvertiert -- reale Pump-Cadence evtl. kuerzer als Konfig. "
                "aquabloom_pumpen_intervall_stunden pruefen (T-0294) oder "
                "Events im UI als AquaBloom markieren (T-0294b)."
            ),
        )

    def _finde_paar_schliessen(self, events, oeffnen):
        """Sucht das SCHLIESSEN-Pendant zum OEFFNEN (Sensor-Heuristik-Paar)."""
        toleranz = timedelta(minutes=PAAR_TOLERANZ_MIN)
        for e in events:
            if (
                e.aktion == VentilAktion.SCHLIESSEN
                and e.ausloser == Ausloser.UNBEKANNT
                and e.ventil_id == "sensor_heuristik"
                and oeffnen.zeitstempel < e.zeitstempel
                <= oeffnen.zeitstempel + toleranz
            ):
                return e
        return None

    async def _konvertiere_zu_aquabloom(
        self, zone: ZonenKonfig, oeffnen, schliessen,
    ) -> int:
        """Patch eines UNBEKANNT-Event-Paars auf AQUABLOOM + Tropfer-Liter."""
        dauer_s = int(zone.aquabloom_pumpen_dauer_sekunden)
        liter_pro_puls = (
            dauer_s / 3600.0
            * float(zone.aquabloom_tropfer_anzahl)
            * float(zone.aquabloom_tropfer_liter_pro_stunde)
        )

        if oeffnen.id is None:
            logger.warning(
                "aquabloom.kein_event_id", zone_id=zone.zone_id,
            )
            return 0

        await self._speicher.aktualisiere_ventil_ereignis(
            oeffnen.id, ausloser=Ausloser.AQUABLOOM,
        )
        if schliessen and schliessen.id is not None:
            await self._speicher.aktualisiere_ventil_ereignis(
                schliessen.id,
                ausloser=Ausloser.AQUABLOOM,
                liter=round(liter_pro_puls, 4),
                dauer_sekunden=dauer_s,
                # F8: Zeitstempel konsistent zur neuen Dauer mitziehen. Sonst
                # bricht der T-0296-Start-Anker (schliessen.ts - dauer ≈
                # oeffnen.ts): die Konversion setzt dauer_s (z. B. 600), laesst
                # aber den Heuristik-Zeitstempel stehen -> Anker driftet > 300s
                # -> finde_ventil_paar findet das Paar nicht mehr.
                zeitstempel=oeffnen.zeitstempel + timedelta(seconds=dauer_s),
            )

        logger.info(
            "aquabloom.heuristik_konvertiert",
            zone_id=zone.zone_id,
            zeit=oeffnen.zeitstempel.isoformat(),
            liter=round(liter_pro_puls, 4),
            paar=schliessen is not None,
        )
        return 1


def _ist_konfiguriert(zone: ZonenKonfig) -> bool:
    """True wenn die Zone alle Pflicht-Aquabloom-Felder gesetzt hat."""
    return (
        zone.aquabloom_pumpen_dauer_sekunden is not None
        and zone.aquabloom_pumpen_dauer_sekunden > 0
        and zone.aquabloom_pumpen_intervall_stunden is not None
        and zone.aquabloom_pumpen_intervall_stunden > 0
        and zone.aquabloom_tropfer_anzahl is not None
        and zone.aquabloom_tropfer_anzahl > 0
        and zone.aquabloom_tropfer_liter_pro_stunde is not None
        and zone.aquabloom_tropfer_liter_pro_stunde > 0
    )


def _in_saison(zone: ZonenKonfig, jetzt: datetime) -> bool:
    """True wenn `jetzt` im MM-DD-Range [aktiv_ab, aktiv_bis] liegt.

    Beide Felder None -> ganzjaehrig aktiv. Nur eines gesetzt ->
    Teil-Saison (z. B. nur `ab`).
    """
    if zone.aquabloom_aktiv_ab is None and zone.aquabloom_aktiv_bis is None:
        return True
    jetzt_key = jetzt.month * 100 + jetzt.day
    von_key = _mmdd_zu_key(zone.aquabloom_aktiv_ab) if zone.aquabloom_aktiv_ab else 0
    bis_key = _mmdd_zu_key(zone.aquabloom_aktiv_bis) if zone.aquabloom_aktiv_bis else 1231
    if von_key <= bis_key:
        return von_key <= jetzt_key <= bis_key
    # Wraparound (z.B. 10-15..03-15)
    return jetzt_key >= von_key or jetzt_key <= bis_key


def _mmdd_zu_key(mm_dd: str) -> int:
    """'05-01' -> 501."""
    teile = mm_dd.split("-")
    return int(teile[0]) * 100 + int(teile[1])
