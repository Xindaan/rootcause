"""T-0444 (Stufe 2): Tagesbudget-Warnung im MANUELLEN Bewaesserungs-Pfad.

Bis dahin prueften `/api/ventil/manuell-start` und `/api/ventil/pre-soak-start`
nur das Hahn-Cluster-Budget (gleichzeitige l/min). `tages_budget_sekunden`
lebte ausschliesslich im Automatik-Motor -- Andres drei Laeufe in 14 h
(74 + 30 + 90 min) liefen ohne jede Gegenrechnung durch.

Entschieden: KEIN harter Block. Der manuelle Pfad ist der Weg, auf dem
bewusst mehr gegossen wird; er soll nur sichtbar machen, was er tut.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import bewaesserung.api_server as api_server
from bewaesserung.api_server import _baue_budget_warnung, app, konfiguriere_api
from bewaesserung.modelle import (
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import HahnLockEntscheidung


def _run(coro):
    return asyncio.run(coro)


# --- Reine Rechnung -------------------------------------------------------


def _zone(budget_s: float = 3600.0) -> ZonenKonfig:
    return ZonenKonfig(
        zone_id="bambuswald", name="Bambus", ventil_kanal=2,
        tages_budget_sekunden=budget_s,
    )


def test_budget_warnung_fehlt_wenn_summe_unter_budget():
    assert _baue_budget_warnung(_zone(3600.0), 1200.0, 1800.0) is None


def test_budget_warnung_fehlt_bei_punktgenauer_ausschoepfung():
    """Grenze inklusiv: genau das Budget ist noch keine Ueberschreitung."""
    assert _baue_budget_warnung(_zone(3600.0), 1800.0, 1800.0) is None


def test_budget_warnung_bei_ueberschreitung():
    warnung = _baue_budget_warnung(_zone(3600.0), 1800.0, 1801.0)
    assert warnung is not None
    assert warnung["tages_budget_sekunden"] == 3600.0
    assert warnung["verbraucht_sekunden"] == 1800.0
    assert warnung["geplant_sekunden"] == 1801.0
    assert warnung["text"]


def test_budget_warnung_zaehlt_den_geplanten_lauf_mit():
    """Der Realfall: das Budget reisst erst MIT dem neuen Lauf.

    Waere nur der bisherige Verbrauch geprueft, bliebe genau der Fall stumm,
    um den es geht -- der Lauf, der die Grenze ueberschreitet.
    """
    assert _baue_budget_warnung(_zone(3600.0), 3000.0, 0.0) is None
    assert _baue_budget_warnung(_zone(3600.0), 3000.0, 1200.0) is not None


def test_budget_warnung_ignoriert_kritisch_faktor():
    """Der `tages_budget_kritisch_faktor` ist ein Automatik-Konzept.

    Er hebt das Budget bei kritischer Trockenheit an (Runaway-Schutz mit
    Bypass). Im manuellen Pfad gibt es keine Kritisch-Klassifikation, also
    wird gegen das BASIS-Budget gerechnet.
    """
    zone = _zone(3600.0).model_copy(
        update={"tages_budget_kritisch_faktor": 3.0},
    )
    assert _baue_budget_warnung(zone, 3000.0, 1200.0) is not None


def test_budget_warnung_aus_wenn_kein_budget_gesetzt():
    assert _baue_budget_warnung(_zone(0.0), 9999.0, 9999.0) is None


# --- T-0452: eigene Advisory-Schwelle unter dem Budget --------------------


def _zone_mit_anteil(budget_s: float, anteil: float) -> ZonenKonfig:
    return _zone(budget_s).model_copy(
        update={"tages_advisory_anteil": anteil},
    )


def test_t0452_der_anlassfall_meldet_jetzt():
    """Der Fall, wegen dem T-0452 existiert: 194 min bei Budget 300 min.

    bambuswald am 28.07.: 74 + 30 min gelaufen, 90 min geplant. Gegen das
    Budget (18000 s) blieb das stumm, weil das Budget bewusst als
    Runaway-Notbremse weit ueber dem legitimen Bedarf liegt. Gegen die
    Warnschwelle (0.6 x 300 = 180 min) meldet es.
    """
    zone = _zone_mit_anteil(18000.0, 0.6)
    warnung = _baue_budget_warnung(zone, (74 + 30) * 60.0, 90 * 60.0)
    assert warnung is not None
    assert warnung["advisory_schwelle_sekunden"] == 10800.0
    # Kernpunkt: das BUDGET ist dabei nicht ueberschritten (194 < 300).
    assert warnung["budget_ueberschritten"] is False
    assert "Warnschwelle erreicht" in warnung["text"]
    assert "Tagesbudget ueberschritten" not in warnung["text"]


def test_t0452_text_nennt_budget_wenn_budget_wirklich_reisst():
    """Gegenprobe: oberhalb des Budgets muss der Text das auch sagen.

    Sonst laesst sich im Log/UI nicht mehr unterscheiden, ob ein Runaway
    laeuft oder nur bewusst viel gegossen wird.
    """
    zone = _zone_mit_anteil(18000.0, 0.6)
    warnung = _baue_budget_warnung(zone, 18000.0, 60.0)
    assert warnung is not None
    assert warnung["budget_ueberschritten"] is True
    assert "Tagesbudget ueberschritten" in warnung["text"]


def test_t0452_default_anteil_verhaelt_sich_wie_vor_der_aenderung():
    """Ohne Config-Eintrag (Anteil 1.0) bleibt die Schwelle das Budget."""
    zone = _zone(3600.0)
    assert zone.tages_advisory_anteil == 1.0
    assert _baue_budget_warnung(zone, 1800.0, 1800.0) is None
    warnung = _baue_budget_warnung(zone, 1800.0, 1801.0)
    assert warnung is not None
    assert warnung["advisory_schwelle_sekunden"] == 3600.0
    assert warnung["budget_ueberschritten"] is True


def test_t0452_anteil_grenze_ist_inklusiv():
    """Genau auf der Warnschwelle ist noch keine Warnung -- wie beim Budget."""
    zone = _zone_mit_anteil(18000.0, 0.6)
    assert _baue_budget_warnung(zone, 10800.0, 0.0) is None
    assert _baue_budget_warnung(zone, 10800.0, 1.0) is not None


def test_t0452_unsinnige_anteile_werden_geklemmt():
    """0/negativ wuerde bei JEDEM Start warnen, >1 die Warnung verstecken.

    Beides macht das Advisory wertlos -- einmal durch Abstumpfung, einmal
    durch Stille. Ein Tippfehler in der YAML darf das nicht ausloesen.
    """
    # Anteil 0 -> faellt auf das Budget zurueck, warnt NICHT bei jedem Start.
    assert _baue_budget_warnung(_zone_mit_anteil(3600.0, 0.0), 60.0, 60.0) is None
    assert _baue_budget_warnung(_zone_mit_anteil(3600.0, -2.0), 60.0, 60.0) is None
    # Anteil > 1 -> auf 1.0 geklemmt, Warnung bleibt am Budget haengen.
    zone_hoch = _zone_mit_anteil(3600.0, 5.0)
    assert _baue_budget_warnung(zone_hoch, 3600.0, 0.0) is None
    warnung = _baue_budget_warnung(zone_hoch, 3600.0, 1.0)
    assert warnung is not None
    assert warnung["advisory_schwelle_sekunden"] == 3600.0


# --- Endpoints ------------------------------------------------------------


class _SicherungAttrappe:
    def __init__(self):
        self.gestartet: list[tuple] = []

    async def pruefe_hahn_lock(self, _kanal):
        return HahnLockEntscheidung(erlaubt=True)

    async def bewaessere(self, kanal, zone_ids, dauer_s, ausloser):
        self.gestartet.append((kanal, tuple(zone_ids), dauer_s, ausloser))
        return True


class _MotorAttrappe:
    def __init__(self, verbrauch_s: float):
        self.verbrauch_s = verbrauch_s

    async def tagesverbrauch(self, _zone_id: str) -> float:
        return self.verbrauch_s


def _konfig(budget_s: float) -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="t", client_secret="t"),
        zonen=[
            ZonenKonfig(
                zone_id="bambuswald", name="Bambus",
                ventil_geraet_id="dswc-1", ventil_kanal=2,
                tages_budget_sekunden=budget_s,
                pre_soak_min=5, pre_soak_pause_min=25,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(id="o", breite=52.52, laenge=13.405)],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
    )


@pytest.fixture
def umgebung(tmp_path):
    """Liefert einen Builder: (budget_s, verbrauch_s) -> (client, sicherung)."""
    speicher = Speicher(str(tmp_path / "budget.db"))
    _run(speicher.verbinden())
    clients: list[TestClient] = []

    def baue(budget_s: float, verbrauch_s: float):
        sicherung = _SicherungAttrappe()
        konfiguriere_api(
            speicher,
            _konfig(budget_s),
            _MotorAttrappe(verbrauch_s),  # type: ignore[arg-type]
            MagicMock(),
            ventil_sicherungen={"dswc-1": sicherung},  # type: ignore[dict-item]
        )
        c = TestClient(app)
        clients.append(c)
        return c, sicherung

    try:
        yield baue
    finally:
        for c in clients:
            c.close()
        _run(speicher.schliessen())


def test_manuell_start_warnt_bei_budget_ueberschreitung(umgebung):
    # Budget 60 min, heute schon 50 min gelaufen, jetzt weitere 30 min.
    c, sicherung = umgebung(3600.0, 3000.0)
    r = c.post(
        "/api/ventil/manuell-start",
        json={"zone_id": "bambuswald", "dauer_sekunden": 1800},
    )
    daten = r.json()

    assert daten["ok"] is True, "Warnung ist kein Block -- der Lauf startet"
    assert sicherung.gestartet, "das Ventil muss trotzdem geoeffnet werden"
    warnung = daten["budget_warnung"]
    assert warnung["tages_budget_sekunden"] == 3600.0
    assert warnung["verbraucht_sekunden"] == 3000.0
    assert warnung["geplant_sekunden"] == 1800.0


def test_manuell_start_ohne_ueberschreitung_ohne_warnung(umgebung):
    c, sicherung = umgebung(3600.0, 600.0)
    r = c.post(
        "/api/ventil/manuell-start",
        json={"zone_id": "bambuswald", "dauer_sekunden": 1800},
    )
    daten = r.json()

    assert daten["ok"] is True
    assert "budget_warnung" not in daten
    assert sicherung.gestartet


def test_manuell_start_budget_advisory_bricht_bei_motor_fehler_nicht_ab(umgebung):
    """Ein Advisory darf einen manuellen Start nie verhindern."""
    c, sicherung = umgebung(3600.0, 3000.0)

    class _KaputterMotor:
        async def tagesverbrauch(self, _zone_id):
            raise RuntimeError("DB weg")

    api_server._motor = _KaputterMotor()  # type: ignore[assignment]
    r = c.post(
        "/api/ventil/manuell-start",
        json={"zone_id": "bambuswald", "dauer_sekunden": 1800},
    )
    daten = r.json()

    assert daten["ok"] is True
    assert "budget_warnung" not in daten
    assert sicherung.gestartet


def test_pre_soak_start_rechnet_puls_plus_hauptdose(umgebung):
    """Geplant ist die WASSERZEIT: Vorwaesser-Puls + Hauptdose.

    Die Soak-Pause dazwischen ist ventil-zu und verbraucht nichts -- sie
    darf das Budget nicht belasten.
    """
    # Budget 60 min, heute 20 min gelaufen, geplant 5 + 45 = 50 min -> 70 > 60.
    c, _ = umgebung(3600.0, 1200.0)

    class _PsManagerAttrappe:
        def __init__(self):
            self.starts: list[dict] = []

        def aktiver_lauf_fuer_kanal_der_zone(self, _zone_id):
            return None

        async def starte(self, **kwargs):
            self.starts.append(kwargs)
            return True, None

    ps = _PsManagerAttrappe()
    api_server._pre_soak_managers = {"dswc-1": ps}
    api_server._pre_soak_manager = ps

    r = c.post(
        "/api/ventil/pre-soak-start",
        json={
            "zone_id": "bambuswald", "pre_soak_min": 5,
            "pause_min": 25, "haupt_min": 45,
        },
    )
    daten = r.json()

    assert daten["ok"] is True
    assert ps.starts, "die Sequenz muss trotz Warnung starten"
    warnung = daten["budget_warnung"]
    assert warnung["geplant_sekunden"] == 50 * 60
    assert warnung["verbraucht_sekunden"] == 1200.0


def test_pre_soak_start_ohne_ueberschreitung_ohne_warnung(umgebung):
    c, _ = umgebung(7200.0, 1200.0)

    class _PsManagerAttrappe:
        def aktiver_lauf_fuer_kanal_der_zone(self, _zone_id):
            return None

        async def starte(self, **kwargs):
            return True, None

    ps = _PsManagerAttrappe()
    api_server._pre_soak_managers = {"dswc-1": ps}
    api_server._pre_soak_manager = ps

    r = c.post(
        "/api/ventil/pre-soak-start",
        json={
            "zone_id": "bambuswald", "pre_soak_min": 5,
            "pause_min": 25, "haupt_min": 45,
        },
    )
    daten = r.json()

    assert daten["ok"] is True
    assert "budget_warnung" not in daten
