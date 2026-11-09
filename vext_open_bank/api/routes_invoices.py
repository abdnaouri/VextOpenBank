import io
import base64
import uuid
from decimal import Decimal
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .deps import require_tenant, idem_guard
from ..core.money import to_centimes, from_centimes, extract_canonical_motif, mad_to_centimes, centimes_to_mad_str
from ..core.tenants import TenantConfig, resolve_tenant_by_prefix
from ..core.rib import format_rib, validate_moroccan_rib
from ..core.moroccan_banks import list_receiving_treasury_accounts, get_receiving_treasury_account
from ..core.qr import generate_qr_data_uri, build_emvco_payload
from ..db.session import get_connection
from ..services import invoice_service, reconciliation

router = APIRouter(prefix="/invoices", tags=["Universal Invoicing Engine"])


class UniversalCheckBody(BaseModel):
    # Support both invoice_id and motif for 1:1 cross-compatibility
    invoice_id: Optional[str] = Field(None, description="Unique client invoice or order ID, e.g. inv_123 or topup_01")
    tenant: Optional[str] = Field(None, description="Tenant identifier, e.g. envoisms, soumit, or charikat")
    amount_mad: Any = Field(..., description="Exact expected amount in MAD (e.g. 275.00 or '1500.00')")
    reference: Optional[str] = Field(None, description="Unique bank wire reference motif, e.g. ENV-98421 or SOU-1002")
    motif: Optional[str] = Field(None, description="Alias for reference or canonical motif")
    customer_name: Optional[str] = Field(None, description="Legal name of paying customer or entity")
    customer_ref: Optional[str] = Field(None, description="Customer reference")
    webhook_url: Optional[str] = Field(None, description="Callback webhook URL")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)


class UniversalSimulateBody(BaseModel):
    reference: Optional[str] = Field(None, description="Transfer motif/reference to simulate")
    motif: Optional[str] = Field(None, description="Raw wire label or exact Motif, e.g. 'ENV-12345'")
    amount_mad: Any = Field(..., description="Amount in MAD credited")
    sender_name: Optional[str] = "SIMULATED SENDER SARL"
    sender_rib: Optional[str] = "007780000000123456789012"
    bank_txn_id: Optional[str] = None
    bank_tx_id: Optional[str] = None


@router.get("/instructions")
def get_invoice_instructions(
    reference: Optional[str] = Query(None, min_length=3, max_length=64, description="Canonical bank transfer reference"),
    motif: Optional[str] = Query(None, min_length=3, max_length=64, description="Alias for reference"),
    amount_mad: Any = Query(..., description="Amount in MAD"),
    tenant: Optional[str] = Query(None, description="Tenant slug: envoisms, soumit, charikat"),
    client_name: Optional[str] = Query(None, description="Customer or corporate name"),
    preferred_bank_code: Optional[str] = Query(None, description="Optional bank code for direct intra-bank routing (051, 007, 021, 181)"),
):
    """
    Pure Renderer (Zero DB Mutation).
    Returns formatted Moroccan bank details (CFG, Attijariwafa, BCP, CIH) and instructions
    for customer bank transfer settlement, including high-performance EMVCo / MarocPay QR code.
    """
    effective_ref = reference or motif
    if not effective_ref:
        raise HTTPException(status_code=422, detail="Missing required 'reference' or 'motif' parameter.")

    tenant_cfg = resolve_tenant_by_prefix(tenant or effective_ref)
    acc = get_receiving_treasury_account(preferred_bank_code)

    # Convert amount safely
    centimes = to_centimes(amount_mad)
    amount_decimal = from_centimes(centimes)

    # High-performance cached EMVCo QR code generator
    qr_image_data_uri = generate_qr_data_uri(
        rib=acc["rib"],
        beneficiary=acc["beneficiary_name"],
        centimes=centimes,
        motif=effective_ref,
    )
    emvco_payload = build_emvco_payload(acc["rib"], acc["beneficiary_name"], centimes, effective_ref)
    legacy_qr_text = f"RIB:{acc['rib']};BENEFICIARY:{acc['beneficiary_name']};AMOUNT:{amount_mad};REF:{effective_ref}"

    return {
        "service": f"Charikat.ma Payments ({tenant_cfg.name})",
        "tenant": tenant_cfg.id,
        "tenant_name": tenant_cfg.name,
        "reference": effective_ref,
        "motif": effective_ref,
        "mandatory_motif": effective_ref,
        "amount_mad": float(amount_decimal),
        "amount_mad_display": f"{amount_decimal:,.2f} MAD",
        "amount_centimes": centimes,
        "beneficiary": acc["beneficiary_name"],
        "bank_name": acc["bank_name"],
        "bank_code": acc["bank_code"],
        "agency": acc["agency"],
        "client_code": "183397",
        "user_id": "V0183397",
        "rib": acc["rib"],
        "rib_clean": acc["rib"],
        "rib_formatted": acc["rib_formatted"],
        "swift_bic": acc["swift_bic"],
        "transfer_type": acc["transfer_type"],
        "notice": f"Veuillez indiquer impérativement la référence '{effective_ref}' dans le motif du virement.",
        "customer_name": client_name,
        "qr_code_payload": legacy_qr_text,
        "emvco_payload": emvco_payload,
        "qr_code_data_uri": qr_image_data_uri,
        "available_receiving_banks": [a["bank_name"] for a in list_receiving_treasury_accounts()],
    }


@router.post("/check")
def check_or_upsert_invoice(
    body: UniversalCheckBody,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    auth: TenantConfig = Depends(require_tenant),
):
    """
    Canonical Universal Invoice Check & Upsert.
    1. Validates Idempotency-Key with cross-request hashing.
    2. Upserts invoice record in BIGINT centimes.
    3. Resolves current payment status (PENDING_CONFIRMATION, CONFIRMED, ALREADY_RECONCILED, REJECTED_UNDERPAYMENT).
    """
    effective_ref = body.reference or body.motif
    if not effective_ref:
        raise HTTPException(status_code=422, detail="Missing required 'reference' or 'motif' in body.")

    effective_invoice_id = body.invoice_id or f"inv_{effective_ref}"
    effective_customer = body.customer_name or body.customer_ref or "default"
    amount_c = to_centimes(body.amount_mad)
    amount_dec = from_centimes(amount_c)

    conn = get_connection()
    try:
        def _exec():
            res = invoice_service.upsert_and_check(
                conn=conn,
                tenant=auth,
                client_invoice_id=effective_invoice_id,
                reference=effective_ref,
                amount_mad=amount_dec,
                customer_name=effective_customer,
            )
            # Enrich response with 5-year schema fields
            acc = get_receiving_treasury_account(None)
            res["expected_centimes"] = amount_c
            res["received_centimes"] = to_centimes(res.get("paid_mad", 0))
            res["rib"] = acc["rib_formatted"]
            res["beneficiary"] = acc["beneficiary_name"]
            res["motif"] = effective_ref
            res["qr_code_data_uri"] = generate_qr_data_uri(acc["rib"], acc["beneficiary_name"], amount_c, effective_ref)
            return res

        return idem_guard(
            tenant_id=auth.id,
            endpoint="/invoices/check",
            key=idempotency_key,
            body_dict=body.model_dump(mode="json"),
            execute_fn=_exec,
        )
    finally:
        conn.close()


@router.post("/simulate-transfer")
def simulate_incoming_transfer(
    body: UniversalSimulateBody,
    auth: TenantConfig = Depends(require_tenant),
):
    """
    Simulate an incoming bank transfer for automated tests and sandbox validations.
    Applies transfer directly through the reconciliation FSM and outbox queue.
    """
    effective_ref = body.reference or body.motif
    if not effective_ref:
        raise HTTPException(status_code=422, detail="Missing required 'reference' or 'motif' in body.")

    bank_id = body.bank_txn_id or body.bank_tx_id or f"sim_{uuid.uuid4().hex[:12]}"
    conn = get_connection()
    try:
        res = reconciliation.apply_transfer(
            conn=conn,
            tenant_id=auth.id,
            bank_txn_id=bank_id,
            amount_centimes=to_centimes(body.amount_mad),
            reference=effective_ref,
            sender_name=body.sender_name,
            sender_rib=body.sender_rib,
            tolerance_centimes=auth.underpayment_tolerance_centimes,
            raw={"simulated": True, "auth_tenant": auth.id, "motif": effective_ref},
        )
        # Add canonical motif for consistency
        parsed = extract_canonical_motif(effective_ref)
        if parsed:
            res["motif"] = parsed[0]
        else:
            res["motif"] = effective_ref
        res["bank_tx_id"] = bank_id
        return res
    finally:
        conn.close()


@router.get("/receiving-accounts")
def get_all_receiving_accounts():
    """
    List all active multi-bank receiving treasury accounts (CFG, Attijariwafa, BCP, CIH).
    Used by checkout frontends for Smart Treasury Routing.
    """
    return {
        "platform": "Charikat.ma Multi-Tenant Treasury",
        "total_treasury_accounts": len(list_receiving_treasury_accounts()),
        "accounts": list_receiving_treasury_accounts(),
    }
