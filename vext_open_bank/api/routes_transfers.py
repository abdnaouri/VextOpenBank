"""
routes_transfers.py
Beneficiaries, domestic transfers, and unpaid operations.
MobileFabric services: CFGManagePayees, CFGImpayees, CFGTransfersPaymentsOverview
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional

from vext_open_bank.services.mobilefabric import mf_client

router = APIRouter(prefix="/transfers", tags=["Beneficiaries & Domestic Transfers"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class PayeeCreateRequest(BaseModel):
    full_name: str = Field(..., description="Beneficiary full name")
    rib: str = Field(..., min_length=24, max_length=24, description="24-digit Moroccan RIB")
    bank_name: Optional[str] = None
    alias: Optional[str] = None


class TransferExecuteRequest(BaseModel):
    source_rib: str = Field(..., description="Debit account 24-digit RIB")
    beneficiary_rib: str = Field(..., description="Credit account 24-digit RIB")
    amount_mad: float = Field(..., gt=0, description="Amount in MAD")
    label: Optional[str] = Field("Virement", description="Transfer label / motif")
    mfa_confirmed: bool = Field(
        False,
        description="Must be True — caller must complete /security/mfa/verify first",
    )


# ---------------------------------------------------------------------------
# RIB Modulo-97 pre-flight helper
# ---------------------------------------------------------------------------

def _validate_rib_mod97(rib: str) -> bool:
    """Return True if the 24-digit Moroccan RIB passes the Modulo 97 checksum."""
    clean = rib.replace(" ", "").strip()
    if len(clean) != 24 or not clean.isdigit():
        return False
    if clean == "0" * 24:
        return False
    bank = int(clean[:3])
    branch = int(clean[3:6])
    account = int(clean[6:22])
    key = int(clean[22:24])
    remainder = (89 * bank + 15 * branch + 3 * (account % 97)) % 97
    expected = 97 - remainder
    if expected == 97:
        expected = 0
    return key == expected


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/payees", summary="List Registered Payees (CFGManagePayees)")
def list_payees() -> Dict[str, Any]:
    """
    Retrieve all registered external beneficiaries from the MobileFabric
    `/services/CFGManagePayees` integration service.
    """
    try:
        return mf_client.invoke_service("CFGManagePayees", elevated=True)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/payees", summary="Register a New Payee (CFGManagePayees + MFAOtp)")
def create_payee(req: PayeeCreateRequest) -> Dict[str, Any]:
    """
    Add a new external beneficiary.

    **Pre-flight validation:** The 24-digit RIB is checked with the Moroccan
    Modulo 97 checksum before submission to the gateway.

    **MFA required:** The caller must have completed `/api/v1/security/mfa/verify`
    before calling this endpoint (the elevated token is used automatically).
    """
    if not _validate_rib_mod97(req.rib):
        raise HTTPException(
            status_code=400,
            detail=(
                f"RIB '{req.rib}' failed the Moroccan Modulo 97 checksum. "
                "Verify the 24 digits and try again."
            ),
        )

    if not mf_client.elevated_token:
        raise HTTPException(
            status_code=403,
            detail="Step-up MFA required. Call POST /api/v1/security/mfa/challenge then /verify first.",
        )

    try:
        return mf_client.invoke_service(
            "CFGManagePayees",
            params={
                "operation": "add",
                "fullName": req.full_name,
                "rib": req.rib,
                "bankName": req.bank_name or "",
                "alias": req.alias or req.full_name,
            },
            elevated=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/execute", summary="Execute Domestic Transfer (CFGTransfersPaymentsOverview)")
def execute_transfer(req: TransferExecuteRequest) -> Dict[str, Any]:
    """
    Execute an immediate domestic or internal transfer.

    **Requires explicit MFA confirmation:** `mfa_confirmed` must be `true` in
    the request body, and the caller must have already completed the step-up
    flow at `/api/v1/security/mfa/verify`.

    Both source and beneficiary RIBs are validated with Modulo 97 before
    dispatch.
    """
    if not req.mfa_confirmed:
        raise HTTPException(
            status_code=403,
            detail="Explicit MFA confirmation required (set mfa_confirmed: true).",
        )

    if not mf_client.elevated_token:
        raise HTTPException(
            status_code=403,
            detail="Elevated MFA token not present. Complete /api/v1/security/mfa/verify first.",
        )

    for label, rib in [("source_rib", req.source_rib), ("beneficiary_rib", req.beneficiary_rib)]:
        if not _validate_rib_mod97(rib):
            raise HTTPException(
                status_code=400,
                detail=f"{label} '{rib}' failed the Modulo 97 checksum validation.",
            )

    try:
        return mf_client.invoke_service(
            "CFGTransfersPaymentsOverview",
            params={
                "sourceRIB": req.source_rib,
                "beneficiaryRIB": req.beneficiary_rib,
                "amount": str(req.amount_mad),
                "currency": "MAD",
                "label": req.label,
            },
            elevated=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/impayes", summary="List Unpaid Direct Debits & Bounced Cheques (CFGImpayees)")
def list_impayes() -> Dict[str, Any]:
    """
    Retrieve unpaid direct debits, bounced cheques, and settlement incidents
    from `/services/CFGImpayees`.  Critical for automated ERP reconciliation.
    """
    try:
        return mf_client.invoke_service("CFGImpayees", elevated=True)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
