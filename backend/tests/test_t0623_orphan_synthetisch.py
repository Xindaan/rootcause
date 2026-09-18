"""T-0623: geschaetzte Orphan-Closes (Herkunft `orphan_zeitbasiert`, T-0595)
auch aus dem K-Basis-Fit heraus -- wie T-0609 fuer Wirkungsfit, ML-Pulse und
Leck.

Verhaltenstest ueber den echten Lesepfad: Ereignisse landen per
`speichere_ventil_ereignis(herkunft=...)` in einer temporaeren DB, der Job
liest sie ueber `hole_alle_ventil_ereignisse`, das `orphan_synthetisch` setzt.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta

import pytest

from bewaesserung.ml.k_basis_fit_job import KbasisFitJob
from bewaesserung.modelle import (
    Ausloser,
    DatenQuelle,
    MlPhysikDiagnoseKonfig,
    SensorMessung,
    VentilAktion,
    VentilEreignis,
)
from bewaesserung.speicher import HERKUNFT_ORPHAN_ZEITBASIERT, Speicher

from test_k_basis_fit_job import _baue_konfig

JETZT = datetime(2026, 5, 27, 12, 0, 0)
ET0 = 0.1042


async def _k_basis_n_phasen(tmp_path, herkunft_dritter_anker):
    """Vier saubere Trockenphasen (k=0.02); der dritte Anker traegt die
    uebergebene Herkunft. Rueckgabe: `n_phasen` des persistierten Fits."""
    s = Speicher(str(tmp_path / "t0623.db"))
    await s.verbinden()
    try:
        wp, true_k = 20.0, 0.02
        starts = [JETZT - timedelta(days=d) for d in (20, 15, 10, 5)]
        for i, start in enumerate(starts):
            # Lauf vor dem Anker: OEFFNEN + SCHLIESSEN (Automatik).
            await s.speichere_ventil_ereignis(VentilEreignis(
                zeitstempel=start - timedelta(seconds=1800), zone_id="z1",
                ventil_id="v1", aktion=VentilAktion.OEFFNEN,
                dauer_sekunden=0, ausloser=Ausloser.AUTOMATIK,
            ))
            await s.speichere_ventil_ereignis(
                VentilEreignis(
                    zeitstempel=start, zone_id="z1", ventil_id="v1",
                    aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1800,
                    ausloser=Ausloser.WATCHDOG,
                ),
                herkunft=herkunft_dritter_anker if i == 2 else None,
            )
            # Naechster Lauf 60 h spaeter beendet die Phase.
            await s.speichere_ventil_ereignis(VentilEreignis(
                zeitstempel=start + timedelta(hours=60), zone_id="z1",
                ventil_id="v1", aktion=VentilAktion.OEFFNEN,
                dauer_sekunden=0, ausloser=Ausloser.AUTOMATIK,
            ))
            for h in [1, 3, 6, 12, 24, 36, 48]:
                await s.speichere_messung(SensorMessung(
                    zeitstempel=start + timedelta(hours=h),
                    zone_id="z1", geraet_id="g1",
                    boden_feuchte=wp + 50.0 * math.exp(-true_k * h),
                    quelle=DatenQuelle.GARDENA,
                ))
        for t in range(0, 30 * 24):
            await s._db.execute(
                "INSERT OR IGNORE INTO wetter_archiv "
                "(zeitstempel, standort_id, niederschlag_mm, "
                "temperatur, et0_mm, abgerufen_am) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ((JETZT - timedelta(hours=t)).isoformat(), "standort_a",
                 0.0, 20.0, ET0, JETZT.isoformat()),
            )
        await s._db.commit()
        physik = MlPhysikDiagnoseKonfig(
            aktiv=True, min_phasen=3, min_phasen_dauer_h=6,
            max_phasen_dauer_h=72, et0_basis_mm_pro_h=ET0,
        )
        job = KbasisFitJob(s, _baue_konfig(), physik)
        assert await job.aktualisiere_wenn_faellig(JETZT) is True
        assert job.letzter_fehler is None
        row = await s.hole_k_basis("z1")
        assert row is not None
        return row["n_phasen"]
    finally:
        await s.schliessen()


@pytest.mark.parametrize(
    ("herkunft", "erwartet"),
    [
        # Echter Close (keine Herkunft wie Bestandsdaten, Watchdog-Stopp):
        # Anker bleibt, alle vier Phasen im Fit.
        (None, 4),
        # Geschaetzter Orphan-Close: kein Anker, nur drei Phasen.
        (HERKUNFT_ORPHAN_ZEITBASIERT, 3),
    ],
)
def test_t0623_k_basis_fit_ohne_synthetischen_anker(tmp_path, herkunft,
                                                   erwartet):
    assert asyncio.run(_k_basis_n_phasen(tmp_path, herkunft)) == erwartet
