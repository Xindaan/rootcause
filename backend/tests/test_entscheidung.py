from datetime import datetime, timedelta

import pytest

# Einige Tests pruefen die REALE `config/default.yaml` (Smoke-Test der echten
# Anlage). Die Datei ist bewusst nicht Teil des oeffentlichen Ablegers -- dort
# liegt nur `config/default.example.yaml`. Sie tragen deshalb `@pytest.mark.live_config`
# (Mechanik in `backend/conftest.py`): im Snapshot uebersprungen, privat
# unveraendert.


from bewaesserung.entscheidung import (
    Entscheidungsmotor,
    _kanal_dose_ziel,
    _robuster_aktuellwert,
)
from bewaesserung.modelle import (
    Ausloser,
    BewaesserungsEntscheidung,
    BewaesserungsStrategie,
    BlockerTyp,
    EntscheidungsScope,
    FeuchteRegime,
    SchwellenAdaptionKonfig,
    SensorMessung,
    SensorWarnung,
    SensorWarnungTyp,
    VentilAktion,
    VentilEreignis,
    WetterStunde,
    WetterVorhersage,
    ZeitFenster,
    ZonenKonfig,
    ZonenModus,
)

JETZT = datetime(2026, 4, 6, 6, 0)


class SpeicherAttrappe:
    def __init__(self):
        self.messungen: dict[str, list[SensorMessung]] = {}
        self.letzte_ereignisse: dict[str, VentilEreignis] = {}
        self.heutige_ereignisse: dict[str, list[VentilEreignis]] = {}
        self.gespeicherte_entscheidungen = []
        # H-1: pro Zone Liste offener SensorWarnung-Objekte (Default leer).
        self.offene_warnungen: dict[str, list[SensorWarnung]] = {}
        # T-0443: persistierter Zustand laufender Bewaesserungen. Default
        # leer = kein Kanal aktiv, damit Bestandstests unveraendert bleiben.
        self.live_lauf_states: list[dict] = []
        # T-0446: zweite Zustandstabelle -- waehrend der Soak-Pause ist das
        # Ventil zu, die Sequenz laeuft aber weiter.
        self.pre_soak_states: list[dict] = []
        # T-0445: ANKUNFTszeit je Zone (empfangen_am), nicht Messzeit.
        # Leer = fuer keine Zone ist je ein Wert eingetroffen.
        self.ankuenfte: dict[str, datetime] = {}
        self.geoeffnete_warnungen: list[SensorWarnung] = []

    async def max_feuchte_im_fenster(
        self, zone_id, quelle, vor, stunden=24,
    ):
        """T-0502 (Korrektur 10.08.): modelliert die echte Abfrage samt Filtern.

        Bewusst nicht "gib irgendeinen Wert zurueck": Quelle, striktes VOR und
        die Fensterbreite entscheiden ueber das Guard-Verhalten. Ein Mock, der
        sie ignoriert, testet am Vertrag vorbei
        (fehlerpattern_mock_ignoriert_query_filter).
        """
        grenze = vor - timedelta(hours=stunden)
        werte = [
            m.boden_feuchte for m in self.messungen.get(zone_id, [])
            if m.boden_feuchte is not None
            and getattr(m.quelle, "value", str(m.quelle)) == quelle
            and grenze <= m.zeitstempel < vor
        ]
        return max(werte) if werte else None

    async def letzter_positiver_feuchtewert(
        self, zone_id, quelle, vor, max_tage=14,
    ):
        """T-0502: modelliert die echte Abfrage samt ihrer Filter.

        Bewusst nicht "gib irgendeinen Wert zurueck": die Filter (Quelle,
        strikt VOR dem Zeitpunkt, nur Werte > 0, Fenster) entscheiden ueber
        das Guard-Verhalten. Ein Mock, der sie ignoriert, testet am
        eigentlichen Vertrag vorbei (fehlerpattern_mock_ignoriert_query_filter).
        """
        grenze = vor - timedelta(days=max_tage)
        passend = [
            m for m in self.messungen.get(zone_id, [])
            if m.boden_feuchte is not None
            and m.boden_feuchte > 0
            and grenze <= m.zeitstempel < vor
            and getattr(m.quelle, "value", str(m.quelle)) == quelle
        ]
        if not passend:
            return None
        return float(max(passend, key=lambda m: m.zeitstempel).boden_feuchte)

    async def letzte_ankunft_feuchte(self, zone_id: str) -> datetime | None:
        return self.ankuenfte.get(zone_id)

    async def oeffne_sensor_warnung(self, warnung: SensorWarnung) -> bool:
        self.geoeffnete_warnungen.append(warnung)
        return True

    async def hole_live_lauf_states(
        self, geraet_id: str | None = None,
    ) -> list[dict]:
        if geraet_id is None:
            return list(self.live_lauf_states)
        return [
            s for s in self.live_lauf_states if s.get("geraet_id") == geraet_id
        ]

    async def hole_pre_soak_states(self) -> list[dict]:
        return list(self.pre_soak_states)

    async def offene_sensor_warnungen(
        self, zone_id: str | None = None
    ) -> list[SensorWarnung]:
        if zone_id is None:
            return [w for liste in self.offene_warnungen.values() for w in liste]
        return list(self.offene_warnungen.get(zone_id, []))

    async def hole_messungen(self, zone_id, von=None, bis=None) -> list[SensorMessung]:
        messungen = self.messungen.get(zone_id, [])
        gefiltert = []
        for messung in messungen:
            if von and messung.zeitstempel < von:
                continue
            if bis and messung.zeitstempel > bis:
                continue
            gefiltert.append(messung)
        return sorted(gefiltert, key=lambda messung: messung.zeitstempel, reverse=True)

    async def letzte_messung(self, zone_id) -> SensorMessung | None:
        messungen = await self.hole_messungen(zone_id)
        return messungen[0] if messungen else None

    async def letzte_messung_aggregiert(
        self, zone_id, fenster_minuten=90, jetzt=None,
    ) -> SensorMessung | None:
        """T-0179c: Mock identisch zu letzte_messung (Tests nutzen
        Einzel-Sensor pro Zone; Aggregat ist nicht im Test-Fokus).
        Echte Aggregations-/Lead-Logik wird in test_speicher_multisensor.py
        gegen die echte Speicher-Klasse getestet.

        T-0383: respektiert `fenster_minuten` -- genau an diesem Fenster hing
        der stille Sensor-Dropout (Beat altert raus, None ohne Log).
        """
        messung = await self.letzte_messung(zone_id)
        if messung is None:
            return None
        bezug = jetzt or messung.zeitstempel
        if bezug - messung.zeitstempel > timedelta(minutes=fenster_minuten):
            return None
        return messung

    async def letzte_messungen_pro_geraet(
        self, zone_id, fenster_minuten=90, jetzt=None,
    ) -> list[SensorMessung]:
        msg = await self.letzte_messung(zone_id)
        return [msg] if msg else []

    async def letztes_ventil_ereignis(self, zone_id) -> VentilEreignis | None:
        return self.letzte_ereignisse.get(zone_id)

    async def hole_ventil_ereignisse(self, zone_id, von=None, bis=None):
        """Analog zur echten Speicher.hole_ventil_ereignisse; ASC nach Zeitstempel."""
        letztes = self.letzte_ereignisse.get(zone_id)
        heutige = self.heutige_ereignisse.get(zone_id, [])
        # Uniqueness bewahren via ID falls vorhanden, sonst via Zeitstempel
        alle: list[VentilEreignis] = list(heutige)
        if letztes is not None and letztes not in alle:
            alle.append(letztes)
        if von is not None:
            alle = [e for e in alle if e.zeitstempel >= von]
        if bis is not None:
            alle = [e for e in alle if e.zeitstempel <= bis]
        return sorted(alle, key=lambda e: e.zeitstempel)

    async def letztes_bestaetigtes_ventil_ereignis(
        self, zone_id: str, ausloser_ausser=None,
    ) -> VentilEreignis | None:
        """Mock fuer den Speicher-Helper: akzeptiert Einzel-Ausloser oder
        Iterable (T-0114)."""
        kandidaten: list[VentilEreignis] = []
        letztes = self.letzte_ereignisse.get(zone_id)
        if letztes is not None:
            kandidaten.append(letztes)
        kandidaten.extend(self.heutige_ereignisse.get(zone_id, []))
        if ausloser_ausser is not None:
            if isinstance(ausloser_ausser, Ausloser):
                ausgeschlossen = {ausloser_ausser}
            else:
                ausgeschlossen = set(ausloser_ausser)
            kandidaten = [
                e for e in kandidaten if e.ausloser not in ausgeschlossen
            ]
        if not kandidaten:
            return None
        kandidaten.sort(key=lambda e: e.zeitstempel, reverse=True)
        return kandidaten[0]

    async def letzter_bestaetigter_wasser_anker(
        self, zone_id: str, ausloser_ausser=None,
    ) -> VentilEreignis | None:
        kandidaten: list[VentilEreignis] = []
        letztes = self.letzte_ereignisse.get(zone_id)
        if letztes is not None:
            kandidaten.append(letztes)
        kandidaten.extend(self.heutige_ereignisse.get(zone_id, []))
        if ausloser_ausser is not None:
            if isinstance(ausloser_ausser, Ausloser):
                ausgeschlossen = {ausloser_ausser}
            else:
                ausgeschlossen = set(ausloser_ausser)
            kandidaten = [
                e for e in kandidaten if e.ausloser not in ausgeschlossen
            ]
        gruppen_mit_haupt = {
            e.lauf_gruppe for e in kandidaten
            if e.lauf_gruppe and e.phase == "haupt"
        }
        kandidaten = [
            e for e in kandidaten
            if not (
                e.lauf_gruppe and e.phase == "pre_soak"
                and e.lauf_gruppe not in gruppen_mit_haupt
            )
        ]
        if not kandidaten:
            return None
        kandidaten.sort(key=lambda e: e.zeitstempel, reverse=True)
        return kandidaten[0]

    async def ventil_ereignisse_heute(self, zone_id) -> list[VentilEreignis]:
        return list(self.heutige_ereignisse.get(zone_id, []))

    async def speichere_entscheidung(self, entscheidung) -> None:
        self.gespeicherte_entscheidungen.append(entscheidung)

    async def hole_entscheidungen(self, zone_id=None, limit=50):
        # Liste nach Zone filtern, absteigend nach zeitstempel sortieren
        zutreffend = [
            e for e in self.gespeicherte_entscheidungen
            if zone_id is None or e.zone_id == zone_id
        ]
        zutreffend.sort(key=lambda e: e.zeitstempel, reverse=True)
        return zutreffend[:limit]

    async def speichere_wetter(self, abfrage_zeit, stunden) -> None:
        return None


class WetterClientAttrappe:
    def __init__(self, vorhersage: WetterVorhersage, regen_schwelle_mm: float = 2.0):
        self.vorhersage = vorhersage
        self.regen_schwelle_mm = regen_schwelle_mm

    async def hole_vorhersage(self) -> WetterVorhersage:
        return self.vorhersage


class WetterManagerAttrappe:
    """Minimale Attrappe fuer WetterManager (Multi-Standort-Interface)."""

    def __init__(self, client: WetterClientAttrappe):
        self._client = client

    @property
    def standard_client(self):
        return self._client

    def hole_client(self, standort_id: str):
        return self._client


@pytest.fixture
def zone_automatik() -> ZonenKonfig:
    return ZonenKonfig(
        zone_id="zone-1",
        name="Bambushecke",
        modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=35.0,
        feuchte_kritisch=20.0,
        max_dauer_sekunden=1800,
        min_pause_minuten=120,
        tages_budget_sekunden=120.0,
        bevorzugte_zeiten=[ZeitFenster(von="05:00", bis="07:00")],
    )


def test_kanal_dose_ziel_nutzt_strategieziel_mit_korridor_fallback():
    """T-0345: Der Kanal-Dosis-Helfer uebernimmt das Strategieziel aus
    entscheide_pro_zone. Ohne Verdict bleibt der T-0344-Korridor-Fallback."""
    korridor = ZonenKonfig(
        zone_id="hecke", name="Hecke", modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=32.0, feuchte_kritisch=22.0,
        optimum_feuchte_min=38.0, optimum_feuchte_max=50.0,
        bewaesserungs_strategie=BewaesserungsStrategie.KORRIDOR,
    )
    # KORRIDOR + opt_max -> durchdringend bis opt_max (50), statt eff. min (35.8)
    assert _kanal_dose_ziel(korridor, 35.8) == 50.0
    # opt_max <= effektive Schwelle (Hitze-Anhebung) -> kein Rueckschritt
    assert _kanal_dose_ziel(korridor, 55.0) == 55.0
    # KORRIDOR-Strategieziel darf die Trigger-Schwelle nicht unterschreiten.
    assert _kanal_dose_ziel(korridor, 35.8, strategie_ziel=25.0) == 35.8

    haeufig = ZonenKonfig(
        zone_id="bambus", name="Bambus", modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=60.0, feuchte_kritisch=50.0,
        bewaesserungs_strategie=BewaesserungsStrategie.HAEUFIG_KLEIN,
    )
    assert _kanal_dose_ziel(haeufig, 60.0, strategie_ziel=75.0) == 75.0
    # Ohne Verdict bleibt der sichere Schwellen-Fallback erhalten.
    assert _kanal_dose_ziel(haeufig, 60.0) == 60.0

    selten = ZonenKonfig(
        zone_id="wald", name="Wald", modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=38.0, feuchte_kritisch=25.0,
        optimum_feuchte_max=50.0,
        bewaesserungs_strategie=BewaesserungsStrategie.SELTEN_GROSS,
    )
    assert _kanal_dose_ziel(selten, 38.0, strategie_ziel=85.0) == 85.0

    niedrig = selten.model_copy(update={
        "zone_id": "sedum",
        "bewaesserungs_strategie": BewaesserungsStrategie.KONSTANT_NIEDRIG,
    })
    assert _kanal_dose_ziel(niedrig, 38.0, strategie_ziel=25.0) == 25.0


def test_berechne_dauer_nutzt_kalibrierte_hecke_rate():
    """T-0347: delta_pp_pro_minute steuert die Auto-Dose. Hecke-Rate 0.33 ->
    +20pp brauchen ~60min (empirisch aus 35 Real-Laeufen), NICHT 20min wie beim
    Default 1.0. _berechne_dauer ist rein (kein self-Zugriff) -> Dummy-self."""
    h = ZonenKonfig(
        zone_id="hecke", name="Hecke", modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=32.0, feuchte_kritisch=22.0,
        optimum_feuchte_max=50.0, max_dauer_sekunden=5400,
        delta_pp_pro_minute=0.33,
        bewaesserungs_strategie=BewaesserungsStrategie.KORRIDOR,
    )
    # Sensor 30 -> Ziel opt_max 50 -> delta 20 -> 20/0.33 ~ 60 min
    s = Entscheidungsmotor._berechne_dauer(None, h, 30.0, 0.0, ziel_schwelle=50.0)
    assert 3500 <= s <= 3700, f"erwartet ~3636s (60min), war {s}"
    # Gegenprobe: ohne Rate (Default 1.0) waere die Dose ~3x kuerzer
    h_default = ZonenKonfig(
        zone_id="x", name="x", modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=32.0, feuchte_kritisch=22.0,
        optimum_feuchte_max=50.0, max_dauer_sekunden=5400,
    )
    s_def = Entscheidungsmotor._berechne_dauer(None, h_default, 30.0, 0.0, ziel_schwelle=50.0)
    assert s_def < s, "Default-Rate (1.0) giesst systematisch kuerzer als kalibrierte 0.33"


def baue_vorhersage(
    regen_pro_stunde: float = 0.0,
    et0_pro_stunde: float = 0.0,
    anzahl_stunden: int = 48,
    regen_wahrscheinlichkeit: float = 0.0,
) -> WetterVorhersage:
    return WetterVorhersage(
        abfrage_zeitstempel=JETZT,
        stunden=[
            WetterStunde(
                zeitstempel=JETZT + timedelta(hours=index + 1),
                temperatur=18.0,
                niederschlag_mm=regen_pro_stunde,
                niederschlag_wahrscheinlichkeit=regen_wahrscheinlichkeit,
                wind_kmh=8.0,
                et0_mm=et0_pro_stunde,
            )
            for index in range(anzahl_stunden)
        ],
    )


def baue_messung(zeitstempel: datetime, feuchte: float, zone_id: str = "zone-1") -> SensorMessung:
    return SensorMessung(
        zeitstempel=zeitstempel,
        zone_id=zone_id,
        boden_feuchte=feuchte,
        boden_temperatur=14.0,
        umgebungs_temperatur=18.0,
        licht_intensitaet=1000.0,
        batterie_prozent=90.0,
    )


def baue_ventil_ereignis(
    zeitstempel: datetime,
    dauer_sekunden: int,
    zone_id: str = "zone-1",
) -> VentilEreignis:
    return VentilEreignis(
        zeitstempel=zeitstempel,
        zone_id=zone_id,
        ventil_id="ventil-1",
        aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=dauer_sekunden,
        ausloser=Ausloser.AUTOMATIK,
    )


def baue_motor(
    zone: ZonenKonfig,
    *,
    feuchte: float = 28.0,
    jetzt: datetime = JETZT,
    vorhersage: WetterVorhersage | None = None,
    heutige_ereignisse: list[VentilEreignis] | None = None,
    letztes_ereignis: VentilEreignis | None = None,
    messungen: list[SensorMessung] | None = None,
) -> tuple[Entscheidungsmotor, SpeicherAttrappe]:
    speicher = SpeicherAttrappe()
    speicher.messungen[zone.zone_id] = messungen or [baue_messung(jetzt, feuchte, zone.zone_id)]
    if heutige_ereignisse is not None:
        speicher.heutige_ereignisse[zone.zone_id] = heutige_ereignisse
    if letztes_ereignis is not None:
        speicher.letzte_ereignisse[zone.zone_id] = letztes_ereignis

    wetter_client = WetterClientAttrappe(vorhersage or baue_vorhersage())
    wetter_manager = WetterManagerAttrappe(wetter_client)
    motor = Entscheidungsmotor(speicher, wetter_manager, [zone])
    motor._jetzt = lambda: jetzt
    return motor, speicher


def baue_kanal_zonen() -> tuple[ZonenKonfig, ZonenKonfig]:
    zone_a = ZonenKonfig(
        zone_id="zone-a",
        name="Zone A",
        modus=ZonenModus.AUTOMATIK,
        ventil_kanal=1,
        feuchte_schwelle_min=35.0,
        feuchte_schwelle_max=65.0,
        feuchte_kritisch=20.0,
        max_dauer_sekunden=1800,
        min_pause_minuten=120,
        tages_budget_sekunden=1200.0,
        bevorzugte_zeiten=[ZeitFenster(von="05:00", bis="07:00")],
    )
    zone_b = zone_a.model_copy(update={"zone_id": "zone-b", "name": "Zone B"})
    return zone_a, zone_b


def baue_kanal_motor(
    zone_a: ZonenKonfig,
    zone_b: ZonenKonfig,
    feuchte_a: float,
    feuchte_b: float,
) -> tuple[Entscheidungsmotor, SpeicherAttrappe]:
    speicher = SpeicherAttrappe()
    speicher.messungen[zone_a.zone_id] = [
        baue_messung(JETZT, feuchte_a, zone_a.zone_id),
    ]
    speicher.messungen[zone_b.zone_id] = [
        baue_messung(JETZT, feuchte_b, zone_b.zone_id),
    ]
    wetter_manager = WetterManagerAttrappe(WetterClientAttrappe(baue_vorhersage()))
    motor = Entscheidungsmotor(speicher, wetter_manager, [zone_a, zone_b])
    motor._jetzt = lambda: JETZT
    return motor, speicher


@pytest.mark.asyncio
async def test_feuchte_unter_schwelle_und_kein_regen_bewaessert(zone_automatik):
    motor, speicher = baue_motor(zone_automatik, feuchte=28.0, vorhersage=baue_vorhersage())

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is True
    assert entscheidung.dauer_sekunden == 420
    assert entscheidung.naechste_pruefung == JETZT + timedelta(minutes=5)
    assert "Feuchte 28%" in entscheidung.begruendung
    assert entscheidung.blocker_typ is None
    assert entscheidung.scope == EntscheidungsScope.ZONE
    assert entscheidung.scope_ref == zone_automatik.zone_id
    assert len(speicher.gespeicherte_entscheidungen) == 1


@pytest.mark.asyncio
async def test_feuchte_unter_schwelle_und_regen_erwartet_bewaessert_nicht(zone_automatik):
    """Regen, der die Zone ueber die Schwelle heben WUERDE, sperrt.

    T-0439 (27.07.): Der Regen wurde von 0,5 auf 1,5 mm/h angehoben, damit die
    urspruengliche Zusicherung erhalten bleibt. Frueher genuegten 3 mm, weil
    das Gate ein reiner mm-Schalter war (>= 2 mm -> sperren, egal wie trocken
    die Zone ist). Jetzt entscheidet die erwartete WIRKUNG: 9 mm x 4 pp/mm
    ueber der 2-mm-Schwelle = 28 pp, damit 28 -> 56 weit ueber Schwelle 35.
    Der Fall "Regen zu schwach, um zu helfen" steht als eigener Test darunter
    (`test_t0439_...`). Die Zusicherung ist nicht gebogen, sondern praezisiert.
    """
    motor, _ = baue_motor(
        zone_automatik,
        feuchte=28.0,
        vorhersage=baue_vorhersage(regen_pro_stunde=1.5),
    )

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.REGEN_ERWARTET
    assert "Regen" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_t0322_starkregen_wahrscheinlich_bewaessert_nicht(zone_automatik):
    """T-0322: Bei sehr hoher Regenwahrscheinlichkeit (>= Schwelle 80) NICHT
    giessen, auch wenn die mm-Prognose niedrig ist (open-meteo unterschaetzt
    Konvektion; Realfall 21.06.: 0.4mm/P68%, DWD warnte 15-30 l/m2)."""
    motor, _ = baue_motor(
        zone_automatik,
        feuchte=28.0,
        vorhersage=baue_vorhersage(
            regen_pro_stunde=0.1, regen_wahrscheinlichkeit=90.0,
        ),
    )

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.REGEN_ERWARTET
    assert "Starkregen" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_t0322_maessige_regenwahrscheinlichkeit_blockt_nicht(zone_automatik):
    """T-0322 Gegenprobe: maessige Wahrscheinlichkeit (< Schwelle) darf den
    Konvektions-Guard NICHT ausloesen -> kein Unter-Giessen bei dry-P-Spikes."""
    motor, _ = baue_motor(
        zone_automatik,
        feuchte=28.0,
        vorhersage=baue_vorhersage(
            regen_pro_stunde=0.0, regen_wahrscheinlichkeit=50.0,
        ),
    )

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert "Starkregen" not in entscheidung.begruendung


@pytest.mark.asyncio
async def test_ohne_gueltige_feuchtemessung_traegt_keine_messung_blocker(zone_automatik):
    ungueltige_messung = SensorMessung(
        zeitstempel=JETZT,
        zone_id=zone_automatik.zone_id,
        boden_feuchte=None,
        boden_temperatur=14.0,
        umgebungs_temperatur=18.0,
        licht_intensitaet=1000.0,
        batterie_prozent=90.0,
    )
    motor, _ = baue_motor(zone_automatik, messungen=[ungueltige_messung])

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.KEINE_MESSUNG
    assert entscheidung.scope == EntscheidungsScope.ZONE
    assert entscheidung.begruendung == "Keine gueltige Feuchtemessung vorhanden"


@pytest.mark.asyncio
async def test_robuste_feuchte_blockiert_zu_alten_fallback(zone_automatik):
    # T-0098: Letzte Messung 50 h alt -> kein Fallback mehr, KEINE_MESSUNG.
    alte_messung = baue_messung(
        JETZT - timedelta(hours=50), 28.0, zone_automatik.zone_id,
    )
    motor, _ = baue_motor(zone_automatik, messungen=[alte_messung])

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.KEINE_MESSUNG


@pytest.mark.asyncio
async def test_t0383_robuste_feuchte_akzeptiert_beat_knapp_ausserhalb_fensters(
    zone_automatik,
):
    """T-0383 (ersetzt `test_robuste_feuchte_akzeptiert_30h_fallback`): eine
    Messung knapp ausserhalb des 90-min-Aggregat-Fensters (Offline-/WS-Gap,
    Backfill spaeter) muss weiter entscheidungsfaehig sein -- vorher fiel sie
    still auf None."""
    alte_messung = baue_messung(
        JETZT - timedelta(minutes=100), 18.0, zone_automatik.zone_id,
    )
    motor, _ = baue_motor(zone_automatik, messungen=[alte_messung])

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.blocker_typ != BlockerTyp.KEINE_MESSUNG


@pytest.mark.asyncio
async def test_t0383_robuste_feuchte_lehnt_30h_alte_messung_ab(zone_automatik):
    """T-0383 -- bewusste Vertrags-KORREKTUR gegenueber T-0098.

    T-0098 erlaubte Fallback bis MAX_FALLBACK_ALTER_STUNDEN=48h. Das war seit
    T-0179c faktisch tot: `_robuste_feuchte` liest ueber
    `letzte_messung_aggregiert`, dessen 90-min-SQL-Fenster die Zeile laengst
    wegfilterte (der alte Test war nur gruen, weil die SpeicherAttrappe
    `fenster_minuten` ignorierte -- Mock-vs-Realitaet-Drift).

    Der 48h-Vertrag stammt zudem aus der Zeit VOR der Autonomie
    (`ventilsteuerung_aktiv` erst seit 26.06.2026). Eine tagealte Bodenfeuchte
    darf kein autonomes Ventil oeffnen. Neuer expliziter, geloggter Gate:
    AGGREGAT_FALLBACK_FENSTER_MIN (240 min; seit T-0476 in `speicher.py`,
    weil der Anzeige-Pfad denselben Horizont fuehrt)."""
    alte_messung = baue_messung(
        JETZT - timedelta(hours=30), 18.0, zone_automatik.zone_id,
    )
    motor, _ = baue_motor(zone_automatik, messungen=[alte_messung])

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.KEINE_MESSUNG


@pytest.mark.asyncio
async def test_festklemmender_sensor_blockiert_empfehlung(zone_automatik):
    # H-1: Sensor liefert frische Messung 28 % (unter Schwelle 35 %) — ohne
    # Festklemm-Schutz wuerde das Gardena-Polling jeden Tick "akut" empfehlen.
    # Eine offene SENSOR_EINGEFROREN-Warnung muss _robuste_feuchte zwingen,
    # None zurueckzugeben -> Empfehlung faellt auf KEINE_MESSUNG-Pfad statt
    # in die Akut-Spirale.
    motor, speicher = baue_motor(zone_automatik, feuchte=28.0)
    speicher.offene_warnungen[zone_automatik.zone_id] = [
        SensorWarnung(
            zeitstempel=JETZT - timedelta(hours=1),
            zone_id=zone_automatik.zone_id,
            typ=SensorWarnungTyp.SENSOR_EINGEFROREN,
            details="48 Messungen in 48h, Spanne 0.00%",
        )
    ]

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.KEINE_MESSUNG


@pytest.mark.asyncio
async def test_andere_offene_warnung_blockiert_empfehlung_nicht(zone_automatik):
    # H-1 Negativ-Kontrolle: andere Warnungstypen (Batterie, Ausfall,
    # BEWAESSERUNG_OHNE_WIRKUNG) duerfen die Empfehlung NICHT blockieren —
    # sonst wuerde z. B. ein Mai-Batterie-Tausch alle Bewaesserungen pausieren.
    motor, speicher = baue_motor(zone_automatik, feuchte=28.0)
    speicher.offene_warnungen[zone_automatik.zone_id] = [
        SensorWarnung(
            zeitstempel=JETZT - timedelta(hours=1),
            zone_id=zone_automatik.zone_id,
            typ=SensorWarnungTyp.BATTERIE_NIEDRIG,
            details="Batterie 18%",
        )
    ]

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.blocker_typ != BlockerTyp.KEINE_MESSUNG


@pytest.mark.asyncio
async def test_feuchte_unter_schwelle_ausserhalb_bevorzugter_zeit_bewaessert_nicht(zone_automatik):
    motor, _ = baue_motor(zone_automatik, feuchte=28.0, jetzt=datetime(2026, 4, 6, 12, 0))

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.ZEITFENSTER
    assert "ausserhalb bevorzugter Zeit" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_feuchte_unter_kritisch_ausserhalb_bevorzugter_zeit_bewaessert(zone_automatik):
    motor, _ = baue_motor(zone_automatik, feuchte=18.0, jetzt=datetime(2026, 4, 6, 12, 0))

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is True
    assert "kritischer Wert" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_tagesbudget_erschoepft_bewaessert_nicht(zone_automatik):
    ereignisse = [
        baue_ventil_ereignis(JETZT - timedelta(hours=3), 60),
        baue_ventil_ereignis(JETZT - timedelta(hours=1), 70),
    ]
    motor, _ = baue_motor(zone_automatik, feuchte=28.0, heutige_ereignisse=ereignisse)

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.BUDGET_ERSCHOEPFT
    assert "Tagesbudget erreicht" in entscheidung.begruendung


def _zone_mit_notreserve(faktor: float) -> ZonenKonfig:
    """T-0354: zone_automatik-Klon mit Kritisch-Notreserve-Faktor."""
    return ZonenKonfig(
        zone_id="zone-1",
        name="Hecke-Test",
        modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=35.0,
        feuchte_kritisch=20.0,
        max_dauer_sekunden=1800,
        min_pause_minuten=120,
        tages_budget_sekunden=120.0,
        tages_budget_kritisch_faktor=faktor,
        bevorzugte_zeiten=[ZeitFenster(von="05:00", bis="07:00")],
    )


@pytest.mark.asyncio
async def test_budget_kritisch_notreserve_giesst_im_reserve_band():
    """T-0354: Bei kritischer Trockenheit (<kritisch) darf bis faktor x proaktiv
    gegossen werden -- echter Durst stirbt nicht am proaktiven Cap."""
    zone = _zone_mit_notreserve(3.0)  # proaktiv 120s, Notreserve 360s
    # 200s Verbrauch: > proaktiv 120, < Notreserve 360; Ereignisse >2h alt -> Pause gehalten
    ereignisse = [
        baue_ventil_ereignis(JETZT - timedelta(hours=5), 100),
        baue_ventil_ereignis(JETZT - timedelta(hours=4), 100),
    ]
    motor, _ = baue_motor(zone, feuchte=15.0, heutige_ereignisse=ereignisse)  # 15 < kritisch 20

    entscheidung = await motor.pruefe_zone(zone.zone_id)

    assert entscheidung.soll_bewaessern is True
    assert entscheidung.blocker_typ != BlockerTyp.BUDGET_ERSCHOEPFT


@pytest.mark.asyncio
async def test_budget_kritisch_notreserve_gedeckelt():
    """T-0354: Auch kritisch wird ueber faktor x proaktiv hinaus geblockt --
    Runaway-Schutz bleibt (stuck-low-Sensor giesst nicht endlos)."""
    zone = _zone_mit_notreserve(3.0)  # Notreserve 360s
    ereignisse = [
        baue_ventil_ereignis(JETZT - timedelta(hours=5), 200),
        baue_ventil_ereignis(JETZT - timedelta(hours=4), 200),
    ]  # 400s > Notreserve 360
    motor, _ = baue_motor(zone, feuchte=15.0, heutige_ereignisse=ereignisse)

    entscheidung = await motor.pruefe_zone(zone.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.BUDGET_ERSCHOEPFT


@pytest.mark.asyncio
async def test_budget_default_faktor_kritisch_blockt_weiterhin():
    """T-0354: Default-Faktor 1.0 = unveraendert -- kritisch blockt weiterhin am
    proaktiven Budget (kein Regress fuer Zonen ohne gesetzten Faktor)."""
    zone = _zone_mit_notreserve(1.0)  # proaktiv 120, keine Reserve
    ereignisse = [
        baue_ventil_ereignis(JETZT - timedelta(hours=5), 80),
        baue_ventil_ereignis(JETZT - timedelta(hours=4), 80),
    ]  # 160s > 120
    motor, _ = baue_motor(zone, feuchte=15.0, heutige_ereignisse=ereignisse)

    entscheidung = await motor.pruefe_zone(zone.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.BUDGET_ERSCHOEPFT


@pytest.mark.asyncio
async def test_min_pause_nicht_eingehalten_bewaessert_nicht(zone_automatik):
    letztes_ereignis = baue_ventil_ereignis(JETZT - timedelta(minutes=30), 60)
    motor, _ = baue_motor(zone_automatik, feuchte=28.0, letztes_ereignis=letztes_ereignis)

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.PAUSE_AKTIV
    assert "Min-Pause" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_reiner_pre_soak_puls_ankert_min_pause_nicht(zone_automatik):
    """T-0363: Puls ohne Hauptdose darf den Retry nicht fuer volle Pause sperren."""
    puls = VentilEreignis(
        zeitstempel=JETZT - timedelta(minutes=30),
        zone_id=zone_automatik.zone_id,
        ventil_id="ventil-1",
        aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=300,
        ausloser=Ausloser.AUTOMATIK,
        lauf_gruppe="presoak-fehler",
        phase="pre_soak",
    )
    motor, _ = baue_motor(
        zone_automatik, feuchte=28.0, heutige_ereignisse=[puls],
    )

    eingehalten, verbleibend = await motor._pause_eingehalten(
        zone_automatik, JETZT, scope=EntscheidungsScope.KANAL,
    )

    assert eingehalten is True
    assert verbleibend == 0.0


@pytest.mark.asyncio
async def test_pre_soak_mit_hauptdose_ankert_min_pause(zone_automatik):
    gruppe = "presoak-ok"
    puls = VentilEreignis(
        zeitstempel=JETZT - timedelta(minutes=60),
        zone_id=zone_automatik.zone_id,
        ventil_id="ventil-1",
        aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=300,
        ausloser=Ausloser.AUTOMATIK,
        lauf_gruppe=gruppe,
        phase="pre_soak",
    )
    haupt = puls.model_copy(update={
        "zeitstempel": JETZT - timedelta(minutes=30),
        "dauer_sekunden": 1800,
        "phase": "haupt",
    })
    motor, _ = baue_motor(
        zone_automatik, feuchte=28.0, heutige_ereignisse=[puls, haupt],
    )

    eingehalten, verbleibend = await motor._pause_eingehalten(
        zone_automatik, JETZT, scope=EntscheidungsScope.KANAL,
    )

    assert eingehalten is False
    assert verbleibend > 0


@pytest.mark.asyncio
async def test_shadow_giess_empfehlung_sperrt_folgezyklus_fuer_min_pause(zone_automatik):
    # Kein echtes Ventil-Ereignis (Shadow-Mode). Stattdessen liegt 30 min vorher
    # schon eine soll_bewaessern=1-Entscheidung im Log. Das muss jetzt als
    # Pause-Anker wirken — sonst spammt das Warum-Panel jeden 5-min-Zyklus.
    motor, speicher = baue_motor(zone_automatik, feuchte=28.0)
    speicher.gespeicherte_entscheidungen.append(
        BewaesserungsEntscheidung(
            zeitstempel=JETZT - timedelta(minutes=30),
            zone_id=zone_automatik.zone_id,
            soll_bewaessern=True,
            dauer_sekunden=300,
            begruendung="Shadow-Empfehlung 30 min vorher",
            scope=EntscheidungsScope.ZONE,
            scope_ref=zone_automatik.zone_id,
        )
    )

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.PAUSE_AKTIV


@pytest.mark.asyncio
async def test_shadow_pause_ignoriert_andere_scope(zone_automatik):
    # Pause fuer scope=KANAL darf nicht durch eine scope=ZONE-Entscheidung
    # gesetzt werden (sonst blockieren sich Kanal- und Zone-Pfad gegenseitig).
    motor, speicher = baue_motor(zone_automatik, feuchte=28.0)
    speicher.gespeicherte_entscheidungen.append(
        BewaesserungsEntscheidung(
            zeitstempel=JETZT - timedelta(minutes=30),
            zone_id=zone_automatik.zone_id,
            soll_bewaessern=True,
            dauer_sekunden=300,
            begruendung="ZONE-Empfehlung — sollte KANAL-Pause nicht setzen",
            scope=EntscheidungsScope.ZONE,
            scope_ref=zone_automatik.zone_id,
        )
    )

    # Direkt die Hilfsfunktion testen: KANAL-Scope-Lookup findet keine
    # passende Giess-Entscheidung, Pause gilt als eingehalten.
    eingehalten, verbleibend = await motor._pause_eingehalten(
        zone_automatik, JETZT, scope=EntscheidungsScope.KANAL
    )
    assert eingehalten is True
    assert verbleibend == 0.0

    # Umgekehrt: ZONE-Scope findet sie und blockt.
    eingehalten, _ = await motor._pause_eingehalten(
        zone_automatik, JETZT, scope=EntscheidungsScope.ZONE
    )
    assert eingehalten is False


@pytest.mark.asyncio
async def test_t0340_scharfe_zone_kein_pause_anker_nach_fehlversuch(zone_automatik):
    """T-0340: Eine SCHARFE Zone (ventilsteuerung_aktiv + automatik +
    auto_loop_opt_in) darf NICHT durch eine soll=1-Entscheidung min_pause-
    geankert werden, die KEIN Wasser geliefert hat (bewaessere False, z.B.
    transienter 502 -> kein OEFFNEN). Sonst blockiert ein Fehlversuch den Retry
    stundenlang (Realfall 26.06. Hecke: 4h Block). Shadow-Zonen brauchen den
    Anker weiter (kein Event-Pfad)."""
    from types import SimpleNamespace

    scharf = zone_automatik.model_copy(update={"auto_loop_opt_in": True})
    motor, speicher = baue_motor(scharf, feuchte=28.0)
    # soll=1 KANAL-Entscheidung vor 30 min, ABER kein OEFFNEN (Fehlversuch).
    speicher.gespeicherte_entscheidungen.append(
        BewaesserungsEntscheidung(
            zeitstempel=JETZT - timedelta(minutes=30),
            zone_id=scharf.zone_id, soll_bewaessern=True, dauer_sekunden=300,
            begruendung="soll=1 aber bewaessere False -> kein Wasser geliefert",
            scope=EntscheidungsScope.KANAL, scope_ref="2",
        )
    )

    # SCHARF (Live): soll=1-Anker greift NICHT -> Pause frei -> Retry moeglich.
    motor._konfig = SimpleNamespace(ventilsteuerung_aktiv=True)
    eingehalten, verbleibend = await motor._pause_eingehalten(
        scharf, JETZT, scope=EntscheidungsScope.KANAL,
    )
    assert eingehalten is True, "scharfe Zone darf nach Fehlversuch nicht blocken"
    assert verbleibend == 0.0

    # SHADOW-Kontrolle (ventilsteuerung_aktiv=False): Anker greift -> blockiert.
    motor._konfig = SimpleNamespace(ventilsteuerung_aktiv=False)
    eingehalten_shadow, _ = await motor._pause_eingehalten(
        scharf, JETZT, scope=EntscheidungsScope.KANAL,
    )
    assert eingehalten_shadow is False, "Shadow-Zone behaelt den Anti-Spam-Anker"


@pytest.mark.asyncio
async def test_echtes_ventil_ereignis_dominiert_shadow_anker(zone_automatik):
    # Live-Mode-Invariante: sobald ein reales ventil_ereignis da ist,
    # definiert dessen Zeitstempel die Pause — auch wenn eine aeltere
    # Shadow-Empfehlung im Log liegt.
    motor, speicher = baue_motor(
        zone_automatik,
        feuchte=28.0,
        letztes_ereignis=baue_ventil_ereignis(JETZT - timedelta(minutes=10), 60),
    )
    speicher.gespeicherte_entscheidungen.append(
        BewaesserungsEntscheidung(
            zeitstempel=JETZT - timedelta(hours=3),  # aelter als min_pause
            zone_id=zone_automatik.zone_id,
            soll_bewaessern=True,
            dauer_sekunden=300,
            begruendung="alte Shadow-Empfehlung",
            scope=EntscheidungsScope.ZONE,
            scope_ref=zone_automatik.zone_id,
        )
    )

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    # 10 min < 120 min min_pause -> PAUSE_AKTIV anhand des Ventil-Events
    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.PAUSE_AKTIV


def test_robuster_wert_filtert_einzelnen_spike():
    # Aktuell=85 ist Ausreisser gegen Historie ~30
    wert, war_ausreisser = _robuster_aktuellwert([85.0, 30.0, 31.0, 30.0, 29.0])
    assert war_ausreisser is True
    # Median der Historie [30, 31, 30, 29] = 30
    assert wert == 30.0


def test_robuster_wert_ohne_ausreichend_historie_nimmt_rohwert():
    # Nur 2 Werte insgesamt -> Historie hat 1 Element -> kein Filter
    wert, war_ausreisser = _robuster_aktuellwert([42.0, 32.0])
    assert war_ausreisser is False
    assert wert == 42.0
    # 1 Wert -> trivialer Pass-Through
    wert, war_ausreisser = _robuster_aktuellwert([50.0])
    assert war_ausreisser is False
    assert wert == 50.0


def test_effektive_schwelle_ohne_hitze_keine_anhebung():
    motor, _ = baue_motor(ZonenKonfig(zone_id="z", name="z"))
    effektiv, anhebung = motor._effektive_schwelle(basis=35.0, et0_24h_mm=1.0)
    assert effektiv == 35.0
    assert anhebung == 0.0


def test_effektive_schwelle_hebt_bei_hitze():
    # median=2.5 default, k=1.0 -> ET0 5 mm/d heisst +2.5 %-Punkte
    motor, _ = baue_motor(ZonenKonfig(zone_id="z", name="z"))
    effektiv, anhebung = motor._effektive_schwelle(basis=35.0, et0_24h_mm=5.0)
    assert effektiv == 37.5
    assert anhebung == 2.5


def test_effektive_schwelle_respektiert_deckel():
    # ET0=100 wuerde theoretisch +97.5 geben, aber cap=10 bei Defaults
    motor, _ = baue_motor(ZonenKonfig(zone_id="z", name="z"))
    effektiv, anhebung = motor._effektive_schwelle(basis=35.0, et0_24h_mm=100.0)
    assert effektiv == 45.0
    assert anhebung == 10.0


def test_effektive_schwelle_abschaltbar():
    # schwellen_adaption.aktiv=false -> keine Anhebung, auch bei hoher ET0
    speicher = SpeicherAttrappe()
    speicher.messungen["z"] = [baue_messung(JETZT, 30.0, "z")]
    wetter_client = WetterClientAttrappe(baue_vorhersage())
    motor = Entscheidungsmotor(
        speicher, WetterManagerAttrappe(wetter_client),
        [ZonenKonfig(zone_id="z", name="z")],
        schwellen_adaption=SchwellenAdaptionKonfig(aktiv=False),
    )
    effektiv, anhebung = motor._effektive_schwelle(basis=35.0, et0_24h_mm=10.0)
    assert effektiv == 35.0
    assert anhebung == 0.0


@pytest.mark.asyncio
async def test_hitze_schwelle_erzwingt_mindestdauer_statt_null(zone_automatik):
    """Codex-Finding P1: Feuchte zwischen Basis und effektiver Schwelle ergab
    soll_bewaessern=True mit dauer=0 (VentilSicherung lehnt ab). Jetzt Mindestdauer."""
    # Basis-Schwelle 35, Hitze hebt auf 37.5 (ET0=5 mm/d, k=1.0, median=2.5).
    # Feuchte 36 => ueber basis, aber unter effektiv => giessen mit Mindestdauer.
    hitze = baue_vorhersage(et0_pro_stunde=5.0 / 24)
    motor, _ = baue_motor(zone_automatik, feuchte=36.0, vorhersage=hitze)

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)
    assert entscheidung.soll_bewaessern is True
    # Mindestens MIN_DAUER_SEKUNDEN, nicht 0
    assert entscheidung.dauer_sekunden >= 60


@pytest.mark.asyncio
async def test_unbekannt_events_blockieren_budget_und_pause_nicht(zone_automatik):
    """Codex-Finding P1: UNBEKANNT-Heuristik-Events duerfen Tagesbudget
    nicht belasten und keine Pause setzen — sie sind unsichere Kandidaten."""
    # Zone hat tages_budget=120s, max_dauer=1800s.
    # Ein unklassifiziertes Heuristik-Event mit 600s heute.
    unbekannt = baue_ventil_ereignis(JETZT - timedelta(hours=1), 600)
    unbekannt = unbekannt.model_copy(update={"ausloser": Ausloser.UNBEKANNT,
                                              "ventil_id": "sensor_heuristik"})
    motor, _ = baue_motor(
        zone_automatik, feuchte=28.0, heutige_ereignisse=[unbekannt],
    )
    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)
    # Budget 120s, UNBEKANNT-Event zaehlt nicht → giessen moeglich
    assert entscheidung.soll_bewaessern is True
    assert entscheidung.blocker_typ is None


@pytest.mark.asyncio
async def test_hitze_prognose_blockt_naeher_an_schwelle(zone_automatik):
    # Zone hat feuchte_schwelle_min=35. Ohne Hitze wuerde bei 36% FEUCHTE_OK greifen.
    # Mit Hitze (ET0=5 mm/24h -> +2.5%-Punkte Anhebung) ist die effektive Schwelle 37.5:
    # 36 < 37.5 -> NICHT mehr FEUCHTE_OK, Motor giesst (bevorzugte Zeit ist JETZT 06:00).
    hitze = baue_vorhersage(et0_pro_stunde=5.0 / 24)  # 5 mm/24h
    motor, _ = baue_motor(zone_automatik, feuchte=36.0, vorhersage=hitze)

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is True
    assert entscheidung.blocker_typ is None
    assert "+2" in entscheidung.begruendung  # Anhebungs-Anmerkung sichtbar
    assert "Hitze" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_ohne_hitze_feuchte_bei_schwelle_bleibt_blockiert(zone_automatik):
    # Gegenprobe: bei ET0=0 muss der Motor bei 36% (ueber Basis 35) weiter FEUCHTE_OK sagen.
    motor, _ = baue_motor(zone_automatik, feuchte=36.0)  # default vorhersage ET0=0

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.FEUCHTE_OK
    assert "Hitze" not in entscheidung.begruendung


def test_robuster_wert_meldet_keinen_alarm_bei_mad_null():
    # Historie vollstaendig identisch -> MAD=0, wir wollen KEINEN False-Positive
    # (sonst wuerden stabile Zonen bei kleinen Aenderungen staendig gefiltert)
    wert, war_ausreisser = _robuster_aktuellwert([50.0, 30.0, 30.0, 30.0, 30.0])
    assert war_ausreisser is False
    assert wert == 50.0


@pytest.mark.asyncio
async def test_spike_in_aktuellster_messung_wird_durchgereicht(zone_automatik):
    """T-0125 (Architektur-Wechsel 05.05.2026): Sensor = Ground Truth.

    Vorher: MAD-Filter glaettet den 85%-Spike weg, Motor nutzt Median 30%
    -> giesst trotz hohem Sensor-Wert.
    Jetzt: 85 % ist Realitaet (z. B. nach Regen, Bewaesserung, Schlauch).
    Sensor wird durchgereicht, FEUCHTE_OK-Blocker greift, kein Trigger.
    """
    messungen = [
        baue_messung(JETZT, 85.0),                         # Sensor jetzt
        baue_messung(JETZT - timedelta(hours=1), 30.0),
        baue_messung(JETZT - timedelta(hours=2), 31.0),
        baue_messung(JETZT - timedelta(hours=3), 30.0),
        baue_messung(JETZT - timedelta(hours=4), 29.0),
    ]
    motor, _ = baue_motor(zone_automatik, messungen=messungen)

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.FEUCHTE_OK
    # Begruendung nutzt den echten Sensor-Wert (85), nicht den Median (30)
    assert "Feuchte 85%" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_monitoring_zone_bewaessert_nie(zone_automatik):
    zone = zone_automatik.model_copy(update={"modus": ZonenModus.MONITORING})
    motor, _ = baue_motor(zone, feuchte=10.0)

    entscheidung = await motor.pruefe_zone(zone.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.begruendung == "Zone im Monitoring-Modus"


@pytest.mark.asyncio
async def test_t0271_vorhersage_zone_monitoring_loest_welkepunkt_auf(
    zone_automatik,
):
    """T-0271: auch im Monitoring-Modus muss `vorhersage_zone`
    `welkepunkt_wert` + `welkepunkt_quelle` befuellen -- vorher
    uebersprang der Stub-Branch `_hole_kalibrier_referenzen` komplett,
    sodass Frontend keinen Welkepunkt sah und die T-0269-Physik-
    Augmentation keine Prognose rechnen konnte.

    Zone hat `welkepunkt=20.0` (manueller Override) -> Erwartung:
    `welkepunkt_wert=20.0`, `welkepunkt_quelle="manuell"`,
    `soll_bewaessern=False` (Stub-Pfad unveraendert).
    """
    zone = zone_automatik.model_copy(update={
        "modus": ZonenModus.MONITORING,
        "welkepunkt": 20.0,
    })
    motor, _ = baue_motor(zone, feuchte=30.0)

    empfehlung = await motor.vorhersage_zone(zone.zone_id)

    # T-0182-Stub-Vertrag bleibt: keine Auto-Bewaesserung im Monitoring.
    assert empfehlung.soll_bewaessern is False
    assert empfehlung.ml_status_grund == "zone_monitoring"
    # T-0271-Erweiterung: Welkepunkt-Felder befuellt.
    assert empfehlung.welkepunkt_wert == 20.0
    assert empfehlung.welkepunkt_quelle == "manuell"


@pytest.mark.asyncio
async def test_t0271_monitoring_ohne_welkepunkt_konfig_liefert_quelle_keine(
    zone_automatik,
):
    """T-0271: wenn weder `welkepunkt`-Konfig noch genug Kalibrierungs-
    Datenpunkte vorliegen, durchlaeuft `_hole_kalibrier_referenzen` die
    Kaskade und landet im Fallback. `welkepunkt_quelle` darf nicht
    `None` sein (Default ist "keine"). Wert kann je nach Tagesmin-
    Datenlage 'tagesmin_schaetzung' oder 'feuchte_kritisch_fallback' werden.
    """
    zone = zone_automatik.model_copy(update={
        "modus": ZonenModus.MONITORING,
        "welkepunkt": None,
    })
    motor, _ = baue_motor(zone, feuchte=30.0)

    empfehlung = await motor.vorhersage_zone(zone.zone_id)

    assert empfehlung.soll_bewaessern is False
    # Quelle ist immer ein nicht-leerer String (kein None).
    assert isinstance(empfehlung.welkepunkt_quelle, str)
    assert empfehlung.welkepunkt_quelle != ""
    # Bei zone_automatik (feuchte_kritisch=20.0) MUSS spaetestens
    # `feuchte_kritisch_fallback` greifen -> welkepunkt_wert ist gesetzt.
    assert empfehlung.welkepunkt_wert is not None


@pytest.mark.asyncio
async def test_kanal_entscheidung_traegt_scope_und_scope_ref():
    zone_a, zone_b = baue_kanal_zonen()
    motor, _ = baue_kanal_motor(zone_a, zone_b, 28.0, 30.0)

    entscheidung = await motor.pruefe_kanal(1, [zone_a, zone_b])

    assert entscheidung.soll_bewaessern is True
    assert entscheidung.scope == EntscheidungsScope.KANAL
    assert entscheidung.scope_ref == "1"
    assert entscheidung.zone_id == zone_a.zone_id


@pytest.mark.asyncio
async def test_kanal_startet_wenn_trockenste_unter_schwelle_trotz_ok_durchschnitt():
    zone_a, zone_b = baue_kanal_zonen()
    motor, _ = baue_kanal_motor(zone_a, zone_b, 28.0, 48.0)

    entscheidung = await motor.pruefe_kanal(1, [zone_a, zone_b])

    assert entscheidung.soll_bewaessern is True
    assert entscheidung.dauer_sekunden == 420
    assert "trockenste Zone zone-a 28%" in entscheidung.begruendung
    assert "nasseste Zone zone-b 48%" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_kanal_trigger_ausschluss_fuehrt_auf_verlaesslichen_partner():
    """T-0337: Eine Zone mit kanal_trigger_ausschluss treibt den Min-Trigger
    NICHT. zone-a (28, unter Schwelle) ausgeschlossen -> die verlaessliche
    zone-b (50, ok) fuehrt -> kein Giessen (statt min->Giessen wie sonst)."""
    zone_a, zone_b = baue_kanal_zonen()
    zone_a = zone_a.model_copy(update={"kanal_trigger_ausschluss": True})
    motor, _ = baue_kanal_motor(zone_a, zone_b, 28.0, 50.0)

    entscheidung = await motor.pruefe_kanal(1, [zone_a, zone_b])

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.FEUCHTE_OK


@pytest.mark.asyncio
async def test_kanal_trigger_ausschluss_fallback_wenn_alle_ausgeschlossen():
    """T-0337: Sind ALLE Trigger-Zonen ausgeschlossen -> Fallback auf alle
    (kein Blindflug). zone-a 28 treibt dann wieder -> Giessen."""
    zone_a, zone_b = baue_kanal_zonen()
    zone_a = zone_a.model_copy(update={"kanal_trigger_ausschluss": True})
    zone_b = zone_b.model_copy(update={"kanal_trigger_ausschluss": True})
    motor, _ = baue_kanal_motor(zone_a, zone_b, 28.0, 50.0)

    entscheidung = await motor.pruefe_kanal(1, [zone_a, zone_b])

    assert entscheidung.soll_bewaessern is True


@pytest.mark.asyncio
async def test_t0382_dropout_verlaesslicher_sensor_kein_fallback_auf_ausgeschlossenen():
    """T-0382: Faellt der einzige NICHT-ausgeschlossene Sensor diesen Zyklus aus
    (kein valider Messwert), darf der Kanal NICHT auf den
    kanal_trigger_ausschluss-Sensor zurueckfallen und die (laengst nasse) Zone
    ueber-waessern. Realfall 06.07.: bambuswald/sensor-b (ausgeschlossen, 40%)
    uebernahm bei Yogaraum-Dropout -> 90-min-Lauf in die nasse Zone (Runaway,
    den T-0337 gerade verhindern soll). Ohne Fix: `or zonenwerte`-Fallback ->
    zone-a 28% < Schwelle -> soll=True."""
    zone_a, zone_b = baue_kanal_zonen()
    # zone_a = ausgeschlossener Artefakt-Sensor, UNTER Schwelle (wuerde giessen)
    zone_a = zone_a.model_copy(update={"kanal_trigger_ausschluss": True})
    speicher = SpeicherAttrappe()
    speicher.messungen[zone_a.zone_id] = [
        baue_messung(JETZT, 28.0, zone_a.zone_id),
    ]
    # zone_b = verlaesslicher Partner mit DROPOUT (kein Messwert diesen Zyklus):
    # messungen[zone_b] bleibt leer -> _robuste_feuchte gibt None.
    wetter_manager = WetterManagerAttrappe(WetterClientAttrappe(baue_vorhersage()))
    motor = Entscheidungsmotor(speicher, wetter_manager, [zone_a, zone_b])
    motor._jetzt = lambda: JETZT

    entscheidung = await motor.pruefe_kanal(1, [zone_a, zone_b])

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.KEINE_MESSUNG
    assert "kein verlaesslicher Trigger" in entscheidung.begruendung
    assert entscheidung.scope == EntscheidungsScope.KANAL


@pytest.mark.asyncio
async def test_t0383_beat_ausserhalb_aggregat_fenster_faellt_nicht_still_aus():
    """T-0383: Ein Beat knapp ausserhalb des 90-min-Aggregat-Fensters (Realfall:
    Offline/WS-Gap, Backfill traegt erst spaeter nach) darf die Zone NICHT still
    aus der Entscheidung kippen. Der Fallback-Horizont (240 min) nimmt denselben
    Sensor, nur aelter."""
    zone_a, _ = baue_kanal_zonen()
    speicher = SpeicherAttrappe()
    speicher.messungen[zone_a.zone_id] = [
        baue_messung(JETZT - timedelta(minutes=100), 50.0, zone_a.zone_id),
    ]
    wetter_manager = WetterManagerAttrappe(WetterClientAttrappe(baue_vorhersage()))
    motor = Entscheidungsmotor(speicher, wetter_manager, [zone_a])
    motor._jetzt = lambda: JETZT

    wert = await motor._robuste_feuchte(zone_a.zone_id, JETZT)

    assert wert == 50.0


@pytest.mark.asyncio
async def test_t0383_beat_jenseits_fallback_horizont_gibt_none():
    """T-0383: Jenseits des Fallback-Horizonts (240 min) bleibt es bei None --
    eine sehr alte Messung darf keine Giessentscheidung treiben."""
    zone_a, _ = baue_kanal_zonen()
    speicher = SpeicherAttrappe()
    speicher.messungen[zone_a.zone_id] = [
        baue_messung(JETZT - timedelta(minutes=300), 50.0, zone_a.zone_id),
    ]
    wetter_manager = WetterManagerAttrappe(WetterClientAttrappe(baue_vorhersage()))
    motor = Entscheidungsmotor(speicher, wetter_manager, [zone_a])
    motor._jetzt = lambda: JETZT

    assert await motor._robuste_feuchte(zone_a.zone_id, JETZT) is None


@pytest.mark.asyncio
async def test_t0383_verspaeteter_beat_haelt_kanal_trigger_beim_verlaesslichen_sensor():
    """T-0383 + T-0382 zusammen (Realfall 06.07.): der verlaessliche Partner
    (zone-b, nass 80) hat nur einen 100-min-alten Beat, der ausgeschlossene
    Artefakt-Sensor (zone-a, 28) einen frischen. Vor dem Fix fiel zone-b still
    raus -> zone-a triggerte -> Ueber-Giessen. Jetzt fuehrt zone-b weiter."""
    zone_a, zone_b = baue_kanal_zonen()
    zone_a = zone_a.model_copy(update={"kanal_trigger_ausschluss": True})
    speicher = SpeicherAttrappe()
    speicher.messungen[zone_a.zone_id] = [
        baue_messung(JETZT, 28.0, zone_a.zone_id),
    ]
    speicher.messungen[zone_b.zone_id] = [
        baue_messung(JETZT - timedelta(minutes=100), 80.0, zone_b.zone_id),
    ]
    wetter_manager = WetterManagerAttrappe(WetterClientAttrappe(baue_vorhersage()))
    motor = Entscheidungsmotor(speicher, wetter_manager, [zone_a, zone_b])
    motor._jetzt = lambda: JETZT

    entscheidung = await motor.pruefe_kanal(1, [zone_a, zone_b])

    assert entscheidung.soll_bewaessern is False
    assert "zone-b" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_kanal_start_erlaubt_bereits_nasse_zone_wenn_noch_inaktiv():
    zone_a, zone_b = baue_kanal_zonen()
    motor, _ = baue_kanal_motor(zone_a, zone_b, 28.0, 76.0)

    entscheidung = await motor.pruefe_kanal(1, [zone_a, zone_b])

    assert entscheidung.soll_bewaessern is True
    assert entscheidung.dauer_sekunden == 420
    assert "nasseste Zone zone-b 76%" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_kanal_startet_nicht_wenn_keine_zone_unter_effektiver_schwelle():
    zone_a, zone_b = baue_kanal_zonen()
    motor, _ = baue_kanal_motor(zone_a, zone_b, 36.0, 70.0)

    entscheidung = await motor.pruefe_kanal(1, [zone_a, zone_b])

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.FEUCHTE_OK
    assert "trockenste Zone zone-a 36%" in entscheidung.begruendung
    assert "nasseste Zone zone-b 70%" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_kanal_dauer_basiert_auf_trockenster_zone_nicht_durchschnitt():
    zone_a, zone_b = baue_kanal_zonen()
    motor, _ = baue_kanal_motor(zone_a, zone_b, 25.0, 65.0)

    entscheidung = await motor.pruefe_kanal(1, [zone_a, zone_b])

    assert entscheidung.soll_bewaessern is True
    assert entscheidung.dauer_sekunden == 600


@pytest.mark.asyncio
async def test_kanal_dauer_nutzt_groesstes_defizit_bei_unterschiedlichen_schwellen():
    zone_a, zone_b = baue_kanal_zonen()
    zone_b = zone_b.model_copy(update={
        "feuchte_schwelle_min": 60.0,
        "feuchte_schwelle_max": 75.0,
    })
    motor, _ = baue_kanal_motor(zone_a, zone_b, 30.0, 52.0)

    entscheidung = await motor.pruefe_kanal(1, [zone_a, zone_b])

    assert entscheidung.soll_bewaessern is True
    assert entscheidung.dauer_sekunden == 480
    assert "groesste Defizit-Zone zone-b 52%" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_t0345_kanal_dauer_nutzt_haeufig_klein_optimum_max():
    """Bambus-Regression: HAEUFIG_KLEIN dosiert bis optimum_max, nicht
    nur bis zur effektiven Min-Schwelle."""
    zone_a, zone_b = baue_kanal_zonen()
    zone_a = zone_a.model_copy(update={
        "bewaesserungs_strategie": BewaesserungsStrategie.HAEUFIG_KLEIN,
        "feuchte_schwelle_min": 60.0,
        "feuchte_kritisch": 50.0,
        "optimum_feuchte_min": 60.0,
        "optimum_feuchte_max": 75.0,
        "delta_pp_pro_minute": 1.0,
        "max_dauer_sekunden": 1800,
    })
    from bewaesserung.entscheidung_pro_zone import ProZoneAuswertung
    motor, _ = baue_kanal_motor(zone_a, zone_b, 59.0, 80.0)
    motor._strategie_verdict_pro_zone = lambda z, f, e, n, j: _async_return(
        ProZoneAuswertung(
            zone_id=z.zone_id,
            soll_bewaessern=True,
            empfehlungs_typ="praeventiv",
            ziel_feuchte=75.0,
            effektive_sicherheits_tage=1.0,
            aktive_strategie="haeufig_klein",
            grund="unter Wohl-Min.",
            ziel_feuchte_roh=75.0,
        )
    )

    entscheidung = await motor.pruefe_kanal(1, [zone_a, zone_b])

    assert entscheidung.soll_bewaessern is True
    assert entscheidung.dauer_sekunden == 960
    assert "Ziel 75%" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_kanal_max_stop_erkennt_nasseste_zone_ueber_stop_schwelle():
    zone_a, zone_b = baue_kanal_zonen()
    motor, _ = baue_kanal_motor(zone_a, zone_b, 30.0, 76.0)

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b],
    )

    assert soll_stoppen is True
    assert "Zone zone-b 76% ueber Stop-Schwelle 75%" in begruendung


@pytest.mark.asyncio
async def test_kanal_ohne_gueltige_feuchtemessung_traegt_keine_messung_blocker():
    zone_a, zone_b = baue_kanal_zonen()
    speicher = SpeicherAttrappe()
    wetter_manager = WetterManagerAttrappe(WetterClientAttrappe(baue_vorhersage()))
    motor = Entscheidungsmotor(speicher, wetter_manager, [zone_a, zone_b])
    motor._jetzt = lambda: JETZT

    entscheidung = await motor.pruefe_kanal(1, [zone_a, zone_b])

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.KEINE_MESSUNG
    assert entscheidung.scope == EntscheidungsScope.KANAL
    assert entscheidung.scope_ref == "1"


def test_dauer_berechnung_ist_proportional_zur_feuchte_differenz(zone_automatik):
    motor, _ = baue_motor(zone_automatik)

    kurze_dauer = motor._berechne_dauer(zone_automatik, aktuelle_feuchte=34.0, et0_6h=0.0)
    lange_dauer = motor._berechne_dauer(zone_automatik, aktuelle_feuchte=30.0, et0_6h=0.0)

    assert kurze_dauer == 60
    assert lange_dauer == 300


@pytest.mark.asyncio
async def test_predictive_watering_mit_fallendem_trend_liefert_zeitpunkt(zone_automatik):
    messungen = [
        baue_messung(JETZT - timedelta(hours=24), 60.0),
        baue_messung(JETZT - timedelta(hours=18), 54.0),
        baue_messung(JETZT - timedelta(hours=12), 48.0),
        baue_messung(JETZT - timedelta(hours=6), 42.0),
        baue_messung(JETZT, 36.0),
    ]
    leere_vorhersage = WetterVorhersage(abfrage_zeitstempel=JETZT, stunden=[])
    motor, _ = baue_motor(
        zone_automatik,
        feuchte=36.0,
        messungen=messungen,
        vorhersage=leere_vorhersage,
    )

    zeitpunkt, begruendung = await motor.prognostiziere_bewaesserung(zone_automatik.zone_id)

    assert zeitpunkt is not None
    assert zeitpunkt > JETZT
    assert "Schwelle" in begruendung


@pytest.mark.asyncio
async def test_predictive_watering_mit_stabilem_trend_liefert_none(zone_automatik):
    messungen = [
        baue_messung(JETZT - timedelta(hours=24), 50.0),
        baue_messung(JETZT - timedelta(hours=18), 50.0),
        baue_messung(JETZT - timedelta(hours=12), 50.0),
        baue_messung(JETZT - timedelta(hours=6), 50.0),
        baue_messung(JETZT, 50.0),
    ]
    leere_vorhersage = WetterVorhersage(abfrage_zeitstempel=JETZT, stunden=[])
    motor, _ = baue_motor(
        zone_automatik,
        feuchte=50.0,
        messungen=messungen,
        vorhersage=leere_vorhersage,
    )

    zeitpunkt, begruendung = await motor.prognostiziere_bewaesserung(zone_automatik.zone_id)

    assert zeitpunkt is None
    assert begruendung == "Feuchte stabil/steigend"


# T-0089: Pro-Zone-Wirkungsrate + Optimum-Max-Ziel ----------------------------


def test_berechne_dauer_default_bei_none():
    """delta_pp_pro_minute=None -> globaler Default 1.0 pp/min (Backward-Compat)."""
    zone = ZonenKonfig(
        zone_id="z", name="z", feuchte_schwelle_min=60.0,
        max_dauer_sekunden=3600, delta_pp_pro_minute=None,
    )
    motor, _ = baue_motor(zone, feuchte=50.0)
    # 10 pp / 1.0 = 600 s, ohne ET0-Aufschlag.
    assert motor._berechne_dauer(zone, aktuelle_feuchte=50.0, et0_6h=0.0) == 600


def test_berechne_dauer_nutzt_zone_delta_pp_pro_minute():
    """Zone-spezifische Wirkungsrate wirkt: 0.5 pp/min => doppelte Dauer."""
    zone = ZonenKonfig(
        zone_id="z", name="z", feuchte_schwelle_min=60.0,
        max_dauer_sekunden=3600, delta_pp_pro_minute=0.5,
    )
    motor, _ = baue_motor(zone, feuchte=50.0)
    # 10 pp / 0.5 = 1200 s.
    assert motor._berechne_dauer(zone, aktuelle_feuchte=50.0, et0_6h=0.0) == 1200


def test_berechne_dauer_clippt_pathologische_werte_auf_default():
    """delta_pp_pro_minute<=0 fallback auf Default (kein DivisionByZero)."""
    zone = ZonenKonfig(
        zone_id="z", name="z", feuchte_schwelle_min=60.0,
        max_dauer_sekunden=3600, delta_pp_pro_minute=0.0,
    )
    motor, _ = baue_motor(zone, feuchte=50.0)
    assert motor._berechne_dauer(zone, aktuelle_feuchte=50.0, et0_6h=0.0) == 600


def test_berechne_dauer_geclippt_auf_max_dauer():
    """Bambus-realistisch: 0.4 pp/min, 25 pp Differenz => 3750 s, geclippt auf 1800."""
    zone = ZonenKonfig(
        zone_id="z", name="z", feuchte_schwelle_min=75.0,
        max_dauer_sekunden=1800, delta_pp_pro_minute=0.4,
    )
    motor, _ = baue_motor(zone, feuchte=50.0)
    assert motor._berechne_dauer(zone, aktuelle_feuchte=50.0, et0_6h=0.0) == 1800


def test_berechne_dauer_log_decay_korrektur_verlaengert_lange_dosen():
    """T-0091a: bei alpha < 0 sinkt die effektive Wirkungsrate fuer lange
    Dauern - Empfehlung wird laenger, weil mehr Wasser verloren geht.

    Setup: 0.2 pp/min, 25 pp Differenz, max_dauer 10000s.
    Ohne Korrektur (alpha=0): 25/0.2 = 125 min = 7500 s.
    Mit alpha=-0.5: bei 125 min effektive Rate = 0.2 * (1 + -0.5*log(125/30))
                  = 0.2 * (1 - 0.5*1.43) = 0.2 * 0.286 = 0.057
                  Iteration 2: dauer = 25/0.057 = 439 min = 26340 s
                  Sanity-Cap 0.05 setzt Floor.
                  geclippt auf max_dauer 10000.
    Wichtig: lange_dauer >= ohne_korrektur (also nicht weniger).
    """
    zone_ohne = ZonenKonfig(
        zone_id="z", name="z", feuchte_schwelle_min=75.0,
        max_dauer_sekunden=20000, delta_pp_pro_minute=0.2,
        wirkungsrate_dauer_alpha=0.0,
    )
    zone_mit = zone_ohne.model_copy(update={"wirkungsrate_dauer_alpha": -0.5})
    motor_o, _ = baue_motor(zone_ohne, feuchte=50.0)
    motor_m, _ = baue_motor(zone_mit, feuchte=50.0)
    ohne = motor_o._berechne_dauer(zone_ohne, aktuelle_feuchte=50.0, et0_6h=0.0)
    mit = motor_m._berechne_dauer(zone_mit, aktuelle_feuchte=50.0, et0_6h=0.0)
    assert ohne == 7500
    # Mit Korrektur deutlich laenger (mind. 50 % mehr).
    assert mit > ohne * 1.5


def test_berechne_dauer_log_decay_kurze_dosen_unveraendert():
    """T-0091a: alpha-Korrektur greift erst ab d > 30 min. Kurze Dosen
    (< 30 min) bleiben unveraendert.

    Setup: 0.5 pp/min, 5 pp Differenz -> 5/0.5 = 10 min = 600 s. < 30 min.
    """
    zone = ZonenKonfig(
        zone_id="z", name="z", feuchte_schwelle_min=55.0,
        max_dauer_sekunden=3600, delta_pp_pro_minute=0.5,
        wirkungsrate_dauer_alpha=-0.5,
    )
    motor, _ = baue_motor(zone, feuchte=50.0)
    # Erwartet: 600 s, keine Aenderung weil Dauer-Schaetzung < 30 min
    assert motor._berechne_dauer(zone, aktuelle_feuchte=50.0, et0_6h=0.0) == 600


def test_dauer_fuer_sicherheitsabstand_zielt_auf_optimum_max_wenn_groesser():
    """T-0089 Schritt B: Reserve-Ziel kleiner als Optimum-Max -> Optimum gewinnt."""
    zone = ZonenKonfig(
        zone_id="z", name="z", feuchte_schwelle_min=60.0,
        max_dauer_sekunden=3600, delta_pp_pro_minute=1.0,
        optimum_feuchte_max=75.0,
    )
    motor, _ = baue_motor(zone, feuchte=55.0)
    # Reserve = 47 + 5 + 2*3 = 58 (kleiner als 75) -> Ziel ist 75.
    # Dauer = (75 - 55) / 1.0 * 60 = 1200 s.
    dauer = motor._dauer_fuer_sicherheitsabstand(
        zone, aktuelle_feuchte=55.0, welkepunkt=47.0,
        sicherheits_tage=3.0, decay_pp_pro_tag=2.0, et0_6h=0.0,
    )
    assert dauer == 1200


def test_dauer_fuer_sicherheitsabstand_nimmt_reserve_wenn_groesser_als_optimum():
    """Reserve-Ziel groesser als Optimum-Max -> Reserve gewinnt (defensiv)."""
    zone = ZonenKonfig(
        zone_id="z", name="z", feuchte_schwelle_min=40.0,
        max_dauer_sekunden=7200, delta_pp_pro_minute=1.0,
        optimum_feuchte_max=50.0,
    )
    motor, _ = baue_motor(zone, feuchte=20.0)
    # Reserve = 20 + 5 + 10*3 = 55 (groesser als optimum_max 50) -> Ziel 55.
    # Dauer = (55 - 20) / 1.0 * 60 = 2100 s.
    dauer = motor._dauer_fuer_sicherheitsabstand(
        zone, aktuelle_feuchte=20.0, welkepunkt=20.0,
        sicherheits_tage=3.0, decay_pp_pro_tag=10.0, et0_6h=0.0,
    )
    assert dauer == 2100


def test_dauer_fuer_sicherheitsabstand_kein_optimum_zielt_auf_reserve():
    """Ohne optimum_feuchte_max: altes Verhalten, nur Reserve."""
    zone = ZonenKonfig(
        zone_id="z", name="z", feuchte_schwelle_min=40.0,
        max_dauer_sekunden=3600, delta_pp_pro_minute=1.0,
        optimum_feuchte_max=None,
    )
    motor, _ = baue_motor(zone, feuchte=50.0)
    # Reserve = 47 + 5 + 2*3 = 58 -> ziel 58, Dauer = 8/1.0*60 = 480 s.
    dauer = motor._dauer_fuer_sicherheitsabstand(
        zone, aktuelle_feuchte=50.0, welkepunkt=47.0,
        sicherheits_tage=3.0, decay_pp_pro_tag=2.0, et0_6h=0.0,
    )
    assert dauer == 480


def test_berechne_dauer_plateau_modell_saturiert_bei_grossem_delta():
    """T-0091b: Plateau-Modell. Bei delta > wmax * 0.95 wird auf 95 %
    geclippt - System empfiehlt nicht absurde Dauern fuer unmoegliche Ziele.

    Setup: wmax=8, r0=0.5, tau=16. Differenz 15 pp (>> 8) -> Cap auf 7.6.
    d = -16 * log(1 - 7.6/8) = -16 * log(0.05) = 47.9 min.
    Plus ET0=0 -> dauer ~ 2880 s.
    """
    zone = ZonenKonfig(
        zone_id="z", name="z", feuchte_schwelle_min=75.0,
        max_dauer_sekunden=20000,
        wirkung_max_pp=8.0, wirkungsrate_initial=0.5,
    )
    motor, _ = baue_motor(zone, feuchte=60.0)
    dauer = motor._berechne_dauer(zone, aktuelle_feuchte=60.0, et0_6h=0.0)
    # ~48 min = 2880 s, Toleranz fuer Rundungen
    assert 2700 < dauer < 3000


def test_berechne_dauer_plateau_modell_kleine_differenz():
    """T-0091b: kleine Differenz innerhalb Plateau - moderate Dauer.

    Setup: wmax=8, r0=0.5, tau=16. Differenz 4 pp (-> 50 % vom Plateau).
    d = -16 * log(1 - 4/8) = -16 * log(0.5) = 11.1 min = 666 s.
    """
    zone = ZonenKonfig(
        zone_id="z", name="z", feuchte_schwelle_min=64.0,
        max_dauer_sekunden=20000,
        wirkung_max_pp=8.0, wirkungsrate_initial=0.5,
    )
    motor, _ = baue_motor(zone, feuchte=60.0)
    dauer = motor._berechne_dauer(zone, aktuelle_feuchte=60.0, et0_6h=0.0)
    # ~11 min = 666 s, Toleranz
    assert 600 < dauer < 750


def test_berechne_dauer_plateau_hat_vorrang_vor_alpha():
    """T-0091b: wenn Plateau-Werte gesetzt, ueberschreibt es alpha-Modell.

    Setup: delta_pp=0.3 -> Erst-Schaetzung 50 min (loest alpha-Korrektur
    aus). Mit alpha=-0.6 fuehrt das zu rohe Dauer ~100 min. Mit Plateau
    wmax=6 ist Soll-Wirkung 15 pp >> Plateau, geclippt auf 95 % von 6 = 5.7,
    Dauer = -20*log(0.05) = 60 min.
    """
    zone_alpha = ZonenKonfig(
        zone_id="z", name="z", feuchte_schwelle_min=75.0,
        max_dauer_sekunden=20000, delta_pp_pro_minute=0.3,
        wirkungsrate_dauer_alpha=-0.6,
    )
    zone_plateau = zone_alpha.model_copy(update={
        "wirkung_max_pp": 6.0,
        "wirkungsrate_initial": 0.3,
    })
    motor_a, _ = baue_motor(zone_alpha, feuchte=60.0)
    motor_p, _ = baue_motor(zone_plateau, feuchte=60.0)
    dauer_alpha = motor_a._berechne_dauer(
        zone_alpha, aktuelle_feuchte=60.0, et0_6h=0.0,
        clip_auf_max=False,
    )
    dauer_plateau = motor_p._berechne_dauer(
        zone_plateau, aktuelle_feuchte=60.0, et0_6h=0.0,
        clip_auf_max=False,
    )
    # alpha-Modell: Erst-Schaetzung 50 min, alpha-Korrektur greift,
    # rohe Dauer >> 80 min
    assert dauer_alpha > 80 * 60
    # Plateau-Modell saturiert -> ~60 min
    assert 50 * 60 < dauer_plateau < 70 * 60


@pytest.mark.asyncio
async def test_t0125_keine_messung_liefert_none(zone_automatik):
    """T-0125: Wenn keine Sensor-Messung vorliegt, None-Pfad
    (Empfehlungslogik faellt auf KEINE_MESSUNG-Blocker)."""
    motor, speicher = baue_motor(zone_automatik)
    speicher.messungen[zone_automatik.zone_id] = []  # explizit leer
    wert = await motor._robuste_feuchte(zone_automatik.zone_id, JETZT)
    assert wert is None


@pytest.mark.asyncio
async def test_t0125_zu_alte_messung_liefert_none(zone_automatik):
    """T-0125: T-0098-Logik bleibt: Messung > 48 h alt -> None."""
    alt = JETZT - timedelta(hours=49)
    motor, _ = baue_motor(
        zone_automatik, messungen=[baue_messung(alt, 50.0)],
    )
    wert = await motor._robuste_feuchte(zone_automatik.zone_id, JETZT)
    assert wert is None


@pytest.mark.asyncio
async def test_t0125_grosser_sprung_wird_durchgereicht(zone_automatik):
    """T-0125: kein MAD-Filter mehr — Sprung 30 -> 85 (Regen) wird als
    Ground Truth durchgereicht. Kein 'Outlier-Verwerfen'.
    """
    messungen = [
        baue_messung(JETZT, 85.0),
        baue_messung(JETZT - timedelta(hours=1), 30.0),
        baue_messung(JETZT - timedelta(hours=2), 30.0),
    ]
    motor, _ = baue_motor(zone_automatik, messungen=messungen)
    wert = await motor._robuste_feuchte(zone_automatik.zone_id, JETZT)
    assert wert == 85.0  # Echter Sensor-Wert, nicht Median 30


@pytest.mark.asyncio
async def test_t0125_grosser_sprung_nach_unten_wird_auch_durchgereicht(zone_automatik):
    """T-0125: auch Down-Spikes sind real (Verdunstung, Sensor-Umzug, etc.).
    Defekt-Erkennung passiert via T-0098 (Alter) + Health-Monitor, nicht
    via Glaettung.
    """
    messungen = [
        baue_messung(JETZT, 25.0),
        baue_messung(JETZT - timedelta(hours=1), 65.0),
        baue_messung(JETZT - timedelta(hours=2), 65.0),
    ]
    motor, _ = baue_motor(zone_automatik, messungen=messungen)
    wert = await motor._robuste_feuchte(zone_automatik.zone_id, JETZT)
    assert wert == 25.0


# --- T-0281: pruefe_zone-Strategie-Konvergenz (Logging-Pfad) ---------------

@pytest.mark.asyncio
async def test_t0281_pruefe_zone_strategie_kein_bedarf_blockt(zone_automatik):
    """T-0281: pruefe_zone (Legacy-Logging-Pfad) -- KONSTANT_NIEDRIG unter
    Schwelle, in-window (JETZT 06:00), kein Blocker, aber Strategie sagt
    'kein_bedarf' -> soll_bewaessern=False + STRATEGIE_KEIN_BEDARF. Sonst
    schriebe das entscheidung_log einen falschen 'giessen'-Eintrag (UI
    EntscheidungsLog/Historie) + setzte einen falschen Pause-Anker."""
    from bewaesserung.entscheidung_pro_zone import ProZoneAuswertung
    zone = zone_automatik.model_copy(update={
        "bewaesserungs_strategie": BewaesserungsStrategie.KONSTANT_NIEDRIG,
    })
    motor, _ = baue_motor(zone, feuchte=28.0)
    motor._strategie_verdict_pro_zone = lambda z, f, e, n, j: _async_return(
        ProZoneAuswertung(
            zone_id=z.zone_id, soll_bewaessern=False,
            empfehlungs_typ="kein_bedarf", ziel_feuchte=None,
            effektive_sicherheits_tage=1.5, aktive_strategie="konstant_niedrig",
            grund="KONSTANT_NIEDRIG: Trockenphase ist Feature.",
        )
    )
    e = await motor.pruefe_zone(zone.zone_id)
    assert e.soll_bewaessern is False
    assert e.blocker_typ == BlockerTyp.STRATEGIE_KEIN_BEDARF
    assert "Trockenphase" in e.begruendung


@pytest.mark.asyncio
async def test_t0281_pruefe_zone_strategie_akut_startet(zone_automatik):
    """T-0281: Strategie sagt akut -> pruefe_zone laesst durch
    (Notfall-Schutz, soll_bewaessern=True)."""
    from bewaesserung.entscheidung_pro_zone import ProZoneAuswertung
    zone = zone_automatik.model_copy(update={
        "bewaesserungs_strategie": BewaesserungsStrategie.SELTEN_GROSS,
    })
    motor, _ = baue_motor(zone, feuchte=22.0)
    motor._strategie_verdict_pro_zone = lambda z, f, e, n, j: _async_return(
        ProZoneAuswertung(
            zone_id=z.zone_id, soll_bewaessern=True,
            empfehlungs_typ="akut", ziel_feuchte=70.0,
            effektive_sicherheits_tage=4.0, aktive_strategie="selten_gross",
            grund="Welkepunkt in 1.0d.",
        )
    )
    e = await motor.pruefe_zone(zone.zone_id)
    assert e.soll_bewaessern is True


@pytest.mark.asyncio
async def test_t0281_pruefe_zone_korridor_unveraendert(zone_automatik):
    """T-0281 Gegenprobe: KORRIDOR (Default) wird NICHT vom Strategie-
    Verdict-Guard erfasst -> schwellen-basiertes Verhalten bleibt
    (soll_bewaessern=True unter Schwelle, in-window). Der Verdict darf
    fuer KORRIDOR gar nicht aufgerufen werden."""
    from bewaesserung.entscheidung_pro_zone import ProZoneAuswertung
    aufrufe: list = []

    def _spy(z, f, e, n, j):
        aufrufe.append(z.zone_id)
        return _async_return(ProZoneAuswertung(
            zone_id=z.zone_id, soll_bewaessern=False,
            empfehlungs_typ="kein_bedarf", ziel_feuchte=None,
            effektive_sicherheits_tage=3.0, aktive_strategie="korridor",
            grund="sollte nicht greifen",
        ))
    motor, _ = baue_motor(zone_automatik, feuchte=28.0)
    motor._strategie_verdict_pro_zone = _spy
    e = await motor.pruefe_zone(zone_automatik.zone_id)
    assert e.soll_bewaessern is True
    assert aufrufe == []


# --- T-0231 Phase 3: pruefe_kanal-Strategie-Konvergenz ---------------------

@pytest.mark.asyncio
async def test_t0231_konstant_niedrig_unter_schwelle_aber_strategie_blockt():
    """T-0231 Phase 3: KONSTANT_NIEDRIG-Zone unter feuchte_schwelle_min,
    aber Welkepunkt-Reserve gross genug -> Auto-Loop blockt mit
    STRATEGIE_KEIN_BEDARF.

    Vor T-0231 wuerde pruefe_kanal die Zone starten (Schwellen-basiert
    allein). Dashboard zeigte aber 'kein_bedarf' -- Diskrepanz.
    """
    zone_a, _zone_b = baue_kanal_zonen()
    # Strategie auf KONSTANT_NIEDRIG umstellen + welkepunkt explizit
    # setzen, damit das Verdict deterministisch ist.
    zone_a = zone_a.model_copy(update={
        "bewaesserungs_strategie": BewaesserungsStrategie.KONSTANT_NIEDRIG,
        "welkepunkt": 15.0,
        "optimum_feuchte_min": 25.0,
    })
    # Speicher braucht hole_kalibrierungen + ml_service -> wir nutzen
    # eine Zone in der bedarf-Liste + faken die Strategie-Pipeline ueber
    # _strategie_verdict_pro_zone, indem wir auf den Stub-Fehler-Pfad
    # zaehlen: wenn Speicher die Methode nicht hat, faellt der Check
    # via safe-default zurueck auf "ok" -- das deckt diese Konvergenz
    # NICHT ab. Stattdessen mocke ich _strategie_verdict_pro_zone
    # direkt mit einer ProZoneAuswertung 'kein_bedarf'.
    from bewaesserung.entscheidung_pro_zone import ProZoneAuswertung
    motor, _ = baue_kanal_motor(zone_a, zone_a, 28.0, 28.0)
    motor._strategie_verdict_pro_zone = lambda z, f, e, n, j: _async_return(
        ProZoneAuswertung(
            zone_id=z.zone_id,
            soll_bewaessern=False,
            empfehlungs_typ="kein_bedarf",
            ziel_feuchte=None,
            effektive_sicherheits_tage=1.5,
            aktive_strategie="konstant_niedrig",
            grund="Trockenphase Feature.",
        )
    )

    entscheidung = await motor.pruefe_kanal(1, [zone_a])

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.STRATEGIE_KEIN_BEDARF
    assert "Strategie" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_t0231_konstant_niedrig_strategie_akut_startet():
    """KONSTANT_NIEDRIG sagt 'akut' bei Welkepunkt-Naehe -> Auto-Loop
    startet trotzdem. Notfall-Schutz uebersteuert nichts."""
    zone_a, _ = baue_kanal_zonen()
    zone_a = zone_a.model_copy(update={
        "bewaesserungs_strategie": BewaesserungsStrategie.KONSTANT_NIEDRIG,
        "welkepunkt": 15.0,
    })
    from bewaesserung.entscheidung_pro_zone import ProZoneAuswertung
    motor, _ = baue_kanal_motor(zone_a, zone_a, 18.0, 18.0)
    motor._strategie_verdict_pro_zone = lambda z, f, e, n, j: _async_return(
        ProZoneAuswertung(
            zone_id=z.zone_id,
            soll_bewaessern=True,
            empfehlungs_typ="akut",
            ziel_feuchte=25.0,
            effektive_sicherheits_tage=1.5,
            aktive_strategie="konstant_niedrig",
            grund="Welkepunkt-Reserve unter 5pp.",
        )
    )

    entscheidung = await motor.pruefe_kanal(1, [zone_a])

    assert entscheidung.soll_bewaessern is True
    assert entscheidung.dauer_sekunden == 420
    assert "Ziel 25%" in entscheidung.begruendung


@pytest.mark.asyncio
async def test_t0231_korridor_unveraendert_default():
    """KORRIDOR (Default) wird vom Konvergenz-Check NICHT geblockt --
    selbst wenn die Pipeline crasht (Stub-Speicher ohne Methoden).
    Wichtig fuer Backward-Compat aller 14 Zonen heute."""
    zone_a, zone_b = baue_kanal_zonen()
    # zone_a/b sind Default-KORRIDOR.
    motor, _ = baue_kanal_motor(zone_a, zone_b, 28.0, 30.0)
    entscheidung = await motor.pruefe_kanal(1, [zone_a, zone_b])
    assert entscheidung.soll_bewaessern is True
    assert entscheidung.blocker_typ is None


async def _async_return(value):
    return value


def test_plateau_transparenz_t0291():
    """T-0291: erwarteter Endwert + Dosen-bis-Ziel fuer plateau-begrenzte
    Einzeldosen (macht die flache Max-Dose ehrlich)."""
    from bewaesserung.entscheidung import _plateau_transparenz
    # bambus-aehnlich: wmax=8, r0=0.5 (tau=16). 48-min-Dose hebt ~7.6 pp.
    endwert, einzel, dosen = _plateau_transparenz(8.0, 0.5, 50.0, 75.0, 48 * 60)
    assert einzel == 7.6                       # Max-Einzeldose ~0.95*wmax
    assert 57.0 <= endwert <= 58.5             # 50 + ~7.6
    assert dosen == 4                          # ceil(25 / 7.6)
    # Schon ueber Ziel -> 0 Dosen.
    assert _plateau_transparenz(8.0, 0.5, 80.0, 75.0, 48 * 60)[2] == 0
    # Kleiner Abstand (<= Einzeldosis) -> 1 Dose.
    assert _plateau_transparenz(8.0, 0.5, 70.0, 75.0, 48 * 60)[2] == 1
    # Kein Plateau-Modell (wmax/r0 fehlen) -> alle None (log-Decay-Pfad).
    assert _plateau_transparenz(None, None, 50.0, 75.0, 600) == (None, None, None)


# =====================================================================
# T-0439: Regen-Gate wirkungsbasiert statt reines mm
# =====================================================================

@pytest.mark.asyncio
async def test_t0439_schwacher_regen_sperrt_trockene_zone_nicht(zone_automatik):
    """Der Realfall: 4,5 mm Prognose sperrten bambuswald bei 20 % (kritisch 45).

    Unter dichtem Blattdach kommen davon nur wenige pp an -- der Regen loest
    das Problem also nicht, die Sperre kostete nur einen noetigen Guss (Andre
    hat am 27.07. 15:32 von Hand nachgegossen). Jetzt entscheidet die
    erwartete Wirkung: 4 pp/mm x (3 - 2) = 4 pp, damit 28 -> 32 und immer noch
    unter Schwelle 35 -> giessen.
    """
    motor, _ = baue_motor(
        zone_automatik, feuchte=28.0,
        vorhersage=baue_vorhersage(regen_pro_stunde=0.5),  # 3 mm/6h
    )
    e = await motor.pruefe_zone(zone_automatik.zone_id)
    assert e.soll_bewaessern is True, (
        "Regen, der die Zone nicht ueber die Schwelle hebt, darf nicht sperren"
    )
    assert e.blocker_typ != BlockerTyp.REGEN_ERWARTET


@pytest.mark.asyncio
async def test_t0439_landregen_sperrt_immer_unabhaengig_vom_zonenfaktor(
    zone_automatik,
):
    """Hard-Stop `regen_gate_immer_ab_mm`.

    Schutz gegen den eigenen Fix: ein zu klein geratener Zonen-Faktor duerfte
    sonst in einen Dauerregen hinein giessen lassen. Faktor hier bewusst
    winzig (0.01 pp/mm) -- die Wirkungsrechnung wuerde NICHT sperren, der
    Hard-Stop muss es trotzdem tun.
    """
    zone = zone_automatik.model_copy(update={"regen_faktor_pp_pro_mm": 0.01})
    motor, _ = baue_motor(
        zone, feuchte=28.0,
        vorhersage=baue_vorhersage(regen_pro_stunde=2.5),  # 15 mm/6h
    )
    e = await motor.pruefe_zone(zone.zone_id)
    assert e.soll_bewaessern is False
    assert e.blocker_typ == BlockerTyp.REGEN_ERWARTET


@pytest.mark.asyncio
async def test_t0439_zonenfaktor_none_verhaelt_sich_wie_globale_konstante(
    zone_automatik,
):
    """Isomorphie/Backward-Compat: Zonen ohne eigenen Faktor rechnen mit
    REGEN_FEUCHTE_FAKTOR. Keine Zone in der Live-Config hat einen gesetzt
    (die Messung liegt auf der Sensor-Aufloesungsgrenze), also haengt das
    gesamte scharfe Verhalten an diesem Pfad."""
    from bewaesserung.entscheidung import REGEN_FEUCHTE_FAKTOR
    motor, _ = baue_motor(zone_automatik, feuchte=28.0)
    assert zone_automatik.regen_faktor_pp_pro_mm is None
    assert motor._regen_faktor_pp_pro_mm(zone_automatik) == REGEN_FEUCHTE_FAKTOR


@pytest.mark.asyncio
async def test_t0439_gate_sperrt_strikt_seltener_als_die_alte_mm_regel(
    zone_automatik,
):
    """Kern-Zusicherung: das neue Gate giesst eher mehr, nie weniger.

    Die alte Bedingung (`mm >= schwelle`) ist Vorbedingung der neuen, also
    kann kein Fall entstehen, in dem frueher gegossen wurde und jetzt nicht.
    Ueber ein Raster geprueft statt an einem Beispiel behauptet.
    """
    motor, _ = baue_motor(zone_automatik, feuchte=28.0)
    schwelle_mm = motor._regen_schwelle_mm()
    for mm in (0.0, 1.0, 1.9, 2.0, 3.0, 5.0, 9.9, 10.0, 25.0):
        alt_blockt = mm >= schwelle_mm
        neu_blockt, _ = motor._regen_gate(zone_automatik, 28.0, 35.0, mm)
        assert not (neu_blockt and not alt_blockt), (
            f"{mm}mm: neues Gate sperrt, altes nicht -- Regression"
        )


@pytest.mark.asyncio
async def test_t0439_modus_mm_stellt_altverhalten_wieder_her(zone_automatik):
    """Ein Wort in der Config dreht zurueck -- die Rueckfallebene, falls das
    wirkungsbasierte Gate im Feld unerwartet handelt."""
    from types import SimpleNamespace

    from bewaesserung.modelle import WetterKonfig
    motor, _ = baue_motor(
        zone_automatik, feuchte=28.0,
        vorhersage=baue_vorhersage(regen_pro_stunde=0.5),
    )
    motor._konfig = SimpleNamespace(
        wetter=WetterKonfig(regen_gate_modus="mm", regen_gate_immer_ab_mm=10.0),
    )
    e = await motor.pruefe_zone(zone_automatik.zone_id)
    assert e.soll_bewaessern is False
    assert e.blocker_typ == BlockerTyp.REGEN_ERWARTET


# =====================================================================
# T-0440: Zwiebelsensor darf den Kanal-Lauf nicht beenden
# =====================================================================

@pytest.mark.asyncio
async def test_t0440_ausgeschlossene_zone_stoppt_den_lauf_nicht():
    """Der Realfall: 22222222 sitzt in der Tropfer-Zwiebel und meldet 85,
    waehrend die Geschwisterzone am selben Ventil noch Wasser braucht.

    Frueher beendete das den Lauf nach im Median 66 min -- die durchgehende
    90-min-Dose, auf die 11111111 monoton antwortet, kam nie zustande.
    """
    zone_a, zone_b = baue_kanal_zonen()
    zone_b = zone_b.model_copy(update={"kanal_max_stop_ausschluss": True})
    # zone_b weit ueber ihrer Stop-Schwelle (65 + 10 = 75), zone_a trocken.
    motor, _ = baue_kanal_motor(zone_a, zone_b, 30.0, 90.0)

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b],
    )

    assert soll_stoppen is False, (
        "Zwiebelsensor darf den Lauf der Geschwisterzone nicht abschneiden"
    )
    assert "zone-a" in begruendung, (
        "die Begruendung muss die verbliebene Stop-Quelle nennen, nicht die "
        "ausgeschlossene"
    )


@pytest.mark.asyncio
async def test_t0440_nicht_ausgeschlossene_zone_stoppt_weiterhin():
    """Gegenprobe: der Max-Stop bleibt eine Sicherheitsfunktion.

    Der Ausschluss waehlt nur die Datenquelle; er schaltet den Stop nicht ab.
    Laeuft die NICHT ausgeschlossene Zone ueber ihre Schwelle, wird gestoppt.
    """
    zone_a, zone_b = baue_kanal_zonen()
    zone_b = zone_b.model_copy(update={"kanal_max_stop_ausschluss": True})
    motor, _ = baue_kanal_motor(zone_a, zone_b, 90.0, 30.0)

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b],
    )

    assert soll_stoppen is True
    assert "zone-a" in begruendung


@pytest.mark.asyncio
async def test_t0440_alle_ausgeschlossen_faellt_auf_volle_liste_zurueck():
    """Fail-safe wie beim Trigger-Ausschluss (T-0382).

    Eine degenerierte Config, die JEDE Zone des Kanals ausschliesst, darf den
    Max-Stop nicht blind machen -- sonst laeuft das Ventil bis max_dauer,
    egal wie nass es ist. Dann gilt wieder die volle Liste.
    """
    zone_a, zone_b = baue_kanal_zonen()
    zone_a = zone_a.model_copy(update={"kanal_max_stop_ausschluss": True})
    zone_b = zone_b.model_copy(update={"kanal_max_stop_ausschluss": True})
    motor, _ = baue_kanal_motor(zone_a, zone_b, 30.0, 90.0)

    soll_stoppen, _ = await motor.pruefe_kanal_max_stop(1, [zone_a, zone_b])

    assert soll_stoppen is True, (
        "alle ausgeschlossen -> Fallback auf alle, Sicherheit bleibt"
    )


@pytest.mark.asyncio
async def test_t0440_default_aus_laesst_bestandskanaele_unveraendert():
    """Isomorphie: ohne gesetztes Flag exakt das alte Verhalten. Betrifft
    jeden anderen Kanal (hecke, waldblumenhain, magerwiese)."""
    zone_a, zone_b = baue_kanal_zonen()
    assert zone_a.kanal_max_stop_ausschluss is False
    motor, _ = baue_kanal_motor(zone_a, zone_b, 30.0, 76.0)

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b],
    )

    assert soll_stoppen is True
    assert "Zone zone-b 76% ueber Stop-Schwelle 75%" in begruendung


@pytest.mark.live_config
def test_t0440_live_config_trigger_und_stop_sind_komplementaer():
    """Die scharfe Konfig muss die Rollen SAUBER trennen.

    Waeren beide Zonen von beidem ausgeschlossen, waere der Kanal blind;
    waere dieselbe Zone von beidem ausgeschlossen, entschiede eine einzige
    Quelle ueber Start UND Ende. Erwartet: bambuswald triggert nicht, stoppt
    aber; yogaraum triggert, stoppt aber nicht.
    """
    from pathlib import Path

    from bewaesserung.konfig import lade_konfig
    k = lade_konfig(Path(__file__).resolve().parents[2] / "config" / "default.yaml")
    zonen = {z.zone_id: z for z in k.zonen}
    bw, yr = zonen["bambuswald"], zonen["bambuswald_yogaraum"]
    assert bw.kanal_trigger_ausschluss is True
    assert bw.kanal_max_stop_ausschluss is False
    assert yr.kanal_trigger_ausschluss is False
    assert yr.kanal_max_stop_ausschluss is True


# =====================================================================
# T-0444: Notbremse fuer Max-Stop-ausgeschlossene Zonen
# =====================================================================

def _kanal_zonen_mit_max_75():
    """Zonen wie `baue_kanal_zonen`, aber mit Stop-Schwelle 85 (75 + 10).

    75 ist die reale `feuchte_schwelle_max` von bambuswald_yogaraum -- der
    Zone, um die es in T-0440/T-0444 geht.
    """
    zone_a, zone_b = baue_kanal_zonen()
    zone_a = zone_a.model_copy(update={"feuchte_schwelle_max": 75.0})
    zone_b = zone_b.model_copy(update={"feuchte_schwelle_max": 75.0})
    return zone_a, zone_b


@pytest.mark.asyncio
async def test_t0444_ausgeschlossene_zone_bei_90_stoppt_nicht():
    """T-0440-Verhalten bleibt: 90 ueber Stop-Schwelle 85, aber unter der
    Notbremse 95 -> der Zwiebelsensor kappt den Lauf weiterhin nicht."""
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    zone_b = zone_b.model_copy(update={"kanal_max_stop_ausschluss": True})
    motor, _ = baue_kanal_motor(zone_a, zone_b, 30.0, 90.0)

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b],
    )

    assert soll_stoppen is False
    assert "Notbremse" not in begruendung


@pytest.mark.asyncio
@pytest.mark.parametrize("feuchte", [95.0, 96.0, 100.0])
async def test_t0444_ausgeschlossene_zone_ab_95_stoppt_ueber_notbremse(feuchte):
    """Der Trade ist bewusst, aber nicht unbegrenzt: AB 95 ist Schluss.

    Der Rasterpunkt 95 muss selbst ausloesen (`>=`, nicht `>`). Der Gardena-
    Sensor quantisiert auf 5 pp -- oberhalb von 95 existiert nur noch 100.
    Mit `>` waeren die 95er-Messungen bremsfrei und die Notbremse zoege real
    erst am Skalenende, an dem echte Uebersaettigung nicht mehr messbar ist.
    Belegter Anlass: yogaraum stand am 27.07. bei 100.
    """
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    zone_b = zone_b.model_copy(update={"kanal_max_stop_ausschluss": True})
    motor, _ = baue_kanal_motor(zone_a, zone_b, 30.0, feuchte)

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b],
    )

    assert soll_stoppen is True
    assert "Notbremse" in begruendung
    assert "zone-b" in begruendung


@pytest.mark.asyncio
async def test_t0444_knapp_unter_der_notbremse_stoppt_nicht():
    """Gegenprobe zum `>=`: die Grenze verschiebt sich nicht nach unten."""
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    zone_b = zone_b.model_copy(update={"kanal_max_stop_ausschluss": True})
    motor, _ = baue_kanal_motor(zone_a, zone_b, 30.0, 94.9)

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b],
    )

    assert soll_stoppen is False
    assert "Notbremse" not in begruendung


@pytest.mark.asyncio
async def test_t0444_notbremse_nur_isoliert_stoppt_trotz_trockener_partnerzone():
    """Isomorphie-Fall aus T-0444: genau EINE Zone ausgeschlossen.

    Die verbleibende Stop-Quelle liegt weit unter ihrer Schwelle und wuerde
    innerhalb eines Laufs nie ausloesen (Realfall: 11111111 braucht ~9 h bis
    zum Peak, Schwelle 85). Ohne Notbremse haette der Kanal damit gar keine
    obere Grenze mehr -- genau die Luecke, die T-0440 hinterliess.
    """
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    zone_b = zone_b.model_copy(update={"kanal_max_stop_ausschluss": True})
    motor, _ = baue_kanal_motor(zone_a, zone_b, 40.0, 96.0)

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b],
    )

    assert soll_stoppen is True, (
        "die trockene Partnerzone darf die Notbremse nicht aushebeln"
    )
    assert "Notbremse" in begruendung


@pytest.mark.asyncio
async def test_t0444_notbremse_ist_pro_zone_konfigurierbar():
    """Die Schwelle ist ein Zonen-Feld, kein globaler Wert."""
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    zone_b = zone_b.model_copy(update={
        "kanal_max_stop_ausschluss": True,
        "kanal_max_stop_notbremse_pp": 88.0,
    })
    motor, _ = baue_kanal_motor(zone_a, zone_b, 30.0, 90.0)

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b],
    )

    assert soll_stoppen is True
    assert "88%" in begruendung


@pytest.mark.asyncio
async def test_t0444_alle_ausgeschlossen_prueft_normale_schwellen_nicht_notbremse():
    """Der T-0382-Fallback bleibt semantisch unveraendert.

    Sind ALLE Zonen ausgeschlossen, gilt wieder die volle Liste gegen die
    NORMALEN Schwellen (85) -- nicht gegen die viel hoehere Notbremse (95).
    Sonst waere der degenerierte Config-Fall lockerer als der normale.
    """
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    zone_a = zone_a.model_copy(update={"kanal_max_stop_ausschluss": True})
    zone_b = zone_b.model_copy(update={"kanal_max_stop_ausschluss": True})
    motor, _ = baue_kanal_motor(zone_a, zone_b, 30.0, 90.0)

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b],
    )

    assert soll_stoppen is True
    assert "Stop-Schwelle" in begruendung
    assert "Notbremse" not in begruendung


@pytest.mark.live_config
def test_t0444_live_config_setzt_notbremse_fuer_die_ausgeschlossene_zone():
    """Die scharfe Konfig muss zum Ausschluss auch eine Notbremse tragen.

    Ein `kanal_max_stop_ausschluss` ohne gesetzte Notbremse laeuft zwar auf
    den Modell-Default 95, aber der Wert soll an der Zone SICHTBAR stehen --
    er ist eine Sicherheits-Entscheidung, kein Implementierungsdetail.
    """
    from pathlib import Path

    from bewaesserung.konfig import lade_konfig
    k = lade_konfig(Path(__file__).resolve().parents[2] / "config" / "default.yaml")
    zonen = {z.zone_id: z for z in k.zonen}
    yr = zonen["bambuswald_yogaraum"]
    assert yr.kanal_max_stop_ausschluss is True
    assert yr.kanal_max_stop_notbremse_pp == 95.0
    # Gegenprobe: die Stop-fuehrende Zone bleibt beim Default.
    assert zonen["bambuswald"].kanal_max_stop_notbremse_pp == 95.0


# =====================================================================
# T-0445: Frische-Pruefung -- Messzeit != Ankunftszeit
# =====================================================================

@pytest.mark.asyncio
async def test_t0445_ohne_lauf_start_keine_frische_pruefung():
    """Backward-Compat: der dritte Aufrufer (Puls-Gate) reicht keinen Start
    durch und muss sich exakt wie vor T-0445 verhalten."""
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    motor, speicher = baue_kanal_motor(zone_a, zone_b, 30.0, 40.0)
    assert speicher.ankuenfte == {}

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b],
    )

    assert soll_stoppen is False
    assert "unter Max-Stop" in begruendung


@pytest.mark.asyncio
async def test_t0445_frische_stop_nach_ablauf_des_fensters():
    """Realfall 28.07.: der stop-relevante Sensor liefert waehrend des Laufs
    keinen NEU EINGETROFFENEN Wert. Ein alter Wert unter Schwelle ist dann
    kein Freibrief, sondern ein unbekannter Zustand."""
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    motor, speicher = baue_kanal_motor(zone_a, zone_b, 30.0, 40.0)
    lauf_start = JETZT - timedelta(minutes=121)
    # Letzte Ankunft VOR Laufbeginn -> waehrend des Laufs kam nichts an.
    speicher.ankuenfte = {
        "zone-a": lauf_start - timedelta(minutes=5),
        "zone-b": lauf_start - timedelta(minutes=5),
    }

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b], lauf_start=lauf_start,
    )

    assert soll_stoppen is True
    assert "kein neu eingetroffener Messwert seit Laufbeginn" in begruendung
    assert "unbekannter Zustand" in begruendung
    typen = {w.typ for w in speicher.geoeffnete_warnungen}
    assert typen == {SensorWarnungTyp.KEINE_ANKUNFT_IM_LAUF}


@pytest.mark.asyncio
async def test_t0445_frische_stop_feuert_nicht_bei_neuer_ankunft():
    """Ein einziger waehrend des Laufs eingetroffener Wert genuegt -- der
    Max-Stop hat dann Augen, auch wenn der Wert unauffaellig ist."""
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    motor, speicher = baue_kanal_motor(zone_a, zone_b, 30.0, 40.0)
    lauf_start = JETZT - timedelta(minutes=121)
    speicher.ankuenfte = {
        "zone-a": lauf_start + timedelta(minutes=10),
        "zone-b": lauf_start - timedelta(minutes=5),
    }

    soll_stoppen, _ = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b], lauf_start=lauf_start,
    )

    assert soll_stoppen is False
    assert speicher.geoeffnete_warnungen == []


@pytest.mark.asyncio
async def test_t0445_frische_stop_feuert_nicht_vor_ablauf_des_fensters():
    """Ein einzelner ausgefallener Beat darf keinen Lauf beenden -- der
    Gardena-Beat faellt regelmaessig aus. Erst 2 Intervalle."""
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    motor, speicher = baue_kanal_motor(zone_a, zone_b, 30.0, 40.0)
    lauf_start = JETZT - timedelta(minutes=119)
    speicher.ankuenfte = {
        "zone-a": lauf_start - timedelta(minutes=5),
        "zone-b": lauf_start - timedelta(minutes=5),
    }

    soll_stoppen, _ = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b], lauf_start=lauf_start,
    )

    assert soll_stoppen is False
    assert speicher.geoeffnete_warnungen == []


@pytest.mark.asyncio
async def test_t0445_karenz_wenn_keine_zone_je_eine_ankunft_hatte():
    """Post-Migrations-Karenz.

    Direkt nach der Migration steht `empfangen_am` fuer ALLE Bestandszeilen
    auf NULL, `letzte_ankunft_feuchte` liefert also ueberall None. Ein ueber
    den Restart wiederhergestellter Lauf traegt seine originale
    `gestartet_am` und laege sofort jenseits des Fensters -- er wuerde im
    ersten Loop-Tick faelschlich gestoppt. Ohne erfasste Ankunft gibt es
    keine Frische-AUSSAGE, also auch keinen Frische-Stop. Die Karenz endet
    von selbst mit dem ersten getrackten Beat.
    """
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    motor, speicher = baue_kanal_motor(zone_a, zone_b, 30.0, 40.0)
    lauf_start = JETZT - timedelta(minutes=180)
    speicher.ankuenfte = {"zone-a": None, "zone-b": None}

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b], lauf_start=lauf_start,
    )

    assert soll_stoppen is False
    assert "unter Max-Stop" in begruendung
    assert speicher.geoeffnete_warnungen == []


@pytest.mark.asyncio
async def test_t0445_gemischte_karenz_stoppt_ueber_die_getrackte_zone():
    """Eine Zone in Karenz, eine mit erfasster, aber veralteter Ankunft.

    Die getrackte Zone traegt eine echte Aussage -- sie hat waehrend des
    Laufs nichts geliefert. Die Karenz der anderen darf das nicht
    ueberstimmen, sonst haengt der Schutz an der langsamsten Migration.
    Gemeldet wird nur die Zone, ueber die eine Aussage moeglich ist.
    """
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    motor, speicher = baue_kanal_motor(zone_a, zone_b, 30.0, 40.0)
    lauf_start = JETZT - timedelta(minutes=180)
    speicher.ankuenfte = {
        "zone-a": None,
        "zone-b": lauf_start - timedelta(minutes=5),
    }

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b], lauf_start=lauf_start,
    )

    assert soll_stoppen is True
    assert "kein neu eingetroffener Messwert" in begruendung
    assert [w.zone_id for w in speicher.geoeffnete_warnungen] == ["zone-b"]


@pytest.mark.asyncio
async def test_t0445_blackout_ohne_jede_messung_stoppt_nach_dem_fenster():
    """Der datenaermste Zustand ueberhaupt: KEINE Zone liefert einen Wert.

    Bis T-0445 kehrte `pruefe_kanal_max_stop` hier sofort mit False zurueck
    -- der Lauf lief also gerade dann ungebremst weiter, wenn gar nichts mehr
    gemessen wird. Anders als die Karenz oben fehlen hier die MESSWERTE
    selbst, nicht nur ihre Ankunftszeiten.
    """
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    motor, speicher = baue_kanal_motor(zone_a, zone_b, 30.0, 40.0)
    speicher.messungen = {}
    lauf_start = JETZT - timedelta(minutes=121)

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b], lauf_start=lauf_start,
    )

    assert soll_stoppen is True
    assert "keine gueltige Feuchtemessung seit Laufbeginn" in begruendung
    assert {w.zone_id for w in speicher.geoeffnete_warnungen} == {
        "zone-a", "zone-b",
    }


@pytest.mark.asyncio
async def test_t0445_blackout_stoppt_nicht_vor_dem_fenster():
    """Auch der Blackout braucht das Fenster -- ein einzelner Beat-Ausfall
    darf keinen Lauf beenden."""
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    motor, speicher = baue_kanal_motor(zone_a, zone_b, 30.0, 40.0)
    speicher.messungen = {}
    lauf_start = JETZT - timedelta(minutes=119)

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b], lauf_start=lauf_start,
    )

    assert soll_stoppen is False
    assert "Keine gueltige Feuchtemessung fuer Max-Stop" in begruendung
    assert speicher.geoeffnete_warnungen == []


@pytest.mark.asyncio
async def test_t0445_blackout_ohne_lauf_start_bleibt_wie_bisher():
    """Backward-Compat: Aufrufer ohne Lauf-Bezug koennen nichts stoppen."""
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    motor, speicher = baue_kanal_motor(zone_a, zone_b, 30.0, 40.0)
    speicher.messungen = {}

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b],
    )

    assert soll_stoppen is False
    assert "Keine gueltige Feuchtemessung fuer Max-Stop" in begruendung


@pytest.mark.asyncio
async def test_t0445_ausgeschlossene_zone_zaehlt_nicht_als_frische_quelle():
    """Die Frische wird an den NORMALEN Stop-Quellen gemessen.

    Die per `kanal_max_stop_ausschluss` herausgenommene Zone haelt nur die
    Notbremse bei 95 -- ihre Beats machen den eigentlichen Max-Stop nicht
    sehend. Meldet also nur sie, bleibt der Zustand unbekannt.
    """
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    zone_b = zone_b.model_copy(update={"kanal_max_stop_ausschluss": True})
    motor, speicher = baue_kanal_motor(zone_a, zone_b, 30.0, 40.0)
    lauf_start = JETZT - timedelta(minutes=121)
    speicher.ankuenfte = {
        "zone-a": lauf_start - timedelta(minutes=5),
        "zone-b": lauf_start + timedelta(minutes=10),
    }

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b], lauf_start=lauf_start,
    )

    assert soll_stoppen is True
    assert "kein neu eingetroffener Messwert" in begruendung
    assert [w.zone_id for w in speicher.geoeffnete_warnungen] == ["zone-a"]


@pytest.mark.asyncio
async def test_t0445_schwellen_stop_hat_vorrang_vor_frische_stop():
    """Liegt ein echter Ueberschreitungs-Grund vor, wird der gemeldet --
    die Frische-Begruendung wuerde die Ursache verschleiern."""
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    motor, speicher = baue_kanal_motor(zone_a, zone_b, 90.0, 40.0)
    lauf_start = JETZT - timedelta(minutes=200)
    # Veraltete (nicht: fehlende) Ankuenfte -- der Frische-Stop WUERDE hier
    # feuern, die Schwelle kommt ihm nur zuvor.
    speicher.ankuenfte = {
        "zone-a": lauf_start - timedelta(minutes=5),
        "zone-b": lauf_start - timedelta(minutes=5),
    }

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b], lauf_start=lauf_start,
    )

    assert soll_stoppen is True
    assert "ueber Stop-Schwelle" in begruendung
    assert speicher.geoeffnete_warnungen == []


# =====================================================================
# T-0434: exakt 0.0 ist Kontaktverlust, kein Messwert
# =====================================================================

@pytest.mark.asyncio
async def test_t0434_null_feuchte_blockiert_statt_kritisch_zu_giessen(
    zone_automatik,
):
    """Der Kern des Tasks: 0.0 darf nicht als "kritisch trocken" gelten.

    0.0 < `feuchte_kritisch` haette `ist_kritisch` gesetzt, und kritisch
    umgeht `bevorzugte_zeiten` -- der Sensorausfall haette also Vollgas zu
    jeder Tageszeit ausgeloest, bis das Tagesbudget leer ist.
    """
    motor, _ = baue_motor(zone_automatik, feuchte=0.0)

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is False
    assert entscheidung.blocker_typ == BlockerTyp.KEINE_MESSUNG
    assert entscheidung.begruendung == "Keine gueltige Feuchtemessung vorhanden"
    assert "Feuchte 0%" not in entscheidung.begruendung


@pytest.mark.asyncio
async def test_t0434_fuenf_prozent_bleibt_kritisch_und_giesst(zone_automatik):
    """Gegenprobe aus der Akzeptanz: 5.0 ist ein ECHTER Messwert.

    Wichtig, weil 5.0 der kleinste je gemessene Wert oberhalb von 0 ist --
    genau der Wert, der vor jedem der vier Abstuerze auf 0.0 stand. Der
    Guard darf nur die 0 selbst nehmen, nicht den Bereich darueber.
    """
    motor, _ = baue_motor(zone_automatik, feuchte=5.0)

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.soll_bewaessern is True
    assert entscheidung.blocker_typ is None


@pytest.mark.asyncio
async def test_t0434_pro_zone_abschaltbar_fuer_fyta_zonen(zone_automatik):
    """FYTA liefert einen kontinuierlichen Uebergang (0 -> 3 -> 4 -> 5),
    dort kann 0.0 ein echter Randwert sein. Der Guard muss abschaltbar sein,
    ohne dass die Zone dadurch andere Schutzmechanismen verliert."""
    zone = zone_automatik.model_copy(
        update={"feuchte_null_ist_defekt": False},
    )
    motor, _ = baue_motor(zone, feuchte=0.0)

    entscheidung = await motor.pruefe_zone(zone.zone_id)

    assert entscheidung.blocker_typ != BlockerTyp.KEINE_MESSUNG
    assert entscheidung.soll_bewaessern is True


# =====================================================================
# T-0489 (Audit A3): 0.0 ist quellenabhaengig, nicht nur zonenabhaengig
# =====================================================================

def test_t0489_null_ist_sensordefekt_kennt_die_quelle():
    """Der Guard darf nur fuer Gardena und unbekannte Quellen greifen.

    Nachgezaehlt 02.08. auf einer DB-Kopie: ueber 16.670 Gardena-Zeilen gibt
    es NULL Werte zwischen 0 und 5 -- eine 0.0 dort ist immer Ausfall. Ueber
    206.315 FYTA-Zeilen gibt es 265 Werte zwischen 0 und 5 und 448 Nullen;
    dort ist 0.0 der untere Rand einer echten Skala.
    """
    from bewaesserung.modelle import DatenQuelle, null_ist_sensordefekt

    zone = ZonenKonfig(
        zone_id="z", name="Z", modus=ZonenModus.MONITORING,
        feuchte_schwelle_min=30.0, feuchte_kritisch=20.0,
    )

    assert null_ist_sensordefekt(zone, DatenQuelle.GARDENA) is True
    assert null_ist_sensordefekt(zone, DatenQuelle.FYTA) is False
    # Unbekannte Quelle verhaelt sich wie Gardena (fail-safe).
    assert null_ist_sensordefekt(zone, None) is True
    assert null_ist_sensordefekt(zone, "") is True
    # Roh-String aus der DB statt Enum.
    assert null_ist_sensordefekt(zone, "fyta") is False

    # Das Zonenflag bleibt der Abschalter davor.
    aus = zone.model_copy(update={"feuchte_null_ist_defekt": False})
    assert null_ist_sensordefekt(aus, DatenQuelle.GARDENA) is False


@pytest.mark.asyncio
async def test_t0489_echte_fyta_null_wird_nicht_mehr_verworfen(zone_automatik):
    """Der eigentliche Fehler: Backend und UI deuteten denselben Punkt
    verschieden.

    `frontend/src/komponenten/sensor-defekt.ts` wertet 0.0 seit T-0258/T-0390
    bewusst nur bei Quelle `gardena` (oder unbekannt) als Defekt. Das Backend
    verwarf sie fuer JEDE Quelle, solange das Zonenflag stand -- und keine der
    14 Zonen setzt es auf false. Eine echte FYTA-Null verschwand damit im
    Backend als "keine Messung", waehrend die UI sie anzeigte.
    """
    from bewaesserung.modelle import DatenQuelle

    messung = baue_messung(JETZT, 0.0, zone_automatik.zone_id)
    fyta_null = messung.model_copy(update={"quelle": DatenQuelle.FYTA})
    motor, _ = baue_motor(zone_automatik, messungen=[fyta_null])

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.blocker_typ != BlockerTyp.KEINE_MESSUNG

    # Gegenprobe: dieselbe 0.0 von Gardena bleibt ein Ausfall.
    gardena_null = messung.model_copy(update={"quelle": DatenQuelle.GARDENA})
    motor2, _ = baue_motor(zone_automatik, messungen=[gardena_null])
    assert (await motor2.pruefe_zone(zone_automatik.zone_id)).blocker_typ == (
        BlockerTyp.KEINE_MESSUNG
    )


# =====================================================================
# T-0502: entscheidend ist der WEG auf die 0, nicht der Wert
# =====================================================================

def test_t0502_trajektorie_entscheidet_ueber_die_null():
    """Reine Logik. Der letzte positive Wert trennt Abstieg von Sprung.

    Die Fuenfer-Quantisierung der Gardena-Skala macht den Weg nach unten zu
    ... 15, 10, 5, 0. Wer zuletzt bei <= 10 stand, ist dort hingetrocknet.
    """
    from bewaesserung.modelle import DatenQuelle, null_ist_sensordefekt

    zone = ZonenKonfig(
        zone_id="z", name="Z", modus=ZonenModus.MONITORING,
        feuchte_schwelle_min=30.0, feuchte_kritisch=20.0,
    )
    g = DatenQuelle.GARDENA

    # Echtes Austrocknen -- die gemessenen magerwiese-Episoden (max24h 5/10/15).
    assert null_ist_sensordefekt(zone, g, max_24h=5.0) is False
    assert null_ist_sensordefekt(zone, g, max_24h=10.0) is False
    assert null_ist_sensordefekt(zone, g, max_24h=15.0) is False
    # Sprung aus gesundem Bereich -- die gemessenen hecke-Episoden
    # (max24h 30/50/50/70/100, Sensor lag im Lager). GENAU DIESE FUENF stufte
    # das alte Vorgaengerwert-Kriterium als echten Messwert ein: ihr letzter
    # positiver Wert war 5.0, wie beim Austrocknen auch.
    assert null_ist_sensordefekt(zone, g, max_24h=30.0) is True
    assert null_ist_sensordefekt(zone, g, max_24h=50.0) is True
    assert null_ist_sensordefekt(zone, g, max_24h=100.0) is True
    # Die Luecke zwischen 20 und 30 traegt die Schwelle 25.
    assert null_ist_sensordefekt(zone, g, max_24h=20.0) is False
    assert null_ist_sensordefekt(zone, g, max_24h=25.0) is True
    # Kein Fenster auffindbar -> kein Beleg -> fail-safe beim Defekt-Urteil.
    assert null_ist_sensordefekt(zone, g, max_24h=None) is True
    # FYTA bleibt unberuehrt: dort ist 0 immer ein echter Wert.
    assert null_ist_sensordefekt(
        zone, DatenQuelle.FYTA, max_24h=100.0,
    ) is False


@pytest.mark.asyncio
async def test_t0502_heruntergetrocknete_null_giesst_statt_zu_blockieren(
    zone_automatik,
):
    """Der Fall, der Andre aufgefallen ist -- und die teure Fehlerrichtung.

    magerwiese ist ueber zwoelf Tage 35 -> 25 -> 20 -> 15 -> 5 -> 0 gelaufen
    und danach bei Regen wieder auf 100 gestiegen. Der Sensor misst. Mit dem
    reinen Wert-Guard galt diese 0 als "keine Messung": an einer SCHARFEN
    Zone heisst das, dass bei echter Trockenheit NICHT gegossen wird.
    """
    from bewaesserung.modelle import DatenQuelle

    verlauf = [
        baue_messung(JETZT - timedelta(hours=h), wert, zone_automatik.zone_id)
        for h, wert in ((72, 15.0), (48, 10.0), (24, 5.0), (0, 0.0))
    ]
    verlauf = [m.model_copy(update={"quelle": DatenQuelle.GARDENA}) for m in verlauf]
    # juengste zuerst -- `baue_motor` nimmt die Liste, wie sie kommt
    motor, _ = baue_motor(zone_automatik, messungen=list(reversed(verlauf)))

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.blocker_typ != BlockerTyp.KEINE_MESSUNG, (
        "heruntergetrocknete 0 muss ein gueltiger Messwert bleiben"
    )


@pytest.mark.asyncio
async def test_t0502_sprung_aus_gesundem_bereich_bleibt_defekt(zone_automatik):
    """Gegenprobe und der urspruengliche Realfall: hecke am 26.05., Sensor im
    Lager, vier Tage konstant 0.0 -- davor gesunder Bereich. Der Sprung muss
    weiterhin als Defekt gelten, sonst haetten wir T-0434 rueckgaengig
    gemacht."""
    from bewaesserung.modelle import DatenQuelle

    verlauf = [
        baue_messung(JETZT - timedelta(hours=h), wert, zone_automatik.zone_id)
        for h, wert in ((48, 45.0), (24, 40.0), (0, 0.0))
    ]
    verlauf = [m.model_copy(update={"quelle": DatenQuelle.GARDENA}) for m in verlauf]
    motor, _ = baue_motor(zone_automatik, messungen=list(reversed(verlauf)))

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.blocker_typ == BlockerTyp.KEINE_MESSUNG


@pytest.mark.asyncio
async def test_t0502_stufenabstieg_binnen_24h_ist_trotzdem_ein_sprung(
    zone_automatik,
):
    """**Der Fall, an dem das alte Kriterium scheiterte -- Regression fuer die
    Korrektur vom 10.08.2026.**

    Ein herausgezogener Sensor faellt nicht in einem Schritt auf 0, sondern
    binnen Stunden durch die Fuenferstufen: 50 -> 30 -> 15 -> 5 -> 0. Sein
    letzter positiver Wert ist damit 5.0 -- exakt wie beim echten
    Austrocknen ueber zwoelf Tage. Das Vorgaengerwert-Kriterium
    (`letzter positiver <= 10` = echt) stufte deshalb ALLE FUENF gemessenen
    hecke-Episoden als gueltigen Messwert ein, also genau den Realfall
    "Sensor lag im Lager", gegen den T-0434 gebaut wurde.

    Das Fenster-Maximum trennt: hier 50 in den letzten 24 h, gemessen liegen
    die hecke-Episoden bei 30..100 und die echten Austrocknungen bei 5..15.

    Dieser Test war vor der Korrektur ROT (Entscheidung kam ohne Blocker
    durch) -- er ist also scharf und nicht bloss Beiwerk.
    """
    from bewaesserung.modelle import DatenQuelle

    verlauf = [
        baue_messung(JETZT - timedelta(hours=h), wert, zone_automatik.zone_id)
        for h, wert in ((20, 50.0), (14, 30.0), (8, 15.0), (2, 5.0), (0, 0.0))
    ]
    verlauf = [m.model_copy(update={"quelle": DatenQuelle.GARDENA}) for m in verlauf]
    motor, _ = baue_motor(zone_automatik, messungen=list(reversed(verlauf)))

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.blocker_typ == BlockerTyp.KEINE_MESSUNG, (
        "Stufenabstieg binnen 24 h aus gesundem Bereich muss Defekt bleiben"
    )


@pytest.mark.asyncio
async def test_t0502_langsamer_abstieg_ueber_tage_bleibt_echter_messwert(
    zone_automatik,
):
    """Gegenprobe zum Test darueber, gleiche Endwerte, anderer Weg.

    Derselbe letzte positive Wert (5.0) und dieselbe 0 -- aber der Weg dorthin
    liegt ausserhalb des 24-h-Fensters. Das ist die magerwiese-Signatur
    (35 -> 0 ueber zwoelf Tage, danach Regen und zurueck auf 100). Hier darf
    der Guard NICHT feuern, sonst bekommt eine real austrocknende scharfe Zone
    "keine Messung" statt "kritisch" und wird nicht gegossen -- die
    gefaehrliche Fehlerrichtung aus T-0502.
    """
    from bewaesserung.modelle import DatenQuelle

    verlauf = [
        baue_messung(JETZT - timedelta(hours=h), wert, zone_automatik.zone_id)
        for h, wert in ((96, 35.0), (72, 25.0), (48, 15.0), (2, 5.0), (0, 0.0))
    ]
    verlauf = [m.model_copy(update={"quelle": DatenQuelle.GARDENA}) for m in verlauf]
    motor, _ = baue_motor(zone_automatik, messungen=list(reversed(verlauf)))

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.blocker_typ != BlockerTyp.KEINE_MESSUNG, (
        "Abstieg ueber Tage ist ein echter Messwert am Skalenende"
    )


@pytest.mark.asyncio
async def test_t0434_greift_ab_der_ersten_messung_ohne_historie(
    zone_automatik,
):
    """Der Unterschied zum bestehenden H-1-Guard.

    `_ist_sensor_festklemmend` haengt am LeckDetektor und braucht 48 h plus
    >= 10 Messungen. Genau diese ersten 48 h waren die Luecke. Hier liegt
    EINE einzige Messung vor und die Zone ist trotzdem geschuetzt.
    """
    motor, speicher = baue_motor(
        zone_automatik,
        messungen=[baue_messung(JETZT, 0.0, zone_automatik.zone_id)],
    )
    # Ausdruecklich KEINE offene SENSOR_EINGEFROREN-Warnung: der bestehende
    # H-1-Guard greift hier also nachweislich nicht, der neue steht allein.
    speicher.offene_warnungen[zone_automatik.zone_id] = []
    assert await motor._ist_sensor_festklemmend(zone_automatik.zone_id) is False

    entscheidung = await motor.pruefe_zone(zone_automatik.zone_id)

    assert entscheidung.blocker_typ == BlockerTyp.KEINE_MESSUNG


@pytest.mark.live_config
def test_t0434_live_config_schuetzt_beide_lead_zonen():
    """Isomorphie-Check als Test.

    Bei einer Zone mit `aggregat_lead_geraet` gibt es per Definition keinen
    zweiten Sensor, der den Ausfall ueberstimmt (Memory
    fehlerpattern_ausschluss_lead_fallback_bei_dropout). Beide Lead-Zonen
    -- hecke und waldblumenhain -- muessen den Guard tragen, und alle vier
    scharfen Zonen ebenfalls.
    """
    from pathlib import Path

    from bewaesserung.konfig import lade_konfig
    k = lade_konfig(
        Path(__file__).resolve().parents[2] / "config" / "default.yaml",
    )
    zonen = {z.zone_id: z for z in k.zonen}

    for zone_id in ("hecke", "waldblumenhain"):
        zone = zonen[zone_id]
        assert zone.aggregat_lead_geraet, f"{zone_id} ist keine Lead-Zone mehr"
        assert zone.feuchte_null_ist_defekt is True, (
            f"{zone_id} liest nur den Lead -- ohne Guard giesst ein "
            f"0.0-Ausfall ungebremst"
        )

    for zone_id in (
        "bambuswald", "bambuswald_yogaraum", "hecke", "waldblumenhain",
    ):
        assert zonen[zone_id].feuchte_null_ist_defekt is True


# =====================================================================
# T-0444 (Nachtrag): der Ausschluss darf nicht am Schweigen eines
# FREMDEN Sensors umkippen
# =====================================================================

@pytest.mark.asyncio
async def test_t0444_stopzone_ohne_messwert_kippt_ausschluss_nicht_um():
    """Die Falle, die T-0434 scharf gemacht hat.

    Die stop-fuehrende Zone (bambuswald/11111111) ist Gardena-Bauart und
    kann selbst auf 0.0 fallen -- seit T-0434 fliegt sie dann aus
    `zonenwerte`. Haenge der Fallback an `stop_werte` statt an der
    Konfiguration, wuerde yogaraum in diesem Moment still wieder gegen die
    normale Schwelle 85 gekappt statt gegen seine Notbremse 95: der bewusste
    T-0440-Ausschluss loeste sich auf, ausgeloest vom Schweigen eines
    fremden Sensors.
    """
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    zone_b = zone_b.model_copy(update={"kanal_max_stop_ausschluss": True})
    # zone_a liest 0.0 -> T-0434 verwirft den Wert -> keine Stop-Zone uebrig.
    motor, _ = baue_kanal_motor(zone_a, zone_b, 0.0, 90.0)

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b],
    )

    assert soll_stoppen is False, (
        "90 liegt ueber der normalen Schwelle 85, aber unter der Notbremse "
        "95 -- der Ausschluss muss halten"
    )
    assert "Stop-Schwelle" not in begruendung


@pytest.mark.asyncio
async def test_t0444_notbremse_greift_auch_ohne_stopzone():
    """Gegenstueck: der Ausschluss haelt, die Sicherheit aber auch.

    Dieselbe Konstellation bei 95 -- die Notbremse ist in diesem Zustand die
    EINZIGE verbliebene Aufsicht und muss ziehen.
    """
    zone_a, zone_b = _kanal_zonen_mit_max_75()
    zone_b = zone_b.model_copy(update={"kanal_max_stop_ausschluss": True})
    motor, _ = baue_kanal_motor(zone_a, zone_b, 0.0, 95.0)

    soll_stoppen, begruendung = await motor.pruefe_kanal_max_stop(
        1, [zone_a, zone_b],
    )

    assert soll_stoppen is True
    assert "Notbremse" in begruendung


# --- T-0560: `ist_kritisch` respektiert das aktive FeuchteRegime ---

def _zone_mit_winterregime() -> ZonenKonfig:
    """hecke-Profil: Basis kritisch 22, im Winterregime 14.

    Das Zeitfenster liegt bewusst frueh morgens, damit der Test die
    Zeitfenster-Schranke prueft -- sie ist eine der drei Schranken, die
    `ist_kritisch` oeffnet (die anderen: Tagesbudget-Faktor, Pause-Bypass).
    """
    return ZonenKonfig(
        zone_id="zone-1",
        name="Hecke-Test",
        modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=35.0,
        feuchte_kritisch=22.0,
        max_dauer_sekunden=1800,
        bevorzugte_zeiten=[ZeitFenster(von="05:00", bis="07:00")],
        feuchte_regime=[
            FeuchteRegime(
                name="winter_vegetationsruhe",
                von_mm_dd="11-15", bis_mm_dd="03-15",
                feuchte_kritisch=14.0,
                grund="Vegetationsruhe -- kaum Bedarf",
            ),
        ],
    )


@pytest.mark.asyncio
async def test_t0560_regime_kritisch_oeffnet_zeitfenster_nicht():
    """Im Winterregime (kritisch 14) ist Feuchte 18 NICHT kritisch.

    Vorher las `ist_kritisch` den Basiswert 22 -> 18 galt als kritisch und
    umging `bevorzugte_zeiten`. Genau in der Phase, in der das Regime
    "kaum giessen" bedeutet, waren damit Zeitfenster, Tagesbudget-Faktor
    und Pause-Bypass geoeffnet.
    """
    zone = _zone_mit_winterregime()
    # 15.12. um 12:00 -- im Regime, ausserhalb des 05:00-07:00-Fensters.
    winter = datetime(2026, 12, 15, 12, 0)
    motor, _ = baue_motor(zone, feuchte=18.0, jetzt=winter)

    entscheidung = await motor.pruefe_zone(zone.zone_id)

    assert entscheidung.blocker_typ == BlockerTyp.ZEITFENSTER
    assert entscheidung.soll_bewaessern is False


@pytest.mark.asyncio
async def test_t0560_regime_kritisch_greift_unter_regime_schwelle():
    """Gegenprobe: unter der REGIME-Schwelle bleibt kritisch kritisch.

    Ohne diesen Fall koennte man `ist_kritisch` auch komplett abschalten,
    ohne dass ein Test es merkt.
    """
    zone = _zone_mit_winterregime()
    winter = datetime(2026, 12, 15, 12, 0)
    motor, _ = baue_motor(zone, feuchte=10.0, jetzt=winter)  # 10 < 14

    entscheidung = await motor.pruefe_zone(zone.zone_id)

    assert entscheidung.blocker_typ != BlockerTyp.ZEITFENSTER
    assert entscheidung.soll_bewaessern is True


@pytest.mark.asyncio
async def test_t0560_ausserhalb_des_regimes_gilt_der_basiswert():
    """Im Sommer (kein Regime aktiv) bleibt kritisch bei 22."""
    zone = _zone_mit_winterregime()
    sommer = datetime(2026, 7, 15, 12, 0)
    motor, _ = baue_motor(zone, feuchte=18.0, jetzt=sommer)  # 18 < 22

    entscheidung = await motor.pruefe_zone(zone.zone_id)

    assert entscheidung.blocker_typ != BlockerTyp.ZEITFENSTER
    assert entscheidung.soll_bewaessern is True
