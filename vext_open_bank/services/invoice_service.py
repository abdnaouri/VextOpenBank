import uuid
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, Optional

from ..db.session import get_connection
from ..core.money import to_centimes, from_centimes
from ..core.state_machine import InvoiceState, API_STATUS
from ..core.tenants import TenantConfig

def next_reference(conn: sqlite3.Connection, tenant_prefix: str, year: int) -> str:
    """Generate safe, monotonically increasing reference per (prefix, year)."""
    with conn:
        conn.execute("""
            INSERT INTO invoice_seq (prefix, year, n) VALUES (?, ?, 1)
            ON CONFLICT(prefix, year) DO UPDATE SET n = invoice_seq.n + 1
        """, (tenant_prefix, year))
        row = conn.execute(
            "SELECT n FROM invoice_seq WHERE prefix = ? AND year = ?",
            (tenant_prefix, year),
        ).fetchone()
        n = row["n"] if row else 1
    return f"{tenant_prefix}-{year}-{n:04d}"

def upsert_and_check(
    conn: sqlite3.Connection,
    tenant: TenantConfig,
    client_invoice_id: str,
    reference: str,
    amount_mad: Decimal,
    customer_name: Optional[str] = None,
    instructions_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Atomic Upsert & Check for universal invoice.
    Deterministic: If invoice does not exist, inserts as AWAITING_TRANSFER.
    Returns standard API contract with BigInt centime precision and schema version.
    """
    amount_c = to_centimes(amount_mad)
    now_str = datetime.now(timezone.utc).isoformat()

    with conn:
        row = conn.execute(
            "SELECT * FROM invoices WHERE tenant_id = ? AND reference = ?",
            (tenant.id, reference),
        ).fetchone()

        if row is None:
            inv_id = f"inv_{uuid.uuid4().hex[:12]}"
            instructions_data = instructions_payload or {
                "reference": reference,
                "amount_mad": str(from_centimes(amount_c)),
                "beneficiary": "VEXT AI",
                "rib": "050810006011833971200189",
                "bank": "CFG Bank",
            }
            # Dual-populate legacy columns and 5-year core columns
            conn.execute("""
                INSERT INTO invoices (
                    id, tenant_id, client_invoice_id, reference, customer_name,
                    amount_centimes, subtotal_centimes, tax_centimes, paid_centimes,
                    currency, state, receiving_account_id, instructions_json,
                    version, created_at, updated_at,
                    motif, tenant, customer_ref, expected_centimes, received_centimes, status, webhook_url
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, 0, 0, 0,
                    'MAD', ?, ?, ?,
                    0, ?, ?,
                    ?, ?, ?, ?, 0, ?, ?
                )
            """, (
                inv_id, tenant.id, client_invoice_id, reference,
                customer_name or "N/A", amount_c,
                InvoiceState.AWAITING_TRANSFER.value,
                tenant.receiving_account_id or "cfg_main",
                json.dumps(instructions_data),
                now_str, now_str,
                reference, tenant.id, customer_name or "N/A", amount_c,
                InvoiceState.AWAITING_TRANSFER.value,
                tenant.webhook_url or "https://charikat.ma/api/webhooks/vextopenbank"
            ))
            row = conn.execute(
                "SELECT * FROM invoices WHERE id = ?", (inv_id,)
            ).fetchone()

        state_enum = InvoiceState(row["state"])
        status = API_STATUS[state_enum]
        paid_mad = from_centimes(row["paid_centimes"])
        total_mad = from_centimes(row["amount_centimes"])

        instructions = {}
        if row["instructions_json"]:
            try:
                instructions = json.loads(row["instructions_json"])
            except Exception:
                instructions = {}

        return {
            "invoice_id": row["client_invoice_id"],
            "reference": row["reference"],
            "status": status,
            "state": row["state"],
            "amount_mad": float(total_mad),
            "amount_mad_display": f"{total_mad:,.2f} MAD",
            "paid_mad": float(paid_mad),
            "paid_mad_display": f"{paid_mad:,.2f} MAD",
            "instructions": instructions,
            "tenant": tenant.id,
            "schema_version": "1.0",
        }
