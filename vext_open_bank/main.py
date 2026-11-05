"""
VextOpenBank API - Autonomous Banking Interface for CFG Bank
"""

import sys
from pathlib import Path

# Ensure project root is in sys.path for cloud hosts
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from vext_open_bank.core.config import settings
from vext_open_bank.api.routes_auth import router as auth_router
from vext_open_bank.api.routes_accounts import router as accounts_router
from vext_open_bank.api.routes_cards import router as cards_router
from vext_open_bank.api.routes_pfm import router as pfm_router
from vext_open_bank.api.routes_profile import router as profile_router
from vext_open_bank.api.routes_catalog import router as catalog_router
from vext_open_bank.api.routes_automation import router as automation_router
from vext_open_bank.api.routes_features import router as features_router
from vext_open_bank.api.routes_envoisms import router as envoisms_router
from vext_open_bank.api.routes_security import router as security_router
from vext_open_bank.api.routes_transfers import router as transfers_router
from vext_open_bank.api.routes_comms import router as comms_router
from vext_open_bank.api.routes_agency import router as agency_router
from vext_open_bank.services.airlock import airlock_keepalive_worker
from vext_open_bank.services.envoisms_bridge import envoisms_bridge

logger = logging.getLogger("VextOpenBank")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background Airlock session persistence worker
    keepalive_task = asyncio.create_task(airlock_keepalive_worker())
    logger.info("Airlock IAM session keep-alive background worker started.")

    # Start autonomous EnvoiSMS bank reconciliation & anti-throttling worker
    envoisms_task = asyncio.create_task(envoisms_bridge.run_sync_worker())
    logger.info("EnvoiSMS autonomous bank reconciliation background worker started.")

    try:
        yield
    finally:
        keepalive_task.cancel()
        envoisms_task.cancel()
        try:
            await asyncio.gather(keepalive_task, envoisms_task, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        logger.info("Background workers gracefully stopped.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="""
# VextOpenBank — Programmatic Open Banking Gateway for CFG Bank

Autonomous API bridge and service suite replicating and extending CFG Bank operations.

### Key Capabilities:
* **Accounts & Balances**: Ledgers, formatted Moroccan RIBs, and Modulo 97 validation.
* **Cards Management**: Live card limits, statuses, and remote lock/unlock.
* **Personal Finance Management**: Spending taxonomy and 30-day liquidity charts.
* **Customer Profile**: Relationship manager and managing branch directory.
* **Backend Catalog**: 42 discovered MobileFabric integration services and biller taxonomy.
* **Step-Up MFA & Security**: Token lifecycle, SMS/Entrust MFA elevation challenge-verify state machine.
* **Domestic Transfers & Beneficiaries**: Modulo-97 checked transfers and unpaid operations (impayés).
* **Communications & Dynamics CRM**: Secure bank inbox and CRM support ticketing.
* **Agency & Market Rates**: Branch/ATM locators, appointments, and live BAM FX rates.
* **Programmatic Automations**: Berlin Group NextGenPSD2 AIS/PIS schema, CSV bookkeeping, and balance monitors.
    """,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ORIGIN_SHIELD_SECRET = os.environ.get("ORIGIN_SHIELD_SECRET", "cf_shield_origin_auth_837291048576")
INTERNAL_API_KEY = os.environ.get("VEXT_INTERNAL_API_KEY", "vext_internal_live_key_9f8e7d6c5b4a")


@app.middleware("http")
async def enforce_internal_security(request, call_next):
    shield_header = request.headers.get("x-origin-shield")
    internal_key = request.headers.get("x-vext-key") or request.headers.get("authorization")

    # Allow public instructions without exposing backend internals
    is_public_endpoint = request.url.path == f"{settings.API_PREFIX}/envoisms/instructions"

    is_trusted = (
        is_public_endpoint
        or shield_header == ORIGIN_SHIELD_SECRET
        or internal_key == INTERNAL_API_KEY
        or internal_key == f"Bearer {INTERNAL_API_KEY}"
    )

    if not is_trusted:
        # Camouflage decoy: do not reveal any security, origin, or gateway cues
        accept_header = request.headers.get("accept", "")
        if request.url.path == "/" and "text/html" in accept_header:
            from fastapi.responses import HTMLResponse
            return HTMLResponse(
                status_code=200,
                content="""<!DOCTYPE html>
<html>
<head><title>Service Status</title></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display:flex; align-items:center; justify-content:center; height:100vh; margin:0; background:#fbfbfb; color:#555;">
  <div style="text-align:center; padding:40px; border:1px solid #eaeaea; border-radius:8px; background:#fff; max-width:400px; box-shadow:0 2px 8px rgba(0,0,0,0.04);">
    <h3 style="color:#222; margin-top:0;">Static Asset Dispatcher</h3>
    <p style="font-size:14px; line-height:1.5;">Worker process idle. No asynchronous tasks scheduled.</p>
  </div>
</body>
</html>"""
            )
        elif request.url.path == "/":
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=200,
                content={"status": "idle", "module": "static-asset-dispatcher", "workers": 0}
            )
        else:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=404,
                content={"detail": "Not Found"}
            )

    return await call_next(request)

# Register Sub-Routers
app.include_router(auth_router, prefix=settings.API_PREFIX)
app.include_router(accounts_router, prefix=settings.API_PREFIX)
app.include_router(cards_router, prefix=settings.API_PREFIX)
app.include_router(pfm_router, prefix=settings.API_PREFIX)
app.include_router(profile_router, prefix=settings.API_PREFIX)
app.include_router(catalog_router, prefix=settings.API_PREFIX)
app.include_router(automation_router, prefix=settings.API_PREFIX)
app.include_router(features_router, prefix=settings.API_PREFIX)
app.include_router(envoisms_router, prefix=settings.API_PREFIX)
app.include_router(security_router, prefix=settings.API_PREFIX)
app.include_router(transfers_router, prefix=settings.API_PREFIX)
app.include_router(comms_router, prefix=settings.API_PREFIX)
app.include_router(agency_router, prefix=settings.API_PREFIX)


@app.get("/app", response_class=HTMLResponse, tags=["Dashboard UI"])
def serve_dashboard():
    dashboard_path = os.path.join(os.path.dirname(__file__), "frontend", "dashboard.html")
    if os.path.exists(dashboard_path):
        with open(dashboard_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Dashboard UI Not Found</h1>"


@app.get("/", tags=["Root"])
def root_status():
    return {
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "dashboard_ui": "/app",
        "docs_url": "/docs",
        "health_check": f"{settings.API_PREFIX}/auth/status",
        "open_banking_endpoint": f"{settings.API_PREFIX}/automation/open-banking/account",
    }
