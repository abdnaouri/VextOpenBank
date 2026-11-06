"""
base.py
Base Bank Connector Interface & Statement Parsing Engine.

Provides normalized abstractions for connecting SaaS tenant accounts
across Moroccan banks (Attijariwafa Bank, Crédit du Maroc, CIH, BCP, CFG).
"""

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
import logging
import re
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET

from vext_open_bank.services.envoisms_bridge import envoisms_bridge

logger = logging.getLogger("BankConnectors")


class BaseBankConnector(ABC):
    """
    Abstract Base Class for Moroccan Commercial Bank Connectors.
    Supports both Direct API Emulation and EBICS (MT940 / CAMT.053) Statement Ingestion.
    """

    def __init__(self, bank_code: str, bank_name: str):
        self.bank_code = bank_code
        self.bank_name = bank_name

    @abstractmethod
    def sync_live_transactions(self, account_rib: str, credentials: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Pull recent incoming transactions using tenant's API credentials or online banking session.
        """
        pass

    def parse_mt940_statement(self, mt940_text: str, default_recipient_rib: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Parse SWIFT MT940 statement text (the EBICS corporate standard used by
        Attijariwafa Bank, BCP, BMCE, SGMB, and Crédit du Maroc).

        Extracts statement lines (:61:) and unstructured narrative (:86:) motifs.
        """
        transfers: List[Dict[str, Any]] = []
        recipient_rib = default_recipient_rib or f"{self.bank_code}78000000000000000000"

        # Match account tag :25: if present
        acc_match = re.search(r":25:([0-9A-Z/\s]+)", mt940_text)
        if acc_match:
            clean_acc = acc_match.group(1).replace(" ", "").replace("/", "").strip()
            if len(clean_acc) == 24 and clean_acc.isdigit():
                recipient_rib = clean_acc

        # Parse :61: lines (Statement Line) and :86: lines (Information to Account Owner)
        # Regex for :61: line: Date (YYMMDD), EntryDate (optional MMDD), C/D, Amount (with comma)
        # Example :61:2611061106CR18000,00NTRFNONREF
        lines = mt940_text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith(":61:"):
                raw_61 = line[4:]
                m61 = re.match(
                    r"^(\d{6})(?:\d{4})?(C|CR|D|DR)([A-Z]{0,3})(\d+,\d{2})(?:[A-Z0-9]+)?",
                    raw_61,
                )
                if m61:
                    date_str = m61.group(1) # YYMMDD
                    cd_ind = m61.group(2)
                    amount_str = m61.group(4).replace(",", ".")
                    amount_float = float(amount_str)

                    # We only reconcile credits (CRDT / incoming wires)
                    is_credit = cd_ind in ("C", "CR")

                    # Look ahead for :86: narrative
                    motif = "Virement Bancaire"
                    if i + 1 < len(lines) and lines[i + 1].strip().startswith(":86:"):
                        motif = lines[i + 1].strip()[4:].strip()
                        i += 1

                    if is_credit:
                        # Convert YYMMDD to ISO date
                        iso_date = f"20{date_str[:2]}-{date_str[2:4]}-{date_str[4:6]}"
                        tx_id = f"{self.bank_code}-MT940-{date_str}-{abs(hash(motif + amount_str)) % 1000000:06d}"
                        transfers.append({
                            "bank_tx_id": tx_id,
                            "amount_mad": amount_float,
                            "sender_name": "Tiers Émetteur",
                            "motif": motif,
                            "sender_rib": None,
                            "recipient_rib": recipient_rib,
                            "recipient_bank_code": self.bank_code,
                            "value_date": iso_date,
                        })
            i += 1

        return transfers

    def parse_camt053_xml(self, xml_content: str, default_recipient_rib: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Parse ISO 20022 CAMT.053 XML Bank-to-Customer Statement file
        (supported by Crédit du Maroc, BMCI, and modern EBICS corporate gateways).
        """
        transfers: List[Dict[str, Any]] = []
        recipient_rib = default_recipient_rib or f"{self.bank_code}78000000000000000000"

        try:
            # Strip XML namespaces for simplified element matching
            clean_xml = re.sub(r'xmlns(:\w+)?="[^"]+"', '', xml_content)
            root = ET.fromstring(clean_xml)

            # Find IBAN/BBAN
            acc_elem = root.find(".//Acct/Id/Othr/Id")
            if acc_elem is None:
                acc_elem = root.find(".//Acct/Id/IBAN")
            if acc_elem is not None and acc_elem.text:
                acc_text = acc_elem.text.replace(" ", "").replace("MA64", "").strip()
                if len(acc_text) == 24 and acc_text.isdigit():
                    recipient_rib = acc_text

            # Iterate over Entries (<Ntry>)
            for ntry in root.findall(".//Ntry"):
                # Credit/Debit Indicator
                cdt_dbt = ntry.find("CdtDbtInd")
                if cdt_dbt is None or cdt_dbt.text != "CRDT":
                    continue

                # Amount
                amt_elem = ntry.find("Amt")
                if amt_elem is None or not amt_elem.text:
                    continue
                amount_mad = float(amt_elem.text.strip())

                # Value Date
                val_date = datetime.now().strftime("%Y-%m-%d")
                dt_elem = ntry.find(".//ValDt/Dt")
                if dt_elem is None:
                    dt_elem = ntry.find(".//BookgDt/Dt")
                if dt_elem is not None and dt_elem.text:
                    val_date = dt_elem.text.strip()

                # Narrative / Motif
                ustrd_elem = ntry.find(".//RmtInf/Ustrd")
                motif = ustrd_elem.text.strip() if ustrd_elem is not None and ustrd_elem.text else "Virement CAMT.053"

                # Sender Name
                sender_name = "Tiers Émetteur"
                dbtr_elem = ntry.find(".//RltdPties/Dbtr/Nm")
                if dbtr_elem is not None and dbtr_elem.text:
                    sender_name = dbtr_elem.text.strip()

                # Sender RIB
                sender_rib = None
                s_rib_elem = ntry.find(".//RltdPties/DbtrAcct/Id/Othr/Id")
                if s_rib_elem is not None and s_rib_elem.text:
                    sender_rib = s_rib_elem.text.strip()

                tx_id = f"{self.bank_code}-CAMT-{abs(hash(motif + str(amount_mad) + val_date)) % 10000000:07d}"
                transfers.append({
                    "bank_tx_id": tx_id,
                    "amount_mad": amount_mad,
                    "sender_name": sender_name,
                    "motif": motif,
                    "sender_rib": sender_rib,
                    "recipient_rib": recipient_rib,
                    "recipient_bank_code": self.bank_code,
                    "value_date": val_date,
                })
        except Exception as exc:
            logger.error("CAMT.053 XML parse error for bank %s: %s", self.bank_code, exc)

        return transfers

    def ingest_transfers(self, transfers: List[Dict[str, Any]]) -> int:
        """
        Commit extracted transfers into the unified VextOpenBank SQLite ledger.
        Returns count of successfully recorded transfers.
        """
        count = 0
        for tx in transfers:
            envoisms_bridge.record_incoming_bank_transfer(
                bank_tx_id=tx["bank_tx_id"],
                amount_mad=tx["amount_mad"],
                sender_name=tx.get("sender_name", "Tiers Émetteur"),
                motif=tx.get("motif", ""),
                sender_rib=tx.get("sender_rib"),
                recipient_rib=tx.get("recipient_rib", f"{self.bank_code}78000000000000000000"),
                recipient_bank_code=self.bank_code,
                value_date=tx.get("value_date"),
            )
            count += 1
        return count
