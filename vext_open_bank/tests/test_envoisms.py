"""
test_envoisms.py
Automated Pytest for EnvoiSMS Bank Bridge Reconciliation
"""

import pytest
import time
import uuid
from fastapi.testclient import TestClient
from vext_open_bank.main import app, ORIGIN_SHIELD_SECRET

client = TestClient(app, headers={"x-origin-shield": ORIGIN_SHIELD_SECRET})

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


# ---------------------------------------------------------------------------
# Transactional Outbox tests
# ---------------------------------------------------------------------------

def test_outbox_row_persists_after_reconciliation():
    """
    After a successful reconciliation, a row must exist in webhook_outbox
    regardless of network conditions (written in the same ACID transaction).
    """
    import sqlite3, uuid
    from vext_open_bank.services.envoisms_bridge import EnvoiSMSBridge

    bridge = EnvoiSMSBridge(db_path=f"/tmp/vext_test_{uuid.uuid4().hex}.db")
    run_id = uuid.uuid4().hex[:8]
    topup_id = f"topup_outbox_{run_id}"
    ref = f"ENV-{run_id.upper()}"
    amount = 500.0

    # Record an incoming transfer
    bridge.record_incoming_bank_transfer(
        bank_tx_id=f"CFG-{run_id}",
        amount_mad=amount,
        sender_name="OUTBOX TEST SARL",
        motif=f"Recharge {ref}",
    )

    # Reconcile (skip the async dispatch so we can inspect the DB synchronously)
    success, result = bridge.reconcile_topup(
        topup_id=topup_id,
        account_id="acc_outbox_test",
        amount_mad=amount,
        reference=ref,
        skip_async_trigger=True,
    )

    assert success is True
    assert result["status"] == "CONFIRMED"

    # Outbox row must exist and be PENDING
    with sqlite3.connect(bridge.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM webhook_outbox WHERE idempotency_key = ?",
            (f"vext_reconcile_{topup_id}",),
        ).fetchone()

    assert row is not None, "Outbox row not persisted after reconciliation"
    assert row["status"] == "PENDING"
    assert row["tenant"] == "ENV"
    assert row["attempts"] == 0


def test_reconcile_idempotent_does_not_duplicate_outbox():
    """
    Calling reconcile_topup twice on the same topup_id must not create two
    outbox rows (INSERT OR IGNORE guarantees this).
    """
    import sqlite3, uuid
    from vext_open_bank.services.envoisms_bridge import EnvoiSMSBridge

    bridge = EnvoiSMSBridge(db_path=f"/tmp/vext_test_{uuid.uuid4().hex}.db")
    run_id = uuid.uuid4().hex[:8]
    topup_id = f"topup_idem_{run_id}"
    ref = f"ENV-{run_id.upper()}"
    amount = 750.0

    bridge.record_incoming_bank_transfer(
        bank_tx_id=f"CFG-IDEM-{run_id}",
        amount_mad=amount,
        sender_name="IDEM SARL",
        motif=f"Paiement {ref}",
    )

    bridge.reconcile_topup(topup_id=topup_id, account_id="acc_idem",
                           amount_mad=amount, reference=ref, skip_async_trigger=True)

    # Second call — must return ALREADY_RECONCILED
    success2, result2 = bridge.reconcile_topup(topup_id=topup_id, account_id="acc_idem",
                                                amount_mad=amount, reference=ref,
                                                skip_async_trigger=True)
    assert result2["status"] == "ALREADY_RECONCILED"

    # Only one outbox row
    with sqlite3.connect(bridge.db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM webhook_outbox WHERE idempotency_key = ?",
            (f"vext_reconcile_{topup_id}",),
        ).fetchone()[0]
    assert count == 1, f"Expected 1 outbox row, got {count}"


def test_webhook_signature_format():
    """
    _sign_webhook must produce a header in `t=<ts>,v1=<64-char-hex>` format,
    and verify_webhook_signature must accept it immediately after generation.
    """
    from vext_open_bank.services.envoisms_bridge import EnvoiSMSBridge

    secret = "test_secret_32bytes_long_enough!"
    payload = '{"event":"bank_transfer.confirmed","topup_id":"tp_001"}'

    header, timestamp = EnvoiSMSBridge._sign_webhook(payload, secret)

    # Header format validation
    assert header.startswith("t="), f"Missing t= prefix: {header}"
    assert ",v1=" in header, f"Missing v1= segment: {header}"
    parts = dict(item.split("=", 1) for item in header.split(","))
    assert len(parts["v1"]) == 64, "HMAC-SHA256 hex must be 64 chars"

    # Verification must pass
    valid = EnvoiSMSBridge.verify_webhook_signature(payload, header, secret, tolerance_seconds=300)
    assert valid is True, "Signature verification failed immediately after signing"


def test_webhook_signature_rejects_replay():
    """
    A signature with a timestamp older than 300 s must be rejected.
    """
    import hashlib, hmac as _hmac
    from vext_open_bank.services.envoisms_bridge import EnvoiSMSBridge

    secret = "replay_test_secret"
    payload = '{"event":"bank_transfer.confirmed"}'
    old_ts = str(int(time.time()) - 400)  # 400 s ago — beyond 300 s window
    message = f"{old_ts}.{payload}".encode("utf-8")
    sig = _hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    stale_header = f"t={old_ts},v1={sig}"

    result = EnvoiSMSBridge.verify_webhook_signature(payload, stale_header, secret)
    assert result is False, "Stale signature should be rejected"


def test_outbox_retry_on_network_failure():
    """
    When dispatch_pending_webhooks cannot reach the target_url, the outbox
    row must remain active (RETRYING) and NOT be DELIVERED or DEAD_LETTER
    after the first failed attempt.
    """
    import sqlite3, uuid
    from vext_open_bank.services.envoisms_bridge import EnvoiSMSBridge

    bridge = EnvoiSMSBridge(db_path=f"/tmp/vext_test_{uuid.uuid4().hex}.db")
    run_id = uuid.uuid4().hex[:8]

    # Insert an outbox row pointing to an unreachable URL
    now_ts = int(time.time())
    with sqlite3.connect(bridge.db_path) as conn:
        conn.execute("""
            INSERT INTO webhook_outbox
            (id, tenant, event_type, target_url, payload_json, idempotency_key,
             status, attempts, next_attempt_at, created_at)
            VALUES (?, 'ENV', 'bank_transfer.confirmed',
                    'http://127.0.0.1:19999/unreachable',
                    '{"test":1}', ?, 'PENDING', 0, ?, ?)
        """, (str(uuid.uuid4()), f"idem_net_{run_id}", now_ts - 1, now_ts))
        conn.commit()

    delivered = bridge.dispatch_pending_webhooks()
    assert delivered == 0, "Should deliver 0 on network failure"

    # Row must now be RETRYING (not DEAD_LETTER after just 1 attempt)
    with sqlite3.connect(bridge.db_path) as conn:
        row = conn.execute(
            "SELECT status, attempts FROM webhook_outbox WHERE idempotency_key = ?",
            (f"idem_net_{run_id}",),
        ).fetchone()
    assert row[0] == "RETRYING", f"Expected RETRYING, got {row[0]}"
    assert row[1] == 1


def test_multi_tenant_routing():
    """
    reconcile_topup must route ENV- references to the ENV tenant URL
    and MED- references to the MED tenant URL.
    """
    import sqlite3, uuid
    from vext_open_bank.services.envoisms_bridge import EnvoiSMSBridge, TENANT_WEBHOOK_URLS

    bridge = EnvoiSMSBridge(db_path=f"/tmp/vext_test_{uuid.uuid4().hex}.db")

    for prefix, tenant in [("ENV", "ENV"), ("MED", "MED")]:
        run_id = uuid.uuid4().hex[:8]
        topup_id = f"topup_tenant_{run_id}"
        ref = f"{prefix}-{run_id.upper()}"
        amount = 300.0

        bridge.record_incoming_bank_transfer(
            bank_tx_id=f"CFG-MT-{run_id}",
            amount_mad=amount,
            sender_name="TENANT TEST SARL",
            motif=f"Paiement {ref}",
        )
        bridge.reconcile_topup(topup_id=topup_id, account_id="acc_mt",
                               amount_mad=amount, reference=ref, skip_async_trigger=True)

        with sqlite3.connect(bridge.db_path) as conn:
            row = conn.execute(
                "SELECT tenant, target_url FROM webhook_outbox WHERE idempotency_key = ?",
                (f"vext_reconcile_{topup_id}",),
            ).fetchone()

        assert row is not None, f"No outbox row for {prefix} tenant"
        assert row[0] == tenant, f"Wrong tenant: expected {tenant}, got {row[0]}"
        expected_url = TENANT_WEBHOOK_URLS.get(tenant, "")
        assert row[1] == expected_url, f"Wrong URL for {tenant}: {row[1]}"


def test_envoisms_underpayment_protection():
    """
    If a client attempts to pay 10.00 MAD on an 18,000.00 MAD invoice/topup,
    the system must reject the transaction with HTTP 400 (Incohérence de montant)
    and report the exact missing amount (17,990.00 MAD).
    """
    unique_run = uuid.uuid4().hex[:8]
    topup_id = f"topup_fraud_{unique_run}"
    ref = f"ENV-{unique_run.upper()}"
    invoice_amount = 18000.0
    underpaid_wire = 10.0

    # 1. Transfer arrives on bank ledger with 10.00 MAD but referencing the 18,000 MAD invoice
    sim = client.post("/api/v1/envoisms/simulate-incoming-transfer", json={
        "amount_mad": underpaid_wire,
        "sender_name": "FRAUD SUSPECT SARL",
        "motif": f"Recharge {ref}",
    })
    assert sim.status_code == 200

    # 2. Check topup expecting 18,000 MAD -> Must reject with HTTP 400
    res = client.post("/api/v1/envoisms/check-topup", json={
        "topup_id": topup_id,
        "account_id": "acc_fraud_test",
        "amount_mad": invoice_amount,
        "reference": ref,
        "customer_name": "Fraud Suspect"
    })
    assert res.status_code == 400
    data = res.json()["detail"]
    assert data["status"] == "REJECTED_UNDERPAYMENT"
    assert data["error"] == "INCOHERENCE_DE_MONTANT"
    assert data["expected_amount_mad"] == 18000.0
    assert data["received_amount_mad"] == 10.0
    assert data["missing_amount_mad"] == 17990.0
    assert data["variance_mad"] == 17990.0
    assert "17,990.00 MAD" in data["message"]


def test_envoisms_centime_tolerance_accepted():
    """
    Minor centime rounding variations within ±0.05 MAD must be accepted cleanly.
    e.g. Invoiced 18,000.00 MAD, wired 18,000.03 MAD (+0.03 MAD variance).
    """
    unique_run = uuid.uuid4().hex[:8]
    topup_id = f"topup_centime_{unique_run}"
    ref = f"ENV-{unique_run.upper()}"
    invoice_amount = 18000.0
    wire_amount = 18000.03  # +0.03 MAD centime rounding variation

    # 1. Incoming transfer arrives
    sim = client.post("/api/v1/envoisms/simulate-incoming-transfer", json={
        "amount_mad": wire_amount,
        "sender_name": "CENTIME TEST SARL",
        "motif": f"Recharge {ref}",
    })
    assert sim.status_code == 200

    # 2. Check topup -> Within ±0.05 MAD must confirm (200 OK)
    res = client.post("/api/v1/envoisms/check-topup", json={
        "topup_id": topup_id,
        "account_id": "acc_centime_test",
        "amount_mad": invoice_amount,
        "reference": ref,
        "customer_name": "Centime Test"
    })
    assert res.status_code == 200
    assert res.json()["status"] == "CONFIRMED"


def test_moroccan_interbank_wires_identification():
    """
    Verify that incoming wires from different Moroccan banks (Attijariwafa, CIH, BCP)
    are properly identified and enriched with originating bank metadata.
    """
    bank_test_cases = [
        ("007780000000123456789012", "Attijariwafa Bank"),
        ("181780000000123456789012", "CIH Bank"),
        ("021780000000123456789012", "Banque Centrale Populaire"),
    ]

    for sender_rib, expected_bank in bank_test_cases:
        unique_run = uuid.uuid4().hex[:8]
        topup_id = f"topup_bam_{unique_run}"
        ref = f"ENV-{unique_run.upper()}"
        amount = 550.0

        # Simulate incoming BAM wire
        sim = client.post("/api/v1/envoisms/simulate-incoming-transfer", json={
            "amount_mad": amount,
            "sender_name": "MOROCCAN CORP SARL",
            "motif": f"Recharge {ref}",
            "sender_rib": sender_rib,
        })
        assert sim.status_code == 200

        # Reconcile
        res = client.post("/api/v1/envoisms/check-topup", json={
            "topup_id": topup_id,
            "account_id": "acc_bam_user",
            "amount_mad": amount,
            "reference": ref,
        })
        assert res.status_code == 200
        details = res.json()["details"]
        assert details["originating_bank"] == expected_bank
        assert "BAM Virement Instantané" in details["transfer_channel"]
        assert details["sender_rib"] == sender_rib


def test_messy_bank_app_motif_reconciliation():
    """
    Verify that real-world Moroccan bank app motif pollution
    (e.g. Attijariwafa 'VIR INST EMIS REF: ENV-98421' or space variations 'ENV 98421')
    are cleanly parsed and reconciled without false negatives.
    """
    messy_motifs = [
        "VIR INST EMIS REF: ENV-{ref}",
        "VIREMENT EMIS VERS CFG BANK ENV {ref}",
        "/REF/ENV-{ref}/ROC/",
    ]

    for motif_template in messy_motifs:
        token = uuid.uuid4().hex[:6].upper()
        ref = f"ENV-{token}"
        actual_motif = motif_template.format(ref=token)
        unique_run = uuid.uuid4().hex[:8]
        topup_id = f"topup_messy_{unique_run}"
        amount = 890.0

        # Simulate transfer with polluted motif
        sim = client.post("/api/v1/envoisms/simulate-incoming-transfer", json={
            "amount_mad": amount,
            "sender_name": "CHAABI CLIENT SARL",
            "motif": actual_motif,
            "sender_rib": "021780000000123456789012",
        })
        assert sim.status_code == 200

        # Reconcile with canonical reference
        res = client.post("/api/v1/envoisms/check-topup", json={
            "topup_id": topup_id,
            "account_id": "acc_messy_test",
            "amount_mad": amount,
            "reference": ref,
        })
        assert res.status_code == 200
        assert res.json()["status"] == "CONFIRMED"


def test_multi_wire_cumulative_installments():
    """
    Top 0.01% SaaS capability:
    When a customer hits BAM mobile daily caps (e.g. 20,000 MAD) and splits
    an 18,000.00 MAD invoice into 2 wires (10,000.00 MAD + 8,000.00 MAD),
    the first wire reports partial payment with missing balance,
    and the second wire completes the settlement automatically.
    """
    unique_run = uuid.uuid4().hex[:8]
    topup_id = f"topup_split_{unique_run}"
    ref = f"ENV-{unique_run.upper()}"
    target_amount = 18000.0

    # 1. First wire arrives: 10,000 MAD
    client.post("/api/v1/envoisms/simulate-incoming-transfer", json={
        "amount_mad": 10000.0,
        "sender_name": "MULTI WIRE SARL",
        "motif": f"Part 1 {ref}",
    })

    # Checking topup: must reject with HTTP 400 and report missing balance (8,000 MAD)
    check1 = client.post("/api/v1/envoisms/check-topup", json={
        "topup_id": topup_id,
        "account_id": "acc_split_client",
        "amount_mad": target_amount,
        "reference": ref,
    })
    assert check1.status_code == 400
    err = check1.json()["detail"]
    assert err["error"] == "INCOHERENCE_DE_MONTANT"
    assert err["received_amount_mad"] == 10000.0
    assert err["missing_amount_mad"] == 8000.0
    assert err["installments_count"] == 1

    # 2. Second wire arrives: 8,000 MAD
    client.post("/api/v1/envoisms/simulate-incoming-transfer", json={
        "amount_mad": 8000.0,
        "sender_name": "MULTI WIRE SARL",
        "motif": f"Part 2 {ref}",
    })

    # Checking topup now: 10,000 + 8,000 = 18,000 MAD -> Must confirm!
    check2 = client.post("/api/v1/envoisms/check-topup", json={
        "topup_id": topup_id,
        "account_id": "acc_split_client",
        "amount_mad": target_amount,
        "reference": ref,
    })
    assert check2.status_code == 200
    data = check2.json()
    assert data["status"] == "CONFIRMED"
    assert data["details"]["installments_count"] == 2
    assert len(data["details"]["related_bank_tx_ids"]) == 2


def test_multi_bank_receiving_accounts_instructions():
    """
    Test Smart Treasury Routing for receiving accounts:
    - Default returns primary CFG Bank
    - preferred_bank_code=007 returns Attijariwafa Bank receiving account
    - preferred_bank_code=021 returns BCP receiving account
    - preferred_bank_code=181 returns CIH Bank receiving account
    """
    # 1. Default (CFG Bank)
    cfg_inst = client.get("/api/v1/envoisms/instructions").json()
    assert cfg_inst["bank_code"] == "051"
    assert "CFG Bank" in cfg_inst["bank_name"]
    assert len(cfg_inst["available_receiving_banks"]) >= 4

    # 2. Attijariwafa Bank
    awb_inst = client.get("/api/v1/envoisms/instructions?preferred_bank_code=007").json()
    assert awb_inst["bank_code"] == "007"
    assert awb_inst["bank_name"] == "Attijariwafa Bank"
    assert awb_inst["rib"].startswith("007")

    # 3. List all receiving accounts
    accs = client.get("/api/v1/envoisms/receiving-accounts").json()
    assert accs["total_treasury_accounts"] >= 4


def test_universal_ingestion_and_suspense_claim():
    """
    Test non-CFG bank transfer ingestion and suspense account claim:
    1. Ingest transfer received on Attijariwafa Bank account with no reference
    2. Check that it appears in /unmatched-transfers
    3. Claim it for a specific topup intent
    """
    unique_run = uuid.uuid4().hex[:8]
    custom_tx_id = f"AWB-INTRA-{unique_run}"
    amount = 1450.0

    # 1. Ingest transfer received on Attijariwafa
    ingest = client.post("/api/v1/envoisms/ingest-transfer", json={
        "bank_tx_id": custom_tx_id,
        "amount_mad": amount,
        "sender_name": "CASABLANCA IMPORTS SARL",
        "motif": "VIREMENT COMMERCIAL SANS REF",
        "sender_rib": "007780000000999999999999",
        "recipient_rib": "007780000000123456789012",
        "recipient_bank_code": "007",
    })
    assert ingest.status_code == 200
    assert ingest.json()["status"] == "INGESTED"

    # 2. Check suspense account
    suspense = client.get("/api/v1/envoisms/unmatched-transfers").json()
    matching_tx = next((t for t in suspense["transfers"] if t["bank_tx_id"] == custom_tx_id), None)
    assert matching_tx is not None
    assert matching_tx["amount_mad"] == amount

    # 3. Claim the wire for a client topup intent
    claim = client.post("/api/v1/envoisms/claim-unmatched-wire", json={
        "bank_tx_id": custom_tx_id,
        "topup_id": f"topup_claim_{unique_run}",
        "account_id": "acc_claimed_user",
        "reference": f"ENV-CLAIM-{unique_run.upper()}",
    })
    assert claim.status_code == 200
    assert claim.json()["status"] == "CONFIRMED"


def test_attijari_mt940_statement_ingestion_and_reconciliation():
    """
    Test EBICS / Host-to-Host MT940 statement file ingestion from Attijariwafa Bank.
    Extracts incoming wire with reference and reconciles topup intent automatically.
    """
    unique_run = uuid.uuid4().hex[:8]
    ref = f"ENV-{unique_run.upper()}"
    amount = 18000.0

    mt940_sample = f"""
:20:AWB20261106001
:25:007780000000123456789012
:28C:001/01
:60F:C261101MAD50000,00
:61:2611061106CR18000,00NTRFNONREF//AWB{unique_run}
:86:VIR INST EMIS REF: {ref} / CLIENT CASABLANCA
:62F:C261106MAD68000,00
-"""

    # Ingest statement
    res_parse = client.post("/api/v1/envoisms/parse-bank-statement", json={
        "bank_code": "007",
        "statement_content": mt940_sample,
        "file_format": "MT940",
        "recipient_rib": "007780000000123456789012",
    })
    assert res_parse.status_code == 200
    assert res_parse.json()["transfers_ingested"] >= 1

    # Check topup reconciliation
    res_reconcile = client.post("/api/v1/envoisms/check-topup", json={
        "topup_id": f"topup_mt940_{unique_run}",
        "account_id": "acc_awb_tenant",
        "amount_mad": amount,
        "reference": ref,
    })
    assert res_reconcile.status_code == 200
    assert res_reconcile.json()["status"] == "CONFIRMED"


def test_cdm_camt053_xml_statement_ingestion_and_reconciliation():
    """
    Test ISO 20022 CAMT.053 XML statement file ingestion from Crédit du Maroc (CDM).
    Extracts XML <Ntry> credits and reconciles tenant invoice automatically.
    """
    unique_run = uuid.uuid4().hex[:8]
    ref = f"MED-{unique_run.upper()}"
    amount = 5200.0

    camt053_sample = f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <Stmt>
      <Id>CDM-STMT-20261106</Id>
      <Acct>
        <Id><Othr><Id>078780000000123456789078</Id></Othr></Id>
      </Acct>
      <Ntry>
        <Amt Ccy="MAD">{amount}</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <ValDt><Dt>2026-11-06</Dt></ValDt>
        <NtryDtls>
          <TxDtls>
            <RltdPties>
              <Dbtr><Nm>CLINIQUE RABAT NORD SARL</Nm></Dbtr>
            </RltdPties>
            <RmtInf>
              <Ustrd>REGLEMENT FACTURE {ref} MEDIUNIT</Ustrd>
            </RmtInf>
          </TxDtls>
        </NtryDtls>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>"""

    # Ingest statement
    res_parse = client.post("/api/v1/envoisms/parse-bank-statement", json={
        "bank_code": "078",
        "statement_content": camt053_sample,
        "file_format": "CAMT.053",
        "recipient_rib": "078780000000123456789078",
    })
    assert res_parse.status_code == 200
    assert res_parse.json()["transfers_ingested"] >= 1

    # Check topup reconciliation
    res_reconcile = client.post("/api/v1/envoisms/check-topup", json={
        "topup_id": f"topup_camt_{unique_run}",
        "account_id": "acc_cdm_tenant",
        "amount_mad": amount,
        "reference": ref,
    })
    assert res_reconcile.status_code == 200
    assert res_reconcile.json()["status"] == "CONFIRMED"


def test_tenant_bank_registration():
    """
    Test registering a tenant's proprietary non-CFG bank account (e.g. Attijariwafa or CDM).
    """
    res = client.post("/api/v1/envoisms/register-tenant-bank", json={
        "tenant_id": "NEWTENANT",
        "bank_code": "007",
        "account_rib": "007780000000998877665544",
        "account_name": "New Tenant Attijari Account",
    })
    assert res.status_code == 200
    assert res.json()["status"] == "REGISTERED"

    # Verify presence in configs list
    configs = client.get("/api/v1/envoisms/tenant-bank-configs").json()
    tenant_obj = next((t for t in configs["tenants"] if t["tenant_id"] == "NEWTENANT"), None)
    assert tenant_obj is not None
    assert tenant_obj["bank_code"] == "007"
    assert tenant_obj["rib"] == "007780000000998877665544"
