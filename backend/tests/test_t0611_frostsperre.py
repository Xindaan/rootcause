"""T-0611: Frostsperre fuer die Auto-Schleife (Andres Entscheid 29.09.2026).

Schwelle 2 °C (stuendliche Vorhersage), kein Durchbruch bei kritischer
Trockenheit, Fenster = laufende Stunde bis Zyklusende + 1 h, fehlende
Vorhersage sperrt. Manuelle Starts bleiben offen und bekommen einen Hinweis.

Geprueft wird an den Grenzen, an denen die Sperre wirkt:
1. reine Fensterbewertung (`bewerte_frost`, `zyklus_dauer_s`),
2. jedes automatische Oeffnen ueber `VentilSicherung.bewaessere` (Pre-Soak-
   Hauptdose nach der Pause, Wiederaufnahme nach Neustart; der Auto-Loop-
   Einzellauf laeuft durch dieselbe Methode),
3. der Entscheidungsmotor: kritische Zone + 1 °C -> kein Giessen, Grund im Log,
4. Konfig-Whitelist und Hinweis beim manuellen Start.

Die Testkoerper sind harmlos: Attrappen-Client, temporaere DB, kein Netz.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import bewaesserung.api_server as api_server
from bewaesserung.entscheidung import Entscheidungsmotor
from bewaesserung.frostsperre import FrostPruefer, bewerte_frost, zyklus_dauer_s
from bewaesserung.konfig import lade_konfig
from bewaesserung.modelle import (
    Ausloser,
    BlockerTyp,
    GardenaKonfig,
    GesamtKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    WetterStunde,
    WetterVorhersage,
    ZonenKonfig,
    ZonenModus,
)
from test_entscheidung import (
    WetterClientAttrappe,
    baue_kanal_motor,
    baue_kanal_zonen,
    baue_messung,
)
from test_konfig_gesamt_whitelist import _BASIS
from test_t0612_kanal_sperre import (  # noqa: F401  (speicher = Fixture)
    BAMBUS_ZONEN,
    K_BAMBUS,
    VALVES,
    _dienst,
    _geoeffnete_valves,
    _raeume,
    _starte_auto_sequenz,
    speicher,
)
from test_ventil_sicherung import ClientAttrappe as _ClientAttrappe

TAG = datetime(2026, 10, 20)


def _stunden(temps: dict[int, float], tag: datetime = TAG, warm: float = 8.0):
    """48 Stunden ab `tag` 00:00, `warm` ausser in den genannten Stunden."""
    return [
        WetterStunde(zeitstempel=tag + timedelta(hours=h), temperatur=temps.get(h, warm))
        for h in range(48)
    ]


# --- 1. Fensterbewertung ----------------------------------------------------


@pytest.mark.parametrize(
    ("jetzt_h", "zyklus_min", "temps", "ohne", "gesperrt", "grund"),
    [
        # laufende Stunde kalt, obwohl "jetzt" schon 40 min drin ist
        (6.67, 30, {6: 1.4}, (), True, "Frostsperre: 1,4 °C um 06:00"),
        # kalt erst im Nachlauf (Zyklusende 14:30 + 1 h)
        (14.0, 30, {15: 1.9}, (), True, "Frostsperre: 1,9 °C um 15:00"),
        # kalte Nacht nach dem Fenster sperrt den Nachmittagslauf NICHT
        (14.0, 30, {h: -3.0 for h in range(18, 30)}, (), False, None),
        # Schwelle exklusiv: genau 2,0 °C sperrt nicht
        (6.0, 30, {6: 2.0, 7: 2.0}, (), False, None),
        # Zeitumstellung im Fruehjahr: 02:00 fehlt (Luecke 2 h) -> erlaubt
        (1.5, 60, {}, (2,), False, None),
        # Luecke von 3 h -> fail-safe
        (1.5, 120, {}, (2, 3), True, "Frostsperre: Vorhersage-Luecke nach 01:00 (fail-safe)"),
        # laufende Stunde fehlt -> fail-safe
        (6.5, 30, {}, (6,), True, "Frostsperre: keine Vorhersage fuer 06:00 (fail-safe)"),
    ],
    ids=[
        "laufende_stunde", "nachlauf", "kalte_nacht_danach", "schwelle_exklusiv",
        "dst_luecke_2h", "luecke_3h", "laufende_stunde_fehlt",
    ],
)
def test_bewerte_frost_fenster(jetzt_h, zyklus_min, temps, ohne, gesperrt, grund):
    stunden = [
        s for s in _stunden(temps) if s.zeitstempel.hour not in ohne
        or s.zeitstempel.day != TAG.day
    ]
    jetzt = TAG + timedelta(hours=jetzt_h)
    befund = bewerte_frost(stunden, jetzt, zyklus_min * 60, 2.0)
    assert befund.gesperrt is gesperrt, befund
    assert befund.grund == grund


def test_bewerte_frost_ohne_vorhersage_sperrt():
    befund = bewerte_frost([], TAG.replace(hour=6), 1800, 2.0)
    assert befund.gesperrt is True
    assert "fail-safe" in befund.grund


def test_bewerte_frost_vorhersage_endet_vor_dem_fensterende():
    stunden = _stunden({})[:8]  # 00:00..07:00
    befund = bewerte_frost(stunden, TAG.replace(hour=6), 3600, 2.0)
    assert befund.gesperrt is True
    assert befund.grund == "Frostsperre: keine Vorhersage bis 08:00 (fail-safe)"


def test_zyklus_dauer_umfasst_die_ganze_pre_soak_sequenz():
    """Der Auto-Loop faehrt fuer eine Pre-Soak-Zone Puls + Pause + Hauptdose;
    das Frostfenster des Start-Entscheids muss bis zur Hauptdose reichen."""
    basis = dict(zone_id="z", name="z", modus=ZonenModus.AUTOMATIK, ventil_kanal=1)
    mit = ZonenKonfig(**basis, pre_soak_modus="immer", pre_soak_min=5, pre_soak_pause_min=30)
    ohne = ZonenKonfig(**basis)
    assert zyklus_dauer_s(mit, 3600) == 30 * 60 + 3600
    assert zyklus_dauer_s(ohne, 3600) == 3600


# --- 2. Jedes automatische Oeffnen (VentilSicherung) ------------------------


class _Uhr:
    def __init__(self, jetzt: datetime):
        self.jetzt = jetzt

    def __call__(self) -> datetime:
        return self.jetzt


def _pruefer(temps: dict[int, float], uhr: _Uhr) -> FrostPruefer:
    client = WetterClientAttrappe(
        WetterVorhersage(abfrage_zeitstempel=TAG, stunden=_stunden(temps)),
    )
    return FrostPruefer(lambda _zid: client, 2.0, jetzt_func=uhr)


@pytest.mark.parametrize(
    "ausloser", [Ausloser.AUTOMATIK, Ausloser.MANUELL],
    ids=["automatik_gesperrt", "manuell_bleibt_offen"],
)
async def test_hauptdose_nach_der_pause_oeffnet_nicht_unter_frost(speicher, ausloser):
    """Puls um 06:10 bei 8 °C, ab 08:00 sagt die Vorhersage 1 °C. Die
    Hauptdose (06:41, 60 min, Fenster bis 08:41) darf fuer die Automatik
    nicht mehr oeffnen; eine manuelle Sequenz giesst weiter."""
    client = _ClientAttrappe()
    _, sicherung, mgr = _dienst(speicher, client)
    uhr = _Uhr(TAG.replace(hour=6, minute=10))
    sicherung.setze_frost_pruefer(_pruefer({8: 1.0, 9: 1.0}, uhr))
    try:
        t0 = await _starte_auto_sequenz(mgr, ausloser=ausloser)
        assert _geoeffnete_valves(client) == [VALVES[K_BAMBUS]]  # Puls lief
        assert await sicherung.stoppe(K_BAMBUS, ausloser)
        vorher = len(client.oeffnen_aufrufe)

        uhr.jetzt = TAG.replace(hour=6, minute=41)
        await mgr.tick(t0 + timedelta(minutes=31))

        neu = _geoeffnete_valves(client, vorher)
        if ausloser == Ausloser.AUTOMATIK:
            assert neu == []
            assert not sicherung.ist_aktiv(K_BAMBUS)
        else:
            assert neu == [VALVES[K_BAMBUS]]
    finally:
        _raeume(sicherung)


@pytest.mark.parametrize(
    ("kalt", "erwartet_offen"), [(True, False), (False, True)],
    ids=["frost_gesperrt", "warm_oeffnet"],
)
async def test_wiederaufnahme_nach_neustart_oeffnet_nicht_unter_frost(
    speicher, kalt, erwartet_offen,
):
    """Persistierte Automatik-Sequenz in der Pause, Hauptdose faellig. Nach
    dem Neustart zieht `recover_aus_db` sie nach -- unter Frost nicht."""
    client = _ClientAttrappe()
    _, sicherung, mgr = _dienst(speicher, client)
    uhr = _Uhr(TAG.replace(hour=6, minute=41))
    sicherung.setze_frost_pruefer(_pruefer({7: 1.0} if kalt else {}, uhr))
    await speicher.setze_pre_soak_state(
        zone_id="bambuswald", kanal=K_BAMBUS, zone_ids_kanal=list(BAMBUS_ZONEN),
        pre_soak_s=300, pause_s=1800, haupt_s=3600,
        gestartet_am=datetime.now() - timedelta(minutes=31), phase="pause",
        ausloser=Ausloser.AUTOMATIK.value, haupt_pulse=1, haupt_pause_s=0,
        haupt_pulse_gestartet=0,
    )
    try:
        await mgr.recover_aus_db()
        offen = _geoeffnete_valves(client) == [VALVES[K_BAMBUS]]
        assert offen is erwartet_offen, client.oeffnen_aufrufe
    finally:
        _raeume(sicherung)


async def test_pruefer_fehler_sperrt_die_automatik(speicher):
    """Fail-closed: wirft der Pruefer, oeffnet die Automatik nicht."""
    client = _ClientAttrappe()
    _, sicherung, _ = _dienst(speicher, client)

    class _Kaputt:
        async def pruefe(self, *a, **kw):
            raise RuntimeError("kaputt")

    sicherung.setze_frost_pruefer(_Kaputt())
    try:
        ok = await sicherung.bewaessere(
            K_BAMBUS, list(BAMBUS_ZONEN), 600, Ausloser.AUTOMATIK,
        )
        assert ok is False
        assert client.oeffnen_aufrufe == []
    finally:
        _raeume(sicherung)


# --- 3. Entscheidungsmotor: Frost steht ueber dem Kritisch-Bypass -----------


def _kalter_kanal_motor(temp: float, stunden: list | None = None):
    """Kanal mit kritisch trockener Zone (15 % < kritisch 20) um 12:00,
    AUSSERHALB der bevorzugten Zeit 05-07 -- giessen kaeme nur ueber den
    Kritisch-Bypass zustande. `stunden` ersetzt die Vorhersage."""
    jetzt = TAG.replace(hour=12)
    zone_a, zone_b = baue_kanal_zonen()
    motor, speicher_attr = baue_kanal_motor(zone_a, zone_b, 15.0, 15.0)
    for z in (zone_a, zone_b):
        speicher_attr.messungen[z.zone_id] = [baue_messung(jetzt, 15.0, z.zone_id)]
    motor._jetzt = lambda: jetzt
    client = WetterClientAttrappe(
        WetterVorhersage(
            abfrage_zeitstempel=jetzt,
            stunden=_stunden({}, warm=temp) if stunden is None else stunden,
        ),
    )
    motor._wetter_manager = SimpleNamespace(
        standard_client=client, hole_client=lambda _sid: client,
    )
    motor.setze_frost_pruefer(FrostPruefer(motor._wetter_fuer_zone, 2.0))
    return motor, [zone_a, zone_b]


@pytest.mark.parametrize(
    ("temp", "soll"), [(1.0, False), (8.0, True)],
    ids=["frost_sperrt_trotz_kritisch", "warm_kritisch_giesst"],
)
async def test_kanal_kritisch_und_frost_kein_giessen(temp, soll):
    motor, zonen = _kalter_kanal_motor(temp)
    e = await motor.pruefe_kanal(1, zonen)
    assert e.soll_bewaessern is soll, e.begruendung
    if soll:
        assert e.blocker_typ is None
    else:
        assert e.blocker_typ == BlockerTyp.FROSTSPERRE
        assert "Frostsperre: 1,0 °C um 12:00" in e.begruendung


@pytest.mark.parametrize("pfad", ["pruefe_zone", "vorhersage_zone"])
async def test_zonenpfade_zeigen_die_sperre(pfad):
    """Log (pruefe_zone) und Dashboard (vorhersage_zone) widersprechen dem
    Kanal-Entscheid nicht: dieselbe Sperre mit demselben Grund."""
    motor, zonen = _kalter_kanal_motor(1.0)
    e = await getattr(motor, pfad)(zonen[0].zone_id)
    assert e.soll_bewaessern is False
    assert e.blocker_typ == BlockerTyp.FROSTSPERRE
    text = getattr(e, "begruendung", None) or getattr(e, "grund", "")
    assert "Frostsperre: 1,0 °C um 12:00" in text


def test_motor_baut_den_pruefer_aus_der_konfig():
    basis = dict(
        gardena=GardenaKonfig(client_id="t", client_secret="t"), zonen=[],
        wetter=WetterKonfig(standorte=[WetterStandortKonfig(id="o", breite=52.5, laenge=13.4)]),
    )
    wm = SimpleNamespace(standard_client=None, hole_client=lambda _s: None)
    an = Entscheidungsmotor(None, wm, [], konfig=GesamtKonfig(**basis, frostsperre_schwelle_c=3.0))
    aus = Entscheidungsmotor(None, wm, [], konfig=GesamtKonfig(**basis, frostsperre_aktiv=False))
    assert an.frost_pruefer is not None and an.frost_pruefer.schwelle_c == 3.0
    assert aus.frost_pruefer is None


# --- 4. Konfig und manueller Start ------------------------------------------


def test_frostsperre_konfig_kommt_aus_der_yaml(tmp_path):
    pfad = tmp_path / "k.yaml"
    pfad.write_text(_BASIS + "frostsperre_aktiv: false\nfrostsperre_schwelle_c: 3.5\n")
    k = lade_konfig(pfad)
    assert (k.frostsperre_aktiv, k.frostsperre_schwelle_c) == (False, 3.5)
    pfad.write_text(_BASIS)
    k = lade_konfig(pfad)
    assert (k.frostsperre_aktiv, k.frostsperre_schwelle_c) == (True, 2.0)


async def test_manueller_start_unter_frost_giesst_mit_hinweis(speicher, monkeypatch):
    client = _ClientAttrappe()
    _, sicherung, _ = _dienst(speicher, client)
    uhr = _Uhr(TAG.replace(hour=6, minute=10))
    pruefer = _pruefer({6: 0.5}, uhr)
    sicherung.setze_frost_pruefer(pruefer)
    monkeypatch.setattr(api_server._laufzeit, "motor", SimpleNamespace(frost_pruefer=pruefer))
    try:
        antwort = await api_server.ventil_manuell_start(
            {"zone_id": "bambuswald", "dauer_sekunden": 120},
        )
        assert antwort["ok"] is True, antwort
        assert _geoeffnete_valves(client) == [VALVES[K_BAMBUS]]
        assert antwort["frost_hinweis"]["grund"] == "Frostsperre: 0,5 °C um 06:00"
    finally:
        _raeume(sicherung)


# --- Gate W1 B4: Fail-safe nur Oktober bis April, Sperre meldet sich laut ---


@pytest.mark.parametrize(
    ("monat", "gesperrt"), [(4, True), (5, False), (9, False), (10, True)],
    ids=["april", "mai", "september", "oktober"],
)
def test_failsafe_ohne_vorhersage_nur_oktober_bis_april(monat, gesperrt):
    """Andres Entscheid: eine fehlende Vorhersage sperrt nur von Oktober bis
    April. Beide Seiten tragen `vorhersage_fehlt` (fuer UI und Push)."""
    befund = bewerte_frost([], datetime(2026, monat, 15, 6, 30), 1800, 2.0)
    assert befund.gesperrt is gesperrt, befund
    assert befund.vorhersage_fehlt is True


@pytest.mark.parametrize("monat", [5, 10], ids=["mai", "oktober"])
def test_frost_in_vorhandenen_stunden_sperrt_auch_bei_luecke(monat):
    """Spaetfrost im Mai sperrt trotz Luecke; und der Grund ist dann Frost,
    nicht die fehlende Vorhersage (laufende Stunde 06:00 fehlt, 07:00 kalt)."""
    tag = datetime(2026, monat, 15)
    stunden = [s for s in _stunden({7: 1.0}, tag=tag) if s.zeitstempel.hour != 6]
    befund = bewerte_frost(stunden, tag.replace(hour=6, minute=30), 1800, 2.0)
    assert befund.gesperrt is True
    assert befund.vorhersage_fehlt is False
    assert befund.grund == "Frostsperre: 1,0 °C um 07:00"


class _WetterKaputt:
    async def hole_vorhersage(self):
        raise RuntimeError("Open-Meteo nicht erreichbar (Attrappe)")


@pytest.mark.parametrize(
    ("monat", "gesperrt"), [(7, False), (10, True)], ids=["juli", "oktober"],
)
async def test_pruefer_ohne_vorhersage_sperrt_nur_im_winterhalbjahr(monat, gesperrt):
    pruefer = FrostPruefer(lambda _zid: _WetterKaputt(), 2.0)
    befund = await pruefer.pruefe(["z"], 1800, jetzt=datetime(2026, monat, 15, 6))
    assert befund.gesperrt is gesperrt
    assert befund.vorhersage_fehlt is True


async def test_fehlende_vorhersage_hat_eigenen_blocker_und_kein_frost_label():
    """Oktober, Vorhersage leer: der Kanal ist gesperrt, aber als
    FROST_VORHERSAGE_FEHLT -- UI-Label ohne "Frost"."""
    motor, zonen = _kalter_kanal_motor(8.0, stunden=[])
    e = await motor.pruefe_kanal(1, zonen)
    assert e.soll_bewaessern is False
    assert e.blocker_typ == BlockerTyp.FROST_VORHERSAGE_FEHLT
    label = api_server._blocker_label(e.blocker_typ.value)
    assert "vorhersage fehlt" in label.lower() and "Frost" not in label


def _watchdog(speicher, bote, pruefer):
    from bewaesserung.modelle import WatchdogKonfig
    from bewaesserung.watchdog import WatchdogJob

    return WatchdogJob(
        speicher=speicher, benachrichtiger=bote,
        konfig=WatchdogKonfig(
            aktiv=True, empfaenger="+490000", intervall_minuten=1,
            throttle_stunden=1,   # Trigger drosselt fest auf 24 h
        ),
        zonen=[], gardena_zone_ids=[],
        motor=SimpleNamespace(frost_pruefer=pruefer),
    )


class _Bote:
    def __init__(self):
        self.gesendet: list[str] = []

    async def sende_text(self, empfaenger, text):
        self.gesendet.append(text)
        return True


@pytest.mark.parametrize(
    ("stunden", "erwartet"),
    [
        (_stunden({6: 1.4, 30: 1.4}), "Frostsperre aktiv: Frost 1,4 °C um 06:00"),
        ([], "Frostsperre aktiv: Vorhersage fehlt"),
    ],
    ids=["frost", "vorhersage_fehlt"],
)
async def test_frostsperre_meldet_sich_gedrosselt_per_push(speicher, stunden, erwartet):
    client = WetterClientAttrappe(WetterVorhersage(abfrage_zeitstempel=TAG, stunden=stunden))
    pruefer = FrostPruefer(lambda _zid: client, 2.0)
    bote = _Bote()
    job = _watchdog(speicher, bote, pruefer)
    t = TAG.replace(hour=6, minute=10)

    assert (await pruefer.pruefe(["z"], 1800, jetzt=t)).gesperrt
    await job.pruefe_und_sende_wenn_faellig(jetzt=t + timedelta(minutes=5))
    assert len(bote.gesendet) == 1
    assert erwartet in bote.gesendet[0]
    assert "Manuelles Gießen bleibt möglich" in bote.gesendet[0]
    if not stunden:
        assert "°C" not in bote.gesendet[0]   # keine erfundene Temperatur

    t2 = t + timedelta(hours=23)              # Sperre besteht weiter
    await pruefer.pruefe(["z"], 1800, jetzt=t2)
    await job.pruefe_und_sende_wenn_faellig(jetzt=t2 + timedelta(minutes=5))
    assert len(bote.gesendet) == 1            # 24-h-Drossel

    t3 = t + timedelta(hours=24, minutes=10)
    await pruefer.pruefe(["z"], 1800, jetzt=t3)
    await job.pruefe_und_sende_wenn_faellig(jetzt=t3 + timedelta(minutes=5))
    assert len(bote.gesendet) == 2            # naechster Tag


@pytest.mark.parametrize("fall", ["geraeumt", "veraltet"])
async def test_frostsperre_push_schweigt_wenn_die_sperre_vorbei_ist(speicher, fall):
    """Ein freier Befund fuer dieselbe Zonenmenge raeumt die Sperre; eine
    Sperre, die seit ueber 2 h nicht mehr festgestellt wurde, ist vorbei."""
    kalt = WetterVorhersage(abfrage_zeitstempel=TAG, stunden=_stunden({6: 1.4}))
    client = WetterClientAttrappe(kalt)
    pruefer = FrostPruefer(lambda _zid: client, 2.0)
    bote = _Bote()
    job = _watchdog(speicher, bote, pruefer)
    t = TAG.replace(hour=6, minute=10)
    assert (await pruefer.pruefe(["z"], 1800, jetzt=t)).gesperrt
    if fall == "geraeumt":
        client.vorhersage = WetterVorhersage(abfrage_zeitstempel=TAG, stunden=_stunden({}))
        assert not (await pruefer.pruefe(["z"], 1800, jetzt=t)).gesperrt
        tick = t + timedelta(minutes=5)
    else:
        tick = t + timedelta(hours=2, minutes=5)
    await job.pruefe_und_sende_wenn_faellig(jetzt=tick)
    assert bote.gesendet == []



# --- Gate W1 B8/B9: Multi-Standort und fehlende Temperatur -------------------


async def test_failsafe_eines_zweiten_standorts_geht_im_min_nicht_verloren():
    """Gate W1 B8: Zonen eines Kanals an zwei Standorten. Der erste ist warm,
    der zweite hat keine Vorhersage (Oktober: fail-safe). Ohne das
    Sofort-Return verloere der fail-safe-Befund (ohne Temperatur) das min()
    gegen den warmen Standort, und der Kanal waere frei."""
    warm = WetterClientAttrappe(WetterVorhersage(abfrage_zeitstempel=TAG, stunden=_stunden({})))
    leer = WetterClientAttrappe(WetterVorhersage(abfrage_zeitstempel=TAG, stunden=[]))
    pruefer = FrostPruefer({"a": warm, "b": leer}.__getitem__, 2.0)
    befund = await pruefer.pruefe(["a", "b"], 1800, jetzt=TAG.replace(hour=6))
    assert befund.gesperrt is True
    assert befund.vorhersage_fehlt is True


def test_open_meteo_null_temperatur_bleibt_none():
    """Gate W1 B9: ein null-Wert von Open-Meteo wird nicht still zu 0.0."""
    from bewaesserung.wetter import WetterClient

    daten = {"hourly": {
        "time": ["2026-10-20T06:00", "2026-10-20T07:00"],
        "temperature_2m": [None, 5.0],
    }}
    stunden = WetterClient(52.5, 13.4)._parse_stunden(daten)
    assert [s.temperatur for s in stunden] == [None, 5.0]


def test_stunde_ohne_temperatur_ist_unvollstaendig_nicht_null_grad():
    """Gate W1 B9: fehlt die Temperatur der laufenden Stunde, ist das eine
    unvollstaendige Vorhersage (Oktober: fail-safe), keine "0,0 °C"."""
    stunden = [
        WetterStunde(zeitstempel=s.zeitstempel, temperatur=None)
        if s.zeitstempel.hour == 6 and s.zeitstempel.day == TAG.day else s
        for s in _stunden({})
    ]
    befund = bewerte_frost(stunden, TAG.replace(hour=6, minute=30), 1800, 2.0)
    assert befund.gesperrt is True
    assert befund.vorhersage_fehlt is True
    assert "0,0" not in (befund.grund or "")


async def test_wetter_ereignisse_ueberspringen_stunden_ohne_temperatur(speicher):
    """Gate W1 B9 (Isomorphie): die Frost-/Hitze-Erkennung vergleicht nur
    Stunden mit Temperatur; None darf weder werfen noch Frost ausloesen."""
    from bewaesserung.wetter_ereignisse import pruefe_wetter_ereignisse

    jetzt = datetime.now().replace(minute=0, second=0, microsecond=0)
    vorhersage = WetterVorhersage(abfrage_zeitstempel=jetzt, stunden=[
        WetterStunde(zeitstempel=jetzt + timedelta(hours=h), temperatur=None)
        for h in range(1, 12)
    ])
    assert await pruefe_wetter_ereignisse(vorhersage, speicher, "o") == []
