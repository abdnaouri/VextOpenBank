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
from vext_open_bank.api.routes_soumit import router as soumit_router
from vext_open_bank.api.routes_invoices import router as invoices_router
from vext_open_bank.api.routes_aliases import router as aliases_router
from vext_open_bank.api.routes_security import router as security_router
from vext_open_bank.api.routes_transfers import router as transfers_router
from vext_open_bank.api.routes_comms import router as comms_router
from vext_open_bank.api.routes_agency import router as agency_router
from vext_open_bank.routes_treasury import build_treasury_router
from vext_open_bank.routes_public import build_public_renderer_router
from vext_open_bank.core.db import Database
from vext_open_bank.core.rib import validate_moroccan_rib
from vext_open_bank.core.outbox import OutboxWorker
from vext_open_bank.db.session import get_database, DATABASE_PATH
from vext_open_bank.services.airlock import airlock_keepalive_worker
from vext_open_bank.services.envoisms_bridge import envoisms_bridge
from vext_open_bank.workers.outbox_worker import run_outbox_worker

logger = logging.getLogger("VextOpenBank")

# Immutable Treasury Constants
CFG_RIB_RAW = os.getenv("VEXT_TREASURY_RIB", "050 810 0060118339712001 89")
CFG_RIB_CLEAN = validate_moroccan_rib(CFG_RIB_RAW)
BENEFICIARY_NAME = os.getenv("VEXT_BENEFICIARY_NAME", "VEXT AI")
CLIENT_CODE = "183397"
USER_ID = "V0183397"

# Unified Zero-Maintenance Outbox Worker
default_webhook_secret = os.getenv("VEXT_WEBHOOK_SECRET", "vext_unified_secret_key_2026")
unified_worker = OutboxWorker(
    db=get_database(),
    tenant_secrets={
        "envoisms": os.getenv("ENVOISMS_VEXT_SECRET", os.getenv("ENVOISMS_SECRET", default_webhook_secret)),
        "soumit": os.getenv("SOUMIT_VEXT_SECRET", default_webhook_secret),
        "charikat": os.getenv("CHARIKAT_VEXT_SECRET", default_webhook_secret),
    },
    default_secret=default_webhook_secret,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize SQLite database schema
    try:
        from vext_open_bank.db.session import init_db
        init_db()
    except Exception as exc:
        logger.warning("DB init in lifespan warning: %s", exc)

    # Start background Airlock session persistence worker
    keepalive_task = asyncio.create_task(airlock_keepalive_worker())
    logger.info("Airlock IAM session keep-alive background worker started.")

    # Start autonomous EnvoiSMS bank reconciliation & anti-throttling worker
    envoisms_task = asyncio.create_task(envoisms_bridge.run_sync_worker())
    logger.info("EnvoiSMS autonomous bank reconciliation background worker started.")

    # Start autonomous transactional outbox webhook dispatcher worker (legacy & unified)
    outbox_task = asyncio.create_task(run_outbox_worker(interval_seconds=15))
    unified_outbox_task = asyncio.create_task(unified_worker.run_forever())
    logger.info("Transactional outbox webhook background workers started.")

    try:
        yield
    finally:
        unified_worker.stop()
        keepalive_task.cancel()
        envoisms_task.cancel()
        outbox_task.cancel()
        try:
            await asyncio.gather(keepalive_task, envoisms_task, outbox_task, unified_outbox_task, return_exceptions=True)
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

import secrets
import time
import hashlib
import hmac
from vext_open_bank.core.exchange_logger import exchange_logger

# Cryptographic Origin Shield & Internal Key sourced dynamically from OS environment
# If unconfigured, generates secure ephemeral runtime secrets to prevent unauthorized access
ORIGIN_SHIELD_SECRET = os.environ.get("ORIGIN_SHIELD_SECRET") or secrets.token_urlsafe(32)
INTERNAL_API_KEY = (
    os.environ.get("VEXT_INTERNAL_API_KEY")
    or os.environ.get("INTERNAL_API_KEY")
    or secrets.token_urlsafe(32)
)


@app.middleware("http")
async def enforce_internal_security_and_telemetry(request, call_next):
    start_time = time.time()
    shield_header = request.headers.get("x-origin-shield") or request.headers.get("X-Vext-Edge-Secret", "")
    internal_key = request.headers.get("x-vext-key") or request.headers.get("authorization", "")

    # Allow public instructions, payment sheets, and avis receipts without exposing backend internals
    is_public_endpoint = (
        request.url.path == f"{settings.API_PREFIX}/envoisms/instructions"
        or request.url.path == f"{settings.API_PREFIX}/soumit/instructions"
        or request.url.path == f"{settings.API_PREFIX}/invoices/instructions"
        or request.url.path.startswith("/p/")
    )

    # Check tenant API keys
    token_candidate = None
    if internal_key and internal_key.startswith("Bearer "):
        token_candidate = internal_key[7:].strip()
    elif internal_key:
        token_candidate = internal_key.strip()

    is_tenant_key = False
    if token_candidate:
        cand_hash = hashlib.sha256(token_candidate.encode()).hexdigest()
        from vext_open_bank.core.tenants import SEED_TENANTS
        is_tenant_key = any(t.api_key_hash == cand_hash for t in SEED_TENANTS.values())

    # Constant-time comparison for token and origin shield
    origin_matches = bool(ORIGIN_SHIELD_SECRET) and bool(shield_header) and hmac.compare_digest(shield_header, ORIGIN_SHIELD_SECRET)
    internal_matches = False
    if bool(INTERNAL_API_KEY) and bool(token_candidate):
        internal_matches = hmac.compare_digest(token_candidate, INTERNAL_API_KEY)

    is_trusted = (
        is_public_endpoint
        or origin_matches
        or internal_matches
        or is_tenant_key
    )

    # Safely inspect request body if JSON or form
    req_body_data = None
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            raw_body = await request.body()
            if raw_body:
                import json
                req_body_data = json.loads(raw_body.decode("utf-8", errors="replace"))
        elif "application/x-www-form-urlencoded" in content_type:
            raw_body = await request.body()
            if raw_body and len(raw_body) < 10000:
                from urllib.parse import parse_qs
                req_body_data = {
                    k: v[0] if len(v) == 1 else v
                    for k, v in parse_qs(raw_body.decode("utf-8", errors="replace")).items()
                }
    except Exception:
        req_body_data = None

    response = None
    if not is_trusted:
        # Camouflage decoy: do not reveal any security, origin, or gateway cues
        accept_header = request.headers.get("accept", "")
        if request.url.path == "/" and "text/html" in accept_header:
            from fastapi.responses import HTMLResponse
            response = HTMLResponse(
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
            response = JSONResponse(
                status_code=200,
                content={"status": "idle", "module": "static-asset-dispatcher", "workers": 0}
            )
        else:
            from fastapi.responses import JSONResponse
            response = JSONResponse(
                status_code=404,
                content={"detail": "Not Found"}
            )
    else:
        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = (time.time() - start_time) * 1000.0
            try:
                exchange_logger.log_exchange(
                    exchange_type="INCOMING_HTTP_REQUEST",
                    target=request.url.path,
                    direction="INBOUND",
                    status_code=500,
                    request_data={
                        "query": dict(request.query_params),
                        "client": request.client.host if request.client else "unknown",
                        "body": req_body_data,
                    },
                    response_data={"error": str(exc)},
                    headers=dict(request.headers),
                    duration_ms=duration_ms,
                    metadata={"trusted": is_trusted, "method": request.method, "exception": str(exc)},
                )
            except Exception:
                pass
            raise

    # Safely extract response body for JSON payloads so discovery engine can inspect response fields
    resp_body_data = None
    if response and hasattr(response, "body_iterator"):
        try:
            resp_content_type = response.headers.get("content-type", "")
            if "application/json" in resp_content_type:
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                body_bytes = b"".join(chunks)
                try:
                    import json
                    resp_body_data = json.loads(body_bytes.decode("utf-8", errors="replace"))
                except Exception:
                    resp_body_data = {"raw_text": body_bytes[:500].decode("utf-8", errors="replace")}

                from starlette.responses import Response
                response = Response(
                    content=body_bytes,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                    background=response.background,
                )
        except Exception as exc:
            logger.debug("Failed extracting response body for telemetry: %s", exc)

    duration_ms = (time.time() - start_time) * 1000.0

    # Structured telemetry & exchange logging for all incoming connections
    try:
        req_headers = dict(request.headers)
        query_params = dict(request.query_params)
        req_payload = {
            "query": query_params,
            "client": request.client.host if request.client else "unknown",
        }
        if req_body_data is not None:
            req_payload["body"] = req_body_data

        res_payload = (
            resp_body_data
            if resp_body_data is not None
            else {"status_code": response.status_code if response else 500}
        )

        exchange_logger.log_exchange(
            exchange_type="INCOMING_HTTP_REQUEST",
            target=request.url.path,
            direction="INBOUND",
            status_code=response.status_code if response else 500,
            request_data=req_payload,
            response_data=res_payload,
            headers=req_headers,
            duration_ms=duration_ms,
            metadata={"trusted": is_trusted, "method": request.method},
        )
    except Exception as exc:
        logger.debug("Incoming HTTP exchange log failed: %s", exc)

    return response

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
app.include_router(soumit_router, prefix=settings.API_PREFIX)
app.include_router(invoices_router, prefix=settings.API_PREFIX)
app.include_router(aliases_router, prefix=settings.API_PREFIX)
app.include_router(security_router, prefix=settings.API_PREFIX)
app.include_router(transfers_router, prefix=settings.API_PREFIX)
app.include_router(comms_router, prefix=settings.API_PREFIX)
app.include_router(agency_router, prefix=settings.API_PREFIX)

# Corporate Treasury & Relevé Bancaire Engine
treasury_router = build_treasury_router(
    get_database(),
    rib_formatted=CFG_RIB_RAW,
    rib_clean=CFG_RIB_CLEAN,
    beneficiary=BENEFICIARY_NAME,
    client_code=CLIENT_CODE,
    user_id=USER_ID,
)
app.include_router(treasury_router, prefix=settings.API_PREFIX)

# Public French/Moroccan Corporate Payment & Receipt Portal (/p/inv/{motif} & /p/avis/{bank_tx_id})
public_router = build_public_renderer_router(
    get_database(),
    rib_formatted=CFG_RIB_RAW,
    rib_clean=CFG_RIB_CLEAN,
    beneficiary=BENEFICIARY_NAME,
    client_code=CLIENT_CODE,
    user_id=USER_ID,
)
app.include_router(public_router)



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
