"""T-0423/T-0424: Regen-Ensemble + Radar-Nowcast.

Die Zahlen in den Tests sind die live verifizierten vom 22.07.2026
(icon_d2_eps, 20 Member); die Koordinaten hier sind Platzhalter.
"""

from datetime import datetime, timedelta

import pytest

from bewaesserung.radar_nowcast import Nowcast, werte_aus_gitter
from bewaesserung.wetter_ensemble import (
    EnsembleClient, perzentil_der_membersummen,
)

# T-0425: seit der Fensterkorrektur zaehlt `perzentil_der_membersummen` ab
# `jetzt`, nicht ab Listenanfang. Ohne expliziten Wert waere jeder Test
# tageszeitabhaengig -- und ab dem 23.07.2026 dauerhaft rot, weil der
# Testblock dann komplett in der Vergangenheit liegt. Dieselbe Konvention
# wie T-0508: jeder Aufrufer reicht sein eigenes `jetzt` durch.
VOR_BLOCK = datetime(2026, 7, 21, 23, 0)


def _hourly(member_reihen: list[list[float]]) -> dict:
    """Baut den Open-Meteo-Block: erste Spalte ohne Suffix, dann _memberNN.

    Zeitachse ab 2026-07-22T00:00 -- mit `jetzt=VOR_BLOCK` ist der gesamte
    Block Zukunft, die Tests messen also weiter genau das, was sie messen
    wollen, und nicht den Zeitfilter.
    """
    start = datetime(2026, 7, 22, 0, 0)
    h = {"time": [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
                  for i in range(len(member_reihen[0]))]}
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
    ]), stunden=4, jetzt=VOR_BLOCK)
    assert ens is not None
    assert ens.p20 == pytest.approx(10.0)
    assert ens.p50 == pytest.approx(10.0)
    assert ens.n_member == 2


def test_realzahlen_22_07_reproduzieren():
    """Live-Werte vom 22.07.: min 0,5 / p20 1,2 / Median 2,7 / max 10,5."""
    summen = [0.5, 0.6, 1.2, 1.3, 1.5, 1.8, 2.0, 2.3, 2.5, 2.7,
              2.9, 3.1, 3.4, 3.8, 4.4, 5.2, 6.1, 7.3, 8.8, 10.5]
    ens = perzentil_der_membersummen(_hourly([[s] for s in summen]), stunden=1, jetzt=VOR_BLOCK)
    assert ens is not None
    assert ens.n_member == 20
    assert ens.minimum == pytest.approx(0.5)
    # T-0564: nearest-rank `ceil(0.2*20)-1 = 3` -> 1,3. Vorher stand hier
    # 1,5 mit dem Kommentar "Index int(0.2*20)=4" -- das war die alte
    # Implementierung, einen Rang zu hoch. Die Docstring-Zeile oben nennt
    # als beobachteten Live-Wert 1,2 (Index 2); nearest-rank liegt naeher
    # daran als der alte Wert. Fuer p20 ist die Richtung entscheidend: zu
    # hoch heisst "mehr Regen erwartet" und damit "weniger giessen".
    assert ens.p20 == pytest.approx(1.3)
    assert ens.maximum == pytest.approx(10.5)
    assert ens.wahrsch_ueber_1mm == pytest.approx(0.90)


def test_spread_unterscheidet_landregen_von_konvektion():
    """Der Grund, warum ein fester Multiplikator (regen*0.5) nicht reicht:
    er ignoriert den Spread. p20 ist verteilungssensitiv."""
    einig = perzentil_der_membersummen(
        _hourly([[5.0], [5.1], [4.9], [5.0], [5.2]]), stunden=1, jetzt=VOR_BLOCK)
    uneinig = perzentil_der_membersummen(
        _hourly([[0.0], [0.1], [0.2], [12.0], [13.0]]), stunden=1, jetzt=VOR_BLOCK)
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
    ]), stunden=2, jetzt=VOR_BLOCK)
    assert ens.n_member == 2
    assert ens.minimum == pytest.approx(10.0)


def test_stunden_fenster_wird_respektiert():
    ens = perzentil_der_membersummen(_hourly([
        [1.0] * 48, [1.0] * 48,
    ]), stunden=24, jetzt=VOR_BLOCK)
    assert ens.p50 == pytest.approx(24.0)


# --------------------------------------------------------------------------
# T-0425: das Fenster zaehlt ab `jetzt`, nicht ab Listenanfang
# --------------------------------------------------------------------------

def test_fenster_zaehlt_ab_jetzt_nicht_ab_listenanfang():
    """DER Regressionstest zu T-0425 (sechster Fall der T-0508-Klasse).

    Open-Meteo liefert ab heute 00:00 Ortszeit. Der Regen liegt hier
    komplett am Nachmittag; wer ab Listenanfang schneidet, sieht um 11:00
    die trockene Nacht und meldet 0 mm -- also "kein Regen erwartet",
    obwohl es in einer Stunde losgeht.
    """
    block = _hourly([[0.0] * 12 + [1.0] * 12, [0.0] * 12 + [1.0] * 12])
    ens = perzentil_der_membersummen(
        block, stunden=6, jetzt=datetime(2026, 7, 22, 11, 0),
    )
    assert ens is not None
    assert ens.p50 == pytest.approx(6.0), (
        "12:00-17:00 sind sechs Regenstunden; ab Listenanfang waeren es "
        "00:00-05:00 und damit 0.0"
    )


def test_unvollstaendiges_fenster_gibt_none_statt_kurzer_summe():
    """Eine 24-h-Summe ueber 13 Stunden waere falsch ETIKETTIERT, nicht nur
    ungenau -- sie liefe als 24-h-Prognose in die Auswertung und saehe
    systematisch zu trocken aus. `None` heisst 'keine Aussage'."""
    block = _hourly([[1.0] * 24, [1.0] * 24])
    assert perzentil_der_membersummen(
        block, stunden=24, jetzt=datetime(2026, 7, 22, 11, 0),
    ) is None
    # Gegenprobe: passt das Fenster, kommt sehr wohl ein Wert.
    assert perzentil_der_membersummen(
        block, stunden=24, jetzt=VOR_BLOCK,
    ) is not None


def test_modellrand_kuerzt_das_fenster_statt_als_null_mm_zu_zaehlen():
    """icon_d2_eps reicht bei forecast_days=3 gemessen 66 von 72 Stunden
    (05.08.2026); der Rest ist None. `sum(v or 0.0)` haette diese Stunden
    als 0 mm gezaehlt -- also als 'kein Regen' statt 'unbekannt'."""
    block = _hourly([[1.0] * 20 + [None] * 4, [1.0] * 24])
    assert perzentil_der_membersummen(
        block, stunden=24, jetzt=VOR_BLOCK,
    ) is None
    # 20 Stunden reichen fuer einen 20-h-Horizont -- der Rand kuerzt, er
    # sperrt nicht.
    ens = perzentil_der_membersummen(block, stunden=20, jetzt=VOR_BLOCK)
    assert ens is not None and ens.p50 == pytest.approx(20.0)


def test_leerer_block_gibt_none():
    assert perzentil_der_membersummen({}, stunden=24, jetzt=VOR_BLOCK) is None
    assert perzentil_der_membersummen({"time": []}, stunden=24, jetzt=VOR_BLOCK) is None


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
async def test_job_schreibt_nur_den_24h_horizont():
    """T-0516: nur noch 24 h -- der 48-h-Horizont ist ersatzlos raus.

    Vorher hiess dieser Test `test_job_schreibt_beide_horizonte` und
    begruendete den zweiten Horizont damit, dass 24 h und 48 h voellig
    verschiedene Verteilungen liefern (22.07. real: p20 0,0 gegen 1,2).
    Das stimmt weiterhin -- nur hat den 48-h-Wert nie jemand gelesen
    (`wasserbilanz_job` filtert hart auf 24), und seit dem T-0425-Fix ist
    er ohnehin unerfuellbar: `icon_d2_eps` reicht nicht ueber 48 h ab
    Mitternacht hinaus, ab `jetzt` bleibt weniger uebrig.

    Der Test prueft deshalb beides: dass 24 h geschrieben wird UND dass
    **kein Horizont ueber 24 h** dazukommt.

    **T-0538 (13.08.2026):** der 6-h-Horizont ist dazugekommen und faellt
    nicht unter dieses Verbot -- sechs Stunden ab `jetzt` liegen immer im
    48-h-Fenster von `icon_d2_eps`, das Vollstaendigkeitsproblem des 48ers
    gibt es dort nicht. Der Test prueft deshalb die Obergrenze statt der
    exakten Menge; er soll den Ruecksturz zu 48 h verhindern, nicht jede
    Erweiterung nach unten.
    """
    from datetime import datetime

    from bewaesserung.regen_ensemble_job import RegenEnsembleJob

    sp = _SpeicherSpy()
    job = RegenEnsembleJob(sp, [("teststandort", 52.52, 13.40)])

    async def fake(breite, laenge):
        # Regen liegt im zweiten Tag -- fruehr der Beleg dafuer, dass sich
        # die Horizonte unterscheiden. Jetzt der Beleg dafuer, dass der
        # 24-h-Wert davon unberuehrt bleibt.
        return {"hourly": _hourly([[0.0] * 24 + [2.0] * 24,
                                   [0.0] * 24 + [3.0] * 24])}

    job._hole = fake
    n = await job.aktualisiere_wenn_faellig(datetime(2026, 7, 21, 23, 0))
    horizonte = {e["horizont"] for e in sp.eintraege}
    assert n == len(horizonte), "pro Standort und Horizont genau ein Eintrag"
    assert 24 in horizonte, "der 24-h-Horizont traegt die Bilanz"
    assert max(horizonte) <= 24, f"Horizont ueber 24 h: {horizonte}"
    h24 = next(e for e in sp.eintraege if e["horizont"] == 24)
    assert h24["p20"] == 0.0, "24h trocken -- Regen liegt im zweiten Tag"


@pytest.mark.asyncio
async def test_job_intervall_gate():
    """60 min: icon_d2_eps laeuft 8x taeglich. Haeufiger abzufragen liefert
    dieselben Zahlen und belastet eine fremde Gratis-API ohne Gegenwert."""
    from datetime import datetime, timedelta

    from bewaesserung.regen_ensemble_job import RegenEnsembleJob

    sp = _SpeicherSpy()
    job = RegenEnsembleJob(sp, [("teststandort", 52.52, 13.40)])

    async def fake(breite, laenge):
        # 72 h, nicht 24: der zweite Aufruf liegt 61 min spaeter, und seit
        # T-0425 zaehlt das Fenster ab `jetzt`. Ein zu knappes Fenster wuerde
        # eine Fensterluecke messen statt das Intervall-Gate.
        return {"hourly": _hourly([[1.0] * 72, [1.0] * 72])}

    job._hole = fake
    from bewaesserung.regen_ensemble_job import HORIZONTE

    t0 = datetime(2026, 7, 21, 23, 0)
    # Ein Eintrag je Standort UND Horizont. Die Zahl steht bewusst nicht
    # fest im Test: geprueft wird das Intervall-Gate, nicht die Zahl der
    # Horizonte (T-0516 hatte 48 h entfernt, T-0538 hat 6 h ergaenzt --
    # beide Male scheiterte hier sonst ein Test, der nichts damit zu tun hat).
    pro_lauf = len(HORIZONTE)
    assert await job.aktualisiere_wenn_faellig(t0) == pro_lauf
    assert await job.aktualisiere_wenn_faellig(t0 + timedelta(minutes=5)) == 0
    assert await job.aktualisiere_wenn_faellig(t0 + timedelta(minutes=61)) == pro_lauf


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
    assert await job.aktualisiere_wenn_faellig(datetime(2026, 7, 21, 23, 0)) == 0
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
    await job.aktualisiere_wenn_faellig(datetime(2026, 7, 21, 23, 0))
    h24 = next(e for e in sp.eintraege if e["horizont"] == 24)
    assert h24["det"] == pytest.approx(2.4)


@pytest.mark.asyncio
async def test_deterministischer_wert_nutzt_dasselbe_fenster_wie_die_perzentile():
    """T-0425, Isomorphie zur Fensterkorrektur.

    `det[:stunden]` trug denselben Listenanfang-Fehler wie die Member-Summen.
    Waere nur einer der beiden korrigiert worden, staende in derselben Zeile
    ein Punktwert aus Fenster A neben Perzentilen aus Fenster B -- und genau
    der Vergleich dieser beiden Zahlen ist der Zweck der Tabelle (T-0425).
    Ein Bug, der beide gleich verschiebt, ist harmlos fuer den Vergleich;
    einer, der nur eine Seite verschiebt, macht ihn gegenstandslos.

    Die erste Stunde traegt 9,0 mm und liegt VOR `jetzt`. Wer ab
    Listenanfang schneidet, schreibt 9,0 statt 1,0.
    """
    from bewaesserung.regen_ensemble_job import RegenEnsembleJob

    sp = _SpeicherSpy()
    job = RegenEnsembleJob(sp, [("teststandort", 52.52, 13.40)])

    async def fake(breite, laenge):
        return {"hourly": _hourly([
            [9.0] + [0.0] * 23 + [1.0] * 24,
            [9.0] + [0.0] * 23 + [1.0] * 24,
        ])}

    job._hole = fake
    await job.aktualisiere_wenn_faellig(datetime(2026, 7, 22, 0, 30))
    h24 = next(e for e in sp.eintraege if e["horizont"] == 24)
    assert h24["det"] == pytest.approx(1.0)
    assert h24["p20"] == pytest.approx(1.0)
    assert h24["det"] == pytest.approx(h24["p20"]), (
        "identische Member -> Punktwert und Perzentil muessen gleich sein; "
        "Abweichung heisst: zwei verschiedene Fenster"
    )


def test_t0564_perzentil_ist_nearest_rank():
    """Der Rang-Fehler direkt, an einer Reihe ohne Rundungs-Unschaerfe.

    Alte Formel `int(p * n)` greift systematisch einen Rang zu hoch:
    p10 -> 3,0 statt 2,0; p20 -> 5,0 statt 4,0; p90 -> 19,0 statt 18,0.
    """
    from bewaesserung.wetter_ensemble import _perzentil

    werte = [float(i) for i in range(1, 21)]

    assert _perzentil(werte, 0.10) == pytest.approx(2.0)
    assert _perzentil(werte, 0.20) == pytest.approx(4.0)
    assert _perzentil(werte, 0.50) == pytest.approx(10.0)
    assert _perzentil(werte, 0.90) == pytest.approx(18.0)
    # Raender: kein Index unter 0, keiner ueber n-1.
    assert _perzentil(werte, 0.0) == pytest.approx(1.0)
    assert _perzentil(werte, 1.0) == pytest.approx(20.0)
    assert _perzentil([], 0.2) == pytest.approx(0.0)
