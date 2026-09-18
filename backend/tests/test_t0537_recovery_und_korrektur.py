"""T-0537: Recovery-Close mit exakter Dauer + vollstaendiges Korrektur-Werkzeug.

Der Anlass (12./13.08.2026): der Rechner schlief 17 Minuten mitten in einem
Waldblumen-Puls, die lokalen Timer froren ein, geschlossen hat der
Cloud-Override. Der Reconnect-Sync trug danach einen Ersatz-Close mit
`min(verstrichen, 3600)` = 3600 s nach statt der echten 1797 -- doppeltes
Wasser in der Bilanz, dazu `ausloser=watchdog` und ohne Lauf-Gruppe, womit
der Puls aus seiner Pre-Soak-Sequenz fiel und aus `ECHTES_KANAL_WASSER`.

Die Information war zu dem Zeitpunkt exakt bekannt (`live_lauf_state`), sie
wurde nur weggeworfen. Diese Tests halten fest, dass sie jetzt benutzt wird.
"""

from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import Ausloser, VentilAktion, VentilEreignis
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import VentilSicherung

GERAET = "bbbb0001-0000-4000-8000-000000000001"
VALVE = f"{GERAET}:1"
GRUPPE = "presoak_waldblumenhain_20260812180424"


class ClientAttrappe:
    _VENTIL_OFFEN_STATES = {"OPEN", "OPENING"}

    def __init__(self):
        self.oeffnen_aufrufe: list[tuple] = []

    async def ventil_oeffnen(self, geraet_id, dauer_sekunden, valve_id=None):
        self.oeffnen_aufrufe.append((geraet_id, dauer_sekunden, valve_id))

    async def ventil_schliessen(self, geraet_id, valve_id=None):
        pass

    def offene_valves(self) -> dict:
        return {}


async def _sicherung(sp) -> VentilSicherung:
    return VentilSicherung(
        client=ClientAttrappe(), speicher=sp,  # type: ignore[arg-type]
        ventil_geraet_id=GERAET, kanal_zu_valve_id={1: VALVE},
    )


async def _abgelaufener_state(sp, dauer_s=1797, vor_min=67):
    """Der Realfall: Lauf laengst vorbei, Close nie geschrieben."""
    gestartet = datetime.now() - timedelta(minutes=vor_min)
    await sp.setze_live_lauf_state(
        kanal=1, valve_id=VALVE, geraet_id=GERAET,
        zone_ids=["waldblumenhain"], dauer_sekunden=dauer_s,
        ausloser="automatik", gestartet_am=gestartet,
        lauf_gruppe=GRUPPE, phase="haupt",
    )
    return gestartet


# --- Recovery traegt den Close exakt nach ---------------------------------


@pytest.mark.asyncio
async def test_recovery_schreibt_close_mit_geplanter_dauer(tmp_path):
    sp = Speicher(str(tmp_path / "s.db"))
    await sp.verbinden()
    try:
        gestartet = await _abgelaufener_state(sp)
        assert await (await _sicherung(sp)).recover_aus_db() == 0

        events = await sp.hole_ventil_ereignisse("waldblumenhain")
        closes = [e for e in events if e.aktion == VentilAktion.SCHLIESSEN]
        assert len(closes) == 1
        close = closes[0]
        # Kein Schaetzwert: geplantes Ende, geplante Dauer.
        assert close.dauer_sekunden == 1797
        assert abs(
            (close.zeitstempel - (gestartet + timedelta(seconds=1797)))
            .total_seconds()
        ) < 1
        # Ausloeser aus dem State -- `watchdog` faellt aus ECHTES_KANAL_WASSER.
        assert close.ausloser == Ausloser.AUTOMATIK
        # Und die Sequenz bleibt beisammen.
        assert close.lauf_gruppe == GRUPPE
        assert close.phase == "haupt"
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_recovery_schreibt_nicht_doppelt(tmp_path):
    """Hat die WS-Pipeline den Close doch geschrieben, passiert hier nichts."""
    sp = Speicher(str(tmp_path / "s.db"))
    await sp.verbinden()
    try:
        gestartet = await _abgelaufener_state(sp)
        await sp.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=gestartet + timedelta(seconds=1790),
            zone_id="waldblumenhain", ventil_id=VALVE,
            aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1790,
            ausloser=Ausloser.AUTOMATIK,
        ))
        await (await _sicherung(sp)).recover_aus_db()

        closes = [
            e for e in await sp.hole_ventil_ereignisse("waldblumenhain")
            if e.aktion == VentilAktion.SCHLIESSEN
        ]
        assert len(closes) == 1
        assert closes[0].dauer_sekunden == 1790  # der echte, nicht der neue
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_recovery_laufender_lauf_bekommt_keinen_close(tmp_path):
    """Bestandsverhalten: ein noch laufender Vorgang wird rekonstruiert,
    nicht geschlossen."""
    sp = Speicher(str(tmp_path / "s.db"))
    await sp.verbinden()
    try:
        await sp.setze_live_lauf_state(
            kanal=1, valve_id=VALVE, geraet_id=GERAET,
            zone_ids=["waldblumenhain"], dauer_sekunden=5400,
            ausloser="automatik",
            gestartet_am=datetime.now() - timedelta(minutes=10),
        )
        sicherung = await _sicherung(sp)
        assert await sicherung.recover_aus_db() == 1
        assert sicherung.ist_aktiv(1)
        assert await sp.hole_ventil_ereignisse("waldblumenhain") == []
        sicherung._aktiv[1].timer_handle.cancel()
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_recovery_close_deckt_alle_zonen_des_kanals(tmp_path):
    """Geteilter Kanal (bambuswald + yogaraum): beide Zonen brauchen ihn."""
    sp = Speicher(str(tmp_path / "s.db"))
    await sp.verbinden()
    try:
        await sp.setze_live_lauf_state(
            kanal=1, valve_id=VALVE, geraet_id=GERAET,
            zone_ids=["bambuswald", "bambuswald_yogaraum"],
            dauer_sekunden=600, ausloser="automatik",
            gestartet_am=datetime.now() - timedelta(minutes=30),
        )
        await (await _sicherung(sp)).recover_aus_db()
        for zone in ("bambuswald", "bambuswald_yogaraum"):
            closes = [
                e for e in await sp.hole_ventil_ereignisse(zone)
                if e.aktion == VentilAktion.SCHLIESSEN
            ]
            assert len(closes) == 1, zone
            assert closes[0].dauer_sekunden == 600
    finally:
        await sp.schliessen()


# --- Korrektur-Werkzeug ---------------------------------------------------


async def _ereignis(sp, **kwargs) -> int:
    await sp.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=kwargs.get("zeitstempel", datetime(2026, 8, 12, 21, 25, 13)),
        zone_id="waldblumenhain", ventil_id=VALVE,
        aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=kwargs.get("dauer_sekunden", 3600),
        ausloser=kwargs.get("ausloser", Ausloser.WATCHDOG),
    ))
    events = await sp.hole_ventil_ereignisse("waldblumenhain")
    return events[-1].id


@pytest.mark.asyncio
async def test_korrektur_kann_lauf_gruppe_und_phase_setzen(tmp_path):
    """Ohne das musste die Reparatur vom 13.08. an der API vorbei."""
    sp = Speicher(str(tmp_path / "s.db"))
    await sp.verbinden()
    try:
        eid = await _ereignis(sp)
        assert await sp.aktualisiere_ventil_ereignis(
            eid, lauf_gruppe=GRUPPE, phase="haupt",
        )
        nachher = await sp.hole_ventil_ereignis(eid)
        assert nachher.lauf_gruppe == GRUPPE
        assert nachher.phase == "haupt"
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_korrigierte_dauer_hinterlaesst_eine_spur(tmp_path):
    """Der Kern: `ausloser_korrektur` sah eine Dauer-Aenderung nicht."""
    sp = Speicher(str(tmp_path / "s.db"))
    await sp.verbinden()
    try:
        eid = await _ereignis(sp)
        await sp.aktualisiere_ventil_ereignis(
            eid, dauer_sekunden=1797,
            jetzt=datetime(2026, 8, 13, 8, 0, 0),
        )
        roh = await _roh(sp, eid)
        assert roh["ausloser_korrektur"] is None  # Ausloeser blieb
        log = _log(roh)
        assert [(e["feld"], e["alt"], e["neu"]) for e in log] == [
            ("dauer_sekunden", 3600, 1797),
        ]
        assert log[0]["geaendert_am"] == "2026-08-13T08:00:00"
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_korrektur_log_sammelt_mehrere_laeufe_auf(tmp_path):
    """Zweite Korrektur haengt an, sie ueberschreibt die erste nicht."""
    sp = Speicher(str(tmp_path / "s.db"))
    await sp.verbinden()
    try:
        eid = await _ereignis(sp)
        await sp.aktualisiere_ventil_ereignis(eid, dauer_sekunden=1797)
        await sp.aktualisiere_ventil_ereignis(eid, ausloser=Ausloser.AUTOMATIK)
        roh = await _roh(sp, eid)
        felder = [e["feld"] for e in _log(roh)]
        assert felder == ["dauer_sekunden", "ausloser"]
        # Altfeld bleibt bedient -- der UI-Marker haengt daran.
        assert roh["ausloser_korrektur"] is not None
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_unveraenderte_werte_erzeugen_keinen_audit_eintrag(tmp_path):
    sp = Speicher(str(tmp_path / "s.db"))
    await sp.verbinden()
    try:
        eid = await _ereignis(sp)
        await sp.aktualisiere_ventil_ereignis(
            eid, dauer_sekunden=3600, ausloser=Ausloser.WATCHDOG,
        )
        roh = await _roh(sp, eid)
        assert roh["korrektur_log"] is None
    finally:
        await sp.schliessen()


async def _roh(sp, eid: int) -> dict:
    async with sp._db.execute(
        "SELECT * FROM ventil_ereignis WHERE id = ?", (eid,),
    ) as cursor:
        zeile = await cursor.fetchone()
    return dict(zeile)


def _log(roh: dict) -> list[dict]:
    import json
    return json.loads(roh["korrektur_log"])
