"""Tests fuer fyta_import (Einmal-CSV-Import)."""
from datetime import datetime

from bewaesserung.fyta_import import _parse_zeile


def test_f18_parse_zeile_date_utc_wird_lokal_konvertiert():
    """F18/T-0176: `date_utc` ist naiv-UTC -> muss in lokale Zeit konvertiert
    werden (Sommer CEST = +2h), sonst 2h zu frueh + Cross-Source-Duplikate
    gegen den API-Backfill (der parse_fyta_zeitstempel nutzt). Vorher:
    naive datetime.fromisoformat -> 10:00 statt 12:00."""
    zeile = {"user_plant_id": "42", "date_utc": "2026-06-01T10:00:00"}
    messung = _parse_zeile(zeile, {42: "zitrus"})
    assert messung is not None
    # 10:00 UTC -> 12:00 lokal (CEST, Sommerzeit)
    assert messung.zeitstempel == datetime(2026, 6, 1, 12, 0, 0)
