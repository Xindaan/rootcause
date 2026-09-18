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

from .bilanz import ereignis_zu_liter, ist_wasser_ereignis
from .modelle import wetterstandort_je_zone
from .wasserbilanz import (
    benetzte_flaeche_m2,
    entscheidung_mit_ensemble,
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

    async def _ensemble_urteil(
        self, standort_id: str, dr_mm: float, speicher, tag_von: datetime,
        et0_tag_mm: float,
    ) -> tuple[bool | None, float | None, float | None]:
        """T-0423-Regel auswerten: `Dr + ET0 - Regen_p20 > RAW`?

        Rueckgabe `(giessen, p20_mm, et0_prognose_mm)`, alle None wenn kein
        Ensemble-Abruf zum Tag vorliegt. Bewusst None und nicht `False`/0.0 --
        "keine Aussage" ist etwas anderes als "kein Regen", und die spaetere
        Auswertung (T-0425) muss das unterscheiden koennen.

        Der p20 kommt aus dem 24-h-Horizont: die Regel fragt, ob die Zone in
        den naechsten 24 h unter RAW faellt, also ist das der passende
        Horizont. Die Tabelle fuehrt daneben 48 h -- welcher besser
        entscheidet, ist Teil von T-0425 und wird hier nicht vorweggenommen.
        """
        try:
            zeilen = await self._speicher.hole_regen_ensemble(
                standort_id=standort_id, limit=200,
            )
        except Exception:  # noqa: BLE001
            logger.exception("wasserbilanz.ensemble_lesefehler",
                             standort=standort_id)
            return None, None, None

        # Der BILANZTAG, nicht `bis`: `bis` ist Mitternacht des Folgetags,
        # der Wissensstand gehoert aber zum Tag, den der Schritt abdeckt.
        tag = tag_von.strftime("%Y-%m-%d")
        passend = [
            z for z in zeilen
            if z.get("horizont_stunden") == 24
            and str(z.get("abfrage_zeitstempel", "")).startswith(tag)
        ]
        if not passend:
            return None, None, None
        # Der letzte Abruf des Tages -- der Wissensstand, mit dem die Engine
        # am Abend entschieden haette.
        letzter = max(passend, key=lambda z: z["abfrage_zeitstempel"])
        p20 = letzter.get("p20")
        if p20 is None:
            return None, None, None

        # ET0 der kommenden 24 h. Es gibt dafuer keine eigene Groesse im Job,
        # deshalb dient der ET0 DES TAGES als Naeherung -- ET0 ist von Tag zu
        # Tag stark autokorreliert. Das ist eine Annahme, keine Messung, und
        # sie wird deshalb als eigene Spalte MITGESCHRIEBEN statt still in das
        # Urteil verrechnet: T-0425 kann so nachrechnen, wie empfindlich das
        # Ergebnis darauf reagiert. Ein 0.0-Default waere hier der schlimmere
        # Fehler -- er nimmt der Regel den gesamten Austrocknungs-Term.
        et0_prognose = float(et0_tag_mm or 0.0)

        giessen, _dr_prognose = entscheidung_mit_ensemble(
            dr_mm, et0_prognose, float(p20), speicher,
        )
        return giessen, float(p20), et0_prognose

    async def _tages_input(
        self, zone, standort_id: str, von: datetime, bis: datetime,
    ) -> tuple[float, float, float, bool]:
        """(et0_mm, regen_mm, bewaesserung_mm, engine_hat_entschieden).

        ET0/Regen kommen aus `hole_forecast_stunden` -- die Funktion nimmt pro
        Vorhersage-Stunde die JUENGSTE Abfrage. Ohne diese Dedup summierte man
        alle Modelllaeufe desselben Tages auf (gemessen: 109,79 statt
        3,27 mm ET0, Faktor 33).

        T-0490 (Audit A2): das vierte Element ist neu und der Grund fuer die
        Signatur-Aenderung. "Wasser floss" und "die Engine hat entschieden"
        sind zwei verschiedene Aussagen -- `bewaesserung_mm` summiert JEDE
        zaehlende Quelle (Zeitplan, App, AquaBloom), nur `automatik` ist die
        Engine. Gemessen am 02.08.: bambuswald, bambuswald_yogaraum und hecke
        standen fuer den 01.08. auf `ist_entscheidung=1`, obwohl saemtliche
        Events dieses Tages `ausloser=zeitplan` waren.
        """
        fc = await self._speicher.hole_forecast_stunden(standort_id, von, bis)
        et0 = sum(e for _, (r, e) in fc.items())
        regen = sum(r for _, (r, e) in fc.items())

        ereignisse = await self._speicher.hole_ventil_ereignisse(
            zone.zone_id, von=von, bis=bis,
        )
        liter = 0.0
        engine = False
        for e in ereignisse:
            # T-0566: dieselbe Regel wie in `bilanz.py`. Der harte
            # SCHLIESSEN-Filter verlor das Einzel-OEFFNEN aus
            # `POST /api/giessen`; die FAO-56-Reihe rechnete dieses Wasser
            # nicht mit.
            if not ist_wasser_ereignis(e):
                continue
            # Nur echtes Wasser -- `ignoriert` sind Fremd-Laeufe auf
            # geteilten Kanaelen (Grass-Regime), die diese Zone nicht giessen.
            if e.ausloser.value == "ignoriert":
                continue
            if e.ausloser.value == "automatik":
                engine = True
            liter += ereignis_zu_liter(e, zone, self._konfig.bilanz) or 0.0

        return et0, regen, liter_zu_mm(liter, bezugsflaeche_m2(zone)), engine

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

        # T-0488: EINE Wahrheit fuer zone_id -> wetter_standort. Vorher stand
        # hier ein `getattr(zone, "wetter_standort")` mit dem Kommentar, der
        # Default sei folgenlos, weil nur Freiland-Zonen durch den
        # Flaechenfilter kaemen. Das stimmte nicht: `bezugsflaeche_m2` liefert
        # auch fuer die Balkon-Toepfe zitrus (0.05 m2), mandevilla (0.05) und
        # kasten_4 (0.10) einen Wert, und die drei liegen am Suedbalkon mit
        # einem anderen `wetter_standort`. Sie wurden also mit dem Wetter des
        # Gartenstandorts
        # bilanziert (Audit A1, 02.08.).
        standorte = getattr(self._konfig, "standorte", None)
        standort_je_zone = wetterstandort_je_zone(standorte)
        standard_standort = (
            (standorte[0].wetter_standort or standorte[0].standort_id)
            if standorte else "standard"
        )

        for zone in self._zonen:
            if bezugsflaeche_m2(zone) is None:
                continue  # Topf-/Indoor-Zonen ohne Flaeche
            standort = standort_je_zone.get(zone.zone_id) or standard_standort
            if zone.zone_id not in standort_je_zone:
                # Zone in keinem Standort-Block -- Bilanz rechnet dann mit
                # fremdem Wetter weiter. Nicht still lassen.
                logger.warning(
                    "wasserbilanz.zone_ohne_standort",
                    zone_id=zone.zone_id,
                    ersatz_standort=standort,
                )
            try:
                et0, regen, bew_mm, engine = await self._tages_input(
                    zone, standort, von, bis,
                )
                vorher = await self._speicher.hole_letzten_bilanz_zustand(
                    zone.zone_id,
                )
                # T-0563: Idempotenz pro Tag. `_letzter_lauf` ist reiner
                # Prozess-Zustand ([[fehlerpattern_datei_statt_prozesszustand]]),
                # nach jedem Neustart also leer -- und weil die Zeile auf
                # `bis` geschrieben wird und `hole_letzten_bilanz_zustand`
                # die JUENGSTE liefert, las der zweite Lauf seine eigene
                # Ausgabe als Vorzustand. `INSERT OR REPLACE` ueberschrieb
                # dieselbe Zeile mit einem weiteren ET0-Tag: Dr waechst,
                # ohne dass eine Zeile dazukommt.
                #
                # Nachgerechnet (6 mm ET0/Tag, RAW 9,0): Dr nach Lauf
                # 1/2/3/4 = 6,0 / 12,0 / 18,0 / 22,5 mm -- `wuerde_giessen`
                # kippt allein durch Neustarts von 0 auf 1. An der Live-DB
                # sind 23 Tageszeilen exakte Vielfache der Tagesdifferenz.
                # Laptop-Schlaf ist hier Normalbetrieb
                # ([[betriebsform_laptop_schlaeft]]), Neustarts sind es also
                # auch.
                if vorher and str(vorher.get("zeitstempel")) == bis.isoformat():
                    logger.debug(
                        "wasserbilanz.tag_bereits_geschrieben",
                        zone_id=zone.zone_id, tag=bis.isoformat(),
                    )
                    continue
                dr_alt = float(vorher["dr_mm"]) if vorher else 0.0
                speicher = speicher_fuer_zone()
                schritt = schreibe_fort(dr_alt, et0, regen, bew_mm, speicher)

                # Was die ECHTE Engine an dem Tag getan hat -- der
                # Vergleichspunkt. Ohne ihn ist die Reihe wertlos.
                #
                # T-0490: frueher stand hier `bew_mm > 0`, also "es floss
                # Wasser". Das ist eine andere Aussage: Zeitplan-, App- und
                # AquaBloom-Wasser zaehlen in `bew_mm` mit, sind aber keine
                # Engine-Entscheidung. Die Shadow-Reihe soll die Bilanz gegen
                # die ENGINE vergleichen, nicht gegen den Wasserhahn.
                ist = engine

                # T-0423-Konsument (05.08.2026): die Ensemble-Regel
                # auswerten, nicht nur die Verteilung sammeln.
                # `entscheidung_mit_ensemble` existiert samt Tests seit Juli,
                # wurde aber ausschliesslich aus Tests gerufen -- damit war
                # T-0425 ("entscheidet der p20 besser?") gar nicht
                # beantwortbar, obwohl die Daten seit 23.07. laufen.
                #
                # WEITERHIN SHADOW. Der Wert wird neben die echte
                # Entscheidung geschrieben, nicht an ihre Stelle. Die
                # Freigabe haengt an T-0425 und ist eine eigene Entscheidung.
                ens_giessen, p20, et0_prog = await self._ensemble_urteil(
                    standort, schritt.dr_nachher_mm, speicher, von, et0,
                )

                await self._speicher.speichere_bilanz_zustand(
                    zone.zone_id, bis, schritt,
                    wuerde_giessen=schritt.giessen,
                    ist_entscheidung=ist,
                    quelle="shadow",
                    wuerde_giessen_ensemble=ens_giessen,
                    regen_p20_mm=p20,
                    et0_prognose_24h_mm=et0_prog,
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
                    ensemble_wuerde_giessen=ens_giessen,
                    regen_p20_mm=None if p20 is None else round(p20, 2),
                    engine_hat_gegossen=ist,
                    # Der interessante Fall fuer die spaetere Auswertung.
                    uneinig=schritt.giessen != ist,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "wasserbilanz.fehler", zone_id=zone.zone_id,
                )
        return geschrieben
