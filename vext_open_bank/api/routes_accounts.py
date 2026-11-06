"""
routes_accounts.py
Account management, balances, RIB operations, and statements.
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, Any, List
from vext_open_bank.core.moroccan_banks import resolve_moroccan_bank
from vext_open_bank.services.data_store import data_store
from vext_open_bank.services.mobilefabric import mf_client

router = APIRouter(prefix="/accounts", tags=["Accounts & Ledger"])


@router.get("/")
def get_accounts() -> Dict[str, Any]:
    """Retrieve active bank account summaries, ledger balances, and IDs."""
    return data_store.get_account_details()


@router.get("/rib")
def get_account_rib() -> Dict[str, Any]:
    """Retrieve full Moroccan 24-digit RIB details and structure."""
    acc = data_store.get_account_details()
    return {
        "rib": acc["rib"],
        "formatted_rib": acc["formatted_rib"],
        "bank_code": acc["bank_code"],
        "bank_name": "CFG Bank",
        "branch_code": acc["branch_code"],
        "account_number": acc["account_number"],
        "rib_key": acc["rib_key"],
        "account_owner": "VEXT AI",
        "currency": acc["currency"],
    }


@router.post("/validate-rib")
def validate_rib(rib: str) -> Dict[str, Any]:
    """
    Validate a Moroccan 24-digit RIB using the standard Modulo 97 checksum formula.
    Formula: Key = 97 - ((89 * Bank + 15 * Branch + 3 * Account) % 97)
    """
    clean_rib = rib.replace(" ", "").strip()
    if len(clean_rib) != 24 or not clean_rib.isdigit():
        return {
            "is_valid": False,
            "rib": clean_rib,
            "error": "RIB must be exactly 24 numeric digits.",
        }

    bank = int(clean_rib[:3])
    branch = int(clean_rib[3:6])
    account = int(clean_rib[6:22])
    key = int(clean_rib[22:24])

    remainder = (89 * bank + 15 * branch + 3 * (account % 97)) % 97
    expected_key = 97 - remainder
    if expected_key == 97:
        expected_key = 0

    is_valid = (key == expected_key)
    bank_meta = resolve_moroccan_bank(clean_rib[:3])
    return {
        "rib": clean_rib,
        "is_valid": is_valid,
        "bank_code": clean_rib[:3],
        "bank_name": bank_meta["name"],
        "bank_full_name": bank_meta["full_name"],
        "supports_bam_instant": bank_meta.get("supports_instant", False),
        "branch_code": clean_rib[3:6],
        "account_number": clean_rib[6:22],
        "provided_key": key,
        "calculated_key": expected_key,
    }


@router.get("/overview-live")
def get_live_overview() -> Dict[str, Any]:
    """Invoke the live MobileFabric AccountOverview service on the gateway."""
    try:
        res = mf_client.invoke_service("CFGAccountOverview/AccountOverview")
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
