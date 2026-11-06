"""
routes_envoisms.py
Production API Bridge connecting VextOpenBank (CFG Bank / VEXT AI) with EnvoiSMS.ma
Powering frictionless, automated bank transfer top-ups and reconciliation.
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


class GenericBankTransferIngestion(BaseModel):
    bank_tx_id: Optional[str] = Field(None, description="Bank transaction reference or cleared wire ID")
    amount_mad: float = Field(..., gt=0, description="Amount credited in MAD")
    sender_name: str = Field(..., description="Originating sender legal name")
    motif: str = Field(..., description="Wire description or motif entered by sender")
    sender_rib: Optional[str] = Field("007780000000123456789012", description="24-digit sender account RIB")
    recipient_rib: Optional[str] = Field("050810006011833971200189", description="Receiving treasury account RIB")
    recipient_bank_code: Optional[str] = Field("051", description="Bank code receiving funds, e.g. 051 (CFG), 007 (Attijari), 021 (BCP), 181 (CIH)")
    value_date: Optional[str] = None


class ClaimWireRequest(BaseModel):
    bank_tx_id: str = Field(..., description="Unmatched bank transaction ID from suspense account")
    topup_id: str = Field(..., description="Target invoice or topup ID")
    account_id: str = Field(..., description="Tenant client account ID")
    reference: str = Field(..., description="Target canonical reference, e.g. ENV-98421")


@router.get("/instructions")
def get_banking_instructions(
    reference: Optional[str] = None,
    amount_mad: Optional[float] = 275.0,
    preferred_bank_code: Optional[str] = Query(
        None,
        description="Optional preferred bank for zero-fee intra-bank routing (051=CFG, 007=Attijariwafa, 021=BCP, 181=CIH)",
    ),
):
    """
    Returns canonical Moroccan bank account details for EnvoiSMS top-up.
    Supports Smart Bank Routing: displays direct intra-bank RIB if customer banks with Attijariwafa,
    CIH, or BCP to eliminate interbank delays and transfer fees.
    """
    ref_code = reference or f"ENV-{datetime.now().strftime('%H%M%S')}"
    acc = get_receiving_treasury_account(preferred_bank_code)

    return {
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
        "notice": "Votre recharge EnvoiSMS est validée et créditée automatiquement dès détection des fonds.",
        "available_receiving_banks": [a["bank_name"] for a in list_receiving_treasury_accounts()],
    }


@router.get("/receiving-accounts")
def get_all_receiving_accounts():
    """
    List all active multi-bank receiving treasury accounts (CFG, Attijariwafa, BCP, CIH).
    Used by checkout frontends for Smart Treasury Routing.
    """
    return {
        "total_treasury_accounts": len(list_receiving_treasury_accounts()),
        "accounts": list_receiving_treasury_accounts(),
    }


@router.post("/ingest-transfer")
def ingest_generic_bank_transfer(req: GenericBankTransferIngestion):
    """
    Universal Multi-Bank Ingestion Endpoint.
    Ingests bank transfers originating from or received on ANY Moroccan bank (CFG, Attijariwafa,
    BCP, CIH, or incoming EBICS / ISO 20022 CAMT.053 statement jobs).
    """
    tx_id = req.bank_tx_id or f"TX-BAM-{datetime.now().strftime('%Y%m%d%H%M%S')}-{abs(hash(req.motif)) % 10000}"
    rec = envoisms_bridge.record_incoming_bank_transfer(
        bank_tx_id=tx_id,
        amount_mad=req.amount_mad,
        sender_name=req.sender_name,
        motif=req.motif,
        sender_rib=req.sender_rib,
        recipient_rib=req.recipient_rib,
        recipient_bank_code=req.recipient_bank_code,
        value_date=req.value_date,
    )
    return {
        "status": "INGESTED",
        "message": f"Virement entrant de {req.amount_mad:,.2f} MAD ingéré avec succès sur le compte {req.recipient_bank_code or '051'}",
        "transfer": rec,
    }


@router.get("/unmatched-transfers")
def list_unmatched_transfers(limit: int = Query(50, ge=1, le=200)):
    """
    Inspect the Bank Suspense Account ('Compte d'attente') containing unreconciled transfers
    or transfers missing client reference motifs ('Zombie Wires').
    """
    unmatched = envoisms_bridge.get_unmatched_transfers(limit=limit)
    return {
        "unmatched_count": len(unmatched),
        "transfers": unmatched,
    }


@router.post("/claim-unmatched-wire")
def claim_unmatched_wire(req: ClaimWireRequest):
    """
    Allocate an unreferenced transfer from the suspense account to an intent/invoice.
    """
    success, result = envoisms_bridge.claim_unmatched_transfer(
        bank_tx_id=req.bank_tx_id,
        topup_id=req.topup_id,
        account_id=req.account_id,
        reference=req.reference,
    )
    if not success and result.get("status") == "REJECTED_UNDERPAYMENT":
        raise HTTPException(status_code=400, detail=result)
    if not success and "error" in result:
        raise HTTPException(status_code=404, detail=result)
    return result


@router.post("/check-topup")
def check_and_reconcile_topup(req: TopupCheckRequest):
    """
    Zero-latency check to determine if the customer's transfer has arrived on CFG Bank.
    Resolves directly from local SQLite database in < 5ms without synchronous calls to CFG.
    If unmatched, records intent and triggers non-blocking background sync (subject to strict cooldown).
    If matched within ±0.05 MAD, locks the bank transaction and issues a cryptographically signed confirmation.
    If an underpayment or variance > 0.05 MAD is detected, rejects with HTTP 400 (Incohérence de montant)
    and reports the exact missing amount to the client.
    """
    success, result = envoisms_bridge.reconcile_topup(
        topup_id=req.topup_id,
        account_id=req.account_id,
        amount_mad=req.amount_mad,
        reference=req.reference,
        customer_name=req.customer_name,
        customer_phone=req.customer_phone
    )
    if not success and result.get("status") == "REJECTED_UNDERPAYMENT":
        raise HTTPException(status_code=400, detail=result)
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


class BankStatementParseRequest(BaseModel):
    bank_code: str = Field(..., min_length=3, max_length=3, description="3-digit bank code, e.g. '007' (Attijariwafa), '078' (CDM)")
    statement_content: str = Field(..., description="Raw MT940 text or CAMT.053 XML content")
    file_format: Optional[str] = Field("MT940", description="'MT940' or 'CAMT.053'")
    recipient_rib: Optional[str] = Field(None, description="Optional tenant account RIB")


class TenantBankRegisterRequest(BaseModel):
    tenant_id: str = Field(..., description="Tenant identifier, e.g. 'ENV', 'MED', 'BAC'")
    bank_code: str = Field(..., min_length=3, max_length=3, description="3-digit Moroccan bank code")
    account_rib: str = Field(..., min_length=24, max_length=24, description="24-digit Moroccan RIB")
    account_name: Optional[str] = None


@router.post("/parse-bank-statement", summary="Ingest MT940 / CAMT.053 Statement from Non-CFG Bank")
def parse_and_ingest_statement(req: BankStatementParseRequest):
    """
    Parse an official EBICS / Host-to-Host statement file (MT940 or CAMT.053 XML)
    downloaded or streamed from non-CFG corporate banks (Attijariwafa Bank, Crédit du Maroc, BCP, CIH).
    Automatically commits all extracted credit lines into the VextOpenBank reconciliation ledger.
    """
    count, transfers = bank_registry.parse_and_ingest_statement(
        bank_code=req.bank_code,
        statement_content=req.statement_content,
        file_format=req.file_format or "MT940",
        recipient_rib=req.recipient_rib,
    )
    return {
        "status": "PROCESSED",
        "bank_code": req.bank_code,
        "transfers_ingested": count,
        "transfers": transfers,
    }


@router.get("/tenant-bank-configs", summary="List Tenant Bank Accounts")
def list_tenant_bank_configs():
    """
    List registered SaaS tenant bank accounts across Moroccan institutions (Attijariwafa, CDM, CFG).
    """
    return {
        "tenants": bank_registry.list_all_tenant_configs(),
    }


@router.post("/register-tenant-bank", summary="Register Tenant Non-CFG Bank Account")
def register_tenant_bank(req: TenantBankRegisterRequest):
    """
    Register a SaaS tenant's dedicated receiving account at a non-CFG bank (e.g. Attijariwafa or CDM).
    """
    bank_registry.register_tenant_bank_account(
        tenant_id=req.tenant_id,
        bank_code=req.bank_code,
        account_rib=req.account_rib,
        name=req.account_name,
    )
    return {
        "status": "REGISTERED",
        "tenant_id": req.tenant_id.upper(),
        "bank_code": req.bank_code,
        "rib": req.account_rib,
    }
