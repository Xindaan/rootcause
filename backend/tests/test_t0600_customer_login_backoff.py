"""T-0600 (Gesamtreview LI-5): kein Login-Sturm nach abgelehntem Customer-Login.

Repro aus dem Review (auth_sturm): HTTP 429 gestubbt, 5 Sensoren -> 5
password-grant-POSTs in EINEM DHS-Lauf, dazu je einer pro weiterem Aufrufer.
Gegen die inoffizielle Customer-API am selben Konto wie die Gardena-App ist
das die Klasse Husqvarna-Soft-Ban.

Alle HTTP-Clients sind Attrappen; ein echter Login ist hier nicht moeglich.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import bewaesserung.gardena_customer_auth as auth_mod
from bewaesserung.gardena_customer_auth import (
    GardenaCustomerAuth,
    GardenaCustomerAuthError,
)
from bewaesserung.sensor_dhs_backfill import SensorDhsBackfillJob

SENSOREN = {f"{i}" * 8 + "-0000-0000-0000-000000000000": f"zone_{i}" for i in range(5)}
T0 = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


class _Uhr:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _login_client(status):
    """Attrappe fuer httpx.AsyncClient im Auth-Modul; zaehlt die POSTs."""
    class _Antwort:
        status_code = status
        text = "{}"

        def json(self):
            if status == 200:
                return {"access_token": "tok", "expires_in": 864000}
            return {"error": "too_many_requests"}

    client = AsyncMock()
    client.post = AsyncMock(return_value=_Antwort())
    # Auth- und Backfill-Modul teilen sich das httpx-Modul, also EINE
    # Attrappe fuer beide: ein DHS-GET ohne Token laesst den Test scheitern.
    client.get = AsyncMock(side_effect=AssertionError("DHS-GET ohne Token"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def test_429_bei_fuenf_sensoren_genau_ein_login_pro_sperrfenster(
    tmp_path, monkeypatch,
):
    uhr = _Uhr(T0)
    monkeypatch.setattr(auth_mod, "_jetzt_utc", uhr)
    auth = GardenaCustomerAuth("u@example.com", "pw", tmp_path / "token.json")
    original = auth.hole_gueltigen_token
    aufrufe = []

    async def _gezaehlt():
        aufrufe.append(1)
        return await original()

    auth.hole_gueltigen_token = _gezaehlt
    job = SensorDhsBackfillJob(
        auth=auth, speicher=MagicMock(), location_id="loc",
        sensor_zu_zone=SENSOREN,
    )
    login = _login_client(429)
    with patch.object(auth_mod.httpx, "AsyncClient", return_value=login):
        asyncio.run(job.aktualisiere(datetime(2026, 9, 26, 12, 0)))
        # Lauf bricht nach dem ersten Auth-Fehler ab (AK2).
        assert len(aufrufe) == 1
        # Zweiter Lauf 30 min spaeter, noch in der 429-Sperre (2 h).
        uhr.t = T0 + timedelta(minutes=30)
        asyncio.run(job.aktualisiere(datetime(2026, 9, 26, 12, 30)))
    assert login.get.await_count == 0
    assert login.post.await_count == 1, (
        "pro Sperrfenster genau ein password-grant, egal wie viele Sensoren "
        "und Laeufe"
    )


def test_sperre_waechst_exponentiell_mit_deckel_und_erfolg_setzt_zurueck(
    tmp_path, monkeypatch,
):
    uhr = _Uhr(T0)
    monkeypatch.setattr(auth_mod, "_jetzt_utc", uhr)
    auth = GardenaCustomerAuth("u@example.com", "pw", tmp_path / "token.json")

    def _versuch(status):
        login = _login_client(status)
        with patch.object(auth_mod.httpx, "AsyncClient", return_value=login):
            try:
                asyncio.run(auth.hole_gueltigen_token())
            except GardenaCustomerAuthError:
                pass
        return login.post.await_count

    # 401: 30 min, dann 60 min, dann 120, 240, Deckel 240.
    erwartet = [30, 60, 120, 240, 240]
    for minuten in erwartet:
        assert _versuch(401) == 1
        uhr.t += timedelta(minutes=minuten) - timedelta(seconds=1)
        assert _versuch(401) == 0, f"innerhalb der {minuten}-min-Sperre kein POST"
        uhr.t += timedelta(seconds=1)

    # Erfolg setzt zurueck: die naechste Ablehnung sperrt wieder nur 30 min.
    assert _versuch(200) == 1
    auth._cache = None  # Token verwerfen, damit der naechste Aufruf einloggt
    (tmp_path / "token.json").unlink()
    assert _versuch(401) == 1
    uhr.t += timedelta(minutes=30)
    assert _versuch(401) == 1

