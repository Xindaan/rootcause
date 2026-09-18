"""T-0549: ein Testlauf zaehlt erst, wenn die HAUPTDOSE gelaufen ist.

**Der Realfall.** Von 24 Laeufen der T-0535-Messreihe endeten zwei nach dem
Pre-Soak:

  Lauf  8 (15.08., Stufe 60): `waldblumenhain` belegte den Hahn ab 20:44
                              fuer 2,5 h -- die Hauptdose bekam den Kanal nie.
  Lauf 12 (19.08., Stufe 45): nach dem Pre-Soak kam die Hauptdose nicht,
                              zwei Stunden spaeter startete ein neuer Zyklus.

Beide hatten ihren Zaehler-Slot trotzdem verbraucht. Netto 22 verwertbare
Laeufe, Blockbalance 7/7/8 statt 8/8/8 -- ueber acht Prozent der Reihe.

**Der Modulkopf von `dosis_test.py` hatte das Prinzip schon richtig**
("eine blosse Entscheidung ist noch kein Wasser"), die Abgrenzung griff nur
eine Stufe zu frueh: ein gestarteter Pre-Soak ist ebenfalls noch nicht die
Dosis, um die es geht.

Zwei Waechter, jeder mit eigener Negativprobe:
  1. Der Buchungs-ZEITPUNKT haengt am ersten Haupt-Puls (Callback).
  2. Die Buchung selbst ist idempotent (Lauf-Gruppe schon verbucht?).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import Ausloser
from bewaesserung.pre_soak import PreSoakManager
from bewaesserung.speicher import Speicher


T0 = datetime(2026, 8, 27, 6, 0)


class _Sicherung:
    """Ventil-Attrappe; `haupt_schlaegt_fehl` simuliert einen belegten Hahn."""

    def __init__(self, haupt_schlaegt_fehl: bool = False) -> None:
        self.aufrufe: list[tuple] = []
        self._haupt_fehlt = haupt_schlaegt_fehl

    async def bewaessere(
        self, kanal, zone_ids, dauer_s, ausloser,
        *, lauf_gruppe=None, phase=None,
    ) -> bool:
        self.aufrufe.append((phase, dauer_s))
        if phase == "haupt" and self._haupt_fehlt:
            return False
        return True

    async def stoppe(self, kanal, ausloser) -> bool:
        return True

    def ist_aktiv(self, kanal) -> bool:
        return False


async def _starte(mgr, haupt_min=36):
    ok, fehler = await mgr.starte(
        zone_id="bambuswald", kanal=2, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=20, haupt_min=haupt_min,
        ausloser=Ausloser.AUTOMATIK,
    )
    assert ok, fehler
    return mgr.laufender_lauf("bambuswald")


def _mgr(sicherung, gebucht: list):
    async def callback(lauf):
        gebucht.append((lauf.zone_id, lauf.lauf_gruppe, lauf.haupt_s))

    return PreSoakManager(sicherung, haupt_start_callback=callback)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Waechter 1: der Zeitpunkt
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_soak_start_bucht_noch_nicht():
    """Der Kern von T-0549. Bis hierher lief nur der Anfeucht-Puls."""
    gebucht: list = []
    mgr = _mgr(_Sicherung(), gebucht)
    await _starte(mgr)
    assert gebucht == []


@pytest.mark.asyncio
async def test_hauptdose_bucht_genau_einmal():
    gebucht: list = []
    mgr = _mgr(_Sicherung(), gebucht)
    lauf = await _starte(mgr)
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=21))
    assert len(gebucht) == 1
    zone_id, gruppe, haupt_s = gebucht[0]
    assert zone_id == "bambuswald"
    assert gruppe == lauf.lauf_gruppe
    assert haupt_s == 36 * 60


@pytest.mark.asyncio
async def test_der_realfall_hahn_belegt_bucht_nicht():
    """Lauf 8 vom 15.08.: der Pre-Soak lief, die Hauptdose bekam den Kanal
    nicht. Genau dieser Lauf darf keinen Slot verbrauchen."""
    gebucht: list = []
    mgr = _mgr(_Sicherung(haupt_schlaegt_fehl=True), gebucht)
    lauf = await _starte(mgr)
    for minute in (21, 25, 30):
        await mgr.tick(lauf.gestartet_am + timedelta(minutes=minute))
    assert gebucht == []


@pytest.mark.asyncio
async def test_der_realfall_sequenz_bricht_ab_bucht_nicht():
    """Lauf 12 vom 19.08.: nach dem Pre-Soak kam gar nichts mehr."""
    gebucht: list = []
    mgr = _mgr(_Sicherung(), gebucht)
    lauf = await _starte(mgr)
    await mgr.stoppe("bambuswald")
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=21))
    assert gebucht == []


@pytest.mark.asyncio
async def test_folge_pulse_buchen_nicht_erneut():
    """Cycle-and-Soak: drei Haupt-Pulse sind EIN Lauf, nicht drei."""
    gebucht: list = []
    sicherung = _Sicherung()
    mgr = PreSoakManager(  # type: ignore[arg-type]
        sicherung,
        haupt_start_callback=lambda lauf: _sammle(gebucht, lauf),
    )
    ok, fehler = await mgr.starte(
        zone_id="bambuswald", kanal=2, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=20, haupt_min=30,
        ausloser=Ausloser.AUTOMATIK, haupt_pulse=3, haupt_puls_pause_min=5,
    )
    assert ok, fehler
    lauf = mgr.laufender_lauf("bambuswald")
    for minute in (21, 40, 60, 80):
        await mgr.tick(lauf.gestartet_am + timedelta(minutes=minute))
    assert len(gebucht) == 1
    assert sum(1 for phase, _ in sicherung.aufrufe if phase == "haupt") >= 2


async def _sammle(ziel: list, lauf) -> None:
    ziel.append((lauf.zone_id, lauf.lauf_gruppe, lauf.haupt_s))


@pytest.mark.asyncio
async def test_wiederhergestellter_lauf_bucht_nach_neustart_nicht_erneut():
    """Nach einem Neustart traegt ein Lauf in der Haupt-Phase den Zaehler
    bereits -- er darf nicht ein zweites Mal gemeldet werden."""
    gebucht: list = []
    mgr = _mgr(_Sicherung(), gebucht)
    lauf = await _starte(mgr)
    lauf.phase = "haupt"
    lauf.haupt_pulse_gestartet = 0  # wie ein Alt-State ohne den Zaehler
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=25))
    assert gebucht == []


@pytest.mark.asyncio
async def test_fehler_im_callback_haelt_die_bewaesserung_nicht_an():
    """Die Buchung ist Buchhaltung. Sie darf niemals Wasser verhindern."""
    async def kaputt(lauf):
        raise RuntimeError("DB weg")

    sicherung = _Sicherung()
    mgr = PreSoakManager(sicherung, haupt_start_callback=kaputt)  # type: ignore[arg-type]
    lauf = await _starte(mgr)
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=21))
    assert any(phase == "haupt" for phase, _ in sicherung.aufrufe)
    assert mgr.laufender_lauf("bambuswald").phase == "haupt"


@pytest.mark.asyncio
async def test_negativprobe_ohne_callback_laeuft_alles_normal_weiter():
    """Negativprobe zu Waechter 1: ohne Callback wird nichts gebucht, aber
    die Sequenz laeuft unveraendert. Belegt, dass die Buchung angehaengt und
    nicht in den Giess-Pfad verwoben ist."""
    sicherung = _Sicherung()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    lauf = await _starte(mgr)
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=21))
    assert any(phase == "haupt" for phase, _ in sicherung.aufrufe)


# --------------------------------------------------------------------------
# Waechter 2: Idempotenz der Buchung selbst
# --------------------------------------------------------------------------

@pytest.fixture
async def speicher(tmp_path):
    sp = Speicher(str(tmp_path / "t0549.db"))
    await sp.verbinden()
    try:
        yield sp
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_lauf_gruppe_wird_nur_einmal_verbucht(speicher):
    from bewaesserung.dosis_test import DosisTestKonfig, verbuche_lauf
    from bewaesserung.modelle import ZonenKonfig

    konfig = DosisTestKonfig(
        aktiv=True, ventil_geraet_id="dswc1", ventil_kanal=2,
        stufen_gesamt_min=(45, 60, 75), wiederholungen=8, seed=1,
    )
    zone = ZonenKonfig(
        zone_id="bambuswald", name="Bambuswald", ventil_kanal=2,
        ventil_geraet_id="dswc1", pre_soak_min=5,
    )
    erste = await verbuche_lauf(
        speicher, konfig, zone, "gruppe-1", T0, haupt_sekunden_ist=2160,
    )
    zweite = await verbuche_lauf(
        speicher, konfig, zone, "gruppe-1", T0, haupt_sekunden_ist=2160,
    )
    assert erste is True
    assert zweite is False
    assert await speicher.zaehle_dosis_test_laeufe("dswc1", 2) == 1


@pytest.mark.asyncio
async def test_negativprobe_ohne_gruppen_pruefung_zaehlt_doppelt(speicher):
    """Negativprobe zu Waechter 2: nimmt man die Pruefung heraus, zaehlt
    derselbe Lauf zweimal -- genau davor schuetzt sie."""
    from bewaesserung.dosis_test import DosisTestKonfig, verbuche_lauf
    from bewaesserung.modelle import ZonenKonfig

    konfig = DosisTestKonfig(
        aktiv=True, ventil_geraet_id="dswc1", ventil_kanal=2,
        stufen_gesamt_min=(45, 60, 75), wiederholungen=8, seed=1,
    )
    zone = ZonenKonfig(
        zone_id="bambuswald", name="Bambuswald", ventil_kanal=2,
        ventil_geraet_id="dswc1", pre_soak_min=5,
    )
    # Ohne Gruppe greift die Dedup-Pruefung bewusst nicht.
    await verbuche_lauf(speicher, konfig, zone, None, T0, haupt_sekunden_ist=2160)
    await verbuche_lauf(speicher, konfig, zone, None, T0, haupt_sekunden_ist=2160)
    assert await speicher.zaehle_dosis_test_laeufe("dswc1", 2) == 2


@pytest.mark.asyncio
async def test_abgebrochener_lauf_gibt_die_stufe_wieder_frei(speicher):
    """Akzeptanzkriterium 2: bricht die Sequenz vor der Hauptdose ab, bekommt
    der NAECHSTE Lauf dieselbe Stufe."""
    from bewaesserung.dosis_test import DosisTestKonfig, stufe_fuer_lauf

    konfig = DosisTestKonfig(
        aktiv=True, ventil_geraet_id="dswc1", ventil_kanal=2,
        stufen_gesamt_min=(45, 60, 75), wiederholungen=8, seed=1,
    )
    index_vorher = await speicher.zaehle_dosis_test_laeufe("dswc1", 2)
    stufe_vorher = stufe_fuer_lauf(konfig, index_vorher, T0.date())
    # Abbruch nach dem Pre-Soak -> keine Buchung -> Index unveraendert
    index_nachher = await speicher.zaehle_dosis_test_laeufe("dswc1", 2)
    assert index_nachher == index_vorher
    assert stufe_fuer_lauf(konfig, index_nachher, T0.date()) == stufe_vorher
