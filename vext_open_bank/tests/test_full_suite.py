"""
Comprehensive test suite for the complete 42-service MobileFabric coverage,
step-up MFA state machine, transfers, PFM mutations, Berlin Group AIS/PIS,
agency, and comms endpoints.
"""

from unittest.mock import patch
from fastapi.testclient import TestClient

from vext_open_bank.main import app, ORIGIN_SHIELD_SECRET
from vext_open_bank.services.mobilefabric import mf_client

client = TestClient(app, headers={"x-origin-shield": ORIGIN_SHIELD_SECRET})


# ---------------------------------------------------------------------------
# Security & Step-Up MFA Tests
# ---------------------------------------------------------------------------

def test_mfa_challenge_sms():
    with patch.object(mf_client, "trigger_mfa_challenge", return_value={"status": "OTP_SENT"}):
        resp = client.post("/api/v1/security/mfa/challenge", json={"channel": "sms"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "CHALLENGE_SENT"
        assert data["channel"] == "sms"


def test_mfa_verify_and_elevation():
    with patch.object(mf_client, "verify_mfa_and_elevate", return_value="mock_elevated_jwt_token_1234567890abcdef"):
        resp = client.post("/api/v1/security/mfa/verify", json={"otp_code": "123456"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ELEVATED"
        assert "elevated_token_prefix" in data


def test_token_validate_endpoint():
    with patch.object(mf_client, "validate_token", return_value={"valid": True, "expires_in": 3600}):
        resp = client.post("/api/v1/security/token/validate", json={"token": "test-token"})
        assert resp.status_code == 200
        assert resp.json()["valid"] is True


def test_logout_endpoint():
    with patch.object(mf_client, "logout", return_value={"success": True}):
        resp = client.post("/api/v1/security/logout")
        assert resp.status_code == 200
        assert resp.json()["status"] == "LOGGED_OUT"


def test_secondary_tokens():
    with patch.object(mf_client, "get_microsoft_token", return_value={"access_token": "ms-crm-token"}):
        resp = client.post("/api/v1/security/token/microsoft")
        assert resp.status_code == 200
        assert resp.json()["access_token"] == "ms-crm-token"

    with patch.object(mf_client, "get_access_token1", return_value={"access_token": "acc1-token"}):
        resp = client.post("/api/v1/security/token/access1")
        assert resp.status_code == 200
        assert resp.json()["access_token"] == "acc1-token"


# ---------------------------------------------------------------------------
# Transfers, Payees & Impayés Tests
# ---------------------------------------------------------------------------

def test_transfers_payees_rib_mod97_validation():
    # Invalid RIB should fail with 400
    resp = client.post(
        "/api/v1/transfers/payees",
        json={
            "full_name": "Test Beneficiary",
            "rib": "000000000000000000000000",
        },
    )
    assert resp.status_code == 400
    assert "Modulo 97" in resp.json()["detail"]


def test_transfers_execute_requires_mfa():
    # Valid Modulo 97 Moroccan RIB: 051 780 0000000000000000 37
    # bank=051, branch=780, account=0, key=37
    # 89*51 + 15*780 + 3*0 = 4539 + 11700 = 16239 % 97 = 40; 97 - 40 = 57 -> key 57
    valid_rib = "051780000000000000000057"

    # mfa_confirmed = False should be rejected
    resp = client.post(
        "/api/v1/transfers/execute",
        json={
            "source_rib": valid_rib,
            "beneficiary_rib": valid_rib,
            "amount_mad": 500.0,
            "mfa_confirmed": False,
        },
    )
    assert resp.status_code == 403


def test_impayes_endpoint():
    with patch.object(mf_client, "invoke_service", return_value={"unpaid_operations": []}):
        resp = client.get("/api/v1/transfers/impayes")
        assert resp.status_code == 200
        assert "unpaid_operations" in resp.json()


# ---------------------------------------------------------------------------
# PFM Mutation & Evolution Tests
# ---------------------------------------------------------------------------

def test_pfm_evolution():
    with patch.object(mf_client, "invoke_service", return_value={"chart": []}):
        resp = client.get("/api/v1/pfm/evolution")
        assert resp.status_code == 200
        assert "cash_evolution" in resp.json()


def test_pfm_category_patch():
    with patch.object(mf_client, "invoke_service", return_value={"updated": True}):
        resp = client.patch(
            "/api/v1/pfm/transactions/TX-999/category",
            json={"category_code": "restaurant"},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] is True


# ---------------------------------------------------------------------------
# Communications & Dynamics CRM Tests
# ---------------------------------------------------------------------------

def test_comms_inbox_and_incidents():
    with patch.object(mf_client, "invoke_service", return_value={"messages": []}):
        resp = client.get("/api/v1/comms/inbox")
        assert resp.status_code == 200

    with patch.object(mf_client, "invoke_service", return_value={"incidentId": "INC-12345"}):
        resp = client.post(
            "/api/v1/comms/incidents",
            json={
                "subject": "Carte bloquée au GAB",
                "description": "Erreur lors du retrait au GAB Casablanca Centre",
            },
        )
        assert resp.status_code == 200
        assert "incident" in resp.json()


# ---------------------------------------------------------------------------
# Agency & Market Data Tests
# ---------------------------------------------------------------------------

def test_agency_market_rates_and_locations():
    with patch.object(mf_client, "invoke_service", return_value={"eur_mad": 10.85}):
        resp = client.get("/api/v1/agency/market-rates")
        assert resp.status_code == 200

    with patch.object(mf_client, "invoke_service", return_value=[{"id": "branch-001"}]):
        resp = client.get("/api/v1/agency/locations?cash_deposit_enabled=true")
        assert resp.status_code == 200
        assert "branches" in resp.json()
        assert "atms" in resp.json()


# ---------------------------------------------------------------------------
# Berlin Group NextGenPSD2 AIS & PIS Tests
# ---------------------------------------------------------------------------

def test_berlin_group_balances():
    resp = client.get("/api/v1/automation/open-banking/accounts/acc-001/balances")
    assert resp.status_code == 200
    data = resp.json()
    assert "balances" in data
    balance_types = [b["balanceType"] for b in data["balances"]]
    assert "closingBooked" in balance_types
    assert "expected" in balance_types


def test_berlin_group_transactions():
    resp = client.get("/api/v1/automation/open-banking/accounts/acc-001/transactions")
    assert resp.status_code == 200
    data = resp.json()
    assert "transactions" in data
    assert "booked" in data["transactions"]


def test_berlin_group_sepa_payment_initiation():
    valid_rib = "051780000000000000000057"
    payload = {
        "instructedAmount": {"currency": "MAD", "amount": "2500.00"},
        "debtorAccount": {"bban": valid_rib},
        "creditorAccount": {"bban": valid_rib},
        "creditorName": "Fournisseur Casablanca",
        "remittanceInformationUnstructured": "Paiement facture 2026-F10",
    }
    resp = client.post(
        "/api/v1/automation/open-banking/payments/sepa-credit-transfers",
        json=payload,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["transactionStatus"] == "RCVD"
    assert "_links" in data
    assert "scaOAuth" in data["_links"]
