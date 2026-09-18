"""T-0610 E23: FYTA-Login mit Backoff nach Ablehnung (Muster T-0600/T-0621).

Vorher loggte sich jeder Aufrufer (Polling, Luecken-, Status-, Optimum-Job,
Backfill, Endpoint-Probe) nach einem 401/4xx bei jedem Lauf erneut ein. Ein
"Neustart" ist hier eine zweite FytaClient-Instanz mit derselben Sperrdatei.
Alle HTTP-Clients sind Attrappen (kein Netz); die Uhr ist gestubbt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from structlog.testing import capture_logs

import bewaesserung.fyta_client as fyta_mod
from bewaesserung.fyta_client import FytaClient
from bewaesserung.modelle import FytaKonfig

T0 = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
LOGIN = "fyta@example.com"
PASSWORT = "pw-geheim"


class _Uhr:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def uhr(monkeypatch):
    monkeypatch.delenv("FYTA_ACCESS_TOKEN", raising=False)
    u = _Uhr(T0)
    monkeypatch.setattr(fyta_mod, "_jetzt_utc", u)
    return u


def _http(antwort_status=None, fehler=None):
    class _Antwort:
        status_code = antwort_status
        text = "{}"

        def json(self):
            return {"access_token": "tok", "expires_in": 3600}

    client = AsyncMock()
    if fehler is not None:
        client.post = AsyncMock(side_effect=fehler)
    else:
        client.post = AsyncMock(return_value=_Antwort())
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _client(tmp_path, login=LOGIN) -> FytaClient:
    return FytaClient(
        FytaKonfig(), email=login, password=PASSWORT,
        token_pfad=tmp_path / "token.json",
        sperre_pfad=tmp_path / "sperre.json",
    )


def _versuch(client: FytaClient, status=None, fehler=None) -> int:
    """Ein Token-Sicherstellen wie bei jedem Aufrufer; gibt die Anzahl
    Login-POSTs zurueck."""
    http = _http(status, fehler)
    with patch.object(fyta_mod.httpx, "AsyncClient", return_value=http):
        asyncio.run(client._stelle_token_sicher())
    return http.post.await_count


def _schreibe_sperre(tmp_path, *, login=LOGIN, bis, fehlversuche=1):
    (tmp_path / "sperre.json").write_text(json.dumps({
        "identitaet": hashlib.sha256(login.encode()).hexdigest(),
        "bis": bis.timestamp(), "fehlversuche": fehlversuche, "status": 401,
    }))


@pytest.mark.parametrize("status", [401, 403, 404])
def test_ablehnung_sperrt_auch_nach_neustart_und_eskalation_bleibt(
    tmp_path, uhr, status,
):
    client = _client(tmp_path)
    assert _versuch(client, status) == 1
    assert _versuch(client, status) == 0, "zweiter Aufrufer in der Sperre"
    inhalt = (tmp_path / "sperre.json").read_text()
    assert LOGIN not in inhalt and PASSWORT not in inhalt
    assert (tmp_path / "sperre.json").stat().st_mode & 0o777 == 0o600

    neu = _client(tmp_path)  # simulierter Neustart
    uhr.t = T0 + timedelta(minutes=30) - timedelta(seconds=1)
    assert _versuch(neu, status) == 0, "Sperre aus der Datei muss greifen"
    uhr.t = T0 + timedelta(minutes=30)
    assert _versuch(neu, status) == 1, "nach Ablauf genau ein Versuch"

    # Zweite Ablehnung -> Stufe 2 (60 min), auch ueber einen weiteren Neustart.
    t2 = uhr.t
    uhr.t = t2 + timedelta(minutes=60) - timedelta(seconds=1)
    assert _versuch(_client(tmp_path), status) == 0


def test_429_sperrt_zwei_stunden(tmp_path, uhr):
    client = _client(tmp_path)
    assert _versuch(client, 429) == 1
    uhr.t = T0 + timedelta(hours=2) - timedelta(seconds=1)
    assert _versuch(client, 429) == 0
    uhr.t = T0 + timedelta(hours=2)
    assert _versuch(client, 429) == 1


def test_erfolg_loescht_sperre_und_stufe(tmp_path, uhr):
    client = _client(tmp_path)
    assert _versuch(client, 401) == 1
    uhr.t = T0 + timedelta(minutes=30)
    assert _versuch(client, 200) == 1
    assert not (tmp_path / "sperre.json").exists()

    # Token ungueltig, naechste Ablehnung: wieder Stufe 1 (30 min), auch
    # nach einem Neustart.
    client._token_ablauf = datetime.now() - timedelta(seconds=1)
    assert _versuch(client, 401) == 1
    uhr.t += timedelta(minutes=30)
    neu = _client(tmp_path)
    neu._token_ablauf = datetime.now() - timedelta(seconds=1)
    assert _versuch(neu, 401) == 1


@pytest.mark.parametrize("status", [500, 502, 503])
def test_serverfehler_sperrt_nicht_und_zaehlt_nicht(tmp_path, uhr, status):
    """T-0610 W13b (Entscheid Andre 14.10.): 5xx ist keine Ablehnung. Jeder
    Takt darf erneut einloggen, es entsteht keine Sperrdatei, und ein spaeteres
    401 sperrt danach mit Stufe 1 (30 min), nicht mit einer hochgezaehlten."""
    client = _client(tmp_path)
    assert _versuch(client, status) == 1
    assert _versuch(client, status) == 1, "5xx darf den naechsten Takt nicht sperren"
    assert not (tmp_path / "sperre.json").exists()
    assert _versuch(_client(tmp_path), status) == 1, "auch nicht ueber einen Neustart"

    assert _versuch(client, 401) == 1
    uhr.t = T0 + timedelta(minutes=30)
    assert _versuch(client, 401) == 1, "Stufe 1 nach 5xx-Vorlauf, nicht Stufe 3"


def test_serverfehler_laesst_bestehende_stufe_unveraendert(tmp_path, uhr):
    """Abgelaufene Sperre mit 2 Fehlversuchen: ein 5xx im erlaubten Versuch
    aendert weder die Datei noch die Stufe; das naechste 401 sperrt mit Stufe 3
    (120 min), wie ohne den 5xx-Versuch."""
    _schreibe_sperre(tmp_path, bis=T0 - timedelta(minutes=1), fehlversuche=2)
    vorher = (tmp_path / "sperre.json").read_bytes()
    client = _client(tmp_path)
    assert _versuch(client, 503) == 1
    assert (tmp_path / "sperre.json").read_bytes() == vorher
    assert _versuch(client, 503) == 1

    assert _versuch(client, 401) == 1
    uhr.t = T0 + timedelta(minutes=120) - timedelta(seconds=1)
    assert _versuch(_client(tmp_path), 401) == 0
    uhr.t = T0 + timedelta(minutes=120)
    assert _versuch(_client(tmp_path), 401) == 1


def test_netzfehler_sperrt_nicht(tmp_path, uhr):
    client = _client(tmp_path)
    assert _versuch(client, fehler=httpx.ConnectError("weg")) == 1
    assert _versuch(client, fehler=httpx.ConnectError("weg")) == 1
    assert not (tmp_path / "sperre.json").exists()


@pytest.mark.parametrize("inhalt, ereignis", [
    ("{kein json", "fyta.login_sperre_ungueltig"),
    ('{"identitaet": "x"}', "fyta.login_sperre_ungueltig"),
    ("fremd", "fyta.login_sperre_fremd"),
])
def test_kaputte_oder_fremde_datei_sperrt_nicht_und_warnt(
    tmp_path, uhr, inhalt, ereignis,
):
    if inhalt == "fremd":
        _schreibe_sperre(tmp_path, login="andere@example.com",
                         bis=T0 + timedelta(hours=3))
    else:
        (tmp_path / "sperre.json").write_text(inhalt)
    with capture_logs() as logs:
        client = _client(tmp_path)
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == [ereignis]
    assert _versuch(client, 401) == 1


def test_abgelaufene_sperre_behaelt_die_stufe_mit_deckel(tmp_path, uhr):
    """Abgelaufen -> ein Versuch erlaubt; die Stufe bleibt (40 Fehlversuche,
    ohne Exponent-Kappung OverflowError) und die neue Sperre hat den Deckel."""
    _schreibe_sperre(tmp_path, bis=T0 - timedelta(minutes=1), fehlversuche=40)
    with capture_logs() as logs:
        client = _client(tmp_path)
    assert "fyta.login_sperre_abgelaufen" in [e["event"] for e in logs]
    assert _versuch(client, 401) == 1
    uhr.t = T0 + fyta_mod.LOGIN_SPERRE_MAX - timedelta(seconds=1)
    assert _versuch(_client(tmp_path), 401) == 0
    uhr.t = T0 + fyta_mod.LOGIN_SPERRE_MAX
    assert _versuch(_client(tmp_path), 401) == 1


def test_sperre_weit_in_der_zukunft_wird_auf_den_deckel_gekappt(tmp_path, uhr):
    _schreibe_sperre(tmp_path, bis=T0 + timedelta(days=30))
    client = _client(tmp_path)
    uhr.t = T0 + fyta_mod.LOGIN_SPERRE_MAX - timedelta(seconds=1)
    assert _versuch(client, 401) == 0
    uhr.t = T0 + fyta_mod.LOGIN_SPERRE_MAX
    assert _versuch(client, 401) == 1
