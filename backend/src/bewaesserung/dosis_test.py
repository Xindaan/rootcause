"""T-0535: randomisierter Dosis-Test (blockrandomisiert, default INAKTIV).

Zweck: fuer eine Zone (aktuell bambuswald am DSWC1/Kanal 2) wird die
GESAMT-Ventilzeit der naechsten n Giessvorgaenge auf feste Stufen gesetzt
(45 / 60 / 75 min), statt die berechnete Dosis zu nehmen. Damit wird
messbar, ab welcher Dauer der Sensor aaaa0002 ueberhaupt reagiert.

Abgrenzung -- was dieses Modul NICHT tut:

- Es aendert den TRIGGER nicht. Die Engine entscheidet weiter selbst, WANN
  gegossen wird; dieses Modul ueberschreibt nur das WIE LANGE.
- Es zaehlt nicht selbst. `stufe_fuer_lauf` bekommt den Lauf-Index von
  aussen, und der kommt aus der DB (`zaehle_dosis_test_laeufe`), also aus
  den tatsaechlich gestarteten Laeufen. Eine blosse Entscheidung
  (`soll_bewaessern=True`) ist noch kein Wasser -- Blocker und
  Shadow-Modus koennen sie folgenlos machen.

Blockrandomisierung statt freier Ziehung: pro Block kommt jede Stufe genau
einmal vor, nur die Reihenfolge im Block ist gemischt. Ein Abbruch
mittendrin hinterlaesst dadurch trotzdem (fast) balancierte Gruppen.
"""

from dataclasses import dataclass
from datetime import date
from random import Random

import structlog

logger = structlog.get_logger()

# Untere Klemme fuer jede Ventildauer der Engine. Der Wert steht HIER und
# `entscheidung.MIN_DAUER_SEKUNDEN` verweist darauf -- ein Import in die
# andere Richtung waere zirkulaer (entscheidung.py laedt dieses Modul), und
# eine Kopie waere eine zweite Wahrheit.
MIN_DAUER_SEKUNDEN = 60


@dataclass(frozen=True)
class DosisTestKonfig:
    """Konfiguration des Dosis-Tests (Top-Level-Block `dosis_test:`).

    `ventil_geraet_id` + `ventil_kanal` adressieren bewusst die HARDWARE,
    nicht eine Zone: an DSWC1/K2 haengen bambuswald und
    bambuswald_yogaraum seriell am selben Ventil. Wer auf `zone_id`
    matchen wuerde, verpasst die Kanal-Entscheidung immer dann, wenn die
    jeweils andere Zone die Start-Zone ist.
    """

    aktiv: bool = False
    ventil_geraet_id: str = ""
    ventil_kanal: int | None = None
    stufen_gesamt_min: tuple[int, ...] = ()
    wiederholungen: int = 0
    seed: int = 0
    bis: date | None = None


def plan_sequenz(konfig: DosisTestKonfig) -> list[int]:
    """Blockrandomisierte Stufen-Folge, deterministisch aus `seed`.

    `wiederholungen` Bloecke; jeder Block enthaelt jede Stufe genau einmal
    in gemischter Reihenfolge. Nutzt eine EIGENE `Random`-Instanz, nicht
    das globale `random` -- sonst haengt die Sequenz davon ab, wer im
    Prozess sonst noch gewuerfelt hat.
    """
    stufen = list(konfig.stufen_gesamt_min)
    if not stufen or konfig.wiederholungen <= 0:
        return []
    wuerfel = Random(konfig.seed)
    sequenz: list[int] = []
    for _ in range(int(konfig.wiederholungen)):
        block = list(stufen)
        wuerfel.shuffle(block)
        sequenz.extend(block)
    return sequenz


def stufe_fuer_lauf(
    konfig: DosisTestKonfig, lauf_index: int, heute: date,
) -> int | None:
    """Gesamt-Minuten fuer den naechsten Lauf, oder None.

    None bedeutet: der Test greift nicht (inaktiv, Plan erschoepft, oder
    das `bis`-Datum ist ueberschritten). Der Aufrufer faellt dann auf die
    normale Dosis-Berechnung zurueck.
    """
    if not konfig.aktiv:
        return None
    if konfig.bis is not None and heute > konfig.bis:
        return None
    sequenz = plan_sequenz(konfig)
    if lauf_index < 0 or lauf_index >= len(sequenz):
        return None
    return sequenz[lauf_index]


def haupt_sekunden(
    stufe_gesamt_min: int,
    pre_soak_min: int | None,
    max_dauer_sekunden: int,
) -> int:
    """Hauptdose in Sekunden fuer eine Teststufe.

    Die Stufe ist eine GESAMT-Ventilzeit; der Pre-Soak-Puls laeuft
    zusaetzlich zur Hauptdose und wird deshalb abgezogen.

    Greift die Klemmung, ist die gefahrene Dauer nicht mehr die geplante
    Teststufe -- das macht den Messpunkt wertlos, wenn es unbemerkt
    passiert. Deshalb WARN mit beiden Werten.
    """
    vorlauf = int(pre_soak_min or 0)
    roh = (int(stufe_gesamt_min) - vorlauf) * 60
    geklemmt = max(MIN_DAUER_SEKUNDEN, min(int(max_dauer_sekunden), roh))
    if geklemmt != roh:
        logger.warning(
            "dosis_test.stufe_geklemmt",
            stufe_gesamt_min=int(stufe_gesamt_min),
            pre_soak_min=vorlauf,
            gewuenscht_s=roh,
            gefahren_s=geklemmt,
            max_dauer_sekunden=int(max_dauer_sekunden),
        )
    return geklemmt


def gilt_fuer_zone(konfig: DosisTestKonfig, zone) -> bool:
    """Haengt `zone` an dem Ventil, das der Test bespielt?

    Match auf (geraet_id, kanal), NICHT auf zone_id -- siehe Klassen-
    Docstring von `DosisTestKonfig`.
    """
    if not konfig.aktiv:
        return False
    if not konfig.ventil_geraet_id or konfig.ventil_kanal is None:
        return False
    return (
        getattr(zone, "ventil_geraet_id", None) == konfig.ventil_geraet_id
        and getattr(zone, "ventil_kanal", None) == konfig.ventil_kanal
    )


async def verbuche_lauf(
    speicher,
    konfig: DosisTestKonfig,
    zone,
    lauf_gruppe: str | None,
    zeitstempel,
    haupt_sekunden_ist: int | None = None,
) -> bool:
    """Verbucht EINEN gestarteten Testlauf (genau einmal pro Vorgang).

    Gerufen wird beim ERSTEN HAUPT-PULS, nicht beim Start der Sequenz.

    **Das war bis T-0549 anders, und die Begruendung hier war falsch.** Es
    stand: "Bricht ein Lauf vor der Hauptdose ab, ist das ein verbrauchter
    Testlauf ohne Messwert -- das ist akzeptabel." Die Messreihe hat gezeigt,
    dass es das nicht ist: zwei von 24 Laeufen endeten nach dem Pre-Soak
    (Lauf 8 am 15.08., waldblumenhain belegte den Hahn 2,5 h; Lauf 12 am
    19.08., die Hauptdose kam nicht), beide verbrauchten ihren Slot, und die
    Blockbalance stand am Ende auf 7/7/8 statt 8/8/8. Bei 24 geplanten
    Laeufen sind zwei verlorene Slots ueber 8 Prozent der Reihe.

    Der Modulkopf hatte das Prinzip schon richtig ("eine blosse Entscheidung
    ist noch kein Wasser") -- die Abgrenzung griff nur eine Stufe zu frueh.
    Ein gestarteter Pre-Soak ist ebenfalls noch nicht die Dosis, um die es
    geht.

    `haupt_sekunden_ist` ist die TATSAECHLICH kommandierte Hauptdose. Der
    Aufrufer kennt sie (er hat sie gerade gestartet und dabei auf volle
    Minuten aufgerundet); nur ohne diese Angabe wird der Sollwert aus der
    Stufe rekonstruiert. Sonst stuende in der Messreihe ein Sollwert an
    der Stelle des Istwerts (Memory:
    fehlerpattern_benachbartes_feld_als_messwert).

    Rueckgabe: True, wenn eine Zeile geschrieben wurde.
    """
    if not gilt_fuer_zone(konfig, zone):
        return False
    heute = zeitstempel.date() if hasattr(zeitstempel, "date") else zeitstempel
    try:
        # T-0549: dieselbe Sequenz darf nur einmal zaehlen. Greift, wenn ein
        # zweiter Aufrufer dazukommt oder ein Retry denselben Lauf erneut
        # meldet.
        if lauf_gruppe and await speicher.dosis_test_lauf_verbucht(lauf_gruppe):
            logger.debug(
                "dosis_test.lauf_bereits_verbucht",
                zone_id=zone.zone_id, lauf_gruppe=lauf_gruppe,
            )
            return False
        index = await speicher.zaehle_dosis_test_laeufe(
            konfig.ventil_geraet_id, int(konfig.ventil_kanal),
        )
        stufe = stufe_fuer_lauf(konfig, index, heute)
        if stufe is None:
            return False
        gefahren = (
            int(haupt_sekunden_ist)
            if haupt_sekunden_ist is not None
            else haupt_sekunden(
                stufe, zone.pre_soak_min, zone.max_dauer_sekunden,
            )
        )
        await speicher.speichere_dosis_test_lauf(
            zeitstempel=zeitstempel,
            ventil_geraet_id=konfig.ventil_geraet_id,
            ventil_kanal=int(konfig.ventil_kanal),
            stufe_gesamt_min=int(stufe),
            haupt_sekunden=gefahren,
            lauf_gruppe=lauf_gruppe,
            zone_id=zone.zone_id,
        )
    except Exception:
        logger.exception(
            "dosis_test.verbuchen_fehler",
            zone_id=getattr(zone, "zone_id", None),
            lauf_gruppe=lauf_gruppe,
        )
        return False
    logger.info(
        "dosis_test.lauf_verbucht",
        zone_id=zone.zone_id,
        stufe_gesamt_min=int(stufe),
        lauf_index=index,
        lauf_gruppe=lauf_gruppe,
    )
    return True
