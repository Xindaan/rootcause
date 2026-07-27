"""T-0231 Phase 1: Gemeinsame Pro-Zone-Entscheidungs-Logik.

Vor T-0231 hat `pruefe_kanal` (Auto-Loop) nur `effektiv_schwelle_min`
gepruft und Strategien (KORRIDOR/HAEUFIG_KLEIN/SELTEN_GROSS/
KONSTANT_NIEDRIG) + Welkepunkt + sicherheits_tage ignoriert.
`vorhersage_zone` (Dashboard-Dry-Run) hat die volle Logik genutzt.
Konsequenz: Dashboard zeigte 'kein_bedarf', Auto-Loop haette gestartet.

Diese Datei extrahiert die Strategie-Auswertung als gemeinsame, pure
Funktion, die beide Pfade aufrufen koennen. Phase 1 baute die Funktion
+ Tests; Phase 2+3 verdrahteten die echten Aufrufer (entscheidung.py).

Seit dem T-0231-Cleanup ist diese Funktion die Single Source of Truth
fuer die Strategie-Klassifikation. Die alte Inline-Methode
`Entscheidungsmotor._klassifiziere_empfehlung` wurde entfernt;
`vorhersage_zone` und `pruefe_kanal` rufen ausschliesslich
`entscheide_pro_zone`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from bewaesserung.modelle import (
    BewaesserungsStrategie,
    ZonenKonfig,
    effektiv_optimum_max,
    effektiv_optimum_min,
)


# Mapping `empfehlungs_typ` -> soll_bewaessern. `wohlfuehl_grenze` ist
# bewusst KEIN harter Trigger (nur Hinweis im Dashboard), waehrend
# 'akut' und 'praeventiv' Auto-Bewaesserung ausloesen.
EMPFEHLUNG_TRIGGERT_BEWAESSERUNG: frozenset[str] = frozenset({"akut", "praeventiv"})


@dataclass(frozen=True)
class ProZoneKontext:
    """Alle externen Eingaben, die fuer eine Pro-Zone-Entscheidung
    gebraucht werden. Caller (vorhersage_zone bzw. pruefe_kanal) baut
    diese Struktur und uebergibt sie an `entscheide_pro_zone`.

    Felder:
    - `aktuelle_feuchte`: juengste Median-Feuchte der Zone (% VWC-Proxy).
    - `prognose`: Dict Horizont-h -> prognostizierte Feuchte (typisch
      6/12/24h). None oder leer = keine ML-Prognose verfuegbar.
    - `welkepunkt_wert`: aufgeloester Welkepunkt (Regime / Konfig /
      Kalibrierungs-Median / Tagesmin-Schaetzung / feuchte_kritisch).
      None nur wenn die ganze Aufloesungs-Kette nichts liefert.
    - `tage_bis_welke`: ETA bis Sensor <= Welkepunkt (linear). None
      wenn welkepunkt unbekannt oder decay <= 0 (steht stabil).
    - `fk_wert`: aufgeloeste Feldkapazitaet (fuer SELTEN_GROSS-Ziel).
      None wenn Kalibrierungs-Job noch keine Werte hat.
    - `sicherheits_tage_konfig`: zone.sicherheits_tage (Default 3.0
      fuer KORRIDOR; Strategien koennen eigenen Default ueberschreiben).
    - `decay_pp_pro_tag`: ET0-getriebener Decay-Wert pro Tag. KORRIDOR
      nutzt das fuer die Ziel-Reserve `welkepunkt + 5 + decay*st_eff`.
      Default 0 -- dann faellt die Reserve auf welkepunkt+5 zurueck.
    """
    aktuelle_feuchte: float
    prognose: dict[int, float] | None
    welkepunkt_wert: float | None
    tage_bis_welke: float | None
    fk_wert: float | None
    sicherheits_tage_konfig: float = 3.0
    decay_pp_pro_tag: float = 0.0
    # T-0279 Phase 2: robuste Reserve bis optimum_min aus der Physik-
    # Prognose (exp-Decay, ET0-moduliert) -- Quelle fuer den proaktiven
    # Trigger. Bezugslinie ist optimum_min, NICHT der Welkepunkt
    # (Asymptote, als Metrik unbrauchbar -- Metrik-Redesign 31.05.).
    # Bewusst SEPARAT von `tage_bis_welke` (das aus ML/Heuristik kommt
    # und intermittierend halluziniert, T-0278). None = keine Physik
    # gefittet / kein optimum_min -> proaktiver Trigger inaktiv
    # (Fallback altes Verhalten).
    tage_bis_proaktiv: float | None = None
    # T-0286: True wenn ein Ground-Truth-Lauf (live/manuell/DHS) innerhalb
    # des Sensor-Nachlauf-Fensters (versickerungs_karenz) endete. Dann
    # unterdrueckt der proaktive Trigger sich selbst -- die traegen
    # Multi-Sensor-Werte (FYTA) haengen dem frisch gegossenen Gardena
    # noch nach. Nur proaktiv betroffen; akut (Notfall) ignoriert das.
    kuerzlich_gegossen: bool = False


@dataclass(frozen=True)
class ProZoneAuswertung:
    """Ergebnis der Strategie-Auswertung pro Zone. Einheitliches
    Format fuer beide Aufrufer (vorhersage_zone + pruefe_kanal)."""
    zone_id: str
    soll_bewaessern: bool
    empfehlungs_typ: str
    """'akut' / 'praeventiv' / 'wohlfuehl_grenze' / 'kein_bedarf'.
    'akut' + 'praeventiv' triggern Bewaesserung; 'wohlfuehl_grenze'
    ist nur ein Hinweis (kein Trigger), 'kein_bedarf' = nichts tun."""
    ziel_feuchte: float | None
    """Bewaesserungs-Ziel-Feuchte (zone-spezifisch: KORRIDOR ->
    max(welke+reserve, opt_max); HAEUFIG_KLEIN -> opt_max;
    SELTEN_GROSS -> Feldkapazitaet; KONSTANT_NIEDRIG -> opt_min).
    None bei 'kein_bedarf'."""
    effektive_sicherheits_tage: float
    """Strategie-interner Default kann sicherheits_tage_konfig
    ueberschreiben (HAEUFIG_KLEIN=1.0, SELTEN_GROSS=4.0,
    KONSTANT_NIEDRIG=1.5, KORRIDOR=zone-konfig)."""
    aktive_strategie: str
    grund: str
    """Menschen-lesbare Begruendung fuer Dashboard + Audit-Log."""
    ziel_feuchte_roh: float | None = None
    """T-0279 Phase 1b: das Strategie-Ziel, AUCH bei 'kein_bedarf' gesetzt
    (waehrend `ziel_feuchte` dort None ist). Quelle fuer die adoptierbare
    Zieldosis der globalen 1-Klick-Uebernahme -- damit der Banner auch
    bei kein_bedarf eine sinnvolle Dauer (Richtung Strategie-Ziel)
    anbieten kann. None nur wenn die Strategie gar kein Ziel kennt."""


def proaktiv_trigger_aktiv(
    schwelle_tage: float | None,
    reserve_tage: float | None,
    kuerzlich_gegossen: bool = False,
) -> bool:
    """T-0279 Phase 2: True wenn die Physik-Reserve bis optimum_min unter
    die pro-Zone konfigurierte proaktive Schwelle gefallen ist.

    Primitiv-Variante (nur Zahlen) als Single-Source-of-Truth, damit
    `vorhersage_zone` denselben Trigger VOR dem FEUCHTE_OK-Gate pruefen
    kann ohne einen vollen ProZoneKontext zu bauen.

    Voraussetzungen (alle noetig):
    - `schwelle_tage` gesetzt (Opt-In pro Zone, proaktiv_tage_vor_optimum_min).
    - `reserve_tage` vorhanden (Physik gefittet + optimum_min aufloesbar).
    - Reserve <= konfigurierte Schwelle.

    T-0286: `kuerzlich_gegossen` = True (ein Ground-Truth-Lauf endete
    innerhalb des Sensor-Nachlauf-Fensters, versickerungs_karenz)
    unterdrueckt den Trigger. Bei Multi-Sensor-Zonen mit unkorrelierten
    Sensoren reagiert direkt nach dem Giessen nur die Beregnungszone
    (Gardena), die traegen Sensoren (FYTA, andere Mikro-Standorte) hinken
    Stunden nach -> min-Sensor/Median zeigen noch "trocken" und der
    proaktive Trigger wuerde faelschlich erneut giessen wollen. NUR der
    proaktive Trigger ist betroffen; akut (Notfall-Schutz) bleibt aktiv.
    """
    if kuerzlich_gegossen:
        return False
    return (
        schwelle_tage is not None
        and reserve_tage is not None
        and reserve_tage <= schwelle_tage
    )


def _proaktiv_trigger_greift(
    zone: ZonenKonfig, kontext: ProZoneKontext,
) -> bool:
    """Komfort-Wrapper um `proaktiv_trigger_aktiv` fuer den Kontext-Pfad."""
    return proaktiv_trigger_aktiv(
        zone.proaktiv_tage_vor_optimum_min,
        kontext.tage_bis_proaktiv,
        kuerzlich_gegossen=kontext.kuerzlich_gegossen,
    )


def entscheide_pro_zone(
    zone: ZonenKonfig,
    kontext: ProZoneKontext,
    jetzt: datetime,
) -> ProZoneAuswertung:
    """Pro-Zone-Entscheidung mit voller Strategie + Welkepunkt-Logik.

    Pure Funktion: keine DB-Reads, keine ML-Calls, keine Side-Effects.
    Der Caller baut `ProZoneKontext` aus seinen verfuegbaren Quellen
    und bekommt eine deterministische Auswertung zurueck.

    Single Source of Truth fuer die Strategie-Klassifikation (die alte
    Inline-Methode `_klassifiziere_empfehlung` wurde im T-0231-Cleanup
    entfernt).
    """
    strategie = zone.bewaesserungs_strategie
    opt_min = effektiv_optimum_min(zone, jetzt)
    opt_max = effektiv_optimum_max(zone, jetzt)

    # Multi-Horizont-Pruefung: niedrigster prognostizierter Wert aus
    # 6h/12h/24h-Prognose. Spiegelt T-0103-Folge 29.04.: kurzfristige
    # Drops sollen auch triggern, nicht nur 24h-Horizont.
    prognosen = [
        kontext.prognose.get(h)
        for h in (6, 12, 24)
        if kontext.prognose
    ]
    prognosen_validiert = [p for p in prognosen if p is not None]
    prognose_min = (
        min(prognosen_validiert)
        if prognosen_validiert
        else kontext.aktuelle_feuchte
    )

    # 'akut' hat in JEDER Strategie Vorrang (Notfall-Schutz).
    welke_akut = (
        kontext.tage_bis_welke is not None
        and kontext.tage_bis_welke <= 1.5
    )

    if strategie == BewaesserungsStrategie.HAEUFIG_KLEIN:
        st_eff = 1.0
        if welke_akut:
            empf_typ = "akut"
            grund = (
                f"Welkepunkt in {kontext.tage_bis_welke:.1f}d -- "
                f"Notfall-Schutz auch in HAEUFIG_KLEIN."
            )
        elif opt_min is not None and (
            kontext.aktuelle_feuchte < opt_min or prognose_min < opt_min
        ):
            empf_typ = "praeventiv"
            grund = (
                f"Sensor {kontext.aktuelle_feuchte:.1f}% bzw. Prognose-Min "
                f"{prognose_min:.1f}% unter Wohl-Min {opt_min:.1f}% "
                f"(HAEUFIG_KLEIN primaerer Trigger)."
            )
        elif (
            kontext.tage_bis_welke is not None
            and kontext.tage_bis_welke <= st_eff
        ):
            empf_typ = "praeventiv"
            grund = (
                f"Welkepunkt-Reserve {kontext.tage_bis_welke:.1f}d <= "
                f"{st_eff}d (HAEUFIG_KLEIN)."
            )
        else:
            empf_typ = "kein_bedarf"
            grund = (
                f"Sensor {kontext.aktuelle_feuchte:.1f}% ueber Wohl-Min, "
                f"Welkepunkt-Reserve ok."
            )
        ziel = opt_max if opt_max is not None else 75.0
        return _baue_auswertung(
            zone, empf_typ, ziel, st_eff, strategie.value, grund,
        )

    if strategie == BewaesserungsStrategie.SELTEN_GROSS:
        st_eff = 4.0
        if welke_akut:
            empf_typ = "akut"
            grund = (
                f"Welkepunkt in {kontext.tage_bis_welke:.1f}d -- "
                f"SELTEN_GROSS Tiefen-Dose noetig."
            )
        elif _proaktiv_trigger_greift(zone, kontext):
            # T-0279 Phase 2: proaktiv VOR dem 1.5d-Akut-Rand, damit der
            # Tiefenlauf rechtzeitig + im bevorzugten Fenster liegt.
            empf_typ = "praeventiv"
            grund = (
                f"Physik-Reserve bis optimum_min "
                f"{kontext.tage_bis_proaktiv:.1f}d <= proaktiv-"
                f"Schwelle {zone.proaktiv_tage_vor_optimum_min}d "
                f"(SELTEN_GROSS proaktiver Tiefenlauf)."
            )
        else:
            empf_typ = "kein_bedarf"
            grund = (
                f"SELTEN_GROSS triggert nur bei Welkepunkt-Reserve "
                f"<= 1.5d (aktuell {kontext.tage_bis_welke})."
            )
        # Ziel = Feldkapazitaet, Fallback optimum_max + 5
        if kontext.fk_wert is not None:
            ziel = kontext.fk_wert
        elif opt_max is not None:
            ziel = opt_max + 5.0
        else:
            ziel = 80.0
        return _baue_auswertung(
            zone, empf_typ, ziel, st_eff, strategie.value, grund,
        )

    if strategie == BewaesserungsStrategie.KONSTANT_NIEDRIG:
        st_eff = 1.5
        # Trigger nur nahe Welkepunkt -- bewusst Trockenphasen
        if (
            kontext.welkepunkt_wert is not None
            and kontext.aktuelle_feuchte <= kontext.welkepunkt_wert + 5.0
        ):
            empf_typ = "akut"
            grund = (
                f"Sensor {kontext.aktuelle_feuchte:.1f}% <= "
                f"Welkepunkt+5pp {kontext.welkepunkt_wert + 5.0:.1f}% "
                f"(KONSTANT_NIEDRIG Notfall-Schutz)."
            )
        elif welke_akut:
            empf_typ = "akut"
            grund = (
                f"Welkepunkt in {kontext.tage_bis_welke:.1f}d "
                f"(KONSTANT_NIEDRIG)."
            )
        else:
            empf_typ = "kein_bedarf"
            grund = (
                f"KONSTANT_NIEDRIG: Trockenphase ist Feature, "
                f"Sensor {kontext.aktuelle_feuchte:.1f}% in Toleranz."
            )
        # Ziel = optimum_min (NICHT max -- bewusst niedrig halten)
        if opt_min is not None:
            ziel = opt_min
        elif kontext.welkepunkt_wert is not None:
            ziel = kontext.welkepunkt_wert + 10.0
        else:
            ziel = 35.0
        return _baue_auswertung(
            zone, empf_typ, ziel, st_eff, strategie.value, grund,
        )

    # KORRIDOR (Default, Backward-Compat + wohlfuehl_grenze)
    st_eff = kontext.sicherheits_tage_konfig
    if kontext.tage_bis_welke is None:
        empf_typ = "praeventiv"
        grund = "Welkepunkt unbekannt -- praeventiv als Sicherheits-Default."
    elif welke_akut:
        empf_typ = "akut"
        grund = (
            f"Welkepunkt in {kontext.tage_bis_welke:.1f}d (KORRIDOR)."
        )
    elif _proaktiv_trigger_greift(zone, kontext):
        # T-0279 Phase 2 (Isomorphie zu SELTEN_GROSS): proaktiver
        # praeventiv-Trigger auf Physik-Reserve bis optimum_min. Greift
        # nur wenn `proaktiv_tage_vor_optimum_min` gesetzt -- sonst altes
        # Verhalten (heute 0 KORRIDOR-Zonen, T-0272).
        empf_typ = "praeventiv"
        grund = (
            f"Physik-Reserve bis optimum_min "
            f"{kontext.tage_bis_proaktiv:.1f}d <= proaktiv-Schwelle "
            f"{zone.proaktiv_tage_vor_optimum_min}d (KORRIDOR proaktiv)."
        )
    elif kontext.tage_bis_welke <= st_eff:
        empf_typ = "praeventiv"
        grund = (
            f"Welkepunkt-Reserve {kontext.tage_bis_welke:.1f}d <= "
            f"{st_eff}d (KORRIDOR)."
        )
    elif opt_min is not None and (
        kontext.aktuelle_feuchte < opt_min or prognose_min < opt_min
    ):
        empf_typ = "wohlfuehl_grenze"
        grund = (
            f"Welkepunkt-Reserve ok, aber Sensor {kontext.aktuelle_feuchte:.1f}% "
            f"bzw. Prognose-Min {prognose_min:.1f}% unter Wohl-Min "
            f"{opt_min:.1f}%."
        )
    else:
        empf_typ = "kein_bedarf"
        grund = (
            f"KORRIDOR: Welkepunkt-Reserve {kontext.tage_bis_welke:.1f}d > "
            f"{st_eff}d, Sensor {kontext.aktuelle_feuchte:.1f}% ueber Wohl-Min."
        )
    # Ziel = max(welkepunkt + 5 + decay*st_eff, optimum_max).
    # Wenn welkepunkt hoch ist (z. B. 50pp) UND decay >0, kann die
    # Reserve ueber opt_max gehen -- KORRIDOR will dann den hoeheren
    # Wert anpeilen, damit die Welkepunkt-Reserve am Ende der
    # Bewaesserung noch eingehalten ist.
    if kontext.welkepunkt_wert is not None:
        reserve = (
            kontext.welkepunkt_wert
            + 5.0
            + kontext.decay_pp_pro_tag * st_eff
        )
        ziel = max(reserve, opt_max) if opt_max is not None else reserve
    else:
        ziel = opt_max if opt_max is not None else 60.0
    return _baue_auswertung(
        zone, empf_typ, ziel, st_eff, strategie.value, grund,
    )


def _baue_auswertung(
    zone: ZonenKonfig,
    empf_typ: str,
    ziel_feuchte: float,
    st_eff: float,
    strategie_value: str,
    grund: str,
) -> ProZoneAuswertung:
    """Zentraler Konstruktor fuer ProZoneAuswertung. Setzt
    soll_bewaessern konsistent aus empfehlungs_typ ab und
    haendelt 'kein_bedarf' (ziel = None)."""
    soll = empf_typ in EMPFEHLUNG_TRIGGERT_BEWAESSERUNG
    return ProZoneAuswertung(
        zone_id=zone.zone_id,
        soll_bewaessern=soll,
        empfehlungs_typ=empf_typ,
        ziel_feuchte=None if empf_typ == "kein_bedarf" else ziel_feuchte,
        effektive_sicherheits_tage=st_eff,
        aktive_strategie=strategie_value,
        grund=grund,
        # T-0279 Phase 1b: Strategie-Ziel auch bei kein_bedarf erhalten.
        ziel_feuchte_roh=ziel_feuchte,
    )
