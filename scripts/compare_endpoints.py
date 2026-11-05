#!/usr/bin/env python3
"""Compatibility wrapper pointing to reconcile_surfaces.py."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import reconcile_surfaces

if __name__ == "__main__":
    # If called with legacy --runtime flag, translate to --dynamic
    args = []
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--runtime":
            args.append("--dynamic")
        else:
            args.append(arg)
        i += 1
    sys.exit(reconcile_surfaces.main(args))
