

def test_t0516_nur_noch_24h_horizont():
    """T-0516: der 48-h-Horizont ist raus, und das muss so bleiben.

    Zwei Gruende, die zusammen wirken: er hatte nie einen Konsumenten
    (`wasserbilanz_job._ensemble_urteil` filtert hart auf 24), und seit dem
    T-0425-Fix ist er unerfuellbar -- `icon_d2_eps` liefert nur 48 Stunden ab
    Mitternacht, ab `jetzt` bleiben `48 - Abfragestunde` uebrig, die
    Vollstaendigkeitspruefung schlaegt also zu jeder Stunde ausser 00:00 zu.

    Wer ihn zurueckholt, ohne vorher die Modellwahl zu klaeren, baut einen
    Abruf, der nichts schreibt.

    **Praezisiert 13.08.2026 (T-0538):** der Test stand auf
    `HORIZONTE == (24,)` und war damit enger als seine eigene Begruendung --
    die zielt ausschliesslich nach OBEN ("weiter als 24 h braucht ein anderes
    Modell"). Ein zusaetzlicher 6-h-Horizont hat dieses Problem nicht:
    sechs Stunden ab `jetzt` liegen immer im 48-h-Fenster von `icon_d2_eps`,
    auch spaet abends. Der Test prueft jetzt die Aussage statt des Wertes,
    sonst blockiert er jede legitime Erweiterung nach unten.
    """
    from bewaesserung.regen_ensemble_job import HORIZONTE

    assert HORIZONTE, "mindestens ein Horizont erwartet"
    assert max(HORIZONTE) <= 24, (
        f"HORIZONTE ist {HORIZONTE} -- ein Horizont ueber 24 h braucht ein "
        "Modell, das weiter reicht als icon_d2_eps (siehe T-0516)"
    )
    assert 24 in HORIZONTE, "der 24-h-Horizont ist der Konsument der Bilanz"


def test_t0538_sechs_stunden_horizont_wird_gesammelt():
    """T-0538: ohne 6-h-Reihe laesst sich die Sperr-Regel nicht umstellen.

    Die heutige Regel liest einen 6-h-Punktwert und kippte damit ueber 1.304
    Abfragepaare 24-mal das Urteil. Der Ersatz waere der p20 der
    Member-Summen ueber dasselbe Fenster -- den kann aber niemand rechnen,
    solange der Job dieses Fenster gar nicht sammelt.
    """
    from bewaesserung.regen_ensemble_job import HORIZONTE

    assert 6 in HORIZONTE
