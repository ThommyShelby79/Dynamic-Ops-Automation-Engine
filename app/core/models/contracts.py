from datetime import datetime
from typing import List

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VolumeForecastPayload(BaseModel):
    """
    Immutable schema for WFM forecasting payloads.

    Attributes:
        interval_minutes: Forecast bucket size in minutes (>0).
        aht: Average Handling Time in seconds (>0).
        historical_volumes: Non‑empty sequence of historical volume observations.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_minutes: int = Field(..., gt=0, description="Forecast interval in minutes")
    aht: float = Field(..., gt=0, description="Average Handling Time (seconds)")
    historical_volumes: List[float] = Field(
        ..., min_length=1, description="Historical contact volumes"
    )

    @field_validator("historical_volumes")
    @classmethod
    def _check_volumes_not_empty(cls, v: List[float]) -> List[float]:
        if len(v) == 0:
            raise ValueError("historical_volumes must contain at least one element")
        return v


class KPIHealthPayload(BaseModel):
    """
    Immutable schema for CX sentiment / KPI health payloads.

    All percentages are in the range [0, 100].
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    client_id: str = Field(..., min_length=1)
    timestamp: datetime
    csat: float = Field(..., ge=0, le=100, description="Customer Satisfaction score (0‑100)")
    sla_percent: float = Field(..., ge=0, le=100, description="Service Level Agreement %")
    fcr_percent: float = Field(..., ge=0, le=100, description="First Contact Resolution %")
    rolling_window_days: int = Field(..., gt=0, description="Rolling window length in days")


class AdherenceEventPayload(BaseModel):
    """
    Immutable schema for RTA floor adherence events.

    ``state_slug`` must be a lower‑case, underscore‑separated identifier
    (e.g. ``on_call``, ``break``).
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(..., min_length=1)
    state_slug: str = Field(
        ...,
        min_length=1,
        pattern=r"^[a-z_]+$",
        description="Agent state identifier (lower_case_underscored)",
    )
    duration_seconds: int = Field(..., ge=0)
    timestamp: datetime