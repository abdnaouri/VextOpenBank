#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
APK_PATH="${1:-}"

if [ -z "$APK_PATH" ] || [ ! -f "$APK_PATH" ]; then
    echo "Usage: $0 <path-to-apk>"
    exit 1
fi

mkdir -p "$ROOT/work/triage"

if command -v aapt2 >/dev/null 2>&1; then
    aapt2 dump badging "$APK_PATH" > "$ROOT/work/triage/package-manifest.xml" 2>/dev/null || true
fi

pkg_name="UNKNOWN"
version_name="UNKNOWN"

if [ -f "$ROOT/work/triage/package-manifest.xml" ]; then
    pkg_line=$(grep "package: name=" "$ROOT/work/triage/package-manifest.xml" || true)
    if [ -n "$pkg_line" ]; then
        pkg_name=$(echo "$pkg_line" | sed -n "s/.*name='\([^']*\)'.*/\1/p")
        version_name=$(echo "$pkg_line" | sed -n "s/.*versionName='\([^']*\)'.*/\1/p")
    fi
fi

# Secret / Tracker scanning using apkradar or apkleaks
if [ -x "$VENV/bin/apkradar" ]; then
    "$VENV/bin/apkradar" audit "$APK_PATH" --offline --output "$ROOT/work/triage/secret-scan.json" 2>/dev/null || \
    "$VENV/bin/apkradar" audit "$APK_PATH" --output "$ROOT/work/triage/secret-scan.json" 2>/dev/null || \
    echo "{}" > "$ROOT/work/triage/secret-scan.json"
elif command -v apkleaks >/dev/null 2>&1; then
    apkleaks -f "$APK_PATH" -o "$ROOT/work/triage/secret-scan.json" >/dev/null 2>&1 || true
else
    echo "{}" > "$ROOT/work/triage/secret-scan.json"
fi

# APKID Packer / Obfuscator identification
if [ -x "$VENV/bin/apkid" ]; then
    "$VENV/bin/apkid" --json "$APK_PATH" > "$ROOT/work/triage/apkid.json" 2>/dev/null || true
fi

apk_dir=$(dirname "$APK_PATH")
apk_count=$(find "$apk_dir" -maxdepth 1 -name "*.apk" 2>/dev/null | wc -l | tr -d ' ')
if [ "$apk_count" -gt 1 ]; then
    split_status="split ($apk_count apks detected in directory)"
else
    split_status="single"
fi

file_list=$(unzip -Z1 "$APK_PATH" 2>/dev/null || true)

has_flutter=$(echo "$file_list" | grep -q "libflutter\.so" && echo "true" || echo "false")
has_hermes=$(echo "$file_list" | grep -q "libhermes\.so" && echo "true" || echo "false")
has_rn_bundle=$(echo "$file_list" | grep -q "assets/index\.android\.bundle" && echo "true" || echo "false")
has_kony=$(echo "$file_list" | grep -q -E 'libkonyjsvm\.so|assets/startup\.js|assets/application\.properties' && echo "true" || echo "false")
has_xamarin=$(echo "$file_list" | grep -q -E 'libmonosgen.*\.so|assemblies/' && echo "true" || echo "false")
has_cordova=$(echo "$file_list" | grep -q -E 'assets/www/cordova\.js' && echo "true" || echo "false")
dex_count=$(echo "$file_list" | grep -cE '^classes[0-9]*\.dex$' || echo "0")

framework_verdict="Standard JVM"
if [ "$has_kony" = "true" ]; then
    framework_verdict="Kony / Temenos"
elif [ "$has_flutter" = "true" ]; then
    framework_verdict="Flutter"
elif [ "$has_hermes" = "true" ] || [ "$has_rn_bundle" = "true" ]; then
    framework_verdict="React Native"
elif [ "$has_xamarin" = "true" ]; then
    framework_verdict="Xamarin"
elif [ "$has_cordova" = "true" ]; then
    framework_verdict="Cordova / PhoneGap"
fi

# Probe asset bundle encryption (e.g. startup.js.mp3 in Kony)
bundle_encryption="Not Applicable"
mp3_entry=$(echo "$file_list" | grep -E 'assets/.*\.(mp3|js|bundle)$' | head -n 1)
if [ -n "$mp3_entry" ]; then
    magic_hex=$(unzip -p "$APK_PATH" "$mp3_entry" 2>/dev/null | head -c 4 | xxd -p || true)
    if [ "$magic_hex" = "504b0304" ]; then
        bundle_encryption="Plaintext (ZIP compressed)"
    elif echo "$magic_hex" | grep -qE '^(2f|2a|20|[0-9a-f]{2})' && unzip -p "$APK_PATH" "$mp3_entry" 2>/dev/null | head -c 16 | grep -qaE '^[[:print:][:space:]]+$'; then
        bundle_encryption="Plaintext (ASCII source)"
    else
        bundle_encryption="Encrypted / Proprietary binary (magic: $magic_hex)"
    fi
fi

# Check Kony application.properties if present
kony_network_trust="None"
kony_use_crypto="None"
if echo "$file_list" | grep -q "assets/application.properties"; then
    kony_props=$(unzip -p "$APK_PATH" "assets/application.properties" 2>/dev/null || true)
    kony_network_trust=$(echo "$kony_props" | grep "^NetworkTrustConfig=" | cut -d'=' -f2 | tr -d '\r\n' || echo "None")
    kony_use_crypto=$(echo "$kony_props" | grep "^UseCryptoLibrary=" | cut -d'=' -f2 | tr -d '\r\n' || echo "None")
fi

http_stack_hint="UNKNOWN"
if echo "$file_list" | grep -qi "okhttp"; then
    http_stack_hint="OkHttp (heuristic string present)"
elif echo "$file_list" | grep -qi "cronet"; then
    http_stack_hint="Cronet (heuristic string present)"
fi

obfuscation_hint="Standard"
if [ "$dex_count" -gt 1 ]; then
    obfuscation_hint="MultiDex"
fi

native_libs=$(echo "$file_list" | grep -E '^lib/.*\.so$' | tr '\n' ',' | sed 's/,$//')

cat <<EOF > "$ROOT/work/triage/profile.json"
{
  "package": "$pkg_name",
  "versionName": "$version_name",
  "framework_verdict": "$framework_verdict",
  "bundle_encryption": "$bundle_encryption",
  "kony_network_trust": "$kony_network_trust",
  "kony_use_crypto": "$kony_use_crypto",
  "http_stack_hint": "$http_stack_hint",
  "obfuscation_hint": "$obfuscation_hint",
  "dex_count": $dex_count,
  "native_libs": "$native_libs",
  "split_status": "$split_status"
}
EOF

echo "Profile generated: $ROOT/work/triage/profile.json"
