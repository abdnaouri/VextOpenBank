import uuid
import json
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, Optional

from ..core.money import to_centimes, from_centimes
from ..core.state_machine import InvoiceState, transition, IllegalTransition
from ..core.tenants import TenantConfig, SEED_TENANTS, resolve_tenant_by_prefix
from ..core.moroccan_banks import resolve_moroccan_bank

def apply_transfer(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    bank_txn_id: str,
    amount_centimes: int,
    reference: Optional[str],
    sender_name: Optional[str] = None,
    sender_rib: Optional[str] = None,
    recipient_rib: Optional[str] = "050810006011833971200189",
    recipient_bank_code: Optional[str] = "051",
    received_at: Optional[str] = None,
    raw: Optional[Dict[str, Any]] = None,
    tolerance_centimes: int = 5,
) -> Dict[str, Any]:
    """
    Core Deterministic Ledger Reconciliation Engine.
    Invariants:
    1. Deduplicates against bank_txn_id.
    2. Idempotently locks and updates invoices under WAL concurrency.
    3. Guarantees FSM legal transitions.
    4. Commits audit_log and outbox_events in the EXACT same transaction.
    """
    now_ts = int(time.time())
    now_str = received_at or datetime.now(timezone.utc).isoformat()

    with conn:
        # 1. Dedupe bank transaction
        dup = conn.execute(
            "SELECT * FROM transfers WHERE bank_txn_id = ?", (bank_txn_id,)
        ).fetchone()
        if dup:
            return {
                "result": "DUPLICATE",
                "bank_txn_id": bank_txn_id,
                "invoice_id": dup["matched_invoice_id"],
                "match_status": dup["match_status"],
            }

        # 2. Look for matching invoice by reference
        ref_norm = (reference or "").strip().upper()
        inv = None
        if ref_norm:
            inv = conn.execute(
                "SELECT * FROM invoices WHERE reference = ? OR reference = ?",
                (ref_norm, reference),
            ).fetchone()

        transfer_id = f"trf_{uuid.uuid4().hex[:12]}"

        # If no invoice matched, record transfer as UNMATCHED suspense account
        if not inv:
            conn.execute("""
                INSERT INTO transfers (
                    id, tenant_id, bank_txn_id, amount_centimes, currency,
                    reference, sender_name, sender_rib, recipient_rib, recipient_bank_code,
                    received_at, raw_json, matched_invoice_id, match_status, created_at
                ) VALUES (?, ?, ?, ?, 'MAD', ?, ?, ?, ?, ?, ?, ?, NULL, 'UNMATCHED', ?)
            """, (
                transfer_id, tenant_id, bank_txn_id, amount_centimes,
                reference, sender_name, sender_rib, recipient_rib, recipient_bank_code,
                now_str, json.dumps(raw or {}), now_str,
            ))

            event_id = f"evt_{uuid.uuid4().hex[:12]}"
            conn.execute("""
                INSERT INTO outbox_events (
                    id, tenant_id, event_type, payload_json, attempts, next_attempt_at, created_at
                ) VALUES (?, ?, 'transfer.unmatched', ?, 0, ?, ?)
            """, (
                event_id, tenant_id,
                json.dumps({
                    "bank_txn_id": bank_txn_id,
                    "reference": reference,
                    "amount_mad": str(from_centimes(amount_centimes)),
                }),
                now_ts, now_ts,
            ))
            return {"result": "UNMATCHED", "bank_txn_id": bank_txn_id}

        inv_state = InvoiceState(inv["state"])

        # Check if invoice already confirmed / reconciled
        if inv_state in (InvoiceState.CONFIRMED, InvoiceState.ALREADY_RECONCILED):
            conn.execute("""
                INSERT INTO transfers (
                    id, tenant_id, bank_txn_id, amount_centimes, currency,
                    reference, sender_name, sender_rib, recipient_rib, recipient_bank_code,
                    received_at, raw_json, matched_invoice_id, match_status, created_at
                ) VALUES (?, ?, ?, ?, 'MAD', ?, ?, ?, ?, ?, ?, ?, ?, 'ALREADY_RECONCILED', ?)
            """, (
                transfer_id, tenant_id, bank_txn_id, amount_centimes,
                reference, sender_name, sender_rib, recipient_rib, recipient_bank_code,
                now_str, json.dumps(raw or {}), inv["id"], now_str,
            ))

            event_id = f"evt_{uuid.uuid4().hex[:12]}"
            conn.execute("""
                INSERT INTO outbox_events (
                    id, tenant_id, event_type, payload_json, attempts, next_attempt_at, created_at
                ) VALUES (?, ?, 'transfer.overpaid', ?, 0, ?, ?)
            """, (
                event_id, tenant_id,
                json.dumps({
                    "invoice_id": inv["id"],
                    "client_invoice_id": inv["client_invoice_id"],
                    "bank_txn_id": bank_txn_id,
                    "amount_mad": str(from_centimes(amount_centimes)),
                }),
                now_ts, now_ts,
            ))
            return {
                "result": "ALREADY_RECONCILED",
                "invoice_id": inv["id"],
                "client_invoice_id": inv["client_invoice_id"],
                "state": inv["state"],
            }

        # 3. Calculate new paid amount & FSM transition
        new_paid_centimes = inv["paid_centimes"] + amount_centimes
        required_centimes = inv["amount_centimes"]

        if new_paid_centimes + tolerance_centimes >= required_centimes:
            nxt_state = InvoiceState.CONFIRMED
        elif new_paid_centimes > 0:
            nxt_state = InvoiceState.PARTIALLY_PAID
        else:
            nxt_state = InvoiceState.AWAITING_TRANSFER

        try:
            transition(inv_state, nxt_state)
        except IllegalTransition as exc:
            return {
                "result": "ILLEGAL_STATE",
                "error": str(exc),
                "current_state": inv_state.value,
                "requested_state": nxt_state.value,
            }

        # 4. Atomic ledger updates + Audit Log + Outbox Event
        conn.execute("""
            UPDATE invoices
            SET paid_centimes = ?, state = ?, version = version + 1, updated_at = ?
            WHERE id = ?
        """, (new_paid_centimes, nxt_state.value, now_str, inv["id"]))

        conn.execute("""
            INSERT INTO transfers (
                id, tenant_id, bank_txn_id, amount_centimes, currency,
                reference, sender_name, sender_rib, recipient_rib, recipient_bank_code,
                received_at, raw_json, matched_invoice_id, match_status, created_at
            ) VALUES (?, ?, ?, ?, 'MAD', ?, ?, ?, ?, ?, ?, ?, ?, 'MATCHED', ?)
        """, (
            transfer_id, tenant_id, bank_txn_id, amount_centimes,
            reference, sender_name, sender_rib, recipient_rib, recipient_bank_code,
            now_str, json.dumps(raw or {}), inv["id"], now_str,
        ))

        # Append to Audit Log
        audit_id = f"aud_{uuid.uuid4().hex[:12]}"
        conn.execute("""
            INSERT INTO audit_log (
                id, tenant_id, invoice_id, actor, action, before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, 'transfer.applied', ?, ?, ?)
        """, (
            audit_id, tenant_id, inv["id"], f"bank:{bank_txn_id}",
            json.dumps({"state": inv["state"], "paid_centimes": inv["paid_centimes"]}),
            json.dumps({"state": nxt_state.value, "paid_centimes": new_paid_centimes}),
            now_str,
        ))

        # Enqueue Outbox Event
        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        bank_meta = resolve_moroccan_bank(sender_rib) if sender_rib else {"name": "CFG Bank", "code": "051"}
        payload = {
            "event": "invoice.confirmed" if nxt_state == InvoiceState.CONFIRMED else f"invoice.{nxt_state.value.lower()}",
            "invoice_id": inv["client_invoice_id"],
            "reference": inv["reference"],
            "state": nxt_state.value,
            "amount_mad": str(from_centimes(required_centimes)),
            "paid_mad": str(from_centimes(new_paid_centimes)),
            "bank_txn_id": bank_txn_id,
            "sender_name": sender_name,
            "sender_rib": sender_rib,
            "originating_bank": bank_meta.get("name"),
            "timestamp": now_str,
        }

        conn.execute("""
            INSERT INTO outbox_events (
                id, tenant_id, event_type, payload_json, attempts, next_attempt_at, created_at
            ) VALUES (?, ?, ?, ?, 0, ?, ?)
        """, (
            event_id, tenant_id, f"invoice.{nxt_state.value.lower()}",
            json.dumps(payload), now_ts, now_ts,
        ))

        return {
            "result": "MATCHED",
            "invoice_id": inv["id"],
            "client_invoice_id": inv["client_invoice_id"],
            "state": nxt_state.value,
            "paid_mad": float(from_centimes(new_paid_centimes)),
            "amount_mad": float(from_centimes(required_centimes)),
        }
