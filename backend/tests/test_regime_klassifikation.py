"""T-0349: Tests fuer die Single-Source-Regime-Klassifikation."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bewaesserung.ml.regime_klassifikation import (
    REGIME_AUSGESCHLOSSEN_CROSSSPRAY,
    REGIME_AUSGESCHLOSSEN_MLAUSSCHLUSS,
    REGIME_GIESS_RECOVERY,
    REGIME_REGEN,
    REGIME_REGEN_UNBEKANNT,
    REGIME_TROCKNUNG,
    RegimeKontext,
    ausschluss_fenster_fuer_zone,
    cross_spray_quellen,
    klassifiziere_regime,
    lade_regime_kontext,
)
from bewaesserung.modelle import (
    Ausloser,
    GardenaKonfig,
    GesamtKonfig,
    MlAusschlussFenster,
    StandortKonfig,
    VentilAktion,
    VentilEreignis,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher

SNAP = datetime(2026, 6, 15, 12, 0)
WETTER_OK = datetime(2026, 6, 20, 0, 0)  # Archiv deckt alle Fenster ab


def _kontext(**kw) -> RegimeKontext:
    basis = dict(wetter_max_ts=WETTER_OK)
    basis.update(kw)
    return RegimeKontext(**basis)


def test_trocknung_default():
    assert klassifiziere_regime(SNAP, 6, _kontext()) == REGIME_TROCKNUNG


def test_giess_recovery_eigener_lauf_im_fenster():
    k = _kontext(laeufe_eigene=[SNAP + timedelta(hours=2)])
    assert klassifiziere_regime(SNAP, 6, k) == REGIME_GIESS_RECOVERY


def test_giess_recovery_settling_vorlauf():
    """Lauf 5h VOR dem Snapshot liegt im 6h-Settling -> recovery."""
    k = _kontext(laeufe_eigene=[SNAP - timedelta(hours=5)])
    assert klassifiziere_regime(SNAP, 6, k) == REGIME_GIESS_RECOVERY
    # 7h davor liegt ausserhalb des Settling-Fensters -> trocknung.
    k2 = _kontext(laeufe_eigene=[SNAP - timedelta(hours=7)])
    assert klassifiziere_regime(SNAP, 6, k2) == REGIME_TROCKNUNG


def test_horizont_beeinflusst_fenster():
    """Lauf 20h nach Snapshot: im 24h-Fenster recovery, im 6h nicht."""
    k = _kontext(laeufe_eigene=[SNAP + timedelta(hours=20)])
    assert klassifiziere_regime(SNAP, 24, k) == REGIME_GIESS_RECOVERY
    assert klassifiziere_regime(SNAP, 6, k) == REGIME_TROCKNUNG


def test_cross_spray_schlaegt_trocknung_und_regen():
    k = _kontext(
        laeufe_cross=[SNAP + timedelta(hours=1)],
        wetter_regen=[(SNAP + timedelta(hours=2), 5.0)],
    )
    assert klassifiziere_regime(SNAP, 6, k) == REGIME_AUSGESCHLOSSEN_CROSSSPRAY


def test_cross_spray_schlaegt_eigenen_lauf():
    """Prioritaet (Verifier-Review 01.07., Abweichung zum T-0348-Skript):
    crossspray VOR giess_recovery — bei Doppel-Wasser (eigener Lauf +
    Cross-Spray) ist die Wirkung nicht attribuierbar."""
    k = _kontext(
        laeufe_eigene=[SNAP + timedelta(hours=1)],
        laeufe_cross=[SNAP + timedelta(hours=1)],
    )
    assert klassifiziere_regime(SNAP, 6, k) == REGIME_AUSGESCHLOSSEN_CROSSSPRAY


def test_mlausschluss_fenster():
    k = _kontext(
        ausschluss_fenster=[
            (SNAP - timedelta(days=1), SNAP + timedelta(days=1)),
        ],
    )
    assert (
        klassifiziere_regime(SNAP, 6, k)
        == REGIME_AUSGESCHLOSSEN_MLAUSSCHLUSS
    )
    # Fenster komplett VOR dem Klassifikations-Fenster -> normal.
    k2 = _kontext(
        ausschluss_fenster=[
            (SNAP - timedelta(days=3), SNAP - timedelta(hours=7)),
        ],
    )
    assert klassifiziere_regime(SNAP, 6, k2) == REGIME_TROCKNUNG


def test_mlausschluss_schlaegt_giess_recovery():
    """Sensor im Kalibrier-Fenster: Ist-Wert unbrauchbar, auch wenn
    gegossen wurde (Verifier-Review 01.07.)."""
    k = _kontext(
        laeufe_eigene=[SNAP + timedelta(hours=1)],
        ausschluss_fenster=[
            (SNAP - timedelta(days=1), SNAP + timedelta(days=1)),
        ],
    )
    assert (
        klassifiziere_regime(SNAP, 6, k)
        == REGIME_AUSGESCHLOSSEN_MLAUSSCHLUSS
    )


def test_mlausschluss_overlap_am_fenster_ende():
    """Fenster-Overlap zaehlt auch, wenn nur der Ist-Zeitpunkt (ts+h)
    im Ausschluss-Fenster liegt — nicht nur der Snapshot selbst."""
    k = _kontext(
        ausschluss_fenster=[
            (SNAP + timedelta(hours=5), SNAP + timedelta(days=1)),
        ],
    )
    assert (
        klassifiziere_regime(SNAP, 6, k)
        == REGIME_AUSGESCHLOSSEN_MLAUSSCHLUSS
    )
    # 24h-Fenster endet vor Fenster-Beginn? Nein — 5h < 24h -> auch dort.
    # Gegenprobe: Ausschluss beginnt NACH Fenster-Ende -> trocknung.
    k2 = _kontext(
        ausschluss_fenster=[
            (SNAP + timedelta(hours=7), SNAP + timedelta(days=1)),
        ],
    )
    assert klassifiziere_regime(SNAP, 6, k2) == REGIME_TROCKNUNG


def test_regen_und_schwelle():
    k = _kontext(wetter_regen=[(SNAP + timedelta(hours=1), 1.5)])
    assert klassifiziere_regime(SNAP, 6, k) == REGIME_REGEN
    # Unter Schwelle (1.0 mm) -> trocknung.
    k2 = _kontext(wetter_regen=[(SNAP + timedelta(hours=1), 0.8)])
    assert klassifiziere_regime(SNAP, 6, k2) == REGIME_TROCKNUNG


def test_regen_unbekannt_bei_era5_lag():
    """Archiv endet VOR Fenster-Ende -> Regen nicht beurteilbar."""
    k = _kontext(wetter_max_ts=SNAP + timedelta(hours=3))
    assert klassifiziere_regime(SNAP, 6, k) == REGIME_REGEN_UNBEKANNT
    k2 = _kontext(wetter_max_ts=None)
    assert klassifiziere_regime(SNAP, 6, k2) == REGIME_REGEN_UNBEKANNT


# ---------------------------------------------------------------------------
# Konfig-Helfer + Loader.
# ---------------------------------------------------------------------------

def _baue_konfig() -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=[
            ZonenKonfig(
                zone_id="hecke", name="Hecke", modus="monitoring",
                ventil_kanal=2,
                feuchte_schwelle_min=32, feuchte_schwelle_max=55,
                feuchte_kritisch=22,
                cross_spray_quell_zonen=["magerwiese"],
            ),
            ZonenKonfig(
                zone_id="magerwiese", name="Magerwiese", modus="monitoring",
                ventil_kanal=1,
                feuchte_schwelle_min=25, feuchte_schwelle_max=50,
                feuchte_kritisch=15,
            ),
        ],
        wetter=WetterKonfig(
            standorte=[
                WetterStandortKonfig(id="standort_a", breite=52.52, laenge=13.405),
            ],
        ),
        standorte=[
            StandortKonfig(
                standort_id="standort_a", name="Standort A",
                wetter_standort="standort_a",
                zonen=["hecke", "magerwiese"],
            ),
        ],
        ml_ausschluss_fenster=[
            MlAusschlussFenster(
                zone_id="hecke",
                von=SNAP - timedelta(days=2),
                bis=SNAP + timedelta(days=2),
                grund="Test-Fenster",
            ),
        ],
    )


def test_cross_spray_quellen_aus_konfig():
    konfig = _baue_konfig()
    assert cross_spray_quellen(konfig, "hecke") == ["magerwiese"]
    assert cross_spray_quellen(konfig, "magerwiese") == []
    assert cross_spray_quellen(konfig, "unbekannt") == []


def test_ausschluss_fenster_nutzt_echte_von_bis():
    konfig = _baue_konfig()
    fenster = ausschluss_fenster_fuer_zone(konfig, "hecke")
    assert fenster == [(SNAP - timedelta(days=2), SNAP + timedelta(days=2))]


@pytest.mark.asyncio
async def test_lade_regime_kontext_aus_speicher(tmp_path):
    """Loader: eigener echter Lauf vs ignoriert-Lauf der Cross-Quelle."""
    s = Speicher(str(tmp_path / "regime.db"))
    await s.verbinden()
    try:
        # Echter hecke-Lauf.
        await s.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=SNAP + timedelta(hours=1), zone_id="hecke",
            ventil_id="v2", aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=1800, ausloser=Ausloser.MANUELL,
        ))
        # 'ignoriert'-Lauf der hecke selbst -> KEIN eigener Wasser-Lauf.
        await s.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=SNAP + timedelta(hours=2), zone_id="hecke",
            ventil_id="v2", aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=900, ausloser=Ausloser.IGNORIERT,
        ))
        # 'ignoriert'-Lauf der magerwiese -> Cross-Spray zaehlt TROTZDEM.
        await s.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=SNAP + timedelta(hours=3), zone_id="magerwiese",
            ventil_id="v1", aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=3600, ausloser=Ausloser.IGNORIERT,
        ))
        # OEFFNEN + dauer=0 Events duerfen NICHT zaehlen.
        await s.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=SNAP + timedelta(hours=4), zone_id="hecke",
            ventil_id="v2", aktion=VentilAktion.OEFFNEN,
            dauer_sekunden=0, ausloser=Ausloser.MANUELL,
        ))
        kontext = await lade_regime_kontext(
            s, _baue_konfig(), "hecke",
            von=SNAP - timedelta(hours=6), bis=SNAP + timedelta(hours=24),
        )
        assert kontext.laeufe_eigene == [SNAP + timedelta(hours=1)]
        assert kontext.laeufe_cross == [SNAP + timedelta(hours=3)]
        assert kontext.ausschluss_fenster == [
            (SNAP - timedelta(days=2), SNAP + timedelta(days=2)),
        ]
        # Kein Wetter-Archiv -> regen_unbekannt waere die Folge.
        assert kontext.wetter_max_ts is None
    finally:
        await s.schliessen()


@pytest.mark.asyncio
async def test_t0470_heuristik_ereignis_der_quellzone_ist_kein_lauf(tmp_path):
    """T-0470: ein Heuristik-Sprung der Quell-Zone belegt keine Beregnung.

    Zwei Stellen beantworteten dieselbe Frage ("hat die Quell-Zone physisch
    beregnet?") mit verschiedenen Regeln: `sensor_backfill._cross_spray_quelle`
    verlangt `ventil_id != 'sensor_heuristik'`, dieser Loader hatte gar keinen
    Filter. Ein Heuristik-Ereignis heisst aber "dort gab es einen Feuchtesprung
    ohne Erklaerung" -- gerade KEIN Beleg fuer einen Regnerlauf. Folge: das
    Fenster wurde als `ausgeschlossen_cross_spray` etikettiert und fiel aus der
    Auswertung, obwohl nichts lief.

    Verschaerfend: die FREMDWASSER-Paare aus T-0469 tragen dieselbe
    `ventil_id`. Ohne den Filter kann sich Cross-Spray zwischen wechselseitig
    konfigurierten Zonen selbst weitertragen.
    """
    s = Speicher(str(tmp_path / "regime_t0470.db"))
    await s.verbinden()
    try:
        # Heuristik-Sprung der Quell-Zone -> darf NICHT als Lauf zaehlen.
        await s.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=SNAP + timedelta(hours=1), zone_id="magerwiese",
            ventil_id="sensor_heuristik", aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=1800, ausloser=Ausloser.MANUELL,
        ))
        kontext = await lade_regime_kontext(
            s, _baue_konfig(), "hecke",
            von=SNAP - timedelta(hours=6), bis=SNAP + timedelta(hours=24),
        )
        assert kontext.laeufe_cross == [], (
            "Heuristik-Ereignis der Quell-Zone als physischer Lauf gewertet"
        )

        # Gegenprobe: ein echter Kanal-Lauf derselben Zone zaehlt weiterhin,
        # und zwar auch als `ignoriert` -- der Regner lief physisch.
        await s.speichere_ventil_ereignis(VentilEreignis(
            zeitstempel=SNAP + timedelta(hours=2), zone_id="magerwiese",
            ventil_id="v1", aktion=VentilAktion.SCHLIESSEN,
            dauer_sekunden=3600, ausloser=Ausloser.IGNORIERT,
        ))
        kontext = await lade_regime_kontext(
            s, _baue_konfig(), "hecke",
            von=SNAP - timedelta(hours=6), bis=SNAP + timedelta(hours=24),
        )
        assert kontext.laeufe_cross == [SNAP + timedelta(hours=2)]
    finally:
        await s.schliessen()
