"""Wasser-Bilanz pro Zone (T-0029).

Berechnet fuer ein Zeitfenster:
- **Zugefuehrt**: Bewaesserung (Kanal-Durchfluss × Dauer × Zone-Anteil
  ODER explizite `liter` aus ventil_ereignis) + Regen (mm × Flaeche)
- **Verdunstet**: ET0 × Flaeche
- **Bilanz**: Zugefuehrt − Verdunstet

Regen und ET0 kommen bevorzugt aus `wetter_archiv` (T-0035 Ground-Truth),
Fallback auf `wetter_vorhersage` (Forecast zum Abfragezeitpunkt) wenn
das Archiv dort noch nicht reicht. Wir labeln die Antwort entsprechend.

Mixed-Quelle ist OK — z.B. Forecast fuer die letzten 24 h (Archiv hat
5-Tage-Delay), Archiv fuer die ersten 5 Tage einer 7-Tages-Bilanz.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from bewaesserung.modelle import (
    Ausloser,
    BilanzKonfig,
    KEINE_WASSER_AUSLOESER,
    VentilAktion,
    VentilEreignis,
    WetterArchivStunde,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher

QuelleNiederschlag = Literal["archiv", "forecast", "gemischt", "keine"]


@dataclass
class WasserBilanz:
    zone_id: str
    fenster_von: datetime
    fenster_bis: datetime
    flaeche_m2: float
    bewaesserung_liter: float
    regen_liter: float
    zugefuehrt_liter: float
    verdunstet_liter: float
    bilanz_liter: float
    quelle_niederschlag: QuelleNiederschlag
    indikativ: bool  # True falls Defaults genutzt (keine Messung/Kalibrierung)
    # T-0575: WELCHE Seite unsicher ist. `indikativ` allein faltet zwei
    # verschiedene Ursachen in ein Bool -- eine Bewaesserung ohne Literwert
    # und ein Wetter aus der Vorhersage. Die Kachel zeigte daraufhin auch
    # bei einer Bewaesserungs-Unsicherheit einen Tooltip ueber die
    # WETTER-Quelle, nannte also die falsche Ursache. Getrennt gefuehrt
    # kann die Anzeige sagen, was sie wirklich weiss.
    bewaesserung_indikativ: bool = False


async def berechne_bilanz(
    zone: ZonenKonfig,
    von: datetime,
    bis: datetime,
    speicher: Speicher,
    bilanz_konfig: BilanzKonfig,
    standort_id: str,
) -> WasserBilanz | None:
    """Berechnet die Wasser-Bilanz fuer eine Zone im Zeitraum [von, bis].

    Gibt None zurueck, wenn die Zone keine `flaeche_m2` konfiguriert hat —
    dann ist eine Bilanz nicht sinnvoll. Aufrufer entscheidet ueber Fallback.
    """
    if zone.flaeche_m2 is None or zone.flaeche_m2 <= 0:
        return None

    bewaesserung_l, bewaesserung_indikativ = await _summe_bewaesserung(
        zone, von, bis, speicher, bilanz_konfig,
    )
    regen_mm, et0_mm, quelle = await _summe_regen_und_et0(
        standort_id, von, bis, speicher,
    )

    regen_l = regen_mm * zone.flaeche_m2
    verdunstet_l = et0_mm * zone.flaeche_m2
    zugefuehrt_l = bewaesserung_l + regen_l

    return WasserBilanz(
        zone_id=zone.zone_id,
        fenster_von=von,
        fenster_bis=bis,
        flaeche_m2=zone.flaeche_m2,
        bewaesserung_liter=round(bewaesserung_l, 1),
        regen_liter=round(regen_l, 1),
        zugefuehrt_liter=round(zugefuehrt_l, 1),
        verdunstet_liter=round(verdunstet_l, 1),
        bilanz_liter=round(zugefuehrt_l - verdunstet_l, 1),
        quelle_niederschlag=quelle,
        indikativ=bewaesserung_indikativ or quelle == "forecast",
        bewaesserung_indikativ=bewaesserung_indikativ,
    )


def berechne_bilanz_liter_aus_cache(
    zone: ZonenKonfig,
    von: datetime,
    bis: datetime,
    ventile: list[VentilEreignis],
    archiv: list[WetterArchivStunde],
    forecast: dict[datetime, tuple[float, float]],
    bilanz_konfig: BilanzKonfig,
) -> float | None:
    """T-0056: Sync-Variante fuer ML-Feature-Export ohne DB-Hits.

    `ventile` muss bereits nach zone_id gefiltert sein. `archiv`/`forecast`
    stammen vom richtigen Standort. Rueckgabe: Bilanz in Litern, oder None
    wenn die Zone keine `flaeche_m2` hat (dann kein Feature moeglich).

    Semantik identisch zu `berechne_bilanz()`, ohne DB-Round-Trips:
      bilanz_liter = bewaesserung_liter + regen_liter - verdunstet_liter
    """
    if zone.flaeche_m2 is None or zone.flaeche_m2 <= 0:
        return None

    bewaesserung_l = 0.0
    for e in ventile:
        if e.zeitstempel < von or e.zeitstempel > bis:
            continue
        if not ist_wasser_ereignis(e):   # T-0566: eine Regel, vier Leser
            continue
        liter = ereignis_zu_liter(e, zone, bilanz_konfig)
        if liter is None:
            continue
        bewaesserung_l += liter

    archiv_stunden = {
        a.zeitstempel.replace(minute=0, second=0, microsecond=0)
        for a in archiv if von <= a.zeitstempel <= bis
    }
    regen_mm = sum(a.niederschlag_mm for a in archiv if von <= a.zeitstempel <= bis)
    et0_mm = sum(a.et0_mm for a in archiv if von <= a.zeitstempel <= bis)

    for zeit, (r, e) in forecast.items():
        if not (von <= zeit <= bis):
            continue
        if zeit in archiv_stunden:
            continue
        regen_mm += r
        et0_mm += e

    regen_l = regen_mm * zone.flaeche_m2
    verdunstet_l = et0_mm * zone.flaeche_m2
    return bewaesserung_l + regen_l - verdunstet_l


def ist_wasser_ereignis(e: VentilEreignis) -> bool:
    """T-0566: Traegt dieses Ereignis eine Wassermenge?

    Die eine Regel fuer alle Aggregatoren. Sie muss zwei Formen kennen:
    - Das **Paar** (Live-WS, DHS, Pre-Soak): die Menge sitzt am SCHLIESSEN,
      das OEFFNEN traegt `dauer=0` und keine Liter.
    - Das **Einzel-OEFFNEN** aus `POST /api/giessen`: der Nutzer loggt eine
      Giesskannen-/Schlauch-Aktion nachtraeglich, es gibt kein Ventil und
      also auch kein SCHLIESSEN. Das ist eine bewusste Ausnahme des
      Ventil-Event-Vertrags ([[ventil_event_vertrag]], Ergaenzung
      17.04.2026), keine Schlamperei.

    Bis T-0566 kannte nur `bilanz.py` beide Formen -- und dort stand die
    Bedingung zweimal woertlich. Wochen-Report und der FAO-56-Bilanz-Job
    filterten hart auf SCHLIESSEN und verloren dieses Wasser: drei
    Verbraucher, zwei Zaehlregeln. Aktuell schlafend (die sechs Alt-Events
    liegen im April/Mai 2026, 0 in den letzten 90 Tagen), kehrt aber bei
    der naechsten Schlauch-Bewaesserung zurueck.
    """
    return not (
        e.aktion == VentilAktion.OEFFNEN
        and e.dauer_sekunden == 0
        and e.liter is None
    )


async def _summe_bewaesserung(
    zone: ZonenKonfig,
    von: datetime, bis: datetime,
    speicher: Speicher,
    bilanz_konfig: BilanzKonfig,
) -> tuple[float, bool]:
    """Liter Bewaesserungswasser dieser Zone im Fenster. True wenn Fallback."""
    indikativ = False
    ereignisse = await speicher.hole_ventil_ereignisse(
        zone.zone_id, von=von, bis=bis,
    )
    total = 0.0
    for e in ereignisse:
        # OEFFNEN-Eintraege haben Dauer=0 — die echte Bewaesserungsmenge
        # steht am paarweisen SCHLIESSEN. Beim manuellen API-Logging gibt es
        # nur ein OEFFNEN-Event mit voller Dauer (kein SCHLIESSEN). Daher:
        # OEFFNEN MIT positiver Dauer zaehlt, OEFFNEN MIT 0s wird stillschweigend
        # uebersprungen (kein "indikativ"-Flag).
        if not ist_wasser_ereignis(e):   # T-0566: eine Regel, vier Leser
            continue
        liter = ereignis_zu_liter(e, zone, bilanz_konfig)
        if liter is None:
            indikativ = True
            continue
        total += liter
    return total, indikativ


def ereignis_zu_liter(
    e: VentilEreignis, zone: ZonenKonfig, konfig: BilanzKonfig,
) -> float | None:
    """Konvertiert ein Ventilereignis in Liter. None wenn weder liter noch Dauer."""
    # 0. Kein Zonenwasser -- UNBEKANNT (Heuristik unklassifiziert), IGNORIERT
    #    (vom User als Phantom markiert) oder FREMDWASSER (echter Sprung, aber
    #    aus dem Kanal einer Nachbarzone). Dieser Ausschluss steht VOR allem
    #    anderen, auch vor dem expliziten Literwert: ein spaeter
    #    umklassifiziertes Ereignis behaelt sein `liter`-Feld, und mit dem
    #    alten Zweig-1-zuerst wurde es entgegen dem zentralen Vertrag als
    #    Zonenwasser bilanziert (T-0487 / Audit A9). Aktuell noch latent --
    #    am 02.08. gab es 0 DB-Zeilen mit einem dieser Ausloeser und
    #    `liter IS NOT NULL`. Der Aufrufer liest None als "indikativ".
    if e.ausloser in KEINE_WASSER_AUSLOESER:
        return None
    # 1. Expliziter Wert dominiert (manuelle Schlauch-Bewaesserung mit Angabe)
    if e.liter is not None:
        return e.liter
    if e.dauer_sekunden <= 0:
        return None
    # F19: AquaBloom-Puls = fixe Tropfer-Dosis aus der Zonen-Konfig (analog
    # aquabloom_job). Job-konvertierte Events tragen e.liter schon (#1 oben);
    # UI-bulk-geflippte (Banner "AquaBloom") nicht -> sonst zeigt die Bilanz
    # "indikativ" statt Litern. Aus Pumpen-Dauer + Tropfern rechnen, NICHT aus
    # e.dauer (die traegt beim Bulk-Flip noch die Heuristik-Dauer).
    if e.ausloser == Ausloser.AQUABLOOM:
        if (
            zone.aquabloom_pumpen_dauer_sekunden
            and zone.aquabloom_tropfer_anzahl
            and zone.aquabloom_tropfer_liter_pro_stunde
        ):
            return (
                float(zone.aquabloom_pumpen_dauer_sekunden) / 3600.0
                * float(zone.aquabloom_tropfer_anzahl)
                * float(zone.aquabloom_tropfer_liter_pro_stunde)
            )
        return None
    minuten = e.dauer_sekunden / 60.0
    # 2. Manuell via /api/giessen (freier Schlauch, kein Gardena-Ventil) ->
    #    manuell_liter_pro_minute. Andere MANUELL-Events (Live-WebSocket bei
    #    App-Bedienung, DHS-Manual-Events) laufen ueber den Kanal -> Kanal-Rate.
    #    Erkennung an `ventil_id == 'manuell'` (vom manuellen Endpoint gesetzt).
    if e.ausloser == Ausloser.MANUELL and e.ventil_id == "manuell":
        return minuten * konfig.manuell_liter_pro_minute * zone.anteil_kanal
    # 3. (Der Ausschluss der Kein-Wasser-Ausloeser steht als Schritt 0 oben.)
    # 4. Sonst (Automatik, Manuell-via-Gardena, Watchdog, Notfall-Stopp):
    #    Kanal-basierter Durchfluss × Zone-Anteil.
    rate = konfig.liter_pro_minute_fuer_zone(zone)
    if rate is None:
        return None
    return minuten * rate * zone.anteil_kanal


async def _summe_regen_und_et0(
    standort_id: str, von: datetime, bis: datetime, speicher: Speicher,
) -> tuple[float, float, QuelleNiederschlag]:
    """Regen_mm + ET0_mm ueber den Zeitraum. Archiv bevorzugt, Forecast als Fallback."""
    archiv = await speicher.hole_wetter_archiv(standort_id, von=von, bis=bis)
    archiv_stunden = {a.zeitstempel.replace(minute=0, second=0, microsecond=0)
                      for a in archiv}
    regen_mm = sum(a.niederschlag_mm for a in archiv)
    et0_mm = sum(a.et0_mm for a in archiv)

    # Nur fuer Stunden OHNE Archiv-Deckung auf Forecast zurueckfallen.
    forecast = await speicher.hole_forecast_stunden(standort_id, von, bis)
    forecast_regen = 0.0
    forecast_et0 = 0.0
    forecast_benutzt = False
    for zeit, (r, e) in forecast.items():
        if zeit in archiv_stunden:
            continue
        forecast_regen += r
        forecast_et0 += e
        forecast_benutzt = True

    regen_gesamt = regen_mm + forecast_regen
    et0_gesamt = et0_mm + forecast_et0

    if archiv and forecast_benutzt:
        quelle: QuelleNiederschlag = "gemischt"
    elif archiv:
        quelle = "archiv"
    elif forecast_benutzt:
        quelle = "forecast"
    else:
        quelle = "keine"
    return regen_gesamt, et0_gesamt, quelle
