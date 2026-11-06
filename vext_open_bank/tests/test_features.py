"""
Automated Pytest Suite for VextOpenBank Advanced & Unexposed Features
"""

import pytest
from fastapi.testclient import TestClient
from vext_open_bank.main import app, ORIGIN_SHIELD_SECRET

client = TestClient(app, headers={"x-origin-shield": ORIGIN_SHIELD_SECRET})

def test_root_and_dashboard():
    res = client.get("/")
    assert res.status_code == 200
    assert "dashboard_ui" in res.json()

    res_app = client.get("/app")
    assert res_app.status_code == 200
    assert "VextOpenBank" in res_app.text

def test_dotations():
    res = client.get("/api/v1/features/dotations/status")
    assert res.status_code == 200
    data = res.json()
    assert "tourist_endowment" in data
    assert data["tourist_endowment"]["total_quota_mad"] == 100000.0

    alloc = client.post("/api/v1/features/dotations/allocate", json={
        "account_rib": "051780000000123456789001",
        "dotation_type": "touristique",
        "beneficiary_code": "cfg$endowment_type_me",
        "amount_mad": 5000.0
    })
    assert alloc.status_code == 200
    assert alloc.json()["status"] == "SUCCESS"

def test_fatourati_bills():
    cat = client.get("/api/v1/features/bills/catalog")
    assert cat.status_code == 200
    assert cat.json()["total_billers"] >= 10

    inq = client.post("/api/v1/features/bills/inquire", json={
        "biller_code": "LYDEC_CASA",
        "subscriber_reference": "REF_1234"
    })
    assert inq.status_code == 200
    assert len(inq.json()["bills"]) > 0

    pay = client.post("/api/v1/features/bills/pay", json={
        "biller_code": "LYDEC_CASA",
        "subscriber_reference": "REF_1234",
        "bill_id": "FAC-001",
        "amount_mad": 248.5,
        "account_rib": "051780000000123456789001"
    })
    assert pay.status_code == 200
    assert pay.json()["status"] == "SETTLED"

def test_cheques():
    order = client.post("/api/v1/features/cheques/order", json={
        "account_rib": "051780000000123456789001",
        "number_of_leaves": 25,
        "stub_position": "talon_a_gauche",
        "delivery_branch_code": "001"
    })
    assert order.status_code == 200
    assert order.json()["status"] == "ORDER_SUBMITTED"

    deposit = client.post("/api/v1/features/cheques/deposit-simulate", json={
        "account_rib": "051780000000123456789001",
        "cheque_number": "7654321",
        "drawee_bank_code": "051",
        "drawee_account": "7800000012345678",
        "amount_mad": 2500.0,
        "has_front_image": True,
        "has_back_image": True
    })
    assert deposit.status_code == 200
    assert "cmc7_micr_line" in deposit.json()

def test_credit_simulation():
    sim = client.post("/api/v1/features/credits/simulate", json={
        "capital_amount_mad": 1000000,
        "annual_rate_percent": 4.5,
        "duration_months": 240,
        "insurance_rate_percent": 0.35
    })
    assert sim.status_code == 200
    data = sim.json()
    assert data["monthly_payment_ex_insurance_mad"] > 0
    assert data["total_monthly_payment_mad"] > data["monthly_payment_ex_insurance_mad"]

def test_mwallet_and_standing_orders():
    w = client.get("/api/v1/features/wallet/status")
    assert w.status_code == 200
    assert w.json()["hps_switch_connected"] is True

    freq = client.get("/api/v1/features/standing-orders/frequencies")
    assert freq.status_code == 200
    assert len(freq.json()["frequencies"]) == 5


def test_fatourati_bills_underpayment_and_centime_tolerance():
    # 1. Underpayment: Paying 10.00 MAD on a 248.50 MAD bill -> 400 Bad Request
    underpay = client.post("/api/v1/features/bills/pay", json={
        "biller_code": "LYDEC_CASA",
        "subscriber_reference": "REF_1234",
        "bill_id": "FAC-001",
        "amount_mad": 10.0,
        "account_rib": "051780000000123456789001"
    })
    assert underpay.status_code == 400
    err = underpay.json()["detail"]
    assert err["error"] == "INCOHERENCE_DE_MONTANT"
    assert err["expected_total_ttc"] == 248.5
    assert err["received_amount_mad"] == 10.0
    assert err["missing_amount_mad"] == 238.5
    assert err["variance_mad"] == 238.5
    assert "238.50 MAD" in err["message"]

    # 2. Centime variation within ±0.05 MAD (e.g. 248.53 MAD, variance = +0.03 MAD) -> Accepted (200 OK)
    centime_ok = client.post("/api/v1/features/bills/pay", json={
        "biller_code": "LYDEC_CASA",
        "subscriber_reference": "REF_1234",
        "bill_id": "FAC-001",
        "amount_mad": 248.53,
        "account_rib": "051780000000123456789001"
    })
    assert centime_ok.status_code == 200
    assert centime_ok.json()["status"] == "SETTLED"

    # 3. Overpayment > 0.05 MAD (e.g. 300.00 MAD on 248.50 MAD) -> 400 Bad Request
    overpay = client.post("/api/v1/features/bills/pay", json={
        "biller_code": "LYDEC_CASA",
        "subscriber_reference": "REF_1234",
        "bill_id": "FAC-001",
        "amount_mad": 300.0,
        "account_rib": "051780000000123456789001"
    })
    assert overpay.status_code == 400
    err_over = overpay.json()["detail"]
    assert err_over["error"] == "INCOHERENCE_DE_MONTANT"
    assert err_over["overpaid_amount_mad"] == 51.5
    assert err_over["missing_amount_mad"] == 0.0
