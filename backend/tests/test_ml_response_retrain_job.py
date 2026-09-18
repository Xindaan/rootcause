"""Tests fuer T-0065 MlResponseRetrainJob (Trigger + Gate + Rollback).

Die Feature-Pipeline wird per Monkey-Patch durch einen vorgefertigten
DataFrame ersetzt — die Tests pruefen Trigger-Logik, Gate-Entscheidung,
Symlink-Deployment und Rollback bei Gate-Rejection.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta

import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("pandas")

import numpy as np
import pandas as pd

from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    MlBewaesserungsResponseKonfig,
    SpeicherKonfig,
    StandortKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.ml.response_retrain_job import (
    PRUEF_INTERVALL_EVENTS,
    MlResponseRetrainJob,
)
from bewaesserung.ml.response_vorhersage import MLResponseService


def _synthetik(n: int, zone_id: str = "waldblumenhain", seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t0 = datetime(2026, 1, 1, 6, 0)
    zeilen = []
    for i in range(n):
        dauer_s = float(rng.integers(300, 1800))
        lps = float(rng.uniform(0.05, 0.10))
        f_vor = float(rng.uniform(25, 60))
        et0_nach = float(rng.uniform(0.5, 3.5))
        delta = (
            0.02 * dauer_s * lps
            - 0.1 * f_vor
            - 0.2 * et0_nach
            + rng.normal(0, 0.3)
            + 10
        )
        zeilen.append({
            "zone_id": zone_id,
            "zeitstempel": (t0 + timedelta(hours=i * 6)).isoformat(),
            "event_id": i + 1,
            "shared_valve": False,
            "dauer_s": int(dauer_s),
            "liter_pro_sekunde": lps,
            "f_vor": f_vor,
            "f_vor_gradient_3h": rng.uniform(-0.3, 0.3),
            "f_vor_gradient_24h": rng.uniform(-0.3, 0.3),
            "et0_nach_6h": et0_nach,
            "niederschlag_nach_24h": float(rng.uniform(0, 1)),
            "temperatur_ereignis": float(rng.uniform(10, 25)),
            "vpd_mittel": float(rng.uniform(0.2, 1.8)),
            "jahreszeit_sin": math.sin(2 * math.pi * i / 365),
            "jahreszeit_cos": math.cos(2 * math.pi * i / 365),
            "tageszeit_sin": math.sin(2 * math.pi * 6 / 24),
            "tageszeit_cos": math.cos(2 * math.pi * 6 / 24),
            "delta_6h": delta,
            "delta_12h": delta * 0.9,
            "delta_24h": delta * 0.6,
        })
    return pd.DataFrame(zeilen)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def konfig():
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t"),
        zonen=[ZonenKonfig(
            zone_id="waldblumenhain", name="Waldblumenhain", ventil_kanal=1,
        )],
        wetter=WetterKonfig(standorte=[WetterStandortKonfig(
            id="o", breite=52.52, laenge=13.405,
        )]),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
        standorte=[StandortKonfig(
            standort_id="garten", name="G",
            wetter_standort="o", zonen=["waldblumenhain"],
        )],
    )


@pytest.fixture
def response_konfig():
    return MlBewaesserungsResponseKonfig(
        aktiv=True,
        wirksam=False,
        min_events=5,
        retrain_intervall_tage=14,
        retrain_event_schwelle=5,
        gate_mae_faktor=0.5,
    )


@pytest.fixture(autouse=True)
def _reset_singleton():
    MLResponseService.zuruecksetzen()
    yield
    MLResponseService.zuruecksetzen()


def _patche_features(monkeypatch, df: pd.DataFrame) -> None:
    """Ersetzt erstelle_response_features durch eine Konstante."""
    async def _fake(speicher, konfig, von, bis):  # noqa: ANN001
        return df
    # Der Job importiert dynamisch per `from ... import` in
    # `_baue_trainingsdaten`, daher muss das Modul gepatcht werden.
    monkeypatch.setattr(
        "bewaesserung.ml.response_features.erstelle_response_features",
        _fake,
    )


def test_erster_lauf_trainiert(monkeypatch, tmp_path, konfig, response_konfig):
    df = _synthetik(n=30)
    _patche_features(monkeypatch, df)
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    ausgefuehrt = _run(job.aktualisiere_wenn_faellig())
    assert ausgefuehrt is True
    erg = job.letztes_ergebnis["waldblumenhain"]
    assert erg["status"] == "uebernommen"
    assert erg["version"] is not None
    # Symlinks sind gesetzt.
    zone_dir = tmp_path / "waldblumenhain"
    assert (zone_dir / "aktuell_waldblumenhain_inverse.lgbm").is_symlink()


def test_ohne_aktiv_kein_lauf(monkeypatch, tmp_path, konfig):
    resp = MlBewaesserungsResponseKonfig(aktiv=False)
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=resp,
        basis_verzeichnis=tmp_path,
    )
    assert _run(job.aktualisiere_wenn_faellig()) is False


def test_zu_wenig_events_status_uebersprungen(monkeypatch, tmp_path, konfig):
    resp = MlBewaesserungsResponseKonfig(aktiv=True, min_events=20)
    _patche_features(monkeypatch, _synthetik(n=10))
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=resp,
        basis_verzeichnis=tmp_path,
    )
    _run(job.aktualisiere_wenn_faellig())
    erg = job.letztes_ergebnis["waldblumenhain"]
    assert erg["status"] == "uebersprungen"


def test_zweiter_lauf_zu_frueh_ohne_neue_events(monkeypatch, tmp_path, konfig, response_konfig):
    df = _synthetik(n=30)
    _patche_features(monkeypatch, df)
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    _run(job.aktualisiere_wenn_faellig())
    # Gleich danach: Trigger darf nicht mehr feuern (zeit noch nicht, events
    # identisch).
    ausgefuehrt = _run(job.aktualisiere_wenn_faellig())
    assert ausgefuehrt is False


def test_event_trigger_feuert(monkeypatch, tmp_path, konfig, response_konfig):
    df_klein = _synthetik(n=10)
    _patche_features(monkeypatch, df_klein)
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    t0 = datetime(2026, 8, 1, 12, 0, 0)
    _run(job.aktualisiere_wenn_faellig(jetzt=t0))
    # Nun 5+ neue Events (Schwelle = 5).
    df_gross = _synthetik(n=16)
    _patche_features(monkeypatch, df_gross)
    # T-0458: Der Events-Trigger wird nur noch gedrosselt geprueft
    # (PRUEF_INTERVALL_EVENTS), weil die Pruefung den 365-Tage-Scan kostet.
    # Nach Ablauf der Drossel muss er unveraendert feuern.
    spaeter = t0 + PRUEF_INTERVALL_EVENTS
    ausgefuehrt = _run(job.aktualisiere_wenn_faellig(jetzt=spaeter))
    assert ausgefuehrt is True


def test_t0458_kein_datenaufbau_wenn_nichts_faellig(
    monkeypatch, tmp_path, konfig, response_konfig,
):
    """T-0458: der 365-Tage-Scan darf nicht in jedem 5-Minuten-Zyklus laufen.

    Bis 01.08.2026 baute `aktualisiere_wenn_faellig` die Trainingsdaten
    unbedingt auf und pruefte erst danach pro Zone, ob ueberhaupt etwas
    faellig ist -- bei einem Retrain-Intervall von Tagen. Gemessen waren das
    69 s pro Zyklus, die den Event-Loop blockierten (Heartbeat-Phase
    `ml_response_retrain`).
    """
    aufrufe = {"n": 0}
    df = _synthetik(n=30)

    async def _zaehlend(speicher, konfig, von, bis):  # noqa: ANN001
        aufrufe["n"] += 1
        return df

    monkeypatch.setattr(
        "bewaesserung.ml.response_features.erstelle_response_features",
        _zaehlend,
    )
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    t0 = datetime(2026, 8, 1, 12, 0, 0)
    assert _run(job.aktualisiere_wenn_faellig(jetzt=t0)) is True
    assert aufrufe["n"] == 1, "Erstlauf muss die Daten bauen (Trigger initial)"

    # Die naechsten 5-Minuten-Zyklen: nichts faellig (Intervall 14 Tage),
    # also darf kein einziger Scan mehr laufen.
    for i in range(1, 12):
        _run(job.aktualisiere_wenn_faellig(jetzt=t0 + timedelta(minutes=5 * i)))
    assert aufrufe["n"] == 1, (
        f"{aufrufe['n'] - 1} unnoetige 365-Tage-Scans in 55 Minuten "
        "-- T-0458 ist zurueck"
    )

    # Nach Ablauf der Drossel darf wieder genau einmal geprueft werden.
    _run(job.aktualisiere_wenn_faellig(jetzt=t0 + PRUEF_INTERVALL_EVENTS))
    assert aufrufe["n"] == 2


def test_t0458_zeit_trigger_ueberholt_die_drossel(
    monkeypatch, tmp_path, konfig, response_konfig,
):
    """Das Vorab-Gate darf einen zeitlich faelligen Retrain nicht verschlucken.

    Die Drossel gilt nur fuer den Events-Trigger. Ist das Retrain-Intervall
    um, muss der Job laufen -- unabhaengig davon, wann zuletzt geprueft wurde.
    """
    _patche_features(monkeypatch, _synthetik(n=30))
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    t0 = datetime(2026, 8, 1, 12, 0, 0)
    assert _run(job.aktualisiere_wenn_faellig(jetzt=t0)) is True
    # 14 Tage = retrain_intervall_tage der Fixture.
    spaet = t0 + timedelta(days=14, minutes=1)
    assert _run(job.aktualisiere_wenn_faellig(jetzt=spaet)) is True


# --- T-0477: Retention im Retrain-Pfad ---


class _FakeSpeicherReferenzen:
    """Minimaler Speicher-Ersatz: liefert nur die Modellreferenzen."""

    def __init__(self, referenzen: dict[str, set[str]] | None, fehler: bool = False):
        self._referenzen = referenzen or {}
        self._fehler = fehler
        self.aufrufe = 0

    async def hole_referenzierte_response_modellversionen(self):
        self.aufrufe += 1
        if self._fehler:
            raise RuntimeError("DB weg")
        return dict(self._referenzen)


def _lege_altlast(zone_dir, versionen):
    zone_dir.mkdir(parents=True, exist_ok=True)
    for v in versionen:
        (zone_dir / v).mkdir()
        (zone_dir / v / "inverse.lgbm").write_text("alt")


def test_t0477_retention_raeumt_nach_retrain_auf(
    monkeypatch, tmp_path, konfig, response_konfig,
):
    """Nach einem Retrain verschwinden unreferenzierte Altversionen.

    Geschuetzt bleiben: die DB-referenzierte Version, das Symlink-Ziel
    (= die frisch trainierte) und die juengsten fuenf.
    """
    alt = [f"v202501{tag:02d}_000000" for tag in range(1, 11)]
    _lege_altlast(tmp_path / "waldblumenhain", alt)
    _patche_features(monkeypatch, _synthetik(n=30))
    speicher = _FakeSpeicherReferenzen({"waldblumenhain": {alt[0]}})
    job = MlResponseRetrainJob(
        speicher=speicher, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )

    _run(job.aktualisiere_wenn_faellig(jetzt=datetime(2026, 8, 1, 12, 0, 0)))

    erg = job.letztes_ergebnis["waldblumenhain"]
    assert erg["status"] == "uebernommen"
    zone_dir = tmp_path / "waldblumenhain"
    verbleibend = sorted(
        p.name for p in zone_dir.iterdir()
        if p.is_dir() and not p.is_symlink()
    )
    # DB-Referenz ueberlebt, obwohl sie die aelteste ist.
    assert alt[0] in verbleibend
    # Die dazwischenliegenden sind weg.
    assert alt[1] not in verbleibend and alt[5] not in verbleibend
    # Juengste fuenf der Altlast + neue Version.
    assert erg["version"] in verbleibend
    assert erg["retention"]["entfernt"] == 5
    # Und die Symlinks zeigen weiterhin auf etwas Vorhandenes.
    for link in sorted(zone_dir.glob("aktuell_*")):
        assert link.is_symlink() and link.resolve().exists()


def test_t0477_ohne_db_referenzen_wird_nicht_aufgeraeumt(
    monkeypatch, tmp_path, konfig, response_konfig,
):
    """DB-Fehler darf nie als 'nichts referenziert' durchgehen.

    Sonst wuerde genau die Version entfernt, die eine gespeicherte
    Entscheidung erklaert.
    """
    alt = [f"v202502{tag:02d}_000000" for tag in range(1, 11)]
    _lege_altlast(tmp_path / "waldblumenhain", alt)
    _patche_features(monkeypatch, _synthetik(n=30))
    speicher = _FakeSpeicherReferenzen(None, fehler=True)
    job = MlResponseRetrainJob(
        speicher=speicher, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )

    _run(job.aktualisiere_wenn_faellig(jetzt=datetime(2026, 8, 1, 12, 0, 0)))

    zone_dir = tmp_path / "waldblumenhain"
    verbleibend = [
        p.name for p in zone_dir.iterdir() if p.is_dir() and not p.is_symlink()
    ]
    assert len(verbleibend) == 11, "trotz DB-Fehler wurde geloescht"
    assert "retention" not in job.letztes_ergebnis["waldblumenhain"]
    assert speicher.aufrufe == 1


def test_t0477_referenzen_werden_einmal_pro_zyklus_geholt(
    monkeypatch, tmp_path, konfig, response_konfig,
):
    _patche_features(monkeypatch, _synthetik(n=30))
    speicher = _FakeSpeicherReferenzen({})
    job = MlResponseRetrainJob(
        speicher=speicher, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    t0 = datetime(2026, 8, 1, 12, 0, 0)
    _run(job.aktualisiere_wenn_faellig(jetzt=t0))
    assert speicher.aufrufe == 1
    # Zweiter Zyklus ohne faellige Zone: keine weitere Abfrage.
    _run(job.aktualisiere_wenn_faellig(jetzt=t0 + timedelta(minutes=5)))
    assert speicher.aufrufe == 1


# --- T-0482: eigene Mindestmenge fuer AquaBloom-Pump-Zonen ---


def _konfig_mit_pump_zone(konfig: GesamtKonfig) -> GesamtKonfig:
    """Ergaenzt `zitrus` als Pump-Zone (kein Ventil, AquaBloom-Pflichtfelder)."""
    konfig.zonen.append(ZonenKonfig(
        zone_id="zitrus", name="Zitrus", ventil_kanal=None,
        aquabloom_pumpen_dauer_sekunden=900,
        aquabloom_pumpen_intervall_stunden=12.0,
        aquabloom_tropfer_anzahl=2,
        aquabloom_tropfer_liter_pro_stunde=1.6,
    ))
    konfig.standorte[0].zonen.append("zitrus")
    return konfig


def test_t0482_pump_zone_nutzt_eigene_hoehere_mindestmenge(
    tmp_path, konfig, response_konfig,
):
    """Ventil-Zone bei min_events, Pump-Zone bei min_events_pump_zone.

    Die Pump-Zonen-Labels sind schwaecher (kurzer Puls, kaum variable
    Dauer, 24h-Horizont oft entwertet) — ohne eigene Schwelle wuerde aus
    sechs Zeilen ein Modell gebaut.
    """
    resp = MlBewaesserungsResponseKonfig(
        aktiv=True, min_events=5, min_events_pump_zone=20,
    )
    job = MlResponseRetrainJob(
        speicher=None, konfig=_konfig_mit_pump_zone(konfig),
        response_konfig=resp, basis_verzeichnis=tmp_path,
    )
    assert job._min_events_fuer("waldblumenhain") == 5
    assert job._min_events_fuer("zitrus") == 20
    # Unbekannte Zone faellt auf die Ventil-Schwelle zurueck, nie auf 0.
    assert job._min_events_fuer("gibt_es_nicht") == 5


def test_t0482_pump_zone_unter_schwelle_wird_uebersprungen(
    monkeypatch, tmp_path, konfig,
):
    """Der Fall zitrus: 6 Rows, Ventil-Schwelle 5, Pump-Schwelle 20.

    Ohne die eigene Schwelle wuerde hier trainiert — das ist genau die
    Entscheidung vom 02.08.2026, die der Test festhaelt.
    """
    resp = MlBewaesserungsResponseKonfig(
        aktiv=True, min_events=5, min_events_pump_zone=20,
    )
    _patche_features(monkeypatch, _synthetik(n=6, zone_id="zitrus"))
    job = MlResponseRetrainJob(
        speicher=None, konfig=_konfig_mit_pump_zone(konfig),
        response_konfig=resp, basis_verzeichnis=tmp_path,
    )
    _run(job.aktualisiere_wenn_faellig())
    erg = job.letztes_ergebnis["zitrus"]
    assert erg["status"] == "uebersprungen"
    assert "zu_wenig_events" in erg["grund"]


def test_t0482_pump_zone_ueber_schwelle_trainiert(
    monkeypatch, tmp_path, konfig,
):
    """Gegenprobe: der Fall kasten_4/mandevilla muss durchgehen.

    Ohne diesen Test koennte die Schwelle beliebig hoch stehen und alle
    Pump-Zonen dauerhaft aussperren — der Fix waere dann wirkungslos.
    """
    resp = MlBewaesserungsResponseKonfig(
        aktiv=True, min_events=5, min_events_pump_zone=20,
    )
    _patche_features(monkeypatch, _synthetik(n=34, zone_id="zitrus"))
    job = MlResponseRetrainJob(
        speicher=None, konfig=_konfig_mit_pump_zone(konfig),
        response_konfig=resp, basis_verzeichnis=tmp_path,
    )
    _run(job.aktualisiere_wenn_faellig())
    assert job.letztes_ergebnis["zitrus"]["status"] == "uebernommen"


# --- T-0473: Faelligkeit ohne 365-Tage-Feature-Scan ---


@pytest.fixture
def db_speicher(tmp_path):
    from bewaesserung.speicher import Speicher

    s = Speicher(str(tmp_path / "retrain.db"))
    _run(s.verbinden())
    try:
        yield s
    finally:
        _run(s.schliessen())


def _schreibe_wasser_events(speicher, t0, anzahl, zone_id="waldblumenhain"):
    from bewaesserung.modelle import Ausloser, VentilAktion, VentilEreignis

    for i in range(anzahl):
        _run(speicher.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=t0 + timedelta(minutes=i),
            zone_id=zone_id, ventil_id="uuid-A:1",
            aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
            ausloser=Ausloser.MANUELL,
        )))


def test_t0473_events_pruefung_baut_keine_features(
    monkeypatch, tmp_path, konfig, response_konfig, db_speicher,
):
    """Der Events-Trigger darf den 365-Tage-Scan nicht mehr ausloesen.

    T-0458 hat den Scan auf alle 6 h gedrosselt, aber nicht billiger gemacht:
    jede Pruefung blockierte den Event-Loop weiterhin ~59 s. Seit T-0473
    beantwortet eine Zaehlabfrage die Frage, und der Feature-Aufbau laeuft
    nur noch, wenn wirklich eine Zone retrainiert wird.
    """
    aufrufe = {"n": 0}

    async def _zaehlend(speicher, konfig, von, bis):  # noqa: ANN001
        aufrufe["n"] += 1
        return _synthetik(n=30)

    monkeypatch.setattr(
        "bewaesserung.ml.response_features.erstelle_response_features",
        _zaehlend,
    )
    job = MlResponseRetrainJob(
        speicher=db_speicher, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    t0 = datetime(2026, 8, 1, 12, 0, 0)
    _schreibe_wasser_events(db_speicher, t0 - timedelta(days=30), 6)

    assert _run(job.aktualisiere_wenn_faellig(jetzt=t0)) is True
    assert aufrufe["n"] == 1, "Erstlauf trainiert und braucht die Daten"

    # Die Events-Pruefung nach Ablauf der Drossel: keine neuen Ereignisse,
    # also kein Trigger -- und vor allem kein Feature-Aufbau.
    assert _run(job.aktualisiere_wenn_faellig(
        jetzt=t0 + PRUEF_INTERVALL_EVENTS,
    )) is False
    assert aufrufe["n"] == 1, (
        "Die Faelligkeitspruefung hat wieder ein Jahr Features gebaut "
        "-- T-0473 ist zurueck"
    )

    # Genug neue Ereignisse (Schwelle 5) -> Trigger feuert unveraendert.
    _schreibe_wasser_events(db_speicher, t0 + timedelta(hours=1), 5)
    assert _run(job.aktualisiere_wenn_faellig(
        jetzt=t0 + 2 * PRUEF_INTERVALL_EVENTS,
    )) is True
    assert aufrufe["n"] == 2


def test_t0473_gespeicherter_zaehler_ist_der_proxy_nicht_die_zeilenzahl(
    monkeypatch, tmp_path, konfig, response_konfig, db_speicher,
):
    """Trigger und Merker muessen dieselbe Einheit haben.

    Der Trigger liest jetzt die Zaehlabfrage, der Merker speicherte bisher die
    DF-Zeilenzahl. Zwei Quellen ergeben eine Differenz, die keine neuen
    Ereignisse sind -- der Events-Trigger feuerte dann bei jeder Pruefung
    (oder nie, je nach Vorzeichen).
    """
    _patche_features(monkeypatch, _synthetik(n=30))
    job = MlResponseRetrainJob(
        speicher=db_speicher, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    t0 = datetime(2026, 8, 1, 12, 0, 0)
    _schreibe_wasser_events(db_speicher, t0 - timedelta(days=30), 6)

    assert _run(job.aktualisiere_wenn_faellig(jetzt=t0)) is True
    assert job._letzter_event_zaehler["waldblumenhain"] == 6, (
        "Merker haelt die DF-Zeilenzahl (30) statt des Zaehl-Proxys (6)"
    )

    # Folgepruefungen ohne neue Ereignisse duerfen nie feuern.
    for i in (1, 2, 3):
        assert _run(job.aktualisiere_wenn_faellig(
            jetzt=t0 + i * PRUEF_INTERVALL_EVENTS,
        )) is False


def test_t0473_ohne_speicher_bleibt_der_events_trigger_wirksam(
    monkeypatch, tmp_path, konfig, response_konfig,
):
    """Faellt die Zaehlabfrage aus, gilt der teure Alt-Pfad -- nicht "null".

    Ein stiller Null-Zaehler saehe aus wie "keine neuen Ereignisse" und wuerde
    den Events-Trigger dauerhaft stilllegen, ohne dass etwas fehlschlaegt.
    """
    _patche_features(monkeypatch, _synthetik(n=10))
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    t0 = datetime(2026, 8, 1, 12, 0, 0)
    _run(job.aktualisiere_wenn_faellig(jetzt=t0))
    _patche_features(monkeypatch, _synthetik(n=16))
    assert _run(job.aktualisiere_wenn_faellig(
        jetzt=t0 + PRUEF_INTERVALL_EVENTS,
    )) is True


# --- T-0483: erfolgloser Feature-Aufbau darf sich nicht endlos wiederholen ---


def _zaehlender_feature_bau(monkeypatch, ergebnis):
    """Patcht den Feature-Aufbau und zaehlt die Aufrufe.

    `ergebnis` ist entweder ein DataFrame oder eine Exception-Instanz; im
    zweiten Fall wirft der Fake, was `_baue_trainingsdaten_sicher` zu `None`
    macht.
    """
    aufrufe = {"n": 0}

    async def _fake(speicher, konfig, von, bis):  # noqa: ANN001
        aufrufe["n"] += 1
        if isinstance(ergebnis, Exception):
            raise ergebnis
        return ergebnis

    monkeypatch.setattr(
        "bewaesserung.ml.response_features.erstelle_response_features",
        _fake,
    )
    return aufrufe


def test_t0483_leerer_bestand_baut_features_nicht_in_jedem_zyklus(
    monkeypatch, tmp_path, konfig, response_konfig,
):
    """T-0483: leerer Bestand darf den 365-Tage-Aufbau nicht endlos wiederholen.

    Hat eine Zone noch nie trainiert, liefert `_ist_faellig` den Trigger
    "initial" -- und der galt in `_koennte_faellig_sein` VOR der Drossel.
    Lieferte der Aufbau dann nichts Verwertbares, blieb `_letzter_lauf`
    ungesetzt, "initial" galt weiter, und der Aufbau lief in jedem
    5-Minuten-Zyklus erneut. Genau die Event-Loop-Blockade aus T-0458/T-0473,
    nur unter anderer Vorbedingung.

    Real bei frisch aufgesetztem Bestand oder wenn alle Kandidaten durch die
    Feature-Filter fallen (`ml_ausschluss_fenster`, fehlende `f_vor`-Messungen).
    """
    aufrufe = _zaehlender_feature_bau(monkeypatch, pd.DataFrame())
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    t0 = datetime(2026, 8, 1, 12, 0, 0)
    for i in range(12):
        assert _run(job.aktualisiere_wenn_faellig(
            jetzt=t0 + timedelta(minutes=5 * i),
        )) is False
    assert aufrufe["n"] == 1, (
        f"{aufrufe['n']} Feature-Aufbauten in 55 Minuten bei leerem Bestand "
        "-- der initial-Trigger umgeht die Drossel wieder"
    )

    # Nach Ablauf der Drossel genau ein weiterer Versuch.
    _run(job.aktualisiere_wenn_faellig(jetzt=t0 + PRUEF_INTERVALL_EVENTS))
    assert aufrufe["n"] == 2


def test_t0483_fehlgeschlagener_aufbau_wird_genauso_gedrosselt(
    monkeypatch, tmp_path, konfig, response_konfig,
):
    """Dieselbe Drossel gilt fuer den Fehlerfall (Entscheidung Andre, 02.08.).

    `_baue_trainingsdaten_sicher` macht jede Exception zu `None`, faellt also
    in denselben Ausstieg wie das leere DataFrame. Ein dauerhaft
    fehlschlagender Aufbau wiederholte sich sonst ebenfalls alle 5 Minuten.

    Verworfen wurde das Muster des Schwester-Jobs (`ml/retrain_job.py`: kurzer
    Retry-Anker nach Fehler). Dort ist ein Fehlversuch billig, hier kostet er
    den vollen Feature-Aufbau -- bei 30-min-Retry waeren das ~48 Blockaden
    taeglich statt 4. Der Fehler bleibt ueber `letzter_fehler` in
    `/api/ml/status` sofort sichtbar; gedrosselt wird nur der Retry.
    """
    aufrufe = _zaehlender_feature_bau(monkeypatch, RuntimeError("DB weg"))
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    t0 = datetime(2026, 8, 1, 12, 0, 0)
    for i in range(12):
        assert _run(job.aktualisiere_wenn_faellig(
            jetzt=t0 + timedelta(minutes=5 * i),
        )) is False
    assert aufrufe["n"] == 1, (
        f"{aufrufe['n']} fehlschlagende Feature-Aufbauten in 55 Minuten"
    )
    # Der Fehler ist trotzdem sofort sichtbar, nicht erst nach der Drossel.
    assert job.letzter_fehler is not None
    assert job.letzter_fehler["typ"] == "RuntimeError"


def test_t0483_daten_nach_leerlauf_trainieren_unveraendert(
    monkeypatch, tmp_path, konfig, response_konfig,
):
    """Die Sperre darf den Retrain nur verzoegern, nicht verschlucken.

    `_letzter_lauf` behaelt die Bedeutung "hat trainiert" -- der Fehlversuch
    fasst ihn nicht an. Kommen Daten dazu, feuert "initial" mit Ablauf der
    Drossel unveraendert.
    """
    _patche_features(monkeypatch, pd.DataFrame())
    job = MlResponseRetrainJob(
        speicher=None, konfig=konfig, response_konfig=response_konfig,
        basis_verzeichnis=tmp_path,
    )
    t0 = datetime(2026, 8, 1, 12, 0, 0)
    assert _run(job.aktualisiere_wenn_faellig(jetzt=t0)) is False
    assert job._letzter_lauf == {}, "Fehlversuch darf nicht als Lauf zaehlen"

    _patche_features(monkeypatch, _synthetik(n=30))
    assert _run(job.aktualisiere_wenn_faellig(
        jetzt=t0 + PRUEF_INTERVALL_EVENTS,
    )) is True
    assert job.letztes_ergebnis["waldblumenhain"]["status"] == "uebernommen"
