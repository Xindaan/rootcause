"""Leck-/Dauerlauf-Detektion fuer Bewaesserung und Sensoren.

Zwei unabhaengige Pruefungen pro Zone, saisonal aktiv (Mai-Sept):

1) **BEWAESSERUNG_OHNE_WIRKUNG**: nach einem SCHLIESSEN-Event sollte die
   Feuchte innerhalb weniger Minuten sichtbar steigen. Tut sie es nicht,
   koennte der Schlauch ab sein, der Sensor falsch liegen oder ein
   Schwanenhals verstopft sein.

   **T-0197 (2026-05-16)**: Erwartungswert pro Zone aus dem Wirkungs-
   Modell statt globaler Konstante. Drei Pfade:
   - Plateau-Modell (T-0091b, `wirkung_max_pp` + `wirkungsrate_initial`):
     `total(d) = wmax × (1 − exp(−d/τ))`, `τ = wmax/r0`. Modelliert
     Substrate, die nach kurzer Zeit saettigen (Bambus-Mikrodrip,
     Topf-Substrat).
   - Lineares Modell (`delta_pp_pro_minute` + optional
     `wirkungsrate_dauer_alpha`-log-Decay, T-0091a): klassisches
     pp/min-Modell mit optionaler Decay-Korrektur fuer lange Dosen.
   - Default-Fallback: globale `BEW_ERWARTET_PRO_SEK`-Konstante
     fuer Zonen ohne Konfig.

   Begruendung: Bambuswald + bambuswald_yogaraum haben Plateau bei
   ~8 pp. Lineares 1.2 pp/min × 45 min = 54 pp erwartet, Schwelle
   16.2 pp -> chronische Phantom-Warnungen, weil Plateau-Saettigung
   physikalisch hoechstens 7.5 pp zulaesst. Forensik: 7 Phantom-
   Warnungen 01.-14.05. fuer Bambuswald, alle real +10 pp = ueber
   Plateau-Erwartung.

2) **SENSOR_EINGEFROREN**: wenn die Feuchte ueber 48h praktisch konstant
   bleibt, liefert der Sensor vermutlich einen konstanten Wert (lose,
   defekt, oder im Funk-Loch). Plausibilitaets-Check ohne Ventil-Bezug.

Die Warnungen sind *nicht* selbstschliessend — sobald z.B. ein Feuchte-
Anstieg registriert wird, wird die Warnung hier in derselben Routine
wieder geschlossen. So bleibt die Ops-Timeline synchron.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

from bewaesserung.modelle import (
    Ausloser,
    SensorWarnung,
    SensorWarnungTyp,
    VentilAktion,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

# Saison: Monate in denen die Pruefungen laufen. Ausserhalb ist die
# Verdunstung so klein und Bewaesserung so selten, dass Alarme mehr stoeren
# als helfen.
SAISON_MONATE_DEFAULT: frozenset[int] = frozenset({5, 6, 7, 8, 9})

# "Bewaesserung ohne Wirkung"-Parameter
BEW_FENSTER_START_MIN = 20        # frueheste Auswertung nach SCHLIESSEN
BEW_FENSTER_ENDE_MIN = 90         # spaeteste Auswertung nach SCHLIESSEN
# T-0213: Mindest-Dauer hoch von 30 s auf 240 s. Bei sehr kurzen
# Laeufen (User-Testlaeufe, 1 min in der Gardena-App) ist die
# physikalisch erwartete Sensor-Reaktion < 1 pp -- unter der
# Sensor-Aufloesung (Gardena 5 pp Quantisierung, FYTA-Beam-Cadence
# 3-4 h). Eine Auswertung ist dann statistisch wertlos und produziert
# nur False-Positives. Realfall 19.05.: 58 s-Testlauf -> Alarme bei
# bambuswald/yogaraum/waldblumen/magerwiese.
BEW_MIN_DAUER_SEKUNDEN = 240
BEW_ERWARTET_PRO_SEK = 0.02       # ~1.2 %-Punkte pro Minute (konservativ)
BEW_ALARM_FAKTOR = 0.3            # Alarm wenn Real < 30 % des Erwarteten
# T-0213: Hard-Floor in pp. Liegt die erwartete Wirkung darunter,
# kann der Sensor das physikalisch nicht aufloesen (Gardena 5 pp
# Quantisierung, FYTA 3-4 h Update-Cadence). Auswertung dann
# ueberspringen. Beispiel: Plateau-Modell Bambus bei 58 s liefert
# erwartet 0.1 pp, Schwelle (mit Faktor 0.3) waere 0.03 pp --
# Groessenordnungen unter Sensor-Aufloesung.
#
# === T-0428 (23.07.2026): DIESER FLOOR WAR ZU SCHWACH ===
# Der Kommentar oben nennt zweimal "Gardena 5 pp Quantisierung" als
# Begruendung -- der Wert war trotzdem 1.0, also ein Fuenftel davon.
# Schlimmer: der Floor greift auf `erwartet`, verglichen wird aber gegen
# `erwartet * BEW_ALARM_FAKTOR` (0.3). Die wirksame Alarmgrenze fiel
# damit bis auf 0.3 pp -- der Sensor kann aber nur 5.0 pp oder nichts.
#
# Empirisch (DB, 23.07.): alle vier Gardena senden seit dem 01.07.
# AUSSCHLIESSLICH Vielfache von 5, kleinster beobachteter Abstand exakt
# 5.0. Von 248 "Bewaesserung ohne Wirkung"-Warnungen hatten **140
# (56 %) eine Erwartung unterhalb der Sensor-Aufloesung**, davon 118
# (84 %) mit `delta` exakt 0.0. Das ist kein Nachweis eines Problems,
# sondern der wahrscheinlichste normale Messwert.
BEW_ERWARTET_MIN_PP = 1.0  # ersetzt durch aufloesungs_min_pp(), s.u.

# T-0428: Quantisierung pro Geraete-Familie, in Prozentpunkten.
# Gardena liefert nur Vielfache von 5 (verifiziert ueber alle vier
# Sensoren). FYTA quantisiert feiner, hat aber statt dessen eine
# Update-Cadence von 3-4 h -- die zeitliche Untergrenze deckt
# BEW_MIN_DAUER_SEKUNDEN bereits ab.
AUFLOESUNG_PP = {"gardena": 5.0, "fyta": 1.0}
AUFLOESUNG_PP_DEFAULT = 5.0  # konservativ: im Zweifel die groebere

# T-0428: Sicherheitsfaktor auf die Aufloesung.
# Ein 5-pp-Raster springt selbst bei exakt 5 pp echter Wirkung nur dann,
# wenn der wahre Wert zufaellig eine Bin-Grenze ueberquert. Fuer eine
# verlaessliche Detektion braucht es etwa das Doppelte. Praktisch heisst
# das: nur Laeufe auswerten, deren erwartete Wirkung ueber ~10 pp liegt.
# Fuer bambuswald (wmax 26, r0 0.33, tau ~79 min) sind das Laeufe ab
# rund 38 min -- jeder Pre-Soak-Puls faellt damit korrekt heraus.
AUFLOESUNG_SICHERHEITSFAKTOR = 2.0

# T-0433: Hoechstalter eines Messwerts fuer den Lead-Divergenz-Check.
# Die Warnung behauptet einen JETZT-Zustand; auf einem 6h alten Wert waere
# das eine Behauptung ueber Vergangenes. Outdoor-Cadence liegt bei
# 15-30 min, 3 h laesst also reichlich Luft fuer Luecken.
DIVERGENZ_MAX_ALTER_H = 3.0

# "Sensor eingefroren"-Parameter
EINGEFROREN_FENSTER_STUNDEN = 48
EINGEFROREN_MIN_MESSUNGEN = 10
# T-0428: war 1.0 -- auf einem 5-pp-Sensor bedeutete das faktisch "exakt
# konstant". Der Irrtum geht Richtung False Negative (ein eingefrorener
# Sensor wird spaeter erkannt), ist also harmlos, aber es ist dieselbe
# Blindheit gegenueber der Quantisierung. Eine Spanne UNTER einer vollen
# Stufe heisst "der Sensor hat sich nie um eine Stufe bewegt".
EINGEFROREN_SPANNE_PROZENT = 1.0  # ersetzt durch aufloesungs_min_pp()


def quelle_aus_geraet_id(geraet_id: str | None) -> str:
    """T-0428: Geraete-Familie aus der ID ableiten.

    FYTA-Sensoren tragen das Praefix `fyta_`, Gardena-Sensoren sind UUIDs.
    Die ID ist damit die zuverlaessigste Quelle -- sie liegt an jeder
    Messung an, waehrend eine Zone gemischte Sensoren haben kann.
    """
    return "fyta" if (geraet_id or "").startswith("fyta_") else "gardena"


def aufloesungs_min_pp(quelle: str | None = None) -> float:
    """T-0428: kleinste Wirkung, die ein Sensor dieser Familie zeigen KANN.

    Rueckgabe ist die Quantisierung mal Sicherheitsfaktor -- also die
    Untergrenze, ab der eine Auswertung ueberhaupt aussagekraeftig ist.
    Ohne bekannte Quelle die groebere Annahme (Gardena), damit der
    Detektor im Zweifel schweigt statt falsch zu alarmieren.
    """
    aufl = AUFLOESUNG_PP.get((quelle or "").lower(), AUFLOESUNG_PP_DEFAULT)
    return aufl * AUFLOESUNG_SICHERHEITSFAKTOR


@dataclass(frozen=True)
class KanalRolle:
    """T-0433: Rolle einer Zone an ihrem Ventil-Kanal, fuer den Divergenz-Check.

    Warum das noetig wurde: `kanal_trigger_ausschluss` war **write-only**.
    Ausserhalb der Trigger-Filterung in `entscheidung.py` las das Feld
    niemand -- eine ausgeschlossene Zone war damit nicht bloss
    nicht-triggernd, sondern unsichtbar. Genau deshalb konnte die
    ausgeschlossene Zone im Juli 77 -> 30 fallen, ohne dass irgendetwas
    anschlug, waehrend die Lead-Zone durchgehend "satt" meldete.
    """

    kanal: int
    #: Traegt diese Zone `kanal_trigger_ausschluss` (= die stumme Zone)?
    ausgeschlossen: bool
    #: Eigene Kritisch-Schwelle -- jede Zone hat ihre eigene, ein
    #: Roh-Vergleich der Absolutwerte waere bei verschiedenen Kennlinien
    #: bedeutungslos.
    feuchte_kritisch: float | None = None
    #: Ventil-Geraet. ZWINGEND Teil der Kanal-Identitaet: zwei DSWCs haben
    #: beide einen "Kanal 2". Eine erste Fassung verglich nur die
    #: Kanalnummer und zaehlte damit `hecke` (DSWC 2 / K2) als Lead des
    #: Bambus-Kanals (DSWC 1 / K2) -- aufgefallen erst im Dry-Run gegen die
    #: echten Daten, nicht in den synthetischen Tests. Dokumentierte
    #: Fehlerklasse: `fehlerpattern_multi_dswc_kanal_lookup`.
    geraet_id: str | None = None

    @property
    def kanal_schluessel(self) -> tuple[str | None, int]:
        """Physische Kanal-Identitaet -- niemals die Kanalnummer allein."""
        return (self.geraet_id, self.kanal)


@dataclass(frozen=True)
class WirkungsProfil:
    """T-0197: Erwartete Sensor-Wirkung in Abhaengigkeit der Bewaesserungs-Dauer.

    Pro Zone aus `ZonenKonfig` abgeleitet (siehe `aus_zone()`). Drei
    Modell-Pfade:

    - `wirkung_max_pp` + `wirkungsrate_initial` -> Plateau-Modell:
      `total(d) = wmax × (1 − exp(−d/τ))` mit `τ = wmax / r0`.
    - `delta_pp_pro_minute` -> Lineares Modell (optional mit
      `log_decay_alpha` < 0 fuer Tiefensickerungs-Korrektur).
    - Sonst: globale `BEW_ERWARTET_PRO_SEK`-Konstante.
    """

    # Plateau-Modell (T-0091b)
    wirkung_max_pp: float | None = None
    wirkungsrate_initial: float | None = None      # pp/min
    # Lineares Modell (T-0089/T-0091a)
    delta_pp_pro_minute: float | None = None
    log_decay_alpha: float = 0.0

    @classmethod
    def aus_zone(cls, zone: ZonenKonfig) -> "WirkungsProfil":
        """Baut das Profil aus der ZonenKonfig (Backward-Compat: leere
        Konfig -> Default-Profil -> Fallback auf globale Konstante).
        """
        return cls(
            wirkung_max_pp=zone.wirkung_max_pp,
            wirkungsrate_initial=zone.wirkungsrate_initial,
            delta_pp_pro_minute=zone.delta_pp_pro_minute,
            log_decay_alpha=float(zone.wirkungsrate_dauer_alpha or 0.0),
        )

    def erwartete_wirkung_pp(self, dauer_sekunden: int) -> float:
        """T-0197: Erwarteter Sensor-Anstieg in pp fuer die gegebene
        Dauer. Auswahl-Reihenfolge: Plateau > Linear > globaler Default.
        """
        if dauer_sekunden <= 0:
            return 0.0
        d_min = dauer_sekunden / 60.0

        # Plateau-Modell hat Vorrang: physikalisch saturierendes
        # Substrat (Bambus-Mikrodrip, Topfsubstrat).
        if (
            self.wirkung_max_pp is not None
            and self.wirkungsrate_initial is not None
            and self.wirkung_max_pp > 0.0
            and self.wirkungsrate_initial > 0.0
        ):
            wmax = float(self.wirkung_max_pp)
            r0 = float(self.wirkungsrate_initial)
            tau = wmax / r0
            return wmax * (1.0 - math.exp(-d_min / tau))

        # Lineares Modell mit optionalem Decay (alpha < 0 = realistisch
        # fuer lange Dosen, Wasser sickert durch).
        if self.delta_pp_pro_minute is not None and self.delta_pp_pro_minute > 0:
            r = float(self.delta_pp_pro_minute)
            if self.log_decay_alpha and d_min > 1.0:
                # rate(d) = r * (1 + alpha * log(d/30)), Sanity-Cap > 0
                korrigiert = r * (1.0 + self.log_decay_alpha * math.log(d_min / 30.0))
                effektiv = max(0.05, korrigiert)
                return effektiv * d_min
            return r * d_min

        # Default-Fallback fuer Zonen ohne Wirkungs-Konfig.
        return float(dauer_sekunden) * BEW_ERWARTET_PRO_SEK


class LeckDetektor:
    """Periodische Pruefung ausserhalb der Entscheidungslogik."""

    def __init__(
        self,
        speicher: Speicher,
        saison_monate: frozenset[int] | set[int] | None = None,
        wirkungs_profile: dict[str, WirkungsProfil] | None = None,
        zone_quellen: dict[str, str] | None = None,
        max_feuchte_pro_zone: dict[str, float] | None = None,
        ausschluss_fenster_pro_zone: (
            dict[str, list[tuple[datetime, datetime, str | None]]] | None
        ) = None,
        fenster_ende_min_pro_zone: dict[str, int] | None = None,
        kanal_topologie: dict[str, "KanalRolle"] | None = None,
    ) -> None:
        """`wirkungs_profile` (T-0197): pro Zone die erwartete Wirkung.
        Fehlende Zone -> Default-Profil (= globaler Fallback). main.py
        baut das Mapping aus `konfig.zonen`.

        `ausschluss_fenster_pro_zone` (T-0213): pro Zone Zeitraeume in
        denen der Detektor stumm bleibt (Bodenart-Reset, Sensor-
        Neueinbau). Analog T-0211a fuer die Heuristik. Map kommt aus
        `main._baue_ausschluss_fenster_pro_zone(konfig.ml_ausschluss_
        fenster)`. Ist `jetzt` in einem Fenster, werden offene
        Warnungen geschlossen (sie sind in der Kalibrier-Phase ohnehin
        unbegruendet).

        `fenster_ende_min_pro_zone` (T-0251): pro Zone Override fuer
        `BEW_FENSTER_ENDE_MIN`. Substrate mit langsamer Sensor-Antwort
        (Bambus-Mikrodrip, Magerwiese-Sprinkler) zeigen den
        Bewaesserungs-Sprung erst nach 80-120 min -- mit dem 90-min-
        Default sieht der Detektor noch nichts und alarmiert
        faelschlich. Pro Zone in ZonenKonfig
        `detektor_fenster_ende_min` setzbar.
        """
        self._speicher = speicher
        self._saison_monate = frozenset(saison_monate) if saison_monate else SAISON_MONATE_DEFAULT
        self._wirkungs_profile: dict[str, WirkungsProfil] = dict(
            wirkungs_profile or {}
        )
        # T-0428: Sensor-Quelle pro Zone, nur fuer die Aufloesungs-
        # Untergrenze. Leer -> konservativer Default (Gardena-Raster,
        # 5 pp), damit der Detektor im Zweifel schweigt.
        self._zone_quellen: dict[str, str] = dict(zone_quellen or {})
        # T-0428: `feuchte_schwelle_max` pro Zone fuer das Saettigungs-Gate.
        self._max_feuchte: dict[str, float] = dict(max_feuchte_pro_zone or {})
        self._ausschluss_fenster_pro_zone: dict[
            str, list[tuple[datetime, datetime, str | None]]
        ] = dict(ausschluss_fenster_pro_zone or {})
        # T-0228 Stufe 2c: dynamische Wartungs-Fenster aus
        # `wartungs_fenster`-Tabelle, geladen 1x pro Tick.
        self._wartungs_fenster_pro_zone: dict[
            str, list[tuple[datetime, datetime, str | None]]
        ] = {}
        self._fenster_ende_min_pro_zone: dict[str, int] = dict(
            fenster_ende_min_pro_zone or {}
        )
        # T-0433: Kanal-Topologie fuer den Lead-Divergenz-Check.
        # Leer -> Check ist ein No-op (kein Verhalten fuer Bestands-Setups).
        self._kanal_topologie: dict[str, KanalRolle] = dict(
            kanal_topologie or {}
        )

    def _profil_fuer(self, zone_id: str) -> WirkungsProfil:
        """Pro-Zone-Profil oder leeres Default (-> globaler Fallback)."""
        return self._wirkungs_profile.get(zone_id, WirkungsProfil())

    def _mindest_erwartung_pp(self, zone_id: str) -> float:
        """T-0428: ab welcher erwarteten Wirkung ist eine Auswertung
        ueberhaupt aussagekraeftig?

        Ohne Quellen-Angabe konservativ die Gardena-Quantisierung (5 pp)
        mal Sicherheitsfaktor. Konservativ heisst hier: der Detektor
        schweigt lieber, als auf einem Raster zu alarmieren, das die
        erwartete Wirkung gar nicht abbilden kann.
        """
        return aufloesungs_min_pp(self._zone_quellen.get(zone_id))

    def _fenster_ende_min(self, zone_id: str) -> int:
        """T-0251: pro-Zone-Detektor-Fenster-Ende (Minuten nach SCHLIESSEN).
        Default `BEW_FENSTER_ENDE_MIN` (90)."""
        return self._fenster_ende_min_pro_zone.get(
            zone_id, BEW_FENSTER_ENDE_MIN,
        )

    def _ist_in_ausschluss(self, zone_id: str, jetzt: datetime) -> bool:
        """T-0213 + T-0228 Stufe 2c: True wenn `jetzt` in einem
        Ausschluss-Fenster der Zone liegt. Zwei Quellen:
        - statische `ml_ausschluss_fenster` aus YAML (T-0213)
        - dynamische Wartungs-Fenster aus DB (T-0228 Stufe 2c)
        Pausiert beide Detektor-Pruefungen (BEWAESSERUNG_OHNE_WIRKUNG
        + SENSOR_EINGEFROREN), weil Sensor-Werte in der Phase nicht
        aussagekraeftig sind.

        T-0386: Diese beiden Detektoren rechnen AGGREGAT ueber alle Sensoren
        der Zone (gemischte `werte`) -- ein einzelner Sensor laesst sich hier
        nicht sauber herausrechnen. Darum bewusst KONSERVATIV: JEDES Fenster
        (zone-weit ODER geraet-scoped) pausiert die ganze Zone (das 3. Tupel-
        Element geraet_id wird hier absichtlich ignoriert). Sicher (hoechstens
        Ueber-Pausierung), kein Umbau am Safety-Detektor. Per-Sensor-Honoring
        gehoert -- falls je gewuenscht -- in einen eigenen, getesteten Schritt."""
        for quelle in (
            self._ausschluss_fenster_pro_zone.get(zone_id),
            self._wartungs_fenster_pro_zone.get(zone_id),
        ):
            if not quelle:
                continue
            for von, bis, _f_geraet in quelle:
                if von <= jetzt <= bis:
                    return True
        return False

    async def _lade_wartungs_fenster(self, jetzt: datetime) -> None:
        """T-0228 Stufe 2c: offene Wartungs-Fenster aus DB in den
        In-Memory-Cache. Wird 1x pro `pruefe_alle`-Tick aufgerufen."""
        try:
            offene = await self._speicher.hole_wartungs_fenster(nur_offen=True)
        except Exception:
            logger.exception("leck_detektor.wartungs_fenster_load_fehler")
            return
        cap = jetzt + timedelta(days=1)
        # T-0386: 3-Tupel (geraet_id=None -- Wartungs-Fenster sind zone-weit).
        cache: dict[str, list[tuple[datetime, datetime, str | None]]] = {}
        for w in offene:
            zid = w["zone_id"]
            try:
                von = datetime.fromisoformat(w["von_am"])
            except (TypeError, ValueError):
                continue
            cache.setdefault(zid, []).append((von, cap, None))
        self._wartungs_fenster_pro_zone = cache

    async def pruefe_alle(
        self,
        zone_ids: list[str],
        jetzt: datetime | None = None,
    ) -> None:
        jetzt = jetzt or datetime.now()
        if jetzt.month not in self._saison_monate:
            return
        # T-0228 Stufe 2c: Wartungs-Fenster einmal pro Tick laden
        # (sync `_ist_in_ausschluss` greift dann auf den Cache zu).
        await self._lade_wartungs_fenster(jetzt)
        for zone_id in zone_ids:
            try:
                await self._pruefe_bewaesserung_ohne_wirkung(zone_id, jetzt)
                await self._pruefe_sensor_eingefroren(zone_id, jetzt)
                await self._pruefe_lead_divergenz(zone_id, jetzt)
            except Exception:
                logger.exception("leck_detektor.zone_fehler", zone_id=zone_id)

    async def _pruefe_bewaesserung_ohne_wirkung(
        self, zone_id: str, jetzt: datetime
    ) -> None:
        # T-0213: ml_ausschluss_fenster respektieren. In Bodenart-Reset-
        # Phasen sind Sensor-Werte volatil (10-20 pp Modell-Wechsel-
        # Spruenge) -- weder Bewertung der Wirkung noch eines
        # eingefrorenen Sensors macht Sinn. Offene Warnungen schliessen
        # (sie waren in der Phase ohnehin unbegruendet).
        if self._ist_in_ausschluss(zone_id, jetzt):
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG, jetzt,
            )
            return

        # T-0251: pro-Zone-Fenster-Ende (Default 90, langsame Substrate
        # bis 180 min). Fenster-Anfang bleibt global 20 min.
        fenster_ende_min = self._fenster_ende_min(zone_id)
        # Suchfenster fuer ALLE Kandidaten der letzten 4h, weil der
        # aktive Re-Check (s.u.) auch aeltere SCHLIESSEN-Events anfasst.
        fenster_anfang = jetzt - timedelta(hours=4)
        ereignisse = await self._speicher.hole_ventil_ereignisse(
            zone_id, von=fenster_anfang, bis=jetzt,
        )
        # SCHLIESSEN-Event im Auswertefenster [jetzt-ENDE, jetzt-START].
        # T-0185 (13.05.2026): wenn der einzige Kandidat als IGNORIERT/
        # UNBEKANNT klassifiziert ist (= Phantom-Event vom Heuristik-Job,
        # User hat ihn nachtraeglich als "kein echter Lauf" markiert),
        # gibt es keinen Wirkungs-Erwartungswert -> offene Warnung
        # automatisch schliessen statt sie haengen zu lassen.
        kandidat = None
        phantom_kandidat_gefunden = False
        for e in reversed(ereignisse):
            if e.aktion != VentilAktion.SCHLIESSEN:
                continue
            if e.dauer_sekunden < BEW_MIN_DAUER_SEKUNDEN:
                continue
            alter_min = (jetzt - e.zeitstempel).total_seconds() / 60
            if not (BEW_FENSTER_START_MIN <= alter_min <= fenster_ende_min):
                continue
            # T-0185: nur IGNORIERT (explizit als Phantom klassifiziert) als
            # Phantom-Kandidat werten. UNBEKANNT (Heuristik-default, noch
            # nicht klassifiziert) wird wie vorher behandelt — die Warnung
            # ist da gerechtfertigt, bis der User klassifiziert.
            if e.ausloser == Ausloser.IGNORIERT:
                phantom_kandidat_gefunden = True
                continue
            kandidat = e
            break
        if kandidat is None:
            if phantom_kandidat_gefunden:
                # Nur Phantom-Events im Fenster -> Warnung war eh
                # unbegruendet, jetzt schliessen.
                await self._speicher.schliesse_sensor_warnung(
                    zone_id, SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG, jetzt,
                )
                return
            # T-0251: aktiver Re-Check fuer haengende Warnungen. Wenn
            # eine offene "ohne Wirkung"-Warnung existiert und im
            # erweiterten 4h-Fenster (= jetzt-4h .. jetzt-ENDE_MIN) ein
            # SCHLIESSEN-Event liegt, fuer das delta inzwischen ueber
            # der Schwelle ist (Sensor hat verspaetet reagiert),
            # schliesse die Warnung. Verhindert haengende False-
            # Positives bei langsamen Substraten wie Bambus.
            await self._re_check_haengende_warnung(
                zone_id, ereignisse, fenster_ende_min, jetzt,
            )
            return

        # Feuchte VOR der Bewaesserung: Messung direkt vor dem SCHLIESSEN,
        # reduziert um die Bewaesserungsdauer (wir wollen den Start-Feuchte).
        start_zeit = kandidat.zeitstempel - timedelta(seconds=kandidat.dauer_sekunden)
        vor_messungen = await self._speicher.hole_messungen(
            zone_id,
            von=start_zeit - timedelta(hours=1),
            bis=start_zeit + timedelta(minutes=5),
        )
        # Feuchte NACH der Bewaesserung: neueste Messung
        nach_messungen = await self._speicher.hole_messungen(
            zone_id, von=kandidat.zeitstempel
        )
        # T-0400: vor UND nach vom selben Geraet -- sonst mischt der
        # Wirkungs-Check zwei Sensor-Skalen (isomorph T-0396).
        vor, nach = _wirkungs_paar_gleiches_geraet(vor_messungen, nach_messungen)
        if vor is None or nach is None:
            return

        delta = nach - vor
        # T-0197: Pro-Zone-Wirkungs-Modell statt globaler Konstante.
        profil = self._profil_fuer(zone_id)
        erwartet = profil.erwartete_wirkung_pp(kandidat.dauer_sekunden)
        # T-0213: Hard-Floor unter Sensor-Aufloesung. Bei sehr kurzen
        # Laeufen oder Plateau-Saettigung kann die Erwartung physikalisch
        # so klein werden, dass sie unter Quantisierung / Mess-Rauschen
        # liegt -- jede Auswertung produziert dann nur False-Positives.
        # In diesem Fall keinen Alarm setzen + offene Warnung schliessen.
        # T-0428: Untergrenze ist die SENSOR-AUFLOESUNG, nicht 1.0 pp.
        # Und sie greift auf die tatsaechliche Alarmgrenze
        # (`erwartet * BEW_ALARM_FAKTOR`), nicht auf `erwartet` --
        # sonst fiel die wirksame Grenze auf ein Drittel des Floors.
        # T-0428 (2): Saettigungs-Gate -- zweite, UNABHAENGIGE Absicherung.
        # Steht der Boden schon ueber dem Zonen-Maximum, kann Wasser den
        # Messwert nicht heben, egal wie lange gegossen wird und egal wie
        # fein der Sensor aufloest. Realfall 23.07.: die Bambuswald-Karte
        # sagte selbst "Feuchte 90 % ueber Maximum 75 %" und zeigte
        # gleichzeitig "Bewaesserung ohne Wirkung".
        max_feuchte = self._max_feuchte.get(zone_id)
        if max_feuchte is not None and vor >= max_feuchte:
            logger.debug(
                "leck_detektor.gesaettigt_uebersprungen",
                zone_id=zone_id, vor=round(vor, 1), maximum=max_feuchte,
            )
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG, jetzt,
            )
            return

        mindest_pp = self._mindest_erwartung_pp(zone_id)
        if erwartet < mindest_pp:
            logger.debug(
                "leck_detektor.unter_sensoraufloesung",
                zone_id=zone_id, erwartet=round(erwartet, 2),
                mindest_pp=mindest_pp, dauer_s=kandidat.dauer_sekunden,
            )
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG, jetzt,
            )
            return
        schwelle = erwartet * BEW_ALARM_FAKTOR

        if delta < schwelle:
            details = (
                f"Dauer {kandidat.dauer_sekunden}s, Feuchte {vor:.1f}% -> {nach:.1f}% "
                f"(delta {delta:+.1f}, erwartet >={schwelle:.1f})"
            )
            neu = await self._speicher.oeffne_sensor_warnung(SensorWarnung(
                zeitstempel=jetzt,
                zone_id=zone_id,
                typ=SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG,
                details=details,
            ))
            if neu:
                logger.warning(
                    "leck_detektor.bewaesserung_ohne_wirkung",
                    zone_id=zone_id, vor=round(vor, 1), nach=round(nach, 1),
                    delta=round(delta, 2), dauer_s=kandidat.dauer_sekunden,
                )
        else:
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG, jetzt
            )

    async def _re_check_haengende_warnung(
        self,
        zone_id: str,
        ereignisse: list,
        fenster_ende_min: int,
        jetzt: datetime,
    ) -> None:
        """T-0251: aktiver Re-Check fuer haengende "ohne Wirkung"-Warnungen.

        Wird aufgerufen wenn die normale Kandidaten-Suche im Fenster
        [jetzt-ENDE, jetzt-START] kein SCHLIESSEN gefunden hat, aber
        evtl. eine offene Warnung existiert. Pruefen wir, ob im
        erweiterten Fenster (4h zurueck bis ENDE_MIN) ein SCHLIESSEN
        existiert, fuer das delta inzwischen ueber der Schwelle ist
        (Sensor hat verspaetet reagiert).

        Vermeidet haengende False-Positives bei langsamen Substraten
        (z.B. Bambus mit ~80 min Sensor-Latenz, wo der erste Detektor-
        Tick noch nichts sieht aber der zweite Tick 60 min spaeter
        den Sprung registrieren wuerde).
        """
        offene = await self._speicher.offene_sensor_warnungen(zone_id)
        if not any(
            w.typ == SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG
            for w in offene
        ):
            return  # nichts zu schliessen

        # Spaetes Re-Check-Fenster: Events aelter als ENDE_MIN aber
        # juenger als 4h. Wir wollen NICHT in die normale Auswertung
        # eingreifen, sondern nur stale Warnungen pruefen.
        kandidat = None
        for e in reversed(ereignisse):
            if e.aktion != VentilAktion.SCHLIESSEN:
                continue
            if e.dauer_sekunden < BEW_MIN_DAUER_SEKUNDEN:
                continue
            if e.ausloser not in (Ausloser.AUTOMATIK, Ausloser.MANUELL):
                continue
            alter_min = (jetzt - e.zeitstempel).total_seconds() / 60
            # Spaeter als normales Fenster-Ende, juenger als 4h
            if alter_min <= fenster_ende_min:
                continue
            if alter_min > 240:  # 4h Cap
                continue
            kandidat = e
            break
        if kandidat is None:
            # T-0297: kein verspaeteter automatik/manuell-Kandidat. Existiert
            # ueberhaupt noch ein RECHTFERTIGENDES SCHLIESSEN (non-ignoriert,
            # dauer >= min) im 4h-Fenster? Wenn NICHT, ist die offene Warnung
            # verwaist und muss geschlossen werden -- sonst haengt sie ewig:
            #   - das Original-Event wurde nach Warnungs-Oeffnung auf
            #     'ignoriert' umklassifiziert (T-0277-Bulk-Flip "Regen/Glitch")
            #     und ist aus dem [START,ENDE]-Fenster gealtert, ODER
            #   - es ist komplett aus dem 4h-Fenster gefallen.
            # Realfall waldblumenhain: Heuristik-Event 03.06. 19:45 (1080s)
            # oeffnete die Warnung als 'unbekannt', wurde 08.06. auf
            # 'ignoriert' geflippt -> Badge hing 5 Tage. (UNBEKANNT zaehlt
            # weiter als rechtfertigend = noch nicht klassifiziert.)
            rechtfertigend = any(
                e.aktion == VentilAktion.SCHLIESSEN
                and e.dauer_sekunden >= BEW_MIN_DAUER_SEKUNDEN
                and e.ausloser in (
                    Ausloser.AUTOMATIK, Ausloser.MANUELL, Ausloser.UNBEKANNT,
                )
                for e in ereignisse
            )
            if not rechtfertigend:
                await self._speicher.schliesse_sensor_warnung(
                    zone_id, SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG, jetzt,
                )
            return

        # Sensor-Werte VOR + NACH (gleiche Logik wie Haupt-Pfad)
        start_zeit = kandidat.zeitstempel - timedelta(
            seconds=kandidat.dauer_sekunden,
        )
        vor_messungen = await self._speicher.hole_messungen(
            zone_id,
            von=start_zeit - timedelta(hours=1),
            bis=start_zeit + timedelta(minutes=5),
        )
        nach_messungen = await self._speicher.hole_messungen(
            zone_id, von=kandidat.zeitstempel,
        )
        # T-0400: vor/nach vom selben Geraet (s. _wirkungs_paar_gleiches_geraet).
        vor, nach = _wirkungs_paar_gleiches_geraet(vor_messungen, nach_messungen)
        if vor is None or nach is None:
            return

        delta = nach - vor
        profil = self._profil_fuer(zone_id)
        erwartet = profil.erwartete_wirkung_pp(kandidat.dauer_sekunden)
        # T-0428: dieselbe Aufloesungs-Untergrenze wie im Alarm-Pfad.
        # Beide Stellen MUESSEN mitziehen -- sonst wuerde der Re-Check
        # eine Warnung offen halten, die der Alarm-Pfad nie mehr setzen
        # wuerde (Isomorphie-Check T-0428).
        if erwartet < self._mindest_erwartung_pp(zone_id):
            # Erwartung unter Sensor-Aufloesung -> Warnung war ohnehin
            # falsch (T-0213-Pfad). Schliessen.
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG, jetzt,
            )
            return
        schwelle = erwartet * BEW_ALARM_FAKTOR
        if delta >= schwelle:
            logger.info(
                "leck_detektor.spaete_wirkung_erkannt",
                zone_id=zone_id, vor=round(vor, 1), nach=round(nach, 1),
                delta=round(delta, 2),
                dauer_s=kandidat.dauer_sekunden,
                alter_min=round((jetzt - kandidat.zeitstempel).total_seconds() / 60),
            )
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG, jetzt,
            )

    async def _pruefe_sensor_eingefroren(
        self, zone_id: str, jetzt: datetime
    ) -> None:
        # T-0213: ml_ausschluss_fenster respektieren. Bei Sensor-
        # Neueinbau (Hecke 19.05.) liefert der Sensor anfangs konstant
        # 0 -- das wuerde als "eingefroren" markiert, ist aber regulaere
        # Einschwing-Phase. Offene Warnung schliessen, neue nicht
        # oeffnen, solange wir im Fenster sind.
        if self._ist_in_ausschluss(zone_id, jetzt):
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.SENSOR_EINGEFROREN, jetzt,
            )
            return

        von = jetzt - timedelta(hours=EINGEFROREN_FENSTER_STUNDEN)
        messungen = await self._speicher.hole_messungen(zone_id, von=von)
        # T-0391: PRO GERAET pruefen statt ueber die zonen-gemischten Werte.
        # "Eingefroren" ist eine Eigenschaft EINES Sensors; ein zone-weites
        # max-min liess die Varianz gesunder Nachbarn einen eingefrorenen
        # Einzelsensor maskieren. Kritisch bei `aggregat_lead_geraet` (hecke):
        # dort traegt genau EIN Sensor die Bewaesserungs-Entscheidung.
        # Isomorph zu `fehlerpattern_dedup_pro_zone_multisensor`.
        # `SensorWarnung` hat kein geraet_id-Feld -> betroffene Geraete stehen
        # in `details` (kein Schema-Umbau). NICHT gefangen wird ein Stufensprung
        # (flat 0 -> flat 100): max-min ist dann gross, per Definition kein
        # Flatline -- dafuer braeuchte es einen eigenen Detektor.
        pro_geraet: dict[str, list[float]] = {}
        for m in messungen:
            if m.boden_feuchte is None:
                continue
            pro_geraet.setdefault(m.geraet_id, []).append(float(m.boden_feuchte))

        geprueft = 0
        eingefroren: list[tuple[str, float, int]] = []
        for geraet_id, werte in sorted(pro_geraet.items()):
            if len(werte) < EINGEFROREN_MIN_MESSUNGEN:
                continue  # zu duenne Datenlage fuer DIESEN Sensor
            geprueft += 1
            spanne = max(werte) - min(werte)
            # T-0428: Schwelle ist eine volle Quantisierungs-Stufe, nicht
            # die alte 1.0. Auf einem 5-pp-Raster bedeutete 1.0 faktisch
            # "exakt konstant" -- ein Sensor, der genau einmal um eine
            # Stufe sprang, galt als gesund. "Spanne unter einer Stufe"
            # ist die praezise Formulierung von "hat sich nie bewegt".
            # Hier bewusst OHNE Sicherheitsfaktor: der Detektor soll bei
            # 48 h Konstanz anschlagen, nicht erst bei zwei Stufen.
            #
            # WICHTIG -- Quelle aus der GERAETE-ID, nicht aus der Zone.
            # Diese Schleife laeuft PRO GERAET; eine Zone kann Gardena und
            # FYTA mischen. Ein erster Entwurf nahm die Zonen-Quelle und
            # haette damit die Gardena-Schwelle (5 pp) auf FYTA-Sensoren
            # angewandt -- gemessen haette das drei NEUE False Positives
            # erzeugt (drei FYTA-Sensoren der waldblumen-Zone, alle mit
            # Spanne 3.0). Gerade die waldblumen-FYTA stehen wegen des
            # Skalenbruchs (T-0385) ohnehin niedrig; sie zusaetzlich als
            # "eingefroren" zu melden waere doppelt falsch.
            eingefroren_schwelle = aufloesungs_min_pp(
                quelle_aus_geraet_id(geraet_id)
            ) / AUFLOESUNG_SICHERHEITSFAKTOR
            if spanne < eingefroren_schwelle:
                eingefroren.append((geraet_id, spanne, len(werte)))

        if geprueft == 0:
            # Kein Sensor hat genug Messungen -> Zustand unveraendert lassen.
            # NICHT schliessen: sonst versteckt eine Datenluecke eine echte
            # offene Warnung (Verhalten wie vor T-0391).
            return

        if not eingefroren:
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.SENSOR_EINGEFROREN, jetzt
            )
            return

        details = "; ".join(
            f"{geraet_id}: {anzahl} Messungen in {EINGEFROREN_FENSTER_STUNDEN}h, "
            f"Spanne {spanne:.2f}% (Schwelle "
            f"{aufloesungs_min_pp(quelle_aus_geraet_id(geraet_id)) / AUFLOESUNG_SICHERHEITSFAKTOR:.1f}%)"
            for geraet_id, spanne, anzahl in eingefroren
        )
        neu = await self._speicher.oeffne_sensor_warnung(SensorWarnung(
            zeitstempel=jetzt,
            zone_id=zone_id,
            typ=SensorWarnungTyp.SENSOR_EINGEFROREN,
            details=details,
        ))
        if neu:
            logger.warning(
                "leck_detektor.sensor_eingefroren",
                zone_id=zone_id,
                geraete=[g for g, _, _ in eingefroren],
                geprueft=geprueft,
            )

    async def _pruefe_lead_divergenz(
        self, zone_id: str, jetzt: datetime
    ) -> None:
        """T-0433: schlaegt an, wenn der Kanal-Ausschluss das Ergebnis kippt.

        **Nicht** ein Alarm auf blosse Divergenz. Zwischen `bambuswald` und
        `bambuswald_yogaraum` liegt dauerhaft ein Offset von ~20 pp; darauf
        zu alarmieren produziert nur Alarm-Muedigkeit, und ein Waechter, den
        man wegklickt, ist keiner (dieselbe Lehre wie beim AST-Guard mit 15
        Fehlalarmen, T-0416).

        Entscheidungsrelevant wird der Dissens erst in genau einer
        Konstellation: **die stumme Zone meldet unter IHRER Kritisch-
        Schwelle, waehrend der Lead ueber SEINER liegt.** Dann -- und nur
        dann -- aendert der Ausschluss das Ergebnis: der Kanal giesst nicht,
        obwohl eine seiner Zonen um Wasser bittet. Solange beide dasselbe
        sagen, ist der Ausschluss folgenlos und niemand muss etwas wissen.

        Verglichen wird jede Zone gegen ihre EIGENE Schwelle, nie die
        Absolutwerte gegeneinander: die beiden Sensoren haben verschiedene
        Kennlinien, ein Roh-Delta waere bedeutungslos.

        Bewusst keine Giess-Reaktion. Welcher der beiden Sensoren recht hat,
        ist eine offene Frage (T-0433) -- der Waechter macht den Dissens
        sichtbar, er entscheidet ihn nicht.
        """
        rolle = self._kanal_topologie.get(zone_id)
        if rolle is None or not rolle.ausgeschlossen:
            return  # nur die stumme Zone traegt die Warnung
        if rolle.feuchte_kritisch is None:
            return
        # In Kalibrier-/Wartungsfenstern ist der Wert der stummen Zone
        # ohnehin nicht belastbar -- dann schweigen und ggf. aufraeumen.
        if self._ist_in_ausschluss(zone_id, jetzt):
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.LEAD_DIVERGENZ, jetzt,
            )
            return

        eigen = await self._letzte_feuchte_frisch(zone_id, jetzt)
        if eigen is None:
            return  # keine frischen Daten -> Zustand nicht anfassen
        if eigen >= rolle.feuchte_kritisch:
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.LEAD_DIVERGENZ, jetzt,
            )
            return

        # Die Lead-Zonen desselben Kanals: sagen die auch "durstig", ist der
        # Ausschluss folgenlos (der Kanal giesst dann ohnehin).
        leads: list[tuple[str, float, float]] = []
        for andere_id, andere in self._kanal_topologie.items():
            if andere_id == zone_id or andere.ausgeschlossen:
                continue
            # (geraet_id, kanal) -- NICHT die Kanalnummer allein, sonst
            # gilt hecke (DSWC 2 / K2) als Lead des Bambus-Kanals
            # (DSWC 1 / K2). Siehe KanalRolle.geraet_id.
            if andere.kanal_schluessel != rolle.kanal_schluessel:
                continue
            if andere.feuchte_kritisch is None:
                continue
            wert = await self._letzte_feuchte_frisch(andere_id, jetzt)
            if wert is not None:
                leads.append((andere_id, wert, andere.feuchte_kritisch))
        if not leads:
            return
        # Nur wenn KEIN Lead unter seiner Schwelle liegt, bleibt die Bitte
        # der stummen Zone unbeantwortet.
        if any(wert < krit for _, wert, krit in leads):
            await self._speicher.schliesse_sensor_warnung(
                zone_id, SensorWarnungTyp.LEAD_DIVERGENZ, jetzt,
            )
            return

        lead_text = ", ".join(
            f"{lid} {wert:.0f}% (kritisch {krit:.0f}%)" for lid, wert, krit in leads
        )
        details = (
            f"{zone_id} {eigen:.0f}% unter kritisch "
            f"{rolle.feuchte_kritisch:.0f}%, aber vom Kanal-Trigger "
            f"ausgeschlossen; Lead meldet satt: {lead_text}. "
            f"Kanal {rolle.kanal} giesst deshalb nicht."
        )
        neu = await self._speicher.oeffne_sensor_warnung(SensorWarnung(
            zeitstempel=jetzt,
            zone_id=zone_id,
            typ=SensorWarnungTyp.LEAD_DIVERGENZ,
            details=details,
        ))
        if neu:
            logger.warning(
                "leck_detektor.lead_divergenz",
                zone_id=zone_id,
                kanal=rolle.kanal,
                eigen=round(eigen, 1),
                eigen_kritisch=rolle.feuchte_kritisch,
                leads={lid: round(w, 1) for lid, w, _ in leads},
            )

    async def _letzte_feuchte_frisch(
        self, zone_id: str, jetzt: datetime,
        max_alter_h: float = DIVERGENZ_MAX_ALTER_H,
    ) -> float | None:
        """Juengster Feuchtewert der Zone, falls frisch genug.

        Ein veralteter Wert darf hier nichts ausloesen: die Warnung sagt
        "das ist JETZT der Zustand". Bei Sensor-Dropout lieber schweigen --
        dafuer ist die AUSFALL-Warnung zustaendig.
        """
        von = jetzt - timedelta(hours=max_alter_h)
        messungen = await self._speicher.hole_messungen(zone_id, von=von)
        gueltig = [
            m for m in messungen if m.boden_feuchte is not None
        ]
        if not gueltig:
            return None
        juengste = max(gueltig, key=lambda m: m.zeitstempel)
        return float(juengste.boden_feuchte)


def _wirkungs_paar_gleiches_geraet(
    vor_messungen, nach_messungen,
) -> tuple[float | None, float | None]:
    """T-0400: `vor` UND `nach` vom SELBEN Geraet zurueckgeben.

    Vorher nahm `_letzte_gueltige_feuchte` den chronologisch letzten gueltigen
    Wert IRGENDEINES Geraets -- bei Multi-Sensor-Zonen (hecke, waldblumenhain)
    konnten `vor` (z.B. Gardena) und `nach` (z.B. FYTA, s. T-0410) von
    verschiedenen Sensoren mit unvergleichbaren Skalen stammen; das Delta war
    dann Muell und der Wirkungs-Check falsch (isomorph zu T-0396).

    Waehlt das Geraet mit der JUENGSTEN nach-Messung, das auch eine vor-Messung
    hat -- bei hecke faellt das natuerlich auf den 5-min-Gardena (= faktisch der
    aggregat_lead), ohne Config-Abhaengigkeit. Single-Sensor-Zonen: identisch
    zum alten Verhalten (letzter gueltiger vor/nach).
    """
    def juengste_pro_geraet(messungen) -> dict[str, float]:
        pro: dict[str, float] = {}
        for m in sorted(messungen, key=lambda x: x.zeitstempel, reverse=True):
            if m.boden_feuchte is not None and m.geraet_id not in pro:
                pro[m.geraet_id] = float(m.boden_feuchte)
        return pro

    vor_pro = juengste_pro_geraet(vor_messungen)
    nach_pro = juengste_pro_geraet(nach_messungen)
    for m in sorted(nach_messungen, key=lambda x: x.zeitstempel, reverse=True):
        if m.geraet_id in vor_pro and m.geraet_id in nach_pro:
            return vor_pro[m.geraet_id], nach_pro[m.geraet_id]
    return None, None
