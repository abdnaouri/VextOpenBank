"""
routes_envoisms.py
Production API Bridge connecting VextOpenBank (CFG Bank / VEXT AI) with EnvoiSMS.ma
Powering frictionless, automated bank transfer top-ups and reconciliation.
"""

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
import time
from datetime import datetime
from vext_open_bank.services.envoisms_bridge import envoisms_bridge, SHARED_WEBHOOK_SECRET

router = APIRouter(prefix="/envoisms", tags=["EnvoiSMS.ma Automated Bank Bridge"])


class TopupCheckRequest(BaseModel):
    topup_id: str = Field(..., description="Unique EnvoiSMS top-up transaction ID, e.g. topup_01hx...")
    account_id: str = Field(..., description="EnvoiSMS customer account ID")
    amount_mad: float = Field(..., gt=0, description="Exact amount in MAD expected (>= 275 MAD)")
    reference: str = Field(..., description="Unique bank transfer reference motif, e.g. ENV-98421")
    customer_name: Optional[str] = Field(None, description="Sender customer name or corporate entity")
    customer_phone: Optional[str] = Field(None, description="Sender phone number")


class IncomingTransferSimulation(BaseModel):
    amount_mad: float = Field(..., gt=0)
    sender_name: str = Field(..., description="Name of sender appearing on bank ledger")
    motif: str = Field(..., description="Transfer motif/libellé, e.g. 'Recharge EnvoiSMS ENV-98421'")
    sender_rib: Optional[str] = "007780000000123456789012"
    bank_tx_id: Optional[str] = None


@router.get("/instructions")
def get_banking_instructions(reference: Optional[str] = None, amount_mad: Optional[float] = 275.0):
    """
    Returns canonical CFG Bank account details for EnvoiSMS top-up.
    Ultra-low latency (< 1ms), pure static delivery, zero CFG traffic.
    """
    ref_code = reference or f"ENV-{datetime.now().strftime('%H%M%S')}"
    return {
        "bank_name": "CFG Bank",
        "bank_code": "051",
        "agency": "Agence Rabat Zaer / Casablanca",
        "beneficiary_name": "VEXT AI",
        "rib": "050810006011833971200189",
        "rib_formatted": "050 810 0060118339712001 89",
        "swift_bic": "CFGBMAMC",
        "transfer_type": "Virement Instantané (BAM) ou Virement Ordinaire",
        "mandatory_motif": ref_code,
        "amount_mad": amount_mad,
        "notice": "Votre recharge EnvoiSMS est validée et créditée automatiquement dès détection des fonds."
    }


@router.post("/check-topup")
def check_and_reconcile_topup(req: TopupCheckRequest):
    """
    Zero-latency check to determine if the customer's transfer has arrived on CFG Bank.
    Resolves directly from local SQLite database in < 5ms without synchronous calls to CFG.
    If unmatched, records intent and triggers non-blocking background sync (subject to strict cooldown).
    If matched, locks the bank transaction and issues a cryptographically signed confirmation.
    """
    success, result = envoisms_bridge.reconcile_topup(
        topup_id=req.topup_id,
        account_id=req.account_id,
        amount_mad=req.amount_mad,
        reference=req.reference,
        customer_name=req.customer_name,
        customer_phone=req.customer_phone
    )
    return result


@router.get("/sync-status")
def get_bridge_sync_status() -> Dict[str, Any]:
    """
    Diagnostic status of the bank reconciliation bridge and CFG anti-throttling shields.
    """
    now = time.time()
    last_sync = envoisms_bridge._last_sync_timestamp
    elapsed = now - last_sync if last_sync > 0 else -1
    active_pending = envoisms_bridge.get_active_pending_intents()

    return {
        "status": "OPERATIONAL",
        "origin_masking": "ACTIVE (Strict mobile app headers, zero client IP forwarding)",
        "anti_throttling_cooldown_seconds": envoisms_bridge.MIN_SYNC_INTERVAL,
        "seconds_since_last_cfg_sync": round(elapsed, 1) if elapsed >= 0 else None,
        "cooldown_active": not envoisms_bridge.should_trigger_sync(),
        "active_pending_intents": len(active_pending),
        "local_ledger_ready": True
    }


@router.post("/simulate-incoming-transfer")
def simulate_incoming_transfer(req: IncomingTransferSimulation):
    """
    Simulate an incoming interbank transfer to the VEXT AI CFG account.
    Used for end-to-end integration tests and frictionless demo runs.
    """
    tx_id = req.bank_tx_id or f"CFG-SIM-{datetime.now().strftime('%Y%m%d%H%M%S')}-{abs(hash(req.motif)) % 10000}"
    rec = envoisms_bridge.record_incoming_bank_transfer(
        bank_tx_id=tx_id,
        amount_mad=req.amount_mad,
        sender_name=req.sender_name,
        motif=req.motif,
        sender_rib=req.sender_rib
    )
    return {
        "status": "RECORDED",
        "message": f"Virement entrant de {req.amount_mad:,.2f} MAD enregistré sur le compte VEXT AI",
        "transfer": rec
    }
