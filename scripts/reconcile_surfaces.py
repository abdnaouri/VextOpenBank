#!/usr/bin/env python3
"""Reconcile static interface inventory against runtime captures.

Inputs:
  --static   interface_contract.json produced by discover_contract.py
  --dynamic  exchanges.jsonl produced by Frida transport_observe.js
  --out      surface_report.json (and sibling .md)

Classification:
  DYNAMIC  = route present in runtime capture but not in static contract
             (built via reflection or string concatenation — top priority)
  DEAD     = route present in static contract but never observed at runtime
             (feature-flagged or region-locked — low priority)
  ACTIVE   = matched in both, hit count recorded

Also detects multi-service chains: groups of requests arriving within
CHAIN_WINDOW_MS from >= CHAIN_MIN_HOSTS distinct hosts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CHAIN_WINDOW_MS = 2000
CHAIN_MIN_HOSTS = 2

# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

_VAR_SEG = re.compile(r"\{[^}]+\}|<[^>]+>|:[A-Za-z_][A-Za-z0-9_]*")
_HEX = re.compile(r"/[0-9a-fA-F]{8,}(?:/|$)")


def _norm_path(path: str) -> str:
    """Collapse template segments so static '{id}' and runtime '/42' match."""
    if not path:
        return "/"
    p = path.split("?", 1)[0].split("#", 1)[0]
    p = _VAR_SEG.sub("/{}", p)
    p = _HEX.sub("/{}/", p)
    p = re.sub(r"/{2,}", "/", p)
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p or "/"


def _host(url: str) -> str:
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def _route_key(verb: str, url: str) -> str:
    return f"{(verb or 'GET').upper()} {_norm_path(url)}"


# ---------------------------------------------------------------------------
# loaders
# ---------------------------------------------------------------------------

def _as_ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e12 else 1.0)
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value[:19], fmt).replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except ValueError:
                continue
    return None


def load_static(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        data = data.get("routes") or data.get("endpoints") or list(data.values())
    if not isinstance(data, list):
        raise SystemExit(f"unexpected static shape in {path}")
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        url = item.get("url_template") or item.get("url") or item.get("path") or ""
        verb = (item.get("verb") or item.get("method") or "GET").upper()
        out.append(
            {
                "key": _route_key(verb, url),
                "verb": verb,
                "url": url,
                "host": _host(url),
                "source": item.get("source_file", ""),
                "model": item.get("request_model", ""),
            }
        )
    return out


def load_dynamic(path: Path) -> list[dict[str, Any]]:
    out = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = rec.get("url") or rec.get("uri") or ""
            verb = (rec.get("verb") or rec.get("method") or "GET").upper()
            out.append(
                {
                    "key": _route_key(verb, url),
                    "verb": verb,
                    "url": url,
                    "host": _host(url),
                    "ts": _as_ts(rec.get("ts") or rec.get("timestamp")),
                    "resp_code": rec.get("resp_code"),
                }
            )
    return out


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def classify(
    static: list[dict[str, Any]], dynamic: list[dict[str, Any]]
) -> dict[str, Any]:
    static_map: dict[str, dict[str, Any]] = {}
    for r in static:
        static_map.setdefault(r["key"], r)

    dyn_hits: dict[str, int] = defaultdict(int)
    dyn_sample: dict[str, dict[str, Any]] = {}
    for r in dynamic:
        dyn_hits[r["key"]] += 1
        dyn_sample.setdefault(r["key"], r)

    static_only = sorted(set(static_map) - set(dyn_hits))
    runtime_only = sorted(set(dyn_hits) - set(static_map))
    matched = sorted(set(static_map) & set(dyn_hits))

    return {
        "static_only": [
            {"key": k, "verb": static_map[k]["verb"], "url": static_map[k]["url"],
             "host": static_map[k]["host"], "source": static_map[k]["source"]}
            for k in static_only
        ],
        "runtime_only": [
            {"key": k, "verb": dyn_sample[k]["verb"], "url": dyn_sample[k]["url"],
             "host": dyn_sample[k]["host"], "hits": dyn_hits[k],
             "sample_status": dyn_sample[k].get("resp_code")}
            for k in runtime_only
        ],
        "matched": [
            {"key": k, "verb": static_map[k]["verb"], "url": static_map[k]["url"],
             "host": static_map[k]["host"], "hits": dyn_hits[k]}
            for k in matched
        ],
    }


def detect_chains(
    dynamic: list[dict[str, Any]], window_ms: int = CHAIN_WINDOW_MS, min_hosts: int = CHAIN_MIN_HOSTS
) -> list[dict[str, Any]]:
    timed = sorted(
        (r for r in dynamic if r.get("ts") is not None and r.get("host")),
        key=lambda r: r["ts"],
    )
    chains: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for rec in timed:
        if not current:
            current = [rec]
            continue
        if rec["ts"] - current[0]["ts"] <= window_ms / 1000.0:
            current.append(rec)
        else:
            if len({r["host"] for r in current}) >= min_hosts:
                chains.append(current)
            current = [rec]
    if current and len({r["host"] for r in current}) >= min_hosts:
        chains.append(current)

    out = []
    for ch in chains:
        hosts = []
        for r in ch:
            if r["host"] not in hosts:
                hosts.append(r["host"])
        out.append(
            {
                "start_ts": ch[0]["ts"],
                "end_ts": ch[-1]["ts"],
                "duration_ms": int((ch[-1]["ts"] - ch[0]["ts"]) * 1000),
                "hosts": hosts,
                "steps": [
                    {"key": r["key"], "host": r["host"], "status": r.get("resp_code")}
                    for r in ch
                ],
            }
        )
    return out


def render_md(report: dict[str, Any]) -> str:
    lines = ["# Surface Reconciliation Report", ""]
    lines += [
        f"- Static-only (DEAD): {len(report['static_only'])}",
        f"- Runtime-only (DYNAMIC — top priority): {len(report['runtime_only'])}",
        f"- Matched (ACTIVE): {len(report['matched'])}",
        f"- Multi-service chains: {len(report['chains'])}",
        "",
    ]
    if report["runtime_only"]:
        lines += ["## Runtime-only (investigate)", ""]
        lines += ["| verb | url | host | hits | sample status |",
                  "|------|-----|------|------|---------------|"]
        for r in report["runtime_only"]:
            lines.append(
                f"| {r['verb']} | `{r['url']}` | {r['host']} | {r['hits']} | "
                f"{r.get('sample_status', '')} |"
            )
        lines.append("")
    if report["static_only"]:
        lines += ["## Static-only (dead / gated)", ""]
        lines += ["| verb | url | host | source |", "|------|-----|------|--------|"]
        for r in report["static_only"]:
            lines.append(
                f"| {r['verb']} | `{r['url']}` | {r['host']} | {r['source']} |"
            )
        lines.append("")
    if report["chains"]:
        lines += ["## Multi-service chains", ""]
        for i, ch in enumerate(report["chains"], 1):
            hosts = " -> ".join(ch["hosts"])
            lines.append(
                f"{i}. span {ch['duration_ms']}ms across {hosts} "
                f"({len(ch['steps'])} calls)"
            )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--static", required=True, type=Path)
    ap.add_argument("--dynamic", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument(
        "--chain-window-ms",
        type=int,
        default=CHAIN_WINDOW_MS,
        help="Window (ms) for multi-service chain detection (default 2000)",
    )
    ap.add_argument(
        "--chain-min-hosts",
        type=int,
        default=CHAIN_MIN_HOSTS,
        help="Minimum distinct hosts for a multi-service chain (default 2)",
    )
    args = ap.parse_args(argv)

    if not args.static.exists():
        print(f"missing static contract: {args.static}", file=sys.stderr)
        return 1
    if not args.dynamic.exists():
        print(f"missing runtime capture: {args.dynamic}", file=sys.stderr)
        return 1

    static = load_static(args.static)
    dynamic = load_dynamic(args.dynamic)

    report = classify(static, dynamic)
    report["chains"] = detect_chains(
        dynamic, window_ms=args.chain_window_ms, min_hosts=args.chain_min_hosts
    )
    report["meta"] = {
        "static_count": len(static),
        "dynamic_count": len(dynamic),
        "chain_window_ms": args.chain_window_ms,
        "chain_min_hosts": args.chain_min_hosts,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    md_path = args.out.with_suffix(".md")
    md_path.write_text(render_md(report))

    print(f"wrote {args.out}")
    print(f"wrote {md_path}")
    print(
        f"dynamic={len(report['runtime_only'])} "
        f"dead={len(report['static_only'])} "
        f"active={len(report['matched'])} "
        f"chains={len(report['chains'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
