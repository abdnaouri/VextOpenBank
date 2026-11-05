"""
routes_cards.py
Card operations, status queries, and limits.
"""

from fastapi import APIRouter
from typing import Dict, Any, List
from vext_open_bank.services.data_store import data_store

router = APIRouter(prefix="/cards", tags=["Cards Management"])


@router.get("/")
def get_cards() -> List[Dict[str, Any]]:
    """Retrieve all payment cards, statuses, and linked accounts."""
    return data_store.get_cards()


@router.get("/limits")
def get_card_limits() -> Dict[str, Any]:
    """Retrieve default and configured ATM withdrawal and POS purchase limits."""
    return {
        "card_id": "card_01",
        "currency": "MAD",
        "limits": {
            "atm_withdrawal_daily": 10000.0,
            "atm_withdrawal_weekly": 30000.0,
            "pos_payment_daily": 25000.0,
            "pos_payment_monthly": 100000.0,
            "e_commerce_enabled": True,
            "contactless_enabled": True,
            "international_enabled": False,
        },
    }


@router.post("/{card_id}/toggle-lock")
def toggle_card_lock(card_id: str, lock: bool) -> Dict[str, Any]:
    """Temporarily lock or unlock a payment card."""
    return {
        "card_id": card_id,
        "is_locked": lock,
        "status": "Bloquée" if lock else "Active",
        "message": f"Card {card_id} successfully {'locked' if lock else 'unlocked'}.",
    }
