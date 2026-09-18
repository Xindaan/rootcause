"""T-0543: der Status-Code eines abgelehnten Ventil-Kommandos darf nicht
am Antwort-Schema haengen.

**Der Realfall (15.08.2026).** Drei abgelehnte Kommandos, zwei davon
SCHLIESSEN (13:57:41 oeffnen, 14:39:51 + 14:40:31 schliessen, Kanal 2). Im
Log stand nur `KeyError: 'errors'` -- geworfen von der Zeile, die die
Fehlermeldung BAUEN sollte:

    raise Exception(f"{r.status_code} : {response['errors'][0]['title']}")

Damit war der Status-Code weg, bevor ihn jemand sieht. Und genau der
entscheidet ueber das richtige Verhalten: **429 heisst Soft-Ban, dort ist ein
Retry das Falscheste** (2-4 h warten); ein 502 ist eine Cloud-Delle, wo ein
Retry richtig ist. Vierzig Sekunden nach dem ersten Fehlschlag wurde erneut
geschlossen -- ob das in einen Ban hineinfeuerte, ist nicht mehr feststellbar.

Der Patch aendert **kein Verhalten**: gleiche Anfrage, gleiche Bedingung
(`!= 202`). Er sorgt nur dafuer, dass die Exception traegt, was passiert ist.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bewaesserung.gardena_client import GardenaKommandoFehler
from gardena.smart_system import SmartSystem


class _Antwort:
    """Minimale httpx-Response-Attrappe."""

    def __init__(self, status_code: int, body=None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("kein JSON")
        return self._body


class _Client:
    def __init__(self, antwort: _Antwort):
        self._antwort = antwort
        self.aufrufe: list[tuple] = []

    async def put(self, url, headers=None, data=None):
        self.aufrufe.append((url, headers, data))
        return self._antwort


class _SmartSystemAttrappe:
    """Nur was `call_smart_system_service` von `self` braucht."""

    SMART_HOST = "https://api.smart.gardena.dev"

    def __init__(self, antwort: _Antwort):
        self.client = _Client(antwort)

    def create_header(self, include_json=False):
        return {"X-Api-Key": "k"}


def _rufe(antwort: _Antwort, daten=None):
    attrappe = _SmartSystemAttrappe(antwort)
    coro = SmartSystem.call_smart_system_service(
        attrappe, "geraet:2", daten or {"type": "VALVE_CONTROL"},
    )
    return attrappe, coro


def test_202_wirft_nicht_und_sendet_das_kommando():
    attrappe, coro = _rufe(_Antwort(202))
    assert asyncio.run(coro) is None
    (url, headers, data), = attrappe.client.aufrufe
    assert url.endswith("/v2/command/geraet:2")
    assert json.loads(data) == {"data": {"type": "VALVE_CONTROL"}}


def test_429_ohne_errors_feld_behaelt_den_status_code():
    """Der Realfall: Body ohne `errors` -- vorher KeyError, jetzt 429."""
    _, coro = _rufe(_Antwort(429, body={"message": "rate limited"}))
    with pytest.raises(GardenaKommandoFehler) as exc:
        asyncio.run(coro)
    assert exc.value.status_code == 429
    assert exc.value.ist_soft_ban is True


def test_body_ist_gar_kein_json():
    """502 mit HTML-Fehlerseite -- auch dann muss der Code durchkommen."""
    _, coro = _rufe(_Antwort(502, text="<html>Bad Gateway</html>"))
    with pytest.raises(GardenaKommandoFehler) as exc:
        asyncio.run(coro)
    assert exc.value.status_code == 502
    assert exc.value.ist_soft_ban is False
    assert "Bad Gateway" in exc.value.detail


def test_leerer_body_ist_kein_absturz():
    _, coro = _rufe(_Antwort(503, text="   "))
    with pytest.raises(GardenaKommandoFehler) as exc:
        asyncio.run(coro)
    assert exc.value.status_code == 503
    assert exc.value.detail == "<leerer Body>"


def test_schema_konformer_fehler_behaelt_den_klartext():
    """Wenn die Cloud sich ans Schema haelt, soll die gute Meldung bleiben --
    der Patch darf keine Information WEGnehmen."""
    _, coro = _rufe(_Antwort(
        400, body={"errors": [{"title": "Invalid valve id"}]},
    ))
    with pytest.raises(GardenaKommandoFehler) as exc:
        asyncio.run(coro)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid valve id"


def test_langer_body_wird_gekuerzt():
    """Eine 50-KB-HTML-Seite gehoert nicht in eine Exception-Nachricht."""
    _, coro = _rufe(_Antwort(500, text="x" * 5000))
    with pytest.raises(GardenaKommandoFehler) as exc:
        asyncio.run(coro)
    assert len(exc.value.detail) == 300


def test_patch_ist_beim_import_aktiv():
    """Regression: der Patch wird beim Modul-Import gesetzt. Faellt der Aufruf
    von `_patche_kommando_fehlermeldung()` je weg, ist wieder die
    Original-Methode aktiv -- und der KeyError zurueck."""
    assert SmartSystem.call_smart_system_service.__name__ == "call_gepatched"
