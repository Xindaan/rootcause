"""T-0066: Dry-Run-Empfehlung fuer das Dashboard-Panel.

Testet `Entscheidungsmotor.vorhersage_zone()` auf:
1. Isomorphie zu `pruefe_zone` — jeder Blocker-Zweig liefert das richtige
   `blocker_typ`.
2. Keine Seiteneffekte — kein `entscheidung_log`, kein `ml_dauer_vorschlag`,
   kein `ventil_ereignis` wird geschrieben.
3. Positiv-Pfad mit Heuristik-Dauer + Liter aus BilanzKonfig.
4. ML-Shadow-Pfad: Modell wird konsultiert, aber NICHTS persistiert.
5. API-Endpoint: 200 bei gueltiger Zone, 404 bei unbekannter.

Das Modell-Mocking nutzt die bestehenden Fixtures aus `test_entscheidung`
(SpeicherAttrappe / WetterClientAttrappe).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from types import SimpleNamespace

from bewaesserung.entscheidung import Entscheidungsmotor, MIN_DAUER_SEKUNDEN
from bewaesserung.modelle import (
    Ausloser,
    BewaesserungsStrategie,
    BilanzKonfig,
    BlockerTyp,
    MlBewaesserungsResponseKonfig,
    MlPhysikDiagnoseKonfig,
    SensorMessung,
    VentilAktion,
    VentilEreignis,
    WetterVorhersage,
    ZeitFenster,
    ZonenKonfig,
    ZonenModus,
)

from test_entscheidung import (
    JETZT,
    SpeicherAttrappe,
    WetterClientAttrappe,
    WetterManagerAttrappe,
    baue_messung,
    baue_vorhersage,
)


def _zone(
    *,
    feuchte_schwelle_min: float = 35.0,
    feuchte_kritisch: float = 20.0,
    ventil_kanal: int | None = 1,
    max_dauer_sekunden: int = 1800,
    tages_budget_sekunden: float = 3600.0,
    modus: ZonenModus = ZonenModus.AUTOMATIK,
    optimum_min: float | None = None,
    optimum_max: float | None = None,
) -> ZonenKonfig:
    return ZonenKonfig(
        zone_id="zone-1",
        name="Testzone",
        modus=modus,
        ventil_kanal=ventil_kanal,
        feuchte_schwelle_min=feuchte_schwelle_min,
        feuchte_schwelle_max=65.0,
        feuchte_kritisch=feuchte_kritisch,
        optimum_feuchte_min=optimum_min,
        optimum_feuchte_max=optimum_max,
        max_dauer_sekunden=max_dauer_sekunden,
        min_pause_minuten=120,
        tages_budget_sekunden=tages_budget_sekunden,
        bevorzugte_zeiten=[ZeitFenster(von="05:00", bis="07:00")],
        flaeche_m2=40.0,
        anteil_kanal=1.0,
    )


def _motor(
    zone: ZonenKonfig,
    *,
    feuchte: float = 30.0,
    jetzt: datetime = JETZT,
    vorhersage: WetterVorhersage | None = None,
    letztes_ereignis: VentilEreignis | None = None,
    heutige_ereignisse: list[VentilEreignis] | None = None,
    response_konfig: MlBewaesserungsResponseKonfig | None = None,
    response_service=None,
    bilanz_konfig: BilanzKonfig | None = None,
    messungen: list[SensorMessung] | None = None,
) -> tuple[Entscheidungsmotor, SpeicherAttrappe]:
    speicher = SpeicherAttrappe()
    if messungen is None:
        speicher.messungen[zone.zone_id] = [baue_messung(jetzt, feuchte, zone.zone_id)]
    else:
        speicher.messungen[zone.zone_id] = list(messungen)
    if letztes_ereignis is not None:
        speicher.letzte_ereignisse[zone.zone_id] = letztes_ereignis
    if heutige_ereignisse is not None:
        speicher.heutige_ereignisse[zone.zone_id] = heutige_ereignisse
    wetter_client = WetterClientAttrappe(vorhersage or baue_vorhersage())
    wetter_manager = WetterManagerAttrappe(wetter_client)

    # Default-BilanzKonfig, damit Liter-Pfad im Positiv-Fall funktioniert.
    bilanz = bilanz_konfig or BilanzKonfig(
        kanal_liter_pro_minute={1: 6.0, 2: 1.87},
        manuell_liter_pro_minute=10.0,
    )

    # Drift-Metriken-Stub: leeres Dict = keine Ampel.
    async def _leeres_drift(zone_id=None, fenster_tage=30, jetzt=None):
        return {}
    speicher.hole_dauer_drift_metriken = _leeres_drift  # type: ignore

    motor = Entscheidungsmotor(
        speicher, wetter_manager, [zone],
        response_konfig=response_konfig,
        response_service=response_service,
        bilanz_konfig=bilanz,
    )
    motor._jetzt = lambda: jetzt
    return motor, speicher


# ---------- 1) Blocker-Kaskaden-Isomorphie ----------

@pytest.mark.asyncio
async def test_vorhersage_zone_unbekannt():
    motor, _ = _motor(_zone())
    empf = await motor.vorhersage_zone("nicht-existent")
    assert empf.soll_bewaessern is False
    assert empf.blocker_typ is None
    assert "unbekannt" in empf.grund.lower()


@pytest.mark.asyncio
async def test_vorhersage_zone_monitoring_modus():
    motor, _ = _motor(_zone(modus=ZonenModus.MONITORING))
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.soll_bewaessern is False
    assert "Monitoring" in empf.grund


@pytest.mark.asyncio
async def test_t0182_monitoring_unter_kritisch_ist_akut():
    """T-0182: Monitoring-Zone mit Sensor unter `feuchte_kritisch` muss
    `empfehlungs_typ='akut'` setzen, damit Watchdog-Akut-Push (T-0166)
    auch fuer Monitoring-Zonen greift. Vorher: hardcoded 'kein_bedarf'
    Default fuehrte zu fehlenden Akut-Pushs bei FYTA-Topfpflanzen
    (Realfall Mandevilla 13.05.2026).
    """
    zone = _zone(
        modus=ZonenModus.MONITORING,
        feuchte_schwelle_min=28.0,
        feuchte_kritisch=18.0,
    )
    motor, _ = _motor(zone, feuchte=15.0)  # unter kritisch
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.soll_bewaessern is False  # Monitoring schaltet nicht
    assert empf.empfehlungs_typ == "akut"


@pytest.mark.asyncio
async def test_t0182_monitoring_unter_schwelle_ist_praeventiv():
    """T-0182: Sensor unter Schwelle (aber ueber kritisch) -> praeventiv."""
    zone = _zone(
        modus=ZonenModus.MONITORING,
        feuchte_schwelle_min=28.0,
        feuchte_kritisch=18.0,
    )
    motor, _ = _motor(zone, feuchte=22.0)  # zwischen kritisch und schwelle
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.empfehlungs_typ == "praeventiv"


@pytest.mark.asyncio
async def test_t0182_monitoring_ueber_schwelle_ist_kein_bedarf():
    """T-0182: Sensor ueber Schwelle -> kein_bedarf (Default-Verhalten)."""
    zone = _zone(
        modus=ZonenModus.MONITORING,
        feuchte_schwelle_min=28.0,
        feuchte_kritisch=18.0,
    )
    motor, _ = _motor(zone, feuchte=45.0)  # gesund
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.empfehlungs_typ == "kein_bedarf"


@pytest.mark.asyncio
async def test_t0183_monitoring_akut_grund_nennt_sensor_und_kritisch():
    """T-0183: bei Monitoring-akut muss `grund` die aktuelle Feuchte +
    `feuchte_kritisch`-Schwelle nennen, damit das Frontend den
    Beobachtungs-Banner ohne extra API-Call rendern kann.
    Realfall: Mandevilla 19 % bei kritisch 18 %.
    """
    zone = _zone(
        modus=ZonenModus.MONITORING,
        feuchte_schwelle_min=28.0,
        feuchte_kritisch=18.0,
    )
    motor, _ = _motor(zone, feuchte=15.0)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.empfehlungs_typ == "akut"
    assert "15" in empf.grund  # aktuelle Feuchte
    assert "18" in empf.grund  # kritisch
    assert "kritisch" in empf.grund.lower()
    assert "Monitoring" in empf.grund


@pytest.mark.asyncio
async def test_t0183_monitoring_praeventiv_grund_nennt_sensor_und_schwelle():
    """T-0183: bei Monitoring-praeventiv muss `grund` die aktuelle Feuchte +
    `feuchte_schwelle_min` nennen (statt nur "Zone im Monitoring")."""
    zone = _zone(
        modus=ZonenModus.MONITORING,
        feuchte_schwelle_min=28.0,
        feuchte_kritisch=18.0,
    )
    motor, _ = _motor(zone, feuchte=22.0)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.empfehlungs_typ == "praeventiv"
    assert "22" in empf.grund
    assert "28" in empf.grund
    assert "Schwelle" in empf.grund


@pytest.mark.asyncio
async def test_vorhersage_zone_keine_messung():
    zone = _zone()
    motor, _ = _motor(zone, messungen=[])
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.soll_bewaessern is False
    assert empf.blocker_typ == BlockerTyp.KEINE_MESSUNG


@pytest.mark.asyncio
async def test_vorhersage_zone_feuchte_ok():
    zone = _zone(feuchte_schwelle_min=30.0)
    motor, _ = _motor(zone, feuchte=45.0)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.soll_bewaessern is False
    assert empf.blocker_typ == BlockerTyp.FEUCHTE_OK
    assert empf.feuchte_aktuell == 45.0


@pytest.mark.asyncio
async def test_t0279_proaktiv_feuert_ueber_schwelle():
    """T-0279 Phase 2 Reachability-Fix (31.05.): bei SELTEN_GROSS mit
    proaktiv-Konfig liefert vorhersage_zone 'praeventiv' OBWOHL die
    Feuchte noch UEBER der Schwelle liegt -- der proaktive Trigger
    (Physik-Reserve bis optimum_min <= Schwelle) umgeht das FEUCHTE_OK-
    Gate. Ohne Fix kaeme faelschlich 'kein_bedarf' (wie im
    feuchte_ok-Test oben, der dieselbe Feuchte/Schwelle nutzt).

    Realfall-Spiegel waldblumen: feuchte 45% > Schwelle, Welkepunkt 32,
    optimum_min 40 -> Physik-Reserve ~3d <= proaktiv-Schwelle 5d.
    """
    zone = ZonenKonfig(
        zone_id="zone-1", name="Waldtest", ventil_kanal=1,
        feuchte_schwelle_min=35.0, feuchte_schwelle_max=65.0,
        feuchte_kritisch=20.0,
        max_dauer_sekunden=1800, min_pause_minuten=120,
        tages_budget_sekunden=3600.0,
        # Zeitfenster deckt JETZT ab, damit der Positiv-Pfad (nicht
        # ZEITFENSTER-Blocker) erreicht wird.
        bevorzugte_zeiten=[ZeitFenster(von="00:00", bis="23:59")],
        flaeche_m2=40.0, anteil_kanal=1.0,
        bewaesserungs_strategie=BewaesserungsStrategie.SELTEN_GROSS,
        welkepunkt=32.0,
        optimum_feuchte_min=40.0, optimum_feuchte_max=60.0,
        k_basis_pro_h=0.01,
        proaktiv_tage_vor_optimum_min=5.0,
    )
    motor, _ = _motor(zone, feuchte=45.0)
    # Physik-Diagnose aktiv + leere Standorte (ET0 faellt auf et0_basis).
    motor._konfig = SimpleNamespace(
        ml_physik_diagnose=MlPhysikDiagnoseKonfig(aktiv=True),
        standorte=[],
    )
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.empfehlungs_typ == "praeventiv"
    # Dauer wird auf das SELTEN_GROSS-Ziel (Feldkapazitaet/optimum_max+5)
    # gerechnet -> > 0, NICHT der MIN-Clip-Fall.
    assert empf.dauer_s_empfehlung is not None
    assert empf.dauer_s_empfehlung > 0
    # Grund darf NICHT "unter Schwelle" behaupten (Feuchte ist drueber).
    assert "Proaktiver Tiefenlauf" in (empf.grund or "")
    assert "unter Schwelle" not in (empf.grund or "")
    assert empf.soll_bewaessern is True


@pytest.mark.asyncio
async def test_t0286_proaktiv_unterdrueckt_nach_kuerzlichem_lauf():
    """T-0286 Recency-Guard (Engine-Integration): gleiche Lage wie der
    Reachability-Test (proaktiv wuerde feuern), aber ein Ground-Truth-Lauf
    endete vor 1 h -- innerhalb versickerungs_karenz_stunden (6 h). Die
    traegen Multi-Sensor (FYTA) haengen dem frisch gegossenen Gardena
    nach, deshalb darf der proaktive Trigger NICHT erneut feuern. ->
    FEUCHTE_OK/kein_bedarf statt praeventiv."""
    zone = ZonenKonfig(
        zone_id="zone-1", name="Waldtest", ventil_kanal=1,
        feuchte_schwelle_min=35.0, feuchte_schwelle_max=65.0,
        feuchte_kritisch=20.0,
        max_dauer_sekunden=1800, min_pause_minuten=120,
        tages_budget_sekunden=3600.0,
        bevorzugte_zeiten=[ZeitFenster(von="00:00", bis="23:59")],
        flaeche_m2=40.0, anteil_kanal=1.0,
        bewaesserungs_strategie=BewaesserungsStrategie.SELTEN_GROSS,
        welkepunkt=32.0,
        optimum_feuchte_min=40.0, optimum_feuchte_max=60.0,
        k_basis_pro_h=0.01,
        proaktiv_tage_vor_optimum_min=5.0,
        versickerungs_karenz_stunden=6,
    )
    # Ground-Truth-Lauf (manuell, echte ventil_id) endete vor 1 h.
    lauf = VentilEreignis(
        zeitstempel=JETZT - timedelta(hours=1),
        zone_id="zone-1", ventil_id="gardena-uuid",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=1800,
        ausloser=Ausloser.MANUELL,
    )
    motor, _ = _motor(zone, feuchte=45.0, heutige_ereignisse=[lauf])
    motor._konfig = SimpleNamespace(
        ml_physik_diagnose=MlPhysikDiagnoseKonfig(aktiv=True),
        standorte=[],
    )
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.empfehlungs_typ == "kein_bedarf"
    assert empf.blocker_typ == BlockerTyp.FEUCHTE_OK
    assert empf.soll_bewaessern is False


@pytest.mark.asyncio
async def test_vorhersage_zone_regen_erwartet():
    zone = _zone()
    # 3 mm in 6 h ueber der Schwelle 2 mm
    vorhersage = baue_vorhersage(regen_pro_stunde=0.5, anzahl_stunden=12)
    motor, _ = _motor(zone, feuchte=25.0, vorhersage=vorhersage)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.soll_bewaessern is False
    assert empf.blocker_typ == BlockerTyp.REGEN_ERWARTET


@pytest.mark.asyncio
async def test_vorhersage_zone_ausserhalb_zeitfenster():
    zone = _zone()
    ausserhalb = JETZT.replace(hour=14, minute=0)
    motor, _ = _motor(zone, feuchte=25.0, jetzt=ausserhalb)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.soll_bewaessern is False
    assert empf.blocker_typ == BlockerTyp.ZEITFENSTER


@pytest.mark.asyncio
async def test_vorhersage_zone_budget_erschoepft():
    zone = _zone(tages_budget_sekunden=120.0)
    # Heutiges Ereignis verbraucht bereits mehr als das Budget.
    verbrauch = VentilEreignis(
        zeitstempel=JETZT - timedelta(minutes=30),
        zone_id="zone-1", ventil_id="v1",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=200,
        ausloser=Ausloser.AUTOMATIK,
    )
    motor, _ = _motor(
        zone, feuchte=25.0, heutige_ereignisse=[verbrauch],
    )
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.soll_bewaessern is False
    assert empf.blocker_typ == BlockerTyp.BUDGET_ERSCHOEPFT


@pytest.mark.asyncio
async def test_vorhersage_zone_pause_aktiv():
    zone = _zone()
    kurzes_ereignis = VentilEreignis(
        zeitstempel=JETZT - timedelta(minutes=30),
        zone_id="zone-1", ventil_id="v1",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
        ausloser=Ausloser.AUTOMATIK,
    )
    motor, _ = _motor(
        zone, feuchte=25.0, letztes_ereignis=kurzes_ereignis,
    )
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.soll_bewaessern is False
    assert empf.blocker_typ == BlockerTyp.PAUSE_AKTIV


# ---------- 2) Keine Seiteneffekte ----------

@pytest.mark.asyncio
async def test_vorhersage_zone_schreibt_keine_entscheidung_db(monkeypatch):
    """Ein Positiv-Pfad darf nicht `speichere_entscheidung` aufrufen —
    sonst wuerde das `_pause_eingehalten`-Anker verschieben."""
    # optimum_min > feuchte -> echter Bedarf (wohlfuehl_grenze), damit der
    # Positiv-Pfad (soll_bewaessern=True) deterministisch erreicht wird
    # (T-0279-Folge: kein_bedarf -> soll_bewaessern=False).
    zone = _zone(optimum_min=40.0)
    motor, speicher = _motor(zone, feuchte=25.0)
    vorher = len(speicher.gespeicherte_entscheidungen)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.soll_bewaessern is True
    assert len(speicher.gespeicherte_entscheidungen) == vorher, (
        "vorhersage_zone darf keinen entscheidung_log-Eintrag schreiben"
    )


@pytest.mark.asyncio
async def test_vorhersage_zone_schreibt_keinen_dauer_vorschlag():
    """ML-Shadow-Pfad (aktiv=true, wirksam=false) darf nicht persistieren."""
    zone = _zone(optimum_min=40.0)  # echter Bedarf -> Positiv-Pfad
    response_service = _FakeResponseService(dauer_rueckgabe=900)
    response_konfig = MlBewaesserungsResponseKonfig(aktiv=True, wirksam=False)
    motor, speicher = _motor(
        zone, feuchte=25.0,
        response_konfig=response_konfig,
        response_service=response_service,
    )

    aufrufe: list[dict] = []

    async def _zaehl_persist(**kwargs):
        aufrufe.append(kwargs)
    speicher.speichere_dauer_vorschlag = _zaehl_persist  # type: ignore

    empf = await motor.vorhersage_zone("zone-1")
    assert empf.soll_bewaessern is True
    assert empf.dauer_s_heuristik is not None
    assert empf.dauer_s_ml == 900
    assert empf.ml_aktiv is True
    assert empf.ml_wirksam is False
    assert aufrufe == [], (
        "speichere_dauer_vorschlag darf im Dry-Run nicht aufgerufen werden"
    )


# ---------- 3) Positiv-Pfad mit Liter ----------

@pytest.mark.asyncio
async def test_vorhersage_zone_positiv_heuristik_mit_liter():
    # Kanal 1 = 6 L/min in der Default-Bilanz; optimum_min > feuchte ->
    # echter Bedarf (Positiv-Pfad, soll_bewaessern=True).
    zone = _zone(ventil_kanal=1, optimum_min=40.0)
    motor, _ = _motor(zone, feuchte=25.0)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.soll_bewaessern is True
    assert empf.blocker_typ is None
    assert empf.dauer_s_heuristik is not None and empf.dauer_s_heuristik > 0
    # Liter = dauer_min * 6 L/min.
    erwartet = round(empf.dauer_s_heuristik / 60.0 * 6.0, 1)
    assert empf.liter_heuristik == erwartet


@pytest.mark.asyncio
async def test_vorhersage_zone_ohne_bilanz_konfig_hat_keine_liter():
    """BilanzKonfig fehlt ganz → `liter_heuristik` bleibt None,
    Panel zeigt nur Minuten ohne Crash."""
    zone = _zone(optimum_min=40.0)  # echter Bedarf -> Positiv-Pfad
    speicher = SpeicherAttrappe()
    speicher.messungen[zone.zone_id] = [baue_messung(JETZT, 25.0, zone.zone_id)]

    async def _leeres_drift(zone_id=None, fenster_tage=30, jetzt=None):
        return {}
    speicher.hole_dauer_drift_metriken = _leeres_drift  # type: ignore

    wetter_client = WetterClientAttrappe(baue_vorhersage())
    wetter_manager = WetterManagerAttrappe(wetter_client)
    motor = Entscheidungsmotor(
        speicher, wetter_manager, [zone], bilanz_konfig=None,
    )
    motor._jetzt = lambda: JETZT
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.soll_bewaessern is True
    assert empf.liter_heuristik is None


# ---------- 4) Drift-Ampel ----------

@pytest.mark.asyncio
async def test_vorhersage_zone_drift_ampel_gruen():
    zone = _zone()
    motor, speicher = _motor(zone, feuchte=25.0)

    async def _drift(zone_id=None, fenster_tage=30, jetzt=None):
        return {"zone-1": {
            "mae_heuristik": 100.0, "mae_ml": 40.0,
            "n_bewertet": 12, "n_ml_bewertet": 12,
        }}
    speicher.hole_dauer_drift_metriken = _drift  # type: ignore

    empf = await motor.vorhersage_zone("zone-1")
    assert empf.drift_ampel == "gruen"
    assert empf.drift_mae_heuristik == 100.0
    assert empf.drift_mae_ml == 40.0
    assert empf.drift_n_bewertet == 12


@pytest.mark.asyncio
async def test_vorhersage_zone_drift_ampel_rot():
    zone = _zone()
    motor, speicher = _motor(zone, feuchte=25.0)

    async def _drift(zone_id=None, fenster_tage=30, jetzt=None):
        return {"zone-1": {
            "mae_heuristik": 100.0, "mae_ml": 90.0,
            "n_bewertet": 10, "n_ml_bewertet": 10,
        }}
    speicher.hole_dauer_drift_metriken = _drift  # type: ignore

    empf = await motor.vorhersage_zone("zone-1")
    assert empf.drift_ampel == "rot"


@pytest.mark.asyncio
async def test_vorhersage_zone_drift_ampel_wenig_daten_keine_ampel():
    """n < 5 → keine Ampel, auch wenn MAE-Verhaeltnis gut aussaehe."""
    zone = _zone()
    motor, speicher = _motor(zone, feuchte=25.0)

    async def _drift(zone_id=None, fenster_tage=30, jetzt=None):
        return {"zone-1": {
            "mae_heuristik": 100.0, "mae_ml": 30.0,
            "n_bewertet": 3, "n_ml_bewertet": 3,
        }}
    speicher.hole_dauer_drift_metriken = _drift  # type: ignore

    empf = await motor.vorhersage_zone("zone-1")
    assert empf.drift_ampel is None
    assert empf.drift_n_bewertet == 3


# ---------- Fake-ResponseService fuer ML-Pfad ----------

class _FakeResponseService:
    def __init__(self, dauer_rueckgabe: int | None = 900):
        self._dauer = dauer_rueckgabe
        self._version = "v-test-2026-04-23"

    def lade_zone(self, zone_id: str, *, force: bool = False) -> bool:
        return True

    def version(self, zone_id: str) -> str | None:
        return self._version

    def inverse_dauer(self, **kwargs) -> int | None:
        return self._dauer


@pytest.mark.asyncio
async def test_vorhersage_zone_ohne_ml_pfad_leer():
    """Kein Service konfiguriert → ml_aktiv bleibt false, ml-Felder None."""
    zone = _zone()
    motor, _ = _motor(zone, feuchte=25.0)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.ml_aktiv is False
    assert empf.dauer_s_ml is None
    assert empf.modell_version is None


@pytest.mark.asyncio
async def test_vorhersage_zone_ml_service_exception_faellt_zurueck():
    """Wenn der Service crasht, darf das Panel trotzdem Heuristik liefern."""
    zone = _zone(optimum_min=40.0)  # echter Bedarf -> Positiv-Pfad

    class _KrasherService:
        def lade_zone(self, *args, **kwargs):
            raise RuntimeError("simulierter ML-Loadfehler")

        def version(self, *args, **kwargs):
            return None

        def inverse_dauer(self, **kwargs):
            return 42

    motor, _ = _motor(
        zone, feuchte=25.0,
        response_konfig=MlBewaesserungsResponseKonfig(aktiv=True, wirksam=False),
        response_service=_KrasherService(),
    )
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.soll_bewaessern is True
    assert empf.dauer_s_heuristik is not None
    assert empf.dauer_s_ml is None  # Fallback


@pytest.mark.asyncio
async def test_vorhersage_zone_pause_aktiv_liefert_hypothetische_dauer():
    """Mentale Planung: auch wenn Pause laeuft, will der User sehen
    welche Dosis die Empfehlung jetzt wuerde (Blocker-Grund bleibt).
    """
    zone = _zone(ventil_kanal=1)
    kurzes_ereignis = VentilEreignis(
        zeitstempel=JETZT - timedelta(minutes=30),
        zone_id="zone-1", ventil_id="v1",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=600,
        ausloser=Ausloser.AUTOMATIK,
    )
    motor, _ = _motor(
        zone, feuchte=25.0, letztes_ereignis=kurzes_ereignis,
    )
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.soll_bewaessern is False
    assert empf.blocker_typ == BlockerTyp.PAUSE_AKTIV
    # Hypothetische Dauer + Liter sollen gesetzt sein.
    assert empf.dauer_s_heuristik is not None and empf.dauer_s_heuristik > 0
    assert empf.liter_heuristik is not None and empf.liter_heuristik > 0
    # Grund bleibt inhaltlich die Blocker-Erklaerung.
    assert "Pause" in empf.grund
    assert "warten" in empf.grund


@pytest.mark.asyncio
async def test_vorhersage_zone_zeitfenster_liefert_hypothetische_dauer():
    zone = _zone(ventil_kanal=1)
    ausserhalb = JETZT.replace(hour=14, minute=0)
    motor, _ = _motor(zone, feuchte=25.0, jetzt=ausserhalb)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.blocker_typ == BlockerTyp.ZEITFENSTER
    assert empf.dauer_s_heuristik is not None and empf.dauer_s_heuristik > 0
    assert empf.liter_heuristik is not None


@pytest.mark.asyncio
async def test_vorhersage_zone_budget_erschoepft_liefert_hypothetische_dauer():
    zone = _zone(ventil_kanal=1, tages_budget_sekunden=120.0)
    verbrauch = VentilEreignis(
        zeitstempel=JETZT - timedelta(minutes=30),
        zone_id="zone-1", ventil_id="v1",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=200,
        ausloser=Ausloser.AUTOMATIK,
    )
    motor, _ = _motor(
        zone, feuchte=25.0, heutige_ereignisse=[verbrauch],
    )
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.blocker_typ == BlockerTyp.BUDGET_ERSCHOEPFT
    assert empf.dauer_s_heuristik is not None and empf.dauer_s_heuristik > 0


@pytest.mark.asyncio
async def test_vorhersage_zone_regen_erwartet_liefert_hypothetische_dauer():
    """Bei Regen: user moechte trotzdem sehen, was ohne Regen empfohlen
    waere — nuetzlich wenn der Regen ausfaellt."""
    zone = _zone(ventil_kanal=1)
    vorhersage = baue_vorhersage(regen_pro_stunde=0.5, anzahl_stunden=12)
    motor, _ = _motor(zone, feuchte=25.0, vorhersage=vorhersage)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.blocker_typ == BlockerTyp.REGEN_ERWARTET
    assert empf.dauer_s_heuristik is not None and empf.dauer_s_heuristik > 0


@pytest.mark.asyncio
async def test_t0279_1b_feuchte_ok_liefert_adoptierbare_zieldosis():
    """T-0279 Phase 1b: FEUCHTE_OK liefert jetzt eine ADOPTIERBARE
    Zieldosis (Richtung Strategie-Ziel optimum_max) fuer die 1-Klick-
    Uebernahme. ABER: dauer_s_heuristik bleibt None -- die MIN-Clip-
    Heuristik (raise-to-Schwelle = 0 -> verwirrendes '1 min') wird NICHT
    gesetzt; nur dauer_s_empfehlung traegt die sinnvolle Zieldosis.
    feuchte 45 % >= Schwelle 30, KORRIDOR-Ziel optimum_max 60 -> Gap 15."""
    zone = _zone(feuchte_schwelle_min=30.0).model_copy(update={
        "welkepunkt": 32.0,
        "optimum_feuchte_min": 40.0,
        "optimum_feuchte_max": 60.0,
    })
    leicht = baue_vorhersage(et0_pro_stunde=0.05, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=45.0, vorhersage=leicht)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.blocker_typ == BlockerTyp.FEUCHTE_OK
    assert empf.empfehlungs_typ == "kein_bedarf"
    assert empf.dauer_s_heuristik is None
    assert empf.dauer_s_empfehlung is not None
    assert empf.dauer_s_empfehlung > 0


@pytest.mark.asyncio
async def test_t0279_1b_feuchte_ok_nahe_ziel_keine_zieldosis():
    """T-0279 Phase 1b Gegenprobe: ist die Feuchte schon nah am Ziel
    (< 2 pp = unter Sensor-Aufloesung), wird KEINE Zieldosis angeboten
    (sonst MIN-Clip-'1 min'). feuchte 59 %, optimum_max-Ziel 60 -> Gap 1."""
    zone = _zone(feuchte_schwelle_min=30.0).model_copy(update={
        "welkepunkt": 32.0,
        "optimum_feuchte_min": 40.0,
        "optimum_feuchte_max": 60.0,
    })
    leicht = baue_vorhersage(et0_pro_stunde=0.05, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=59.0, vorhersage=leicht)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.blocker_typ == BlockerTyp.FEUCHTE_OK
    assert empf.dauer_s_heuristik is None
    assert empf.dauer_s_empfehlung is None


@pytest.mark.asyncio
async def test_t0279_1b_kein_bedarf_unter_schwelle_liefert_zieldosis():
    """T-0279 Phase 1b (Main-Pfad): SELTEN_GROSS unter Schwelle, aber
    Reserve ok + kein proaktiver Trigger -> empfehlungs_typ kein_bedarf.
    Trotzdem liefert die Engine eine adoptierbare Zieldosis Richtung
    Strategie-Ziel (Feldkapazitaet-Fallback optimum_max+5) fuer die
    1-Klick-Uebernahme. feuchte 38 %, Ziel ~65 -> Gap gross genug."""
    zone = _zone(feuchte_schwelle_min=40.0).model_copy(update={
        "bewaesserungs_strategie": BewaesserungsStrategie.SELTEN_GROSS,
        "welkepunkt": 32.0,
        "optimum_feuchte_min": 40.0,
        "optimum_feuchte_max": 60.0,
    })
    leicht = baue_vorhersage(et0_pro_stunde=0.05, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=38.0, vorhersage=leicht)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.empfehlungs_typ == "kein_bedarf"
    assert empf.dauer_s_empfehlung is not None
    assert empf.dauer_s_empfehlung > 0


@pytest.mark.asyncio
async def test_vorhersage_zone_keine_messung_weiterhin_ohne_dauer():
    """Gegenprobe: ohne Feuchte-Messung kein Input → keine Dauer."""
    zone = _zone()
    motor, _ = _motor(zone, messungen=[])
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.blocker_typ == BlockerTyp.KEINE_MESSUNG
    assert empf.dauer_s_heuristik is None
    assert empf.feuchte_aktuell is None


@pytest.mark.asyncio
async def test_vorhersage_zone_ml_dauer_wird_auf_max_geclippt():
    zone = _zone(max_dauer_sekunden=1800)
    motor, _ = _motor(
        zone, feuchte=20.0,
        response_konfig=MlBewaesserungsResponseKonfig(aktiv=True, wirksam=False),
        response_service=_FakeResponseService(dauer_rueckgabe=9999),
    )
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.dauer_s_ml == 1800


# ---------- T-0075: Kausale Empfehlung ----------

def _zone_mit_welkepunkt(**kwargs) -> ZonenKonfig:
    """Wrapper: zone.welkepunkt setzen, sonst greift Fallback-Kette."""
    welke = kwargs.pop("welkepunkt", None)
    z = _zone(**kwargs)
    if welke is not None:
        return z.model_copy(update={"welkepunkt": welke})
    return z


@pytest.mark.asyncio
async def test_kausal_kein_bedarf_lange_reserve():
    """Sensor 70 %, Welkepunkt 30 %, kein ET0/Regen → mehrtaegige Reserve,
    Empfehlungs-Typ='kein_bedarf'."""
    zone = _zone_mit_welkepunkt(
        feuchte_schwelle_min=40.0, welkepunkt=30,
    )
    # Sehr trockenes Wetter (et0=0.5 mm/h * 24 = 12 mm/Tag → Decay 24 pp/Tag
    # waere sehr extrem) — wir wollen langen Reserve, also et0 niedrig:
    leichtes_wetter = baue_vorhersage(et0_pro_stunde=0.05, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=70.0, vorhersage=leichtes_wetter)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.blocker_typ == BlockerTyp.FEUCHTE_OK
    assert empf.empfehlungs_typ == "kein_bedarf"
    assert empf.welkepunkt_wert == 30.0
    assert empf.welkepunkt_quelle == "manuell"
    # Reserve = (70 - 30) / decay; decay min 0.5 → tage_bis_welkepunkt >= 80.
    # bei moderaten et0 noch deutlich >> sicherheits_tage=3.
    assert empf.tage_bis_welkepunkt is not None
    assert empf.tage_bis_welkepunkt > 3.0
    assert "kein Bedarf" in empf.erklarung_kurz.lower() or "reserve" in empf.erklarung_kurz.lower()


@pytest.mark.asyncio
async def test_kausal_akut_nahe_welkepunkt():
    """Sensor 32 %, Welkepunkt 30 %, sommerlich-hoher ET0 →
    Reserve <= 1.5 Tage → 'akut'.

    Reserve = (32-30) / decay. Bei et0=2 mm/Tag → decay = 4 pp/Tag
    → Reserve 0.5 Tage → akut."""
    zone = _zone_mit_welkepunkt(
        feuchte_schwelle_min=40.0, welkepunkt=30,
    )
    sommer = baue_vorhersage(et0_pro_stunde=0.09, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=32.0, vorhersage=sommer)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.empfehlungs_typ == "akut"
    assert empf.dauer_s_empfehlung is not None
    assert empf.dauer_s_empfehlung >= MIN_DAUER_SEKUNDEN
    assert "akut" in empf.erklarung_kurz.lower()


@pytest.mark.asyncio
async def test_kausal_praeventiv_im_arbeitsband():
    """Sensor 36 %, Welkepunkt 30 (5 pp drueber, nicht akut), unter
    Schwelle 40, hoher ET0 sodass Reserve < sicherheits_tage → praeventiv.

    Schluessel: Decay muss gross genug sein, dass die Reserve kleiner
    als die Default-sicherheits_tage (3) ist — sonst greift der neue
    'kein_bedarf'-Pfad und liefert keine Empfehlung."""
    zone = _zone_mit_welkepunkt(
        feuchte_schwelle_min=40.0, welkepunkt=30,
    )
    # Reserve = (36-30) = 6 pp. Mit decay 3 pp/Tag → 2 Tage Reserve →
    # zwischen 1.5 und 3 Tage → praeventiv.
    # decay = ET0_FAKTOR(2) × et0_24h. Et0=1.5 mm/Tag → 0.0625 mm/h.
    wetter = baue_vorhersage(et0_pro_stunde=0.07, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=36.0, vorhersage=wetter)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.empfehlungs_typ == "praeventiv"
    assert empf.dauer_s_empfehlung is not None
    # Erklaerung enthaelt "praeventiv" oder Reserve/Tage-Aussage
    assert empf.erklarung_lang


@pytest.mark.asyncio
async def test_kausal_unter_schwelle_aber_grosse_reserve_ist_kein_bedarf():
    """T-0075-Bugfix: Bambus 55 % unter Schwelle 60 %, Welkepunkt 50 %,
    niedriger ET0 → Reserve > sicherheits_tage → 'kein_bedarf' (NICHT
    praeventiv). Vorher: jeder Sensor-Wert unter Schwelle wurde stumpf
    als 'praeventiv' klassifiziert, egal wie gross die Reserve war."""
    zone = _zone_mit_welkepunkt(
        feuchte_schwelle_min=60.0, welkepunkt=50,
    )
    # Decay-Default 0.5 pp/Tag (mind.) → Reserve = 5/0.5 = 10 Tage > 3.
    motor, _ = _motor(zone, feuchte=55.0)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.empfehlungs_typ == "kein_bedarf"
    # Bei kein_bedarf wird KEINE Dauer empfohlen (Gap zum Ziel < 2 pp).
    assert empf.dauer_s_empfehlung is None
    # T-0279-Folge: kein_bedarf -> soll_bewaessern=False, blocker=None
    # (Dashboard-'ok'), auch wenn JETZT (06:00) im bevorzugten Fenster
    # liegt. Vorher lief der Main-Pfad faelschlich auf soll_bewaessern=True.
    assert empf.soll_bewaessern is False
    assert empf.blocker_typ is None
    # Reserve-Tage werden trotzdem ausgewiesen.
    assert empf.tage_bis_welkepunkt is not None
    assert empf.tage_bis_welkepunkt > 3.0


@pytest.mark.asyncio
async def test_kausal_decay_heuristik_baut_prognose():
    """Heuristik-Decay-Pfad ohne ML: Prognose 6/12/24h ist gesetzt,
    monoton fallend, prognose_quelle='heuristik'."""
    zone = _zone_mit_welkepunkt(welkepunkt=30)
    # Mittleres ET0
    wetter = baue_vorhersage(et0_pro_stunde=0.2, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=55.0, vorhersage=wetter)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.prognose_quelle == "heuristik"
    assert empf.prognose_6h is not None
    assert empf.prognose_12h is not None
    assert empf.prognose_24h is not None
    # Monotonie: spaetere Horizonte sind nicht hoeher als fruehere
    assert empf.prognose_6h >= empf.prognose_12h >= empf.prognose_24h


@pytest.mark.asyncio
async def test_kausal_welkepunkt_quelle_manuell_hat_vorrang():
    """Wenn zone.welkepunkt gesetzt → Quelle='manuell', Wert kommt nicht
    aus Schaetzung."""
    zone = _zone_mit_welkepunkt(welkepunkt=42)
    motor, _ = _motor(zone, feuchte=50.0)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.welkepunkt_wert == 42.0
    assert empf.welkepunkt_quelle == "manuell"


@pytest.mark.asyncio
async def test_kausal_welkepunkt_quelle_feuchte_kritisch_fallback():
    """Ohne manuellen Welkepunkt + ohne Kalibrierungsdaten + zu kurze
    Sensorhistorie → Fallback auf zone.feuchte_kritisch."""
    zone = _zone(feuchte_kritisch=20.0)  # kein welkepunkt-Override
    motor, _ = _motor(zone, feuchte=45.0)
    # SpeicherAttrappe hat keine hole_kalibrierungen + kaum Tagesdaten
    # (nur 1 Messung) → fällt durch Kette bis feuchte_kritisch.
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.welkepunkt_wert == 20.0
    assert empf.welkepunkt_quelle == "feuchte_kritisch_fallback"


@pytest.mark.asyncio
async def test_zeit_bis_welkepunkt_lineare_interpolation():
    """Direkter Helper-Test: aktuell 60, Welke 40, Decay 5 pp/Tag → 4 Tage."""
    zone = _zone()
    motor, _ = _motor(zone)
    tage = motor._zeit_bis_welkepunkt(
        aktuelle_feuchte=60.0, welkepunkt=40.0, decay_pp_pro_tag=5.0,
    )
    assert tage == 4.0


@pytest.mark.asyncio
async def test_zeit_bis_welkepunkt_unter_welke_liefert_null():
    zone = _zone()
    motor, _ = _motor(zone)
    tage = motor._zeit_bis_welkepunkt(
        aktuelle_feuchte=20.0, welkepunkt=30.0, decay_pp_pro_tag=5.0,
    )
    assert tage == 0.0


@pytest.mark.asyncio
async def test_dauer_fuer_sicherheitsabstand_clippt_auf_max():
    """Extremer Decay (z.B. 30 pp/Tag, sicherheits=3 → Ziel-Feuchte 95+)
    → Dauer clipt auf zone.max_dauer_sekunden."""
    zone = _zone(max_dauer_sekunden=600)
    motor, _ = _motor(zone)
    dauer = motor._dauer_fuer_sicherheitsabstand(
        zone, aktuelle_feuchte=30.0, welkepunkt=30.0,
        sicherheits_tage=3.0, decay_pp_pro_tag=30.0, et0_6h=0.0,
    )
    assert dauer == 600


@pytest.mark.asyncio
async def test_kausal_blocker_kaskade_isomorph():
    """Isomorphie-Sicherheitsnetz: alle Blocker-Typen (REGEN, ZEITFENSTER,
    BUDGET, PAUSE) liefern weiter ein korrektes blocker_typ-Feld plus
    haben jetzt zusaetzlich kausale Felder gesetzt."""
    zone = _zone_mit_welkepunkt(welkepunkt=30, tages_budget_sekunden=120.0)
    # BUDGET_ERSCHOEPFT triggern: heutiger Verbrauch über Budget
    verbrauch = VentilEreignis(
        zeitstempel=JETZT - timedelta(minutes=30),
        zone_id="zone-1", ventil_id="v1",
        aktion=VentilAktion.SCHLIESSEN, dauer_sekunden=200,
        ausloser=Ausloser.AUTOMATIK,
    )
    motor, _ = _motor(zone, feuchte=25.0, heutige_ereignisse=[verbrauch])
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.blocker_typ == BlockerTyp.BUDGET_ERSCHOEPFT
    # Kausale Felder muessen auch in Blocker-Faellen vorhanden sein
    assert empf.welkepunkt_wert == 30.0
    assert empf.dauer_s_empfehlung is not None
    assert empf.empfehlungs_typ in ("akut", "praeventiv")


@pytest.mark.asyncio
async def test_sicherheits_tage_override_aendert_dauer():
    """Override-Param: groesserer sicherheits_tage → groessere Dauer.

    Setup so, dass beide Pfade in 'akut' oder 'praeventiv' fallen
    (Default 3 Tage UND Override 10 Tage), nicht in 'kein_bedarf' —
    dafuer braucht's einen hohen genug Decay. Mit Reserve (40-30)=10 pp
    und decay 4 pp/Tag → Reserve 2.5 Tage < default(3) und < override(10)
    → beides praeventiv."""
    zone = _zone_mit_welkepunkt(
        welkepunkt=30, feuchte_schwelle_min=50.0, max_dauer_sekunden=10000,
    )
    # Decay 4 pp/Tag = 2.0 × et0=2 mm/Tag = 0.0833 mm/h
    wetter = baue_vorhersage(et0_pro_stunde=0.09, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=40.0, vorhersage=wetter)
    empf_default = await motor.vorhersage_zone("zone-1")
    empf_lang = await motor.vorhersage_zone(
        "zone-1", sicherheits_tage_override=10.0,
    )
    # Sanity: beide Pfade muessen den kausalen Block treffen
    assert empf_default.empfehlungs_typ in ("akut", "praeventiv"), (
        f"Default war {empf_default.empfehlungs_typ}, sollte akut/praeventiv "
        f"sein (Reserve {empf_default.tage_bis_welkepunkt})"
    )
    assert empf_lang.empfehlungs_typ in ("akut", "praeventiv"), (
        f"Override war {empf_lang.empfehlungs_typ}"
    )
    # Bei laengerem Sicherheitsabstand: Dauer grösser oder gleich
    assert empf_lang.dauer_s_empfehlung is not None
    assert empf_default.dauer_s_empfehlung is not None
    assert empf_lang.dauer_s_empfehlung >= empf_default.dauer_s_empfehlung


# ---------- T-0103: Strategie-aware Empfehlungen ----------



def _zone_mit_strategie(
    strategie: BewaesserungsStrategie,
    *,
    welkepunkt: float | None = None,
    optimum_min: float | None = None,
    optimum_max: float | None = None,
    **kwargs,
) -> ZonenKonfig:
    z = _zone(**kwargs)
    update: dict = {"bewaesserungs_strategie": strategie}
    if welkepunkt is not None:
        update["welkepunkt"] = welkepunkt
    if optimum_min is not None:
        update["optimum_feuchte_min"] = optimum_min
    if optimum_max is not None:
        update["optimum_feuchte_max"] = optimum_max
    return z.model_copy(update=update)


@pytest.mark.asyncio
async def test_haeufig_klein_triggert_an_wohlfuehl_min():
    """T-0103 HAEUFIG_KLEIN: Sensor 58 % unter Wohlfuehl-Min 60 % triggert
    `praeventiv` (NICHT kein_bedarf wie bei KORRIDOR), auch wenn Welkepunkt-
    Reserve gross ist.
    """
    zone = _zone_mit_strategie(
        BewaesserungsStrategie.HAEUFIG_KLEIN,
        welkepunkt=30, optimum_min=60.0, optimum_max=75.0,
        # HAEUFIG_KLEIN-Realitaet: Schwelle == optimum_min (Bambus 60/60).
        # Sonst greift FEUCHTE_OK vor dem Strategie-Klassifikator.
        feuchte_schwelle_min=60.0,
    )
    # Niedriger ET0 -> grosse Welkepunkt-Reserve
    leichtes_wetter = baue_vorhersage(et0_pro_stunde=0.05, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=58.0, vorhersage=leichtes_wetter)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.aktive_strategie == "haeufig_klein"
    assert empf.empfehlungs_typ == "praeventiv", (
        f"erwartet praeventiv (Sensor unter Wohl-Min), bekam {empf.empfehlungs_typ}"
    )


@pytest.mark.asyncio
async def test_haeufig_klein_kein_bedarf_im_wohlfuehlbereich():
    """T-0103 HAEUFIG_KLEIN: Sensor 65 % im Wohl 60-75 -> kein_bedarf."""
    zone = _zone_mit_strategie(
        BewaesserungsStrategie.HAEUFIG_KLEIN,
        welkepunkt=30, optimum_min=60.0, optimum_max=75.0,
        feuchte_schwelle_min=60.0,
    )
    leichtes_wetter = baue_vorhersage(et0_pro_stunde=0.05, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=65.0, vorhersage=leichtes_wetter)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.empfehlungs_typ == "kein_bedarf"


@pytest.mark.asyncio
async def test_korridor_wohlfuehl_grenze_als_sanfter_hinweis():
    """T-0103 KORRIDOR: Sensor unter Wohlfuehl-Min ABER Welkepunkt-Reserve
    > sicherheits_tage -> neue Stufe `wohlfuehl_grenze` (statt heute
    kein_bedarf).
    """
    # Welkepunkt 30, Sensor 36 (unter optimum_min 38), kein ET0/Regen
    # -> Reserve = (36-30) / 0.5 (Floor) = 12 Tage > sicherheits_tage 3
    zone = _zone_mit_strategie(
        BewaesserungsStrategie.KORRIDOR,
        welkepunkt=30, optimum_min=38.0, optimum_max=55.0,
        feuchte_schwelle_min=38.0,
    )
    # ET0=0 -> Decay-Floor 0.5 pp/Tag, Reserve maximal
    kein_wetter = baue_vorhersage(et0_pro_stunde=0.0, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=36.0, vorhersage=kein_wetter)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.aktive_strategie == "korridor"
    assert empf.empfehlungs_typ == "wohlfuehl_grenze", (
        f"erwartet wohlfuehl_grenze, bekam {empf.empfehlungs_typ}"
    )


@pytest.mark.asyncio
async def test_selten_gross_kein_praeventiv_nur_akut_oder_kein_bedarf():
    """T-0103 SELTEN_GROSS: nur akut oder kein_bedarf — kein praeventiv.

    Sensor 35, Welkepunkt 30, niedriger ET0 -> Reserve > 1.5 -> kein_bedarf
    (bei KORRIDOR waere das 'praeventiv').
    """
    zone = _zone_mit_strategie(
        BewaesserungsStrategie.SELTEN_GROSS,
        welkepunkt=30, optimum_min=40.0, optimum_max=55.0,
        feuchte_schwelle_min=40.0,
    )
    leichtes_wetter = baue_vorhersage(et0_pro_stunde=0.05, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=35.0, vorhersage=leichtes_wetter)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.aktive_strategie == "selten_gross"
    # Reserve = (35-30)/0.5 = 10 Tage > 1.5 -> kein_bedarf
    assert empf.empfehlungs_typ == "kein_bedarf"


@pytest.mark.asyncio
async def test_selten_gross_zielt_auf_feldkapazitaet_fallback():
    """T-0103 SELTEN_GROSS bei akut: Ziel = Feldkapazitaet, Fallback
    optimum_max + 5 wenn FK fehlt. Bei Welkepunkt 30, Sensor 32,
    optimum_max 55 -> Ziel = 60. Dauer ca. (60-32)/1pp/min = 28 min.
    """
    # Hoher ET0 fuer kurze Reserve -> akut
    starkes_wetter = baue_vorhersage(et0_pro_stunde=0.5, regen_pro_stunde=0.0)
    zone = _zone_mit_strategie(
        BewaesserungsStrategie.SELTEN_GROSS,
        welkepunkt=30, optimum_min=40.0, optimum_max=55.0,
        feuchte_schwelle_min=40.0, max_dauer_sekunden=10000,
    )
    motor, _ = _motor(zone, feuchte=32.0, vorhersage=starkes_wetter)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.aktive_strategie == "selten_gross"
    assert empf.empfehlungs_typ == "akut"
    # Ziel = optimum_max + 5 = 60 (FK fehlt). Differenz zu Sensor 32 ist
    # 28 pp -> bei 1 pp/min Default ca. 28 min, plus ET0-Aufschlag.
    # Wir testen tolerant: > 20 min (Mindest-Dauer + Aufschlag-Buffer).
    assert empf.dauer_s_empfehlung is not None
    assert empf.dauer_s_empfehlung >= 20 * 60


@pytest.mark.asyncio
async def test_konstant_niedrig_zielt_nur_auf_optimum_min():
    """T-0103 KONSTANT_NIEDRIG: Trigger bei Sensor <= Welke+5pp,
    Ziel = optimum_min (NICHT max — bewusst niedrig halten).
    """
    # Welkepunkt 30, Sensor 33 = unter Welke+5 -> Trigger
    zone = _zone_mit_strategie(
        BewaesserungsStrategie.KONSTANT_NIEDRIG,
        welkepunkt=30, optimum_min=35.0, optimum_max=50.0,
        feuchte_schwelle_min=40.0, max_dauer_sekunden=10000,
    )
    leichtes_wetter = baue_vorhersage(et0_pro_stunde=0.05, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=33.0, vorhersage=leichtes_wetter)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.aktive_strategie == "konstant_niedrig"
    assert empf.empfehlungs_typ == "akut"
    # Ziel = optimum_min = 35 (nicht 50 wie bei KORRIDOR)
    # Dauer = (35 - 33) / delta_pp = klein
    assert empf.dauer_s_empfehlung is not None
    # Dauer-Sanity: bei 1 pp/min Default und 2 pp Differenz -> ca. 2 min,
    # aber MIN_DAUER_SEKUNDEN=60 clipped. Wir testen nur dass es viel
    # kleiner ist als bei KORRIDOR (Ziel 50).
    zone_korridor = _zone_mit_strategie(
        BewaesserungsStrategie.KORRIDOR,
        welkepunkt=30, optimum_min=35.0, optimum_max=50.0,
        feuchte_schwelle_min=40.0, max_dauer_sekunden=10000,
    )
    motor_k, _ = _motor(zone_korridor, feuchte=33.0, vorhersage=leichtes_wetter)
    empf_k = await motor_k.vorhersage_zone("zone-1")
    if empf_k.dauer_s_empfehlung is not None:
        assert empf.dauer_s_empfehlung <= empf_k.dauer_s_empfehlung


@pytest.mark.asyncio
async def test_konstant_niedrig_kein_bedarf_im_trockenbereich():
    """T-0103 KONSTANT_NIEDRIG: Sensor 38 (ueber Welke+5=35) ->
    kein_bedarf, bewusst trocken halten. Bei KORRIDOR mit Sensor < Wohl-Min
    waere das `wohlfuehl_grenze`, hier ist Trockenphase Feature.

    Setup: ET0=0 -> Decay-Floor 0.5 -> Reserve gross (8/0.5=16 Tage),
    keine welke_akut-Auswirkung.
    """
    zone = _zone_mit_strategie(
        BewaesserungsStrategie.KONSTANT_NIEDRIG,
        welkepunkt=30, optimum_min=40.0, optimum_max=50.0,
        feuchte_schwelle_min=40.0,
    )
    # ET0=0 -> Decay-Floor 0.5 -> grosse Reserve, kein akut-Trigger
    kein_wetter = baue_vorhersage(et0_pro_stunde=0.0, regen_pro_stunde=0.0)
    # Sensor 38: ueber Welke+5 (35), aber unter Wohl-Min (40)
    motor, _ = _motor(zone, feuchte=38.0, vorhersage=kein_wetter)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.empfehlungs_typ == "kein_bedarf"
    # Erklaerung enthaelt Trockenphasen-Hinweis
    assert "Trockenphase" in empf.erklarung_kurz or "Trockenphase" in empf.erklarung_lang


@pytest.mark.asyncio
async def test_haeufig_klein_triggert_bei_prognose_unter_wohl_min_trotz_feuchte_ok():
    """T-0103-Folge 29.04.: Bei HAEUFIG_KLEIN soll FEUCHTE_OK NICHT
    greifen, wenn die Prognose (6h/12h/24h) unter Wohl-Min faellt.
    Realfall: Bambus Sensor 65 % >= Schwelle 60 -> heute kein_bedarf.
    Mit Prognose 24h = 57 % unter Wohl-Min 60 -> sollte praeventiv sein.
    """
    zone = _zone_mit_strategie(
        BewaesserungsStrategie.HAEUFIG_KLEIN,
        welkepunkt=47, optimum_min=60.0, optimum_max=75.0,
        feuchte_schwelle_min=60.0,
    )
    # Hoher ET0 -> Decay 7 pp/Tag -> Prognose 24h = 65 - 7 = 58 < opt_min 60
    starkes_wetter = baue_vorhersage(et0_pro_stunde=0.15, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=65.0, vorhersage=starkes_wetter)
    empf = await motor.vorhersage_zone("zone-1")
    # Klassifikator wurde aufgerufen statt FEUCHTE_OK
    assert empf.aktive_strategie == "haeufig_klein"
    assert empf.empfehlungs_typ == "praeventiv", (
        f"erwartet praeventiv (HAEUFIG_KLEIN + Prognose unter Wohl-Min), "
        f"bekam {empf.empfehlungs_typ}"
    )
    # Sollte keine FEUCHTE_OK-Sperre haben
    assert empf.blocker_typ != BlockerTyp.FEUCHTE_OK


@pytest.mark.asyncio
async def test_haeufig_klein_feuchte_ok_bei_prognose_im_wohlfuehlbereich():
    """T-0103-Folge: bei HAEUFIG_KLEIN UND Sensor >= Schwelle UND Prognose
    bleibt im Wohl -> FEUCHTE_OK greift normal (kein praeventiv).
    Sicherheitsnetz: HAEUFIG_KLEIN macht FEUCHTE_OK nicht generell aus.
    """
    zone = _zone_mit_strategie(
        BewaesserungsStrategie.HAEUFIG_KLEIN,
        welkepunkt=47, optimum_min=60.0, optimum_max=75.0,
        feuchte_schwelle_min=60.0,
    )
    # ET0=0 -> Decay-Floor 0.5 pp/Tag -> Prognose 24h = 65 - 0.5 ~ 64.5 > 60
    leichtes_wetter = baue_vorhersage(et0_pro_stunde=0.0, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=65.0, vorhersage=leichtes_wetter)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.aktive_strategie == "haeufig_klein"
    assert empf.empfehlungs_typ == "kein_bedarf"
    assert empf.blocker_typ == BlockerTyp.FEUCHTE_OK


# Hinweis: Multi-Horizont-Multi-Trigger nur bei HAEUFIG_KLEIN (nicht KORRIDOR),
# weil KORRIDOR-FEUCHTE_OK bewusst greift wenn Sensor >= Schwelle ist —
# wohlfuehl_grenze bleibt der sanfte Hinweis fuer den Fall Sensor < opt_min.


# ---------- T-0086: Mehrfach-Takt-Empfehlung ----------

@pytest.mark.asyncio
async def test_mehrfach_takt_wenn_rohe_dauer_ueber_max_dauer():
    """T-0086: rohe Dauer > max_dauer -> Hauptdose=max_dauer + folge_dose
    mit Restdauer + Verzoegerung. Setup: niedrige Wirkungsrate (0.2)
    und max_dauer 30 min, sodass Empfehlung max_dauer ueberschreitet.
    """
    zone = _zone_mit_strategie(
        BewaesserungsStrategie.HAEUFIG_KLEIN,
        welkepunkt=47, optimum_min=60.0, optimum_max=75.0,
        feuchte_schwelle_min=60.0,
        max_dauer_sekunden=1800,  # 30 min
    )
    # Niedrige Wirkungsrate -> lange Empfehlung
    zone = zone.model_copy(update={"delta_pp_pro_minute": 0.15})
    leichtes_wetter = baue_vorhersage(et0_pro_stunde=0.05, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=55.0, vorhersage=leichtes_wetter)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.empfehlungs_typ == "praeventiv"
    # Rohe Dauer = (75-55)/0.15 = 133 min = 8000 s, geclippt auf 1800 s
    assert empf.dauer_s_empfehlung == 1800
    # Folge-Dose vorhanden
    assert empf.folge_dose_dauer_s is not None
    assert empf.folge_dose_dauer_s > 0
    # rohe Dauer > 1800 -> folge_dose >= ~5000 s (rohe minus 1800)
    assert empf.folge_dose_dauer_s >= 3000
    assert empf.folge_dose_verzoegerung_h is not None
    assert empf.folge_dose_verzoegerung_h >= 6.0
    assert empf.folge_dose_liter is not None
    assert empf.folge_dose_liter > 0


@pytest.mark.asyncio
async def test_kein_mehrfach_takt_bei_kurzer_empfehlung():
    """T-0086: rohe Dauer <= max_dauer -> kein folge_dose (None).
    Setup: KORRIDOR mit hoher Wirkungsrate -> Empfehlung passt in
    max_dauer.
    """
    zone = _zone_mit_strategie(
        BewaesserungsStrategie.KORRIDOR,
        welkepunkt=30, optimum_min=40.0, optimum_max=55.0,
        feuchte_schwelle_min=40.0,
        max_dauer_sekunden=10000,
    )
    zone = zone.model_copy(update={"delta_pp_pro_minute": 1.0})
    # ET0 hoch -> Reserve klein -> akut
    starkes_wetter = baue_vorhersage(et0_pro_stunde=0.5, regen_pro_stunde=0.0)
    motor, _ = _motor(zone, feuchte=33.0, vorhersage=starkes_wetter)
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.empfehlungs_typ == "akut"
    assert empf.dauer_s_empfehlung is not None
    # Bei 1 pp/min und ~22 pp Differenz: 22 min = 1320 s, < max_dauer 10000.
    assert empf.dauer_s_empfehlung < 10000
    assert empf.folge_dose_dauer_s is None
    assert empf.folge_dose_verzoegerung_h is None


# T-0105: ML-Prognose + strategie-aware Reserve-Grenze + neue
# `_zeit_bis_grenze_aus_prognose`-Funktion.

@pytest.mark.asyncio
async def test_zeit_bis_grenze_aus_prognose_findet_unterschreiter():
    """Prognose 12h=55, 24h=50, Grenze=52 -> linear interpoliert ~19h.

    (55-52)/(55-50) = 0.6 Anteil zwischen 12 und 24h => 12 + 12*0.6 = 19.2h.
    """
    zone = _zone()
    motor, _ = _motor(zone)
    prognose = {6: 58.0, 12: 55.0, 24: 50.0}
    tage = motor._zeit_bis_grenze_aus_prognose(
        aktuelle_feuchte=60.0, grenze=52.0, prognose=prognose,
        decay_pp_pro_tag=10.0,
    )
    assert tage is not None
    assert 0.7 <= tage <= 0.9  # ~19h


@pytest.mark.asyncio
async def test_zeit_bis_grenze_extrapoliert_wenn_horizont_nicht_reicht():
    """Wenn die 24h-Prognose noch ueber Grenze liegt: linear extrapolieren."""
    zone = _zone()
    motor, _ = _motor(zone)
    # Aktuell 70, alle Prognosen >= 60, Grenze=50, Decay 5/Tag
    # -> nach 24h bei 60, dann noch (60-50)/5 = 2 Tage extrapoliert -> 3.0
    prognose = {6: 67.0, 12: 65.0, 24: 60.0}
    tage = motor._zeit_bis_grenze_aus_prognose(
        aktuelle_feuchte=70.0, grenze=50.0, prognose=prognose,
        decay_pp_pro_tag=5.0,
    )
    assert tage is not None
    assert 2.5 <= tage <= 3.5


@pytest.mark.asyncio
async def test_prognose_ml_oder_heuristik_fallback_ohne_service():
    """Ohne ml_vorhersage_service -> Heuristik-Fallback (T-0121: + Decay)."""
    zone = _zone()
    motor, _ = _motor(zone)  # _motor() injectet aktuell kein ml-Service
    prognose, quelle, decay_eff = await motor._prognose_ml_oder_heuristik(
        zone.zone_id, 60.0, decay_pp_pro_tag=5.0, horizonte_h=[6, 12, 24],
    )
    assert quelle == "heuristik"
    assert prognose[24] == 55.0  # 60 - 5*1.0 Tag
    assert decay_eff == 5.0  # Heuristik: unveraendert


@pytest.mark.asyncio
async def test_kein_bedarf_bei_haeufig_klein_zeigt_wohl_min_label():
    """T-0105 + User-Befund 30.04.: bei HAEUFIG_KLEIN ist die Reserve-
    Aussage gegen Wohl-Min, nicht Welkepunkt."""
    zone = _zone_mit_strategie(
        BewaesserungsStrategie.HAEUFIG_KLEIN,
        welkepunkt=47.0,
        optimum_min=60.0,
        optimum_max=75.0,
    )
    # Sensor 75 % weit ueber Wohlfuehl-Bereich -> kein_bedarf erwartet
    motor, _ = _motor(zone, feuchte=75.0)
    empf = await motor.vorhersage_zone(zone.zone_id)
    assert empf.empfehlungs_typ == "kein_bedarf"
    assert empf.reserve_grenze_label == "Wohl-Min"
    assert empf.tage_bis_reserve_grenze is not None


@pytest.mark.asyncio
async def test_ml_decay_uebersteuert_heuristik_extrapolation():
    """T-0121: Realfall Waldblumen 02.05.: aktuell 40 %, ML-Prognose
    40/40/36 (kalt, niedriger ET0). Heuristik-Decay aus aggressivem
    ET0-Forecast = 10 pp/Tag → wuerde Welkepunkt in 1.4 Tagen prognosti-
    zieren. ML-impliziter Decay nur 4 pp/Tag → Welkepunkt erst in
    >2 Tagen — keine 'akut'-Klassifikation.
    """
    zone = _zone()
    motor, _ = _motor(zone)
    aktuell = 40.0
    grenze = 32.0  # Welkepunkt
    prognose = {6: 40.0, 12: 40.0, 24: 36.2}

    # Mit Heuristik-Decay 10 pp/Tag (aggressiv): Extrapolation 4.2/10 = 0.42
    # Total ~1.42 Tage -> "akut"
    tage_heuristik = motor._zeit_bis_grenze_aus_prognose(
        aktuell, grenze, prognose, decay_pp_pro_tag=10.0,
    )
    assert tage_heuristik is not None
    assert 1.3 <= tage_heuristik <= 1.5

    # Mit ML-Decay 4 pp/Tag (Realitaet): Extrapolation 4.2/4 = 1.05
    # Total ~2.05 Tage -> "praeventiv"
    tage_ml = motor._zeit_bis_grenze_aus_prognose(
        aktuell, grenze, prognose, decay_pp_pro_tag=4.0,
    )
    assert tage_ml is not None
    assert tage_ml > 2.0
    # ML-Decay liefert MEHR Reserve-Tage (entspannter) als Heuristik
    assert tage_ml > tage_heuristik * 1.4


# ---------- T-0378: kritische Trockenheit schlaegt min_pause ----------

def _zone_pause_bypass(
    *, min_pause_minuten: int, karenz_h: int,
    bypass_karenz_h: float = 1.0, feuchte_kritisch: float = 20.0,
) -> ZonenKonfig:
    """Zone mit explizit gesetzter Pause + beiden Karenzen.

    T-0414: `karenz_h` (versickerungs_karenz_stunden) steuert die
    Sensor-Heuristik, `bypass_karenz_h` NUR den T-0378-Pause-Bypass. Dass es
    zwei getrennte Werte sind, ist der Kern des T-0414-Fixes -- vorher nutzte
    der Bypass den 3-h-Wert und war dadurch bei kurzer min_pause wirkungslos.
    """
    return ZonenKonfig(
        zone_id="zone-1", name="Z", modus=ZonenModus.AUTOMATIK,
        feuchte_schwelle_min=35.0, feuchte_schwelle_max=70.0,
        feuchte_kritisch=feuchte_kritisch, ventil_kanal=1,
        max_dauer_sekunden=1800, tages_budget_sekunden=36000.0,
        min_pause_minuten=min_pause_minuten,
        versickerungs_karenz_stunden=karenz_h,
        pause_bypass_karenz_stunden=bypass_karenz_h,
    )


def _lauf(*, vor_minuten: int, dauer_minuten: int) -> list[VentilEreignis]:
    """Ein abgeschlossener Lauf: OEFFNEN vor `vor_minuten`, SCHLIESSEN
    `dauer_minuten` spaeter. Der Pause-Anker haengt am OEFFNEN, der
    Karenz-Guard am SCHLIESSEN -- deshalb braucht es beide Events."""
    start = JETZT - timedelta(minutes=vor_minuten)
    return [
        VentilEreignis(
            zeitstempel=start, zone_id="zone-1", ventil_id="v1",
            aktion=VentilAktion.OEFFNEN, dauer_sekunden=0,
            ausloser=Ausloser.AUTOMATIK,
        ),
        VentilEreignis(
            zeitstempel=start + timedelta(minutes=dauer_minuten),
            zone_id="zone-1", ventil_id="v1",
            aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=dauer_minuten * 60, ausloser=Ausloser.AUTOMATIK,
        ),
    ]


@pytest.mark.asyncio
async def test_t0378_kritisch_ueberspringt_pause_wenn_karenz_abgelaufen():
    """Der Kern von T-0378: Feuchte unter `feuchte_kritisch`, min_pause laeuft
    noch -- aber der letzte echte Lauf liegt ausserhalb der Versickerungs-
    Karenz. Dann darf gegossen werden, und das Flag sagt es.

    Konstellation wie hecke: Pause 240 min, Karenz 3 h, Lauf 30 min.
    Lauf-Ende bei t0+30 -> Karenz bis t0+210; Pause bis t0+240.
    Bei t0+215 ist die Karenz abgelaufen, die Pause nicht -> Bypass.
    """
    zone = _zone_pause_bypass(min_pause_minuten=240, karenz_h=3)
    motor, _ = _motor(
        zone, feuchte=15.0,
        heutige_ereignisse=_lauf(vor_minuten=215, dauer_minuten=30),
    )
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.blocker_typ != BlockerTyp.PAUSE_AKTIV, (
        "kritische Trockenheit muss die min_pause schlagen"
    )
    assert empf.pause_bypass_kritisch_aktiv is True, (
        "der Bypass muss fuer die UI sichtbar sein (Gegenstueck zu "
        "budget_notreserve_aktiv)"
    )


@pytest.mark.asyncio
async def test_t0378_karenz_guard_verhindert_doppeldosis():
    """Gegenprobe -- der Guard ist der eigentliche Schutz: derselbe kritische
    Wert, aber der letzte Lauf liegt INNERHALB der Karenz. Dann NICHT giessen.

    Ohne diesen Guard waere der Bypass eine sofortige Doppel-Dose: ein traeger
    Sensor liest nach einem echten Lauf noch ~1 h lang "kritisch"
    (Memory domain_giesswirkung_sensor_verzoegerung).

    T-0414: Fenster ist jetzt `pause_bypass_karenz_stunden` (1 h ab
    SCHLIESSEN). Der Lauf endete vor 10 min -> mitten in der Karenz.
    (Vorher stand hier ein Lauf von vor 100 min -- der lag in der alten
    3-h-Karenz, faellt aber aus der neuen 1-h-Karenz heraus.)
    """
    zone = _zone_pause_bypass(min_pause_minuten=240, karenz_h=3)
    motor, _ = _motor(
        zone, feuchte=15.0,
        heutige_ereignisse=_lauf(vor_minuten=40, dauer_minuten=30),
    )
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.blocker_typ == BlockerTyp.PAUSE_AKTIV
    assert empf.pause_bypass_kritisch_aktiv is False


@pytest.mark.asyncio
async def test_t0378_nicht_kritisch_bleibt_blockiert():
    """Ueber `feuchte_kritisch` bleibt die min_pause unangetastet -- der
    Bypass ist eine Ausnahme fuer echten Trockenstress, kein Freibrief."""
    zone = _zone_pause_bypass(min_pause_minuten=240, karenz_h=3)
    motor, _ = _motor(
        zone, feuchte=25.0,  # > feuchte_kritisch 20
        heutige_ereignisse=_lauf(vor_minuten=215, dauer_minuten=30),
    )
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.blocker_typ == BlockerTyp.PAUSE_AKTIV
    assert empf.pause_bypass_kritisch_aktiv is False


@pytest.mark.asyncio
async def test_t0414_bypass_wirkt_jetzt_auch_bei_kurzer_pause():
    """T-0414: DER FIX. Vorher war der Bypass bei `min_pause <= 3 h` tot,
    weil der Guard `versickerungs_karenz_stunden` (3 h, ab SCHLIESSEN)
    nutzte und damit die Pause (ab OEFFNEN) immer ueberlebte.

    Konstellation bambuswald: Pause 120 min, Dose 30 min.
    Close bei t0+30 -> Bypass-Karenz (1 h) bis t0+90; Pause bis t0+120.
    Bei t0+110 ist die Karenz abgelaufen, die Pause nicht -> Bypass.
    Mit der alten 3-h-Karenz (bis t0+210) war hier NIE ein Fenster.
    """
    zone = _zone_pause_bypass(
        min_pause_minuten=120, karenz_h=3, bypass_karenz_h=1.0,
    )
    motor, _ = _motor(
        zone, feuchte=15.0,
        heutige_ereignisse=_lauf(vor_minuten=110, dauer_minuten=30),
    )
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.blocker_typ != BlockerTyp.PAUSE_AKTIV
    assert empf.pause_bypass_kritisch_aktiv is True


@pytest.mark.asyncio
async def test_t0414_lange_dose_behaelt_runaway_schutz():
    """Die Gegenprobe zum Fix -- T-0414 darf den Runaway-Schutz nicht
    aufweichen. Nach einer LANGEN Dose liegt das Karenz-Ende weiterhin
    hinter dem Pause-Ende, also kein Bypass.

    bambuswald mit 90-min-Dose: Close bei t0+90, Bypass-Karenz bis t0+150,
    Pause bis t0+120. Bei t0+110 greift die Karenz -> blockiert.
    Das ist gewollt: nach 90 min Wasser sofort nachzulegen, weil ein traeger
    Sensor noch "kritisch" liest, ist genau die Doppel-Dose, die der Guard
    verhindern soll.
    """
    zone = _zone_pause_bypass(
        min_pause_minuten=120, karenz_h=3, bypass_karenz_h=1.0,
    )
    motor, _ = _motor(
        zone, feuchte=15.0,
        heutige_ereignisse=_lauf(vor_minuten=110, dauer_minuten=90),
    )
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.blocker_typ == BlockerTyp.PAUSE_AKTIV
    assert empf.pause_bypass_kritisch_aktiv is False


@pytest.mark.asyncio
async def test_t0414_bypass_karenz_ist_unabhaengig_von_versickerung():
    """Die beiden Karenzen duerfen nicht wieder verschmelzen: eine Zone mit
    6 h Versickerungs-Karenz (waldblumenhain/magerwiese) muss trotzdem das
    1-h-Bypass-Fenster bekommen. Faellt jemand auf
    `versickerungs_karenz_stunden` zurueck, schlaegt dieser Test an.
    """
    zone = _zone_pause_bypass(
        min_pause_minuten=120, karenz_h=6, bypass_karenz_h=1.0,
    )
    motor, _ = _motor(
        zone, feuchte=15.0,
        heutige_ereignisse=_lauf(vor_minuten=110, dauer_minuten=30),
    )
    empf = await motor.vorhersage_zone("zone-1")
    assert empf.pause_bypass_kritisch_aktiv is True
