"""T-0477: Retention der Response-Modellversionen.

Schwerpunkt: die Schutzmengen-Berechnung (DB-Referenz, aufgeloestes
Symlink-Ziel, Juengsten-Puffer) und die Pfade, auf denen NICHT geloescht
werden darf.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from bewaesserung.ml.response_retention import (
    MAX_ENTFERNUNGEN_PRO_LAUF,
    STANDARD_N_JUENGSTE,
    bewerte_zone,
    liste_versionen,
    lies_symlink_ziele,
    raeume_zone_auf,
)


def _lege_version(zone_dir: Path, version: str) -> Path:
    pfad = zone_dir / version
    pfad.mkdir(parents=True, exist_ok=True)
    (pfad / "inverse.lgbm").write_text("modell")
    (pfad / "forward_q50.lgbm").write_text("modell")
    (pfad / "metadata.json").write_text("{}")
    return pfad


def _setze_symlink(zone_dir: Path, zone_id: str, version: str) -> None:
    """Wie ResponseTrainingsPipeline._setze_symlink: relativ auf die Datei."""
    for kind, datei in (
        ("forward_q50", "forward_q50.lgbm"),
        ("inverse", "inverse.lgbm"),
    ):
        link = zone_dir / f"aktuell_{zone_id}_{kind}.lgbm"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(Path(version) / datei)
    meta = zone_dir / f"aktuell_{zone_id}_metadata.json"
    if meta.is_symlink() or meta.exists():
        meta.unlink()
    meta.symlink_to(Path(version) / "metadata.json")


@pytest.fixture
def zone_dir(tmp_path: Path) -> Path:
    d = tmp_path / "bambuswald"
    d.mkdir()
    return d


# --- Bestandsaufnahme ---


def test_liste_versionen_ignoriert_fremde_eintraege(zone_dir: Path):
    _lege_version(zone_dir, "v20260101_010101")
    _lege_version(zone_dir, "v20260102_010101")
    (zone_dir / "nicht_eine_version").mkdir()
    (zone_dir / "_staging_inverse.lgbm").write_text("x")
    (zone_dir / "v20260103").mkdir()          # falsches Format
    _setze_symlink(zone_dir, "bambuswald", "v20260102_010101")

    assert liste_versionen(zone_dir) == [
        "v20260101_010101", "v20260102_010101",
    ]


def test_liste_versionen_leeres_verzeichnis(tmp_path: Path):
    assert liste_versionen(tmp_path / "gibt_es_nicht") == []


def test_symlink_ziel_wird_aufgeloest_nicht_am_namen_verglichen(zone_dir: Path):
    _lege_version(zone_dir, "v20260101_010101")
    _lege_version(zone_dir, "v20260505_120000")
    _setze_symlink(zone_dir, "bambuswald", "v20260505_120000")

    ziele, defekt, fremd = lies_symlink_ziele(zone_dir)
    assert ziele == {"v20260505_120000"}
    assert defekt == []
    assert fremd == []


def test_absoluter_symlink_wird_ebenfalls_aufgeloest(zone_dir: Path):
    ziel = _lege_version(zone_dir, "v20260101_010101")
    link = zone_dir / "aktuell_bambuswald_inverse.lgbm"
    link.symlink_to(ziel / "inverse.lgbm")     # absolut

    ziele, defekt, fremd = lies_symlink_ziele(zone_dir)
    assert ziele == {"v20260101_010101"}
    assert (defekt, fremd) == ([], [])


def test_defekter_symlink_wird_gemeldet(zone_dir: Path):
    _lege_version(zone_dir, "v20260101_010101")
    link = zone_dir / "aktuell_bambuswald_inverse.lgbm"
    link.symlink_to(Path("v20260909_090909") / "inverse.lgbm")

    ziele, defekt, fremd = lies_symlink_ziele(zone_dir)
    assert ziele == set()
    assert defekt == ["aktuell_bambuswald_inverse.lgbm"]
    assert fremd == []


def test_symlink_aus_der_zone_heraus_wird_gemeldet(zone_dir: Path, tmp_path: Path):
    _lege_version(zone_dir, "v20260101_010101")
    aussen = tmp_path / "woanders"
    aussen.mkdir()
    (aussen / "inverse.lgbm").write_text("x")
    link = zone_dir / "aktuell_bambuswald_inverse.lgbm"
    link.symlink_to(os.path.relpath(aussen / "inverse.lgbm", zone_dir))

    _, defekt, fremd = lies_symlink_ziele(zone_dir)
    assert defekt == []
    assert fremd == ["aktuell_bambuswald_inverse.lgbm"]


# --- Schutzmenge ---


def test_schutzmenge_ist_vereinigung_aus_drei_quellen(zone_dir: Path):
    versionen = [f"v202601{tag:02d}_000000" for tag in range(1, 13)]
    for v in versionen:
        _lege_version(zone_dir, v)
    # Symlink auf eine ALTE Version (Gate hat die neueren abgelehnt).
    _setze_symlink(zone_dir, "bambuswald", "v20260103_000000")

    befund = bewerte_zone(
        zone_dir, "bambuswald",
        referenzierte={"v20260101_000000", "v20260105_000000"},
        n_juengste=5,
    )

    assert befund.referenziert == {"v20260101_000000", "v20260105_000000"}
    assert befund.symlink_ziele == {"v20260103_000000"}
    assert befund.juengste == set(versionen[-5:])
    assert befund.geschuetzt == {
        "v20260101_000000", "v20260103_000000", "v20260105_000000",
        *versionen[-5:],
    }
    # Kandidaten: alles andere, aeltestes zuerst.
    assert befund.kandidaten == [
        "v20260102_000000", "v20260104_000000",
        "v20260106_000000", "v20260107_000000",
    ]
    assert befund.aufraeumbar is True


def test_referenzierte_version_bleibt_auch_wenn_uralt(zone_dir: Path):
    for tag in range(1, 21):
        _lege_version(zone_dir, f"v202603{tag:02d}_000000")
    _setze_symlink(zone_dir, "bambuswald", "v20260320_000000")

    befund = bewerte_zone(
        zone_dir, "bambuswald", referenzierte={"v20260301_000000"},
    )
    assert "v20260301_000000" in befund.geschuetzt
    assert "v20260301_000000" not in befund.kandidaten


def test_juengsten_puffer_default_ist_fuenf(zone_dir: Path):
    for tag in range(1, 11):
        _lege_version(zone_dir, f"v202604{tag:02d}_000000")
    befund = bewerte_zone(zone_dir, "bambuswald")
    assert len(befund.juengste) == STANDARD_N_JUENGSTE
    assert len(befund.kandidaten) == 5


def test_weniger_versionen_als_puffer_kein_kandidat(zone_dir: Path):
    _lege_version(zone_dir, "v20260101_000000")
    _lege_version(zone_dir, "v20260102_000000")
    befund = bewerte_zone(zone_dir, "bambuswald")
    assert befund.kandidaten == []


# --- Aufraeumen ---


def test_raeume_zone_auf_entfernt_nur_kandidaten(zone_dir: Path, caplog):
    caplog.set_level("INFO")
    for tag in range(1, 8):
        _lege_version(zone_dir, f"v202605{tag:02d}_000000")
    _setze_symlink(zone_dir, "bambuswald", "v20260501_000000")

    bericht = raeume_zone_auf(
        zone_dir, "bambuswald",
        referenzierte={"v20260502_000000"},
        n_juengste=3,
    )

    verbleibend = liste_versionen(zone_dir)
    # geschuetzt: Symlink (01), DB (02), juengste drei (05,06,07)
    assert set(verbleibend) >= {
        "v20260501_000000", "v20260502_000000",
        "v20260505_000000", "v20260506_000000", "v20260507_000000",
    }
    assert "v20260503_000000" not in verbleibend
    assert "v20260504_000000" not in verbleibend
    assert bericht["entfernt"] == 2


def test_symlinks_bleiben_nach_aufraeumen_aufloesbar(zone_dir: Path):
    for tag in range(1, 21):
        _lege_version(zone_dir, f"v202606{tag:02d}_000000")
    _setze_symlink(zone_dir, "bambuswald", "v20260602_000000")

    for _ in range(10):
        raeume_zone_auf(zone_dir, "bambuswald", referenzierte=set(), n_juengste=2)

    for link in sorted(zone_dir.glob("aktuell_*")):
        assert link.is_symlink()
        assert link.resolve().exists(), f"{link.name} zeigt ins Leere"


def test_deckel_begrenzt_entfernungen_pro_lauf(zone_dir: Path):
    for tag in range(1, 26):
        _lege_version(zone_dir, f"v202607{tag:02d}_000000")
    _setze_symlink(zone_dir, "bambuswald", "v20260725_000000")

    bericht = raeume_zone_auf(zone_dir, "bambuswald", n_juengste=2)
    assert bericht["kandidaten"] == 23
    assert bericht["entfernt"] == MAX_ENTFERNUNGEN_PRO_LAUF
    # Die aeltesten zuerst.
    assert liste_versionen(zone_dir)[0] == "v20260706_000000"


def test_defekter_symlink_verhindert_jedes_loeschen(zone_dir: Path, caplog):
    caplog.set_level("WARNING")
    for tag in range(1, 11):
        _lege_version(zone_dir, f"v202608{tag:02d}_000000")
    (zone_dir / "aktuell_bambuswald_inverse.lgbm").symlink_to(
        Path("v20261231_235959") / "inverse.lgbm"
    )

    bericht = raeume_zone_auf(zone_dir, "bambuswald", n_juengste=2)

    assert bericht["entfernt"] == 0
    assert bericht["uebersprungen"].startswith("defekte_symlinks")
    assert len(liste_versionen(zone_dir)) == 10
    assert "ml.response_retention.uebersprungen" in caplog.text


def test_fremder_symlink_verhindert_jedes_loeschen(zone_dir: Path, tmp_path: Path):
    for tag in range(1, 11):
        _lege_version(zone_dir, f"v202609{tag:02d}_000000")
    aussen = tmp_path / "aussen"
    aussen.mkdir()
    (aussen / "inverse.lgbm").write_text("x")
    (zone_dir / "aktuell_bambuswald_inverse.lgbm").symlink_to(
        os.path.relpath(aussen / "inverse.lgbm", zone_dir)
    )

    bericht = raeume_zone_auf(zone_dir, "bambuswald", n_juengste=2)
    assert bericht["entfernt"] == 0
    assert bericht["uebersprungen"].startswith("fremde_symlinks")
    assert len(liste_versionen(zone_dir)) == 10


def test_fremde_dateien_werden_nie_angefasst(zone_dir: Path):
    for tag in range(1, 11):
        _lege_version(zone_dir, f"v202610{tag:02d}_000000")
    (zone_dir / "_staging_inverse.lgbm").write_text("x")
    fremd_dir = zone_dir / "manuell_gesichert"
    fremd_dir.mkdir()
    (fremd_dir / "irgendwas.txt").write_text("x")

    raeume_zone_auf(zone_dir, "bambuswald", n_juengste=1)

    assert (zone_dir / "_staging_inverse.lgbm").exists()
    assert (fremd_dir / "irgendwas.txt").exists()


def test_zone_ohne_verzeichnis_ist_harmlos(tmp_path: Path):
    bericht = raeume_zone_auf(tmp_path / "existiert_nicht", "avocado")
    assert bericht["entfernt"] == 0
    assert bericht["kandidaten"] == 0
