"""T-0578 B: vorausschauend giessen, bevor das Giessfenster zugeht.

Andres Entscheid 16.09.2026, Weg B. Seit T-0576 aktiv ist, wartet Bedarf, der
nachmittags entsteht, bis zum naechsten Morgen. Die Engine loeste bisher nur
auf den AKTUELLEN Messwert aus. Hier die zusaetzliche Frage:

    Geht das Giessfenster in den naechsten `VORLAUF_MIN` Minuten zu, und
    faellt die Zone laut Prognose unter ihre Schwelle, BEVOR es wieder
    aufgeht?  -> dann jetzt so entscheiden, als laege sie schon dort.

**Was sich NICHT aendert.** Die Antwort ist ein vorhergesagter Feuchtewert,
kein Giessbefehl. Er ersetzt den Messwert nur fuer den Schwellen-Trigger und
alles, was die Situation zum spaeteren Zeitpunkt bewertet (Strategie-Urteil,
Regen-Sperre, Dosis). Regen, Tagesbudget, Mindestpause und das Giessfenster
selbst greifen unveraendert. Der Kritisch-Bypass haengt weiter am MESSWERT:
eine Prognose darf keine Schranke oeffnen, die fuer echte Not gedacht ist.

**Nur fuer Zonen mit `giessfenster_et0.modus: aktiv`.** Ohne datengetriebenes
Fenster gibt es kein "Fenster geht zu" -- die alten Uhrzeitfenster lagen
morgens und abends, dazwischen war die Nacht ohnehin kurz genug.

Reine Funktionen ohne DB/Async, damit sie ausfuehrbar testbar sind.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from bewaesserung.giessfenster import (
    MODUS_AKTIV,
    naechster_erlaubter_start,
    urteil_fuer_zone,
)

#: Wie kurz vor Fensterschluss vorausgeschaut wird. Die Engine prueft alle
#: 5 min; 30 min lassen auch einen verspaeteten Zyklus (Laptop-Sleep, langer
#: Backfill) noch innerhalb des Fensters starten.
VORLAUF_MIN = 30

#: Schrittweite der Suche nach dem Fensterschluss.
SCHRITT_MIN = 15

#: Wie weit nach dem Schliessen nach der naechsten Oeffnung gesucht wird. Die
#: Wettervorhersage reicht ~48 h; darueber entscheidet ohnehin nur die Klammer.
SUCHE_OEFFNUNG_H = 36


@dataclass(frozen=True)
class FensterSchluss:
    """Das Fenster ist jetzt offen und geht bald zu."""

    zu_ab: datetime
    #: Naechster freier Start nach dem Schliessen; None = keiner gefunden.
    wieder_auf: datetime | None


@dataclass(frozen=True)
class VorgezogenerBedarf:
    """Die Zone unterschreitet ihre Schwelle, bevor das Fenster wieder aufgeht."""

    messwert: float
    prognose_feuchte: float
    wieder_auf: datetime | None
    stunden: float

    def text(self) -> str:
        """Begruendungszusatz; Zahlen wie in der Engine formatiert."""
        bis = (
            f"bis zum naechsten Fenster {self.wieder_auf:%H:%M}"
            if self.wieder_auf is not None
            else f"in {self.stunden:.0f} h"
        )
        return (
            f"vorgezogen: jetzt {_zahl(self.messwert)}%, Prognose "
            f"{self.prognose_feuchte:.1f}% {bis}"
        )


def _zahl(wert: float) -> str:
    text = f"{wert:.1f}"
    return text[:-2] if text.endswith(".0") else text


def fenster_schliesst_bald(
    zone, jetzt: datetime, wetter, *, vorlauf_min: int = VORLAUF_MIN,
) -> FensterSchluss | None:
    """Offen jetzt, zu innerhalb von `vorlauf_min`? Sonst None.

    Aus derselben Urteilsfunktion wie die Engine (`urteil_fuer_zone`) -- ein
    eigener Nachbau der Fensterlogik liefe beim naechsten neuen Kriterium
    auseinander.
    """
    gf = getattr(zone, "giessfenster_et0", None)
    if gf is None or gf.modus != MODUS_AKTIV:
        return None
    if not urteil_fuer_zone(gf, jetzt, wetter).erlaubt:
        return None
    zu_ab = None
    t = jetzt
    ende = jetzt + timedelta(minutes=vorlauf_min)
    while t < ende:
        t = min(t + timedelta(minutes=SCHRITT_MIN), ende)
        if not urteil_fuer_zone(gf, t, wetter).erlaubt:
            zu_ab = t
            break
    if zu_ab is None:
        return None
    wieder_auf, _ = naechster_erlaubter_start(
        gf, ab=zu_ab, bis=zu_ab + timedelta(hours=SUCHE_OEFFNUNG_H),
        wetter=wetter, schritt_min=SCHRITT_MIN,
    )
    return FensterSchluss(zu_ab=zu_ab, wieder_auf=wieder_auf)


def feuchte_nach_stunden(
    aktuell: float, prognose: dict[int, float], decay_pp_pro_tag: float,
    stunden: float,
) -> float:
    """Prognostizierte Feuchte nach `stunden`.

    Linear zwischen (0 h, aktuell) und den Prognose-Horizonten; hinter dem
    letzten Horizont mit `decay_pp_pro_tag` weiter. Dieselbe Lesart wie
    `Entscheidungsmotor._zeit_bis_grenze_aus_prognose`, nur in die andere
    Richtung gefragt (Wert zur Zeit statt Zeit zum Wert).
    """
    punkte = [(0.0, float(aktuell))] + sorted(
        (float(h), float(w)) for h, w in prognose.items() if w is not None
    )
    for (h0, w0), (h1, w1) in zip(punkte, punkte[1:]):
        if stunden <= h1:
            if h1 == h0:
                return w1
            return w0 + (w1 - w0) * (stunden - h0) / (h1 - h0)
    h_letzt, w_letzt = punkte[-1]
    return w_letzt - max(0.0, decay_pp_pro_tag) * (stunden - h_letzt) / 24.0


def _feuchte_bis(
    jetzt: datetime, bis: datetime, messwert: float,
    prognose: dict[int, float], decay_pp_pro_tag: float,
) -> tuple[float, float]:
    """Prognostizierte Feuchte zum Zeitpunkt `bis` und die Stunden bis dahin.

    Eine Rechnung fuer T-0578 (`bewerte_vorausschau`, Bezug Schwelle) und
    T-0622 (`wird_kritisch_bis`, Bezug kritisch).
    """
    stunden = max(0.0, (bis - jetzt).total_seconds() / 3600.0)
    return (
        feuchte_nach_stunden(messwert, prognose, decay_pp_pro_tag, stunden),
        stunden,
    )


def wird_kritisch_bis(
    *,
    jetzt: datetime,
    bis: datetime,
    messwert: float,
    kritisch: float,
    prognose: dict[int, float],
    decay_pp_pro_tag: float,
) -> bool:
    """T-0622 Regel 2: faellt die Zone bis `bis` unter `kritisch`?

    `bis` ist die naechste Fensteroeffnung (oder der Suchhorizont, wenn keine
    gefunden wurde). Dieselbe Rechnung wie `bewerte_vorausschau`, nur gegen
    die Kritisch-Grenze statt gegen die Schwelle, und ohne die Abkuerzung
    "Messwert schon darunter": eine schon kritische Zone ist hier kein Fall
    (der Hahn-Vorrang fragt nur fuer einen NICHT kritischen Frager).
    """
    wert, _ = _feuchte_bis(jetzt, bis, messwert, prognose, decay_pp_pro_tag)
    return wert < kritisch


def bewerte_vorausschau(
    *,
    schluss: FensterSchluss,
    jetzt: datetime,
    messwert: float,
    schwelle: float,
    prognose: dict[int, float],
    decay_pp_pro_tag: float,
) -> VorgezogenerBedarf | None:
    """Faellt die Zone unter `schwelle`, bevor das Fenster wieder aufgeht?

    Ohne gefundene Oeffnung gilt der Suchhorizont als Wartezeit: dann ist die
    naechste Gelegenheit mindestens so weit weg, und das spricht fuer, nicht
    gegen das Giessen jetzt.
    """
    if messwert < schwelle:
        return None   # regulaerer Bedarf, nicht vorgezogen
    bis = schluss.wieder_auf or (
        schluss.zu_ab + timedelta(hours=SUCHE_OEFFNUNG_H)
    )
    wert, stunden = _feuchte_bis(jetzt, bis, messwert, prognose, decay_pp_pro_tag)
    if wert >= schwelle:
        return None
    return VorgezogenerBedarf(
        # Ungerundet: 39,96 ist unter 40, gerundet saehe es aus wie 40.
        messwert=messwert, prognose_feuchte=wert,
        wieder_auf=schluss.wieder_auf, stunden=round(stunden, 1),
    )
