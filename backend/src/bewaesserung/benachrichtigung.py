"""iMessage-Benachrichtigungen fuer Giess-Erinnerungen.

Sendet Nachrichten via osascript (AppleScript) an iMessage.
Cooldown-Logik verhindert Spam (max 1 Nachricht pro Zone pro Zeitraum).
"""

import asyncio
import subprocess
from datetime import datetime, timedelta

import structlog

from bewaesserung.modelle import ZonenKonfig, ZonenModus

logger = structlog.get_logger()


class Benachrichtiger:
    """Sendet iMessage-Erinnerungen wenn Monitoring-Zonen zu trocken sind."""

    def __init__(self):
        self._letzte_nachricht: dict[str, datetime] = {}  # zone_id -> zeitstempel

    async def pruefe_und_sende(
        self,
        zone: ZonenKonfig,
        aktuelle_feuchte: float | None,
    ) -> None:
        """Prueft ob eine Zone eine Benachrichtigung braucht und sendet sie.

        Args:
            zone: Zonenkonfiguration (muss Monitoring-Modus + Benachrichtigung haben).
            aktuelle_feuchte: Letzte gemessene Bodenfeuchte in Prozent.
        """
        if zone.modus != ZonenModus.MONITORING:
            return
        if zone.benachrichtigung is None:
            return
        if not zone.benachrichtigung.empfaenger:
            logger.debug("benachrichtigung.kein_empfaenger", zone=zone.zone_id)
            return
        if aktuelle_feuchte is None:
            return

        # Feuchte ueber Schwelle — alles gut
        if aktuelle_feuchte >= zone.feuchte_schwelle_min:
            return

        # Cooldown pruefen
        jetzt = datetime.now()
        letzte = self._letzte_nachricht.get(zone.zone_id)
        cooldown = timedelta(hours=zone.benachrichtigung.cooldown_stunden)
        if letzte and (jetzt - letzte) < cooldown:
            verbleibend = cooldown - (jetzt - letzte)
            logger.debug(
                "benachrichtigung.cooldown",
                zone=zone.zone_id,
                verbleibend_h=f"{verbleibend.total_seconds() / 3600:.1f}",
            )
            return

        # Nachricht senden
        text = (
            f"Giess-Erinnerung {zone.name}: "
            f"Bodenfeuchte bei {aktuelle_feuchte:.0f}% "
            f"(Schwelle: {zone.feuchte_schwelle_min:.0f}%). "
            f"Bitte giessen!"
        )

        erfolg = await self._sende_imessage(zone.benachrichtigung.empfaenger, text)
        if erfolg:
            self._letzte_nachricht[zone.zone_id] = jetzt
            logger.info(
                "benachrichtigung.gesendet",
                zone=zone.zone_id,
                feuchte=f"{aktuelle_feuchte:.0f}%",
                empfaenger=zone.benachrichtigung.empfaenger[:6] + "...",
            )
        else:
            logger.error("benachrichtigung.fehlgeschlagen", zone=zone.zone_id)

    async def sende_prognose(
        self,
        zone: ZonenKonfig,
        zeitpunkt: datetime,
        grund: str,
    ) -> None:
        """Sendet eine proaktive Prognose-Nachricht (z.B. 'Morgen giessen').

        Nutzt denselben Cooldown wie regulaere Benachrichtigungen.
        """
        if zone.modus != ZonenModus.MONITORING:
            return
        if zone.benachrichtigung is None or not zone.benachrichtigung.empfaenger:
            return

        jetzt = datetime.now()
        letzte = self._letzte_nachricht.get(zone.zone_id)
        cooldown = timedelta(hours=zone.benachrichtigung.cooldown_stunden)
        if letzte and (jetzt - letzte) < cooldown:
            return

        # Zeitpunkt formatieren
        if zeitpunkt.date() == jetzt.date():
            wann = f"heute gegen {zeitpunkt.strftime('%H:%M')}"
        elif zeitpunkt.date() == (jetzt + timedelta(days=1)).date():
            wann = f"morgen gegen {zeitpunkt.strftime('%H:%M')}"
        else:
            wann = zeitpunkt.strftime("%d.%m. gegen %H:%M")

        text = (
            f"Prognose {zone.name}: "
            f"Voraussichtlich {wann} Giessen noetig. "
            f"({grund})"
        )

        erfolg = await self._sende_imessage(zone.benachrichtigung.empfaenger, text)
        if erfolg:
            self._letzte_nachricht[zone.zone_id] = jetzt
            logger.info(
                "benachrichtigung.prognose_gesendet",
                zone=zone.zone_id,
                wann=wann,
            )

    async def sende_text(self, empfaenger: str, text: str) -> bool:
        """Oeffentlicher Wrapper um `_sende_imessage` — fuer Reports/Jobs,
        die den Cooldown nicht brauchen und direkt einen Text verschicken
        wollen (z.B. `WochenReportJob`). Gibt True bei Erfolg zurueck.
        """
        return await self._sende_imessage(empfaenger, text)

    async def _sende_imessage(self, empfaenger: str, text: str) -> bool:
        """Sendet eine iMessage via osascript (AppleScript).

        Multi-line-Support: Newlines im `text` werden als AppleScript-
        `return` konkateniert, doppelte Anfuehrungszeichen werden escaped.
        Empfaenger wird ebenfalls escaped, damit fremde Eingabe kein
        Script-Injection-Vector ist.
        """
        def escape_str(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        zeilen = text.split("\n")
        zeilen_escaped = [f'"{escape_str(z)}"' for z in zeilen]
        text_expression = ' & return & '.join(zeilen_escaped)
        empfaenger_escaped = escape_str(empfaenger)

        script = f'''
        tell application "Messages"
            set targetService to 1st account whose service type = iMessage
            set targetBuddy to participant "{empfaenger_escaped}" of targetService
            send {text_expression} to targetBuddy
        end tell
        '''

        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error(
                    "imessage.osascript_fehler",
                    returncode=proc.returncode,
                    stderr=stderr.decode().strip(),
                )
                return False

            return True

        except FileNotFoundError:
            logger.error("imessage.osascript_nicht_gefunden")
            return False
        except Exception as exc:
            logger.error("imessage.fehler", fehler=str(exc))
            return False
