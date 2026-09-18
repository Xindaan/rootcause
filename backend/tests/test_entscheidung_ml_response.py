"""T-0065: Integrations-Tests fuer die ML-Weiche in _berechne_dauer."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta


from bewaesserung.entscheidung import Entscheidungsmotor
from bewaesserung.modelle import (
    MlBewaesserungsResponseKonfig,
    SensorMessung,
    WetterStunde,
    ZeitFenster,
    ZonenKonfig,
    ZonenModus,
)
from bewaesserung.wetter import WetterVorhersage

JETZT = datetime(2026, 4, 22, 6, 0)


# --- Attrappen ---


class SpeicherDauerAttrappe:
    """Minimale Speicher-Attrappe fuer _dauer_mit_ml_weiche-Tests."""

    def __init__(self):
        self.messungen: dict[str, list[SensorMessung]] = {}
        self.gespeicherte_entscheidungen = []
        self.vorschlaege: list[dict] = []

    async def hole_messungen(self, zone_id, von=None, bis=None):
        msgs = self.messungen.get(zone_id, [])
        out = []
        for m in msgs:
            if von and m.zeitstempel < von:
                continue
            if bis and m.zeitstempel > bis:
                continue
            out.append(m)
        return sorted(out, key=lambda m: m.zeitstempel, reverse=True)

    async def letzte_messung(self, zone_id):
        m = await self.hole_messungen(zone_id)
        return m[0] if m else None

    async def letzte_messung_aggregiert(
        self, zone_id, fenster_minuten=90, jetzt=None,
    ):
        # T-0179c: Mock delegiert auf letzte_messung
        return await self.letzte_messung(zone_id)

    async def letzte_messungen_pro_geraet(
        self, zone_id, fenster_minuten=90, jetzt=None,
    ):
        m = await self.letzte_messung(zone_id)
        return [m] if m else []

    async def letztes_ventil_ereignis(self, zone_id):
        return None

    async def hole_ventil_ereignisse(self, zone_id, von=None, bis=None):
        return []

    async def letztes_bestaetigtes_ventil_ereignis(self, zone_id, ausloser_ausser=None):
        return None

    async def ventil_ereignisse_heute(self, zone_id):
        return []

    async def speichere_entscheidung(self, entscheidung):
        self.gespeicherte_entscheidungen.append(entscheidung)

    async def hole_entscheidungen(self, zone_id=None, limit=50):
        return []

    async def speichere_wetter(self, *args, **kwargs):
        return None

    async def speichere_dauer_vorschlag(
        self, *, zeitstempel, zone_id, f_vor, ziel_schwelle,
        heuristik_s, ml_s, ml_modell_version, features_json, modus,
    ) -> int:
        self.vorschlaege.append({
            "zeitstempel": zeitstempel, "zone_id": zone_id,
            "f_vor": f_vor, "ziel_schwelle": ziel_schwelle,
            "heuristik_s": heuristik_s, "ml_s": ml_s,
            "ml_modell_version": ml_modell_version,
            "features_json": features_json, "modus": modus,
        })
        return len(self.vorschlaege)


class WetterClientAttrappe:
    def __init__(self, vorhersage):
        self._v = vorhersage
        self.regen_schwelle_mm = 2.0

    async def hole_vorhersage(self):
        return self._v


class WetterManagerAttrappe:
    def __init__(self, vorhersage):
        self._c = WetterClientAttrappe(vorhersage)

    @property
    def standard_client(self):
        return self._c

    def hole_client(self, standort_id):
        return self._c


class ResponseServiceAttrappe:
    """Minimaler Fake des MLResponseService fuer die Entscheidungs-Tests."""

    def __init__(self, dauer: int | None, version: str = "v-test"):
        self._dauer = dauer
        self._version = version
        self.aufrufe: list[dict] = []

    def lade_zone(self, zone_id, *, force=False) -> bool:
        return True

    def version(self, zone_id):
        return self._version

    def inverse_dauer(self, *, zone_id, f_vor, ziel_schwelle, **kw):
        self.aufrufe.append({
            "zone_id": zone_id, "f_vor": f_vor, "ziel_schwelle": ziel_schwelle,
            **kw,
        })
        return self._dauer


# --- Fixtures ---


def _vorhersage(et0_pro_stunde: float = 0.3) -> WetterVorhersage:
    return WetterVorhersage(
        abfrage_zeitstempel=JETZT,
        stunden=[
            WetterStunde(
                zeitstempel=JETZT + timedelta(hours=i + 1),
                temperatur=18.0, niederschlag_mm=0.0,
                niederschlag_wahrscheinlichkeit=0.0, wind_kmh=8.0,
                et0_mm=et0_pro_stunde,
            )
            for i in range(48)
        ],
    )


def _zone(ml_dosis_opt_in: bool = False) -> ZonenKonfig:
    return ZonenKonfig(
        zone_id="waldblumenhain", name="Waldblumenhain",
        modus=ZonenModus.AUTOMATIK,
        # T-0512: die ML-Dosis ist seit 05.08. dreistufig -- global `wirksam`
        # UND `ml_dosis_opt_in` je Zone. Default hier False, damit die
        # Bestandstests weiter das Shadow-Verhalten pruefen.
        ml_dosis_opt_in=ml_dosis_opt_in,
        feuchte_schwelle_min=45.0, feuchte_kritisch=20.0,
        max_dauer_sekunden=1800, min_pause_minuten=120,
        tages_budget_sekunden=3600.0,
        bevorzugte_zeiten=[ZeitFenster(von="05:00", bis="07:00")],
    )


def _messung(jetzt, feuchte) -> SensorMessung:
    return SensorMessung(
        zeitstempel=jetzt, zone_id="waldblumenhain",
        boden_feuchte=feuchte, boden_temperatur=14.0,
        umgebungs_temperatur=18.0, licht_intensitaet=1000.0,
        batterie_prozent=90.0,
    )


def _baue_motor(
    zone_ml_opt_in: bool = False,*, response_konfig, response_service=None, feuchte=25.0):
    zone = _zone(ml_dosis_opt_in=zone_ml_opt_in)
    speicher = SpeicherDauerAttrappe()
    speicher.messungen[zone.zone_id] = [_messung(JETZT, feuchte)]
    wm = WetterManagerAttrappe(_vorhersage())
    motor = Entscheidungsmotor(
        speicher, wm, [zone],
        response_konfig=response_konfig,
        response_service=response_service,
    )
    motor._jetzt = lambda: JETZT
    return motor, speicher, zone


def _run(coro):
    return asyncio.run(coro)


# --- Tests ---


def test_ohne_aktiv_keine_db_row_nur_heuristik():
    motor, speicher, _ = _baue_motor(
        response_konfig=MlBewaesserungsResponseKonfig(aktiv=False),
    )
    e = _run(motor.pruefe_zone("waldblumenhain"))
    assert e.soll_bewaessern is True
    assert speicher.vorschlaege == []
    # Heuristik: 20 pp * 60s * (1 + 1.8/6) ≈ 1560s, geclippt auf 1800.
    assert e.dauer_sekunden > 0


def test_aktiv_shadow_persistiert_beide_empfehlungen():
    svc = ResponseServiceAttrappe(dauer=600)
    motor, speicher, _ = _baue_motor(
        response_konfig=MlBewaesserungsResponseKonfig(aktiv=True, wirksam=False),
        response_service=svc,
    )
    e = _run(motor.pruefe_zone("waldblumenhain"))
    assert len(speicher.vorschlaege) == 1
    row = speicher.vorschlaege[0]
    assert row["modus"] == "shadow"
    assert row["ml_s"] == 600
    assert row["heuristik_s"] > 0
    # Shadow: Heuristik wird produktiv benutzt.
    assert e.dauer_sekunden == row["heuristik_s"]
    assert row["ml_modell_version"] == "v-test"


def test_globales_wirksam_allein_reicht_nicht_mehr():
    """T-0512: `wirksam: true` OHNE Zonen-Opt-in laesst die Heuristik stehen.

    Dieser Test hiess bis 05.08. `test_wirksam_nutzt_ml_dauer_clip_auf_max_dauer`
    und sicherte zu, dass der globale Schalter genuegt. Genau das ist jetzt
    anders: die ML-Dosis wird pro Zone freigeschaltet, weil die Shadow-Reihe
    pro Zone gegensaetzlich ausfaellt (bambuswald ML besser, hecke schlechter).
    Wer nur global schaltet, bekommt das alte Verhalten -- nicht ML ueberall.
    """
    svc = ResponseServiceAttrappe(dauer=9999)
    motor, speicher, zone = _baue_motor(
        response_konfig=MlBewaesserungsResponseKonfig(aktiv=True, wirksam=True),
        response_service=svc,
    )
    e = _run(motor.pruefe_zone("waldblumenhain"))
    row = speicher.vorschlaege[0]
    assert row["modus"] == "shadow"
    assert e.dauer_sekunden == row["heuristik_s"]


def test_wirksam_nutzt_ml_dauer_clip_auf_max_dauer():
    svc = ResponseServiceAttrappe(dauer=9999)  # > max_dauer_sekunden
    motor, speicher, zone = _baue_motor(
        response_konfig=MlBewaesserungsResponseKonfig(aktiv=True, wirksam=True),
        response_service=svc,
        zone_ml_opt_in=True,
    )
    e = _run(motor.pruefe_zone("waldblumenhain"))
    row = speicher.vorschlaege[0]
    assert row["modus"] == "wirksam"
    # ml_s in DB = geclipte Dauer (die tatsaechlich verwendet wurde).
    assert row["ml_s"] == zone.max_dauer_sekunden
    assert e.dauer_sekunden == zone.max_dauer_sekunden


def test_wirksam_ohne_modell_faellt_auf_heuristik():
    """ML-Service liefert None (kein Modell) → Heuristik, ml_s=None in DB."""
    svc = ResponseServiceAttrappe(dauer=None)
    motor, speicher, _ = _baue_motor(
        response_konfig=MlBewaesserungsResponseKonfig(aktiv=True, wirksam=True),
        response_service=svc,
    )
    e = _run(motor.pruefe_zone("waldblumenhain"))
    row = speicher.vorschlaege[0]
    assert row["ml_s"] is None
    assert row["ml_modell_version"] is None
    assert e.dauer_sekunden == row["heuristik_s"]


def test_features_json_enthaelt_eingaben():
    svc = ResponseServiceAttrappe(dauer=800)
    motor, speicher, _ = _baue_motor(
        response_konfig=MlBewaesserungsResponseKonfig(aktiv=True, wirksam=False),
        response_service=svc,
    )
    _run(motor.pruefe_zone("waldblumenhain"))
    row = speicher.vorschlaege[0]
    payload = json.loads(row["features_json"])
    assert "f_vor" in payload and "ziel_schwelle" in payload
    assert "et0_6h" in payload and "et0_24h" in payload
