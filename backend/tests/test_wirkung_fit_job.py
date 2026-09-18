"""T-0292 Stufe 2: Tests fuer den Plateau-Wirkungs-Fit-Job + die
observation-gated Engine-Adoption.

Deckt ab:
  - Job-Gates: inaktiv, Intervall, keine Paare.
  - Fit-Round-Trip: saubere Plateau-Daten -> angenommen=True, wmax/r0
    plausibel, in `wirkung_fit` persistiert.
  - Rausch-Daten -> angenommen=False persistiert (Realdaten-Befund 06.06.).
  - Adoption `_aufgeloeste_wirkung`: adoptieren=False ist ein perfekter
    No-op; angenommen+frisch -> Fit; abgelehnt/zu-alt -> Konfig.
  - `_berechne_dauer`: ohne Override identisch zu Konfig; mit Override
    aenderbar (Adoption wirkt).

Setup-Pattern (analog test_skalen_mapping_fit_job): lokaler `Speicher`
auf tmp_path, echte wirkungsrate-Kalibrierungs-Records schreiben.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

import pytest

from bewaesserung.entscheidung import Entscheidungsmotor
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    MlWirkungFitKonfig,
    WetterKonfig,
    ZonenKonfig,
)
from bewaesserung.ml.wirkung_fit_job import WirkungFitJob
from bewaesserung.speicher import Speicher

JETZT = datetime(2026, 6, 7, 12, 0, 0)


def _baue_zone(
    zone_id: str = "bambuswald",
    *,
    wirkung_max_pp: float | None = 15.0,
    wirkungsrate_initial: float | None = 1.5,
) -> ZonenKonfig:
    return ZonenKonfig(
        zone_id=zone_id,
        name=zone_id,
        modus="automatik",
        feuchte_schwelle_min=30,
        feuchte_schwelle_max=60,
        feuchte_kritisch=20,
        max_dauer_sekunden=3600,
        wirkung_max_pp=wirkung_max_pp,
        wirkungsrate_initial=wirkungsrate_initial,
    )


def _baue_konfig(zone: ZonenKonfig | None = None) -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=[zone or _baue_zone()],
        wetter=WetterKonfig(),
    )


async def _schreibe_plateau_serie(
    s: Speicher,
    zone_id: str,
    *,
    wmax: float,
    r0: float,
    n: int,
    rauschen_pp: float = 0.3,
    dauer_min: float = 2.0,
    dauer_max: float = 40.0,
    jetzt: datetime = JETZT,
) -> None:
    """Schreibt `n` wirkungsrate-Records, die dem Plateau-Modell
    `delta = wmax * (1 - exp(-dauer/tau))` (tau=wmax/r0) folgen.
    Gespeichert wird `wert`=rate (delta/dauer) + `basis_mm`=dauer, exakt
    wie kalibrierung._scan_wirkungsrate es tut. Distinkte Stunden, weil
    speichere_kalibrierung auf die Stunde dedupt.
    """
    tau = wmax / r0
    rng = random.Random(42)
    for i in range(n):
        frac = i / max(n - 1, 1)
        dauer = dauer_min + (dauer_max - dauer_min) * frac
        delta = wmax * (1.0 - math.exp(-dauer / tau)) + rng.uniform(
            -rauschen_pp, rauschen_pp,
        )
        rate = delta / dauer
        ts = jetzt - timedelta(hours=i + 1)
        await s.speichere_kalibrierung(
            zeitstempel=ts, zone_id=zone_id, typ="wirkungsrate",
            wert=round(rate, 3), basis_mm=round(dauer, 1),
            notizen=f"delta={delta:.1f}pp",
        )


async def _schreibe_rausch_serie(
    s: Speicher, zone_id: str, *, n: int, jetzt: datetime = JETZT,
) -> None:
    """Schreibt `n` Records, deren delta UNABHAENGIG von der Dauer ist
    (reines Rauschen). Der Plateau-Fit darf das NICHT annehmen."""
    rng = random.Random(7)
    for i in range(n):
        frac = i / max(n - 1, 1)
        dauer = 2.0 + 38.0 * frac
        delta = rng.uniform(5.0, 35.0)  # keine Dauer-Abhaengigkeit
        rate = delta / dauer
        ts = jetzt - timedelta(hours=i + 1)
        await s.speichere_kalibrierung(
            zeitstempel=ts, zone_id=zone_id, typ="wirkungsrate",
            wert=round(rate, 3), basis_mm=round(dauer, 1),
            notizen=f"delta={delta:.1f}pp",
        )


# --------------------------------------------------------------------------
# Job-Gates
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_job_inaktiv(tmp_path):
    s = Speicher(str(tmp_path / "t.db"))
    await s.verbinden()
    try:
        wf = MlWirkungFitKonfig(aktiv=False)
        await _schreibe_plateau_serie(s, "bambuswald", wmax=20, r0=2.0, n=40)
        job = WirkungFitJob(s, _baue_konfig(), wf)
        assert await job.aktualisiere_wenn_faellig(JETZT) is False
        assert job.letzter_erfolg is None
        assert await s.hole_wirkung_fit("bambuswald") is None
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_intervall_gate(tmp_path):
    s = Speicher(str(tmp_path / "t.db"))
    await s.verbinden()
    try:
        wf = MlWirkungFitKonfig(aktiv=True, intervall_stunden=24, min_n=30)
        await _schreibe_plateau_serie(s, "bambuswald", wmax=20, r0=2.0, n=40)
        job = WirkungFitJob(s, _baue_konfig(), wf)
        assert await job.aktualisiere_wenn_faellig(JETZT) is True
        # 1 h spaeter: nicht faellig.
        assert await job.aktualisiere_wenn_faellig(
            JETZT + timedelta(hours=1)) is False
        # 25 h spaeter: wieder faellig.
        assert await job.aktualisiere_wenn_faellig(
            JETZT + timedelta(hours=25)) is True
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_skip_keine_paare(tmp_path):
    """Zone ohne wirkungsrate-Records -> kein Fit-Record persistiert."""
    s = Speicher(str(tmp_path / "t.db"))
    await s.verbinden()
    try:
        wf = MlWirkungFitKonfig(aktiv=True, min_n=30)
        job = WirkungFitJob(s, _baue_konfig(), wf)
        assert await job.aktualisiere_wenn_faellig(JETZT) is True  # Scan lief
        assert await s.hole_wirkung_fit("bambuswald") is None
    finally:
        await s.schliessen()


# --------------------------------------------------------------------------
# Fit-Qualitaet
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fit_angenommen_roundtrip(tmp_path):
    """Saubere Plateau-Daten -> angenommen=True, wmax/r0 nahe der Wahrheit."""
    s = Speicher(str(tmp_path / "t.db"))
    await s.verbinden()
    try:
        wf = MlWirkungFitKonfig(aktiv=True, min_n=30)
        await _schreibe_plateau_serie(s, "bambuswald", wmax=20, r0=2.0, n=40)
        job = WirkungFitJob(s, _baue_konfig(), wf)
        assert await job.aktualisiere_wenn_faellig(JETZT) is True
        fit = await s.hole_wirkung_fit("bambuswald")
        assert fit is not None
        assert fit["angenommen"] is True
        assert fit["grund"] == "ok"
        assert 16.0 <= fit["wmax"] <= 24.0   # ~20
        assert 1.5 <= fit["r0"] <= 2.6       # ~2.0
        assert fit["r2"] >= 0.5
        assert fit["n"] == 40
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_fit_abgelehnt_rauschen(tmp_path):
    """Rausch-Daten (delta unabhaengig von Dauer) -> angenommen=False,
    aber der Record wird persistiert (Beobachtung in /api/ml/status)."""
    s = Speicher(str(tmp_path / "t.db"))
    await s.verbinden()
    try:
        wf = MlWirkungFitKonfig(aktiv=True, min_n=30)
        await _schreibe_rausch_serie(s, "bambuswald", n=40)
        job = WirkungFitJob(s, _baue_konfig(), wf)
        assert await job.aktualisiere_wenn_faellig(JETZT) is True
        fit = await s.hole_wirkung_fit("bambuswald")
        assert fit is not None
        assert fit["angenommen"] is False
        assert fit["grund"] != "ok"
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_fit_abgelehnt_zu_wenig_paare(tmp_path):
    """Weniger als min_n Paare -> angenommen=False mit 'zu wenig'-Grund."""
    s = Speicher(str(tmp_path / "t.db"))
    await s.verbinden()
    try:
        wf = MlWirkungFitKonfig(aktiv=True, min_n=30)
        await _schreibe_plateau_serie(s, "bambuswald", wmax=20, r0=2.0, n=10)
        job = WirkungFitJob(s, _baue_konfig(), wf)
        assert await job.aktualisiere_wenn_faellig(JETZT) is True
        fit = await s.hole_wirkung_fit("bambuswald")
        assert fit is not None
        assert fit["angenommen"] is False
        assert "zu wenig" in fit["grund"].lower()
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_fenster_filtert_alte_records(tmp_path):
    """Records ausserhalb `fenster_tage` zaehlen nicht fuer den Fit."""
    s = Speicher(str(tmp_path / "t.db"))
    await s.verbinden()
    try:
        wf = MlWirkungFitKonfig(aktiv=True, min_n=30, fenster_tage=7)
        # 40 saubere Records, aber 80 Tage in der Vergangenheit.
        alt = JETZT - timedelta(days=80)
        await _schreibe_plateau_serie(
            s, "bambuswald", wmax=20, r0=2.0, n=40, jetzt=alt,
        )
        job = WirkungFitJob(s, _baue_konfig(), wf)
        assert await job.aktualisiere_wenn_faellig(JETZT) is True
        # Alle Paare ausserhalb des 7-Tage-Fensters -> keine Paare -> kein Fit.
        assert await s.hole_wirkung_fit("bambuswald") is None
    finally:
        await s.schliessen()


# --------------------------------------------------------------------------
# Engine-Adoption (observation-gated)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_aufgeloeste_wirkung_adoptieren_aus(tmp_path):
    """adoptieren=False (Default) -> (None, None, 'konfig'), selbst wenn ein
    angenommener Fit in der DB liegt. Perfekter No-op."""
    s = Speicher(str(tmp_path / "t.db"))
    await s.verbinden()
    try:
        await s.upsert_wirkung_fit(
            "bambuswald", wmax=22.0, r0=2.2, tau=10.0, n=40, r2=0.95,
            mse=0.5, angenommen=True, grund="ok", gefittet_am=JETZT,
        )
        zone = _baue_zone()
        motor = Entscheidungsmotor(
            s, None, [zone],
            wirkung_fit_konfig=MlWirkungFitKonfig(adoptieren=False),
        )
        wmax, r0, quelle = await motor._aufgeloeste_wirkung(zone, JETZT)
        assert (wmax, r0, quelle) == (None, None, "konfig")
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_aufgeloeste_wirkung_angenommen(tmp_path):
    """adoptieren=True + angenommener frischer Fit -> (wmax, r0, 'fit')."""
    s = Speicher(str(tmp_path / "t.db"))
    await s.verbinden()
    try:
        await s.upsert_wirkung_fit(
            "bambuswald", wmax=22.0, r0=2.2, tau=10.0, n=40, r2=0.95,
            mse=0.5, angenommen=True, grund="ok", gefittet_am=JETZT,
        )
        zone = _baue_zone()
        motor = Entscheidungsmotor(
            s, None, [zone],
            wirkung_fit_konfig=MlWirkungFitKonfig(adoptieren=True),
        )
        wmax, r0, quelle = await motor._aufgeloeste_wirkung(zone, JETZT)
        assert quelle == "fit"
        assert wmax == 22.0
        assert r0 == 2.2
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_aufgeloeste_wirkung_abgelehnt_bleibt_konfig(tmp_path):
    """adoptieren=True aber Fit angenommen=False -> Konfig."""
    s = Speicher(str(tmp_path / "t.db"))
    await s.verbinden()
    try:
        await s.upsert_wirkung_fit(
            "bambuswald", wmax=0.0, r0=0.0, tau=0.0, n=12, r2=0.1,
            mse=20.0, angenommen=False, grund="Quality-Gate verfehlt",
            gefittet_am=JETZT,
        )
        zone = _baue_zone()
        motor = Entscheidungsmotor(
            s, None, [zone],
            wirkung_fit_konfig=MlWirkungFitKonfig(adoptieren=True),
        )
        wmax, r0, quelle = await motor._aufgeloeste_wirkung(zone, JETZT)
        assert (wmax, r0, quelle) == (None, None, "konfig")
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_aufgeloeste_wirkung_zu_alt_bleibt_konfig(tmp_path):
    """adoptieren=True + angenommener aber zu alter Fit -> Konfig."""
    s = Speicher(str(tmp_path / "t.db"))
    await s.verbinden()
    try:
        await s.upsert_wirkung_fit(
            "bambuswald", wmax=22.0, r0=2.2, tau=10.0, n=40, r2=0.95,
            mse=0.5, angenommen=True, grund="ok",
            gefittet_am=JETZT - timedelta(days=40),
        )
        zone = _baue_zone()
        motor = Entscheidungsmotor(
            s, None, [zone],
            wirkung_fit_konfig=MlWirkungFitKonfig(
                adoptieren=True, max_fit_alter_tage=30,
            ),
        )
        wmax, r0, quelle = await motor._aufgeloeste_wirkung(zone, JETZT)
        assert (wmax, r0, quelle) == (None, None, "konfig")
    finally:
        await s.schliessen()


def test_berechne_dauer_override_noop(tmp_path):
    """`_berechne_dauer` ohne Override == Konfig-Verhalten; mit Override
    aenderbar (Adoption wirkt)."""
    zone = _baue_zone(wirkung_max_pp=15.0, wirkungsrate_initial=1.5)
    motor = Entscheidungsmotor(
        Speicher(str(tmp_path / "t.db")), None, [zone],
    )
    d_konfig = motor._berechne_dauer(
        zone, aktuelle_feuchte=20.0, et0_6h=0.0, ziel_schwelle=30.0,
    )
    d_none = motor._berechne_dauer(
        zone, aktuelle_feuchte=20.0, et0_6h=0.0, ziel_schwelle=30.0,
        wmax_override=None, r0_override=None,
    )
    assert d_konfig == d_none
    # Anderer Fit (groesseres wmax, schnellere Rate) -> kuerzere Dauer.
    d_fit = motor._berechne_dauer(
        zone, aktuelle_feuchte=20.0, et0_6h=0.0, ziel_schwelle=30.0,
        wmax_override=30.0, r0_override=3.0,
    )
    assert d_fit != d_konfig
    assert d_fit < d_konfig
