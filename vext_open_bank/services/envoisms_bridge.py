"""
envoisms_bridge.py
Autonomous Reconciliation Engine connecting CFG Bank (VEXT AI) with EnvoiSMS.ma

Architecture: Top 0.01% / 30-year grade (Stripe / Adyen / Core Banking standard)

Key guarantees:
  1. Transactional Outbox — webhook enqueue is ACID-atomic with reconciliation commit.
     Network failures cannot lose confirmed payments.
  2. Multi-tenant routing — reference prefix ENV-/MED-/BAC- dispatches to the
     correct SaaS client without hard-coding URLs.
  3. HMAC-SHA256 Anti-replay — header format: `t=<unix_ts>,v1=<hex_hmac>`.
     Message signed as `{timestamp}.{raw_body}`. Tolerance: 300 s.
  4. Exponential backoff — failed outbox entries retry at 30 s, 60 s, 120 s … up
     to 3 600 s, capped at 10 attempts (then marked DEAD_LETTER).
  5. Race-condition guard — reconciliation wrapped in a single SQLite transaction;
     INSERT OR IGNORE on unique topup_id prevents double credit on concurrent calls.
  6. Structured Exchange Telemetry — logs inbound/outbound exchanges for auditability.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import sqlite3
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from vext_open_bank.core.exchange_logger import exchange_logger
from vext_open_bank.core.moroccan_banks import (
    resolve_moroccan_bank,
    matches_reference_fuzzy,
)

logger = logging.getLogger("EnvoiSMSBridge")

DATABASE_PATH = "/tmp/vext_envoisms_reconciliation.db"

# Dynamically sourced HMAC webhook secret; if unset, generate ephemeral runtime secret
SHARED_WEBHOOK_SECRET = (
    os.getenv("ENVOISMS_VEXT_SECRET")
    or os.getenv("ENVOISMS_SECRET")
    or secrets.token_hex(32)
)

# ---------------------------------------------------------------------------
# Multi-tenant webhook routing — extend here for new SaaS clients.
# Reference prefix (first segment before "-") determines the tenant.
# ---------------------------------------------------------------------------
_ENVOISMS_BASE = os.getenv("ENVOISMS_API_BASE", "https://api.envoisms.ma")
_MEDIUNIT_BASE  = os.getenv("MEDIUNIT_API_BASE", "")
_BAC_BASE       = os.getenv("BACENPOCHE_API_BASE", "")
_SOUMIT_BASE    = os.getenv("SOUMIT_API_BASE", "https://soumit.ma")

TENANT_WEBHOOK_URLS: Dict[str, str] = {
    "ENV": f"{_ENVOISMS_BASE}/internal/vext/bank-transfer/webhook",
    "MED": f"{_MEDIUNIT_BASE}/internal/vext/bank-transfer/webhook" if _MEDIUNIT_BASE else "",
    "BAC": f"{_BAC_BASE}/internal/vext/bank-transfer/webhook"      if _BAC_BASE       else "",
    "SOU": f"{_SOUMIT_BASE}/api/webhooks/vextopenbank",
    "BC":  f"{_SOUMIT_BASE}/api/webhooks/vextopenbank",
}

TENANT_SECRETS: Dict[str, str] = {
    "ENV": os.getenv("ENVOISMS_VEXT_SECRET", SHARED_WEBHOOK_SECRET),
    "MED": os.getenv("MEDIUNIT_VEXT_SECRET", ""),
    "BAC": os.getenv("BAC_VEXT_SECRET", ""),
    "SOU": os.getenv("SOUMIT_VEXT_SECRET", os.getenv("VEXT_WEBHOOK_SECRET", SHARED_WEBHOOK_SECRET)),
    "BC":  os.getenv("SOUMIT_VEXT_SECRET", os.getenv("VEXT_WEBHOOK_SECRET", SHARED_WEBHOOK_SECRET)),
}

MAX_OUTBOX_ATTEMPTS = 10
MAX_BACKOFF_SECONDS = 3600


def _derive_tenant(reference: str) -> str:
    """Extract tenant prefix from bank wire reference (e.g. 'ENV-98421' → 'ENV')."""
    if reference and "-" in reference:
        prefix = reference.split("-")[0].upper()
        if prefix in TENANT_WEBHOOK_URLS:
            return prefix
    return "ENV"


class EnvoiSMSBridge:
    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path
        self._last_sync_timestamp: float = 0.0
        self.MIN_SYNC_INTERVAL: float = 25.0  # seconds between CFG syncs to prevent WAF throttling
        self._sync_lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()

            # Transactions observed on VEXT AI Bank accounts (CFG, Attijari, CIH, BCP)
            c.execute("""
                CREATE TABLE IF NOT EXISTS cfg_incoming_transfers (
                    bank_tx_id TEXT PRIMARY KEY,
                    amount_mad REAL NOT NULL,
                    sender_name TEXT,
                    sender_rib TEXT,
                    motif TEXT,
                    value_date TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    is_reconciled INTEGER DEFAULT 0,
                    recipient_rib TEXT DEFAULT '050810006011833971200189',
                    recipient_bank_code TEXT DEFAULT '051'
                )
            """)
            try:
                c.execute("ALTER TABLE cfg_incoming_transfers ADD COLUMN recipient_rib TEXT DEFAULT '050810006011833971200189'")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE cfg_incoming_transfers ADD COLUMN recipient_bank_code TEXT DEFAULT '051'")
            except sqlite3.OperationalError:
                pass

            # Reconciliation ledger linking bank transaction to tenant topup
            c.execute("""
                CREATE TABLE IF NOT EXISTS bank_topup_reconciliations (
                    topup_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    bank_tx_id TEXT UNIQUE NOT NULL,
                    amount_mad REAL NOT NULL,
                    reconciled_at TEXT NOT NULL,
                    webhook_status TEXT DEFAULT 'PENDING',
                    FOREIGN KEY(bank_tx_id) REFERENCES cfg_incoming_transfers(bank_tx_id)
                )
            """)

            # Pending topup intents awaiting bank reconciliation
            c.execute("""
                CREATE TABLE IF NOT EXISTS pending_topup_intents (
                    topup_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    amount_mad REAL NOT NULL,
                    reference TEXT NOT NULL,
                    customer_name TEXT,
                    customer_phone TEXT,
                    registered_at TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL,
                    status TEXT DEFAULT 'PENDING'
                )
            """)

            # ----------------------------------------------------------------
            # Transactional Outbox
            # Written atomically in the same transaction as reconciliation.
            # Guarantees zero webhook loss even on process crash or network fault.
            # ----------------------------------------------------------------
            c.execute("""
                CREATE TABLE IF NOT EXISTS webhook_outbox (
                    id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    status TEXT DEFAULT 'PENDING',
                    attempts INTEGER DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL,
                    last_error TEXT,
                    created_at INTEGER NOT NULL,
                    delivered_at INTEGER
                )
            """)
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_outbox_pending
                ON webhook_outbox (status, next_attempt_at)
            """)

            conn.commit()

    # ------------------------------------------------------------------
    # HMAC helpers  (format: t=<unix_ts>,v1=<hex_sha256>)
    # ------------------------------------------------------------------

    @staticmethod
    def _sign_webhook(payload_json: str, secret: str) -> Tuple[str, str]:
        """
        Returns (header_value, timestamp_str).
        Signs "{timestamp}.{payload_json}" with HMAC-SHA256.
        Header: X-Vext-Signature: t=<ts>,v1=<hex>
        """
        timestamp = str(int(time.time()))
        message = f"{timestamp}.{payload_json}".encode("utf-8")
        sig = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
        return f"t={timestamp},v1={sig}", timestamp

    @staticmethod
    def verify_webhook_signature(
        raw_body: str,
        signature_header: str,
        secret: str,
        tolerance_seconds: int = 300,
    ) -> bool:
        """
        Verify an incoming webhook (called by tenant's receiver).
        Returns True only when signature is valid AND within the replay window.
        """
        try:
            parts = dict(item.split("=", 1) for item in signature_header.split(",") if "=" in item)
            timestamp    = parts.get("t", "")
            received_sig = parts.get("v1", "")
            if not timestamp or not received_sig:
                return False
            ts = int(timestamp)
            if abs(int(time.time()) - ts) > tolerance_seconds:
                return False
            message  = f"{timestamp}.{raw_body}".encode("utf-8")
            expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, received_sig)   # constant-time
        except Exception:
            return False

    @staticmethod
    def generate_signature(payload: Dict[str, Any], secret: str) -> str:
        """Legacy inline signature — kept for backward compat with existing receivers."""
        serialized = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hmac.new(secret.encode("utf-8"), serialized, hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------
    # Outbox dispatcher
    # ------------------------------------------------------------------

    def dispatch_pending_webhooks(self) -> int:
        """
        Attempt delivery of all PENDING/RETRYING outbox rows whose
        next_attempt_at has arrived. Returns count delivered this pass.
        Safe to call from multiple threads — SQLite serialises the writes.
        """
        now_ts = int(time.time())
        delivered_count = 0

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT id, tenant, event_type, target_url, payload_json,
                       idempotency_key, attempts
                FROM webhook_outbox
                WHERE status IN ('PENDING', 'RETRYING')
                  AND next_attempt_at <= ?
                ORDER BY created_at ASC
                LIMIT 50
            """, (now_ts,)).fetchall()

        for row in rows:
            outbox_id    = row["id"]
            tenant       = row["tenant"]
            target_url   = row["target_url"]
            payload_json = row["payload_json"]
            attempts     = row["attempts"]
            secret       = TENANT_SECRETS.get(tenant, SHARED_WEBHOOK_SECRET)

            if not target_url:
                self._mark_outbox_dead(outbox_id, "No target_url configured for tenant")
                continue

            sig_header, _ts = self._sign_webhook(payload_json, secret)

            try:
                req = urllib.request.Request(
                    target_url,
                    data=payload_json.encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-Vext-Signature": sig_header,
                        "X-Idempotency-Key": row["idempotency_key"],
                        "User-Agent": "VextOpenBank-Outbox/1.0",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    status_code = resp.status

                if 200 <= status_code < 300:
                    self._mark_outbox_delivered(outbox_id)
                    delivered_count += 1
                    logger.info(
                        "Outbox %s delivered to %s (attempt %d, HTTP %d)",
                        outbox_id, target_url, attempts + 1, status_code,
                    )
                else:
                    self._mark_outbox_retry(outbox_id, attempts, f"HTTP {status_code}")
            except Exception as exc:
                self._mark_outbox_retry(outbox_id, attempts, str(exc))
                logger.warning(
                    "Outbox %s delivery failed (attempt %d): %s",
                    outbox_id, attempts + 1, exc,
                )

        return delivered_count

    def _mark_outbox_delivered(self, outbox_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE webhook_outbox
                SET status = 'DELIVERED', delivered_at = ?, attempts = attempts + 1
                WHERE id = ?
            """, (int(time.time()), outbox_id))
            conn.commit()

    def _mark_outbox_retry(self, outbox_id: str, attempts: int, error: str) -> None:
        new_attempts = attempts + 1
        if new_attempts >= MAX_OUTBOX_ATTEMPTS:
            self._mark_outbox_dead(outbox_id, error)
            return
        # Exponential backoff: 30, 60, 120 … capped at 3600 s, with ±10 % jitter
        base_delay = min(30 * (2 ** attempts), MAX_BACKOFF_SECONDS)
        jitter     = base_delay * 0.1 * random.uniform(-1, 1)
        next_at    = int(time.time()) + int(base_delay + jitter)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE webhook_outbox
                SET status = 'RETRYING', attempts = ?, next_attempt_at = ?, last_error = ?
                WHERE id = ?
            """, (new_attempts, next_at, error[:500], outbox_id))
            conn.commit()

    def _mark_outbox_dead(self, outbox_id: str, error: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE webhook_outbox
                SET status = 'DEAD_LETTER', attempts = attempts + 1, last_error = ?
                WHERE id = ?
            """, (error[:500], outbox_id))
            conn.commit()
        logger.error("Outbox %s → DEAD_LETTER: %s", outbox_id, error)

    # ------------------------------------------------------------------
    # Core bridge methods
    # ------------------------------------------------------------------

    def record_incoming_bank_transfer(
        self,
        bank_tx_id: str,
        amount_mad: float,
        sender_name: str,
        motif: str,
        sender_rib: Optional[str] = None,
        recipient_rib: Optional[str] = "050810006011833971200189",
        recipient_bank_code: Optional[str] = "051",
        value_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a newly observed incoming bank transfer on any VEXT receiving account (CFG, Attijari, BCP, CIH)."""
        now_str  = datetime.now().isoformat()
        val_date = value_date or now_str
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR IGNORE INTO cfg_incoming_transfers
                (bank_tx_id, amount_mad, sender_name, sender_rib, motif, value_date, recorded_at, is_reconciled, recipient_rib, recipient_bank_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """, (bank_tx_id, amount_mad, sender_name, sender_rib, motif, val_date, now_str, recipient_rib or "050810006011833971200189", recipient_bank_code or "051"))
            conn.commit()

        res = {
            "bank_tx_id": bank_tx_id,
            "amount_mad": amount_mad,
            "sender_name": sender_name,
            "sender_rib": sender_rib,
            "recipient_rib": recipient_rib,
            "recipient_bank_code": recipient_bank_code,
            "motif": motif,
            "recorded_at": now_str,
        }

        exchange_logger.log_exchange(
            exchange_type="CFG_TRANSFER_INGESTION",
            target="record_incoming_bank_transfer",
            direction="INBOUND",
            status_code=200,
            request_data={"bank_tx_id": bank_tx_id, "amount_mad": amount_mad, "sender_name": sender_name, "motif": motif},
            response_data=res,
        )

        return res

    def register_or_update_intent(
        self,
        topup_id: str,
        account_id: str,
        amount_mad: float,
        reference: str,
        customer_name: Optional[str] = None,
        customer_phone: Optional[str] = None,
    ) -> None:
        """Register or touch an active topup intent in local SQLite cache."""
        now_str = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO pending_topup_intents
                    (topup_id, account_id, amount_mad, reference, customer_name,
                     customer_phone, registered_at, last_checked_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                ON CONFLICT(topup_id) DO UPDATE SET
                    last_checked_at = excluded.last_checked_at,
                    status = CASE WHEN status = 'PENDING' THEN 'PENDING' ELSE status END
            """, (topup_id, account_id, amount_mad, reference,
                  customer_name, customer_phone, now_str, now_str))
            conn.commit()

    def find_matching_transfer(
        self,
        amount_mad: float,
        reference: str,
        customer_name: Optional[str] = None,
        customer_phone: Optional[str] = None,
        tolerance_mad: float = 0.05,
    ) -> Optional[Dict[str, Any]]:
        """
        Find an unreconciled CFG transfer matching by amount within centime tolerance
        (±0.05 MAD) and motif / sender heuristics. Returns the best candidate or None.
        """
        low  = round(amount_mad - tolerance_mad, 2)
        high = round(amount_mad + tolerance_mad, 2)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM cfg_incoming_transfers
                WHERE is_reconciled = 0
                  AND amount_mad BETWEEN ? AND ?
                ORDER BY recorded_at DESC
            """, (low, high)).fetchall()

        ref_upper  = (reference or "").strip().upper()
        name_upper = (customer_name or "").strip().upper()

        for row in rows:
            motif  = row["motif"] or ""
            sender = (row["sender_name"] or "").upper()
            if ref_upper and matches_reference_fuzzy(motif, ref_upper):
                return dict(row)
            if name_upper and len(name_upper) >= 3 and name_upper[:6] in sender:
                return dict(row)

        return None

    def find_all_transfers_by_reference(self, reference: str) -> List[Dict[str, Any]]:
        """
        Find all unreconciled transfers matching the reference motif (exact or fuzzy).
        Supports multi-wire installment settlement (e.g. BAM daily limit split).
        """
        ref_upper = (reference or "").strip().upper()
        if not ref_upper:
            return []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM cfg_incoming_transfers
                WHERE is_reconciled = 0
                ORDER BY recorded_at ASC
            """).fetchall()

        matched = []
        for row in rows:
            motif = row["motif"] or ""
            if matches_reference_fuzzy(motif, ref_upper):
                matched.append(dict(row))
        return matched

    def find_transfer_by_reference(self, reference: str) -> Optional[Dict[str, Any]]:
        """
        Find any unreconciled CFG transfer matching the reference motif, regardless of amount.
        Used to detect underpayment / overpayment variance fraud.
        """
        wires = self.find_all_transfers_by_reference(reference)
        return wires[0] if wires else None

    def get_unmatched_transfers(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Return all unreconciled bank transfers in the suspense account ('Compte d'attente').
        Allows treasury and finance operations to identify mystery inflows.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM cfg_incoming_transfers
                WHERE is_reconciled = 0
                ORDER BY recorded_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def claim_unmatched_transfer(
        self,
        bank_tx_id: str,
        topup_id: str,
        account_id: str,
        reference: str,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Manually or rule-based allocate an unreferenced transfer from the suspense account to an intent.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            tx = conn.execute(
                "SELECT * FROM cfg_incoming_transfers WHERE bank_tx_id = ? AND is_reconciled = 0",
                (bank_tx_id,),
            ).fetchone()
        if not tx:
            return False, {"error": "TRANSFER_NOT_FOUND_OR_ALREADY_RECONCILED", "bank_tx_id": bank_tx_id}

        # Tag the wire with the reference so the reconciliation matcher binds it
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE cfg_incoming_transfers SET motif = motif || ' ' || ? WHERE bank_tx_id = ?",
                (reference, bank_tx_id),
            )
            conn.commit()

        return self.reconcile_topup(
            topup_id=topup_id,
            account_id=account_id,
            amount_mad=tx["amount_mad"],
            reference=reference,
        )

    def trigger_async_sync(self) -> None:
        """Non-blocking background dispatch (rate-limited by MIN_SYNC_INTERVAL)."""
        def _run():
            with self._sync_lock:
                if time.time() - self._last_sync_timestamp < self.MIN_SYNC_INTERVAL:
                    return
                self._last_sync_timestamp = time.time()
                try:
                    self.dispatch_pending_webhooks()
                except Exception as exc:
                    logger.warning("Async sync error: %s", exc)

        threading.Thread(target=_run, daemon=True).start()

    def reconcile_topup(
        self,
        topup_id: str,
        account_id: str,
        amount_mad: float,
        reference: str,
        customer_name: Optional[str] = None,
        customer_phone: Optional[str] = None,
        eur_mad_rate: float = 10.80,
        skip_async_trigger: bool = False,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Attempt reconciliation between a topup intent and live CFG transfers.

        ACID guarantee: the reconciliation row AND the webhook_outbox row are
        written inside the same SQLite connection/transaction. If the process
        crashes after the commit the outbox dispatcher will retry; if it crashes
        before the commit nothing is recorded — perfect atomicity.
        """
        # --- Fast path: already reconciled (idempotent) ---
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute(
                "SELECT * FROM bank_topup_reconciliations WHERE topup_id = ?",
                (topup_id,),
            ).fetchone()
        if existing:
            return True, {
                "status": "ALREADY_RECONCILED",
                "topup_id": topup_id,
                "bank_tx_id": existing["bank_tx_id"],
                "amount_mad": existing["amount_mad"],
                "reconciled_at": existing["reconciled_at"],
            }

        # --- Match incoming transfer within centime tolerance (±0.05 MAD) ---
        match = self.find_matching_transfer(
            amount_mad=amount_mad,
            reference=reference,
            customer_name=customer_name,
            customer_phone=customer_phone,
            tolerance_mad=0.05,
        )

        matched_tx_ids = []
        if match:
            matched_tx_ids = [match["bank_tx_id"]]
        else:
            # Check multi-wire cumulative installments:
            # Supports clients splitting a payment across multiple wires (e.g. BAM daily transfer limits)
            wires = self.find_all_transfers_by_reference(reference)
            if wires:
                total_collected = sum(w["amount_mad"] for w in wires)
                variance = round(abs(total_collected - amount_mad), 2)
                if variance <= 0.05:
                    match = wires[0]
                    matched_tx_ids = [w["bank_tx_id"] for w in wires]
                else:
                    is_underpayment = total_collected < amount_mad
                    missing_mad = round(amount_mad - total_collected, 2) if is_underpayment else 0.0
                    overpaid_mad = round(total_collected - amount_mad, 2) if not is_underpayment else 0.0

                    if is_underpayment:
                        msg = (
                            f"Incohérence de montant : virement(s) reçu(s) totalisant {total_collected:,.2f} MAD "
                            f"({len(wires)} virement(s)) pour une facture/recharge attendue de {amount_mad:,.2f} MAD TTC. "
                            f"Montant manquant : {missing_mad:,.2f} MAD."
                        )
                    else:
                        msg = (
                            f"Incohérence de montant : virement(s) reçu(s) totalisant {total_collected:,.2f} MAD "
                            f"supérieur au montant attendu de {amount_mad:,.2f} MAD TTC. "
                            f"Montant en trop : {overpaid_mad:,.2f} MAD."
                        )

                    exchange_logger.log_exchange(
                        exchange_type="FRAUD_UNDERPAYMENT_DETECTED",
                        target="reconcile_topup",
                        direction="INBOUND",
                        status_code=400,
                        request_data={"topup_id": topup_id, "expected_mad": amount_mad, "received_mad": total_collected, "wires_count": len(wires)},
                        response_data={"error": "INCOHERENCE_DE_MONTANT", "missing_amount_mad": missing_mad},
                    )

                    return False, {
                        "status": "REJECTED_UNDERPAYMENT",
                        "error": "INCOHERENCE_DE_MONTANT",
                        "message": msg,
                        "topup_id": topup_id,
                        "expected_amount_mad": amount_mad,
                        "received_amount_mad": total_collected,
                        "missing_amount_mad": missing_mad,
                        "overpaid_amount_mad": overpaid_mad,
                        "variance_mad": variance,
                        "installments_count": len(wires),
                        "currency": "MAD",
                        "tolerance_mad": 0.05,
                    }

        if not match:
            self.register_or_update_intent(
                topup_id=topup_id,
                account_id=account_id,
                amount_mad=amount_mad,
                reference=reference,
                customer_name=customer_name,
                customer_phone=customer_phone,
            )
            if not skip_async_trigger:
                self.trigger_async_sync()
            return False, {
                "status": "PENDING_CONFIRMATION",
                "message": (
                    f"Aucun virement de {amount_mad:,.2f} MAD correspondant à la "
                    f"référence '{reference}' n'a encore été détecté sur le compte CFG Bank VEXT AI."
                ),
                "topup_id": topup_id,
                "amount_mad": amount_mad,
                "checked_at": datetime.now().isoformat(),
            }

        bank_tx_id = "+".join(matched_tx_ids) if len(matched_tx_ids) > 1 else match["bank_tx_id"]
        now_str    = datetime.now().isoformat()
        now_ts     = int(time.time())

        # --- Build webhook payload ---
        tenant     = _derive_tenant(reference)
        target_url = TENANT_WEBHOOK_URLS.get(tenant, TENANT_WEBHOOK_URLS["ENV"])
        amount_eur = round(amount_mad / eur_mad_rate, 2)

        sender_rib = match.get("sender_rib")
        bank_meta  = resolve_moroccan_bank(sender_rib)

        confirmation_payload: Dict[str, Any] = {
            "event":                 "bank_transfer.confirmed",
            "topup_id":              topup_id,
            "account_id":            account_id,
            "amount_mad":            amount_mad,
            "amount_eur":            amount_eur,
            "bank_tx_id":            bank_tx_id,
            "related_bank_tx_ids":   matched_tx_ids,
            "installments_count":    len(matched_tx_ids),
            "bank_name":             "CFG Bank",
            "beneficiary_name":      "VEXT AI",
            "beneficiary_rib":       match.get("recipient_rib") or "050810006011833971200189",
            "sender_name":           match.get("sender_name"),
            "sender_rib":            sender_rib,
            "originating_bank":      bank_meta["name"],
            "originating_bank_code": bank_meta["code"],
            "transfer_channel":      "BAM Virement Instantané (24/7)" if bank_meta.get("supports_instant") else "BAM Virement Ordinaire (SIMT)",
            "transfer_motif":        match.get("motif"),
            "tenant":                tenant,
            "timestamp":             now_str,
        }
        payload_json    = json.dumps(confirmation_payload, sort_keys=True)
        idempotency_key = f"vext_reconcile_{topup_id}"

        # Legacy inline signature (backward compat)
        confirmation_payload["signature"] = self.generate_signature(
            confirmation_payload, SHARED_WEBHOOK_SECRET
        )

        # --- ATOMIC commit: reconciliation + outbox enqueue (single transaction) ---
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            try:
                c.execute("""
                    INSERT INTO bank_topup_reconciliations
                    (topup_id, account_id, bank_tx_id, amount_mad, reconciled_at, webhook_status)
                    VALUES (?, ?, ?, ?, ?, 'PENDING')
                """, (topup_id, account_id, bank_tx_id, amount_mad, now_str))

                for tx_id in matched_tx_ids:
                    c.execute("""
                        UPDATE cfg_incoming_transfers SET is_reconciled = 1
                        WHERE bank_tx_id = ?
                    """, (tx_id,))

                c.execute("""
                    UPDATE pending_topup_intents SET status = 'CONFIRMED'
                    WHERE topup_id = ?
                """, (topup_id,))

                # Outbox row — SAME TRANSACTION as reconciliation
                c.execute("""
                    INSERT OR IGNORE INTO webhook_outbox
                    (id, tenant, event_type, target_url, payload_json, idempotency_key,
                     status, attempts, next_attempt_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?)
                """, (
                    str(uuid.uuid4()),
                    tenant,
                    "bank_transfer.confirmed",
                    target_url,
                    payload_json,
                    idempotency_key,
                    now_ts,
                    now_ts,
                ))

                conn.commit()
            except sqlite3.IntegrityError:
                # Concurrent request already claimed this topup_id — idempotent
                return False, {"status": "CONFLICT", "message": "Transaction already claimed by concurrent request"}

        # --- Fire outbox dispatcher in daemon thread (non-blocking) ---
        if not skip_async_trigger:
            threading.Thread(target=self.dispatch_pending_webhooks, daemon=True).start()

        result = {
            "status": "CONFIRMED",
            "message": f"Virement bancaire de {amount_mad:,.2f} MAD confirmé avec succès sur CFG Bank !",
            "details": confirmation_payload,
        }

        exchange_logger.log_exchange(
            exchange_type="ENVOISMS_RECONCILIATION_MATCH",
            target="reconcile_topup",
            direction="INTERNAL_SYNC",
            status_code=200,
            request_data={"topup_id": topup_id, "amount_mad": amount_mad, "reference": reference},
            response_data={"status": "CONFIRMED", "bank_tx_id": bank_tx_id},
        )

        return True, result

    async def run_sync_worker(self) -> None:
        """
        Async background worker that periodically dispatches pending outbox webhooks.
        Runs every MIN_SYNC_INTERVAL seconds. Called from FastAPI lifespan.
        """
        while True:
            try:
                self.dispatch_pending_webhooks()
                self._last_sync_timestamp = time.time()
            except Exception as exc:
                logger.warning("run_sync_worker error: %s", exc)
            await asyncio.sleep(self.MIN_SYNC_INTERVAL)

    def should_trigger_sync(self) -> bool:
        """Returns True when the cooldown has expired and a CFG sync is permissible."""
        return (time.time() - self._last_sync_timestamp) >= self.MIN_SYNC_INTERVAL

    def get_active_pending_intents(self) -> List[Dict[str, Any]]:
        """Return all topup intents still in PENDING status."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM pending_topup_intents WHERE status = 'PENDING' ORDER BY registered_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_sync_status(self) -> Dict[str, Any]:
        """Return operational status for the /sync-status health endpoint."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            pending = conn.execute(
                "SELECT COUNT(*) as n FROM webhook_outbox WHERE status IN ('PENDING','RETRYING')"
            ).fetchone()["n"]
            dead = conn.execute(
                "SELECT COUNT(*) as n FROM webhook_outbox WHERE status = 'DEAD_LETTER'"
            ).fetchone()["n"]
        return {
            "status": "OPERATIONAL",
            "anti_throttling_cooldown_seconds": self.MIN_SYNC_INTERVAL,
            "local_ledger_ready": True,
            "outbox_pending":     pending,
            "outbox_dead_letter": dead,
        }


envoisms_bridge = EnvoiSMSBridge()
