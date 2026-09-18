#!/bin/bash
# ml-drift-bewertung.sh — schneller MAE-Vergleich pro ML-Modell-Version.
#
# Zweck:
#   Nach einem Retrain (CLI oder Auto) sehen, ob das NEUE Modell besser
#   ist als das alte — getrennt fuer "alle Live-Daten" vs. "nur neue
#   Modell-Version" pro Zone+Horizont.
#
# Nutzung:
#   ./scripts/ml-drift-bewertung.sh                     # heutige Version
#   ./scripts/ml-drift-bewertung.sh 2026-04-26          # spezifische Version
#   ./scripts/ml-drift-bewertung.sh --vergleich         # alt vs. neu Side-by-Side
#
# Voraussetzung: vom Repo-Root aus aufrufen, oder $GARDENA_ROOT setzen.

set -euo pipefail

DB="${GARDENA_ROOT:-.}/backend/daten/bewaesserung.db"
if [[ ! -f "$DB" ]]; then
    echo "FEHLER: $DB nicht gefunden. Vom Repo-Root starten oder GARDENA_ROOT setzen." >&2
    exit 1
fi

ARG="${1:-$(date +%Y-%m-%d)}"

if [[ "$ARG" == "--vergleich" ]]; then
    echo "=== Alle Modell-Versionen mit ausreichender Eval-Anzahl (n>=5) ==="
    sqlite3 "$DB" "
SELECT modell_version,
       horizont_h,
       COUNT(*) AS n,
       ROUND(AVG(ABS(abweichung)), 2) AS mae_pp,
       ROUND(AVG(abweichung), 2) AS bias_pp,
       MIN(date(evaluiert_am)) AS erste_eval,
       MAX(date(evaluiert_am)) AS letzte_eval
  FROM ml_vorhersage_log
 WHERE abweichung IS NOT NULL
 GROUP BY modell_version, horizont_h
HAVING COUNT(*) >= 5
 ORDER BY modell_version DESC, horizont_h;
"
    exit 0
fi

echo "=== Live-MAE pro Zone+Horizont, Modell-Version *${ARG}* ==="
echo "    (leer = noch keine Live-Evaluierungen vom neuen Modell)"
echo ""
sqlite3 "$DB" "
SELECT zone_id, horizont_h,
       COUNT(*) AS n,
       ROUND(AVG(ABS(abweichung)), 2) AS mae_pp,
       ROUND(AVG(abweichung), 2) AS bias_pp,
       MIN(evaluiert_am) AS erste_eval,
       MAX(evaluiert_am) AS letzte_eval
  FROM ml_vorhersage_log
 WHERE abweichung IS NOT NULL
   AND modell_version LIKE '%${ARG}%'
 GROUP BY zone_id, horizont_h
 ORDER BY zone_id, horizont_h;
"

echo ""
echo "=== Backlog Drift-Job: noch offene Zeilen ==="
sqlite3 "$DB" "
SELECT horizont_h, COUNT(*) AS n_offen_und_faellig
  FROM ml_vorhersage_log
 WHERE evaluiert_am IS NULL
   AND prognose_ziel_zeit <= datetime('now', 'localtime', '-30 minutes')
 GROUP BY horizont_h
 ORDER BY horizont_h;
"

echo ""
echo "Hinweis:"
echo "  - 6h-Prognosen vom neuen Modell brauchen 6h Wartezeit + Drift-Job-Tick"
echo "    (lauft 1x/h) bevor sie hier auftauchen."
echo "  - Bei n<5 ist der MAE noch nicht aussagekraeftig — Rauschen dominiert."
echo "  - Fuer Side-by-Side mit dem Vorgaenger-Modell:"
echo "      ./scripts/ml-drift-bewertung.sh --vergleich"
