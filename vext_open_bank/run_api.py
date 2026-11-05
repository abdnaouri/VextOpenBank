#!/usr/bin/env python3
"""
Entrypoint to run VextOpenBank FastAPI server.
"""

import uvicorn

if __name__ == "__main__":
    print("[*] Starting VextOpenBank API on http://127.0.0.1:8000")
    print("[*] Swagger API Documentation available at http://127.0.0.1:8000/docs")
    uvicorn.run("vext_open_bank.main:app", host="127.0.0.1", port=8000, reload=False)
