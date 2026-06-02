import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import BackgroundTasks
from app.core.models.tenant import TenantContext
from app.core.models.contracts import KPIHealthPayload
from app.core.models.events import SystemEvent
from app.services.webhook_dispatcher import WebhookDispatcher

class SentinelEngine:
    """
    Asynchronous CX Sentinel that monitors KPI health, raises internal
    system events on threshold breaches, and queues them for background fan-out.
    """

    @staticmethod
    async def evaluate_kpi_health(
        payload: KPIHealthPayload,
        tenant: TenantContext,
        dispatcher: WebhookDispatcher,
        background_tasks: BackgroundTasks,
    ) -> Dict[str, Any]:
        """
        Compare the incoming KPI payload against the tenant's critical thresholds.
        """
        sla_critical = tenant.sla_overrides.get("sla_critical_percent", 80.0)
        csat_critical = tenant.sla_overrides.get("csat_critical_percent", 75.0)

        breached = False
        events_dispatched = []

        if payload.sla_percent < sla_critical:
            breached = True
            event = SystemEvent(
                event_id=uuid.uuid4(),
                event_type="kpi.sla_breach",
                tenant_id=tenant.tenant_id,
                timestamp=datetime.now(timezone.utc),
                payload={
                    "client_id": payload.client_id,
                    "timestamp": payload.timestamp.isoformat(),
                    "sla_percent": payload.sla_percent,
                    "csat": payload.csat,
                    "threshold_sla": sla_critical,
                },
            )
            # 🚀 PERFORMANCE FIX: Hand the async job to the background thread!
            background_tasks.add_task(dispatcher.dispatch_event, event, tenant)
            events_dispatched.append(str(event.event_id))

        if payload.csat < csat_critical:
            breached = True
            event = SystemEvent(
                event_id=uuid.uuid4(),
                event_type="kpi.csat_breach",
                tenant_id=tenant.tenant_id,
                timestamp=datetime.now(timezone.utc),
                payload={
                    "client_id": payload.client_id,
                    "csat": payload.csat,
                    "threshold_csat": csat_critical,
                },
            )
            # 🚀 PERFORMANCE FIX: Hand the async job to the background thread!
            background_tasks.add_task(dispatcher.dispatch_event, event, tenant)
            events_dispatched.append(str(event.event_id))

        return {
            "status": "breached" if breached else "stable",
            "events_dispatched": events_dispatched,
            "details": {
                "client_id": payload.client_id,
                "sla_percent": payload.sla_percent,
                "csat": payload.csat,
                "thresholds": {
                    "sla_critical": sla_critical,
                    "csat_critical": csat_critical,
                },
            },
        }