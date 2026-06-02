import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from pydantic import BaseModel, ConfigDict, Field

class SystemEvent(BaseModel):
    """
    Canonical internal event structure.
    This model is immutable to guarantee that events are never modified
    after creation, which is critical for audit trails and replay.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4, description="Unique event identifier")
    event_type: str = Field(..., min_length=1, description="Dotted event type, e.g. 'kpi.sla_breach'")
    tenant_id: str = Field(..., min_length=1)
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the event was created"
    )
    payload: Dict[str, Any] = Field(..., description="Arbitrary event-specific data")

    @property
    def canonical_type(self) -> str:
        """Normalised event type string for routing purposes."""
        return self.event_type.lower().strip()