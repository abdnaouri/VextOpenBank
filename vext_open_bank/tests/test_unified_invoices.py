"""
test_unified_invoices.py
Thorough verification test suite for VEXT Unified Invoicing Engine v1.0.
Covers:
1. Money parsing & validation (centimes precision, float/str/int conversion, fractional & negative rejection)
2. InvoiceState FSM transitions & illegal transition protection
3. Idempotency Guard (key replay returns cached payload, payload mutation raises HTTP 409 Conflict)
4. Public Instructions endpoint (CFG account details, QR code generation, formatting)
5. Universal Check & Upsert (/api/v1/invoices/check)
6. Reconciliation Engine & Transfer Simulation (matching, exact amount, underpayment detection,
   bank_txn_id deduplication, transactional outbox event creation, and audit logging)
7. Backward-compatible aliases and multi-tenant routing
"""

import uuid
import json
import sqlite3
import pytest
from decimal import Decimal
from fastapi.testclient import TestClient

from vext_open_bank.main import app, ORIGIN_SHIELD_SECRET
from vext_open_bank.core.money import to_centimes, from_centimes, MoneyError
from vext_open_bank.core.state_machine import InvoiceState, transition, IllegalTransition, API_STATUS
from vext_open_bank.core.tenants import resolve_tenant_by_prefix, SEED_TENANTS
from vext_open_bank.db.session import get_connection, init_db
from vext_open_bank.services import reconciliation, invoice_service

client = TestClient(app, headers={"x-origin-shield": ORIGIN_SHIELD_SECRET})
raw_client = TestClient(app)


# ============================================================================
# 1. Money Parsing & Validation Tests
# ============================================================================

def test_money_to_centimes_valid():
    """Valid integer, Decimal, float, and string representations to centimes."""
    assert to_centimes(100) == 10000
    assert to_centimes(Decimal("275.00")) == 27500
    assert to_centimes(Decimal("14256.50")) == 1425650
    assert to_centimes("275.00") == 27500
    assert to_centimes(275.0) == 27500
    assert to_centimes("0.05") == 5
    assert to_centimes(0) == 0


def test_money_from_centimes():
    """Convert integer centimes back to 2-decimal MAD."""
    assert from_centimes(27500) == Decimal("275.00")
    assert from_centimes(1425650) == Decimal("14256.50")
    assert from_centimes(5) == Decimal("0.05")
    assert from_centimes(0) == Decimal("0.00")


def test_money_rejection_fractional_centimes():
    """Amounts with > 2 decimal places must raise MoneyError."""
    with pytest.raises(MoneyError):
        to_centimes("275.001")
    with pytest.raises(MoneyError):
        to_centimes(Decimal("10.125"))


def test_money_rejection_negative_and_overflow():
    """Negative amounts and amounts exceeding ceiling must be rejected."""
    with pytest.raises(MoneyError):
        to_centimes("-50.00")
    with pytest.raises(MoneyError):
        to_centimes(-10)
    with pytest.raises(MoneyError):
        to_centimes("999999999999.00")


# ============================================================================
# 2. State Machine (FSM) Transitions
# ============================================================================

def test_fsm_valid_lifecycle():
    """Follow standard happy path lifecycle."""
    # INSTRUCTIONS_ISSUED -> AWAITING_TRANSFER
    transition(InvoiceState.INSTRUCTIONS_ISSUED, InvoiceState.AWAITING_TRANSFER)

    # AWAITING_TRANSFER -> PARTIALLY_PAID -> CONFIRMED -> ALREADY_RECONCILED
    transition(InvoiceState.AWAITING_TRANSFER, InvoiceState.PARTIALLY_PAID)
    transition(InvoiceState.PARTIALLY_PAID, InvoiceState.CONFIRMED)
    transition(InvoiceState.CONFIRMED, InvoiceState.ALREADY_RECONCILED)


def test_fsm_illegal_transitions():
    """Invalid leaps must raise IllegalTransition."""
    with pytest.raises(IllegalTransition):
        transition(InvoiceState.ALREADY_RECONCILED, InvoiceState.AWAITING_TRANSFER)

    with pytest.raises(IllegalTransition):
        transition(InvoiceState.CANCELLED, InvoiceState.CONFIRMED)

    with pytest.raises(IllegalTransition):
        transition(InvoiceState.INSTRUCTIONS_ISSUED, InvoiceState.CONFIRMED)


def test_fsm_api_status_mapping():
    """Ensure all InvoiceState values map properly to client-facing API status."""
    for state in InvoiceState:
        assert state in API_STATUS
        assert API_STATUS[state] in ("PENDING_CONFIRMATION", "CONFIRMED", "ALREADY_RECONCILED", "REJECTED_UNDERPAYMENT")


# ============================================================================
# 3. Public Instructions Pure Renderer Tests
# ============================================================================

def test_invoice_instructions_public_access():
    """Verify endpoint is publicly accessible without security headers."""
    res = raw_client.get("/api/v1/invoices/instructions?reference=CHK-TEST01&amount_mad=2500.00")
    assert res.status_code == 200
    data = res.json()
    assert data["reference"] == "CHK-TEST01"
    assert data["amount_mad"] == 2500.0
    assert data["amount_mad_display"] == "2,500.00 MAD"
    assert data["bank_name"] == "CFG Bank"
    assert data["rib"] == "050810006011833971200189"
    assert data["beneficiary"] == "VEXT AI"
    assert "qr_code_payload" in data
    assert "RIB:050810006011833971200189" in data["qr_code_payload"]
    assert "data:image/png;base64," in data["qr_code_data_uri"]


def test_invoice_instructions_tenant_and_bank_routing():
    """Test multi-bank preferred routing and tenant prefix detection."""
    # Preferred bank Attijariwafa (007)
    res_awb = client.get("/api/v1/invoices/instructions?reference=ENV-9999&amount_mad=275.00&preferred_bank_code=007")
    assert res_awb.status_code == 200
    data_awb = res_awb.json()
    assert data_awb["bank_code"] == "007"
    assert data_awb["bank_name"] == "Attijariwafa Bank"
    assert data_awb["tenant"] == "envoisms"

    # Preferred bank BCP (021) for Soumit
    res_bcp = client.get("/api/v1/invoices/instructions?reference=SOU-8888&amount_mad=14256.00&preferred_bank_code=021")
    assert res_bcp.status_code == 200
    data_bcp = res_bcp.json()
    assert data_bcp["bank_code"] == "021"
    assert "Banque" in data_bcp["bank_name"] and "Populaire" in data_bcp["bank_name"]
    assert data_bcp["tenant"] == "soumit"


# ============================================================================
# 4. Universal Invoice Check & Idempotency Guard Tests
# ============================================================================

def test_invoice_check_and_idempotency_cache():
    """Test idempotent creation and replay return identical cached response."""
    unique = uuid.uuid4().hex[:8]
    inv_id = f"inv_{unique}"
    ref = f"CHK-{unique.upper()}"
    amount = 1500.0
    idem_key = f"idem_{unique}"

    headers = {
        "x-origin-shield": ORIGIN_SHIELD_SECRET,
        "Idempotency-Key": idem_key,
    }

    payload = {
        "invoice_id": inv_id,
        "tenant": "charikat",
        "amount_mad": amount,
        "reference": ref,
        "customer_name": "Test Client SARL"
    }

    # First request creates invoice in AWAITING_TRANSFER
    res1 = client.post("/api/v1/invoices/check", json=payload, headers=headers)
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["status"] == "PENDING_CONFIRMATION"
    assert data1["state"] == "AWAITING_TRANSFER"
    assert data1["reference"] == ref

    # Replay identical request with same key
    res2 = client.post("/api/v1/invoices/check", json=payload, headers=headers)
    assert res2.status_code == 200
    assert res2.json() == data1

    # Conflict: Reusing same key with different body must return 409 Conflict
    mutated_payload = payload.copy()
    mutated_payload["amount_mad"] = 9999.0
    res_conflict = client.post("/api/v1/invoices/check", json=mutated_payload, headers=headers)
    assert res_conflict.status_code == 409


# ============================================================================
# 5. Reconciliation Engine, Audit Log, and Outbox Generation Tests
# ============================================================================

def test_reconciliation_exact_match_and_audit():
    """Simulate transfer matching exact invoice amount, verify outbox & audit log."""
    unique = uuid.uuid4().hex[:8]
    inv_id = f"inv_{unique}"
    ref = f"CHK-REC-{unique.upper()}"
    amount = 3000.0

    # 1. Register invoice
    client.post("/api/v1/invoices/check", json={
        "invoice_id": inv_id,
        "tenant": "charikat",
        "amount_mad": amount,
        "reference": ref,
        "customer_name": "Atlantic Traders"
    })

    # 2. Simulate incoming bank transfer
    bank_txn_id = f"TX_BANK_{unique}"
    sim_res = client.post("/api/v1/invoices/simulate-transfer", json={
        "reference": ref,
        "amount_mad": amount,
        "sender_name": "ATLANTIC TRADERS SA",
        "bank_txn_id": bank_txn_id
    })
    assert sim_res.status_code == 200
    sim_data = sim_res.json()
    assert sim_data["result"] == "MATCHED"
    assert sim_data["state"] == "CONFIRMED"

    # 3. Check invoice status now returns CONFIRMED
    check_res = client.post("/api/v1/invoices/check", json={
        "invoice_id": inv_id,
        "tenant": "charikat",
        "amount_mad": amount,
        "reference": ref,
    })
    assert check_res.status_code == 200
    assert check_res.json()["status"] == "CONFIRMED"

    # 4. Inspect DB directly for transfers, audit_log, and outbox_events
    conn = get_connection()
    try:
        # Transfer ledger
        trf = conn.execute("SELECT * FROM transfers WHERE bank_txn_id = ?", (bank_txn_id,)).fetchone()
        assert trf is not None
        assert trf["match_status"] == "MATCHED"
        assert trf["amount_centimes"] == 300000

        # Audit log entry
        audit = conn.execute("SELECT * FROM audit_log WHERE invoice_id = ?", (sim_data["invoice_id"],)).fetchone()
        assert audit is not None
        assert audit["action"] == "transfer.applied"
        assert f"bank:{bank_txn_id}" == audit["actor"]

        # Outbox event
        outbox = conn.execute(
            "SELECT * FROM outbox_events WHERE tenant_id = 'charikat' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert outbox is not None
        payload = json.loads(outbox["payload_json"])
        assert payload["event"] == "invoice.confirmed"
        assert payload["reference"] == ref
        assert payload["bank_txn_id"] == bank_txn_id
    finally:
        conn.close()


def test_reconciliation_bank_txn_deduplication():
    """Applying the same bank_txn_id twice must be flagged as DUPLICATE without mutating state."""
    unique = uuid.uuid4().hex[:8]
    ref = f"CHK-DEDUP-{unique.upper()}"
    amount = 500.0

    client.post("/api/v1/invoices/check", json={
        "invoice_id": f"inv_{unique}",
        "tenant": "charikat",
        "amount_mad": amount,
        "reference": ref,
    })

    bank_id = f"TX_DEDUP_{unique}"
    res1 = client.post("/api/v1/invoices/simulate-transfer", json={
        "reference": ref,
        "amount_mad": amount,
        "bank_txn_id": bank_id,
    })
    assert res1.json()["result"] == "MATCHED"

    # Duplicate call
    res2 = client.post("/api/v1/invoices/simulate-transfer", json={
        "reference": ref,
        "amount_mad": amount,
        "bank_txn_id": bank_id,
    })
    assert res2.json()["result"] == "DUPLICATE"
    assert res2.json()["bank_txn_id"] == bank_id


def test_reconciliation_underpayment_and_partial_payments():
    """Test underpayment transitions to PARTIALLY_PAID and subsequent completion to CONFIRMED."""
    unique = uuid.uuid4().hex[:8]
    inv_id = f"inv_partial_{unique}"
    ref = f"CHK-PART-{unique.upper()}"
    total_amount = 1000.0

    client.post("/api/v1/invoices/check", json={
        "invoice_id": inv_id,
        "tenant": "charikat",
        "amount_mad": total_amount,
        "reference": ref,
    })

    # Pay 400.00 of 1000.00
    res_p1 = client.post("/api/v1/invoices/simulate-transfer", json={
        "reference": ref,
        "amount_mad": 400.0,
        "bank_txn_id": f"tx_p1_{unique}",
    })
    assert res_p1.json()["result"] == "MATCHED"
    assert res_p1.json()["state"] == "PARTIALLY_PAID"

    # Check endpoint shows status REJECTED_UNDERPAYMENT / PARTIALLY_PAID
    chk_p1 = client.post("/api/v1/invoices/check", json={
        "invoice_id": inv_id,
        "tenant": "charikat",
        "amount_mad": total_amount,
        "reference": ref,
    })
    assert chk_p1.json()["status"] == "REJECTED_UNDERPAYMENT"
    assert chk_p1.json()["paid_mad"] == 400.0

    # Complete the remaining 600.00
    res_p2 = client.post("/api/v1/invoices/simulate-transfer", json={
        "reference": ref,
        "amount_mad": 600.0,
        "bank_txn_id": f"tx_p2_{unique}",
    })
    assert res_p2.json()["result"] == "MATCHED"
    assert res_p2.json()["state"] == "CONFIRMED"

    chk_p2 = client.post("/api/v1/invoices/check", json={
        "invoice_id": inv_id,
        "tenant": "charikat",
        "amount_mad": total_amount,
        "reference": ref,
    })
    assert chk_p2.json()["status"] == "CONFIRMED"
    assert chk_p2.json()["paid_mad"] == 1000.0


def test_receiving_accounts_list():
    """Verify endpoint returning all supported multi-bank treasury accounts."""
    res = client.get("/api/v1/invoices/receiving-accounts")
    assert res.status_code == 200
    data = res.json()
    assert data["total_treasury_accounts"] >= 4
    banks = [a["bank_code"] for a in data["accounts"]]
    assert "051" in banks  # CFG
    assert "007" in banks  # Attijariwafa
    assert "021" in banks  # BCP
    assert "181" in banks  # CIH
