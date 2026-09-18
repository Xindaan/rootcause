"""T-0447 / T-0446: eine Wahrheit fuer "auf diesem Kanal laeuft ein Vorgang".

Vor diesem Modul gab es zwei Implementierungen derselben Frage, in zwei
Modulen, mit derselben Logik:

- `entscheidung._kanal_aktiv_bewaesserung` (T-0119)
- `sensor_backfill._aktive_bewaesserung_auf_kanal` (T-0123)

Sie sind bereits auseinandergelaufen: die T-0443-Frische-Schranke landete nur
in der ersten. Genau der Verlauf, den die Single-Source-of-Truth-Regel der
Projekt-CLAUDE.md verhindern soll -- zwei Wahrheiten driften nicht sofort
auseinander, sondern beim naechsten Fix.

**Zwei Zustandstabellen, nicht eine** (T-0446). `live_lauf_state` sagt "ein
Ventil ist offen". Das reicht nicht: waehrend der Soak-Pause eines Pre-Soaks
ist das Ventil ZU (Bambus: 25 min zwischen Puls und Hauptlauf), der Vorgang
laeuft aber weiter. Wer nur das Ventil prueft, haelt die Pause faelschlich
fuer "nichts los" -- und der scharfe Auto-Loop tut das nicht (er prueft in
`main.py` den Pre-Soak-Lauf und ueberspringt den Kanal ueber die ganze
Sequenz). Ohne `pre_soak_state` haetten Shadow-Push und scharfes Verhalten
also unterschiedliche Wahrheiten.

**Warum die beiden Tabellen verschieden gematcht werden.** `live_lauf_state`
hat `geraet_id` und wird ueber `(kanal, geraet_id)` gematcht -- robust, auch
wenn ein Lauf mit unvollstaendiger Zonenliste gestartet wurde.
`pre_soak_state` hat KEINE `geraet_id` (s. Schema in `speicher.py`); ein
Match auf den blossen Kanal wuerde bei zwei DSWCs mit gleicher Kanalnummer
falsch anschlagen. Deshalb dort Zugehoerigkeit ueber `zone_ids_kanal`, was
den physischen Kanal ueber seine Zonen eindeutig identifiziert.

**Frische-Schranke** (T-0443, Entscheidung Andre 28.07.): beide Tabellen
koennen haengenbleiben (Realfall 02.07.: `live_lauf_state` hing 12 h nach
einem fehlgeschlagenen Close). Eine Geister-Zeile wuerde die Notpfade, die
diesen Check konsultieren, dauerhaft stumm schalten. Eine Zeile gilt deshalb
nur bis zu ihrem planmaessigen Ende plus Puffer; darueber wird sie ignoriert
und gemeldet. Der Ausfall ist damit auf die echte Vorgangslaenge begrenzt
statt unbegrenzt.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import structlog

logger = structlog.get_logger()

# Puffer ueber das planmaessige Ende hinaus, bevor eine Zustandszeile als
# Geist gilt. Deckt Cloud-Latenz und einen Loop-Tick (~300 s) ab.
TOLERANZ = timedelta(minutes=15)

# T-0537: so lange gilt ein OEFFNEN ohne SCHLIESSEN als "laeuft noch".
# Ohne die Schranke wuerde ein einziges verlorenes SCHLIESSEN den Kanal
# dauerhaft als belegt fuehren -- und fuer Fremdlaeufe (`ausloser='ignoriert'`)
# greift der Orphan-Close-Job bewusst NICHT, dort gibt es also keine zweite
# Heilung. 150 min = laengste konfigurierte `max_dauer_sekunden` (5400 s) plus
# 60 min Puffer fuer Cloud-Latenz und App-Laeufe ohne Zonen-Obergrenze.
# Der `HahnArbiter` importiert die Konstante von hier -- eine Wahrheit.
MAX_OFFEN = timedelta(minutes=150)

# Phasen, in denen eine Pre-Soak-Sequenz beendet ist. Single Source of Truth
# -- `pre_soak.py` importiert sie von hier.
PRE_SOAK_END_PHASEN = ("fertig", "fehler", "stop_fehler")


def pre_soak_puls_s(haupt_s: int, haupt_pulse: int) -> int:
    """Dauer EINES Haupt-Pulses. Die Gesamtmenge bleibt `haupt_s`."""
    return max(1, int(haupt_s) // max(1, int(haupt_pulse)))


def pre_soak_ende_s(
    pause_s: int, haupt_s: int, haupt_pulse: int = 1, haupt_pause_s: int = 0,
) -> int:
    """Offset ab `gestartet_am`, ab dem die gesamte Sequenz vorbei ist.

    `pause_s` ist die Gesamt-Pause vom Pre-Soak-START bis zum Haupt-START,
    enthaelt den Vorbenetzungs-Puls also bereits (s. `PreSoakManager.starte`).
    """
    puls_s = pre_soak_puls_s(haupt_s, haupt_pulse)
    n = max(1, int(haupt_pulse))
    return int(pause_s) + (n - 1) * (puls_s + int(haupt_pause_s)) + puls_s


def _naiv(wert: datetime) -> datetime:
    """Berlin-naive Vergleichsbasis. Die DB speichert naiv; ein aware
    `jetzt` wuerde den Vergleich mit einem TypeError sprengen, der oben in
    einem `except` als "kein Vorgang" verschwinden wuerde -- also fail-open
    an genau der Stelle, die schuetzen soll."""
    return wert.replace(tzinfo=None) if wert.tzinfo is not None else wert


def _frisch(
    gestartet: object, dauer_s: int, jetzt: datetime, kontext: dict,
) -> bool:
    """True wenn die Zeile ihr planmaessiges Ende + TOLERANZ noch nicht
    ueberschritten hat. Ohne verwertbaren Startzeitpunkt: True -- dann ist
    die Existenz der Zeile die einzige Aussage, die wir haben."""
    if not isinstance(gestartet, datetime):
        return True
    ende = _naiv(gestartet) + timedelta(seconds=max(int(dauer_s or 0), 0))
    if jetzt <= ende + TOLERANZ:
        return True
    logger.warning(
        "kanal_zustand.zeile_veraltet",
        gestartet_am=_naiv(gestartet).isoformat(),
        geplantes_ende=ende.isoformat(),
        jetzt=jetzt.isoformat(),
        **kontext,
    )
    return False


async def kanal_vorgang_laeuft(
    speicher,
    *,
    zone_id: str,
    kanal: int | None,
    geraet_id: str | None = None,
    jetzt: datetime | None = None,
) -> bool:
    """True wenn auf dem Kanal der Zone gerade ein Bewaesserungs-Vorgang
    laeuft -- offenes Ventil ODER laufende Pre-Soak-Sequenz (auch in deren
    Soak-Pause).

    Fail-open (False) bei fehlendem Kanal, Speicher ohne die Accessoren
    (alte Mocks) oder DB-Fehler: ein Zustands-Check, der selbst ausfaellt,
    darf nicht die halbe Anlage blockieren. Der Aufrufer hat in jedem Fall
    noch seinen jeweiligen Zweit-Guard (Event-Fenster bzw. Dedup).
    """
    if kanal is None:
        return False
    eff_jetzt = _naiv(jetzt or datetime.now())

    if await _ventil_offen(speicher, zone_id, kanal, geraet_id, eff_jetzt):
        return True
    if await _ventil_offen_laut_ereignis(
        speicher, zone_id, kanal, geraet_id, eff_jetzt,
    ):
        return True
    return await _pre_soak_laeuft(speicher, zone_id, eff_jetzt)


async def _ventil_offen(
    speicher, zone_id: str, kanal: int, geraet_id: str | None,
    jetzt: datetime,
) -> bool:
    hole = getattr(speicher, "hole_live_lauf_states", None)
    if hole is None:
        return False
    try:
        states = await hole()
    except Exception:
        logger.exception("kanal_zustand.live_lauf_states_fehler", zone_id=zone_id)
        return False
    for s in states:
        if s.get("kanal") != kanal:
            continue
        if geraet_id is not None and s.get("geraet_id") != geraet_id:
            continue
        if _frisch(
            s.get("gestartet_am"), s.get("dauer_sekunden"), jetzt,
            {"quelle": "live_lauf_state", "zone_id": zone_id, "kanal": kanal,
             "geraet_id": s.get("geraet_id")},
        ):
            return True
    return False


async def _ventil_offen_laut_ereignis(
    speicher, zone_id: str, kanal: int, geraet_id: str | None,
    jetzt: datetime,
) -> bool:
    """T-0537: dritte Quelle -- offenes OEFFNEN in `ventil_ereignis`.

    `live_lauf_state` kennt nur Laeufe, die DIESE Engine gestartet hat. Der
    taegliche Magerwiese-Lauf kommt aus der Gardena-App und steht dort nie;
    fuer beide Konsumenten sah der Kanal damit frei aus, obwohl Wasser lief.
    Gleiche Fehlerklasse wie der geraetelokale Hahn-Lock aus T-0536, nur eine
    Ebene tiefer.

    **Kanaele ohne `:<nummer>`-Suffix bleiben aussen vor.** Die Ereignisse
    tragen dort die blosse Geraete-UUID (einkanaliges WaterControl) und sind
    keinem Kanal zuzuordnen; in dieser Anlage haengt jedes Ventil an einem
    Dual Water Control und traegt den Suffix.
    """
    hole = getattr(speicher, "hole_offene_ventil_kanaele", None)
    if hole is None:
        return False
    try:
        offene = await hole(seit=jetzt - MAX_OFFEN, bis=jetzt)
    except Exception:
        logger.exception(
            "kanal_zustand.offene_kanaele_fehler", zone_id=zone_id,
        )
        return False
    for eintrag in offene:
        ventil_id = str(eintrag.get("ventil_id") or "")
        e_geraet, trenner, e_kanal = ventil_id.rpartition(":")
        if not trenner or not e_kanal.isdigit():
            continue
        if int(e_kanal) != kanal:
            continue
        # Ohne bekannte Geraete-ID wie im `live_lauf_state`-Pfad: der blosse
        # Kanal entscheidet. Bei zwei DSWCs mit gleicher Kanalnummer ist das
        # unscharf, aber in die sichere Richtung (eher belegt als frei).
        if geraet_id is not None and e_geraet != geraet_id:
            continue
        logger.info(
            "kanal_zustand.fremdlauf_erkannt",
            zone_id=zone_id, kanal=kanal, ventil_id=ventil_id,
            zonen=list(eintrag.get("zone_ids") or ()),
            ausloser=eintrag.get("ausloser"),
        )
        return True
    return False


async def _pre_soak_laeuft(speicher, zone_id: str, jetzt: datetime) -> bool:
    for s in await _laufende_pre_soak_states(speicher, jetzt):
        # Zugehoerigkeit ueber die Zonen des Kanals, nicht ueber die
        # Kanalnummer -- die Tabelle hat keine `geraet_id`, s. Modul-Kopf.
        zonen = s.get("zone_ids_kanal") or []
        if zone_id == s.get("zone_id") or zone_id in zonen:
            return True
    return False


async def _laufende_pre_soak_states(speicher, jetzt: datetime) -> list[dict]:
    """Alle Pre-Soak-Zeilen, die weder beendet noch veraltet sind."""
    hole = getattr(speicher, "hole_pre_soak_states", None)
    if hole is None:
        return []
    try:
        states = await hole()
    except Exception:
        logger.exception("kanal_zustand.pre_soak_states_fehler")
        return []
    laufend: list[dict] = []
    for s in states:
        if s.get("phase") in PRE_SOAK_END_PHASEN:
            continue
        ende_s = pre_soak_ende_s(
            s.get("pause_s") or 0, s.get("haupt_s") or 0,
            s.get("haupt_pulse") or 1, s.get("haupt_pause_s") or 0,
        )
        if _frisch(
            s.get("gestartet_am"), ende_s, jetzt,
            {"quelle": "pre_soak_state", "lauf_zone_id": s.get("zone_id"),
             "phase": s.get("phase")},
        ):
            laufend.append(s)
    return laufend


async def laufende_pre_soak_zonen(
    speicher, jetzt: datetime | None = None,
) -> set[str]:
    """T-0536: alle Zonen, deren Pre-Soak-Sequenz gerade laeuft.

    Enthaelt die Startzone UND ihre Geschwister am selben Kanal, weil die
    Sequenz den physischen Kanal belegt, nicht die Zone. Gleiche Frische- und
    Phasen-Regeln wie `kanal_vorgang_laeuft` -- deshalb hier und nicht im
    Arbiter (eine Wahrheit, s. Modul-Kopf).
    """
    eff_jetzt = _naiv(jetzt or datetime.now())
    zonen: set[str] = set()
    for s in await _laufende_pre_soak_states(speicher, eff_jetzt):
        if s.get("zone_id"):
            zonen.add(str(s["zone_id"]))
        zonen.update(str(z) for z in (s.get("zone_ids_kanal") or []))
    return zonen
