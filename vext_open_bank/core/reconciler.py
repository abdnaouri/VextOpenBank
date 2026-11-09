# vext_open_bank/core/reconciler.py
import hashlib
import json
import time
import uuid
from typing import Any
from vext_open_bank.core.db import Database
from vext_open_bank.core.money import centimes_to_mad_str, extract_canonical_motif
from vext_open_bank.core.ledger import record_treasury_credit_in_tx


def ingest_and_reconcile_transfer(
    db: Database,
    *,
    bank_tx_id: str | None,
    raw_label: str,
    amount_centimes: int,
    sender_name: str = "",
    sender_rib: str = "",
) -> dict[str, Any]:
    """
    Atomically records a bank wire, matches by Motif, updates invoice state,
    and enqueues a guaranteed webhook in the transactional outbox.
    """
    now = int(time.time())
    parsed = extract_canonical_motif(raw_label)
    extracted_motif = parsed[0] if parsed else None

    # Deterministic bank_tx_id fallback if simulated without an explicit bank reference
    canonical_tx_id = bank_tx_id or (
        "BAM-" + hashlib.sha256(f"{raw_label}|{amount_centimes}|{now // 60}".encode()).hexdigest()[:20].upper()
    )

    with db.write_tx() as conn:
        # 1. Deduplicate identical bank transaction IDs (idempotent bank ingestion)
        existing_tx = conn.execute(
            "SELECT bank_tx_id, reconciliation_state, extracted_motif FROM bank_transfers WHERE bank_tx_id = ?",
            (canonical_tx_id,),
        ).fetchone()
        if existing_tx:
            return {
                "duplicate": True,
                "bank_tx_id": existing_tx["bank_tx_id"],
                "motif": existing_tx["extracted_motif"],
                "state": existing_tx["reconciliation_state"],
            }

        invoice = (
            conn.execute("SELECT * FROM invoices WHERE motif = ?", (extracted_motif,)).fetchone()
            if extracted_motif
            else None
        )

        if invoice is None:
            conn.execute(
                """
                INSERT INTO bank_transfers
                (bank_tx_id, raw_label, extracted_motif, amount_centimes, sender_name, sender_rib, reconciliation_state, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, 'ORPHANED', ?)
                """,
                (canonical_tx_id, raw_label, extracted_motif, amount_centimes, sender_name, sender_rib, now),
            )
            return {
                "duplicate": False,
                "bank_tx_id": canonical_tx_id,
                "motif": extracted_motif,
                "state": "ORPHANED",
            }

        # 2. Cumulative centime tracking (handles split wires gracefully)
        new_received = int(invoice["received_centimes"]) + amount_centimes
        expected = int(invoice["expected_centimes"])

        if new_received == expected:
            new_status = "CONFIRMED"
            event_type = "invoice.confirmed"
        elif new_received > expected:
            new_status = "OVERPAID"
            event_type = "invoice.overpaid"
        else:
            new_status = "UNDERPAID"
            event_type = "invoice.underpaid"

        conn.execute(
            """
            INSERT INTO bank_transfers
            (bank_tx_id, raw_label, extracted_motif, amount_centimes, sender_name, sender_rib, reconciliation_state, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (canonical_tx_id, raw_label, extracted_motif, amount_centimes, sender_name, sender_rib, new_status, now),
        )

        conn.execute(
            """
            UPDATE invoices
            SET received_centimes = ?,
                status = ?,
                updated_at = ?,
                confirmed_at = CASE WHEN ? IN ('CONFIRMED', 'OVERPAID') AND confirmed_at IS NULL THEN ? ELSE confirmed_at END
            WHERE motif = ?
            """,
            (new_received, new_status, now, new_status, now, extracted_motif),
        )

        # 3. Record in Treasury Double-Entry Ledger (Immutable accounting record)
        ledger_entry = record_treasury_credit_in_tx(
            conn,
            bank_tx_id=canonical_tx_id,
            motif=extracted_motif,
            tenant=invoice["tenant"],
            raw_label=raw_label,
            sender_name=sender_name,
            sender_rib=sender_rib,
            amount_centimes=amount_centimes,
        )

        # 4. Enqueue webhook into Transactional Outbox in the SAME SQLite transaction
        event_id = f"evt_{uuid.uuid4().hex}"
        webhook_payload = {
            "event_id": event_id,
            "event": event_type,
            "tenant": invoice["tenant"],
            "motif": extracted_motif,
            "customer_ref": invoice["customer_ref"],
            "status": new_status,
            "expected_centimes": expected,
            "received_centimes": new_received,
            "delta_centimes": new_received - expected,
            "amount_mad": centimes_to_mad_str(new_received),
            "bank_tx_id": canonical_tx_id,
            "operation_ref": ledger_entry["operation_ref"],
            "sender_name": sender_name,
            "timestamp": now,
            "metadata": json.loads(invoice["metadata_json"]),
        }

        # Check columns to satisfy legacy schema NOT NULL constraints if present
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(outbox_events)").fetchall()}
        if "tenant_id" in cols:
            conn.execute(
                """
                INSERT INTO outbox_events
                (id, event_id, tenant_id, tenant, event_type, motif, webhook_url, payload_json, status, attempts, next_attempt_at, locked_until, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, 0, ?)
                """,
                (
                    event_id,
                    event_id,
                    invoice["tenant"],
                    invoice["tenant"],
                    event_type,
                    extracted_motif,
                    invoice["webhook_url"],
                    json.dumps(webhook_payload, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO outbox_events
                (event_id, tenant, motif, webhook_url, payload_json, status, attempts, next_attempt_at, locked_until, created_at)
                VALUES (?, ?, ?, ?, ?, 'PENDING', 0, ?, 0, ?)
                """,
                (
                    event_id,
                    invoice["tenant"],
                    extracted_motif,
                    invoice["webhook_url"],
                    json.dumps(webhook_payload, separators=(",", ":")),
                    now,
                    now,
                ),
            )

        return {
            "duplicate": False,
            "bank_tx_id": canonical_tx_id,
            "motif": extracted_motif,
            "state": new_status,
            "expected_centimes": expected,
            "received_centimes": new_received,
            "operation_ref": ledger_entry["operation_ref"],
            "event_id": event_id,
        }
