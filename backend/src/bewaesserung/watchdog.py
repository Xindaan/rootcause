"""T-0126 (H-2): Externes Watchdog mit iMessage-Push.

Adressiert Pre-Mortem-Akte 1, 2, 4 (siehe `~/.claude/plans/es-ist-6-monate-...`):
ohne externen Push-Pfad merkt der User im Urlaub nichts vom stillen Sensor-
Defekt, Husqvarna-Block oder DHS-Stille. Lokale UI-Indikatoren (Ops-Timeline,
Drift-Ampel) sind die Erst-Linie -- der User schaut taeglich rein
(Memory `feedback_app_check_gewohnheit.md`). Watchdog ist die Zweit-Linie
fuer Vergesslichkeits- und Urlaubs-Faelle.

Trigger heute (DHS-Stille kommt mit H-8 Endpoint-Health-Check):

1. **AKUT_IN_FOLGE**: `empfehlungs_typ='akut'` an N aufeinanderfolgenden
   Tagen fuer dieselbe Zone. Default N=3 -- 1-2 Tage faengt der User
   ueblicherweise selbst beim Dashboard-Check ab.

2. **HUSQVARNA_BLOCK**: weniger als M Gardena-Sensor-Messungen in den
   letzten F Minuten ueber alle Gardena-Zonen hinweg. Default M=5 in 60 min.
   Fruehwarn-Indikator fuer den Soft-Ban-Wiederholungsfall vom 23.04.2026
   (siehe `fehlerpattern_husqvarna_softban.md`). Zone_id='_global'.

Throttle pro Trigger-Klasse + Zone via DB-Tabelle `watchdog_event`:
max 1 Push pro `throttle_stunden` (Default 24 h). Ueberlebt Restart, damit
ein Backend-Crash-Loop nicht zu Mail-Lawinen fuehrt.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta
from typing import Any, Protocol

import structlog

from bewaesserung.benachrichtigung import Benachrichtiger
from bewaesserung.endpoint_health import STATUS_HOST_OFFLINE
from bewaesserung.entscheidung_pro_zone import EMPFEHLUNG_TRIGGERT_BEWAESSERUNG
from bewaesserung.modelle import WatchdogKonfig, ZonenKonfig, ZonenModus
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()


# Trigger-Klassen-Strings — werden auch in der DB-Throttle-Tabelle als typ
# verwendet, also stabil halten (Migration sonst noetig).
TYP_AKUT_IN_FOLGE = "akut_in_folge"
TYP_HUSQVARNA_BLOCK = "husqvarna_block"
TYP_ENDPOINT_HEALTH = "endpoint_health"        # T-0132 (H-8)
TYP_PFLEGE_FAELLIG = "pflege_faellig"          # T-0228 Stufe 2b
TYP_UNBEKANNT_EVENT = "unbekannt_event"        # T-0094
TYP_SHADOW_EMPFEHLUNG = "shadow_empfehlung"    # T-0256 Phase 1
TYP_HAHN_WARTEZEIT = "hahn_wartezeit"          # T-0537 Punkt 3


class _MotorProtokoll(Protocol):
    """Minimaler EntscheidungsMotor-Vertrag fuer Watchdog-Trigger F.

    Vermeidet zyklischen Import (Watchdog -> entscheidung.Entscheidungsmotor
    -> ... -> Watchdog). Wir brauchen nur `vorhersage_zone`.
    """

    async def vorhersage_zone(self, zone_id: str) -> Any: ...

# Sentinel fuer flotten-weite Trigger ohne konkrete Zone.
GLOBAL_ZONE_ID = "_global"


class WatchdogJob:
    """Periodische Anomalie-Detektion + iMessage-Push."""

    def __init__(
        self,
        speicher: Speicher,
        benachrichtiger: Benachrichtiger,
        konfig: WatchdogKonfig,
        zonen: list[ZonenKonfig],
        gardena_zone_ids: list[str],
        empfaenger_default: str = "",
        motor: _MotorProtokoll | None = None,
        ausschluss_fenster: "list | None" = None,
        hahn_arbiter: "Any | None" = None,
    ) -> None:
        self._speicher = speicher
        self._benachrichtiger = benachrichtiger
        self._konfig = konfig
        self._zonen = list(zonen)
        # Husqvarna-Block-Trigger zaehlt nur Messungen aus diesen Zonen --
        # FYTA-Zonen wuerden sonst Husqvarna-Stille kaschieren oder umgekehrt.
        # Caller (main.py) leitet das aus FytaKonfig + ZonenListe ab.
        self._gardena_zone_ids = list(gardena_zone_ids)
        # Konfig-empfaenger > Default (z.B. globaler IMESSAGE_EMPFAENGER env).
        self._empfaenger = konfig.empfaenger or empfaenger_default
        self._intervall = timedelta(minutes=konfig.intervall_minuten)
        self._throttle = timedelta(hours=konfig.throttle_stunden)
        self._letzte_aktualisierung: datetime | None = None
        # T-0256 Phase 1: optional, nur fuer Shadow-Empfehlungs-Push.
        # Wenn None, ist Trigger F still No-Op (Watchdog ohne Motor-Ref
        # ist z.B. in alten Tests legitim).
        self._motor = motor
        # T-0426: die aktiven Regime-Fenster (aus `konfig.ml_ausschluss_fenster`).
        # Der Watchdog kannte sie bisher nicht und meldete deshalb "bitte
        # giessen" fuer Zonen, bei denen genau dieser Zustand dokumentiert
        # erwartet wird. Optional, damit bestehende Aufrufe/Tests ohne
        # Fenster weiterlaufen (dann faellt nur der Hinweis-Satz weg).
        self._ausschluss_fenster = list(ausschluss_fenster or [])
        # T-0537 Punkt 3: der `HahnArbiter` fuehrt seit dieser Aenderung Buch
        # darueber, welcher Kanal wie lange auf den Hahn wartet. Optional --
        # ohne Arbiter (Tests, Setup ohne Ventilgeraete) ist Trigger H ein
        # stiller No-Op statt eines Fehlers.
        self._hahn_arbiter = hahn_arbiter

    async def pruefe_und_sende_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> dict[str, int]:
        """Laeuft max. einmal pro `intervall_minuten`. Gibt Statistik
        `{geprueft, gesendet}` zurueck.

        Bei `aktiv=False` oder leerem Empfaenger: stiller No-Op (kein Tick).
        """
        if not self._konfig.aktiv or not self._empfaenger:
            return {"geprueft": 0, "gesendet": 0}

        jetzt = jetzt or datetime.now()
        if (
            self._letzte_aktualisierung
            and (jetzt - self._letzte_aktualisierung) < self._intervall
        ):
            return {"geprueft": 0, "gesendet": 0}
        self._letzte_aktualisierung = jetzt

        # T-0563: jeder Trigger einzeln isoliert. Vorher liefen die sieben
        # ohne eigenes `except` hintereinander, und `_letzte_aktualisierung`
        # war oben schon gesetzt -- ein dauerhaft werfender Trigger A machte
        # damit B bis H unsichtbar, darunter den Husqvarna-Soft-Ban-Alarm
        # und die Hahn-Wartezeit-Wache. Der Aufrufer in `main.py` faengt die
        # Exception zwar, aber da ist der Tick schon verloren.
        #
        # Reihenfolge unveraendert. Nur E und H hatten bisher eigene
        # Fehlerbaender; die anderen fuenf bekommen dasselbe.
        gesendet = 0
        for name, pruefung in (
            ("akut_in_folge", self._pruefe_akut_in_folge),
            ("husqvarna_block", self._pruefe_husqvarna_block),
            ("endpoint_health", self._pruefe_endpoint_health),
            ("pflege_erinnerungen", self._pruefe_pflege_erinnerungen),
            ("unbekannt_events", self._pruefe_unbekannt_events),
            ("shadow_empfehlungen", self._pruefe_shadow_empfehlungen),
            ("hahn_wartezeit", self._pruefe_hahn_wartezeit),
        ):
            try:
                gesendet += await pruefung(jetzt)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("watchdog.trigger_fehler", trigger=name)

        if gesendet:
            logger.info("watchdog.tick", gesendet=gesendet)
        return {"geprueft": 1, "gesendet": gesendet}

    async def _pruefe_akut_in_folge(self, jetzt: datetime) -> int:
        """Trigger A: 'akut'-Empfehlung an N Tagen in Folge fuer dieselbe Zone.

        Liest `empfehlungs_audit` (geschrieben von EmpfehlungsAuditJob,
        T-0122) und prueft pro Zone, ob in JEDEM der letzten N Kalender-
        Tage mindestens ein Snapshot mit `empfehlungs_typ='akut'` existiert.
        """
        n_tage = self._konfig.akut_in_folge_tage
        # Audit-Eintraege der letzten N+1 Tage holen, damit wir Tages-
        # Buckets sauber fuellen koennen. T-0166-Fix: `jetzt` durchreichen,
        # damit Test-Szenarien mit fixiertem Datum deterministisch laufen.
        eintraege = await self._speicher.hole_empfehlungs_audit(
            tage=n_tage + 1, limit=5000, jetzt=jetzt,
        )

        # Pro Zone: Set der Tage (date-Objekte) mit mindestens einem 'akut'.
        akut_tage_pro_zone: dict[str, set] = {}
        for e in eintraege:
            if e.get("empfehlungs_typ") != "akut":
                continue
            zone = e.get("zone_id")
            ts = e.get("zeitstempel")
            if not zone or not ts:
                continue
            try:
                tag = datetime.fromisoformat(ts).date()
            except ValueError:
                continue
            akut_tage_pro_zone.setdefault(zone, set()).add(tag)

        # Soll-Tage: jetzt.date(), jetzt-1, ..., jetzt-(N-1).
        soll_tage = {(jetzt - timedelta(days=d)).date() for d in range(n_tage)}

        gesendet = 0
        for zone_id, tage in akut_tage_pro_zone.items():
            if not soll_tage.issubset(tage):
                continue
            # Throttle pruefen
            zuletzt = await self._speicher.hole_letzten_watchdog_push(
                TYP_AKUT_IN_FOLGE, zone_id,
            )
            if zuletzt is not None and (jetzt - zuletzt) < self._throttle:
                continue

            zone_name = self._zone_name(zone_id)

            # T-0426: bevor "bitte giessen" rausgeht, pruefen WORAUF das
            # 'akut' beruht. Zwei Faelle machen die Aufforderung falsch:
            #
            # (a) Der Sensor liest exakt 0.0 -- das ist bei den
            #     Gardena-Bodensensoren die Kontaktverlust-Signatur, kein
            #     Messwert. "Giess mal" auf Basis eines toten Sensors
            #     schickt den Nutzer in den Garten, ohne dass irgendetwas
            #     ueber die echte Feuchte bekannt waere.
            # (b) Fuer die Zone laeuft ein Regime, das genau diesen Zustand
            #     ERWARTET (z.B. magerwiese im Grass-Regime: der Kanal
            #     beregnet den Rasen, das Beet bekommt bewusst kein Wasser,
            #     ein flacher Sensor ist dokumentiert und richtig).
            #
            # Realfall 23.07.: beides gleichzeitig. Die Meldung lautete
            # "bitte manuell giessen" fuer eine Zone, bei der wir bewusst
            # entschieden hatten, dass sie gerade kein Wasser bekommt --
            # auf Basis eines Sensors, der seit 4 Tagen 0.0 lieferte.
            ausgefallen = await self._speicher.sensoren_auf_null_seit(
                zone_id, mindest_stunden=self._NULL_AUSFALL_STUNDEN,
                jetzt=jetzt,
            )
            regime = self._aktives_regime(zone_id, jetzt)

            if ausgefallen:
                g = ausgefallen[0]
                text = (
                    f"Watchdog: {zone_name} -- Sensor liefert seit "
                    f"{g['stunden']:.0f} h konstant 0 % (Kontaktverlust, "
                    f"kein Messwert). Die 'akut'-Meldung der letzten "
                    f"{n_tage} Tage beruht darauf. Feuchte ist derzeit "
                    f"UNBEKANNT -- vor Ort pruefen, Sensor neu einschlemmen."
                )
            else:
                text = (
                    f"Watchdog: {zone_name} steht seit {n_tage} Tagen "
                    f"auf 'akut'. Bitte ins Dashboard schauen oder "
                    f"manuell giessen."
                )
            if regime:
                text += f" Hinweis: {regime}"
            erfolg = await self._benachrichtiger.sende_text(self._empfaenger, text)
            if erfolg:
                await self._speicher.setze_watchdog_push(
                    TYP_AKUT_IN_FOLGE, zone_id, jetzt,
                )
                logger.warning(
                    "watchdog.akut_in_folge_push",
                    zone_id=zone_id, n_tage=n_tage,
                )
                gesendet += 1
        return gesendet

    async def _pruefe_husqvarna_block(self, jetzt: datetime) -> int:
        """Trigger B: juengste Gardena-Sensor-Messung aelter als Schwelle.

        Realdaten 2026-05-04: normale Cadence ~3-8 Messungen/h ueber alle
        Gardena-Sensoren (~1 pro Sensor pro Stunde, Husqvarna sendet keine
        Updates ohne Wertaenderung). Frueheres "min N in M Min"-Muster
        produzierte Fehlalarme, weil bei stabilem Wetter eine ganze Stunde
        ohne Update normal ist.

        Neue Logik: wenn die juengste Gardena-Messung aelter als
        `husqvarna_max_alter_minuten` ist (Default 90 min), dann liegt ein
        echter Cadence-Einbruch vor. 67 h Soft-Ban-Block vom 23.04.2026
        wuerde so spaetestens nach 90 min erkannt.
        """
        if not self._gardena_zone_ids:
            return 0

        # Juengste Messung ueber alle Gardena-Zonen finden.
        juengste_zeit: datetime | None = None
        for zone_id in self._gardena_zone_ids:
            messung = await self._speicher.letzte_messung(zone_id)
            if messung is None or messung.boden_feuchte is None:
                continue
            if juengste_zeit is None or messung.zeitstempel > juengste_zeit:
                juengste_zeit = messung.zeitstempel

        if juengste_zeit is None:
            # Keine Messung jemals -- Erstinstall, kein Push.
            return 0

        alter = jetzt - juengste_zeit
        max_alter = timedelta(minutes=self._konfig.husqvarna_max_alter_minuten)
        if alter <= max_alter:
            return 0

        # Throttle pruefen
        zuletzt = await self._speicher.hole_letzten_watchdog_push(
            TYP_HUSQVARNA_BLOCK, GLOBAL_ZONE_ID,
        )
        if zuletzt is not None and (jetzt - zuletzt) < self._throttle:
            return 0

        alter_min = int(alter.total_seconds() / 60)
        text = (
            f"Watchdog: Juengste Gardena-Sensor-Messung ist {alter_min} min "
            f"alt (Schwelle {self._konfig.husqvarna_max_alter_minuten} min). "
            f"Moeglicher Husqvarna-Soft-Ban -- Service-Logs pruefen."
        )
        erfolg = await self._benachrichtiger.sende_text(self._empfaenger, text)
        if not erfolg:
            return 0
        await self._speicher.setze_watchdog_push(
            TYP_HUSQVARNA_BLOCK, GLOBAL_ZONE_ID, jetzt,
        )
        logger.warning(
            "watchdog.husqvarna_block_push",
            alter_min=alter_min,
            schwelle_min=self._konfig.husqvarna_max_alter_minuten,
        )
        return 1

    async def _pruefe_hahn_wartezeit(self, jetzt: datetime) -> int:
        """T-0537 Punkt 3 (Trigger H): teure Zone wartet zu lange auf den Hahn.

        Die andere Haelfte der Naht zum `HahnArbiter`: der fuehrt Buch, hier
        wird gelesen. Beides gehoert in denselben Schritt -- ein Zustand ohne
        Leser waere der siebte Fall von
        [[fehlerpattern_detektor_ohne_konsument]] in diesem Projekt.

        Gemeldet wird NUR, was Andre als Schaden benannt hat: eine der teuren
        Zonen hat Bedarf (sonst fragte sie nicht an) und wird seit mehr als
        `schwelle_stunden` vom Arbiter abgelehnt. Die Frische-Schranke sitzt im
        Arbiter (`WARTEN_FRISCHE`) -- eine Zone, die aufgehoert hat zu fragen,
        wartet nicht mehr, sie hat keinen Bedarf mehr.
        """
        wache = getattr(self._konfig, "hahn_wache", None)
        if wache is None or not wache.aktiv or not wache.zonen:
            return 0
        if self._hahn_arbiter is None:
            return 0

        schwelle = timedelta(hours=wache.schwelle_stunden)
        gesendet = 0
        for zone_id in wache.zonen:
            try:
                zustand = self._hahn_arbiter.wartezustand(zone_id, jetzt=jetzt)
            except Exception:
                logger.exception("watchdog.hahn_wartezeit_fehler", zone_id=zone_id)
                continue
            if zustand is None:
                continue
            wartet = jetzt - zustand.seit
            if wartet < schwelle:
                continue

            zuletzt = await self._speicher.hole_letzten_watchdog_push(
                TYP_HAHN_WARTEZEIT, zone_id,
            )
            if zuletzt is not None and (jetzt - zuletzt) < self._throttle:
                continue

            blocker = ", ".join(zustand.aktive_zonen) or "unbekannt"
            stunden = wartet.total_seconds() / 3600
            text = (
                f"Watchdog: {self._zone_name(zone_id)} wartet seit "
                f"{stunden:.1f} h auf den Haupthahn (blockiert von {blocker}). "
                f"Die Zone hat Bedarf, der Start wird abgelehnt. Laeuft dort "
                f"eine Dauer-Rasenberegnung? Sonst manuell giessen."
            )
            erfolg = await self._benachrichtiger.sende_text(self._empfaenger, text)
            if not erfolg:
                continue
            await self._speicher.setze_watchdog_push(
                TYP_HAHN_WARTEZEIT, zone_id, jetzt,
            )
            logger.warning(
                "watchdog.hahn_wartezeit_push",
                zone_id=zone_id,
                wartet_h=round(stunden, 2),
                blocker=list(zustand.aktive_zonen),
            )
            gesendet += 1
        return gesendet

    async def _pruefe_unbekannt_events(self, jetzt: datetime) -> int:
        """T-0094 Trigger E: iMessage-Push bei unklassifiziertem Sensor-Sprung.

        Sensor-Heuristik (sensor_backfill) erkennt unerklaerliche
        Feuchte-Spruenge und loggt sie mit `ausloser='unbekannt'`. Heute
        zeigt das Dashboard sie im UnbekanntEventBanner -- aber nur wenn
        der User aktiv reinschaut. Mit Trigger E: aktiver iMessage-Push
        mit Sensor-Delta + Bitte um Klassifikation.

        Voraussetzungen aus Realfall 18.04.:
        - max 1 Frage pro Zone pro Throttle-Fenster (24h Default).
        - Nur Gardena-Zonen (FYTA-Beam-Cadence ist anders, andere
          Heuristik-Sensitivitaet).
        - Pro Klassifikation faellt das Event aus dem Filter
          (PATCH ausloser='manuell'/'ignoriert' aendert die WHERE-Klausel),
          d. h. nach User-Antwort kommt automatisch keine Folge-Frage.

        Pre-Filter (sensor_backfill T-0123): Heuristik schreibt KEINE
        UNBEKANNT-Events waehrend eines protokollierten Live-Laufs auf
        dem Kanal. Damit ist die Anti-Frage-Bedingung "keine Frage waehrend
        Bewaesserung" implizit erfuellt -- alle UNBEKANNT-Events sind
        per Definition unklare Sprung-Ereignisse ohne bekannten Ausloser.
        """
        fenster_h = 24
        seit = jetzt - timedelta(hours=fenster_h)
        try:
            eintraege = await self._speicher.hole_offene_unbekannt_events(
                seit=seit,
                zone_ids=self._gardena_zone_ids,
                nur_juengster_pro_zone=True,
            )
        except Exception:
            logger.exception("watchdog.unbekannt_events_lesen_fehler")
            return 0

        gesendet = 0
        for e in eintraege:
            zone_id = e["zone_id"]
            # Throttle: pro Zone max 1 Push pro `throttle_stunden`.
            zuletzt = await self._speicher.hole_letzten_watchdog_push(
                TYP_UNBEKANNT_EVENT, zone_id,
            )
            if zuletzt is not None and (jetzt - zuletzt) < self._throttle:
                continue

            ts_event = self._parse_event_zeitstempel(e["zeitstempel"])
            if ts_event is None:
                continue

            f_vor, f_nach = await self._sensor_delta_um(zone_id, ts_event)
            zone_name = self._zone_name(zone_id)
            zeit_str = ts_event.strftime("%d.%m. %H:%M")
            delta_text = self._formatiere_sensor_delta(f_vor, f_nach)
            text = (
                f"Watchdog: Sensor {zone_name} sprang um {zeit_str} "
                f"({delta_text}). Hast du gegossen? Im Dashboard auf "
                f"'Manuell' oder 'Regen/Glitch' klassifizieren."
            )
            erfolg = await self._benachrichtiger.sende_text(
                self._empfaenger, text,
            )
            if not erfolg:
                continue
            await self._speicher.setze_watchdog_push(
                TYP_UNBEKANNT_EVENT, zone_id, jetzt,
            )
            logger.warning(
                "watchdog.unbekannt_event_push",
                zone_id=zone_id, event_zeit=ts_event.isoformat(),
                f_vor=f_vor, f_nach=f_nach,
            )
            gesendet += 1
        return gesendet

    @staticmethod
    def _parse_event_zeitstempel(roh: str) -> datetime | None:
        """SQLite-Eintraege haben unterschiedliche Praezisionen
        (manchmal Mikrosekunden). isoformat() ist tolerant aber
        unter Python 3.11+ exception-prone bei ungewohnten Formaten."""
        try:
            return datetime.fromisoformat(roh)
        except (TypeError, ValueError):
            return None

    async def _sensor_delta_um(
        self, zone_id: str, ts: datetime,
    ) -> tuple[float | None, float | None]:
        """T-0094: liefert (feuchte_vor, feuchte_nach) um ein Event,
        damit der Push den Spike-Wert zeigt. Pragmatisch: juengste
        Messung 60 min vor + erste Messung 60 min nach. Wenn nichts
        gefunden: (None, None) -- Push zeigt dann generischen Text."""
        from datetime import timedelta as _td
        vor_fenster_start = ts - _td(minutes=60)
        nach_fenster_ende = ts + _td(minutes=60)
        try:
            messungen = await self._speicher.hole_messungen(
                zone_id, von=vor_fenster_start, bis=nach_fenster_ende,
            )
        except Exception:
            return (None, None)
        # `hole_messungen` returnt DESC sortiert. Wir wollen
        # "letzte vor ts" und "erste nach ts".
        vor: float | None = None
        nach: float | None = None
        for m in messungen:
            if m.boden_feuchte is None:
                continue
            if m.zeitstempel <= ts:
                if vor is None:  # juengste vor ts (DESC -> erstes Match)
                    vor = float(m.boden_feuchte)
            else:
                nach = float(m.boden_feuchte)  # spaeteste nach ts
        return (vor, nach)

    @staticmethod
    def _formatiere_sensor_delta(
        vor: float | None, nach: float | None,
    ) -> str:
        if vor is not None and nach is not None:
            delta = nach - vor
            return f"{vor:.0f}% -> {nach:.0f}%, +{delta:.0f}pp"
        if nach is not None:
            return f"jetzt {nach:.0f}%"
        return "Sensor-Sprung erkannt"

    async def _pruefe_pflege_erinnerungen(self, jetzt: datetime) -> int:
        """T-0228 Stufe 2b (Trigger D): iMessage bei faelligen
        Pflege-Erinnerungen.

        Liest die offenen Eintraege aus `pflege_erinnerung` und feuert
        einen Push, wenn `faellig_am <= jetzt`. Pro Eintrag eigener
        Throttle (zone_id=`pflege:<id>`), damit Wiederholungs-Pushes
        erst nach `throttle_stunden` raus.

        Bewusst KEIN Vorlauf-Push (z. B. "in 3 Tagen faellig") -- die
        UI-Kachel `PflegeErinnerungenBlock` hat dafuer den 3-Tage-
        Vorlauf. Watchdog-Push ist fuer "jetzt wirklich erledigen".
        """
        hole = getattr(self._speicher, "hole_pflege_erinnerungen", None)
        if hole is None:
            return 0
        try:
            eintraege = await hole(
                nur_offen=True, anstehend_tage=0, jetzt=jetzt,
            )
        except Exception:
            logger.exception("watchdog.pflege_erinnerungen_lesen_fehler")
            return 0

        gesendet = 0
        for e in eintraege:
            eid = e.get("id")
            if eid is None:
                continue
            throttle_key = f"pflege:{eid}"
            zuletzt = await self._speicher.hole_letzten_watchdog_push(
                TYP_PFLEGE_FAELLIG, throttle_key,
            )
            if zuletzt is not None and (jetzt - zuletzt) < self._throttle:
                continue

            zone = e.get("zone_id") or ""
            typ = e.get("typ") or "erinnerung"
            beschreibung = (e.get("beschreibung") or "").strip()
            zone_text = f" [{self._zone_name(zone)}]" if zone else ""
            text = (
                f"Watchdog: Pflege-Erinnerung faellig{zone_text} -- "
                f"{typ}. {beschreibung}"
                if beschreibung
                else f"Watchdog: Pflege-Erinnerung faellig{zone_text} -- {typ}."
            )
            erfolg = await self._benachrichtiger.sende_text(
                self._empfaenger, text,
            )
            if not erfolg:
                continue
            await self._speicher.setze_watchdog_push(
                TYP_PFLEGE_FAELLIG, throttle_key, jetzt,
            )
            logger.warning(
                "watchdog.pflege_push",
                erinnerung_id=eid, typ=typ, zone=zone,
            )
            gesendet += 1
        return gesendet

    async def _pruefe_endpoint_health(self, jetzt: datetime) -> int:
        """T-0132 (H-8): Endpoint-Health-Trigger.

        Liest `endpoint_health`-Tabelle (geschrieben von EndpointHealthJob)
        und feuert iMessage, wenn ein Endpoint laenger als 24 h einen
        Nicht-OK-Status haelt (= Schema-Drift / Auth-Bruch / dauerhafter
        Connect-Fehler). Adressiert Pre-Mortem Akt 4: ohne diesen Trigger
        koennte ein DHS-Schema-Update wochenlang unbemerkt bleiben.
        """
        hole = getattr(self._speicher, "hole_endpoint_health", None)
        if hole is None:
            return 0
        try:
            eintraege = await hole()
        except Exception:
            logger.exception("watchdog.endpoint_health_lesen_fehler")
            return 0

        gesendet = 0
        for e in eintraege:
            status = e.get("status")
            # T-0290: 'host_offline' = der Probe fiel in ein Offline-Fenster
            # (DNS-Fehler), nicht der Endpoint kaputt -> kein Push (wie 'ok').
            if not status or status in ("ok", STATUS_HOST_OFFLINE):
                continue
            letzter_erfolg = e.get("letzter_erfolg")
            if not letzter_erfolg:
                continue  # Nie erfolgreich = Erstinstall-Phase, kein Push
            try:
                erfolg_dt = datetime.fromisoformat(letzter_erfolg)
            except ValueError:
                continue
            if (jetzt - erfolg_dt) < timedelta(hours=24):
                continue  # Noch unter Schwelle -- transient, ignorieren

            endpoint = e.get("endpoint", "unbekannt")
            zuletzt = await self._speicher.hole_letzten_watchdog_push(
                TYP_ENDPOINT_HEALTH, endpoint,
            )
            if zuletzt is not None and (jetzt - zuletzt) < self._throttle:
                continue

            details = (e.get("details") or "")[:120]
            text = (
                f"Watchdog: Endpoint {endpoint} hat seit "
                f"{(jetzt - erfolg_dt).days} Tagen Status '{status}'. "
                f"Details: {details}. "
                f"Inoffizielle Schnittstelle -- moegliches Schema-Update."
            )
            erfolg = await self._benachrichtiger.sende_text(self._empfaenger, text)
            if not erfolg:
                continue
            await self._speicher.setze_watchdog_push(
                TYP_ENDPOINT_HEALTH, endpoint, jetzt,
            )
            logger.warning(
                "watchdog.endpoint_health_push",
                endpoint=endpoint, status=status,
                tage_seit_erfolg=(jetzt - erfolg_dt).days,
            )
            gesendet += 1
        return gesendet

    # T-0426: ab wie vielen Stunden konstanter 0.0 ein Sensor als
    # ausgefallen gilt. 24 h ist bewusst grosszuegig -- ein wirklich
    # knochentrockener Boden kann kurzzeitig 0 zeigen; erst die lange
    # Serie ist die Kontaktverlust-Signatur.
    _NULL_AUSFALL_STUNDEN = 24.0

    def _aktives_regime(self, zone_id: str, jetzt: datetime) -> str | None:
        """T-0426: laeuft fuer die Zone gerade ein dokumentiertes Regime?

        Maschinenlesbarer Marker ist `events_auto_ignorieren` im
        `ml_ausschluss_fenster` -- er bedeutet "Laeufe dieser Zone zaehlen
        in diesem Fenster bewusst nicht als echte Bewaesserung". Genau dann
        ist ein flacher Sensor erwartet und "bitte giessen" irrefuehrend.

        Realfall magerwiese: Fenster 13.06.-31.07. mit
        `events_auto_ignorieren: true` -- der Kanal beregnet den Rasen, das
        Beet bekommt bewusst kein Wasser.
        """
        for f in self._ausschluss_fenster:
            if getattr(f, "zone_id", None) != zone_id:
                continue
            if not getattr(f, "events_auto_ignorieren", False):
                continue
            von = getattr(f, "von", None)
            bis = getattr(f, "bis", None)
            if von and jetzt < von:
                continue
            if bis and jetzt > bis:
                continue
            bis_text = f" (bis {bis:%d.%m.})" if bis else ""
            return (
                f"fuer diese Zone laeuft ein dokumentiertes Regime{bis_text}"
                f" -- Laeufe zaehlen dort bewusst nicht als echte "
                f"Bewaesserung. Vor dem Giessen den Zonenblock in "
                f"config/default.yaml lesen."
            )
        return None

    def _zone_name(self, zone_id: str) -> str:
        for z in self._zonen:
            if z.zone_id == zone_id:
                return z.name
        return zone_id

    # ----- T-0256 Phase 1: Shadow-Empfehlungs-Push -----

    async def _pruefe_shadow_empfehlungen(self, jetzt: datetime) -> int:
        """Trigger F: pro automatik-Zone den Auto-Loop-Empfehlungs-Pfad
        ausfuehren und bei `soll_bewaessern=true` einen iMessage-Push
        schicken. Pre-T-0021-Beobachtungs-Modus: User sieht in
        Realzeit was der Auto-Loop tun WUERDE, vor dem Scharfschalten.

        Konvergenz mit T-0231: `motor.vorhersage_zone()` ist der
        gleiche Pfad, den der scharfe Auto-Loop in `pruefe_kanal`
        ueber `entscheide_pro_zone` nimmt. Damit kein Drift zwischen
        Push-Verhalten heute und Ventil-Verhalten morgen.

        Throttle: pro Zone via `min_pause_minuten` aus der Konfig --
        identisch zum Throttle, den der scharfe Auto-Loop zwischen
        zwei Laeufen einhaelt. Damit feuert der Shadow-Push GENAU
        dann, wann auch das Ventil aufmachen wuerde.
        """
        if self._motor is None:
            return 0  # Konfig-Pfad ohne Motor-Ref (alte Tests)

        gesendet = 0
        for zone in self._zonen:
            if zone.modus != ZonenModus.AUTOMATIK:
                continue
            if zone.ventil_kanal is None:
                continue

            # Throttle pro Zone via min_pause_minuten (Default 120).
            letzter = await self._speicher.hole_letzten_watchdog_push(
                TYP_SHADOW_EMPFEHLUNG, zone.zone_id,
            )
            min_pause = timedelta(minutes=max(zone.min_pause_minuten or 0, 0))
            if letzter is not None and (jetzt - letzter) < min_pause:
                continue

            # T-0443: kein Push, solange auf dem Kanal Wasser laeuft.
            # Zwei Gruende, warum das hier UND im Motor stehen muss:
            # (1) Der Race-Fallback unten setzt `soll=True` an der
            #     Motor-Entscheidung VORBEI (er liest einen aelteren
            #     empfehlungs_audit-Snapshot). Ein Fix nur im Bypass des
            #     Motors laesst diesen Pfad offen -- eine Zone, die zum
            #     ersten Mal pusht (`letzter is None`), pusht sonst weiter
            #     in laufendes Wasser hinein.
            # (2) Der scharfe Auto-Loop ueberspringt aktive Kanaele bereits
            #     eine Ebene hoeher (`main.py`: Pre-Soak-Lauf bzw.
            #     `ist_aktiv(kanal)` -> `continue`). Ohne diesen Check
            #     wuerde der Shadow-Push etwas melden, was das Ventil
            #     nachweislich nicht tun wuerde -- genau der Drift, den
            #     der Docstring oben ausschliesst.
            # Realfall 28.07. 05:14: "giesse 90 min" waehrend der manuelle
            # Hauptlauf seit 04:32 offen war.
            # `getattr`, weil aeltere Motor-Stubs in Tests die Methode nicht
            # haben (gleiche Konvention wie bei `hole_live_lauf_states`).
            kanal_check = getattr(
                self._motor, "kanal_aktiv_bewaesserung", None,
            )
            if kanal_check is not None:
                try:
                    kanal_aktiv = await kanal_check(zone.zone_id, jetzt)
                except Exception:
                    logger.exception(
                        "watchdog.shadow_kanal_check_fehler",
                        zone_id=zone.zone_id,
                    )
                    kanal_aktiv = False
                if kanal_aktiv:
                    logger.debug(
                        "watchdog.shadow_kanal_aktiv",
                        zone_id=zone.zone_id, kanal=zone.ventil_kanal,
                    )
                    continue

            # Empfehlung holen (gleicher Pfad wie EmpfehlungsAuditJob).
            try:
                empf = await self._motor.vorhersage_zone(zone.zone_id)
            except Exception:
                logger.exception(
                    "watchdog.shadow_motor_fehler", zone_id=zone.zone_id,
                )
                continue

            soll = bool(getattr(empf, "soll_bewaessern", False))
            blocker = getattr(empf, "blocker_typ", None)
            typ = getattr(empf, "empfehlungs_typ", "")
            dauer_s = int(getattr(empf, "dauer_s_empfehlung", None) or 0)
            prog_6h_ohne = getattr(empf, "prognose_6h", None)
            race_fallback = False

            # T-0261-Fix (2026-05-26): Race-Behandlung. EmpfehlungsAuditJob
            # und WatchdogJob laufen im selben Master-Loop-Tick hinter-
            # einander. AuditJob ruft `motor.vorhersage_zone()` ZUERST,
            # schreibt bei `soll_bewaessern=true` einen entscheidung_log-
            # Eintrag. Sekunden spaeter ruft WatchdogJob `motor.vorhersage_
            # zone()` erneut auf -- die Funktion sieht den eigenen
            # entscheidung_log-Eintrag als Pause-Anker, liefert
            # `blocker_typ=PAUSE_AKTIV` zurueck. Folge: Push wird im
            # ersten Tick nach Restart oder nach langer Trockenphase
            # unterdrueckt, obwohl das System Bedarf erkannt hat.
            # Realfall 26.05. 11:44: yogaraum 55% praeventiv, AuditJob
            # schrieb soll=1, Watchdog sah PAUSE_AKTIV, kein iMessage.
            #
            # Override: wenn (a) wir noch nie fuer diese Zone gepusht
            # haben (letzter is None), (b) Motor-Blocker ist PAUSE_AKTIV,
            # (c) Bedarfs-Klassifikation ist real (akut/praeventiv),
            # dann lese den letzten empfehlungs_audit-Snapshot der Zone
            # und verwende dessen Dauer-/Prognose-Werte (sind vor dem
            # Pause-Anker geschrieben worden).
            if (not soll and blocker == "PAUSE_AKTIV"
                    and typ in ("akut", "praeventiv")
                    and letzter is None):
                # T-0166-Pattern (Zeit-Determinismus): `jetzt` durchreichen,
                # sonst rechnet der Speicher `von = datetime.now() - 1 Tag`
                # und schliesst Test-Snapshots aus, die mit Mock-Jetzt
                # geschrieben wurden. Im Produktiv-Pfad ist `jetzt` ≈
                # `datetime.now()`, also fachlich neutral.
                snaps = await self._speicher.hole_empfehlungs_audit(
                    zone_id=zone.zone_id, tage=1, limit=10, jetzt=jetzt,
                )
                # Juengster Snapshot mit Bedarfs-Empfehlung + Dauer.
                # WICHTIG: nicht auf soll_bewaessern filtern -- nach
                # dem Pause-Anker sind alle Snapshots `soll=0` mit
                # blocker=PAUSE_AKTIV, haben aber trotzdem die
                # Bedarfs-Klassifikation (`praeventiv`) und die
                # berechnete Dauer (`dauer_s_empfehlung`) im Eintrag.
                # Diese Werte reflektieren das, was der scharfe
                # Auto-Loop tun WUERDE.
                fallback = next(
                    (s for s in snaps
                     if s.get("empfehlungs_typ") in ("akut", "praeventiv")
                     and (s.get("dauer_s_empfehlung") or 0) > 0),
                    None,
                )
                if fallback is not None:
                    soll = True
                    dauer_s = int(fallback.get("dauer_s_empfehlung") or 0)
                    typ = fallback.get("empfehlungs_typ") or typ
                    prog_6h_ohne = fallback.get("prognose_6h")
                    race_fallback = True
                    # `empf` selbst nicht modifizieren -- die Werte
                    # gehen ueber dauer_s_override + prog_6h_ohne +
                    # race_hinweis an _format_shadow_empfehlung.

            if not soll:
                continue
            # T-0560: zusaetzlich den Empfehlungs-TYP pruefen. `soll_bewaessern`
            # aus `vorhersage_zone` ist bewusst weiter gefasst als der
            # Entscheidungspfad -- es ist `empf_typ != "kein_bedarf"` und
            # schliesst damit `wohlfuehl_grenze` ein, den
            # `entscheidung_pro_zone.EMPFEHLUNG_TRIGGERT_BEWAESSERUNG`
            # ausdruecklich NICHT als Trigger fuehrt ("nur Hinweis im
            # Dashboard"). Fuer eine Karte ist "du koenntest giessen" richtig;
            # fuer einen iMessage-Push um 6 Uhr frueh ist es das nicht.
            # Ohne diese Zeile wurde aus einem sanften Wohlfuehl-Hinweis ein
            # "giesse 22 min"-Push, waehrend die Engine nichts tat.
            if typ and typ not in EMPFEHLUNG_TRIGGERT_BEWAESSERUNG:
                logger.debug(
                    "watchdog.shadow_kein_harter_trigger",
                    zone_id=zone.zone_id, empfehlungs_typ=typ,
                )
                continue
            if dauer_s <= 0:
                continue  # Sanity: 0-Dauer-Empfehlung nicht pushen

            erwartete_wirkung = self._erwartete_wirkung_pp(zone, dauer_s)
            prog_6h_mit = None
            if prog_6h_ohne is not None:
                prog_6h_mit = round(prog_6h_ohne + erwartete_wirkung, 1)

            # T-0263 (2026-05-26): ML-Dauer-Vergleich + Drift-Ampel
            # als Diagnose-Zeile im Push. Heuristik bleibt die Wahrheit
            # (oben), ML ist Sanity-Check: passt das ML-Modell, oder
            # sollte man es ignorieren? Quelle: ml_dauer_vorschlag-
            # Tabelle (juengster Eintrag) + hole_dauer_drift_metriken
            # fuer die Ampel (gruen/gelb/rot/nur_heuristik/keine_daten).
            ml_dauer_min: int | None = None
            ml_ampel: str | None = None
            try:
                ml_vorschlag = await self._speicher.hole_letzten_ml_dauer_vorschlag(
                    zone.zone_id,
                )
                if ml_vorschlag and ml_vorschlag.get("ml_s") is not None:
                    ml_dauer_min = int(round(float(ml_vorschlag["ml_s"]) / 60.0))
                # Ampel-Bestimmung: gleiche Logik wie /api/ml/dauer-drift.
                # T-0166-Pattern (Zeit-Determinismus): `jetzt` durchreichen
                # (wie bei hole_empfehlungs_audit oben), sonst rechnet der
                # Speicher das 30-Tage-Fenster gegen `datetime.now()` statt
                # gegen die Tick-Zeitreferenz -- macht den Drift-Lookup
                # tageszeitabhaengig. Produktiv ist `jetzt` ~= now(), neutral.
                drift = await self._speicher.hole_dauer_drift_metriken(
                    zone.zone_id, fenster_tage=30, jetzt=jetzt,
                )
                d = drift.get(zone.zone_id) or {}
                mae_h = d.get("mae_heuristik")
                mae_m = d.get("mae_ml")
                if mae_h is not None and mae_m is not None:
                    if mae_m <= 0.5 * mae_h:
                        ml_ampel = "gruen"
                    elif mae_m <= 0.8 * mae_h:
                        ml_ampel = "gelb"
                    else:
                        ml_ampel = "rot"
                elif mae_h is not None:
                    ml_ampel = "nur_heuristik"
            except Exception:
                logger.exception(
                    "watchdog.shadow_ml_lookup_fehler",
                    zone_id=zone.zone_id,
                )

            text = self._format_shadow_empfehlung(
                zone=zone, empf=empf,
                erwartete_wirkung_pp=erwartete_wirkung,
                prog_6h_mit=prog_6h_mit, prog_6h_ohne=prog_6h_ohne,
                dauer_s_override=dauer_s if race_fallback else None,
                race_hinweis=race_fallback,
                ml_dauer_min=ml_dauer_min,
                ml_ampel=ml_ampel,
            )
            empfaenger = self._empfaenger_fuer_zone(zone)
            if not empfaenger:
                continue
            erfolg = await self._benachrichtiger.sende_text(empfaenger, text)
            if not erfolg:
                continue
            await self._speicher.setze_watchdog_push(
                TYP_SHADOW_EMPFEHLUNG, zone.zone_id, jetzt,
            )
            logger.info(
                "watchdog.shadow_empfehlung_push",
                zone_id=zone.zone_id,
                dauer_s=dauer_s,
                erwartete_wirkung_pp=round(erwartete_wirkung, 1),
            )
            gesendet += 1
        return gesendet

    def _erwartete_wirkung_pp(
        self, zone: ZonenKonfig, dauer_s: int,
    ) -> float:
        """Plateau-Modell (T-0091b): wirkung(d) = wmax * (1 - exp(-d/tau))
        mit tau = wmax / wirkungsrate_initial.

        Wenn Plateau-Konfig fehlt: 0.0 (kein "mit"-Wert im Push). Caller
        zeigt dann nur den "ohne Aktion"-Wert.
        """
        wmax = getattr(zone, "wirkung_max_pp", None)
        r0 = getattr(zone, "wirkungsrate_initial", None)
        if not wmax or not r0 or wmax <= 0 or r0 <= 0:
            return 0.0
        tau = wmax / r0
        dauer_min = dauer_s / 60.0
        if dauer_min <= 0:
            return 0.0
        return wmax * (1.0 - math.exp(-dauer_min / tau))

    def _format_shadow_empfehlung(
        self,
        zone: ZonenKonfig,
        empf: Any,
        erwartete_wirkung_pp: float,
        prog_6h_mit: float | None,
        prog_6h_ohne: float | None,
        dauer_s_override: int | None = None,
        race_hinweis: bool = False,
        ml_dauer_min: int | None = None,
        ml_ampel: str | None = None,
    ) -> str:
        """Mehrzeiliger iMessage-Text. Bewusst kompakt: Zone, Dauer,
        aktuelle Feuchte, 6h-Erwartung mit/ohne, Grund + Strategie.

        T-0261: `dauer_s_override` erlaubt es, im Race-Fall die Dauer
        aus dem Pre-Pause-Audit-Snapshot zu verwenden, weil `empf` aus
        einem PAUSE_AKTIV-Branch keine Dauer-Berechnung enthaelt.
        `race_hinweis=True` fuegt eine kurze Zeile hinzu, damit der
        User versteht warum er den Push trotz Pause sieht."""
        # T-0535: bei scharfem Dosis-Test faehrt das Ventil die Teststufe,
        # nicht die berechnete Dosis. Der Push meldet sonst eine andere
        # Zahl als Dashboard und Hardware -- zwei Wahrheiten fuer denselben
        # Lauf. `dauer_s_override` bleibt vorn: das ist der explizite
        # Audit-Snapshot aus dem Race-Fall (T-0261) und damit die genauere
        # Aussage darueber, was entschieden wurde.
        dauer_quelle = dauer_s_override if dauer_s_override is not None \
            else (
                getattr(empf, "dauer_s_dosis_test", None)
                or empf.dauer_s_empfehlung
                or 0
            )
        dauer_min = round(dauer_quelle / 60.0)
        feuchte = getattr(empf, "feuchte_aktuell", None)
        zeilen = [f"Shadow-Empfehlung {zone.name}: gieße {dauer_min} min"]
        if feuchte is not None:
            zeilen.append(f"Aktuell: {feuchte:.0f}%")
        if prog_6h_mit is not None and prog_6h_ohne is not None:
            zeilen.append(
                f"Nach 6h: {prog_6h_mit:.0f}% mit / {prog_6h_ohne:.0f}% ohne "
                f"(+{erwartete_wirkung_pp:.0f}pp Wirkung)"
            )
        elif prog_6h_ohne is not None:
            zeilen.append(f"Nach 6h ohne Aktion: {prog_6h_ohne:.0f}%")
        grund = (getattr(empf, "erklarung_kurz", None)
                 or getattr(empf, "grund", None) or "")
        if grund:
            zeilen.append(f"Grund: {grund}")
        strategie = getattr(empf, "aktive_strategie", None)
        if strategie:
            zeilen.append(f"Strategie: {strategie}")
        # T-0263: ML-Vergleichs-Zeile. Heuristik bleibt oben die Wahrheit,
        # hier nur als Diagnose: wuerde ML eine andere Dauer empfehlen,
        # und passt das Modell aktuell zur Realitaet?
        if ml_dauer_min is not None:
            ampel_klartext = {
                "gruen": "gruen, ML besser als Heuristik",
                "gelb": "gelb, ML in der Naehe der Heuristik",
                "rot": "rot, ML schlechter -- ignoriere",
                "nur_heuristik": "noch kein ML-Vergleich",
                None: "kein Vergleich",
            }.get(ml_ampel, ml_ampel or "kein Vergleich")
            zeilen.append(
                f"ML-Vergleich: {ml_dauer_min} min ({ampel_klartext})"
            )
        elif ml_ampel == "nur_heuristik":
            zeilen.append("ML-Vergleich: noch kein ML-Modell aktiv")
        if race_hinweis:
            zeilen.append(
                "(Hinweis: System hat Bedarf erkannt, kurz danach selbst "
                "auf Pause gegangen -- erster Push, kein Race-Verlust.)"
            )
        return "\n".join(zeilen)

    def _empfaenger_fuer_zone(self, zone: ZonenKonfig) -> str:
        """zone.benachrichtigung.empfaenger > Watchdog-Default."""
        zb = getattr(zone, "benachrichtigung", None)
        zone_empfaenger = getattr(zb, "empfaenger", None) if zb else None
        return zone_empfaenger or self._empfaenger
