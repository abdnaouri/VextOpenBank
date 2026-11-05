#!/usr/bin/env python3
"""
Entrypoint to run VextOpenBank FastAPI server.
"""

import sys
from pathlib import Path
import os
import uvicorn

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"[*] Starting VextOpenBank API on 0.0.0.0:{port}")
    uvicorn.run("vext_open_bank.main:app", host="0.0.0.0", port=port, reload=False)
