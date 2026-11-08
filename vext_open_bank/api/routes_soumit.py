"""
routes_soumit.py
Dedicated API Bridge connecting VextOpenBank (CFG Bank / VEXT AI) with Soumit.ma
Powering frictionless, automated bank transfer subscriptions, proforma settlements, and instant ledger reconciliation.
Treasury Account: CFG Bank 050810006011833971200189 (Beneficiary: VEXT AI)
"""

from fastapi import APIRouter, HTTPException, Header, Query
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
import time
from datetime import datetime
from vext_open_bank.services.envoisms_bridge import envoisms_bridge, SHARED_WEBHOOK_SECRET
from vext_open_bank.core.moroccan_banks import (
    get_receiving_treasury_account,
    list_receiving_treasury_accounts,
    resolve_moroccan_bank,
)
from vext_open_bank.services.bank_connectors import bank_registry

router = APIRouter(prefix="/soumit", tags=["Soumit.ma Automated Bank Bridge"])


class SoumitOrderCheckRequest(BaseModel):
    order_id: str = Field(..., description="Unique Soumit order ID, e.g. ord_bc_12345 or BC-2026-XXXX")
    account_id: str = Field(..., description="Soumit user or organization ID")
    amount_mad: float = Field(..., gt=0, description="Exact amount in MAD expected (e.g. 14256.00 for annual Pro TTC)")
    reference: str = Field(..., description="Unique bank transfer reference motif, e.g. BC-2026-ABC123 or SOU-10001")
    customer_name: Optional[str] = Field(None, description="Sender customer name or corporate entity")
    customer_phone: Optional[str] = Field(None, description="Sender phone number")


class SoumitIncomingTransferSimulation(BaseModel):
    amount_mad: float = Field(..., gt=0)
    sender_name: str = Field(..., description="Name of sender appearing on bank ledger")
    motif: str = Field(..., description="Transfer motif/libellé, e.g. 'Reglement BC-2026-ABC123'")
    sender_rib: Optional[str] = "007780000000123456789012"
    bank_tx_id: Optional[str] = None


@router.get("/instructions")
def get_soumit_banking_instructions(
    reference: Optional[str] = None,
    amount_mad: Optional[float] = 14256.0,
    preferred_bank_code: Optional[str] = Query(
        None,
        description="Optional preferred bank for zero-fee intra-bank routing (051=CFG, 007=Attijariwafa, 021=BCP, 181=CIH)",
    ),
):
    """
    Returns canonical Moroccan bank account details for Soumit.ma subscription.
    Supports Smart Bank Routing: displays direct intra-bank RIB if customer banks with Attijariwafa,
    CIH, or BCP to eliminate interbank delays and transfer fees.
    Default treasury: CFG Bank (050810006011833971200189 / VEXT AI).
    """
    ref_code = reference or f"BC-{datetime.now().strftime('%Y')}-{datetime.now().strftime('%H%M%S')}"
    acc = get_receiving_treasury_account(preferred_bank_code)

    return {
        "service": "Soumit.ma - Intelligence des Marchés Publics",
        "bank_name": acc["bank_name"],
        "bank_code": acc["bank_code"],
        "agency": acc["agency"],
        "beneficiary_name": acc["beneficiary_name"],
        "rib": acc["rib"],
        "rib_formatted": acc["rib_formatted"],
        "swift_bic": acc["swift_bic"],
        "transfer_type": acc["transfer_type"],
        "mandatory_motif": ref_code,
        "amount_mad": amount_mad,
        "notice": "Votre abonnement Soumit est activé automatiquement dès détection de l'ordre de virement par notre passerelle bancaire.",
        "support_email": "contact@vext.im",
        "available_receiving_banks": [a["bank_name"] for a in list_receiving_treasury_accounts()],
    }


@router.get("/receiving-accounts")
def get_all_receiving_accounts():
    """
    List all active multi-bank receiving treasury accounts (CFG, Attijariwafa, BCP, CIH).
    Used by Soumit / Charikat checkout frontends for Smart Treasury Routing.
    """
    return {
        "tenant": "soumit.ma",
        "total_treasury_accounts": len(list_receiving_treasury_accounts()),
        "accounts": list_receiving_treasury_accounts(),
    }


@router.post("/check-order")
def check_and_reconcile_soumit_order(
    req: SoumitOrderCheckRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """
    Zero-latency check to determine if customer's transfer has arrived on CFG Bank.
    Resolves directly from local SQLite database in < 5ms without synchronous calls to CFG.
    If matched within ±0.05 MAD, locks bank transaction, dispatches webhook to https://soumit.ma/api/webhooks/vextopenbank,
    and returns cryptographically signed confirmation.
    Protected by strict Idempotency-Key support for network retries.
    """
    req_hash = f"{req.order_id}:{req.amount_mad}:{req.reference}"
    if idempotency_key:
        cached = envoisms_bridge.check_idempotency(idempotency_key, "/soumit/check-order", req_hash)
        if cached:
            status_code, resp_payload = cached
            if status_code == 400:
                raise HTTPException(status_code=400, detail=resp_payload)
            return resp_payload

    success, result = envoisms_bridge.reconcile_topup(
        topup_id=req.order_id,
        account_id=req.account_id,
        amount_mad=req.amount_mad,
        reference=req.reference,
        customer_name=req.customer_name,
        customer_phone=req.customer_phone,
    )
    if not success and result.get("status") == "REJECTED_UNDERPAYMENT":
        if idempotency_key:
            envoisms_bridge.store_idempotency(idempotency_key, "/soumit/check-order", req_hash, 400, result)
        raise HTTPException(status_code=400, detail=result)

    if idempotency_key:
        envoisms_bridge.store_idempotency(idempotency_key, "/soumit/check-order", req_hash, 200, result)

    return result


@router.get("/sync-status")
def get_soumit_sync_status() -> Dict[str, Any]:
    """
    Diagnostic status of Soumit bank reconciliation bridge and CFG anti-throttling shields.
    """
    now = time.time()
    last_sync = envoisms_bridge._last_sync_timestamp
    elapsed = now - last_sync if last_sync > 0 else -1
    active_pending = envoisms_bridge.get_active_pending_intents()

    return {
        "tenant": "soumit.ma",
        "status": "OPERATIONAL",
        "anti_throttling_cooldown_seconds": envoisms_bridge.MIN_SYNC_INTERVAL,
        "seconds_since_last_cfg_sync": round(elapsed, 1) if elapsed >= 0 else None,
        "cooldown_active": not envoisms_bridge.should_trigger_sync(),
        "active_pending_intents": len(active_pending),
        "local_ledger_ready": True,
        "treasury_account": "050810006011833971200189 (CFG Bank - VEXT AI)",
        "webhook_endpoint": "https://soumit.ma/api/webhooks/vextopenbank",
    }


@router.post("/simulate-incoming-transfer")
def simulate_soumit_incoming_transfer(req: SoumitIncomingTransferSimulation):
    """
    Simulate an incoming bank transfer to the VEXT AI CFG account with Soumit motif.
    Used for end-to-end integration tests and frictionless demo runs.
    """
    tx_id = req.bank_tx_id or f"CFG-SOU-{datetime.now().strftime('%Y%m%d%H%M%S')}-{abs(hash(req.motif)) % 10000}"
    rec = envoisms_bridge.record_incoming_bank_transfer(
        bank_tx_id=tx_id,
        amount_mad=req.amount_mad,
        sender_name=req.sender_name,
        motif=req.motif,
        sender_rib=req.sender_rib,
    )
    return {
        "status": "RECORDED",
        "message": f"Virement entrant de {req.amount_mad:,.2f} MAD enregistré sur le compte VEXT AI pour Soumit",
        "transfer": rec,
    }
