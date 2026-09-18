"""T-0504: Tests fuer die periodische FYTA-Lueckenfuellung.

Der Bug: `hole_aktuelle_werte` fragt nur ein Zwei-Tage-Fenster ab. Ein
Sensor, der laenger offline war und dann seinen lokalen Speicher
hochlaedt, liefert Messungen mit ihrem ECHTEN Messdatum -- die fallen
aus dem Fenster und werden nie geholt. Einziger Rettungsanker war ein
Neustart innerhalb von sieben Tagen.

Getestet wird:
- die Speicher-Abfrage, die die Lueckenbreite ueberhaupt sichtbar macht
- die Fensterberechnung inkl. Deckel, Puffer und Konfig-Schnitt
- das Faelligkeits- und Fehlerverhalten des Jobs
- die Verdrahtung in den Entscheidungsloop (Regression gegen
  `fehlerpattern_job_nicht_in_loop_verdrahtet`)
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest

from bewaesserung.fyta_luecken_job import (
    BASIS_FENSTER_TAGE,
    PUFFER_TAGE,
    FytaLueckenJob,
)
from bewaesserung.modelle import (
    DatenQuelle,
    FytaKonfig,
    FytaPflanzenKonfig,
    SensorMessung,
)
from bewaesserung.speicher import Speicher


class _FakeFytaClient:
    """Minimaler FytaClient-Stub -- nur `_pflanzen_map` wird gebraucht."""

    def __init__(self, pflanzen: list[FytaPflanzenKonfig]):
        self._pflanzen_map = {p.fyta_id: p for p in pflanzen}
        self._konfig = FytaKonfig(
            api_url="https://fyta.example/api", pflanzen=pflanzen,
        )

    async def _stelle_token_sicher(self) -> bool:
        return True


@pytest.fixture
async def speicher(tmp_path):
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    yield s
    await s.schliessen()


async def _messung(speicher, geraet_id, zone_id, zeitstempel, quelle=DatenQuelle.FYTA):
    await speicher.speichere_messung(SensorMessung(
        zeitstempel=zeitstempel,
        zone_id=zone_id,
        geraet_id=geraet_id,
        boden_feuchte=42.0,
        quelle=quelle,
    ))


# --------------------------------------------------------------------
# Speicher-Abfrage
# --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_letzte_zeitstempel_pro_geraet_nimmt_maximum(speicher):
    """Pro Geraet der juengste Wert -- ueber Zonen- und Fenstergrenzen."""
    jetzt = datetime(2026, 8, 4, 10, 0)
    await _messung(speicher, "fyta_1", "zone_a", jetzt - timedelta(days=9))
    await _messung(speicher, "fyta_1", "zone_a", jetzt - timedelta(days=5))
    await _messung(speicher, "fyta_2", "zone_b", jetzt - timedelta(hours=1))

    ergebnis = await speicher.letzte_zeitstempel_pro_geraet(DatenQuelle.FYTA)

    assert ergebnis["fyta_1"] == jetzt - timedelta(days=5)
    assert ergebnis["fyta_2"] == jetzt - timedelta(hours=1)


@pytest.mark.asyncio
async def test_letzte_zeitstempel_ignoriert_fremde_quelle(speicher):
    """Gardena-Messungen duerfen das FYTA-Fenster nicht beeinflussen."""
    jetzt = datetime(2026, 8, 4, 10, 0)
    await _messung(speicher, "gardena_x", "zone_a", jetzt - timedelta(days=30),
                   quelle=DatenQuelle.GARDENA)
    await _messung(speicher, "fyta_1", "zone_a", jetzt)

    ergebnis = await speicher.letzte_zeitstempel_pro_geraet(DatenQuelle.FYTA)

    assert set(ergebnis) == {"fyta_1"}


@pytest.mark.asyncio
async def test_letzte_zeitstempel_kein_eintrag_ohne_messung(speicher):
    """Leeres Dict statt None-Werten -- der Aufrufer soll 'stumm seit X'
    von 'noch nie gesehen' unterscheiden koennen."""
    ergebnis = await speicher.letzte_zeitstempel_pro_geraet(DatenQuelle.FYTA)
    assert ergebnis == {}


# --------------------------------------------------------------------
# Fensterberechnung
# --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_basis_fenster_auch_wenn_alles_frisch(speicher):
    """Alles frisch heisst NICHT "nichts zu tun" -- ein Loch in der Mitte
    waere unsichtbar. Deshalb laeuft das Basis-Fenster immer."""
    jetzt = datetime(2026, 8, 4, 10, 0)
    await _messung(speicher, "fyta_1", "zone_a", jetzt - timedelta(hours=2))
    job = FytaLueckenJob(speicher, _FakeFytaClient([
        FytaPflanzenKonfig(fyta_id=1, zone_id="zone_a", name="A"),
    ]))

    assert await job.bestimme_fenster_tage(jetzt) == BASIS_FENSTER_TAGE


@pytest.mark.asyncio
async def test_fenster_waechst_ueber_das_basis_hinaus(speicher):
    """Stille laenger als das Basis-Fenster zieht es auf.

    Der Puffer obendrauf ist noetig, weil die FYTA-API nach UTC bucketet
    und die DB Beispielstadt-naiv speichert -- ohne ihn fehlt an der unteren
    Grenze ein 2-h-Band (verifiziert 04.08.2026: Abweichung pro Tag exakt
    8 Messungen = 2 h bei 15-min-Cadence). Er faengt zugleich das
    Abschneiden von `.days` ab.
    """
    jetzt = datetime(2026, 8, 4, 10, 0)
    await _messung(speicher, "fyta_1", "zone_a", jetzt - timedelta(days=12))
    await _messung(speicher, "fyta_2", "zone_b", jetzt - timedelta(hours=1))
    job = FytaLueckenJob(speicher, _FakeFytaClient([
        FytaPflanzenKonfig(fyta_id=1, zone_id="zone_a", name="A"),
        FytaPflanzenKonfig(fyta_id=2, zone_id="zone_b", name="B"),
    ]))

    # Massgeblich ist das AELTESTE Geraet, nicht der Durchschnitt.
    assert await job.bestimme_fenster_tage(jetzt) == 12 + PUFFER_TAGE


@pytest.mark.asyncio
async def test_fenster_wird_gedeckelt(speicher):
    """Ein monatelang stummer Sensor darf das Fenster nicht sprengen."""
    jetzt = datetime(2026, 8, 4, 10, 0)
    await _messung(speicher, "fyta_1", "zone_a", jetzt - timedelta(days=200))
    job = FytaLueckenJob(speicher, _FakeFytaClient([
        FytaPflanzenKonfig(fyta_id=1, zone_id="zone_a", name="A"),
    ]), deckel_tage=30)

    assert await job.bestimme_fenster_tage(jetzt) == 30


@pytest.mark.asyncio
async def test_unkonfiguriertes_geraet_reisst_fenster_nicht_auf(speicher):
    """Ein aus der Konfig entferntes Geraet bleibt mit seiner uralten
    letzten Messung in der DB stehen. Wuerde es zaehlen, liefe der Job
    fuer immer am Deckel -- die Frage 'welche Geraete zaehlen?'
    beantwortet die Konfig, nicht die Werteliste
    (fehlerpattern_fallback_an_messwert_statt_konfig).
    """
    jetzt = datetime(2026, 8, 4, 10, 0)
    await _messung(speicher, "fyta_99", "alt_zone", jetzt - timedelta(days=120))
    await _messung(speicher, "fyta_1", "zone_a", jetzt - timedelta(hours=1))
    job = FytaLueckenJob(speicher, _FakeFytaClient([
        FytaPflanzenKonfig(fyta_id=1, zone_id="zone_a", name="A"),
    ]), deckel_tage=30)

    # Basis-Fenster, NICHT der Deckel.
    assert await job.bestimme_fenster_tage(jetzt) == BASIS_FENSTER_TAGE


@pytest.mark.asyncio
async def test_konfiguriertes_geraet_ohne_messung_ist_keine_luecke(speicher):
    """Neu angelegter Sensor = Erstbefuellung, nicht Lueckenfuellung.
    Sonst zoege jeder neue Sensor das Fenster auf den Deckel."""
    jetzt = datetime(2026, 8, 4, 10, 0)
    await _messung(speicher, "fyta_1", "zone_a", jetzt - timedelta(hours=1))
    job = FytaLueckenJob(speicher, _FakeFytaClient([
        FytaPflanzenKonfig(fyta_id=1, zone_id="zone_a", name="A"),
        FytaPflanzenKonfig(fyta_id=2, zone_id="zone_neu", name="Neu"),
    ]), deckel_tage=30)

    # Basis-Fenster, NICHT der Deckel.
    assert await job.bestimme_fenster_tage(jetzt) == BASIS_FENSTER_TAGE


@pytest.mark.asyncio
async def test_loch_in_der_mitte_wird_nachgeholt(speicher):
    """DER Kernfall -- und der Grund fuer das Basis-Fenster.

    Ein Sensor war 30.07.-02.08. stumm und synct am 04.08. seinen ganzen
    Speicher hoch. Der 15-min-Poll holt davon die letzten zwei Tage. Ab
    da ist die LETZTE Messung wieder taufrisch -- das Loch davor bleibt
    aber offen, und ein Fenster, das nur "seit wann stumm?" misst, sieht
    es nie. Genau diesen Fall soll der Job abdecken; am 02.08. hat ihn
    nur ein zufaelliger Restart gerettet (T-0499, ~1.300 Messungen).
    """
    jetzt = datetime(2026, 8, 4, 20, 0)
    # Alt-Bestand bis 30.07., dann Loch, dann frische Poll-Daten.
    await _messung(speicher, "fyta_1", "zone_a", datetime(2026, 7, 30, 6, 30))
    await _messung(speicher, "fyta_1", "zone_a", jetzt - timedelta(hours=1))
    job = FytaLueckenJob(speicher, _FakeFytaClient([
        FytaPflanzenKonfig(fyta_id=1, zone_id="zone_a", name="A"),
    ]))

    fenster = await job.bestimme_fenster_tage(jetzt)

    # Muss die Tage 31.07.-02.08. mit abdecken, obwohl die juengste
    # Messung eine Stunde alt ist.
    assert fenster >= 5, (
        f"Fenster {fenster} laesst das Loch 31.07.-02.08. offen"
    )


@pytest.mark.asyncio
async def test_fenster_null_ohne_konfigurierte_pflanzen(speicher):
    job = FytaLueckenJob(speicher, _FakeFytaClient([]))
    assert await job.bestimme_fenster_tage(datetime(2026, 8, 4)) == 0


@pytest.mark.asyncio
async def test_basis_fenster_deckt_den_startup_hook_ab(speicher):
    """Das Basis-Fenster darf nicht kleiner sein als der 7-Tage-Anker,
    den es ersetzt -- sonst waere der Job ein Rueckschritt gegenueber
    dem blossen Neustart."""
    assert BASIS_FENSTER_TAGE >= 7


# --------------------------------------------------------------------
# Faelligkeit und Fehlerpfade
# --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_wird_mit_berechnetem_fenster_gerufen(speicher, monkeypatch):
    """Der Job reicht sein dynamisches Fenster an den Backfill durch --
    nicht die alten fixen sieben Tage."""
    jetzt = datetime(2026, 8, 4, 10, 0)
    await _messung(speicher, "fyta_1", "zone_a", jetzt - timedelta(days=9))
    job = FytaLueckenJob(speicher, _FakeFytaClient([
        FytaPflanzenKonfig(fyta_id=1, zone_id="zone_a", name="A"),
    ]))

    gerufen: list[int] = []

    async def _fake_backfill(sp, client, tage_zurueck):
        gerufen.append(tage_zurueck)
        return 17, 3

    monkeypatch.setattr(
        "bewaesserung.fyta_backfill.backfill_lueckenfuellung", _fake_backfill,
    )

    importiert, duplikate = await job.aktualisiere_wenn_faellig(jetzt)

    assert gerufen == [9 + PUFFER_TAGE]
    assert (importiert, duplikate) == (17, 3)


@pytest.mark.asyncio
async def test_zweiter_lauf_im_intervall_macht_nichts(speicher, monkeypatch):
    jetzt = datetime(2026, 8, 4, 10, 0)
    await _messung(speicher, "fyta_1", "zone_a", jetzt - timedelta(days=9))
    job = FytaLueckenJob(speicher, _FakeFytaClient([
        FytaPflanzenKonfig(fyta_id=1, zone_id="zone_a", name="A"),
    ]), intervall_stunden=24)

    aufrufe: list[int] = []

    async def _fake_backfill(sp, client, tage_zurueck):
        aufrufe.append(tage_zurueck)
        return 1, 0

    monkeypatch.setattr(
        "bewaesserung.fyta_backfill.backfill_lueckenfuellung", _fake_backfill,
    )

    await job.aktualisiere_wenn_faellig(jetzt)
    await job.aktualisiere_wenn_faellig(jetzt + timedelta(hours=5))
    assert len(aufrufe) == 1

    await job.aktualisiere_wenn_faellig(jetzt + timedelta(hours=25))
    assert len(aufrufe) == 2


@pytest.mark.asyncio
async def test_backfill_fehler_crasht_nicht_und_blockiert_nicht(speicher, monkeypatch):
    """Der Aufrufer ist der Entscheidungsloop. Ein Fehler darf ihn weder
    reissen noch den Job in eine Dauerschleife schicken -- die
    Faelligkeit wird VOR der Arbeit gestempelt."""
    jetzt = datetime(2026, 8, 4, 10, 0)
    await _messung(speicher, "fyta_1", "zone_a", jetzt - timedelta(days=9))
    job = FytaLueckenJob(speicher, _FakeFytaClient([
        FytaPflanzenKonfig(fyta_id=1, zone_id="zone_a", name="A"),
    ]))

    aufrufe = []

    async def _kaputt(sp, client, tage_zurueck):
        aufrufe.append(tage_zurueck)
        raise RuntimeError("FYTA down")

    monkeypatch.setattr(
        "bewaesserung.fyta_backfill.backfill_lueckenfuellung", _kaputt,
    )

    assert await job.aktualisiere_wenn_faellig(jetzt) == (0, 0)
    assert job.letzter_fehler == "FYTA down"

    # Direkt danach nicht erneut -- sonst haemmert der Loop alle 5 min.
    await job.aktualisiere_wenn_faellig(jetzt + timedelta(minutes=5))
    assert len(aufrufe) == 1


@pytest.mark.asyncio
async def test_kein_backfill_ohne_konfigurierte_pflanzen(speicher, monkeypatch):
    """Ohne FYTA-Konfiguration gibt es nichts abzufragen -- kein
    API-Verkehr, auch nicht fuer das Basis-Fenster."""
    jetzt = datetime(2026, 8, 4, 10, 0)
    job = FytaLueckenJob(speicher, _FakeFytaClient([]))

    async def _darf_nicht_laufen(sp, client, tage_zurueck):
        raise AssertionError("Backfill ohne Pflanzen gerufen")

    monkeypatch.setattr(
        "bewaesserung.fyta_backfill.backfill_lueckenfuellung", _darf_nicht_laufen,
    )

    assert await job.aktualisiere_wenn_faellig(jetzt) == (0, 0)


# --------------------------------------------------------------------
# Verdrahtung
# --------------------------------------------------------------------

def test_job_ist_im_entscheidungsloop_verdrahtet():
    """Regression gegen `fehlerpattern_job_nicht_in_loop_verdrahtet`:
    ein Job, den niemand tickt, ist toter Code -- und ein NameError im
    Loop laesst alle NACHFOLGENDEN Jobs still ausfallen.

    Geprueft wird die ganze Kette: Parameter, Aufruf, und dass die
    Variable in `main()` vor dem `if konfig.fyta`-Zweig existiert.
    """
    from bewaesserung import main as main_modul

    quelle = inspect.getsource(main_modul._entscheidungsloop)
    assert "fyta_luecken_job" in inspect.signature(
        main_modul._entscheidungsloop
    ).parameters
    assert "await fyta_luecken_job.aktualisiere_wenn_faellig()" in quelle

    main_quelle = inspect.getsource(main_modul)
    # Ohne diese Vorab-Initialisierung wirft der Loop bei fehlender
    # FYTA-Konfiguration NameError.
    assert "fyta_luecken_job = None" in main_quelle
    assert "fyta_luecken_job=fyta_luecken_job" in main_quelle
