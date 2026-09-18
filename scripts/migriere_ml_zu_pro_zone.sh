#!/bin/bash
# T-0082: Initial-Trainings-Lauf pro Cluster (= pro Zone, default).
#
# Iteriert ueber alle distinkten cluster_ids aus config/default.yaml.
# Pro Cluster wird `python -m bewaesserung.ml.cli trainiere --cluster X
# --quantile` aufgerufen. Cluster mit < mindest_zeilen (Default 1500)
# werden in der TrainingsPipeline geskippt mit klarem Log-Hinweis.
#
# Nach erfolgreichem Lauf liegen Modelle unter
# `backend/daten/ml/feuchte/<cluster_id>/<datum>/`.
# Symlinks `aktuell_*h.lgbm` zeigen jeweils auf die juengste Trainings-
# Version. Das alte globale `aktuell_*h.lgbm` im Wurzel-ML-Dir bleibt
# unangetastet — Inferenz nutzt es als Fallback fuer Zonen ohne
# Pro-Cluster-Modell.
#
# Nach Lauf:
#   1. `cluster_strategie: pro_zone` in config/default.yaml unter
#      `ml.retrain` setzen, damit Auto-Retrain pro Cluster geht.
#   2. Backend-Restart.

set -euo pipefail

PROJEKT_ROOT="${GARDENA_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
VENV_PYTHON="${PROJEKT_ROOT}/.venv/bin/python"
CONFIG="${PROJEKT_ROOT}/config/default.yaml"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "FEHLER: $VENV_PYTHON nicht gefunden." >&2
    exit 1
fi

# Cluster-Liste aus Konfig extrahieren (cluster_id || zone_id).
CLUSTER_IDS=$("$VENV_PYTHON" - <<'PY'
import sys
sys.path.insert(0, "backend/src")
from bewaesserung.konfig import lade_konfig
konfig = lade_konfig()
clusters: dict[str, list[str]] = {}
for zone in konfig.zonen:
    cid = zone.cluster_id or zone.zone_id
    clusters.setdefault(cid, []).append(zone.zone_id)
for cid in sorted(clusters):
    print(f"{cid}\t{','.join(clusters[cid])}")
PY
)

VON=$(date -v-60d +%Y-%m-%d 2>/dev/null || date -d '60 days ago' +%Y-%m-%d)
BIS=$(date +%Y-%m-%d)

cd "$PROJEKT_ROOT/backend"

echo "=== T-0082 Pro-Zone-Cluster-Trainings-Lauf ==="
echo "  Zeitfenster: $VON .. $BIS"
echo ""

ERFOLG=0
SKIPPED=0
FEHLER=0
while IFS=$'\t' read -r cluster_id zone_list; do
    echo "--- Cluster $cluster_id ($zone_list) ---"
    if "$VENV_PYTHON" -m bewaesserung.ml.cli trainiere \
        --von "$VON" --bis "$BIS" --quantile --cluster "$cluster_id"; then
        ERFOLG=$((ERFOLG + 1))
        echo "  OK"
    else
        rc=$?
        if [[ $rc -eq 0 ]]; then
            SKIPPED=$((SKIPPED + 1))
        else
            FEHLER=$((FEHLER + 1))
            echo "  FEHLER (exit=$rc)"
        fi
    fi
    echo ""
done <<< "$CLUSTER_IDS"

echo "=== Fertig ==="
echo "  Erfolgreich trainiert: $ERFOLG"
echo "  Uebersprungen        : $SKIPPED"
echo "  Fehler               : $FEHLER"
echo ""
echo "Naechste Schritte:"
echo "  1. config/default.yaml ml.retrain.cluster_strategie auf 'pro_zone' setzen"
echo "  2. Backend restart (./service.sh restart oder ./start.sh)"
echo "  3. /api/ml/status pruefen — pro-Zone-Modelle aktiv?"
