"""T-0618 (Andre 13.10.2026, Pflanzenwohl): kritisch wartende Sequenz vor
nicht kritischem Frager; die verdraengte Sequenz zeigt "verdraengt".

Ausgangslage T-0552/T-0598: eine wartende Mikrodrip-Sequenz (Soak-Pause, Ventil
zu) reserviert den Hahn, AUSSER ein Frager mit JETZT offenem Zeitfenster will
starten -- dann wird sie verdraengt. Realfall 22.09.: yogaraum (45 % bei
kritisch 50) verlor so ihre Hauptdose an waldblumenhain.

Neu: ist die wartende Sequenz kritisch und der Frager nicht, behaelt sie den
Hahn. Beide kritisch, wartende nicht kritisch oder kein Urteil: wie bisher.
Kein laufendes Ventil wird angefasst (Arbiter-Invariante); umgesetzt ist nur
das Verweigern des Starts. Gate W5: die Regel gilt nur fuer AUTOMATIK-Starts
(manuell exakt wie vor T-0618), vermerkt wird eine Verdraengung erst, wenn der
Frager wirklich geoeffnet hat, und kritisch zaehlen nur scharfe Loop-Zonen.

Alles mit Attrappen: keine Cloud, kein echtes Ventil; die VentilSicherung-Tests
nutzen eine temporaere SQLite-Datei.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import bewaesserung.api_server as api_server
import bewaesserung.hahn_arbiter as mod
from bewaesserung.entscheidung import Entscheidungsmotor
from bewaesserung.hahn_arbiter import HahnArbiter, baue_kanalprofile
from bewaesserung.gardena_client import GardenaClient
from bewaesserung.modelle import (
    Ausloser,
    FeuchteRegime,
    ZeitFenster,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.pre_soak import HAUPTDOSE_VERDRAENGT, PreSoakManager
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import VentilSicherung

GERAET = "dswc-test"
K1 = 1   # waldblumenhain: Sprinkler, exklusiv, Fenster (Klammer 04-17)
K2 = 2   # bambuswald + bambuswald_yogaraum: Mikrodrip, kein Fenster
IM_FENSTER = datetime(2026, 7, 1, 10, 0)
AUTO = Ausloser.AUTOMATIK


class _Zone:
    def __init__(self, zone_id, kanal, exklusiv, fenster):
        self.zone_id = zone_id
        self.ventil_kanal = kanal
        self.ventil_geraet_id = GERAET
        self.exklusiv = exklusiv
        self.bevorzugte_zeiten = [
            ZeitFenster(von=f.split("-")[0], bis=f.split("-")[1]) for f in fenster
        ]
        self.hahn_cluster = "haupthahn"
        self.verbrauch_lpm = 10.0
        self.giessfenster_et0 = None


ZONEN = [
    _Zone("waldblumenhain", K1, True, ["04:00-17:00"]),
    _Zone("bambuswald", K2, False, []),
    _Zone("bambuswald_yogaraum", K2, False, []),
]


class _Speicher:
    async def hole_offene_ventil_kanaele(self, **_):
        return []


class _BelegteSicherung:
    """Engine-Sicht: haelt einen fremden Kanal aktiv (anderer Blocker)."""

    def aktive_kanaele(self):
        return {3: ("fremd",)}


def _arbiter(urteil: dict | None):
    """`urteil`: zone_ids-Tupel -> True/False/None (Kritisch-Pruefer)."""
    pruefer = None
    if urteil is not None:
        async def pruefer(zone_ids, jetzt):
            return urteil[tuple(zone_ids)]
    return HahnArbiter(
        _Speicher(), baue_kanalprofile(ZONEN, geraet_id=GERAET),
        {"haupthahn": 100.0}, kritisch_pruefer=pruefer,
    )


@pytest.fixture
def wartende_sequenz(monkeypatch):
    """bambuswald steht in der Soak-Pause (Pre-Soak-Sicht des Arbiters)."""
    async def _pre_soak(speicher, jetzt=None):
        return {"bambuswald"}
    monkeypatch.setattr(mod, "laufende_pre_soak_zonen", _pre_soak)


WARTEND = ("bambuswald", "bambuswald_yogaraum")
FRAGER = ("waldblumenhain",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wartend_kritisch", "frager_kritisch", "frager_darf"),
    [
        # Der neue Zweig: Pflanzenwohl schlaegt das verfallende Fenster.
        (True, False, False),
        # Beide kritisch: Fenster-Vorrang wie bisher (T-0552/T-0598).
        (True, True, True),
        # Wartende nicht kritisch: wie bisher.
        (False, False, True),
        # Kein Urteil ueber den Frager (kein Messwert): kein Vorrang ohne Beleg.
        (True, None, True),
    ],
    ids=["kritisch_vor_nicht_kritisch", "beide_kritisch",
         "wartend_nicht_kritisch", "frager_unbekannt"],
)
async def test_vorrang_je_kritisch_zweig(
    wartende_sequenz, wartend_kritisch, frager_kritisch, frager_darf,
):
    arb = _arbiter({WARTEND: wartend_kritisch, FRAGER: frager_kritisch})

    e = await arb.pruefe(GERAET, K1, jetzt=IM_FENSTER, ausloser=AUTO)
    arb.vermerke_verdraengung(e)   # wie VentilSicherung nach dem Oeffnen

    assert e.erlaubt is frager_darf, e.grund
    verdraengt = arb.verdraengung("bambuswald_yogaraum")
    if frager_darf:
        # Verdraengt -> fuer die Karte vermerkt, mit dem Verdraenger.
        assert verdraengt is not None
        assert verdraengt.durch == FRAGER
        assert verdraengt.seit == IM_FENSTER
    else:
        assert "bambuswald" in e.aktive_zonen
        assert verdraengt is None


@pytest.mark.asyncio
async def test_verdraengung_nur_vermerkt_wenn_der_frager_startet(
    wartende_sequenz,
):
    """Ein Frager, den ein ANDERER Blocker abweist, hat niemanden verdraengt.
    Sonst zeigte die Karte "verdraengt", obwohl die Sequenz den Hahn behaelt."""
    arb = _arbiter({WARTEND: False, FRAGER: False})
    arb.registriere_sicherung("fremd-geraet", _BelegteSicherung())

    e = await arb.pruefe(GERAET, K1, jetzt=IM_FENSTER, ausloser=AUTO)
    arb.vermerke_verdraengung(e)

    assert e.erlaubt is False
    assert e.verdraengt == ()
    assert arb.verdraengung("bambuswald") is None


@pytest.mark.asyncio
async def test_verdraengung_endet_wenn_der_kanal_wieder_starten_darf(
    wartende_sequenz,
):
    """Kommt die Hauptdose doch noch (Frager war kurz), darf die Karte nicht
    weiter "verdraengt" zeigen, waehrend Wasser laeuft."""
    arb = _arbiter({WARTEND: False, FRAGER: False})
    e = await arb.pruefe(GERAET, K1, jetzt=IM_FENSTER, ausloser=AUTO)
    assert e.erlaubt
    arb.vermerke_verdraengung(e)
    assert arb.verdraengung("bambuswald") is not None

    spaeter = IM_FENSTER + timedelta(minutes=15)
    assert (await arb.pruefe(GERAET, K2, jetzt=spaeter)).erlaubt

    assert arb.verdraengung("bambuswald") is None


# --- Gate W5: echte VentilSicherung, Ausloeser und Vermerk nach dem Oeffnen --


class _FesteUhr(datetime):
    """`datetime.now()` des Arbiters auf IM_FENSTER (Fenster 04-17 offen)."""

    @classmethod
    def now(cls, tz=None):
        return IM_FENSTER


class _ClientOhneNetz(GardenaClient):
    """Echter GardenaClient, nur die Cloud-Kommandos sind Attrappen."""

    def __init__(self, oeffnen_fehler: bool = False):
        super().__init__(client_id="test", client_secret="test")
        self.oeffnen_fehler = oeffnen_fehler
        self.geoeffnet: list[str | None] = []

    async def ventil_oeffnen(self, geraet_id, dauer_sekunden, valve_id=None):
        if self.oeffnen_fehler:
            raise ConnectionError("Netz weg (Attrappe)")
        self.geoeffnet.append(valve_id)

    async def ventil_schliessen(self, geraet_id, valve_id=None):
        return None


@pytest.fixture
async def echte_sicherung(tmp_path, monkeypatch, wartende_sequenz):
    """Arbiter + echte VentilSicherung (temporaere DB, Attrappen-Client).
    bambuswald wartet kritisch, waldblumenhain ist nicht kritisch."""
    monkeypatch.setattr(mod, "datetime", _FesteUhr)
    speicher = Speicher(str(tmp_path / "t0618.db"))
    await speicher.verbinden()
    arb = _arbiter({WARTEND: True, FRAGER: False})
    erzeugt: list[VentilSicherung] = []

    def baue(client):
        sicherung = VentilSicherung(
            client=client, speicher=speicher, ventil_geraet_id=GERAET,
            kanal_zu_valve_id={K1: "valve-k1", K2: "valve-k2"},
        )
        arb.registriere_sicherung(GERAET, sicherung)
        sicherung.setze_hahn_arbiter(arb)
        erzeugt.append(sicherung)
        return sicherung

    try:
        yield arb, baue
    finally:
        for sicherung in erzeugt:
            for a in sicherung._aktiv.values():
                if a.timer_handle:
                    a.timer_handle.cancel()
        await speicher.schliessen()


@pytest.mark.asyncio
async def test_manueller_start_verdraengt_wie_vor_t0618(echte_sicherung):
    """Gate W5 B1: Andres manueller Start im Fenster laeuft wie vorher, auch
    gegen eine kritisch wartende Sequenz (Grundsatz T-0612)."""
    arb, baue = echte_sicherung
    client = _ClientOhneNetz()
    sicherung = baue(client)

    ok = await sicherung.bewaessere(K1, list(FRAGER), 600, Ausloser.MANUELL)

    assert ok is True
    assert client.geoeffnet == ["valve-k1"]
    # Echter Start -> die wartende Sequenz ist verdraengt (Karte).
    assert arb.verdraengung("bambuswald").durch == FRAGER


@pytest.mark.asyncio
async def test_automatik_start_wird_von_kritisch_wartender_abgewiesen(
    echte_sicherung,
):
    arb, baue = echte_sicherung
    client = _ClientOhneNetz()
    sicherung = baue(client)

    ok = await sicherung.bewaessere(K1, list(FRAGER), 600, Ausloser.AUTOMATIK)

    assert ok is False
    assert client.geoeffnet == []
    assert arb.verdraengung("bambuswald") is None


@pytest.mark.asyncio
async def test_preflight_ohne_start_vermerkt_keine_verdraengung(echte_sicherung):
    """Gate W5 B2: der Pre-flight der Start-Routen (danach z. B. Budget-
    Abweisung T-0444) darf die Karte nicht auf "verdraengt" stellen."""
    arb, baue = echte_sicherung
    sicherung = baue(_ClientOhneNetz())

    lock = await sicherung.pruefe_hahn_lock(K1, ausloser=Ausloser.MANUELL)

    assert lock.erlaubt is True
    assert lock.verdraengt   # er WUERDE verdraengen ...
    assert arb.verdraengung("bambuswald") is None   # ... hat es aber nicht


@pytest.mark.asyncio
async def test_gescheitertes_oeffnen_vermerkt_keine_verdraengung(
    echte_sicherung,
):
    """Gate W5 B2: Cloud-Fehler beim Oeffnen -> kein Start, kein Vermerk."""
    arb, baue = echte_sicherung
    sicherung = baue(_ClientOhneNetz(oeffnen_fehler=True))

    ok = await sicherung.bewaessere(K1, list(FRAGER), 600, Ausloser.MANUELL)

    assert ok is False
    assert arb.verdraengung("bambuswald") is None


# --- Kritisch-Begriff des Loops ---------------------------------------------


def _motor(werte: dict[str, float | None], zonen: list[ZonenKonfig]):
    motor = object.__new__(Entscheidungsmotor)
    motor._zonen = {z.zone_id: z for z in zonen}

    async def _robuste_feuchte(zone_id, jetzt):
        return werte.get(zone_id)
    motor._robuste_feuchte = _robuste_feuchte
    return motor


def _zk(zone_id, kritisch, ausschluss=False, regime=None, scharf=True):
    """`scharf` = Auto-Loop-Zone (`ist_auto_loop_zone`), sonst Monitoring."""
    return ZonenKonfig(
        zone_id=zone_id, name=zone_id, ventil_kanal=K2,
        feuchte_kritisch=kritisch, kanal_trigger_ausschluss=ausschluss,
        feuchte_regime=regime or [],
        modus=ZonenModus.AUTOMATIK if scharf else ZonenModus.MONITORING,
        auto_loop_opt_in=scharf,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("werte", "erwartet"),
    [
        # Realfall 22.09.: yogaraum 45 bei kritisch 50 -> Kanal kritisch.
        ({"bambuswald": 60.0, "bambuswald_yogaraum": 45.0}, True),
        ({"bambuswald": 60.0, "bambuswald_yogaraum": 55.0}, False),
        # Ausgeschlossener Trigger-Sensor (T-0337) zaehlt nicht mit.
        ({"bambuswald": 10.0, "bambuswald_yogaraum": 55.0}, False),
        # Kein gueltiger Messwert der Trigger-Zone: kein Urteil.
        ({"bambuswald": 10.0, "bambuswald_yogaraum": None}, None),
    ],
    ids=["yogaraum_kritisch", "keiner_kritisch", "ausschluss_zaehlt_nicht",
         "kein_messwert"],
)
async def test_kanal_kritisch_wie_der_loop(werte, erwartet):
    zonen = [
        _zk("bambuswald", 20.0, ausschluss=True),
        _zk("bambuswald_yogaraum", 50.0),
    ]
    motor = _motor(werte, zonen)

    assert await motor.kanal_kritisch(WARTEND, IM_FENSTER) is erwartet


@pytest.mark.asyncio
async def test_kanal_kritisch_nur_mit_scharfen_loop_zonen():
    """Gate W5 B3: wie `_baue_auto_loop_kanaele` -- eine Monitoring-Zone am
    selben Kanal macht ihn nicht kritisch."""
    motor = _motor(
        {"bambuswald": 10.0, "bambuswald_yogaraum": 55.0},
        [_zk("bambuswald", 20.0, scharf=False), _zk("bambuswald_yogaraum", 50.0)],
    )

    assert await motor.kanal_kritisch(WARTEND, IM_FENSTER) is False


@pytest.mark.asyncio
async def test_kanal_kritisch_nutzt_die_regime_schwelle():
    """T-0560-Klasse: `effektiv_feuchte_kritisch`, nicht der Basiswert."""
    regime = [FeuchteRegime(
        name="sommer", von_mm_dd="06-01", bis_mm_dd="08-31", feuchte_kritisch=30.0,
    )]
    motor = _motor(
        {"bambuswald_yogaraum": 40.0},
        [_zk("bambuswald_yogaraum", 50.0, regime=regime)],
    )

    assert await motor.kanal_kritisch(
        ("bambuswald_yogaraum",), IM_FENSTER,
    ) is False


# --- Karte: "verdraengt" von der Arbiter-Entscheidung bis zur Statusroute ----


class _PreSoakSicherung:
    """Hauptdose scheitert (Hahn belegt); Verdraengung kommt vom Arbiter."""

    def __init__(self, arbiter):
        self._arbiter = arbiter

    async def bewaessere(self, kanal, zone_ids, dauer_s, ausloser,
                         *, lauf_gruppe=None, phase=None) -> bool:
        return phase == "pre_soak"

    async def stoppe(self, kanal, ausloser) -> bool:
        return True

    def ist_aktiv(self, kanal) -> bool:
        return False

    def hahn_verdraengung(self, zone_id):
        return self._arbiter.verdraengung(zone_id)


async def _verdraengte_sequenz(monkeypatch, wartend_kritisch):
    async def _pre_soak(speicher, jetzt=None):
        return {"bambuswald"}
    monkeypatch.setattr(mod, "laufende_pre_soak_zonen", _pre_soak)
    arb = _arbiter({WARTEND: wartend_kritisch, FRAGER: False})
    mgr = PreSoakManager(_PreSoakSicherung(arb))  # type: ignore[arg-type]
    ok, fehler = await mgr.starte(
        "bambuswald", K2, list(WARTEND), pre_soak_min=5, pause_min=25,
        haupt_min=30, ausloser=Ausloser.AUTOMATIK,
    )
    assert ok, fehler
    lauf = mgr.laufender_lauf("bambuswald")
    lauf.gestartet_am = IM_FENSTER - timedelta(minutes=10)
    arb.vermerke_verdraengung(
        await arb.pruefe(GERAET, K1, jetzt=IM_FENSTER, ausloser=AUTO),
    )
    monkeypatch.setattr(api_server._laufzeit, "pre_soak_manager", mgr)
    monkeypatch.setattr(api_server._laufzeit, "pre_soak_managers", {})
    return mgr, lauf


@pytest.mark.asyncio
async def test_statusroute_zeigt_verdraengung_und_endgrund(monkeypatch):
    mgr, lauf = await _verdraengte_sequenz(monkeypatch, wartend_kritisch=False)

    antwort = await api_server.ventil_pre_soak_status(zone_id="bambuswald_yogaraum")
    (eintrag,) = antwort["laeufe"]
    assert eintrag["fremd"] is True
    assert eintrag["verdraengt"]["durch"] == list(FRAGER)

    # Hauptdose bleibt aus (Hahn belegt), Nachzug-Fenster laeuft ab: der
    # Endgrund nennt die Verdraengung statt "Tick/Sleep > Toleranz".
    await mgr.tick(lauf.gestartet_am + timedelta(minutes=25 + 21))
    assert lauf.phase == "fehler"
    assert lauf.fehler.startswith(HAUPTDOSE_VERDRAENGT), lauf.fehler
    assert "waldblumenhain" in lauf.fehler


@pytest.mark.asyncio
async def test_statusroute_ohne_verdraengung_bei_kritischem_vorrang(monkeypatch):
    """Gegenstueck: die kritische Sequenz behielt den Hahn -> nichts zu zeigen."""
    _, _ = await _verdraengte_sequenz(monkeypatch, wartend_kritisch=True)

    antwort = await api_server.ventil_pre_soak_status(zone_id="bambuswald")
    (eintrag,) = antwort["laeufe"]
    assert eintrag["verdraengt"] is None


@pytest.mark.asyncio
async def test_alte_verdraengung_gehoert_nicht_zur_neuen_sequenz():
    """Eine Verdraengung VOR dem Start dieser Sequenz betraf eine fruehere."""
    alt = mod.Verdraengung(seit=IM_FENSTER - timedelta(minutes=1), durch=FRAGER)
    sicherung = SimpleNamespace(hahn_verdraengung=lambda zone_id: alt)
    mgr = PreSoakManager(sicherung)  # type: ignore[arg-type]
    lauf = SimpleNamespace(
        zone_id="bambuswald", phase="pause", gestartet_am=IM_FENSTER,
    )

    assert mgr.verdraengung_fuer(lauf) is None  # type: ignore[arg-type]
    lauf.gestartet_am = IM_FENSTER - timedelta(minutes=2)
    assert mgr.verdraengung_fuer(lauf) is not None  # type: ignore[arg-type]


# T-0610 AT-3 (14.10.2026): die Verdrahtung in main.ausfuehren prueft jetzt ein
# echter Start-Lauf, test_t0610_at11_start_lauf.py::test_arbiter_bekommt_die_pruefer_des_motors (vorher AST-Test).
