"""T-0536: geraeteuebergreifende Hahn-Arbitrierung.

Der Vorfall, den diese Datei absichert (12.08.2026): waldblumenhain (DSWC 1,
K1) und magerwiese (DSWC 2, K1) sind beide `exklusiv: true` im selben
`hahn_cluster` -- und liefen trotzdem parallel. Zwei unabhaengige Ursachen,
beide brauchen einen eigenen roten Test:

1. der Lock sass pro Geraet (`VentilSicherung._aktiv` kennt nur eigene Kanaele)
2. der Sperrzustand kannte nur Engine-Laeufe; die magerwiese-Laeufe kommen aus
   der Gardena-App und stehen nur in `ventil_ereignis`
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung import hahn_arbiter
from bewaesserung.hahn_arbiter import HahnArbiter, Kanalprofil, baue_kanalprofile
from bewaesserung.modelle import (
    Ausloser,
    VentilAktion,
    VentilEreignis,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import VentilSicherung

DSWC1 = "bbbb0001-0000-4000-8000-000000000001"
DSWC2 = "bbbb0002-0000-4000-8000-000000000002"
CLUSTER = "garten-haupthahn"

# Die reale Topologie aus config/default.yaml (Stand 12.08.2026).
PROFILE = {
    (DSWC1, 1): Kanalprofil(("waldblumenhain",), CLUSTER, 6.0, True),
    (DSWC1, 2): Kanalprofil(
        ("bambuswald", "bambuswald_yogaraum"), CLUSTER, 1.87, False,
    ),
    (DSWC2, 1): Kanalprofil(("magerwiese",), CLUSTER, 6.0, True),
    (DSWC2, 2): Kanalprofil(("hecke",), CLUSTER, 1.4, False),
}
BUDGET = {CLUSTER: 10.0}


class SicherungAttrappe:
    """Nur die Sicht, die der Arbiter braucht: welche Kanaele laufen."""

    def __init__(self, aktiv: dict[int, tuple[str, ...]] | None = None):
        self._aktiv = dict(aktiv or {})

    def aktive_kanaele(self) -> dict[int, tuple[str, ...]]:
        return dict(self._aktiv)


class SpeicherOhneEreignisse:
    """Speicher-Attrappe ohne Ereignis-Sicht (Bestandsverhalten)."""

    async def hole_pre_soak_states(self) -> list[dict]:
        return []


class SpeicherMitEreignissen(SpeicherOhneEreignisse):
    def __init__(self, offene: list[dict] | None = None,
                 pre_soak: list[dict] | None = None):
        self.offene = list(offene or [])
        self.pre_soak = list(pre_soak or [])
        self.abfragen: list[datetime] = []

    async def hole_offene_ventil_kanaele(
        self, seit: datetime, bis: datetime | None = None,
    ) -> list[dict]:
        self.abfragen.append((seit, bis))
        return list(self.offene)

    async def hole_pre_soak_states(self) -> list[dict]:
        return list(self.pre_soak)


class SpeicherMitDbFehler(SpeicherOhneEreignisse):
    async def hole_offene_ventil_kanaele(
        self, seit: datetime, bis: datetime | None = None,
    ) -> list[dict]:
        raise RuntimeError("database is locked")


def _arbiter(speicher=None, profile=None, budget=None) -> HahnArbiter:
    return HahnArbiter(
        speicher=speicher or SpeicherOhneEreignisse(),
        kanalprofile=profile or PROFILE,
        cluster_max_lpm=budget or BUDGET,
    )


# --- Ursache 1: der Lock war geraetelokal ---------------------------------


@pytest.mark.asyncio
async def test_exklusiver_sprinkler_blockt_exklusiven_sprinkler_auf_anderem_dswc():
    """Der Vorfall selbst: magerwiese laeuft (DSWC 2), waldblumen will starten."""
    arbiter = _arbiter()
    arbiter.registriere_sicherung(DSWC1, SicherungAttrappe())
    arbiter.registriere_sicherung(
        DSWC2, SicherungAttrappe({1: ("magerwiese",)}),
    )
    entscheidung = await arbiter.pruefe(DSWC1, 1)
    assert entscheidung.erlaubt is False
    assert "magerwiese" in entscheidung.aktive_zonen


@pytest.mark.asyncio
async def test_volumen_budget_summiert_ueber_geraetegrenzen():
    """Isomorphie-Check aus T-0536: das Budget war ebenfalls geraetelokal.

    hecke (1.4) + bambus (1.87) laufen, magerwiese-Kanal ist hier bewusst
    NICHT exklusiv gesetzt -- sonst wuerde schon die Exklusivregel greifen und
    der Volumenpfad bliebe ungetestet. 1.4 + 1.87 + 6.0 = 9.27 <= 10, also
    erlaubt; mit engerem Budget muss es blocken.
    """
    profile = dict(PROFILE)
    profile[(DSWC2, 1)] = Kanalprofil(("magerwiese",), CLUSTER, 6.0, False)
    arbiter = _arbiter(profile=profile)
    arbiter.registriere_sicherung(
        DSWC1, SicherungAttrappe({2: ("bambuswald",)}),
    )
    arbiter.registriere_sicherung(DSWC2, SicherungAttrappe({2: ("hecke",)}))
    ok = await arbiter.pruefe(DSWC2, 1)
    assert ok.erlaubt is True
    assert ok.verbrauch_aktuell_lpm == pytest.approx(3.27)

    eng = _arbiter(profile=profile, budget={CLUSTER: 9.0})
    eng.registriere_sicherung(DSWC1, SicherungAttrappe({2: ("bambuswald",)}))
    eng.registriere_sicherung(DSWC2, SicherungAttrappe({2: ("hecke",)}))
    blockiert = await eng.pruefe(DSWC2, 1)
    assert blockiert.erlaubt is False
    assert "belegt" in blockiert.grund


# --- Ursache 2: Fremdlaeufe stehen nur in ventil_ereignis -----------------


@pytest.mark.asyncio
async def test_fremdlauf_aus_ereignistabelle_blockt_exklusiven_start():
    """`ausloser='ignoriert'` heisst NICHT "Ventil zu" -- App-Lauf blockt."""
    speicher = SpeicherMitEreignissen(offene=[
        {"ventil_id": f"{DSWC2}:1", "zone_ids": ["magerwiese"],
         "ausloser": "ignoriert"},
    ])
    arbiter = _arbiter(speicher)
    arbiter.registriere_sicherung(DSWC1, SicherungAttrappe())
    arbiter.registriere_sicherung(DSWC2, SicherungAttrappe())  # _aktiv leer!
    entscheidung = await arbiter.pruefe(DSWC1, 1)
    assert entscheidung.erlaubt is False
    assert "magerwiese" in entscheidung.aktive_zonen


@pytest.mark.asyncio
async def test_frische_schranke_haelt_geist_offen_nicht_ewig():
    """Die Ereignis-Abfrage bekommt ein `seit` -- sonst sperrt ein verlorenes
    SCHLIESSEN den Hahn dauerhaft (Fremdlaeufe heilt der Orphan-Job nicht)."""
    speicher = SpeicherMitEreignissen()
    arbiter = _arbiter(speicher)
    jetzt = datetime(2026, 8, 12, 18, 29)
    await arbiter.pruefe(DSWC1, 1, jetzt=jetzt)
    assert speicher.abfragen == [(jetzt - timedelta(minutes=150), jetzt)]


@pytest.mark.asyncio
async def test_nicht_kanal_ventil_ids_blocken_nicht():
    """`sensor_heuristik` (Aquabloom/Heuristik) ist kein Ventil am Hahn."""
    speicher = SpeicherMitEreignissen(offene=[
        {"ventil_id": "sensor_heuristik", "zone_ids": ["kasten_4"],
         "ausloser": "aquabloom"},
    ])
    arbiter = _arbiter(speicher)
    assert (await arbiter.pruefe(DSWC1, 1)).erlaubt is True


# --- Betreiber-Regel: exklusiv heisst "gar nichts anderes" ----------------


@pytest.mark.asyncio
async def test_exklusiv_blockt_auch_druckkompensierten_mikrodrip():
    """Andre 12.08.: bei Regnern darf NICHTS laufen, auch kein Tropfkreis."""
    arbiter = _arbiter()
    arbiter.registriere_sicherung(DSWC1, SicherungAttrappe())
    arbiter.registriere_sicherung(DSWC2, SicherungAttrappe({2: ("hecke",)}))
    entscheidung = await arbiter.pruefe(DSWC1, 1)
    assert entscheidung.erlaubt is False
    assert "hecke" in entscheidung.aktive_zonen


@pytest.mark.asyncio
async def test_exklusiv_blockt_unbekannten_aktiven_kanal():
    """Ein Kanal ohne Konfig-Profil kann alles sein -- fuer einen Sprinkler
    ist das Grund genug, nicht zu starten."""
    speicher = SpeicherMitEreignissen(offene=[
        {"ventil_id": "fremdes-geraet:3", "zone_ids": [], "ausloser": "manuell"},
    ])
    arbiter = _arbiter(speicher)
    entscheidung = await arbiter.pruefe(DSWC1, 1)
    assert entscheidung.erlaubt is False
    assert "fremdes-geraet:3" in entscheidung.aktive_zonen


@pytest.mark.asyncio
async def test_mikrodrip_startet_nicht_gegen_laufenden_sprinkler():
    """Gegenrichtung: der Tropfkreis darf sich nicht dazustellen."""
    arbiter = _arbiter()
    arbiter.registriere_sicherung(
        DSWC1, SicherungAttrappe({1: ("waldblumenhain",)}),
    )
    arbiter.registriere_sicherung(DSWC2, SicherungAttrappe())
    entscheidung = await arbiter.pruefe(DSWC2, 2)
    assert entscheidung.erlaubt is False
    assert "waldblumenhain" in entscheidung.aktive_zonen


@pytest.mark.asyncio
async def test_zwei_mikrodrip_kreise_laufen_weiter_parallel():
    """Kein Ueberblocken: druckkompensierte Kreise duerfen sich teilen."""
    arbiter = _arbiter()
    arbiter.registriere_sicherung(
        DSWC1, SicherungAttrappe({2: ("bambuswald", "bambuswald_yogaraum")}),
    )
    arbiter.registriere_sicherung(DSWC2, SicherungAttrappe())
    assert (await arbiter.pruefe(DSWC2, 2)).erlaubt is True


@pytest.mark.asyncio
async def test_eigener_kanal_blockt_sich_nicht_selbst():
    """Der laufende eigene Kanal ist Sache von `bewaessere` (Belegt-Guard)."""
    speicher = SpeicherMitEreignissen(offene=[
        {"ventil_id": f"{DSWC1}:1", "zone_ids": ["waldblumenhain"],
         "ausloser": "automatik"},
    ])
    arbiter = _arbiter(speicher)
    arbiter.registriere_sicherung(
        DSWC1, SicherungAttrappe({1: ("waldblumenhain",)}),
    )
    assert (await arbiter.pruefe(DSWC1, 1)).erlaubt is True


# --- Pre-Soak: die Sequenz reserviert den Hahn ueber die Pause ------------


def _pre_soak_zeile(zone_id: str, zonen: list[str], gestartet: datetime) -> dict:
    return {
        "zone_id": zone_id, "kanal": 1, "zone_ids_kanal": zonen,
        "pre_soak_s": 300, "pause_s": 1800, "haupt_s": 3600,
        "gestartet_am": gestartet, "phase": "pause", "ausloser": "automatik",
        "haupt_pulse": 1, "haupt_pause_s": 0, "haupt_pulse_gestartet": 0,
    }


@pytest.mark.asyncio
async def test_pre_soak_pause_eines_sprinklers_reserviert_den_hahn():
    """Waehrend der Soak-Pause ist das Ventil zu, der Vorgang laeuft aber --
    sonst legt sich ein Fremdstart in die Pause und kollidiert mit der
    Hauptdose (genau das Muster vom 12.08. um 18:29)."""
    jetzt = datetime(2026, 8, 12, 18, 20)
    speicher = SpeicherMitEreignissen(pre_soak=[
        _pre_soak_zeile("waldblumenhain", ["waldblumenhain"],
                        jetzt - timedelta(minutes=10)),
    ])
    arbiter = _arbiter(speicher)
    fremd = await arbiter.pruefe(DSWC2, 2, jetzt=jetzt)
    assert fremd.erlaubt is False
    assert "waldblumenhain" in fremd.aktive_zonen
    # Die eigene Hauptdose darf die eigene Sequenz NICHT blockieren.
    eigen = await arbiter.pruefe(DSWC1, 1, jetzt=jetzt)
    assert eigen.erlaubt is True


@pytest.mark.asyncio
async def test_pre_soak_pause_eines_mikrodrip_reserviert_nicht():
    """Bei druckkompensiertem Mikrodrip genuegt der Ventilzustand -- eine
    25-min-Pause darf den Hahn nicht ohne physischen Grund sperren."""
    jetzt = datetime(2026, 8, 12, 9, 0)
    speicher = SpeicherMitEreignissen(pre_soak=[
        _pre_soak_zeile("bambuswald", ["bambuswald", "bambuswald_yogaraum"],
                        jetzt - timedelta(minutes=10)),
    ])
    arbiter = _arbiter(speicher)
    assert (await arbiter.pruefe(DSWC2, 2, jetzt=jetzt)).erlaubt is True


# --- Fehlerrichtung -------------------------------------------------------


@pytest.mark.asyncio
async def test_db_fehler_blockt_exklusiven_start_aber_nicht_mikrodrip():
    """Fail-closed nur dort, wo der Schaden still ist."""
    arbiter = _arbiter(SpeicherMitDbFehler())
    sprinkler = await arbiter.pruefe(DSWC1, 1)
    assert sprinkler.erlaubt is False
    assert "DB-Fehler" in sprinkler.grund
    assert (await arbiter.pruefe(DSWC1, 2)).erlaubt is True


@pytest.mark.asyncio
async def test_speicher_ohne_ereignis_sicht_ist_kein_fehlerfall():
    """Alte Attrappen/Setups ohne den Accessor duerfen nicht fail-closed."""
    arbiter = _arbiter(SpeicherOhneEreignisse())
    assert (await arbiter.pruefe(DSWC1, 1)).erlaubt is True


# --- Profil-Aufbau aus der Zonen-Konfig -----------------------------------


def test_baue_kanalprofile_verdichtet_zonen_am_gleichen_kanal():
    zonen = [
        ZonenKonfig(zone_id="bambuswald", name="Bambus", ventil_kanal=2,
                    hahn_cluster=CLUSTER, verbrauch_lpm=1.87, exklusiv=False),
        ZonenKonfig(zone_id="bambuswald_yogaraum", name="Yoga", ventil_kanal=2,
                    hahn_cluster=CLUSTER, verbrauch_lpm=1.87, exklusiv=False),
        ZonenKonfig(zone_id="waldblumenhain", name="Wald", ventil_kanal=1,
                    hahn_cluster=CLUSTER, verbrauch_lpm=6.0, exklusiv=True),
        ZonenKonfig(zone_id="zitrus", name="Zitrus"),  # ohne Ventil
    ]
    profile = baue_kanalprofile(zonen, geraet_id=DSWC1)
    assert set(profile) == {(DSWC1, 1), (DSWC1, 2)}
    assert profile[(DSWC1, 2)].zone_ids == ("bambuswald", "bambuswald_yogaraum")
    # Ein Lauf, ein Verbrauch: max() statt Summe.
    assert profile[(DSWC1, 2)].verbrauch_lpm == pytest.approx(1.87)
    assert profile[(DSWC1, 1)].exklusiv is True


def test_baue_kanalprofile_exklusiv_faerbt_den_ganzen_kanal():
    """Eine exklusive Zone am Kanal macht den KANAL exklusiv."""
    zonen = [
        ZonenKonfig(zone_id="a", name="A", ventil_kanal=1,
                    hahn_cluster=CLUSTER, verbrauch_lpm=1.0, exklusiv=False),
        ZonenKonfig(zone_id="b", name="B", ventil_kanal=1,
                    hahn_cluster=CLUSTER, verbrauch_lpm=6.0, exklusiv=True),
    ]
    profile = baue_kanalprofile(zonen, geraet_id=DSWC1)
    assert profile[(DSWC1, 1)].exklusiv is True
    assert profile[(DSWC1, 1)].verbrauch_lpm == pytest.approx(6.0)


# --- Echte DB-Sicht -------------------------------------------------------


@pytest.fixture
def speicher(tmp_path):
    sp = Speicher(str(tmp_path / "hahn.db"))
    asyncio.get_event_loop_policy().new_event_loop()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(sp.verbinden())
    yield sp, loop
    # Ohne schliessen() im Teardown haengt die Suite (aiosqlite-Fixture).
    loop.run_until_complete(sp.schliessen())
    loop.close()


def _ereignis(zone_id, ventil_id, aktion, zeit, ausloser=Ausloser.IGNORIERT):
    return VentilEreignis(
        zeitstempel=zeit, zone_id=zone_id, ventil_id=ventil_id,
        aktion=aktion, dauer_sekunden=0, ausloser=ausloser,
    )


def test_hole_offene_ventil_kanaele_kennt_offene_und_geschlossene(speicher):
    sp, loop = speicher
    basis = datetime(2026, 8, 12, 18, 0)

    async def _lauf():
        # Geschlossener Lauf.
        await sp.speichere_ventil_ereignis(
            _ereignis("hecke", f"{DSWC2}:2", VentilAktion.OEFFNEN, basis))
        await sp.speichere_ventil_ereignis(
            _ereignis("hecke", f"{DSWC2}:2", VentilAktion.SCHLIESSEN,
                      basis + timedelta(minutes=5)))
        # Offener Fremdlauf (App).
        await sp.speichere_ventil_ereignis(
            _ereignis("magerwiese", f"{DSWC2}:1", VentilAktion.OEFFNEN,
                      basis + timedelta(minutes=24)))
        # Kein Ventil am Hahn.
        await sp.speichere_ventil_ereignis(
            _ereignis("kasten_4", "sensor_heuristik", VentilAktion.OEFFNEN,
                      basis + timedelta(minutes=10), Ausloser.AQUABLOOM))
        return await sp.hole_offene_ventil_kanaele(
            seit=basis - timedelta(hours=2))

    offene = loop.run_until_complete(_lauf())
    assert [o["ventil_id"] for o in offene] == [f"{DSWC2}:1"]
    assert offene[0]["zone_ids"] == ["magerwiese"]


def test_hole_offene_ventil_kanaele_doppel_oeffnen_zaehlt_als_geschlossen(speicher):
    """Doppel-OEFFNEN mit EINEM SCHLIESSEN: der Kanal ist zu."""
    sp, loop = speicher
    basis = datetime(2026, 8, 12, 13, 34)

    async def _lauf():
        await sp.speichere_ventil_ereignis(
            _ereignis("magerwiese", f"{DSWC2}:1", VentilAktion.OEFFNEN, basis))
        await sp.speichere_ventil_ereignis(
            _ereignis("magerwiese", f"{DSWC2}:1", VentilAktion.OEFFNEN,
                      basis + timedelta(seconds=1)))
        await sp.speichere_ventil_ereignis(
            _ereignis("magerwiese", f"{DSWC2}:1", VentilAktion.SCHLIESSEN,
                      basis + timedelta(minutes=5)))
        return await sp.hole_offene_ventil_kanaele(
            seit=basis - timedelta(hours=2))

    assert loop.run_until_complete(_lauf()) == []


def test_hole_offene_ventil_kanaele_ignoriert_zu_alte_opens(speicher):
    sp, loop = speicher
    basis = datetime(2026, 8, 12, 3, 0)

    async def _lauf():
        await sp.speichere_ventil_ereignis(
            _ereignis("magerwiese", f"{DSWC2}:1", VentilAktion.OEFFNEN, basis))
        return await sp.hole_offene_ventil_kanaele(
            seit=basis + timedelta(minutes=1))

    assert loop.run_until_complete(_lauf()) == []


# --- Integration: VentilSicherung + Arbiter -------------------------------


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


class SpeicherStumm(SpeicherOhneEreignisse):
    async def speichere_ventil_ereignis(self, ereignis) -> None:
        pass

    async def existiert_schliessen_seit(self, ventil_id, seit, zone_ids=None):
        return False

    async def loesche_live_lauf_state(self, kanal, geraet_id) -> None:
        pass


@pytest.mark.asyncio
async def test_bewaessere_geht_ueber_den_arbiter():
    """Der Gate muss im echten Startpfad haengen, nicht nur in der Pruefmethode
    ([[fehlerpattern_detektor_ohne_konsument]])."""
    client = ClientAttrappe()
    sicherung = VentilSicherung(
        client=client, speicher=SpeicherStumm(),  # type: ignore[arg-type]
        ventil_geraet_id=DSWC1,
        kanal_zu_valve_id={1: "valve-wald"},
    )
    arbiter = _arbiter(SpeicherMitEreignissen(offene=[
        {"ventil_id": f"{DSWC2}:1", "zone_ids": ["magerwiese"],
         "ausloser": "ignoriert"},
    ]))
    arbiter.registriere_sicherung(DSWC1, sicherung)
    sicherung.setze_hahn_arbiter(arbiter)

    ok = await sicherung.bewaessere(1, ["waldblumenhain"], 600, Ausloser.MANUELL)
    assert ok is False
    assert client.oeffnen_aufrufe == []
    assert not sicherung.ist_aktiv(1)


class ClientLangsam(ClientAttrappe):
    """Oeffnen dauert -- macht das Fenster zwischen Pruefung und Oeffnen auf."""

    async def ventil_oeffnen(self, geraet_id, dauer_sekunden, valve_id=None):
        await asyncio.sleep(0.05)
        self.oeffnen_aufrufe.append((geraet_id, dauer_sekunden, valve_id))


@pytest.mark.asyncio
async def test_zwei_gleichzeitige_starter_kommen_nicht_beide_durch():
    """Auto-Loop und manueller Start im selben Moment: nur einer oeffnet.

    Ohne den Reservierungs-Lock sehen beide einen leeren Hahn, weil zwischen
    Pruefung und Oeffnen mehrere `await` liegen.
    """
    arbiter = _arbiter(SpeicherMitEreignissen())
    sicherungen = {}
    for gid, valve in ((DSWC1, "valve-wald"), (DSWC2, "valve-mager")):
        s = VentilSicherung(
            client=ClientLangsam(), speicher=SpeicherStumm(),  # type: ignore[arg-type]
            ventil_geraet_id=gid, kanal_zu_valve_id={1: valve},
        )
        s.setze_hahn_arbiter(arbiter)
        arbiter.registriere_sicherung(gid, s)
        sicherungen[gid] = s

    ergebnisse = await asyncio.gather(
        sicherungen[DSWC1].bewaessere(1, ["waldblumenhain"], 600, Ausloser.AUTOMATIK),
        sicherungen[DSWC2].bewaessere(1, ["magerwiese"], 600, Ausloser.MANUELL),
    )
    assert sorted(ergebnisse) == [False, True]
    geoeffnet = sum(
        len(s._client.oeffnen_aufrufe) for s in sicherungen.values()
    )
    assert geoeffnet == 1


@pytest.mark.asyncio
async def test_reservierung_wartet_nicht_unbegrenzt(monkeypatch):
    """Ein haengender Cloud-Aufruf haelt den Lock -- er darf nicht die ganze
    Anlage stilllegen. Wer die Reservierung nicht bekommt, startet nicht."""
    monkeypatch.setattr(hahn_arbiter, "RESERVIERUNG_TIMEOUT_S", 0.05)
    arbiter = _arbiter(SpeicherMitEreignissen())
    sicherung = VentilSicherung(
        client=ClientAttrappe(), speicher=SpeicherStumm(),  # type: ignore[arg-type]
        ventil_geraet_id=DSWC1, kanal_zu_valve_id={2: "valve-bambus"},
    )
    sicherung.setze_hahn_arbiter(arbiter)
    arbiter.registriere_sicherung(DSWC1, sicherung)

    await arbiter._start_lock.acquire()   # jemand haengt
    try:
        ok = await sicherung.bewaessere(
            2, ["bambuswald"], 600, Ausloser.AUTOMATIK,
        )
    finally:
        arbiter._start_lock.release()
    assert ok is False
    assert sicherung._client.oeffnen_aufrufe == []


@pytest.mark.asyncio
async def test_ohne_arbiter_bleibt_die_lokale_rechnung():
    """Backward-Compat: Sicherungen ohne Arbiter verhalten sich wie vorher."""
    client = ClientAttrappe()
    sicherung = VentilSicherung(
        client=client, speicher=SpeicherStumm(),  # type: ignore[arg-type]
        ventil_geraet_id=DSWC1,
        kanal_zu_valve_id={1: "valve-wald"},
        kanal_zu_zone_lockprofile={1: [("waldblumenhain", CLUSTER, 6.0, True)]},
        cluster_max_lpm=BUDGET,
    )
    ok = await sicherung.bewaessere(1, ["waldblumenhain"], 600, Ausloser.MANUELL)
    assert ok is True
