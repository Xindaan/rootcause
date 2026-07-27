#!/bin/bash
# Pflanzen-Dashboard starten (Backend + Frontend in einem Prozess)
#
# Nutzung:
#   ./start.sh              — Startet interaktiv mit Browser (Port 8090)
#   ./start.sh --service    — Daemon-Modus fuer launchd (kein kill, kein Browser)
#   ./start.sh --dev        — Startet Backend + Vite-Dev-Server (Port 5173)
#   ./start.sh --build      — Baut Frontend neu und startet
#   ./start.sh --help       — Zeigt diese Hilfe
#
# Dashboard erreichbar unter: http://127.0.0.1:8090
#
# T-0098: Interaktive Modi (default/--dev/--build) entladen automatisch
# einen aktiven launchd-Daemon (de.xindaan.pflanzen-dashboard) und
# killen Orphan-Prozesse vor Start. Reload des Daemons danach manuell:
#     launchctl load ~/Library/LaunchAgents/de.xindaan.pflanzen-dashboard.plist
# Der --service-Modus selbst macht KEINE Hygiene — der Single-Instance-
# Lock im Backend (main.py) faengt Konflikte sauber ab und exitet mit
# Exit-Code 1, damit launchd nicht in Crash-Loops respawnt.

set -euo pipefail

PROJEKT_ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="${PROJEKT_ROOT}/.venv/bin"
FRONTEND="${PROJEKT_ROOT}/frontend"
BACKEND="${PROJEKT_ROOT}/backend"
STATIC="${BACKEND}/static"

usage() {
    cat <<EOF
Pflanzen-Dashboard starten

Nutzung:
  ./start.sh              Interaktiv starten, Browser oeffnen
  ./start.sh --build      Frontend immer neu bauen, dann starten
  ./start.sh --dev        Backend + Vite Dev-Server starten
  ./start.sh --service    Daemon-Modus fuer launchd

Service einfacher bedienen:
  ./service.sh install
  ./service.sh restart
  ./service.sh restart --build
  ./service.sh logs

Dashboard: http://127.0.0.1:8090
EOF
}

# .env laden
if [ -f "${PROJEKT_ROOT}/.env" ]; then
    set -a; source "${PROJEKT_ROOT}/.env"; set +a
fi

export PYTHONPATH="${BACKEND}/src"
export PATH="${VENV}:${PATH}"

LAUNCHD_LABEL="de.xindaan.pflanzen-dashboard"
LAUNCHD_PLIST="${HOME}/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"

# T-0098 Pre-Flight-Check (Bug-Quelle 30.04.):
# 1. Wenn der launchd-Daemon aktiv ist, kollidiert er mit dem
#    interaktiven Start (Port 8090 + DB-Lock). Wir laden ihn aus,
#    bevor wir starten — beim Beenden wird er nicht automatisch
#    wieder geladen, das macht der User explizit.
# 2. Killt Orphan-`bewaesserung.main`-Prozesse (z. B. nohup-Reste,
#    Memory `fehlerpattern_backend_orphan_prozess.md`).
# 3. Faellt zurueck auf alten lsof-Pfad fuer Port 8090.
beende_alte_instanz() {
    if launchctl list 2>/dev/null | grep -q "${LAUNCHD_LABEL}"; then
        echo "⚠  launchd-Daemon ${LAUNCHD_LABEL} laeuft — entlade ihn vor Start."
        echo "   Wieder aktivieren am Ende: launchctl load ${LAUNCHD_PLIST}"
        launchctl unload "${LAUNCHD_PLIST}" 2>/dev/null || true
        sleep 1
    fi
    if pgrep -f "bewaesserung.main" >/dev/null 2>&1; then
        echo "Stoppe alte bewaesserung.main-Prozesse..."
        pkill -f "bewaesserung.main" 2>/dev/null || true
        sleep 2
    fi
    alte_pids=$(lsof -ti:8090 2>/dev/null || true)
    if [ -n "$alte_pids" ]; then
        echo "Port 8090 noch belegt von PID ${alte_pids} — kille."
        echo "$alte_pids" | xargs kill 2>/dev/null || true
        sleep 1
    fi
    # T-0127 (H-3a): Lock-Pfad nach ~/Library/Application Support verschoben.
    # Alten /tmp-Pfad noch mitraeumen, solange Bestand da ist (Migration).
    LOCK_PFAD_NEU="${HOME}/Library/Application Support/de.xindaan.pflanzen-dashboard/bewaesserung.pid"
    LOCK_PFAD_ALT="/tmp/bewaesserung.pid"
    for lock_pfad in "${LOCK_PFAD_NEU}" "${LOCK_PFAD_ALT}"; do
        if [ -f "${lock_pfad}" ]; then
            # Single-Instance-Lock-File aus main.py — uebrig, wenn vorheriger
            # Prozess hart gekillt wurde. flock im neuen Prozess macht das
            # wieder sauber, aber wir raeumen Stale-Files vor dem Start.
            stale_pid=$(cat "${lock_pfad}" 2>/dev/null || echo "?")
            if ! kill -0 "${stale_pid}" 2>/dev/null; then
                rm -f "${lock_pfad}"
            fi
        fi
    done
}

fall_back_build() {
    echo "⚙  Frontend wird gebaut..."
    cd "${FRONTEND}"
    npm run build
    cd "${PROJEKT_ROOT}"
    echo "✓  Build fertig: ${STATIC}"
}

case "${1:-}" in
    -h|--help|help)
        usage
        ;;
    --dev)
        export GARDENA_START_MODUS="interaktiv"
        beende_alte_instanz
        echo "=== Dev-Modus: Backend + Vite ==="
        cd "${BACKEND}"
        python -m bewaesserung.main &
        BACKEND_PID=$!
        cd "${FRONTEND}"
        npx vite --host &
        VITE_PID=$!
        trap "kill $BACKEND_PID $VITE_PID 2>/dev/null" EXIT
        echo ""
        echo "Backend:   http://127.0.0.1:8090"
        echo "Dashboard: http://localhost:5173"
        echo ""
        wait
        ;;
    --build)
        export GARDENA_START_MODUS="interaktiv"
        beende_alte_instanz
        fall_back_build
        echo ""
        echo "=== Starte Pflanzen-Dashboard ==="
        cd "${BACKEND}"
        exec python -m bewaesserung.main
        ;;
    --service)
        export GARDENA_START_MODUS="service"
        # Daemon-Modus: Kein kill, kein Browser — fuer launchd/Autostart
        if [ ! -f "${STATIC}/index.html" ]; then
            echo "Kein Frontend-Build gefunden."
            fall_back_build
        fi
        echo "=== Pflanzen-Dashboard (Service) ==="
        cd "${BACKEND}"
        exec python -m bewaesserung.main
        ;;
    "")
        export GARDENA_START_MODUS="interaktiv"
        beende_alte_instanz
        # Interaktiv: Pruefen ob Build vorhanden, Browser oeffnen
        if [ ! -f "${STATIC}/index.html" ]; then
            echo "Kein Frontend-Build gefunden."
            fall_back_build
        fi
        echo "=== Pflanzen-Dashboard ==="
        echo "Dashboard: http://127.0.0.1:8090"
        echo ""
        # Browser oeffnen sobald Server bereit ist
        (sleep 3 && open "http://127.0.0.1:8090") &
        cd "${BACKEND}"
        exec python -m bewaesserung.main
        ;;
    *)
        echo "Unbekannte Option: $1" >&2
        echo "" >&2
        usage >&2
        exit 2
        ;;
esac
