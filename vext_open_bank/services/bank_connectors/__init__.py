"""
bank_connectors package
"""

from vext_open_bank.services.bank_connectors.base import BaseBankConnector
from vext_open_bank.services.bank_connectors.attijari_connector import AttijariwafaConnector
from vext_open_bank.services.bank_connectors.cdm_connector import CreditDuMarocConnector
from vext_open_bank.services.bank_connectors.registry import bank_registry, BankConnectorRegistry

__all__ = [
    "BaseBankConnector",
    "AttijariwafaConnector",
    "CreditDuMarocConnector",
    "bank_registry",
    "BankConnectorRegistry",
]
