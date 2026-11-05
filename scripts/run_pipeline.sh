#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"

if [ -f "$VENV/bin/activate" ]; then
    # shellcheck source=/dev/null
    source "$VENV/bin/activate"
fi

PYTHON_BIN="python3"
if [ -x "$VENV/bin/python" ]; then
    PYTHON_BIN="$VENV/bin/python"
fi

mkdir -p "$ROOT/work/logs"

log_step() {
    local phase="$1"
    local status="$2"
    local artifact="$3"
    local duration_ms="$4"
    printf '{"phase":"%s","status":"%s","artifact":"%s","duration_ms":%d}\n' \
        "$phase" "$status" "$artifact" "$duration_ms" >> "$ROOT/work/logs/pipeline.jsonl"
}

run_step() {
    local phase="$1"
    local artifact="$2"
    shift 2
    local start_time
    start_time=$("$PYTHON_BIN" -c 'import time; print(int(time.time() * 1000))')

    echo "=== Running Phase: $phase ==="
    if "$@"; then
        local end_time
        end_time=$("$PYTHON_BIN" -c 'import time; print(int(time.time() * 1000))')
        log_step "$phase" "SUCCESS" "$artifact" $((end_time - start_time))
    else
        local end_time
        end_time=$("$PYTHON_BIN" -c 'import time; print(int(time.time() * 1000))')
        echo "Error in phase: $phase" >&2
        echo "Failed command: $*" >&2
        log_step "$phase" "FAILURE" "$artifact" $((end_time - start_time))
    fi
}

run_step "SETUP" "$ROOT/work/" "$ROOT/scripts/setup.sh"

APK_TARGET="$ROOT/work/inbox/client.apk"
if [ ! -f "$APK_TARGET" ]; then
    echo "Notice: Target package not yet placed at $APK_TARGET."
    echo "Drop client.apk into work/inbox/ and re-run."
    exit 0
fi

run_step "PROFILE" "$ROOT/work/triage/profile.json" "$ROOT/scripts/profile.sh" "$APK_TARGET"

mkdir -p "$ROOT/work/static/apktool" "$ROOT/work/static/jadx"

if command -v apktool >/dev/null 2>&1; then
    run_step "STATIC_APKTOOL" "$ROOT/work/static/apktool" apktool d -f "$APK_TARGET" -o "$ROOT/work/static/apktool"
fi

if command -v jadx >/dev/null 2>&1; then
    run_step "STATIC_JADX" "$ROOT/work/static/jadx" jadx -d "$ROOT/work/static/jadx" "$APK_TARGET"
fi

run_step "DISCOVER_CONTRACT" "$ROOT/work/contract/interface_contract.json" "$PYTHON_BIN" "$ROOT/scripts/discover_contract.py" "$ROOT/work/static/jadx"

echo ""
echo "Pipeline static execution completed."
