"""T-0210 Orphan-Close-Job — synthetisches SCHLIESSEN fuer verpasste Events.

Wenn der WebSocket-Live-Stream waehrend Backend-Restart oder Reconnect
das SCHLIESSEN-Event eines laufenden Ventils verpasst, bleibt das
OEFFNEN-Event ohne Pendant in der DB stehen. Symptom (Realfall 18.05.
09:38): Bambus-Bewaesserung 09:08-09:38 (30 min, manuell via Gardena-
App), Backend-Restart 09:22, SCHLIESSEN-Event verloren.

Wirkung ohne Fix:
- `VentilSicherung._aktiv` haengt -> kann zu Phantom-Lock-Konflikten
  beim Hahn-Cluster fuehren ("Bambus ist noch offen!", obwohl es zu
  ist)
- Bilanz verzerrt: kein SCHLIESSEN -> keine Dauer/Liter zugewiesen
- Watchdog kann blind bleiben

Diese Klasse scannt periodisch nach OEFFNEN-Events, die laenger als
`max_dauer_sekunden + grace_minuten` ohne SCHLIESSEN-Pendant in der
DB stehen. Pro Treffer wird ein synthetisches SCHLIESSEN-Event mit
`ausloser=WATCHDOG` geschrieben; seit T-0595 traegt es den Herkunftsvermerk
`orphan_zeitbasiert` in `korrektur_log` (vorher gab es entgegen diesem
Docstring keinen Vermerk). Es ist KEIN Hardware-Beleg "Ventil zu".

T-0595 (HW-6): Laeufe, die eine VentilSicherung noch in `_aktiv` fuehrt,
bleiben unberuehrt -- dort entscheidet die Sicherung (Close-Retry, Heilung,
echter Callback). Die synthetische Dauer ist die kommandierte aus
`live_lauf_state`, wenn der Lauf dort steht, sonst `max_dauer`.

**Atomar pro Zone**: scannt + schreibt pro Zone in einer Schleife,
keine Cross-Zone-Locks.

**Idempotent**: zweiter Lauf findet das OEFFNEN nicht mehr als
orphan, weil das synthetische SCHLIESSEN jetzt da steht.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from bewaesserung.modelle import (
    Ausloser,
    VentilAktion,
    VentilEreignis,
    ZonenKonfig,
    verstrichen_s_gespeichert,
)
from bewaesserung.speicher import HERKUNFT_ORPHAN_ZEITBASIERT, Speicher

logger = structlog.get_logger()

# Aktuell laufende Bewaesserung max. 2 h (max_dauer_sekunden in
# ZonenKonfig liegt bei den meisten Zonen bei 1800-3600 s). Wir nehmen
# einen Sicherheits-Grace von 30 min: erst nach OEFFNEN_ALTER >
# max_dauer + GRACE wird ein Event als orphan gewertet. Damit verhindern
# wir, dass wir mitten in einem regulaeren Lauf reinpfuschen.
ORPHAN_GRACE = timedelta(minutes=30)
# Fallback-max-Dauer, falls eine Zone keinen Konfig-Eintrag hat.
DEFAULT_MAX_DAUER = timedelta(hours=2)
# Tick-Cadence (vom Aufrufer geprueft via `aktualisiere_wenn_faellig`).
SCAN_INTERVALL = timedelta(minutes=10)
# T-0595: wie weit OEFFNEN-Zeitstempel und Laufstart (`gestartet`) einer
# VentilSicherung bzw. von `live_lauf_state` auseinanderliegen duerfen, um
# denselben Lauf zu meinen. Das OEFFNEN wird nach dem Cloud-Kommando
# geschrieben, also wenige Sekunden NACH dem Start.
LAUF_ZUORDNUNG_TOLERANZ = timedelta(seconds=120)


class OrphanCloseJob:
    """T-0210: Schreibt synthetisches SCHLIESSEN fuer hangende OEFFNEN."""

    def __init__(
        self,
        speicher: Speicher,
        zonen: list[ZonenKonfig],
        scan_intervall: timedelta = SCAN_INTERVALL,
    ) -> None:
        self._speicher = speicher
        self._zonen_konfig: dict[str, ZonenKonfig] = {
            z.zone_id: z for z in zonen
        }
        self._scan_intervall = scan_intervall
        self._letzter_scan: datetime | None = None
        # T-0595: geraet_id -> VentilSicherung. Leer = Verhalten wie vorher
        # (Tests, Betrieb ohne Ventilsteuerung).
        self._ventil_sicherungen: dict = {}

    def setze_ventil_sicherungen(self, ventil_sicherungen: dict) -> None:
        """T-0595: Sicherungen, deren laufende Laeufe der Scan nicht anfasst.

        Bewusst ein Setter: in `main.ausfuehren` entsteht der Job vor den
        Sicherungen. Die Referenz auf das Dict bleibt bestehen, spaeter
        hinzugefuegte Geraete sind also mit drin."""
        self._ventil_sicherungen = ventil_sicherungen

    def _lauf_in_sicherung(self, ereignis: VentilEreignis) -> bool:
        """T-0595: fuehrt eine VentilSicherung den Lauf dieses OEFFNEN noch?"""
        for sicherung in list(self._ventil_sicherungen.values()):
            try:
                if sicherung.fuehrt_lauf_zum_oeffnen(
                    ereignis.ventil_id, ereignis.zeitstempel,
                    toleranz=LAUF_ZUORDNUNG_TOLERANZ,
                ):
                    return True
            except Exception:
                # Im Zweifel nicht synthetisch schliessen: ein haengender
                # Lauf schadet weniger als ein falscher Beleg "Ventil zu".
                logger.exception(
                    "orphan_close.sicherung_abfrage_fehler",
                    ventil_id=ereignis.ventil_id,
                )
                return True
        return False

    @staticmethod
    def _kommandierte_dauer_s(
        ereignis: VentilEreignis, live_states: list[dict],
    ) -> int | None:
        """T-0595: kommandierte Dauer des Laufs aus `live_lauf_state`."""
        for st in live_states:
            ventil = st.get("valve_id") or st.get("geraet_id")
            if ventil != ereignis.ventil_id:
                continue
            if ereignis.zone_id not in (st.get("zone_ids") or ()):
                continue
            gestartet = st.get("gestartet_am")
            if gestartet is None or abs(
                ereignis.zeitstempel - gestartet
            ) > LAUF_ZUORDNUNG_TOLERANZ:
                continue
            dauer = int(st.get("dauer_sekunden") or 0)
            if dauer > 0:
                return dauer
        return None

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> int:
        """Periodic-Trigger-Wrapper analog BackupJob/AquabloomJob."""
        jetzt = jetzt or datetime.now()
        if (
            self._letzter_scan is not None
            and jetzt - self._letzter_scan < self._scan_intervall
        ):
            return 0
        self._letzter_scan = jetzt
        return await self.aktualisiere(jetzt=jetzt)

    async def aktualisiere(self, jetzt: datetime | None = None) -> int:
        """Eigentlicher Scan — gesamt-Anzahl synthetisierter SCHLIESSEN."""
        jetzt = jetzt or datetime.now()
        gesamt = 0
        live_states: list[dict] = []
        hole_states = getattr(self._speicher, "hole_live_lauf_states", None)
        if callable(hole_states):
            try:
                live_states = await hole_states()
            except Exception:
                logger.exception("orphan_close.live_state_lesefehler")
        for zone_id, zone in self._zonen_konfig.items():
            try:
                gesamt += await self._scan_zone(
                    zone_id, zone, jetzt, live_states,
                )
            except Exception:
                logger.exception(
                    "orphan_close.zone_fehler", zone_id=zone_id,
                )
        if gesamt > 0:
            logger.info(
                "orphan_close.synthetische_events", anzahl=gesamt,
            )
        return gesamt

    async def _scan_zone(
        self, zone_id: str, zone: ZonenKonfig, jetzt: datetime,
        live_states: list[dict] | None = None,
    ) -> int:
        max_dauer = timedelta(
            seconds=zone.max_dauer_sekunden,
        ) if zone.max_dauer_sekunden else DEFAULT_MAX_DAUER
        cutoff = jetzt - max_dauer - ORPHAN_GRACE
        orphans = await self._speicher.hole_orphan_oeffnen(
            zone_id=zone_id, cutoff=cutoff, suchfenster=max_dauer + ORPHAN_GRACE,
        )
        if not orphans:
            return 0

        n = 0
        for ereignis in orphans:
            # Heuristik-Pseudo-Paare sind selbst-paarend; bei denen
            # ist OEFFNEN ohne SCHLIESSEN ein Datenfehler, nicht der
            # T-0210-Fall (WS-Reconnect). Wir lassen sie in Ruhe --
            # die kommen aus `sensor_backfill.py` und schreiben ihr
            # Paar atomar pro Lauf.
            if ereignis.ventil_id == "sensor_heuristik":
                continue

            # T-0595 (HW-6): ein Lauf, den eine VentilSicherung noch fuehrt,
            # ist nicht verwaist. Realfall 24.09.: drei gescheiterte Closes,
            # dann schrieb der Scan 5400 s, und der Close-Fehlerpfad wertete
            # das als "Ventil zu" -- obwohl niemand die Hardware gefragt hatte.
            if self._lauf_in_sicherung(ereignis):
                logger.info(
                    "orphan_close.lauf_in_sicherung_uebersprungen",
                    zone_id=zone_id, ventil_id=ereignis.ventil_id,
                    oeffnen_zeit=ereignis.zeitstempel.isoformat(
                        timespec="minutes",
                    ),
                )
                continue

            dauer_s = int(max_dauer.total_seconds())
            kommandiert = self._kommandierte_dauer_s(ereignis, live_states or [])
            if kommandiert is not None:
                # Laenger kommandiert als max_dauer (Handlauf bis 180 min,
                # Pre-Soak-Haupt): laut Kommando kann der Lauf noch laufen ->
                # in dieser Runde nicht anfassen.
                if ereignis.zeitstempel + timedelta(
                    seconds=kommandiert,
                ) + ORPHAN_GRACE > jetzt:
                    continue
                dauer_s = kommandiert
            schliessen_zeit = ereignis.zeitstempel + timedelta(seconds=dauer_s)
            await self._speicher.speichere_ventil_ereignis(
                VentilEreignis(
                    zeitstempel=schliessen_zeit,
                    zone_id=zone_id,
                    ventil_id=ereignis.ventil_id,
                    aktion=VentilAktion.SCHLIESSEN,
                    dauer_sekunden=dauer_s,
                    ausloser=Ausloser.WATCHDOG,
                ),
                herkunft=HERKUNFT_ORPHAN_ZEITBASIERT,
            )
            logger.warning(
                "orphan_close.synthetisches_schliessen",
                zone_id=zone_id,
                oeffnen_zeit=ereignis.zeitstempel.isoformat(timespec="minutes"),
                schliessen_zeit=schliessen_zeit.isoformat(timespec="minutes"),
                dauer_s=dauer_s,
                dauer_quelle=(
                    "live_lauf_state" if kommandiert is not None else "max_dauer"
                ),
                ventil_id=ereignis.ventil_id,
                ausloser_oeffnen=ereignis.ausloser.value,
            )
            n += 1
        return n

    async def sync_aus_live_state(
        self,
        live_state: dict[str, str],
        jetzt: datetime | None = None,
    ) -> int:
        """T-0210-Folge: Reconnect-Sync mit Gardena-Live-State.

        Anders als `aktualisiere()` (periodisch, max_dauer + grace):
        hier vergleichen wir Echtzeit -- wenn die Cloud-API gerade
        sagt "Ventil zu", aber die DB hat OEFFNEN ohne SCHLIESSEN,
        dann ist ein Event verlorengegangen. Wir schreiben
        synthetisches SCHLIESSEN mit `jetzt` als Zeitstempel und
        Dauer = `jetzt - oeffnen_zeit` (best-effort -- die echte
        Schluss-Zeit haben wir verpasst).

        `live_state` ist `zone_id -> "offen" | "zu" | "unbekannt"`.
        Wird vom `GardenaClient._live_state_pro_zone()` gebaut.

        Suchfenster: 4 h ab OEFFNEN (lang genug fuer den
        Realfall 30-min-Bewaesserung + Reconnect-Verspaetung; kurz
        genug um keine alten OEFFNEN-Events anzufassen).
        """
        if not live_state:
            return 0
        jetzt = jetzt or datetime.now()
        suchfenster = timedelta(hours=4)
        # Cutoff = jetzt (alles OEFFNEN bis JETZT ist potentiell
        # offen). Eigentliche Filterung geschieht ueber den
        # Live-State pro Zone.
        n = 0
        for zone_id, status in live_state.items():
            if status != "zu":
                continue  # Live sagt offen oder unbekannt -> nicht eingreifen
            if zone_id not in self._zonen_konfig:
                continue  # Zonen ohne Konfig nicht anfassen
            zone = self._zonen_konfig[zone_id]
            max_dauer_s = (
                int(zone.max_dauer_sekunden)
                if zone.max_dauer_sekunden
                else int(DEFAULT_MAX_DAUER.total_seconds())
            )
            dauer_cap_s = min(max_dauer_s, 3600)
            orphans = await self._speicher.hole_orphan_oeffnen(
                zone_id=zone_id, cutoff=jetzt, suchfenster=suchfenster,
            )
            for ereignis in orphans:
                if ereignis.ventil_id == "sensor_heuristik":
                    continue
                # T-0616 (aus T-0595 AK2): einen Lauf, den eine Sicherung
                # noch in `_aktiv` fuehrt, schliesst sie selbst (Watchdog,
                # Callback, Heilung) -- mit dem Deckel `dauer + puffer`
                # (T-0572). Ein Sync-SCHLIESSEN mit `jetzt - OEFFNEN` umginge
                # ihn und zaehlte als Beleg, sodass der richtige Wert danach
                # als Doppel verworfen wuerde. Fail-Richtung: der Lauf gilt
                # bis dahin weiter als offen.
                if self._lauf_in_sicherung(ereignis):
                    logger.info(
                        "orphan_close.reconnect_sync_lauf_in_sicherung",
                        zone_id=zone_id, ventil_id=ereignis.ventil_id,
                        oeffnen_zeit=ereignis.zeitstempel.isoformat(
                            timespec="minutes",
                        ),
                    )
                    continue
                # T-0593b: UTC statt naiver Wanduhr. Ein OEFFNEN vor der
                # Umstellung (z. B. 01:50 Sommerzeit), synchronisiert in der
                # Winterzeit, bekam naiv 1 h zu wenig; im Maerz 1 h zu viel.
                # Ein Stempel aus der Wiederholungsstunde bleibt mehrdeutig
                # (fold verloren), s. `verstrichen_s_gespeichert`.
                dauer_s = int(
                    verstrichen_s_gespeichert(ereignis.zeitstempel, jetzt)
                )
                if dauer_s <= 0:
                    continue
                dauer_s = min(dauer_s, dauer_cap_s)
                # T-0408-Isomorphie (20.07.): das `suchfenster` oben ist fix
                # 4 h AB OEFFNEN, der Close wird aber bei `jetzt` geschrieben.
                # Liegt das OEFFNEN laenger als 4 h zurueck (Backend-Downtime,
                # spaeter Reconnect), faellt die EIGENE Schreibung aus dem
                # Pendant-Fenster -> der naechste Reconnect sieht denselben
                # Orphan erneut und schreibt nach. Empirisch: bei 5 h Abstand
                # erzeugen 3 Reconnects 3 Closes, bei 52 min genau einen.
                # Gleiche Klasse wie der T-0408-Duplikat-Sturm (Fenster am
                # falschen Punkt verankert). Guard mit OFFENEM Fenster ab dem
                # OEFFNEN -- zone-scoped, damit bei geteilten Ventilen nicht
                # der Close einer Geschwisterzone faelschlich zaehlt.
                if await self._speicher.existiert_schliessen_seit(
                    ereignis.ventil_id, ereignis.zeitstempel, [zone_id],
                ):
                    continue
                await self._speicher.speichere_ventil_ereignis(VentilEreignis(
                    zeitstempel=jetzt,
                    zone_id=zone_id,
                    ventil_id=ereignis.ventil_id,
                    aktion=VentilAktion.SCHLIESSEN,
                    dauer_sekunden=dauer_s,
                    ausloser=Ausloser.WATCHDOG,
                ))
                logger.warning(
                    "orphan_close.reconnect_sync_close",
                    zone_id=zone_id,
                    oeffnen_zeit=ereignis.zeitstempel.isoformat(timespec="minutes"),
                    schliessen_zeit=jetzt.isoformat(timespec="minutes"),
                    dauer_s=dauer_s,
                    ventil_id=ereignis.ventil_id,
                )
                n += 1
        await self._sync_zu_bestaetigung(live_state, jetzt)
        n += await self._sync_offene_aus_live_state(live_state, jetzt)
        return n

    async def _sync_zu_bestaetigung(
        self, live_state: dict[str, str], jetzt: datetime,
    ) -> None:
        """T-0625 (Gate): Cloud "zu" beendet eine fruehere Offen-Bestaetigung.

        Der Close-Nachtrag oben greift nur fuer ungepaarte OEFFNEN. Hat der
        Orphan-Scan schon ein geschaetztes SCHLIESSEN geschrieben, gilt der
        Lauf dort als gepaart, und es entsteht nichts -- die Bestaetigung aus
        einem frueheren Reconnect hielte den Kanal dann bis Meldung + MAX_OFFEN
        belegt, obwohl die Cloud zu meldet (Gate: offen@160/zu@200 belegt bis
        311). Der Vermerk ist kein Ereignis und kein Herkunftsvermerk; die
        Bilanz bleibt beim geschaetzten bzw. spaeter echten Close."""
        vermerke = getattr(self._speicher, "vermerke_cloud_zu", None)
        if vermerke is None:
            return
        erledigt: set[str] = set()
        for zone_id, status in live_state.items():
            if status != "zu" or zone_id not in self._zonen_konfig:
                continue
            zone = self._zonen_konfig[zone_id]
            if zone.ventil_kanal is None or not zone.ventil_geraet_id:
                continue
            ventil_id = f"{zone.ventil_geraet_id}:{zone.ventil_kanal}"
            if ventil_id in erledigt:
                continue
            erledigt.add(ventil_id)
            try:
                if await vermerke(ventil_id, bis=jetzt):
                    logger.info(
                        "orphan_close.reconnect_sync_zu_bestaetigt",
                        zone_id=zone_id, ventil_id=ventil_id,
                        zeitpunkt=jetzt.isoformat(timespec="minutes"),
                    )
            except Exception:
                logger.exception(
                    "orphan_close.reconnect_sync_zu_fehler",
                    zone_id=zone_id, ventil_id=ventil_id,
                )

    async def _sync_offene_aus_live_state(
        self, live_state: dict[str, str], jetzt: datetime,
    ) -> int:
        """T-0559: die Gegenrichtung -- Cloud sagt OFFEN, die DB kennt nichts.

        `sync_aus_live_state` deckte bis hier nur "Cloud zu, DB offen" ab. Die
        andere Richtung fehlte, und sie ist die gefaehrlichere: **ein Lauf,
        den niemand kennt, blockiert niemanden.**

        Der Ablauf, der T-0554 erzeugt hat (29.08., bambuswald + hecke):
        waehrend die WebSocket weg ist, startet ein Guss in der Gardena-App.
        Es entsteht kein OEFFNEN-Event. Nach dem Reconnect fragt der
        `HahnArbiter` seine drei Quellen -- Engine-Zustand (kennt nur eigene
        Laeufe), Ereignis-Sicht (kein OEFFNEN da) und Pre-Soak-Zustaende --
        und alle drei sagen "Kanal frei". Die Automatik oeffnet ueber den
        laufenden Handguss. Genau das hat Andre beobachtet.

        **Korrektur eines eigenen Review-Befunds:** ich hatte den DHS-Nachtrag
        als Ursache benannt (`ventil_id='gardena_web'` faellt aus dem
        `GLOB '*:[0-9]*'`-Filter von `hole_offene_ventil_kanaele`). Der Filter
        greift wirklich, aber die Ursache ist es nicht: `_parse_events`
        verlangt `start` UND `stop`, DHS liefert also ausschliesslich
        abgeschlossene Laeufe und schreibt beide Events als Paar. Eine
        DHS-Zeile erscheint nie als offener Kanal. Waehrend eines laufenden
        Fremdgusses gibt es aus DHS schlicht nichts.

        Die Ventil-ID wird aus `ventil_geraet_id` + `ventil_kanal` gebaut.
        Das ist keine Erfindung, sondern exakt das Format, das die Gardena-
        Cloud fuer Multi-Valve-Geraete selbst vergibt (in der Live-DB
        nachgesehen: `bbbb0001-...:2`), und genau das, was
        `hahn_arbiter._parse_ventil_id` wieder zerlegt.

        **Fehlerrichtung, bewusst gewaehlt.** Ein zu Unrecht geschriebenes
        OEFFNEN sperrt den Kanal, statt ihn zu ueberfahren -- es kostet
        also Wasser, das nicht floss, und widerspricht insoweit
        [[feedback_sand_lieber_zu_viel_giessen]]. Es ist aber von selbst
        begrenzt: der Arbiter sieht offene Laeufe nur `MAX_OFFEN` lang
        (150 min), der Orphan-Scan schliesst sie nach `max_dauer + Grace`,
        und der naechste Reconnect korrigiert ueber die andere Richtung.
        Die Gegenseite -- paralleler Guss auf einem exklusiven
        Sprinkler-Kanal -- ist nicht begrenzt und nicht reversibel.
        """
        offen_gemeldet = [
            zid for zid, status in live_state.items()
            if status == "offen" and zid in self._zonen_konfig
        ]
        if not offen_gemeldet:
            return 0

        # T-0625: "kennt die DB den Lauf schon?" fragt jetzt nach dem
        # juengsten OEFFNEN des Kanals ohne echtes SCHLIESSEN (Rueckblick
        # `CLOUD_OFFEN_RUECKBLICK`, ein geschaetztes Orphan-SCHLIESSEN beendet
        # nichts). Bis T-0625 lief das ueber `hole_offene_ventil_kanaele` mit
        # 4-h-Fenster: ein bekannter Lauf 150-240 min nach dem OEFFNEN bekam
        # nichts, und der Arbiter (MAX_OFFEN 150 min) sah den Kanal frei,
        # obwohl die Cloud offen meldete. Ein zweites OEFFNEN haette das
        # behoben, aber den Lauf zerlegt: der Orphan-Scan schaetzte dann fuer
        # beide Haelften ein SCHLIESSEN, die Bilanz zaehlte doppelt. Deshalb
        # bekommt der bekannte Lauf eine Bestaetigung am OEFFNEN; sie haelt
        # den Kanal in der Ereignis-Sicht bis Meldung + MAX_OFFEN belegt.
        hole = getattr(self._speicher, "hole_bekanntes_offenes_oeffnen", None)
        if hole is None:
            return 0

        n = 0
        erledigt: set[str] = set()
        for zone_id in offen_gemeldet:
            zone = self._zonen_konfig[zone_id]
            if zone.ventil_kanal is None or not zone.ventil_geraet_id:
                continue  # kein physischer Kanal -> nichts zu sperren
            ventil_id = f"{zone.ventil_geraet_id}:{zone.ventil_kanal}"
            if ventil_id in erledigt:
                continue  # Geschwisterzone am selben Kanal
            erledigt.add(ventil_id)
            try:
                bekannt = await hole(ventil_id, bis=jetzt)
                if bekannt is not None and bekannt.id is not None:
                    await self._speicher.aktualisiere_ventil_ereignis(
                        bekannt.id, cloud_offen_bestaetigt=jetzt, jetzt=jetzt,
                    )
                    logger.info(
                        "orphan_close.reconnect_sync_offen_bestaetigt",
                        zone_id=zone_id, ventil_id=ventil_id,
                        oeffnen_zeit=bekannt.zeitstempel.isoformat(
                            timespec="minutes",
                        ),
                        zeitpunkt=jetzt.isoformat(timespec="minutes"),
                    )
                    continue  # bekannter Lauf: kein zweites OEFFNEN
            except Exception:
                logger.exception(
                    "orphan_close.live_state_offen_lesefehler",
                    zone_id=zone_id, ventil_id=ventil_id,
                )
                continue
            await self._speicher.speichere_ventil_ereignis(VentilEreignis(
                zeitstempel=jetzt,
                zone_id=zone_id,
                ventil_id=ventil_id,
                aktion=VentilAktion.OEFFNEN,
                dauer_sekunden=0,
                ausloser=Ausloser.MANUELL,
            ))
            logger.warning(
                "orphan_close.reconnect_sync_offen",
                zone_id=zone_id,
                ventil_id=ventil_id,
                zeitpunkt=jetzt.isoformat(timespec="minutes"),
                hinweis="Cloud meldet offen, die DB kannte den Lauf nicht -- "
                        "synthetisches OEFFNEN, damit der Hahn belegt ist",
            )
            n += 1
        return n
