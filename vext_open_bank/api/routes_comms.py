"""
routes_comms.py
Inbox, CRM ticketing, notifications, and push subscriptions.
MobileFabric services:
  CFGDetailMessages, CFGMessagesMultiActions, CFGNotificationListAction,
  Incident, IncidentDynamics, CFGSubscribepushNotification,
  CFGUnsubscribepushNotification, KPNS
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from vext_open_bank.services.mobilefabric import mf_client

router = APIRouter(prefix="/comms", tags=["Communications, CRM & Inbox"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class InboxBatchRequest(BaseModel):
    message_ids: List[str] = Field(..., description="List of message IDs to act on")
    action: str = Field(
        ...,
        description="Action to perform: 'read', 'archive', or 'delete'",
    )


class IncidentCreateRequest(BaseModel):
    subject: str = Field(..., description="Incident subject / title")
    description: str = Field(..., description="Detailed description of the problem")
    category: Optional[str] = Field("GENERAL", description="Incident category code")
    priority: Optional[str] = Field("NORMAL", description="'LOW', 'NORMAL', or 'HIGH'")


class PushSubscribeRequest(BaseModel):
    device_token: str = Field(..., description="FCM / APNS device push token")
    platform: str = Field("android", description="'android' or 'ios'")


class PushUnsubscribeRequest(BaseModel):
    device_token: str = Field(..., description="Device push token to remove")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/inbox", summary="Fetch Secure Bank Inbox (CFGDetailMessages)")
def get_inbox() -> Dict[str, Any]:
    """
    Retrieve bank notices, transaction alerts, and advisor messages from
    `/services/CFGDetailMessages`.
    """
    try:
        return mf_client.invoke_service("CFGDetailMessages")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/inbox/batch", summary="Bulk Inbox Actions (CFGMessagesMultiActions)")
def inbox_batch_action(req: InboxBatchRequest) -> Dict[str, Any]:
    """
    Perform a bulk action (mark-read, archive, delete) on multiple inbox
    messages via `/services/CFGMessagesMultiActions`.
    """
    valid_actions = {"read", "archive", "delete"}
    if req.action not in valid_actions:
        raise HTTPException(
            status_code=400,
            detail=f"action must be one of {sorted(valid_actions)}",
        )
    try:
        return mf_client.invoke_service(
            "CFGMessagesMultiActions",
            params={
                "messageIds": ",".join(req.message_ids),
                "action": req.action,
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/notifications", summary="List Notification Actions (CFGNotificationListAction)")
def list_notifications() -> Dict[str, Any]:
    """
    Retrieve pending notification actions from
    `/services/CFGNotificationListAction`.
    """
    try:
        return mf_client.invoke_service("CFGNotificationListAction")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/incidents", summary="File Support Ticket (Incident + IncidentDynamics)")
def create_incident(req: IncidentCreateRequest) -> Dict[str, Any]:
    """
    Open a support incident in CFG Bank's Microsoft Dynamics CRM via
    `/services/Incident` and `/services/IncidentDynamics`.

    The incident is first created in the MobileFabric Incident service, then
    propagated to Dynamics CRM via IncidentDynamics for SLA tracking.
    """
    try:
        incident_result = mf_client.invoke_service(
            "Incident",
            params={
                "subject": req.subject,
                "description": req.description,
                "category": req.category,
                "priority": req.priority,
            },
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Incident creation failed: {exc}",
        ) from exc

    # Propagate to Dynamics CRM (best-effort)
    dynamics_result: Optional[Dict] = None
    try:
        incident_id = incident_result.get("incidentId") or incident_result.get("id")
        if incident_id:
            dynamics_result = mf_client.invoke_service(
                "IncidentDynamics",
                params={"incidentId": incident_id},
            )
    except Exception as exc:
        dynamics_result = {"error": str(exc), "note": "CRM propagation failed — incident still created"}

    return {
        "incident": incident_result,
        "dynamics_crm": dynamics_result,
    }


@router.post("/push/subscribe", summary="Subscribe to Push Notifications (CFGSubscribepushNotification + KPNS)")
def subscribe_push(req: PushSubscribeRequest) -> Dict[str, Any]:
    """
    Register a device for push notifications via
    `/services/CFGSubscribepushNotification` and the Kony Push Notification
    Service (`KPNS`).
    """
    try:
        result = mf_client.invoke_service(
            "CFGSubscribepushNotification",
            params={
                "deviceToken": req.device_token,
                "platform": req.platform,
            },
        )
        # Also register with KPNS
        try:
            kpns_result = mf_client.invoke_service(
                "KPNS",
                params={
                    "deviceToken": req.device_token,
                    "platform": req.platform,
                    "action": "subscribe",
                },
            )
            result["kpns"] = kpns_result
        except Exception as kpns_exc:
            result["kpns_error"] = str(kpns_exc)
        return result
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/push/unsubscribe", summary="Unsubscribe from Push Notifications (CFGUnsubscribepushNotification)")
def unsubscribe_push(req: PushUnsubscribeRequest) -> Dict[str, Any]:
    """
    Remove a device from push notification subscriptions via
    `/services/CFGUnsubscribepushNotification`.
    """
    try:
        return mf_client.invoke_service(
            "CFGUnsubscribepushNotification",
            params={"deviceToken": req.device_token},
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
