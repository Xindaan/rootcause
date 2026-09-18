"""T-0426: Watchdog unterscheidet Sensor-Ausfall von echtem Trockenstress
und kennt dokumentierte Regime-Fenster.

Realfall 23.07.2026: iMessage meldete "Magerwiese steht seit 3 Tagen auf
'akut'. Bitte ins Dashboard schauen oder manuell giessen." Beide Haelften der
Meldung waren irrefuehrend:
  - Der Sensor stand seit 4 Tagen auf exakt 0.0 (Kontaktverlust), die
    Feuchte war also UNBEKANNT, nicht kritisch.
  - Fuer die Zone lief das dokumentierte Grass-Regime (Kanal beregnet den
    Rasen, das Beet bekommt bewusst kein Wasser) -- ein flacher Sensor ist
    dort erwartet.
"""

from datetime import datetime, timedelta

import pytest

from bewaesserung.modelle import MlAusschlussFenster, WatchdogKonfig, ZonenKonfig, ZonenModus
from bewaesserung.watchdog import TYP_AKUT_IN_FOLGE, WatchdogJob

JETZT = datetime(2026, 7, 23, 6, 0)


class _SpeicherAttrappe:
    def __init__(self, null_seit_stunden=None):
        self._null = null_seit_stunden
        self.pushes = []

    async def hole_empfehlungs_audit(self, tage, limit, jetzt):
        # 'akut' an drei Tagen in Folge fuer magerwiese
        return [
            {"zone_id": "magerwiese", "empfehlungs_typ": "akut",
             "zeitstempel": (JETZT - timedelta(days=d)).isoformat()}
            for d in range(3)
        ]

    async def hole_letzten_watchdog_push(self, typ, zone_id):
        return None

    async def setze_watchdog_push(self, typ, zone_id, jetzt):
        self.pushes.append((typ, zone_id))

    async def sensoren_auf_null_seit(self, zone_id, mindest_stunden, jetzt=None):
        if self._null is None:
            return []
        return [{"geraet_id": "testsensor-1", "seit": jetzt - timedelta(hours=self._null),
                 "stunden": self._null, "n_messungen": 96}]

    # Restliche Trigger sollen still No-Op sein
    async def hole_messungen_anzahl_seit(self, *a, **k): return 999
    async def hole_endpoint_health(self, *a, **k): return []
    async def hole_faellige_pflege(self, *a, **k): return []
    async def hole_unbekannte_events(self, *a, **k): return []


class _BenachrichtigerAttrappe:
    def __init__(self):
        self.texte = []

    async def sende_text(self, empfaenger, text):
        self.texte.append(text)
        return True


def _zone():
    return ZonenKonfig(
        zone_id="magerwiese", name="Magerwiese", modus=ZonenModus.MONITORING,
        feuchte_schwelle_min=22.0, feuchte_schwelle_max=42.0,
        feuchte_kritisch=14.0, ventil_kanal=1,
    )


def _job(speicher, benachrichtiger, fenster=None):
    return WatchdogJob(
        speicher=speicher, benachrichtiger=benachrichtiger,
        konfig=WatchdogKonfig(aktiv=True, empfaenger="test", akut_in_folge_tage=3),
        zonen=[_zone()], gardena_zone_ids=["magerwiese"],
        ausschluss_fenster=fenster,
    )


@pytest.mark.asyncio
async def test_toter_sensor_meldet_ausfall_statt_giessen():
    """DER Realfall. Bei 0.0 seit 4 Tagen darf NICHT 'bitte giessen' kommen --
    die Feuchte ist unbekannt, nicht kritisch. Wer auf Basis eines toten
    Sensors giesst, giesst blind."""
    sp = _SpeicherAttrappe(null_seit_stunden=96)
    ben = _BenachrichtigerAttrappe()
    await _job(sp, ben)._pruefe_akut_in_folge(JETZT)
    (text,) = ben.texte
    assert "Kontaktverlust" in text
    assert "UNBEKANNT" in text
    assert "manuell giessen" not in text
    assert "96 h" in text or "96h" in text


@pytest.mark.asyncio
async def test_gesunder_sensor_meldet_weiter_giessen():
    """Gegenprobe: ohne Ausfall bleibt die alte, richtige Meldung."""
    sp = _SpeicherAttrappe(null_seit_stunden=None)
    ben = _BenachrichtigerAttrappe()
    await _job(sp, ben)._pruefe_akut_in_folge(JETZT)
    (text,) = ben.texte
    assert "manuell giessen" in text
    assert "Kontaktverlust" not in text


@pytest.mark.asyncio
async def test_aktives_regime_wird_angehaengt():
    """Laeuft ein `events_auto_ignorieren`-Fenster, muss die Meldung das
    sagen -- sonst schickt sie den Nutzer gegen eine bewusste Entscheidung
    in den Garten."""
    fenster = [MlAusschlussFenster(
        zone_id="magerwiese",
        von=datetime(2026, 6, 13), bis=datetime(2026, 7, 31),
        events_auto_ignorieren=True, grund="Grass-Regime",
    )]
    sp = _SpeicherAttrappe(null_seit_stunden=None)
    ben = _BenachrichtigerAttrappe()
    await _job(sp, ben, fenster)._pruefe_akut_in_folge(JETZT)
    (text,) = ben.texte
    assert "Regime" in text
    assert "31.07." in text


@pytest.mark.asyncio
async def test_abgelaufenes_regime_wird_nicht_angehaengt():
    """Ein Fenster, das vorbei ist, darf die Meldung nicht mehr entschaerfen
    -- sonst ignoriert der Nutzer nach Regime-Ende echte Warnungen."""
    fenster = [MlAusschlussFenster(
        zone_id="magerwiese",
        von=datetime(2026, 6, 1), bis=datetime(2026, 7, 1),
        events_auto_ignorieren=True, grund="abgelaufen",
    )]
    sp = _SpeicherAttrappe(null_seit_stunden=None)
    ben = _BenachrichtigerAttrappe()
    await _job(sp, ben, fenster)._pruefe_akut_in_folge(JETZT)
    assert "Regime" not in ben.texte[0]


@pytest.mark.asyncio
async def test_fenster_ohne_auto_ignorieren_ist_kein_regime():
    """Ein reines ML-Ausschluss-Fenster (z.B. Sensor-Kalibrierung) sagt
    nichts darueber, ob die Zone Wasser braucht."""
    fenster = [MlAusschlussFenster(
        zone_id="magerwiese",
        von=datetime(2026, 6, 13), bis=datetime(2026, 7, 31),
        events_auto_ignorieren=False, grund="nur ML-Ausschluss",
    )]
    sp = _SpeicherAttrappe(null_seit_stunden=None)
    ben = _BenachrichtigerAttrappe()
    await _job(sp, ben, fenster)._pruefe_akut_in_folge(JETZT)
    assert "Regime" not in ben.texte[0]


@pytest.mark.asyncio
async def test_ohne_fenster_laeuft_alles_wie_bisher():
    """Backward-Compat: bestehende Aufrufe ohne `ausschluss_fenster`."""
    sp = _SpeicherAttrappe(null_seit_stunden=None)
    ben = _BenachrichtigerAttrappe()
    job = WatchdogJob(
        speicher=sp, benachrichtiger=ben,
        konfig=WatchdogKonfig(aktiv=True, empfaenger="test", akut_in_folge_tage=3),
        zonen=[_zone()], gardena_zone_ids=["magerwiese"],
    )
    await job._pruefe_akut_in_folge(JETZT)
    assert "manuell giessen" in ben.texte[0]
