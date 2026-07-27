"""Tests fuer Pre-Soak als loop-getriebene State-Machine (T-0111/T-0116/T-0336).

Strategie: VentilSicherung gemockt. Die Sequenz wird durch explizite
`tick(jetzt)`-Aufrufe mit kontrollierten Wall-Clock-Werten getrieben (kein
asyncio.sleep, kein Hintergrund-Task mehr). Das erlaubt deterministische Tests
der Phasen-Uebergaenge, der Idempotenz und der Sleep/Wake-Robustheit.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import Ausloser
from bewaesserung.pre_soak import PreSoakManager


class VentilSicherungAttrappe:
    """Minimaler VentilSicherung-Ersatz fuer Pre-Soak-Tests."""

    def __init__(
        self,
        bewaessere_erfolg: bool = True,
        bewaessere_zweiter_call_fehler: bool = False,
        stoppe_erfolg: bool = True,
        ist_aktiv: bool = False,
    ) -> None:
        self.bewaessere_aufrufe: list[tuple[int, list[str], int, Ausloser]] = []
        # T-0335: Pre-Soak-Marker (lauf_gruppe, phase) pro bewaessere()-Aufruf.
        self.bewaessere_marker: list[tuple[str | None, str | None]] = []
        self.stoppe_aufrufe: list[tuple[int, Ausloser]] = []
        self._bewaessere_erfolg = bewaessere_erfolg
        self._zweiter_call_fehler = bewaessere_zweiter_call_fehler
        self._stoppe_erfolg = stoppe_erfolg
        self._ist_aktiv = ist_aktiv

    async def bewaessere(
        self, kanal: int, zone_ids: list[str],
        dauer_s: int, ausloser: Ausloser,
        *, lauf_gruppe: str | None = None, phase: str | None = None,
    ) -> bool:
        self.bewaessere_aufrufe.append((kanal, list(zone_ids), dauer_s, ausloser))
        self.bewaessere_marker.append((lauf_gruppe, phase))
        if self._zweiter_call_fehler and len(self.bewaessere_aufrufe) >= 2:
            return False
        return self._bewaessere_erfolg

    async def stoppe(self, kanal: int, ausloser: Ausloser) -> bool:
        self.stoppe_aufrufe.append((kanal, ausloser))
        return self._stoppe_erfolg

    def ist_aktiv(self, kanal: int) -> bool:
        return self._ist_aktiv


# --- Validierung + Grundfluss ---

@pytest.mark.asyncio
async def test_starte_validiert_dauern():
    """Negative Dauern oder pause < pre_soak werden abgelehnt."""
    mgr = PreSoakManager(VentilSicherungAttrappe())  # type: ignore[arg-type]

    ok, fehler = await mgr.starte(
        zone_id="bambuswald", kanal=1, zone_ids_kanal=["bambuswald"],
        pre_soak_min=0, pause_min=30, haupt_min=60,
    )
    assert not ok and fehler and "> 0" in fehler

    ok, fehler = await mgr.starte(
        zone_id="bambuswald", kanal=1, zone_ids_kanal=["bambuswald"],
        pre_soak_min=10, pause_min=5, haupt_min=60,
    )
    assert not ok and fehler and "pause_min" in fehler


@pytest.mark.asyncio
async def test_starte_tickt_puls_sofort_dann_pause_dann_haupt():
    """starte() startet den Puls sofort; tick treibt Pause -> Hauptdose."""
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]

    ok, _ = await mgr.starte(
        zone_id="bambuswald", kanal=1,
        zone_ids_kanal=["bambuswald", "bambuswald_yogaraum"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    assert ok
    lauf = mgr.laufender_lauf("bambuswald")
    assert lauf is not None
    t0 = lauf.gestartet_am

    # Puls sofort gefeuert (genau 1 Aufruf, pre_soak_s).
    assert len(sicherung.bewaessere_aufrufe) == 1
    puls = sicherung.bewaessere_aufrufe[0]
    assert puls[0] == 1 and puls[1] == ["bambuswald", "bambuswald_yogaraum"]
    assert puls[2] == 5 * 60 and puls[3] == Ausloser.MANUELL
    assert lauf.phase == "pre_soak"

    # tick in der Pause-Phase (10 min) -> kein neuer Aufruf, Phase pause.
    await mgr.tick(t0 + timedelta(minutes=10))
    assert len(sicherung.bewaessere_aufrufe) == 1
    assert lauf.phase == "pause"

    # tick nach Pausenende (35 min) -> Hauptdose feuert.
    await mgr.tick(t0 + timedelta(minutes=35))
    assert len(sicherung.bewaessere_aufrufe) == 2
    assert sicherung.bewaessere_aufrufe[1][2] == 60 * 60
    assert lauf.phase == "haupt"

    # tick nach Sequenz-Ende (95 min) -> fertig.
    await mgr.tick(t0 + timedelta(minutes=95))
    assert lauf.phase == "fertig"


@pytest.mark.asyncio
async def test_lauf_gruppe_und_phasen_marker():
    """T-0335: Puls + Haupt teilen EINE lauf_gruppe, Phasen pre_soak/haupt."""
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]

    await mgr.starte(
        zone_id="bambuswald", kanal=1, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    lauf = mgr.laufender_lauf("bambuswald")
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=35))

    assert len(sicherung.bewaessere_marker) == 2
    (puls_g, puls_p), (haupt_g, haupt_p) = sicherung.bewaessere_marker
    assert puls_p == "pre_soak" and haupt_p == "haupt"
    assert puls_g and puls_g == haupt_g
    assert puls_g.startswith("presoak_bambuswald_")


@pytest.mark.asyncio
async def test_auto_pre_soak_schreibt_automatik_ausloser():
    """T-0343 (Regression): Ein Auto-Loop-Pre-Soak (ausloser=AUTOMATIK) schreibt
    ALLE Ventil-Events mit AUTOMATIK -- nicht faelschlich MANUELL (der Bug:
    pre_soak.py hatte Ausloser.MANUELL hartcodiert, Auto-Pre-Soaks erschienen
    als 'manuell' in der Giess-Historie)."""
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    await mgr.starte(
        zone_id="hecke", kanal=2, zone_ids_kanal=["hecke"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
        ausloser=Ausloser.AUTOMATIK,
    )
    lauf = mgr.laufender_lauf("hecke")
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=35))
    ausloeser = [a[3] for a in sicherung.bewaessere_aufrufe]
    assert ausloeser == [Ausloser.AUTOMATIK, Ausloser.AUTOMATIK]


@pytest.mark.asyncio
async def test_manueller_pre_soak_default_bleibt_manuell():
    """Default-Pfad (Dashboard, kein ausloser-Arg) bleibt MANUELL."""
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    await mgr.starte(
        zone_id="bambuswald", kanal=2, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    assert sicherung.bewaessere_aufrufe[0][3] == Ausloser.MANUELL


@pytest.mark.asyncio
async def test_recover_behaelt_ausloser_automatik(tmp_path):
    """T-0343: Auto-Pre-Soak wird nach Restart als AUTOMATIK fortgesetzt
    (ausloser in pre_soak_state persistiert + recovered)."""
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "ps.db"))
    await sp.verbinden()
    try:
        gestartet = datetime.now() - timedelta(minutes=10)
        await sp.setze_pre_soak_state(
            zone_id="hecke", kanal=2, zone_ids_kanal=["hecke"],
            pre_soak_s=300, pause_s=1800, haupt_s=3600,
            gestartet_am=gestartet, phase="pause", ausloser="automatik",
        )
        sicherung = VentilSicherungAttrappe()
        mgr = PreSoakManager(sicherung, sp)  # type: ignore[arg-type]
        await mgr.recover_aus_db()
        await mgr.tick(gestartet + timedelta(minutes=35))
        assert any(
            a[3] == Ausloser.AUTOMATIK and a[2] == 3600
            for a in sicherung.bewaessere_aufrufe
        ), "Hauptdose nach Recovery muss AUTOMATIK sein"
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_sleep_ueber_pause_zieht_hauptdose_nach():
    """KERN (T-0336): Ein grosser Wall-Clock-Sprung ueber die Pause hinweg
    (= Laptop-Sleep) -> der naechste tick zieht die Hauptdose nach. Mit dem
    alten asyncio.sleep waere sie evtl. nie gefeuert."""
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]

    await mgr.starte(
        zone_id="magerwiese", kanal=1, zone_ids_kanal=["magerwiese"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    lauf = mgr.laufender_lauf("magerwiese")
    assert len(sicherung.bewaessere_aufrufe) == 1  # Puls

    # "Laptop schlief 2h, Backend lief weiter" -> erster tick nach Wake
    # springt von ~0 auf +120 min (mitten in der Hauptdose-Phase).
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=40))
    assert len(sicherung.bewaessere_aufrufe) == 2, "Hauptdose muss nachgezogen werden"
    assert sicherung.bewaessere_aufrufe[1][2] == 60 * 60
    assert lauf.phase == "haupt"


@pytest.mark.asyncio
async def test_kurze_hauptdose_wird_nicht_verschluckt():
    """T-0344 (Live-Bug): Hauptdose-Fenster kuerzer als der Loop-Tick
    (haupt_min=1 -> 60s << 300s). Der erste Tick nach Pausenende landet HINTER
    dem nominalen Fenster [pause_s, pause_s+haupt_s]. Vor dem Fix schluckte der
    fertig-Zweig die Dose (nur Puls lief); jetzt wird sie innerhalb der Toleranz
    nachgezogen."""
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]

    await mgr.starte(
        zone_id="hecke", kanal=2, zone_ids_kanal=["hecke"],
        pre_soak_min=5, pause_min=30, haupt_min=1,
    )
    lauf = mgr.laufender_lauf("hecke")
    assert len(sicherung.bewaessere_aufrufe) == 1  # Puls

    # Pause endet bei 30 min, Hauptdose-Fenster nur [30, 31] min. Erster Tick
    # nach Pause kommt 3 min "spaet" -> nominales Fenster vorbei, aber < Toleranz.
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=33))
    assert len(sicherung.bewaessere_aufrufe) == 2, "Hauptdose darf nicht verschluckt werden"
    assert sicherung.bewaessere_aufrufe[1][2] == 60  # haupt_s = 1 min
    assert sicherung.bewaessere_marker[1] == (lauf.lauf_gruppe, "haupt")
    assert lauf.phase == "haupt"


@pytest.mark.asyncio
async def test_hauptdose_weit_ueberfaellig_wird_als_verfehlt_markiert():
    """T-0344: Ist die Hauptdose um mehr als die Toleranz ueberfaellig (langer
    Laptop-Sleep), ist die Vorbenetzung verpufft -> kein echter Pre-Soak mehr.
    Dann KEINE veraltete Dose blind feuern, sondern sichtbar als fehler markieren."""
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]

    await mgr.starte(
        zone_id="hecke", kanal=2, zone_ids_kanal=["hecke"],
        pre_soak_min=5, pause_min=30, haupt_min=1,
    )
    lauf = mgr.laufender_lauf("hecke")
    assert len(sicherung.bewaessere_aufrufe) == 1  # Puls

    # Fenster [30, 31] min, Toleranz 600 s -> Grenze 41 min. +45 min > Grenze.
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=45))
    assert len(sicherung.bewaessere_aufrufe) == 1, "Keine veraltete Hauptdose feuern"
    assert lauf.phase == "fehler"
    assert lauf.fehler and "verpasst" in lauf.fehler


@pytest.mark.asyncio
async def test_tick_ist_idempotent():
    """Mehrfacher tick im selben Phasen-Fenster -> genau ein Puls, ein Haupt."""
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]

    await mgr.starte(
        zone_id="bambuswald", kanal=1, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    lauf = mgr.laufender_lauf("bambuswald")
    t0 = lauf.gestartet_am

    # Mehrfach im Puls-Fenster ticken -> kein zweiter Puls.
    for _ in range(3):
        await mgr.tick(t0 + timedelta(minutes=1))
    assert len(sicherung.bewaessere_aufrufe) == 1

    # Mehrfach im Haupt-Fenster ticken -> genau ein Haupt-Aufruf.
    for _ in range(3):
        await mgr.tick(t0 + timedelta(minutes=40))
    assert len(sicherung.bewaessere_aufrufe) == 2


@pytest.mark.asyncio
async def test_doppel_start_blockiert():
    """Solange eine Sequenz laeuft, lehnt starte() einen zweiten Lauf ab."""
    mgr = PreSoakManager(VentilSicherungAttrappe())  # type: ignore[arg-type]

    ok, _ = await mgr.starte(
        zone_id="bambuswald", kanal=1, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    assert ok
    ok2, fehler = await mgr.starte(
        zone_id="bambuswald", kanal=1, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    assert not ok2 and fehler and "laeuft bereits" in fehler


# --- stoppe ---

@pytest.mark.asyncio
async def test_stoppe_bei_offenem_ventil_ruft_ventil_stop():
    """Abbruch waehrend der Puls-Phase (Ventil offen) -> Ventil-Stop + fehler."""
    sicherung = VentilSicherungAttrappe(ist_aktiv=True)
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]

    await mgr.starte(
        zone_id="bambuswald", kanal=1, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    ok, _ = await mgr.stoppe("bambuswald")
    assert ok
    lauf = mgr.laufender_lauf("bambuswald")
    assert lauf.phase == "fehler" and lauf.fehler == "Vom User abgebrochen"
    assert sicherung.stoppe_aufrufe == [(1, Ausloser.MANUELL)]


@pytest.mark.asyncio
async def test_stoppe_in_pause_ohne_ventil_stop():
    """Abbruch in der Pause (Ventil zu) -> kein Ventil-Stop noetig, nur State."""
    sicherung = VentilSicherungAttrappe(ist_aktiv=False)
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]

    await mgr.starte(
        zone_id="bambuswald", kanal=1, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    ok, _ = await mgr.stoppe("bambuswald")
    assert ok
    assert sicherung.stoppe_aufrufe == []  # Ventil war zu -> kein Stop
    assert mgr.laufender_lauf("bambuswald").phase == "fehler"


@pytest.mark.asyncio
async def test_stoppe_ohne_laufende_sequenz_meldet_fehler():
    mgr = PreSoakManager(VentilSicherungAttrappe())  # type: ignore[arg-type]
    ok, fehler = await mgr.stoppe("bambuswald")
    assert not ok and fehler and "Keine laufende" in fehler


@pytest.mark.asyncio
async def test_stoppe_meldet_fehler_wenn_ventil_close_scheitert():
    sicherung = VentilSicherungAttrappe(stoppe_erfolg=False, ist_aktiv=True)
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    await mgr.starte(
        zone_id="bambuswald", kanal=1, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    ok, fehler = await mgr.stoppe("bambuswald")
    assert not ok and fehler and "fehlgeschlagen" in fehler
    assert mgr.laufender_lauf("bambuswald").phase == "stop_fehler"


@pytest.mark.asyncio
async def test_t0342_stop_fehler_loest_sich_auf_wenn_ventil_zu():
    """T-0342: stop_fehler (Ventil-Zustand unklar) bleibt sichtbar, solange das
    Ventil offen gemeldet wird, und loest sich auf benignes fehler auf, sobald
    die Sicherung den Kanal nicht mehr aktiv meldet (Watchdog/Cloud-Close) ->
    Warn-Karte verschwindet, ohne den offenen Zustand je zu maskieren."""
    sicherung = VentilSicherungAttrappe(stoppe_erfolg=False, ist_aktiv=True)
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    await mgr.starte(
        zone_id="bambuswald", kanal=1, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    await mgr.stoppe("bambuswald")
    assert mgr.laufender_lauf("bambuswald").phase == "stop_fehler"

    # Ventil noch offen gemeldet -> Warnung bleibt (NICHT maskieren).
    await mgr.tick()
    assert mgr.laufender_lauf("bambuswald").phase == "stop_fehler"

    # Watchdog/Cloud-Timer hat geschlossen -> Kanal nicht mehr aktiv -> aufloesen.
    sicherung._ist_aktiv = False
    await mgr.tick()
    lauf = mgr.laufender_lauf("bambuswald")
    assert lauf.phase == "fehler" and "geschlossen" in (lauf.fehler or "")


@pytest.mark.asyncio
async def test_t0346_sekunden_bis_naechster_uebergang():
    """T-0346: Der feine Pre-Soak-Ticker fragt die Sekunden bis zur naechsten
    zeit-getriebenen Phasengrenze ab, um exakt dort aufzuwachen (statt am 5-min-
    Loop). Grenzen: pre_soak_s (->pause), pause_s (->haupt), pause_s+haupt_s
    (->fertig)."""
    from datetime import timedelta

    sicherung = VentilSicherungAttrappe(ist_aktiv=False)
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]

    # Keine Laeufe -> None (Ticker faellt auf Idle-Intervall).
    assert mgr.sekunden_bis_naechster_uebergang() is None

    await mgr.starte(
        zone_id="bambuswald", kanal=1, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=30, haupt_min=60,  # 300 / 1800 / 3600 s
    )
    start = mgr.laufender_lauf("bambuswald").gestartet_am

    # Direkt nach Start: naechste Grenze = pre_soak_s = 300 s.
    assert mgr.sekunden_bis_naechster_uebergang(jetzt=start) == pytest.approx(300, abs=1)
    # In der Pause (verstrichen 600 s, zwischen 300 und 1800): naechste Grenze
    # = pause_s = 1800 -> 1200 s verbleibend.
    assert mgr.sekunden_bis_naechster_uebergang(
        jetzt=start + timedelta(seconds=600)
    ) == pytest.approx(1200, abs=1)
    # T-0437: ueberfaellige, NICHT gestartete Dose -> "sofort aufwecken" (0.5),
    # nicht "schlaf bis zur naechsten Grenze". Vorher hing dieser Zweig an
    # `phase == "pause"`; nach einem verschlafenen Tick steht die Phase aber
    # noch auf "pre_soak", und der Ticker haette hier 3400 s weitergeschlafen
    # und die Dose ~1 h zu spaet gestartet. Jetzt entscheidet der Puls-Zaehler.
    assert mgr.sekunden_bis_naechster_uebergang(
        jetzt=start + timedelta(seconds=2000)
    ) == pytest.approx(0.5, abs=0.01)

    # Regulaerer Verlauf: Hauptdose per Tick gestartet -> naechste Grenze ist
    # das Dosen-Ende. Das ist die urspruengliche T-0346-Zusicherung, jetzt mit
    # dem Zustand, den sie beschreibt (statt mit einer nie gelaufenen Dose).
    await mgr.tick(start + timedelta(seconds=1800))
    assert mgr.laufender_lauf("bambuswald").haupt_pulse_gestartet == 1
    # verstrichen 2000, Grenze pause_s + haupt_s = 5400 -> 3400 s verbleibend.
    assert mgr.sekunden_bis_naechster_uebergang(
        jetzt=start + timedelta(seconds=2000)
    ) == pytest.approx(3400, abs=1)
    # Nach allen Grenzen (verstrichen 6000 s > 5400): keine zukuenftige Grenze
    # mehr -> None (Ticker idled, der naechste Tick zieht auf fertig nach).
    assert mgr.sekunden_bis_naechster_uebergang(
        jetzt=start + timedelta(seconds=6000)
    ) is None

    # Endphase -> None (kein Aufwecken noetig).
    mgr.laufender_lauf("bambuswald").phase = "fertig"
    assert mgr.sekunden_bis_naechster_uebergang(jetzt=start) is None


# --- Fehlerpfade ---

@pytest.mark.asyncio
async def test_pre_soak_fehler_setzt_phase_und_bricht_ab():
    """bewaessere() im Puls False -> Lauf auf fehler, keine Hauptdose."""
    sicherung = VentilSicherungAttrappe(bewaessere_erfolg=False)
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]

    ok, _ = await mgr.starte(
        zone_id="bambuswald", kanal=1, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    assert not ok  # starte meldet den Puls-Fehler durch
    lauf = mgr.laufender_lauf("bambuswald")
    assert lauf.phase == "fehler" and lauf.fehler and "Pre-Soak" in lauf.fehler
    assert len(sicherung.bewaessere_aufrufe) == 1


@pytest.mark.asyncio
async def test_haupt_fehler_retryt_und_startet_spaeter():
    """T-0363: Hauptdose-Fehler nach Puls retryt innerhalb der Toleranz."""
    sicherung = VentilSicherungAttrappe(bewaessere_zweiter_call_fehler=True)
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]

    await mgr.starte(
        zone_id="bambuswald", kanal=1, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    lauf = mgr.laufender_lauf("bambuswald")
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=35))
    assert lauf.phase == "pause"
    assert lauf.fehler and "Retry" in lauf.fehler
    assert len(sicherung.bewaessere_aufrufe) == 2

    sicherung._zweiter_call_fehler = False
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=36))
    assert lauf.phase == "haupt"
    assert len(sicherung.bewaessere_aufrufe) == 3


@pytest.mark.asyncio
async def test_haupt_fehler_nach_toleranz_setzt_phase_fehler():
    """T-0363: Ist das Hauptfenster vorbei, endet der Lauf sichtbar als Fehler."""
    sicherung = VentilSicherungAttrappe(bewaessere_zweiter_call_fehler=True)
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]

    await mgr.starte(
        zone_id="bambuswald", kanal=1, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=30, haupt_min=1,
    )
    lauf = mgr.laufender_lauf("bambuswald")
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=35))
    assert lauf.phase == "pause"

    await mgr.tick(lauf.gestartet_am + timedelta(minutes=42))
    assert lauf.phase == "fehler"
    assert lauf.fehler and "Fenster verpasst" in lauf.fehler


@pytest.mark.asyncio
async def test_status_dict_serialisiert_phase_und_dauern():
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    await mgr.starte(
        zone_id="bambuswald", kanal=2, zone_ids_kanal=["bambuswald"],
        pre_soak_min=3, pause_min=10, haupt_min=45,
    )
    daten = mgr.laufender_lauf("bambuswald").status_dict()
    assert daten["zone_id"] == "bambuswald" and daten["kanal"] == 2
    assert daten["pre_soak_s"] == 180 and daten["pause_s"] == 600
    assert daten["haupt_s"] == 2700 and daten["phase"] == "pre_soak"
    assert daten["fehler"] is None and isinstance(daten["gestartet_am"], str)


# --- Recovery aus DB-State (T-0116) ---

@pytest.mark.asyncio
async def test_recover_in_pause_dann_tick_startet_hauptdose(tmp_path):
    """Recovery in Pause-Phase: kein sofortiger Haupt-Call (noch nicht faellig);
    der naechste tick nach Pausenende startet die Hauptdose."""
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "ps.db"))
    await sp.verbinden()
    try:
        gestartet = datetime.now() - timedelta(minutes=10)
        await sp.setze_pre_soak_state(
            zone_id="bambuswald", kanal=2, zone_ids_kanal=["bambuswald"],
            pre_soak_s=300, pause_s=1800, haupt_s=3600,
            gestartet_am=gestartet, phase="pause",
        )
        sicherung = VentilSicherungAttrappe()
        mgr = PreSoakManager(sicherung, sp)  # type: ignore[arg-type]
        n = await mgr.recover_aus_db()
        assert n == 1
        lauf = mgr.laufender_lauf("bambuswald")
        assert lauf is not None and lauf.phase == "pause"
        assert sicherung.bewaessere_aufrufe == []  # in Pause noch nichts

        # Nach Pausenende ticken -> Hauptdose.
        await mgr.tick(gestartet + timedelta(minutes=35))
        assert any(a[0] == 2 and a[2] == 3600 for a in sicherung.bewaessere_aufrufe)
        assert lauf.phase == "haupt"
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_recover_in_haupt_ruft_kein_neues_open(tmp_path):
    """Recovery mitten in der Hauptdose: kein erneuter bewaessere()-Call
    (Cloud-Override laeuft schon, idempotent ueber Phasen-Rang)."""
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "ps.db"))
    await sp.verbinden()
    try:
        gestartet = datetime.now() - timedelta(minutes=50)
        await sp.setze_pre_soak_state(
            zone_id="bambuswald", kanal=2, zone_ids_kanal=["bambuswald"],
            pre_soak_s=300, pause_s=1800, haupt_s=3600,
            gestartet_am=gestartet, phase="haupt",
        )
        sicherung = VentilSicherungAttrappe()
        mgr = PreSoakManager(sicherung, sp)  # type: ignore[arg-type]
        n = await mgr.recover_aus_db()
        assert n == 1
        assert sicherung.bewaessere_aufrufe == [], (
            "Recovery in Haupt-Phase darf kein neues Cloud-Override aufsetzen"
        )
        assert mgr.laufender_lauf("bambuswald").phase == "haupt"
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_recover_skip_abgelaufen_loescht_state(tmp_path):
    """Sequenz-Ende liegt in der Vergangenheit -> State weg, kein Lauf."""
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "ps.db"))
    await sp.verbinden()
    try:
        gestartet = datetime.now() - timedelta(hours=3)
        await sp.setze_pre_soak_state(
            zone_id="bambuswald", kanal=2, zone_ids_kanal=["bambuswald"],
            pre_soak_s=300, pause_s=1800, haupt_s=3600,
            gestartet_am=gestartet, phase="haupt",
        )
        sicherung = VentilSicherungAttrappe()
        mgr = PreSoakManager(sicherung, sp)  # type: ignore[arg-type]
        n = await mgr.recover_aus_db()
        assert n == 0
        assert await sp.hole_pre_soak_states() == []
        assert mgr.laufender_lauf("bambuswald") is None
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_recover_zieht_verschluckte_hauptdose_nach(tmp_path):
    """T-0344 (isomorph zum _tick_lauf-Fix): Restart, waehrend ein Lauf mit
    kurzem Haupt-Fenster (haupt_s < Tick) noch nie die Hauptdose fuhr. Frueher
    verwarf recover ihn als 'abgelaufen' (verstrichen >= pause_s+haupt_s) -> die
    Hauptdose ging verloren. Jetzt zieht der Recovery-Tick sie nach."""
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "ps.db"))
    await sp.verbinden()
    try:
        # Puls lief (phase=pause), Pause 30 min, Haupt nur 1 min. Restart 33 min
        # nach Start: nominales Ende (31 min) knapp ueberschritten, < Toleranz.
        gestartet = datetime.now() - timedelta(minutes=33)
        await sp.setze_pre_soak_state(
            zone_id="hecke", kanal=2, zone_ids_kanal=["hecke"],
            pre_soak_s=300, pause_s=1800, haupt_s=60,
            gestartet_am=gestartet, phase="pause",
        )
        sicherung = VentilSicherungAttrappe()
        mgr = PreSoakManager(sicherung, sp)  # type: ignore[arg-type]
        n = await mgr.recover_aus_db()
        assert n == 1, "Lauf mit nachziehbarer Hauptdose wird wiederhergestellt"
        assert any(a[0] == 2 and a[2] == 60 for a in sicherung.bewaessere_aufrufe), \
            "Hauptdose muss beim Recovery nachgezogen werden"
        assert mgr.laufender_lauf("hecke").phase == "haupt"
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_recover_verfehlte_hauptdose_kein_zombie(tmp_path):
    """T-0344: Restart so spaet, dass die Hauptdose nicht mehr nachziehbar ist
    (Vorbenetzung verpufft). Keine veraltete Dose, kein Zombie in laufender_lauf,
    State weg."""
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "ps.db"))
    await sp.verbinden()
    try:
        # haupt_s=60, pause_s=1800, Toleranz 600 -> Nachzug-Grenze 41 min. +50 min.
        gestartet = datetime.now() - timedelta(minutes=50)
        await sp.setze_pre_soak_state(
            zone_id="hecke", kanal=2, zone_ids_kanal=["hecke"],
            pre_soak_s=300, pause_s=1800, haupt_s=60,
            gestartet_am=gestartet, phase="pause",
        )
        sicherung = VentilSicherungAttrappe()
        mgr = PreSoakManager(sicherung, sp)  # type: ignore[arg-type]
        n = await mgr.recover_aus_db()
        assert n == 0
        assert sicherung.bewaessere_aufrufe == [], "keine veraltete Hauptdose"
        assert mgr.laufender_lauf("hecke") is None, "kein Zombie-Lauf"
        assert await sp.hole_pre_soak_states() == []
    finally:
        await sp.schliessen()


@pytest.mark.asyncio
async def test_persist_und_loesch_e2e(tmp_path):
    """starte() persistiert; tick bis fertig loescht den State."""
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "ps.db"))
    await sp.verbinden()
    try:
        sicherung = VentilSicherungAttrappe()
        mgr = PreSoakManager(sicherung, sp)  # type: ignore[arg-type]
        await mgr.starte(
            "bambuswald", kanal=2, zone_ids_kanal=["bambuswald"],
            pre_soak_min=5, pause_min=30, haupt_min=60,
        )
        states = await sp.hole_pre_soak_states()
        assert len(states) == 1 and states[0]["zone_id"] == "bambuswald"

        lauf = mgr.laufender_lauf("bambuswald")
        # Erst in die Hauptdose ticken (Pause -> Haupt), dann nach Hauptdose-Ende
        # -> fertig + State weg. Ein direkter Sprung Puls->Ende wuerde die nie
        # gelaufene Hauptdose als verpasst markieren (T-0344), nicht still fertig.
        await mgr.tick(lauf.gestartet_am + timedelta(minutes=35))
        assert lauf.phase == "haupt"
        await mgr.tick(lauf.gestartet_am + timedelta(minutes=95))
        assert lauf.phase == "fertig"
        assert await sp.hole_pre_soak_states() == []
    finally:
        await sp.schliessen()


# --- T-0411: Kanal-geteiltes Ventil vs. zone-gekeyter State ---
#
# Fehlerklasse: `_laeufe` ist `zone_id`-gekeyt, das Ventil haengt aber am
# KANAL. bambuswald und bambuswald_yogaraum teilen (DSWC 1, Kanal 2) -- ein
# Pre-Soak der einen Zone giesst die andere mit. Vorher meldete die Abfrage
# fuer die Geschwister-Zone "nichts laeuft" (UI zeigte "Im Korridor", waehrend
# Wasser lief) und ein zweiter Start dort kam ungehindert durch.
#
# Besonders heikel ist die PAUSE-Phase: da ist das Ventil ZU, also greift auch
# der Belegt-Guard der VentilSicherung nicht.

KANAL_ZONEN = ["bambuswald", "bambuswald_yogaraum"]


@pytest.mark.asyncio
async def test_geschwister_zone_sieht_lauf_des_geteilten_kanals():
    """Abfrage fuer die NICHT startende Zone am selben Kanal findet den Lauf."""
    mgr = PreSoakManager(VentilSicherungAttrappe())  # type: ignore[arg-type]
    ok, _ = await mgr.starte(
        zone_id="bambuswald", kanal=2, zone_ids_kanal=KANAL_ZONEN,
        pre_soak_min=5, pause_min=30, haupt_min=90,
    )
    assert ok

    # Alt (zone-gekeyt): findet nichts -- genau der Bug.
    assert mgr.laufender_lauf("bambuswald_yogaraum") is None
    # Neu (kanal-bewusst): findet den Lauf der Nachbarzone.
    fremd = mgr.lauf_fuer_kanal_der_zone("bambuswald_yogaraum")
    assert fremd is not None
    assert fremd.zone_id == "bambuswald"     # als fremd erkennbar
    assert fremd.kanal == 2
    # Die startende Zone bekommt weiterhin ihren eigenen Lauf.
    eigen = mgr.lauf_fuer_kanal_der_zone("bambuswald")
    assert eigen is not None and eigen.zone_id == "bambuswald"


@pytest.mark.asyncio
async def test_geschwister_sieht_lauf_auch_in_pause_phase():
    """Pause-Phase: Ventil ist ZU, die Sequenz laeuft aber weiter.

    Der kritische Fall -- hier greift der Belegt-Guard der VentilSicherung
    nicht, der Kanal ist trotzdem vergeben.
    """
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    ok, _ = await mgr.starte(
        zone_id="bambuswald", kanal=2, zone_ids_kanal=KANAL_ZONEN,
        pre_soak_min=5, pause_min=30, haupt_min=90,
    )
    assert ok
    lauf = mgr.laufender_lauf("bambuswald")
    assert lauf is not None

    await mgr.tick(lauf.gestartet_am + timedelta(minutes=10))
    assert lauf.phase == "pause"
    assert not sicherung.ist_aktiv(2)  # Ventil zu -> Belegt-Guard blind

    fremd = mgr.aktiver_lauf_fuer_kanal_der_zone("bambuswald_yogaraum")
    assert fremd is not None and fremd.phase == "pause"


@pytest.mark.asyncio
async def test_zweiter_pre_soak_auf_geschwister_zone_wird_blockiert():
    """Kein zweiter Lauf auf demselben Ventil-Kanal -- auch nicht ueber die
    Nachbarzone, auch nicht waehrend der Pause."""
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    ok, _ = await mgr.starte(
        zone_id="bambuswald", kanal=2, zone_ids_kanal=KANAL_ZONEN,
        pre_soak_min=5, pause_min=30, haupt_min=90,
    )
    assert ok
    lauf = mgr.laufender_lauf("bambuswald")
    assert lauf is not None
    aufrufe_vorher = len(sicherung.bewaessere_aufrufe)

    ok2, fehler = await mgr.starte(
        zone_id="bambuswald_yogaraum", kanal=2, zone_ids_kanal=KANAL_ZONEN,
        pre_soak_min=5, pause_min=30, haupt_min=90,
    )
    assert not ok2
    assert fehler and "bambuswald" in fehler  # nennt die blockierende Zone
    assert len(sicherung.bewaessere_aufrufe) == aufrufe_vorher  # kein Wasser

    # ... und ebenso in der Pause-Phase (Ventil zu).
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=10))
    assert lauf.phase == "pause"
    ok3, _ = await mgr.starte(
        zone_id="bambuswald_yogaraum", kanal=2, zone_ids_kanal=KANAL_ZONEN,
        pre_soak_min=5, pause_min=30, haupt_min=90,
    )
    assert not ok3
    assert len(sicherung.bewaessere_aufrufe) == aufrufe_vorher


@pytest.mark.asyncio
async def test_fremde_zone_ohne_gemeinsamen_kanal_bleibt_unberuehrt():
    """Gegenprobe: eine Zone an einem ANDEREN Kanal darf nicht faelschlich
    blockiert werden (sonst waere der Fix zu breit)."""
    mgr = PreSoakManager(VentilSicherungAttrappe())  # type: ignore[arg-type]
    ok, _ = await mgr.starte(
        zone_id="bambuswald", kanal=2, zone_ids_kanal=KANAL_ZONEN,
        pre_soak_min=5, pause_min=30, haupt_min=90,
    )
    assert ok

    assert mgr.lauf_fuer_kanal_der_zone("hecke") is None
    assert mgr.aktiver_lauf_fuer_kanal_der_zone("hecke") is None
    ok2, _ = await mgr.starte(
        zone_id="hecke", kanal=1, zone_ids_kanal=["hecke"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    assert ok2


@pytest.mark.asyncio
async def test_beendeter_fremd_lauf_wird_nicht_mehr_gemeldet():
    """Ein abgeschlossener Lauf der Nachbarzone darf die eigene Karte nicht
    weiter blockieren/behelligen."""
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    ok, _ = await mgr.starte(
        zone_id="bambuswald", kanal=2, zone_ids_kanal=KANAL_ZONEN,
        pre_soak_min=5, pause_min=30, haupt_min=90,
    )
    assert ok
    lauf = mgr.laufender_lauf("bambuswald")
    assert lauf is not None

    # Phasen einzeln durchtreten (die State-Machine macht pro tick einen
    # Uebergang): Puls -> Pause -> Haupt -> fertig.
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=10))
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=35))
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=125))
    assert lauf.phase == "fertig"

    assert mgr.lauf_fuer_kanal_der_zone("bambuswald_yogaraum") is None
    assert mgr.aktiver_lauf_fuer_kanal_der_zone("bambuswald_yogaraum") is None
    # Die eigene Zone sieht ihren Endzustand weiterhin (UI zeigt "fertig").
    assert mgr.lauf_fuer_kanal_der_zone("bambuswald") is not None


@pytest.mark.asyncio
async def test_status_dict_liefert_ausloser_und_hauptdosis():
    """T-0431/T-0432: der Status muss Ausloeser UND committete Hauptdauer
    tragen.

    Realfall 23.07.2026: die Zonen-Karte beschriftete einen von der
    AUTOMATIK gestarteten Pre-Soak auf `hecke` als "Manuell aktiv" -- sie
    konnte den Ausloeser nicht kennen, weil `status_dict()` ihn nicht
    ausgab, obwohl er in `pre_soak_state.ausloser` steht (T-0343).
    Zeitgleich zeigte die Karte eine LIVE neu gerechnete Dauer neben dem
    Lauf; das sah aus, als haette die Automatik die Dosis verkuerzt.
    Beide Fehler brauchen dieselben zwei Felder im Status.
    """
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    await mgr.starte(
        zone_id="hecke", kanal=2, zone_ids_kanal=["hecke"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
        ausloser=Ausloser.AUTOMATIK,
    )
    status = mgr.laufender_lauf("hecke").status_dict()
    assert status["ausloser"] == "automatik"
    # Die committete Hauptdauer, gegen die die UI die Neuberechnung
    # abgrenzen muss.
    assert status["haupt_s"] == 60 * 60


@pytest.mark.asyncio
async def test_status_dict_manuell_bleibt_manuell():
    """T-0431 Gegenprobe: ein manueller Lauf darf NICHT als Automatik
    ausgewiesen werden -- sonst kippt der Fix nur die Falschaussage um."""
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    await mgr.starte(
        zone_id="bambuswald", kanal=2, zone_ids_kanal=["bambuswald"],
        pre_soak_min=5, pause_min=30, haupt_min=60,
    )
    assert mgr.laufender_lauf("bambuswald").status_dict()["ausloser"] == "manuell"


# --- T-0437: Cycle-and-Soak (Mehrfach-Puls-Hauptdose) ---------------------


async def _starte_3x30(sicherung, **kwargs):
    """Andres 25.07.-Regime: 5 min Puls, 25 min Pause, 3x30 min mit 21 min Soak."""
    mgr = PreSoakManager(sicherung, **kwargs)  # type: ignore[arg-type]
    ok, fehler = await mgr.starte(
        zone_id="waldblumenhain", kanal=1, zone_ids_kanal=["waldblumenhain"],
        pre_soak_min=5, pause_min=25, haupt_min=90,
        ausloser=Ausloser.AUTOMATIK,
        haupt_pulse=3, haupt_puls_pause_min=21,
    )
    assert ok, fehler
    return mgr, mgr.laufender_lauf("waldblumenhain")


@pytest.mark.asyncio
async def test_t0437_drei_pulse_teilen_die_dosis_auf():
    """Die Hauptdose wird AUFGETEILT, nicht vervielfacht: 90 min -> 3x30 min.

    Das ist die zentrale Zusicherung. Wuerde `haupt_pulse` die Dosis
    vervielfachen, liefe die Zone 270 statt 90 min -- dreifache Wassermenge
    auf einer scharfen Zone.
    """
    sicherung = VentilSicherungAttrappe()
    mgr, lauf = await _starte_3x30(sicherung)
    t0 = lauf.gestartet_am

    # Vorbenetzung sofort.
    assert len(sicherung.bewaessere_aufrufe) == 1
    assert sicherung.bewaessere_aufrufe[0][2] == 5 * 60

    # Puls 1 bei 25 min.
    await mgr.tick(t0 + timedelta(minutes=25))
    assert len(sicherung.bewaessere_aufrufe) == 2
    assert sicherung.bewaessere_aufrufe[1][2] == 30 * 60
    assert lauf.phase == "haupt" and lauf.haupt_pulse_gestartet == 1

    # Soak-Pause nach Puls 1 (55 min = 25 + 30) -> kein Wasser, eigene Phase.
    await mgr.tick(t0 + timedelta(minutes=60))
    assert len(sicherung.bewaessere_aufrufe) == 2
    assert lauf.phase == "haupt_pause"

    # Puls 2 bei 25 + 30 + 21 = 76 min.
    await mgr.tick(t0 + timedelta(minutes=76))
    assert len(sicherung.bewaessere_aufrufe) == 3
    assert lauf.haupt_pulse_gestartet == 2

    # Puls 3 bei 76 + 30 + 21 = 127 min.
    await mgr.tick(t0 + timedelta(minutes=127))
    assert len(sicherung.bewaessere_aufrufe) == 4
    assert lauf.haupt_pulse_gestartet == 3

    # Gesamt-Wassermenge = Vorbenetzung + genau haupt_s.
    haupt_summe = sum(a[2] for a in sicherung.bewaessere_aufrufe[1:])
    assert haupt_summe == 90 * 60

    # Ende bei 127 + 30 = 157 min.
    await mgr.tick(t0 + timedelta(minutes=158))
    assert lauf.phase == "fertig"


@pytest.mark.asyncio
async def test_t0437_default_ein_puls_bleibt_unveraendert():
    """Isomorphie-Gegenprobe: Bestandszonen (hecke, Bambus) haben keinen
    haupt_pulse in der Config -> Default 1 -> exakt das alte Verhalten."""
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    await mgr.starte(
        zone_id="hecke", kanal=2, zone_ids_kanal=["hecke"],
        pre_soak_min=5, pause_min=25, haupt_min=60,
    )
    lauf = mgr.laufender_lauf("hecke")
    t0 = lauf.gestartet_am
    assert lauf.haupt_pulse == 1 and lauf.haupt_pause_s == 0

    await mgr.tick(t0 + timedelta(minutes=25))
    assert len(sicherung.bewaessere_aufrufe) == 2
    assert sicherung.bewaessere_aufrufe[1][2] == 60 * 60  # am Stueck
    await mgr.tick(t0 + timedelta(minutes=86))
    assert lauf.phase == "fertig"


@pytest.mark.asyncio
async def test_t0437_ticker_kennt_alle_puls_grenzen():
    """Falle 1: `sekunden_bis_naechster_uebergang` rechnete frueher nur mit
    (pre_soak_s, pause_s, pause_s+haupt_s). Ohne die Puls-Grenzen wacht der
    feine Ticker nach Puls 1 nicht mehr auf und verschlaeft Puls 2 und 3."""
    sicherung = VentilSicherungAttrappe()
    mgr, lauf = await _starte_3x30(sicherung)
    t0 = lauf.gestartet_am

    await mgr.tick(t0 + timedelta(minutes=25))       # Puls 1 laeuft
    # Bei 60 min (in der Soak-Pause) ist die naechste Grenze Puls 2 bei 76 min.
    delta = mgr.sekunden_bis_naechster_uebergang(t0 + timedelta(minutes=60))
    assert delta is not None
    assert abs(delta - 16 * 60) < 1.0


@pytest.mark.asyncio
async def test_t0437_puls_gate_stoppt_folge_pulse():
    """Falle 4: waehrend einer Soak-Pause ist das Ventil zu, der Kanal-Max-Stop
    im Loop prueft dort NICHT (er verlangt ist_aktiv). Ohne Gate wuerde Puls 2
    feuern, obwohl die Zone waehrend des Einsickerns uebersaettigt ist."""
    sicherung = VentilSicherungAttrappe()
    gate_aufrufe: list[tuple[int, list[str]]] = []

    async def gate(kanal: int, zone_ids: list[str]) -> tuple[bool, str | None]:
        gate_aufrufe.append((kanal, list(zone_ids)))
        return False, "Feuchte 70% ueber Stop-Schwelle 65%"

    mgr, lauf = await _starte_3x30(sicherung, puls_gate=gate)
    t0 = lauf.gestartet_am

    # Puls 1 laeuft OHNE Gate-Frage (der Loop hat gerade entschieden).
    await mgr.tick(t0 + timedelta(minutes=25))
    assert len(sicherung.bewaessere_aufrufe) == 2
    assert gate_aufrufe == []

    # Puls 2 wird vom Gate abgelehnt -> kein Wasser, Sequenz sauber beendet.
    await mgr.tick(t0 + timedelta(minutes=76))
    assert len(gate_aufrufe) == 1
    assert len(sicherung.bewaessere_aufrufe) == 2
    assert lauf.phase == "fertig"

    # Auch spaeter feuert nichts mehr nach.
    await mgr.tick(t0 + timedelta(minutes=127))
    assert len(sicherung.bewaessere_aufrufe) == 2


@pytest.mark.asyncio
async def test_t0437_puls_gate_fehler_giesst_nicht():
    """Ein kaputtes Gate darf nicht in ein Giessen fallen (fail-closed)."""
    sicherung = VentilSicherungAttrappe()

    async def gate(kanal: int, zone_ids: list[str]) -> tuple[bool, str | None]:
        raise RuntimeError("Sensor-Abfrage kaputt")

    mgr, lauf = await _starte_3x30(sicherung, puls_gate=gate)
    t0 = lauf.gestartet_am
    await mgr.tick(t0 + timedelta(minutes=25))
    await mgr.tick(t0 + timedelta(minutes=76))
    assert len(sicherung.bewaessere_aufrufe) == 2   # nur Vorbenetzung + Puls 1
    assert lauf.phase == "fertig"


@pytest.mark.asyncio
async def test_t0437_recover_giesst_gelaufene_pulse_nicht_erneut(tmp_path):
    """Falle 2: ohne persistierten Puls-Zaehler faengt ein Restart mitten in
    der Sequenz wieder bei Puls 1 an und giesst doppelt."""
    from bewaesserung.speicher import Speicher

    speicher = Speicher(str(tmp_path / "t0437.db"))
    await speicher.verbinden()
    try:
        sicherung = VentilSicherungAttrappe()
        mgr, lauf = await _starte_3x30(sicherung, speicher=speicher)
        t0 = lauf.gestartet_am
        await mgr.tick(t0 + timedelta(minutes=25))    # Puls 1
        await mgr.tick(t0 + timedelta(minutes=76))    # Puls 2
        assert lauf.haupt_pulse_gestartet == 2

        # Neustart: neuer Manager, gleiche DB.
        sicherung2 = VentilSicherungAttrappe()
        mgr2 = PreSoakManager(sicherung2, speicher)  # type: ignore[arg-type]
        await mgr2.recover_aus_db()
        lauf2 = mgr2.laufender_lauf("waldblumenhain")
        assert lauf2 is not None
        assert lauf2.haupt_pulse == 3
        assert lauf2.haupt_pause_s == 21 * 60
        assert lauf2.haupt_pulse_gestartet == 2
        # Puls 1 und 2 duerfen NICHT erneut laufen.
        assert sicherung2.bewaessere_aufrufe == []
    finally:
        await speicher.schliessen()


@pytest.mark.asyncio
async def test_t0437_puls_unter_einer_minute_wird_abgelehnt():
    """Aufteilung muss aufgehen: 2 min auf 3 Pulse waere unter der
    Sensor-/Hardware-Aufloesung."""
    sicherung = VentilSicherungAttrappe()
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    ok, fehler = await mgr.starte(
        zone_id="waldblumenhain", kanal=1, zone_ids_kanal=["waldblumenhain"],
        pre_soak_min=5, pause_min=25, haupt_min=2,
        haupt_pulse=3, haupt_puls_pause_min=21,
    )
    assert not ok
    assert "unter 1 min" in (fehler or "")


@pytest.mark.asyncio
async def test_t0437_altzeile_ohne_zaehler_giesst_nicht_doppelt(tmp_path):
    """SICHERHEIT: Ein `pre_soak_state` aus der Zeit VOR der T-0437-Migration
    hat phase='haupt', aber haupt_pulse_gestartet=0 (Spalten-Default).

    Die Idempotenz haengt jetzt am Zaehler statt am Phasen-Rang -- ohne
    Nachziehen wuerde der Tick Puls 1 ein zweites Mal giessen. Genau diese
    Doppelgiess-Klasse steht in CLAUDE.md ("Wird dieselbe Bewaesserung
    irgendwo zweimal gezaehlt?"). Mehrfach-Puls-Variante, weil dort zusaetzlich
    noch Puls 2 und 3 nachliefen.
    """
    from bewaesserung.speicher import Speicher

    sp = Speicher(str(tmp_path / "alt.db"))
    await sp.verbinden()
    try:
        gestartet = datetime.now() - timedelta(minutes=40)
        # Wie eine Altzeile: die neuen Spalten werden NICHT mitgegeben.
        await sp.setze_pre_soak_state(
            zone_id="waldblumenhain", kanal=1, zone_ids_kanal=["waldblumenhain"],
            pre_soak_s=300, pause_s=1500, haupt_s=5400,
            gestartet_am=gestartet, phase="haupt", ausloser="automatik",
        )
        sicherung = VentilSicherungAttrappe()
        mgr = PreSoakManager(sicherung, sp)  # type: ignore[arg-type]
        await mgr.recover_aus_db()
        lauf = mgr.laufender_lauf("waldblumenhain")
        assert lauf is not None
        assert lauf.haupt_pulse_gestartet >= 1, (
            "Phase 'haupt' muss den Zaehler nachziehen, sonst giesst Puls 1 doppelt"
        )
        assert sicherung.bewaessere_aufrufe == []
    finally:
        await sp.schliessen()
