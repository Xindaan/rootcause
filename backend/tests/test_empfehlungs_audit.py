"""T-0122: Tests fuer den Empfehlungs-Audit-Job + Speicher-Integration."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.empfehlungs_audit_job import EmpfehlungsAuditJob
from bewaesserung.modelle import (
    BewaesserungsStrategie,
    BlockerTyp,
    GiessEmpfehlung,
    SensorMessung,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "audit.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


class _MotorStub:
    """Minimaler Entscheidungsmotor-Ersatz."""

    def __init__(self, empfehlungen_pro_zone: dict[str, GiessEmpfehlung]):
        self._empfehlungen = empfehlungen_pro_zone
        self.aufrufe: list[str] = []

    async def vorhersage_zone(self, zone_id: str) -> GiessEmpfehlung:
        self.aufrufe.append(zone_id)
        return self._empfehlungen[zone_id]


def _empfehlung(
    zone_id: str = "bambuswald",
    typ: str = "praeventiv",
    feuchte: float = 50.0,
    prog_6h: float = 48.0,
    prog_24h: float = 42.0,
    quelle: str = "ml",
    prog_physik_6h: float | None = None,
    prog_physik_24h: float | None = None,
    physik_quelle: str = "keine",
    k_basis_pro_h: float | None = None,
) -> GiessEmpfehlung:
    return GiessEmpfehlung(
        zone_id=zone_id,
        zeitstempel=datetime.now(),
        soll_bewaessern=False,
        blocker_typ=BlockerTyp.ZEITFENSTER,
        grund="Test",
        feuchte_aktuell=feuchte,
        welkepunkt_wert=32.0,
        optimum_min=40.0,
        optimum_max=60.0,
        prognose_quelle=quelle,
        prognose_6h=prog_6h,
        prognose_12h=(prog_6h + prog_24h) / 2,
        prognose_24h=prog_24h,
        tage_bis_welkepunkt=2.5,
        empfehlungs_typ=typ,
        aktive_strategie=BewaesserungsStrategie.HAEUFIG_KLEIN.value,
        dauer_s_empfehlung=1800,
        erklarung_kurz="kurz",
        erklarung_lang="lang",
        # T-0270: Physik-Diagnose-Felder fuer Bias-Audit-Tests.
        prognose_physik_6h=prog_physik_6h,
        prognose_physik_12h=(
            (prog_physik_6h + prog_physik_24h) / 2
            if prog_physik_6h is not None and prog_physik_24h is not None
            else None
        ),
        prognose_physik_24h=prog_physik_24h,
        physik_quelle=physik_quelle,
        k_basis_pro_h=k_basis_pro_h,
    )


def _msg(zeit: datetime, feuchte: float, zone: str = "bambuswald"):
    return SensorMessung(
        zeitstempel=zeit, zone_id=zone,
        boden_feuchte=feuchte, boden_temperatur=15.0, batterie_prozent=95.0,
    )


def test_snapshot_persistiert_pro_zone(speicher):
    motor = _MotorStub({
        "bambuswald": _empfehlung(),
        "waldblumenhain": _empfehlung("waldblumenhain", typ="kein_bedarf"),
    })
    job = EmpfehlungsAuditJob(
        speicher, motor, zone_ids=["bambuswald", "waldblumenhain"],
    )
    stats = _run(job.aktualisiere_wenn_faellig())
    assert stats["snapshots"] == 2
    eintraege = _run(speicher.hole_empfehlungs_audit(tage=1))
    assert len(eintraege) == 2
    typen = {e["zone_id"]: e["empfehlungs_typ"] for e in eintraege}
    assert typen == {"bambuswald": "praeventiv", "waldblumenhain": "kein_bedarf"}


def test_t0158_hole_audit_nutzt_jetzt_parameter_unabhaengig_von_realzeit(speicher):
    """T-0158 (Folge T-0166): `hole_empfehlungs_audit(jetzt=X)` muss
    den Datums-Filter relativ zu X aufbauen, nicht zu `datetime.now()`.

    Regression: bei jetzt = 2020-01-15 12:00 muss ein Audit-Eintrag
    von 2020-01-14 (1 Tag vorher) gefunden werden, auch wenn die
    Realzeit Jahre spaeter ist. Vorher: Speicher rechnete
    `datetime.now() - timedelta(days=N)` und filterte alle Eintraege
    aus der Vergangenheit weg.
    """
    historisches_jetzt = datetime(2020, 1, 15, 12, 0)
    vor_24h = historisches_jetzt - timedelta(hours=24)

    # Audit-Eintrag manuell schreiben mit explizitem alten Zeitstempel.
    _run(speicher.setze_empfehlungs_audit(
        zeitstempel=vor_24h,
        zone_id="bambuswald",
        empfehlungs_typ="akut",
        soll_bewaessern=False,
        blocker_typ=None,
        feuchte_aktuell=25.0,
        welkepunkt_wert=18.0,
        optimum_min=30.0,
        optimum_max=60.0,
        prognose_quelle="ml",
        prognose_6h=22.0,
        prognose_12h=20.0,
        prognose_24h=18.0,
        tage_bis_welkepunkt=2.0,
        dauer_s_empfehlung=1800,
        aktive_strategie="haeufig_klein",
    ))

    # Ohne jetzt-Parameter: filtert nach Realzeit, findet Eintrag NICHT.
    ohne_jetzt = _run(speicher.hole_empfehlungs_audit(tage=2))
    assert len(ohne_jetzt) == 0, (
        "Sanity: Audit-Eintrag aus 2020 darf bei Realzeit-Filter nicht "
        "erscheinen (Tests laufen 2026+)."
    )

    # Mit jetzt-Parameter: filtert relativ zur Test-Referenzzeit, findet Eintrag.
    mit_jetzt = _run(speicher.hole_empfehlungs_audit(
        tage=2, jetzt=historisches_jetzt,
    ))
    assert len(mit_jetzt) == 1, (
        "T-0158: hole_empfehlungs_audit muss den `jetzt`-Parameter "
        "fuer den Datums-Filter respektieren, sonst sind Watchdog-Tests "
        "datums-flaky (4-h-Fenster ab Test-Anchor)."
    )
    assert mit_jetzt[0]["empfehlungs_typ"] == "akut"


def test_intervall_blockt_doppel_lauf(speicher):
    motor = _MotorStub({"bambuswald": _empfehlung()})
    job = EmpfehlungsAuditJob(
        speicher, motor, zone_ids=["bambuswald"], intervall_minuten=60,
    )
    s1 = _run(job.aktualisiere_wenn_faellig())
    s2 = _run(job.aktualisiere_wenn_faellig())
    assert s1["snapshots"] == 1
    assert s2["snapshots"] == 0  # Intervall-Gate


def test_eval_fuellt_ist_feuchte(speicher):
    """Snapshot vor 7 h, Sensor-Wert nach 6 h verfuegbar -> ist + abweichung."""
    motor = _MotorStub({"bambuswald": _empfehlung(prog_6h=48.0, prog_24h=42.0)})
    job = EmpfehlungsAuditJob(
        speicher, motor, zone_ids=["bambuswald"], intervall_minuten=60,
    )
    # Snapshot in der Vergangenheit setzen (manuell gegen die Intervall-Logik)
    snapshot_zeit = datetime.now() - timedelta(hours=7)
    _run(speicher.setze_empfehlungs_audit(
        zeitstempel=snapshot_zeit, zone_id="bambuswald",
        empfehlungs_typ="praeventiv", soll_bewaessern=False,
        blocker_typ="ZEITFENSTER", feuchte_aktuell=50.0,
        welkepunkt_wert=32.0, optimum_min=40.0, optimum_max=60.0,
        prognose_quelle="ml", prognose_6h=48.0, prognose_12h=45.0,
        prognose_24h=42.0, tage_bis_welkepunkt=2.5,
        dauer_s_empfehlung=1800,
        aktive_strategie="haeufig_klein",
    ))
    # Sensor-Werte um die Zielzeitpunkte
    _run(speicher.speichere_messung(_msg(
        snapshot_zeit + timedelta(hours=6, minutes=10), 47.0,
    )))
    # 24 h ist noch in der Zukunft -> ist_24h bleibt None
    stats = _run(job.aktualisiere_wenn_faellig())
    assert stats["evals"] == 1
    # Hole_empfehlungs_audit liefert DESC — zuerst der neue Snapshot,
    # dann der eben evaluierte. Wir wollen den evaluierten.
    eintraege = _run(speicher.hole_empfehlungs_audit(tage=1))
    evaluierte = [e for e in eintraege if e["evaluiert_am"] is not None]
    assert len(evaluierte) == 1
    e = evaluierte[0]
    assert e["ist_feuchte_6h"] == 47.0
    # prognose_6h=48.0, ist=47.0 -> abweichung +1.0
    assert e["abweichung_6h"] == pytest.approx(1.0, rel=0.01)
    assert e["ist_feuchte_24h"] is None  # Zukunft


def test_eval_keine_messung_bleibt_offen(speicher):
    """T-0264 (2026-05-26, neuer Vertrag): ohne Messung im 6h-/24h-
    Fenster bleibt der Audit OFFEN (evaluiert_am=NULL), damit der
    naechste Tick es nochmal versucht. Vorher: Audit wurde mit
    ist_*=None markiert -> Lerndatenpunkt verloren wenn Sensor-Wert
    spaeter eintrudelt.
    """
    motor = _MotorStub({"bambuswald": _empfehlung()})
    job = EmpfehlungsAuditJob(
        speicher, motor, zone_ids=["bambuswald"],
    )
    snapshot_zeit = datetime.now() - timedelta(hours=7)
    _run(speicher.setze_empfehlungs_audit(
        zeitstempel=snapshot_zeit, zone_id="bambuswald",
        empfehlungs_typ="praeventiv", soll_bewaessern=False,
        blocker_typ=None, feuchte_aktuell=50.0,
        welkepunkt_wert=32.0, optimum_min=40.0, optimum_max=60.0,
        prognose_quelle="ml", prognose_6h=48.0, prognose_12h=45.0,
        prognose_24h=42.0, tage_bis_welkepunkt=2.5,
        dauer_s_empfehlung=1800, aktive_strategie="haeufig_klein",
    ))
    # Keine Sensor-Messungen
    stats = _run(job.aktualisiere_wenn_faellig())
    # T-0264: keine Eval, Audit bleibt offen.
    assert stats["evals"] == 0
    eintraege = _run(speicher.hole_empfehlungs_audit(tage=1))
    # Filter auf den manuell gesetzten Snapshot (Job hat ggf. noch
    # einen neuen geschrieben).
    alter = [e for e in eintraege
             if e["zeitstempel"] == snapshot_zeit.isoformat()]
    assert len(alter) == 1
    assert alter[0]["evaluiert_am"] is None


def test_t0236_pro_zone_stats_ignoriert_eintrags_limit(speicher):
    """T-0236-Bug-Fix (25.05.): pro-Zone-Aggregat per SQL ueber den
    ganzen Zeitraum, NICHT aus den paginierten `eintraege` errechnet.

    Original-Bug: das Frontend hat MAE_6h pro Zone aus den letzten
    500 Eintraegen selbst gerechnet (`eintraege.filter(zone==X)...`).
    Bei 14 Zonen reichte das Limit nur fuer ~1.5 Tage, der Fenster-
    Toggle (7/30/90 d) hatte effektiv keine Wirkung. Folge: Zone
    `bambuswald_yogaraum` mit 30d-MAE 8.6 pp wurde als 'Reif fuer
    Auto' gelabelt, weil der Mini-Ausschnitt zufaellig 4.3 pp zeigte.

    Regression: 800 evaluierte Eintraege ueber 30 d in 2 Zonen mit
    klar verschiedenen MAE-Profilen. pro_zone_stats muss korrekt
    differenzieren, auch wenn der Eintraege-Limit (default 500)
    nicht alle erfasst.
    """
    basis = datetime.now() - timedelta(days=20)

    async def _setze_und_eval(zone: str, ts: datetime, ist: float, abw: float):
        await speicher.setze_empfehlungs_audit(
            zeitstempel=ts, zone_id=zone,
            empfehlungs_typ="praeventiv", soll_bewaessern=False,
            blocker_typ=None, feuchte_aktuell=50.0,
            welkepunkt_wert=32.0, optimum_min=40.0, optimum_max=60.0,
            prognose_quelle="ml", prognose_6h=48.0, prognose_12h=45.0,
            prognose_24h=42.0, tage_bis_welkepunkt=2.5,
            dauer_s_empfehlung=1800, aktive_strategie="haeufig_klein",
        )
        # ID des eben geschriebenen Eintrags holen + Evaluierung setzen.
        # Test-only: direkter SQL ist hier ok, kein API-Pfad.
        cur = await speicher._db.execute(
            "SELECT id FROM empfehlungs_audit WHERE zone_id=? AND zeitstempel=?",
            (zone, ts.isoformat()),
        )
        row = await cur.fetchone()
        await cur.close()
        await speicher.aktualisiere_empfehlungs_audit_eval(
            audit_id=int(row["id"]),
            ist_feuchte_6h=ist, ist_feuchte_24h=ist,
            abweichung_6h=abw, abweichung_24h=abw,
            evaluiert_am=ts + timedelta(hours=6),
        )

    async def _seed():
        for i in range(400):
            ts = basis + timedelta(minutes=i * 30)
            # Zone A: prog=48, ist=50 -> Abweichung -2.0 pp (|2|)
            await _setze_und_eval("zone_a", ts, ist=50.0, abw=-2.0)
            # Zone B: prog=48, ist=57 -> Abweichung -9.0 pp (|9|)
            await _setze_und_eval("zone_b", ts, ist=57.0, abw=-9.0)

    _run(_seed())

    stats = _run(speicher.hole_empfehlungs_audit_stats_pro_zone(tage=30))

    assert set(stats.keys()) == {"zone_a", "zone_b"}
    assert stats["zone_a"]["n"] == 400
    assert stats["zone_a"]["n_evaluiert"] == 400
    assert stats["zone_a"]["mae_6h_pp"] == pytest.approx(2.0, abs=0.01)
    assert stats["zone_b"]["n"] == 400
    assert stats["zone_b"]["n_evaluiert"] == 400
    assert stats["zone_b"]["mae_6h_pp"] == pytest.approx(9.0, abs=0.01)
    # typ_verteilung pro Zone
    assert stats["zone_a"]["typ_verteilung"] == {"praeventiv": 400}
    assert stats["zone_b"]["typ_verteilung"] == {"praeventiv": 400}

    # Gegencheck: hole_empfehlungs_audit mit Default-Limit 500 wuerde
    # die Pro-Zone-Stats verfaelschen, wenn man sie selbst berechnete.
    eintraege = _run(speicher.hole_empfehlungs_audit(tage=30, limit=500))
    assert len(eintraege) == 500  # Limit greift
    # In den juengsten 500 sind beide Zonen ungleich gewichtet (Reihen-
    # folge INSERT) -- waere als selbst-Aggregat instabil. Hier nur
    # verifizieren dass eintraege.length deutlich < total (800) ist.
    assert len(eintraege) < 800


def test_t0264_evaluiert_am_nicht_gesetzt_wenn_beide_ist_werte_none(speicher):
    """T-0264 (2026-05-26): wenn _sensor_nah_zeitpunkt fuer beide
    Horizonte None liefert (z.B. Eval-Fenster noch zu frueh oder
    keine Sensor-Messung im Toleranzbereich), darf der Audit NICHT
    auf evaluiert_am gesetzt werden -- sonst geht der Lerndatenpunkt
    verloren.

    Realfall yogaraum 26.05.: Snapshot 11:49 wurde vom Job um 17:13
    (= vor ziel=17:49) gesehen, ist-Werte None, evaluiert_am=17:13
    gesetzt -> nachfolgender 18:14-Tick hat den Snapshot uebersehen,
    Sensor-Wert 18:07:57 nicht erfasst.
    """
    motor = _MotorStub({"bambuswald": _empfehlung()})
    job = EmpfehlungsAuditJob(speicher, motor, zone_ids=["bambuswald"])

    snapshot_zeit = datetime.now() - timedelta(hours=7)  # >6h alt
    _run(speicher.setze_empfehlungs_audit(
        zeitstempel=snapshot_zeit, zone_id="bambuswald",
        empfehlungs_typ="praeventiv", soll_bewaessern=False,
        blocker_typ="ZEITFENSTER", feuchte_aktuell=50.0,
        welkepunkt_wert=32.0, optimum_min=40.0, optimum_max=60.0,
        prognose_quelle="ml", prognose_6h=48.0, prognose_12h=45.0,
        prognose_24h=42.0, tage_bis_welkepunkt=2.5,
        dauer_s_empfehlung=1800, aktive_strategie="haeufig_klein",
    ))
    # Keine Sensor-Messungen -> beide ist-Werte werden None.

    stats = _run(job.aktualisiere_wenn_faellig())

    # Job hat eval versucht, aber keinen ist-Wert gefunden:
    # T-0264-Vertrag: Audit bleibt offen (evaluiert_am=NULL),
    # damit der naechste Tick es nochmal versuchen kann.
    assert stats["evals"] == 0, (
        f"Erwartet 0 evals (beide ist-Werte None), war: {stats}"
    )
    eintraege = _run(speicher.hole_empfehlungs_audit(tage=1))
    # Filter auf den manuell gesetzten Snapshot (Job kann ggf. noch
    # einen neuen schreiben mit anderem Zeitstempel).
    alter = [e for e in eintraege
             if e["zeitstempel"] == snapshot_zeit.isoformat()]
    assert len(alter) == 1
    assert alter[0]["evaluiert_am"] is None, (
        "T-0264: evaluiert_am darf NICHT gesetzt sein wenn beide "
        f"ist-Werte None sind. War: {alter[0]['evaluiert_am']}"
    )


def test_t0264_evaluiert_am_gesetzt_wenn_mindestens_ein_ist_wert_vorhanden(speicher):
    """T-0264 Sanity: wenn nur der 6h-Wert messbar ist (24h noch in
    Zukunft), wird der Audit trotzdem als evaluiert markiert. Der
    24h-Wert bleibt einfach None.
    """
    motor = _MotorStub({"bambuswald": _empfehlung()})
    job = EmpfehlungsAuditJob(speicher, motor, zone_ids=["bambuswald"])

    snapshot_zeit = datetime.now() - timedelta(hours=7)
    _run(speicher.setze_empfehlungs_audit(
        zeitstempel=snapshot_zeit, zone_id="bambuswald",
        empfehlungs_typ="praeventiv", soll_bewaessern=False,
        blocker_typ=None, feuchte_aktuell=50.0,
        welkepunkt_wert=32.0, optimum_min=40.0, optimum_max=60.0,
        prognose_quelle="ml", prognose_6h=48.0, prognose_12h=45.0,
        prognose_24h=42.0, tage_bis_welkepunkt=2.5,
        dauer_s_empfehlung=1800, aktive_strategie="haeufig_klein",
    ))
    # Sensor-Messung im 6h-Fenster (snapshot+6h ± 30 min).
    _run(speicher.speichere_messung(_msg(
        snapshot_zeit + timedelta(hours=6, minutes=5), 47.0,
    )))

    stats = _run(job.aktualisiere_wenn_faellig())

    assert stats["evals"] == 1
    eintraege = _run(speicher.hole_empfehlungs_audit(tage=1))
    evaluierte = [e for e in eintraege if e["evaluiert_am"] is not None]
    assert len(evaluierte) == 1
    e = evaluierte[0]
    assert e["ist_feuchte_6h"] == 47.0
    assert e["ist_feuchte_24h"] is None      # 24h-Ziel noch nicht erreicht
    assert e["abweichung_6h"] == pytest.approx(1.0, rel=0.01)


def test_motor_fehler_blockt_andere_zonen_nicht(speicher):
    """Wenn vorhersage_zone fuer eine Zone crasht, laufen die anderen weiter."""
    class _CrashMotor:
        async def vorhersage_zone(self, zone_id):
            if zone_id == "kaputt":
                raise RuntimeError("boom")
            return _empfehlung(zone_id)

    job = EmpfehlungsAuditJob(
        speicher, _CrashMotor(), zone_ids=["kaputt", "bambuswald"],
    )
    stats = _run(job.aktualisiere_wenn_faellig())
    assert stats["snapshots"] == 1  # nur Bambus durchgekommen
    eintraege = _run(speicher.hole_empfehlungs_audit(tage=1))
    assert [e["zone_id"] for e in eintraege] == ["bambuswald"]


def test_t0270_snapshot_persistiert_physik_felder(speicher):
    """T-0270: wenn `GiessEmpfehlung` Physik-Felder traegt
    (z.B. vom API-Augmentations-Helper oder via `konfig`-aware
    AuditJob), landen sie in `empfehlungs_audit`.
    """
    motor = _MotorStub({
        "bambuswald": _empfehlung(
            prog_6h=58.0, prog_24h=50.0,
            prog_physik_6h=54.0, prog_physik_24h=42.0,
            physik_quelle="gefittet", k_basis_pro_h=0.025,
        ),
    })
    job = EmpfehlungsAuditJob(speicher, motor, zone_ids=["bambuswald"])
    _run(job.aktualisiere_wenn_faellig())
    eintraege = _run(speicher.hole_empfehlungs_audit(tage=1))
    assert len(eintraege) == 1
    e = eintraege[0]
    assert e["prognose_physik_6h"] == pytest.approx(54.0)
    assert e["prognose_physik_24h"] == pytest.approx(42.0)
    assert e["physik_quelle"] == "gefittet"
    assert e["k_basis_pro_h"] == pytest.approx(0.025)


def test_t0270_eval_rechnet_physik_abweichung_parallel(speicher):
    """T-0270: Eval-Tick fuellt sowohl `abweichung_6h` (ML) als auch
    `abweichung_physik_6h` (Physik), gegen denselben ist-Sensor-Wert.
    """
    motor = _MotorStub({
        "bambuswald": _empfehlung(prog_6h=58.0, prog_24h=50.0),
    })
    job = EmpfehlungsAuditJob(
        speicher, motor, zone_ids=["bambuswald"], intervall_minuten=60,
    )
    # Snapshot 7 h vor jetzt -- mit Physik-Werten, sodass das Eval-
    # Fenster greift und beide Abweichungen rechnen kann.
    snapshot_zeit = datetime.now() - timedelta(hours=7)
    _run(speicher.setze_empfehlungs_audit(
        zeitstempel=snapshot_zeit, zone_id="bambuswald",
        empfehlungs_typ="praeventiv", soll_bewaessern=False,
        blocker_typ="ZEITFENSTER", feuchte_aktuell=60.0,
        welkepunkt_wert=40.0, optimum_min=60.0, optimum_max=75.0,
        prognose_quelle="ml",
        prognose_6h=58.0, prognose_12h=54.0, prognose_24h=50.0,
        tage_bis_welkepunkt=4.0, dauer_s_empfehlung=600,
        aktive_strategie="haeufig_klein",
        prognose_physik_6h=54.0, prognose_physik_12h=48.0,
        prognose_physik_24h=42.0, physik_quelle="gefittet",
        k_basis_pro_h=0.025,
    ))
    # Sensor-Wert nach ~6 h: real bei 52 -> ML weicht +6, Physik +2.
    _run(speicher.speichere_messung(_msg(
        snapshot_zeit + timedelta(hours=6, minutes=5), 52.0,
    )))
    stats = _run(job.aktualisiere_wenn_faellig())
    assert stats["evals"] == 1
    eintraege = _run(speicher.hole_empfehlungs_audit(tage=1))
    e = [x for x in eintraege if x["evaluiert_am"] is not None][0]
    assert e["ist_feuchte_6h"] == pytest.approx(52.0)
    # ML: 58 - 52 = +6
    assert e["abweichung_6h"] == pytest.approx(6.0, rel=0.05)
    # Physik: 54 - 52 = +2
    assert e["abweichung_physik_6h"] == pytest.approx(2.0, rel=0.05)
