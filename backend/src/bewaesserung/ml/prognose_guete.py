"""T-0573: Traegt eine ML-Prognose ihre eigene Vertrauenswuerdigkeit.

Ausloeser (Maxibaer-Screenshot 09.09.2026): auf einer Karte standen der
Istwert 30 und daneben "24h: 66". Die 66 war korrekt gerechnet -- nur auf
einer Feature-Zeile vom 08.09. 05:55, also 25 h alt und von einem ANDEREN
physischen Sensor (FYTA Beam), weil unter derselben `plant_id` seit dem
08.09. 06:00 ein FYTA Terra steckt.

Gemessen, nicht vermutet (09.09. 07:16): der ML-Pfad steht NICHT still.
`live_vorhersage` rechnet bei jedem Poll -- aber `_filtere_ausschluss_fenster`
schneidet alles ab dem 08.09. 06:00 weg, also bleibt die juengste
verwertbare Zeile fuer immer der 08.09. 05:55. Dass `ml_vorhersage_log`
seit dem 08.09. 09:22 keine neue Zeile hat, ist die FOLGE des
`INSERT OR IGNORE` auf `feature_zeitstempel` (Dedup), kein zweiter Defekt.

Fehlerklasse `fehlerpattern_detektor_ohne_konsument`: die Pipeline weiss,
dass die Daten unbrauchbar sind (das Ausschlussfenster steht in der
Konfig), die Anzeige erfaehrt es nie. Dieses Modul ist der Konsument.

Anzeige-Gegenstueck: `frontend/src/komponenten/ml_prognose_guete.ts`. Die
TS-Datei nannte dieses Modul bisher einseitig -- wer nur hier arbeitet, sah
die Kopplung nicht. Aendert sich das Urteil oder eines seiner Felder, ist
die TS-Seite mitzufuehren.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Ab wann eine Prognose nicht mehr als aktueller Wert durchgeht --
# gemessen als RUECKSTAND auf die juengste vorliegende Messung der Zone,
# NICHT gegen die Wanduhr.
#
# Der Task schlug 3 h Wanduhr-Alter vor. Gegen die Produktionsdaten vom
# 09.09.2026 07:16 gemessen haette das 5 von 14 Zonen stumm geschaltet
# (avocado 10,3 h / fuchsie 10,1 h / kroton 4,1 h / pilea 4,1 h /
# zitrus_ii 4,3 h) -- alles Bluetooth-only-FYTA-Zonen, die per Design nur
# alle paar Stunden syncen (`ausfall_schwelle_stunden: 96`, T-0214).
# Deren Prognose ist so frisch, wie die Daten es zulassen; sie luegt
# nicht.
#
# Der Rueckstand trennt sauber: dieselbe Messung ergab fuer GENAU EINE
# Zone einen Rueckstand > 0,4 h -- mandevilla_maxi mit 25,0 h, also den
# Fall aus dem Screenshot. Das ist auch die ehrlichere Formulierung des
# Defekts: nicht "die Zahl ist alt", sondern "die Zahl ignoriert
# Messungen, die vorliegen".
PROGNOSE_MAX_RUECKSTAND_STUNDEN = 3.0

# Alter Name, damit bestehende Importe nicht brechen.
PROGNOSE_MAX_ALTER_STUNDEN = PROGNOSE_MAX_RUECKSTAND_STUNDEN

# Gruende, warum eine Prognose nicht als Zahl gezeigt werden darf.
GRUND_VERALTET = "veraltet"
GRUND_GERAETEWECHSEL = "geraetewechsel"
GRUND_HERKUNFT_UNBEKANNT = "herkunft_unbekannt"


@dataclass(frozen=True)
class PrognoseGuete:
    """Urteil ueber EINE Prognose. `gueltig=False` heisst: nicht als Zahl
    anzeigen. `grund` ist der maschinenlesbare Schluessel fuer die UI.

    Zwei getrennte Zahlen mit getrennten Aufgaben:
    - `alter_stunden`: Wanduhr-Alter der Feature-Zeile, fuer den Tooltip.
    - `rueckstand_stunden`: wie weit die Zeile hinter der juengsten
      Messung der Zone liegt. DAS ist die Entscheidungsgroesse.
    """

    gueltig: bool
    grund: str | None
    alter_stunden: float | None
    rueckstand_stunden: float | None = None


def sensor_id_zum_zeitpunkt(
    historie: list[tuple[datetime, str]],
    zeitpunkt: datetime,
) -> str | None:
    """Welche `sensor_id` (MAC) hing zum `zeitpunkt` an dieser `geraet_id`?

    `historie` ist aufsteigend nach Zeit sortiert. Liefert den juengsten
    Eintrag mit `zeitstempel <= zeitpunkt`, oder None, wenn die Historie
    nicht so weit zurueckreicht -- das ist bewusst NICHT dasselbe wie
    "kein Geraet": ein leerer String bedeutet "Pflanze ohne Geraet"
    (T-0571) und ist eine echte Aussage, None ist Unwissen.
    """
    treffer: str | None = None
    for zeit, sensor_id in historie:
        if zeit <= zeitpunkt:
            treffer = sensor_id
        else:
            break
    return treffer


def geraet_gewechselt(
    historie: list[tuple[datetime, str]] | None,
    feature_zeitstempel: datetime,
) -> bool:
    """Hat sich die physische Sensor-Identitaet seit der Feature-Zeile
    geaendert?

    Realfall Maxibaer: `fyta_900001` trug am 08.09. 05:55 noch die MAC
    mac-beam-1 (Beam), ab 06:38 die MAC mac-terra-1 (Terra
    aus der Hecke). Gleiche `geraet_id`, anderes Geraet, andere Kennlinie
    -- in den Messdaten voellig unsichtbar
    (`fehlerpattern_skalen_mix_multisensor_aggregat`).

    Bewusst konservativ: ohne Historie oder ohne Eintrag VOR der
    Feature-Zeile wird kein Wechsel behauptet. Ein unbewiesener Wechsel
    darf keine gesunde Prognose wegwerfen -- die Alterspruefung faengt
    diese Faelle ohnehin ab, sobald die Zeile stehenbleibt.
    """
    if not historie:
        return False
    vorher = sensor_id_zum_zeitpunkt(historie, feature_zeitstempel)
    if vorher is None:
        return False
    aktuell = historie[-1][1]
    return vorher != aktuell


def bewerte_prognose(
    feature_zeitstempel: datetime | None,
    geraet_id: str | None,
    jetzt: datetime,
    sensor_historie: dict[str, list[tuple[datetime, str]]] | None = None,
    letzte_messung: datetime | None = None,
    max_rueckstand_stunden: float = PROGNOSE_MAX_RUECKSTAND_STUNDEN,
) -> PrognoseGuete:
    """Darf diese Prognose als Zahl auf der Karte stehen?

    `letzte_messung` ist der Zeitstempel hinter dem Istwert, den die Karte
    ANZEIGT. Genau gegen den wird gemessen: der Widerspruch im Screenshot
    war "Istwert 30 (06:57 heute) neben 24h: 66 (Zeile von gestern
    05:55)". Fehlt der Wert, faellt es auf die Wanduhr zurueck.

    Reihenfolge der Gruende ist Absicht: der Geraetewechsel ist die
    schwerwiegendere Aussage (die Zahl gehoert zu einem anderen Sensor),
    der Rueckstand die haeufigere. Beides zugleich -> Geraetewechsel
    gewinnt, weil er auch nach einem frischen Lauf noch gilt.
    """
    if feature_zeitstempel is None:
        # Keine Herkunft -> keine Pruefung moeglich. Die Zahl trotzdem zu
        # zeigen hiesse, genau die Luecke offenzulassen, die dieser Task
        # schliesst.
        return PrognoseGuete(False, GRUND_HERKUNFT_UNBEKANNT, None, None)

    alter_stunden = (jetzt - feature_zeitstempel).total_seconds() / 3600.0
    bezug = letzte_messung if letzte_messung is not None else jetzt
    rueckstand = (bezug - feature_zeitstempel).total_seconds() / 3600.0

    if geraet_id:
        historie = (sensor_historie or {}).get(geraet_id)
        if geraet_gewechselt(historie, feature_zeitstempel):
            return PrognoseGuete(
                False, GRUND_GERAETEWECHSEL, alter_stunden, rueckstand,
            )

    if rueckstand > max_rueckstand_stunden:
        return PrognoseGuete(False, GRUND_VERALTET, alter_stunden, rueckstand)

    return PrognoseGuete(True, None, alter_stunden, rueckstand)
