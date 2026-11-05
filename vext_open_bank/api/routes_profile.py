"""
routes_profile.py
Customer profile, advisor contact, and branch routing.
"""

from fastapi import APIRouter
from typing import Dict, Any
from vext_open_bank.services.data_store import data_store

router = APIRouter(prefix="/profile", tags=["Customer Profile & Advisor"])


@router.get("/")
def get_profile() -> Dict[str, Any]:
    """Retrieve client identity, assigned relationship manager, and managing branch."""
    return data_store.get_user_profile()


@router.get("/advisor")
def get_advisor() -> Dict[str, Any]:
    """Retrieve dedicated relationship manager contact information."""
    prof = data_store.get_user_profile()
    return prof.get("relationship_manager", {})


@router.get("/branch")
def get_branch() -> Dict[str, Any]:
    """Retrieve managing branch location, email, and phone."""
    prof = data_store.get_user_profile()
    return prof.get("branch", {})
