"""T-0416: Detektor fuer FYTA-Kalibrier-Pushes.

Die Tests bilden die drei realen Pushes (08./13./20.07.2026) als Muster ab
sowie die Faelle, die NICHT feuern duerfen. Der wichtigste davon ist der
AquaBloom-Zyklus in `kasten_4` -- er hat einen ersten Entwurf des Detektors
zu Fall gebracht (s. `test_aquabloom_...`).
"""

from datetime import datetime, timedelta

import pytest

from bewaesserung.fyta_sprung_detektor import (
    GARDENA_RUHE_PP,
    MIN_SPRUNG_PP,
    FytaSprungDetektor,
    finde_spruenge,
    gardena_ruhig,
    yaml_vorschlag,
)
from bewaesserung.modelle import (
    Ausloser, SensorMessung, VentilAktion, VentilEreignis,
)

T0 = datetime(2026, 7, 20, 14, 34)


def _m(geraet, zone, feuchte, minuten, quelle="fyta"):
    return SensorMessung(
        zeitstempel=T0 + timedelta(minutes=minuten),
        zone_id=zone, geraet_id=geraet,
        boden_feuchte=feuchte, quelle=quelle,
    )


class _SpeicherAttrappe:
    def __init__(self, messungen, ereignisse=None):
        self._m = messungen
        self._e = ereignisse or []
        self.warnungen = []

    async def hole_messungen(self, zone_id, von=None, bis=None):
        return [m for m in self._m if m.zone_id == zone_id]

    async def hole_ventil_ereignisse(self, zone_id, von=None, bis=None):
        return [
            e for e in self._e
            if e.zone_id == zone_id
            and (von is None or e.zeitstempel >= von)
            and (bis is None or e.zeitstempel <= bis)
        ]

    async def oeffne_sensor_warnung(self, warnung):
        self.warnungen.append(warnung)
        return True


class _Zone:
    def __init__(self, zone_id):
        self.zone_id = zone_id


# --------------------------------------------------------------------------
# Sprung-Erkennung
# --------------------------------------------------------------------------

def test_findet_sprung_in_einem_intervall():
    m = [_m("fyta_1", "hecke", 100.0, 0), _m("fyta_1", "hecke", 62.0, 15)]
    (treffer,) = finde_spruenge(m)
    assert treffer.geraet_id == "fyta_1"
    assert treffer.delta == pytest.approx(-38.0)


def test_rampe_ueber_stunden_ist_kein_sprung():
    """Ein Push ist ein Schnitt zwischen zwei Messungen. Eine langsame
    Aenderung ist Boden (Trocknung), kein Server-Deploy -- selbst wenn die
    Gesamtdifferenz gross ist."""
    m = [_m("fyta_1", "hecke", 100.0 - i * 5, i * 60) for i in range(8)]
    assert finde_spruenge(m) == []


def test_grosser_abstand_zaehlt_nicht_als_intervall():
    """Zwei Messungen weit auseinander sind kein 'ein Messintervall' --
    dazwischen kann alles passiert sein (Sensor offline, Nachtruecke)."""
    m = [_m("fyta_1", "hecke", 100.0, 0), _m("fyta_1", "hecke", 62.0, 240)]
    assert finde_spruenge(m) == []


def test_gardena_quelle_wird_nicht_als_fyta_sprung_gemeldet():
    m = [
        _m("gard_1", "hecke", 100.0, 0, quelle="gardena"),
        _m("gard_1", "hecke", 62.0, 15, quelle="gardena"),
    ]
    assert finde_spruenge(m) == []


# --------------------------------------------------------------------------
# Kontrollgruppe -- der Kern von T-0416
# --------------------------------------------------------------------------

def test_kontrollgruppe_nur_aus_derselben_zone():
    """DER REGRESSION-TEST fuer den Realdaten-Fund.

    Ein erster Entwurf pruefte alle Gardena-Sensoren global. Dadurch bekam
    `kasten_4` (Topf, kein Gardena drin) eine 'Kontrollgruppe' aus fremden
    Zonen geliehen und meldete AquaBloom-Zyklen als Hersteller-Push.
    Ein Gardena in fremder Erde belegt nichts.
    """
    messungen = [
        _m("gard_bambus", "bambuswald", 60.0, 0, quelle="gardena"),
        _m("gard_bambus", "bambuswald", 60.0, 15, quelle="gardena"),
    ]
    ruhig, geprueft = gardena_ruhig(messungen, T0 + timedelta(minutes=15),
                                    zonen={"kasten_4"})
    assert ruhig is False
    assert geprueft == []


def test_kontrollgruppe_ruhig_wenn_gardena_daneben_still_steht():
    messungen = [
        _m("gard_h", "hecke", 45.0, 0, quelle="gardena"),
        _m("gard_h", "hecke", 45.0, 15, quelle="gardena"),
    ]
    ruhig, geprueft = gardena_ruhig(messungen, T0 + timedelta(minutes=15),
                                    zonen={"hecke"})
    assert ruhig is True
    assert geprueft == ["gard_h"]


def test_kontrollgruppe_nicht_ruhig_wenn_gardena_mitspringt():
    """Springt der Gardena mit, ist es Boden (Regen/Wasser) -- kein Artefakt."""
    messungen = [
        _m("gard_h", "hecke", 45.0, 0, quelle="gardena"),
        _m("gard_h", "hecke", 45.0 + GARDENA_RUHE_PP + 1, 15, quelle="gardena"),
    ]
    ruhig, _ = gardena_ruhig(messungen, T0 + timedelta(minutes=15),
                             zonen={"hecke"})
    assert ruhig is False


# --------------------------------------------------------------------------
# Ende-zu-Ende
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_push_20_07_muster_wird_erkannt():
    """Realmuster 20.07.: zwei FYTA in zwei Zonen fallen, Gardena bleibt."""
    messungen = [
        _m("fyta_100001", "waldblumenhain", 100.0, 0),
        _m("fyta_100001", "waldblumenhain", 62.0, 15),
        _m("fyta_100002", "hecke", 23.0, 0),
        _m("fyta_100002", "hecke", 15.0, 15),
        _m("gard_w", "waldblumenhain", 44.0, 0, quelle="gardena"),
        _m("gard_w", "waldblumenhain", 44.0, 15, quelle="gardena"),
    ]
    sp = _SpeicherAttrappe(messungen)
    d = FytaSprungDetektor(sp, [_Zone("waldblumenhain"), _Zone("hecke")])
    (befund,) = await d.pruefe(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert befund.regel == "kontrollgruppe"
    assert sorted(befund.geraete) == ["fyta_100001", "fyta_100002"]


@pytest.mark.asyncio
async def test_aquabloom_zyklus_ist_kein_push():
    """DER FALSE-POSITIVE-FALL aus den Realdaten (13./15.07., kasten_4).

    Topf-Zone ohne Gardena-Partner, AquaBloom hebt die Feuchte ~+10 pp --
    und seit T-0409 schreibt die Sensor-Heuristik dafuer KEINE Ventil-Events
    mehr, faellt also nicht ueber die Wasser-Karenz raus. Einzige Zone,
    keine Kontrollgruppe -> darf NICHT feuern.
    """
    messungen = [
        _m("fyta_100003", "kasten_4", 25.0, 0),
        _m("fyta_100003", "kasten_4", 35.0, 15),
        # Gardena in einer ANDEREN Zone, ruhig -- darf nicht helfen.
        _m("gard_bambus", "bambuswald", 60.0, 0, quelle="gardena"),
        _m("gard_bambus", "bambuswald", 60.0, 15, quelle="gardena"),
    ]
    sp = _SpeicherAttrappe(messungen)
    d = FytaSprungDetektor(sp, [_Zone("kasten_4"), _Zone("bambuswald")])
    assert await d.pruefe(T0 - timedelta(hours=1), T0 + timedelta(hours=1)) == []


@pytest.mark.asyncio
async def test_bewaesserung_erklaert_den_sprung():
    """Lief Wasser, ist der Sprung erklaert -- auch wenn er gross ist."""
    messungen = [
        _m("fyta_1", "hecke", 30.0, 0),
        _m("fyta_1", "hecke", 55.0, 15),
        _m("fyta_2", "waldblumenhain", 30.0, 0),
        _m("fyta_2", "waldblumenhain", 55.0, 15),
    ]
    ereignisse = [
        VentilEreignis(
            zeitstempel=T0 - timedelta(minutes=30), zone_id=z,
            ventil_id="v1", aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=1800, ausloser=Ausloser.AUTOMATIK,
        )
        for z in ("hecke", "waldblumenhain")
    ]
    sp = _SpeicherAttrappe(messungen, ereignisse)
    d = FytaSprungDetektor(sp, [_Zone("hecke"), _Zone("waldblumenhain")])
    assert await d.pruefe(T0 - timedelta(hours=1), T0 + timedelta(hours=1)) == []


@pytest.mark.asyncio
async def test_heuristik_pseudo_event_erklaert_nichts():
    """Die Sensor-Heuristik leitet ihre Events SELBST aus Feuchte-Spruengen
    ab. Wuerde sie als Wasser-Beleg zaehlen, erklaerte ein Push sich selbst
    zirkulaer weg."""
    messungen = [
        _m("fyta_1", "hecke", 30.0, 0),
        _m("fyta_1", "hecke", 55.0, 15),
        _m("fyta_2", "waldblumenhain", 30.0, 0),
        _m("fyta_2", "waldblumenhain", 55.0, 15),
    ]
    ereignisse = [
        VentilEreignis(
            zeitstempel=T0 - timedelta(minutes=30), zone_id=z,
            ventil_id="sensor_heuristik", aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=1800, ausloser=Ausloser.AUTOMATIK,
        )
        for z in ("hecke", "waldblumenhain")
    ]
    sp = _SpeicherAttrappe(messungen, ereignisse)
    d = FytaSprungDetektor(sp, [_Zone("hecke"), _Zone("waldblumenhain")])
    assert len(await d.pruefe(
        T0 - timedelta(hours=1), T0 + timedelta(hours=1))) == 1


@pytest.mark.asyncio
async def test_einzelne_zone_ohne_kontrollgruppe_feuert_nicht():
    """Ein einzelner Sensor ohne Gardena-Partner reicht nicht -- sonst wird
    jeder Giessvorgang in einer Topf-Zone zum 'Push'."""
    messungen = [
        _m("fyta_1", "kasten_4", 25.0, 0),
        _m("fyta_1", "kasten_4", 40.0, 15),
    ]
    sp = _SpeicherAttrappe(messungen)
    d = FytaSprungDetektor(sp, [_Zone("kasten_4")])
    assert await d.pruefe(T0 - timedelta(hours=1), T0 + timedelta(hours=1)) == []


@pytest.mark.asyncio
async def test_meldung_schreibt_warnung_ohne_giess_reaktion():
    messungen = [
        _m("fyta_100001", "waldblumenhain", 100.0, 0),
        _m("fyta_100001", "waldblumenhain", 62.0, 15),
        _m("fyta_100002", "hecke", 23.0, 0),
        _m("fyta_100002", "hecke", 15.0, 15),
    ]
    sp = _SpeicherAttrappe(messungen)
    d = FytaSprungDetektor(sp, [_Zone("waldblumenhain"), _Zone("hecke")])
    await d.pruefe_und_melde(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    (warnung,) = sp.warnungen
    assert warnung.typ.value == "fyta_kalibrier_push"
    assert "KEINE Giess-Reaktion" in warnung.details


def test_yaml_vorschlag_ist_geraet_scoped_und_eng():
    """Das Fenster darf nur den Sprung abdecken (~1 h), nicht Tage --
    es wird als Chart-Overlay gerendert (T-0407) und die Werte sind ab der
    naechsten Messung auf der neuen Skala wieder valide (T-0385)."""
    from bewaesserung.fyta_sprung_detektor import PushBefund, SprungBefund
    b = PushBefund(
        zeitpunkt=T0,
        spruenge=[SprungBefund("fyta_100001", "waldblumenhain",
                               100.0, 62.0, T0)],
        regel="kontrollgruppe",
    )
    text = yaml_vorschlag(b)
    assert "geraet_id: fyta_100001" in text
    assert "zone_id: waldblumenhain" in text
    assert "T-0416" in text
    von = text.split("von: ")[1].split("\n")[0]
    bis = text.split("bis: ")[1].split("\n")[0]
    spanne = datetime.fromisoformat(bis) - datetime.fromisoformat(von)
    assert spanne <= timedelta(hours=2), "Fenster zu breit -- verdeckt Daten"


@pytest.mark.asyncio
async def test_intervall_gate_laeuft_nicht_in_jedem_zyklus():
    """Der Detektor haengt im 5-min-Entscheidungs-Loop, scannt aber Messreihen
    ueber alle Zonen -- das darf nicht jeden Zyklus laufen
    (fehlerpattern_redundanter_df_build_eventloop)."""
    sp = _SpeicherAttrappe([])
    d = FytaSprungDetektor(sp, [_Zone("hecke")])
    jetzt = T0
    await d.aktualisiere_wenn_faellig(jetzt)
    erster = d._letzter_lauf
    # 5 Minuten spaeter: darf NICHT erneut scannen.
    await d.aktualisiere_wenn_faellig(jetzt + timedelta(minutes=5))
    assert d._letzter_lauf == erster
    # Nach dem Intervall: laeuft wieder.
    await d.aktualisiere_wenn_faellig(
        jetzt + timedelta(minutes=FytaSprungDetektor.INTERVALL_MIN + 1)
    )
    assert d._letzter_lauf != erster


def test_rueckblick_groesser_als_intervall():
    """Sonst faellt ein Sprung zwischen zwei Laeufen durch -- genau der Fehler,
    den der Detektor verhindern soll."""
    assert (
        FytaSprungDetektor.RUECKBLICK_MIN > FytaSprungDetektor.INTERVALL_MIN
    )


def test_schwelle_ueber_sensor_rauschen():
    """Sanity: die Sprungschwelle muss klar ueber der Gardena-Ruhe-Toleranz
    liegen, sonst kollidieren die beiden Regeln."""
    assert MIN_SPRUNG_PP > GARDENA_RUHE_PP * 2


# --------------------------------------------------------------------------
# T-0416 Statuswechsel 23.07.: Severity KRITISCH + iMessage-Push
# --------------------------------------------------------------------------

class _BenachrichtigerAttrappe:
    def __init__(self, erfolg=True):
        self.texte = []
        self._erfolg = erfolg

    async def sende_text(self, empfaenger, text):
        self.texte.append(text)
        return self._erfolg


@pytest.mark.asyncio
async def test_push_wird_gesendet():
    """T-0416 (23.07.): FYTA hat ein `calibration_version`-Feld abgesagt --
    dieser Detektor ist dauerhaft die einzige Quelle. Der Push vom 20.07.
    kippte ueber den Median still den operativen Feuchtewert (T-0421).
    Deshalb iMessage statt nur Ops-Timeline."""
    messungen = [
        _m("fyta_100001", "waldblumenhain", 100.0, 0),
        _m("fyta_100001", "waldblumenhain", 62.0, 15),
        _m("fyta_100002", "hecke", 23.0, 0),
        _m("fyta_100002", "hecke", 15.0, 15),
    ]
    sp = _SpeicherAttrappe(messungen)
    ben = _BenachrichtigerAttrappe()
    d = FytaSprungDetektor(sp, [_Zone("waldblumenhain"), _Zone("hecke")],
                           benachrichtiger=ben, empfaenger="test")
    await d.pruefe_und_melde(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert len(ben.texte) == 1
    text = ben.texte[0]
    assert "FYTA-Kalibrierung verschoben" in text
    assert "Sensoren sind in Ordnung" in text, "kein Defekt-Alarm"
    assert "kein Giess-Anlass" in text, (
        "Lehre T-0426: ein Daten-Artefakt darf keine Giess-Aufforderung sein"
    )


@pytest.mark.asyncio
async def test_push_wird_gedrosselt():
    """Die Erkennung laeuft stuendlich mit 90-min-Rueckblick -- ohne Throttle
    wuerde derselbe Sprung mehrfach pushen."""
    messungen = [
        _m("fyta_100001", "waldblumenhain", 100.0, 0),
        _m("fyta_100001", "waldblumenhain", 62.0, 15),
        _m("fyta_100002", "hecke", 23.0, 0),
        _m("fyta_100002", "hecke", 15.0, 15),
    ]
    sp = _SpeicherAttrappe(messungen)
    ben = _BenachrichtigerAttrappe()
    d = FytaSprungDetektor(sp, [_Zone("waldblumenhain"), _Zone("hecke")],
                           benachrichtiger=ben, empfaenger="test")
    for _ in range(3):
        await d.pruefe_und_melde(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert len(ben.texte) == 1, "Throttle muss die Wiederholungen schlucken"


@pytest.mark.asyncio
async def test_ohne_benachrichtiger_kein_absturz():
    """Backward-Compat: der Detektor lief bis 22.07. ohne Push."""
    messungen = [
        _m("fyta_100001", "waldblumenhain", 100.0, 0),
        _m("fyta_100001", "waldblumenhain", 62.0, 15),
        _m("fyta_100002", "hecke", 23.0, 0),
        _m("fyta_100002", "hecke", 15.0, 15),
    ]
    sp = _SpeicherAttrappe(messungen)
    d = FytaSprungDetektor(sp, [_Zone("waldblumenhain"), _Zone("hecke")])
    befunde = await d.pruefe_und_melde(
        T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert len(befunde) == 1


def test_severity_ist_kritisch():
    """Die Ops-Timeline muss den Typ als KRITISCH fuehren -- ROUTINE geht im
    Ausnahme-Feed unter, und genau das soll der Detektor verhindern."""
    import inspect

    from bewaesserung import api_server
    quelle = inspect.getsource(api_server)
    # Der Typ muss im KRITISCH-Set stehen (nicht nur in der titel_map).
    idx_set = quelle.index('severity = "KRITISCH"')
    block = quelle[max(0, idx_set - 1200):idx_set]
    assert "FYTA_KALIBRIER_PUSH" in block, (
        "FYTA_KALIBRIER_PUSH fehlt im KRITISCH-Set (T-0416 Statuswechsel)"
    )


# --------------------------------------------------------------------------
# T-0416 Stufe 2: automatische Quarantaene
# --------------------------------------------------------------------------

class _SpeicherMitAusschluss(_SpeicherAttrappe):
    def __init__(self, messungen, ereignisse=None):
        super().__init__(messungen, ereignisse)
        self.fenster = []

    async def speichere_auto_ausschluss(
        self, zone_id, geraet_id, von, bis, grund, quelle="fyta_sprung_detektor",
    ):
        schluessel = (zone_id, geraet_id, von)
        if any(f[:3] == schluessel for f in self.fenster):
            return False  # idempotent wie der UNIQUE-Index
        self.fenster.append((zone_id, geraet_id, von, bis, grund))
        return True


def _push_messungen():
    return [
        _m("fyta_100001", "waldblumenhain", 100.0, 0),
        _m("fyta_100001", "waldblumenhain", 62.0, 15),
        _m("fyta_100002", "hecke", 23.0, 0),
        _m("fyta_100002", "hecke", 15.0, 15),
    ]


@pytest.mark.asyncio
async def test_ausschluss_wird_automatisch_gesetzt():
    """T-0416 Stufe 2: FYTA hat ein calibration_version-Feld abgesagt, die
    Pushes kommen woechentlich und rueckwirkend ist die Zuordnung unmoeglich.
    Jede Stunde ohne Fenster kontaminiert Fits und ML-Fenster."""
    sp = _SpeicherMitAusschluss(_push_messungen())
    d = FytaSprungDetektor(sp, [_Zone("waldblumenhain"), _Zone("hecke")])
    await d.pruefe_und_melde(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert len(sp.fenster) == 2
    geraete = {f[1] for f in sp.fenster}
    assert geraete == {"fyta_100001", "fyta_100002"}


@pytest.mark.asyncio
async def test_ausschluss_ist_geraet_scoped_nicht_zone_weit():
    """DER Sicherheits-Test. Ein FYTA-Push betrifft die FYTA-Sensoren --
    der Gardena in derselben Zone misst weiter richtig und ist bei
    hecke/waldblumenhain sogar der Aggregat-Lead (T-0421). Ein zone-weites
    Fenster wuerde ihn stilllegen und die Zone blind machen."""
    sp = _SpeicherMitAusschluss(_push_messungen())
    d = FytaSprungDetektor(sp, [_Zone("waldblumenhain"), _Zone("hecke")])
    await d.pruefe_und_melde(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    for zone_id, geraet_id, *_ in sp.fenster:
        assert geraet_id.startswith("fyta_"), (
            f"Fenster fuer {geraet_id} -- nur FYTA-Sensoren duerfen in "
            f"Quarantaene, nie der Gardena-Lead"
        )
        assert geraet_id, "geraet_id ist Pflicht (sonst zone-weit)"


@pytest.mark.asyncio
async def test_ausschluss_fenster_bleibt_eng():
    """Nur der Sprung ist das Artefakt; ab der naechsten Messung sind die
    Werte auf der neuen Skala valide (T-0385). Breite Fenster verdecken im
    Chart-Overlay genau die Daten, die sie erklaeren sollen (T-0407)."""
    sp = _SpeicherMitAusschluss(_push_messungen())
    d = FytaSprungDetektor(sp, [_Zone("waldblumenhain"), _Zone("hecke")])
    await d.pruefe_und_melde(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    for _, _, von, bis, _ in sp.fenster:
        assert bis - von <= timedelta(hours=2)


@pytest.mark.asyncio
async def test_ausschluss_wirkt_sofort_in_der_lebenden_konfig():
    """Ohne den In-Memory-Merge wuerde das Fenster erst nach dem naechsten
    Neustart wirken -- bis dahin lernt jeder Fit-Job den Sprung mit.
    Alle sieben Konsumenten lesen diese Liste."""
    sp = _SpeicherMitAusschluss(_push_messungen())
    lebende_liste = []
    d = FytaSprungDetektor(
        sp, [_Zone("waldblumenhain"), _Zone("hecke")],
        konfig_fenster=lebende_liste,
    )
    await d.pruefe_und_melde(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert len(lebende_liste) == 2
    assert all(f.geraet_id.startswith("fyta_") for f in lebende_liste)


@pytest.mark.asyncio
async def test_ausschluss_ist_idempotent():
    """Der Detektor laeuft stuendlich mit 90-min-Rueckblick und sieht
    denselben Sprung mehrfach -- er darf nicht jedes Mal ein Fenster
    anlegen."""
    sp = _SpeicherMitAusschluss(_push_messungen())
    lebende_liste = []
    d = FytaSprungDetektor(
        sp, [_Zone("waldblumenhain"), _Zone("hecke")],
        konfig_fenster=lebende_liste,
    )
    for _ in range(3):
        await d.pruefe_und_melde(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert len(sp.fenster) == 2
    assert len(lebende_liste) == 2, "auch die lebende Liste darf nicht wachsen"


@pytest.mark.asyncio
async def test_speicher_fehler_blockiert_den_detektor_nicht():
    """Die Quarantaene ist Zusatz-Nutzen. Faellt der Schreibpfad aus, muss
    die Warnung trotzdem rausgehen."""
    sp = _SpeicherMitAusschluss(_push_messungen())

    async def kaputt(*a, **k):
        raise RuntimeError("db weg")

    sp.speichere_auto_ausschluss = kaputt
    d = FytaSprungDetektor(sp, [_Zone("waldblumenhain"), _Zone("hecke")])
    befunde = await d.pruefe_und_melde(
        T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert len(befunde) == 1
    assert sp.warnungen, "Warnung muss trotz Speicher-Fehler rausgehen"
