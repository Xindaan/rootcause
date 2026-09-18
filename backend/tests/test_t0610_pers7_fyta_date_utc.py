"""T-0610 PERS-7: der FYTA-Live-Poll ueberspringt Eintraege ohne lesbares
`date_utc`, statt einen Zeitstempel zu erfinden.

Vorher bekam ein kaputter Eintrag `datetime.now()`. Das schob die
Dedup-Schwelle auf "jetzt", und die echte Folgemessung (aelter als jetzt)
wurde verworfen. Ein fehlendes Feld bekam "heute 00:00" -- ebenfalls erfunden.
"""
from __future__ import annotations

import httpx
import pytest

from bewaesserung.fyta_client import FytaClient, parse_fyta_zeitstempel
from bewaesserung.modelle import FytaKonfig, FytaPflanzenKonfig
from test_fyta_client import _FakeDate  # heute = 2026-04-10


def _messung(date_utc, feuchte):
    eintrag = {"soil_moisture": feuchte, "temperature": 19, "light": 8,
               "soil_fertility": 0}
    if date_utc is not ...:
        eintrag["date_utc"] = date_utc
    return eintrag


def _fake_client(messungen: list[dict]) -> type:
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, headers, json):
            return httpx.Response(
                200,
                json={"user_plants": [
                    {"user_plant_id": 100001, "measurements": messungen},
                ]},
                request=httpx.Request("POST", url),
            )
    return _Client


async def _poll(monkeypatch, messungen):
    monkeypatch.setattr(
        "bewaesserung.fyta_client.httpx.AsyncClient", _fake_client(messungen),
    )
    monkeypatch.setattr("bewaesserung.fyta_client.date", _FakeDate)
    client = FytaClient(
        FytaKonfig(
            api_url="https://fyta.example/api",
            pflanzen=[FytaPflanzenKonfig(fyta_id=100001, zone_id="zitrus", name="Z")],
        ),
        access_token="token",
    )
    return await client.hole_aktuelle_werte()


@pytest.mark.asyncio
async def test_unlesbares_date_utc_zwischen_zwei_gueltigen(monkeypatch):
    erg = await _poll(monkeypatch, [
        _messung("2026-04-09T22:15:00", 41),
        _messung("kaputt", 99),
        _messung("2026-04-10T08:30:00", 43),
    ])

    assert [(m.zeitstempel, m.boden_feuchte) for m in erg] == [
        (parse_fyta_zeitstempel("2026-04-09T22:15:00"), 41.0),
        (parse_fyta_zeitstempel("2026-04-10T08:30:00"), 43.0),
    ]


@pytest.mark.asyncio
async def test_fehlendes_date_utc_wird_uebersprungen(monkeypatch):
    erg = await _poll(monkeypatch, [
        _messung("2026-04-09T22:15:00", 41),
        _messung(..., 99),  # Feld fehlt ganz
        _messung("2026-04-10T08:30:00", 43),
    ])

    assert [m.boden_feuchte for m in erg] == [41.0, 43.0]


# --- T-0616 W7e: dieselbe Regel im Backfill (CLI `hole_tag` und Start-Hook) ---

_BACKFILL_EINTRAEGE = [
    _messung("2026-05-01T08:00:00", 45),
    _messung(..., 44),          # Feld fehlt: frueher "Tag T00:00"
    _messung("kaputt", 43),     # unlesbar
]


@pytest.mark.asyncio
async def test_backfill_hole_tag_erfindet_keinen_zeitstempel():
    from datetime import date

    from structlog.testing import capture_logs

    from bewaesserung.fyta_backfill import hole_tag

    with capture_logs() as logs:
        messungen = await hole_tag(
            _fake_client(_BACKFILL_EINTRAEGE)(), "token", date(2026, 5, 1),
            "https://fyta.example/api", {100001: "zitrus"},
        )

    assert [m.zeitstempel for m in messungen] == [
        parse_fyta_zeitstempel("2026-05-01T08:00:00"),
    ]
    assert [m.boden_feuchte for m in messungen] == [45.0]
    warnungen = [
        e for e in logs
        if e["event"] == "backfill.zeitstempel_unlesbar_uebersprungen"
    ]
    assert [w["anzahl"] for w in warnungen] == [2]


@pytest.mark.asyncio
async def test_backfill_lueckenfuellung_schreibt_keine_erfundene_messung(
    tmp_path, monkeypatch,
):
    from bewaesserung.fyta_backfill import backfill_lueckenfuellung
    from bewaesserung.speicher import Speicher

    pflanze = FytaPflanzenKonfig(fyta_id=100001, zone_id="zitrus", name="Z")

    class _FytaStub:
        _pflanzen_map = {100001: pflanze}
        _konfig = FytaKonfig(api_url="https://fyta.example/api", pflanzen=[pflanze])
        _token = "token"

        async def _stelle_token_sicher(self) -> bool:
            return True

    monkeypatch.setattr(
        "bewaesserung.fyta_backfill.httpx.AsyncClient",
        _fake_client(_BACKFILL_EINTRAEGE),
    )
    speicher = Speicher(str(tmp_path / "pers7_backfill.db"))
    await speicher.verbinden()
    try:
        importiert, duplikate = await backfill_lueckenfuellung(
            speicher, _FytaStub(), tage_zurueck=0,
        )
        async with speicher._db.execute(
            "SELECT boden_feuchte FROM sensor_messung WHERE zone_id = 'zitrus'",
        ) as cursor:
            werte = [z[0] for z in await cursor.fetchall()]
    finally:
        await speicher.schliessen()

    assert (importiert, duplikate) == (1, 0)
    assert werte == [45.0]
