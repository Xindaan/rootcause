"""T-0335: Giess-Historie -- gruppiert rohe Ventil-Events zu Giess-Laeufen.

Single Source der Lauf-Gruppierung (Backend, pytest-getestet) -- das Frontend
zeigt nur an. Stuetzt sich auf den T-0335-Backend-Marker (`lauf_gruppe`/`phase`)
statt auf fragile zeitliche Heuristik.

Begriffe:
- *Puls* = ein OEFFNEN + sein SCHLIESSEN (Dauer aus dem SCHLIESSEN).
- *Lauf* = was der User als eine Bewaesserung versteht:
  - Pulse mit gleicher `lauf_gruppe` = EIN Pre-Soak-Lauf (Phasen pre_soak/haupt).
  - Puls ohne `lauf_gruppe` = ein Einzel-Lauf.
- `zaehlt=False` fuer ignoriert/unbekannt/aquabloom -> keine echte Zonen-
  Bewaesserung (Cross-Spray/Phantom; s. Memory magerwiese_grass_regime, T-0332).
"""

from __future__ import annotations

from .modelle import Ausloser, VentilAktion, VentilEreignis

# Ausloeser, die KEINE echte Zonen-Bewaesserung sind (nicht mitzaehlen).
_NICHT_ZAEHLEND = {
    Ausloser.IGNORIERT.value,
    Ausloser.UNBEKANNT.value,
    Ausloser.AQUABLOOM.value,
}


def _puls_dict(start: VentilEreignis | None, schliessen: VentilEreignis) -> dict:
    """Baut einen abgeschlossenen Puls aus OEFFNEN (optional) + SCHLIESSEN."""
    return {
        "start": (start or schliessen).zeitstempel,
        "ende": schliessen.zeitstempel,
        "dauer_s": schliessen.dauer_sekunden,
        "ausloser": schliessen.ausloser.value,
        "lauf_gruppe": schliessen.lauf_gruppe,
        "phase": schliessen.phase,
        "liter": schliessen.liter,
        "laeuft_noch": False,
        "zone_id": schliessen.zone_id,
        "ventil_id": schliessen.ventil_id,
    }


def _offener_puls_dict(o: VentilEreignis, *, laeuft_noch: bool) -> dict:
    """Puls fuer ein OEFFNEN ohne SCHLIESSEN.

    laeuft_noch=True: juengstes OEFFNEN, Lauf plausibel noch aktiv.
    laeuft_noch=False: Waise -- ein spaeteres OEFFNEN beweist, dass das
    Ventil zwischenzeitlich zu war (Close-Event verloren); Ende unbekannt.
    """
    return {
        "start": o.zeitstempel,
        "ende": None,
        "dauer_s": None,
        "ausloser": o.ausloser.value,
        "lauf_gruppe": o.lauf_gruppe,
        "phase": o.phase,
        "liter": o.liter,
        "laeuft_noch": laeuft_noch,
        "zone_id": o.zone_id,
        "ventil_id": o.ventil_id,
    }


def gruppiere_giess_laeufe(ereignisse: list[VentilEreignis]) -> list[dict]:
    """Formt rohe Ventil-Events einer Zone zu Giess-Laeufen (neueste zuerst).

    Erwartet die Events EINER Zone (Aufrufer filtert per zone_id). Paart
    OEFFNEN->SCHLIESSEN je `ventil_id` (FIFO), gruppiert die Pulse nach
    `lauf_gruppe` und liefert Lauf-Dicts mit Methode/Dauer/Ausloeser/Phasen.
    """
    evs = sorted(ereignisse, key=lambda e: e.zeitstempel)

    # 1) OEFFNEN -> SCHLIESSEN zu Pulsen paaren (FIFO pro ventil_id).
    offene: dict[str, list[VentilEreignis]] = {}
    pulse: list[dict] = []
    for e in evs:
        if e.aktion == VentilAktion.OEFFNEN:
            warteschlange = offene.setdefault(e.ventil_id, [])
            # Ein Ventil kann physisch nur einmal offen sein: ein aelteres
            # OEFFNEN ohne SCHLIESSEN ist ein Waise (Close-Event verloren,
            # z.B. WS-Drop) und darf NICHT das SCHLIESSEN des naechsten
            # Laufs konsumieren -- sonst kaskadiert die FIFO-Paarung ueber
            # alle Folge-Laeufe (Realfall magerwiese 28.06./10.07.2026).
            for o in warteschlange:
                pulse.append(_offener_puls_dict(o, laeuft_noch=False))
            warteschlange.clear()
            warteschlange.append(e)
        elif e.aktion == VentilAktion.SCHLIESSEN:
            warteschlange = offene.get(e.ventil_id) or []
            start = warteschlange.pop(0) if warteschlange else None
            pulse.append(_puls_dict(start, e))
    # Noch offene OEFFNEN ohne SCHLIESSEN = laufender Puls.
    for warteschlange in offene.values():
        for o in warteschlange:
            pulse.append(_offener_puls_dict(o, laeuft_noch=True))

    pulse.sort(key=lambda p: p["start"])

    # 2) Pulse zu Laeufen gruppieren. Gleiche lauf_gruppe -> Pre-Soak; sonst
    #    je ein Einzel-Lauf. Reihenfolge stabil ueber den ersten Puls.
    laeufe: list[dict] = []
    gruppen_index: dict[str, int] = {}
    for p in pulse:
        gruppe = p["lauf_gruppe"]
        if gruppe and gruppe in gruppen_index:
            laeufe[gruppen_index[gruppe]]["_pulse"].append(p)
            continue
        eintrag = {"_pulse": [p], "_gruppe": gruppe}
        if gruppe:
            gruppen_index[gruppe] = len(laeufe)
        laeufe.append(eintrag)

    # 3) Lauf-Dicts ausformen.
    ergebnis: list[dict] = []
    for eintrag in laeufe:
        ps = eintrag["_pulse"]
        is_pre_soak = bool(eintrag["_gruppe"]) and len(ps) >= 1
        # Pre-Soak nur dann als solchen labeln, wenn der Marker gesetzt ist
        # (auch ein 1-Puls-Rest eines Pre-Soak bleibt korrekt zugeordnet).
        methode = "pre_soak" if eintrag["_gruppe"] else "einzel"
        dauern = [p["dauer_s"] for p in ps if p["dauer_s"] is not None]
        dauer_gesamt = sum(dauern) if dauern else None
        liter = [p["liter"] for p in ps if p["liter"] is not None]
        ausloser = ps[0]["ausloser"]
        ergebnis.append({
            "start": ps[0]["start"].isoformat(),
            "ende": ps[-1]["ende"].isoformat() if ps[-1]["ende"] else None,
            "methode": methode,
            "ausloser": ausloser,
            "zaehlt": ausloser not in _NICHT_ZAEHLEND,
            "laeuft_noch": any(p["laeuft_noch"] for p in ps),
            "dauer_gesamt_s": dauer_gesamt,
            "liter_gesamt": round(sum(liter), 1) if liter else None,
            # zone_ids: Liste, damit die Multi-Zonen-Aggregation (serieller Strang)
            # mehrere Zonen unter einem physischen Lauf fuehren kann. ventil_id =
            # Dedup-Anker dafuer.
            "zone_ids": [ps[0]["zone_id"]],
            "ventil_id": ps[0]["ventil_id"],
            "phasen": [
                {
                    "phase": p["phase"] or ("haupt" if is_pre_soak else None),
                    "start": p["start"].isoformat(),
                    "dauer_s": p["dauer_s"],
                }
                for p in ps
            ],
        })

    # Neueste zuerst (Historie-Konvention).
    ergebnis.sort(key=lambda lauf: lauf["start"], reverse=True)
    return ergebnis


def aggregiere_zonen_laeufe(laeufe: list[dict]) -> list[dict]:
    """Merged Giess-Laeufe mehrerer Zonen zu EINER chronologischen Liste und
    dedupliziert physische Laeufe, die mehrere Zonen am selben Ventil betreffen.

    Hintergrund: Ein serieller Strang (z.B. Bambus: bambuswald +
    bambuswald_yogaraum am selben Kanal/Ventil) schreibt pro Zone ein eigenes
    OEFFNEN/SCHLIESSEN -- `gruppiere_giess_laeufe` (pro Zone aufgerufen) erzeugt
    daraus N separate Laeufe fuer EINEN physischen Lauf. Hier zu EINEM Eintrag
    zusammengefuehrt, dessen `zone_ids` alle betroffenen Zonen listet.

    Dedup-Anker: (ventil_id, Start bis zur Sekunde). Die pro-Zone-Events eines
    Laufs entstehen im selben Schreib-Loop (Sub-Millisekunden auseinander) ->
    identische Sekunde; echte separate Laeufe liegen Minuten auseinander.
    Neueste zuerst.
    """
    nach_anker: dict[tuple[str, str], dict] = {}
    for lauf in sorted(laeufe, key=lambda l: l["start"]):
        anker = (lauf.get("ventil_id") or "", lauf["start"][:19])
        vorhanden = nach_anker.get(anker)
        if vorhanden is None:
            nach_anker[anker] = {**lauf, "zone_ids": list(lauf.get("zone_ids", []))}
        else:
            for zid in lauf.get("zone_ids", []):
                if zid not in vorhanden["zone_ids"]:
                    vorhanden["zone_ids"].append(zid)
    merged = list(nach_anker.values())
    merged.sort(key=lambda lauf: lauf["start"], reverse=True)
    return merged
