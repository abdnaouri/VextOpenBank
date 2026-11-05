"""
routes_pfm.py
Personal Finance Management (PFM) and analytics.
MobileFabric services: CFGDataServices, CFGPFMDetail, CFGSetCategory
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from vext_open_bank.services.data_store import data_store
from vext_open_bank.services.mobilefabric import mf_client

router = APIRouter(prefix="/pfm", tags=["Personal Finance Management"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CategoryPatchRequest(BaseModel):
    category_code: str = Field(..., description="New PFM category code to assign")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/categories", summary="PFM Expense Category Taxonomy")
def get_categories() -> List[Dict[str, str]]:
    """Retrieve categorized expense taxonomy (live from CFGDataServices, with SQLite fallback)."""
    return data_store.get_pfm_categories()


@router.get("/evolution", summary="30-Day Liquidity Evolution (CFGDataServices)")
def get_pfm_evolution() -> Dict[str, Any]:
    """
    Expose the 30-day daily liquidity curve and category percentages.

    Calls `/services/CFGDataServices` with both `cfgCashBankEvolutionChart`
    and `cfgPFMDonut` operations.
    """
    result: Dict[str, Any] = {}

    try:
        result["cash_evolution"] = mf_client.invoke_service(
            "CFGDataServices/cfgCashBankEvolutionChart", api_version="1.1"
        )
    except Exception as exc:
        result["cash_evolution_error"] = str(exc)

    try:
        result["pfm_donut"] = mf_client.invoke_service(
            "CFGDataServices/cfgPFMDonut", api_version="1.1"
        )
    except Exception as exc:
        result["pfm_donut_error"] = str(exc)

    if "cash_evolution_error" in result and "pfm_donut_error" in result:
        raise HTTPException(
            status_code=502,
            detail="Both CFGDataServices operations failed. Check gateway connectivity.",
        )

    return result


@router.get("/transactions", summary="PFM Transaction Drilldown (CFGPFMDetail)")
def get_pfm_transactions(
    category_code: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Granular line-item drilldown per budget category from
    `/services/CFGPFMDetail`.

    Optionally filter by `category_code` (e.g. 'alimentation', 'rent').
    """
    params: Dict[str, str] = {}
    if category_code:
        params["categoryCode"] = category_code
    try:
        return mf_client.invoke_service("CFGPFMDetail", params=params)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.patch(
    "/transactions/{tx_id}/category",
    summary="Re-classify a Transaction Category (CFGSetCategory)",
)
def patch_transaction_category(
    tx_id: str,
    req: CategoryPatchRequest,
) -> Dict[str, Any]:
    """
    Re-assign the PFM category of a specific transaction programmatically
    (e.g. from an AI bookkeeping agent) via `/services/CFGSetCategory`.
    """
    try:
        return mf_client.invoke_service(
            "CFGSetCategory",
            params={
                "transactionId": tx_id,
                "categoryCode": req.category_code,
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Legacy live endpoints (kept for backward-compat)
# ---------------------------------------------------------------------------

@router.get("/donut-live", summary="Live PFM Donut Chart (legacy alias)")
def get_live_pfm_donut() -> Dict[str, Any]:
    """Query live PFM donut chart category breakdown from MobileFabric."""
    try:
        return mf_client.invoke_service("CFGDataServices/cfgPFMDonut", api_version="1.1")
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@router.get("/evolution-live", summary="Live 30-Day Cash Evolution (legacy alias)")
def get_live_cash_evolution() -> Dict[str, Any]:
    """Query live 30-day cash and bank balance evolution chart from MobileFabric."""
    try:
        return mf_client.invoke_service(
            "CFGDataServices/cfgCashBankEvolutionChart", api_version="1.1"
        )
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
