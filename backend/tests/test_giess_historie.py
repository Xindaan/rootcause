"""T-0335: Gruppierung roher Ventil-Events zu Giess-Laeufen."""

from datetime import datetime, timedelta

from bewaesserung.giess_historie import (
    aggregiere_zonen_laeufe,
    gruppiere_giess_laeufe,
)
from bewaesserung.modelle import Ausloser, VentilAktion, VentilEreignis

BASIS = datetime(2026, 6, 25, 8, 0, 0)


def _ev(min_offset, aktion, *, dauer=0, ausloser=Ausloser.MANUELL,
        ventil_id="v1", lauf_gruppe=None, phase=None, zone="bambuswald"):
    return VentilEreignis(
        zeitstempel=BASIS + timedelta(minutes=min_offset),
        zone_id=zone,
        ventil_id=ventil_id,
        aktion=aktion,
        dauer_sekunden=dauer,
        ausloser=ausloser,
        lauf_gruppe=lauf_gruppe,
        phase=phase,
    )


def test_einzellauf_oeffnen_schliessen():
    """Ein OEFFNEN + SCHLIESSEN ohne Marker = ein Einzel-Lauf mit Dauer."""
    evs = [
        _ev(0, VentilAktion.OEFFNEN, ausloser=Ausloser.AUTOMATIK),
        _ev(30, VentilAktion.SCHLIESSEN, dauer=1800, ausloser=Ausloser.AUTOMATIK),
    ]
    laeufe = gruppiere_giess_laeufe(evs)
    assert len(laeufe) == 1
    lauf = laeufe[0]
    assert lauf["methode"] == "einzel"
    assert lauf["ausloser"] == "automatik"
    assert lauf["dauer_gesamt_s"] == 1800
    assert lauf["zaehlt"] is True
    assert lauf["laeuft_noch"] is False
    assert len(lauf["phasen"]) == 1


def test_pre_soak_wird_als_ein_lauf_gruppiert():
    """Puls + Haupt mit gleicher lauf_gruppe = EIN Pre-Soak-Lauf, 2 Phasen,
    Gesamtdauer = Summe."""
    g = "presoak_bambuswald_20260625"
    evs = [
        _ev(0, VentilAktion.OEFFNEN, lauf_gruppe=g, phase="pre_soak"),
        _ev(5, VentilAktion.SCHLIESSEN, dauer=300, lauf_gruppe=g, phase="pre_soak"),
        _ev(30, VentilAktion.OEFFNEN, lauf_gruppe=g, phase="haupt"),
        _ev(90, VentilAktion.SCHLIESSEN, dauer=3600, lauf_gruppe=g, phase="haupt"),
    ]
    laeufe = gruppiere_giess_laeufe(evs)
    assert len(laeufe) == 1
    lauf = laeufe[0]
    assert lauf["methode"] == "pre_soak"
    assert lauf["dauer_gesamt_s"] == 300 + 3600
    phasen = [p["phase"] for p in lauf["phasen"]]
    assert phasen == ["pre_soak", "haupt"]


def test_ignoriert_zaehlt_nicht():
    """Auto-ignorierte/Cross-Spray-Events: zaehlt=False (keine echte Bewaesserung)."""
    evs = [
        _ev(0, VentilAktion.OEFFNEN, ausloser=Ausloser.IGNORIERT),
        _ev(10, VentilAktion.SCHLIESSEN, dauer=600, ausloser=Ausloser.IGNORIERT),
    ]
    laeufe = gruppiere_giess_laeufe(evs)
    assert len(laeufe) == 1
    assert laeufe[0]["zaehlt"] is False


def test_laufender_lauf_ohne_schliessen():
    """OEFFNEN ohne SCHLIESSEN = laeuft_noch, keine Dauer."""
    laeufe = gruppiere_giess_laeufe([_ev(0, VentilAktion.OEFFNEN)])
    assert len(laeufe) == 1
    assert laeufe[0]["laeuft_noch"] is True
    assert laeufe[0]["dauer_gesamt_s"] is None


def test_mehrere_laeufe_neueste_zuerst():
    """Einzel- und Pre-Soak-Laeufe gemischt -> chronologisch absteigend."""
    g = "presoak_x"
    evs = [
        _ev(0, VentilAktion.OEFFNEN),
        _ev(10, VentilAktion.SCHLIESSEN, dauer=600),
        _ev(120, VentilAktion.OEFFNEN, lauf_gruppe=g, phase="pre_soak"),
        _ev(125, VentilAktion.SCHLIESSEN, dauer=300, lauf_gruppe=g, phase="pre_soak"),
        _ev(150, VentilAktion.OEFFNEN, lauf_gruppe=g, phase="haupt"),
        _ev(200, VentilAktion.SCHLIESSEN, dauer=3000, lauf_gruppe=g, phase="haupt"),
    ]
    laeufe = gruppiere_giess_laeufe(evs)
    assert len(laeufe) == 2
    # Neuester (Pre-Soak ab min 120) zuerst, dann der Einzel-Lauf (min 0).
    assert laeufe[0]["methode"] == "pre_soak"
    assert laeufe[1]["methode"] == "einzel"
    assert laeufe[0]["start"] > laeufe[1]["start"]


def test_getrennte_einzellaeufe_nicht_zusammengefasst():
    """Zwei Einzellaeufe ohne Marker bleiben ZWEI Laeufe (keine Heuristik-
    Verschmelzung -- der Marker ist die einzige Gruppierungs-Quelle)."""
    evs = [
        _ev(0, VentilAktion.OEFFNEN),
        _ev(5, VentilAktion.SCHLIESSEN, dauer=300),
        _ev(8, VentilAktion.OEFFNEN),
        _ev(40, VentilAktion.SCHLIESSEN, dauer=1920),
    ]
    laeufe = gruppiere_giess_laeufe(evs)
    assert len(laeufe) == 2
    assert all(lauf["methode"] == "einzel" for lauf in laeufe)


def test_verwaistes_oeffnen_kaskadiert_nicht():
    """Regression (magerwiese 10.07.2026): OEFFNEN ohne SCHLIESSEN (Close-
    Event verloren) darf NICHT das SCHLIESSEN des naechsten Laufs konsumieren.
    Vorher schob der Waise die FIFO-Paarung um eins -> letzter Lauf haengt
    faelschlich auf laeuft_noch=True, obwohl sein SCHLIESSEN existiert."""
    evs = [
        _ev(0, VentilAktion.OEFFNEN),  # Waise: Close verloren
        _ev(60, VentilAktion.OEFFNEN),
        _ev(68, VentilAktion.SCHLIESSEN, dauer=480),
        _ev(120, VentilAktion.OEFFNEN),
        _ev(128, VentilAktion.SCHLIESSEN, dauer=493),
    ]
    laeufe = gruppiere_giess_laeufe(evs)  # neueste zuerst
    assert len(laeufe) == 3
    assert laeufe[0]["dauer_gesamt_s"] == 493
    assert laeufe[0]["laeuft_noch"] is False
    assert laeufe[1]["dauer_gesamt_s"] == 480
    # Der Waise: Ende unbekannt, aber NICHT "laeuft noch" -- das spaetere
    # OEFFNEN beweist, dass das Ventil zwischenzeitlich zu war.
    assert laeufe[2]["ende"] is None
    assert laeufe[2]["laeuft_noch"] is False
    assert laeufe[2]["dauer_gesamt_s"] is None


def test_verwaistes_oeffnen_plus_echter_laufender_lauf():
    """Waise am Anfang + echtes offenes OEFFNEN am Ende: nur der juengste
    unpaarige Lauf gilt als laufend."""
    evs = [
        _ev(0, VentilAktion.OEFFNEN),  # Waise
        _ev(60, VentilAktion.OEFFNEN),
        _ev(68, VentilAktion.SCHLIESSEN, dauer=480),
        _ev(120, VentilAktion.OEFFNEN),  # laeuft wirklich noch
    ]
    laeufe = gruppiere_giess_laeufe(evs)
    assert len(laeufe) == 3
    assert laeufe[0]["laeuft_noch"] is True
    assert laeufe[1]["dauer_gesamt_s"] == 480
    assert laeufe[2]["laeuft_noch"] is False


def test_fenstergrenze_schneidet_paar_orphan_schliessen():
    """Fenstergrenze schneidet ein OEFFNEN/SCHLIESSEN-Paar: das SCHLIESSEN
    ohne OEFFNEN im Fenster wird ein eigener Lauf (start=ende-Zeitstempel)
    und konsumiert KEIN OEFFNEN eines Folge-Laufs."""
    evs = [
        _ev(0, VentilAktion.SCHLIESSEN, dauer=600),  # OEFFNEN vor Fenster
        _ev(60, VentilAktion.OEFFNEN),
        _ev(70, VentilAktion.SCHLIESSEN, dauer=600),
    ]
    laeufe = gruppiere_giess_laeufe(evs)
    assert len(laeufe) == 2
    assert laeufe[0]["dauer_gesamt_s"] == 600
    assert laeufe[0]["laeuft_noch"] is False
    assert laeufe[1]["start"] == laeufe[1]["ende"]  # Orphan-SCHLIESSEN-Lauf
    assert all(lauf["laeuft_noch"] is False for lauf in laeufe)


# --- Multi-Zonen-Aggregation (T-0335 "Alle Zonen") ---

def test_aggregiere_serieller_strang_dedupt_zu_einem_lauf():
    """End-to-end: EIN physischer Lauf an zwei Zonen am selben Ventil (Bambus-
    Strang) -> pro Zone gruppiert + aggregiert = EIN Lauf mit beiden zone_ids,
    KEINE Doppelzaehlung."""
    def lauf_events(zone):
        return [
            _ev(0, VentilAktion.OEFFNEN, ventil_id="valveBambus", zone=zone),
            _ev(35, VentilAktion.SCHLIESSEN, dauer=2100,
                ventil_id="valveBambus", zone=zone),
        ]
    laeufe = (
        gruppiere_giess_laeufe(lauf_events("bambuswald"))
        + gruppiere_giess_laeufe(lauf_events("bambuswald_yogaraum"))
    )
    out = aggregiere_zonen_laeufe(laeufe)
    assert len(out) == 1
    assert set(out[0]["zone_ids"]) == {"bambuswald", "bambuswald_yogaraum"}
    assert out[0]["dauer_gesamt_s"] == 2100


def test_aggregiere_verschiedene_ventile_bleiben_getrennt():
    """Verschiedene Ventile (verschiedene Zonen) -> getrennte Laeufe, selbst bei
    gleicher Startzeit (kein faelschlicher Merge ohne ventil_id-Match)."""
    laeufe = [
        {"start": "2026-06-24T12:47:00", "ventil_id": "valveA",
         "zone_ids": ["bambuswald"], "dauer_gesamt_s": 600},
        {"start": "2026-06-24T12:47:00", "ventil_id": "valveB",
         "zone_ids": ["waldblumenhain"], "dauer_gesamt_s": 600},
    ]
    assert len(aggregiere_zonen_laeufe(laeufe)) == 2


def test_aggregiere_gleiches_ventil_verschiedene_zeiten_getrennt():
    """Gleiches Ventil, aber Laeufe Minuten auseinander -> getrennt, neueste
    zuerst."""
    laeufe = [
        {"start": "2026-06-24T12:47:00", "ventil_id": "valveA",
         "zone_ids": ["bambuswald"], "dauer_gesamt_s": 600},
        {"start": "2026-06-24T14:00:00", "ventil_id": "valveA",
         "zone_ids": ["bambuswald"], "dauer_gesamt_s": 600},
    ]
    out = aggregiere_zonen_laeufe(laeufe)
    assert len(out) == 2
    assert out[0]["start"] > out[1]["start"]
