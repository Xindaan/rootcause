"""T-0576 E: Giessfenster aus der Verdunstung statt aus der Uhrzeit.

Andres Entscheid 16.09.2026, Option E: eine geweitete statische Klammer, und
darin eine ZWEISEITIGE Bedingung aus der stuendlichen ET0-Vorhersage.

    Start erlaubt, wenn
      (1) ET0-Summe NACH Zyklusende   >= trocknung_min_mm   kein nasses Laub ueber Nacht
      (2) ET0-Summe WAEHREND Zyklus   <= sonne_max_mm       nicht in die pralle Sonne

**Warum zwei Seiten.** (1) allein war der erste Entwurf und haette den Abend
ausgeschlossen -- zu Recht, gemessen 05.-15.09.: ein Zyklus ab 18 h hat danach
nur 0,06 mm Trocknung, ab 09 h 1,51 mm. Andres Einwand hat (2) ergaenzt: ob
mittags giessen schadet, haengt vom Wetter ab, nicht von der Uhr. Gemessen fuer
12-15 Uhr am selben Standort: 0,21 mm bei 17 °C (16.09., bedeckt) gegen
1,12 mm bei 27 °C (08.09.). Faktor 5 im SELBEN Zeitfenster.

**Warum der Abend so wichtig ist.** waldblumenhain lief in 60 Tagen 22-mal
abends und 21-mal morgens. Der Bedarf entsteht tagsueber (Lead um 08:00 auf 45
ueber Schwelle 40, um 17:00 auf 40). Das Abendfenster war die erste Gelegenheit
danach -- mit der Klammer bis 17:00 wird es der Nachmittag.

**Was hier NICHT entschieden wird, und warum.**
- Der Kritisch-Bypass: er sitzt bei allen drei Aufrufern AUSSERHALB
  (`if not in_bevorzugter_zeit and not ist_kritisch`) und gilt damit unveraendert.
- Die Schwellen: `trocknung_min_mm` und `sonne_max_mm` sind Setzungen, keine
  Messwerte (ET0 ist ueber Gras definiert, der Waldblumenhain steht im
  Eichenschatten). Sie werden im Schattenbetrieb kalibriert. Die Vorhersagen
  dafuer liegen ohnehin persistent in `wetter_vorhersage`.

Reine Funktion ohne DB/Async, damit sie ausfuehrbar testbar ist.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

MODUS_AUS = "aus"
MODUS_SCHATTEN = "schatten"
MODUS_AKTIV = "aktiv"

GRUND_OK = "ok"
GRUND_AUSSERHALB_KLAMMER = "ausserhalb_klammer"
GRUND_ZU_WENIG_TROCKNUNG = "zu_wenig_trocknung"
GRUND_ZU_VIEL_SONNE = "zu_viel_sonne"
GRUND_KEINE_VORHERSAGE = "keine_vorhersage"


@dataclass(frozen=True)
class FensterUrteil:
    """Was die datengetriebene Bedingung zu einem Startzeitpunkt sagt."""

    erlaubt: bool
    grund: str
    et0_waehrend_mm: float | None
    et0_nach_mm: float | None
    #: True, wenn die Vorhersage die benoetigten Stunden nicht abdeckt. Dann
    #: entscheidet die Klammer allein (Andres Entscheid 3: Rueckfall statt
    #: fail-closed -- sonst verdurstet die Zone bei einem Datenausfall).
    daten_fehlen: bool = False


def in_zeitfenstern(fenster, zeitpunkt: datetime) -> bool:
    """Liegt `zeitpunkt` in einem der Fenster? Leere Liste = immer.

    Identische Semantik zur frueheren `_ist_bevorzugte_zeit`, inklusive
    Fenstern ueber Mitternacht (`von > bis`).
    """
    if not fenster:
        return True
    uhr = zeitpunkt.time()
    for f in fenster:
        von = time.fromisoformat(f.von)
        bis = time.fromisoformat(f.bis)
        if von <= bis and von <= uhr <= bis:
            return True
        if von > bis and (uhr >= von or uhr <= bis):
            return True
    return False


def _summe_et0(wetter, von: datetime, bis: datetime) -> tuple[float, bool]:
    """ET0-Summe der Vorhersagestunden in [von, bis). Zweiter Wert: vollstaendig?

    Eine Stunde zaehlt, wenn ihr Beginn im Intervall liegt. Fehlen Stunden
    (Vorhersage endet zu frueh, oder die Abfrage ist gescheitert und `stunden`
    leer), ist das Ergebnis NICHT als Aussage verwertbar -- `sum([])` ist 0 und
    saehe sonst aus wie "keine Verdunstung" (dieselbe Falle wie im Tagesplan,
    T-0575).
    """
    stunden = [
        s for s in (getattr(wetter, "stunden", None) or [])
        if von <= s.zeitstempel < bis
    ]
    erwartet = max(1, int(round((bis - von).total_seconds() / 3600)))
    return sum(float(s.et0_mm or 0.0) for s in stunden), len(stunden) >= erwartet


def bewerte_giessfenster(
    *,
    zeitpunkt: datetime,
    wetter,
    klammer,
    zyklus_min: int,
    trocknung_fenster_h: int,
    trocknung_min_mm: float | None,
    sonne_max_mm: float | None,
) -> FensterUrteil:
    """Das datengetriebene Urteil fuer einen Start um `zeitpunkt`.

    Reihenfolge: erst die Klammer (grob, statisch), dann die Daten. Eine
    Seite ohne gesetzte Schwelle wird nicht geprueft -- so laesst sich im
    Schattenbetrieb jede Haelfte einzeln beobachten.
    """
    if not in_zeitfenstern(klammer, zeitpunkt):
        return FensterUrteil(False, GRUND_AUSSERHALB_KLAMMER, None, None)

    # Stundenraster: der laufende Zyklus beginnt in der aktuellen Stunde.
    start = zeitpunkt.replace(minute=0, second=0, microsecond=0)
    ende = zeitpunkt + timedelta(minutes=zyklus_min)
    ende_stunde = ende.replace(minute=0, second=0, microsecond=0)
    if ende_stunde < ende:
        ende_stunde += timedelta(hours=1)

    waehrend, waehrend_voll = _summe_et0(wetter, start, ende_stunde)
    nach, nach_voll = _summe_et0(
        wetter, ende_stunde, ende_stunde + timedelta(hours=trocknung_fenster_h),
    )

    braucht_nach = trocknung_min_mm is not None
    braucht_waehrend = sonne_max_mm is not None
    if (braucht_nach and not nach_voll) or (braucht_waehrend and not waehrend_voll):
        return FensterUrteil(
            True, GRUND_KEINE_VORHERSAGE,
            waehrend if waehrend_voll else None,
            nach if nach_voll else None,
            daten_fehlen=True,
        )

    if braucht_nach and nach < trocknung_min_mm:
        return FensterUrteil(False, GRUND_ZU_WENIG_TROCKNUNG, waehrend, nach)
    if braucht_waehrend and waehrend > sonne_max_mm:
        return FensterUrteil(False, GRUND_ZU_VIEL_SONNE, waehrend, nach)
    return FensterUrteil(True, GRUND_OK, waehrend, nach)


def urteil_fuer_zone(gf, zeitpunkt: datetime, wetter) -> FensterUrteil:
    """`bewerte_giessfenster` mit den Parametern eines `GiessfensterEt0Konfig`.

    Engine und Tagesplan rufen beide hierueber -- zwei Stellen, die die sechs
    Parameter einzeln durchreichen, laufen beim naechsten neuen Feld
    auseinander (fehlerpattern_parallele_blocker_kaskade).
    """
    return bewerte_giessfenster(
        zeitpunkt=zeitpunkt, wetter=wetter, klammer=gf.klammer,
        zyklus_min=gf.zyklus_min,
        trocknung_fenster_h=gf.trocknung_fenster_h,
        trocknung_min_mm=gf.trocknung_min_mm,
        sonne_max_mm=gf.sonne_max_mm,
    )


def _mm(wert: float | None) -> str:
    return "?" if wert is None else f"{wert:.2f}".replace(".", ",")


def beschreibe_sperre(gf, urteil: FensterUrteil) -> str:
    """Klartext, WARUM das Giessfenster gerade zu ist.

    Ersetzt im Aktivmodus das pauschale "ausserhalb bevorzugter Zeit" -- das
    stimmte nur fuer die alten Uhrzeitfenster. Wer mittags "ausserhalb
    bevorzugter Zeit" liest, sucht den Fehler in der Uhr, nicht im Wetter.
    """
    if urteil.grund == GRUND_AUSSERHALB_KLAMMER:
        fenster = ", ".join(f"{f.von}-{f.bis}" for f in gf.klammer)
        return f"ausserhalb Giessfenster {fenster}"
    if urteil.grund == GRUND_ZU_WENIG_TROCKNUNG:
        return (
            f"trocknet danach nicht ab (Verdunstung {_mm(urteil.et0_nach_mm)} mm "
            f"in {gf.trocknung_fenster_h} h, noetig {_mm(gf.trocknung_min_mm)})"
        )
    if urteil.grund == GRUND_ZU_VIEL_SONNE:
        return (
            f"zu viel Sonne beim Giessen (Verdunstung "
            f"{_mm(urteil.et0_waehrend_mm)} mm, erlaubt {_mm(gf.sonne_max_mm)})"
        )
    return "ausserhalb bevorzugter Zeit"


def naechster_erlaubter_start(
    gf, *, ab: datetime, bis: datetime, wetter, schritt_min: int = 15,
) -> tuple[datetime | None, FensterUrteil | None]:
    """Erster Zeitpunkt in [ab, bis), zu dem das Giessfenster offen ist.

    Fuer die Anzeige ("frei ab 05:00"), nicht fuer die Steuerung: die Engine
    prueft alle 5 min selbst. Raster `schritt_min`, beginnend bei `ab`
    aufgerundet. Liefert zusaetzlich das Urteil des ERSTEN gesperrten
    Schritts (None, wenn schon `ab` frei ist) -- damit die Anzeige sagen
    kann, woran es gerade liegt.
    """
    schritt = timedelta(minutes=schritt_min)
    t = ab.replace(second=0, microsecond=0)
    rest = t.minute % schritt_min
    if rest or t < ab:
        t += timedelta(minutes=schritt_min - rest) if rest else schritt
    erstes_gesperrt: FensterUrteil | None = None
    while t < bis:
        urteil = urteil_fuer_zone(gf, t, wetter)
        if urteil.erlaubt:
            return t, erstes_gesperrt
        if erstes_gesperrt is None:
            erstes_gesperrt = urteil
        t += schritt
    return None, erstes_gesperrt
