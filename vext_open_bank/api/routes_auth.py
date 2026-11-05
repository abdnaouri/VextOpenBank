"""
routes_auth.py
Authentication routes for Token Server, MobileFabric, and Web Portal.
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, Any
from vext_open_bank.services.onegini import onegini_client
from vext_open_bank.services.mobilefabric import mf_client
from vext_open_bank.services.airlock import airlock_web

router = APIRouter(prefix="/auth", tags=["Authentication & Gateways"])


@router.get("/status")
def get_auth_status() -> Dict[str, Any]:
    """Check connectivity to all three CFG Bank gateways."""
    token_server_ok = False
    challenge = None
    try:
        c_data = onegini_client.get_challenge()
        challenge = c_data.get("challenge")
        token_server_ok = True
    except Exception:
        pass

    mf_ok = False
    try:
        token = mf_client.login_claims()
        mf_ok = bool(token)
    except Exception:
        pass

    portal_status = {}
    try:
        portal_status = airlock_web.check_portal_status()
    except Exception:
        pass

    return {
        "token_server": {
            "status": "connected" if token_server_ok else "unreachable",
            "active_challenge": challenge,
        },
        "mobilefabric_gateway": {
            "status": "connected" if mf_ok else "unreachable",
            "has_claims_token": bool(mf_client.claims_token),
        },
        "airlock_web_portal": {
            "status": "connected" if portal_status.get("airlock_iam_ready") else "unreachable",
            "details": portal_status,
        },
    }


@router.post("/mobilefabric/refresh-claims")
def refresh_mobilefabric_claims() -> Dict[str, Any]:
    """Force refresh the MobileFabric JWT claims authorization token."""
    try:
        token = mf_client.login_claims()
        return {
            "status": "success",
            "claims_token_prefix": token[:50] + "...",
            "jwt_length": len(token),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
