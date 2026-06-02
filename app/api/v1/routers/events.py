import uuid
from datetime import datetime, timezone
from typing import Dict, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.core.config_manager import ConfigManager, get_config_manager
from app.core.models.events import SystemEvent
from app.services.webhook_dispatcher import WebhookDispatcher, WebhookDispatchError

router = APIRouter(
    prefix="/api/v1/events",
    tags=["Internal Events"],
    responses={404: {"description": "Tenant not found"}, 422: {"description": "Validation error"}},
)

class EventDispatchRequest(BaseModel):
    event_type: str = Field(..., min_length=1, description="Dotted event type, e.g. 'kpi.sla_breach'")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Event specific data")

webhook_dispatcher = WebhookDispatcher()

@router.post(
    "/dispatch",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Dispatch an internal system event to registered webhooks",
)
async def dispatch_internal_event(
    event_request: EventDispatchRequest = Body(...),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    config_manager: ConfigManager = Depends(get_config_manager),
):
    """
    Accept an internal event, validate it into a canonical SystemEvent, 
    and fan it out to all active webhook destinations.
    """
    # Await async resolution. Core main.py handles exceptions globally.
    tenant = await config_manager.get_tenant(x_tenant_id)

    system_event = SystemEvent(
        event_id=uuid.uuid4(),
        event_type=event_request.event_type,
        tenant_id=x_tenant_id,
        timestamp=datetime.now(timezone.utc),
        payload=event_request.payload,
    )

    try:
        await webhook_dispatcher.dispatch_event(system_event, tenant)
    except WebhookDispatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )

    return {
        "status": "dispatched",
        "event_id": str(system_event.event_id),
        "tenant_id": x_tenant_id,
    }