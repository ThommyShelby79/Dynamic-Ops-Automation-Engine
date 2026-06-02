import logging
from app.core.models.contracts import AdherenceEventPayload

logger = logging.getLogger(__name__)

async def persist_adherence_event(payload: AdherenceEventPayload, tenant_id: str) -> None:
    """
    Background worker to persist adherence events to the database.
    This runs outside the request/response cycle.
    """
    try:
        # Simulate database I/O latency
        # In a real environment, you'd await db.execute(...) here
        logger.info(
            "Background Task: Persisting adherence event for tenant %s. "
            "Agent: %s, State: %s",
            tenant_id,
            payload.agent_id,
            payload.state_slug
        )
    except Exception as e:
        logger.error("Background Task: Failed to persist event: %s", str(e))