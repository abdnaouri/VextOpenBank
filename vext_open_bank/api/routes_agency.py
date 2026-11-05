"""
routes_agency.py
Branch & ATM locators, advisor appointments, and live market data.
MobileFabric services:
  CFGBankLocations, CFGLocationServices,
  CFGMeetingRequestMultipleDel, PrePublicLandingService
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional

from vext_open_bank.services.mobilefabric import mf_client

router = APIRouter(prefix="/agency", tags=["Branches, Appointments & Market Data"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class AppointmentRequest(BaseModel):
    action: str = Field(
        "book",
        description="'book' to create a new appointment, 'cancel' to delete one",
    )
    branch_code: Optional[str] = Field(None, description="Target branch code")
    advisor_code: Optional[str] = Field(None, description="Assigned relationship manager code")
    preferred_date: Optional[str] = Field(None, description="ISO date, e.g. 2026-12-01")
    preferred_time: Optional[str] = Field(None, description="HH:MM, e.g. 10:30")
    appointment_id: Optional[str] = Field(None, description="Required when action='cancel'")
    subject: Optional[str] = Field("Rendez-vous bancaire", description="Meeting subject")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/market-rates", summary="Live BAM FX Rates & Market Data (PrePublicLandingService)")
def get_market_rates() -> Dict[str, Any]:
    """
    Pull live Moroccan foreign exchange rates (MAD) and market indices from
    `/services/PrePublicLandingService` (unauthenticated public endpoint).

    Data sourced from Bank Al-Maghrib (BAM) official rates.
    """
    try:
        return mf_client.invoke_service("PrePublicLandingService")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/locations", summary="Branch & ATM Locator (CFGBankLocations + CFGLocationServices)")
def get_locations(
    cash_deposit_enabled: bool = Query(
        False,
        description="Filter to ATMs that support cash deposits",
    ),
    city: Optional[str] = Query(None, description="Filter by city name"),
) -> Dict[str, Any]:
    """
    Return branch and ATM locations from `/services/CFGBankLocations` and
    `/services/CFGLocationServices`.

    Use `cash_deposit_enabled=true` to narrow results to cash-deposit-capable
    ATMs for automated cash reconciliation workflows.
    """
    params: Dict[str, Any] = {}
    if cash_deposit_enabled:
        params["cashDepositEnabled"] = "true"
    if city:
        params["city"] = city

    results: Dict[str, Any] = {}

    try:
        results["branches"] = mf_client.invoke_service("CFGBankLocations", params=params)
    except Exception as exc:
        results["branches_error"] = str(exc)

    try:
        results["atms"] = mf_client.invoke_service("CFGLocationServices", params=params)
    except Exception as exc:
        results["atms_error"] = str(exc)

    if "branches_error" in results and "atms_error" in results:
        raise HTTPException(
            status_code=502,
            detail=f"Both location services failed: {results}",
        )

    return results


@router.post("/appointments", summary="Book or Cancel Branch Appointment (CFGMeetingRequestMultipleDel)")
def manage_appointment(req: AppointmentRequest) -> Dict[str, Any]:
    """
    Book or cancel a branch appointment with the assigned relationship manager
    via `/services/CFGMeetingRequestMultipleDel`.

    - Set `action='book'` with `branch_code`, `preferred_date`, and `preferred_time`.
    - Set `action='cancel'` with `appointment_id`.
    """
    if req.action not in {"book", "cancel"}:
        raise HTTPException(
            status_code=400,
            detail="action must be 'book' or 'cancel'",
        )

    if req.action == "cancel" and not req.appointment_id:
        raise HTTPException(
            status_code=400,
            detail="appointment_id is required when action='cancel'",
        )

    if req.action == "book" and not req.preferred_date:
        raise HTTPException(
            status_code=400,
            detail="preferred_date is required when action='book'",
        )

    params: Dict[str, str] = {
        "action": req.action,
        "subject": req.subject or "",
    }
    if req.branch_code:
        params["branchCode"] = req.branch_code
    if req.advisor_code:
        params["advisorCode"] = req.advisor_code
    if req.preferred_date:
        params["preferredDate"] = req.preferred_date
    if req.preferred_time:
        params["preferredTime"] = req.preferred_time
    if req.appointment_id:
        params["appointmentId"] = req.appointment_id

    try:
        return mf_client.invoke_service("CFGMeetingRequestMultipleDel", params=params)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
