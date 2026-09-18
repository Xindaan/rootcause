"""T-0472: unbekannte `/api`-Pfade muessen 404 liefern, nicht die index.html.

**Der Befund (01.08., live gegengeprueft 05.08.).** Der SPA-Fallback ist als
`@app.get("/{pfad:path}")` registriert und faengt damit auch `/api/...`, das
keine Route trifft. Der Kommentar darueber behauptete das Gegenteil ("Alle
nicht-API-Routen bekommen index.html"). Gemessen gegen die laufende App:
`/api/gibt-es-nicht` antwortete **200 `text/html`** mit der index.html,
waehrend `/api/zonen` korrekt 401 + JSON lieferte.

**Warum das teuer wird.** `authFetch` in `frontend/src/api.ts` prueft `r.ok`.
Bei 200 ist das `true`, der Aufruf laeuft in `r.json()` und stirbt an einem
JSON-Parse-Fehler. Ein umbenannter oder im laufenden Prozess fehlender
Endpoint meldet sich also nicht als "404", sondern als kaputtes JSON an einer
ganz anderen Stelle -- die Diagnose sucht dann im Frontend statt im Routing.
Gleiche Klasse wie `fehlerpattern_detektor_ohne_konsument`, nur von der
anderen Seite: der Fehlerpfad existiert, wird aber nie erreicht.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bewaesserung import api_server
from bewaesserung.api_server import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.skipif(
    not api_server._static_dir.is_dir(),
    reason="SPA-Fallback ist nur registriert, wenn static/ gebaut ist",
)
def test_unbekannter_api_pfad_liefert_404(client):
    antwort = client.get("/api/gibt-es-nicht-t0472")
    assert antwort.status_code == 404, (
        "unbekannter API-Pfad faellt in den SPA-Fallback -> das Frontend "
        "bekommt 200 + HTML und stirbt spaeter im JSON-Parser"
    )
    assert "text/html" not in antwort.headers.get("content-type", "")


@pytest.mark.skipif(
    not api_server._static_dir.is_dir(),
    reason="SPA-Fallback ist nur registriert, wenn static/ gebaut ist",
)
def test_nackter_api_pfad_liefert_404(client):
    """`/api` ohne Schraegstrich ist ebenfalls kein Frontend-Pfad."""
    assert client.get("/api").status_code == 404


@pytest.mark.skipif(
    not api_server._static_dir.is_dir(),
    reason="SPA-Fallback ist nur registriert, wenn static/ gebaut ist",
)
def test_frontend_route_bekommt_weiterhin_index_html(client):
    """Gegenprobe: der Fallback darf nicht zu scharf werden.

    Eine SPA-Route wie `/freigabe` ist keine Datei und muss weiterhin die
    index.html bekommen, sonst ist ein Reload auf jeder Unterseite kaputt.
    """
    antwort = client.get("/freigabe")
    assert antwort.status_code == 200
    assert "text/html" in antwort.headers.get("content-type", "")


@pytest.mark.skipif(
    not api_server._static_dir.is_dir(),
    reason="SPA-Fallback ist nur registriert, wenn static/ gebaut ist",
)
@pytest.mark.parametrize("pfad", [
    "/%2e%2e/%2e%2e/config/default.yaml",
    "/..%2f..%2fconfig%2fdefault.yaml",
    "/%2e%2e/%2e%2e/.env",
])
def test_kodierte_traversal_liefert_keine_fremde_datei(client, pfad):
    """T-0557: prozentkodierte Dot-Segmente duerfen nicht aus static/ fuehren.

    Der SPA-Fallback steht in `ROUTE_ROLLEN` als `public` -- diese Route ist
    also der einzige Weg ins Dateisystem, der KEINEN API-Key braucht. uvicorn
    `unquote`t den Rohpfad, bevor der Handler ihn sieht; ein Browser
    normalisiert nur die unkodierte Form weg. Vor dem Fix lieferte der erste
    Pfad hier die vollstaendige `config/default.yaml` (160 KB) mit
    `text/plain`, der dritte die `.env` mit den Zugangsdaten.

    Erwartung ist bewusst "index.html", nicht 403: ein 403 wuerde verraten,
    welche Pfade existieren.
    """
    antwort = client.get(pfad)
    assert antwort.status_code == 200
    assert "text/html" in antwort.headers.get("content-type", ""), (
        f"{pfad} liefert eine Datei ausserhalb von static/ aus"
    )
    assert b"GARDENA_CLIENT_SECRET" not in antwort.content
    assert b"zonen:" not in antwort.content


@pytest.mark.skipif(
    not api_server._static_dir.is_dir(),
    reason="SPA-Fallback ist nur registriert, wenn static/ gebaut ist",
)
def test_absoluter_pfad_liefert_keine_systemdatei(client):
    """`/%2fetc%2fhosts` -> pathlib ersetzt bei absolutem rechten Operanden
    den linken, `_static_dir / "/etc/hosts"` ist also `/etc/hosts`.

    Eigener Testfall statt Parametrisierung oben: das ist ein anderer
    Mechanismus (Absolutheit statt Dot-Segmente) und faellt nicht mit
    derselben Ursache.
    """
    antwort = client.get("/%2fetc%2fhosts")
    assert antwort.status_code == 200
    assert "text/html" in antwort.headers.get("content-type", "")
    assert b"localhost" not in antwort.content


@pytest.mark.skipif(
    not api_server._static_dir.is_dir(),
    reason="SPA-Fallback ist nur registriert, wenn static/ gebaut ist",
)
def test_echte_statische_datei_wird_weiterhin_ausgeliefert(client):
    """Gegenprobe zum Containment: die index.html selbst liegt IN static/
    und muss den Praefix-Vergleich passieren.

    Ohne diesen Fall wuerde ein zu scharfer Guard (der z. B. jede Datei
    ablehnt) von den Tests oben nicht bemerkt.
    """
    antwort = client.get("/index.html")
    assert antwort.status_code == 200
    assert "text/html" in antwort.headers.get("content-type", "")


@pytest.mark.skipif(
    not api_server._static_dir.is_dir(),
    reason="SPA-Fallback ist nur registriert, wenn static/ gebaut ist",
)
def test_pfad_der_nur_mit_api_beginnt_bleibt_frontend(client):
    """`/apidoku` ist kein API-Pfad -- die Grenze ist der Schraegstrich.

    Ein naives `startswith("api")` haette diese Route mit 404 beantwortet.
    """
    antwort = client.get("/apidoku")
    assert antwort.status_code == 200
    assert "text/html" in antwort.headers.get("content-type", "")
