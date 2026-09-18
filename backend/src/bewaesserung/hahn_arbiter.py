"""T-0536: zentrale, geraeteuebergreifende Arbitrierung des Wasserhahns.

**Warum es das gibt.** Der Hahn-Lock aus T-0151/T-0153 sass in der
`VentilSicherung` -- und davon gibt es eine Instanz PRO Water-Control-Geraet.
Jede kannte nur die Kanaele ihres eigenen Geraets, und ihr Sperrzustand
(`_aktiv`) enthielt nur Laeufe, die die Engine selbst gestartet hat. Damit
war der Lock an zwei Stellen blind:

1. **Ueber Geraetegrenzen.** waldblumenhain (DSWC 1, K1) und magerwiese
   (DSWC 2, K1) sind beide `exklusiv: true` im selben
   `hahn_cluster: garten-haupthahn`. Die Sicherung von DSWC 1 sah
   magerwiese nie. Am 12.08.2026 liefen beide Sprinkler parallel an einem
   10-L/min-Hahn mit 2-bar-Minderer (2x 6,0 L/min) -- kein Volumen-, sondern
   ein Druckproblem: die Sprinkler-Reichweite kollabiert, der Lauf wird aber
   voll verbucht.
2. **Ueber Ausloeser-Grenzen.** Die magerwiese-Laeufe kommen aus der
   Gardena-App (Zone ist `modus: monitoring`, Events `ausloser='ignoriert'`).
   Sie stehen in `ventil_ereignis`, aber nie in `_aktiv`. Ein bloss
   geraeteuebergreifender Arbiter haette den Vorfall deshalb NICHT
   verhindert -- er muss den Hahn-Zustand aus der DB lesen.

**Drei Quellen, Vereinigungsmenge.** Ein Kanal gilt als aktiv, wenn er in
mindestens einer auftaucht:

- `engine`: `_aktiv` aller registrierten `VentilSicherung`-Instanzen. Fuer
  eigene Laeufe die schnellste und verlaesslichste Quelle (kein DB-Lag).
- `ereignis`: offenes OEFFNEN ohne SCHLIESSEN in `ventil_ereignis`. Das ist
  die einzige Quelle, die FREMDE Laeufe kennt (App, Cloud-Zeitplan, Cron).
  **`ausloser='ignoriert'` wird hier bewusst MITGEZAEHLT** -- "ignoriert"
  heisst "zaehlt nicht in Bilanz/ML", nicht "Ventil ist zu". Genau
  umgekehrt als im Orphan-Close-Job, wo dieselbe Zeile ausgeschlossen wird.
- `pre_soak`: laufende Pre-Soak-Sequenz einer EXKLUSIVEN Zone. Waehrend der
  Soak-Pause ist deren Ventil zu, der Vorgang laeuft aber weiter; ohne diese
  Quelle koennte sich ein fremder Lauf in die Pause legen und die Hauptdose
  danach blockieren (bzw. mit ihr kollidieren).

**Betreiber-Vorgabe (Andre, 12.08.2026), schaerfer als T-0153.** Startet eine
`exklusiv`-Zone, darf NICHTS anderes laufen -- kein zweiter Regner, keine
Tropfbewaesserung, auf keinem Ventil, egal welches Geraet. Der Check nimmt
deshalb fuer exklusive Starter JEDEN aktiven Kanal als Blocker, auch einen
ohne Cluster-/Verbrauchs-Konfiguration und auch einen voellig unbekannten.
Fuer nicht-exklusive Starter bleibt es bei der alten, cluster-lokalen
Rechnung (aktiver Exklusiv-Nachbar oder Volumen-Ueberschreitung).

**Invariante (Andre, 13.08.2026): ein laufender Vorgang wird NIE abgebrochen.**
Der Arbiter kennt nur ein Werkzeug, und das ist das Verweigern eines STARTS.
Wer schon laeuft, laeuft zu Ende -- auch wenn eine exklusive Zone deshalb
warten muss. Wartezeit ist billig, ein zerstueckelter Tropflauf nicht: die
Soak-Wirkung haengt an der ununterbrochenen Einwirkzeit. Falls hier je eine
"dann stoppen wir eben den anderen"-Logik entstehen soll, ist das eine
Betreiber-Entscheidung und kein Implementierungsdetail.

**Was der Arbiter NICHT kann.** Er kontrolliert nur Starts DIESER Engine.
Startet die Gardena-App ihren Regner, waehrend ein Tropfkreis laeuft, sieht
er das (Ereignis-Quelle), kann es aber nicht verhindern -- dafuer muesste er
ein fremdes Ventil schliessen, und das faellt unter dieselbe Invariante.

**Fehlerrichtung ist asymmetrisch.** Faellt die DB-Abfrage aus, blockiert ein
exklusiver Starter (fail-closed): ein verpasster Sprinkler-Lauf kostet einen
Loop-Tick, ein unbemerkt halbierter Sprinkler-Lauf kostet eine stille
Unterversorgung bei voller Verbuchung. Nicht-exklusive Starter laufen mit dem
Engine-Zustand weiter (fail-open) -- sie duerfen nicht an einem DB-Fehler
haengen bleiben.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

from bewaesserung.kanal_zustand import MAX_OFFEN, laufende_pre_soak_zonen

logger = structlog.get_logger()

# `MAX_OFFEN` (Frische-Schranke fuer ein OEFFNEN ohne SCHLIESSEN) wohnt seit
# T-0537 in `kanal_zustand` -- beide Module beantworten dieselbe Frage und
# duerfen dabei nicht auseinanderlaufen. Import oben, hier nur der Zeiger.

# So lange wartet ein Starter maximal auf die Reservierung, bevor er aufgibt
# (s. `HahnArbiter.reservierung`). Grosszuegig gegenueber einem normalen
# Cloud-Aufruf, aber endlich.
RESERVIERUNG_TIMEOUT_S = 60.0

# T-0537 Punkt 3: so lange bleibt eine Ablehnung "frisch". Der Auto-Loop fragt
# alle 5 Minuten erneut an, SOLANGE die Zone Bedarf hat -- hoert er auf zu
# fragen, ist der Bedarf weg (Regen, Sensor erholt, Zeitfenster zu) und die
# Wartezeit darf nicht weiterlaufen. Ohne diese Schranke wuerde eine einzelne
# Ablehnung um 08:00 die Wache um 14:00 ausloesen, obwohl seit 08:05 niemand
# mehr starten wollte -- eine Wache, die einen laengst erledigten Wunsch meldet,
# wird nach dem zweiten Fehlalarm ignoriert. 30 min = sechs Loop-Ticks, also
# grosszuegig gegenueber einem einzelnen Aussetzer.
WARTEN_FRISCHE = timedelta(minutes=30)


@dataclass(frozen=True)
class HahnLockEntscheidung:
    """Ergebnis der Hahn-Pruefung.

    `erlaubt=True` -> Ventil darf oeffnen.
    `erlaubt=False` -> `grund` enthaelt Klartext-Begruendung,
    `aktive_zonen` listet die Zonen, die den Start verhindern.
    """
    erlaubt: bool
    grund: str = ""
    aktive_zonen: tuple[str, ...] = ()
    verbrauch_aktuell_lpm: float = 0.0
    verbrauch_neu_lpm: float = 0.0
    budget_lpm: float = 0.0


@dataclass(frozen=True)
class Wartezustand:
    """T-0537 Punkt 3: seit wann wartet ein Kanal auf den Hahn.

    `seit` ist der Beginn der ununterbrochenen Wartestrecke (erste Ablehnung
    nach einer Pause), `zuletzt` die juengste Ablehnung -- daraus entscheidet
    der Leser, ob noch jemand wartet oder ob der Bedarf vorbei ist
    (s. `WARTEN_FRISCHE`). `grund`/`aktive_zonen` stammen aus der letzten
    Ablehnung, damit eine Meldung sagen kann, WER blockiert.
    """
    seit: datetime
    zuletzt: datetime
    grund: str = ""
    aktive_zonen: tuple[str, ...] = ()


@dataclass(frozen=True)
class Kanalprofil:
    """Lock-Eigenschaften eines physischen Kanals (Geraet + Kanalnummer).

    Mehrere Zonen koennen an einem Kanal haengen (bambuswald +
    bambuswald_yogaraum an DSWC1/K2): sie teilen einen Lauf, also zaehlt der
    hoechste `verbrauch_lpm` einmal, und `exklusiv` ist True, sobald
    IRGENDEINE Zone des Kanals exklusiv ist.
    """
    zone_ids: tuple[str, ...] = ()
    cluster: str | None = None
    verbrauch_lpm: float | None = None
    exklusiv: bool = False
    # T-0552: hat mindestens eine Zone des Kanals ein `bevorzugte_zeiten`-
    # Fenster? Das ist eine ANDERE Eigenschaft als `exklusiv` und darf nicht
    # mit ihr verwechselt werden:
    #
    #   exklusiv               = Hydraulik. Braucht der Kanal den vollen Druck?
    #   zeitfenster_gebunden   = Agronomie. Verfaellt die Gelegenheit heute?
    #
    # Sprinkler haben beides (Druck UND Fenster gegen Aerosolverlust und
    # Blattbrand), Mikrodrip keines von beidem -- T-0260 hat die Fenster dort
    # ausdruecklich entfernt, weil sie bei Tropfbewaesserung nichts schuetzen.
    # Dass heute dieselben Zonen beide Merkmale tragen, ist ein Zufall der
    # aktuellen Anlage, kein Gesetz.
    zeitfenster_gebunden: bool = False


@dataclass(frozen=True)
class AktiverKanal:
    """Ein Kanal, auf dem gerade Wasser laeuft (oder ein Vorgang haengt)."""
    geraet_id: str
    kanal: int
    zone_ids: tuple[str, ...] = ()
    quelle: str = "engine"        # engine | ereignis | pre_soak


def baue_kanalprofile(
    zonen, geraet_id: str | None = None,
) -> dict[tuple[str, int], Kanalprofil]:
    """Verdichtet die Zonen-Konfig zu einem Profil je (geraet_id, kanal).

    `zonen` sind `ZonenKonfig`-Objekte. `geraet_id` ueberschreibt das Feld der
    Zone und ist der Normalfall aus `main.py`: dort ist die Zuordnung schon
    aufgeloest, inklusive der Backward-Compat-Regel "Zone ohne
    `ventil_geraet_id` haengt an der ersten entdeckten DSWC". Ohne den
    Parameter zaehlt das Zonenfeld, und Zonen ohne Kanal oder ohne Geraet
    fallen raus (kein physischer Kanal).
    """
    profile: dict[tuple[str, int], Kanalprofil] = {}
    for z in zonen:
        kanal = getattr(z, "ventil_kanal", None)
        eigen_geraet = geraet_id or getattr(z, "ventil_geraet_id", None)
        if kanal is None or not eigen_geraet:
            continue
        schluessel = (eigen_geraet, int(kanal))
        alt = profile.get(schluessel, Kanalprofil())
        verbrauch = getattr(z, "verbrauch_lpm", None)
        if alt.verbrauch_lpm is not None:
            verbrauch = (
                alt.verbrauch_lpm if verbrauch is None
                else max(alt.verbrauch_lpm, verbrauch)
            )
        profile[schluessel] = Kanalprofil(
            zone_ids=alt.zone_ids + (z.zone_id,),
            cluster=getattr(z, "hahn_cluster", None) or alt.cluster,
            verbrauch_lpm=verbrauch,
            exklusiv=alt.exklusiv or bool(getattr(z, "exklusiv", False)),
            # Wie bei `exklusiv`: gebunden, sobald IRGENDEINE Zone des Kanals
            # ein Fenster hat -- die Zonen teilen sich einen Lauf.
            zeitfenster_gebunden=(
                alt.zeitfenster_gebunden
                or bool(getattr(z, "bevorzugte_zeiten", None))
            ),
        )
    return profile


def _parse_ventil_id(ventil_id: str) -> tuple[str, int] | None:
    """`"<geraet-uuid>:<kanal>"` -> `(geraet_id, kanal)`; sonst None.

    Nicht-Kanal-Quellen (`sensor_heuristik` fuer Aquabloom/Heuristik-Events)
    tragen keinen Kanal-Suffix und sind kein Ventil -- die gehoeren nicht in
    den Hahn-Zustand.
    """
    geraet_id, trenner, kanal_roh = ventil_id.rpartition(":")
    if not trenner or not geraet_id or not kanal_roh.isdigit():
        return None
    return geraet_id, int(kanal_roh)


class HahnArbiter:
    """Eine Wahrheit fuer "darf dieser Kanal jetzt oeffnen"."""

    def __init__(
        self,
        speicher,
        kanalprofile: dict[tuple[str, int], Kanalprofil],
        cluster_max_lpm: dict[str, float],
        max_offen: timedelta = MAX_OFFEN,
    ):
        self._speicher = speicher
        self._profile = dict(kanalprofile)
        self._cluster_max_lpm = dict(cluster_max_lpm)
        self._max_offen = max_offen
        # geraet_id -> Objekt mit `aktive_kanaele()`. Registrierung statt
        # Konstruktor-Argument, weil die Sicherungen erst nach dem Arbiter
        # entstehen koennen und die Referenz in beide Richtungen gebraucht wird.
        self._sicherungen: dict[str, object] = {}
        # zone_id -> (geraet_id, kanal), fuer die Pre-Soak-Aufloesung.
        self._zone_zu_kanal: dict[str, tuple[str, int]] = {
            zid: schluessel
            for schluessel, profil in self._profile.items()
            for zid in profil.zone_ids
        }

        # Zwischen "darf ich?" und "ich laufe jetzt" liegen mehrere `await`
        # (DB-Lesen, Cloud-Call). Ohne Serialisierung koennen zwei Starter --
        # Auto-Loop und manueller Endpoint, oder Auto-Loop und Pre-Soak --
        # beide ein Ja bekommen und danach beide oeffnen. Genau das soll hier
        # nicht mehr passieren koennen, also haelt der Starter den Lock bis
        # sein Kanal im Sperrzustand steht.
        self._start_lock = asyncio.Lock()

        # T-0537 Punkt 3: (geraet_id, kanal) -> `Wartezustand`. Der Arbiter ist
        # sonst gedaechtnislos, sieht aber als EINZIGER jede Startanfrage samt
        # Ergebnis -- und nur hier ist "wollte starten, durfte nicht" ueberhaupt
        # sichtbar. Der Leser sitzt im `WatchdogJob` (Trigger H); ohne ihn waere
        # das hier der siebte Fall von [[fehlerpattern_detektor_ohne_konsument]].
        self._wartend: dict[tuple[str, int], Wartezustand] = {}

    def registriere_sicherung(self, geraet_id: str, sicherung) -> None:
        self._sicherungen[geraet_id] = sicherung

    @asynccontextmanager
    async def reservierung(self):
        """Kontext fuer "pruefen und oeffnen am Stueck" (s. `_start_lock`).

        Der Lock wird ueber den Cloud-Aufruf gehalten -- genau das macht ihn
        wirksam, und genau das macht ihn gefaehrlich: haengt der Aufruf, waere
        ohne Schranke JEDER weitere Ventilstart blockiert, dauerhaft und
        lautlos. Deshalb wird nur die ANNAHME begrenzt, nicht die Haltedauer:
        wer die Reservierung nicht binnen `RESERVIERUNG_TIMEOUT_S` bekommt,
        startet nicht (der Auto-Loop versucht es im naechsten Tick wieder).
        Ein Timeout auf die Haltedauer waere falsch -- er wuerde einen
        laufenden Oeffnungsversuch fuer gescheitert erklaeren, obwohl das
        Ventil offen sein kann ([[fehlerpattern_api_timeout_kein_fehlschlag]]).
        """
        try:
            await asyncio.wait_for(
                self._start_lock.acquire(), RESERVIERUNG_TIMEOUT_S,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.error(
                "hahn_arbiter.reservierung_timeout",
                wartezeit_s=RESERVIERUNG_TIMEOUT_S,
                hinweis="Ein anderer Starter haelt die Reservierung -- "
                        "haengender Cloud-Aufruf? Start wird verweigert.",
            )
            raise
        try:
            yield
        finally:
            self._start_lock.release()

    def profil(self, geraet_id: str, kanal: int) -> Kanalprofil | None:
        return self._profile.get((geraet_id, int(kanal)))

    async def pruefe(
        self, geraet_id: str, kanal: int, jetzt: datetime | None = None,
    ) -> HahnLockEntscheidung:
        """Darf `kanal` auf `geraet_id` jetzt oeffnen?

        Nebenwirkung (T-0537 Punkt 3): jede Anfrage schreibt den Wartezustand
        des Kanals fort -- Ablehnung startet bzw. verlaengert ihn, ein Ja
        loescht ihn. Bewusst in dieser Klammer und nicht in den fuenf
        Return-Pfaden von `_pruefe_intern`: einen davon zu vergessen hiesse,
        dass eine Zone nach der Rueckkehr aus genau diesem Fall ewig zu warten
        SCHEINT, obwohl sie laengst laeuft.
        """
        kanal = int(kanal)
        eff_jetzt = jetzt or datetime.now()
        entscheidung = await self._pruefe_intern(geraet_id, kanal, eff_jetzt)
        self._schreibe_wartezustand(geraet_id, kanal, eff_jetzt, entscheidung)
        return entscheidung

    def _schreibe_wartezustand(
        self, geraet_id: str, kanal: int, jetzt: datetime,
        entscheidung: HahnLockEntscheidung,
    ) -> None:
        schluessel = (geraet_id, int(kanal))
        if entscheidung.erlaubt:
            self._wartend.pop(schluessel, None)
            return
        alt = self._wartend.get(schluessel)
        # Eine Ablehnung nach langer Stille beginnt eine NEUE Wartestrecke.
        # Sonst zaehlte die Wache Pausen mit, in denen gar kein Bedarf bestand,
        # und meldete nach dem ersten Blocker des Tages faelschlich "wartet
        # seit heute frueh".
        fortsetzung = alt is not None and (jetzt - alt.zuletzt) <= WARTEN_FRISCHE
        self._wartend[schluessel] = Wartezustand(
            seit=alt.seit if fortsetzung else jetzt,
            zuletzt=jetzt,
            grund=entscheidung.grund,
            aktive_zonen=tuple(entscheidung.aktive_zonen),
        )

    def wartezustand(
        self, zone_id: str, jetzt: datetime | None = None,
        frische: timedelta = WARTEN_FRISCHE,
    ) -> Wartezustand | None:
        """T-0537 Punkt 3: wartet diese Zone gerade auf den Hahn?

        Zone statt (Geraet, Kanal), damit die Aufloesung an EINER Stelle bleibt
        -- mehrere Zonen teilen sich einen Kanal (bambuswald +
        bambuswald_yogaraum), und ein zweites Mapping beim Aufrufer waere genau
        die zweite Wahrheit, die die Projektregel verbietet.

        `None` heisst "wartet nicht": Zone ohne Kanal, nie abgelehnt, oder die
        letzte Ablehnung ist aelter als `frische` -- dann fragt niemand mehr
        nach, der Bedarf ist vorbei.
        """
        schluessel = self._zone_zu_kanal.get(zone_id)
        if schluessel is None:
            return None
        zustand = self._wartend.get(schluessel)
        if zustand is None:
            return None
        if ((jetzt or datetime.now()) - zustand.zuletzt) > frische:
            return None
        return zustand

    async def _pruefe_intern(
        self, geraet_id: str, kanal: int, eff_jetzt: datetime,
    ) -> HahnLockEntscheidung:
        eigen = self._profile.get((geraet_id, kanal)) or Kanalprofil()

        aktive, db_fehler = await self._aktive_kanaele(
            eff_jetzt, ausser=(geraet_id, kanal), frager=eigen,
        )

        # --- Betreiber-Regel: exklusiv heisst "gar nichts anderes" ---------
        if eigen.exklusiv:
            if db_fehler:
                return HahnLockEntscheidung(
                    erlaubt=False,
                    grund=(
                        "Hahn-Zustand nicht lesbar (DB-Fehler) -- exklusiver "
                        "Lauf wird vorsichtshalber nicht gestartet."
                    ),
                    verbrauch_neu_lpm=eigen.verbrauch_lpm or 0.0,
                )
            if aktive:
                zonen = self._zonen_namen(aktive)
                return HahnLockEntscheidung(
                    erlaubt=False,
                    grund=(
                        f"exklusive Zone {list(eigen.zone_ids)} startet nicht, "
                        f"solange irgendetwas laeuft: {zonen} "
                        f"(Quellen: {sorted({a.quelle for a in aktive})})."
                    ),
                    aktive_zonen=zonen,
                    verbrauch_aktuell_lpm=self._summe_lpm(aktive, eigen.cluster),
                    verbrauch_neu_lpm=eigen.verbrauch_lpm or 0.0,
                    budget_lpm=self._cluster_max_lpm.get(eigen.cluster or "", 0.0),
                )

        # --- Ab hier: cluster-lokale Rechnung wie T-0151/T-0153 -----------
        if eigen.cluster is None or eigen.verbrauch_lpm is None:
            return HahnLockEntscheidung(erlaubt=True)
        budget = self._cluster_max_lpm.get(eigen.cluster)
        if budget is None:
            return HahnLockEntscheidung(erlaubt=True)

        im_cluster = [
            a for a in aktive
            if (self._profile.get((a.geraet_id, a.kanal)) or Kanalprofil()).cluster
            == eigen.cluster
        ]
        aktiver_verbrauch = self._summe_lpm(im_cluster, eigen.cluster)
        aktive_zonen = self._zonen_namen(im_cluster)
        aktiver_exklusiv = any(
            (self._profile.get((a.geraet_id, a.kanal)) or Kanalprofil()).exklusiv
            for a in im_cluster
        )

        if im_cluster and aktiver_exklusiv:
            return HahnLockEntscheidung(
                erlaubt=False,
                grund=(
                    f"hahn_cluster '{eigen.cluster}' exklusiv blockiert "
                    f"(aktive Zone exklusiv): aktive Zonen {list(aktive_zonen)} "
                    f"verhindern Mitstart."
                ),
                aktive_zonen=aktive_zonen,
                verbrauch_aktuell_lpm=aktiver_verbrauch,
                verbrauch_neu_lpm=eigen.verbrauch_lpm,
                budget_lpm=budget,
            )

        gesamt = aktiver_verbrauch + eigen.verbrauch_lpm
        if gesamt > budget + 1e-9:  # Float-Toleranz
            return HahnLockEntscheidung(
                erlaubt=False,
                grund=(
                    f"hahn_cluster '{eigen.cluster}' belegt: aktuell "
                    f"{aktiver_verbrauch:.2f} L/min, neu "
                    f"+{eigen.verbrauch_lpm:.2f} L/min = {gesamt:.2f} > "
                    f"Budget {budget:.2f} L/min."
                ),
                aktive_zonen=aktive_zonen,
                verbrauch_aktuell_lpm=aktiver_verbrauch,
                verbrauch_neu_lpm=eigen.verbrauch_lpm,
                budget_lpm=budget,
            )
        return HahnLockEntscheidung(
            erlaubt=True,
            verbrauch_aktuell_lpm=aktiver_verbrauch,
            verbrauch_neu_lpm=eigen.verbrauch_lpm,
            budget_lpm=budget,
        )

    # --- Zustandserhebung -------------------------------------------------

    async def _aktive_kanaele(
        self, jetzt: datetime, ausser: tuple[str, int],
        frager: Kanalprofil | None = None,
    ) -> tuple[list[AktiverKanal], bool]:
        """Vereinigungsmenge der drei Quellen, ohne den eigenen Kanal.

        `frager` ist das Profil des Kanals, der fragt. Es entscheidet mit
        darueber, ob eine wartende Pre-Soak-Sequenz als belegend zaehlt
        (T-0552, siehe unten).

        Returns `(aktive, db_fehler)`. `db_fehler=True` heisst: die
        Ereignis-Sicht war nicht lesbar, der Zustand ist also unvollstaendig.
        """
        gefunden: dict[tuple[str, int], AktiverKanal] = {}
        db_fehler = False

        for geraet_id, sicherung in self._sicherungen.items():
            hole = getattr(sicherung, "aktive_kanaele", None)
            if hole is None:
                continue
            try:
                kanaele = hole()
            except Exception:
                logger.exception("hahn_arbiter.engine_zustand_fehler",
                                 geraet=geraet_id)
                continue
            for kanal, zone_ids in kanaele.items():
                schluessel = (geraet_id, int(kanal))
                if schluessel == ausser:
                    continue
                gefunden[schluessel] = AktiverKanal(
                    geraet_id=geraet_id, kanal=int(kanal),
                    zone_ids=tuple(zone_ids), quelle="engine",
                )

        try:
            offene = await self._offene_aus_ereignissen(jetzt)
        except Exception:
            logger.exception("hahn_arbiter.ereignis_zustand_fehler")
            offene = []
            db_fehler = True
        for eintrag in offene:
            schluessel = (eintrag.geraet_id, eintrag.kanal)
            if schluessel == ausser or schluessel in gefunden:
                continue
            if schluessel not in self._profile:
                logger.warning(
                    "hahn_arbiter.unbekannter_kanal_aktiv",
                    geraet=eintrag.geraet_id, kanal=eintrag.kanal,
                    zonen=list(eintrag.zone_ids),
                    hinweis="Kanal laeuft, ist aber in keiner Zone konfiguriert "
                            "-- blockt exklusive Starter, faellt aus der "
                            "Volumen-Rechnung.",
                )
            gefunden[schluessel] = eintrag

        try:
            pre_soak_zonen = await laufende_pre_soak_zonen(
                self._speicher, jetzt=jetzt,
            )
        except Exception:
            logger.exception("hahn_arbiter.pre_soak_zustand_fehler")
            pre_soak_zonen = set()
        for zone_id in pre_soak_zonen:
            schluessel = self._zone_zu_kanal.get(zone_id)
            if schluessel is None or schluessel == ausser or schluessel in gefunden:
                continue
            profil = self._profile.get(schluessel) or Kanalprofil()
            # T-0552: Wer darf eine WARTENDE Sequenz verdraengen?
            #
            # Bis hierher galt: nur exklusive Sequenzen reservieren den Hahn
            # ueber ihre Soak-Pause hinweg, alles andere wurde uebergangen.
            # Begruendung war, dass eine 25-min-Pause den Hahn nicht ohne
            # physischen Grund sperren soll. Fuer die Frage "darf X PARALLEL
            # zu Y laufen" ist das richtig -- nur wurde derselbe Filter fuer
            # eine zweite Frage benutzt, die er nicht beantwortet: "darf ein
            # Starter eine wartende Sequenz VERDRAENGEN". Dort ist die Antwort
            # fast immer nein, denn der Starter blockiert danach genau die
            # Hauptdose, auf die die Sequenz wartet.
            #
            # Realfall 29.08.2026: waldblumenhain wurde um 18:02:43 korrekt
            # abgewiesen (Ventile von K2 offen) und kam um 18:08:19 durch,
            # vierzig Sekunden nach deren Schliessen. bambuswald, yogaraum und
            # hecke verloren dadurch ihre Hauptdose. Dasselbe am 15.08. --
            # das war Lauf 8 aus T-0549.
            #
            # Die Ausnahme, und sie ist Betreiber-Entscheidung (Andre,
            # 29.08.): **wer ein Zeitfenster hat, das heute verfaellt, geht
            # vor.** Ein Sprinkler muss vor der Nacht laufen, sonst ist die
            # Gelegenheit weg; Mikrodrip kann jederzeit nachholen, ihm fehlt
            # nur Zeit, nicht die Gelegenheit.
            #
            # Bewusst an `zeitfenster_gebunden` und NICHT an `exklusiv`
            # gehaengt, obwohl heute dieselben Zonen beides tragen. Das ist
            # ein Zufall der Anlage: `exklusiv` ist Hydraulik,
            # `zeitfenster_gebunden` ist Agronomie. Wer hier das falsche Feld
            # liest, bekommt die richtige Antwort nur so lange, wie die
            # Konfiguration zufaellig passt.
            if not profil.exklusiv:
                frager_hat_fenster = bool(
                    frager is not None and frager.zeitfenster_gebunden
                )
                if frager_hat_fenster and not profil.zeitfenster_gebunden:
                    logger.warning(
                        "hahn_arbiter.sequenz_weicht_zeitfenster",
                        wartend=list(profil.zone_ids),
                        weicht_fuer=list(frager.zone_ids),
                        hinweis="Die wartende Sequenz verliert dadurch ihre "
                                "Hauptdose und muss spaeter neu ansetzen. "
                                "Gewollt: das Zeitfenster verfaellt, die "
                                "Mikrodrip-Gabe nicht (T-0552).",
                    )
                    continue
                # Sonst gilt die Reservierung: die wartende Sequenz behaelt
                # den Hahn ueber ihre Soak-Pause.
            gefunden[schluessel] = AktiverKanal(
                geraet_id=schluessel[0], kanal=schluessel[1],
                zone_ids=profil.zone_ids, quelle="pre_soak",
            )

        return list(gefunden.values()), db_fehler

    async def _offene_aus_ereignissen(self, jetzt: datetime) -> list[AktiverKanal]:
        hole = getattr(self._speicher, "hole_offene_ventil_kanaele", None)
        if hole is None:
            # Alte Speicher-Attrappen (Tests) ohne den Accessor: Ereignis-Sicht
            # entfaellt, aber das ist KEIN Fehlerfall -- kein fail-closed.
            return []
        zeilen = await hole(seit=jetzt - self._max_offen, bis=jetzt)
        aktive: list[AktiverKanal] = []
        for zeile in zeilen:
            zerlegt = _parse_ventil_id(str(zeile.get("ventil_id", "")))
            if zerlegt is None:
                continue
            geraet_id, kanal = zerlegt
            aktive.append(AktiverKanal(
                geraet_id=geraet_id, kanal=kanal,
                zone_ids=tuple(zeile.get("zone_ids") or ()),
                quelle="ereignis",
            ))
        return aktive

    # --- Hilfen -----------------------------------------------------------

    def _zonen_namen(self, aktive) -> tuple[str, ...]:
        namen: list[str] = []
        for a in aktive:
            profil = self._profile.get((a.geraet_id, a.kanal))
            quelle = profil.zone_ids if profil and profil.zone_ids else a.zone_ids
            for zid in quelle or (f"{a.geraet_id}:{a.kanal}",):
                if zid not in namen:
                    namen.append(zid)
        return tuple(namen)

    def _summe_lpm(self, aktive, cluster: str | None) -> float:
        """Summe der bekannten Verbraeuche im selben Cluster."""
        summe = 0.0
        for a in aktive:
            profil = self._profile.get((a.geraet_id, a.kanal))
            if profil is None or profil.verbrauch_lpm is None:
                continue
            if cluster is not None and profil.cluster != cluster:
                continue
            summe += profil.verbrauch_lpm
        return summe
