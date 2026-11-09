import os
import json
import sqlite3
from typing import Generator
from contextlib import contextmanager

from vext_open_bank.core.db import Database, SCHEMA_SQL

DATABASE_PATH = os.getenv("VEXT_DB_PATH", "/tmp/vext_envoisms_reconciliation.db")

# Global singleton Database instance ensuring serialized write-locking & immediate WAL execution
_global_db = Database(DATABASE_PATH)


def init_db(db_path: str = DATABASE_PATH) -> None:
    """Initialize SQLite database with WAL pragmas and unified engine schema."""
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        c = conn.cursor()

        # Invoices table (all amounts in BIGINT centimes)
        c.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                client_invoice_id TEXT NOT NULL,
                reference TEXT UNIQUE NOT NULL,
                customer_name TEXT,
                amount_centimes INTEGER NOT NULL,
                subtotal_centimes INTEGER DEFAULT 0,
                tax_centimes INTEGER DEFAULT 0,
                paid_centimes INTEGER DEFAULT 0,
                currency TEXT DEFAULT 'MAD',
                state TEXT NOT NULL,
                receiving_account_id TEXT DEFAULT 'cfg_main',
                instructions_json TEXT,
                version INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT,
                UNIQUE (tenant_id, client_invoice_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_invoices_ref ON invoices(reference);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_invoices_tenant_state ON invoices(tenant_id, state);")

        # Transfers ledger table (bank transactions, amounts in BIGINT centimes)
        c.execute("""
            CREATE TABLE IF NOT EXISTS transfers (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                bank_txn_id TEXT UNIQUE NOT NULL,
                amount_centimes INTEGER NOT NULL,
                currency TEXT DEFAULT 'MAD',
                reference TEXT,
                sender_name TEXT,
                sender_rib TEXT,
                recipient_rib TEXT DEFAULT '050810006011833971200189',
                recipient_bank_code TEXT DEFAULT '051',
                received_at TEXT NOT NULL,
                raw_json TEXT,
                matched_invoice_id TEXT,
                match_status TEXT DEFAULT 'UNMATCHED',
                created_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_transfers_ref ON transfers(reference);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_transfers_bank_txn ON transfers(bank_txn_id);")

        # Dedicated Idempotency Store (24h TTL)
        c.execute("""
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response_status INTEGER NOT NULL,
                response_json TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (key, tenant_id, endpoint)
            )
        """)

        # Transactional Outbox Events
        c.execute("""
            CREATE TABLE IF NOT EXISTS outbox_events (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                attempts INTEGER DEFAULT 0,
                next_attempt_at INTEGER NOT NULL,
                delivered_at INTEGER,
                last_error TEXT,
                created_at INTEGER NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_outbox_events_pending ON outbox_events(delivered_at, next_attempt_at);")

        # Append-only Audit Log
        c.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY,
                tenant_id TEXT,
                invoice_id TEXT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # Invoice sequence generator table
        c.execute("""
            CREATE TABLE IF NOT EXISTS invoice_seq (
                prefix TEXT NOT NULL,
                year INTEGER NOT NULL,
                n INTEGER DEFAULT 1,
                PRIMARY KEY (prefix, year)
            )
        """)

        # Legacy Reconciliation Exceptions (Dead-Letter Table)
        c.execute("""
            CREATE TABLE IF NOT EXISTS reconciliation_exceptions (
                id TEXT PRIMARY KEY,
                bank_tx_id TEXT,
                raw_transfer_json TEXT,
                failure_reason TEXT NOT NULL,
                attempt_count INTEGER DEFAULT 1,
                last_attempt_at TEXT NOT NULL,
                resolved INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        conn.commit()
    finally:
        conn.close()


def get_connection(db_path: str = DATABASE_PATH) -> sqlite3.Connection:
    """Provide a SQLite connection configured with WAL pragmas."""
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_session(db_path: str = DATABASE_PATH) -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_database() -> Database:
    """Return singleton Database instance with BEGIN IMMEDIATE write serialization."""
    return _global_db
