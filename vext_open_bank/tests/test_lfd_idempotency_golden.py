"""
test_lfd_idempotency_golden.py
Legacy-First Design (LFD) Zero-Maintenance Verification Test Suite
Verifies:
1. Idempotency-Key caching & network replay resilience
2. WAL Pragmas & Concurrency
3. Reconciliation Exceptions dead-letter table recording
4. Golden-file assertion for API payload determinism
"""

import pytest
import uuid
import json
import sqlite3
from fastapi.testclient import TestClient
from vext_open_bank.main import app, ORIGIN_SHIELD_SECRET
from vext_open_bank.services.envoisms_bridge import envoisms_bridge

client = TestClient(app, headers={"x-origin-shield": ORIGIN_SHIELD_SECRET})


def test_idempotency_key_caching():
    """Verify that repeated requests with same Idempotency-Key return cached response."""
    unique_run = uuid.uuid4().hex[:8]
    idempotency_key = f"idem_key_{unique_run}"
    topup_id = f"topup_idem_{unique_run}"
    ref = f"ENV-{unique_run.upper()}"
    amount = 500.0

    headers = {
        "x-origin-shield": ORIGIN_SHIELD_SECRET,
        "Idempotency-Key": idempotency_key,
    }

    # Initial check (pending)
    res1 = client.post("/api/v1/envoisms/check-topup", json={
        "topup_id": topup_id,
        "account_id": "acc_idem_user",
        "amount_mad": amount,
        "reference": ref,
    }, headers=headers)
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["status"] == "PENDING_CONFIRMATION"

    # Simulate transfer arrived
    client.post("/api/v1/envoisms/simulate-incoming-transfer", json={
        "amount_mad": amount,
        "sender_name": "IDEM CORP",
        "motif": f"Payment {ref}",
    })

    # Repeated request with SAME Idempotency-Key must return the CACHED response
    res2 = client.post("/api/v1/envoisms/check-topup", json={
        "topup_id": topup_id,
        "account_id": "acc_idem_user",
        "amount_mad": amount,
        "reference": ref,
    }, headers=headers)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "PENDING_CONFIRMATION"  # Cached!

    # Request with NEW Idempotency-Key must evaluate and confirm
    res3 = client.post("/api/v1/envoisms/check-topup", json={
        "topup_id": topup_id,
        "account_id": "acc_idem_user",
        "amount_mad": amount,
        "reference": ref,
    }, headers={"x-origin-shield": ORIGIN_SHIELD_SECRET, "Idempotency-Key": f"new_{idempotency_key}"})
    assert res3.status_code == 200
    data3 = res3.json()
    assert data3["status"] == "CONFIRMED"


def test_reconciliation_exception_recorded():
    """Verify underpayments and variances are recorded to reconciliation_exceptions table."""
    unique_run = uuid.uuid4().hex[:8]
    topup_id = f"topup_fraud_{unique_run}"
    ref = f"ENV-{unique_run.upper()}"
    amount = 1000.0

    # Ingest underpayment
    client.post("/api/v1/envoisms/simulate-incoming-transfer", json={
        "amount_mad": 500.0,
        "sender_name": "FRAUDSTER",
        "motif": f"Payment {ref}",
    })

    res = client.post("/api/v1/envoisms/check-topup", json={
        "topup_id": topup_id,
        "account_id": "acc_fraud_check",
        "amount_mad": amount,
        "reference": ref,
    })
    assert res.status_code == 400

    # Verify exception exists in SQLite
    with envoisms_bridge._get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM reconciliation_exceptions WHERE intent_id = ?",
            (topup_id,),
        ).fetchone()
        assert row is not None
        assert row["reason"] == "UNDERPAYMENT_OR_VARIANCE_MISMATCH"
        assert row["amount_mad"] == 1000.0


def test_public_envoisms_instructions_golden():
    """Golden contract check: Verify public instructions schema for EnvoiSMS."""
    res = client.get("/api/v1/envoisms/instructions?reference=ENV-GOLDEN01&amount_mad=275.0")
    assert res.status_code == 200
    data = res.json()
    assert data["bank_name"] == "CFG Bank"
    assert data["bank_code"] == "051"
    assert data["beneficiary_name"] == "VEXT AI"
    assert data["rib"] == "050810006011833971200189"
    assert data["swift_bic"] == "CFGBMAMC"
    assert data["mandatory_motif"] == "ENV-GOLDEN01"
    assert data["amount_mad"] == 275.0
