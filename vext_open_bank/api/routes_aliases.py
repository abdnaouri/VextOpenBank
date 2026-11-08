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
