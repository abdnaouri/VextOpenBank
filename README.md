# VextOpenBank — Interoperability Audit Harness

Harness for authorized client interoperability audit of `com.cfgbank.mobileapp`.

## Prerequisites

- macOS Apple Silicon with Homebrew
- Python 3.12+ (isolated automatically via `.venv`)
- Node.js / npm (Volta supported)
- Tools: `apktool`, `jadx`, `ripgrep`, `jq`, `frida-tools`, `apkid`, `apkradar`

## Setup

Run the toolchain bootstrap once:

```bash
./scripts/install_toolchain.sh
```

This:
1. Installs/updates Homebrew tools (`apktool`, `jadx`, `jq`, `ripgrep`)
2. Creates `.venv` using Python 3.12 and installs `apkradar`, `frida-tools`, `apkid`, `androguard`, `requests`, `jinja2`
3. Installs `mobile-security-mcp` globally via npm
4. Pulls community Frida bypass suites into `work/dynamic/`

## Pipeline Execution

Drop your APK into `work/inbox/`:

```bash
cp cfg_bank_merged.apk work/inbox/client.apk
```

Run stages:

```bash
# Run automated static analysis phases (setup -> profile -> decompile -> extract -> instrument)
./pipeline.sh all

# Or run individual stages:
./pipeline.sh setup       # verify toolchain
./pipeline.sh profile     # package triage, packer and framework fingerprint
./pipeline.sh decompile   # apktool and jadx decompilation
./pipeline.sh extract     # extract interface contract
./pipeline.sh instrument  # prints Frida attach instructions
./pipeline.sh reconcile   # reconciles static contract with dynamic exchanges.jsonl
```

## Runtime Capture (Phase 6)

Launch Frida with all security compatibility and observation layers:

```bash
frida -U -f com.cfgbank.mobileapp \
  -l work/dynamic/compat_environment.js \
  -l work/dynamic/transport_shim.js \
  -l work/dynamic/transport_observe.js \
  -l work/dynamic/integrity_observe.js \
  --no-pause
```

Drive the app through login, OTP, dashboard, and account transfer flows. Then pull logs:

```bash
adb pull /sdcard/exchanges.jsonl work/logs/
adb pull /sdcard/integrity.jsonl work/logs/
```

## Reconciliation (Phase 7)

```bash
./scripts/orchestrate.sh
```

Generates `work/contract/surface_report.json` and `work/contract/surface_report.md`.

- **DYNAMIC (`runtime_only`)**: Endpoints invoked at runtime but absent statically (reflection / dynamic concatenation) — **priority audit target**.
- **DEAD (`static_only`)**: Static definitions never hit at runtime (feature-flagged / dead code).
- **ACTIVE (`matched`)**: Endpoints confirmed in both static and dynamic surfaces.
- **Chains**: Multi-service request clusters arriving within 2s across >= 2 hosts.

## Reproduction Harness (Phase 8 & 9)

```bash
.venv/bin/python scripts/gen_harness.py
.venv/bin/python work/repro/replay.py --url "<target>"
.venv/bin/python work/repro/replay.py --url "<target>" --strip-auth
```
