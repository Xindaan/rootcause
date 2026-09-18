"""T-0055-B1 — Sensor-basierte Heuristik fuer verpasste Bewaesserungs-Events.

**Warum**: Der DHS-Backfill (B3) ist der Haupt-Automatik-Pfad — aber inoffiziell.
Wenn Gardena den Endpoint aendert oder der Customer-Login abgelaufen ist,
verlieren wir die Event-Historie wieder. Die Sensor-Heuristik ist der
Zero-Dependency-Fallback: aus dem Feuchte-Verlauf in `sensor_messung`
erkennen wir Spruenge und schreiben sie als Kandidaten in `ventil_ereignis`.

**Kein Ersatz fuer DHS**: die Heuristik kann Start/Stop-Zeit nur grob
schaetzen, kennt keine Dauer und kann Regen nicht sicher unterscheiden.
Jeder erkannte Event wird deshalb mit `ausloser=UNBEKANNT` geschrieben —
der User klassifiziert manuell via Ops-Tab (B2). Ausnahme seit T-0469:
ist eine Cross-Spray-Quelle der Zone konfiguriert und lief sie im Fenster,
steht die Klassifikation schon fest — dann wird direkt `FREMDWASSER`
(+ `quell_zone`) geschrieben statt den Sprung zu unterdruecken.

**Dedup mit anderen Quellen**: Live-Events, DHS-Backfill (`gardena_web`)
und manuelle API-Eintraege haben Vorrang. Die Heuristik schreibt nur,
wenn im +/-30min-Fenster kein anderer Event existiert.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from bewaesserung.kanal_zustand import kanal_vorgang_laeuft
from bewaesserung.modelle import (
    Ausloser,
    SensorMessung,
    VentilAktion,
    VentilEreignis,
)
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

# Erkennungs-Parameter.
# Sprung-Schwelle: 5 % liegt bewusst zwischen Tagesrauschen (~1-2 %) und
# Einschlemm-Signatur (~20 %+). Niedriger waere False-Positives durch
# Sensor-Kalibrierung; hoeher wuerde kleine Mikrodrip-Gaben verpassen.
MIN_DELTA_PROZENT = 5.0
MAX_FENSTER_MIN = 90             # Max Abstand zwischen zwei Messungen
# DHS-Pull nutzt 60 s Fenster (Ground-Truth sekundengenau). Heuristik ist
# zeitlich unschaerfer — Sensor-Rhythmus ~15 min → 30 min Dedup deckt
# mindestens einen Sensor-Zyklus in beide Richtungen ab.
DEDUP_FENSTER_MIN = 30
# Versickerungs-Karenz: nach dem Ende eines Ground-Truth-Events (Live-UUID,
# manuell, gardena_web) kann der Sensor mit mehreren Stunden Verzoegerung
# einen Feuchte-Sprung zeigen — besonders bei Microdrip, wo Wasser erst
# langsam an den Sensor sickert. Beobachtet am 21.04.2026: 20-min-
# Bewaesserung 09:41-10:01 → Sensor-Sprung 60→65 erst um 11:45. Ohne
# Karenz loest die Heuristik einen Phantom-Event aus. 3 h decken den
# Microdrip-Worst-Case ab, ohne echte Folge-Events zu verpassen (die
# waeren in der Praxis nicht innerhalb derselben 3 h).
# T-0071: Sandboden mit Sprinkler (Waldblumenhain) zeigt Sensor-Antwort
# erst nach 4-6 h — reproduziert 23.04.2026: Close 12:04 + Sensor-Sprung
# 16:54 wurde als neuer Event geloggt. Zone-spezifischer Override kommt
# aus `ZonenKonfig.versickerungs_karenz_stunden` ueber die Constructor-
# Option `karenz_stunden_pro_zone` rein.
VERSICKERUNGS_KARENZ_DEFAULT = timedelta(hours=3)
# T-0531: Wie lange nach dem ENDE eines fremden Laufs darf ein Sprung noch als
# dessen Cross-Spray gelten? Die Versickerungs-Karenz oben taugt dafuer nicht:
# sie modelliert Wasser, das im eigenen Boden langsam zum Sensor sickert. Beim
# Cross-Spray trifft der Strahl den Sensor direkt und hoert mit dem Ventil auf.
# Ein Lauf, der eine Stunde vorher endete, ist deshalb keine belegte Ursache
# mehr, sondern ein Verdacht -- und ein Verdacht gehoert nicht als Fakt ins
# Feld `quell_zone`. Realfall 08.08.2026: Andres Handguss an der Hecke wurde
# einem Magerwiesen-Lauf zugeschrieben, der 58 min vorher geendet hatte.
CROSS_SPRAY_NACHLAUF = timedelta(minutes=30)
REGEN_AUSSCHLUSS_FAKTOR = 0.3    # Regen > 30 % erwartete Zufuhr -> nicht werten

# T-0126: Erweiterter Regen-Check fuer Sand-Lag. Sensor reagiert auf Regen
# erst 4-6 h spaeter (Versickerungs-Zeit). Wenn im 6h-Fenster vor dem
# Sprung nennenswerter Regen (>= REGEN_PHANTOM_SCHWELLE_MM) gefallen ist,
# wird der Sprung als Regen-Folge interpretiert -> kein Phantom-Event.
# Realfall 05.05.: Regen 04:00-11:00 mit 0.1-3.1 mm/h. Sprung 04:34
# Yogaraum war Regen-Folge, der alte 30-min-Filter sah nur 0.1 mm im
# unmittelbaren Sprung-Fenster und schrieb Phantom-Paar.
REGEN_PHANTOM_LOOKBACK_H = 6
REGEN_PHANTOM_SCHWELLE_MM = 0.5

# Eichung Feuchte-Sprung -> mm Wasserzugabe (grob, 1 %-Punkt ≈ 1 mm)
EICHUNG_MM_PRO_PROZENT = 1.0

# Trigger-Logik
INTERVALL_MINUTEN_DEFAULT = 30   # Cadence des Jobs
SICHERHEITS_OVERLAP = timedelta(hours=1)    # gegen verspaetete Inserts
ERST_LAUF_RUECKBLICK = timedelta(days=7)    # seedt kein Anker vorhanden


class SensorBackfillJob:
    """Periodischer Sensor-Heuristik-Job analog WetterArchivJob."""

    def __init__(
        self,
        speicher: Speicher,
        zone_ids: list[str],
        intervall_minuten: int = INTERVALL_MINUTEN_DEFAULT,
        karenz_stunden_pro_zone: dict[str, int] | None = None,
        zone_zu_kanal: dict[str, int] | None = None,
        zone_zu_geraet: dict[str, str] | None = None,
        indoor_zone_ids: set[str] | None = None,
        standort_pro_zone: dict[str, str] | None = None,
        # Fallback-Standort fuer Zonen ohne Mapping. Kommt aus der Konfig
        # (erster konfigurierter Standort). Frueher stand hier hartkodiert der
        # reale Wohnort -- fuer jede andere Installation lief der Regen-Check
        # damit gegen einen nicht existierenden Standort und lieferte still
        # 0 mm, d.h. der Phantom-Schutz war wirkungslos.
        standort_default: str | None = None,
        min_delta_pp_pro_zone: dict[str, float] | None = None,
        rollup_pro_zone: dict[str, tuple[int, float]] | None = None,
        ausschluss_fenster_pro_zone: dict[
            str, list[tuple[datetime, datetime, str | None]]
        ] | None = None,
        cross_spray_quellen: dict[str, list[str]] | None = None,
    ) -> None:
        """SensorBackfillJob.

        `karenz_stunden_pro_zone` ueberschreibt die Versickerungs-Karenz
        (in ganzen Stunden) pro Zone. Fehlt ein Mapping fuer eine Zone,
        gilt `VERSICKERUNGS_KARENZ_DEFAULT` (3 h). Siehe T-0071 /
        MEMORY: fehlerpattern_config_whitelist.md.

        `zone_zu_kanal` (T-0123) ist Pflicht, wenn Phantom-Erzeugung
        waehrend Live-Manuell-Laeufen vermieden werden soll: bei aktivem
        `live_lauf_state` auf dem Kanal der Zone wird der Heuristik-
        Sprung NICHT als externes Event geschrieben, weil er offensichtlich
        vom Backend-Lauf verursacht wurde. Ohne Mapping bleibt das alte
        Verhalten (= Phantom-Risiko nur bei Race mit DB-Schreibung).

        `indoor_zone_ids` (T-0175) markiert Zonen, fuer die der Regen-
        Check uebersprungen wird (Wohnung-Pflanzen koennen keinen Regen
        bekommen). Sonst wuerde die DB nach Wetter fuer den FYTA-
        Standort durchsucht und ggf. faelschlich Phantom-Events
        unterdrueckt — bei Indoor ist das definitiv nicht relevant.

        `min_delta_pp_pro_zone` (T-0187) ueberschreibt die globale Beat-
        zu-Beat-Schwelle MIN_DELTA_PROZENT (5.0) pro Zone. FYTA-Indoor-
        Toepfe mit AquaBloom-Tropfer brauchen niedrigere Schwellen
        (~3.0), weil Wasser lokal langsam sickert.

        `rollup_pro_zone` (T-0187) aktiviert zusaetzlich einen Multi-
        Beat-Differenz-Check: tuple `(fenster_min, schwelle_pp)`. Wenn
        die Feuchte zwischen einer alten und einer neuen Messung im
        Fenster mind. `schwelle_pp` gestiegen ist, wird ein Event-
        Kandidat angemeldet. Erkennt langsame Sicker-Cadence (Realfall
        15.05.: mandevilla 41->50 ueber 2.5 h, kein einzelner Beat ueber
        Beat-Schwelle).
        """
        self._speicher = speicher
        self._zone_ids = list(zone_ids)
        self._intervall = timedelta(minutes=intervall_minuten)
        self._letzte_aktualisierung: datetime | None = None
        self._karenz_pro_zone: dict[str, timedelta] = {
            zid: timedelta(hours=int(h))
            for zid, h in (karenz_stunden_pro_zone or {}).items()
        }
        self._zone_zu_kanal: dict[str, int] = dict(zone_zu_kanal or {})
        self._zone_zu_geraet: dict[str, str] = dict(zone_zu_geraet or {})
        self._indoor_zone_ids: set[str] = set(indoor_zone_ids or set())
        # F13: zone_id -> wetter_standort (aus konfig.standorte). Fehlt ein
        # Mapping, faellt der Regen-Check auf `standort_default` zurueck (erster
        # konfigurierter Standort). Verhindert, dass Outdoor-Balkon-Zonen mit
        # wetter_standort=berlin gegen den falschen Standort geprueft werden.
        self._standort_pro_zone: dict[str, str] = dict(standort_pro_zone or {})
        self._standort_default: str | None = standort_default
        self._min_delta_pp_pro_zone: dict[str, float] = dict(
            min_delta_pp_pro_zone or {}
        )
        # T-0312: zone_id -> Quell-Zonen, deren Sprinkler-Lauf den Sensor
        # dieser Zone per Cross-Spray treffen kann (z.B. hecke -> [magerwiese]).
        self._cross_spray_quellen: dict[str, list[str]] = dict(
            cross_spray_quellen or {}
        )
        self._rollup_pro_zone: dict[str, tuple[int, float]] = dict(
            rollup_pro_zone or {}
        )
        # T-0211a: Ausschluss-Fenster pro Zone (= ml_ausschluss_fenster aus
        # default.yaml). Waehrend dieser Phase wird die Heuristik komplett
        # ausgesetzt — typischerweise Bodenart-Reset-Phasen, in denen
        # Sensor-Modell-Wechsel 10-20 pp Phantom-Spruenge erzeugt.
        self._ausschluss_fenster_pro_zone: dict[
            str, list[tuple[datetime, datetime, str | None]]
        ] = dict(ausschluss_fenster_pro_zone or {})
        # T-0228 Stufe 2: dynamische Wartungs-Fenster aus
        # `wartungs_fenster`-Tabelle. Wird einmal pro `aktualisiere_wenn_
        # faellig`-Tick geladen (sync-Helper `_ist_in_ausschluss` darf
        # nicht jeden Zonen-Check eine DB-Abfrage feuern). Format wie
        # `_ausschluss_fenster_pro_zone`: dict[zone_id, list[(von, bis)]].
        # Sicherheits-Cap: offene Fenster (bis_am IS NULL) werden als
        # "bis jetzt + 1 Tag" behandelt, damit ein vergessenes Beenden
        # nicht ewig pausiert.
        self._wartungs_fenster_pro_zone: dict[
            str, list[tuple[datetime, datetime, str | None]]
        ] = {}

    async def _lade_wartungs_fenster(self, jetzt: datetime) -> None:
        """T-0228 Stufe 2: laedt offene Wartungs-Fenster aus der DB in
        den In-Memory-Cache. Wird 1x pro Tick aufgerufen."""
        try:
            offene = await self._speicher.hole_wartungs_fenster(nur_offen=True)
        except Exception:
            logger.exception("sensor_backfill.wartungs_fenster_load_fehler")
            return
        cap = jetzt + timedelta(days=1)
        cache: dict[str, list[tuple[datetime, datetime, str | None]]] = {}
        for w in offene:
            zid = w["zone_id"]
            try:
                von = datetime.fromisoformat(w["von_am"])
            except (TypeError, ValueError):
                continue
            cache.setdefault(zid, []).append((von, cap, None))
        self._wartungs_fenster_pro_zone = cache

    async def aktualisiere_wenn_faellig(self, jetzt: datetime | None = None) -> int:
        """Laeuft max. einmal pro `intervall_minuten`. Gibt Anzahl neuer Events."""
        jetzt = jetzt or datetime.now()
        if (
            self._letzte_aktualisierung
            and jetzt - self._letzte_aktualisierung < self._intervall
        ):
            return 0
        # T-0228 Stufe 2: Wartungs-Fenster vor der Zone-Schleife laden,
        # damit `_ist_in_ausschluss` (sync) gegen den Cache prueft.
        await self._lade_wartungs_fenster(jetzt)
        total = 0
        for zone_id in self._zone_ids:
            try:
                total += await self._pruefe_zone(zone_id, jetzt)
            except Exception:
                logger.exception("sensor_backfill.zone_fehler", zone_id=zone_id)
        self._letzte_aktualisierung = jetzt
        if total > 0:
            logger.info("sensor_backfill.events_geschrieben", gesamt=total)
        return total

    def _ist_in_ausschluss(
        self, zone_id: str, jetzt: datetime, geraet_id: str | None = None,
    ) -> bool:
        """T-0211a + T-0228 Stufe 2: True wenn die Zone (bzw. ein Sensor)
        gerade in einem Ausschluss-Fenster ist. Zwei Quellen:
        - statische `ml_ausschluss_fenster` aus YAML (T-0211a)
        - dynamische Wartungs-Fenster aus DB (T-0228 Stufe 2)
        Heuristik soll dann nicht laufen — Sensor-Modell-Wechsel in
        Bodenart-Reset-Phasen produziert Phantom-Events.

        T-0386: geraet_id-aware. `geraet_id=None` (Default, Zonen-Ebene) ->
        nur ZONE-WEITE Fenster (Fenster-geraet_id None) treffen; ein
        geraet-scoped Fenster pausiert damit NICHT die ganze Zone. Mit
        konkreter `geraet_id` treffen zone-weite ODER auf genau diesen Sensor
        gescopte Fenster. (Ein Fenster mit geraet_id gilt genau dann, wenn
        `f_geraet is None or f_geraet == geraet_id` -- deckt beide Faelle ab.)
        """
        for quelle in (
            self._ausschluss_fenster_pro_zone.get(zone_id),
            self._wartungs_fenster_pro_zone.get(zone_id),
        ):
            if not quelle:
                continue
            for von, bis, f_geraet in quelle:
                if von <= jetzt <= bis and (
                    f_geraet is None or f_geraet == geraet_id
                ):
                    return True
        return False

    async def _pruefe_zone(self, zone_id: str, jetzt: datetime) -> int:
        """Untersucht Sensor-Historie einer Zone, schreibt Heuristik-Events."""
        # T-0211a: waehrend Bodenart-Reset / Sensor-Versetzung pausiert die
        # Heuristik. Sensor-Modell-Wechsel-Spruenge sind kein echter
        # Bewaesserungs-Hinweis und wuerden Phantom-UNBEKANNT-Events fluten.
        if self._ist_in_ausschluss(zone_id, jetzt):
            logger.debug(
                "sensor_backfill.skip_ausschluss_fenster",
                zone_id=zone_id, jetzt=jetzt.isoformat(timespec="minutes"),
            )
            return 0
        anker = await self._letzte_heuristik_zeit(zone_id)
        if anker is None:
            anker = jetzt - ERST_LAUF_RUECKBLICK
        von = anker - SICHERHEITS_OVERLAP
        bis = jetzt - timedelta(minutes=5)
        if bis <= von:
            return 0

        messungen = await self._speicher.hole_messungen(zone_id, von=von, bis=bis)
        # hole_messungen gibt DESC zurueck; wir wollen chronologisch
        messungen = sorted(
            [m for m in messungen if m.boden_feuchte is not None],
            key=lambda m: m.zeitstempel,
        )
        if len(messungen) < 2:
            return 0

        # T-0265-Fix (2026-05-26): pro `geraet_id` getrennt iterieren,
        # NICHT zonen-weit ueber gemischte Sensoren. Bei Multi-Sensor-
        # Zonen (z.B. waldblumenhain mit Gardena 35 % + FYTA-A 57 % +
        # FYTA-D 45 %) produziert die zonen-weite Iteration Pseudo-
        # Spruenge von +12 bis +22 pp, sobald die Mess-Sequenz von
        # einem Sensor zum naechsten wechselt. Realfall 24.-26.05.
        # waldblumen: 54 Phantom-UNBEKANNT-Events aus Sensor-Mix-
        # Wechseln, jeder einzelne Sensor stabil. Analog zu T-0224
        # (FYTA-Dedup-Bug 22.05.).
        nach_sensor: dict[str, list[SensorMessung]] = {}
        for m in messungen:
            nach_sensor.setdefault(m.geraet_id or "_unbekannt", []).append(m)

        geschrieben = 0
        for sensor_id, sensor_messungen in nach_sensor.items():
            # T-0386: einen geraet-scoped ausgeschlossenen Sensor ueberspringen
            # (der Zonen-Gate oben faengt nur ZONE-WEITE Fenster). So laeuft die
            # Heuristik auf dem gesunden Nachbarsensor weiter, waehrend der
            # settlende/versetzte Sensor keine Phantom-Events erzeugt.
            if self._ist_in_ausschluss(zone_id, jetzt, sensor_id):
                continue
            if len(sensor_messungen) < 2:
                continue
            for i in range(1, len(sensor_messungen)):
                alt = sensor_messungen[i - 1]
                neu = sensor_messungen[i]
                if await self._schreibe_wenn_kandidat(zone_id, alt, neu, jetzt):
                    geschrieben += 1

        # T-0187: Roll-Up-Pass fuer Zonen mit langsamer Sicker-Cadence.
        # T-0265: pro Sensor (= jede Multi-Sensor-Zone) eigenen
        # Roll-Up-Scan, sonst kollidiert die Mischsequenz auch hier.
        if zone_id in self._rollup_pro_zone:
            for sensor_messungen in nach_sensor.values():
                if len(sensor_messungen) >= 2:
                    geschrieben += await self._scan_rollup(
                        zone_id, sensor_messungen, jetzt,
                    )
        return geschrieben

    async def _schreibe_wenn_kandidat(
        self, zone_id: str, alt: SensorMessung, neu: SensorMessung,
        jetzt: datetime,
    ) -> bool:
        delta = float(neu.boden_feuchte) - float(alt.boden_feuchte)
        # T-0187: pro-Zone-Schwelle vor globalem Default.
        schwelle = self._min_delta_pp_pro_zone.get(zone_id, MIN_DELTA_PROZENT)
        if delta < schwelle:
            return False
        dt_min = (neu.zeitstempel - alt.zeitstempel).total_seconds() / 60.0
        if dt_min <= 0 or dt_min > MAX_FENSTER_MIN:
            return False

        return await self._versuche_schreiben(zone_id, alt, neu, delta, jetzt)

    async def _scan_rollup(
        self, zone_id: str, messungen: list[SensorMessung], jetzt: datetime,
    ) -> int:
        """T-0187: Multi-Beat-Roll-Up-Heuristik fuer langsame Sicker-Cadence.

        Iteriert chronologisch. Pro Messung `neu` sucht die niedrigste
        Feuchte im `fenster_min`-Lookback. Wenn delta `neu - min_im_fenster`
        die Roll-Up-Schwelle ueberschreitet, wird der Sprung-Start (Tal)
        bis `neu` (Berg) als Event-Paar gemeldet. Dedup ueber Event-Fenster
        30 min verhindert Kollision mit der Beat-Logik (T-0114-Pattern).

        Nach einer erfolgreichen Erkennung wird das Fenster uebersprungen,
        damit derselbe Sprung nicht mehrfach gemeldet wird.
        """
        fenster_min, schwelle_pp = self._rollup_pro_zone[zone_id]
        fenster = timedelta(minutes=fenster_min)
        geschrieben = 0
        i = 1
        while i < len(messungen):
            neu = messungen[i]
            min_messung: SensorMessung | None = None
            for j in range(i - 1, -1, -1):
                if (neu.zeitstempel - messungen[j].zeitstempel) > fenster:
                    break
                if (
                    min_messung is None
                    or messungen[j].boden_feuchte < min_messung.boden_feuchte
                ):
                    min_messung = messungen[j]
            if min_messung is None:
                i += 1
                continue
            delta = float(neu.boden_feuchte) - float(min_messung.boden_feuchte)
            if delta < schwelle_pp:
                i += 1
                continue
            if await self._versuche_schreiben(
                zone_id, min_messung, neu, delta, jetzt,
            ):
                geschrieben += 1
                # Ueberspringen: alle Messungen im Roll-Up-Fenster nach
                # `neu` gehoeren noch zum selben Sprung-Plateau.
                ueberhol_zeit = neu.zeitstempel + fenster
                while (
                    i < len(messungen)
                    and messungen[i].zeitstempel <= ueberhol_zeit
                ):
                    i += 1
            else:
                i += 1
        return geschrieben

    async def _versuche_schreiben(
        self,
        zone_id: str,
        alt: SensorMessung,
        neu: SensorMessung,
        delta: float,
        jetzt: datetime,
    ) -> bool:
        """T-0187 (extrahiert aus _schreibe_wenn_kandidat): Pre-Checks +
        Event-Paar-Insert. Identische Semantik fuer Beat- und Roll-Up-
        Pfad. `delta` wird als Schaetzgrundlage fuer die Pseudo-Dauer
        durchgereicht.
        """
        # T-0175: Indoor-Zonen koennen keinen Regen abbekommen — Regen-
        # Check komplett skippen. Sonst wuerde die DB nach Wetter
        # durchsucht und ggf. ein Sprung-Event als "Regen" gewertet,
        # obwohl die Pflanze in der Wohnung steht.
        ist_indoor = zone_id in self._indoor_zone_ids

        if not ist_indoor:
            # Regen-Ausschluss: wenn Regen im Fenster > 30 % der erwarteten
            # Zufuhr (delta in mm), interpretieren wir den Sprung als Regen,
            # nicht Bewaesserung.
            regen_mm = await self._regen_im_fenster(
                zone_id, alt.zeitstempel, neu.zeitstempel,
            )
            if regen_mm > REGEN_AUSSCHLUSS_FAKTOR * delta * EICHUNG_MM_PRO_PROZENT:
                return False

            # T-0126: Erweiterter Regen-Check mit Sand-Lag (6h-Lookback).
            # Sensor reagiert mit 4-6 h Verzoegerung auf Regen; das enge
            # Sprung-Fenster oben sieht das nicht. Wenn im 6h-Lookback
            # nennenswerter Regen gefallen ist, ist der Sprung wahrscheinlich
            # Regen-Folge -> kein Phantom.
            regen_mm_lookback = await self._regen_im_fenster(
                zone_id,
                alt.zeitstempel - timedelta(hours=REGEN_PHANTOM_LOOKBACK_H),
                neu.zeitstempel,
            )
            if regen_mm_lookback >= REGEN_PHANTOM_SCHWELLE_MM:
                logger.info(
                    "sensor_backfill.skip_regen_lookback",
                    zone_id=zone_id,
                    regen_mm=round(regen_mm_lookback, 2),
                    delta=round(delta, 1),
                )
                return False

        # T-0123: Live-Lauf-Check VOR DB-Lookup. Wenn auf dem Kanal der
        # Zone gerade eine Backend-Bewaesserung laeuft, ist der Sprung
        # offensichtlich von ihr verursacht — kein Phantom-Event schreiben.
        # OEFFNEN-Events der Live-Bewaesserung kommen via WS-Pipeline mit
        # ggf. mehreren Sekunden Delay, daher reicht `_event_im_fenster`
        # alleine nicht (Realfall 02.05.: id 480/481 entstand waehrend
        # laufender Waldblumen-Bewaesserung).
        mitte = alt.zeitstempel + (neu.zeitstempel - alt.zeitstempel) / 2
        # T-0283 (2026-06-02): OEFFNEN/SCHLIESSEN-Spanne VOR dem
        # Dedup berechnen. Das Event-Paar wird auf [mitte - dauer/2,
        # mitte + dauer/2] gelegt; das Dedup-Fenster muss diese Spanne
        # abdecken, sonst lagen bei langen Events (dauer/2 > DEDUP_FENSTER_MIN)
        # OEFFNEN UND SCHLIESSEN ausserhalb des +/-30min-Fensters um `mitte`
        # -> kein Treffer -> das Event wurde in JEDEM Backfill-Lauf neu
        # erkannt + dupliziert (Realfall pilea 31.05.: delta=70 -> 70min-
        # Event, 50 identische Phantom-Eintraege). Kurze Events (waldblumen
        # 24min) blieben im Fenster und wurden korrekt dedupt.
        dauer_s = max(60, int(delta * EICHUNG_MM_PRO_PROZENT * 60))
        start = mitte - timedelta(seconds=dauer_s / 2)
        stop = mitte + timedelta(seconds=dauer_s / 2)

        if await self._aktive_bewaesserung_auf_kanal(zone_id, jetzt):
            logger.debug(
                "sensor_backfill.skip_aktive_bewaess",
                zone_id=zone_id,
                mitte=mitte.isoformat(timespec="minutes"),
                delta=round(delta, 1),
            )
            return False

        # Dedup: Live / Manuell / DHS / Heuristik haben alle Vorrang, wenn
        # schon ein Event in der Event-Spanne (+/- DEDUP_FENSTER_MIN Marge)
        # liegt. Fenster = volle Event-Spanne, damit auch lange Events ihr
        # eigenes Paar aus dem Vorlauf wiederfinden (T-0283).
        dedup_marge = timedelta(minutes=DEDUP_FENSTER_MIN)
        if await self._event_im_fenster(
            zone_id, start - dedup_marge, stop + dedup_marge,
        ):
            return False

        # Versickerungs-Karenz: Ein Ground-Truth-Event (Live/Manuell/DHS),
        # dessen SCHLIESSEN bis zu 3 h vor der aktuellen Messung lag, kann
        # den jetzigen Feuchte-Sprung verursacht haben (Microdrip-Versickerung).
        # Sensor-Heuristik ist kein Ground-Truth und zaehlt nicht.
        if await self._ground_truth_noch_am_wirken(zone_id, neu.zeitstempel):
            return False

        # T-0312: Cross-Spray-Erkennung. An dieser Stelle ist KEIN eigener
        # Kanal-Lauf bekannt (Live-Check + Dedup + Ground-Truth-Karenz oben
        # passiert). Wenn jetzt eine konfigurierte Quell-Zone (z.B. der
        # Strapazierrasen-Viereckregner ueber den Magerwiesenkanal) im
        # Fenster+Karenz lief, ist der Feuchte-Sprung wahrscheinlich deren
        # Strahl auf den Sensor -- KEIN eigener Microdrip-Lauf.
        #
        # T-0469 (Verhaltensaenderung): frueher wurde das Ereignis hier
        # komplett unterdrueckt (`return False`). Damit war ein realer, vom
        # Sensor sauber gemessener Feuchtesprung in `ventil_ereignis` gar
        # nicht mehr auffindbar -- dieselbe Falschaussage ("ist nie
        # passiert"), gegen die T-0453 die Klasse FREMDWASSER eingefuehrt
        # hat, nur radikaler. Jetzt wird das Paar mit `ausloser=FREMDWASSER`
        # + `quell_zone` geschrieben. FREMDWASSER steht in
        # KEINE_WASSER_AUSLOESER: es zaehlt weiterhin NICHT als Kanal-Wasser
        # und verfaelscht Wirkungsrate/ML/Schwellen nicht -- der Sprung ist
        # aber als Abfrage rekonstruierbar statt nur forensisch.
        quell_zone = await self._cross_spray_quelle(zone_id, start, stop)
        ausloser = (
            Ausloser.FREMDWASSER if quell_zone is not None
            else Ausloser.UNBEKANNT
        )

        # F22: Heuristik-Paar atomar schreiben. Sonst hinterlaesst ein Crash
        # zwischen den beiden Inserts ein lone OEFFNEN (sensor_heuristik wird
        # vom Orphan-Job geskippt -> bleibt unsichtbar im Paar-Modell haengen).
        # Der DHS-Ersetzungspfad nutzt `transaktion()` schon als Vorbild.
        async with self._speicher.transaktion():
            await self._speicher.speichere_ventil_ereignis(VentilEreignis(
                zeitstempel=start, zone_id=zone_id,
                ventil_id="sensor_heuristik",
                aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
                ausloser=ausloser, quell_zone=quell_zone,
            ))
            await self._speicher.speichere_ventil_ereignis(VentilEreignis(
                zeitstempel=stop, zone_id=zone_id,
                ventil_id="sensor_heuristik",
                aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=dauer_s,
                ausloser=ausloser, quell_zone=quell_zone,
            ))
        logger.info(
            "sensor_backfill.event_erkannt",
            zone_id=zone_id,
            start=start.isoformat(timespec="minutes"),
            delta=round(delta, 1),
            dauer_s=dauer_s,
            ausloser=ausloser.value,
            quell_zone=quell_zone,
        )
        return True

    async def _letzte_heuristik_zeit(self, zone_id: str) -> datetime | None:
        """MAX(zeitstempel) fuer bisherige Heuristik-Events dieser Zone."""
        return await self._speicher.max_ventil_zeitstempel(
            zone_id, "sensor_heuristik",
        )

    async def _aktive_bewaesserung_auf_kanal(
        self, zone_id: str, jetzt: datetime | None = None,
    ) -> bool:
        """T-0123: True wenn auf dem Kanal der Zone gerade eine Backend-
        Bewaesserung laeuft. Faellt auf False zurueck wenn Mapping fehlt,
        Speicher die Accessoren nicht hat, oder etwas crasht.

        **T-0447: gemeinsame Implementierung mit `entscheidung`** (s.
        `kanal_zustand`). Vorher stand hier eine zweite Kopie derselben
        Logik, und sie war bereits gedriftet: die T-0443-Frische-Schranke
        gab es nur drueben. Damit trug dieser Pfad weiter das Risiko, dass
        eine haengengebliebene `live_lauf_state`-Zeile die Heuristik-Events
        des Kanals dauerhaft unterdrueckt.

        **T-0446 (Verhaltensaenderung, gewollt):** der Check deckt jetzt auch
        die Soak-Pause einer Pre-Soak-Sequenz ab. Ein Feuchte-Sprung in der
        Pause stammt vom Vorbenetzungs-Puls, fuer den bereits ein echtes
        Event-Paar existiert -- ein Heuristik-Event daneben waere die
        Doppelzaehlung, die dieser Guard von Anfang an verhindern sollte.
        """
        return await kanal_vorgang_laeuft(
            self._speicher,
            zone_id=zone_id,
            kanal=self._zone_zu_kanal.get(zone_id),
            geraet_id=self._zone_zu_geraet.get(zone_id),
            jetzt=jetzt,
        )

    async def _cross_spray_quelle(
        self, zone_id: str, start: datetime, stop: datetime,
    ) -> str | None:
        """T-0312: Die Quell-Zone, wenn der Feuchte-Sprung wahrscheinlich
        Cross-Spray einer Nachbar-Zone ist (deren Sprinkler-Strahl den Sensor
        dieser Zone trifft) statt eines eigenen Kanal-Laufs. Sonst None.

        Bedingung: eine konfigurierte Quell-Zone (`cross_spray_quell_zonen`)
        hatte einen ECHTEN Kanal-Lauf (ventil_ereignis mit ventil_id !=
        'sensor_heuristik' -- auch ignoriert/automatik/gardena_web zaehlt, der
        Regner lief physisch) im Fenster [start - Karenz, stop]. Der EIGENE
        Kanal-Lauf ist an der Aufruf-Stelle bereits ausgeschlossen (Live-Check
        + Dedup + Ground-Truth-Karenz). Die Karenz deckt den Sensor-Lag
        zwischen Beregnung und Mess-Sprung ab (Zone-Karenz, Default 3 h).

        **T-0469: Rueckgabevertrag geaendert** (vorher `_ist_cross_spray` ->
        bool). Der Aufrufer schreibt den Namen als `quell_zone` an das
        FREMDWASSER-Ereignis, statt den Sprung zu unterdruecken. Bei mehreren
        konfigurierten Quellen gewinnt die erste mit echtem Lauf -- anders
        als in `api_server._ermittle_fremdwasser_quelle` ist das kein
        Ratespiel, sondern eine gepflegte Topologie-Angabe.
        """
        quellen = self._cross_spray_quellen.get(zone_id)
        if not quellen:
            return None
        karenz = self._karenz_pro_zone.get(zone_id, VERSICKERUNGS_KARENZ_DEFAULT)
        von = start - karenz
        for quell_zone in quellen:
            try:
                events = await self._speicher.hole_ventil_ereignisse(
                    quell_zone, von=von, bis=stop,
                )
            except Exception:
                logger.exception(
                    "sensor_backfill.cross_spray_query_fehler",
                    zone_id=zone_id, quell_zone=quell_zone,
                )
                continue
            # Echter Lauf der Quell-Zone = der Regner lief physisch. Eigene
            # Heuristik-Spruenge der Quell-Zone sind KEIN realer Lauf --
            # inklusive der FREMDWASSER-Paare aus T-0469, die genau denselben
            # `ventil_id` tragen. Damit kann sich Cross-Spray auch bei
            # wechselseitig konfigurierten Zonen nicht selbst weitertragen.
            echte = [e for e in events if e.ventil_id != "sensor_heuristik"]
            if not echte:
                continue
            if self._quelle_traegt_den_sprung(echte, start, stop):
                logger.info(
                    "sensor_backfill.cross_spray_erkannt",
                    zone_id=zone_id, quell_zone=quell_zone,
                    start=start.isoformat(timespec="minutes"),
                )
                return quell_zone
            # T-0531: im Fenster lief zwar etwas, aber es traegt den Sprung
            # nicht. Als Verdacht protokollieren, NICHT als `quell_zone`
            # schreiben -- der Aufrufer setzt dann `ausloser=UNBEKANNT`, und
            # die UI fordert zur Klassifikation auf, statt eine Ursache zu
            # behaupten.
            logger.info(
                "sensor_backfill.cross_spray_nur_verdacht",
                zone_id=zone_id, verdacht_quelle=quell_zone,
                start=start.isoformat(timespec="minutes"),
                grund="Quell-Lauf endete zu lange vor dem Wasserfenster",
            )
        return None

    @staticmethod
    def _quelle_traegt_den_sprung(
        echte, start: datetime, stop: datetime,
    ) -> bool:
        """T-0531: Kann einer dieser Laeufe das Wasserfenster erklaeren?

        `start`/`stop` sind kein gemessener Sprungzeitpunkt, sondern das aus
        dem Delta konstruierte Wasserfenster um die Mitte zwischen zwei
        Messungen (s. `dauer_s` oben). Eine Kausalpruefung gegen `start`
        alleine waere deshalb falsch: ein Lauf, der nach `start` beginnt,
        kann das Fenster trotzdem verursacht haben.

        Was den Fall dagegen wirklich trennt, ist die **Naehe**: beim
        Cross-Spray endet der Wassereintrag mit dem Ventil. Ein Lauf, der ins
        Fenster hineinreicht oder hoechstens `CROSS_SPRAY_NACHLAUF` davor
        endete, ist eine belegte Ursache. Endete er lange davor, ist er eine
        Vermutung -- und die gehoert nicht als Fakt ins Feld `quell_zone`
        (Realfall 08.08.2026: Handguss der Hecke, Magerwiesen-Lauf 58 min
        vorher beendet, Andre widersprach der Aussage zu Recht).

        Ein Lauf ohne SCHLIESSEN gilt als noch offen und damit als Ursache --
        die vorsichtige Richtung, denn ein fehlendes Close heisst hier
        "laeuft womoeglich noch", nicht "hat nie gewaessert".
        """
        frueheste_wirkung = start - CROSS_SPRAY_NACHLAUF
        oeffnungen = sorted(
            e.zeitstempel for e in echte
            if e.aktion == VentilAktion.OEFFNEN and e.zeitstempel <= stop
        )
        if not oeffnungen:
            return False
        enden = sorted(
            e.zeitstempel for e in echte
            if e.aktion == VentilAktion.SCHLIESSEN
        )
        for beginn in oeffnungen:
            ende = next((e for e in enden if e >= beginn), None)
            if ende is None or ende >= frueheste_wirkung:
                return True
        return False

    async def _event_im_fenster(
        self, zone_id: str, von: datetime, bis: datetime,
    ) -> bool:
        """True wenn im Fenster [von, bis] schon ein Ventil-Event liegt.

        T-0283: nimmt jetzt das fertige Fenster entgegen (vorher nur
        `mitte` + festes +/-DEDUP_FENSTER_MIN). Der Aufrufer spannt das
        Fenster ueber die volle Event-Spanne, damit lange Heuristik-Events
        ihr eigenes Paar aus dem Vorlauf wiederfinden (sonst Re-Detektions-
        Loop + Duplikat-Flut, Realfall pilea delta=70).

        Bisher wurde nur OEFFNEN gezaehlt, was bei Backend-Crash-Loops und
        Race-Conditions zu Mehrfach-SCHLIESSEN-Duplikaten ohne neues OEFFNEN
        gefuehrt hat (T-0114). Jetzt blockt jedes Event im Fenster — egal
        welche aktion oder welcher ausloser. User-klassifizierte Events
        (manuell) blockieren Re-Erzeugung; das ist gewuenscht, weil der
        User sich bereits entschieden hat.

        T-0205: `Ausloser.IGNORIERT` bei einem **Nicht-Heuristik**-Event
        wird ausgenommen. Ein als "kein echter Lauf" geflipter Live-/
        Manuell-Event darf die Heuristik fuer den echten Sensor-Sprung
        nicht blockieren. Realfall 14.05.: User flipt manuellen
        08:36-09:06-Lauf auf IGNORIERT, weil der Schlauch nie Wasser
        gefuehrt hat. Sensor-Sprung 09:30 (Handwerker-Abklemmen) soll
        dann als UNBEKANNT-Heuristik-Event erfasst werden.

        Gegensaetzliche Logik fuer **Heuristik**-Events (ventil_id =
        "sensor_heuristik") mit IGNORIERT: hier hat der User explizit
        gesagt "war Regen / Sensor-Glitch, gar kein realer Sprung" --
        die Heuristik darf den Sprung NICHT wieder neu generieren
        (T-0114-Regression-Schutz).
        """
        ereignisse = await self._speicher.hole_ventil_ereignisse(
            zone_id, von=von, bis=bis,
        )
        blockierer = [
            e for e in ereignisse
            if not (
                e.ausloser == Ausloser.IGNORIERT
                and e.ventil_id != "sensor_heuristik"
            )
        ]
        return len(blockierer) > 0

    async def _ground_truth_noch_am_wirken(
        self, zone_id: str, jetzt_messung: datetime,
    ) -> bool:
        """True wenn ein Ground-Truth-SCHLIESSEN in der Karenz-Zeit liegt.

        Karenz pro Zone aus `karenz_stunden_pro_zone` (T-0071), Fallback
        `VERSICKERUNGS_KARENZ_DEFAULT` (3 h). Ground-Truth = Live-WebSocket
        (UUID), manueller Eintrag oder DHS. Sensor-Heuristik zaehlt explizit
        NICHT (sonst kaskadiert sich ein False-Positive in alle Folge-
        Heuristiken).

        T-0205: `Ausloser.IGNORIERT` zaehlt auch NICHT als Ground-Truth.
        Der User hat das Event explizit als "kein echter Lauf"
        klassifiziert (Misfire, fehlgeschlagene Bewaesserung, Wartung).
        Damit wird die Karenz nach dem Flip aufgehoben und Sensor-
        Spruenge im selben Zeitraum koennen wieder als UNBEKANNT
        nachgeholt werden -- siehe `rescan_zone_nach_flip`.
        """
        karenz = self._karenz_pro_zone.get(zone_id, VERSICKERUNGS_KARENZ_DEFAULT)
        von = jetzt_messung - karenz
        ereignisse = await self._speicher.hole_ventil_ereignisse(
            zone_id, von=von, bis=jetzt_messung,
        )
        for e in ereignisse:
            if e.aktion != VentilAktion.SCHLIESSEN:
                continue
            if e.ventil_id == "sensor_heuristik":
                continue
            if e.ausloser == Ausloser.IGNORIERT:
                continue
            return True
        return False

    async def rescan_zone_nach_flip(
        self, zone_id: str, mitte: datetime,
    ) -> int:
        """T-0205: Re-Scan der Karenz-Periode nach Event-Flip auf
        `ignoriert`. Wenn ein Live-/Manuell-Event nachtraeglich als
        Misfire klassifiziert wird, soll die Heuristik den Sensor-
        Sprung im selben Zeitfenster nachholen.

        Hintergrund: regulaerer Periodic-Scan setzt seinen Anker auf
        die juengste Heuristik-Zeit (`_letzte_heuristik_zeit`). Wenn
        bereits ein neuerer Scan ohne Schreib-Erfolg gelaufen ist,
        liegt der Anker hinter der Karenz-Periode -- der Sprung wird
        nicht mehr betrachtet.

        Diese Methode scannt einen festen Zeitraum um `mitte` (= das
        SCHLIESSEN des geflipten Events) und ruft `_versuche_schreiben`
        ohne Anker-Logik direkt auf. Aufrufer ist der PATCH-/Bulk-
        Endpoint nach erfolgreichem Flip.
        """
        # Maximale Karenz pro Zone + Lookback auf 1 h vor das Event
        karenz = self._karenz_pro_zone.get(
            zone_id, VERSICKERUNGS_KARENZ_DEFAULT,
        )
        von = mitte - timedelta(hours=1)
        bis = mitte + karenz
        messungen = await self._speicher.hole_messungen(
            zone_id, von=von, bis=bis,
        )
        messungen = sorted(
            [m for m in messungen if m.boden_feuchte is not None],
            key=lambda m: m.zeitstempel,
        )
        if len(messungen) < 2:
            return 0

        # F12/T-0265: pro `geraet_id` getrennt iterieren, NICHT zonen-weit
        # ueber gemischte Sensoren -- sonst erzeugt der Sensor-Wechsel in der
        # Mischsequenz (waldblumen: Gardena 35 + FYTA-A 57 + FYTA-D 45)
        # Pseudo-Spruenge -> Phantom-UNBEKANNT genau die, die der User per Flip
        # gerade weggeraeumt hat (Banner-Loop). Analog zum Fix in _pruefe_zone.
        nach_sensor: dict[str, list[SensorMessung]] = {}
        for m in messungen:
            nach_sensor.setdefault(m.geraet_id or "_unbekannt", []).append(m)

        geschrieben = 0
        for sensor_messungen in nach_sensor.values():
            if len(sensor_messungen) < 2:
                continue
            for i in range(1, len(sensor_messungen)):
                alt = sensor_messungen[i - 1]
                neu = sensor_messungen[i]
                # T-0447: `mitte` ist der Anker des geflipten Events, nicht
                # "jetzt". Fuer den Kanal-Zustands-Check zaehlt die reale
                # Gegenwart -- die State-Tabellen halten nur laufende Vorgaenge.
                if await self._schreibe_wenn_kandidat(
                    zone_id, alt, neu, datetime.now(),
                ):
                    geschrieben += 1
                    logger.info(
                        "sensor_backfill.rescan_nach_flip.schreibe",
                        zone_id=zone_id,
                        delta=round(
                            float(neu.boden_feuchte) - float(alt.boden_feuchte),
                            1,
                        ),
                        mitte=mitte.isoformat(timespec="minutes"),
                    )
        return geschrieben

    async def _regen_im_fenster(
        self, zone_id: str, von: datetime, bis: datetime,
    ) -> float:
        """Summe Niederschlag (mm) im Zeitraum.

        Nutzt `hole_wetter_kombiniert` (T-0063a), das Archiv UND Vorhersage
        per Stunde mischt: Archiv hat Vorrang (nachtraeglich korrigiert), fehlende
        Stunden werden aus dem Forecast gefuellt (neueste Abfrage pro Stunde).

        **Regression-Fix 2026-04-22**: Vorher war die Logik `if archiv: nur
        Archiv, sonst Forecast`. Das versagte am 19.04.2026, als das Archiv
        bereits Daten fuer fruehe Stunden des Tages hatte (0 mm), aber noch
        keine fuer den Regen-Zeitraum ab 15:00 (Archiv-Lag 3-5 Tage). Die
        Heuristik addierte nur die 0-mm-Archiv-Stunden, ignorierte den
        vorhergesagten Regen, und schrieb 3 Phantom-Events fuer den
        Waldblumenhain. `hole_wetter_kombiniert` faellt pro Stunde zurueck,
        nicht pro Fenster — kein Luecken-Blindspot.
        """
        # F13: Standort pro Zone statt hartkodiertem Ort; Fallback = erster
        # konfigurierter Standort (aus der Konfig injiziert), nicht ein
        # Städtename im Code.
        standort = self._standort_pro_zone.get(zone_id) or self._standort_default or ""
        stunden = await self._speicher.hole_wetter_kombiniert(
            standort, von=von, bis=bis,
        )
        return sum(s.niederschlag_mm for s in stunden)
