import logging
import httpx
from app.core.models.events import SystemEvent
from app.core.models.tenant import TenantContext, WebhookDestination

logger = logging.getLogger(__name__)

class WebhookDispatchError(Exception):
    """Raised when one or more critical webhook destinations fail to respond."""
    pass

class WebhookDispatcher:
    """
    Asynchronously fan-out internal SystemEvents to registered tenant destinations.
    """
    async def dispatch_event(self, event: SystemEvent, tenant: TenantContext) -> None:
        destinations = tenant.get_active_webhooks_for_event(event.canonical_type)
        if not destinations:
            logger.debug(f"No active webhooks registered for event '{event.canonical_type}' on tenant '{tenant.tenant_id}'.")
            return

        async with httpx.AsyncClient() as client:
            for dest in destinations:
                await self._send_webhook(client, dest, event)

    async def _send_webhook(self, client: httpx.AsyncClient, destination: WebhookDestination, event: SystemEvent) -> None:
        headers = {"Content-Type": "application/json"}
        
        # Inject configured auth headers
        if destination.auth_scheme == "bearer" and destination.auth_secret:
            headers["Authorization"] = f"Bearer {destination.auth_secret}"
        elif destination.auth_scheme == "basic" and destination.auth_secret:
            headers["Authorization"] = f"Basic {destination.auth_secret}"

        try:
            logger.info(f"Dispatching webhook '{destination.name}' to {destination.url}")
            response = await client.post(
                str(destination.url),
                json=event.model_dump(mode="json"),
                headers=headers,
                timeout=destination.timeout_seconds
            )
            response.raise_for_status() # Catch HTTP errorsCatch HTTP errors
        except httpx.HTTPStatusError as exc:
            logger.error(f"Webhook '{destination.name}' returned error status {exc.response.status_code}")
        except httpx.RequestError as exc:
            logger.error(f"Failed to reach webhook destination '{destination.name}' at {destination.url}: {exc}")