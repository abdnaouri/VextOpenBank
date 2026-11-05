"""
test_envoisms.py
Automated Pytest for EnvoiSMS Bank Bridge Reconciliation
"""

import pytest
import time
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
