"""Globale pytest-Fixtures.

T-0140: Auth-Layer ist standardmaessig fuer alle Tests deaktiviert,
weil die meisten Test-Module die Endpoints ohne `X-Api-Key`-Header
aufrufen (sie testen Geschaeftslogik, nicht Auth). `test_api_auth.py`
ueberschreibt das in seiner eigenen `client`-Fixture, damit Auth dort
scharf greift.
"""
from __future__ import annotations

import pytest

from bewaesserung.api_auth import auth_dependency
from bewaesserung.api_server import app


@pytest.fixture(autouse=True)
def _auth_per_default_aus():
    async def _kein_check() -> None:
        return None

    app.dependency_overrides[auth_dependency] = _kein_check
    try:
        yield
    finally:
        app.dependency_overrides.pop(auth_dependency, None)
