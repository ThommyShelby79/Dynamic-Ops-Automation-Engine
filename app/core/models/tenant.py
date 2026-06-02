"""
app/core/models/tenant.py

Universal Tenant Identity Models — Pydantic v2
Defines the canonical schema for all tenant-scoped runtime context.
Every pipeline, worker, and endpoint must resolve from these models.
No hardcoded values are permitted downstream of this layer.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class TenantTier(str, Enum):
    """Billing/capacity tier that gates SLA enforcement and rate limits."""
    FREE = "free"
    STANDARD = "standard"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"

class WebhookAuthScheme(str, Enum):
    """Supported authentication schemes for outbound webhook calls."""
    NONE = "none"
    BEARER = "bearer"
    HMAC_SHA256 = "hmac_sha256"
    BASIC = "basic"

class AlertSeverity(str, Enum):
    """Severity levels used by the alerting subsystem."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    PAGE = "page"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class ErlangThresholds(BaseModel):
    """Erlang B / Erlang C traffic-engineering parameters scoped to a tenant."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    blocking_probability_target: Annotated[float, Field(gt=0.0, lt=1.0)] = 0.01
    trunk_circuit_count: Annotated[int, Field(ge=1, le=10_000)] = 30
    average_call_duration_seconds: Annotated[float, Field(gt=0.0, le=3_600.0)] = 180.0
    inter_arrival_rate_per_hour: Annotated[float, Field(gt=0.0)] = 120.0
    agent_count: Annotated[int, Field(ge=1, le=50_000)] = 10
    target_service_level_percent: Annotated[float, Field(gt=0.0, lt=100.0)] = 80.0
    service_level_threshold_seconds: Annotated[float, Field(gt=0.0, le=600.0)] = 20.0
    max_queue_wait_seconds: Annotated[float, Field(gt=0.0, le=3_600.0)] = 300.0
    overflow_enabled: bool = Field(default=True)

    @model_validator(mode="after")
    def validate_erlang_c_feasibility(self) -> ErlangThresholds:
        arrival_rate_per_second = self.inter_arrival_rate_per_hour / 3600.0
        offered_load = arrival_rate_per_second * self.average_call_duration_seconds
        if offered_load >= self.agent_count:
            raise ValueError(
                f"Erlang C system is infeasible: offered load A={offered_load:.4f} "
                f"must be strictly less than agent_count N={self.agent_count}. "
            )
        return self


class SLAConfig(BaseModel):
    """Service Level Agreement enforcement parameters for a tenant."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_requests_per_minute: Annotated[int, Field(ge=1, le=1_000_000)] = 1_000
    max_concurrent_jobs: Annotated[int, Field(ge=1, le=10_000)] = 50
    job_timeout_seconds: Annotated[float, Field(gt=0.0, le=86_400.0)] = 300.0
    retry_max_attempts: Annotated[int, Field(ge=0, le=10)] = 3
    retry_backoff_base_seconds: Annotated[float, Field(ge=0.0, le=300.0)] = 2.0
    retry_backoff_max_seconds: Annotated[float, Field(ge=1.0, le=3_600.0)] = 60.0
    alert_on_failure_count: Annotated[int, Field(ge=1)] = 5
    alert_severity: AlertSeverity = Field(default=AlertSeverity.WARNING)
    data_retention_days: Annotated[int, Field(ge=1, le=3_650)] = 90

    @model_validator(mode="after")
    def validate_backoff_ordering(self) -> SLAConfig:
        if self.retry_backoff_base_seconds > self.retry_backoff_max_seconds:
            raise ValueError("retry_backoff_base_seconds must be <= retry_backoff_max_seconds.")
        return self


class WebhookDestination(BaseModel):
    """A single outbound webhook endpoint registered for a tenant."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=128)]
    url: AnyHttpUrl
    auth_scheme: WebhookAuthScheme = Field(default=WebhookAuthScheme.NONE)
    auth_secret: Annotated[str | None, Field(default=None)] = None
    timeout_seconds: Annotated[float, Field(gt=0.0, le=60.0)] = 10.0
    events: Annotated[list[str], Field(min_length=1)]
    enabled: bool = Field(default=True)

    @model_validator(mode="after")
    def validate_auth_secret_consistency(self) -> WebhookDestination:
        scheme_requires_secret = self.auth_scheme in {
            WebhookAuthScheme.BEARER,
            WebhookAuthScheme.HMAC_SHA256,
            WebhookAuthScheme.BASIC,
        }
        if scheme_requires_secret and not self.auth_secret:
            raise ValueError(f"auth_scheme={self.auth_scheme.value} requires auth_secret.")
        if self.auth_scheme == WebhookAuthScheme.NONE and self.auth_secret is not None:
            raise ValueError("auth_secret must be None when auth_scheme=none.")
        return self

    @field_validator("events", mode="before")
    @classmethod
    def normalize_event_slugs(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(item).strip().lower() for item in v]
        raise ValueError("events must be a list of strings.")


# ---------------------------------------------------------------------------
# Tier Defaults & Builders
# ---------------------------------------------------------------------------

_TIER_SLA_DEFAULTS: dict[TenantTier, dict[str, Any]] = {
    TenantTier.FREE: {
        "max_requests_per_minute": 60,
        "max_concurrent_jobs": 2,
        "job_timeout_seconds": 60.0,
        "retry_max_attempts": 1,
        "data_retention_days": 7,
        "alert_severity": AlertSeverity.INFO,
    },
    TenantTier.STANDARD: {
        "max_requests_per_minute": 1_000,
        "max_concurrent_jobs": 50,
        "job_timeout_seconds": 300.0,
        "retry_max_attempts": 3,
        "data_retention_days": 90,
        "alert_severity": AlertSeverity.WARNING,
    },
    TenantTier.PROFESSIONAL: {
        "max_requests_per_minute": 10_000,
        "max_concurrent_jobs": 500,
        "job_timeout_seconds": 1_800.0,
        "retry_max_attempts": 5,
        "data_retention_days": 365,
        "alert_severity": AlertSeverity.CRITICAL,
    },
    TenantTier.ENTERPRISE: {
        "max_requests_per_minute": 100_000,
        "max_concurrent_jobs": 5_000,
        "job_timeout_seconds": 3_600.0,
        "retry_max_attempts": 10,
        "data_retention_days": 2_555,
        "alert_severity": AlertSeverity.PAGE,
    },
}

def build_sla_for_tier(tier: TenantTier, overrides: dict[str, Any] | None = None) -> SLAConfig:
    """Construct an SLAConfig from a tier's defaults, optionally merged with overrides."""
    base = dict(_TIER_SLA_DEFAULTS[tier])
    if overrides:
        base.update(overrides)
    return SLAConfig(**base)


# ---------------------------------------------------------------------------
# Core Tenant Context
# ---------------------------------------------------------------------------

class TenantContext(BaseModel):
    """Canonical runtime identity for a tenant across the entire engine."""
    model_config = ConfigDict(frozen=True, extra="ignore")

    tenant_id: str = Field(..., min_length=1)
    tenant_name: str = Field(..., min_length=1)
    tier: TenantTier = Field(default=TenantTier.STANDARD)
    is_active: bool = Field(default=True)

    sla_overrides: dict[str, Any] = Field(default_factory=dict)
    erlang: ErlangThresholds = Field(...)
    webhooks: list[WebhookDestination] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Phase 7.2 — API Key Security
    api_keys: list[str] = Field(
        default_factory=list,
        description="Valid API keys for this tenant. Validated on every ingest request.",
    )

    def get_active_webhooks_for_event(self, event_type: str) -> list[WebhookDestination]:
        canonical_event = event_type.lower().strip()
        active = []
        for wh in self.webhooks:
            if not wh.enabled:
                continue
            if "*" in wh.events or canonical_event in [e.lower().strip() for e in wh.events]:
                active.append(wh)
        return active

    def __str__(self) -> str:
        return f"TenantContext(id={self.tenant_id}, tier={self.tier.value}, active={self.is_active})"