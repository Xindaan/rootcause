"""Tests fuer T-0047 ML-Drift-Log (Speicher + Job)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.ml.drift_job import MlDriftJob
from bewaesserung.modelle import DatenQuelle, SensorMessung
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "drift.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


async def _logge(
    speicher: Speicher,
    zeit: datetime,
    zone_id: str,
    horizont: int,
    prognose: float,
    modell: str = "modell_6h_2026-04-19.lgbm",
    feature_zeitstempel: datetime | None = None,
) -> None:
    await speicher.logge_ml_vorhersage(
        zeitstempel=zeit,
        zone_id=zone_id,
        horizont_h=horizont,
        prognose_ziel_zeit=zeit + timedelta(hours=horizont),
        prognose_feuchte=prognose,
        modell_version=modell,
        feature_zeitstempel=feature_zeitstempel,
    )


async def _fuege_messung(
    speicher: Speicher, zeit: datetime, zone_id: str, feuchte: float,
) -> None:
    await speicher.speichere_messung(SensorMessung(
        zeitstempel=zeit,
        zone_id=zone_id,
        boden_feuchte=feuchte,
        quelle=DatenQuelle.GARDENA,
    ))


# --- Speicher-API ---


def test_logge_ml_vorhersage_persistiert(speicher):
    jetzt = datetime(2026, 4, 19, 10, 0)
    _run(_logge(speicher, jetzt, "bambuswald", 6, 42.5))

    metriken = _run(speicher.hole_drift_metriken(None, 30, jetzt))
    # Ohne Evaluation darf der MAE-Report keine Eintraege liefern
    assert metriken == {}


def test_logge_ml_vorhersage_dedupliziert_feature_zeitstempel(speicher):
    jetzt = datetime(2026, 4, 19, 10, 0)
    feature_zeit = datetime(2026, 4, 19, 9, 45)
    _run(_logge(
        speicher, jetzt, "bambuswald", 6, 42.0,
        feature_zeitstempel=feature_zeit,
    ))
    _run(_logge(
        speicher, jetzt + timedelta(minutes=1), "bambuswald", 6, 41.0,
        feature_zeitstempel=feature_zeit,
    ))

    async def _count():
        assert speicher._db is not None
        async with speicher._db.execute(
            "SELECT COUNT(*) AS n FROM ml_vorhersage_log",
        ) as cur:
            row = await cur.fetchone()
        return row["n"]

    assert _run(_count()) == 1


def test_logge_ml_vorhersage_erlaubt_unterschiedliche_feature_zeiten(speicher):
    jetzt = datetime(2026, 4, 19, 10, 0)
    _run(_logge(
        speicher, jetzt, "bambuswald", 6, 42.0,
        feature_zeitstempel=datetime(2026, 4, 19, 9, 45),
    ))
    _run(_logge(
        speicher, jetzt + timedelta(minutes=1), "bambuswald", 6, 41.0,
        feature_zeitstempel=datetime(2026, 4, 19, 10, 0),
    ))

    async def _count():
        assert speicher._db is not None
        async with speicher._db.execute(
            "SELECT COUNT(*) AS n FROM ml_vorhersage_log",
        ) as cur:
            row = await cur.fetchone()
        return row["n"]

    assert _run(_count()) == 2


def test_evaluiere_offene_vorhersagen_setzt_ist_und_abweichung(speicher):
    # Inferenz um 10:00, Horizont 6h → Ziel 16:00
    inferenz = datetime(2026, 4, 19, 10, 0)
    ziel = inferenz + timedelta(hours=6)
    _run(_logge(speicher, inferenz, "bambuswald", 6, prognose=42.0))
    # Messung 10 Min nach dem Ziel → im ±30-Min-Fenster
    _run(_fuege_messung(speicher, ziel + timedelta(minutes=10), "bambuswald", 45.0))

    # Jetzt = Ziel + Toleranz, damit die Zeile faellig ist
    jetzt = ziel + timedelta(minutes=31)
    n, _cursor = _run(speicher.evaluiere_offene_vorhersagen(jetzt))
    assert n == 1

    metriken = _run(speicher.hole_drift_metriken("bambuswald", 30, jetzt))
    assert metriken[6]["n"] == 1
    assert abs(metriken[6]["mae"] - 3.0) < 0.001


def test_evaluiere_wartet_bis_ziel_zeit_erreicht(speicher):
    inferenz = datetime(2026, 4, 19, 10, 0)
    _run(_logge(speicher, inferenz, "bambuswald", 6, 42.0))
    # Messung vor dem Ziel + Jetzt vor dem Ziel → Zeile darf noch nicht
    # evaluiert werden
    _run(_fuege_messung(speicher, inferenz + timedelta(hours=1), "bambuswald", 41.0))
    jetzt = inferenz + timedelta(hours=2)
    n, _cursor = _run(speicher.evaluiere_offene_vorhersagen(jetzt))
    assert n == 0


def test_evaluiere_ueberspringt_ohne_passende_messung(speicher):
    inferenz = datetime(2026, 4, 19, 10, 0)
    ziel = inferenz + timedelta(hours=6)
    _run(_logge(speicher, inferenz, "bambuswald", 6, 42.0))
    # Messung fuer falsche Zone — darf nicht matchen
    _run(_fuege_messung(speicher, ziel, "waldblumenhain", 45.0))

    jetzt = ziel + timedelta(minutes=31)
    n, _cursor = _run(speicher.evaluiere_offene_vorhersagen(jetzt))
    assert n == 0


def test_evaluiere_ist_idempotent(speicher):
    inferenz = datetime(2026, 4, 19, 10, 0)
    ziel = inferenz + timedelta(hours=6)
    _run(_logge(speicher, inferenz, "bambuswald", 6, 42.0))
    _run(_fuege_messung(speicher, ziel, "bambuswald", 43.0))

    jetzt = ziel + timedelta(minutes=31)
    n1, _c1 = _run(speicher.evaluiere_offene_vorhersagen(jetzt))
    n2, _c2 = _run(speicher.evaluiere_offene_vorhersagen(jetzt + timedelta(hours=1)))
    assert n1 == 1
    assert n2 == 0


def test_evaluiere_cursor_blockiert_unevaluierbare_zeilen_nicht(speicher):
    """T-0080b: 5000 unevaluierbare Pilea-Zeilen + 1 evaluierbare bambuswald-
    Zeile am Ende — der Catchup-Loop muss mit Cursor an den Pilea-Zeilen
    vorbeischieben und die bambuswald-Zeile evaluieren. Vorher haengen
    geblieben (Pilea besetzt erste 5000 ORDER-BY-Plaetze, andere kommen nie).
    """
    # Pilea-Prognose ohne Sensor-Messung (unevaluierbar)
    inferenz_pilea = datetime(2026, 4, 19, 10, 0)
    for i in range(5):
        _run(_logge(
            speicher,
            inferenz_pilea + timedelta(seconds=i),
            "pilea", 6, 50.0,
        ))

    # bambuswald-Prognose, EVALUIERBAR — aber spaeter
    inferenz_bambus = datetime(2026, 4, 19, 11, 0)
    ziel_bambus = inferenz_bambus + timedelta(hours=6)
    _run(_logge(speicher, inferenz_bambus, "bambuswald", 6, 40.0))
    _run(_fuege_messung(speicher, ziel_bambus, "bambuswald", 44.0))

    jetzt = ziel_bambus + timedelta(minutes=31)
    # Mit batch_limit=3 muessen wir mehrere Catchup-Iterationen durchlaufen.
    job = MlDriftJob(speicher, intervall_stunden=1, batch_limit=3)
    assert _run(job.aktualisiere_wenn_faellig(jetzt)) is True

    # Trotz 5 unevaluierbarer Pilea-Zeilen vor der bambuswald-Zeile in der
    # ORDER-BY-Queue: bambuswald muss evaluiert sein.
    metriken = _run(speicher.hole_drift_metriken("bambuswald", 30, jetzt))
    assert metriken[6]["n"] == 1
    assert metriken[6]["mae"] == 4.0


def test_hole_drift_metriken_gruppiert_pro_horizont(speicher):
    inferenz = datetime(2026, 4, 19, 10, 0)
    for h, pred, ist in ((6, 40.0, 43.0), (12, 38.0, 40.0), (24, 35.0, 39.0)):
        ziel = inferenz + timedelta(hours=h)
        _run(_logge(speicher, inferenz, "bambuswald", h, pred))
        _run(_fuege_messung(speicher, ziel, "bambuswald", ist))

    jetzt = inferenz + timedelta(hours=25)
    _run(speicher.evaluiere_offene_vorhersagen(jetzt))

    metriken = _run(speicher.hole_drift_metriken("bambuswald", 30, jetzt))
    assert metriken[6]["mae"] == 3.0
    assert metriken[12]["mae"] == 2.0
    assert metriken[24]["mae"] == 4.0


def test_hole_drift_metriken_respektiert_zone(speicher):
    inferenz = datetime(2026, 4, 19, 10, 0)
    ziel = inferenz + timedelta(hours=6)
    _run(_logge(speicher, inferenz, "bambuswald", 6, 40.0))
    _run(_logge(speicher, inferenz, "waldblumenhain", 6, 30.0))
    _run(_fuege_messung(speicher, ziel, "bambuswald", 42.0))
    _run(_fuege_messung(speicher, ziel, "waldblumenhain", 35.0))

    jetzt = ziel + timedelta(minutes=31)
    _run(speicher.evaluiere_offene_vorhersagen(jetzt))

    bambus = _run(speicher.hole_drift_metriken("bambuswald", 30, jetzt))
    wald = _run(speicher.hole_drift_metriken("waldblumenhain", 30, jetzt))
    assert bambus[6]["mae"] == 2.0
    assert wald[6]["mae"] == 5.0


# --- MlDriftJob ---


def test_drift_job_laeuft_im_intervall(speicher):
    job = MlDriftJob(speicher, intervall_stunden=1)
    jetzt = datetime(2026, 4, 19, 10, 0)
    assert _run(job.aktualisiere_wenn_faellig(jetzt)) is True
    # Noch zu frueh → kein zweiter Lauf
    assert _run(job.aktualisiere_wenn_faellig(jetzt + timedelta(minutes=30))) is False
    # Nach Intervall → wieder
    assert _run(job.aktualisiere_wenn_faellig(jetzt + timedelta(hours=1, minutes=1))) is True


def test_drift_job_evaluiert_offene_zeilen(speicher):
    inferenz = datetime(2026, 4, 19, 10, 0)
    ziel = inferenz + timedelta(hours=6)
    _run(_logge(speicher, inferenz, "bambuswald", 6, 40.0))
    _run(_fuege_messung(speicher, ziel, "bambuswald", 44.0))

    job = MlDriftJob(speicher, intervall_stunden=1)
    jetzt = ziel + timedelta(minutes=31)
    assert _run(job.aktualisiere_wenn_faellig(jetzt)) is True

    metriken = _run(speicher.hole_drift_metriken(None, 30, jetzt))
    assert metriken[6]["n"] == 1
    assert metriken[6]["mae"] == 4.0


def test_drift_job_catchup_loop_holt_grossen_backlog_in_einem_tick_auf(speicher):
    """T-0080: Bei batch_limit=2 + 5 evaluierbaren Zeilen muss der Job in
    einem Tick mehrfach aufrufen, bis der Backlog leer ist (n=5 nicht n=2).

    Reproduziert das Husqvarna-Block-Szenario: Backend war Tage offline,
    tausende offene Prognosen, 1×/h-Tick + altes 1000-Limit haetten den
    Backlog langsamer abgebaut als er waechst. Catchup-Loop fixt das.
    """
    inferenz = datetime(2026, 4, 19, 10, 0)
    ziel = inferenz + timedelta(hours=6)
    # 5 Prognosen, alle mit gleichem Ziel + 1 passender Sensor-Messung pro Zone.
    for i, zone in enumerate(("z1", "z2", "z3", "z4", "z5")):
        _run(_logge(speicher, inferenz + timedelta(seconds=i), zone, 6, 40.0))
        _run(_fuege_messung(speicher, ziel, zone, 44.0))

    job = MlDriftJob(speicher, intervall_stunden=1, batch_limit=2)
    jetzt = ziel + timedelta(minutes=31)
    assert _run(job.aktualisiere_wenn_faellig(jetzt)) is True

    metriken = _run(speicher.hole_drift_metriken(None, 30, jetzt))
    # Alle 5 Zeilen evaluiert in einem Tick (3 Catchup-Runs: 2+2+1).
    assert metriken[6]["n"] == 5


def test_drift_job_catchup_bricht_ab_wenn_kein_fortschritt(speicher):
    """Wenn Zeilen nicht evaluierbar sind (keine passende Sensor-Messung),
    bleibt evaluiert_am NULL und ORDER BY ASC liefert sie immer wieder.
    Catchup muss bei aktualisiert==0 abbrechen, sonst Endlos-Schleife."""
    inferenz = datetime(2026, 4, 19, 10, 0)
    ziel = inferenz + timedelta(hours=6)
    # 3 Prognosen, KEINE Sensor-Messungen — nichts kann evaluiert werden.
    for i, zone in enumerate(("z1", "z2", "z3")):
        _run(_logge(speicher, inferenz + timedelta(seconds=i), zone, 6, 40.0))

    job = MlDriftJob(speicher, intervall_stunden=1, batch_limit=2)
    jetzt = ziel + timedelta(minutes=31)
    # Soll terminieren, nicht haengen.
    result = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert result is True
    metriken = _run(speicher.hole_drift_metriken(None, 30, jetzt))
    # Nichts evaluiert — kein Eintrag im Metriken-Dict fuer Horizont 6.
    assert 6 not in metriken


def test_drift_job_catchup_hardcap(speicher, monkeypatch):
    """Bei pathologisch grossem Backlog darf der Tick nicht ewig blocken —
    Hardcap nach catchup_max_runs Aufrufen, Warnung im Log."""
    aufrufe = []

    async def fake_evaluiere(jetzt, toleranz_minuten=30, limit=5000,
                              nach_ziel_zeit=None):
        aufrufe.append(1)
        # Cursor schiebt sich monoton weiter, sodass Catchup-Loop weiterlaeuft.
        # Limit voll ausgeschoepft → Hardcap muss greifen.
        return limit, f"2026-04-19T{12 + len(aufrufe):02d}:00:00"

    monkeypatch.setattr(speicher, "evaluiere_offene_vorhersagen", fake_evaluiere)
    job = MlDriftJob(speicher, intervall_stunden=1, batch_limit=10,
                     catchup_max_runs=3)
    jetzt = datetime(2026, 4, 26, 16, 0)
    assert _run(job.aktualisiere_wenn_faellig(jetzt)) is True
    assert len(aufrufe) == 3  # Hardcap greift, kein 4. Aufruf


def test_hole_drift_status_unterscheidet_keine_daten_von_backlog(speicher):
    """T-0080: Status-API liefert n_offen_im_fenster, damit das UI zwischen
    'wirklich nichts geloggt' und 'Drift-Job ist im Backlog' unterscheiden
    kann. Vorher rendert beides als n=0/mae=null."""
    # Beide Prognosen mit Zielzeit 16:00, also vor jetzt=17:00 + Toleranz.
    _run(_logge(speicher, datetime(2026, 4, 26, 10, 0), "bambuswald", 6, 40.0))
    _run(_logge(speicher, datetime(2026, 4, 26, 4, 0), "bambuswald", 12, 40.0))
    # Keine Sensor-Messung → bleibt offen + faellig

    jetzt = datetime(2026, 4, 26, 17, 0)
    status = _run(speicher.hole_drift_status(None, 7, jetzt))

    assert status[6]["n_offen_im_fenster"] == 1
    assert status[12]["n_offen_im_fenster"] == 1
    assert status[24]["n_offen_im_fenster"] == 0  # nichts geloggt
    assert status[6]["letzte_evaluierung"] is None  # nie evaluiert


def test_hole_drift_log_liefert_neueste_zuerst(speicher):
    """T-0080c: Inspektor-Endpoint — Prognose vs. Ist, neueste zuerst."""
    inferenz1 = datetime(2026, 4, 25, 10, 0)
    inferenz2 = datetime(2026, 4, 26, 10, 0)
    ziel1 = inferenz1 + timedelta(hours=6)
    ziel2 = inferenz2 + timedelta(hours=6)

    _run(_logge(speicher, inferenz1, "bambuswald", 6, 40.0))
    _run(_logge(speicher, inferenz2, "bambuswald", 6, 50.0))
    _run(_fuege_messung(speicher, ziel1, "bambuswald", 44.0))
    _run(_fuege_messung(speicher, ziel2, "bambuswald", 55.0))

    jetzt = ziel2 + timedelta(minutes=31)
    _run(speicher.evaluiere_offene_vorhersagen(jetzt))

    log = _run(speicher.hole_drift_log(zone_id="bambuswald", horizont_h=6, n=10))
    assert len(log) == 2
    # Neueste zuerst
    assert log[0]["zeitstempel"].startswith("2026-04-26")
    assert log[0]["prognose_feuchte"] == 50.0
    assert log[0]["ist_feuchte"] == 55.0
    assert log[0]["abweichung"] == 5.0
    assert log[1]["zeitstempel"].startswith("2026-04-25")


def test_hole_drift_log_filtert_zone_und_horizont(speicher):
    inferenz = datetime(2026, 4, 26, 10, 0)
    ziel6 = inferenz + timedelta(hours=6)
    ziel12 = inferenz + timedelta(hours=12)

    _run(_logge(speicher, inferenz, "bambuswald", 6, 40.0))
    _run(_logge(speicher, inferenz, "bambuswald", 12, 35.0))
    _run(_logge(speicher, inferenz, "waldblumenhain", 6, 30.0))
    _run(_fuege_messung(speicher, ziel6, "bambuswald", 44.0))
    _run(_fuege_messung(speicher, ziel12, "bambuswald", 38.0))
    _run(_fuege_messung(speicher, ziel6, "waldblumenhain", 33.0))

    jetzt = ziel12 + timedelta(minutes=31)
    _run(speicher.evaluiere_offene_vorhersagen(jetzt))

    # Filter Zone
    bambus = _run(speicher.hole_drift_log(zone_id="bambuswald"))
    assert all(r["zone_id"] == "bambuswald" for r in bambus)
    assert len(bambus) == 2

    # Filter Zone + Horizont
    bambus_6 = _run(speicher.hole_drift_log(zone_id="bambuswald", horizont_h=6))
    assert len(bambus_6) == 1
    assert bambus_6[0]["horizont_h"] == 6


def test_hole_drift_log_n_geclippt_auf_500(speicher):
    """Schutz gegen UI-Versehen — n=999999 wird hart auf 500 geclippt."""
    # Wir loggen nur 1 Zeile, aber das Limit muss trotzdem >0 und <=500 sein
    _run(_logge(speicher, datetime(2026, 4, 26, 10, 0), "bambuswald", 6, 40.0))

    # Negative + Null werden auf 1 hochgesetzt; rieseige Werte auf 500.
    log = _run(speicher.hole_drift_log(n=999999, nur_evaluiert=False))
    assert len(log) <= 500


def test_hole_drift_log_nur_evaluiert_default(speicher):
    """Default `nur_evaluiert=True` filtert offene Zeilen aus — UI sieht
    nur die mit Vergleichswerten."""
    inferenz = datetime(2026, 4, 26, 10, 0)
    ziel = inferenz + timedelta(hours=6)
    _run(_logge(speicher, inferenz, "bambuswald", 6, 40.0))
    _run(_fuege_messung(speicher, ziel, "bambuswald", 44.0))
    # offen + ohne Match
    _run(_logge(speicher, inferenz + timedelta(seconds=1), "pilea", 6, 50.0))

    jetzt = ziel + timedelta(minutes=31)
    _run(speicher.evaluiere_offene_vorhersagen(jetzt))

    nur_eval = _run(speicher.hole_drift_log())
    assert all(r["abweichung"] is not None for r in nur_eval)
    assert len(nur_eval) == 1

    alle = _run(speicher.hole_drift_log(nur_evaluiert=False))
    assert len(alle) == 2


def test_drift_job_fehler_isoliert(speicher, monkeypatch):
    """Ein Speicher-Fehler darf den Job nicht crashen lassen, und
    `_letzte_aktualisierung` bleibt ungesetzt, damit der naechste Zyklus
    es erneut versucht."""
    async def boom(*_a, **_kw):
        raise RuntimeError("DB kaputt")

    job = MlDriftJob(speicher, intervall_stunden=1)
    monkeypatch.setattr(speicher, "evaluiere_offene_vorhersagen", boom)

    jetzt = datetime(2026, 4, 19, 10, 0)
    assert _run(job.aktualisiere_wenn_faellig(jetzt)) is False
    # Da der letzte Lauf fehlgeschlagen ist, ist Job sofort wieder faellig.
    assert _run(job.aktualisiere_wenn_faellig(jetzt + timedelta(minutes=1))) is False


# --- T-0065/T-0610 EL-8: ml_dauer_vorschlag-Drift (nur gefahrene Dosen, pp) ---


def _vorschlag(speicher, zeit, zone_id="waldblumenhain", et0_6h=1.2):
    import json
    return _run(speicher.speichere_dauer_vorschlag(
        zeitstempel=zeit, zone_id=zone_id, f_vor=25.0, ziel_schwelle=45.0,
        heuristik_s=1560, ml_s=600, ml_modell_version="v-test",
        features_json=json.dumps({"et0_6h": et0_6h}), modus="shadow",
    ))


async def _zeile(speicher, row_id):
    async with speicher._db.execute(
        """SELECT bewertet_am, ist_delta_6h, heuristik_prognose_delta,
                  ml_prognose_delta, heuristik_fehler, ml_fehler,
                  gefahrene_dauer_s
             FROM ml_dauer_vorschlag WHERE id = ?""",
        (row_id,),
    ) as cur:
        return dict(await cur.fetchone())


def _bewerte(speicher, jetzt, pulse, heur=7.5, ml=3.0):
    from bewaesserung.ml.dauer_drift_bewertung import bewerte_dauer_vorschlaege

    aufrufe = []

    async def pulse_laden(von, bis):
        return [p for p in pulse
                if von <= datetime.fromisoformat(p["zeitstempel"]) <= bis]

    async def heuristik_pp(zone_id, dauer_s, et0_6h, zeitpunkt):
        aufrufe.append((zone_id, dauer_s, et0_6h, zeitpunkt))
        return heur

    n = _run(bewerte_dauer_vorschlaege(
        speicher, jetzt, pulse_laden=pulse_laden,
        heuristik_pp=heuristik_pp, ml_pp=lambda zid, puls: ml,
    ))
    return n, aufrufe


def test_el8_gefahrene_dosis_in_pp_gegen_puls_label(speicher):
    """Ein Lauf nach dem Vorschlag: Ist = 6-h-Label des Pulses, Heuristik und
    ML kommen als pp (nicht `sekunden / 60`), die Heuristik bekommt die
    GEFAHRENE Dauer und das ET0 des Vorschlags."""
    zeit = datetime(2026, 4, 22, 6, 0)
    rid = _vorschlag(speicher, zeit)
    puls = {"zone_id": "waldblumenhain", "zeitstempel": "2026-04-22T06:02:00",
            "dauer_s": 900, "delta_6h": 4.0}
    n, aufrufe = _bewerte(speicher, zeit + timedelta(hours=7), [puls])
    assert n == 1
    assert aufrufe == [("waldblumenhain", 900.0, 1.2, zeit)]
    z = _run(_zeile(speicher, rid))
    assert z["gefahrene_dauer_s"] == 900
    assert z["ist_delta_6h"] == 4.0
    assert z["heuristik_prognose_delta"] == 7.5
    assert z["ml_prognose_delta"] == 3.0
    assert abs(z["heuristik_fehler"] - 3.5) < 1e-9
    assert abs(z["ml_fehler"] - 1.0) < 1e-9


def test_el8_je_lauf_ein_vorschlag_und_abschluss_ohne_lauf(speicher):
    """Zwei Vorschlaege vor EINEM Lauf: nur der juengste bekommt ihn, und
    jeder Vorschlag hoechstens einen Lauf. Ein Puls einer anderen Zone und
    einer ausserhalb des Zuordnungsfensters zaehlen nicht. Ohne Lauf bleibt die Zeile offen, bis sie nach 18 h ohne gefahrene
    Dauer abgeschlossen wird (zaehlt dann nicht in die Ampel)."""
    zeit = datetime(2026, 4, 22, 6, 0)
    frueh = _vorschlag(speicher, zeit)
    spaet = _vorschlag(speicher, zeit + timedelta(minutes=5))
    fern = _vorschlag(speicher, zeit + timedelta(hours=2))
    # spaeter Vorschlag einer anderen Zone: zieht das Lade-Fenster der Pulse
    # bis 10:30 auf, damit der 08:45-Puls geladen wird und nur das
    # Zuordnungsfenster ihn von `fern` trennt.
    _vorschlag(speicher, zeit + timedelta(hours=4), zone_id="hecke")
    pulse = [
        {"zone_id": "waldblumenhain", "zeitstempel": "2026-04-22T06:06:00",
         "dauer_s": 600, "delta_6h": 3.0},
        # zweiter Lauf im Fenster desselben Vorschlags: bleibt unzugeordnet
        {"zone_id": "waldblumenhain", "zeitstempel": "2026-04-22T06:20:00",
         "dauer_s": 1200, "delta_6h": 5.0},
        {"zone_id": "hecke", "zeitstempel": "2026-04-22T06:01:00",
         "dauer_s": 600, "delta_6h": 9.0},
        {"zone_id": "waldblumenhain", "zeitstempel": "2026-04-22T08:45:00",
         "dauer_s": 600, "delta_6h": 9.0},
    ]
    n, _ = _bewerte(speicher, zeit + timedelta(hours=11), pulse)
    assert n == 1
    assert _run(_zeile(speicher, spaet))["gefahrene_dauer_s"] == 600
    assert _run(_zeile(speicher, frueh))["bewertet_am"] is None
    assert _run(_zeile(speicher, fern))["bewertet_am"] is None

    n, _ = _bewerte(speicher, zeit + timedelta(hours=20, minutes=1), pulse)
    assert n == 2
    for rid in (frueh, fern):
        z = _run(_zeile(speicher, rid))
        assert z["bewertet_am"] is not None
        assert z["gefahrene_dauer_s"] is None
        assert z["heuristik_fehler"] is None
    m = _run(speicher.hole_dauer_drift_metriken(
        "waldblumenhain", 30, jetzt=zeit + timedelta(days=1)))
    assert m["waldblumenhain"]["n_bewertet"] == 1


def test_el8_lauf_ohne_label_wartet_dann_ohne_fehler(speicher):
    """Gefahren, aber das 6-h-Label fehlt (noch): offen bis zum Abschluss,
    dann mit gefahrener Dauer, aber ohne Ist/Fehler."""
    zeit = datetime(2026, 4, 22, 6, 0)
    rid = _vorschlag(speicher, zeit)
    puls = {"zone_id": "waldblumenhain", "zeitstempel": "2026-04-22T06:02:00",
            "dauer_s": 600, "delta_6h": float("nan")}
    assert _bewerte(speicher, zeit + timedelta(hours=7), [puls])[0] == 0
    assert _bewerte(speicher, zeit + timedelta(hours=18), [puls])[0] == 1
    z = _run(_zeile(speicher, rid))
    assert z["gefahrene_dauer_s"] == 600
    assert z["ist_delta_6h"] is None and z["heuristik_fehler"] is None


def test_el8_drift_job_ruft_dauer_bewerter(speicher):
    """Der Drift-Job reicht seinen Takt an den Dauer-Bewerter weiter."""
    gerufen = []

    class Bewerter:
        async def bewerte(self, jetzt):
            gerufen.append(jetzt)
            return 0

    jetzt = datetime(2026, 4, 22, 12, 0)
    job = MlDriftJob(speicher, intervall_stunden=1, dauer_bewerter=Bewerter())
    assert _run(job.aktualisiere_wenn_faellig(jetzt)) is True
    assert gerufen == [jetzt]


# --- T-0567: Drift-Auswertung an den Sensor der Prognose gebunden ---

def _fuege_messung_geraet(speicher, zeit, zone_id, feuchte, geraet):
    return speicher.speichere_messung(SensorMessung(
        zeitstempel=zeit, zone_id=zone_id, geraet_id=geraet,
        boden_feuchte=feuchte, quelle=DatenQuelle.GARDENA,
    ))


def test_t0567_eval_nimmt_den_sensor_der_prognose(speicher):
    """Die Prognose gilt fuer EINEN Sensor -- der Ist-Wert muss es auch.

    Ohne Bindung suchte die Auswertung nur nach `zone_id` + Zeitnaehe. Bei
    `waldblumenhain` lagen im +/- 30-min-Fenster in 100 % der geprueften
    Faelle mehrere Kandidaten mit einer Median-Spanne von 32 pp; die
    gemessene `abweichung` war dann der Skalenwechsel, nicht der
    Prognosefehler -- und daraus baut sich die Drift-Ampel.
    """
    inferenz = datetime(2026, 4, 19, 10, 0)
    ziel = inferenz + timedelta(hours=6)
    _run(speicher.logge_ml_vorhersage(
        zeitstempel=inferenz, zone_id="waldblumenhain", horizont_h=6,
        prognose_ziel_zeit=ziel, prognose_feuchte=42.0,
        modell_version="m.lgbm", geraet_id="gardena-lead",
    ))
    # Fremder Sensor exakt am Ziel, Lead 10 min daneben.
    _run(_fuege_messung_geraet(speicher, ziel, "waldblumenhain", 12.0, "fyta-a"))
    _run(_fuege_messung_geraet(
        speicher, ziel + timedelta(minutes=10), "waldblumenhain", 45.0,
        "gardena-lead",
    ))

    jetzt = ziel + timedelta(minutes=31)
    n, _ = _run(speicher.evaluiere_offene_vorhersagen(jetzt))

    assert n == 1
    metriken = _run(speicher.hole_drift_metriken("waldblumenhain", 30, jetzt))
    assert abs(metriken[6]["mae"] - 3.0) < 0.001, (
        "MAE muss gegen den Lead (45,0) gerechnet sein, nicht gegen fyta (12,0)"
    )


def test_t0567_altzeile_ohne_sensor_wird_weiter_zonenweit_bewertet(speicher):
    """Gegenprobe und Bestandsschutz.

    Zeilen aus der Zeit vor der Spalte haben kein `geraet_id`. Ihr
    Bezugssensor ist nicht rekonstruierbar; sie zu verwerfen wuerde die
    Historie loeschen statt sie zu verbessern. Sie werden deshalb weiter
    zonenweit ausgewertet -- ohne diesen Fall koennte man den Filter hart
    setzen und die gesamte Alt-Drift verlieren.
    """
    inferenz = datetime(2026, 4, 19, 10, 0)
    ziel = inferenz + timedelta(hours=6)
    _run(_logge(speicher, inferenz, "waldblumenhain", 6, prognose=42.0))
    _run(_fuege_messung_geraet(
        speicher, ziel + timedelta(minutes=5), "waldblumenhain", 45.0, "fyta-a",
    ))

    jetzt = ziel + timedelta(minutes=31)
    n, _ = _run(speicher.evaluiere_offene_vorhersagen(jetzt))

    assert n == 1, "Altzeilen muessen weiterhin auswertbar bleiben"
