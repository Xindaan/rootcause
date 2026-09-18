"""T-0228 Stufe 2b: Memory-Seed-Import fuer Pflege-Erinnerungen.

Einmaliger Import der TASK.md-/Memory-Datums-Trigger in die
`pflege_erinnerung`-Tabelle. Idempotent: bestehende Eintraege mit
gleichem (typ, zone_id, faellig_am) werden uebersprungen.

Quellen:
- `arbeitspattern_conditional_trigger_calendar.md`-Pattern (Memory)
- TASK.md ## Backlog Trigger-Daten
- `fehlerpattern_substrat_stau_sensor_lock.md` (Memory)

Ausfuehrung:
    cd backend && /path/to/.venv/bin/python skripte/seed_pflege_erinnerungen.py

Bei spaeteren Triggern manuell weitere Eintraege via UI / curl
oder ein Folgeskript anlegen.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


# Datums-Trigger aus TASK.md / Memory-Index. Format:
# (typ, zone_id|None, faellig_am, intervall_tage|None, beschreibung)
SEEDS = [
    # zitrus-Substrat-Stau wurde am 25.05. bei T-0228 Stufe 1 direkt
    # eingespielt (id=1). Hier zur Vollstaendigkeit erneut, idempotent.
    (
        "beobachtung", "zitrus", "2026-06-06T12:00:00", None,
        "zitrus Substrat-Stau Nachschau (Memory "
        "fehlerpattern_substrat_stau_sensor_lock.md; Tropfer-Rate auf "
        "Minimum gedreht 23.05.).",
    ),

    # Standort-Reorganisation Berlin Folge (T-0165a-f, 09.05. +2-3 Wochen)
    (
        "kalibrierung", "kasten_4", "2026-05-30T12:00:00", None,
        "T-0165a Realdaten-Validierung Mischkasten (4 Spezies, Schwellen "
        "35/60/20). Tagesmin-p10 ueber 2-3 Trockenphasen pruefen + "
        "Welkepunkt-Auto-Aufloesung greifen lassen.",
    ),
    (
        "kalibrierung", "zitrus", "2026-05-30T12:00:00", None,
        "T-0165b Zitrobaer Outdoor-Kalibrierung. Tagesmin-p10 vs. "
        "Indoor-Historie vergleichen; feuchte_schwelle_min ggf. auf "
        "28-32 anheben.",
    ),
    (
        "kalibrierung", "mandevilla_maxi", "2026-05-30T12:00:00", None,
        "T-0165d Mandevilla Maxibaer Nordbalkon-Kalibrierung. "
        "Tagesmin-Verteilung vs. Suedbalkon-Stand.",
    ),
    (
        "kalibrierung", "pilea", "2026-05-30T12:00:00", None,
        "T-0165e Pilea Nordbalkon-Kalibrierung. Tagesmin-p10 bestaetigen.",
    ),
    (
        "kalibrierung", "fuchsie", "2026-05-30T12:00:00", None,
        "T-0165f Fuchsie Indoor-Kalibrierung. Tagesmin-p10 vs. alte "
        "Indoor-Werte (falls vorhanden) abgleichen.",
    ),

    # Mandevilla Suedbalkon-Outdoor-Kalibrierung
    (
        "kalibrierung", "mandevilla", "2026-06-01T12:00:00", None,
        "T-0165i Mandevilla Suedbalkon-Outdoor-Kalibrierung. Schwellen "
        "28/50/18 sind Indoor-Stand, Suedbalkon trocknet schneller. "
        "Nach 2-3 Wochen Outdoor-Realdaten Tagesmin-p10 pruefen.",
    ),

    # T-0179a Plateau-Modell-Re-Fit (4-6 Wochen nach FYTA-Inbetriebnahme).
    # Konkretes Datum unklar weil Sensoren noch nicht installiert sind --
    # 15.06. als grobe Annahme.
    (
        "ml_kalibrierung", "waldblumenhain", "2026-06-15T12:00:00", None,
        "T-0179a Plateau-Modell-Re-Fit nach 4-6 Wochen FYTA-Datenphase. "
        "Sprinkler-Becher-Test-Befund mit drei Sensor-Stroemen abgleichen, "
        "wirkung_max_pp + wirkungsrate_initial neu fitten.",
    ),

    # T-0067 Waldblumenhain Dosis-Experiment (bedingter Trigger)
    (
        "beobachtung", "waldblumenhain", "2026-06-15T08:00:00", None,
        "T-0067 Dosis-Experiment 30 -> 90 min pruefen, sobald: Sensor "
        "<= 45 % UND Boden-T >= 15 C UND keine Regen-Prognose 48 h. "
        "Wenn Bedingungen nicht erfuellt: Erinnerung verschieben.",
    ),

    # T-0249 Folge: yogaraum-Sensor + wirkung_max_pp.
    # Visueller Check 25.05. zeigt: KEIN Tropfer-Hotspot (Bilder im
    # Chat-Log). Alternative Ursachen: Sensor-Tiefe, lokale Verdichtung
    # am Schlauch entlang, Hardware-Varianz. Aktion 3
    # (wirkung_max_pp hoch) wahrscheinlich der echte Fix.
    (
        "ml_kalibrierung", "bambuswald_yogaraum",
        "2026-06-08T12:00:00", None,
        "T-0249 Aktion 2/3: yogaraum-Sensor-Position 25.05. visuell "
        "ueberprueft -- KEIN Tropfer-Hotspot (Bambuswald-Sensor liegt "
        "aehnlich, ohne Drift). Alternative Ursachen: Sensor-Tiefe, "
        "lokale Verdichtung am Mikrodrip-Schlauch, Hardware-Varianz. "
        "Aktion: Sensor evtl. 2-3 cm tiefer eindruecken (NICHT "
        "versetzen) + 48h ml_ausschluss. Falls Drift nach 2 Wochen "
        "weiter da: wirkung_max_pp 8 -> 12-15 (das ist wahrscheinlich "
        "der echte Fix, weil Auto-Kalibrierungs-Median 0.334 pp/min "
        "real beobachtbares Plateau anzeigt -- nicht durch Position "
        "verursacht).",
    ),

    # Etablierungs-Jahr-Senkungen (in 1 Jahr)
    (
        "schwellen_senkung", "hecke", "2027-05-01T12:00:00", None,
        "T-0163 Hecke-Schwellen auf 'etabliert' senken (1 Jahr nach "
        "Pflanzung Ostern 2026). Pre-Check: visueller Hecken-Status. "
        "min 28, max 50, kritisch 18, optimum 35-45.",
    ),
    (
        "schwellen_senkung", "magerwiese", "2027-05-01T12:00:00", None,
        "T-0159 Magerwiese-Schwellen auf 'etabliert' senken (1 Jahr nach "
        "Hauptsaat 14.03.2026). Pre-Check: Sensor-Tagesmin Mai-Juni 2027 "
        "ansehen; nicht senken wenn Spezies-Verlust sichtbar. "
        "min 18, max 35, kritisch 12, optimum 22-32.",
    ),
]


def importiere(db_pfad: Path) -> tuple[int, int]:
    """Liefert (neu_angelegt, uebersprungen)."""
    c = sqlite3.connect(str(db_pfad), timeout=15.0)
    c.row_factory = sqlite3.Row
    jetzt_iso = datetime.now().isoformat()

    neu = 0
    skip = 0
    for typ, zone_id, faellig, intervall, beschreibung in SEEDS:
        # Dedup-Check
        if zone_id is None:
            row = c.execute(
                """SELECT id FROM pflege_erinnerung
                   WHERE typ = ? AND zone_id IS NULL AND faellig_am = ?""",
                (typ, faellig),
            ).fetchone()
        else:
            row = c.execute(
                """SELECT id FROM pflege_erinnerung
                   WHERE typ = ? AND zone_id = ? AND faellig_am = ?""",
                (typ, zone_id, faellig),
            ).fetchone()
        if row:
            skip += 1
            continue
        c.execute(
            """INSERT INTO pflege_erinnerung
               (zone_id, typ, faellig_am, intervall_tage, beschreibung,
                quelle, angelegt_am)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                zone_id, typ, faellig, intervall, beschreibung,
                "memory", jetzt_iso,
            ),
        )
        neu += 1

    c.commit()
    c.close()
    return neu, skip


if __name__ == "__main__":
    pfad = Path(__file__).resolve().parents[1] / "daten" / "bewaesserung.db"
    if not pfad.exists():
        raise SystemExit(f"DB nicht gefunden: {pfad}")
    neu, skip = importiere(pfad)
    print(f"Importiert: {neu} neu, {skip} bereits vorhanden (uebersprungen).")
