#!/bin/bash
# Rueckwaertskompatibler Einstieg fuer das neue Top-Level-Service-Script.

set -euo pipefail

PROJEKT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec bash "${PROJEKT_ROOT}/service.sh" install
