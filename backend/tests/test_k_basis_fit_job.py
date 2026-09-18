"""Hybrid Stufe 1: Tests fuer den KbasisFitJob.

Deckt Phase-Detektor (Regen-Filter, Bewaesserungs-Anker, Min-Dauer),
Fit-Korrektheit auf synthetischen ODE-Daten, UPSERT und
T-0108-Status-Flow ab.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from bewaesserung.ml.k_basis_fit_job import (
    KbasisFitJob,
    _filtere_messungen_fenster,
    _phasen_kandidaten,
    _regen_im_fenster,
    _wasser_intervalle,
)
from bewaesserung.modelle import (
    Ausloser,
    DatenQuelle,
    GardenaKonfig,
    GesamtKonfig,
    MlAusschlussFenster,
    MlPhysikDiagnoseKonfig,
    SensorMessung,
    StandortKonfig,
    VentilAktion,
    VentilEreignis,
    WetterArchivStunde,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher


def _baue_konfig(zone_id: str = "z1") -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=[
            ZonenKonfig(
                zone_id=zone_id, name=zone_id, modus="automatik",
                ventil_kanal=1,
                feuchte_schwelle_min=30, feuchte_schwelle_max=60,
                feuchte_kritisch=20, welkepunkt=20.0,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[
                WetterStandortKonfig(
                    id="standort_a", breite=52.52, laenge=13.405,
                ),
            ],
        ),
        standorte=[
            StandortKonfig(
                standort_id="standort_a", name="Standort A",
                wetter_standort="standort_a", zonen=[zone_id],
            ),
        ],
    )


def _schliessen(basis: datetime, zone: str = "z1", dauer_s: int = 600,
                ausloser: Ausloser = Ausloser.MANUELL) -> VentilEreignis:
    return VentilEreignis(
        zeitstempel=basis, zone_id=zone, ventil_id="v1",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=dauer_s,
        ausloser=ausloser,
    )


def _oeffnen(basis: datetime, zone: str = "z1") -> VentilEreignis:
    return VentilEreignis(
        zeitstempel=basis, zone_id=zone, ventil_id="v1",
        aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
        ausloser=Ausloser.MANUELL,
    )


def test_phasen_kandidaten_min_dauer():
    """Schliessungen ohne folgendes Wasser binnen max_dauer_h erzeugen
    eine Phase bis Maximum. Kuerzere Abstaende werden verworfen."""
    basis = datetime(2026, 5, 1, 12, 0)
    schliessungen = [
        _schliessen(basis),
        _schliessen(basis + timedelta(hours=12)),
    ]
    # Oeffnen 2h nach erstem Schliessen -> erste Phase nur 2h (< min=6h).
    ereignisse = schliessungen + [_oeffnen(basis + timedelta(hours=2))]
    kandidaten = _phasen_kandidaten(
        schliessungen, _wasser_intervalle(ereignisse),
        min_dauer_h=6, max_dauer_h=72,
    )
    # Erste Phase verworfen (< 6h), zweite Phase 72h ohne Wasser.
    assert len(kandidaten) == 1
    assert kandidaten[0][0] == basis + timedelta(hours=12)


def test_phasen_kandidaten_keine_schliessung():
    assert _phasen_kandidaten([], [], 6, 72) == []


def test_t0350_schliessen_ohne_oeffnen_bricht_phase():
    """T-0350-Kern: ein spaeteres SCHLIESSEN dauer>0 OHNE geloggtes
    OEFFNEN (DHS-Backfill/Orphan) muss die Phase am rueckgerechneten
    Wasser-Start beenden — die alte Nur-OEFFNEN-Logik uebersah das."""
    basis = datetime(2026, 5, 1, 12, 0)
    anker = _schliessen(basis)
    # Close 10h spaeter mit 2h Dauer -> Wasser-Start bei basis+8h.
    spaeter = _schliessen(basis + timedelta(hours=10), dauer_s=7200)
    kandidaten = _phasen_kandidaten(
        [anker], _wasser_intervalle([anker, spaeter]),
        min_dauer_h=6, max_dauer_h=72,
    )
    assert kandidaten == [(basis, basis + timedelta(hours=8))]
    # Mit min_dauer 9h faellt die Phase komplett weg.
    assert _phasen_kandidaten(
        [anker], _wasser_intervalle([anker, spaeter]),
        min_dauer_h=9, max_dauer_h=72,
    ) == []


def test_t0350_ignoriert_lauf_bricht_phase():
    """Auch 'ignoriert'/'unbekannt'-Laeufe brechen die Phase — der
    Fluss war physisch, nur buchhalterisch ausgeblendet."""
    basis = datetime(2026, 5, 1, 12, 0)
    anker = _schliessen(basis)
    ignoriert = _schliessen(
        basis + timedelta(hours=4), dauer_s=1800, ausloser=Ausloser.IGNORIERT,
    )
    kandidaten = _phasen_kandidaten(
        [anker], _wasser_intervalle([anker, ignoriert]),
        min_dauer_h=6, max_dauer_h=72,
    )
    assert kandidaten == []


def test_t0350_ueberspannendes_intervall_verwirft_phase():
    """Wasser-Intervall, das den Anker ueberspannt (parallel laufender
    zweiter Lauf), macht die Phase unbrauchbar."""
    basis = datetime(2026, 5, 1, 12, 0)
    anker = _schliessen(basis)
    # Lauf 11:30-13:00 laeuft ueber den Anker (12:00) hinweg.
    parallel = _schliessen(basis + timedelta(hours=1), dauer_s=5400)
    kandidaten = _phasen_kandidaten(
        [anker], _wasser_intervalle([anker, parallel]),
        min_dauer_h=6, max_dauer_h=72,
    )
    assert kandidaten == []


def test_t0350_verbots_fenster_verwirft_phase():
    """Phase, die ein auto-ignore-Fenster ueberlappt, wird verworfen."""
    basis = datetime(2026, 5, 1, 12, 0)
    anker = _schliessen(basis)
    fenster = [(basis + timedelta(hours=20), basis + timedelta(hours=30))]
    assert _phasen_kandidaten(
        [anker], _wasser_intervalle([anker]),
        min_dauer_h=6, max_dauer_h=72, verbots_fenster=fenster,
    ) == []
    # Fenster komplett nach Phasen-Ende -> Phase bleibt.
    fenster_spaet = [(basis + timedelta(hours=80), basis + timedelta(hours=90))]
    assert len(_phasen_kandidaten(
        [anker], _wasser_intervalle([anker]),
        min_dauer_h=6, max_dauer_h=72, verbots_fenster=fenster_spaet,
    )) == 1


def test_regen_im_fenster():
    basis = datetime(2026, 5, 1, 12, 0)
    wetter = [
        WetterArchivStunde(
            zeitstempel=basis + timedelta(hours=h),
            niederschlag_mm=val, temperatur=20.0, et0_mm=0.1,
        )
        for h, val in [(1, 0.5), (3, 2.0), (10, 0.1), (50, 5.0)]
    ]
    # Fenster (basis, basis + 24h]: 0.5 + 2.0 + 0.1 = 2.6
    assert abs(
        _regen_im_fenster(wetter, basis, basis + timedelta(hours=24))
        - 2.6
    ) < 1e-6


def test_filtere_messungen_fenster_respektiert_geraet_und_wartung():
    basis = datetime(2026, 5, 27, 12, 0)
    konfig = _baue_konfig("z1")
    konfig.ml_ausschluss_fenster = [
        MlAusschlussFenster(
            zone_id="z1",
            geraet_id="gardena-1",
            von=basis,
            bis=basis + timedelta(hours=1),
        ),
    ]
    messungen = [
        SensorMessung(
            zeitstempel=basis + timedelta(minutes=10),
            zone_id="z1", geraet_id="gardena-1",
            boden_feuchte=60.0, quelle=DatenQuelle.GARDENA,
        ),
        SensorMessung(
            zeitstempel=basis + timedelta(minutes=10),
            zone_id="z1", geraet_id="gardena-2",
            boden_feuchte=61.0, quelle=DatenQuelle.GARDENA,
        ),
        SensorMessung(
            zeitstempel=basis + timedelta(hours=3),
            zone_id="z1", geraet_id="gardena-2",
            boden_feuchte=55.0, quelle=DatenQuelle.GARDENA,
        ),
    ]

    gefiltert = _filtere_messungen_fenster(
        messungen,
        konfig,
        "z1",
        wartungs_fenster=[
            (basis + timedelta(hours=2), basis + timedelta(hours=4)),
        ],
    )

    assert [m.geraet_id for m in gefiltert] == ["gardena-2"]


@pytest.mark.asyncio
async def test_job_inaktiv_returns_false(tmp_path):
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        physik = MlPhysikDiagnoseKonfig(aktiv=False)
        job = KbasisFitJob(s, _baue_konfig(), physik)
        ok = await job.aktualisiere_wenn_faellig(datetime.now())
        assert ok is False
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_job_skip_wenn_keine_phasen(tmp_path):
    """Keine SCHLIESSEN-Events -> Skip, kein Upsert."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        physik = MlPhysikDiagnoseKonfig(
            aktiv=True, min_phasen=3, min_phasen_dauer_h=6,
            max_phasen_dauer_h=72,
        )
        job = KbasisFitJob(s, _baue_konfig(), physik)
        ok = await job.aktualisiere_wenn_faellig(
            datetime(2026, 5, 27, 12),
        )
        assert ok is True
        row = await s.hole_k_basis("z1")
        assert row is None
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_fit_round_trip(tmp_path):
    """Synthetische Phasen mit bekanntem k=0.02 -> Job fittet das
    annaehernd und persistiert in `physik_k_basis`."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        wp = 20.0
        true_k = 0.02
        et0 = 0.1042
        jetzt = datetime(2026, 5, 27, 12, 0, 0)
        # 4 Trockenphasen, je ca. 48 h, ohne Regen, mit deutlich
        # dazwischenliegenden Bewaesserungs-Events.
        phasen_starts = [
            jetzt - timedelta(days=20),
            jetzt - timedelta(days=15),
            jetzt - timedelta(days=10),
            jetzt - timedelta(days=5),
        ]
        for start in phasen_starts:
            # Schliessen-Event als Phase-Anker.
            await s.speichere_ventil_ereignis(VentilEreignis(
                zeitstempel=start, zone_id="z1", ventil_id="v1",
                aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1800,
                ausloser=Ausloser.MANUELL,
            ))
            # OEffnungs-Event 60 h spaeter (definiert Phasen-Ende).
            await s.speichere_ventil_ereignis(VentilEreignis(
                zeitstempel=start + timedelta(hours=60), zone_id="z1",
                ventil_id="v1",
                aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
                ausloser=Ausloser.MANUELL,
            ))
            # Sensor-Zeitreihe nach Phasenstart: f(t) folgt dem Modell.
            f0 = 70.0
            for h in [1, 3, 6, 12, 24, 36, 48]:
                f_ist = wp + (f0 - wp) * math.exp(-true_k * h)
                await s.speichere_messung(SensorMessung(
                    zeitstempel=start + timedelta(hours=h),
                    zone_id="z1", geraet_id="g1",
                    boden_feuchte=f_ist, quelle=DatenQuelle.GARDENA,
                ))
        # Wetter-Archiv mit konstanter ET0 = Basis, kein Regen.
        for t in range(0, 30 * 24):
            await s._db.execute(
                "INSERT OR IGNORE INTO wetter_archiv "
                "(zeitstempel, standort_id, niederschlag_mm, "
                "temperatur, et0_mm, abgerufen_am) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (jetzt - timedelta(hours=t)).isoformat(),
                    "standort_a", 0.0, 20.0, et0,
                    jetzt.isoformat(),
                ),
            )
        await s._db.commit()

        physik = MlPhysikDiagnoseKonfig(
            aktiv=True, min_phasen=3, min_phasen_dauer_h=6,
            max_phasen_dauer_h=72,
            et0_basis_mm_pro_h=et0,
        )
        job = KbasisFitJob(s, _baue_konfig(), physik)
        ok = await job.aktualisiere_wenn_faellig(jetzt)
        assert ok is True
        assert job.letzter_fehler is None
        row = await s.hole_k_basis("z1")
        assert row is not None
        # Grid-Search-Aufloesung 0.005-0.05 in 10 Stufen
        # -> Treffer auf 0.02 erwartet.
        assert abs(row["k_basis"] - 0.02) < 0.005
        assert row["n_phasen"] >= 3
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_t0400_multisensor_verschiedene_skalen_kein_mix(tmp_path):
    """T-0400 (b): zwei Geraete EINER Zone auf VERSCHIEDENEN Skalen (Gardena
    0-100 -> Asymptote 20, FYTA 0-65 -> Asymptote 10), beide mit demselben
    wahren k=0.02. Ein gemischter Fit ueber beide Skalen (alte Bug-Klasse)
    hallucinierte ein Muell-k; der Per-Geraet-Fit + Median-k muss das wahre
    k annaehernd zuruecksichern."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        true_k = 0.02
        et0 = 0.1042
        jetzt = datetime(2026, 5, 27, 12, 0, 0)
        # Zwei Geraete, verschiedene f0/Asymptote (= verschiedene Skalen).
        geraete = [
            ("gardena-x", 70.0, 20.0),   # Gardena-Index
            ("fyta-y", 45.0, 10.0),      # FYTA-VWC, andere Skala
        ]
        phasen_starts = [
            jetzt - timedelta(days=20),
            jetzt - timedelta(days=15),
            jetzt - timedelta(days=10),
            jetzt - timedelta(days=5),
        ]
        for start in phasen_starts:
            await s.speichere_ventil_ereignis(VentilEreignis(
                zeitstempel=start, zone_id="z1", ventil_id="v1",
                aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1800,
                ausloser=Ausloser.MANUELL,
            ))
            await s.speichere_ventil_ereignis(VentilEreignis(
                zeitstempel=start + timedelta(hours=60), zone_id="z1",
                ventil_id="v1", aktion=VentilAktion.OEFFNEN,
                dauer_sekunden=0, ausloser=Ausloser.MANUELL,
            ))
            for gid, f0, wp in geraete:
                for h in [1, 3, 6, 12, 24, 36, 48]:
                    f_ist = wp + (f0 - wp) * math.exp(-true_k * h)
                    await s.speichere_messung(SensorMessung(
                        zeitstempel=start + timedelta(hours=h),
                        zone_id="z1", geraet_id=gid,
                        boden_feuchte=f_ist,
                        quelle=(DatenQuelle.GARDENA if gid.startswith("gardena")
                                else DatenQuelle.FYTA),
                    ))
        for t in range(0, 30 * 24):
            await s._db.execute(
                "INSERT OR IGNORE INTO wetter_archiv "
                "(zeitstempel, standort_id, niederschlag_mm, "
                "temperatur, et0_mm, abgerufen_am) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (jetzt - timedelta(hours=t)).isoformat(),
                    "standort_a", 0.0, 20.0, et0, jetzt.isoformat(),
                ),
            )
        await s._db.commit()

        physik = MlPhysikDiagnoseKonfig(
            aktiv=True, min_phasen=3, min_phasen_dauer_h=6,
            max_phasen_dauer_h=72, et0_basis_mm_pro_h=et0,
        )
        job = KbasisFitJob(s, _baue_konfig(), physik)
        ok = await job.aktualisiere_wenn_faellig(jetzt)
        assert ok is True
        assert job.letzter_fehler is None
        row = await s.hole_k_basis("z1")
        assert row is not None
        # Trotz zweier verschiedener Skalen: k aus dem Per-Geraet-Fit ~0.02.
        assert abs(row["k_basis"] - 0.02) < 0.005
        assert row["n_phasen"] >= 3
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_regen_phase_wird_verworfen(tmp_path):
    """Phase mit > max_regen_im_fenster_mm Niederschlag -> nicht
    in den Fit aufgenommen."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        jetzt = datetime(2026, 5, 27, 12, 0, 0)
        start = jetzt - timedelta(days=10)
        await s.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=start, zone_id="z1", ventil_id="v1",
            aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1800,
            ausloser=Ausloser.MANUELL,
        ))
        # Sensor-Zeitreihe egal -- die Phase wird durch Regen verworfen.
        await s.speichere_messung(SensorMessung(
            zeitstempel=start + timedelta(hours=1), zone_id="z1",
            geraet_id="g1", boden_feuchte=70.0, quelle=DatenQuelle.GARDENA,
        ))
        # 5 mm Regen 12 h spaeter -> ueber Schwelle 1 mm.
        await s._db.execute(
            "INSERT INTO wetter_archiv "
            "(zeitstempel, standort_id, niederschlag_mm, "
            "temperatur, et0_mm, abgerufen_am) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                (start + timedelta(hours=12)).isoformat(),
                "standort_a", 5.0, 20.0, 0.1,
                jetzt.isoformat(),
            ),
        )
        await s._db.commit()
        physik = MlPhysikDiagnoseKonfig(
            aktiv=True, min_phasen=1, min_phasen_dauer_h=6,
            max_phasen_dauer_h=72, max_regen_im_fenster_mm=1.0,
        )
        job = KbasisFitJob(s, _baue_konfig(), physik)
        ok = await job.aktualisiere_wenn_faellig(jetzt)
        assert ok is True
        row = await s.hole_k_basis("z1")
        # Phase verworfen -> kein UPSERT.
        assert row is None
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_t0350_cross_spray_lauf_bricht_phase_im_job(tmp_path):
    """Integration: ein 'ignoriert'-Lauf der Cross-Spray-Quell-Zone
    mitten in der Trockenphase verhindert den Fit der Ziel-Zone."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        konfig = _baue_konfig("z1")
        konfig.zonen[0].cross_spray_quell_zonen = ["q1"]
        konfig.zonen.append(ZonenKonfig(
            zone_id="q1", name="q1", modus="monitoring", ventil_kanal=2,
            feuchte_schwelle_min=30, feuchte_schwelle_max=60,
            feuchte_kritisch=20,
        ))
        konfig.standorte[0].zonen.append("q1")
        jetzt = datetime(2026, 5, 27, 12, 0, 0)
        start = jetzt - timedelta(days=10)
        await s.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=start, zone_id="z1", ventil_id="v1",
            aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1800,
            ausloser=Ausloser.MANUELL,
        ))
        # Quell-Zonen-Lauf ('ignoriert'!) 3h nach Phasen-Start.
        await s.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=start + timedelta(hours=3), zone_id="q1",
            ventil_id="v2", aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=3600, ausloser=Ausloser.IGNORIERT,
        ))
        # Saubere Sensor-Daten + Wetter, damit NUR der Cross-Spray-
        # Brecher die Phase verwirft.
        for h in [1, 3, 6, 12, 24]:
            await s.speichere_messung(SensorMessung(
                zeitstempel=start + timedelta(hours=h),
                zone_id="z1", geraet_id="g1",
                boden_feuchte=60.0, quelle=DatenQuelle.GARDENA,
            ))
        for t in range(0, 15 * 24):
            await s._db.execute(
                "INSERT OR IGNORE INTO wetter_archiv "
                "(zeitstempel, standort_id, niederschlag_mm, "
                "temperatur, et0_mm, abgerufen_am) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (jetzt - timedelta(hours=t)).isoformat(),
                    "standort_a", 0.0, 20.0, 0.1042, jetzt.isoformat(),
                ),
            )
        await s._db.commit()
        physik = MlPhysikDiagnoseKonfig(
            aktiv=True, min_phasen=1, min_phasen_dauer_h=6,
            max_phasen_dauer_h=72, et0_basis_mm_pro_h=0.1042,
        )
        job = KbasisFitJob(s, konfig, physik)
        ok = await job.aktualisiere_wenn_faellig(jetzt)
        assert ok is True
        # Phase durch Quell-Lauf gebrochen (nur noch 3h < 6h) -> kein Fit.
        assert await s.hole_k_basis("z1") is None
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_t0350_welkepunkt_proxy_null_wird_verworfen(tmp_path):
    """wert<=0-Kalibrier-Rows (Sensor-ausgebaut-Phase, Realfall hecke
    welkepunkt_proxy=0.0) duerfen keinen Welkepunkt liefern — sonst
    fittet der Job gegen eine Null-Asymptote (Live-Fit mae=24pp)."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        konfig = _baue_konfig("z1")
        konfig.zonen[0].welkepunkt = None  # erzwinge Proxy-Pfad
        jetzt = datetime(2026, 5, 27, 12, 0, 0)
        for i in range(3):
            await s.speichere_kalibrierung(
                zeitstempel=jetzt - timedelta(days=i + 1, hours=1),
                zone_id="z1", typ="welkepunkt_proxy", wert=0.0,
            )
        physik = MlPhysikDiagnoseKonfig(aktiv=True)
        job = KbasisFitJob(s, konfig, physik)
        assert await job._welkepunkt_fuer_zone("z1") is None
        # Positive Werte liefern weiter den Median.
        for i in range(3):
            await s.speichere_kalibrierung(
                zeitstempel=jetzt - timedelta(days=i + 10, hours=2),
                zone_id="z1", typ="welkepunkt_proxy", wert=30.0 + i,
            )
        assert await job._welkepunkt_fuer_zone("z1") == 31.0
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_t0108_status_flow_bei_crash(tmp_path):
    """Crash im Scan setzt `letzter_fehler` (T-0108)."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        physik = MlPhysikDiagnoseKonfig(aktiv=True, min_phasen=1)
        job = KbasisFitJob(s, _baue_konfig(), physik)

        async def boom(*a, **kw):
            raise RuntimeError("simulierter Fehler")

        job._scan_alle_zonen = boom  # type: ignore[assignment]
        ok = await job.aktualisiere_wenn_faellig(datetime(2026, 5, 27))
        assert ok is False
        assert job.letzter_fehler is not None
        assert job.letzter_fehler["typ"] == "RuntimeError"
        assert "simulierter Fehler" in job.letzter_fehler["nachricht"]
    finally:
        await s.schliessen()


def test_t0484_pump_zonen_fallen_nicht_mehr_aus_dem_scan():
    """T-0484: das aeussere Zonen-Gate schloss aus, was die innere Logik will.

    `_wasser_intervalle` sagt in seinem eigenen Docstring "bewusst ALLE
    ausloser -- der Fluss war physisch". Der Scan davor sammelte die Zonen
    aber mit `z.ventil_kanal is not None` ein. AquaBloom-Pulse sind echtes
    Wasser mit bekannter Dauer und Litermenge, haben aber keinen Ventil-Kanal;
    sie fielen deshalb nie in den k-Fit, obwohl der Fit genau die Trocknung
    zwischen solchen Wassergaben lernen soll. Gleiche Fehlerklasse wie T-0482.

    Statischer Test auf die Auswahlregel: der Scan selbst braucht Speicher,
    Wetter-Archiv und Sensorreihen, und diese Fehlerklasse sitzt in der einen
    Zeile davor.
    """
    import inspect

    from bewaesserung.aquabloom_job import _ist_konfiguriert
    from bewaesserung.ml import k_basis_fit_job

    quelle = inspect.getsource(k_basis_fit_job.KbasisFitJob._scan_alle_zonen)
    assert "z.ventil_kanal is not None or _ist_konfiguriert(z)" in quelle, (
        "Pump-Zonen sind wieder aus der Zonen-Auswahl des k-Fits gefallen"
    )

    # Und die Auswahlregel trifft eine echte Pump-Zone ohne Ventil-Kanal.
    pump = ZonenKonfig(
        zone_id="kasten_4", name="Kasten 4", modus="monitoring",
        ventil_kanal=None,
        feuchte_schwelle_min=32, feuchte_schwelle_max=58,
        feuchte_kritisch=20, welkepunkt=20.0,
        aquabloom_pumpen_dauer_sekunden=600,
        aquabloom_pumpen_intervall_stunden=12.0,
        aquabloom_tropfer_anzahl=2,
        aquabloom_tropfer_liter_pro_stunde=2.0,
    )
    assert pump.ventil_kanal is None
    assert _ist_konfiguriert(pump), (
        "Testzone ist keine gueltige Pump-Zone -- dann prueft der Test nichts"
    )


def test_t0503_pump_zonen_helfer_delegiert_statt_zu_duplizieren():
    """T-0503: `_ist_pump_zone` darf die Pflichtfelder nicht nachbauen.

    Eine zweite Fassung der AquaBloom-Pruefung liefe still auseinander,
    sobald jemand ein fuenftes Pflichtfeld ergaenzt -- und genau dieses
    Auseinanderlaufen (aeusseres Gate gegen innere Logik) war der Kern von
    T-0482/T-0484.
    """
    import inspect

    from bewaesserung.entscheidung import _ist_pump_zone

    quelle = inspect.getsource(_ist_pump_zone)
    assert "_ist_konfiguriert" in quelle, "delegiert nicht"
    assert "aquabloom_pumpen_dauer_sekunden" not in quelle, (
        "Pflichtfelder werden hier nachgebaut statt delegiert"
    )

    pump = ZonenKonfig(
        zone_id="kasten_4", name="Kasten 4", modus="monitoring",
        ventil_kanal=None,
        feuchte_schwelle_min=32, feuchte_schwelle_max=58,
        feuchte_kritisch=20, welkepunkt=20.0,
        aquabloom_pumpen_dauer_sekunden=600,
        aquabloom_pumpen_intervall_stunden=12.0,
        aquabloom_tropfer_anzahl=2,
        aquabloom_tropfer_liter_pro_stunde=2.0,
    )
    ohne = ZonenKonfig(
        zone_id="hecke", name="Hecke", modus="automatik", ventil_kanal=2,
        feuchte_schwelle_min=32, feuchte_schwelle_max=55,
        feuchte_kritisch=25, welkepunkt=25.0,
    )
    assert _ist_pump_zone(pump) is True
    assert _ist_pump_zone(ohne) is False


def test_t0503_monitoring_zweig_fragt_das_modell_ab():
    """T-0503: der MONITORING-Zweig muss die Dosis-Empfehlung mitfuehren.

    Vorher stiegen BEIDE Konsumenten fuer Monitoring-Zonen aus, bevor der
    ML-Aufruf kam -- die Modelle wurden trainiert, geladen und nie gefragt
    (`fehlerpattern_detektor_ohne_konsument`). Andres Entscheidung 05.08.:
    Anzeige im Dashboard. `soll_bewaessern` bleibt False, diese Zonen haben
    kein Ventil.
    """
    import inspect

    from bewaesserung.entscheidung import Entscheidungsmotor

    quelle = inspect.getsource(Entscheidungsmotor.vorhersage_zone)
    # `ml_s_mon` und `_ist_pump_zone` kommen ausschliesslich im
    # Monitoring-Zweig vor -- ein Textsplit auf eine Anker-Zeile waere
    # unnoetig fragil (die erste Fassung splittete an einer Zeile, die auch
    # weiter oben steht).
    assert "_ist_pump_zone(zone)" in quelle, (
        "Pump-Zonen-Gate fehlt im Monitoring-Zweig"
    )
    assert "ml_s_mon, ml_version_mon, ml_grund_mon" in quelle, (
        "Monitoring-Zweig fragt das Response-Modell nicht ab"
    )
    assert "dauer_s_ml=ml_s_mon" in quelle, (
        "Ergebnis wird nicht in die Empfehlung geschrieben"
    )
    assert "soll_bewaessern=False" in quelle, (
        "Der Monitoring-Zweig darf nie zum Giessen fuehren"
    )
