"""T-0610 EL-8 (Lesart I2): Drift-Ampel der Dosis-Vorschlaege bewertet nur
GEFAHRENE Dosen, in einer Einheit (pp Feuchte-Anstieg in 6 h).

Vorher (T-0065) wurde jede `ml_dauer_vorschlag`-Zeile bewertet, auch nie
gefahrene, und die Prognose war `sekunden / 60` (Minuten) gegen ein Ist in
Prozentpunkten. Jetzt:

- Gefahren = ein Puls aus der Response-Feature-Pipeline
  (`erstelle_response_features`), also exakt die Laeufe, die auch das
  Training sieht (Filter laut deren Modul-Doku "isomorph in Training +
  Drift-Evaluation"). Der Puls muss in `[t_vorschlag, t_vorschlag +
  ZUORDNUNG_MIN]` beginnen.
- Je Lauf genau ein Vorschlag: der juengste offene Vorschlag DERSELBEN Zone
  vor dem Pulsstart. Seit EL-3 (A2) schreibt eine scharfe Kanal-Zone keinen
  eigenen Vorschlag mehr; dort ist das der Kanal-Vorschlag (zone_id = die
  Startzone des Kanals), sonst der Zonen-Vorschlag. Ein Vorschlag nimmt
  hoechstens einen Lauf.
- Ist = Label `delta_6h` des Pulses (Feuchte 6 h nach Pulsende minus
  Feuchte vor dem Start, gleicher Sensor) -- dasselbe Label, auf das das
  Forward-Modell trainiert ist.
- Heuristik = Inverse von `_berechne_dauer` fuer die gefahrene Dauer
  (aufgeloeste Wirkungsrate/Plateau des Motors, ET0 aus dem Vorschlag).
- ML = Forward-q50 des Response-Modells mit den Merkmalen des Pulses.

Abschluss ohne Lauf: ist `ABSCHLUSS_NACH_H` nach dem Vorschlag kein Puls
zugeordnet (oder hat der Puls kein 6-h-Label), wird die Zeile mit
`gefahrene_dauer_s` NULL bzw. ohne Fehler abgeschlossen und zaehlt nicht in
die Ampel. Keine Steuerwirkung: nur Diagnose (Freigabe-Tab, Watchdog-Push).
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Awaitable, Callable

import structlog

logger = structlog.get_logger()

# Zwischen Vorschlag und Pulsstart: der Auto-Loop startet im selben Zyklus,
# ein Pre-Soak beginnt mit dem ersten Puls ebenfalls sofort. 30 min decken
# zwei bis drei Loop-Zyklen Verzug (Hahn belegt, Retry nach Fehlversuch).
ZUORDNUNG_MIN = 30
# Bis dahin muss ein Lauf, der im Zuordnungsfenster begann, beendet sein und
# sein 6-h-Label haben (6 h + 90 min Messtoleranz + Puls-Cluster). Danach
# wird die Zeile ohne Lauf abgeschlossen.
ABSCHLUSS_NACH_H = 18

HeuristikPp = Callable[[str, float, float, datetime], Awaitable[float | None]]
MlPp = Callable[[str, dict], float | None]
FeatureQuelle = Callable[[datetime, datetime], Awaitable[list[dict]]]


def _zahl(wert) -> float | None:
    try:
        f = float(wert)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _et0_6h(features_json: str | None) -> float:
    try:
        daten = json.loads(features_json or "{}")
    except (TypeError, ValueError):
        return 0.0
    return _zahl(daten.get("et0_6h")) or 0.0


async def bewerte_dauer_vorschlaege(
    speicher,
    jetzt: datetime,
    *,
    pulse_laden: FeatureQuelle,
    heuristik_pp: HeuristikPp,
    ml_pp: MlPp,
    limit: int = 500,
) -> int:
    """Bewertet faellige Vorschlaege. Rueckgabe: Anzahl abgeschlossener Zeilen."""
    offen = await speicher.hole_dauer_vorschlaege_unbewertet(
        bis_zeitstempel=jetzt, limit=limit,
    )
    if not offen:
        return 0
    for v in offen:
        v["_zs"] = datetime.fromisoformat(v["zeitstempel"])
    von = min(v["_zs"] for v in offen)
    bis = max(v["_zs"] for v in offen) + timedelta(minutes=ZUORDNUNG_MIN)
    pulse = await pulse_laden(von, bis)

    # Zuordnung: je Puls der juengste Vorschlag derselben Zone im Fenster
    # davor -- gesucht unter ALLEN Vorschlaegen, auch schon bewerteten. Ist
    # der juengste schon bewertet, hat er den Lauf in einem frueheren Takt
    # bekommen; ein aelterer offener Vorschlag erbt ihn dann nicht.
    alle = await speicher.hole_dauer_vorschlag_zeiten(
        von - timedelta(minutes=ZUORDNUNG_MIN), bis,
    )
    nach_zone: dict[str, list[tuple[datetime, int]]] = {}
    for z in alle:
        nach_zone.setdefault(str(z["zone_id"]), []).append(
            (datetime.fromisoformat(z["zeitstempel"]), int(z["id"])),
        )
    offen_ids = {int(v["id"]) for v in offen}
    zuordnung: dict[int, dict] = {}
    for puls in sorted(pulse, key=lambda p: p["zeitstempel"]):
        t_start = datetime.fromisoformat(str(puls["zeitstempel"]))
        kandidaten = [
            (zs, rid) for zs, rid in nach_zone.get(str(puls["zone_id"]), [])
            if zs <= t_start <= zs + timedelta(minutes=ZUORDNUNG_MIN)
        ]
        if not kandidaten:
            continue
        _zs, rid = max(kandidaten)
        if rid in offen_ids and rid not in zuordnung:
            zuordnung[rid] = puls

    abgeschlossen = 0
    for v in offen:
        puls = zuordnung.get(int(v["id"]))
        final = jetzt >= v["_zs"] + timedelta(hours=ABSCHLUSS_NACH_H)
        ist = _zahl(puls.get("delta_6h")) if puls is not None else None
        if puls is None or ist is None:
            if not final:
                continue  # Lauf oder Label kann noch kommen
            await speicher.markiere_dauer_vorschlag_bewertet(
                row_id=int(v["id"]), bewertet_am=jetzt,
                ist_delta_6h=None, heuristik_prognose_delta=None,
                ml_prognose_delta=None, heuristik_fehler=None, ml_fehler=None,
                gefahrene_dauer_s=(
                    int(puls["dauer_s"]) if puls is not None else None
                ),
            )
            abgeschlossen += 1
            continue
        dauer_s = float(puls["dauer_s"])
        heur = await heuristik_pp(
            str(v["zone_id"]), dauer_s, _et0_6h(v.get("features_json")), v["_zs"],
        )
        if heur is None:
            # Zone nicht (mehr) konfiguriert: nicht bewertbar, abschliessen.
            heur_fehler = None
        else:
            heur_fehler = abs(heur - ist)
        ml = ml_pp(str(v["zone_id"]), puls)
        ml = _zahl(ml)
        await speicher.markiere_dauer_vorschlag_bewertet(
            row_id=int(v["id"]), bewertet_am=jetzt,
            ist_delta_6h=ist,
            heuristik_prognose_delta=heur,
            ml_prognose_delta=ml,
            heuristik_fehler=heur_fehler,
            ml_fehler=abs(ml - ist) if ml is not None else None,
            gefahrene_dauer_s=int(dauer_s),
        )
        abgeschlossen += 1
    return abgeschlossen


class DauerDriftBewerter:
    """Verdrahtung fuer den Drift-Job: Speicher, Konfig (Puls-Pipeline) und
    Motor (Wirkungsrate, Forward-Modell)."""

    def __init__(self, speicher, konfig, motor):
        self._speicher = speicher
        self._konfig = konfig
        self._motor = motor

    async def _pulse(self, von: datetime, bis: datetime) -> list[dict]:
        from bewaesserung.ml.response_features import erstelle_response_features

        df = await erstelle_response_features(self._speicher, self._konfig, von, bis)
        if df is None or len(df) == 0:
            return []
        return df.to_dict(orient="records")

    async def bewerte(self, jetzt: datetime) -> int:
        return await bewerte_dauer_vorschlaege(
            self._speicher, jetzt,
            pulse_laden=self._pulse,
            heuristik_pp=self._motor.heuristik_wirkung_pp,
            ml_pp=self._motor.ml_forward_pp,
        )
