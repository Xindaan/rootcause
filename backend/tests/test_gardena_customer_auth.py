import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from bewaesserung.gardena_customer_auth import (
    GardenaCustomerAuth,
    GardenaCustomerAuthError,
    baue_aus_env,
)


def _run(coro):
    return asyncio.run(coro)


def _mock_http_antwort(status, body):
    class AntwortMock:
        status_code = status
        text = json.dumps(body)
        def json(self):
            return body
    return AntwortMock()


def test_hole_token_ohne_cache_macht_password_grant(tmp_path):
    auth = GardenaCustomerAuth("user@example.com", "pw", tmp_path / "token.json")

    mock_antwort = _mock_http_antwort(200, {
        "access_token": "new-token-123", "refresh_token": "refresh-xyz",
        "token_type": "Bearer", "scope": "iam:read iam:write", "expires_in": 864000,
    })
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_antwort)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("bewaesserung.gardena_customer_auth.httpx.AsyncClient", return_value=mock_client):
        token = _run(auth.hole_gueltigen_token())

    assert token == "new-token-123"
    # Token-Datei existiert mit 0600
    pfad = tmp_path / "token.json"
    assert pfad.exists()
    assert oct(pfad.stat().st_mode)[-3:] == "600"
    # Inhalt plausibel
    daten = json.loads(pfad.read_text())
    assert daten["access_token"] == "new-token-123"
    assert daten["scope"] == "iam:read iam:write"
    # Request-Body hatte client_id=smart-garden-web + grant_type=password
    aufruf = mock_client.post.call_args
    assert aufruf.kwargs["data"]["client_id"] == "smartgarden-jwt-client"
    assert aufruf.kwargs["data"]["grant_type"] == "password"
    assert aufruf.kwargs["data"]["username"] == "user@example.com"


def test_gueltiger_cache_wird_ohne_http_call_zurueckgegeben(tmp_path):
    pfad = tmp_path / "token.json"
    # Cache mit 5 Tagen Gueltigkeit
    pfad.write_text(json.dumps({
        "access_token": "alt-token",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
    }))
    auth = GardenaCustomerAuth("u", "p", pfad)

    with patch("bewaesserung.gardena_customer_auth.httpx.AsyncClient") as mock_cls:
        token = _run(auth.hole_gueltigen_token())
        mock_cls.assert_not_called()
    assert token == "alt-token"


def test_cache_mit_kurzer_restlaufzeit_wird_neu_geholt(tmp_path):
    pfad = tmp_path / "token.json"
    # Nur 12 Stunden Restlaufzeit → unter dem 24h-Puffer → refresh noetig
    pfad.write_text(json.dumps({
        "access_token": "bald-abgelaufen",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
    }))
    auth = GardenaCustomerAuth("u", "p", pfad)

    mock_antwort = _mock_http_antwort(200, {
        "access_token": "ganz-frischer-token", "expires_in": 864000, "token_type": "Bearer",
    })
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_antwort)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    with patch("bewaesserung.gardena_customer_auth.httpx.AsyncClient", return_value=mock_client):
        token = _run(auth.hole_gueltigen_token())
    assert token == "ganz-frischer-token"


def test_http_fehler_wird_als_auth_error_gemeldet(tmp_path):
    auth = GardenaCustomerAuth("u", "p", tmp_path / "token.json")

    mock_antwort = _mock_http_antwort(400, {
        "error": "invalid_grant",
        "error_description": "Bad credentials",
        "error_code": "invalid.grant",
    })
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_antwort)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    with patch("bewaesserung.gardena_customer_auth.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(GardenaCustomerAuthError):
            _run(auth.hole_gueltigen_token())


def test_fehlende_credentials_werfen_sofort(tmp_path):
    with pytest.raises(GardenaCustomerAuthError):
        GardenaCustomerAuth("", "pw", tmp_path / "t.json")
    with pytest.raises(GardenaCustomerAuthError):
        GardenaCustomerAuth("u", "", tmp_path / "t.json")


def test_baue_aus_env_ohne_variablen_gibt_none(tmp_path, monkeypatch):
    monkeypatch.delenv("GARDENA_CUSTOMER_EMAIL", raising=False)
    monkeypatch.delenv("GARDENA_CUSTOMER_PASSWORD", raising=False)
    assert baue_aus_env(tmp_path / "t.json") is None


def test_baue_aus_env_mit_variablen_gibt_instanz(tmp_path, monkeypatch):
    monkeypatch.setenv("GARDENA_CUSTOMER_EMAIL", "a@b")
    monkeypatch.setenv("GARDENA_CUSTOMER_PASSWORD", "pw")
    auth = baue_aus_env(tmp_path / "t.json")
    assert isinstance(auth, GardenaCustomerAuth)
