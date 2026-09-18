"""T-0050b + T-0196: Cache FYTA-Plant-Optimum-Ranges fuer mehrere Achsen.

Laeuft periodisch (Default 24 h), holt fuer jede in `konfig.fyta.pflanzen`
konfigurierte Pflanze ALLE messbaren Optimum-Schwellen aus der FYTA-Detail-
API (`plant.measurements.{moisture,light,temperature,salinity}`) und schreibt
sie in zwei Tabellen:

1. **`plant_optimum`** (T-0050b, moisture-only): Backward-Kompat fuer
   `schwellen_vorschlag.py` und alle bestehenden Konsumenten. Nur die
   Feuchte-Werte werden hier gespiegelt.
2. **`plant_optimum_achse`** (T-0196, Multi-Achse EAV): Neue Tabelle pro
   (zone_id, achse). Achsen: feuchte, licht_ppfd, licht_dli, temperatur,
   salinitaet. Quelle fuer Chart-Optimum-Baender und Push-Heuristiken.

**Verwendung Feuchte-Pfad**: Der T-0049-Vorschlag (`schwellen_vorschlag.py`)
liest Optimum-Werte pro Zone zuerst aus `zone.optimum_feuchte_*` (Config-
Override hat Vorrang, weil User-Wissen spezifischer ist als FYTA-Default),
dann aus dem Cache (`plant_optimum`).

**Gilt nur fuer FYTA-Zonen**: Die Werte sind auf der FYTA-Sensor-Skala
kalibriert, nicht auf der Gardena-Skala. Fuer Gardena-Zonen (waldblumenhain,
bambuswald, bambuswald_yogaraum) waere der Wert nicht aussagekraeftig —
deshalb werden nur Zonen mit FYTA-Zuordnung gescannt.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from bewaesserung.fyta_client import FytaClient
from bewaesserung.modelle import GesamtKonfig
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()


class PlantOptimumJob:
    """Periodisch (Default 24 h): FYTA-Plant-Optimum pro Zone cachen."""

    def __init__(
        self,
        speicher: Speicher,
        konfig: GesamtKonfig,
        fyta_client: FytaClient | None,
        intervall_stunden: int = 24,
    ) -> None:
        self._speicher = speicher
        self._konfig = konfig
        self._client = fyta_client
        self._intervall = timedelta(hours=intervall_stunden)
        self._letzte_aktualisierung: datetime | None = None

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> bool:
        """Laeuft hoechstens einmal pro Intervall. True wenn gelaufen."""
        if self._client is None or self._konfig.fyta is None:
            return False
        if not self._konfig.fyta.pflanzen:
            return False
        jetzt = jetzt or datetime.now()
        if (self._letzte_aktualisierung
                and jetzt - self._letzte_aktualisierung < self._intervall):
            return False

        gesamt = 0
        erfolge_feuchte = 0
        erfolge_achsen = 0
        for pflanze in self._konfig.fyta.pflanzen:
            gesamt += 1
            try:
                optima = await self._client.hole_plant_optima_alle_achsen(
                    pflanze.fyta_id,
                )
            except Exception:
                logger.exception(
                    "plant_optimum.abruf_fehler",
                    zone_id=pflanze.zone_id, fyta_id=pflanze.fyta_id,
                )
                continue
            if optima is None:
                continue

            # 1) Backward-Kompat: Feuchte separat in plant_optimum-Tabelle
            #    (T-0050b-Verbraucher lesen weiter aus dieser Tabelle).
            feuchte = optima.get("feuchte")
            if feuchte and feuchte.get("min_good") is not None \
                    and feuchte.get("max_good") is not None:
                try:
                    await self._speicher.speichere_plant_optimum(
                        zone_id=pflanze.zone_id,
                        feuchte_min=feuchte["min_good"],
                        feuchte_max=feuchte["max_good"],
                        feuchte_min_akzeptabel=feuchte.get("min_akzeptabel"),
                        feuchte_max_akzeptabel=feuchte.get("max_akzeptabel"),
                        quelle="fyta",
                    )
                    erfolge_feuchte += 1
                except Exception:
                    logger.exception(
                        "plant_optimum.speicher_fehler_feuchte",
                        zone_id=pflanze.zone_id,
                    )

            # 2) T-0196: Alle Achsen in plant_optimum_achse (Multi-Achse EAV).
            #    T-0196d: current-Wert aus FYTA values.current uebernehmen
            #    (Salinity, Temperatur, Feuchte, Licht-PPFD). FYTA liefert
            #    fuer licht_dli kein current — der wird gleich aus den
            #    sensor_messung-PPFD-Werten der letzten 24 h aggregiert.
            for achse, werte in optima.items():
                try:
                    await self._speicher.speichere_plant_optimum_achse(
                        zone_id=pflanze.zone_id,
                        achse=achse,
                        einheit=werte["einheit"],
                        min_good=werte.get("min_good"),
                        max_good=werte.get("max_good"),
                        min_akzeptabel=werte.get("min_akzeptabel"),
                        max_akzeptabel=werte.get("max_akzeptabel"),
                        current=werte.get("current"),
                        quelle="fyta",
                    )
                    erfolge_achsen += 1
                except Exception:
                    logger.exception(
                        "plant_optimum.speicher_fehler_achse",
                        zone_id=pflanze.zone_id, achse=achse,
                    )

            # 3) T-0196e: DLI-Tagesaggregat (mol/day) aus den letzten 24 h
            #    PPFD-Werten der Zone. Naeherung: avg(licht) * 86400s / 1e6.
            #    Speichern als current bei achse=licht_dli, FALLS die DLI-
            #    Achse vorhin geliefert wurde (sonst keine Schwellen zum
            #    Vergleichen).
            if "licht_dli" in optima:
                try:
                    dli = await self._berechne_dli_tagesaggregat(
                        pflanze.zone_id, jetzt,
                    )
                except Exception:
                    logger.exception(
                        "plant_optimum.dli_aggregat_fehler",
                        zone_id=pflanze.zone_id,
                    )
                    dli = None
                if dli is not None:
                    try:
                        await self._speicher.speichere_plant_optimum_achse(
                            zone_id=pflanze.zone_id,
                            achse="licht_dli",
                            einheit=optima["licht_dli"]["einheit"],
                            min_good=optima["licht_dli"].get("min_good"),
                            max_good=optima["licht_dli"].get("max_good"),
                            min_akzeptabel=optima["licht_dli"].get("min_akzeptabel"),
                            max_akzeptabel=optima["licht_dli"].get("max_akzeptabel"),
                            current=dli,
                            quelle="aggregat",
                        )
                    except Exception:
                        logger.exception(
                            "plant_optimum.dli_speicher_fehler",
                            zone_id=pflanze.zone_id,
                        )

        self._letzte_aktualisierung = jetzt
        if erfolge_feuchte > 0 or erfolge_achsen > 0:
            logger.info(
                "plant_optimum.aktualisiert",
                erfolge_feuchte=erfolge_feuchte,
                erfolge_achsen=erfolge_achsen,
                gesamt_pflanzen=gesamt,
            )
        return True

    async def _berechne_dli_tagesaggregat(
        self, zone_id: str, jetzt: datetime,
    ) -> float | None:
        """T-0196e: DLI (Daily Light Integral) aus PPFD-Stundenwerten.

        FYTA-Sensoren liefern `licht` als PPFD (Photosynthetic Photon
        Flux Density) in μmol·m⁻²·s⁻¹ (laut Plant-Detail-API als
        'μmol/h' bezeichnet — die FYTA-Einheits-Angabe ist
        irrefuehrend, der numerische Wert entspricht der Standard-
        PPFD-Skala).

        DLI in mol/(m²·day) = sum_over_day(PPFD_i × Δt_i) / 1e6, wobei
        Δt_i die Sekunden zwischen Messung i und i+1 sind.

        Naeherung hier (24 h gleitendes Fenster statt Kalender-Tag,
        Trapez-Regel ueber Zeitdeltas): summiere mean(PPFD_i, PPFD_{i+1})
        × Δt_i. Bei dichten 15-min-Cadence (FYTA Beam) ist der
        Diskretisierungsfehler vernachlaessigbar.

        Returns: DLI in mol/day, oder None wenn weniger als 2 PPFD-Werte
        im 24-h-Fenster.
        """
        von = jetzt - timedelta(hours=24)
        messungen = await self._speicher.hole_messungen(zone_id, von=von, bis=jetzt)
        ppfd_werte = [(m.zeitstempel, m.licht) for m in messungen if m.licht is not None]
        if len(ppfd_werte) < 2:
            return None
        # Chronologisch sortieren (hole_messungen liefert DESC, Trapez
        # braucht aufsteigende Zeit).
        ppfd_werte.sort(key=lambda x: x[0])
        integral_mol = 0.0
        for i in range(len(ppfd_werte) - 1):
            t1, p1 = ppfd_werte[i]
            t2, p2 = ppfd_werte[i + 1]
            dt_s = (t2 - t1).total_seconds()
            if dt_s <= 0 or dt_s > 7200:
                # Sicherheits-Cap: Luecken >2 h nicht ueber Trapez glaetten,
                # sonst wuerden Sensor-Ausfaelle zu falschen DLI fuehren.
                continue
            integral_mol += (p1 + p2) / 2.0 * dt_s / 1e6
        return integral_mol
