"""
test_envoisms.py
Automated Pytest for EnvoiSMS Bank Bridge Reconciliation
"""

import pytest
import uuid
from fastapi.testclient import TestClient
from vext_open_bank.main import app

client = TestClient(app)

def test_envoisms_instructions():
    res = client.get("/api/v1/envoisms/instructions?reference=ENV-10001&amount_mad=275.0")
    assert res.status_code == 200
    data = res.json()
    assert data["bank_name"] == "CFG Bank"
    assert data["beneficiary_name"] == "VEXT AI"
    assert data["mandatory_motif"] == "ENV-10001"
    assert data["amount_mad"] == 275.0

def test_envoisms_reconciliation_flow():
    unique_run = uuid.uuid4().hex[:8]
    topup_id = f"topup_test_{unique_run}"
    ref = f"ENV-{unique_run.upper()}"
    amount = 1690.0

    # 1. Check before transfer exists
    check_pre = client.post("/api/v1/envoisms/check-topup", json={
        "topup_id": topup_id,
        "account_id": "acc_pytest_user",
        "amount_mad": amount,
        "reference": ref,
        "customer_name": "Test Company"
    })
    assert check_pre.status_code == 200
    assert check_pre.json()["status"] == "PENDING_CONFIRMATION"

    # 2. Incoming transfer arrives on CFG
    sim = client.post("/api/v1/envoisms/simulate-incoming-transfer", json={
        "amount_mad": amount,
        "sender_name": "TEST COMPANY SARL",
        "motif": f"Reglement recharge {ref}"
    })
    assert sim.status_code == 200
    assert sim.json()["status"] == "RECORDED"

    # 3. Check and confirm
    check_post = client.post("/api/v1/envoisms/check-topup", json={
        "topup_id": topup_id,
        "account_id": "acc_pytest_user",
        "amount_mad": amount,
        "reference": ref,
        "customer_name": "Test Company"
    })
    assert check_post.status_code == 200
    data = check_post.json()
    assert data["status"] == "CONFIRMED"
    assert "signature" in data["details"]
    assert data["details"]["amount_mad"] == amount

    # 4. Check already reconciled
    check_dup = client.post("/api/v1/envoisms/check-topup", json={
        "topup_id": topup_id,
        "account_id": "acc_pytest_user",
        "amount_mad": amount,
        "reference": ref,
        "customer_name": "Test Company"
    })
    assert check_dup.status_code == 200
    assert check_dup.json()["status"] == "ALREADY_RECONCILED"
