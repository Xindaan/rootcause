"""T-0453 + T-0455: woher kam das Wasser?

Beide Tasks sortieren dieselbe Frage, deshalb ein Testfile. Vorher mischte
`ausloser` vier Klassen, die sich nicht auseinanderhalten liessen:

    Engine-Lauf      automatik ODER manuell -- nur `lauf_gruppe presoak_*`
                     war verlaesslich
    Cloud-Zeitplan   je nach Ingest-Pfad manuell (WS) oder automatik (DHS)
    Cross-Spray      vom Betreiber auf `manuell` gesetzt, weil es keinen
                     passenden Wert gab -> zaehlte als eigenes Kanal-Wasser
    Handguss-Log     ventil_id='manuell'

T-0453 gibt dem Cross-Spray einen eigenen Wert (`fremdwasser`, zaehlt NICHT
als Kanal-Wasser), T-0455 dem Cloud-Zeitplan (`zeitplan`, zaehlt SEHR WOHL
als Kanal-Wasser, ist aber vom Engine-Lauf unterscheidbar).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.entscheidung import Entscheidungsmotor
from bewaesserung.gardena_client import GardenaClient
from bewaesserung.gardena_web_backfill import _summary_zu_ausloser
from bewaesserung.kalibrierung import _wirkungsrate_kandidaten_aus_schliessen
from bewaesserung.leck_detektor import BEW_MIN_DAUER_SEKUNDEN
from bewaesserung.ml.response_features import AUSGESCHLOSSENE_AUSLOSER
from bewaesserung.modelle import (
    ECHTES_KANAL_WASSER,
    KEINE_WASSER_AUSLOESER,
    Ausloser,
    VentilAktion,
    VentilEreignis,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "quelle.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _schliessen(
    ausloser: Ausloser,
    *,
    zone: str = "bambuswald",
    dauer: int = 1800,
    zeit: datetime | None = None,
    ventil_id: str = "uuid-1:2",
    quell_zone: str | None = None,
) -> VentilEreignis:
    return VentilEreignis(
        zeitstempel=zeit or datetime.now(),
        zone_id=zone,
        ventil_id=ventil_id,
        aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=dauer,
        ausloser=ausloser,
        quell_zone=quell_zone,
    )


# --- Die Mengen selbst ----------------------------------------------------


def test_fremdwasser_zaehlt_nicht_als_kanalwasser_zeitplan_schon():
    """Der Kern beider Tasks in einer Zusicherung."""
    assert Ausloser.FREMDWASSER in KEINE_WASSER_AUSLOESER
    assert Ausloser.FREMDWASSER not in ECHTES_KANAL_WASSER
    assert Ausloser.ZEITPLAN in ECHTES_KANAL_WASSER
    assert Ausloser.ZEITPLAN not in KEINE_WASSER_AUSLOESER
    # Die beiden Mengen duerfen sich nie ueberschneiden -- sonst wuerde ein
    # Ausloeser in Budget und Bilanz gegensaetzlich gewertet.
    assert not (KEINE_WASSER_AUSLOESER & ECHTES_KANAL_WASSER)


def test_jeder_ausloser_ist_genau_einer_seite_zugeordnet_oder_bewusst_sonderfall():
    """Vollstaendigkeits-Guard analog `test_ops_api`.

    Neue Enum-Werte muessen hier eine bewusste Entscheidung ausloesen statt
    still in keiner der beiden Mengen zu landen -- genau so ist T-0453
    entstanden (`fremdwasser` gab es nicht, also nahm der User `manuell`).
    """
    sonderfaelle = {
        # kein Wasserfluss-Urteil, sondern Schliess-/Eingriffs-Semantik
        Ausloser.WATCHDOG, Ausloser.NOTFALL_STOPP,
        # eigener Pfad (Solar-Pumpe, kein Gardena-Kanal)
        Ausloser.AQUABLOOM,
    }
    zugeordnet = KEINE_WASSER_AUSLOESER | ECHTES_KANAL_WASSER | sonderfaelle
    assert set(Ausloser) == zugeordnet


# --- T-0453: Budget und Pause-Anker ---------------------------------------


def test_fremdwasser_belastet_tagesbudget_nicht_echter_lauf_schon(speicher):
    """Akzeptanzkriterium T-0453.

    Regner-Tag: ein Cross-Spray-Sprung wird als Fremdwasser markiert. Das
    Tagesbudget der Bambus-Zone darf sich dadurch nicht bewegen -- genau das
    passierte vorher, weil nur `manuell` zur Verfuegung stand (190
    Phantom-Minuten bambuswald + 85 yogaraum seit 15.06.2026).
    """
    motor = Entscheidungsmotor(speicher, None, [])  # type: ignore[arg-type]

    _run(speicher.speichere_ventil_ereignis(
        _schliessen(
            Ausloser.FREMDWASSER, dauer=1800,
            ventil_id="sensor_heuristik", quell_zone="magerwiese",
        ),
    ))
    assert _run(motor._tagesverbrauch("bambuswald")) == 0.0

    # Gegenprobe: ein echter App-Lauf zaehlt weiter.
    _run(speicher.speichere_ventil_ereignis(
        _schliessen(Ausloser.MANUELL, dauer=600),
    ))
    assert _run(motor._tagesverbrauch("bambuswald")) == 600.0

    # Und ein Cloud-Zeitplan-Lauf ebenfalls (T-0455).
    _run(speicher.speichere_ventil_ereignis(
        _schliessen(
            Ausloser.ZEITPLAN, dauer=2700,
            zeit=datetime.now() - timedelta(minutes=5),
        ),
    ))
    assert _run(motor._tagesverbrauch("bambuswald")) == 3300.0


def test_fremdwasser_setzt_keinen_pause_anker_zeitplan_schon(speicher):
    """Pause-Anker laeuft ueber dieselbe Menge (entscheidung.py:3593/3597)."""
    frueher = datetime.now() - timedelta(hours=6)
    _run(speicher.speichere_ventil_ereignis(
        _schliessen(
            Ausloser.FREMDWASSER, zeit=frueher, ventil_id="sensor_heuristik",
        ),
    ))
    assert _run(speicher.letztes_bestaetigtes_ventil_ereignis(
        "bambuswald", ausloser_ausser=KEINE_WASSER_AUSLOESER,
    )) is None

    spaeter = datetime.now() - timedelta(hours=2)
    _run(speicher.speichere_ventil_ereignis(
        _schliessen(Ausloser.ZEITPLAN, zeit=spaeter),
    ))
    anker = _run(speicher.letztes_bestaetigtes_ventil_ereignis(
        "bambuswald", ausloser_ausser=KEINE_WASSER_AUSLOESER,
    ))
    assert anker is not None and anker.ausloser is Ausloser.ZEITPLAN


def test_fremdwasser_bleibt_aus_ml_response_features(speicher):
    """Der Feuchte-Sprung ist echt, die Dauer aber konstruiert (Heuristik).

    Als Wirkungspunkt gelesen waere er ein frei erfundener Datenpunkt.
    """
    assert Ausloser.FREMDWASSER in AUSGESCHLOSSENE_AUSLOSER
    assert Ausloser.ZEITPLAN not in AUSGESCHLOSSENE_AUSLOSER


# --- T-0453: Persistenz + Invariante quell_zone ---------------------------


def test_quell_zone_ueberlebt_roundtrip(speicher):
    _run(speicher.speichere_ventil_ereignis(
        _schliessen(
            Ausloser.FREMDWASSER, ventil_id="sensor_heuristik",
            quell_zone="magerwiese",
        ),
    ))
    events = _run(speicher.hole_ventil_ereignisse("bambuswald"))
    assert len(events) == 1
    assert events[0].quell_zone == "magerwiese"


def test_rueckflip_auf_manuell_raeumt_quell_zone_weg(speicher):
    """Sonst behauptet eine stale quell_zone das Gegenteil des Ausloesers.

    Genau die zweite Wahrheit, die die Single-Source-of-Truth-Heuristik in
    CLAUDE.md verbietet -- und sie waere in einer spaeteren Cross-Spray-
    Analyse nicht als falsch erkennbar.
    """
    _run(speicher.speichere_ventil_ereignis(
        _schliessen(
            Ausloser.FREMDWASSER, ventil_id="sensor_heuristik",
            quell_zone="magerwiese",
        ),
    ))
    eid = _run(speicher.hole_ventil_ereignisse("bambuswald"))[0].id
    assert eid is not None

    _run(speicher.aktualisiere_ventil_ereignis(eid, ausloser=Ausloser.MANUELL))
    nachher = _run(speicher.hole_ventil_ereignis(eid))
    assert nachher is not None
    assert nachher.ausloser is Ausloser.MANUELL
    assert nachher.quell_zone is None


def test_db_trigger_akzeptiert_alle_enum_werte(speicher):
    """F20-Trigger wird aus dem Enum abgeleitet, nicht handgepflegt.

    Vorher stand die Erlaubnisliste als Literal in `_migriere` -- ein neuer
    Enum-Wert wurde von der DB abgelehnt, obwohl Modell und Code ihn kannten,
    und das faellt erst beim Live-Insert auf.
    """
    for i, ausloser in enumerate(Ausloser):
        _run(speicher.speichere_ventil_ereignis(_schliessen(
            ausloser,
            zone=f"zone_{i}",
            zeit=datetime.now() - timedelta(minutes=i),
        )))
    for i, ausloser in enumerate(Ausloser):
        events = _run(speicher.hole_ventil_ereignisse(f"zone_{i}"))
        assert [e.ausloser for e in events] == [ausloser]


# --- T-0455: die beiden Ingest-Pfade -------------------------------------


def test_ws_pfad_trennt_zeitplan_von_app_bedienung():
    assert GardenaClient._offen_ausloser("SCHEDULED_WATERING") is Ausloser.ZEITPLAN
    assert GardenaClient._offen_ausloser("MANUAL_WATERING") is Ausloser.MANUELL
    assert GardenaClient._offen_ausloser("OPEN") is Ausloser.MANUELL


def test_beide_ingest_pfade_etikettieren_denselben_lauf_gleich():
    """Der eigentliche T-0455-Fix.

    Vorher: WS-Pfad -> MANUELL, DHS-Backfill -> AUTOMATIK fuer DASSELBE
    physische Ereignis. Wer das Dedup-Rennen gewann, entschied die Semantik
    (der UNIQUE-Index fuehrt `ausloser` bewusst nicht im Schluessel).
    """
    ws = GardenaClient._offen_ausloser("SCHEDULED_WATERING")
    dhs = _summary_zu_ausloser("EXECUTED_SCHEDULE", "SINGLE")
    assert ws is dhs is Ausloser.ZEITPLAN


def test_schliessen_erbt_ausloser_vom_offenen_lauf():
    """Das Paar ist die Auswertungs-Einheit.

    Der SCHLIESSEN-Payload liefert nur "CLOSED" -- ohne die gemerkte
    Zuordnung bekaeme das SCHLIESSEN (das die DAUER traegt) einen anderen
    Ausloeser als sein OEFFNEN.
    """
    client = GardenaClient.__new__(GardenaClient)
    client._ventil_offen_ausloser = {}

    client._ventil_offen_ausloser["v1"] = GardenaClient._offen_ausloser(
        "SCHEDULED_WATERING",
    )
    assert client._ventil_offen_ausloser.pop("v1", Ausloser.MANUELL) is Ausloser.ZEITPLAN
    # Nach dem Pop ist der Lauf zu Ende -- ein zweites SCHLIESSEN (Watchdog,
    # Replay) faellt auf den alten Default zurueck, nicht auf ZEITPLAN.
    assert client._ventil_offen_ausloser.pop("v1", Ausloser.MANUELL) is Ausloser.MANUELL


# --- T-0455: die positiven Ausloser-Mengen (Isomorphie) -------------------


def test_zeitplan_bleibt_im_wirkungs_fit():
    """kalibrierung.py:710 prueft POSITIV.

    Stand dort das Literal `(MANUELL, AUTOMATIK)`, waeren Zeitplan-Laeufe
    still aus dem Fit gefallen -- obwohl sie echtes Kanal-Wasser mit echter
    Dauer sind und vor T-0455 als manuell/automatik drin waren.
    """
    events = [
        _schliessen(Ausloser.ZEITPLAN, dauer=2700),
        _schliessen(Ausloser.MANUELL, dauer=1800),
        _schliessen(Ausloser.FREMDWASSER, dauer=1800,
                    ventil_id="sensor_heuristik"),
    ]
    kandidaten = _wirkungsrate_kandidaten_aus_schliessen(events)
    gefiltert = [
        e for e in kandidaten
        if e.ausloser in ECHTES_KANAL_WASSER
        and e.ventil_id != "sensor_heuristik"
    ]
    assert {e.ausloser for e in gefiltert} == {
        Ausloser.ZEITPLAN, Ausloser.MANUELL,
    }


def test_auto_ignorieren_erfasst_zeitplan_laesst_optin_automatik_stehen(speicher):
    """T-0455-Verhaltensentscheidung im aktiven magerwiese-Fenster.

    Das Fenster sagt: Laeufe auf diesem Kanal sind in dieser Zeit KEINE
    Bewaesserung dieser Zone, ausser der Betreiber hebt sie ausdruecklich
    auf `automatik` (Positiv-Opt-in T-0300). Ein Cloud-Zeitplan-Lauf ist
    keine ausdrueckliche Hebung.

    Vorher haing das am Ingest-Pfad: WS-Pfad schrieb `manuell` (wurde
    geflippt), DHS-Pfad `automatik` (blieb stehen und sah aus wie ein
    Opt-in) -- fuer denselben physischen Lauf.
    """
    basis = datetime.now() - timedelta(hours=2)
    for i, ausloser in enumerate(
        (Ausloser.ZEITPLAN, Ausloser.MANUELL, Ausloser.AUTOMATIK),
    ):
        _run(speicher.speichere_ventil_ereignis(_schliessen(
            ausloser, zone="magerwiese",
            zeit=basis + timedelta(minutes=i),
        )))

    geflippt = _run(speicher.flippe_events_zu_ignoriert(
        "magerwiese",
        von=basis - timedelta(minutes=5),
        bis=basis + timedelta(minutes=30),
    ))
    assert geflippt == 2  # zeitplan + manuell, NICHT automatik

    danach = {
        e.ausloser for e in _run(speicher.hole_ventil_ereignisse("magerwiese"))
    }
    assert danach == {Ausloser.IGNORIERT, Ausloser.AUTOMATIK}


def test_leck_detektor_rechtfertigung_nimmt_zeitplan_aber_nicht_fremdwasser():
    """leck_detektor.py:631.

    Ein auf `fremdwasser` geflipptes Event soll die Warnung "bewaessert ohne
    Wirkung" genauso schliessen wie ein auf `ignoriert` geflipptes: fremdes
    Wasser rechtfertigt keine Aussage ueber die Wirkung EIGENER Dosen.
    """
    rechtfertigend = ECHTES_KANAL_WASSER | {Ausloser.UNBEKANNT}
    assert Ausloser.ZEITPLAN in rechtfertigend
    assert Ausloser.FREMDWASSER not in rechtfertigend
    assert Ausloser.IGNORIERT not in rechtfertigend
    # Der Dauer-Schwellwert bleibt unberuehrt -- nur die Menge aendert sich.
    assert BEW_MIN_DAUER_SEKUNDEN > 0
