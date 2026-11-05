#!/usr/bin/env bash
# VextOpenBank — 6-stage APK analysis orchestration
# Stage 1: setup    Stage 2: profile     Stage 3: decompile
# Stage 4: extract  Stage 5: instrument  Stage 6: reconcile

set -uo pipefail
shopt -s nullglob

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$ROOT/work"
LOGS="$WORK/logs"
PIPELINE_LOG="$LOGS/pipeline.jsonl"
TIMESTAMP_FMT='%Y-%m-%dT%H:%M:%SZ'
VENV="$ROOT/.venv"

# Ensure tools in homebrew and android build-tools are in PATH
export PATH="/opt/homebrew/bin:/opt/homebrew/share/android-commandlinetools/build-tools/34.0.0:$PATH"

if [ -f "$VENV/bin/activate" ]; then
  # shellcheck source=/dev/null
  source "$VENV/bin/activate"
fi

PYTHON_BIN="python3"
if [ -x "$VENV/bin/python" ]; then
  PYTHON_BIN="$VENV/bin/python"
fi

get_time_ms() {
  "$PYTHON_BIN" -c 'import time; print(int(time.time() * 1000))'
}

mkdir -p "$WORK"/{inbox,triage,static,contract,dynamic,logs,repro} "$ROOT"/{scripts,config}
touch "$PIPELINE_LOG"

log_event() {
  local phase="$1" status="$2" artifact="$3" dur_ms="$4"
  printf '{"ts":"%s","phase":"%s","status":"%s","artifact":"%s","duration_ms":%s}\n' \
    "$(date -u +"$TIMESTAMP_FMT")" "$phase" "$status" "$artifact" "$dur_ms" >> "$PIPELINE_LOG"
  echo "[$phase] $status — $artifact (${dur_ms}ms)"
}

run_stage() {
  local name="$1"; shift
  local start; start=$(get_time_ms)
  if "$@"; then
    local end; end=$(get_time_ms)
    log_event "$name" "ok" "$ARTIFACT" "$((end - start))"
    return 0
  else
    local rc=$?; local end; end=$(get_time_ms)
    log_event "$name" "fail:$rc" "$ARTIFACT" "$((end - start))"
    return $rc
  fi
}

# ---------- Stage 1: environment ----------
ARTIFACT="$WORK/triage/env.txt"
stage_setup() {
  # shellcheck source=/dev/null
  source ~/.zshrc 2>/dev/null || true
  : > "$ARTIFACT"
  for t in adb aapt2 apktool jadx apkleaks frida frida-ps python3 jq rg; do
    if command -v "$t" >/dev/null 2>&1; then
      printf '%s\t%s\n' "$t" "$(command -v "$t")" >> "$ARTIFACT"
    else
      printf '%s\tMISSING\n' "$t" >> "$ARTIFACT"
    fi
  done
  echo "--- toolchain ---"; cat "$ARTIFACT"
}

# ---------- Stage 2: profile ----------
ARTIFACT="$WORK/triage/profile.json"
stage_profile() {
  local apk="$WORK/inbox/client.apk"
  [[ -f "$apk" ]] || { echo "no APK at $apk"; return 1; }
  bash "$ROOT/scripts/profile.sh" "$apk" || return 1
  return 0
}

# ---------- Stage 3: decompile ----------
ARTIFACT="$WORK/static"
stage_decompile() {
  local apk="$WORK/inbox/client.apk"
  [[ -f "$apk" ]] || return 1
  local start; start=$(date +%s)
  if command -v apktool >/dev/null 2>&1; then
    apktool d -f -o "$WORK/static/apktool" "$apk" >> "$LOGS/apktool.log" 2>&1 || true
  fi
  if command -v jadx >/dev/null 2>&1; then
    jadx -d "$WORK/static/jadx" --no-res "$apk" >> "$LOGS/jadx.log" 2>&1 || true
  fi
  local end; end=$(date +%s)
  echo "decompile wallclock=$((end-start))s"
}

# ---------- Stage 4: contract extraction ----------
ARTIFACT="$WORK/contract/interface_contract.json"
stage_extract() {
  "$PYTHON_BIN" "$ROOT/scripts/discover_contract.py" "$WORK/static/jadx" || return 1
  [[ -f "$ARTIFACT" ]]
}

# ---------- Stage 5: instrumentation (manual launch) ----------
ARTIFACT="$WORK/dynamic"
stage_instrument() {
  echo "--- instrumentation ready ---"
  echo "Launch with:"
  echo "  frida -U -f com.cfgbank.mobileapp \\"
  echo "    -l $WORK/dynamic/compat_environment.js \\"
  echo "    -l $WORK/dynamic/transport_shim.js \\"
  echo "    -l $WORK/dynamic/transport_observe.js \\"
  echo "    -l $WORK/dynamic/integrity_observe.js \\"
  echo "    --no-pause"
  echo "Then: adb pull /sdcard/exchanges.jsonl $LOGS/"
  echo "      adb pull /sdcard/integrity.jsonl $LOGS/"
}

# ---------- Stage 6: reconcile ----------
ARTIFACT="$WORK/contract/surface_report.json"
stage_reconcile() {
  "$PYTHON_BIN" "$ROOT/scripts/reconcile_surfaces.py" \
    --static "$WORK/contract/interface_contract.json" \
    --dynamic "$LOGS/exchanges.jsonl" \
    --out "$WORK/contract/surface_report.json" || return 1
  [[ -f "$ARTIFACT" ]]
}

# ---------- dispatch ----------
stage="${1:-all}"
case "$stage" in
  setup)      run_stage setup stage_setup ;;
  profile)    run_stage profile stage_profile ;;
  decompile)  run_stage decompile stage_decompile ;;
  extract)    run_stage extract stage_extract ;;
  instrument) run_stage instrument stage_instrument ;;
  reconcile)  run_stage reconcile stage_reconcile ;;
  all)
    run_stage setup stage_setup
    run_stage profile stage_profile
    run_stage decompile stage_decompile
    run_stage extract stage_extract
    run_stage instrument stage_instrument
    echo
    echo "Drop exchanges.jsonl into $LOGS/ then run:"
    echo "  $0 reconcile"
    ;;
  *)
    echo "usage: $0 {setup|profile|decompile|extract|instrument|reconcile|all}"
    exit 2
    ;;
esac
