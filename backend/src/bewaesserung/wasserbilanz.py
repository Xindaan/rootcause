"""T-0422: FAO-56-Wasserbilanz der Wurzelzone (Shadow).

**Warum.** Die Steuerung entscheidet heute an einem Sensor-Relativwert gegen
eine Schwelle. Das beantwortet "ist es trocken?", aber nicht "wie viel Wasser
fehlt?". Fuer die zweite Frage braucht es eine Bilanz in mm -- und die ist
auch die Voraussetzung dafuer, Regen ueberhaupt verrechnen zu koennen
(T-0423: p20 des Ensembles statt Ja/Nein-Schalter).

**Das Modell (FAO-56, Allen et al.).** Die Wurzelzone ist ein Speicher:

    TAW = 1000 x (theta_FC - theta_WP) x Zr     [mm]   Gesamtvorrat
    RAW = p x TAW                                [mm]   ohne Stress nutzbar
    Dr_neu = Dr_alt + ET0 x Kc - Regen - Bewaesserung   [mm]   Auszehrung

`Dr` (depletion) ist die Auszehrung seit der letzten Feldkapazitaet, in mm.
Bei `Dr > RAW` beginnt Trockenstress -- das ist die Giess-Bedingung.

**Warum das auf Sand teurer ist als auf Lehm.** Fuer lehmigen Sand und
flache Wurzeln (Zr ~0,25 m) ist TAW ~20 mm und RAW ~8 mm. Bei ET0 4,5 mm/Tag
sind das **unter zwei Tagen Puffer**. Auf Lehm waeren es fuenf. Ein
uebersehener Giess-Tag ist hier also nicht "etwas trockener", sondern der
Unterschied zwischen Reserve und Stress. Genau deshalb rechnet T-0423 den
Regen konservativ (p20) statt mit dem Punktwert.

**BEWUSSTE GRENZE -- die mm-Naeherung bei Tropfbewaesserung.**
`mm = Liter / Flaeche` unterstellt flaechige Verteilung. Bei einem
Sprinkler (waldblumenhain, magerwiese) stimmt das. Bei Mikrodrip-Ringen um
einzelne Horste (bambuswald, hecke) geht das Wasser punktuell in einen
Bruchteil der Flaeche -- die reale Infiltration dort ist tiefer und
raeumlich enger als die Bilanz annimmt. Fuer Tropf-Zonen ist die mm-Bilanz
deshalb ein INDIKATOR, keine Messung. (Dieselbe Falle wie bei der
Bambus-Dosis-Rechnung: dort war Liter/Pflanze die richtige Groesse, nicht
mm/m2 -- s. Memory `domain_bambus_giessregime`.)

**Shadow.** Dieses Modul entscheidet nichts. Es rechnet parallel mit und
loggt, was es entschieden HAETTE. Erst wenn belegt ist, dass die Bilanz
besser trifft als die Sensor-Schwelle, wandert sie in den Live-Pfad.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog

logger = structlog.get_logger()

# --- Bodenparameter, FAO-56 Tabelle 19 ------------------------------------
# (theta_FC - theta_WP) in mm Wasser je m Bodentiefe.
# Der Referenzgarten ist Sandboden unter Kiefern; die Pflanzloecher wurden mit
# Rhododendronerde/Kompost angereichert (s. Pflanzplan), daher nicht der
# reine Sand-Wert, sondern der obere Rand von "loamy sand".
NUTZBARE_FELDKAPAZITAET_MM_PRO_M = {
    "sand": 60.0,
    "lehmiger_sand": 90.0,
    "sandiger_lehm": 120.0,
    "lehm": 160.0,
}
BODENART_DEFAULT = "lehmiger_sand"

# p-Faktor: welcher Anteil von TAW ohne Stress entnommen werden darf.
# FAO-56 nennt fuer Gehoelze/Straeucher 0,4-0,5. Konservativ 0,4 --
# lieber frueher giessen als die Pflanze in den Stress laufen lassen.
P_FAKTOR_DEFAULT = 0.4

# Wurzeltiefe in m. Fargesia und die Waldstauden sind Flachwurzler;
# der Gardena-Sensor misst 3-18 cm, deckt diese Zone also weitgehend ab.
WURZELTIEFE_M_DEFAULT = 0.25


@dataclass(frozen=True)
class BodenSpeicher:
    """Die Speichergroessen einer Zone, in mm."""
    taw_mm: float          # total available water
    raw_mm: float          # readily available water (= p x TAW)
    bodenart: str
    wurzeltiefe_m: float
    p_faktor: float

    @property
    def puffer_tage(self) -> float:
        """Nur zur Einordnung: Tage bis RAW bei 4,5 mm/Tag Sommer-ET0."""
        return self.raw_mm / 4.5


def speicher_fuer_zone(
    bodenart: str | None = None,
    wurzeltiefe_m: float | None = None,
    p_faktor: float | None = None,
) -> BodenSpeicher:
    """TAW/RAW aus Bodenart und Wurzeltiefe (FAO-56)."""
    art = (bodenart or BODENART_DEFAULT).lower()
    nfk = NUTZBARE_FELDKAPAZITAET_MM_PRO_M.get(
        art, NUTZBARE_FELDKAPAZITAET_MM_PRO_M[BODENART_DEFAULT]
    )
    zr = wurzeltiefe_m if wurzeltiefe_m is not None else WURZELTIEFE_M_DEFAULT
    p = p_faktor if p_faktor is not None else P_FAKTOR_DEFAULT
    taw = nfk * zr
    return BodenSpeicher(
        taw_mm=taw, raw_mm=p * taw, bodenart=art,
        wurzeltiefe_m=zr, p_faktor=p,
    )


@dataclass(frozen=True)
class BilanzSchritt:
    """Ein Fortschreibungs-Schritt der Auszehrung."""
    dr_vorher_mm: float
    dr_nachher_mm: float
    et0_mm: float
    regen_mm: float
    bewaesserung_mm: float
    speicher: BodenSpeicher

    @property
    def giessen(self) -> bool:
        """Die Kernbedingung: Auszehrung ueber der nutzbaren Reserve."""
        return self.dr_nachher_mm > self.speicher.raw_mm

    @property
    def defizit_mm(self) -> float:
        """Wie viel mm fehlen bis zur Feldkapazitaet."""
        return max(0.0, self.dr_nachher_mm)

    @property
    def anteil_raw(self) -> float:
        """Auszehrung als Anteil der nutzbaren Reserve (1.0 = Stressbeginn)."""
        return self.dr_nachher_mm / self.speicher.raw_mm if self.speicher.raw_mm else 0.0


def schreibe_fort(
    dr_vorher_mm: float,
    et0_mm: float,
    regen_mm: float,
    bewaesserung_mm: float,
    speicher: BodenSpeicher,
    kc: float = 1.0,
) -> BilanzSchritt:
    """Fortschreibung: Dr_neu = Dr_alt + ET0 x Kc - Regen - Bewaesserung.

    Zwei Kappungen, beide physikalisch:
    - **Dr >= 0**: mehr Wasser als bis zur Feldkapazitaet kann der Boden nicht
      halten, der Ueberschuss versickert. Ohne diese Kappung wuerde ein
      Starkregen ein "Guthaben" anlegen, das die naechste Trockenphase
      faelschlich abpuffert. Auf Sand ist genau das falsch -- dort laeuft
      der Ueberschuss schnell durch.
    - **Dr <= TAW**: unterhalb des Welkepunkts gibt es nichts mehr zu
      entnehmen. Ein hoeherer Wert waere rechnerisch moeglich, physikalisch
      aber sinnlos und wuerde nach einem Regen eine viel zu lange
      Wiederauffuell-Phase vortaeuschen.
    """
    dr = dr_vorher_mm + et0_mm * kc - regen_mm - bewaesserung_mm
    dr = max(0.0, min(dr, speicher.taw_mm))
    return BilanzSchritt(
        dr_vorher_mm=dr_vorher_mm, dr_nachher_mm=dr,
        et0_mm=et0_mm, regen_mm=regen_mm,
        bewaesserung_mm=bewaesserung_mm, speicher=speicher,
    )


# --- Benetzte Flaeche bei Tropfbewaesserung (T-0422 Variante 2) -----------
# Ein Tropfer erzeugt auf Sand eine Benetzungszwiebel von ~20-25 cm Radius --
# Sand leitet lateral schlecht, das Wasser geht in die Tiefe. Bei 30 cm
# Tropferabstand ueberlappen sich die Zwiebeln gerade, es entsteht ein
# durchgehender Streifen von ~45 cm Breite entlang des Rohrs.
TROPFER_ABSTAND_M = 0.30
BENETZUNGSBREITE_SAND_M = 0.45


def benetzte_flaeche_m2(n_tropfer: int) -> float:
    """T-0422 Variante 2: die Flaeche, auf die das Tropfwasser wirklich geht.

    **Warum die Config-`flaeche_m2` hier NICHT taugt.** Sie ist bei den
    Bambus-Zonen die BALLEN-Flaeche (0,58 m2 je Pflanze, T-0029-Konvention
    fuer die Liter-Bilanz). Der Tropfring liegt aber 15-20 cm vom Halmfuss
    entfernt AUSSEN herum und benetzt mehr als den Ballen. Mit der
    Ballenflaeche gerechnet ergaben sich 46 mm je Tagesgabe -- das Doppelte
    des gesamten Bodenspeichers, also eine physikalisch sinnlose Zahl.

    **Streifen statt Kreis.** Die Memory-Formel `0,0072 x N^2` unterstellt
    EINEN Kreis aus allen Tropfern. Bei mehreren Horsten (bambuswald: 25
    Tropfer auf 3 Ringe) ueberschaetzt das die Flaeche stark, weil die
    Flaeche quadratisch mit dem Umfang waechst. Der Streifen-Ansatz
    `N x Abstand x Breite` ist unabhaengig davon, wie die Tropfer auf Ringe
    verteilt sind -- und genau das ist hier unbekannt.

    Verifiziert an bambuswald: 25 Tropfer -> 3,4 m2 statt 1,74 m2 Ballen.
    Eine reale Tagesgabe (80,6 L) sind damit 24 mm statt 46 mm -- also
    "Speicher etwa voll" statt "doppelter Speicherinhalt".
    """
    if n_tropfer <= 0:
        return 0.0
    return n_tropfer * TROPFER_ABSTAND_M * BENETZUNGSBREITE_SAND_M


def liter_zu_mm(liter: float, flaeche_m2: float | None) -> float:
    """Wasser-Input in mm. Ohne Flaeche 0.0 statt Division durch Null.

    ACHTUNG bei Tropfbewaesserung -- s. Modul-Docstring: das Ergebnis ist
    dort ein Indikator, keine Messung.
    """
    if not flaeche_m2 or flaeche_m2 <= 0:
        return 0.0
    return liter / flaeche_m2


def entscheidung_mit_ensemble(
    dr_mm: float,
    et0_24h_mm: float,
    regen_konservativ_mm: float,
    speicher: BodenSpeicher,
    kc: float = 1.0,
) -> tuple[bool, float]:
    """T-0422-Kernbedingung: `Dr + ET0 - Regen_konservativ > RAW`?

    `regen_konservativ_mm` ist der p20 der Ensemble-Member-Summen (T-0423),
    NICHT der deterministische Punktwert. Begruendung dort: bei konvektivem
    Sommerregen ist der Punktwert eine Stichprobe aus einer breiten
    Verteilung, und die Kosten sind asymmetrisch -- ein unnoetiger Guss ist
    billig, ein ausgefallener auf 8 mm RAW teuer.

    Rueckgabe `(giessen, prognostizierte_auszehrung_mm)`.
    """
    dr_prognose = dr_mm + et0_24h_mm * kc - regen_konservativ_mm
    dr_prognose = max(0.0, min(dr_prognose, speicher.taw_mm))
    return dr_prognose > speicher.raw_mm, dr_prognose
