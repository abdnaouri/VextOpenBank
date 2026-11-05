#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys

def run_rg(pattern, directory):
    try:
        cmd = [
            "rg",
            "--no-heading",
            "--line-number",
            pattern,
            directory
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        return res.stdout.splitlines()
    except Exception:
        return []

def main():
    parser = argparse.ArgumentParser(description="Extract API route contracts from decompiled sources.")
    parser.add_argument("source_dir", nargs="?", default="./work/static/jadx", help="Path to decompiled source directory")
    parser.add_argument("--base-url", default="", help="Base URL to prefix discovered route templates")
    args = parser.parse_args()

    target_dir = args.source_dir
    base_url = args.base_url.rstrip("/")

    os.makedirs("./work/contract", exist_ok=True)
    os.makedirs("./work/dynamic", exist_ok=True)

    contract_routes = []
    todo_list = []

    # 1. Retrofit annotations sweep
    annot_pattern = r'@(GET|POST|PUT|DELETE|PATCH)\s*\(\s*(?:value\s*=\s*|path\s*=\s*)?["\']([^"\']+)["\']'
    matches = run_rg(r'@(GET|POST|PUT|DELETE|PATCH)\s*\(', target_dir)

    for line in matches:
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file_path, line_no, content = parts[0], parts[1], parts[2]
        m = re.search(annot_pattern, content)
        if m:
            verb = m.group(1).upper()
            url_template = m.group(2)
            if base_url and not url_template.startswith("http"):
                full_url = f"{base_url}/{url_template.lstrip('/')}"
            else:
                full_url = url_template

            todo_list.append({
                "route": full_url,
                "field": "headers",
                "reason": "Verify runtime headers attached by interceptors"
            })

            contract_routes.append({
                "url_template": full_url,
                "verb": verb,
                "headers_known": [],
                "headers_unknown": ["Authorization"],
                "request_model": "UNKNOWN",
                "response_model": "UNKNOWN",
                "auth_required": "UNKNOWN",
                "integrity_scheme": "UNKNOWN",
                "source_file": f"{file_path}:{line_no}"
            })

    # 2. Smali fallback if Java source search returned few results
    smali_dir = "./work/static/apktool/smali"
    if os.path.isdir(smali_dir) and len(contract_routes) < 5:
        smali_matches = run_rg(r'const-string[^,]+,\s*"(/api/[^"]+|/v[0-9]/[^"]+|/mobile/[^"]+)"', smali_dir)
        for line in smali_matches:
            parts = line.split(":", 2)
            if len(parts) >= 3:
                m = re.search(r'"([^"]+)"', parts[2])
                if m:
                    route = m.group(1)
                    if not any(r["url_template"] == route for r in contract_routes):
                        contract_routes.append({
                            "url_template": f"{base_url}/{route.lstrip('/')}" if base_url else route,
                            "verb": "UNKNOWN",
                            "headers_known": [],
                            "headers_unknown": ["Authorization"],
                            "request_model": "UNKNOWN",
                            "response_model": "UNKNOWN",
                            "auth_required": "UNKNOWN",
                            "integrity_scheme": "UNKNOWN",
                            "source_file": f"{parts[0]}:{parts[1]}"
                        })

    # Check framework profile context
    profile_path = "./work/triage/profile.json"
    framework_note = ""
    if os.path.exists(profile_path):
        try:
            with open(profile_path) as pf:
                prof = json.load(pf)
                verdict = prof.get("framework_verdict", "")
                if "Kony" in verdict or "Flutter" in verdict or "React Native" in verdict:
                    framework_note = f"\n> [!NOTE]\n> Target identified as **{verdict}**. Business logic resides in non-JVM runtime/bundles. Static Java annotations will be sparse or absent; Phase 6 dynamic observation is the primary source of truth.\n\n"
        except Exception:
            pass

    with open("./work/contract/interface_contract.json", "w") as f:
        json.dump(contract_routes, f, indent=2)

    with open("./work/dynamic/todo.json", "w") as f:
        json.dump(todo_list, f, indent=2)

    with open("./work/contract/routes.md", "w") as f:
        f.write("# Discovered Interface Contract\n\n")
        if framework_note:
            f.write(framework_note)
        f.write("| Verb | Route Template | Auth Required | Integrity Scheme | Source |\n")
        f.write("|------|----------------|---------------|------------------|--------|\n")
        for r in contract_routes:
            f.write(f"| {r['verb']} | `{r['url_template']}` | {r['auth_required']} | {r['integrity_scheme']} | `{r['source_file']}` |\n")

    print(f"Contract discovery complete. Discovered {len(contract_routes)} routes.")

if __name__ == "__main__":
    main()
