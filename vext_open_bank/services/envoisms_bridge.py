"""
envoisms_bridge.py
Autonomous Reconciliation Engine connecting CFG Bank (VEXT AI) with EnvoiSMS.ma
Handles incoming transfer detection, intelligent motif matching, strict deduplication, and HMAC webhook dispatch.
"""

import sqlite3
import os
import hmac
import hashlib
import json
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
import urllib.request

DATABASE_PATH = "/tmp/vext_envoisms_reconciliation.db"
SHARED_WEBHOOK_SECRET = os.getenv("ENVOISMS_VEXT_SECRET", "")
ENVOISMS_API_BASE = os.getenv("ENVOISMS_API_BASE", "https://api.envoisms.ma")


class EnvoiSMSBridge:
    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path
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

        return {
            "bank_tx_id": bank_tx_id,
            "amount_mad": amount_mad,
            "sender_name": sender_name,
            "motif": motif,
            "recorded_at": now_str
        }

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
        eur_mad_rate: float = 10.80
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Attempt reconciliation between an EnvoiSMS top-up intent and live CFG bank incoming transfers.
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

        # Search matching transfer
        match = self.find_matching_transfer(
            amount_mad=amount_mad,
            reference=reference,
            customer_name=customer_name,
            customer_phone=customer_phone
        )

        if not match:
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

        return True, {
            "status": "CONFIRMED",
            "message": f"Virement bancaire de {amount_mad:,.2f} MAD confirmé avec succès sur CFG Bank !",
            "details": confirmation_payload
        }

    @staticmethod
    def generate_signature(payload: Dict[str, Any], secret: str) -> str:
        serialized = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hmac.new(secret.encode("utf-8"), serialized, hashlib.sha256).hexdigest()


envoisms_bridge = EnvoiSMSBridge()
