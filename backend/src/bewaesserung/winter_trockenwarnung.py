"""T-0636: Lage der Gartenzonen waehrend eines Automatik-Sperrfensters.

Gemeinsame Datenbasis fuer den Watchdog-Trigger (Trocken- und
Ausfallmeldung) und den Wochenbericht-Abschnitt "Garten (Winterpause)":

- nur `sensor_messung` mit `quelle='gardena'`, nie FYTA;
- Zone mit `aggregat_lead_geraet`: nur dieses Geraet;
- sonst alle Gardena-Geraete der Zone; trocken nur, wenn ALLE aktiven
  Geraete trocken sind (konservativ gegen einen einzelnen Ausreisser).
  "Aktiv" = juengster Wert nicht aelter als `ausfall_stunden`; ein seit
  Monaten stummes Altgeraet blockiert die Warnung damit nicht.

Trocken heisst: mindestens 2 Werte in den letzten `mindestdauer_stunden`,
alle unter der Schwelle, UND der letzte Wert vor bzw. am Fensteranfang
ebenfalls darunter (das ganze Fenster ist abgedeckt). Frost (Wert
<= `frost_schwelle_c` in den letzten `frost_nachlauf_stunden`) wird separat
gemeldet; der Aufrufer unterdrueckt dann die Warnung (Befund 08.04.2026:
gefrorener Boden liest trocken und bleibt Stunden nach dem Auftauen unten).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from bewaesserung.modelle import SensorMessung, WinterTrockenwarnungKonfig
from bewaesserung.speicher import Speicher


@dataclass(frozen=True)
class TrockenBefund:
    trocken: bool
    frost: bool = False
    # Juengster Wert ueber die aktiven Geraete (fuer den Meldungstext).
    messung: SensorMessung | None = None
    # Juengster Frostwert im Nachlauf-Fenster (fuer das Log).
    frost_messung: SensorMessung | None = None


def zahl(wert: float) -> str:
    """Deutsche Kurzform: 12.0 -> '12', 4.5 -> '4,5'."""
    if float(wert).is_integer():
        return str(int(wert))
    return f"{wert:.1f}".replace(".", ",")


def boden_text(messung: SensorMessung) -> str:
    if messung.boden_temperatur is None:
        return "Boden unbekannt"
    return f"Boden {zahl(messung.boden_temperatur)} °C"


async def juengste_messung(
    speicher: Speicher, zone_id: str, lead: str | None, jetzt: datetime,
) -> SensorMessung | None:
    """Juengster Gardena-Wert der Zone (Lead bzw. ueber alle Geraete)."""
    liste = await speicher.letzte_gardena_messungen_pro_geraet(
        zone_id, bis=jetzt, geraet_id=lead,
    )
    return liste[0] if liste else None


async def pruefe_trocken(
    speicher: Speicher,
    zone_id: str,
    lead: str | None,
    schwelle: float,
    konfig: WinterTrockenwarnungKonfig,
    jetzt: datetime,
) -> TrockenBefund:
    fenster_start = jetzt - timedelta(hours=konfig.mindestdauer_stunden)
    frost_start = jetzt - timedelta(hours=konfig.frost_nachlauf_stunden)
    aktiv_grenze = jetzt - timedelta(hours=konfig.ausfall_stunden)

    letzte = await speicher.letzte_gardena_messungen_pro_geraet(
        zone_id, bis=jetzt, geraet_id=lead,
    )
    aktive = [m for m in letzte if m.zeitstempel >= aktiv_grenze]
    if not aktive:
        return TrockenBefund(trocken=False)
    aktive_ids = {m.geraet_id for m in aktive}

    messungen = await speicher.hole_gardena_messungen(
        zone_id, von=min(fenster_start, frost_start), bis=jetzt,
        geraet_id=lead,
    )

    for gid in aktive_ids:
        im_fenster = [
            m.boden_feuchte for m in messungen
            if m.geraet_id == gid and m.zeitstempel > fenster_start
            and m.boden_feuchte is not None
        ]
        if len(im_fenster) < 2:
            return TrockenBefund(trocken=False)
        if any(w >= schwelle for w in im_fenster):
            return TrockenBefund(trocken=False)
        referenz = await speicher.letzte_gardena_messungen_pro_geraet(
            zone_id, bis=fenster_start, geraet_id=gid,
        )
        if not referenz or referenz[0].boden_feuchte >= schwelle:
            return TrockenBefund(trocken=False)

    frost = [
        m for m in messungen
        if m.geraet_id in aktive_ids and m.zeitstempel >= frost_start
        and m.boden_temperatur is not None
        and m.boden_temperatur <= konfig.frost_schwelle_c
    ]
    return TrockenBefund(
        trocken=True,
        frost=bool(frost),
        messung=aktive[0],
        frost_messung=frost[-1] if frost else None,
    )


async def lage_zeile(
    speicher: Speicher,
    zone_id: str,
    name: str,
    lead: str | None,
    konfig: WinterTrockenwarnungKonfig,
    jetzt: datetime,
) -> str:
    """Eine Zeile fuer den Wochenbericht-Abschnitt "Garten (Winterpause)"."""
    m = await juengste_messung(speicher, zone_id, lead, jetzt)
    if m is None:
        return f"  {name}: keine Daten"
    alter_h = int((jetzt - m.zeitstempel).total_seconds() // 3600)
    if (jetzt - m.zeitstempel) > timedelta(hours=konfig.ausfall_stunden):
        return f"  {name}: kein Messwert seit {alter_h} h"
    return (
        f"  {name}: {zahl(m.boden_feuchte)} %, {boden_text(m)}, "
        f"Messwert vor {alter_h} h"
    )
