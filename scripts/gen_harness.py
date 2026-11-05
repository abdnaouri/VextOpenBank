#!/usr/bin/env python3
import argparse
import json
import os
import sys

def sanitize_ident(name):
    clean = "".join([c if c.isalnum() else "_" for c in name])
    if clean and clean[0].isdigit():
        clean = "_" + clean
    return clean or "DefaultEndpoint"

def generate_kotlin(contract_path, out_path):
    routes = []
    if os.path.exists(contract_path):
        with open(contract_path, "r") as f:
            try:
                routes = json.load(f)
            except Exception:
                routes = []

    lines = [
        "package repro.client",
        "",
        "import retrofit2.Call",
        "import retrofit2.http.*",
        "",
        "interface GeneratedApiClient {"
    ]

    seen = {}

    for idx, r in enumerate(routes):
        verb = r.get("verb", "GET").upper()
        template = r.get("url_template", f"/api/endpoint_{idx}")
        safe_template = template.replace('"', '\\"')

        base = sanitize_ident(template.strip("/").replace("/", "_"))
        if base in seen:
            seen[base] += 1
            method_name = f"{base}_{seen[base]}"
        else:
            seen[base] = 1
            method_name = base

        lines.append(f'    @{verb}("{safe_template}")')
        lines.append(f"    fun {method_name}(): Call<Map<String, Any>>")
        lines.append("")

    lines.append("}")
    lines.append("")

    with open(out_path, "w") as f:
        f.write("\n".join(lines))

def generate_replay(exchanges_path, out_path):
    script_content = f'''#!/usr/bin/env python3
import argparse
import json
import os
import sys
import requests

def main():
    parser = argparse.ArgumentParser(description="Replay recorded HTTP exchange.")
    parser.add_argument("--index", type=int, default=0, help="Index of exchange in log")
    parser.add_argument("--url", type=str, default=None, help="Filter exchange by URL substring")
    parser.add_argument("--log", type=str, default="{exchanges_path}", help="Path to exchanges.jsonl")
    parser.add_argument("--strip-auth", action="store_true", help="Strip Authorization headers for Phase 9 auth class test")
    args = parser.parse_args()

    if not os.path.exists(args.log):
        print(f"Log file not found: {{args.log}}")
        sys.exit(1)

    exchanges = []
    with open(args.log, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    exchanges.append(json.loads(line))
                except Exception:
                    pass

    if not exchanges:
        print("No valid exchanges loaded.")
        sys.exit(1)

    selected = None
    if args.url:
        for ex in exchanges:
            if args.url in ex.get("url", ""):
                selected = ex
                break
    else:
        if 0 <= args.index < len(exchanges):
            selected = exchanges[args.index]

    if not selected:
        print("No matching exchange found.")
        sys.exit(1)

    url = selected.get("url")
    verb = selected.get("verb", "GET").upper()
    headers = dict(selected.get("req_headers", {{}}))
    body = selected.get("req_body", None)

    if args.strip_auth:
        headers = {{k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-auth-token", "x-api-key")}}
        print("[replay] Authorization headers stripped for Phase 9 privilege/auth test.")

    print(f"Executing {{verb}} {{url}}...")
    resp = requests.request(verb, url, headers=headers, data=body)
    print(f"Response Status: {{resp.status_code}}")
    print("Response Body:")
    print(resp.text)

if __name__ == "__main__":
    main()
'''
    with open(out_path, "w") as f:
        f.write(script_content)
    os.chmod(out_path, 0o755)

def main():
    contract_file = "./work/contract/interface_contract.json"
    exchanges_file = "./work/logs/exchanges.jsonl"
    os.makedirs("./work/repro", exist_ok=True)

    generate_kotlin(contract_file, "./work/repro/client_stubs.kt")
    generate_replay(exchanges_file, "./work/repro/replay.py")

    with open("./work/repro/README.md", "w") as f:
        f.write("# Replay & Client Stubs Harness\n\n")
        f.write("1. Inspect `client_stubs.kt` for Retrofit interfaces.\n")
        f.write("2. Ensure network access to the target host.\n")
        f.write("3. Run `./replay.py --index 0` or `./replay.py --url <substring>`.\n")
        f.write("4. Use `./replay.py --url <substring> --strip-auth` for Phase 9 authorization tests.\n")

    print("Harness generation completed.")

if __name__ == "__main__":
    main()
