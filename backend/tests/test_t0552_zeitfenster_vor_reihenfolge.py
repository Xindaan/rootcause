"""T-0552: wer ein verfallendes Zeitfenster hat, verdraengt eine wartende Sequenz.

**Der Realfall, live beobachtet am 29.08.2026:**

    18:02:42  bambuswald + yogaraum + hecke  pre_soak oeffnen   (K2)
    18:02:43  pre_soak_blockiert kanal=1                        <- waldblumenhain abgewiesen
    18:07:39  dieselben drei                 pre_soak schliessen -> Soak-Pause
    18:08:19  waldblumenhain                 pre_soak oeffnen   (K1)

Waldblumenhain wurde abgewiesen, solange die Ventile offen waren, und kam
vierzig Sekunden nach deren Schliessen durch. Die Soak-Pause reservierte den
Hahn nicht -- die drei Mikrodrip-Zonen verloren ihre Hauptdose.

**Die Ursache war eine Verwechslung zweier Eigenschaften.** `exklusiv` ist
Hydraulik ("braucht den vollen Druck"), das Zeitfenster ist Agronomie
("verfaellt heute"). Der Arbiter benutzte `exklusiv` fuer beide Fragen. Dass
das bisher die richtige Antwort gab, lag nur daran, dass in dieser Anlage
dieselben Zonen zufaellig beides tragen.

**Die Betreiber-Regel (Andre, 29.08.):** Waldblumenhain muss vor der Nacht
laufen, sonst ist die Gelegenheit weg. Bambus und Hecke sind Mikrodrip, denen
fehlt nur Zeit, nicht die Gelegenheit -- sie koennen danach.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from bewaesserung.hahn_arbiter import (
    HahnArbiter,
    Kanalprofil,
    baue_kanalprofile,
)


GERAET = "dswc1"
K1 = (GERAET, 1)   # waldblumenhain: exklusiv + Zeitfenster
K2 = (GERAET, 2)   # bambuswald/hecke: Mikrodrip, kein Fenster

T0 = datetime(2026, 8, 29, 18, 8)


class _Zone:
    def __init__(self, zone_id, kanal, exklusiv, fenster):
        self.zone_id = zone_id
        self.ventil_kanal = kanal
        self.ventil_geraet_id = GERAET
        self.exklusiv = exklusiv
        self.bevorzugte_zeiten = fenster
        self.hahn_cluster = "haupthahn"
        self.verbrauch_lpm = 10.0


ZONEN = [
    _Zone("waldblumenhain", 1, True, ["18:00-21:00"]),
    _Zone("bambuswald", 2, False, []),
    _Zone("hecke", 2, False, []),
]


class _Speicher:
    """Keine offenen Ventile; die Pre-Soak-Sicht wird je Test gesetzt."""

    def __init__(self, pre_soak_zonen=()):
        self.pre_soak_zonen = set(pre_soak_zonen)

    async def hole_offene_ventil_kanaele(self, seit=None, bis=None):
        return []


def _arbiter(pre_soak_zonen=(), profile=None):
    profile = profile or baue_kanalprofile(ZONEN, geraet_id=GERAET)
    arb = object.__new__(HahnArbiter)
    arb._profile = profile
    arb._sicherungen = {}
    arb._speicher = _Speicher(pre_soak_zonen)
    arb._zone_zu_kanal = {
        z.zone_id: (GERAET, z.ventil_kanal) for z in ZONEN
    }
    arb._cluster_max_lpm = {"haupthahn": 100.0}
    arb._max_offen = __import__("datetime").timedelta(hours=3)
    arb._wartend = {}
    return arb


async def _aktive(arb, frager_schluessel, pre_soak_zonen):
    """Ruft `_aktive_kanaele` mit gesetzter Pre-Soak-Sicht."""
    import bewaesserung.hahn_arbiter as mod

    async def _stub(speicher, jetzt=None):
        return set(pre_soak_zonen)

    original = mod.laufende_pre_soak_zonen
    mod.laufende_pre_soak_zonen = _stub
    try:
        aktive, _ = await arb._aktive_kanaele(
            T0, ausser=frager_schluessel,
            frager=arb._profile.get(frager_schluessel),
        )
        return {tuple(sorted(a.zone_ids)) for a in aktive}
    finally:
        mod.laufende_pre_soak_zonen = original


# --------------------------------------------------------------------------
# Das Profil traegt beide Eigenschaften getrennt
# --------------------------------------------------------------------------

def test_profil_trennt_hydraulik_von_agronomie():
    profile = baue_kanalprofile(ZONEN, geraet_id=GERAET)
    assert profile[K1].exklusiv is True
    assert profile[K1].zeitfenster_gebunden is True
    assert profile[K2].exklusiv is False
    assert profile[K2].zeitfenster_gebunden is False


def test_ein_fenster_an_einer_zone_bindet_den_ganzen_kanal():
    """Analog zu `exklusiv`: die Zonen eines Kanals teilen einen Lauf."""
    zonen = [
        _Zone("a", 3, False, []),
        _Zone("b", 3, False, ["05:00-07:00"]),
    ]
    profile = baue_kanalprofile(zonen, geraet_id=GERAET)
    assert profile[(GERAET, 3)].zeitfenster_gebunden is True


# --------------------------------------------------------------------------
# Die Regel
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zone_mit_fenster_verdraengt_wartende_sequenz():
    """Der Realfall: waldblumenhain darf in die Soak-Pause von K2 hinein,
    weil sein Fenster heute verfaellt."""
    arb = _arbiter()
    aktive = await _aktive(arb, K1, pre_soak_zonen={"bambuswald"})
    assert aktive == set(), "waldblumenhain muss durchkommen"


@pytest.mark.asyncio
async def test_zone_ohne_fenster_verdraengt_NICHT():
    """Der eigentliche Fix. Ein Starter ohne verfallendes Fenster muss die
    wartende Sequenz respektieren -- sonst kostet er ihr die Hauptdose, ohne
    dass ihm selbst etwas verloren ginge."""
    profile = baue_kanalprofile(ZONEN, geraet_id=GERAET)
    # Hypothetisch: K1 exklusiv, aber OHNE Fenster.
    profile[K1] = Kanalprofil(
        zone_ids=("waldblumenhain",), cluster="haupthahn",
        verbrauch_lpm=10.0, exklusiv=True, zeitfenster_gebunden=False,
    )
    arb = _arbiter(profile=profile)
    aktive = await _aktive(arb, K1, pre_soak_zonen={"bambuswald"})
    assert aktive == {("bambuswald", "hecke")}, (
        "ohne Fenster muss die wartende Sequenz den Hahn behalten"
    )


@pytest.mark.asyncio
async def test_wartende_sequenz_MIT_fenster_wird_nie_verdraengt():
    """Zwei Zeitfenster-Zonen: hier gilt wieder wer zuerst da war, denn beiden
    verfaellt etwas."""
    zonen = ZONEN + [_Zone("magerwiese", 3, True, ["19:00-21:00"])]
    profile = baue_kanalprofile(zonen, geraet_id=GERAET)
    arb = _arbiter(profile=profile)
    arb._zone_zu_kanal["magerwiese"] = (GERAET, 3)
    aktive = await _aktive(arb, K1, pre_soak_zonen={"magerwiese"})
    assert aktive == {("magerwiese",)}


@pytest.mark.asyncio
async def test_keine_wartende_sequenz_keine_blockade():
    arb = _arbiter()
    assert await _aktive(arb, K1, pre_soak_zonen=set()) == set()


# --------------------------------------------------------------------------
# Negativproben
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_negativprobe_alter_filter_laesst_jeden_durch():
    """Baut den alten Zustand nach (`if not profil.exklusiv: continue` ohne
    Ansehen des Fragers). Dann kommt auch ein Starter OHNE Fenster durch --
    genau der Fehler, den T-0552 behebt."""
    profile = baue_kanalprofile(ZONEN, geraet_id=GERAET)
    profile[K1] = Kanalprofil(
        zone_ids=("waldblumenhain",), cluster="haupthahn",
        verbrauch_lpm=10.0, exklusiv=True, zeitfenster_gebunden=False,
    )
    arb = _arbiter(profile=profile)
    # Alter Pfad = Frager unbekannt -> darf niemanden verdraengen duerfen.
    import bewaesserung.hahn_arbiter as mod

    async def _stub(speicher, jetzt=None):
        return {"bambuswald"}

    original = mod.laufende_pre_soak_zonen
    mod.laufende_pre_soak_zonen = _stub
    try:
        aktive, _ = await arb._aktive_kanaele(T0, ausser=K1, frager=None)
    finally:
        mod.laufende_pre_soak_zonen = original
    assert {tuple(sorted(a.zone_ids)) for a in aktive} == {
        ("bambuswald", "hecke"),
    }, "ohne bekannten Frager muss die Sequenz geschuetzt bleiben (fail-safe)"


@pytest.mark.asyncio
async def test_negativprobe_ohne_fenster_am_profil_faellt_die_regel_zurueck():
    """Zweite Negativprobe: nimmt man dem Frager das Fenster-Merkmal, greift
    die Ausnahme nicht mehr und die Sequenz behaelt den Hahn."""
    profile = baue_kanalprofile(ZONEN, geraet_id=GERAET)
    ohne = Kanalprofil(
        zone_ids=profile[K1].zone_ids, cluster=profile[K1].cluster,
        verbrauch_lpm=profile[K1].verbrauch_lpm, exklusiv=True,
        zeitfenster_gebunden=False,
    )
    profile[K1] = ohne
    arb = _arbiter(profile=profile)
    assert await _aktive(arb, K1, pre_soak_zonen={"bambuswald"}) != set()


# --------------------------------------------------------------------------
# Regression: die echte Konfiguration
# --------------------------------------------------------------------------

@pytest.mark.live_config
def test_echte_konfig_traegt_die_erwartete_verteilung():
    """Haelt fest, worauf die Regel heute trifft. Aendert jemand ein Fenster
    oder ein exklusiv-Flag, faellt dieser Test und die Wirkung wird neu
    bewertet."""
    from bewaesserung.konfig import lade_konfig

    konfig = lade_konfig()
    profile = baue_kanalprofile(
        [z for z in konfig.zonen if z.ventil_kanal is not None],
    )
    gefunden = {
        tuple(sorted(p.zone_ids)): (p.exklusiv, p.zeitfenster_gebunden)
        for p in profile.values()
    }
    assert ("waldblumenhain",) in gefunden
    assert gefunden[("waldblumenhain",)] == (True, True)
    bambus = next(k for k in gefunden if "bambuswald" in k)
    assert gefunden[bambus] == (False, False)
