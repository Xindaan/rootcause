#!/bin/bash
# Einfache Bedienoberflaeche fuer den macOS launchd-Service.

set -euo pipefail

PROJEKT_ROOT="$(cd "$(dirname "$0")" && pwd)"
LABEL="de.xindaan.pflanzen-dashboard"
PLIST_NAME="${LABEL}.plist"
PLIST_SRC="${PROJEKT_ROOT}/service/${PLIST_NAME}"
PLIST_DST="${HOME}/Library/LaunchAgents/${PLIST_NAME}"
LOG_DIR="${PROJEKT_ROOT}/daten/logs"
STDOUT_LOG="${LOG_DIR}/service-stdout.log"
STDERR_LOG="${LOG_DIR}/service-stderr.log"
FRONTEND="${PROJEKT_ROOT}/frontend"
API_HEALTH="http://127.0.0.1:8090/api/health"
DOMAIN="gui/$(id -u)"
SERVICE_TARGET="${DOMAIN}/${LABEL}"

usage() {
    cat <<EOF
Pflanzen-Dashboard Service

Nutzung:
  ./service.sh install            Service installieren und starten
  ./service.sh restart            Service neu starten
  ./service.sh restart --build    Frontend bauen und Service neu starten
  ./service.sh logs               Service-Logs live anzeigen
  ./service.sh status             Service- und API-Status anzeigen
  ./service.sh stop               Service stoppen

Dashboard: http://127.0.0.1:8090
EOF
}

service_geladen() {
    # Autoritativer Check: print exitet 0 wenn der Service im launchd-Domain registriert ist.
    launchctl print "${SERVICE_TARGET}" >/dev/null 2>&1
}

frontend_bauen() {
    echo "Baue Frontend..."
    (cd "${FRONTEND}" && npm run build)
    echo "Frontend-Build fertig."
}

plist_verlinken() {
    mkdir -p "${HOME}/Library/LaunchAgents" "${LOG_DIR}"
    # Die Repo-plist (PLIST_SRC) ist ein Template mit Platzhalter-Pfad
    # (/ABSOLUTER/PFAD/ZU/Gardena), damit kein maschinenspezifischer Pfad im
    # oeffentlichen Repo steht. Hier wird sie mit dem echten PROJEKT_ROOT
    # gerendert -- NICHT symlinken: ein Symlink auf die Platzhalter-plist
    # laesst launchd mit Exit 78 (EX_CONFIG) crashen (Pfad nicht aufloesbar).
    # rm -f zuerst, damit '>' nie durch einen alten Symlink in die Repo-Datei
    # schreibt.
    rm -f "${PLIST_DST}"
    sed "s|/ABSOLUTER/PFAD/ZU/Gardena|${PROJEKT_ROOT}|g" "${PLIST_SRC}" > "${PLIST_DST}"
}

service_laden() {
    # Idempotent: registriert den Service, falls noch nicht geschehen.
    # Modernes bootstrap statt legacy load - load wirft errno 5 wenn schon geladen.
    if service_geladen; then
        return 0
    fi
    launchctl bootstrap "${DOMAIN}" "${PLIST_DST}"
}

service_entladen() {
    # Idempotent: deregistriert den Service, falls geladen.
    if service_geladen; then
        launchctl bootout "${SERVICE_TARGET}"
    fi
}

service_installieren() {
    plist_verlinken
    service_laden
    echo "Starte Service..."
    launchctl kickstart -k "${SERVICE_TARGET}"
    echo "Service installiert und gestartet."
    echo "Dashboard: http://127.0.0.1:8090"
    echo "Logs:      ./service.sh logs"
}

service_neustarten() {
    if [ "${1:-}" = "--build" ]; then
        frontend_bauen
    elif [ -n "${1:-}" ]; then
        echo "Unbekannte restart-Option: $1" >&2
        echo "Erlaubt ist nur: ./service.sh restart --build" >&2
        exit 2
    fi

    plist_verlinken
    service_laden
    echo "Starte Service neu..."
    launchctl kickstart -k "${SERVICE_TARGET}"
    echo "Service laeuft. Dashboard: http://127.0.0.1:8090"
}

service_logs() {
    mkdir -p "${LOG_DIR}"
    touch "${STDOUT_LOG}" "${STDERR_LOG}"
    echo "Zeige Logs. Beenden mit Ctrl+C."
    tail -n 80 -f "${STDOUT_LOG}" "${STDERR_LOG}"
}

service_status() {
    echo "Service: ${LABEL}"
    if service_geladen; then
        pid="$(launchctl list 2>/dev/null | awk -v l="${LABEL}" '$3 == l {print $1}')"
        if [ -n "${pid}" ] && [ "${pid}" != "-" ]; then
            echo "  launchd: laeuft (PID ${pid})"
        else
            echo "  launchd: geladen, aber nicht aktiv"
        fi
    else
        echo "  launchd: nicht geladen"
    fi

    echo ""
    echo "API: ${API_HEALTH}"
    if antwort="$(curl -fsS --max-time 2 "${API_HEALTH}" 2>/dev/null)"; then
        echo "  erreichbar: ja"
        echo "  antwort: ${antwort}"
    else
        echo "  erreichbar: nein"
    fi

    echo ""
    echo "Logs:"
    echo "  ${STDOUT_LOG}"
    echo "  ${STDERR_LOG}"
}

service_stoppen() {
    if service_geladen; then
        echo "Stoppe Service..."
        service_entladen
        echo "Service gestoppt."
    else
        echo "Service ist nicht geladen."
    fi
}

case "${1:-}" in
    install)
        service_installieren
        ;;
    restart)
        if [ -n "${3:-}" ]; then
            echo "Zu viele Argumente fuer restart." >&2
            usage >&2
            exit 2
        fi
        service_neustarten "${2:-}"
        ;;
    logs)
        service_logs
        ;;
    status)
        service_status
        ;;
    stop)
        service_stoppen
        ;;
    -h|--help|help|"")
        usage
        ;;
    *)
        echo "Unbekannter Befehl: $1" >&2
        echo "" >&2
        usage >&2
        exit 2
        ;;
esac
