"""
registry.py
Multi-Tenant Bank Connector Registry & Dispatcher.

Orchestrates statement synchronization and API connectors across
tenants who bank with different institutions (Attijariwafa, CDM, BCP, CIH, CFG).
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from vext_open_bank.services.bank_connectors.base import BaseBankConnector
from vext_open_bank.services.bank_connectors.attijari_connector import AttijariwafaConnector
from vext_open_bank.services.bank_connectors.cdm_connector import CreditDuMarocConnector

logger = logging.getLogger("BankRegistry")


class BankConnectorRegistry:
    def __init__(self):
        # Register supported bank connectors
        self._connectors: Dict[str, BaseBankConnector] = {
            "007": AttijariwafaConnector(),
            "078": CreditDuMarocConnector(),
        }

        # Tenant bank connections: tenant_prefix -> connection config
        # Example: 'ENV' -> {'bank_code': '007', 'rib': '007780000000123456789012', ...}
        self._tenant_configs: Dict[str, Dict[str, Any]] = {
            "ENV": {
                "tenant_id": "ENV",
                "bank_code": "051", # Default CFG
                "rib": "050810006011833971200189",
                "name": "EnvoiSMS.ma Primary Treasury",
            },
            "MED": {
                "tenant_id": "MED",
                "bank_code": "007", # Attijariwafa Bank
                "rib": "007780000000123456789012",
                "name": "MediUnit Attijari Treasury",
            },
            "BAC": {
                "tenant_id": "BAC",
                "bank_code": "078", # Crédit du Maroc
                "rib": "078780000000123456789078",
                "name": "BacEnPoche CDM Treasury",
            },
        }

    def get_connector(self, bank_code: str) -> Optional[BaseBankConnector]:
        """Retrieve connector instance for a given 3-digit Moroccan bank code."""
        return self._connectors.get(bank_code)

    def register_tenant_bank_account(
        self,
        tenant_id: str,
        bank_code: str,
        account_rib: str,
        name: Optional[str] = None,
        credentials: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register or update a SaaS tenant's dedicated receiving bank account."""
        self._tenant_configs[tenant_id.upper()] = {
            "tenant_id": tenant_id.upper(),
            "bank_code": bank_code,
            "rib": account_rib,
            "name": name or f"{tenant_id} {bank_code} Treasury",
            "credentials": credentials or {},
        }

    def get_tenant_bank_config(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve registered bank account config for a tenant."""
        return self._tenant_configs.get(tenant_id.upper())

    def list_all_tenant_configs(self) -> List[Dict[str, Any]]:
        """List all registered SaaS tenant treasury configurations."""
        return list(self._tenant_configs.values())

    def parse_and_ingest_statement(
        self,
        bank_code: str,
        statement_content: str,
        file_format: str = "MT940",
        recipient_rib: Optional[str] = None,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """
        Universal Statement Ingestion:
        Takes an MT940 or CAMT.053 file downloaded from a tenant's bank portal (Attijari, CDM, etc.)
        and automatically parses and commits all incoming wires into VextOpenBank's reconciliation engine.
        """
        connector = self.get_connector(bank_code)
        if not connector:
            # Fallback to generic base connector with provided bank_code
            class GenericConnector(BaseBankConnector):
                def sync_live_transactions(self, r, c): return []
            connector = GenericConnector(bank_code=bank_code, bank_name=f"Bank {bank_code}")

        fmt = file_format.upper()
        if fmt == "CAMT053" or fmt == "CAMT.053" or statement_content.strip().startswith("<?xml") or "<Document" in statement_content:
            transfers = connector.parse_camt053_xml(statement_content, default_recipient_rib=recipient_rib)
        else:
            transfers = connector.parse_mt940_statement(statement_content, default_recipient_rib=recipient_rib)

        ingested_count = connector.ingest_transfers(transfers)
        return ingested_count, transfers


bank_registry = BankConnectorRegistry()
