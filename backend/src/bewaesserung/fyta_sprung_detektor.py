"""T-0416: Detektor fuer FYTA-seitige Kalibrier-Pushes (Skalen-Spruenge).

**Problem.** FYTA aendert die Kalibrierkurve ihrer Sensoren serverseitig und
fortlaufend (schriftlich bestaetigt 21.07.2026). Drei Pushes in zwoelf Tagen:
08.07. (runter, alle), 13.07. (hoch, 4 von 7), 20.07. (runter, nur Terra).
Alle drei wurden **nur zufaellig** gefunden, weil der User nachfragte. Ein
unbemerkter Push verfaelscht still ML-Training, Wirkungs-Fits und jede
FYTA-basierte Ableitung -- ohne dass irgendetwas rot wird.

**Der Kern-Diskriminator: die eingebaute Kontrollgruppe.**
In `waldblumenhain` und `hecke` steckt je ein Gardena-Sensor im selben Boden
wie die FYTA-Sensoren. Gleicher Boden, gleiches Wasser, gleiches Wetter.
Springt die FYTA und der Gardena danebendrin nicht, ist das per Definition ein
Hersteller-Artefakt und keine Bodenaenderung. Am 20.07. war das exakt so.

Diese Regel ist staerker als jede Schwellen-Heuristik, weil sie nicht fragt
"ist der Sprung gross genug?", sondern "sieht ihn nur ein Hersteller?".
Deshalb ist sie hier der PRIMAERE Pfad; die Signatur-Heuristik (mehrere Zonen,
kein Ventil, kein Regen) ist nur der Fallback fuer Zonen ohne Gardena-Partner
(kasten_4, mandevilla, zitrus).

**Was dieser Detektor bewusst NICHT tut.**
- **Keine Giess-Reaktion.** Ein Skalen-Sprung ist ein Daten-Artefakt. Wer
  daraufhin giesst, giesst wegen eines Server-Deploys bei FYTA.
- **Er schreibt NICHT selbst in `config/default.yaml`.** Die
  `ml_ausschluss_fenster` dort sind handgepflegt, kommentiert und die Single
  Source of Truth. Ein Job, der YAML umschreibt, zerstoert Kommentare und
  kaempft mit den Edits des Users. Stattdessen liefert der Detektor einen
  **fertig einsetzbaren YAML-Block** in Warnung + Log; das Eintragen bleibt
  eine bewusste menschliche Handlung. (Siehe Modul-Ende: warum die
  Auto-Injektion in die ML-Pfade ein eigener Schritt ist.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog

from .modelle import SensorMessung, SensorWarnung, SensorWarnungTyp

logger = structlog.get_logger()

# Ein Push verschiebt die Kurve deutlich; echte Bodenaenderungen in EINEM
# Messintervall (~15 min) sind ohne Wasser praktisch nie so gross.
MIN_SPRUNG_PP = 8.0
# Zwei Messungen gelten als "dasselbe Intervall", wenn sie so nah liegen.
INTERVALL_TOLERANZ_MIN = 20
# Ventil-Karenz: liegt ein Lauf so kurz zurueck, erklaert Wasser den Sprung.
VENTIL_KARENZ_H = 2.0
# Der Gardena-Partner darf sich leicht bewegen (Rauschen), ohne dass wir den
# Sprung als "auch von Gardena gesehen" werten. Deutlich unter MIN_SPRUNG_PP.
GARDENA_RUHE_PP = 3.0


@dataclass
class SprungBefund:
    """Ein Sensor, der in einem Intervall gesprungen ist."""
    geraet_id: str
    zone_id: str
    von_wert: float
    nach_wert: float
    zeitstempel: datetime

    @property
    def delta(self) -> float:
        return self.nach_wert - self.von_wert


@dataclass
class PushBefund:
    """Ein erkannter (mutmasslicher) Hersteller-Push."""
    zeitpunkt: datetime
    spruenge: list[SprungBefund]
    # Welche Regel gefeuert hat -- fuer die Meldung wichtig, weil die
    # Kontrollgruppen-Regel VIEL staerker ist als die Signatur-Heuristik.
    regel: str
    kontrollgruppe: list[str] = field(default_factory=list)

    @property
    def geraete(self) -> list[str]:
        return [s.geraet_id for s in self.spruenge]

    @property
    def zonen(self) -> list[str]:
        return sorted({s.zone_id for s in self.spruenge})


def _paare_pro_geraet(
    messungen: list[SensorMessung],
) -> dict[str, list[tuple[SensorMessung, SensorMessung]]]:
    """Aufeinanderfolgende Messpaare pro Geraet.

    Pro Geraet rechnen, nie ueber Quellen poolen -- ein Aggregat ueber
    Gardena+FYTA mischt zwei Skalen und macht jeden Sprung unlesbar
    (Memory fehlerpattern_skalen_mix_multisensor_aggregat).
    """
    pro_geraet: dict[str, list[SensorMessung]] = {}
    for m in messungen:
        if m.boden_feuchte is None or not m.geraet_id:
            continue
        pro_geraet.setdefault(m.geraet_id, []).append(m)

    paare: dict[str, list[tuple[SensorMessung, SensorMessung]]] = {}
    for geraet, liste in pro_geraet.items():
        liste.sort(key=lambda m: m.zeitstempel)
        paare[geraet] = list(zip(liste, liste[1:]))
    return paare


def finde_spruenge(
    messungen: list[SensorMessung], quelle: str = "fyta",
) -> list[SprungBefund]:
    """Alle Einzel-Intervall-Spruenge >= MIN_SPRUNG_PP einer Quelle.

    Bewusst nur Sprung, nicht Rampe: ein Push ist ein Schnitt zwischen zwei
    Messungen. Eine ueber Stunden laufende Aenderung ist Boden, nicht Server.
    """
    treffer: list[SprungBefund] = []
    for geraet, paare in _paare_pro_geraet(
        [m for m in messungen if (m.quelle or "") == quelle]
    ).items():
        for vorher, nachher in paare:
            abstand = nachher.zeitstempel - vorher.zeitstempel
            if abstand > timedelta(minutes=INTERVALL_TOLERANZ_MIN):
                continue
            delta = (nachher.boden_feuchte or 0) - (vorher.boden_feuchte or 0)
            if abs(delta) >= MIN_SPRUNG_PP:
                treffer.append(SprungBefund(
                    geraet_id=geraet,
                    zone_id=nachher.zone_id,
                    von_wert=float(vorher.boden_feuchte or 0),
                    nach_wert=float(nachher.boden_feuchte or 0),
                    zeitstempel=nachher.zeitstempel,
                ))
    return treffer


def gardena_ruhig(
    messungen: list[SensorMessung], zeitpunkt: datetime,
    zonen: set[str],
) -> tuple[bool, list[str]]:
    """Hat sich der **co-lokalisierte** Gardena-Sensor NICHT bewegt?

    Das ist der Kern von T-0416. Rueckgabe `(ruhig, geprueft)`:
    - `ruhig=True` nur, wenn mindestens EIN Gardena-Sensor **in einer der
      betroffenen Zonen** im Fenster Daten hat und alle unter
      GARDENA_RUHE_PP bleiben.
    - Ohne solchen Sensor: `ruhig=False`, `geprueft=[]` -- **kein stiller
      Freispruch**. Keine Kontrollgruppe heisst "nicht beweisbar", nicht
      "bestaetigt"; der Caller faellt auf die Signatur-Heuristik zurueck.

    **`zonen` ist nicht optional, und das ist der ganze Punkt.** Ein erster
    Entwurf pruefte ALLE Gardena-Sensoren global -- am Realdaten-Test fielen
    dadurch drei False Positives an: `kasten_4` (Topf-Zone, AquaBloom, KEIN
    Gardena-Sensor drin) wurde am 13.07. 08:07/20:09 und 15.07. 08:01 als
    "Push" gemeldet, weil irgendein Gardena in einer ANDEREN Zone gerade
    ruhig war. Das sind in Wahrheit die AquaBloom-Zyklen (~08:00/~20:00,
    +9..+11 pp), die seit T-0409 keine Ventil-Events mehr schreiben und
    deshalb auch nicht ueber die Wasser-Karenz herausfallen.
    Ein Gardena-Sensor in fremder Erde ist keine Kontrollgruppe -- anderer
    Boden, anderes Wasser. Nur "im selben Boden" traegt das Argument.
    """
    fenster = timedelta(minutes=INTERVALL_TOLERANZ_MIN)
    geprueft: list[str] = []
    for geraet, paare in _paare_pro_geraet(
        [m for m in messungen
         if (m.quelle or "") == "gardena" and m.zone_id in zonen]
    ).items():
        relevant = [
            (v, n) for v, n in paare
            if abs((n.zeitstempel - zeitpunkt).total_seconds())
            <= fenster.total_seconds()
        ]
        if not relevant:
            continue
        geprueft.append(geraet)
        for vorher, nachher in relevant:
            delta = abs(
                (nachher.boden_feuchte or 0) - (vorher.boden_feuchte or 0)
            )
            if delta >= GARDENA_RUHE_PP:
                return False, geprueft
    return bool(geprueft), geprueft


def yaml_vorschlag(befund: PushBefund) -> str:
    """Fertig einsetzbarer `ml_ausschluss_fenster`-Block.

    Geraet-scoped und **eng** (nur der Sprung, ~1 h) -- NICHT tagelang:
    die Fenster werden als Chart-Overlay gerendert und verdecken sonst die
    Daten, die sie erklaeren sollen (T-0407). Ab der naechsten Messung sind
    die Werte auf der neuen Skala wieder valide; der Sprung selbst ist das
    Artefakt, nicht die Folgezeit (Lehre T-0385).
    """
    von = befund.zeitpunkt - timedelta(minutes=30)
    bis = befund.zeitpunkt + timedelta(minutes=30)
    zeilen: list[str] = []
    for s in sorted(befund.spruenge, key=lambda x: x.geraet_id):
        zeilen.append(
            f"  - zone_id: {s.zone_id}\n"
            f"    geraet_id: {s.geraet_id}\n"
            f"    von: {von.isoformat(timespec='seconds')}\n"
            f"    bis: {bis.isoformat(timespec='seconds')}\n"
            f"    grund: \"T-0416: FYTA-Kalibrier-Push "
            f"{s.von_wert:.0f} -> {s.nach_wert:.0f} "
            f"({s.delta:+.0f} pp) in einem Messintervall. "
            f"Regel: {befund.regel}.\""
        )
    return "\n".join(zeilen)


def _baue_fenster(zone_id: str, geraet_id: str, von, bis, grund):
    """Baut ein `MlAusschlussFenster` fuer die lebende Konfig-Liste.

    Import lokal, um einen Zyklus (modelle -> ... -> detektor) zu vermeiden.
    """
    from .modelle import MlAusschlussFenster
    return MlAusschlussFenster(
        zone_id=zone_id, geraet_id=geraet_id, von=von, bis=bis, grund=grund,
    )


class FytaSprungDetektor:
    """Prueft ein Zeitfenster auf Hersteller-Pushes.

    Read-only gegenueber der Bewaesserung: schreibt nur eine Warnung.
    """

    # Intervall-Gate wie wetter_archiv_job/backup_job: der Detektor scannt
    # Messreihen ueber alle Zonen -- das gehoert NICHT in jeden 5-min-Zyklus
    # (Memory fehlerpattern_redundanter_df_build_eventloop).
    INTERVALL_MIN = 60
    # Rueckblick > Intervall, damit ein Sprung nicht zwischen zwei Laeufen
    # durchfaellt. Die Ueberlappung ist ungefaehrlich: `oeffne_sensor_warnung`
    # dedupliziert pro (zone, typ, offen), ein zweimal gesehener Push erzeugt
    # keine zweite Zeile.
    RUECKBLICK_MIN = 90

    # T-0416 (23.07.): Push-Throttle. Ein Push pro Tag reicht -- die
    # Sprung-Erkennung laeuft stuendlich mit 90-min-Rueckblick, ein Push
    # koennte sonst mehrfach fuer denselben Sprung feuern.
    PUSH_THROTTLE_STUNDEN = 24

    def __init__(
        self, speicher, zonen, benachrichtiger=None, empfaenger: str = "",
        konfig_fenster: "list | None" = None,
    ) -> None:
        self._speicher = speicher
        self._zonen = zonen
        self._letzter_lauf: datetime | None = None
        # T-0416: iMessage-Push. Optional -- ohne Benachrichtiger meldet der
        # Detektor weiterhin nur in die Ops-Timeline (Verhalten bis 23.07.).
        self._benachrichtiger = benachrichtiger
        self._empfaenger = empfaenger
        self._letzter_push: datetime | None = None
        # T-0416 Stufe 2: die LEBENDE Konfig-Liste. Ein hier eingefuegtes
        # Fenster erreicht alle sieben Konsumenten sofort, weil sie
        # dieselbe Liste lesen -- ohne dass eine Call-Site angefasst wird.
        # None -> nur DB-Persistenz, Wirkung erst nach Neustart.
        self._konfig_fenster = konfig_fenster

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> list[PushBefund]:
        """Intervall-Gate fuer den Entscheidungs-Loop."""
        jetzt = jetzt or datetime.now()
        if self._letzter_lauf is not None:
            verstrichen = (jetzt - self._letzter_lauf).total_seconds()
            if verstrichen < self.INTERVALL_MIN * 60:
                return []
        self._letzter_lauf = jetzt
        return await self.pruefe_und_melde(
            jetzt - timedelta(minutes=self.RUECKBLICK_MIN), jetzt,
        )

    async def _messungen(
        self, von: datetime, bis: datetime,
    ) -> list[SensorMessung]:
        alle: list[SensorMessung] = []
        for zone in self._zonen:
            alle.extend(
                await self._speicher.hole_messungen(zone.zone_id, von, bis)
            )
        return alle

    async def _wasser_im_fenster(
        self, zone_id: str, zeitpunkt: datetime,
    ) -> bool:
        """Lief in dieser Zone kuerzlich Wasser? Dann erklaert es den Sprung.

        Sensor-Heuristik-Pseudo-Events zaehlen NICHT als Beleg -- die werden
        selbst aus Feuchte-Spruengen abgeleitet und wuerden hier zirkulaer
        einen Push als Bewaesserung wegerklaeren.
        """
        ereignisse = await self._speicher.hole_ventil_ereignisse(
            zone_id=zone_id,
            von=zeitpunkt - timedelta(hours=VENTIL_KARENZ_H),
            bis=zeitpunkt + timedelta(minutes=INTERVALL_TOLERANZ_MIN),
        )
        return any(
            e.ventil_id != "sensor_heuristik" for e in ereignisse
        )

    async def pruefe(self, von: datetime, bis: datetime) -> list[PushBefund]:
        messungen = await self._messungen(von, bis)
        spruenge = finde_spruenge(messungen)
        if not spruenge:
            return []

        # Nach Zeitpunkt clustern -- ein Push trifft mehrere Sensoren
        # praktisch gleichzeitig.
        cluster: dict[datetime, list[SprungBefund]] = {}
        for s in spruenge:
            passend = next(
                (k for k in cluster
                 if abs((k - s.zeitstempel).total_seconds())
                 <= INTERVALL_TOLERANZ_MIN * 60),
                None,
            )
            cluster.setdefault(passend or s.zeitstempel, []).append(s)

        befunde: list[PushBefund] = []
        for zeitpunkt, gruppe in cluster.items():
            # Wasser erklaert den Sprung -> kein Push.
            mit_wasser = [
                s for s in gruppe
                if await self._wasser_im_fenster(s.zone_id, s.zeitstempel)
            ]
            ohne_wasser = [s for s in gruppe if s not in mit_wasser]
            if not ohne_wasser:
                continue

            ruhig, geprueft = gardena_ruhig(
                messungen, zeitpunkt, {s.zone_id for s in ohne_wasser},
            )
            if ruhig:
                regel = "kontrollgruppe"
            elif len({s.zone_id for s in ohne_wasser}) >= 2:
                # Fallback fuer Zonen ohne Gardena-Partner.
                regel = "signatur"
            else:
                continue

            befunde.append(PushBefund(
                zeitpunkt=zeitpunkt,
                spruenge=ohne_wasser,
                regel=regel,
                kontrollgruppe=geprueft,
            ))
        return befunde

    async def pruefe_und_melde(
        self, von: datetime, bis: datetime,
    ) -> list[PushBefund]:
        befunde = await self.pruefe(von, bis)
        for b in befunde:
            deltas = ", ".join(
                f"{s.geraet_id} {s.von_wert:.0f}->{s.nach_wert:.0f} "
                f"({s.delta:+.0f})"
                for s in sorted(b.spruenge, key=lambda x: x.geraet_id)
            )
            logger.warning(
                "fyta.kalibrier_push_verdacht",
                zeitpunkt=b.zeitpunkt.isoformat(timespec="minutes"),
                regel=b.regel,
                geraete=b.geraete,
                zonen=b.zonen,
                kontrollgruppe=b.kontrollgruppe,
                deltas=deltas,
                yaml_vorschlag=yaml_vorschlag(b),
            )
            regel_text = (
                "Kontrollgruppe: Gardena danebendrin ruhig"
                if b.regel == "kontrollgruppe"
                else "Signatur: mehrere Zonen, kein Wasser"
            )
            neu = await self._speicher.oeffne_sensor_warnung(SensorWarnung(
                zeitstempel=b.zeitpunkt,
                zone_id=b.zonen[0],
                typ=SensorWarnungTyp.FYTA_KALIBRIER_PUSH,
                details=(
                    f"Mutmasslicher FYTA-Kalibrier-Push ({regel_text}). "
                    f"Betroffen: {deltas}. Zonen: {', '.join(b.zonen)}. "
                    f"KEINE Giess-Reaktion. Zeitstempel gegenueber FYTA "
                    f"belegbar."
                ),
            ))
            await self._setze_ausschluss(b)
            await self._sende_push(b, deltas, regel_text)
            if not neu:
                # `oeffne_sensor_warnung` dedupliziert pro (zone, typ, offen).
                # Pushes wiederholen sich aber (3x in 12 Tagen), und eine noch
                # offene Warnung wuerde den naechsten still schlucken -- samt
                # der Zeitstempel, die den Beleg gegenueber FYTA ausmachen.
                # Deshalb hier explizit protokollieren: die DB-Zeile fehlt,
                # das Log traegt den Fall trotzdem.
                logger.warning(
                    "fyta.kalibrier_push_warnung_unterdrueckt",
                    grund="offene Warnung gleichen Typs in der Zone",
                    zone_id=b.zonen[0],
                    zeitpunkt=b.zeitpunkt.isoformat(timespec="minutes"),
                    deltas=deltas,
                )
        return befunde


    async def _setze_ausschluss(self, befund) -> int:
        """T-0416 Stufe 2 (23.07.): Quarantaene automatisch setzen.

        **Warum das jetzt sein muss.** Bis 22.07. lieferte der Detektor nur
        einen YAML-Vorschlag -- die Quarantaene haing daran, dass jemand die
        Warnung liest und die Datei nachzieht. FYTA hat ein
        `calibration_version`-Feld abgesagt (kein Quick Fix, niedrige
        Prioritaet), die Pushes kommen grob woechentlich, und rueckwirkend
        laesst sich einem Wert nie ansehen, auf welcher Skala er entstand.
        Jede Stunde ohne Fenster ist eine Stunde, in der Fits und
        ML-Fenster ueber die Push-Grenze hinweg kontaminiert werden.

        **Geraet-scoped, nicht zone-weit.** Ein FYTA-Push betrifft die
        FYTA-Sensoren; der Gardena in derselben Zone misst weiter richtig --
        bei hecke und waldblumenhain ist er sogar der Aggregat-Lead
        (T-0421). Ein zone-weites Fenster wuerde ihn mit stilllegen.

        **Eng, nicht tagelang.** Nur der Sprung selbst ist das Artefakt; ab
        der naechsten Messung sind die Werte auf der neuen Skala wieder
        valide (Lehre T-0385). Breite Fenster verdecken ausserdem im
        Chart-Overlay genau die Daten, die sie erklaeren sollen (T-0407).
        """
        von = befund.zeitpunkt - timedelta(minutes=30)
        bis = befund.zeitpunkt + timedelta(minutes=30)
        gesetzt = 0
        for sp in befund.spruenge:
            grund = (
                f"T-0416 AUTOMATISCH: FYTA-Kalibrier-Push erkannt "
                f"({befund.regel}), {sp.von_wert:.0f} -> {sp.nach_wert:.0f} "
                f"({sp.delta:+.0f} pp) in einem Messintervall. Werte vor und "
                f"nach diesem Fenster liegen auf VERSCHIEDENEN Skalen und "
                f"sind nicht vergleichbar."
            )
            try:
                neu = await self._speicher.speichere_auto_ausschluss(
                    sp.zone_id, sp.geraet_id, von, bis, grund,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "fyta.ausschluss_speichern_fehlgeschlagen",
                    fehler=str(exc), geraet=sp.geraet_id,
                )
                continue
            if not neu:
                continue
            gesetzt += 1
            # Sofort in die lebende Konfig-Liste -- sonst wirkt das Fenster
            # erst nach dem naechsten Neustart, und bis dahin lernt jeder
            # Fit-Job den Sprung mit.
            if self._konfig_fenster is not None:
                self._konfig_fenster.append(_baue_fenster(
                    sp.zone_id, sp.geraet_id, von, bis, grund,
                ))
            logger.warning(
                "fyta.ausschluss_automatisch_gesetzt",
                zone_id=sp.zone_id, geraet_id=sp.geraet_id,
                von=von.isoformat(timespec="minutes"),
                bis=bis.isoformat(timespec="minutes"),
            )
        return gesetzt

    async def _sende_push(self, befund, deltas: str, regel_text: str) -> None:
        """T-0416 (23.07.): iMessage bei erkanntem Push.

        **Warum hochgestuft.** Bis 22.07. lief die Warnung als ROUTINE in der
        Ops-Timeline -- vertretbar, solange sie nur die Bruecke bis zu einem
        Hersteller-Feld (`calibration_version`) war. FYTA hat abgesagt: kein
        Quick Fix, niedrige Prioritaet. Damit ist dieser Detektor die einzige
        Quelle, dauerhaft.
        Der Push vom 20.07. hat ueber den Median still den operativen
        Feuchtewert gekippt (T-0421) und eine 90-min-Giessempfehlung erzeugt.
        Ein Signal, das genau das verhindern soll, darf nicht im
        Ausnahme-Feed untergehen.

        Bewusst SACHLICH formuliert, kein Defekt-Alarm: die Sensoren sind in
        Ordnung, der Hersteller hat die Kurve verschoben. Und ausdruecklich
        ohne Handlungsaufforderung ans Ventil -- ein Skalen-Sprung ist ein
        Daten-Artefakt, kein Giess-Anlass (Lehre T-0426: der Watchdog schickte
        den Nutzer wegen eines toten Sensors in den Garten).
        """
        if not self._benachrichtiger or not self._empfaenger:
            return
        jetzt = befund.zeitpunkt
        if self._letzter_push is not None:
            verstrichen = (jetzt - self._letzter_push).total_seconds()
            if verstrichen < self.PUSH_THROTTLE_STUNDEN * 3600:
                logger.info(
                    "fyta.kalibrier_push_gedrosselt",
                    zeitpunkt=jetzt.isoformat(timespec="minutes"),
                )
                return
        text = (
            f"FYTA-Kalibrierung verschoben ({regel_text}). "
            f"Betroffen: {deltas}. "
            f"Zonen: {', '.join(befund.zonen)}. "
            f"Die Sensoren sind in Ordnung -- der Hersteller hat die Kurve "
            f"geaendert. Werte vor/nach dem Sprung sind NICHT vergleichbar; "
            f"kein Giess-Anlass. Ausschluss-Fenster pruefen (TASK T-0416)."
        )
        try:
            erfolg = await self._benachrichtiger.sende_text(
                self._empfaenger, text,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("fyta.kalibrier_push_fehlgeschlagen", fehler=str(exc))
            return
        if erfolg:
            self._letzter_push = jetzt


# ---------------------------------------------------------------------------
# BEWUSSTE SCOPE-GRENZE (T-0416 Stufe 1)
#
# Der Detektor meldet, er greift nicht automatisch in die ML-Pfade ein.
# Grund ist kein Zeitmangel, sondern eine Architektur-Frage, die eine
# Entscheidung braucht:
#
# `ml_ausschluss_fenster` wird an SIEBEN Stellen unabhaengig aus der Konfig in
# (von, bis, geraet_id)-Tripel uebersetzt (regime_klassifikation,
# k_basis_fit_job, skalen_mapping_fit_job, kalibrierung, schwellen_vorschlag,
# api_server, features). Ein DB-gestuetztes Fenster muesste an allen sieben
# ankommen, sonst entsteht genau das Muster
# `fehlerpattern_detektor_ohne_konsument`: Fenster geschrieben, halbe Pipeline
# liest es nicht -- schlimmer als kein Fenster, weil es Sicherheit vortaeuscht.
# Erschwerend: die sieben Helfer sind synchron, ein DB-Read waere async.
#
# Sauberer Weg (eigener Task): EIN Helfer `ausschluss_fenster_fuer_zone`, der
# Konfig + DB vereint, die sieben Call-Sites darauf migrieren, DANN den
# Detektor daran haengen. Bis dahin ist das Eintragen ein bewusster,
# nachvollziehbarer Handgriff -- was zur handgepflegten Natur der Datei passt.
# ---------------------------------------------------------------------------
