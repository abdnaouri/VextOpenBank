"""
routes_catalog.py
Discovered backend integration service catalog and utilities.
"""

from fastapi import APIRouter
from typing import Dict, Any, List
from vext_open_bank.services.mobilefabric import mf_client

router = APIRouter(prefix="/catalog", tags=["Backend Services & Billers"])


@router.get("/services")
def get_service_catalog() -> Dict[str, Any]:
    """Retrieve all 42 backend MobileFabric integration services discovered from the server."""
    try:
        data = mf_client.get_service_catalog()
        services = data.get("services_meta", {})
        return {
            "total_services": len(services),
            "app_id": data.get("appId"),
            "app_name": data.get("name"),
            "services": services,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/billers")
def get_billers() -> List[Dict[str, Any]]:
    """Retrieve Moroccan utility and telecom billers supported by CFG Bank."""
    return [
        {"code": "IAM", "name": "Maroc Telecom", "category": "Telecom"},
        {"code": "ORANGE", "name": "Orange Maroc", "category": "Telecom"},
        {"code": "INWI", "name": "Inwi", "category": "Telecom"},
        {"code": "LYDEC", "name": "Lydec Casablanca", "category": "Utilities (Water/Electricity)"},
        {"code": "REDAL", "name": "Redal Rabat-Salé", "category": "Utilities (Water/Electricity)"},
        {"code": "AMENDIS_TNG", "name": "Amendis Tanger", "category": "Utilities (Water/Electricity)"},
        {"code": "AMENDIS_TET", "name": "Amendis Tétouan", "category": "Utilities (Water/Electricity)"},
        {"code": "RAM", "name": "Royal Air Maroc", "category": "Travel"},
        {"code": "ONCF", "name": "ONCF Billetterie", "category": "Transport"},
        {"code": "DGI", "name": "Direction Générale des Impôts (Vignette)", "category": "Government Taxes"},
        {"code": "TGR", "name": "Trésorerie Générale du Royaume", "category": "Government Taxes"},
    ]
