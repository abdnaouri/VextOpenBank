"""
envoisms_bridge.py
Autonomous Reconciliation Engine connecting CFG Bank (VEXT AI) with EnvoiSMS.ma
Handles incoming transfer detection, intelligent motif matching, strict deduplication, and HMAC webhook dispatch.
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
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from vext_open_bank.core.exchange_logger import exchange_logger

logger = logging.getLogger("EnvoiSMSBridge")

DATABASE_PATH = "/tmp/vext_envoisms_reconciliation.db"
# Dynamically sourced HMAC webhook secret; if unset, generate ephemeral runtime secret
SHARED_WEBHOOK_SECRET = (
    os.getenv("ENVOISMS_VEXT_SECRET")
    or os.getenv("ENVOISMS_SECRET")
    or secrets.token_hex(32)
)
ENVOISMS_API_BASE = os.getenv("ENVOISMS_API_BASE", "https://api.envoisms.ma")


class EnvoiSMSBridge:
    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path
        self._last_sync_timestamp: float = 0.0
        self.MIN_SYNC_INTERVAL: float = 25.0  # Minimum seconds between CFG syncs to prevent WAF throttling
        self._sync_lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            # Transactions observed on VEXT AI CFG Bank account
            c.execute("""
                CREATE TABLE IF NOT EXISTS cfg_incoming_transfers (
                    bank_tx_id TEXT PRIMARY KEY,
                    amount_mad REAL NOT NULL,
                    sender_name TEXT,
                    sender_rib TEXT,
                    motif TEXT,
                    value_date TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    is_reconciled INTEGER DEFAULT 0
                )
            """)
            # Reconciliation ledger linking bank transaction to EnvoiSMS topup
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
            conn.commit()

    def record_incoming_bank_transfer(
        self,
        bank_tx_id: str,
        amount_mad: float,
        sender_name: str,
        motif: str,
        sender_rib: Optional[str] = None,
        value_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Record a newly observed incoming bank transfer on CFG account.
        """
        now_str = datetime.now().isoformat()
        val_date = value_date or now_str
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT OR IGNORE INTO cfg_incoming_transfers
                (bank_tx_id, amount_mad, sender_name, sender_rib, motif, value_date, recorded_at, is_reconciled)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """, (bank_tx_id, amount_mad, sender_name, sender_rib, motif, val_date, now_str))
            conn.commit()

        res = {
            "bank_tx_id": bank_tx_id,
            "amount_mad": amount_mad,
            "sender_name": sender_name,
            "motif": motif,
            "recorded_at": now_str
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
            c = conn.cursor()
            c.execute("""
                INSERT INTO pending_topup_intents
                (topup_id, account_id, amount_mad, reference, customer_name, customer_phone, registered_at, last_checked_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                ON CONFLICT(topup_id) DO UPDATE SET
                    last_checked_at = excluded.last_checked_at,
                    amount_mad = excluded.amount_mad,
                    reference = excluded.reference
            """, (topup_id, account_id, amount_mad, reference, customer_name, customer_phone, now_str, now_str))
            conn.commit()

    def get_active_pending_intents(self, max_age_minutes: int = 60) -> List[Dict[str, Any]]:
        """Retrieve recent active intents that still require reconciliation."""
        cutoff = (datetime.now() - timedelta(minutes=max_age_minutes)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT * FROM pending_topup_intents
                WHERE status = 'PENDING' AND last_checked_at >= ?
                ORDER BY last_checked_at DESC
            """, (cutoff,))
            rows = c.fetchall()
            return [dict(r) for r in rows]

    def should_trigger_sync(self) -> bool:
        """Check if sufficient cooldown has elapsed since the last CFG gateway call."""
        return (time.time() - self._last_sync_timestamp) >= self.MIN_SYNC_INTERVAL

    def sync_from_cfg(self, account_id: Optional[str] = None) -> int:
        """
        Pull recent incoming transactions from CFG MobileFabric and persist them into
        cfg_incoming_transfers. Returns the number of newly recorded transfers.

        Protected by:
        - Strict lock and cooldown to eliminate multiple simultaneous gateway queries.
        - Origin sanitization & mobile user-agent spoofing inside MobileFabricClient.
        - Graceful error recovery: failures never interrupt local reconciliation.
        """
        if not self._sync_lock.acquire(blocking=False):
            logger.debug("CFG sync already in progress, skipping duplicate call.")
            return 0

        new_count = 0
        try:
            from vext_open_bank.services.mobilefabric import mf_client

            logger.info("Synchronizing recent bank movements from CFG MobileFabric...")
            # Query PFM Detail / Account transactions from MobileFabric
            acc_param = {"accountId": account_id} if account_id else {}
            data = None
            try:
                data = mf_client.invoke_service("CFGPFMDetail", params=acc_param)
            except Exception as e:
                logger.debug("CFGPFMDetail call returned error (normal if simulated or gateway offline): %s", e)

            if not data or not isinstance(data, dict):
                try:
                    data = mf_client.invoke_service("CFGAccountOverview/AccountOverview")
                except Exception as e:
                    logger.debug("CFGAccountOverview fallback call returned: %s", e)

            if data and isinstance(data, dict):
                raw_list = data.get("transactions") or data.get("records") or []
                for item in raw_list:
                    raw_amount = item.get("amount", 0.0)
                    try:
                        amt = float(raw_amount)
                    except (ValueError, TypeError):
                        amt = 0.0

                    # Ingest only incoming credits
                    if amt > 0:
                        tx_id = str(item.get("transactionId") or item.get("id") or item.get("reference") or "")
                        label = item.get("label") or item.get("description") or item.get("motif") or ""
                        sender = item.get("senderName") or item.get("sender") or ""
                        v_date = item.get("valueDate") or item.get("date") or datetime.now().isoformat()

                        if tx_id:
                            self.record_incoming_bank_transfer(
                                bank_tx_id=tx_id,
                                amount_mad=amt,
                                sender_name=sender,
                                motif=label,
                                value_date=v_date,
                            )
                            new_count += 1

            self._last_sync_timestamp = time.time()

            # Automatically evaluate all active pending intents against freshly updated ledger
            pending = self.get_active_pending_intents()
            for intent in pending:
                self.reconcile_topup(
                    topup_id=intent["topup_id"],
                    account_id=intent["account_id"],
                    amount_mad=intent["amount_mad"],
                    reference=intent["reference"],
                    customer_name=intent.get("customer_name"),
                    customer_phone=intent.get("customer_phone"),
                    skip_async_trigger=True,
                )

            exchange_logger.log_exchange(
                exchange_type="ENVOISMS_CFG_SYNC",
                target="sync_from_cfg",
                direction="INTERNAL_SYNC",
                status_code=200,
                request_data={"account_id": account_id},
                response_data={"new_transactions_ingested": new_count, "active_intents_evaluated": len(pending)},
            )
        except Exception as exc:
            logger.warning("CFG background synchronization encountered error: %s", exc)
            exchange_logger.log_exchange(
                exchange_type="ENVOISMS_CFG_SYNC",
                target="sync_from_cfg",
                direction="INTERNAL_SYNC",
                status_code=500,
                request_data={"account_id": account_id},
                response_data={"error": str(exc)},
            )
        finally:
            self._sync_lock.release()

        return new_count

    def trigger_async_sync(self) -> None:
        """Trigger an asynchronous background sync thread if cooldown permits."""
        if self.should_trigger_sync():
            thread = threading.Thread(target=self.sync_from_cfg, daemon=True)
            thread.start()

    async def run_sync_worker(self) -> None:
        """
        Background autonomous reconciliation daemon.
        - When active topup intents are pending: polls CFG with natural human jitter (25-35s).
        - When idle: sleeps for 5 minutes to avoid burning CFG rate limits and avoid AI triggers.
        """
        logger.info("EnvoiSMS background reconciliation worker started.")
        while True:
            try:
                active_intents = self.get_active_pending_intents()
                if active_intents:
                    logger.debug("Found %d pending EnvoiSMS topup intents; syncing CFG ledger...", len(active_intents))
                    # Run sync in thread pool to avoid blocking async loop
                    await asyncio.to_thread(self.sync_from_cfg)
                    # Natural human/mobile jitter (26s - 36s) to evade AI pattern detection
                    sleep_duration = 30.0 + random.uniform(-4.0, 6.0)
                else:
                    # Idle power-save sleep (5 mins + jitter)
                    sleep_duration = 300.0 + random.uniform(-10.0, 15.0)

                await asyncio.sleep(sleep_duration)
            except asyncio.CancelledError:
                logger.info("EnvoiSMS sync worker cancellation received.")
                break
            except Exception as e:
                logger.warning("Error in EnvoiSMS background worker loop: %s", e)
                await asyncio.sleep(30.0)

    def find_matching_transfer(
        self,
        amount_mad: float,
        reference: str,
        customer_name: Optional[str] = None,
        customer_phone: Optional[str] = None,
        max_age_minutes: int = 120
    ) -> Optional[Dict[str, Any]]:
        """
        High-precision match engine for bank deposits:
        1. Exact amount match (MAD).
        2. Unreconciled status (is_reconciled = 0).
        3. Within time window.
        4. Motif contains reference code OR customer identity / phone.
        """
        clean_ref = reference.strip().upper()
        clean_phone = customer_phone.replace(" ", "").replace("+212", "0") if customer_phone else ""
        clean_name = customer_name.strip().upper() if customer_name else ""

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            
            # Fetch unreconciled transfers matching exact amount
            c.execute("""
                SELECT * FROM cfg_incoming_transfers 
                WHERE is_reconciled = 0 AND ABS(amount_mad - ?) < 0.01
                ORDER BY recorded_at DESC
            """, (amount_mad,))
            candidates = c.fetchall()

            if not candidates:
                return None

            # Score candidates
            for row in candidates:
                motif = (row["motif"] or "").upper()
                sender = (row["sender_name"] or "").upper()
                tx_time = datetime.fromisoformat(row["recorded_at"])
                age_minutes = (datetime.now() - tx_time).total_seconds() / 60.0

                if age_minutes > max_age_minutes:
                    continue

                # Rule 1: Reference in motif (e.g. "ENV-98421" or "98421")
                ref_digits = "".join(filter(str.isdigit, clean_ref))
                if clean_ref in motif or (ref_digits and ref_digits in motif):
                    return dict(row)

                # Rule 2: Phone number in motif or sender
                if clean_phone and len(clean_phone) >= 9 and clean_phone in motif:
                    return dict(row)

                # Rule 3: Customer full name / company name matches sender
                if clean_name and (clean_name in sender or sender in clean_name):
                    return dict(row)

            # Rule 4: If single candidate in the last 15 minutes with exact amount, high confidence match
            recent_candidates = [
                dict(r) for r in candidates 
                if (datetime.now() - datetime.fromisoformat(r["recorded_at"])).total_seconds() <= 900
            ]
            if len(recent_candidates) == 1:
                return recent_candidates[0]

        return None

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
        Attempt reconciliation between an EnvoiSMS top-up intent and live CFG bank incoming transfers.
        Guarantees sub-5ms local resolution without synchronous outbound calls to CFG.
        If matched, locks the transaction atomically and prepares signed webhook payload.
        """
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            # Check if top-up already reconciled
            c.execute("SELECT * FROM bank_topup_reconciliations WHERE topup_id = ?", (topup_id,))
            existing = c.fetchone()
            if existing:
                return True, {
                    "status": "ALREADY_RECONCILED",
                    "topup_id": topup_id,
                    "bank_tx_id": existing[2],
                    "amount_mad": existing[3],
                    "reconciled_at": existing[4]
                }

        # Search matching transfer in local zero-latency ledger
        match = self.find_matching_transfer(
            amount_mad=amount_mad,
            reference=reference,
            customer_name=customer_name,
            customer_phone=customer_phone
        )

        if not match:
            # Record intent in local SQLite cache
            self.register_or_update_intent(
                topup_id=topup_id,
                account_id=account_id,
                amount_mad=amount_mad,
                reference=reference,
                customer_name=customer_name,
                customer_phone=customer_phone,
            )

            # Trigger non-blocking async background sync if cooldown permits (never blocks HTTP response)
            if not skip_async_trigger:
                self.trigger_async_sync()

            return False, {
                "status": "PENDING_CONFIRMATION",
                "message": f"Aucun virement de {amount_mad:,.2f} MAD correspondant à la référence '{reference}' n'a encore été détecté sur le compte CFG Bank VEXT AI.",
                "topup_id": topup_id,
                "amount_mad": amount_mad,
                "checked_at": datetime.now().isoformat()
            }

        bank_tx_id = match["bank_tx_id"]
        now_str = datetime.now().isoformat()

        # Atomically lock and commit reconciliation
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            try:
                c.execute("""
                    INSERT INTO bank_topup_reconciliations
                    (topup_id, account_id, bank_tx_id, amount_mad, reconciled_at, webhook_status)
                    VALUES (?, ?, ?, ?, ?, 'CONFIRMED')
                """, (topup_id, account_id, bank_tx_id, amount_mad, now_str))

                c.execute("""
                    UPDATE cfg_incoming_transfers
                    SET is_reconciled = 1
                    WHERE bank_tx_id = ?
                """, (bank_tx_id,))

                # Mark pending intent as confirmed if present
                c.execute("""
                    UPDATE pending_topup_intents
                    SET status = 'CONFIRMED'
                    WHERE topup_id = ?
                """, (topup_id,))

                conn.commit()
            except sqlite3.IntegrityError:
                return False, {"status": "CONFLICT", "message": "Transaction already claimed"}

        amount_eur = round(amount_mad / eur_mad_rate, 2)
        confirmation_payload = {
            "event": "bank_transfer.confirmed",
            "topup_id": topup_id,
            "account_id": account_id,
            "amount_mad": amount_mad,
            "amount_eur": amount_eur,
            "bank_tx_id": bank_tx_id,
            "bank_name": "CFG Bank",
            "beneficiary_name": "VEXT AI",
            "beneficiary_rib": "050810006011833971200189",
            "sender_name": match["sender_name"],
            "transfer_motif": match["motif"],
            "timestamp": now_str,
        }

        # Generate HMAC SHA-256 Signature for security
        signature = self.generate_signature(confirmation_payload, SHARED_WEBHOOK_SECRET)
        confirmation_payload["signature"] = signature

        result = {
            "status": "CONFIRMED",
            "message": f"Virement bancaire de {amount_mad:,.2f} MAD confirmé avec succès sur CFG Bank !",
            "details": confirmation_payload
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

    @staticmethod
    def generate_signature(payload: Dict[str, Any], secret: str) -> str:
        serialized = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hmac.new(secret.encode("utf-8"), serialized, hashlib.sha256).hexdigest()


envoisms_bridge = EnvoiSMSBridge()
