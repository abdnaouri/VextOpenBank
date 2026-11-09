# vext_open_bank/core/db.py
import contextlib
import os
import sqlite3
import threading
import time
from typing import Iterator

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
PRAGMA temp_store = MEMORY;
PRAGMA cache_size = -20000; -- 20MB RAM cache

CREATE TABLE IF NOT EXISTS invoices (
    motif TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    customer_ref TEXT NOT NULL,
    expected_centimes INTEGER NOT NULL CHECK (expected_centimes > 0),
    received_centimes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'AWAITING_TRANSFER',
    -- Allowed: AWAITING_TRANSFER, CONFIRMED, UNDERPAID, OVERPAID, EXPIRED
    webhook_url TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    confirmed_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status, created_at);

CREATE TABLE IF NOT EXISTS bank_transfers (
    bank_tx_id TEXT PRIMARY KEY,
    raw_label TEXT NOT NULL,
    extracted_motif TEXT,
    amount_centimes INTEGER NOT NULL CHECK (amount_centimes > 0),
    sender_name TEXT,
    sender_rib TEXT,
    reconciliation_state TEXT NOT NULL, -- MATCHED, UNDERPAID, OVERPAID, ORPHANED
    ingested_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_events (
    event_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    motif TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING, DELIVERED, DEAD
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    locked_until INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    delivered_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_outbox_poll
    ON outbox_events(status, next_attempt_at, locked_until)
    WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS idempotency_keys (
    idem_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_idem_expires ON idempotency_keys(expires_at);
"""


class Database:
    """Thread-safe, zero-maintenance SQLite WAL manager with immediate write serialization."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._write_lock = threading.RLock()
        self._local = threading.local()
        self._init_schema()

    def _init_schema(self) -> None:
        """Initializes tables idempotently, safely handling legacy or unified column differences."""
        with self._write_lock:
            conn = self._get_conn()
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA temp_store = MEMORY;")

            # 1. invoices table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS invoices (
                    motif TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    customer_ref TEXT NOT NULL,
                    expected_centimes INTEGER NOT NULL CHECK (expected_centimes > 0),
                    received_centimes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'AWAITING_TRANSFER',
                    webhook_url TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    confirmed_at INTEGER
                );
            """)

            # Verify and migrate invoices table columns if legacy exists
            inv_cols = {row["name"] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()}
            if inv_cols:
                # Add 5-year columns if missing
                if "motif" not in inv_cols:
                    conn.execute("ALTER TABLE invoices ADD COLUMN motif TEXT;")
                if "tenant" not in inv_cols:
                    conn.execute("ALTER TABLE invoices ADD COLUMN tenant TEXT;")
                if "customer_ref" not in inv_cols:
                    conn.execute("ALTER TABLE invoices ADD COLUMN customer_ref TEXT;")
                if "expected_centimes" not in inv_cols:
                    conn.execute("ALTER TABLE invoices ADD COLUMN expected_centimes INTEGER;")
                if "received_centimes" not in inv_cols:
                    conn.execute("ALTER TABLE invoices ADD COLUMN received_centimes INTEGER DEFAULT 0;")
                if "status" not in inv_cols:
                    conn.execute("ALTER TABLE invoices ADD COLUMN status TEXT DEFAULT 'AWAITING_TRANSFER';")
                if "webhook_url" not in inv_cols:
                    conn.execute("ALTER TABLE invoices ADD COLUMN webhook_url TEXT;")
                if "metadata_json" not in inv_cols:
                    conn.execute("ALTER TABLE invoices ADD COLUMN metadata_json TEXT DEFAULT '{}';")
                if "confirmed_at" not in inv_cols:
                    conn.execute("ALTER TABLE invoices ADD COLUMN confirmed_at INTEGER;")
                
                # If this was a legacy schema containing 'reference', sync the columns
                if "reference" in inv_cols:
                    conn.execute("""
                        UPDATE invoices
                        SET motif = COALESCE(motif, reference),
                            tenant = COALESCE(tenant, tenant_id),
                            customer_ref = COALESCE(customer_ref, client_invoice_id, customer_name),
                            expected_centimes = COALESCE(expected_centimes, amount_centimes),
                            received_centimes = COALESCE(received_centimes, paid_centimes, 0),
                            status = COALESCE(status, state, 'AWAITING_TRANSFER')
                        WHERE motif IS NULL OR status IS NULL;
                    """)
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_motif ON invoices(motif);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status, created_at);")

            # 2. bank_transfers table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bank_transfers (
                    bank_tx_id TEXT PRIMARY KEY,
                    raw_label TEXT NOT NULL,
                    extracted_motif TEXT,
                    amount_centimes INTEGER NOT NULL CHECK (amount_centimes > 0),
                    sender_name TEXT,
                    sender_rib TEXT,
                    reconciliation_state TEXT NOT NULL,
                    ingested_at INTEGER NOT NULL
                );
            """)

            # 3. outbox_events table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS outbox_events (
                    event_id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    motif TEXT NOT NULL,
                    webhook_url TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL,
                    locked_until INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at INTEGER NOT NULL,
                    delivered_at INTEGER
                );
            """)
            outbox_cols = {row["name"] for row in conn.execute("PRAGMA table_info(outbox_events)").fetchall()}
            if outbox_cols:
                if "event_id" not in outbox_cols:
                    conn.execute("ALTER TABLE outbox_events ADD COLUMN event_id TEXT;")
                if "tenant" not in outbox_cols:
                    conn.execute("ALTER TABLE outbox_events ADD COLUMN tenant TEXT;")
                if "motif" not in outbox_cols:
                    conn.execute("ALTER TABLE outbox_events ADD COLUMN motif TEXT;")
                if "webhook_url" not in outbox_cols:
                    conn.execute("ALTER TABLE outbox_events ADD COLUMN webhook_url TEXT;")
                if "status" not in outbox_cols:
                    conn.execute("ALTER TABLE outbox_events ADD COLUMN status TEXT DEFAULT 'PENDING';")
                if "locked_until" not in outbox_cols:
                    conn.execute("ALTER TABLE outbox_events ADD COLUMN locked_until INTEGER NOT NULL DEFAULT 0;")
                
                # Sync legacy rows if needed
                if "id" in outbox_cols:
                    conn.execute("""
                        UPDATE outbox_events
                        SET event_id = COALESCE(event_id, id),
                            tenant = COALESCE(tenant, tenant_id)
                        WHERE event_id IS NULL;
                    """)

            outbox_cols_updated = {row["name"] for row in conn.execute("PRAGMA table_info(outbox_events)").fetchall()}
            if "status" in outbox_cols_updated and "locked_until" in outbox_cols_updated:
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_outbox_poll
                        ON outbox_events(status, next_attempt_at, locked_until)
                        WHERE status = 'PENDING';
                """)

            # 4. idempotency_keys table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    idem_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    response_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_idem_expires ON idempotency_keys(expires_at);")

            # 5. treasury_ledger table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS treasury_ledger (
                    entry_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_ref TEXT NOT NULL UNIQUE,
                    bank_tx_id TEXT NOT NULL UNIQUE,
                    motif TEXT,
                    tenant TEXT,
                    sens TEXT NOT NULL CHECK (sens IN ('CREDIT', 'DEBIT')),
                    canal TEXT NOT NULL DEFAULT 'VIREMENT_INSTANTANE_BAM',
                    libelle_bancaire TEXT NOT NULL,
                    nom_donneur_ordre TEXT NOT NULL,
                    rib_donneur_ordre TEXT NOT NULL,
                    montant_centimes INTEGER NOT NULL CHECK (montant_centimes > 0),
                    solde_avant_centimes INTEGER NOT NULL,
                    solde_apres_centimes INTEGER NOT NULL,
                    date_operation TEXT NOT NULL,
                    date_valeur TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_created ON treasury_ledger(created_at DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_motif ON treasury_ledger(motif);")

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path,
                timeout=5.0,
                isolation_level=None,  # Autocommit mode so we explicitly control BEGIN IMMEDIATE
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.execute("PRAGMA foreign_keys = ON;")
            self._local.conn = conn
        return conn

    @contextlib.contextmanager
    def read_conn(self) -> Iterator[sqlite3.Connection]:
        """Concurrent lock-free WAL snapshot reader."""
        yield self._get_conn()

    @contextlib.contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        """Serialized atomic write transaction (prevents SQLITE_BUSY under async bursts)."""
        with self._write_lock:
            conn = self._get_conn()
            conn.execute("BEGIN IMMEDIATE;")
            try:
                yield conn
                conn.execute("COMMIT;")
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                raise

    def run_maintenance(self) -> dict[str, int]:
        """Autonomous self-healing janitor: prunes expired keys, checkpoints WAL, optimizes indexes."""
        now = int(time.time())
        cutoff_90d = now - (90 * 86400)
        with self.write_tx() as conn:
            # Safely prune if columns exist
            idem_cols = {row["name"] for row in conn.execute("PRAGMA table_info(idempotency_keys)").fetchall()}
            if "expires_at" in idem_cols:
                cur1 = conn.execute("DELETE FROM idempotency_keys WHERE expires_at < ?", (now,))
                pruned_idem = cur1.rowcount
            else:
                pruned_idem = 0

            outbox_cols = {row["name"] for row in conn.execute("PRAGMA table_info(outbox_events)").fetchall()}
            if "status" in outbox_cols and "delivered_at" in outbox_cols:
                cur2 = conn.execute(
                    "DELETE FROM outbox_events WHERE status = 'DELIVERED' AND delivered_at < ?",
                    (cutoff_90d,),
                )
                pruned_outbox = cur2.rowcount
            else:
                pruned_outbox = 0

            conn.execute("PRAGMA optimize;")

        # Non-blocking PASSIVE checkpoint most runs; TRUNCATE reclaims disk space when idle
        with self._write_lock:
            conn = self._get_conn()
            wal_row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
        return {
            "pruned_idem_keys": pruned_idem,
            "pruned_outbox": pruned_outbox,
            "wal_busy": wal_row[0] if wal_row else 0,
        }
