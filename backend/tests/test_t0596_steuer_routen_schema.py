"""T-0596: Schema und Obergrenzen fuer manuell-start und pre-soak-start.

Befund (Gesamtreview HW-7/API-2): beide Routen pruefen nur die Untergrenze.
`manuell-start` mit `dauer_sekunden=604800` startete das Ventil (200 OK), die
Attrappe erhielt 604800 s; falsche Typen endeten als HTTP 500. Der
Cloud-Override deckelt NICHT bei 3600 s (Log 29.08.: Close nach 5396 s).

Jeder Ablehnungsfall prueft die URSACHE (Fehlertyp bzw. Meldung im 422),
nicht nur den Status, und dass kein Start bei der Attrappe ankam.
"""
from __future__ import annotations

import json

import pytest

import bewaesserung.api_server as api_server
from bewaesserung.modelle import Ausloser
from bewaesserung.speicher import Speicher
from bewaesserung.ventil_sicherung import VENTIL_MAX_DAUER_S, VentilSicherung
from test_api_tagesbudget_manuell import umgebung  # noqa: F401  (Fixture)
from test_ventil_sicherung import ClientAttrappe


def _post_roh(client, pfad: str, roh: str):
    """JSON-Text direkt senden (fuer NaN, das `json=` nicht erzeugt)."""
    return client.post(
        pfad, content=roh, headers={"Content-Type": "application/json"},
    )


def _fehlertext(r) -> str:
    return json.dumps(r.json().get("detail"), ensure_ascii=False)


# --- manuell-start -----------------------------------------------------------

@pytest.mark.parametrize(
    ("roh", "ursache"),
    [
        # AC 3: der Repro-Fall aus dem Review (eine Woche).
        ('{"zone_id": "bambuswald", "dauer_sekunden": 604800}', "Obergrenze"),
        # Knapp ueber der UI-Grenze von 180 min.
        ('{"zone_id": "bambuswald", "dauer_sekunden": 10801}', "Obergrenze"),
        ('{"zone_id": "bambuswald", "dauer_sekunden": -5}', "greater_than"),
        # NaN ist mit und ohne strict ein Fehler; geprueft wird, dass er als
        # 422 am Feld ankommt (vorher: 500 bzw. stille Umdeutung).
        ('{"zone_id": "bambuswald", "dauer_sekunden": NaN}', "dauer_sekunden"),
        ('{"zone_id": "bambuswald", "dauer_sekunden": "600"}', "int_type"),
        ('{"zone_id": "gibt_es_nicht", "dauer_sekunden": 60}', "Unbekannte Zone"),
    ],
    ids=["woche", "ueber_180min", "negativ", "nan", "string", "zone_unbekannt"],
)
def test_manuell_start_lehnt_ungueltige_bodies_mit_422_ab(umgebung, roh, ursache):  # noqa: F811
    c, sicherung = umgebung(0.0, 0.0)
    r = _post_roh(c, "/api/ventil/manuell-start", roh)

    assert r.status_code == 422, (r.status_code, r.text)
    assert ursache in _fehlertext(r), _fehlertext(r)
    assert sicherung.gestartet == [], "Ventil darf nicht starten"


def test_manuell_start_erlaubt_die_ui_grenze_von_180_min(umgebung):  # noqa: F811
    c, sicherung = umgebung(0.0, 0.0)
    r = c.post(
        "/api/ventil/manuell-start",
        json={"zone_id": "bambuswald", "dauer_sekunden": 180 * 60},
    )
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert [g[2] for g in sicherung.gestartet] == [10800]


def test_manuell_start_groesseres_max_dauer_der_zone_hebt_die_grenze(umgebung):  # noqa: F811
    """Die 180 min sind ein Boden: erlaubt die Zone mehr, gilt die Zone."""
    c, sicherung = umgebung(0.0, 0.0)
    api_server._laufzeit.konfig.zonen[0].max_dauer_sekunden = 4 * 3600
    r = c.post(
        "/api/ventil/manuell-start",
        json={"zone_id": "bambuswald", "dauer_sekunden": 4 * 3600},
    )
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert [g[2] for g in sicherung.gestartet] == [4 * 3600]


# --- pre-soak-start ----------------------------------------------------------

class _PsManagerAttrappe:
    def __init__(self):
        self.starts: list[dict] = []

    def aktiver_lauf_fuer_kanal_der_zone(self, _zone_id):
        return None

    async def starte(self, **kwargs):
        self.starts.append(kwargs)
        return True, None


def _mit_ps_attrappe() -> _PsManagerAttrappe:
    ps = _PsManagerAttrappe()
    api_server._laufzeit.pre_soak_managers = {"dswc-1": ps}
    api_server._laufzeit.pre_soak_manager = ps
    return ps


@pytest.mark.parametrize(
    ("roh", "ursache"),
    [
        ('{"zone_id": "bambuswald", "haupt_min": 100000}', "Obergrenze"),
        ('{"zone_id": "bambuswald", "haupt_min": 241}', "Obergrenze"),
        ('{"zone_id": "bambuswald", "haupt_min": NaN}', "haupt_min"),
        ('{"zone_id": "bambuswald", "haupt_min": 60, "pre_soak_min": 31}',
         "less_than_equal"),
        ('{"zone_id": "bambuswald", "haupt_min": 60, "pause_min": -1}',
         "greater_than"),
        ('{"zone_id": "bambuswald", "haupt_min": 60, "haupt_pulse": 0}',
         "greater_than_equal"),
        ('{"zone_id": "gibt_es_nicht", "haupt_min": 60}', "Unbekannte Zone"),
    ],
    ids=[
        "haupt_riesig", "haupt_ueber_240", "haupt_nan", "vor_ueber_30",
        "pause_negativ", "pulse_null", "zone_unbekannt",
    ],
)
def test_pre_soak_start_lehnt_ungueltige_bodies_mit_422_ab(umgebung, roh, ursache):  # noqa: F811
    c, _ = umgebung(0.0, 0.0)
    ps = _mit_ps_attrappe()
    r = _post_roh(c, "/api/ventil/pre-soak-start", roh)

    assert r.status_code == 422, (r.status_code, r.text)
    assert ursache in _fehlertext(r), _fehlertext(r)
    assert ps.starts == [], "Sequenz darf nicht starten"


def test_pre_soak_start_erlaubt_die_ui_grenze_von_240_min(umgebung):  # noqa: F811
    c, _ = umgebung(0.0, 0.0)
    ps = _mit_ps_attrappe()
    r = c.post(
        "/api/ventil/pre-soak-start",
        json={"zone_id": "bambuswald", "haupt_min": 240},
    )
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert [s["haupt_min"] for s in ps.starts] == [240]


# --- letzte Schranke in VentilSicherung ------------------------------------

@pytest.mark.parametrize(
    ("dauer_s", "startet"),
    [(VENTIL_MAX_DAUER_S, True), (VENTIL_MAX_DAUER_S + 1, False)],
    ids=["grenze", "ueber_grenze"],
)
async def test_bewaessere_lehnt_dauer_ueber_der_obergrenze_ab(
    tmp_path, dauer_s, startet,
):
    """Faengt jeden Pfad ab, der an den Routen vorbeikommt (Pre-Soak-Tick,
    kuenftige Aufrufer). Gilt fuer jeden Ausloser."""
    speicher = Speicher(str(tmp_path / "t0596.db"))
    await speicher.verbinden()
    client = ClientAttrappe()
    sicherung = VentilSicherung(
        client=client,  # type: ignore[arg-type]
        speicher=speicher,
        ventil_geraet_id="ventil-001",
    )
    try:
        ok = await sicherung.bewaessere(1, ["z"], dauer_s, Ausloser.MANUELL)
        assert ok is startet
        assert [d for _, d, _ in client.oeffnen_aufrufe] == (
            [dauer_s] if startet else []
        )
    finally:
        if sicherung.ist_aktiv(1):
            await sicherung.stoppe(1, Ausloser.MANUELL)
        await speicher.schliessen()
