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
`ausloser=WATCHDOG` und `details`-Vermerk geschrieben.

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
)
from bewaesserung.speicher import Speicher

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
        for zone_id, zone in self._zonen_konfig.items():
            try:
                gesamt += await self._scan_zone(zone_id, zone, jetzt)
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

            schliessen_zeit = ereignis.zeitstempel + max_dauer
            dauer_s = int(max_dauer.total_seconds())
            await self._speicher.speichere_ventil_ereignis(VentilEreignis(
                zeitstempel=schliessen_zeit,
                zone_id=zone_id,
                ventil_id=ereignis.ventil_id,
                aktion=VentilAktion.SCHLIESSEN,
                dauer_sekunden=dauer_s,
                ausloser=Ausloser.WATCHDOG,
            ))
            logger.warning(
                "orphan_close.synthetisches_schliessen",
                zone_id=zone_id,
                oeffnen_zeit=ereignis.zeitstempel.isoformat(timespec="minutes"),
                schliessen_zeit=schliessen_zeit.isoformat(timespec="minutes"),
                dauer_s=dauer_s,
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
                dauer_s = int(
                    (jetzt - ereignis.zeitstempel).total_seconds()
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
        return n
