"""
test_envoisms.py
Automated Pytest for EnvoiSMS Bank Bridge Reconciliation
"""

import pytest
import uuid
from fastapi.testclient import TestClient
from vext_open_bank.main import app

client = TestClient(app, headers={"x-origin-shield": "cf_shield_origin_auth_837291048576"})

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


def test_public_instructions_without_shield():
    # Calling instructions without origin shield header must succeed cleanly
    raw_client = TestClient(app)
    res = raw_client.get("/api/v1/envoisms/instructions")
    assert res.status_code == 200
    assert res.json()["bank_name"] == "CFG Bank"


def test_bridge_sync_status():
    res = client.get("/api/v1/envoisms/sync-status")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "OPERATIONAL"
    assert data["anti_throttling_cooldown_seconds"] == 25.0
    assert data["local_ledger_ready"] is True


def test_mobilefabric_origin_isolation():
    from vext_open_bank.services.mobilefabric import mf_client
    headers = mf_client._base_headers()
    # Confirm strict mobile headers
    assert headers["X-Kony-Platform-Type"] == "android"
    # Ensure no origin leakage
    assert "X-Forwarded-For" not in headers
    assert "CF-Connecting-IP" not in headers
    assert "Origin" not in headers
    assert "Referer" not in headers


def test_camouflage_decoy_untrusted_requests():
    # Client without shield or key headers (simulating direct internet scanners)
    untrusted = TestClient(app)

    # 1. Probing protected routes yields 404 with zero cues
    res_probe = untrusted.post("/api/v1/envoisms/check-topup", json={"topup_id": "test"})
    assert res_probe.status_code == 404
    assert res_probe.json() == {"detail": "Not Found"}
    assert "Cloudflare" not in res_probe.text
    assert "blocked" not in res_probe.text

    # 2. Hitting root with JSON yields benign idle asset worker
    res_root_json = untrusted.get("/", headers={"accept": "application/json"})
    assert res_root_json.status_code == 200
    assert res_root_json.json()["module"] == "static-asset-dispatcher"

    # 3. Hitting root with HTML yields benign maintenance page
    res_root_html = untrusted.get("/", headers={"accept": "text/html"})
    assert res_root_html.status_code == 200
    assert "Static Asset Dispatcher" in res_root_html.text
