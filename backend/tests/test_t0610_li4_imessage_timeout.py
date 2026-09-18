"""T-0610 LI-4: ein haengender osascript-Versand haelt den Entscheidungsloop
nicht mehr auf. Nach `IMESSAGE_TIMEOUT_S` wird der Prozess beendet und der
Versand gilt als fehlgeschlagen.

Kein echtes osascript: `create_subprocess_exec` ist durch eine Attrappe
ersetzt, deren `communicate` nie zurueckkehrt. Der Test selbst ist ausserdem
mit 2 s begrenzt, damit er ohne den Schutz rot wird statt zu haengen.
"""
from __future__ import annotations

import asyncio

import pytest

import bewaesserung.benachrichtigung as benachrichtigung


class _HaengenderProzess:
    def __init__(self):
        self.returncode = None
        self.getoetet = False
        self._ende = asyncio.Event()

    async def communicate(self):
        await self._ende.wait()
        return b"", b""

    def kill(self):
        self.getoetet = True
        self.returncode = -9
        self._ende.set()

    async def wait(self):
        await self._ende.wait()
        return self.returncode


@pytest.mark.asyncio
async def test_haengender_versand_endet_nach_timeout(monkeypatch):
    prozesse: list[_HaengenderProzess] = []

    async def fake_exec(*args, **kwargs):
        assert args[0] == "osascript"
        prozesse.append(_HaengenderProzess())
        return prozesse[-1]

    monkeypatch.setattr(benachrichtigung.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(benachrichtigung, "IMESSAGE_TIMEOUT_S", 0.05)

    ergebnis = await asyncio.wait_for(
        benachrichtigung.Benachrichtiger().sende_text("+490000", "Test"),
        timeout=2.0,
    )

    assert ergebnis is False
    assert len(prozesse) == 1 and prozesse[0].getoetet is True
