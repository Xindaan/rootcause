#!/bin/bash
# Doppelklick-Log-Betrachter fuer den Pflanzen-Dashboard launchd-Service.
# Finder-Doppelklick oeffnet ein Terminal und tailt stdout + stderr live.
# Beenden: Ctrl+C oder Fenster schliessen - das Backend laeuft als Daemon weiter.

PROJEKT_ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${PROJEKT_ROOT}/daten/logs"
STDOUT_LOG="${LOG_DIR}/service-stdout.log"
STDERR_LOG="${LOG_DIR}/service-stderr.log"

mkdir -p "${LOG_DIR}"
touch "${STDOUT_LOG}" "${STDERR_LOG}"

echo "=== Pflanzen-Dashboard Logs (live) - Ctrl+C zum Beenden ==="
echo "stdout: ${STDOUT_LOG}"
echo "stderr: ${STDERR_LOG}"
echo

exec tail -n 120 -f "${STDOUT_LOG}" "${STDERR_LOG}"
