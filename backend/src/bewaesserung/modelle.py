"""Gemeinsame Datenmodelle — Vertrag fuer alle Module.

Dieses Modul definiert alle Pydantic-Modelle, die zwischen den Modulen
ausgetauscht werden. Es ist die Single Source of Truth fuer Datenstrukturen
und wird VOR allen anderen Modulen implementiert.
"""

from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field, model_validator

# T-0535: der Dosis-Test bringt seine Konfig-Dataclass selbst mit.
# `dosis_test` importiert bewusst NICHT aus modelle (es arbeitet auf
# Duck-Typing gegen ZonenKonfig), deshalb ist diese Richtung zyklenfrei.
from bewaesserung.dosis_test import DosisTestKonfig


# --- Enums ---

class ZonenModus(str, Enum):
    """Betriebsmodus einer Zone."""
    AUTOMATIK = "automatik"
    MONITORING = "monitoring"


class VentilAktion(str, Enum):
    """Moegliche Ventilaktionen."""
    OEFFNEN = "oeffnen"
    SCHLIESSEN = "schliessen"
    PAUSE = "pause"
    FORTSETZEN = "fortsetzen"


class Ausloser(str, Enum):
    """Wer hat die Aktion ausgeloest."""
    AUTOMATIK = "automatik"
    MANUELL = "manuell"
    NOTFALL_STOPP = "notfall_stopp"
    WATCHDOG = "watchdog"
    # Sensor-Heuristik hat ein Ventil-Event geraten, aber der User hat es noch
    # nicht klassifiziert. Wird in der Bilanz uebersprungen + als "indikativ"
    # markiert; UI fordert Nutzer zur Klassifikation auf (T-0055-B1 + T-0055-B2).
    UNBEKANNT = "unbekannt"
    # User hat ein Heuristik-Event als Phantom klassifiziert (Regen, Sensor-
    # Glitch, sonstiger False Positive). Bleibt in der DB als Marker, damit der
    # Heuristik-Job das Event nicht regeneriert; wird von Wirkungsrate/Bilanz/
    # Budget/ML genauso ignoriert wie UNBEKANNT.
    IGNORIERT = "ignoriert"
    # T-0168: Synthetisches Event vom AquabloomJob fuer Solar-Mini-Pumpen
    # an FYTA-Topfpflanzen (kein Gardena-Ventil). Wird vom Tagesjob basierend
    # auf Konfig (Frequenz + Dauer + Tropfer-Modell) geschrieben.
    # Wirkungsrate-Median (T-0085) ignoriert AQUABLOOM (zu kleine Pulse,
    # Sensor-Antwort unter Quantisierungs-Rauschen). Response-Features
    # nehmen AQUABLOOM nur bei `delta_6h > 1.0` ins Training.
    AQUABLOOM = "aquabloom"
    # T-0453: Der Feuchte-Sprung ist ECHT, aber das Wasser kam nicht aus dem
    # Kanal dieser Zone -- typisch der Rasen-Regner einer Nachbar-Zone, dessen
    # Strahl diesen Sensor trifft (Cross-Spray, s. Memory
    # cross_spray_regner_sensor_topologie). Vorher gab es dafuer keinen Wert:
    # der User waehlte MANUELL (= "hier kam Wasser an"), womit das Ereignis
    # als eigenes Kanal-Wasser in Tagesbudget und Pause-Anker zaehlte
    # (190 Phantom-Minuten bambuswald + 85 yogaraum seit 15.06.2026).
    # IGNORIERT waere ebenfalls falsch -- das bedeutet "ist nie passiert".
    # Zaehlt daher wie UNBEKANNT/IGNORIERT NICHT als Kanal-Wasser, bleibt aber
    # als Marker stehen (Heuristik-Job regeneriert das Event dann nicht) und
    # traegt optional in `VentilEreignis.quell_zone`, WOHER das Wasser kam.
    FREMDWASSER = "fremdwasser"
    # T-0455: Lauf aus dem Gardena-Cloud-Zeitplan (App-Schedule). Das ist
    # ECHTES Kanal-Wasser und zaehlt voll in Bilanz/Budget -- der Zeitplan ist
    # der bewusste Fallback fuer den Fall, dass der Rechner aus ist. Er ist
    # aber KEIN Engine-Lauf, und genau das war vorher nicht unterscheidbar:
    # der WS-Pfad schrieb SCHEDULED_WATERING als MANUELL, der DHS-Backfill
    # dasselbe Ereignis als AUTOMATIK -- wer das Dedup-Rennen gewann, entschied
    # die Semantik. Merksatz fuer Auswertungen: `automatik` allein belegt
    # KEINEN Engine-Lauf, nur `automatik` MIT `lauf_gruppe presoak_*`.
    ZEITPLAN = "zeitplan"


# Kein-Wasser-Ausloser-Set: Heuristik-Treffer ohne bestaetigte Bewaesserung
# im eigenen Kanal. Werden in Bilanz/Budget/Wirkungsrate/ML von echten
# Ground-Truth-Ereignissen getrennt — sie sind indikativ (UNBEKANNT), Phantome
# (IGNORIERT) oder fremdes Wasser (FREMDWASSER), aber keine Zufuhr, die dieser
# Kanal geleistet hat.
KEINE_WASSER_AUSLOESER = frozenset({
    Ausloser.UNBEKANNT, Ausloser.IGNORIERT, Ausloser.FREMDWASSER,
})

# T-0455 Gegenstueck zu KEINE_WASSER_AUSLOESER: bestaetigtes Wasser AUS DEM
# KANAL DIESER ZONE, mit belastbarer Dauer.
#
# Warum als eigenes Set und nicht `not in KEINE_WASSER_AUSLOESER`: mehrere
# Konsumenten (Wirkungs-Fit, Leck-Detektor, Ops-Severity) pruefen bewusst
# POSITIV und wollen WATCHDOG/NOTFALL_STOPP/AQUABLOOM nicht mitnehmen. Bis
# T-0455 stand dort ueberall das Literal `(MANUELL, AUTOMATIK)` -- ein neu
# hinzugefuegter Echt-Wasser-Wert waere dort STILL herausgefallen (genau die
# Fehlerklasse aus fehlerpattern_detektor_ohne_konsument). Neue Echt-Wasser-
# Ausloeser gehoeren deshalb hier hinein, nicht in neun Literale.
ECHTES_KANAL_WASSER = frozenset({
    Ausloser.MANUELL, Ausloser.AUTOMATIK, Ausloser.ZEITPLAN,
})


class BlockerTyp(str, Enum):
    """Strukturierter Grund warum nicht bewaessert wird."""
    FEUCHTE_OK = "FEUCHTE_OK"
    KEINE_MESSUNG = "KEINE_MESSUNG"
    REGEN_ERWARTET = "REGEN_ERWARTET"
    ZEITFENSTER = "ZEITFENSTER"
    BUDGET_ERSCHOEPFT = "BUDGET_ERSCHOEPFT"
    PAUSE_AKTIV = "PAUSE_AKTIV"
    # T-0231 Phase 3: pruefe_kanal-Konvergenz. Schwelle ist
    # unterschritten, aber die aktive Strategie (KONSTANT_NIEDRIG /
    # SELTEN_GROSS / KORRIDOR mit Welkepunkt-Reserve) sagt
    # 'kein_bedarf' -- Dashboard und Auto-Loop sind dann konsistent
    # (vor T-0231 wuerde Auto-Loop trotzdem starten).
    STRATEGIE_KEIN_BEDARF = "STRATEGIE_KEIN_BEDARF"


class EntscheidungsScope(str, Enum):
    """Ob eine Entscheidung fuer Zone oder Kanal gilt."""
    ZONE = "zone"
    KANAL = "kanal"


class BewaesserungsStrategie(str, Enum):
    """T-0103: Pflanzen-abhaengige Bewaesserungs-Strategie pro Zone.

    Steuert Trigger-Logik in der kausalen Empfehlung:

    - `KORRIDOR` (Default): Welkepunkt-basiert (heutige Logik) plus
      sanfter `wohlfuehl_grenze`-Hinweis. Backward-kompatibel.
    - `HAEUFIG_KLEIN`: Wohlfuehl-Min als primaerer Trigger, kleine
      Hysterese. Fuer Pflanzen mit kontinuierlichem Wasserbedarf
      (Bambus-Topf, Farne, Salate).
    - `SELTEN_GROSS`: nur Welkepunkt-Reserve triggert (kein
      praeventiv/wohlfuehl_grenze), Ziel-Feuchte = Feldkapazitaet.
      Fuer Tiefwurzler (Hecken, Zitrus, etablierte Stauden).
    - `KONSTANT_NIEDRIG`: Trigger erst nahe Welkepunkt (+5pp Reserve),
      Ziel = optimum_min. Fuer Pflanzen mit Trockenstress als Feature
      (Magerstauden, Sukkulenten, Heidekraut — Konkurrenz-Unterdrueckung).
    """
    KORRIDOR = "korridor"
    HAEUFIG_KLEIN = "haeufig_klein"
    SELTEN_GROSS = "selten_gross"
    KONSTANT_NIEDRIG = "konstant_niedrig"


# --- Sensordaten ---

class DatenQuelle(str, Enum):
    """Herkunft der Sensordaten."""
    GARDENA = "gardena"
    FYTA = "fyta"


class SensorMessung(BaseModel):
    """Ein einzelner Messwert von Gardena Smart Sensor oder FYTA."""
    zeitstempel: datetime
    zone_id: str
    geraet_id: str = ""
    boden_feuchte: float | None = Field(None, ge=0, le=100, description="Prozent")
    boden_temperatur: float | None = Field(None, description="Celsius")
    umgebungs_temperatur: float | None = Field(None, description="Celsius")
    licht_intensitaet: float | None = Field(None, ge=0, description="Lux")
    batterie_prozent: float | None = Field(None, ge=0, le=100)
    # FYTA-spezifisch
    boden_fruchtbarkeit: float | None = Field(None, ge=0, description="NPK-Index")
    licht: float | None = Field(None, ge=0, description="FYTA Licht-Wert")
    quelle: DatenQuelle = DatenQuelle.GARDENA


# T-0502: Bis zu welchem Wert gilt die Annaeherung an 0 als plausibler
# Trocknungspfad? Die Gardena-Skala ist in Fuenferschritten quantisiert
# (nachgezaehlt: kein einziger Wert zwischen 0 und 5 in 16.670 Zeilen), der
# Weg nach unten ist also ... 15, 10, 5, 0. Wer zuletzt bei <= 10 stand, ist
# dort hingetrocknet; wer bei 40 stand, ist gesprungen.
# T-0502 (Korrektur 10.08.2026): Schwelle fuer "aus gesundem Bereich auf 0
# gesprungen". Abgeleitet aus 14 Null-Episoden -- Lager-Faelle liegen bei
# max24h 30..100, echtes Austrocknen bei 5..15; dazwischen kein Datenpunkt.
# 25 sitzt in dieser Luecke. Stichprobe klein (drei Sensoren), beim naechsten
# Null-Ereignis nachpruefen.
NULL_SPRUNG_MAX_24H = 25.0


# T-0428: Anzeige-Aufloesung je Datenquelle in Prozentpunkten.
# Gardena quantisiert in 5-pp-Stufen, FYTA liefert Einzelschritte.
#
# T-0544 (22.08.2026): von `leck_detektor.py` hierher gezogen. Der Detektor
# war nicht mehr der einzige Leser -- die Plateau-Erkennung in
# `kalibrierung.py` braucht dieselbe Zahl, und eine zweite Kopie waere genau
# die Fehlerklasse, die die Projektregel "keine zweite Wahrheit" meint.
# `leck_detektor` importiert sie von hier weiter.
AUFLOESUNG_PP = {"gardena": 5.0, "fyta": 1.0}
AUFLOESUNG_PP_DEFAULT = 5.0  # konservativ: im Zweifel die groebere


def ist_mehrdeutige_lokalzeit(zeitpunkt: datetime) -> bool:
    """T-0566: Faellt dieser Zeitpunkt in die Wiederholungsstunde?

    `fold=1` markiert in Python das ZWEITE Auftreten einer doppelt
    vorkommenden Lokalzeit. Mehrdeutig ist ein Zeitpunkt genau dann, wenn
    derselbe naive Wert mit `fold=0` und `fold=1` verschiedene UTC-Offsets
    hat. Am 25.10.2026 trifft das auf 02:00-03:00 lokal zu.

    Liegt hier und nicht bei einem der Parser, weil DHS und FYTA beide
    UTC liefern und beide dieselbe Falle haben -- ein Import quer zwischen
    zwei unabhaengigen Client-Modulen waere die falsche Kopplung.
    """
    naiv = zeitpunkt.replace(tzinfo=None)
    try:
        offset_a = naiv.astimezone().utcoffset()
        offset_b = naiv.replace(fold=1).astimezone().utcoffset()
    except (ValueError, OSError):
        return False
    return offset_a != offset_b


def aufloesung_pp(quelle) -> float:
    """Anzeige-Aufloesung einer Quelle; unbekannt -> die groebere Annahme."""
    return AUFLOESUNG_PP.get(str(quelle or "").lower(), AUFLOESUNG_PP_DEFAULT)


def null_ist_sensordefekt(
    zone_konf,
    quelle: DatenQuelle | str | None,
    max_24h: float | None = None,
) -> bool:
    """Ist ein Messwert von exakt 0.0 ein Defektzeichen oder ein echter Wert?

    T-0489 (Audit A3): die Antwort haengt an der QUELLE, nicht nur an der Zone.
    Ein Gardena-Sensor liefert nie 0 < x < 5 -- eine 0.0 dort heisst
    ausgesteckt, Substrat-Isolation oder Kontaktverlust (Realfall hecke
    26.05.: vier Tage konstant 0.0, Sensor lag im Lager). FYTA liefert
    dagegen echte Nullen UND echte 3er/4er.

    Nachgezaehlt 02.08. auf einer DB-Kopie: 206.315 FYTA-Zeilen mit 448
    Nullen, 265 Werten zwischen 0 und 5 und kleinstem positiven Wert 3.0;
    16.670 Gardena-Zeilen mit 2.125 Nullen und **null** Werten zwischen 0
    und 5, kleinster positiver Wert 5.0.

    Spiegelt bewusst `frontend/src/komponenten/sensor-defekt.ts`. Vorher
    verwarf das Backend eine echte FYTA-Null als "keine Messung", waehrend
    die UI sie als Messwert anzeigte -- derselbe Punkt, zwei Wahrheiten.
    Unbekannte Quelle verhaelt sich wie Gardena (fail-safe).

    Das Zonenflag `feuchte_null_ist_defekt` bleibt als Abschalter davor:
    steht es auf false, ist 0.0 fuer diese Zone nie ein Defektzeichen.

    ---

    T-0502 (Andres Rueckfrage, 04.08.): **entscheidend ist nicht der WERT,
    sondern der WEG dorthin.** Die Zahlen oben belegen die Fuenfer-
    Quantisierung der Gardena-Skala, nicht einen Defekt. Null ist die
    unterste Stufe dieser Skala und heisst "hoechstens etwa 2,5", nicht
    "kein Messwert" -- der Schritt 5 -> 0 ist derselbe wie 15 -> 10.

        Maximum der letzten 24 h = 50, dann 0  -> Sprung   -> Defekt
        Maximum der letzten 24 h = 10, dann 0  -> Abstieg  -> echter Wert

    Der Realfall, gegen den der Guard gebaut wurde, ist die Sprung-Variante:
    hecke am 26.05., Sensor im Lager, vorher gesunder Bereich. Der Realfall
    magerwiese ist die Abstiegs-Variante: 35 -> 25 -> 20 -> 15 -> 5 -> 0 ueber
    zwoelf Tage, danach Regen und der Sensor steigt wieder auf 100. Der misst.

    **KORREKTUR 10.08.2026 -- hier stand bis dahin der VORGAENGERWERT
    (`letzter positiver Wert <= 10` = Abstieg), und der trennt nichts.**
    Die Gardena-Skala quantisiert in Fuenferschritten, also kuendigt eine 5
    JEDE Null an -- beim echten Austrocknen wie beim herausgezogenen Sensor,
    der binnen Stunden durch die Stufen faellt. Nachgerechnet ueber alle 14
    Null-Episoden mit n>=20: der letzte positive Wert ist in beiden Klassen
    5.0. Das alte Kriterium stufte damit **alle fuenf hecke-Episoden als
    echten Messwert** ein -- genau den Lager-Fall, den es abfangen sollte.

    Das Fenster-MAXIMUM trennt sauber, mit einer Luecke ohne Datenpunkt:

        hecke      (Sensor im Lager)     max24h  30 / 50 / 50 / 70 / 100
        magerwiese (echtes Austrocknen)  max24h   5 / 10 / 15

    Schwelle daher `NULL_SPRUNG_MAX_24H = 25`. Volle Auswertung in
    `docs/analyse/t0502_null_trajektorie_datenlage.md`.

    `max_24h` ist das Maximum DERSELBEN Quelle im Fenster (FYTA und Gardena
    liegen auf verschiedenen Skalen, sie duerfen hier nicht gemischt werden,
    vgl. Skalen-Mix-Fehlerpattern). None heisst "kein Beleg im Fenster" --
    dann bleibt es beim fail-safe Defekt-Urteil.

    **Warum die Fehlerrichtung wichtig ist:** an einer SCHARFEN Zone bedeutet
    ein falsches "Defekt", dass bei echter Trockenheit NICHT gegossen wird.
    Das ist die Umkehrung des Fehlers, gegen den der Guard urspruenglich
    gebaut wurde, und schlimmer -- der alte Fehler kostete Wasser, dieser
    kostet die Pflanze.

    **Bewusst NICHT abgedeckt, zwei Faelle.** (a) Ein Sensor, der plausibel
    auf 0 abgesunken und dann dort defekt stehengeblieben ist, gilt weiterhin
    als gueltig -- das zu trennen braeuchte ein Trocknungsmodell pro Zone, und
    bei 0 ist die Skala ohnehin nach unten gesaettigt. (b) **Kontaktverlust in
    der Trockenphase** (waldblumenhain aaaa0001: Fingerprobe feucht, Sensor 0)
    hat max24h 15-20 und liegt damit auf der Seite des echten Austrocknens.
    Die Zeitreihe kann diese beiden Klassen nicht trennen -- beide steigen
    auch bei Wasser wieder an, die Kontrollbedingung "reagiert auf Regen"
    hilft also nicht. Das ist eine bewusste Luecke, keine Nachlaessigkeit.
    """
    if not getattr(zone_konf, "feuchte_null_ist_defekt", True):
        return False
    wert = getattr(quelle, "value", quelle)
    if str(wert or "").strip().lower() == DatenQuelle.FYTA.value:
        return False
    if max_24h is None:
        return True
    return float(max_24h) >= NULL_SPRUNG_MAX_24H


# --- Ventilereignisse ---

class VentilEreignis(BaseModel):
    """Dokumentiert eine Ventilaktion (oeffnen, schliessen, etc.)."""
    id: int | None = None   # DB-Primary-Key, optional (bei frischem Insert None)
    zeitstempel: datetime
    zone_id: str
    ventil_id: str
    aktion: VentilAktion
    dauer_sekunden: int = 0
    ausloser: Ausloser
    # Optionale Echt-Wassermenge in Liter. Wird nur bei manuellen Giessungen
    # mit unbekannter Durchflussrate gesetzt (z.B. freier Gartenschlauch
    # statt Gardena-Kanal). Bilanz bevorzugt diesen Wert, sonst wird
    # dauer_sekunden × kanal_rate × zone_anteil gerechnet.
    liter: float | None = None
    # T-0335: Pre-Soak-Lauf-Gruppierung. `lauf_gruppe` teilen alle Events eines
    # orchestrierten Pre-Soak (Puls + Haupt) -> Giess-Historie zeigt EINEN Lauf.
    # `phase` = "pre_soak" | "haupt". Beide None fuer Einzellaeufe (Auto-Loop,
    # manuell, watchdog). Wird ueber AktiveBewaesserung von OEFFNEN aufs
    # SCHLIESSEN getragen, damit das Paar konsistent gruppiert.
    lauf_gruppe: str | None = None
    phase: str | None = None
    # T-0453: Nur bei `ausloser == FREMDWASSER` gesetzt -- die zone_id, aus
    # deren Kanal das Wasser tatsaechlich kam (Cross-Spray-Quelle). Macht aus
    # "Fremdwasser" ein "Fremdwasser aus magerwiese", damit Cross-Spray-
    # Analysen eine Abfrage sind statt Forensik ueber Zeitfenster. Bei jedem
    # anderen Ausloeser None -- `Speicher.aktualisiere_ventil_ereignis`
    # erzwingt das beim Umklassifizieren (Single Source of Truth).
    quell_zone: str | None = None


# --- Wetter ---

class WetterStunde(BaseModel):
    """Wettervorhersage fuer eine einzelne Stunde."""
    zeitstempel: datetime
    temperatur: float               # Celsius
    niederschlag_mm: float = 0.0
    niederschlag_wahrscheinlichkeit: float = Field(0.0, ge=0, le=100)
    wind_kmh: float = 0.0
    wind_richtung_grad: float = 0.0  # 0=Nord, 90=Ost, 180=Sued, 270=West
    et0_mm: float = 0.0             # Referenz-Evapotranspiration
    # T-0045: Relative Luftfeuchte in % (fuer VPD/Transpirations-Feature).
    # Nullable, weil Open-Meteo das Feld zwar immer liefert, Bestandsdaten in
    # der DB aber ohne Luftfeuchte geschrieben wurden (Backfill noetig).
    luftfeuchte_prozent: float | None = None


class WetterVorhersage(BaseModel):
    """Wettervorhersage fuer die naechsten Stunden."""
    abfrage_zeitstempel: datetime
    stunden: list[WetterStunde] = []

    def zukunftsstunden(
        self, stunden: int = 6, jetzt: datetime | None = None,
    ) -> list[WetterStunde]:
        """Die naechsten N Stunden AB `jetzt`, chronologisch sortiert.

        **T-0508 (05.08.2026).** Vorher stand hier ueberall `self.stunden[:n]`
        ohne jeden Zeitfilter. Open-Meteo liefert bei `forecast_days=2` ab
        **heute 00:00 Ortszeit** -- die ersten sechs Eintraege sind also
        00:00-05:00, unabhaengig davon, wie spaet es ist. Im Abend-Giessfenster
        (18:00-21:00) las das Regen-Gate damit 13 bis 21 Stunden in die
        VERGANGENHEIT.

        Nie aufgefallen, weil `/api/wetter` korrekt auf Zukunftsstunden
        filtert: das Dashboard zeigte die richtigen Zahlen, nur die Engine
        rechnete mit den falschen.

        **`> jetzt`, nicht `>= Stundenanfang`.** Die angebrochene aktuelle
        Stunde faellt heraus. Das unterschaetzt den kommenden Regen um
        hoechstens eine Stunde -- und das ist die sichere Richtung: zu wenig
        gesehener Regen laesst giessen (billig), zu viel gesehener sperrt den
        Guss (Trockenstress, teuer; Cost-Loss aus T-0423). Die Gegenvariante
        wuerde ausserdem Regen mitzaehlen, der in den vergangenen Minuten
        dieser Stunde schon gefallen ist -- also eine kleine Neuauflage genau
        des Fehlers, der hier behoben wird.

        Gleiches Idiom wie `entscheidung.py:1233`, das immer korrekt war --
        dieselbe Datei enthielt beide Varianten nebeneinander.

        `jetzt=None` faellt auf `datetime.now()` zurueck; im Produktivpfad
        reicht jeder Aufrufer seinen eigenen `jetzt`-Wert durch, damit Tests
        nicht tageszeitabhaengig werden
        (siehe Memory `fehlerpattern_jetzt_nicht_durchgereicht`).
        """
        bezug = jetzt if jetzt is not None else datetime.now()
        return sorted(
            (s for s in self.stunden if s.zeitstempel > bezug),
            key=lambda s: s.zeitstempel,
        )[:stunden]

    def niederschlag_naechste_stunden(
        self, stunden: int = 6, jetzt: datetime | None = None,
    ) -> float:
        """Kumulierter Niederschlag in den naechsten N Stunden (mm)."""
        return sum(
            s.niederschlag_mm for s in self.zukunftsstunden(stunden, jetzt)
        )

    def et0_naechste_stunden(
        self, stunden: int = 6, jetzt: datetime | None = None,
    ) -> float:
        """Kumulierte Evapotranspiration in den naechsten N Stunden (mm)."""
        return sum(s.et0_mm for s in self.zukunftsstunden(stunden, jetzt))

    def max_regen_wahrscheinlichkeit_naechste_stunden(
        self, stunden: int = 6, jetzt: datetime | None = None,
    ) -> float:
        """T-0322: Hoechste Niederschlags-Wahrscheinlichkeit (%) in den naechsten
        N Stunden. Konvektions-/Starkregen-Signal, wenn das Open-Meteo-Modell die
        mm unterschaetzt -- Realfall 21.06.: 0-0.4mm/weather_code 80, aber P68%,
        waehrend der DWD 15-30 l/m2 + Gewitter warnte."""
        werte = [
            s.niederschlag_wahrscheinlichkeit
            for s in self.zukunftsstunden(stunden, jetzt)
        ]
        return max(werte) if werte else 0.0


# --- Wetter-Ereignisse (ML-relevant) ---

class WetterEreignisTyp(str, Enum):
    """Typ eines Wetter-Ereignisses."""
    FROST = "frost"
    HITZE = "hitze"
    STARKREGEN = "starkregen"


class WetterEreignis(BaseModel):
    """Ein erkanntes Wetter-Ereignis (z.B. Frostwarnung)."""
    zeitstempel: datetime                 # Wann erkannt
    typ: WetterEreignisTyp
    standort_id: str = ""
    details: str = ""                     # z.B. "Min -2.3°C um 04:00"
    beginn: datetime | None = None        # Prognostizierter Beginn
    ende: datetime | None = None          # Prognostiziertes Ende


class SensorWarnungTyp(str, Enum):
    """Typ einer Sensorwarnung."""
    AUSFALL = "ausfall"
    BATTERIE_NIEDRIG = "batterie_niedrig"
    BATTERIE_KRITISCH = "batterie_kritisch"
    BEWAESSERUNG_OHNE_WIRKUNG = "bewaesserung_ohne_wirkung"
    SENSOR_EINGEFROREN = "sensor_eingefroren"
    # T-0416: serverseitiger FYTA-Kalibrier-Push (Skalen-Sprung). KEIN
    # Sensor-Defekt und kein Boden-Ereignis -- ein Daten-Artefakt beim
    # Hersteller. Loest bewusst keine Giess-Reaktion aus.
    FYTA_KALIBRIER_PUSH = "fyta_kalibrier_push"
    # T-0433: eine vom Kanal-Trigger ausgeschlossene Zone meldet kritisch,
    # waehrend der Lead satt meldet -- der Ausschluss aendert also gerade
    # das Ergebnis. Bis dahin war `kanal_trigger_ausschluss` write-only:
    # ausserhalb der Trigger-Filterung las das Feld niemand, eine
    # ausgeschlossene Zone war nicht bloss nicht-triggernd, sondern
    # unsichtbar. Kein Sensor-Defekt -- ein Dissens, den ein Mensch
    # aufloesen muss.
    LEAD_DIVERGENZ = "lead_divergenz"
    # T-0445: waehrend eines laufenden Vorgangs ist fuer die stop-relevanten
    # Zonen des Kanals laenger als das Frische-Fenster kein Messwert NEU
    # EINGETROFFEN (Ankunftszeit, nicht Messzeit). Der Max-Stop urteilte in
    # dem Fenster auf Werten, die vor dem Giessen entstanden sind. Der Lauf
    # wird beendet; kein Sensor-Defekt, aber ein blindes Fenster, das ein
    # Mensch sehen muss.
    KEINE_ANKUNFT_IM_LAUF = "keine_ankunft_im_lauf"



# T-0546 (22.08.2026): Warntypen, die ein EREIGNIS melden und keinen Zustand.
#
# Alle anderen Typen beschreiben eine Lage, die endet -- der Sensor meldet
# sich wieder, die Batterie wird gewechselt, die Wirkung stellt sich ein --
# und werden dann von ihrem Detektor geschlossen. Ein Kalibrier-Push beim
# Hersteller ist dagegen ein Vorgang ohne Dauer. Auf ihn passt "behoben"
# nicht, und deshalb hat nie jemand einen Schliesser geschrieben.
#
# Folge, gefunden am 22.08.: die Warnung 516 vom 30.07. stand 23 Tage
# unveraendert auf der kasten_4-Karte und war zu diesem Zeitpunkt die
# EINZIGE offene Warnung im System. Wer die Karte ansieht, liest eine drei
# Wochen alte Lage als aktuellen Zustand.
#
# Die Loesung ist kein Schliesser, sondern eine Lebensdauer: die Meldung hat
# ihren Zweck erfuellt, sobald sie einmal gesehen wurde.
EREIGNIS_WARNUNGEN = frozenset({SensorWarnungTyp.FYTA_KALIBRIER_PUSH})

# Sieben Tage. Lang genug, dass eine Abwesenheit ueber ein Wochenende die
# Meldung nicht verschluckt; kurz genug, dass die Karte nicht monatelang
# eine alte Lage zeigt.
EREIGNIS_WARNUNG_LEBENSDAUER_TAGE = 7


class SensorWarnung(BaseModel):
    """Persistierte Sensorwarnung fuer Ops-Timeline.

    T-0526: `geraet_id` macht den Offen-Schluessel geraetescharf. Leer =
    Zonen-Warnung (der Normalfall fuer alle Typen ausser AUSFALL und
    BATTERIE_*), gefuellt = genau dieses Geraet. Ohne das Feld konnte in
    einer Zone mit mehreren Sensoren immer nur EINE Ausfall-Warnung offen
    sein, und ein zweiter toter Sensor blieb unsichtbar.
    """
    id: int | None = None
    zeitstempel: datetime
    zone_id: str
    typ: SensorWarnungTyp
    details: str = ""
    behoben_um: datetime | None = None
    geraet_id: str = ""


class FytaGeraeteStatus(BaseModel):
    """T-0527: Geraete-Momentaufnahme aus der FYTA-Customer-API.

    Die Messreihe (`/user-plant/list-measurements`) liefert nur
    soil_moisture/temperature/light/soil_fertility. Alles zum ZUSTAND des
    Geraets steht ausschliesslich am Pflanzen-Objekt und ist dort eine
    Momentaufnahme ohne Historie -- bei einem toten Geraet friert sie auf
    dem Stand des letzten Kontakts ein.

    Genau daran scheiterte die Faulbaum-Diagnose (08.08.2026): die Frage
    "wie lief der Akku VOR dem Ausfall?" war nicht beantwortbar, weil
    niemand den Wert je mitgeschrieben hatte. Diese Tabelle ist die
    Historie, die es dafuer braucht.

    `battery_level` ist ein echter, variierender Wert (ueber 15 Geraete
    gemessen: 75/80/85/90/92/95/99/100, grobe 5er-Schritte) und steht NUR
    im Detail-Endpoint `/user-plant/<id>`, nicht in der Liste.
    RSSI/Signalstaerke und Batteriespannung liefert die API nicht.
    """
    zeitstempel: datetime
    geraet_id: str                      # fyta_<plant_id>, Join zu sensor_messung
    plant_id: int
    sensor_id: str = ""                 # MAC, mehrere Pflanzen koennen sie teilen
    zone_id: str = ""
    battery_level: float | None = None
    is_battery_low: bool | None = None
    sensor_status: int | None = None    # 1 = ok, 2 = offline
    wifi_status: int | None = None
    hub_status: int | None = None
    is_outdated: bool | None = None     # FYTAs eigenes Stale-Flag
    firmware: str = ""
    last_data_received_at: datetime | None = None


# --- Entscheidung ---

class BewaesserungsEntscheidung(BaseModel):
    """Ergebnis der Entscheidungslogik fuer eine Zone."""
    zeitstempel: datetime
    zone_id: str
    soll_bewaessern: bool
    dauer_sekunden: int = 0
    begruendung: str                # Menschenlesbare Erklaerung
    naechste_pruefung: datetime | None = None
    blocker_typ: BlockerTyp | None = None
    scope: EntscheidungsScope = EntscheidungsScope.ZONE
    scope_ref: str = ""

    @model_validator(mode="after")
    def _setze_scope_ref_default(self) -> "BewaesserungsEntscheidung":
        """Leitet fuer Zonen-Entscheidungen scope_ref aus zone_id ab."""
        if not self.scope_ref and self.scope == EntscheidungsScope.ZONE:
            self.scope_ref = self.zone_id
        return self


class GiessEmpfehlung(BaseModel):
    """T-0066: Dry-Run-Empfehlung fuer das Dashboard-Panel.

    Spiegel der Blocker-Kaskade aus `pruefe_zone`, aber **ohne** DB-Writes
    und **ohne** `entscheidung_log`-Row. Liefert alle Daten, die das
    Frontend-Panel braucht: Heuristik-Dauer, ML-Dauer (falls aktiv),
    Drift-Ampel, strukturierter Grund fuer den Nicht-Giess-Fall.

    Wichtig — kein Side-Effect: Ein GET-Poll auf diesen Endpoint darf
    weder den `_pause_eingehalten`-Anker verschieben (der liest
    `entscheidung_log`), noch einen Shadow-Vorschlag persistieren
    (der koennte das Drift-MAE verzerren).
    """
    zone_id: str
    zeitstempel: datetime
    soll_bewaessern: bool
    blocker_typ: BlockerTyp | None = None
    grund: str
    feuchte_aktuell: float | None = None
    effektive_schwelle: float | None = None
    dauer_s_heuristik: int | None = None
    liter_heuristik: float | None = None
    # T-0535: Hauptdose aus dem laufenden Dosis-Test (feste Teststufe statt
    # berechneter Dosis), sonst None. Gesetzt genau dann, wenn der Auto-Loop
    # ueber `_dauer_mit_ml_weiche` ebenfalls diese Stufe faehrt -- beide
    # Pfade rufen dieselbe Funktion (`_dosis_test_stufe` +
    # `dosis_test.haupt_sekunden`), damit Anzeige und gefahrene Dauer nicht
    # auseinander laufen koennen.
    #
    # BEWUSST ein EIGENES Feld, kein Ueberschreiben von `dauer_s_heuristik`:
    # das traegt die echte Heuristik-Zahl und wird vom ML-Drift-Vergleich
    # (`_drift_ampel`), `liter_heuristik` und der MAE-Auswertung gebraucht.
    # Zwei verschiedene Groessen unter einem Namen waere
    # [[fehlerpattern_skalen_mix_multisensor_aggregat]].
    dauer_s_dosis_test: int | None = None
    # T-0089: Liter passend zur tatsaechlich angezeigten Hauptdauer
    # (`dauer_s_ml` bei mlPrimaer, sonst `dauer_s_empfehlung`, sonst
    # `dauer_s_heuristik`). Vorher zeigte das Frontend immer
    # liter_heuristik — Inkonsistenz wenn die Hauptdauer aus der kausalen
    # Empfehlung kam (z.B. 20 min × 0.36 L/min ≈ 7 L statt 4.3 L Heuristik).
    liter_haupt: float | None = None
    # T-0085: Pro-Zone-Wirkungsrate-Aufloesung — Wert + Quelle.
    # Quelle = "manuell" (Konfig-Override) | "kalibrierung" (Median
    # aus letzten Bewaesserungs-Events) | "default" (1.0 pp/min, kein
    # Wert verfuegbar) | "keine" (Empfehlung ohne Dauer-Berechnung).
    delta_pp_pro_minute_wert: float | None = None
    delta_pp_pro_minute_quelle: str = "keine"
    ml_aktiv: bool = False
    ml_wirksam: bool = False
    dauer_s_ml: int | None = None
    modell_version: str | None = None
    # T-0164: Status-Grund fuer das Frontend, warum kein ML-Vorschlag
    # da ist (oder "ok" wenn er da ist). Werte:
    #   "ok"               -> dauer_s_ml ist gesetzt
    #   "konfig_aus"       -> ml_bewaesserungs_response.aktiv=false
    #   "service_aus"      -> kein Response-Service initialisiert
    #   "modell_fehlt"     -> Modell-Datei fuer Zone fehlt/laed nicht
    #   "sensor_ueber_ziel"-> Sensor schon ueber Ziel-Schwelle
    #   "inverse_kein_wert"-> Modell-inverse returnte None (Edge-Case)
    #   "inferenz_fehler"  -> Exception in der Inferenz (geloggt)
    #   "kein_bedarf"      -> Empfehlungs-Klassifikation = kein_bedarf
    # None = altes Verhalten ohne Status (Backward-Compat).
    ml_status_grund: str | None = None
    # Drift-Ampel aus den letzten 30 Tagen — nur gesetzt wenn
    # n_bewertet >= MIN_DRIFT_SAMPLES und beide MAE-Werte vorhanden.
    drift_ampel: str | None = None            # "gruen" | "gelb" | "rot"
    drift_mae_heuristik: float | None = None
    drift_mae_ml: float | None = None
    drift_n_bewertet: int | None = None
    # T-0356: True, wenn das Tagesbudget wegen kritischer Trockenheit auf die
    # Notreserve (kritisch_faktor x Basis) angehoben wurde. Frontend zeigt
    # "Notreserve aktiv" NUR dann (im Bedarfsfall), sonst nichts. Nur im
    # BUDGET_ERSCHOEPFT-Zweig gesetzt; sonst False.
    budget_notreserve_aktiv: bool = False
    # T-0378: True, wenn die min_pause wegen kritischer Trockenheit
    # uebersprungen wurde (`ist_kritisch` UND NICHT `_kuerzlich_gegossen`).
    # Gegenstueck zu `budget_notreserve_aktiv` -- beides sind gedeckelte
    # Ausnahmen vom Normalbetrieb, und beide muessen sichtbar sein, sonst
    # widerspricht die Karte still der Engine ("Min-Pause aktiv", waehrend
    # gegossen wird). Anders als die Notreserve NICHT an einen Blocker-Zweig
    # gebunden: der Bypass fuehrt gerade dazu, dass KEIN Blocker greift ->
    # das Flag reist bis zur finalen Empfehlung mit.
    pause_bypass_kritisch_aktiv: bool = False
    # T-0075: Kausale Empfehlung — Schwellen-Bezugspunkte, Prognose,
    # Sicherheitsabstand-Erklaerung. Alle Felder optional, weil sie
    # bei fehlender Datenhistorie/ML/Wetter rausfallen koennen.
    welkepunkt_wert: float | None = None
    welkepunkt_quelle: str = "keine"
    """Quelle fuer welkepunkt_wert. "manuell" | "kalibrierung" |
    "tagesmin_schaetzung" | "feuchte_kritisch_fallback" | "keine"."""
    optimum_min: float | None = None
    optimum_max: float | None = None
    feldkapazitaet_wert: float | None = None
    feldkapazitaet_quelle: str = "keine"
    """Quelle fuer feldkapazitaet_wert. "kalibrierung" | "keine"."""
    prognose_6h: float | None = None
    prognose_12h: float | None = None
    prognose_24h: float | None = None
    prognose_quelle: str = "keine"
    """"ml" | "heuristik" | "keine"."""
    tage_bis_welkepunkt: float | None = None
    """Wie viele Tage ohne Giessen bis Sensor-Wert <= Welkepunkt."""
    tage_bis_reserve_grenze: float | None = None
    """T-0105: Tage bis zur strategie-spezifischen Reserve-Grenze.
    Bei HAEUFIG_KLEIN ist das Wohl-Min, sonst Welkepunkt. Wird im
    Frontend statt `tage_bis_welkepunkt` angezeigt, wenn vorhanden."""
    reserve_grenze_label: str = "Welkepunkt"
    """T-0105: Label fuer die Reserve-Grenze ('Welkepunkt' | 'Wohl-Min').
    Steuert die Frontend-Anzeige 'Reserve X Tage ueber <label>'."""
    dauer_s_empfehlung: int | None = None
    """Kausal hergeleitete Empfehlungs-Dauer (im Gegensatz zur reinen
    Heuristik-Dauer): so viel, dass nach `sicherheits_tage` Decay die
    Feuchte immer noch >= Welkepunkt + 5 pp Puffer ist. Geclippt auf
    [MIN_DAUER, zone.max_dauer_sekunden]. UI zeigt diese als
    Hauptzahl, dauer_s_heuristik als Vergleich (deckt nur Schwelle ab)."""
    deckung_nach_giessen_tage: float | None = None
    """Wie viele Tage Reserve schafft die empfohlene Dauer."""
    empfehlungs_typ: str = "kein_bedarf"
    """T-0103: Empfehlungs-Stufe. "akut" (jetzt unter Welkepunkt-Naehe) |
    "praeventiv" (Reserve schmilzt bald) | "wohlfuehl_grenze" (NEU
    29.04. — Sensor unter Wohlfuehl-Min, aber Welkepunkt-Reserve OK,
    sanfter Hinweis bei KORRIDOR / primaerer Trigger bei HAEUFIG_KLEIN) |
    "kein_bedarf" (im Wohlfuehlbereich)."""
    erklarung_kurz: str = ""
    erklarung_lang: str = ""
    # T-0103: aktive Strategie pro Zone (Default KORRIDOR = Backward-Compat).
    aktive_strategie: str = "korridor"
    # T-0086: Mehrfach-Takt-Empfehlung wenn Single-Dose `max_dauer_sekunden`
    # ueberschreitet. dauer_s_empfehlung enthaelt dann die Hauptdose
    # (geclippt auf max_dauer), folge_dose_dauer_s die Restdauer fuer
    # einen zweiten Takt nach `folge_dose_verzoegerung_h` Stunden.
    folge_dose_dauer_s: int | None = None
    folge_dose_verzoegerung_h: float | None = None
    folge_dose_liter: float | None = None
    # Hybrid Stufe 1: Trocknungs-Modell-Prognose (Read-Only-Diagnose,
    # parallel zu `prognose_*h` aus LightGBM/Heuristik). Quelle:
    # "gefittet" = `k_basis_pro_h` aus Tabelle `physik_k_basis`,
    # "gefittet_veraltet" = derselbe Wert, aber der Fit ist aelter als
    #   `K_BASIS_MAX_ALTER_TAGE` (T-0359): die Zahl wird weiter benutzt, die
    #   Quelle sagt, dass man sich nicht mehr auf sie stuetzen sollte,
    # "default_tau" = Fallback aus `default_tau_stunden`,
    # "keine" = Welkepunkt fehlt oder Aggregat unbekannt.
    prognose_physik_6h: float | None = None
    prognose_physik_12h: float | None = None
    prognose_physik_24h: float | None = None
    k_basis_pro_h: float | None = None
    physik_quelle: str = "keine"
    # T-0353: State-Space-Shadow-Prognose (Physik-Decay + Regen-Input +
    # Giess-Puls-Input, ml/state_space.py). Read-only-Diagnose wie die
    # Physik-Felder; Quelle = "<k_quelle>+wirkung" (Puls-Parameter
    # vorhanden) | "<k_quelle>+ohne_wirkung" (degeneriert zu Physik+
    # Regen) | "keine".
    prognose_statespace_6h: float | None = None
    prognose_statespace_12h: float | None = None
    prognose_statespace_24h: float | None = None
    statespace_quelle: str = "keine"
    # T-0351: rohe Heuristik-Decay-Rate (ET0*2-Formel), IMMER gesetzt
    # wenn berechnet — unabhaengig davon, ob die ML-Prognose gewinnt.
    # Der Audit-Job leitet daraus die Shadow-Spalte
    # `prognose_heuristik_24h` ab (Dauer-Messung Heuristik vs Physik).
    decay_heuristik_pp_pro_tag: float | None = None
    # T-0291: Transparenz fuer plateau-begrenzte Einzeldosen. Die empfohlene
    # Einzeldose ist durch das Plateau-Modell auf ~0.95*wirkung_max_pp pp
    # gedeckelt -- bei grossem Ziel-Abstand erreicht sie das Ziel NICHT in
    # einem Schritt (die flache Dauer ist kein Bug, sondern die Max-Einzeldose).
    erwarteter_endwert_pp: float | None = None
    """Feuchte nach der empfohlenen Einzeldose (aktuell + Plateau-Wirkung der Dauer)."""
    einzeldosis_max_pp: float | None = None
    """Max pp, die EINE Dose ueberhaupt heben kann (~0.95*wirkung_max_pp)."""
    dosen_bis_ziel: int | None = None
    """Anzahl Dosen (mit Versickerungs-Pause) bis zum Ziel; >1 = plateau-begrenzt."""


# --- Konfiguration (Zonenebene) ---

class ZeitFenster(BaseModel):
    """Ein bevorzugtes Zeitfenster fuer Bewaesserung."""
    von: str = Field(..., pattern=r"^\d{2}:\d{2}$")   # z.B. "05:00"
    bis: str = Field(..., pattern=r"^\d{2}:\d{2}$")    # z.B. "07:00"


class GiessfensterEt0Konfig(BaseModel):
    """T-0576 E: datengetriebenes Giessfenster, siehe `giessfenster.py`.

    `modus`:
      - "aus":      nur die bisherigen `bevorzugte_zeiten`, keine Auswertung
      - "schatten": Verhalten UNVERAENDERT (`bevorzugte_zeiten` gilt), die
                    Bedingung wird nur ausgewertet und geloggt -- zum
                    Kalibrieren der Schwellen
      - "aktiv":    `klammer` ersetzt `bevorzugte_zeiten`, die Bedingung
                    schneidet darin zu

    Default ist "schatten": wer den Block setzt, soll nicht versehentlich live
    umschalten.
    """
    modus: str = Field("schatten", pattern=r"^(aus|schatten|aktiv)$")
    klammer: list[ZeitFenster] = []
    # Pflichtfeld ohne Default: ein Default, der still vom realen Zyklus
    # abweicht, ist genau fehlerpattern_config_whitelist in der schlimmsten
    # Variante ("Default != Produktionswert").
    zyklus_min: int = Field(..., ge=1, le=24 * 60)
    trocknung_fenster_h: int = Field(6, ge=1, le=24)
    trocknung_min_mm: float | None = Field(None, ge=0)
    sonne_max_mm: float | None = Field(None, ge=0)


class BenachrichtigungsKonfig(BaseModel):
    """Konfiguration fuer Giess-Erinnerungen (Monitoring-Zonen)."""
    typ: str = "imessage"
    empfaenger: str = ""
    cooldown_stunden: int = 24


class EndpointHealthKonfig(BaseModel):
    """T-0132 (H-8): Endpoint-Health-Check fuer DHS + FYTA.

    Ein Probe-Call pro Endpoint pro Tag, Schema-Validierung. Bei
    Schema-Drift / Auth-Fehler markiert der Job den Endpoint als
    nicht-ok; der Watchdog feuert iMessage, sobald ein Endpoint
    laenger als 24 h ohne erfolgreichen Probe ist.

    Default aktiv -- Last ist minimal (1 Call/Tag/Endpoint), und der
    Schutz gegen stille Schema-Drift ist hoch.
    """
    aktiv: bool = True
    intervall_stunden: int = Field(default=24, ge=1)


class HahnWacheKonfig(BaseModel):
    """T-0537 Punkt 3: Wartezeit-Wache am geteilten Haupthahn.

    Andres Regel (13.08.2026): die teuren Zonen -- waldblumenhain (einziger
    Sprinkler), hecke und bambuswald (Mikrodrip) -- muessen "im Rahmen"
    drankommen und duerfen keinen ganzen Tag blockiert sein; ein Verzug von
    rund zwei Stunden ist ausdruecklich in Ordnung, `magerwiese` darf als
    flexibler Rasensprinkler ungestoert fertig laufen.

    Bewusst KEINE Regelmechanik: der Rasen laeuft im Handbetrieb, sein Takt
    ist nicht vorhersagbar, also wird er auch nicht modelliert. Gemessen wird
    der Schaden, den Andre benannt hat -- wartet eine der Zonen laenger als
    `schwelle_stunden` auf den Hahn, kommt eine Meldung. An den bisherigen
    Rasentagen (08.08., 12.08.) haette diese Wache nie ausgeloest: das
    Morgenfenster 04:00-08:00 liegt vor dem Rasenbeginn. Sie ist ein Netz,
    kein Regler.

    `zonen` ist explizit und wird NICHT aus `exklusiv` o. ae. abgeleitet --
    welche Pflanzen teuer sind, weiss die Konfig nicht, das weiss Andre.
    Leere Liste = Wache still aus (nichts zu ueberwachen).
    """
    aktiv: bool = False
    schwelle_stunden: float = Field(default=6.0, gt=0)
    zonen: list[str] = []


class WatchdogKonfig(BaseModel):
    """T-0126 (H-2): proaktive Push-Benachrichtigungen bei System-Anomalien.

    Adressiert Pre-Mortem-Akte 1, 2, 4: User merkt im Urlaub nichts vom
    stillen Sensor-Defekt, Husqvarna-Block oder DHS-Stille. Dieser Job
    feuert iMessage bei (a) `empfehlungs_typ='akut'` N Tage in Folge fuer
    eine Zone, (b) Husqvarna-Cadence-Block (Sensor-Stille der gesamten
    Gardena-Flotte). Throttle pro Trigger-Klasse + Zone via DB-Tabelle.

    Memory `feedback_app_check_gewohnheit.md`: Defaults konservativ,
    weil der User regelmaessig in Apps schaut -- Push ist fuer Vergesslich-
    keits- und Urlaubs-Faelle.
    """
    aktiv: bool = False                          # Opt-In
    empfaenger: str = ""                          # iMessage; leer -> IMESSAGE_EMPFAENGER env
    intervall_minuten: int = Field(default=30, ge=1)
    throttle_stunden: int = Field(default=24, ge=1)
    # Trigger A: empfehlungs_typ='akut' N Tage in Folge fuer dieselbe Zone
    akut_in_folge_tage: int = Field(default=3, ge=1)
    # Trigger B: Husqvarna-Block-Detektion via Alter der juengsten Gardena-
    # Messung. Realdaten (2026-05-04): normale Cadence ~3-8 Messungen/h
    # ueber alle Gardena-Sensoren -- das frueher genutzte "min N Messungen
    # in M Minuten"-Muster fuehrte zu Fehlalarmen, weil Husqvarna pro
    # Sensor nur ~1 Update/h sendet (nicht 30/h wie irrig in der Memory
    # `gardena_dhs_endpoint.md` notiert -- diese 30/h galt nur fuer
    # Live-WS-Hochlast, nicht im Normalbetrieb). Neuer Trigger:
    # juengste Messung aelter als `husqvarna_max_alter_minuten` -> Push.
    # 90 min ist grosszuegig genug fuer normale Stille-Phasen, eng
    # genug um echten Block (vom 23.04.2026: 67 h Stille) frueh zu fangen.
    husqvarna_max_alter_minuten: int = Field(default=90, ge=10)
    # T-0537 Punkt 3: Wartezeit-Wache am Haupthahn (Trigger H). Sitzt hier und
    # nicht als eigener Top-Level-Block, weil sie genau das ist, was dieser Job
    # tut: eine Anomalie erkennen und EINMAL pro `throttle_stunden` pushen.
    # Nested-Modell statt flacher Felder -- `WatchdogKonfig(**watchdog_roh)`
    # loest den Unterblock selbst auf, es braucht also keinen zweiten
    # Whitelist-Eintrag in `konfig.py` (Memory: fehlerpattern_config_whitelist).
    hahn_wache: HahnWacheKonfig = HahnWacheKonfig()


class FeuchteRegime(BaseModel):
    """T-0128 (H-4): Saison- oder phasen-abhaengige Feuchte-Schwellen.

    Adressiert Pre-Mortem-Akt 3 (Magerwiese verdraengt von Konkurrenz-
    Graesern, weil das System konstantes Optimum 60-75 % erzwungen hat).
    Verschiedene Pflanztypen brauchen aktive Phasen unterschiedlicher
    Feuchte:
    - Magerwiese: Anwachs hoch + Sommer-Trockenphase niedrig (gegen
      Quecke/Knaulgras) + Winter mittel.
    - Sukkulenten / Zitrus: Sommer normal + Winter-Trockenruhe.
    - Anwachs-Phasen frischer Pflanzungen: hoeher als etabliert.

    Datum-Range mit MM-DD (Jahreswechsel-Wraparound erlaubt: bis_mm_dd
    < von_mm_dd bedeutet "ueber Jahresgrenze", z. B. 10-15 -> 03-15 =
    Winter-Regime).

    Felder mit None werden vom Zone-Default uebernommen -- so muss man
    pro Regime nur die abweichenden Werte setzen.
    """
    name: str = ""                                # z.B. "anwachs", "sommer_trocken"
    von_mm_dd: str = Field(..., pattern=r"^\d{2}-\d{2}$")
    bis_mm_dd: str = Field(..., pattern=r"^\d{2}-\d{2}$")
    feuchte_schwelle_min: float | None = None
    feuchte_schwelle_max: float | None = None
    feuchte_kritisch: float | None = None
    optimum_min: float | None = None
    optimum_max: float | None = None
    welkepunkt: float | None = None               # H-4 Stufe 1b: Regime-Override
                                                  # fuer den Stress-Punkt der
                                                  # Pflanze (Magerstauden tolerieren
                                                  # niedrigeren als Bambus).
    grund: str = ""                               # Doku, warum das Regime so ist


def _datum_im_regime(regime: FeuchteRegime, jetzt: datetime) -> bool:
    """True wenn `jetzt` im MM-DD-Range des Regimes liegt.

    Wraparound: wenn `bis < von` (z.B. von=10-15, bis=03-15), gilt der
    Range ueber den Jahreswechsel. Vergleich rein mm-dd, jahresunabhaengig.
    """
    von_mm, von_tag = (int(x) for x in regime.von_mm_dd.split("-"))
    bis_mm, bis_tag = (int(x) for x in regime.bis_mm_dd.split("-"))
    von_key = von_mm * 100 + von_tag
    bis_key = bis_mm * 100 + bis_tag
    jetzt_key = jetzt.month * 100 + jetzt.day
    if von_key <= bis_key:
        return von_key <= jetzt_key <= bis_key
    # Wraparound: z.B. 10-15 .. 03-15
    return jetzt_key >= von_key or jetzt_key <= bis_key


def aktives_regime(zone: "ZonenKonfig", jetzt: datetime) -> FeuchteRegime | None:
    """Liefert das erste Regime, dessen Datum-Range `jetzt` enthaelt.

    Reihenfolge der Liste = Prioritaet (erstes Match gewinnt). None wenn
    Zone keine Regimes hat oder kein Regime aktiv ist (Konfig-Luecke ->
    Fallback auf Zone-Defaults via `effektiv_*`-Helpers).
    """
    if not zone.feuchte_regime:
        return None
    for r in zone.feuchte_regime:
        if _datum_im_regime(r, jetzt):
            return r
    return None


def effektiv_optimum_min(zone: "ZonenKonfig", jetzt: datetime) -> float | None:
    r = aktives_regime(zone, jetzt)
    if r is not None and r.optimum_min is not None:
        return r.optimum_min
    return zone.optimum_feuchte_min


def effektiv_optimum_max(zone: "ZonenKonfig", jetzt: datetime) -> float | None:
    r = aktives_regime(zone, jetzt)
    if r is not None and r.optimum_max is not None:
        return r.optimum_max
    return zone.optimum_feuchte_max


def effektiv_schwelle_min(zone: "ZonenKonfig", jetzt: datetime) -> float:
    r = aktives_regime(zone, jetzt)
    if r is not None and r.feuchte_schwelle_min is not None:
        return r.feuchte_schwelle_min
    return zone.feuchte_schwelle_min


def effektiv_schwelle_max(zone: "ZonenKonfig", jetzt: datetime) -> float:
    r = aktives_regime(zone, jetzt)
    if r is not None and r.feuchte_schwelle_max is not None:
        return r.feuchte_schwelle_max
    return zone.feuchte_schwelle_max


def effektiv_feuchte_kritisch(zone: "ZonenKonfig", jetzt: datetime) -> float:
    r = aktives_regime(zone, jetzt)
    if r is not None and r.feuchte_kritisch is not None:
        return r.feuchte_kritisch
    return zone.feuchte_kritisch


def effektiv_welkepunkt(zone: "ZonenKonfig", jetzt: datetime) -> float | None:
    """H-4 Stufe 1b: liefert NUR den Regime-Override (oder None).

    Anders als die anderen `effektiv_*`-Helper faellt diese Funktion NICHT
    auf `zone.welkepunkt` zurueck -- der Welkepunkt-Pfad in der
    Aufloesungs-Kette behandelt diese Stufen explizit (Regime > zone-
    konfig > Kalibrierungs-Median > Tagesmin-Schaetzung > feuchte_kritisch).
    """
    r = aktives_regime(zone, jetzt)
    if r is not None and r.welkepunkt is not None:
        return r.welkepunkt
    return None


class ZonenKonfig(BaseModel):
    """Konfiguration einer einzelnen Bewaesserungszone."""
    zone_id: str
    name: str
    modus: ZonenModus = ZonenModus.AUTOMATIK
    ventil_kanal: int | None = None       # Kanal am Dual Water Control (1 oder 2)
    ventil_name: str | None = None        # Name des Ventil-Kanals in Gardena-App
    # T-0203 (2026-05-17): Geraete-UUID der Smart/Dual Water Control,
    # an der die Zone haengt. Notwendig sobald mehr als eine DSWC im
    # Account ist (Andre hat 2. DSWC "Magerwiese und Hecke" am 17.05.
    # eingebunden). None = "primary"-Verhalten (= erste entdeckte
    # DSWC), Backward-Compat fuer Single-DSWC-Setups.
    # Beispiel: ventil_geraet_id: "11111111-1111-1111-1111-111111111111"
    ventil_geraet_id: str | None = None
                                          # (falls abweichend von zone.name, z.B. "Bambus" statt "Bambuswald")
    # T-0334 (2026-06-25): Pro-Strang-Opt-In fuer den autonomen Auto-Loop.
    # Das globale `ventilsteuerung_aktiv` ist der Master-Switch ("Backend darf
    # ueberhaupt schalten"); dieses Feld waehlt PRO ZONE, ob der Auto-Loop sie
    # eigenstaendig giesst. Eine Zone wird nur autonom geschaltet, wenn
    # `ventilsteuerung_aktiv=true` UND `modus=automatik` UND `auto_loop_opt_in=true`.
    # Default False = konservativ: selbst nach globalem Scharfschalten feuert
    # KEINE Zone, bis sie explizit opt-in ist (verhindert, dass z.B. waldblumen-
    # hain/hecke unbeabsichtigt mitlaufen). Shadow-Logging (pruefe_alle_zonen)
    # bleibt fuer alle automatik-Zonen unberuehrt. Manuelle Endpoints ebenfalls.
    auto_loop_opt_in: bool = False

    # T-0512 (05.08.2026): pro Zone freischalten, ob die ML-Dauer die
    # heuristische ERSETZT. Bewusst dieselbe dreistufige Mechanik wie bei der
    # Ventilsteuerung (global scharf + Zonen-Modus + Opt-in): wirksam ist die
    # ML-Dosis nur bei `bewaesserungs_response.wirksam=true` UND
    # `ml_dosis_opt_in=true`. Der globale Schalter bleibt der Not-Aus.
    #
    # Warum nicht global allein: die Shadow-Reihe (`ml_dauer_vorschlag`,
    # bewertet gegen die eingetretene Feuchteaenderung) faellt pro Zone
    # GEGENSAETZLICH aus. Stand 05.08. -- bambuswald mittlerer Fehler 42,6
    # (Heuristik) gegen 25,8 (ML), hecke dagegen 23,5 gegen 41,6. Ein
    # pauschales `true` wuerde hecke sehenden Auges verschlechtern.
    ml_dosis_opt_in: bool = False
    # T-0492 (2026-08-05): Zeitpunkt, ab dem diese Zone TATSAECHLICH autonom
    # gegossen hat. `auto_loop_opt_in` sagt nur, ob sie es HEUTE tut -- fuer
    # historische Daten braucht es das Datum, denn dieselbe Zone hat vorher
    # im Shadow gelaufen und dabei `ausloser=automatik` geloggt, OHNE dass
    # Wasser floss. Diese Shadow-Events duerfen nicht ins Response-Training
    # (sie haetten eine Dosis ohne Wirkung, also einen erfundenen Nullpunkt).
    #
    # Belegt aus der Git-Historie von `config/default.yaml`, nicht geschaetzt:
    #   2026-06-26 13:45 `aae56d6` T-0334 GO-LIVE (Bambus-Strang + Hecke)
    #   2026-07-27 15:15 `216c0f1` T-0436 (waldblumenhain)
    # Bewusst mit Uhrzeit: am 26.06. liegen Events sowohl vor als auch nach
    # dem Deploy, ein reines Datum wuerde die Vormittags-Shadow-Events
    # mitnehmen.
    #
    # None = nie autonom scharf gewesen -> `automatik`-Events dieser Zone
    # bleiben ausgeschlossen (positive Provenienzregel: im Zweifel raus).
    auto_loop_scharf_seit: datetime | None = None
    feuchte_schwelle_min: float = 35.0    # Unter diesem Wert: bewaessern
    # T-0491 (13.08.2026): darf None sein = "diese Zone hat keine
    # Nass-Obergrenze". Gesetzt fuer zitrus und mandevilla_maxi, deren
    # Warnung zu 100 % anstand und damit nichts mehr aussagte -- und zwar
    # schon vor dem FYTA-Skalenbruch, also nicht kalibrierbar.
    # **Wer diesen Typ nutzt, muss None behandeln:** `entscheidung.py`
    # ueberspringt solche Zonen im Kanal-Max-Stop, das Frontend zeichnet
    # weder Referenzlinie noch Nass-Status. In JavaScript ist `wert > null`
    # gleichbedeutend mit `wert > 0` -- ohne die Guards haetten genau die
    # Zonen dauerhaft "nass" angezeigt, die die Warnung losgeworden sind.
    feuchte_schwelle_max: float | None = 65.0  # Darueber: nicht bewaessern
    feuchte_kritisch: float = 20.0        # Sofort bewaessern (auch ausserhalb bevorzugter Zeit)
    max_dauer_sekunden: int = 1800
    min_pause_minuten: int = 120
    tages_budget_sekunden: float = 3600.0
    # T-0354: bei kritischer Trockenheit darf bis faktor x tages_budget gegossen
    # werden (echter Durst stirbt nicht am proaktiven Cap). GEDECKELT -> Runaway-
    # Schutz bleibt (stuck-low-Sensor giesst nicht endlos). 1.0 = unveraendert.
    tages_budget_kritisch_faktor: float = 1.0
    # T-0452: Schwelle der Tagesbudget-WARNUNG im manuellen Pfad, als Anteil
    # von `tages_budget_sekunden`. Bewusst ein eigener Wert: das Budget selbst
    # ist ein harter Automatik-Blocker (BlockerTyp.BUDGET_ERSCHOEPFT) und
    # deshalb absichtlich grosszuegig ueber dem legitimen Bedarf gesetzt --
    # als Runaway-Notbremse. Es dort abzusenken, damit die Warnung frueher
    # greift, wuerde das normale Giessen sperren (derselbe Fehler wurde bei
    # `hecke` mit T-0356 schon einmal zurueckgedreht). 1.0 = Warnung genau am
    # Budget, also Verhalten wie vor T-0452.
    tages_advisory_anteil: float = 1.0
    bevorzugte_zeiten: list[ZeitFenster] = []
    # T-0576 E: datengetriebene Verfeinerung des Fensters. None = aus.
    giessfenster_et0: GiessfensterEt0Konfig | None = None
    benachrichtigung: BenachrichtigungsKonfig | None = None
    # ML-Metadaten (Feature Engineering)
    ist_topf: bool = False               # Topf/Kuebel vs. Freiland
    ist_indoor: bool = False             # Innenpflanze (kein Regen/Wind)
    # Bilanz-Parameter (T-0029). `flaeche_m2` ist die Pflanz-/Nutz-Flaeche
    # fuer ET0- und Regen-Rechnung. `anteil_kanal` teilt Bewaesserung am
    # gemeinsamen Kanal auf (1.0 fuer Einzel-Zonen, sonst anteilig nach
    # Tropfer-/Sprinkler-Verteilung). Summe pro Kanal <= 1.0; Differenz geht
    # an "unbilanziert" (z.B. Bambus-Gruppe ohne Sensor).
    flaeche_m2: float | None = None
    anteil_kanal: float = 1.0
    # T-0050: Pflanzen-Optimum pro Zone (manuelle Annotation, Quelle "config").
    # Fuer FYTA-Zonen wird das Optimum primaer aus `plant_optimum`-Cache
    # gezogen (Quelle "fyta"); dieses Feld ist der Fallback/Override und
    # fuer Gardena-Zonen der primaere Weg, weil es dort keine Plant-API gibt.
    # None = unbekannt, T-0049-Vorschlag bleibt rein empirisch.
    optimum_feuchte_min: float | None = None
    optimum_feuchte_max: float | None = None
    # T-0071: Versickerungs-Karenz in Stunden pro Zone.
    # Nach einem Ground-Truth-SCHLIESSEN ignoriert die Sensor-Heuristik fuer
    # `versickerungs_karenz_stunden` jeden Feuchte-Sprung (Annahme: das Wasser
    # sickert noch, Sprung ist kein neues Ereignis). Default 3 h deckt den
    # Microdrip-Worst-Case ab. Sandboden mit Sprinkler (z. B. Waldblumenhain)
    # zeigt Sensor-Antwort erst nach 4-6 h — da ist 3 h zu kurz und erzeugt
    # Phantom-Events (reproduziert 23.04.2026: Close 12:04 + Sensor-Sprung
    # 16:54 wurde als neuer Event geloggt). Wert aus YAML kommt durchs
    # `_parse_zonen`-Whitelist (siehe fehlerpattern_config_whitelist.md).
    versickerungs_karenz_stunden: int = 3
    # T-0422: Anzahl Tropfer am Mikrodrip-Ring dieser Zone. Nur fuer die
    # FAO-56-Bilanz gebraucht -- daraus wird die BENETZTE Flaeche abgeleitet
    # (`wasserbilanz.benetzte_flaeche_m2`), die bei Tropfbewaesserung die
    # richtige mm-Bezugsgroesse ist. `flaeche_m2` ist dort die Ballenflaeche
    # und liefert physikalisch unmoegliche mm-Werte.
    # None = Sprinkler-Zone -> die Bilanz nutzt `flaeche_m2`.
    # Gezaehlt von Andre am 19.04. (Memory gardena_microdrip_hydraulik).
    tropfer_anzahl: int | None = None
    # T-0414: EIGENE, kuerzere Karenz nur fuer den T-0378-Pause-Bypass.
    # Frueher nutzte der Bypass-Guard `versickerungs_karenz_stunden` mit --
    # das machte ihn fuer Zonen mit min_pause <= 3 h (bambuswald,
    # bambuswald_yogaraum: 120 min) faktisch wirkungslos, weil der Guard
    # (ab SCHLIESSEN) die Pause (ab OEFFNEN) immer ueberlebte.
    # Die dokumentierte Begruendung des Guards ist der ~1-h-Sensor-Nachlauf
    # (Memory domain_giesswirkung_sensor_verzoegerung): "warte, bis der Sensor
    # die letzte Dose ueberhaupt sehen konnte". `versickerungs_karenz_stunden`
    # ist dagegen ein T-0071-Parameter fuer einen ANDEREN Zweck (Phantom-
    # Events der Sensor-Heuristik unterdruecken) und dreimal so breit.
    # Runaway-Schutz bleibt: nach einer langen Dose (90 min) liegt das
    # Karenz-Ende weiterhin hinter dem Pause-Ende -> kein Bypass, korrekt.
    pause_bypass_karenz_stunden: float = 1.0
    # T-0251: Detektor-Fenster-Ende fuer "Bewaesserung ohne Wirkung"
    # pro Zone in Minuten (Default global 90, leck_detektor.py).
    # Substrate mit langsamer Sensor-Antwort (Mikrodrip + dichter
    # Wurzelballen wie Bambus, grosse Sprinkler-Flaechen wie Magerwiese)
    # zeigen den Bewaesserungs-Sprung erst nach 80-120 min. Mit dem
    # Standard-90-Fenster sieht der Detektor noch kein Delta und
    # alarmiert faelschlich. Empfehlung: bambus/yogaraum/magerwiese
    # auf 180. None = globaler Default greift. Wert aus YAML kommt
    # durch `_parse_zonen`-Whitelist (siehe Memory
    # fehlerpattern_config_whitelist.md).
    detektor_fenster_ende_min: int | None = None
    # T-0540 (22.08.2026): pro Zone abschalten, ob der Leck-Detektor
    # "Bewaesserung ohne Wirkung" prueft. Betrifft NUR diese eine Pruefung --
    # `sensor_eingefroren` und `lead_divergenz` laufen weiter.
    #
    # **Wofuer das gedacht ist.** Nicht fuer "die Zone nervt", sondern fuer den
    # Fall, dass der Detektor eine Groesse erwartet, die der Sensor an seiner
    # Position bauartbedingt nicht liefern kann. Realfall bambuswald: aaaa0002
    # sitzt bewusst AUSSERHALB der Tropfer-Zwiebel (macht ihn zur Dosis-
    # Kontrolle, T-0535) und zeigt unter 40 min Laufzeit rund +1,5 pp, waehrend
    # die Alarmschwelle bei 2,5 pp liegt -- der Alarm ist damit strukturell
    # garantiert, nicht diagnostisch. 90 Meldungen fuer diese eine Zone.
    #
    # **Was der Preis ist.** Fuer die Zone entfaellt die automatische Kontrolle,
    # ob Wasser ankommt. Vor dem Setzen pruefen, ob eine ANDERE Zone am selben
    # Ventil das mit abdeckt: bei bambuswald ist das `bambuswald_yogaraum`
    # (DSWC1/K2, reagiert mit ~+17 pp). Faellt diese Deckung weg, gehoert der
    # Schalter zurueck auf true.
    #
    # Wert aus YAML kommt durch `_parse_zonen`-Whitelist (siehe Memory
    # fehlerpattern_config_whitelist.md).
    wirkungs_alarm_aktiv: bool = True
    # T-0574: Gegenstueck fuer die Pruefung "Sensor eingefroren". Gedacht fuer
    # Sensoren, deren Konstanz ein DOKUMENTIERTER Zustand ist und nicht ein
    # Defekt -- magerwiese liegt auf trockenem Sand unter der Aufloesung ihrer
    # Skala und meldet dort konstant 0,0. In genau diesem Bereich hat der
    # Detektor keine Trennschaerfe: ein toter und ein lebender Sensor sehen
    # beide gleich aus (`fehlerpattern_gardena_kontaktverlust_trockenphase`:
    # nicht der WERT 0.0 zeigt den Ausfall, die TRAJEKTORIE).
    # Zurueck auf true, sobald die Zone die Totzone verlaesst -- also wenn der
    # Sensor wieder dauerhaft ueber 0 liegt oder das Grass-Regime zurueckgebaut
    # ist (T-0379). Das ist der Ausloeser, an dem auffaellt, dass die
    # Entscheidung nicht mehr traegt.
    # Wert aus YAML kommt durch `_parse_zonen`-Whitelist (siehe Memory
    # fehlerpattern_config_whitelist.md).
    eingefroren_alarm_aktiv: bool = True
    # T-0075: Welkepunkt + Sicherheitsabstand fuer kausale Giess-Empfehlung.
    # `welkepunkt` ist der manuelle Override fuer den botanischen Stress-Punkt.
    # None = Aufloesungs-Kette aus Kalibrierungs-Median / Sensor-Tagesmin-
    # p10 / feuchte_kritisch (siehe entscheidung.py::_hole_kalibrier_referenzen).
    welkepunkt: float | None = None
    # `sicherheits_tage` = Reserve-Tage ueber Welkepunkt nach einer
    # Bewaesserung. Default 3 deckt durchschnittliches Wochenende.
    sicherheits_tage: float = 3.0
    # T-0082: Pro-Zone-ML-Modell (Cluster-Architektur).
    # `cluster_id` gruppiert Zonen mit gemeinsamem ML-Feuchte-Modell.
    # Default None = Pro-Sensor-Modell (cluster_id == zone_id).
    # Override nur wenn zwei Sensoren bewusst ein gemeinsames Modell
    # teilen sollen (z.B. zwei Sensoren am gleichen Bewaesserungs-Kanal,
    # die identisch positioniert sind). Wird durch
    # `_parse_zonen`-Whitelist durchgereicht (siehe
    # fehlerpattern_config_whitelist.md).
    cluster_id: str | None = None
    # T-0089: Pro-Zone-Wirkungsrate fuer Heuristik-Dauer-Berechnung.
    # Kalibrierter Sensor-Anstieg pro Bewaesserungs-Minute (in
    # Prozentpunkten). Default None = globaler Standardwert
    # `DEFAULT_DELTA_PP_PRO_MINUTE` aus entscheidung.py (Backward-Compat).
    # Realistische Werte (T-0067 / hydraulische Schaetzung):
    #   waldblumenhain  : 0.17  (Sprinkler-Sand, gemessen 90 min Test 27.04.)
    #   bambuswald*     : 0.30-0.50  (Mikrotropf, Schaetzung)
    #   FYTA-Topf-Zonen : irrelevant (Monitoring-Modus)
    # T-0085 wird das spaeter durch Auto-Kalibrierung aus Sensor-Daten ersetzen.
    delta_pp_pro_minute: float | None = None
    # T-0103: Bewaesserungs-Strategie pro Zone steuert die Trigger-Logik
    # in der kausalen Empfehlung. Default `KORRIDOR` = heutiges Verhalten
    # plus sanfte `wohlfuehl_grenze`-Stufe. Andere Werte aktivieren
    # spezialisierte Logik (siehe `BewaesserungsStrategie`).
    bewaesserungs_strategie: BewaesserungsStrategie = BewaesserungsStrategie.KORRIDOR
    # T-0128 (H-4): Saisonale/phasen-abhaengige Feuchte-Regimes. Leer
    # (Default) -> alte konstante Felder gelten ganzjaehrig (Backward-
    # Compat). Mit Eintraegen: das aktive Regime overrideted optimum_*,
    # feuchte_schwelle_* und feuchte_kritisch fuer den Zeitraum.
    # Adressiert Magerwiesen-/Sukkulenten-/Anwachs-Faelle, die mit
    # konstanter Soll-Feuchte falsch betrieben wuerden.
    feuchte_regime: list[FeuchteRegime] = []
    # T-0091a: log-Decay-Korrektur fuer Wirkungsrate bei langen Bewaesserungen.
    # Modelliert Tiefensickerung: rate(d) = rate0 * (1 + alpha * log(d/30)).
    # Bei alpha=0 (Default) keine Korrektur (Backward-Compat). Bei
    # alpha=-0.5 sinkt die Rate fuer 90 min auf ~45 % der 30-min-Rate.
    # Realdaten 29.04.: Bambus-Mikrodrip 90 min liefert nur 0.056 pp/min
    # persistent (vs. 0.167 bei kuerzerer Dose) -> alpha ~ -0.6.
    # Untergrenze fuer effektive Rate: 0.05 pp/min (Sanity-Cap).
    # T-0091b ueberschreibt alpha falls beide gesetzt sind.
    wirkungsrate_dauer_alpha: float = 0.0
    # T-0091b: Plateau-Modell fuer Wirkungsrate (29.04. abend).
    # log-Decay-Modell extrapoliert ohne physikalisches Plateau und
    # empfiehlt absurde Folge-Dauern (Realfall: 304 min Total fuer
    # 60 -> 75 bei Bambus). Plateau-Modell:
    #   total_wirkung(d) = wirkung_max_pp * (1 - exp(-d / tau))
    #   tau = wirkung_max_pp / wirkungsrate_initial
    # Inverse fuer Caller: d = -tau * log(1 - delta/wmax) mit Cap auf
    # delta <= 0.95 * wmax (95 %-Sicherheit, sonst d -> infinity).
    # Default None = Plateau-Modell aus, Fallback auf alpha-Modell oder
    # linear (Backward-Compat).
    wirkung_max_pp: float | None = None
    wirkungsrate_initial: float | None = None
    # T-0440: Diese Zone treibt den KANAL-MAX-STOP nicht.
    #
    # Gegenstueck zu `kanal_trigger_ausschluss`, aus demselben Grund und mit
    # umgekehrter Wirkung. Der Max-Stop rechnet auf der NASSESTEN Zone; sitzt
    # deren Sensor in der benetzten Zwiebel am Tropfer, beendet er den Lauf,
    # sobald die ZWIEBEL voll ist -- nicht wenn die Zone versorgt ist.
    #
    # Realfall an einer Mikrodrip-Geschwisterzone: die Regen-Kontrolle zeigt
    # 0,90x bei flaechigem Wasser gegen 2,65x beim Guss, also Zwiebel-Signatur.
    # Der Stop feuert bei `feuchte_schwelle_max + 10` = 85 nach im Median
    # 66 min (n=13) und schneidet damit einen 90-min-Lauf, den die
    # Geschwisterzone am selben Ventil braucht: deren Dosis-Antwort ist
    # monoton in der DURCHGEHENDEN Dauer (+5,9/+7,9/+11,5/+13,8 pp bei
    # 30/45/60/90 min, isoliert gemessen) und erreicht erst bei 90 min in
    # 100 % der Faelle die eigene Schwelle.
    #
    # Beide Zonen haben dieselbe Hardware (Ring, 30 cm Teilung, ~8 Tropfer),
    # dieselbe Art, denselben Boden, dasselbe Ventil. Es gibt keinen Grund,
    # warum die eine weniger Wasser braucht -- verschieden ist nur, WO die
    # Sensoren stecken. Darum wird hier nicht zwischen Beduerfnissen
    # geschlichtet, sondern der ungeeignete Sensor aus der Stop-Rolle genommen.
    kanal_max_stop_ausschluss: bool = False
    # T-0444: harte Notbremse fuer eine `kanal_max_stop_ausschluss`-Zone.
    # T-0440 hat die Bremse fuer sie ersatzlos entfernt -- der Zwiebelsensor
    # soll nicht mehr jeden Lauf kappen, aber der Kanal darf auch nicht ganz
    # ohne obere Grenze laufen. Die Notbremse greift auf deutlich hoeherem
    # Niveau als `feuchte_schwelle_max + 10`.
    #
    # Warum unter 100: bei 100 ist das Skalenende erreicht (Gardena liefert
    # einen Relativ-Index, s. Memory gardena_sensor_skala) -- ab dort ist
    # echte Uebersaettigung nicht mehr von "nass" unterscheidbar. Eine Bremse
    # bei 100 waere also keine.
    kanal_max_stop_notbremse_pp: float = 95.0
    # T-0434: gilt exakt 0.0 in dieser Zone als Defekt statt als Messwert?
    #
    # Der Gardena-Bodensensor faellt bei Kontaktverlust in der TROCKENphase
    # auf exakt 0 und bleibt dort (Memory
    # fehlerpattern_gardena_kontaktverlust_trockenphase) -- also genau dann,
    # wenn die Automatik giessen will. 0.0 < `feuchte_kritisch` heisst
    # "kritisch trocken", und kritisch umgeht `bevorzugte_zeiten`: der
    # Ausfall loest damit Vollgas zu jeder Tageszeit aus.
    #
    # Warum exakt 0.0 und nicht eine Bandbreite: die Gardena-Sensoren haben
    # in der ganzen Historie NIE einen Wert zwischen 0 und 5 geliefert
    # (Stand 29.07.: 7871 Messungen der drei Aussensensoren seit Mai, der
    # kleinste Wert oberhalb von 0 ist ueberall exakt 5.0). Es gibt keinen
    # graduellen Uebergang nach unten -- 0 ist kein niedriger Messwert,
    # sondern der Aus-Zustand.
    #
    # Warum trotzdem pro Zone schaltbar: fuer FYTA-Sensoren gilt das NICHT.
    # Sie liefern einen kontinuierlichen Uebergang (belegte Reihe 09.07.:
    # 0 -> 3 -> 4 -> 5 -> 6), dort kann 0.0 ein echter Randwert sein. Alle
    # vier scharfen Zonen lesen zwar Gardena (bambuswald, yogaraum, sowie
    # hecke + waldblumenhain ueber ihren `aggregat_lead_geraet`), aber eine
    # reine FYTA-Zone soll den Guard abschalten koennen.
    feuchte_null_ist_defekt: bool = True
    # T-0439: wieviel Feuchte (pp) bringt 1 mm Regen IN DIESER ZONE?
    # Interzeption (dichtes Blattdach nimmt weg) und Kronentraufe (Baumkrone
    # konzentriert) verschieben das je Zone. None = globale Engine-Konstante
    # REGEN_FEUCHTE_FAKTOR (4.0), also unveraendertes Verhalten.
    #
    # ACHTUNG, bevor hier jemand Zahlen eintraegt: eine Messung ueber 21
    # Regenereignisse (27.07.) ergab Mediane von 0,0 bis 3,6 pp/mm bei
    # Einzelwerten von -20 bis +80 -- und alle Werte sind Vielfache von 5
    # (Gardena-Quantisierung). Die Mediane liegen damit AUF der
    # Aufloesungsgrenze, die Streuung ist groesser als der Effekt. Aus dieser
    # Datenlage laesst sich kein Zonen-Faktor kalibrieren; die Einzel-Nacht-
    # Zahlen aus dem T-0439-Anlass (waldblumenhain 4,9) sind nicht
    # repraesentativ (Median dort 0,44). Erst mit laengerer Bright-Sky-Reihe
    # fitten (T-0425), s. `fehlerpattern_detektor_unter_sensoraufloesung`.
    regen_faktor_pp_pro_mm: float | None = None
    # T-0111: Pre-Soak-Sequenz-Defaults pro Zone fuer das Dashboard.
    # `pre_soak_min` = empfohlene Vorwaesserungs-Dauer (z.B. 5 min).
    # `pre_soak_pause_min` = Wartezeit zwischen Pre-Soak und Hauptdose
    #                       (z.B. 30 min, damit Wasser einsickert).
    # Beide None = kein Pre-Soak-Vorschlag im Frontend.
    pre_soak_min: int | None = None
    pre_soak_pause_min: int = 30
    # T-0336: Pre-Soak-Policy fuer den Auto-Loop. `nie` = Einzellauf (Default,
    # Backward-Compat). `immer` = der Auto-Loop giesst diese Zone als Pre-Soak
    # (Puls + Pause + Hauptdose), nicht als Einzellauf. Greift nur zusammen mit
    # ventilsteuerung_aktiv + auto_loop_opt_in. Sprinkler-Zonen brauchen das
    # (Einzellauf benetzt nur die Oberflaeche, T-0325).
    pre_soak_modus: str = "nie"
    # T-0437: Cycle-and-Soak. Die Hauptdose wird in `haupt_pulse` gleich lange
    # Pulse AUFGETEILT (nicht vervielfacht -- `haupt_s` bleibt die
    # Gesamt-Wassermenge), zwischen den Pulsen liegt `haupt_puls_pause_min`
    # Einsickerzeit. Default 1/0 = exakt das bisherige Verhalten
    # (ein Hauptlauf am Stueck), damit Bestandszonen unveraendert bleiben.
    # Hintergrund: auf Sandboden mit Sprinkler laeuft eine lange Dose am
    # Stueck oberflaechlich ab; Andres Realguss 25.07. am waldblumenhain war
    # 5 min + 3x30 min mit ~21 min Pausen und hob den Sensor 25 -> 75.
    haupt_pulse: int = 1
    haupt_puls_pause_min: int = 0
    # T-0151: Hahn-Cluster fuer Durchfluss-Budget. Mehrere Zonen am
    # gleichen Wasserhahn teilen sich den verfuegbaren Druck/Durchfluss.
    # Bei parallelem Lauf summieren sich die `verbrauch_lpm`-Werte; die
    # Summe darf `HahnCluster.max_durchfluss_lpm` nicht ueberschreiten.
    # Druckkompensierte Mikrodrip-Strecken duerfen typisch parallel zu
    # anderen Mikrodrip-Strecken laufen, druckabhaengige Sprinkler
    # (hoeherer Verbrauch) blockieren oft das gesamte Budget.
    # `hahn_cluster=None` und `verbrauch_lpm=None` (Default) = Lock-Check
    # uebersprungen (Backward-Compat fuer FYTA-Topf-Zonen + Setups ohne
    # Hahn-Konflikt). Whitelist im Konfig-Loader.
    hahn_cluster: str | None = None
    verbrauch_lpm: float | None = None
    # T-0153: Druck-Exklusivitaet — druckabhaengige Zonen (Sprinkler,
    # Viereckregner) sollten NICHT parallel zu anderen Cluster-Zonen
    # laufen. Bei `exklusiv=True` blockt der Lock jeden Mitstart, sobald
    # diese Zone aktiv ist (oder umgekehrt: diese Zone darf nicht
    # starten wenn schon irgendwas im Cluster laeuft) — unabhaengig vom
    # Volumen-Budget. Modelliert den Druck-Konflikt explizit: Sprinkler
    # bei reduziertem Druck verlieren Reichweite (Topologie-Aenderung,
    # nicht durch Volumen-Buchhaltung abbildbar). Default False =
    # heutiges Verhalten.
    exklusiv: bool = False
    # T-0168: AquaBloom-Auto-Logging fuer Solar-Mini-Pumpen an FYTA-
    # Topfpflanzen (kein Gardena-Ventil). Tagesjob `aquabloom_job.py`
    # schreibt synthetische `ventil_ereignis`-Eintraege als Event-Paar
    # OEFFNEN(0) + SCHLIESSEN(dauer) pro echtem Pumpen-Puls. Liter-
    # Berechnung physikalisch ueber Tropfer-Anzahl × Spec.
    # Alle Felder None-default = AquaBloom inaktiv fuer diese Zone.
    aquabloom_pumpen_dauer_sekunden: int | None = None
    aquabloom_pumpen_intervall_stunden: float | None = None
    aquabloom_anker_zeitstempel: datetime | None = None
    aquabloom_tropfer_anzahl: int | None = None
    aquabloom_tropfer_liter_pro_stunde: float | None = None
    aquabloom_aktiv_ab: str | None = None       # "MM-DD"
    aquabloom_aktiv_bis: str | None = None      # "MM-DD"
    # T-0169: Logging-Einheit fuer den manuellen "Gegossen"-Button im
    # Frontend. Default "sekunden" = bisheriges Verhalten. Mit "ml"
    # zeigt das Frontend Liter-Buttons (z.B. 100/250/500/1000 ml),
    # Backend rechnet Pseudo-Dauer fuer ML-Features ueber
    # `bilanz.manuell_liter_pro_minute`. Default-Optionen [100, 250,
    # 500, 1000] werden vom Frontend genutzt, wenn die Konfig leer ist.
    logging_einheit: str = "sekunden"           # "sekunden" | "ml"
    logging_optionen_ml: list[int] = []
    # T-0187: Sensor-Heuristik pro Zone konfigurierbar.
    # `heuristik_min_delta_pp` ueberschreibt die globale Beat-zu-Beat-
    # Schwelle (Default MIN_DELTA_PROZENT=5.0 aus sensor_backfill.py).
    # FYTA-Indoor-Toepfe mit AquaBloom-Tropfer sehen pro 15-min-Beat
    # nur 2-3 pp Anstieg (Wasser sickert lokal langsam, Sensor sieht
    # nur die unmittelbare Umgebung) -> globale 5 pp verpasst echte
    # Pulse. Realfall 15.05.: zitrus 72->75->78 (3 pp/Beat), mandevilla
    # 41->50 in 2.5 h (1-2 pp/Beat) -> kein Event geschrieben.
    # None = globale Schwelle gilt (Backward-Compat fuer Gardena-Zonen).
    heuristik_min_delta_pp: float | None = None
    # Roll-Up-Check: kumulative Erhoehung ueber Multi-Beat-Fenster.
    # Erkennt langsame Sicker-Cadence (Mandevilla-Pattern: 1-2 pp/Beat
    # ueber Stunden, kein einzelner Beat schlaegt durch die Schwelle).
    # Beide Felder None oder 0 = Roll-Up aus (Backward-Compat).
    # Bei aktivem Roll-Up wird zusaetzlich zur Beat-Logik ein 60-min-
    # Differenz-Check ausgefuehrt: messung(t) - messung(t - fenster)
    # >= schwelle_pp -> Event-Kandidat. Dedup ueber bestehendes 30-min-
    # Fenster verhindert Doppel-Erkennung (Beat schlaegt zuerst, Roll-
    # Up findet im Dedup-Fenster denselben Sprung und schweigt).
    heuristik_rollup_fenster_min: int | None = None
    heuristik_rollup_schwelle_pp: float | None = None
    # T-0312: Cross-Spray-Quell-Zonen. Liste von zone_ids, deren (Viereck-
    # regner-)Lauf den Sensor DIESER Zone physisch treffen kann (Strahl-
    # Ueberlappung). Beispiel: hecke -> ["magerwiese"], weil die
    # Strapazierrasen-Beregnung ueber den Magerwiesenkanal die Hecke-Sensoren
    # benetzt. sensor_backfill wertet einen Feuchte-Sprung dann NICHT als
    # eigene (Microdrip-)Bewaesserung, wenn eine dieser Quell-Zonen im
    # Fenster+Karenz lief und KEIN eigener Kanal-Lauf vorliegt -> Cross-Spray.
    # Default [] = kein Cross-Spray moeglich (Backward-Compat). Generisch:
    # gilt fuer jede Zone, deren Sensor von einem Nachbar-Sprinkler getroffen wird.
    cross_spray_quell_zonen: list[str] = []
    # T-0332: Aggregat-Lead. Wenn gesetzt (geraet_id), nutzt die Zone fuer
    # die Entscheidung NUR diesen Sensor statt des Median ueber alle Zonen-
    # Sensoren. Zweck: waehrend des Cross-Spray-Regimes (s. cross_spray_
    # quell_zonen) saeuft die FYTA-Seite -> der 2:1-FYTA-Median ueber-liest
    # die echte Hecke-Feuchte. Lead auf den Gardena-Sensor entkoppelt die
    # Entscheidung. Reversibel: Feld entfernen -> zurueck zum Median.
    # None = Median-Verhalten (Backward-Compat).
    aggregat_lead_geraet: str | None = None
    # T-0337: Kanal-Trigger-Ausschluss. Bei geteiltem Kanal (mehrere Zonen,
    # ein Ventil) treibt `pruefe_kanal` heute den Min-Trigger nach der
    # trockensten Zone. Ist diese Zone ein unzuverlaessiger Sensor (z.B.
    # bambuswald/sensor-b: hydrophob-niedrig wenn trocken + Lag nach dem Guss),
    # ueber-waessert das Min am Artefakt. True = die Zone treibt den Trigger
    # NICHT (bleibt aber im informativen Durchschnitt + Logging). Der
    # verlaessliche Strang-Partner (yogaraum) fuehrt. False = Default (alle
    # Zonen treiben, = heutiges Min).
    kanal_trigger_ausschluss: bool = False
    # T-0214: Pro-Zone-Schwelle fuer SENSOR_AUSFALL-Warnung. None =
    # globaler Default (3h Gardena, 12h FYTA). FYTA-Indoor-Pflanzen
    # ohne Beam-Hub-Reichweite (mandevilla_maxi, pilea) brauchen
    # groessere Schwellen wie 96h, damit die Bluetooth-Pull-Cadence
    # alle 2-4 Tage nicht dauerhaft 'ausfall' triggert.
    ausfall_schwelle_stunden: int | None = None
    # T-0484 Teil 2: Erreicht Regen den Wurzelballen dieser Zone?
    # Die Kalibrierung leitet die FELDKAPAZITAET daraus ab, dass der Boden
    # 12-24 h nach durchdringendem Regen gesaettigt ist. Fuer einen
    # ueberdachten Balkontopf ist diese Annahme frei erfunden -- dort kam
    # kein Wasser an, und der Sensorwert zum Regenzeitpunkt ist einfach der
    # Trockenwert. Default True = Freiland (bisheriges Verhalten aller
    # Ventil-Zonen). Fuer Topf-Zonen bewusst explizit setzen; NICHT aus
    # `ventil_kanal` ableiten, das war der T-0482/T-0484-Fehler.
    regen_erreicht_ballen: bool = True
    # T-0181: Liste der Sensor-Quellen die als "Calibration-Pair"
    # gegen Gardena gefittet werden sollen. Voraussetzung: die
    # Sensoren sind physisch direkt neben dem Gardena-Sensor platziert
    # (gleiche Bodenschicht, ~30-50 cm Abstand), sodass Differenzen
    # reine Skalen-/Sensor-Effekte sind, keine Substrat-Heterogenitaet.
    # Default []: keine Fit-Teilnahme. Beispiel waldblumenhain:
    # `calibration_pair_quellen: ["fyta"]` (FYTA-A neben Gardena im
    # A-D-E-Schnittpunkt, siehe Memory
    # `arbeitspattern_sensor_platzierung.md`).
    calibration_pair_quellen: list[str] = []
    # Hybrid Stufe 1: ZonenKonfig-Override fuer die Trocknungs-Konstante
    # des Physik-Moduls (`physik_trocknung.py`). Wenn `None`, wird der
    # gefittete Wert aus Tabelle `physik_k_basis` benutzt; existiert auch
    # der nicht, faellt das Modul auf `default_tau_stunden` zurueck.
    # Einheit 1/h, typische Werte 0.005-0.05 (tau = 20-200 h).
    k_basis_pro_h: float | None = None
    # T-0279 Phase 2: proaktiver Bewaesserungs-Trigger fuer SELTEN_GROSS
    # (+ KORRIDOR). Wenn gesetzt, schlaegt die Engine `praeventiv` vor,
    # sobald die PHYSIK-Reserve bis zur Komfort-Unterkante (optimum_min)
    # <= diesem Wert (Tage) faellt -- nicht erst beim 1.5d-Akut-Rand.
    # Bezugslinie ist bewusst optimum_min, NICHT der Welkepunkt: der
    # Welkepunkt ist die Decay-Asymptote, "Tage bis Welkepunkt" ist als
    # Trigger-Metrik unbrauchbar (exakt asymptotisch -> quasi unendlich,
    # linear -> konstant wegen exp-Selbstaehnlichkeit). Erst optimum_min
    # liefert ein feuchte-abhaengiges Signal (Metrik-Redesign 31.05.).
    # Fuer feucht-liebende Zonen (waldblumen Hortensien/Farne), die den
    # Tiefenlauf rechtzeitig + im bevorzugten Morgen-Fenster brauchen.
    # None = altes Verhalten (nur akut bei <= 1.5d). Quelle ist die
    # Physik-Prognose (stabil), nicht die ML-24h-Prognose (T-0278).
    proaktiv_tage_vor_optimum_min: float | None = None
    # T-0279 Phase 2: bei Multi-Sensor-Zonen den proaktiven Trigger auf
    # die TROCKENSTE gemappte Sensor-Lesung stuetzen (statt Median).
    # Sensor-Review waldblumen 30.05.: der Median verwaessert das
    # konservative Gardena-Warnsignal (Gardena 45 vs FYTA-Paar 53/54
    # -> Median 46.5). Fuer feucht-liebende Zonen, die dem trockensten
    # Sensor folgen sollen. Default False = Median-Pfad. Betrifft NUR
    # den proaktiven Trigger; normale Entscheidung + Akut-Schutz nutzen
    # weiter das Median-Aggregat.
    proaktiv_min_sensor: bool = False


def ist_auto_loop_zone(zone: "ZonenKonfig") -> bool:
    """T-0370: Zonen-seitiger Teil der 3-stufigen Scharfschaltung (T-0334):
    modus=AUTOMATIK + ventil_kanal gesetzt + auto_loop_opt_in. Single Source
    fuer _baue_auto_loop_kanaele (main.py) UND das API-Feld `autonom_scharf`
    (api_server._baue_zone_dict) -- vorher hielt nur der Loop-Filter diese
    Wahrheit, das Frontend konnte scharf vs. Shadow nicht unterscheiden.
    Der globale Master-Switch `ventilsteuerung_aktiv` kommt beim Aufrufer
    dazu (AppKonfig-Ebene, nicht Zonen-Ebene)."""
    return (zone.modus == ZonenModus.AUTOMATIK
            and zone.ventil_kanal is not None
            and zone.auto_loop_opt_in)


class WetterStandortKonfig(BaseModel):
    """Wetter-Konfiguration fuer einen einzelnen Standort."""
    id: str                          # z.B. "garten", "balkon"
    breite: float
    laenge: float


class WetterKonfig(BaseModel):
    """Konfiguration fuer die Wettervorhersage (Multi-Standort)."""
    standorte: list[WetterStandortKonfig] = []
    cache_minuten: int = 30
    regen_schwelle_mm: float = 2.0
    # T-0322: Konvektions-/Starkregen-Guard. Blockiert einen geplanten Lauf, wenn
    # die MAX-Niederschlagswahrscheinlichkeit der naechsten 6h >= Schwelle liegt --
    # auch wenn das Modell wenig mm zeigt (open-meteo unterschaetzt Konvektion).
    # Konservativer Default 80 (= sehr wahrscheinlicher Regen, kein Unter-Giess-
    # Risiko durch dry-P-Spikes). Niedriger = faengt mehr, aber mehr Fehl-Skips.
    # 100 (oder >100) = Guard aus.
    regen_wahrscheinlichkeit_schwelle_prozent: float = 80.0

    # --- T-0439: Regen-Gate wirkungsbasiert statt reines mm ---
    # Das alte Gate war `niederschlag_6h >= regen_schwelle_mm` -- ein Schalter,
    # der weder die Zone noch ihr Defizit kennt. Realfall 27.07.: bambuswald
    # bei 20 % (kritisch 45) wurde von 4,5 mm Prognose gesperrt, obwohl unter
    # dichtem Blattdach je Regenereignis nur wenige pp ankommen. Cost-Loss
    # (T-0423): ein ausgefallener Guss ist teuer, ein ueberfluessiger billig.
    # `wirkung`: sperrt nur, wenn der ERWARTETE Feuchte-Effekt die Zone ueber
    #            ihre effektive Schwelle heben wuerde.
    # `mm`:      Alt-Verhalten, ein Wort zum Zurueckdrehen.
    regen_gate_modus: str = "wirkung"
    # Hard-Stop unabhaengig vom Zonen-Faktor: ab dieser 6h-Menge wird IMMER
    # gesperrt. Schuetzt davor, dass ein zu klein geratener Zonen-Faktor in
    # einen Landregen hinein giessen laesst. 0 = aus.
    regen_gate_immer_ab_mm: float = 10.0

    # --- T-0509: welches Archiv-Produkt ist unsere Ground-Truth? ---
    # Bis 05.08.2026 stand hier nichts, und `wetter_archiv.py` rief die
    # Archive-API OHNE `models`. Der Default ist `best_match`, an diesem
    # Standort `ecmwf_ifs` -- nicht ERA5, wie 12 Stellen behaupteten (T-0507).
    #
    # Gegen die eigenen Bodensensoren geprueft (86 Tage, 65 auswertbare
    # Zone-Tage ohne Ventil-Ereignis): `ecmwf_ifs` sagt in 40 % der Faelle
    # Regen an, den der Boden nie sieht; `era5` in 24,6 %, die DWD-Station in
    # 21,5 %. Der Fehler ist einseitig -- "Regen angesagt, Boden bleibt
    # trocken" kommt 26x vor, der umgekehrte Fall genau 1x.
    #
    # `era5` und nicht die Station, obwohl die minimal besser trifft:
    # era5 liegt auf UNSEREN Koordinaten statt 12 km entfernt, ist
    # lueckenlos und ein Ein-Parameter-Wechsel an derselben API. Die Station
    # laeuft seit T-0505 ohnehin als unabhaengige Gegenprobe mit
    # (`wetter_messung_station`) -- damit haben wir beide, ohne die Bilanz
    # an eine Punktmessung mit Luecken zu haengen.
    #
    # Leerstring = Archive-Default (also zurueck auf `ecmwf_ifs`).
    # ACHTUNG beim Umstellen: die Tabelle darf keine zwei Produkte mischen.
    # Wer den Wert aendert, faehrt danach den Backfill ueber die volle
    # Historie (`python -m bewaesserung.wetter_archiv --von ... --bis ...`).
    archiv_modell: str = "era5"

    # Rueckwaertskompatibilitaet: breite/laenge direkt (alter Single-Standort)
    breite: float = 0.0
    laenge: float = 0.0


class SpeicherKonfig(BaseModel):
    """Konfiguration fuer die Datenbank."""
    db_pfad: str = "./daten/bewaesserung.db"


class GardenaKonfig(BaseModel):
    """Konfiguration fuer die Gardena API."""
    client_id: str
    client_secret: str = ""


# --- Standort-Gruppierung ---

class StandortKonfig(BaseModel):
    """Gruppierung von Zonen nach physischem Standort."""
    standort_id: str
    name: str
    wetter_standort: str = ""        # Referenz auf WetterStandortKonfig.id
    zonen: list[str] = []


def wetterstandort_je_zone(standorte) -> dict[str, str]:
    """zone_id -> wetter_standort, die eine Wahrheit aus `konfig.standorte[]`.

    T-0488 (Audit A1): `wetter_standort` sitzt auf StandortKonfig, NICHT auf
    ZonenKonfig. Ein `getattr(zone, "wetter_standort")` liefert deshalb immer
    None und faellt still auf den ERSTEN Standort zurueck -- im
    Wasserbilanz-Job hiess das, dass die drei Berliner Balkonzonen mit
    Bilanzflaeche mit dem Wetter des Gartenstandorts
    gerechnet wurden. Gemessen am 02.08.: alle acht Bilanzzeilen trugen
    et0=1,8 / regen=7,0, waehrend die neueste Berliner Stundenprognose fuer
    denselben Tag auf 52,7 mm kam.

    Fallback auf `standort_id`, wenn kein `wetter_standort` gesetzt ist --
    so machen es Entscheidung, API und ML bereits.
    """
    mapping: dict[str, str] = {}
    for standort in standorte or []:
        ziel = standort.wetter_standort or standort.standort_id
        for zone_id in standort.zonen:
            mapping[zone_id] = ziel
    return mapping


class BalkonKonfig(BaseModel):
    """Metadaten fuer einen Balkon (ML-relevant)."""
    himmelsrichtung: str             # "sued", "nord"
    regen_wind: list[str] = []       # Windrichtungen bei denen Regen ankommt
    sonnig: bool = True


# --- FYTA ---

class FytaPflanzenKonfig(BaseModel):
    """Zuordnung einer FYTA-Pflanze zu einer Zone."""
    fyta_id: int
    zone_id: str
    name: str


class FytaKonfig(BaseModel):
    """Konfiguration fuer die FYTA-API."""
    api_url: str = "https://web.fyta.de/api"
    poll_intervall_sekunden: int = 900  # 15 Min
    pflanzen: list[FytaPflanzenKonfig] = []


# T-0234: MicrodripKonfig entfernt (toter Konfig-Vertrag).
# Use-Case (synthetische Bewaesserungs-Events fuer nicht-smarte Pumpen)
# wird seit T-0168 vom AquabloomJob abgedeckt. Es gab nie Konsumenten der
# Konfig im Code -- nur Parser + Speicher-Feld, beide jetzt weg.


class MlAusschlussFenster(BaseModel):
    """Zeitfenster pro Zone, das ML-Training + Baseline-Evaluation
    ueberspringt.

    Hintergrund: Sensor-Umzuege, Austausch, Kalibrierung oder bekannte
    Datenluecken erzeugen Messwerte, die das Modell nicht lernen soll.
    Statt stumm zu puffern: explizit in der Config eintragen, damit es
    nachvollziehbar bleibt und nichts vergessen wird.
    """
    zone_id: str
    von: datetime                    # inklusiv
    bis: datetime                    # inklusiv
    grund: str = ""                  # Menschenlesbare Notiz
    # T-0267: optionaler Filter pro Sensor-Geraet. Default `None` = das
    # Fenster gilt fuer ALLE Sensoren der Zone (Backward-Compat).
    # Beispiel: ein FYTA-Einschlaemm-Fenster soll nur die FYTA-Messungen
    # ausschneiden, nicht den Gardena-Sensor derselben Zone. Dafuer
    # mehrere Eintraege mit dem gleichen (zone_id, von, bis) und je
    # einem unterschiedlichen `geraet_id` anlegen.
    geraet_id: str | None = None
    # T-0250: Auto-Flip aller `manuell`/`watchdog`-Events der Zone im
    # Fenster auf `ignoriert`. Use-Case: User nutzt den Ventilkanal
    # temporaer fuer einen Fremd-Zweck (z.B. hecke-Kanal beregnet eine
    # neue Gras-Aussaat-Flaeche, nicht die Hecke selbst). Ohne Flip
    # zaehlen die Live-Events als "Hecke-Bewaesserung" in Bilanz +
    # Wirkungsrate -- semantisch falsch. Mit Flip bleibt der Konsens
    # konsistent: ml_ausschluss_fenster => Events in diesem Zeitraum
    # sind kein echter Lauf der Zone.
    #
    # Default `False`: bestehende Anwendung "Sensor-Drama-Phase mit
    # echter Bewaesserung dazwischen" (z.B. waldblumenhain Phase 3 mit
    # User-Guss) bleibt unberuehrt -- der manuelle Guss soll in der
    # Bilanz weiter zaehlen, nur ML soll nicht trainieren.
    events_auto_ignorieren: bool = False
    # T-0304: Zweck des Fensters -- steuert die UI-Darstellung. ZWEI voellig
    # verschiedene Faelle, die das UI frueher beide als "Kalibrierung laeuft"
    # rendert hat:
    #   "sensor_kalibrierung": Sensor liefert UNzuverlaessige Werte
    #     (Bodenart-Reset, Umzug, Einschlemmen). -> Werte + Empfehlung
    #     unzuverlaessig, Karte zeigt "Kalibrierung", Chart dimmt die Linie.
    #   "event_ignore": Sensor ist OK, nur Kanal-EVENTS werden ignoriert
    #     (z.B. Gras-Beregnung auf dem Magerwiese-Kanal, T-0300). -> Werte +
    #     Empfehlung GUELTIG, Karte zeigt "Events ignoriert", Chart dimmt NICHT.
    # Default None -> Inferenz aus `events_auto_ignorieren` (Backward-Compat:
    # alte Configs ohne `zweck` bekommen automatisch den richtigen Wert).
    # Aufloesung ueber `effektiver_zweck`.
    zweck: str | None = None

    @property
    def effektiver_zweck(self) -> str:
        """T-0304: 'sensor_kalibrierung' | 'event_ignore'. Explizit gesetzt
        gewinnt; sonst inferiert aus events_auto_ignorieren."""
        if self.zweck in ("sensor_kalibrierung", "event_ignore"):
            return self.zweck
        return "event_ignore" if self.events_auto_ignorieren else "sensor_kalibrierung"


class WetterArchivStunde(BaseModel):
    """Historische Wetterstunde aus dem Open-Meteo-Archiv.

    Quelle ist der Archive-API-Default (`best_match`), an diesem Standort
    `ecmwf_ifs` — NICHT ERA5 und keine Stationsmessung. Siehe die Korrektur
    im Modul-Docstring von `wetter_archiv.py` (T-0507).
    """
    zeitstempel: datetime
    niederschlag_mm: float = 0.0
    temperatur: float | None = None
    et0_mm: float = 0.0


class BilanzKonfig(BaseModel):
    """Wasser-Bilanz Kalibrierung fuer T-0029.

    `kanal_liter_pro_minute`: Durchfluss pro Gardena-Ventilkanal
      (z.B. {1: 6.0, 2: 1.87}), gemessen am tatsaechlichen Ausgang.
    `geraet_kanal_liter_pro_minute`: Multi-DSWC-Override pro
      Geraet+Kanal. Wenn gesetzt, gewinnt dieser Wert vor der legacy
      Kanalrate.
    `manuell_liter_pro_minute`: Fallback bei ausloeser=manuell ohne
      explizite liter-Angabe (freier Gartenschlauch, gemessen).
    """
    kanal_liter_pro_minute: dict[int, float] = {}
    geraet_kanal_liter_pro_minute: dict[str, dict[int, float]] = {}
    manuell_liter_pro_minute: float = Field(default=10.0, gt=0)

    def liter_pro_minute_fuer_zone(self, zone: ZonenKonfig) -> float | None:
        if zone.ventil_kanal is None:
            return None
        if zone.ventil_geraet_id:
            pro_kanal = self.geraet_kanal_liter_pro_minute.get(zone.ventil_geraet_id)
            if pro_kanal and zone.ventil_kanal in pro_kanal:
                return pro_kanal[zone.ventil_kanal]
        return self.kanal_liter_pro_minute.get(zone.ventil_kanal)


class BackupKonfig(BaseModel):
    """Konfiguration fuer den periodischen DB-Backup-Job (T-0043).

    Intervall-gesteuerter Job, der taegliche Snapshots in
    `<verzeichnis>/taeglich/` ablegt (Retention `retention_taeglich_tage`)
    und optional den ersten Snapshot jedes Monats in
    `<verzeichnis>/monatlich/` dauerhaft aufhebt.
    """
    aktiv: bool = True
    # `intervall_stunden=0` ist ein zulaessiger Test-Shortcut ("immer feuern").
    intervall_stunden: int = Field(default=24, ge=0)
    verzeichnis: str = "./daten/backup"
    retention_taeglich_tage: int = Field(default=14, ge=1)
    monatlich_aktiv: bool = True
    max_dateien: int = Field(default=200, ge=1)  # Sicherheitsnetz gegen Ueberlauf
    # T-0131 (H-7): Off-Mac-Spiegelung des Backups, z. B. iCloud Drive.
    # Path-Expansion (`~`, env) wird bei Verwendung gemacht. None = aus.
    # Empfohlen: `~/Library/Mobile Documents/com~apple~CloudDocs/
    # Pflanzen-Dashboard-Backup` -- iCloud Drive synced automatisch
    # zu Apple-Cloud + anderen Geraeten. Mac-Disk-Defekt (Akut-Plan
    # Akt 5b) wird so abgefangen.
    spiegel_verzeichnis: str | None = None


class MlRetrainKonfig(BaseModel):
    """T-0048: Automatischer wiederkehrender ML-Retrain mit Deploy-Gate.

    Retrain laeuft im Service-Loop, Intervall-gesteuert (Default 7 Tage).
    Neue Modelle werden erst live geschaltet, wenn pro Horizont
    `mae_neu < gate_faktor * mae_alt` gilt — sonst Rollback ohne
    Aenderung an den Live-Symlinks.

    Validierung: `gate_faktor` > 0 und < 10. Werte < 1 sind die
    Standard-Logik ("neues Modell muss spuerbar besser sein"). Werte
    >= 1 sind Sonder-Modus fuer Konzept-Drift-Perioden ("auch
    schlechtere CV-MAE akzeptieren, weil das neue Modell auf der
    Live-Verteilung besser ist als das alte; CV laeuft auf Trainings-
    Daten der vergangenen Wetter-Realitaet"). Werte > 10 sind eine
    technische Schutzgrenze gegen unsinnige Konfig (z. B. Tippfehler
    "150" statt "1.5").
    """
    aktiv: bool = False  # Opt-In: nach erstem erfolgreichen Dry-Run anschalten
    intervall_tage: int = Field(default=7, ge=1)
    trainings_fenster_tage: int = Field(default=60, ge=7)
    gate_faktor: float = Field(default=0.95, gt=0, lt=10)
    folds: int = Field(default=3, ge=2)
    quantile: bool = True   # Quantile-Modelle trainieren (bevorzugt seit T-0046)
    monotone_constraints: bool = False  # Opt-In fuer Regen-Monotonie A/B
    ausgabe_pfad: str = ""  # Default: konfig.ML_DATEN_PFAD
    tmp_suffix: str = "_tmp"
    archiv_suffix: str = "_archiv"
    # Beim Service-Start NICHT sofort retrainen — 2 Min CPU-Last kollidiert
    # mit Inferenz-Welle vom Browser und macht das Dashboard langsam.
    # Default 30 Min gibt dem Loop Zeit stabil zu werden; kann auf 0 gesetzt
    # werden, wenn absichtlich ein Initial-Retrain erwuenscht ist.
    start_verzoegerung_minuten: int = Field(default=30, ge=0)
    # T-0403: Feature-Bau in einem eigenen Prozess statt in einem Thread.
    # `asyncio.to_thread` isoliert nur Code, der den GIL abgibt -- der
    # DataFrame-Bau ist Zeile-fuer-Zeile-Python und tut das nicht. Gemessen
    # 13.08.2026: Event-Loop zu 82 % der Laufzeit laenger als 100 ms
    # blockiert, obwohl der Aufruf im Thread lag. Default an; auf false
    # setzen faellt auf das alte Verhalten zurueck (ein Prozess, blockierend).
    # Faellt der Subprozess aus, weicht der Job von selbst dorthin aus.
    #
    # **Default bewusst False, scharf ueber `config/default.yaml`.** Der
    # Subprozess laedt die Standard-Konfig und damit die PRODUKTIV-Datenbank.
    # Als Modell-Default wuerde er in jedem Test anspringen, der den Job ohne
    # explizite Konfig baut -- also die echte DB lesen und die Suite von
    # 35 s auf Minuten ziehen. Ein Schalter, der in Tests etwas anderes tut
    # als gemeint, ist gefaehrlicher als der Zustand, den er behebt.
    feature_bau_subprozess: bool = False
    # T-0082: Cluster-Architektur fuer Pro-Zone-Modelle.
    # "global" (Default): heutiges Verhalten — ein Modell fuer alle
    #   Zonen, in `ml/aktuell_*.lgbm`. Maximale Backward-Compat.
    # "pro_zone": iteriert ueber alle distinkten cluster_id (default
    #   == zone_id), trainiert pro Cluster ein eigenes Modell unter
    #   `ml/feuchte/<cluster_id>/`. Aktivierung erst nach Initial-Lauf.
    cluster_strategie: str = Field(default="global", pattern="^(global|pro_zone)$")
    # Mindest-Trainingszeilen pro Cluster. Cluster unter dieser Schwelle
    # werden uebersprungen (Status `unzureichend_daten`); Inferenz fuer
    # diese Zonen nutzt das `_legacy_global`-Modell als Fallback. 1500 ist
    # der Erfahrungswert fuer stabile LightGBM-Quantile-Modelle.
    mindest_zeilen_pro_cluster: int = Field(default=1500, ge=100)
    # Pro Cluster anpassbarer Gate-Faktor; leerer Default = `gate_faktor`
    # gilt fuer alle Cluster. Beispiel `{"bambuswald": 0.99}` lockert
    # das Gate fuer einen datenarmen Cluster, damit Verbesserungen
    # schneller durchkommen.
    gate_faktor_pro_cluster: dict[str, float] = {}


class MlBewaesserungsResponseKonfig(BaseModel):
    """T-0065: Pro-Zone-Response-Modell fuer Giessdauer-Empfehlung.

    Ersetzt die feste Heuristik in _berechne_dauer durch ein
    LightGBM-Inverses-Modell (ziel_delta, f_vor, Kontext -> dauer_s).
    Parallel laeuft ein Forward-Modell (q10/q50/q90) als Shadow-Benchmark.

    Zwei Gates:
    - `aktiv`: Modell wird geladen, Empfehlung parallel zur Heuristik in
      `ml_dauer_vorschlag` persistiert (Shadow-Logging).
    - `wirksam`: ML-Empfehlung wird tatsaechlich von _berechne_dauer
      zurueckgegeben (statt Heuristik). Erst nach MAE-Gate.

    `min_events` ist die Mindestanzahl echter SCHLIESSEN-Events mit
    Wasserfluss pro Zone, unter der der Service None liefert und auf
    Heuristik zurueckfaellt (kein Modell-Deploy). Bei N=5 reicht das
    fuer erstes Training aus, 10 ist defensiver Default fuer `wirksam`.
    """
    aktiv: bool = False
    wirksam: bool = False
    min_events: int = Field(default=10, ge=3)
    # T-0482: AquaBloom-Pump-Zonen brauchen eine eigene, hoehere Schwelle.
    # Ihre Labels sind systematisch schwaecher als die der Ventil-Zonen:
    # der Puls ist mit 10-15 min kurz und in der Dauer kaum variabel,
    # delta_6h liegt meist bei 2-6 pp, und der 12-Stunden-Pumptakt
    # entwertet den 24h-Horizont regelmaessig (naechster Puls im Fenster).
    # Mit `min_events`=3 wuerde daraus ein Modell aus wenigen Zeilen
    # gebaut — fuer zitrus (6 Rows/Jahr) genau der Fall, den Andre am
    # 02.08.2026 ausgeschlossen hat.
    min_events_pump_zone: int = Field(default=20, ge=3)
    retrain_intervall_tage: int = Field(default=14, ge=1)
    retrain_event_schwelle: int = Field(default=5, ge=1)
    gate_mae_faktor: float = Field(default=0.5, gt=0, le=1)
    ausgabe_pfad: str = ""  # leer -> ML_DATEN_PFAD/response
    # Puls-Aggregation: aufeinanderfolgende Events auf dem gleichen Kanal,
    # die weniger als `cluster_gap_min` Minuten Pause zwischen
    # `prev.t_end` und `next.t_start` haben, werden als ein
    # Bewaesserungs-Puls betrachtet (dauer_s = Summe, liter = Summe).
    # Default 60 min deckt das "5 min Anpuls + 30 min Pause + richtig
    # giessen"-Muster ab.
    cluster_gap_min: int = Field(default=60, ge=0)
    # T-0539: Feature-Bau in einem eigenen Prozess statt in `to_thread`.
    # Default False wie im Feuchte-Pfad -- ein Job, der ohne Konfig gebaut
    # wird (Tests), darf keinen Subprozess auf die Produktiv-DB loslassen.
    # Produktionswert steht in `config/default.yaml` und MUSS in `konfig.py`
    # gelesen werden ([[fehlerpattern_config_whitelist]]).
    feature_bau_subprozess: bool = False


class SchwellenAdaptionKonfig(BaseModel):
    """ET0-adaptive Anhebung der feuchte_schwelle_min bei Hitze.

    Bei hoher prognostizierter Evapotranspiration wird frueher gegossen,
    damit die Pflanze nicht in Trockenstress rutscht, bevor die Basis-Schwelle
    ueberhaupt erreicht wird. Formel:
        effektiv = basis + k * max(0, ET0_24h - median_et0_mm_pro_tag)
        gedeckelt auf basis + anhebung_max
    Abschaltbar via aktiv=false.
    """
    aktiv: bool = True
    k: float = Field(default=1.0, ge=0)
    median_et0_mm_pro_tag: float = Field(default=2.5, gt=0)  # Brandenburg
    anhebung_max: float = Field(default=10.0, ge=0)          # Deckel in Prozentpunkten


class KalibrierungKonfig(BaseModel):
    """T-0063: Automatische Feldkapazitaets- und Welkepunkt-Erkennung.

    Laeuft periodisch (Default alle 6 h) und scannt die letzten
    `rueckblick_tage` Tage Sensor+Wetter-Daten. Bei Regen-Peaks
    (`regen_min_mm` innerhalb 24 h) wird 12-24 h spaeter der
    Sensor-Plateau-Wert als Feldkapazitaets-Kandidat persistiert.
    In Saison (Mai-September) wird zusaetzlich bei Trockenphasen
    (`welkepunkt_min_tage` ohne > 0.5 mm Regen) das Sensor-Minimum
    als Welkepunkt-Proxy gespeichert.

    Die Ergebnisse landen in Tabelle `feldkapazitaet_messung` und
    koennen via `GET /api/kalibrierung/{zone_id}` ausgelesen werden.
    """
    aktiv: bool = True
    intervall_stunden: int = Field(default=6, ge=1)
    rueckblick_tage: int = Field(default=60, ge=7, le=365)
    regen_min_mm: float = Field(default=10.0, gt=0)
    # T-0063a: von 3.0 auf 5.0 angehoben. Der Gardena-Sensor misst in
    # 5-%-Stufen; mit `plateau_max_delta=3` war das erlaubte Spread
    # (2 x delta = 6 %) unter der Sensor-Aufloesung und verwarf selbst
    # saubere Plateaus mit 2 Stufen-Abfall in 12 h.
    plateau_max_delta: float = Field(default=5.0, gt=0)
    welkepunkt_min_tage: int = Field(default=10, ge=3)
    saison_monate: list[int] = [5, 6, 7, 8, 9]  # Mai-September


class MlSkalenMappingKonfig(BaseModel):
    """T-0181: Skalen-Mapping-Fit-Job.

    Fittet lineare Skalen-Mappings `feuchte_mapped = a * feuchte_raw + b`
    pro Sensor-Quelle gegen den Gardena-Sensor als Referenz, sofern eine
    Zone explizit `calibration_pair_quellen` setzt (Voraussetzung: die
    fremden Sensoren stehen physisch unmittelbar neben dem Gardena-
    Sensor in derselben Bodenschicht).

    Greift erst, wenn pro Quelle mindestens `min_obs` zeitlich gepaarte
    Messungen vorliegen und die Spannweite mindestens `min_spannweite_pp`
    betraegt — sonst ist der lineare Fit unter Sensor-Rauschen nicht
    identifizierbar. Wenn der Residuum-MAE des Fits ueber
    `max_residual_pp` liegt, wird das Mapping verworfen (Mapping nicht
    linear genug, Sensor evtl. defekt).

    Die Ergebnisse landen in Tabelle `sensor_skalen_mapping`
    (`upsert_skalen_mapping`) und wirken live ueber
    `letzte_messung_aggregiert` (schon seit Mai 2026 verdrahtet).
    """
    aktiv: bool = False
    intervall_stunden: int = Field(default=24, ge=1)
    min_obs: int = Field(default=50, ge=10)
    min_spannweite_pp: float = Field(default=20.0, gt=0)
    max_residual_pp: float = Field(default=10.0, gt=0)
    # T-0285: Mindest-|Pearson-r| zwischen Fremd- und Referenz-Sensor.
    # Residuum + Spannweite allein erkennen einen Rausch-Fit NICHT: bei
    # unkorrelierten Sensoren (verschiedene Mikro-Standorte) liefert
    # polyfit eine instabile Steigung mit zufaellig kleinem MAE
    # (Realfall waldblumen: FYTA<->Gardena r~0, Fit a=1.666 zog FYTA
    # 58->51). Unter dieser Schwelle wird KEIN Mapping geschrieben und
    # ein evtl. vorhandenes (Fehl-)Mapping geloescht -> Identity.
    min_korrelation: float = Field(default=0.5, ge=0.0, le=1.0)


class MlPhysikDiagnoseKonfig(BaseModel):
    """Hybrid Stufe 1: Trocknungs-Modul als Read-Only-Diagnose neben
    der LightGBM-Prognose.

    Fittet pro Zone die Trocknungs-Konstante `k_basis_pro_h` aus
    historischen Trockenphasen (≥ `min_phasen_dauer_h` ohne
    Bewaesserung + ohne Regen). Das Physik-Modell
    `f(t) = wp + (f0 - wp) * exp(-k_eff * t)` mit
    `k_eff = k_basis * ET0_aktuell / ET0_basis` liefert dann eine
    deterministische Prognose, die in `GiessEmpfehlung` neben
    `prognose_*h` (LightGBM) zurueckgegeben wird.

    Read-only: aendert `pruefe_zone`/`vorhersage_zone`/Blocker-Kaskade
    NICHT. Nur zur Beobachtung / spaeteren Vergleichsmessung.

    `et0_basis_mm_pro_h` = Brandenburg-Mai-September-Norm 2.5 mm/Tag
    / 24 h ≈ 0.104 mm/h (siehe
    `SchwellenAdaptionKonfig.median_et0_mm_pro_tag`).
    """
    aktiv: bool = False
    intervall_stunden: int = Field(default=24, ge=1)
    min_phasen: int = Field(default=3, ge=1)
    min_phasen_dauer_h: int = Field(default=6, ge=1)
    max_phasen_dauer_h: int = Field(default=72, ge=1)
    default_tau_stunden: float = Field(default=48.0, gt=0)
    et0_basis_mm_pro_h: float = Field(default=2.5 / 24.0, gt=0)
    # Maximaler Regen im Fit-Fenster (mm). Trockenphase mit zu viel
    # Regen ist keine Trockenphase.
    max_regen_im_fenster_mm: float = Field(default=1.0, ge=0)


class MlWirkungFitKonfig(BaseModel):
    """T-0292 Stufe 2: Auto-Fit der Plateau-Wirkungs-Parameter (wmax/r0).

    `aktiv` laesst den Fit-Job laufen: pro Zone werden die schon
    quality-gefilterten `(dauer, delta)`-Paare aus den `wirkungsrate`-
    Kalibrierungs-Records (T-0085, gespeichert als `wert`=rate +
    `basis_mm`=dauer_min -> `delta = wert * basis_mm`) genommen und mit
    `fitte_plateau` (ml/wirkung_fit.py) gefittet. Jeder Lauf persistiert
    das Ergebnis inkl. `angenommen`-Flag + `grund` in Tabelle
    `wirkung_fit` -- auch abgelehnte Fits, fuer die Beobachtung in
    `/api/ml/status`. Read-only bzgl. der Entscheidung.

    `adoptieren` ist der separate, bewusst gefaehrliche Schalter: nur
    wenn True nutzt `_berechne_dauer` (entscheidung.py) den gefitteten
    `wmax`/`r0` statt der Konfig-Werte `zone.wirkung_max_pp` /
    `zone.wirkungsrate_initial`. Default False -> erst beobachten, dann
    adoptieren (Hybrid-Stufe-1->2-Muster, arbeitspattern_hybrid_physik_
    ml_read_only). Adoption greift nur fuer Fits mit `angenommen=True`,
    die juenger als `max_fit_alter_tage` sind; sonst Konfig-Fallback.

    BEFUND 06.06.: heutige Realdaten fallen durchs Quality-Gate
    (`delta` 2-40 pp bei gleicher Dauer-Spanne, r2 < min_r2) -> der Job
    schreibt zwar Records, `adoptieren` greift aber bei keiner Zone.
    """
    aktiv: bool = False
    adoptieren: bool = False
    intervall_stunden: int = Field(default=24, ge=1)
    fenster_tage: int = Field(default=60, ge=7)
    min_n: int = Field(default=30, ge=5)
    min_dauer_spread: float = Field(default=3.0, gt=1.0)
    min_r2: float = Field(default=0.5, ge=0.0, le=1.0)
    max_mse_pp2: float = Field(default=9.0, gt=0)
    konsens_toleranz: float = Field(default=0.35, gt=0)
    max_fit_alter_tage: int = Field(default=30, ge=1)


class MlStateSpaceKonfig(BaseModel):
    """T-0353 (re-scoped): State-Space-Shadow-Forecaster.

    Physik-Decay + Regen-Input + Giess-Puls-Input als Forward-Trajektorie
    (`ml/state_space.py`). **Shadow-only**: Bei `aktiv=True` schreibt der
    EmpfehlungsAuditJob zusaetzlich `prognose_statespace_*h` in
    `empfehlungs_audit`; KEIN Einfluss auf `pruefe_zone`/`vorhersage_zone`/
    Blocker-Kaskade oder Dosierung. Erst nach mehrwoechigem 3-Wege-
    Shadow-Vergleich (ml/physik/statespace, regime-getrennt) darf ueber
    das separate Routing (MlForecastRoutingKonfig) mehr entschieden werden.

    `regen_faktor_pp_pro_mm`: Default 4.0 = Engine-Konstante
    REGEN_FEUCHTE_FAKTOR aus entscheidung.py (`_wetter_aenderung` nutzt
    sie ebenfalls pro Stunde ohne Schwelle) — bewusst KEIN neuer Fit.
    `ramp_stunden`: Sensor-Verzoegerung des Giess-Pulses (~1h Einsickern,
    Memory domain_giesswirkung_sensor_verzoegerung, +Puffer).
    `zonen`: Whitelist; leer = alle Zonen mit aufloesbarem Welkepunkt.
    """
    aktiv: bool = False
    ramp_stunden: float = Field(default=1.5, ge=0)
    puls_lookback_stunden: float = Field(default=3.0, ge=0)
    regen_faktor_pp_pro_mm: float = Field(default=4.0, ge=0)
    zonen: list[str] = []


class MlForecastRoutingKonfig(BaseModel):
    """T-0353: pro-Zone-Routing der Feuchte-Prognose (Shadow).

    T-0348-Verdikt: die Physik-vs-ML-Spaltung laeuft ueber Datendichte
    pro Zone (waldblumenhain physik-dominant, Mikrodrip-Zonen ML-
    dominant) -> pro Zone eine Prognose-Quelle statt Monolith-Umbau.

    HEUTE nur Beobachtung: der EmpfehlungsAuditJob loggt die
    Router-Wahl als `routing_quelle` in `empfehlungs_audit`.
    `entscheidung.py` ruft den Router NICHT auf — die Umschaltung des
    Entscheidungs-Pfads ist ein separater, user-gated Schritt nach dem
    mehrwoechigen Shadow-Beweis.

    `zonen`: zone_id -> "ml" | "physik" | "statespace".
    """
    aktiv: bool = False
    default_quelle: str = "ml"
    zonen: dict[str, str] = {}


class HahnCluster(BaseModel):
    """T-0151: Gemeinsamer Wasserhahn-Cluster fuer mehrere Zonen.

    `cluster_id` matcht `ZonenKonfig.hahn_cluster`. `max_durchfluss_lpm`
    ist die Druckminderer-Obergrenze in Litern pro Minute, die VOR
    Druck-Kollaps gehalten werden muss. Wenn die Summe der `verbrauch_lpm`
    aller laufenden Bewaesserungen im Cluster + Verbrauch der neu zu
    startenden Zone <= `max_durchfluss_lpm`, darf parallel gestartet
    werden — sonst wird mit `HAHN_BELEGT` abgelehnt.

    Faustwerte (Hauswasseranschluss + 2-bar-Druckminderer fuer
    Mikrodrip):
      - Mikrodrip Bambus-Kette: ~1.87 L/min druckkompensiert
      - Mikrodrip Hecke (geplant): ~1-2 L/min druckkompensiert
      - Sprinkler Viereckregner Waldblumen: ~6 L/min druckabhaengig

    Empfohlener `max_durchfluss_lpm` = Hahn-Maximum (= Sprinkler-Solo-
    Durchfluss). Beispiel: 6.0 L/min. Damit laeuft Sprinkler
    alleine durch (6.0 <= 6.0), blockt aber jede Mikrodrip-Parallelitaet
    (6.0+1.87 > 6.0). Zwei Mikrodrip-Strecken parallel (Bambus 1.87 +
    Hecke ~1.5 = 3.37) bleiben unter dem Budget und duerfen gleichzeitig.

    Achtung: 4.5 als Budget waere falsch — Sprinkler-Solo (6.0) wuerde
    selbst alleine schon ablehnen. Das ist der typische Bug bei der
    Wahl: Budget muss >= max(verbrauch_lpm aller Cluster-Zonen) sein.
    """
    cluster_id: str
    max_durchfluss_lpm: float = Field(gt=0)


class WochenReportKonfig(BaseModel):
    """T-0038: Wochen-Report per iMessage.

    Sonntags 20:00 wird eine Zusammenfassung der vergangenen 7 Tage an den
    Empfaenger gesendet (Bewaesserungen, Regen/ET0, ML-MAE, Warnungen).
    `tag_der_woche`: Python-weekday (0=Mo, 6=So). Idempotent pro ISO-Woche.
    """
    aktiv: bool = False
    empfaenger: str = ""
    tag_der_woche: int = Field(default=6, ge=0, le=6)  # Sonntag
    stunde: int = Field(default=20, ge=0, le=23)


class GesamtKonfig(BaseModel):
    """Gesamtkonfiguration der Anwendung."""
    gardena: GardenaKonfig
    zonen: list[ZonenKonfig]
    wetter: WetterKonfig
    speicher: SpeicherKonfig = SpeicherKonfig()
    standorte: list[StandortKonfig] = []
    balkon_ausrichtung: dict[str, BalkonKonfig] = {}
    fyta: FytaKonfig | None = None
    # T-0234: microdrip_solar entfernt (toter Konfig-Vertrag, AquabloomJob
    # deckt den Use-Case ab).
    ventilsteuerung_aktiv: bool = False  # Opt-In: True = Ventile werden angesteuert
    schwellen_adaption: SchwellenAdaptionKonfig = SchwellenAdaptionKonfig()
    bilanz: BilanzKonfig = BilanzKonfig()
    backup: BackupKonfig = BackupKonfig()
    ml_retrain: MlRetrainKonfig = MlRetrainKonfig()
    ml_bewaesserungs_response: MlBewaesserungsResponseKonfig = MlBewaesserungsResponseKonfig()
    ml_ausschluss_fenster: list[MlAusschlussFenster] = []
    wochen_report: WochenReportKonfig = WochenReportKonfig()
    kalibrierung: KalibrierungKonfig = KalibrierungKonfig()
    ml_skalen_mapping: MlSkalenMappingKonfig = MlSkalenMappingKonfig()
    ml_physik_diagnose: MlPhysikDiagnoseKonfig = MlPhysikDiagnoseKonfig()
    ml_wirkung_fit: MlWirkungFitKonfig = MlWirkungFitKonfig()
    # T-0353: State-Space-Shadow + pro-Zone-Prognose-Routing (Shadow).
    ml_state_space: MlStateSpaceKonfig = MlStateSpaceKonfig()
    ml_forecast_routing: MlForecastRoutingKonfig = MlForecastRoutingKonfig()
    watchdog: WatchdogKonfig = WatchdogKonfig()
    endpoint_health: EndpointHealthKonfig = EndpointHealthKonfig()
    # T-0050b: Intervall fuer FYTA-Plant-Optimum-Cache (nur FYTA-Zonen).
    # 24 h ist ueblich — FYTA aendert Optimum-Werte selten (meist nie).
    plant_optimum_intervall_stunden: int = Field(default=24, ge=1)
    # T-0151: Hahn-Cluster-Definitionen. Default leer = kein Lock-Check
    # (Backward-Compat). Wenn gesetzt + Zonen referenzieren `hahn_cluster`,
    # gilt das Durchfluss-Budget als Vor-Bedingung beim Ventil-Start.
    hahn_cluster: list[HahnCluster] = []
    # T-0204: Klartext-Namen pro Sensor-Geraet-ID, fuer SensorListeDiagnose.
    # FYTA-Sensoren werden automatisch aus `fyta.pflanzen[].name` aufgeloest
    # (Schluessel `fyta_<fyta_id>`); Gardena-Sensoren (UUID) muessen hier
    # manuell gepflegt werden. Leerer Default = Fallback auf gekuerzte ID.
    sensor_namen: dict[str, str] = {}
    # T-0535: randomisierter Dosis-Test (Default INAKTIV = exakter No-op).
    # Ueberschreibt fuer das konfigurierte Ventil die berechnete Hauptdose
    # durch eine blockrandomisierte Teststufe. Whitelist-Pattern: der Block
    # wird in `konfig.lade_konfig` explizit gelesen, sonst bliebe der
    # YAML-Eintrag stumm (Memory: fehlerpattern_config_whitelist.md).
    dosis_test: DosisTestKonfig = DosisTestKonfig()
    log_level: str = "INFO"
