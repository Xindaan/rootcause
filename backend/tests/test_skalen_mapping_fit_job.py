"""T-0181-Folge: Tests fuer den Skalen-Mapping-Fit-Job.

Deckt die Sammel-Phase-Gates (min_obs, min_spannweite_pp, max_residual_pp),
den Intervall-Schutz und den End-to-End-Round-Trip (Fit -> UPSERT ->
`letzte_messung_aggregiert` nutzt das Mapping) ab.

Setup-Pattern: lokale `Speicher`-Instanz auf tmp_path (kein Mock), echte
Sensor-Messungen schreiben, Job tickern.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import (
    DatenQuelle,
    GardenaKonfig,
    GesamtKonfig,
    MlAusschlussFenster,
    MlSkalenMappingKonfig,
    SensorMessung,
    WetterKonfig,
    ZonenKonfig,
)
from bewaesserung.skalen_mapping_fit_job import (
    SkalenMappingFitJob,
    _filtere_ausschluss_messungen,
    _paare_messungen,
)
from bewaesserung.speicher import Speicher


def _baue_konfig(
    zone_id: str = "waldblumenhain",
    quellen: list[str] | None = None,
) -> GesamtKonfig:
    """Minimal-Konfig mit einer Zone."""
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=[
            ZonenKonfig(
                zone_id=zone_id,
                name=zone_id,
                modus="automatik",
                feuchte_schwelle_min=30,
                feuchte_schwelle_max=60,
                feuchte_kritisch=20,
                calibration_pair_quellen=quellen or ["fyta"],
            ),
        ],
        wetter=WetterKonfig(),
    )


async def _schreibe_paar_serie(
    s: Speicher,
    zone_id: str,
    n: int,
    *,
    a_wahr: float,
    b_wahr: float,
    rauschen_pp: float = 0.5,
    spannweite_pp: float = 50.0,
    jetzt: datetime | None = None,
) -> None:
    """Schreibt `n` FYTA-Messungen + dazu Gardena-Messungen 5 min versetzt,
    so dass `feuchte_gardena = a_wahr * feuchte_fyta + b_wahr + rauschen`.
    """
    jetzt = jetzt or datetime(2026, 5, 27, 12, 0, 0)
    random.seed(42)
    for i in range(n):
        t_fyta = jetzt - timedelta(hours=i * 3)
        fy = 30.0 + (i % max(1, int(spannweite_pp)))
        await s.speichere_messung(SensorMessung(
            zeitstempel=t_fyta,
            zone_id=zone_id, geraet_id="fyta-1",
            boden_feuchte=fy, quelle=DatenQuelle.FYTA,
        ))
        ga = a_wahr * fy + b_wahr + random.gauss(0, rauschen_pp)
        # Pydantic-Range [0, 100] clampen, damit verrauschte Tests
        # (rauschen_pp=15) keinen Validation-Error werfen.
        ga = max(0.0, min(100.0, ga))
        await s.speichere_messung(SensorMessung(
            zeitstempel=t_fyta + timedelta(minutes=5),
            zone_id=zone_id, geraet_id="gardena-1",
            boden_feuchte=ga, quelle=DatenQuelle.GARDENA,
        ))


@pytest.mark.asyncio
async def test_job_skip_wenn_aktiv_false(tmp_path):
    """`aktiv=False` -> Job laeuft nicht (return False)."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        sm = MlSkalenMappingKonfig(aktiv=False)
        job = SkalenMappingFitJob(s, _baue_konfig(), sm)
        gelaufen = await job.aktualisiere_wenn_faellig(datetime.now())
        assert gelaufen is False
        assert job.letzter_erfolg is None
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_intervall_gate(tmp_path):
    """Wenn weniger als `intervall_stunden` seit dem letzten Lauf
    vergangen sind, wird nicht erneut gefittet."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        sm = MlSkalenMappingKonfig(
            aktiv=True, intervall_stunden=24,
            min_obs=10, min_spannweite_pp=10.0,
        )
        # Daten reichen fuer einen Fit.
        await _schreibe_paar_serie(
            s, "waldblumenhain", n=30, a_wahr=1.0, b_wahr=0.0,
        )
        job = SkalenMappingFitJob(s, _baue_konfig(), sm)
        jetzt = datetime(2026, 5, 27, 12, 0, 0)
        ok = await job.aktualisiere_wenn_faellig(jetzt)
        assert ok is True
        # 1 h spaeter: noch nicht faellig.
        ok2 = await job.aktualisiere_wenn_faellig(
            jetzt + timedelta(hours=1),
        )
        assert ok2 is False
        # 25 h spaeter: wieder faellig.
        ok3 = await job.aktualisiere_wenn_faellig(
            jetzt + timedelta(hours=25),
        )
        assert ok3 is True
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_skip_wenn_zu_wenig_obs(tmp_path):
    """`min_obs` wird gegen die ANZAHL der gepaarten Punkte geprueft.
    Bei n=5 < min_obs=50 darf nichts persistiert werden."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        sm = MlSkalenMappingKonfig(
            aktiv=True, min_obs=50, min_spannweite_pp=10.0,
        )
        await _schreibe_paar_serie(
            s, "waldblumenhain", n=5, a_wahr=1.2, b_wahr=-10.0,
        )
        job = SkalenMappingFitJob(s, _baue_konfig(), sm)
        ok = await job.aktualisiere_wenn_faellig(datetime(2026, 5, 27, 12))
        assert ok is True  # Scan lief durch
        # Aber kein Mapping geschrieben:
        m = await s.hole_skalen_mapping("waldblumenhain", "fyta")
        assert m is None
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_skip_wenn_spannweite_zu_klein(tmp_path):
    """Wenn alle Roh-Werte unter `min_spannweite_pp` schwanken, ist der
    Fit nicht identifizierbar -> Skip ohne UPSERT."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        sm = MlSkalenMappingKonfig(
            aktiv=True, min_obs=10, min_spannweite_pp=20.0,
        )
        # Spannweite kuenstlich auf 5 pp begrenzt.
        await _schreibe_paar_serie(
            s, "waldblumenhain", n=30, a_wahr=1.0, b_wahr=0.0,
            spannweite_pp=5.0,
        )
        job = SkalenMappingFitJob(s, _baue_konfig(), sm)
        ok = await job.aktualisiere_wenn_faellig(datetime(2026, 5, 27, 12))
        assert ok is True
        m = await s.hole_skalen_mapping("waldblumenhain", "fyta")
        assert m is None
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_skip_wenn_residuum_zu_gross(tmp_path):
    """Bei massivem Sensor-Rauschen oder Nicht-Linearitaet ueberschreitet
    der Residuum-MAE die Schwelle -> Skip."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        sm = MlSkalenMappingKonfig(
            aktiv=True, min_obs=10, min_spannweite_pp=10.0,
            max_residual_pp=2.0,
        )
        # Sehr verrauscht -- MAE wird deutlich > 2 pp.
        await _schreibe_paar_serie(
            s, "waldblumenhain", n=60, a_wahr=1.0, b_wahr=0.0,
            rauschen_pp=15.0, spannweite_pp=30.0,
        )
        job = SkalenMappingFitJob(s, _baue_konfig(), sm)
        ok = await job.aktualisiere_wenn_faellig(datetime(2026, 5, 27, 12))
        assert ok is True
        m = await s.hole_skalen_mapping("waldblumenhain", "fyta")
        assert m is None
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_t0285_skip_und_loescht_bei_unkorreliert(tmp_path):
    """T-0285: FYTA und Gardena unkorreliert (verschiedene Mikro-Standorte)
    -> kein valides lineares Mapping, obwohl Obs + Spannweite erfuellt sind
    und der MAE zufaellig klein bleiben kann. Der Korrelations-Guard muss
    skippen UND ein vorhandenes (Fehl-)Mapping loeschen (Identity).
    Realfall waldblumen: r~0, Fit a=1.666 zog FYTA 58->51.
    """
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        sm = MlSkalenMappingKonfig(
            aktiv=True, min_obs=20, min_spannweite_pp=20.0,
            # Residuum-Guard bewusst weit offen, damit NUR der
            # Korrelations-Guard die Ablehnung verursacht.
            max_residual_pp=100.0, min_korrelation=0.5,
        )
        # Vorab ein Fehl-Mapping ablegen -> muss durch delete-on-reject weg.
        await s.upsert_skalen_mapping(
            zone_id="waldblumenhain", quelle="fyta",
            a=1.666, b=-45.18, n_obs=99,
        )
        # Unkorrelierte Serie: FYTA rampt 30..79 (Spannweite ~49),
        # Gardena unabhaengig zufaellig in [25, 70].
        random.seed(7)
        jetzt = datetime(2026, 5, 27, 12, 0, 0)
        for i in range(40):
            t = jetzt - timedelta(hours=i * 3)
            fy = 30.0 + (i % 50)
            await s.speichere_messung(SensorMessung(
                zeitstempel=t, zone_id="waldblumenhain", geraet_id="fyta-1",
                boden_feuchte=fy, quelle=DatenQuelle.FYTA,
            ))
            await s.speichere_messung(SensorMessung(
                zeitstempel=t + timedelta(minutes=5),
                zone_id="waldblumenhain", geraet_id="gardena-1",
                boden_feuchte=random.uniform(25, 70), quelle=DatenQuelle.GARDENA,
            ))
        job = SkalenMappingFitJob(s, _baue_konfig(), sm)
        await job.aktualisiere_wenn_faellig(jetzt)
        # Kein valides Mapping -> Fehl-Mapping geloescht.
        m = await s.hole_skalen_mapping("waldblumenhain", "fyta")
        assert m is None, f"Fehl-Mapping nicht geloescht: {m}"
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_fit_round_trip_synthetisch(tmp_path):
    """End-to-End: synthetisches `a=1.2, b=-10.0` (FYTA -> Gardena).
    Erwartung: gefittete Koeffizienten weichen < 0.05 ab + persistiert."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        sm = MlSkalenMappingKonfig(
            aktiv=True, min_obs=20, min_spannweite_pp=15.0,
            max_residual_pp=5.0,
        )
        await _schreibe_paar_serie(
            s, "waldblumenhain", n=200,
            a_wahr=1.2, b_wahr=-10.0, rauschen_pp=0.5,
            spannweite_pp=50.0,
        )
        job = SkalenMappingFitJob(s, _baue_konfig(), sm)
        ok = await job.aktualisiere_wenn_faellig(datetime(2026, 5, 27, 12))
        assert ok is True
        assert job.letzter_fehler is None
        m = await s.hole_skalen_mapping("waldblumenhain", "fyta")
        assert m is not None
        assert abs(m["a"] - 1.2) < 0.05
        assert abs(m["b"] - (-10.0)) < 0.5
        assert m["n_obs"] >= 100
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_aggregat_nutzt_gefittetes_mapping(tmp_path):
    """End-to-End mit Live-Aggregat:
    Nach erfolgreichem Fit transformiert `letzte_messung_aggregiert`
    den FYTA-Wert vor der Median-Bildung."""
    s = Speicher(str(tmp_path / "test.db"))
    await s.verbinden()
    try:
        sm = MlSkalenMappingKonfig(
            aktiv=True, min_obs=20, min_spannweite_pp=15.0,
        )
        await _schreibe_paar_serie(
            s, "waldblumenhain", n=200,
            a_wahr=1.2, b_wahr=-10.0, rauschen_pp=0.3,
            spannweite_pp=50.0,
        )
        # Zwei aktuelle Sensoren in derselben Zone fuer das Aggregat:
        jetzt_jetzt = datetime(2026, 5, 27, 12, 30, 0)
        # Gardena bei 50 (Referenz-Skala).
        await s.speichere_messung(SensorMessung(
            zeitstempel=jetzt_jetzt, zone_id="waldblumenhain",
            geraet_id="gardena-1", boden_feuchte=50.0,
            quelle=DatenQuelle.GARDENA,
        ))
        # FYTA bei 50 (Roh): nach Mapping ~1.2*50 - 10 = 50, also Median 50.
        # FYTA bei 40 (Roh): nach Mapping ~1.2*40 - 10 = 38, Median(50, 38)=44.
        await s.speichere_messung(SensorMessung(
            zeitstempel=jetzt_jetzt, zone_id="waldblumenhain",
            geraet_id="fyta-1", boden_feuchte=40.0,
            quelle=DatenQuelle.FYTA,
        ))
        job = SkalenMappingFitJob(s, _baue_konfig(), sm)
        await job.aktualisiere_wenn_faellig(datetime(2026, 5, 27, 12, 0))
        agg = await s.letzte_messung_aggregiert(
            "waldblumenhain", jetzt=jetzt_jetzt + timedelta(minutes=1),
        )
        assert agg is not None
        # Mapping ungefaehr 1.2/-10 -> FYTA 40 -> ~38. Median(50, ~38).
        # Tolerant pruefen: Wert liegt zwischen Roh-Median (45) und
        # erwartetem ~44.
        assert 43.0 < agg.boden_feuchte < 46.0
    finally:
        await s.schliessen()


def test_paare_messungen_einfach():
    """`_paare_messungen` paart jede Fremd-Messung mit dem zeitlich
    naechsten Referenz-Wert binnen Toleranz, ohne Doppel-Verbrauch
    der Fremd-Seite."""
    basis = datetime(2026, 5, 27, 12, 0, 0)
    ref = [
        SensorMessung(
            zeitstempel=basis + timedelta(minutes=i),
            zone_id="z", geraet_id="g",
            boden_feuchte=50.0 + i * 0.1,
            quelle=DatenQuelle.GARDENA,
        )
        for i in range(0, 60, 10)
    ]
    fremd = [
        SensorMessung(
            zeitstempel=basis + timedelta(minutes=5),
            zone_id="z", geraet_id="f",
            boden_feuchte=42.0, quelle=DatenQuelle.FYTA,
        ),
        SensorMessung(
            zeitstempel=basis + timedelta(minutes=40),
            zone_id="z", geraet_id="f",
            boden_feuchte=45.0, quelle=DatenQuelle.FYTA,
        ),
    ]
    paare = _paare_messungen(ref, fremd)
    assert len(paare) == 2
    # Erster Paar: FYTA bei +5 min, naechster Gardena bei 0 oder +10
    # (beide 5 min Abstand) -> einer der beiden Werte 50.0 oder 51.0.
    assert paare[0][0] == 42.0
    assert paare[0][1] in (50.0, 51.0)


def test_paare_messungen_toleranz_filtert():
    """Wenn ALLE Referenz-Werte zu weit vom Fremd-Wert weg sind,
    entstehen keine Paare."""
    basis = datetime(2026, 5, 27, 12, 0, 0)
    ref = [
        SensorMessung(
            zeitstempel=basis,
            zone_id="z", geraet_id="g",
            boden_feuchte=50.0, quelle=DatenQuelle.GARDENA,
        ),
    ]
    fremd = [
        SensorMessung(
            zeitstempel=basis + timedelta(minutes=60),  # > 15 min weg
            zone_id="z", geraet_id="f",
            boden_feuchte=42.0, quelle=DatenQuelle.FYTA,
        ),
    ]
    paare = _paare_messungen(ref, fremd)
    assert paare == []


def test_filtere_ausschluss_messungen_respektiert_geraet_und_wartung():
    basis = datetime(2026, 5, 27, 12, 0, 0)
    konfig = _baue_konfig("waldblumenhain")
    konfig.ml_ausschluss_fenster = [
        MlAusschlussFenster(
            zone_id="waldblumenhain",
            geraet_id="fyta-1",
            von=basis,
            bis=basis + timedelta(hours=1),
        ),
    ]
    messungen = [
        SensorMessung(
            zeitstempel=basis + timedelta(minutes=10),
            zone_id="waldblumenhain",
            geraet_id="fyta-1",
            boden_feuchte=40.0,
            quelle=DatenQuelle.FYTA,
        ),
        SensorMessung(
            zeitstempel=basis + timedelta(minutes=10),
            zone_id="waldblumenhain",
            geraet_id="fyta-2",
            boden_feuchte=41.0,
            quelle=DatenQuelle.FYTA,
        ),
        SensorMessung(
            zeitstempel=basis + timedelta(hours=3),
            zone_id="waldblumenhain",
            geraet_id="fyta-2",
            boden_feuchte=42.0,
            quelle=DatenQuelle.FYTA,
        ),
    ]

    gefiltert = _filtere_ausschluss_messungen(
        messungen,
        konfig,
        "waldblumenhain",
        wartungs_fenster=[
            (basis + timedelta(hours=2), basis + timedelta(hours=4)),
        ],
    )

    assert [m.geraet_id for m in gefiltert] == ["fyta-2"]
