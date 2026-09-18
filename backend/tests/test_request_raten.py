"""T-0463: Regressionsschutz gegen die Effekt-Rueckkopplungsklasse (T-0459).

T-0459 verlangte als Akzeptanzkriterium einen Test, der die Fehlerklasse
abdeckt ("Effekt darf keinen State in den Deps haben, den er selbst als neues
Objekt schreibt"). Er liess sich nicht direkt bauen: es gibt bewusst keine
Frontend-Testsuite, und `react-hooks/set-state-in-effect` feuert nur bei
synchronem `setState` im Effekt-Body -- in beiden T-0459-Faellen stand der
Aufruf in einem async-Callback nach `await`.

Statt der Ursache prueft dieser Schutz die Wirkung: die Request-Rate pro
Route. Getestet wird hier der Zaehler dahinter, mit den echten Zahlen aus dem
Audit (0,2 req/s normal gegen 40,5 und 118,9 req/s im Sturm).

Abgedeckt:
- Sturm auf einer Route reisst den Deckel, Normalbetrieb nicht.
- Aggregation auf das Route-Template, nicht auf den konkreten Pfad (in
  T-0459 kam der GESAMTE Sturm aus einer einzigen Zone).
- Die drei Wege, auf denen der Schutz frueher still versagt haette:
  kurzes Fenster nach dem Start, Warn-Flut ins Log, Fenster laeuft ab.
- Middleware + Endpoint haengen wirklich am Zaehler (kein Detektor ohne
  Konsument).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from bewaesserung.request_raten import (
    DEFAULT_DECKEL_RPS,
    UNBEKANNT,
    RequestRatenZaehler,
)


def _zaehler(**kwargs) -> RequestRatenZaehler:
    kwargs.setdefault("start", 1000.0)
    return RequestRatenZaehler(**kwargs)


def _fuettere(z: RequestRatenZaehler, route: str, rps: float, sekunden: float, ab: float) -> float:
    """`rps` Requests/Sekunde ueber `sekunden` einspeisen, gleichmaessig verteilt."""
    anzahl = int(rps * sekunden)
    for i in range(anzahl):
        z.zaehle("GET", route, jetzt=ab + i * (sekunden / max(anzahl, 1)))
    return ab + sekunden


# --- Kern: Sturm sichtbar, Normalbetrieb still ---------------------------


def test_normalbetrieb_reisst_keinen_deckel():
    """0,2 req/s -- das gemeinte 5-Sekunden-Raster des Dashboards."""
    z = _zaehler()
    ende = _fuettere(z, "/api/ventil-status", rps=0.2, sekunden=60.0, ab=1000.0)

    assert z.ueberschreitungen(ende) == []
    assert z.schnappschuss(ende)["ok"] is True


def test_mehrere_tabs_reissen_keinen_deckel():
    """Fuenf offene Tabs sind Alltag, kein Bug: 5 x 0,2 = 1,0 req/s."""
    z = _zaehler()
    ende = _fuettere(z, "/api/ventil-status", rps=1.0, sekunden=60.0, ab=1000.0)

    assert z.ueberschreitungen(ende) == []


def test_sturm_aus_t0459_reisst_den_deckel():
    """118,9 req/s auf /api/ventil/pre-soak-status -- der schwerste Befund."""
    z = _zaehler()
    ende = _fuettere(z, "/api/ventil/pre-soak-status", rps=118.9, sekunden=60.0, ab=1000.0)

    risse = z.ueberschreitungen(ende)
    assert len(risse) == 1
    assert risse[0].route == "GET /api/ventil/pre-soak-status"
    assert risse[0].rate_rps == pytest.approx(118.9, rel=0.02)
    assert risse[0].deckel_rps == DEFAULT_DECKEL_RPS
    assert z.schnappschuss(ende)["ok"] is False


def test_zweiter_sturm_aus_t0459_reisst_den_deckel():
    """40,5 req/s auf /api/ventil-status (Sturm bei laufender Bewaesserung)."""
    z = _zaehler()
    ende = _fuettere(z, "/api/ventil-status", rps=40.5, sekunden=60.0, ab=1000.0)

    risse = z.ueberschreitungen(ende)
    assert [u.route for u in risse] == ["GET /api/ventil-status"]


def test_beide_stuerme_werden_nach_rate_sortiert():
    """Der schwerere Riss steht oben -- sonst muss man die Liste lesen."""
    z = _zaehler()
    _fuettere(z, "/api/ventil-status", rps=40.5, sekunden=60.0, ab=1000.0)
    ende = _fuettere(z, "/api/ventil/pre-soak-status", rps=118.9, sekunden=60.0, ab=1000.0)

    risse = z.ueberschreitungen(ende)
    assert [u.route for u in risse] == [
        "GET /api/ventil/pre-soak-status",
        "GET /api/ventil-status",
    ]


# --- Aggregationsebene ---------------------------------------------------


def test_sturm_einer_einzelnen_zone_verschwindet_nicht():
    """In T-0459 erzeugte EINE Zone (waldblumenhain) 5289 von 5289 Requests.

    Wuerde pro konkretem Pfad statt pro Route-Template gezaehlt, verteilte
    sich der Sturm nie -- aber die 20 ruhigen Zonen wuerden ihn im Mittelwert
    verduennen, sobald irgendwer ueber Routen aggregiert. Der Zaehler bekommt
    deshalb das Template und sieht die Summe.
    """
    z = _zaehler()
    # 120 req/s auf EINE Zone, dazu 20 Zonen im Normaltakt -- alles laeuft
    # ueber dasselbe Template.
    ende = _fuettere(z, "/api/zonen/{zone_id}/messwerte", rps=120.0, sekunden=60.0, ab=1000.0)

    risse = z.ueberschreitungen(ende)
    assert [u.route for u in risse] == ["GET /api/zonen/{zone_id}/messwerte"]


def test_methode_trennt_die_schluessel():
    """GET-Poll und POST-Aktion auf demselben Pfad sind verschiedene Dinge."""
    z = _zaehler()
    for i in range(600):
        z.zaehle("GET", "/api/giessen", jetzt=1000.0 + i * 0.1)
    z.zaehle("POST", "/api/giessen", jetzt=1060.0)

    raten = z.raten(1060.0)
    assert "GET /api/giessen" in raten
    assert raten["POST /api/giessen"]["anzahl"] == 1


def test_unbekannte_route_landet_in_einem_sammel_eimer():
    """404-Pfade kommen aus fremder Quelle -- roh gezaehlt sprengen sie die
    Schluesselmenge (jede erfundene URL ein eigener Eintrag)."""
    z = _zaehler()
    z.zaehle("GET", None, jetzt=1000.0)
    z.zaehle("GET", None, jetzt=1001.0)

    raten = z.raten(1002.0)
    assert list(raten) == [f"GET {UNBEKANNT}"]
    assert raten[f"GET {UNBEKANNT}"]["anzahl"] == 2


def test_statische_assets_haben_einen_eigenen_deckel():
    """Ein Full-Reload zieht ein Dutzend Dateien in unter einer Sekunde.

    Mit dem Default-Deckel waere jeder Reload ein Fehlalarm.
    """
    z = _zaehler()
    ende = _fuettere(z, "/{pfad:path}", rps=8.0, sekunden=20.0, ab=1000.0)

    assert z.ueberschreitungen(ende) == []
    assert z.deckel_fuer("GET /{pfad:path}") == 20.0


# --- Die drei stillen Versagensarten -------------------------------------


def test_kurz_nach_dem_start_wird_nicht_gemeldet():
    """Zwei schnelle Klicks in der ersten Sekunde sind rechnerisch 20 req/s.

    Ohne Mindest-Beobachtungsdauer waere jeder Prozessstart ein Fehlalarm.
    """
    z = _zaehler(mindest_beobachtung_s=10.0)
    z.zaehle("GET", "/api/zonen", jetzt=1000.0)
    z.zaehle("GET", "/api/zonen", jetzt=1000.1)

    assert z.ueberschreitungen(1000.2) == []


def test_sturm_direkt_nach_dem_start_wird_trotzdem_gesehen():
    """Der Nenner ist die BEOBACHTETE Dauer, nicht das volle Fenster.

    Sonst waere ein Sturm, der direkt nach einem Reload anlaeuft, bis zu
    Faktor 60 kleingerechnet -- also genau dann unsichtbar, wenn das Frontend
    sich gerade neu montiert hat.
    """
    z = _zaehler(fenster_s=60.0, mindest_beobachtung_s=10.0)
    ende = _fuettere(z, "/api/ventil/pre-soak-status", rps=118.9, sekunden=11.0, ab=1000.0)

    risse = z.ueberschreitungen(ende)
    assert len(risse) == 1
    assert risse[0].rate_rps == pytest.approx(118.9, rel=0.05)


def test_fenster_laeuft_ab_und_die_rate_faellt():
    """Nach dem Fix muss der Zaehler auch wieder gruen werden.

    Ein Detektor, der einmal ausgeloest fuer immer rot bleibt, wird
    weggeschaut.
    """
    z = _zaehler(fenster_s=60.0)
    _fuettere(z, "/api/ventil-status", rps=118.9, sekunden=60.0, ab=1000.0)
    assert z.ueberschreitungen(1060.0)

    # 60 s Ruhe (ein Poll alle 5 s), das Sturmfenster ist rausgelaufen.
    ende = _fuettere(z, "/api/ventil-status", rps=0.2, sekunden=61.0, ab=1060.0)
    assert z.ueberschreitungen(ende) == []


class _LogSpion:
    """structlog haengt hier nicht an der stdlib, `caplog` sieht die Warnung
    also nicht. Der Spion ersetzt den Modul-Logger direkt."""

    def __init__(self) -> None:
        self.warnungen: list[dict] = []

    def warning(self, ereignis: str, **kwargs) -> None:
        self.warnungen.append({"ereignis": ereignis, **kwargs})


@pytest.fixture
def log_spion(monkeypatch) -> _LogSpion:
    import bewaesserung.request_raten as modul

    spion = _LogSpion()
    monkeypatch.setattr(modul, "logger", spion)
    return spion


def test_warnung_wird_gedrosselt(log_spion):
    """Ein Sturm mit 40 req/s wuerde sonst genau das Log fluten, das ohnehin
    unbegrenzt waechst (T-0461): die Deckel-Pruefung laeuft ~300x."""
    z = _zaehler(warn_intervall_s=300.0)
    _fuettere(z, "/api/ventil-status", rps=40.0, sekunden=299.0, ab=1000.0)

    assert len(log_spion.warnungen) == 1
    warnung = log_spion.warnungen[0]
    assert warnung["ereignis"] == "request_raten.deckel_gerissen"
    assert warnung["route"] == "GET /api/ventil-status"
    # Der Hinweis muss die Fehlerklasse benennen -- sonst steht im Log eine
    # Zahl, mit der niemand etwas anfangen kann.
    assert "T-0459" in warnung["hinweis"]


def test_warnung_kommt_nach_ablauf_des_intervalls_erneut(log_spion):
    """Gedrosselt heisst nicht verstummt -- ein Sturm ueber Stunden muss
    weiter im Log auftauchen, sonst sieht man nur den Beginn."""
    z = _zaehler(warn_intervall_s=300.0)
    _fuettere(z, "/api/ventil-status", rps=40.0, sekunden=700.0, ab=1000.0)

    # Erste Warnung nach Ablauf der Mindest-Beobachtung (~t+10 s), danach
    # alle 300 s: ~1010, ~1310, ~1610.
    assert len(log_spion.warnungen) == 3


def test_speicher_bleibt_unter_dem_sturm_beschraenkt():
    """Bei 119 req/s duerfen nicht 7000 Zeitstempel pro Route liegenbleiben.

    Der Zaehler ist Dauerlast im laufenden Betrieb; er darf nicht selbst zum
    Problem werden, das er finden soll.
    """
    z = _zaehler(fenster_s=60.0)
    ende = _fuettere(z, "/api/ventil-status", rps=118.9, sekunden=300.0, ab=1000.0)
    z.raten(ende)

    assert len(z._eimer["GET /api/ventil-status"]) <= 61


def test_gesamt_zaehler_bleibt_ueber_das_fenster_hinaus_stehen():
    """Das Smoke-Skript misst sein eigenes Fenster per Differenz von `gesamt`.

    Wuerde `gesamt` mit dem Rollfenster gekuerzt, waere die Differenz falsch,
    sobald das Skript laenger misst als `fenster_s`.
    """
    z = _zaehler(fenster_s=60.0)
    ende = _fuettere(z, "/api/zonen", rps=1.0, sekunden=120.0, ab=1000.0)

    schnapp = z.schnappschuss(ende)
    eintrag = schnapp["routen"]["GET /api/zonen"]
    assert eintrag["gesamt"] == 120
    assert eintrag["anzahl"] <= 61  # Fenster


# --- Verdrahtung: Middleware + Endpoint ----------------------------------


def test_middleware_zaehlt_das_route_template_nicht_den_pfad():
    """Ohne diesen Test waere der Zaehler ein Detektor ohne Zulauf: der
    Route-Eintrag steht erst NACH dem Routing in `scope["route"]`."""
    z = RequestRatenZaehler(start=1000.0)
    mini = FastAPI()

    @mini.middleware("http")
    async def zaehlen(request: Request, call_next):
        try:
            return await call_next(request)
        finally:
            route = request.scope.get("route")
            z.zaehle(request.method, getattr(route, "path", None))

    @mini.get("/api/zonen/{zone_id}/messwerte")
    async def messwerte(zone_id: str):
        return {"zone": zone_id}

    client = TestClient(mini)
    client.get("/api/zonen/waldblumenhain/messwerte?stunden=48")
    client.get("/api/zonen/bambuswald/messwerte")
    client.get("/api/gibtsnicht")

    raten = z.raten()
    assert raten["GET /api/zonen/{zone_id}/messwerte"]["gesamt"] == 2
    assert raten[f"GET {UNBEKANNT}"]["gesamt"] == 1


def test_middleware_zaehlt_auch_fehlerhafte_requests():
    """Eine Fehlerschleife im Frontend ist genau so ein Sturm wie eine
    Erfolgsschleife -- und wuerde ohne `finally` unsichtbar bleiben."""
    z = RequestRatenZaehler(start=1000.0)
    mini = FastAPI()

    @mini.middleware("http")
    async def zaehlen(request: Request, call_next):
        try:
            return await call_next(request)
        finally:
            route = request.scope.get("route")
            z.zaehle(request.method, getattr(route, "path", None))

    @mini.get("/api/kaputt")
    async def kaputt():
        raise RuntimeError("boom")

    client = TestClient(mini, raise_server_exceptions=False)
    client.get("/api/kaputt")

    assert z.raten()["GET /api/kaputt"]["gesamt"] == 1


def test_endpoint_liefert_den_schnappschuss_des_echten_zaehlers():
    """Verdrahtungs-Nachweis gegen `fehlerpattern_detektor_ohne_konsument`:
    der Endpoint muss am prozessweiten Zaehler haengen, nicht an einer
    frischen Instanz."""
    from bewaesserung.api_server import app
    from bewaesserung.request_raten import zaehler as echter_zaehler

    client = TestClient(app)
    antwort = client.get("/api/ops/request-raten")
    assert antwort.status_code == 200

    body = antwort.json()
    for feld in ("fenster_s", "beobachtet_s", "default_deckel_rps", "routen", "ueberschreitungen", "ok"):
        assert feld in body, feld

    # Der Abruf selbst muss im Zaehler stehen -- sonst zaehlt die Middleware
    # in eine andere Instanz als die, die der Endpoint ausliest.
    assert "GET /api/ops/request-raten" in echter_zaehler.raten()


def test_endpoint_steht_in_der_route_matrix():
    """`validiere_route_matrix` blockt sonst den Start (T-0140)."""
    from bewaesserung.api_auth import ROUTE_ROLLEN

    assert ROUTE_ROLLEN[("GET", "/api/ops/request-raten")] == "read"


# --- Smoke-Check-Skript ---------------------------------------------------


def _skript():
    """`backend/skripte/` ist kein Paket -- ueber den Pfad laden."""
    import importlib.util
    from pathlib import Path

    pfad = Path(__file__).resolve().parents[1] / "skripte" / "pruefe_request_raten.py"
    spec = importlib.util.spec_from_file_location("pruefe_request_raten", pfad)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def test_skript_rechnet_die_rate_aus_der_differenz():
    """Das Skript misst SEIN Fenster, nicht das des Servers.

    Sonst waere das Ergebnis still falsch, sobald `--dauer` von `fenster_s`
    abweicht -- und `--dauer` ist genau der Knopf, an dem gedreht wird.
    """
    s = _skript()
    vorher = {"routen": {"GET /api/ventil-status": {"gesamt": 1000}}}
    nachher = {"routen": {"GET /api/ventil-status": {"gesamt": 2200}}}

    raten = s.rate_je_route(vorher, nachher, dauer_s=30.0)
    assert raten["GET /api/ventil-status"] == pytest.approx(40.0)


def test_skript_sieht_eine_erst_neu_auftauchende_route():
    """Eine Route, die im ersten Schnappschuss fehlt, startet bei 0.

    Ohne diesen Default faellt genau der Endpoint durchs Raster, dessen Sturm
    waehrend der Messung anlaeuft.
    """
    s = _skript()
    raten = s.rate_je_route({"routen": {}}, {"routen": {"GET /api/neu": {"gesamt": 600}}}, 30.0)
    assert raten["GET /api/neu"] == pytest.approx(20.0)


def test_skript_erkennt_den_spa_fallback_als_fehlenden_endpoint():
    """Ein Backend mit altem Code antwortet hier NICHT mit 404.

    Der SPA-Fallback (`GET /{pfad:path}`) faengt jeden unbekannten Pfad ab und
    liefert 200 + index.html. Ohne Guard stirbt das Skript an einem
    JSONDecodeError statt zu sagen, dass ein Neustart fehlt.
    """
    s = _skript()

    class _Antwort:
        def __init__(self, koerper: bytes) -> None:
            self._koerper = koerper

        def read(self) -> bytes:
            return self._koerper

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request

    original = urllib.request.urlopen
    try:
        urllib.request.urlopen = lambda *a, **k: _Antwort(b"<!doctype html>\n<html lang=\"de\">")
        with pytest.raises(s.EndpointFehlt):
            s.hole("http://127.0.0.1:8090", None, 5.0)

        # Auch gueltiges JSON ohne `routen` ist kein Schnappschuss.
        urllib.request.urlopen = lambda *a, **k: _Antwort(b'{"ok": true}')
        with pytest.raises(s.EndpointFehlt):
            s.hole("http://127.0.0.1:8090", None, 5.0)
    finally:
        urllib.request.urlopen = original
