"""
test_soumit.py
Automated Pytest for Soumit.ma Bank Bridge Reconciliation & Verification
Treasury Account: CFG Bank 050810006011833971200189 (VEXT AI)
"""

import pytest
import time
import uuid
import sqlite3
from fastapi.testclient import TestClient
from vext_open_bank.main import app, ORIGIN_SHIELD_SECRET
from vext_open_bank.services.envoisms_bridge import EnvoiSMSBridge, TENANT_WEBHOOK_URLS, TENANT_SECRETS

client = TestClient(app, headers={"x-origin-shield": ORIGIN_SHIELD_SECRET})


def test_soumit_instructions_public():
    """Public bank instructions endpoint must return CFG Bank treasury account."""
    raw_client = TestClient(app)
    res = raw_client.get("/api/v1/soumit/instructions?reference=BC-2026-TEST01&amount_mad=14256.0")
    assert res.status_code == 200
    data = res.json()
    assert data["bank_name"] == "CFG Bank"
    assert data["beneficiary_name"] == "VEXT AI"
    assert data["rib"] == "050810006011833971200189"
    assert data["mandatory_motif"] == "BC-2026-TEST01"
    assert data["amount_mad"] == 14256.0


def test_soumit_tenant_routing_config():
    """Verify Soumit webhook routing and tenant prefix mapping."""
    assert "SOU" in TENANT_WEBHOOK_URLS
    assert "BC" in TENANT_WEBHOOK_URLS
    assert "https://soumit.ma/api/webhooks/vextopenbank" in TENANT_WEBHOOK_URLS["SOU"]
    assert "https://soumit.ma/api/webhooks/vextopenbank" in TENANT_WEBHOOK_URLS["BC"]


def test_soumit_reconciliation_flow():
    """End-to-end check of Soumit order check, simulation, and reconciliation."""
    unique_run = uuid.uuid4().hex[:6].upper()
    order_id = f"ord_bc_{unique_run.lower()}"
    ref = f"BC-2026-{unique_run}"
    amount = 14256.0

    # 1. Check order before transfer: should be PENDING_CONFIRMATION
    check_pre = client.post("/api/v1/soumit/check-order", json={
        "order_id": order_id,
        "account_id": "usr_soumit_client_1",
        "amount_mad": amount,
        "reference": ref,
        "customer_name": "SARL TRAVAUX PUBLICS MAROC"
    })
    assert check_pre.status_code == 200
    assert check_pre.json()["status"] == "PENDING_CONFIRMATION"

    # 2. Simulate incoming bank transfer arriving on CFG Bank
    sim = client.post("/api/v1/soumit/simulate-incoming-transfer", json={
        "amount_mad": amount,
        "sender_name": "SARL TRAVAUX PUBLICS MAROC",
        "motif": f"Virement reglement abonnement {ref}"
    })
    assert sim.status_code == 200
    assert sim.json()["status"] == "RECORDED"

    # 3. Check again: must be CONFIRMED with cryptographic signature and CFG details
    check_post = client.post("/api/v1/soumit/check-order", json={
        "order_id": order_id,
        "account_id": "usr_soumit_client_1",
        "amount_mad": amount,
        "reference": ref,
        "customer_name": "SARL TRAVAUX PUBLICS MAROC"
    })
    assert check_post.status_code == 200
    data = check_post.json()
    assert data["status"] == "CONFIRMED"
    assert "signature" in data["details"]
    assert data["details"]["amount_mad"] == amount
    assert data["details"]["beneficiary_rib"] == "050810006011833971200189"
    assert data["details"]["tenant"] == "BC"

    # 4. Check already reconciled idempotency
    check_dup = client.post("/api/v1/soumit/check-order", json={
        "order_id": order_id,
        "account_id": "usr_soumit_client_1",
        "amount_mad": amount,
        "reference": ref,
        "customer_name": "SARL TRAVAUX PUBLICS MAROC"
    })
    assert check_dup.status_code == 200
    assert check_dup.json()["status"] == "ALREADY_RECONCILED"


def test_soumit_sync_status():
    """Verify sync status diagnostic endpoint for Soumit."""
    res = client.get("/api/v1/soumit/sync-status")
    assert res.status_code == 200
    data = res.json()
    assert data["tenant"] == "soumit.ma"
    assert data["status"] == "OPERATIONAL"
    assert "050810006011833971200189" in data["treasury_account"]
