"""T-0621: die Customer-Login-Sperre aus T-0600 uebersteht Neustarts.

Ein "Neustart" ist hier eine ZWEITE GardenaCustomerAuth-Instanz mit derselben
Sperrdatei. Alle HTTP-Clients sind Attrappen (kein Netz); die Uhr ist gestubbt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

import bewaesserung.gardena_customer_auth as auth_mod
from bewaesserung.gardena_customer_auth import (
    GardenaCustomerAuth,
    GardenaCustomerAuthError,
)

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
LOGIN = "u@example.com"
PASSWORT = "pw-geheim"


class _Uhr:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def uhr(monkeypatch):
    u = _Uhr(T0)
    monkeypatch.setattr(auth_mod, "_jetzt_utc", u)
    return u


def _login_client(status):
    class _Antwort:
        status_code = status
        text = "{}"

        def json(self):
            if status == 200:
                return {"access_token": "tok", "expires_in": 864000}
            return {"error": "invalid_grant"}

    client = AsyncMock()
    client.post = AsyncMock(return_value=_Antwort())
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _auth(tmp_path, login=LOGIN):
    return GardenaCustomerAuth(
        login, PASSWORT, tmp_path / "token.json",
        sperre_datei=tmp_path / "sperre.json",
    )


def _versuch(auth, status):
    """Ein Token-Abruf; gibt die Anzahl password-grant-POSTs zurueck."""
    login = _login_client(status)
    with patch.object(auth_mod.httpx, "AsyncClient", return_value=login):
        try:
            asyncio.run(auth.hole_gueltigen_token())
        except GardenaCustomerAuthError:
            pass
    return login.post.await_count


def _schreibe_sperre(tmp_path, *, login=LOGIN, bis, fehlversuche=1):
    (tmp_path / "sperre.json").write_text(json.dumps({
        "identitaet": hashlib.sha256(login.encode()).hexdigest(),
        "bis": bis.timestamp(), "fehlversuche": fehlversuche, "status": 401,
    }))


def test_ablehnung_sperrt_auch_nach_neustart_und_eskalation_bleibt(tmp_path, uhr):
    """AK1: zweite Instanz setzt bis Ablauf keinen POST ab, danach genau einen."""
    assert _versuch(_auth(tmp_path), 401) == 1
    inhalt = (tmp_path / "sperre.json").read_text()
    assert LOGIN not in inhalt and PASSWORT not in inhalt
    assert (tmp_path / "sperre.json").stat().st_mode & 0o777 == 0o600

    neu = _auth(tmp_path)  # simulierter Neustart
    uhr.t = T0 + timedelta(minutes=30) - timedelta(seconds=1)
    assert _versuch(neu, 401) == 0, "Sperre aus der Datei muss greifen"
    uhr.t = T0 + timedelta(minutes=30)
    assert _versuch(neu, 401) == 1, "nach Ablauf genau ein Versuch"

    # Zweite Ablehnung -> Stufe 2 (60 min), auch ueber einen weiteren Neustart.
    t2 = uhr.t
    dritte = _auth(tmp_path)
    uhr.t = t2 + timedelta(minutes=60) - timedelta(seconds=1)
    assert _versuch(dritte, 401) == 0, "Eskalationsstufe muss den Neustart ueberstehen"


def test_erfolg_loescht_die_sperrdatei(tmp_path, uhr):
    """AK2: nach Erfolg startet ein Neustart ohne Sperre und ohne Altstufe."""
    auth = _auth(tmp_path)
    assert _versuch(auth, 401) == 1
    uhr.t = T0 + timedelta(minutes=30)
    assert _versuch(auth, 200) == 1
    assert not (tmp_path / "sperre.json").exists()

    (tmp_path / "token.json").unlink()  # Token weg, naechster Abruf loggt ein
    neu = _auth(tmp_path)
    assert _versuch(neu, 401) == 1
    # Stufe wieder 1: nach 30 min ist der naechste Versuch erlaubt.
    uhr.t += timedelta(minutes=30)
    assert _versuch(_auth(tmp_path), 401) == 1


@pytest.mark.parametrize("inhalt, ereignis", [
    ("{kein json", "gardena_auth.login_sperre_ungueltig"),
    ('{"identitaet": "x"}', "gardena_auth.login_sperre_ungueltig"),
    ("[1, 2]", "gardena_auth.login_sperre_ungueltig"),
])
def test_kaputte_datei_sperrt_nicht_und_warnt(tmp_path, uhr, inhalt, ereignis):
    """AK2: kaputte Datei -> keine Sperre, Warnung, kein Absturz."""
    (tmp_path / "sperre.json").write_text(inhalt)
    with capture_logs() as logs:
        auth = _auth(tmp_path)
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == [ereignis]
    assert _versuch(auth, 401) == 1


def test_fremde_datei_sperrt_nicht_und_warnt(tmp_path, uhr):
    """AK2: Sperre eines anderen Logins gilt hier nicht."""
    _schreibe_sperre(tmp_path, login="andere@example.com",
                     bis=T0 + timedelta(hours=3))
    with capture_logs() as logs:
        auth = _auth(tmp_path)
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
        "gardena_auth.login_sperre_fremd"]
    assert _versuch(auth, 401) == 1


def test_abgelaufene_sperre_sperrt_nicht_behaelt_aber_die_stufe(tmp_path, uhr):
    """Abgelaufen -> ein Versuch erlaubt; die Stufe bleibt (hier 40 Fehlversuche,
    vorher OverflowError ab dem 37.) und die neue Sperre greift mit Deckel."""
    _schreibe_sperre(tmp_path, bis=T0 - timedelta(minutes=1), fehlversuche=40)
    with capture_logs() as logs:
        auth = _auth(tmp_path)
    assert "gardena_auth.login_sperre_abgelaufen" in [e["event"] for e in logs]
    login = _login_client(401)
    with patch.object(auth_mod.httpx, "AsyncClient", return_value=login):
        with pytest.raises(GardenaCustomerAuthError):
            asyncio.run(auth.hole_gueltigen_token())
    assert login.post.await_count == 1
    uhr.t = T0 + auth_mod.LOGIN_SPERRE_MAX - timedelta(seconds=1)
    assert _versuch(_auth(tmp_path), 401) == 0


def test_sperre_weit_in_der_zukunft_wird_auf_den_deckel_gekappt(tmp_path, uhr):
    _schreibe_sperre(tmp_path, bis=T0 + timedelta(days=30))
    auth = _auth(tmp_path)
    uhr.t = T0 + auth_mod.LOGIN_SPERRE_MAX - timedelta(seconds=1)
    assert _versuch(auth, 401) == 0
    uhr.t = T0 + auth_mod.LOGIN_SPERRE_MAX
    assert _versuch(auth, 401) == 1


def test_schreibfehler_warnt_und_sperre_im_speicher_gilt(tmp_path, uhr):
    blockiert = tmp_path / "ist_eine_datei"
    blockiert.write_text("")
    auth = GardenaCustomerAuth(
        LOGIN, PASSWORT, tmp_path / "token.json",
        sperre_datei=blockiert / "sperre.json",
    )
    with capture_logs() as logs:
        assert _versuch(auth, 401) == 1
    assert "gardena_auth.login_sperre_schreiben_fehlgeschlagen" in [
        e["event"] for e in logs]
    uhr.t = T0 + timedelta(minutes=29)
    assert _versuch(auth, 401) == 0
