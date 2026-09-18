"""tools/ui_pruefserver.py darf NIE einen schreibenden Request weiterleiten.

Der Server steht bei UI-Pruefungen vor dem Live-Backend (Port 8090, echte
Ventilsteuerung). Ein "Giessen"-Knopf ist beim Durchklicken immer in
Reichweite; der Schutz muss im Server sitzen.

Zwei Regeln aus den Lehren vom 04.09.2026 (Kicktipp):
1. Der Testkoerper muss auch OHNE den Schutz harmlos sein. Deshalb laeuft der
   Test gegen ein ATTRAPPEN-Backend, das Requests nur mitschreibt -- niemals
   gegen 8090. Faellt der Riegel in der Negativprobe weg, trifft der POST die
   Attrappe.
2. Wenn zwei Ursachen dasselbe Signal liefern, prueft der Test die Ursache.
   Ohne eigenen Riegel antwortet die stdlib mit 501 und leitet AUCH nicht
   weiter -- "POST kam nicht an" waere aus zwei Gruenden gruen. Geprueft wird
   deshalb 405 plus die eigene Meldung.
"""
from __future__ import annotations

import http.server
import importlib.util
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[2]

# Im oeffentlichen Snapshot ist tools/ui_pruefserver.py bewusst nicht enthalten. Erkannt wird
# der Snapshot am fehlenden `config/default.yaml` -- NICHT am fehlenden
# tools/ui_pruefserver.py: sonst wuerde ein privat geloeschtes Werkzeug seinen
# Test still ueberspringen statt ihn scheitern zu lassen.
if not (WURZEL / "config" / "default.yaml").exists():
    pytest.skip("oeffentlicher Snapshot: tools/ui_pruefserver.py ist privat", allow_module_level=True)


def _lade_modul():
    spec = importlib.util.spec_from_file_location(
        "ui_pruefserver", WURZEL / "tools" / "ui_pruefserver.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def aufbau(tmp_path):
    empfangen: list[tuple[str, str, str | None]] = []

    class Attrappe(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _merke(self):
            empfangen.append((self.command, self.path, self.headers.get("X-Api-Key")))
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _merke

    backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Attrappe)
    threading.Thread(target=backend.serve_forever, daemon=True).start()

    (tmp_path / "index.html").write_text("<html>app</html>")
    mod = _lade_modul()
    proxy = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        mod.baue_handler(tmp_path, f"http://127.0.0.1:{backend.server_port}"),
    )
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{proxy.server_port}", empfangen
    finally:
        proxy.shutdown()
        backend.shutdown()


def _anfrage(url, methode, key="k"):
    req = urllib.request.Request(url, method=methode, data=b"{}" if methode != "GET" else None)
    req.add_header("X-Api-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.mark.parametrize("methode", ["POST", "PUT", "PATCH", "DELETE"])
def test_schreibende_requests_erreichen_das_backend_nie(aufbau, methode):
    """Z. B. `POST /api/ventil/manuell-start` -- beim Klicken schnell passiert."""
    basis, empfangen = aufbau
    status, body = _anfrage(f"{basis}/api/ventil/manuell-start", methode)
    assert empfangen == [], f"{methode} wurde weitergeleitet: {empfangen}"
    # Die URSACHE, nicht nur das Signal (s. Modul-Docstring, Regel 2).
    assert status == 405, status
    assert b"nur GET/HEAD erlaubt" in body, body


def test_get_wird_mit_api_key_weitergeleitet(aufbau):
    """Gegenstueck: ohne weitergeleitetes GET waere der Server nutzlos -- und
    der Test oben auch dann gruen, wenn gar nichts durchginge."""
    basis, empfangen = aufbau
    status, body = _anfrage(f"{basis}/api/zonen?x=1", "GET", key="geheim")
    assert status == 200, body
    assert empfangen == [("GET", "/api/zonen?x=1", "geheim")]


def test_statische_dateien_und_spa_rueckfall(aufbau):
    basis, empfangen = aufbau
    for pfad in ("/", "/uebersicht/zone/7"):
        status, body = _anfrage(f"{basis}{pfad}", "GET")
        assert status == 200 and b"app" in body, pfad
    assert empfangen == [], "statische Pfade duerfen nicht ans Backend"
