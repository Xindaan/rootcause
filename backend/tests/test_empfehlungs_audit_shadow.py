"""T-0349/T-0351/T-0353: Tests fuer die Shadow-Phasen des Audit-Jobs.

Deckt ab: Snapshot-Persistenz der State-Space-/Heuristik-/Routing-
Felder, Eval-Abweichungen, 24h-Nachzug (eigener UPDATE-Pfad) und den
lag-gated Regime-Stempel-Pass.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.empfehlungs_audit_job import EmpfehlungsAuditJob
from bewaesserung.modelle import (
    Ausloser,
    BlockerTyp,
    DatenQuelle,
    GardenaKonfig,
    GesamtKonfig,
    GiessEmpfehlung,
    MlForecastRoutingKonfig,
    SensorMessung,
    StandortKonfig,
    VentilAktion,
    VentilEreignis,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "audit_shadow.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


class _MotorStub:
    def __init__(self, empfehlungen: dict[str, GiessEmpfehlung]):
        self._empfehlungen = empfehlungen

    async def vorhersage_zone(self, zone_id: str) -> GiessEmpfehlung:
        return self._empfehlungen[zone_id]


def _konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=[
            ZonenKonfig(
                zone_id="z1", name="z1", modus="monitoring", ventil_kanal=1,
                feuchte_schwelle_min=30, feuchte_schwelle_max=60,
                feuchte_kritisch=20,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[
                WetterStandortKonfig(id="standort_a", breite=52.52, laenge=13.405),
            ],
        ),
        standorte=[
            StandortKonfig(
                standort_id="standort_a", name="Standort A",
                wetter_standort="standort_a", zonen=["z1"],
            ),
        ],
        ml_forecast_routing=MlForecastRoutingKonfig(
            aktiv=True, zonen={"z1": "statespace"},
        ),
    )


def _empfehlung(feuchte=50.0, **kw) -> GiessEmpfehlung:
    basis = dict(
        zone_id="z1", zeitstempel=datetime.now(),
        soll_bewaessern=False, blocker_typ=BlockerTyp.ZEITFENSTER,
        grund="Test", feuchte_aktuell=feuchte,
        prognose_quelle="ml", prognose_6h=48.0, prognose_12h=45.0,
        prognose_24h=42.0, empfehlungs_typ="praeventiv",
    )
    basis.update(kw)
    return GiessEmpfehlung(**basis)


def test_snapshot_persistiert_shadow_felder(speicher):
    """Statespace-/Heuristik-/Routing-Felder landen im Audit-Snapshot."""
    empf = _empfehlung(
        prognose_statespace_6h=47.5,
        prognose_statespace_12h=44.0,
        prognose_statespace_24h=40.5,
        statespace_quelle="gefittet+wirkung",
        decay_heuristik_pp_pro_tag=4.0,
    )
    job = EmpfehlungsAuditJob(
        speicher, _MotorStub({"z1": empf}), zone_ids=["z1"],
        konfig=_konfig(),
    )
    stats = _run(job.aktualisiere_wenn_faellig())
    assert stats["snapshots"] == 1
    zeile = _run(speicher.hole_empfehlungs_audit(tage=1))[0]
    assert zeile["prognose_statespace_6h"] == 47.5
    assert zeile["prognose_statespace_24h"] == 40.5
    assert zeile["statespace_quelle"] == "gefittet+wirkung"
    # T-0351: 50.0 - 4.0 = 46.0.
    assert zeile["prognose_heuristik_24h"] == 46.0
    # Routing wollte statespace, statespace_6h ist da -> "statespace".
    assert zeile["routing_quelle"] == "statespace"


def test_snapshot_ohne_shadow_bleibt_null(speicher):
    """Ohne Shadow-Daten bleiben die neuen Spalten NULL (kein Raten)."""
    job = EmpfehlungsAuditJob(
        speicher, _MotorStub({"z1": _empfehlung()}), zone_ids=["z1"],
    )
    _run(job.aktualisiere_wenn_faellig())
    zeile = _run(speicher.hole_empfehlungs_audit(tage=1))[0]
    assert zeile["prognose_statespace_6h"] is None
    assert zeile["statespace_quelle"] is None
    assert zeile["prognose_heuristik_24h"] is None
    assert zeile["routing_quelle"] is None


def _msg(zeit: datetime, feuchte: float, zone: str = "z1") -> SensorMessung:
    return SensorMessung(
        zeitstempel=zeit, zone_id=zone, geraet_id="g1",
        boden_feuchte=feuchte, quelle=DatenQuelle.GARDENA,
    )


def test_eval_und_nachzug_24h(speicher):
    """6h-Eval schreibt statespace-6h-Abweichung; der 24h-Nachzug traegt
    spaeter ist_feuchte_24h + alle 24h-Abweichungen nach, OHNE die
    6h-Felder oder evaluiert_am anzufassen (Verifier-Finding F4)."""
    basis = datetime.now() - timedelta(hours=30)
    _run(speicher.setze_empfehlungs_audit(
        zeitstempel=basis, zone_id="z1", empfehlungs_typ="praeventiv",
        soll_bewaessern=False, blocker_typ=None, feuchte_aktuell=50.0,
        welkepunkt_wert=30.0, optimum_min=40.0, optimum_max=60.0,
        prognose_quelle="ml", prognose_6h=48.0, prognose_12h=45.0,
        prognose_24h=42.0, tage_bis_welkepunkt=3.0,
        dauer_s_empfehlung=None, aktive_strategie="korridor",
        prognose_statespace_6h=47.0, prognose_statespace_12h=44.0,
        prognose_statespace_24h=41.0, statespace_quelle="gefittet+wirkung",
        prognose_heuristik_24h=46.0,
    ))
    # Sensor-Wert NUR bei +6h -> Eval schliesst mit ist_24h=None ab.
    _run(speicher.speichere_messung(_msg(basis + timedelta(hours=6), 46.0)))
    job = EmpfehlungsAuditJob(
        speicher, _MotorStub({}), zone_ids=[],
    )
    eval_zeit = basis + timedelta(hours=7)
    evals = _run(job._evaluiere_offene(eval_zeit))
    assert evals == 1
    zeile = _run(speicher.hole_empfehlungs_audit(tage=3))[0]
    assert zeile["ist_feuchte_6h"] == 46.0
    assert zeile["ist_feuchte_24h"] is None
    assert zeile["abweichung_statespace_6h"] == pytest.approx(1.0)
    assert zeile["abweichung_statespace_24h"] is None
    evaluiert_am_vorher = zeile["evaluiert_am"]

    # +24h-Messung kommt spaeter -> Nachzug findet sie.
    _run(speicher.speichere_messung(_msg(basis + timedelta(hours=24), 40.0)))
    nachzuege = _run(job._nachzug_24h(basis + timedelta(hours=26)))
    assert nachzuege == 1
    zeile = _run(speicher.hole_empfehlungs_audit(tage=3))[0]
    assert zeile["ist_feuchte_24h"] == 40.0
    assert zeile["abweichung_24h"] == pytest.approx(2.0)
    assert zeile["abweichung_statespace_24h"] == pytest.approx(1.0)
    assert zeile["abweichung_heuristik_24h"] == pytest.approx(6.0)
    # 6h-Felder + evaluiert_am unangetastet.
    assert zeile["ist_feuchte_6h"] == 46.0
    assert zeile["abweichung_statespace_6h"] == pytest.approx(1.0)
    assert zeile["evaluiert_am"] == evaluiert_am_vorher
    # Nachzug ist idempotent: Zeile faellt aus dem Kandidaten-Set.
    assert _run(job._nachzug_24h(basis + timedelta(hours=27))) == 0


def _audit_zeile(speicher, basis: datetime) -> None:
    _run(speicher.setze_empfehlungs_audit(
        zeitstempel=basis, zone_id="z1", empfehlungs_typ="kein_bedarf",
        soll_bewaessern=False, blocker_typ=None, feuchte_aktuell=50.0,
        welkepunkt_wert=30.0, optimum_min=40.0, optimum_max=60.0,
        prognose_quelle="ml", prognose_6h=48.0, prognose_12h=45.0,
        prognose_24h=42.0, tage_bis_welkepunkt=3.0,
        dauer_s_empfehlung=None, aktive_strategie="korridor",
    ))


def test_regime_stempel_lag_gated(speicher):
    """Ohne Wetter-Abdeckung wird trocknung/regen NICHT gestempelt
    (ERA5-Lag, Verifier-Finding F1); giess_recovery braucht kein Wetter.
    Mit Abdeckung wird trocknung gestempelt."""
    konfig = _konfig()
    basis = datetime.now() - timedelta(days=3)
    _audit_zeile(speicher, basis)
    job = EmpfehlungsAuditJob(
        speicher, _MotorStub({}), zone_ids=[], konfig=konfig,
    )
    # 1) Kein Wetter-Archiv -> Klassifikation waere regen_unbekannt
    #    wegen fehlender Abdeckung -> KEIN Stempel.
    assert _run(job._stemple_regimes(datetime.now())) == 0
    zeile = _run(speicher.hole_empfehlungs_audit(tage=7))[0]
    assert zeile["regime_6h"] is None

    # 2) Eigener Giesslauf im Fenster -> Stempel OHNE Wetter moeglich.
    _run(speicher.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=basis + timedelta(hours=2), zone_id="z1",
        ventil_id="v1", aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=1800, ausloser=Ausloser.MANUELL,
    )))
    assert _run(job._stemple_regimes(datetime.now())) == 1
    zeile = _run(speicher.hole_empfehlungs_audit(tage=7))[0]
    assert zeile["regime_6h"] == "giess_recovery"
    assert zeile["regime_24h"] == "giess_recovery"


def test_regime_stempel_trocknung_mit_wetterabdeckung(speicher):
    konfig = _konfig()
    basis = datetime.now() - timedelta(days=3)
    _audit_zeile(speicher, basis)
    # Wetter-Archiv deckt [basis-6h, basis+24h] ab, kein Regen.
    async def _fuelle_wetter():
        for h in range(-6, 26):
            await speicher._db.execute(
                "INSERT OR IGNORE INTO wetter_archiv "
                "(zeitstempel, standort_id, niederschlag_mm, temperatur, "
                "et0_mm, abgerufen_am) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (basis + timedelta(hours=h)).isoformat(),
                    "standort_a", 0.0, 20.0, 0.1, datetime.now().isoformat(),
                ),
            )
        await speicher._db.commit()
    _run(_fuelle_wetter())
    job = EmpfehlungsAuditJob(
        speicher, _MotorStub({}), zone_ids=[], konfig=konfig,
    )
    assert _run(job._stemple_regimes(datetime.now())) == 1
    zeile = _run(speicher.hole_empfehlungs_audit(tage=7))[0]
    assert zeile["regime_6h"] == "trocknung"
    assert zeile["regime_24h"] == "trocknung"
