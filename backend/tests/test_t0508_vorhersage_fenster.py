"""T-0508: "naechste N Stunden" muss ab JETZT zaehlen, nicht ab Listenanfang.

`WetterVorhersage.niederschlag_naechste_stunden` und die beiden
Schwesterfunktionen slicten `self.stunden[:n]` ohne jeden Zeitfilter. Open-Meteo
liefert bei `forecast_days=2` ab **heute 00:00 Ortszeit** -- die ersten sechs
Eintraege sind also 00:00-05:00, unabhaengig von der Uhrzeit.

Wirkung, live gemessen: im Zonen-Replay ueber die vier scharf geschalteten
Zonen sperrte das Regen-Gate in 268 Entscheidungen **kein einziges Mal
korrekt**; die teuren Faelle stiegen von 5 % auf 9 %.

Die Tests hier bauen bewusst das Muster nach, das den Fehler maximal sichtbar
macht: Regen in der vergangenen Nacht, trocken in der Zukunft. Gegen den alten
Code sind sie rot.
"""

from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import WetterStunde, WetterVorhersage

TAG = datetime(2026, 8, 5)


def _vorhersage(
    regen_stunden: set[int] | None = None,
    et0: float = 0.3,
    stunden_anzahl: int = 48,
) -> WetterVorhersage:
    """48 h ab TAG 00:00 -- genau das Raster, das Open-Meteo liefert."""
    regen_stunden = regen_stunden if regen_stunden is not None else set()
    stunden = [
        WetterStunde(
            zeitstempel=TAG + timedelta(hours=i),
            temperatur=20.0,
            niederschlag_mm=5.0 if i in regen_stunden else 0.0,
            niederschlag_wahrscheinlichkeit=95.0 if i in regen_stunden else 5.0,
            et0_mm=et0,
        )
        for i in range(stunden_anzahl)
    ]
    return WetterVorhersage(abfrage_zeitstempel=TAG, stunden=stunden)


# --------------------------------------------------------------------------
# Der Kernfall: Abend-Giessfenster
# --------------------------------------------------------------------------

def test_alte_semantik_haette_hier_das_gegenteil_gesagt():
    """Die eigentliche Gegenprobe -- und sie steht bewusst IM Test.

    Ein `git stash` auf den alten Code taugt hier nicht als Beleg: die alte
    Signatur nahm kein `jetzt`, also scheitern alle Tests dieser Datei dort mit
    `TypeError`, nicht an einer Zusicherung. Rot heisst dann nur "Signatur neu",
    nicht "Verhalten war falsch".

    Deshalb wird das alte Verhalten hier nachgebaut (`stunden[:n]`, genau die
    Zeile aus `modelle.py:300` vor dem Fix) und dem neuen gegenuebergestellt.
    Der Abstand zwischen beiden Zahlen IST der Bug -- 30 mm gegen 0 mm im
    selben Moment, aus derselben Vorhersage.
    """
    v = _vorhersage(regen_stunden={0, 1, 2, 3, 4, 5})
    jetzt = TAG + timedelta(hours=19, minutes=30)

    alt = sum(s.niederschlag_mm for s in v.stunden[:6])       # modelle.py:300, alt
    neu = v.niederschlag_naechste_stunden(6, jetzt)

    assert alt == 30.0        # sperrt den Guss -- wegen Regen von heute Nacht
    assert neu == 0.0         # laesst giessen -- die naechsten 6 h sind trocken
    assert alt != neu

    # Und dasselbe fuer die beiden Schwesterfunktionen, damit die Klasse
    # vollstaendig abgedeckt ist und nicht nur ihr bekanntester Vertreter.
    assert max(s.niederschlag_wahrscheinlichkeit for s in v.stunden[:6]) == 95.0
    assert v.max_regen_wahrscheinlichkeit_naechste_stunden(6, jetzt) == 5.0


def test_abendfenster_liest_nicht_die_vergangene_nacht():
    """DER Regressionstest.

    19:30 im Abend-Giessfenster, 30 mm sind nachts zwischen 00:00 und 05:00
    gefallen, die Zukunft ist trocken. Der alte Code las `stunden[:6]` = genau
    diese Nachtstunden und sperrte den Guss wegen laengst gefallenen Regens.
    """
    v = _vorhersage(regen_stunden={0, 1, 2, 3, 4, 5})
    jetzt = TAG + timedelta(hours=19, minutes=30)

    assert v.niederschlag_naechste_stunden(6, jetzt) == 0.0
    assert v.max_regen_wahrscheinlichkeit_naechste_stunden(6, jetzt) == 5.0


def test_abendfenster_sieht_echten_kommenden_regen():
    """Die Gegenrichtung -- der Fix darf nicht einfach alles auf 0 setzen."""
    v = _vorhersage(regen_stunden={20, 21, 22})
    jetzt = TAG + timedelta(hours=19, minutes=30)

    assert v.niederschlag_naechste_stunden(6, jetzt) == 15.0
    assert v.max_regen_wahrscheinlichkeit_naechste_stunden(6, jetzt) == 95.0


def test_fenster_beginnt_bei_der_naechsten_vollen_stunde():
    """`> jetzt`: die angebrochene Stunde faellt heraus.

    Bewusst die sichere Richtung -- zu wenig gesehener Regen laesst giessen
    (billig), zu viel gesehener sperrt den Guss (Trockenstress, teuer).
    """
    v = _vorhersage()
    jetzt = TAG + timedelta(hours=8, minutes=50)

    fenster = v.zukunftsstunden(6, jetzt)
    assert fenster[0].zeitstempel == TAG + timedelta(hours=9)
    assert len(fenster) == 6
    assert fenster[-1].zeitstempel == TAG + timedelta(hours=14)


# --------------------------------------------------------------------------
# Die 24-h-Variante aendert ihre Bedeutung -- Decay-Pfad
# --------------------------------------------------------------------------

def test_24h_variante_reicht_jetzt_in_den_folgetag():
    """Semantik-Wechsel, den der Fix mitbringt.

    Alt war `stunden[:24]` der heutige KALENDERTAG: um 19:30 also 19 Stunden
    Vergangenheit und 5 Stunden Zukunft. Neu sind es echte naechste 24 h, die
    ueber Mitternacht reichen. Das ist die Groesse, die der Decay-Pfad
    (`entscheidung.py:1922-1925`) braucht -- er fragt, wie stark die Zone in
    den kommenden 24 h austrocknet.
    """
    v = _vorhersage(et0=0.5)
    jetzt = TAG + timedelta(hours=19, minutes=30)

    # Zuerst ueber die oeffentliche Funktion pruefen, damit dieser Test auch
    # gegen den ALTEN Code an einer Zusicherung scheitert und nicht schon am
    # fehlenden Helfer -- sonst belegt er das Verhalten nicht, nur die neue
    # Signatur. Alt waeren es 24 Kalendertagsstunden gewesen: ebenfalls 12.0,
    # aber ueberwiegend aus der Vergangenheit. Deshalb zaehlt hier die
    # Fensterlage, nicht die Summe.
    assert v.et0_naechste_stunden(24, jetzt) == 12.0
    assert v.et0_naechste_stunden(6, jetzt) == 3.0

    fenster = v.zukunftsstunden(24, jetzt)
    assert len(fenster) == 24
    assert fenster[0].zeitstempel == TAG + timedelta(hours=20)
    assert fenster[-1].zeitstempel == TAG + timedelta(hours=43)   # Folgetag 19:00


def test_et0_fenster_liegt_in_der_zukunft_nicht_in_der_nacht():
    """ET0 war der Beleg, der keine Auslegung zuliess.

    Live gemessen 05.08. 08:50: die Engine las `et0_6h` = 0.0 mm, weil sie die
    Nacht las -- nachts verdunstet nichts. Die echten naechsten 6 h lagen bei
    1.66 mm. Hier nachgebaut: ET0 nur tagsueber, Abfrage am Vormittag.

    Bewusst nur ueber die oeffentliche Funktion, damit die Gegenprobe gegen den
    alten Code an einer Zusicherung scheitert.
    """
    stunden = [
        WetterStunde(
            zeitstempel=TAG + timedelta(hours=i),
            temperatur=20.0,
            et0_mm=0.0 if i < 8 else 0.4,
        )
        for i in range(48)
    ]
    v = WetterVorhersage(abfrage_zeitstempel=TAG, stunden=stunden)
    jetzt = TAG + timedelta(hours=8, minutes=50)

    # Alt: `stunden[:6]` = 00:00-05:00 = reine Nacht = 0.0 mm.
    assert v.et0_naechste_stunden(6, jetzt) == pytest.approx(2.4)


def test_24h_decay_zaehlt_keinen_vergangenen_regen_mehr():
    """Regen von heute Morgen darf den Decay von morgen nicht bremsen.

    Der Decay rechnet `2.0*et0_24h - 4.0*max(0, regen_24h - 2.0)`. Zaehlte er
    laengst gefallenen Regen mit, prognostizierte er eine zu langsame
    Austrocknung -- die Zone haette zu spaet Wasser bekommen.
    """
    v = _vorhersage(regen_stunden={6, 7, 8})
    jetzt = TAG + timedelta(hours=19, minutes=30)

    assert v.niederschlag_naechste_stunden(24, jetzt) == 0.0


# --------------------------------------------------------------------------
# Robustheit
# --------------------------------------------------------------------------

def test_unsortierte_eingabe_wird_sortiert():
    """Die Reihenfolge der API ist nicht garantiert; `[:n]` verliess sich darauf."""
    v = _vorhersage(regen_stunden={20})
    v.stunden.reverse()
    jetzt = TAG + timedelta(hours=19, minutes=30)

    fenster = v.zukunftsstunden(3, jetzt)
    assert [s.zeitstempel.hour for s in fenster] == [20, 21, 22]
    assert v.niederschlag_naechste_stunden(3, jetzt) == 5.0


def test_keine_zukunftsstunden_liefert_nullen_statt_absturz():
    """Veraltete Vorhersage (Abruf fehlgeschlagen, Cache alt) -> 0, kein Crash.

    Wichtig fuer die Richtung: 0 mm Regen bedeutet "Gate sperrt nicht", die
    Zone bekommt also im Zweifel Wasser. Bei einer veralteten Vorhersage ist
    das die richtige Vorsicht.
    """
    v = _vorhersage(regen_stunden={0, 1, 2})
    jetzt = TAG + timedelta(days=5)

    assert v.zukunftsstunden(6, jetzt) == []
    assert v.niederschlag_naechste_stunden(6, jetzt) == 0.0
    assert v.et0_naechste_stunden(6, jetzt) == 0.0
    assert v.max_regen_wahrscheinlichkeit_naechste_stunden(6, jetzt) == 0.0


def test_weniger_stunden_verfuegbar_als_angefragt():
    """Am Ende des Vorhersage-Horizonts gibt es weniger als N Stunden."""
    v = _vorhersage(et0=1.0)
    jetzt = TAG + timedelta(hours=45)

    assert len(v.zukunftsstunden(6, jetzt)) == 2
    assert v.et0_naechste_stunden(6, jetzt) == 2.0
