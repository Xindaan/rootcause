"""Feature Engineering fuer Bodenfeuchte-Vorhersage.

Baut aus DB-Rohdaten (Sensor, Ventil, Wetter) eine Feature-Matrix
fuer LightGBM Training und Inferenz.

LEAKAGE-SICHER: Wetter-Features kommen aus Forecasts, die zum
jeweiligen Zeitpunkt t verfuegbar waren — nicht aus tatsaechlich
eingetretenem Wetter.
"""

import asyncio
import bisect
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta

from bewaesserung.bilanz import berechne_bilanz_liter_aus_cache
from bewaesserung.modelle import (
    GesamtKonfig,
    KEINE_WASSER_AUSLOESER,
    SensorMessung,
    VentilEreignis,
    WetterArchivStunde,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher

try:
    import pandas as pd
except ImportError:
    raise ImportError(
        "ML-Abhaengigkeiten fehlen. Installiere mit: pip install -e '.[ml]'"
    )

logger = logging.getLogger(__name__)

# Windrichtungs-Lookup: Text → Grad (Mitte des Sektors)
WIND_RICHTUNG_GRAD = {
    "nord": 0, "n": 0,
    "nordost": 45, "no": 45,
    "ost": 90, "o": 90,
    "suedost": 135, "so": 135,
    "sued": 180, "s": 180,
    "suedwest": 225, "sw": 225,
    "west": 270, "w": 270,
    "nordwest": 315, "nw": 315,
}


def _vpd_kpa(t_celsius: float, rh_prozent: float) -> float:
    """Vapor Pressure Deficit in kPa via Magnus-Formel (T-0045).

    VPD quantifiziert das Verdunstungspotenzial: niedrige Luftfeuchte und
    hohe Temperatur → hoher VPD → hoher Transpirations-Druck auf die Pflanze.
    Implizit in ET0 enthalten, aber ET0 ist eine Tagessumme; VPD traegt den
    stuendlichen Momentan-Stress direkt ins Feature-Set.

    Magnus (IAPWS-Naeherung):
        e_s(T) = 0.6108 * exp(17.27 * T / (T + 237.3))   [kPa]
        VPD    = e_s * (1 - RH/100)

    Vertrag: weder `t_celsius` noch `rh_prozent` duerfen None sein — die
    Aufrufer-Seite (Feature-Extraktion) filtert Bestandsdaten ohne Feuchte
    vorher aus. None hier zu dulden wuerde NaN-Propagation verschleiern
    statt zu fixen, daher explizites TypeError.
    """
    if t_celsius is None or rh_prozent is None:
        raise TypeError(
            "_vpd_kpa benoetigt t_celsius und rh_prozent als float — "
            "None muss vom Aufrufer gefiltert werden (siehe Feature-Extraktion)."
        )
    if rh_prozent < 0 or rh_prozent > 100:
        # defensiv: aus der DB/API koennte ein Ausreisser kommen → clamp
        rh_prozent = max(0.0, min(100.0, rh_prozent))
    e_s = 0.6108 * math.exp(17.27 * t_celsius / (t_celsius + 237.3))
    return e_s * (1.0 - rh_prozent / 100.0)


def _wind_match_ordinal(
    zone: ZonenKonfig,
    standort_id: str,
    konfig: GesamtKonfig,
) -> int:
    """Berechnet wind_match Ordinalwert fuer eine Zone.

    0 = indoor (kein Regen)
    1 = Balkon ohne passende Wind-Config
    2 = Balkon mit Regen-Exposition
    3 = Garten (direkte Exposition)
    """
    if zone.ist_indoor:
        return 0
    if not zone.ist_topf:
        return 3  # Freiland/Garten
    # Topf, outdoor → Balkon
    balkon = konfig.balkon_ausrichtung.get(standort_id)
    if balkon and balkon.regen_wind:
        return 2  # Balkon mit Regen-Exposition
    return 1  # Balkon ohne Config


def _berechne_regen_faktor(
    wind_grad: float,
    zone: ZonenKonfig,
    standort_id: str,
    konfig: GesamtKonfig,
) -> float:
    """Grober Regen-Expositions-Faktor (0.0–1.0).

    Garten: 1.0 (Regen kommt immer an)
    Indoor: 0.0 (kein Regen)
    Balkon: 1.0 bei passendem Wind, 0.2 bei falschem Wind
    """
    if zone.ist_indoor:
        return 0.0
    if not zone.ist_topf:
        return 1.0  # Garten

    balkon = konfig.balkon_ausrichtung.get(standort_id)
    if not balkon or not balkon.regen_wind:
        return 0.5  # Unbekannt → konservativ

    # Pruefe ob Windrichtung zu einer der Regen-Richtungen passt
    for richtung in balkon.regen_wind:
        regen_grad = WIND_RICHTUNG_GRAD.get(richtung.lower(), None)
        if regen_grad is None:
            continue
        # Winkel-Differenz (zirkulaer)
        diff = abs(wind_grad - regen_grad)
        if diff > 180:
            diff = 360 - diff
        if diff <= 67.5:  # ±67.5° Toleranz (~3 Sektoren)
            return 1.0
    return 0.2  # Kein passender Wind


def _zone_zu_standort(zone_id: str, konfig: GesamtKonfig) -> str | None:
    """Findet den Standort einer Zone."""
    for standort in (konfig.standorte or []):
        if zone_id in standort.zonen:
            return standort.standort_id
    return None


def _zone_zu_wetter_standort(zone_id: str, konfig: GesamtKonfig) -> str | None:
    """Findet den Wetter-Standort einer Zone."""
    for standort in (konfig.standorte or []):
        if zone_id in standort.zonen:
            return standort.wetter_standort or standort.standort_id
    return None


def _erster_wetter_standort(konfig: GesamtKonfig) -> str:
    """Fallback-Standort fuer Zonen ohne Standort-Zuordnung: der erste
    KONFIGURIERTE Standort. Frueher stand hier hartkodiert der reale Wohnort --
    fuer jede andere Installation zog das Wetter-Features gegen einen nicht
    existierenden Standort (still leer statt Fehler)."""
    for standort in (konfig.standorte or []):
        return standort.wetter_standort or standort.standort_id
    return ""


class FeatureExtraktor:
    """Baut Feature-Matrix aus DB-Daten fuer ML-Training und Inferenz.

    Jede Zeile = (zone_id, zeitpunkt, features..., ziel_feuchte_6h, _12h, _24h)
    """

    # Lag-Offsets in Stunden
    LAG_STUNDEN = [1, 3, 6, 12, 24]
    # Rolling-Window-Groessen in Stunden
    ROLLING_FENSTER = [3, 6, 12, 24]
    # Vorhersage-Horizonte
    HORIZONTE = [6, 12, 24]

    def __init__(self, speicher: Speicher, konfig: GesamtKonfig):
        self._speicher = speicher
        self._konfig = konfig
        # Zonen-Lookup
        self._zonen: dict[str, ZonenKonfig] = {
            z.zone_id: z for z in konfig.zonen
        }
        # T-0228 Stufe 2c: dynamische Wartungs-Fenster aus DB. Wird
        # vor `erstelle_trainingsdaten` einmalig sync via
        # `setze_wartungs_fenster` befuellt -- der Caller (Training-
        # Pipeline) liest selbst async aus dem Speicher. Live-Inference
        # laesst es leer und filtert nur YAML-Fenster (kurze Fenster
        # haben ohnehin kleinen Live-Impact).
        self._wartungs_fenster: list = []

    def setze_wartungs_fenster(self, fenster: list) -> None:
        """T-0228 Stufe 2c: aktuelle Wartungs-Fenster setzen.

        Erwartet Liste von Dicts mit `zone_id` + `von_am` + `bis_am`
        (NULL = offen, dann wird ein 24h-Cap angewandt). Wird vom
        ML-Training-Pfad gerufen, NICHT von Live-Inference."""
        self._wartungs_fenster = list(fenster or [])

    async def erstelle_trainingsdaten(
        self, von: datetime, bis: datetime,
    ) -> "pd.DataFrame":
        """Baut Feature-Matrix aus DB-Daten.

        LEAKAGE-SICHER: Wetter-Features kommen aus Forecasts,
        die zum jeweiligen Zeitpunkt t verfuegbar waren.

        T-0064: Der CPU-intensive Pandas-Teil (Gruppierung, Feature-Bau,
        DataFrame-Aufbau, Ausschluss-Filter) laeuft in `asyncio.to_thread`,
        damit der Event-Loop auch beim ersten Cache-Miss responsiv bleibt.
        Ohne das blockiert /api/live-vorhersage parallele Requests fuer
        Sensor-/Ventil-Endpoints waehrend des Feature-Builds.

        Returns:
            DataFrame mit Features + Zielvariablen (ziel_feuchte_6h, _12h, _24h).
            Zeilen ohne gueltige Zielvariable werden gefiltert.
        """
        # Lade alle Rohdaten (Puffer: 24h vor 'von' fuer Lag-Features)
        puffer_von = von - timedelta(hours=max(self.LAG_STUNDEN) + 1)
        # Puffer nach 'bis' fuer Zielvariablen
        puffer_bis = bis + timedelta(hours=max(self.HORIZONTE) + 1)

        messungen = await self._speicher.hole_alle_messungen(puffer_von, puffer_bis)
        ventil_ereignisse = await self._speicher.hole_alle_ventil_ereignisse(
            puffer_von, puffer_bis
        )
        # Fuer rueckwaertige Bilanz-Fallbacks brauchen wir auch Forecast-
        # Abfragen kurz vor dem Messpuffer. Der eigentliche Leakage-Schutz
        # passiert spaeter pro Feature-Zeitpunkt t.
        wetter_roh = await self._speicher.hole_wetter_vorhersagen(
            puffer_von - timedelta(hours=max(self.HORIZONTE)), puffer_bis
        )

        if not messungen:
            # F9: Retrain meldet sonst nur "zu_wenig_daten" ohne Grund —
            # ist die DB leer? Zone-Filter falsch? Puffer-Fenster zu kurz?
            logger.warning(
                "ml.features.keine_messungen von=%s bis=%s "
                "ventil_events=%d wetter_stunden=%d",
                puffer_von.isoformat(), puffer_bis.isoformat(),
                len(ventil_ereignisse), len(wetter_roh),
            )
            return pd.DataFrame()

        # Wetter-Index vorab bauen — wir brauchen die Standort-Keys fuer
        # die async Bilanz-Cache-Pulls, danach wird er im Thread
        # gleichermassen verwendet.
        wetter_index = self._baue_wetter_index(wetter_roh)

        # T-0056: Bilanz-Caches einmal pro Wetter-Standort laden, nicht pro Zeile.
        archiv_pro_standort: dict[str, list[WetterArchivStunde]] = {}
        for wsid in wetter_index.keys():
            archiv_pro_standort[wsid] = await self._speicher.hole_wetter_archiv(
                wsid, von=puffer_von, bis=puffer_bis,
            )

        return await asyncio.to_thread(
            self._baue_dataframe_sync,
            messungen,
            ventil_ereignisse,
            wetter_index,
            archiv_pro_standort,
            von,
            bis,
        )

    def _baue_dataframe_sync(
        self,
        messungen,
        ventil_ereignisse,
        wetter_index,
        archiv_pro_standort: "dict[str, list[WetterArchivStunde]]",
        von: datetime,
        bis: datetime,
    ) -> "pd.DataFrame":
        """Sync-CPU-Phase von erstelle_trainingsdaten (T-0064).

        Erwartet die bereits geladenen Rohdaten + Caches und baut daraus
        den Feature-DataFrame. Wird aus dem Event-Loop via to_thread
        aufgerufen; darf selbst keine awaits enthalten.
        """
        # T-0567: Schluessel ist jetzt (zone_id, geraet_id) -- eine Zone mit
        # drei Sensoren liefert drei Serien statt einer gemischten. Alles
        # darunter (Zonen-Konfig, Ventile, Wetter) haengt weiter an der Zone.
        messungen_pro_sensor = self._gruppiere_messungen(messungen)
        ventile_pro_zone = self._gruppiere_ventile(ventil_ereignisse)

        zeilen = []
        for (zone_id, _geraet_id), zone_messungen in messungen_pro_sensor.items():
            zone = self._zonen.get(zone_id)
            if not zone:
                continue

            standort_id = _zone_zu_standort(zone_id, self._konfig)
            wetter_standort = _zone_zu_wetter_standort(zone_id, self._konfig)
            zone_ventile = ventile_pro_zone.get(zone_id, [])

            wsid = wetter_standort or _erster_wetter_standort(self._konfig)
            zone_zeilen = self._baue_zone_features(
                zone=zone,
                messungen=zone_messungen,
                ventile=zone_ventile,
                wetter_index=wetter_index,
                wetter_standort=wsid,
                standort_id=standort_id or "",
                archiv_cache=archiv_pro_standort.get(wsid, []),
                von=von,
                bis=bis,
            )
            zeilen.extend(zone_zeilen)

        if not zeilen:
            logger.warning(
                "ml.features.keine_feature_zeilen "
                "messungen=%d zonen=%d von=%s bis=%s",
                len(messungen), len(messungen_pro_zone),
                von.isoformat(), bis.isoformat(),
            )
            return pd.DataFrame()

        df = pd.DataFrame(zeilen)
        df = self._filtere_ausschluss_fenster(df)
        return df

    def _filtere_ausschluss_fenster(self, df: "pd.DataFrame") -> "pd.DataFrame":
        """Entfernt Zeilen, die in konfigurierten ML-Ausschluss-Fenstern liegen.

        Nutzung: Sensor-Umzug, Kalibrierung, bekannte Datenluecken. Die
        Fenster stehen in config/default.yaml unter ``ml_ausschluss_fenster``
        und werden hier auf (zone_id, zeitstempel) gematcht. T-0267: ein
        Fenster mit gesetztem `geraet_id` greift nur fuer Messungen von
        genau diesem Sensor — verhindert dass FYTA-spezifische Einschlaemm-
        Fenster auch die parallel laufenden Gardena-Messungen ausschneiden.
        """
        fenster = getattr(self._konfig, "ml_ausschluss_fenster", []) or []
        if df.empty:
            return df

        entfernt_gesamt = 0
        for f in fenster:
            mask = (
                (df["zone_id"] == f.zone_id)
                & (df["zeitstempel"] >= f.von.isoformat())
                & (df["zeitstempel"] <= f.bis.isoformat())
            )
            # T-0267: optionaler Sensor-Filter.
            if getattr(f, "geraet_id", None):
                mask = mask & (df["geraet_id"] == f.geraet_id)
            entfernt = int(mask.sum())
            if entfernt:
                entfernt_gesamt += entfernt
                df = df[~mask]

        # T-0228 Stufe 2c: zusaetzlich dynamische Wartungs-Fenster aus DB.
        # Offene Fenster (`bis_am IS NULL`) mit 24h-Cap nach jetzt.
        from datetime import datetime as _dt, timedelta as _td
        cap_jetzt = _dt.now() + _td(days=1)
        for w in self._wartungs_fenster:
            try:
                v = w["von_am"]
                b = w.get("bis_am") or cap_jetzt.isoformat()
            except (KeyError, TypeError):
                continue
            zid = w.get("zone_id")
            if not zid:
                continue
            mask = (
                (df["zone_id"] == zid)
                & (df["zeitstempel"] >= v)
                & (df["zeitstempel"] <= b)
            )
            entfernt = int(mask.sum())
            if entfernt:
                entfernt_gesamt += entfernt
                df = df[~mask]

        if entfernt_gesamt:
            # DEBUG statt INFO: wird bei jedem Feature-Bau (mehrmals pro
            # Minute bei ML-Inferenz) ausgegeben, spamt sonst das Log.
            # Die konkreten Fenster stehen in config/default.yaml.
            import logging
            logging.getLogger(__name__).debug(
                "ML-Ausschluss: %d Zeilen gefiltert (%d Fenster)",
                entfernt_gesamt, len(fenster),
            )
        return df.reset_index(drop=True)

    def _gruppiere_messungen(
        self, messungen: list[SensorMessung],
    ) -> dict[tuple[str, str], list[SensorMessung]]:
        """Gruppiert Messungen nach (Zone, SENSOR), chronologisch sortiert.

        **T-0567: die Gruppierung lief bis hier nur ueber `zone_id`.** Lag-,
        Rolling-, Trend- UND Ziel-Lookups suchen den zeitlich naechsten Wert
        in dieser Liste -- bei einer Multi-Sensor-Zone also quer ueber
        verschiedene Sensoren mit verschiedenen Skalen.

        An Produktivdaten gemessen (04.07.-02.09.): bei `waldblumenhain`
        stammen **13,0 %** der Labels (1732 von 13331) von einem anderen
        Sensor als der Eingangswert, mit einem Median-Fehler von 10 pp und
        Ausreissern bis 100 pp; bei `hecke` 7,3 % mit Median 20 pp. Der
        Rolling-Mittelwert mischt sogar in 100 % der Zeilen. Konkret: eine
        Gardena-Zeile mit eigenem Wert 45 bekam `ziel_delta_6h = -34`, weil
        das "Ziel" von einer FYTA-Sonde kam.

        `response_features.py` hat genau diese Fehlerklasse unter T-0366
        bereits behoben ("f_vor und JEDES f_nach stammen garantiert vom
        selben Sensor"), ebenso `k_basis_fit_job`. `features.py` war die
        letzte nicht nachgezogene Stelle.
        """
        gruppen: dict[tuple[str, str], list[SensorMessung]] = defaultdict(list)
        for m in messungen:
            gruppen[(m.zone_id, m.geraet_id or "")].append(m)
        # Chronologisch sortieren (aelteste zuerst)
        for schluessel in gruppen:
            gruppen[schluessel].sort(key=lambda m: m.zeitstempel)
        return gruppen

    def _gruppiere_ventile(
        self, ereignisse: list[VentilEreignis],
    ) -> dict[str, list[VentilEreignis]]:
        """Gruppiert Ventilereignisse nach Zone, chronologisch."""
        gruppen: dict[str, list[VentilEreignis]] = defaultdict(list)
        for e in ereignisse:
            gruppen[e.zone_id].append(e)
        for zone_id in gruppen:
            gruppen[zone_id].sort(key=lambda e: e.zeitstempel)
        return gruppen

    def _baue_wetter_index(
        self, wetter_roh: list[dict],
    ) -> dict[str, dict]:
        """Indexiert Wetter-Rohdaten nach standort_id fuer schnellen Zugriff.

        Pro Standort liefert die Struktur:
            {
                "alle": list[dict] — original, sortiert nach
                    (abfrage_zeitstempel, vorhersage_zeitstempel)
                    (Speicher.hole_wetter_vorhersagen sortiert in der SQL),
                "abfragen_sortiert": list[str] — distinkte abfrage_zeitstempel,
                    chronologisch aufsteigend
                "pro_abfrage": dict[str, list[dict]] — abfrage_zeitstempel
                    → Liste der zugehoerigen Forecast-Stunden (alle
                    `vorhersage_zeitstempel` mit dieser Abfrage)
            }

        T-0076c: Damit ist `_hole_wetter_fuer_horizont` O(log A + B) pro
        Aufruf statt O(N): bisect_right ueber `abfragen_sortiert` findet
        die juengste Abfrage <= zeitpunkt, danach wird die zugehoerige
        Forecast-Liste direkt aus `pro_abfrage` geholt — kein voller
        Scan ueber `alle` mehr.
        """
        index: dict[str, dict] = {}
        gruppen: dict[str, list[dict]] = defaultdict(list)
        for w in wetter_roh:
            gruppen[w["standort_id"]].append(w)

        for standort_id, eintraege in gruppen.items():
            pro_abfrage: dict[str, list[dict]] = defaultdict(list)
            for w in eintraege:
                pro_abfrage[w["abfrage_zeitstempel"]].append(w)
            # Speicher.hole_wetter_vorhersagen sortiert bereits — aber wir
            # rechnen defensiv: explizit sorted(...) gibt eine korrekte
            # Liste auch wenn der Caller die Quelle aendert.
            abfragen_sortiert = sorted(pro_abfrage.keys())
            index[standort_id] = {
                "alle": eintraege,
                "abfragen_sortiert": abfragen_sortiert,
                "pro_abfrage": dict(pro_abfrage),
            }
        return index

    def _hole_wetter_fuer_horizont(
        self,
        zeitpunkt: datetime,
        horizont_stunden: int,
        wetter_index: dict[str, dict],
        wetter_standort: str,
    ) -> dict:
        """Holt leakage-sichere Wetter-Features fuer einen Horizont.

        Nutzt den neuesten Forecast, der VOR zeitpunkt abgefragt wurde.
        Summiert/mittelt ueber den Horizont-Zeitraum.

        T-0076c: O(log A + B) pro Aufruf — `bisect_right` auf den
        sortierten distinkten `abfrage_zeitstempel` (A Stueck) findet
        die juengste Abfrage <= zeitpunkt, danach wird die zugehoerige
        Forecast-Liste direkt geholt (B Stunden, typisch ~12-72).
        Vorher: linearer Scan ueber alle (~720 × 100 = 72k) Eintraege
        pro Feature-Zeitpunkt.
        """
        bucket = wetter_index.get(wetter_standort)
        if not bucket:
            return self._leere_wetter_features(horizont_stunden)

        zp_iso = zeitpunkt.isoformat()
        horizont_ende = (zeitpunkt + timedelta(hours=horizont_stunden)).isoformat()

        # Binary Search: juengste abfrage_zeitstempel <= zp_iso. ISO-Strings
        # sind lexikographisch chronologisch (wenn Format konsistent).
        abfragen_sortiert = bucket["abfragen_sortiert"]
        idx = bisect.bisect_right(abfragen_sortiert, zp_iso) - 1
        if idx < 0:
            return self._leere_wetter_features(horizont_stunden)
        letzte_abfrage = abfragen_sortiert[idx]

        # Direkter Lookup statt Scan — typisch B = 12-72 Forecast-Stunden
        forecast_stunden = bucket["pro_abfrage"].get(letzte_abfrage, [])
        relevante = [
            w for w in forecast_stunden
            if zp_iso <= w["vorhersage_zeitstempel"] <= horizont_ende
        ]

        # T-0100: Defensive Dedup pro vorhersage_zeitstempel — schuetzt
        # gegen Bestandsdubletten in `wetter_vorhersage` (vor UNIQUE-Index
        # entstanden). Ohne dieses Deduplizieren wuerde `niederschlag_summe`
        # fuer betroffene Feature-Zeitpunkte verdoppelt.
        if relevante:
            gesehen: set[str] = set()
            relevante_uniq: list[dict] = []
            for w in relevante:
                key = w["vorhersage_zeitstempel"]
                if key in gesehen:
                    continue
                gesehen.add(key)
                relevante_uniq.append(w)
            relevante = relevante_uniq

        if not relevante:
            return self._leere_wetter_features(horizont_stunden)

        n = len(relevante)
        niederschlag_summe = sum(w["niederschlag_mm"] or 0 for w in relevante)
        et0_summe = sum(w["et0_mm"] or 0 for w in relevante)
        temp_mittel = sum(w["temperatur"] or 0 for w in relevante) / n
        wind_mittel = sum(w["wind_kmh"] or 0 for w in relevante) / n

        # Windrichtung: zirkulaerer Mittelwert (sin/cos)
        sin_sum = sum(math.sin(math.radians(w["wind_richtung_grad"] or 0)) for w in relevante)
        cos_sum = sum(math.cos(math.radians(w["wind_richtung_grad"] or 0)) for w in relevante)
        wind_richtung_mittel = math.degrees(math.atan2(sin_sum / n, cos_sum / n)) % 360

        # T-0045: VPD als Stunden-Mittel ueber den Horizont.
        # NaN wenn keine der relevanten Stunden Luftfeuchte hat (Bestandsdaten
        # vor Backfill). Einzeln fehlende Stunden werden ausgelassen, nicht
        # mit 0 gefuellt — 0 % RH waere ein unplausibler Wuesten-Wert.
        vpd_werte = [
            _vpd_kpa(w["temperatur"] or 0.0, w["luftfeuchte"])
            for w in relevante
            if w.get("luftfeuchte") is not None and w["temperatur"] is not None
        ]
        vpd_mittel = sum(vpd_werte) / len(vpd_werte) if vpd_werte else None

        return {
            f"niederschlag_summe_{horizont_stunden}h": niederschlag_summe,
            f"et0_summe_{horizont_stunden}h": et0_summe,
            f"temperatur_mittel_{horizont_stunden}h": round(temp_mittel, 2),
            f"wind_kmh_mittel_{horizont_stunden}h": round(wind_mittel, 2),
            f"wind_richtung_sin_{horizont_stunden}h": round(
                math.sin(math.radians(wind_richtung_mittel)), 4
            ),
            f"wind_richtung_cos_{horizont_stunden}h": round(
                math.cos(math.radians(wind_richtung_mittel)), 4
            ),
            f"vpd_mittel_{horizont_stunden}h": (
                round(vpd_mittel, 4) if vpd_mittel is not None else None
            ),
        }

    def _leere_wetter_features(self, horizont_stunden: int) -> dict:
        """Leere Wetter-Features (NaN) wenn kein Forecast verfuegbar."""
        return {
            f"niederschlag_summe_{horizont_stunden}h": None,
            f"et0_summe_{horizont_stunden}h": None,
            f"temperatur_mittel_{horizont_stunden}h": None,
            f"wind_kmh_mittel_{horizont_stunden}h": None,
            f"wind_richtung_sin_{horizont_stunden}h": None,
            f"wind_richtung_cos_{horizont_stunden}h": None,
            f"vpd_mittel_{horizont_stunden}h": None,
        }

    def _hole_bilanz_forecast_bis_zeitpunkt(
        self,
        zeitpunkt: datetime,
        von: datetime,
        bis: datetime,
        wetter_index: dict[str, dict],
        wetter_standort: str,
    ) -> dict[datetime, tuple[float, float]]:
        """Forecast-Fallback fuer Bilanzfeatures ohne Zukunfts-Leakage.

        Fuer jede Vorhersage-Stunde im rueckwaertigen Bilanzfenster wird
        hoechstens die juengste Forecast-Abfrage verwendet, die zum
        Feature-Zeitpunkt `zeitpunkt` bereits verfuegbar war.

        T-0076c: Statt linearen Scan ueber alle Forecasts iterieren wir
        nur ueber Abfragen <= zeitpunkt (bisect_right + Slice). Bei
        einer typischen 90-Tage-Trainingsperiode mit ~720 Abfragen
        spart das Faktor 5-10x Vergleiche pro Feature-Zeitpunkt.
        """
        beste = self._scan_beste_forecast(
            zeitpunkt, von, bis, wetter_index, wetter_standort,
        )
        return self._forecast_aus_beste(beste, von.isoformat(), bis.isoformat())

    def _scan_beste_forecast(
        self,
        zeitpunkt: datetime,
        von: datetime,
        bis: datetime,
        wetter_index: dict[str, dict],
        wetter_standort: str,
    ) -> dict[str, dict]:
        """T-0316: Der teure Teil von `_hole_bilanz_forecast_bis_zeitpunkt` --
        der Prefix-Scan ueber alle zum `zeitpunkt` verfuegbaren Abfragen, der
        pro ziel-Stunde die juengste Forecast-Abfrage behaelt.

        Ausgelagert, damit `_baue_zone_features` den Scan EINMAL pro Feature-
        Zeitpunkt fahren kann (statt 3x pro Horizont mit identischem
        `zeitpunkt`/`bis`) und das Ergebnis nur noch auf die jeweiligen
        Teilfenster filtert (`_forecast_aus_beste`). War der dominante
        DF-Build-Hotspot (Profil: ~2.7s self-time, 6.8 Mio dict.gets).

        Rueckgabe: `ziel_iso -> roher Forecast-Eintrag`.
        """
        bucket = wetter_index.get(wetter_standort)
        if not bucket:
            return {}

        verfuegbar_bis = zeitpunkt.isoformat()
        von_iso = von.isoformat()
        bis_iso = bis.isoformat()

        # Nur Abfragen, die zum Feature-Zeitpunkt verfuegbar waren.
        abfragen_sortiert = bucket["abfragen_sortiert"]
        cutoff = bisect.bisect_right(abfragen_sortiert, verfuegbar_bis)
        if cutoff == 0:
            return {}

        beste_pro_stunde: dict[str, dict] = {}
        # Iteration in chronologischer Reihenfolge — die juengste
        # Abfrage ueberschreibt am Ende automatisch (gleiche `ziel`-
        # Stunde aber spaeteres `abfrage`).
        for abfrage in abfragen_sortiert[:cutoff]:
            for w in bucket["pro_abfrage"].get(abfrage, []):
                ziel = w["vorhersage_zeitstempel"]
                if ziel < von_iso or ziel > bis_iso:
                    continue
                bisher = beste_pro_stunde.get(ziel)
                if bisher is None or abfrage > bisher["abfrage_zeitstempel"]:
                    beste_pro_stunde[ziel] = w

        return beste_pro_stunde

    @staticmethod
    def _forecast_aus_beste(
        beste_pro_stunde: dict[str, dict],
        von_iso: str,
        bis_iso: str,
    ) -> dict[datetime, tuple[float, float]]:
        """T-0316: Filtert das (wiederverwendbare) beste-pro-Stunde-Dict auf
        [von_iso, bis_iso] -- exakt die gleiche String-Vergleichs-Semantik wie
        der Scan -- und konvertiert zu `{ziel_datetime: (niederschlag, et0)}`.
        """
        return {
            datetime.fromisoformat(ziel): (
                float(w["niederschlag_mm"] or 0.0),
                float(w["et0_mm"] or 0.0),
            )
            for ziel, w in beste_pro_stunde.items()
            if von_iso <= ziel <= bis_iso
        }

    def _baue_zone_features(
        self,
        zone: ZonenKonfig,
        messungen: list[SensorMessung],
        ventile: list[VentilEreignis],
        wetter_index: dict[str, list[dict]],
        wetter_standort: str,
        standort_id: str,
        von: datetime,
        bis: datetime,
        archiv_cache: list[WetterArchivStunde] | None = None,
    ) -> list[dict]:
        """Baut Feature-Zeilen fuer eine einzelne Zone."""
        zeilen = []
        archiv_cache = archiv_cache or []

        for i, messung in enumerate(messungen):
            t = messung.zeitstempel
            # Nur Messungen im gewuenschten Zeitraum (nicht Puffer)
            if t < von or t > bis:
                continue
            # Brauche Feuchte als Basis
            if messung.boden_feuchte is None:
                continue

            zeile: dict = {}

            # --- Identifikation ---
            zeile["zone_id"] = zone.zone_id
            zeile["zeitstempel"] = t.isoformat()
            # T-0267: geraet_id mitfuehren, damit pro-Sensor-Ausschluss-
            # Fenster (FYTA-spezifisch, Gardena-spezifisch) zielgenau
            # filtern koennen. Wird in `NICHT_FEATURES` (training.py)
            # als nicht-feature gekennzeichnet und vom Training ignoriert.
            zeile["geraet_id"] = messung.geraet_id

            # --- Aktuelle Sensorwerte ---
            zeile["boden_feuchte_aktuell"] = messung.boden_feuchte
            zeile["boden_temperatur"] = messung.boden_temperatur
            zeile["licht"] = messung.licht

            # --- Lag-Features ---
            for lag_h in self.LAG_STUNDEN:
                lag_wert = self._finde_naechsten_wert(
                    messungen, i, t - timedelta(hours=lag_h)
                )
                zeile[f"feuchte_t_minus_{lag_h}h"] = lag_wert

            # --- Differenz-Features ---
            f_aktuell = messung.boden_feuchte
            f_1h = zeile.get("feuchte_t_minus_1h")
            f_6h = zeile.get("feuchte_t_minus_6h")
            f_24h = zeile.get("feuchte_t_minus_24h")
            zeile["feuchte_diff_1h"] = (f_aktuell - f_1h) if f_1h is not None else None
            zeile["feuchte_diff_6h"] = (f_aktuell - f_6h) if f_6h is not None else None
            zeile["feuchte_diff_24h"] = (f_aktuell - f_24h) if f_24h is not None else None

            # --- Rolling Averages ---
            for fenster_h in self.ROLLING_FENSTER:
                avg = self._rolling_average(
                    messungen, i, t, fenster_h
                )
                zeile[f"feuchte_rolling_{fenster_h}h"] = avg

            # --- Feuchte-Trend (Steigung ueber letzte 6h) ---
            zeile["feuchte_trend_6h"] = self._berechne_trend(messungen, i, t, 6)

            # --- Bewaesserungs-Features ---
            zeile.update(self._bewaesserungs_features(ventile, t))

            # --- Wetter-Features (leakage-sicher) ---
            for horizont in self.HORIZONTE:
                zeile.update(self._hole_wetter_fuer_horizont(
                    t, horizont, wetter_index, wetter_standort
                ))

            # --- T-0056: Wasser-Bilanz pro Horizont (rueckwaerts) ---
            # bilanz_liter_Xh = Wasser-Netto (Bewaesserung + Regen - ET0)
            # im Fenster (t - Xh, t]. NaN fuer Zonen ohne flaeche_m2
            # (FYTA-Indoor etc.) → LightGBM behandelt NaN sauber.
            # bilanz_diff_24h ist ein Residual: gemessene Feuchte-Aenderung
            # minus erwartete (Liter → Millimeter). Quantifiziert Versickerung
            # + Pflanzenaufnahme, die sonst nicht explizit modelliert sind.
            # T-0316: Forecast-Prefix-Scan haengt nur von zeitpunkt/bis (=t)
            # ab, nicht von `von`. Einmal fuer das weiteste Fenster scannen,
            # dann pro Horizont auf das Teilfenster filtern (beweisbar
            # identisch -- die von/bis-Filterung waehlt nur welche ziel-Stunden,
            # nicht welche Abfrage je Stunde gewinnt).
            max_h = max(self.HORIZONTE)
            beste_forecast = self._scan_beste_forecast(
                zeitpunkt=t,
                von=t - timedelta(hours=max_h),
                bis=t,
                wetter_index=wetter_index,
                wetter_standort=wetter_standort,
            )
            t_iso = t.isoformat()
            for horizont in self.HORIZONTE:
                bilanz_von = t - timedelta(hours=horizont)
                bilanz_forecast = self._forecast_aus_beste(
                    beste_forecast, bilanz_von.isoformat(), t_iso,
                )
                liter = berechne_bilanz_liter_aus_cache(
                    zone=zone,
                    von=bilanz_von,
                    bis=t,
                    ventile=ventile,
                    archiv=archiv_cache,
                    forecast=bilanz_forecast,
                    bilanz_konfig=self._konfig.bilanz,
                )
                zeile[f"bilanz_liter_{horizont}h"] = (
                    round(liter, 3) if liter is not None else None
                )

            bilanz_24 = zeile.get("bilanz_liter_24h")
            feuchte_diff_24 = zeile.get("feuchte_diff_24h")
            if (bilanz_24 is not None and feuchte_diff_24 is not None
                    and zone.flaeche_m2):
                erwartet_mm = bilanz_24 / zone.flaeche_m2
                zeile["bilanz_diff_24h"] = round(feuchte_diff_24 - erwartet_mm, 3)
            else:
                zeile["bilanz_diff_24h"] = None

            # Wind-Match als separater Baustein. T-0318 Punkt 5, geklaert
            # 28.07.: stand frueher IN der Horizont-Schleife, mit einem
            # f-String ohne Platzhalter (`f"wind_match"`) -- der Verdacht war,
            # dass die Horizonte sich gegenseitig ueberschreiben und nur der
            # letzte ueberlebt. Das ist NICHT der Fall: `_wind_match_ordinal`
            # haengt gar nicht vom Horizont ab, alle drei Durchlaeufe schrieben
            # denselben Wert. Also kein Daten-Bug, aber dreifache Berechnung
            # an der falschen Stelle. Der Wert ist horizont-invariant und
            # gehoert deshalb vor die Schleife.
            zeile["wind_match"] = _wind_match_ordinal(
                zone, standort_id, self._konfig,
            )

            # --- Effektiver Niederschlag + Bausteine ---
            for horizont in self.HORIZONTE:
                niederschlag_roh = zeile.get(f"niederschlag_summe_{horizont}h")
                # Roh-Niederschlag (unveraendert, bereits im Dict)
                zeile[f"niederschlag_roh_{horizont}h"] = niederschlag_roh

                # Effektiver Niederschlag (grobe Heuristik)
                if niederschlag_roh is not None:
                    # Windrichtung fuer Regen-Faktor
                    wind_sin = zeile.get(f"wind_richtung_sin_{horizont}h", 0) or 0
                    wind_cos = zeile.get(f"wind_richtung_cos_{horizont}h", 1) or 1
                    wind_grad = math.degrees(math.atan2(wind_sin, wind_cos)) % 360
                    faktor = _berechne_regen_faktor(
                        wind_grad, zone, standort_id, self._konfig
                    )
                    zeile[f"effektiver_niederschlag_{horizont}h"] = round(
                        niederschlag_roh * faktor, 3
                    )
                else:
                    zeile[f"effektiver_niederschlag_{horizont}h"] = None

            # --- Zeitliche Features (zirkulaer kodiert) ---
            zeile["stunde_sin"] = round(math.sin(2 * math.pi * t.hour / 24), 4)
            zeile["stunde_cos"] = round(math.cos(2 * math.pi * t.hour / 24), 4)
            tag_im_jahr = t.timetuple().tm_yday
            zeile["tag_im_jahr_sin"] = round(
                math.sin(2 * math.pi * tag_im_jahr / 365), 4
            )
            zeile["tag_im_jahr_cos"] = round(
                math.cos(2 * math.pi * tag_im_jahr / 365), 4
            )

            # --- Zonen-Features (statisch) ---
            zeile["ist_topf"] = int(zone.ist_topf)
            zeile["ist_indoor"] = int(zone.ist_indoor)
            zeile["hat_regen_exposition"] = int(not zone.ist_indoor)
            # Sonnig: aus balkon_ausrichtung (True fuer Garten als Default)
            balkon = self._konfig.balkon_ausrichtung.get(standort_id)
            zeile["sonnig"] = int(balkon.sonnig if balkon else not zone.ist_indoor)
            zeile["feuchte_schwelle_min"] = zone.feuchte_schwelle_min
            zeile["zone_kategorie"] = zone.zone_id
            zeile["sensor_quelle"] = messung.quelle.value
            # Legacy-Alias fuer bereits deployte Modelle. Neues Training
            # schliesst diese Spalte aus und nutzt zone_kategorie/sensor_quelle.
            zeile["quelle"] = zone.zone_id

            # --- Zielvariablen ---
            # T-0062a: Delta-Ziel zusaetzlich zum absoluten Ziel. Der
            # Regressor trainiert ab jetzt auf `ziel_delta_Xh` (Aenderung
            # gegenueber aktueller Feuchte), nicht auf `ziel_feuchte_Xh`
            # (absolutes Niveau). Grund: der absolute Regressor hatte einen
            # Bias-Anker bei ~44 % (Mittel der Trainingszielverteilung),
            # der bei Saettigung 100 % die Prognose Richtung 70 zog.
            # Das Delta-Ziel ist mean ≈ 0 und zieht nicht zum Mean zurueck.
            aktuelle_feuchte = zeile.get("boden_feuchte_aktuell")
            for horizont in self.HORIZONTE:
                ziel = self._finde_naechsten_wert(
                    messungen, i, t + timedelta(hours=horizont),
                    richtung="vorwaerts",
                )
                zeile[f"ziel_feuchte_{horizont}h"] = ziel
                # Delta nur setzen wenn beide Werte vorhanden
                if ziel is not None and aktuelle_feuchte is not None:
                    zeile[f"ziel_delta_{horizont}h"] = ziel - aktuelle_feuchte
                else:
                    zeile[f"ziel_delta_{horizont}h"] = None

            zeilen.append(zeile)

        return zeilen

    def _finde_naechsten_wert(
        self,
        messungen: list[SensorMessung],
        aktueller_index: int,
        ziel_zeit: datetime,
        richtung: str = "rueckwaerts",
        max_abweichung_min: int = 45,
    ) -> float | None:
        """Findet den Feuchte-Wert naechst am Zielzeitpunkt.

        Sucht ab aktueller_index rueckwaerts (fuer Lags) oder
        vorwaerts (fuer Zielvariablen). Akzeptiert max 45min Abweichung.
        """
        beste_abweichung = timedelta(minutes=max_abweichung_min)
        bester_wert = None

        if richtung == "rueckwaerts":
            bereich = range(aktueller_index, -1, -1)
        else:
            bereich = range(aktueller_index, len(messungen))

        for j in bereich:
            m = messungen[j]
            if m.boden_feuchte is None:
                continue
            abweichung = abs(m.zeitstempel - ziel_zeit)
            if abweichung <= beste_abweichung:
                beste_abweichung = abweichung
                bester_wert = m.boden_feuchte
            elif richtung == "rueckwaerts" and m.zeitstempel < ziel_zeit - timedelta(minutes=max_abweichung_min):
                break  # Zu weit in der Vergangenheit
            elif richtung == "vorwaerts" and m.zeitstempel > ziel_zeit + timedelta(minutes=max_abweichung_min):
                break  # Zu weit in der Zukunft

        return bester_wert

    def _rolling_average(
        self,
        messungen: list[SensorMessung],
        aktueller_index: int,
        zeitpunkt: datetime,
        fenster_stunden: int,
    ) -> float | None:
        """Gleitender Durchschnitt der Feuchte ueber die letzten N Stunden."""
        grenze = zeitpunkt - timedelta(hours=fenster_stunden)
        werte = []
        for j in range(aktueller_index, -1, -1):
            m = messungen[j]
            if m.zeitstempel < grenze:
                break
            if m.boden_feuchte is not None:
                werte.append(m.boden_feuchte)
        return round(sum(werte) / len(werte), 2) if werte else None

    def _berechne_trend(
        self,
        messungen: list[SensorMessung],
        aktueller_index: int,
        zeitpunkt: datetime,
        stunden: int,
    ) -> float | None:
        """Lineare Steigung der Feuchte (% pro Stunde) ueber die letzten N Stunden."""
        grenze = zeitpunkt - timedelta(hours=stunden)
        punkte = []  # (stunden_offset, feuchte)
        for j in range(aktueller_index, -1, -1):
            m = messungen[j]
            if m.zeitstempel < grenze:
                break
            if m.boden_feuchte is not None:
                offset = (m.zeitstempel - grenze).total_seconds() / 3600
                punkte.append((offset, m.boden_feuchte))

        if len(punkte) < 2:
            return None

        # Einfache lineare Regression
        n = len(punkte)
        sum_x = sum(p[0] for p in punkte)
        sum_y = sum(p[1] for p in punkte)
        sum_xy = sum(p[0] * p[1] for p in punkte)
        sum_xx = sum(p[0] ** 2 for p in punkte)
        nenner = n * sum_xx - sum_x ** 2
        if abs(nenner) < 1e-10:
            return 0.0
        steigung = (n * sum_xy - sum_x * sum_y) / nenner
        return round(steigung, 4)

    def _bewaesserungs_features(
        self, ventile: list[VentilEreignis], zeitpunkt: datetime,
    ) -> dict:
        """Bewaesserungs-Features: Dauer, Haeufigkeit, letzte Bewaesserung."""
        ergebnis: dict = {
            "stunden_seit_letzter_bewaesserung": None,
            "letzte_bewaesserung_dauer_s": None,
            "bewaesserung_summe_6h": 0,
            "bewaesserung_summe_12h": 0,
            "bewaesserung_summe_24h": 0,
            "bewaesserung_anzahl_24h": 0,
            "bewaesserung_letzte_stunde": 0,
        }

        if not ventile:
            return ergebnis

        grenze_1h = zeitpunkt - timedelta(hours=1)
        grenze_6h = zeitpunkt - timedelta(hours=6)
        grenze_12h = zeitpunkt - timedelta(hours=12)
        grenze_24h = zeitpunkt - timedelta(hours=24)

        # Paarweise Zuordnung OEFFNEN→SCHLIESSEN:
        # - OEFFNEN ohne Dauer (Gardena): Dauer kommt erst mit nachfolgendem SCHLIESSEN
        # - OEFFNEN mit Dauer (manuell geloggt): sofort finalisiert
        # - SCHLIESSEN mit Dauer (Gardena): finalisiert letztes offenes OEFFNEN
        letzte_bewaesserung: datetime | None = None
        letzte_dauer: int | None = None
        offen: bool = False  # wartet letztes OEFFNEN auf SCHLIESSEN?

        for v in ventile:
            if v.zeitstempel > zeitpunkt:
                break  # Nur vergangene Events
            if v.ausloser in KEINE_WASSER_AUSLOESER:
                continue

            if v.aktion.value == "oeffnen":
                letzte_bewaesserung = v.zeitstempel
                if v.dauer_sekunden > 0:
                    letzte_dauer = v.dauer_sekunden
                    offen = False
                else:
                    letzte_dauer = None
                    offen = True
            elif v.aktion.value == "schliessen" and v.dauer_sekunden > 0 and offen:
                # Finalisiere die laufende Bewaesserung mit der gemessenen Dauer
                letzte_dauer = v.dauer_sekunden
                offen = False

            # Zaehl-/Summen-Features: jedes Event mit Dauer > 0 traegt bei
            if v.dauer_sekunden > 0:
                if v.zeitstempel >= grenze_24h:
                    ergebnis["bewaesserung_anzahl_24h"] += 1
                    ergebnis["bewaesserung_summe_24h"] += v.dauer_sekunden
                if v.zeitstempel >= grenze_12h:
                    ergebnis["bewaesserung_summe_12h"] += v.dauer_sekunden
                if v.zeitstempel >= grenze_6h:
                    ergebnis["bewaesserung_summe_6h"] += v.dauer_sekunden
                if v.zeitstempel >= grenze_1h:
                    ergebnis["bewaesserung_letzte_stunde"] = 1

        if letzte_bewaesserung:
            diff = (zeitpunkt - letzte_bewaesserung).total_seconds() / 3600
            ergebnis["stunden_seit_letzter_bewaesserung"] = round(diff, 2)
            # Dauer nur setzen wenn wirklich bekannt (nicht fallback auf fremde Events)
            ergebnis["letzte_bewaesserung_dauer_s"] = letzte_dauer

        return ergebnis
