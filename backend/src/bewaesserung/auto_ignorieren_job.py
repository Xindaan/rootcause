"""T-0250: Periodischer Job, der manuell-/watchdog-Events im
`ml_ausschluss_fenster` automatisch auf `ignoriert` flippt.

Hintergrund (Realfall hecke 24./25.05.):
- User nutzt einen Ventilkanal temporaer fuer einen FREMD-Zweck
  (Hecke-Kanal beregnet eine Gras-Aussaat-Flaeche, nicht die Hecke).
- Die Live-Pipeline (Gardena-WebSocket) traegt die Events als
  `ausloser=manuell` ein, ohne den Konfig-Hintergrund zu kennen.
- `ml_ausschluss_fenster` filtert NUR ML-Training -- nicht Bilanz,
  Wirkungsrate, Frontend-Anzeige. Konsequenz: hunderte Phantom-Liter
  auf dem Hecke-Konto.

Loesung: opt-in pro Fenster via `events_auto_ignorieren: True`.
Der Job laeuft 1×/10 min, flippt manuell/watchdog -> ignoriert. Andere
Fenster (z.B. waldblumenhain Phase 3 mit echtem User-Guss zwischen
Sensor-Drama-Phasen) bleiben unberuehrt.

Idempotent: zweiter Lauf flippt nichts mehr (Filter `ausloser IN
('manuell','watchdog')` greift dann ins Leere).

Atomar pro Zone+Fenster: ein UPDATE-Statement, retry'd ueber
`_mit_lock_retry`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from bewaesserung.modelle import MlAusschlussFenster
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

SCAN_INTERVALL = timedelta(minutes=10)


class AutoIgnorierenJob:
    """T-0250: Auto-Flip manuell/watchdog -> ignoriert in opt-in
    `ml_ausschluss_fenster`-Eintraegen."""

    def __init__(
        self,
        speicher: Speicher,
        fenster_liste: list[MlAusschlussFenster],
        scan_intervall: timedelta = SCAN_INTERVALL,
    ) -> None:
        self._speicher = speicher
        # Nur Fenster mit Opt-In behalten.
        self._aktive_fenster: list[MlAusschlussFenster] = [
            f for f in fenster_liste if f.events_auto_ignorieren
        ]
        self._scan_intervall = scan_intervall
        self._letzter_scan: datetime | None = None

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> int:
        """Periodic-Trigger-Wrapper. Cadence: SCAN_INTERVALL (10 min)."""
        jetzt = jetzt or datetime.now()
        if (
            self._letzter_scan is not None
            and jetzt - self._letzter_scan < self._scan_intervall
        ):
            return 0
        self._letzter_scan = jetzt
        return await self.aktualisiere(jetzt=jetzt)

    async def aktualisiere(self, jetzt: datetime | None = None) -> int:
        """Eigentlicher Scan: gesamt-Anzahl geflipter Events."""
        jetzt = jetzt or datetime.now()
        if not self._aktive_fenster:
            return 0

        gesamt = 0
        for fenster in self._aktive_fenster:
            # Nur Fenster bearbeiten die noch aktiv sind (jetzt im
            # `[von, bis]`-Intervall, oder gerade abgelaufen aber noch
            # mit Catch-Up-Bedarf bis `bis`).
            bis_effektiv = min(jetzt, fenster.bis)
            if bis_effektiv < fenster.von:
                # Fenster liegt komplett in der Zukunft -> noch nichts
                # zu flippen.
                continue
            try:
                n = await self._speicher.flippe_events_zu_ignoriert(
                    zone_id=fenster.zone_id,
                    von=fenster.von,
                    bis=bis_effektiv,
                )
            except Exception:
                logger.exception(
                    "auto_ignorieren.fehler",
                    zone_id=fenster.zone_id,
                    von=fenster.von.isoformat(timespec="minutes"),
                    bis=bis_effektiv.isoformat(timespec="minutes"),
                )
                continue
            if n > 0:
                logger.info(
                    "auto_ignorieren.geflipt",
                    zone_id=fenster.zone_id,
                    anzahl=n,
                    von=fenster.von.isoformat(timespec="minutes"),
                    bis=bis_effektiv.isoformat(timespec="minutes"),
                )
                gesamt += n

        if gesamt > 0:
            logger.info("auto_ignorieren.gesamt", anzahl=gesamt)
        return gesamt
