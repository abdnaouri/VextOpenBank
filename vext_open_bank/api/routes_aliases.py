"""
routes_aliases.py
Backward-compatible aliases for legacy EnvoiSMS and Soumit endpoints delegating
to the Unified Invoicing Engine or maintaining seamless backward routing.
"""

from decimal import Decimal
from typing import Optional, Dict, Any
from fastapi import APIRouter, Header, HTTPException, Query, Depends
from pydantic import BaseModel, Field

from .deps import require_tenant
from .routes_invoices import (
    router as invoices_router,
    UniversalCheckBody,
    UniversalSimulateBody,
    get_invoice_instructions,
    check_or_upsert_invoice,
    simulate_incoming_transfer as simulate_transfer_endpoint,
)
from ..core.tenants import SEED_TENANTS, TenantConfig

router = APIRouter(tags=["Legacy Invoicing Aliases"])

# Models matching legacy schemas for check endpoints
class LegacyEnvoismsCheckRequest(BaseModel):
    topup_id: str = Field(..., description="Unique EnvoiSMS topup ID")
    account_id: str = Field(..., description="EnvoiSMS customer account ID")
    amount_mad: float = Field(..., gt=0)
    reference: str = Field(..., description="Transfer motif, e.g. ENV-98421")
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None

class LegacySoumitCheckRequest(BaseModel):
    order_id: str = Field(..., description="Unique Soumit order ID")
    account_id: str = Field(..., description="Soumit user account ID")
    amount_mad: float = Field(..., gt=0)
    reference: str = Field(..., description="Transfer motif, e.g. BC-2026-1001")
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None

class LegacySimulateRequest(BaseModel):
    amount_mad: float = Field(..., gt=0)
    sender_name: str
    motif: str
    sender_rib: Optional[str] = "007780000000123456789012"
    bank_tx_id: Optional[str] = None


class LegacyEnvoiSmsTopupRequest(BaseModel):
    order_id: str = Field(..., examples=["12345"])
    amount_dh: Any = Field(..., examples=["2500.00"])
    client_id: str = Field(default="envoisms_client")
    callback_url: str


class LegacySoumitSubscriptionRequest(BaseModel):
    reference: str = Field(..., examples=["SOU-9981", "BC-4410"])
    montant_ttc: Any = Field(..., examples=["4800.00"])
    entreprise_id: str = Field(default="soumit_org")
    webhook_endpoint: str


@router.post("/envoisms/topup", tags=["Legacy Invoicing Aliases"])
def legacy_envoisms_bridge(
    req: LegacyEnvoiSmsTopupRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    auth: TenantConfig = Depends(require_tenant),
):
    raw_id = req.order_id.upper()
    motif = raw_id if raw_id.startswith("ENV-") else f"ENV-{raw_id}"
    unified_req = UniversalCheckBody(
        invoice_id=req.order_id,
        motif=motif,
        reference=motif,
        amount_mad=req.amount_dh,
        customer_ref=req.client_id,
        webhook_url=req.callback_url,
        metadata={"legacy_bridge": "envoisms"},
    )
    return check_or_upsert_invoice(unified_req, idempotency_key=idempotency_key, auth=auth)


@router.post("/soumit/subscription", tags=["Legacy Invoicing Aliases"])
def legacy_soumit_bridge(
    req: LegacySoumitSubscriptionRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    auth: TenantConfig = Depends(require_tenant),
):
    raw_ref = req.reference.upper()
    motif = raw_ref if raw_ref.startswith(("SOU-", "BC-")) else f"SOU-{raw_ref}"
    unified_req = UniversalCheckBody(
        invoice_id=req.reference,
        motif=motif,
        reference=motif,
        amount_mad=req.montant_ttc,
        customer_ref=req.entreprise_id,
        webhook_url=req.webhook_endpoint,
        metadata={"legacy_bridge": "soumit"},
    )
    return check_or_upsert_invoice(unified_req, idempotency_key=idempotency_key, auth=auth)


@router.get("/v2/invoices/instructions", tags=["Universal Invoicing Engine Aliases"])
def get_v2_invoice_instructions(
    reference: str = Query(...),
    amount_mad: Decimal = Query(...),
    tenant: Optional[str] = None,
    client_name: Optional[str] = None,
    preferred_bank_code: Optional[str] = None,
):
    """v2 alias forwarding directly to canonical invoices instructions."""
    return get_invoice_instructions(
        reference=reference,
        amount_mad=amount_mad,
        tenant=tenant,
        client_name=client_name,
        preferred_bank_code=preferred_bank_code,
    )
