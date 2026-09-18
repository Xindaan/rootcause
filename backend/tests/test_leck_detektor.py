import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.leck_detektor import (
    BEW_ALARM_FAKTOR,
    BEW_ERWARTET_PRO_SEK,
    LeckDetektor,
    WirkungsProfil,
    _wirkungs_paar_gleiches_geraet,
)
from bewaesserung.modelle import (
    Ausloser,
    SensorMessung,
    SensorWarnungTyp,
    VentilAktion,
    VentilEreignis,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


JULI = datetime(2026, 7, 15, 8, 0)  # Saison aktiv
FEBRUAR = datetime(2026, 2, 15, 8, 0)  # ausserhalb Saison


def _messung(zeitstempel: datetime, feuchte: float, zone_id: str = "bambus") -> SensorMessung:
    return SensorMessung(
        zeitstempel=zeitstempel,
        zone_id=zone_id,
        boden_feuchte=feuchte,
        boden_temperatur=15.0,
        batterie_prozent=95.0,
    )


async def _fuelle_bewaesserung(
    speicher: Speicher,
    *,
    zone_id: str = "bambus",
    schliessen_zeit: datetime,
    dauer_sekunden: int,
    feuchte_vor: float,
    feuchte_nach: float,
    ausloser: Ausloser = Ausloser.AUTOMATIK,
) -> None:
    oeffnen_zeit = schliessen_zeit - timedelta(seconds=dauer_sekunden)
    # Messungen vor + nach der Bewaesserung
    await speicher.speichere_messung(_messung(oeffnen_zeit - timedelta(minutes=10), feuchte_vor, zone_id))
    await speicher.speichere_messung(_messung(oeffnen_zeit - timedelta(minutes=1), feuchte_vor, zone_id))
    # Bewaesserungs-Events
    await speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=oeffnen_zeit, zone_id=zone_id, ventil_id="v1",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0, ausloser=ausloser,
    ))
    await speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=schliessen_zeit, zone_id=zone_id, ventil_id="v1",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=dauer_sekunden,
        ausloser=ausloser,
    ))
    # Messung "nach" Auswertezeit — typischerweise 30min spaeter
    await speicher.speichere_messung(_messung(
        schliessen_zeit + timedelta(minutes=25), feuchte_nach, zone_id,
    ))


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "leck.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def test_bewaesserung_ohne_wirkung_loest_warnung_aus(speicher):
    # T-0428: 300s -> 3600s und Delta 0.3 -> 0.0.
    # Der alte Fixture-Lauf (300 s -> 6 pp Erwartung) liegt unter der
    # Sensor-Aufloesung und darf seit T-0428 KEINEN Alarm mehr ausloesen.
    # Ausserdem ist ein Delta von 0.3 pp auf einem 5-pp-Raster physikalisch
    # unmoeglich -- der Sensor zeigt 0 oder 5. Eine echte Stunde
    # Bewaesserung ohne jede Reaktion ist der Fall, den der Detektor
    # finden SOLL.
    schliessen = JULI
    _run(_fuelle_bewaesserung(
        speicher, schliessen_zeit=schliessen - timedelta(minutes=30),
        dauer_sekunden=3600, feuchte_vor=35.0, feuchte_nach=35.0,
    ))

    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=schliessen))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    typen = {w.typ for w in offen}
    assert SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG in typen


def test_sichtbarer_feuchte_anstieg_loest_keine_warnung_aus(speicher):
    schliessen = JULI
    # Erwartet: 300 * 0.02 = 6 %, Alarm < 1.8. Realer Anstieg: 4 % -> OK
    _run(_fuelle_bewaesserung(
        speicher, schliessen_zeit=schliessen - timedelta(minutes=30),
        dauer_sekunden=300, feuchte_vor=30.0, feuchte_nach=34.0,
    ))

    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=schliessen))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert all(w.typ != SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG for w in offen)


def test_wirkungslos_warnung_schliesst_sich_wieder(speicher):
    # Erste Pruefung: kein Anstieg -> Warnung
    schliessen = JULI
    # T-0428: 300s -> 3600s, Delta auf 0.0 (5-pp-Raster, s.o.).
    _run(_fuelle_bewaesserung(
        speicher, schliessen_zeit=schliessen - timedelta(minutes=30),
        dauer_sekunden=3600, feuchte_vor=35.0, feuchte_nach=35.0,
    ))
    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=schliessen))
    assert any(
        w.typ == SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG
        for w in _run(speicher.offene_sensor_warnungen("bambus"))
    )

    # Neue Bewaesserung mit echtem Anstieg -> Warnung geht zu
    spaeter = schliessen + timedelta(hours=2)
    _run(_fuelle_bewaesserung(
        speicher, schliessen_zeit=spaeter - timedelta(minutes=30),
        dauer_sekunden=3600, feuchte_vor=30.0, feuchte_nach=55.0,
    ))
    _run(detektor.pruefe_alle(["bambus"], jetzt=spaeter))
    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert all(w.typ != SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG for w in offen)


def test_t0185_phantom_kandidat_schliesst_offene_warnung(speicher):
    """T-0185: Wenn der einzige SCHLIESSEN-Kandidat im Auswerte-Fenster
    `ausloser=IGNORIERT` ist (User hat ihn nachtraeglich als Phantom
    markiert), darf die offene `bewaesserung_ohne_wirkung`-Warnung
    nicht haengen bleiben - sie war eh unbegruendet.
    Realfall 12.05.2026: Sensor-Doppelung -> Heuristik-Phantom ->
    LeckDetektor schrieb Warnung -> User klassifizierte als IGNORIERT ->
    Warnung blieb 24h+ haengen.
    """
    schliessen = JULI
    # Schritt 1: Heuristik schreibt Event, LeckDetektor sieht "keine
    # Wirkung" -> Warnung gesetzt
    _run(_fuelle_bewaesserung(
        speicher, schliessen_zeit=schliessen - timedelta(minutes=30),
        dauer_sekunden=3600, feuchte_vor=45.0, feuchte_nach=45.0,
        ausloser=Ausloser.UNBEKANNT,
    ))
    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=schliessen))
    # Bei UNBEKANNT noch keine Auto-Close -> Warnung muss offen sein
    offen_vor = _run(speicher.offene_sensor_warnungen("bambus"))
    assert any(
        w.typ == SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG for w in offen_vor
    )

    # Schritt 2: User klassifiziert Event als IGNORIERT (Phantom-Markierung).
    # Wir simulieren das, indem wir die Events neu schreiben.
    import asyncio
    async def umklassifizieren():
        events = await speicher.hole_ventil_ereignisse(
            "bambus",
            von=schliessen - timedelta(hours=1),
            bis=schliessen,
        )
        for e in events:
            if e.id is not None:
                await speicher.aktualisiere_ventil_ereignis(
                    e.id, ausloser=Ausloser.IGNORIERT,
                )
    asyncio.run(umklassifizieren())

    # Schritt 3: LeckDetektor laeuft erneut -> findet nur IGNORIERT-
    # Kandidat -> schliesst offene Warnung
    _run(detektor.pruefe_alle(["bambus"], jetzt=schliessen + timedelta(minutes=1)))
    offen_nach = _run(speicher.offene_sensor_warnungen("bambus"))
    assert all(
        w.typ != SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG for w in offen_nach
    ), "Phantom-Kandidat muss offene Warnung schliessen"


def test_t0297_gealterter_ignoriert_kandidat_schliesst_verwaiste_warnung(speicher):
    """T-0297: Realfall waldblumenhain. Eine offene "ohne Wirkung"-Warnung
    wurde aus einem UNBEKANNT-Event geoeffnet; spaeter flippt der User das
    Event auf IGNORIERT, ABER das Event ist da schon aus dem
    [START,ENDE]-Fenster (und sogar aus dem 4h-Re-Check-Fenster) gealtert.
    Vorher: weder Haupt-Pfad (kein Kandidat in [20,90min]) noch
    _re_check_haengende_warnung (nur automatik/manuell) schloss die
    Warnung -> sie hing tagelang. Fix: re_check schliesst, wenn KEIN
    rechtfertigendes SCHLIESSEN (non-ignoriert) im 4h-Fenster mehr liegt.
    """
    schliessen = JULI
    _run(_fuelle_bewaesserung(
        speicher, schliessen_zeit=schliessen - timedelta(minutes=30),
        dauer_sekunden=1080, feuchte_vor=56.0, feuchte_nach=56.0,
        ausloser=Ausloser.UNBEKANNT,
    ))
    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=schliessen))
    assert any(
        w.typ == SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG
        for w in _run(speicher.offene_sensor_warnungen("bambus"))
    ), "Warnung muss bei UNBEKANNT zunaechst offen sein"

    # User flippt das Event auf IGNORIERT.
    async def _flip():
        for e in await speicher.hole_ventil_ereignisse(
            "bambus", von=schliessen - timedelta(hours=1), bis=schliessen,
        ):
            if e.id is not None:
                await speicher.aktualisiere_ventil_ereignis(
                    e.id, ausloser=Ausloser.IGNORIERT,
                )
    _run(_flip())

    # Detektor laeuft erst 5 h spaeter -> Event ist aus dem 4h-Fenster
    # gealtert (kein Kandidat irgendeiner Art mehr).
    spaet = schliessen + timedelta(hours=5)
    _run(detektor.pruefe_alle(["bambus"], jetzt=spaet))
    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert all(
        w.typ != SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG for w in offen
    ), "Verwaiste Warnung (gealtertes/ignoriertes Event) muss schliessen"


def test_t0297_unbekannter_kandidat_im_fenster_haelt_warnung_offen(speicher):
    """T-0297-Gegentest: ein noch UNKLASSIFIZIERTES (unbekannt) SCHLIESSEN
    im 4h-Fenster ist 'rechtfertigend' -> die Warnung darf NICHT vorzeitig
    geschlossen werden (User koennte es noch als echten Lauf bestaetigen).
    """
    schliessen = JULI
    # Event bei jetzt-120min: ausserhalb [20,90min]-Hauptfenster, aber im
    # 4h-Re-Check-Fenster; ausloser UNBEKANNT (nicht automatik/manuell).
    jetzt = schliessen
    _run(_fuelle_bewaesserung(
        speicher, schliessen_zeit=jetzt - timedelta(minutes=30),
        dauer_sekunden=1080, feuchte_vor=56.0, feuchte_nach=56.0,
        ausloser=Ausloser.UNBEKANNT,
    ))
    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=jetzt))
    assert any(
        w.typ == SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG
        for w in _run(speicher.offene_sensor_warnungen("bambus"))
    )
    # 2 h spaeter: Event jetzt ~150min alt (aus [20,90], aber in (90,240]),
    # immer noch UNBEKANNT -> rechtfertigend -> Warnung bleibt offen.
    spaeter = jetzt + timedelta(hours=2)
    _run(detektor.pruefe_alle(["bambus"], jetzt=spaeter))
    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert any(
        w.typ == SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG for w in offen
    ), "Unklassifiziertes Event im Fenster darf Warnung nicht schliessen"


def test_sensor_eingefroren_bei_konstantem_wert(speicher):
    # 12 Messungen ueber 48h, Spanne 0.3 %
    basis = JULI - timedelta(hours=48)
    for i in range(12):
        _run(speicher.speichere_messung(_messung(
            basis + timedelta(hours=i * 4), 42.0 + (0.1 if i % 3 else 0.0),
        )))

    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=JULI))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert any(w.typ == SensorWarnungTyp.SENSOR_EINGEFROREN for w in offen)


def test_sensor_eingefroren_ignoriert_zu_wenig_messungen(speicher):
    # nur 3 Messungen ueber 48h -> kein Urteil
    basis = JULI - timedelta(hours=30)
    for i in range(3):
        _run(speicher.speichere_messung(_messung(
            basis + timedelta(hours=i * 5), 42.0,
        )))

    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=JULI))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert offen == []


def _messung_geraet(t: datetime, feuchte: float, geraet_id: str) -> SensorMessung:
    return SensorMessung(
        zeitstempel=t, zone_id="bambus", geraet_id=geraet_id,
        boden_feuchte=feuchte, boden_temperatur=15.0, batterie_prozent=95.0,
    )


def test_t0400_wirkungs_paar_nimmt_gleiches_geraet(speicher):
    """T-0400: `vor` und `nach` muessen vom selben Sensor stammen. Bei zwei
    Geraeten mit unvergleichbaren Skalen (Gardena-Index vs FYTA-VWC) darf der
    Wirkungs-Check nicht Gardena-vor mit FYTA-nach verrechnen."""
    def _m(t_offset_min, feuchte, geraet):
        return _messung_geraet(
            JULI + timedelta(minutes=t_offset_min), feuchte, geraet)
    # Gardena: vor 40 -> nach 60 (Wirkung 20). FYTA daneben auf anderer Skala.
    vor = [_m(-30, 40.0, "gardena"), _m(-25, 11.0, "fyta")]
    nach = [_m(90, 12.0, "fyta"), _m(95, 60.0, "gardena")]  # gardena juenger
    v, n, _g = _wirkungs_paar_gleiches_geraet(vor, nach)
    assert (v, n) == (40.0, 60.0), "vor/nach muessen beide vom Gardena kommen"


def test_t0400_wirkungs_paar_single_sensor_unveraendert(speicher):
    """Single-Sensor-Zone: identisch zum alten letzter-gueltiger-Wert-Verhalten."""
    def _m(t_offset_min, feuchte):
        return _messung_geraet(
            JULI + timedelta(minutes=t_offset_min), feuchte, "gardena")
    v, n, _g = _wirkungs_paar_gleiches_geraet(
        [_m(-30, 42.0), _m(-25, 43.0)], [_m(90, 55.0), _m(95, 58.0)])
    assert (v, n) == (43.0, 58.0)


def test_t0400_wirkungs_paar_kein_gemeinsames_geraet(speicher):
    """Kein Geraet mit vor UND nach -> (None, None), kein Muell-Delta."""
    def _m(t_offset_min, feuchte, geraet):
        return _messung_geraet(
            JULI + timedelta(minutes=t_offset_min), feuchte, geraet)
    v, n, _g = _wirkungs_paar_gleiches_geraet(
        [_m(-30, 40.0, "gardena")], [_m(90, 20.0, "fyta")])
    assert (v, n) == (None, None)


def test_sensor_eingefroren_pro_geraet_nicht_zone_gemischt(speicher):
    """T-0391: Ein eingefrorener Sensor darf nicht von der Varianz eines
    gesunden Nachbar-Sensors derselben Zone maskiert werden.

    Vorher rechnete der Detektor `max-min` ueber die ZONEN-GEMISCHTEN Werte ->
    der variierende Nachbar blies die Spanne auf -> keine Warnung. Kritisch fuer
    Zonen mit `aggregat_lead_geraet` (hecke): dort traegt genau EIN Sensor die
    Bewaesserungs-Entscheidung, und der Auto-Loop giesst auf einem toten Wert.
    """
    basis = JULI - timedelta(hours=48)
    for i in range(12):
        t = basis + timedelta(hours=i * 4)
        # Sensor A: eingefroren (Spanne 0.0)
        _run(speicher.speichere_messung(_messung_geraet(t, 42.0, "gardena-a")))
        # Sensor B: gesund, variiert stark -- haette A frueher maskiert
        _run(speicher.speichere_messung(
            _messung_geraet(t, 30.0 + i * 2.0, "fyta-b")))

    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=JULI))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    eingefroren = [
        w for w in offen if w.typ == SensorWarnungTyp.SENSOR_EINGEFROREN
    ]
    assert len(eingefroren) == 1, "Eingefrorener Sensor darf nicht maskiert werden"
    assert "gardena-a" in eingefroren[0].details
    assert "fyta-b" not in eingefroren[0].details


def test_t0529_zwei_eingefrorene_geben_zwei_warnungen(speicher):
    """T-0529: eine Warnung je Geraet, nicht eine je Zone."""
    basis = JULI - timedelta(hours=48)
    for i in range(12):
        t = basis + timedelta(hours=i * 4)
        _run(speicher.speichere_messung(_messung_geraet(t, 42.0, "gardena-a")))
        _run(speicher.speichere_messung(_messung_geraet(t, 55.0, "gardena-b")))

    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=JULI))

    offen = [w for w in _run(speicher.offene_sensor_warnungen("bambus"))
             if w.typ == SensorWarnungTyp.SENSOR_EINGEFROREN]
    assert len(offen) == 2, f"je Geraet eine Warnung erwartet: {offen}"
    assert {w.geraet_id for w in offen} == {"gardena-a", "gardena-b"}


def test_t0529_auftauender_sensor_laesst_den_anderen_stehen(speicher):
    """Der eigentliche Grund fuer T-0529: der Lebenszyklus.

    Solange beide Sensoren in EINER zonenweiten Warnung steckten, galt sie
    als behoben, sobald einer wieder variierte -- der zweite blieb
    eingefroren und niemand sah es mehr. Gleiche Klasse wie
    [[fehlerpattern_dedup_pro_zone_multisensor]].
    """
    basis = JULI - timedelta(hours=48)
    for i in range(12):
        t = basis + timedelta(hours=i * 4)
        _run(speicher.speichere_messung(_messung_geraet(t, 42.0, "gardena-a")))
        _run(speicher.speichere_messung(_messung_geraet(t, 55.0, "gardena-b")))

    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=JULI))
    assert len([w for w in _run(speicher.offene_sensor_warnungen("bambus"))
                if w.typ == SensorWarnungTyp.SENSOR_EINGEFROREN]) == 2

    # Sensor A taut auf: neue Messungen mit Varianz, B bleibt konstant.
    spaeter = JULI + timedelta(hours=48)
    for i in range(12):
        t = JULI + timedelta(hours=i * 4)
        _run(speicher.speichere_messung(
            _messung_geraet(t, 30.0 + i * 3.0, "gardena-a")))
        _run(speicher.speichere_messung(_messung_geraet(t, 55.0, "gardena-b")))

    _run(detektor.pruefe_alle(["bambus"], jetzt=spaeter))

    offen = [w for w in _run(speicher.offene_sensor_warnungen("bambus"))
             if w.typ == SensorWarnungTyp.SENSOR_EINGEFROREN]
    assert len(offen) == 1, f"nur der aufgetaute darf verschwinden: {offen}"
    assert offen[0].geraet_id == "gardena-b"


def test_sensor_eingefroren_keine_warnung_wenn_alle_geraete_variieren(speicher):
    """T-0391 Gegenprobe: variieren ALLE Sensoren, entsteht keine Warnung."""
    basis = JULI - timedelta(hours=48)
    for i in range(12):
        t = basis + timedelta(hours=i * 4)
        _run(speicher.speichere_messung(
            _messung_geraet(t, 30.0 + i * 1.5, "gardena-a")))
        _run(speicher.speichere_messung(
            _messung_geraet(t, 35.0 + i * 1.5, "fyta-b")))

    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=JULI))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert not [
        w for w in offen if w.typ == SensorWarnungTyp.SENSOR_EINGEFROREN
    ]


def test_ausserhalb_saison_wird_nichts_gemeldet(speicher):
    # Ideal-Bedingung fuer "ohne Wirkung" + "eingefroren", aber Februar -> kein Alarm
    schliessen = FEBRUAR
    _run(_fuelle_bewaesserung(
        speicher, schliessen_zeit=schliessen - timedelta(minutes=30),
        dauer_sekunden=300, feuchte_vor=30.0, feuchte_nach=30.1,
    ))
    for i in range(12):
        _run(speicher.speichere_messung(_messung(
            FEBRUAR - timedelta(hours=48) + timedelta(hours=i * 4), 42.0,
        )))

    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=FEBRUAR))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert offen == []


def test_konstanten_spanne_liefert_korrekte_erwartungswerte():
    # Dokumentiert die Rechnung, damit ein Tweak bewusst erfolgen muss
    dauer = 300
    erwartet = dauer * BEW_ERWARTET_PRO_SEK
    schwelle = erwartet * BEW_ALARM_FAKTOR
    assert erwartet == 6.0
    assert schwelle == pytest.approx(1.8)


# --- T-0197: Pro-Zone-Wirkungs-Profil --------------------------------


def test_t0197_plateau_modell_saturiert_bei_45min():
    """Bambus-Plateau (wmax=8, r0=0.5) liefert bei 45 min ~7.5 pp,
    nicht linear 22 pp wie der globale Default es taete.
    """
    profil = WirkungsProfil(
        wirkung_max_pp=8.0,
        wirkungsrate_initial=0.5,
        log_decay_alpha=-0.6,  # wird beim Plateau-Pfad ignoriert
    )
    erwartet = profil.erwartete_wirkung_pp(2696)  # 45 min
    # total = 8 * (1 - exp(-45/16)) = 8 * 0.940 = ~7.52
    assert erwartet == pytest.approx(7.52, abs=0.05)


def test_t0197_plateau_modell_extrem_lange_dauer_bleibt_unter_wmax():
    """Plateau saturiert asymptotisch — bei 6 h Dauer keine absurden
    Erwartungen. Global-Linear haette 432 pp erwartet.
    """
    profil = WirkungsProfil(wirkung_max_pp=8.0, wirkungsrate_initial=0.5)
    erwartet = profil.erwartete_wirkung_pp(21600)  # 360 min = 6 h
    assert erwartet <= 8.0
    assert erwartet >= 7.9  # praktisch wmax


def test_t0197_lineares_modell_ohne_decay():
    """Lineare Rate ohne Decay (alpha=0) skaliert ueber die Zeit."""
    profil = WirkungsProfil(delta_pp_pro_minute=0.4)
    assert profil.erwartete_wirkung_pp(60) == pytest.approx(0.4)  # 1 min
    assert profil.erwartete_wirkung_pp(3600) == pytest.approx(24.0)  # 60 min


def test_t0197_lineares_modell_mit_log_decay():
    """alpha<0 bremst lange Dosen ab (T-0091a Tiefensickerungs-Modell)."""
    profil = WirkungsProfil(delta_pp_pro_minute=0.4, log_decay_alpha=-0.6)
    # 90 min: rate(90) = 0.4 * (1 + (-0.6) * log(90/30)) = 0.4 * (1 - 0.66) = 0.137
    # erwartet = 0.137 * 90 = ~12.3 pp (statt linearer 36 pp)
    erwartet_90 = profil.erwartete_wirkung_pp(5400)
    assert erwartet_90 < 0.4 * 90  # weniger als linear
    assert erwartet_90 > 0.05 * 90  # mehr als Untergrenze


def test_t0197_lineares_modell_floor_verhindert_negative_rate():
    """Sehr lange Dauer + starker Decay -> Rate koennte negativ werden;
    Sanity-Floor 0.05 pp/min haelt das ab.
    """
    profil = WirkungsProfil(delta_pp_pro_minute=0.4, log_decay_alpha=-2.0)
    # Bei 600 min waere korrigierte Rate negativ -> Floor 0.05 pro min
    erwartet = profil.erwartete_wirkung_pp(36000)  # 600 min
    assert erwartet == pytest.approx(0.05 * 600)


def test_t0197_default_profil_nutzt_globale_konstante():
    """Leeres Profil fallt zurueck auf BEW_ERWARTET_PRO_SEK (Backward-Compat)."""
    profil = WirkungsProfil()
    dauer = 300
    erwartet = profil.erwartete_wirkung_pp(dauer)
    assert erwartet == pytest.approx(dauer * BEW_ERWARTET_PRO_SEK)


def test_t0197_plateau_modell_keine_phantom_warnung_bambus_regression(speicher):
    """Regression auf reale Bambuswald-Daten 14.05.: 45 min Bewaesserung,
    Sensor 60% -> 70% (+10 pp). Vor T-0197 loeste der LeckDetektor hier
    eine "Bewaesserung ohne Wirkung" aus (Schwelle 16.2 pp). Mit Plateau-
    Profil sollte +10 pp >> Plateau-Erwartung 7.5 pp sein -> keine Warnung.
    """
    _run(_fuelle_bewaesserung(
        speicher,
        zone_id="bambus",
        schliessen_zeit=JULI,
        dauer_sekunden=2696,
        feuchte_vor=60.0,
        feuchte_nach=70.0,
    ))
    detektor = LeckDetektor(
        speicher,
        wirkungs_profile={
            "bambus": WirkungsProfil(
                wirkung_max_pp=8.0,
                wirkungsrate_initial=0.5,
                log_decay_alpha=-0.6,
            ),
        },
    )
    _run(detektor.pruefe_alle(["bambus"], jetzt=JULI + timedelta(minutes=30)))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert offen == []


def test_t0197_default_pfad_bestehendes_verhalten(speicher):
    """Negativ-Kontrolle: Zone ohne Wirkungs-Profil bekommt den globalen
    Default-Pfad (BEW_ERWARTET_PRO_SEK).

    T-0428: Fixture von 300 s auf 3600 s angehoben. 300 s ergeben im
    Default-Pfad 6 pp Erwartung -- das liegt unter der Gardena-Aufloesung
    (5 pp x Sicherheitsfaktor = 10 pp) und darf seit T-0428 keinen Alarm
    mehr ausloesen. Mit 3600 s sind es 72 pp Erwartung, Alarmgrenze 21,6 pp;
    ein Anstieg von 0 pp nach einer vollen Stunde ist der Fall, den der
    Detektor finden soll. Delta ausserdem auf 0.0 statt 0.5 -- ein
    Gardena-Sensor kann 0,5 pp gar nicht anzeigen.
    """
    _run(_fuelle_bewaesserung(
        speicher,
        zone_id="bambus",
        schliessen_zeit=JULI,
        dauer_sekunden=3600,
        feuchte_vor=50.0,
        feuchte_nach=50.0,  # keine Reaktion trotz voller Stunde
    ))
    # Kein wirkungs_profile -> Default-Konstante
    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=JULI + timedelta(minutes=30)))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert len(offen) == 1
    assert offen[0].typ == SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG


# ---------------------------------------------------------------------------
# T-0213: Hard-Floor + Min-Dauer + ml_ausschluss_fenster respektieren
# ---------------------------------------------------------------------------


def test_t0213_kurzer_testlauf_loest_keine_warnung_aus(speicher):
    """Realfall 19.05.2026: User 58 s-Testlauf in der Gardena-App, kein
    Sensor-Anstieg. Vor T-0213 loeste der Detektor "ohne Wirkung" aus,
    weil 58 s > altes Minimum 30 s war. Mit `BEW_MIN_DAUER_SEKUNDEN=240`
    wird der Kandidat gar nicht erst bewertet -> keine Warnung.
    """
    _run(_fuelle_bewaesserung(
        speicher,
        zone_id="bambus",
        schliessen_zeit=JULI,
        dauer_sekunden=58,
        feuchte_vor=70.0, feuchte_nach=70.0,
        ausloser=Ausloser.MANUELL,
    ))
    detektor = LeckDetektor(speicher)
    _run(detektor.pruefe_alle(["bambus"], jetzt=JULI + timedelta(minutes=30)))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert offen == []


def test_t0213_hard_floor_unterdrueckt_alarm_bei_kleiner_erwartung(speicher):
    """Plateau-Modell mit niedrigem wmax: bei 240 s erwartete Wirkung
    < 1.0 pp (Hard-Floor BEW_ERWARTET_MIN_PP). Sensor zeigt erwartungs-
    gemaess kein Delta -> Detektor soll NICHT alarmieren (Werte unter
    Sensor-Aufloesung). Vor T-0213: alarmiert algorithmisch.
    """
    # Plateau wmax=2, r0=0.1 pp/min -> tau=20 min. Bei 240 s (= 4 min)
    # erwartet = 2 * (1 - exp(-4/20)) = 2 * 0.181 = 0.36 pp.
    _run(_fuelle_bewaesserung(
        speicher,
        zone_id="bambus",
        schliessen_zeit=JULI,
        dauer_sekunden=240,
        feuchte_vor=70.0, feuchte_nach=70.0,
    ))
    detektor = LeckDetektor(
        speicher,
        wirkungs_profile={
            "bambus": WirkungsProfil(
                wirkung_max_pp=2.0, wirkungsrate_initial=0.1,
            ),
        },
    )
    _run(detektor.pruefe_alle(["bambus"], jetzt=JULI + timedelta(minutes=30)))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert offen == [], f"erwartet keine Warnung, war {[w.typ for w in offen]}"


def test_t0213_ml_ausschluss_fenster_schliesst_offene_warnung(speicher):
    """Wenn `jetzt` in einem ml_ausschluss_fenster der Zone liegt,
    soll eine alte BEWAESSERUNG_OHNE_WIRKUNG-Warnung geschlossen
    werden (Bodenart-Reset-Phase -> Sensor-Werte volatil, alter
    Alarm war Phantom). Heuristik analog T-0211a fuer den Detektor.
    """
    # Erst Phantom-Warnung erzeugen (mit kurzem Lauf VOR T-0213-Fix
    # hatte sich das gehaeuft) -- manuell direkt setzen.
    from bewaesserung.modelle import SensorWarnung
    jetzt = JULI
    _run(speicher.oeffne_sensor_warnung(SensorWarnung(
        zeitstempel=jetzt - timedelta(hours=1),
        zone_id="bambus",
        typ=SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG,
        details="alt-phantom",
    )))

    fenster_pro_zone = {
        "bambus": [(jetzt - timedelta(hours=12), jetzt + timedelta(hours=12), None)],
    }
    detektor = LeckDetektor(
        speicher,
        ausschluss_fenster_pro_zone=fenster_pro_zone,
    )
    _run(detektor.pruefe_alle(["bambus"], jetzt=jetzt))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert offen == [], f"Warnung sollte im Fenster geschlossen werden, war {offen}"


def test_t0213_ml_ausschluss_fenster_unterdrueckt_sensor_eingefroren(speicher):
    """Hecke-Realfall 19.05.: neuer Sensor liefert 0% konstant
    (Einschwing-Phase). Ohne T-0213 wuerde SENSOR_EINGEFROREN
    feuern. Mit ml_ausschluss_fenster bleibt der Detektor stumm
    + schliesst alte Warnungen.
    """
    # 48h konstante Werte
    jetzt = JULI
    for h in range(48, 0, -1):
        _run(speicher.speichere_messung(_messung(
            jetzt - timedelta(hours=h), 0.0, "hecke",
        )))

    fenster_pro_zone = {
        "hecke": [(jetzt - timedelta(days=2), jetzt + timedelta(days=2), None)],
    }
    detektor = LeckDetektor(
        speicher,
        ausschluss_fenster_pro_zone=fenster_pro_zone,
    )
    _run(detektor.pruefe_alle(["hecke"], jetzt=jetzt))

    offen = _run(speicher.offene_sensor_warnungen("hecke"))
    assert offen == []


def test_t0213_ausserhalb_ausschluss_funktioniert_normal(speicher):
    """Negativ-Kontrolle: ausserhalb des Ausschluss-Fensters laeuft
    der Detektor normal (sonst wuerde der Fix den ganzen Detektor
    deaktivieren).
    """
    schliessen = JULI
    _run(_fuelle_bewaesserung(
        speicher, zone_id="bambus",
        schliessen_zeit=schliessen - timedelta(minutes=30),
        dauer_sekunden=600,        # 10 min, > Min-Dauer
        feuchte_vor=35.0,
        feuchte_nach=35.3,         # erwartet > 1.0 pp, real 0.3
    ))
    # Fenster liegt 2 Tage in der Vergangenheit -> jetzt ausserhalb.
    fenster_pro_zone = {
        "bambus": [(JULI - timedelta(days=5), JULI - timedelta(days=2), None)],
    }
    detektor = LeckDetektor(
        speicher,
        ausschluss_fenster_pro_zone=fenster_pro_zone,
    )
    _run(detektor.pruefe_alle(["bambus"], jetzt=schliessen))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    typen = {w.typ for w in offen}
    assert SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG in typen


# ---------------------------------------------------------------------------
# T-0251: Pro-Zone-Detektor-Fenster + aktiver Re-Check fuer langsame Substrate
# ---------------------------------------------------------------------------


def test_t0251_pro_zone_fenster_180min(speicher):
    """Langsame Substrate (Bambus): Sensor-Sprung kommt 80 min nach
    SCHLIESSEN. Mit Default-90-min-Fenster wuerde Detektor 25 min nach
    SCHLIESSEN noch 0 delta sehen und Alarm setzen. Mit 180-min-Fenster
    fuer die Zone sieht der Detektor den Sprung erst nach 80 min, dann
    delta > schwelle -> KEIN Alarm.
    """
    schliessen = JULI
    # Sprung-Messung kommt 80 min spaeter (statt typisch 25 min)
    _run(_fuelle_bewaesserung(
        speicher, schliessen_zeit=schliessen,
        dauer_sekunden=1800, feuchte_vor=60.0, feuchte_nach=60.0,
    ))
    # Spaete Sprung-Messung manuell ergaenzen
    _run(speicher.speichere_messung(_messung(
        schliessen + timedelta(minutes=80), 67.0, "bambus",
    )))
    # Pro-Zone Fenster 180 min + Plateau-Profil (Bambus-realistisch).
    # erwartet(1800s, wmax=8, r0=0.5, tau=16): 8 * (1 - exp(-30/16))
    # ~= 6.8pp, schwelle 2.0pp; delta=7 pp > schwelle -> kein Alarm.
    detektor = LeckDetektor(
        speicher,
        fenster_ende_min_pro_zone={"bambus": 180},
        wirkungs_profile={"bambus": WirkungsProfil(
            wirkung_max_pp=8.0, wirkungsrate_initial=0.5,
        )},
    )
    jetzt_eval = schliessen + timedelta(minutes=100)
    _run(detektor.pruefe_alle(["bambus"], jetzt=jetzt_eval))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert offen == [], (
        f"Mit 180-min-Fenster sollte der spaete Sprung erkannt werden, "
        f"war {[w.details for w in offen]}"
    )


def test_t0251_default_fenster_alarmiert_weiter_bei_echtem_defekt(speicher):
    """Negativ-Kontrolle: ohne Pro-Zone-Override gilt das 90-min-
    Default-Fenster. Wenn auch nach 25 min noch 0 delta -> Alarm
    (T-0213-Verhalten bleibt unveraendert).
    """
    schliessen = JULI
    _run(_fuelle_bewaesserung(
        speicher, schliessen_zeit=schliessen - timedelta(minutes=30),
        dauer_sekunden=1800,
        feuchte_vor=35.0, feuchte_nach=35.0,
    ))
    detektor = LeckDetektor(speicher)  # kein Pro-Zone-Override
    _run(detektor.pruefe_alle(["bambus"], jetzt=schliessen))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    typen = {w.typ for w in offen}
    assert SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG in typen


def test_t0251_aktiver_re_check_schliesst_haengende_warnung(speicher):
    """Wenn eine offene Warnung von einem frueheren Detektor-Lauf
    existiert UND der Sensor-Sprung inzwischen erkennbar ist (im
    erweiterten 4h-Fenster), soll die Warnung beim naechsten Tick
    aktiv geschlossen werden -- auch ohne neuen Lauf.
    """
    from bewaesserung.modelle import SensorWarnung
    schliessen = JULI
    # Lauf + Pre-Sensor-Werte
    _run(_fuelle_bewaesserung(
        speicher, schliessen_zeit=schliessen,
        dauer_sekunden=1800, feuchte_vor=60.0, feuchte_nach=60.0,
    ))
    # Hängende Warnung simulieren (entstanden vom ersten Detektor-Tick
    # 25 min nach Lauf, als noch kein Sprung sichtbar war)
    _run(speicher.oeffne_sensor_warnung(SensorWarnung(
        zeitstempel=schliessen + timedelta(minutes=25),
        zone_id="bambus",
        typ=SensorWarnungTyp.BEWAESSERUNG_OHNE_WIRKUNG,
        details="alte False-Positive (Sensor reagierte spaeter)",
    )))
    # Spaete Sprung-Messung kommt 80 min nach SCHLIESSEN
    _run(speicher.speichere_messung(_messung(
        schliessen + timedelta(minutes=80), 67.0, "bambus",
    )))
    # Naechster Detektor-Tick 120 min nach SCHLIESSEN -- ausserhalb
    # Default-90-Fenster, aber im 4h-Re-Check-Bereich. Plateau-Profil
    # noetig, sonst macht globaler Default-MAE 1800*0.02=36 die Schwelle
    # zu hoch (+7 pp wuerde nicht reichen).
    detektor = LeckDetektor(
        speicher,
        wirkungs_profile={"bambus": WirkungsProfil(
            wirkung_max_pp=8.0, wirkungsrate_initial=0.5,
        )},
    )
    jetzt_eval = schliessen + timedelta(minutes=120)
    _run(detektor.pruefe_alle(["bambus"], jetzt=jetzt_eval))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert offen == [], (
        f"Re-Check sollte die haengende Warnung schliessen, war {offen}"
    )


# ---------------------------------------------------------------------------
# T-0433: Lead-Divergenz -- der Kanal-Ausschluss aendert das Ergebnis
# ---------------------------------------------------------------------------
#
# Hintergrund (Realfall bambuswald, Juli 2026): `kanal_trigger_ausschluss`
# war write-only. Ausserhalb der Trigger-Filterung in entscheidung.py las das
# Feld niemand -- eine ausgeschlossene Zone war damit nicht bloss
# nicht-triggernd, sondern unsichtbar. Die ausgeschlossene Zone fiel 77 -> 30,
# waehrend die Lead-Zone durchgehend "satt" meldete; nichts schlug an.

from bewaesserung.leck_detektor import KanalRolle  # noqa: E402

_TOPOLOGIE = {
    # die stumme Zone: unter kritisch 45, aber vom Trigger ausgeschlossen
    "bambus": KanalRolle(kanal=2, ausgeschlossen=True, feuchte_kritisch=45.0),
    # der Lead am selben Kanal
    "yoga": KanalRolle(kanal=2, ausgeschlossen=False, feuchte_kritisch=50.0),
}


def _detektor_mit_topologie(speicher) -> LeckDetektor:
    return LeckDetektor(speicher, kanal_topologie=_TOPOLOGIE)


def _setze_feuchte(speicher, jetzt, *, bambus: float, yoga: float) -> None:
    _run(speicher.speichere_messung(
        _messung(jetzt - timedelta(minutes=10), bambus, "bambus")))
    _run(speicher.speichere_messung(
        _messung(jetzt - timedelta(minutes=10), yoga, "yoga")))


def test_lead_divergenz_warnt_wenn_stumme_zone_kritisch_und_lead_satt(speicher):
    """Der Fall, der monatelang unsichtbar war: die ausgeschlossene Zone
    bittet um Wasser, der Lead meldet satt -> Kanal giesst nicht."""
    _setze_feuchte(speicher, JULI, bambus=30.0, yoga=70.0)

    _run(_detektor_mit_topologie(speicher).pruefe_alle(["bambus", "yoga"], jetzt=JULI))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    treffer = [w for w in offen if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]
    assert len(treffer) == 1
    # Die Details muessen beide Seiten nennen -- eine Warnung, die nur
    # "Divergenz" sagt, zwingt zum Nachschlagen.
    assert "30" in treffer[0].details and "70" in treffer[0].details
    assert "yoga" in treffer[0].details


def test_lead_divergenz_schweigt_beim_dauerhaften_offset(speicher):
    """KEIN Alarm auf blosse Divergenz.

    Zwischen den beiden Bambus-Sensoren liegen dauerhaft ~20 pp. Waeren das
    schon 20 pp Alarmgrund, feuerte der Waechter im Dauerbetrieb -- und ein
    Waechter, den man wegklickt, ist keiner. Beide ueber ihrer Schwelle =
    der Ausschluss ist folgenlos.
    """
    _setze_feuchte(speicher, JULI, bambus=55.0, yoga=75.0)

    _run(_detektor_mit_topologie(speicher).pruefe_alle(["bambus", "yoga"], jetzt=JULI))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert not [w for w in offen if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]


def test_lead_divergenz_schweigt_wenn_lead_auch_durstig(speicher):
    """Sind beide unter ihrer Schwelle, giesst der Kanal ohnehin -- der
    Ausschluss aendert dann nichts und niemand muss etwas wissen."""
    _setze_feuchte(speicher, JULI, bambus=30.0, yoga=40.0)  # yoga < 50

    _run(_detektor_mit_topologie(speicher).pruefe_alle(["bambus", "yoga"], jetzt=JULI))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert not [w for w in offen if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]


def test_lead_divergenz_schliesst_sich_wenn_zone_sich_erholt(speicher):
    """Erholt sich die stumme Zone, muss die Warnung von selbst zugehen --
    sonst bleibt eine Dauerwarnung stehen und entwertet die naechste."""
    _setze_feuchte(speicher, JULI, bambus=30.0, yoga=70.0)
    det = _detektor_mit_topologie(speicher)
    _run(det.pruefe_alle(["bambus", "yoga"], jetzt=JULI))
    assert [w for w in _run(speicher.offene_sensor_warnungen("bambus"))
            if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]

    spaeter = JULI + timedelta(hours=2)
    _setze_feuchte(speicher, spaeter, bambus=60.0, yoga=70.0)
    _run(det.pruefe_alle(["bambus", "yoga"], jetzt=spaeter))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert not [w for w in offen if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]


def test_lead_divergenz_schweigt_bei_veralteten_daten(speicher):
    """Die Warnung behauptet einen JETZT-Zustand. Auf einem 6 h alten Wert
    waere das eine Behauptung ueber Vergangenes -- bei Sensor-Dropout ist
    die AUSFALL-Warnung zustaendig, nicht diese."""
    alt = JULI - timedelta(hours=6)
    _run(speicher.speichere_messung(_messung(alt, 30.0, "bambus")))
    _run(speicher.speichere_messung(_messung(alt, 70.0, "yoga")))

    _run(_detektor_mit_topologie(speicher).pruefe_alle(["bambus", "yoga"], jetzt=JULI))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert not [w for w in offen if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]


def test_lead_divergenz_ohne_topologie_ist_no_op(speicher):
    """Ohne Kanal-Topologie (Bestands-Setups, Tests) darf der Check nichts
    tun -- kein Verhalten aus dem Nichts."""
    _setze_feuchte(speicher, JULI, bambus=30.0, yoga=70.0)

    _run(LeckDetektor(speicher).pruefe_alle(["bambus", "yoga"], jetzt=JULI))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert not [w for w in offen if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]


def test_lead_divergenz_nur_an_der_stummen_zone(speicher):
    """Die Warnung gehoert an die ausgeschlossene Zone, nicht an den Lead --
    dort waere sie irrefuehrend (der Lead ist ja satt)."""
    _setze_feuchte(speicher, JULI, bambus=30.0, yoga=70.0)

    _run(_detektor_mit_topologie(speicher).pruefe_alle(["bambus", "yoga"], jetzt=JULI))

    offen_lead = _run(speicher.offene_sensor_warnungen("yoga"))
    assert not [w for w in offen_lead if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]


def test_lead_divergenz_ignoriert_gleiche_kanalnummer_auf_anderem_geraet(speicher):
    """Regression: (geraet_id, kanal) ist die Kanal-Identitaet, nicht die Nummer.

    Zwei DSWCs haben beide einen "Kanal 2". Die erste Fassung verglich nur
    die Kanalnummer und zog damit `hecke` (DSWC 2 / K2) als Lead des
    Bambus-Kanals (DSWC 1 / K2) heran -- aufgefallen im Dry-Run gegen die
    echten Daten, nicht in den synthetischen Tests.

    Der Schaden waere nicht kosmetisch: eine durstige Fremd-Zone haette die
    Warnung UNTERDRUECKT ("ein Lead ist ja auch durstig, der Kanal giesst
    schon"), obwohl sie an einem voellig anderen Ventil haengt und fuer den
    Bambus kein Wasser bedeutet. Dokumentierte Klasse:
    `fehlerpattern_multi_dswc_kanal_lookup`.
    """
    topo = {
        "bambus": KanalRolle(kanal=2, ausgeschlossen=True, feuchte_kritisch=45.0,
                             geraet_id="dswc-1"),
        "yoga": KanalRolle(kanal=2, ausgeschlossen=False, feuchte_kritisch=50.0,
                           geraet_id="dswc-1"),
        # Gleiche Kanalnummer, ANDERES Geraet -- darf den Bambus-Kanal
        # weder als Lead stuetzen noch die Warnung unterdruecken.
        "hecke": KanalRolle(kanal=2, ausgeschlossen=False, feuchte_kritisch=22.0,
                            geraet_id="dswc-2"),
    }
    _setze_feuchte(speicher, JULI, bambus=30.0, yoga=70.0)
    _run(speicher.speichere_messung(
        _messung(JULI - timedelta(minutes=10), 15.0, "hecke")))  # hecke DURSTIG

    _run(LeckDetektor(speicher, kanal_topologie=topo).pruefe_alle(
        ["bambus", "yoga", "hecke"], jetzt=JULI))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    treffer = [w for w in offen if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]
    # Die Warnung MUSS stehen: hecke ist am anderen Ventil, ihr Durst
    # bringt dem Bambus kein Wasser.
    assert len(treffer) == 1, "hecke (anderes DSWC) hat die Warnung unterdrueckt"
    assert "hecke" not in treffer[0].details


# =====================================================================
# T-0438: Warnung friert Zahlen ein und behauptet die falsche Ursache
# =====================================================================

_TOPOLOGIE_T0438 = {
    "bambus": KanalRolle(kanal=2, ausgeschlossen=True, feuchte_kritisch=45.0),
    # Lead MIT Giess-Schwelle -- genau das fehlte vorher in der Rolle.
    "yoga": KanalRolle(
        kanal=2, ausgeschlossen=False,
        feuchte_kritisch=50.0, feuchte_schwelle_min=61.0,
    ),
}


def _detektor_t0438(speicher) -> LeckDetektor:
    return LeckDetektor(speicher, kanal_topologie=_TOPOLOGIE_T0438)


def test_t0438_details_werden_bei_offener_warnung_aufgefrischt(speicher):
    """(a) Eingefrorene Zahlen.

    `oeffne_sensor_warnung` liefert bei bereits offener Warnung False und
    liess `details` unberuehrt -- die Karte zeigte am 27.07. um 15:27 die
    Zahlen von 05:25 (30/70 statt real 20/55). Zweiter Durchlauf mit neuen
    Werten muss die Details mitziehen.
    """
    _setze_feuchte(speicher, JULI, bambus=30.0, yoga=70.0)
    _run(_detektor_t0438(speicher).pruefe_alle(["bambus", "yoga"], jetzt=JULI))

    spaeter = JULI + timedelta(hours=10)
    _setze_feuchte(speicher, spaeter, bambus=20.0, yoga=63.0)
    _run(_detektor_t0438(speicher).pruefe_alle(["bambus", "yoga"], jetzt=spaeter))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    treffer = [w for w in offen if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]
    assert len(treffer) == 1, "es darf keine zweite Warnung entstehen"
    assert "20" in treffer[0].details and "63" in treffer[0].details, (
        f"Details nicht aufgefrischt: {treffer[0].details}"
    )
    assert "30" not in treffer[0].details


def test_t0438_schliesst_wenn_lead_unter_giess_schwelle_faellt(speicher):
    """(c) Schliess-Kriterium.

    Lead 55 bei Giess-Schwelle 61: der Kanal wuerde ausloesen, der Ausschluss
    ist damit folgenlos und die Aussage "Lead meldet satt" falsch. Frueher
    schloss die Warnung erst unter kritisch 50 -- dazwischen behauptete sie
    weiter das Gegenteil (Realfall 27.07.).
    """
    _setze_feuchte(speicher, JULI, bambus=30.0, yoga=70.0)
    _run(_detektor_t0438(speicher).pruefe_alle(["bambus", "yoga"], jetzt=JULI))
    assert [w for w in _run(speicher.offene_sensor_warnungen("bambus"))
            if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]

    spaeter = JULI + timedelta(hours=2)
    _setze_feuchte(speicher, spaeter, bambus=30.0, yoga=55.0)
    _run(_detektor_t0438(speicher).pruefe_alle(["bambus", "yoga"], jetzt=spaeter))

    offen = _run(speicher.offene_sensor_warnungen("bambus"))
    assert not [w for w in offen if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ], (
        "Lead unter Giess-Schwelle -> Kanal loest aus -> Warnung gegenstandslos"
    )


def test_t0438_nennt_den_echten_blocker_statt_den_ausschluss(speicher):
    """(b) Falsche Ursache.

    Am 27.07. sagte die Warnung "Kanal 2 giesst deshalb nicht", geblockt hat
    aber REGEN_ERWARTET. Wer das liest, sucht an der falschen Stelle.
    """
    from bewaesserung.modelle import (
        BewaesserungsEntscheidung, BlockerTyp, EntscheidungsScope,
    )
    _run(speicher.speichere_entscheidung(BewaesserungsEntscheidung(
        zeitstempel=JULI - timedelta(minutes=5),
        zone_id="yoga", soll_bewaessern=False,
        begruendung="Regen", blocker_typ=BlockerTyp.REGEN_ERWARTET,
        scope=EntscheidungsScope.KANAL, scope_ref="2",
    )))
    _setze_feuchte(speicher, JULI, bambus=30.0, yoga=70.0)
    _run(_detektor_t0438(speicher).pruefe_alle(["bambus", "yoga"], jetzt=JULI))

    treffer = [w for w in _run(speicher.offene_sensor_warnungen("bambus"))
               if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]
    assert len(treffer) == 1
    assert "REGEN_ERWARTET" in treffer[0].details
    assert "giesst deshalb nicht" not in treffer[0].details


def test_t0438_behaelt_die_kausalaussage_wenn_sie_stimmt(speicher):
    """Gegenprobe zu (b): blockt wirklich die Feuchtelage, DARF die Warnung
    den Ausschluss als Ursache nennen. Der Fix soll die Aussage praezisieren,
    nicht generell entfernen."""
    from bewaesserung.modelle import (
        BewaesserungsEntscheidung, BlockerTyp, EntscheidungsScope,
    )
    _run(speicher.speichere_entscheidung(BewaesserungsEntscheidung(
        zeitstempel=JULI - timedelta(minutes=5),
        zone_id="yoga", soll_bewaessern=False,
        begruendung="Feuchte ok", blocker_typ=BlockerTyp.FEUCHTE_OK,
        scope=EntscheidungsScope.KANAL, scope_ref="2",
    )))
    _setze_feuchte(speicher, JULI, bambus=30.0, yoga=70.0)
    _run(_detektor_t0438(speicher).pruefe_alle(["bambus", "yoga"], jetzt=JULI))

    treffer = [w for w in _run(speicher.offene_sensor_warnungen("bambus"))
               if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]
    assert "giesst deshalb nicht" in treffer[0].details


def test_t0438_rolle_ohne_giess_schwelle_faellt_auf_kritisch_zurueck(speicher):
    """Backward-Compat: Rollen ohne `feuchte_schwelle_min` (alte Konfig,
    Tests, Zonen ohne gesetzten Wert) verhalten sich wie vorher."""
    _setze_feuchte(speicher, JULI, bambus=30.0, yoga=70.0)
    _run(_detektor_mit_topologie(speicher).pruefe_alle(["bambus", "yoga"], jetzt=JULI))
    assert [w for w in _run(speicher.offene_sensor_warnungen("bambus"))
            if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]

    spaeter = JULI + timedelta(hours=2)
    _setze_feuchte(speicher, spaeter, bambus=30.0, yoga=55.0)
    _run(_detektor_mit_topologie(speicher).pruefe_alle(["bambus", "yoga"], jetzt=spaeter))
    # 55 liegt ueber kritisch 50 -> alte Topologie haelt die Warnung offen
    assert [w for w in _run(speicher.offene_sensor_warnungen("bambus"))
            if w.typ == SensorWarnungTyp.LEAD_DIVERGENZ]
