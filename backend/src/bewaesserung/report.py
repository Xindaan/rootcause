"""T-0038: Wochen-Report per iMessage.

Baut jeden Sonntag 20:00 eine Text-Zusammenfassung der vergangenen 7 Tage
und sendet sie via `Benachrichtiger` an den konfigurierten Empfaenger.

Inhalt:
  * Bewaesserungen pro Zone (Anzahl + geschaetzte Liter)
  * Niederschlag + Verdunstung (Summe aus wetter_archiv, pro Standort)
  * ML-Drift (MAE pro Horizont aus ml_vorhersage_log)
  * Aktive Warnungen (Sensor + Wetter)

Idempotent pro ISO-Woche: auch bei mehreren Triggern pro Stunde wird
maximal eine Nachricht gesendet.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from bewaesserung.benachrichtigung import Benachrichtiger
from bewaesserung.bilanz import ereignis_zu_liter, ist_wasser_ereignis
from bewaesserung.modelle import (
    GesamtKonfig,
    KEINE_WASSER_AUSLOESER,
    VentilAktion,
    WochenReportKonfig,
)
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()


async def baue_wochen_report(
    speicher: Speicher,
    konfig: GesamtKonfig,
    fenster_ende: datetime,
) -> str:
    """Baut den Report-Text fuer das 7-Tage-Fenster bis `fenster_ende`.

    `fenster_ende` ist typischerweise `jetzt` beim Trigger; der Report
    deckt `jetzt - 7d` bis `jetzt` ab. Gibt einen mehrzeiligen String
    zurueck (mit `\\n`-Separatoren — iMessage rendert die als Zeilenumbruch,
    siehe `Benachrichtiger._sende_imessage`).
    """
    fenster_start = fenster_ende - timedelta(days=7)
    zeilen: list[str] = []
    iso_woche = fenster_ende.isocalendar()
    zeilen.append(
        f"Gardena-Wochen-Report "
        f"KW{iso_woche.week:02d}/{iso_woche.year} "
        f"({fenster_start.strftime('%d.%m.')}–{fenster_ende.strftime('%d.%m.')})"
    )
    zeilen.append("")

    # === Bewaesserungen pro Zone ===
    ereignisse = await speicher.hole_ventil_ereignisse_fenster(
        von=fenster_start, bis=fenster_ende,
    )
    # Gruppieren nach zone_id; nur SCHLIESSEN-Events (dort ist dauer+liter final).
    # UNBEKANNT-Events werden separat gezaehlt und als "indikativ" markiert,
    # weil sie noch nicht vom User klassifiziert sind.
    zone_zu_events: dict[str, list] = {}
    for e in ereignisse:
        # T-0566: dieselbe Regel wie in `bilanz.py`. Der harte
        # SCHLIESSEN-Filter verlor das Einzel-OEFFNEN aus
        # `POST /api/giessen` -- der Wochen-Report zeigte dieses Wasser
        # schlicht nicht.
        if not ist_wasser_ereignis(e):
            continue
        zone_zu_events.setdefault(e.zone_id, []).append(e)

    bew_zeilen: list[str] = []
    for zone in konfig.zonen:
        events = zone_zu_events.get(zone.zone_id, [])
        if not events:
            continue
        bestaetigte = [e for e in events if e.ausloser not in KEINE_WASSER_AUSLOESER]
        unbestaetigte = [e for e in events if e.ausloser in KEINE_WASSER_AUSLOESER]
        liter_summe = sum(
            (ereignis_zu_liter(e, zone, konfig.bilanz) or 0.0) for e in bestaetigte
        )
        teile = [f"{len(bestaetigte)}x / {liter_summe:.0f}L"]
        if unbestaetigte:
            teile.append(f"+{len(unbestaetigte)} indikativ")
        bew_zeilen.append(f"  {zone.name}: {', '.join(teile)}")

    if bew_zeilen:
        zeilen.append("Bewaesserungen:")
        zeilen.extend(bew_zeilen)
    else:
        zeilen.append("Bewaesserungen: keine")
    zeilen.append("")

    # === Wetter-Bilanz (pro Standort) ===
    wetter_zeilen: list[str] = []
    gesehen: set[str] = set()
    for standort in konfig.standorte:
        wid = standort.wetter_standort
        if not wid or wid in gesehen:
            continue
        gesehen.add(wid)
        archiv = await speicher.hole_wetter_archiv(
            wid, von=fenster_start, bis=fenster_ende,
        )
        if not archiv:
            continue
        regen = sum(a.niederschlag_mm for a in archiv)
        et0 = sum(a.et0_mm for a in archiv)
        wetter_zeilen.append(f"  {standort.name}: {regen:.1f} mm Regen, {et0:.1f} mm ET0")
    if wetter_zeilen:
        zeilen.append("Wetter:")
        zeilen.extend(wetter_zeilen)
        zeilen.append("")

    # === ML-Drift (alle Zonen zusammen, ohne zone_id-Filter) ===
    metriken = await speicher.hole_drift_metriken(
        zone_id=None, fenster_tage=7, jetzt=fenster_ende,
    )
    if metriken:
        ml_teile = []
        for h in (6, 12, 24):
            if h in metriken:
                ml_teile.append(f"{h}h MAE {metriken[h]['mae']:.1f}%")
        if ml_teile:
            zeilen.append("ML-Drift (7d):")
            zeilen.append("  " + ", ".join(ml_teile))
            zeilen.append("")

    # === Warnungen ===
    sensor_warn = await speicher.hole_sensor_warnungen(von=fenster_start)
    # hole_wetter_ereignisse akzeptiert nur `von` — wir filtern `bis`
    # in Python, weil der Zeitraum selten mehr als ~1000 Eintraege hat.
    wetter_warn_alle = await speicher.hole_wetter_ereignisse(von=fenster_start)
    wetter_warn = [e for e in wetter_warn_alle if e.zeitstempel <= fenster_ende]
    offene_sensor = [w for w in sensor_warn if w.behoben_um is None]
    if offene_sensor or wetter_warn:
        zeilen.append("Warnungen:")
        if offene_sensor:
            zeilen.append(f"  Sensor offen: {len(offene_sensor)}")
        if wetter_warn:
            zeilen.append(f"  Wetter-Ereignisse: {len(wetter_warn)}")
    else:
        zeilen.append("Warnungen: keine")

    return "\n".join(zeilen)


class WochenReportJob:
    """Zeit-getriggerter Job — feuert einmal pro ISO-Woche zur Konfig-Zeit."""

    def __init__(
        self,
        speicher: Speicher,
        konfig: GesamtKonfig,
        report_konfig: WochenReportKonfig,
        benachrichtiger: Benachrichtiger,
    ) -> None:
        self._speicher = speicher
        self._konfig = konfig
        self._report = report_konfig
        self._benachrichtiger = benachrichtiger
        # ISO-Wochen-String "YYYY-Wxx" der letzten erfolgreich gesendeten
        # Nachricht. Verhindert Doppel-Sends im 60-s-Loop-Takt innerhalb
        # der Trigger-Stunde. In-memory reicht — ein Service-Restart
        # zwischen 20:00 und 20:59 koennte einmal doppelt senden, das ist
        # akzeptabel (sehr seltener Fall).
        self._gesendet_woche: str | None = None

    def _effektiver_empfaenger(self) -> str:
        """Wenn `wochen_report.empfaenger` leer ist, fallback auf den
        ersten Zone-Benachrichtigungs-Empfaenger — User hat denselben
        iMessage-Kontakt auch schon fuer die Giess-Erinnerungen
        konfiguriert, doppelte Eingabe waere Fehlerquelle.
        """
        if self._report.empfaenger:
            return self._report.empfaenger
        for zone in self._konfig.zonen:
            if zone.benachrichtigung and zone.benachrichtigung.empfaenger:
                return zone.benachrichtigung.empfaenger
        return ""

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> bool:
        """Prueft ob Trigger-Zeit erreicht + Woche noch nicht gesendet. True = gesendet."""
        if not self._report.aktiv:
            return False
        empfaenger = self._effektiver_empfaenger()
        if not empfaenger:
            return False
        jetzt = jetzt or datetime.now()
        if jetzt.weekday() != self._report.tag_der_woche:
            return False
        if jetzt.hour != self._report.stunde:
            return False
        iso_kw = jetzt.isocalendar()
        woche_key = f"{iso_kw.year}-W{iso_kw.week:02d}"
        if self._gesendet_woche == woche_key:
            return False

        try:
            text = await baue_wochen_report(self._speicher, self._konfig, jetzt)
        except Exception:
            logger.exception("wochen_report.baue_fehler")
            return False

        erfolg = await self._benachrichtiger.sende_text(empfaenger, text)
        if erfolg:
            self._gesendet_woche = woche_key
            logger.info(
                "wochen_report.gesendet",
                woche=woche_key,
                zeichen=len(text),
                empfaenger=empfaenger[:6] + "...",
            )
            return True
        logger.warning("wochen_report.senden_fehlgeschlagen", woche=woche_key)
        return False
