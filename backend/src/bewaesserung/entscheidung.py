"""Regelbasierter Entscheidungsmotor fuer Bewaesserung und Prognosen."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, time, timedelta
from typing import Any

import structlog

from bewaesserung.modelle import (
    Ausloser,
    BewaesserungsStrategie,
    BilanzKonfig,
    BlockerTyp,
    BewaesserungsEntscheidung,
    EntscheidungsScope,
    GiessEmpfehlung,
    KEINE_WASSER_AUSLOESER,
    MlBewaesserungsResponseKonfig,
    MlWirkungFitKonfig,
    SchwellenAdaptionKonfig,
    SensorMessung,
    SensorWarnungTyp,
    StandortKonfig,
    VentilAktion,
    WetterStunde,
    ZonenKonfig,
    ZonenModus,
    effektiv_feuchte_kritisch,
    effektiv_optimum_min,
    effektiv_schwelle_min,
    effektiv_welkepunkt,
)
from bewaesserung.speicher import Speicher
from bewaesserung.wetter import WetterClient, WetterManager

logger = structlog.get_logger()

STANDARD_PRUEFINTERVALL = timedelta(minutes=5)
REGEN_FEUCHTE_FAKTOR = 4.0
ET0_FEUCHTE_FAKTOR = 2.0
PROGNOSE_EPSILON = 0.05

# Mindestdauer bei aktiver Giess-Empfehlung. Verhindert 0s-Events wenn
# die Feuchte zwischen Basis- und effektiver (ET0-adaptierter) Schwelle liegt.
MIN_DAUER_SEKUNDEN = 60

# T-0089: Default Sensor-Anstieg pro Bewaesserungs-Minute (Prozentpunkte).
# Wird genutzt wenn `ZonenKonfig.delta_pp_pro_minute=None`. Heutiger Wert
# 1.0 entspricht der vor T-0089 hartcodierten Heuristik. Realistische
# Pro-Zone-Werte sind via Konfig-Override deutlich niedriger (z.B. 0.17
# fuer Waldblumen-Sprinkler aus T-0067-Test 27.04.).
DEFAULT_DELTA_PP_PRO_MINUTE = 1.0

# Ausreisser-Filter (MAD). Ein einzelner Spike (Sensor-Tausch, Einschlemmen,
# Funk-Glitch) soll die Entscheidung nicht kippen. Wir vergleichen den
# aktuellsten Wert gegen Median + MAD der 4 vorherigen Messungen:
# |x - med| > 3 * MAD -> gilt als Ausreisser, Median wird verwendet.
AUSREISSER_FENSTER = 5          # aktueller Wert + 4 Historien-Werte
AUSREISSER_MAD_FAKTOR = 3.0
HISTORIE_STUNDEN = 24           # Zeitfenster fuer MAD-Referenz

# T-0098: Wenn das 24h-Fenster leer ist (Sensor-Aussetzer), nutzen wir die
# letzte gespeicherte Messung als Fallback — aber nur, wenn sie nicht zu
# alt ist. SensorHealthMonitor warnt bereits ab 3 h (Gardena) bzw. 12 h
# (FYTA); 48 h ist die obere Schranke fuer den Fallback. Darueber wird
# der Pfad als KEINE_MESSUNG behandelt, sonst kann eine Tage alte
# Messung still eine Giessempfehlung treiben.
#
# T-0383 (06.07.2026): Diese Schranke BINDET NICHT MEHR und bleibt nur als
# aeusserer Backstop stehen. Effektiver Staleness-Gate des Entscheidungspfads
# ist AGGREGAT_FALLBACK_FENSTER_MINUTEN (240 min, s.u.) -- eine Messung, die
# aelter ist, kommt gar nicht erst aus dem Speicher zurueck. Historie: der
# 48-h-Vertrag (T-0098) war seit T-0179c ohnehin faktisch tot, weil
# `_robuste_feuchte` seither ueber `letzte_messung_aggregiert` liest (90-min-
# SQL-Fenster). Nur der Test-Mock ignorierte das Fenster und hielt den
# Vertrag scheinbar gruen. Bewusst NICHT auf 48 h zurueckgedreht: der Vertrag
# stammt aus der Zeit vor `ventilsteuerung_aktiv` (26.06.2026) -- eine
# tagealte Bodenfeuchte darf kein autonomes Ventil oeffnen.
MAX_FALLBACK_ALTER_STUNDEN = 48

# T-0383: Das Aggregat-Fenster (`letzte_messung_aggregiert`, Default 90 min)
# war der FAKTISCHE, aber STILLE Staleness-Gate des Entscheidungspfads: der
# Gardena-Sensor beatet ~60-minuetlich, d.h. nur ~30 min Puffer. Nach einem
# Offline-/WS-Gap (Replay-Guard unterdrueckt die nachgespielten Beats, der
# DHS-Backfill traegt sie erst spaeter nach) altert der letzte Beat aus dem
# Fenster -> `letzte_messung_aggregiert` gab still None zurueck (der Zweig
# loggt nichts) und MAX_FALLBACK_ALTER_STUNDEN wurde nie erreicht, weil die
# Zeile schon vom SQL-Fenster gefiltert war. Realfall 06.07.: Yogaraum um
# 12:37 noch da (Beat 88.6 min alt), um 12:43 weg (94.2 min) -> Kanal fiel
# auf den ausgeschlossenen sensor-b (T-0382).
#
# Fix: findet das frische 90-min-Fenster nichts, wird EINMAL bewusst und
# GELOGGT auf diesen Horizont erweitert -- gleicher Sensor, nur aelter.
# 240 min deckt einen verpassten Beat (60) + ein mehrstuendiges Offline-
# Fenster ab. Bodenfeuchte aendert sich in 4 h vernachlaessigbar (Decay
# ~1-3 pp/Tag), eine 4 h alte Messung darf also giessen -- eine 48 h alte
# NICHT (darum bleibt MAX_FALLBACK_ALTER_STUNDEN als Backstop stehen).
AGGREGAT_FALLBACK_FENSTER_MINUTEN = 240


def _plateau_transparenz(
    wirkung_max_pp: float | None,
    wirkungsrate_initial: float | None,
    aktuelle_feuchte: float,
    ziel: float | None,
    dauer_s: int | None,
) -> tuple[float | None, float | None, int | None]:
    """T-0291: Transparenz fuer das plateau-begrenzte Dosier-Modell.

    Die empfohlene Einzeldose kann hoechstens ~0.95*wirkung_max_pp pp heben
    (Plateau-Cap in `_berechne_dauer`). Bei grossem Ziel-Abstand erreicht
    eine Dose das Ziel daher NICHT -- die flache Dauer ist kein Bug, sondern
    die Max-Einzeldose. Diese Funktion liefert die Felder, mit denen das UI
    das ehrlich zeigen kann.

    Returns `(erwarteter_endwert_pp, einzeldosis_max_pp, dosen_bis_ziel)`:
    - erwarteter_endwert_pp: Feuchte nach der Dose (aktuell + Plateau-Wirkung
      der `dauer_s`), gedeckelt auf 100.
    - einzeldosis_max_pp: was EINE Dose maximal hebt (~0.95*wmax).
    - dosen_bis_ziel: ceil(Ziel-Abstand / einzeldosis_max); 0 wenn schon am
      Ziel, >1 = plateau-begrenzt (mehrere Dosen noetig). None ohne Ziel.

    Alle None, wenn die Zone kein Plateau-Modell hat (wmax/r0 fehlen) --
    dann greift der log-Decay-Pfad und diese Transparenz ist nicht anwendbar.
    """
    import math
    wmax = wirkung_max_pp
    r0 = wirkungsrate_initial
    if not (wmax and r0 and wmax > 0 and r0 > 0):
        return None, None, None
    einzeldosis_max = round(wmax * 0.95, 1)
    endwert: float | None = None
    if dauer_s and dauer_s > 0:
        tau = wmax / r0
        wirkung = wmax * (1.0 - math.exp(-(dauer_s / 60.0) / tau))
        endwert = round(min(100.0, aktuelle_feuchte + wirkung), 1)
    dosen: int | None = None
    if ziel is not None:
        differenz = max(ziel - aktuelle_feuchte, 0.0)
        dosen = 0 if differenz <= 0 else max(1, math.ceil(differenz / (wmax * 0.95)))
    return endwert, einzeldosis_max, dosen


def _robuster_aktuellwert(werte_desc: list[float]) -> tuple[float, bool]:
    """Robuster Feuchte-Wert anhand letzter Messungen (DESC nach Zeit).

    Nutzt Median Absolute Deviation. Bei weniger als drei Historien-Werten
    oder MAD=0 (alle identisch) wird der Rohwert unveraendert zurueckgegeben —
    wir wollen keine falschen Alarme bei frischen Zonen oder konstanter Feuchte.
    """
    if not werte_desc:
        raise ValueError("werte_desc darf nicht leer sein")
    aktuell = werte_desc[0]
    historie = werte_desc[1:]
    if len(historie) < 2:
        return aktuell, False
    med = statistics.median(historie)
    abweichungen = [abs(w - med) for w in historie]
    mad = statistics.median(abweichungen)
    if mad == 0:
        return aktuell, False
    if abs(aktuell - med) > AUSREISSER_MAD_FAKTOR * mad:
        return med, True
    return aktuell, False


def _kanal_dose_ziel(
    zone: "ZonenKonfig",
    effektive_schwelle: float,
    strategie_ziel: float | None = None,
) -> float:
    """Dose-Ziel-Feuchte fuer den Auto-Loop-Kanal-Pfad (pruefe_kanal).

    T-0345: Wenn `entscheide_pro_zone` ein Strategieziel liefert, ist dieses
    die Single Source fuer die Dosis:
    HAEUFIG_KLEIN -> optimum_max, SELTEN_GROSS -> Feldkapazitaet,
    KONSTANT_NIEDRIG -> optimum_min, KORRIDOR -> Reserve/optimum_max.
    KORRIDOR wird dabei nie unter die effektive Trigger-Schwelle abgesenkt,
    weil diese Schwelle die untere Korridorgrenze des Kanalstarts ist.

    Ohne Strategie-Ziel bleibt der T-0344-Fallback fuer KORRIDOR erhalten:
    durchdringend bis `optimum_feuchte_max`, wenn gesetzt, sonst bis zur
    effektiven Min-Schwelle. Rein/testbar.
    """
    if strategie_ziel is not None:
        ziel = max(0.0, min(100.0, float(strategie_ziel)))
        if zone.bewaesserungs_strategie == BewaesserungsStrategie.KORRIDOR:
            return max(ziel, float(effektive_schwelle))
        return ziel
    if (
        zone.bewaesserungs_strategie == BewaesserungsStrategie.KORRIDOR
        and zone.optimum_feuchte_max is not None
        and zone.optimum_feuchte_max > effektive_schwelle
    ):
        return zone.optimum_feuchte_max
    return effektive_schwelle


class Entscheidungsmotor:
    """Leitet aus Feuchte, Wetter und Konfiguration eine Bewaesserungsentscheidung ab."""

    def __init__(
        self,
        speicher: Speicher,
        wetter_manager: WetterManager,
        zonen: list[ZonenKonfig],
        standorte: list[StandortKonfig] | None = None,
        schwellen_adaption: SchwellenAdaptionKonfig | None = None,
        response_konfig: MlBewaesserungsResponseKonfig | None = None,
        response_service: Any | None = None,
        bilanz_konfig: BilanzKonfig | None = None,
        ml_vorhersage_service: Any | None = None,
        konfig: Any | None = None,
        wirkung_fit_konfig: MlWirkungFitKonfig | None = None,
    ):
        self._speicher = speicher
        self._wetter_manager = wetter_manager
        self._zonen = {zone.zone_id: zone for zone in zonen}
        self._schwellen_adaption = schwellen_adaption or SchwellenAdaptionKonfig()
        # T-0065: Response-Modell (Dauer-Empfehlung). Beide optional —
        # ohne Konfig oder Service bleibt die Heuristik allein (aktiv=False).
        self._response_konfig = response_konfig or MlBewaesserungsResponseKonfig()
        self._response_service = response_service
        # T-0292 Stufe 2: Plateau-Wirkungs-Fit-Adoption. Ohne Konfig oder
        # mit `adoptieren=False` (Default) liefert `_aufgeloeste_wirkung`
        # (None, None) -> `_berechne_dauer` behaelt die Konfig-Werte
        # `zone.wirkung_max_pp`/`wirkungsrate_initial` (perfekter No-op).
        self._wirkung_fit_konfig = wirkung_fit_konfig or MlWirkungFitKonfig()
        # T-0066: Kanal-Liter-Raten fuer Liter-Umrechnung in vorhersage_zone.
        # Fehlt die Bilanz-Konfig, bleiben `liter_heuristik` etc. None.
        self._bilanz_konfig = bilanz_konfig
        # T-0105: MLVorhersageService fuer Feuchte-Prognose in der kausalen
        # Empfehlung. Wenn None oder verfuegbar=False -> Heuristik-Fallback.
        # Niederschlag/Regen ist im ML als Feature drin (vs. Heuristik mit
        # vereinfachter `regen_24h - schwelle`-Korrektur).
        self._ml_vorhersage_service = ml_vorhersage_service
        # GesamtKonfig fuer ML-live_vorhersage (braucht der Service).
        self._konfig = konfig
        # Zone → Wetter-Standort Mapping aus StandortKonfig
        self._zone_standort: dict[str, str] = {}
        for s in (standorte or []):
            wetter_id = s.wetter_standort or s.standort_id
            for z in s.zonen:
                self._zone_standort[z] = wetter_id

    def setze_ml_vorhersage_service(self, service: Any | None) -> None:
        """T-0105: ML-Service nachtraeglich setzen. main.py initialisiert
        den ML-Service erst nach dem Motor-Konstruktor — daher Setter."""
        self._ml_vorhersage_service = service

    def setze_konfig(self, konfig: Any | None) -> None:
        """T-0105: GesamtKonfig nachtraeglich setzen, fuer
        `MLVorhersageService.live_vorhersage`-Aufrufe."""
        self._konfig = konfig

    async def pruefe_zone(self, zone_id: str) -> BewaesserungsEntscheidung:
        """Prueft eine einzelne Zone und gibt eine Entscheidung zurueck."""
        jetzt = self._jetzt()
        naechste_pruefung = jetzt + STANDARD_PRUEFINTERVALL
        zone = self._zonen.get(zone_id)

        if zone is None:
            logger.warning("entscheidung.zone_unbekannt", zone_id=zone_id)
            return await self._erstelle_entscheidung(
                zone_id=zone_id,
                soll_bewaessern=False,
                begruendung="Zone ist unbekannt",
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
            )

        if zone.modus == ZonenModus.MONITORING:
            return await self._erstelle_entscheidung(
                zone_id=zone_id,
                soll_bewaessern=False,
                begruendung="Zone im Monitoring-Modus",
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
            )

        robust = await self._robuste_feuchte(zone_id, jetzt)
        if robust is None:
            return await self._erstelle_entscheidung(
                zone_id=zone_id,
                soll_bewaessern=False,
                begruendung="Keine gueltige Feuchtemessung vorhanden",
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.KEINE_MESSUNG,
            )
        aktuelle_feuchte = robust
        wetter = await self._wetter_fuer_zone(zone_id).hole_vorhersage()
        niederschlag_6h = wetter.niederschlag_naechste_stunden(6)
        niederschlag_24h = wetter.niederschlag_naechste_stunden(24)
        et0_6h = wetter.et0_naechste_stunden(6)
        et0_24h = wetter.et0_naechste_stunden(24)
        # T-0135 (H-4 Stufe 1b): Schwelle Regime-aware aufloesen.
        eff_schwelle_min = effektiv_schwelle_min(zone, jetzt)
        effektive_schwelle, anhebung = self._effektive_schwelle(
            eff_schwelle_min, et0_24h,
        )

        if aktuelle_feuchte >= effektive_schwelle:
            return await self._erstelle_entscheidung(
                zone_id=zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Feuchte {self._format_zahl(aktuelle_feuchte)}% ist nicht unter "
                    f"Schwelle {self._format_zahl(effektive_schwelle)}%"
                    + (f" (+{self._format_zahl(anhebung)}% Hitze)" if anhebung > 0 else "")
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.FEUCHTE_OK,
            )

        regen_schwelle_mm = self._regen_schwelle_mm()
        if niederschlag_6h >= regen_schwelle_mm:
            return await self._erstelle_entscheidung(
                zone_id=zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Feuchte {self._format_zahl(aktuelle_feuchte)}% unter Schwelle "
                    f"{self._format_zahl(effektive_schwelle)}%, aber "
                    f"{self._format_zahl(niederschlag_6h)}mm Regen in 6h erwartet"
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.REGEN_ERWARTET,
            )

        # T-0322: Konvektions-/Starkregen-Guard. Auch bei niedriger mm-Prognose
        # blockieren, wenn die Regenwahrscheinlichkeit sehr hoch ist -- open-meteo
        # unterschaetzt Konvektion notorisch (Realfall 21.06.: 0.4mm/P68%, DWD
        # warnte 15-30 l/m2 + Gewitter). Schwelle konservativ (Default 80%).
        regen_wahrsch_6h = wetter.max_regen_wahrscheinlichkeit_naechste_stunden(6)
        if regen_wahrsch_6h >= self._regen_wahrscheinlichkeit_schwelle():
            return await self._erstelle_entscheidung(
                zone_id=zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Feuchte {self._format_zahl(aktuelle_feuchte)}% unter Schwelle "
                    f"{self._format_zahl(effektive_schwelle)}%, aber Starkregen "
                    f"wahrscheinlich ({self._format_zahl(regen_wahrsch_6h)}% in 6h)"
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.REGEN_ERWARTET,
            )

        ist_kritisch = aktuelle_feuchte < zone.feuchte_kritisch
        in_bevorzugter_zeit = self._ist_bevorzugte_zeit(zone, jetzt)
        if not in_bevorzugter_zeit and not ist_kritisch:
            return await self._erstelle_entscheidung(
                zone_id=zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Feuchte {self._format_zahl(aktuelle_feuchte)}% unter Schwelle, "
                    "aber ausserhalb bevorzugter Zeit"
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.ZEITFENSTER,
            )

        tagesverbrauch = await self._tagesverbrauch(zone_id)
        budget = self._effektives_tagesbudget(zone, ist_kritisch)
        if tagesverbrauch >= budget:
            return await self._erstelle_entscheidung(
                zone_id=zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Tagesbudget erreicht ({self._format_zahl(tagesverbrauch)} von "
                    f"{self._format_zahl(budget)})"
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.BUDGET_ERSCHOEPFT,
            )

        pause_eingehalten, verbleibende_pause = await self._pause_eingehalten(
            zone, jetzt, scope=EntscheidungsScope.ZONE,
        )
        # T-0378: kritische Trockenheit schlaegt min_pause (gedeckelt).
        if not pause_eingehalten and not await self._pause_bypass_bei_kritisch(
            zone, ist_kritisch, jetzt,
        ):
            return await self._erstelle_entscheidung(
                zone_id=zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Min-Pause noch nicht eingehalten, noch "
                    f"{self._format_zahl(verbleibende_pause)} Minuten warten"
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.PAUSE_AKTIV,
            )

        # T-0281 (Isomorphie zu T-0280 + pruefe_kanal/T-0231): die explizit
        # anders konfigurierten Strategien (KONSTANT_NIEDRIG/SELTEN_GROSS)
        # koennen auch UNTER Schwelle 'kein_bedarf' sagen (Trockenphase als
        # Feature bzw. Tiefen-Dose-Strategie mit Reserve). Dann darf
        # pruefe_zone KEIN soll_bewaessern=True ins entscheidung_log
        # schreiben -- sonst zeigen EntscheidungsLog/Historie/
        # EntscheidungsErklaerung falsche "giessen"-Eintraege UND der
        # Eintrag setzt einen falschen Pause-Anker (_pause_eingehalten).
        # KORRIDOR/HAEUFIG_KLEIN behalten ihr schwellen-basiertes Verhalten
        # (exakt wie pruefe_kanal). Verdict-Fehler -> safe-default: weiter
        # wie bisher (Schwelle entscheidet allein). Echte Bewaesserung
        # macht ausschliesslich pruefe_kanal -- hier nur Logging-Konsistenz.
        if zone.bewaesserungs_strategie in (
            BewaesserungsStrategie.KONSTANT_NIEDRIG,
            BewaesserungsStrategie.SELTEN_GROSS,
        ):
            try:
                strategie_verdict = await self._strategie_verdict_pro_zone(
                    zone, aktuelle_feuchte, et0_24h, niederschlag_24h, jetzt,
                )
            except Exception:
                logger.exception(
                    "pruefe_zone.strategie_verdict_fehler", zone_id=zone_id,
                )
                strategie_verdict = None
            if strategie_verdict is not None and not strategie_verdict.soll_bewaessern:
                return await self._erstelle_entscheidung(
                    zone_id=zone_id,
                    soll_bewaessern=False,
                    begruendung=strategie_verdict.grund,
                    zeitpunkt=jetzt,
                    naechste_pruefung=naechste_pruefung,
                    blocker_typ=BlockerTyp.STRATEGIE_KEIN_BEDARF,
                )

        dauer_sekunden = await self._dauer_mit_ml_weiche(
            zone, jetzt, aktuelle_feuchte, et0_6h,
            ziel_schwelle=effektive_schwelle,
            niederschlag_6h=niederschlag_6h,
            niederschlag_24h=niederschlag_24h,
            et0_24h=et0_24h,
        )
        schwelle_text = f"{self._format_zahl(effektive_schwelle)}%"
        if anhebung > 0:
            schwelle_text += f" (+{self._format_zahl(anhebung)}% Hitze)"
        teile = [
            f"Feuchte {self._format_zahl(aktuelle_feuchte)}% unter Schwelle {schwelle_text}",
            f"Regen naechste 6h {self._format_zahl(niederschlag_6h)}mm",
        ]
        if ist_kritisch and not in_bevorzugter_zeit:
            teile.append("kritischer Wert ausserhalb bevorzugter Zeit")
        else:
            teile.append("bevorzugte Zeit aktiv")
        teile.append(f"Dauer {dauer_sekunden}s")

        logger.info(
            "entscheidung.bewaessern",
            zone_id=zone_id,
            feuchte=aktuelle_feuchte,
            niederschlag_6h=niederschlag_6h,
            et0_6h=et0_6h,
            dauer_sekunden=dauer_sekunden,
        )

        return await self._erstelle_entscheidung(
            zone_id=zone_id,
            soll_bewaessern=True,
            dauer_sekunden=dauer_sekunden,
            begruendung=", ".join(teile),
            zeitpunkt=jetzt,
            naechste_pruefung=naechste_pruefung,
        )

    async def pruefe_kanal(
        self, kanal: int, zonen: list[ZonenKonfig],
    ) -> BewaesserungsEntscheidung:
        """Prueft einen Ventilkanal mit mehreren Sensoren (Min-Start).

        Zonen mit gleichem ventil_kanal werden als ein physischer Bewaesserungskreis
        behandelt. Gestartet wird nach der trockensten Zone unter ihrer effektiven
        Mindestschwelle; der Durchschnitt bleibt nur informativ.
        """
        jetzt = self._jetzt()
        naechste_pruefung = jetzt + STANDARD_PRUEFINTERVALL
        ref_zone = zonen[0]  # Referenz fuer Schwellen, Budget, Zeiten
        zone_ids = [z.zone_id for z in zonen]

        # Feuchte-Werte aller Zonen sammeln (robust gegen Einzel-Spikes).
        feuchte_messpunkte: list[tuple[ZonenKonfig, float]] = []
        for zone in zonen:
            wert = await self._robuste_feuchte(zone.zone_id, jetzt)
            if wert is not None:
                feuchte_messpunkte.append((zone, wert))

        if not feuchte_messpunkte:
            return await self._erstelle_entscheidung(
                zone_id=ref_zone.zone_id,
                soll_bewaessern=False,
                begruendung=f"Kanal {kanal}: Keine gueltige Feuchtemessung",
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.KEINE_MESSUNG,
                scope=EntscheidungsScope.KANAL,
                scope_ref=str(kanal),
            )

        # Wetter VOR dem Feuchte-Check holen, damit die Schwelle adaptiv sein kann
        wetter = await self._wetter_fuer_zone(ref_zone.zone_id).hole_vorhersage()
        niederschlag_6h = wetter.niederschlag_naechste_stunden(6)
        niederschlag_24h = wetter.niederschlag_naechste_stunden(24)
        et0_6h = wetter.et0_naechste_stunden(6)
        et0_24h = wetter.et0_naechste_stunden(24)

        zonenwerte = []
        for zone, feuchte in feuchte_messpunkte:
            # T-0135 (H-4 Stufe 1b): pro Zone Regime-aware aufloesen
            # (verschiedene Zonen koennen verschiedene Regimes haben).
            eff_schwelle_min = effektiv_schwelle_min(zone, jetzt)
            effektive_schwelle, anhebung = self._effektive_schwelle(
                eff_schwelle_min, et0_24h,
            )
            zonenwerte.append({
                "zone": zone,
                "feuchte": feuchte,
                "effektive_schwelle": effektive_schwelle,
                "anhebung": anhebung,
                "defizit": effektive_schwelle - feuchte,
            })

        # T-0337: Kanal-Trigger-Lead. Zonen mit `kanal_trigger_ausschluss`
        # (z.B. bambuswald/sensor-b: hydrophob-niedrig + Lag) treiben den
        # Min-Trigger NICHT -> der verlaessliche Strang-Partner fuehrt, statt
        # dass ein min() am Sensor-Artefakt ueber-waessert. Durchschnitt +
        # Logging bleiben ueber ALLE Zonen (informativ). Graceful: sind alle
        # Trigger-Zonen ausgeschlossen/fehlen -> Fallback auf alle (kein Blind).
        trigger_werte = [
            zw for zw in zonenwerte
            if not zw["zone"].kanal_trigger_ausschluss
        ]

        # T-0382: Wenn KEINE nicht-ausgeschlossene Zone diesen Zyklus einen
        # validen Messwert hat, NICHT auf den ausgeschlossenen Artefakt-Sensor
        # zurueckfallen. Zwei Faelle unterscheiden:
        #  - Der Kanal HAT per Config verlaessliche Trigger-Zonen, aber keine
        #    liefert gerade einen Wert (Sensor-Dropout, z.B. Yogaraum kurz None)
        #    -> kein verlaesslicher Trigger -> NICHT giessen. Der fruehere
        #    `or zonenwerte`-Fallback liess hier den T-0337-ausgeschlossenen
        #    sensor-b (hydrophob-niedrig) trockenste werden und ueber-waesserte
        #    die laengst nasse Zone (Runaway -- genau was T-0337 verhindern soll).
        #  - ALLE Zonen des Kanals sind per Config ausgeschlossen (degenerierte
        #    Config) -> Fallback auf alle, damit der Kanal nicht blind wird.
        if not trigger_werte:
            hat_verlaessliche_zonen = any(
                not z.kanal_trigger_ausschluss for z in zonen
            )
            if hat_verlaessliche_zonen:
                logger.warning(
                    "entscheidung.kanal_kein_verlaesslicher_trigger",
                    kanal=kanal,
                    einzelwerte={
                        str(zw["zone"].zone_id): round(float(zw["feuchte"]), 1)
                        for zw in zonenwerte
                    },
                    zonen=zone_ids,
                )
                return await self._erstelle_entscheidung(
                    zone_id=ref_zone.zone_id,
                    soll_bewaessern=False,
                    begruendung=(
                        f"Kanal {kanal}: nur ausgeschlossene Sensoren liefern "
                        f"Werte (kein verlaesslicher Trigger) -> keine "
                        f"Bewaesserung"
                    ),
                    zeitpunkt=jetzt,
                    naechste_pruefung=naechste_pruefung,
                    blocker_typ=BlockerTyp.KEINE_MESSUNG,
                    scope=EntscheidungsScope.KANAL,
                    scope_ref=str(kanal),
                )
            trigger_werte = zonenwerte

        trockenste = min(trigger_werte, key=lambda item: item["feuchte"])
        nasseste = max(zonenwerte, key=lambda item: item["feuchte"])
        durchschnitt = sum(float(item["feuchte"]) for item in zonenwerte) / len(zonenwerte)
        bedarf = [item for item in trigger_werte if item["defizit"] > 0]

        logger.info(
            "entscheidung.kanal_feuchte",
            kanal=kanal,
            trockenste_zone=trockenste["zone"].zone_id,
            min_feuchte=round(float(trockenste["feuchte"]), 1),
            nasseste_zone=nasseste["zone"].zone_id,
            max_feuchte=round(float(nasseste["feuchte"]), 1),
            durchschnitt=round(durchschnitt, 1),
            einzelwerte={
                str(item["zone"].zone_id): round(float(item["feuchte"]), 1)
                for item in zonenwerte
            },
            trigger_zonen=[str(zw["zone"].zone_id) for zw in trigger_werte],
            zonen=zone_ids,
        )

        if not bedarf:
            return await self._erstelle_entscheidung(
                zone_id=ref_zone.zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Kanal {kanal}: trockenste Zone {trockenste['zone'].zone_id} "
                    f"{self._format_zahl(trockenste['feuchte'])}% nicht unter Schwelle "
                    f"{self._format_zahl(trockenste['effektive_schwelle'])}%, "
                    f"nasseste Zone {nasseste['zone'].zone_id} "
                    f"{self._format_zahl(nasseste['feuchte'])}%, "
                    f"Durchschnitt {self._format_zahl(durchschnitt)}%"
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.FEUCHTE_OK,
                scope=EntscheidungsScope.KANAL,
                scope_ref=str(kanal),
            )

        # T-0231 Phase 3: Strategie-Konvergenz-Check. Bisher genuegte
        # Defizit > 0 fuer Auto-Start; jetzt muss pro Bedarfs-Zone
        # auch die Strategie zustimmen -- ABER nur fuer die explizit
        # anders konfigurierten Strategien (KONSTANT_NIEDRIG +
        # SELTEN_GROSS). Diese Strategien sagen bewusst 'kein_bedarf'
        # auch unter Schwelle (KONSTANT_NIEDRIG fuer Trockenphasen-
        # foerdernde Pflanzen, SELTEN_GROSS fuer Tiefen-Dose-Strategie).
        # KORRIDOR (Default) + HAEUFIG_KLEIN behalten ihr heutiges
        # Schwellen-basiertes Verhalten -- sonst wuerden alle 14 Zonen
        # vom Refactor betroffen sein und Tests brechen ohne dass
        # T-0021 das verlangt.
        # Bei Fehler im Strategie-Verdict (DB-Lock, fehlende Kalibrier-
        # Daten) faellt die Zone auf das Pre-T-0231-Verhalten zurueck
        # (Schwelle entscheidet allein) -- Safe-Default.
        # Wichtig: 'akut' hat in jeder Strategie Vorrang, der Check
        # uebersteuert keine Notfaelle.
        konvergenz_strategien = {
            BewaesserungsStrategie.KONSTANT_NIEDRIG,
            BewaesserungsStrategie.SELTEN_GROSS,
        }
        strategie_verdicts: dict[str, "object"] = {}
        bedarf_strategie_ok = []
        for item in bedarf:
            if item["zone"].bewaesserungs_strategie not in konvergenz_strategien:
                # KORRIDOR / HAEUFIG_KLEIN: alte Logik (Schwelle reicht).
                bedarf_strategie_ok.append(item)
                continue
            try:
                verdict = await self._strategie_verdict_pro_zone(
                    item["zone"],
                    float(item["feuchte"]),
                    et0_24h,
                    niederschlag_24h,
                    jetzt,
                )
            except Exception:
                logger.exception(
                    "pruefe_kanal.strategie_verdict_fehler",
                    zone_id=item["zone"].zone_id, kanal=kanal,
                )
                bedarf_strategie_ok.append(item)
                continue
            strategie_verdicts[item["zone"].zone_id] = verdict
            if verdict.soll_bewaessern:
                bedarf_strategie_ok.append(item)

        if not bedarf_strategie_ok:
            # Alle Bedarfs-Zonen sagen 'kein_bedarf' nach Strategie ->
            # Dashboard und Auto-Loop konvergieren auf "warten".
            zone_grunde = ", ".join(
                f"{zid}={v.empfehlungs_typ}"
                for zid, v in strategie_verdicts.items()
            )
            return await self._erstelle_entscheidung(
                zone_id=ref_zone.zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Kanal {kanal}: Schwelle unterschritten "
                    f"(trockenste {trockenste['zone'].zone_id} "
                    f"{self._format_zahl(trockenste['feuchte'])}% < "
                    f"{self._format_zahl(trockenste['effektive_schwelle'])}%), "
                    f"aber Strategie sagt 'kein_bedarf' "
                    f"(Welkepunkt-Reserve / Wohl-Min ok). "
                    f"Verdicts: {zone_grunde}."
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.STRATEGIE_KEIN_BEDARF,
                scope=EntscheidungsScope.KANAL,
                scope_ref=str(kanal),
            )

        # Nur Zonen mit Strategie-OK als Kandidaten fuer Min-Start.
        # bedarf war [item], filtere auf strategie_ok.
        bedarf = bedarf_strategie_ok

        startwert = max(
            bedarf,
            key=lambda item: (item["defizit"], -float(item["feuchte"])),
        )
        start_zone = startwert["zone"]
        start_feuchte = float(startwert["feuchte"])
        start_schwelle = float(startwert["effektive_schwelle"])
        start_anhebung = float(startwert["anhebung"])
        start_label = (
            f"trockenste Zone {start_zone.zone_id}"
            if startwert is trockenste
            else f"groesste Defizit-Zone {start_zone.zone_id}"
        )
        regen_schwelle_mm = self._regen_schwelle_mm()

        if niederschlag_6h >= regen_schwelle_mm:
            return await self._erstelle_entscheidung(
                zone_id=ref_zone.zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Kanal {kanal}: {start_label} "
                    f"{self._format_zahl(start_feuchte)}% unter Schwelle "
                    f"{self._format_zahl(start_schwelle)}%, "
                    f"aber {self._format_zahl(niederschlag_6h)}mm Regen in 6h erwartet"
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.REGEN_ERWARTET,
                scope=EntscheidungsScope.KANAL,
                scope_ref=str(kanal),
            )

        # T-0322: Konvektions-/Starkregen-Guard (analog pruefe_zone) -- blockt bei
        # sehr hoher Regenwahrscheinlichkeit auch ohne mm-Prognose.
        regen_wahrsch_6h = wetter.max_regen_wahrscheinlichkeit_naechste_stunden(6)
        if regen_wahrsch_6h >= self._regen_wahrscheinlichkeit_schwelle():
            return await self._erstelle_entscheidung(
                zone_id=ref_zone.zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Kanal {kanal}: {start_label} "
                    f"{self._format_zahl(start_feuchte)}% unter Schwelle, aber "
                    f"Starkregen wahrscheinlich "
                    f"({self._format_zahl(regen_wahrsch_6h)}% in 6h)"
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.REGEN_ERWARTET,
                scope=EntscheidungsScope.KANAL,
                scope_ref=str(kanal),
            )

        ist_kritisch = start_feuchte < start_zone.feuchte_kritisch
        in_bevorzugter_zeit = self._ist_bevorzugte_zeit(ref_zone, jetzt)

        if not in_bevorzugter_zeit and not ist_kritisch:
            return await self._erstelle_entscheidung(
                zone_id=ref_zone.zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Kanal {kanal}: {start_label} "
                    f"{self._format_zahl(start_feuchte)}% unter Schwelle, "
                    "aber ausserhalb bevorzugter Zeit"
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.ZEITFENSTER,
                scope=EntscheidungsScope.KANAL,
                scope_ref=str(kanal),
            )

        tagesverbrauch = await self._tagesverbrauch(ref_zone.zone_id)
        budget = self._effektives_tagesbudget(ref_zone, ist_kritisch)
        if tagesverbrauch >= budget:
            return await self._erstelle_entscheidung(
                zone_id=ref_zone.zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Kanal {kanal}: Tagesbudget erreicht ({self._format_zahl(tagesverbrauch)} "
                    f"von {self._format_zahl(budget)}s)"
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.BUDGET_ERSCHOEPFT,
                scope=EntscheidungsScope.KANAL,
                scope_ref=str(kanal),
            )

        pause_eingehalten, verbleibende_pause = await self._pause_eingehalten(
            ref_zone, jetzt, scope=EntscheidungsScope.KANAL,
        )
        # T-0378: kritische Trockenheit schlaegt min_pause (gedeckelt).
        # `ist_kritisch` bezieht sich hier auf die START-Zone des Kanals
        # (start_feuchte < start_zone.feuchte_kritisch), der Karenz-Guard
        # auf die ref_zone -- dieselbe Paarung nutzen schon Zeitfenster-
        # und Budget-Check darueber.
        if not pause_eingehalten and not await self._pause_bypass_bei_kritisch(
            ref_zone, ist_kritisch, jetzt,
        ):
            return await self._erstelle_entscheidung(
                zone_id=ref_zone.zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Kanal {kanal}: Min-Pause noch nicht eingehalten, "
                    f"noch {self._format_zahl(verbleibende_pause)} Min warten"
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.PAUSE_AKTIV,
                scope=EntscheidungsScope.KANAL,
                scope_ref=str(kanal),
            )

        # T-0345: Die Dosis nutzt dasselbe Strategieziel wie der
        # Pro-Zone-/Dashboard-Pfad. Der Trigger bleibt bewusst kompatibel
        # zum bestehenden Kanalverhalten; nur die Wassermenge darf nicht
        # mehr auf die effektive Min-Schwelle zurueckfallen.
        dosis_verdict = strategie_verdicts.get(start_zone.zone_id)
        if dosis_verdict is None:
            try:
                dosis_verdict = await self._strategie_verdict_pro_zone(
                    start_zone,
                    start_feuchte,
                    et0_24h,
                    niederschlag_24h,
                    jetzt,
                )
            except Exception:
                logger.exception(
                    "pruefe_kanal.dose_ziel_fehler",
                    zone_id=start_zone.zone_id,
                    kanal=kanal,
                )
                dosis_verdict = None
        strategie_ziel = None
        if dosis_verdict is not None:
            strategie_ziel = getattr(dosis_verdict, "ziel_feuchte", None)
            if strategie_ziel is None:
                strategie_ziel = getattr(dosis_verdict, "ziel_feuchte_roh", None)
        dose_ziel = _kanal_dose_ziel(
            start_zone,
            start_schwelle,
            strategie_ziel=strategie_ziel,
        )

        dauer_sekunden = await self._dauer_mit_ml_weiche(
            start_zone, jetzt, start_feuchte, et0_6h,
            ziel_schwelle=dose_ziel,
            niederschlag_6h=niederschlag_6h,
            niederschlag_24h=niederschlag_24h,
            et0_24h=et0_24h,
        )
        schwelle_text = f"{self._format_zahl(start_schwelle)}%"
        if start_anhebung > 0:
            schwelle_text += f" (+{self._format_zahl(start_anhebung)}% Hitze)"

        return await self._erstelle_entscheidung(
            zone_id=ref_zone.zone_id,
            soll_bewaessern=True,
            dauer_sekunden=dauer_sekunden,
            begruendung=(
                f"Kanal {kanal}: {start_label} "
                f"{self._format_zahl(start_feuchte)}% unter Schwelle {schwelle_text}, "
                f"nasseste Zone {nasseste['zone'].zone_id} "
                f"{self._format_zahl(nasseste['feuchte'])}%, "
                f"Ziel {self._format_zahl(dose_ziel)}%, "
                f"Dauer {dauer_sekunden}s"
            ),
            zeitpunkt=jetzt,
            naechste_pruefung=naechste_pruefung,
            scope=EntscheidungsScope.KANAL,
            scope_ref=str(kanal),
        )

    async def pruefe_kanal_max_stop(
        self, kanal: int, zonen: list[ZonenKonfig],
    ) -> tuple[bool, str]:
        """Prueft aktive Kanaele auf Max-Stop nach der nassesten Zone."""
        jetzt = self._jetzt()
        zonenwerte = []
        for zone in zonen:
            wert = await self._robuste_feuchte(zone.zone_id, jetzt)
            if wert is not None:
                zonenwerte.append({"zone": zone, "feuchte": float(wert)})

        if not zonenwerte:
            return False, f"Kanal {kanal}: Keine gueltige Feuchtemessung fuer Max-Stop"

        nasseste = max(zonenwerte, key=lambda item: item["feuchte"])
        for item in sorted(zonenwerte, key=lambda item: item["feuchte"], reverse=True):
            zone = item["zone"]
            feuchte = float(item["feuchte"])
            stop_schwelle = zone.feuchte_schwelle_max + 10.0
            if feuchte > stop_schwelle:
                begruendung = (
                    f"Kanal {kanal}: Zone {zone.zone_id} "
                    f"{self._format_zahl(feuchte)}% ueber Stop-Schwelle "
                    f"{self._format_zahl(stop_schwelle)}%"
                )
                logger.info(
                    "entscheidung.kanal_max_stop",
                    kanal=kanal,
                    zone_id=zone.zone_id,
                    feuchte=round(feuchte, 1),
                    stop_schwelle=round(stop_schwelle, 1),
                    nasseste_zone=nasseste["zone"].zone_id,
                    nasseste_feuchte=round(float(nasseste["feuchte"]), 1),
                )
                return True, begruendung

        begruendung = (
            f"Kanal {kanal}: nasseste Zone {nasseste['zone'].zone_id} "
            f"{self._format_zahl(nasseste['feuchte'])}% unter Max-Stop"
        )
        return False, begruendung

    async def pruefe_alle_zonen(self) -> list[BewaesserungsEntscheidung]:
        """Prueft alle konfigurierten Zonen."""
        entscheidungen: list[BewaesserungsEntscheidung] = []
        for zone in self._zonen.values():
            entscheidungen.append(await self.pruefe_zone(zone.zone_id))
        return entscheidungen

    async def prognostiziere_bewaesserung(self, zone_id: str) -> tuple[datetime | None, str]:
        """Predictive Watering: Wann wird voraussichtlich bewaessert werden muessen?"""
        zone = self._zonen.get(zone_id)
        if zone is None:
            return None, "Zone ist unbekannt"

        jetzt = self._jetzt()
        messungen = await self._speicher.hole_messungen(
            zone_id,
            von=jetzt - timedelta(hours=24),
            bis=jetzt,
        )
        feuchte_messungen = sorted(
            [messung for messung in messungen if messung.boden_feuchte is not None],
            key=lambda messung: messung.zeitstempel,
        )

        if len(feuchte_messungen) < 2:
            return None, "Zu wenige Feuchtedaten fuer Prognose"

        # T-0135 (H-4 Stufe 1b): Schwelle Regime-aware aufloesen.
        eff_schwelle_min = effektiv_schwelle_min(zone, jetzt)
        aktuelle_feuchte = float(feuchte_messungen[-1].boden_feuchte or 0.0)
        if aktuelle_feuchte <= eff_schwelle_min:
            return jetzt, "Schwelle bereits erreicht oder unterschritten"

        trend_pro_stunde = self._berechne_feuchte_trend(feuchte_messungen)
        wetter = await self._wetter_fuer_zone(zone_id).hole_vorhersage()
        zukunftsstunden = sorted(
            [stunde for stunde in wetter.stunden if stunde.zeitstempel > jetzt],
            key=lambda stunde: stunde.zeitstempel,
        )[:48]

        if not zukunftsstunden:
            if trend_pro_stunde >= -PROGNOSE_EPSILON:
                return None, "Feuchte stabil/steigend"

            stunden_bis_schwelle = (
                (aktuelle_feuchte - eff_schwelle_min) / abs(trend_pro_stunde)
            )
            zeitpunkt = jetzt + timedelta(hours=stunden_bis_schwelle)
            return (
                zeitpunkt,
                f"Feuchte faellt ~{abs(trend_pro_stunde):.1f}%/h, "
                f"Schwelle in ~{stunden_bis_schwelle:.1f}h erreicht",
            )

        feuchte = aktuelle_feuchte
        vorheriger_zeitpunkt = jetzt
        aenderungsraten: list[float] = []

        for stunde in zukunftsstunden:
            delta_stunden = (
                stunde.zeitstempel - vorheriger_zeitpunkt
            ).total_seconds() / 3600
            if delta_stunden <= 0:
                vorheriger_zeitpunkt = stunde.zeitstempel
                continue

            aenderung = trend_pro_stunde * delta_stunden + self._wetter_aenderung(stunde)
            naechste_feuchte = feuchte + aenderung
            aenderungsraten.append(aenderung / delta_stunden)

            if naechste_feuchte <= eff_schwelle_min:
                absenkung = feuchte - naechste_feuchte
                anteil = 1.0
                if absenkung > 0:
                    anteil = min(
                        max((feuchte - eff_schwelle_min) / absenkung, 0.0),
                        1.0,
                    )

                zeitpunkt = vorheriger_zeitpunkt + timedelta(hours=delta_stunden * anteil)
                stunden_bis_schwelle = max(
                    (zeitpunkt - jetzt).total_seconds() / 3600,
                    0.0,
                )
                mittlere_rate = self._mittlere_rate(aenderungsraten)
                return (
                    zeitpunkt,
                    f"Feuchte faellt ~{abs(mittlere_rate):.1f}%/h, "
                    f"Schwelle in ~{stunden_bis_schwelle:.1f}h erreicht",
                )

            feuchte = naechste_feuchte
            vorheriger_zeitpunkt = stunde.zeitstempel

        mittlere_rate = self._mittlere_rate(aenderungsraten)
        if mittlere_rate >= -PROGNOSE_EPSILON and trend_pro_stunde >= -PROGNOSE_EPSILON:
            return None, "Feuchte stabil/steigend"

        return None, (
            f"Schwelle innerhalb des Prognosefensters ({len(zukunftsstunden)}h) nicht erreicht"
        )

    def _berechne_dauer(
        self, zone: ZonenKonfig, aktuelle_feuchte: float, et0_6h: float,
        ziel_schwelle: float | None = None,
        delta_pp_pro_minute_override: float | None = None,
        clip_auf_max: bool = True,
        wmax_override: float | None = None,
        r0_override: float | None = None,
    ) -> int:
        """Berechnet die Bewaesserungsdauer in Sekunden.

        `ziel_schwelle` ueberschreibt die Zone-Basis-Schwelle (fuer ET0-
        adaptierte Entscheidungen). Unten gecapped bei MIN_DAUER_SEKUNDEN,
        damit bei sehr kleinen Differenzen keine 0s-Empfehlungen entstehen,
        die die VentilSicherung ablehnt und das Shadow-Log fluten.

        T-0089: Pro-Zone-Wirkungsrate (`zone.delta_pp_pro_minute`) bestimmt
        den Sensor-Anstieg pro Minute. Bei None faellt das auf
        `DEFAULT_DELTA_PP_PRO_MINUTE` (1.0) zurueck — heutiges Pre-T-0089-
        Verhalten. Bei pathologischen Werten (<=0) ebenfalls Default.

        T-0085: `delta_pp_pro_minute_override` erlaubt dem Caller, einen
        bereits via `_aufgeloeste_wirkungsrate(zone)` ermittelten Wert
        durchzureichen (z.B. aus Kalibrierungs-Median). Wenn None, wird
        der Konfig-Override aus `zone.delta_pp_pro_minute` genutzt.
        Damit muss `_berechne_dauer` selbst nicht async werden.
        """
        # Note: hier KEIN Regime-Override -- die meisten Caller uebergeben
        # `ziel_schwelle` explizit (sie haben `eff_schwelle_min` schon
        # aufgeloest). Der Default-Fallback bleibt auf der konstanten
        # Zone-Schwelle, weil _berechne_dauer kein `jetzt` kennt.
        schwelle = ziel_schwelle if ziel_schwelle is not None else zone.feuchte_schwelle_min
        feuchte_differenz = max(schwelle - aktuelle_feuchte, 0.0)
        if delta_pp_pro_minute_override is not None and delta_pp_pro_minute_override > 0:
            delta_pp_pro_minute = delta_pp_pro_minute_override
        else:
            delta_pp_pro_minute = (
                zone.delta_pp_pro_minute
                if zone.delta_pp_pro_minute is not None
                else DEFAULT_DELTA_PP_PRO_MINUTE
            )
        if delta_pp_pro_minute <= 0:
            delta_pp_pro_minute = DEFAULT_DELTA_PP_PRO_MINUTE
        # T-0091b: Plateau-Modell hat Vorrang vor T-0091a-log-Decay.
        # total_wirkung(d) = wirkung_max_pp * (1 - exp(-d/tau))
        # tau = wirkung_max_pp / wirkungsrate_initial
        # Inverse: d = -tau * log(1 - delta/wmax)
        # Cap: delta <= 0.95 * wmax (sonst log negativ → unendlich).
        # T-0292 Stufe 2: gefittete wmax/r0 (Override) haben Vorrang vor der
        # Konfig, wenn der Caller sie aufgeloest hat (`adoptieren=True` +
        # angenommener Fit). Sonst None -> Konfig-Wert (No-op).
        wmax = wmax_override if wmax_override is not None else zone.wirkung_max_pp
        r0 = r0_override if r0_override is not None else zone.wirkungsrate_initial
        if wmax is not None and r0 is not None and r0 > 0 and wmax > 0:
            import math
            tau = wmax / r0
            # Cap der Soll-Wirkung auf 95 % vom Plateau
            erreichbar_max = wmax * 0.95
            delta_eff = min(feuchte_differenz, erreichbar_max)
            if delta_eff <= 0:
                basis_dauer_min = 0.0
            else:
                basis_dauer_min = -tau * math.log(1.0 - delta_eff / wmax)
            basis_dauer = basis_dauer_min * 60.0
        else:
            # T-0091a: log-Decay-Korrektur bei langen Dosen.
            # rate(d) = rate0 * (1 + alpha * log(d/30)) fuer d > 30 min.
            # Bei alpha=0 (Default) keine Aenderung. Iterativer Loop weil
            # die Dauer selbst von der effektiven Rate abhaengt — 2 Schritte
            # konvergieren in der Praxis (Fixpunkt nahe genug).
            alpha = zone.wirkungsrate_dauer_alpha
            effektive_rate = delta_pp_pro_minute
            if alpha != 0.0 and feuchte_differenz > 0:
                import math
                # Erste Schaetzung Dauer mit Basis-Rate
                geschaetzt_min = feuchte_differenz / delta_pp_pro_minute
                for _ in range(2):
                    if geschaetzt_min <= 30:
                        effektive_rate = delta_pp_pro_minute
                    else:
                        log_anteil = math.log(geschaetzt_min / 30.0)
                        faktor = 1.0 + alpha * log_anteil
                        # Sanity-Cap: Rate nicht unter 0.05 pp/min fallen
                        effektive_rate = max(0.05, delta_pp_pro_minute * faktor)
                    geschaetzt_min = feuchte_differenz / effektive_rate
            basis_dauer = (feuchte_differenz / effektive_rate) * 60.0
        verdunstungs_faktor = 1.0 + max(et0_6h, 0.0) / 6.0
        dauer = int(round(basis_dauer * verdunstungs_faktor))
        if dauer <= 0:
            # Feuchte ueber Basis aber unter effektiver Schwelle (Hitze-Anhebung)
            # oder numerische Rundung → Mindestdauer, damit die Entscheidung wirksam wird.
            dauer = MIN_DAUER_SEKUNDEN
        if not clip_auf_max:
            # T-0086: Caller will die ROHE Dauer (zur Mehrfach-Takt-Berechnung).
            return max(0, dauer)
        return max(0, min(zone.max_dauer_sekunden, dauer))

    async def _dauer_mit_ml_weiche(
        self,
        zone: ZonenKonfig,
        jetzt: datetime,
        aktuelle_feuchte: float,
        et0_6h: float,
        ziel_schwelle: float,
        *,
        niederschlag_6h: float = 0.0,
        niederschlag_24h: float = 0.0,
        et0_24h: float = 0.0,
        vpd_mittel: float | None = None,
        temperatur_ereignis: float | None = None,
    ) -> int:
        """Berechnet Dauer, konsultiert optional das Response-Modell.

        Verhalten:
        - `response.aktiv=false` → Heuristik, keine DB-Row.
        - `response.aktiv=true, wirksam=false` → Heuristik, Shadow-Row in
          `ml_dauer_vorschlag` (Heuristik + ML-Empfehlung falls verfuegbar).
        - `response.aktiv=true, wirksam=true` → ML-Dauer (falls Modell
          geladen + Empfehlung > 0), sonst Heuristik-Fallback.

        T-0085: Wirkungsrate via `_aufgeloeste_wirkungsrate` (Konfig →
        Kalibrierungs-Median → Default) durchreichen.
        """
        delta_pp_wert, _quelle = await self._aufgeloeste_wirkungsrate(
            zone, jetzt,
        )
        wmax_wert, r0_wert, _wf_quelle = await self._aufgeloeste_wirkung(
            zone, jetzt,
        )
        heuristik_s = self._berechne_dauer(
            zone, aktuelle_feuchte, et0_6h, ziel_schwelle=ziel_schwelle,
            delta_pp_pro_minute_override=delta_pp_wert,
            wmax_override=wmax_wert, r0_override=r0_wert,
        )
        if not self._response_konfig.aktiv:
            return heuristik_s

        ml_s: int | None = None
        modell_version: str | None = None
        if self._response_service is not None:
            try:
                self._response_service.lade_zone(zone.zone_id)
                modell_version = self._response_service.version(zone.zone_id)
                roh_ml = self._response_service.inverse_dauer(
                    zone_id=zone.zone_id,
                    f_vor=aktuelle_feuchte,
                    ziel_schwelle=ziel_schwelle,
                    et0_nach_6h=et0_6h,
                    et0_vor_24h=et0_24h,
                    niederschlag_nach_24h=niederschlag_24h,
                    vpd_mittel=vpd_mittel if vpd_mittel is not None else 0.8,
                    temperatur_ereignis=(
                        temperatur_ereignis if temperatur_ereignis is not None else 18.0
                    ),
                    jetzt=jetzt,
                )
                if roh_ml is not None:
                    # Auf Zone-Grenzen clippen: max gemaess Konfig + MIN-Dauer.
                    ml_s = max(
                        MIN_DAUER_SEKUNDEN,
                        min(zone.max_dauer_sekunden, int(roh_ml)),
                    )
            except Exception:
                logger.exception(
                    "response_modell.inferenz_fehler", zone_id=zone.zone_id,
                )

        features_json = json.dumps({
            "f_vor": aktuelle_feuchte,
            "ziel_schwelle": ziel_schwelle,
            "et0_6h": et0_6h,
            "et0_24h": et0_24h,
            "niederschlag_6h": niederschlag_6h,
            "niederschlag_24h": niederschlag_24h,
            "vpd_mittel": vpd_mittel,
            "temperatur_ereignis": temperatur_ereignis,
        })
        modus = "wirksam" if self._response_konfig.wirksam else "shadow"
        try:
            await self._speicher.speichere_dauer_vorschlag(
                zeitstempel=jetzt,
                zone_id=zone.zone_id,
                f_vor=float(aktuelle_feuchte),
                ziel_schwelle=float(ziel_schwelle),
                heuristik_s=int(heuristik_s),
                ml_s=ml_s,
                ml_modell_version=modell_version if ml_s is not None else None,
                features_json=features_json,
                modus=modus,
            )
        except Exception:
            logger.exception(
                "response_modell.persist_fehler", zone_id=zone.zone_id,
            )

        if self._response_konfig.wirksam and ml_s is not None:
            return ml_s
        return heuristik_s

    # ---------- T-0066: Dry-Run-Empfehlung fuer das Dashboard-Panel ----------

    def _liter_fuer_dauer(self, zone: ZonenKonfig, dauer_s: int) -> float | None:
        """Heuristische Liter-Umrechnung aus Kanal-Rate (T-0066).

        Spiegelt `bilanz._ereignis_liter` fuer den Automatik-Fall (Kanal-Rate
        × Minuten × zone.anteil_kanal). Ohne BilanzKonfig oder ohne Eintrag
        fuer den Zone-Kanal → None (Panel zeigt dann nur Minuten).
        """
        if self._bilanz_konfig is None or zone.ventil_kanal is None:
            return None
        rate = self._bilanz_konfig.liter_pro_minute_fuer_zone(zone)
        if rate is None:
            return None
        return round((dauer_s / 60.0) * rate * zone.anteil_kanal, 1)

    def _ml_dauer_dry_run(
        self,
        zone: ZonenKonfig,
        jetzt: datetime,
        aktuelle_feuchte: float,
        et0_6h: float,
        et0_24h: float,
        niederschlag_6h: float,
        niederschlag_24h: float,
        ziel_schwelle: float,
    ) -> tuple[int | None, str | None, str | None]:
        """ML-Inferenz ohne Persistenz (T-0066, T-0164 erweitert).

        Rueckgabe `(ml_s, modell_version, status_grund)`.

        `status_grund` erklaert dem Frontend, warum kein ML-Wert kam, damit
        es nicht "raten" muss (T-0164 User-Anforderung). Werte:
          - "ok"               -> ml_s ist gesetzt
          - "konfig_aus"       -> ml_bewaesserungs_response.aktiv=false
          - "service_aus"      -> kein Response-Service initialisiert
          - "modell_fehlt"     -> Modell-Datei fuer Zone nicht geladen
          - "sensor_ueber_ziel"-> ziel_delta <= 0 (Sensor schon ueber Ziel)
          - "inverse_kein_wert"-> Modell-inverse returnte None (Edge-Case)
          - "inferenz_fehler"  -> Exception in der Inferenz (geloggt)

        Niemals crashen — Panel muss weiter laufen.
        """
        if not self._response_konfig.aktiv:
            return None, None, "konfig_aus"
        if self._response_service is None:
            return None, None, "service_aus"
        version: str | None = None
        try:
            self._response_service.lade_zone(zone.zone_id)
            version = self._response_service.version(zone.zone_id)
            if version is None:
                return None, None, "modell_fehlt"
            # T-0164: ziel_delta-Vorabcheck mit eigenem Status-Grund,
            # damit das Frontend "Sensor 60 % schon am Ziel — kein
            # ML-Vorschlag" anzeigen kann statt nur Lücke.
            ziel_delta = float(ziel_schwelle) - float(aktuelle_feuchte)
            if ziel_delta <= 0.0:
                return None, version, "sensor_ueber_ziel"
            roh = self._response_service.inverse_dauer(
                zone_id=zone.zone_id,
                f_vor=aktuelle_feuchte,
                ziel_schwelle=ziel_schwelle,
                et0_nach_6h=et0_6h,
                et0_vor_24h=et0_24h,
                niederschlag_nach_24h=niederschlag_24h,
                vpd_mittel=0.8,
                temperatur_ereignis=18.0,
                jetzt=jetzt,
            )
        except Exception:
            logger.exception(
                "vorhersage_zone.ml_inferenz_fehler", zone_id=zone.zone_id,
            )
            return None, version, "inferenz_fehler"
        if roh is None:
            return None, version, "inverse_kein_wert"
        ml_s = max(
            MIN_DAUER_SEKUNDEN, min(zone.max_dauer_sekunden, int(roh)),
        )
        return ml_s, version, "ok"

    # ---------- T-0075: Welkepunkt + Decay-Prognose + kausale Erklaerung ----

    # TTL-Cache fuer p10-Tagesmin-Schaetzung (DB-schwer, aendert sich pro Tag).
    _WELKE_CACHE_TTL_S: float = 6 * 3600   # 6 h
    _WELKE_TAGESMIN_MIN_TAGE: int = 14     # weniger Daten -> Fallback

    # T-0085: TTL-Cache fuer Wirkungsrate-Aufloesung (DB-Read jede Empfehlung
    # waere Verschwendung). 6 h reichen — Konfig-Aenderungen wirken
    # innerhalb dieser Zeitspanne, schneller fuer Tests via clear_caches.
    _WIRKUNGSRATE_CACHE_TTL_S: float = 6 * 3600
    _WIRKUNGSRATE_MIN_KALIBRIER_N: int = 3
    _WIRKUNGSRATE_KALIBRIER_FENSTER_TAGE: int = 90
    # T-0292 Stufe 2: TTL-Cache fuer die Plateau-Fit-Adoption.
    _WIRKUNG_FIT_CACHE_TTL_S: float = 6 * 3600

    async def _p10_tagesmin_schaetzung(
        self, zone_id: str, jetzt: datetime | None = None,
    ) -> float | None:
        """Schaetzt den Welkepunkt aus echten Sensor-Tagesminima.

        Liest die letzten 90 Tage `sensor_messung`, gruppiert pro Tag,
        nimmt das Tages-Minimum (filtert 0-Artefakte), und errechnet das
        10. Perzentil dieser Tagesminima minus 3 pp Sicherheitsmarge.

        Begruendung 10. Perzentil: ein Tagesminimum, das nur in 1 von
        10 Tagen unterschritten wurde, ist eine sichere Untergrenze
        (statistisch robuster als das absolute Minimum, das ein Sensor-
        Glitch sein kann).

        TTL-Cache 6 h, weil DB-Aggregation und Tagesgranularitaet
        (Aenderung pro Tag, nicht pro Stunde).
        """
        if not hasattr(self, "_welke_cache"):
            self._welke_cache: dict[str, tuple[datetime, float | None]] = {}
        jetzt = jetzt or self._jetzt()
        cached = self._welke_cache.get(zone_id)
        if cached is not None:
            cache_zeit, cache_wert = cached
            if (jetzt - cache_zeit).total_seconds() < self._WELKE_CACHE_TTL_S:
                return cache_wert

        von = jetzt - timedelta(days=90)
        try:
            messungen = await self._speicher.hole_messungen(
                zone_id, von=von, bis=jetzt,
            )
        except Exception:
            logger.exception("welke_schaetzung.db_fehler", zone_id=zone_id)
            return None

        # Pro Tag das Min-Filter (ohne 0%-Artefakte aus Battery-Disconnect).
        tagesmin: dict[str, float] = {}
        for m in messungen:
            f = m.boden_feuchte
            if f is None or f <= 0:
                continue
            tag = m.zeitstempel.date().isoformat()
            cur = tagesmin.get(tag)
            if cur is None or f < cur:
                tagesmin[tag] = float(f)

        if len(tagesmin) < self._WELKE_TAGESMIN_MIN_TAGE:
            self._welke_cache[zone_id] = (jetzt, None)
            return None

        werte = sorted(tagesmin.values())
        # p10 linear interpoliert.
        if len(werte) == 1:
            p10 = werte[0]
        else:
            pos = 0.10 * (len(werte) - 1)
            unten_i = int(pos)
            oben_i = min(unten_i + 1, len(werte) - 1)
            gewicht = pos - unten_i
            p10 = werte[unten_i] * (1 - gewicht) + werte[oben_i] * gewicht
        # Auf gueltigen Bereich klemmen + 3 pp Sicherheitsmarge nach unten.
        wert = max(0.0, min(100.0, round(p10 - 3.0, 1)))
        self._welke_cache[zone_id] = (jetzt, wert)
        return wert

    async def _aufgeloeste_wirkungsrate(
        self, zone: ZonenKonfig, jetzt: datetime | None = None,
    ) -> tuple[float, str]:
        """T-0085: 3-stufige Aufloesungs-Kette fuer delta_pp_pro_minute.

        Prioritaet:
          1. zone.delta_pp_pro_minute (Konfig-Override)              -> "manuell"
          2. Median aus feldkapazitaet_messung typ='wirkungsrate'    -> "kalibrierung"
             (n >= _WIRKUNGSRATE_MIN_KALIBRIER_N in letzten
             _WIRKUNGSRATE_KALIBRIER_FENSTER_TAGE Tagen)
          3. DEFAULT_DELTA_PP_PRO_MINUTE                              -> "default"

        TTL-Cache 6 h pro Zone — Konfig-Aenderungen + neue
        Kalibrierungs-Werte wirken innerhalb dieser Zeitspanne.
        """
        # Konfig-Override hat absoluten Vorrang — kein Cache-Lookup noetig.
        if (
            zone.delta_pp_pro_minute is not None
            and zone.delta_pp_pro_minute > 0
        ):
            return zone.delta_pp_pro_minute, "manuell"

        if not hasattr(self, "_wirkungsrate_cache"):
            self._wirkungsrate_cache: dict[
                str, tuple[datetime, float, str]
            ] = {}
        jetzt = jetzt or datetime.now()
        cached = self._wirkungsrate_cache.get(zone.zone_id)
        if cached is not None:
            cache_zeit, cache_wert, cache_quelle = cached
            if (
                (jetzt - cache_zeit).total_seconds()
                < self._WIRKUNGSRATE_CACHE_TTL_S
            ):
                return cache_wert, cache_quelle

        # DB-Lookup: letzte 20 Wirkungsrate-Eintraege, dann auf Fenster
        # filtern. Defensiv: SpeicherAttrappen in Tests haben evtl. keine
        # `hole_kalibrierungen`-Methode → Fallback auf Default.
        try:
            eintraege = await self._speicher.hole_kalibrierungen(
                zone.zone_id, typ="wirkungsrate", limit=20,
            )
        except (AttributeError, Exception) as exc:
            logger.error(
                "wirkungsrate.kalibrierung_fehler", zone_id=zone.zone_id,
                fehler=str(exc),
            )
            self._wirkungsrate_cache[zone.zone_id] = (
                jetzt, DEFAULT_DELTA_PP_PRO_MINUTE, "default",
            )
            return DEFAULT_DELTA_PP_PRO_MINUTE, "default"

        grenze = jetzt - timedelta(days=self._WIRKUNGSRATE_KALIBRIER_FENSTER_TAGE)
        aktuelle = [
            float(e["wert"])
            for e in eintraege
            if datetime.fromisoformat(e["zeitstempel"]) >= grenze
        ]
        if len(aktuelle) >= self._WIRKUNGSRATE_MIN_KALIBRIER_N:
            wert = round(statistics.median(aktuelle), 3)
            quelle = "kalibrierung"
        else:
            wert = DEFAULT_DELTA_PP_PRO_MINUTE
            quelle = "default"

        self._wirkungsrate_cache[zone.zone_id] = (jetzt, wert, quelle)
        return wert, quelle

    async def _aufgeloeste_wirkung(
        self, zone: ZonenKonfig, jetzt: datetime | None = None,
    ) -> tuple[float | None, float | None, str]:
        """T-0292 Stufe 2: liefert (wmax, r0) fuer das Plateau-Modell in
        `_berechne_dauer`.

        Returns (None, None, "konfig"), wenn KEIN Override gilt -- bei
        `adoptieren=False` (Default) ODER ohne angenommenen, frischen Fit.
        Der Aufrufer reicht (None, None) als `wmax_override`/`r0_override`
        durch; `_berechne_dauer` greift dann auf die Konfig-Werte zu
        (perfekter No-op). Returns (wmax, r0, "fit") nur bei adoptierbarem
        Fit (angenommen=True + juenger als `max_fit_alter_tage`).

        TTL-Cache 6 h pro Zone analog `_aufgeloeste_wirkungsrate`.
        """
        if not self._wirkung_fit_konfig.adoptieren:
            return None, None, "konfig"

        if not hasattr(self, "_wirkung_fit_cache"):
            self._wirkung_fit_cache: dict[
                str, tuple[datetime, float | None, float | None, str]
            ] = {}
        jetzt = jetzt or datetime.now()
        cached = self._wirkung_fit_cache.get(zone.zone_id)
        if cached is not None:
            cache_zeit, c_wmax, c_r0, c_quelle = cached
            if (
                (jetzt - cache_zeit).total_seconds()
                < self._WIRKUNG_FIT_CACHE_TTL_S
            ):
                return c_wmax, c_r0, c_quelle

        def _konfig() -> tuple[None, None, str]:
            self._wirkung_fit_cache[zone.zone_id] = (
                jetzt, None, None, "konfig",
            )
            return None, None, "konfig"

        try:
            fit = await self._speicher.hole_wirkung_fit(zone.zone_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "wirkung_fit.lese_fehler", zone_id=zone.zone_id,
                fehler=str(exc),
            )
            return _konfig()

        if not fit or not fit.get("angenommen"):
            return _konfig()
        # Frische-Pruefung: zu alte Fits nicht adoptieren.
        gefittet_am = fit.get("gefittet_am")
        if gefittet_am:
            try:
                alter = (jetzt - datetime.fromisoformat(gefittet_am)).days
                if alter > self._wirkung_fit_konfig.max_fit_alter_tage:
                    return _konfig()
            except (TypeError, ValueError):
                pass
        wmax = fit.get("wmax")
        r0 = fit.get("r0")
        if not wmax or not r0 or wmax <= 0 or r0 <= 0:
            return _konfig()

        # T-0292/T-0416 (21.07.): Ueberschreibt der Fit einen von Hand
        # kalibrierten Konfig-Wert, muss das SICHTBAR sein. Realfall: die
        # bambuswald-Rate 0.33 stammt aus einem kontrollierten Testlauf mit
        # vorab fixierter Entscheidungsregel (T-0326) -- eine bessere Evidenz
        # als ein Fit ueber vermischte Betriebsdaten. Wenn `adoptieren` je auf
        # true geht, waere so eine Messung sonst still weg: kein Log, kein
        # Diff, keine Chance es zu bemerken.
        k_wmax = zone.wirkung_max_pp
        k_r0 = zone.wirkungsrate_initial
        if k_r0 and abs(float(r0) - float(k_r0)) / float(k_r0) > 0.25:
            logger.warning(
                "wirkung_fit.ueberschreibt_konfig_deutlich",
                zone_id=zone.zone_id,
                konfig_r0=k_r0, fit_r0=round(float(r0), 3),
                konfig_wmax=k_wmax, fit_wmax=round(float(wmax), 2),
                gefittet_am=gefittet_am,
                hinweis=(
                    "Fit weicht >25% vom Konfig-Wert ab und ersetzt ihn. "
                    "Bei handkalibrierten Zonen pruefen, welche Evidenz "
                    "staerker ist (TASK.md T-0292)."
                ),
            )
        self._wirkung_fit_cache[zone.zone_id] = (
            jetzt, float(wmax), float(r0), "fit",
        )
        return float(wmax), float(r0), "fit"

    async def _hole_kalibrier_referenzen(
        self, zone: ZonenKonfig, jetzt: datetime | None = None,
    ) -> tuple[float | None, str, float | None, str]:
        """Welkepunkt + Feldkapazitaet + Quellen aufloesen.

        Welkepunkt-Prioritaet:
          0. aktives FeuchteRegime mit `welkepunkt`         -> "regime"
             (T-0135 / H-4 Stufe 1b -- Magerwiese-Sommer-
             Trockenphase darf einen ganz anderen Stress-
             Punkt haben als Bambus oder Etabliert-Phase.)
          1. zone.welkepunkt (manueller Override)           -> "manuell"
          2. Median aus feldkapazitaet_messung
             typ='welkepunkt_proxy' bei n >= 3              -> "kalibrierung"
          3. p10(Tagesmin der letzten 90 Tage) - 3 pp       -> "tagesmin_schaetzung"
          4. effektiv_feuchte_kritisch (Regime-aware)       -> "feuchte_kritisch_fallback"
          5. None                                           -> "keine"

        Feldkapazitaet:
          - Median aus feldkapazitaet_messung typ='feldkapazitaet'
            bei n >= 3                                     -> "kalibrierung"
          - sonst None                                      -> "keine"
        """
        eff_jetzt = jetzt or datetime.now()

        # 0. Regime-Override (T-0135 / H-4 Stufe 1b)
        regime_welke = effektiv_welkepunkt(zone, eff_jetzt)
        if regime_welke is not None:
            welke_wert: float | None = float(regime_welke)
            welke_quelle = "regime"
        elif zone.welkepunkt is not None:
            # 1. Manueller Override
            welke_wert = float(zone.welkepunkt)
            welke_quelle = "manuell"
        else:
            welke_wert = None
            welke_quelle = "keine"

        # 2. Kalibrierungs-Median (Welkepunkt-Proxy)
        if welke_wert is None:
            try:
                kandidaten = await self._speicher.hole_kalibrierungen(
                    zone_id=zone.zone_id, typ="welkepunkt_proxy", limit=50,
                )
            except Exception:
                logger.exception(
                    "welke_referenz.kalibrierung_fehler", zone_id=zone.zone_id,
                )
                kandidaten = []
            werte = [float(k["wert"]) for k in kandidaten if k.get("wert") is not None]
            if len(werte) >= 3:
                werte_sort = sorted(werte)
                median = werte_sort[len(werte_sort) // 2]
                welke_wert = round(median, 1)
                welke_quelle = "kalibrierung"

        # 3. p10-Tagesmin-Schaetzung
        if welke_wert is None:
            schaetzung = await self._p10_tagesmin_schaetzung(zone.zone_id, jetzt)
            if schaetzung is not None:
                welke_wert = schaetzung
                welke_quelle = "tagesmin_schaetzung"

        # 4. feuchte_kritisch-Fallback (Regime-aware)
        if welke_wert is None:
            kritisch = effektiv_feuchte_kritisch(zone, eff_jetzt)
            if kritisch is not None:
                welke_wert = float(kritisch)
                welke_quelle = "feuchte_kritisch_fallback"

        # Feldkapazitaet — kein Manual-Override-Feld, nur Kalibrierung.
        fk_wert: float | None = None
        fk_quelle = "keine"
        try:
            fk_kandidaten = await self._speicher.hole_kalibrierungen(
                zone_id=zone.zone_id, typ="feldkapazitaet", limit=50,
            )
        except Exception:
            logger.exception(
                "fk_referenz.kalibrierung_fehler", zone_id=zone.zone_id,
            )
            fk_kandidaten = []
        fk_werte = [float(k["wert"]) for k in fk_kandidaten if k.get("wert") is not None]
        if len(fk_werte) >= 3:
            fk_sort = sorted(fk_werte)
            fk_wert = round(fk_sort[len(fk_sort) // 2], 1)
            fk_quelle = "kalibrierung"

        return welke_wert, welke_quelle, fk_wert, fk_quelle

    def _decay_pp_pro_tag(
        self, et0_24h_mm: float, niederschlag_24h_mm: float,
        regen_schwelle_mm: float,
    ) -> float:
        """Heuristische Trockenheits-Rate.

        Konsistent mit `_wetter_aenderung` (ET0_FEUCHTE_FAKTOR=2.0,
        REGEN_FEUCHTE_FAKTOR=4.0). Ueber 24 h:
          decay = ET0_FEUCHTE_FAKTOR × et0_24h
                  - REGEN_FEUCHTE_FAKTOR × max(0, regen_24h - schwelle)
        Mindestens 0.5 pp/Tag (sonst stagniert die Prognose und
        Welkepunkt waere "nie" erreicht — auch bei klarem Regen-Vorhersage
        zwingt das ET0 die Pflanzen zu langsamer Verdunstung).
        """
        et0_anteil = ET0_FEUCHTE_FAKTOR * max(0.0, et0_24h_mm)
        regen_brutto = max(0.0, niederschlag_24h_mm - regen_schwelle_mm)
        regen_anteil = REGEN_FEUCHTE_FAKTOR * regen_brutto
        return max(0.5, et0_anteil - regen_anteil)

    def _decay_prognose(
        self, aktuelle_feuchte: float, decay_pp_pro_tag: float,
        horizonte_h: list[int],
    ) -> dict[int, float]:
        """{horizont_h: prognose_feuchte} via linearen Decay.

        Ohne ML-Pfad — pure Heuristik. Geclippt auf [0, 100].
        """
        ergebnis: dict[int, float] = {}
        for h in horizonte_h:
            wert = aktuelle_feuchte - decay_pp_pro_tag * (h / 24.0)
            ergebnis[h] = max(0.0, min(100.0, round(wert, 1)))
        return ergebnis

    async def _prognose_ml_oder_heuristik(
        self,
        zone_id: str,
        aktuelle_feuchte: float,
        decay_pp_pro_tag: float,
        horizonte_h: list[int],
    ) -> tuple[dict[int, float], str, float]:
        """T-0105/T-0121: liefert Prognose-Dict + Quelle + effektive Decay-Rate.

        Bevorzugt ML (Niederschlag, ET0, Saison als Features mit drin).
        Fallback Heuristik wenn:
        - kein ML-Service injected
        - Service nicht `ist_verfuegbar`
        - keine Vorhersage fuer die Zone (z.B. Cluster nicht trainiert)
        - Drift-Ampel rot (Modell nicht verlaesslich)
        - Exception beim Aufruf

        Rueckgabe (decay_effektiv_pp_pro_tag):
        - Bei "ml": Decay aus (aktuelle_feuchte - prognose_24h). Wenn ML
          steigenden/stabilen Verlauf prognostiziert (Regen), wird
          `decay_min = max(0.5, 0.3 * heuristik_decay)` als konservative
          Untergrenze genutzt — sonst rechnet `_zeit_bis_grenze_aus_prognose`
          mit nahezu Null und die Reserve-Tage explodieren.
        - Bei "heuristik": unveraenderte Heuristik-Decay-Rate.
        """
        # Heuristik immer als Fallback bereit
        heuristik = self._decay_prognose(
            aktuelle_feuchte, decay_pp_pro_tag, horizonte_h,
        )

        ml = self._ml_vorhersage_service
        if ml is None or not getattr(ml, "ist_verfuegbar", False):
            return heuristik, "heuristik", decay_pp_pro_tag
        if self._konfig is None or self._speicher is None:
            return heuristik, "heuristik", decay_pp_pro_tag

        try:
            ergebnisse = await ml.live_vorhersage(
                zone_id, self._speicher, self._konfig,
            )
        except Exception:
            logger.exception(
                "vorhersage_zone.ml_prognose_fehler", zone_id=zone_id,
            )
            return heuristik, "heuristik", decay_pp_pro_tag

        if not ergebnisse:
            return heuristik, "heuristik", decay_pp_pro_tag

        # ergebnisse: dict mit keys "6h"/"12h"/"24h" -> Vorhersage-Objekte
        # mit feuchte_prognose + ggf. q10/q90.
        ml_werte: dict[int, float] = {}
        for h in (6, 12, 24):
            schluessel = f"{h}h"
            v = ergebnisse.get(schluessel)
            if v is not None and getattr(v, "feuchte_prognose", None) is not None:
                ml_werte[h] = max(
                    0.0, min(100.0, round(float(v.feuchte_prognose), 1))
                )

        if not ml_werte:
            return heuristik, "heuristik", decay_pp_pro_tag

        # ML-impliziter Decay aus dem 24h-Wert ableiten.
        # Ohne 24h-Wert: 12h-Wert (auf Tagesrate hochskaliert).
        if 24 in ml_werte:
            decay_ml = aktuelle_feuchte - ml_werte[24]
        elif 12 in ml_werte:
            decay_ml = (aktuelle_feuchte - ml_werte[12]) * 2.0
        elif 6 in ml_werte:
            decay_ml = (aktuelle_feuchte - ml_werte[6]) * 4.0
        else:
            decay_ml = decay_pp_pro_tag

        # T-0121: Bei steigendem/stabilem ML-Verlauf (z.B. Regen-Forecast)
        # wuerde decay_ml <= 0 die Reserve-Tage explodieren lassen. Konservative
        # Untergrenze: 30 % des Heuristik-Decays, mindestens 0.5 pp/Tag.
        decay_effektiv = max(0.5, max(decay_ml, 0.3 * decay_pp_pro_tag))

        ergebnis: dict[int, float] = {}
        for h in horizonte_h:
            if h in ml_werte:
                ergebnis[h] = ml_werte[h]
            elif h <= 24:
                # Zwischen-Horizont — ML-impliziten Decay verwenden.
                wert = aktuelle_feuchte - decay_effektiv * (h / 24.0)
                ergebnis[h] = max(0.0, min(100.0, round(wert, 1)))
            else:
                # Extrapolation > 24h: Basis = 24h-Wert, ML-Decay als Rate.
                rest_h = h - 24
                basis = ml_werte.get(24, aktuelle_feuchte - decay_effektiv)
                wert = basis - decay_effektiv * (rest_h / 24.0)
                ergebnis[h] = max(0.0, min(100.0, round(wert, 1)))

        return ergebnis, "ml", decay_effektiv

    def _zeit_bis_grenze_aus_prognose(
        self,
        aktuelle_feuchte: float,
        grenze: float | None,
        prognose: dict[int, float],
        decay_pp_pro_tag: float,
    ) -> float | None:
        """Tage bis prognose_feuchte <= grenze, basiert auf Prognose-Dict.

        Sucht den ersten Horizont bei dem die Prognose unter `grenze`
        faellt; interpoliert linear. Wenn die Prognose den Grenzwert
        innerhalb des Horizonts NICHT unterschreitet, extrapoliert mit
        `decay_pp_pro_tag` ab dem letzten Horizont weiter.
        """
        if grenze is None or decay_pp_pro_tag <= 0.0:
            return None
        if aktuelle_feuchte <= grenze:
            return 0.0

        sortierte_horizonte = sorted(prognose.keys())
        vorher_h = 0
        vorher_wert = aktuelle_feuchte
        for h in sortierte_horizonte:
            wert = prognose[h]
            if wert <= grenze:
                # Linear interpolieren zwischen (vorher_h, vorher_wert)
                # und (h, wert).
                if vorher_wert == wert:
                    return round(h / 24.0, 1)
                anteil = (vorher_wert - grenze) / (vorher_wert - wert)
                interpoliert_h = vorher_h + (h - vorher_h) * anteil
                return round(interpoliert_h / 24.0, 1)
            vorher_h = h
            vorher_wert = wert

        # Prognose hat Grenze nicht unterschritten — extrapolieren
        # ab letztem Wert mit decay_pp_pro_tag.
        if vorher_wert <= grenze:
            return round(vorher_h / 24.0, 1)
        rest_pp = vorher_wert - grenze
        rest_tage = rest_pp / decay_pp_pro_tag
        return round(vorher_h / 24.0 + rest_tage, 1)

    def _zeit_bis_welkepunkt(
        self, aktuelle_feuchte: float, welkepunkt: float | None,
        decay_pp_pro_tag: float,
    ) -> float | None:
        """Tage bis prognose_feuchte <= welkepunkt (linearer Decay).

        None wenn welkepunkt unbekannt, decay <= 0 (Pflanze wird
        feuchter), oder Sensor schon unter Welkepunkt (dann 0.0).
        """
        if welkepunkt is None or decay_pp_pro_tag <= 0.0:
            return None
        if aktuelle_feuchte <= welkepunkt:
            return 0.0
        return round((aktuelle_feuchte - welkepunkt) / decay_pp_pro_tag, 1)

    async def _physik_reserve_tage(
        self,
        zone: ZonenKonfig,
        feuchte: float,
        welkepunkt: float | None,
        jetzt: datetime,
    ) -> float | None:
        """T-0279 Phase 2: Reserve in Tagen bis zur Komfort-Unterkante
        (optimum_min) aus der Physik-Prognose (exp-Decay zum Welkepunkt
        als Asymptote, ET0-moduliert).

        Robuste Quelle fuer den proaktiven Bewaesserungs-Trigger --
        anders als die ML-24h-Prognose (Mean-Reversion-Halluzination,
        T-0278) ist der Physik-Decay monoton + stabil.

        Bezugslinie ist optimum_min, NICHT der Welkepunkt: der Welkepunkt
        ist die Decay-Asymptote, "Tage bis Welkepunkt" ist als Trigger-
        Metrik unbrauchbar (asymptotisch quasi unendlich bzw. linear
        konstant -- Metrik-Redesign 31.05.). Der Welkepunkt bleibt als
        ODE-Asymptote noetig.

        None wenn: kein Welkepunkt, kein optimum_min, Physik-Diagnose
        inaktiv, k_basis nicht aufloesbar. Dann faellt der proaktive
        Trigger aus (Default-Verhalten).
        """
        if welkepunkt is None or self._konfig is None:
            return None
        ziel_feuchte = effektiv_optimum_min(zone, jetzt)
        if ziel_feuchte is None:
            return None
        from bewaesserung.ml.physik_diagnose import (
            hole_et0_zukunft,
            loese_k_basis,
        )
        from bewaesserung.ml.physik_trocknung import (
            tage_bis_zielfeuchte_physik,
        )
        try:
            aufloesung = await loese_k_basis(
                zone=zone, speicher=self._speicher, konfig=self._konfig,
            )
        except Exception:
            logger.exception(
                "physik_reserve.k_basis_fehler", zone_id=zone.zone_id,
            )
            return None
        if aufloesung is None:
            return None
        k_basis, et0_basis, _quelle = aufloesung
        et0_zukunft = await hole_et0_zukunft(
            zone_id=zone.zone_id,
            konfig=self._konfig,
            wetter_manager=self._wetter_manager,
        )
        # T-0279 Phase 2: feucht-liebende Multi-Sensor-Zonen folgen dem
        # TROCKENSTEN gemappten Sensor (statt Median), damit das
        # konservative Gardena-Warnsignal nicht verwaessert wird.
        f_start = feuchte
        if getattr(zone, "proaktiv_min_sensor", False):
            try:
                f_min = await self._speicher.min_gemappte_feuchte(
                    zone.zone_id, jetzt=jetzt,
                )
            except Exception:
                logger.exception(
                    "physik_reserve.min_feuchte_fehler",
                    zone_id=zone.zone_id,
                )
                f_min = None
            if f_min is not None:
                f_start = f_min
        return tage_bis_zielfeuchte_physik(
            f_start=f_start,
            ziel_feuchte=ziel_feuchte,
            welkepunkt=welkepunkt,
            k_basis_pro_h=k_basis,
            et0_basis_mm_pro_h=et0_basis,
            et0_zukunft_pro_h=et0_zukunft,
        )

    # T-0279 Phase 1b: Untergrenze fuer eine "adoptierbare" Zieldosis.
    # Unter ~2 pp Differenz zum Strategie-Ziel ist ein Top-Up nicht
    # sinnvoll (Gardena-Quantisierung ~5 pp -> waere Rauschen, und der
    # MIN-Clip wuerde einen verwirrenden "1 min"-Vorschlag erzeugen).
    MIN_ZIELDOSIS_DIFFERENZ_PP = 2.0

    async def _hypothetische_zieldosis_s(
        self,
        zone: ZonenKonfig,
        feuchte: float,
        ziel_feuchte_roh: float | None,
        et0_6h: float,
        jetzt: datetime,
    ) -> int | None:
        """T-0279 Phase 1b: adoptierbare Zieldosis (Sekunden) AUCH bei
        kein_bedarf -- Zeit, um die Feuchte auf das Strategie-Ziel zu
        heben. Quelle fuer die globale 1-Klick-Uebernahme (Bambus etc.),
        damit der Frontend-Banner ueberall eine sinnvolle Dauer hat.

        None wenn kein Ziel vorliegt oder die Differenz zum Ziel zu klein
        ist (`MIN_ZIELDOSIS_DIFFERENZ_PP`) -- kein MIN-Clip-"1 min"-
        Vorschlag. Bewusst NUR Heuristik-Dauer (Wirkungsrate +
        Plateau-Modell), KEINE ML-24h-Feuchte-Prognose (T-0278).
        """
        if ziel_feuchte_roh is None:
            return None
        if ziel_feuchte_roh - feuchte < self.MIN_ZIELDOSIS_DIFFERENZ_PP:
            return None
        delta_pp, _quelle = await self._aufgeloeste_wirkungsrate(zone, jetzt)
        wmax_wert, r0_wert, _wf_quelle = await self._aufgeloeste_wirkung(
            zone, jetzt,
        )
        dauer = self._berechne_dauer(
            zone, feuchte, et0_6h,
            ziel_schwelle=ziel_feuchte_roh,
            delta_pp_pro_minute_override=delta_pp,
            clip_auf_max=True,
            wmax_override=wmax_wert, r0_override=r0_wert,
        )
        return dauer if dauer and dauer > 0 else None

    async def _kuerzlich_gegossen(
        self, zone: ZonenKonfig, jetzt: datetime,
        karenz_h: float | None = None,
    ) -> bool:
        """T-0286: True wenn ein Ground-Truth-Lauf (live/manuell/DHS)
        innerhalb `zone.versickerungs_karenz_stunden` vor `jetzt` ein
        SCHLIESSEN hatte. Sensor-Heuristik + IGNORIERT zaehlen NICHT
        (kein echter Lauf -- Vorbild `sensor_backfill._ground_truth_
        noch_am_wirken`).

        Dient als Recency-Guard fuer den proaktiven Trigger: bei Multi-
        Sensor-Zonen mit unkorrelierten Sensoren reagiert direkt nach dem
        Giessen nur die Beregnungszone (Gardena), die traegen Sensoren
        (FYTA, andere Mikro-Standorte) hinken Stunden nach. min-Sensor +
        Median zeigen dann noch "trocken" -> ohne Guard wuerde der
        proaktive Trigger erneut giessen wollen (Realfall T-0285). Das
        Karenz-Fenster ist genau das Sensor-Nachlauf-Fenster der Zone.
        """
        # T-0414: `karenz_h` explizit uebergeben (Pause-Bypass nutzt eine
        # eigene, kuerzere Karenz); ohne Angabe gilt weiter die
        # Versickerungs-Karenz der Zone (Bestandsverhalten).
        if karenz_h is None:
            karenz_h = getattr(zone, "versickerungs_karenz_stunden", None)
        if not karenz_h or karenz_h <= 0:
            return False
        von = jetzt - timedelta(hours=float(karenz_h))
        try:
            ereignisse = await self._speicher.hole_ventil_ereignisse(
                zone.zone_id, von=von, bis=jetzt,
            )
        except Exception:
            logger.exception(
                "kuerzlich_gegossen.fehler", zone_id=zone.zone_id,
            )
            return False
        for e in ereignisse:
            if e.aktion != VentilAktion.SCHLIESSEN:
                continue
            if e.ventil_id == "sensor_heuristik":
                continue
            if e.ausloser == Ausloser.IGNORIERT:
                continue
            return True
        return False

    async def _pause_bypass_bei_kritisch(
        self, zone: ZonenKonfig, ist_kritisch: bool, jetzt: datetime,
    ) -> bool:
        """T-0378: darf kritische Trockenheit die min_pause ueberspringen?

        User-Entscheidung 02.07.: "Ja, kritische Trockenheit schlaegt
        min_pause. Es geht um das Pflanzenwohl, nicht willkuerliche
        Einstellungen." Schliesst die in T-0354 notierte Restluecke -- die
        Notreserve griff bis hier erst NACH Pause-Ablauf.

        KEIN nackter Bypass. min_pause deckt auch den Sensor-Nachlauf ab
        (~1 h bis die Wirkung sichtbar ist, s. Memory
        `domain_giesswirkung_sensor_verzoegerung`). Direkt nach einem
        ECHTEN Lauf liest ein traeger Sensor noch "kritisch" -- ohne Guard
        waere die Folge eine sofortige Doppel-Dose. Deshalb:

            ist_kritisch UND NICHT _kuerzlich_gegossen

        `_kuerzlich_gegossen` (T-0286) fragt nach einem Ground-Truth-Lauf im
        Karenz-Fenster; Sensor-Heuristik + IGNORIERT zaehlen dort bewusst nicht.

        **T-0414: das Fenster ist `pause_bypass_karenz_stunden` (Default 1 h),
        NICHT `versickerungs_karenz_stunden`.** Mit letzterem (3 h) war der
        Bypass fuer jede Zone mit `min_pause <= 3 h` wirkungslos: der Guard
        rechnet ab dem SCHLIESSEN, die Pause ab dem OEFFNEN -- der Guard
        ueberlebte die Pause also immer. Gemessen fuer bambuswald
        (Pause 120 min, Karenz 180 min): bei 60/90/110 min nach OEFFNEN
        jedes Mal PAUSE_AKTIV, Bypass nie. 1 h entspricht der dokumentierten
        Begruendung (Sensor-Nachlauf); die 3 h stammen aus T-0071 und dienen
        einem anderen Zweck.

        Der Runaway-Schutz bleibt trotzdem intakt: nach einer langen Dose
        liegt das Karenz-Ende weiterhin hinter dem Pause-Ende. Beispiel
        bambuswald, 90-min-Dose: Close bei t0+90, Karenz bis t0+150, Pause
        bis t0+120 -> kein Bypass. Genau richtig -- nach 90 min Wasser sofort
        nachzulegen ist der Runaway, den der Guard verhindern soll.

        Zweite Schranke bleibt das Tagesbudget: der Budget-Check laeuft an
        allen drei Aufrufstellen VOR dem Pause-Check, ein Stuck-low-Sensor
        kann also auch mit Pause-Bypass nicht endlos giessen. (Bei
        bambuswald/-yogaraum steht `tages_budget_kritisch_faktor` auf 1.0,
        d.h. der Bypass bekommt dort nicht einmal Zusatzbudget; nur hecke
        hat 1.5.)
        """
        if not ist_kritisch:
            return False
        karenz_h = getattr(zone, "pause_bypass_karenz_stunden", 1.0)
        if await self._kuerzlich_gegossen(zone, jetzt, karenz_h=karenz_h):
            return False
        logger.info(
            "entscheidung.pause_bypass_kritisch",
            zone_id=zone.zone_id,
            feuchte_kritisch=zone.feuchte_kritisch,
            bypass_karenz_stunden=karenz_h,
            min_pause_minuten=zone.min_pause_minuten,
            zeit=jetzt.isoformat(),
        )
        return True

    async def _strategie_verdict_pro_zone(
        self,
        zone: ZonenKonfig,
        feuchte: float,
        et0_24h: float,
        niederschlag_24h: float,
        jetzt: datetime,
    ):
        """T-0231 Phase 3: pro Zone die volle Pre-Berechnung +
        `entscheide_pro_zone`-Auswertung. Ein Aufruf, ein Verdict.

        Wird von `pruefe_kanal` pro Bedarfs-Zone gerufen, um zu
        entscheiden ob der heutige Schwellen-Trigger (feuchte < schwelle)
        auch nach Strategie/Welkepunkt-Logik ein 'soll_bewaessern=True'
        ergibt. KONSTANT_NIEDRIG- und SELTEN_GROSS-Zonen sagen typisch
        'kein_bedarf' auch unter Schwelle -- der Auto-Loop konvergiert
        damit auf das Dashboard-Verdict (Voraussetzung T-0021).

        Spiegelt die Vorbereitung in `vorhersage_zone`-Pfad (siehe
        entscheidung.py:1808-1828): Kalibrier-Referenzen + Decay +
        ML-/Heuristik-Prognose + tage_bis_welke. Verhalten ist mit
        vorhersage_zone identisch (Phase 1 + Phase 2 bewiesen).
        """
        from bewaesserung.entscheidung_pro_zone import (
            ProZoneKontext,
            entscheide_pro_zone,
        )
        welkepunkt_wert, _wq, fk_wert, _fq = (
            await self._hole_kalibrier_referenzen(zone, jetzt)
        )
        decay_basis = self._decay_pp_pro_tag(
            et0_24h_mm=et0_24h,
            niederschlag_24h_mm=niederschlag_24h,
            regen_schwelle_mm=self._regen_schwelle_mm(),
        )
        prognose, _pq, prognose_decay = (
            await self._prognose_ml_oder_heuristik(
                zone.zone_id, feuchte, decay_basis, [6, 12, 24],
            )
        )
        tage_bis_welke = self._zeit_bis_welkepunkt(
            feuchte, welkepunkt_wert, prognose_decay,
        )
        # T-0279 Phase 2: robuste Physik-Reserve (bis optimum_min) fuer
        # den proaktiven Trigger.
        tage_bis_proaktiv = await self._physik_reserve_tage(
            zone, feuchte, welkepunkt_wert, jetzt,
        )
        # T-0286: Recency-Guard fuer den proaktiven Trigger.
        kuerzlich_gegossen = await self._kuerzlich_gegossen(zone, jetzt)
        kontext = ProZoneKontext(
            aktuelle_feuchte=feuchte,
            prognose=prognose,
            welkepunkt_wert=welkepunkt_wert,
            tage_bis_welke=tage_bis_welke,
            fk_wert=fk_wert,
            sicherheits_tage_konfig=zone.sicherheits_tage,
            decay_pp_pro_tag=prognose_decay,
            tage_bis_proaktiv=tage_bis_proaktiv,
            kuerzlich_gegossen=kuerzlich_gegossen,
        )
        return entscheide_pro_zone(zone, kontext, jetzt)

    def _dauer_fuer_sicherheitsabstand(
        self, zone: ZonenKonfig, aktuelle_feuchte: float,
        welkepunkt: float, sicherheits_tage: float,
        decay_pp_pro_tag: float, et0_6h: float,
        delta_pp_pro_minute_override: float | None = None,
        ziel_feuchte_override: float | None = None,
        clip_auf_max: bool = True,
        wmax_override: float | None = None,
        r0_override: float | None = None,
    ) -> int:
        """Welche Dauer bringt die Feuchte so weit hoch, dass nach
        `sicherheits_tage` × `decay_pp_pro_tag` Trockenheit die Feuchte
        immer noch >= welkepunkt + 5 pp Puffer ist?

        T-0089 Schritt B: Ziel-Feuchte = max(welkepunkt-Reserve,
        Pflanzen-Optimum-Max). User-Mental-Model "bis ins Wohlfuehl-
        Maximum giessen" wird damit eingehalten, ohne den Welkepunkt-
        Schutz zu schwaechen — bei sehr hohem Decay (z.B. Hitzewelle)
        kann die Reserve weiterhin hoeher als das Optimum-Max liegen
        und gewinnt dann.

        T-0085: `delta_pp_pro_minute_override` reicht den vom Caller
        bereits aufgeloesten Wirkungsrate-Wert (z.B. Kalibrierungs-
        Median) an `_berechne_dauer` durch.

        T-0103: `ziel_feuchte_override` reicht den strategie-spezifischen
        Ziel-Wert aus `entscheide_pro_zone` durch (HAEUFIG_KLEIN
        zielt auf optimum_max, SELTEN_GROSS auf Feldkapazitaet,
        KONSTANT_NIEDRIG auf optimum_min). Wenn None: heutige Logik
        max(welkepunkt-Reserve, optimum_max) gilt (Backward-Compat
        fuer KORRIDOR).

        Nutzt `_berechne_dauer` mit angepasster Ziel-Schwelle. Geclippt
        auf [MIN_DAUER_SEKUNDEN, zone.max_dauer_sekunden].
        """
        if ziel_feuchte_override is not None:
            ziel_feuchte = ziel_feuchte_override
        else:
            ziel_reserve = welkepunkt + 5.0 + decay_pp_pro_tag * sicherheits_tage
            if zone.optimum_feuchte_max is not None:
                ziel_feuchte = max(ziel_reserve, zone.optimum_feuchte_max)
            else:
                ziel_feuchte = ziel_reserve
        ziel_feuchte = max(0.0, min(100.0, ziel_feuchte))
        if ziel_feuchte <= aktuelle_feuchte:
            # Schon ueber Ziel — Mindest-Dauer reicht (oder 0, je nach Caller).
            return MIN_DAUER_SEKUNDEN
        return self._berechne_dauer(
            zone, aktuelle_feuchte, et0_6h, ziel_schwelle=ziel_feuchte,
            delta_pp_pro_minute_override=delta_pp_pro_minute_override,
            clip_auf_max=clip_auf_max,
            wmax_override=wmax_override, r0_override=r0_override,
        )

    def _baue_erklarung(
        self, *, aktuelle_feuchte: float, welkepunkt: float | None,
        welkepunkt_quelle: str, optimum_min: float | None,
        optimum_max: float | None, prognose: dict[int, float],
        tage_bis_welke: float | None, dauer_s: int | None,
        deckung_tage: float | None, niederschlag_24h_mm: float,
        empfehlungs_typ: str,
        strategie: BewaesserungsStrategie = BewaesserungsStrategie.KORRIDOR,
        tage_bis_grenze: float | None = None,
        reserve_grenze_label: str = "Welkepunkt",
    ) -> tuple[str, str]:
        """Erzaehlende Erklaerung (kurz, lang) fuers Frontend-Panel.

        T-0103: strategie-aware. Pro Strategie unterschiedliche Texte —
        bei KONSTANT_NIEDRIG erklaert `kein_bedarf` als bewusste
        Trockenphase, bei SELTEN_GROSS sind `akut`-Empfehlungen
        Tiefen-Bewaesserung.
        """
        # Quelle nur in Klammern wenn nicht "manuell" — fuer manuelle
        # Konfig-Werte ist die Herkunft selbsterklaerend.
        quelle_label = ""
        if welkepunkt is not None and welkepunkt_quelle not in ("manuell", "keine"):
            label_map = {
                "kalibrierung": " (aus Kalibrierungs-Median)",
                "tagesmin_schaetzung": " (Schaetzung aus Sensor-Historie)",
                "feuchte_kritisch_fallback": " (aus Konfig, kein Sensor-Signal)",
            }
            quelle_label = label_map.get(welkepunkt_quelle, "")

        sensor_text = f"Sensor {aktuelle_feuchte:.0f}%"
        welke_text = (
            f"Welkepunkt {welkepunkt:.0f}%{quelle_label}"
            if welkepunkt is not None else "Welkepunkt unbekannt"
        )
        wohl_text = (
            f"Wohlfuehlbereich {optimum_min:.0f}-{optimum_max:.0f}%"
            if optimum_min is not None and optimum_max is not None else None
        )

        if empfehlungs_typ == "akut":
            kurz = f"akut — {dauer_s // 60 if dauer_s else 0} min"
            if deckung_tage is not None:
                kurz += f" fuer {deckung_tage:.0f} Tage Reserve"
            lang_teile = [f"{sensor_text}, {welke_text}."]
            if tage_bis_welke is not None and tage_bis_welke <= 1.0:
                lang_teile.append("Welkepunkt heute/morgen erreicht.")
            elif tage_bis_welke is not None:
                lang_teile.append(f"Welkepunkt in ~{tage_bis_welke:.1f} Tagen.")
            if dauer_s and deckung_tage is not None:
                lang_teile.append(
                    f"{dauer_s // 60} min jetzt → deckt etwa {deckung_tage:.0f} Tage."
                )
            return kurz, " ".join(lang_teile)

        if empfehlungs_typ == "praeventiv":
            kurz = f"praeventiv — {dauer_s // 60 if dauer_s else 0} min"
            if deckung_tage is not None:
                kurz += f" fuer {deckung_tage:.0f} Tage Reserve"
            lang_teile = [f"{sensor_text}, {welke_text}."]
            if wohl_text:
                lang_teile[-1] = f"{sensor_text}. {wohl_text}, {welke_text}."
            # Prognose-Aussage
            p24 = prognose.get(24)
            p48 = prognose.get(48)
            if p24 is not None and p48 is not None:
                lang_teile.append(
                    f"Ohne Giessen morgen frueh {p24:.0f}%, uebermorgen {p48:.0f}%."
                )
            elif p24 is not None:
                lang_teile.append(f"Ohne Giessen morgen frueh {p24:.0f}%.")
            if dauer_s and deckung_tage is not None:
                lang_teile.append(
                    f"{dauer_s // 60} min jetzt → deckt etwa {deckung_tage:.0f} Tage."
                )
            if niederschlag_24h_mm > 0.5:
                lang_teile.append(
                    f"({niederschlag_24h_mm:.1f} mm Regen 24 h einberechnet.)"
                )
            return kurz, " ".join(lang_teile)

        # T-0103: wohlfuehl_grenze (KORRIDOR sanfter Hinweis,
        # bei HAEUFIG_KLEIN wird stattdessen 'praeventiv' geliefert).
        if empfehlungs_typ == "wohlfuehl_grenze":
            kurz = (
                f"Wohlfuehl-Grenze — {dauer_s // 60 if dauer_s else 0} min waere optimal"
            )
            lang_teile = [sensor_text]
            if wohl_text:
                lang_teile[-1] = f"{sensor_text}. {wohl_text}."
            p24 = prognose.get(24)
            if optimum_min is not None and aktuelle_feuchte < optimum_min:
                lang_teile.append(
                    f"Sensor unter Wohlfuehl-Min ({optimum_min:.0f}%), "
                    f"aber Welkepunkt-Reserve noch {tage_bis_welke:.0f} Tage."
                )
            elif p24 is not None and optimum_min is not None and p24 < optimum_min:
                lang_teile.append(
                    f"Prognose morgen {p24:.0f}% unter Wohlfuehl-Min ({optimum_min:.0f}%)."
                )
            lang_teile.append("Kein Notfall, aber Hinweis.")
            return kurz, " ".join(lang_teile)

        # kein_bedarf
        # T-0103: bei KONSTANT_NIEDRIG ist Trockenphase ein Feature, nicht
        # ein Mangel — Erklaerung explizit.
        if strategie == BewaesserungsStrategie.KONSTANT_NIEDRIG:
            kurz = "Trockenphase aktiv — kein Bedarf"
            if tage_bis_welke is not None:
                kurz += f" (Reserve {tage_bis_welke:.0f} Tage)"
            lang_teile = [sensor_text]
            if welkepunkt is not None:
                lang_teile.append(
                    f"{welke_text}. Bewusst niedrig gehalten — "
                    f"Konkurrenz-Pflanzen werden gehemmt."
                )
            return kurz, " ".join(lang_teile)

        # T-0105 + User-Befund 30.04.: bei HAEUFIG_KLEIN ist die strategische
        # Untergrenze Wohl-Min (nicht Welkepunkt). Reserve-Aussage muss das
        # spiegeln, sonst irritiert "Reserve 4 Tage ueber Welkepunkt"
        # waehrend man im Wohlfuehlbereich bleiben moechte.
        ist_haeufig_klein = strategie == BewaesserungsStrategie.HAEUFIG_KLEIN
        bezug_tage = tage_bis_grenze if ist_haeufig_klein else tage_bis_welke
        bezug_label = reserve_grenze_label if ist_haeufig_klein else "Welkepunkt"

        kurz = "kein Bedarf"
        if bezug_tage is not None:
            kurz += f" — Reserve {bezug_tage:.0f} Tage"
        lang_teile = [sensor_text]
        if wohl_text:
            lang_teile[-1] = f"{sensor_text}, im {wohl_text}"
        if bezug_tage is not None:
            lang_teile.append(
                f"Prognose ~{bezug_tage:.0f} Tage ueber {bezug_label}."
            )
        if welkepunkt is not None and not ist_haeufig_klein:
            lang_teile.insert(-1, f"{welke_text},")
        return kurz, " ".join(lang_teile) + ("." if not lang_teile[-1].endswith(".") else "")

    async def _drift_ampel(
        self, zone_id: str,
    ) -> tuple[str | None, float | None, float | None, int | None]:
        """Drift-Ampel aus den letzten 30 Tagen (T-0066).

        Rueckgabe `(ampel, mae_heuristik, mae_ml, n_bewertet)`. Ampel-
        Schwellen analog `/api/ml/dauer-drift`:
          - n < 5 oder ML-MAE fehlt → None (zu wenig Daten, keine Ampel)
          - ml <= 0.5 × heuristik → "gruen"
          - ml <= 0.8 × heuristik → "gelb"
          - sonst                 → "rot"
        """
        try:
            metriken = await self._speicher.hole_dauer_drift_metriken(
                zone_id=zone_id, fenster_tage=30,
            )
        except Exception:
            logger.exception("vorhersage_zone.drift_fehler", zone_id=zone_id)
            return None, None, None, None
        z = metriken.get(zone_id)
        if not z:
            return None, None, None, None
        mae_h = z.get("mae_heuristik")
        mae_m = z.get("mae_ml")
        n = z.get("n_bewertet")
        if n is None or n < 5 or mae_h is None or mae_m is None:
            return None, mae_h, mae_m, n
        if mae_m <= 0.5 * mae_h:
            ampel = "gruen"
        elif mae_m <= 0.8 * mae_h:
            ampel = "gelb"
        else:
            ampel = "rot"
        return ampel, mae_h, mae_m, n

    async def vorhersage_zone(
        self, zone_id: str,
        sicherheits_tage_override: float | None = None,
    ) -> GiessEmpfehlung:
        """Dry-Run-Empfehlung fuer eine Zone — ohne DB-Writes (T-0066).

        Spiegelt die Blocker-Kaskade aus `pruefe_zone`, aber:
          - Keine `_erstelle_entscheidung`-Row im entscheidung_log.
          - Kein `speichere_dauer_vorschlag` (Shadow-Persistenz aus).
          - Kein `_pause_eingehalten`-Anker-Update.

        Falls die Kaskade ein `soll_bewaessern=True` ergibt, wird die
        Heuristik-Dauer berechnet und (falls ML aktiv) das Response-
        Modell konsultiert — beides rein berechnend. Drift-Ampel aus
        `ml_dauer_vorschlag`-Historie.

        Isomorphie zu `pruefe_zone`: Jeder Blocker-Zweig dort hat hier
        einen Spiegel-Return. Wenn pruefe_zone um einen neuen Blocker
        erweitert wird, MUSS dieser Pfad mit (siehe CLAUDE.md-
        Fehler-Heuristiken + MEMORY: arbeitspattern_isomorphiecheck.md).
        """
        jetzt = self._jetzt()
        zone = self._zonen.get(zone_id)

        if zone is None:
            return GiessEmpfehlung(
                zone_id=zone_id, zeitstempel=jetzt,
                soll_bewaessern=False, grund="Zone unbekannt",
                ml_aktiv=self._response_konfig.aktiv,
                ml_wirksam=self._response_konfig.wirksam,
                ml_status_grund="zone_unbekannt",
            )

        ml_aktiv = bool(self._response_konfig.aktiv)
        ml_wirksam = bool(self._response_konfig.wirksam)

        if zone.modus == ZonenModus.MONITORING:
            # T-0182 (13.05.2026): trotzdem empfehlungs_typ klassifizieren,
            # damit Watchdog-Akut-Push auch fuer Monitoring-Zonen greift.
            # Vorher: empfehlungs_typ fiel auf Default "kein_bedarf" zurueck,
            # auch wenn der Sensor unter `feuchte_kritisch` lag (Realfall
            # Mandevilla 13.05.: 19 % bei Schwelle 28 / kritisch 18).
            # Minimale Klassifikation: aktuelle Feuchte vs. Schwellen.
            # `soll_bewaessern=False` bleibt (keine Auto-Bewaesserung).
            #
            # T-0271 (27.05.): zusaetzlich die kausalen Referenzen
            # (Welkepunkt + Feldkapazitaet) auf dem Stub-Pfad aufloesen.
            # Vorher uebersprang der monitoring-Branch
            # `_hole_kalibrier_referenzen` komplett -> Frontend zeigte fuer
            # zitrus_ii (welkepunkt=25 manuell konfiguriert!), magerwiese,
            # hecke, mandevilla, zitrus nie einen Welkepunkt; die
            # Hybrid-Stufe-1-Physik-Prognose (T-0269) konnte mangels
            # Welkepunkt nicht rechnen. Additive Befuellung, kein Effekt
            # auf `soll_bewaessern` / Blocker / Dauer.
            from bewaesserung.modelle import (
                effektiv_feuchte_kritisch as _eff_krit_mon,
                effektiv_schwelle_min as _eff_schw_mon,
            )
            robust = await self._robuste_feuchte(zone_id, jetzt)
            _eff_schwelle = _eff_schw_mon(zone, jetzt)
            _eff_kritisch = _eff_krit_mon(zone, jetzt)
            # T-0271: Welkepunkt + Feldkapazitaet robust holen. Fehler
            # darf die Monitoring-Empfehlung NICHT kippen -- diese ist
            # der Pflicht-Pfad fuer Watchdog-Akut-Push.
            wp_wert_mon: float | None = None
            wp_quelle_mon = "keine"
            fk_wert_mon: float | None = None
            fk_quelle_mon = "keine"
            try:
                wp_wert_mon, wp_quelle_mon, fk_wert_mon, fk_quelle_mon = (
                    await self._hole_kalibrier_referenzen(zone, jetzt)
                )
            except Exception:
                logger.exception(
                    "monitoring.kalibrier_referenzen_fehler",
                    zone_id=zone_id,
                )
            # T-0183: grund-Text mit aktuellen Werten, damit das Frontend
            # einen aussagekraeftigen Banner rendern kann ohne extra API.
            if robust is None:
                empf_typ_mon = "kein_bedarf"
                grund_mon = "Zone im Monitoring-Modus (keine gueltige Messung)"
            elif robust <= _eff_kritisch:
                empf_typ_mon = "akut"
                grund_mon = (
                    f"Sensor {robust:.0f}% unter kritisch {_eff_kritisch:.0f}% "
                    "- bitte manuell pruefen (Monitoring-Modus)"
                )
            elif robust < _eff_schwelle:
                empf_typ_mon = "praeventiv"
                grund_mon = (
                    f"Sensor {robust:.0f}% unter Schwelle {_eff_schwelle:.0f}% "
                    "- im Auge behalten (Monitoring-Modus)"
                )
            else:
                empf_typ_mon = "kein_bedarf"
                grund_mon = "Zone im Monitoring-Modus (Sensor im gruenen Bereich)"
            return GiessEmpfehlung(
                zone_id=zone_id, zeitstempel=jetzt,
                soll_bewaessern=False,
                grund=grund_mon,
                feuchte_aktuell=robust,
                effektive_schwelle=_eff_schwelle,
                ml_aktiv=ml_aktiv, ml_wirksam=ml_wirksam,
                ml_status_grund="zone_monitoring",
                aktive_strategie=zone.bewaesserungs_strategie.value,
                empfehlungs_typ=empf_typ_mon,
                # T-0271: additive Referenzen, damit Frontend und
                # Physik-Augmentation (T-0269) auch im monitoring-Pfad
                # einen Welkepunkt sehen.
                welkepunkt_wert=wp_wert_mon,
                welkepunkt_quelle=wp_quelle_mon,
                feldkapazitaet_wert=fk_wert_mon,
                feldkapazitaet_quelle=fk_quelle_mon,
            )

        robust = await self._robuste_feuchte(zone_id, jetzt)
        if robust is None:
            return GiessEmpfehlung(
                zone_id=zone_id, zeitstempel=jetzt,
                soll_bewaessern=False,
                blocker_typ=BlockerTyp.KEINE_MESSUNG,
                grund="Keine gueltige Feuchtemessung vorhanden",
                ml_aktiv=ml_aktiv, ml_wirksam=ml_wirksam,
                ml_status_grund="keine_messung",
                aktive_strategie=zone.bewaesserungs_strategie.value,
            )
        aktuelle_feuchte = robust

        wetter = await self._wetter_fuer_zone(zone_id).hole_vorhersage()
        niederschlag_6h = wetter.niederschlag_naechste_stunden(6)
        niederschlag_24h = wetter.niederschlag_naechste_stunden(24)
        et0_6h = wetter.et0_naechste_stunden(6)
        et0_24h = wetter.et0_naechste_stunden(24)
        # T-0135 (H-4 Stufe 1b): Schwelle Regime-aware aufloesen.
        eff_schwelle_min = effektiv_schwelle_min(zone, jetzt)
        effektive_schwelle, anhebung = self._effektive_schwelle(
            eff_schwelle_min, et0_24h,
        )

        # T-0075: Kausale Referenzen — vor Blocker-Kaskade laden, damit
        # auch FEUCHTE_OK + Blocker-Returns Welkepunkt + Prognose +
        # Reserve-Aussage liefern koennen. Alle Methoden sind robust
        # (returnen None bei fehlenden Daten/DB-Fehler).
        welkepunkt_wert, welkepunkt_quelle, fk_wert, fk_quelle = (
            await self._hole_kalibrier_referenzen(zone, jetzt)
        )
        sicherheits_tage = (
            sicherheits_tage_override
            if sicherheits_tage_override is not None
            else zone.sicherheits_tage
        )
        regen_schwelle_mm = self._regen_schwelle_mm()
        decay_pp_pro_tag = self._decay_pp_pro_tag(
            et0_24h_mm=et0_24h,
            niederschlag_24h_mm=niederschlag_24h,
            regen_schwelle_mm=regen_schwelle_mm,
        )
        # T-0105: ML-Prognose bevorzugen (Niederschlag/ET0/Saison als
        # Features), Heuristik-Fallback wenn ML nicht verfuegbar.
        # T-0121: zusaetzlich liefert die Methode den effektiven Decay
        # zurueck — bei "ml" der ML-implizite Decay (statt heuristik-ET0).
        prognose, prognose_quelle, prognose_decay_pp_pro_tag = await self._prognose_ml_oder_heuristik(
            zone_id, aktuelle_feuchte, decay_pp_pro_tag, [6, 12, 24, 48, 72],
        )
        # T-0105 + User-Befund 30.04.: bei HAEUFIG_KLEIN ist die strategische
        # Untergrenze das Wohl-Min (nicht der Welkepunkt). "Reserve" wird
        # darauf bezogen — sonst ist die Aussage "Reserve 4 Tage ueber
        # Welkepunkt" trotz HAEUFIG_KLEIN-Strategie kontraintuitiv.
        if (zone.bewaesserungs_strategie == BewaesserungsStrategie.HAEUFIG_KLEIN
                and zone.optimum_feuchte_min is not None):
            reserve_grenze_wert = zone.optimum_feuchte_min
            reserve_grenze_label = "Wohl-Min"
        else:
            reserve_grenze_wert = welkepunkt_wert
            reserve_grenze_label = "Welkepunkt"
        # T-0121: bei ML-Quelle Extrapolation MIT ML-Decay (statt
        # Heuristik-Decay). Bei nahezu konstantem ML-Verlauf (Sensor
        # bleibt stabil dank niedriger ET0) waere die Heuristik-Rate
        # (ET0-basiert) viel zu pessimistisch und triggert false-akut.
        tage_bis_grenze = self._zeit_bis_grenze_aus_prognose(
            aktuelle_feuchte, reserve_grenze_wert, prognose,
            prognose_decay_pp_pro_tag,
        )
        # Backward-compat: tage_bis_welkepunkt = Tage bis Welkepunkt-Linie,
        # auch bei HAEUFIG_KLEIN. Wird vom Klassifikator weiter genutzt
        # (akut/praeventiv/kein_bedarf-Logik basiert auf Welkepunkt).
        tage_bis_welke = self._zeit_bis_grenze_aus_prognose(
            aktuelle_feuchte, welkepunkt_wert, prognose,
            prognose_decay_pp_pro_tag,
        )
        # T-0279 Phase 2: Physik-Reserve (bis optimum_min) fuer den
        # proaktiven Trigger.
        tage_bis_proaktiv = await self._physik_reserve_tage(
            zone, aktuelle_feuchte, welkepunkt_wert, jetzt,
        )
        # T-0286: Recency-Guard -- nach einem Ground-Truth-Lauf innerhalb
        # des Sensor-Nachlauf-Fensters keinen proaktiven Trigger (traege
        # Multi-Sensor hinken dem frisch gegossenen Gardena nach).
        kuerzlich_gegossen = await self._kuerzlich_gegossen(zone, jetzt)
        # T-0279 Phase 2 (Reachability-Fix 31.05.): der proaktive Trigger
        # soll feuern, SOLANGE die Feuchte noch UEBER der normalen Schwelle
        # liegt (das ist sein Zweck: rechtzeitig vor dem akut-Rand). Ohne
        # diese Ausnahme wuerde das FEUCHTE_OK-Gate unten den Trigger
        # immer wegschneiden (er feuert per Definition oberhalb der
        # Schwelle). Single-Source-Helper mit entscheide_pro_zone teilen.
        from bewaesserung.entscheidung_pro_zone import proaktiv_trigger_aktiv
        proaktiv_greift = proaktiv_trigger_aktiv(
            zone.proaktiv_tage_vor_optimum_min, tage_bis_proaktiv,
            kuerzlich_gegossen=kuerzlich_gegossen,
        )

        # T-0103-Folge (29.04.): bei HAEUFIG_KLEIN soll FEUCHTE_OK NICHT
        # greifen, wenn die Prognose (6h/12h/24h) unter Wohl-Min faellt.
        # Damit der Klassifikator die proaktive Empfehlung liefern kann.
        prognose_unter_wohl_min = (
            zone.bewaesserungs_strategie == BewaesserungsStrategie.HAEUFIG_KLEIN
            and zone.optimum_feuchte_min is not None
            and any(
                p is not None and p < zone.optimum_feuchte_min
                for p in (prognose.get(6), prognose.get(12), prognose.get(24))
            )
        )

        if (
            aktuelle_feuchte >= effektive_schwelle
            and not prognose_unter_wohl_min
            and not proaktiv_greift
        ):
            # FEUCHTE_OK: kein aktiver Bedarf. Kausale Reserve-Aussage
            # ("Reserve N Tage bis Welkepunkt") liefern.
            # T-0279 Phase 1b: trotzdem eine ADOPTIERBARE Zieldosis
            # berechnen (Richtung Strategie-Ziel, z.B. Bambus -> optimum_max
            # / SELTEN_GROSS -> Feldkapazitaet), damit die globale 1-Klick-
            # Uebernahme auch hier eine sinnvolle Dauer hat. NICHT die
            # MIN-Clip-Heuristik (raise-to-Schwelle = 0): dauer_s_heuristik
            # bleibt None, nur dauer_s_empfehlung traegt die Zieldosis.
            from bewaesserung.entscheidung_pro_zone import (
                ProZoneKontext as _ProZoneKontext,
                entscheide_pro_zone as _entscheide_pro_zone,
            )
            _ok_auswertung = _entscheide_pro_zone(
                zone=zone,
                kontext=_ProZoneKontext(
                    aktuelle_feuchte=aktuelle_feuchte,
                    prognose=prognose,
                    welkepunkt_wert=welkepunkt_wert,
                    tage_bis_welke=tage_bis_welke,
                    fk_wert=fk_wert,
                    sicherheits_tage_konfig=sicherheits_tage,
                    decay_pp_pro_tag=prognose_decay_pp_pro_tag,
                    tage_bis_proaktiv=tage_bis_proaktiv,
                    kuerzlich_gegossen=kuerzlich_gegossen,
                ),
                jetzt=jetzt,
            )
            ok_dauer_s_empfehlung = await self._hypothetische_zieldosis_s(
                zone, aktuelle_feuchte, _ok_auswertung.ziel_feuchte_roh,
                et0_6h, jetzt,
            )
            ok_liter_haupt = (
                self._liter_fuer_dauer(zone, ok_dauer_s_empfehlung)
                if ok_dauer_s_empfehlung is not None
                else None
            )
            grund = (
                f"Feuchte {self._format_zahl(aktuelle_feuchte)}% ist nicht unter "
                f"Schwelle {self._format_zahl(effektive_schwelle)}%"
                + (f" (+{self._format_zahl(anhebung)}% Hitze)" if anhebung > 0 else "")
            )
            erklarung_kurz, erklarung_lang = self._baue_erklarung(
                aktuelle_feuchte=aktuelle_feuchte,
                welkepunkt=welkepunkt_wert,
                welkepunkt_quelle=welkepunkt_quelle,
                optimum_min=zone.optimum_feuchte_min,
                optimum_max=zone.optimum_feuchte_max,
                prognose=prognose,
                tage_bis_welke=tage_bis_welke,
                dauer_s=None,
                deckung_tage=None,
                niederschlag_24h_mm=niederschlag_24h,
                empfehlungs_typ="kein_bedarf",
                strategie=zone.bewaesserungs_strategie,
                tage_bis_grenze=tage_bis_grenze,
                reserve_grenze_label=reserve_grenze_label,
            )
            return GiessEmpfehlung(
                zone_id=zone_id, zeitstempel=jetzt,
                soll_bewaessern=False, blocker_typ=BlockerTyp.FEUCHTE_OK,
                grund=grund,
                feuchte_aktuell=aktuelle_feuchte,
                effektive_schwelle=effektive_schwelle,
                ml_aktiv=ml_aktiv, ml_wirksam=ml_wirksam,
                welkepunkt_wert=welkepunkt_wert,
                welkepunkt_quelle=welkepunkt_quelle,
                optimum_min=zone.optimum_feuchte_min,
                optimum_max=zone.optimum_feuchte_max,
                feldkapazitaet_wert=fk_wert,
                feldkapazitaet_quelle=fk_quelle,
                prognose_6h=prognose.get(6),
                prognose_12h=prognose.get(12),
                prognose_24h=prognose.get(24),
                prognose_quelle=prognose_quelle,
                # T-0351: rohe Heuristik-Rate fuer den Shadow-Vergleich
                # (nur Diagnose-Feld, kein Entscheidungs-Einfluss).
                decay_heuristik_pp_pro_tag=decay_pp_pro_tag,
                tage_bis_welkepunkt=tage_bis_welke,
                tage_bis_reserve_grenze=tage_bis_grenze,
                reserve_grenze_label=reserve_grenze_label,
                empfehlungs_typ="kein_bedarf",
                aktive_strategie=zone.bewaesserungs_strategie.value,
                # T-0279 Phase 1b: adoptierbare Zieldosis (1-Klick), aber
                # soll_bewaessern bleibt False (kein aktiver Bedarf).
                dauer_s_empfehlung=ok_dauer_s_empfehlung,
                liter_haupt=ok_liter_haupt,
                erklarung_kurz=erklarung_kurz,
                erklarung_lang=erklarung_lang,
            )

        # Ab hier liegt die Feuchte unter der Schwelle. Damit das Panel
        # auch in REGEN/ZEITFENSTER/BUDGET/PAUSE-Blockern zeigt, **was**
        # gegossen werden wuerde (mentale Planung), rechnen wir die
        # Dauer + ML + Ampel jetzt schon. `soll_bewaessern` bleibt in
        # den Blocker-Zweigen trotzdem False — das Frontend rendert das
        # dann als "WUERDE X min / Grund: ..." (gedaempfte Darstellung).
        # T-0085: Wirkungsrate einmal pro Empfehlung aufloesen + an alle
        # Dauer-Berechnungen durchreichen, damit Heuristik + kausale
        # Empfehlung denselben Wert nutzen.
        delta_pp_wert, delta_pp_quelle = await self._aufgeloeste_wirkungsrate(
            zone, jetzt,
        )
        # T-0292 Stufe 2: gefittete wmax/r0 einmal pro Empfehlung aufloesen +
        # an alle Dauer-Berechnungen durchreichen (Heuristik + kausal +
        # Transparenz). (None, None) wenn `adoptieren=False` -> Konfig.
        wmax_wert, r0_wert, _wf_quelle = await self._aufgeloeste_wirkung(
            zone, jetzt,
        )
        dauer_s_heuristik = self._berechne_dauer(
            zone, aktuelle_feuchte, et0_6h, ziel_schwelle=effektive_schwelle,
            delta_pp_pro_minute_override=delta_pp_wert,
            wmax_override=wmax_wert, r0_override=r0_wert,
        )
        # T-0164: ML-Aufruf nach Klassifikation, damit ziel_schwelle
        # konsistent mit kausal ist (HAEUFIG_KLEIN -> optimum_max,
        # SELTEN_GROSS -> Feldkapazitaet, KORRIDOR -> feuchte_schwelle_min).
        # Initial mit effektive_schwelle als Fallback; wird ggf. unten
        # nach der entscheide_pro_zone-Klassifikation mit ziel_feuchte_kausal
        # ersetzt.
        ml_s, modell_version, ml_status_grund = self._ml_dauer_dry_run(
            zone, jetzt, aktuelle_feuchte, et0_6h, et0_24h,
            niederschlag_6h, niederschlag_24h, effektive_schwelle,
        )
        ampel, mae_h, mae_m, n_bewertet = await self._drift_ampel(zone_id)
        liter_heuristik = self._liter_fuer_dauer(zone, dauer_s_heuristik)

        # T-0075/T-0103: Kausale Empfehlungs-Dauer + Strategie-aware
        # Empfehlungs-Typ. Klassifikation MUSS vor Dauer-Berechnung
        # (bei kein_bedarf entfaellt die Dauer komplett).
        # T-0231: Klassifikation via `entscheide_pro_zone` aus dem Modul
        # `entscheidung_pro_zone` (Single Source). vorhersage_zone +
        # pruefe_kanal nutzen denselben Pfad, damit Dashboard +
        # Auto-Loop NICHT auseinander laufen koennen.
        if welkepunkt_wert is not None:
            from bewaesserung.entscheidung_pro_zone import (
                ProZoneKontext,
                entscheide_pro_zone,
            )
            _auswertung = entscheide_pro_zone(
                zone=zone,
                kontext=ProZoneKontext(
                    aktuelle_feuchte=aktuelle_feuchte,
                    prognose=prognose,
                    welkepunkt_wert=welkepunkt_wert,
                    tage_bis_welke=tage_bis_welke,
                    fk_wert=fk_wert,
                    sicherheits_tage_konfig=sicherheits_tage,
                    decay_pp_pro_tag=prognose_decay_pp_pro_tag,
                    tage_bis_proaktiv=tage_bis_proaktiv,
                    kuerzlich_gegossen=kuerzlich_gegossen,
                ),
                jetzt=jetzt,
            )
            empf_typ = _auswertung.empfehlungs_typ
            ziel_feuchte_kausal = _auswertung.ziel_feuchte
            st_eff = _auswertung.effektive_sicherheits_tage

            # T-0164: ML neu rechnen mit kausalem Ziel (Strategie-aware).
            # Wenn die Strategie HAEUFIG_KLEIN auf optimum_max zielt, soll
            # auch das ML-Modell auf optimum_max rechnen — sonst kapituliert
            # ML mit "sensor_ueber_ziel" obwohl kausal eine Empfehlung gibt.
            # Nur wenn kausal einen aktiven Trigger sieht — bei
            # `kein_bedarf` bleibt der Init-Wert (mit effektive_schwelle
            # als Ziel), damit Schwellen-getriebene Bewaesserungen weiter
            # einen ML-Vergleichswert haben.
            if empf_typ != "kein_bedarf" and ziel_feuchte_kausal is not None:
                ml_s, modell_version, ml_status_grund = self._ml_dauer_dry_run(
                    zone, jetzt, aktuelle_feuchte, et0_6h, et0_24h,
                    niederschlag_6h, niederschlag_24h, ziel_feuchte_kausal,
                )

            # Dauer + Deckung nur bei aktiv-Trigger berechnen.
            if empf_typ == "kein_bedarf" or ziel_feuchte_kausal is None:
                # T-0279 Phase 1b: auch bei kein_bedarf (z.B. SELTEN_GROSS/
                # KONSTANT_NIEDRIG unter Schwelle, aber Reserve ok) eine
                # adoptierbare Zieldosis Richtung Strategie-Ziel liefern,
                # damit die globale 1-Klick-Uebernahme ueberall greift.
                # soll_bewaessern bleibt unveraendert (kein aktiver Trigger).
                dauer_s_empfehlung: int | None = (
                    await self._hypothetische_zieldosis_s(
                        zone, aktuelle_feuchte,
                        _auswertung.ziel_feuchte_roh, et0_6h, jetzt,
                    )
                )
                deckung_tage_roh: float | None = tage_bis_welke
                folge_dose_dauer_s: int | None = None
                folge_dose_verzoegerung_h: float | None = None
                folge_dose_liter: float | None = None
            else:
                # T-0086: rohe Dauer (ungeclippt) berechnen, um zu sehen
                # ob max_dauer_sekunden anschlaegt -> Mehrfach-Takt.
                # T-0121: Dauer-Berechnung mit ML-Decay (statt Heuristik-
                # ET0). Bei stabilem ML-Verlauf entfaellt der ueber-
                # konservative Sicherheits-Aufschlag — sonst rechnet die
                # Heuristik "deck dich gegen 10 pp/Tag Decay ein", obwohl
                # ML "nur 4 pp/Tag" sagt, und wir landen bei 90 min wo
                # 30 min reichen wuerden.
                rohe_dauer_s = self._dauer_fuer_sicherheitsabstand(
                    zone, aktuelle_feuchte, welkepunkt_wert,
                    st_eff, prognose_decay_pp_pro_tag, et0_6h,
                    delta_pp_pro_minute_override=delta_pp_wert,
                    ziel_feuchte_override=ziel_feuchte_kausal,
                    clip_auf_max=False,
                    wmax_override=wmax_wert, r0_override=r0_wert,
                )
                dauer_s_empfehlung = min(rohe_dauer_s, zone.max_dauer_sekunden)
                # T-0086: Wenn rohe Dauer max_dauer ueberschreitet,
                # Folge-Dose vorschlagen.
                folge_dose_dauer_s = None
                folge_dose_verzoegerung_h = None
                folge_dose_liter = None
                if rohe_dauer_s > zone.max_dauer_sekunden:
                    folge_dose_dauer_s = rohe_dauer_s - zone.max_dauer_sekunden
                    # Verzoegerung: max(versickerungs_karenz, 6h-Default).
                    # 6h gibt der Hauptdose Zeit fuer Sensor-Antwort + erlaubt
                    # ein Bewaesserungs-Fenster spaeter am Tag.
                    folge_dose_verzoegerung_h = float(
                        max(zone.versickerungs_karenz_stunden, 6)
                    )
                    folge_dose_liter = round(
                        self._liter_fuer_dauer(zone, folge_dose_dauer_s),
                        1,
                    )
                ziel_clipped = max(0.0, min(100.0, ziel_feuchte_kausal))
                deckung_tage_roh = None
                if decay_pp_pro_tag > 0:
                    deckung_tage_roh = round(
                        (ziel_clipped - welkepunkt_wert) / decay_pp_pro_tag,
                        1,
                    )
        else:
            # Ohne Welkepunkt: kausale Dauer = Heuristik (kein besseres Signal).
            dauer_s_empfehlung = dauer_s_heuristik
            deckung_tage_roh = None
            empf_typ = "praeventiv"  # Default wenn unter Schwelle
            # T-0086: Mehrfach-Takt nur im Welkepunkt-Pfad (rohe Dauer-Info).
            folge_dose_dauer_s = None
            folge_dose_verzoegerung_h = None
            folge_dose_liter = None

        erklarung_kurz, erklarung_lang = self._baue_erklarung(
            aktuelle_feuchte=aktuelle_feuchte,
            welkepunkt=welkepunkt_wert,
            welkepunkt_quelle=welkepunkt_quelle,
            optimum_min=zone.optimum_feuchte_min,
            optimum_max=zone.optimum_feuchte_max,
            prognose=prognose,
            tage_bis_welke=tage_bis_welke,
            dauer_s=dauer_s_empfehlung,
            deckung_tage=deckung_tage_roh,
            niederschlag_24h_mm=niederschlag_24h,
            empfehlungs_typ=empf_typ,
            strategie=zone.bewaesserungs_strategie,
            tage_bis_grenze=tage_bis_grenze,
            reserve_grenze_label=reserve_grenze_label,
        )

        # T-0378: wird weiter unten gesetzt, wenn die min_pause wegen
        # kritischer Trockenheit uebersprungen wird. Bewusst HIER
        # initialisiert (vor `_erstelle`), damit die Blocker-Returns oberhalb
        # des Pause-Checks nicht in ein ungebundenes Closure-Read laufen.
        # `_erstelle` liest die Variable ueber das Closure zum AUFRUFzeitpunkt
        # -- so muss das Flag nicht durch jeden einzelnen Return gefaedelt
        # werden, und ein spaeter ergaenzter Return kann es nicht vergessen.
        pause_bypass_kritisch_aktiv = False

        # Gemeinsamer Bauplan fuer alle Rueckgaben ab hier — enthaelt
        # feuchte, schwelle, dauer_*, ml_*, drift_*, kausale Felder.
        def _erstelle(
            *, soll_bewaessern: bool, blocker_typ: BlockerTyp | None, grund: str,
            budget_notreserve_aktiv: bool = False,
        ) -> GiessEmpfehlung:
            # T-0089: liter_haupt analog zur Frontend-Auswahl-Logik
            # (mlPrimaer ? ml : empfehlung ?? heuristik). Damit zeigt
            # die UI Liter passend zur tatsaechlich angezeigten Dauer.
            if soll_bewaessern and ml_wirksam and ml_s is not None:
                liter_haupt = self._liter_fuer_dauer(zone, ml_s)
            elif dauer_s_empfehlung is not None:
                liter_haupt = self._liter_fuer_dauer(zone, dauer_s_empfehlung)
            else:
                liter_haupt = liter_heuristik
            # T-0291: Plateau-Transparenz -- erwarteter Endwert der Dose +
            # wie viele Dosen das Ziel braucht (macht die flache Max-Einzel-
            # dose ehrlich, statt sie wie eine Ziel-erreichende Dauer zu zeigen).
            _ziel_transp = (
                zone.optimum_feuchte_max
                if zone.optimum_feuchte_max is not None
                else effektive_schwelle
            )
            # T-0292 Stufe 2: Transparenz konsistent mit der Dauer-Berechnung
            # -- wenn ein Fit adoptiert wurde, zeigt die UI dessen Plateau.
            _endwert_pp, _einzeldosis_pp, _dosen_ziel = _plateau_transparenz(
                wmax_wert if wmax_wert is not None else zone.wirkung_max_pp,
                r0_wert if r0_wert is not None else zone.wirkungsrate_initial,
                aktuelle_feuchte, _ziel_transp, dauer_s_empfehlung,
            )
            return GiessEmpfehlung(
                zone_id=zone_id, zeitstempel=jetzt,
                soll_bewaessern=soll_bewaessern, blocker_typ=blocker_typ,
                budget_notreserve_aktiv=budget_notreserve_aktiv,
                pause_bypass_kritisch_aktiv=pause_bypass_kritisch_aktiv,
                grund=grund,
                feuchte_aktuell=aktuelle_feuchte,
                effektive_schwelle=effektive_schwelle,
                dauer_s_heuristik=dauer_s_heuristik,
                liter_heuristik=liter_heuristik,
                liter_haupt=liter_haupt,
                # T-0085: Wirkungsrate-Quelle (manuell / kalibrierung / default).
                delta_pp_pro_minute_wert=delta_pp_wert,
                delta_pp_pro_minute_quelle=delta_pp_quelle,
                ml_aktiv=ml_aktiv, ml_wirksam=ml_wirksam,
                dauer_s_ml=ml_s,
                modell_version=modell_version,
                ml_status_grund=ml_status_grund,
                drift_ampel=ampel,
                drift_mae_heuristik=mae_h,
                drift_mae_ml=mae_m,
                drift_n_bewertet=n_bewertet,
                # T-0075: kausale Felder
                welkepunkt_wert=welkepunkt_wert,
                welkepunkt_quelle=welkepunkt_quelle,
                optimum_min=zone.optimum_feuchte_min,
                optimum_max=zone.optimum_feuchte_max,
                feldkapazitaet_wert=fk_wert,
                feldkapazitaet_quelle=fk_quelle,
                prognose_6h=prognose.get(6),
                prognose_12h=prognose.get(12),
                prognose_24h=prognose.get(24),
                prognose_quelle=prognose_quelle,
                # T-0351: rohe Heuristik-Rate fuer den Shadow-Vergleich
                # (nur Diagnose-Feld, kein Entscheidungs-Einfluss).
                decay_heuristik_pp_pro_tag=decay_pp_pro_tag,
                tage_bis_welkepunkt=tage_bis_welke,
                tage_bis_reserve_grenze=tage_bis_grenze,
                reserve_grenze_label=reserve_grenze_label,
                dauer_s_empfehlung=dauer_s_empfehlung,
                deckung_nach_giessen_tage=deckung_tage_roh,
                empfehlungs_typ=empf_typ,
                erklarung_kurz=erklarung_kurz,
                erklarung_lang=erklarung_lang,
                # T-0103: aktive Strategie aus Zone-Konfig durchreichen
                aktive_strategie=zone.bewaesserungs_strategie.value,
                # T-0086: Mehrfach-Takt-Felder
                folge_dose_dauer_s=folge_dose_dauer_s,
                folge_dose_verzoegerung_h=folge_dose_verzoegerung_h,
                folge_dose_liter=folge_dose_liter,
                # T-0291: Plateau-Transparenz
                erwarteter_endwert_pp=_endwert_pp,
                einzeldosis_max_pp=_einzeldosis_pp,
                dosen_bis_ziel=_dosen_ziel,
            )

        # T-0279 Phase 2 (Reachability-Fix): dieser Bewaesserungs-Zweig
        # wird jetzt auch erreicht, wenn die Feuchte noch UEBER der
        # Schwelle liegt aber der proaktive Trigger greift. Der Grund-
        # Text darf dann nicht "unter Schwelle" behaupten (waere falsch).
        if aktuelle_feuchte < effektive_schwelle:
            basis_grund = (
                f"Feuchte {self._format_zahl(aktuelle_feuchte)}% unter Schwelle "
                f"{self._format_zahl(effektive_schwelle)}%"
                + (f" (+{self._format_zahl(anhebung)}% Hitze)" if anhebung > 0 else "")
            )
        else:
            basis_grund = (
                f"Proaktiver Tiefenlauf: Physik-Reserve bis optimum_min "
                f"<= {zone.proaktiv_tage_vor_optimum_min}d "
                f"(Feuchte {self._format_zahl(aktuelle_feuchte)}% noch ueber "
                f"Schwelle {self._format_zahl(effektive_schwelle)}%)"
            )

        if niederschlag_6h >= regen_schwelle_mm:
            grund = (
                f"{basis_grund}, aber "
                f"{self._format_zahl(niederschlag_6h)}mm Regen in 6h erwartet"
            )
            return _erstelle(
                soll_bewaessern=False, blocker_typ=BlockerTyp.REGEN_ERWARTET,
                grund=grund,
            )

        ist_kritisch = aktuelle_feuchte < zone.feuchte_kritisch
        in_bevorzugter_zeit = self._ist_bevorzugte_zeit(zone, jetzt)
        if not in_bevorzugter_zeit and not ist_kritisch:
            grund = f"{basis_grund}, aber ausserhalb bevorzugter Zeit"
            return _erstelle(
                soll_bewaessern=False, blocker_typ=BlockerTyp.ZEITFENSTER,
                grund=grund,
            )

        tagesverbrauch = await self._tagesverbrauch(zone_id)
        budget = self._effektives_tagesbudget(zone, ist_kritisch)
        if tagesverbrauch >= budget:
            grund = (
                f"Tagesbudget erreicht ({self._format_zahl(tagesverbrauch)} von "
                f"{self._format_zahl(budget)})"
            )
            return _erstelle(
                soll_bewaessern=False, blocker_typ=BlockerTyp.BUDGET_ERSCHOEPFT,
                grund=grund,
                budget_notreserve_aktiv=(
                    ist_kritisch and zone.tages_budget_kritisch_faktor > 1.0
                ),
            )

        pause_eingehalten, verbleibende_pause = await self._pause_eingehalten(
            zone, jetzt, scope=EntscheidungsScope.ZONE,
        )
        # T-0378: kritische Trockenheit schlaegt min_pause (gedeckelt).
        # Auch im Prognose-/Dashboard-Pfad, sonst zeigt die Karte
        # "Min-Pause aktiv", waehrend die Engine giessen wuerde
        # (UI-Vertrag: Dashboard darf der Entscheidung nicht widersprechen).
        if not pause_eingehalten:
            pause_bypass_kritisch_aktiv = await self._pause_bypass_bei_kritisch(
                zone, ist_kritisch, jetzt,
            )
        if not pause_eingehalten and not pause_bypass_kritisch_aktiv:
            grund = (
                f"Min-Pause noch nicht eingehalten, noch "
                f"{self._format_zahl(verbleibende_pause)} Minuten warten"
            )
            return _erstelle(
                soll_bewaessern=False, blocker_typ=BlockerTyp.PAUSE_AKTIV,
                grund=grund,
            )

        # T-0279-Folge (Dashboard-Konsistenz): die End-Aktion ist nur dann
        # "giessen", wenn die Strategie tatsaechlich Bedarf sieht. Bei
        # `kein_bedarf` (z.B. SELTEN_GROSS/KONSTANT_NIEDRIG unter Schwelle
        # mit ausreichender Reserve) darf vorhersage_zone NICHT
        # soll_bewaessern=True liefern -- sonst zeigt die V3-Karte
        # `getActionState` faelschlich 'giessen' und `KritischBand` umgeht
        # den T-0278-Engine-Check (soll_bewaessern als Bedarfs-Signal),
        # obwohl `pruefe_kanal` die Zone gar nicht giessen wuerde. Die
        # Blocker-Returns oben sind bereits soll_bewaessern=False -- nur der
        # finale "passt-alles"-Pfad muss kein_bedarf respektieren. Die
        # adoptierbare Zieldosis (T-0279 Phase 1b, dauer_s_empfehlung) bleibt
        # ueber _erstelle erhalten.
        return _erstelle(
            soll_bewaessern=(empf_typ != "kein_bedarf"),
            blocker_typ=None, grund=basis_grund,
        )

    async def _erstelle_entscheidung(
        self,
        zone_id: str,
        soll_bewaessern: bool,
        begruendung: str,
        zeitpunkt: datetime,
        naechste_pruefung: datetime,
        dauer_sekunden: int = 0,
        blocker_typ: BlockerTyp | None = None,
        scope: EntscheidungsScope = EntscheidungsScope.ZONE,
        scope_ref: str | None = None,
    ) -> BewaesserungsEntscheidung:
        entscheidung = BewaesserungsEntscheidung(
            zeitstempel=zeitpunkt,
            zone_id=zone_id,
            soll_bewaessern=soll_bewaessern,
            dauer_sekunden=dauer_sekunden,
            begruendung=begruendung,
            naechste_pruefung=naechste_pruefung,
            blocker_typ=blocker_typ,
            scope=scope,
            scope_ref=scope_ref or zone_id,
        )
        await self._speicher.speichere_entscheidung(entscheidung)
        return entscheidung

    def _effektive_schwelle(
        self, basis: float, et0_24h_mm: float,
    ) -> tuple[float, float]:
        """Effektive Feuchte-Mindestschwelle bei Beruecksichtigung der Verdunstung.

        Gibt (effektive_schwelle, anhebung) zurueck. Anhebung > 0 bedeutet:
        heute ist Hitze-Stress prognostiziert, wir giessen frueher als sonst.
        """
        cfg = self._schwellen_adaption
        if not cfg.aktiv:
            return basis, 0.0
        ueberschuss = max(0.0, et0_24h_mm - cfg.median_et0_mm_pro_tag)
        anhebung = min(cfg.k * ueberschuss, cfg.anhebung_max)
        return basis + anhebung, anhebung

    def _effektives_tagesbudget(self, zone, ist_kritisch: bool) -> float:
        """T-0354: Effektives Tagesbudget. Bei kritischer Trockenheit bis zur
        Notreserve (kritisch_faktor x proaktiv) -- echter Durst soll nicht am
        proaktiven Cap sterben. Der Faktor ist GEDECKELT (kein unbegrenzter
        Bypass), damit ein stuck-low-Sensor (sensor-b-Klasse) nicht endlos
        giesst -- der Runaway-Schutz bleibt. Default-Faktor 1.0 = unveraendert."""
        if ist_kritisch:
            return zone.tages_budget_sekunden * zone.tages_budget_kritisch_faktor
        return zone.tages_budget_sekunden

    async def _tagesverbrauch(self, zone_id: str) -> float:
        # UNBEKANNT-Events (Sensor-Heuristik, nicht klassifiziert) sind Kandidaten,
        # keine bestaetigten Bewaesserungen — sie duerfen Budget/Pause nicht
        # belasten. Erst nach User-Klassifikation zaehlen sie (ausloser-Wechsel
        # ueber PATCH /api/ventil-ereignis).
        ereignisse = await self._speicher.ventil_ereignisse_heute(zone_id)
        return float(sum(
            max(e.dauer_sekunden, 0) for e in ereignisse
            if e.ausloser not in KEINE_WASSER_AUSLOESER
        ))

    async def _kanal_aktiv_bewaesserung(self, zone_id: str) -> bool:
        """T-0119: True wenn auf dem Kanal der Zone gerade eine Bewaesserung
        laeuft (live_lauf_state). Zonen ohne `ventil_kanal` -> False.
        Speicher ohne live_lauf_state-Methode (alte Mocks) -> False.
        """
        zone = self._zonen.get(zone_id)
        if zone is None or zone.ventil_kanal is None:
            return False
        hole_states = getattr(self._speicher, "hole_live_lauf_states", None)
        if hole_states is None:
            return False
        try:
            states = await hole_states()
        except Exception:
            return False
        return any(
            s.get("kanal") == zone.ventil_kanal
            and (
                zone.ventil_geraet_id is None
                or s.get("geraet_id") == zone.ventil_geraet_id
            )
            for s in states
        )

    async def _ist_sensor_festklemmend(self, zone_id: str) -> bool:
        """H-1 (Pre-Mortem-Roadmap): True wenn fuer die Zone aktuell eine
        offene SENSOR_EINGEFROREN-Warnung existiert.

        Der LeckDetektor erkennt festklemmende Sensoren ueber die 48h-Spanne
        bereits, die Warnung landet aber nur in der Ops-Timeline. Bisher lief
        der konstante Wert weiter in die Empfehlung -- bei aktiver
        Ventilsteuerung kann das eine wochenlange Akut-Spirale ausloesen
        (Pre-Mortem Akt 1: Waldblumenhain konstant 32 % -> 6 Wochen taegliche
        Volldosis -> Wurzelfaeule). Dieser Helper ist die Bruecke vom
        Detektor zur Empfehlungslogik.
        """
        hole_offene = getattr(self._speicher, "offene_sensor_warnungen", None)
        if hole_offene is None:
            return False
        try:
            offene = await hole_offene(zone_id)
        except Exception:
            logger.exception(
                "entscheidung.offene_sensor_warnungen_fehler",
                zone_id=zone_id,
            )
            return False
        return any(w.typ == SensorWarnungTyp.SENSOR_EINGEFROREN for w in offene)

    async def _robuste_feuchte(self, zone_id: str, jetzt: datetime) -> float | None:
        """T-0125: Liefert die juengste valide Sensor-Messung als Ground Truth.

        Architektur-Wechsel 05.05.2026: der frueher hier sitzende MAD-Outlier-
        Filter ist entfallen. Sensor-Wert ist die Realitaet — Spruenge nach
        oben sind Wasser-Intake (Bewaesserung, Regen, Schlauch), Spruenge
        nach unten sind nahezu immer real (Verdunstung, Entwaesserung).
        Echte Sensor-Defekte werden durch andere, klarere Mechanismen
        abgefangen:

        - Skala-Verletzung (Wert ausserhalb 0..100): unten in dieser Methode
        - Sensor > MAX_FALLBACK_ALTER_STUNDEN alt (T-0098): unten
        - SENSOR_EINGEFROREN-Warnung des SensorHealthMonitor (H-1): unten
        - Hardware-Ausfall (kein neuer Wert seit N h): SensorHealthMonitor
          schreibt eine SensorWarnung, der User sieht im Ops-Tab.

        Der frueher noetige MAD-Filter war Legacy aus einer Zeit ohne
        live_lauf_state und ohne ML-Prognose. Beides existiert heute:
        - Bewaesserungs-Spruenge sind ueber `live_lauf_state` nachvollziehbar
        - Regen-Spruenge sind ueber `wetter_archiv` + Niederschlag-Forecast
          eindeutig kausal zuordenbar
        - ML-Modell lernt aus diesen Korrelationen und liefert
          Decay/Anstieg-Prognosen

        Symptomatisches Filter-Glaetten dagegen verfaelscht den Audit-Log,
        irritiert die UI (Sensor 85 % auf der Karte vs. 55 % in der
        Empfehlung) und sabotiert das ML-Lernen.

        Returns:
            float Sensor-Wert (0..100) wenn alles ok, None bei Defekt/zu alt.
        """
        if await self._ist_sensor_festklemmend(zone_id):
            logger.warning(
                "entscheidung.sensor_festklemmend_blockiert",
                zone_id=zone_id,
            )
            return None

        # T-0179c: Aggregat ueber alle Sensoren der Zone (Median).
        # Bei einem Sensor identisch zu letzte_messung().
        # T-0383: `jetzt` durchreichen (sonst intern now() -> zweite Zeit-
        # wahrheit, vgl. fehlerpattern_jetzt_nicht_durchgereicht).
        messung = await self._speicher.letzte_messung_aggregiert(
            zone_id, jetzt=jetzt,
        )
        if messung is None or messung.boden_feuchte is None:
            # T-0383: Kein frischer Wert im 90-min-Aggregat-Fenster. Frueher
            # hier ein STILLES None -> Zone verschwand ohne Log aus der Kanal-
            # Entscheidung (T-0382-Ausloeser). Jetzt: einmalig auf den
            # dokumentierten Fallback-Horizont erweitern -- gleicher Sensor
            # (bzw. gleicher Aggregat-Lead), nur aelter -- und laut loggen.
            messung = await self._speicher.letzte_messung_aggregiert(
                zone_id,
                fenster_minuten=AGGREGAT_FALLBACK_FENSTER_MINUTEN,
                jetzt=jetzt,
            )
            if messung is None or messung.boden_feuchte is None:
                logger.warning(
                    "entscheidung.keine_messung_im_fallback_horizont",
                    zone_id=zone_id,
                    fallback_fenster_min=AGGREGAT_FALLBACK_FENSTER_MINUTEN,
                )
                return None
            logger.warning(
                "entscheidung.aggregat_fenster_leer_nutze_fallback",
                zone_id=zone_id,
                alter_minuten=round(
                    (jetzt - messung.zeitstempel).total_seconds() / 60, 1,
                ),
                fallback_fenster_min=AGGREGAT_FALLBACK_FENSTER_MINUTEN,
            )

        wert = float(messung.boden_feuchte)
        # Skala-Verletzung -> Hardware-Defekt, ablehnen
        if not (0.0 <= wert <= 100.0):
            logger.warning(
                "entscheidung.feuchte_ausserhalb_skala",
                zone_id=zone_id, wert=wert,
            )
            return None

        alter_stunden = (jetzt - messung.zeitstempel).total_seconds() / 3600
        if alter_stunden > MAX_FALLBACK_ALTER_STUNDEN:
            logger.warning(
                "entscheidung.fallback_zu_alt",
                zone_id=zone_id,
                alter_stunden=round(alter_stunden, 1),
                max_alter_stunden=MAX_FALLBACK_ALTER_STUNDEN,
            )
            return None

        return wert

    # T-0275 (28.05.): `_robuste_feuchte_legacy_mad` entfernt (T-0125-
    # Quarantaene-Beobachtungsfrist abgelaufen, kein Konsument mehr).

    def _zone_ist_scharf(self, zone: ZonenKonfig) -> bool:
        """T-0334/T-0340: Zone laeuft wirklich autonom (Auto-Loop schaltet ihr
        Ventil) -- 3-stufig: globales ventilsteuerung_aktiv UND modus=automatik
        UND auto_loop_opt_in. Nur dann schreibt ein erfolgreicher Lauf ein
        OEFFNEN; deshalb gilt fuer scharfe Zonen der soll=1-Pause-Anker NICHT
        (sonst blockiert ein Fehlversuch den Retry, T-0340)."""
        konfig = self._konfig
        global_aktiv = bool(getattr(konfig, "ventilsteuerung_aktiv", False))
        return (
            global_aktiv
            and zone.modus == ZonenModus.AUTOMATIK
            and bool(getattr(zone, "auto_loop_opt_in", False))
        )

    async def _pause_eingehalten(
        self, zone: ZonenKonfig, jetzt: datetime,
        scope: EntscheidungsScope = EntscheidungsScope.ZONE,
    ) -> tuple[bool, float]:
        anker = await self._letzter_giess_zeitpunkt(
            zone.zone_id, scope, ist_live=self._zone_ist_scharf(zone),
        )
        if anker is None:
            return True, 0.0

        pause = jetzt - anker
        erforderliche_pause = timedelta(minutes=zone.min_pause_minuten)
        if pause >= erforderliche_pause:
            return True, 0.0

        verbleibend = (erforderliche_pause - pause).total_seconds() / 60
        return False, max(verbleibend, 0.0)

    async def _letzter_giess_zeitpunkt(
        self, zone_id: str, scope: EntscheidungsScope, ist_live: bool = False,
    ) -> datetime | None:
        """Juengster Giess-Zeitpunkt: echter Ventil-Event ODER Shadow-Empfehlung.

        Im Live-Modus dominiert der ventil_ereignis-Zeitstempel; im Shadow-Modus
        (ventilsteuerung_aktiv=false) gibt es keine Events, also nehmen wir
        die letzte passende soll_bewaessern=1-Entscheidung als Pause-Anker —
        sonst wuerde der Motor jeden 5-min-Zyklus erneut "Giess" empfehlen.

        T-0340: `ist_live` (Zone scharf -- ventilsteuerung_aktiv + automatik +
        auto_loop_opt_in) schaltet den soll=1-Anker AUS. Eine scharfe Zone
        schreibt bei ERFOLGREICHEM Lauf ein OEFFNEN (echter Anker); ein
        FEHLVERSUCH (bewaessere False, z.B. transienter 502) schreibt KEIN
        OEFFNEN. Den soll=1-Anker hier anzuwenden wuerde den naechsten Zyklus
        faelschlich per min_pause blockieren -> Zone bleibt trocken bis Pause-
        Ablauf/Restart (Realfall 26.06. Hecke: 502 -> 4h Block). Im Live-Modus
        daher NUR echte Events ankern; den Anti-Spam-Anker brauchen nur Shadow-
        Zonen (kein Event-Pfad).

        UNBEKANNT-Ventil-Events (Sensor-Heuristik, nicht klassifiziert) werden
        uebersprungen — sie sind unsichere Kandidaten und sollen den Motor
        nicht an echten spaeteren Bewaesserungen hindern.
        """
        anker: datetime | None = None
        # Juengstes NICHT-UNBEKANNT Ventil-Event via SQL ORDER BY + LIMIT 1.
        # Vorher: ganze Historie laden + Python-Reversed-Scan — quadratisch
        # teuer, auf dem Pi fuer den Loop-Takt problematisch (F6).
        wasser_anker = getattr(
            self._speicher, "letzter_bestaetigter_wasser_anker", None,
        )
        if callable(wasser_anker):
            letztes = await wasser_anker(
                zone_id, ausloser_ausser=KEINE_WASSER_AUSLOESER,
            )
        else:
            letztes = await self._speicher.letztes_bestaetigtes_ventil_ereignis(
                zone_id, ausloser_ausser=KEINE_WASSER_AUSLOESER,
            )
        if letztes is not None:
            anker = letztes.zeitstempel

        # T-0340: soll=1-Entscheidungs-Anker nur fuer Shadow-Zonen (s.o.).
        if not ist_live:
            entscheidungen = await self._speicher.hole_entscheidungen(
                zone_id=zone_id, limit=50,
            )
            for e in entscheidungen:
                if e.soll_bewaessern and e.scope == scope:
                    if anker is None or e.zeitstempel > anker:
                        anker = e.zeitstempel
                    break
        return anker

    def _ist_bevorzugte_zeit(self, zone: ZonenKonfig, zeitpunkt: datetime) -> bool:
        if not zone.bevorzugte_zeiten:
            return True

        aktuelle_uhrzeit = zeitpunkt.time()
        for fenster in zone.bevorzugte_zeiten:
            von = time.fromisoformat(fenster.von)
            bis = time.fromisoformat(fenster.bis)
            if von <= bis and von <= aktuelle_uhrzeit <= bis:
                return True
            if von > bis and (aktuelle_uhrzeit >= von or aktuelle_uhrzeit <= bis):
                return True

        return False

    def _berechne_feuchte_trend(self, messungen: list[SensorMessung]) -> float:
        start = messungen[0].zeitstempel
        punkte = [
            (
                (messung.zeitstempel - start).total_seconds() / 3600,
                float(messung.boden_feuchte or 0.0),
            )
            for messung in messungen
            if messung.boden_feuchte is not None
        ]

        if len(punkte) < 2:
            return 0.0

        mittel_x = sum(x for x, _ in punkte) / len(punkte)
        mittel_y = sum(y for _, y in punkte) / len(punkte)
        zaehler = sum((x - mittel_x) * (y - mittel_y) for x, y in punkte)
        nenner = sum((x - mittel_x) ** 2 for x, _ in punkte)
        if nenner == 0:
            return 0.0

        return zaehler / nenner

    def _wetter_aenderung(self, stunde: WetterStunde) -> float:
        return (
            stunde.niederschlag_mm * REGEN_FEUCHTE_FAKTOR
            - stunde.et0_mm * ET0_FEUCHTE_FAKTOR
        )

    def _wetter_fuer_zone(self, zone_id: str) -> WetterClient:
        """Gibt den WetterClient fuer den Standort der Zone zurueck."""
        standort_id = self._zone_standort.get(zone_id)
        if standort_id:
            client = self._wetter_manager.hole_client(standort_id)
            if client:
                return client
        return self._wetter_manager.standard_client

    def _regen_schwelle_mm(self) -> float:
        return float(getattr(self._wetter_manager.standard_client, "regen_schwelle_mm", 0.0) or 0.0)

    def _regen_wahrscheinlichkeit_schwelle(self) -> float:
        """T-0322: Prozent-Schwelle fuer den Konvektions-Guard (Default 80)."""
        return float(getattr(
            self._wetter_manager.standard_client,
            "regen_wahrscheinlichkeit_schwelle_prozent", 80.0,
        ) or 80.0)

    @staticmethod
    def _mittlere_rate(raten: list[float]) -> float:
        if not raten:
            return 0.0
        return sum(raten) / len(raten)

    @staticmethod
    def _format_zahl(wert: float) -> str:
        text = f"{wert:.1f}"
        if text.endswith(".0"):
            return text[:-2]
        return text

    def _jetzt(self) -> datetime:
        return datetime.now()
