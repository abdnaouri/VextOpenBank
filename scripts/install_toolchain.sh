#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
PYTHON_BIN="python3.12"

echo "=== [1/5] Checking Homebrew tools ==="
for pkg in apktool jadx jq ripgrep; do
  if ! command -v "$pkg" >/dev/null 2>&1 && ! command -v "${pkg/ripgrep/rg}" >/dev/null 2>&1; then
    echo "Installing $pkg via brew..."
    NONINTERACTIVE=1 brew install "$pkg" || true
  else
    echo "  $pkg: OK"
  fi
done

echo "=== [2/5] Creating Python 3.12 venv at $VENV ==="
if [ ! -d "$VENV" ]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install apkradar frida-tools apkid androguard requests jinja2 --quiet

echo "=== [3/5] Installing mobile-security-mcp via npm ==="
if command -v npm >/dev/null 2>&1; then
  npm install -g mobile-security-mcp 2>/dev/null || echo "npm install -g mobile-security-mcp skipped or failed"
fi

echo "=== [4/5] Downloading community Frida bypass suites ==="
FRIDA_DIR="$ROOT/work/dynamic"
SRC_DIR="$ROOT/.frida-src"
mkdir -p "$FRIDA_DIR" "$SRC_DIR"

if [ ! -d "$SRC_DIR/bypass-suite" ]; then
  echo "Cloning android-frida-bypass-suite..."
  git clone --depth=1 https://github.com/mrmtwoj/android-frida-bypass-suite "$SRC_DIR/bypass-suite" 2>/dev/null || true
fi
if [ -f "$SRC_DIR/bypass-suite/acyber_frida.js" ]; then
  cp "$SRC_DIR/bypass-suite/acyber_frida.js" "$FRIDA_DIR/"
fi

if [ ! -d "$SRC_DIR/mobile-kit" ]; then
  echo "Cloning frida-mobile-kit..."
  git clone --depth=1 https://github.com/unrandoms/frida-mobile-kit "$SRC_DIR/mobile-kit" 2>/dev/null || true
fi
if [ -f "$SRC_DIR/mobile-kit/scripts/traffic/log-http-requests.js" ]; then
  cp "$SRC_DIR/mobile-kit/scripts/traffic/log-http-requests.js" "$FRIDA_DIR/"
fi

echo "=== [5/5] Toolchain Verification ==="
"$VENV/bin/python" -c "import apkradar; print('  apkradar: OK')"
"$VENV/bin/python" -c "import frida; print('  frida host library: ' + frida.__version__)"
"$VENV/bin/python" -c "import apkid; print('  apkid: OK')"
if command -v apktool >/dev/null 2>&1; then
  echo "  apktool: $(apktool --version | head -n 1)"
fi
if command -v jadx >/dev/null 2>&1; then
  echo "  jadx: $(jadx --version 2>&1 | head -n 1)"
fi
if command -v adb >/dev/null 2>&1; then
  echo "  adb: $(adb version 2>&1 | head -n 1)"
fi

echo ""
echo "Toolchain installation and verification complete."
echo "Note: Ensure frida-server matching host version is running on your Android test device."
