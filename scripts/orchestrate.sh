#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"

if [ ! -x "$VENV/bin/python" ]; then
    echo "Error: Python venv not found. Run ./scripts/install_toolchain.sh first." >&2
    exit 1
fi

STATIC_CONTRACT="$ROOT/work/contract/interface_contract.json"
DYNAMIC_LOG="$ROOT/work/logs/exchanges.jsonl"
REPORT_OUT="$ROOT/work/contract/surface_report.json"

mkdir -p "$ROOT/work/contract"

"$VENV/bin/python" "$ROOT/scripts/reconcile_surfaces.py" \
    --static "$STATIC_CONTRACT" \
    --dynamic "$DYNAMIC_LOG" \
    --out "$REPORT_OUT" \
    "$@"

echo ""
echo "=== Surface Reconciliation Complete ==="
if [ -f "$REPORT_OUT" ] && command -v jq >/dev/null 2>&1; then
    printf "Runtime-only (DYNAMIC): %s\n" "$(jq '.runtime_only | length' "$REPORT_OUT")"
    printf "Static-only (DEAD):     %s\n" "$(jq '.static_only | length' "$REPORT_OUT")"
    printf "Matched (ACTIVE):       %s\n" "$(jq '.matched | length' "$REPORT_OUT")"
    printf "Multi-service Chains:   %s\n" "$(jq '.chains | length' "$REPORT_OUT")"
fi
echo "Markdown summary: ${REPORT_OUT%.json}.md"
