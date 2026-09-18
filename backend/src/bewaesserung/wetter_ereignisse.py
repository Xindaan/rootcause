"""Erkennung von Wetter-Ereignissen aus Vorhersagedaten.

Prueft periodisch auf Frost, Hitze und Starkregen und speichert
erkannte Ereignisse in der DB (dedupliziert pro Tag + Standort).
ML-relevant: Frost beeinflusst Bewaesserungsentscheidungen und Pflanzenstress.
"""

from datetime import datetime, timedelta

import structlog

from bewaesserung.modelle import WetterEreignis, WetterEreignisTyp, WetterVorhersage
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

# Schwellenwerte
FROST_SCHWELLE_C = 0.0
HITZE_SCHWELLE_C = 35.0
STARKREGEN_SCHWELLE_MM_3H = 15.0


async def pruefe_wetter_ereignisse(
    vorhersage: WetterVorhersage,
    speicher: Speicher,
    standort_id: str,
) -> list[WetterEreignis]:
    """Prueft eine Vorhersage auf relevante Wetter-Ereignisse.

    Dedupliziert: Pro Typ + Standort wird max. ein Ereignis pro Tag gespeichert.
    Gibt die neu erkannten Ereignisse zurueck.
    """
    jetzt = datetime.now()
    heute_start = jetzt.replace(hour=0, minute=0, second=0, microsecond=0)
    naechste_12h = [
        s for s in vorhersage.stunden
        if jetzt <= s.zeitstempel <= jetzt + timedelta(hours=12)
    ]

    if not naechste_12h:
        return []

    neue_ereignisse: list[WetterEreignis] = []

    # --- Frost ---
    frost_stunden = [s for s in naechste_12h if s.temperatur < FROST_SCHWELLE_C]
    if frost_stunden:
        min_temp_stunde = min(frost_stunden, key=lambda s: s.temperatur)
        bereits = await speicher.wetter_ereignis_existiert(
            WetterEreignisTyp.FROST, standort_id, heute_start
        )
        if not bereits:
            ereignis = WetterEreignis(
                zeitstempel=jetzt,
                typ=WetterEreignisTyp.FROST,
                standort_id=standort_id,
                details=(
                    f"Min {min_temp_stunde.temperatur:.1f}°C "
                    f"um {min_temp_stunde.zeitstempel.strftime('%H:%M')}"
                ),
                beginn=frost_stunden[0].zeitstempel,
                ende=frost_stunden[-1].zeitstempel,
            )
            await speicher.speichere_wetter_ereignis(ereignis)
            neue_ereignisse.append(ereignis)
            logger.warning(
                "wetter.frost_erkannt",
                standort=standort_id,
                min_temp=min_temp_stunde.temperatur,
                beginn=frost_stunden[0].zeitstempel.strftime("%H:%M"),
            )

    # --- Hitze ---
    hitze_stunden = [s for s in naechste_12h if s.temperatur > HITZE_SCHWELLE_C]
    if hitze_stunden:
        max_temp_stunde = max(hitze_stunden, key=lambda s: s.temperatur)
        bereits = await speicher.wetter_ereignis_existiert(
            WetterEreignisTyp.HITZE, standort_id, heute_start
        )
        if not bereits:
            ereignis = WetterEreignis(
                zeitstempel=jetzt,
                typ=WetterEreignisTyp.HITZE,
                standort_id=standort_id,
                details=(
                    f"Max {max_temp_stunde.temperatur:.1f}°C "
                    f"um {max_temp_stunde.zeitstempel.strftime('%H:%M')}"
                ),
                beginn=hitze_stunden[0].zeitstempel,
                ende=hitze_stunden[-1].zeitstempel,
            )
            await speicher.speichere_wetter_ereignis(ereignis)
            neue_ereignisse.append(ereignis)
            logger.warning(
                "wetter.hitze_erkannt",
                standort=standort_id,
                max_temp=max_temp_stunde.temperatur,
            )

    # --- Starkregen (3h-Fenster) ---
    for i in range(len(naechste_12h) - 2):
        summe_3h = sum(naechste_12h[j].niederschlag_mm for j in range(i, i + 3))
        if summe_3h >= STARKREGEN_SCHWELLE_MM_3H:
            bereits = await speicher.wetter_ereignis_existiert(
                WetterEreignisTyp.STARKREGEN, standort_id, heute_start
            )
            if not bereits:
                ereignis = WetterEreignis(
                    zeitstempel=jetzt,
                    typ=WetterEreignisTyp.STARKREGEN,
                    standort_id=standort_id,
                    details=f"{summe_3h:.1f} mm in 3h ab {naechste_12h[i].zeitstempel.strftime('%H:%M')}",
                    beginn=naechste_12h[i].zeitstempel,
                    ende=naechste_12h[i + 2].zeitstempel,
                )
                await speicher.speichere_wetter_ereignis(ereignis)
                neue_ereignisse.append(ereignis)
                logger.warning(
                    "wetter.starkregen_erkannt",
                    standort=standort_id,
                    mm_3h=summe_3h,
                )
            break  # Nur ein Starkregen-Event pro Durchlauf

    return neue_ereignisse
