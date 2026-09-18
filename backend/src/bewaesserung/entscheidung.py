"""Regelbasierter Entscheidungsmotor fuer Bewaesserung und Prognosen."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta
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
    null_ist_sensordefekt,
    SchwellenAdaptionKonfig,
    SensorMessung,
    SensorWarnung,
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
from bewaesserung import dosis_test
from bewaesserung.giessfenster import (
    MODUS_AUS as GF_MODUS_AUS,
    MODUS_SCHATTEN as GF_MODUS_SCHATTEN,
    beschreibe_sperre,
    in_zeitfenstern,
    urteil_fuer_zone,
)
from bewaesserung.kanal_zustand import kanal_vorgang_laeuft
from bewaesserung.vorausschau import (
    VorgezogenerBedarf,
    bewerte_vorausschau,
    fenster_schliesst_bald,
)
from bewaesserung.speicher import AGGREGAT_FALLBACK_FENSTER_MIN, Speicher
from bewaesserung.warn_drossel import WarnDrossel
from bewaesserung.wetter import WetterClient, WetterManager

logger = structlog.get_logger()

# Begruendungstext bei gesperrtem Uhrzeitfenster (alte `bevorzugte_zeiten`).
GF_TEXT_STATISCH = "ausserhalb bevorzugter Zeit"

# T-0540: Warnungen ueber dokumentierte Dauerzustaende auf eine Meldung je Zone
# und Tag drosseln. Betroffen sind die drei Meldungen in `_robuste_feuchte` --
# zusammen ueber 90.000 Eintraege in den vorliegenden stdout-Logs, praktisch
# alle fuer Sensoren, die bekannt ausserhalb der Hub-Reichweite liegen oder
# bekannt konstant sind. Gedrosselt wird die MELDUNG, nicht das `return None`
# darunter: die Zone faellt weiterhin bei jedem Tick aus der Bewertung, sie
# sagt es nur nicht mehr jedes Mal. Schluessel enthaelt die zone_id, sonst
# drosselt die erste Zone die zweite mit weg
# (`fehlerpattern_dedup_pro_zone_multisensor`).
_feuchte_warn_drossel = WarnDrossel()


def _feuchte_warn_drossel_zuruecksetzen() -> None:
    """Nur fuer Tests: Modul-Zustand leeren."""
    _feuchte_warn_drossel.zuruecksetzen()


STANDARD_PRUEFINTERVALL = timedelta(minutes=5)
# T-0445: Frische-Fenster des Kanal-Max-Stops = 2 Gardena-Beat-Intervalle.
# Messzeit != Ankunftszeit -- der Stop kann nur auf Werte reagieren, die
# waehrend des Laufs auch EINGETROFFEN sind. Ein einzelner ausgefallener
# Beat darf keinen Lauf beenden (der Gardena-Beat faellt regelmaessig aus),
# zwei hintereinander sind ein unbekannter Zustand.
MAX_STOP_FRISCHE_FENSTER_MIN = 120.0
REGEN_FEUCHTE_FAKTOR = 4.0
ET0_FEUCHTE_FAKTOR = 2.0
PROGNOSE_EPSILON = 0.05

# Mindestdauer bei aktiver Giess-Empfehlung. Verhindert 0s-Events wenn
# die Feuchte zwischen Basis- und effektiver (ET0-adaptierter) Schwelle liegt.
# Der Wert wohnt in `dosis_test`, weil dessen Klemmung ihn ebenfalls braucht
# und ein Import in die andere Richtung zyklisch waere. Hier bewusst nur ein
# Alias, damit es keine zweite Wahrheit gibt (Importe aus `entscheidung`
# bleiben gueltig).
MIN_DAUER_SEKUNDEN = dosis_test.MIN_DAUER_SEKUNDEN

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
# ist AGGREGAT_FALLBACK_FENSTER_MIN (240 min, in `speicher.py`) -- eine, die
# aelter ist, kommt gar nicht erst aus dem Speicher zurueck. Historie: der
# 48-h-Vertrag (T-0098) war seit T-0179c ohnehin faktisch tot, weil
# `_robuste_feuchte` seither ueber `letzte_messung_aggregiert` liest (90-min-
# SQL-Fenster). Nur der Test-Mock ignorierte das Fenster und hielt den
# Vertrag scheinbar gruen. Bewusst NICHT auf 48 h zurueckgedreht: der Vertrag
# stammt aus der Zeit vor `ventilsteuerung_aktiv` (26.06.2026) -- eine
# tagealte Bodenfeuchte darf kein autonomes Ventil oeffnen.
# T-0476: hat einen ZWEITEN Konsumenten -- `_cache_wert_wenn_frisch` in
# api_server.py kappt damit den In-Memory-Cache-Fallback der Anzeige.
# Wer diesen "nur Backstop" anfasst, aendert die Zonen-Karte mit.
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
# GELOGGT auf `AGGREGAT_FALLBACK_FENSTER_MIN` erweitert -- gleicher Sensor,
# nur aelter.
#
# T-0476 (10.08.2026): Die Konstante steht jetzt in `speicher.py` (neben
# AGGREGAT_FENSTER_MIN) und heisst dort AGGREGAT_FALLBACK_FENSTER_MIN --
# inkl. der Begruendung der 240 min. Grund fuer den Umzug: der Anzeige-Pfad
# fuehrt seit T-0476 denselben Horizont, und zwei Module mit je eigener
# Wahrheit ueber "wie alt darf Bodenfeuchte sein" driften auseinander.


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


def _ist_pump_zone(zone: ZonenKonfig) -> bool:
    """T-0503: vollstaendig konfigurierte AquaBloom-Pump-Zone?

    Delegiert bewusst an `aquabloom_job._ist_konfiguriert`, statt die vier
    Pflichtfelder ein zweites Mal zu pruefen. Eine eigene Fassung waere
    genau die zweite Wahrheit, die Heuristik 1 der Projekt-CLAUDE.md
    verbietet -- und sie wuerde still auseinanderlaufen, sobald jemand ein
    fuenftes Pflichtfeld ergaenzt.

    Lokaler Import gegen einen Zirkel: `aquabloom_job` zieht Modelle und
    Speicher, die hier oben schon stehen.
    """
    from bewaesserung.aquabloom_job import _ist_konfiguriert
    return _ist_konfiguriert(zone)


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

    # T-0318 Punkt 1, entschieden 28.07.: `setze_konfig` entfernt. Der
    # Verdacht dort ("main.py muesste den Setter rufen, sonst stale Konfig")
    # ist widerlegt -- `main.py` uebergibt `konfig=konfig` bereits im
    # Konstruktor, und eine Konfig-Neuladung zur Laufzeit gibt es nicht
    # (`lade_konfig` laeuft genau einmal beim Start). Der Setter war damit
    # echter toter Code, kein vergessener Aufruf. Anders als bei
    # `kanal_aktiv_bewaesserung` (T-0443): dort war die Vermutung "totes
    # Duplikat" falsch. Deshalb wird hier pro Punkt entschieden, nicht
    # pauschal aufgeraeumt.

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
        niederschlag_6h = wetter.niederschlag_naechste_stunden(6, jetzt)
        niederschlag_24h = wetter.niederschlag_naechste_stunden(24, jetzt)
        et0_6h = wetter.et0_naechste_stunden(6, jetzt)
        et0_24h = wetter.et0_naechste_stunden(24, jetzt)
        # T-0135 (H-4 Stufe 1b): Schwelle Regime-aware aufloesen.
        eff_schwelle_min = effektiv_schwelle_min(zone, jetzt)
        effektive_schwelle, anhebung = self._effektive_schwelle(
            eff_schwelle_min, et0_24h,
        )

        # T-0578 B: vor dem Fensterschluss zaehlt der vorhergesagte Wert.
        messwert = aktuelle_feuchte
        vorgezogen = await self._vorgezogener_bedarf(
            zone, jetzt, wetter, messwert, effektive_schwelle,
            et0_24h=et0_24h, niederschlag_24h=niederschlag_24h,
        )
        if vorgezogen is not None:
            aktuelle_feuchte = vorgezogen.prognose_feuchte

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
        regen_blockt, regen_pp = self._regen_gate(  # T-0439
            zone, aktuelle_feuchte, effektive_schwelle, niederschlag_6h,
        )
        if regen_blockt:
            return await self._erstelle_entscheidung(
                zone_id=zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Feuchte {self._format_zahl(aktuelle_feuchte)}% unter Schwelle "
                    f"{self._format_zahl(effektive_schwelle)}%, aber "
                    + self._regen_gate_text(niederschlag_6h, regen_pp)
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.REGEN_ERWARTET,
            )

        # T-0322: Konvektions-/Starkregen-Guard. Auch bei niedriger mm-Prognose
        # blockieren, wenn die Regenwahrscheinlichkeit sehr hoch ist -- open-meteo
        # unterschaetzt Konvektion notorisch (Realfall 21.06.: 0.4mm/P68%, DWD
        # warnte 15-30 l/m2 + Gewitter). Schwelle konservativ (Default 80%).
        regen_wahrsch_6h = wetter.max_regen_wahrscheinlichkeit_naechste_stunden(6, jetzt)
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

        # T-0560: `effektiv_feuchte_kritisch` statt `zone.feuchte_kritisch`.
        # Ein aktives `feuchte_regime` konnte den Wert bisher nicht kippen,
        # obwohl die Welkepunkt-Kette (`_hole_kalibrier_referenzen`) genau
        # diesen Helfer schon benutzt. `ist_kritisch` ist kein Anzeigewert:
        # es umgeht `bevorzugte_zeiten`, hebt das Tagesbudget um
        # `tages_budget_kritisch_faktor` (hecke: 1.5) und schaltet den
        # Pause-Bypass frei. Im hecke-Winterregime (15.11.-15.03.,
        # `feuchte_kritisch` 14 statt 22) haette der Basiswert diese drei
        # Schranken in genau der Phase geoeffnet, in der das Regime
        # "kaum giessen" bedeutet. magerwiese analog (8 statt 14).
        # T-0578: am MESSWERT, nicht an der Prognose (Modul-Doku vorausschau).
        ist_kritisch = messwert < effektiv_feuchte_kritisch(zone, jetzt)
        in_bevorzugter_zeit, fenster_text = self._pruefe_giessfenster(
            zone, jetzt, wetter,
        )
        if not in_bevorzugter_zeit and not ist_kritisch:
            return await self._erstelle_entscheidung(
                zone_id=zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Feuchte {self._format_zahl(aktuelle_feuchte)}% unter Schwelle, "
                    f"aber {fenster_text}"
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
            *([vorgezogen.text()] if vorgezogen is not None else []),
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
        niederschlag_6h = wetter.niederschlag_naechste_stunden(6, jetzt)
        niederschlag_24h = wetter.niederschlag_naechste_stunden(24, jetzt)
        et0_6h = wetter.et0_naechste_stunden(6, jetzt)
        et0_24h = wetter.et0_naechste_stunden(24, jetzt)

        zonenwerte = []
        for zone, feuchte in feuchte_messpunkte:
            # T-0135 (H-4 Stufe 1b): pro Zone Regime-aware aufloesen
            # (verschiedene Zonen koennen verschiedene Regimes haben).
            eff_schwelle_min = effektiv_schwelle_min(zone, jetzt)
            effektive_schwelle, anhebung = self._effektive_schwelle(
                eff_schwelle_min, et0_24h,
            )
            # T-0578 B: `feuchte` ist ab hier der BEWERTUNGSwert (Trigger,
            # Strategie, Regen-Sperre, Dosis), `messwert` der Sensor (Anzeige,
            # Kritisch-Bypass). Ohne Vorausschau sind beide gleich.
            vorgezogen = await self._vorgezogener_bedarf(
                zone, jetzt, wetter, feuchte, effektive_schwelle,
                et0_24h=et0_24h, niederschlag_24h=niederschlag_24h,
            )
            bewertung = (
                vorgezogen.prognose_feuchte if vorgezogen is not None else feuchte
            )
            if vorgezogen is not None:
                logger.info(
                    "vorausschau.vorgezogen",
                    kanal=kanal, zone_id=zone.zone_id,
                    messwert=round(float(feuchte), 1),
                    prognose=round(float(bewertung), 1),
                    schwelle=round(float(effektive_schwelle), 1),
                    wieder_auf=(
                        vorgezogen.wieder_auf.isoformat()
                        if vorgezogen.wieder_auf is not None else None
                    ),
                    stunden=vorgezogen.stunden,
                )
            zonenwerte.append({
                "zone": zone,
                "feuchte": bewertung,
                "messwert": feuchte,
                "vorgezogen": vorgezogen,
                "effektive_schwelle": effektive_schwelle,
                "anhebung": anhebung,
                "defizit": effektive_schwelle - bewertung,
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
                        str(zw["zone"].zone_id): round(float(zw["messwert"]), 1)
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
        nasseste = max(zonenwerte, key=lambda item: item["messwert"])
        durchschnitt = sum(float(item["messwert"]) for item in zonenwerte) / len(zonenwerte)
        bedarf = [item for item in trigger_werte if item["defizit"] > 0]

        logger.info(
            "entscheidung.kanal_feuchte",
            kanal=kanal,
            trockenste_zone=trockenste["zone"].zone_id,
            min_feuchte=round(float(trockenste["messwert"]), 1),
            nasseste_zone=nasseste["zone"].zone_id,
            max_feuchte=round(float(nasseste["messwert"]), 1),
            durchschnitt=round(durchschnitt, 1),
            einzelwerte={
                str(item["zone"].zone_id): round(float(item["messwert"]), 1)
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
                    f"{self._format_zahl(nasseste['messwert'])}%, "
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
        if startwert["vorgezogen"] is not None:
            start_label += f" ({startwert['vorgezogen'].text()})"
        regen_schwelle_mm = self._regen_schwelle_mm()

        # T-0439: gegen die START-Zone gerechnet -- dieselbe Zone, die auch
        # die Dosis bestimmt (T-0345). Sonst entschieden Trigger und Sperre
        # ueber verschiedene Zonen.
        regen_blockt, regen_pp = self._regen_gate(
            start_zone, start_feuchte, start_schwelle, niederschlag_6h,
        )
        if regen_blockt:
            return await self._erstelle_entscheidung(
                zone_id=ref_zone.zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Kanal {kanal}: {start_label} "
                    f"{self._format_zahl(start_feuchte)}% unter Schwelle "
                    f"{self._format_zahl(start_schwelle)}%, "
                    "aber " + self._regen_gate_text(niederschlag_6h, regen_pp)
                ),
                zeitpunkt=jetzt,
                naechste_pruefung=naechste_pruefung,
                blocker_typ=BlockerTyp.REGEN_ERWARTET,
                scope=EntscheidungsScope.KANAL,
                scope_ref=str(kanal),
            )

        # T-0322: Konvektions-/Starkregen-Guard (analog pruefe_zone) -- blockt bei
        # sehr hoher Regenwahrscheinlichkeit auch ohne mm-Prognose.
        regen_wahrsch_6h = wetter.max_regen_wahrscheinlichkeit_naechste_stunden(6, jetzt)
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

        # T-0560: `effektiv_feuchte_kritisch` statt `zone.feuchte_kritisch`.
        # Ein aktives `feuchte_regime` konnte den Wert bisher nicht kippen,
        # obwohl die Welkepunkt-Kette (`_hole_kalibrier_referenzen`) genau
        # diesen Helfer schon benutzt. `ist_kritisch` ist kein Anzeigewert:
        # es umgeht `bevorzugte_zeiten`, hebt das Tagesbudget um
        # `tages_budget_kritisch_faktor` (hecke: 1.5) und schaltet den
        # Pause-Bypass frei. Im hecke-Winterregime (15.11.-15.03.,
        # `feuchte_kritisch` 14 statt 22) haette der Basiswert diese drei
        # Schranken in genau der Phase geoeffnet, in der das Regime
        # "kaum giessen" bedeutet. magerwiese analog (8 statt 14).
        # T-0578: am MESSWERT, nicht an der Prognose (Modul-Doku vorausschau).
        ist_kritisch = (
            float(startwert["messwert"])
            < effektiv_feuchte_kritisch(start_zone, jetzt)
        )
        in_bevorzugter_zeit, fenster_text = self._pruefe_giessfenster(
            ref_zone, jetzt, wetter,
        )

        if not in_bevorzugter_zeit and not ist_kritisch:
            return await self._erstelle_entscheidung(
                zone_id=ref_zone.zone_id,
                soll_bewaessern=False,
                begruendung=(
                    f"Kanal {kanal}: {start_label} "
                    f"{self._format_zahl(start_feuchte)}% unter Schwelle, "
                    f"aber {fenster_text}"
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
                f"{self._format_zahl(nasseste['messwert'])}%, "
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
        lauf_start: datetime | None = None,
    ) -> tuple[bool, str]:
        """Prueft aktive Kanaele auf Max-Stop nach der nassesten Zone.

        `lauf_start` = Beginn des laufenden Vorgangs. Nur damit ist die
        Frische-Pruefung (T-0445) moeglich; ohne Angabe verhaelt sich die
        Funktion wie vor T-0445.
        """
        jetzt = self._jetzt()
        zonenwerte = []
        for zone in zonen:
            wert = await self._robuste_feuchte(zone.zone_id, jetzt)
            if wert is not None:
                zonenwerte.append({"zone": zone, "feuchte": float(wert)})

        if not zonenwerte:
            # T-0445: der datenaermste Zustand ueberhaupt -- keine einzige
            # Zone des Kanals liefert einen verwertbaren Wert (auch nicht
            # ueber den 48h-Fallback in `_robuste_feuchte`). Bis hierher kehrte
            # die Funktion mit False zurueck, der Lauf lief also gerade dann
            # ungebremst weiter, wenn gar nichts mehr gemessen wird. Ohne
            # `lauf_start` bleibt es dabei (Aufrufer ohne Lauf-Bezug koennen
            # nichts stoppen); mit laufendem Vorgang jenseits des
            # Frische-Fensters wird geschlossen.
            leer_grund = f"Kanal {kanal}: Keine gueltige Feuchtemessung fuer Max-Stop"
            if lauf_start is None:
                return False, leer_grund
            lauf_minuten = (jetzt - lauf_start).total_seconds() / 60.0
            if lauf_minuten <= MAX_STOP_FRISCHE_FENSTER_MIN:
                return False, leer_grund
            zone_ids = [str(z.zone_id) for z in zonen]
            begruendung = (
                f"Kanal {kanal}: keine gueltige Feuchtemessung seit Laufbeginn "
                f"({lauf_minuten:.0f} min, Fenster "
                f"{MAX_STOP_FRISCHE_FENSTER_MIN:.0f} min) -- unbekannter "
                f"Zustand, Zonen {', '.join(zone_ids)}"
            )
            logger.warning(
                "entscheidung.kanal_max_stop_blackout",
                kanal=kanal,
                zonen=zone_ids,
                lauf_start=lauf_start.isoformat(),
                lauf_minuten=round(lauf_minuten, 1),
                fenster_min=MAX_STOP_FRISCHE_FENSTER_MIN,
            )
            await self._melde_frische_ausfall(zone_ids, begruendung, jetzt)
            return True, begruendung

        # T-0440: Zonen mit `kanal_max_stop_ausschluss` beenden den Lauf nicht.
        # Ihr Sensor sitzt in der benetzten Zwiebel am Tropfer und meldet
        # "voll", sobald die ZWIEBEL voll ist -- nicht wenn die Zone versorgt
        # ist. Er schnitt damit einen Lauf ab, den die Geschwisterzone am
        # selben Ventil noch braucht (Median 66 min statt 90).
        #
        # Fallback wie beim Trigger-Ausschluss (T-0382): sind ALLE Zonen des
        # Kanals ausgeschlossen, gilt wieder die volle Liste. Der Max-Stop ist
        # eine SICHERHEITS-Funktion; sie darf nie ganz verschwinden, nur ihre
        # Datenquelle darf gewaehlt werden.
        #
        # T-0444: der Ausschluss nimmt die Zone aus der NORMALEN Schwelle,
        # nicht aus der Sicherheit. Sie wird darunter gegen ihre eigene
        # Notbremse (`kanal_max_stop_notbremse_pp`) geprueft. Grund: T-0440
        # hat die obere Grenze fuer yogaraum ersatzlos entfernt -- bambuswald
        # erreicht seine Schwelle 85 innerhalb eines Laufs nie (~9 h bis zum
        # Peak), der Kanal war damit faktisch unbegrenzt.
        stop_werte = [
            zw for zw in zonenwerte
            if not getattr(zw["zone"], "kanal_max_stop_ausschluss", False)
        ]
        notbremse_werte = [
            zw for zw in zonenwerte
            if getattr(zw["zone"], "kanal_max_stop_ausschluss", False)
        ]
        # Der Fallback haengt an der KONFIGURATION, nicht an der Frage, wer
        # gerade einen Messwert liefert. T-0434 hat den Unterschied scharf
        # gemacht: seit exakt 0.0 als Defekt gilt, faellt eine Zone haeufiger
        # aus `zonenwerte` heraus -- und der Sensor der stop-fuehrenden Zone
        # ist Gardena-Bauart, kann also selbst auf 0.0
        # gehen. Haette der Fallback weiter an `stop_werte` gehangen, waere
        # yogaraum in genau diesem Moment still wieder gegen die normale
        # Schwelle 85 gekappt worden statt gegen seine Notbremse 95 -- der
        # bewusste T-0440-Ausschluss haette sich ohne eine Logzeile
        # aufgeloest, ausgeloest vom Schweigen eines FREMDEN Sensors.
        alle_konfig_ausgeschlossen = all(
            getattr(zone, "kanal_max_stop_ausschluss", False) for zone in zonen
        )
        if not stop_werte and alle_konfig_ausgeschlossen:
            # Degenerierte Config: JEDE Zone des Kanals ist ausgeschlossen.
            # Das ist der T-0382-Fall -- die Sicherheitsfunktion darf nie ganz
            # verschwinden, also gilt wieder die volle Liste gegen die
            # normalen Schwellen. Eine zusaetzliche Notbremsen-Runde waere
            # dann nur Doppelpruefung.
            logger.warning(
                "entscheidung.kanal_max_stop_alle_ausgeschlossen",
                kanal=kanal,
                zonen=[str(zw["zone"].zone_id) for zw in zonenwerte],
            )
            stop_werte = zonenwerte
            notbremse_werte = []
        elif not stop_werte:
            # Die stop-fuehrende Zone existiert, schweigt aber gerade. Der
            # Ausschluss bleibt bestehen; die Aufsicht fuehrt jetzt allein
            # die Notbremse weiter unten. Laut, weil es ein datenaermerer
            # Zustand ist als gedacht.
            logger.warning(
                "entscheidung.kanal_max_stop_stopzone_ohne_messwert",
                kanal=kanal,
                ausgeschlossen=[
                    str(zw["zone"].zone_id) for zw in notbremse_werte
                ],
                ohne_wert=[
                    str(zone.zone_id) for zone in zonen
                    if not getattr(zone, "kanal_max_stop_ausschluss", False)
                ],
            )

        # Wer fuehrt gerade die Aufsicht? Normalerweise `stop_werte`; ist die
        # stop-fuehrende Zone stumm, sind es die Notbremsen-Zonen. Auf DIESER
        # Liste muss die Frische-Pruefung laufen -- sonst traegt eine stumme
        # Notbremse einen drei Stunden alten Wert und niemand merkt es.
        aufsicht = stop_werte or notbremse_werte
        nasseste = max(aufsicht, key=lambda item: item["feuchte"])
        for item in sorted(stop_werte, key=lambda item: item["feuchte"], reverse=True):
            zone = item["zone"]
            feuchte = float(item["feuchte"])
            # T-0491 (13.08.2026): `feuchte_schwelle_max` ist optional, seit
            # zitrus und mandevilla_maxi ihre Nass-Warnung abgegeben haben
            # (sie stand dort dauerhaft an und sagte nichts mehr aus). Beide
            # sind Pump-Zonen ohne `ventil_kanal` und kommen hier nie an --
            # aber die Zeile darunter haette bei None geworfen, und der
            # naechste, der eine Kanal-Zone entlastet, findet den Absturz in
            # der Giess-Entscheidung statt in einem Test.
            if zone.feuchte_schwelle_max is None:
                continue
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

        # T-0444: Notbremse fuer die ausgeschlossenen Zonen. Bewusst NACH der
        # normalen Schwelle -- feuert eine von beiden, ist der Lauf ohnehin
        # zu Ende; die normale Begruendung ist die aussagekraeftigere.
        for item in sorted(
            notbremse_werte, key=lambda item: item["feuchte"], reverse=True,
        ):
            zone = item["zone"]
            feuchte = float(item["feuchte"])
            notbremse = float(
                getattr(zone, "kanal_max_stop_notbremse_pp", 95.0),
            )
            # `>=`, nicht `>`: der Gardena-Sensor liefert ein striktes
            # 5-pp-Raster, oberhalb von 95 gibt es nur noch 100. Mit `>`
            # bliebe der Rasterpunkt 95 selbst bremsfrei und die Notbremse
            # zoege real erst bei 100 -- genau dem Skalenende, an dem keine
            # Aussage mehr moeglich ist. Der Rasterpunkt muss selbst
            # ausloesen. Die NORMALE Schwelle darueber bleibt bei `>`: dort
            # liegt der naechste Rasterpunkt oberhalb der Schwelle.
            if feuchte >= notbremse:
                begruendung = (
                    f"Kanal {kanal}: Notbremse -- Zone {zone.zone_id} "
                    f"{self._format_zahl(feuchte)}% erreicht Notbremse "
                    f"{self._format_zahl(notbremse)}% "
                    f"(vom Max-Stop ausgeschlossen)"
                )
                logger.warning(
                    "entscheidung.kanal_max_stop_notbremse",
                    kanal=kanal,
                    zone_id=zone.zone_id,
                    feuchte=round(feuchte, 1),
                    notbremse_pp=round(notbremse, 1),
                )
                return True, begruendung

        frische_stop, frische_grund = await self._pruefe_max_stop_frische(
            kanal, aufsicht, lauf_start, jetzt,
        )
        if frische_stop:
            return True, frische_grund

        begruendung = (
            f"Kanal {kanal}: nasseste Zone {nasseste['zone'].zone_id} "
            f"{self._format_zahl(nasseste['feuchte'])}% unter Max-Stop"
        )
        return False, begruendung

    async def _pruefe_max_stop_frische(
        self,
        kanal: int,
        stop_werte: list[dict],
        lauf_start: datetime | None,
        jetzt: datetime,
    ) -> tuple[bool, str]:
        """T-0445: hat der Max-Stop waehrend dieses Laufs ueberhaupt Augen?

        Der Zeitstempel einer Messung ist die MESSzeit. Ob die Zeile zum
        Urteilszeitpunkt schon in der DB stand, steht dort nicht -- ein
        Nachtrag aus dem DHS-Backfill sieht hinterher aus wie eine 30 min
        alte Messung, war waehrend des Laufs aber unsichtbar. Gebraucht wird
        die ANKUNFTszeit (`empfangen_am`).

        Ist seit `lauf_start` fuer KEINE stop-relevante Zone ein Wert neu
        eingetroffen und laeuft der Vorgang laenger als das Frische-Fenster,
        ist das kein Freibrief, sondern ein unbekannter Zustand -> schliessen.
        Zonen ohne jede erfasste Ankunft (alle Zeilen `empfangen_am IS
        NULL`, Zustand direkt nach der Migration) liefern dagegen KEINE
        Frische-Aussage und werden uebersprungen -- Karenz, siehe Kommentar
        im Rumpf.
        """
        if lauf_start is None:
            return False, ""
        lauf_minuten = (jetzt - lauf_start).total_seconds() / 60.0
        if lauf_minuten <= MAX_STOP_FRISCHE_FENSTER_MIN:
            return False, ""

        # Alte Speicher-Attrappen ohne den Accessor: Verhalten wie vor T-0445.
        hole_ankunft = getattr(self._speicher, "letzte_ankunft_feuchte", None)
        if hole_ankunft is None:
            return False, ""

        # Karenz nach Migration bzw. Erstinbetriebnahme der Spalte: fuer eine
        # Zone, die NOCH NIE eine erfasste Ankunft hat (alle Bestandszeilen
        # tragen `empfangen_am IS NULL`), gibt es keine Frische-Aussage. Sie
        # zaehlt darum nicht als "nichts eingetroffen", sondern wird
        # uebersprungen -- sonst wuerde ein ueber den Restart wiederher-
        # gestellter Lauf (`gestartet_am` bleibt der originale) im ersten
        # Loop-Tick nach der Migration faelschlich gestoppt. Die Karenz endet
        # von selbst mit dem ersten getrackten Beat der Zone.
        veraltete: list[str] = []
        for zw in stop_werte:
            zone_id = str(zw["zone"].zone_id)
            ankunft = await hole_ankunft(zone_id)
            if ankunft is None:
                continue
            if ankunft > lauf_start:
                return False, ""
            veraltete.append(zone_id)

        if not veraltete:
            return False, ""

        zone_ids = veraltete
        begruendung = (
            f"Kanal {kanal}: kein neu eingetroffener Messwert seit Laufbeginn "
            f"({lauf_minuten:.0f} min, Fenster "
            f"{MAX_STOP_FRISCHE_FENSTER_MIN:.0f} min) -- unbekannter Zustand, "
            f"Zonen {', '.join(zone_ids)}"
        )
        logger.warning(
            "entscheidung.kanal_max_stop_frische",
            kanal=kanal,
            zonen=zone_ids,
            lauf_start=lauf_start.isoformat(),
            lauf_minuten=round(lauf_minuten, 1),
            fenster_min=MAX_STOP_FRISCHE_FENSTER_MIN,
        )
        await self._melde_frische_ausfall(zone_ids, begruendung, jetzt)
        return True, begruendung

    async def _melde_frische_ausfall(
        self, zone_ids: list[str], details: str, jetzt: datetime,
    ) -> None:
        """Ops-Meldung zum Frische-Stop (T-0445).

        Ein Frische-Stop ist kein Routine-Ereignis: er beendet einen Lauf,
        ohne dass ein Messwert das gerechtfertigt haette. Ohne Eintrag in der
        Ops-Timeline waere er nur eine Logzeile -- Detektor ohne Konsument.
        """
        oeffne = getattr(self._speicher, "oeffne_sensor_warnung", None)
        if oeffne is None:
            return
        for zone_id in zone_ids:
            try:
                neu = await oeffne(SensorWarnung(
                    zeitstempel=jetzt,
                    zone_id=zone_id,
                    typ=SensorWarnungTyp.KEINE_ANKUNFT_IM_LAUF,
                    details=details,
                ))
                if not neu:
                    auffrischen = getattr(
                        self._speicher, "aktualisiere_offene_warn_details", None,
                    )
                    if auffrischen is not None:
                        await auffrischen(
                            zone_id,
                            SensorWarnungTyp.KEINE_ANKUNFT_IM_LAUF,
                            details,
                        )
            except Exception:
                # Die Ops-Meldung darf den Stop nie verhindern.
                logger.exception(
                    "entscheidung.kanal_max_stop_frische_warnung_fehler",
                    zone_id=zone_id,
                )

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

        T-0535: laeuft ein Dosis-Test auf dem Ventil dieser Zone, gewinnt
        seine Teststufe VOR jeder Heuristik -- und es wird auch keine
        `ml_dauer_vorschlag`-Zeile geschrieben. Die Zeile wuerde sonst eine
        Heuristik-/ML-Provenienz behaupten fuer eine Dauer, die aus einem
        Wuerfelplan stammt.
        """
        stufe = await self._dosis_test_stufe(zone, jetzt)
        if stufe is not None:
            return dosis_test.haupt_sekunden(
                stufe, zone.pre_soak_min, zone.max_dauer_sekunden,
            )

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
        modus = "wirksam" if self._ml_dosis_wirksam(zone) else "shadow"
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

        if self._ml_dosis_wirksam(zone) and ml_s is not None:
            return ml_s
        return heuristik_s

    async def _dosis_test_stufe(
        self, zone: ZonenKonfig, jetzt: datetime,
    ) -> int | None:
        """T-0535: Teststufe (Gesamt-Minuten) fuer diese Zone, sonst None.

        Zwei Vertraege, die diese Funktion strikt einhaelt:

        (a) NUR LESEN, NICHT ZAEHLEN. `_dauer_mit_ml_weiche` erzeugt eine
            ENTSCHEIDUNG, kein Wasser -- Blocker und Shadow-Modus koennen
            sie folgenlos machen, und beide Aufrufer koennen im selben
            Zyklus laufen. Wuerde hier hochgezaehlt, verbrauchte der Plan
            Stufen ohne Messwert. Der Index kommt deshalb IMMER frisch aus
            `zaehle_dosis_test_laeufe`; solange kein Lauf verbucht ist,
            liefert der Hook stabil dieselbe Stufe.

        (b) KANAL, NICHT ZONE. bambuswald und bambuswald_yogaraum haengen
            am selben Ventil; die Kanal-Entscheidung laeuft unter
            `start_zone`, was die jeweils andere Zone sein kann. Gematcht
            wird auf (ventil_geraet_id, ventil_kanal).

        Fehler beim Zaehlen fuehren zu None (= normale Dosis-Berechnung),
        nicht zu einer geratenen Stufe.
        """
        konfig = getattr(self._konfig, "dosis_test", None)
        if konfig is None or not dosis_test.gilt_fuer_zone(konfig, zone):
            return None
        try:
            index = await self._speicher.zaehle_dosis_test_laeufe(
                konfig.ventil_geraet_id, int(konfig.ventil_kanal),
            )
        except Exception:
            logger.exception(
                "dosis_test.index_fehler", zone_id=zone.zone_id,
            )
            return None
        return dosis_test.stufe_fuer_lauf(konfig, index, jetzt.date())

    def _ml_dosis_wirksam(self, zone) -> bool:
        """T-0512: ersetzt die ML-Dauer fuer DIESE Zone die heuristische?

        Dreistufig wie die Ventilsteuerung: der globale Schalter ist der
        Not-Aus, das Zonen-Opt-in die eigentliche Freigabe. Beides muss `true`
        sein.

        Der Grund fuer die Zonen-Ebene ist geblieben, seine Begruendung nicht:

        Hier stand bis 10.08.2026 "die Shadow-Reihe faellt pro Zone
        GEGENSAETZLICH aus (bambuswald 42,6 gegen 25,8 zugunsten ML)".
        **T-0513 hat das widerlegt, und zwar doppelt:**

        1. Die Zahl war ueber ALLE Modellversionen gepoolt und stammte fast
           vollstaendig aus einem April-Modell. Je Version kehrt sich das
           Urteil um -- die juengste bambuswald-Version verliert.
        2. Schwerer: die Shadow-Reihe kann die Frage prinzipiell nicht
           entscheiden. Gegossen wird immer die Heuristik-Dauer, beide
           Prognosen werden aber gegen DASSELBE eingetretene Delta gemessen.
           Der Schaetzer mit der kleineren vorhergesagten Aenderung gewinnt
           dadurch zwangslaeufig (gemessen: 556 von 556 Faellen). Dazu lag
           `ist_delta_6h` in 82 % der Faelle innerhalb eines
           Fuenfer-Quantisierungsschritts -- es gab gar keine aufloesbare
           Wirkung.

        **Es gibt derzeit also KEINEN Beleg, dass die ML-Dauer irgendwo
        besser ist als die Heuristik.** Die Zonen-Ebene bleibt trotzdem
        richtig -- aber als Vorsichtsmassnahme, nicht als Konsequenz einer
        Messung: sie erlaubt, das Experiment auf eine Zone zu begrenzen.
        Ein gueltiger Test braucht die ML-Dauer TATSAECHLICH gelaufen
        (A/B ueber Laeufe derselben Zone) -- siehe T-0534.

        Wer hier ein Opt-in setzt, startet ein Experiment. Nicht eine
        Verbesserung.
        """
        if not self._response_konfig.wirksam:
            return False
        return bool(getattr(zone, "ml_dosis_opt_in", False))

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
        # T-0561: Stufe 3 der Welkepunkt-Kette -- p10 der Tagesminima. Ueber
        # gemischte Sensoren ist das Minimum systematisch der Sensor mit der
        # niedrigsten Skala, nicht der trockenste Boden. Bei Zonen mit
        # `aggregat_lead_geraet` deshalb nur dessen Werte; die Kette faellt
        # sonst auf Stufe 4 (`feuchte_kritisch`), was ehrlicher ist als eine
        # Schaetzung aus dem falschen Skalenraum.
        lead = None
        hole_lead = getattr(self._speicher, "aggregat_lead", None)
        if callable(hole_lead):
            lead = hole_lead(zone_id)
        try:
            messungen = await self._speicher.hole_messungen(
                zone_id, von=von, bis=jetzt, geraet_id=lead,
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
        # T-0577: ungueltige Prognosen NICHT verwenden. Bis 16.09. blendete
        # nur die Karte sie aus; die Engine rechnete mit derselben Zahl Trigger
        # und Dosis weiter. Das Urteil haengt seit T-0577 an der Prognose
        # selbst (`live_vorhersage`), hier wird es nur gelesen.
        verworfen: dict[str, str | None] = {}
        for h in (6, 12, 24):
            schluessel = f"{h}h"
            v = ergebnisse.get(schluessel)
            if v is None or getattr(v, "feuchte_prognose", None) is None:
                continue
            if not getattr(v, "gueltig", True):
                verworfen[schluessel] = getattr(v, "ungueltig_grund", None)
                continue
            ml_werte[h] = max(
                0.0, min(100.0, round(float(v.feuchte_prognose), 1))
            )

        if verworfen:
            # Laut, nicht leise: dieser Rueckfall weckt einen Pfad, der auf den
            # scharfen Zonen seit dem 17.07. nicht mehr gelaufen ist
            # (Charakterisierung in test_t0577_heuristik_rueckfall.py). Das
            # erste echte Auftreten soll im Log stehen, nicht erst in der Bilanz.
            logger.warning(
                "vorhersage_zone.ml_prognose_ungueltig",
                zone_id=zone_id,
                verworfen=verworfen,
                rueckfall="heuristik" if not ml_werte else "teilweise_ml",
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

        **T-0443: zusaetzlich `kanal_aktiv_bewaesserung`.** Der T-0414-Kommentar
        oben beschreibt die Asymmetrie (Guard rechnet ab SCHLIESSEN, Pause ab
        OEFFNEN) nur in der Richtung "Guard ueberlebt zu lange". Die
        Gegenrichtung fehlte: die Karenz laeuft ab, WAEHREND Wasser fliesst.
        Der Event-Guard ist blind fuer ein offenes Ventil, der Kanal-Zustand
        nicht. Beide zusammen decken "laeuft gerade" und "gerade fertig".

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
        # T-0443: der Event-Guard oben sieht ein GERADE OFFENES Ventil nicht.
        # Er zaehlt nur SCHLIESSEN-Events in einem Zeitfenster; ein OEFFNEN
        # ohne folgendes SCHLIESSEN faellt durch. Realfall 28.07. 05:14:
        # manueller Pre-Soak, Puls-Close 04:12, Hauptlauf seit 04:32 offen ->
        # das letzte SCHLIESSEN lag 2,5 min VOR dem 1-h-Fenster [04:14, 05:14],
        # der offene Hauptlauf war unsichtbar -> Bypass -> Push "giesse 90 min",
        # waehrend das Wasser lief. Die Pre-Soak-Struktur (kurzer Puls, lange
        # Pause, langer Hauptlauf) schiebt das letzte SCHLIESSEN systematisch
        # aus der Karenz; bei Dosen bis 90 min faellt auch das OEFFNEN selbst
        # aus dem Fenster. Ein Zeitfenster kann das nicht loesen, der
        # Kanal-Zustand schon.
        if await self.kanal_aktiv_bewaesserung(zone.zone_id, jetzt):
            logger.info(
                "entscheidung.pause_bypass_geblockt_kanal_aktiv",
                zone_id=zone.zone_id,
                kanal=zone.ventil_kanal,
                zeit=jetzt.isoformat(),
            )
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
        # T-0443: dieselbe Blindstelle wie beim Kritisch-Bypass, anderes
        # Feature (Fenster ist hier `versickerungs_karenz_stunden`). Ein seit
        # 03:00 offener Lauf ist unsichtbar, sobald das letzte SCHLIESSEN
        # laenger als die Karenz zurueckliegt -> proaktiver Trigger waehrend
        # laufendem Wasser. Fachlich falsch aus demselben Grund: der Sensor-
        # Nachlauf hat noch nicht einmal begonnen.
        kuerzlich_gegossen = (
            await self._kuerzlich_gegossen(zone, jetzt)
            or await self.kanal_aktiv_bewaesserung(zone.zone_id, jetzt)
        )
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
                # T-0512: fuer eine unbekannte Zone kann nichts wirksam sein.
                # Der globale Schalter allein waere hier eine Falschaussage
                # Richtung UI -- die ML-Dosis haengt seit 05.08. zusaetzlich
                # am Zonen-Opt-in, und eine Zone, die es nicht gibt, hat keins.
                ml_wirksam=False,
                ml_status_grund="zone_unbekannt",
            )

        ml_aktiv = bool(self._response_konfig.aktiv)
        ml_wirksam = self._ml_dosis_wirksam(zone)

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

            # T-0503 (Andres Entscheidung 05.08.2026): Dosis-Empfehlung fuer
            # Pump-Zonen im Dashboard sichtbar machen.
            #
            # **Warum das noetig war.** Die Response-Modelle der
            # AquaBloom-Zonen werden trainiert, deployed und in den Service
            # geladen -- und von niemandem abgefragt: beide Konsumenten
            # (`pruefe_zone` und dieser Zweig) steigen fuer MONITORING-Zonen
            # vorher aus, der ML-Aufruf steht weiter unten hinter beiden
            # `return`s. Lehrbuchfall
            # [[fehlerpattern_detektor_ohne_konsument]].
            #
            # **Und sie sind gut.** Relativer CV-Fehler, gemessen 05.08.:
            # kasten_4 12,1 %, mandevilla 15,6 % -- gegen hecke 37,1 % und
            # bambuswald_yogaraum 206,1 %, die beide produktiv genutzt
            # werden. Die ungenutzten Modelle waren die besten im Bestand.
            #
            # Rein anzeigend: `soll_bewaessern` bleibt False, diese Zonen
            # haben kein Ventil. Nur fuer vollstaendig konfigurierte
            # Pump-Zonen, damit nicht jede Monitoring-Zone einen
            # Wetter-Abruf plus Inferenz ausloest.
            ml_s_mon: int | None = None
            ml_version_mon: str | None = None
            ml_grund_mon = "zone_monitoring"
            if robust is not None and _ist_pump_zone(zone):
                try:
                    _w = await self._wetter_fuer_zone(zone_id).hole_vorhersage()
                    ml_s_mon, ml_version_mon, ml_grund_mon = (
                        self._ml_dauer_dry_run(
                            zone, jetzt, robust,
                            _w.et0_naechste_stunden(6, jetzt),
                            _w.et0_naechste_stunden(24, jetzt),
                            _w.niederschlag_naechste_stunden(6, jetzt),
                            _w.niederschlag_naechste_stunden(24, jetzt),
                            _eff_schwelle,
                        )
                    )
                except Exception:
                    # Anzeige-Pfad: ein Fehler hier darf die Monitoring-
                    # Empfehlung nicht kippen, sie ist der eigentliche Zweck.
                    logger.exception(
                        "monitoring.ml_dosis_fehler", zone_id=zone_id,
                    )
                    ml_s_mon, ml_version_mon = None, None
                    ml_grund_mon = "inferenz_fehler"

            return GiessEmpfehlung(
                zone_id=zone_id, zeitstempel=jetzt,
                soll_bewaessern=False,
                grund=grund_mon,
                feuchte_aktuell=robust,
                effektive_schwelle=_eff_schwelle,
                ml_aktiv=ml_aktiv, ml_wirksam=ml_wirksam,
                dauer_s_ml=ml_s_mon,
                modell_version=ml_version_mon,
                ml_status_grund=ml_grund_mon,
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
        niederschlag_6h = wetter.niederschlag_naechste_stunden(6, jetzt)
        niederschlag_24h = wetter.niederschlag_naechste_stunden(24, jetzt)
        et0_6h = wetter.et0_naechste_stunden(6, jetzt)
        et0_24h = wetter.et0_naechste_stunden(24, jetzt)
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
        # T-0443: plus laufendes Wasser auf dem Kanal (Begruendung in
        # `_strategie_verdict_pro_zone`, gleiche Blindstelle).
        kuerzlich_gegossen = (
            await self._kuerzlich_gegossen(zone, jetzt)
            or await self.kanal_aktiv_bewaesserung(zone.zone_id, jetzt)
        )
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

        # T-0578 B: dieselbe Vorausschau wie `pruefe_kanal`, sonst zeigte die
        # Karte "kein Bedarf", waehrend die Engine giesst (UI-Vertrag). Die
        # schon berechnete Prognose wird durchgereicht, nicht neu geholt.
        vorgezogen = await self._vorgezogener_bedarf(
            zone, jetzt, wetter, aktuelle_feuchte, effektive_schwelle,
            et0_24h=et0_24h, niederschlag_24h=niederschlag_24h,
            prognose=prognose, decay_pp_pro_tag=prognose_decay_pp_pro_tag,
        )
        # `bewertung` traegt ab hier alles, was die Lage beim spaeteren
        # Giessen beurteilt (Klassifikation, Dosis, Regen-Sperre).
        # `aktuelle_feuchte` bleibt der Sensor: Anzeige, Erklaerung, Kritisch.
        bewertung = (
            vorgezogen.prognose_feuchte if vorgezogen is not None
            else aktuelle_feuchte
        )
        if vorgezogen is not None:
            tage_bis_welke_bewertung = self._zeit_bis_welkepunkt(
                bewertung, welkepunkt_wert, prognose_decay_pp_pro_tag,
            )
            tage_bis_proaktiv_bewertung = await self._physik_reserve_tage(
                zone, bewertung, welkepunkt_wert, jetzt,
            )
        else:
            tage_bis_welke_bewertung = tage_bis_welke
            tage_bis_proaktiv_bewertung = tage_bis_proaktiv

        if (
            aktuelle_feuchte >= effektive_schwelle
            and not prognose_unter_wohl_min
            and not proaktiv_greift
            and vorgezogen is None
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
            zone, bewertung, et0_6h, ziel_schwelle=effektive_schwelle,
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
            zone, jetzt, bewertung, et0_6h, et0_24h,
            niederschlag_6h, niederschlag_24h, effektive_schwelle,
        )
        ampel, mae_h, mae_m, n_bewertet = await self._drift_ampel(zone_id)
        liter_heuristik = self._liter_fuer_dauer(zone, dauer_s_heuristik)

        # T-0535: laeuft ein Dosis-Test auf dem Ventil dieser Zone, faehrt der
        # Auto-Loop die Teststufe -- also muss das Dashboard sie zeigen, sonst
        # widerspricht die Anzeige der Realitaet. Bewusst ueber DIESELBE
        # Funktion wie der scharfe Pfad (`_dauer_mit_ml_weiche` ruft exakt
        # diese zwei Zeilen): eine kopierte Formel koennte wieder divergieren,
        # und genau das ist der Bug, den dieses Feld schliesst.
        #
        # Seiteneffektfrei: `_dosis_test_stufe` liest den Lauf-Index nur
        # (Vertrag (a) dort). `vorhersage_zone` laeuft bei jedem Dashboard-
        # Poll -- wuerde hier gezaehlt, verbrannte die Anzeige den Testplan.
        _dosis_test_stufe_min = await self._dosis_test_stufe(zone, jetzt)
        dauer_s_dosis_test: int | None = (
            dosis_test.haupt_sekunden(
                _dosis_test_stufe_min, zone.pre_soak_min,
                zone.max_dauer_sekunden,
            )
            if _dosis_test_stufe_min is not None
            else None
        )

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
                    aktuelle_feuchte=bewertung,
                    prognose=prognose,
                    welkepunkt_wert=welkepunkt_wert,
                    tage_bis_welke=tage_bis_welke_bewertung,
                    fk_wert=fk_wert,
                    sicherheits_tage_konfig=sicherheits_tage,
                    decay_pp_pro_tag=prognose_decay_pp_pro_tag,
                    tage_bis_proaktiv=tage_bis_proaktiv_bewertung,
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
                    zone, jetzt, bewertung, et0_6h, et0_24h,
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
                        zone, bewertung,
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
                    zone, bewertung, welkepunkt_wert,
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
                    # T-0560: `_liter_fuer_dauer` gibt None zurueck ohne
                    # BilanzKonfig, ohne `ventil_kanal` oder ohne Rate fuer
                    # den Kanal -- `round(None)` warf dann TypeError und riss
                    # Dashboard-Snapshot, Audit-Snapshot und Shadow-Push
                    # dieser Zone mit. Die drei anderen Aufrufer von
                    # `_liter_fuer_dauer` behandeln None bereits; nur hier
                    # fehlte es.
                    _folge_liter = self._liter_fuer_dauer(
                        zone, folge_dose_dauer_s,
                    )
                    folge_dose_liter = (
                        None if _folge_liter is None else round(_folge_liter, 1)
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
            # T-0535: die Teststufe steht an der SPITZE der Hierarchie --
            # sie ist die Dauer, die real gefahren wird, also muss die
            # angezeigte Wassermenge ihr folgen (dieselbe Reihenfolge wie
            # `dauerHauptSekunden` im Frontend).
            if dauer_s_dosis_test is not None:
                liter_haupt = self._liter_fuer_dauer(zone, dauer_s_dosis_test)
            elif soll_bewaessern and ml_wirksam and ml_s is not None:
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
                bewertung, _ziel_transp, dauer_s_empfehlung,
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
                # T-0535: NICHT `dauer_s_heuristik` ueberschreiben -- das
                # Feld traegt weiter die echte Heuristik-Zahl fuer
                # Drift-Ampel und MAE-Auswertung.
                dauer_s_dosis_test=dauer_s_dosis_test,
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
        if vorgezogen is not None:
            basis_grund = (
                f"Feuchte {self._format_zahl(aktuelle_feuchte)}% noch ueber "
                f"Schwelle {self._format_zahl(effektive_schwelle)}%, "
                f"{vorgezogen.text()}"
            )
        elif aktuelle_feuchte < effektive_schwelle:
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

        regen_blockt, regen_pp = self._regen_gate(  # T-0439
            zone, bewertung, effektive_schwelle, niederschlag_6h,
        )
        if regen_blockt:
            grund = (
                f"{basis_grund}, aber "
                + self._regen_gate_text(niederschlag_6h, regen_pp)
            )
            return _erstelle(
                soll_bewaessern=False, blocker_typ=BlockerTyp.REGEN_ERWARTET,
                grund=grund,
            )

        # T-0560: Konvektions-Guard wie in `pruefe_zone` (T-0322), an
        # derselben Stelle der Kette (nach dem mm-Gate). Er fehlte hier,
        # obwohl diese Funktion die Blocker des Entscheidungspfads spiegeln
        # soll. Folge: bei 0,6 mm Prognose und 90 % Regenwahrscheinlichkeit
        # sperrte die Engine mit REGEN_ERWARTET, waehrend das Panel
        # `soll_bewaessern=True` samt Dauer lieferte -- und der Watchdog
        # daraus einen "giesse 22 min"-Push baute.
        regen_wahrsch_6h = wetter.max_regen_wahrscheinlichkeit_naechste_stunden(
            6, jetzt,
        )
        if regen_wahrsch_6h >= self._regen_wahrscheinlichkeit_schwelle():
            return _erstelle(
                soll_bewaessern=False,
                blocker_typ=BlockerTyp.REGEN_ERWARTET,
                grund=(
                    f"{basis_grund}, aber Starkregen wahrscheinlich "
                    f"({self._format_zahl(regen_wahrsch_6h)}% in 6h)"
                ),
            )

        # T-0560: `effektiv_feuchte_kritisch` statt `zone.feuchte_kritisch`.
        # Ein aktives `feuchte_regime` konnte den Wert bisher nicht kippen,
        # obwohl die Welkepunkt-Kette (`_hole_kalibrier_referenzen`) genau
        # diesen Helfer schon benutzt. `ist_kritisch` ist kein Anzeigewert:
        # es umgeht `bevorzugte_zeiten`, hebt das Tagesbudget um
        # `tages_budget_kritisch_faktor` (hecke: 1.5) und schaltet den
        # Pause-Bypass frei. Im hecke-Winterregime (15.11.-15.03.,
        # `feuchte_kritisch` 14 statt 22) haette der Basiswert diese drei
        # Schranken in genau der Phase geoeffnet, in der das Regime
        # "kaum giessen" bedeutet. magerwiese analog (8 statt 14).
        ist_kritisch = aktuelle_feuchte < effektiv_feuchte_kritisch(zone, jetzt)
        in_bevorzugter_zeit, fenster_text = self._pruefe_giessfenster(
            zone, jetzt, wetter,
        )
        if not in_bevorzugter_zeit and not ist_kritisch:
            grund = f"{basis_grund}, aber {fenster_text}"
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
        # T-0560 gepruefT und BEWUSST NICHT geaendert: hier stand kurzzeitig
        # `empf_typ in EMPFEHLUNG_TRIGGERT_BEWAESSERUNG`, weil das Panel
        # `wohlfuehl_grenze` als Giess-Auftrag fuehrt und der Entscheidungs-
        # pfad nicht. Fuenf Bestandstests in `test_vorhersage_zone.py` sichern
        # aber genau diese weitere Lesart ab (einer sagt im Kommentar
        # ausdruecklich "optimum_min > feuchte -> echter Bedarf,
        # soll_bewaessern=True"), und das Frontend haengt seine Anzeige daran.
        # Fuer eine EMPFEHLUNG ist "du koenntest giessen" die richtige
        # Antwort. Falsch war der Konsument: der Watchdog pushte auf diesem
        # Feld, ohne den Typ zu pruefen -- dort ist es gefixt.
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

    async def tagesverbrauch(self, zone_id: str) -> float:
        """T-0444: oeffentlicher Zugang zum Tagesverbrauch einer Zone.

        Der manuelle Pfad (`/api/ventil/manuell-start`, `pre-soak-start`)
        rechnete das Tagesbudget bis dahin gar nicht gegen -- die Logik lebte
        nur im Automatik-Motor. Bewusst ein duenner Wrapper und KEINE zweite
        Query im API-Server: die Filterregel (UNBEKANNT-Events zaehlen nicht)
        darf nur an einer Stelle stehen.
        """
        return await self._tagesverbrauch(zone_id)

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

    async def kanal_aktiv_bewaesserung(
        self, zone_id: str, jetzt: datetime | None = None,
    ) -> bool:
        """T-0119: True wenn auf dem Kanal der Zone gerade ein Bewaesserungs-
        Vorgang laeuft. Zonen ohne `ventil_kanal` -> False. Speicher ohne die
        State-Accessoren (alte Mocks) -> False.

        **T-0447: die Mechanik liegt in `kanal_zustand.kanal_vorgang_laeuft`**,
        gemeinsam mit `sensor_backfill`. Sie deckt seit T-0446 beide
        Zustandstabellen ab -- offenes Ventil UND laufende Pre-Soak-Sequenz,
        deren Soak-Pause eingeschlossen. Diese Methode ist nur noch die
        Zone-zu-Kanal-Aufloesung.

        **T-0443: bis 28.07. war das hier definiert-aber-nie-gerufen** (genau
        eine Fundstelle im Backend, die Definition selbst) — dieselbe
        write-only-Signatur wie `kanal_trigger_ausschluss` vor seinem Fix. In
        Reviews liest sich so etwas wie vorhandener Schutz, ist aber keiner.
        Jetzt zwei Konsumenten: der Kritisch-Bypass unten und der
        Shadow-Empfehlungs-Push im WatchdogJob (deshalb public).

        Die Frage, die diese Funktion beantwortet, ist bewusst eine andere als
        die von `_kuerzlich_gegossen`: hier "laeuft JETZT Wasser" ueber den
        persistierten Zustand und auf KANAL-Ebene, dort "war kuerzlich Wasser"
        ueber ein Event-Zeitfenster und auf ZONEN-Ebene. Beides wird
        gebraucht: das Zeitfenster sieht ein offenes Ventil nicht (ein OEFFNEN
        ohne SCHLIESSEN zaehlt dort nicht mit), und der Kanal-Zustand endet mit
        dem Close, deckt den Sensor-Nachlauf danach also nicht ab.

        Kanal statt Zone ist hier tragend: bambuswald und bambuswald_yogaraum
        haengen seriell am selben Kanal (DSWC1/K2). Haelt die Schwesterzone das
        Ventil offen, laeuft auch hier Wasser.

        `jetzt` fuer die Frische-Schranke (s. `LIVE_LAUF_TOLERANZ`); ohne
        Angabe die Systemzeit. Produktivcode reicht `jetzt` durch, damit Tests
        nicht tageszeitabhaengig werden.
        """
        zone = self._zonen.get(zone_id)
        if zone is None:
            return False
        return await kanal_vorgang_laeuft(
            self._speicher,
            zone_id=zone_id,
            kanal=zone.ventil_kanal,
            geraet_id=zone.ventil_geraet_id,
            jetzt=jetzt or self._jetzt(),
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
            # T-0540: gedrosselt. Der Zustand ist bei den lauten Zonen
            # (pilea, zitrus_ii, avocado, fuchsie) dokumentiert und dauerhaft;
            # sie sind saemtlich `monitoring` ohne Ventilkanal, das `return
            # None` ist dort also folgenlos.
            if _feuchte_warn_drossel.faellig(
                f"sensor_festklemmend_blockiert:{zone_id}", jetzt,
            ):
                logger.warning(
                    "entscheidung.sensor_festklemmend_blockiert",
                    zone_id=zone_id,
                    drossel_stunden=_feuchte_warn_drossel.intervall_stunden,
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
                fenster_minuten=AGGREGAT_FALLBACK_FENSTER_MIN,
                jetzt=jetzt,
            )
            if messung is None or messung.boden_feuchte is None:
                # T-0540: gedrosselt. Lauteste Meldung im ganzen Log
                # (63.008 Eintraege); die betroffenen Zonen
                # (mandevilla_maxi, kroton, fuchsie) haben ihre Sensoren seit
                # 06.08. ausser Hub-Reichweite -- dokumentierter Dauerzustand,
                # kein Vorfall (Memory fyta_sensor_offline_hub_distanz).
                if _feuchte_warn_drossel.faellig(
                    f"keine_messung_im_fallback_horizont:{zone_id}", jetzt,
                ):
                    logger.warning(
                        "entscheidung.keine_messung_im_fallback_horizont",
                        zone_id=zone_id,
                        fallback_fenster_min=AGGREGAT_FALLBACK_FENSTER_MIN,
                        drossel_stunden=(
                            _feuchte_warn_drossel.intervall_stunden
                        ),
                    )
                return None
            # T-0540: gedrosselt, gleiche Klasse wie die beiden darueber --
            # nur ist hier ein Wert vorhanden, bloss aelter. Die Entscheidung
            # laeuft also weiter, unabhaengig davon ob geloggt wird.
            _fallback_faellig = _feuchte_warn_drossel.faellig(
                f"aggregat_fenster_leer_nutze_fallback:{zone_id}", jetzt,
            )
            if _fallback_faellig:
                logger.warning(
                    "entscheidung.aggregat_fenster_leer_nutze_fallback",
                    zone_id=zone_id,
                    alter_minuten=round(
                        (jetzt - messung.zeitstempel).total_seconds() / 60, 1,
                    ),
                    fallback_fenster_min=AGGREGAT_FALLBACK_FENSTER_MIN,
                    drossel_stunden=(
                        _feuchte_warn_drossel.intervall_stunden
                    ),
                )

        wert = float(messung.boden_feuchte)
        # Skala-Verletzung -> Hardware-Defekt, ablehnen
        if not (0.0 <= wert <= 100.0):
            logger.warning(
                "entscheidung.feuchte_ausserhalb_skala",
                zone_id=zone_id, wert=wert,
            )
            return None

        # T-0434: exakt 0.0 ist beim Gardena-Bodensensor die dokumentierte
        # Kontaktverlust-Signatur, kein Messwert (Config-Kommentar
        # default.yaml:488-490, Memory
        # fehlerpattern_gardena_kontaktverlust_trockenphase). Der Sensor
        # faellt in der TROCKENphase auf 0 -- genau dann, wenn die Automatik
        # giessen will.
        #
        # Ein Guard dafuer existiert bereits (`_ist_sensor_festklemmend`,
        # H-1), er haengt aber am LeckDetektor und der braucht
        # EINGEFROREN_FENSTER_STUNDEN = 48 plus >= 10 Messungen. Die ersten
        # ~48 h einer 0.0-Phase waren darum ungeschuetzt: 0.0 <
        # `feuchte_kritisch` -> ist_kritisch -> umgeht `bevorzugte_zeiten`
        # (s. oben im Modul) -> Vollgas zu jeder Tageszeit bis zum
        # Tagesbudget. Bei waldblumenhain sind das 18000 s = 300 min/Tag,
        # also bis zu 600 min Wasser, bevor der Detektor ueberhaupt greift.
        # Nachgezaehlt fuer die 0.0-Phase 01.-09.07.: 9 Entscheidungen mit
        # "Feuchte 0%" gegen 21 mit KEINE_MESSUNG.
        #
        # Der Guard sitzt bewusst HIER und nicht im LeckDetektor: er braucht
        # keine Historie, sondern nur den einen Wert -- und wirkt damit ab
        # der ersten Messung statt ab Stunde 48.
        # T-0489: quellenbewusst -- eine 0.0 von FYTA ist ein echter Messwert.
        # T-0502: und trajektorien-bewusst. Entscheidend ist, wie der Wert auf
        # 0 gekommen ist: heruntergetrocknet (Maximum der letzten 24 h unter
        # 25) oder gesprungen (Sensor raus). Die Abfrage laeuft NUR im
        # Nullfall, nicht bei jeder Entscheidung -- eine Aggregat-Abfrage
        # ueber `idx_sensor_zone_zeit`.
        zone_konf = self._zonen.get(zone_id)
        max_24h = None
        if wert == 0.0:
            # T-0502 (Korrektur 10.08.): Fenster-MAXIMUM statt Vorgaengerwert.
            # Der Vorgaenger ist wegen der Fuenfer-Quantisierung in beiden
            # Klassen 5.0 und trennte deshalb nichts -- Details im Docstring
            # von `null_ist_sensordefekt`.
            max_24h = await self._speicher.max_feuchte_im_fenster(
                zone_id,
                quelle=getattr(messung.quelle, "value", str(messung.quelle)),
                vor=messung.zeitstempel,
            )
        if wert == 0.0 and null_ist_sensordefekt(
            zone_konf, messung.quelle, max_24h,
        ):
            logger.warning(
                "entscheidung.feuchte_null_implausibel",
                zone_id=zone_id,
                geraet_id=str(messung.geraet_id or ""),
                zeitstempel=messung.zeitstempel.isoformat(),
                # T-0502: der Grund gehoert ins Log, sonst laesst sich
                # spaeter nicht unterscheiden, ob der Guard einen Sprung
                # gesehen hat oder gar kein Fenster fand.
                max_24h=max_24h,
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

    async def _vorgezogener_bedarf(
        self, zone: ZonenKonfig, jetzt: datetime, wetter,
        messwert: float, schwelle: float, *,
        et0_24h: float, niederschlag_24h: float,
        prognose: dict[int, float] | None = None,
        decay_pp_pro_tag: float | None = None,
    ) -> VorgezogenerBedarf | None:
        """T-0578 B: faellt die Zone unter die Schwelle, bevor das Giessfenster
        wieder aufgeht? Dann der vorhergesagte Wert, sonst None.

        EINE Stelle fuer `pruefe_kanal` (steuert), `vorhersage_zone`
        (Dashboard/Watchdog) und `pruefe_zone` -- dieselbe Lehre wie beim
        Giessfenster selbst (fehlerpattern_parallele_blocker_kaskade).

        Reihenfolge nach Kosten: Schwelle und Fensterschluss sind reine
        Rechnungen, die Prognose (ML) kommt nur, wenn beides zutrifft -- also
        hoechstens ~6 Engine-Zyklen pro Fensterschluss. Kurz nach einem Lauf
        (Sensor-Nachlauf, T-0286) oder bei laufendem Wasser wird nicht
        vorausgeschaut: der Messwert hat die letzte Gabe noch nicht gesehen,
        eine Prognose darauf wuerde doppelt giessen.
        """
        if messwert < schwelle:
            return None
        schluss = fenster_schliesst_bald(zone, jetzt, wetter)
        if schluss is None:
            return None
        if (
            await self._kuerzlich_gegossen(zone, jetzt)
            or await self.kanal_aktiv_bewaesserung(zone.zone_id, jetzt)
        ):
            return None
        if prognose is None or decay_pp_pro_tag is None:
            decay_basis = self._decay_pp_pro_tag(
                et0_24h_mm=et0_24h,
                niederschlag_24h_mm=niederschlag_24h,
                regen_schwelle_mm=self._regen_schwelle_mm(),
            )
            prognose, _quelle, decay_pp_pro_tag = (
                await self._prognose_ml_oder_heuristik(
                    zone.zone_id, messwert, decay_basis, [6, 12, 24],
                )
            )
        return bewerte_vorausschau(
            schluss=schluss, jetzt=jetzt, messwert=messwert, schwelle=schwelle,
            prognose=prognose, decay_pp_pro_tag=decay_pp_pro_tag,
        )

    def _pruefe_giessfenster(
        self, zone: ZonenKonfig, zeitpunkt: datetime, wetter=None,
    ) -> tuple[bool, str]:
        """Darf die Zone zu `zeitpunkt` starten, und wenn nein: warum?

        **T-0576 E: EINE Stelle fuer alle drei Aufrufer** (`pruefe_zone`, der
        Kanalpfad und `vorhersage_zone`). Eine datengetriebene Bedingung, die
        nur an einem davon haengt, waere `fehlerpattern_parallele_blocker_kaskade`.
        Der Kritisch-Bypass sitzt bei allen dreien AUSSERHALB dieser Funktion
        und gilt damit unveraendert (Andres Entscheid 4).

        Ohne `giessfenster_et0` oder mit `modus: aus`: exakt das bisherige
        Verhalten. `schatten`: bisheriges Verhalten, Bedingung wird nur
        ausgewertet und geloggt. `aktiv`: die Klammer ersetzt
        `bevorzugte_zeiten`, die Bedingung schneidet darin zu -- und wird
        ebenfalls geloggt (Andre 16.09.: das Urteil wird zum Nachjustieren
        der Schwellen gebraucht, auch wenn es scharf ist).

        Der Text ist der Teil nach "aber ..." in der Begruendung. Im
        Aktivmodus nennt er die echte Ursache (Klammer, Sonne, Trocknung)
        statt "ausserhalb bevorzugter Zeit" -- das war mittags schlicht falsch.
        """
        statisch = in_zeitfenstern(zone.bevorzugte_zeiten, zeitpunkt)
        gf = getattr(zone, "giessfenster_et0", None)
        if gf is None or gf.modus == GF_MODUS_AUS:
            return statisch, GF_TEXT_STATISCH

        urteil = urteil_fuer_zone(gf, zeitpunkt, wetter)
        self._logge_giessfenster(zone, gf.modus, zeitpunkt, statisch, urteil)
        if gf.modus == GF_MODUS_SCHATTEN:
            return statisch, GF_TEXT_STATISCH
        if urteil.erlaubt:
            return True, ""
        return False, beschreibe_sperre(gf, urteil)

    def _logge_giessfenster(self, zone, modus, zeitpunkt, statisch, urteil):
        """T-0576 E: Urteil ins Log (`giessfenster.schatten` bzw.
        `giessfenster.aktiv`) -- aber nicht bei jedem Aufruf.

        `statisch_erlaubt` bleibt auch im Aktivmodus drin: es ist das, was die
        alten Uhrzeitfenster gesagt haetten, also der Vergleich, an dem sich
        die Umstellung messen laesst.

        `vorhersage_zone` laeuft auch fuer das Dashboard, das jede Minute
        pollt. Ein Log pro Aufruf fluetete das Service-Log. Geloggt wird, wenn
        sich das Urteil gegenueber dem letzten Eintrag der Zone aendert, und
        sonst hoechstens einmal pro Stunde -- genug, um spaeter jede Stunde
        einzeln kalibrieren zu koennen.
        """
        if not hasattr(self, "_gf_log_letzt"):
            self._gf_log_letzt = {}
        schluessel = (
            modus, zeitpunkt.replace(minute=0, second=0, microsecond=0),
            statisch, urteil.erlaubt, urteil.grund,
        )
        if self._gf_log_letzt.get(zone.zone_id) == schluessel:
            return
        self._gf_log_letzt[zone.zone_id] = schluessel
        logger.info(
            f"giessfenster.{modus}",
            zone_id=zone.zone_id,
            statisch_erlaubt=statisch,
            daten_erlaubt=urteil.erlaubt,
            weicht_ab=statisch != urteil.erlaubt,
            grund=urteil.grund,
            et0_waehrend_mm=(
                round(urteil.et0_waehrend_mm, 2)
                if urteil.et0_waehrend_mm is not None else None
            ),
            et0_nach_mm=(
                round(urteil.et0_nach_mm, 2)
                if urteil.et0_nach_mm is not None else None
            ),
            daten_fehlen=urteil.daten_fehlen,
        )

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

    def _regen_faktor_pp_pro_mm(self, zone) -> float:
        """T-0439: wieviel Feuchte (pp) bringt 1 mm Regen IN DIESER ZONE.

        None an der Zone -> globale Engine-Konstante, also unveraendertes
        Verhalten. Zonen-Werte sind bewusst NICHT gesetzt: die Messung ueber
        21 Regenereignisse liegt auf der Sensor-Aufloesungsgrenze (s.
        Kommentar an `ZonenKonfig.regen_faktor_pp_pro_mm`).
        """
        wert = getattr(zone, "regen_faktor_pp_pro_mm", None)
        return REGEN_FEUCHTE_FAKTOR if wert is None else float(wert)

    def _regen_gate(
        self, zone, aktuelle_feuchte: float, effektive_schwelle: float,
        niederschlag_6h: float,
    ) -> tuple[bool, float]:
        """T-0439: Blockt der erwartete Regen diesen Guss? -> (blockt, pp).

        **Alt:** `niederschlag_6h >= regen_schwelle_mm` -- ein reiner
        mm-Schalter. Er kennt weder die Zone noch ihr Defizit und sperrt eine
        Zone auf 20 % genauso wie eine auf 60 %. Realfall 27.07.: bambuswald
        bei 20 % (kritisch 45) gesperrt wegen 4,5 mm Prognose; Andre hat von
        Hand nachgegossen.

        **Neu:** der erwartete Feuchte-Effekt entscheidet. Gesperrt wird nur,
        wenn der Regen die Zone ueber ihre effektive Schwelle heben WUERDE.
        Cost-Loss (T-0423): ausgefallener Guss teuer, ueberfluessiger billig.

        Zwei bewusste Sicherungen:
        - `regen_gate_immer_ab_mm` (Default 10) sperrt ab Landregen-Menge
          unabhaengig vom Zonen-Faktor -- damit ein zu klein geratener Faktor
          nicht in einen Dauerregen hinein giessen laesst.
        - `regen_gate_modus: "mm"` dreht in einem Wort auf das Alt-Verhalten
          zurueck.

        Das Gate wird STRIKT seltener sperren als vorher (die alte Bedingung
        ist Vorbedingung der neuen). Es giesst also eher mehr, nie weniger.
        """
        schwelle_mm = self._regen_schwelle_mm()
        if niederschlag_6h < schwelle_mm:
            return False, 0.0
        erwartet_pp = self._regen_faktor_pp_pro_mm(zone) * max(
            0.0, niederschlag_6h - schwelle_mm,
        )
        wetter_cfg = getattr(self._konfig, "wetter", None)
        modus = str(getattr(wetter_cfg, "regen_gate_modus", "wirkung") or "wirkung")
        if modus != "wirkung":
            return True, erwartet_pp
        immer_ab = float(getattr(wetter_cfg, "regen_gate_immer_ab_mm", 10.0) or 0.0)
        if immer_ab > 0 and niederschlag_6h >= immer_ab:
            return True, erwartet_pp
        return (aktuelle_feuchte + erwartet_pp >= effektive_schwelle), erwartet_pp

    def _regen_gate_text(self, niederschlag_6h: float, erwartet_pp: float) -> str:
        """Einheitlicher Sperrgrund -- nennt Menge UND erwartete Wirkung."""
        return (
            f"{self._format_zahl(niederschlag_6h)}mm Regen in 6h erwartet "
            f"(~{self._format_zahl(erwartet_pp)} pp in dieser Zone)"
        )

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
