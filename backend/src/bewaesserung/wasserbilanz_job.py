"""T-0422: taeglicher Shadow-Lauf der FAO-56-Bilanz.

**Was der Job tut.** Einmal taeglich pro Zone die Auszehrung `Dr`
fortschreiben (ET0, Regen, Bewaesserung des Vortags) und daneben schreiben,
was die ECHTE Engine an diesem Tag entschieden hat. Ueber mehrere Wochen wird
damit belegbar, ob die Bilanz frueher, spaeter oder gleich entscheidet wie die
Sensor-Schwelle.

**Er entscheidet nichts.** Kein Ventil, keine Empfehlung, kein Push. Der
einzige Zweck ist die Vergleichsreihe fuer die spaetere Entscheidung.

**Warum ueberhaupt Shadow und nicht direkt live** (Andres Grundsatz vom
23.07., Memory `feedback_sensor_schlaegt_berechnung`): Sensoren sind das Mass
der Wirksamkeit, nicht die Rechnung. Die Bilanz beruht auf Annahmen --
Bodenart, Wurzeltiefe, benetzte Flaeche, Kc -- die alle still falsch sein
koennen. Genau das ist beim Bau zweimal passiert: erst mit der Ballenflaeche
(46 mm je Gabe = doppelter Bodenspeicher), davor bei der Bambus-Dosis mit
zwei verschiedenen falschen Nennern. Beide Rechnungen liefen widerspruchslos
durch; nur der Abgleich mit dem Sensor hat sie widerlegt.

**Bezugsflaeche pro Zone.** Tropf-Zonen rechnen ueber die BENETZTE Flaeche
(aus der Tropferzahl), Sprinkler-Zonen ueber `flaeche_m2`. Das ist der
T-0422-Variante-2-Fix -- bei Mikrodrip ist `flaeche_m2` die Ballenflaeche
und als mm-Nenner physikalisch falsch.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from .bilanz import ereignis_zu_liter
from .wasserbilanz import (
    benetzte_flaeche_m2,
    liter_zu_mm,
    schreibe_fort,
    speicher_fuer_zone,
)

logger = structlog.get_logger()


def bezugsflaeche_m2(zone) -> float | None:
    """Die Flaeche, auf die das Wasser dieser Zone wirklich geht.

    Tropf -> benetzter Streifen aus der Tropferzahl.
    Sprinkler (keine Tropferzahl) -> die Zonenflaeche.
    """
    n = getattr(zone, "tropfer_anzahl", None)
    if n:
        return benetzte_flaeche_m2(n)
    return getattr(zone, "flaeche_m2", None)


class WasserbilanzJob:
    """Schreibt die Bilanz taeglich fort. Shadow, ohne Nebenwirkung."""

    INTERVALL_STUNDEN = 24

    def __init__(self, speicher, zonen, konfig) -> None:
        self._speicher = speicher
        self._zonen = zonen
        self._konfig = konfig
        self._letzter_lauf: datetime | None = None

    async def _tages_input(
        self, zone, standort_id: str, von: datetime, bis: datetime,
    ) -> tuple[float, float, float]:
        """(et0_mm, regen_mm, bewaesserung_mm) fuer den Zeitraum.

        ET0/Regen kommen aus `hole_forecast_stunden` -- die Funktion nimmt pro
        Vorhersage-Stunde die JUENGSTE Abfrage. Ohne diese Dedup summierte man
        alle Modelllaeufe desselben Tages auf (gemessen: 109,79 statt
        3,27 mm ET0, Faktor 33).
        """
        fc = await self._speicher.hole_forecast_stunden(standort_id, von, bis)
        et0 = sum(e for _, (r, e) in fc.items())
        regen = sum(r for _, (r, e) in fc.items())

        ereignisse = await self._speicher.hole_ventil_ereignisse(
            zone.zone_id, von=von, bis=bis,
        )
        liter = 0.0
        for e in ereignisse:
            if e.aktion.value != "schliessen":
                continue
            # Nur echtes Wasser -- `ignoriert` sind Fremd-Laeufe auf
            # geteilten Kanaelen (Grass-Regime), die diese Zone nicht giessen.
            if e.ausloser.value == "ignoriert":
                continue
            liter += ereignis_zu_liter(e, zone, self._konfig.bilanz) or 0.0

        return et0, regen, liter_zu_mm(liter, bezugsflaeche_m2(zone))

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> int:
        jetzt = jetzt or datetime.now()
        if self._letzter_lauf is not None:
            verstrichen = (jetzt - self._letzter_lauf).total_seconds()
            if verstrichen < self.INTERVALL_STUNDEN * 3600:
                return 0
        self._letzter_lauf = jetzt

        von = (jetzt - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        bis = von + timedelta(days=1)
        geschrieben = 0

        for zone in self._zonen:
            if bezugsflaeche_m2(zone) is None:
                continue  # Topf-/Indoor-Zonen ohne Flaeche
            # ACHTUNG: `wetter_standort` sitzt auf StandortKonfig, NICHT auf
            # ZonenKonfig -- der getattr liefert deshalb immer None und der
            # Default greift immer. Folgenlos, solange nur Freiland-Zonen
            # hierher kommen (Topf/Indoor werden oben per bezugsflaeche_m2
            # uebersprungen) und die alle am selben Standort liegen. Der
            # Default kommt jetzt aus der Konfig statt hartkodiert.
            standort = (
                getattr(zone, "wetter_standort", None)
                or (self._konfig.standorte[0].wetter_standort
                    if getattr(self._konfig, "standorte", None) else "")
                or "standard"
            )
            try:
                et0, regen, bew_mm = await self._tages_input(
                    zone, standort, von, bis,
                )
                vorher = await self._speicher.hole_letzten_bilanz_zustand(
                    zone.zone_id,
                )
                dr_alt = float(vorher["dr_mm"]) if vorher else 0.0
                speicher = speicher_fuer_zone()
                schritt = schreibe_fort(dr_alt, et0, regen, bew_mm, speicher)

                # Was die ECHTE Engine an dem Tag getan hat -- der
                # Vergleichspunkt. Ohne ihn ist die Reihe wertlos.
                ist = bew_mm > 0
                await self._speicher.speichere_bilanz_zustand(
                    zone.zone_id, bis, schritt,
                    wuerde_giessen=schritt.giessen,
                    ist_entscheidung=ist,
                    quelle="shadow",
                )
                geschrieben += 1
                logger.info(
                    "wasserbilanz.fortgeschrieben",
                    zone_id=zone.zone_id, tag=von.strftime("%Y-%m-%d"),
                    dr_mm=round(schritt.dr_nachher_mm, 1),
                    raw_mm=round(speicher.raw_mm, 1),
                    et0_mm=round(et0, 1), regen_mm=round(regen, 1),
                    bewaesserung_mm=round(bew_mm, 1),
                    bilanz_wuerde_giessen=schritt.giessen,
                    engine_hat_gegossen=ist,
                    # Der interessante Fall fuer die spaetere Auswertung.
                    uneinig=schritt.giessen != ist,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "wasserbilanz.fehler", zone_id=zone.zone_id,
                )
        return geschrieben
