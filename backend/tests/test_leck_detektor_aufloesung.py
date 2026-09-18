"""T-0428: Alarmgrenze muss ueber der Sensor-Aufloesung liegen.

Realfall 23.07.2026: beide Bambus-Karten zeigten rot "Bewaesserung ohne
Wirkung" nach einem 297-s-Pre-Soak --
  bambuswald  90.0% -> 90.0%  (delta +0.0, erwartet >=0.5)
  yogaraum    60.0% -> 60.0%  (delta +0.0, erwartet >=1.0)
Zurueckgerechnet (erwartet = schwelle / 0.3) waren das 1,7 bzw. 3,3 pp
erwartete Wirkung. Der Gardena-Sensor kann aber nur 5,0 pp anzeigen oder
nichts -- `delta +0.0` war dort nicht der Nachweis eines Problems, sondern
der wahrscheinlichste normale Messwert.
"""

import pytest

from bewaesserung.leck_detektor import (
    AUFLOESUNG_PP,
    AUFLOESUNG_SICHERHEITSFAKTOR,
    BEW_ALARM_FAKTOR,
    WirkungsProfil,
    aufloesungs_min_pp,
)


def test_gardena_untergrenze_liegt_ueber_der_quantisierung():
    """Die Kernaussage: die Untergrenze muss GROESSER sein als die
    Stufenhoehe. Bei 5 pp Raster ist alles darunter nicht messbar."""
    assert aufloesungs_min_pp("gardena") > AUFLOESUNG_PP["gardena"]
    assert aufloesungs_min_pp("gardena") == pytest.approx(10.0)


def test_unbekannte_quelle_faellt_auf_die_groebere_annahme():
    """Konservativ: ohne Wissen ueber die Quelle lieber schweigen als
    auf einem Raster alarmieren, das die Erwartung nicht abbilden kann."""
    assert aufloesungs_min_pp(None) == aufloesungs_min_pp("gardena")
    assert aufloesungs_min_pp("unbekannt") == aufloesungs_min_pp("gardena")


def test_fyta_darf_feiner_aufloesen():
    """FYTA quantisiert feiner; die zeitliche Untergrenze deckt dort
    BEW_MIN_DAUER_SEKUNDEN ab."""
    assert aufloesungs_min_pp("fyta") < aufloesungs_min_pp("gardena")


def test_screenshot_faelle_wuerden_nicht_mehr_alarmieren():
    """DER Regression-Test. Beide Faelle aus dem Screenshot vom 23.07.
    lagen unter der Sensor-Aufloesung und duerfen nicht mehr feuern."""
    grenze = aufloesungs_min_pp("gardena")
    for zone, schwelle_im_screenshot in (("bambuswald", 0.5), ("yogaraum", 1.0)):
        erwartet = schwelle_im_screenshot / BEW_ALARM_FAKTOR
        assert erwartet < grenze, (
            f"{zone}: erwartete Wirkung {erwartet:.1f} pp muesste unter der "
            f"Grenze {grenze} liegen und den Alarm unterdruecken"
        )


def test_pre_soak_puls_faellt_unter_die_grenze():
    """Ein 297-s-Pre-Soak (knapp ueber BEW_MIN_DAUER_SEKUNDEN=240) ist
    grundsaetzlich nicht auswertbar -- das war der ausloesende Lauf."""
    profil = WirkungsProfil(wirkung_max_pp=26.0, wirkungsrate_initial=0.33)
    erwartet = profil.erwartete_wirkung_pp(297)
    assert erwartet < aufloesungs_min_pp("gardena")


def test_lange_dose_wird_weiterhin_ausgewertet():
    """GEGENPROBE -- der Fix darf den Detektor nicht abschalten.
    Eine echte 90-min-Dose auf bambuswald liegt klar ueber der Grenze und
    muss weiterhin alarmieren koennen, wenn nichts passiert."""
    profil = WirkungsProfil(wirkung_max_pp=26.0, wirkungsrate_initial=0.33)
    erwartet = profil.erwartete_wirkung_pp(5400)
    assert erwartet > aufloesungs_min_pp("gardena"), (
        "90-min-Dose muss auswertbar bleiben, sonst ist der Detektor tot"
    )


def test_grenzdauer_liegt_im_erwarteten_bereich():
    """Die Task nennt ~38 min als Grenze fuer bambuswald. Der Test haelt
    die Groessenordnung fest, damit eine spaetere Parameter-Aenderung
    sichtbar wird."""
    profil = WirkungsProfil(wirkung_max_pp=26.0, wirkungsrate_initial=0.33)
    grenze = aufloesungs_min_pp("gardena")
    unter = [s for s in range(60, 7200, 60)
             if profil.erwartete_wirkung_pp(s) < grenze]
    erste_auswertbare_min = (max(unter) + 60) / 60 if unter else 0
    assert 30 <= erste_auswertbare_min <= 50, (
        f"Grenze liegt bei {erste_auswertbare_min:.0f} min, erwartet ~38"
    )


def test_sicherheitsfaktor_ist_begruendet_gross():
    """Ein 5-pp-Raster springt bei exakt 5 pp Wirkung nur, wenn der wahre
    Wert zufaellig eine Bin-Grenze ueberquert -- Faktor 1 reicht nicht."""
    assert AUFLOESUNG_SICHERHEITSFAKTOR >= 2.0


def test_quelle_kommt_aus_der_geraete_id_nicht_aus_der_zone():
    """T-0428-Nachtrag: der Eingefroren-Detektor laeuft PRO GERAET, eine
    Zone kann aber Gardena und FYTA mischen.

    Ein erster Entwurf nahm die Zonen-Quelle und haette die Gardena-
    Schwelle (5 pp) auf FYTA-Sensoren angewandt. Gemessen an echten Daten
    haette das drei NEUE False Positives erzeugt -- drei FYTA-Sensoren der
    waldblumen-Zone, alle mit 48-h-Spanne 3.0, die wegen des
    Skalenbruchs (T-0385) ohnehin niedrig stehen.
    """
    from bewaesserung.leck_detektor import quelle_aus_geraet_id

    # Synthetische IDs. Verhaltensrelevant ist allein das `fyta_`-Praefix
    # bzw. dessen Fehlen -- echte Geraete-IDs haben in einer Datei, die
    # public geht, nichts zu suchen.
    assert quelle_aus_geraet_id("fyta_900001") == "fyta"
    assert quelle_aus_geraet_id("00000000-1111-2222-3333-444444444444") == "gardena"
    assert quelle_aus_geraet_id(None) == "gardena", "unbekannt -> konservativ"


def test_fyta_spanne_3pp_ist_nicht_eingefroren():
    """Konkrete Gegenprobe zum Realfall: 3 pp Spanne ueber 48 h ist fuer
    einen FYTA-Sensor normale Bewegung, fuer einen Gardena dagegen
    weniger als eine Stufe."""
    fyta_schwelle = aufloesungs_min_pp("fyta") / AUFLOESUNG_SICHERHEITSFAKTOR
    gardena_schwelle = aufloesungs_min_pp("gardena") / AUFLOESUNG_SICHERHEITSFAKTOR
    assert 3.0 > fyta_schwelle, "FYTA mit 3 pp Spanne bewegt sich"
    assert 3.0 < gardena_schwelle, "Gardena mit 3 pp hat keine Stufe geschafft"
