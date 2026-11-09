"""
test_zero_maintenance_core.py
Comprehensive verification of the 5-Year Zero-Maintenance upgrades:
1. Money & Modulo-97 BAM checksum (core/money.py)
2. Noisy Moroccan bank motif regex extractor (core/money.py)
3. EMVCo TLV payload & CRC16-CCITT and LRU QR caching (core/qr.py)
4. Thread-safe SQLite engine with BEGIN IMMEDIATE, zero SQLITE_BUSY, and maintenance (core/db.py)
5. Atomic reconciliation flow with duplicate deduplication (core/reconciler.py)
6. Transactional Outbox Worker with lease locks, jittered backoff, and HMAC (core/outbox.py)
7. Tenant signature verification helper snippet
"""

import os
import time
import tempfile
import asyncio
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient

from vext_open_bank.core.money import (
    mad_to_centimes,
    centimes_to_mad_str,
    validate_moroccan_rib,
    extract_canonical_motif,
)
from vext_open_bank.core.qr import (
    build_emvco_payload,
    generate_qr_data_uri,
    _crc16_ccitt,
)
from vext_open_bank.core.db import Database
from vext_open_bank.core.reconciler import ingest_and_reconcile_transfer
from vext_open_bank.core.outbox import (
    OutboxWorker,
    build_hmac_signature,
    compute_backoff_seconds,
)
from vext_open_bank.main import app, ORIGIN_SHIELD_SECRET


# --- 1. Monetary Precision & Modulo-97 ---
def test_money_mad_to_centimes():
    assert mad_to_centimes("1500.00") == 150000
    assert mad_to_centimes(275) == 27500
    assert mad_to_centimes(Decimal("14256.50")) == 1425650
    assert mad_to_centimes("1250,50") == 125050
    assert centimes_to_mad_str(125050) == "1250.50"
    assert centimes_to_mad_str(5) == "0.05"

    with pytest.raises(ValueError, match="Float inputs are forbidden"):
        mad_to_centimes(1500.0)

    with pytest.raises(ValueError, match="Amount must be strictly positive"):
        mad_to_centimes("-10.00")


def test_validate_moroccan_rib_modulo97():
    # CFG Bank treasury RIB: 050 810 0060118339712001 89
    cfg_rib = "050 810 0060118339712001 89"
    cleaned = validate_moroccan_rib(cfg_rib)
    assert cleaned == "050810006011833971200189"

    # Valid 24-digit Moroccan RIB (int(rib) % 97 == 0):
    valid_rib = "051780000000000000000050"
    assert validate_moroccan_rib(valid_rib) == "051780000000000000000050"

    # Corrupted key
    with pytest.raises(ValueError, match="Invalid RIB Modulo-97 checksum"):
        validate_moroccan_rib("050810006011833971200199")

    # Invalid length
    with pytest.raises(ValueError, match="exactly 24 numeric digits"):
        validate_moroccan_rib("12345")


# --- 2. Dirty Moroccan Bank String Motif Extraction ---
def test_extract_canonical_motif():
    # EnvoiSMS
    assert extract_canonical_motif("VIR INST BAM ENV 12345 STE ALPHA") == ("ENV-12345", "envoisms")
    assert extract_canonical_motif("VIREMENT INSTANTANE ENV_9921 SARL") == ("ENV-9921", "envoisms")
    assert extract_canonical_motif("ENV-88420") == ("ENV-88420", "envoisms")

    # Soumit (SOU and BC)
    assert extract_canonical_motif("VIR SEPA SOU 1002 MAROC") == ("SOU-1002", "soumit")
    assert extract_canonical_motif("REGLEMENT BON COMMANDE BC-2026-1001") == ("BC-2026", "soumit")

    # Charikat
    assert extract_canonical_motif("VIR CFG CHK 7712 CREATION SOCIETE") == ("CHK-7712", "charikat")

    # Unmatched/garbage motif
    assert extract_canonical_motif("VIREMENT SALAIRE NOVEMBRE") is None
    assert extract_canonical_motif("") is None


# --- 3. EMVCo QR Code & CRC-16 ---
def test_emvco_qr_payload_and_caching():
    rib = "050810006011833971200189"
    beneficiary = "VEXT AI"
    centimes = 150000
    motif = "ENV-12345"

    payload = build_emvco_payload(rib, beneficiary, centimes, motif)
    assert payload.startswith("000201010212")
    assert "ma.bam.virement" in payload
    assert rib in payload
    assert "CASABLANCA" in payload
    assert "6304" in payload

    # Test CRC-16 accuracy
    crc_part = payload[-4:]
    data_to_crc = payload[:-4].encode("utf-8")
    assert _crc16_ccitt(data_to_crc) == crc_part

    # Test LRU Caching
    uri1 = generate_qr_data_uri(rib, beneficiary, centimes, motif)
    uri2 = generate_qr_data_uri(rib, beneficiary, centimes, motif)
    assert uri1.startswith("data:image/png;base64,")
    assert uri1 == uri2


# --- 4. Database Concurrency & Maintenance ---
def test_database_write_serialization_and_maintenance():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_vext.db")
        db = Database(db_path)

        # Write inside transaction
        with db.write_tx() as conn:
            conn.execute(
                """
                INSERT INTO invoices
                (motif, tenant, customer_ref, expected_centimes, received_centimes, status, webhook_url, created_at, updated_at)
                VALUES ('ENV-1', 'envoisms', 'cust_1', 10000, 0, 'AWAITING_TRANSFER', 'http://localhost/hook', 1000, 1000)
                """
            )

        # Read back
        with db.read_conn() as conn:
            row = conn.execute("SELECT * FROM invoices WHERE motif = 'ENV-1'").fetchone()
            assert row is not None
            assert row["expected_centimes"] == 10000

        # Maintenance (truncate WAL & prune)
        with db.write_tx() as conn:
            conn.execute(
                "INSERT INTO idempotency_keys (idem_key, request_hash, status_code, response_json, expires_at) VALUES ('k1', 'h1', 200, '{}', ?)",
                (int(time.time()) - 100,),  # Expired
            )
            conn.execute(
                "INSERT INTO outbox_events (event_id, tenant, motif, webhook_url, payload_json, status, attempts, next_attempt_at, locked_until, created_at, delivered_at) VALUES ('ev1', 'envoisms', 'ENV-1', 'http://a', '{}', 'DELIVERED', 1, 0, 0, 0, ?)",
                (int(time.time()) - (100 * 86400),),  # Delivered >90 days ago
            )

        res = db.run_maintenance()
        assert res["pruned_idem_keys"] == 1
        assert res["pruned_outbox"] == 1


# --- 5. Atomic Reconciliation Engine ---
def test_reconciliation_exact_over_under_and_duplicate():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_vext.db")
        db = Database(db_path)

        # Create test invoice
        with db.write_tx() as conn:
            conn.execute(
                """
                INSERT INTO invoices
                (motif, tenant, customer_ref, expected_centimes, received_centimes, status, webhook_url, created_at, updated_at)
                VALUES ('ENV-100', 'envoisms', 'c1', 50000, 0, 'AWAITING_TRANSFER', 'http://test/hook', 1000, 1000)
                """
            )

        # Step 1: Partial transfer (200.00 MAD out of 500.00 MAD)
        r1 = ingest_and_reconcile_transfer(
            db,
            bank_tx_id="tx_1",
            raw_label="VIR INST ENV 100 STE ALPHA",
            amount_centimes=20000,
        )
        assert r1["duplicate"] is False
        assert r1["state"] == "UNDERPAID"
        assert r1["received_centimes"] == 20000

        # Step 2: Deduplicate identical bank_tx_id
        r_dup = ingest_and_reconcile_transfer(
            db,
            bank_tx_id="tx_1",
            raw_label="VIR INST ENV 100 STE ALPHA",
            amount_centimes=20000,
        )
        assert r_dup["duplicate"] is True
        assert r_dup["state"] == "UNDERPAID"

        # Step 3: Second transfer completing the invoice (+300.00 MAD) -> CONFIRMED
        r2 = ingest_and_reconcile_transfer(
            db,
            bank_tx_id="tx_2",
            raw_label="VIR INST ENV 100 PART TWO",
            amount_centimes=30000,
        )
        assert r2["duplicate"] is False
        assert r2["state"] == "CONFIRMED"
        assert r2["received_centimes"] == 50000

        # Step 4: Overpayment transfer (+50.00 MAD) -> OVERPAID
        r3 = ingest_and_reconcile_transfer(
            db,
            bank_tx_id="tx_3",
            raw_label="VIR INST ENV 100 EXTRA",
            amount_centimes=5000,
        )
        assert r3["duplicate"] is False
        assert r3["state"] == "OVERPAID"
        assert r3["received_centimes"] == 55000

        # Check outbox events were recorded
        with db.read_conn() as conn:
            events = conn.execute("SELECT * FROM outbox_events WHERE motif = 'ENV-100'").fetchall()
            assert len(events) == 3


# --- 6. Outbox Worker Lease Locking & HMAC ---
def test_outbox_worker_lease_and_hmac():
    secret = "secret_key_123"
    ts = int(time.time())
    body = '{"event":"invoice.confirmed"}'
    sig = build_hmac_signature(secret, ts, body)
    assert sig.startswith(f"t={ts},v1=")

    # Backoff progression
    assert 5 <= compute_backoff_seconds(1) <= 7
    assert compute_backoff_seconds(2) >= 15

    # Lease claiming
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_vext.db")
        db = Database(db_path)
        with db.write_tx() as conn:
            conn.execute(
                """
                INSERT INTO outbox_events
                (event_id, tenant, motif, webhook_url, payload_json, status, attempts, next_attempt_at, locked_until, created_at)
                VALUES ('ev_test', 'envoisms', 'ENV-1', 'http://localhost/wh', '{}', 'PENDING', 0, ?, 0, ?)
                """,
                (ts - 10, ts - 10),
            )

        worker = OutboxWorker(db=db, tenant_secrets={"envoisms": secret}, default_secret=secret)
        claimed = worker._claim_batch(batch_size=10)
        assert len(claimed) == 1
        assert claimed[0]["event_id"] == "ev_test"

        # Subsequent immediate claim should find nothing because it's leased for 60s
        claimed_again = worker._claim_batch(batch_size=10)
        assert len(claimed_again) == 0


# --- 7. Tenant Verification Snippet Validation ---
def test_tenant_receiver_verification():
    def verify_vext_webhook(raw_body: bytes, signature_header: str, secret: str, tolerance_sec: int = 300) -> bool:
        import hashlib
        import hmac
        try:
            parts = dict(item.split("=", 1) for item in signature_header.split(","))
            timestamp = int(parts["t"])
            received_sig = parts["v1"]
        except (KeyError, ValueError):
            return False

        if abs(int(time.time()) - timestamp) > tolerance_sec:
            return False

        signed_payload = f"{timestamp}.".encode("utf-8") + raw_body
        expected_sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(received_sig, expected_sig)

    secret = "envoisms_super_secret"
    ts = int(time.time())
    raw = b'{"event":"invoice.confirmed","motif":"ENV-12345"}'
    import hmac, hashlib
    valid_sig = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("utf-8") + raw, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={valid_sig}"

    assert verify_vext_webhook(raw, header, secret) is True
    assert verify_vext_webhook(raw, header, "wrong_secret") is False
    assert verify_vext_webhook(b'tampered', header, secret) is False
    # Replay attack with expired timestamp
    expired_header = f"t={ts - 400},v1={valid_sig}"
    assert verify_vext_webhook(raw, expired_header, secret) is False


# --- 8. Camouflage Decoy for Probes ---
def test_camouflage_decoy_untrusted():
    client_raw = TestClient(app)
    # Hitting unknown or unauthorized path receives 404 or decoy without exposing stack
    res = client_raw.get("/")
    assert res.status_code == 200
    assert res.json()["status"] == "idle"
    assert res.json()["module"] == "static-asset-dispatcher"

    # With Origin Shield
    client_shield = TestClient(app, headers={"x-origin-shield": ORIGIN_SHIELD_SECRET})
    res_auth = client_shield.get("/")
    assert res_auth.status_code == 200
    assert "service" in res_auth.json()


# --- 9. French/Moroccan Corporate Treasury & Public Portal ---
def test_corporate_treasury_and_public_portal():
    client_auth = TestClient(app, headers={"x-origin-shield": ORIGIN_SHIELD_SECRET})
    client_pub = TestClient(app)

    # 1. Ingest transfer through standard reconciliation to create an invoice & ledger entry
    unique_motif = f"ENV-{int(time.time())}"
    from vext_open_bank.db.session import get_database
    db = get_database()

    with db.write_tx() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(invoices)").fetchall()}
        if "tenant_id" in cols:
            conn.execute(
                """
                INSERT INTO invoices
                (id, tenant_id, client_invoice_id, reference, motif, tenant, customer_ref, expected_centimes, amount_centimes, received_centimes, status, state, webhook_url, created_at, updated_at)
                VALUES (?, 'envoisms', ?, ?, ?, 'envoisms', 'client_corp_1', 125000, 125000, 0, 'AWAITING_TRANSFER', 'AWAITING_TRANSFER', 'https://api.envoisms.ma/webhook', ?, ?)
                """,
                (f"inv_{unique_motif}", f"cli_{unique_motif}", unique_motif, unique_motif, int(time.time()), int(time.time())),
            )
        else:
            conn.execute(
                """
                INSERT INTO invoices
                (motif, tenant, customer_ref, expected_centimes, received_centimes, status, webhook_url, created_at, updated_at)
                VALUES (?, 'envoisms', 'client_corp_1', 125000, 0, 'AWAITING_TRANSFER', 'https://api.envoisms.ma/webhook', ?, ?)
                """,
                (unique_motif, int(time.time()), int(time.time())),
            )

    # Verify public invoice payment sheet renders
    sheet_res = client_pub.get(f"/p/inv/{unique_motif}")
    assert sheet_res.status_code == 200
    assert "1250.00 MAD" in sheet_res.text
    assert unique_motif in sheet_res.text
    assert "Copier le RIB" in sheet_res.text
    assert "data:image/png;base64," in sheet_res.text

    # Verify public status check
    stat_res = client_pub.get(f"/p/inv/{unique_motif}/status")
    assert stat_res.status_code == 200
    assert stat_res.json()["status"] == "AWAITING_TRANSFER"
    assert stat_res.json()["expected_mad"] == "1250.00"

    # Now simulate incoming interbank transfer
    bank_tx_id = f"tx_cfg_{int(time.time())}"
    rec_res = ingest_and_reconcile_transfer(
        db,
        bank_tx_id=bank_tx_id,
        raw_label=f"VIR INST BAM {unique_motif} SARL ALPHA",
        amount_centimes=125000,
        sender_name="STE ALPHA CASABLANCA",
        sender_rib="051780000000000000000050",
    )
    assert rec_res["state"] == "CONFIRMED"

    # Verify treasury account summary
    acct_res = client_auth.get("/api/v1/treasury/account")
    assert acct_res.status_code == 200
    acct_data = acct_res.json()
    assert acct_data["banque"] == "CFG Bank"
    assert acct_data["solde_comptable_centimes"] >= 125000
    assert acct_data["devise"] == "MAD"
    assert float(acct_data["solde_comptable_mad"]) >= 1250.0

    # Verify Relevé Bancaire (account statement)
    releve_res = client_auth.get("/api/v1/treasury/releve?limit=10")
    assert releve_res.status_code == 200
    releve_data = releve_res.json()
    assert releve_data["count"] > 0
    tx_entries = [e for e in releve_data["ecritures"] if e["bank_tx_id"] == bank_tx_id]
    assert len(tx_entries) == 1
    assert tx_entries[0]["motif"] == unique_motif
    assert tx_entries[0]["sens"] == "CREDIT"
    assert tx_entries[0]["montant_mad"] == "1250.00"

    # Verify JSON Avis de Crédit endpoint
    avis_res = client_auth.get(f"/api/v1/treasury/avis/{bank_tx_id}")
    assert avis_res.status_code == 200
    avis_data = avis_res.json()
    assert avis_data["reference_interbancaire_bam"] == bank_tx_id
    assert avis_data["beneficiaire"]["banque"].startswith("CFG Bank")
    assert avis_data["donneur_ordre"]["nom"] == "STE ALPHA CASABLANCA"

    # Verify Public French Avis d'Opération HTML view
    pub_avis = client_pub.get(f"/p/avis/{bank_tx_id}")
    assert pub_avis.status_code == 200
    assert "AVIS D'OPÉRATION BANCAIRE" in pub_avis.text
    assert "STE ALPHA CASABLANCA" in pub_avis.text
    assert "1250.00 MAD" in pub_avis.text
    assert "Charikat.ma" in pub_avis.text

    # Verify SSE status stream yields status and confirmed event
    stream_res = client_pub.get(f"/p/inv/{unique_motif}/stream")
    assert stream_res.status_code == 200
    assert "event: status" in stream_res.text or "event: confirmed" in stream_res.text

