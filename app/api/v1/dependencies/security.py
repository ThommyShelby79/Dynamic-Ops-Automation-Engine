"""
app/api/v1/dependencies/security.py

Phase 7.2 — API Key Security Dependency
Provides the `verify_api_key` FastAPI dependency, which must be injected into
every tenant-scoped endpoint that requires authenticated access.

Authentication contract:
  - The caller MUST supply both X-Tenant-ID and X-API-Key request headers.
  - X-Tenant-ID is already resolved to a TenantContext by TenantMiddleware and
    stored in request.state.tenant — this dependency reuses that resolved context
    rather than performing a second registry lookup.
  - The value of X-API-Key is checked against tenant.api_keys (exact match, O(n)
    linear scan over a short list; upgrade to a set on TenantContext if lists grow
    large enough to matter).
  - All failure modes return HTTP 401 with a consistent ErrorEnvelope body so that
    callers cannot enumerate valid tenant IDs or distinguish missing-tenant from
    wrong-key errors.

Design notes:
  - The dependency does NOT re-invoke ConfigManager.get_tenant() because
    TenantMiddleware already did so and stored the result in request.state. A second
    lookup would be redundant I/O and could cause cache-bypass races during TTL
    expiry windows.
  - Constant-time comparison (hmac.compare_digest) is used to prevent timing
    side-channel attacks that would allow an attacker to incrementally guess a valid
    key character-by-character.
  - The dependency returns the validated TenantContext so endpoints can declare it
    as a typed parameter via `Depends(verify_api_key)` without needing a separate
    `request.state` access.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from app.core.models.tenant import TenantContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Header extractors
# FastAPI's APIKeyHeader handles the OpenAPI schema annotation automatically,
# so the security scheme will appear correctly in /docs.
# auto_error=False lets us emit a single, unified 401 rather than FastAPI's
# default 403 for a missing header.
# ---------------------------------------------------------------------------

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_tenant_id_header = APIKeyHeader(name="X-Tenant-ID", auto_error=False)

# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

async def verify_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Depends(_api_key_header)] = None,
    x_tenant_id: Annotated[str | None, Depends(_tenant_id_header)] = None,
) -> TenantContext:
    """
    FastAPI dependency that enforces API key authentication for a tenant.

    Resolves the TenantContext from request.state (already populated by
    TenantMiddleware) and validates the supplied X-API-Key against the
    tenant's registered key list.

    Args:
        request:      The active FastAPI/Starlette request object.
        x_api_key:    Value of the X-API-Key header, or None if absent.
        x_tenant_id:  Value of the X-Tenant-ID header, or None if absent.
                      Sourced here only for log correlation; resolution was
                      already performed upstream by TenantMiddleware.

    Returns:
        The authenticated TenantContext for downstream endpoint use.

    Raises:
        HTTPException(401): On any authentication failure — missing header,
                            tenant with no keys configured, or key mismatch.
    """
    _UNAUTHORIZED = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key.",
        headers={"WWW-Authenticate": "ApiKey"},
    )

    # ── 1. Require the X-API-Key header to be present ───────────────
    if not x_api_key:
        logger.warning(
            "API key authentication failed: X-API-Key header absent "
            "[tenant_id=%s, path=%s]",
            x_tenant_id or "unknown",
            request.url.path,
        )
        raise _UNAUTHORIZED

    # ── 2. Retrieve TenantContext from request.state ─────────────────
    # TenantMiddleware runs before any dependency and guarantees this is set
    # for all non-exempt paths. A missing state attribute indicates a routing
    # misconfiguration, not a client error, so we raise 500 explicitly.
    tenant: TenantContext | None = getattr(request.state, "tenant", None)
    if tenant is None:
        logger.error(
            "verify_api_key: TenantContext not found in request.state. "
            "Ensure TenantMiddleware is registered and this path is not exempt. "
            "[path=%s]",
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal configuration error. Please contact support.",
        )

    # ── 3. Tenant must have at least one registered key ──────────────
    if not tenant.api_keys:
        logger.warning(
            "API key authentication failed: tenant has no api_keys configured "
            "[tenant_id=%s]",
            tenant.tenant_id,
        )
        raise _UNAUTHORIZED

    # ── 4. Constant-time comparison against all registered keys ──────
    # hmac.compare_digest prevents timing side-channels. We encode both sides
    # to bytes as required by the stdlib implementation.
    provided_bytes = x_api_key.encode("utf-8")
    authenticated = any(
        hmac.compare_digest(provided_bytes, registered_key.encode("utf-8"))
        for registered_key in tenant.api_keys
    )

    if not authenticated:
        logger.warning(
            "API key authentication failed: key mismatch "
            "[tenant_id=%s, path=%s]",
            tenant.tenant_id,
            request.url.path,
        )
        raise _UNAUTHORIZED

    logger.debug(
        "API key authenticated successfully [tenant_id=%s]",
        tenant.tenant_id,
    )
    return tenant