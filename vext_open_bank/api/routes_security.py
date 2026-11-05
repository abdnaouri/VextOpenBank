"""
routes_security.py
Step-up MFA state machine covering 8 MobileFabric services:
  MFAOtp, CopyMFAOtp, EntrustService, AcccessTokenForMFA,
  CFGValidateToken, _internal_logout, AccessToken1, MicrosoftToken
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict

from vext_open_bank.services.mobilefabric import mf_client

router = APIRouter(prefix="/security", tags=["Step-Up MFA & Token Lifecycle"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class MFAChallengeRequest(BaseModel):
    channel: str = Field(
        "sms",
        description="Delivery channel for OTP: 'sms', 'email', or 'entrust'",
    )


class MFAVerifyRequest(BaseModel):
    otp_code: str = Field(..., min_length=4, max_length=10, description="OTP received by the user")


class ValidateTokenRequest(BaseModel):
    token: str = Field(..., description="JWT claims token to validate")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/mfa/challenge", summary="Trigger MFA OTP or Push Challenge")
def trigger_mfa_challenge(req: MFAChallengeRequest) -> Dict[str, Any]:
    """
    **Step 1 — MFA step-up initiation.**

    Calls `/services/MFAOtp` (with `CopyMFAOtp` as automatic fallback) to
    dispatch an SMS/email one-time-password or an Entrust push notification.

    Required before executing any mutating operation (transfers, payee addition,
    bill payment, dotation activation, e-signature).
    """
    try:
        result = mf_client.trigger_mfa_challenge(channel=req.channel)
        return {"status": "CHALLENGE_SENT", "channel": req.channel, "gateway_response": result}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MFA challenge failed: {exc}") from exc


@router.post("/mfa/verify", summary="Submit OTP & Exchange for Elevated Token")
def verify_mfa(req: MFAVerifyRequest) -> Dict[str, Any]:
    """
    **Step 2 — MFA step-up verification.**

    Submits the OTP to `/services/AcccessTokenForMFA`.  On success the
    short-lived elevated `claims_token` is stored in-process and injected
    automatically into subsequent mutating service calls via
    `mf_client.elevated_token`.

    Returns a **prefix** of the elevated token (never the full value) so the
    caller can confirm the exchange succeeded.
    """
    try:
        elevated = mf_client.verify_mfa_and_elevate(otp_code=req.otp_code)
        return {
            "status": "ELEVATED",
            "elevated_token_prefix": elevated[:40] + "...",
            "message": "Step-up complete. Mutating operations are now authorized.",
        }
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MFA verification failed: {exc}") from exc


@router.post("/token/validate", summary="Validate a Claims Token (CFGValidateToken)")
def validate_token(req: ValidateTokenRequest) -> Dict[str, Any]:
    """
    Call `/services/CFGValidateToken` to check whether a JWT claims token is
    still alive and has not been revoked by the gateway.
    """
    try:
        return mf_client.validate_token(req.token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/logout", summary="Invalidate Session (_internal_logout)")
def logout_session() -> Dict[str, Any]:
    """
    Explicitly invalidate the current MobileFabric session via
    `/services/_internal_logout`.  Clears both the standard and elevated
    in-memory tokens.
    """
    try:
        result = mf_client.logout()
        return {"status": "LOGGED_OUT", "gateway_response": result}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/token/microsoft", summary="Obtain Microsoft Token for Dynamics CRM")
def get_microsoft_token() -> Dict[str, Any]:
    """
    Exchange the current MobileFabric session for a MicrosoftToken scoped to
    Dynamics CRM — required by the Incidents / ticketing router.
    """
    try:
        return mf_client.get_microsoft_token()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/token/access1", summary="Obtain AccessToken1 (secondary auth flow)")
def get_access_token1() -> Dict[str, Any]:
    """
    Mint an AccessToken1 via the secondary MobileFabric authentication flow.
    Required for specific entrust-secured services.
    """
    try:
        return mf_client.get_access_token1()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
