#!/usr/bin/env bash
set -uo pipefail

if [ -f "$HOME/.zshrc" ]; then 
    source "$HOME/.zshrc" >/dev/null 2>&1 || true
fi

mkdir -p ./work/inbox \
         ./work/triage \
         ./work/static \
         ./work/contract/schemas \
         ./work/dynamic \
         ./work/logs \
         ./work/repro

tools=("adb" "aapt2" "apktool" "jadx" "apkleaks" "frida" "frida-ps" "python3" "jq" "rg")
missing_tools=()

echo "=== Toolchain Verification ==="

for tool in "${tools[@]}"; do
    if command -v "$tool" >/dev/null 2>&1; then
        case "$tool" in
            python3)
                ver=$(python3 --version 2>&1)
                ;;
            jq|rg|apktool|jadx|frida|frida-ps)
                ver=$("$tool" --version 2>&1 | head -n 1)
                ;;
            adb)
                ver=$(adb version 2>&1 | head -n 1)
                ;;
            aapt2)
                ver=$(aapt2 version 2>&1 | head -n 1)
                ;;
            apkleaks)
                ver="present"
                ;;
            *)
                ver="installed"
                ;;
        esac
        printf "%-12s : %s\n" "$tool" "$ver"
    else
        printf "%-12s : NOT FOUND\n" "$tool"
        missing_tools+=("$tool")
    fi
done

echo ""
if [ ${#missing_tools[@]} -gt 0 ]; then
    echo "=== Missing Dependencies Instructions ==="
    for missing in "${missing_tools[@]}"; do
        case "$missing" in
            adb|aapt2)
                echo "[$missing] macOS: brew install --cask android-platform-tools | Linux: apt-get install -y android-tools-adb aapt"
                ;;
            apktool)
                echo "[$missing] macOS: brew install apktool | Linux: apt-get install -y apktool"
                ;;
            jadx)
                echo "[$missing] macOS: brew install jadx | Linux: apt-get install -y jadx"
                ;;
            apkleaks)
                echo "[$missing] macOS/Linux: pip3 install apkleaks"
                ;;
            frida|frida-ps)
                echo "[$missing] macOS/Linux: pip3 install frida-tools"
                ;;
            python3)
                echo "[$missing] macOS: brew install python3 | Linux: apt-get install -y python3"
                ;;
            jq)
                echo "[$missing] macOS: brew install jq | Linux: apt-get install -y jq"
                ;;
            rg)
                echo "[$missing] macOS: brew install ripgrep | Linux: apt-get install -y ripgrep"
                ;;
        esac
    done
else
    echo "All dependencies verified."
fi
