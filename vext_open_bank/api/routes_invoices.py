import io
import base64
import uuid
from decimal import Decimal
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

try:
    import qrcode
except ImportError:
    qrcode = None

from .deps import require_tenant, idem_guard
from ..core.money import to_centimes, from_centimes
from ..core.tenants import TenantConfig, resolve_tenant_by_prefix
from ..core.rib import format_rib
from ..core.moroccan_banks import list_receiving_treasury_accounts, get_receiving_treasury_account
from ..db.session import get_connection
from ..services import invoice_service, reconciliation

router = APIRouter(prefix="/invoices", tags=["Universal Invoicing Engine"])

class UniversalCheckBody(BaseModel):
    invoice_id: str = Field(..., description="Unique client invoice or order ID, e.g. inv_123 or topup_01")
    tenant: str = Field(..., description="Tenant identifier, e.g. envoisms, soumit, or charikat")
    amount_mad: Decimal = Field(..., gt=0, description="Exact expected amount in MAD (e.g. 275.00 or 14256.00)")
    reference: str = Field(..., description="Unique bank wire reference motif, e.g. ENV-98421 or SOU-1002")
    customer_name: Optional[str] = Field(None, description="Legal name of paying customer or entity")

class UniversalSimulateBody(BaseModel):
    reference: str = Field(..., description="Transfer motif/reference to simulate")
    amount_mad: Decimal = Field(..., gt=0, description="Amount in MAD credited")
    sender_name: Optional[str] = "SIMULATED SENDER SARL"
    sender_rib: Optional[str] = "007780000000123456789012"
    bank_txn_id: Optional[str] = None

@router.get("/instructions")
def get_invoice_instructions(
    reference: str = Query(..., min_length=3, max_length=64, description="Canonical bank transfer reference"),
    amount_mad: Decimal = Query(..., gt=0, description="Amount in MAD"),
    tenant: Optional[str] = Query(None, description="Tenant slug: envoisms, soumit, charikat"),
    client_name: Optional[str] = Query(None, description="Customer or corporate name"),
    preferred_bank_code: Optional[str] = Query(None, description="Optional bank code for direct intra-bank routing (051, 007, 021, 181)"),
):
    """
    Pure Renderer (Zero DB Mutation).
    Returns formatted Moroccan bank details (CFG, Attijariwafa, BCP, CIH) and instructions
    for customer bank transfer settlement.
    """
    tenant_cfg = resolve_tenant_by_prefix(tenant or reference)
    acc = get_receiving_treasury_account(preferred_bank_code)

    # Generate QR Code payload for mobile banking / EPC / transfer scanning
    qr_text = f"RIB:{acc['rib']};BENEFICIARY:{acc['beneficiary_name']};AMOUNT:{amount_mad};REF:{reference}"
    qr_image_data_uri = None
    if qrcode is not None:
        try:
            qr = qrcode.QRCode(box_size=4, border=2)
            qr.add_data(qr_text)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            qr_image_data_uri = f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"
        except Exception:
            qr_image_data_uri = None

    return {
        "service": f"Charikat.ma Payments ({tenant_cfg.name})",
        "tenant": tenant_cfg.id,
        "tenant_name": tenant_cfg.name,
        "reference": reference,
        "mandatory_motif": reference,
        "amount_mad": float(amount_mad),
        "amount_mad_display": f"{amount_mad:,.2f} MAD",
        "beneficiary": acc["beneficiary_name"],
        "bank_name": acc["bank_name"],
        "bank_code": acc["bank_code"],
        "agency": acc["agency"],
        "rib": acc["rib"],
        "rib_formatted": acc["rib_formatted"],
        "swift_bic": acc["swift_bic"],
        "transfer_type": acc["transfer_type"],
        "notice": f"Veuillez indiquer impérativement la référence '{reference}' dans le motif du virement.",
        "customer_name": client_name,
        "qr_code_payload": qr_text,
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
    1. Validates Idempotency-Key.
    2. Upserts invoice record in BIGINT centimes.
    3. Resolves current payment status (PENDING_CONFIRMATION, CONFIRMED, ALREADY_RECONCILED, REJECTED_UNDERPAYMENT).
    """
    conn = get_connection()
    try:
        def _exec():
            return invoice_service.upsert_and_check(
                conn=conn,
                tenant=auth,
                client_invoice_id=body.invoice_id,
                reference=body.reference,
                amount_mad=body.amount_mad,
                customer_name=body.customer_name,
            )

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
    bank_id = body.bank_txn_id or f"sim_{uuid.uuid4().hex[:12]}"
    conn = get_connection()
    try:
        res = reconciliation.apply_transfer(
            conn=conn,
            tenant_id=auth.id,
            bank_txn_id=bank_id,
            amount_centimes=to_centimes(body.amount_mad),
            reference=body.reference,
            sender_name=body.sender_name,
            sender_rib=body.sender_rib,
            tolerance_centimes=auth.underpayment_tolerance_centimes,
            raw={"simulated": True, "auth_tenant": auth.id},
        )
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
