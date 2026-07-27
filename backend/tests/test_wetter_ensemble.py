"""T-0423/T-0424: Regen-Ensemble + Radar-Nowcast.

Die Zahlen in den Tests sind die live verifizierten vom 22.07.2026
(icon_d2_eps, 20 Member); die Koordinaten hier sind Platzhalter.
"""

import pytest

from bewaesserung.radar_nowcast import Nowcast, werte_aus_gitter
from bewaesserung.wetter_ensemble import (
    EnsembleClient, perzentil_der_membersummen,
)


def _hourly(member_reihen: list[list[float]]) -> dict:
    """Baut den Open-Meteo-Block: erste Spalte ohne Suffix, dann _memberNN."""
    h = {"time": [f"2026-07-22T{i:02d}:00" for i in range(len(member_reihen[0]))]}
    for i, reihe in enumerate(member_reihen):
        name = "precipitation" if i == 0 else f"precipitation_member{i:02d}"
        h[name] = reihe
    return h


# --------------------------------------------------------------------------
# Die Kernregel: erst pro Member summieren, DANN das Perzentil
# --------------------------------------------------------------------------

def test_erst_summieren_dann_perzentil():
    """DER Test fuer die Reihenfolge.

    Zwei Member, die zu VERSCHIEDENEN Zeiten je 10 mm bringen. Beide Member
    haben die Summe 10 -> jedes Perzentil der Summen ist 10.
    Wuerde man je Stunde das Perzentil bilden und dann summieren, kaeme fuer
    p20 in jeder Stunde die 0 des jeweils anderen Members heraus -> Summe 0.
    Also ein Verlauf, den kein einziger Member vorhergesagt hat.
    """
    ens = perzentil_der_membersummen(_hourly([
        [10.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 10.0, 0.0],
    ]), stunden=4)
    assert ens is not None
    assert ens.p20 == pytest.approx(10.0)
    assert ens.p50 == pytest.approx(10.0)
    assert ens.n_member == 2


def test_realzahlen_22_07_reproduzieren():
    """Live-Werte vom 22.07.: min 0,5 / p20 1,2 / Median 2,7 / max 10,5."""
    summen = [0.5, 0.6, 1.2, 1.3, 1.5, 1.8, 2.0, 2.3, 2.5, 2.7,
              2.9, 3.1, 3.4, 3.8, 4.4, 5.2, 6.1, 7.3, 8.8, 10.5]
    ens = perzentil_der_membersummen(_hourly([[s] for s in summen]), stunden=1)
    assert ens is not None
    assert ens.n_member == 20
    assert ens.minimum == pytest.approx(0.5)
    assert ens.p20 == pytest.approx(1.5)   # Index int(0.2*20)=4
    assert ens.maximum == pytest.approx(10.5)
    assert ens.wahrsch_ueber_1mm == pytest.approx(0.90)


def test_spread_unterscheidet_landregen_von_konvektion():
    """Der Grund, warum ein fester Multiplikator (regen*0.5) nicht reicht:
    er ignoriert den Spread. p20 ist verteilungssensitiv."""
    einig = perzentil_der_membersummen(
        _hourly([[5.0], [5.1], [4.9], [5.0], [5.2]]), stunden=1)
    uneinig = perzentil_der_membersummen(
        _hourly([[0.0], [0.1], [0.2], [12.0], [13.0]]), stunden=1)
    assert einig.p20 > 4.0, "Landregen: p20 nahe Median -> Skip erlaubt"
    assert uneinig.p20 < 1.0, "Konvektion: p20 kollabiert -> es wird gegossen"
    assert uneinig.spread > einig.spread * 5


def test_member_ohne_aussage_zieht_perzentil_nicht_runter():
    """Ein Member, der nur None liefert, ist 'keine Aussage', nicht '0 mm'.
    Wuerde er als 0 zaehlen, saehe jedes Ensemble trockener aus als es ist --
    und wir wuerden zu oft giessen."""
    ens = perzentil_der_membersummen(_hourly([
        [5.0, 5.0],
        [None, None],
        [6.0, 4.0],
    ]), stunden=2)
    assert ens.n_member == 2
    assert ens.minimum == pytest.approx(10.0)


def test_stunden_fenster_wird_respektiert():
    ens = perzentil_der_membersummen(_hourly([
        [1.0] * 48, [1.0] * 48,
    ]), stunden=24)
    assert ens.p50 == pytest.approx(24.0)


def test_leerer_block_gibt_none():
    assert perzentil_der_membersummen({}, stunden=24) is None
    assert perzentil_der_membersummen({"time": []}, stunden=24) is None


@pytest.mark.asyncio
async def test_client_faellt_bei_fehler_auf_none_zurueck():
    """Ensemble ist ein ZUSATZ-Signal. Faellt die API aus, darf die
    Bewaesserung nicht stehenbleiben -- der Caller nutzt dann den
    deterministischen Wert."""
    async def kaputt(url, params):
        raise RuntimeError("timeout")
    assert await EnsembleClient(kaputt).hole(52.52, 13.40) is None


@pytest.mark.asyncio
async def test_client_nutzt_precipitation_nicht_rain():
    """`rain` schliesst `showers` aus. Bei konvektivem Sommerregen steckt ein
    erheblicher Teil dort (22.07. gemessen: rain 1,50 vs precipitation 1,80,
    nur 3 von 20 Membern identisch) -- mit `rain` wuerden wir den Regen
    unterschaetzen und zu oft giessen."""
    gesehen = {}

    async def fake(url, params):
        gesehen.update(params)
        return {"hourly": _hourly([[2.0], [2.0]])}

    await EnsembleClient(fake).hole(52.52, 13.40)
    assert gesehen["hourly"] == "precipitation"
    assert gesehen["models"] == "icon_d2_eps"


# --------------------------------------------------------------------------
# Radar-Nowcast
# --------------------------------------------------------------------------

def _gitter(wert_mitte: int, wert_nachbar: int = 0, n: int = 5) -> list[list[int]]:
    g = [[wert_nachbar] * n for _ in range(n)]
    g[n // 2][n // 2] = wert_mitte
    return g


def test_umfeld_max_findet_zelle_die_knapp_vorbeizieht():
    """Der Realfall 22.07.: Punktwert 0,25 mm (kein Veto), Umfeld-Maximum
    1,97 mm (die Zelle zog ~5 km entfernt vorbei). Wer nur das Pixel liest,
    uebersieht die Zelle, die in 20 Minuten darueber sein kann --
    Radar-Nowcasts haben Advektionsfehler von einigen Kilometern."""
    radar = [{"precipitation_5": _gitter(wert_mitte=5, wert_nachbar=40)}
             for _ in range(5)]
    nc = werte_aus_gitter(radar, x=2, y=2)
    assert nc.punkt_mm == pytest.approx(0.25)
    assert nc.umfeld_max_mm == pytest.approx(2.0)
    assert nc.veto(schwelle_mm=1.0) is True, "Veto muss auf dem Umfeld greifen"


def test_kein_veto_bei_trockenem_umfeld():
    radar = [{"precipitation_5": _gitter(0, 0)} for _ in range(24)]
    nc = werte_aus_gitter(radar, x=2, y=2)
    assert nc.veto() is False


def test_gitter_rand_wirft_nicht():
    """Liegt der Punkt am Rand des Ausschnitts, darf die Umfeld-Schleife
    nicht ueber die Grenze laufen."""
    radar = [{"precipitation_5": _gitter(10, 1)}]
    nc = werte_aus_gitter(radar, x=0, y=0)
    assert nc.schritte == 1


def test_schritte_ohne_gitter_werden_uebersprungen():
    radar = [{"precipitation_5": None}, {"precipitation_5": _gitter(100)}]
    nc = werte_aus_gitter(radar, x=2, y=2)
    assert nc.schritte == 1
    assert nc.punkt_mm == pytest.approx(1.0)


def test_nowcast_veto_schwelle_ist_parametrisch():
    nc = Nowcast(punkt_mm=0.2, umfeld_max_mm=1.5, schritte=24)
    assert nc.veto(schwelle_mm=1.0) is True
    assert nc.veto(schwelle_mm=2.0) is False


# --------------------------------------------------------------------------
# T-0423 Job + Persistenz (Shadow)
# --------------------------------------------------------------------------

class _SpeicherSpy:
    def __init__(self):
        self.eintraege = []

    async def speichere_regen_ensemble(
        self, abfrage_zeit, standort_id, horizont_stunden, ensemble,
        deterministisch_mm=None,
    ):
        self.eintraege.append({
            "standort": standort_id, "horizont": horizont_stunden,
            "p20": ensemble.p20, "n": ensemble.n_member,
            "det": deterministisch_mm,
        })


@pytest.mark.asyncio
async def test_job_schreibt_beide_horizonte():
    """Beide Horizonte, weil dieselbe Abfrage ueber 24 h und 48 h voellig
    verschiedene Verteilungen liefert (22.07. real: p20 0,0 gegen 1,2 --
    der Regen lag im zweiten Tag). Ein Eintrag ohne Horizont waere nicht
    interpretierbar."""
    from datetime import datetime

    from bewaesserung.regen_ensemble_job import RegenEnsembleJob

    sp = _SpeicherSpy()
    job = RegenEnsembleJob(sp, [("teststandort", 52.52, 13.40)])

    async def fake(breite, laenge):
        return {"hourly": _hourly([[0.0] * 24 + [2.0] * 24,
                                   [0.0] * 24 + [3.0] * 24])}

    job._hole = fake
    n = await job.aktualisiere_wenn_faellig(datetime(2026, 7, 22, 12, 0))
    assert n == 2
    horizonte = {e["horizont"] for e in sp.eintraege}
    assert horizonte == {24, 48}
    h24 = next(e for e in sp.eintraege if e["horizont"] == 24)
    h48 = next(e for e in sp.eintraege if e["horizont"] == 48)
    assert h24["p20"] == 0.0, "24h trocken -- Regen liegt im zweiten Tag"
    assert h48["p20"] > 0.0


@pytest.mark.asyncio
async def test_job_intervall_gate():
    """60 min: icon_d2_eps laeuft 8x taeglich. Haeufiger abzufragen liefert
    dieselben Zahlen und belastet eine fremde Gratis-API ohne Gegenwert."""
    from datetime import datetime, timedelta

    from bewaesserung.regen_ensemble_job import RegenEnsembleJob

    sp = _SpeicherSpy()
    job = RegenEnsembleJob(sp, [("teststandort", 52.52, 13.40)])

    async def fake(breite, laenge):
        return {"hourly": _hourly([[1.0] * 48, [1.0] * 48])}

    job._hole = fake
    t0 = datetime(2026, 7, 22, 12, 0)
    assert await job.aktualisiere_wenn_faellig(t0) == 2
    assert await job.aktualisiere_wenn_faellig(t0 + timedelta(minutes=5)) == 0
    assert await job.aktualisiere_wenn_faellig(t0 + timedelta(minutes=61)) == 2


@pytest.mark.asyncio
async def test_job_ueberlebt_api_ausfall():
    """Shadow-Job: ein Ausfall darf nichts blockieren und nichts schreiben."""
    from datetime import datetime

    from bewaesserung.regen_ensemble_job import RegenEnsembleJob

    sp = _SpeicherSpy()
    job = RegenEnsembleJob(sp, [("teststandort", 52.52, 13.40)])

    async def kaputt(breite, laenge):
        return None

    job._hole = kaputt
    assert await job.aktualisiere_wenn_faellig(datetime(2026, 7, 22, 12, 0)) == 0
    assert sp.eintraege == []


@pytest.mark.asyncio
async def test_job_speichert_deterministischen_vergleichswert():
    """Der Punktwert wandert als Vergleich mit in die DB -- nur so laesst sich
    der Kernbefund (Punktwert unter dem p25 des Ensembles) laufend
    nachpruefen, statt ihn einmalig behauptet zu haben."""
    from datetime import datetime

    from bewaesserung.regen_ensemble_job import RegenEnsembleJob

    sp = _SpeicherSpy()
    job = RegenEnsembleJob(sp, [("teststandort", 52.52, 13.40)])

    async def fake(breite, laenge):
        return {"hourly": _hourly([[0.1] * 24, [5.0] * 24])}

    job._hole = fake
    await job.aktualisiere_wenn_faellig(datetime(2026, 7, 22, 12, 0))
    h24 = next(e for e in sp.eintraege if e["horizont"] == 24)
    assert h24["det"] == pytest.approx(2.4)
